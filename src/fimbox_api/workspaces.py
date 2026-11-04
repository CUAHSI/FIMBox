"""Storage-authorized workspace descriptors and trusted live-job associations."""

import json
from datetime import datetime, timezone
from typing import Annotated
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from botocore.exceptions import ClientError
from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field, ValidationError, field_validator

from .auth import UserSession, get_authenticated_session, get_redis_client
from .config import settings
from .storage import output_file_key, output_s3_client

router = APIRouter()
JOB_WORKSPACE_PREFIX = "workspace:job:"
MAX_DESCRIPTOR_BYTES = 16 * 1024


class WorkspaceLocation(BaseModel):
    bucket: str = Field(min_length=1, max_length=255, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
    prefix: str = Field(min_length=1, max_length=700)

    @field_validator("prefix")
    @classmethod
    def valid_prefix(cls, value: str) -> str:
        value = value.strip("/")
        if not value or any(part in {"", ".", ".."} for part in value.split("/")):
            raise ValueError("Workspace prefix must have nonempty path segments without '.' or '..'")
        if len(value.encode("utf-8")) > 700:
            raise ValueError("Workspace prefix exceeds 700 UTF-8 bytes")
        return value


class Workspace(WorkspaceLocation):
    format: str = "fimbox-workspace"
    version: int = 1
    workspace_id: UUID
    name: str = Field(min_length=1, max_length=200)
    created_by: str
    created_at: datetime


class WorkspaceOpen(BaseModel):
    location: str

    model_config = {"extra": "forbid"}


class WorkspaceCreate(WorkspaceOpen):
    name: str = Field(min_length=1, max_length=200)

    @field_validator("name")
    @classmethod
    def valid_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Workspace name must not be blank")
        return value


def parse_location(value: str) -> WorkspaceLocation:
    parts = urlsplit(value)
    if parts.scheme != "s3" or parts.query or parts.fragment or parts.username or parts.password:
        raise ValueError("Workspace location must be s3://bucket/prefix without credentials, query, or fragment")
    return WorkspaceLocation(bucket=parts.netloc, prefix=parts.path)


def __invalid(message: str, location: list[str]) -> HTTPException:
    return HTTPException(status_code=422, detail=[{
        "loc": location, "msg": message, "type": "value_error",
    }])


def __location(value: str, *, header: bool = False) -> WorkspaceLocation:
    try:
        return parse_location(value)
    except ValueError as error:
        location = ["header", "X-FIMBox-Workspace"] if header else ["body", "location"]
        raise __invalid(str(error), location) from None


def open_workspace(session: UserSession, location: WorkspaceLocation) -> Workspace:
    client = output_s3_client(session)
    try:
        response = client.get_object(
            Bucket=location.bucket, Key=output_file_key(location.prefix, "workspace.json"),
        )
    except ClientError as error:
        if error.response.get("Error", {}).get("Code") in {"NoSuchKey", "404", "NotFound"}:
            raise HTTPException(status_code=404, detail="Workspace not found; create it explicitly") from None
        raise
    body = response["Body"]
    try:
        content = body.read(MAX_DESCRIPTOR_BYTES + 1)
    finally:
        body.close()
    if len(content) > MAX_DESCRIPTOR_BYTES:
        raise HTTPException(status_code=502, detail="Workspace descriptor exceeds 16 KiB")
    try:
        workspace = Workspace.model_validate_json(content)
    except ValidationError:
        raise HTTPException(status_code=502, detail="Invalid workspace descriptor") from None
    if workspace.format != "fimbox-workspace" or workspace.version != 1:
        raise HTTPException(status_code=409, detail="Unsupported workspace descriptor format/version")
    if (workspace.bucket, workspace.prefix) != (location.bucket, location.prefix):
        raise HTTPException(status_code=409, detail="Workspace descriptor location does not match")
    return workspace


@router.post("/workspaces", status_code=201, response_model=Workspace)
def create_workspace(
    body: WorkspaceCreate, session: Annotated[UserSession, Depends(get_authenticated_session)],
) -> Workspace:
    location = __location(body.location)
    workspace = Workspace(
        **location.model_dump(), workspace_id=uuid4(), name=body.name,
        created_by=session.user_id, created_at=datetime.now(timezone.utc),
    )
    parts = location.prefix.split("/")
    if len(parts) < 4 or parts[1:3] != ["data", "contents"]:
        raise __invalid("Choose a workspace subprefix beneath <resource_id>/data/contents/", ["body", "location"])
    client = output_s3_client(session)
    existing = client.list_objects_v2(
        Bucket=location.bucket, Prefix=location.prefix + "/", MaxKeys=1,
    )
    if existing.get("Contents"):
        raise HTTPException(status_code=409, detail="Workspace subprefix is not empty; open an existing workspace or choose an empty subprefix")
    try:
        client.put_object(
            Bucket=location.bucket, Key=output_file_key(location.prefix, "workspace.json"),
            Body=workspace.model_dump_json().encode(), ContentType="application/json", IfNoneMatch="*",
        )
    except ClientError as error:
        if error.response.get("Error", {}).get("Code") in {"PreconditionFailed", "ConditionalRequestConflict", "409", "412"}:
            raise HTTPException(status_code=409, detail="Workspace already exists; open it instead") from None
        raise
    return workspace


@router.post("/workspaces/open", response_model=Workspace)
def load_workspace(
    body: WorkspaceOpen, session: Annotated[UserSession, Depends(get_authenticated_session)],
) -> Workspace:
    workspace = open_workspace(session, __location(body.location))
    # Check listing independently of descriptor read permission without writing.
    output_s3_client(session).list_objects_v2(
        Bucket=workspace.bucket, Prefix=output_file_key(workspace.prefix, "jobs") + "/", MaxKeys=1,
    )
    return workspace


def selected_workspace(
    session: Annotated[UserSession, Depends(get_authenticated_session)],
    value: Annotated[str | None, Header(alias="X-FIMBox-Workspace")] = None,
) -> Workspace:
    if value is None:
        raise HTTPException(status_code=400, detail="Create or open a workspace and send X-FIMBox-Workspace")
    return open_workspace(session, __location(value, header=True))


WorkspaceSelection = Annotated[Workspace, Depends(selected_workspace)]


def bind_job(workspace: Workspace, job_id: str) -> None:
    get_redis_client().set(
        JOB_WORKSPACE_PREFIX + str(UUID(job_id)),
        json.dumps({"workspace_id": str(workspace.workspace_id), "bucket": workspace.bucket, "prefix": workspace.prefix}),
        ex=settings.result_ttl_seconds,
    )


def job_is_bound(workspace: Workspace, job_id: str) -> bool:
    value = get_redis_client().get(JOB_WORKSPACE_PREFIX + str(UUID(job_id)))
    if value is None:
        return False
    try:
        binding = json.loads(value)
    except ValueError:
        raise HTTPException(status_code=503, detail="Invalid live job workspace association") from None
    return binding == {
        "workspace_id": str(workspace.workspace_id), "bucket": workspace.bucket, "prefix": workspace.prefix,
    }


def require_path(workspace: Workspace, bucket: str, prefix: str) -> None:
    if bucket != workspace.bucket or not prefix.startswith(workspace.prefix + "/"):
        raise HTTPException(status_code=403, detail="Result path is outside the active workspace")
    if any(part in {"", ".", ".."} for part in prefix.split("/")):
        raise __invalid("Invalid workspace result path", ["body"])


def destination(workspace: Workspace, job_id: str, bucket: str | None, prefix: str) -> tuple[str, str]:
    derived = output_file_key(workspace.prefix, f"jobs/{UUID(job_id)}/outputs")
    if (bucket is not None and bucket != workspace.bucket) or (prefix and prefix != derived):
        raise __invalid(
            "Output destination is assigned by the active workspace; omit output_bucket/output_prefix", ["body"],
        )
    return workspace.bucket, derived
