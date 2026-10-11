import json
import time
import unittest
from unittest.mock import MagicMock, Mock, patch
from urllib.parse import parse_qs, urlparse
from uuid import uuid4

import httpx
from fastapi.testclient import TestClient
from pydantic import SecretStr
from redis.exceptions import ConnectionError

from fimbox_api import auth, main
from fimbox_api.config import Settings


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
            ("GET", f"/jobs/{job_id}/outputs/0.tif", None),
            ("GET", f"/jobs/{job_id}/files/0", None),
            ("GET", f"/jobs/{job_id}/files/0/content", None),
            ("DELETE", f"/jobs/{job_id}", None),
            ("POST", "/jobs", {}),
            ("POST", "/jobs/hand-fim?output_bucket=bucket&output_prefix=out", []),
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
