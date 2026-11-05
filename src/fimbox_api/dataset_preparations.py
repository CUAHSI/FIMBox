"""Authenticated submission and Redis tracking for prepared input datasets."""

import json
import logging
import re
from datetime import datetime, timezone
from hashlib import sha256
from uuid import uuid4

from pydantic import BaseModel, Field, field_validator, model_validator
from redis.exceptions import RedisError

from .auth import UserSession, get_redis_client
from .celery_app import celery_app
from .config import settings

logger = logging.getLogger(__name__)
RUN_PREFIX = "dataset:prepare:"
RUN_TTL_SECONDS = 7 * 24 * 60 * 60
BATCH_SIZE = 10
VERSION_PATTERN = r"^\d+(?:\.\d+)*$"
HUC_PATTERN = r"^\d{8}$"


class PreparationRequest(BaseModel):
    source_version: str = Field(pattern=VERSION_PATTERN, max_length=32)
    hucs: list[str] = Field(default_factory=list)
    all_hucs: bool = False

    @field_validator("hucs")
    @classmethod
    def valid_hucs(cls, value: list[str]) -> list[str]:
        if len(set(value)) != len(value) or any(not re.fullmatch(HUC_PATTERN, huc) for huc in value):
            raise ValueError("HUCs must be unique eight-digit strings")
        return value

    @model_validator(mode="after")
    def require_huc_selection(self) -> "PreparationRequest":
        if not self.all_hucs and not self.hucs:
            raise ValueError("Select one or more HUCs or choose prepare all HUCs")
        if self.all_hucs and self.hucs:
            raise ValueError("Choose either specific HUCs or all HUCs, not both")
        return self


class PreparationRun(BaseModel):
    job_id: str
    status: str
    source_version: str
    source_location: str
    dataset_name: str
    destination_location: str
    destination_bucket: str
    destination_prefix: str
    submitted_by: str
    submitted_at: str
    all_hucs: bool
    requested_huc_count: int
    celery_task_id: str | None = None
    error: str | None = None
    total_hucs: int = 0
    completed_hucs: int = 0


def __run_key(job_id: str) -> str:
    return RUN_PREFIX + job_id


def __state_key(job_id: str) -> str:
    return RUN_PREFIX + job_id + ":state"


def __completed_hucs_key(job_id: str) -> str:
    return RUN_PREFIX + job_id + ":hucs"


def __finished_hucs_key(job_id: str) -> str:
    return RUN_PREFIX + job_id + ":finished"


def __failed_hucs_key(job_id: str) -> str:
    return RUN_PREFIX + job_id + ":failed"


def __destination_key(bucket: str, prefix: str) -> str:
    destination = f"{bucket}/{prefix}".encode()
    return RUN_PREFIX + "destination:" + sha256(destination).hexdigest()


def __source_location(version: str) -> str:
    prefix = f"hand_fim_{version.replace('.', '_')}/"
    return f"s3://ciroh-owp-hand-fim/{prefix}"


def __serialize_session(session: UserSession) -> dict[str, str | float | None]:
    return {
        "user_id": session.user_id,
        "access_token": session.access_token.get_secret_value(),
        "refresh_token": session.refresh_token.get_secret_value() if session.refresh_token else None,
        "scope": session.scope,
        "expires_at": session.expires_at,
    }


def submit(session: UserSession, request: PreparationRequest) -> PreparationRun:
    if "write" not in session.scope.split():
        raise PermissionError("HydroShare write access is required to prepare the input dataset")
    bucket = settings.stream_input_bucket
    prefix = settings.stream_input_prefix.strip("/")
    if not bucket or not prefix:
        raise RuntimeError("No input dataset location is configured")

    job_id = str(uuid4())
    submitted_at = datetime.now(timezone.utc).isoformat()
    context = {
        "job_id": job_id,
        "source_version": request.source_version,
        "source_location": __source_location(request.source_version),
        "dataset_name": "Input dataset",
        "hucs": request.hucs,
        "all_hucs": request.all_hucs,
        "destination_bucket": bucket,
        "destination_prefix": prefix,
        "destination_location": f"s3://{bucket}/{prefix}",
        "submitted_by": session.user_id,
        "submitted_at": submitted_at,
        "session": __serialize_session(session),
    }
    client = get_redis_client()
    destination_key = __destination_key(bucket, prefix)
    if not client.set(destination_key, job_id, ex=RUN_TTL_SECONDS, nx=True):
        raise FileExistsError("A preparation is already running for the input dataset")
    try:
        with client.pipeline(transaction=True) as pipeline:
            pipeline.set(__run_key(job_id), json.dumps(context), ex=RUN_TTL_SECONDS)
            pipeline.hset(__state_key(job_id), mapping={"status": "queued"})
            pipeline.expire(__state_key(job_id), RUN_TTL_SECONDS)
            pipeline.execute()
    except RedisError:
        client.delete(destination_key)
        raise

    try:
        celery_app.send_task(
            "fimbox_api.tasks.prepare_dataset",
            args=[job_id],
            task_id=job_id,
            retry=False,
        )
    except Exception as error:
        logger.error("Celery dataset preparation submission failed (%s)", type(error).__name__)
        __set_state(job_id, status="failed", error="Celery could not queue the dataset preparation")
        client.delete(destination_key)
        raise RuntimeError("Celery could not queue the dataset preparation") from None
    __set_state(job_id, celery_task_id=job_id)
    return get_run(job_id, session.user_id)


