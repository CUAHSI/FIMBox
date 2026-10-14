import importlib.util
import io
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch


@unittest.skipUnless(
    Path("/home/entry.py").is_file(), "Requires the HAND-FIM worker image"
)
class ContainerScenarioTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location("hand_fim_entry", "/home/entry.py")
        assert spec is not None and spec.loader is not None
        cls.entry = importlib.util.module_from_spec(spec)
        with patch.object(sys, "path", ["/home", *sys.path]):
            spec.loader.exec_module(cls.entry)

    def setUp(self):
        self.scenarios = [{
            "ScenarioID": "regression",
            "Reaches": [
                {"HUC": "03020201", "ReachID": "8780625", "Streamflow": 227},
                {"HUC": "03030002", "ReachID": "8891104", "Streamflow": 227},
            ],
        }]
        self.future = Mock()
        executor = Mock()
        executor.submit.return_value = self.future
        context = Mock()
        context.__enter__ = Mock(return_value=executor)
        context.__exit__ = Mock(return_value=False)
        for name, kwargs in (
            ("__load_scenarios", {"return_value": self.scenarios}),
            ("__download_huc_fim", {}),
            ("__write_flow_input_file", {"return_value": Path("/tmp/flows.csv")}),
            ("ProcessPoolExecutor", {"return_value": context}),
        ):
            patcher = patch.object(self.entry, name, **kwargs)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_computation_failure_propagates(self):
        self.future.result.side_effect = RuntimeError("computation failed")
        with self.assertRaisesRegex(RuntimeError, "computation failed"):
            self.entry.generate_scenario_fim(Path("scenario.json"), 1, "regression")

    def test_missing_huc_fails_instead_of_partial_result(self):
        with patch.object(self.entry.Path, "mkdir"), patch.object(
            self.entry.Path,
            "glob",
            side_effect=[[Path("/tmp/first_inundation.tif")], []],
        ):
            with self.assertRaisesRegex(RuntimeError, "No output raster found"):
                self.entry.generate_scenario_fim(Path("scenario.json"), 1, "regression")
        self.assertEqual(self.future.result.call_count, 2)

    def test_upstream_mapping_failure_propagates(self):
        process = Mock()
        process.stdout = io.StringIO("upstream computation failed\n")
        process.wait.return_value = 1
        run_fim = self.entry.runFIM
        with patch.object(run_fim.os, "chdir"), patch.object(
            run_fim, "load_dotenv"
        ), patch.object(run_fim.os.path, "exists", return_value=True), patch.object(
            run_fim.subprocess, "Popen", return_value=process
        ), patch.object(run_fim.shutil, "rmtree"), patch.object(
            run_fim.os, "makedirs",
        ), patch.object(sys, "path", [*sys.path]):
            with self.assertRaises(subprocess.CalledProcessError) as raised:
                run_fim.runfim(
                    "/home/code/inundation-mapping", "/home/output",
                    "12345678", "/tmp/flows.csv", label="regression",
                )
        self.assertEqual(raised.exception.returncode, 1)

    def test_single_huc_removes_intermediate_directory_after_moving_raster(self):
        self.scenarios[0]["Reaches"] = self.scenarios[0]["Reaches"][:1]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            label_dir = (
                root / "flood_03020201" / "03020201_inundation"
                / "regression__03020201"
            )
            label_dir.mkdir(parents=True)
            (label_dir / "regression__03020201_inundation.tif").write_bytes(b"raster")
            (label_dir / "mapping.log").write_text("progress")
            cache = root / "flood_03020201" / "03020201"
            cache.mkdir()

            def output_path(value):
                path = Path(value)
                if path.is_relative_to("/home/output"):
                    return root / path.relative_to("/home/output")
                return path

            with patch.object(self.entry, "Path", side_effect=output_path), patch.object(
                self.entry, "__clean_fim_geotiff",
            ), patch.object(self.entry, "__convert_to_cog"):
                self.entry.generate_scenario_fim(Path("scenario.json"), 1, "job")
            self.assertFalse(label_dir.exists())
            self.assertEqual(
                (root / "scenarios" / "job" / "regression.tif").read_bytes(),
                b"raster",
            )
            self.assertTrue(cache.is_dir())


if __name__ == "__main__":
    unittest.main()
