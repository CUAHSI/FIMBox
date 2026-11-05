"""Offline preparation of a Parquet index for streaming HAND-FIM.

Converts one downloaded FIM HUC directory (``flood_<huc>/<huc>``) into the
layout read by ``fimserve.streamFIM``::

    dataset.parquet
    hucs/<huc>/huc.parquet                       branch grids and source raster URLs
    hucs/<huc>/reaches/<feature_id>.parquet      ratings and occupied blocks

No rasters are copied: huc.parquet references NOAA's original tiled GeoTIFFs in
the public HAND-FIM bucket, which jobs read with range requests. If the HUC is
not available locally, only the files read here are downloaded first. Publish
the output directory to the configured HydroShare resource. It is not used
during request execution.
"""

import csv
import math
import re
import shutil
from collections.abc import Callable
from pathlib import Path

import boto3
import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import rasterio
from botocore import UNSIGNED
from botocore.config import Config
from rasterio.windows import Window

DATASET_FORMAT = "fimbox-hand-stream"
DATASET_VERSION = 3
LAKE_FREE = -999
SOURCE_BUCKET = "ciroh-owp-hand-fim"
SOURCE_REGION = "us-east-2"
SOURCE_HTTPS_BASE = f"https://{SOURCE_BUCKET}.s3.{SOURCE_REGION}.amazonaws.com/"
CATCHMENTS_FILE = "gw_catchments_reaches_filtered_addedAttributes_{branch}.tif"
REM_FILE = "rem_zeroed_masked_{branch}.tif"
# REMs are read in place from the NOAA bucket, so only catchments are downloaded to build the index.
SOURCE_FILES = re.compile(
    r"(branch_ids\.csv|hydrotable\.csv|branches/(\d+)/(gw_catchments_reaches_filtered_addedAttributes_\2\.tif"
    r"|hydroTable_\2\.csv))"
)


class PreparationError(RuntimeError):
    """The source hydrofabric cannot be converted without changing results."""


def __branch_ids(huc_dir: Path, branches_file: Path | None) -> list[str]:
    path = branches_file or huc_dir / "branch_ids.csv"
    if path.is_file():
        with path.open(newline="") as handle:
            rows = [row for row in csv.reader(handle) if row and row[-1].strip()]
        return [row[-1].strip() for row in rows if row[-1].strip().isdigit()]
    branches = huc_dir / "branches"
    return sorted((p.name for p in branches.iterdir() if p.is_dir()), key=int) if branches.is_dir() else []


def __read_ratings(huc_dir: Path, branch: str) -> dict[tuple[int, str], dict]:
    """Reads non-lake rating rows for one branch in hydro-table order."""

    huc_table = huc_dir / "hydrotable.csv"
    path = huc_table if huc_table.is_file() else huc_dir / "branches" / branch / f"hydroTable_{branch}.csv"
    if not path.is_file():
        raise PreparationError(f"Branch {branch} has no hydro-table")
    ratings: dict[tuple[int, str], dict] = {}
    features: dict[int, set[str]] = {}
    with path.open(newline="") as handle:
        for position, row in enumerate(csv.DictReader(handle)):
            if path == huc_table and str(row.get("branch_id", "")).strip() != branch:
                continue
            if int(float(row["LakeID"])) != LAKE_FREE:
                continue
            hydroid = int(row["HydroID"])
            feature_id = str(row["feature_id"]).strip()
            features.setdefault(hydroid, set()).add(feature_id)
            rating = ratings.setdefault((hydroid, feature_id), {
                "feature_id": feature_id, "discharge_cms": [], "stage": [], "row_order": [],
            })
            rating["discharge_cms"].append(float(row["discharge_cms"]))
            rating["stage"].append(float(row["stage"]))
            rating["row_order"].append(position)
    for (hydroid, _feature), rating in ratings.items():
        if len(features[hydroid]) == 1:
            del rating["row_order"]
    return ratings


def __offsets(reference, dataset, branch: str) -> tuple[int, int]:
    ref, transform = reference.transform, dataset.transform
    if dataset.crs != reference.crs or not math.isclose(transform.a, ref.a) or not math.isclose(transform.e, ref.e) \
            or transform.b != ref.b or transform.d != ref.d:
        raise PreparationError(f"Branch {branch} grid is not compatible with branch 0")
    col = (transform.c - ref.c) / ref.a
    row = (transform.f - ref.f) / ref.e
    if abs(col - round(col)) > 1e-6 or abs(row - round(row)) > 1e-6:
        raise PreparationError(f"Branch {branch} is not pixel-aligned with branch 0")
    col, row = int(round(col)), int(round(row))
    if col < 0 or row < 0 or col + dataset.width > reference.width or row + dataset.height > reference.height:
        raise PreparationError(f"Branch {branch} extends beyond branch 0")
    return row, col


