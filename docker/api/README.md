# FIMBox API and worker

The Compose stack runs FastAPI, Redis, and one Celery HAND-FIM worker.
The API is available at `http://localhost:8000` and its interactive docs at
`/docs`. Redis is internal to the Compose network.

Start and stop from the repository root:

```bash
docker compose up --build
docker compose down
```

Compose builds the worker on `linux/amd64` from `cuahsi/handfim:latest`. Build
the base image locally only when changing the container implementation; see
[the HAND-FIM guide](../handfim/README.md).

## Local mode and authentication

With neither HydroShare OAuth credential configured, the API uses a shared
local identity. Anyone who can reach the service can submit, inspect, or
cancel local jobs, so keep the loopback-only default for trusted local use.
Set both `HYDROSHARE_CLIENT_ID` and `HYDROSHARE_CLIENT_SECRET` to require
authenticated sessions; setting only one is a startup error. The API reports
its mode at `GET /auth/config`.

To enable local HydroShare login, register a confidential OAuth client and put
both credentials in the root `.env` file (never commit it). Register these
exact callback URLs with the client:

- `http://localhost:8000/auth/callback`
- `http://localhost:8000/docs/oauth2-redirect`

Compose reads `.env` literally for the API; use unquoted values. Recreate the
API after changing credentials:

```bash
docker compose --env-file /dev/null up -d --build api
```

The CLI discovers whether authentication is enabled and uses a FIMBox session
token, not the HydroShare provider token. See the [SDK and CLI guide](../../sdk/README.md)
for login and token handling. For public deployments, use HTTPS, secure OAuth
state cookies, protected Redis, and matching public callback URLs.

## Jobs and output

Submit a scenario JSON array through the CLI:

```bash
pixi run fimbox submit --body test-data/single_reach.json
```

Or use `POST /jobs/hand-fim` with an object containing `scenarios` and optional
`output_bucket`, `output_prefix`, `overwrite`, `max_procs`, and
`execution_mode` fields.
Requests require unique scenario IDs, eight-digit HUCs, and finite,
nonnegative streamflows. The API validates requests before enqueueing.

Without `output_bucket`, the worker writes to the Compose output mount under
`scenarios/<job_id>/`, then removes the files before job completion. Results
contain metadata, not downloadable artifacts. To retain output, provide a
HydroShare S3 bucket and optional prefix; the bucket/resource must already
exist and the submitting user must have write access. Uploaded files are
reported in the job's file manifest. Existing keys cause failure unless
`overwrite` is enabled.

Both legacy and streaming jobs also save the validated input scenario array as
`<prefix>/scenarios.json` (`application/json`), preserving scenario IDs, reaches,
and streamflows in the same format accepted as input. It is included in the
result's `files` list and retained alongside the S3 outputs after job metadata
expires. The same overwrite policy applies to this key: use a separate prefix
for independent runs, or enable `overwrite` to replace it. Local-only jobs still
remove all files during cleanup.

Both modes also retain `<prefix>/README.md` (`text/markdown`), listed in `files`.
It describes the HAND-FIM outputs and input units, records the job ID, UTC
worker start time, execution mode, effective process limit, overwrite setting,
scenario/reach counts, and output paths. Streaming runs also record the prepared
hydrofabric location and processing tile size. It includes rerun and path-based
map examples without credentials. Exact software and immutable dataset versions
are not captured. The README follows the same overwrite policy as other files.

HydroShare S3 output requires OAuth authentication. For each such job, the API
requests temporary credentials and stores them in Redis; neither provider
tokens nor S3 credentials are passed in Celery arguments or returned to the
client. There is no static AWS-credential fallback.

