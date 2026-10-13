import logging
import mimetypes
import os
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
from .models import HandFimParameters, HandFimJobOptions, OutputFile, StoredHandFimResult
from .job_logs import append_job_log, clear_job_logs
from .storage import StorageCredentialsError, output_file_key, s3_client

logger = logging.getLogger(__name__)


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


def __run_scenario(command: list[str], job_id: str) -> None:
    process = subprocess.Popen(
        command,
        cwd=settings.hand_fim_entry.parent,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        env={**os.environ, "PYTHONUNBUFFERED": "1"},
    )
    try:
        if process.stdout is None:
            raise RuntimeError("HAND-FIM subprocess output is unavailable")
        for line in process.stdout:
            append_job_log(job_id, line)
    except RedisError:
        process.terminate()
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


@celery_app.task(name="fimbox_api.tasks.hand_fim")
def hand_fim(
    job_id: str,
    payload: list[dict[str, Any]],
    output_bucket: str | None = None,
    output_prefix: str = "",
    max_procs: int | None = None,
    overwrite: bool = False,
) -> dict[str, Any]:
    """Run the container scenario command and upload its job-specific output tree."""
    job_id = str(UUID(job_id))
    scenarios = HandFimParameters.model_validate(payload)
    destination = HandFimJobOptions(
        output_bucket=output_bucket, output_prefix=output_prefix,
        max_procs=max_procs, overwrite=overwrite,
    )
    input_dir = settings.hand_fim_input_dir / "jobs" / job_id
    output_dir = settings.hand_fim_output_dir / "scenarios" / job_id
    input_dir.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)

    scenario_file = input_dir / "scenario.json"
    try:
        clear_job_logs(job_id)
        append_job_log(job_id, "Starting HAND-FIM job.\n")
        scenario_file.write_text(scenarios.model_dump_json(), encoding="utf-8")
        logger.info("Starting HAND-FIM job %s", job_id)
        # A redelivered task must not mistake a previous attempt's files for success.
        for path in __output_files(output_dir):
            path.unlink()
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
        logger.info("Completed HAND-FIM job %s", job_id)
        append_job_log(job_id, "HAND-FIM job completed.\n")
        return StoredHandFimResult(
            outputs=outputs, files=files,
            local_output_dir=str(output_dir),
            **destination.model_dump(exclude={"max_procs", "overwrite"}),
        ).model_dump(mode="json")
    except (
        OSError, subprocess.CalledProcessError, RuntimeError,
        BotoCoreError, ClientError, S3UploadFailedError,
        StorageCredentialsError, RedisError,
    ):
        logger.exception("HAND-FIM job %s failed", job_id)
        raise
