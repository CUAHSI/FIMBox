"""Opt-in S3 job archives, independent of Redis result and ownership expiry."""

from datetime import datetime, timezone
from hashlib import sha256
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit
from uuid import UUID

from botocore.exceptions import ClientError
from celery import Task
from celery.signals import task_revoked
from fastapi import Depends, Header, HTTPException
from pydantic import BaseModel, Field, ValidationError, field_validator

from .auth import UserSession, get_redis_client
from .config import settings
from .job_logs import JOB_LOG_PREFIX
from .job_timing import mark_job_time
from .models import JobStatus, StoredHandFimResult
from .storage import output_file_key, output_s3_client, s3_client

MAX_RECORD_BYTES = 32 * 1024 * 1024


class HistoryLocation(BaseModel):
    bucket: str = Field(min_length=1, max_length=255, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
    prefix: str = Field(min_length=1, max_length=800)

    @field_validator("prefix")
    @classmethod
    def valid_prefix(cls, value: str) -> str:
        value = value.strip("/")
        if not value or any(part in {"", ".", ".."} for part in value.split("/")):
            raise ValueError("History prefix must have nonempty path segments without '.' or '..'")
        if len(value.encode("utf-8")) > 800:
            raise ValueError("History prefix exceeds 800 UTF-8 bytes")
        return value


class ArchivedJob(JobStatus):
    format: str = "fimbox-job-history"
    version: int = 1
    user_id: str
    job_type: str
    submitted_at: datetime | None
    updated_at: datetime
    request: dict[str, Any]
    source: Literal["submission", "redis-export"] = "submission"


class HistoryExport(BaseModel):
    exported: int = 0
    skipped_existing: int = 0
    skipped_active: int = 0


class HistoryClear(BaseModel):
    cleared: int = 0
    skipped_active: int = 0


def parse_location(value: str) -> HistoryLocation:
    parts = urlsplit(value)
    if parts.scheme != "s3" or parts.query or parts.fragment or parts.username or parts.password:
        raise ValueError("History location must be s3://bucket/prefix without credentials, query, or fragment")
    return HistoryLocation(bucket=parts.netloc, prefix=parts.path)


def selected_location(
    value: Annotated[str | None, Header(alias="X-FIMBox-History-Location")] = None,
) -> HistoryLocation | None:
    if value is None:
        return None
    if not settings.authentication_enabled:
        raise HTTPException(status_code=409, detail="Persistent S3 history requires authentication")
    try:
        return parse_location(value)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from None


HistorySelection = Annotated[HistoryLocation | None, Depends(selected_location)]


def __prefix(location: HistoryLocation, user_id: str) -> str:
    identity = sha256(user_id.encode()).hexdigest()
    return output_file_key(location.prefix, f"users/{identity}/jobs") + "/"


def __key(location: HistoryLocation, user_id: str, job_id: str) -> str:
    return __prefix(location, user_id) + str(UUID(job_id)) + ".json"


def __write(client: Any, location: HistoryLocation, record: ArchivedJob, *, if_absent: bool = False) -> None:
    content = record.model_dump_json().encode()
    if len(content) > MAX_RECORD_BYTES:
        raise RuntimeError("Job history record exceeds the 32 MiB limit")
    client.put_object(
        Bucket=location.bucket, Key=__key(location, record.user_id, record.job_id),
        Body=content, ContentType="application/json",
        **({"IfNoneMatch": "*"} if if_absent else {}),
    )


def persist(client: Any, location: HistoryLocation, record: ArchivedJob, logs: bytes | None = None) -> None:
    """Publish a snapshot, writing retained logs before the terminal record."""
    if logs:
        client.put_object(
            Bucket=location.bucket,
            Key=__key(location, record.user_id, record.job_id).removesuffix(".json") + ".log",
            Body=logs, ContentType="text/plain; charset=utf-8",
        )
    record.updated_at = datetime.now(timezone.utc)
    __write(client, location, record)


def prepare(
    job_id: str, session: UserSession, location: HistoryLocation | None,
    job_type: str, request: dict[str, Any],
) -> dict[str, Any]:
    """Write queued metadata before enqueueing; return non-secret task kwargs."""
    if location is None:
        return {}
    now = datetime.now(timezone.utc)
    record = ArchivedJob(
        job_id=job_id, status="queued", user_id=session.user_id, job_type=job_type,
        submitted_at=now, updated_at=now, request=request,
    )
    __write(s3_client(UUID(job_id)), location, record)
    return {"history_archive": {
        "location": location.model_dump(), "record": record.model_dump(mode="json"),
    }}


def read(session: UserSession, location: HistoryLocation, job_id: str) -> ArchivedJob | None:
    client = output_s3_client(session)
    try:
        response = client.get_object(
            Bucket=location.bucket, Key=__key(location, session.user_id, job_id),
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
    if record.format != "fimbox-job-history" or record.version != 1 \
            or record.user_id != session.user_id or record.job_id != str(UUID(job_id)):
        raise HTTPException(status_code=403, detail="Job history record does not belong to this user/job")
    return record


def export_snapshot(session: UserSession, location: HistoryLocation, job: JobStatus) -> bool:
    """Retain a completed Redis job without inventing missing submission metadata."""
    record = ArchivedJob(
        **job.model_dump(), user_id=session.user_id, job_type="unknown",
        submitted_at=None, updated_at=datetime.now(timezone.utc), request={},
        source="redis-export",
    )
    logs = get_redis_client().get(JOB_LOG_PREFIX + job.job_id)
    client = output_s3_client(session)
    if logs:
        try:
            client.put_object(
                Bucket=location.bucket,
                Key=__key(location, session.user_id, job.job_id).removesuffix(".json") + ".log",
                Body=logs, ContentType="text/plain; charset=utf-8", IfNoneMatch="*",
            )
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") != "PreconditionFailed":
                raise
    try:
        __write(client, location, record, if_absent=True)
    except ClientError as error:
        if error.response.get("Error", {}).get("Code") == "PreconditionFailed":
            return False
        raise
    return True


def list_records(session: UserSession, location: HistoryLocation) -> list[ArchivedJob]:
    """List only this authenticated user's namespace, including all S3 pages."""
    client = output_s3_client(session)
    records = []
    prefix = __prefix(location, session.user_id)
    for page in client.get_paginator("list_objects_v2").paginate(Bucket=location.bucket, Prefix=prefix):
        for item in page.get("Contents", []):
            name = item["Key"][len(prefix):]
            if not name.endswith(".json") or "/" in name:
                continue
            try:
                job_id = str(UUID(name[:-5]))
            except ValueError:
                raise HTTPException(status_code=502, detail="Invalid job identifier in history archive") from None
            record = read(session, location, job_id)
            if record is not None:
                records.append(record)
    return sorted(records, key=lambda record: record.submitted_at or record.updated_at, reverse=True)


def delete(session: UserSession, location: HistoryLocation, job_id: str) -> None:
    """Delete a job's archived log, then its record, from this user's namespace."""
    client = output_s3_client(session)
    key = __key(location, session.user_id, job_id)
    client.delete_object(Bucket=location.bucket, Key=key.removesuffix(".json") + ".log")
    client.delete_object(Bucket=location.bucket, Key=key)


def read_logs(
    session: UserSession, location: HistoryLocation, job_id: str, offset: int, max_bytes: int,
) -> tuple[str, int]:
    client = output_s3_client(session)
    key = __key(location, session.user_id, job_id).removesuffix(".json") + ".log"
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
                return super().__call__(*args, **kwargs)
            finally:
                if job_id:
                    mark_job_time(job_id, "finished_at")
        location = HistoryLocation.model_validate(archive["location"])
        record = ArchivedJob.model_validate(archive["record"])
        client = s3_client(UUID(record.job_id))
        record.status = "started"
        record.started_at = mark_job_time(record.job_id, "started_at")
        record.updated_at = datetime.now(timezone.utc)
        persist(client, location, record)
        try:
            result = super().__call__(*args, **kwargs)
        except Exception as error:
            # Preserve failed jobs as well as successes; never swallow execution/archive errors.
            record.status = "failure"
            record.error = str(error)
            record.finished_at = mark_job_time(record.job_id, "finished_at")
            self.__finish(client, location, record)
            raise
        record.status = "success"
        record.result = StoredHandFimResult.model_validate(result)
        record.finished_at = mark_job_time(record.job_id, "finished_at")
        self.__finish(client, location, record)
        return result

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
