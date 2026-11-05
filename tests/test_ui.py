import io
import json
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from botocore.exceptions import ClientError
import httpx
from fastapi.testclient import TestClient
from pydantic import SecretStr

from fimbox_api import auth, main, ui, workspaces


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

    def test_page_remembers_workspaces_without_saved_location_tab_or_pickers(self):
        page = self.client.get("/ui").text
        self.assertNotIn('data-tab="locations"', page)
        self.assertNotIn('id="location-form"', page)
        self.assertIn('id="workspace-list"', page)
        self.assertIn("Remembered workspaces", page)
        self.assertIn("Forget only removes the browser shortcut", page)
        self.assertIn('id="workspace-context"', page)
        self.assertIn('id="workspace-name"', page)
        self.assertIn('id="workspace-switch"', page)
        self.assertIn('id="workspace-create"', page)
        self.assertIn('id="workspace-open"', page)
        self.assertNotIn('id="dataset-preparation-form"', page)
        self.assertIn('id="input-files-help"', page)
        self.assertNotIn('id="open-input-default"', page)
        self.assertNotIn('name="hucs"', page)
        self.assertNotIn('name="all_hucs"', page)
        self.assertNotIn('id="open-output"', page)
        self.assertIn("Select a job to view its results.", page)
        self.assertIn("Workspace jobs", page)
        self.assertIn('name="display_name"', page)
        self.assertIn("duplicate names are allowed", page)
        self.assertIn('name="base_job_id"', page)
        self.assertIn('name="candidate_job_id"', page)
        self.assertNotIn('name="base_prefix"', page)
        self.assertNotIn('name="candidate_prefix"', page)
        self.assertNotIn("Your jobs", page)
        self.assertIn('href="/ui/static/app.css?v=cam-1531-scrollable-reaches"', page)
        self.assertIn('src="/ui/static/app.js?v=cam-1527-reaches"', page)
        self.assertNotIn('id="json-file"', page)
        self.assertNotIn('id="json-url-form"', page)
        self.assertNotIn('id="json-url"', page)
        self.assertIn('id="flow-selection"', page)
        self.assertIn('id="flow-selection-content"', page)
        self.assertIn("Choose a streamflow source and period", page)
        self.assertIn("Every generated scenario includes every reach in this set.", page)
        self.assertIn('id="reach-set-builder"', page)
        self.assertIn('data-tab="compare">Compare</button>', page)
        self.assertLess(page.index('data-name="compare"'), page.index('id="compare-results"'))
        self.assertLess(page.index('id="compare-results"'), page.index('data-name="results"'))
        self.assertIn('id="scenario-output"', page)
        self.assertLess(page.index('id="reach-set-builder"'), page.index('id="flow-selection"'))
        self.assertLess(page.index('id="reach-set-builder"'), page.index('id="reach-tools"'))
        self.assertLess(page.index('id="reach-picker"'), page.index('id="scenario-output"'))
        self.assertLess(page.index('id="scenario-output"'), page.index('id="flow-selection"'))
        self.assertIn('role="tablist" aria-label="Reach set tools"', page)
        self.assertIn('aria-controls="timestep-panel"', page)
        self.assertIn('aria-controls="reach-picker"', page)
        self.assertIn('aria-controls="scenario-output"', page)
        self.assertIn('id="scenario-output" data-panel="reach-tools" data-name="scenarios" role="tabpanel" aria-labelledby="scenarios-tab">', page)
        self.assertIn('aria-controls="scenario-output" aria-selected="true" class="active">Scenarios</button>', page)
        self.assertLess(page.index('id="scenarios-tab"'), page.index('id="reach-map-tab"'))
        self.assertLess(page.index('id="reach-map-tab"'), page.index('id="reach-set-timesteps"'))
        self.assertIn('aria-controls="reach-picker" aria-selected="false" tabindex="-1">Map</button>', page)
        self.assertIn('aria-controls="timestep-panel" aria-selected="false" tabindex="-1">Streamflows (NWM)</button>', page)
        self.assertIn("Fit reach set", page)
        self.assertIn("Scenarios to submit", page)
        self.assertLess(page.index('id="scenario-output"'), page.index('id="reach-set-add-scenario"'))
        self.assertLess(page.index('id="reach-set-add-scenario"'), page.index('id="scenario-editor"'))
        self.assertIn('id="reach-set-timesteps"', page)
        self.assertNotIn('id="timestep-load"', page)
        self.assertNotIn('id="picker-flows"', page)
        self.assertIn("flow selection is separate from the map", page)
        self.assertIn('id="scenario-editor" role="region" aria-label="Scenarios to submit" tabindex="0"', page)
        self.assertIn('id="reach-set-editor" role="region" aria-label="Reach set" tabindex="0"', page)
        css = self.client.get("/ui/static/app.css?v=cam-1531-scrollable-reaches").text
        self.assertIn("[hidden] { display: none !important; }", css)
        self.assertIn("#scenario-editor { max-height: 60vh; overflow: auto; scrollbar-gutter: stable; }", css)
        self.assertIn("#reach-set-editor { max-height: 60vh; overflow: auto; scrollbar-gutter: stable; }", css)
        self.assertIn("s3://resource/&lt;resource_id&gt;/data/contents/&lt;empty_subprefix&gt;", page)
        self.assertIn("no existing files or folders", page)
        self.assertIn("Do not choose the resource root", page)
        self.assertNotIn('class="location-picker"', page)
        self.assertNotIn('id="jobs-clear"', page)
        self.assertNotIn('id="history-export"', page)
        self.assertNotIn('name="output_bucket"', page)
        self.assertNotIn('name="output_prefix"', page)
        self.assertNotIn('name="overwrite"', page)
        script = self.client.get("/ui/static/app.js").text
        self.assertNotIn("uploadScenarios", script)
        self.assertNotIn("loadScenariosFromUrl", script)
        self.assertNotIn("scenarios_url", script)
        self.assertIn('text: "Select streamflow"', script)
        self.assertIn("openFlowSelection(scenario)", script)
        self.assertIn('text: "View on map"', script)
        self.assertIn('text: "Add to reach set"', script)
        self.assertIn("function closeFlowSelection", script)
        self.assertNotIn("fimbox-locations:", script)
        self.assertIn("fimbox-workspace:v1:", script)
        self.assertIn("fimbox-workspaces:v1:", script)
        self.assertNotIn("/ui/preparations", script)
        self.assertIn("X-FIMBox-Workspace", script)
        self.assertIn('api("/workspaces/open"', script)
        self.assertIn('api("/workspaces"', script)
        self.assertNotIn("fimbox-history:", script)
        self.assertNotIn("/jobs/history/", script)
        self.assertIn("No work can be performed in anonymous mode.", script)
        submit_job = script.split("async function submitJob", 1)[1].split("// ---------- Results", 1)[0]
        compare_job = script.split("async function submitCompare", 1)[1].split("// ---------- Startup", 1)[0]
        self.assertIn("body.display_name = displayName", submit_job)
        self.assertIn("body.display_name = displayName", compare_job)
        self.assertIn('method: "PATCH"', script)
        self.assertIn("api(`/jobs/${jobId}`", script)
        self.assertIn('["Job ID", jobId]', script)
        self.assertNotIn("openOutput", script)
        self.assertNotIn("#open-output", script)
        self.assertIn('jobs.filter((job) => job.status === "success")', script)
        self.assertIn('api(`/jobs/${baseJobId}`)', script)
        self.assertIn('api(`/jobs/${candidateJobId}`)', script)
        self.assertIn("Choose two different workspace jobs to compare.", script)
        self.assertIn("base_bucket: baseJob.result.output_bucket", compare_job)
        self.assertIn("base_prefix: baseJob.result.output_prefix", compare_job)
        self.assertIn("candidate_bucket: candidateJob.result.output_bucket", compare_job)
        self.assertIn("candidate_prefix: candidateJob.result.output_prefix", compare_job)
        for field in ("output_bucket", "output_prefix", "overwrite"):
            self.assertNotIn(field, submit_job)
        self.assertNotIn("overwrite", compare_job)

    def test_ui_routes_are_not_in_openapi(self):
        self.assertFalse([path for path in main.app.openapi()["paths"] if path.startswith("/ui")])

    @unittest.skipUnless(shutil.which("node"), "Node.js is required for browser-state unit tests")
    def test_browser_state_regressions(self):
        scripts = [
            Path(__file__).with_name("ui_workspaces.test.cjs"),
            Path(__file__).with_name("ui_streamflow.test.cjs"),
        ]
        completed = subprocess.run(
            [shutil.which("node"), "--test", *(str(script) for script in scripts)],
            capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)

    def test_config_without_streaming(self):
        config = self.client.get("/ui/config").json()
        self.assertTrue(config["authentication_enabled"])
        self.assertFalse(config["streaming_enabled"])
        self.assertNotIn("dataset_preparation_enabled", config)
        self.assertEqual(config["execution_modes"], ["fimserv-2026", "fimserv-2024"])
        self.assertIsNone(config["input_dataset"])

    def test_config_with_streaming_dataset(self):
        self.patch_settings(stream_input_bucket="resource", stream_input_prefix="/abc/data/contents/",
                            stream_input_unsigned=True)
        config = self.client.get("/ui/config").json()
        self.assertTrue(config["streaming_enabled"])
        self.assertEqual(config["execution_modes"], [
            "streaming-2026", "streaming-2024", "fimserv-2026", "fimserv-2024",
        ])
        self.assertEqual(config["input_dataset"], {
            "bucket": "resource", "prefix": "abc/data/contents", "unsigned": True,
            "endpoint": main.settings.s3_endpoint_url,
        })
        self.assertEqual(config["source_endpoint"], "https://s3.us-east-2.amazonaws.com")

    def test_preparation_submission_routes_are_removed(self):
        self.assertEqual(self.client.post("/ui/preparations", json={}).status_code, 404)
        self.assertEqual(self.client.get("/ui/preparations/job-123").status_code, 404)
        self.assertEqual(self.client.post("/ui/preparations/job-123/cancel").status_code, 404)

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

    def test_noaa_source_bucket_uses_public_source_client_only_for_hand_fim_prefixes(self):
        source = Mock()
        source.list_objects_v2.return_value = {}
        source_client = self.patch(ui, "source_s3_client", return_value=source)
        for prefix in ("", "hand_fim_4_5_2_11/03020203"):
            response = self.client.get("/ui/objects", params={"bucket": ui.SOURCE_BUCKET, "prefix": prefix})
            self.assertEqual(response.status_code, 200)
        self.assertEqual(source_client.call_count, 2)
        self.s3.list_objects_v2.return_value = {}
        self.client.get("/ui/objects", params={"bucket": ui.SOURCE_BUCKET, "prefix": "private"})
        self.assertEqual(source_client.call_count, 2)
        self.assertEqual(self.client.get("/ui/config").json()["source_bucket"], ui.SOURCE_BUCKET)

    def test_unsigned_download_omits_response_header_overrides(self):
        source = Mock()
        source.meta.config.signature_version = ui.UNSIGNED
        source.generate_presigned_url.return_value = "https://noaa.test/f.csv"
        self.patch(ui, "source_s3_client", return_value=source)
        response = self.client.get("/ui/objects/download", params={
            "bucket": ui.SOURCE_BUCKET, "key": "hand_fim_4_5_2_11/hydrotable.csv"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(source.generate_presigned_url.call_args.kwargs["Params"],
                         {"Bucket": ui.SOURCE_BUCKET, "Key": "hand_fim_4_5_2_11/hydrotable.csv"})

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

    def test_raster_preview_returns_png_without_exposing_signed_url(self):
        self.s3.generate_presigned_url.return_value = "https://storage.test/input.tif?signature=secret"
        with patch.object(ui, "raster_preview", return_value=b"\x89PNG\r\n\x1a\n") as render:
            for suffix in (".tif", ".TIFF", ".cog"):
                response = self.client.get("/ui/objects/raster", params={"bucket": "fim", "key": "input/a" + suffix})
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.headers["content-type"], "image/png")
                self.assertEqual(response.headers["cache-control"], "no-store")
                self.assertNotIn(b"secret", response.content)
                self.assertEqual(render.call_args.args[0].path, self.s3.generate_presigned_url.return_value)
        self.s3.generate_presigned_url.assert_called_with(
            "get_object", Params={"Bucket": "fim", "Key": "input/a.cog"}, ExpiresIn=ui.DOWNLOAD_URL_SECONDS,
        )

    def test_raster_preview_validates_paths_auth_and_storage_errors(self):
        with patch.object(ui, "raster_preview") as render:
            for key in ("input/a.json", "input/../a.tif"):
                response = self.client.get("/ui/objects/raster", params={"bucket": "fim", "key": key})
                self.assertEqual(response.status_code, 400)
            self.s3.head_object.assert_not_called()
            for code, status in (("AccessDenied", 403), ("NoSuchKey", 404)):
                self.s3.head_object.side_effect = client_error(code)
                response = self.client.get("/ui/objects/raster", params={"bucket": "fim", "key": "input/a.tif"})
                self.assertEqual(response.status_code, status)
            render.assert_not_called()
            main.app.dependency_overrides.clear()
            response = self.client.get("/ui/objects/raster", params={"bucket": "fim", "key": "input/a.tif"})
            self.assertEqual(response.status_code, 401)

    def test_unsigned_raster_preview_is_scoped_to_input_dataset(self):
        self.patch_settings(hydroshare_client_id="", hydroshare_client_secret=None,
                            stream_input_bucket="resource", stream_input_prefix="abc/data",
                            stream_input_unsigned=True)
        self.anonymous.generate_presigned_url.return_value = "https://storage.test/public.tif"
        with patch.object(ui, "raster_preview", return_value=b"PNG") as render:
            response = self.client.get("/ui/objects/raster", params={"bucket": "resource", "key": "abc/data/a.tif"})
            self.assertEqual(response.status_code, 200)
            self.output_client.assert_not_called()
            for bucket, key in (("resource", "abc/database/a.tif"), ("other", "abc/data/a.tif")):
                response = self.client.get("/ui/objects/raster", params={"bucket": bucket, "key": key})
                self.assertEqual(response.status_code, 409)
            self.assertEqual(render.call_count, 1)

    def test_raster_preview_read_failure_is_sanitized(self):
        from fimbox_api.rasters import RasterReadError

        self.s3.generate_presigned_url.return_value = "https://storage.test/a.tif?secret"
        with patch.object(ui, "raster_preview", side_effect=RasterReadError("Could not read raster preview from storage.")):
            response = self.client.get("/ui/objects/raster", params={"bucket": "fim", "key": "input/a.tif"})
        self.assertEqual(response.status_code, 502)
        self.assertNotIn("secret", response.text)

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

    def test_scenario_url_loading_route_is_removed(self):
        response = self.client.get("/ui/scenarios/load", params={"url": "https://example.org/s.json"})
        self.assertEqual(response.status_code, 404)

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
