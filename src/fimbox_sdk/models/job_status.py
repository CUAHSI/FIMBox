from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field
from typing_extensions import Self

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.stored_hand_fim_result import StoredHandFimResult


T = TypeVar("T", bound="JobStatus")


@_attrs_define
class JobStatus:
    """
    Attributes:
        job_id (str):
        status (str):
        result (None | StoredHandFimResult | Unset):
        error (None | str | Unset):
    """

    job_id: str
    status: str
    result: None | StoredHandFimResult | Unset = UNSET
    error: None | str | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        from ..models.stored_hand_fim_result import StoredHandFimResult

        job_id = self.job_id

        status = self.status

        result: dict[str, Any] | None | Unset
        if isinstance(self.result, Unset):
            result = UNSET
        elif isinstance(self.result, StoredHandFimResult):
            result = self.result.to_dict()
        else:
            result = self.result

        error: None | str | Unset
        if isinstance(self.error, Unset):
            error = UNSET
        else:
            error = self.error

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "job_id": job_id,
                "status": status,
            }
        )
        if result is not UNSET:
            field_dict["result"] = result
        if error is not UNSET:
            field_dict["error"] = error

        return field_dict

    @classmethod
    def from_dict(cls, src_dict: Mapping[str, Any]) -> Self:
        from ..models.stored_hand_fim_result import StoredHandFimResult

        d = dict(src_dict)
        job_id = d.pop("job_id")

        status = d.pop("status")

        def _parse_result(data: object) -> None | StoredHandFimResult | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                result_type_0 = StoredHandFimResult.from_dict(data)

                return result_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | StoredHandFimResult | Unset, data)

        result = _parse_result(d.pop("result", UNSET))

        def _parse_error(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        error = _parse_error(d.pop("error", UNSET))

        job_status = cls(
            job_id=job_id,
            status=status,
            result=result,
            error=error,
        )

        job_status.additional_properties = d
        return job_status

    @property
    def additional_keys(self) -> list[str]:
        return list(self.additional_properties.keys())

    def __getitem__(self, key: str) -> Any:
        return self.additional_properties[key]

    def __setitem__(self, key: str, value: Any) -> None:
        self.additional_properties[key] = value

    def __delitem__(self, key: str) -> None:
        del self.additional_properties[key]

    def __contains__(self, key: str) -> bool:
        return key in self.additional_properties
