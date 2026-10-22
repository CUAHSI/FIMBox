"""Bounded-memory HAND-FIM inundation from a prepared cloud-optimized hydrofabric.

This module is an opt-in alternative to ``runFIM.runfim``. Instead of
downloading a complete HUC and running the NOAA mosaic wrapper over whole
branch rasters, it reads a reach index prepared offline (see
``prepare_streaming_dataset.py``), fetches only the native-resolution REM and
catchment COG blocks occupied by the requested reaches, and emits fixed-size
output COG tiles one at a time.

Scientific behavior mirrors the pinned NOAA inundation-mapping code
(``tools/inundation.py`` and ``tools/overlapping_inundation.py`` at
9d39cb85ae789b30964a82c19dd850d29fccf5df) followed by FIMBox's binary cleaning:

* Each HydroID stage is ``float32(round(np.interp(Q, discharge_cms, stage), 4))``
  using that HydroID's non-lake rating rows for requested features in table
  order. For shared HydroIDs, Q is the forecast for the first matching row's
  feature, matching NOAA's first interpolated stage.
* A branch pixel is flooded when its catchment HydroID has a stage, its REM is
  ``>= 0``, and ``stage - REM > 0``.
* Branch mosaics take the maximum signed HydroID, so a pixel is flooded when any
  branch floods it. Output tiles are 1 (flooded) and NaN (otherwise).

Tiles use the native grid of each HUC's branch 0; HUCs are never resampled to a
shared grid. Overlapping tiles from neighboring HUCs combine as a flood union.

The module depends only on NumPy and Rasterio so it can be tested without the
fimserve or NOAA packages. Callers supply object access functions.
"""

import json
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

import numpy as np
import rasterio
from rasterio.crs import CRS
from rasterio.io import MemoryFile
from rasterio.shutil import copy as raster_copy
from rasterio.transform import Affine, array_bounds
from rasterio.warp import transform_bounds
from rasterio.windows import Window

DATASET_FORMAT = "fimbox-hand-stream"
DATASET_VERSION = 1
MANIFEST_FORMAT = "fimbox-scenario-tiles"
MANIFEST_VERSION = 1
DEFAULT_TILE_SIZE = 2048
DEFAULT_MAX_BLOCKS = 100_000
DEFAULT_MAX_TILES = 10_000
# Applied by callers that open remote rasters; values are deliberately small.
GDAL_STREAMING_OPTIONS = {
    "GDAL_CACHEMAX": 64,
    "VSI_CACHE": False,
    "GDAL_DISABLE_READDIR_ON_OPEN": "EMPTY_DIR",
    "CPL_VSIL_CURL_ALLOWED_EXTENSIONS": ".tif",
    "GDAL_HTTP_MERGE_CONSECUTIVE_RANGES": "YES",
    "GDAL_HTTP_MAX_RETRY": 3,
    "GDAL_HTTP_RETRY_DELAY": 1,
    "GDAL_HTTP_TIMEOUT": 60,
    "GDAL_NUM_THREADS": 1,
}

ReadJson = Callable[[str], dict | None]
RasterPath = Callable[[str], str]
WriteTile = Callable[[str, bytes], str]


class StreamingInputError(RuntimeError):
    """The prepared dataset or request cannot produce a valid streaming result."""


@dataclass
class BranchPlan:
    branch: str
    rem: str
    catchments: str
    row_off: int
    col_off: int
    width: int
    height: int
    stages: dict[int, np.float32] = field(default_factory=dict)
    blocks: set[tuple[int, int]] = field(default_factory=set)

    def lookup(self) -> tuple[np.ndarray, np.ndarray]:
        hydroids = np.array(sorted(self.stages), dtype=np.int64)
        stages = np.array([self.stages[h] for h in hydroids], dtype=np.float32)
        return hydroids, stages


