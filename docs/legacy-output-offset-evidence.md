# Evidence: legacy output is one row north of the source-aligned output

Verified on 2026-10-27 using the downloaded single-reach parity case.

## Interactive visual report

Open the generated [offline visual report](../res_download/parity/legacy-offset-report.html)
in a browser. The generated file is a local deliverable, not a committed fixture.
It embeds a crop of the original NOAA REM/catchment cells and compares the
independently reconstructed flood mask against the downloaded legacy and
streaming masks on the same native grid.

- Start with **Source vs. legacy differences**: orange cells are legacy-only
  flooding, cyan cells are source-only flooding, and purple cells flood in both.
- Zoom into the grid and point at a cell to inspect the original REM, HydroID,
  interpolated stage, native row/column, and each mask's wet/dry classification.
- Switch to **Original inputs only** or the catchment background to inspect the
  source cells without flood overlays.
- Enable **Demonstration: move legacy 1 row south** to see all 486 differences
  disappear. This changes only the display, not any saved data. Reset restores
  the measured, unaligned comparison.

Generate it again from the repository root, using a Python environment with
NumPy and Rasterio and the downloaded outputs at their documented paths:

```bash
python tools/visualize_legacy_offset.py \
  --output res_download/parity/legacy-offset-report.html
```

The [generator](../tools/visualize_legacy_offset.py) reads source raster windows
from NOAA's public endpoint, uses the documented rating brackets below, and
embeds the evidence in the [HTML template](../tools/legacy_offset_report.html).
Generation needs internet access; the resulting HTML is self-contained and
can be shared/opened offline without a server or credentials. `--legacy` and
`--streaming` can select other local copies of **this same case**; this is a
case-specific evidence tool, not a general scenario runner.

## Conclusion and scope

For **HUC 03020201, reach 8780625, discharge 7.0 m3/s**, the legacy output
places the flood mask **10 metres (one pixel) north** of the location
independently reconstructed from NOAA's native REM, catchments, and rating
curves. Here, north means grid north in EPSG:5070.

The streaming output matches that source-derived mask at its actual geographic
placement, with **zero differing cells**. The legacy output has **486 differing
cells** when compared at its actual placement, without parity's automatic
realignment.

The follow-up investigation below reproduces the displacement in the pinned
NOAA branch-mosaic writer: floating-point destination windows place data one
row too far north. x does **not** establish that every legacy scenario has
the same shift or validate the absolute geographic accuracy of NOAA's
hydrofabric against ground observations. No production coordinates were changed
during this investigation.

## Local evidence

The inputs and outputs used were:

- [Scenario input](../res_download/parity/scenarios.json), identical to the
  [legacy scenario input](../res_download/legacy/scenarios.json).
- [Legacy GeoTIFF](../res_download/legacy/single-reach-scenario.tif).
- [Legacy COG](../res_download/legacy/single-reach-scenario.cog).
- [Streaming COG](../res_download/parity/single-reach-scenario/03020201/2_0.cog).
- [Streaming manifest](../res_download/parity/single-reach-scenario/manifest.json).
- [Parity report](../res_download/parity/parity/report.json).

These are local, untracked downloads, not fixtures committed with this note.
The SHA-256 values identify the inspected files:

| File           | SHA-256                                                              |
| -------------- | -------------------------------------------------------------------- |
| Legacy GeoTIFF | `b91044aaa9564bc6d21f4f31162cc7cf37f34a83583676dbc03134cb3e5f75e3` |
| Legacy COG     | `838934e9fc9c41c5f8088ef45234051380bfbb5da1e13fd6df64ba08d4125b86` |
| Streaming COG  | `3e6ec1fb3193424cc048da301cbb3917137c4200c1f9a1c6aeee16cfd017fa8a` |

### 1. The pixel arrays are identical, but their origins differ

Both COGs:

