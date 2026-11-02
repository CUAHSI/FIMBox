"""Build an offline visual report for the documented HUC 03020201 offset case."""

import argparse
import hashlib
import json
import math
from pathlib import Path

import numpy as np
import rasterio
from rasterio.transform import Affine
from rasterio.windows import Window

ROOT = Path(__file__).resolve().parents[1]
SOURCE_BASE = (
    "https://ciroh-owp-hand-fim.s3.us-east-2.amazonaws.com/"
    "hand_fim_4_5_2_11/03020201/branches/0/"
)
# Source hydro-table brackets documented in docs/legacy-output-offset-evidence.md.
RATING_BRACKETS = {
    11632048: (2.4557773459754286, 11.702491111442828),
    11632049: (2.5749294685224284, 9.876610732716388),
    11632050: (3.237160429723855, 14.35561251451124),
    11632051: (2.7320025765722296, 11.67922556841276),
}


def __grid_offset(reference: Affine, transform: Affine) -> tuple[int, int]:
    if not np.allclose(
        [reference.a, reference.b, reference.d, reference.e],
        [transform.a, transform.b, transform.d, transform.e], rtol=0, atol=1e-9,
    ):
        raise ValueError("Output resolution/orientation does not match the native source grid")
    col, row = ~reference * (transform.c, transform.f)
    if abs(col - round(col)) > 1e-6 or abs(row - round(row)) > 1e-6:
        raise ValueError("Output is not aligned to whole native-source pixels")
    return round(row), round(col)


def reconstruct_source(rem: np.ndarray, catchments: np.ndarray, stages: dict) -> np.ndarray:
    """Reconstruct flooding independently from the original source cells."""

    flooded = np.zeros(rem.shape, dtype=bool)
    for hydroid, stage in stages.items():
        flooded |= (catchments == hydroid) & np.isfinite(rem) & (rem >= 0) & (rem < stage)
    return flooded


def __place(mask: np.ndarray, shape: tuple[int, int], row: int, col: int) -> np.ndarray:
    result = np.zeros(shape, dtype=bool)
    result[row:row + mask.shape[0], col:col + mask.shape[1]] = mask
    return result


def read_evidence(legacy_path: Path, streaming_path: Path) -> dict:
    """Read the real output placements and reconstruct this case from NOAA inputs."""

    stages = {
        hydroid: np.float32(round(np.interp([7.0], q, [0.3048, 0.6096])[0], 4))
        for hydroid, q in RATING_BRACKETS.items()
    }
    rem_url = SOURCE_BASE + "rem_zeroed_masked_0.tif"
    catchments_url = SOURCE_BASE + "gw_catchments_reaches_filtered_addedAttributes_0.tif"
    with rasterio.open(legacy_path) as legacy, rasterio.open(streaming_path) as streaming:
        with rasterio.Env(
            GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR",
            CPL_VSIL_CURL_ALLOWED_EXTENSIONS=".tif", GDAL_HTTP_TIMEOUT=60,
        ):
            with rasterio.open(rem_url) as rem:
                if legacy.crs != rem.crs or streaming.crs != rem.crs:
                    raise ValueError("Outputs and source must use the same CRS")
                if rem.transform.b or rem.transform.d or rem.transform.a <= 0 or rem.transform.e >= 0:
                    raise ValueError("The visual report requires a north-up source grid")
                origin = rem.transform
                lr, lc = __grid_offset(origin, legacy.transform)
                sr, sc = __grid_offset(origin, streaming.transform)
                top, left = min(lr, sr) - 2, min(lc, sc) - 2
                bottom = max(lr + legacy.height, sr + streaming.height) + 2
                right = max(lc + legacy.width, sc + streaming.width) + 2
                if top < 0 or left < 0 or bottom > rem.height or right > rem.width:
                    raise ValueError("Comparison extent falls outside the source raster")
                shape = (bottom - top, right - left)
                if math.prod(shape) > 1_000_000:
                    raise ValueError("Comparison exceeds the one-million-cell report limit")
                window = Window(left, top, shape[1], shape[0])
                elevations = rem.read(1, window=window, masked=True).astype(float).filled(np.nan)
            with rasterio.open(catchments_url) as catchments:
                if catchments.crs != legacy.crs or catchments.transform != origin:
                    raise ValueError("Source REM and catchment grids do not match")
                cats = catchments.read(1, window=window, masked=True).filled(0)
        expected = reconstruct_source(elevations, cats, stages)
        legacy_mask = __place(
            legacy.read(1, masked=True).filled(0) == 1, shape, lr - top, lc - left,
        )
        streaming_mask = __place(
            streaming.read(1, masked=True).filled(0) == 1, shape, sr - top, sc - left,
        )
        output_info = {
            "legacy": {"name": str(legacy_path), "transform": list(legacy.transform)[:6]},
            "streaming": {"name": str(streaming_path), "transform": list(streaming.transform)[:6]},
        }
    for name, path in (("legacy", legacy_path), ("streaming", streaming_path)):
        output_info[name]["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    return {
        "width": shape[1], "height": shape[0], "top": top, "left": left,
        "transform": list(origin * Affine.translation(left, top))[:6],
        "source_transform": list(origin)[:6], "crs": "EPSG:5070",
        "rem": [float(value) if np.isfinite(value) else None for value in elevations.ravel()],
        "catchments": cats.ravel().tolist(),
        "source": expected.astype(int).ravel().tolist(),
        "legacy": legacy_mask.astype(int).ravel().tolist(),
        "streaming": streaming_mask.astype(int).ravel().tolist(),
        "stages": {str(h): float(stage) for h, stage in stages.items()},
        "brackets": {str(h): list(q) for h, q in RATING_BRACKETS.items()},
        "outputs": output_info,
        "source_urls": {
            "REM": rem_url, "Catchments": catchments_url,
            "Rating table": SOURCE_BASE + "hydroTable_0.csv",
        },
        "metrics": {
            "source_wet": int(expected.sum()), "legacy_wet": int(legacy_mask.sum()),
            "streaming_wet": int(streaming_mask.sum()),
            "legacy_differences": int(np.count_nonzero(expected != legacy_mask)),
            "streaming_differences": int(np.count_nonzero(expected != streaming_mask)),
            "legacy_rows_relative_to_streaming": lr - sr,
            "legacy_columns_relative_to_streaming": lc - sc,
        },
    }


def write_report(data: dict, output: Path) -> None:
    template = Path(__file__).with_name("legacy_offset_report.html").read_text(encoding="utf-8")
    payload = json.dumps(data, separators=(",", ":"), allow_nan=False)
    payload = payload.replace("&", "\\u0026").replace("<", "\\u003c").replace(">", "\\u003e")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(template.replace("__EVIDENCE_JSON__", payload), encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--legacy", type=Path, default=ROOT / "res_download/legacy/single-reach-scenario.cog")
    parser.add_argument(
        "--streaming", type=Path,
        default=ROOT / "res_download/parity/single-reach-scenario/03020201/2_0.cog",
    )
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    evidence = read_evidence(args.legacy, args.streaming)
    write_report(evidence, args.output)
    print(json.dumps(evidence["metrics"], indent=2))
    print(f"Written {args.output.resolve()}")
