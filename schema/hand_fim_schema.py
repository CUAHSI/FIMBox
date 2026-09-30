"""Pydantic models for the HAND-FIM input/output schema.

These models mirror the structures defined in ``schema/schema.md`` and
``schema/schema.json``. Input is grouped by ``ScenarioID``, with each
scenario containing one or more river reach streamflow entries. Each
scenario is processed to produce a single output GeoTIFF; distinct
scenarios each produce their own output GeoTIFF.
"""

from __future__ import annotations

from typing import List

from pydantic import BaseModel, Field, RootModel


class Reach(BaseModel):
    """A single streamflow estimate for a river reach within a watershed."""

    HUC: str = Field(
        ...,
        pattern=r"^[0-9]+$",
        description=(
            "Hydrologic Unit Code identifying the primary watershed "
            "corresponding to the input data. Used to locate the "
            "necessary input HAND and rating curve datasets."
        ),
        examples=["12345678"],
    )
    ReachID: str = Field(
        ...,
        pattern=r"^[0-9]+$",
        description=(
            "NWM identifier for the river reach corresponding to the "
            "input data. Used to process HAND-FIM for the location of "
            "interest."
        ),
        examples=["9821264"],
    )
    Streamflow: float = Field(
        ...,
        ge=0,
        description=(
            "Streamflow value in cubic meters per second (cms) used as "
            "input for the HAND-FIM method. Determines the extent of "
            "flooding in the inundation mapping process."
        ),
        examples=[300.1],
    )

    model_config = {"extra": "forbid"}


class Scenario(BaseModel):
    """A flood inundation mapping scenario.

    Groups one or more reach/streamflow entries that are combined into a
    single output GeoTIFF.
    """

    ScenarioID: str = Field(
        ...,
        min_length=1,
        description=(
            "Unique identifier for the flood inundation mapping "
            "scenario. Also used to define the output file naming."
        ),
        examples=["my-scenario", "scenario1", "scenario2"],
    )
    Reaches: List[Reach] = Field(
        ...,
        min_length=1,
        description="List of river reach streamflow entries belonging to this scenario.",
    )

    model_config = {"extra": "forbid"}


class ScenarioList(RootModel[List[Scenario]]):
    """Top-level input payload: a list of scenarios to process.

    Each scenario produces one output GeoTIFF.
    """

    root: List[Scenario] = Field(..., min_length=1)


class HandFimOutput(BaseModel):
    """Reference to a single generated GeoTIFF output for a scenario."""

    ScenarioID: str
    geotiff_url: str = Field(
        ..., description="Location of the generated GeoTIFF output for this scenario."
    )

    model_config = {"extra": "forbid"}


class HandFimResult(BaseModel):
    """Result of a HAND-FIM request: one output GeoTIFF reference per scenario."""

    outputs: List[HandFimOutput]

    model_config = {"extra": "forbid"}
