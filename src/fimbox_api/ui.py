"""Browser page for browsing input data, submitting HAND-FIM jobs, and viewing results.

The JSON helpers below exist only for the page and are excluded from OpenAPI so the
generated SDK contract is unchanged. Job submission/status and map tiles reuse the
public API routes.
"""

import io
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import quote

import pyarrow as pa
import pyarrow.parquet as pq
from botocore.exceptions import ClientError
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel

from .auth import UserSession, get_authenticated_session
from .config import settings
from .models import ExecutionMode
from .storage import anonymous_s3_client, output_s3_client

logger = logging.getLogger(__name__)
STATIC_DIR = Path(__file__).parent / "static" / "ui"
PREVIEW_SUFFIXES = (".json", ".md", ".txt", ".csv", ".log", ".xml", ".yaml", ".yml", ".geojson")
PREVIEW_MAX_BYTES = 512 * 1024
DOWNLOAD_URL_SECONDS = 300
LIST_MAX_KEYS = 1000
PARQUET_MAX_ROWS = 200
PARQUET_MAX_READ_BYTES = 64 * 1024 * 1024
PARQUET_MAX_CELL_CHARS = 200

router = APIRouter(prefix="/ui", include_in_schema=False)
AuthenticatedSession = Annotated[UserSession, Depends(get_authenticated_session)]
Bucket = Annotated[str, Query(min_length=1, max_length=255, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")]
Key = Annotated[str, Query(min_length=1, max_length=1024)]
Prefix = Annotated[str, Query(max_length=1024)]


class InputDataset(BaseModel):
    bucket: str
    prefix: str
    unsigned: bool


class UiConfig(BaseModel):
    authentication_enabled: bool
    require_hydroshare_output: bool
    streaming_enabled: bool
    execution_modes: list[ExecutionMode]
    input_dataset: InputDataset | None


class Example(BaseModel):
    name: str
    scenarios: list[dict[str, Any]]


class Folder(BaseModel):
    name: str
    prefix: str


class StoredObject(BaseModel):
    name: str
    key: str
    size_bytes: int
    last_modified: datetime | None = None


class ObjectListing(BaseModel):
    bucket: str
    prefix: str
    folders: list[Folder]
    files: list[StoredObject]
    next_token: str | None = None


class ObjectPreview(BaseModel):
    key: str
    size_bytes: int
    text: str
    truncated: bool


class ParquetColumn(BaseModel):
    name: str
    type: str


class ParquetPreview(BaseModel):
    key: str
    size_bytes: int
    num_rows: int
    num_row_groups: int
    columns: list[ParquetColumn]
    rows: list[list[str | None]]
    truncated: bool


class DownloadLink(BaseModel):
    url: str
    expires_in: int


@router.get("", response_class=FileResponse)
def page() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html", media_type="text/html")


@router.get("/config", response_model=UiConfig)
def ui_config() -> UiConfig:
    streaming = settings.streaming_enabled
    return UiConfig(
        authentication_enabled=settings.authentication_enabled,
        require_hydroshare_output=settings.require_hydroshare_output,
        streaming_enabled=streaming,
        execution_modes=["legacy", "updatedlegacy", *(["streaming", "updatedstreaming"] if streaming else [])],
        input_dataset=InputDataset(
            bucket=settings.stream_input_bucket,
            prefix=settings.stream_input_prefix.strip("/"),
            unsigned=settings.stream_input_unsigned,
        ) if streaming else None,
    )


@router.get("/examples", response_model=list[Example])
def examples() -> list[Example]:
    """Sample scenario arrays from the test-data directory."""
    found = []
    for path in sorted(settings.examples_dir.glob("*.json")):
        try:
            scenarios = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            logger.warning("Skipping unreadable example %s", path.name)
            continue
        if isinstance(scenarios, list):
            found.append(Example(name=path.stem, scenarios=scenarios))
    return found


def __clean_path(path: str, *, folder: bool) -> str:
    stripped = path.strip("/") if folder else path
    if len(path.encode("utf-8")) > 1024 or (
        stripped and any(part in {"", ".", ".."} for part in stripped.split("/"))
    ):
        raise HTTPException(status_code=400, detail="Invalid object path")
    if not folder and not stripped:
        raise HTTPException(status_code=400, detail="Invalid object path")
    return stripped + "/" if folder and stripped else stripped


def __s3_client(bucket: str, path: str, session: UserSession):
    """Unsigned reads within a public input dataset; otherwise the user's HydroShare permissions."""
    root = settings.stream_input_prefix.strip("/")
    if (
        settings.streaming_enabled and settings.stream_input_unsigned
        and bucket == settings.stream_input_bucket
        and (not root or path == root or path.startswith(root + "/"))
    ):
        return anonymous_s3_client()
    if not settings.authentication_enabled:
        raise HTTPException(status_code=409, detail="HydroShare S3 reads require configured authentication")
    return output_s3_client(session)


def __s3_error(error: ClientError) -> HTTPException:
    code = error.response.get("Error", {}).get("Code", "")
    if code in {"NoSuchKey", "404", "NotFound"}:
        return HTTPException(status_code=404, detail="Object not found")
    if code in {"NoSuchBucket"}:
        return HTTPException(status_code=404, detail="Bucket not found")
    if code in {"AccessDenied", "403", "Forbidden"}:
        return HTTPException(status_code=403, detail="Access denied by HydroShare storage")
    logger.error("S3 request failed (%s)", code or "unknown")
    return HTTPException(status_code=503, detail="Output storage unavailable")


@router.get("/objects", response_model=ObjectListing)
def list_objects(
    bucket: Bucket, session: AuthenticatedSession, prefix: Prefix = "",
    token: Annotated[str | None, Query(max_length=4096)] = None,
) -> ObjectListing:
    """List one folder level of a bucket prefix."""
    prefix = __clean_path(prefix, folder=True)
    client = __s3_client(bucket, prefix.rstrip("/"), session)
    params: dict[str, Any] = {"Bucket": bucket, "Prefix": prefix, "Delimiter": "/", "MaxKeys": LIST_MAX_KEYS}
    if token:
        params["ContinuationToken"] = token
    try:
        response = client.list_objects_v2(**params)
    except ClientError as error:
        raise __s3_error(error) from None
    folders = [
        Folder(name=item["Prefix"][len(prefix):].rstrip("/"), prefix=item["Prefix"])
        for item in response.get("CommonPrefixes", [])
    ]
    files = [
        StoredObject(
            name=item["Key"][len(prefix):], key=item["Key"],
            size_bytes=item.get("Size", 0), last_modified=item.get("LastModified"),
        )
        for item in response.get("Contents", []) if item["Key"] != prefix
    ]
    return ObjectListing(
        bucket=bucket, prefix=prefix, folders=folders, files=files,
        next_token=response.get("NextContinuationToken") if response.get("IsTruncated") else None,
    )


@router.get("/objects/preview", response_model=ObjectPreview)
def preview_object(bucket: Bucket, key: Key, session: AuthenticatedSession) -> ObjectPreview:
    """Return the beginning of a text object for in-page display."""
    key = __clean_path(key, folder=False)
    if not key.lower().endswith(PREVIEW_SUFFIXES):
        raise HTTPException(status_code=400, detail="Only text files can be previewed; download instead")
    client = __s3_client(bucket, key, session)
    try:
        response = client.get_object(Bucket=bucket, Key=key, Range=f"bytes=0-{PREVIEW_MAX_BYTES - 1}")
        content = response["Body"].read(PREVIEW_MAX_BYTES)
    except ClientError as error:
        raise __s3_error(error) from None
    content_range = response.get("ContentRange", "")
    size = int(content_range.rsplit("/", 1)[1]) if "/" in content_range and not content_range.endswith("*") else len(content)
    return ObjectPreview(
        key=key, size_bytes=size, text=content.decode("utf-8", errors="replace"), truncated=size > len(content),
    )


class __RangeReader(io.RawIOBase):
    """Seekable read-only view of an S3 object that fetches byte ranges on demand."""

    def __init__(self, client, bucket: str, key: str, size: int, budget: int):
        self.__client, self.__bucket, self.__key = client, bucket, key
        self.__size, self.__budget, self.__position = size, budget, 0

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def tell(self) -> int:
        return self.__position

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        base = {io.SEEK_SET: 0, io.SEEK_CUR: self.__position, io.SEEK_END: self.__size}[whence]
        self.__position = max(0, base + offset)
        return self.__position

    def readinto(self, buffer) -> int:
        length = min(len(buffer), self.__size - self.__position)
        if length <= 0:
            return 0
        if length > self.__budget:
            raise HTTPException(status_code=413, detail="Parquet preview would read too much data; download instead")
        self.__budget -= length
        end = self.__position + length - 1
        response = self.__client.get_object(Bucket=self.__bucket, Key=self.__key, Range=f"bytes={self.__position}-{end}")
        data = response["Body"].read(length)
        buffer[:len(data)] = data
        self.__position += len(data)
        return len(data)


def __parquet_cell(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, bytes):
        text = f"<{len(value)} bytes>"
    elif isinstance(value, (dict, list)):
        text = json.dumps(value, default=str)
    else:
        text = str(value)
    return text if len(text) <= PARQUET_MAX_CELL_CHARS else text[:PARQUET_MAX_CELL_CHARS - 1] + "…"


@router.get("/objects/parquet", response_model=ParquetPreview)
def preview_parquet(
    bucket: Bucket, key: Key, session: AuthenticatedSession,
    limit: Annotated[int, Query(ge=1, le=PARQUET_MAX_ROWS)] = 50,
) -> ParquetPreview:
    """Return a Parquet object's schema and first rows, reading only the needed byte ranges."""
    key = __clean_path(key, folder=False)
    if not key.lower().endswith(".parquet"):
        raise HTTPException(status_code=400, detail="Only .parquet files can be previewed as tables")
    client = __s3_client(bucket, key, session)
    try:
        size = client.head_object(Bucket=bucket, Key=key)["ContentLength"]
        reader = __RangeReader(client, bucket, key, size, PARQUET_MAX_READ_BYTES)
        parquet = pq.ParquetFile(io.BufferedReader(reader, buffer_size=256 * 1024))
        batch = next(parquet.iter_batches(batch_size=limit), None)
    except ClientError as error:
        raise __s3_error(error) from None
    except pa.ArrowException as error:
        raise HTTPException(status_code=422, detail=f"Not a readable Parquet file: {error}") from None
    rows = [] if batch is None else [
        [__parquet_cell(value) for value in row.values()] for row in batch.to_pylist()
    ]
    metadata = parquet.metadata
    return ParquetPreview(
        key=key, size_bytes=size, num_rows=metadata.num_rows, num_row_groups=metadata.num_row_groups,
        columns=[ParquetColumn(name=field.name, type=str(field.type)) for field in parquet.schema_arrow],
        rows=rows, truncated=metadata.num_rows > len(rows),
    )


@router.get("/objects/download", response_model=DownloadLink)
def download_object(bucket: Bucket, key: Key, session: AuthenticatedSession) -> DownloadLink:
    """A short-lived presigned URL so large files bypass the API process."""
    key = __clean_path(key, folder=False)
    client = __s3_client(bucket, key, session)
    try:
        client.head_object(Bucket=bucket, Key=key)
    except ClientError as error:
        raise __s3_error(error) from None
    name = key.rsplit("/", 1)[-1]
    url = client.generate_presigned_url(
        "get_object",
        Params={
            "Bucket": bucket, "Key": key,
            "ResponseContentDisposition": f"attachment; filename*=UTF-8''{quote(name)}",
        },
        ExpiresIn=DOWNLOAD_URL_SECONDS,
    )
    return DownloadLink(url=url, expires_in=DOWNLOAD_URL_SECONDS)