def __occupied_blocks(path: Path, hydroids: np.ndarray, block_size: int) -> dict[int, list[list[int]]]:
    """Maps each HydroID to the COG blocks containing it, reading one block at a time."""

    blocks: dict[int, list[list[int]]] = {}
    with rasterio.open(path) as dataset:
        for block_row in range(math.ceil(dataset.height / block_size)):
            for block_col in range(math.ceil(dataset.width / block_size)):
                window = Window(
                    block_col * block_size, block_row * block_size,
                    min(block_size, dataset.width - block_col * block_size),
                    min(block_size, dataset.height - block_row * block_size),
                )
                values = np.unique(dataset.read(1, window=window))
                for hydroid in np.intersect1d(values, hydroids):
                    blocks.setdefault(int(hydroid), []).append([block_row, block_col])
    return blocks


def __write_parquet(path: Path, rows: list[dict], schema: pa.Schema) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.Table.from_pylist(rows, schema=schema), path, compression="snappy")


def __write_huc(path: Path, metadata: dict) -> None:
    schema = pa.schema([
        ("huc", pa.string()), ("crs", pa.string()), ("transform", pa.list_(pa.float64(), 6)),
        ("width", pa.int64()), ("height", pa.int64()), ("block_size", pa.int32()),
        ("branch", pa.string()), ("rem", pa.string()), ("catchments", pa.string()),
        ("row_off", pa.int64()), ("col_off", pa.int64()),
        ("branch_width", pa.int64()), ("branch_height", pa.int64()),
    ])
    grid = {key: metadata[key] for key in ("huc", "crs", "transform", "width", "height", "block_size")}
    rows = [
        {
            **grid, "branch": branch, "rem": source["rem"], "catchments": source["catchments"],
            "row_off": source["row_off"], "col_off": source["col_off"],
            "branch_width": source["width"], "branch_height": source["height"],
        }
        for branch, source in metadata["branches"].items()
    ]
    __write_parquet(path, rows, schema)


def __write_reach(path: Path, huc: str, feature_id: str, entries: list[dict]) -> None:
    schema = pa.schema([
        ("huc", pa.string()), ("feature_id", pa.string()), ("branch", pa.string()),
        ("hydroid", pa.int64()), ("discharge_cms", pa.list_(pa.float64())),
        ("stage", pa.list_(pa.float64())), ("row_order", pa.list_(pa.int64())),
        ("blocks", pa.list_(pa.list_(pa.int32(), 2))),
    ])
    rows = [
        {
            "huc": huc, "feature_id": feature_id, "branch": entry["branch"],
            "hydroid": rating["id"], "discharge_cms": rating["discharge_cms"],
            "stage": rating["stage"], "row_order": rating.get("row_order"),
            "blocks": entry["blocks"],
        }
        for entry in entries for rating in entry["hydroids"]
    ]
    __write_parquet(path, rows, schema)


def __dataset_root(output_dir: Path, block_size: int, fim_version: str) -> None:
    expected = {
        "format": DATASET_FORMAT, "version": DATASET_VERSION,
        "block_size": block_size, "fim_version": fim_version,
    }
    path = output_dir / "dataset.parquet"
    if (output_dir / "dataset.json").exists():
        raise PreparationError("JSON metadata is no longer supported; prepare into a new output directory")
    if path.is_file():
        try:
            rows = pq.read_table(path).to_pylist()
        except (pa.ArrowException, OSError) as error:
            raise PreparationError(f"{path} is not valid Parquet") from error
        if rows != [expected]:
            raise PreparationError(f"{path} describes a different dataset version or block size")
    else:
        __write_parquet(path, [expected], pa.schema([
            ("format", pa.string()), ("version", pa.int32()),
            ("block_size", pa.int32()), ("fim_version", pa.string()),
        ]))


def __source_prefix(huc: str, fim_version: str) -> str:
    if len(huc) != 8 or not huc.isdigit():
        raise PreparationError("HUC must be an eight-digit identifier")
    if not re.fullmatch(r"\d+(\.\d+)*", fim_version):
        raise PreparationError("FIM version must be dotted digits, e.g. 4.5.2.11")
    return f"hand_fim_{fim_version.replace('.', '_')}/{huc}/"


