"""HydroShare OAuth and Redis sessions adapted from fimbox-api/authentication."""

import hashlib
import json
import logging
import secrets
import time
from typing import Annotated
from urllib.parse import urlencode

import httpx
import redis
from fastapi import Depends, HTTPException
from fastapi.security import OAuth2AuthorizationCodeBearer
from pydantic import BaseModel, Field, SecretStr, ValidationError

from .config import settings

logger = logging.getLogger(__name__)
SESSION_PREFIX = "auth:session:"
USER_JOB_PREFIX = "auth:user_jobs:"
JOB_OWNER_PREFIX = "auth:job_owner:"
AUTH_STATE_PREFIX = "auth:oauth_state:"
STATE_COOKIE = "fimbox_oauth_state"
STATE_TTL_SECONDS = 600
LOCAL_USER_ID = "__fimbox_local__"

oauth2_scheme = OAuth2AuthorizationCodeBearer(
    authorizationUrl=settings.hydroshare_oauth_authorize_url,
    tokenUrl="/auth/oauth/token",
    auto_error=False,
    scopes={scope: scope for scope in settings.hydroshare_scope.split()},
)


class UserSession(BaseModel):
    user_id: str = Field(min_length=1)
    access_token: SecretStr
    refresh_token: SecretStr | None = None
    scope: str
    expires_at: float


