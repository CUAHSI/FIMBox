"""Opt-in integration tests of the patched datadownload against real S3.

``test_datadownload_patch.py`` fakes ``aws s3 sync``, so it cannot notice a
wrong file path, a filter that does not behave as expected, or a hydrotable
whose columns changed. These tests run the real ``aws`` CLI against the public
HAND-FIM bucket. They download well under 1 MB, deliberately avoiding the
~183 MB ``hydrotable.csv`` (only its header is fetched with a byte range).

They are skipped unless ``FIMBOX_RUN_S3_TESTS=1`` is set and the ``aws``
CLI is on the PATH:

    FIMBOX_RUN_S3_TESTS=1 PYTHONPATH=src pixi run python -m unittest tests.test_datadownload_s3 -v
"""

import importlib.util
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

HUC = "03020201"
VERSION = "4.5"

# A small level-path branch of the HUC (about 600 KiB for the two rasters).
SMALL_BRANCH = "1123000057"


@unittest.skipUnless(
    os.environ.get("FIMBOX_RUN_S3_TESTS") == "1",
    "set FIMBOX_RUN_S3_TESTS=1 to run tests that download from S3",
)
@unittest.skipUnless(shutil.which("aws"), "the aws CLI is not installed")
class DataDownloadS3Tests(unittest.TestCase):
    """Check the patched download helpers against the real HAND-FIM bucket."""

    @classmethod
    def setUpClass(cls):
        """Load the patched module and compute the S3 location of the HUC."""
        source = (
            Path(__file__).resolve().parents[1]
            / "docker" / "handfim" / "patches" / "datadownload.py"
        )
        spec = importlib.util.spec_from_file_location("patched_datadownload_s3", source)
        assert spec is not None and spec.loader is not None
        cls.dd = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.dd)
        # Module-level "__" names are mangled when written inside a class, so
        # they are looked up by string instead.
        cls.s3_path = getattr(cls.dd, "__s3_huc_path")(HUC, VERSION)

    def setUp(self):
        """Create an empty temporary directory to download into."""
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.destination = Path(directory.name)

    def downloaded_files(self):
        """Return every file under the download directory.

        Returns
        -------
        set of str
            POSIX-style paths relative to the download directory.
        """
        return {
            path.relative_to(self.destination).as_posix()
            for path in self.destination.rglob("*")
            if path.is_file()
        }

    def sync(self, relative_paths):
        """Download files with the module's real allow-list sync.

        Parameters
        ----------
        relative_paths : list of str
            Exact file paths relative to the HUC's S3 directory.
        """
        getattr(self.dd, "__s3_sync_files")(
            self.s3_path, str(self.destination), relative_paths
        )

    def test_filtered_sync_downloads_only_the_listed_files(self):
        """An allow-list sync of one branch fetches its two rasters and nothing else."""
        expected = getattr(self.dd, "__branch_raster_files")(SMALL_BRANCH)

        self.sync(expected)

        self.assertEqual(self.downloaded_files(), set(expected))

    def test_branch_ids_lists_the_branch_used_by_the_tests(self):
        """``branch_ids.csv`` exists, parses, and lists branch 0 and the test branch."""
        self.sync(["branch_ids.csv"])

        branch_ids = getattr(self.dd, "__read_branch_ids")(
            str(self.destination / "branch_ids.csv"), HUC
        )

        self.assertEqual(self.downloaded_files(), {"branch_ids.csv"})
        self.assertIn("0", branch_ids)
        self.assertIn(SMALL_BRANCH, branch_ids)

    def test_hydrotable_has_the_columns_the_lookup_reads(self):
        """The real hydrotable header contains ``feature_id`` and ``branch_id``."""
        header_file = self.destination / "hydrotable_head.csv"
        bucket, _, prefix = self.s3_path.removeprefix("s3://").partition("/")
        subprocess.run(
            [
                "aws", "s3api", "get-object", "--no-sign-request",
                "--bucket", bucket, "--key", f"{prefix}hydrotable.csv",
                "--range", "bytes=0-8191", str(header_file),
            ],
            check=True,
            stdout=subprocess.DEVNULL,
        )

        header = header_file.read_text().splitlines()[0].split(",")

        self.assertIn("feature_id", header)
        self.assertIn("branch_id", header)


if __name__ == "__main__":
    unittest.main()
