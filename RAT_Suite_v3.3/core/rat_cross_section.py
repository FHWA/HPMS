# core/rat_cross_section.py

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
RAT CROSS SECTIONS
--------------------------------------------------------------------------------
Cuts cross sections across the route from the cached USGS 3DEP 1 m DEM tiles
and measures what the bare-earth surface shows: the cross slope of each
roadway's travel lanes, superelevation through each horizontal curve, and the
foreslopes. Offsets are + to the right of the inventory direction.

Travel lanes. The HPMS line is taken to run down the middle of the roadway it
follows: the inventory roadway of a divided highway, or the whole street where
HPMS shows a two-way facility (Facility_Type 2) with _TWO_LANE_MAX_LANES or
fewer Through_Lanes (one lane each way), no median, or a median narrower than
_WIDE_MEDIAN_FT (no median search is made there). Otherwise, on freeways and
Interstates (F_System 1 or 2) the line is always taken to follow the
inventory roadway, even beside a narrow barrier median. The lanes span the lane half-width either side of it:
lanes x lane width / 2, from HPMS Dir_Through_Lanes (or Through_Lanes / 2 on
divided highways, Through_Lanes on undivided ones) and Lane_Width, defaulting
to 12 ft. Where the DEM does not show that whole width as one plane, two
lanes are fitted instead. A roadway is "Planar" (one cross slope) or
"Crowned" (two slopes falling away from a crown). A facility is treated as
divided, unless it is a two-way, two-lane road as above, where it is a
freeway or Interstate (F_System 1 or 2) or HPMS shows a median of
_WIDE_MEDIAN_FT or more.

Centerline. Tried in order, the first that succeeds is used (where HPMS has
an opposing line, the DEM median is tried first and kept only if the
opposing roadway it finds is centered within _OPP_LINE_AGREE_FT of that
line, since the line is not always on the middle of its roadway):
  HPMS opposing line     where HPMS has a line for the other direction of
                         travel (Facility_Type 6) running alongside the route
                         and crossing the cut 16-320 ft away (nearest on the
                         left, else on the right): the opposing roadway is
                         centered on the pavement the DEM shows around that
                         line (within 15 ft) and the centerline is midway
                         between the two roadways.
  DEM median             a median left of the inventory roadway dipping at
                         least _MEDIAN_MIN_DEPTH_FT below the lanes (or below
                         the extension of their plane where superelevated)
                         and rising back on the far side; centerline midway
                         between the inside pavement edges.
  DEM slopes             for medians too flat to find: midway between the
                         outside pavement edges of the two roadways (both
                         edges must drop away into a foreslope).
  DEM roadways           divided facilities whose roadways are on separate
                         alignments, possibly at different levels: the
                         nearest pavement-like platform left of the inventory
                         lanes, 8-100 ft away and within 60 ft of their level;
                         centerline midway between the inside pavement edges.
  DEM slopes             divided facilities, as above, but either outside
                         edge may meet a cut slope instead of a foreslope.
  HPMS median and lanes  half the HPMS Median_Width plus the lane half-width,
                         left of the HPMS line (medians >= _WIDE_MEDIAN_FT).
  DEM pavement           single roadways: midway between the two pavement
                         edges, when that is 1-15 ft from the HPMS line
                         (the lanes are then fitted about it).
  HPMS line              the HPMS line itself.
The opposing roadway's lanes are reported only where they fit a plane
closely at a plausible cross slope. Opp_Note is "Doubtful" where the
opposing fit may be on side slope rather than pavement: it is not
pavement-like, or steeper than _OPP_DOUBT_SLOPE_PCT where the inventory
lanes are not superelevated alike. Inv_Note marks the inventory lanes the
same way (_inv_doubtful). Where the other direction has its own HPMS line
alongside (paired_with_opposing_line), the HPMS line is taken as its own
direction's lanes even with a narrow median or none reported. In the DEM methods, an opposing
roadway lying on another HPMS line that runs within 45 degrees of the route
(a ramp or frontage road) is rejected. The other lines are those within
NEARBY_SEARCH_FT of the section (select_nearby_lines), matched by location
rather than route name or milepost.

Superelevation. Measured on the inventory roadway's lanes every
XS_SUPER_STEP_FT through each curve (on single roadways, about the pavement
middle), skipping samples on bridge and tunnel spans. If most samples in the
middle half of the curve are crowned or slope toward the outside of the
curve, the curve is reported as "NC" (no superelevation); a curve lying
wholly on a structure is reported as "Structure". Otherwise the plateau is
the median of the middle half; full superelevation runs between the first
and last samples reaching XS_FULL_SUPER_FRAC of it, and the reported value is
the median over that run. Positive values slope down toward the inside of
the curve.

Stations. In each superelevated curve, the start and end of full
superelevation and mid-curve; in each NC curve, its start, middle and end;
the route's first and last points; and on tangents, enough evenly spaced
stations that no two are more than XS_TANGENT_MAX_GAP_FT apart. Stations
falling on a bridge or tunnel are moved just off it (curve stations only if
that keeps them within the curve), and stations at the same spot are merged.