`execution_mode` defaults to `legacy`. `updatedlegacy` runs the same FIMserv
workflow with separately pinned NOAA v4.8.14.0, using the same inputs and
producing the same COG layout. It is intended for comparison against the
original legacy implementation and streaming; rebuild the base/worker image
before selecting it. See [updated legacy mode](../handfim/README.md#updated-legacy-mode).
`updatedstreaming` is streaming with NOAA v4.8.14.0's own per-pixel rule
(including its ~3 cm minimum depth); it has the same requirements as
`streaming`. See [updated streaming mode](../handfim/README.md#updated-streaming-mode).
`streaming` runs the
[streaming HAND-FIM mode](../handfim/README.md#streaming-mode): it requires
`output_bucket`, permits only `max_procs` 1, and returns 409 unless the
deployment sets `STREAM_INPUT_BUCKET`. The worker passes the job's temporary
S3 credentials to the computation through its environment, never its command
line. Tiles go directly to `<prefix>/<ScenarioID>/<huc>/<row>_<col>.cog`; the
`<prefix>/<ScenarioID>/manifest.json` that lists them is published only after
every scenario succeeds, and the job result reports it in
`scenario_manifests` (`files` contains `scenarios.json` and `README.md`, not the raster
tiles). These stable tile paths make
overwrites non-atomic: readers can see mixed old/new tiles during a replacement,
and a failed run can leave partially overwritten or unreferenced tiles.
Failure cleanup retains tiles rather than risking deletion of shared output.
A manifest this job already published is rolled back to the replaced version
unless another job has published over it since; an internal `publication_id`
in the manifest identifies its publisher but is not part of any file path.
Manifest rollback cannot restore overwritten raster bytes.
Do not run concurrent jobs targeting the same scenario prefix.
An existing manifest causes failure unless `overwrite` is enabled. After a
successful replacement, tiles referenced only by the previous manifest are
deleted, while tiles reused by the new manifest are retained. Older manifests
with run-specific tile paths remain readable and are cleaned up when replaced.

`GET /jobs` lists tracked jobs. Use `GET /jobs/{job_id}` to poll and
`GET /jobs/{job_id}/logs?offset=0` to read worker logs by byte offset.
`DELETE /jobs/{job_id}` cancels a queued job on a best-effort basis; running
jobs cannot be interrupted. Job metadata and ownership expire according to
`RESULT_TTL_SECONDS` (default 86400 seconds).

### Legacy/streaming parity jobs

`POST /jobs/parity` checks the streaming implementation against a retained
legacy run. The body has `legacy_bucket` and `legacy_prefix` (the legacy run's
`output_prefix`), plus `output_bucket`, `output_prefix`, and optional
`overwrite` for the new streaming outputs. The worker reads the legacy run's
`scenarios.json`, confirms every `<ScenarioID>.cog` exists, and reruns the
same scenarios as a normal streaming job at the output prefix. It then
compares each legacy COG with the union of that scenario's streaming tiles,
cell by cell on the legacy grid. Like streaming jobs, parity requires
`STREAM_INPUT_BUCKET` and authentication, and uses `STREAM_QUEUE` if it is
set. The output prefix must differ from the legacy prefix.

The retained reference may also be an `updatedlegacy` run: pass its bucket
and prefix as `legacy_bucket` and `legacy_prefix`. The parity endpoint does
not rerun the reference; it compares its saved COGs against a fresh streaming run.
Set the optional `execution_mode` to `updatedstreaming` (default `streaming`)
to rerun with NOAA's newer rule; this is the like-for-like check against an
`updatedlegacy` reference.

Poll `GET /jobs/{job_id}` as usual. The result's `parity` list gives, per
scenario, `matches`, flooded/legacy-only/streaming-only/different cell
counts, `missing_reaches`, and up to 10 `sample_differences` in the legacy
CRS. If the streaming mask is offset from legacy by whole pixels (at most 2
rows/columns, estimated from the flooded-cell centroids), it is realigned
before counting when that reduces the differences; the offset is reported as
`grid_shift_rows`/`grid_shift_columns` (positive is south/east), with the
pre-alignment count in `unaligned_different_cells`. `matches` is false if any
cell differs after alignment or any reach was missing from the streaming
dataset; a grid shift alone does not cause a mismatch. Differences are reported, not failed: the job succeeds and
logs `MATCH`/`MISMATCH` per scenario. `<output_prefix>/parity/report.json`
holds the full report. `<output_prefix>/parity/<ScenarioID>.diff.tif` is
drawn after alignment on the legacy grid (padded by 2 pixels) and uses
0 for equal cells, 1 for legacy-only flooding, and 2 for streaming-only
flooding. Both are also listed in `files`. Existing parity outputs fail the
job before computation unless `overwrite` is enabled.

### Comparing two results

`POST /jobs/compare` diffs two retained results without rerunning HAND-FIM,
for example a legacy run against a streaming run, or two runs with different
settings. The body has `base_bucket`/`base_prefix` and
`candidate_bucket`/`candidate_prefix` (each an earlier job's output location),
plus `output_bucket`, `output_prefix`, and optional `overwrite`. The output
prefix must differ from both results, and the endpoint requires
authentication but not streaming configuration.

The worker reads both `scenarios.json` files and compares the ScenarioIDs they
share; any others are listed in the result's `compared.skipped_scenarios`, and
the job fails if none are shared. For each result, a scenario's flood mask is
its `<ScenarioID>/manifest.json` tiles if present, otherwise `<ScenarioID>.cog`.
The comparison uses the same grid-shift handling and outputs as parity jobs.
In the result's `parity` entries and the diff rasters, `legacy_*` fields and
value 1 describe the base, and `streaming_*` fields and value 2 describe the
candidate. `/outputs/tiles` and the job raster routes draw `*.diff.tif` files
with red for base-only and orange for candidate-only flooding.

## Web page

`GET /ui` serves a browser page with three tabs:

- **Input dataset** browses and downloads the prepared streaming hydrofabric at
  `STREAM_INPUT_BUCKET`/`STREAM_INPUT_PREFIX`. Without that setting, the tab
  explains that legacy jobs download NOAA HAND data per HUC instead.
- **Submit job** edits scenarios as a table or JSON. You can load the examples
  from the `test-data/*.json` files, upload a local JSON file, or load one
  from a URL. URLs can be public `https://` links or
  `s3://<bucket>/<key>` paths read with your HydroShare credentials.
  `/ui?scenarios_url=<url>` opens the page with the URL ready to load. Then
  set the job options and submit to `POST /jobs/hand-fim`.
- **Results** lists your jobs, streams their logs, and lets you cancel queued
  jobs. For retained S3 output it shows several views:
  - the generated `README.md`, whose relative links open files or map layers
  - the `scenarios.json` parameters, with an "Edit and resubmit" button
  - a browser for the output files
  - a Leaflet map built from the authenticated `/outputs/info|tiles` routes
  - parity reports, with each scenario's diff raster on the map

  The **Compare two results** form submits `POST /jobs/compare`; "Compare as
  base"/"Compare as candidate" on an opened result fills it in. A comparison
  job's view links to both results and shows its input parameters from the
  base result.

  Any retained output prefix can also be opened directly, even after the
  job's 24-hour metadata has expired.

Sign-in uses the same HydroShare device flow as the CLI, through a popup. The
FIMBox session token is kept in `sessionStorage`. In anonymous local mode, the
page can only submit and track jobs, because S3 browsing and maps need a
HydroShare session.

The page uses helper routes under `/ui/*` that are excluded from OpenAPI and
the SDK:

- folder listing
- previews of the first 512 KiB of text files
- Parquet table previews: the schema, row/row-group counts, and up to 200 rows
  (100 on the page) from the first batch. The API reads only the footer and
  the needed column chunks with ranged S3 GETs, stops after 64 MiB (413), and
  shortens long cells to 200 characters
- download links
- scenario JSON loading (signed in, at most 2 MiB). Because the page's CSP
  blocks direct browser fetches, the API fetches `https://` URLs itself: no
  credentials, a 10 second timeout, and at most 5 redirects. Every hop must
  resolve only to public IP addresses, and the connection is pinned to the
  address that was checked.

Downloads are short-lived (5 minute) presigned S3 URLs signed with the user's
HydroShare S3 credentials, so large files bypass the API. The URL exposes the
access key ID, but not the secret. When `STREAM_INPUT_UNSIGNED=true`, unsigned
requests read the input dataset. Leaflet loads from unpkg and basemap tiles
come from OpenStreetMap; the page's CSP permits only those origins. Examples
are read from `EXAMPLES_DIR`, which the API image sets to `/app/test-data`.

## Raster viewing

The cloud notebook displays retained outputs by storage path:

- `GET /outputs/info?bucket=resource&key=...` returns WGS84 bounds/zoom levels.
- `GET /outputs/tiles/{z}/{x}/{y}.png?bucket=resource&key=...` renders a tile.

Keys reference COG/TIFF objects or a streaming scenario's `manifest.json`.
These routes require configured authentication and use the current session's
HydroShare S3 credentials and permissions, not job ownership or result metadata.
Credentials are cached separately from jobs and renewed on expiry; no job
lookup is performed. Scenario metadata is read from the retained manifest,
and tile keys must stay within its scenario directory. Access continues after
the 24-hour job TTL while objects exist and the user has read permission.

The original job-scoped endpoints remain available:

- `GET /jobs/{job_id}/rasters/{file_index}/info` returns WGS84 bounds/zoom levels.
- `GET /jobs/{job_id}/rasters/{file_index}/tiles/{z}/{x}/{y}.png` returns a
  256-pixel Web Mercator inundation tile.

Streamed scenarios use `GET /jobs/{job_id}/scenarios/{scenario_index}/info`
and `.../scenarios/{scenario_index}/tiles/{z}/{x}/{y}.png`, where
`scenario_index` indexes `scenario_manifests`. Each map tile composites the
union of intersecting output tiles; requests touching more than
`STREAM_DISPLAY_MAX_TILES` (default 16) output tiles return 400, and the info
`minzoom` reflects that limit.

`file_index` is the zero-based index in the successful job's `files` manifest.
Only COG/TIFF entries with retained S3 keys are accepted. Each request checks
job ownership and success before accessing storage; arbitrary URLs/keys cannot
be requested. No raster files are downloaded to the API filesystem. rio-tiler
reads windows/overviews through GDAL's signed S3 range requests using a
request-scoped rasterio environment. Directory listing is disabled. Responses
are `no-store`; errors do not expose S3 credentials. Missing/near-expiry S3
credentials are renewed using the owner's current HydroShare session.

Rebuild/redeploy the API with the updated requirements. No extra public tile
service or S3 CORS configuration is required. Local metadata-only jobs cannot
be mapped after cleanup. Job metadata/ownership TTL limits only the original
job-scoped routes, not path-based reads. See [the notebook guide](../../notebooks/README.md).
The slim API image installs the native `libexpat1` runtime needed by rasterio
and checks raster imports during the build. Python requirements alone do not
provide this system library. The HAND-FIM worker base already supplies it
through its GDAL dependencies.

## Configuration notes

- `FIMBOX_API_PORT` changes the host port (default `8000`).
- `FIMBOX_INPUT_DIR` and `FIMBOX_OUTPUT_DIR` set host bind-mount directories.
- `HAND_FIM_MAX_PROCS` sets default computation concurrency (default `1`);
  a job can override it with `max_procs`.
- `RESULT_TTL_SECONDS` and `VISIBILITY_TIMEOUT_SECONDS` should exceed the
  longest queue wait and job duration. Configure them consistently for API
  and worker.
- Streaming mode: `STREAM_INPUT_BUCKET` (enables it), `STREAM_INPUT_PREFIX`,
  `STREAM_INPUT_UNSIGNED` (read a public dataset anonymously),
  `STREAM_TILE_SIZE` (output window in pixels, multiple of 512, default 2048),
  `STREAM_MIN_CREDENTIAL_SECONDS` (default 3600; the job fails early if its S3
  credentials expire sooner), `STREAM_QUEUE` (route streaming jobs to a
  dedicated worker queue), and `STREAM_DISPLAY_MAX_TILES`.
- Use one worker per shared watershed cache. Do not scale workers against the
  same filesystem without cache coordination.

For API-only host development, run `pixi install` and `pixi run api`; Redis and
a separately running container worker are still required.

## Tests

Host tests mock the expensive scientific computation:

```bash
pixi run sdk-generate
PYTHONPATH=src python -m unittest discover -s tests -v
```

Container scenario regressions require the worker image:

```bash
docker compose --env-file /dev/null run --rm --no-deps \
  -v "$PWD/tests:/tests:ro" --entrypoint /opt/conda/bin/python \
  worker /tests/test_container_scenario.py
```

For a real scientific comparison (no mocked computation), see the
[legacy/streaming parity test](../handfim/README.md#legacystreaming-parity-test).
It fails on differing flooded cells and retains a JSON report and spatial
difference rasters for diagnosis.
