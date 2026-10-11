from http import HTTPStatus
from io import BytesIO
from typing import Any
from urllib.parse import quote
from uuid import UUID

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.http_validation_error import HTTPValidationError
from ...types import File, Response


def _get_kwargs(
    job_id: UUID,
    index: int,
) -> dict[str, Any]:

    _kwargs: dict[str, Any] = {
        "method": "get",
        "url": "/jobs/{job_id}/files/{index}/content".format(
            job_id=quote(str(job_id), safe=""),
            index=quote(str(index), safe=""),
        ),
    }

    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> File | HTTPValidationError | None:
    if response.status_code == 200:
        response_200 = File(payload=BytesIO(response.content))

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
) -> Response[File | HTTPValidationError]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    job_id: UUID,
    index: int,
    *,
    client: AuthenticatedClient | Client,
) -> Response[File | HTTPValidationError]:
    """Get Local Job File

     Download a local output file; requires the owner's session when auth is enabled.
    Authentication is required only when the server has OAuth credentials configured. See GET
    /auth/config.

    Args:
        job_id (UUID):
        index (int):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[File | HTTPValidationError]
    """

    kwargs = _get_kwargs(
        job_id=job_id,
        index=index,
    )

    response = client.get_httpx_client().request(
        **kwargs,
    )

    return _build_response(client=client, response=response)


def sync(
    job_id: UUID,
    index: int,
    *,
    client: AuthenticatedClient | Client,
) -> File | HTTPValidationError | None:
    """Get Local Job File

     Download a local output file; requires the owner's session when auth is enabled.
    Authentication is required only when the server has OAuth credentials configured. See GET
    /auth/config.

    Args:
        job_id (UUID):
        index (int):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        File | HTTPValidationError
    """

    return sync_detailed(
        job_id=job_id,
        index=index,
        client=client,
    ).parsed


async def asyncio_detailed(
    job_id: UUID,
    index: int,
    *,
    client: AuthenticatedClient | Client,
) -> Response[File | HTTPValidationError]:
    """Get Local Job File

     Download a local output file; requires the owner's session when auth is enabled.
    Authentication is required only when the server has OAuth credentials configured. See GET
    /auth/config.

    Args:
        job_id (UUID):
        index (int):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[File | HTTPValidationError]
    """

    kwargs = _get_kwargs(
        job_id=job_id,
        index=index,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    job_id: UUID,
    index: int,
    *,
    client: AuthenticatedClient | Client,
) -> File | HTTPValidationError | None:
    """Get Local Job File

     Download a local output file; requires the owner's session when auth is enabled.
    Authentication is required only when the server has OAuth credentials configured. See GET
    /auth/config.

    Args:
        job_id (UUID):
        index (int):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        File | HTTPValidationError
    """

    return (
        await asyncio_detailed(
            job_id=job_id,
            index=index,
            client=client,
        )
    ).parsed
