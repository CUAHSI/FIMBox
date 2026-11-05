"""Prepare and publish sharded, cloud-optimized HAND-FIM source coverage."""

import io
import json
import re
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import boto3
import pyarrow as pa
import pyarrow.parquet as pq
from botocore import UNSIGNED
from botocore.config import Config
from botocore.exceptions import ClientError

import prepare_streaming_dataset as preparation

DEFAULT_FIM_VERSION = "4.5.2.11"
DATASET_FILE = "dataset.parquet"
DATASET_JSON_FILE = "dataset.json"
DATASET_INFO_FILE = "dataset-info.txt"
HUC_MARKER = "huc.parquet"
UPLOAD_LOG_INTERVAL = 100
UPLOAD_WORKERS = 10  # botocore's default connection pool size


def list_hucs(client, fim_version: str = DEFAULT_FIM_VERSION) -> list[str]:
    """Lists every eight-digit HUC published under a NOAA FIM version."""

    if not re.fullmatch(r"\d+(\.\d+)*", fim_version):
        raise preparation.PreparationError("FIM version must be dotted digits, e.g. 4.5.2.11")
    prefix = f"hand_fim_{fim_version.replace('.', '_')}/"
    paginator = client.get_paginator("list_objects_v2")
    hucs = {
        item["Prefix"][len(prefix):-1]
        for page in paginator.paginate(
            Bucket=preparation.SOURCE_BUCKET, Prefix=prefix, Delimiter="/",
        )
        for item in page.get("CommonPrefixes", [])
        if re.fullmatch(r"\d{8}", item["Prefix"][len(prefix):-1])
    }
    if not hucs:
        raise preparation.PreparationError(
            f"No HUCs found at s3://{preparation.SOURCE_BUCKET}/{prefix}"
        )
    return sorted(hucs)


def __output_key(prefix: str, relative_path: Path) -> str:
    normalized_prefix = prefix.strip("/")
    if normalized_prefix and any(
        part in {"", ".", ".."} or "\\" in part for part in normalized_prefix.split("/")
    ):
        raise preparation.PreparationError("Output prefix contains an unsafe path")
    parts = relative_path.as_posix().split("/")
    if not parts or any(part in {"", ".", ".."} or "\\" in part for part in parts):
        raise preparation.PreparationError("Prepared dataset contains an unsafe path")
    return "/".join(part for part in (normalized_prefix, *parts) if part)


def __head_exists(client, bucket: str, key: str) -> bool:
    try:
        client.head_object(Bucket=bucket, Key=key)
    except ClientError as error:
        code = error.response.get("Error", {}).get("Code")
        if code in {"404", "NoSuchKey", "NotFound"}:
            return False
        raise
    return True


def __metadata_bytes(block_size: int, fim_version: str) -> bytes:
    table = pa.Table.from_pylist(
        [{
            "format": preparation.DATASET_FORMAT,
            "version": preparation.DATASET_VERSION,
            "block_size": block_size,
            "fim_version": fim_version,
        }],
        schema=pa.schema([
            ("format", pa.string()), ("version", pa.int32()),
            ("block_size", pa.int32()), ("fim_version", pa.string()),
        ]),
    )
    stream = io.BytesIO()
    pq.write_table(table, stream, compression="snappy")
    return stream.getvalue()


def __dataset_info_bytes(
    bucket: str, prefix: str, hucs: list[str], block_size: int, fim_version: str,
) -> bytes:
    source_prefix = f"hand_fim_{fim_version.replace('.', '_')}/"
    lines = [
        "format=fimbox-hand-stream",
        f"format_version={preparation.DATASET_VERSION}",
        f"fim_version={fim_version}",
        f"source=s3://{preparation.SOURCE_BUCKET}/{source_prefix}",
        f"published_location=s3://{bucket}/{prefix.strip('/')}/",
        f"block_size={block_size}",
        f"huc_count={len(hucs)}",
    ]
    text = "\n".join([
        *lines,
        "hucs:",
        *hucs,
        "",
    ])
    return text.encode("utf-8")


