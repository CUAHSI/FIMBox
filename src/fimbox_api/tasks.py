import json
import logging
import mimetypes
import os
import secrets
import shutil
import signal
import subprocess
import sys
from pathlib import Path
from typing import Any
from uuid import UUID

from boto3.exceptions import S3UploadFailedError
from botocore.exceptions import BotoCoreError, ClientError
from redis.exceptions import RedisError

from schemas.hand_fim_schema import HandFimOutput

from .celery_app import celery_app
from .config import settings
from .models import HandFimParameters, HandFimJobOptions, OutputFile, ScenarioManifest, StoredHandFimResult
from .job_logs import append_job_log, clear_job_logs
from .storage import StorageCredentialsError, job_s3_environment, output_file_key, s3_client

logger = logging.getLogger(__name__)
MANIFEST_FORMAT = "fimbox-scenario-tiles"
MAX_MANIFEST_BYTES = 8 * 1024 * 1024


def __output_files(output_dir: Path) -> list[Path]:
    if output_dir.is_symlink():
        raise RuntimeError("Job output directory is a symbolic link")
    files = []
    for path in sorted(output_dir.rglob("*")):
        if path.is_symlink():
            raise RuntimeError("Job output directory contains a symbolic link")
        if path.is_file():
            files.append(path)
        elif not path.is_dir():
            raise RuntimeError("Job output directory contains an unsupported file")
    return files


def __run_scenario(command: list[str], job_id: str, extra_env: dict[str, str] | None = None) -> None:
    process = subprocess.Popen(
        command,
        cwd=settings.hand_fim_entry.parent,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        env={
            **os.environ, "PYTHONUNBUFFERED": "1",
            "TMPDIR": str(settings.hand_fim_input_dir / "jobs" / job_id / "tmp"),
            **(extra_env or {}),
        },
        start_new_session=True,
    )
    try:
        if process.stdout is None:
            raise RuntimeError("HAND-FIM subprocess output is unavailable")
        for line in process.stdout:
            append_job_log(job_id, line)
    except (RedisError, OSError, RuntimeError):
        # Stop mapping descendants too, before removing files they may be writing.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            logger.debug("HAND-FIM job %s subprocess group already exited", job_id)
        process.wait()
        raise
    finally:
        if process.stdout is not None:
            process.stdout.close()
    return_code = process.wait()
    if return_code:
        raise subprocess.CalledProcessError(return_code, command)


def __reject_existing_s3_keys(client: Any, bucket: str, keys: list[str]) -> None:
    for key in keys:
        try:
            client.head_object(Bucket=bucket, Key=key)
        except ClientError as error:
            code = error.response.get("Error", {}).get("Code")
            if code in {"404", "NoSuchKey", "NotFound"}:
                continue
            raise
        raise RuntimeError(
            f"S3 output object s3://{bucket}/{key} already exists; "
            "resubmit with overwrite=true to replace it"
        )


def __remove_job_path(root: Path, path: Path) -> None:
    for parent in path.relative_to(root).parents:
        directory = root / parent
        if directory.is_symlink():
            raise RuntimeError(f"Job cleanup parent is a symbolic link: {directory}")
    if path.is_symlink():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)
    elif path.exists():
        path.unlink()


def __cleanup_job(job_id: str, scenarios: HandFimParameters) -> None:
    input_root = settings.hand_fim_input_dir
    output_root = settings.hand_fim_output_dir
    paths = [
        (input_root, input_root / "jobs" / job_id),
        (output_root, output_root / "scenarios" / job_id),
    ]
    hucs = set()
    for scenario in scenarios.root:
        for reach in scenario.Reaches:
            hucs.add(reach.HUC)
            paths.append((
                input_root,
                input_root / f"{scenario.ScenarioID}__{reach.HUC}",
            ))
    paths.extend(
        (output_root, output_root / f"flood_{huc}") for huc in sorted(hucs)
    )
    errors = []
    for root, path in dict.fromkeys(paths):
        try:
            __remove_job_path(root, path)
        except (OSError, RuntimeError) as error:
            logger.exception("Failed to clean HAND-FIM job %s path %s", job_id, path)
            errors.append(str(error))
    if errors:
        raise RuntimeError("HAND-FIM job cleanup failed: " + "; ".join(errors))
    logger.info("Removed HAND-FIM job %s inputs, downloads, and outputs", job_id)


def __is_missing(error: ClientError) -> bool:
    return error.response.get("Error", {}).get("Code") in {"404", "NoSuchKey", "NotFound"}


def __load_manifest(client: Any, bucket: str, key: str) -> dict | None:
    try:
        response = client.get_object(Bucket=bucket, Key=key)
    except ClientError as error:
        if __is_missing(error):
            return None
        raise
    with response["Body"] as body:
        return json.loads(body.read(MAX_MANIFEST_BYTES + 1))


def __delete_keys(client: Any, bucket: str, keys: list[str]) -> None:
    for start in range(0, len(keys), 1000):
        client.delete_objects(Bucket=bucket, Delete={
            "Objects": [{"Key": key} for key in keys[start:start + 1000]], "Quiet": True,
        })


