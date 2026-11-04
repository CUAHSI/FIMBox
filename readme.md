# FIMBox

FIMBox runs HAND-based Flood Inundation Mapping (HAND-FIM) jobs through a local
API, a Celery worker, and a `fimbox` command-line client. HydroShare
authentication and a persistent S3 workspace are required.

## Get started locally

Requirements: Docker Compose and Pixi. From the repository root:

```bash
pixi install
docker compose up --build
```

Configure both HydroShare OAuth credentials and callback URLs as described in
the [API guide](docker/api/README.md#authentication). Anonymous/local submission
is no longer supported. Without OAuth configuration, protected operations
return a configuration error. The stack starts the API at
[http://localhost:8000](http://localhost:8000), a worker, and Redis. Worker
mounts are temporary computation storage, not the durable results store.

`pixi install` generates the local SDK and installs the `fimbox` CLI.
In another terminal, submit a sample job:

```bash
pixi run fimbox workspace create \
  s3://resource/RESOURCE_ID/data/contents/workspaces/study-a --name "Study A"
pixi run fimbox submit --body test-data/single_reach.json
pixi run fimbox jobs
pixi run fimbox status --job-id JOB_ID
```

The image build and initial watershed downloads need network access. Stop the
stack without deleting local data with `docker compose down`.

A browser page at [http://localhost:8000/ui](http://localhost:8000/ui) can also
submit jobs. It lets you edit the parameters, starting from the `test-data`
examples, and you can follow logs there. With HydroShare authentication enabled,
the page also browses and downloads the input dataset and retained results, and
maps the results. See the [API guide](docker/api/README.md#web-page).

Create a workspace at an empty subprefix under an existing HydroShare resource's
`RESOURCE_ID/data/contents/`, or open one with `fimbox workspace open LOCATION`.
Collaborators open the same location using their own HydroShare permissions.
All new job history, logs, and outputs stay inside the workspace. Write access
allows submission; read access allows inspection. Only a job's submitter can
cancel it. History persists beyond Redis expiry, subject to storage lifecycle
policies. Legacy personal archives are not imported, and shared deletion is
not provided. See [workspaces](docker/api/README.md#persistent-s3-workspaces).

### Use a different API domain

The CLI defaults to `http://localhost:8000`. To connect to an existing API
instead, run `pixi install` (no local Docker stack is needed) and pass
`--api-url` **before the command**, replacing the example domain:

```bash
pixi run fimbox --api-url https://api.example.org jobs
pixi run fimbox --api-url https://api.example.org submit --body test-data/single_reach.json
```

Alternatively, set the API URL for all CLI commands in your terminal:

```bash
export FIMBOX_API_URL=https://api.example.org
pixi run fimbox jobs
```

An explicit `--api-url` overrides `FIMBOX_API_URL`.

For an API that requires authentication, start by checking your session:

```bash
export FIMBOX_API_URL=https://api.example.org
pixi run fimbox session
```

If no token is configured or saved, the CLI displays a browser login link.
Open it and sign in with HydroShare. The CLI polls for approval for up to
10 minutes, retrieves the session token automatically, and saves it in your OS
credential store after the command succeeds. No token copying or pasting is
needed for subsequent commands:

```bash
pixi run fimbox jobs
pixi run fimbox logout
```

`logout` revokes the session and removes the saved token. Avoid putting tokens
directly in commands or shell history. For environment-based authentication,
see the [SDK and CLI guide](sdk/README.md).

## Guides

- [API, local services, authentication, output, and tests](docker/api/README.md)
- [HAND-FIM container image and scenario commands](docker/handfim/README.md)
- [Python SDK and `fimbox` CLI](sdk/README.md)
- [Google Cloud deployment with Terraform](terraform/README.md)
- [Cloud job notebook](notebooks/README.md)
- [Sample scenario files](test-data/README.md)
