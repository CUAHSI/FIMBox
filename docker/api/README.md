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
`output_bucket`, `output_prefix`, `overwrite`, and `max_procs` fields.
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

`GET /jobs` lists tracked jobs. Use `GET /jobs/{job_id}` to poll and
`GET /jobs/{job_id}/logs?offset=0` to read worker logs by byte offset.
`DELETE /jobs/{job_id}` cancels a queued job on a best-effort basis; running
jobs cannot be interrupted. Job metadata and ownership expire according to
`RESULT_TTL_SECONDS` (default 86400 seconds).

## Configuration notes

- `FIMBOX_API_PORT` changes the host port (default `8000`).
- `FIMBOX_INPUT_DIR` and `FIMBOX_OUTPUT_DIR` set host bind-mount directories.
- `HAND_FIM_MAX_PROCS` sets default computation concurrency (default `1`);
  a job can override it with `max_procs`.
- `RESULT_TTL_SECONDS` and `VISIBILITY_TIMEOUT_SECONDS` should exceed the
  longest queue wait and job duration. Configure them consistently for API
  and worker.
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
