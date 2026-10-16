import runpy
import sys
import unittest
from pathlib import Path
from types import ModuleType
from unittest.mock import Mock, patch


BACKEND = Path(__file__).resolve().parents[1] / "sdk" / "build_backend.py"


class SdkPackagingTests(unittest.TestCase):
    def setUp(self):
        self.calls = Mock()
        setuptools = ModuleType("setuptools")
        setuptools.build_meta = self.calls.setuptools
        generator = ModuleType("generate")
        generator.main = self.calls.generate
        with patch.dict(sys.modules, {
            "setuptools": setuptools,
            "setuptools.build_meta": setuptools.build_meta,
            "generate": generator,
        }):
            self.backend = runpy.run_path(str(BACKEND))

    def test_builds_generate_before_setuptools_and_preserve_arguments(self):
        for hook in ("build_wheel", "build_editable", "build_sdist"):
            with self.subTest(hook=hook):
                self.calls.reset_mock()
                settings = {"verbose": "true"}
                arguments = ("dist", settings)
                if hook != "build_sdist":
                    arguments += ("metadata",)
                build = getattr(self.calls.setuptools, hook)
                build.return_value = "artifact"
                result = self.backend[hook](*arguments)
                self.assertEqual(result, "artifact")
                self.assertEqual(self.calls.mock_calls[0][0], "generate")
                self.calls.generate.assert_called_once_with()
                build.assert_called_once_with(*arguments)

    def test_generation_failure_stops_every_build(self):
        for hook in ("build_wheel", "build_editable", "build_sdist"):
            with self.subTest(hook=hook):
                self.calls.reset_mock()
                self.calls.generate.side_effect = SystemExit("generation failed")
                with self.assertRaisesRegex(SystemExit, "generation failed"):
                    self.backend[hook]("dist")
                getattr(self.calls.setuptools, hook).assert_not_called()

    def test_other_hooks_delegate_to_setuptools(self):
        for hook in (
            "get_requires_for_build_editable",
            "get_requires_for_build_sdist",
            "get_requires_for_build_wheel",
            "prepare_metadata_for_build_editable",
            "prepare_metadata_for_build_wheel",
        ):
            with self.subTest(hook=hook):
                self.assertIs(self.backend[hook], getattr(self.calls.setuptools, hook))
