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

HydroShare S3 output requires OAuth authentication. For each such job, the API
requests temporary credentials and stores them in Redis; neither provider
tokens nor S3 credentials are passed in Celery arguments or returned to the
client. There is no static AWS-credential fallback.

`execution_mode` defaults to `legacy`. `streaming` runs the
[streaming HAND-FIM mode](../handfim/README.md#streaming-mode): it requires
`output_bucket`, permits only `max_procs` 1, and returns 409 unless the
deployment sets `STREAM_INPUT_BUCKET`. The worker passes the job's temporary
S3 credentials to the computation through its environment, never its command
line. Tiles go directly to `<prefix>/<ScenarioID>/<huc>/<row>_<col>.cog`; the
`<prefix>/<ScenarioID>/manifest.json` that lists them is published only after
every scenario succeeds, and the job result reports it in
`scenario_manifests` (with `files` empty). These stable tile paths make
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

## Raster viewing

The cloud notebook displays retained rasters through authenticated endpoints:

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
be mapped after cleanup. Job metadata/ownership TTL still limits API access to
retained S3 rasters. See [the notebook guide](../../notebooks/README.md).
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
