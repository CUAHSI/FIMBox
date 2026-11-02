"""Regression test for the legacy one-row mosaic offset in pinned NOAA code.

The test exercises inundation-mapping commit
9d39cb85ae789b30964a82c19dd850d29fccf5df, tools/overlapping_inundation.py,
whose merge_data writes each partition at line 437 with an unrounded window.
It requires that module, so run it inside the HAND-FIM image:

    docker run --rm --platform linux/amd64 -v "$PWD/tests:/tests:ro" \
      -e PYTHONPATH=/home/code/inundation-mapping/tools \
      --entrypoint /opt/conda/bin/python cuahsi/handfim:latest \
      -m unittest discover -s /tests -p test_legacy_mosaic_offset.py -v
"""

import inspect
import sys
import tempfile
import unittest
import warnings
from contextlib import ExitStack
from pathlib import Path
from unittest import mock

try:
    import numpy as np
    import overlapping_inundation as upstream
    import rasterio
    import rasterio.io
    from affine import Affine
    from rasterio.windows import Window
except ImportError:
    upstream = None

UPSTREAM_WRITE = "rst.write_band(1, window_data.astype(dtype), window=final_window)"
UPSTREAM_WRITE_LINE = 437

# Branch 0 grid for HUC 03020201 from the downloaded legacy/streaming case.
TRANSFORM = (10, 0, 1483259.4963396771, 0, -10, 1623580.8454759854)
HEIGHT, WIDTH = 13042, 13778
PARTITIONS = (30, 30)
MARKER_ROW, MARKER_COL = 4281, 1771
MARKER = 7
NODATA = -9999


def write_marker_raster(path):
    """Write a sparse branch-sized raster containing one flooded marker cell.

    Arguments:
        path: Destination GeoTIFF path.
    """
    profile = {
        "driver": "GTiff",
        "dtype": "int16",
        "count": 1,
        "height": HEIGHT,
        "width": WIDTH,
        "crs": "EPSG:5070",
        "transform": Affine(*TRANSFORM),
        "nodata": NODATA,
        "tiled": True,
        "blockxsize": 256,
        "blockysize": 256,
        "sparse_ok": True,
    }
    with rasterio.open(path, "w", **profile) as dst:
        dst.write(np.array([[MARKER]], dtype="int16"), 1, window=Window(MARKER_COL, MARKER_ROW, 1, 1))


def merge(source, output, merge_data=None):
    """Run pinned OverlapWindowMerge and record writes of the marker partition.

    Arguments:
        source: Input inundation raster.
        output: Output mosaic path.
        merge_data: Optional replacement for upstream.merge_data.
    Returns:
        A list of (caller function, caller line, window, marker positions) tuples.
    """
    writes = []
    original_write = rasterio.io.DatasetWriter.write_band

    def record_write(self, bidx, src, window=None):
        positions = np.argwhere(src == MARKER)
        if positions.size:
            caller = sys._getframe(1)
            writes.append((caller.f_code.co_name, caller.f_lineno, window, positions.tolist()))
        return original_write(self, bidx, src, window=window)

    with ExitStack() as stack:
        stack.enter_context(warnings.catch_warnings())
        warnings.simplefilter("ignore", DeprecationWarning)
        stack.enter_context(mock.patch.object(rasterio.io.DatasetWriter, "write_band", record_write))
        if merge_data is not None:
            stack.enter_context(mock.patch.object(upstream, "merge_data", merge_data))
        upstream.OverlapWindowMerge([str(source)], PARTITIONS).merge_rasters(str(output), threaded=False)
    return writes


def marker_positions(path):
    """Return native rows/cols where the marker value appears in a mosaic.

    Arguments:
        path: Mosaic raster path.
    Returns:
        A tuple of the raster transform and a list of [row, col] positions.
    """
    window = Window(MARKER_COL - 5, MARKER_ROW - 5, 11, 11)
    with rasterio.open(path) as src:
        data = src.read(1, window=window)
        transform = src.transform
    rows, cols = np.nonzero(data == MARKER)
    return transform, [[int(r) + MARKER_ROW - 5, int(c) + MARKER_COL - 5] for r, c in zip(rows, cols)]



@unittest.skipIf(upstream is None, "requires pinned NOAA overlapping_inundation (run in cuahsi/handfim)")
class PinnedMosaicRowOffsetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.source = Path(cls.tmp.name) / "inundation.tif"
        write_marker_raster(cls.source)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_line_437_is_the_unrounded_partition_write(self):
        lines, start = inspect.getsourcelines(upstream.merge_data)
        write_lines = [start + i for i, line in enumerate(lines) if UPSTREAM_WRITE in line]
        self.assertEqual(write_lines, [UPSTREAM_WRITE_LINE])

    def test_line_437_writes_marker_one_row_north(self):
        output = Path(self.tmp.name) / "upstream.tif"
        writes = merge(self.source, output)

        self.assertEqual(len(writes), 1)
        function, line, window, positions = writes[0]
        self.assertEqual((function, line), ("merge_data", UPSTREAM_WRITE_LINE))
        # from_bounds turns partition row 9 (native row 3906) into a float just below it.
        self.assertAlmostEqual(window.row_off, 3906, places=9)
        self.assertLess(window.row_off, 3906)
        # The partition array is correct: the marker is at relative row 4281 - 3906.
        self.assertEqual(positions, [[MARKER_ROW - 3906, MARKER_COL - 1377]])

        transform, written = marker_positions(output)
        self.assertEqual(transform, Affine(*TRANSFORM))
        self.assertEqual(written, [[MARKER_ROW - 1, MARKER_COL]])

    def test_rounded_line_437_window_preserves_marker_row(self):
        original = upstream.merge_data

        def rounded_merge_data(rst_data, window_bnds, final_window, *args, **kwargs):
            final_window = Window(
                round(final_window.col_off),
                round(final_window.row_off),
                round(final_window.width),
                round(final_window.height),
            )
            return original(rst_data, window_bnds, final_window, *args, **kwargs)

        output = Path(self.tmp.name) / "rounded.tif"
        writes = merge(self.source, output, rounded_merge_data)

        self.assertEqual(writes[0][2].row_off, 3906)
        _, written = marker_positions(output)
        self.assertEqual(written, [[MARKER_ROW, MARKER_COL]])


if __name__ == "__main__":
    unittest.main()
