from typing import Annotated

from fastapi import APIRouter, Cookie, Depends, Form, HTTPException, Response
from fastapi.responses import RedirectResponse

from .auth import (
    STATE_COOKIE,
    STATE_TTL_SECONDS,
    SessionToken,
    UserSession,
    complete_oauth_authorization,
    create_oauth_authorization_redirect,
    get_authenticated_session,
    issue_user_session_from_authorization_code,
    oauth2_scheme,
    revoke_session,
)
from .config import settings
from .models import AuthenticationConfig

router = APIRouter(prefix="/auth", tags=["authentication"])


@router.get("/config", response_model=AuthenticationConfig)
def authentication_config() -> AuthenticationConfig:
    """Discover whether this server requires HydroShare authentication."""
    return AuthenticationConfig(authentication_enabled=settings.authentication_enabled)


@router.get("/login")
def begin_oauth_login(scope: str | None = None) -> RedirectResponse:
    url, state = create_oauth_authorization_redirect(scope)
    response = RedirectResponse(url, status_code=302)
    response.set_cookie(
        STATE_COOKIE, state, max_age=STATE_TTL_SECONDS, httponly=True,
        secure=settings.oauth_state_cookie_secure, samesite="lax", path="/auth",
    )
    return response


@router.get("/callback", response_model=SessionToken)
def complete_oauth_login(
    response: Response,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    cookie_state: Annotated[str | None, Cookie(alias=STATE_COOKIE)] = None,
) -> SessionToken:
    if error:
        raise HTTPException(status_code=400, detail="OAuth authorization denied")
    if not code:
        raise HTTPException(status_code=400, detail="Missing authorization code")
    token = complete_oauth_authorization(code, state or "", cookie_state)
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
