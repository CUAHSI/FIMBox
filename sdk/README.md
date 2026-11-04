
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

Execution modes are `fimserv-2024` (default), `fimserv-2026`, `streaming-2024`,
and `streaming-2026`. The old names are rejected rather than accepted as aliases.
SDK enum members are `FIMSERV_2024`, `FIMSERV_2026`, `STREAMING_2024`, and
`STREAMING_2026`.

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
use the worker's `HAND_FIM_MAX_PROCS` setting. Output location is derived by
the server from the selected workspace. `--execution-mode streaming-2024`
selects the opt-in streaming computation.

`--execution-mode fimserv-2026` runs the normal FIMserv/COG workflow with
NOAA v4.8.14.0 instead of the original pinned NOAA code. In SDK submissions,
select `HandFimJobRequestExecutionMode.FIMSERV_2026`. Rebuild the worker
first. See [updated legacy mode](../docker/handfim/README.md#updated-legacy-mode).

`--execution-mode streaming-2026` (`HandFimJobRequestExecutionMode.STREAMING_2026`)
streams with NOAA v4.8.14.0's own per-pixel rule, matching `fimserv-2026`; it
has the same requirements as streaming. See
[updated streaming mode](../docker/handfim/README.md#updated-streaming-mode).

Submissions using `--body` remain noninteractive for job options:

```bash
fimbox submit --body test-data/single_reach.json --max-procs 4
```

All protected commands require HydroShare authentication; job and output
operations also require an active workspace. Anonymous local submission is
not supported. The CLI checks `FIMBOX_SESSION_TOKEN`,
then the OS credential store scoped to the API URL, then starts browser login.
Open the displayed link and sign in with HydroShare; the CLI polls for approval
for up to 10 minutes and retrieves the FIMBox session token automatically.
No copy/paste is needed. Browser login requires HTTPS except for loopback
development URLs. After the command succeeds, the token is saved securely in
the OS credential store. Explicit `--session-token` or `FIMBOX_SESSION_TOKEN`
values remain available for noninteractive use and are not saved.
`fimbox logout` revokes the session and removes the saved token.
Workspace job commands refuse APIs whose `/auth/config` does not explicitly
advertise workspace enforcement; upgrade the API before using the updated client.

## Use the SDK

### Workspaces

```bash
fimbox workspace create s3://resource/RESOURCE_ID/data/contents/workspaces/study-a --name "Flood study"
fimbox workspace open s3://resource/RESOURCE_ID/data/contents/workspaces/study-a
fimbox workspace show
fimbox submit --body test-data/single_reach.json
fimbox jobs
fimbox status --job-id JOB_ID
fimbox workspace job JOB_ID
fimbox workspace forget
```

Create a workspace in an existing HydroShare resource under
`s3://resource/<resource-id>/data/contents/`, using an empty subprefix. Opening
an existing workspace validates it without creating or changing it. The CLI
saves its selected location in `workspace.json`, scoped to the API URL; it
never reads or migrates the former `history.json`. Use `--workspace LOCATION`
or `FIMBOX_WORKSPACE` to override the saved selection. `workspace forget`
forgets only the local preference; it does not delete workspace data or outputs.

Workspaces require OAuth and use each signed-in user's existing HydroShare/S3
permissions. FIMBox does not create memberships, invitations, or storage grants:
share the resource through HydroShare, and collaborators can open the same
workspace if their storage permissions allow it. Job records and outputs are
workspace-scoped; the API chooses their destination. Only a job's submitter can
cancel it. The CLI does not import history, share/delete jobs, or delete
workspace data. `workspace job JOB_ID` displays a live or retained workspace
job record.

SDK callers pass the location as `X-FIMBox-Workspace` on job operations:

```python
client = authenticated_client.with_headers({
    "X-FIMBox-Workspace": "s3://resource/RESOURCE_ID/data/contents/workspaces/study-a",
})
```

Create and open workspaces with the generated
`create_workspace_workspaces_post` and
`load_workspace_workspaces_open_post` operations and the `WorkspaceCreate`,
`WorkspaceOpen`, and `Workspace` models. Job request bodies contain scenarios
and optional `max_procs` / `execution_mode`; do not provide an output bucket,
prefix, or overwrite setting.

### Generated operations

Generate SDK operation modules from the committed OpenAPI document. The
generated client provides typed models and synchronous and asynchronous
operations. For example:

```python
import os
from fimbox_sdk import AuthenticatedClient
from fimbox_sdk.api.default import list_jobs_jobs_get

with AuthenticatedClient(
    base_url="http://localhost:8000",
    token=os.environ["FIMBOX_SESSION_TOKEN"],
    headers={"X-FIMBox-Workspace": os.environ["FIMBOX_WORKSPACE"]},
) as client:
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

Install the optional `fimbox[map]` dependency to use `fimbox_map.output_map` with
an existing connected SDK client and a list of `s3://bucket/key` paths to
scenario `manifest.json` files or COG/TIFF outputs. No job ID or result is needed,
so retained outputs remain viewable after the 24-hour Redis metadata expiry.
`job_map(client, job_id, result)` remains a compatibility wrapper that extracts
paths from a saved result and does not look up the job.
See [the notebook guide](../notebooks/README.md). The helper fetches raster
metadata and visible PNG tiles using generated operations; session tokens stay
in the kernel. The API must include the matching `/outputs` endpoints, which
authorize reads within the active workspace using the current session's
HydroShare S3 permissions. Configure `X-FIMBox-Workspace` on this client too.
The **Auto-zoom** checkbox is enabled by default. Disable it to preserve the
current center and zoom when selecting outputs or reloading tiles. Streaming
tiles still require the API's minimum display zoom; below it, the map shows
a zoom-in message instead of requesting tiles. Re-enabling auto-zoom fits the
selected output again.

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