def __validated_manifest(path: Path, scenario_id: str, scenario_prefix: str) -> tuple[bytes, dict]:
    if not path.is_file() or path.is_symlink() or path.stat().st_size > MAX_MANIFEST_BYTES:
        raise RuntimeError(f"Streaming HAND-FIM produced no valid manifest for scenario {scenario_id!r}")
    content = path.read_bytes()
    manifest = json.loads(content)
    tiles = manifest.get("tiles")
    if manifest.get("format") != MANIFEST_FORMAT or manifest.get("scenario_id") != scenario_id \
            or not isinstance(tiles, list) or not tiles:
        raise RuntimeError(f"Streaming HAND-FIM manifest for scenario {scenario_id!r} is invalid")
    for tile in tiles:
        key = tile.get("key") if isinstance(tile, dict) else None
        if not isinstance(key, str) or not key.startswith(scenario_prefix) \
                or not key.endswith(".cog") or ".." in key.split("/"):
            raise RuntimeError(f"Streaming HAND-FIM manifest for scenario {scenario_id!r} has an invalid tile key")
    return content, manifest


def __hand_fim_streaming(
    job_id: str, scenarios: HandFimParameters, destination: HandFimJobOptions,
    scenario_file: Path, output_dir: Path,
) -> dict[str, Any]:
    """Stream tiles directly to S3, then publish one manifest per scenario."""
    if not settings.streaming_enabled:
        raise RuntimeError("Streaming execution is not configured on this worker")
    bucket = destination.output_bucket
    client = s3_client(UUID(job_id))
    publication_id = secrets.token_urlsafe(16)
    scenario_ids = [scenario.ScenarioID for scenario in scenarios.root]
    manifest_keys = {sid: output_file_key(destination.output_prefix, f"{sid}/manifest.json") for sid in scenario_ids}
    scenario_prefixes = {
        sid: output_file_key(destination.output_prefix, sid) + "/" for sid in scenario_ids
    }
    previous = {}
    if destination.overwrite:
        previous = {sid: __load_manifest(client, bucket, key) for sid, key in manifest_keys.items()}
    else:
        __reject_existing_s3_keys(client, bucket, list(manifest_keys.values()))
    published = []
    try:
        __run_scenario(
            [sys.executable, str(settings.hand_fim_entry), "stream-scenario", str(scenario_file), str(output_dir)],
            job_id,
            {
                **job_s3_environment(UUID(job_id), settings.stream_min_credential_seconds),
                "FIMBOX_STREAM_INPUT_BUCKET": settings.stream_input_bucket,
                "FIMBOX_STREAM_INPUT_PREFIX": settings.stream_input_prefix,
                "FIMBOX_STREAM_INPUT_UNSIGNED": "true" if settings.stream_input_unsigned else "false",
                "FIMBOX_STREAM_OUTPUT_BUCKET": bucket,
                "FIMBOX_STREAM_OUTPUT_PREFIX": destination.output_prefix,
                "FIMBOX_STREAM_TILE_SIZE": str(settings.stream_tile_size),
            },
        )
        manifests = {
            sid: __validated_manifest(output_dir / f"{sid}.manifest.json", sid, scenario_prefixes[sid])
            for sid in scenario_ids
        }
        refs = []
        # Publish manifests only after all tiles finish; existing tiles may already be overwritten.
        for sid, (content, manifest) in manifests.items():
            manifest["publication_id"] = publication_id
            content = json.dumps(manifest).encode()
            client.put_object(Bucket=bucket, Key=manifest_keys[sid], Body=content, ContentType="application/json")
            published.append(sid)
            refs.append(ScenarioManifest(
                ScenarioID=sid, key=manifest_keys[sid], tile_count=len(manifest["tiles"]),
                bounds=tuple(manifest["bounds_wgs84"]), tile_size=int(manifest["tile_size"]),
                resolution=min(float(huc["resolution"]) for huc in manifest["hucs"].values()),
            ))
    except BaseException:
        for sid in published:
            key = manifest_keys[sid]
            try:
                # Another job may have published since; only roll back a manifest that is still ours.
                current = __load_manifest(client, bucket, key)
                if not isinstance(current, dict) or current.get("publication_id") != publication_id:
                    continue
                if isinstance(previous.get(sid), dict):
                    client.put_object(
                        Bucket=bucket, Key=key, Body=json.dumps(previous[sid]).encode(),
                        ContentType="application/json",
                    )
                else:
                    client.delete_object(Bucket=bucket, Key=key)
            except (BotoCoreError, ClientError, ValueError):
                logger.exception("Could not roll back manifest %s after failed job %s", key, job_id)
        append_job_log(job_id, "Streaming job failed; tiles are retained because output paths are shared.\n")
        raise
    for sid, manifest in previous.items():
        if not isinstance(manifest, dict):
            continue
        scenario_root = output_file_key(destination.output_prefix, sid) + "/"
        current_keys = {tile["key"] for tile in manifests[sid][1]["tiles"]}
        stale = [
            tile["key"] for tile in manifest.get("tiles", [])
            if isinstance(tile, dict) and isinstance(tile.get("key"), str)
            and tile["key"].startswith(scenario_root)
            and tile["key"].endswith(".cog") and tile["key"] not in current_keys
        ]
        try:
            __delete_keys(client, bucket, stale)
        except (BotoCoreError, ClientError):
            logger.exception("Could not remove replaced tiles for scenario %s in job %s", sid, job_id)
    append_job_log(job_id, "Streaming HAND-FIM manifests published.\n")
    return StoredHandFimResult(
        outputs=[HandFimOutput(ScenarioID=sid) for sid in scenario_ids],
        execution_mode="streaming", scenario_manifests=refs, local_output_dir=None,
        **destination.model_dump(include={"output_bucket", "output_prefix"}),
    ).model_dump(mode="json")


