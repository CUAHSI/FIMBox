"""Installable CLI using the locally generated FIMBox SDK."""

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit
from uuid import UUID

import click
import httpx

from fimbox_sdk import AuthenticatedClient, Client
from fimbox_sdk.api.authentication import current_session_auth_session_get, logout_auth_logout_post
from fimbox_sdk.api.default import (
    cancel_job_jobs_job_id_delete,
    get_job_file_jobs_job_id_files_index_get,
    get_job_output_jobs_job_id_outputs_index_tif_get,
    get_job_status_jobs_job_id_get,
    health_health_get,
    list_jobs_jobs_get,
    submit_hand_fim_jobs_hand_fim_post,
    submit_job_jobs_post,
)
from fimbox_sdk.models import JobRequest, Scenario
from fimbox_sdk.types import Response


@dataclass
class Settings:
    api_url: str
    session_token: str | None
    timeout: float


def __call(
    ctx: click.Context, operation: Callable[..., Response[Any]],
    authenticated: bool = True, **kwargs: Any,
) -> None:
    settings: Settings = ctx.obj
    client: Client | AuthenticatedClient
    if authenticated:
        token = settings.session_token
        if not token:
            click.echo(
                f"Get a FIMBox session token in your browser: {settings.api_url}/auth/login",
                err=True,
            )
            click.echo("After signing in, copy the access_token from the response.", err=True)
            token = click.prompt("FIMBox session token", hide_input=True)
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


@click.group(name="fimbox")
@click.option("--api-url", envvar="FIMBOX_API_URL", default="http://localhost:8000", show_default=True)
@click.option("--session-token", envvar="FIMBOX_SESSION_TOKEN", help="Prefer the environment variable or hidden prompt.")
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
@click.option("--body", required=True, type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--output-bucket", default=None, help="Optional S3 bucket; omit to keep output on the container mount.")
@click.option("--output-prefix", default="", help="S3 key prefix; requires --output-bucket.")
@click.pass_context
def submit(ctx: click.Context, body: Path, output_bucket: str | None, output_prefix: str) -> None:
    """Submit a scenario JSON array; print the queued job ID without waiting."""
    if output_bucket is None and output_prefix:
        raise click.ClickException("--output-prefix requires --output-bucket.")
    __call(
        ctx, submit_hand_fim_jobs_hand_fim_post.sync_detailed,
        body=__scenarios(__body(body)), output_bucket=output_bucket, output_prefix=output_prefix,
    )


@app.command("submit-envelope")
@click.option("--body", required=True, type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.pass_context
def submit_envelope(ctx: click.Context, body: Path) -> None:
    """Submit a JSON envelope containing payload, output_bucket, output_prefix."""
    payload = __body(body)
    try:
        request = JobRequest.from_dict(payload)
        __scenarios(payload["payload"])
        if request.to_dict() != payload:
            raise ValueError
    except (ValueError, KeyError, TypeError, AttributeError):
        raise click.ClickException("Body must match the generated JobRequest model.") from None
    __call(ctx, submit_job_jobs_post.sync_detailed, body=request)


@app.command()
@click.pass_context
def jobs(ctx: click.Context) -> None:
    """List the authenticated user's job IDs."""
    __call(ctx, list_jobs_jobs_get.sync_detailed)


@app.command()
@click.option("--job-id", required=True, type=click.UUID)
@click.pass_context
def status(ctx: click.Context, job_id: UUID) -> None:
    """Get a job's status and output manifest."""
    __call(ctx, get_job_status_jobs_job_id_get.sync_detailed, job_id=job_id)


@app.command()
@click.option("--job-id", required=True, type=click.UUID)
@click.option("--index", required=True, type=click.IntRange(min=0))
@click.pass_context
def output(ctx: click.Context, job_id: UUID, index: int) -> None:
    """Get a presigned URL for a file in the job's output manifest."""
    __call(ctx, get_job_file_jobs_job_id_files_index_get.sync_detailed, job_id=job_id, index=index)


@app.command("scenario-output")
@click.option("--job-id", required=True, type=click.UUID)
@click.option("--index", required=True, type=click.IntRange(min=0))
@click.pass_context
def scenario_output(ctx: click.Context, job_id: UUID, index: int) -> None:
    """Get a presigned URL for a scenario's COG."""
    __call(ctx, get_job_output_jobs_job_id_outputs_index_tif_get.sync_detailed, job_id=job_id, index=index)


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
