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
    get_job_status_jobs_job_id_get,
    health_health_get,
    list_jobs_jobs_get,
    submit_hand_fim_jobs_hand_fim_post,
)
from fimbox_sdk.models import AuthenticationConfig, HandFimJobRequest, HandFimJobRequestExecutionMode, Scenario
from fimbox_sdk.types import UNSET, Response


@dataclass
class Settings:
    api_url: str
    session_token: str | None
    timeout: float
    api_config: AuthenticationConfig | None = None


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
    authenticated: bool = True, **kwargs: Any,
) -> None:
    settings: Settings = ctx.obj
    client: Client | AuthenticatedClient
    browser_login = False
    saved = False
    if authenticated:
        authenticated = __authentication_enabled(settings)
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
    if response.content:
        try:
            click.echo(json.dumps(json.loads(response.content), indent=2, ensure_ascii=True))
        except ValueError:
            raise click.ClickException("API returned a non-JSON response.") from None
    if operation is logout_auth_logout_post.sync_detailed:
        __delete_token(settings.api_url)
    elif browser_login:
        __save_token(settings.api_url, token.strip())


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
@click.pass_context
def app(ctx: click.Context, api_url: str, session_token: str | None, timeout: float) -> None:
    """Submit and manage HAND-FIM jobs with the generated Python SDK."""
    origin = urlsplit(api_url)
    if (
        origin.scheme not in ("http", "https") or not origin.netloc or origin.username or origin.password
        or origin.query or origin.fragment
    ):
        raise click.ClickException("API URL must be HTTP(S), without credentials, query, or fragment.")
    ctx.obj = Settings(api_url.rstrip("/"), session_token, timeout)


@app.command()
@click.option("--body", type=click.Path(exists=True, dir_okay=False, path_type=Path),
              help="Scenario JSON array. Omit to enter HAND-FIM inputs interactively.")
@click.option("--output-bucket", default=None,
              help="S3 output bucket; prompted interactively when required by the API.")
@click.option("--output-prefix", default="", help="S3 key prefix; requires --output-bucket.")
@click.option("--max-procs", type=click.IntRange(min=1), default=None,
              help="Max concurrent HAND-FIM processes; JSON submissions default to worker configuration, interactive default is 1.")
@click.option("--overwrite/--no-overwrite", default=False, show_default=True,
              help="Replace existing S3 objects with matching output keys.")
@click.option("--execution-mode", type=click.Choice(["legacy", "streaming"]), default="legacy", show_default=True,
              help="streaming range-reads a prepared cloud-optimized hydrofabric and writes tiled COGs; requires --output-bucket.")
@click.pass_context
def submit(
    ctx: click.Context, body: Path | None, output_bucket: str | None,
    output_prefix: str, max_procs: int | None, overwrite: bool, execution_mode: str,
) -> None:
    """Submit HAND-FIM scenarios from JSON or interactive inputs."""
    if body is not None and output_bucket is None and output_prefix:
        raise click.ClickException("--output-prefix requires --output-bucket.")
    scenarios = __scenarios(__body(body)) if body is not None else __interactive_scenarios()
    if body is None and max_procs is None and execution_mode == "legacy":
        max_procs = click.prompt(
            "Max concurrent processes", default=1, type=click.IntRange(min=1),
        )
    if body is None:
        config = __api_config(ctx.obj)
        if config.require_hydroshare_output and output_bucket is None:
            output_bucket = click.prompt("HydroShare output bucket (required by the API)")
        if output_bucket is not None:
            if ctx.get_parameter_source("output_prefix") == click.core.ParameterSource.DEFAULT:
                output_prefix = click.prompt("Output prefix (blank for none)", default="")
            if ctx.get_parameter_source("overwrite") == click.core.ParameterSource.DEFAULT:
                overwrite = click.confirm("Replace existing output objects?", default=False)
    if output_bucket is None and output_prefix:
        raise click.ClickException("--output-prefix requires --output-bucket.")
    __call(
        ctx, submit_hand_fim_jobs_hand_fim_post.sync_detailed,
        body=HandFimJobRequest(
            scenarios=scenarios,
            output_bucket=output_bucket,
            output_prefix=output_prefix,
            max_procs=max_procs,
            overwrite=overwrite,
            # Omitted for legacy so servers without streaming support still accept the request.
            execution_mode=(
                HandFimJobRequestExecutionMode(execution_mode) if execution_mode != "legacy" else UNSET
            ),
        ),
    )


@app.command()
@click.pass_context
def jobs(ctx: click.Context) -> None:
    """List the authenticated user's job IDs and current statuses."""
    __call(ctx, list_jobs_jobs_get.sync_detailed)


@app.command()
@click.option("--job-id", required=True, type=click.UUID)
@click.pass_context
def status(ctx: click.Context, job_id: UUID) -> None:
    """Get a job's status and output manifest."""
    __call(ctx, get_job_status_jobs_job_id_get.sync_detailed, job_id=job_id)


@app.command()
@click.option("--job-id", required=True, type=click.UUID)
@click.pass_context
def cancel(ctx: click.Context, job_id: UUID) -> None:
    """Request cancellation of a queued job."""
    __call(ctx, cancel_job_jobs_job_id_delete.sync_detailed, job_id=job_id)


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
