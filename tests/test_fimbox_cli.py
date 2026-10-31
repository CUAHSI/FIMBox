import json
import re
import tempfile
import time
import unittest
from http import HTTPStatus
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

import httpx
import click
from click.testing import CliRunner
from fastapi.testclient import TestClient
from pydantic import SecretStr

import fimbox_cli
from fimbox_api import auth, main, tasks
from fimbox_sdk import AuthenticatedClient, Client
from fimbox_sdk.api.default import submit_hand_fim_jobs_hand_fim_post
from fimbox_sdk.models import AuthenticationConfig, HandFimJobRequest, HTTPValidationError, JobSubmitted, Reach, Scenario

PAYLOAD = [{
    "ScenarioID": "test",
    "Reaches": [{"HUC": "03020201", "ReachID": "8780625", "Streamflow": 7}],
}]


class SdkCliTests(unittest.TestCase):
    def setUp(self):
        self.runner = CliRunner()
        self.real_auth_mode = fimbox_cli.__dict__["__authentication_enabled"]
        patcher = patch.object(fimbox_cli, "__authentication_enabled", return_value=True)
        self.auth_mode = patcher.start()
        self.addCleanup(patcher.stop)
        self.real_api_config = fimbox_cli.__dict__["__api_config"]
        patcher = patch.object(fimbox_cli, "__api_config", return_value=AuthenticationConfig(
            authentication_enabled=True, require_hydroshare_output=False,
        ))
        self.api_config = patcher.start()
        self.addCleanup(patcher.stop)
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
        self.login_responses = {}
        self.response = httpx.Response(202, json={
            "job_id": self.job_id, "status": "queued", "status_url": f"/jobs/{self.job_id}",
        })

        def respond(request):
            self.requests.append(request)
            if request.url.path in self.login_responses:
                return self.login_responses[request.url.path].pop(0)
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
        self.assertNotIn("submit-envelope", result.output)
        self.assertNotIn("scenario-output", result.output)
        result = self.invoke("submit-envelope", "--help")
        self.assertEqual(result.exit_code, 2)
        self.assertIn("No such command", result.output)
        result = self.invoke("submit", "--help")
        self.assertIn("--output-bucket", result.output)
        self.assertIn("--overwrite", result.output)
        self.assertIn("--body", result.output)
        self.assertEqual(self.requests, [])

    def test_cli_uses_generated_endpoint_and_models(self):
        result = self.invoke(
            "submit", "--body", str(self.body),
            "--output-bucket", "bucket", "--output-prefix", "data/output", "--overwrite",
        )
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(json.loads(result.output)["job_id"], self.job_id)
        self.assertEqual(len(self.requests), 1, "SDK must not fetch OpenAPI at runtime")
        request = self.requests[0]
        self.assertEqual(request.url.path, "/jobs/hand-fim")
        self.assertEqual(request.method, "POST")
        self.assertEqual(dict(request.url.params), {})
        self.assertEqual(json.loads(request.content), {
            "scenarios": PAYLOAD,
            "output_bucket": "bucket",
            "output_prefix": "data/output",
            "max_procs": None,
            "overwrite": True,
        })
        self.assertEqual(request.headers["Authorization"], "Bearer test-session")

    def test_generated_sdk_returns_typed_results(self):
        scenario = Scenario(scenario_id="test", reaches=[Reach(huc="03020201", reach_id="8780625", streamflow=7)])
        with AuthenticatedClient(base_url="http://localhost:8000", token="sdk-session") as client:
            response = submit_hand_fim_jobs_hand_fim_post.sync_detailed(
                client=client,
                body=HandFimJobRequest(
                    scenarios=[scenario], output_bucket="bucket", output_prefix="output",
                ),
            )
        self.assertEqual(response.status_code, HTTPStatus.ACCEPTED)
        self.assertIsInstance(response.parsed, JobSubmitted)
        self.assertEqual(response.parsed.job_id, self.job_id)
        self.assertEqual(self.requests[0].headers["Authorization"], "Bearer sdk-session")

    def test_updatedlegacy_mode_is_sent_through_sdk(self):
        result = self.invoke(
            "submit", "--body", str(self.body), "--execution-mode", "updatedlegacy",
        )
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(json.loads(self.requests[0].content)["execution_mode"], "updatedlegacy")

    def test_updatedstreaming_mode_is_sent_through_sdk(self):
        result = self.invoke(
            "submit", "--body", str(self.body), "--execution-mode", "updatedstreaming",
            "--output-bucket", "bucket", "--output-prefix", "runs",
        )
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(json.loads(self.requests[0].content)["execution_mode"], "updatedstreaming")

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
        self.assertEqual(result.exit_code, 1)
        self.assertIn("Input method", result.output)
        self.assertEqual(self.requests, [])

    def test_submit_defaults_to_local_output(self):
        result = self.invoke("submit", "--body", str(self.body))
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(dict(self.requests[-1].url.params), {})
        self.assertEqual(json.loads(self.requests[-1].content), {
            "scenarios": PAYLOAD,
            "output_bucket": None,
            "output_prefix": "",
            "max_procs": None,
            "overwrite": False,
        })
        result = self.invoke("submit", "--body", str(self.body), "--output-prefix", "orphan")
        self.assertEqual(result.exit_code, 1)

    def test_submit_interactive_manual_inputs_build_a_scenario(self):
        result = self.invoke(
            "submit",
            input="manual\nmanual-test\n03020201\n8780625, 8780626\n1.5, 2.5\n3\n",
        )
        self.assertEqual(result.exit_code, 0, result.output)
        request = json.loads(self.requests[-1].content)
        self.assertEqual(request["scenarios"], [{
            "ScenarioID": "manual-test",
            "Reaches": [
                {"HUC": "03020201", "ReachID": "8780625", "Streamflow": 1.5},
                {"HUC": "03020201", "ReachID": "8780626", "Streamflow": 2.5},
            ],
        }])
        self.assertEqual(request["max_procs"], 3)

    def test_submit_interactive_json_inputs_load_file(self):
        result = self.invoke("submit", input=f"json\n{self.body}\n1\n")
        self.assertEqual(result.exit_code, 0, result.output)
        request = json.loads(self.requests[-1].content)
        self.assertEqual(request["scenarios"], PAYLOAD)
        self.assertEqual(request["max_procs"], 1)

    def test_interactive_required_output_prompts_and_discovers_config_once(self):
        self.login_responses["/auth/config"] = [httpx.Response(200, json={
            "authentication_enabled": True, "require_hydroshare_output": True,
        })]
        with patch.object(fimbox_cli, "__api_config", wraps=self.real_api_config), patch.object(
            fimbox_cli, "__authentication_enabled", wraps=self.real_auth_mode,
        ):
            result = self.invoke(
                "submit", input="\nasdf\n\n\n\n\nbucket\nruns/test\ny\n",
            )
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("HydroShare output bucket (required by the API)", result.output)
        self.assertEqual([request.url.path for request in self.requests], [
            "/auth/config", "/jobs/hand-fim",
        ])
        request = json.loads(self.requests[-1].content)
        self.assertEqual(request["output_bucket"], "bucket")
        self.assertEqual(request["output_prefix"], "runs/test")
        self.assertTrue(request["overwrite"])

    def test_interactive_output_flags_skip_prompts(self):
        self.api_config.return_value.require_hydroshare_output = True
        for flag, expected in (("--overwrite", True), ("--no-overwrite", False)):
            with self.subTest(flag=flag):
                result = self.invoke(
                    "submit", "--output-bucket", "bucket", "--output-prefix", "",
                    "--max-procs", "4", flag, input=f"json\n{self.body}\n",
                )
                self.assertEqual(result.exit_code, 0, result.output)
                self.assertNotIn("HydroShare output bucket", result.output)
                self.assertNotIn("Output prefix", result.output)
                self.assertNotIn("Replace existing", result.output)
                self.assertNotIn("Max concurrent", result.output)
                request = json.loads(self.requests[-1].content)
                self.assertEqual(request["output_bucket"], "bucket")
                self.assertEqual(request["output_prefix"], "")
                self.assertEqual(request["max_procs"], 4)
                self.assertEqual(request["overwrite"], expected)

    def test_interactive_optional_bucket_prompts_for_remaining_output_options(self):
        result = self.invoke(
            "submit", "--output-bucket", "bucket",
            input=f"json\n{self.body}\n1\n\nn\n",
        )
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("Output prefix", result.output)
        self.assertIn("Replace existing", result.output)
        request = json.loads(self.requests[-1].content)
        self.assertEqual(request["output_prefix"], "")
        self.assertFalse(request["overwrite"])

    def test_interactive_local_output_does_not_prompt_for_storage(self):
        self.api_config.return_value = AuthenticationConfig(
            authentication_enabled=False, require_hydroshare_output=False,
        )
        self.auth_mode.side_effect = self.real_auth_mode
        result = self.invoke("submit", input=f"json\n{self.body}\n1\n", token="")
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertNotIn("output bucket", result.output)
        self.assertNotIn("Output prefix", result.output)
        self.assertNotIn("Replace existing", result.output)
        self.assertNotIn("authorization", self.requests[-1].headers)
        self.credential_store.get_password.assert_not_called()

    def test_required_bucket_abort_does_not_submit(self):
        self.api_config.return_value.require_hydroshare_output = True
        result = self.invoke("submit", input=f"json\n{self.body}\n1\n")
        self.assertEqual(result.exit_code, 1, result.output)
        self.assertIn("HydroShare output bucket", result.output)
        self.assertEqual(self.requests, [])

    def test_required_bucket_prompt_accepts_an_explicit_prefix(self):
        self.api_config.return_value.require_hydroshare_output = True
        result = self.invoke(
            "submit", "--output-prefix", "runs", "--no-overwrite",
            input=f"json\n{self.body}\n1\nbucket\n",
        )
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertNotIn("Output prefix", result.output)
        request = json.loads(self.requests[-1].content)
        self.assertEqual(request["output_bucket"], "bucket")
        self.assertEqual(request["output_prefix"], "runs")

    def test_interactive_config_failure_does_not_submit(self):
        self.api_config.side_effect = click.ClickException("Cannot determine API configuration.")
        result = self.invoke("submit", input=f"json\n{self.body}\n1\n")
        self.assertEqual(result.exit_code, 1, result.output)
        self.assertIn("Cannot determine API configuration", result.output)
        self.assertEqual(self.requests, [])

    def test_submit_interactive_rejects_mismatched_reach_and_flow_counts(self):
        result = self.invoke(
            "submit", input="manual\nscenario1\n03020201\n8780625,8780626\n10\n",
        )
        self.assertEqual(result.exit_code, 1, result.output)
        self.assertIn("must match", result.output)
        self.assertEqual(self.requests, [])

    def test_max_procs_cli_uses_generated_schema(self):
        result = self.invoke("submit", "--body", str(self.body), "--max-procs", "4")
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(json.loads(self.requests[-1].content)["max_procs"], 4)
        self.assertEqual(len(self.requests), 1)
        for value in ("0", "-1", "1.5"):
            result = self.invoke("submit", "--body", str(self.body), "--max-procs", value)
            self.assertEqual(result.exit_code, 2)
        self.assertEqual(len(self.requests), 1)

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

    def configure_browser_login(self, api_url="http://localhost:8000"):
        self.login_responses = {
            "/auth/device/start": [httpx.Response(200, json={
                "device_code": "b" * 43 + "." + "s" * 43,
                "verification_uri": api_url + "/auth/device/verify?code=" + "b" * 43,
                "expires_in": 600, "interval": 5,
            })],
            "/auth/device/poll": [httpx.Response(200, json={
                "status": "authorized",
                "token": {
                    "access_token": "hidden-secret", "token_type": "bearer",
                    "expires_in": 600, "user_id": "alice", "scope": "read",
                },
            })],
        }

    def test_browser_login_polls_custom_url_without_prompting(self):
        self.configure_browser_login("https://fimbox.test")
        self.login_responses["/auth/device/poll"].insert(
            0, httpx.Response(200, json={"status": "pending"}),
        )
        self.response = httpx.Response(200, json={"jobs": []})
        with patch("fimbox_login.time.sleep") as sleep, patch("click.prompt") as prompt:
            result = self.invoke("--api-url", "https://fimbox.test", "jobs", token="")
        prompt.assert_not_called()
        sleep.assert_called_once_with(5)
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertNotIn("hidden-secret", result.output)
        self.assertNotIn("s" * 43, result.output)
        self.assertIn("https://fimbox.test/auth/device/verify?code=" + "b" * 43, result.output)
        self.assertIn("Waiting for browser approval", result.output)
        self.assertEqual([request.url.path for request in self.requests], [
            "/auth/device/start", "/auth/device/poll", "/auth/device/poll", "/jobs",
        ])
        for request in self.requests[:-1]:
            self.assertNotIn("authorization", request.headers)
            self.assertEqual(request.url.host, "fimbox.test")
        self.assertEqual(json.loads(self.requests[1].content), {
            "device_code": "b" * 43 + "." + "s" * 43,
        })
        self.credential_store.set_password.assert_called_once_with(
            "fimbox", "https://fimbox.test", "hidden-secret",
        )
        self.assertEqual(self.requests[-1].headers["Authorization"], "Bearer hidden-secret")
        self.assertEqual(self.requests[-1].url.host, "fimbox.test")

    def test_browser_login_on_loopback_saves_token(self):
        self.configure_browser_login()
        self.response = httpx.Response(200, json={"jobs": []})
        result = self.invoke("jobs", token="")
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("http://localhost:8000/auth/device/verify", result.output)
        self.assertNotIn("hidden-secret", result.output)
        self.credential_store.set_password.assert_called_once_with(
            "fimbox", "http://localhost:8000", "hidden-secret",
        )

    def test_browser_login_failures_stop_before_operation_or_token_storage(self):
        for status, message in (
            ("denied", "denied"), ("failed", "authentication failed"),
        ):
            with self.subTest(status=status):
                self.configure_browser_login()
                self.requests.clear()
                self.login_responses["/auth/device/poll"] = [
                    httpx.Response(200, json={"status": status}),
                ]
                result = self.invoke("jobs", token="")
                self.assertEqual(result.exit_code, 1, result.output)
                self.assertIn(message, result.output)
                self.assertEqual(len(self.requests), 2)
                self.credential_store.set_password.assert_not_called()

    def test_browser_login_timeout_is_a_cli_error(self):
        self.configure_browser_login()
        with patch("fimbox_login.BrowserLogin.wait", side_effect=TimeoutError("Browser login timed out")):
            result = self.invoke("jobs", token="")
        self.assertEqual(result.exit_code, 1, result.output)
        self.assertIn("Browser login timed out", result.output)
        self.assertEqual(len(self.requests), 1)
        self.credential_store.set_password.assert_not_called()

    def test_browser_login_start_failure_is_explicit(self):
        self.configure_browser_login()
        self.login_responses["/auth/device/start"] = [
            httpx.Response(503, json={"detail": "private-server-details"}),
        ]
        result = self.invoke("jobs", token="")
        self.assertEqual(result.exit_code, 1, result.output)
        self.assertIn("HTTP 503", result.output)
        self.assertNotIn("private-server-details", result.output)
        self.assertEqual(len(self.requests), 1)
        self.credential_store.set_password.assert_not_called()

    def test_browser_login_refuses_non_loopback_http(self):
        result = self.invoke("--api-url", "http://remote.test", "jobs", token="")
        self.assertEqual(result.exit_code, 1, result.output)
        self.assertIn("requires HTTPS", result.output)
        self.assertEqual(self.requests, [])
        self.credential_store.set_password.assert_not_called()

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

    def test_local_mode_never_prompts_or_reads_keychain_even_with_override_token(self):
        self.auth_mode.return_value = False
        self.response = httpx.Response(200, json={"jobs": []})
        for token in ("", "unused-token"):
            result = self.invoke("jobs", token=token)
            self.assertEqual(result.exit_code, 0, result.output)
            self.assertNotIn("authorization", self.requests[-1].headers)
            self.assertNotIn("session token", result.output)
        self.credential_store.get_password.assert_not_called()
        self.credential_store.set_password.assert_not_called()

    def test_local_mode_submits_without_a_token(self):
        self.auth_mode.return_value = False
        result = self.invoke("submit", "--body", str(self.body), token="")
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertNotIn("authorization", self.requests[-1].headers)
        self.assertEqual(json.loads(self.requests[-1].content), {
            "scenarios": PAYLOAD,
            "output_bucket": None,
            "output_prefix": "",
            "max_procs": None,
            "overwrite": False,
        })
        self.credential_store.get_password.assert_not_called()

    def test_auth_mode_is_discovered_from_generated_public_endpoint(self):
        settings = fimbox_cli.Settings("http://localhost:8000", None, 30)
        self.response = httpx.Response(200, json={
            "authentication_enabled": False, "require_hydroshare_output": False,
        })
        with patch.object(fimbox_cli, "__authentication_enabled", wraps=self.real_auth_mode), patch.object(
            fimbox_cli, "__api_config", wraps=self.real_api_config,
        ):
            self.assertFalse(fimbox_cli.__dict__["__authentication_enabled"](settings))
            self.assertFalse(fimbox_cli.__dict__["__api_config"](settings).require_hydroshare_output)
            self.assertEqual(len(self.requests), 1, "Configuration is cached for this invocation")
            for response in (
                httpx.Response(503), httpx.Response(200, json={}),
                httpx.Response(200, json={
                    "authentication_enabled": "false", "require_hydroshare_output": False,
                }),
                httpx.Response(200, json={
                    "authentication_enabled": False, "require_hydroshare_output": "false",
                }),
                httpx.Response(200, json={"authentication_enabled": False}),
                httpx.Response(200, content=b"invalid"),
            ):
                self.response = response
                settings.api_config = None
                with self.assertRaises(click.ClickException):
                    fimbox_cli.__dict__["__authentication_enabled"](settings)
        self.assertEqual(self.requests[-1].url.path, "/auth/config")
        self.assertNotIn("authorization", self.requests[-1].headers)

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

    def test_failed_browser_authenticated_request_does_not_save_token(self):
        self.configure_browser_login()
        self.response = httpx.Response(401, json={"detail": "Invalid session"})
        result = self.invoke("jobs", token="")
        self.assertEqual(result.exit_code, 1)
        self.assertIn("HTTP 401", result.output)
        self.assertEqual(len(self.requests), 3)
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
        self.configure_browser_login()
        self.response = httpx.Response(200, json={"jobs": []})
        result = self.invoke("jobs", token="")
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
        result = self.invoke("jobs")
        self.assertEqual(result.exit_code, 1)
        self.assertIn("HTTP 422", result.output)
        self.response = httpx.Response(200, json={"not": "a job list"})
        result = self.invoke("jobs")
        self.assertEqual(result.exit_code, 1)
        self.assertIn("SDK contract", result.output)

    def test_noninteractive_submission_reports_required_output_bucket(self):
        self.api_config.return_value.require_hydroshare_output = True
        self.response = httpx.Response(422, json={
            "detail": [{
                "loc": ["body", "output_bucket"],
                "msg": "This deployment requires a HydroShare output_bucket",
                "type": "value_error",
            }],
        })
        result = self.invoke("submit", "--body", str(self.body))
        self.assertEqual(result.exit_code, 1, result.output)
        self.assertIn("HTTP 422", result.output)
        self.assertIn("requires a HydroShare output_bucket", result.output)
        self.assertNotIn("SDK contract", result.output)
        self.assertEqual(len(self.requests), 1)
        self.credential_store.set_password.assert_not_called()

    def test_connection_and_option_errors(self):
        with patch.object(httpx, "Client", side_effect=httpx.ConnectError("connection refused")):
            result = self.invoke("health")
        self.assertEqual(result.exit_code, 1)
        self.assertIn("Cannot reach the API", result.output)
        result = self.invoke("status", "--job-id", "invalid")
        self.assertEqual(result.exit_code, 2)
        result = self.invoke("--api-url", "http://user:password@localhost", "health")
        self.assertEqual(result.exit_code, 1)
        self.assertNotIn("password", result.output)


