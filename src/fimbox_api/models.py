import math

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
            for reach in scenario.Reaches:
                if len(reach.HUC) != 8:
                    raise ValueError("HAND-FIM requires eight-digit HUC identifiers")
                if not math.isfinite(reach.Streamflow):
                    raise ValueError("Streamflow must be finite")
        return self


class OutputDestination(BaseModel):
    output_bucket: str | None = Field(
        default=None, min_length=1, max_length=255, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$",
    )
    output_prefix: str = Field(default="", max_length=900)

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


class HandFimJobOptions(OutputDestination):
    max_procs: int | None = Field(
        default=None, ge=1,
        description="Max concurrent HAND-FIM processes; omit to use the worker's HAND_FIM_MAX_PROCS (default 1).",
    )


class JobRequest(HandFimJobOptions):
    payload: HandFimParameters

    model_config = {"extra": "forbid"}


class JobSubmitted(BaseModel):
    job_id: str
    status: str
    status_url: str


class AuthenticationConfig(BaseModel):
    authentication_enabled: bool


class OutputFile(BaseModel):
    path: str
    key: str | None = None
    size_bytes: int
    download_url: str


class StoredHandFimResult(HandFimResult, OutputDestination):
    """Successful outputs and the destination persisted by the worker."""

    files: list[OutputFile] = Field(default_factory=list)
    local_output_dir: str | None = None


class PresignedOutput(BaseModel):
    url: str
    expires_in: int


class JobSummary(BaseModel):
    job_id: str
    status: str


class JobList(BaseModel):
    jobs: list[JobSummary]


class JobStatus(JobSummary):
    result: StoredHandFimResult | None = None
    error: str | None = None