def __ensure_dataset_metadata(
    client, bucket: str, prefix: str, hucs: list[str], block_size: int, fim_version: str,
) -> None:
    json_key = __output_key(prefix, Path(DATASET_JSON_FILE))
    if __head_exists(client, bucket, json_key):
        raise preparation.PreparationError(
            "JSON dataset metadata is unsupported; publish into a new dataset prefix"
        )

    key = __output_key(prefix, Path(DATASET_FILE))
    if __head_exists(client, bucket, key):
        response = client.get_object(Bucket=bucket, Key=key)
        try:
            rows = pq.read_table(io.BytesIO(response["Body"].read())).to_pylist()
        except (pa.ArrowException, OSError) as error:
            raise preparation.PreparationError(
                f"s3://{bucket}/{key} is not valid Parquet"
            ) from error
        expected = [{
            "format": preparation.DATASET_FORMAT,
            "version": preparation.DATASET_VERSION,
            "block_size": block_size,
            "fim_version": fim_version,
        }]
        if rows != expected:
            raise preparation.PreparationError(
                f"s3://{bucket}/{key} describes a different dataset version or block size"
            )
    else:
        client.put_object(
            Bucket=bucket, Key=key, Body=__metadata_bytes(block_size, fim_version),
            ContentType="application/vnd.apache.parquet",
        )

    info_key = __output_key(prefix, Path(DATASET_INFO_FILE))
    expected_info = __dataset_info_bytes(bucket, prefix, hucs, block_size, fim_version)
    if __head_exists(client, bucket, info_key):
        response = client.get_object(Bucket=bucket, Key=info_key)
        existing_info = response["Body"].read()
        provenance_keys = (
            b"preparation_job_id=", b"dataset_name=", b"submitted_by=", b"submitted_at=",
        )
        stable_info = b"\n".join(
            line for line in existing_info.splitlines()
            if not line.startswith(provenance_keys)
        )
        if stable_info != expected_info.rstrip(b"\n"):
            raise preparation.PreparationError(
                f"s3://{bucket}/{info_key} describes different source coverage or location"
            )
    else:
        client.put_object(
            Bucket=bucket, Key=info_key, Body=expected_info, ContentType="text/plain",
        )


def __ensure_run_provenance(client, bucket: str, prefix: str, context: dict | None) -> None:
    if not context:
        return
    job_id = str(context["job_id"])
    provenance = {
        "job_id": job_id,
        "source_version": context["source_version"],
        "source_location": context["source_location"],
        "destination_location": context["destination_location"],
        "dataset_name": context["dataset_name"],
        "submitted_by": context["submitted_by"],
        "submitted_at": context["submitted_at"],
        "all_hucs": context["all_hucs"],
        "hucs": context["hucs"],
    }
    key = __output_key(prefix, Path("preparations") / f"{job_id}.json")
    expected = json.dumps(provenance, indent=2, sort_keys=True).encode()
    if __head_exists(client, bucket, key):
        response = client.get_object(Bucket=bucket, Key=key)
        if response["Body"].read() != expected:
            raise preparation.PreparationError(f"s3://{bucket}/{key} contains conflicting run provenance")
    else:
        client.put_object(
            Bucket=bucket, Key=key, Body=expected, ContentType="application/json",
        )


def select_hucs(available: list[str], requested: list[str], all_hucs: bool) -> list[str]:
    if all_hucs:
        return available
    missing = sorted(set(requested) - set(available))
    if missing:
        raise preparation.PreparationError(
            f"Requested HUCs are not available in this source version: {', '.join(missing)}"
        )
    return sorted(requested)


def __delete_huc_objects(client, bucket: str, prefix: str, huc: str) -> int:
    """Removes every object under the HUC's prefix, including its old completion marker."""
    huc_prefix = __output_key(prefix, Path("hucs") / huc) + "/"
    deleted = 0
    for page in client.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=huc_prefix):
        keys = [{"Key": item["Key"]} for item in page.get("Contents", [])]
        for start in range(0, len(keys), 1000):
            client.delete_objects(Bucket=bucket, Delete={"Objects": keys[start:start + 1000], "Quiet": True})
        deleted += len(keys)
    return deleted


def __publish_huc(client, bucket: str, prefix: str, huc: str, output_dir: Path, log=print) -> None:
    marker = output_dir / "hucs" / huc / HUC_MARKER
    if not marker.is_file():
        raise preparation.PreparationError(f"Prepared HUC {huc} has no completion marker")

    paths = sorted(output_dir.rglob("*"))
    for path in paths:
        if path.is_symlink():
            raise preparation.PreparationError(f"Prepared dataset contains a symlink: {path}")
    files = [path for path in paths if path.is_file()]
    uploads = [path for path in files if path != output_dir / DATASET_FILE and path != marker]
    total = len(uploads) + 1
    removed = __delete_huc_objects(client, bucket, prefix, huc)
    log(f"HUC {huc}: removed {removed} existing objects; uploading {total} files to S3")
    started = time.monotonic()

    def upload(path: Path) -> None:
        content_type = "image/tiff" if path.suffix.lower() == ".tif" else "application/vnd.apache.parquet"
        client.upload_file(
            str(path), bucket, __output_key(prefix, path.relative_to(output_dir)),
            ExtraArgs={"ContentType": content_type},
        )

    with ThreadPoolExecutor(max_workers=UPLOAD_WORKERS) as pool:
        for number, _ in enumerate(pool.map(upload, uploads), 1):
            if number % UPLOAD_LOG_INTERVAL == 0:
                log(f"HUC {huc}: uploaded {number}/{total} files ({time.monotonic() - started:.0f}s)")
    client.upload_file(
        str(marker), bucket, __output_key(prefix, marker.relative_to(output_dir)),
        ExtraArgs={"ContentType": "application/vnd.apache.parquet"},
    )
    log(f"HUC {huc}: uploaded {total}/{total} files in {time.monotonic() - started:.0f}s")


