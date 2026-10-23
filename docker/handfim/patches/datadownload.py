"""
Patched replacement for fimserve's `datadownload` module.

Upstream `DownloadHUC8` runs an unfiltered `aws s3 sync` of the whole
pre-computed HAND-FIM output for a HUC (logs, SRC tables, per-branch
hydrotables, geopackages, ...). The mosaic process
(`inundate_mosaic_wrapper.py` -> `Inundate_gms`) only reads:

  - fim_inputs.csv                        (copied from branch_ids.csv)
  - hydrotable.csv
  - branches/<b>/rem_zeroed_masked_<b>.tif
  - branches/<b>/gw_catchments_reaches_filtered_addedAttributes_<b>.tif

and only for the branches that contain the requested NWM reaches, so this
module downloads just those files. The public API used by the rest of the
container (`setup_directories`, `EnvFile`, `clone_repository`,
`DownloadHUC8`) is kept, with two intentional differences from upstream:

  - `DownloadHUC8` accepts `feature_ids` / `include_rating_curve` and no
    longer takes `stream_order`.
  - `uniqueFID` / `feature_IDs.csv` generation was removed: nothing reads it
    and it parsed the full hydrotable in pandas.
"""

import csv
import os
import subprocess
from typing import Iterable, List, Optional, Set, Tuple, Union

S3_BUCKET = "s3://ciroh-owp-hand-fim"

# Data version -> S3 prefix. Anything else falls back to the default (4.9).
S3_VERSION_PREFIXES = {
    "4.5": "hand_fim_4_5_2_11",
    "4.8": "hand_fim_4_8_7_2",
}
S3_DEFAULT_PREFIX = "hand_fim_4_9_9_0"

CORE_FILES = ("branch_ids.csv", "hydrotable.csv")
RATING_CURVE_FILE = "branches/0/hydroTable_0.csv"


def setup_directories(parent_dir: Union[str, None] = None) -> tuple[str, str, str]:
    """
    Prepares the necessary directory structure for the FIM process.
    It creates directories for the code, data inputs, and outputs
    if they do not already exist.

    Parameters
    ----------
    parent_dir : str, optional
        The root directory under which the code, data, and output directories will be created.
        default is None, which means the current working directory will be used.

    Returns
    -------
    directories: tuple[str, str, str]
        A tuple containing the paths to the code directory, data directory,
        and output directory in that order.

    """

    # set the parent directory to the current working directory if not provided
    if parent_dir is None:
        parent_dir = os.getcwd()

    # build paths for code, data, and output directories
    code_dir = os.path.join(parent_dir, "code", "inundation-mapping")
    data_dir = os.path.join(parent_dir, "data", "inputs")
    output_dir = os.path.join(parent_dir, "output")

    # Create the directories if not exist
    os.makedirs(code_dir, exist_ok=True)
    os.makedirs(data_dir, exist_ok=True)
    os.makedirs(output_dir, exist_ok=True)

    return code_dir, data_dir, output_dir


def clone_repository(code_dir, version=None):
    repo_path = os.path.join(code_dir)
    repo_url = "https://github.com/NOAA-OWP/inundation-mapping.git"

    # Map version strings to git tags
    version_tag_map = {
        "4.5": "v4.6.1.4",
        "default": "main",
    }

    # Determine tag to checkout
    version_tag = version_tag_map.get(version, version_tag_map["default"])
    if os.path.exists(repo_path) and os.listdir(repo_path):
        print(f"Repository already exists at {repo_path}. Skipping clone.")
        return

    # Clone repo
    subprocess.run(["git", "clone", repo_url, repo_path], check=True)

    # Checkout specific version/tag (only if not main)
    if version_tag != "main":
        try:
            subprocess.run(["git", "checkout", version_tag], cwd=repo_path, check=True)
        except subprocess.CalledProcessError:
            subprocess.run(["git", "tag"], cwd=repo_path)  # List available tags
            raise
    print(f"Repository cloned into: {repo_path} (version: {version_tag})")


def __s3_huc_path(huc_number: str, version: Optional[str]) -> str:
    """
    Builds the S3 location of the pre-computed HAND-FIM data for a HUC.

    Arguments:
        huc_number - str: The HUC identifier for the watershed.
        version - str: The data version ("4.5", "4.8", or anything else for 4.9).
    Returns:
        str: The S3 URI, with a trailing slash.
    """

    prefix = S3_VERSION_PREFIXES.get(version, S3_DEFAULT_PREFIX)
    return f"{S3_BUCKET}/{prefix}/{huc_number}/"


