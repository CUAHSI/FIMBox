import io
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from botocore.exceptions import ClientError
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
