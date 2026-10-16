import json
import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from urllib.parse import urlsplit
from uuid import UUID

import boto3
import httpx
from botocore.config import Config
from pydantic import BaseModel, Field, SecretStr, ValidationError, field_validator
from rasterio.session import AWSSession

from .auth import UserSession, get_redis_client
from .config import settings

logger = logging.getLogger(__name__)
JOB_CREDENTIALS_PREFIX = "storage:job_credentials:"


class StorageCredentialsError(RuntimeError):
    """Sanitized HydroShare credentials failure safe for task results."""


class HydroShareS3Credentials(BaseModel):
    access_key: SecretStr = Field(min_length=1)
    secret_key: SecretStr = Field(min_length=1)
    expiry: datetime

    @field_validator("expiry")
    @classmethod
    def validate_expiry(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value <= datetime.now(timezone.utc):
            raise ValueError("S3 credentials must have a future timezone-aware expiry")
        return value


def create_job_credentials(job_id: UUID, session: UserSession) -> None:
    try:
        with httpx.Client(timeout=10, follow_redirects=False) as client:
            response = client.post(
                settings.hydroshare_s3_credentials_url,
                headers={"Authorization": f"Bearer {session.access_token.get_secret_value()}"},
                json={"expiry": settings.hydroshare_s3_credentials_expiry_days},
            )
            response.raise_for_status()
            credentials = HydroShareS3Credentials.model_validate(response.json())
    except httpx.HTTPStatusError as error:
        logger.error("HydroShare S3 credential exchange failed (HTTP %s)", error.response.status_code)
        raise StorageCredentialsError(
            f"HydroShare S3 credential exchange failed (HTTP {error.response.status_code})"
        ) from None
    except (httpx.RequestError, ValidationError, ValueError):
        logger.error("HydroShare S3 credential exchange failed")
        raise StorageCredentialsError("HydroShare S3 credential exchange failed") from None
    ttl = min(
        settings.result_ttl_seconds,
        math.floor((credentials.expiry - datetime.now(timezone.utc)).total_seconds()),
    )
    if ttl < 1:
        raise StorageCredentialsError("HydroShare S3 credentials expired before storage")
    get_redis_client().set(
        JOB_CREDENTIALS_PREFIX + str(job_id),
        json.dumps({
            "access_key": credentials.access_key.get_secret_value(),
            "secret_key": credentials.secret_key.get_secret_value(),
            "expiry": credentials.expiry.isoformat(),
        }),
        ex=ttl,
    )


def delete_job_credentials(job_id: UUID) -> None:
    get_redis_client().delete(JOB_CREDENTIALS_PREFIX + str(job_id))


def __job_credentials(job_id: UUID) -> HydroShareS3Credentials:
    raw = get_redis_client().get(JOB_CREDENTIALS_PREFIX + str(job_id))
    if raw is None:
        raise StorageCredentialsError("Job S3 credentials are missing or expired")
    try:
        return HydroShareS3Credentials.model_validate_json(raw)
    except ValidationError:
        logger.error("Job S3 credentials are invalid or expired")
        raise StorageCredentialsError("Job S3 credentials are invalid or expired") from None


def output_file_key(prefix: str, relative_path: str) -> str:
    key = "/".join(
        part for part in (prefix.strip("/"), relative_path) if part
    )
    if len(key.encode("utf-8")) > 1024:
        raise StorageCredentialsError("Output object key exceeds 1024 UTF-8 bytes")
    return key


def s3_client(job_id: UUID):
    credentials = __job_credentials(job_id)
    return boto3.client(
        "s3",
        endpoint_url=settings.s3_endpoint_url,
        region_name=settings.s3_region,
        aws_access_key_id=credentials.access_key.get_secret_value(),
        aws_secret_access_key=credentials.secret_key.get_secret_value(),
        aws_session_token="",
        config=Config(
            signature_version="s3v4",
            s3={"addressing_style": settings.s3_addressing_style},
        ),
    )


@dataclass
class RasterSource:
    path: str
    options: dict[str, str | bool] = field(default_factory=dict, repr=False)
    session: AWSSession | None = field(default=None, repr=False)


def raster_source(job_id: UUID, session: UserSession, bucket: str, key: str) -> RasterSource:
    """Configure signed S3 range reads without exposing credentials to clients."""
    if get_redis_client().get(JOB_CREDENTIALS_PREFIX + str(job_id)) is None:
        create_job_credentials(job_id, session)
    credentials = __job_credentials(job_id)
    if (credentials.expiry - datetime.now(timezone.utc)).total_seconds() < 120:
        create_job_credentials(job_id, session)
        credentials = __job_credentials(job_id)
    endpoint = urlsplit(settings.s3_endpoint_url)
    if endpoint.scheme not in {"http", "https"} or not endpoint.netloc or endpoint.path.strip("/"):
        raise StorageCredentialsError("Raster S3 endpoint must be an HTTP(S) origin")
    return RasterSource(
        path=f"s3://{bucket}/{key}",
        session=AWSSession(
            aws_access_key_id=credentials.access_key.get_secret_value(),
            aws_secret_access_key=credentials.secret_key.get_secret_value(),
            aws_session_token="",
            region_name=settings.s3_region,
            aws_unsigned=False,
        ),
        options={
            "AWS_S3_ENDPOINT": endpoint.netloc,
            "AWS_HTTPS": endpoint.scheme == "https",
            "AWS_VIRTUAL_HOSTING": settings.s3_addressing_style == "virtual",
            "AWS_NO_SIGN_REQUEST": False,
        },
    )
