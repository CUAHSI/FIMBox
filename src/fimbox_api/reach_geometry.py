"""Build per-scenario GeoJSON layers from USGS NLDI reach geometries."""

import json
import math
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx

from .models import HandFimParameters

NLDI_COMID_URL = "https://api.water.usgs.gov/nldi/linked-data/comid"
MAX_RESPONSE_BYTES = 2 * 1024 * 1024


def __valid_line(line: object) -> bool:
    if not isinstance(line, list) or len(line) < 2:
        return False
    for position in line:
        if not isinstance(position, list) or len(position) not in (2, 3):
            return False
        if any(isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value)
               for value in position):
            return False
        if not -180 <= position[0] <= 180 or not -90 <= position[1] <= 90:
            return False
    return True


def __lookup(client: httpx.Client, reach_id: str) -> tuple[dict | None, str | None]:
    try:
        with client.stream("GET", f"{NLDI_COMID_URL}/{reach_id}") as response:
            response.raise_for_status()
            content = bytearray()
            for chunk in response.iter_bytes():
                content.extend(chunk)
                if len(content) > MAX_RESPONSE_BYTES:
                    return None, "NLDI response exceeded the size limit"
        document = json.loads(content)
        features = document["features"]
        if document["type"] != "FeatureCollection" or not isinstance(features, list) or len(features) != 1:
            raise ValueError("expected one feature")
        feature = features[0]
        geometry = feature["geometry"]
        identifier = feature["properties"]["comid"]
        coordinates = geometry["coordinates"]
        geometry_type = geometry["type"]
        valid = (
            __valid_line(coordinates) if geometry_type == "LineString" else
            isinstance(coordinates, list) and bool(coordinates) and all(map(__valid_line, coordinates))
            if geometry_type == "MultiLineString" else False
        )
        if str(identifier) != str(int(reach_id)) or not valid:
            raise ValueError("invalid reach geometry")
        return geometry, None
    except httpx.HTTPStatusError as error:
        return None, f"NLDI returned HTTP {error.response.status_code}"
    except httpx.RequestError:
        return None, "NLDI request failed"
    except (ValueError, KeyError, TypeError, IndexError, OverflowError):
        return None, "NLDI returned invalid reach geometry"


def write_layers(
    scenarios: HandFimParameters, output_dir: Path, missing_reaches: dict[str, set[str]] | None = None,
) -> tuple[list[Path], list[str]]:
    """Write all requested reach entries, retaining explicit errors as null geometries."""
    ids = list(dict.fromkeys(reach.ReachID for scenario in scenarios.root for reach in scenario.Reaches))
    with httpx.Client(timeout=10.0, trust_env=False, follow_redirects=False) as client:
        with ThreadPoolExecutor(max_workers=8) as executor:
            results = dict(zip(ids, executor.map(lambda reach_id: __lookup(client, reach_id), ids)))

    warnings = [f"Reach {reach_id}: {error}" for reach_id, (_, error) in results.items() if error]
    paths = []
    for scenario in scenarios.root:
        missing = None if missing_reaches is None else missing_reaches[scenario.ScenarioID]
        features = []
        for reach in scenario.Reaches:
            geometry, error = results[reach.ReachID]
            properties = {
                "HUC": reach.HUC, "ReachID": reach.ReachID, "Streamflow": reach.Streamflow,
                "geometry_error": error,
                "processing_status": (
                    "unknown" if missing is None else
                    "skipped" if f"{reach.HUC}:{reach.ReachID}" in missing else "not_reported_missing"
                ),
            }
            features.append({"type": "Feature", "geometry": geometry, "properties": properties})
        path = output_dir / f"{scenario.ScenarioID}.reaches.geojson"
        path.write_text(json.dumps({"type": "FeatureCollection", "features": features}), encoding="utf-8")
        paths.append(path)
    return paths, warnings