def __set_state(job_id: str, **fields: str) -> None:
    client = get_redis_client()
    client.hset(__state_key(job_id), mapping=fields)
    client.expire(__state_key(job_id), RUN_TTL_SECONDS)


def __is_cancelled(job_id: str) -> bool:
    return get_redis_client().hget(__state_key(job_id), "status") == "cancelled"


def is_cancelled(job_id: str) -> bool:
    return __is_cancelled(job_id)


def cancel(job_id: str, user_id: str) -> PreparationRun:
    """Cancel an unfinished run, release the input dataset reservation and revoke queued tasks."""

    client = get_redis_client()
    run = get_run(job_id, user_id)
    if run.status not in ("queued", "running"):
        raise FileExistsError("Only queued or running preparations can be cancelled")
    __set_state(job_id, status="cancelled", error="Cancelled by the user")
    destination_key = __destination_key(run.destination_bucket, run.destination_prefix)
    if client.get(destination_key) == job_id:
        client.delete(destination_key)
    try:
        celery_app.control.revoke(job_id)
    except Exception as error:
        logger.warning("Celery revoke failed (%s)", type(error).__name__)
    return get_run(job_id, user_id)


def get_run(job_id: str, user_id: str) -> PreparationRun:
    client = get_redis_client()
    raw = client.get(__run_key(job_id))
    if raw is None:
        raise KeyError("Dataset preparation job not found")
    context = json.loads(raw)
    if context.get("submitted_by") != user_id:
        raise KeyError("Dataset preparation job not found")
    state = client.hgetall(__state_key(job_id))
    return PreparationRun(
        job_id=job_id,
        status=state.get("status", "queued"),
        source_version=context["source_version"],
        source_location=context["source_location"],
        dataset_name=context["dataset_name"],
        destination_location=context["destination_location"],
        destination_bucket=context["destination_bucket"],
        destination_prefix=context["destination_prefix"],
        submitted_by=context["submitted_by"],
        submitted_at=context["submitted_at"],
        all_hucs=context["all_hucs"],
        requested_huc_count=len(context["hucs"]),
        celery_task_id=state.get("celery_task_id") or None,
        error=state.get("error") or None,
        total_hucs=int(state.get("total_hucs", 0)),
        completed_hucs=client.scard(__completed_hucs_key(job_id)),
    )


def load_run_context(job_id: str) -> dict:
    raw = get_redis_client().get(__run_key(job_id))
    if raw is None:
        raise RuntimeError("Preparation session expired or was not found")
    return json.loads(raw)


def mark_running(job_id: str, total_hucs: int) -> None:
    if __is_cancelled(job_id):
        return
    __set_state(job_id, status="running", total_hucs=str(total_hucs))


def mark_huc_complete(job_id: str, huc: str) -> None:
    client = get_redis_client()
    key = __completed_hucs_key(job_id)
    client.sadd(key, huc)
    client.expire(key, RUN_TTL_SECONDS)
    client.srem(__failed_hucs_key(job_id), huc)
    __mark_huc_finished(client, job_id, huc)


def mark_huc_failed(job_id: str, huc: str, error: str) -> None:
    client = get_redis_client()
    failed_key = __failed_hucs_key(job_id)
    client.sadd(failed_key, huc)
    client.expire(failed_key, RUN_TTL_SECONDS)
    if not __is_cancelled(job_id):
        __set_state(job_id, status="failed", error=error[:500])
    __mark_huc_finished(client, job_id, huc)


def __mark_huc_finished(client, job_id: str, huc: str) -> None:
    finished_key = __finished_hucs_key(job_id)
    client.sadd(finished_key, huc)
    client.expire(finished_key, RUN_TTL_SECONDS)
    context = load_run_context(job_id)
    total_hucs = int(client.hget(__state_key(job_id), "total_hucs") or 0)
    if __is_cancelled(job_id):
        return
    if total_hucs and client.scard(finished_key) >= total_hucs:
        if client.scard(__failed_hucs_key(job_id)):
            __set_state(job_id, status="failed")
        else:
            __set_state(job_id, status="completed", error="")
        client.delete(__destination_key(context["destination_bucket"], context["destination_prefix"]))


def mark_failed(job_id: str, error: str) -> None:
    if __is_cancelled(job_id):
        return
    __set_state(job_id, status="failed", error=error[:500])
    try:
        context = load_run_context(job_id)
    except RuntimeError:
        return
    get_redis_client().delete(__destination_key(context["destination_bucket"], context["destination_prefix"]))
