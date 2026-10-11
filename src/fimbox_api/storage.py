import json
import logging
import math
from datetime import datetime, timezone
from uuid import UUID

import boto3
import httpx
from botocore.config import Config
from botocore.exceptions import ClientError
from fastapi import HTTPException
from pydantic import BaseModel, Field, SecretStr, ValidationError, field_validator

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


def output_key(prefix: str, job_id: UUID, index: int) -> str:
    return "/".join(part for part in (prefix.strip("/"), str(job_id), f"{index}.tif") if part)


def output_file_key(prefix: str, job_id: UUID, relative_path: str) -> str:
    key = "/".join(
        part for part in (prefix.strip("/"), str(job_id), relative_path) if part
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


def presign_output(job_id: UUID, bucket: str, key: str) -> tuple[str, int]:
    credentials = __job_credentials(job_id)
    expires_in = min(
        settings.s3_presigned_url_seconds,
        math.floor((credentials.expiry - datetime.now(timezone.utc)).total_seconds()),
    )
    if expires_in < 1:
        raise StorageCredentialsError("Job S3 credentials have expired")
    client = s3_client(job_id)
    try:
        client.head_object(Bucket=bucket, Key=key)
    except ClientError as error:
        if error.response["Error"]["Code"] in ("404", "NoSuchKey", "NotFound"):
            raise HTTPException(status_code=404, detail="Output object no longer available") from error
        raise
    url = client.generate_presigned_url(
        "get_object",
        Params={"Bucket": bucket, "Key": key},
        ExpiresIn=expires_in,
        HttpMethod="GET",
    )
    return url, expires_in