class SessionToken(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int
    user_id: str
    scope: str


class HydroShareToken(BaseModel):
    access_token: str = Field(min_length=1)
    refresh_token: str | None = None
    scope: str | None = None


def get_redis_client(*, decode_responses: bool = True) -> redis.Redis:
    return redis.Redis.from_url(settings.redis_url, decode_responses=decode_responses)


def __session_key(token: str) -> str:
    return SESSION_PREFIX + hashlib.sha256(token.encode()).hexdigest()


def __require_oauth_configuration() -> None:
    if not settings.hydroshare_client_id or not settings.hydroshare_client_secret.get_secret_value():
        logger.error("HydroShare OAuth client credentials are not configured")
        raise HTTPException(status_code=503, detail="HydroShare OAuth is not configured")


def create_oauth_authorization_redirect(
    scope: str | None = None, device_browser_code: str | None = None,
) -> tuple[str, str]:
    __require_oauth_configuration()
    state = secrets.token_urlsafe(32)
    requested_scope = scope or settings.hydroshare_scope
    get_redis_client().setex(
        AUTH_STATE_PREFIX + state, STATE_TTL_SECONDS,
        json.dumps({"scope": requested_scope, "device_browser_code": device_browser_code}),
    )
    query = urlencode({
        "response_type": "code",
        "client_id": settings.hydroshare_client_id,
        "redirect_uri": settings.hydroshare_oauth_redirect_uri,
        "scope": requested_scope,
        "state": state,
    })
    separator = "&" if "?" in settings.hydroshare_oauth_authorize_url else "?"
    return f"{settings.hydroshare_oauth_authorize_url}{separator}{query}", state


def issue_user_session_from_authorization_code(
    code: str,
    redirect_uri: str,
    code_verifier: str | None = None,
) -> SessionToken:
    __require_oauth_configuration()
    if redirect_uri not in (
        settings.hydroshare_oauth_redirect_uri,
        settings.swagger_oauth_redirect_uri,
    ):
        raise HTTPException(status_code=400, detail="Unregistered redirect URI")
    payload = {
        "grant_type": "authorization_code",
        "code": code,
        "client_id": settings.hydroshare_client_id,
        "client_secret": settings.hydroshare_client_secret.get_secret_value(),
        "redirect_uri": redirect_uri,
    }
    if code_verifier:
        payload["code_verifier"] = code_verifier
    stage = "token exchange"
    try:
        with httpx.Client(timeout=10, follow_redirects=False) as client:
            response = client.post(settings.hydroshare_oauth_token_url, data=payload)
            response.raise_for_status()
            token = HydroShareToken.model_validate(response.json())
            stage = "user-info lookup"
            response = client.get(
                settings.hydroshare_userinfo_url,
                headers={"Authorization": f"Bearer {token.access_token}"},
            )
            response.raise_for_status()
            userinfo = response.json()
    except httpx.HTTPStatusError as error:
        status_code = error.response.status_code
        # Only standardized OAuth error codes are safe to expose.
        provider_error = None
        try:
            data = error.response.json()
        except ValueError:
            data = None
        if isinstance(data, dict) and data.get("error") in (
            "invalid_request", "invalid_client", "invalid_grant",
            "unauthorized_client", "unsupported_grant_type", "invalid_scope",
            "access_denied", "invalid_token", "insufficient_scope",
        ):
            provider_error = data["error"]
        detail = f"HydroShare {stage} failed (HTTP {status_code})"
        if provider_error:
            detail += f": {provider_error}"
        logger.error("%s", detail)
        raise HTTPException(status_code=502, detail=detail) from None
    except httpx.RequestError as error:
        detail = f"HydroShare {stage} connection failed ({type(error).__name__})"
        logger.error("%s", detail)
        raise HTTPException(status_code=502, detail=detail) from None
    except (ValidationError, ValueError):
        detail = f"HydroShare {stage} returned an invalid response"
        logger.error("%s", detail)
        raise HTTPException(status_code=502, detail=detail) from None
    if not isinstance(userinfo, dict):
        logger.error("HydroShare user lookup returned an invalid response")
        raise HTTPException(status_code=502, detail="Invalid HydroShare user response")
    user_id = userinfo.get("sub") or userinfo.get("username")
    if not isinstance(user_id, str) or not user_id:
        logger.error("HydroShare user lookup returned no authenticated identity")
        raise HTTPException(status_code=502, detail="Unable to determine authenticated user")
    if user_id == LOCAL_USER_ID:
        logger.error("HydroShare user lookup returned a reserved identity")
        raise HTTPException(status_code=502, detail="Invalid HydroShare user identity")

    session_token = secrets.token_urlsafe(32)
    session = UserSession(
        user_id=user_id,
        access_token=SecretStr(token.access_token),
        refresh_token=SecretStr(token.refresh_token) if token.refresh_token else None,
        scope=token.scope or settings.hydroshare_scope,
        expires_at=time.time() + settings.session_ttl_seconds,
    )
    # SecretStr masks serialization; explicitly store provider tokens only in Redis.
    stored = session.model_dump()
    stored["access_token"] = token.access_token
    stored["refresh_token"] = token.refresh_token
    get_redis_client().set(
        __session_key(session_token), json.dumps(stored), ex=settings.session_ttl_seconds,
    )
    return SessionToken(
        access_token=session_token,
        expires_in=settings.session_ttl_seconds,
        user_id=session.user_id,
        scope=session.scope,
    )


def consume_oauth_state(state: str, cookie_state: str | None) -> str | None:
    if not state or not cookie_state or not secrets.compare_digest(state, cookie_state):
        raise HTTPException(status_code=400, detail="Invalid OAuth state")
    stored = get_redis_client().getdel(AUTH_STATE_PREFIX + state)
    if stored is None:
        raise HTTPException(status_code=400, detail="Invalid or expired OAuth state")
    # Existing browser transactions issued before deployment stored only scope.
    if not stored.startswith("{"):
        return None
    return json.loads(stored)["device_browser_code"]


def complete_oauth_authorization(code: str, state: str, cookie_state: str | None) -> SessionToken:
    consume_oauth_state(state, cookie_state)
    return issue_user_session_from_authorization_code(
        code=code, redirect_uri=settings.hydroshare_oauth_redirect_uri,
    )


def get_authenticated_session(token: Annotated[str | None, Depends(oauth2_scheme)]) -> UserSession:
    if not settings.authentication_enabled:
        return UserSession(
            user_id=LOCAL_USER_ID, access_token=SecretStr(""), scope="", expires_at=0,
        )
    if not token:
        raise HTTPException(
            status_code=401, detail="Authentication required", headers={"WWW-Authenticate": "Bearer"},
        )
    client = get_redis_client()
    raw = client.get(__session_key(token))
    if raw is None:
        raise HTTPException(
            status_code=401, detail="Invalid or expired session", headers={"WWW-Authenticate": "Bearer"},
        )
    try:
        session = UserSession.model_validate_json(raw)
    except ValidationError:
        logger.error("Invalid session data in Redis")
        raise HTTPException(status_code=503, detail="Session data unavailable") from None
    if session.user_id == LOCAL_USER_ID:
        raise HTTPException(status_code=401, detail="Invalid or expired session")
    if session.expires_at <= time.time():
        client.delete(__session_key(token))
        raise HTTPException(status_code=401, detail="Session expired", headers={"WWW-Authenticate": "Bearer"})
    return session


def revoke_session(token: str) -> None:
    get_redis_client().delete(__session_key(token))


def track_user_job(user_id: str, job_id: str) -> None:
    with get_redis_client().pipeline(transaction=True) as pipeline:
        pipeline.set(JOB_OWNER_PREFIX + job_id, user_id, ex=settings.result_ttl_seconds)
        pipeline.zadd(USER_JOB_PREFIX + user_id, {job_id: time.time() + settings.result_ttl_seconds})
        pipeline.expire(USER_JOB_PREFIX + user_id, settings.result_ttl_seconds)
        pipeline.execute()


def untrack_user_job(user_id: str, job_id: str) -> None:
    with get_redis_client().pipeline(transaction=True) as pipeline:
        pipeline.delete(JOB_OWNER_PREFIX + job_id)
        pipeline.zrem(USER_JOB_PREFIX + user_id, job_id)
        pipeline.execute()


def user_has_job(user_id: str, job_id: str) -> bool:
    return get_redis_client().get(JOB_OWNER_PREFIX + job_id) == user_id


def list_user_jobs(user_id: str) -> list[str]:
    client = get_redis_client()
    client.zremrangebyscore(USER_JOB_PREFIX + user_id, "-inf", time.time())
    return client.zrange(USER_JOB_PREFIX + user_id, 0, -1)
