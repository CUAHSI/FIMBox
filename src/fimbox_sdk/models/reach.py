from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

from attrs import define as _attrs_define
from typing_extensions import Self

T = TypeVar("T", bound="Reach")


@_attrs_define
class Reach:
    """A single streamflow estimate for a river reach within a watershed.

    Attributes:
        huc (str): Hydrologic Unit Code identifying the primary watershed corresponding to the input data. Used to
            locate the necessary input HAND and rating curve datasets.
        reach_id (str): NWM identifier for the river reach corresponding to the input data. Used to process HAND-FIM for
            the location of interest.
        streamflow (float): Streamflow value in cubic meters per second (cms) used as input for the HAND-FIM method.
            Determines the extent of flooding in the inundation mapping process.
    """

    huc: str
    reach_id: str
    streamflow: float

    def to_dict(self) -> dict[str, Any]:
        huc = self.huc

        reach_id = self.reach_id

        streamflow = self.streamflow

        field_dict: dict[str, Any] = {}

        field_dict.update(
            {
                "HUC": huc,
                "ReachID": reach_id,
                "Streamflow": streamflow,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls, src_dict: Mapping[str, Any]) -> Self:
        d = dict(src_dict)
        huc = d.pop("HUC")

        reach_id = d.pop("ReachID")

        streamflow = d.pop("Streamflow")

        reach = cls(
            huc=huc,
            reach_id=reach_id,
            streamflow=streamflow,
        )

        return reach
