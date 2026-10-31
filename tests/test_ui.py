import io
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from botocore.exceptions import ClientError
import httpx
from fastapi.testclient import TestClient
from pydantic import SecretStr

from fimbox_api import auth, main, ui


def client_error(code):
    return ClientError({"Error": {"Code": code}}, "GetObject")


class UiTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(main.app)
        self.patch_settings(
            hydroshare_client_id="test-client",
            hydroshare_client_secret=SecretStr("test-secret"),
            stream_input_bucket="",
            stream_input_prefix="",
            stream_input_unsigned=False,
        )
        main.app.dependency_overrides[auth.get_authenticated_session] = lambda: auth.UserSession(
            user_id="alice", access_token="provider-token", scope="read", expires_at=time.time() + 60,
        )
        self.addCleanup(main.app.dependency_overrides.clear)
        self.s3 = Mock()
        self.output_client = self.patch(ui, "output_s3_client", return_value=self.s3)
        self.anonymous = Mock()
        self.anonymous_client = self.patch(ui, "anonymous_s3_client", return_value=self.anonymous)

    def patch(self, target, name, **kwargs):
        patcher = patch.object(target, name, **kwargs)
        mock = patcher.start()
        self.addCleanup(patcher.stop)
        return mock

    def patch_settings(self, **values):
        for name, value in values.items():
            patcher = patch.object(main.settings, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_page_and_assets_are_served_with_security_headers(self):
        response = self.client.get("/ui")
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/html", response.headers["content-type"])
        self.assertIn("FIMBox", response.text)
        self.assertIn("default-src 'self'", response.headers["content-security-policy"])
        self.assertEqual(response.headers["x-content-type-options"], "nosniff")
        self.assertEqual(response.headers["cache-control"], "no-store")
        for asset in ("app.js", "app.css"):
            response = self.client.get(f"/ui/static/{asset}")
            self.assertEqual(response.status_code, 200)
            self.assertNotEqual(response.headers.get("cache-control"), "no-store")

    def test_ui_routes_are_not_in_openapi(self):
        self.assertFalse([path for path in main.app.openapi()["paths"] if path.startswith("/ui")])

    def test_config_without_streaming(self):
        config = self.client.get("/ui/config").json()
        self.assertTrue(config["authentication_enabled"])
        self.assertFalse(config["streaming_enabled"])
        self.assertEqual(config["execution_modes"], ["legacy", "updatedlegacy"])
        self.assertIsNone(config["input_dataset"])

    def test_config_with_streaming_dataset(self):
        self.patch_settings(stream_input_bucket="resource", stream_input_prefix="/abc/data/contents/",
                            stream_input_unsigned=True)
        config = self.client.get("/ui/config").json()
        self.assertTrue(config["streaming_enabled"])
        self.assertIn("updatedstreaming", config["execution_modes"])
        self.assertEqual(config["input_dataset"], {
            "bucket": "resource", "prefix": "abc/data/contents", "unsigned": True,
        })

    def test_examples_include_test_data_and_skip_invalid_files(self):
        names = {example["name"] for example in self.client.get("/ui/examples").json()}
        self.assertIn("single_reach", names)
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "good.json").write_text(json.dumps([{"ScenarioID": "a", "Reaches": []}]))
            Path(directory, "bad.json").write_text("{not json")
            Path(directory, "object.json").write_text("{}")
            self.patch_settings(examples_dir=Path(directory))
            examples = self.client.get("/ui/examples").json()
        self.assertEqual(examples, [{"name": "good", "scenarios": [{"ScenarioID": "a", "Reaches": []}]}])

    def test_list_objects_returns_folders_files_and_token(self):
        self.s3.list_objects_v2.return_value = {
            "CommonPrefixes": [{"Prefix": "out/run/a/"}],
            "Contents": [{"Key": "out/run/", "Size": 0}, {"Key": "out/run/README.md", "Size": 12}],
            "IsTruncated": True, "NextContinuationToken": "next",
        }
        response = self.client.get("/ui/objects", params={"bucket": "fim", "prefix": "out/run", "token": "t"})
        self.assertEqual(response.status_code, 200)
        listing = response.json()
        self.assertEqual(listing["prefix"], "out/run/")
        self.assertEqual(listing["folders"], [{"name": "a", "prefix": "out/run/a/"}])
        self.assertEqual([item["name"] for item in listing["files"]], ["README.md"])
        self.assertEqual(listing["next_token"], "next")
        self.s3.list_objects_v2.assert_called_once_with(
            Bucket="fim", Prefix="out/run/", Delimiter="/", MaxKeys=ui.LIST_MAX_KEYS, ContinuationToken="t",
        )
        self.anonymous_client.assert_not_called()

    def test_invalid_paths_and_buckets_are_rejected(self):
        for params in ({"bucket": "fim", "prefix": "out/../secret"}, {"bucket": "fim", "prefix": "a//b"},
                       {"bucket": "../fim", "prefix": ""}):
            self.assertIn(self.client.get("/ui/objects", params=params).status_code, {400, 422})
        self.assertEqual(self.client.get("/ui/objects/download", params={"bucket": "fim", "key": "a/./b"}).status_code, 400)
        self.s3.list_objects_v2.assert_not_called()

    def test_local_mode_cannot_read_hydroshare_storage(self):
        self.patch_settings(hydroshare_client_id="", hydroshare_client_secret=None)
        response = self.client.get("/ui/objects", params={"bucket": "fim"})
        self.assertEqual(response.status_code, 409)
        self.output_client.assert_not_called()

    def test_unsigned_input_dataset_uses_anonymous_client_only_inside_dataset(self):
        self.patch_settings(hydroshare_client_id="", hydroshare_client_secret=None,
                            stream_input_bucket="resource", stream_input_prefix="abc/data",
                            stream_input_unsigned=True)
        self.anonymous.list_objects_v2.return_value = {}
        response = self.client.get("/ui/objects", params={"bucket": "resource", "prefix": "abc/data/hand"})
        self.assertEqual(response.status_code, 200)
        self.anonymous_client.assert_called_once()
        self.assertEqual(self.client.get("/ui/objects", params={"bucket": "resource", "prefix": "abc/database"}).status_code, 409)
        self.assertEqual(self.client.get("/ui/objects", params={"bucket": "other", "prefix": "abc/data"}).status_code, 409)

    def test_preview_returns_truncated_text(self):
        self.s3.get_object.return_value = {"Body": io.BytesIO(b"# Title\n"), "ContentRange": "bytes 0-7/900000"}
        response = self.client.get("/ui/objects/preview", params={"bucket": "fim", "key": "out/README.md"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {
            "key": "out/README.md", "size_bytes": 900000, "text": "# Title\n", "truncated": True,
        })
        self.s3.get_object.assert_called_once_with(
            Bucket="fim", Key="out/README.md", Range=f"bytes=0-{ui.PREVIEW_MAX_BYTES - 1}",
        )

    def test_preview_rejects_binary_files(self):
        response = self.client.get("/ui/objects/preview", params={"bucket": "fim", "key": "out/a.cog"})
        self.assertEqual(response.status_code, 400)
        self.s3.get_object.assert_not_called()

    def serve_parquet(self, content):
        def get_object(Bucket, Key, Range):
            start, end = map(int, Range.removeprefix("bytes=").split("-"))
            return {"Body": io.BytesIO(content[start:end + 1])}

        self.s3.head_object.return_value = {"ContentLength": len(content)}
        self.s3.get_object.side_effect = get_object

    def parquet_bytes(self, rows=250):
        import pyarrow as pa
        import pyarrow.parquet as pq

        table = pa.table({
            "feature_id": list(range(rows)),
            "name": [None if i == 1 else "x" * (300 if i == 2 else 3) for i in range(rows)],
            "blob": [b"\x00\x01"] * rows,
            "tags": [[i, i + 1] for i in range(rows)],
        })
        buffer = io.BytesIO()
        pq.write_table(table, buffer, row_group_size=100)
        return buffer.getvalue()

    def test_parquet_preview_returns_schema_and_first_rows(self):
        self.serve_parquet(self.parquet_bytes())
        response = self.client.get("/ui/objects/parquet", params={"bucket": "fim", "key": "in/reach.parquet", "limit": 3})
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["num_rows"], 250)
        self.assertEqual(body["num_row_groups"], 3)
        self.assertTrue(body["truncated"])
        self.assertEqual(body["columns"], [
            {"name": "feature_id", "type": "int64"},
            {"name": "name", "type": "string"},
            {"name": "blob", "type": "binary"},
            {"name": "tags", "type": "list<element: int64>"},
        ])
        self.assertEqual(body["rows"][0], ["0", "xxx", "<2 bytes>", "[0, 1]"])
        self.assertIsNone(body["rows"][1][1])
        self.assertEqual(len(body["rows"][2][1]), ui.PARQUET_MAX_CELL_CHARS)
        self.assertEqual(len(body["rows"]), 3)
        for call in self.s3.get_object.call_args_list:
            self.assertIn("Range", call.kwargs)

    def test_parquet_preview_validates_input(self):
        response = self.client.get("/ui/objects/parquet", params={"bucket": "fim", "key": "in/a.json"})
        self.assertEqual(response.status_code, 400)
        response = self.client.get("/ui/objects/parquet", params={"bucket": "fim", "key": "in/a.parquet", "limit": 0})
        self.assertEqual(response.status_code, 422)
        self.serve_parquet(b"not a parquet file at all")
        response = self.client.get("/ui/objects/parquet", params={"bucket": "fim", "key": "in/a.parquet"})
        self.assertEqual(response.status_code, 422)

    def test_parquet_preview_limits_bytes_read_and_maps_storage_errors(self):
        self.serve_parquet(self.parquet_bytes())
        with patch.object(ui, "PARQUET_MAX_READ_BYTES", 16):
            response = self.client.get("/ui/objects/parquet", params={"bucket": "fim", "key": "in/a.parquet"})
        self.assertEqual(response.status_code, 413)
        self.s3.head_object.side_effect = client_error("NoSuchKey")
        response = self.client.get("/ui/objects/parquet", params={"bucket": "fim", "key": "in/a.parquet"})
        self.assertEqual(response.status_code, 404)

    def test_load_scenarios_from_s3_with_user_credentials(self):
        self.s3.get_object.return_value = {"Body": io.BytesIO(b'[{"ScenarioID": "a", "Reaches": []}]')}
        response = self.client.get("/ui/scenarios/load", params={"url": "s3://resource/abc/data/contents/s.json"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["scenarios"], [{"ScenarioID": "a", "Reaches": []}])
        self.s3.get_object.assert_called_once_with(
            Bucket="resource", Key="abc/data/contents/s.json", Range=f"bytes=0-{ui.remote_json.MAX_BYTES}",
        )
        for url, status in (("s3://bad bucket/k.json", 400), ("s3://resource/a/../k.json", 400)):
            self.assertEqual(self.client.get("/ui/scenarios/load", params={"url": url}).status_code, status, url)
        self.s3.get_object.return_value = {"Body": io.BytesIO(b'{"ScenarioID": "a"}')}
        response = self.client.get("/ui/scenarios/load", params={"url": "s3://resource/abc/s.json"})
        self.assertEqual(response.status_code, 422)
        self.s3.get_object.return_value = {"Body": io.BytesIO(b"x" * (ui.remote_json.MAX_BYTES + 1))}
        response = self.client.get("/ui/scenarios/load", params={"url": "s3://resource/abc/s.json"})
        self.assertEqual(response.status_code, 413)

    def test_load_scenarios_from_https_maps_errors(self):
        with patch.object(ui.remote_json, "fetch_json", return_value=[{"ScenarioID": "a"}]) as fetch:
            response = self.client.get("/ui/scenarios/load", params={"url": "https://example.org/s.json"})
        self.assertEqual(response.json(), {"source": "https://example.org/s.json", "scenarios": [{"ScenarioID": "a"}]})
        fetch.assert_called_once_with("https://example.org/s.json")
        for error, status in (
            (ui.remote_json.RemoteJsonError("Only https:// URLs can be loaded"), 400),
            (httpx.ConnectTimeout("slow"), 502),
        ):
            with patch.object(ui.remote_json, "fetch_json", side_effect=error):
                response = self.client.get("/ui/scenarios/load", params={"url": "https://example.org/s.json"})
            self.assertEqual(response.status_code, status)

    def test_retrospective_streamflow_uses_whole_utc_days_and_maps_errors(self):
        flows = {
            "reach_id": 7, "start": ui.datetime(2018, 9, 1, tzinfo=ui.UTC), "end": ui.datetime(2018, 9, 1, 23, tzinfo=ui.UTC),
            "flows": [1.0, None], "min": 1.0, "mean": 1.0, "max": 1.0, "max_time": ui.datetime(2018, 9, 1, tzinfo=ui.UTC),
        }
        params = {"reach_id": 7, "start": "2018-09-01", "end": "2018-09-01"}
        with patch.object(ui.retrospective, "streamflow", return_value=flows) as streamflow:
            response = self.client.get("/ui/streamflow/retrospective", params=params)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["flows"], [1.0, None])
        streamflow.assert_called_once_with(
            7, ui.datetime(2018, 9, 1, tzinfo=ui.UTC), ui.datetime(2018, 9, 1, 23, tzinfo=ui.UTC),
        )
        for error, status in (
            (ui.retrospective.RetrospectiveError("Reach 7 is not in the NWM v3.0 retrospective"), 400),
            (httpx.ConnectTimeout("slow"), 502),
        ):
            with patch.object(ui.retrospective, "streamflow", side_effect=error):
                response = self.client.get("/ui/streamflow/retrospective", params=params)
            self.assertEqual(response.status_code, status)
        self.assertEqual(self.client.get("/ui/streamflow/retrospective", params={**params, "reach_id": 0}).status_code, 422)

    def test_storage_errors_are_mapped(self):
        for code, status in (("NoSuchKey", 404), ("AccessDenied", 403), ("SlowDown", 503)):
            self.s3.get_object.side_effect = client_error(code)
            response = self.client.get("/ui/objects/preview", params={"bucket": "fim", "key": "out/a.json"})
            self.assertEqual(response.status_code, status, code)

    def test_download_returns_presigned_url(self):
        self.s3.generate_presigned_url.return_value = "https://s3.example/signed"
        response = self.client.get("/ui/objects/download", params={"bucket": "fim", "key": "out/my file.cog"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"url": "https://s3.example/signed", "expires_in": ui.DOWNLOAD_URL_SECONDS})
        self.s3.head_object.assert_called_once_with(Bucket="fim", Key="out/my file.cog")
        self.s3.generate_presigned_url.assert_called_once_with(
            "get_object",
            Params={
                "Bucket": "fim", "Key": "out/my file.cog",
                "ResponseContentDisposition": "attachment; filename*=UTF-8''my%20file.cog",
            },
            ExpiresIn=ui.DOWNLOAD_URL_SECONDS,
        )

    def test_download_of_missing_object_is_404(self):
        self.s3.head_object.side_effect = client_error("404")
        response = self.client.get("/ui/objects/download", params={"bucket": "fim", "key": "out/missing.cog"})
        self.assertEqual(response.status_code, 404)
        self.s3.generate_presigned_url.assert_not_called()


if __name__ == "__main__":
    unittest.main()
