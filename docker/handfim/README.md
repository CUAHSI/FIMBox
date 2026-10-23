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
- `stream-scenario` runs scenarios in streaming mode (below).
- `clean` cleans previously generated FIM GeoTIFFs.

The API worker invokes `scenario` directly. For request structure and
submission, see the [API guide](../api/README.md); example inputs are in
[`test-data/`](../../test-data/README.md).

Outputs are written under `/home/output/`. The worker uploads the current
job's files to S3 when configured, then removes job inputs, scratch files,
watershed downloads, and local outputs. Only one worker should use a shared
watershed filesystem.

## Streaming mode

Legacy `scenario` downloads whole HUC hydrofabrics, which must fit in Cloud
Run's memory-backed filesystem. Streaming mode instead range-reads a prepared,
cloud-optimized copy of the hydrofabric from S3 and uploads output tiles as
they are produced, targeting a 4 GiB worker with no scratch storage.
`patches/streamFIM.py` reimplements the NOAA inundation rule (interpolated
stage per HydroID, `stage > REM` inside the reach's catchments, maximum across
branches) and is checked against a NumPy reference of the NOAA code.

Prepare each HUC once. The script downloads only the files it reads (branch
list, hydro-table, and each branch's REM and catchment rasters) from NOAA's
public `ciroh-owp-hand-fim` bucket for `--fim-version` (default 4.5.2.11) into
`--huc-dir` (default `output/flood_<huc>/<huc>`, the fimserve layout), unless
branch 0's rasters are already there. Interrupted downloads resume:

```bash
python /home/prepare_streaming_dataset.py 03020201 /data/hand-stream
```

This writes `dataset.json`, `hucs/<huc>/huc.json`, lossless 512-pixel COGs
(`hucs/<huc>/branches/<b>/rem.tif` and `catchments.tif`), and one
`hucs/<huc>/reaches/<feature_id>.json` per reach holding its rating curves and
the COG blocks its catchments occupy. HydroIDs mapped to multiple feature IDs
retain each feature's rows and their source-table positions. Streaming combines
only requested features in original row order before interpolation, matching
NOAA's HydroID grouping rather than selecting an arbitrary feature.
Publish the directory unchanged to a
HydroShare resource (or any S3 bucket). `huc.json` is written last, so a
partially published HUC is reported as missing.

When uploading with recent AWS CLI versions to HydroShare, disable optional
upload checksums to avoid AWS chunked-transfer framing being stored as file
content by the S3 gateway:

```bash
AWS_REQUEST_CHECKSUM_CALCULATION=when_required aws s3 sync /data/hand-stream/ \
  s3://resource/<resource_id>/data/contents/hand-stream/ \
  --endpoint-url https://s3.hydroshare.org --size-only
```

Use your HydroShare S3 credentials for this command. If previously uploaded
JSON files start with a hexadecimal chunk length or COGs do not start with a
TIFF header, re-upload the entire prepared dataset from the original local
files with this setting. Correcting only the JSON files is insufficient:
chunk framing also corrupts raster byte offsets. `--size-only` replaces
objects whose stored size differs from the original local file.

`stream-scenario <json_file> <output_dir>` reads `FIMBOX_STREAM_INPUT_BUCKET`,
`FIMBOX_STREAM_INPUT_PREFIX`, optional `FIMBOX_STREAM_INPUT_UNSIGNED`,
`FIMBOX_STREAM_OUTPUT_BUCKET`/`_PREFIX`,
`FIMBOX_STREAM_TILE_SIZE`, `FIMBOX_S3_*`, and `AWS_*` credentials from the
environment (the API worker sets these). For each fixed output window it reads
only the blocks of reaches intersecting that window, writes a binary
inundation COG (1 = wet, nodata = dry) to
`<prefix>/<ScenarioID>/<huc>/<row>_<col>.cog`, and records
it in `<output_dir>/<ScenarioID>.manifest.json`. Peak memory depends on the
tile size, not the HUC size. HUCs keep their native grids instead of being
mosaicked; viewers show the union of wet cells, whereas legacy output keeps the
first HUC's value where HUCs overlap. Tile paths are stable across runs:
overwrites are non-atomic and failures can leave mixed old/new tiles.
Use different prefixes for concurrent runs; failed runs retain uploaded tiles.

Each output tile is cropped to its flooded-cell bounding box before COG
encoding. Manifest bounds therefore frame inundation rather than empty tile
padding, including when viewed on the notebook map. The row/column in each
tile filename still identifies its original processing window.
