"""Installable CLI using the locally generated FIMBox SDK."""

import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit
from uuid import UUID

import click
import httpx
import keyring
from keyring.backend import KeyringBackend
from keyring.backends.chainer import ChainerBackend
from keyring.errors import KeyringError, PasswordDeleteError

from fimbox_login import begin_login
from fimbox_sdk import AuthenticatedClient, Client
from fimbox_sdk.api.authentication import (
    authentication_config_auth_config_get, current_session_auth_session_get, logout_auth_logout_post,
)
from fimbox_sdk.api.default import (
    cancel_job_jobs_job_id_delete,
    create_workspace_workspaces_post,
    get_job_status_jobs_job_id_get,
    health_health_get,
    list_jobs_jobs_get,
    load_workspace_workspaces_open_post,
    submit_hand_fim_jobs_hand_fim_post,
    update_job_display_name_jobs_job_id_patch,
)
from fimbox_sdk.models import (
    AuthenticationConfig, HandFimJobRequest, HandFimJobRequestExecutionMode, JobDisplayNameUpdate, Scenario,
    Workspace, WorkspaceCreate, WorkspaceOpen,
)
from fimbox_sdk.types import UNSET, Response


@dataclass
class Settings:
    api_url: str
    session_token: str | None
    timeout: float
    api_config: AuthenticationConfig | None = None
    workspace_location: str | None = None


def __workspace_preferences() -> tuple[Path, dict[str, str]]:
    path = Path(click.get_app_dir("fimbox")) / "workspace.json"
    try:
        preferences = json.loads(path.read_text()) if path.exists() else {}
    except (OSError, ValueError):
        raise click.ClickException("Cannot read the saved FIMBox workspace location.") from None
    if not isinstance(preferences, dict) or any(
        not isinstance(key, str) or not isinstance(value, str) for key, value in preferences.items()
    ):
        raise click.ClickException("The saved FIMBox workspace preferences are invalid.")
    return path, preferences


def __valid_workspace_location(location: str) -> bool:
    try:
        parts = urlsplit(location)
    except ValueError:
        return False
    segments = parts.path.strip("/").split("/")
    return (
        parts.scheme == "s3"
        and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,254}", parts.netloc) is not None
        and not parts.query
        and not parts.fragment
        and not parts.username
        and not parts.password
        and len(parts.path.strip("/").encode("utf-8")) <= 700
        and all(segment not in ("", ".", "..") for segment in segments)
    )


def __workspace_location(settings: Settings) -> str | None:
    location = settings.workspace_location
    if location is None:
        location = __workspace_preferences()[1].get(settings.api_url)
    if location is not None and not __valid_workspace_location(location):
        raise click.ClickException("The selected workspace location is invalid.")
    return location


def __save_workspace(settings: Settings, location: str | None) -> None:
    path, preferences = __workspace_preferences()
    if location is None:
        preferences.pop(settings.api_url, None)
    else:
        preferences[settings.api_url] = location
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(preferences, indent=2) + "\n")
    except OSError:
        raise click.ClickException(
            "The workspace request succeeded, but its location could not be saved."
        ) from None


def __credential_store() -> KeyringBackend:
    backend = keyring.get_keyring()
    if isinstance(backend, ChainerBackend):
        candidates = backend.backends
    else:
        candidates = [backend]
    for candidate in candidates:
        if type(candidate).__module__ in {
            "keyring.backends.macOS", "keyring.backends.Windows",
            "keyring.backends.SecretService", "keyring.backends.kwallet",
        }:
            return candidate
    raise click.ClickException(
        "No supported OS credential store is available. Configure macOS Keychain, "
        "Windows Credential Locker, or Linux Secret Service/KWallet, "
        "or supply FIMBOX_SESSION_TOKEN. No plaintext fallback is used."
    )


def __stored_token(api_url: str) -> str | None:
    try:
        return __credential_store().get_password("fimbox", api_url)
    except (KeyringError, OSError):
        raise click.ClickException("Cannot read the FIMBox session from the OS credential store.") from None


