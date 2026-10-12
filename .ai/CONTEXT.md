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

- **`src/fimbox_cli.py`** — Handwritten Click CLI using generated SDK operation
  modules/models, not runtime OpenAPI parsing or handwritten HTTP routes.
  Commands: submit, jobs, status, cancel, session, logout, health. `--body`
  reads a scenario JSON array.
  `submit --max-procs N` selects per-job concurrency. Omission uses
  HAND_FIM_MAX_PROCS (worker default 1).
  `fimbox`/`pixi run fimbox` are the installed console entry points.
  Defaults to `http://localhost:8000`.
  Discovers `/auth/config` via the SDK; local mode uses Client without a token,
  prompt, or keychain access. Discovery errors fail explicitly.
  Authenticated session precedence: option/environment, OS keychain scoped to API URL, hidden
  prompt. Successful prompted tokens are saved with keyring; logout and saved
  token HTTP 401 clear the entry. Only OS secure backends are accepted, no
  plaintext fallback; keychain errors are explicit. Tests mock the keychain.
  The prompt shows the configured API's `/auth/login` link and instructions
  to copy the returned FIMBox `access_token`.
  No plaintext credential files, Docker subprocesses, or API server imports.
  Help does not require a server. CLI rejects fields dropped by generated
  models and surfaces HTTP/transport/contract errors without retries.
- **`src/fimbox_sdk/`** — Generated, do not hand-edit. Local
  openapi-python-client 0.28.0 generates Client/AuthenticatedClient, attrs models,
  sync/async operation modules, errors and response types for every endpoint.
  Module names derive from FastAPI operation IDs. Dependencies: httpx, attrs,
  python-dateutil, typing-extensions. No server imports.
- **`sdk/openapi.json`**, **`sdk/generator.yaml`** — Captured API contract
  and local generator config. `pixi run sdk-generate` regenerates only the SDK.
- **`sdk/generate.py`** — Local generation driver for pinned generator 0.28.
  Corrects its parsed optional-security flag before template rendering, so
  job operations accept Client or AuthenticatedClient without changing the
  captured spec or hand-editing generated files. Warnings/errors fail generation.
- **`pyproject.toml`**, **`MANIFEST.in`** — Installable `fimbox` distribution:
  only fimbox_sdk and fimbox_cli, with fimbox console entry point. Python 3.10+.
  Does not package server modules. Optional codegen dependency
  pins generator; spec/config included in source distribution.
- **`tests/test_fimbox_cli.py`** — SDK/CLI regression tests with mock HTTP,
  typed sync/async responses, source-spec parity, and submission through FastAPI.

## Cloud example notebook (`notebooks/`)

- **`notebooks/cloud_hand_fim.ipynb`** — User-run generated SDK example: HTTPS API
  configuration, SDK auth discovery/masked ipywidgets.Password token input,
  separate Connect cell (no blocking getpass), typed direct submission
  with opt-in USE_MACOS_KEYCHAIN for clipboard/widget issues (native Keychain,
  no plaintext backend; URL matches CLI credential account),
  using AuthenticatedClient (no CLI subprocess) and bounded polling/resume.
  Setup reprocesses the kernel's site-package .pth files after editable install
  so SDK imports work without a kernel restart.
  No saved outputs or live cloud execution during validation.
- **`tests/test_cloud_notebook.py`** — Cell syntax/editable-import checks and
  mocked SDK/auth/submission/monitoring workflow.

## Schema (`src/schemas/`)

- **`src/schemas/hand_fim_schema.py`** — Pydantic v2 models defining the HAND-FIM
  request/response contract (mirrors `schema/schema.md` / `schema/schema.json`):
  - `Reach`: one streamflow estimate for a river reach (`HUC`, `ReachID`,
    `Streamflow` in cms).
  - `Scenario`: a named (`ScenarioID`) group of `Reach` entries that together
    produce one output GeoTIFF.
  - `ScenarioList`: top-level input payload — a list of `Scenario` objects.
  - `HandFimOutput` / `HandFimResult`: output metadata mapping each scenario
    to its `ScenarioID`.
  All models use `extra = "forbid"` for strict validation.
  Swagger reach examples use the sample HUC/reach pair `03020201` / `8780625`,
  not the former placeholder HUC `12345678`.
  Used by the API and worker to validate job inputs. The SDK models are
  generated separately from the API's OpenAPI schema.

