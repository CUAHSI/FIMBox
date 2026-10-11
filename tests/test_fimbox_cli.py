import json
import re
import tempfile
import time
import unittest
from http import HTTPStatus
from pathlib import Path
from unittest.mock import patch
from uuid import UUID, uuid4

import httpx
import click
from click.testing import CliRunner
from fastapi.testclient import TestClient

import fimbox_cli
from fimbox_api import auth, main, tasks
from fimbox_sdk import AuthenticatedClient
from fimbox_sdk.api.default import get_job_file_jobs_job_id_files_index_get, submit_hand_fim_jobs_hand_fim_post
from fimbox_sdk.models import JobSubmitted, PresignedOutput, Reach, Scenario

PAYLOAD = [{
    "ScenarioID": "test",
    "Reaches": [{"HUC": "03020201", "ReachID": "8780625", "Streamflow": 7}],
}]


class SdkCliTests(unittest.TestCase):
    def setUp(self):
        self.runner = CliRunner()
        patcher = patch.object(fimbox_cli, "__credential_store")
        self.credential_store = patcher.start().return_value
        self.credential_store.get_password.return_value = None
        self.addCleanup(patcher.stop)
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.body = Path(self.directory.name) / "scenario.json"
        self.body.write_text(json.dumps(PAYLOAD))
        self.job_id = str(uuid4())
        self.requests = []
        self.response = httpx.Response(202, json={
            "job_id": self.job_id, "status": "queued", "status_url": f"/jobs/{self.job_id}",
        })

        def respond(request):
            self.requests.append(request)
            return self.response

        factory = httpx.Client
        self.transport = httpx.MockTransport(respond)
        patcher = patch.object(
            httpx, "Client",
            side_effect=lambda **kwargs: factory(transport=self.transport, **kwargs),
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def invoke(self, *arguments, token="test-session", **kwargs):
        return self.runner.invoke(
            fimbox_cli.app, list(arguments),
            env={"FIMBOX_API_URL": "http://localhost:8000", "FIMBOX_SESSION_TOKEN": token},
            **kwargs,
        )

    def test_help_does_not_request_openapi_or_require_server(self):
        result = self.invoke("--help")
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("submit", result.output)
        self.assertIn("output", result.output)
        result = self.invoke("submit", "--help")
        self.assertIn("--output-bucket", result.output)
        self.assertIn("--body", result.output)
        self.assertEqual(self.requests, [])

    def test_cli_uses_generated_endpoint_and_models(self):
        result = self.invoke(
            "submit", "--body", str(self.body),
            "--output-bucket", "bucket", "--output-prefix", "data/output",
        )
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(json.loads(result.output)["job_id"], self.job_id)
        self.assertEqual(len(self.requests), 1, "SDK must not fetch OpenAPI at runtime")
        request = self.requests[0]
        self.assertEqual(request.url.path, "/jobs/hand-fim")
        self.assertEqual(request.method, "POST")
        self.assertEqual(dict(request.url.params), {"output_bucket": "bucket", "output_prefix": "data/output"})
        self.assertEqual(json.loads(request.content), PAYLOAD)
        self.assertEqual(request.headers["Authorization"], "Bearer test-session")

    def test_generated_sdk_returns_typed_results(self):
        scenario = Scenario(scenario_id="test", reaches=[Reach(huc="03020201", reach_id="8780625", streamflow=7)])
        with AuthenticatedClient(base_url="http://localhost:8000", token="sdk-session") as client:
            response = submit_hand_fim_jobs_hand_fim_post.sync_detailed(
                client=client, body=[scenario], output_bucket="bucket", output_prefix="output",
            )
        self.assertEqual(response.status_code, HTTPStatus.ACCEPTED)
        self.assertIsInstance(response.parsed, JobSubmitted)
        self.assertEqual(response.parsed.job_id, self.job_id)
        self.assertEqual(self.requests[0].headers["Authorization"], "Bearer sdk-session")
        self.response = httpx.Response(200, json={"url": "https://s3.test/signed", "expires_in": 60})
        with AuthenticatedClient(base_url="http://localhost:8000", token="sdk-session") as client:
            output = get_job_file_jobs_job_id_files_index_get.sync(
                UUID(self.job_id), 0, client=client,
            )
        self.assertIsInstance(output, PresignedOutput)
        self.assertEqual(output.expires_in, 60)

    def test_envelope_submission(self):
        envelope = {"payload": PAYLOAD, "output_bucket": "bucket", "output_prefix": ""}
        self.body.write_text(json.dumps(envelope))
        result = self.invoke("submit-envelope", "--body", str(self.body))
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(self.requests[-1].url.path, "/jobs")
        self.assertEqual(json.loads(self.requests[-1].content), envelope)

    def test_invalid_input_is_not_silently_discarded_or_submitted(self):
        for payload in ([], {}, [{**PAYLOAD[0], "extra": "not allowed"}], [{"ScenarioID": "missing"}]):
            self.body.write_text(json.dumps(payload))
            result = self.invoke(
                "submit", "--body", str(self.body), "--output-bucket", "bucket", "--output-prefix", "",
            )
            self.assertEqual(result.exit_code, 1, result.output)
        self.body.write_text("invalid json")
        result = self.invoke(
            "submit", "--body", str(self.body), "--output-bucket", "bucket", "--output-prefix", "",
        )
        self.assertEqual(result.exit_code, 1)
        result = self.invoke("submit")
        self.assertEqual(result.exit_code, 2)
        self.assertEqual(self.requests, [])

    def test_submit_defaults_to_local_output(self):
        result = self.invoke("submit", "--body", str(self.body))
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertNotIn("output_bucket", self.requests[-1].url.params)
        result = self.invoke("submit", "--body", str(self.body), "--output-prefix", "orphan")
        self.assertEqual(result.exit_code, 1)
        self.body.write_text(json.dumps({"payload": PAYLOAD}))
        result = self.invoke("submit-envelope", "--body", str(self.body))
        self.assertEqual(result.exit_code, 0, result.output)

    def test_max_procs_cli_and_envelope_use_generated_schema(self):
        result = self.invoke("submit", "--body", str(self.body), "--max-procs", "4")
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(self.requests[-1].url.params["max_procs"], "4")
        self.assertEqual(len(self.requests), 1)
        for value in ("0", "-1", "1.5"):
            result = self.invoke("submit", "--body", str(self.body), "--max-procs", value)
            self.assertEqual(result.exit_code, 2)
        self.assertEqual(len(self.requests), 1)
        self.body.write_text(json.dumps({"payload": PAYLOAD, "max_procs": 3}))
        result = self.invoke("submit-envelope", "--body", str(self.body))
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(json.loads(self.requests[-1].content)["max_procs"], 3)

    def test_path_commands_and_presigned_url(self):
        self.response = httpx.Response(200, json={"url": "https://s3.test/signed", "expires_in": 60})
        result = self.invoke("output", "--job-id", self.job_id, "--index", "2")
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(self.requests[-1].url.path, f"/jobs/{self.job_id}/files/2")
        self.assertEqual(json.loads(result.output)["expires_in"], 60)
        result = self.invoke("scenario-output", "--job-id", self.job_id, "--index", "0")
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(self.requests[-1].url.path, f"/jobs/{self.job_id}/outputs/0.tif")
        self.assertEqual(len(self.requests), 2, "CLI does not download or forward tokens to S3")

    def test_jobs_status_cancel_session_logout_and_health(self):
        responses = [
            ("jobs", [], "/jobs", 200, {"jobs": [{"job_id": self.job_id, "status": "started"}]}),
            ("status", ["--job-id", self.job_id], f"/jobs/{self.job_id}", 200,
             {"job_id": self.job_id, "status": "queued", "result": None, "error": None}),
            ("cancel", ["--job-id", self.job_id], f"/jobs/{self.job_id}", 204, None),
            ("session", [], "/auth/session", 200, {"user_id": "alice"}),
            ("logout", [], "/auth/logout", 204, None),
            ("health", [], "/health", 200, {"status": "ok"}),
        ]
        for name, args, path, code, payload in responses:
            with self.subTest(name=name):
                self.response = httpx.Response(code, json=payload) if payload is not None else httpx.Response(code)
                result = self.invoke(name, *args)
                self.assertEqual(result.exit_code, 0, result.output)
                self.assertEqual(self.requests[-1].url.path, path)
                if name == "jobs":
                    self.assertEqual(json.loads(result.output), payload)
        self.assertNotIn("authorization", self.requests[-1].headers)

    def test_hidden_session_prompt_and_custom_url(self):
        self.response = httpx.Response(200, json={"jobs": []})
        result = self.invoke("--api-url", "https://fimbox.test", "jobs", token="", input="hidden-secret\n")
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertNotIn("hidden-secret", result.output)
        self.assertIn("https://fimbox.test/auth/login", result.output)
        self.assertIn("copy the access_token", result.output)
        self.assertEqual(self.requests[-1].headers["Authorization"], "Bearer hidden-secret")
        self.assertEqual(self.requests[-1].url.host, "fimbox.test")

    def test_default_session_prompt_includes_local_login_link(self):
        self.response = httpx.Response(200, json={"jobs": []})
        result = self.invoke("jobs", token="", input="hidden-secret\n")
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("http://localhost:8000/auth/login", result.output)
        self.assertNotIn("hidden-secret", result.output)
        self.credential_store.set_password.assert_called_once_with(
            "fimbox", "http://localhost:8000", "hidden-secret",
        )

    def test_saved_token_is_reused_and_scoped_to_api_url(self):
        self.response = httpx.Response(200, json={"jobs": []})
        self.credential_store.get_password.return_value = "saved-secret"
        result = self.invoke("--api-url", "https://other.test/", "jobs", token="")
        self.assertEqual(result.exit_code, 0, result.output)
        self.credential_store.get_password.assert_called_with("fimbox", "https://other.test")
        self.assertEqual(self.requests[-1].headers["Authorization"], "Bearer saved-secret")
        self.assertNotIn("saved-secret", result.output)
        self.assertNotIn("session token", result.output)
        self.credential_store.set_password.assert_not_called()

    def test_explicit_token_takes_precedence_over_saved_token(self):
        self.response = httpx.Response(200, json={"jobs": []})
        self.credential_store.get_password.return_value = "saved-secret"
        result = self.invoke("jobs")
        self.assertEqual(result.exit_code, 0, result.output)
        self.credential_store.get_password.assert_not_called()
        self.assertEqual(self.requests[-1].headers["Authorization"], "Bearer test-session")

    def test_invalid_saved_token_is_cleared_without_retry(self):
        self.credential_store.get_password.return_value = "expired"
        self.response = httpx.Response(401, json={"detail": "Invalid or expired session"})
        result = self.invoke("jobs", token="")
        self.assertEqual(result.exit_code, 1)
        self.credential_store.delete_password.assert_called_once_with("fimbox", "http://localhost:8000")
        self.assertEqual(len(self.requests), 1)

    def test_logout_clears_saved_token_after_revocation(self):
        self.credential_store.get_password.return_value = "saved"
        self.response = httpx.Response(204)
        result = self.invoke("logout", token="")
        self.assertEqual(result.exit_code, 0, result.output)
        self.credential_store.delete_password.assert_called_once_with("fimbox", "http://localhost:8000")

    def test_failed_prompted_request_does_not_save_token(self):
        self.response = httpx.Response(401, json={"detail": "Invalid session"})
        result = self.invoke("jobs", token="", input="bad-secret\n")
        self.assertEqual(result.exit_code, 1)
        self.credential_store.set_password.assert_not_called()

    def test_keychain_failure_is_explicit_and_never_leaks_token(self):
        from keyring.errors import KeyringError

        self.credential_store.get_password.side_effect = KeyringError("backend details")
        result = self.invoke("jobs", token="")
        self.assertEqual(result.exit_code, 1)
        self.assertIn("Cannot read", result.output)
        self.assertEqual(self.requests, [])
        self.credential_store.get_password.side_effect = None
        self.credential_store.set_password.side_effect = KeyringError("secret-details")
        self.response = httpx.Response(200, json={"jobs": []})
        result = self.invoke("jobs", token="", input="hidden-secret\n")
        self.assertEqual(result.exit_code, 1)
        self.assertIn("request succeeded", result.output)
        self.assertNotIn("secret-details", result.output)
        self.assertNotIn("hidden-secret", result.output)


    def test_http_errors_and_redirects_exit_nonzero_without_retry(self):
        for status in (401, 403, 409, 503):
            self.response = httpx.Response(status, json={"detail": "request rejected"})
            result = self.invoke("jobs")
            self.assertEqual(result.exit_code, 1, result.output)
            self.assertIn(f"HTTP {status}", result.output)
        self.response = httpx.Response(302, headers={"Location": "https://outside.test"})
        result = self.invoke("jobs")
        self.assertEqual(result.exit_code, 1)
        self.assertEqual(len(self.requests), 5)
        self.assertTrue(all(request.url.host == "localhost" for request in self.requests))

    def test_validation_error_and_unexpected_contract(self):
        self.response = httpx.Response(422, json={"detail": [{"loc": ["body"], "msg": "Invalid", "type": "value_error"}]})
        result = self.invoke("output", "--job-id", self.job_id, "--index", "0")
        self.assertEqual(result.exit_code, 1)
        self.assertIn("HTTP 422", result.output)
        self.response = httpx.Response(200, json={"not": "a presigned output"})
        result = self.invoke("output", "--job-id", self.job_id, "--index", "0")
        self.assertEqual(result.exit_code, 1)
        self.assertIn("SDK contract", result.output)

    def test_connection_and_option_errors(self):
        with patch.object(httpx, "Client", side_effect=httpx.ConnectError("connection refused")):
            result = self.invoke("health")
        self.assertEqual(result.exit_code, 1)
        self.assertIn("Cannot reach the API", result.output)
        result = self.invoke("output", "--job-id", "invalid", "--index", "-1")
        self.assertEqual(result.exit_code, 2)
        result = self.invoke("--api-url", "http://user:password@localhost", "health")
        self.assertEqual(result.exit_code, 1)
        self.assertNotIn("password", result.output)


class SdkIntegrationTests(unittest.TestCase):
    def setUp(self):
        main.app.dependency_overrides[auth.get_authenticated_session] = lambda: auth.UserSession(
            user_id="alice", access_token="provider", scope="read write", expires_at=time.time() + 60,
        )
        self.addCleanup(main.app.dependency_overrides.clear)

    def test_generated_client_enqueues_through_fastapi(self):
        with TestClient(main.app) as http_client, patch.object(main, "create_job_credentials"), patch.object(
            main, "track_user_job",
        ), patch.object(tasks.hand_fim, "apply_async") as enqueue:
            client = AuthenticatedClient(base_url="http://testserver", token="session")
            client.set_httpx_client(http_client)
            response = submit_hand_fim_jobs_hand_fim_post.sync_detailed(
                client=client, body=[Scenario.from_dict(PAYLOAD[0])],
                output_bucket="bucket", output_prefix="output",
            )
            self.assertEqual(response.status_code, 202)
            self.assertIsInstance(response.parsed, JobSubmitted)
            enqueue.assert_called_once_with(
                args=[response.parsed.job_id, PAYLOAD, "bucket", "output", None],
                task_id=response.parsed.job_id, retry=False,
            )

    def test_cli_enqueues_via_generated_client(self):
        with tempfile.TemporaryDirectory() as directory:
            body = Path(directory) / "scenario.json"
            body.write_text(json.dumps(PAYLOAD))
            with TestClient(main.app) as client, patch.object(
                AuthenticatedClient, "get_httpx_client", return_value=client,
            ), patch.object(main, "create_job_credentials"), patch.object(main, "track_user_job"), patch.object(
                tasks.hand_fim, "apply_async",
            ) as enqueue:
                result = CliRunner().invoke(fimbox_cli.app, [
                    "submit", "--body", str(body), "--output-bucket", "bucket", "--output-prefix", "output",
                ], env={"FIMBOX_SESSION_TOKEN": "session"})
            self.assertEqual(result.exit_code, 0, result.output)
            self.assertEqual(enqueue.call_args.kwargs["args"][1], PAYLOAD)

    def test_captured_spec_matches_source_and_covers_all_endpoints(self):
        root = Path(__file__).resolve().parents[1]
        spec = json.loads((root / "sdk" / "openapi.json").read_text())
        self.assertEqual(spec, main.app.openapi(), "Regenerate the SDK after API schema changes")
        for path_item in spec["paths"].values():
            for method, operation in path_item.items():
                if method not in {"get", "post", "delete", "put", "patch"}:
                    continue
                group = "authentication" if "authentication" in operation.get("tags", []) else "default"
                operation_name = re.sub("_+", "_", operation["operationId"])
                module = root / "src" / "fimbox_sdk" / "api" / group / f"{operation_name}.py"
                self.assertTrue(module.is_file(), module)

    def test_async_sdk_is_generated_and_usable(self):
        import asyncio

        async def run():
            async def respond(request):
                self.assertEqual(request.headers["Authorization"], "Bearer session")
                return httpx.Response(202, json={"job_id": "test", "status": "queued", "status_url": "/jobs/test"})

            client = AuthenticatedClient(
                base_url="http://testserver", token="session",
                httpx_args={"transport": httpx.MockTransport(respond)},
            )
            async with client:
                return await submit_hand_fim_jobs_hand_fim_post.asyncio(
                    client=client, body=[Scenario.from_dict(PAYLOAD[0])],
                    output_bucket="bucket", output_prefix="output",
                )

        result = asyncio.run(run())
        self.assertIsInstance(result, JobSubmitted)
        self.assertEqual(result.status, "queued")


class CredentialBackendTests(unittest.TestCase):
    def test_secure_os_backend_is_selected(self):
        backend = type("SecureBackend", (), {"__module__": "keyring.backends.macOS"})()
        with patch.object(fimbox_cli.keyring, "get_keyring", return_value=backend):
            self.assertIs(fimbox_cli.__dict__["__credential_store"](), backend)

    def test_plaintext_or_unavailable_backend_is_rejected(self):
        from keyring.backends.fail import Keyring

        with patch.object(fimbox_cli.keyring, "get_keyring", return_value=Keyring()):
            with self.assertRaisesRegex(click.ClickException, "No supported OS credential store"):
                fimbox_cli.__dict__["__credential_store"]()


if __name__ == "__main__":
    unittest.main()
