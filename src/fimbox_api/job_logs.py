from uuid import UUID

from .auth import get_redis_client
from .config import settings

JOB_LOG_PREFIX = "jobs:logs:"


def __job_log_key(job_id: str) -> str:
    return JOB_LOG_PREFIX + str(UUID(job_id))


def clear_job_logs(job_id: str) -> None:
    get_redis_client(decode_responses=False).delete(__job_log_key(job_id))


def append_job_log(job_id: str, content: str) -> None:
    if not content:
        return
    client = get_redis_client(decode_responses=False)
    key = __job_log_key(job_id)
    client.append(key, content)
    client.expire(key, settings.result_ttl_seconds)


def read_job_logs(job_id: str, offset: int, max_bytes: int) -> tuple[str, int]:
    client = get_redis_client(decode_responses=False)
    key = __job_log_key(job_id)
    length = client.strlen(key)
    start = min(offset, length)
    if start == length:
        return "", start

    content = client.getrange(key, start, start + max_bytes - 1)
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as error:
        if error.reason == "unexpected end of data" and error.end == len(content):
            content = content[:error.start]
            text = content.decode("utf-8")
        else:
            text = content.decode("utf-8", errors="replace")
    return text, start + len(content)
