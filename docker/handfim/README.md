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

## Updated legacy mode

`scenario --updated-legacy` runs the same FIMserv workflow and post-processing
using NOAA v4.8.14.0 (`b4459b5d4dd71e67f84a5656d7109237e21903ed`).
That version replaces the old `OverlapWindowMerge` with Rasterio's merge.
The image contains separate original and updated NOAA checkouts; default
legacy execution still uses the original pinned commit. Both modes use the
same NOAA 4.5 hydrofabric downloads, not a newer input dataset.

Rebuild the base image and API worker before using this option. From the host:

```bash
fimbox submit --body test-data/single_reach.json \
  --execution-mode updatedlegacy \
  --output-bucket YOUR_HYDROSHARE_BUCKET --output-prefix updatedlegacy
```

In the SDK, use `HandFimJobRequestExecutionMode.UPDATEDLEGACY`; the API wire
value is `"updatedlegacy"`. It accepts the same output/concurrency settings as
legacy and returns ordinary `<ScenarioID>.cog` outputs plus `scenarios.json`.
Use that output prefix as `legacy_prefix` in `POST /jobs/parity` to compare it
with streaming. Use different prefixes for original, updated, and parity runs.

This tests a newer NOAA implementation, not just a mosaic-only patch:
v4.8.14.0 also suppresses flood depths below approximately 3 cm (0.03048 m
for float REMs). Plain streaming does not apply that threshold, so genuine
mask differences can remain even when the row shift is gone; compare with
[updated streaming mode](#updated-streaming-mode) instead.

Container routing and real synthetic FIMserv/NOAA execution tests:

```bash
docker run --rm --platform linux/amd64 -v "$PWD:/repo:ro" \
  --entrypoint /opt/conda/bin/python cuahsi/handfim:latest \
  -m unittest discover -s /repo/tests -p test_updated_legacy.py -v
```

## Updated streaming mode

`stream-scenario --updated` (API/CLI `execution_mode` `updatedstreaming`, SDK
`HandFimJobRequestExecutionMode.UPDATEDSTREAMING`) streams the same prepared
dataset as streaming mode but classifies every pixel with NOAA v4.8.14.0's own
code from the updated checkout: `__subset_hydroTable_to_forecast` interpolates
each HydroID's stage and the numba kernel `__go_fast_mapping` marks wet cells
(see `noaa_stream_rule.py`). It therefore inherits NOAA's 0.03048 m minimum
depth (30 mm with int16 catchments) and any future rule fixes in that
checkout. FIMBox still plans blocks, unions branches (equivalent to NOAA's
`rasterio.merge` max), and writes tiles. Both streaming modes use the same
prepared dataset.

It has the same constraints as streaming (`output_bucket`, `max_procs` 1,
`STREAM_INPUT_BUCKET`). To check it, run `updatedlegacy` to one prefix, then
`POST /jobs/parity` with `"execution_mode": "updatedstreaming"` and that
prefix as `legacy_prefix`. On HUC 03020201 reach 8780625 at 7 cms both modes
flood the same 1163 cells; plain streaming adds 79 cells shallower than 3 cm.

Container tests comparing it with NOAA's public `inundate()` (mount a NOAA
`tools/` directory with `FIMBOX_UPDATED_NOAA_TOOLS` if the image predates
this checkout):

```bash
docker run --rm --platform linux/amd64 -v "$PWD:/repo:ro" -w /repo \
  --entrypoint /opt/conda/bin/python cuahsi/handfim:latest \
  -m unittest tests.test_updated_streaming -v
```

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

This writes `dataset.parquet`, `hucs/<huc>/huc.parquet`, lossless 512-pixel COGs
(`hucs/<huc>/branches/<b>/rem.tif` and `catchments.tif`), and one
`hucs/<huc>/reaches/<feature_id>.parquet` per reach holding its rating curves and
the COG blocks its catchments occupy. Rasters remain COG GeoTIFFs for spatial
range reads; all prepared metadata and reach indexes use Parquet.

`dataset.parquet` has one row with `format` (string), `version` (int32),
`block_size` (int32), and `fim_version` (string).

`huc.parquet` has one row per branch, with the HUC's reference-grid columns
repeated so each row is independently useful:

| Columns | Type | Meaning |
| ------- | ---- | ------- |
| `huc`, `branch` | string | HUC-8 and branch identifiers, preserving leading zeros |
| `crs` | string | Reference-grid CRS as WKT |
| `transform` | list of 6 float64 | Reference-grid affine coefficients `[a, b, c, d, e, f]` |
| `width`, `height` | int64 | Reference-grid dimensions in pixels |
| `block_size` | int32 | COG block size in pixels |
| `rem`, `catchments` | string | Raster paths relative to the dataset root |
| `row_off`, `col_off` | int64 | Branch origin on the reference grid, in pixels |
| `branch_width`, `branch_height` | int64 | Branch raster dimensions in pixels |

All tables use Snappy compression for compatibility with Parquet viewers.
Existing Zstandard-compressed files remain readable by the streaming worker,
but must be regenerated or recompressed for viewers without Zstandard support.
The schema and dataset version are unchanged by this codec choice.
Reach tables have one row per
branch/HydroID for that reach, with these typed columns:

