from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.http_validation_error import HTTPValidationError
from ...models.session_token import SessionToken
from ...types import UNSET, Response, Unset


def _get_kwargs(
    *,
    code: None | str | Unset = UNSET,
    state: None | str | Unset = UNSET,
    error: None | str | Unset = UNSET,
    fimbox_oauth_state: None | str | Unset = UNSET,
) -> dict[str, Any]:

    cookies = {}
    if fimbox_oauth_state is not UNSET:
        cookies["fimbox_oauth_state"] = fimbox_oauth_state

    params: dict[str, Any] = {}

    json_code: None | str | Unset
    if isinstance(code, Unset):
        json_code = UNSET
    else:
        json_code = code
    params["code"] = json_code

    json_state: None | str | Unset
    if isinstance(state, Unset):
        json_state = UNSET
    else:
        json_state = state
    params["state"] = json_state

    json_error: None | str | Unset
    if isinstance(error, Unset):
        json_error = UNSET
    else:
        json_error = error
    params["error"] = json_error

    params = {k: v for k, v in params.items() if v is not UNSET and v is not None}

    _kwargs: dict[str, Any] = {
        "method": "get",
        "url": "/auth/callback",
        "params": params,
        "cookies": cookies,
    }

    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> HTTPValidationError | SessionToken | None:
    if response.status_code == 200:
        response_200 = SessionToken.from_dict(response.json())

        return response_200

    if response.status_code == 422:
        response_422 = HTTPValidationError.from_dict(response.json())

        return response_422

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Response[HTTPValidationError | SessionToken]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient | Client,
    code: None | str | Unset = UNSET,
    state: None | str | Unset = UNSET,
    error: None | str | Unset = UNSET,
    fimbox_oauth_state: None | str | Unset = UNSET,
) -> Response[HTTPValidationError | SessionToken]:
    """Complete Oauth Login

    Args:
        code (None | str | Unset):
        state (None | str | Unset):
        error (None | str | Unset):
        fimbox_oauth_state (None | str | Unset):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[HTTPValidationError | SessionToken]
    """

    kwargs = _get_kwargs(
        code=code,
        state=state,
        error=error,
        fimbox_oauth_state=fimbox_oauth_state,
    )

    response = client.get_httpx_client().request(
        **kwargs,
    )

    return _build_response(client=client, response=response)


def sync(
    *,
    client: AuthenticatedClient | Client,
    code: None | str | Unset = UNSET,
    state: None | str | Unset = UNSET,
    error: None | str | Unset = UNSET,
    fimbox_oauth_state: None | str | Unset = UNSET,
) -> HTTPValidationError | SessionToken | None:
    """Complete Oauth Login

    Args:
        code (None | str | Unset):
        state (None | str | Unset):
        error (None | str | Unset):
        fimbox_oauth_state (None | str | Unset):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        HTTPValidationError | SessionToken
    """

    return sync_detailed(
        client=client,
        code=code,
        state=state,
        error=error,
        fimbox_oauth_state=fimbox_oauth_state,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient | Client,
    code: None | str | Unset = UNSET,
    state: None | str | Unset = UNSET,
    error: None | str | Unset = UNSET,
    fimbox_oauth_state: None | str | Unset = UNSET,
) -> Response[HTTPValidationError | SessionToken]:
    """Complete Oauth Login

    Args:
        code (None | str | Unset):
        state (None | str | Unset):
        error (None | str | Unset):
        fimbox_oauth_state (None | str | Unset):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[HTTPValidationError | SessionToken]
    """

    kwargs = _get_kwargs(
        code=code,
        state=state,
        error=error,
        fimbox_oauth_state=fimbox_oauth_state,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient | Client,
    code: None | str | Unset = UNSET,
    state: None | str | Unset = UNSET,
    error: None | str | Unset = UNSET,
    fimbox_oauth_state: None | str | Unset = UNSET,
) -> HTTPValidationError | SessionToken | None:
    """Complete Oauth Login

    Args:
        code (None | str | Unset):
        state (None | str | Unset):
        error (None | str | Unset):
        fimbox_oauth_state (None | str | Unset):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        HTTPValidationError | SessionToken
    """

    return (
        await asyncio_detailed(
            client=client,
            code=code,
            state=state,
            error=error,
            fimbox_oauth_state=fimbox_oauth_state,
        )
    ).parsed
