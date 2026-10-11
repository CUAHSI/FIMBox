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
    output_bucket: str = Field(
        min_length=1, max_length=255, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$",
    )
    output_prefix: str = Field(max_length=900)

    model_config = {"extra": "forbid"}

    @field_validator("output_prefix")
    @classmethod
    def validate_prefix_size(cls, value: str) -> str:
        if len(value.encode("utf-8")) > 900:
            raise ValueError("Output prefix must not exceed 900 UTF-8 bytes")
        return value


class JobRequest(OutputDestination):
    payload: HandFimParameters

    model_config = {"extra": "forbid"}


class JobSubmitted(BaseModel):
    job_id: str
    status: str
    status_url: str


class OutputFile(BaseModel):
    path: str
    key: str
    size_bytes: int
    download_url: str


class StoredHandFimResult(HandFimResult, OutputDestination):
    """Successful outputs and the destination persisted by the worker."""

    files: list[OutputFile] = Field(default_factory=list)


class PresignedOutput(BaseModel):
    url: str
    expires_in: int


class JobStatus(BaseModel):
    job_id: str
    status: str
    result: StoredHandFimResult | None = None
    error: str | None = None