- Use EPSG:5070, with 10-metre pixels and north-up transforms.
- Have 339 rows and 123 columns.
- Contain 1,242 flooded cells.
- Have exactly equal arrays, including NaNs in the same positions.

Their affine transforms, in Rasterio's `(a, b, c, d, e, f)` order, are:

```text
Legacy:
(10.0, 0.0, 1500969.4963396771, 0.0, -10.0, 1580780.8454759854)

Streaming:
(10.0, 0.0, 1500969.4963396771, 0.0, -10.0, 1580770.8454759854)
```

Only the northing origin differs: legacy minus streaming is **+10.0 metres**.
Consequently, each identical pixel value is placed one row north in legacy.
The legacy GeoTIFF has the same transform as the legacy COG, so the discrepancy
is already present before that final COG representation.

This alone proves relative displacement, but not which placement matches
the inputs. The source reconstruction below supplies that independent check.

### 2. Parity detects the shift but removes it before declaring a match

The downloaded report contains:

```json
{
  "legacy_flooded_cells": 1242,
  "streaming_flooded_cells": 1242,
  "different_cells": 0,
  "grid_shift_rows": 1,
  "grid_shift_columns": 0,
  "unaligned_different_cells": 486,
  "matches": true
}
```

In [the comparison implementation](../src/fimbox_api/parity.py),
`grid_shift_rows` describes streaming's position relative to legacy; positive
means south. The function tries a small centroid-derived shift and accepts it
when it reduces differences. Thus `matches: true` here means equal **after
alignment**, not equal geographic placement.

### 3. NOAA's native inputs support the streaming placement

The source is NOAA's public `ciroh-owp-hand-fim` S3 bucket, region `us-east-2`,
version `4.5.2.11`:

