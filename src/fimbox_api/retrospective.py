"""Hourly NWM v3.0 retrospective streamflow for one reach.

Reads the public NOAA Zarr store on AWS (CONUS chrtout, 1979-02-01 to 2023-02-01) with
plain HTTPS GETs: the feature_id index once per process, then one 672-hour chunk
(~6-12 MB compressed) per 28 days requested.
"""

import functools
import math
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
LAST_TIME = FIRST_TIME + timedelta(hours=TIME_STEPS - 1)


class RetrospectiveError(ValueError):
    """The request cannot be answered from the retrospective dataset."""


def __read(client: httpx.Client, key: str, dtype: str) -> np.ndarray:
    response = client.get(f"{ZARR_URL}/{key}")
    response.raise_for_status()
    return np.frombuffer(zstandard.ZstdDecompressor().decompress(response.content), dtype)


@functools.lru_cache(maxsize=1)
def __feature_ids() -> np.ndarray:
    with httpx.Client(timeout=TIMEOUT_SECONDS, trust_env=False) as client:
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