@celery_app.task(name="fimbox_api.tasks.hand_fim")
def hand_fim(
    job_id: str,
    payload: list[dict[str, Any]],
    output_bucket: str | None = None,
    output_prefix: str = "",
    max_procs: int | None = None,
    overwrite: bool = False,
    execution_mode: str = "legacy",
) -> dict[str, Any]:
    """Run the container scenario command and upload its job-specific output tree."""
    job_id = str(UUID(job_id))
    scenarios = HandFimParameters.model_validate(payload)
    destination = HandFimJobOptions(
        output_bucket=output_bucket, output_prefix=output_prefix,
        max_procs=max_procs, overwrite=overwrite, execution_mode=execution_mode,
    )
    input_dir = settings.hand_fim_input_dir / "jobs" / job_id
    output_dir = settings.hand_fim_output_dir / "scenarios" / job_id
    scenario_file = input_dir / "scenario.json"
    try:
        input_dir.mkdir(parents=True, exist_ok=True)
        (input_dir / "tmp").mkdir(exist_ok=True)
        output_dir.mkdir(parents=True, exist_ok=True)
        clear_job_logs(job_id)
        append_job_log(job_id, "Starting HAND-FIM job.\n")
        scenario_file.write_text(scenarios.model_dump_json(), encoding="utf-8")
        logger.info("Starting HAND-FIM job %s", job_id)
        # A redelivered task must not mistake a previous attempt's files for success.
        for path in __output_files(output_dir):
            path.unlink()
        if destination.execution_mode == "streaming":
            return __hand_fim_streaming(job_id, scenarios, destination, scenario_file, output_dir)
        __run_scenario(
            [
                sys.executable,
                str(settings.hand_fim_entry),
                "scenario",
                str(scenario_file),
                str(destination.max_procs if destination.max_procs is not None else settings.hand_fim_max_procs),
                job_id,
            ],
            job_id,
        )
        outputs = []
        for scenario in scenarios.root:
            cog = output_dir / f"{scenario.ScenarioID}.cog"
            if not cog.is_file() or cog.stat().st_size == 0:
                raise RuntimeError(
                    f"HAND-FIM produced no COG for scenario {scenario.ScenarioID!r}"
                )
            outputs.append(
                HandFimOutput(
                    ScenarioID=scenario.ScenarioID,
                )
            )
        files = []
        client = s3_client(UUID(job_id)) if destination.output_bucket is not None else None
        output_paths = __output_files(output_dir)
        if client is not None and not destination.overwrite:
            __reject_existing_s3_keys(
                client,
                destination.output_bucket,
                [
                    output_file_key(
                        destination.output_prefix,
                        path.relative_to(output_dir).as_posix(),
                    )
                    for path in output_paths
                ],
            )
        for path in output_paths:
            relative_path = path.relative_to(output_dir).as_posix()
            key = (
                output_file_key(destination.output_prefix, relative_path)
                if destination.output_bucket is not None else None
            )
            content_type = (
                "image/tiff" if path.suffix.lower() in (".tif", ".tiff", ".cog")
                else mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            )
            if client is not None:
                client.upload_file(
                    str(path), destination.output_bucket, key,
                    ExtraArgs={"ContentType": content_type},
                )
            files.append(OutputFile(
                path=relative_path, key=key, size_bytes=path.stat().st_size,
            ))
        logger.info("Prepared HAND-FIM job %s outputs; cleaning local files", job_id)
        append_job_log(job_id, "HAND-FIM outputs prepared; cleaning local files.\n")
        return StoredHandFimResult(
            outputs=outputs, files=files,
            local_output_dir=None,
            **destination.model_dump(include={"output_bucket", "output_prefix"}),
        ).model_dump(mode="json")
    except (
        OSError, subprocess.CalledProcessError, RuntimeError,
        BotoCoreError, ClientError, S3UploadFailedError,
        StorageCredentialsError, RedisError,
    ):
        logger.exception("HAND-FIM job %s failed", job_id)
        raise
    finally:
        __cleanup_job(job_id, scenarios)