class SdkIntegrationTests(unittest.TestCase):
    def setUp(self):
        for name, value in (
            ("hydroshare_client_id", "test-client"),
            ("hydroshare_client_secret", SecretStr("test-secret")),
        ):
            patcher = patch.object(main.settings, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        main.app.dependency_overrides[auth.get_authenticated_session] = lambda: auth.UserSession(
            user_id="alice", access_token="provider", scope="read write", expires_at=time.time() + 60,
        )
        self.addCleanup(main.app.dependency_overrides.clear)

    def test_interactive_cli_prompts_for_cloud_output_and_enqueues(self):
        with TestClient(main.app) as http_client, patch.object(
            Client, "get_httpx_client", return_value=http_client,
        ), patch.object(
            AuthenticatedClient, "get_httpx_client", return_value=http_client,
        ), patch.object(
            main.settings, "require_hydroshare_output", True,
        ), patch.object(main, "create_job_credentials"), patch.object(main, "track_user_job"), patch.object(
            tasks.hand_fim, "apply_async",
        ) as enqueue:
            result = CliRunner().invoke(
                fimbox_cli.app, ["submit"],
                env={"FIMBOX_API_URL": "http://testserver", "FIMBOX_SESSION_TOKEN": "session"},
                input="\ncloud-test\n\n\n\n\nbucket\nruns/test\nn\n",
            )
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn('"status": "queued"', result.output)
        self.assertEqual(enqueue.call_count, 1)
        self.assertEqual(enqueue.call_args.kwargs["args"][2:], ["bucket", "runs/test", 1, False])

    def test_required_output_bucket_rejection_matches_sdk_contract(self):
        with TestClient(main.app) as http_client, patch.object(
            main.settings, "require_hydroshare_output", True,
        ), patch.object(tasks.hand_fim, "apply_async") as enqueue:
            client = AuthenticatedClient(base_url="http://testserver", token="session")
            client.set_httpx_client(http_client)
            response = submit_hand_fim_jobs_hand_fim_post.sync_detailed(
                client=client,
                body=HandFimJobRequest(
                    scenarios=[Scenario.from_dict(PAYLOAD[0])],
                    output_bucket=None, output_prefix="",
                ),
            )
            self.assertEqual(response.status_code, 422)
            self.assertIsInstance(response.parsed, HTTPValidationError)
            self.assertEqual(json.loads(response.content)["detail"], [{
                "loc": ["body", "output_bucket"],
                "msg": "This deployment requires a HydroShare output_bucket",
                "type": "value_error",
            }])
            enqueue.assert_not_called()

    def test_generated_client_enqueues_through_fastapi(self):
        with TestClient(main.app) as http_client, patch.object(main, "create_job_credentials"), patch.object(
            main, "track_user_job",
        ), patch.object(tasks.hand_fim, "apply_async") as enqueue:
            client = AuthenticatedClient(base_url="http://testserver", token="session")
            client.set_httpx_client(http_client)
            response = submit_hand_fim_jobs_hand_fim_post.sync_detailed(
                client=client,
                body=HandFimJobRequest(
                    scenarios=[Scenario.from_dict(PAYLOAD[0])],
                    output_bucket="bucket", output_prefix="output",
                ),
            )
            self.assertEqual(response.status_code, 202)
            self.assertIsInstance(response.parsed, JobSubmitted)
            enqueue.assert_called_once_with(
                args=[response.parsed.job_id, PAYLOAD, "bucket", "output", None, False],
                task_id=response.parsed.job_id, retry=False,
            )

    def test_cli_enqueues_via_generated_client(self):
        with tempfile.TemporaryDirectory() as directory:
            body = Path(directory) / "scenario.json"
            body.write_text(json.dumps(PAYLOAD))
            with TestClient(main.app) as client, patch.object(
                AuthenticatedClient, "get_httpx_client", return_value=client,
            ), patch.object(
                Client, "get_httpx_client", return_value=client,
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
        live_schema = main.app.openapi()
        self.assertEqual(live_schema["info"], spec["info"])
        self.assertEqual(
            live_schema["paths"],
            {path: item for path, item in spec["paths"].items() if not path.startswith("/auth/")},
            "Live OpenAPI docs should omit auth routes without changing the SDK spec",
        )
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
                    client=client,
                    body=HandFimJobRequest(
                        scenarios=[Scenario.from_dict(PAYLOAD[0])],
                        output_bucket="bucket", output_prefix="output",
                    ),
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
