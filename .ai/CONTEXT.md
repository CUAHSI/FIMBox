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
  - Presents an input-method menu (`select_input_mode`): (1) enter parameters
    manually for the `reachfim` container command (`collect_args_manual`), or
    (2) load one or more scenarios from a JSON file matching
    `schema/schema.json` (`collect_args_from_json`), validated on the host via
    the Pydantic `ScenarioList` model from `schema/hand_fim_schema.py`
    (imported by adding `schema/` to `sys.path`) before ever starting Docker.
    The `reachfim_interval` sub-command has been removed.
  - Validates business-logic constraints Typer can't enforce (e.g., matching
    counts of reach IDs/flow rates) in `validate_args` — a no-op for the JSON
    flow since that's already schema-validated when loaded.
  - Creates `input/`/`output/` mount directories (`prepare_volumes`) and runs
    `docker run` with those volumes mounted into `/home/data/inputs` and
    `/home/output`. For the JSON flow, the scenario file is copied into the
    mounted input directory and the container's `scenario` command is
    invoked; otherwise the container's `reachfim` command is invoked with the
    manually-entered arguments.
  - **Note:** `run()` previously contained a leftover `import pdb; pdb.set_trace()`
    debug breakpoint; it has since been removed.

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
  All models use `extra = "forbid"` for strict validation. **Now actively used**
  by `src/hand_fim.py`'s JSON-file input flow to validate scenario files before
  invoking Docker (see `load_scenarios`). `pydantic` was added as a host
  dependency in `pixi.toml` specifically for this.

## Container-side code (`docker/handfim/`)

- **`docker/handfim/entry.py`** — The Typer CLI that runs *inside* the
  `cuahsi/handfim` Docker container (invoked by `src/hand_fim.py` via
  `docker run ... <command> <args>`). Commands:
  - `reachfim` — Generates FIM for one or more explicit reach IDs + flow rates.
    Downloads HUC data if needed (`__download_huc_fim`), writes a
    `feature_id,discharge` CSV per reach (`__write_flow_input_file`), runs FIM
    generation in parallel via `ProcessPoolExecutor` (`__compute_fim_scenario`
    → `runFIM.runfim`), reorganizes outputs per reach, cleans rasters, and
    converts to Cloud-Optimized GeoTIFF (COG). One output per reach (not
    merged) — used by the host's manual-entry flow.
  - `scenario` — Generates one FIM GeoTIFF per scenario defined in a JSON file
    matching `schema/schema.json` (`__load_scenarios` does light-weight
    structural validation without a pydantic dependency in the image).
    Reaches are grouped by `(ScenarioID, HUC)`; reaches sharing a HUC within a
    scenario are combined into one flow-rate file (`__write_flow_input_file`
    already supports multiple reaches per file) so the existing mosaic
    process yields a single combined raster for them. If a scenario spans
    multiple HUCs, each HUC group is computed separately and the resulting
    per-HUC rasters are cleaned (`__clean_fim_geotiff`) and then combined into
    one final raster per scenario via `rasterio.merge.merge`. Outputs are
    written to `/home/output/scenarios/[<subdir>/]<ScenarioID>.tif` and
    converted to COG. Per-HUC output rasters are located with a recursive
    glob (`{huc}_inundation/**/{label}*_inundation.tif`) rather than assuming
    a fixed nesting depth, and explicitly exclude the sibling `*_depth.tif`
    file (both share the `{label}` prefix) — this avoids picking the wrong
    file and avoids "no output raster found" false negatives if `runFIM.py`'s
    output layout ever changes.
  - `clean` — Standalone command to clean previously generated FIM GeoTIFFs.
  - Helper internals: `__crop_data` (crop raster to nonzero bounding box),
    `__clean_fim_geotiff` (binarize FIM raster: >0 → 1, else NaN, then crop),
    `__clean_fims` (apply cleaning to every `*.tif` under a directory),
    `__convert_to_cog` (batch `gdal_translate` to COG format), and
    `__load_scenarios` (parse + validate a scenario JSON file for the
    `scenario` command).

- **`docker/handfim/patches/runFIM.py`** — Patched replacement for fimserve's
  `runFIM` module. `runfim()` shells out to NOAA/OWP's
  `inundate_mosaic_wrapper.py` (from the `inundation-mapping` repo) for a given
  HUC + discharge CSV, optionally producing a depth raster, then moves outputs
  into a per-run `label` subdirectory (added to support parallel runs of the
  same HUC without mosaic outputs colliding — see inline comment dated
  06/21/25). `runOWPHANDFIM()` is a simpler wrapper that globs discharge CSVs
  for a HUC and calls `runfim` for each. This patched file is applied via
  `COPY patches/runFIM.py /home/code/FIMServ/src/fimserve/runFIM.py` in the
  Dockerfile (this `COPY` line was previously commented out, silently
  leaving the image running upstream FIMServ's unpatched `runFIM.py` the
  whole time our streaming/`-v`/`stdbuf` fixes were "in place" — now fixed).
  **Important**: the upstream FIMServ `runfim()` (from
  `CUAHSI/FIMserv@mods-for-docker-execution`, which is what actually gets
  `pip install -e .`'d before this `COPY` runs) has a *different positional
  parameter order* than our patched file — upstream is
  `(code_dir, output_dir, HUC_code, data_dir, depth=False, label="")` (depth
  5th, label 6th), while ours is `(..., label="", depth=False)` (label 5th,
  depth 6th). `entry.py`'s `__generate_fim` previously called
  `runFIM.runfim(..., label)` positionally, which happened to bind `label`
  to the *upstream* signature's `depth` parameter whenever the patch wasn't
  applied — a non-empty label string is truthy, silently turning on depth
  raster generation and leaving `label` defaulted to `""` (output landing
  directly in `{HUC_code}_inundation/` instead of a per-job subdirectory).
  Fixed by always calling with `label=label` (a keyword argument), which
  binds correctly regardless of which signature is actually in effect.

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
  - `get_stage` (CLI: `get_stage`) — flow (cms) → stage (m) via rating curve;
    used by `entry.py`'s `reachfim` command to derive output labels.
  - `get_flow` (CLI: `get_flow`) — stage (m) → flow (cms) via rating curve.
  - `compute_rating_increments` (CLI: `get_rating_increments`) — generates a
    list of (stage, flow) pairs at fixed stage increments across a reach's
    rating-curve range. No longer used by `entry.py` since the
    `reachfim_interval` command was removed; still available as a standalone
    CLI command.

## Known gaps / things to watch

- `schema/hand_fim_schema.py` doesn't appear to be consumed elsewhere yet (no
  references found in `src/` or `docker/`) — likely scaffolding for a
  future API/service wrapper around the CLI workflow.
- Most of `fimserve`'s original functionality (streamflow retrieval, plotting,
  evaluation, ML enhancement) is disabled in the patched `__init__.py`; only
  download + core HAND-FIM compute are active.
