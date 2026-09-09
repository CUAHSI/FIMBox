# FIMBox — Project Context (Python Files)

This document summarizes the purpose of every Python file in the repository, to
give future work quick orientation without re-reading all source files.

## Project overview

FIMBox is a CLI + Docker workflow tool for running CUAHSI's HAND-based Flood
Inundation Mapping (FIM). A host-side Typer CLI (`src/`) checks Docker, collects
parameters interactively, and runs the `cuahsi/handfim:latest` container. Inside
that container (`docker/handfim/`), a second Typer CLI (`entry.py`) drives the
actual FIM computation using NOAA/OWP's `inundation-mapping` tooling and the
`fimserve` package (vendored/patched under `docker/handfim/patches/`).

## Host-side CLI (`src/`)

- **`src/fimbox.py`** — Top-level Typer app / entry point (`fimbox` command).
  Defines the `Mode` enum (Derived HAND Input, HAND-FIM, HAND-ML, FIM
  Evaluation). Verifies Docker is installed and running (`check_docker`),
  presents an interactive mode-selection menu (or accepts `--mode`), and
  dispatches to `hand_fim.run()` for the HAND-FIM mode. Other modes currently
  print a "coming soon" panel (`run_coming_soon`).

- **`src/hand_fim.py`** — Implements the HAND-FIM mode end-to-end on the host:
  - Ensures the `cuahsi/handfim:latest` Docker image exists locally, offering to
    `docker pull` it if missing (`image_exists`, `pull_image`, `ensure_image`).
  - Presents a sub-command menu: `reachfim` (specific reach IDs + flow rates) vs
    `reachfim_interval` (stage increments derived from rating curves), and
    interactively collects parameters via `typer.prompt` (`collect_args`).
  - Validates business-logic constraints Typer can't enforce (e.g., matching
    counts of reach IDs/flow rates) in `validate_args`.
  - Creates `input/`/`output/` mount directories (`prepare_volumes`) and runs
    `docker run` with those volumes mounted into `/home/data/inputs` and
    `/home/output`, forwarding args to the container's `entry.py` command.
  - **Note:** `run()` currently contains a leftover `import pdb; pdb.set_trace()`
    debug breakpoint before "Preparing mount directories" — likely unintentional
    and should be removed before this is considered production-ready.

## Schema (`schema/`)

- **`schema/hand_fim_schema.py`** — Pydantic v2 models defining the HAND-FIM
  request/response contract (mirrors `schema/schema.md` / `schema/schema.json`):
  - `Reach`: one streamflow estimate for a river reach (`HUC`, `ReachID`,
    `Streamflow` in cms).
  - `Scenario`: a named (`ScenarioID`) group of `Reach` entries that together
    produce one output GeoTIFF.
  - `ScenarioList`: top-level input payload — a list of `Scenario` objects.
  - `HandFimOutput` / `HandFimResult`: output payload mapping each `ScenarioID`
    to a `geotiff_url`.
  All models use `extra = "forbid"` for strict validation. This looks like the
  schema for a future API/service layer around the HAND-FIM Docker workflow.

## Container-side code (`docker/handfim/`)

- **`docker/handfim/entry.py`** — The Typer CLI that runs *inside* the
  `cuahsi/handfim` Docker container (invoked by `src/hand_fim.py` via
  `docker run ... <command> <args>`). Commands:
  - `reachfim` — Generates FIM for one or more explicit reach IDs + flow rates.
    Downloads HUC data if needed (`__download_huc_fim`), writes a
    `feature_id,discharge` CSV per reach (`__write_flow_input_file`), runs FIM
    generation in parallel via `ProcessPoolExecutor` (`__compute_fim_scenario`
    → `runFIM.runfim`), reorganizes outputs per reach, cleans rasters, and
    converts to Cloud-Optimized GeoTIFF (COG).
  - `reachfim_interval` — Same pipeline but derives a range of flow scenarios
    from a single reach's rating curve at fixed stage increments (via
    `compute_rating_increments.compute_rating_increments`), running one FIM
    scenario per stage/flow pair.
  - `clean` — Standalone command to clean previously generated FIM GeoTIFFs.
  - Helper internals: `__crop_data` (crop raster to nonzero bounding box),
    `__clean_fim_geotiff` (binarize FIM raster: >0 → 1, else NaN, then crop),
    `__clean_fims` (apply cleaning to every `*.tif` under a directory), and
    `__convert_to_cog` (batch `gdal_translate` to COG format).

- **`docker/handfim/patches/runFIM.py`** — Patched replacement for fimserve's
  `runFIM` module. `runfim()` shells out to NOAA/OWP's
  `inundate_mosaic_wrapper.py` (from the `inundation-mapping` repo) for a given
  HUC + discharge CSV, optionally producing a depth raster, then moves outputs
  into a per-run `label` subdirectory (added to support parallel runs of the
  same HUC without mosaic outputs colliding — see inline comment dated
  06/21/25). `runOWPHANDFIM()` is a simpler wrapper that globs discharge CSVs
  for a HUC and calls `runfim` for each.

- **`docker/handfim/patches/fimserve__init__.py`** — Patched `fimserve/__init__.py`.
  Most of the original package's exports (streamflow retrieval, plotting,
  statistics, FIM evaluation, subsetting, surrogate-model enhancement, etc.) are
  commented out; only `DownloadHUC8` (data download) and `runOWPHANDFIM` (patched
  above) are actively exported. Indicates this is a trimmed-down fimserve build
  used only for the core HAND-FIM download + compute path.

- **`docker/handfim/compute_rating_increments.py`** — Typer CLI + library used
  by `entry.py` for rating-curve interpolation:
  - `interpolate_y` — generic 1D linear interpolation between two DataFrame
    columns (returns `-9999` if `x_value` is out of range).
  - `__load_rating_curve` — loads `hydroTable_0.csv` for a HUC/reach from the
    expected FIM output directory structure (`output/flood_<huc>/<huc>/branches/0`).
  - `get_stage` (CLI: `get_stage`) — flow (cms) → stage (m) via rating curve.
  - `get_flow` (CLI: `get_flow`) — stage (m) → flow (cms) via rating curve.
  - `compute_rating_increments` (CLI: `get_rating_increments`) — generates a
    list of (stage, flow) pairs at fixed stage increments across a reach's
    rating-curve range; used by `entry.py`'s `reachfim_interval` command to
    build multiple FIM scenarios from a single reach.

## Known gaps / things to watch

- `src/hand_fim.py` has a stray `pdb.set_trace()` — should be removed.
- `schema/hand_fim_schema.py` doesn't appear to be consumed elsewhere yet (no
  references found in `src/` or `docker/`) — likely scaffolding for a
  future API/service wrapper around the CLI workflow.
- Most of `fimserve`'s original functionality (streamflow retrieval, plotting,
  evaluation, ML enhancement) is disabled in the patched `__init__.py`; only
  download + core HAND-FIM compute are active.
