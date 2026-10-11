## Building Docker Images

```
docker build --platform linux/amd64 -t cuahsi/handfim:latest docker/handfim
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
docker compose --env-file /dev/null up --build
```

The main Compose file runs API, worker, and Redis. The base HAND-FIM image
can be built separately as shown above. The Compose project name remains `api` to preserve
existing containers and sessions; no data migration is needed.

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
docker compose --env-file /dev/null up -d --build api
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
submission with an S3 bucket, the API uses the HydroShare access token to
request temporary S3 credentials. Local-output jobs skip that exchange.

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

### Output storage: local by default, optional HydroShare S3

Without `output_bucket`, files remain in the worker's Compose-mounted
`/home/output/scenarios/<job_id>/` directory. The result includes
`local_output_dir` and a file manifest with `key: null`. No S3 account,
credentials, network access, or upload is required; session authentication
and job ownership still apply.

Compose uses `FIMBOX_OUTPUT_DIR` (default `./docker/output`, resolved relative to
the root `docker-compose.yml`) as the host bind mount.
The API mounts the same directory read-only for authenticated downloads.
Set an absolute `FIMBOX_OUTPUT_DIR` to select a different host directory.
No output path from a job request is used to select an arbitrary filesystem
location. Job files stay isolated beneath `scenarios/<job_id>/`.

```bash
curl --fail -X POST http://localhost:8000/jobs/hand-fim \
  -H "Authorization: Bearer $FIMBOX_SESSION_TOKEN" \
  -H 'Content-Type: application/json' \
  --data-binary @test-data/single_reach.json
```

To additionally upload output to S3, supply `output_bucket` and optionally
`output_prefix` (defaults to the bucket root). A prefix without a bucket
is rejected. The default **S3 endpoint** is `https://s3.hydroshare.org`.
`S3_ENDPOINT_URL` may override this for a compatible HydroShare deployment;
also configure its credential API URL. `S3_REGION` defaults to `us-east-1`
and `S3_ADDRESSING_STYLE` to `path`.

For each S3-output job only, the API makes an authenticated request:

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

For S3 output, the caller supplies `output_bucket` and optionally `output_prefix`. The bucket
and destination HydroShare resource must already exist. Supply the resource's
S3 bucket and writable prefix, not an arbitrary new bucket. The submitting
HydroShare user must have write access there; `HeadObject`/`GetObject` are
also needed for URL issuance/downloads. Output paths append
`<job_id>/<relative-file-path>` to your prefix. Job ownership checks do not bypass
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

`GET /jobs` (and `fimbox jobs`) lists the authenticated user's tracked job IDs
and current statuses:

```json
{"jobs": [{"job_id": "<uuid>", "status": "started"}]}
```

The `jobs` array now contains objects instead of plain ID strings. Status
names match the individual job endpoint; the list excludes results and errors.
Poll
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

For S3 jobs, the worker recursively uploads **all files in this job's output directory**
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
for a presigned URL to an uploaded file, or a local authenticated download URL.
Scenario `geotiff_url` endpoints also work in both modes.
Existing successful results without a manifest retain their legacy
`<index>.tif` download behavior.

For local jobs, file/scenario endpoints return:

```json
{"url": "/jobs/<uuid>/files/0/content", "expires_in": 0}
```

`expires_in: 0` means this is not a presigned URL. Resolve the URL against
the API origin and send your FIMBox session token to download it.
`GET /jobs/<uuid>/files/<index>/content` streams the file, checks ownership,
and rejects traversal/symlinks. Missing local files return 404.
Local downloads require the shared output mount and unexpired job metadata.
The SDK provides `get_local_job_file_jobs_job_id_files_index_content_get`
with a generated binary `File` response (buffered in memory).

For S3 jobs, request the relative `geotiff_url` with your session token to
obtain a fresh presigned GET URL as JSON:

```json
{"url": "https://<storage-endpoint>/<signed-object-url>", "expires_in": 3600}
```

Follow `url` to download the COG without sending your FIMBox session token to
the storage service. Treat signed URLs as temporary credentials and do not
share them. `S3_PRESIGNED_URL_SECONDS` controls validity (default 3600,
maximum 604800 seconds); the actual returned lifetime is capped by the
remaining HydroShare S3 credential lifetime.
Deleted objects return HTTP 404 and signing/storage failures return HTTP 503.
The API shares the worker's local output bind mount read-only.

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
  (default `1`, must be positive). A job's `max_procs` overrides it: supply
  `?max_procs=N` on `POST /jobs/hand-fim` or `"max_procs": N` in the
  `POST /jobs` envelope. Values must be integers greater than zero; omission
  uses the worker setting.
