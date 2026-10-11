# AGENTS.md

This repository maintains supplemental context for AI coding agents in the
`.ai/` directory. Before answering questions about this codebase or making
changes, consult `.ai/` for existing notes on project structure and file
purposes.

- `.ai/CONTEXT.md` — Summary of the purpose of each Python file in this
  repository (host CLI, container entry point, schema, patched fimserve code,
  rating-curve utilities), plus known gaps/issues.

Keep `.ai/CONTEXT.md` (and any other files added to `.ai/`) up to date when
making structural changes to the project, so future sessions can rely on it.

## Repo layout

- `src/fimbox_cli.py` — Installable SDK-backed Click CLI. Run with `fimbox`
  after `python -m pip install .`, or `pixi run fimbox`.
- `src/fimbox_sdk/` — Generated sync/async Python client and attrs models.
  Do not hand-edit; regenerate with `pixi run sdk-generate`.
- `src/fimbox_api/` — FastAPI service, HydroShare sessions, Celery worker,
  local/S3 output handling, and Pydantic job schemas.
- `src/schemas/hand_fim_schema.py` — Shared scenario models used by API/worker.
- `sdk/` — Captured OpenAPI document and pinned local generator configuration.
- `docker/handfim/` — Code that runs *inside* the `cuahsi/handfim` Docker
  image: `entry.py` (container CLI), `compute_rating_increments.py` (rating
  curve interpolation), and `patches/` (files that intentionally override
  upstream `fimserve` modules — see "External dependencies" below).
- `test-data/` — Sample input data for manual testing.
- `docker/input/`, `docker/output/` — Default runtime bind mounts for the API
  Compose stack; configurable through FIMBOX_INPUT_DIR/FIMBOX_OUTPUT_DIR.
  Output files live under scenarios/<job_id>/ and are shared read-only with API.
- `pixi.toml` / `pixi.lock` / `.pixi/` — Pixi-managed Python environment
  (Python 3.14). Use `pixi install` to set up, `pixi run fimbox` to execute.
  Don't hand-edit `pixi.lock`.
- `fimbox_map.qgz` — Binary QGIS project file; not something to open/edit as
  text.

## Build / run / test commands

- Build the base Docker image: `docker build --platform linux/amd64 -t cuahsi/handfim:latest docker/handfim`.
- Run the service from the repository root: `docker compose --env-file /dev/null up --build`.
- Run the CLI: `fimbox submit --body test-data/single_reach.json`.
  Optional `--max-procs N` overrides worker concurrency (default 1);
  optional `--output-bucket` enables S3, otherwise output stays on the mount.
- Tests: `PYTHONPATH=src python -m unittest discover -s tests -v`.
  Tests cover API/worker/SDK/CLI/auth; scientific computation is mocked.
  Container-only regressions require the HAND-FIM image.
  No repository-wide linter/formatter is configured.

## External dependencies & patches

- The container wraps NOAA/OWP's `inundation-mapping` FIM tooling and a
  vendored `fimserve` package.
- `docker/handfim/patches/` intentionally overrides parts of upstream
  `fimserve` (e.g. `fimserve__init__.py` disables most of the original
  package's exports; `runFIM.py` adds a `label` parameter to avoid mosaic
  output collisions during parallel runs). Treat these as deliberate
  divergences, not bugs to "fix" back to upstream behavior.

## Conventions

- Follow Unix philosophy: keep code simple, and make each function, module,
  and command do one thing well. Prefer small, composable units over
  multi-purpose ones. Avoid scope creep — when implementing a requested
  change, don't bundle in unrelated refactors, features, or "while I'm here"
  fixes; raise them separately instead.
- Container CLI commands are built with Typer; parameters typically use
  `Annotated[type, typer.Argument(..., help=...)] = default` for container-side
  commands (`entry.py`). The installable host CLI uses Click and the generated
  SDK; authentication tokens are entered with a hidden prompt or environment.
- Module-private helper functions are prefixed with a double underscore
  (e.g. `__write_flow_input_file`) even though they're module-level, not
  class methods — follow this pattern for new internal helpers.
- Docstrings favor a plain "Arguments:/Returns:" style rather than
  Google/NumPy docstring formats (except in `compute_rating_increments.py`,
  which uses `====` underlines).

## Validation notes

- Update sdk/openapi.json and regenerate the SDK when the API schema changes.
- Preserve explicit HTTP/transport errors and user ownership checks.
- Use one worker per shared watershed cache.
