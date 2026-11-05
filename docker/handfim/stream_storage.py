"""S3 access for the container's ``stream-scenario`` command.

Source HAND rasters (NOAA's bucket, or relative keys in the dataset) are read with GDAL range requests; reach indexes are
read with small object GETs or, for a HUC's reaches.parquet, ranged GETs; output tiles are uploaded one at a time as they
are produced. Nothing is staged on the local (memory-backed) filesystem except
the small per-scenario manifests returned to the worker.
"""

import io
import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import boto3
import rasterio
from botocore import UNSIGNED
from botocore.config import Config
from botocore.exceptions import ClientError
from rasterio.session import AWSSession

MAX_INDEX_BYTES = 16 * 1024 * 1024
RANGE_BLOCK_BYTES = 1024 * 1024
NOAA_RASTER_BASE = "https://ciroh-owp-hand-fim.s3.us-east-2.amazonaws.com/"
TILE_CONTENT_TYPE = "image/tiff; application=geotiff; profile=cloud-optimized"


def join_key(*parts: str) -> str:
    return "/".join(part.strip("/") for part in parts if part and part.strip("/"))


@dataclass(frozen=True)
class StreamConfig:
    input_bucket: str
    input_prefix: str
    input_unsigned: bool
    output_bucket: str
    output_prefix: str
    endpoint_url: str
    region: str
    addressing_style: str
    access_key: str
    secret_key: str
    tile_size: int

    @classmethod
    def from_env(cls, environ=os.environ) -> "StreamConfig":
        def required(name: str) -> str:
            value = environ.get(name, "")
            if not value:
                raise RuntimeError(f"{name} is required for streaming mode")
            return value

        return cls(
            input_bucket=required("FIMBOX_STREAM_INPUT_BUCKET"),
            input_prefix=environ.get("FIMBOX_STREAM_INPUT_PREFIX", ""),
            input_unsigned=environ.get("FIMBOX_STREAM_INPUT_UNSIGNED", "").lower() in {"1", "true", "yes"},
            output_bucket=required("FIMBOX_STREAM_OUTPUT_BUCKET"),
            output_prefix=environ.get("FIMBOX_STREAM_OUTPUT_PREFIX", ""),
            endpoint_url=required("FIMBOX_S3_ENDPOINT_URL"),
            region=environ.get("FIMBOX_S3_REGION", "us-east-1"),
            addressing_style=environ.get("FIMBOX_S3_ADDRESSING_STYLE", "path"),
            access_key=required("AWS_ACCESS_KEY_ID"),
            secret_key=required("AWS_SECRET_ACCESS_KEY"),
            tile_size=int(environ.get("FIMBOX_STREAM_TILE_SIZE", "2048")),
        )

    def scenario_prefix(self, scenario_id: str) -> str:
        return join_key(self.output_prefix, scenario_id)


class RangedObject(io.RawIOBase):
    """Seekable read-only view of an S3 object that fetches aligned blocks on demand."""

    def __init__(self, client, bucket: str, key: str, size: int):
        self.__client, self.__bucket, self.__key, self.__size = client, bucket, key, size
        self.__position = 0
        self.__blocks: dict[int, bytes] = {}

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

    def __block(self, number: int) -> bytes:
        if number not in self.__blocks:
            start = number * RANGE_BLOCK_BYTES
            end = min(start + RANGE_BLOCK_BYTES, self.__size) - 1
            response = self.__client.get_object(
                Bucket=self.__bucket, Key=self.__key, Range=f"bytes={start}-{end}",
            )
            with response["Body"] as body:
                self.__blocks[number] = body.read()
        return self.__blocks[number]

    def readinto(self, buffer) -> int:
        wanted = min(len(buffer), self.__size - self.__position)
        copied = 0
        while copied < wanted:
            number, offset = divmod(self.__position, RANGE_BLOCK_BYTES)
            chunk = self.__block(number)[offset:offset + wanted - copied]
            buffer[copied:copied + len(chunk)] = chunk
            copied += len(chunk)
            self.__position += len(chunk)
        return copied


def __client(config: StreamConfig, unsigned: bool):
    options = {"s3": {"addressing_style": config.addressing_style}}
    if "request_checksum_calculation" in Config.OPTION_DEFAULTS:
        options["request_checksum_calculation"] = "when_required"
    if unsigned:
        return boto3.client(
            "s3", endpoint_url=config.endpoint_url, region_name=config.region,
            config=Config(signature_version=UNSIGNED, **options),
        )
    return boto3.client(
        "s3", endpoint_url=config.endpoint_url, region_name=config.region,
        aws_access_key_id=config.access_key, aws_secret_access_key=config.secret_key,
        aws_session_token="", config=Config(signature_version="s3v4", **options),
    )


