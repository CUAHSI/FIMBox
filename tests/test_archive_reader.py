import base64
import json
import re
import tempfile
import time
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import h5py
import httpx
import numpy as np

from fimbox_api import archive_reader

HOUR = datetime(2024, 9, 27, 1, tzinfo=UTC)
FEATURES = [30, 10, 20, 40, 50, 60, 70, 80]
PACKED = [300, -999900, 0, 125, 5, 6, 7, 8]
EXPECTED = {"30": 3.0, "10": None, "20": 0.0, "40": 1.25, "80": 0.08, "999": None}
REACHES = list(EXPECTED)
OBJECT_URL = f"{archive_reader.OBJECT_ORIGIN}/{archive_reader.archive_key(HOUR)}"
INDEX_URL = f"{archive_reader.INDEX_ORIGIN}/{archive_reader.archive_key(HOUR)}.json"


class Archive:
    """A NetCDF4-layout object plus the Kerchunk index that describes its chunks."""

    def __init__(self, hour=HOUR, features=FEATURES, packed=PACKED, chunk=None, units="m3 s-1"):
        chunk = chunk or len(features)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "object.nc"
            with h5py.File(path, "w") as dataset:
                def create(name, values, size, dtype):
                    return dataset.create_dataset(
                        name, data=np.array(values, dtype=dtype), chunks=(size,),
                        shuffle=True, compression="gzip", compression_opts=2,
                    )
                create("time", [int(hour.timestamp() // 60)], 1, "i4").attrs["units"] = (
                    "minutes since 1970-01-01 00:00:00 UTC"
                )
                create("feature_id", features, len(features), "i8")
                flow = create("streamflow", packed, chunk, "i4")
                flow.attrs["units"] = units
                flow.attrs["scale_factor"] = 0.01
                flow.attrs["add_offset"] = 0.0
                flow.attrs["missing_value"] = np.int32(-999900)
                flow.attrs["_FillValue"] = np.int32(-999900)
            self.content = path.read_bytes()
            with h5py.File(path, "r") as dataset:
                self.chunks = {
                    name: {
                        info.chunk_offset[0] // size: (info.byte_offset, info.size)
                        for info in (
                            dataset[name].id.get_chunk_info(i)
                            for i in range(dataset[name].id.get_num_chunks())
                        )
                    }
                    for name, size in (("time", 1), ("feature_id", len(features)), ("streamflow", chunk))
                }
        self.hour, self.features, self.chunk = hour, features, chunk

    def index(self, url=OBJECT_URL, templated=False):
        def array(dtype, length, size):
            return json.dumps({
                "zarr_format": 2, "shape": [length], "chunks": [size], "dtype": dtype,
                "compressor": None, "fill_value": -999900 if dtype == "<i4" else None, "order": "C",
                "filters": [{"id": "shuffle", "elementsize": int(dtype[-1])}, {"id": "zlib", "level": 2}],
            })
        refs = {
            ".zgroup": '{"zarr_format":2}',
            "time/.zarray": array("<i4", 1, 1),
            "time/.zattrs": json.dumps({"units": "minutes since 1970-01-01 00:00:00 UTC"}),
            "feature_id/.zarray": array("<i8", len(self.features), len(self.features)),
            "feature_id/.zattrs": "{}",
            "streamflow/.zarray": array("<i4", len(self.features), self.chunk),
            "streamflow/.zattrs": json.dumps({
                "units": "m3 s-1", "scale_factor": 0.01, "add_offset": 0.0, "missing_value": -999900,
            }),
        }
        offset, length = self.chunks["time"][0]
        refs["time/0"] = "base64:" + base64.b64encode(self.content[offset:offset + length]).decode()
        target = "{{u}}" if templated else url
        for name in ("feature_id", "streamflow"):
            for number, (offset, length) in self.chunks[name].items():
                refs[f"{name}/{number}"] = [target, offset, length]
        return {"version": 1, "templates": {"u": url}, "refs": refs} if templated else {"version": 1, "refs": refs}


class Body(httpx.SyncByteStream):
    def __init__(self, content):
        self.content = content

    def __iter__(self):
        yield self.content


class Upstream:
    """Mock HTTP origins with Range, ETag and If-Match handling."""

    def __init__(self, archive=None, index=None, etags=None, full_body=False, key_url=OBJECT_URL):
        self.objects = {key_url: archive.content} if archive else {}
        self.indexes = {INDEX_URL: index} if index is not None else {}
        self.etags = etags
        self.full_body = full_body
        self.requests = []
        self.body_reads = 0

    def handle(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.requests.append((url, request.headers.get("range"), request.headers.get("if-match")))
        if url in self.indexes:
            return httpx.Response(200, stream=Body(json.dumps(self.indexes[url]).encode()))
        if url not in self.objects:
            return httpx.Response(404)
        content = self.objects[url]
        etag = '"v1"' if self.etags is None else self.etags.pop(0)
        if request.headers.get("if-match") not in (None, etag):
            return httpx.Response(412)
        if self.full_body:
            upstream = self

            class Counting(httpx.SyncByteStream):
                def __iter__(self):
                    upstream.body_reads += 1
                    yield content

            return httpx.Response(200, stream=Counting())
        start, end = (int(value) for value in re.fullmatch(r"bytes=(\d+)-(\d+)", request.headers["range"]).groups())
        end = min(end, len(content) - 1)
        return httpx.Response(
            206, stream=Body(content[start:end + 1]),
            headers={"Content-Range": f"bytes {start}-{end}/{len(content)}", "ETag": etag},
        )

    def read(self, hour=HOUR, reaches=REACHES, stats=None):
        client = httpx.Client(transport=httpx.MockTransport(self.handle))
        with patch.object(archive_reader, "http_client", return_value=client):
            return archive_reader.read_hour(
                hour, reaches, time.monotonic() + 30, stats or archive_reader.ReadStats(),
            )


class ArchiveReaderTests(unittest.TestCase):
    def test_indexed_multi_chunk_reads_each_needed_chunk_once_with_partial_responses(self):
        archive = Archive(chunk=3)
        upstream = Upstream(archive, archive.index())
        stats = archive_reader.ReadStats()
        self.assertEqual(upstream.read(stats=stats), EXPECTED)
        objects = [request for request in upstream.requests if request[0] == OBJECT_URL]
        # one feature_id chunk and the streamflow chunks holding positions 0, 2, 3 and 7
        self.assertEqual(len(objects), 1 + 3)
        self.assertEqual(len({request[1] for request in objects}), len(objects))
        self.assertEqual((stats.indexed_hours, stats.range_hours, stats.requests), (1, 0, 5))

    def test_indexed_single_chunk_matches_including_zero_and_templated_references(self):
        archive = Archive()
        upstream = Upstream(archive, archive.index(templated=True))
        self.assertEqual(upstream.read(), EXPECTED)
        self.assertEqual(len([r for r in upstream.requests if r[0] == OBJECT_URL]), 2)

    def test_adjacent_hours_resolve_reaches_from_their_own_layout(self):
        later = datetime(2024, 9, 27, 2, tzinfo=UTC)
        archive = Archive(hour=later, features=[80, 70, 30, 20], packed=[800, 700, 300, 0])
        url = f"{archive_reader.OBJECT_ORIGIN}/{archive_reader.archive_key(later)}"
        index_url = f"{archive_reader.INDEX_ORIGIN}/{archive_reader.archive_key(later)}.json"
        upstream = Upstream(archive, archive.index(url), key_url=url)
        upstream.indexes = {index_url: upstream.indexes[INDEX_URL]}
        self.assertEqual(
            upstream.read(hour=later, reaches=["30", "20", "80", "10"]),
            {"30": 3.0, "20": 0.0, "80": 8.0, "10": None},
        )

    def test_range_only_path_matches_the_indexed_path_for_the_same_object(self):
        archive = Archive(chunk=3)
        stats = archive_reader.ReadStats()
        upstream = Upstream(archive)
        self.assertEqual(upstream.read(stats=stats), EXPECTED)
        self.assertEqual((stats.indexed_hours, stats.range_hours), (0, 1))
        self.assertTrue(all(request[1] for request in upstream.requests if request[0] == OBJECT_URL))

    def test_hour_without_index_or_object_is_unavailable(self):
        stats = archive_reader.ReadStats()
        self.assertIsNone(Upstream().read(stats=stats))
        self.assertEqual(stats.missing_hours, 1)

    def test_missing_object_with_an_index_is_unavailable(self):
        archive = Archive()
        upstream = Upstream(index=archive.index())
        self.assertIsNone(upstream.read())

    def test_negative_and_nonfinite_flows_are_null(self):
        archive = Archive(features=[1, 2, 3], packed=[-5, 100, -999900])
        upstream = Upstream(archive, archive.index())
        self.assertEqual(upstream.read(reaches=["1", "2", "3"]), {"1": None, "2": 1.0, "3": None})

    def test_malformed_references_wrong_time_and_changed_identity_fail(self):
        archive = Archive()
        whole = archive.index()
        whole["refs"]["streamflow/0"] = [OBJECT_URL]
        other = archive.index(url="https://storage.googleapis.com/national-water-model/nwm.other.nc")
        invalid_units = archive.index()
        invalid_units["refs"]["streamflow/.zattrs"] = json.dumps({"units": "ft3 s-1"})
        for index in (whole, other, invalid_units, {"version": 2, "refs": {}}):
            with self.subTest(index=str(index)[:40]), self.assertRaises(archive_reader.ArchiveReadError):
                Upstream(archive, index).read()
        later = datetime(2024, 9, 27, 2, tzinfo=UTC)
        later_url = f"{archive_reader.OBJECT_ORIGIN}/{archive_reader.archive_key(later)}"
        later_index = f"{archive_reader.INDEX_ORIGIN}/{archive_reader.archive_key(later)}.json"
        mislabelled = Upstream(archive, archive.index(later_url), key_url=later_url)
        mislabelled.indexes = {later_index: mislabelled.indexes[INDEX_URL]}
        for upstream in (mislabelled, Upstream(archive, key_url=later_url)):
            with self.assertRaises(archive_reader.ArchiveReadError):
                upstream.read(hour=later)

    def test_full_body_response_to_a_range_is_aborted_without_reading_it(self):
        for index in (None, Archive().index()):
            archive = Archive()
            upstream = Upstream(archive, index, full_body=True)
            with self.assertRaises(archive_reader.ArchiveReadError):
                upstream.read()
            self.assertEqual(upstream.body_reads, 0)

    def test_object_version_change_between_reads_fails(self):
        archive = Archive(chunk=3)
        upstream = Upstream(archive, archive.index(), etags=['"v1"', '"v2"', '"v2"', '"v2"'])
        with self.assertRaises(archive_reader.ArchiveReadError):
            upstream.read()
        self.assertEqual(upstream.requests[2][2], '"v1"')

    def test_expired_deadline_raises_before_any_request(self):
        upstream = Upstream()
        client = httpx.Client(transport=httpx.MockTransport(upstream.handle))
        with patch.object(archive_reader, "http_client", return_value=client), \
             self.assertRaises(archive_reader.ArchiveDeadlineExceeded):
            archive_reader.read_hour(HOUR, REACHES, time.monotonic() - 1, archive_reader.ReadStats())
        self.assertEqual(upstream.requests, [])


if __name__ == "__main__":
    unittest.main()
