import importlib.util
import io
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch


class RunFimCleanupTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        source = (
            Path(__file__).resolve().parents[1]
            / "docker" / "handfim" / "patches" / "runFIM.py"
        )
        spec = importlib.util.spec_from_file_location("cleanup_test.runFIM", source)
        assert spec is not None and spec.loader is not None
        cls.run_fim = importlib.util.module_from_spec(spec)
        downloads = types.ModuleType("cleanup_test.datadownload")
        downloads.setup_directories = Mock()
        with patch.dict(sys.modules, {"cleanup_test.datadownload": downloads}):
            spec.loader.exec_module(cls.run_fim)

    def test_repeated_run_removes_stale_outputs_and_preserves_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            huc_dir = root / "flood_03020201"
            cache = huc_dir / "03020201" / "cache.csv"
            cache.parent.mkdir(parents=True)
            cache.write_text("watershed data")
            shared = huc_dir / "03020201_inundation"
            other = shared / "other-scenario" / "other_inundation.tif"
            other.parent.mkdir(parents=True)
            other.write_text("other job")
            label = "single-reach-scenario__03020201"
            output = shared / label
            temp = output / "temp"
            temp.mkdir(parents=True)
            (temp / "stale.tif").write_text("failed attempt")
            final = output / f"{label}_inundation.tif"
            final.write_text("previous run")
            depth = output / f"{label}_depth.tif"
            depth.write_text("previous depth")

            def compute(*args, **kwargs):
                self.assertFalse(final.exists())
                self.assertFalse(depth.exists())
                self.assertFalse((temp / "stale.tif").exists())
                (temp / final.name).write_text("new raster")
                return Mock(stdout=io.StringIO("mosaicking\n"), wait=Mock(return_value=0))

            with patch.object(self.run_fim.os, "chdir"), patch.object(
                self.run_fim, "load_dotenv",
            ), patch.object(self.run_fim.subprocess, "Popen", side_effect=compute), patch.object(
                sys, "path", [*sys.path],
            ):
                for _ in range(2):
                    self.run_fim.runfim(
                        str(root / "code"), str(root), "03020201",
                        f"/tmp/{label}", label=label,
                    )
            self.assertEqual(final.read_text(), "new raster")
            self.assertFalse(temp.exists())
            self.assertEqual(cache.read_text(), "watershed data")
            self.assertEqual(other.read_text(), "other job")

    def test_symlink_output_directory_is_rejected_without_deleting_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            external = root / "external"
            external.mkdir()
            raster = external / "flows_inundation.tif"
            raster.write_text("preserve")
            shared = root / "flood_03020201" / "03020201_inundation"
            shared.mkdir(parents=True)
            (shared / "regression").symlink_to(external, target_is_directory=True)
            with patch.object(self.run_fim.os, "chdir"), patch.object(
                self.run_fim, "load_dotenv",
            ), patch.object(self.run_fim.subprocess, "Popen") as compute, patch.object(
                sys, "path", [*sys.path],
            ):
                with self.assertRaisesRegex(RuntimeError, "symbolic link"):
                    self.run_fim.runfim(
                        str(root / "code"), str(root), "03020201",
                        "/tmp/flows.csv", label="regression",
                    )
            compute.assert_not_called()
            self.assertEqual(raster.read_text(), "preserve")

    def test_unlabeled_run_preserves_other_rasters_in_shared_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            shared = root / "flood_03020201" / "03020201_inundation"
            temp = shared / "temp"
            temp.mkdir(parents=True)
            final = shared / "flows_inundation.tif"
            final.write_text("previous run")
            other = shared / "other_inundation.tif"
            other.write_text("preserve")

            def compute(*args, **kwargs):
                self.assertFalse(final.exists())
                (temp / final.name).write_text("new raster")
                return Mock(stdout=io.StringIO(), wait=Mock(return_value=0))

            with patch.object(self.run_fim.os, "chdir"), patch.object(
                self.run_fim, "load_dotenv",
            ), patch.object(self.run_fim.subprocess, "Popen", side_effect=compute), patch.object(
                sys, "path", [*sys.path],
            ):
                self.run_fim.runfim(
                    str(root / "code"), str(root), "03020201", "/tmp/flows.csv",
                )
            self.assertEqual(final.read_text(), "new raster")
            self.assertEqual(other.read_text(), "preserve")

    def test_traversal_label_is_rejected_before_cleanup(self):
        with patch.object(self.run_fim.os, "chdir"), patch.object(
            self.run_fim, "load_dotenv",
        ), patch.object(self.run_fim.shutil, "rmtree") as cleanup, patch.object(
            sys, "path", [*sys.path],
        ):
            with self.assertRaisesRegex(ValueError, "single directory names"):
                self.run_fim.runfim(
                    "/home/code", "/home/output", "03020201",
                    "/tmp/flows.csv", label="../other",
                )
        cleanup.assert_not_called()


if __name__ == "__main__":
    unittest.main()
