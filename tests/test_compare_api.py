import io
import json
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

import numpy as np
import rasterio
from botocore.exceptions import ClientError
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError
from rasterio.transform import Affine

from fimbox_api import auth, main, rasters, tasks
from fimbox_api.models import CompareJobRequest
from fimbox_api.parity import compare_scenario_outputs
from fimbox_api.storage import RasterSource

REACH = {"HUC": "03020201", "ReachID": "8780625", "Streamflow": 227}
REQUEST = {
    "base_bucket": "fim-output", "base_prefix": "runs/legacy",
    "candidate_bucket": "fim-output", "candidate_prefix": "runs/streaming",
    "output_bucket": "fim-output", "output_prefix": "runs/compare",
}
TILE_KEYS = ["runs/streaming/s1/03020201/0_0.cog", "runs/streaming/s1/03020201/0_1.cog"]


def missing(operation: str) -> ClientError:
    return ClientError({"Error": {"Code": "404"}}, operation)


def scenarios(*ids: str) -> bytes:
    return json.dumps([{"ScenarioID": sid, "Reaches": [REACH]} for sid in ids]).encode()


def write_raster(path: Path, values, col=0) -> Path:
    data = np.asarray(values, dtype=np.float32)
    with rasterio.open(
        path, "w", driver="GTiff", width=data.shape[1], height=data.shape[0], count=1,
        dtype="float32", crs="EPSG:5070", nodata=float("nan"),
        transform=Affine(10, 0, 500000 + 10 * col, 0, -10, 2000000),
    ) as output:
        output.write(data, 1)
    return path


class CompareRequestTests(unittest.TestCase):
    def test_requires_output_bucket_and_distinct_safe_locations(self):
        CompareJobRequest(**REQUEST)
        with self.assertRaisesRegex(ValidationError, "requires output_bucket"):
            CompareJobRequest(**{**REQUEST, "output_bucket": None, "output_prefix": ""})
        with self.assertRaisesRegex(ValidationError, "requires output_prefix"):
            CompareJobRequest(**{**REQUEST, "output_prefix": "/"})
        with self.assertRaisesRegex(ValidationError, "different results"):
            CompareJobRequest(**{**REQUEST, "candidate_prefix": "/runs/legacy/"})
        with self.assertRaisesRegex(ValidationError, "must differ"):
            CompareJobRequest(**{**REQUEST, "output_prefix": "runs/streaming/"})
        with self.assertRaisesRegex(ValidationError, "'..'"):
            CompareJobRequest(**{**REQUEST, "base_prefix": "runs/../other"})
        CompareJobRequest(**{**REQUEST, "candidate_bucket": "other", "candidate_prefix": "runs/legacy"})