def source_raster_url(huc: str, branch: str, file_template: str, fim_version: str) -> str:
    return SOURCE_HTTPS_BASE + __source_prefix(huc, fim_version) + f"branches/{branch}/" + file_template.format(branch=branch)


def download_huc(
    huc: str,
    huc_dir: Path,
    fim_version: str = "4.5.2.11",
    client=None,
    log: Callable[[str], None] = print,
) -> None:
    """
    Downloads the hydrofabric files read by prepare_huc unless already present.

    REM rasters are not downloaded; the prepared index references them in NOAA's
    bucket. The download is skipped when branch 0 catchments exist in huc_dir. Otherwise
    missing files are fetched from the public NOAA HAND-FIM bucket; files are
    renamed into place only when complete, so an interrupted run resumes.

    Arguments:
        huc: Eight-digit HUC identifier.
        huc_dir: Destination flood_<huc>/<huc> directory.
        fim_version: Source hydrofabric version, e.g. 4.5.2.11.
        client: Optional S3 client; defaults to an unsigned boto3 client.
        log: Progress output function.
    Returns:
        None
    """

    branch0 = huc_dir / "branches" / "0"
    if (branch0 / CATCHMENTS_FILE.format(branch="0")).is_file():
        log(f"HUC {huc} found at {huc_dir}; skipping download")
        return
    prefix = __source_prefix(huc, fim_version)
    client = client or boto3.client(
        "s3", region_name=SOURCE_REGION, config=Config(signature_version=UNSIGNED),
    )
    keys = [
        item["Key"][len(prefix):]
        for page in client.get_paginator("list_objects_v2").paginate(Bucket=SOURCE_BUCKET, Prefix=prefix)
        for item in page.get("Contents", [])
    ]
    wanted = [key for key in keys if SOURCE_FILES.fullmatch(key)]
    if "hydrotable.csv" in wanted:
        wanted = [key for key in wanted if not key.endswith(".csv") or "/" not in key]
    if not wanted:
        raise PreparationError(f"No hydrofabric found at s3://{SOURCE_BUCKET}/{prefix}")
    for key in wanted:
        destination = huc_dir / key
        if destination.is_file():
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        partial = destination.with_name(destination.name + ".part")
        log(f"Downloading s3://{SOURCE_BUCKET}/{prefix}{key}")
        client.download_file(SOURCE_BUCKET, prefix + key, str(partial))
        partial.replace(destination)


