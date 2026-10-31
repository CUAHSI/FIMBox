"""Windowed reads of job rasters; S3 credentials stay inside the API."""

import json
import logging
import math
import threading
from collections import OrderedDict
from collections.abc import Callable
from typing import Any

import morecantile
import numpy as np
import rasterio
from botocore.exceptions import ClientError
from rasterio.errors import RasterioError
from rio_tiler.errors import RioTilerError, TileOutsideBounds
from rio_tiler.io import Reader
from rio_tiler.models import ImageData

from .models import RasterInfo, ScenarioManifest
from .storage import RasterSource

logger = logging.getLogger(__name__)
COLORMAP = {0: (0, 0, 0, 0), 1: (0, 120, 255, 190)}
# Parity diffs: 1=base/legacy-only flooding, 2=candidate/streaming-only flooding.
DIFF_COLORMAP = {0: (0, 0, 0, 0), 1: (220, 40, 40, 210), 2: (255, 160, 0, 210)}
DIFF_SUFFIX = ".diff.tif"
EARTH_CIRCUMFERENCE = 40075016.686
MAX_MANIFEST_BYTES = 8 * 1024 * 1024
MANIFEST_CACHE_SIZE = 32
__manifest_cache: OrderedDict[tuple[str, str], tuple[str, list[tuple[str, tuple]]]] = OrderedDict()
__manifest_lock = threading.Lock()


class RasterReadError(RuntimeError):
    """A sanitized raster read failure."""


def raster_info(source: RasterSource) -> RasterInfo:
    try:
        with rasterio.Env(
            session=source.session,
            GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR", GDAL_HTTP_TIMEOUT=20,
            GDAL_HTTP_MAX_RETRY=0, VSI_CACHE=False, **source.options,
        ), Reader(source.path) as reader:
            return RasterInfo(
                bounds=reader.get_geographic_bounds("EPSG:4326"),
                minzoom=max(0, min(22, reader.minzoom)),
                maxzoom=max(0, min(22, reader.maxzoom)),
            )
    except (RasterioError, RioTilerError):
        logger.error("Could not read job raster metadata")
        raise RasterReadError("Could not read job raster metadata from S3.") from None


def raster_tile(source: RasterSource, z: int, x: int, y: int) -> bytes:
    try:
        with rasterio.Env(
            session=source.session,
            GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR", GDAL_HTTP_TIMEOUT=20,
            GDAL_HTTP_MAX_RETRY=0, VSI_CACHE=False, **source.options,
        ), Reader(source.path) as reader:
            image = reader.tile(x, y, z, tilesize=256, indexes=(1,))
            # HAND-FIM outputs are binary: show flooded cells, not dry land/nodata.
            colormap = DIFF_COLORMAP if source.path.lower().endswith(DIFF_SUFFIX) else COLORMAP
            return image.render(img_format="PNG", colormap=colormap)
    except TileOutsideBounds:
        return ImageData(np.ma.masked_all((1, 256, 256), dtype="uint8")).render(
            img_format="PNG", colormap={0: (0, 0, 0, 0)},
        )
    except (RasterioError, RioTilerError):
        logger.error("Could not read job raster tile")
        raise RasterReadError("Could not read job raster tile from S3.") from None


class TooManyTilesError(ValueError):
    """A map tile would need more scenario tiles than the configured limit."""


def __transparent_png() -> bytes:
    return ImageData(np.ma.masked_all((1, 256, 256), dtype="uint8")).render(
        img_format="PNG", colormap={0: (0, 0, 0, 0)},
    )


def scenario_info(manifest: ScenarioManifest, max_tiles: int) -> RasterInfo:
    """Bounds and the zoom range at which a map tile touches at most ``max_tiles`` scenario tiles."""
    span = max(1, math.isqrt(max_tiles) - 1) * manifest.tile_size * manifest.resolution
    minzoom = max(0, min(22, math.ceil(math.log2(EARTH_CIRCUMFERENCE / span)) + 1))
    native = math.ceil(math.log2(EARTH_CIRCUMFERENCE / (256 * manifest.resolution)))
    return RasterInfo(bounds=manifest.bounds, minzoom=minzoom, maxzoom=max(minzoom, min(22, native)))


