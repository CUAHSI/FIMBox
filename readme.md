# FIMBox

FIMBox runs HAND-based Flood Inundation Mapping (HAND-FIM) jobs through a local
API, a Celery worker, and a `fimbox` command-line client. Job outputs are
temporary in local mode; HydroShare S3 output is optional.

## Get started locally

Requirements: Docker Compose and Pixi. From the repository root:

```bash
pixi install
docker compose up --build
```

This starts the API at [http://localhost:8000](http://localhost:8000), a worker, and Redis. The API is
bound to loopback and uses a shared anonymous local identity by default; use
only on a trusted machine. HAND-FIM input data and generated files use the
`data/input/` and `data/output/` mounts. Local output is removed when each job
finishes.

`pixi install` generates the local SDK and installs the `fimbox` CLI.
In another terminal, submit a sample job:

```bash
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
