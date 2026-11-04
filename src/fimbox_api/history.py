"""Workspace S3 job records, independent of Redis result and ownership expiry."""

import logging
from datetime import datetime, timezone
from typing import Any, Literal
from uuid import UUID

from botocore.exceptions import ClientError
from celery import Task
from celery.signals import task_revoked
from fastapi import HTTPException
from pydantic import ValidationError

from .auth import UserSession, get_redis_client
from .job_logs import JOB_LOG_PREFIX
from .job_resources import PeakMemorySampler
from .job_timing import mark_job_time
from .models import JobStatus, StoredHandFimResult
from .storage import output_file_key, output_s3_client, s3_client
from .workspaces import Workspace as HistoryLocation
from .workspaces import require_path

MAX_RECORD_BYTES = 32 * 1024 * 1024
logger = logging.getLogger(__name__)


class ArchivedJob(JobStatus):
    format: str = "fimbox-job-history"
    version: int = 2
    workspace_id: UUID
    user_id: str
    job_type: str
    submitted_at: datetime | None
    updated_at: datetime
    request: dict[str, Any]
    source: Literal["submission", "redis-export"] = "submission"


def __key(location: HistoryLocation, job_id: str) -> str:
    return output_file_key(location.prefix, f"jobs/{UUID(job_id)}/record.json")


def __log_key(location: HistoryLocation, job_id: str) -> str:
    return output_file_key(location.prefix, f"jobs/{UUID(job_id)}/worker.log")


def __write(client: Any, location: HistoryLocation, record: ArchivedJob, *, if_absent: bool = False) -> None:
    content = record.model_dump_json().encode()
    if len(content) > MAX_RECORD_BYTES:
        raise RuntimeError("Job history record exceeds the 32 MiB limit")
    client.put_object(
        Bucket=location.bucket, Key=__key(location, record.job_id),
        Body=content, ContentType="application/json",
        **({"IfNoneMatch": "*"} if if_absent else {}),
    )


def persist(client: Any, location: HistoryLocation, record: ArchivedJob, logs: bytes | None = None) -> None:
    """Publish a snapshot, writing retained logs before the terminal record."""
    if logs:
        client.put_object(
            Bucket=location.bucket,
            Key=__log_key(location, record.job_id),
            Body=logs, ContentType="text/plain; charset=utf-8",
        )
    record.updated_at = datetime.now(timezone.utc)
    __write(client, location, record)


def prepare(
    job_id: str, session: UserSession, location: HistoryLocation,
    job_type: str, request: dict[str, Any],
) -> dict[str, Any]:
    """Write queued metadata before enqueueing; return non-secret task kwargs."""
    now = datetime.now(timezone.utc)
    record = ArchivedJob(
        job_id=job_id, status="queued", user_id=session.user_id, job_type=job_type,
        workspace_id=location.workspace_id,
        submitted_at=now, updated_at=now, request=request,
    )
    __write(s3_client(UUID(job_id)), location, record, if_absent=True)
    return {"history_archive": {
        "location": location.model_dump(mode="json"), "record": record.model_dump(mode="json"),
    }}


def read(session: UserSession, location: HistoryLocation, job_id: str) -> ArchivedJob | None:
    client = output_s3_client(session)
    try:
        response = client.get_object(
            Bucket=location.bucket, Key=__key(location, job_id),
        )
    except ClientError as error:
        if error.response.get("Error", {}).get("Code") in {"NoSuchKey", "404", "NotFound"}:
            return None
        raise
    body = response["Body"]
    try:
        content = body.read(MAX_RECORD_BYTES + 1)
    finally:
        body.close()
    if len(content) > MAX_RECORD_BYTES:
        raise HTTPException(status_code=502, detail="Job history record exceeds the 32 MiB limit")
    try:
        record = ArchivedJob.model_validate_json(content)
    except ValidationError:
        raise HTTPException(status_code=502, detail="Invalid S3 job history record") from None
    if record.format != "fimbox-job-history" or record.version != 2 \
            or record.workspace_id != location.workspace_id or record.job_id != str(UUID(job_id)):
        raise HTTPException(status_code=403, detail="Job record does not belong to this workspace/job")
    if record.result is not None:
        result = record.result
        if result.output_bucket is None:
            raise HTTPException(status_code=502, detail="Workspace result has no output bucket")
        require_path(location, result.output_bucket, result.output_prefix)
        for key in [item.key for item in result.files] + [item.key for item in result.scenario_manifests]:
            if key is not None:
                require_path(location, result.output_bucket, key)
    return record


