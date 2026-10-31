import math
import re
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from schemas.hand_fim_schema import HandFimResult, ScenarioList


class HandFimParameters(ScenarioList):
    """The shared scenario JSON schema with worker-specific validation."""

    @model_validator(mode="after")
    def validate_worker_inputs(self) -> "HandFimParameters":
        identifiers = [scenario.ScenarioID for scenario in self.root]
        if len(set(identifiers)) != len(identifiers):
            raise ValueError("ScenarioID values must be unique within a job")
        for scenario in self.root:
            if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", scenario.ScenarioID) is None:
                raise ValueError("ScenarioID must be a safe filename using letters, digits, '.', '_' or '-'")
            for reach in scenario.Reaches:
                if len(reach.HUC) != 8:
                    raise ValueError("HAND-FIM requires eight-digit HUC identifiers")
                if not math.isfinite(reach.Streamflow):
                    raise ValueError("Streamflow must be finite")
        return self


class OutputDestination(BaseModel):
    output_bucket: str | None = Field(
        default="resource", min_length=1, max_length=255, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$",
    )
    output_prefix: str = Field(max_length=900)

    model_config = {"extra": "forbid"}

    @model_validator(mode="after")
    def validate_destination(self) -> "OutputDestination":
        if self.output_bucket is None and self.output_prefix:
            raise ValueError("output_prefix requires output_bucket")
        return self

    @field_validator("output_prefix")
    @classmethod
    def validate_prefix_size(cls, value: str) -> str:
        if len(value.encode("utf-8")) > 900:
            raise ValueError("Output prefix must not exceed 900 UTF-8 bytes")
        return value


StreamingMode = Literal["streaming", "updatedstreaming"]
ExecutionMode = Literal["legacy", "updatedlegacy", "streaming", "updatedstreaming"]
STREAMING_MODES = ("streaming", "updatedstreaming")


class HandFimJobOptions(OutputDestination):
    max_procs: int | None = Field(
        default=None, ge=1,
        description="Max concurrent HAND-FIM processes; omit to use the worker's HAND_FIM_MAX_PROCS (default 1).",
    )
    overwrite: bool = Field(
        default=False,
        description="Replace existing S3 objects with matching output keys; by default, conflicting keys fail the job.",
    )
    execution_mode: ExecutionMode = Field(
        default="legacy",
        description=(
            "legacy downloads whole HUCs and writes one COG per scenario. updatedlegacy uses the same "
            "workflow with NOAA v4.8.14.0 instead of the original pinned code. streaming range-reads a prepared "
            "cloud-optimized hydrofabric per reach and writes tiled COGs plus a manifest per scenario; "
            "it requires output_bucket and a single process. updatedstreaming is streaming with NOAA "
            "v4.8.14.0's own flood kernel, matching updatedlegacy."
        ),
    )

    @property
    def streamed(self) -> bool:
        return self.execution_mode in STREAMING_MODES

    @model_validator(mode="after")
    def validate_execution_mode(self) -> "HandFimJobOptions":
        if self.streamed:
            if self.output_bucket is None:
                raise ValueError(f"{self.execution_mode} execution_mode requires output_bucket")
            if self.max_procs not in (None, 1):
                raise ValueError(f"{self.execution_mode} execution_mode runs one process; omit max_procs or use 1")
        return self


class HandFimJobRequest(HandFimJobOptions):
    scenarios: HandFimParameters


class JobSubmitted(BaseModel):
    job_id: str
    status: str
    status_url: str


class AuthenticationConfig(BaseModel):
    authentication_enabled: bool
    require_hydroshare_output: bool


class OutputFile(BaseModel):
    path: str
    key: str | None = None
    size_bytes: int


class ScenarioManifest(BaseModel):
    """A published per-scenario tile manifest from a streaming job."""

    ScenarioID: str
    key: str
    tile_count: int = Field(ge=1)
    bounds: tuple[float, float, float, float]
    resolution: float = Field(gt=0)
    tile_size: int = Field(gt=0)


class ParityJobRequest(OutputDestination):
    """Rerun a retained legacy run's scenarios with streaming and diff the outputs."""

    legacy_bucket: str = Field(min_length=1, max_length=255, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
    legacy_prefix: str = Field(
        max_length=900,
        description="Output prefix of a legacy run containing scenarios.json and <ScenarioID>.cog files.",
    )
    overwrite: bool = Field(
        default=False,
        description="Replace existing S3 objects with matching output keys; by default, conflicting keys fail the job.",
    )
    execution_mode: StreamingMode = Field(
        default="streaming",
        description="Streaming mode to rerun; use updatedstreaming to compare against an updatedlegacy run.",
    )

    @model_validator(mode="after")
    def validate_parity_destination(self) -> "ParityJobRequest":
        if self.output_bucket is None:
            raise ValueError("parity comparison requires output_bucket")
        if any(part in {".", ".."} for part in self.legacy_prefix.split("/")):
            raise ValueError("legacy_prefix must not contain '.' or '..' segments")
        if (self.legacy_bucket, self.legacy_prefix.strip("/")) == (self.output_bucket, self.output_prefix.strip("/")):
            raise ValueError("output_prefix must differ from the legacy run's prefix")
        return self


class ParityDifference(BaseModel):
    x: float
    y: float
    value: Literal[1, 2] = Field(description="1=legacy-only flooding, 2=streaming-only flooding")


class ScenarioParity(BaseModel):
    """Cell-level comparison of one legacy scenario COG against its streaming tiles.

    Cell counts are measured after realigning a small whole-pixel grid shift, which
    is reported separately and does not affect ``matches``.
    """

    ScenarioID: str
    legacy_key: str
    diff_key: str
    crs: str
    legacy_flooded_cells: int
    streaming_flooded_cells: int
    legacy_only_cells: int
    streaming_only_cells: int
    different_cells: int
    grid_shift_rows: int = Field(
        default=0, description="Streaming rows offset from legacy (positive is south); realigned before counting.",
    )
    grid_shift_columns: int = Field(
        default=0, description="Streaming columns offset from legacy (positive is east); realigned before counting.",
    )
    unaligned_different_cells: int = Field(
        default=0, description="Differing cells before grid-shift realignment.",
    )
    missing_reaches: list[str] = Field(default_factory=list)
    sample_differences: list[ParityDifference] = Field(default_factory=list)
    matches: bool


class StoredHandFimResult(HandFimResult, OutputDestination):
    """Successful outputs and the destination persisted by the worker."""

    execution_mode: ExecutionMode = "legacy"
    files: list[OutputFile] = Field(default_factory=list)
    scenario_manifests: list[ScenarioManifest] = Field(default_factory=list)
    parity: list[ScenarioParity] = Field(
        default_factory=list,
        description="Legacy/streaming comparisons; present only for parity jobs.",
    )
    local_output_dir: str | None = None


class JobSummary(BaseModel):
    job_id: str
    status: str


class JobList(BaseModel):
    jobs: list[JobSummary]


class JobStatus(JobSummary):
    result: StoredHandFimResult | None = None
    error: str | None = None


class JobLogs(BaseModel):
    job_id: str
    status: str
    logs: str
    next_offset: int


class RasterInfo(BaseModel):
    bounds: tuple[float, float, float, float]
    minzoom: int
    maxzoom: int
