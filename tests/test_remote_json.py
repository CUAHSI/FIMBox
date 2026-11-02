import json
import socket
import unittest
from unittest.mock import patch

import httpx

from fimbox_api import remote_json


def addrinfo(*addresses):
    return lambda host, port, type: [
        (socket.AF_INET6 if ":" in address else socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, port))
        for address in addresses
    ]


class RemoteJsonTests(unittest.TestCase):
    def setUp(self):
        self.requests = []
        self.routes = {}
        transport = httpx.MockTransport(self.handle)
        real_client = httpx.Client
        patcher = patch.object(
            remote_json.httpx, "Client",
            side_effect=lambda **kwargs: real_client(transport=transport, **kwargs),
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self.resolve = patch.object(remote_json.socket, "getaddrinfo", side_effect=addrinfo("93.184.216.34"))
        self.resolve.start()
        self.addCleanup(self.resolve.stop)

    def handle(self, request):
        self.requests.append(request)
        return self.routes.get(
            (request.headers["host"], request.url.path), httpx.Response(404),
        )

    def test_fetches_json_pinned_to_the_checked_address(self):
        self.routes["example.org", "/s.json"] = httpx.Response(200, json=[{"ScenarioID": "a"}])
        self.assertEqual(remote_json.fetch_json("https://example.org/s.json"), [{"ScenarioID": "a"}])
        request = self.requests[0]
        self.assertEqual(request.url.host, "93.184.216.34")
        self.assertEqual(request.headers["host"], "example.org")
        self.assertEqual(request.extensions["sni_hostname"], "example.org")

    def test_redirects_are_followed_and_rechecked(self):
        self.routes["example.org", "/s.json"] = httpx.Response(302, headers={"location": "https://cdn.example.org/x.json"})
        self.routes["cdn.example.org", "/x.json"] = httpx.Response(200, json=[])
        self.assertEqual(remote_json.fetch_json("https://example.org/s.json"), [])
        self.routes["example.org", "/s.json"] = httpx.Response(302, headers={"location": "https://internal.example/x"})
        with patch.object(remote_json.socket, "getaddrinfo", side_effect=lambda host, port, type: addrinfo(
            "10.0.0.5" if host == "internal.example" else "93.184.216.34")(host, port, type)):
            with self.assertRaisesRegex(remote_json.RemoteJsonError, "non-public"):
                remote_json.fetch_json("https://example.org/s.json")
        self.routes["example.org", "/loop"] = httpx.Response(302, headers={"location": "/loop"})
        with self.assertRaisesRegex(remote_json.RemoteJsonError, "redirects"):
            remote_json.fetch_json("https://example.org/loop")

    def test_rejects_unsafe_urls_and_addresses(self):
        for url, message in (
            ("http://example.org/s.json", "https"),
            ("file:///etc/passwd", "https"),
            ("https://user:pw@example.org/s.json", "credentials"),
            ("https://example.org:99999/s.json", "port"),
        ):
            with self.assertRaisesRegex(remote_json.RemoteJsonError, message):
                remote_json.fetch_json(url)
        for address in ("127.0.0.1", "169.254.169.254", "192.168.1.2", "::1", "::ffff:10.0.0.1", "fd00::1", "224.0.0.1"):
            with patch.object(remote_json.socket, "getaddrinfo", side_effect=addrinfo("93.184.216.34", address)):
                with self.assertRaisesRegex(remote_json.RemoteJsonError, "non-public", msg=address):
                    remote_json.fetch_json("https://example.org/s.json")
        self.assertEqual(self.requests, [])

    def test_rejects_bad_responses(self):
        self.routes["example.org", "/missing"] = httpx.Response(403)
        self.routes["example.org", "/html"] = httpx.Response(200, text="<html>")
        self.routes["example.org", "/big"] = httpx.Response(200, content=b"[" + b" " * remote_json.MAX_BYTES + b"]")
        for path, message in (("/missing", "HTTP 403"), ("/html", "valid JSON"), ("/big", "larger")):
            with self.assertRaisesRegex(remote_json.RemoteJsonError, message):
                remote_json.fetch_json(f"https://example.org{path}")


if __name__ == "__main__":
    unittest.main()