def __s3_sync_files(s3_path: str, destination: str, relative_paths: Iterable[str]) -> None:
    """
    Downloads only the listed files (paths relative to `s3_path`) using
    `aws s3 sync` with an exclude-everything-then-include allow-list. Files
    that already exist and are unchanged are not downloaded again.

    Arguments:
        s3_path - str: The S3 URI of the HUC data.
        destination - str: The local directory that mirrors `s3_path`.
        relative_paths - Iterable[str]: Exact file paths relative to `s3_path`.
    Returns:
        None
    """

    command = [
        "aws", "s3", "sync", s3_path, destination, "--no-sign-request",
        "--exclude", "*",
    ]
    for relative_path in relative_paths:
        command += ["--include", relative_path]

    # check=True so a failed download surfaces here instead of later as a
    # confusing missing-file error during mosaicking.
    subprocess.run(command, check=True)


def __branch_raster_files(branch_id: str) -> List[str]:
    """
    Lists the two rasters the mosaic process reads for a branch, relative
    to the HUC data directory.

    Arguments:
        branch_id - str: The branch identifier.
    Returns:
        List[str]: The REM and catchment raster paths.
    """

    return [
        f"branches/{branch_id}/rem_zeroed_masked_{branch_id}.tif",
        f"branches/{branch_id}/gw_catchments_reaches_filtered_addedAttributes_{branch_id}.tif",
    ]


def __read_branch_ids(branch_ids_path: str, huc_number: str) -> List[str]:
    """
    Reads the branch identifiers for a HUC from branch_ids.csv. The file has
    no header and one `HUC,branch` row per branch.

    Arguments:
        branch_ids_path - str: Path to branch_ids.csv.
        huc_number - str: The HUC identifier for the watershed.
    Returns:
        List[str]: The branch identifiers, in file order.
    """

    with open(branch_ids_path, newline="") as f:
        return [
            row[1].strip()
            for row in csv.reader(f)
            if len(row) >= 2 and row[0].strip() == huc_number
        ]


def __branches_containing_features(
    hydrotable_path: str, feature_ids: Iterable[str]
) -> Tuple[Set[str], Set[str]]:
    """
    Finds every branch whose hydrotable rows include any of the requested
    feature ids. A feature can be in several branches (e.g. branch 0 and its
    level-path branch), and the mosaic needs all of them. Values are matched
    as strings, the same way the mosaic process joins flows to the hydrotable.

    Arguments:
        hydrotable_path - str: Path to the HUC's hydrotable.csv.
        feature_ids - Iterable[str]: The requested NWM feature ids.
    Returns:
        Tuple[Set[str], Set[str]]: (branch ids, requested feature ids that
            were not found in the hydrotable).
    """

    wanted = {str(feature_id).strip() for feature_id in feature_ids}
    found_features: Set[str] = set()
    branches: Set[str] = set()

    with open(hydrotable_path, newline="") as f:
        reader = csv.reader(f)
        header = next(reader)
        feature_index = header.index("feature_id")
        branch_index = header.index("branch_id")
        for row in reader:
            if row[feature_index] in wanted:
                found_features.add(row[feature_index])
                branches.add(row[branch_index])

    return branches, wanted - found_features


def __write_fim_inputs(huc_dir: str, flood_dir: str, huc_number: str, branch_ids: List[str]) -> str:
    """
    Writes flood_<huc>/fim_inputs.csv listing the branches that are present
    on disk. The mosaic process runs every branch listed in this file, so it
    must not list branches whose rasters were not downloaded. Branches
    downloaded by earlier runs stay listed (the cache is shared between
    runs), and the file is replaced atomically so concurrent readers never
    see a partial file.

    Arguments:
        huc_dir - str: The local HUC data directory.
        flood_dir - str: The local flood_<huc> directory.
        huc_number - str: The HUC identifier for the watershed.
        branch_ids - List[str]: All branches listed in branch_ids.csv.
    Returns:
        str: The path to fim_inputs.csv.
    """

    present = [
        branch_id
        for branch_id in branch_ids
        if all(
            os.path.isfile(os.path.join(huc_dir, relative_path))
            for relative_path in __branch_raster_files(branch_id)
        )
    ]

    fim_inputs_path = os.path.join(flood_dir, "fim_inputs.csv")
    temporary_path = f"{fim_inputs_path}.{os.getpid()}.tmp"
    with open(temporary_path, "w", newline="") as f:
        writer = csv.writer(f)
        for branch_id in present:
            writer.writerow([huc_number, branch_id])
    os.replace(temporary_path, fim_inputs_path)

    return fim_inputs_path


