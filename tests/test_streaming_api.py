import importlib.util
import io
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from uuid import uuid4

import numpy as np
import rasterio
from botocore.exceptions import ClientError
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError
from rasterio.crs import CRS
from rasterio.transform import Affine

from fimbox_api import auth, main, rasters, storage, tasks
from fimbox_api.models import HandFimJobOptions, ScenarioManifest

HANDFIM = Path(__file__).resolve().parents[1] / "docker" / "handfim"
PAYLOAD = [{"ScenarioID": "s1", "Reaches": [{"HUC": "03020201", "ReachID": "8780625", "Streamflow": 227}]}]
DESTINATION = {"output_bucket": "fim-output", "output_prefix": "data/output"}
SECRETS = {"AWS_ACCESS_KEY_ID": "test-access", "AWS_SECRET_ACCESS_KEY": "test-secret"}


def load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


stream = load("streaming_api_test.streamFIM", HANDFIM / "patches" / "streamFIM.py")
stream_storage = load("streaming_api_test.stream_storage", HANDFIM / "stream_storage.py")


def missing(operation: str) -> ClientError:
    return ClientError({"Error": {"Code": "404"}}, operation)


def manifest_document(scenario_id: str, keys: list[str]) -> dict:
    return {
        "format": "fimbox-scenario-tiles", "version": 1, "scenario_id": scenario_id, "tile_size": 2048,
        "hucs": {"03020201": {"crs": "EPSG:5070", "resolution": 10.0}},
        "bounds_wgs84": [-78, 35, -77, 36],
        "tiles": [{"key": key, "bounds_wgs84": [-78, 35, -77, 36]} for key in keys],
    }


class StreamingOptionTests(unittest.TestCase):
    def test_streaming_requires_s3_and_one_process(self):
        HandFimJobOptions(**DESTINATION, execution_mode="streaming", max_procs=1)
        with self.assertRaisesRegex(ValidationError, "requires output_bucket"):
            HandFimJobOptions(output_bucket=None, output_prefix="", execution_mode="streaming")
        with self.assertRaisesRegex(ValidationError, "one process"):
            HandFimJobOptions(**DESTINATION, execution_mode="streaming", max_procs=2)
        self.assertEqual(HandFimJobOptions(**DESTINATION).execution_mode, "legacy")


class _Lazy(dict):
    """Manifest document computed when the worker reads it (after the attempt ID exists)."""

    def __init__(self, factory):
        super().__init__()
        self.factory = factory


class StreamingWorkerTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.job_id = str(uuid4())
        patches = [
            patch.object(tasks.settings, "hand_fim_input_dir", self.root / "input"),
            patch.object(tasks.settings, "hand_fim_output_dir", self.root / "output"),
            patch.object(tasks.settings, "hand_fim_entry", self.root / "entry.py"),
            patch.object(tasks.settings, "stream_input_bucket", "hydrofabric"),
            patch.object(tasks.settings, "stream_input_prefix", "hand/v1"),
            patch.object(tasks, "job_s3_environment", return_value=dict(SECRETS)),
            patch.object(tasks, "clear_job_logs"),
            patch.object(tasks, "append_job_log"),
        ]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = patch.object(tasks, "s3_client")
        self.client = patcher.start().return_value
        self.addCleanup(patcher.stop)
        self.client.head_object.side_effect = missing("HeadObject")
        self.client.get_paginator.return_value.paginate.return_value = [
            {"Contents": [{"Key": "partial"}]},
        ]

    def spawn(self, tile_key=None):
        def run(command, **kwargs):
            env = kwargs["env"]
            self.command, self.env = command, env
            prefix = "data/output/s1"
            document = manifest_document("s1", [tile_key or f"{prefix}/03020201/0_0.cog"])
            Path(command[-1], "s1.manifest.json").write_text(json.dumps(document))
            return Mock(stdout=io.StringIO("streamed\n"), wait=Mock(return_value=0))
        return run

    def test_streams_with_credentials_only_in_environment_and_publishes_manifest(self):
        with patch.object(tasks.subprocess, "Popen", side_effect=self.spawn()):
            result = tasks.hand_fim.run(self.job_id, PAYLOAD, **DESTINATION, execution_mode="streaming")
        self.assertEqual(self.command[2], "stream-scenario")
        self.assertNotIn("test-secret", " ".join(self.command))
        self.assertEqual(self.env["AWS_SECRET_ACCESS_KEY"], "test-secret")
        self.assertEqual(self.env["FIMBOX_STREAM_INPUT_BUCKET"], "hydrofabric")
        self.assertEqual(self.env["FIMBOX_STREAM_OUTPUT_PREFIX"], "data/output")
        published = json.loads(self.client.put_object.call_args.kwargs["Body"])
        self.assertEqual(published["tiles"][0]["key"], "data/output/s1/03020201/0_0.cog")
        self.assertNotIn("FIMBOX_STREAM_ATTEMPT", self.env)
        self.client.put_object.assert_called_once()
        self.assertEqual(self.client.put_object.call_args.kwargs["Key"], "data/output/s1/manifest.json")
        self.assertEqual(result["execution_mode"], "streaming")
        self.assertEqual(result["files"], [])
        self.assertEqual(result["scenario_manifests"][0]["key"], "data/output/s1/manifest.json")
        self.assertEqual(result["scenario_manifests"][0]["tile_count"], 1)
        self.client.delete_objects.assert_not_called()

    def test_tile_outside_scenario_prefix_fails_without_deleting_shared_tiles(self):
        with patch.object(tasks.subprocess, "Popen", side_effect=self.spawn("data/output/other/x.cog")):
            with self.assertRaisesRegex(RuntimeError, "invalid tile key"):
                tasks.hand_fim.run(self.job_id, PAYLOAD, **DESTINATION, execution_mode="streaming")
        self.client.put_object.assert_not_called()
        self.client.get_paginator.assert_not_called()
        self.client.delete_objects.assert_not_called()

    def test_existing_manifest_is_rejected_before_computing(self):
        self.client.head_object.side_effect = None
        with patch.object(tasks.subprocess, "Popen") as run:
            with self.assertRaisesRegex(RuntimeError, "already exists"):
                tasks.hand_fim.run(self.job_id, PAYLOAD, **DESTINATION, execution_mode="streaming")
        run.assert_not_called()

    def test_overwrite_removes_replaced_tiles_after_publishing(self):
        for layout in ("attempts/old", "old"):
            with self.subTest(layout=layout):
                key = f"data/output/s1/{layout}/03020201/0_0.cog"
                old = manifest_document("s1", [
                    key, "data/output/s1/03020201/0_0.cog", "elsewhere/x.cog",
                ])
                self.client.get_object.return_value = {"Body": io.BytesIO(json.dumps(old).encode())}
                with patch.object(tasks.subprocess, "Popen", side_effect=self.spawn()):
                    tasks.hand_fim.run(
                        self.job_id, PAYLOAD, **DESTINATION, overwrite=True, execution_mode="streaming",
                    )
                deleted = self.client.delete_objects.call_args.kwargs["Delete"]["Objects"]
                self.assertEqual(deleted, [{"Key": key}])

    def run_two_scenarios_with_second_publish_failing(self, current_manifest):
        payload = [PAYLOAD[0], {**PAYLOAD[0], "ScenarioID": "s2"}]
        old = manifest_document("s1", ["data/output/s1/attempts/old/03020201/0_0.cog"])
        reads = iter([old, None, current_manifest])
        def get_object(**_kwargs):
            document = next(reads)
            if document is None:
                raise missing("GetObject")
            if isinstance(document, _Lazy):
                document = document.factory()
            return {"Body": io.BytesIO(json.dumps(document).encode())}

        self.client.get_object.side_effect = get_object
        self.client.put_object.side_effect = [None, ClientError({"Error": {"Code": "500"}}, "PutObject"), None]

        def run(command, **kwargs):
            for sid in ("s1", "s2"):
                document = manifest_document(sid, [f"data/output/{sid}/03020201/0_0.cog"])
                Path(command[-1], f"{sid}.manifest.json").write_text(json.dumps(document))
            return Mock(stdout=io.StringIO(""), wait=Mock(return_value=0))

        with patch.object(tasks.subprocess, "Popen", side_effect=run):
            with self.assertRaises(ClientError):
                tasks.hand_fim.run(self.job_id, payload, **DESTINATION, overwrite=True, execution_mode="streaming")
        return old

    def test_failed_publish_restores_replaced_manifest(self):
        current = lambda: json.loads(self.client.put_object.call_args_list[0].kwargs["Body"])
        old = self.run_two_scenarios_with_second_publish_failing(_Lazy(current))
        restored = self.client.put_object.call_args_list[-1].kwargs
        self.assertEqual(restored["Key"], "data/output/s1/manifest.json")
        self.assertEqual(json.loads(restored["Body"]), old)
        self.client.delete_object.assert_not_called()
        self.client.delete_objects.assert_not_called()

    def test_failed_publish_leaves_newer_manifest_from_another_job(self):
        newer = manifest_document("s1", ["data/output/s1/03020201/0_0.cog"])
        newer["publication_id"] = "other-job"
        self.run_two_scenarios_with_second_publish_failing(newer)
        self.assertEqual(self.client.put_object.call_count, 2)
        self.client.delete_object.assert_not_called()
        self.client.delete_objects.assert_not_called()

    def test_unconfigured_worker_fails_explicitly(self):
        with patch.object(tasks.settings, "stream_input_bucket", ""), patch.object(tasks.subprocess, "Popen") as run:
            with self.assertRaisesRegex(RuntimeError, "not configured"):
                tasks.hand_fim.run(self.job_id, PAYLOAD, **DESTINATION, execution_mode="streaming")
        run.assert_not_called()


class StreamingApiTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(main.app)
        self.session = auth.UserSession(user_id="alice", access_token="token", scope="read", expires_at=time.time() + 60)
        main.app.dependency_overrides[auth.get_authenticated_session] = lambda: self.session
        self.addCleanup(main.app.dependency_overrides.clear)
        for name, value in (
            ("hydroshare_client_id", "test-client"),
            ("hydroshare_client_secret", SecretStr("test-secret")),
            ("stream_input_bucket", "hydrofabric"),
        ):
            patcher = patch.object(main.settings, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        for name in ("track_user_job", "untrack_user_job", "user_has_job", "create_job_credentials"):
            patcher = patch.object(main, name, return_value=True)
            setattr(self, name, patcher.start())
            self.addCleanup(patcher.stop)
        self.manifest = ScenarioManifest(
            ScenarioID="s1", key="data/output/s1/manifest.json", tile_count=2,
            bounds=(-78, 35, -77, 36), resolution=10.0, tile_size=2048,
        )
        self.result = {
            "outputs": [{"ScenarioID": "s1"}], **DESTINATION, "execution_mode": "streaming",
            "scenario_manifests": [self.manifest.model_dump(mode="json")],
        }

    def submit(self, **options):
        with patch.object(tasks.hand_fim, "apply_async") as enqueue:
            response = self.client.post("/jobs/hand-fim", json={"scenarios": PAYLOAD, **DESTINATION, **options})
        return response, enqueue

    def test_streaming_submission_is_queued_with_mode_and_optional_queue(self):
        response, enqueue = self.submit(execution_mode="streaming")
        self.assertEqual(response.status_code, 202)
        self.assertEqual(enqueue.call_args.kwargs["args"][-1], "streaming")
        self.assertNotIn("queue", enqueue.call_args.kwargs)
        with patch.object(main.settings, "stream_queue", "streaming"):
            _response, enqueue = self.submit(execution_mode="streaming")
        self.assertEqual(enqueue.call_args.kwargs["queue"], "streaming")
        _response, enqueue = self.submit()
        self.assertEqual(len(enqueue.call_args.kwargs["args"]), 6)

    def test_streaming_submission_requires_configuration_and_valid_options(self):
        with patch.object(main.settings, "stream_input_bucket", ""):
            response, enqueue = self.submit(execution_mode="streaming")
        self.assertEqual(response.status_code, 409)
        enqueue.assert_not_called()
        response, _ = self.submit(execution_mode="streaming", max_procs=4)
        self.assertEqual(response.status_code, 422)

    def test_scenario_info_and_tile_use_owned_manifest(self):
        with patch.object(main.celery_app, "AsyncResult", return_value=Mock(state="SUCCESS", result=self.result)), \
                patch.object(main, "session_s3_client") as client, \
                patch.object(main, "load_scenario_tiles", return_value=[("k", (0, 0, 1, 1))]) as load_tiles, \
                patch.object(main, "scenario_tile", return_value=b"png") as render:
            info = self.client.get(f"/jobs/{uuid4()}/scenarios/0/info")
            tile = self.client.get(f"/jobs/{uuid4()}/scenarios/0/tiles/9/140/200.png")
            missing_index = self.client.get(f"/jobs/{uuid4()}/scenarios/1/info")
        self.assertEqual(info.status_code, 200)
        self.assertEqual(info.json()["bounds"], [-78, 35, -77, 36])
        self.assertEqual((tile.status_code, tile.content), (200, b"png"))
        self.assertEqual(load_tiles.call_args.args[1:], ("fim-output", self.manifest))
        self.assertEqual(render.call_args.args[:5], ([("k", (0, 0, 1, 1))], 9, 140, 200, 16))
        self.assertEqual(missing_index.status_code, 404)
        client.assert_called_once()

    def test_foreign_job_and_too_many_tiles_are_rejected(self):
        self.user_has_job.return_value = False
        self.assertEqual(self.client.get(f"/jobs/{uuid4()}/scenarios/0/info").status_code, 403)
        self.user_has_job.return_value = True
        with patch.object(main.celery_app, "AsyncResult", return_value=Mock(state="SUCCESS", result=self.result)), \
                patch.object(main, "session_s3_client"), patch.object(main, "load_scenario_tiles", return_value=[]), \
                patch.object(main, "scenario_tile", side_effect=rasters.TooManyTilesError("Zoom in")):
            response = self.client.get(f"/jobs/{uuid4()}/scenarios/0/tiles/2/1/1.png")
        self.assertEqual(response.status_code, 400)


class ScenarioCompositingTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        crs = CRS.from_epsg(5070)
        origin = Affine(10, 0, 1500000, 0, -10, 1600000)
        self.tiles = []
        for col in range(2):
            flooded = np.zeros((512, 512), dtype=bool)
            flooded[100:400, 100:400] = True
            transform = origin @ Affine.translation(col * 512, 0)
            path = self.root / f"0_{col}.cog"
            path.write_bytes(stream.encode_tile(flooded, transform, crs))
            with rasterio.open(path) as dataset:
                bounds = dataset.bounds
            from rasterio.warp import transform_bounds
            self.tiles.append((path.name, tuple(transform_bounds(crs, "EPSG:4326", *bounds))))

    def source(self, key):
        return storage.RasterSource(str(self.root / key))

    def test_union_renders_flooded_pixels_and_limits_tile_count(self):
        import morecantile
        west, south, east, north = self.tiles[0][1]
        tile = morecantile.tms.get("WebMercatorQuad").tile((west + east) / 2, (south + north) / 2, 12)
        png = rasters.scenario_tile(self.tiles, tile.z, tile.x, tile.y, 16, self.source)
        with rasterio.MemoryFile(png) as memory, memory.open() as dataset:
            self.assertTrue(np.any(dataset.read(4) > 0))
        with self.assertRaises(rasters.TooManyTilesError):
            rasters.scenario_tile(self.tiles, 5, *morecantile.tms.get("WebMercatorQuad").tile(west, south, 5)[:2], 1, self.source)
        empty = rasters.scenario_tile(self.tiles, 12, 0, 0, 16, self.source)
        with rasterio.MemoryFile(empty) as memory, memory.open() as dataset:
            self.assertFalse(np.any(dataset.read(dataset.count) > 0))

    def test_manifest_loading_validates_keys_and_revalidates_cache(self):
        manifest = ScenarioManifest(
            ScenarioID="s1", key="out/s1/manifest.json", tile_count=1,
            bounds=(0, 0, 1, 1), resolution=10.0, tile_size=2048,
        )
        client = Mock()
        client.get_object.return_value = {
            "Body": io.BytesIO(json.dumps(manifest_document("s1", ["out/s1/attempts/a/0_0.cog"])).encode()),
            "ETag": '"v1"',
        }
        tiles = rasters.load_scenario_tiles(client, "bucket", manifest)
        self.assertEqual(tiles[0][0], "out/s1/attempts/a/0_0.cog")
        client.get_object.side_effect = ClientError({"Error": {"Code": "304"}}, "GetObject")
        self.assertEqual(rasters.load_scenario_tiles(client, "bucket", manifest), tiles)
        self.assertEqual(client.get_object.call_args.kwargs["IfNoneMatch"], '"v1"')
        client.get_object.side_effect = None
        for index, key in enumerate((
            "out/s1/03020201/2_0.cog",
            "out/s1/run-id/03020201/2_0.cog",
        )):
            with self.subTest(key=key):
                client.get_object.return_value = {
                    "Body": io.BytesIO(json.dumps(manifest_document("s1", [key])).encode()),
                    "ETag": f'"stable-{index}"',
                }
                tiles = rasters.load_scenario_tiles(client, "bucket", manifest)
                self.assertEqual(tiles[0][0], key)
        for key in (
            "other/secret.cog", "out/s10/secret.cog",
            "out/s1/../secret.cog", "out/s1/manifest.json",
        ):
            with self.subTest(invalid_key=key):
                client.get_object.return_value = {
                    "Body": io.BytesIO(json.dumps(manifest_document("s1", [key])).encode()),
                    "ETag": '"v2"',
                }
                with self.assertRaises(rasters.RasterReadError):
                    rasters.load_scenario_tiles(client, "bucket", manifest)

    def test_info_minzoom_bounds_tiles_per_map_tile(self):
        manifest = ScenarioManifest(
            ScenarioID="s1", key="k", tile_count=1, bounds=(0, 0, 1, 1), resolution=10.0, tile_size=2048,
        )
        info = rasters.scenario_info(manifest, 16)
        span = rasters.EARTH_CIRCUMFERENCE / 2 ** info.minzoom
        self.assertLessEqual(span, 3 * 2048 * 10)
        self.assertGreaterEqual(info.maxzoom, info.minzoom)


class StreamStorageTests(unittest.TestCase):
    def config(self, **overrides):
        environ = {
            "FIMBOX_STREAM_INPUT_BUCKET": "hydrofabric", "FIMBOX_STREAM_INPUT_PREFIX": "hand/v1",
            "FIMBOX_STREAM_OUTPUT_BUCKET": "fim-output", "FIMBOX_STREAM_OUTPUT_PREFIX": "data/output",
            "FIMBOX_S3_ENDPOINT_URL": "https://s3.example.org",
            **SECRETS, **overrides,
        }
        return stream_storage.StreamConfig.from_env(environ)

    def test_missing_configuration_is_explicit(self):
        with self.assertRaisesRegex(RuntimeError, "FIMBOX_STREAM_INPUT_BUCKET"):
            self.config(FIMBOX_STREAM_INPUT_BUCKET="")

    def test_reads_index_writes_tiles_and_manifests(self):
        input_client, output_client = Mock(), Mock()
        input_client.get_object.side_effect = lambda Bucket, Key: (
            {"Body": io.BytesIO(b'{"ok": true}'), "ContentLength": 12} if Key == "hand/v1/dataset.json"
            else (_ for _ in ()).throw(ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject"))
        )
        seen = {}

        def run_scenario(scenario, read_json, raster_path, write_tile, tile_size, log):
            seen.update(
                dataset=read_json("dataset.json"), absent=read_json("hucs/x/huc.json"),
                raster=raster_path("hucs/x/rem.tif"), key=write_tile("x/0_0.cog", b"tile"),
                tile_size=tile_size,
            )
            return {"tiles": [{"key": seen["key"]}]}

        module = SimpleNamespace(
            GDAL_STREAMING_OPTIONS={}, run_scenario=run_scenario,
            dumps_manifest=lambda manifest: json.dumps(manifest).encode(),
        )
        with tempfile.TemporaryDirectory() as directory, patch.object(
            stream_storage.boto3, "client", side_effect=[input_client, output_client],
        ):
            paths = stream_storage.run_streaming(
                [{"ScenarioID": "s1"}], Path(directory), module, self.config(), log=lambda _: None,
            )
            self.assertEqual(json.loads(paths[0].read_text()), {"tiles": [{"key": "data/output/s1/x/0_0.cog"}]})
        self.assertEqual(seen["dataset"], {"ok": True})
        self.assertIsNone(seen["absent"])
        self.assertEqual(seen["raster"], "s3://hydrofabric/hand/v1/hucs/x/rem.tif")
        self.assertEqual(seen["tile_size"], 2048)
        output_client.put_object.assert_called_once()
        self.assertEqual(output_client.put_object.call_args.kwargs["Bucket"], "fim-output")


class JobEnvironmentTests(unittest.TestCase):
    def test_short_lived_credentials_are_rejected(self):
        from datetime import datetime, timedelta, timezone
        credentials = storage.HydroShareS3Credentials(
            access_key="a", secret_key="b", expiry=datetime.now(timezone.utc) + timedelta(minutes=5),
        )
        with patch.object(storage, "__job_credentials", return_value=credentials):
            with self.assertRaisesRegex(storage.StorageCredentialsError, "too soon"):
                storage.job_s3_environment(uuid4(), 3600)
            environment = storage.job_s3_environment(uuid4(), 60)
        self.assertEqual(environment["AWS_SECRET_ACCESS_KEY"], "b")


if __name__ == "__main__":
    unittest.main()
