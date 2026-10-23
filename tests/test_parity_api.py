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

from fimbox_api import auth, main, tasks
from fimbox_api.models import ParityJobRequest, ScenarioManifest

PAYLOAD = [{"ScenarioID": "s1", "Reaches": [{"HUC": "03020201", "ReachID": "8780625", "Streamflow": 227}]}]
REQUEST = {
    "legacy_bucket": "fim-output", "legacy_prefix": "runs/legacy",
    "output_bucket": "fim-output", "output_prefix": "runs/streaming",
}
TILE_KEY = "runs/streaming/s1/03020201/0_0.cog"


def missing(operation: str) -> ClientError:
    return ClientError({"Error": {"Code": "404"}}, operation)


class ParityRequestTests(unittest.TestCase):
    def test_requires_bucket_and_distinct_safe_prefix(self):
        ParityJobRequest(**REQUEST)
        with self.assertRaisesRegex(ValidationError, "requires output_bucket"):
            ParityJobRequest(**{**REQUEST, "output_bucket": None, "output_prefix": ""})
        with self.assertRaisesRegex(ValidationError, "must differ"):
            ParityJobRequest(**{**REQUEST, "output_prefix": "/runs/legacy/"})
        with self.assertRaisesRegex(ValidationError, "'..'"):
            ParityJobRequest(**{**REQUEST, "legacy_prefix": "runs/../other"})
        ParityJobRequest(**{**REQUEST, "output_bucket": "other", "output_prefix": "runs/legacy"})


class ParityWorkerTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        (self.root / "input").mkdir()
        self.job_id = str(uuid4())
        self.objects = {
            ("fim-output", "runs/legacy/scenarios.json"): json.dumps(PAYLOAD).encode(),
            ("fim-output", "runs/streaming/s1/manifest.json"): json.dumps({
                "tiles": [{"key": TILE_KEY}], "missing_reaches": [],
            }).encode(),
        }
        self.files = {
            ("fim-output", "runs/legacy/s1.cog"): self.raster("legacy.tif", [[1, 1, np.nan]]),
            ("fim-output", TILE_KEY): self.raster("tile.tif", [[1, 1, np.nan]]),
        }
        self.uploaded, self.put = {}, {}
        for patcher in (
            patch.object(tasks.settings, "hand_fim_input_dir", self.root / "input"),
            patch.object(tasks, "append_job_log"),
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
        patcher = patch.object(tasks, "hand_fim", return_value={
            "outputs": [{"ScenarioID": "s1"}], "output_bucket": "fim-output", "output_prefix": "runs/streaming",
            "execution_mode": "streaming", "scenario_manifests": [ScenarioManifest(
                ScenarioID="s1", key="runs/streaming/s1/manifest.json", tile_count=1,
                bounds=(-78, 35, -77, 36), resolution=10.0, tile_size=2048,
            ).model_dump(mode="json")],
        })
        self.hand_fim = patcher.start()
        self.addCleanup(patcher.stop)

    def raster(self, name, values, col=0):
        path = self.root / name
        data = np.asarray(values, dtype=np.float32)
        with rasterio.open(
            path, "w", driver="GTiff", width=data.shape[1], height=data.shape[0], count=1,
            dtype="float32", crs="EPSG:5070", nodata=float("nan"),
            transform=Affine(10, 0, 500000 + 10 * col, 0, -10, 2000000),
        ) as output:
            output.write(data, 1)
        return path

    def get_object(self, Bucket, Key):
        if (Bucket, Key) not in self.objects:
            raise missing("GetObject")
        return {"Body": io.BytesIO(self.objects[Bucket, Key])}

    def head_object(self, Bucket, Key):
        if (Bucket, Key) not in self.files and Key not in self.put:
            raise missing("HeadObject")
        return {}

    def run_parity(self, overwrite=False):
        return tasks.hand_fim_parity.run(self.job_id, **REQUEST, overwrite=overwrite)

    def test_matching_outputs_rerun_legacy_scenarios_with_streaming_and_publish_report(self):
        result = self.run_parity()
        self.hand_fim.assert_called_once_with(
            self.job_id, PAYLOAD, "fim-output", "runs/streaming", None, False, "streaming",
        )
        parity = result["parity"][0]
        self.assertTrue(parity["matches"])
        self.assertEqual(parity["legacy_flooded_cells"], 2)
        self.assertEqual(parity["different_cells"], 0)
        self.assertEqual((parity["grid_shift_rows"], parity["unaligned_different_cells"]), (0, 0))
        self.assertEqual(parity["legacy_key"], "runs/legacy/s1.cog")
        self.assertEqual(parity["diff_key"], "runs/streaming/parity/s1.diff.tif")
        report = json.loads(self.put["runs/streaming/parity/report.json"])
        self.assertEqual(report["scenarios"], result["parity"])
        self.assertIn("runs/streaming/parity/s1.diff.tif", self.uploaded)
        self.assertEqual(
            [entry["key"] for entry in result["files"]],
            ["runs/streaming/parity/report.json", "runs/streaming/parity/s1.diff.tif"],
        )
        self.assertEqual(list((self.root / "input").iterdir()), [])

    def test_differences_and_missing_reaches_are_reported_as_mismatches(self):
        self.files["fim-output", TILE_KEY] = self.raster("changed.tif", [[1, np.nan, 1]])
        self.objects["fim-output", "runs/streaming/s1/manifest.json"] = json.dumps({
            "tiles": [{"key": TILE_KEY}], "missing_reaches": ["03020201:8780625"],
        }).encode()
        parity = self.run_parity()["parity"][0]
        self.assertFalse(parity["matches"])
        self.assertEqual((parity["legacy_only_cells"], parity["streaming_only_cells"]), (1, 1))
        self.assertEqual(parity["missing_reaches"], ["03020201:8780625"])
        self.assertEqual([d["value"] for d in parity["sample_differences"]], [1, 2])

    def test_missing_legacy_inputs_fail_before_streaming(self):
        del self.files["fim-output", "runs/legacy/s1.cog"]
        with self.assertRaisesRegex(RuntimeError, "s1.cog does not exist"):
            self.run_parity()
        del self.objects["fim-output", "runs/legacy/scenarios.json"]
        with self.assertRaisesRegex(RuntimeError, "no s3://fim-output/runs/legacy/scenarios.json"):
            self.run_parity()
        self.hand_fim.assert_not_called()

    def test_existing_report_requires_overwrite(self):
        self.put["runs/streaming/parity/report.json"] = b"{}"
        with self.assertRaisesRegex(RuntimeError, "already exists"):
            self.run_parity()
        self.hand_fim.assert_not_called()
        self.assertTrue(self.run_parity(overwrite=True)["parity"][0]["matches"])

    def test_tile_outside_scenario_prefix_is_rejected(self):
        self.objects["fim-output", "runs/streaming/s1/manifest.json"] = json.dumps({
            "tiles": [{"key": "runs/legacy/s1.cog"}],
        }).encode()
        with self.assertRaisesRegex(RuntimeError, "invalid tile key"):
            self.run_parity()
        self.client.download_file.assert_not_called()


class ParityApiTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(main.app)
        session = auth.UserSession(user_id="alice", access_token="token", scope="read", expires_at=time.time() + 60)
        main.app.dependency_overrides[auth.get_authenticated_session] = lambda: session
        self.addCleanup(main.app.dependency_overrides.clear)
        for name, value in (
            ("hydroshare_client_id", "test-client"),
            ("hydroshare_client_secret", SecretStr("test-secret")),
            ("stream_input_bucket", "hydrofabric"),
        ):
            patcher = patch.object(main.settings, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        for name in ("track_user_job", "untrack_user_job", "create_job_credentials", "delete_job_credentials"):
            patcher = patch.object(main, name)
            setattr(self, name, patcher.start())
            self.addCleanup(patcher.stop)

    def submit(self, **options):
        with patch.object(tasks.hand_fim_parity, "apply_async") as enqueue:
            response = self.client.post("/jobs/parity", json={**REQUEST, **options})
        return response, enqueue

    def test_parity_job_is_queued_with_legacy_and_output_locations(self):
        response, enqueue = self.submit(overwrite=True)
        self.assertEqual(response.status_code, 202)
        job_id = response.json()["job_id"]
        self.assertEqual(enqueue.call_args.kwargs["args"], [
            job_id, "fim-output", "runs/legacy", "fim-output", "runs/streaming", True,
        ])
        self.assertEqual(enqueue.call_args.kwargs["task_id"], job_id)
        self.assertNotIn("queue", enqueue.call_args.kwargs)
        self.create_job_credentials.assert_called_once()
        self.track_user_job.assert_called_once_with("alice", job_id)
        with patch.object(main.settings, "stream_queue", "streaming"):
            _response, enqueue = self.submit()
        self.assertEqual(enqueue.call_args.kwargs["queue"], "streaming")

    def test_parity_requires_streaming_authentication_and_valid_locations(self):
        with patch.object(main.settings, "stream_input_bucket", ""):
            response, enqueue = self.submit()
        self.assertEqual(response.status_code, 409)
        enqueue.assert_not_called()
        with patch.object(main.settings, "hydroshare_client_id", ""):
            response, enqueue = self.submit()
        self.assertEqual(response.status_code, 409)
        response, enqueue = self.submit(output_prefix="runs/legacy")
        self.assertEqual(response.status_code, 422)
        enqueue.assert_not_called()

    def test_queue_failure_releases_credentials_and_tracking(self):
        with patch.object(tasks.hand_fim_parity, "apply_async", side_effect=OSError("down")):
            with self.assertRaises(OSError):
                self.client.post("/jobs/parity", json=REQUEST)
        self.delete_job_credentials.assert_called_once()
        self.untrack_user_job.assert_called_once()
