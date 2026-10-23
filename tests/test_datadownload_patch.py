import importlib.util
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

HUC = "03020201"

# Real reaches from test-data/single_huc_many_reaches.json. In the v4.5
# hydrotable for this HUC they are only in branch 0.
BRANCH_0_REACH = "8780625"
OTHER_BRANCH_0_REACH = "8780659"
# Real features that are in branch 0 *and* a level-path branch (a feature in
# several branches needs every one of them), and one that is only in a
# level-path branch.
MULTI_BRANCH_REACH = "933020084"
LEVEL_PATH_ONLY_REACH = "8791039"

BRANCH_0 = "0"
MULTI_BRANCH = "1123000017"  # also contains MULTI_BRANCH_REACH
LEVEL_PATH_BRANCH = "1123000060"  # contains LEVEL_PATH_ONLY_REACH
UNUSED_BRANCH = "1123000057"  # contains none of the reaches above

# Real branch ids for the HUC; the fixtures below use a subset of the 59 listed
# in the actual branch_ids.csv.
BRANCH_IDS = "".join(
    f"{HUC},{branch}\n"
    for branch in (BRANCH_0, UNUSED_BRANCH, LEVEL_PATH_BRANCH, MULTI_BRANCH)
)
HYDROTABLE = (
    "HydroID,branch_id,feature_id,stage\n"
    f"1,{BRANCH_0},{BRANCH_0_REACH},0.0\n"
    f"2,{BRANCH_0},{OTHER_BRANCH_0_REACH},0.0\n"
    f"3,{BRANCH_0},{MULTI_BRANCH_REACH},0.0\n"
    f"4,{MULTI_BRANCH},{MULTI_BRANCH_REACH},0.0\n"
    f"5,{LEVEL_PATH_BRANCH},{LEVEL_PATH_ONLY_REACH},0.0\n"
)


