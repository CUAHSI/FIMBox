"""Pixel-level comparison of legacy and streaming scenario flood masks."""

import math
from contextlib import ExitStack
from pathlib import Path

import numpy as np
import rasterio
from rasterio.enums import Resampling
from rasterio.transform import Affine
from rasterio.vrt import WarpedVRT
from rasterio.warp import transform_bounds
from rasterio.windows import Window

MAX_SAMPLE_DIFFERENCES = 10
MAX_GRID_SHIFT = 2
WINDOW_SIZE = 512


def __windows(width: int, height: int):
    for row in range(0, height, WINDOW_SIZE):
        for col in range(0, width, WINDOW_SIZE):
            yield Window(col, row, min(WINDOW_SIZE, width - col), min(WINDOW_SIZE, height - row))


def __masks(legacy, streaming, window: Window) -> tuple[np.ndarray, np.ndarray]:
    expected = legacy.read(1, window=window, masked=True).filled(0) == 1
    actual = np.zeros(expected.shape, dtype=bool)
    for view in streaming:
        actual |= view.read(1, window=window, masked=True).filled(0) == 1
    return expected, actual


def __compare(legacy, streaming, transform: Affine, width: int, height: int, output=None) -> dict:
    """Count flooded and differing cells, optionally writing the diff raster.

    Arguments:
        legacy: Legacy view on the comparison grid.
        streaming: Streaming views on the (possibly shifted) comparison grid.
        transform: Comparison grid transform, used for sample coordinates.
        width: Comparison grid width.
        height: Comparison grid height.
        output: Open diff raster to write, or None to only count.
    Returns:
        Cell counts, sample differences, and flooded-cell centroids in grid pixels.
    """

    report = {
        "legacy_flooded_cells": 0,
        "streaming_flooded_cells": 0,
        "legacy_only_cells": 0,
        "streaming_only_cells": 0,
        "sample_differences": [],
    }
    sums = np.zeros((2, 2))
    for window in __windows(width, height):
        expected, actual = __masks(legacy, streaming, window)
        lost = expected & ~actual
        added = actual & ~expected
        difference = lost.astype(np.uint8) + 2 * added.astype(np.uint8)
        if output is not None:
            output.write(difference, 1, window=window)
        for index, mask in enumerate((expected, actual)):
            rows, cols = np.nonzero(mask)
            sums[index] += (rows.sum() + rows.size * window.row_off, cols.sum() + cols.size * window.col_off)
        report["legacy_flooded_cells"] += int(expected.sum())
        report["streaming_flooded_cells"] += int(actual.sum())
        report["legacy_only_cells"] += int(lost.sum())
        report["streaming_only_cells"] += int(added.sum())
        remaining = MAX_SAMPLE_DIFFERENCES - len(report["sample_differences"])
        for row, col in np.argwhere(difference)[:remaining]:
            x, y = transform * (col + window.col_off + 0.5, row + window.row_off + 0.5)
            report["sample_differences"].append({"x": x, "y": y, "value": int(difference[row, col])})
    report["different_cells"] = report["legacy_only_cells"] + report["streaming_only_cells"]
    counts = (report["legacy_flooded_cells"], report["streaming_flooded_cells"])
    report["centroids"] = [tuple(sums[i] / counts[i]) if counts[i] else None for i in range(2)]
    return report


