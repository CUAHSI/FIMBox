# FIMBox — Project Context (Python Files)

This document summarizes the purpose of every Python file in the repository, to
give future work quick orientation without re-reading all source files.

## Project overview

FIMBox provides a FastAPI/Celery service, a generated Python SDK, and a local HTTP CLI
for running CUAHSI's HAND-based Flood Inundation Mapping (FIM).
The local CLI submits jobs to the service instead of launching Docker. Inside
that container (`docker/handfim/`), a second Typer CLI (`entry.py`) drives the
actual FIM computation using NOAA/OWP's `inundation-mapping` tooling and the
`fimserve` package (vendored/patched under `docker/handfim/patches/`).

## Host-side CLI (`src/`)

- **`src/fimbox.py`** — Compatibility script entry point for `fimbox_cli.app`.
  No Docker checks or mode menu. `pixi run fimbox` uses the installed console script.
- **`src/fimbox_cli.py`** — Handwritten Click CLI using generated SDK operation
  modules/models, not runtime OpenAPI parsing or handwritten HTTP routes.
  Commands: submit, submit-envelope, jobs, status, output, scenario-output,
  cancel, session, logout, health. `--body` reads JSON.
  Defaults to `http://localhost:8000`.
  Session supplied through `FIMBOX_SESSION_TOKEN` or hidden prompt.
  The prompt shows the configured API's `/auth/login` link and instructions
  to copy the returned FIMBox `access_token`.
  No local credential persistence, Docker subprocesses, or API server imports.
  Help does not require a server. CLI rejects fields dropped by generated
  models and surfaces HTTP/transport/contract errors without retries.
- **`src/fimbox_sdk/`** — Generated, do not hand-edit. Local
  openapi-python-client 0.28.0 generates Client/AuthenticatedClient, attrs models,
  sync/async operation modules, errors and response types for every endpoint.
  Module names derive from FastAPI operation IDs. Dependencies: httpx, attrs,
  python-dateutil, typing-extensions. No server imports.
- **`sdk/openapi.json`**, **`sdk/generator.yaml`** — Captured API contract
  and local generator config. `pixi run sdk-generate` regenerates only the SDK.
- **`pyproject.toml`**, **`MANIFEST.in`** — Installable `fimbox` distribution:
  only fimbox_sdk and fimbox_cli, with fimbox console entry point. Python 3.10+.
  Does not package server/legacy workflow modules. Optional codegen dependency
  pins generator; spec/config included in source distribution.
- **`tests/test_fimbox_cli.py`** — SDK/CLI regression tests with mock HTTP,
  typed sync/async responses, source-spec parity, and submission through FastAPI.

- **`src/compose_runner.py`** — Legacy `run_compose()` helper for launching
  one-off Docker Compose service commands. Accepts the service, container
  command, environment overrides, and optional bind mounts; intended for reuse
  by host-side workflow modes.

- **`src/hand_fim.py`** — Legacy direct-container host workflow, retained but
  no longer used by the local entry point:
  - Ensures the `cuahsi/handfim:latest` Docker image exists locally, offering to
    `docker pull` it if missing (`image_exists`, `pull_image`, `ensure_image`).
  - Presents an input-method menu (`select_input_mode`): (1) enter parameters
    manually for the `reachfim` container command (`collect_args_manual`), or
    (2) load one or more scenarios from a JSON file matching
    `schema/schema.json` (`collect_args_from_json`), validated on the host via
    the Pydantic `ScenarioList` model from
    `src/schemas/hand_fim_schema.py` before ever starting Docker.
    The `reachfim_interval` sub-command has been removed.
  - Collects manual and scenario inputs with explicit Typer prompts, then
    validates them with Pydantic `ReachFimArgs` and `ScenarioArgs` models.
    Manual comma-separated reach/flow inputs are converted to shared schema
    `Reach` objects before validation; scenario-file contents are validated
    against `ScenarioList`.
  - Uses `compose_runner.run_compose()` to run the `fimbox` Compose service with
    prompted input and output bind mounts (defaults `./input` and `./output`)
    relative to the CLI's current working directory. Compose creates those host
    directories as needed. For the JSON flow, the scenario file is mounted read-only at
    `/home/data/inputs/scenario.json`; otherwise the container's `reachfim`
    command is invoked with the manually-entered arguments.
  - **Note:** `run()` previously contained a leftover `import pdb; pdb.set_trace()`
    debug breakpoint; it has since been removed.

