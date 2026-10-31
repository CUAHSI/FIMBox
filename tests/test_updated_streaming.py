"""Container-side checks that updatedstreaming delegates to NOAA's newer code; run inside the HAND-FIM image."""

import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

HANDFIM = Path(__file__).resolve().parents[1] / "docker" / "handfim"
TOOLS = Path(os.environ.get("FIMBOX_UPDATED_NOAA_TOOLS", "/home/code/inundation-mapping-updated/tools"))


def load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@unittest.skipUnless(
    (TOOLS / "inundation.py").is_file() and importlib.util.find_spec("numba"),
    "Requires the HAND-FIM image with the updated NOAA checkout",
)
class UpdatedStreamingNoaaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixtures = load("updated_streaming_fixtures", Path(__file__).with_name("test_streaming_fim.py"))
        cls.rule = load("updated_streaming_rule", HANDFIM / "noaa_stream_rule.py")
        cls.noaa = cls.rule.load_inundation(TOOLS)
        cls.directory = tempfile.TemporaryDirectory()
        root = Path(cls.directory.name)
        cls.huc_dir, cls.sources, cls.rows = cls.fixtures.build_hydrofabric(root)
        cls.dataset = root / "dataset"
        cls.fixtures.prepare.prepare_huc("12090301", cls.huc_dir, cls.dataset, block_size=128, log=lambda _: None)

    @classmethod
    def tearDownClass(cls):
        cls.directory.cleanup()

    def stream(self, reaches: list[dict], rule=None) -> np.ndarray:
        tiles = {}

        def write_tile(key, data):
            tiles[key] = data
            return key

        manifest = self.fixtures.stream.run_scenario(
            {"ScenarioID": "s1", "Reaches": reaches},
            lambda key: self.fixtures.read_index(self.dataset, key),
            lambda key: str(self.dataset / key), write_tile, tile_size=512, log=lambda _: None,
            **({"rule": rule} if rule else {}),
        )
        return self.fixtures.mosaic(manifest, tiles)

    def noaa_inundate(self, reaches: list[dict]) -> np.ndarray:
        """Runs NOAA's public inundate() per branch on full rasters, then a max/union mosaic."""

        import pandas as pd
        import rasterio

        forecast = {}
        for reach in reaches:
            forecast.setdefault(str(reach["ReachID"]), float(reach["Streamflow"]))
        forecast = pd.DataFrame({"discharge": forecast}).rename_axis("feature_id")
        merged = np.zeros((700, 900), dtype=bool)
        with tempfile.TemporaryDirectory() as directory:
            for branch, (rem, _, (row_off, col_off)) in self.sources.items():
                table = pd.DataFrame([row for row in self.rows if row["branch_id"] == branch])
                table = table.set_index(["HUC", "feature_id", "HydroID"])
                folder = self.huc_dir / "branches" / branch
                output = Path(directory) / f"{branch}.tif"
                self.noaa.inundate(
                    str(folder / f"rem_zeroed_masked_{branch}.tif"),
                    str(folder / f"gw_catchments_reaches_filtered_addedAttributes_{branch}.tif"),
                    None, table, forecast, mask_type="filter", inundation_raster=str(output), quiet=True,
                )
                with rasterio.open(output) as dataset:
                    flooded = dataset.read(1) > 0
                merged[row_off:row_off + rem.shape[0], col_off:col_off + rem.shape[1]] |= flooded
        return merged

    def test_updated_streaming_matches_noaa_inundate_and_drops_shallow_cells(self):
        reaches = [
            {"HUC": "12090301", "ReachID": "11", "Streamflow": 15},
            {"HUC": "12090301", "ReachID": "12", "Streamflow": 25},
            {"HUC": "12090301", "ReachID": "14", "Streamflow": 5},
        ]
        expected = self.noaa_inundate(reaches)
        actual = self.stream(reaches, self.rule.updated_rule(self.noaa))
        pinned = self.stream(reaches)
        self.assertTrue(expected.any())
        np.testing.assert_array_equal(actual, expected)
        self.assertTrue((pinned & ~actual).any(), "fixture should contain depths below 0.03048 m")
        self.assertFalse((actual & ~pinned).any())

    def test_rule_uses_noaa_minimum_depth(self):
        flood = self.rule.updated_rule(self.noaa)(self.fixtures.stream.BranchPlan(
            branch="0", rem="", catchments="", row_off=0, col_off=0, width=3, height=1,
            ratings={7: [("1", 10.0, 0.0, 0.0), ("1", 10.0, 10.0, 1.0)]},
        ))
        rem = np.array([[0.975, 0.965, -1.0]], dtype=np.float32)
        catchments = np.array([[7, 7, 7]], dtype=np.int32)
        self.assertEqual(flood(rem, catchments).tolist(), [[False, True, False]])

    def test_dry_updated_streaming_matches_noaa(self):
        reaches = [{"HUC": "12090301", "ReachID": "13", "Streamflow": 0}]
        expected = self.noaa_inundate(reaches)
        self.assertFalse(expected.any())
        actual = self.stream(reaches, self.rule.updated_rule(self.noaa))
        np.testing.assert_array_equal(actual, expected)

    def test_missing_checkout_fails_explicitly(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(RuntimeError, "rebuild the HAND-FIM image"):
                self.rule.load_inundation(Path(directory))

    def test_precompiled_noaa_cache_loads_in_fresh_processes(self):
        code = """
import sys
from pathlib import Path
from types import SimpleNamespace
import numpy as np

sys.path.insert(0, sys.argv[1])
import noaa_stream_rule
if sys.argv[3] == 'warm':
    sys.path.insert(0, sys.argv[2])
    import inundation
else:
    inundation = noaa_stream_rule.load_inundation(Path(sys.argv[2]))
plan = SimpleNamespace(ratings={7: [('1', 10., 0., 0.), ('1', 10., 10., 1.)]})
flood = noaa_stream_rule.updated_rule(inundation)(plan)
actual = flood(np.array([[.975, .965, -1]], dtype=np.float32),
               np.array([[7, 7, 7]], dtype=np.int32))
assert actual.tolist() == [[False, True, False]], actual
if sys.argv[3] != 'warm':
    assert sum(getattr(inundation, '__go_fast_mapping')._cache_hits.values()) > 0
"""
        with tempfile.TemporaryDirectory() as directory:
            shutil.copyfile(TOOLS / "inundation.py", Path(directory) / "inundation.py")
            for mode in ("warm", "cached", "cached"):
                result = subprocess.run(
                    [sys.executable, "-c", code, str(HANDFIM), directory, mode],
                    capture_output=True, text=True,
                )
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_stream_command_selects_noaa_rule(self):
        from typer.testing import CliRunner

        sys.path.insert(0, str(HANDFIM))
        try:
            entry = load("updated_streaming_entry", HANDFIM / "entry.py")
            import stream_storage

            with tempfile.TemporaryDirectory() as directory, \
                    patch.object(stream_storage, "run_streaming") as run, \
                    patch.object(stream_storage.StreamConfig, "from_env"), \
                    patch("noaa_stream_rule.updated_rule", return_value="noaa-rule"):
                scenario = Path(directory) / "s.json"
                scenario.write_text('[{"ScenarioID": "s1", "Reaches": [{"HUC": "12090301", "ReachID": "11", "Streamflow": 1}]}]')
                result = CliRunner().invoke(entry.app, ["stream-scenario", str(scenario), directory, "--updated"])
                self.assertEqual(result.exit_code, 0, result.output)
                self.assertEqual(run.call_args.kwargs["rule"], "noaa-rule")
        finally:
            sys.path.remove(str(HANDFIM))


if __name__ == "__main__":
    unittest.main()