def prepare_huc(
    huc: str,
    huc_dir: Path,
    output_dir: Path,
    block_size: int = 512,
    fim_version: str = "4.5.2.11",
    branches_file: Path | None = None,
    log: Callable[[str], None] = print,
) -> dict:
    """
    Converts one HUC into per-reach Parquet block/rating tables that reference NOAA's source rasters.

    Arguments:
        huc: Eight-digit HUC identifier.
        huc_dir: Downloaded hydrofabric directory containing ``branches/``.
        output_dir: Dataset root to create or extend.
        block_size: Index granularity in pixels; reads are windowed on this grid.
        fim_version: Source hydrofabric version recorded in dataset.parquet.
        branches_file: Optional HUC,branch CSV; defaults to branch_ids.csv.
        log: Progress output function.
    Returns:
        The written HUC metadata.
    """

    if len(huc) != 8 or not huc.isdigit():
        raise PreparationError("HUC must be an eight-digit identifier")
    if block_size <= 0 or block_size % 16:
        raise PreparationError("Block size must be a positive multiple of 16")
    __dataset_root(output_dir, block_size, fim_version)
    huc_out = output_dir / "hucs" / huc
    if huc_out.exists():
        shutil.rmtree(huc_out)
    branch_sources = {}
    for branch in __branch_ids(huc_dir, branches_file):
        folder = huc_dir / "branches" / branch
        catchments = folder / CATCHMENTS_FILE.format(branch=branch)
        local_rem = folder / REM_FILE.format(branch=branch)
        rem = local_rem if local_rem.is_file() else "/vsicurl/" + source_raster_url(
            huc, branch, REM_FILE, fim_version,
        )
        if catchments.is_file():
            branch_sources[branch] = (rem, catchments)
        else:
            log(f"Skipping branch {branch}: catchments raster is missing")
    if "0" not in branch_sources:
        raise PreparationError("Branch 0 is required as the HUC reference grid")

    reaches: dict[str, list[dict]] = {}
    branches = {}
    with rasterio.open(branch_sources["0"][0]) as reference:
        metadata = {
            "huc": huc, "crs": reference.crs.to_wkt(), "transform": list(reference.transform)[:6],
            "width": reference.width, "height": reference.height, "block_size": block_size,
        }
        for branch, (rem, catchments) in branch_sources.items():
            try:
                rem_dataset = rasterio.open(rem)
            except rasterio.errors.RasterioIOError as error:
                raise PreparationError(f"Branch {branch} REM could not be opened: {error}") from error
            with rem_dataset, rasterio.open(catchments) as catchment_dataset:
                row_off, col_off = __offsets(reference, rem_dataset, branch)
                if __offsets(reference, catchment_dataset, branch) != (row_off, col_off) \
                        or rem_dataset.shape != catchment_dataset.shape:
                    raise PreparationError(f"Branch {branch} REM and catchments are not aligned")
                shape = rem_dataset.shape
            log(f"Indexing branch {branch}")
            branches[branch] = {
                "rem": source_raster_url(huc, branch, REM_FILE, fim_version),
                "catchments": source_raster_url(huc, branch, CATCHMENTS_FILE, fim_version),
                "row_off": row_off, "col_off": col_off, "height": shape[0], "width": shape[1],
            }
            ratings = __read_ratings(huc_dir, branch)
            blocks = __occupied_blocks(
                catchments,
                np.array(sorted({hydroid for hydroid, _ in ratings}), dtype=np.int64), block_size,
            )
            per_feature: dict[str, dict] = {}
            for (hydroid, _feature), rating in ratings.items():
                entry = per_feature.setdefault(rating["feature_id"], {
                    "branch": branch, "hydroids": [], "blocks": set(),
                })
                entry["hydroids"].append({
                    "id": hydroid, "discharge_cms": rating["discharge_cms"], "stage": rating["stage"],
                    **({"row_order": rating["row_order"]} if "row_order" in rating else {}),
                })
                entry["blocks"].update(tuple(block) for block in blocks.get(hydroid, []))
            for feature_id, entry in per_feature.items():
                entry["blocks"] = [list(block) for block in sorted(entry["blocks"])]
                reaches.setdefault(feature_id, []).append(entry)
    metadata["branches"] = branches
    for feature_id, entries in reaches.items():
        __write_reach(huc_out / "reaches" / f"{feature_id}.parquet", huc, feature_id, entries)
    # Written last so a partially prepared HUC is never discoverable.
    __write_huc(huc_out / "huc.parquet", metadata)
    log(f"Prepared HUC {huc}: {len(branches)} branches, {len(reaches)} reaches")
    return metadata


if __name__ == "__main__":
    from typing import Optional

    import typer

    def main(
        huc: str = typer.Argument(..., help="Eight-digit HUC identifier."),
        output_dir: Optional[Path] = typer.Argument(
            None, help="Local dataset root to create or extend. Omit when using --output-bucket.",
        ),
        huc_dir: Optional[Path] = typer.Option(
            None, help="Local flood_<huc>/<huc> directory; downloaded if missing. "
                       "Defaults to output/flood_<huc>/<huc>.",
        ),
        block_size: int = typer.Option(512, help="COG block size and index granularity."),
        fim_version: str = typer.Option("4.5.2.11", help="Source hydrofabric version."),
        branches_file: Optional[Path] = typer.Option(None, help="Optional HUC,branch CSV."),
        output_bucket: Optional[str] = typer.Option(
            None, help="Publish to S3 next to the other input files; uses ambient AWS credentials.",
        ),
        output_prefix: str = typer.Option("", help="S3 key prefix of the input dataset."),
    ) -> None:
        if bool(output_dir) == bool(output_bucket):
            raise typer.BadParameter("Provide exactly one of OUTPUT_DIR or --output-bucket")
        if output_bucket:
            import prepare_streaming_collection as collection

            if branches_file or huc_dir:
                raise typer.BadParameter("--huc-dir and --branches-file apply only to local output")
            client = boto3.client("s3")
            source = collection.source_s3_client()
            collection.initialize_collection(
                output_client=client, output_bucket=output_bucket, output_prefix=output_prefix,
                available_hucs=collection.list_hucs(source, fim_version), fim_version=fim_version, block_size=block_size,
            )
            collection.prepare_single_huc(
                source_client=source, output_client=client,
                output_bucket=output_bucket, output_prefix=output_prefix,
                huc=huc, fim_version=fim_version, block_size=block_size,
            )
            return
        huc_dir = huc_dir or Path("output") / f"flood_{huc}" / huc
        download_huc(huc, huc_dir, fim_version)
        prepare_huc(huc, huc_dir, output_dir, block_size, fim_version, branches_file)

    typer.run(main)