def __save_token(api_url: str, token: str) -> None:
    try:
        __credential_store().set_password("fimbox", api_url, token)
    except (KeyringError, OSError):
        raise click.ClickException(
            "API request succeeded, but the session could not be saved to the OS credential store. "
            "Do not repeat a submission just to save the token."
        ) from None


def __delete_token(api_url: str) -> None:
    try:
        store = __credential_store()
        if store.get_password("fimbox", api_url) is not None:
            store.delete_password("fimbox", api_url)
    except PasswordDeleteError:
        raise click.ClickException("Could not remove the saved FIMBox session.") from None
    except (KeyringError, OSError):
        raise click.ClickException("Cannot clear the FIMBox session from the OS credential store.") from None


def __api_config(settings: Settings) -> AuthenticationConfig:
    if settings.api_config is not None:
        return settings.api_config
    try:
        with Client(
            base_url=settings.api_url, timeout=httpx.Timeout(settings.timeout), follow_redirects=False,
        ) as client:
            response = authentication_config_auth_config_get.sync_detailed(client=client)
    except httpx.RequestError:
        raise click.ClickException("Cannot reach the API to determine authentication mode.") from None
    except (ValueError, KeyError, TypeError, AttributeError):
        raise click.ClickException("API authentication configuration does not match the SDK contract.") from None
    if response.status_code != 200 or not isinstance(response.parsed, AuthenticationConfig):
        raise click.ClickException(
            f"Cannot determine API authentication mode (HTTP {response.status_code})."
        )
    if not isinstance(response.parsed.authentication_enabled, bool):
        raise click.ClickException("API returned an invalid authentication mode.")
    if not isinstance(response.parsed.require_hydroshare_output, bool):
        raise click.ClickException("API returned an invalid output requirement.")
    settings.api_config = response.parsed
    return response.parsed


def __authentication_enabled(settings: Settings) -> bool:
    return __api_config(settings).authentication_enabled


def __browser_token(settings: Settings) -> str:
    try:
        with Client(
            base_url=settings.api_url, timeout=httpx.Timeout(settings.timeout),
            follow_redirects=False,
        ) as client:
            login = begin_login(client)
            click.echo(f"Sign in with HydroShare in your browser: {login.authorization_url}", err=True)
            click.echo("Waiting for browser approval (up to 10 minutes)...", err=True)
            return login.wait()
    except (RuntimeError, ValueError, TimeoutError, PermissionError) as error:
        raise click.ClickException(str(error)) from None


