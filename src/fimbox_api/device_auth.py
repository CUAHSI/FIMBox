"""Short-lived, browser-approved session handoff for SDK clients."""

import hashlib
import logging
import math
import secrets
import time
from collections.abc import Callable
from typing import Literal, TypeVar
from urllib.parse import urlsplit, urlunsplit

from fastapi import HTTPException
from pydantic import BaseModel, Field, SecretStr
from redis.exceptions import WatchError

from .auth import SessionToken, get_redis_client
from .config import settings

logger = logging.getLogger(__name__)
DEVICE_PREFIX = "auth:device:"
DEVICE_TTL_SECONDS = 600
POLL_INTERVAL_SECONDS = 5
T = TypeVar("T")


class DeviceLoginStarted(BaseModel):
    device_code: str
    verification_uri: str
    expires_in: int
    interval: int


class DeviceLoginRequest(BaseModel):
    device_code: SecretStr = Field(min_length=87, max_length=87)


class DeviceLoginPoll(BaseModel):
    status: Literal["pending", "authorized", "denied", "failed"]
    token: SessionToken | None = None


class DeviceRecord(BaseModel):
    secret_hash: str
    expires_at: float
    status: Literal["pending", "authorizing", "authorized", "denied", "failed", "consumed"] = "pending"
    last_poll: float = 0
    token: SessionToken | None = None
    token_expires_at: float | None = None


def __update_device(browser_code: str, update: Callable[[DeviceRecord], T]) -> T:
    if len(browser_code) != 43 or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for c in browser_code):
        raise HTTPException(status_code=400, detail="Invalid login transaction")
    key = DEVICE_PREFIX + browser_code
    with get_redis_client().pipeline() as pipe:
        for _ in range(5):
            try:
                pipe.watch(key)
                raw = pipe.get(key)
                if raw is None:
                    raise HTTPException(status_code=410, detail="Login transaction expired")
                record = DeviceRecord.model_validate_json(raw)
                if record.expires_at <= time.time():
                    raise HTTPException(status_code=410, detail="Login transaction expired")
                result = update(record)
                pipe.multi()
                pipe.set(key, record.model_dump_json(), keepttl=True)
                pipe.execute()
                return result
            except WatchError:
                continue
    logger.error("Concurrent login transaction updates exceeded retry limit")
    raise HTTPException(status_code=503, detail="Login transaction busy; start a new login")


def start_device_login(remote_address: str) -> DeviceLoginStarted:
    if not settings.authentication_enabled:
        raise HTTPException(status_code=409, detail="Authentication is disabled in local mode")
    client = get_redis_client()
    rate_key = "auth:device_rate:" + hashlib.sha256(remote_address.encode()).hexdigest()
    with client.pipeline() as pipe:
        pipe.incr(rate_key)
        pipe.expire(rate_key, 60)
        count = pipe.execute()[0]
    if count > 30:
        raise HTTPException(status_code=429, detail="Too many login attempts", headers={"Retry-After": "60"})
    browser_code = secrets.token_urlsafe(32)
    secret = secrets.token_urlsafe(32)
    record = DeviceRecord(
        secret_hash=hashlib.sha256(secret.encode()).hexdigest(),
        expires_at=time.time() + DEVICE_TTL_SECONDS,
    )
    client.set(DEVICE_PREFIX + browser_code, record.model_dump_json(), ex=DEVICE_TTL_SECONDS)
    # Use the configured callback origin, not an untrusted request Host header.
    callback = urlsplit(settings.hydroshare_oauth_redirect_uri)
    path = callback.path.rsplit("/", 1)[0] + "/device/verify"
    verification_uri = urlunsplit((callback.scheme, callback.netloc, path, "code=" + browser_code, ""))
    return DeviceLoginStarted(
        device_code=browser_code + "." + secret, verification_uri=verification_uri,
        expires_in=DEVICE_TTL_SECONDS, interval=POLL_INTERVAL_SECONDS,
    )


def approve_device_login(browser_code: str) -> None:
    def approve(record: DeviceRecord) -> None:
        if record.status != "pending":
            raise HTTPException(status_code=409, detail="Login transaction already started")
        record.status = "authorizing"

    __update_device(browser_code, approve)


def finish_device_login(browser_code: str, token: SessionToken | None, *, failed: bool = False) -> None:
    def finish(record: DeviceRecord) -> None:
        if record.status != "authorizing":
            raise HTTPException(status_code=409, detail="Login transaction already completed")
        record.status = "failed" if failed else ("authorized" if token is not None else "denied")
        record.token = token
        record.token_expires_at = time.time() + token.expires_in if token else None

    __update_device(browser_code, finish)


def poll_device_login(device_code: str) -> DeviceLoginPoll:
    browser_code, separator, secret = device_code.partition(".")
    if not separator or len(secret) != 43:
        raise HTTPException(status_code=400, detail="Invalid login transaction")

    def poll(record: DeviceRecord) -> DeviceLoginPoll:
        if not secrets.compare_digest(record.secret_hash, hashlib.sha256(secret.encode()).hexdigest()):
            raise HTTPException(status_code=400, detail="Invalid login transaction")
        if record.status == "consumed":
            raise HTTPException(status_code=410, detail="Login transaction already consumed")
        now = time.time()
        if now - record.last_poll < POLL_INTERVAL_SECONDS:
            raise HTTPException(
                status_code=429, detail="Poll interval exceeded",
                headers={"Retry-After": str(POLL_INTERVAL_SECONDS)},
            )
        record.last_poll = now
        if record.status == "authorized":
            assert record.token is not None and record.token_expires_at is not None
            remaining = record.token_expires_at - now
            if remaining <= 0:
                raise HTTPException(status_code=410, detail="Authorized session expired")
            token = record.token.model_copy(update={"expires_in": math.ceil(remaining)})
            record.token = None
            record.status = "consumed"
            return DeviceLoginPoll(status="authorized", token=token)
        if record.status == "denied":
            record.status = "consumed"
            return DeviceLoginPoll(status="denied")
        if record.status == "failed":
            record.status = "consumed"
            return DeviceLoginPoll(status="failed")
        return DeviceLoginPoll(status="pending")

    return __update_device(browser_code, poll)
