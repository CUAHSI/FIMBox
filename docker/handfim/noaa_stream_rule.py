"""Streaming flood rule backed by NOAA inundation-mapping's own newer code.

``updatedstreaming`` mode plugs this rule into ``fimserve.streamFIM``. Each
branch's stages come from NOAA's ``__subset_hydroTable_to_forecast`` and each
streamed window is classified by NOAA's numba kernel ``__go_fast_mapping``,
both loaded from the updated checkout used by ``updatedlegacy``. That keeps
NOAA's minimum depth (0.03048 m, or 30 mm for int16 catchments), int16
HydroID/stage quantization, and lake filter identical without copying them.

Only the per-pixel rule comes from NOAA; FIMBox still plans blocks, unions
branches, and writes native-grid tiles. NOAA's mosaic (``rasterio.merge`` with
``method='max'``) is an equivalent union on the branch 0 grid. NOAA's
``inundate_gms`` passes ``hucs=None``, so its catchment polygon mask is unused.
"""

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import numpy as np
import pandas as pd

UPDATED_NOAA_TOOLS = Path("/home/code/inundation-mapping-updated/tools")
LAKE_FREE = -999


def load_inundation(tools_dir: Path = UPDATED_NOAA_TOOLS) -> ModuleType:
    """
    Loads NOAA's ``tools/inundation.py`` without importing the rest of its tools.

    Arguments:
        tools_dir: The NOAA checkout's ``tools`` directory.
    Returns:
        The loaded NOAA inundation module.
    """

    path = tools_dir / "inundation.py"
    if not path.is_file():
        raise RuntimeError("Updated NOAA code is missing; rebuild the HAND-FIM image")
    existing = sys.modules.get("inundation")
    if existing is not None:
        if Path(existing.__file__).resolve() != path.resolve():
            raise RuntimeError("A different NOAA inundation checkout is already loaded")
        return existing
    # Numba's on-disk cache imports the kernel's original module name.
    spec = importlib.util.spec_from_file_location("inundation", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["inundation"] = module
    spec.loader.exec_module(module)
    return module


def __hydro_table(plan) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Builds NOAA's hydro-table and forecast inputs from a branch plan's rating rows."""

    rows = [
        ("0", feature_id, hydroid, stage, q, LAKE_FREE)
        for hydroid, ratings in plan.ratings.items()
        for feature_id, _, q, stage in ratings
    ]
    table = pd.DataFrame(
        rows, columns=["HUC", "feature_id", "HydroID", "stage", "discharge_cms", "LakeID"],
    ).set_index(["HUC", "feature_id", "HydroID"])
    discharges = {
        feature_id: discharge for ratings in plan.ratings.values() for feature_id, discharge, _, _ in ratings
    }
    forecast = pd.DataFrame(
        {"discharge": list(discharges.values())}, index=pd.Index(list(discharges), name="feature_id"),
    )
    return table, forecast


def updated_rule(module: ModuleType | None = None):
    """
    Creates a ``streamFIM.FloodRule`` that delegates to NOAA's newer code.

    Arguments:
        module: A loaded NOAA inundation module; loaded from the image if None.
    Returns:
        A rule that builds each branch's window flood classifier.
    """

    module = module or load_inundation()
    subset = getattr(module, "__subset_hydroTable_to_forecast")
    kernel = getattr(module, "__go_fast_mapping")

    def rule(plan):
        table, forecast = __hydro_table(plan)
        stages = {}

        def flood(rem: np.ndarray, catchments: np.ndarray) -> np.ndarray:
            if table.empty:
                return np.zeros(rem.shape, dtype=bool)
            int16 = catchments.dtype == np.int16
            if int16 not in stages:
                stages[int16] = subset(table, forecast, None, int16, False)[0]
            nodata = np.int16(0) if int16 else np.int32(0)
            _, extent = kernel(
                rem.copy(), catchments.copy(), stages[int16],
                rem.shape[1], rem.shape[0], nodata, 30 if int16 else 0.03048,
            )
            return extent > 0

        return flood

    return rule
