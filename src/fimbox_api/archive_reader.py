"""Selective reads of NOAA analysis_assim channel_rt objects.

An hour is read through its CIROH Kerchunk index when one is published, and
through HTTPS range requests on the NOAA object otherwise. Both paths validate
the same science metadata and return the same reach-to-discharge mapping.
"""

import base64
import io
import json
import logging
import math
import re
import threading
import time
import zlib
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import h5py
import httpx
import numpy as np

logger = logging.getLogger(__name__)

INDEX_ORIGIN = "https://ciroh-nwm-zarr-copy.s3.amazonaws.com/national-water-model"
OBJECT_ORIGIN = "https://storage.googleapis.com/national-water-model"
CONNECT_TIMEOUT_SECONDS = 5
READ_TIMEOUT_SECONDS = 20
MAX_INDEX_BYTES = 2 * 1024 * 1024
MAX_READ_BYTES = 8 * 1024 * 1024
MAX_HOUR_BYTES = 24 * 1024 * 1024
MAX_ARRAY_ELEMENTS = 10_000_000
MAX_CHUNKS = 4096
RANGE_BLOCK_BYTES = 256 * 1024
MAX_DECODED_HOURS = 2
_DECODE_SLOTS = threading.BoundedSemaphore(MAX_DECODED_HOURS)
_HDF5_LOCK = threading.Lock()
_CLIENT: httpx.Client | None = None
_CLIENT_LOCK = threading.Lock()
_CONTENT_RANGE = re.compile(r"bytes (\d+)-(\d+)/(\d+)")
_TEMPLATE = re.compile(r"\{\{\s*(\w+)\s*\}\}")
_TIME_UNITS = re.compile(
    r"(second|minute|hour|day)s?\s+since\s+(\d{4}-\d{2}-\d{2})(?:[ T](\d{2}:\d{2}:\d{2}))?"
    r"\s*(?:UTC|Z|\+00:00)?",
    re.IGNORECASE,
)
_DISCHARGE_UNITS = {"m3/s", "m3s-1"}


class ArchiveReadError(Exception):
    """An archive object or index could not be read or failed validation."""


class ArchiveDeadlineExceeded(Exception):
    """An archive read exceeded its lookup deadline."""


class ObjectMissing(Exception):
    """The NOAA object for an hour does not exist."""


@dataclass
class ReadStats:
    """Counters for one lookup."""

    indexed_hours: int = 0
    range_hours: int = 0
    missing_hours: int = 0
    requests: int = 0
    bytes: int = 0


def archive_key(hour: datetime) -> str:
    return (
        f"nwm.{hour:%Y%m%d}/analysis_assim/"
        f"nwm.t{hour:%H}z.analysis_assim.channel_rt.tm00.conus.nc"
    )