def __call(
    ctx: click.Context, operation: Callable[..., Response[Any]],
    authenticated: bool = True, workspace_required: bool = False,
    expected_status: int | None = None, expected_type: type[Any] | None = None,
    **kwargs: Any,
) -> Response[Any]:
    settings: Settings = ctx.obj
    client: Client | AuthenticatedClient
    browser_login = False
    saved = False
    workspace = __workspace_location(settings) if workspace_required else None
    if workspace_required and not workspace:
        raise click.ClickException(
            "No workspace is selected. Create one with 'fimbox workspace create' or open one with "
            "'fimbox workspace open', then retry."
        )
    if workspace_required and __api_config(settings).workspace_required is not True:
        raise click.ClickException(
            "This API does not enforce workspaces. Upgrade the API before submitting or reading workspace jobs."
        )
    if authenticated:
        authenticated = __authentication_enabled(settings)
        if not authenticated:
            raise click.ClickException(
                "This command requires an authenticated API. Anonymous local mode is not supported."
            )
    if authenticated:
        token = settings.session_token
        if not token:
            token = __stored_token(settings.api_url)
            saved = token is not None
        if not token:
            token = __browser_token(settings)
            browser_login = True
        if not token.strip():
            raise click.ClickException("A FIMBox session token is required.")
        client = AuthenticatedClient(
            base_url=settings.api_url, token=token.strip(),
            timeout=httpx.Timeout(settings.timeout), follow_redirects=False,
        )
    else:
        client = Client(
            base_url=settings.api_url, timeout=httpx.Timeout(settings.timeout),
            follow_redirects=False,
        )
    if workspace:
        client = client.with_headers({"X-FIMBox-Workspace": workspace})
    try:
        with client:
            response = operation(client=client, **kwargs)
    except httpx.RequestError:
        raise click.ClickException(
            "Cannot reach the API. Check --api-url and that the API service is running."
        ) from None
    except (ValueError, KeyError, TypeError, AttributeError):
        raise click.ClickException("API response does not match the generated SDK contract.") from None
    if not 200 <= response.status_code < 300:
        if response.status_code == 401 and saved:
            __delete_token(settings.api_url)
        detail = response.status_code.phrase
        try:
            body = json.loads(response.content)
            if isinstance(body, dict) and "detail" in body:
                detail = json.dumps(body["detail"], ensure_ascii=True)
        except ValueError:
            pass
        if 300 <= response.status_code < 400:
            detail = "API redirects are not followed; check --api-url."
        raise click.ClickException(f"API returned HTTP {response.status_code}: {detail}")
    if expected_status is not None and response.status_code != expected_status:
        raise click.ClickException(
            f"API returned HTTP {response.status_code}; expected HTTP {expected_status}."
        )
    if expected_type is not None and not isinstance(response.parsed, expected_type):
        raise click.ClickException("API response does not match the generated SDK contract.")
    if response.content:
        try:
            click.echo(json.dumps(json.loads(response.content), indent=2, ensure_ascii=True))
        except ValueError:
            raise click.ClickException("API returned a non-JSON response.") from None
    if operation is logout_auth_logout_post.sync_detailed:
        __delete_token(settings.api_url)
    elif browser_login:
        __save_token(settings.api_url, token.strip())
    return response


