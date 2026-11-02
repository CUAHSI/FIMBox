from datetime import datetime, timezone
from uuid import UUID

from .auth import get_redis_client
from .config import settings

JOB_TIMING_PREFIX = "jobs:timing:"


def __job_timing_key(job_id: str) -> str:
    return JOB_TIMING_PREFIX + str(UUID(job_id))


def mark_job_time(job_id: str, field: str) -> datetime:
    """Record started_at or finished_at for a job's execution; return the timestamp.

    Arguments:
        job_id: Celery task ID of the job.
        field: Either "started_at" or "finished_at".
    Returns:
        The recorded UTC timestamp.
    """
    now = datetime.now(timezone.utc)
    key = __job_timing_key(job_id)
    client = get_redis_client()
    client.hset(key, field, now.isoformat())
    client.expire(key, settings.result_ttl_seconds)
    return now


def clear_job_times(job_id: str) -> None:
    get_redis_client().delete(__job_timing_key(job_id))


def read_job_times(job_id: str) -> dict[str, datetime]:
    """Return the recorded started_at/finished_at timestamps that exist for a job."""
    values = get_redis_client(decode_responses=True).hgetall(__job_timing_key(job_id))
    return {
        name: datetime.fromisoformat(value)
        for name, value in values.items() if name in {"started_at", "finished_at"}
    }
