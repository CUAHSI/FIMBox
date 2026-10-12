from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

from attrs import define as _attrs_define
from typing_extensions import Self

T = TypeVar("T", bound="HandFimOutput")


@_attrs_define
class HandFimOutput:
    """Metadata for a single generated scenario output.

    Attributes:
        scenario_id (str):
    """

    scenario_id: str

    def to_dict(self) -> dict[str, Any]:
        scenario_id = self.scenario_id

        field_dict: dict[str, Any] = {}

        field_dict.update(
            {
                "ScenarioID": scenario_id,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls, src_dict: Mapping[str, Any]) -> Self:
        d = dict(src_dict)
        scenario_id = d.pop("ScenarioID")

        hand_fim_output = cls(
            scenario_id=scenario_id,
        )

        return hand_fim_output