## API and worker (`src/fimbox_api/`)

Adapted from the application in `CUAHSI/fimbox-api` at commit
`b6ddd220bd72c950cfa16a6f76bf3de3a54acd11`; the template's Terraform deployment
is adapted in `terraform/` for Cloud Run. OAuth/session/job-ownership behavior is adapted from the
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
  Neither OAuth credential enables shared anonymous local mode; both enforce
  authentication. Partial credentials fail startup, with input values hidden.
  REQUIRE_HYDROSHARE_OUTPUT defaults false locally; Terraform sets it true for
  the API, which fails startup without OAuth credentials and rejects missing
  output_bucket at the submission route before enqueueing.
- **`src/fimbox_api/celery_app.py`** — JSON-only Celery configuration and
  task discovery, started-state tracking, late acknowledgement, and a
  single-process worker.
- **`src/fimbox_api/models.py`** — Job response models and request models
  reusing `src/schemas/hand_fim_schema.py`. Adds API-specific unique
  scenario ID, HUC-8, finite streamflow validation, per-job output bucket/prefix,
  and stored destination metadata.
  Bucket is optional (local default); prefix defaults empty and requires a
  bucket when nonempty. Result includes local_output_dir; local file keys are null.
  HandFimJobOptions extends output options with optional positive max_procs,
  used as query parameters by the submission route. Worker revalidates it
  before execution.
- **`src/fimbox_api/main.py`** — FastAPI app: `POST /jobs/hand-fim` accepts
  the scenario array with destination query parameters. All job routes
  enforce ownership via a session when configured, or a reserved shared local
  identity without credentials. OpenAPI documents both alternatives consistently
  across modes. Also provides user job listing,
  with JobList/JobSummary objects containing job_id/status (not plain ID
  strings), sharing status normalization with individual job polling,
  queued-job cancellation, public health, and explicit storage/queue errors.
- **`src/fimbox_api/tasks.py`** — Real HAND-FIM Celery task. Revalidates input,
  substitutes safe job-specific internal scenario names, writes the scenario
  JSON, invokes the existing container `scenario` CLI in a subprocess, verifies
  every COG exists, recursively uploads all files from the job-specific
  `/home/output/scenarios/<job_id>/` directory with unchanged relative filenames
  under `<prefix>/<job_id>/`, and returns a file manifest plus scenario IDs.
  Shared watershed caches and other jobs are excluded. Redelivery clears
  local job files first.
  Without a bucket, leaves files on the Compose output bind mount, skips
  credential generation/S3 entirely, and returns local output metadata.
  Compose preserves the existing FIMBOX_OUTPUT_DIR bind configuration
  (default ./data/output relative to the repository root); only the worker mounts it.
- **`src/fimbox_api/storage.py`** — Shared S3 client configuration and safe
  job-specific object-key construction using job-specific temporary credentials. Submissions
  exchange the user's HydroShare access token through
  `/hsapi/user/service/accounts/s3/` (`{"expiry": <days>}`), store the returned
  access/secret/expiry only in Redis, and use them for uploads at
  `https://s3.hydroshare.org`. No static AWS credential fallback. Credential
  failure prevents enqueue.
- **`src/fimbox_api/auth.py`** — HydroShare authorization-code exchange,
  verified user-info identity, Redis-backed opaque sessions with expiry,
  single-use browser-bound OAuth state, and per-user job ownership/tracking.
  Provider tokens stay in Redis. No prototype mock-token or client-supplied
  identity fallback when authentication is configured; bearer authentication
  uses the real supplied token. Anonymous local mode uses a reserved identity
  that cannot be issued by the provider or loaded from an authenticated session.
  OAuth failures report the token-exchange/user-info stage, HTTP status, and
  allowlisted OAuth error codes without exposing provider bodies or tokens.
- **`src/fimbox_api/auth_routes.py`** — Browser login/callback, PKCE form-token
  exchange for Swagger, authenticated session info, and local session logout.
  Public `/auth/config` advertises authentication_enabled; logout in anonymous
  mode returns 409. HydroShare S3 submissions require configured authentication.
