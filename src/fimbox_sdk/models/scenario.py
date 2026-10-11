from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from attrs import define as _attrs_define
from typing_extensions import Self

if TYPE_CHECKING:
    from ..models.reach import Reach


T = TypeVar("T", bound="Scenario")


@_attrs_define
class Scenario:
    """A flood inundation mapping scenario.

    Groups one or more reach/streamflow entries that are combined into a
    single output GeoTIFF.

        Attributes:
            scenario_id (str): Unique identifier for the flood inundation mapping scenario. Also used to define the output
                file naming.
            reaches (list[Reach]): List of river reach streamflow entries belonging to this scenario.
    """

    scenario_id: str
    reaches: list[Reach]

    def to_dict(self) -> dict[str, Any]:
        scenario_id = self.scenario_id

        reaches = []
        for reaches_item_data in self.reaches:
            reaches_item = reaches_item_data.to_dict()
            reaches.append(reaches_item)

        field_dict: dict[str, Any] = {}

        field_dict.update(
            {
                "ScenarioID": scenario_id,
                "Reaches": reaches,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls, src_dict: Mapping[str, Any]) -> Self:
        from ..models.reach import Reach

        d = dict(src_dict)
        scenario_id = d.pop("ScenarioID")

        reaches = []
        _reaches = d.pop("Reaches")
        for reaches_item_data in _reaches:
            reaches_item = Reach.from_dict(reaches_item_data)

            reaches.append(reaches_item)

        scenario = cls(
            scenario_id=scenario_id,
            reaches=reaches,
        )

        return scenario