## Schema (`src/schemas/`)

- **`src/schemas/hand_fim_schema.py`** — Pydantic v2 models defining the HAND-FIM
  request/response contract (mirrors `schema/schema.md` / `schema/schema.json`):
  - `Reach`: one streamflow estimate for a river reach (`HUC`, `ReachID`,
    `Streamflow` in cms).
  - `Scenario`: a named (`ScenarioID`) group of `Reach` entries that together
    produce one output GeoTIFF.
  - `ScenarioList`: top-level input payload — a list of `Scenario` objects.
  - `HandFimOutput` / `HandFimResult`: output payload mapping each `ScenarioID`
    to a `geotiff_url`.
  All models use `extra = "forbid"` for strict validation.
  Swagger reach examples use the sample HUC/reach pair `03020201` / `8780625`,
  not the former placeholder HUC `12345678`.
  Actively used by `src/hand_fim.py`'s JSON-file input flow to validate scenario files before
  invoking Docker (see `load_scenarios`). `pydantic` was added as a host
  dependency in `pixi.toml` specifically for this.

## API and worker (`src/fimbox_api/`)

Adapted from the application in `CUAHSI/fimbox-api` at commit
`b6ddd220bd72c950cfa16a6f76bf3de3a54acd11`; the template's Terraform deployment
is not copied. OAuth/session/job-ownership behavior is adapted from the
`authentication` branch at `5da02c2171aca24ab69a0687be64b730e4d0272c`.

- **`src/fimbox_api/__init__.py`** — Package marker and template attribution.
- **`src/fimbox_api/config.py`** — Environment-based API/worker settings,
  Redis URLs, metadata TTL, visibility timeout, HAND-FIM runtime paths,
  S3-compatible endpoint/signing settings, and HydroShare OAuth configuration.
  OAuth credentials accept `API_CLIENT_ID` / `API_CLIENT_SECRET` aliases;
  `HYDROSHARE_CLIENT_*` takes precedence. Swagger publishes only the client ID;
  the secret is used by the server-side authorization-code exchange.
  The API Compose service loads root `.env` in raw mode to preserve literal
  credential characters; Compose 2.30+ is required.
- **`src/fimbox_api/celery_app.py`** — JSON-only Celery configuration and
  task discovery, started-state tracking, late acknowledgement, and a
  single-process worker.
- **`src/fimbox_api/models.py`** — Job response models and request models
  reusing `src/schemas/hand_fim_schema.py`. Adds API-specific unique
  scenario ID, HUC-8, finite streamflow validation, per-job output bucket/prefix,
  stored destination metadata, and presigned-URL responses.
  Bucket is optional (local default); prefix defaults empty and requires a
  bucket when nonempty. Result includes local_output_dir; local file keys are null.
- **`src/fimbox_api/main.py`** — FastAPI app: `POST /jobs` accepts the
  template's typed `payload` envelope; `POST /jobs/hand-fim` accepts the
  scenario array directly with destination query parameters. All job routes
  require a session and enforce user ownership. Also provides user job listing,
  polling, fresh S3 presigned URL responses or authenticated local content URLs,
  queued-job cancellation, public health, and explicit storage/queue errors.
  Local `/jobs/<uuid>/files/<index>/content` streams files from the read-only
  shared output mount with ownership and traversal/symlink checks.
- **`src/fimbox_api/tasks.py`** — Real HAND-FIM Celery task. Revalidates input,
  substitutes safe job-specific internal scenario names, writes the scenario
  JSON, invokes the existing container `scenario` CLI in a subprocess, verifies
  every COG exists, recursively uploads all files from the job-specific
  `/home/output/scenarios/<job_id>/` directory with unchanged relative filenames
  under `<prefix>/<job_id>/`, and returns a file manifest plus scenario URLs.
  Shared watershed caches and other jobs are excluded. Redelivery clears
  local job files first. `/jobs/<job_id>/files/<index>` signs any manifest file;
  scenario endpoints sign the preserved COG key, with legacy-key compatibility
  for old results without manifests.
  Without a bucket, leaves files on the Compose output bind mount, skips
  credential generation/S3 entirely, and returns local output metadata.
  Compose preserves the existing FIMBOX_OUTPUT_DIR bind configuration
  (default ../output relative to docker/api); API mounts the same path read-only.
