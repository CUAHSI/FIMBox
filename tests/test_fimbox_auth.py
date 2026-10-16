import json
import os
import re
import time
import unittest
from copy import copy
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch
from urllib.parse import parse_qs, urlparse
from uuid import uuid4

import httpx
from fastapi.testclient import TestClient
from fastapi.openapi.utils import get_openapi
from pydantic import SecretStr, ValidationError
from redis.exceptions import ConnectionError, WatchError

from fimbox_api import auth, main
from fimbox_api.config import Settings
from fimbox_api import device_auth


class RedisPipeline:
    """Exercise queued writes against the existing in-memory Redis mock."""

    def __init__(self, values):
        self.values = values
        self.commands = []
        self.conflicts = 0

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.commands.clear()

    def watch(self, key):
        self.commands.clear()

    def get(self, key):
        return self.values.get(key)

    def multi(self):
        pass

    def set(self, key, value, **kwargs):
        self.commands.append(("set", key, value))
        assert kwargs == {"keepttl": True}

    def incr(self, key):
        self.commands.append(("incr", key, None))

    def expire(self, key, seconds):
        self.commands.append(("expire", key, seconds))

    def execute(self):
        if self.conflicts:
            self.conflicts -= 1
            raise WatchError()
        results = []
        for command, key, value in self.commands:
            if command == "set":
                self.values[key] = value
                results.append(True)
            elif command == "incr":
                self.values[key] = int(self.values.get(key, 0)) + 1
                results.append(self.values[key])
            else:
                results.append(True)
        self.commands.clear()
        return results


class AuthTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(main.app)
        self.values = {}
        self.redis = MagicMock()
        self.redis.set.side_effect = lambda key, value, **kwargs: self.values.update({key: value})
        self.redis.setex.side_effect = lambda key, ttl, value: self.values.update({key: value})
        self.redis.get.side_effect = self.values.get
        self.redis.getdel.side_effect = lambda key: self.values.pop(key, None)
        self.redis.delete.side_effect = lambda key: self.values.pop(key, None)
        patcher = patch.object(auth, "get_redis_client", return_value=self.redis)
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = patch.object(device_auth, "get_redis_client", return_value=self.redis)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.pipeline = RedisPipeline(self.values)
        for name, value in (
            ("hydroshare_client_id", "test-client"),
            ("hydroshare_client_secret", SecretStr("test-secret")),
            ("oauth_state_cookie_secure", False),
        ):
            patcher = patch.object(auth.settings, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def __provider(self):
        client = Mock()
        client.post.return_value = httpx.Response(
            200, json={"access_token": "hydroshare-access", "refresh_token": "hydroshare-refresh", "scope": "read"},
            request=httpx.Request("POST", "https://provider.example/token"),
        )
        client.get.return_value = httpx.Response(
            200, json={"username": "alice"},
            request=httpx.Request("GET", "https://provider.example/user"),
        )
        context = patch.object(auth.httpx, "Client")
        factory = context.start()
        factory.return_value.__enter__.return_value = client
        self.addCleanup(context.stop)
        return client

    def test_local_mode_exposes_config_and_needs_no_session(self):
        with patch.object(auth.settings, "hydroshare_client_id", ""), patch.object(
            auth.settings, "hydroshare_client_secret", SecretStr(""),
        ):
            response = self.client.get("/auth/config")
            self.assertEqual(response.json(), {
                "authentication_enabled": False, "require_hydroshare_output": False,
            })
            response = self.client.get("/auth/session")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["user_id"], auth.LOCAL_USER_ID)
            self.assertEqual(self.client.post("/auth/logout").status_code, 409)
        self.redis.get.assert_not_called()

    def test_configured_mode_advertises_authentication(self):
        self.assertEqual(
            self.client.get("/auth/config").json(), {
                "authentication_enabled": True, "require_hydroshare_output": False,
            },
        )

    def test_config_advertises_required_hydroshare_output(self):
        with patch.object(auth.settings, "require_hydroshare_output", True):
            self.assertEqual(self.client.get("/auth/config").json(), {
                "authentication_enabled": True, "require_hydroshare_output": True,
            })

    def __device_start(self):
        self.redis.pipeline.return_value = self.pipeline
        response = self.client.post("/auth/device/start")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["cache-control"], "no-store")
        started = response.json()
        browser_code, secret = started["device_code"].split(".")
        self.assertNotIn(secret, started["verification_uri"])
        self.assertEqual(started["interval"], 5)
        self.assertEqual(started["expires_in"], 600)
        self.assertNotIn(secret, str(self.values))
        return started, browser_code

    def __device_approve(self, browser_code):
        page = self.client.get("/auth/device/verify", params={"code": browser_code})
        self.assertEqual(page.status_code, 200)
        self.assertIn("only if you started", page.text)
        self.assertIn("frame-ancestors 'none'", page.headers["content-security-policy"])
        csrf = re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)
        response = self.client.post(
            "/auth/device/verify", data={"code": browser_code, "csrf": csrf},
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 302)
        return parse_qs(urlparse(response.headers["location"]).query)["state"][0]

    def test_device_browser_approval_and_one_time_sdk_retrieval(self):
        started, browser_code = self.__device_start()
        state = self.__device_approve(browser_code)
        self.__provider()
        response = self.client.get("/auth/callback", params={"code": "auth-code", "state": state})
        self.assertEqual(response.status_code, 200)
        self.assertIn("Authorization complete", response.text)
        self.assertNotIn("access_token", response.text)
        record = json.loads(self.values[device_auth.DEVICE_PREFIX + browser_code])
        session_token = record["token"]["access_token"]
        self.assertNotIn(session_token, response.text)
        self.assertNotIn(session_token, response.headers.get("location", ""))
        self.pipeline.conflicts = 1
        poll = self.client.post("/auth/device/poll", json={"device_code": started["device_code"]})
        self.assertEqual(poll.status_code, 200)
        self.assertEqual(poll.json()["status"], "authorized")
        self.assertEqual(poll.json()["token"]["access_token"], session_token)
        self.assertEqual(poll.headers["cache-control"], "no-store")
        self.assertNotIn(session_token, self.values[device_auth.DEVICE_PREFIX + browser_code])
        self.assertEqual(
            self.client.get("/auth/session", headers={"Authorization": "Bearer " + session_token}).status_code, 200,
        )
        self.assertEqual(self.client.post(
            "/auth/device/poll", json={"device_code": started["device_code"]},
        ).status_code, 410)
        self.client.cookies.set(auth.STATE_COOKIE, state)
        self.assertEqual(self.client.get(
            "/auth/callback", params={"code": "auth-code", "state": state},
        ).status_code, 400)

    def test_device_pending_secret_validation_and_poll_rate(self):
        started, browser_code = self.__device_start()
        wrong = browser_code + "." + "x" * 43
        self.assertEqual(self.client.post("/auth/device/poll", json={"device_code": wrong}).status_code, 400)
        self.assertEqual(self.client.post("/auth/device/poll", json={"device_code": browser_code}).status_code, 422)
        poll = self.client.post("/auth/device/poll", json={"device_code": started["device_code"]})
        self.assertEqual(poll.json(), {"status": "pending", "token": None})
        limited = self.client.post("/auth/device/poll", json={"device_code": started["device_code"]})
        self.assertEqual(limited.status_code, 429)
        self.assertEqual(limited.headers["retry-after"], "5")
        self.assertEqual(limited.headers["cache-control"], "no-store")
        with patch.object(device_auth.time, "time", return_value=time.time() + 6):
            self.assertEqual(self.client.post(
                "/auth/device/poll", json={"device_code": started["device_code"]},
            ).status_code, 200)

    def test_device_sdk_contract_matches_routes(self):
        spec = json.loads((Path(__file__).resolve().parents[1] / "sdk/openapi.json").read_text())
        routes = [
            copy(route) for route in main.app.routes
            if getattr(route, "path", "") in ("/auth/device/start", "/auth/device/poll", "/auth/callback")
        ]
        for route in routes:
            route.include_in_schema = True
        schema = get_openapi(title=main.app.title, version=main.app.version, routes=routes)
        for path, operations in schema["paths"].items():
            self.assertEqual(spec["paths"][path], operations)
        for name, model in schema["components"]["schemas"].items():
            self.assertEqual(spec["components"]["schemas"][name], model)

    def test_device_denial_and_invalid_callback_state(self):
        started, browser_code = self.__device_start()
        state = self.__device_approve(browser_code)
        self.assertEqual(self.client.get(
            "/auth/callback", params={"error": "access_denied", "state": "invalid"},
        ).status_code, 400)
        response = self.client.get("/auth/callback", params={"error": "access_denied", "state": state})
        self.assertIn("Authorization denied", response.text)
        poll = self.client.post("/auth/device/poll", json={"device_code": started["device_code"]})
        self.assertEqual(poll.json(), {"status": "denied", "token": None})
        self.assertEqual(self.client.post(
            "/auth/device/poll", json={"device_code": started["device_code"]},
        ).status_code, 410)

    def test_device_expiry_and_confirmation_csrf(self):
        started, browser_code = self.__device_start()
        self.assertEqual(self.client.post(
            "/auth/device/verify", data={"code": browser_code, "csrf": "bad"}, follow_redirects=False,
        ).status_code, 400)
        self.assertEqual(self.client.get("/auth/device/verify", params={"code": '<script>'}).status_code, 400)
        with patch.object(device_auth.time, "time", return_value=time.time() + 601):
            self.assertEqual(self.client.post(
                "/auth/device/poll", json={"device_code": started["device_code"]},
            ).status_code, 410)
        self.values.pop(device_auth.DEVICE_PREFIX + browser_code)
        self.assertEqual(self.client.post(
            "/auth/device/poll", json={"device_code": started["device_code"]},
        ).status_code, 410)

    def test_device_start_rate_limit_and_anonymous_mode(self):
        self.redis.pipeline.return_value = self.pipeline
        for _ in range(30):
            self.assertEqual(self.client.post("/auth/device/start").status_code, 200)
        self.assertEqual(self.client.post("/auth/device/start").status_code, 429)
        with patch.object(auth.settings, "hydroshare_client_id", ""), patch.object(
            auth.settings, "hydroshare_client_secret", SecretStr(""),
        ):
            self.assertEqual(self.client.post("/auth/device/start").status_code, 409)

    def test_device_callback_expiry_revokes_unclaimed_session(self):
        started, browser_code = self.__device_start()
        state = self.__device_approve(browser_code)
        self.__provider()
        self.values.pop(device_auth.DEVICE_PREFIX + browser_code)
        self.assertEqual(self.client.get(
            "/auth/callback", params={"code": "auth-code", "state": state},
        ).status_code, 410)
        self.assertFalse(any(key.startswith(auth.SESSION_PREFIX) for key in self.values))

    def test_device_provider_failure_is_reported_to_sdk(self):
        started, browser_code = self.__device_start()
        state = self.__device_approve(browser_code)
        provider = self.__provider()
        provider.post.return_value = httpx.Response(
            401, json={"error": "invalid_client"}, request=httpx.Request("POST", "https://provider.example"),
        )
        with self.assertLogs("fimbox_api.auth", level="ERROR"):
            response = self.client.get("/auth/callback", params={"code": "auth-code", "state": state})
        self.assertEqual(response.status_code, 502)
        poll = self.client.post("/auth/device/poll", json={"device_code": started["device_code"]})
        self.assertEqual(poll.json(), {"status": "failed", "token": None})

    def test_device_callback_is_bound_to_original_transaction(self):
        first, browser_code = self.__device_start()
        second, _ = self.__device_start()
        state = self.__device_approve(browser_code)
        self.__provider()
        self.client.get("/auth/callback", params={"code": "auth-code", "state": state})
        self.assertEqual(self.client.post(
            "/auth/device/poll", json={"device_code": second["device_code"]},
        ).json()["status"], "pending")
        self.assertEqual(self.client.post(
            "/auth/device/poll", json={"device_code": first["device_code"]},
        ).json()["status"], "authorized")

    def test_device_cannot_restart_approval_or_return_expired_session(self):
        started, browser_code = self.__device_start()
        state = self.__device_approve(browser_code)
        page = self.client.get("/auth/device/verify", params={"code": browser_code})
        csrf = re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)
        self.assertEqual(self.client.post(
            "/auth/device/verify", data={"code": browser_code, "csrf": csrf}, follow_redirects=False,
        ).status_code, 409)
        self.__provider()
        with patch.object(auth.settings, "session_ttl_seconds", 2):
            self.client.get("/auth/callback", params={"code": "auth-code", "state": state})
        with patch.object(device_auth.time, "time", return_value=time.time() + 6):
            self.assertEqual(self.client.post(
                "/auth/device/poll", json={"device_code": started["device_code"]},
            ).status_code, 410)

    def test_generated_sdk_helper_completes_browser_login_end_to_end(self):
        from fimbox_login import begin_login
        from fimbox_sdk import Client

        self.redis.pipeline.return_value = self.pipeline
        started = {}

        def respond(request):
            if request.url.path == "/auth/device/start":
                response = self.client.post(request.url.path)
                started.update(response.json())
            else:
                browser_code = started["device_code"].split(".")[0]
                state = self.__device_approve(browser_code)
                browser = self.client.get("/auth/callback", params={"code": "auth-code", "state": state})
                self.assertIn("Authorization complete", browser.text)
                response = self.client.post(request.url.path, json=json.loads(request.content))
            return httpx.Response(response.status_code, json=response.json(), headers=response.headers)

        http = httpx.Client(base_url="http://localhost:8000", transport=httpx.MockTransport(respond), timeout=30)
        self.addCleanup(http.close)
        self.__provider()
        client = Client(base_url="http://localhost:8000").set_httpx_client(http)
        login = begin_login(client)
        token = login.wait()
        self.assertEqual(self.client.get(
            "/auth/session", headers={"Authorization": "Bearer " + token},
        ).json()["user_id"], "alice")

    def test_device_conflict_retry_limit_and_redis_failure_are_explicit(self):
        started, browser_code = self.__device_start()
        self.pipeline.conflicts = 5
        with self.assertLogs("fimbox_api.device_auth", level="ERROR"):
            response = self.client.post("/auth/device/poll", json={"device_code": started["device_code"]})
        self.assertEqual(response.status_code, 503)
        self.assertEqual(json.loads(self.values[device_auth.DEVICE_PREFIX + browser_code])["status"], "pending")
        self.redis.pipeline.side_effect = ConnectionError("unavailable")
        with self.assertLogs("fimbox_api.main", level="ERROR"):
            self.assertEqual(self.client.post("/auth/device/start").status_code, 503)

    def test_partial_oauth_configuration_fails_without_disclosing_inputs(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(Settings(_env_file=None).authentication_enabled)
            self.assertTrue(Settings(
                _env_file=None, API_CLIENT_ID="client", API_CLIENT_SECRET="secret",
            ).authentication_enabled)
            for values in ({"API_CLIENT_ID": "client"}, {"API_CLIENT_SECRET": "private-test-secret"}):
                with self.assertRaises(ValidationError) as raised:
                    Settings(_env_file=None, **values)
                self.assertNotIn("private-test-secret", str(raised.exception))

    def test_provider_cannot_issue_a_session_for_local_identity(self):
        state = self.__login()
        provider = self.__provider()
        provider.get.return_value = httpx.Response(
            200, json={"username": auth.LOCAL_USER_ID},
            request=httpx.Request("GET", "https://provider.example/user"),
        )
        self.assertEqual(
            self.client.get("/auth/callback", params={"code": "code", "state": state}).status_code, 502,
        )
        self.redis.set.assert_not_called()

    def __login(self):
        response = self.client.get("/auth/login", follow_redirects=False)
        self.assertEqual(response.status_code, 302)
        query = parse_qs(urlparse(response.headers["location"]).query)
        self.assertEqual(query["client_id"], ["test-client"])
        self.assertIn("HttpOnly", response.headers["set-cookie"])
        return query["state"][0]

    def test_login_callback_session_and_logout(self):
        state = self.__login()
        provider = self.__provider()
        response = self.client.get("/auth/callback", params={"code": "auth-code", "state": state})
        self.assertEqual(response.status_code, 200)
        token = response.json()
        self.assertEqual(token["user_id"], "alice")
        self.assertEqual(token["expires_in"], auth.settings.session_ttl_seconds)
        self.assertNotIn("hydroshare-access", response.text)
        self.assertNotIn("hydroshare-refresh", response.text)
        self.assertEqual(response.headers["cache-control"], "no-store")
        provider.get.assert_called_once_with(
            auth.settings.hydroshare_userinfo_url,
            headers={"Authorization": "Bearer hydroshare-access"},
        )
        self.redis.set.assert_called_once()
        self.assertEqual(self.redis.set.call_args.kwargs["ex"], auth.settings.session_ttl_seconds)
        headers = {"Authorization": "Bearer " + token["access_token"]}
        session = self.client.get("/auth/session", headers=headers)
        self.assertEqual(session.status_code, 200)
        self.assertEqual(session.json()["user_id"], "alice")
        self.assertNotIn("access_token", session.json())
        self.assertEqual(self.client.post("/auth/logout", headers=headers).status_code, 204)
        self.assertEqual(self.client.get("/auth/session", headers=headers).status_code, 401)

    def test_state_cookie_and_replay_validation(self):
        state = self.__login()
        provider = self.__provider()
        self.assertEqual(self.client.get("/auth/callback", params={"code": "code", "state": "wrong"}).status_code, 400)
        self.client.cookies.clear()
        self.assertEqual(self.client.get("/auth/callback", params={"code": "code", "state": state}).status_code, 400)
        provider.post.assert_not_called()
        self.client.cookies.set(auth.STATE_COOKIE, state)
        self.assertEqual(self.client.get("/auth/callback", params={"code": "code", "state": state}).status_code, 200)
        self.client.cookies.set(auth.STATE_COOKIE, state)
        self.assertEqual(self.client.get("/auth/callback", params={"code": "code", "state": state}).status_code, 400)
        provider.post.assert_called_once()

    def test_provider_failure_does_not_issue_session(self):
        state = self.__login()
        provider = self.__provider()
        provider.post.return_value = httpx.Response(
            401, json={"error": "invalid_client"}, request=httpx.Request("POST", "https://provider.example"),
        )
        response = self.client.get("/auth/callback", params={"code": "code", "state": state})
        self.assertEqual(response.status_code, 502)
        self.assertEqual(
            response.json()["detail"],
            "HydroShare token exchange failed (HTTP 401): invalid_client",
        )
        self.redis.set.assert_not_called()

    def test_userinfo_failure_identifies_stage_without_exposing_tokens(self):
        state = self.__login()
        provider = self.__provider()
        provider.get.return_value = httpx.Response(
            403, json={"error": "insufficient_scope", "error_description": "sensitive"},
            request=httpx.Request("GET", "https://provider.example/user?token=secret"),
        )
        response = self.client.get("/auth/callback", params={"code": "code", "state": state})
        self.assertEqual(response.status_code, 502)
        self.assertEqual(
            response.json()["detail"],
            "HydroShare user-info lookup failed (HTTP 403): insufficient_scope",
        )
        self.assertNotIn("sensitive", response.text)
        self.assertNotIn("secret", response.text)
        self.redis.set.assert_not_called()

    def test_unknown_provider_errors_are_not_exposed(self):
        state = self.__login()
        provider = self.__provider()
        provider.post.return_value = httpx.Response(
            400, json={"error": "secret-code", "error_description": "sensitive"},
            request=httpx.Request("POST", "https://provider.example"),
        )
        response = self.client.get("/auth/callback", params={"code": "code", "state": state})
        self.assertEqual(response.json()["detail"], "HydroShare token exchange failed (HTTP 400)")
        self.assertNotIn("secret-code", response.text)
        self.redis.set.assert_not_called()

    def test_provider_user_identity_is_required(self):
        state = self.__login()
        provider = self.__provider()
        provider.get.return_value = httpx.Response(
            200, json={}, request=httpx.Request("GET", "https://provider.example/user"),
        )
        self.assertEqual(self.client.get("/auth/callback", params={"code": "code", "state": state}).status_code, 502)
        self.redis.set.assert_not_called()

    def test_missing_configuration_and_redis_errors(self):
        with patch.object(auth.settings, "hydroshare_client_secret", SecretStr("")):
            self.assertEqual(self.client.get("/auth/login").status_code, 503)
        self.redis.get.side_effect = ConnectionError("offline")
        self.assertEqual(self.client.get("/auth/session", headers={"Authorization": "Bearer token"}).status_code, 503)

    def test_expired_session(self):
        self.redis.get.side_effect = None
        self.redis.get.return_value = json.dumps({
            "user_id": "alice", "access_token": "provider-token", "scope": "read", "expires_at": time.time() - 1,
        })
        self.assertEqual(self.client.get("/auth/session", headers={"Authorization": "Bearer token"}).status_code, 401)
        self.redis.delete.assert_called_once()

    def test_all_job_routes_require_session(self):
        job_id = str(uuid4())
        routes = [
            ("GET", "/jobs", None),
            ("GET", f"/jobs/{job_id}", None),
            ("DELETE", f"/jobs/{job_id}", None),
            ("POST", "/jobs/hand-fim", {"scenarios": []}),
        ]
        for method, route, payload in routes:
            self.assertEqual(self.client.request(method, route, json=payload).status_code, 401)
            self.assertEqual(self.client.request(
                method, route, json=payload, headers={"Authorization": "Bearer unknown"},
            ).status_code, 401)
        self.assertEqual(self.client.get("/health").status_code, 200)
        self.assertEqual(self.client.get("/auth/token").status_code, 404)

    def test_swagger_code_exchange_requires_pkce_and_registered_redirect(self):
        provider = self.__provider()
        body = {"code": "code", "code_verifier": "a" * 43, "redirect_uri": auth.settings.swagger_oauth_redirect_uri}
        response = self.client.post("/auth/oauth/token", data=body)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(provider.post.call_args.kwargs["data"]["code_verifier"], "a" * 43)
        self.assertEqual(provider.post.call_args.kwargs["data"]["client_id"], "test-client")
        self.assertEqual(provider.post.call_args.kwargs["data"]["client_secret"], "test-secret")
        self.assertNotIn("test-secret", response.text)
        self.assertEqual(self.client.post("/auth/oauth/token", data={"code": "code"}).status_code, 422)
        body["redirect_uri"] = "https://unregistered.example/callback"
        self.assertEqual(self.client.post("/auth/oauth/token", data=body).status_code, 400)
        body["grant_type"] = "client_credentials"
        self.assertEqual(self.client.post("/auth/oauth/token", data=body).status_code, 400)

    def test_job_ownership_and_listing(self):
        pipeline = self.redis.pipeline.return_value.__enter__.return_value
        auth.track_user_job("alice", "job")
        pipeline.set.assert_called_once_with(
            auth.JOB_OWNER_PREFIX + "job", "alice", ex=auth.settings.result_ttl_seconds,
        )
        self.values[auth.JOB_OWNER_PREFIX + "job"] = "alice"
        self.assertTrue(auth.user_has_job("alice", "job"))
        self.assertFalse(auth.user_has_job("bob", "job"))
        self.redis.zrange.return_value = ["job"]
        self.assertEqual(auth.list_user_jobs("alice"), ["job"])
        auth.untrack_user_job("alice", "job")
        pipeline.zrem.assert_called_once_with(auth.USER_JOB_PREFIX + "alice", "job")

    def test_swagger_does_not_publish_client_secret(self):
        response = self.client.get("/docs")
        self.assertEqual(response.status_code, 200)
        self.assertIn("clientId", response.text)
        self.assertNotIn("clientSecret", response.text)
        self.assertNotIn("test-secret", response.text)


class OAuthSettingsTests(unittest.TestCase):
    def test_cloud_policy_cannot_start_anonymously(self):
        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaisesRegex(ValidationError, "requires HydroShare"):
                Settings(_env_file=None, require_hydroshare_output=True)
            self.assertTrue(Settings(
                _env_file=None, require_hydroshare_output=True,
                API_CLIENT_ID="client", API_CLIENT_SECRET="secret",
            ).authentication_enabled)

    def test_api_client_aliases_and_hydroshare_precedence(self):
        with patch.dict("os.environ", {
            "API_CLIENT_ID": "alias-client",
            "API_CLIENT_SECRET": "alias-secret",
        }, clear=True):
            settings = Settings(_env_file=None)
            self.assertEqual(settings.hydroshare_client_id, "alias-client")
            self.assertEqual(settings.hydroshare_client_secret.get_secret_value(), "alias-secret")
            with patch.dict("os.environ", {
                "HYDROSHARE_CLIENT_ID": "preferred-client",
                "HYDROSHARE_CLIENT_SECRET": "preferred-secret",
            }):
                settings = Settings(_env_file=None)
                self.assertEqual(settings.hydroshare_client_id, "preferred-client")
                self.assertEqual(settings.hydroshare_client_secret.get_secret_value(), "preferred-secret")


if __name__ == "__main__":
    unittest.main()
