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
    client: AuthenticatedClient,
    body: list[Scenario],
    output_bucket: None | str | Unset = UNSET,
    output_prefix: str | Unset = "",
) -> Response[HTTPValidationError | JobSubmitted]:
    """Submit Hand Fim

     Submit the scenario JSON array directly, without an envelope.

    Args:
        output_bucket (None | str | Unset):
        output_prefix (str | Unset):  Default: ''.
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
    )

    response = client.get_httpx_client().request(
        **kwargs,
    )

    return _build_response(client=client, response=response)


def sync(
    *,
    client: AuthenticatedClient,
    body: list[Scenario],
    output_bucket: None | str | Unset = UNSET,
    output_prefix: str | Unset = "",
) -> HTTPValidationError | JobSubmitted | None:
    """Submit Hand Fim

     Submit the scenario JSON array directly, without an envelope.

    Args:
        output_bucket (None | str | Unset):
        output_prefix (str | Unset):  Default: ''.
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
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient,
    body: list[Scenario],
    output_bucket: None | str | Unset = UNSET,
    output_prefix: str | Unset = "",
) -> Response[HTTPValidationError | JobSubmitted]:
    """Submit Hand Fim

     Submit the scenario JSON array directly, without an envelope.

    Args:
        output_bucket (None | str | Unset):
        output_prefix (str | Unset):  Default: ''.
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
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient,
    body: list[Scenario],
    output_bucket: None | str | Unset = UNSET,
    output_prefix: str | Unset = "",
) -> HTTPValidationError | JobSubmitted | None:
    """Submit Hand Fim

     Submit the scenario JSON array directly, without an envelope.

    Args:
        output_bucket (None | str | Unset):
        output_prefix (str | Unset):  Default: ''.
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
        )
    ).parsed
