import base64
import json
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import Mock, patch
from uuid import uuid4

import httpx
import numpy as np
import rasterio
from fastapi.testclient import TestClient
from rasterio.shutil import copy as copy_raster
from rasterio.session import AWSSession
from rasterio.transform import from_bounds
from pydantic import SecretStr

import fimbox_map
from fimbox_api import auth, main, rasters, storage
from fimbox_api.models import RasterInfo
from fimbox_sdk import AuthenticatedClient
from fimbox_sdk.models import StoredHandFimResult


PNG = b"\x89PNG\r\n\x1a\n" + b"test-png"


def manifest():
    return {
        "outputs": [], "output_bucket": "resource", "output_prefix": "outputs",
        "files": [
            {"path": "flood.cog", "key": "outputs/flood.cog", "size_bytes": 123},
            {"path": "flood.tif", "key": "outputs/flood.tif", "size_bytes": 456},
            {"path": "logs.txt", "key": "outputs/logs.txt", "size_bytes": 30},
        ],
    }


class RasterApiTests(unittest.TestCase):
    def setUp(self):
        self.job_id = uuid4()
        self.base = f"/jobs/{self.job_id}/rasters"
        self.session = auth.UserSession(
            user_id="alice", access_token="provider-secret", scope="read", expires_at=time.time() + 600,
        )
        main.app.dependency_overrides[auth.get_authenticated_session] = lambda: self.session
        self.addCleanup(main.app.dependency_overrides.clear)
        self.client = TestClient(main.app)
        self.addCleanup(self.client.close)
        self.job = Mock(state="SUCCESS", result=manifest())
        for name, value in (
            ("user_has_job", True),
            ("raster_source", storage.RasterSource("s3://resource/outputs/flood.cog")),
            ("raster_info", RasterInfo(bounds=(-80, 30, -79, 31), minzoom=5, maxzoom=15)),
            ("raster_tile", PNG),
        ):
            patcher = patch.object(main, name, return_value=value)
            setattr(self, name, patcher.start())
            self.addCleanup(patcher.stop)
        patcher = patch.object(main.celery_app, "AsyncResult", return_value=self.job)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_info_and_png_use_only_manifest_object_and_are_not_cached(self):
        response = self.client.get(self.base + "/0/info")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["bounds"], [-80, 30, -79, 31])
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertNotIn("secret", response.text)
        response = self.client.get(self.base + "/1/tiles/5/8/12.png")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, PNG)
        self.assertEqual(response.headers["content-type"], "image/png")
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.raster_source.assert_called_with(self.job_id, self.session, "resource", "outputs/flood.tif")
        self.raster_tile.assert_called_once_with(self.raster_source.return_value, 5, 8, 12)

    def test_foreign_jobs_are_denied_before_any_object_access(self):
        self.user_has_job.return_value = False
        for suffix in ("/0/info", "/0/tiles/0/0/0.png"):
            self.assertEqual(self.client.get(self.base + suffix).status_code, 403)
        self.raster_source.assert_not_called()

    def test_missing_authentication_is_denied(self):
        main.app.dependency_overrides.clear()
        with patch.object(main.settings, "hydroshare_client_id", "test"), patch.object(
            main.settings, "hydroshare_client_secret", SecretStr("test"),
        ):
            self.assertEqual(self.client.get(self.base + "/0/info").status_code, 401)
        self.raster_source.assert_not_called()

    def test_invalid_file_indices_types_states_and_coordinates_do_not_read_s3(self):
        for suffix, status in (
            ("/-1/info", 422), ("/99/info", 404), ("/2/info", 400),
            ("/0/tiles/23/0/0.png", 422), ("/0/tiles/1/2/0.png", 400),
            ("/0/tiles/1/0/2.png", 400), ("/0/tiles/1/-1/0.png", 422),
        ):
            self.assertEqual(self.client.get(self.base + suffix).status_code, status, suffix)
        self.job.state = "STARTED"
        self.assertEqual(self.client.get(self.base + "/0/info").status_code, 409)
        self.job.state = "SUCCESS"
        self.job.result = {**manifest(), "output_bucket": None, "output_prefix": ""}
        self.assertEqual(self.client.get(self.base + "/0/info").status_code, 409)
        self.raster_source.assert_not_called()

    def test_read_errors_are_explicit_and_sanitized(self):
        self.raster_tile.side_effect = rasters.RasterReadError("Could not read job raster tile from S3.")
        response = self.client.get(self.base + "/0/tiles/0/0/0.png")
        self.assertEqual(response.status_code, 502)
        self.assertNotIn("signature", response.text)
        self.assertIn("Could not read", response.text)


class RasterReadTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "flood.cog"
        source = Path(self.directory.name) / "source.tif"
        # Large enough to distinguish window/range reads from a complete download.
        data = np.random.default_rng(42).integers(0, 2, (4096, 4096), dtype=np.uint8)
        data[:256, :256] = 0
        with rasterio.open(
            source, "w", driver="GTiff", width=4096, height=4096, count=1, dtype="uint8",
            crs="EPSG:3857", transform=from_bounds(-1000000, 0, 0, 1000000, 4096, 4096),
        ) as dataset:
            dataset.write(data, 1)
        copy_raster(source, self.path, driver="COG", compress="DEFLATE", blocksize=256)

    def test_real_cog_metadata_and_tiles_use_http_ranges(self):
        body = self.path.read_bytes()
        ranges = []
        authorization_headers = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_HEAD(self):
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Accept-Ranges", "bytes")
                self.end_headers()

            def do_GET(self):
                authorization_headers.append(self.headers.get("Authorization", ""))
                value = self.headers.get("Range")
                if not value:
                    self.send_error(400, "Range required")
                    return
                start, end = value.removeprefix("bytes=").split("-")
                start, end = int(start), min(int(end), len(body) - 1)
                ranges.append((start, end))
                self.send_response(206)
                self.send_header("Content-Range", f"bytes {start}-{end}/{len(body)}")
                self.send_header("Content-Length", str(end - start + 1))
                self.end_headers()
                self.wfile.write(body[start:end + 1])

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            source = storage.RasterSource("s3://resource/flood.cog", {
                "AWS_S3_ENDPOINT": f"127.0.0.1:{server.server_port}",
                "AWS_HTTPS": False,
                "AWS_VIRTUAL_HOSTING": False,
                "AWS_NO_SIGN_REQUEST": False,
            }, session=AWSSession(
                aws_access_key_id="test-access", aws_secret_access_key="test-secret",
                aws_session_token="", region_name="us-east-1", aws_unsigned=False,
            ))
            metadata = rasters.raster_info(source)
            self.assertLess(metadata.bounds[0], 0)
            png = rasters.raster_tile(source, 7, 63, 63)
            self.assertTrue(png.startswith(b"\x89PNG\r\n\x1a\n"))
            with rasterio.MemoryFile(png) as memory, memory.open() as dataset:
                self.assertEqual((dataset.width, dataset.height), (256, 256))
                self.assertEqual(dataset.count, 4)
                alpha = dataset.read(4)
                self.assertTrue(np.any(alpha > 0))
                self.assertTrue(np.any(alpha == 0))
            self.assertTrue(ranges)
            self.assertTrue(all(value.startswith("AWS4-HMAC-SHA256 ") for value in authorization_headers))
            self.assertLess(sum(end - start + 1 for start, end in ranges), len(body) // 2)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_outside_raster_is_transparent(self):
        png = rasters.raster_tile(storage.RasterSource(str(self.path)), 3, 0, 0)
        with rasterio.MemoryFile(png) as memory, memory.open() as dataset:
            self.assertTrue(np.all(dataset.read(4) == 0))

    def test_plain_tiff_can_also_be_tiled(self):
        source = storage.RasterSource(str(Path(self.directory.name) / "source.tif"))
        self.assertTrue(rasters.raster_tile(source, 7, 63, 63).startswith(b"\x89PNG\r\n\x1a\n"))

    def test_invalid_raster_error_does_not_expose_url(self):
        with self.assertRaisesRegex(rasters.RasterReadError, "metadata"):
            rasters.raster_info(storage.RasterSource(str(Path(self.directory.name) / "missing-secret.cog")))


class RasterCredentialsTests(unittest.TestCase):
    def test_expired_redis_entry_is_renewed_using_current_session(self):
        job_id = uuid4()
        session = Mock()
        credentials = storage.HydroShareS3Credentials(
            access_key="access", secret_key="secret", expiry=datetime.now(timezone.utc) + timedelta(hours=1),
        )
        with patch.object(storage, "get_redis_client") as redis, patch.object(
            storage, "create_job_credentials",
        ) as renew, patch.object(storage, "__job_credentials", return_value=credentials):
            redis.return_value.get.return_value = None
            source = storage.raster_source(job_id, session, "resource", "flood.cog")
        renew.assert_called_once_with(job_id, session)
        self.assertEqual(source.path, "s3://resource/flood.cog")
        self.assertEqual(source.options["AWS_S3_ENDPOINT"], "s3.hydroshare.org")
        self.assertEqual(source.session.credentials["aws_access_key_id"], "access")
        self.assertEqual(source.session.credentials["aws_secret_access_key"], "secret")
        self.assertFalse(source.options["AWS_VIRTUAL_HOSTING"])
        self.assertNotIn("secret", repr(source))

    def test_near_expiry_credentials_are_renewed_before_read(self):
        job_id, session = uuid4(), Mock()
        old = storage.HydroShareS3Credentials(
            access_key="old", secret_key="old-secret",
            expiry=datetime.now(timezone.utc) + timedelta(seconds=30),
        )
        fresh = storage.HydroShareS3Credentials(
            access_key="fresh", secret_key="fresh-secret",
            expiry=datetime.now(timezone.utc) + timedelta(hours=1),
        )
        with patch.object(storage, "get_redis_client"), patch.object(
            storage, "create_job_credentials",
        ) as renew, patch.object(storage, "__job_credentials", side_effect=[old, fresh]):
            source = storage.raster_source(job_id, session, "resource", "flood.cog")
        renew.assert_called_once_with(job_id, session)
        self.assertEqual(source.session.credentials["aws_access_key_id"], "fresh")


class NotebookMapTests(unittest.TestCase):
    def test_notebook_map_cell_is_unexecuted_and_uses_existing_session(self):
        notebook = json.loads((Path(__file__).resolve().parents[1] / "notebooks/cloud_hand_fim.ipynb").read_text())
        cells = {cell["id"]: cell for cell in notebook["cells"]}
        cell = cells["map"]
        self.assertEqual(cell["outputs"], [])
        self.assertIsNone(cell["execution_count"])
        source = "".join(cell["source"])
        compile(source, "map", "exec")
        self.assertIn("job_map(api_client, job_id, final_result)", source)
        self.assertNotIn("session_token", source)

    def test_tile_selection_is_bounded_and_clipped(self):
        visible = fimbox_map.__dict__["__visible_tiles"]
        tiles = visible(((-85, -180), (85, 180)), 22, (-180, -85, 180, 85))
        self.assertLessEqual(len(tiles), 64)
        self.assertEqual(visible(((20, 20), (30, 30)), 8, (-80, 30, -79, 31)), [])
        bounds = fimbox_map.__dict__["__tile_bounds"](0, 0, 0)
        self.assertAlmostEqual(bounds[0][0], -85.05112878, places=6)
        self.assertEqual(bounds[1][1], 180)

    def test_widget_fetches_authenticated_pngs_without_credentials_in_urls(self):
        requests = []

        def respond(request):
            requests.append(request)
            self.assertEqual(request.headers["authorization"], "Bearer kernel-only-token")
            self.assertNotIn("kernel-only-token", str(request.url))
            if request.url.path.endswith("/info"):
                return httpx.Response(200, json={
                    "bounds": [-80, 30, -79, 31], "minzoom": 5, "maxzoom": 15,
                })
            return httpx.Response(200, content=PNG, headers={"content-type": "image/png"})

        client = AuthenticatedClient(
            base_url="https://api.example", token="kernel-only-token",
            httpx_args={"transport": httpx.MockTransport(respond)},
        )
        with client, patch("ipyleaflet.Map.fit_bounds") as fit_bounds:
            widget = fimbox_map.job_map(client, uuid4(), StoredHandFimResult.from_dict(manifest()))
            fit_bounds.assert_called_once_with(((30, -80), (31, -79)))
            selector, button, status, map_widget = widget.children
            map_widget.set_trait("bounds", ((30, -80), (31, -79)))
            self.assertGreater(len(map_widget.layers), 1)
            for layer in map_widget.layers[1:]:
                self.assertEqual(layer.url, "data:image/png;base64," + base64.b64encode(PNG).decode())
            self.assertIn("visible tiles", status.value)
            self.assertLessEqual(len(requests), 65)
            before = len(requests)
            map_widget.set_trait("bounds", ((30.01, -80), (31, -79)))
            self.assertEqual(len(requests), before, "Visible tiles should be cached in kernel memory")
            selector.value = 1
            self.assertTrue(any("/rasters/1/" in str(request.url) for request in requests))
            button.click()
            self.assertIn("visible tiles", status.value)
            map_widget.close()
            widget.close()

    def test_missing_storage_is_explicit(self):
        result = StoredHandFimResult.from_dict({**manifest(), "output_bucket": None, "output_prefix": ""})
        with self.assertRaisesRegex(ValueError, "retained S3"):
            fimbox_map.job_map(Mock(), uuid4(), result)

    def test_tile_http_failure_is_visible_and_does_not_leave_stale_overlays(self):
        def respond(request):
            if request.url.path.endswith("/info"):
                return httpx.Response(200, json={
                    "bounds": [-80, 30, -79, 31], "minzoom": 5, "maxzoom": 15,
                })
            return httpx.Response(401)

        client = AuthenticatedClient(
            base_url="https://api.example", token="test",
            httpx_args={"transport": httpx.MockTransport(respond)},
        )
        with client, patch("ipyleaflet.Map.fit_bounds"):
            widget = fimbox_map.job_map(client, uuid4(), StoredHandFimResult.from_dict(manifest()))
            _, _, status, map_widget = widget.children
            map_widget.set_trait("bounds", ((30, -80), (31, -79)))
            self.assertIn("HTTP 401", status.value)
            self.assertEqual(len(map_widget.layers), 1)
            widget.close()
            map_widget.close()
