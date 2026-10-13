import json
import math
import unittest
from unittest.mock import Mock, patch

import httpx

import fimbox_login
from fimbox_sdk import Client


class BrowserLoginTests(unittest.TestCase):
    def setUp(self):
        self.requests = []
        self.clock = Mock()
        self.now = 100
        self.clock.monotonic.side_effect = lambda: self.now
        self.clock.sleep.side_effect = lambda seconds: setattr(self, "now", self.now + seconds)
        patcher = patch.object(fimbox_login, "time", self.clock)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.secret = "s" * 43
        self.device_code = "b" * 43 + "." + self.secret
        self.started = {
            "device_code": self.device_code,
            "verification_uri": "https://api.example/auth/device/verify?code=" + "b" * 43,
            "expires_in": 600, "interval": 5,
        }
        self.responses = [httpx.Response(200, json={"status": "pending", "token": None})]
        self.start_response = None

        def respond(request):
            self.requests.append(request)
            self.assertNotIn(self.secret, str(request.url))
            self.assertNotIn("authorization", request.headers)
            if request.url.path == "/auth/device/start":
                return self.start_response or httpx.Response(200, json=self.started)
            self.assertEqual(request.url.path, "/auth/device/poll")
            self.assertEqual(json.loads(request.content), {"device_code": self.device_code})
            return self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]

        self.client = Client(
            base_url="https://api.example", timeout=httpx.Timeout(30),
            httpx_args={"transport": httpx.MockTransport(respond)},
        )
        self.addCleanup(self.client.get_httpx_client().close)

    def authorized(self):
        return httpx.Response(200, json={
            "status": "authorized",
            "token": {"access_token": "private-session", "expires_in": 800,
                      "user_id": "alice", "scope": "read", "token_type": "bearer"},
        })

    def test_pending_then_authorized_with_private_secret(self):
        self.responses.append(self.authorized())
        login = fimbox_login.begin_login(self.client)
        self.assertEqual(login.authorization_url, self.started["verification_uri"])
        self.assertNotIn(self.secret, repr(login))
        self.assertEqual(login.wait(), "private-session")
        self.clock.sleep.assert_called_once_with(5)
        self.assertEqual(login._device_code, "")
        with self.assertRaisesRegex(RuntimeError, "already completed"):
            login.wait()

    def test_denial_and_provider_failure_are_explicit(self):
        for status, error in (("denied", PermissionError), ("failed", RuntimeError)):
            with self.subTest(status=status):
                self.responses = [httpx.Response(200, json={"status": status})]
                with self.assertRaises(error):
                    fimbox_login.begin_login(self.client).wait()

    def test_poll_slow_down_and_timeout(self):
        self.client.raise_on_unexpected_status = True
        self.responses = [httpx.Response(429), self.authorized()]
        self.assertEqual(fimbox_login.begin_login(self.client).wait(), "private-session")
        self.clock.sleep.assert_called_once_with(10)
        self.responses = [httpx.Response(200, json={"status": "pending"})]
        with self.assertRaises(TimeoutError):
            fimbox_login.begin_login(self.client).wait(timeout=12)
        self.assertEqual(self.now, 122)

    def test_transaction_lifetime_includes_time_spent_in_browser(self):
        login = fimbox_login.begin_login(self.client)
        self.now += 601
        with self.assertRaises(TimeoutError):
            login.wait()
        self.assertEqual(len(self.requests), 1)
        for timeout in (0, -1, math.inf, math.nan):
            with self.assertRaises(ValueError):
                login.wait(timeout=timeout)

    def test_errors_never_include_server_body_or_credentials(self):
        for code in (400, 410, 502, 503, 302):
            with self.subTest(code=code):
                self.responses = [httpx.Response(code, json={"detail": self.secret})]
                with self.assertRaises(RuntimeError) as raised:
                    fimbox_login.begin_login(self.client).wait()
                self.assertIn(f"HTTP {code}", str(raised.exception))
                self.assertNotIn(self.secret, str(raised.exception))
        self.client.raise_on_unexpected_status = True
        self.responses = [httpx.Response(410, content=self.secret.encode())]
        with self.assertRaises(RuntimeError) as raised:
            fimbox_login.begin_login(self.client).wait()
        self.assertNotIn(self.secret, str(raised.exception))

    def test_invalid_authorized_response_and_malformed_start(self):
        for token in (None, {}, {"access_token": "", "expires_in": 4, "scope": "read", "user_id": "alice"}):
            with self.subTest(token=token):
                self.responses = [httpx.Response(200, json={"status": "authorized", "token": token})]
                with self.assertRaises(RuntimeError):
                    fimbox_login.begin_login(self.client).wait()
        self.start_response = httpx.Response(200, json={"private": self.secret})
        with self.assertRaises(RuntimeError) as raised:
            fimbox_login.begin_login(self.client)
        self.assertNotIn(self.secret, str(raised.exception))

    def test_start_http_failure_and_connection_failure(self):
        self.start_response = httpx.Response(404, json={"detail": self.secret})
        with self.assertRaisesRegex(RuntimeError, "HTTP 404"):
            fimbox_login.begin_login(self.client)
        self.start_response = None
        with patch.object(
            self.client.get_httpx_client(), "request", side_effect=httpx.ConnectError(self.secret),
        ):
            with self.assertRaisesRegex(RuntimeError, "connection failed") as raised:
                fimbox_login.begin_login(self.client)
        self.assertNotIn(self.secret, str(raised.exception))

    def test_invalid_origin_redirects_and_unbounded_timeout(self):
        for url in ("http://remote.example", "https://user:password@api.example", "https://api.example?secret=value"):
            with self.subTest(url=url), Client(base_url=url, timeout=httpx.Timeout(30)) as client:
                with self.assertRaises(ValueError):
                    fimbox_login.begin_login(client)
        self.client.get_httpx_client().follow_redirects = True
        with self.assertRaises(ValueError):
            fimbox_login.begin_login(self.client)
        self.client.get_httpx_client().follow_redirects = False
        self.client.get_httpx_client().timeout = httpx.Timeout(None)
        with self.assertRaises(ValueError):
            fimbox_login.begin_login(self.client)

    def test_verification_uri_must_stay_on_api_origin(self):
        for uri in (
            "https://evil.example/verify", "http://api.example/verify",
            "https://api.example/verify#secret",
            self.started["verification_uri"] + "&secret=" + self.secret,
        ):
            with self.subTest(uri=uri):
                self.started["verification_uri"] = uri
                with self.assertRaises(RuntimeError):
                    fimbox_login.begin_login(self.client)


if __name__ == "__main__":
    unittest.main()