def stage_for_discharge(
    discharge: float, discharge_cms: Iterable[float], stage: Iterable[float],
) -> np.float32:
    """
    Interpolates one HydroID stage exactly as NOAA's hydro-table subset does.

    Arguments:
        discharge: Requested discharge in cubic meters per second.
        discharge_cms: Rating-curve discharges in hydro-table row order.
        stage: Rating-curve stages in hydro-table row order.
    Returns:
        The stage rounded to four decimals and stored as float32.
    """

    interpolated = np.interp(
        [discharge], np.asarray(discharge_cms, dtype=float), np.asarray(stage, dtype=float),
    )
    return np.float32(round(interpolated[0], 4))


def flooded_cells(
    rem: np.ndarray, catchments: np.ndarray, hydroids: np.ndarray, stages: np.ndarray,
) -> np.ndarray:
    """
    Vectorized equivalent of NOAA's per-pixel ``__go_fast_mapping`` flood test.

    Arguments:
        rem: Relative elevation window.
        catchments: HydroID window aligned with ``rem``.
        hydroids: Sorted HydroIDs that have a stage.
        stages: float32 stages aligned with ``hydroids``.
    Returns:
        A boolean array that is True where the pixel has positive depth.
    """

    if hydroids.size == 0:
        return np.zeros(rem.shape, dtype=bool)
    index = np.minimum(np.searchsorted(hydroids, catchments), hydroids.size - 1)
    known = hydroids[index] == catchments
    with np.errstate(invalid="ignore"):
        return known & (rem >= 0) & (stages[index] > rem)


def first_discharges(reaches: Iterable[dict]) -> dict[str, dict[str, float]]:
    """
    Groups scenario reaches by HUC, keeping each feature ID's first discharge.

    Arguments:
        reaches: Scenario reach dictionaries with HUC, ReachID, and Streamflow.
    Returns:
        A mapping of HUC to feature ID to discharge.
    """

    by_huc: dict[str, dict[str, float]] = {}
    for reach in reaches:
        by_huc.setdefault(reach["HUC"], {}).setdefault(
            str(reach["ReachID"]), float(reach["Streamflow"]),
        )
    return by_huc


def __require(condition: bool, message: str) -> None:
    if not condition:
        raise StreamingInputError(message)


def __validate_huc(huc: str, metadata: dict) -> None:
    __require(metadata.get("huc") == huc, f"HUC {huc} index does not match its location")
    for name in ("crs", "transform", "width", "height", "block_size", "branches"):
        __require(name in metadata, f"HUC {huc} index is missing {name!r}")
    __require(len(metadata["transform"]) == 6, f"HUC {huc} transform is invalid")
    __require(metadata["block_size"] > 0, f"HUC {huc} block size is invalid")


def plan_huc(
    huc: str, discharges: dict[str, float], read_json: ReadJson,
) -> tuple[dict, list[BranchPlan], list[str]]:
    """
    Resolves requested reaches to branches, stages, and occupied COG blocks.

    Arguments:
        huc: Eight-digit HUC identifier.
        discharges: Feature ID to discharge for reaches in this HUC.
        read_json: Reads a dataset-relative JSON object, returning None if absent.
    Returns:
        The HUC metadata, branch plans that contain flooded candidates, and
        the requested feature IDs that are absent from the prepared dataset.
    """

    metadata = read_json(f"hucs/{huc}/huc.json")
    if metadata is None:
        raise StreamingInputError(f"HUC {huc} is not in the prepared streaming dataset")
    __validate_huc(huc, metadata)
    plans: dict[str, BranchPlan] = {}
    ratings: dict[tuple[str, int], list[tuple[float, dict]]] = {}
    missing = []
    for feature_id, discharge in discharges.items():
        reach = read_json(f"hucs/{huc}/reaches/{feature_id}.json")
        if reach is None:
            missing.append(feature_id)
            continue
        __require(str(reach.get("feature_id")) == feature_id, f"Reach {feature_id} index is inconsistent")
        for entry in reach.get("branches", []):
            branch = str(entry["branch"])
            source = metadata["branches"].get(branch)
            __require(source is not None, f"Reach {feature_id} references unknown branch {branch}")
            plan = plans.setdefault(branch, BranchPlan(
                branch=branch, rem=source["rem"], catchments=source["catchments"],
                row_off=int(source["row_off"]), col_off=int(source["col_off"]),
                width=int(source["width"]), height=int(source["height"]),
            ))
            for rating in entry.get("hydroids", []):
                ratings.setdefault((branch, int(rating["id"])), []).append((discharge, rating))
            plan.blocks.update((int(row), int(col)) for row, col in entry.get("blocks", []))
    for (branch, hydroid), curves in ratings.items():
        if len(curves) == 1:
            discharge, rating = curves[0]
            plans[branch].stages[hydroid] = stage_for_discharge(
                discharge, rating["discharge_cms"], rating["stage"],
            )
        else:
            rows = []
            for discharge, rating in curves:
                order = rating.get("row_order")
                __require(
                    isinstance(order, list)
                    and len(order) == len(rating["discharge_cms"]) == len(rating["stage"]),
                    f"Shared HydroID {hydroid} in branch {branch} needs row order; reprepare the HUC",
                )
                rows.extend(zip(order, rating["discharge_cms"], rating["stage"], [discharge] * len(order)))
            rows.sort(key=lambda row: row[0])
            plans[branch].stages[hydroid] = stage_for_discharge(
                rows[0][3], [row[1] for row in rows], [row[2] for row in rows],
            )
    return metadata, [plans[b] for b in sorted(plans, key=__branch_order) if plans[b].blocks], missing