- **`src/fimbox_api/storage.py`** — Shared S3 client configuration, safe
  job-specific object-key construction, object existence checks, and SigV4
  presigned GET URLs using job-specific temporary credentials. Submissions
  exchange the user's HydroShare access token through
  `/hsapi/user/service/accounts/s3/` (`{"expiry": <days>}`), store the returned
  access/secret/expiry only in Redis, and use them for uploads and signing at
  `https://s3.hydroshare.org`. No static AWS credential fallback. Signed-URL
  lifetime is capped by credential expiry. Credential failure prevents enqueue.
- **`src/fimbox_api/auth.py`** — HydroShare authorization-code exchange,
  verified user-info identity, Redis-backed opaque sessions with expiry,
  single-use browser-bound OAuth state, and per-user job ownership/tracking.
  Provider tokens stay in Redis. No prototype mock-token or client-supplied
  identity fallback; bearer authentication uses the real supplied token.
  OAuth failures report the token-exchange/user-info stage, HTTP status, and
  allowlisted OAuth error codes without exposing provider bodies or tokens.
- **`src/fimbox_api/auth_routes.py`** — Browser login/callback, PKCE form-token
  exchange for Swagger, authenticated session info, and local session logout.
- **`tests/test_fimbox_api.py`** — Focused `unittest` coverage for API
  validation/queueing, status/downloads, cancellation, worker command
  construction, identifier isolation, missing/stale outputs, and failures.
  The FIM computation subprocess is mocked; these tests do not download data.
- **`tests/test_fimbox_auth.py`** — OAuth/state/replay, provider failure,
  session expiry/logout, route authentication, PKCE, and ownership tests.
- **`tests/test_container_scenario.py`** — Container-only regression tests
  that load the real `entry.py` and mock computation/downloads to prove that
  future failures and missing HUC rasters propagate. Skipped on the host.

Deployment assets live in `docker/api/`: a slim API Dockerfile, a worker
Dockerfile extending `cuahsi/handfim:latest`, a role-based entrypoint, pinned
requirements, and an independent Compose stack. Only the worker mounts the
output volume; the API signs uploaded S3 objects. Redis is not exposed to the host.
The worker runs `/home/entry.py` directly without a Docker socket. Keep a
single worker per shared watershed cache. `pixi run api` runs FastAPI only.

## Container-side code (`docker/handfim/`)

- **`docker/handfim/entry.py`** — The Typer CLI that runs *inside* the
  `cuahsi/handfim` Docker container (invoked by `src/hand_fim.py` via
  `docker compose run ... <command> <args>`). Commands:
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
    converted to COG. Scenario computation futures are explicitly resolved so
    errors propagate; missing per-HUC rasters fail the command rather than
    silently returning partial success. Per-HUC output rasters are located
    with a recursive
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
  same HUC without mosaic outputs colliding — see inline comment dated 06/21/25).
  Nonzero mapping subprocess exits raise `CalledProcessError` so the original
  computation failure propagates through scenario futures.
  `runOWPHANDFIM()` is a simpler wrapper that globs discharge CSVs
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

- The shared schema lives under `src/schemas/` and is consumed by both the
  host CLI and the API/worker. Some older CLI help/documentation still refers
  to the former `schema/` directory.
- API result/session/ownership metadata expires; there is no durable job
  registry, automatic object cleanup, provider-token auto-refresh, or
  coordinated multi-worker watershed cache. Configure OAuth and S3 credentials
  before using job routes; jobs from before this integration are not migrated.
- The API/worker now has focused automated tests, but there is no configured
  linter/formatter or full scientific-computation integration test suite.
- Most of `fimserve`'s original functionality (streamflow retrieval, plotting,
  evaluation, ML enhancement) is disabled in the patched `__init__.py`; only
  download + core HAND-FIM compute are active.
