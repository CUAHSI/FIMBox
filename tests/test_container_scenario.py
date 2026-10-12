import importlib.util
import io
import subprocess
import sys
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
        ), patch.object(sys, "path", [*sys.path]):
            with self.assertRaises(subprocess.CalledProcessError) as raised:
                run_fim.runfim(
                    "/home/code/inundation-mapping", "/home/output",
                    "12345678", "/tmp/flows.csv", label="regression",
                )
        self.assertEqual(raised.exception.returncode, 1)


if __name__ == "__main__":
    unittest.main()