def __body(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise click.ClickException("Request body must be a readable JSON file.") from None


def __scenarios(payload: Any) -> list[Scenario]:
    try:
        if not isinstance(payload, list) or not payload:
            raise ValueError
        scenarios = [Scenario.from_dict(item) for item in payload]
        if [scenario.to_dict() for scenario in scenarios] != payload:
            raise ValueError
        return scenarios
    except (ValueError, KeyError, TypeError, AttributeError):
        raise click.ClickException(
            "Body must be a nonempty scenario JSON array matching the generated SDK models. "
            "Unknown fields are not accepted."
        ) from None


def __interactive_scenarios() -> list[Scenario]:
    input_method = click.prompt(
        "Input method",
        type=click.Choice(["manual", "json"]),
        default="manual",
    )
    if input_method == "json":
        path = Path(click.prompt("Path to scenario JSON file"))
        return __scenarios(__body(path))

    scenario_id = click.prompt("Scenario ID", default="scenario1")
    huc = click.prompt("HUC-8 watershed identifier", default="03020202")
    reach_ids = [item.strip() for item in click.prompt(
        "NWM reach ID(s), comma-separated", default="11239409",
    ).split(",")]
    flow_values = [item.strip() for item in click.prompt(
        "Flow rate(s) in m3/s, comma-separated, one per reach ID", default="200.23",
    ).split(",")]

    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", scenario_id):
        raise click.ClickException("Scenario ID must be a safe filename using letters, digits, '.', '_' or '-'.")
    if not re.fullmatch(r"\d{8}", huc):
        raise click.ClickException("HAND-FIM requires an eight-digit HUC identifier.")
    if not reach_ids or any(not re.fullmatch(r"\d+", reach_id) for reach_id in reach_ids):
        raise click.ClickException("Reach IDs must be nonempty integers separated by commas.")
    if len(reach_ids) != len(flow_values):
        raise click.ClickException(
            f"Number of reach IDs ({len(reach_ids)}) must match number of flow rates ({len(flow_values)})."
        )
    try:
        flows = [float(value) for value in flow_values]
    except ValueError:
        raise click.ClickException("Flow rates must be numbers separated by commas.") from None
    if any(not math.isfinite(flow) or flow < 0 for flow in flows):
        raise click.ClickException("Flow rates must be finite, nonnegative numbers.")

    payload = [{
        "ScenarioID": scenario_id,
        "Reaches": [
            {"HUC": huc, "ReachID": reach_id, "Streamflow": flow}
            for reach_id, flow in zip(reach_ids, flows)
        ],
    }]
    return __scenarios(payload)


@click.group(name="fimbox")
@click.option("--api-url", envvar="FIMBOX_API_URL", default="http://localhost:8000", show_default=True)
@click.option("--session-token", envvar="FIMBOX_SESSION_TOKEN",
              help="Prefer FIMBOX_SESSION_TOKEN or omit to use a saved session or browser login.")
@click.option("--timeout", type=click.FloatRange(min=0, min_open=True), default=30.0, show_default=True)
@click.option("--workspace", "workspace_location", envvar="FIMBOX_WORKSPACE",
              help="Workspace location; overrides the saved workspace for this API.")
@click.pass_context
def app(
    ctx: click.Context, api_url: str, session_token: str | None, timeout: float,
    workspace_location: str | None,
) -> None:
    """Submit and manage HAND-FIM jobs in an authenticated workspace."""
    origin = urlsplit(api_url)
    if (
        origin.scheme not in ("http", "https") or not origin.netloc or origin.username or origin.password
        or origin.query or origin.fragment
    ):
        raise click.ClickException("API URL must be HTTP(S), without credentials, query, or fragment.")
    if workspace_location is not None and not __valid_workspace_location(workspace_location):
        raise click.ClickException(
            "Workspace must be an S3 location: s3://bucket/prefix."
        )
    ctx.obj = Settings(api_url.rstrip("/"), session_token, timeout)
    ctx.obj.workspace_location = workspace_location


@app.group()
def workspace() -> None:
    """Create, open, and inspect an authenticated workspace."""


@workspace.command("create")
@click.argument("location")
@click.option("--name", required=True, help="Human-readable workspace name.")
@click.pass_context
def workspace_create(ctx: click.Context, location: str, name: str) -> None:
    """Create a workspace at an empty subprefix under resource data/contents/."""
    if not __valid_workspace_location(location):
        raise click.ClickException(
            "Workspace must be an S3 location: s3://bucket/prefix."
        )
    if not name.strip():
        raise click.ClickException("Workspace name must not be empty.")
    __call(
        ctx, create_workspace_workspaces_post.sync_detailed, body=WorkspaceCreate(location=location, name=name.strip()),
        expected_status=201, expected_type=Workspace,
    )
    __save_workspace(ctx.obj, location)
    ctx.obj.workspace_location = location
    click.echo(f"Workspace created and selected: {location}")


@workspace.command("open")
@click.argument("location")
@click.pass_context
def workspace_open(ctx: click.Context, location: str) -> None:
    """Open an existing workspace and select it for this API."""
    if not __valid_workspace_location(location):
        raise click.ClickException(
            "Workspace must be an S3 location: s3://bucket/prefix."
        )
    __call(
        ctx, load_workspace_workspaces_open_post.sync_detailed, body=WorkspaceOpen(location=location),
        expected_status=200, expected_type=Workspace,
    )
    __save_workspace(ctx.obj, location)
    ctx.obj.workspace_location = location
    click.echo(f"Workspace opened and selected: {location}")


@workspace.command("show")
@click.pass_context
def workspace_show(ctx: click.Context) -> None:
    """Show the selected workspace location."""
    click.echo(__workspace_location(ctx.obj) or "No workspace is selected.")


@workspace.command("forget")
@click.pass_context
def workspace_forget(ctx: click.Context) -> None:
    """Forget the saved workspace location without deleting shared data."""
    __save_workspace(ctx.obj, None)
    click.echo("Saved workspace location forgotten. Workspace data and outputs are unchanged.")


@workspace.command("job")
@click.argument("job_id", type=click.UUID)
@click.pass_context
def workspace_job(ctx: click.Context, job_id: UUID) -> None:
    """Show a live or retained job record from the selected workspace."""
    __call(
        ctx, get_job_status_jobs_job_id_get.sync_detailed,
        workspace_required=True, job_id=job_id,
    )


@app.command()
@click.option("--body", type=click.Path(exists=True, dir_okay=False, path_type=Path),
              help="Scenario JSON array. Omit to enter HAND-FIM inputs interactively.")
@click.option("--max-procs", type=click.IntRange(min=1), default=None,
              help="Max concurrent HAND-FIM processes; JSON submissions default to worker configuration, interactive default is 1.")
@click.option("--execution-mode", type=click.Choice(["fimserv-2024", "fimserv-2026", "streaming-2024", "streaming-2026"]),
              default="fimserv-2024", show_default=True,
              help="streaming-* range-reads a prepared cloud-optimized hydrofabric and writes tiled COGs. "
                   "*-2026 modes use NOAA inundation-mapping v4.8.14.0.")
@click.option("--display-name", help="Optional human-readable name shown in workspace job history.")
@click.pass_context
def submit(
    ctx: click.Context, body: Path | None, max_procs: int | None, execution_mode: str,
    display_name: str | None,
) -> None:
    """Submit HAND-FIM scenarios from JSON or interactive inputs."""
    scenarios = __scenarios(__body(body)) if body is not None else __interactive_scenarios()
    if body is None and max_procs is None and execution_mode not in ("streaming-2024", "streaming-2026"):
        max_procs = click.prompt(
            "Max concurrent processes", default=1, type=click.IntRange(min=1),
        )
    __call(
        ctx, submit_hand_fim_jobs_hand_fim_post.sync_detailed,
        workspace_required=True,
        body=HandFimJobRequest(
            scenarios=scenarios,
            display_name=display_name if display_name is not None else UNSET,
            output_prefix=UNSET,
            overwrite=UNSET,
            max_procs=max_procs if max_procs is not None else UNSET,
            # The default mode is omitted from the request.
            execution_mode=(
                HandFimJobRequestExecutionMode(execution_mode) if execution_mode != "fimserv-2024" else UNSET
            ),
        ),
    )


@app.command()
@click.pass_context
def jobs(ctx: click.Context) -> None:
    """List the active workspace's jobs and their submitters."""
    __call(ctx, list_jobs_jobs_get.sync_detailed, workspace_required=True)


@app.command()
@click.option("--job-id", required=True, type=click.UUID)
@click.pass_context
def status(ctx: click.Context, job_id: UUID) -> None:
    """Get a job's status and output manifest."""
    __call(ctx, get_job_status_jobs_job_id_get.sync_detailed, workspace_required=True, job_id=job_id)


@app.command()
@click.option("--job-id", required=True, type=click.UUID)
@click.option("--display-name", required=True, help="New human-readable job name.")
@click.pass_context
def rename(ctx: click.Context, job_id: UUID, display_name: str) -> None:
    """Rename a job in the active workspace."""
    __call(
        ctx, update_job_display_name_jobs_job_id_patch.sync_detailed,
        workspace_required=True, job_id=job_id,
        body=JobDisplayNameUpdate(display_name=display_name),
    )


@app.command()
@click.option("--job-id", required=True, type=click.UUID)
@click.pass_context
def cancel(ctx: click.Context, job_id: UUID) -> None:
    """Cancel one of your queued jobs in the selected workspace."""
    __call(ctx, cancel_job_jobs_job_id_delete.sync_detailed, workspace_required=True, job_id=job_id)


@app.command()
@click.pass_context
def session(ctx: click.Context) -> None:
    """Show the current authenticated identity and session expiry."""
    __call(ctx, current_session_auth_session_get.sync_detailed)


@app.command()
@click.pass_context
def logout(ctx: click.Context) -> None:
    """Revoke the current FIMBox session."""
    __call(ctx, logout_auth_logout_post.sync_detailed)


@app.command()
@click.pass_context
def health(ctx: click.Context) -> None:
    """Check API health without sending a session token."""
    __call(ctx, health_health_get.sync_detailed, authenticated=False)


if __name__ == "__main__":
    app()