| Column            | Type                   | Meaning                                              |
| ----------------- | ---------------------- | ---------------------------------------------------- |
| `huc`           | string                 | HUC-8, preserving leading zeros                      |
| `feature_id`    | string                 | Reach identifier                                     |
| `branch`        | string                 | Source branch identifier                             |
| `hydroid`       | int64                  | Catchment HydroID                                    |
| `discharge_cms` | list of float64        | Rating discharges in source-table order              |
| `stage`         | list of float64        | Matching stages in meters                            |
| `row_order`     | nullable list of int64 | Source-row positions for shared HydroIDs             |
| `blocks`        | list of int32 pairs    | Occupied`[block_row, block_col]` pairs, zero-based |

`blocks` is the union for the reach within that branch, repeated for each of
its HydroIDs. Rating arrays retain float64 precision and original ordering.
Researchers can read a reach with PyArrow or pandas (with PyArrow installed):

```python
import pyarrow.parquet as pq

dataset = pq.read_table("/data/hand-stream/dataset.parquet").to_pandas()
branches = pq.read_table("/data/hand-stream/hucs/03020201/huc.parquet").to_pandas()
table = pq.read_table("/data/hand-stream/hucs/03020201/reaches/8780625.parquet")
ratings = table.to_pandas().explode(["discharge_cms", "stage"])
```

Or read every reach in a HUC as one table with
`pyarrow.dataset.dataset("/data/hand-stream/hucs/03020201/reaches", format="parquet")`.
PyArrow is included in the HAND-FIM image and the test requirements.

Preparation and streaming use dataset format version 3, with Parquet-only
metadata and reach indexes. JSON metadata and older dataset versions are not
supported. To migrate, prepare into a **new output directory** and publish the
complete dataset; the preparer rejects roots containing `dataset.json` or an
incompatible `dataset.parquet`. Rebuild the HAND-FIM and worker images before
reading or preparing version 3 datasets. Per-scenario output manifests remain
JSON; these are job outputs, not prepared dataset metadata.

HydroIDs mapped to multiple feature IDs
retain each feature's rows and their source-table positions. Streaming combines
only requested features in original row order before interpolation, matching
NOAA's HydroID grouping rather than selecting an arbitrary feature.
Publish the directory unchanged to a
HydroShare resource (or any S3 bucket). `huc.parquet` is written last during
preparation, so an incomplete local HUC is not discoverable. Upload the reach
indexes and rasters before publishing `huc.parquet` to preserve this property remotely.

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

## Legacy/streaming parity test

The opt-in scientific regression test runs the real legacy `scenario` command,
prepares lossless streaming inputs from **the same downloaded HUC files**, and
runs the checkout's streaming computation with local file I/O instead of S3.
Neither computation is mocked. Run from the repository root with a built
HAND-FIM image:

```bash
docker run --rm --platform linux/amd64 \
  -v "$PWD:/repo:ro" -v "$PWD/data/output:/home/output" \
  -e FIMBOX_RUN_PARITY=1 \
  --entrypoint /opt/conda/bin/python cuahsi/handfim:latest \
  /repo/tests/test_scenario_parity.py -v
```

By default it uses [`multiple_scenarios.json`](../../test-data/multiple_scenarios.json).
Select another scenario list by adding, for example,
`-e FIMBOX_PARITY_SCENARIOS=/repo/test-data/many_hucs_many_reaches.json`.
Add `-e FIMBOX_PARITY_EXECUTION_MODE=updatedlegacy` to compare the newer NOAA
workflow with streaming instead of original legacy. This also asserts that
no grid realignment is needed; genuine flood-extent differences still fail
the test. Reports include the selected `execution_mode`.
Missing legacy HUC caches are downloaded, so this can require network access,
substantial disk space, and several minutes. Do not run alongside a worker
using the same HUC cache. Prepared streaming inputs are temporary; generated
scenario IDs and output directories are unique to each test run.

Artifacts persist under `data/output/scenarios/parity-<run_id>/`:

- `report.json` records flooded-cell counts, legacy-only and streaming-only
  counts, and up to ten difference coordinates in the reported CRS per scenario.
- Each scenario directory contains streaming COGs, `manifest.json`, and
  `diff.tif`: **0 = equal, 1 = legacy-only flooding, 2 = streaming-only flooding**.
- Legacy scenario TIFFs/COGs and the exact scenario inputs are retained.

Any differing cell or missing streaming reach fails the test with a diagnostic
report and a nonzero exit code. Comparison uses nearest-neighbor sampling on
the legacy grid over the **union** of both extents, so added flooding outside
the legacy crop is not ignored. Cropping, file encoding, nodata representation,
and tile boundaries do not count as scientific differences. Streaming tiles
combine as a flood union; documented multi-HUC overlap/resampling differences
are **reported, not exempted**. The windowed comparison bounds memory usage.

Without `FIMBOX_RUN_PARITY=1`, only the fast synthetic tests of the diff itself
run; the real scientific test is explicitly skipped. Run these on the host:

```bash
python -m unittest discover -s tests -p test_scenario_parity.py -v
```
