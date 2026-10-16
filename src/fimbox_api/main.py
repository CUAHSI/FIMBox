import logging
from collections.abc import Awaitable, Callable
from typing import Annotated, Any
from uuid import UUID, uuid4

from botocore.exceptions import BotoCoreError, ClientError
from fastapi import Depends, FastAPI, HTTPException, Path, Query, Request, Response
from fastapi.responses import JSONResponse
from fastapi.openapi.utils import get_openapi
from kombu.exceptions import KombuError
from redis.exceptions import RedisError

from .auth import (
    UserSession,
    get_authenticated_session,
    list_user_jobs,
    track_user_job,
    untrack_user_job,
    user_has_job,
)
from .auth_routes import router as auth_router
from .celery_app import celery_app
from .config import settings
from .models import (
    HandFimJobOptions,
    HandFimJobRequest,
    HandFimParameters,
    JobLogs,
    JobList,
    JobSummary,
    JobStatus,
    JobSubmitted,
    RasterInfo,
    StoredHandFimResult,
)
from .storage import (
    StorageCredentialsError,
    create_job_credentials,
    delete_job_credentials,
    RasterSource,
    raster_source,
)
from .rasters import RasterReadError, raster_info, raster_tile
from .job_logs import read_job_logs
from .tasks import hand_fim

logger = logging.getLogger(__name__)
app = FastAPI(
    title="FIMBox HAND-FIM API",
    version="0.1.0",
    swagger_ui_init_oauth={
        "clientId": settings.hydroshare_client_id,
        "usePkceWithAuthorizationCodeGrant": True,
        "scopes": settings.hydroshare_scope,
    },
    swagger_ui_oauth2_redirect_url="/docs/oauth2-redirect",
)
app.include_router(auth_router)
AuthenticatedSession = Annotated[UserSession, Depends(get_authenticated_session)]


@app.middleware("http")
async def protect_login_responses(
    request: Request, call_next: Callable[[Request], Awaitable[Response]],
) -> Response:
    response = await call_next(request)
    if request.url.path.startswith("/auth/device/") or request.url.path == "/auth/callback":
        response.headers["Cache-Control"] = "no-store"
        response.headers["Referrer-Policy"] = "no-referrer"
    return response


def __openapi() -> dict[str, Any]:
    if app.openapi_schema is None:
        schema = get_openapi(title=app.title, version=app.version, routes=app.routes)
        for path, path_item in schema["paths"].items():
            if path.startswith("/jobs"):
                for operation in path_item.values():
                    if isinstance(operation, dict) and "security" in operation:
                        operation["security"].append({})
                        operation["description"] = (
                            operation.get("description", "")
                            + "\nAuthentication is required only when the server has OAuth credentials configured. "
                            "See GET /auth/config."
                        ).strip()
        app.openapi_schema = schema
    return app.openapi_schema


app.openapi = __openapi


@app.exception_handler(KombuError)
@app.exception_handler(RedisError)
def queue_error_handler(_request: Request, error: Exception) -> JSONResponse:
    logger.error("Job queue unavailable: %s", error, exc_info=error)
    return JSONResponse(status_code=503, content={"detail": "Job queue unavailable"})


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.exception_handler(BotoCoreError)
@app.exception_handler(ClientError)
@app.exception_handler(StorageCredentialsError)
def storage_error_handler(_request: Request, error: Exception) -> JSONResponse:
    logger.error("Output storage unavailable", exc_info=error)
    return JSONResponse(status_code=503, content={"detail": "Output storage unavailable"})


def __submit_hand_fim(
    parameters: HandFimParameters,
    destination: HandFimJobOptions,
    session: UserSession,
) -> JobSubmitted:
    if settings.require_hydroshare_output and destination.output_bucket is None:
        raise HTTPException(status_code=422, detail=[{
            "loc": ["body", "output_bucket"],
            "msg": "This deployment requires a HydroShare output_bucket",
            "type": "value_error",
        }])
    job_id = str(uuid4())
    if destination.output_bucket is not None:
        if not settings.authentication_enabled:
            raise HTTPException(status_code=409, detail="HydroShare S3 output requires configured authentication")
        create_job_credentials(UUID(job_id), session)
    try:
        track_user_job(session.user_id, job_id)
        hand_fim.apply_async(
            args=[
                job_id,
                parameters.model_dump(mode="json"),
                destination.output_bucket,
                destination.output_prefix,
                destination.max_procs,
                destination.overwrite,
            ],
            task_id=job_id,
            retry=False,
        )
    except (KombuError, RedisError, OSError):
        if destination.output_bucket is not None:
            delete_job_credentials(UUID(job_id))
        untrack_user_job(session.user_id, job_id)
        raise
    logger.info("Enqueued HAND-FIM job %s", job_id)
    return JobSubmitted(
        job_id=job_id, status="queued", status_url=f"/jobs/{job_id}"
    )


@app.post("/jobs/hand-fim", status_code=202, response_model=JobSubmitted)
def submit_hand_fim(
    body: HandFimJobRequest,
    session: AuthenticatedSession,
) -> JobSubmitted:
    """Submit a HAND-FIM job with scenarios and options in the JSON body."""
    return __submit_hand_fim(body.scenarios, body, session)


