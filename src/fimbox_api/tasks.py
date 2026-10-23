import json
import logging
import mimetypes
import os
import secrets
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
from pathlib import Path
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from boto3.exceptions import S3UploadFailedError
from botocore.exceptions import BotoCoreError, ClientError
from redis.exceptions import RedisError

from schemas.hand_fim_schema import HandFimOutput

from .celery_app import celery_app
from .config import settings
from .models import (
    HandFimParameters, HandFimJobOptions, OutputFile, ScenarioManifest, ScenarioParity, StoredHandFimResult,
)
from .job_logs import append_job_log, clear_job_logs
from .parity import compare_scenario_outputs
from .storage import StorageCredentialsError, job_s3_environment, output_file_key, s3_client

logger = logging.getLogger(__name__)
MANIFEST_FORMAT = "fimbox-scenario-tiles"
MAX_MANIFEST_BYTES = 8 * 1024 * 1024


def __write_output_readme(
    output_dir: Path, job_id: str, scenarios: HandFimParameters, destination: HandFimJobOptions,
) -> None:
    streamed = destination.execution_mode == "streaming"
    max_procs = 1 if streamed else (
        destination.max_procs if destination.max_procs is not None else settings.hand_fim_max_procs
    )
    scenario_ids = [scenario.ScenarioID for scenario in scenarios.root]
    output_keys = [
        output_file_key(destination.output_prefix, f"{sid}/manifest.json" if streamed else f"{sid}.cog")
        for sid in scenario_ids
    ]
    command = [
        "fimbox", "submit", "--body", "scenarios.json",
        "--execution-mode", destination.execution_mode, "--max-procs", str(max_procs),
    ]
    if destination.output_bucket is not None:
        command.extend(["--output-bucket", destination.output_bucket, "--output-prefix", "NEW_OUTPUT_PREFIX"])
    layout = (
        "Each scenario has a `<ScenarioID>/manifest.json` listing its COG tile keys, geographic bounds, "
        "and grid metadata. Tiles live at `<ScenarioID>/<huc>/<row>_<col>.cog`. "
        "HUCs retain their native grids; the map displays the union of flooded cells.\n"
        if streamed else
        "Each scenario produces `<ScenarioID>.cog`, a Cloud Optimized GeoTIFF (COG). "
        "Additional TIFFs and processing files may also be present. "
        "Legacy mosaics retain the first HUC's value where HUCs overlap.\n"
    )
    lines = [
        "# HAND-FIM scenario outputs\n\n",
        "These flood inundation extents were computed by FIMBox using the Height Above Nearest Drainage "
        "(HAND) method and NOAA/OWP inundation-mapping tooling. They represent the supplied reach "
        "streamflows, not water depth, forecast probabilities, or a guarantee of flooding. "
        "Flooded cells have value 1; dry/nodata cells are transparent in the FIMBox map. "
        "Consult each raster's CRS, transform, and nodata metadata for GIS use.\n\n",
        "## How this run was configured\n\n",
        f"- Job ID (provenance only): `{job_id}`\n",
        f"- Worker run started (UTC): `{datetime.now(timezone.utc).isoformat()}`\n",
        f"- Execution mode: `{destination.execution_mode}`\n",
        f"- Effective maximum processes: `{max_procs}`\n",
        f"- Overwrite enabled: `{str(destination.overwrite).lower()}`\n",
        f"- Scenario count: `{len(scenario_ids)}`\n",
        f"- Reach entries: `{sum(len(scenario.Reaches) for scenario in scenarios.root)}`\n",
    ]
    if destination.output_bucket is not None:
        root = f"s3://{destination.output_bucket}/"
        if destination.output_prefix.strip("/"):
            root += destination.output_prefix.strip("/") + "/"
        lines.append(f"- Output location: `{root}`\n")
    if streamed:
        lines.extend([
            f"- Prepared input hydrofabric: `s3://{settings.stream_input_bucket}/"
            f"{settings.stream_input_prefix.strip('/')}`\n",
            f"- Processing tile size: `{settings.stream_tile_size}` pixels\n",
            "\nStreaming reads only intersecting hydrofabric blocks and uploads COG tiles directly to S3.\n",
        ])
    else:
        lines.append("\nLegacy processing downloads HUC inputs and mosaics scenario results into COGs.\n")
    lines.extend([
        "\n## Files and inputs\n\n",
        "- [scenarios.json](scenarios.json): the validated input scenario array, reusable as input. "
        "`ScenarioID` names each scenario; `Reaches` contains eight-digit `HUC` codes, NWM `ReachID` "
        "identifiers, and `Streamflow` in cubic meters per second (m^3/s).\n",
        "- [README.md](README.md): this run description.\n\n",
        layout,
        "\nScenario outputs relative to this directory:\n\n",
    ])
    lines.extend(
        f"- `{sid}/manifest.json`\n" if streamed else f"- `{sid}.cog`\n" for sid in scenario_ids
    )
    lines.extend([
        "\n## Rerun\n\n",
        "Install FIMBox, configure its API URL, and authenticate with HydroShare as required by that API. "
        "Download `scenarios.json`, then run from its directory:\n\n",
        f"```sh\n{shlex.join(command)}\n```\n\n",
        "Replace `NEW_OUTPUT_PREFIX` with a new destination prefix to avoid replacing these outputs. "
        "Rerunning incurs computation costs. The receiving worker must support this execution mode; "
        "streaming also requires the prepared hydrofabric configuration above. "
        "Exact reproduction depends on using the same source data and software environment; "
        "this README does not capture package versions or immutable dataset versions.\n\n",
        "## View retained outputs\n\n",
    ])
    if destination.output_bucket is not None:
        paths = [f"s3://{destination.output_bucket}/{key}" for key in output_keys]
        lines.extend([
            "With an authenticated, connected FIMBox SDK `api_client` and `fimbox[map]` installed:\n\n",
            "```python\nfrom fimbox_map import output_map\n\n",
            f"paths = {json.dumps(paths, indent=4)}\ndisplay(output_map(api_client, paths))\n```\n\n",
            "These paths do not require unexpired job metadata. Retained objects and current HydroShare "
            "read permission are required. Streaming display may require zooming in. "
            "Use separate prefixes for independent or concurrent runs. Overwrites are not atomic; "
            "failed runs can leave partial outputs or mixed old/new tiles.\n",
        ])
    else:
        lines.append("Local-only runs remove all output files during worker cleanup; use S3 to retain them.\n")
    (output_dir / "README.md").write_text("".join(lines), encoding="utf-8")


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
    scenarios_path = output_dir / "scenarios.json"
    scenarios_key = output_file_key(destination.output_prefix, scenarios_path.name)
    readme_path = output_dir / "README.md"
    readme_key = output_file_key(destination.output_prefix, readme_path.name)
    previous = {}
    if destination.overwrite:
        previous = {sid: __load_manifest(client, bucket, key) for sid, key in manifest_keys.items()}
    else:
        __reject_existing_s3_keys(client, bucket, [*manifest_keys.values(), scenarios_key, readme_key])
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
        client.upload_file(
            str(scenarios_path), bucket, scenarios_key,
            ExtraArgs={"ContentType": "application/json"},
        )
        client.upload_file(
            str(readme_path), bucket, readme_key,
            ExtraArgs={"ContentType": "text/markdown"},
        )
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
        files=[
            OutputFile(path=path.name, key=key, size_bytes=path.stat().st_size)
            for path, key in ((scenarios_path, scenarios_key), (readme_path, readme_key))
        ],
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
        (output_dir / "scenarios.json").write_text(scenarios.model_dump_json(), encoding="utf-8")
        __write_output_readme(output_dir, job_id, scenarios, destination)
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


