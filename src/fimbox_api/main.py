import logging
from typing import Annotated
from uuid import UUID, uuid4

from botocore.exceptions import BotoCoreError, ClientError
from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, JSONResponse
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
    JobRequest,
    JobList,
    JobSummary,
    JobStatus,
    JobSubmitted,
    PresignedOutput,
)
from .storage import (
    StorageCredentialsError,
    create_job_credentials,
    delete_job_credentials,
    output_key,
    presign_output,
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
    job_id = str(uuid4())
    if destination.output_bucket is not None:
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


@app.post("/jobs", status_code=202, response_model=JobSubmitted)
def submit_job(body: JobRequest, session: AuthenticatedSession) -> JobSubmitted:
    """Submit HAND-FIM using the template's payload envelope."""
    return __submit_hand_fim(body.payload, body, session)


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


@app.get("/jobs/{job_id}/outputs/{index}.tif", response_model=PresignedOutput)
def get_job_output(
    job_id: UUID, index: int, session: AuthenticatedSession,
) -> PresignedOutput:
    status = get_job_status(job_id, session)
    if status.status != "success" or status.result is None:
        raise HTTPException(status_code=409, detail="Job has not completed successfully")
    if index < 0 or index >= len(status.result.outputs):
        raise HTTPException(status_code=404, detail="Output not found")
    if status.result.output_bucket is None:
        filename = f"{job_id}-{index}.cog"
        file_index = next(
            (i for i, file in enumerate(status.result.files) if file.path == filename), None,
        )
        if file_index is None:
            raise HTTPException(status_code=404, detail="Scenario output not found")
        return PresignedOutput(url=f"/jobs/{job_id}/files/{file_index}/content", expires_in=0)
    key = output_key(status.result.output_prefix, job_id, index)
    if status.result.files:
        filename = f"{job_id}-{index}.cog"
        artifact = next((file for file in status.result.files if file.path == filename), None)
        if artifact is None or artifact.key is None:
            raise HTTPException(status_code=404, detail="Scenario output not found")
        key = artifact.key
    url, expires_in = presign_output(
        job_id,
        status.result.output_bucket,
        key,
    )
    return PresignedOutput(url=url, expires_in=expires_in)


@app.get("/jobs/{job_id}/files/{index}", response_model=PresignedOutput)
def get_job_file(
    job_id: UUID, index: int, session: AuthenticatedSession,
) -> PresignedOutput:
    status = get_job_status(job_id, session)
    if status.status != "success" or status.result is None:
        raise HTTPException(status_code=409, detail="Job has not completed successfully")
    if index < 0 or index >= len(status.result.files):
        raise HTTPException(status_code=404, detail="Output file not found")
    if status.result.output_bucket is None:
        return PresignedOutput(url=f"/jobs/{job_id}/files/{index}/content", expires_in=0)
    key = status.result.files[index].key
    if key is None:
        raise HTTPException(status_code=404, detail="Output object key not found")
    url, expires_in = presign_output(
        job_id, status.result.output_bucket, key,
    )
    return PresignedOutput(url=url, expires_in=expires_in)


@app.get(
    "/jobs/{job_id}/files/{index}/content",
    response_class=FileResponse,
    responses={200: {"content": {"application/octet-stream": {"schema": {"type": "string", "format": "binary"}}}}},
)
def get_local_job_file(job_id: UUID, index: int, session: AuthenticatedSession) -> FileResponse:
    """Download a local output file from the shared mount; requires the owner's session."""
    status = get_job_status(job_id, session)
    if status.status != "success" or status.result is None:
        raise HTTPException(status_code=409, detail="Job has not completed successfully")
    if status.result.output_bucket is not None or index < 0 or index >= len(status.result.files):
        raise HTTPException(status_code=404, detail="Local output file not found")
    root = settings.hand_fim_output_dir.resolve() / "scenarios" / str(job_id)
    relative_path = status.result.files[index].path
    path = root / relative_path
    resolved = path.resolve()
    if (
        not resolved.is_relative_to(root.resolve()) or path.is_symlink()
        or any(parent.is_symlink() for parent in path.parents)
        or not resolved.is_file()
    ):
        raise HTTPException(status_code=404, detail="Local output file not found")
    return FileResponse(resolved, filename=resolved.name, media_type="application/octet-stream")


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