@app.get("/jobs", response_model=JobList)
def list_jobs(session: AuthenticatedSession) -> JobList:
    return JobList(jobs=[
        JobSummary(job_id=job_id, status=__job_status(celery_app.AsyncResult(job_id).state))
        for job_id in list_user_jobs(session.user_id)
    ])


def __require_job_owner(job_id: UUID, session: UserSession) -> None:
    if not user_has_job(session.user_id, str(job_id)):
        raise HTTPException(status_code=403, detail="Job not owned by authenticated user")


def __job_status(state: str) -> str:
    status_map = {
        "PENDING": "queued",
        "RECEIVED": "queued",
        "STARTED": "started",
        "RETRY": "started",
        "SUCCESS": "success",
        "FAILURE": "failure",
        "REVOKED": "revoked",
    }
    return status_map.get(state, state.lower())


@app.get("/jobs/{job_id}", response_model=JobStatus)
def get_job_status(job_id: UUID, session: AuthenticatedSession) -> JobStatus:
    __require_job_owner(job_id, session)
    result = celery_app.AsyncResult(str(job_id))
    state = result.state
    status = __job_status(state)
    if state == "FAILURE":
        return JobStatus(job_id=str(job_id), status=status, error=str(result.result))
    if state == "SUCCESS":
        return JobStatus(job_id=str(job_id), status=status, result=result.result)
    return JobStatus(job_id=str(job_id), status=status)


@app.get("/jobs/{job_id}/logs", response_model=JobLogs)
def get_job_logs(
    job_id: UUID,
    session: AuthenticatedSession,
    offset: Annotated[int, Query(ge=0)] = 0,
    max_bytes: Annotated[int, Query(ge=4, le=1_048_576)] = 65_536,
) -> JobLogs:
    """Poll a job's worker output, continuing from the returned next_offset."""
    __require_job_owner(job_id, session)
    result = celery_app.AsyncResult(str(job_id))
    logs, next_offset = read_job_logs(str(job_id), offset, max_bytes)
    return JobLogs(
        job_id=str(job_id),
        status=__job_status(result.state),
        logs=logs,
        next_offset=next_offset,
    )


def __raster_source(job_id: UUID, file_index: int, session: UserSession) -> RasterSource:
    __require_job_owner(job_id, session)
    job = celery_app.AsyncResult(str(job_id))
    if job.state != "SUCCESS":
        raise HTTPException(status_code=409, detail="Raster viewing requires a successful job")
    result = StoredHandFimResult.model_validate(job.result)
    if file_index >= len(result.files):
        raise HTTPException(status_code=404, detail="Output file not found")
    artifact = result.files[file_index]
    if not artifact.path.lower().endswith((".cog", ".tif", ".tiff")):
        raise HTTPException(status_code=400, detail="Output file is not a raster")
    if result.output_bucket is None or artifact.key is None:
        raise HTTPException(status_code=409, detail="Raster viewing requires retained S3 output")
    return raster_source(job_id, session, result.output_bucket, artifact.key)


@app.exception_handler(RasterReadError)
def raster_error_handler(_request: Request, error: RasterReadError) -> JSONResponse:
    return JSONResponse(
        status_code=502, content={"detail": str(error)},
        headers={"Cache-Control": "no-store"},
    )


@app.get("/jobs/{job_id}/rasters/{file_index}/info", response_model=RasterInfo)
def get_raster_info(
    job_id: UUID, file_index: Annotated[int, Path(ge=0)],
    session: AuthenticatedSession, response: Response,
) -> RasterInfo:
    """Read geographic bounds without downloading the full raster."""
    source = __raster_source(job_id, file_index, session)
    response.headers["Cache-Control"] = "no-store"
    return raster_info(source)


@app.get(
    "/jobs/{job_id}/rasters/{file_index}/tiles/{z}/{x}/{y}.png",
    response_class=Response,
    responses={200: {"content": {"image/png": {"schema": {"type": "string", "format": "binary"}}}}},
)
def get_raster_tile(
    job_id: UUID, file_index: Annotated[int, Path(ge=0)],
    z: Annotated[int, Path(ge=0, le=22)],
    x: Annotated[int, Path(ge=0)],
    y: Annotated[int, Path(ge=0)],
    session: AuthenticatedSession,
) -> Response:
    """Render a 256-pixel inundation tile using range reads against S3."""
    if x >= 2 ** z or y >= 2 ** z:
        raise HTTPException(status_code=400, detail="Tile coordinates are outside this zoom level")
    source = __raster_source(job_id, file_index, session)
    return Response(
        content=raster_tile(source, z, x, y), media_type="image/png",
        headers={"Cache-Control": "no-store"},
    )


@app.delete("/jobs/{job_id}", status_code=204, response_class=Response)
def cancel_job(job_id: UUID, session: AuthenticatedSession) -> Response:
    """Revoke queued jobs; the solo worker cannot interrupt a running subprocess."""
    __require_job_owner(job_id, session)
    result = celery_app.AsyncResult(str(job_id))
    if result.state not in ("PENDING", "RECEIVED"):
        raise HTTPException(
            status_code=409, detail="Only queued jobs can be cancelled"
        )
    celery_app.control.revoke(str(job_id), terminate=False)
    logger.info("Requested cancellation of queued job %s", job_id)
    return Response(status_code=204)
