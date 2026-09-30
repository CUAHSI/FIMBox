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

- `src/` — Host-side Typer CLI (`fimbox.py` entry point, `hand_fim.py` mode
  logic). Run with `python src/fimbox.py`.
- `schema/` — Pydantic models describing the HAND-FIM scenario request/response
  contract (`hand_fim_schema.py`). Not yet wired into any running code.
- `docker/handfim/` — Code that runs *inside* the `cuahsi/handfim` Docker
  image: `entry.py` (container CLI), `compute_rating_increments.py` (rating
  curve interpolation), and `patches/` (files that intentionally override
  upstream `fimserve` modules — see "External dependencies" below).
- `test-data/` — Sample input data for manual testing.
- `input/`, `output/` — Runtime mount directories created by `hand_fim.py`
  when running the container; contents are gitignored and not source.
- `pixi.toml` / `pixi.lock` / `.pixi/` — Pixi-managed Python environment
  (Python 3.14, `rich`, `typer`). Use `pixi install` to set up, `pixi run
  python src/fimbox.py` to execute. Don't hand-edit `pixi.lock`.
- `fimbox_map.qgz` — Binary QGIS project file; not something to open/edit as
  text.

## Build / run / test commands

- Build the Docker image: `cd docker && docker-compose build`.
- Run the CLI: `python src/fimbox.py` (or `pixi run python src/fimbox.py`).
- **There is currently no automated test suite and no linter/formatter
  configured in this repo.** Don't assume `pytest`, `ruff`, `black`, etc. are
  set up — check before suggesting a specific command, and validate changes by
  running the CLI manually (and noting in your response that no automated
  tests exist).

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
- CLI commands are built with Typer; parameters typically use
  `Annotated[type, typer.Argument(..., help=...)] = default` for container-side
  commands (`entry.py`) and `typer.prompt(...)` for interactive host-side
  prompts (`hand_fim.py`).
- Console output uses `rich` (`Console`, `Panel`) for host-side UX.
- Module-private helper functions are prefixed with a double underscore
  (e.g. `__write_flow_input_file`) even though they're module-level, not
  class methods — follow this pattern for new internal helpers.
- Docstrings favor a plain "Arguments:/Returns:" style rather than
  Google/NumPy docstring formats (except in `compute_rating_increments.py`,
  which uses `====` underlines).

## Known issues / TODOs

- `src/hand_fim.py`'s `run()` function contains a leftover
  `import pdb; pdb.set_trace()` breakpoint — flag this if touching that file,
  since it looks unintentional.
- `schema/hand_fim_schema.py` has no current callers; likely scaffolding for a
  future API/service layer.
