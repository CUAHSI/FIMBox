"""Notebook raster maps with authenticated, kernel-side SDK tile requests."""

import base64
import math
from collections.abc import Sequence
from functools import lru_cache
from typing import TYPE_CHECKING
from uuid import UUID

import httpx

from fimbox_sdk import AuthenticatedClient, Client
from fimbox_sdk.api.default import (
    get_raster_info_jobs_job_id_rasters_file_index_info_get as info_operation,
    get_raster_tile_jobs_job_id_rasters_file_index_tiles_z_x_y_png_get as tile_operation,
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
        if (x1 - x0 + 1) * (y1 - y0 + 1) <= 64 or zoom == 0:
            return [(zoom, x, y) for x in range(x0, x1 + 1) for y in range(y0, y1 + 1)]
        zoom -= 1


def job_map(
    client: Client | AuthenticatedClient, job_id: str | UUID, result: StoredHandFimResult,
) -> "VBox":
    """Return an ipyleaflet widget for retained HAND-FIM COG/TIFF outputs.

    Arguments:
        client: Connected generated SDK client. Credentials stay in the kernel.
        job_id: Successful job owned by the current session.
        result: That job's output manifest.

    Returns:
        A widget with a raster selector, map, reload button, and tile-read status.
    """
    from ipyleaflet import ImageOverlay, Map, basemaps
    from ipywidgets import Button, Dropdown, Label, Layout, VBox

    job_id = UUID(str(job_id))
    if not result.output_bucket:
        raise ValueError("Map viewing requires retained S3 outputs.")
    files = [
        (artifact.path, index) for index, artifact in enumerate(result.files)
        if artifact.path.lower().endswith((".cog", ".tif", ".tiff")) and artifact.key
    ]
    if not files:
        raise ValueError("This job has no retained COG/TIFF outputs.")
    # Prefer a COG over the original TIFF when both are in the manifest.
    files.sort(key=lambda item: (not item[0].lower().endswith(".cog"), item[0]))
    selector = Dropdown(options=files, description="Raster:", layout=Layout(width="95%"))
    status = Label()
    reload_button = Button(description="Reload tiles", tooltip="Retry after a read error or renewed login")
    map_widget = Map(
        basemap=basemaps.OpenStreetMap.Mapnik, scroll_wheel_zoom=True,
        layout=Layout(height="600px"), max_zoom=22,
    )
    base_layers = map_widget.layers
    metadata: RasterInfo | None = None
    changing = False

    @lru_cache(maxsize=128)
    def tile(index: int, z: int, x: int, y: int) -> str:
        response = tile_operation.sync_detailed(
            client=client, job_id=job_id, file_index=index, z=z, x=x, y=y,
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
            coordinates = __visible_tiles(
                map_widget.bounds, max(0, int(map_widget.zoom)), metadata.bounds,
            )
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
            response = info_operation.sync_detailed(
                client=client, job_id=job_id, file_index=selector.value,
            )
            if response.status_code != 200 or not isinstance(response.parsed, RasterInfo):
                raise RuntimeError(f"Raster metadata request failed: HTTP {response.status_code}.")
            metadata = response.parsed
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

    select()
    selector.observe(select_changed, names="value")
    map_widget.observe(render, names="bounds")
    reload_button.on_click(reload_tiles)
    return VBox([selector, reload_button, status, map_widget])