def __legacy_scenarios(client: Any, bucket: str, prefix: str) -> HandFimParameters:
    key = output_file_key(prefix, "scenarios.json")
    try:
        response = client.get_object(Bucket=bucket, Key=key)
    except ClientError as error:
        if __is_missing(error):
            raise RuntimeError(f"Legacy run has no s3://{bucket}/{key}") from None
        raise
    with response["Body"] as body:
        content = body.read(MAX_MANIFEST_BYTES + 1)
    if len(content) > MAX_MANIFEST_BYTES:
        raise RuntimeError(f"Legacy s3://{bucket}/{key} is too large")
    try:
        return HandFimParameters.model_validate_json(content)
    except ValueError:
        raise RuntimeError(f"Legacy s3://{bucket}/{key} is not a valid scenario list") from None


def __require_s3_keys(client: Any, bucket: str, keys: list[str]) -> None:
    for key in keys:
        try:
            client.head_object(Bucket=bucket, Key=key)
        except ClientError as error:
            if __is_missing(error):
                raise RuntimeError(f"Legacy output s3://{bucket}/{key} does not exist") from None
            raise


def __compare_scenario(
    client: Any, legacy_bucket: str, legacy_key: str, bucket: str,
    manifest_ref: ScenarioManifest, diff_key: str, work_dir: Path,
) -> ScenarioParity:
    sid = manifest_ref.ScenarioID
    manifest = __load_manifest(client, bucket, manifest_ref.key)
    scenario_prefix = manifest_ref.key.removesuffix("manifest.json")
    tiles = manifest.get("tiles") if isinstance(manifest, dict) else None
    if not isinstance(tiles, list):
        raise RuntimeError(f"Streaming manifest for scenario {sid!r} is unavailable")
    keys = [tile.get("key") if isinstance(tile, dict) else None for tile in tiles]
    if any(
        not isinstance(key, str) or not key.startswith(scenario_prefix)
        or not key.endswith(".cog") or ".." in key.split("/") for key in keys
    ):
        raise RuntimeError(f"Streaming manifest for scenario {sid!r} has an invalid tile key")
    scenario_dir = work_dir / sid
    scenario_dir.mkdir()
    legacy_path = scenario_dir / "legacy.cog"
    client.download_file(legacy_bucket, legacy_key, str(legacy_path))
    tile_paths = []
    for index, key in enumerate(keys):
        tile_paths.append(scenario_dir / f"tile-{index}.cog")
        client.download_file(bucket, key, str(tile_paths[-1]))
    diff_path = scenario_dir / "diff.tif"
    report = compare_scenario_outputs(legacy_path, tile_paths, diff_path)
    client.upload_file(str(diff_path), bucket, diff_key, ExtraArgs={"ContentType": "image/tiff"})
    missing = [str(reach) for reach in manifest.get("missing_reaches", [])]
    return ScenarioParity(
        ScenarioID=sid, legacy_key=legacy_key, diff_key=diff_key, missing_reaches=missing,
        matches=report["different_cells"] == 0 and not missing,
        **{name: report[name] for name in (
            "crs", "legacy_flooded_cells", "streaming_flooded_cells", "legacy_only_cells",
            "streaming_only_cells", "different_cells", "sample_differences",
            "grid_shift_rows", "grid_shift_columns", "unaligned_different_cells",
        )},
    )


