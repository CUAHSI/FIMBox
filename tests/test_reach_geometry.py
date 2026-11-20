import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from fimbox_api import reach_geometry
from fimbox_api.models import HandFimParameters


LINE = {"type": "LineString", "coordinates": [[-79.0, 36.0], [-78.0, 36.0]]}


class ReachGeometryTests(unittest.TestCase):
    def test_nldi_lookup_checks_id_geometry_and_errors(self):
        def lookup(response):
            with httpx.Client(transport=httpx.MockTransport(lambda request: response)) as client:
                return getattr(reach_geometry, "__lookup")(client, "8780625")

        valid = {"type": "FeatureCollection", "features": [
            {"geometry": LINE, "properties": {"comid": 8780625}},
        ]}
        self.assertEqual(lookup(httpx.Response(200, json=valid)), (LINE, None))
        multi = {"type": "MultiLineString", "coordinates": [LINE["coordinates"], LINE["coordinates"]]}
        self.assertEqual(lookup(httpx.Response(200, json={"type": "FeatureCollection", "features": [
            {"geometry": multi, "properties": {"comid": 8780625}},
        ]})), (multi, None))
        invalid = {"type": "FeatureCollection", "features": [
            {"geometry": LINE, "properties": {"comid": 123}},
        ]}
        self.assertEqual(lookup(httpx.Response(200, json=invalid))[1], "NLDI returned invalid reach geometry")
        malformed = {"type": "FeatureCollection", "features": [
            {"geometry": {"type": "LineString", "coordinates": [[400, 36], [0, 0]]},
             "properties": {"comid": 8780625}},
        ]}
        self.assertEqual(lookup(httpx.Response(200, json=malformed))[1], "NLDI returned invalid reach geometry")
        self.assertEqual(lookup(httpx.Response(404))[1], "NLDI returned HTTP 404")
        self.assertEqual(lookup(httpx.Response(200, content=b"not json"))[1], "NLDI returned invalid reach geometry")
        self.assertEqual(
            lookup(httpx.Response(200, content=b"x" * (reach_geometry.MAX_RESPONSE_BYTES + 1)))[1],
            "NLDI response exceeded the size limit",
        )
        with httpx.Client(transport=httpx.MockTransport(
            lambda request: (_ for _ in ()).throw(httpx.ConnectError("unavailable", request=request))
        )) as client:
            self.assertEqual(getattr(reach_geometry, "__lookup")(client, "8780625")[1], "NLDI request failed")

    def test_layers_keep_requested_reaches_and_deduplicate_lookups(self):
        scenarios = HandFimParameters.model_validate([
            {"ScenarioID": "one", "Reaches": [
                {"HUC": "03020201", "ReachID": "8780625", "Streamflow": 7},
                {"HUC": "03020201", "ReachID": "999", "Streamflow": 1},
            ]},
            {"ScenarioID": "two", "Reaches": [
                {"HUC": "03020201", "ReachID": "8780625", "Streamflow": 9},
            ]},
        ])
        def lookup(_client, reach_id):
            return (LINE, None) if reach_id == "8780625" else (None, "NLDI returned HTTP 404")

        with tempfile.TemporaryDirectory() as directory, patch.object(
            reach_geometry, "__lookup", side_effect=lookup,
        ) as mocked:
            paths, warnings = reach_geometry.write_layers(
                scenarios, Path(directory), {"one": {"03020201:999"}, "two": set()},
            )
            one, two = [json.loads(path.read_text()) for path in paths]
            self.assertEqual(mocked.call_count, 2)
            self.assertEqual(warnings, ["Reach 999: NLDI returned HTTP 404"])
            self.assertEqual(one["features"][0]["geometry"], LINE)
            self.assertEqual(one["features"][1]["geometry"], None)
            self.assertEqual(one["features"][1]["properties"]["processing_status"], "skipped")
            self.assertEqual(one["features"][1]["properties"]["geometry_error"], "NLDI returned HTTP 404")
            self.assertEqual(two["features"][0]["properties"]["Streamflow"], 9)
            reach_geometry.write_layers(scenarios, Path(directory))
            self.assertEqual(
                json.loads(paths[0].read_text())["features"][0]["properties"]["processing_status"], "unknown",
            )


if __name__ == "__main__":
    unittest.main()
