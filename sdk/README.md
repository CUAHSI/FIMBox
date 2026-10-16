# Python SDK and `fimbox` CLI

The installable `fimbox` package combines a locally generated Python SDK with
a handwritten Click CLI. The generated `src/fimbox_sdk/` package is
gitignored and generated from [`openapi.json`](openapi.json) automatically
during installation:

```bash
pixi install
pixi run fimbox --help
```

Without Pixi, use `python -m pip install .` (or `python -m pip install -e .`
for editable development), then `fimbox --help`. Both installation methods
run the pinned generator in an isolated build environment; no separate codegen
installation or generation command is needed. Installation needs network
access unless build dependencies are already cached.

The CLI works from
any directory and defaults to `http://localhost:8000`. Set
`FIMBOX_API_URL` or pass `--api-url URL` before the command to select another
API. The CLI does not start Docker; start the API stack separately.

## CLI quick reference

```bash
fimbox submit --body test-data/single_reach.json
fimbox jobs
fimbox status --job-id JOB_ID
fimbox cancel --job-id JOB_ID
fimbox session
fimbox logout
fimbox health
```

Omitting `--body` prompts for manual reach inputs or a scenario JSON file.
`--max-procs N` sets positive per-job concurrency; without it, JSON submissions
use the worker's `HAND_FIM_MAX_PROCS` setting. `--output-bucket` and
`--output-prefix` select retained S3 output. Local output is temporary.
Interactive submissions read the API's `/auth/config` requirements and prompt
for a missing output bucket when HydroShare output is required. When a bucket
is selected, they also prompt for the output prefix (default empty) and
whether to overwrite existing output objects (default no). Explicit
`--output-bucket`, `--output-prefix`, `--overwrite`/`--no-overwrite`, and
`--max-procs` flags skip the corresponding prompts.

Submissions using `--body` remain noninteractive for job options; pass
`--output-bucket` explicitly when the deployment requires it:

```bash
fimbox submit --body test-data/single_reach.json \
  --output-bucket YOUR_HYDROSHARE_BUCKET --output-prefix fimbox
```

When the API requires authentication, the CLI checks `FIMBOX_SESSION_TOKEN`,
then the OS credential store scoped to the API URL, then starts browser login.
Open the displayed link and sign in with HydroShare; the CLI polls for approval
for up to 10 minutes and retrieves the FIMBox session token automatically.
No copy/paste is needed. Browser login requires HTTPS except for loopback
development URLs. After the command succeeds, the token is saved securely in
the OS credential store. Explicit `--session-token` or `FIMBOX_SESSION_TOKEN`
values remain available for noninteractive use and are not saved.
`fimbox logout` revokes the session and removes the saved token.

## Use the SDK

Generate SDK operation modules from the committed OpenAPI document. The
generated client provides typed models and synchronous and asynchronous
operations. For example:

```python
from fimbox_sdk import Client
from fimbox_sdk.api.default import list_jobs_jobs_get

with Client(base_url="http://localhost:8000") as client:
    response = list_jobs_jobs_get.sync_detailed(client=client)
    if response.status_code != 200:
        raise RuntimeError(f"Job list failed: HTTP {response.status_code}")
```

Use `AuthenticatedClient` with a FIMBox session token for authenticated
operations. Detailed operations expose the HTTP status; generated models do
not replace server-side validation. No requests are automatically retried.

For browser-assisted login in scripts or notebooks, see
[`fimbox_login.begin_login`](../src/fimbox_login.py) and the
[cloud notebook guide](../notebooks/README.md). Keep the returned session
token private.

## Notebook raster maps

Install the optional `fimbox[map]` dependency to use `fimbox_map.job_map` with
an existing connected SDK client, successful job ID, and typed output result.
See [the notebook guide](../notebooks/README.md). The helper fetches raster
metadata and visible PNG tiles using generated operations; session tokens stay
in the kernel. The API must include the matching raster endpoints.

## Regenerate the SDK

To update the captured API contract, run the service and capture OpenAPI, then
run the pinned local generator:

```bash
curl --fail http://localhost:8000/openapi.json -o sdk/openapi.json
pixi run sdk-generate
```

The generator configuration maps `image/png` to its binary response parser
because generator 0.28 does not recognize PNG content types directly. The
actual API response remains `image/png`.

Generation does not use a hosted service. Commit the OpenAPI document and
generator configuration, not the generated SDK files. If API operation IDs
or signatures change, update affected CLI and SDK usage.
