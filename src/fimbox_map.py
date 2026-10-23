"""Notebook raster maps with authenticated, kernel-side SDK tile requests."""

import base64
import math
from collections.abc import Sequence
from functools import lru_cache
from typing import TYPE_CHECKING
from uuid import UUID
from urllib.parse import urlsplit

import httpx

from fimbox_sdk import AuthenticatedClient, Client
from fimbox_sdk.api.default import (
    get_output_info_outputs_info_get as info_operation,
    get_output_tile_outputs_tiles_z_x_y_png_get as tile_operation,
)
from fimbox_sdk.models import RasterInfo, StoredHandFimResult

if TYPE_CHECKING:
    from ipywidgets import VBox


def __latitude(y: int, count: int) -> float:
    return math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * y / count))))


def __tile_bounds(z: int, x: int, y: int) -> tuple[tuple[float, float], tuple[float, float]]:
    count = 2 ** z
    return (
        (__latitude(y + 1, count), x / count * 360 - 180),
        (__latitude(y, count), (x + 1) / count * 360 - 180),
    )


def __visible_tiles(
    bounds: Sequence[Sequence[float]],
    zoom: int,
    raster_bounds: Sequence[float],
    minzoom: int = 0,
) -> list[tuple[int, int, int]]:
    (south, west), (north, east) = bounds
    left, bottom, right, top = raster_bounds
    west, south = max(west, left, -180), max(south, bottom, -85.05112878)
    east, north = min(east, right, 180), min(north, top, 85.05112878)
    if west >= east or south >= north:
        return []
    while True:
        count = 2 ** zoom
        x0 = max(0, int((west + 180) / 360 * count))
        x1 = min(count - 1, math.ceil((east + 180) / 360 * count) - 1)

        def row(latitude: float) -> float:
            return (1 - math.asinh(math.tan(math.radians(latitude))) / math.pi) / 2 * count

        y0 = max(0, int(row(north)))
        y1 = min(count - 1, math.ceil(row(south)) - 1)
        if (x1 - x0 + 1) * (y1 - y0 + 1) <= 64 or zoom <= minzoom:
            return [(zoom, x, y) for x in range(x0, x1 + 1) for y in range(y0, y1 + 1)]
        zoom -= 1


def job_map(
    client: Client | AuthenticatedClient, job_id: str | UUID, result: StoredHandFimResult,
) -> "VBox":
    """Compatibility wrapper for mapping a saved result; job_id is no longer looked up."""
    if not result.output_bucket:
        raise ValueError("Map viewing requires retained S3 outputs.")
    paths = [
        f"s3://{result.output_bucket}/{artifact.key}" for artifact in result.files
        if artifact.path.lower().endswith((".cog", ".tif", ".tiff")) and artifact.key
    ]
    paths.sort(key=lambda path: (not path.lower().endswith(".cog"), path))
    manifests = result.scenario_manifests if isinstance(result.scenario_manifests, list) else []
    paths.extend(f"s3://{result.output_bucket}/{manifest.key}" for manifest in manifests)
    return output_map(client, paths)


