import secrets
from typing import Annotated
from urllib.parse import urlsplit

from fastapi import APIRouter, Cookie, Depends, Form, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse

from .auth import (
    STATE_COOKIE,
    STATE_TTL_SECONDS,
    SessionToken,
    UserSession,
    consume_oauth_state,
    create_oauth_authorization_redirect,
    get_authenticated_session,
    issue_user_session_from_authorization_code,
    oauth2_scheme,
    revoke_session,
)
from .config import settings
from .models import AuthenticationConfig
from .device_auth import (
    DeviceLoginPoll,
    DeviceLoginRequest,
    DeviceLoginStarted,
    approve_device_login,
    finish_device_login,
    poll_device_login,
    start_device_login,
)

router = APIRouter(prefix="/auth", tags=["authentication"], include_in_schema=False)


def __protect_response(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    provider = urlsplit(settings.hydroshare_oauth_authorize_url)
    response.headers["Content-Security-Policy"] = (
        f"default-src 'none'; form-action 'self' {provider.scheme}://{provider.netloc}; frame-ancestors 'none'"
    )
    response.headers["X-Content-Type-Options"] = "nosniff"


@router.post("/device/start", response_model=DeviceLoginStarted, responses={
    409: {"description": "Authentication disabled"},
    429: {"description": "Login attempt rate exceeded; see Retry-After"},
    503: {"description": "Login store unavailable"},
})
def begin_device_login(request: Request, response: Response) -> DeviceLoginStarted:
    __protect_response(response)
    return start_device_login(request.client.host if request.client else "unknown")


@router.post("/device/poll", response_model=DeviceLoginPoll, responses={
    400: {"description": "Invalid transaction or retrieval secret"},
    410: {"description": "Expired or consumed transaction/session"},
    429: {"description": "Poll interval exceeded; see Retry-After"},
    503: {"description": "Login store unavailable or transaction busy"},
})
def poll_device_session(body: DeviceLoginRequest, response: Response) -> DeviceLoginPoll:
    __protect_response(response)
    return poll_device_login(body.device_code.get_secret_value())


@router.get("/device/verify", response_class=HTMLResponse)
def verify_device_login(code: str) -> HTMLResponse:
    # The public code is validated before inclusion in HTML.
    if len(code) != 43 or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for c in code):
        raise HTTPException(status_code=400, detail="Invalid login transaction")
    csrf = secrets.token_urlsafe(32)
    response = HTMLResponse(
        "<!doctype html><html><head><title>FIMBox SDK login</title></head><body>"
        "<h1>Authorize your FIMBox SDK</h1>"
        "<p>Continue only if you started this login in your own notebook or application. "
        "Approval grants that application access to your FIMBox account. "
        "Do not approve a link sent by someone else.</p>"
        '<form method="post" action="verify">'
        f'<input type="hidden" name="code" value="{code}">'
        f'<input type="hidden" name="csrf" value="{csrf}">'
        '<button type="submit">Continue to HydroShare</button></form></body></html>'
    )
    response.set_cookie(
        "fimbox_device_csrf", csrf, max_age=STATE_TTL_SECONDS, httponly=True,
        secure=settings.oauth_state_cookie_secure, samesite="strict", path="/auth/device",
    )
    __protect_response(response)
    return response


@router.post("/device/verify", response_class=RedirectResponse)
def authorize_device_login(
    code: Annotated[str, Form()],
    csrf: Annotated[str, Form()],
    cookie_csrf: Annotated[str | None, Cookie(alias="fimbox_device_csrf")] = None,
) -> RedirectResponse:
    if not cookie_csrf or not secrets.compare_digest(csrf, cookie_csrf):
        raise HTTPException(status_code=400, detail="Invalid login confirmation")
    approve_device_login(code)
    url, state = create_oauth_authorization_redirect(device_browser_code=code)
    response = RedirectResponse(url, status_code=302)
    response.set_cookie(
        STATE_COOKIE, state, max_age=STATE_TTL_SECONDS, httponly=True,
        secure=settings.oauth_state_cookie_secure, samesite="lax", path="/auth",
    )
    response.delete_cookie("fimbox_device_csrf", path="/auth/device", secure=settings.oauth_state_cookie_secure)
    __protect_response(response)
    return response


