"""Hourly NWM v3.0 retrospective streamflow for one reach.

Reads the public NOAA Zarr store on AWS (CONUS chrtout, 1979-02-01 to 2023-02-01) with
plain HTTPS GETs: the feature_id index once per process, then one 672-hour chunk
(~6-12 MB compressed) per 28 days requested.
"""

import functools
import math
import time
from datetime import UTC, datetime, timedelta

import httpx
import numpy as np
import zstandard

ZARR_URL = "https://noaa-nwm-retrospective-3-0-pds.s3.amazonaws.com/CONUS/zarr/chrtout.zarr"
FIRST_TIME = datetime(1979, 2, 1, 1, tzinfo=UTC)
TIME_STEPS = 385704
TIME_CHUNK = 672
FEATURE_CHUNK = 30000
SCALE_FACTOR = 0.01
MISSING_VALUE = -999900
MAX_DAYS = 92
TIMEOUT_SECONDS = 60
FEATURE_INDEX_TIMEOUT_SECONDS = 30
LAST_TIME = FIRST_TIME + timedelta(hours=TIME_STEPS - 1)


class RetrospectiveError(ValueError):
    """The request cannot be answered from the retrospective dataset."""


def __read(client: httpx.Client, key: str, dtype: str, timeout: float | None = None) -> np.ndarray:
    options = {} if timeout is None else {"timeout": timeout}
    response = client.get(f"{ZARR_URL}/{key}", **options)
    response.raise_for_status()
    return np.frombuffer(zstandard.ZstdDecompressor().decompress(response.content), dtype)


@functools.lru_cache(maxsize=1)
def __feature_ids() -> np.ndarray:
    with httpx.Client(timeout=FEATURE_INDEX_TIMEOUT_SECONDS, trust_env=False) as client:
        return __read(client, "feature_id/0", "<i8")


def streamflow(reach_id: int, start: datetime, end: datetime) -> dict:
    """Hourly retrospective streamflow (m³/s) for a reach over an inclusive UTC time range.

    Arguments:
        reach_id: NWM feature_id (NHDPlus COMID).
        start: First hour wanted; clipped to the dataset start.
        end: Last hour wanted; clipped to the dataset end.

    Returns:
        A dict with the clipped start/end, the hourly flows (None where missing), and
        min/mean/max statistics with the time of the maximum.
    """
    start, end = max(start, FIRST_TIME), min(end, LAST_TIME)
    if end < start:
        raise RetrospectiveError(
            f"Choose dates between {FIRST_TIME:%Y-%m-%d} and {LAST_TIME:%Y-%m-%d} (NWM v3.0 retrospective)"
        )
    if end - start > timedelta(days=MAX_DAYS):
        raise RetrospectiveError(f"Choose a range of at most {MAX_DAYS} days")
    first = math.ceil((start - FIRST_TIME) / timedelta(hours=1))
    last = math.floor((end - FIRST_TIME) / timedelta(hours=1))

    ids = __feature_ids()
    column = int(np.searchsorted(ids, reach_id))
    if column >= len(ids) or ids[column] != reach_id:
        raise RetrospectiveError(f"Reach {reach_id} is not in the NWM v3.0 retrospective")

    parts = []
    with httpx.Client(timeout=TIMEOUT_SECONDS, trust_env=False) as client:
        for chunk in range(first // TIME_CHUNK, last // TIME_CHUNK + 1):
            values = __read(client, f"streamflow/{chunk}.{column // FEATURE_CHUNK}", "<i4")
            values = values.reshape(TIME_CHUNK, FEATURE_CHUNK)[:, column % FEATURE_CHUNK]
            offset = chunk * TIME_CHUNK
            parts.append(values[max(first - offset, 0):last - offset + 1])
    raw = np.concatenate(parts)
    flows = np.where(raw == MISSING_VALUE, np.nan, raw * SCALE_FACTOR)
    if np.isnan(flows).all():
        raise RetrospectiveError(f"No retrospective streamflow for reach {reach_id} in this range")

    peak = int(np.nanargmax(flows))
    first_time = FIRST_TIME + timedelta(hours=first)
    return {
        "reach_id": reach_id,
        "start": first_time,
        "end": FIRST_TIME + timedelta(hours=last),
        "flows": [None if np.isnan(value) else round(float(value), 3) for value in flows],
        "min": round(float(np.nanmin(flows)), 3),
        "mean": round(float(np.nanmean(flows)), 3),
        "max": round(float(flows[peak]), 3),
        "max_time": first_time + timedelta(hours=peak),
    }


def streamflows(
    reach_ids: list[str], start: datetime, end: datetime, *, deadline: float | None = None,
) -> list[dict]:
    """Return one hourly flow row for a group of reaches, reusing shared Zarr chunks.

    Arguments:
        reach_ids: Canonical decimal NWM feature IDs.
        start: First requested whole UTC hour.
        end: Last requested whole UTC hour.
        deadline: Optional monotonic deadline for the lookup.

    Returns:
        Rows containing the UTC time and a reach-keyed map of flows in m³/s.
    """
    start, end = max(start, FIRST_TIME), min(end, LAST_TIME)
    if end < start:
        return []

    first = math.ceil((start - FIRST_TIME) / timedelta(hours=1))
    last = math.floor((end - FIRST_TIME) / timedelta(hours=1))
    ids = __feature_ids()
    grouped_columns: dict[int, list[tuple[int, int]]] = {}
    for output_column, reach_id in enumerate(reach_ids):
        canonical_id = reach_id.lstrip("0") or "0"
        if len(canonical_id) > 19 or (len(canonical_id) == 19 and canonical_id > "9223372036854775807"):
            continue
        feature_id = int(canonical_id)
        column = int(np.searchsorted(ids, feature_id))
        if column < len(ids) and ids[column] == feature_id:
            grouped_columns.setdefault(column // FEATURE_CHUNK, []).append((output_column, column % FEATURE_CHUNK))

    values = np.full((len(reach_ids), last - first + 1), np.nan, dtype=np.float64)
    with httpx.Client(timeout=TIMEOUT_SECONDS, trust_env=False) as client:
        for feature_chunk, columns in grouped_columns.items():
            for time_chunk in range(first // TIME_CHUNK, last // TIME_CHUNK + 1):
                timeout = TIMEOUT_SECONDS
                if deadline is not None:
                    timeout = min(timeout, deadline - time.monotonic())
                    if timeout <= 0:
                        raise TimeoutError("Retrospective lookup deadline exceeded")
                chunk = __read(client, f"streamflow/{time_chunk}.{feature_chunk}", "<i4", timeout)
                chunk = chunk.reshape(TIME_CHUNK, FEATURE_CHUNK)
                offset = time_chunk * TIME_CHUNK
                first_row = max(first - offset, 0)
                last_row = min(last - offset + 1, TIME_CHUNK)
                time_slice = slice(first_row, last_row)
                output_slice = slice(offset + first_row - first, offset + last_row - first)
                for output_column, chunk_column in columns:
                    raw = chunk[time_slice, chunk_column]
                    values[output_column, output_slice] = np.where(
                        raw == MISSING_VALUE, np.nan, raw * SCALE_FACTOR,
                    )

    rows = []
    for index in range(last - first + 1):
        flows = {}
        for column, reach_id in enumerate(reach_ids):
            value = values[column, index]
            flows[reach_id] = round(float(value), 3) if np.isfinite(value) else None
        rows.append({
            "time": FIRST_TIME + timedelta(hours=first + index),
            "flows_cms": flows,
        })
    return rows
