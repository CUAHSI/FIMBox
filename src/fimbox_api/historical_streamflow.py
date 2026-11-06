"""Hourly NWM retrospective and archived analysis streamflow lookups."""

import logging
import threading
import time
from datetime import datetime, timedelta

import httpx
import zstandard

from . import archive_reader, retrospective
from .archive_reader import ArchiveDeadlineExceeded as LookupDeadlineExceeded
from .archive_reader import ArchiveReadError as HistoricalStreamflowError

logger = logging.getLogger(__name__)

MAX_CONCURRENT_LOOKUPS = 2
MAX_REACHES = 100
MAX_HOURS = 24
LOOKUP_DEADLINE_SECONDS = 45
FEATURE_INDEX_TIMEOUT_SECONDS = 30
_LOOKUP_CAPACITY = threading.BoundedSemaphore(MAX_CONCURRENT_LOOKUPS)


class LookupBusy(Exception):
    """The per-process historical lookup capacity is full."""


def __remaining(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise LookupDeadlineExceeded
    return remaining


def lookup(reach_ids: list[str], start: datetime, end: datetime) -> dict:
    """Read an inclusive hourly window from retrospective and archived NWM products."""
    if not _LOOKUP_CAPACITY.acquire(blocking=False):
        raise LookupBusy
    deadline = time.monotonic() + LOOKUP_DEADLINE_SECONDS
    try:
        samples = []
        retrospective_end = min(end, retrospective.LAST_TIME)
        if start <= retrospective_end:
            try:
                samples.extend(retrospective.streamflows(
                    reach_ids, start, retrospective_end, deadline=deadline,
                ))
            except TimeoutError as error:
                raise LookupDeadlineExceeded from error
            except httpx.TimeoutException as error:
                if time.monotonic() >= deadline:
                    raise LookupDeadlineExceeded from error
                raise HistoricalStreamflowError("The NWM retrospective request timed out") from error
            except (httpx.HTTPError, zstandard.ZstdError) as error:
                raise HistoricalStreamflowError("The NWM retrospective could not be read") from error
            __remaining(deadline)
            for sample in samples:
                sample.update(source="nwm_v3_retrospective", hour_issue=None)

        archive_start = max(start, retrospective.LAST_TIME + timedelta(hours=1))
        stats = archive_reader.ReadStats()
        hour = archive_start
        while hour <= end:
            flows = archive_reader.read_hour(hour, reach_ids, deadline, stats)
            samples.append({
                "time": hour,
                "source": "nwm_archived_analysis",
                "hour_issue": "archive_hour_unavailable" if flows is None else None,
                "flows_cms": {reach_id: None for reach_id in reach_ids} if flows is None else flows,
            })
            hour += timedelta(hours=1)
        if archive_start <= end:
            logger.info(
                "Historical NWM archive lookup: indexed_hours=%d range_hours=%d missing_hours=%d "
                "requests=%d bytes=%d elapsed=%.2fs",
                stats.indexed_hours, stats.range_hours, stats.missing_hours,
                stats.requests, stats.bytes, LOOKUP_DEADLINE_SECONDS - (deadline - time.monotonic()),
            )
        __remaining(deadline)
        return {"units": "m3/s", "samples": samples}
    finally:
        _LOOKUP_CAPACITY.release()