def __branch_order(branch: str) -> tuple[int, str]:
    return (0, f"{int(branch):020d}") if branch.isdigit() else (1, branch)


def schedule_tiles(
    metadata: dict, plans: list[BranchPlan], tile_size: int,
) -> dict[tuple[int, int], list[tuple[int, Window, Window]]]:
    """
    Splits each needed source block into windows of fixed-size output tiles.

    Arguments:
        metadata: HUC metadata with the reference grid and block size.
        plans: Branch plans with occupied block coordinates.
        tile_size: Output tile width and height in reference-grid pixels.
    Returns:
        Output tile (row, column) to (plan index, source window, tile window).
    """

    block = int(metadata["block_size"])
    huc_width, huc_height = int(metadata["width"]), int(metadata["height"])
    schedule: dict[tuple[int, int], list[tuple[int, Window, Window]]] = {}
    for index, plan in enumerate(plans):
        for block_row, block_col in sorted(plan.blocks):
            row0, col0 = block_row * block, block_col * block
            row1, col1 = min(row0 + block, plan.height), min(col0 + block, plan.width)
            __require(
                0 <= row0 < row1 and 0 <= col0 < col1,
                f"Branch {plan.branch} block {block_row},{block_col} is outside its raster",
            )
            top, left = plan.row_off + row0, plan.col_off + col0
            bottom, right = plan.row_off + row1, plan.col_off + col1
            __require(
                top >= 0 and left >= 0 and bottom <= huc_height and right <= huc_width,
                f"Branch {plan.branch} extends beyond the HUC reference grid",
            )
            for tile_row in range(top // tile_size, (bottom - 1) // tile_size + 1):
                for tile_col in range(left // tile_size, (right - 1) // tile_size + 1):
                    r0 = max(top, tile_row * tile_size)
                    r1 = min(bottom, (tile_row + 1) * tile_size)
                    c0 = max(left, tile_col * tile_size)
                    c1 = min(right, (tile_col + 1) * tile_size)
                    schedule.setdefault((tile_row, tile_col), []).append((
                        index,
                        Window(c0 - plan.col_off, r0 - plan.row_off, c1 - c0, r1 - r0),
                        Window(c0 - tile_col * tile_size, r0 - tile_row * tile_size, c1 - c0, r1 - r0),
                    ))
    return schedule


def __check_source(dataset, plan: BranchPlan, transform: Affine, crs: CRS, name: str) -> None:
    expected = transform * Affine.translation(plan.col_off, plan.row_off)
    __require(
        dataset.width == plan.width and dataset.height == plan.height and dataset.count >= 1,
        f"Branch {plan.branch} {name} dimensions do not match the dataset index",
    )
    __require(
        dataset.transform.almost_equals(expected) and dataset.crs == crs,
        f"Branch {plan.branch} {name} grid does not match the dataset index",
    )


def compute_tile(
    metadata: dict,
    plans: list[BranchPlan],
    lookups: list[tuple[np.ndarray, np.ndarray]],
    tile: tuple[int, int],
    parts: list[tuple[int, Window, Window]],
    tile_size: int,
    raster_path: RasterPath,
) -> tuple[np.ndarray, Affine]:
    """
    Computes one output tile from its scheduled source windows.

    Arguments:
        metadata: HUC metadata with the reference grid.
        plans: Branch plans referenced by ``parts``.
        lookups: Sorted HydroID and stage arrays aligned with ``plans``.
        tile: Output tile (row, column).
        parts: Scheduled (plan index, source window, tile window) entries.
        tile_size: Output tile width and height in pixels.
        raster_path: Converts a dataset-relative key to a Rasterio path.
    Returns:
        The tile's boolean flood mask and its affine transform.
    """

    transform = Affine(*metadata["transform"])
    crs = CRS.from_user_input(metadata["crs"])
    tile_row, tile_col = tile
    height = min(tile_size, int(metadata["height"]) - tile_row * tile_size)
    width = min(tile_size, int(metadata["width"]) - tile_col * tile_size)
    flooded = np.zeros((height, width), dtype=bool)
    by_plan: dict[int, list[tuple[Window, Window]]] = {}
    for index, source_window, tile_window in parts:
        by_plan.setdefault(index, []).append((source_window, tile_window))
    for index in sorted(by_plan):
        plan = plans[index]
        hydroids, stages = lookups[index]
        with rasterio.open(raster_path(plan.rem)) as rem, rasterio.open(raster_path(plan.catchments)) as catchments:
            __check_source(rem, plan, transform, crs, "REM")
            __check_source(catchments, plan, transform, crs, "catchments")
            for source_window, tile_window in by_plan[index]:
                rem_values = rem.read(1, window=source_window)
                catchment_values = catchments.read(1, window=source_window)
                rows, cols = tile_window.toslices()
                flooded[rows, cols] |= flooded_cells(rem_values, catchment_values, hydroids, stages)
                del rem_values, catchment_values
    tile_transform = transform * Affine.translation(tile_col * tile_size, tile_row * tile_size)
    return flooded, tile_transform


def encode_tile(flooded: np.ndarray, transform: Affine, crs: CRS) -> bytes:
    """
    Encodes a flood mask as a legacy-compatible float32 1/NaN COG.

    Arguments:
        flooded: Boolean flood mask.
        transform: Tile affine transform.
        crs: Tile coordinate reference system.
    Returns:
        The encoded COG bytes.
    """

    profile = {
        "driver": "GTiff", "dtype": "float32", "count": 1, "nodata": float("nan"),
        "width": flooded.shape[1], "height": flooded.shape[0],
        "transform": transform, "crs": crs, "tiled": True,
        "blockxsize": 512, "blockysize": 512, "compress": "DEFLATE",
    }
    with MemoryFile() as raw, MemoryFile() as encoded:
        with raw.open(**profile) as destination:
            destination.write(np.where(flooded, np.float32(1), np.float32("nan")), 1)
        with raw.open() as source:
            raster_copy(
                source, encoded.name, driver="COG", COMPRESS="DEFLATE",
                BLOCKSIZE=512, OVERVIEW_RESAMPLING="NEAREST",
            )
        return encoded.read()


def run_scenario(
    scenario: dict,
    read_json: ReadJson,
    raster_path: RasterPath,
    write_tile: WriteTile,
    tile_size: int = DEFAULT_TILE_SIZE,
    max_blocks: int = DEFAULT_MAX_BLOCKS,
    max_tiles: int = DEFAULT_MAX_TILES,
    log: Callable[[str], None] = print,
) -> dict:
    """
    Streams one scenario's inundation into independently written COG tiles.

    Only one output tile and one pair of source windows are held at a time.

    Arguments:
        scenario: Scenario dictionary with ScenarioID and Reaches.
        read_json: Reads a dataset-relative JSON object, returning None if absent.
        raster_path: Converts a dataset-relative raster key to a Rasterio path.
        write_tile: Persists one encoded tile under a relative name; returns its key.
        tile_size: Output tile width and height in pixels; a multiple of 512.
        max_blocks: Admission limit for scheduled source blocks per scenario.
        max_tiles: Admission limit for output tiles per scenario.
        log: Progress output function.
    Returns:
        The scenario tile manifest.
    """

    __require(tile_size > 0 and tile_size % 512 == 0, "Streaming tile size must be a positive multiple of 512")
    dataset = read_json("dataset.json")
    __require(
        dataset is not None and dataset.get("format") == DATASET_FORMAT
        and dataset.get("version") == DATASET_VERSION,
        "Prepared streaming dataset is missing or has an unsupported format",
    )
    scenario_id = scenario["ScenarioID"]
    tiles = []
    hucs = {}
    missing_reaches = []
    for huc, discharges in sorted(first_discharges(scenario["Reaches"]).items()):
        metadata, plans, missing = plan_huc(huc, discharges, read_json)
        missing_reaches.extend(f"{huc}:{feature_id}" for feature_id in missing)
        for feature_id in missing:
            log(f"[{scenario_id}] reach {feature_id} is not in HUC {huc}'s prepared index; skipping")
        block_count = sum(len(plan.blocks) for plan in plans)
        __require(
            block_count <= max_blocks,
            f"Scenario {scenario_id} HUC {huc} needs {block_count} source blocks; the limit is {max_blocks}",
        )
        schedule = schedule_tiles(metadata, plans, tile_size)
        __require(
            len(tiles) + len(schedule) <= max_tiles,
            f"Scenario {scenario_id} needs more than {max_tiles} output tiles",
        )
        crs = CRS.from_user_input(metadata["crs"])
        hucs[huc] = {"crs": crs.to_string(), "resolution": abs(metadata["transform"][0])}
        lookups = [plan.lookup() for plan in plans]
        log(f"[{scenario_id}] HUC {huc}: {len(plans)} branches, {block_count} blocks, {len(schedule)} tiles")
        for tile in sorted(schedule):
            flooded, transform = compute_tile(
                metadata, plans, lookups, tile, schedule.pop(tile), tile_size, raster_path,
            )
            if not flooded.any():
                continue
            encoded = encode_tile(flooded, transform, crs)
            height, width = flooded.shape
            del flooded
            bounds = array_bounds(height, width, transform)
            key = write_tile(f"{huc}/{tile[0]}_{tile[1]}.cog", encoded)
            tiles.append({
                "key": key, "huc": huc, "row": tile[0], "col": tile[1],
                "width": width, "height": height, "transform": list(transform)[:6],
                "bounds": [bounds[0], bounds[1], bounds[2], bounds[3]],
                "bounds_wgs84": list(transform_bounds(crs, "EPSG:4326", *bounds, densify_pts=21)),
                "size_bytes": len(encoded),
            })
            del encoded
            log(f"[{scenario_id}] wrote tile {key}")
    if not tiles:
        raise StreamingInputError(f"Scenario {scenario_id} produced no flooded cells")
    west = min(t["bounds_wgs84"][0] for t in tiles)
    south = min(t["bounds_wgs84"][1] for t in tiles)
    east = max(t["bounds_wgs84"][2] for t in tiles)
    north = max(t["bounds_wgs84"][3] for t in tiles)
    return {
        "format": MANIFEST_FORMAT, "version": MANIFEST_VERSION,
        "scenario_id": scenario_id,
        "dataset": {key: dataset[key] for key in ("format", "version", "fim_version") if key in dataset},
        "values": {"dtype": "float32", "flooded": 1, "nodata": "nan"},
        "tile_size": tile_size, "hucs": hucs,
        "bounds_wgs84": [west, south, east, north],
        "missing_reaches": missing_reaches,
        "tiles": tiles,
    }


def dumps_manifest(manifest: dict) -> bytes:
    return json.dumps(manifest, separators=(",", ":"), sort_keys=True).encode("utf-8")
