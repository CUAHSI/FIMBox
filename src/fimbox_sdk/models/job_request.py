from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from typing_extensions import Self

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.scenario import Scenario


T = TypeVar("T", bound="JobRequest")


@_attrs_define
class JobRequest:
    """
    Attributes:
        payload (list[Scenario]): The shared scenario JSON schema with worker-specific validation.
        output_bucket (None | str | Unset):
        output_prefix (str | Unset):  Default: ''.
    """

    payload: list[Scenario]
    output_bucket: None | str | Unset = UNSET
    output_prefix: str | Unset = ""

    def to_dict(self) -> dict[str, Any]:
        payload = []
        for componentsschemas_hand_fim_parameters_item_data in self.payload:
            componentsschemas_hand_fim_parameters_item = (
                componentsschemas_hand_fim_parameters_item_data.to_dict()
            )
            payload.append(componentsschemas_hand_fim_parameters_item)

        output_bucket: None | str | Unset
        if isinstance(self.output_bucket, Unset):
            output_bucket = UNSET
        else:
            output_bucket = self.output_bucket

        output_prefix = self.output_prefix

        field_dict: dict[str, Any] = {}

        field_dict.update(
            {
                "payload": payload,
            }
        )
        if output_bucket is not UNSET:
            field_dict["output_bucket"] = output_bucket
        if output_prefix is not UNSET:
            field_dict["output_prefix"] = output_prefix

        return field_dict

    @classmethod
    def from_dict(cls, src_dict: Mapping[str, Any]) -> Self:
        from ..models.scenario import Scenario

        d = dict(src_dict)
        payload = []
        _payload = d.pop("payload")
        for componentsschemas_hand_fim_parameters_item_data in _payload:
            componentsschemas_hand_fim_parameters_item = Scenario.from_dict(
                componentsschemas_hand_fim_parameters_item_data
            )

            payload.append(componentsschemas_hand_fim_parameters_item)

        def _parse_output_bucket(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        output_bucket = _parse_output_bucket(d.pop("output_bucket", UNSET))

        output_prefix = d.pop("output_prefix", UNSET)

        job_request = cls(
            payload=payload,
            output_bucket=output_bucket,
            output_prefix=output_prefix,
        )

        return job_request