- `CELERY_BROKER_URL`, `CELERY_RESULT_BACKEND`, and
  `CELERY_TASK_DEFAULT_QUEUE` configure the queue for non-Compose deployments.
- `RESULT_TTL_SECONDS` and `VISIBILITY_TIMEOUT_SECONDS` default to 86400
  seconds. Set visibility timeout longer than the longest job to prevent Redis
  from redelivering active work. These settings must agree on API and worker.
- The input/output bind mounts retain watershed data and generated rasters.
  Deleting these host directories deletes datasets. Result metadata expires after its
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
docker compose --env-file /dev/null down
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
docker compose --env-file /dev/null run --rm --no-deps \
  -v "$PWD/tests:/tests:ro" --entrypoint /opt/conda/bin/python \
  worker /tests/test_container_scenario.py
```

## Installable Python SDK and CLI

The `fimbox` distribution includes the generated [`fimbox_sdk`](src/fimbox_sdk)
Python package and an SDK-backed `fimbox` console command. It is generated
locally with [openapi-python-client](https://github.com/openapi-generators/openapi-python-client)
0.28.0 from [`sdk/openapi.json`](sdk/openapi.json). Generation does not send the
API spec to a hosted service. The SDK provides typed models and synchronous and
asynchronous methods for every API endpoint, including authentication.
Generated files should not be hand-edited.

Install from the repository root (Python 3.10+):

```bash
python -m pip install .
fimbox --help
```

For editable development, use `python -m pip install -e .`. This installs only
client dependencies, not FastAPI, Celery, Redis, or Docker. No generator is
required at runtime. The CLI works from any directory and help works offline.
The package has not been published to PyPI; `pip install fimbox` is not the
installation command for this repository.

To build an installable wheel without publishing:

```bash
python -m pip wheel . --no-deps --wheel-dir dist
```

### Using the SDK

Get your [FIMBox session token](http://localhost:8000/auth/login) in a browser.
After signing in with HydroShare, copy the `access_token` from the callback's
JSON response. This is the **FIMBox session token**, not the HydroShare access
token.

```python
import os

import httpx
from fimbox_sdk import AuthenticatedClient
from fimbox_sdk.api.default import submit_hand_fim_jobs_hand_fim_post
from fimbox_sdk.models import JobSubmitted, Reach, Scenario

with AuthenticatedClient(
    base_url="http://localhost:8000",
    token=os.environ["FIMBOX_SESSION_TOKEN"],
    timeout=httpx.Timeout(30),
    raise_on_unexpected_status=True,
) as client:
    response = submit_hand_fim_jobs_hand_fim_post.sync_detailed(
        client=client,
        body=[Scenario(
            scenario_id="single-reach",
            reaches=[Reach(huc="03020201", reach_id="8780625", streamflow=7)],
        )],
        output_bucket="YOUR_HYDROSHARE_BUCKET",
        output_prefix="data/output",
    )
    if response.status_code != 202 or not isinstance(response.parsed, JobSubmitted):
        raise RuntimeError(f"Submission failed: HTTP {response.status_code}")
    print(response.parsed.job_id)
```

Generated operation modules use names derived from FastAPI `operationId`.
For example, use `get_job_status_jobs_job_id_get` to poll, and
`get_job_file_jobs_job_id_files_index_get` for file URLs. Each module provides
`sync`, `sync_detailed`, `asyncio`, and `asyncio_detailed`. Use `async with
AuthenticatedClient(...)` for async calls. Public endpoints accept `Client`.
Models provide `from_dict` and `to_dict`; they are generated attrs classes,
not Pydantic validators. The server remains the authority for input validation.

The generated client's default `raise_on_unexpected_status` is `False`:
undocumented error statuses can have `parsed=None`. Always inspect status
with `*_detailed`, or set `raise_on_unexpected_status=True` and handle
`fimbox_sdk.errors.UnexpectedStatus` (it contains response bytes; do not
log sensitive authentication responses). HTTP transport failures raise
`httpx.RequestError`. No requests are automatically retried.

### Submitting jobs from the CLI

Start the API/Redis/worker stack separately as described above. The CLI does
not launch Docker;
computation runs in the API's worker. The default API URL is
`http://localhost:8000`. Set `FIMBOX_API_URL` or place `--api-url URL` **before
the command** to use another service. Use HTTPS outside local development.

