import logging
from typing import Annotated, Any
from uuid import UUID, uuid4

from botocore.exceptions import BotoCoreError, ClientError
from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
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
    HandFimParameters,
    HandFimJobOptions,
    JobList,
    JobSummary,
    JobStatus,
    JobSubmitted,
)
from .storage import (
    StorageCredentialsError,
    create_job_credentials,
    delete_job_credentials,
)
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


def __openapi() -> dict[str, Any]:
    if app.openapi_schema is None:
        schema = get_openapi(title=app.title, version=app.version, routes=app.routes)
        for path, path_item in schema["paths"].items():
            if path.startswith("/jobs") or path == "/auth/session":
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
        raise HTTPException(status_code=422, detail="This deployment requires a HydroShare output_bucket")
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
    body: HandFimParameters,
    destination: Annotated[HandFimJobOptions, Query()],
    session: AuthenticatedSession,
) -> JobSubmitted:
    """Submit the scenario JSON array directly, without an envelope."""
    return __submit_hand_fim(body, destination, session)


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