Line off the road. Where two or more stations in a row find no inventory
lanes at the HPMS line, with the centerline either more than
_OFF_ROAD_OFFSET_FT beyond the route's usual offset from the line or placed
from the other roadway (on a divided road whose two HPMS lines both follow
an old alignment, the centerline lands midway between them at the usual
offset), and the ground along the HPMS line there is rough (below), the
line is taken to be off the roadway (Line_Off_Road), typically an old
alignment where the DEM shows new construction. Roughness is the ground's
departure from its own running median over _OFF_ROAD_ROUGH_WINDOW_FT, read
along the HPMS line (the vertices' Elev_Raw_Ft), leaving out bridges and
tunnels backed by an HPMS or NBI/NTI record and
_OFF_ROAD_STRUCTURE_BUFFER_FT either side of them (spans found from the DEM
alone are kept, since off the road the uneven ground reads as such spans). Its median over some
_OFF_ROAD_ROUGH_SPAN_FT stretch of the run (extended halfway to the
neighboring stations) must exceed _OFF_ROAD_ROUGH_FT; pavement stays well
under it. The lane fit alone at a
few stations can fail on a road that is plainly there (a wide median with
the two roadways at different levels), and the ground along the line does
not depend on where the stations fall. Where too little ground is left to
judge (the run lies on or beside a structure), the station test decides
alone.
"""
import math
import os
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
from scipy.ndimage import map_coordinates, uniform_filter1d

FEET_PER_METER = 3.28084

XS_HALF_WIDTH_FT = 250.0        # each section spans this far either side of the roadway centerline
XS_TANGENT_MAX_GAP_FT = 1000.0  # longest allowed distance between stations
XS_SUPER_STEP_FT = 50.0         # superelevation sampling interval through each curve
XS_FULL_SUPER_FRAC = 0.9        # fraction of the plateau that counts as full superelevation
XS_MIN_SUPER_PCT = 1.0          # plateau below this: stations at the curve's quarter points

_STEP_FT = 1.0
_DEFAULT_LANE_WIDTH_FT = 12.0
_DEFAULT_LANES = 2
_WIDE_MEDIAN_FT = 20.0          # HPMS medians narrower than this: the HPMS line is the street's middle
_MEDIAN_SEARCH_FT = 160.0       # farthest a median centerline can sit from the HPMS line
_REACH_FT = XS_HALF_WIDTH_FT + _MEDIAN_SEARCH_FT
_MEDIAN_MIN_DEPTH_FT = 1.0
_MEDIAN_MIN_WIDTH_FT = 8.0      # narrowest median between the two roadways' lanes
_MEDIAN_REFINE_FT = 15.0        # pavement-edge centerline must be this close to the median's
_MEDIAN_RECOVERY_FT = 60.0      # a dip that climbs back to road level within this is part of the median
_LEVEL_TOL_FT = 4.0             # the two roadways' lanes must be within this of each other
_LANE_MAX_SLOPE = 0.08          # steepest local slope allowed within a roadway's lanes
_SECTION_MAX_WIDTH_FT = 230.0   # widest plausible divided roadway, outside edge to outside edge
_EDGE_DROP_FT = 0.3             # ground this far below the lane plane is off the pavement
_EDGE_SEARCH_FT = 45.0
_MAX_SHOULDER_FT = 12.0         # widest shoulder counted as pavement when centering a street
_MAX_PAVEMENT_SHIFT_FT = 15.0   # largest correction from the HPMS line to the pavement middle
_MIN_PAVEMENT_SHIFT_FT = 1.0    # smaller corrections are ignored
_DIVIDED_FSYSTEMS = (1, 2)      # Interstates and other freeways/expressways: two roadways unless two-way, two-lane
_TWO_WAY_FACILITY = 2           # HPMS Facility_Type of a two-way roadway
_TWO_LANE_MAX_LANES = 2         # Through_Lanes (both directions) of a two-way road with one lane each way
_PLATFORM_HALF_FT = 12.0        # half-width of the narrowest roadway searched for (two lanes)
_PLATFORM_MAX_SLOPE = 0.12      # steepest cross slope of a roadway platform
_PLATFORM_RMS_FT = 0.25         # largest rms departure of a roadway platform from a plane
_PLATFORM_MAX_WIDTH_FT = 100.0  # wider level ground is not a roadway
_OPP_MAX_LEVEL_DIFF_FT = 60.0   # opposing roadway on its own alignment: largest level difference
_OPP_MAX_GAP_FT = 100.0         # and widest ground between the two roadways' pavement
NEARBY_SEARCH_FT = _REACH_FT    # HPMS lines farther than this from the section are ignored
_OPP_LINE_FACILITY = 6          # HPMS Facility_Type of the non-inventory direction
_OPP_LINE_MIN_FT = 16.0         # closer opposing-direction lines are taken as the same roadway
_OPP_LINE_MAX_FT = 2 * _MEDIAN_SEARCH_FT
_OPP_LINE_AGREE_FT = 25.0       # DEM median kept where its opposing roadway is this close to the opposing line
_OPP_LINE_SUPER_PCT = 2.5       # inventory lanes sloping more than this: the opposing line is used (shallow medians)
_OPP_DOUBT_SLOPE_PCT = 4.0      # lanes steeper than this, unless superelevated alike, are doubtful
_PARALLEL_MAX_DEG = 30.0        # a line within this angle of the route runs alongside it
_LINE_MASK_FT = 6.0             # pavement this close beyond another line's lanes is that road's
_MASK_MAX_DEG = 45.0            # other lines within this angle of the route can be mistaken for it
_CROSSING_MIN_DEG = 30.0        # a line crossing the route at least this steeply is a crossing road
_CROSSING_SAMPLE_FT = (60.0, 150.0)  # ground read along the crossing road this far outside each directional line
_CROSSING_ALONG_FT = 600.0      # crossing road points this close to the crossing are searched
_CROSSING_STEP_FT = 5.0
_CROSSING_LEVEL_FT = 15.0       # crossing road ground this far above (below) the route: over (under)
_CROSSING_NBI_FT = 150.0        # NBI/NTI structure point this close to the crossing: the other road is on it
_CROSSING_END_FT = 30.0         # a line meeting the route this close to its own end (a ramp gore) does not cross it
_CROWN_MIN_SLOPE_PCT = 0.5      # each side of a crown must slope at least this much
_FORESLOPE_SEARCH_FT = 80.0
_FORESLOPE_MIN_DROP_FT = 1.0
_FORESLOPE_SMOOTH_FT = 5        # ground smoothed over this many feet before finding slope faces
_FORESLOPE_FACE_SLOPE = 0.05    # ground falling more steeply than this (20:1) is part of a slope face
_FORESLOPE_STEEP_SLOPE = 0.10   # first face at least this steep (10:1) is the foreslope
_HEADING_HALF_FT = 25.0
_TURN_HALF_FT = 100.0
_MIN_STATION_GAP_FT = 50.0
# The HPMS line is taken to be off the road (old alignment, new construction
# in the DEM) where two or more stations in a row show both: the centerline
# this far beyond the route's typical offset from the line, and no
# inventory lanes at the line (none fitted, or sloping more than
# _OFF_ROAD_SLOPE_PCT, steeper than normal superelevation).
_OFF_ROAD_OFFSET_FT = 25.0
_OFF_ROAD_SLOPE_PCT = 8.0
_OFF_ROAD_MIN_STATIONS = 2
# Second test: the ground along the HPMS line must be rough somewhere in the
# run. Departure of the DEM along the line from its own running median over
# _OFF_ROAD_ROUGH_WINDOW_FT, its median taken over each _OFF_ROAD_ROUGH_SPAN_FT
# stretch. On pavement the roughest stretch measured 0.11 ft (urban DC),
# usually under 0.05 ft; the line across the old I-80 alignment at Council
# Bluffs, Iowa measured 0.27-0.38 ft.
_OFF_ROAD_ROUGH_FT = 0.2
_OFF_ROAD_ROUGH_WINDOW_FT = 50.0
_OFF_ROAD_ROUGH_SPAN_FT = 300.0
_OFF_ROAD_STRUCTURE_BUFFER_FT = 100.0   # ground this close to a bridge or tunnel backed by a record is left out
# Centerline methods that place the centerline from the other roadway (the
# median, the other direction's HPMS line, or the outside edges of both
# roadways). On these the offset reflects the median, not whether the HPMS
# line is on its roadway, so the station test does not use it.
_DIVIDED_CENTER_BASES = ("DEM median", "HPMS opposing line", "DEM slopes", "DEM roadways")
_OFF_ROAD_MIN_GROUND_FT = 100.0         # less usable ground than this along the run: station test alone

STATION_COLUMNS = [
    "Station_ID", "Milepost", "Dist_Ft", "Station_Type", "Curve_No", "Curve_Dir",
    "Curve_Superelevation_Pct", "Curve_Super_Note",
    "Center_Basis", "Center_Offset_Ft", "Centerline_Elev_Ft", "Lane_Half_Width_Ft",
    "Inv_Center_Ft", "Inv_Elev_Ft", "Inv_Form", "Inv_Slope_Pct", "Inv_Left_Slope_Pct", "Inv_Right_Slope_Pct", "Inv_Crown_Ft", "Inv_Note",
    "Opp_Center_Ft", "Opp_Line_Ft", "Opp_Elev_Ft", "Opp_Form", "Opp_Slope_Pct", "Opp_Left_Slope_Pct", "Opp_Right_Slope_Pct", "Opp_Crown_Ft", "Opp_Note",
    "Left_Edge_Ft", "Right_Edge_Ft", "Foreslope_Left_HV", "Foreslope_Right_HV",
    "Line_Off_Road", "Lon", "Lat",
]


# ---------------------------------------------------------------------------
# DEM tiles and route geometry
# ---------------------------------------------------------------------------
class DemTiles:
    """Bilinear point sampling from the cached 1 m tiles (elevation in meters).
    Tiles are read whole on first use and kept for the run."""

    def __init__(self, tile_dir: str, max_tiles: int = 12):
        from core.rat_core import HIGH_RES_CELL_SIZE_DEG
        self.tile_dir = tile_dir
        self.cell = HIGH_RES_CELL_SIZE_DEG
        self.max_tiles = max_tiles
        self._tiles: Dict[tuple, Optional[tuple]] = {}

    def _tile(self, key):
        if key in self._tiles:
            return self._tiles[key]
        if len(self._tiles) >= self.max_tiles:
            self._tiles.pop(next(iter(self._tiles)))
        lon_idx, lat_idx = key
        path = os.path.join(self.tile_dir, f"tile_1m_hr{lat_idx}_{lon_idx}.tif")
        tile = None
        if os.path.exists(path):
            import rasterio
            with rasterio.open(path) as src:
                arr = src.read(1).astype(np.float32)
                if src.nodata is not None:
                    arr[arr == src.nodata] = np.nan
                arr[arr < -1000] = np.nan
                tile = (arr, src.transform)
        self._tiles[key] = tile
        return tile

    def sample(self, lon: np.ndarray, lat: np.ndarray) -> np.ndarray:
        lon = np.asarray(lon, dtype=float)
        lat = np.asarray(lat, dtype=float)
        out = np.full(lon.shape, np.nan)
        keys = np.stack([np.floor(lon / self.cell), np.floor(lat / self.cell)], axis=-1).astype(np.int64)
        for key in {tuple(k) for k in keys.reshape(-1, 2)}:
            sel = (keys[..., 0] == key[0]) & (keys[..., 1] == key[1])
            tile = self._tile(key)
            if tile is None:
                continue
            arr, t = tile
            col = (lon[sel] - t.c) / t.a - 0.5
            row = (lat[sel] - t.f) / t.e - 0.5
            h, w = arr.shape
            z = map_coordinates(arr, [row, col], order=1, mode="nearest")
            z[(row < -0.5) | (row > h - 0.5) | (col < -0.5) | (col > w - 0.5)] = np.nan
            out[sel] = z
        return out


# ---------------------------------------------------------------------------
# Route geometry
# ---------------------------------------------------------------------------
class RouteFrame:
    """The route centerline in UTM feet, indexed by the vertices' Dist_Ft."""

    def __init__(self, vtx: pd.DataFrame):
        from pyproj import Transformer
        lon = vtx["Lon"].to_numpy(dtype=float)
        lat = vtx["Lat"].to_numpy(dtype=float)
        zone = int((lon[0] + 180) / 6) + 1
        epsg = (32600 if lat[0] >= 0 else 32700) + zone
        self.to_utm = Transformer.from_crs("EPSG:4326", f"EPSG:{epsg}", always_xy=True)
        self.to_wgs = Transformer.from_crs(f"EPSG:{epsg}", "EPSG:4326", always_xy=True)
        x, y = self.to_utm.transform(lon, lat)
        self.x = np.asarray(x) * FEET_PER_METER
        self.y = np.asarray(y) * FEET_PER_METER
        self.dist = vtx["Dist_Ft"].to_numpy(dtype=float)
        self.mp = vtx["Milepost"].to_numpy(dtype=float)
        self.elev = vtx["Elev_Ft"].to_numpy(dtype=float)
        self.lon, self.lat = lon, lat

    def index(self, dist_ft: float) -> int:
        return int(np.clip(np.searchsorted(self.dist, dist_ft), 0, len(self.dist) - 1))

    def tangent(self, dist_ft: float, half_ft: float = _HEADING_HALF_FT) -> np.ndarray:
        a, b = self.index(dist_ft - half_ft), self.index(dist_ft + half_ft)
        if b == a:
            a, b = max(a - 1, 0), min(b + 1, len(self.dist) - 1)
        t = np.array([self.x[b] - self.x[a], self.y[b] - self.y[a]])
        n = np.hypot(*t)
        return t / n if n > 0 else np.array([1.0, 0.0])

    def turn_sign(self, d0: float, d1: float) -> int:
        """+1 if the route turns left between d0 and d1, -1 if right."""
        t0 = self.tangent(d0, _TURN_HALF_FT)
        t1 = self.tangent(d1, _TURN_HALF_FT)
        return 1 if t0[0] * t1[1] - t0[1] * t1[0] > 0 else -1

    def axis(self, dist_ft: float):
        """(origin, unit tangent, unit right normal) of the cut at dist_ft,
        in UTM feet."""
        i = self.index(dist_ft)
        t = self.tangent(dist_ft)
        return np.array([self.x[i], self.y[i]]), t, np.array([t[1], -t[0]])

    def cut(self, dem: DemTiles, dist_ft: float, reach_ft: float = _REACH_FT):
        """Ground elevation (ft) along the perpendicular at dist_ft, sampled
        every foot from -reach_ft to +reach_ft (+ = right of travel)."""
        origin, _, (rx, ry) = self.axis(dist_ft)
        o = np.arange(-reach_ft, reach_ft + 0.5 * _STEP_FT, _STEP_FT)
        px = origin[0] + o * rx
        py = origin[1] + o * ry
        lon, lat = self.to_wgs.transform(px / FEET_PER_METER, py / FEET_PER_METER)
        z = dem.sample(np.asarray(lon), np.asarray(lat)) * FEET_PER_METER
        return o, z


def _wkt_parts(wkt: str) -> List[np.ndarray]:
    """Lon/lat vertex arrays of each part of a LineString or MultiLineString."""
    from shapely import wkt as shapely_wkt
    try:
        g = shapely_wkt.loads(wkt)
    except Exception:
        return []
    parts = list(g.geoms) if g.geom_type == "MultiLineString" else [g] if g.geom_type == "LineString" else []
    return [np.asarray(pt.coords)[:, :2] for pt in parts if len(pt.coords) >= 2]


def select_nearby_lines(hpms: pd.DataFrame, route_id: str, section_lonlat: np.ndarray,
                        max_dist_ft: float = NEARBY_SEARCH_FT) -> pd.DataFrame:
    """
    HPMS records (RouteId, Facility_Type, WKT) other than route_id itself
    that come within max_dist_ft of the section (section_lonlat: its
    lon/lat vertices). These are the non-inventory direction (Facility_Type
    6), ramps, frontage roads and cross streets. Matched by location only,
    since states name and measure these routes in different ways.
    """
    from pyproj import Transformer
    from shapely.geometry import LineString
    cols = ["RouteId", "Facility_Type", "WKT"]
    if hpms is None or hpms.empty or not set(cols) <= set(hpms.columns):
        return pd.DataFrame(columns=cols)
    cols = (["RouteId", "Facility_Type"]
            + [c for c in ("FSystem", "Structure_Type", "Start_MP", "End_MP") if c in hpms.columns] + ["WKT"])
    lon0, lat0 = section_lonlat[0]
    epsg = (32600 if lat0 >= 0 else 32700) + int((lon0 + 180) / 6) + 1
    to_utm = Transformer.from_crs("EPSG:4326", f"EPSG:{epsg}", always_xy=True)

    def utm(xy):
        x, y = to_utm.transform(xy[:, 0], xy[:, 1])
        return np.c_[x, y] * FEET_PER_METER

    import shapely
    sec = LineString(utm(np.asarray(section_lonlat)))
    pad = max_dist_ft / FEET_PER_METER / 50000.0   # meters to degrees, generous at any latitude
    minx, miny = np.asarray(section_lonlat).min(axis=0) - pad
    maxx, maxy = np.asarray(section_lonlat).max(axis=0) + pad
    cand = hpms[hpms["RouteId"].astype(str) != str(route_id)]
    geoms = shapely.from_wkt(cand["WKT"].astype(str).to_numpy(), on_invalid="ignore")
    b = shapely.bounds(geoms)
    near = (b[:, 0] <= maxx) & (b[:, 2] >= minx) & (b[:, 1] <= maxy) & (b[:, 3] >= miny)
    keep = []
    for idx, wkt in zip(cand.index[near], cand["WKT"].to_numpy()[near]):
        for part in _wkt_parts(str(wkt)):
            if sec.distance(LineString(utm(part))) <= max_dist_ft:
                keep.append(idx)
                break
    return hpms.loc[keep, cols].reset_index(drop=True)


def classify_crossings(lines: pd.DataFrame, vtx: pd.DataFrame, tile_dir: Optional[str],
                       nbi_points=None) -> pd.DataFrame:
    """
    Adds Crossing_MP (route milepost where the line crosses the route, NaN if
    it does not cross at _CROSSING_MIN_DEG or more) and Crossing ("Over",
    "Under" or "") to each nearby line. Tests in order: the route is on a
    bridge span there (the other road passes under); the line's own HPMS
    record is a bridge (Structure_Type 1) or the route is in a tunnel (it
    passes over); an NBI/NTI structure point (nbi_points, EPSG:4326) lies
    within _CROSSING_NBI_FT of the crossing (over, unless the ground below
    says under); the bare-earth ground along the other road (see
    _crossing_by_ground).
    """
    out = lines.copy()
    out["Crossing_MP"] = np.nan
    out["Crossing"] = ""
    if out.empty:
        return out
    frame = RouteFrame(vtx)
    dem = DemTiles(tile_dir) if tile_dir else None
    nearby = NearbyLines(lines, frame)
    structures = np.empty((0, 2))
    if nbi_points is not None and len(nbi_points):
        px, py = frame.to_utm.transform(nbi_points.geometry.x.to_numpy(), nbi_points.geometry.y.to_numpy())
        structures = np.c_[px, py] * FEET_PER_METER
    route_parts: Dict[str, List[np.ndarray]] = {}
    for _, row in out.iterrows():
        for part in _wkt_parts(str(row["WKT"])):
            x, y = frame.to_utm.transform(part[:, 0], part[:, 1])
            route_parts.setdefault(str(row["RouteId"]), []).append(np.c_[x, y] * FEET_PER_METER)
    rx, ry = frame.x, frame.y
    span_kind = vtx["Structure_Kind"].astype(str).to_numpy() if "Structure_Kind" in vtx.columns else None
    seg = np.c_[rx[:-1], ry[:-1]], np.c_[np.diff(rx), np.diff(ry)]
    for i, row in out.iterrows():
        for part in _wkt_parts(str(row["WKT"])):
            x, y = frame.to_utm.transform(part[:, 0], part[:, 1])
            xy = np.c_[x, y] * FEET_PER_METER
            hit = _first_crossing(seg, xy)
            if hit is None:
                continue
            k, u, kk, v, cos_a = hit
            if math.degrees(math.acos(min(cos_a, 1.0))) < _CROSSING_MIN_DEG:
                continue
            s_line = np.r_[0.0, np.cumsum(np.hypot(*np.diff(xy, axis=0).T))]
            s_hit = s_line[kk] + v * (s_line[kk + 1] - s_line[kk])
            if s_hit < _CROSSING_END_FT or s_hit > s_line[-1] - _CROSSING_END_FT:
                continue
            d_ft = frame.dist[k] + u * (frame.dist[k + 1] - frame.dist[k])
            j = frame.index(d_ft)
            out.at[i, "Crossing_MP"] = round(float(frame.mp[j]), 3)
            point = seg[0][k] + u * seg[1][k]
            kind = span_kind[j] if span_kind is not None else ""
            if kind == "Bridge":
                out.at[i, "Crossing"] = "Under"
            elif kind == "Tunnel" or pd.to_numeric(row.get("Structure_Type"), errors="coerce") == 1:
                out.at[i, "Crossing"] = "Over"
            else:
                ground = ""
                if dem is not None:
                    _, tangent, right = frame.axis(d_ft)
                    opp = nearby.guide(point, tangent, right)["opp"]
                    ground = _crossing_by_ground(frame, dem, route_parts[str(row["RouteId"])],
                                                 point, right, opp, float(frame.elev[j]))
                near_structure = (len(structures) > 0 and
                                  float(np.min(np.hypot(*(structures - point).T))) <= _CROSSING_NBI_FT)
                out.at[i, "Crossing"] = "Over" if near_structure and ground != "Under" else ground
            break
    return out


def _first_crossing(seg, xy):
    """(route segment, fraction along it, line segment, fraction along it,
    |cos| of the angle between them) where polyline xy first crosses the
    route, or None."""
    a, d = seg
    for kk in range(len(xy) - 1):
        p, e = xy[kk], xy[kk + 1] - xy[kk]
        den = d[:, 0] * e[1] - d[:, 1] * e[0]
        ok = np.abs(den) > 1e-9
        rel = p - a
        with np.errstate(divide="ignore", invalid="ignore"):
            u = (rel[:, 0] * e[1] - rel[:, 1] * e[0]) / den
            v = (rel[:, 0] * d[:, 1] - rel[:, 1] * d[:, 0]) / den
        hit = np.flatnonzero(ok & (u >= 0) & (u <= 1) & (v >= 0) & (v <= 1))
        if hit.size:
            k = int(hit[0])
            cos_a = abs(d[k] @ e) / max(np.hypot(*d[k]) * np.hypot(*e), 1e-9)
            return k, float(u[k]), kk, float(v[k]), cos_a
    return None


def _crossing_by_ground(frame, dem, parts, point, right, opp, route_elev_ft) -> str:
    """"Over"/"Under" from the bare-earth ground along the other road just
    beyond each directional roadway, where an overpass's approach fills
    stand: between _CROSSING_SAMPLE_FT outside the HPMS inventory line on
    one side and outside the opposing-direction line (opp, offset in ft; the
    inventory line where there is none) on the other. Each side is compared
    with the ground at its own directional line; the upper quartile of the
    samples is used so that ground in the bridge opening is passed over.
    parts are all the other road's HPMS lines (its records often end at the
    bridge). "" if not clearly either."""
    pts = []
    for xy in parts:
        s = np.r_[0.0, np.cumsum(np.hypot(*np.diff(xy, axis=0).T))]
        d = np.arange(0.0, s[-1] + _CROSSING_STEP_FT / 2, _CROSSING_STEP_FT)
        pts.append(np.c_[np.interp(d, s, xy[:, 0]), np.interp(d, s, xy[:, 1])])
    pts = np.vstack(pts)
    pts = pts[np.hypot(*(pts - point).T) <= _CROSSING_ALONG_FT]
    px, py = pts[:, 0], pts[:, 1]
    t = (px - point[0]) * right[0] + (py - point[1]) * right[1]     # offset right of the route

    def ground(qx, qy):
        lon, lat = frame.to_wgs.transform(np.asarray(qx) / FEET_PER_METER, np.asarray(qy) / FEET_PER_METER)
        return np.atleast_1d(dem.sample(np.asarray(lon), np.asarray(lat))) * FEET_PER_METER

    lo, hi = _CROSSING_SAMPLE_FT
    sides = []
    for ref, sign in ((max(0.0, opp or 0.0), 1), (min(0.0, opp or 0.0), -1)):
        sel = (sign * (t - ref) >= lo) & (sign * (t - ref) <= hi)
        if sel.sum() < 3:
            return ""
        z = ground(px[sel], py[sel])
        if not np.isfinite(z).any():
            return ""
        base = route_elev_ft
        if ref != 0.0:
            q = point + ref * np.asarray(right)
            zb = ground([q[0]], [q[1]])[0]
            base = zb if np.isfinite(zb) else route_elev_ft
        sides.append(float(np.nanpercentile(z, 75)) - base)
    if min(sides) >= _CROSSING_LEVEL_FT:
        return "Over"
    if max(sides) <= -_CROSSING_LEVEL_FT:
        return "Under"
    return ""


class NearbyLines:
    """Other HPMS lines around the route, in the route frame's UTM feet."""

    def __init__(self, lines: Optional[pd.DataFrame], frame: "RouteFrame"):
        self.parts = []     # (is_opposing_direction, Nx2 vertices, functional class or None)
        if lines is None or lines.empty:
            return
        for _, row in lines.iterrows():
            opposing = pd.to_numeric(row["Facility_Type"], errors="coerce") == _OPP_LINE_FACILITY
            fs = pd.to_numeric(row.get("FSystem"), errors="coerce")
            fs = int(fs) if np.isfinite(fs) else None
            for part in _wkt_parts(str(row["WKT"])):
                x, y = frame.to_utm.transform(part[:, 0], part[:, 1])
                self.parts.append((bool(opposing), np.c_[x, y] * FEET_PER_METER, fs))

    def crossings(self, origin, tangent, right, reach_ft: float = _REACH_FT):
        """[(offset_ft, is_opposing_direction, angle_deg, functional class)]
        where the cut through origin along right crosses each line (+ =
        right); angle_deg is the line's angle to the route (0 = alongside)."""
        out = []
        for opposing, xy, fs in self.parts:
            a, d = xy[:-1], np.diff(xy, axis=0)
            den = right[0] * d[:, 1] - right[1] * d[:, 0]
            ok = np.abs(den) > 1e-9
            rel = a - origin
            with np.errstate(divide="ignore", invalid="ignore"):
                t = (rel[:, 0] * d[:, 1] - rel[:, 1] * d[:, 0]) / den
                u = (rel[:, 0] * right[1] - rel[:, 1] * right[0]) / den
            hit = ok & (u >= 0) & (u <= 1) & (np.abs(t) <= reach_ft)
            for k in np.flatnonzero(hit):
                cos_a = min(abs(d[k] @ tangent) / max(np.hypot(*d[k]), 1e-9), 1.0)
                out.append((float(t[k]), opposing, math.degrees(math.acos(cos_a)), fs))
        return out

    def guide(self, origin, tangent, right) -> Dict:
        """The opposing direction's line offset at this cut (nearest on the
        left, else on the right; None if absent) and its functional class
        (None if unknown), and the offsets of the other lines running
        roughly alongside (ramps, frontage roads)."""
        hits = self.crossings(origin, tangent, right)
        opp = [(h[0], h[3]) for h in hits
               if h[1] and h[2] <= _PARALLEL_MAX_DEG and _OPP_LINE_MIN_FT <= abs(h[0]) <= _OPP_LINE_MAX_FT]
        left = [h for h in opp if h[0] < 0]
        pick = max(left) if left else (min(opp) if opp else None)
        others = [h[0] for h in hits if not (h[1] and h[2] <= _PARALLEL_MAX_DEG) and h[2] <= _MASK_MAX_DEG]
        return {"opp": pick[0] if pick else None, "opp_fsystem": pick[1] if pick else None,
                "others": others}


# ---------------------------------------------------------------------------
# Section measurements
# ---------------------------------------------------------------------------
def _smooth(z: np.ndarray, size: int) -> np.ndarray:
    """Moving average that leaves gaps (NaN) as gaps instead of spreading them."""
    ok = np.isfinite(z)
    if not ok.any():
        return z.copy()
    idx = np.arange(len(z))
    filled = np.interp(idx, idx[ok], z[ok])
    out = uniform_filter1d(filled, size)
    out[~ok] = np.nan
    return out


def _at(o: np.ndarray, value: float) -> int:
    return int(np.clip(np.rint((value - o[0]) / _STEP_FT), 0, len(o) - 1))


def fit_lanes(o: np.ndarray, z: np.ndarray, center_ft: float, half_ft: float) -> Optional[Dict]:
    """
    Cross slope of one roadway's travel lanes, center_ft +/- half_ft. Fits
    one plane, and a crown (two planes meeting at a high point); the crown is
    kept when it fits clearly better and each side slopes away from it by at
    least _CROWN_MIN_SLOPE_PCT. Slopes are rise per 100 ft toward the right.
    """
    sel = (o >= center_ft - half_ft) & (o <= center_ft + half_ft) & np.isfinite(z)
    if sel.sum() < max(8, int(1.5 * half_ft)):
        return None
    x, y = o[sel], z[sel]
    if np.max(np.abs(np.gradient(_smooth(y, 3), _STEP_FT))) > _LANE_MAX_SLOPE * 2:
        return None
    p1 = np.polyfit(x, y, 1)
    sse1 = float(np.sum((y - np.polyval(p1, x)) ** 2))
    best = None
    for c in x[(x >= x[0] + 3) & (x <= x[-1] - 3)]:
        A = np.c_[np.ones_like(x), x, np.maximum(x - c, 0.0)]
        coef, *_ = np.linalg.lstsq(A, y, rcond=None)
        sse = float(np.sum((y - A @ coef) ** 2))
        if best is None or sse < best[0]:
            best = (sse, float(c), coef)
    out = {"center": center_ft, "half": half_ft, "level": float(np.mean(y)),
           "form": "Planar", "slope": float(p1[0]) * 100.0,
           "left": np.nan, "right": np.nan, "crown": np.nan, "rms": math.sqrt(sse1 / len(y))}
    if best is not None and best[0] < 0.5 * sse1:
        _, c, coef = best
        s_left, s_right = coef[1] * 100.0, (coef[1] + coef[2]) * 100.0
        if s_left >= _CROWN_MIN_SLOPE_PCT and s_right <= -_CROWN_MIN_SLOPE_PCT:
            out.update({"form": "Crowned", "slope": np.nan, "left": s_left, "right": s_right, "crown": c,
                        "rms": math.sqrt(best[0] / len(y))})
    return out


def _pavement_like(lanes: Optional[Dict]) -> bool:
    """Lanes fit close to a plane (or crown) at a plausible cross slope."""
    if lanes is None or lanes["rms"] > _PLATFORM_RMS_FT:
        return False
    steepest = np.nanmax(np.abs([lanes["slope"], lanes["left"], lanes["right"]]))
    return bool(steepest <= _PLATFORM_MAX_SLOPE * 100.0)


def _plane_edge(o: np.ndarray, z: np.ndarray, fit_lo: float, fit_hi: float, direction: int,
                allow_cut: bool = False) -> Optional[float]:
    """Outer pavement edge: fits a line to the lane between fit_lo and fit_hi
    and walks outward (direction +1 right, -1 left) to where the ground falls
    _EDGE_DROP_FT below that line and stays below (or, with allow_cut,
    leaves it either way)."""
    sel = (o >= fit_lo) & (o <= fit_hi) & np.isfinite(z)
    if sel.sum() < 6:
        return None
    p = np.polyfit(o[sel], z[sel], 1)
    dev = z - np.polyval(p, o)
    below = np.abs(dev) > _EDGE_DROP_FT if allow_cut else dev < -_EDGE_DROP_FT
    i = _at(o, fit_hi if direction > 0 else fit_lo)
    for k in range(int(_EDGE_SEARCH_FT / _STEP_FT)):
        j = i + direction * k
        if j < 3 or j > len(o) - 4 or not np.isfinite(z[j]):
            return None
        if below[j] and below[j + direction] and below[j + 2 * direction]:
            return float(o[j - direction])
    return None


def center_on_pavement(o: np.ndarray, z: np.ndarray, half: float) -> float:
    """Offset of the middle of a single roadway's pavement from the HPMS
    line, found from the two pavement edges (where the ground leaves the plane
    of each outer lane). Refined once; 0 if the edges can't be placed or the
    result is implausible."""
    c = 0.0
    for _ in range(3):
        left = _plane_edge(o, z, c - half, c, -1)
        right = _plane_edge(o, z, c, c + half, +1)
        if left is None or right is None:
            return 0.0
        width = right - left
        if width < 2 * half - 4.0 or width > 2 * half + 2 * _MAX_SHOULDER_FT:
            return 0.0
        new = (left + right) / 2.0
        if abs(new - c) < 0.5:
            c = new
            break
        c = new
    return c if abs(c) <= _MAX_PAVEMENT_SHIFT_FT else 0.0


def _fit_any(o, z, center, halves):
    """Lanes fitted with the first half-width in halves that the DEM
    supports; (None, halves[0]) if none does."""
    for h in halves:
        f = fit_lanes(o, z, center, h)
        if f is not None:
            return f, h
    return None, halves[0]


def _lane_plane(o, z, center, half) -> Optional[np.ndarray]:
    """Line through the lanes center +/- half (polyfit coefficients)."""
    sel = (np.abs(o - center) <= half) & np.isfinite(z)
    if sel.sum() < 6:
        return None
    return np.polyfit(o[sel], z[sel], 1)


def _opposing_ok(o, z, center, half, level, plane=None) -> Optional[Dict]:
    """Opposing roadway's lanes, mirrored about the centerline, if the DEM
    shows pavement there at about the inventory roadway's level or on the
    extension of its plane (both roadways superelevated together)."""
    opp, _ = _fit_any(o, z, 2 * center, _lane_halves(half))
    if not _pavement_like(opp):
        return None
    diff = abs(opp["level"] - level)
    if plane is not None:
        diff = min(diff, abs(opp["level"] - float(np.polyval(plane, 2 * center))))
    return opp if diff <= _LEVEL_TOL_FT else None


def find_median_center(o, z, half, level, plane=None) -> Optional[float]:
    """Centerline of a divided highway with a depressed median left of the
    inventory roadway. The median is found where the ground dips below the
    inventory lanes' level (or, given plane, below the extension of their
    plane) and rises back; the centerline is then
    midway between the inside pavement edges of the two roadways (or midway
    across the median at 70% of its depth when those edges can't be
    placed). Measured against the plane, a dip found beyond a pavement-like
    strip (the opposing lanes, with a ramp or frontage road ditch past them)
    is not the median: extending the plane far across the section can make
    a dip there look deep."""
    road_level = level
    if plane is not None:
        z_ref = z - np.polyval(plane, o)
        level = 0.0
    else:
        z_ref = z
    zs = _smooth(z_ref, 5)
    i_in = _at(o, -half)
    z_min, j_min = np.inf, None
    for j in range(i_in, -1, -1):
        if o[j] < -_MEDIAN_SEARCH_FT or not np.isfinite(zs[j]):
            return None
        if zs[j] < z_min:
            z_min, j_min = zs[j], j
        depth = level - z_min
        if depth >= _MEDIAN_MIN_DEPTH_FT and zs[j] >= z_min + 0.7 * depth:
            rim = z_min + 0.7 * depth
            if plane is not None and _roadway_between(o, z, half, float(o[j_min]), road_level,
                                                      rim + np.polyval(plane, o)):
                return None
            k = j_min
            while k < _at(o, 0.0) and zs[k] < rim:
                k += 1
            c0 = (o[j] + o[k]) / 2.0
            near = _plane_edge(o, z, -half, 0.0, -1)
            far = _plane_edge(o, z, 2 * c0, 2 * c0 + half, +1)
            if near is not None and far is not None and abs((near + far) / 2.0 - c0) <= _MEDIAN_REFINE_FT:
                return (near + far) / 2.0
            return c0
    return None


def _roadway_between(o, z, half, x_dip, road_level, rim) -> bool:
    """True if a two-lane, pavement-like strip lies wholly between the
    inventory lanes' widest inside shoulder and x_dip, standing at about
    road level or above the median's rim (rim: elevation at each offset).
    A flat-bottomed median ditch lies lower and does not count."""
    h = _PLATFORM_HALF_FT
    cs, ok, lev = _platform_scan(o, z, h, x_dip + h, -(half + _MAX_SHOULDER_FT + h))
    if not ok.any():
        return False
    rim_at = np.interp(cs, o, np.broadcast_to(rim, o.shape))
    high = (lev >= road_level - _MEDIAN_MIN_DEPTH_FT) | (lev >= rim_at)
    return bool((ok & high).any())


def find_outer_edges_center(o, z, half, level, plane=None, allow_cut=False) -> Optional[float]:
    """Centerline of a divided highway whose median the DEM does not show
    clearly: midway between the outside pavement edges of the two roadways.
    Walking left from the inventory lanes, a dip below road level (or, given
    plane, below the extension of the inventory lanes' plane) that comes
    back within _MEDIAN_RECOVERY_FT is crossed as median; the first drop
    that does not come back is outside the opposing roadway. With
    allow_cut, ground rising into a cut slope also marks the outside, and
    each outside edge may be a backslope instead of a foreslope."""
    zs = _smooth(z - np.polyval(plane, o) if plane is not None else z, 3)
    if plane is not None:
        level = 0.0
    far = -(_MEDIAN_SEARCH_FT + 2 * half + _EDGE_SEARCH_FT)
    j = _at(o, -half)
    edge = None
    while j > 0 and o[j] > far:
        if not np.isfinite(zs[j - 1]):
            return None
        below = zs[j - 1] < level - _MEDIAN_MIN_DEPTH_FT
        above = allow_cut and zs[j - 1] > level + _MEDIAN_MIN_DEPTH_FT
        if not below and not above:
            j -= 1
            continue
        ahead = (o < o[j]) & (o >= o[j] - _MEDIAN_RECOVERY_FT) & np.isfinite(zs)
        back = np.flatnonzero(ahead & (np.abs(zs - level) <= 0.5))
        if back.size:
            j = int(back.max())
            continue
        edge = j
        break
    if edge is None:
        return None
    right = _plane_edge(o, z, 0.0, half, +1, allow_cut)
    if right is None:
        return None
    c0 = (float(o[edge]) + right) / 2.0
    left = _plane_edge(o, z, 2 * c0 - half, 2 * c0, -1, allow_cut)
    if left is None:
        return None
    width = right - left
    if width < 4 * half + 10.0 or width > _SECTION_MAX_WIDTH_FT:
        return None
    for edge_ft, direction in ((left, -1), (right, +1)):
        if not _falls(o, z, edge_ft, direction) and not (allow_cut and _rises(o, z, edge_ft, direction)):
            return None
    return (left + right) / 2.0


def _rises(o, z, edge_ft, direction) -> bool:
    """True if the ground climbs at least _FORESLOPE_MIN_DROP_FT within
    _FORESLOPE_SEARCH_FT beyond a platform edge (a cut slope)."""
    sel = (o * direction > edge_ft * direction) & (np.abs(o - edge_ft) <= _FORESLOPE_SEARCH_FT) & np.isfinite(z)
    i = _at(o, edge_ft)
    return bool(sel.any() and np.isfinite(z[i]) and np.max(z[sel]) - z[i] >= _FORESLOPE_MIN_DROP_FT)


def _platform_scan(o, z, half, c_lo, c_hi):
    """Window centers c from c_lo to c_hi and whether c +/- half looks like
    pavement there: close to a plane, gently sloped and free of breaks.
    Returns (centers, ok, level)."""
    zs = _smooth(z, 3)
    steep = np.abs(np.gradient(zs, _STEP_FT)) > 2 * _LANE_MAX_SLOPE
    bad = ~np.isfinite(z) | steep
    zf = np.where(np.isfinite(z), z, 0.0)
    w = int(round(half / _STEP_FT))
    n = 2 * w + 1
    cs_bad = np.r_[0, np.cumsum(bad)]
    xi = np.arange(len(o), dtype=float)
    S1 = np.r_[0, np.cumsum(zf)]
    Sx = np.r_[0, np.cumsum(xi)]
    Sxx = np.r_[0, np.cumsum(xi * xi)]
    Sxy = np.r_[0, np.cumsum(xi * zf)]
    Syy = np.r_[0, np.cumsum(zf * zf)]
    i_lo, i_hi = max(_at(o, c_lo), w), min(_at(o, c_hi), len(o) - 1 - w)
    if i_hi < i_lo:
        return np.array([]), np.array([], dtype=bool), np.array([])
    i = np.arange(i_lo, i_hi + 1)
    a, b = i - w, i + w + 1
    nb = cs_bad[b] - cs_bad[a]
    sy, sx = S1[b] - S1[a], Sx[b] - Sx[a]
    sxx, sxy, syy = Sxx[b] - Sxx[a], Sxy[b] - Sxy[a], Syy[b] - Syy[a]
    vx = sxx - sx * sx / n
    slope = (sxy - sx * sy / n) / vx
    sse = np.maximum(syy - sy * sy / n - slope * (sxy - sx * sy / n), 0.0)
    rms = np.sqrt(sse / n)
    ok = (nb == 0) & (rms <= _PLATFORM_RMS_FT) & (np.abs(slope) / _STEP_FT <= _PLATFORM_MAX_SLOPE)
    return o[i], ok, sy / n


def find_separate_roadways(o, z, half, level) -> Optional[tuple]:
    """Divided highway whose opposing roadway is on its own alignment,
    possibly well above or below the inventory roadway with no median
    ditch between them. Finds the nearest pavement-like platform left of
    the inventory lanes, separated from them by _MEDIAN_MIN_WIDTH_FT to
    _OPP_MAX_GAP_FT of ground that is not pavement, and within
    _OPP_MAX_LEVEL_DIFF_FT of their level. Returns
    (centerline, opposing lanes' center): the centerline is midway between
    the two roadways' inside pavement edges."""
    h = min(half, _PLATFORM_HALF_FT)
    cs, ok, lev = _platform_scan(o, z, h, -2 * _MEDIAN_SEARCH_FT, _MAX_PAVEMENT_SHIFT_FT)
    if not ok.any():
        return None
    edges = np.diff(np.r_[0, ok.astype(int), 0])
    starts, ends = np.flatnonzero(edges == 1), np.flatnonzero(edges == -1) - 1
    runs = [(cs[a], cs[b], float(np.mean(lev[a:b + 1]))) for a, b in zip(starts, ends)]
    inv = [r for r in runs if r[1] >= -_MAX_PAVEMENT_SHIFT_FT]
    if not inv:
        return None
    inv_left = inv[0][0]
    if inv_left - h < -(half + _MAX_SHOULDER_FT + _MAX_PAVEMENT_SHIFT_FT):
        return None     # level ground runs on past the inside shoulder: no clear separation
    for c0, c1, lv in reversed([r for r in runs if r[1] < inv_left]):
        gap = (inv_left - h) - (c1 + h)
        width = c1 - c0 + 2 * h
        center = (inv_left + c1) / 2.0
        if center < -_MEDIAN_SEARCH_FT:
            break
        if gap > _OPP_MAX_GAP_FT:
            break
        if gap < _MEDIAN_MIN_WIDTH_FT or width > _PLATFORM_MAX_WIDTH_FT:
            continue
        if abs(lv - level) > _OPP_MAX_LEVEL_DIFF_FT:
            continue
        return center, (c0 + c1) / 2.0
    return None


def _separate(o, z, half, level) -> Optional[tuple]:
    """(centerline, opposing lanes) for roadways on separate alignments."""
    found = find_separate_roadways(o, z, half, level)
    if found is None:
        return None
    opp, _ = _fit_any(o, z, found[1], _lane_halves(half))
    return (found[0], opp) if opp is not None else None


def _checked(found, on_other_road):
    """found, unless its opposing roadway lies on another road's line."""
    if found is None or on_other_road(found[0], found[1]):
        return None
    return found


def _hpms_attrs(hpms_sub: Optional[pd.DataFrame], mp: float) -> Dict:
    """Median type and width, lanes, lane width, functional class and
    facility type from the HPMS section covering mp (None where the input
    lacks them)."""
    out = {"median_type": None, "median_width": None, "through_lanes": None,
           "dir_lanes": None, "lane_width": None, "fsystem": None, "facility_type": None}
    if hpms_sub is None or hpms_sub.empty:
        return out
    cols = {c.lower(): c for c in hpms_sub.columns}
    hit = hpms_sub[(hpms_sub["Start_MP"] <= mp) & (hpms_sub["End_MP"] >= mp)]
    if hit.empty:
        return out

    def first(name):
        if name not in cols:
            return None
        v = pd.to_numeric(hit[cols[name]], errors="coerce").dropna()
        return float(v.iloc[0]) if not v.empty and v.iloc[0] > 0 else None

    mt = first("median_type")
    out.update({"median_type": int(mt) if mt is not None else None,
                "median_width": first("median_width"), "through_lanes": first("through_lanes"),
                "dir_lanes": first("dir_through_lanes"), "lane_width": first("lane_width")})
    fs = first("fsystem")
    out["fsystem"] = int(fs) if fs is not None else None
    ft = first("facility_type")
    out["facility_type"] = int(ft) if ft is not None else None
    return out


def two_way_two_lane(hp: Dict) -> bool:
    """True where HPMS shows a two-way roadway (Facility_Type 2) with no
    more than one through lane each way (Through_Lanes counts both
    directions). Such a road has no median whatever its functional class:
    a divided road has at least two lanes each way, except the rare
    parkway with one lane each way, which is then centered on the HPMS
    line like any undivided road."""
    return (hp.get("facility_type") == _TWO_WAY_FACILITY
            and hp.get("through_lanes") is not None
            and hp["through_lanes"] <= _TWO_LANE_MAX_LANES)


def single_roadway(hp: Dict) -> bool:
    """True where HPMS shows a two-way, two-lane road (two_way_two_lane),
    no median, or one too narrow to be digitized separately (a painted or
    curbed strip on a street): the HPMS line is then the middle of the
    whole street. Otherwise never on a freeway or Interstate, where the
    line follows the inventory roadway even beside a narrow barrier
    median."""
    if two_way_two_lane(hp):
        return True
    if hp.get("fsystem") in _DIVIDED_FSYSTEMS:
        return False
    return hp["median_type"] == 1 or (hp["median_width"] is not None and hp["median_width"] < _WIDE_MEDIAN_FT)


def divided(hp: Dict) -> bool:
    """True where the facility has two roadways: a freeway or Interstate,
    or HPMS shows a median at least _WIDE_MEDIAN_FT wide, and it is not a
    single roadway (single_roadway)."""
    if single_roadway(hp):
        return False
    return (hp["median_type"] or 0) > 1 or hp.get("fsystem") in _DIVIDED_FSYSTEMS


def _lane_halves(half: float) -> tuple:
    """Half-widths to try for one roadway's lanes, widest first: the HPMS
    width, then two lanes (the DEM may not show the full width as one
    plane, e.g. where a climbing lane breaks the cross slope)."""
    return (half, _PLATFORM_HALF_FT) if half > _PLATFORM_HALF_FT else (half,)


def paired_with_opposing_line(hp: Dict, guide: Optional[Dict]) -> bool:
    """True where the other direction has its own HPMS line (Facility_Type
    6) running alongside, of the same functional class where both are
    known. The HPMS line then follows its own direction's lanes, whatever
    the median: the two lines are the two directions of one road. The
    functional class check keeps a frontage road from pairing with a
    freeway's other direction."""
    if not guide or guide.get("opp") is None:
        return False
    fs, opp_fs = hp.get("fsystem"), guide.get("opp_fsystem")
    return fs is None or opp_fs is None or fs == opp_fs


def lane_half_width(hp: Dict, paired: bool = False) -> float:
    """Half the width of the roadway the HPMS line follows. With paired
    (paired_with_opposing_line), one direction's lanes: Dir_Through_Lanes,
    else half of Through_Lanes, or Through_Lanes itself where it is 2 or
    less (taken as that direction's count, since a road whose directions
    have separate lines rarely has one lane each way)."""
    lane_w = hp["lane_width"] or _DEFAULT_LANE_WIDTH_FT
    through = hp["through_lanes"]
    if paired:
        lanes = hp["dir_lanes"] or ((through if through <= _TWO_LANE_MAX_LANES else math.ceil(through / 2))
                                    if through else _DEFAULT_LANES)
    elif single_roadway(hp):
        lanes = through or _DEFAULT_LANES
    else:
        lanes = hp["dir_lanes"] or (math.ceil(through / 2) if through else _DEFAULT_LANES)
    return lanes * lane_w / 2.0


def _falls(o: np.ndarray, z: np.ndarray, edge_ft: float, direction: int) -> bool:
    """True if the ground drops at least _FORESLOPE_MIN_DROP_FT within
    _FORESLOPE_SEARCH_FT beyond a platform edge."""
    i = _at(o, edge_ft)
    sel = (o * direction > edge_ft * direction) & (np.abs(o - edge_ft) <= _FORESLOPE_SEARCH_FT) & np.isfinite(z)
    return bool(sel.any() and np.isfinite(z[i]) and z[i] - np.min(z[sel]) >= _FORESLOPE_MIN_DROP_FT)


def foreslope_hv(o: np.ndarray, z: np.ndarray, edge_ft: float, direction: int) -> Optional[float]:
    """Foreslope beyond a platform edge as H in H:V, or None if the ground
    does not drop. Faces are stretches within _FORESLOPE_SEARCH_FT of the
    edge where the (lightly smoothed) ground falls outward more steeply than
    _FORESLOPE_FACE_SLOPE and drops at least _FORESLOPE_MIN_DROP_FT; each is
    measured by a line fitted between 10% and 90% of its drop. The first face
    at least as steep as _FORESLOPE_STEEP_SLOPE is reported (the face down
    from the shoulder, past any flat strip beside it), else the steepest."""
    i = _at(o, edge_ft)
    if not np.isfinite(z[i]):
        return None
    sel = (o * direction >= edge_ft * direction) & (np.abs(o - edge_ft) <= _FORESLOPE_SEARCH_FT) & np.isfinite(z)
    oo, zz = o[sel], z[sel]
    if direction < 0:
        oo, zz = oo[::-1], zz[::-1]
    if len(zz) < _FORESLOPE_SMOOTH_FT + 2:
        return None
    zs = _smooth(zz, _FORESLOPE_SMOOTH_FT)
    falling = np.gradient(zs, np.abs(oo - oo[0])) < -_FORESLOPE_FACE_SLOPE
    steepest, k, n = None, 0, len(zs)
    while k < n:
        if not falling[k]:
            k += 1
            continue
        j = k
        while j + 1 < n and falling[j + 1]:
            j += 1
        drop = zs[k] - zs[j]
        if drop >= _FORESLOPE_MIN_DROP_FT:
            d = zs[k] - zs[k:j + 1]
            mid = (d >= 0.1 * drop) & (d <= 0.9 * drop)
            if mid.sum() >= 3:
                slope = abs(np.polyfit(oo[k:j + 1][mid], zs[k:j + 1][mid], 1)[0])
            else:
                slope = drop / abs(oo[j] - oo[k])
            if slope >= _FORESLOPE_STEEP_SLOPE:
                return 1.0 / slope
            steepest = slope if steepest is None else max(steepest, slope)
        k = j + 1
    return 1.0 / steepest if steepest else None


def measure_profile(o: np.ndarray, z: np.ndarray, hp: Dict, guide: Optional[Dict] = None) -> Dict:
    """Measurements for one cut: o is offset from the HPMS line (ft, 1 ft
    steps, + right), z ground elevation (ft), hp the HPMS attributes,
    guide the other HPMS lines crossed by the cut (NearbyLines.guide)."""
    guide = guide or {"opp": None, "opp_fsystem": None, "others": []}
    paired = paired_with_opposing_line(hp, guide)
    single, two_roadways = single_roadway(hp) and not paired, divided(hp)
    inv, half = _fit_any(o, z, 0.0, _lane_halves(lane_half_width(hp, paired)))
    level = inv["level"] if inv else float(np.nanmean(z[np.abs(o) <= half]))
    plane = _lane_plane(o, z, 0.0, half)
    center, basis, opp = 0.0, "HPMS line", None

    def on_other_road(c, found) -> bool:
        x = found["center"] if found is not None else 2 * c
        return any(abs(x - g) <= half + _LINE_MASK_FT for g in guide["others"])

    def try_finder(finder, **kw):
        c = finder(o, z, half, level, **kw)
        if c is None and two_roadways and inv is not None and inv["form"] == "Planar":
            # Superelevated: the median may lie above the inventory lanes but
            # still dips below the plane they and the opposing lanes share.
            c = finder(o, z, half, level, plane, **kw)
        if c is None or c > -(half + _MEDIAN_MIN_WIDTH_FT / 2):
            return None
        found = _opposing_ok(o, z, c, half, level, plane)
        return (c, found) if found is not None and not on_other_road(c, found) else None

    def from_line():
        # The opposing roadway is centered on the pavement the DEM shows
        # around the HPMS line for that direction (as single roadways are).
        g = guide["opp"]
        oc = g + center_on_pavement(o - g, z, half)
        if abs(oc) < 2 * half + _MEDIAN_MIN_WIDTH_FT / 2:
            oc = g      # re-centering ran onto the inventory lanes (one wide platform)
        lanes, _ = _fit_any(o, z, oc, _lane_halves(half))
        return oc / 2.0, lanes

    superelevated = inv is not None and inv["form"] == "Planar" and abs(inv["slope"]) > _OPP_LINE_SUPER_PCT

    def near_line(found):
        if found is None or superelevated:
            return None
        c, opp_fit = found
        x = opp_fit["center"] if opp_fit is not None else 2 * c
        return found if abs(x - guide["opp"]) <= _OPP_LINE_AGREE_FT else None

    if not single:
        steps = []
        if guide["opp"] is not None:
            # The DEM median is kept where it agrees with the opposing line
            # (that line is not always on the middle of its roadway).
            steps += [("DEM median", lambda: near_line(try_finder(find_median_center))),
                      ("HPMS opposing line", from_line)]
        else:
            steps.append(("DEM median", lambda: try_finder(find_median_center)))
        steps.append(("DEM slopes", lambda: try_finder(find_outer_edges_center)))
        if two_roadways:
            steps += [("DEM roadways", lambda: _checked(_separate(o, z, half, level), on_other_road)),
                      ("DEM slopes", lambda: try_finder(find_outer_edges_center, allow_cut=True))]
        for name, step in steps:
            found = step()
            if found is not None:
                (center, opp), basis = found, name
                break
        if (basis == "HPMS line" and hp["median_type"] is not None and hp["median_type"] > 1
                and hp["median_width"] and hp["median_width"] >= _WIDE_MEDIAN_FT):
            center, basis = -(hp["median_width"] / 2.0 + half), "HPMS median and lanes"
            opp = _opposing_ok(o, z, center, half, level, plane)

    if basis == "HPMS line" and not single and not two_roadways:
        # No median found and none reported on a road that may be undivided:
        # treat the HPMS line as the middle of a single street.
        street = lane_half_width(dict(hp, median_type=1))
        if street != half:
            fit, h = _fit_any(o, z, 0.0, (street,) + _lane_halves(half))
            if fit is not None:
                inv, half = fit, h

    inv_c = 0.0
    if basis == "HPMS line":
        shift = center_on_pavement(o, z, half)
        if abs(shift) >= _MIN_PAVEMENT_SHIFT_FT:
            center = inv_c = shift
            basis = "DEM pavement"
            inv = fit_lanes(o, z, inv_c, half) or inv
    # Outside pavement edges: of the inventory lanes alone, or of whichever
    # roadway lies on each side of the centerline.
    if basis in ("HPMS line", "DEM pavement"):
        lo, hi = (inv_c, half), (inv_c, half)
    else:
        oc, oh = (opp["center"], opp["half"]) if opp is not None else (2 * center, half)
        lo, hi = ((oc, oh), (inv_c, half)) if oc < inv_c else ((inv_c, half), (oc, oh))
    left_edge = _plane_edge(o, z, lo[0] - lo[1], lo[0], -1)
    right_edge = _plane_edge(o, z, hi[0], hi[0] + hi[1], +1)
    fl = foreslope_hv(o, z, left_edge, -1) if left_edge is not None else None
    fr = foreslope_hv(o, z, right_edge, +1) if right_edge is not None else None

    def r(v, n=2):
        return round(float(v), n) if v is not None and np.isfinite(v) else np.nan

    rec = {
        "Center_Basis": basis,
        "Center_Offset_Ft": round(center, 1),
        "Centerline_Elev_Ft": r(np.interp(center, o, z)),
        "Lane_Half_Width_Ft": half,
        "Inv_Center_Ft": round(inv_c - center, 1),
        "Opp_Center_Ft": round(opp["center"] - center, 1) if opp else np.nan,
        "Opp_Line_Ft": round(guide["opp"] - center, 1) if guide["opp"] is not None else np.nan,
        "Left_Edge_Ft": r(left_edge - center, 1) if left_edge is not None else np.nan,
        "Right_Edge_Ft": r(right_edge - center, 1) if right_edge is not None else np.nan,
        "Foreslope_Left_HV": r(fl, 1) if fl else np.nan,
        "Foreslope_Right_HV": r(fr, 1) if fr else np.nan,
    }
    for tag, lanes in (("Inv", inv), ("Opp", opp)):
        rec[f"{tag}_Elev_Ft"] = r(lanes["level"]) if lanes else np.nan
        rec[f"{tag}_Form"] = lanes["form"] if lanes else ""
        rec[f"{tag}_Slope_Pct"] = r(lanes["slope"]) if lanes else np.nan
        rec[f"{tag}_Left_Slope_Pct"] = r(lanes["left"]) if lanes else np.nan
        rec[f"{tag}_Right_Slope_Pct"] = r(lanes["right"]) if lanes else np.nan
        rec[f"{tag}_Crown_Ft"] = r(lanes["crown"] - center, 1) if lanes and np.isfinite(lanes["crown"]) else np.nan
    rec["Inv_Note"] = "Doubtful" if _inv_doubtful(inv) else ""
    rec["Opp_Note"] = "Doubtful" if _opp_doubtful(inv, opp) else ""
    return rec


def _inv_doubtful(inv: Optional[Dict]) -> bool:
    """Inventory lane fit that may lie on side slope rather than pavement:
    not pavement-like, or crowned with the side covering more of the fitted
    width steeper than _OPP_DOUBT_SLOPE_PCT. A crown near one edge is
    usually a shoulder breaking away and says nothing about the lanes (as in
    _lane_cross_slope); a steep planar fit is left alone, since on a curve
    it is superelevation."""
    if inv is None:
        return False
    if not _pavement_like(inv):
        return True
    if inv["form"] != "Crowned":
        return False
    left_width = min(max(inv["crown"] - (inv["center"] - inv["half"]), 0.0), 2 * inv["half"])
    main = inv["right"] if left_width < inv["half"] else inv["left"]
    return bool(abs(main) > _OPP_DOUBT_SLOPE_PCT)


def _opp_doubtful(inv: Optional[Dict], opp: Optional[Dict]) -> bool:
    """Opposing lane fit that may lie on side slope rather than pavement:
    not pavement-like, or steeper than _OPP_DOUBT_SLOPE_PCT where the
    inventory lanes are not superelevated alike (both planar, sloping the
    same way)."""
    if opp is None:
        return False
    if not _pavement_like(opp):
        return True
    steepest = np.nanmax(np.abs([opp["slope"], opp["left"], opp["right"]]))
    if steepest <= _OPP_DOUBT_SLOPE_PCT:
        return False
    alike = (inv is not None and inv["form"] == "Planar" and opp["form"] == "Planar"
             and inv["slope"] * opp["slope"] > 0)
    return not alike


def measure_section(frame: RouteFrame, dem: DemTiles, dist_ft: float,
                    hpms_sub: Optional[pd.DataFrame] = None,
                    nearby: Optional["NearbyLines"] = None) -> Optional[Dict]:
    """Cuts and measures one cross section. Returns the measurements plus
    the profile re-centered on the roadway centerline, or None if the DEM
    does not cover the station."""
    o, z = frame.cut(dem, dist_ft)
    if not np.isfinite(z[_at(o, 0.0)]):
        return None
    i = frame.index(dist_ft)
    rec = {"Dist_Ft": float(frame.dist[i]), "Milepost": float(frame.mp[i]),
           "Lon": float(frame.lon[i]), "Lat": float(frame.lat[i])}
    guide = nearby.guide(*frame.axis(dist_ft)) if nearby is not None else None
    rec.update(measure_profile(o, z, _hpms_attrs(hpms_sub, rec["Milepost"]), guide))
    center = rec["Center_Offset_Ft"]
    oc = np.arange(-XS_HALF_WIDTH_FT, XS_HALF_WIDTH_FT + 0.5 * _STEP_FT, _STEP_FT)
    zc = np.interp(oc + center, o, z, left=np.nan, right=np.nan)
    return {"rec": rec, "offset": oc, "elev": zc}


# ---------------------------------------------------------------------------
# Superelevation and stations
# ---------------------------------------------------------------------------
def measure_curve_superelevation(frame: RouteFrame, dem: DemTiles, start_ft: float, end_ft: float,
                                 half: float, street: bool = False,
                                 span: Optional[np.ndarray] = None) -> Dict:
    """Superelevation through one curve (see module docstring). Samples on a
    bridge or tunnel span (span, one flag per vertex) are skipped. Returns
    the measured value (NaN for normal crown), "NC" or "", the
    full-superelevation extent, the turn sign and the number of samples."""
    turn = frame.turn_sign(start_ft, end_ft)
    d = np.arange(start_ft, end_ft + 1e-6, XS_SUPER_STEP_FT)
    if d.size < 3:
        d = np.linspace(start_ft, end_ft, 3)
    e = np.full(d.size, np.nan)
    crowned = np.zeros(d.size, dtype=bool)
    for k, dk in enumerate(d):
        if not frame.dist[0] <= dk <= frame.dist[-1]:
            continue
        if span is not None and span[frame.index(dk)]:
            continue
        o, z = frame.cut(dem, dk, reach_ft=half + _MAX_PAVEMENT_SHIFT_FT + _EDGE_SEARCH_FT + 10)
        c = center_on_pavement(o, z, half) if street else 0.0
        lanes, _ = _fit_any(o, z, c, _lane_halves(half))
        if lanes is None:
            continue
        # A crown, or a single slope falling toward the outside of the curve
        # (one side of a crown, or a corner in town), is no superelevation.
        if lanes["form"] == "Crowned" or lanes["slope"] * turn < 0:
            crowned[k] = True
        else:
            e[k] = lanes["slope"] * turn
    out = {"turn": turn, "e": np.nan, "note": "", "full_start": np.nan, "full_end": np.nan, "n": 0}
    if span is not None and all(span[frame.index(dk)] for dk in d):
        out["note"] = "Structure"
        return out
    n = len(d)
    mid = slice(n // 4, n - n // 4)
    n_mid = int(np.isfinite(e[mid]).sum() + crowned[mid].sum())
    if n_mid == 0:
        return out
    if crowned[mid].sum() > n_mid / 2:
        out.update({"note": "NC", "full_start": float(d[0]), "full_end": float(d[-1]), "n": int(crowned.sum())})
        return out
    plateau = float(np.nanmedian(e[mid]))
    if plateau >= XS_MIN_SUPER_PCT:
        full = np.flatnonzero(np.isfinite(e) & (e >= XS_FULL_SUPER_FRAC * plateau))
        a, b = int(full[0]), int(full[-1])
    else:
        a, b = n // 4, n - 1 - n // 4
    run = e[a:b + 1]
    out.update({"e": float(np.nanmedian(run)), "full_start": float(d[a]), "full_end": float(d[b]),
                "n": int(np.isfinite(run).sum())})
    return out


def _off_structure(dist: np.ndarray, span: np.ndarray, d: float) -> float:
    """Moves a station that falls on a bridge or tunnel span just past the
    nearer end of the span, where the DEM shows the roadway."""
    i = int(np.clip(np.searchsorted(dist, d), 0, len(dist) - 1))
    if not span[i]:
        return d
    a = i
    while a > 0 and span[a - 1]:
        a -= 1
    b = i
    while b < len(dist) - 1 and span[b + 1]:
        b += 1
    before, after = dist[a] - _MIN_STATION_GAP_FT, dist[b] + _MIN_STATION_GAP_FT
    if before < dist[0]:
        return float(min(after, dist[-1]))
    if after > dist[-1]:
        return float(before)
    return float(before if d - before <= after - d else after)



def build_cross_sections(vtx: pd.DataFrame, curves: List[Dict], tile_dir: str,
                         hpms_sub: Optional[pd.DataFrame] = None,
                         nearby_lines: Optional[pd.DataFrame] = None):
    """
    Measures superelevation for every curve (adding Curve_No,
    Superelevation_Meas_Pct, Superelevation_Note, Super_Full_Start_Dist and
    Super_Full_End_Dist to each curve dict, distances in meters like
    Start_Dist) and cuts the station cross sections. Returns
    (stations_df, profiles_df); profiles_df holds Station_ID, Offset_Ft,
    Elev_Ft at 1 ft spacing. nearby_lines (from select_nearby_lines) places
    the opposing roadway where HPMS has a line for that direction.
    """
    frame = RouteFrame(vtx)
    dem = DemTiles(tile_dir)
    nearby = NearbyLines(nearby_lines, frame)
    d_min, d_max = float(frame.dist[0]), float(frame.dist[-1])
    span = (vtx["Structure_Kind"].isin(["Bridge", "Tunnel"]).to_numpy()
            if "Structure_Kind" in vtx.columns else np.zeros(len(frame.dist), dtype=bool))

    stations = []
    for no, c in enumerate(sorted(curves, key=lambda c: c["Start_Dist"]), start=1):
        c["Curve_No"] = no
        s_ft = c["Start_Dist"] * FEET_PER_METER
        e_ft = c["End_Dist"] * FEET_PER_METER
        mid_ft = (s_ft + e_ft) / 2
        hp = _hpms_attrs(hpms_sub, float(frame.mp[frame.index(mid_ft)]))
        paired = paired_with_opposing_line(hp, nearby.guide(*frame.axis(mid_ft)))
        street = not paired and (single_roadway(hp) or (hp["median_type"] is None and not divided(hp)))
        half = lane_half_width(dict(hp, median_type=1) if street else hp, paired)
        sup = measure_curve_superelevation(frame, dem, s_ft, e_ft, half, street, span)
        c["Superelevation_Meas_Pct"] = round(sup["e"], 2) if np.isfinite(sup["e"]) else np.nan
        c["Superelevation_Note"] = sup["note"]
        c["Super_Full_Start_Dist"] = sup["full_start"] / FEET_PER_METER if np.isfinite(sup["full_start"]) else np.nan
        c["Super_Full_End_Dist"] = sup["full_end"] / FEET_PER_METER if np.isfinite(sup["full_end"]) else np.nan
        c["Super_Samples"] = sup["n"]
        if sup["note"] == "NC":
            spots = ((s_ft, "Curve start (NC)"), (mid_ft, "Mid-curve"), (e_ft, "Curve end (NC)"))
        else:
            fs = sup["full_start"] if np.isfinite(sup["full_start"]) else s_ft + 0.25 * (e_ft - s_ft)
            fe = sup["full_end"] if np.isfinite(sup["full_end"]) else e_ft - 0.25 * (e_ft - s_ft)
            spots = ((fs, "Full super start"), (mid_ft, "Mid-curve"), (fe, "Full super end"))
        placed = []
        for d, kind in spots if sup["note"] != "Structure" else ():
            d = _off_structure(frame.dist, span, float(np.clip(d, d_min, d_max)))
            if not s_ft <= d <= e_ft:
                continue    # the curve lies on the structure there
            if placed and abs(d - placed[-1]["Dist_Ft"]) < _MIN_STATION_GAP_FT / 5:
                placed[-1]["Station_Type"] += " / " + kind
                continue
            placed.append({"Dist_Ft": d, "Station_Type": kind, "Curve_No": no, "Curve_Dir": c.get("Dir", ""),
                           "Curve_Superelevation_Pct": c["Superelevation_Meas_Pct"],
                           "Curve_Super_Note": sup["note"]})
        stations += placed

    curve_d = np.array([s["Dist_Ft"] for s in stations])
    fixed = sorted([d_min, d_max] + list(curve_d))
    fill = [{"Dist_Ft": d_min, "Station_Type": "Route start"}, {"Dist_Ft": d_max, "Station_Type": "Route end"}]
    for a, b in zip(fixed[:-1], fixed[1:]):
        n_gap = int(math.ceil((b - a) / XS_TANGENT_MAX_GAP_FT)) - 1
        for k in range(1, n_gap + 1):
            fill.append({"Dist_Ft": a + k * (b - a) / (n_gap + 1), "Station_Type": "Tangent"})
    kept = []
    for s in sorted(fill, key=lambda s: s["Dist_Ft"]):
        s["Dist_Ft"] = _off_structure(frame.dist, span, s["Dist_Ft"])
        taken = np.r_[curve_d, [k["Dist_Ft"] for k in kept]]
        if taken.size == 0 or np.min(np.abs(taken - s["Dist_Ft"])) > _MIN_STATION_GAP_FT:
            kept.append(s)
    stations = sorted(stations + kept, key=lambda s: s["Dist_Ft"])

    rows, prof = [], []
    for s in stations:
        res = measure_section(frame, dem, s["Dist_Ft"], hpms_sub, nearby)
        if res is None:
            continue
        sid = f"XS-{len(rows) + 1}"
        r = {"Station_ID": sid, "Station_Type": s["Station_Type"],
             "Curve_No": s.get("Curve_No", np.nan), "Curve_Dir": s.get("Curve_Dir", ""),
             "Curve_Superelevation_Pct": s.get("Curve_Superelevation_Pct", np.nan),
             "Curve_Super_Note": s.get("Curve_Super_Note", "")}
        r.update(res["rec"])
        rows.append(r)
        prof.append(pd.DataFrame({"Station_ID": sid, "Offset_Ft": res["offset"],
                                  "Elev_Ft": np.round(res["elev"], 2)}))
    stations_df = pd.DataFrame(rows, columns=STATION_COLUMNS)
    stations_df["Milepost"] = stations_df["Milepost"].round(4)
    stations_df["Dist_Ft"] = stations_df["Dist_Ft"].round(1)
    stations_df["Line_Off_Road"] = flag_line_off_road(stations_df, line_ground_roughness(vtx))
    profiles_df = pd.concat(prof, ignore_index=True) if prof else pd.DataFrame(
        columns=["Station_ID", "Offset_Ft", "Elev_Ft"])
    return stations_df, profiles_df


def _lane_cross_slope(stations: pd.DataFrame) -> pd.Series:
    """
    Cross slope (absolute, %) of the inventory lanes at each station. A
    planar fit has one slope. For a crowned fit, the slope of the side
    covering more of the fitted width: a crown near one edge of the lanes
    is usually the inside shoulder breaking toward the median, and that
    narrow strip's slope says nothing about whether lanes are there.
    """
    planar = stations["Inv_Slope_Pct"].abs()
    left = stations["Inv_Left_Slope_Pct"].abs()
    right = stations["Inv_Right_Slope_Pct"].abs()
    half = stations["Lane_Half_Width_Ft"]
    left_width = (stations["Inv_Crown_Ft"] - (stations["Inv_Center_Ft"] - half)).clip(lower=0)
    left_width = np.minimum(left_width, 2 * half)
    main_side = right.where(left_width < half, left)
    # Without the crown position, fall back to the steeper side.
    main_side = main_side.fillna(pd.concat([left, right], axis=1).max(axis=1))
    return planar.fillna(main_side)


def line_ground_roughness(vtx: pd.DataFrame) -> Optional[Tuple[np.ndarray, np.ndarray]]:
    """
    (Dist_Ft, roughness in ft) along the HPMS line: the absolute departure of
    the DEM along the line (Elev_Raw_Ft) from its running median over
    _OFF_ROAD_ROUGH_WINDOW_FT. NaN on bridges and tunnels backed by an HPMS
    or NBI/NTI record and within _OFF_ROAD_STRUCTURE_BUFFER_FT of them,
    where the DEM shows the ground below rather than the road. Spans found
    from the DEM alone (3_DIP) are kept: where the line has left the road,
    the uneven ground beside it reads as gaps and humps, and leaving those
    out would hide the very ground that shows the line is off the road.
    None if the vertices lack Elev_Raw_Ft.
    """
    if "Elev_Raw_Ft" not in vtx.columns or len(vtx) < 3:
        return None
    dist = vtx["Dist_Ft"].to_numpy(dtype=float)
    z = vtx["Elev_Raw_Ft"].to_numpy(dtype=float)
    step = float(np.median(np.diff(dist)))
    if not np.isfinite(step) or step <= 0:
        return None
    win = max(3, int(round(_OFF_ROAD_ROUGH_WINDOW_FT / step)) | 1)
    trend = pd.Series(z).rolling(win, center=True, min_periods=3).median().to_numpy()
    rough = np.abs(z - trend)
    if "Structure_Kind" in vtx.columns:
        on = vtx["Structure_Kind"].isin(["Bridge", "Tunnel"]).to_numpy().copy()
        if "Structure_Tier" in vtx.columns:
            on &= vtx["Structure_Tier"].isin(["1_HPMS", "2_NBI"]).to_numpy()
        if on.any():
            k = int(round(_OFF_ROAD_STRUCTURE_BUFFER_FT / step))
            near = pd.Series(on.astype(float)).rolling(2 * k + 1, center=True, min_periods=1).max()
            rough[near.to_numpy() > 0] = np.nan
    return dist, rough


def _ground_rough(ground: Optional[Tuple[np.ndarray, np.ndarray]], lo_ft: float, hi_ft: float) -> Optional[bool]:
    """Whether the ground along the HPMS line between lo_ft and hi_ft is rough:
    the median roughness over some _OFF_ROAD_ROUGH_SPAN_FT stretch of it
    (shorter if the run is shorter) is above _OFF_ROAD_ROUGH_FT. None if too
    little usable ground lies there to judge."""
    if ground is None:
        return None
    dist, rough = ground
    sel = (dist >= lo_ft) & (dist <= hi_ft) & np.isfinite(rough)
    if sel.sum() < 2 or dist[sel][-1] - dist[sel][0] < _OFF_ROAD_MIN_GROUND_FT:
        return None
    d, r = dist[sel], rough[sel]
    span = min(_OFF_ROAD_ROUGH_SPAN_FT, d[-1] - d[0])
    for a in d[d <= d[-1] - span]:
        w = (d >= a) & (d <= a + span)
        if w.sum() >= 2 and np.median(r[w]) > _OFF_ROAD_ROUGH_FT:
            return True
    return False


def flag_line_off_road(stations: pd.DataFrame,
                       ground: Optional[Tuple[np.ndarray, np.ndarray]] = None) -> np.ndarray:
    """
    True at stations where the HPMS line appears to lie off the roadway,
    typically an old alignment where the DEM shows new construction: in a
    run of at least _OFF_ROAD_MIN_STATIONS stations, no inventory lanes are
    found at the line (or they slope more than _OFF_ROAD_SLOPE_PCT, see
    _lane_cross_slope) and either the centerline is more than
    _OFF_ROAD_OFFSET_FT beyond the route's median offset from the line or it
    was placed from the other roadway (_DIVIDED_CENTER_BASES; where both
    directions' lines follow an old alignment, the centerline lands midway
    between them at the usual offset), and the ground along the
    HPMS line is rough somewhere in the run, extended halfway to the
    neighboring stations (ground from line_ground_roughness; see
    _ground_rough). Without
    ground, or with too little of it off structures, the station test
    decides alone.
    """
    n = len(stations)
    if n == 0:
        return np.zeros(0, dtype=bool)
    dist = stations["Dist_Ft"].to_numpy(dtype=float)
    off = stations["Center_Offset_Ft"].abs()
    far = (off - off.median()) > _OFF_ROAD_OFFSET_FT
    if "Center_Basis" in stations.columns:
        far = far | stations["Center_Basis"].isin(_DIVIDED_CENTER_BASES)
    no_lanes = stations["Inv_Elev_Ft"].isna() | (_lane_cross_slope(stations) > _OFF_ROAD_SLOPE_PCT)
    suspect = (far & no_lanes).to_numpy()
    flag = np.zeros(n, dtype=bool)
    i = 0
    while i < n:
        if not suspect[i]:
            i += 1
            continue
        j = i
        while j + 1 < n and suspect[j + 1]:
            j += 1
        if j - i + 1 >= _OFF_ROAD_MIN_STATIONS:
            lo = (dist[i - 1] + dist[i]) / 2 if i > 0 else dist[i]
            hi = (dist[j] + dist[j + 1]) / 2 if j + 1 < n else dist[j]
            if _ground_rough(ground, lo, hi) is not False:
                flag[i:j + 1] = True
        i = j + 1
    return flag


def line_off_road_ranges(stations: pd.DataFrame) -> List[Tuple[float, float]]:
    """Milepost ranges of the flagged runs (Line_Off_Road), each extended
    halfway to the neighboring unflagged stations."""
    if stations.empty or "Line_Off_Road" not in stations.columns:
        return []
    mp = stations["Milepost"].to_numpy(dtype=float)
    flag = stations["Line_Off_Road"].to_numpy(dtype=bool)
    ranges = []
    i, n = 0, len(mp)
    while i < n:
        if not flag[i]:
            i += 1
            continue
        j = i
        while j + 1 < n and flag[j + 1]:
            j += 1
        lo = (mp[i - 1] + mp[i]) / 2 if i > 0 else mp[i]
        hi = (mp[j] + mp[j + 1]) / 2 if j + 1 < n else mp[j]
        ranges.append((float(lo), float(hi)))
        i = j + 1
    return ranges