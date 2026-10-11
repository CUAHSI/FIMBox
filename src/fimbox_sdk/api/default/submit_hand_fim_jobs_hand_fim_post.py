from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.http_validation_error import HTTPValidationError
from ...models.job_submitted import JobSubmitted
from ...models.scenario import Scenario
from ...types import UNSET, Response, Unset


def _get_kwargs(
    *,
    body: list[Scenario],
    output_bucket: None | str | Unset = UNSET,
    output_prefix: str | Unset = "",
    max_procs: int | None | Unset = UNSET,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    params: dict[str, Any] = {}

    json_output_bucket: None | str | Unset
    if isinstance(output_bucket, Unset):
        json_output_bucket = UNSET
    else:
        json_output_bucket = output_bucket
    params["output_bucket"] = json_output_bucket

    params["output_prefix"] = output_prefix

    json_max_procs: int | None | Unset
    if isinstance(max_procs, Unset):
        json_max_procs = UNSET
    else:
        json_max_procs = max_procs
    params["max_procs"] = json_max_procs

    params = {k: v for k, v in params.items() if v is not UNSET and v is not None}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/jobs/hand-fim",
        "params": params,
    }

    _kwargs["json"] = []
    for componentsschemas_hand_fim_parameters_item_data in body:
        componentsschemas_hand_fim_parameters_item = (
            componentsschemas_hand_fim_parameters_item_data.to_dict()
        )
        _kwargs["json"].append(componentsschemas_hand_fim_parameters_item)

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> HTTPValidationError | JobSubmitted | None:
    if response.status_code == 202:
        response_202 = JobSubmitted.from_dict(response.json())

        return response_202

    if response.status_code == 422:
        response_422 = HTTPValidationError.from_dict(response.json())

        return response_422

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Response[HTTPValidationError | JobSubmitted]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: list[Scenario],
    output_bucket: None | str | Unset = UNSET,
    output_prefix: str | Unset = "",
    max_procs: int | None | Unset = UNSET,
) -> Response[HTTPValidationError | JobSubmitted]:
    """Submit Hand Fim

     Submit the scenario JSON array directly, without an envelope.
    Authentication is required only when the server has OAuth credentials configured. See GET
    /auth/config.

    Args:
        output_bucket (None | str | Unset):
        output_prefix (str | Unset):  Default: ''.
        max_procs (int | None | Unset): Max concurrent HAND-FIM processes; omit to use the
            worker's HAND_FIM_MAX_PROCS (default 1).
        body (list[Scenario]): The shared scenario JSON schema with worker-specific validation.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[HTTPValidationError | JobSubmitted]
    """

    kwargs = _get_kwargs(
        body=body,
        output_bucket=output_bucket,
        output_prefix=output_prefix,
        max_procs=max_procs,
    )

    response = client.get_httpx_client().request(
        **kwargs,
    )

    return _build_response(client=client, response=response)


def sync(
    *,
    client: AuthenticatedClient | Client,
    body: list[Scenario],
    output_bucket: None | str | Unset = UNSET,
    output_prefix: str | Unset = "",
    max_procs: int | None | Unset = UNSET,
) -> HTTPValidationError | JobSubmitted | None:
    """Submit Hand Fim

     Submit the scenario JSON array directly, without an envelope.
    Authentication is required only when the server has OAuth credentials configured. See GET
    /auth/config.

    Args:
        output_bucket (None | str | Unset):
        output_prefix (str | Unset):  Default: ''.
        max_procs (int | None | Unset): Max concurrent HAND-FIM processes; omit to use the
            worker's HAND_FIM_MAX_PROCS (default 1).
        body (list[Scenario]): The shared scenario JSON schema with worker-specific validation.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        HTTPValidationError | JobSubmitted
    """

    return sync_detailed(
        client=client,
        body=body,
        output_bucket=output_bucket,
        output_prefix=output_prefix,
        max_procs=max_procs,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: list[Scenario],
    output_bucket: None | str | Unset = UNSET,
    output_prefix: str | Unset = "",
    max_procs: int | None | Unset = UNSET,
) -> Response[HTTPValidationError | JobSubmitted]:
    """Submit Hand Fim

     Submit the scenario JSON array directly, without an envelope.
    Authentication is required only when the server has OAuth credentials configured. See GET
    /auth/config.

    Args:
        output_bucket (None | str | Unset):
        output_prefix (str | Unset):  Default: ''.
        max_procs (int | None | Unset): Max concurrent HAND-FIM processes; omit to use the
            worker's HAND_FIM_MAX_PROCS (default 1).
        body (list[Scenario]): The shared scenario JSON schema with worker-specific validation.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[HTTPValidationError | JobSubmitted]
    """

    kwargs = _get_kwargs(
        body=body,
        output_bucket=output_bucket,
        output_prefix=output_prefix,
        max_procs=max_procs,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient | Client,
    body: list[Scenario],
    output_bucket: None | str | Unset = UNSET,
    output_prefix: str | Unset = "",
    max_procs: int | None | Unset = UNSET,
) -> HTTPValidationError | JobSubmitted | None:
    """Submit Hand Fim

     Submit the scenario JSON array directly, without an envelope.
    Authentication is required only when the server has OAuth credentials configured. See GET
    /auth/config.

    Args:
        output_bucket (None | str | Unset):
        output_prefix (str | Unset):  Default: ''.
        max_procs (int | None | Unset): Max concurrent HAND-FIM processes; omit to use the
            worker's HAND_FIM_MAX_PROCS (default 1).
        body (list[Scenario]): The shared scenario JSON schema with worker-specific validation.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        HTTPValidationError | JobSubmitted
    """

    return (
        await asyncio_detailed(
            client=client,
            body=body,
            output_bucket=output_bucket,
            output_prefix=output_prefix,
            max_procs=max_procs,
        )
    ).parsed
