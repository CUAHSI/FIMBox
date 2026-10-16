"""Windowed reads of job rasters; S3 credentials stay inside the API."""

import logging

import numpy as np
import rasterio
from rasterio.errors import RasterioError
from rio_tiler.errors import RioTilerError, TileOutsideBounds
from rio_tiler.io import Reader
from rio_tiler.models import ImageData

from .models import RasterInfo
from .storage import RasterSource

logger = logging.getLogger(__name__)


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
            return image.render(
                img_format="PNG", colormap={0: (0, 0, 0, 0), 1: (0, 120, 255, 190)},
            )
    except TileOutsideBounds:
        return ImageData(np.ma.masked_all((1, 256, 256), dtype="uint8")).render(
            img_format="PNG", colormap={0: (0, 0, 0, 0)},
        )
    except (RasterioError, RioTilerError):
        logger.error("Could not read job raster tile")
        raise RasterReadError("Could not read job raster tile from S3.") from None