class MultiRasterBaseTests(unittest.TestCase):
    def test_base_tiles_are_combined_like_candidate_tiles(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            left = write_raster(root / "left.tif", [[1, np.nan]])
            right = write_raster(root / "right.tif", [[1, 1]], col=2)
            whole = write_raster(root / "whole.tif", [[1, np.nan, 1, 1]])
            report = compare_scenario_outputs([left, right], [whole], root / "diff.tif")
            self.assertEqual((report["legacy_flooded_cells"], report["different_cells"]), (3, 0))
            report = compare_scenario_outputs([whole], [left], root / "diff.tif")
            self.assertEqual((report["legacy_only_cells"], report["streaming_only_cells"]), (2, 0))


class CompareWorkerTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        (self.root / "input").mkdir()
        self.job_id = str(uuid4())
        self.objects = {
            ("fim-output", "runs/legacy/scenarios.json"): scenarios("s1", "s2"),
            ("fim-output", "runs/streaming/scenarios.json"): scenarios("s1", "s3"),
            ("fim-output", "runs/streaming/s1/manifest.json"): json.dumps({
                "tiles": [{"key": key} for key in TILE_KEYS], "missing_reaches": [],
            }).encode(),
        }
        self.files = {
            ("fim-output", "runs/legacy/s1.cog"): write_raster(self.root / "legacy.tif", [[1, 1, np.nan, 1]]),
            ("fim-output", TILE_KEYS[0]): write_raster(self.root / "t0.tif", [[1, 1]]),
            ("fim-output", TILE_KEYS[1]): write_raster(self.root / "t1.tif", [[np.nan, 1]], col=2),
        }
        self.uploaded, self.put = {}, {}
        self.logs = []
        for patcher in (
            patch.object(tasks.settings, "hand_fim_input_dir", self.root / "input"),
            patch.object(tasks, "append_job_log", side_effect=lambda _job, text: self.logs.append(text)),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = patch.object(tasks, "s3_client")
        self.client = patcher.start().return_value
        self.addCleanup(patcher.stop)
        self.client.get_object.side_effect = self.get_object
        self.client.head_object.side_effect = self.head_object
        self.client.download_file.side_effect = lambda bucket, key, path: shutil.copy(self.files[bucket, key], path)
        self.client.upload_file.side_effect = (
            lambda path, bucket, key, **kwargs: self.uploaded.__setitem__(key, Path(path).read_bytes())
        )
        self.client.put_object.side_effect = lambda Bucket, Key, Body, **kwargs: self.put.__setitem__(Key, Body)

    def get_object(self, Bucket, Key):
        if (Bucket, Key) not in self.objects:
            raise missing("GetObject")
        return {"Body": io.BytesIO(self.objects[Bucket, Key])}

    def head_object(self, Bucket, Key):
        if (Bucket, Key) not in self.files and Key not in self.put:
            raise missing("HeadObject")
        return {}

    def run_compare(self, **options):
        return tasks.hand_fim_compare.run(self.job_id, **{**REQUEST, **options})

    def test_legacy_and_streaming_results_are_compared_for_shared_scenarios(self):
        result = self.run_compare()
        self.assertEqual(result["outputs"], [{"ScenarioID": "s1"}])
        self.assertEqual(result["compared"], {
            "base_bucket": "fim-output", "base_prefix": "runs/legacy",
            "candidate_bucket": "fim-output", "candidate_prefix": "runs/streaming",
            "skipped_scenarios": ["s2", "s3"],
        })
        parity = result["parity"][0]
        self.assertTrue(parity["matches"])
        self.assertEqual((parity["legacy_flooded_cells"], parity["streaming_flooded_cells"]), (3, 3))
        self.assertEqual(parity["legacy_key"], "runs/legacy/s1.cog")
        self.assertEqual(parity["diff_key"], "runs/compare/parity/s1.diff.tif")
        self.assertIn("runs/compare/parity/s1.diff.tif", self.uploaded)
        report = json.loads(self.put["runs/compare/parity/report.json"])
        self.assertEqual(report["scenarios"], result["parity"])
        self.assertEqual(report["candidate_prefix"], "runs/streaming")
        self.assertEqual(
            [entry["key"] for entry in result["files"]],
            ["runs/compare/parity/report.json", "runs/compare/parity/s1.diff.tif"],
        )
        self.assertTrue(any("s2, s3" in line for line in self.logs))
        self.assertEqual(list((self.root / "input").iterdir()), [])

    def test_streaming_base_reports_differences_and_missing_reaches(self):
        self.objects["fim-output", "runs/streaming/s1/manifest.json"] = json.dumps({
            "tiles": [{"key": TILE_KEYS[0]}], "missing_reaches": ["03020201:8780625"],
        }).encode()
        parity = self.run_compare(
            base_prefix="runs/streaming", candidate_prefix="runs/legacy",
        )["parity"][0]
        self.assertEqual(parity["legacy_key"], "runs/streaming/s1/manifest.json")
        self.assertFalse(parity["matches"])
        self.assertEqual((parity["legacy_only_cells"], parity["streaming_only_cells"]), (0, 1))
        self.assertEqual(parity["missing_reaches"], ["03020201:8780625"])

    def test_no_shared_scenarios_or_missing_outputs_fail(self):
        self.objects["fim-output", "runs/streaming/scenarios.json"] = scenarios("s3")
        with self.assertRaisesRegex(RuntimeError, "no ScenarioIDs in common"):
            self.run_compare()
        self.objects["fim-output", "runs/streaming/scenarios.json"] = scenarios("s1")
        del self.files["fim-output", "runs/legacy/s1.cog"]
        with self.assertRaisesRegex(RuntimeError, "neither .*s1.cog nor .*s1/manifest.json"):
            self.run_compare()
        del self.objects["fim-output", "runs/legacy/scenarios.json"]
        with self.assertRaisesRegex(RuntimeError, "no s3://fim-output/runs/legacy/scenarios.json"):
            self.run_compare()

    def test_existing_report_requires_overwrite(self):
        self.put["runs/compare/parity/report.json"] = b"{}"
        with self.assertRaisesRegex(RuntimeError, "already exists"):
            self.run_compare()
        self.client.download_file.assert_not_called()
        self.assertTrue(self.run_compare(overwrite=True)["parity"][0]["matches"])

    def test_forbidden_output_location_is_explained(self):
        self.client.head_object.side_effect = ClientError({"Error": {"Code": "403"}}, "HeadObject")
        with self.assertRaisesRegex(RuntimeError, "denied access to output s3://fim-output/runs/compare/parity/report.json"):
            self.run_compare()
        self.client.download_file.assert_not_called()

    def test_tile_outside_scenario_prefix_is_rejected(self):
        self.objects["fim-output", "runs/streaming/s1/manifest.json"] = json.dumps({
            "tiles": [{"key": "runs/legacy/s1.cog"}],
        }).encode()
        with self.assertRaisesRegex(RuntimeError, "invalid tile key"):
            self.run_compare()


class CompareApiTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(main.app)
        session = auth.UserSession(user_id="alice", access_token="token", scope="read", expires_at=time.time() + 60)
        main.app.dependency_overrides[auth.get_authenticated_session] = lambda: session
        self.addCleanup(main.app.dependency_overrides.clear)
        for name, value in (
            ("hydroshare_client_id", "test-client"),
            ("hydroshare_client_secret", SecretStr("test-secret")),
        ):
            patcher = patch.object(main.settings, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        for name in ("track_user_job", "untrack_user_job", "create_job_credentials", "delete_job_credentials"):
            patcher = patch.object(main, name)
            setattr(self, name, patcher.start())
            self.addCleanup(patcher.stop)

    def submit(self, **options):
        with patch.object(tasks.hand_fim_compare, "apply_async") as enqueue:
            response = self.client.post("/jobs/compare", json={**REQUEST, **options})
        return response, enqueue

    def test_compare_job_is_queued_on_the_default_queue(self):
        response, enqueue = self.submit(overwrite=True)
        self.assertEqual(response.status_code, 202, response.text)
        job_id = response.json()["job_id"]
        self.assertEqual(enqueue.call_args.kwargs["args"], [
            job_id, "fim-output", "runs/legacy", "fim-output", "runs/streaming",
            "fim-output", "runs/compare", True,
        ])
        self.assertEqual(enqueue.call_args.kwargs["task_id"], job_id)
        self.assertNotIn("queue", enqueue.call_args.kwargs)
        self.create_job_credentials.assert_called_once()
        self.track_user_job.assert_called_once_with("alice", job_id)

    def test_compare_requires_authentication_and_valid_locations(self):
        with patch.object(main.settings, "hydroshare_client_id", ""):
            response, enqueue = self.submit()
        self.assertEqual(response.status_code, 409)
        enqueue.assert_not_called()
        response, enqueue = self.submit(output_prefix="runs/legacy")
        self.assertEqual(response.status_code, 422)
        enqueue.assert_not_called()

    def test_queue_failure_releases_credentials_and_tracking(self):
        with patch.object(tasks.hand_fim_compare, "apply_async", side_effect=OSError("down")):
            with self.assertRaises(OSError):
                self.client.post("/jobs/compare", json=REQUEST)
        self.delete_job_credentials.assert_called_once()
        self.untrack_user_job.assert_called_once()


class DiffColormapTests(unittest.TestCase):
    def test_diff_rasters_use_the_two_sided_colormap(self):
        with patch.object(rasters, "Reader") as reader, \
                patch.object(rasters.ImageData, "render", return_value=b"png") as render:
            reader.return_value.__enter__.return_value.tile.return_value = rasters.ImageData(
                np.ma.masked_all((1, 1, 1), dtype="uint8"),
            )
            rasters.raster_tile(RasterSource("s3://fim-output/runs/compare/parity/s1.DIFF.tif"), 1, 0, 0)
            self.assertIs(render.call_args.kwargs["colormap"], rasters.DIFF_COLORMAP)
            rasters.raster_tile(RasterSource("s3://fim-output/runs/legacy/s1.cog"), 1, 0, 0)
            self.assertIs(render.call_args.kwargs["colormap"], rasters.COLORMAP)


if __name__ == "__main__":
    unittest.main()
