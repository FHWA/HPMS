# core/rat_dem_relief.py

# This software was developed by the Federal Highway Administration (FHWA),
# an agency of the U.S. Department of Transportation (U.S. DOT).
#
# To the extent possible under law, the Federal Highway Administration (FHWA)
# has waived all copyright and related or neighboring rights to this software.
#
# This software is dedicated to the public domain under the CC0 1.0 Universal
# Public Domain Dedication. You can copy, modify, distribute, and perform the
# work, even for commercial purposes, all without asking permission.
#
# For more information, please see the LICENSE file or visit:
# https://creativecommons.org/publicdomain/zero/1.0/

"""
RAT DEM SHADED RELIEF
--------------------------------------------------------------------------------
Reads the cached USGS 3DEP 1 m tiles (tile_1m_hr*.tif, EPSG:4326, elevation
in meters) and draws them as hillshaded, elevation-tinted relief. Used by the
structure span report and the plan/profile PDF. Never downloads tiles; areas
without a cached tile are left blank.

CREATED BY: Federal Highway Administration, Office of Highway Policy Information.
VERSION: 3.3, October 29, 2026
"""
import os
import math
from typing import Optional, Tuple

import numpy as np

FEET_PER_METER = 3.28084
M_PER_DEG_LAT = 111320.0


def read_dem_window(dem_dir: str, min_lon: float, min_lat: float, max_lon: float, max_lat: float,
                    max_px: Optional[int] = None):
    """
    Mosaics the cached tiles covering a lon/lat box. With max_px, tiles are
    read at reduced resolution so the longer side of the result is about
    max_px pixels (keeps long routes within memory). Returns
    (elev_m, (min_lon, max_lon, min_lat, max_lat)) or None if no covering
    tile is cached.
    """
    import rasterio
    from rasterio.merge import merge
    from rasterio.enums import Resampling
    from shapely.geometry import box
    from core.rat_core import get_needed_dem_cells

    paths = []
    for lon_idx, lat_idx in sorted(get_needed_dem_cells([box(min_lon, min_lat, max_lon, max_lat)])):
        p = os.path.join(dem_dir, f"tile_1m_hr{lat_idx}_{lon_idx}.tif")
        if os.path.exists(p):
            paths.append(p)
    if not paths:
        return None

    sources = [rasterio.open(p) for p in paths]
    try:
        kwargs = {}
        if max_px:
            native = abs(sources[0].res[0])
            target = max(max_lon - min_lon, max_lat - min_lat) / float(max_px)
            if target > native:
                kwargs["res"] = (target, target)
                kwargs["resampling"] = Resampling.bilinear
        mosaic, transform = merge(sources, bounds=(min_lon, min_lat, max_lon, max_lat),
                                  nodata=np.nan, **kwargs)
    finally:
        for s in sources:
            s.close()

    elev = mosaic[0].astype(float)
    elev[elev < -1000] = np.nan
    if not np.isfinite(elev).any():
        return None
    h, w = elev.shape
    extent = (transform.c, transform.c + w * transform.a, transform.f + h * transform.e, transform.f)
    return elev, extent


def tiles_cover_route(dem_dir: str, lons: np.ndarray, lats: np.ndarray) -> bool:
    """True if at least one cached tile touches the route."""
    from shapely.geometry import LineString
    from core.rat_core import get_needed_dem_cells
    if dem_dir is None or len(lons) < 2:
        return False
    for lon_idx, lat_idx in get_needed_dem_cells([LineString(zip(lons, lats))]):
        if os.path.exists(os.path.join(dem_dir, f"tile_1m_hr{lat_idx}_{lon_idx}.tif")):
            return True
    return False


def hillshade_rgb(elev_ft: np.ndarray, dx_m: float, dy_m: float, cmap: str = "terrain",
                  vert_exag: float = 2.0, clip_pct: Tuple[float, float] = (1, 99)):
    """
    RGB image of elevation (feet) tinted by height and shaded with light from
    the upper left. Returns (rgba, ScalarMappable for a colorbar). Cells
    with no data come back transparent.
    """
    import matplotlib.pyplot as plt
    from matplotlib.colors import LightSource, Normalize

    finite = np.isfinite(elev_ft)
    lo, hi = np.nanpercentile(elev_ft, clip_pct)
    if hi - lo < 1.0:
        lo, hi = lo - 0.5, hi + 0.5
    norm = Normalize(vmin=lo, vmax=hi)
    z = np.where(finite, elev_ft, np.nanmedian(elev_ft))
    ls = LightSource(azdeg=315, altdeg=45)
    rgb = ls.shade(z, cmap=plt.get_cmap(cmap), norm=norm, blend_mode="soft",
                   vert_exag=vert_exag, dx=dx_m, dy=dy_m)
    rgba = np.dstack([rgb[..., :3], np.where(finite, 1.0, 0.0)])
    return rgba, plt.cm.ScalarMappable(norm=norm, cmap=cmap)