@celery_app.task(name="fimbox_api.tasks.hand_fim_parity")
def hand_fim_parity(
    job_id: str,
    legacy_bucket: str,
    legacy_prefix: str,
    output_bucket: str,
    output_prefix: str = "",
    overwrite: bool = False,
) -> dict[str, Any]:
    """Rerun a retained legacy run's scenarios with streaming, then diff each scenario's flood mask."""
    job_id = str(UUID(job_id))
    client = s3_client(UUID(job_id))
    scenarios = __legacy_scenarios(client, legacy_bucket, legacy_prefix)
    scenario_ids = [scenario.ScenarioID for scenario in scenarios.root]
    legacy_keys = {sid: output_file_key(legacy_prefix, f"{sid}.cog") for sid in scenario_ids}
    diff_keys = {sid: output_file_key(output_prefix, f"parity/{sid}.diff.tif") for sid in scenario_ids}
    report_key = output_file_key(output_prefix, "parity/report.json")
    __require_s3_keys(client, legacy_bucket, list(legacy_keys.values()))
    if not overwrite:
        __reject_existing_s3_keys(client, output_bucket, [report_key, *diff_keys.values()])
    result = StoredHandFimResult.model_validate(hand_fim(
        job_id, scenarios.model_dump(mode="json"), output_bucket, output_prefix, None, overwrite, "streaming",
    ))
    append_job_log(job_id, "Comparing streaming tiles with legacy scenario outputs.\n")
    with tempfile.TemporaryDirectory(prefix=f"parity-{job_id}-", dir=settings.hand_fim_input_dir) as work:
        comparisons = []
        for manifest_ref in result.scenario_manifests:
            sid = manifest_ref.ScenarioID
            comparison = __compare_scenario(
                client, legacy_bucket, legacy_keys[sid], output_bucket,
                manifest_ref, diff_keys[sid], Path(work),
            )
            comparisons.append(comparison)
            append_job_log(job_id, (
                f"Scenario {sid}: {'MATCH' if comparison.matches else 'MISMATCH'} "
                f"({comparison.legacy_only_cells} legacy-only, {comparison.streaming_only_cells} "
                f"streaming-only cells, {len(comparison.missing_reaches)} missing reaches, "
                f"grid shift {comparison.grid_shift_rows} rows/{comparison.grid_shift_columns} columns).\n"
            ))
        report = json.dumps({
            "legacy_bucket": legacy_bucket, "legacy_prefix": legacy_prefix,
            "scenarios": [comparison.model_dump(mode="json") for comparison in comparisons],
        }, indent=2).encode()
        client.put_object(Bucket=output_bucket, Key=report_key, Body=report, ContentType="application/json")
        result.files.append(OutputFile(path="parity/report.json", key=report_key, size_bytes=len(report)))
        result.files.extend(
            OutputFile(path=f"parity/{sid}.diff.tif", key=diff_keys[sid],
                       size_bytes=(Path(work) / sid / "diff.tif").stat().st_size)
            for sid in (comparison.ScenarioID for comparison in comparisons)
        )
    result.parity = comparisons
    return result.model_dump(mode="json")