def check_deadline(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise ArchiveDeadlineExceeded
    return remaining


def http_client() -> httpx.Client:
    """Return the shared client; it never follows redirects or reads proxy settings."""
    global _CLIENT
    with _CLIENT_LOCK:
        if _CLIENT is None:
            _CLIENT = httpx.Client(
                timeout=httpx.Timeout(READ_TIMEOUT_SECONDS, connect=CONNECT_TIMEOUT_SECONDS),
                limits=httpx.Limits(max_connections=8, max_keepalive_connections=4),
                trust_env=False,
                follow_redirects=False,
            )
        return _CLIENT


def request_timeout(remaining: float) -> httpx.Timeout:
    return httpx.Timeout(
        connect=min(CONNECT_TIMEOUT_SECONDS, remaining),
        read=min(READ_TIMEOUT_SECONDS, remaining),
        write=min(CONNECT_TIMEOUT_SECONDS, remaining),
        pool=min(CONNECT_TIMEOUT_SECONDS, remaining),
    )


def reach_integer(reach_id: str) -> int | None:
    value = reach_id.lstrip("0") or "0"
    if len(value) > 19 or (len(value) == 19 and value > "9223372036854775807"):
        return None
    return int(value)


class RangeSource:
    """Validated range reads of one NOAA object, pinned to one object version."""

    def __init__(self, url: str, deadline: float, stats: ReadStats):
        self.url = url
        self.deadline = deadline
        self.stats = stats
        self.size: int | None = None
        self.etag: str | None = None
        self.hour_bytes = 0

    def read(self, offset: int, length: int, *, exact: bool = True) -> bytes:
        if offset < 0 or length <= 0 or length > MAX_READ_BYTES:
            raise ArchiveReadError("The archive requested an invalid byte range")
        if self.hour_bytes + length > MAX_HOUR_BYTES:
            raise ArchiveReadError("The archive read exceeded its byte limit")
        wanted_end = offset + length - 1
        headers = {"Range": f"bytes={offset}-{wanted_end}", "Accept-Encoding": "identity"}
        if self.etag and not self.etag.startswith("W/"):
            headers["If-Match"] = self.etag
        remaining = check_deadline(self.deadline)
        first = self.size is None
        body = bytearray()
        try:
            with http_client().stream(
                "GET", self.url, headers=headers, timeout=request_timeout(remaining),
            ) as response:
                if response.status_code == 404 and first:
                    raise ObjectMissing
                if response.status_code == 412:
                    raise ArchiveReadError("The archive object changed during the lookup")
                if response.status_code != 206:
                    raise ArchiveReadError(f"The NWM archive returned HTTP {response.status_code}")
                match = _CONTENT_RANGE.fullmatch(response.headers.get("content-range", ""))
                if match is None:
                    raise ArchiveReadError("The archive range response is invalid")
                start, end, total = (int(value) for value in match.groups())
                if self.size is not None and total != self.size:
                    raise ArchiveReadError("The archive object changed during the lookup")
                last = min(wanted_end, total - 1)
                if start != offset or offset >= total or end != last or (exact and end != wanted_end):
                    raise ArchiveReadError("The archive range response does not match its request")
                expected = end - start + 1
                declared = response.headers.get("content-length")
                if declared is not None and (not declared.isdigit() or int(declared) != expected):
                    raise ArchiveReadError("The archive range response length is invalid")
                etag = response.headers.get("etag")
                if not first and etag != self.etag:
                    raise ArchiveReadError("The archive object changed during the lookup")
                for block in response.iter_raw():
                    check_deadline(self.deadline)
                    body.extend(block)
                    if len(body) > expected:
                        raise ArchiveReadError("The archive range response is longer than requested")
                if len(body) != expected:
                    raise ArchiveReadError("The archive range response is incomplete")
        except httpx.TimeoutException as error:
            if time.monotonic() >= self.deadline:
                raise ArchiveDeadlineExceeded from error
            raise ArchiveReadError("The NWM archive request timed out") from error
        except httpx.HTTPError as error:
            raise ArchiveReadError("The NWM archive could not be read") from error
        if first:
            self.size, self.etag = total, etag
        self.hour_bytes += len(body)
        self.stats.requests += 1
        self.stats.bytes += len(body)
        return bytes(body)


class RangeFile(io.RawIOBase):
    """Seekable read-only view of a NOAA object backed by cached range blocks."""

    def __init__(self, source: RangeSource):
        super().__init__()
        self.source = source
        self.position = 0
        self.blocks: dict[int, bytes] = {0: source.read(0, RANGE_BLOCK_BYTES, exact=False)}
        self.size: int = source.size
        self.error: BaseException | None = None

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def tell(self) -> int:
        return self.position

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        base = {io.SEEK_SET: 0, io.SEEK_CUR: self.position, io.SEEK_END: self.size}[whence]
        self.position = max(0, base + offset)
        return self.position

    def fill(self, first: int, last: int) -> None:
        missing = [index for index in range(first, last + 1) if index not in self.blocks]
        run_start = None
        for index in [*missing, None]:
            if run_start is not None and (index is None or index != previous + 1):
                self.fetch_run(run_start, previous)
                run_start = None
            if index is not None and run_start is None:
                run_start = index
            previous = index

    def fetch_run(self, first: int, last: int) -> None:
        per_request = MAX_READ_BYTES // RANGE_BLOCK_BYTES
        for chunk_first in range(first, last + 1, per_request):
            chunk_last = min(last, chunk_first + per_request - 1)
            start = chunk_first * RANGE_BLOCK_BYTES
            end = min((chunk_last + 1) * RANGE_BLOCK_BYTES, self.size)
            data = self.source.read(start, end - start)
            for index in range(chunk_first, chunk_last + 1):
                offset = (index - chunk_first) * RANGE_BLOCK_BYTES
                self.blocks[index] = data[offset:offset + RANGE_BLOCK_BYTES]

    def readinto(self, buffer) -> int:
        count = min(len(buffer), self.size - self.position)
        if count <= 0:
            return 0
        try:
            first = self.position // RANGE_BLOCK_BYTES
            last = (self.position + count - 1) // RANGE_BLOCK_BYTES
            self.fill(first, last)
            joined = b"".join(self.blocks[index] for index in range(first, last + 1))
        except BaseException as error:
            self.error = error
            raise
        offset = self.position - first * RANGE_BLOCK_BYTES
        buffer[:count] = joined[offset:offset + count]
        self.position += count
        return count


def __index_references(index: dict) -> dict:
    refs = index.get("refs")
    templates = index.get("templates") or {}
    if index.get("version") != 1 or not isinstance(refs, dict) or not isinstance(templates, dict):
        raise ArchiveReadError("The archive index is not a version 1 reference set")

    def expand(text: str) -> str:
        def replace(match):
            if match.group(1) not in templates:
                raise ArchiveReadError("The archive index uses an undefined template")
            return str(templates[match.group(1)])
        return _TEMPLATE.sub(replace, text)

    resolved = {}
    for key, value in refs.items():
        if isinstance(value, list):
            if len(value) != 3:
                raise ArchiveReadError("The archive index references a whole object")
            url, offset, length = value
            if (
                not isinstance(url, str) or isinstance(offset, bool) or isinstance(length, bool)
                or not isinstance(offset, int) or not isinstance(length, int)
                or offset < 0 or length <= 0
            ):
                raise ArchiveReadError("The archive index has an invalid reference")
            resolved[key] = (expand(url), offset, length)
        elif isinstance(value, (str, dict)):
            resolved[key] = value
        else:
            raise ArchiveReadError("The archive index has an invalid reference")
    return resolved


def __index_metadata(references: dict, key: str) -> dict:
    value = references.get(key)
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError as error:
            raise ArchiveReadError("The archive index metadata is invalid") from error
    if not isinstance(value, dict):
        raise ArchiveReadError("The archive index is missing array metadata")
    return value


def __array_layout(references: dict, name: str) -> dict:
    zarray = __index_metadata(references, f"{name}/.zarray")
    attributes = __index_metadata(references, f"{name}/.zattrs")
    try:
        dtype = np.dtype(zarray["dtype"])
        shape, chunks = zarray["shape"], zarray["chunks"]
        stages = list(zarray.get("filters") or [])
        if zarray.get("compressor"):
            stages.append(zarray["compressor"])
        if (
            zarray.get("zarr_format") != 2 or dtype.kind not in "iuf" or zarray.get("order") != "C"
            or len(shape) != 1 or len(chunks) != 1 or not isinstance(shape[0], int)
            or not isinstance(chunks[0], int) or shape[0] < 1 or chunks[0] < 1
            or shape[0] > MAX_ARRAY_ELEMENTS or chunks[0] > MAX_ARRAY_ELEMENTS
        ):
            raise ArchiveReadError("The archive index describes an unsupported array")
        for stage in stages:
            if stage["id"] == "shuffle":
                if stage["elementsize"] != dtype.itemsize:
                    raise ArchiveReadError("The archive index describes an unsupported array")
            elif stage["id"] != "zlib":
                raise ArchiveReadError("The archive index uses an unsupported codec")
    except (KeyError, TypeError, ValueError) as error:
        raise ArchiveReadError("The archive index describes an unsupported array") from error
    chunk_count = -(-shape[0] // chunks[0])
    if chunk_count > MAX_CHUNKS:
        raise ArchiveReadError("The archive index describes too many chunks")
    return {
        "name": name, "dtype": dtype, "length": shape[0], "chunk": chunks[0],
        "chunk_count": chunk_count, "stages": stages, "attributes": attributes,
        "fill_value": zarray.get("fill_value"),
    }


def __decode_chunk(data: bytes, layout: dict) -> np.ndarray:
    expected = layout["chunk"] * layout["dtype"].itemsize
    for stage in reversed(layout["stages"]):
        if stage["id"] == "zlib":
            decoder = zlib.decompressobj()
            try:
                data = decoder.decompress(data, expected + 1)
            except zlib.error as error:
                raise ArchiveReadError("The archive chunk could not be decompressed") from error
            if len(data) > expected or not decoder.eof or decoder.unused_data:
                raise ArchiveReadError("The archive chunk has an invalid size")
        else:
            size = layout["dtype"].itemsize
            count = len(data) // size
            head = np.frombuffer(data, dtype=np.uint8, count=count * size)
            data = head.reshape(size, count).T.tobytes() + data[count * size:]
    if len(data) != expected:
        raise ArchiveReadError("The archive chunk has an invalid size")
    return np.frombuffer(data, dtype=layout["dtype"])


def __read_chunk(
    references: dict, layout: dict, number: int, source: RangeSource, object_url: str,
) -> np.ndarray:
    reference = references.get(f"{layout['name']}/{number}")
    if isinstance(reference, str):
        if reference.startswith("base64:"):
            try:
                data = base64.b64decode(reference[7:], validate=True)
            except ValueError as error:
                raise ArchiveReadError("The archive index has invalid inline data") from error
        else:
            data = reference.encode()
    elif isinstance(reference, tuple):
        url, offset, length = reference
        if url != object_url:
            raise ArchiveReadError("The archive index resolves to a different object")
        data = source.read(offset, length)
    else:
        raise ArchiveReadError("The archive index is missing a chunk")
    return __decode_chunk(data, layout)


def __read_array(references, layout, source, object_url) -> np.ndarray:
    parts = [
        __read_chunk(references, layout, number, source, object_url)
        for number in range(layout["chunk_count"])
    ]
    return np.concatenate(parts)[:layout["length"]]


def __fetch_index(key: str, deadline: float, stats: ReadStats) -> dict | None:
    remaining = check_deadline(deadline)
    body = bytearray()
    try:
        with http_client().stream(
            "GET", f"{INDEX_ORIGIN}/{key}.json",
            headers={"Accept-Encoding": "identity"}, timeout=request_timeout(remaining),
        ) as response:
            if response.status_code == 404:
                return None
            if response.status_code != 200:
                raise ArchiveReadError(f"The NWM archive index returned HTTP {response.status_code}")
            for block in response.iter_raw():
                check_deadline(deadline)
                body.extend(block)
                if len(body) > MAX_INDEX_BYTES:
                    raise ArchiveReadError("The archive index is too large")
    except httpx.TimeoutException as error:
        if time.monotonic() >= deadline:
            raise ArchiveDeadlineExceeded from error
        raise ArchiveReadError("The NWM archive index request timed out") from error
    except httpx.HTTPError as error:
        raise ArchiveReadError("The NWM archive index could not be read") from error
    stats.requests += 1
    stats.bytes += len(body)
    try:
        index = json.loads(body)
    except ValueError as error:
        raise ArchiveReadError("The archive index is not valid JSON") from error
    if not isinstance(index, dict):
        raise ArchiveReadError("The archive index is not a reference set")
    return index


def __text(value) -> str:
    if isinstance(value, bytes):
        return value.decode()
    return str(np.asarray(value).reshape(-1)[0]) if isinstance(value, np.ndarray) else str(value)


def __number(value, default: float) -> float:
    if value is None:
        return default
    array = np.asarray(value).reshape(-1)
    if len(array) != 1:
        raise ArchiveReadError("The archive streamflow packing metadata is invalid")
    return float(array[0])


def __valid_time(values: np.ndarray, units: str) -> datetime:
    match = _TIME_UNITS.fullmatch(units.strip())
    if match is None or np.asarray(values).size != 1:
        raise ArchiveReadError("The archive time metadata is invalid")
    value = float(np.asarray(values).reshape(-1)[0])
    if not math.isfinite(value) or abs(value) > 1e12:
        raise ArchiveReadError("The archive time metadata is invalid")
    unit, date, clock = match.groups()
    origin = datetime.fromisoformat(f"{date}T{clock or '00:00:00'}").replace(tzinfo=UTC)
    return origin + timedelta(**{f"{unit.lower()}s": value})


def __extract(
    hour: datetime,
    reach_ids: list[str],
    *,
    time_values: np.ndarray,
    time_units: str,
    flow_units: str,
    flow_length: int,
    scale: float,
    offset: float,
    missing_values: list,
    read_features: Callable[[], np.ndarray],
    read_flow: Callable[[np.ndarray], np.ndarray],
) -> dict[str, float | None]:
    if __valid_time(time_values, time_units) != hour:
        raise ArchiveReadError("The archive object does not contain its requested UTC hour")
    if re.sub(r"[\s^*]", "", flow_units.lower().replace("³", "3")) not in _DISCHARGE_UNITS:
        raise ArchiveReadError("The archive streamflow units are not m³/s")
    if not (math.isfinite(scale) and math.isfinite(offset)):
        raise ArchiveReadError("The archive streamflow packing metadata is invalid")

    result: dict[str, float | None] = {reach_id: None for reach_id in reach_ids}
    requested = {
        feature_id for reach_id in reach_ids
        if (feature_id := reach_integer(reach_id)) is not None
    }
    if not requested:
        return result
    feature_ids = np.asarray(read_features())
    if feature_ids.ndim != 1 or not np.issubdtype(feature_ids.dtype, np.integer):
        raise ArchiveReadError("The archive feature IDs are not integers")
    if len(feature_ids) != flow_length:
        raise ArchiveReadError("The archive has no one-to-one discharge position for each feature ID")
    positions = np.flatnonzero(np.isin(feature_ids, np.fromiter(requested, dtype=np.int64)))
    if not len(positions):
        return result
    selected_ids = feature_ids[positions]
    del feature_ids
    raw = np.asarray(read_flow(positions)).reshape(-1)
    if len(raw) != len(positions):
        raise ArchiveReadError("The archive flow data has an invalid shape")
    masked = np.zeros(len(raw), dtype=bool)
    for missing in missing_values:
        masked |= raw == missing
    values = raw.astype(np.float64) * scale + offset
    for feature_id, value, is_masked in zip(selected_ids, values, masked):
        if not is_masked and math.isfinite(value) and value >= 0:
            result[str(int(feature_id))] = float(value)
    return result


def __missing_values(*candidates) -> list:
    found = []
    for candidate in candidates:
        if candidate is not None:
            found.extend(np.asarray(candidate).reshape(-1).tolist())
    return found


def __read_indexed(
    index: dict, key: str, hour: datetime, reach_ids: list[str], source: RangeSource,
) -> dict[str, float | None]:
    object_url = f"{OBJECT_ORIGIN}/{key}"
    references = __index_references(index)
    time_layout = __array_layout(references, "time")
    feature_layout = __array_layout(references, "feature_id")
    flow_layout = __array_layout(references, "streamflow")
    attributes = flow_layout["attributes"]
    chunk = flow_layout["chunk"]

    def read_features():
        return __read_array(references, feature_layout, source, object_url)

    def read_flow(positions):
        raw = np.empty(len(positions), dtype=flow_layout["dtype"])
        numbers = positions // chunk
        for number in np.unique(numbers):
            selection = numbers == number
            data = __read_chunk(references, flow_layout, int(number), source, object_url)
            raw[selection] = data[positions[selection] - int(number) * chunk]
            del data
        return raw

    try:
        return __extract(
            hour, reach_ids,
            time_values=__read_array(references, time_layout, source, object_url),
            time_units=__text(__index_metadata(references, "time/.zattrs").get("units", "")),
            flow_units=__text(attributes.get("units", "")),
            flow_length=flow_layout["length"],
            scale=__number(attributes.get("scale_factor"), 1.0),
            offset=__number(attributes.get("add_offset"), 0.0),
            missing_values=__missing_values(flow_layout["fill_value"], attributes.get("missing_value")),
            read_features=read_features,
            read_flow=read_flow,
        )
    except (ValueError, TypeError, IndexError, KeyError, OverflowError) as error:
        raise ArchiveReadError("The archive index data could not be decoded") from error


def __read_ranges(
    hour: datetime, reach_ids: list[str], source: RangeSource,
) -> dict[str, float | None]:
    archive = RangeFile(source)
    try:
        with _HDF5_LOCK, h5py.File(archive, "r", rdcc_nbytes=16 * 1024 * 1024) as dataset:
            flow = dataset["streamflow"]
            if flow.ndim != 1:
                raise ArchiveReadError("The archive object has an invalid variable layout")
            attributes = flow.attrs
            return __extract(
                hour, reach_ids,
                time_values=dataset["time"][()],
                time_units=__text(dataset["time"].attrs.get("units", "")),
                flow_units=__text(attributes.get("units", "")),
                flow_length=flow.shape[0],
                scale=__number(attributes.get("scale_factor"), 1.0),
                offset=__number(attributes.get("add_offset"), 0.0),
                missing_values=__missing_values(
                    attributes.get("_FillValue"), attributes.get("missing_value"),
                ),
                read_features=lambda: dataset["feature_id"][()],
                read_flow=lambda positions: flow[positions],
            )
    except (ArchiveReadError, ArchiveDeadlineExceeded, ObjectMissing):
        raise
    except (OSError, KeyError, ValueError, TypeError, RuntimeError, IndexError, OverflowError) as error:
        if isinstance(archive.error, (ArchiveReadError, ArchiveDeadlineExceeded, ObjectMissing)):
            raise archive.error from error
        raise ArchiveReadError("The archive object could not be decoded") from error
    finally:
        archive.blocks.clear()


def read_hour(
    hour: datetime, reach_ids: list[str], deadline: float, stats: ReadStats,
) -> dict[str, float | None] | None:
    """Return flows for one archive hour, or None when NOAA does not have the object."""
    key = archive_key(hour)
    if not _DECODE_SLOTS.acquire(timeout=check_deadline(deadline)):
        raise ArchiveDeadlineExceeded
    try:
        index = __fetch_index(key, deadline, stats)
        source = RangeSource(f"{OBJECT_ORIGIN}/{key}", deadline, stats)
        try:
            if index is None:
                stats.range_hours += 1
                flows = __read_ranges(hour, reach_ids, source)
            else:
                stats.indexed_hours += 1
                flows = __read_indexed(index, key, hour, reach_ids, source)
        except ObjectMissing:
            stats.missing_hours += 1
            return None
        check_deadline(deadline)
        return flows
    finally:
        _DECODE_SLOTS.release()
