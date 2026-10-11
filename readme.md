## Building Docker Images

```
docker compose -f docker/docker-compose.yml build
```

## HAND-FIM API and worker

The `src/fimbox_api` package adapts the application template from
[CUAHSI/fimbox-api](https://github.com/cuahsi/fimbox-api/tree/b6ddd220bd72c950cfa16a6f76bf3de3a54acd11/code).
It keeps FastAPI, Celery, Redis, job submission, status polling, and queued-job
cancellation, replacing the demonstration task with HAND-FIM. The template's
GCP/Terraform deployment is not included here.
HydroShare OAuth and Redis sessions are adapted from the template's
[`authentication` branch](https://github.com/cuahsi/fimbox-api/tree/5da02c2171aca24ab69a0687be64b730e4d0272c).

Start the local stack from the repository root:

```bash
docker compose -f docker/api/docker-compose.yml up --build
```

The API is available at `http://localhost:8000`, with interactive documentation
at `/docs`. Redis is internal to the stack. The worker image extends
`cuahsi/handfim:latest` and invokes `/home/entry.py scenario` directly; it does
not need access to the Docker socket. Build the existing HAND-FIM image first
if you want to use a locally rebuilt base.

### HydroShare authentication

Register a confidential OAuth client with HydroShare and configure
`HYDROSHARE_CLIENT_ID` and `HYDROSHARE_CLIENT_SECRET` in the root `.env`
for Compose, or in your environment for host-side execution
(never commit credentials).

`API_CLIENT_ID` and `API_CLIENT_SECRET` are also accepted as aliases in the
root `.env` file; the `HYDROSHARE_CLIENT_*` settings take precedence. Compose
loads this optional file into the API with `format: raw` (Compose 2.30+), so
credential values must be literal and unquoted, including any `$` characters.
Swagger's
**Authorize** dialog at `/docs` prefills the configured client ID. Leave its
client-secret field blank: `/auth/oauth/token` uses the configured secret
server-side when exchanging the authorization code with HydroShare. The
secret is never embedded in the Swagger page or sent to the browser.

After adding or changing credentials, recreate the API to load them:

```bash
docker compose --env-file /dev/null -f docker/api/docker-compose.yml up -d --build api
```

This bypasses Compose's global `.env` interpolation; the API still loads
the root `.env` literally through its service-level `env_file`. Export
non-credential Compose settings if needed when using this command.
Then reload `/docs` before clicking **Authorize**.

If `/auth/oauth/token` returns HTTP 502, its response and API logs identify
the failing provider stage and HTTP status without exposing credentials:

- `token exchange ... invalid_client`: verify the client ID/secret and that
  the client is registered with the configured OAuth provider.
- `token exchange ... invalid_grant`: start a fresh authorization attempt;
  codes are short-lived and single-use. Check the exact redirect URI and PKCE
  configuration at the provider.
- `user-info lookup ... HTTP 401/403`: verify the granted scopes and that
  `HYDROSHARE_USERINFO_URL` accepts access tokens from the configured provider.
- A connection failure indicates a provider connectivity/TLS/timeout problem.

#### Redirect URLs to register with the OAuth client

For the local API at `http://localhost:8000`, register these exact redirect
URIs in the HydroShare OAuth client's allowed redirect URLs:

| Login flow                                     | Redirect URI                                   | API environment setting           |
| ---------------------------------------------- | ---------------------------------------------- | --------------------------------- |
| Browser login via`/auth/login`               | `http://localhost:8000/auth/callback`        | `HYDROSHARE_OAUTH_REDIRECT_URI` |
| **Authorize** in Swagger UI at `/docs` | `http://localhost:8000/docs/oauth2-redirect` | `SWAGGER_OAUTH_REDIRECT_URI`    |

If you are authorizing from Swagger UI, the redirect URL to register is
**`http://localhost:8000/docs/oauth2-redirect`**. Register both URIs to support
both login flows. Do not register `/docs`, `/auth/login`, or `/auth/oauth/token`
as the redirect URI.

Redirect URIs must match exactly, including scheme, hostname, port, and path;
these defaults have no trailing slash. `localhost` and `127.0.0.1` are not
interchangeable. Use the same hostname when opening the API in your browser
and when configuring the OAuth client.

For a deployed API, replace `http://localhost:8000` with its public HTTPS
origin in both registered URIs and both environment settings. For example:

```dotenv
HYDROSHARE_OAUTH_REDIRECT_URI=https://fimbox.example.org/auth/callback
SWAGGER_OAUTH_REDIRECT_URI=https://fimbox.example.org/docs/oauth2-redirect
```

Register those same two HTTPS URLs with the deployed OAuth client, then
recreate the API service so it picks up the settings.

The default provider endpoints are HydroShare's `/o/authorize/`, `/o/token/`,
and `/hsapi/userInfo/`. For another compatible OAuth/OIDC deployment, configure
`HYDROSHARE_OAUTH_AUTHORIZE_URL`, `HYDROSHARE_OAUTH_TOKEN_URL`, and
`HYDROSHARE_USERINFO_URL`; the user-info response must contain an authenticated
`username` or `sub`. Also configure the matching scopes with `HYDROSHARE_SCOPE`
(default `read write`).

Visit `http://localhost:8000/auth/login` in a browser. After provider login,
`/auth/callback` exchanges the authorization code, verifies the user through
the provider's user-info endpoint, and returns:

```json
{
  "access_token": "<FIMBox session token>",
  "token_type": "bearer",
  "expires_in": 86400,
  "user_id": "<authenticated username>",
  "scope": "read write"
}
```

Use this **FIMBox session token**, not the provider token, as
`Authorization: Bearer <token>` for every job route. Store it securely.
`GET /auth/session` returns the current identity and expiry; `POST /auth/logout`
revokes the local session. Provider access/refresh tokens are stored only in
Redis, never returned in API responses or sent to the FIM task. At job
submission, the API uses the HydroShare access token to request temporary
S3 credentials for the submitting user.

Login state is tied to an HttpOnly browser cookie and consumed once.
Swagger uses authorization-code + PKCE via `POST /auth/oauth/token`.
Unlike the upstream prototype, missing configuration and provider failures
never mint mock tokens, client-credentials are not treated as user login,
caller-supplied user IDs are not accepted, and bearer authentication uses the
actual supplied session token.

`SESSION_TTL_SECONDS` defaults to 86400; sessions do not auto-refresh provider
tokens. Use HTTPS and `OAUTH_STATE_COOKIE_SECURE=true` in production. Compose
defaults this flag to `false` only for local HTTP development.
When changing the host/port, update both redirect URI settings and the
provider's registered URLs. Access logging is disabled for the supplied API
commands to avoid logging OAuth callback codes.

### HydroShare S3 output storage

Outputs are written to **`https://s3.hydroshare.org`** by default.
`S3_ENDPOINT_URL` may override this for a compatible HydroShare deployment;
also configure its credential API URL. `S3_REGION` defaults to `us-east-1`
and `S3_ADDRESSING_STYLE` to `path`.

For each submitted job, the API makes an authenticated request:

```http
POST https://www.hydroshare.org/hsapi/user/service/accounts/s3/
Authorization: Bearer <HydroShare access token>
Content-Type: application/json

{"expiry": 1}
```

The provider returns `access_key`, `secret_key`, and `expiry`. This uses
HydroShare's service-account API, not AWS STS. The lifetime is expressed in
**days**; `HYDROSHARE_S3_CREDENTIALS_EXPIRY_DAYS` defaults to `1`.
`HYDROSHARE_S3_CREDENTIALS_URL` configures the exchange endpoint.
See [HydroShare's service-account implementation](https://github.com/hydroshare/hydroshare/blob/bfec0e08ccb74561c813353f585c485d9e1f6a5d/hs_rest_api/views/service_account_minio.py)
for the request/response contract.

Temporary credentials are stored under a job-specific Redis key and fetched
by the worker at upload time. Neither provider tokens nor S3 credentials are
sent in Celery task arguments, returned in job results, or written to local
files. API downloads use the same job credentials. There is **no fallback**
to static AWS environment credentials or a privileged service account.
Credential issuance failure returns HTTP 503 without queueing the job.

The caller supplies `output_bucket` and `output_prefix` per job. The bucket
and destination HydroShare resource must already exist. Supply the resource's
S3 bucket and writable prefix, not an arbitrary new bucket. The submitting
HydroShare user must have write access there; `HeadObject`/`GetObject` are
also needed for URL issuance/downloads. Output paths append
`<job_id>/<index>.tif` to your prefix. Job ownership checks do not bypass
HydroShare resource permissions.

Submit the existing scenario JSON array as the request body with destination
query parameters (an empty prefix explicitly selects the bucket root):

```bash
curl --fail -X POST \
  'http://localhost:8000/jobs/hand-fim?output_bucket=fim-output&output_prefix=data/output' \
  -H "Authorization: Bearer $FIMBOX_SESSION_TOKEN" \
  -H 'Content-Type: application/json' \
  --data-binary @test-data/single_reach.json
```

Alternatively, authenticated `POST /jobs` accepts a JSON envelope:

```json
{
  "output_bucket": "fim-output",
  "output_prefix": "data/output",
  "payload": [
    {
      "ScenarioID": "single-reach-scenario",
      "Reaches": [
        {"HUC": "03020201", "ReachID": "8780625", "Streamflow": 227}
      ]
    }
  ]
}
```

Both routes use the shared Pydantic scenario schema from
[`src/schemas/hand_fim_schema.py`](src/schemas/hand_fim_schema.py). API requests
also require unique scenario identifiers, eight-digit HUCs, and finite
nonnegative flows. Invalid requests return HTTP 422 without enqueueing work;
unavailable Redis returns HTTP 503. CSV discharge files are generated by the
container, not accepted as API request bodies.

Submission returns HTTP 202:

The Swagger examples use the sample HUC/reach pair `03020201` / `8780625`.
Use a real reach belonging to the specified HUC, with available HAND data.
The former `12345678` / `9821264` examples were placeholders, not a runnable
watershed scenario. If computation reports a missing `fim_inputs.csv`, check
the requested HUC and its downloaded watershed data; this is a computation
failure before the S3 upload.

```json
{
  "job_id": "<uuid>",
  "status": "queued",
  "status_url": "/jobs/<uuid>"
}
```

`GET /jobs` lists the authenticated user's tracked job IDs. Poll
`GET /jobs/<uuid>` for `queued`, `started`, `success`, `failure`, or
`revoked`. Only the submitting user can poll, cancel, or obtain output URLs
(HTTP 403 otherwise). On success, `result` extends the shared `HandFimResult`
schema with the persisted output destination:

```json
{
  "output_bucket": "fim-output",
  "output_prefix": "data/output",
  "outputs": [
    {
      "ScenarioID": "single-reach-scenario",
      "geotiff_url": "/jobs/<uuid>/outputs/0.tif"
    }
  ]
}
```

The worker recursively uploads **all files in this job's output directory**
(`/home/output/scenarios/<job_id>/` in the container), preserving filenames
and subdirectories:
`s3://<output_bucket>/<output_prefix>/<job_id>/<relative-file-path>`.
This includes `.tif`, `.cog`, and any other generated files, without renaming
them. Shared watershed caches and previous jobs elsewhere in `/home/output`
are excluded. Redelivered jobs clear their local output files before computing.
The existing scenario command still must produce a nonempty COG per scenario
before the job can succeed. Original scenario IDs remain metadata.
Failed computation, missing output, or an upload failure fails the job.

Successful results include a `files` manifest alongside `outputs`, with each
file's relative `path`, S3 `key`, `size_bytes`, and `download_url`
(`/jobs/<job_id>/files/<index>`). Request that URL with your session token
for a presigned URL to any uploaded file. Scenario `geotiff_url` endpoints
continue to work and now sign the preserved `.cog` object key.
Existing successful results without a manifest retain their legacy
`<index>.tif` download behavior.

Request the relative `geotiff_url` with your session token to obtain a fresh
presigned GET URL as JSON (the endpoint no longer streams a local file):

```json
{"url": "https://<storage-endpoint>/<signed-object-url>", "expires_in": 3600}
```

Follow `url` to download the COG without sending your FIMBox session token to
the storage service. Treat signed URLs as temporary credentials and do not
share them. `S3_PRESIGNED_URL_SECONDS` controls validity (default 3600,
maximum 604800 seconds); the actual returned lifetime is capped by the
remaining HydroShare S3 credential lifetime.
Deleted objects return HTTP 404 and signing/storage failures return HTTP 503.
The API no longer mounts the worker's local output volume.

Credentials expire in Redis at the earlier of their provider expiry and
`RESULT_TTL_SECONDS` after submission. Choose a credential lifetime covering
queue wait, computation, and the desired download window. A missing/expired
credential fails worker uploads and returns HTTP 503 for new signed URLs;
there is no automatic refresh. Logout revokes the FIMBox session but does not
revoke credentials for already-submitted jobs or existing signed URLs.
The remote service account expires at its requested lifetime; deleting a
local Redis key on enqueue failure does not delete the remote account.

`DELETE /jobs/<uuid>` requests cancellation of a queued job. Cancellation is
best-effort: a job can start while the request is being processed. Running jobs
cannot be interrupted by this single-process Celery worker and return HTTP 409.

### Configuration and operational limits

- `FIMBOX_API_PORT` controls the Compose host port (default `8000`).
- `HAND_FIM_MAX_PROCS` controls parallel FIM computations inside the worker
  (default `1`, must be positive).
- `CELERY_BROKER_URL`, `CELERY_RESULT_BACKEND`, and
  `CELERY_TASK_DEFAULT_QUEUE` configure the queue for non-Compose deployments.
- `RESULT_TTL_SECONDS` and `VISIBILITY_TIMEOUT_SECONDS` default to 86400
  seconds. Set visibility timeout longer than the longest job to prevent Redis
  from redelivering active work. These settings must agree on API and worker.
- The input/output named volumes retain watershed data and generated rasters.
  Removing volumes deletes these datasets. Result metadata expires after its
  TTL; URL issuance requires unexpired successful result metadata and ownership.
  Uploaded objects have independent storage retention.
- Ownership and per-user job tracking expire `RESULT_TTL_SECONDS` after
  submission. Configure this longer than the maximum queue wait and job
  duration plus your desired download window. Unknown, expired, and legacy
  unowned job IDs return HTTP 403. There is no durable job registry or
  automatic object cleanup.
- Use one worker for these shared volumes: the existing FIM tooling shares
  watershed download/cache directories. Do not scale workers against the same
  volumes without adding cache coordination.
- This local stack binds only to loopback. Before exposing it as a production
  service, configure HTTPS, registered OAuth callbacks, secure state cookies,
  protected/durable Redis, appropriate HydroShare resource permissions, and retention
  policies. Existing jobs submitted before authentication/storage integration
  have no ownership/destination metadata and are not migrated.

Stop the stack without deleting datasets:

```bash
docker compose -f docker/api/docker-compose.yml down
```

For host-side API development, `pixi install` followed by `pixi run api`
starts FastAPI only; a reachable Redis and a separately running container
worker are still required.

### API tests

The focused API/worker tests use standard-library `unittest`, with FastAPI's
test client. They mock the expensive FIM subprocess and OAuth provider, cover
session expiry/state/ownership, and verify real SDK signing without network
access:

```bash
python -m pip install -r docker/api/requirements-test.txt
PYTHONPATH=src python -m unittest discover -s tests -v
```

Container scenario regression tests run inside the worker image and mock the
scientific computation while checking error propagation and missing-HUC
handling:

```bash
docker compose -f docker/api/docker-compose.yml run --rm --no-deps \
  -v "$PWD/tests:/tests:ro" --entrypoint /opt/conda/bin/python \
  worker /tests/test_container_scenario.py
```

## Running the FIM Docker Container

```
pixi install
```

```
pixi run python src/fimbox.py
```

To skip the mode-selection menu, pass `--mode` (or `-m`):

```
pixi run python src/fimbox.py --mode handfim
```

Supported values are `handfim`, `derived-hand`, `hand-ml`, and
`fim-evaluation`. Currently, only `handfim` is implemented; the other modes
display a "coming soon" message. Selecting `handfim` skips only the mode menu:
the CLI still prompts you to choose how to provide HAND-FIM inputs and collect
the required parameters.

Without `--mode`, the CLI walks you through selecting a mode, then lets you
choose between two ways of providing HAND-FIM input:

1. **Manual entry** — interactively type in a single HUC-8 code, one or more
   NWM reach IDs, and matching flow rates (cms).
2. **Scenario JSON file** — point the CLI at a JSON file describing one or
   more scenarios (see [`schema/schema.md`](schema/schema.md) and
   [`schema/schema.json`](schema/schema.json) for the full format). Each
   scenario is validated against the schema, then produces a single combined
   output GeoTIFF, even if it spans multiple reaches or multiple HUCs.

In both cases, the tool prompts for an input directory (default: `./input/`)
and output directory (default: `./output/`) relative to your current working
directory. It mounts them into the container and streams FIM computation
progress to the terminal in real time. Final results are written under the
selected output directory:
`scenarios/<ScenarioID>.tif` (JSON mode) or `flood_<HUC>/...` (manual mode).

### Example: scenario JSON files

Sample scenario files are provided in [`test-data/`](test-data/). When
prompted for "Path to scenario JSON file", enter the path to one of these
(or your own file following the same schema):

#### `test-data/single_reach.json` — one scenario, one reach

A single scenario made up of one reach in one HUC. Produces one output
GeoTIFF for the scenario.

```json
[
  {
    "ScenarioID": "single-reach-scenario",
    "Reaches": [
      { "HUC": "03020201", "ReachID": "8780625", "Streamflow": 227 }
    ]
  }
]
```

`./output/scenarios/single-reach-scenario.tif`:

<img src="docs/images/single-reach-scenario.png" alt="single-reach-scenario output" width="250">

#### `test-data/single_huc_many_reaches.json` — one scenario, many reaches, one HUC

Multiple reaches within the same HUC are combined into a single flow input
file, producing one merged output GeoTIFF for the scenario.

```json
[
  {
    "ScenarioID": "single-huc-senario",
    "Reaches": [
      { "HUC": "03020201", "ReachID": "8780625", "Streamflow": 233 },
      { "HUC": "03020201", "ReachID": "8780659", "Streamflow": 224 },
      { "HUC": "03020201", "ReachID": "8780641", "Streamflow": 239 },
      { "HUC": "03020201", "ReachID": "8780645", "Streamflow": 223 }
    ]
  }
]
```

`./output/scenarios/single-huc-senario.tif`:

<img src="docs/images/single-huc-senario.png" alt="single-huc-senario output" width="250">

#### `test-data/many_hucs_many_reaches.json` — one scenario, many reaches, many HUCs

Reaches spanning multiple HUCs are computed per-HUC, then merged into a
single combined output GeoTIFF for the scenario.

```json
[
  {
    "ScenarioID": "multiple-huc-scenario",
    "Reaches": [
      { "HUC": "03020201", "ReachID": "8780625", "Streamflow": 223 },
      { "HUC": "03020201", "ReachID": "8780659", "Streamflow": 229 },
      { "HUC": "03020201", "ReachID": "8780641", "Streamflow": 231 },
      { "HUC": "03020201", "ReachID": "8780645", "Streamflow": 225 },
      { "HUC": "03030002", "ReachID": "8891104", "Streamflow": 227 },
      { "HUC": "03030002", "ReachID": "8891054", "Streamflow": 240 },
      { "HUC": "03030002", "ReachID": "8891166", "Streamflow": 227 },
      { "HUC": "03030002", "ReachID": "8891174", "Streamflow": 226 }
    ]
  }
]
```

`./output/scenarios/multiple-huc-scenario.tif`:

<img src="docs/images/multiple-huc-scenario.png" alt="multiple-huc-scenario output" width="250">

#### `test-data/multiple_scenarios.json` — many scenarios in one file

Each entry in the top-level list is its own independent scenario; every
scenario produces its own output GeoTIFF. This is useful for running a
batch of related scenarios (e.g. steps of a hydrograph) in one job.

```json
[
  {
    "ScenarioID": "scenario1",
    "Reaches": [
      { "HUC": "03020201", "ReachID": "8780625", "Streamflow": 223 },
      { "HUC": "03020201", "ReachID": "8780659", "Streamflow": 224 },
      { "HUC": "03020201", "ReachID": "8780641", "Streamflow": 228 },
      { "HUC": "03020201", "ReachID": "8780645", "Streamflow": 227 }
    ]
  },
  {
    "ScenarioID": "scenario2",
    "Reaches": [
      { "HUC": "03020201", "ReachID": "8780625", "Streamflow": 222 },
      { "HUC": "03020201", "ReachID": "8780659", "Streamflow": 224 },
      { "HUC": "03020201", "ReachID": "8780641", "Streamflow": 236 },
      { "HUC": "03020201", "ReachID": "8780645", "Streamflow": 235 }
    ]
  }
]
```

`./output/scenarios/scenario1.tif` and `./output/scenarios/scenario2.tif`:

<img src="docs/images/scenario1.png" alt="scenario1 output" width="250"> <img src="docs/images/scenario2.png" alt="scenario2 output" width="250">
