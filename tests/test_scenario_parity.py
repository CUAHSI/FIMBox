"""Pixel-level regression comparison of real legacy and streaming scenarios."""

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path

import numpy as np
import rasterio
from rasterio.transform import Affine

ROOT = Path(__file__).resolve().parents[1]
HANDFIM = ROOT / "docker" / "handfim"
# The container runs this file directly, without PYTHONPATH=src.
sys.path.insert(0, str(ROOT / "src"))

from fimbox_api.parity import compare_scenario_outputs  # noqa: E402


class ScenarioOutputDiffTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.origin = Affine(10, 0, 500000, 0, -10, 2000000)

    def write_raster(self, name, values, row=0, col=0, nodata=float("nan")):
        path = self.root / name
        data = np.asarray(values, dtype=np.float32)
        with rasterio.open(
            path, "w", driver="GTiff", width=data.shape[1], height=data.shape[0],
            count=1, dtype="float32", crs="EPSG:5070", nodata=nodata,
            transform=self.origin * Affine.translation(col, row),
        ) as output:
            output.write(data, 1)
        return path

    def compare(self, legacy, tiles):
        return compare_scenario_outputs(legacy, tiles, self.root / "diff.tif")

    def test_equal_masks_ignore_tile_crops_nodata_and_overlaps(self):
        legacy = self.write_raster("legacy.tif", [[1, np.nan, 1], [np.nan, 1, np.nan]])
        tiles = [
            self.write_raster("left.tif", [[1, 0], [-9999, 1]], nodata=-9999),
            self.write_raster("right.tif", [[1]], col=2),
            self.write_raster("overlap.tif", [[1]], row=1, col=1),
        ]
        report = self.compare(legacy, tiles)
        self.assertEqual(report["different_cells"], 0)
        self.assertEqual(report["legacy_flooded_cells"], 3)
        self.assertEqual(report["streaming_flooded_cells"], 3)
        with rasterio.open(self.root / "diff.tif") as diff:
            self.assertFalse(diff.read(1).any())

    def test_reports_removed_and_added_cells_including_outside_legacy_crop(self):
        legacy = self.write_raster("legacy.tif", [[1, 1]])
        tiles = [self.write_raster("tile.tif", [[1, np.nan, 1]], col=1)]
        report = self.compare(legacy, tiles)
        self.assertEqual(report["legacy_only_cells"], 1)
        self.assertEqual(report["streaming_only_cells"], 1)
        self.assertEqual(report["different_cells"], 2)
        self.assertEqual(
            report["sample_differences"],
            [{"x": 500005.0, "y": 1999995.0, "value": 1},
             {"x": 500035.0, "y": 1999995.0, "value": 2}],
        )
        with rasterio.open(self.root / "diff.tif") as diff:
            np.testing.assert_array_equal(diff.read(1)[2:-2, 2:-2], [[1, 0, 0, 2]])

    def test_small_grid_shift_is_realigned_and_reported(self):
        values = [[1, 1, np.nan], [np.nan, 1, 1]]
        legacy = self.write_raster("legacy.tif", values)
        tile = self.write_raster("tile.tif", values, row=1, col=-1)
        report = self.compare(legacy, [tile])
        self.assertEqual(report["different_cells"], 0)
        self.assertEqual((report["grid_shift_rows"], report["grid_shift_columns"]), (1, -1))
        self.assertEqual(report["unaligned_different_cells"], 8)
        with rasterio.open(self.root / "diff.tif") as diff:
            self.assertFalse(diff.read(1).any())
            self.assertEqual(diff.tags()["grid_shift_rows"], "1")

    def test_real_differences_remain_after_grid_shift_realignment(self):
        legacy = self.write_raster("legacy.tif", [[1, 1, 1, 1]])
        tile = self.write_raster("tile.tif", [[1, 1, 1, np.nan]], row=1)
        report = self.compare(legacy, [tile])
        self.assertEqual(report["grid_shift_rows"], 1)
        self.assertEqual(report["different_cells"], 1)
        self.assertEqual(report["sample_differences"], [{"x": 500035.0, "y": 1999995.0, "value": 1}])

    def test_identical_arrays_beyond_the_shift_limit_are_not_equal(self):
        legacy = self.write_raster("legacy.tif", [[1]])
        tile = self.write_raster("tile.tif", [[1]], row=3)
        report = self.compare(legacy, [tile])
        self.assertEqual(report["different_cells"], 2)
        self.assertEqual((report["grid_shift_rows"], report["grid_shift_columns"]), (0, 0))

    def test_missing_tiles_report_all_legacy_flooding_as_removed(self):
        legacy = self.write_raster("legacy.tif", [[1, 1], [1, np.nan]])
        report = self.compare(legacy, [])
        self.assertEqual(report["legacy_only_cells"], 3)
        self.assertEqual(report["streaming_flooded_cells"], 0)

    def test_counts_differences_across_multiple_comparison_windows(self):
        values = np.ones((513, 513), dtype=np.float32)
        legacy = self.write_raster("legacy.tif", values)
        values[::2] = np.nan
        tile = self.write_raster("tile.tif", values)
        report = self.compare(legacy, [tile])
        self.assertEqual(report["different_cells"], 257 * 513)
        self.assertEqual(len(report["sample_differences"]), 10)


