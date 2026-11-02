import unittest
from datetime import timedelta
from unittest.mock import patch

import httpx
import numpy as np
import zstandard

from fimbox_api import retrospective

FEATURE_IDS = np.array([3, 7, 9, 12], dtype="<i8")


def chunk(t):
    values = np.zeros((4, 3), dtype="<i4")
    values[:, 1] = [(t * 4 + row) * 100 for row in range(4)]
    if t == 1:
        values[1, 1] = retrospective.MISSING_VALUE
    return values


class RetrospectiveTests(unittest.TestCase):
    def setUp(self):
        self.requests = []
        compressor = zstandard.ZstdCompressor()

        def handler(request):
            key = request.url.path.split("chrtout.zarr/", 1)[1]
            self.requests.append(key)
            if key == "feature_id/0":
                return httpx.Response(200, content=compressor.compress(FEATURE_IDS.tobytes()))
            t, f = map(int, key.removeprefix("streamflow/").split("."))
            self.assertEqual(f, 0)
            return httpx.Response(200, content=compressor.compress(chunk(t).tobytes()))

        real_client = httpx.Client
        transport = httpx.MockTransport(handler)
        for name, value in (("TIME_CHUNK", 4), ("FEATURE_CHUNK", 3)):
            patcher = patch.object(retrospective, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = patch.object(retrospective.httpx, "Client", side_effect=lambda **kwargs: real_client(transport=transport))
        patcher.start()
        self.addCleanup(patcher.stop)
        feature_ids = getattr(retrospective, "__feature_ids")
        feature_ids.cache_clear()
        self.addCleanup(feature_ids.cache_clear)

    def hours(self, hours):
        return retrospective.FIRST_TIME + timedelta(hours=hours)

    def test_reads_scaled_hourly_values_across_chunks(self):
        result = retrospective.streamflow(7, self.hours(2), self.hours(6))
        self.assertEqual(self.requests, ["feature_id/0", "streamflow/0.0", "streamflow/1.0"])
        self.assertEqual(result["flows"], [2.0, 3.0, 4.0, None, 6.0])
        self.assertEqual((result["start"], result["end"]), (self.hours(2), self.hours(6)))
        self.assertEqual((result["min"], result["mean"], result["max"]), (2.0, 3.75, 6.0))
        self.assertEqual(result["max_time"], self.hours(6))

    def test_clips_to_dataset_bounds(self):
        result = retrospective.streamflow(7, self.hours(-5), self.hours(1))
        self.assertEqual(result["start"], retrospective.FIRST_TIME)
        self.assertEqual(result["flows"], [0.0, 1.0])

    def test_rejects_unanswerable_requests(self):
        for reach_id, start, end in (
            (8, self.hours(0), self.hours(1)),
            (7, self.hours(-10), self.hours(-5)),
            (7, self.hours(0), self.hours(24 * retrospective.MAX_DAYS + 1)),
            (7, self.hours(5), self.hours(5)),
        ):
            with self.subTest(reach_id=reach_id, start=start), self.assertRaises(retrospective.RetrospectiveError):
                retrospective.streamflow(reach_id, start, end)


if __name__ == "__main__":
    unittest.main()
