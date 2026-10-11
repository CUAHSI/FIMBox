import logging
import mimetypes
import subprocess
import sys
from pathlib import Path
from typing import Any
from uuid import UUID

from boto3.exceptions import S3UploadFailedError
from botocore.exceptions import BotoCoreError, ClientError

from schemas.hand_fim_schema import HandFimOutput

from .celery_app import celery_app
from .config import settings
from .models import HandFimParameters, OutputDestination, OutputFile, StoredHandFimResult
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


@celery_app.task(name="fimbox_api.tasks.hand_fim")
def hand_fim(
    job_id: str,
    payload: list[dict[str, Any]],
    output_bucket: str,
    output_prefix: str,
) -> dict[str, Any]:
    """Run the container scenario command and upload its job-specific output tree."""
    job_id = str(UUID(job_id))
    scenarios = HandFimParameters.model_validate(payload)
    destination = OutputDestination(
        output_bucket=output_bucket, output_prefix=output_prefix,
    )
    input_dir = settings.hand_fim_input_dir / "jobs" / job_id
    output_dir = settings.hand_fim_output_dir / "scenarios" / job_id
    input_dir.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)

    # User identifiers are metadata, never filenames or glob expressions.
    internal = scenarios.model_copy(deep=True)
    for index, scenario in enumerate(internal.root):
        scenario.ScenarioID = f"{job_id}-{index}"
    scenario_file = input_dir / "scenario.json"
    scenario_file.write_text(internal.model_dump_json(), encoding="utf-8")

    logger.info("Starting HAND-FIM job %s", job_id)
    try:
        # A redelivered task must not mistake a previous attempt's files for success.
        for path in __output_files(output_dir):
            path.unlink()
        subprocess.run(
            [
                sys.executable,
                str(settings.hand_fim_entry),
                "scenario",
                str(scenario_file),
                str(settings.hand_fim_max_procs),
                job_id,
            ],
            cwd=settings.hand_fim_entry.parent,
            check=True,
        )
        outputs = []
        for index, scenario in enumerate(scenarios.root):
            cog = output_dir / f"{job_id}-{index}.cog"
            if not cog.is_file() or cog.stat().st_size == 0:
                raise RuntimeError(
                    f"HAND-FIM produced no COG for scenario {scenario.ScenarioID!r}"
                )
            outputs.append(
                HandFimOutput(
                    ScenarioID=scenario.ScenarioID,
                    geotiff_url=f"/jobs/{job_id}/outputs/{index}.tif",
                )
            )
        files = []
        client = s3_client(UUID(job_id))
        for path in __output_files(output_dir):
            relative_path = path.relative_to(output_dir).as_posix()
            key = output_file_key(destination.output_prefix, UUID(job_id), relative_path)
            content_type = (
                "image/tiff" if path.suffix.lower() in (".tif", ".tiff", ".cog")
                else mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            )
            client.upload_file(
                str(path), destination.output_bucket, key,
                ExtraArgs={"ContentType": content_type},
            )
            files.append(OutputFile(
                path=relative_path, key=key, size_bytes=path.stat().st_size,
                download_url=f"/jobs/{job_id}/files/{len(files)}",
            ))
        logger.info("Completed HAND-FIM job %s", job_id)
        return StoredHandFimResult(
            outputs=outputs, files=files, **destination.model_dump(),
        ).model_dump(mode="json")
    except (
        OSError, subprocess.CalledProcessError, RuntimeError,
        BotoCoreError, ClientError, S3UploadFailedError,
        StorageCredentialsError,
    ):
        logger.exception("HAND-FIM job %s failed", job_id)
        raise