- **`tests/test_fimbox_api.py`** — Focused `unittest` coverage for API
  validation/queueing, status, removed output routes, cancellation, worker command
  construction, identifier isolation, missing/stale outputs, and failures.
  The FIM computation subprocess is mocked; these tests do not download data.
- **`tests/test_fimbox_auth.py`** — OAuth/state/replay, provider failure,
  session expiry/logout, route authentication, PKCE, and ownership tests.
- **`tests/test_container_scenario.py`** — Container-only regression tests
  that load the real `entry.py` and mock computation/downloads to prove that
  future failures and missing HUC rasters propagate. Skipped on the host.

## Google Cloud Terraform (`terraform/`)

Adapted from CUAHSI/fimbox-api b6ddd220bd72c950cfa16a6f76bf3de3a54acd11.
Provisions Cloud Run API and google-beta worker pool with separate images,
private VPC/PSA Memorystore (DB0 broker, DB1 results, DB2 sessions/ownership/S3
credentials), Artifact Registry, runtime service accounts and scoped IAM.
Secret Manager OAuth versions may be populated externally (null inputs, pinned
oauth_secret_version), or managed using sensitive hydroshare_client_id/secret
tfvars inputs (values enter state, API pins created versions).
s3_endpoint_url configures API signing and worker uploads together; it must
accept HydroShare-issued credentials. Only the API has
OAuth secret access. API policy always requires HydroShare authentication and
S3 destination; no shared local output mount is deployed.

`deploy_services=false` bootstraps registry/network/Redis/secrets before images
and secret versions exist. API min/max instances, request concurrency, and HTTP
timeout are wired through api_min_instances/api_max_instances/api_concurrency/
api_timeout_seconds (defaults 0/5/80/60).
Enable afterwards. The initial invalid public origin
must be replaced with api_url and applied before registering OAuth redirects.
Worker scratch/cache storage is ephemeral and consumes its memory budget
(default 4 CPU/16Gi); this deployment needs workload-specific capacity checks.
No Filestore, durable metadata archive, or queue autoscaler is supplied.
`terraform/build_push_images.sh TAG` reads registry_url from applied state,
checks tools/Docker, configures gcloud's credential helper, builds both
Linux/amd64 images and pushes them. Prints tfvars image assignments only after
both uploads; never edits configuration or applies infrastructure.
`tests/test_build_push_images.sh` validates it with exported Bash command mocks,
including failures and invocation outside the repository; no real uploads.
`terraform/tests/deployment.tftest.hcl` uses mock providers (Terraform 1.7+) for
bootstrap/auth/S3/secret/image wiring; no actual Google resources are created.

Deployment assets live in `docker/api/`: a slim API Dockerfile, a worker
Dockerfile extending `cuahsi/handfim:latest`, a role-based entrypoint, and pinned
requirements. Root `docker-compose.yml` runs API/worker/Redis; its project
name remains `api` to preserve existing containers and Redis sessions after
consolidation. Both API (read-only) and worker mount data/output by default.
The base HAND-FIM image is built separately from docker/handfim; Compose
contains only API, worker, and Redis.
Redis is not exposed to the host.
The worker runs `/home/entry.py` directly without a Docker socket. Keep a
single worker per shared watershed cache. `pixi run api` runs FastAPI only.

## Container-side code (`docker/handfim/`)

- **`docker/handfim/entry.py`** — The Typer CLI that runs *inside* the
  `cuahsi/handfim` Docker container (invoked by the Celery worker's
  scenario subprocess). Commands:
  - `reachfim` — Generates FIM for one or more explicit reach IDs + flow rates.
    Downloads HUC data if needed (`__download_huc_fim`), writes a
    `feature_id,discharge` CSV per reach (`__write_flow_input_file`), runs FIM
    generation in parallel via `ProcessPoolExecutor` (`__compute_fim_scenario`
    → `runFIM.runfim`), reorganizes outputs per reach, cleans rasters, and
    converts to Cloud-Optimized GeoTIFF (COG). One output per reach (not
    merged). Available directly in the container, not through the job API.
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