def stored_scenario_manifest(client: Any, bucket: str, key: str) -> ScenarioManifest:
    """Recover scenario metadata from its retained manifest instead of the job result."""
    response = client.get_object(Bucket=bucket, Key=key)
    with response["Body"] as body:
        content = body.read(MAX_MANIFEST_BYTES + 1)
    if len(content) > MAX_MANIFEST_BYTES:
        raise RasterReadError("Scenario tile manifest is too large.")
    try:
        document = json.loads(content)
        if document["format"] != "fimbox-scenario-tiles" or document["version"] != 1:
            raise ValueError("Unsupported scenario manifest")
        manifest = ScenarioManifest(
            ScenarioID=document["scenario_id"], key=key, tile_count=len(document["tiles"]),
            bounds=document["bounds_wgs84"], tile_size=document["tile_size"],
            resolution=min(float(huc["resolution"]) for huc in document["hucs"].values()),
        )
        if not math.isfinite(manifest.resolution) or not all(math.isfinite(value) for value in manifest.bounds):
            raise ValueError("Nonfinite scenario metadata")
        return manifest
    except (ValueError, KeyError, TypeError, AttributeError):
        raise RasterReadError("Scenario tile manifest is invalid.") from None


def load_scenario_tiles(client: Any, bucket: str, manifest: ScenarioManifest) -> list[tuple[str, tuple]]:
    """
    Loads a published manifest's tile keys and WGS84 bounds, revalidating cached copies by ETag.

    Only COG keys under the manifest's scenario directory are returned.
    """
    cache_key = (bucket, manifest.key)
    with __manifest_lock:
        cached = __manifest_cache.get(cache_key)
    request = {"Bucket": bucket, "Key": manifest.key}
    if cached is not None:
        request["IfNoneMatch"] = cached[0]
    try:
        response = client.get_object(**request)
    except ClientError as error:
        if cached is not None and error.response.get("Error", {}).get("Code") in {"304", "NotModified"}:
            return cached[1]
        raise
    with response["Body"] as body:
        content = body.read(MAX_MANIFEST_BYTES + 1)
    if len(content) > MAX_MANIFEST_BYTES:
        raise RasterReadError("Scenario tile manifest is too large.")
    try:
        document = json.loads(content)
        root = manifest.key.rsplit("/", 1)[0] + "/" if "/" in manifest.key else ""
        tiles = [
            (tile["key"], tuple(float(value) for value in tile["bounds_wgs84"]))
            for tile in document["tiles"]
        ]
    except (ValueError, KeyError, TypeError):
        raise RasterReadError("Scenario tile manifest is invalid.") from None
    if document.get("scenario_id") != manifest.ScenarioID or any(
        not key.startswith(root) or not key.endswith(".cog")
        or ".." in key.split("/") or len(bounds) != 4 for key, bounds in tiles
    ):
        raise RasterReadError("Scenario tile manifest is invalid.")
    with __manifest_lock:
        __manifest_cache[cache_key] = (response.get("ETag", ""), tiles)
        __manifest_cache.move_to_end(cache_key)
        while len(__manifest_cache) > MANIFEST_CACHE_SIZE:
            __manifest_cache.popitem(last=False)
    return tiles


def scenario_tile(
    tiles: list[tuple[str, tuple]], z: int, x: int, y: int, max_tiles: int,
    source_for: Callable[[str], RasterSource],
) -> bytes:
    """Renders the flood union of every scenario tile intersecting a web map tile."""
    west, south, east, north = morecantile.tms.get("WebMercatorQuad").bounds(x, y, z)
    keys = [
        key for key, (left, bottom, right, top) in tiles
        if left < east and right > west and bottom < north and top > south
    ]
    if not keys:
        return __transparent_png()
    if len(keys) > max_tiles:
        raise TooManyTilesError(f"Zoom in: this map tile covers more than {max_tiles} scenario tiles")
    flooded = np.zeros((256, 256), dtype=bool)
    try:
        for key in keys:
            source = source_for(key)
            with rasterio.Env(
                session=source.session,
                GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR", GDAL_HTTP_TIMEOUT=20,
                GDAL_HTTP_MAX_RETRY=0, VSI_CACHE=False, **source.options,
            ), Reader(source.path) as reader:
                try:
                    image = reader.tile(x, y, z, tilesize=256, indexes=(1,))
                except TileOutsideBounds:
                    continue
            data = image.array[0]
            flooded |= ~np.ma.getmaskarray(data) & (np.ma.getdata(data) == 1)
    except (RasterioError, RioTilerError):
        logger.error("Could not read scenario raster tile")
        raise RasterReadError("Could not read scenario raster tile from S3.") from None
    if not flooded.any():
        return __transparent_png()
    return ImageData(np.ma.array(flooded.astype("uint8")[None], mask=~flooded[None])).render(
        img_format="PNG", colormap=COLORMAP,
    )
