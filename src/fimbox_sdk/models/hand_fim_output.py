from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

from attrs import define as _attrs_define
from typing_extensions import Self

T = TypeVar("T", bound="HandFimOutput")


@_attrs_define
class HandFimOutput:
    """Reference to a single generated GeoTIFF output for a scenario.

    Attributes:
        scenario_id (str):
        geotiff_url (str): Location of the generated GeoTIFF output for this scenario.
    """

    scenario_id: str
    geotiff_url: str

    def to_dict(self) -> dict[str, Any]:
        scenario_id = self.scenario_id

        geotiff_url = self.geotiff_url

        field_dict: dict[str, Any] = {}

        field_dict.update(
            {
                "ScenarioID": scenario_id,
                "geotiff_url": geotiff_url,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls, src_dict: Mapping[str, Any]) -> Self:
        d = dict(src_dict)
        scenario_id = d.pop("ScenarioID")

        geotiff_url = d.pop("geotiff_url")

        hand_fim_output = cls(
            scenario_id=scenario_id,
            geotiff_url=geotiff_url,
        )

        return hand_fim_output
