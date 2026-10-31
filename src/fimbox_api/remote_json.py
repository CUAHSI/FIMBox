"""Fetch a small JSON document from a public https URL without reaching internal addresses.

Each hop's host is resolved once, every resolved address must be globally routable,
and the connection is pinned to the checked address (with SNI and certificate
verification against the original host name) so DNS rebinding can't redirect it.
Redirects are followed manually so each hop is checked the same way.
"""

import ipaddress
import json
import socket
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx

MAX_BYTES = 2 * 1024 * 1024
MAX_REDIRECTS = 5
TIMEOUT_SECONDS = 10


class RemoteJsonError(ValueError):
    """The URL or its response is not an acceptable public JSON document."""


def __public_address(host: str, port: int) -> str:
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except (socket.gaierror, UnicodeError):
        raise RemoteJsonError(f"Cannot resolve host {host}") from None
    addresses = [info[4][0] for info in infos]
    for address in addresses:
        ip = ipaddress.ip_address(address.split("%", 1)[0])
        if ip.version == 6 and ip.ipv4_mapped:
            ip = ip.ipv4_mapped
        if not ip.is_global or ip.is_multicast:
            raise RemoteJsonError(f"Host {host} resolves to a non-public address")
    if not addresses:
        raise RemoteJsonError(f"Cannot resolve host {host}")
    return addresses[0]


def __read_limited(response: httpx.Response) -> bytes:
    content = bytearray()
    for chunk in response.iter_bytes():
        content.extend(chunk)
        if len(content) > MAX_BYTES:
            raise RemoteJsonError(f"Response is larger than {MAX_BYTES // (1024 * 1024)} MiB")
    return bytes(content)


def fetch_json(url: str) -> Any:
    """
    Downloads and parses a JSON document from a public https URL.

    Arguments:
        url: An https URL without credentials.
    Returns:
        The parsed JSON value.
    """
    with httpx.Client(timeout=TIMEOUT_SECONDS, follow_redirects=False, trust_env=False) as client:
        for _ in range(MAX_REDIRECTS + 1):
            parts = urlsplit(url)
            try:
                port = parts.port or 443
            except ValueError:
                raise RemoteJsonError("URL has an invalid port") from None
            if parts.scheme != "https" or not parts.hostname:
                raise RemoteJsonError("Only https:// URLs can be loaded")
            if parts.username is not None or parts.password is not None:
                raise RemoteJsonError("URLs with embedded credentials are not allowed")
            address = __public_address(parts.hostname, port)
            pinned = httpx.URL(url).copy_with(host=address)
            with client.stream(
                "GET", pinned,
                headers={"Host": parts.netloc, "Accept": "application/json, */*;q=0.5"},
                extensions={"sni_hostname": parts.hostname},
            ) as response:
                if response.is_redirect and "location" in response.headers:
                    url = urljoin(url, response.headers["location"])
                    continue
                if response.status_code != 200:
                    raise RemoteJsonError(f"URL returned HTTP {response.status_code}")
                content = __read_limited(response)
            try:
                return json.loads(content)
            except (ValueError, UnicodeDecodeError):
                raise RemoteJsonError("URL did not return valid JSON") from None
    raise RemoteJsonError(f"More than {MAX_REDIRECTS} redirects")