@router.get("/config", response_model=AuthenticationConfig)
def authentication_config() -> AuthenticationConfig:
    """Discover this server's HydroShare authentication and output requirements."""
    return AuthenticationConfig(
        authentication_enabled=settings.authentication_enabled,
        require_hydroshare_output=settings.require_hydroshare_output,
    )


@router.get("/login")
def begin_oauth_login(scope: str | None = None) -> RedirectResponse:
    url, state = create_oauth_authorization_redirect(scope)
    response = RedirectResponse(url, status_code=302)
    response.set_cookie(
        STATE_COOKIE, state, max_age=STATE_TTL_SECONDS, httponly=True,
        secure=settings.oauth_state_cookie_secure, samesite="lax", path="/auth",
    )
    return response


@router.get("/callback", response_model=SessionToken, responses={
    200: {
        "description": "Session JSON for manual login; HTML completion for SDK browser transactions",
        "content": {"text/html": {"schema": {"type": "string"}}},
    },
})
def complete_oauth_login(
    response: Response,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    cookie_state: Annotated[str | None, Cookie(alias=STATE_COOKIE)] = None,
) -> SessionToken | HTMLResponse:
    if not error and not code:
        raise HTTPException(status_code=400, detail="Missing authorization code")
    device_browser_code = consume_oauth_state(state or "", cookie_state)
    if error:
        if device_browser_code:
            finish_device_login(device_browser_code, None)
            denied = HTMLResponse("<h1>Authorization denied</h1><p>You may close this tab.</p>")
            denied.delete_cookie(STATE_COOKIE, path="/auth", secure=settings.oauth_state_cookie_secure)
            __protect_response(denied)
            return denied
        raise HTTPException(status_code=400, detail="OAuth authorization denied")
    assert code is not None
    try:
        token = issue_user_session_from_authorization_code(code, settings.hydroshare_oauth_redirect_uri)
    except HTTPException:
        if device_browser_code:
            finish_device_login(device_browser_code, None, failed=True)
        raise
    if device_browser_code:
        try:
            finish_device_login(device_browser_code, token)
        except HTTPException:
            revoke_session(token.access_token)
            raise
        completed = HTMLResponse("<h1>Authorization complete</h1><p>Return to your application. You may close this tab.</p>")
        completed.delete_cookie(STATE_COOKIE, path="/auth", secure=settings.oauth_state_cookie_secure)
        __protect_response(completed)
        return completed
    response.delete_cookie(STATE_COOKIE, path="/auth", secure=settings.oauth_state_cookie_secure)
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    return token


@router.post("/oauth/token", response_model=SessionToken)
def oauth_token_exchange(
    response: Response,
    code: Annotated[str, Form(min_length=1)],
    code_verifier: Annotated[str, Form(min_length=43, max_length=128)],
    grant_type: Annotated[str, Form()] = "authorization_code",
    redirect_uri: Annotated[str | None, Form()] = None,
) -> SessionToken:
    if grant_type != "authorization_code":
        raise HTTPException(status_code=400, detail="Unsupported grant_type")
    token = issue_user_session_from_authorization_code(
        code, redirect_uri or settings.swagger_oauth_redirect_uri, code_verifier,
    )
    response.headers["Cache-Control"] = "no-store"
    return token


@router.get("/session")
def current_session(session: Annotated[UserSession, Depends(get_authenticated_session)]) -> dict:
    return {"user_id": session.user_id, "scope": session.scope, "expires_at": session.expires_at}


@router.post("/logout", status_code=204)
def logout(
    _session: Annotated[UserSession, Depends(get_authenticated_session)],
    token: Annotated[str | None, Depends(oauth2_scheme)],
) -> Response:
    if not settings.authentication_enabled:
        raise HTTPException(status_code=409, detail="Authentication is disabled in local mode")
    if token is None:
        raise HTTPException(status_code=401, detail="Authentication required")
    revoke_session(token)
    return Response(status_code=204)
