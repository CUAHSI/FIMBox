import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
import rasterio
from rasterio.transform import Affine

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("offset_visualization", ROOT / "tools/visualize_legacy_offset.py")
visual = importlib.util.module_from_spec(spec)
spec.loader.exec_module(visual)


class OffsetVisualizationTests(unittest.TestCase):
    def test_reconstructs_flooding_from_original_input_not_output_masks(self):
        rem = np.array([[0, -0.1, 0.49, 0.5, np.nan, 0.1]])
        cats = np.array([[123, 123, 123, 123, 123, 999]])
        actual = visual.reconstruct_source(rem, cats, {123: np.float32(0.5)})
        np.testing.assert_array_equal(actual, [[True, False, True, False, False, False]])

    def test_native_placement_detects_shift_without_alignment(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            origin = Affine(10, 0, 500000, 0, -10, 2000000)

            def write(name, values, row=0, col=0):
                data = np.asarray(values, dtype="float32")
                path = root / name
                with rasterio.open(
                    path, "w", driver="GTiff", count=1, dtype="float32",
                    height=data.shape[0], width=data.shape[1], crs="EPSG:5070",
                    transform=origin * Affine.translation(col, row), nodata=-9999,
                ) as output:
                    output.write(data, 1)
                return path

            rem = np.full((8, 8), 2.0)
            rem[4, 3] = 0.1
            cats = np.full((8, 8), 11632048)
            rem_path, cats_path = write("rem.tif", rem), write("cats.tif", cats)
            legacy = write("legacy.tif", [[1]], row=3, col=3)
            streaming = write("stream.tif", [[1]], row=4, col=3)
            real_open = rasterio.open

            def open_input(path, *args, **kwargs):
                if path == visual.SOURCE_BASE + "rem_zeroed_masked_0.tif":
                    path = rem_path
                elif path == visual.SOURCE_BASE + "gw_catchments_reaches_filtered_addedAttributes_0.tif":
                    path = cats_path
                return real_open(path, *args, **kwargs)

            with patch.object(visual.rasterio, "open", side_effect=open_input):
                data = visual.read_evidence(legacy, streaming)
            self.assertEqual(data["metrics"], {
                "source_wet": 1, "legacy_wet": 1, "streaming_wet": 1,
                "legacy_differences": 2, "streaming_differences": 0,
                "legacy_rows_relative_to_streaming": -1,
                "legacy_columns_relative_to_streaming": 0,
            })
            self.assertEqual(len(data["rem"]), data["width"] * data["height"])
            self.assertEqual(len(data["outputs"]["legacy"]["sha256"]), 64)

    def test_rejects_fractional_pixel_alignment_and_mismatched_resolution(self):
        origin = Affine(10, 0, 500000, 0, -10, 2000000)
        grid_offset = getattr(visual, "__grid_offset")
        self.assertEqual(grid_offset(origin, origin * Affine.translation(3, 4)), (4, 3))
        with self.assertRaisesRegex(ValueError, "whole native-source pixels"):
            grid_offset(origin, origin * Affine.translation(0, 0.5))
        with self.assertRaisesRegex(ValueError, "resolution"):
            grid_offset(origin, origin * Affine.scale(2))

    def test_embedded_evidence_is_valid_json_and_cannot_close_script_element(self):
        data = {"name": "</script><script>alert('not executable')</script>&"}
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "report.html"
            visual.write_report(data, path)
            report = path.read_text()
        payload = report.split('<script id="evidence" type="application/json">', 1)[1].split("</script>", 1)[0]
        self.assertNotIn("<", payload)
        self.assertEqual(json.loads(payload), data)
        self.assertNotIn("__EVIDENCE_JSON__", report)


if __name__ == "__main__":
    unittest.main()
