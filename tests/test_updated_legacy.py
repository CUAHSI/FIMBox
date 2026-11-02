"""Container-side routing tests; run inside the HAND-FIM image."""

import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


@unittest.skipUnless(importlib.util.find_spec("fimserve"), "Requires the HAND-FIM image")
class UpdatedLegacyRoutingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path = Path(__file__).resolve().parents[1] / "docker" / "handfim" / "entry.py"
        spec = importlib.util.spec_from_file_location("updated_legacy_entry", path)
        cls.entry = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.entry)

    def test_selects_independent_noaa_checkout(self):
        generate = getattr(self.entry, "__generate_fim")
        for updated, directory in (
            (False, "/home/code/inundation-mapping"),
            (True, "/home/code/inundation-mapping-updated"),
        ):
            with self.subTest(updated=updated), patch.object(Path, "is_file", return_value=True), \
                    patch.object(self.entry.runFIM, "runfim") as run:
                generate("03020201", Path("flows.csv"), "scenario__03020201", updated)
                run.assert_called_once_with(
                    directory, "/home/output", "03020201", Path("flows.csv"),
                    label="scenario__03020201",
                )

    def test_missing_updated_checkout_fails_instead_of_running_old_code(self):
        with patch.object(Path, "is_file", return_value=False), \
                patch.object(self.entry.runFIM, "runfim") as run:
            with self.assertRaisesRegex(RuntimeError, "rebuild"):
                getattr(self.entry, "__generate_fim")("03020201", Path("flows.csv"), updated_legacy=True)
            run.assert_not_called()

    def test_process_job_preserves_updated_selection(self):
        with patch.object(self.entry, "__generate_fim") as generate:
            getattr(self.entry, "__compute_fim_scenario")(0, "03020201", "8780625", "flows.csv", "s1", True)
        generate.assert_called_once_with("03020201", "flows.csv", "s1", True)

    def test_scenario_command_exposes_updated_flag(self):
        from typer.testing import CliRunner

        result = CliRunner().invoke(self.entry.app, ["scenario", "--help"])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("--updated-legacy", result.output)

    @unittest.skipUnless(
        Path("/home/code/inundation-mapping-updated").is_dir(), "Requires the updated NOAA checkout",
    )
    def test_real_fimserv_wrapper_produces_native_grid_inundation(self):
        import numpy as np
        import rasterio
        from affine import Affine

        huc = "03020201"
        hydroid = 12340001
        transform = Affine(10, 0, 1483259.4963396771, 0, -10, 1623580.8454759854)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fabric = root / f"flood_{huc}"
            branch = fabric / huc / "branches" / "0"
            branch.mkdir(parents=True)
            (fabric / "fim_inputs.csv").write_text(f"{huc},0\n")
            (fabric / huc / "hydrotable.csv").write_text(
                "HUC,branch_id,feature_id,HydroID,stage,precalb_discharge_cms,discharge_cms,LakeID\n"
                f"{huc},0,8780625,{hydroid},0,0,0,-999\n"
                f"{huc},0,8780625,{hydroid},2,10,10,-999\n"
            )
            rem = np.full((32, 32), 3, dtype="float32")
            rem[20, 15] = 0.5
            catchments = np.full((32, 32), hydroid, dtype="int32")
            for name, data in (
                ("rem_zeroed_masked_0.tif", rem),
                ("gw_catchments_reaches_filtered_addedAttributes_0.tif", catchments),
            ):
                with rasterio.open(
                    branch / name, "w", driver="GTiff", count=1, width=32, height=32,
                    dtype=data.dtype, crs="EPSG:5070", transform=transform, nodata=-9999,
                ) as dst:
                    dst.write(data, 1)
            flows = root / "flows.csv"
            flows.write_text("feature_id,discharge\n8780625,5\n")
            self.entry.runFIM.runfim(
                "/home/code/inundation-mapping-updated", str(root), huc, str(flows), label="updated",
            )
            output = fabric / f"{huc}_inundation" / "updated" / "flows_inundation.tif"
            with rasterio.open(output) as src:
                self.assertEqual(src.transform, transform)
                self.assertEqual(np.argwhere(src.read(1) > 0).tolist(), [[20, 15]])


if __name__ == "__main__":
    unittest.main()