@unittest.skipUnless(
    os.environ.get("FIMBOX_RUN_PARITY") == "1",
    "Set FIMBOX_RUN_PARITY=1 inside the HAND-FIM image for real computation",
)
class LegacyStreamingScenarioParityTests(unittest.TestCase):
    def test_scenarios_match_legacy_execution(self):
        self.assertTrue(Path("/home/entry.py").is_file(), "Requires the HAND-FIM image")
        scenario_path = Path(os.environ.get(
            "FIMBOX_PARITY_SCENARIOS", str(ROOT / "test-data" / "multiple_scenarios.json"),
        ))
        scenarios = json.loads(scenario_path.read_text())
        self.assertIsInstance(scenarios, list)
        self.assertTrue(scenarios, "At least one parity scenario is required")
        run_id = f"parity-{uuid.uuid4().hex}"
        output = Path("/home/output/scenarios") / run_id
        output.mkdir(parents=True)
        print(f"Parity artifacts: {output}", flush=True)
        renamed = [
            {**scenario, "ScenarioID": f"{run_id}-{index}"}
            for index, scenario in enumerate(scenarios)
        ]
        inputs = output / "scenarios.json"
        inputs.write_text(json.dumps(renamed))

        subprocess.run(
            [sys.executable, str(HANDFIM / "entry.py"), "scenario", str(inputs), "1", run_id],
            check=True,
        )
        # Load checkout code, not a potentially stale copy baked into the image.
        spec = importlib.util.spec_from_file_location(
            "parity_prepare", HANDFIM / "prepare_streaming_dataset.py",
        )
        assert spec is not None and spec.loader is not None
        prepare = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(prepare)
        spec = importlib.util.spec_from_file_location(
            "parity_stream", HANDFIM / "patches" / "streamFIM.py",
        )
        assert spec is not None and spec.loader is not None
        stream = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = stream
        spec.loader.exec_module(stream)

        with tempfile.TemporaryDirectory(prefix="fimbox-parity-") as directory:
            dataset = Path(directory)
            for huc in sorted({reach["HUC"] for s in renamed for reach in s["Reaches"]}):
                prepare.prepare_huc(
                    huc, Path(f"/home/output/flood_{huc}/{huc}"), dataset,
                )

            def read_json(key):
                path = dataset / key
                return json.loads(path.read_text()) if path.is_file() else None

            reports = []
            for original, scenario in zip(scenarios, renamed):
                with self.subTest(scenario=original["ScenarioID"]):
                    folder = output / scenario["ScenarioID"]
                    folder.mkdir()

                    def write_tile(key, data):
                        path = folder / key
                        path.parent.mkdir(parents=True, exist_ok=True)
                        path.write_bytes(data)
                        return str(path)

                    manifest = stream.run_scenario(
                        scenario, read_json, lambda key: str(dataset / key), write_tile,
                    )
                    (folder / "manifest.json").write_bytes(stream.dumps_manifest(manifest))
                    report = compare_scenario_outputs(
                        output / f"{scenario['ScenarioID']}.tif",
                        [Path(tile["key"]) for tile in manifest["tiles"]],
                        folder / "diff.tif",
                    )
                    report.update(
                        scenario_id=original["ScenarioID"],
                        missing_reaches=manifest["missing_reaches"],
                    )
                    reports.append(report)
                    (output / "report.json").write_text(json.dumps(reports, indent=2))
                    self.assertFalse(
                        manifest["missing_reaches"],
                        f"Streaming skipped requested reaches: {report}",
                    )
                    self.assertEqual(
                        report["different_cells"], 0,
                        f"Legacy/streaming output differs: {json.dumps(report, indent=2)}",
                    )


if __name__ == "__main__":
    unittest.main()