Authenticated commands use an explicit `--session-token` or
`FIMBOX_SESSION_TOKEN` first, then a session saved in the OS credential store.
If none is available, a hidden prompt includes the browser login link based on
the configured API URL (`--api-url` or `FIMBOX_API_URL`). After the prompted
token succeeds, it is saved automatically and reused on subsequent CLI runs.
Tokens are scoped to the API URL, including any base path; credentials for one
server are never reused for another. No OAuth client secret is needed locally.

Storage uses macOS Keychain, Windows Credential Locker, or Linux Secret
Service/KWallet through `keyring`, with no plaintext file fallback. Your OS may
ask permission to access the credential store. If no supported store is
available, the CLI reports an error; supplying an environment token bypasses
stored-token lookup. Environment/option overrides are not automatically saved.
`fimbox logout` revokes the session and clears the saved token for that API.
A saved token rejected with HTTP 401 is cleared; the request is not retried
automatically. Log in again when a session expires.

For temporary environment-only use, enter the token without shell history:

```bash
read -r -s FIMBOX_SESSION_TOKEN
export FIMBOX_SESSION_TOKEN
```

Submit a scenario JSON array:

```bash
fimbox submit --body test-data/single_reach.json
```

To set per-job concurrency:

```bash
fimbox submit --body test-data/single_reach.json --max-procs 4
```

`--max-procs` must be a positive integer. If omitted, the worker uses
`HAND_FIM_MAX_PROCS` (default 1). The generated SDK submission functions accept
`max_procs` too; concurrency applies inside one job, not to Celery worker count.

This defaults to the Compose-mounted output directory. To opt into S3:

```bash
fimbox submit \
  --body test-data/single_reach.json \
  --output-bucket YOUR_HYDROSHARE_BUCKET \
  --output-prefix data/output
```

`fimbox submit-envelope --body request.json` accepts the `payload`,
optional `output_bucket`, `output_prefix`, and `max_procs` envelope instead.
For example: `{"payload": [...], "max_procs": 4}`. The CLI constructs generated
SDK models, rejects missing or unknown fields rather than silently dropping
them, and sends them through the generated endpoint. Pydantic validation
remains server-side. Submission prints JSON containing `job_id` and does not
wait for computation.

```bash
fimbox jobs
fimbox status --job-id JOB_UUID
fimbox output --job-id JOB_UUID --index 0
fimbox scenario-output --job-id JOB_UUID --index 0
fimbox cancel --job-id JOB_UUID
fimbox session
fimbox logout
fimbox health
```

`output` uses the file manifest; `scenario-output` uses the scenario COG link.
Both print a local authenticated content URL (`expires_in: 0`) or an S3
presigned URL, without downloading. Send the session token only to the API
for local downloads, never to S3. Errors exit nonzero. Redirects are not followed, and submissions
are not retried. `--timeout` controls HTTP timeout (default 30 seconds).
Browser login is still performed in the browser; authentication endpoints
are available in the SDK, not separate CLI login commands.

`pixi install` installs the same editable distribution, and
`pixi run fimbox ...` runs the installed console command.

### Regenerating the SDK

Capture the current service spec, then run the pinned local generator:

```bash
curl --fail http://localhost:8000/openapi.json -o sdk/openapi.json
pixi run sdk-generate
```

This task requires `uv` and overwrites **only** `src/fimbox_sdk`, not the
handwritten CLI. To generate without Pixi/uv:

```bash
python -m pip install '.[codegen]'
openapi-python-client generate \
  --path sdk/openapi.json --config sdk/generator.yaml \
  --meta none --output-path src/fimbox_sdk --overwrite --fail-on-warning
```

Commit the spec and generated files together. Stable API operation IDs preserve
SDK imports; changes to operation signatures may require related CLI updates.
The regression suite checks that the captured spec matches the source API,
all endpoints are generated, SDK response models parse, and CLI submission
uses the generated client:

```bash
PYTHONPATH=src python -m unittest discover -s tests -p test_fimbox_cli.py -v
```

### Example: scenario JSON files

Sample scenario files are provided in [`test-data/`](test-data/).
Submit one with `fimbox submit --body PATH`. Each scenario produces a combined
GeoTIFF, even if it spans multiple reaches or HUCs. Files are stored beneath
`scenarios/<job_id>/` using internal `<job_id>-<index>` names; original scenario
IDs remain metadata. The job's file manifest lists the exact output paths.

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

Example result for this scenario:

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

Example result for this scenario:

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

Example result for this scenario:

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

Example results for these scenarios:

<img src="docs/images/scenario1.png" alt="scenario1 output" width="250"> <img src="docs/images/scenario2.png" alt="scenario2 output" width="250">