def list_records(session: UserSession, location: HistoryLocation) -> list[ArchivedJob]:
    """List the selected workspace's jobs, including all S3 pages."""
    client = output_s3_client(session)
    records = []
    prefix = output_file_key(location.prefix, "jobs") + "/"
    for page in client.get_paginator("list_objects_v2").paginate(Bucket=location.bucket, Prefix=prefix):
        for item in page.get("Contents", []):
            name = item["Key"][len(prefix):]
            if not name.endswith("/record.json") or name.count("/") != 1:
                continue
            try:
                job_id = str(UUID(name.split("/")[0]))
            except ValueError:
                raise HTTPException(status_code=502, detail="Invalid job identifier in history archive") from None
            record = read(session, location, job_id)
            if record is not None:
                records.append(record)
    return sorted(records, key=lambda record: record.submitted_at or record.updated_at, reverse=True)


def read_logs(
    session: UserSession, location: HistoryLocation, job_id: str, offset: int, max_bytes: int,
) -> tuple[str, int]:
    client = output_s3_client(session)
    key = __log_key(location, job_id)
    try:
        response = client.get_object(
            Bucket=location.bucket, Key=key, Range=f"bytes={offset}-{offset + max_bytes - 1}",
        )
    except ClientError as error:
        if error.response.get("Error", {}).get("Code") in {"NoSuchKey", "404", "NotFound", "InvalidRange", "416"}:
            return "", offset
        raise
    body = response["Body"]
    try:
        content = body.read(max_bytes)
    finally:
        body.close()
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as error:
        if error.reason == "unexpected end of data" and error.end == len(content):
            content = content[:error.start]
            text = content.decode("utf-8")
        else:
            text = content.decode("utf-8", errors="replace")
    return text, offset + len(content)


class HistoryTask(Task):
    """Persist task transitions without changing legacy task positional arguments."""

    abstract = True

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        archive = kwargs.pop("history_archive", None)
        if archive is None:
            job_id = self.request.id if self.request_stack is not None else None
            if job_id:
                mark_job_time(job_id, "started_at")
            try:
                with PeakMemorySampler() as memory:
                    result = super().__call__(*args, **kwargs)
            finally:
                if job_id:
                    mark_job_time(job_id, "finished_at")
            return self.__add_memory_usage(result, memory.peak_bytes)
        location = HistoryLocation.model_validate(archive["location"])
        record = ArchivedJob.model_validate(archive["record"])
        client = s3_client(UUID(record.job_id))
        record.status = "started"
        record.started_at = mark_job_time(record.job_id, "started_at")
        record.updated_at = datetime.now(timezone.utc)
        persist(client, location, record)
        try:
            with PeakMemorySampler() as memory:
                result = super().__call__(*args, **kwargs)
        except Exception as error:
            # Preserve failed jobs as well as successes; never swallow execution/archive errors.
            record.status = "failure"
            record.error = str(error)
            record.finished_at = mark_job_time(record.job_id, "finished_at")
            self.__finish(client, location, record)
            raise
        result = self.__add_memory_usage(result, memory.peak_bytes)
        record.status = "success"
        record.result = StoredHandFimResult.model_validate(result)
        record.finished_at = mark_job_time(record.job_id, "finished_at")
        self.__finish(client, location, record)
        return result

    @staticmethod
    def __add_memory_usage(result: Any, peak_bytes: int | None) -> dict[str, Any]:
        if peak_bytes is None:
            logger.warning("Peak worker memory could not be measured for this job")
        if not isinstance(result, dict):
            raise TypeError("Worker task result must be a mapping")
        return {**result, "peak_memory_bytes": peak_bytes}

    @staticmethod
    def __finish(client: Any, location: HistoryLocation, record: ArchivedJob) -> None:
        logs = get_redis_client(decode_responses=False).get(JOB_LOG_PREFIX + record.job_id)
        persist(client, location, record, logs)


def mark_cancelled(session: UserSession, location: HistoryLocation, job_id: str) -> None:
    record = read(session, location, job_id)
    if record is not None:
        record.status = "revoked"
        record.updated_at = datetime.now(timezone.utc)
        __write(output_s3_client(session), location, record)


def enqueue_failed(archive: dict[str, Any]) -> None:
    if not archive:
        return
    descriptor = archive["history_archive"]
    location = HistoryLocation.model_validate(descriptor["location"])
    record = ArchivedJob.model_validate(descriptor["record"])
    record.status = "failure"
    record.error = "Job could not be enqueued"
    record.updated_at = datetime.now(timezone.utc)
    __write(s3_client(UUID(record.job_id)), location, record)


@task_revoked.connect
def archive_revoked_task(request: Any, **kwargs: Any) -> None:
    """The worker also archives revocations made without the selecting client header."""
    descriptor = request.kwargs.get("history_archive")
    if descriptor is None:
        return
    location = HistoryLocation.model_validate(descriptor["location"])
    record = ArchivedJob.model_validate(descriptor["record"])
    record.status = "revoked"
    persist(s3_client(UUID(record.job_id)), location, record)