- [Branch 0 REM](https://ciroh-owp-hand-fim.s3.us-east-2.amazonaws.com/hand_fim_4_5_2_11/03020201/branches/0/rem_zeroed_masked_0.tif).
- [Branch 0 catchments](https://ciroh-owp-hand-fim.s3.us-east-2.amazonaws.com/hand_fim_4_5_2_11/03020201/branches/0/gw_catchments_reaches_filtered_addedAttributes_0.tif).
- [Branch 0 hydro-table](https://ciroh-owp-hand-fim.s3.us-east-2.amazonaws.com/hand_fim_4_5_2_11/03020201/branches/0/hydroTable_0.csv).

The native branch 0 grid has 13,042 rows and 13,778 columns, with transform:

```text
(10.0, 0.0, 1483259.4963396771, 0.0, -10.0, 1623580.8454759854)
```

Expressing each output's upper-left corner on that native grid gives:

| Output    | Native column | Native row |
| --------- | ------------: | ---------: |
| Legacy    |          1771 |       4280 |
| Streaming |          1771 |       4281 |

The four relevant non-lake HydroIDs for reach 8780625 have these rating
points bracketing the requested discharge. Both bracketing stages are
0.3048 and 0.6096 metres, respectively:

| HydroID  | Lower discharge (m3/s) | Upper discharge (m3/s) | Interpolated stage at 7.0 m3/s (m) |
| -------- | ---------------------: | ---------------------: | ---------------------------------: |
| 11632048 |     2.4557773459754286 |     11.702491111442828 |                             0.4546 |
| 11632049 |     2.5749294685224284 |      9.876610732716388 |                             0.4895 |
| 11632050 |      3.237160429723855 |      14.35561251451124 |                             0.4080 |
| 11632051 |     2.7320025765722296 |      11.67922556841276 |                             0.4502 |

Stages use `float32(round(np.interp(Q, discharge_cms, stage), 4))`.
Rating rows were read from the public branch hydro-table. The large CSV read
eventually failed, but the rows for these four HydroIDs had already been
captured; the bracketing values above and the comparison were verified
separately. A complete all-branch source audit was not performed.

For each HydroID, independently evaluate:

```text
catchment == HydroID AND REM >= 0 AND REM < interpolated_stage
```

Reading the native window covering **both output placements** (column 1771,
row 4280, width 123, height 340) produces:

| Mask                                | Flooded cells | Differing cells from source-derived mask |
| ----------------------------------- | ------------: | ---------------------------------------: |
| Native branch 0 reconstruction      |          1242 |                                        0 |
| Streaming at its recorded placement |          1242 |                                        0 |
| Legacy at its recorded placement    |          1242 |                                      486 |

The extra row is important: comparing only one output's cropped extent would
omit cells at the other placement's boundary. The 486 differing cells are the
symmetric difference of the wet masks, not 486 missing flooded cells.

## Reproduce the source comparison

Run the following Python from the repository root in an environment with
NumPy and Rasterio. It uses the two local downloaded COGs and reads windows
from the public source rasters over HTTP; no HydroShare credentials are needed.
The rating brackets are transcribed above so reproduction does not require
downloading the approximately 163 MB branch hydro-table.

This comparison was executed successfully during the investigation:

```python
from pathlib import Path

import numpy as np
import rasterio
from rasterio.windows import Window

root = Path("res_download")
legacy_path = root / "legacy/single-reach-scenario.cog"
stream_path = root / "parity/single-reach-scenario/03020201/2_0.cog"
base = (
    "https://ciroh-owp-hand-fim.s3.us-east-2.amazonaws.com/"
    "hand_fim_4_5_2_11/03020201/branches/0/"
)
curves = {
    11632048: (2.4557773459754286, 11.702491111442828),
    11632049: (2.5749294685224284, 9.876610732716388),
    11632050: (3.237160429723855, 14.35561251451124),
    11632051: (2.7320025765722296, 11.67922556841276),
}
stages = {
    hydroid: np.float32(round(np.interp([7.0], q, [0.3048, 0.6096])[0], 4))
    for hydroid, q in curves.items()
}

with rasterio.open(legacy_path) as legacy, rasterio.open(stream_path) as stream:
    a, b = legacy.read(1), stream.read(1)
    assert np.array_equal(a, b, equal_nan=True)
    assert legacy.crs == stream.crs == rasterio.crs.CRS.from_epsg(5070)
    assert legacy.shape == stream.shape == (339, 123)
    assert legacy.transform.f - stream.transform.f == 10.0
    with rasterio.Env(
        GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR",
        CPL_VSIL_CURL_ALLOWED_EXTENSIONS=".tif",
        GDAL_HTTP_TIMEOUT=30,
    ):
        with rasterio.open(base + "rem_zeroed_masked_0.tif") as rem:
            source_transform = rem.transform
            lc, lr = ~source_transform * (legacy.transform.c, legacy.transform.f)
            sc, sr = ~source_transform * (stream.transform.c, stream.transform.f)
            print("Native-grid origins (col, row):", (lc, lr), (sc, sr))
            assert (lc, lr, sc, sr) == (1771.0, 4280.0, 1771.0, 4281.0)
            window = Window(int(lc), int(lr), legacy.width, legacy.height + 1)
            rem_values = rem.read(1, window=window)
        with rasterio.open(
            base + "gw_catchments_reaches_filtered_addedAttributes_0.tif"
        ) as cats:
            assert cats.transform == source_transform
            catchments = cats.read(1, window=window)
    source = np.zeros(rem_values.shape, dtype=bool)
    for hydroid, stage in stages.items():
        source |= (
            (catchments == hydroid) & (rem_values >= 0) & (rem_values < stage)
        )
    legacy_mask = np.zeros(source.shape, dtype=bool)
    stream_mask = np.zeros(source.shape, dtype=bool)
    legacy_mask[:-1] = a == 1
    stream_mask[1:] = b == 1
    print(
        "Flooded cells (source, legacy, streaming):",
        int(source.sum()), int(legacy_mask.sum()), int(stream_mask.sum()),
    )
    print(
        "Differences from source (legacy, streaming):",
        int(np.count_nonzero(source != legacy_mask)),
        int(np.count_nonzero(source != stream_mask)),
    )
    assert np.array_equal(source, stream_mask)
    assert np.count_nonzero(source != legacy_mask) == 486
```

Expected output:

```text
Native-grid origins (col, row): (1771.0, 4280.0) (1771.0, 4281.0)
Flooded cells (source, legacy, streaming): 1242 1242 1242
Differences from source (legacy, streaming): 486 0
```

## Implication for Parquet preparation

The inspected streaming artifact uses dataset version 1 (JSON reach indexes).
The Parquet migration changes rating/index serialization, not source raster
transforms, branch offsets, or output placement. It therefore neither creates
nor corrects this relative displacement. The existing Parquet regression tests
check unchanged flood results.

Shifting prepared Parquet block indexes or streaming transforms north to match
this legacy output would move this case **away from** the native-source result.
Applying a universal one-row adjustment is not justified: the actual defect is
the mosaic window conversion described below.

## Follow-up: the displacement is introduced at the branch-mosaic write

The responsible code is upstream NOAA
[`tools/overlapping_inundation.py`](https://github.com/NOAA-OWP/inundation-mapping/blob/9d39cb85ae789b30964a82c19dd850d29fccf5df/tools/overlapping_inundation.py),
at the commit pinned by [the HAND-FIM Dockerfile](../docker/handfim/Dockerfile).
It is not the Parquet preparer or FIMBox's final crop/COG conversion.

### Call path and mechanism

1. FIMBox's [`runfim`](../docker/handfim/patches/runFIM.py) calls NOAA's
   `inundate_mosaic_wrapper.py`.
2. The wrapper computes branch inundation rasters, then calls
   [`Mosaic_inundation`](https://github.com/NOAA-OWP/inundation-mapping/blob/9d39cb85ae789b30964a82c19dd850d29fccf5df/tools/mosaic_inundation.py).
   Its `mosaic_by_unit` constructs `OverlapWindowMerge` with **30 by 30**
   partitions.
3. [`create_lat_lons`, line 153](https://github.com/NOAA-OWP/inundation-mapping/blob/9d39cb85ae789b30964a82c19dd850d29fccf5df/tools/overlapping_inundation.py#L153)
   converts partition pixel coordinates into map coordinates, using an origin
   divided by the resolution and then multiplying back.
4. [`read_rst_data`, line 210](https://github.com/NOAA-OWP/inundation-mapping/blob/9d39cb85ae789b30964a82c19dd850d29fccf5df/tools/overlapping_inundation.py#L210)
   converts the map bounds back to source and destination pixel windows with
   `rasterio.windows.from_bounds`. It does not snap near-integer values back
   to integers.
5. [`merge_data`, line 437](https://github.com/NOAA-OWP/inundation-mapping/blob/9d39cb85ae789b30964a82c19dd850d29fccf5df/tools/overlapping_inundation.py#L437)
   passes the floating-point `final_window` directly to
   `rst.write_band(..., window=final_window)`. In the reproduction, reads
   sample from the intended row, but writes use the preceding row.

For this native grid, a regular partition is 434 rows by 459 columns. The
reach spans partition rows 9 and 10 and columns 3 and 4 (zero-based). The
actual windows produced by the pinned methods are:

| Partition row | Intended start row | Floating-point start row | Effective write start row |
| ------------- | -----------------: | -----------------------: | ------------------------: |
| 9             |               3906 |        3905.999999999977 |                      3905 |
| 10            |               4340 |        4339.999999999977 |                      4339 |

Both column partitions have the same row error. For partition `(9, 3)`, the
window is:

```text
col_off = 1377.0
row_off = 3905.999999999977
width   = 459.0
height  = 434.00000000000045
```

The row discrepancy is only about `2.3e-11` pixels, but truncating a value just
below an integer loses a **whole row**. A marker at native row 4281 is read
at window row 375 (`3906 + 375 = 4281`) and written at native row 4280
(`3905 + 375 = 4280`).

FIMBox's later cropping retains that already-shifted placement. It correctly
translates the mosaic transform by the cropped pixel offsets; it cannot
recover where the mosaic pixels originally belonged.

### Automated regression test against line 437

[`tests/test_legacy_mosaic_offset.py`](../tests/test_legacy_mosaic_offset.py)
runs the unmodified pinned `OverlapWindowMerge.merge_rasters` on a sparse
raster that uses the branch 0 grid above. The raster contains a single marker
cell at native row 4281, column 1771. The test intercepts Rasterio's
`write_band` and confirms the following:

- The upstream `rst.write_band(..., window=final_window)` call is at
  `merge_data` line 437.
- The marker partition is written from that line with
  `row_off = 3905.999999999971`. This value is just below 3906; it comes from
  the full upstream path and differs from the isolated reconstruction above in
  the last digits.
- The array written there has the marker at the correct relative row, 375.
- The output mosaic keeps the input transform, but the marker lands at native
  row **4280**.
- A control run that rounds only the window passed to `merge_data` writes at
  `row_off = 3906` and keeps the marker at row **4281**.

The test needs the pinned NOAA module, so it is skipped on the host. Run it
inside the HAND-FIM image (about 15 seconds):

```bash
docker run --rm --platform linux/amd64 -v "$PWD/tests:/tests:ro" \
  -e PYTHONPATH=/home/code/inundation-mapping/tools \
  --entrypoint /opt/conda/bin/python cuahsi/handfim:latest \
  -m unittest discover -s /tests -p test_legacy_mosaic_offset.py -v
```

This passed with the image's NumPy 2.0.2, Rasterio 1.4.4, and GDAL 3.10.3.

### Controlled reproduction with the downloaded flood mask

The investigation loaded the pinned upstream implementations of
`get_window_coords`, `create_lat_lons`, and `read_rst_data` and ran them on a
sparse raster with the real branch 0 grid. The downloaded streaming flood
mask was inserted at its source-verified location. The four intersecting
partitions were read with those upstream methods and written with Rasterio's
window writer, the same write path used by `merge_data`.

This isolates the coordinate/read/write path; it does not rerun the NOAA
science, all-branch aggregation, or the complete container job. Other pixels
were nodata. The experiment compared two destination-write choices:

| Write window                       | Flooded cells | Cropped mask equals downloaded legacy | Cropped transform        |
| ---------------------------------- | ------------: | ------------------------------------- | ------------------------ |
| Unchanged upstream float window    |          1242 | Yes, exactly                          | Equals legacy exactly    |
| Nearest-integer destination window |          1242 | Yes, exactly                          | Equals streaming exactly |

Thus the unmodified window path reproduces both the **entire downloaded
legacy flood mask** and its **exact shifted transform**. Changing only the
destination window to integer pixel coordinates restores the streaming/source
placement without changing the cropped mask.

The experiments ran with Rasterio 1.5.2, GDAL 3.12.2, and NumPy 2.5.4. The
installed versions in the container that produced the downloads were not
recovered. Saved pre-mosaic rasters from that historical run were not available,
so this is a controlled reproduction of the pinned code path, rather than a
trace captured inside that original job.

### Minimal executable reproduction of the row loss

This offline example uses the real grid and a single marker. It creates
sparse temporary files, verifies both the incorrect write and corrected
placement, and deletes the temporary files on exit. It was executed
successfully during the investigation.

```python
import tempfile
from pathlib import Path

import numpy as np
import rasterio
from rasterio.transform import Affine
from rasterio.windows import Window, from_bounds

transform = Affine(10, 0, 1483259.4963396771, 0, -10, 1623580.8454759854)
origin = np.array([transform.f, transform.c]) / 10
upper_left = (origin + [-3906, 1377]) * 10
lower_right = (origin + [-4340, 1836]) * 10
window = from_bounds(
    upper_left[1], lower_right[0], lower_right[1], upper_left[0], transform
)
print("Reconstructed row offset:", window.row_off)
profile = dict(
    driver="GTiff", width=13778, height=13042, count=1,
    dtype="uint8", crs="EPSG:5070", transform=transform,
    nodata=0, tiled=True, SPARSE_OK=True, compress="deflate",
)
with tempfile.TemporaryDirectory() as folder:
    source_path = Path(folder) / "source.tif"
    with rasterio.open(source_path, "w", **profile) as source:
        source.write(
            np.ones((1, 1), dtype="uint8"), 1,
            window=Window(1771, 4281, 1, 1),
        )
    with rasterio.open(source_path) as source:
        data = source.read(1, window=window)
    marker_row, marker_col = np.argwhere(data == 1)[0]
    print("Read marker at native row:", 3906 + int(marker_row))
    assert (int(marker_row), int(marker_col)) == (375, 394)
    for rounded in (False, True):
        destination_window = window
        if rounded:
            destination_window = Window(*(round(value) for value in (
                window.col_off, window.row_off, window.width, window.height
            )))
        path = Path(folder) / f"output-{rounded}.tif"
        with rasterio.open(path, "w", **profile) as destination:
            destination.write(data, 1, window=destination_window)
        with rasterio.open(path) as destination:
            result = destination.read(
                1, window=Window(1771, 4280, 1, 2)
            )[:, 0]
        print("Rounded:", rounded, "marker at rows 4280/4281:", result.tolist())
        assert result.tolist() == ([0, 1] if rounded else [1, 0])
```

Expected output:

```text
Reconstructed row offset: 3905.999999999977
Read marker at native row: 4281
Rounded: False marker at rows 4280/4281: [1, 0]
Rounded: True marker at rows 4280/4281: [0, 1]
```

### Correction point

Fix the upstream branch-mosaic window construction, not the prepared indexes
or final output transform. Preserve integer pixel windows wherever possible;
where aligned-grid windows are recovered from map bounds, validate that their
offsets and dimensions are within a small tolerance of integers before
snapping them. Nonaligned grids need explicit handling rather than unconditional
rounding. Include source reads, destination writes, boundary partitions, and
multiple-branch placement in regression tests.

Rounding the destination is sufficient to fix the isolated case above, but
this document does not apply or certify a general production patch. In
particular, Rasterio's length/floor rounding helpers should not be assumed
equivalent to nearest-integer snapping of both offsets and dimensions.

### Status in the latest NOAA release

NOAA has already removed the faulty code. In
[v4.8.14.0](https://github.com/NOAA-OWP/inundation-mapping/pull/1650)
(2025-10-10, commit `b4459b5`), `tools/overlapping_inundation.py` was deleted.
`mosaic_inundation.mosaic_by_unit` now calls
`rasterio.merge.merge(maps, method="max", ...)` instead. The latest checked
`dev` commit, `558b250` (v4.10.3.1, 2026-10-23), still uses that path and pins
Rasterio 1.4.4, the same version as the HAND-FIM image.

This merge was run in the HAND-FIM image on the same sparse branch 0 marker
raster used by the regression test. A second, smaller branch was added at
row 3000, column 1000, and both input orders were tried. Every mosaic kept the
branch 0 transform and placed the markers at their native positions,
row 4281, column 1771 and row 4500, column 1500, with no one-row shift.

The defect therefore affects only FIMBox's pinned commit `9d39cb85`. This is a
synthetic check of the merge step; it is not a full parity run of the latest
NOAA pipeline. The Dockerfile notes that the pin was kept because later
upstream changes broke mosaicking. Before adopting a newer NOAA commit, rerun
the legacy/streaming parity test on it.
