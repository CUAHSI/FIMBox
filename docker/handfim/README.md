# HAND-FIM container

This directory builds `cuahsi/handfim`, the image that runs HAND-FIM
computation. The API worker image extends it. Build the base image from the
repository root:

```bash
docker build --platform linux/amd64 \
  -t cuahsi/handfim:latest docker/handfim
```

The image wraps NOAA/OWP's `inundation-mapping` tooling and vendored `fimserve`
code. The patches in `patches/` intentionally override upstream behavior;
in particular, the `runFIM.py` patch isolates parallel outputs and propagates
computation failures.

## Container commands

The entry point is `/home/entry.py`:

- `scenario` runs one or more scenarios from a JSON file and produces one
  combined GeoTIFF per scenario.
- `reachfim` computes inundation for reach and flow inputs.
- `clean` cleans previously generated FIM GeoTIFFs.

The API worker invokes `scenario` directly. For request structure and
submission, see the [API guide](../api/README.md); example inputs are in
[`test-data/`](../../test-data/README.md).

Outputs are written under `/home/output/`. The worker uploads the current
job's files to S3 when configured, then removes job inputs, scratch files,
watershed downloads, and local outputs. Only one worker should use a shared
watershed filesystem.