def download_data(
    huc_number: str,
    base_dir: str,
    version=None,
    feature_ids: Optional[Iterable[str]] = None,
    include_rating_curve: bool = False,
) -> None:
    """
    Downloads the minimum data needed to run the HAND-FIM mosaic for a HUC.

    Files are fetched in two stages: first hydrotable.csv and branch_ids.csv,
    then the REM and catchment rasters of only the branches that contain
    `feature_ids`. Anything already on disk is not downloaded again, so
    calling this repeatedly (including with different reaches for the same
    HUC) only fetches what is missing, and needs no network access when
    everything is cached.

    Arguments:
        huc_number - str: The HUC identifier for the watershed.
        base_dir - str: The base directory; data lands in
            `<base_dir>/flood_<huc>/<huc>/`.
        version - str: The data version ("4.5", "4.8", or anything else for 4.9).
        feature_ids - Iterable[str]: NWM feature ids that will be mapped. If
            None, every branch in the HUC is downloaded.
        include_rating_curve - bool: Also download branches/0/hydroTable_0.csv,
            which compute_rating_increments reads for stage lookups.
    Returns:
        None
    """

    huc_number = str(huc_number)
    if feature_ids is not None:
        feature_ids = [str(feature_id).strip() for feature_id in feature_ids]
    flood_dir = os.path.join(base_dir, f"flood_{huc_number}")
    huc_dir = os.path.join(flood_dir, huc_number)
    os.makedirs(huc_dir, exist_ok=True)

    s3_path = __s3_huc_path(huc_number, version)

    # Stage 1: the lookup tables used to choose branches.
    missing_core = [
        name for name in CORE_FILES if not os.path.isfile(os.path.join(huc_dir, name))
    ]
    if missing_core:
        __s3_sync_files(s3_path, huc_dir, missing_core)

    all_branches = __read_branch_ids(os.path.join(huc_dir, "branch_ids.csv"), huc_number)

    # Stage 2: the rasters for the branches that matter.
    if feature_ids is None:
        wanted_branches = set(all_branches)
    else:
        matched, missing_features = __branches_containing_features(
            os.path.join(huc_dir, "hydrotable.csv"), feature_ids
        )
        if missing_features:
            print(
                f"WARNING: feature ids not found in HUC {huc_number}: "
                f"{sorted(missing_features)}"
            )
        wanted_branches = matched & set(all_branches)
        if not wanted_branches:
            raise ValueError(
                f"None of the requested feature ids were found in HUC {huc_number}: "
                f"{sorted(feature_ids)}"
            )

    needed = []
    for branch_id in sorted(wanted_branches):
        needed += __branch_raster_files(branch_id)
    if include_rating_curve:
        needed.append(RATING_CURVE_FILE)

    to_download = [
        relative_path
        for relative_path in needed
        if not os.path.isfile(os.path.join(huc_dir, relative_path))
    ]
    if to_download:
        print(
            f"Downloading {len(to_download)} file(s) for HUC {huc_number} "
            f"({len(wanted_branches)} branch(es))."
        )
        __s3_sync_files(s3_path, huc_dir, to_download)

        still_missing = [
            relative_path
            for relative_path in to_download
            if not os.path.isfile(os.path.join(huc_dir, relative_path))
        ]
        if still_missing:
            raise FileNotFoundError(
                f"Files not found at {s3_path}: {', '.join(still_missing)}"
            )
    else:
        print(f"FIM data for HUC {huc_number} already downloaded. Skipping download.")

    fim_inputs_path = __write_fim_inputs(huc_dir, flood_dir, huc_number, all_branches)
    print(f"Data for HUC {huc_number} downloaded to {huc_dir}; branches listed in {fim_inputs_path}.")


# IN the replacement of Docker, the environement variables are set in the .env file
def EnvFile(code_dir):
    env_dir = os.path.join(code_dir, ".env")
    env_content = """
    inputsDir=inputs
    outputsDir=output
    """
    os.makedirs(os.path.dirname(env_dir), exist_ok=True)

    with open(env_dir, "w") as f:
        f.write(env_content)


def DownloadHUC8(huc, version=None, feature_ids=None, include_rating_curve=False):
    """
    Sets up the working directories and downloads the data needed to run
    HAND-FIM for a HUC. See `download_data` for the download behavior.

    Arguments:
        huc - str: The HUC identifier for the watershed.
        version - str: The data version ("4.5", "4.8", or anything else for 4.9).
        feature_ids - Iterable[str]: NWM feature ids that will be mapped.
        include_rating_curve - bool: Also download branches/0/hydroTable_0.csv.
    Returns:
        None
    """

    code_dir, data_dir, output_dir = setup_directories()
    clone_repository(code_dir, version)

    download_data(str(huc), output_dir, version, feature_ids, include_rating_curve)
    EnvFile(code_dir)
