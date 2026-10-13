"""Browser-assisted login using the generated SDK, including remote notebooks."""

import math
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TypeVar
from urllib.parse import urlsplit

import httpx

from fimbox_sdk import Client, errors
from fimbox_sdk.api.authentication import (
    begin_device_login_auth_device_start_post,
    poll_device_session_auth_device_poll_post,
)
from fimbox_sdk.models import (
    DeviceLoginPoll,
    DeviceLoginPollStatus,
    DeviceLoginRequest,
    DeviceLoginStarted,
    SessionToken,
)
from fimbox_sdk.types import Response

T = TypeVar("T")


def _request(operation: Callable[[], Response[T]]) -> Response[T]:
    try:
        return operation()
    except httpx.RequestError:
        raise RuntimeError("Login connection failed; start a new login before retrying.") from None
    except errors.UnexpectedStatus as error:
        raise RuntimeError(f"Login failed (HTTP {error.status_code}); start a new login.") from None
    except (ValueError, KeyError, TypeError, AttributeError):
        raise RuntimeError("Login response does not match the SDK contract.") from None


@dataclass(repr=False)
class BrowserLogin:
    """A pending login. Display authorization_url, then call wait in the same client context."""

    authorization_url: str
    _client: Client = field(repr=False)
    _device_code: str = field(repr=False)
    _deadline: float = field(repr=False)
    _interval: int = field(repr=False)

    def wait(self, timeout: float = 600) -> str:
        """Wait for browser approval and return the one-time FIMBox session token.

        Arguments:
            timeout: Maximum seconds to wait, bounded by the transaction lifetime.
        Returns:
            Session token for AuthenticatedClient; never print or persist in plaintext.
        """
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("Login timeout must be positive and finite.")
        if not self._device_code:
            raise RuntimeError("Login transaction already completed; start a new login.")
        deadline = min(self._deadline, time.monotonic() + timeout)
        while time.monotonic() < deadline:
            response = _request(lambda: poll_device_session_auth_device_poll_post.sync_detailed(
                client=self._client, body=DeviceLoginRequest(device_code=self._device_code),
            ))
            if response.status_code == 429:
                self._interval += 5
            elif response.status_code != 200:
                raise RuntimeError(f"Login failed (HTTP {response.status_code}); start a new login.")
            elif not isinstance(response.parsed, DeviceLoginPoll):
                raise RuntimeError("Login response does not match the SDK contract.")
            elif response.parsed.status == DeviceLoginPollStatus.DENIED:
                self._device_code = ""
                raise PermissionError("Browser authorization was denied; start a new login.")
            elif response.parsed.status == DeviceLoginPollStatus.FAILED:
                self._device_code = ""
                raise RuntimeError("Browser authentication failed; check the browser and start a new login.")
            elif response.parsed.status == DeviceLoginPollStatus.AUTHORIZED:
                token = response.parsed.token
                self._device_code = ""
                if (
                    not isinstance(token, SessionToken)
                    or not isinstance(token.access_token, str) or not token.access_token
                    or not isinstance(token.expires_in, int) or token.expires_in <= 0
                    or token.token_type != "bearer"
                ):
                    raise RuntimeError("Login returned an invalid session token.")
                return token.access_token
            time.sleep(max(0, min(self._interval, deadline - time.monotonic())))
        raise TimeoutError("Browser login timed out; start a new login.")


def begin_login(client: Client) -> BrowserLogin:
    """Start login over HTTPS (HTTP is allowed only for loopback development).

    Arguments:
        client: Public generated SDK Client with redirects disabled and a finite request timeout.
    Returns:
        BrowserLogin; show its authorization_url to the user, then call wait().
    """
    http = client.get_httpx_client()
    origin = urlsplit(str(http.base_url))
    if (
        origin.scheme not in ("http", "https") or not origin.hostname
        or origin.username or origin.password or origin.query or origin.fragment
        or (origin.scheme == "http" and origin.hostname not in ("localhost", "127.0.0.1", "::1"))
    ):
        raise ValueError("Login requires HTTPS, except for loopback development URLs.")
    if http.follow_redirects:
        raise ValueError("Login requires follow_redirects=False to protect the polling secret.")
    if any(
        value is None or not math.isfinite(value)
        for value in (http.timeout.connect, http.timeout.read, http.timeout.write, http.timeout.pool)
    ):
        raise ValueError("Login requires a finite HTTP timeout.")
    started_at = time.monotonic()
    response = _request(lambda: begin_device_login_auth_device_start_post.sync_detailed(client=client))
    if response.status_code != 200:
        raise RuntimeError(f"Login failed (HTTP {response.status_code}); check the API deployment.")
    started = response.parsed
    if not isinstance(started, DeviceLoginStarted):
        raise RuntimeError("Login response does not match the SDK contract.")
    if (
        not isinstance(started.expires_in, int) or not 0 < started.expires_in <= 600
        or not isinstance(started.interval, int) or not 5 <= started.interval <= 60
        or not isinstance(started.device_code, str) or len(started.device_code) != 87
        or not isinstance(started.verification_uri, str)
    ):
        raise RuntimeError("Login returned invalid transaction parameters.")
    browser = urlsplit(started.verification_uri)
    public_code, separator, secret = started.device_code.partition(".")
    if (
        not separator or len(public_code) != 43 or len(secret) != 43
        or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for c in public_code + secret)
        or browser.query != "code=" + public_code
        or (browser.scheme, browser.netloc) != (origin.scheme, origin.netloc)
        or browser.username or browser.password or browser.fragment
    ):
        raise RuntimeError("Login returned an invalid verification URL or transaction code.")
    return BrowserLogin(
        authorization_url=started.verification_uri, _client=client,
        _device_code=started.device_code, _deadline=started_at + started.expires_in,
        _interval=started.interval,
    )
