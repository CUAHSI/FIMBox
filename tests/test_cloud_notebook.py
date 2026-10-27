import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

import httpx


NOTEBOOK = Path(__file__).resolve().parents[1] / "notebooks" / "cloud_hand_fim.ipynb"


class CloudNotebookTests(unittest.TestCase):
    def setUp(self):
        self.notebook = json.loads(NOTEBOOK.read_text())
        self.sources = {
            cell["id"]: "".join(cell["source"])
            for cell in self.notebook["cells"] if cell["cell_type"] == "code"
        }

    def test_code_compiles(self):
        for cell in self.notebook["cells"]:
            if cell["cell_type"] == "code":
                source = "".join(line for line in cell["source"] if not line.startswith("%"))
                compile(source, cell["id"], "exec")

    def test_map_paths_match_execution_mode_without_a_job_result(self):
        from types import SimpleNamespace

        from fimbox_sdk.models.hand_fim_job_request_execution_mode import HandFimJobRequestExecutionMode

        for mode, suffix in (
            (HandFimJobRequestExecutionMode.LEGACY, ".cog"),
            (HandFimJobRequestExecutionMode.STREAMING, "/manifest.json"),
        ):
            for prefix in ("demo/", ""):
                with self.subTest(mode=mode, prefix=prefix):
                    helper = Mock()
                    namespace = {
                        "EXECUTION_MODE": mode,
                        "HandFimJobRequestExecutionMode": HandFimJobRequestExecutionMode,
                        "OUTPUT_BUCKET": "bucket", "OUTPUT_PREFIX": prefix,
                        "scenarios": [SimpleNamespace(scenario_id="single-reach-scenario")],
                        "api_client": Mock(), "display": Mock(),
                    }
                    with patch("importlib.reload", return_value=helper):
                        exec(self.sources["map"], namespace)
                    root = "s3://bucket/demo" if prefix else "s3://bucket"
                    helper.output_map.assert_called_once_with(
                        namespace["api_client"], [f"{root}/single-reach-scenario{suffix}"],
                    )
                    namespace["display"].assert_called_once_with(helper.output_map.return_value)

    def test_scenarios_json_loads_typed_sample_without_submitting(self):
        from fimbox_sdk.models import Scenario

        root = NOTEBOOK.parents[1]
        namespace = {"ROOT": root, "Scenario": Scenario}
        with redirect_stdout(io.StringIO()) as output:
            exec(self.sources["scenarios-json"], namespace)
        payload = json.loads((root / "test-data" / "single_reach.json").read_text())
        self.assertEqual([scenario.to_dict() for scenario in namespace["scenarios"]], payload)
        self.assertIn("Loaded 1 scenario(s)", output.getvalue())
        self.assertNotIn("scenarios =", self.sources["submission"])

    def test_setup_loads_editable_install_in_running_interpreter(self):
        source = self.sources["dependencies"]
        refresh_and_imports = source.split("# Load editable-install", 1)[1]
        code = (
            "import importlib.util, site, sysconfig\n"
            "assert importlib.util.find_spec('fimbox_sdk') is None\n"
            "# Load editable-install" + refresh_and_imports
        )
        with tempfile.TemporaryDirectory() as directory:
            completed = subprocess.run(
                [sys.executable, "-S", "-c", code],
                cwd=directory, env={key: value for key, value in os.environ.items() if key != "PYTHONPATH"},
                capture_output=True, text=True,
            )
        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_sdk_authentication_submission_and_monitoring(self):
        from fimbox_sdk import AuthenticatedClient, Client
        from fimbox_sdk.models import StoredHandFimResult

        job_id = "4ec24445-d50f-434b-bce6-c1364762bfac"
        requests = []
        states = iter(["queued", "success"])

        def respond(request):
            requests.append(request)
            path = request.url.path
            if path == "/auth/config":
                self.assertNotIn("authorization", request.headers)
                return httpx.Response(200, json={
                    "authentication_enabled": True, "require_hydroshare_output": True,
                })
            if path == "/auth/device/start":
                return httpx.Response(200, json={
                    "device_code": "b" * 43 + "." + "s" * 43,
                    "verification_uri": "https://api.example.org/auth/device/verify?code=" + "b" * 43,
                    "expires_in": 600, "interval": 5,
                })
            if path == "/auth/device/poll":
                self.assertNotIn("authorization", request.headers)
                return httpx.Response(200, json={
                    "status": "authorized",
                    "token": {"access_token": "notebook-test-token", "token_type": "bearer",
                              "expires_in": 600, "user_id": "test", "scope": "read"},
                })
            self.assertEqual(request.headers["authorization"], "Bearer notebook-test-token")
            if path == "/auth/session":
                return httpx.Response(200, json={"user_id": "test", "expires_at": 9999999999})
            if path == "/jobs/hand-fim":
                body = json.loads(request.content)
                self.assertEqual(body["scenarios"][0]["Reaches"][0]["HUC"], "03020201")
                self.assertEqual(body["max_procs"], 1)
                return httpx.Response(202, json={"job_id": job_id, "status": "queued", "status_url": f"/jobs/{job_id}"})
            if path == f"/jobs/{job_id}":
                state = next(states)
                return httpx.Response(200, json={
                    "job_id": job_id, "status": state,
                    "result": {
                        "outputs": [],
                        "files": [{"path": f"{job_id}-0.cog", "size_bytes": 123}],
                    } if state == "success" else None,
                })
            return httpx.Response(401, json={"detail": "sensitive-server-detail"})

        namespace = {
            "API_URL": "https://api.example.org", "os": os,
            "Path": Path, "RESUME_JOB_ID": "", "OUTPUT_BUCKET": "bucket",
            "OUTPUT_PREFIX": "demo", "MAX_PROCS": 1, "WAIT_SECONDS": 60, "POLL_SECONDS": 0,
        }
        imports = self.sources["dependencies"].split("from fimbox_sdk import", 1)[1]
        exec("from fimbox_sdk import" + imports, namespace)
        transport = httpx.MockTransport(respond)
        public_http = httpx.Client(transport=transport, base_url=namespace["API_URL"])
        self.addCleanup(public_http.close)
        with httpx.Client(
            transport=transport, base_url=namespace["API_URL"],
            headers={"Authorization": "Bearer notebook-test-token"},
        ) as authenticated_http, patch.object(
            Client, "get_httpx_client", return_value=public_http,
        ), patch.object(
            AuthenticatedClient, "get_httpx_client", return_value=authenticated_http,
        ), patch.dict("os.environ", {"FIMBOX_SESSION_TOKEN": ""}), patch("IPython.display.display"):
            exec(self.sources["authentication"], namespace)
            self.assertEqual(namespace["session_token"], "notebook-test-token")
            exec(self.sources["connect"], namespace)
            output = io.StringIO()
            with redirect_stdout(output):
                exec(self.sources["scenarios-inline"], namespace)
                exec(self.sources["submission"], namespace)
            self.assertEqual(sum(request.url.path == "/jobs/hand-fim" for request in requests), 1)
            with redirect_stdout(output):
                exec(self.sources["monitoring"], namespace)
            self.assertIsInstance(namespace["final_result"], StoredHandFimResult)
            self.assertNotIn(job_id, output.getvalue())
            self.assertIn("0.cog", output.getvalue())
            operation = Mock()
            operation.sync_detailed.return_value = Mock(status_code=401)
            with self.assertRaisesRegex(RuntimeError, "HTTP 401"):
                namespace["call_api"](operation, client=namespace["api_client"])
            self.assertTrue(all("notebook-test-token" not in str(request.url) for request in requests))

    def test_no_saved_outputs_or_credential_widgets(self):
        for cell in self.notebook["cells"]:
            if cell["cell_type"] == "code":
                self.assertEqual(cell["outputs"], [])
                self.assertIsNone(cell["execution_count"])
        self.assertNotIn("token_input", self.sources["authentication"])

    def test_environment_token_bypasses_browser_login(self):
        from fimbox_sdk import Client
        namespace = {"API_URL": "https://api.example.org", "os": os}
        imports = self.sources["dependencies"].split("from fimbox_sdk import", 1)[1]
        exec("from fimbox_sdk import" + imports, namespace)
        transport = httpx.MockTransport(lambda request: httpx.Response(200, json={
            "authentication_enabled": True, "require_hydroshare_output": True,
        }))
        http = httpx.Client(transport=transport, base_url=namespace["API_URL"])
        self.addCleanup(http.close)
        with patch.object(
            Client, "get_httpx_client", return_value=http,
        ), patch.dict("os.environ", {"FIMBOX_SESSION_TOKEN": "saved-session"}), patch(
            "fimbox_login.begin_login",
        ) as begin:
            exec(self.sources["authentication"], namespace)
        begin.assert_not_called()
        self.assertEqual(namespace["session_token"], "saved-session")

if __name__ == "__main__":
    unittest.main()