def __gdal_environment(config: StreamConfig, options: dict) -> rasterio.Env:
    endpoint = urlsplit(config.endpoint_url)
    if endpoint.scheme not in {"http", "https"} or not endpoint.netloc or endpoint.path.strip("/"):
        raise RuntimeError("S3 endpoint must be an HTTP(S) origin")
    session = AWSSession(
        aws_unsigned=True, region_name=config.region,
    ) if config.input_unsigned else AWSSession(
        aws_access_key_id=config.access_key, aws_secret_access_key=config.secret_key,
        aws_session_token="", region_name=config.region, aws_unsigned=False,
    )
    return rasterio.Env(
        session=session,
        AWS_S3_ENDPOINT=endpoint.netloc,
        AWS_HTTPS=endpoint.scheme == "https",
        AWS_VIRTUAL_HOSTING=config.addressing_style == "virtual",
        **options,
    )


def run_streaming(
    scenarios: list[dict], output_dir: Path, stream_module, config: StreamConfig,
    log: Callable[[str], None] = print, rule=None,
) -> list[Path]:
    """
    Streams each scenario from the prepared dataset to S3 tiles.

    Arguments:
        scenarios: Validated scenario dictionaries.
        output_dir: Local directory for the small per-scenario manifests.
        stream_module: The ``fimserve.streamFIM`` module.
        config: S3 locations and credentials.
        log: Progress output function.
        rule: Optional ``streamFIM.FloodRule``; None uses the module default.
    Returns:
        The written manifest paths, one per scenario.
    """

    input_client = __client(config, config.input_unsigned)
    output_client = __client(config, False)

    def read_index(key: str, feature_ids=None) -> dict | None:
        location = f"s3://{config.input_bucket}/{join_key(config.input_prefix, key)}"
        if key.endswith("/reaches.parquet"):
            return read_reaches(key, location, feature_ids or [])
        try:
            response = input_client.get_object(
                Bucket=config.input_bucket, Key=join_key(config.input_prefix, key),
            )
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") in {"404", "NoSuchKey", "NotFound"}:
                return None
            raise
        with response["Body"] as body:
            if response.get("ContentLength", 0) > MAX_INDEX_BYTES:
                raise RuntimeError(f"Prepared dataset index {location} is too large")
            content = body.read(MAX_INDEX_BYTES + 1)
        if len(content) > MAX_INDEX_BYTES:
            raise RuntimeError(f"Prepared dataset index {location} is too large")
        try:
            return stream_module.read_index_parquet(key, content)
        except stream_module.StreamingInputError as error:
            raise RuntimeError(
                f"Prepared dataset index {location}: {error}. "
                "Check the uploaded object against the local prepared file. "
                "For HydroShare AWS CLI uploads, set "
                "AWS_REQUEST_CHECKSUM_CALCULATION=when_required and re-upload "
                "the HUC indexes and COGs from the original files."
            ) from error

    def read_reaches(key: str, location: str, feature_ids) -> dict | None:
        object_key = join_key(config.input_prefix, key)
        try:
            size = input_client.head_object(Bucket=config.input_bucket, Key=object_key)["ContentLength"]
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") in {"404", "NoSuchKey", "NotFound"}:
                return None
            raise
        try:
            return stream_module.read_reaches_parquet(
                RangedObject(input_client, config.input_bucket, object_key, size), feature_ids,
            )
        except stream_module.StreamingInputError as error:
            raise RuntimeError(f"Prepared dataset index {location}: {error}") from error

    def raster_path(key: str) -> str:
        if key.startswith("https://"):
            # Source rasters are read in place from NOAA's public bucket; no other host is trusted.
            if not key.startswith(NOAA_RASTER_BASE):
                raise RuntimeError("Prepared dataset references an untrusted raster location")
            return "/vsicurl/" + key
        return f"s3://{config.input_bucket}/{join_key(config.input_prefix, key)}"

    output_dir.mkdir(parents=True, exist_ok=True)
    manifests = []
    with __gdal_environment(config, stream_module.GDAL_STREAMING_OPTIONS):
        for scenario in scenarios:
            prefix = config.scenario_prefix(scenario["ScenarioID"])

            def write_tile(name: str, data: bytes, prefix=prefix) -> str:
                key = join_key(prefix, name)
                output_client.put_object(
                    Bucket=config.output_bucket, Key=key, Body=data, ContentType=TILE_CONTENT_TYPE,
                )
                return key

            manifest = stream_module.run_scenario(
                scenario, read_index, raster_path, write_tile, tile_size=config.tile_size, log=log,
                **({"rule": rule} if rule is not None else {}),
            )
            path = output_dir / f"{scenario['ScenarioID']}.manifest.json"
            path.write_bytes(stream_module.dumps_manifest(manifest))
            manifests.append(path)
            log(f"[{scenario['ScenarioID']}] wrote {len(manifest['tiles'])} tiles")
    return manifests