def draw_relief(ax, elev_m: np.ndarray, extent: tuple, cmap: str = "terrain"):
    """Draws lon/lat relief on ax with a ground-true aspect ratio. Returns
    a ScalarMappable (feet) for a colorbar."""
    min_lon, max_lon, min_lat, max_lat = extent
    mid_lat = (min_lat + max_lat) / 2
    h, w = elev_m.shape
    k = math.cos(math.radians(mid_lat))
    dx = (max_lon - min_lon) / w * M_PER_DEG_LAT * k
    dy = (max_lat - min_lat) / h * M_PER_DEG_LAT
    rgba, mappable = hillshade_rgb(elev_m * FEET_PER_METER, dx, dy, cmap=cmap)
    ax.imshow(rgba, extent=(min_lon, max_lon, min_lat, max_lat), origin="upper",
              interpolation="bilinear", zorder=0)
    ax.set_aspect(1.0 / max(k, 0.15))
    return mappable


def padded_lonlat_box(lons: np.ndarray, lats: np.ndarray, pad_frac: float = 0.05,
                      min_half_m: float = 250.0):
    """Lon/lat box around points, padded, and at least 2*min_half_m across."""
    mid_lat = float(np.mean(lats))
    k = max(math.cos(math.radians(mid_lat)), 0.15)
    w_m = (lons.max() - lons.min()) * M_PER_DEG_LAT * k
    h_m = (lats.max() - lats.min()) * M_PER_DEG_LAT
    half_w = max(w_m / 2 * (1 + 2 * pad_frac), min_half_m)
    half_h = max(h_m / 2 * (1 + 2 * pad_frac), min_half_m)
    clon, clat = (lons.max() + lons.min()) / 2, (lats.max() + lats.min()) / 2
    dlon, dlat = half_w / (M_PER_DEG_LAT * k), half_h / M_PER_DEG_LAT
    return clon - dlon, clat - dlat, clon + dlon, clat + dlat


def cached_tiles_bounds(dem_dir: str, lons: np.ndarray, lats: np.ndarray):
    """Lon/lat bounds of all cached tiles the route touches, or None."""
    from shapely.geometry import LineString
    from core.rat_core import get_needed_dem_cells, hires_cell_bounds
    boxes = []
    for lon_idx, lat_idx in get_needed_dem_cells([LineString(zip(lons, lats))]):
        if os.path.exists(os.path.join(dem_dir, f"tile_1m_hr{lat_idx}_{lon_idx}.tif")):
            boxes.append(hires_cell_bounds(lon_idx, lat_idx))
    if not boxes:
        return None
    b = np.array(boxes)
    return b[:, 0].min(), b[:, 1].min(), b[:, 2].max(), b[:, 3].max()


def fit_box_to_aspect(box, target_w_over_h: float, limit=None):
    """
    Grows a lon/lat box (min_lon, min_lat, max_lon, max_lat) about its center
    toward a ground width/height ratio, so a map fills its page. With limit,
    the box never grows past those bounds (e.g. the cached tiles).
    """
    min_lon, min_lat, max_lon, max_lat = box
    k = max(math.cos(math.radians((min_lat + max_lat) / 2)), 0.15)
    w_m = (max_lon - min_lon) * M_PER_DEG_LAT * k
    h_m = (max_lat - min_lat) * M_PER_DEG_LAT
    clon, clat = (min_lon + max_lon) / 2, (min_lat + max_lat) / 2
    if w_m / h_m > target_w_over_h:
        h_m = w_m / target_w_over_h
    else:
        w_m = h_m * target_w_over_h
    dlon, dlat = w_m / 2 / (M_PER_DEG_LAT * k), h_m / 2 / M_PER_DEG_LAT
    out = [clon - dlon, clat - dlat, clon + dlon, clat + dlat]
    if limit is not None:
        out = [max(out[0], min(limit[0], min_lon)), max(out[1], min(limit[1], min_lat)),
               min(out[2], max(limit[2], max_lon)), min(out[3], max(limit[3], max_lat))]
    return tuple(out)