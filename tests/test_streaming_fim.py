import csv
import importlib.util
import json
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import numpy as np
import rasterio
from rasterio.transform import Affine

HANDFIM = Path(__file__).resolve().parents[1] / "docker" / "handfim"
RESOLUTION = 10.0
ORIGIN = Affine(RESOLUTION, 0, 500000, 0, -RESOLUTION, 2000000)
BRANCH1_OFFSET = (130, 210)


def load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


stream = load("streaming_test.streamFIM", HANDFIM / "patches" / "streamFIM.py")
prepare = load("streaming_test.prepare", HANDFIM / "prepare_streaming_dataset.py")


def write_raster(path: Path, data: np.ndarray, transform: Affine, nodata) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with rasterio.open(
        path, "w", driver="GTiff", width=data.shape[1], height=data.shape[0], count=1,
        dtype=data.dtype, crs="EPSG:5070", transform=transform, nodata=nodata,
    ) as dataset:
        dataset.write(data, 1)


def build_hydrofabric(root: Path) -> tuple[Path, dict, list[dict]]:
    """Creates branch 0 and an unaligned, smaller branch 1 with NOAA file names."""

    rng = np.random.default_rng(7)
    huc_dir = root / "flood_12090301" / "12090301"
    shape0 = (700, 900)
    rem0 = rng.uniform(-0.5, 10, shape0).astype(np.float32)
    rem0[::37, ::23] = -9999
    cat0 = (1000 + np.arange(shape0[1]) // 100).astype(np.int32)[None, :].repeat(shape0[0], 0)
    cat0[650:, :] = 0
    shape1 = (350, 400)
    rem1 = rng.uniform(0, 6, shape1).astype(np.float32)
    cat1 = (2000 + np.arange(shape1[0]) // 100).astype(np.int32)[:, None].repeat(shape1[1], 1)
    transform1 = ORIGIN * Affine.translation(BRANCH1_OFFSET[1], BRANCH1_OFFSET[0])
    for branch, rem, cat, transform in (
        ("0", rem0, cat0, ORIGIN), ("1", rem1, cat1, transform1),
    ):
        folder = huc_dir / "branches" / branch
        write_raster(folder / f"rem_zeroed_masked_{branch}.tif", rem, transform, -9999.0)
        write_raster(folder / f"gw_catchments_reaches_filtered_addedAttributes_{branch}.tif", cat, transform, 0)
    (huc_dir / "branch_ids.csv").write_text("12090301,0\n12090301,1\n12090301,2\n")

    features = {**{h: "11" for h in (1000, 1001, 1002, 2000, 2001)},
                **{h: "12" for h in (1003, 1004, 1005)},
                **{h: "13" for h in (1006, 1007, 1008)},
                **{h: "14" for h in (2002, 2003)}}
    rows = []
    for hydroid, feature_id in features.items():
        scale = 1 + (hydroid % 7) / 5
        for discharge, stage in ((0, 0), (10, 2), (20, 4), (40, 8)):
            rows.append({
                "HydroID": hydroid, "feature_id": feature_id, "branch_id": str(hydroid // 1000 - 1),
                "LakeID": 5 if hydroid == 1004 else -999, "HUC": "12090301",
                "stage": stage * scale, "discharge_cms": discharge,
            })
    with (huc_dir / "hydrotable.csv").open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    sources = {"0": (rem0, cat0, (0, 0)), "1": (rem1, cat1, BRANCH1_OFFSET)}
    return huc_dir, sources, rows


def noaa_reference(sources: dict, rows: list[dict], reaches: list[dict]) -> np.ndarray:
    """Independent full-raster NOAA dictionary loop, branch nanmax, and 1/NaN cleaning."""

    forecast = {}
    for reach in reaches:
        forecast.setdefault(str(reach["ReachID"]), float(reach["Streamflow"]))
    merged = np.full((700, 900), np.nan, dtype=np.float32)
    for branch, (rem, cat, (row_off, col_off)) in sources.items():
        curves = {}
        for row in rows:
            if row["branch_id"] == branch and row["LakeID"] == -999 and row["feature_id"] in forecast:
                curve = curves.setdefault(row["HydroID"], ([], [], forecast[row["feature_id"]]))
                curve[0].append(row["discharge_cms"])
                curve[1].append(row["stage"])
        stages = {
            hydroid: np.float32(round(np.interp([q], discharges, values)[0], 4))
            for hydroid, (discharges, values, q) in curves.items()
        }
        depth = np.zeros(rem.shape, dtype=np.float32)
        for hydroid, stage in stages.items():
            mask = (cat == hydroid) & (rem >= 0)
            depth[mask] = np.maximum(stage - rem[mask], 0)
        window = merged[row_off:row_off + rem.shape[0], col_off:col_off + rem.shape[1]]
        window[:] = np.fmax(window, depth)
    return np.nan_to_num(merged, nan=0) > 0


def mosaic(manifest: dict, tiles: dict[str, bytes]) -> np.ndarray:
    result = np.zeros((700, 900), dtype=bool)
    for tile in manifest["tiles"]:
        with rasterio.MemoryFile(tiles[tile["key"]]) as memory, memory.open() as dataset:
            assert dataset.dtypes[0] == "float32"
            data = dataset.read(1)
            col = round((dataset.transform.c - ORIGIN.c) / RESOLUTION)
            row = round((dataset.transform.f - ORIGIN.f) / -RESOLUTION)
        assert np.all((data == 1) | np.isnan(data))
        result[row:row + data.shape[0], col:col + data.shape[1]] |= data == 1
    return result


class StreamingFimTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory()
        root = Path(cls.directory.name)
        huc_dir, cls.sources, cls.rows = build_hydrofabric(root)
        cls.dataset = root / "dataset"
        cls.metadata = prepare.prepare_huc("12090301", huc_dir, cls.dataset, block_size=128, log=lambda _: None)

    @classmethod
    def tearDownClass(cls):
        cls.directory.cleanup()

    def read_json(self, key: str):
        path = self.dataset / key
        return json.loads(path.read_text()) if path.is_file() else None

    def run_local(self, reaches: list[dict], **kwargs) -> tuple[dict, dict[str, bytes]]:
        written = {}

        def write_tile(name: str, data: bytes) -> str:
            written[f"attempt/{name}"] = data
            return f"attempt/{name}"

        manifest = stream.run_scenario(
            {"ScenarioID": "s1", "Reaches": reaches}, self.read_json,
            lambda key: str(self.dataset / key), write_tile, log=lambda _: None, **kwargs,
        )
        return manifest, written

    def test_prepared_cogs_are_lossless_and_index_branch_offsets(self):
        self.assertEqual(set(self.metadata["branches"]), {"0", "1"})
        branch1 = self.metadata["branches"]["1"]
        self.assertEqual((branch1["row_off"], branch1["col_off"]), BRANCH1_OFFSET)
        with rasterio.open(self.dataset / branch1["rem"]) as dataset:
            self.assertEqual(dataset.block_shapes[0], (128, 128))
            np.testing.assert_array_equal(dataset.read(1), self.sources["1"][0])
        reach = self.read_json("hucs/12090301/reaches/12.json")
        hydroids = [entry["id"] for entry in reach["branches"][0]["hydroids"]]
        self.assertEqual(hydroids, [1003, 1005])

    def test_streamed_tiles_match_full_raster_noaa_reference(self):
        reaches = [
            {"HUC": "12090301", "ReachID": "11", "Streamflow": 15},
            {"HUC": "12090301", "ReachID": "12", "Streamflow": 25},
            {"HUC": "12090301", "ReachID": "14", "Streamflow": 5},
            {"HUC": "12090301", "ReachID": "11", "Streamflow": 100},
            {"HUC": "12090301", "ReachID": "99", "Streamflow": 5},
        ]
        manifest, tiles = self.run_local(reaches, tile_size=512)
        expected = noaa_reference(self.sources, self.rows, reaches)
        self.assertTrue(expected.any())
        np.testing.assert_array_equal(mosaic(manifest, tiles), expected)
        self.assertEqual(manifest["missing_reaches"], ["12090301:99"])
        self.assertEqual(set(tiles), {tile["key"] for tile in manifest["tiles"]})
        for tile in manifest["tiles"]:
            with rasterio.MemoryFile(tiles[tile["key"]]) as memory, memory.open() as dataset:
                self.assertEqual(dataset.profile["driver"], "GTiff")
                self.assertEqual(dataset.block_shapes[0], (512, 512))
                wet = dataset.read(1) == 1
                self.assertTrue(wet[0].any())
                self.assertTrue(wet[-1].any())
                self.assertTrue(wet[:, 0].any())
                self.assertTrue(wet[:, -1].any())

    def test_tile_size_does_not_change_results(self):
        reaches = [{"HUC": "12090301", "ReachID": "13", "Streamflow": 30}]
        small, small_tiles = self.run_local(reaches, tile_size=512)
        large, large_tiles = self.run_local(reaches, tile_size=1024)
        np.testing.assert_array_equal(mosaic(small, small_tiles), mosaic(large, large_tiles))
        np.testing.assert_array_equal(
            mosaic(small, small_tiles), noaa_reference(self.sources, self.rows, reaches),
        )
        np.testing.assert_allclose(small["bounds_wgs84"], large["bounds_wgs84"], atol=1e-5)

    def test_admission_limits_missing_huc_and_no_flood_fail_explicitly(self):
        reach = [{"HUC": "12090301", "ReachID": "11", "Streamflow": 15}]
        with self.assertRaisesRegex(stream.StreamingInputError, "source blocks"):
            self.run_local(reach, max_blocks=1)
        with self.assertRaisesRegex(stream.StreamingInputError, "output tiles"):
            self.run_local(reach, max_tiles=0)
        with self.assertRaisesRegex(stream.StreamingInputError, "not in the prepared"):
            self.run_local([{"HUC": "01010001", "ReachID": "1", "Streamflow": 1}])
        with self.assertRaisesRegex(stream.StreamingInputError, "no flooded cells"):
            self.run_local([{"HUC": "12090301", "ReachID": "13", "Streamflow": 0}])

    def test_remote_reads_use_ranges_and_fetch_only_needed_blocks(self):
        root = self.dataset.resolve()
        ranges = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def __body(self):
                path = (root / self.path.lstrip("/")).resolve()
                return path.read_bytes() if path.is_relative_to(root) and path.is_file() else None

            def do_HEAD(self):
                body = self.__body()
                if body is None:
                    self.send_error(404)
                    return
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Accept-Ranges", "bytes")
                self.end_headers()

            def do_GET(self):
                body = self.__body()
                value = self.headers.get("Range")
                if body is None or not value:
                    self.send_error(404 if body is None else 400)
                    return
                start, end = value.removeprefix("bytes=").split("-")
                start, end = int(start), min(int(end), len(body) - 1)
                ranges.append((start, end))
                self.send_response(206)
                self.send_header("Content-Range", f"bytes {start}-{end}/{len(body)}")
                self.send_header("Content-Length", str(end - start + 1))
                self.end_headers()
                self.wfile.write(body[start:end + 1])

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base = f"/vsicurl/http://127.0.0.1:{server.server_port}"
            reaches = [{"HUC": "12090301", "ReachID": "14", "Streamflow": 30}]
            with rasterio.Env(**stream.GDAL_STREAMING_OPTIONS):
                manifest = stream.run_scenario(
                    {"ScenarioID": "remote", "Reaches": reaches}, self.read_json,
                    lambda key: f"{base}/{key}", lambda name, data: name, log=lambda _: None,
                )
        finally:
            server.shutdown()
            server.server_close()
            thread.join()
        self.assertTrue(manifest["tiles"])
        total = sum(path.stat().st_size for path in root.rglob("*.tif"))
        self.assertLess(sum(end - start + 1 for start, end in ranges), total // 4)


class SharedHydroIdTests(unittest.TestCase):
    def test_preparation_and_planning_match_noaa_for_shared_hydroid(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            huc_dir, _, source_rows = build_hydrofabric(root)
            rows = []
            for row in source_rows:
                rows.append(row)
                if row["HydroID"] == 1000:
                    rows.append({**row, "feature_id": "15", "stage": row["stage"] + 1})
            with (huc_dir / "hydrotable.csv").open("w", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
                writer.writeheader()
                writer.writerows(rows)
            output = root / "prepared"
            prepare.prepare_huc("12090301", huc_dir, output, block_size=128, log=lambda _: None)

            def read_json(key):
                path = output / key
                return json.loads(path.read_text()) if path.is_file() else None

            for forecasts in ({"11": 10}, {"15": 20}, {"11": 10, "15": 20}, {"15": 20, "11": 10}):
                with self.subTest(forecasts=forecasts):
                    _, plans, missing = stream.plan_huc("12090301", forecasts, read_json)
                    selected = [
                        row for row in rows if row["HydroID"] == 1000 and row["feature_id"] in forecasts
                    ]
                    # NOAA joins forecasts, groups by HydroID in table order, and takes the first stage.
                    expected = np.float32(round(np.interp(
                        [forecasts[selected[0]["feature_id"]]],
                        [row["discharge_cms"] for row in selected],
                        [row["stage"] for row in selected],
                    )[0], 4))
                    self.assertFalse(missing)
                    self.assertEqual(next(plan for plan in plans if plan.branch == "0").stages[1000], expected)
            reach = read_json("hucs/12090301/reaches/15.json")
            self.assertTrue(reach["branches"][0]["blocks"])
            self.assertIn("row_order", reach["branches"][0]["hydroids"][0])
            for feature in ("11", "15"):
                path = output / "hucs" / "12090301" / "reaches" / f"{feature}.json"
                document = json.loads(path.read_text())
                for entry in document["branches"]:
                    for rating in entry["hydroids"]:
                        rating.pop("row_order", None)
                path.write_text(json.dumps(document))
            with self.assertRaisesRegex(stream.StreamingInputError, "needs row order"):
                stream.plan_huc("12090301", {"11": 10, "15": 20}, read_json)


class FakeS3:
    def __init__(self, keys):
        self.keys = keys
        self.downloads = []

    def get_paginator(self, name):
        assert name == "list_objects_v2"
        keys = self.keys

        class Paginator:
            def paginate(self, Bucket, Prefix):
                yield {"Contents": [{"Key": Prefix + key} for key in keys]}

        return Paginator()

    def download_file(self, bucket, key, filename):
        self.downloads.append((bucket, key))
        Path(filename).write_text(key)


class DownloadHucTests(unittest.TestCase):
    KEYS = [
        "branch_ids.csv", "hydrotable.csv", "wbd.gpkg",
        "branches/0/rem_zeroed_masked_0.tif",
        "branches/0/gw_catchments_reaches_filtered_addedAttributes_0.tif",
        "branches/0/gw_catchments_reaches_filtered_addedAttributes_0.gpkg",
        "branches/0/hydroTable_0.csv", "branches/0/src_full_crosswalked_0.csv",
        "branches/7/rem_zeroed_masked_7.tif", "branches/7/rem_zeroed_masked_8.tif",
    ]

    def test_downloads_only_inputs_and_resumes(self):
        with tempfile.TemporaryDirectory() as directory:
            huc_dir = Path(directory) / "flood_12090301" / "12090301"
            (huc_dir / "branches" / "7").mkdir(parents=True)
            (huc_dir / "branches" / "7" / "rem_zeroed_masked_7.tif").write_text("existing")
            client = FakeS3(self.KEYS)
            prepare.download_huc("12090301", huc_dir, "4.5.2.11", client=client, log=lambda _: None)
            prefix = "hand_fim_4_5_2_11/12090301/"
            self.assertEqual(client.downloads, [(prepare.SOURCE_BUCKET, prefix + key) for key in (
                "branch_ids.csv", "hydrotable.csv", "branches/0/rem_zeroed_masked_0.tif",
                "branches/0/gw_catchments_reaches_filtered_addedAttributes_0.tif",
            )])
            self.assertEqual((huc_dir / "branches" / "7" / "rem_zeroed_masked_7.tif").read_text(), "existing")
            self.assertFalse(list(huc_dir.rglob("*.part")))

            client = FakeS3(self.KEYS)
            prepare.download_huc("12090301", huc_dir, "4.5.2.11", client=client, log=lambda _: None)
            self.assertEqual(client.downloads, [])

    def test_rejects_missing_source_and_bad_version(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(prepare.PreparationError):
                prepare.download_huc("12090301", Path(directory), client=FakeS3(["wbd.gpkg"]), log=lambda _: None)
            with self.assertRaises(prepare.PreparationError):
                prepare.download_huc("12090301", Path(directory), "../x", client=FakeS3([]), log=lambda _: None)


if __name__ == "__main__":
    unittest.main()