class DataDownloadPatchTests(unittest.TestCase):
    """Tests for the patched ``fimserve.datadownload`` module.

    The module is loaded straight from ``docker/handfim/patches`` and every
    ``aws s3 sync`` call is replaced by a fake that writes the files the real
    command would have downloaded, so no network access is needed.
    """

    @classmethod
    def setUpClass(cls):
        """Load the patched ``datadownload`` module from its source file."""
        source = (
            Path(__file__).resolve().parents[1]
            / "docker" / "handfim" / "patches" / "datadownload.py"
        )
        spec = importlib.util.spec_from_file_location("patched_datadownload", source)
        assert spec is not None and spec.loader is not None
        cls.dd = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.dd)

    def setUp(self):
        """Create a temporary download directory and reset the command log."""
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.base = directory.name
        self.huc_dir = Path(self.base) / f"flood_{HUC}" / HUC
        self.commands = []

    def fake_aws(self, command, check=False, **kwargs):
        """Fake ``subprocess.run`` that mimics ``aws s3 sync``.

        Writes the files named by the ``--include`` filters into the
        destination directory and records the command in ``self.commands``.

        Parameters
        ----------
        command : list of str
            The ``aws s3 sync <source> <destination> ...`` command line.
        check : bool, optional
            Accepted for compatibility with ``subprocess.run``; ignored.
        **kwargs
            Other ``subprocess.run`` keyword arguments; ignored.

        Returns
        -------
        subprocess.CompletedProcess
            A successful (return code 0) result for ``command``.
        """
        self.commands.append(command)
        destination = Path(command[4])
        includes = [command[i + 1] for i, v in enumerate(command) if v == "--include"]
        for relative_path in includes:
            target = destination / relative_path
            target.parent.mkdir(parents=True, exist_ok=True)
            if relative_path == "hydrotable.csv":
                target.write_text(HYDROTABLE)
            elif relative_path == "branch_ids.csv":
                target.write_text(BRANCH_IDS)
            else:
                target.write_bytes(b"data")
        return subprocess.CompletedProcess(command, 0)

    def download(self, feature_ids, **kwargs):
        """Run ``download_data`` for the test HUC with ``aws`` faked out.

        Parameters
        ----------
        feature_ids : list of str or None
            Reaches to download; ``None`` requests every branch.
        **kwargs
            Extra keyword arguments passed to ``download_data``, for example
            ``include_rating_curve``.
        """
        with patch.object(self.dd.subprocess, "run", side_effect=self.fake_aws):
            self.dd.download_data(HUC, self.base, "4.5", feature_ids, **kwargs)

    def includes(self, command):
        """Return the ``--include`` patterns of a recorded command.

        Parameters
        ----------
        command : list of str
            An ``aws s3 sync`` command line.

        Returns
        -------
        list of str
            The value following each ``--include`` flag, in order.
        """
        return [command[i + 1] for i, v in enumerate(command) if v == "--include"]

    def included_branches(self, command):
        """Return the branch ids targeted by a recorded command.

        Parameters
        ----------
        command : list of str
            An ``aws s3 sync`` command with ``branches/<id>/...`` includes.

        Returns
        -------
        set of str
            The branch ids found in the include patterns.
        """
        return {path.split("/")[1] for path in self.includes(command)}

    def fim_inputs(self):
        """Return the lines of the generated ``fim_inputs.csv``.

        Returns
        -------
        list of str
            One ``<huc>,<branch>`` line per branch listed for the HUC.
        """
        return (self.huc_dir.parent / "fim_inputs.csv").read_text().splitlines()

    def test_downloads_only_lookup_tables_and_needed_branch_rasters(self):
        """Download the lookup tables first, then only the needed rasters."""
        self.download([BRANCH_0_REACH])

        self.assertEqual(len(self.commands), 2)
        first, second = self.commands
        self.assertEqual(first[:3], ["aws", "s3", "sync"])
        self.assertEqual(
            first[3], f"s3://ciroh-owp-hand-fim/hand_fim_4_5_2_11/{HUC}/"
        )
        self.assertIn("--no-sign-request", first)
        self.assertEqual(first[first.index("--exclude") + 1], "*")
        self.assertLess(first.index("--exclude"), first.index("--include"))
        self.assertEqual(
            sorted(self.includes(first)), ["branch_ids.csv", "hydrotable.csv"]
        )
        self.assertEqual(
            sorted(self.includes(second)),
            [
                "branches/0/gw_catchments_reaches_filtered_addedAttributes_0.tif",
                "branches/0/rem_zeroed_masked_0.tif",
            ],
        )
        self.assertEqual(self.fim_inputs(), [f"{HUC},{BRANCH_0}"])

    def test_multiple_reaches_in_the_same_branch_download_it_once(self):
        """Reaches sharing a branch request that branch's files only once."""
        self.download([BRANCH_0_REACH, OTHER_BRANCH_0_REACH])

        self.assertEqual(self.included_branches(self.commands[1]), {BRANCH_0})
        self.assertEqual(len(self.includes(self.commands[1])), 2)

    def test_feature_in_several_branches_downloads_every_branch(self):
        """A reach present in several branches downloads all of them."""
        self.download([MULTI_BRANCH_REACH])

        self.assertEqual(
            self.included_branches(self.commands[1]), {BRANCH_0, MULTI_BRANCH}
        )
        self.assertEqual(
            self.fim_inputs(), [f"{HUC},{BRANCH_0}", f"{HUC},{MULTI_BRANCH}"]
        )

    def test_cached_data_needs_no_network(self):
        """A repeated request runs no ``aws`` commands and keeps the listing."""
        self.download([MULTI_BRANCH_REACH])
        self.commands.clear()

        self.download([MULTI_BRANCH_REACH])

        self.assertEqual(self.commands, [])
        self.assertEqual(
            self.fim_inputs(), [f"{HUC},{BRANCH_0}", f"{HUC},{MULTI_BRANCH}"]
        )

    def test_new_reach_downloads_only_missing_branch_and_keeps_cached_ones(self):
        """A later reach fetches only its missing branch and keeps cached ones."""
        self.download([BRANCH_0_REACH])
        self.commands.clear()

        self.download([LEVEL_PATH_ONLY_REACH])

        self.assertEqual(len(self.commands), 1)
        self.assertEqual(
            self.included_branches(self.commands[0]), {LEVEL_PATH_BRANCH}
        )
        self.assertEqual(
            self.fim_inputs(), [f"{HUC},{BRANCH_0}", f"{HUC},{LEVEL_PATH_BRANCH}"]
        )

    def test_branches_without_rasters_are_not_listed(self):
        """Branches whose rasters were not downloaded stay out of the listing."""
        self.download([BRANCH_0_REACH])

        self.assertNotIn(f"{HUC},{UNUSED_BRANCH}", self.fim_inputs())

    def test_no_feature_ids_downloads_all_branches(self):
        """Without feature ids every branch in ``branch_ids.csv`` is fetched."""
        self.download(None)

        self.assertEqual(
            self.included_branches(self.commands[1]),
            {BRANCH_0, UNUSED_BRANCH, LEVEL_PATH_BRANCH, MULTI_BRANCH},
        )

    def test_rating_curve_is_optional(self):
        """The branch 0 rating curve is fetched only when requested."""
        self.download([BRANCH_0_REACH])
        self.assertFalse((self.huc_dir / "branches/0/hydroTable_0.csv").exists())

        self.download([BRANCH_0_REACH], include_rating_curve=True)

        self.assertEqual(
            self.includes(self.commands[-1]), ["branches/0/hydroTable_0.csv"]
        )

    def test_unknown_feature_ids_fail_before_downloading_rasters(self):
        """Feature ids found in no branch raise before any raster download."""
        with self.assertRaisesRegex(ValueError, "None of the requested feature ids"):
            self.download(["1"])

        self.assertEqual(len(self.commands), 1)

    def test_partly_unknown_feature_ids_download_the_ones_found(self):
        """Unknown ids only warn; branches of the known ids are fetched."""
        self.download([BRANCH_0_REACH, "1"])

        self.assertEqual(self.included_branches(self.commands[1]), {BRANCH_0})

    def test_download_failure_propagates(self):
        """A failing ``aws`` command raises ``CalledProcessError``."""
        error = subprocess.CalledProcessError(1, ["aws"])
        with patch.object(self.dd.subprocess, "run", side_effect=error):
            with self.assertRaises(subprocess.CalledProcessError):
                self.dd.download_data(HUC, self.base, "4.5", [BRANCH_0_REACH])

    def test_missing_remote_file_is_reported(self):
        """A file still absent after a successful sync raises ``FileNotFoundError``."""

        def incomplete_aws(command, check=False, **kwargs):
            """Fake ``aws`` that serves the lookup tables but no rasters.

            Parameters
            ----------
            command : list of str
                The ``aws s3 sync`` command line.
            check : bool, optional
                Accepted for compatibility with ``subprocess.run``; ignored.
            **kwargs
                Other ``subprocess.run`` keyword arguments; ignored.

            Returns
            -------
            subprocess.CompletedProcess
                A successful result for ``command``.
            """
            if "hydrotable.csv" in command:
                return self.fake_aws(command)
            self.commands.append(command)
            return subprocess.CompletedProcess(command, 0)

        with patch.object(self.dd.subprocess, "run", side_effect=incomplete_aws):
            with self.assertRaisesRegex(FileNotFoundError, "rem_zeroed_masked_0.tif"):
                self.dd.download_data(HUC, self.base, "4.5", [BRANCH_0_REACH])

    def test_download_huc8_passes_reaches_through(self):
        """``DownloadHUC8`` forwards feature ids and the rating curve flag."""
        with patch.object(
            self.dd, "setup_directories", return_value=("code", "data", self.base)
        ), patch.object(self.dd, "clone_repository"), patch.object(
            self.dd, "EnvFile"
        ), patch.object(self.dd, "download_data") as download_data:
            self.dd.DownloadHUC8(
                HUC, version="4.5", feature_ids=[BRANCH_0_REACH],
                include_rating_curve=True,
            )

        download_data.assert_called_once_with(
            HUC, self.base, "4.5", [BRANCH_0_REACH], True
        )


if __name__ == "__main__":
    unittest.main()