def compare_scenario_outputs(legacy_path: Path, tile_paths: list[Path], diff_path: Path) -> dict:
    """Compare flood masks over their union extent, allowing a small whole-pixel grid shift.

    The streaming mask is realigned by the rounded offset between the two masks'
    flooded-cell centroids when that offset is at most MAX_GRID_SHIFT pixels and
    reduces the differences, so a georeferencing offset is reported as
    ``grid_shift_rows``/``grid_shift_columns`` instead of as differing cells.

    Arguments:
        legacy_path: Legacy scenario raster; its grid and CRS define the comparison grid.
        tile_paths: Streaming tile rasters, combined as a flood union.
        diff_path: GeoTIFF to write on the legacy grid, after alignment
            (0=equal, 1=legacy-only flooding, 2=streaming-only flooding).
    Returns:
        Flooded and differing cell counts after alignment, the applied grid shift
        (streaming rows/columns relative to legacy; positive is south/east),
        unaligned different cells, sample difference coordinates, CRS, and diff path.
    """

    with ExitStack() as stack:
        legacy = stack.enter_context(rasterio.open(legacy_path))
        tiles = [stack.enter_context(rasterio.open(path)) for path in tile_paths]
        transform = legacy.transform
        if transform.b or transform.d or transform.a <= 0 or transform.e >= 0:
            raise ValueError("Parity comparison requires a north-up legacy raster")
        sources = [legacy, *tiles]
        if any(source.count != 1 or source.crs is None for source in sources):
            raise ValueError("Parity comparison requires single-band georeferenced rasters")
        bounds = [
            transform_bounds(source.crs, legacy.crs, *source.bounds, densify_pts=21)
            for source in sources
        ]
        west = min(bound[0] for bound in bounds)
        south = min(bound[1] for bound in bounds)
        east = max(bound[2] for bound in bounds)
        north = max(bound[3] for bound in bounds)
        col_start, row_start = ~transform * (west, north)
        col_stop, row_stop = ~transform * (east, south)
        col_start, row_start = math.floor(col_start) - MAX_GRID_SHIFT, math.floor(row_start) - MAX_GRID_SHIFT
        width = math.ceil(col_stop) - col_start + MAX_GRID_SHIFT
        height = math.ceil(row_stop) - row_start + MAX_GRID_SHIFT
        transform *= Affine.translation(col_start, row_start)

        def views(shift_rows: int, shift_cols: int) -> list:
            return [
                stack.enter_context(WarpedVRT(
                    source, crs=legacy.crs, width=width, height=height,
                    transform=transform * Affine.translation(shift_cols, shift_rows),
                    dtype="float32", nodata=float("nan"), resampling=Resampling.nearest,
                ))
                for source in tiles
            ]

        legacy_view = stack.enter_context(WarpedVRT(
            legacy, crs=legacy.crs, transform=transform, width=width, height=height,
            dtype="float32", nodata=float("nan"), resampling=Resampling.nearest,
        ))
        unaligned = __compare(legacy_view, views(0, 0), transform, width, height)
        shift = (0, 0)
        expected_centroid, actual_centroid = unaligned["centroids"]
        if unaligned["different_cells"] and expected_centroid and actual_centroid:
            candidate = tuple(round(a - e) for e, a in zip(expected_centroid, actual_centroid))
            if candidate != (0, 0) and max(map(abs, candidate)) <= MAX_GRID_SHIFT:
                aligned = __compare(legacy_view, views(*candidate), transform, width, height)
                if aligned["different_cells"] < unaligned["different_cells"]:
                    shift = candidate

        diff_path.parent.mkdir(parents=True, exist_ok=True)
        with rasterio.open(
            diff_path, "w", driver="GTiff", count=1, dtype="uint8", nodata=0,
            crs=legacy.crs, transform=transform, width=width, height=height,
            tiled=True, blockxsize=WINDOW_SIZE, blockysize=WINDOW_SIZE, compress="deflate",
        ) as output:
            output.update_tags(
                values="0=equal, 1=legacy-only flooding, 2=streaming-only flooding",
                grid_shift_rows=str(shift[0]), grid_shift_columns=str(shift[1]),
            )
            report = __compare(legacy_view, views(*shift), transform, width, height, output)
        del report["centroids"]
        report.update(
            grid_shift_rows=shift[0], grid_shift_columns=shift[1],
            unaligned_different_cells=unaligned["different_cells"],
            crs=legacy.crs.to_string(), diff_raster=str(diff_path),
        )
        return report