def output_map(client: Client | AuthenticatedClient, paths: Sequence[str]) -> "VBox":
    """Return an ipyleaflet widget for retained S3 raster or scenario manifest paths.

    Arguments:
        client: Connected generated SDK client. Credentials stay in the kernel.
        paths: s3://bucket/key paths to COG/TIFF files or scenario manifest.json files.
            No job ID or saved job result is required.

    Returns:
        A widget with a raster selector, map, reload button, auto-zoom toggle, and tile-read status.
    """
    from ipyleaflet import ImageOverlay, Map, basemaps
    from ipywidgets import Button, Checkbox, Dropdown, HBox, Label, Layout, VBox

    sources = []
    files = []
    for path in paths:
        parsed = urlsplit(path)
        key = parsed.path.removeprefix("/")
        if parsed.scheme != "s3" or not parsed.netloc or parsed.username or parsed.password \
                or parsed.query or parsed.fragment or not key \
                or any(part in {"", ".", ".."} for part in key.split("/")):
            raise ValueError("Map paths must be s3://bucket/key object paths without credentials or queries.")
        streamed = key.endswith("/manifest.json")
        if not streamed and not key.lower().endswith((".cog", ".tif", ".tiff")):
            raise ValueError("Map paths must reference COG/TIFF files or scenario manifest.json files.")
        sources.append((parsed.netloc, key))
        files.append((path, ("scenario" if streamed else "file", len(sources) - 1)))
    if not files:
        raise ValueError("Provide at least one retained COG/TIFF or scenario manifest path.")
    selector = Dropdown(options=files, description="Raster:", layout=Layout(width="95%"))
    status = Label()
    reload_button = Button(description="Reload tiles", tooltip="Retry after a read error or renewed login")
    auto_zoom = Checkbox(value=True, description="Auto-zoom")
    map_widget = Map(
        basemap=basemaps.OpenStreetMap.Mapnik, scroll_wheel_zoom=True,
        layout=Layout(height="600px"), max_zoom=22,
    )
    base_layers = map_widget.layers
    metadata: RasterInfo | None = None
    changing = False

    @lru_cache(maxsize=128)
    def tile(source: tuple[str, int], z: int, x: int, y: int) -> str:
        _kind, index = source
        bucket, key = sources[index]
        response = tile_operation.sync_detailed(
            client=client, bucket=bucket, key=key, z=z, x=x, y=y,
        )
        if response.status_code != 200:
            raise RuntimeError(f"Raster tile request failed: HTTP {response.status_code}.")
        if not response.content.startswith(b"\x89PNG\r\n\x1a\n"):
            raise RuntimeError("Raster tile response is not a PNG.")
        return "data:image/png;base64," + base64.b64encode(response.content).decode("ascii")

    def render(_change=None):
        if changing or metadata is None or not map_widget.bounds:
            return
        status.value = "Loading visible inundation tiles..."
        try:
            zoom = max(0, int(map_widget.zoom))
            # Streamed scenarios are composited from many tile COGs, so the API bounds low zooms.
            minzoom = metadata.minzoom if selector.value[0] == "scenario" else 0
            if zoom < minzoom:
                map_widget.layers = base_layers
                status.value = f"Zoom in to level {minzoom} or closer to view inundation tiles."
                return
            coordinates = __visible_tiles(map_widget.bounds, zoom, metadata.bounds, minzoom)
            overlays = [
                ImageOverlay(url=tile(selector.value, z, x, y), bounds=__tile_bounds(z, x, y))
                for z, x, y in coordinates
            ]
            map_widget.layers = (*base_layers, *overlays)
            status.value = f"{len(overlays)} visible tiles. Blue = inundation; dry/nodata = transparent."
        except (httpx.RequestError, RuntimeError, ValueError, KeyError, TypeError) as error:
            map_widget.layers = base_layers
            detail = str(error) if isinstance(error, RuntimeError) else type(error).__name__
            status.value = f"Map tile read failed: {detail} Check the API/session, then reload tiles."

    def select(_change=None):
        nonlocal metadata, changing
        changing = True
        metadata = None
        map_widget.layers = base_layers
        try:
            kind, index = selector.value
            bucket, key = sources[index]
            response = info_operation.sync_detailed(client=client, bucket=bucket, key=key)
            if response.status_code != 200 or not isinstance(response.parsed, RasterInfo):
                raise RuntimeError(f"Raster metadata request failed: HTTP {response.status_code}.")
            metadata = response.parsed
            map_widget.min_zoom = metadata.minzoom if kind == "scenario" and auto_zoom.value else 0
            if auto_zoom.value:
                if map_widget.zoom < map_widget.min_zoom:
                    map_widget.zoom = map_widget.min_zoom
                west, south, east, north = metadata.bounds
                map_widget.fit_bounds(((south, west), (north, east)))
        finally:
            changing = False
        render()

    def select_changed(change):
        try:
            select(change)
        except (httpx.RequestError, RuntimeError, ValueError, KeyError, TypeError) as error:
            detail = str(error) if isinstance(error, RuntimeError) else type(error).__name__
            status.value = f"Raster selection failed: {detail} Check the API/session, then reload tiles."

    def reload_tiles(_button):
        tile.cache_clear()
        select_changed(None)

    def auto_zoom_changed(change):
        if change["new"]:
            select_changed(None)
        else:
            map_widget.min_zoom = 0
            render()

    select()
    selector.observe(select_changed, names="value")
    map_widget.observe(render, names=["bounds", "zoom"])
    reload_button.on_click(reload_tiles)
    auto_zoom.observe(auto_zoom_changed, names="value")
    return VBox([selector, HBox([reload_button, auto_zoom]), status, map_widget])