def prepare_hucs(
    *,
    source_client,
    output_client,
    output_bucket: str,
    output_prefix: str,
    task_index: int,
    task_count: int,
    fim_version: str = DEFAULT_FIM_VERSION,
    block_size: int = 512,
    run_context: dict | None = None,
    hucs_to_prepare: list[str] | None = None,
    on_huc_complete=None,
    log=print,
) -> list[str]:
    """Prepares this task's stable shard of the selected NOAA HUC list."""

    if task_count < 1 or not 0 <= task_index < task_count:
        raise ValueError("HUC shard index/count are invalid")
    if not output_bucket:
        raise ValueError("Output bucket must be set")
    if not output_prefix.strip("/"):
        raise ValueError("Output prefix must be set")
    if block_size <= 0 or block_size % 16:
        raise ValueError("Block size must be a positive multiple of 16")

    hucs = list_hucs(source_client, fim_version)
    selected = hucs if hucs_to_prepare is None else select_hucs(hucs, hucs_to_prepare, False)
    assigned = selected[task_index::task_count]
    log(f"Task {task_index}/{task_count}: {len(assigned)} of {len(selected)} selected HUCs assigned")
    initialize_collection(
        output_client=output_client, output_bucket=output_bucket, output_prefix=output_prefix,
        available_hucs=hucs, fim_version=fim_version, block_size=block_size, run_context=run_context,
    )

    completed = []
    for huc in assigned:
        prepare_single_huc(
            source_client=source_client, output_client=output_client,
            output_bucket=output_bucket, output_prefix=output_prefix,
            huc=huc, fim_version=fim_version, block_size=block_size, log=log,
        )
        completed.append(huc)
        if on_huc_complete:
            on_huc_complete(huc)
    return completed


def initialize_collection(
    *,
    output_client,
    output_bucket: str,
    output_prefix: str,
    available_hucs: list[str],
    fim_version: str,
    block_size: int = 512,
    run_context: dict | None = None,
) -> None:
    if not output_bucket or not output_prefix.strip("/"):
        raise ValueError("A nonempty output bucket and prefix are required")
    if block_size <= 0 or block_size % 16:
        raise ValueError("Block size must be a positive multiple of 16")
    __ensure_dataset_metadata(
        output_client, output_bucket, output_prefix, available_hucs, block_size, fim_version,
    )
    __ensure_run_provenance(output_client, output_bucket, output_prefix, run_context)


def prepare_single_huc(
    *,
    source_client,
    output_client,
    output_bucket: str,
    output_prefix: str,
    huc: str,
    fim_version: str = DEFAULT_FIM_VERSION,
    block_size: int = 512,
    log=print,
) -> bool:
    """Prepares one HUC and replaces everything under its output prefix with the result."""

    if not re.fullmatch(r"\d{8}", huc):
        raise ValueError("HUC must be an eight-digit string")
    log(f"Preparing HUC {huc}")
    with tempfile.TemporaryDirectory(prefix=f"fimbox-{huc}-") as temporary:
        root = Path(temporary)
        huc_dir = root / "source" / f"flood_{huc}" / huc
        output_dir = root / "prepared"
        preparation.download_huc(
            huc, huc_dir, fim_version=fim_version, client=source_client, log=log,
        )
        source_branches = set(preparation.__branch_ids(huc_dir, None))
        metadata = preparation.prepare_huc(
            huc, huc_dir, output_dir, block_size=block_size,
            fim_version=fim_version, log=log,
        )
        missing_branches = sorted(source_branches - set(metadata["branches"]), key=int)
        if missing_branches:
            raise preparation.PreparationError(
                f"HUC {huc} is missing required branch rasters: {', '.join(missing_branches)}"
            )
        __publish_huc(output_client, output_bucket, output_prefix, huc, output_dir, log)
    log(f"Published HUC {huc}")
    return True


def source_s3_client():
    source = boto3.client(
        "s3", region_name=preparation.SOURCE_REGION,
        config=Config(signature_version=UNSIGNED),
    )
    return source
