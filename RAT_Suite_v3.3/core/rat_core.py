# core/rat_core.py

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
RAT CORE ENGINE v3.3 (Mathematical & Geospatial Backend)
--------------------------------------------------------------------------------
ROLE: Central processing engine for the Roadway Alignment Tool (RAT) Suite.
DESCRIPTION:
This module contains no GUI or plotting code. It provides the mathematical and
geospatial processing functions used by all suite modules, including UTM
coordinate projection, USGS DEM elevation retrieval, UnivariateSpline smoothing,
calculus-based curvature analysis (kappa), and KDTree spatial indexing for
4D geometry generation.
"""
from typing import Dict, List, Tuple, Optional
import math
import os
import glob
import json
import logging
import time
import random
import threading
import numpy as np
import pandas as pd
import geopandas as gpd
import rasterio
import requests
from shapely.geometry import LineString, Point, shape
from shapely.wkt import loads
from shapely.ops import substring
from scipy.interpolate import UnivariateSpline
from scipy.ndimage import distance_transform_edt
from scipy.signal import butter, filtfilt
from scipy.stats import linregress
from pyproj import Transformer
from scipy.spatial import cKDTree

# -------------------------
# Constants
# -------------------------
FEET_PER_METER = 3.28084
METRIC_R_TO_IMPERIAL_D = 1746.38  # deg per 100 ft from radius(m): D = 1746.38/R(m)

# The smoothing factors (H_SMOOTH_FACTOR and its FS variants, and the
# national V_SMOOTH_FACTOR) are set on one-mile chunks densified at 10 ft.
# A spline's s is a total squared residual, so it grows with the number of
# points it is fitted to; spline_s() scales each factor to the points
# actually fitted, so a factor smooths the same whatever the chunk length or
# point spacing.
SMOOTH_REF_POINTS = 1609.344 / (10.0 / FEET_PER_METER)


def spline_s(factor: float, n_points: int) -> float:
    """UnivariateSpline s for a smoothing factor applied to n_points."""
    return float(factor) * n_points / SMOOTH_REF_POINTS


# One vertical smoothing factor nationwide: the elevations all come from the
# USGS 3DEP 1 m DEM. The factor fixes the profile's RMS departure from the
# DEM at sqrt(factor / SMOOTH_REF_POINTS) m (10 -> 0.45 ft). Chosen from
# test routes on I-70 (CO) and US-20 (NE): the number of detected vertical
# curves falls as factors rise from 1 (DEM undulations read as short curves)
# and holds steady from about 8.5-10 to 20-25 before real curves begin to
# merge; 10 is the start of that range.
NATIONAL_V_SMOOTH_FACTOR = 10.0

# Horizontal smoothing factors by functional system where no state value is
# available: the lower median of the calibrated state values
# (national calibration of October 27, 2026, with the 6 ft departure limit
# at curve points).
NATIONAL_DEFAULTS = {
    "H": {1: 75, 2: 100, 3: 30, 4: 30, 5: 30, 6: 30, 7: 75},
}

# ===========================================================================
# CONFIGURATION: vertical curve classes
# ===========================================================================
# AASHTO stopping sight distance on level roadway (A Policy on Geometric
# Design of Highways and Streets, 7th Edition, 2018, Table 3-1):
# design speed (mph) -> SSD (ft).
SSD_TABLE_FT = {
    15: 80, 20: 115, 25: 155, 30: 200, 35: 250, 40: 305, 45: 360,
    50: 425, 55: 495, 60: 570, 65: 645, 70: 730, 75: 820, 80: 910,
}
# Vertical curve class by the design speed its sight distance supports:
# the lowest supported speed (mph) in each class. Below the E value is F.
VC_CLASS_MIN_SPEED_MPH = {"A": 70, "B": 60, "C": 50, "D": 40, "E": 30}
# Reference speed by functional system where the HPMS record carries no
# speed limit. Used only for the Meets_Speed_Ref comparison.
DEFAULT_SPEED_BY_FS_MPH = {1: 75, 2: 65, 3: 55, 4: 45, 5: 35, 6: 30, 7: 25}
# ===========================================================================

SOCRATA_DEFAULT = "https://datahub.transportation.gov/resource/42um-tgh5.json"

DEFAULTS = {
    # --- Geometry preprocessing ---
    'DENSIFY_SPACING_FT':          5.0,
    # Shortest piece of road processed, in densified points (about 110 ft
    # at the 5 ft spacing). Shorter pieces are skipped.
    'H_MIN_CHUNK_POINTS':          23,
    'TRIM_CURVE_ENDPOINTS':        True,

    # --- Horizontal curve detection ---
    'H_MIN_DELTA':                 3.5,
    'H_MIN_CURVE_LENGTH_FT':       100.0,
    'H_MAX_RADIUS_FT':             165000.0,
    'H_USE_HMM_CLASSIFICATION':    True,
    'H_HMM_STICKINESS':            1.0,

    # --- Vertical curve detection ---
    'V_VC_THRESHOLD':              0.002,
    'V_MIN_CURVE_LENGTH_FT':       200.0,
    'V_GAP_TOLERANCE':             5,
    'V_MIN_GRADE_CHANGE':          0.5,
    'V_MIN_OFFSET_FT':             0.10,
    'REGRESSION_WINDOW_FT':        500.0,
    'V_USE_HMM_CLASSIFICATION':    True,
    'V_HMM_STICKINESS':            1.0,

    # --- Structure (bridge/tunnel) profile repair ---
    # A bridge in a bare-earth DEM shows as a gap (sharp drop, sharp rise);
    # a tunnel/covered section shows as a hump (sharp rise, sharp drop).
    # Edges steeper than STRUCTURE_EDGE_GRADE_PCT are treated as DEM
    # discontinuities, since no real roadway grade is that steep.
    'DIP_THRESHOLD_FT':            3.0,     # min gap depth / hump height, and min edge height
    'STRUCTURE_EDGE_GRADE_PCT':    10.0,
    'BRIDGE_MAX_LEN_FT':           8200.0,
    # Longest bridge spanned along an HPMS bridge record when the DEM edges
    # do not give the span (long viaducts over a sloping shore, such as the
    # Newport and Mount Hope bridges in Rhode Island). Not in the GUI.
    'BRIDGE_RECORD_MAX_LEN_FT':    30000.0,
    'TUNNEL_MAX_LEN_FT':           10000.0,
    'STRUCTURE_MATCH_TOL_FT':      300.0,   # HPMS/NBI record within this distance labels a span

    # --- Merge controls ---
    'ENABLE_MERGE':                False,
    'MERGE_GAP_FLOOR_FT':          50.0,
    'MERGE_GAP_PCT':               0.25,

    # --- Chunk stitching / parallel-engine controls ---
    # Largest gap still joined when walking segments in ShapeId order
    # (stitch_linestrings_by_shapeid). Digitized centerlines can have gaps
    # of 50-100+ ft at intersections and interchanges where the roadway is
    # continuous.
    'CHUNK_JOIN_TOLERANCE_FT':     100.0,
    # A continuity chunk longer than this gets split into sub-chunks so
    # a single parallel worker isn't handed an entire multi-hundred-mile
    # Interstate. Sub-chunks are read with CHUNK_SPLIT_PADDING_FT of extra
    # geometry on each side purely for smoothing/curvature context; each
    # sub-chunk still only emits curves/vertices inside its own true
    # (unpadded) MP range, so results across sub-chunks never overlap.
    'MAX_CHUNK_SPLIT_MILES':       25.0,
    # Padding read beyond a sub-chunk's true boundary, sized from the
    # longest observed curve length across multiple states (~8,444 ft in
    # Delaware) with roughly 2.4x safety margin. If a future state's
    # longest curve approaches this value, it should be raised --
    # process_chunk() logs a warning when that happens.
    'CHUNK_SPLIT_PADDING_FT':      20000.0,
    # True lists, in the Run Log, the functional system chosen for each
    # piece of every route that spans more than one (otherwise one summary
    # line per state).
    'LOG_FSYSTEM_DETAIL':          False,

    # --- Smoothing factors: H by functional system, V national ---
    'H_SMOOTH_FACTOR':     NATIONAL_DEFAULTS["H"][1],
    'H_SMOOTH_FACTOR_FS2': NATIONAL_DEFAULTS["H"][2],
    'H_SMOOTH_FACTOR_FS3': NATIONAL_DEFAULTS["H"][3],
    'H_SMOOTH_FACTOR_FS4': NATIONAL_DEFAULTS["H"][4],
    'H_SMOOTH_FACTOR_FS5': NATIONAL_DEFAULTS["H"][5],
    'H_SMOOTH_FACTOR_FS6': NATIONAL_DEFAULTS["H"][6],
    'H_SMOOTH_FACTOR_FS7': NATIONAL_DEFAULTS["H"][7],
    'V_SMOOTH_FACTOR':     NATIONAL_V_SMOOTH_FACTOR,
}

# -------------------------
# Structure records along a route: 0 = none, 1 = HPMS bridge/tunnel record,
# 2 = NBI/NTI structure point. Used to label the spans found in the DEM.
# -------------------------
def build_structure_mask(
    mileposts: np.ndarray,
    coords_wgs: List[Tuple[float, float]],
    route_id: str,
    hpms_structures: pd.DataFrame = None,
    nbi_nti_gdf: gpd.GeoDataFrame = None,
    spatial_tolerance_deg: float = 0.00015,
    records_out: Optional[List[Dict]] = None
) -> np.ndarray:
    """
    Marks the points of a chunk that a structure record covers: 1 where an
    HPMS record on the route has Structure_Type 1 (bridge) or 2 (tunnel)
    and covers the point's reference point, else 2 where an NBI or NTI
    point lies within spatial_tolerance_deg of the point, else 0.

    records_out, if a list, receives one entry per record found, for
    find_structure_spans() to test spans against:
      {"source": 1, "kind": 1 or 2, "start": i, "end": j} for an HPMS
          record covering points i to j;
      {"source": 2, "kind": 1 or 2, "at": k, "length_m": L} for an NBI
          bridge or NTI tunnel point nearest point k (L NaN if unknown).
    """
    n_pts = len(mileposts)
    tier_mask = np.zeros(n_pts, dtype=int)

    # TIER 1: HPMS Structure Limits
    if hpms_structures is not None and not hpms_structures.empty and "Structure_Type" in hpms_structures.columns:
        route_structs = hpms_structures[
            (hpms_structures["RouteId"] == route_id) &
            (hpms_structures["Structure_Type"].isin([1, 2]))
        ]
        for _, row in route_structs.iterrows():
            s_mp = float(row.get("Start_MP", 0.0))
            e_mp = float(row.get("End_MP", 0.0))
            if s_mp < e_mp:
                hit = (mileposts >= s_mp) & (mileposts <= e_mp)
                tier_mask[hit] = 1
                idx = np.flatnonzero(hit)
                if records_out is not None and idx.size:
                    records_out.append({"source": 1, "kind": int(row["Structure_Type"]),
                                        "start": int(idx[0]), "end": int(idx[-1])})

    # TIER 2: NBI/NTI points within spatial_tolerance_deg of a vertex, found
    # with a KDTree in projected coordinates (statewide runs can have more
    # than 100 million vertices, too many for a spatial join).
    if nbi_nti_gdf is not None and not nbi_nti_gdf.empty and len(coords_wgs) > 0:
        # Determine UTM zone from the first route coordinate
        lon0, lat0 = coords_wgs[0]
        utm_epsg = get_appropriate_utm_zone(lon0, lat0)
        fwd = Transformer.from_crs("EPSG:4326", f"EPSG:{utm_epsg}", always_xy=True)

        # Project structure points to UTM
        sx, sy = fwd.transform(nbi_nti_gdf.geometry.x.to_numpy(), nbi_nti_gdf.geometry.y.to_numpy())
        struct_xy = np.column_stack([sx, sy]).astype(float)
        kinds = (nbi_nti_gdf["kind"].to_numpy() if "kind" in nbi_nti_gdf.columns
                 else np.ones(len(nbi_nti_gdf), dtype=int))
        lengths = (nbi_nti_gdf["length_m"].to_numpy(dtype=float) if "length_m" in nbi_nti_gdf.columns
                   else np.full(len(nbi_nti_gdf), np.nan))

        # Drop points that projected to Infinity or NaN
        valid = np.isfinite(struct_xy).all(axis=1)
        struct_xy, kinds, lengths = struct_xy[valid], kinds[valid], lengths[valid]

        if len(struct_xy) > 0:
            struct_tree = cKDTree(struct_xy)
            # Convert the degree tolerance to approximate metres at this latitude
            tol_m = spatial_tolerance_deg * 111_320.0
            lon_lat = np.asarray(coords_wgs, dtype=float)
            vx, vy = fwd.transform(lon_lat[:, 0], lon_lat[:, 1])
            vert_xy = np.column_stack([vx, vy])
            nearest = {}     # structure point -> (distance, nearest vertex)

            # query_ball_point in chunks to keep peak memory bounded
            CHUNK = 50_000
            for start in range(0, n_pts, CHUNK):
                end = min(start + CHUNK, n_pts)
                hits = struct_tree.query_ball_point(vert_xy[start:end], r=tol_m)
                for local_i, hit_list in enumerate(hits):
                    if not hit_list:
                        continue
                    i = start + local_i
                    if tier_mask[i] == 0:
                        tier_mask[i] = 2
                    if records_out is not None:
                        for h in hit_list:
                            dist = float(np.hypot(*(vert_xy[i] - struct_xy[h])))
                            if h not in nearest or dist < nearest[h][0]:
                                nearest[h] = (dist, i)
            if records_out is not None:
                for h, (_, i) in nearest.items():
                    records_out.append({"source": 2, "kind": int(kinds[h]), "at": int(i),
                                        "length_m": float(lengths[h])})

    return tier_mask

# -------------------------
# Structure (bridge/tunnel) profile repair
# -------------------------
STRUCTURE_KIND_LABELS = {0: "None", 1: "Bridge", 2: "Tunnel"}
STRUCTURE_TIER_LABELS = {0: "None", 1: "1_HPMS", 2: "2_NBI", 3: "3_DIP"}
STRUCTURE_RECORD_LABELS = {0: "None", 1: "HPMS", 2: "NBI"}

_EDGE_MERGE_FT = 25.0       # steep pieces closer than this form one edge (stair-stepped embankments)
_ROAD_LEVEL_FIT_FT = 50.0   # short road-side fit used to read abutment/portal elevations
_SPAN_INSIDE_FRAC = 0.9     # share of interior points that must sit on the gap/hump side of the span line
_SPAN_SPLINE_WEIGHT = 100.0 # vertical spline weight on span points (see smooth_plan_profile_from_linestring)
_SPAN_WEIGHT_TAPER_FT = 200.0  # span weight rises from 1 at each end to full over this distance
_SPAN_EXTEND_EDGES = 40     # further edges tried on each side (benches, the far face of a mountain)
_SPAN_END_CHECK_FT = 150.0  # ground checked beyond each span end
_SPAN_END_MAX_GRADE = 0.15  # steeper than this beyond an end is hillside, not road
_SPAN_MAX_GRADE = 0.15      # no bridge deck or tunnel is steeper than this
_SPAN_GRADE_IMPROVE = 0.02  # an extended span must match the road grade better by this much (2%)
_SPAN_BENCH_FT = 200.0      # a bridge end with another edge stepping the same way this close beyond it is on a bench
_BENCH_ROAD_FT = 100.0      # ...unless this much ground at road level lies first, before an edge stepping back
_SPAN_END_FIT_FT = (50.0, 200.0)  # road line fitted this far beyond each span end, to place the end
_SPAN_END_DEPART_FT = 0.5   # ground this far off that line (toward the gap/hump) is not road
_SPAN_END_MAX_MOVE_FT = 100.0
_RECORD_RUN_GAP_FT = 100.0  # HPMS bridge records closer than this form one structure
_END_EDGE_FT = 50.0         # an edge reaching this close to either end of the profile: the data ends in the gap
_END_EDGE_MAX_FT = 150.0    # ...held level only if the stretch from the edge to that end is no longer than this
# A span that no structure record supports (see _structure_stretches) must
# show a structure face at both ends: near each end the ground rises or
# falls by at least _DIP_FACE_MIN_FT within _DIP_FACE_RUN_FT (3.5 ft within
# 15 ft, an average slope of about 23%). The two sides of a bridge gap do; a steep
# pitch of a mountain road (Pikes Peak), rough ground beside an old
# alignment, or noise in a coarse DEM (Seward Highway, Alaska) does not. Such a span must also be no longer than
# twice STRUCTURE_MATCH_TOL_FT: NBI lists every public bridge longer than
# 20 ft, so a longer bridge would have an NBI point near it. A tunnel is
# never found from the DEM alone: every highway tunnel is in the NTI, and a
# hump with no tunnel record is a crest, a hillside or the ground beside
# the road.
_DIP_FACE_MIN_FT = 3.5
_DIP_FACE_RUN_FT = 15.0
_DIP_FACE_OUTSIDE_FT = 60.0   # face searched this far outside each end...
_DIP_FACE_INSIDE_FT = 150.0   # ...and this far inside it (ends are moved out past the face)


def _find_dem_edges(z: np.ndarray, spacing_m: float, edge_grade: float,
                    min_height_m: float) -> List[Tuple[int, int, int]]:
    """Returns (first_idx, last_idx, sign) for each run of near-vertical DEM
    change. sign is -1 for a drop, +1 for a rise."""
    n = len(z)
    g = np.gradient(z, spacing_m)
    steep_idx = np.flatnonzero(np.abs(g) > edge_grade)
    merge_pts = max(1, int(round(_EDGE_MERGE_FT / FEET_PER_METER / spacing_m)))
    edges = []
    k = 0
    while k < len(steep_idx):
        a = b = steep_idx[k]
        sgn = np.sign(g[a])
        k += 1
        while k < len(steep_idx) and steep_idx[k] - b <= merge_pts:
            if np.sign(g[steep_idx[k]]) != sgn:
                break
            b = steep_idx[k]
            k += 1
        height = z[min(b + 1, n - 1)] - z[max(a - 1, 0)]
        if abs(height) >= min_height_m:
            edges.append((int(a), int(b), int(sgn)))
    return edges


def _road_fit(z: np.ndarray, k: int, step: int, spacing_m: float) -> Tuple[float, float]:
    """(elevation at index k, grade) from a short linear fit on the road
    side of k (step=-1 looks backward, +1 forward)."""
    m = max(2, int(round(_ROAD_LEVEL_FIT_FT / FEET_PER_METER / spacing_m)))
    idx = k + step * np.arange(m)
    idx = idx[(idx >= 0) & (idx < len(z))]
    if len(idx) < 2:
        return float(z[k]), 0.0
    coeffs = np.polyfit(idx * spacing_m, z[idx], 1)
    return float(np.polyval(coeffs, k * spacing_m)), float(coeffs[0])


def _outside_grade(z: np.ndarray, k: int, step: int, spacing_m: float) -> Optional[float]:
    """Average grade over _SPAN_END_CHECK_FT of ground beyond index k
    (step=-1 backward, +1 forward), or None near the ends of the profile."""
    m = int(round(_SPAN_END_CHECK_FT / FEET_PER_METER / spacing_m))
    j = k + step * m
    if m < 2 or j < 0 or j >= len(z):
        return None
    return float((z[j] - z[k]) / (step * m * spacing_m))


def _refine_span_end(z: np.ndarray, k: int, step: int, kind: int, spacing_m: float,
                     stop: Optional[int] = None) -> Tuple[int, float]:
    """
    Moves a span end outward (step -1 at the start, +1 at the end) past
    ground that is already rising into a tunnel portal face or falling
    toward a bridge gap, but not yet steeply enough to count as an edge.
    The road line is fitted _SPAN_END_FIT_FT beyond the end, clear of that
    transition. stop (an index beyond the end, such as the near edge of a
    neighboring structure) limits both the fit and the move. Returns
    (index, elevation of the road line there).
    """
    lo, hi = (int(round(d / FEET_PER_METER / spacing_m)) for d in _SPAN_END_FIT_FT)
    idx = k + step * np.arange(lo, hi + 1)
    idx = idx[(idx >= 0) & (idx < len(z))]
    if stop is not None:
        idx = idx[(stop - idx) * step > 0]
    if len(idx) < 5:
        return k, float(z[k])
    coef = np.polyfit(idx * spacing_m, z[idx], 1)
    side = 1.0 if kind == 2 else -1.0
    depart = _SPAN_END_DEPART_FT / FEET_PER_METER
    j = k
    for _ in range(int(_SPAN_END_MAX_MOVE_FT / FEET_PER_METER / spacing_m)):
        nxt = j + step
        if nxt < 0 or nxt >= len(z) or side * (z[j] - np.polyval(coef, j * spacing_m)) <= depart:
            break
        if stop is not None and (stop - nxt) * step <= 0:
            break
        j = nxt
    return j, float(np.polyval(coef, j * spacing_m))


def find_structure_spans(z_vals: np.ndarray, spacing_m: float, params: Dict,
                         structure_mask: Optional[np.ndarray] = None,
                         records: Optional[List[Dict]] = None) -> List[Dict]:
    """
    Finds bridge gaps and tunnel humps directly from the DEM profile.

    A span starts at a near-vertical edge (drop for a bridge, rise for a
    tunnel) and ends at the next opposite edge whose far side is back at
    road level, such that nearly all of the ground in between sits below
    (bridge) or above (tunnel) the straight line joining the two sides by
    at least DIP_THRESHOLD_FT. Each end is then moved out to where the
    ground leaves the road line fitted 50-200 ft beyond it (a portal face
    or embankment can begin rising or falling before it is steep enough
    to be an edge), and its elevation is read from that line.

    A bridge that runs off either end of the profile (a route starting or
    ending mid-river at a state line) has only one abutment in the data.
    With structure_mask (from build_structure_mask), such a bridge is
    spanned from the profile end to that abutment, held level at its
    elevation, where an HPMS or NBI record reaches that end of the profile
    (see _open_bridge_span).

    A span is accepted only if a structure record supports it (a record
    of the same kind whose structure is about as long as the span; see
    _structure_stretches and _supported), or, with no such record, if it
    is a bridge, shows a structure face at both ends and is short
    (_has_face). records are those
    from build_structure_mask(records_out=...); without them, records are
    taken from structure_mask with unknown kind and length.

    Returns a list of dicts: start, end (indices), z_start, z_end, kind
    (1 = Bridge, 2 = Tunnel), open ("start", "end" or ""), and tier where
    a record supports the span (1 HPMS, 2 NBI/NTI, 3 none).
    """
    z = np.asarray(z_vals, dtype=float)
    n = len(z)
    if n < 5:
        return []

    edge_grade = float(params.get('STRUCTURE_EDGE_GRADE_PCT', 10.0)) / 100.0
    min_h = params['DIP_THRESHOLD_M']
    max_len = {1: params['BRIDGE_MAX_LEN_M'], 2: params['TUNNEL_MAX_LEN_M']}

    edges = _find_dem_edges(z, spacing_m, edge_grade, min_h)

    fit_pts = max(2, int(round(_ROAD_LEVEL_FIT_FT / FEET_PER_METER / spacing_m)))

    # For each edge, the end of the nearest earlier edge with the same sign
    # and the start of the nearest later one (far off the profile if none).
    # Not a bench where an edge stepping the other way comes first and at
    # least _BENCH_ROAD_FT of ground lies before it: the ground came back to
    # road level and ran on, so the next edge belongs to another structure
    # (two bridges close together). Short stretches in between still count
    # (the ground under one long bridge rising and falling around piers).
    bench_m = _SPAN_BENCH_FT / FEET_PER_METER
    road_pts = _BENCH_ROAD_FT / FEET_PER_METER / spacing_m
    last_same_before, first_same_after = [], []
    for i, (e_a, e_b, sg) in enumerate(edges):
        prev = [edges[j][1] for j in range(i) if edges[j][2] == sg]
        nxt = [edges[j][0] for j in range(i + 1, len(edges)) if edges[j][2] == sg]
        if i > 0 and edges[i - 1][2] != sg and e_a - edges[i - 1][1] >= road_pts:
            prev = []
        if i + 1 < len(edges) and edges[i + 1][2] != sg and edges[i + 1][0] - e_b >= road_pts:
            nxt = []
        last_same_before.append(prev[-1] if prev else -10 * n)
        first_same_after.append(nxt[0] if nxt else 10 * n)

    tol_pts = int(params.get('STRUCTURE_MATCH_TOL_M', 300.0 / FEET_PER_METER) / spacing_m)
    stretches = _structure_stretches(records, structure_mask, spacing_m)
    run_gap = max(1, int(round(_RECORD_RUN_GAP_FT / FEET_PER_METER / spacing_m)))

    def span_between(p: int, q: int, kind: int) -> Optional[Dict]:
        """Span from opening edge p to closing edge q if the ground between
        lies on the gap/hump side of the line joining the two road ends.
        mismatch: how far the span's grade departs from the road grade just
        outside its ends."""
        a, b = max(edges[p][0] - 1, 0), min(edges[q][1] + 1, n - 1)
        if (b - a) * spacing_m > max_len[kind]:
            return None
        # Each end needs road beyond it to read its elevation; an edge at
        # the very end of the profile is not an abutment or portal.
        if a < fit_pts or b > n - 1 - fit_pts:
            return None
        # A bridge end on a bench: the ground beyond it drops again (before
        # the start) or rises again (after the end) within _SPAN_BENCH_FT,
        # so the road has not been reached yet. Not applied to tunnels,
        # whose portal cuts often step more than once.
        if kind == 1 and ((edges[p][0] - last_same_before[p]) * spacing_m <= bench_m
                          or (first_same_after[q] - edges[q][1]) * spacing_m <= bench_m):
            return None
        za, ga = _road_fit(z, a, -1, spacing_m)
        zb, gb = _road_fit(z, b, +1, spacing_m)
        # Each end must sit on road: the ground just outside it can't keep
        # climbing or falling like a hillside (gullies on a mountain face,
        # or a span ending partway down the face above a tunnel portal).
        for k, step in ((a, -1), (b, +1)):
            g_out = _outside_grade(z, k, step, spacing_m)
            if g_out is not None and abs(g_out) > _SPAN_END_MAX_GRADE:
                return None
        inner_idx = np.arange(a + 1, b)
        if inner_idx.size == 0:
            return None
        line = za + (zb - za) * (inner_idx - a) / (b - a)
        depth = (line - z[inner_idx]) if kind == 1 else (z[inner_idx] - line)
        if (depth > 0).mean() < _SPAN_INSIDE_FRAC or depth.max() < min_h:
            return None
        grade = (zb - za) / ((b - a) * spacing_m)
        if abs(grade) > _SPAN_MAX_GRADE:
            return None
        tier = _supported(stretches, a, b, kind, tol_pts)
        if tier == 3 and (kind == 2 or b - a > 2 * tol_pts or not _has_face(z, spacing_m, a, b)):
            return None
        return {"start": a, "end": b, "z_start": za, "z_end": zb, "kind": kind, "tier": tier,
                "edges": (p, q), "mismatch": abs(grade - (ga + gb) / 2.0)}

    # Bridges are paired first, then tunnels from the edges no bridge used.
    # Otherwise the real road between two nearby bridges (rise, then drop)
    # would read as a tunnel with its ends on the two valley floors.
    used_edges = set()
    spans = []
    for kind in (1, 2):
        s_open, s_close = (-1, +1) if kind == 1 else (+1, -1)
        candidates = []
        for q, (_, b1, sq) in enumerate(edges):
            if sq != s_close or q in used_edges:
                continue
            # Nearest unused opening edge before this closing edge.
            best = None
            for p in range(q - 1, -1, -1):
                if edges[p][2] != s_open or p in used_edges:
                    continue
                if (b1 - edges[p][0]) * spacing_m > max_len[kind]:
                    break
                best = span_between(p, q, kind)
                if best is not None:
                    break
            if best is None:
                continue
            # A bench partway down an embankment (or up a portal cut) splits
            # one side into two edges; the nearest pairing then ends the span
            # on the bench, giving a deck that climbs or falls far more
            # steeply than the road on either side. Try the next edges out
            # and keep the pairing whose grade matches the road best.
            p0, q0 = best["edges"]
            openers = [p for p in range(p0, -1, -1)
                       if edges[p][2] == s_open and p not in used_edges][:_SPAN_EXTEND_EDGES + 1]
            closers = [k for k in range(q0, len(edges))
                       if edges[k][2] == s_close and k not in used_edges][:_SPAN_EXTEND_EDGES + 1]
            # Where an HPMS record of the same kind shows the structure's
            # limits, a pairing that covers the record (to within
            # _RECORD_RUN_GAP_FT of both its ends) is preferred to one that
            # does not: a pier or island in a river splits each side of the
            # gap into more than one edge, and the nearest pairing then
            # ends partway across (Snow River, Seward Highway).
            best_cov = _covers_hpms_record(stretches, best["start"], best["end"], kind, run_gap)
            for p in openers:
                for k in closers:
                    if (p, k) == (p0, q0):
                        continue
                    alt = span_between(p, k, kind)
                    if alt is None:
                        continue
                    alt_cov = _covers_hpms_record(stretches, alt["start"], alt["end"], kind, run_gap)
                    if (alt_cov and not best_cov) or (
                            alt_cov == best_cov and alt["mismatch"] < best["mismatch"] - _SPAN_GRADE_IMPROVE):
                        best, best_cov = alt, alt_cov
            p, k = best["edges"]
            candidates.append(best)
            used_edges.update(range(p, k + 1))

        # Where spans overlap, the longer (outer) one wins -- e.g. a bump on
        # a valley floor under a long bridge.
        # A tunnel may also replace bridges it fully contains -- gullies on
        # the mountainside above a long tunnel read as bridges in the DEM.
        for c in sorted(candidates, key=lambda c: c["end"] - c["start"], reverse=True):
            hits = [k for k in spans if not (c["end"] < k["start"] or c["start"] > k["end"])]
            if not hits:
                spans.append(c)
            elif kind == 2 and all(k["kind"] == 1 and c["start"] <= k["start"] and k["end"] <= c["end"]
                                   for k in hits):
                spans = [k for k in spans if k not in hits] + [c]

    raw_ends = {id(c): (c["start"], c["end"]) for c in spans}
    for c in spans:
        c.pop("edges", None)
        c.pop("mismatch", None)
        c["start"], c["z_start"] = _refine_span_end(z, c["start"], -1, c["kind"], spacing_m)
        c["end"], c["z_end"] = _refine_span_end(z, c["end"], +1, c["kind"], spacing_m)
        c["open"] = ""
    # Two structures close together: an end moved out into the next span
    # (its road line was fitted partly over the other gap) is placed again
    # with the fit and the move kept short of the other span's edge.
    spans.sort(key=lambda c: c["start"])
    for c1, c2 in zip(spans, spans[1:]):
        if c1["end"] >= c2["start"]:
            s1, e1 = raw_ends[id(c1)]
            s2, e2 = raw_ends[id(c2)]
            c1["end"], c1["z_end"] = _refine_span_end(z, e1, +1, c1["kind"], spacing_m, stop=s2)
            c2["start"], c2["z_start"] = _refine_span_end(z, s2, -1, c2["kind"], spacing_m, stop=e1)

    if structure_mask is not None:
        # Bridge records only (an HPMS bridge record or an NBI point, or a
        # record of unknown kind): a tunnel record says nothing about a
        # bridge running off the end of the data.
        bridge_rec = np.zeros(n, dtype=int)
        if records is None:
            bridge_rec[:] = structure_mask
        else:
            for r in records:
                if r["kind"] != 1:
                    continue
                if r["source"] == 1:
                    bridge_rec[r["start"]:r["end"] + 1] = 1
                elif bridge_rec[r["at"]] == 0:
                    bridge_rec[r["at"]] = 2
        for side in ("start", "end"):
            o = _open_bridge_span(z, spacing_m, edges, side, min_h, max_len[1], fit_pts, bench_m,
                                  _record_reach(bridge_rec, side, tol_pts))
            if o is None:
                continue
            near = bridge_rec[max(0, o["start"] - tol_pts):min(n, o["end"] + tol_pts + 1)]
            o["tier"] = 1 if np.any(near == 1) else 2
            # The open span replaces bridges it contains (relief on the
            # ground under the deck); it is dropped if it cuts into another.
            inside = [c for c in spans if o["start"] <= c["start"] and c["end"] <= o["end"]]
            crossing = [c for c in spans if c not in inside and not (o["end"] < c["start"] or o["start"] > c["end"])]
            if crossing or any(c["kind"] != 1 for c in inside):
                continue
            spans = [c for c in spans if c not in inside] + [o]

        hpms_bridges = [(st["start"], st["end"]) for st in stretches
                        if st["source"] == 1 and st["kind"] in (0, 1)]
        spans = _record_bridge_spans(z, spacing_m, params, hpms_bridges, spans, min_h, fit_pts)
    spans = _end_edge_spans(z, spacing_m, edges, spans, fit_pts)
    spans = [c for c in spans if not c.get("end_edge") or _end_edge_supported(
        z, spacing_m, c, stretches, tol_pts)]
    return sorted(spans, key=lambda c: c["start"])


def _structure_stretches(records: Optional[List[Dict]], structure_mask: Optional[np.ndarray],
                         spacing_m: float) -> List[Dict]:
    """
    The stretch of the profile each structure record covers, as dicts
    {source (1 HPMS, 2 NBI/NTI), kind (1 bridge, 2 tunnel, 0 unknown),
    start, end, length} in point indices. HPMS records of the same kind
    less than _RECORD_RUN_GAP_FT apart are joined (one structure split
    into several records); start to end is then the structure. An NBI or
    NTI point may lie anywhere on its structure, so its stretch reaches
    the structure's length (NBI Item 49, NTI G1) to either side of the
    point, and length is that length (0 if not given). Without records,
    runs of the structure mask are used, of unknown kind.
    """
    gap = max(1, int(round(_RECORD_RUN_GAP_FT / FEET_PER_METER / spacing_m)))

    def runs(idx):
        idx = np.unique(np.asarray(idx, dtype=int))
        if idx.size == 0:
            return []
        breaks = np.flatnonzero(np.diff(idx) > gap)
        return list(zip(np.r_[idx[0], idx[breaks + 1]].tolist(), np.r_[idx[breaks], idx[-1]].tolist()))

    out = []
    if records is None:
        if structure_mask is None:
            return []
        for source in (1, 2):
            for a, b in runs(np.flatnonzero(structure_mask == source)):
                out.append({"source": source, "kind": 0, "start": a, "end": b, "length": b - a})
        return out
    for kind in (1, 2):
        idx = [np.arange(r["start"], r["end"] + 1) for r in records
               if r["source"] == 1 and r["kind"] == kind]
        for a, b in runs(np.concatenate(idx) if idx else []):
            out.append({"source": 1, "kind": kind, "start": a, "end": b, "length": b - a})
    for r in records:
        if r["source"] == 2:
            length_m = r.get("length_m")
            pts = int(round(length_m / spacing_m)) if length_m is not None and np.isfinite(length_m) else 0
            out.append({"source": 2, "kind": r["kind"], "start": r["at"] - pts, "end": r["at"] + pts,
                        "length": pts})
    return out


def _supported(stretches: List[Dict], a: int, b: int, kind: int, tol_pts: int) -> int:
    """
    The tier of the record supporting a span from a to b of the given kind:
    1 (HPMS) or 2 (NBI/NTI), or 3 if none does. A record supports the span
    if it is of the same kind (or of unknown kind), the span lies within
    its stretch widened by STRUCTURE_MATCH_TOL_FT at each end, and the span
    is no longer than the structure plus STRUCTURE_MATCH_TOL_FT at each
    end. HPMS records are preferred.
    """
    best = 3
    for st in stretches:
        if st["kind"] not in (0, kind):
            continue
        if a >= st["start"] - tol_pts and b <= st["end"] + tol_pts and b - a <= st["length"] + 2 * tol_pts:
            best = min(best, st["source"])
    return best


def _covers_hpms_record(stretches: List[Dict], a: int, b: int, kind: int, gap_pts: int) -> bool:
    """True if the span a..b covers an HPMS record run of the same kind (or
    of unknown kind) to within gap_pts of both of its ends, the test of
    _record_bridge_spans."""
    return any(st["source"] == 1 and st["kind"] in (0, kind)
               and a <= st["start"] + gap_pts and b >= st["end"] - gap_pts
               and st["start"] <= b and st["end"] >= a
               for st in stretches)


def _has_face(z: np.ndarray, spacing_m: float, a: int, b: int,
              open_side: str = "") -> bool:
    """
    True if, near each end of the span a..b (from _DIP_FACE_OUTSIDE_FT
    outside to _DIP_FACE_INSIDE_FT inside, but no farther in than the
    middle of the span), the ground rises or falls by at least
    _DIP_FACE_MIN_FT between two points _DIP_FACE_RUN_FT apart (to the
    nearest point): the two sides of a bridge gap. Single steep points from DEM noise do not count.
    The open end of a span that runs off the profile is not checked.
    """
    n = len(z)
    min_h = _DIP_FACE_MIN_FT / FEET_PER_METER
    m = max(1, int(round(_DIP_FACE_RUN_FT / FEET_PER_METER / spacing_m)))
    step = np.zeros(n)
    if n > m:
        step[:n - m] = np.abs(z[m:] - z[:n - m])
    out_pts = int(round(_DIP_FACE_OUTSIDE_FT / FEET_PER_METER / spacing_m))
    in_pts = min(int(round(_DIP_FACE_INSIDE_FT / FEET_PER_METER / spacing_m)), (b - a) // 2)
    faces = []
    if open_side != "start":
        faces.append(step[max(0, a - out_pts - m):min(n, a + in_pts + 1)].max(initial=0.0))
    if open_side != "end":
        faces.append(step[max(0, b - in_pts - m):min(n, b + out_pts + 1)].max(initial=0.0))
    return bool(faces) and min(faces) >= min_h


def _end_edge_supported(z, spacing_m, c, stretches, tol_pts) -> bool:
    """An end-of-data stretch (_end_edge_spans) is kept if a structure
    record supports it, else if it shows a face at its inner end."""
    tier = _supported(stretches, c["start"], c["end"], c["kind"], tol_pts)
    if tier < 3:
        c["tier"] = tier
        return True
    if c["kind"] == 1 and _has_face(z, spacing_m, c["start"], c["end"], open_side=c["open"]):
        c["tier"] = 3    # kept on the face alone, not on a record
        return True
    return False


def _end_edge_spans(z, spacing_m, edges, spans, fit_pts):
    """
    Holds the profile level where the data ends in a gap. A line that stops
    at a bridge abutment (or at the face of a cut) can run a few points
    past it, onto the ground below the deck or the slope above the road.
    Where an edge (DIP_THRESHOLD_FT or more at STRUCTURE_EDGE_GRADE_PCT)
    reaches within _END_EDGE_FT of the start or end of the profile, so the
    ground does not come back to road level before the data ends, and the
    stretch from the edge to that end is no longer than _END_EDGE_MAX_FT,
    that stretch takes the elevation of the road on the other side of the
    edge, read from the _ROAD_LEVEL_FIT_FT fit there. No structure record
    is needed: a road cannot drop or climb that much in the last few feet
    of its data. Kind follows the ground: below the road (Bridge) or above
    it (Tunnel). Skipped where a span already reaches that end, or where
    too little road lies beyond the edge to read its elevation.
    """
    n = len(z)
    if not edges:
        return spans
    reach = int(round(_END_EDGE_FT / FEET_PER_METER / spacing_m))
    max_len = int(round(_END_EDGE_MAX_FT / FEET_PER_METER / spacing_m))
    out = list(spans)
    # Start of the profile: the edge nearest the start, if it begins within reach.
    a_e, b_e, _ = edges[0]
    k = b_e + 1
    if (a_e <= reach and k <= max_len and k + fit_pts < n
            and not any(c["start"] <= k for c in out)):
        z_road, _ = _road_fit(z, k, +1, spacing_m)
        out.append({"start": 0, "end": k, "z_start": z_road, "z_end": z_road,
                    "kind": 1 if z[0] < z_road else 2, "open": "start", "end_edge": True})
    # End of the profile: the edge nearest the end, if it ends within reach.
    a_e, b_e, _ = edges[-1]
    k = a_e - 1
    if (b_e >= n - 1 - reach and n - 1 - k <= max_len and k - fit_pts >= 0
            and not any(c["end"] >= k for c in out)):
        z_road, _ = _road_fit(z, k, -1, spacing_m)
        out.append({"start": k, "end": n - 1, "z_start": z_road, "z_end": z_road,
                    "kind": 1 if z[-1] < z_road else 2, "open": "end", "end_edge": True})
    return out


def _record_bridge_spans(z, spacing_m, params, runs, spans, min_h, fit_pts):
    """
    Bridges the DEM edges miss but an HPMS bridge record shows. Long
    bridges whose approach spans run over a sloping shore have no
    near-vertical edge at either end, so the edge pairing finds no span, or
    pieces of one ending on the water (a flat water surface reads like
    road), or a span ending partway up the shore slope short of the
    abutment. For each run of HPMS bridge records (runs: (first, last)
    point index, records joined across gaps up to _RECORD_RUN_GAP_FT)
    that no single span covers to within _RECORD_RUN_GAP_FT of both ends,
    a span is placed at the record's limits, widened to take in any span
    overlapping them, with each end moved out to road level as for other
    spans but stopping short of any neighboring span. At each end where the
    record runs on past the spans it takes in, the ground it adds must
    somewhere lie DIP_THRESHOLD_FT below the new line; otherwise the record
    only runs on over the approach road there (HPMS bridge limits are often
    a few hundred feet off) and that end stays at the span's end. A single
    span with both ends kept is left as it is. Otherwise the span is kept if, as for any
    bridge, the ground between lies below the line joining the two ends
    (90% of it, and by at least DIP_THRESHOLD_FT somewhere), the deck is no
    steeper than _SPAN_MAX_GRADE, and it is no longer than
    BRIDGE_RECORD_MAX_LEN_FT. It replaces the spans it takes in. Records over a tunnel, or over a bridge
    whose deck is in the DEM, fail the ground test and change nothing.
    """
    n = len(z)
    if not runs:
        return spans
    gap = max(1, int(round(_RECORD_RUN_GAP_FT / FEET_PER_METER / spacing_m)))
    max_len = params.get('BRIDGE_RECORD_MAX_LEN_M', 30000.0 / FEET_PER_METER)

    for a, b in runs:
        if b - a < 2:
            continue
        if any(c["start"] <= a + gap and c["end"] >= b - gap for c in spans):
            continue    # already spanned
        over = [c for c in spans if not (c["end"] < a or c["start"] > b)]
        if any(c["kind"] != 1 or c.get("open") for c in over):
            continue
        a2 = min([a] + [c["start"] for c in over])
        b2 = max([b] + [c["end"] for c in over])
        if a2 < fit_pts or b2 > n - 1 - fit_pts:
            continue
        s, z_s = _refine_span_end(z, a2, -1, 1, spacing_m)
        e, z_e = _refine_span_end(z, b2, +1, 1, spacing_m)
        # An end moved out into a neighboring span stops just short of it.
        lo = max([c["end"] + 1 for c in spans if c not in over and c["end"] < a2], default=0)
        hi = min([c["start"] - 1 for c in spans if c not in over and c["start"] > b2], default=n - 1)
        if s < lo:
            s, z_s = lo, float(z[lo])
        if e > hi:
            e, z_e = hi, float(z[hi])
        if (e - s) * spacing_m > max_len:
            continue
        if abs(z_e - z_s) / ((e - s) * spacing_m) > _SPAN_MAX_GRADE:
            continue
        if over:
            # At each end where the record runs on past the spans it takes
            # in, the ground it adds must somewhere lie DIP below the new
            # line; otherwise that end is approach road (HPMS bridge limits
            # are often a few hundred feet off) and stays at the span's end.
            first = min(over, key=lambda c: c["start"])
            last = max(over, key=lambda c: c["end"])

            def _road_only(idx):
                line = z_s + (z_e - z_s) * (idx - s) / max(e - s, 1)
                return idx.size == 0 or (line - z[idx]).max() < min_h

            pull_s = _road_only(np.arange(s, first["start"]))
            pull_e = _road_only(np.arange(last["end"] + 1, e + 1))
            if pull_s:
                s, z_s = first["start"], first["z_start"]
            if pull_e:
                e, z_e = last["end"], last["z_end"]
            if len(over) == 1 and pull_s and pull_e:
                continue    # the record only runs on over road beyond the span
        inner = np.arange(s + 1, e)
        if inner.size == 0:
            continue
        depth = z_s + (z_e - z_s) * (inner - s) / (e - s) - z[inner]
        if (depth > 0).mean() < _SPAN_INSIDE_FRAC or depth.max() < min_h:
            continue
        spans = [c for c in spans if c not in over] + [
            {"start": s, "end": e, "z_start": z_s, "z_end": z_e, "kind": 1, "open": "", "tier": 1}]
    return spans


def _record_reach(structure_mask: np.ndarray, side: str, tol_pts: int) -> Optional[int]:
    """
    How far an HPMS or NBI structure record reaches in from one end of the
    profile: the index of the last record point (from the start) or the
    first (from the end) in a run that begins within tol_pts of that end,
    with gaps of up to tol_pts allowed, extended by tol_pts. None if no
    record lies within tol_pts of that end.
    """
    rec = (structure_mask == 1) | (structure_mask == 2)
    n = len(rec)
    idx = np.flatnonzero(rec if side == "start" else rec[::-1])
    if idx.size == 0 or idx[0] > tol_pts:
        return None
    gaps = np.flatnonzero(np.diff(idx) > tol_pts)
    last = idx[gaps[0]] if gaps.size else idx[-1]
    reach = min(n - 1, last + tol_pts)
    return reach if side == "start" else n - 1 - reach


def _open_bridge_span(z, spacing_m, edges, side, min_h, max_len, fit_pts, bench_m, reach):
    """
    Bridge span from the one abutment in the data to the start or end of
    the profile, or None. The abutment is the edge out of the gap (a rise
    after the start, or a drop before the end) farthest from that end of
    the profile but still within reach of the structure record there:
    the ground under a long bridge can itself step (a riverbank, a levee),
    and the deck lands on the last of those steps. As for any bridge, the
    road beyond the abutment must be road (not a bench or hillside), the
    ground under the deck must lie below it, and the span must be no
    longer than BRIDGE_MAX_LEN_FT. Held level at the abutment elevation.
    """
    if reach is None:
        return None
    n = len(z)
    if side == "start":
        want = +1
        cands = [i for i, e in enumerate(edges) if e[2] == want and fit_pts <= e[0] and e[1] + 1 <= reach]
        cands.sort(key=lambda i: -edges[i][1])
    else:
        want = -1
        cands = [i for i, e in enumerate(edges) if e[2] == want and e[1] <= n - 1 - fit_pts and e[0] - 1 >= reach]
        cands.sort(key=lambda i: edges[i][0])
    for i in cands:
        if side == "start":
            k, step = min(edges[i][1] + 1, n - 1), +1
            nxt = [e[0] for e in edges[i + 1:] if e[2] == want]
            if nxt and (nxt[0] - edges[i][1]) * spacing_m <= bench_m:
                continue
        else:
            k, step = max(edges[i][0] - 1, 0), -1
            prev = [e[1] for e in edges[:i] if e[2] == want]
            if prev and (edges[i][0] - prev[-1]) * spacing_m <= bench_m:
                continue
        g_out = _outside_grade(z, k, step, spacing_m)
        if g_out is None or abs(g_out) > _SPAN_END_MAX_GRADE:
            continue
        k_ref, z_ab = _refine_span_end(z, k, step, 1, spacing_m)
        lo, hi = (0, k_ref) if side == "start" else (k_ref, n - 1)
        if (hi - lo) * spacing_m > max_len:
            continue
        depth = z_ab - z[lo:hi + 1]
        if (depth > 0).mean() < _SPAN_INSIDE_FRAC or depth.max() < min_h:
            continue
        return {"start": lo, "end": hi, "z_start": z_ab, "z_end": z_ab, "kind": 1, "open": side}
    return None


def repair_structure_spans(
    z_vals: np.ndarray,
    spacing_m: float,
    params: Dict,
    structure_mask: np.ndarray = None,
    records: Optional[List[Dict]] = None
) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """
    Replaces each bridge gap / tunnel hump found by find_structure_spans()
    with one straight line between its two observed ends.

    structure_mask and records (from build_structure_mask) decide which
    spans are accepted (find_structure_spans) and label them: a span
    supported by an HPMS record is Tier 1, by an NBI/NTI record Tier 2,
    and one found from the DEM alone Tier 3. A span placed from a record
    that runs off the end of the profile is labeled by the records within
    STRUCTURE_MATCH_TOL_FT of it. Records with no detected span leave the
    profile unchanged (e.g. a bridge over the route, or a DEM that kept
    the deck).

    Returns (z_fixed, tier, kind) -- tier and kind are 0 outside spans.
    """
    z_fixed = np.array(z_vals, dtype=float, copy=True)
    n = len(z_fixed)
    tier = np.zeros(n, dtype=int)
    kind = np.zeros(n, dtype=int)

    for sp in find_structure_spans(z_fixed, spacing_m, params, structure_mask, records):
        a, b = sp["start"], sp["end"]
        z_fixed[a:b + 1] = np.linspace(sp["z_start"], sp["z_end"], b - a + 1)
        # Every span carries the tier of the evidence that accepted it
        # (Section 5.2.8): a record of the same kind (1, 2) or the DEM (3).
        tier[a:b + 1] = sp.get("tier", 3)
        kind[a:b + 1] = sp["kind"]
    return z_fixed, tier, kind


# -------------------------
# Parameter normalization
# -------------------------
H_FACTOR_KEYS = {1: "H_SMOOTH_FACTOR", **{fs: f"H_SMOOTH_FACTOR_FS{fs}" for fs in range(2, 8)}}


def load_state_smoothing_factors(state_fips: str) -> Dict[str, float]:
    """A state's calibrated horizontal smoothing factors from
    national_smoothing_factors.json ({key: factor}, only the functional
    systems the state has); empty if the state or file is missing."""
    json_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'national_smoothing_factors.json')
    if not os.path.exists(json_path):
        return {}
    try:
        with open(json_path, 'r') as f:
            entry = json.load(f).get(str(state_fips).zfill(2)) or {}
    except Exception as e:
        logging.error(f"Failed to read national smoothing factors JSON: {e}")
        return {}
    return {k: v for k, v in entry.items() if v is not None and k in H_FACTOR_KEYS.values()}


def describe_smoothing_factors(params: Dict) -> str:
    """
    One line for the Run Log listing the smoothing factors a run uses: the
    horizontal factor for each functional system, marked "state" (the
    state's calibrated value), "national" (the national value, used where
    the state has none) or "custom" (a value typed in or loaded from saved
    settings), and the vertical factor.
    """
    fips = str(params.get("STATE_FIPS", "") or "").strip().zfill(2)
    state = load_state_smoothing_factors(fips) if fips.isdigit() and fips != "00" else {}

    def _same(a, b):
        try:
            return abs(float(a) - float(b)) < 1e-9
        except (TypeError, ValueError):
            return False

    parts = []
    for fs, key in H_FACTOR_KEYS.items():
        val = params.get(key, NATIONAL_DEFAULTS["H"][fs])
        if key in state:
            source = "state" if _same(val, state[key]) else "custom"
        else:
            source = "national" if _same(val, NATIONAL_DEFAULTS["H"][fs]) else "custom"
        parts.append(f"FS {fs} {float(val):g} ({source})")
    v = params.get("V_SMOOTH_FACTOR", NATIONAL_V_SMOOTH_FACTOR)
    v_source = "national" if _same(v, NATIONAL_V_SMOOTH_FACTOR) else "custom"
    label = f"State {fips}" if state else ("State " + fips if fips.isdigit() and fips != "00" else "this run")
    return (f"Smoothing factors for {label}: horizontal " + ", ".join(parts)
            + f"; vertical {float(v):g} ({v_source}).")


def build_params(user_params: Optional[Dict] = None) -> Dict:
    p = DEFAULTS.copy()

    # 1. Apply user params first (From GUI)
    if user_params:
        for k, v in user_params.items():
            if v is not None and str(v).strip() != "":
                p[k] = v

    # 2. Apply National Smoothing Factors over the top IF it's a batch run
    if p.get('STATE_FIPS'):
        state_fips = str(p['STATE_FIPS']).zfill(2)
        if state_fips not in ["00", "AL"]:
            # The state's factors come from the JSON unless
            # IGNORE_GUI_SMOOTHING_FACTORS is explicitly False (a single-state
            # GUI run, which uses the GUI values).
            force_json = True
            if user_params and "IGNORE_GUI_SMOOTHING_FACTORS" in user_params:
                if not user_params["IGNORE_GUI_SMOOTHING_FACTORS"]:
                    force_json = False

            if force_json:
                # Horizontal factors only; the vertical factor is national.
                p.update(load_state_smoothing_factors(state_fips))

    p['H_SMOOTH_FACTOR'] = float(p.get('H_SMOOTH_FACTOR', NATIONAL_DEFAULTS["H"][1]))
    p['V_SMOOTH_FACTOR'] = float(p.get('V_SMOOTH_FACTOR') or NATIONAL_V_SMOOTH_FACTOR)
    for fs in range(2, 8):
        p.pop(f'V_SMOOTH_FACTOR_FS{fs}', None)   # older run_params files
    # Settings from older run_params files that no method uses.
    for key in ('H_BASE_SMOOTH_WINDOW', 'H_MIN_HEAD_CHANGE', 'H_LOOKAHEAD_DIST_M',
                'V_REVERSAL_TOLERANCE', 'V_MERGE_GAP_FT'):
        p.pop(key, None)
    p['H_MIN_CHUNK_POINTS'] = max(int(p.get('H_MIN_CHUNK_POINTS', 23)), 8)

    p['DENSIFY_SPACING_M'] = p.get('DENSIFY_SPACING_FT', 5.0) / FEET_PER_METER
    p['H_MIN_CURVE_LENGTH_M'] = p.get('H_MIN_CURVE_LENGTH_FT', 100.0) / FEET_PER_METER
    p['H_MAX_RADIUS'] = p.get('H_MAX_RADIUS_FT', 165000.0) / FEET_PER_METER
    p['V_MIN_CURVE_LENGTH'] = p.get('V_MIN_CURVE_LENGTH_FT', 200.0) / FEET_PER_METER
    p['REGRESSION_WINDOW_M'] = p.get('REGRESSION_WINDOW_FT', 500.0) / FEET_PER_METER
    p['DIP_THRESHOLD_M'] = p.get('DIP_THRESHOLD_FT', 3.0) / FEET_PER_METER
    p['BRIDGE_MAX_LEN_M'] = p.get('BRIDGE_MAX_LEN_FT', 8200.0) / FEET_PER_METER
    p['BRIDGE_RECORD_MAX_LEN_M'] = float(p.get('BRIDGE_RECORD_MAX_LEN_FT', 30000.0)) / FEET_PER_METER
    p['TUNNEL_MAX_LEN_M'] = p.get('TUNNEL_MAX_LEN_FT', 10000.0) / FEET_PER_METER
    p['STRUCTURE_MATCH_TOL_M'] = p.get('STRUCTURE_MATCH_TOL_FT', 300.0) / FEET_PER_METER
    return p

# ---------------------------------------------------------------------------
# Centralized Data Loaders (Socrata & Local)
# ---------------------------------------------------------------------------
def filter_local_df_to_state(df: pd.DataFrame, state_fips: str) -> pd.DataFrame:
    """
    Filters a locally cached HPMS extract (e.g. the national "reuse Socrata
    cache" CSV) down to a single state, mirroring the server-side
    stateid='{fips}' filter that fetch_socrata_state() applies on a live
    Socrata query. Used when a run reuses a cached extract.

    Pass state_fips of "LOCAL", "ALL", "00", or falsy to skip filtering
    entirely (intentional all-states runs).
    """
    if not state_fips or str(state_fips).upper() in ("LOCAL", "ALL", "00"):
        return df

    state_col = None
    for candidate in ("stateid", "State_FIPS", "StateFIPS", "STATEID"):
        if candidate in df.columns:
            state_col = candidate
            break

    if state_col is None:
        logging.warning(
            f"No state identifier column found in cached extract "
            f"(looked for stateid/State_FIPS/StateFIPS) -- "
            f"cannot filter to State FIPS {state_fips}. "
            f"Processing will include ALL states in the cache."
        )
        return df

    before_count = len(df)
    # Compare a normalized copy of the state column rather than copying the
    # whole (possibly national) dataframe.
    state_norm = df[state_col].astype(str).str.zfill(2)
    df = df[state_norm == str(state_fips).zfill(2)]
    logging.info(
        f"Filtered cached extract to State FIPS {state_fips}: "
        f"{before_count:,} -> {len(df):,} rows."
    )
    return df


def apply_facility_fsystem_filters(
    df: pd.DataFrame,
    facility_filter: list = None,
    fsystem_filter: list = None,
    include_missing_facility: bool = False,
    include_missing_fsystem: bool = False,
) -> pd.DataFrame:
    """
    Applies the standard Facility_Type / FSystem allow-list filters to an
    already-loaded HPMS dataframe (e.g. from load_local_hpms(), or a
    Socrata extract that was downloaded without server-side filtering).

    The same filtering fetch_socrata_state() applies in its $where clause,
    for local or cached dataframes. An empty or None facility type list
    means types 1 and 2, as on a Socrata query; an empty or None functional
    system list keeps every coded class. Records with no facility type / no functional system
    (Facility_Type_Missing / FSystem_Missing) are kept only with
    include_missing_facility / include_missing_fsystem.
    """
    before = len(df)

    def _keep(col, values, include_missing):
        flag = df.get(f"{col}_Missing", pd.Series(False, index=df.index)).astype(bool)
        coded = df[col].isin(values) if values else pd.Series(True, index=df.index)
        return (coded & ~flag) | (flag & include_missing)

    if "Facility_Type" in df.columns:
        df = df[_keep("Facility_Type", facility_filter or [1, 2], include_missing_facility)]

    if "FSystem" in df.columns:
        df = df[_keep("FSystem", fsystem_filter, include_missing_fsystem)]

    after = len(df)
    if after != before:
        logging.info(
            f"Facility Type / FSystem filter: {before:,} -> {after:,} rows."
        )
    return df


# ===========================================================================
# NATIONAL CALIBRATION SHARED INFRASTRUCTURE
# ===========================================================================
# Sampling, one-mile chunk generation and chunk preloading used by the
# national calibration script in tools/ and by the plan/profile calibration
# cross-check.
SOCRATA_TOKEN = os.environ.get("RAT_SOCRATA_TOKEN", "")

BASE_ENGINE_PARAMS = {
    "DENSIFY_SPACING_FT":    10.0,
    "H_MIN_DELTA":            3.5,
    "H_MIN_CURVE_LENGTH_FT": 100.0,
    "V_MIN_CURVE_LENGTH_FT": 200.0,
    "V_MIN_GRADE_CHANGE":     0.5,
    "ENABLE_MERGE":           False,
    "MERGE_GAP_FLOOR_FT":    50.0,
    "MERGE_GAP_PCT":         0.25,
}

ALL_FIPS = [
    "01","02","04","05","06","08","09","10","11","12","13","15","16",
    "17","18","19","20","21","22","23","24","25","26","27","28","29",
    "30","31","32","33","34","35","36","37","38","39","40","41","42",
    "44","45","46","47","48","49","50","51","53","54","55","56","72"
]



def cochran_sample_size(population: int, variance: float = 0.25,
                        confidence: float = 0.95,
                        margin_of_error: float = 0.05) -> int:
    """
    Cochran's formula with finite population correction, generalized to a
    multi-category outcome. `variance` replaces the original p*q term --
    pass variance=0.25 (the default) to reproduce the original
    conservative single-proportion (p=0.5) behavior, or a value from
    get_cochran_variance() for a multi-category outcome (e.g. HPMS
    reported curve severity class), which uses max_k[p_k*(1-p_k)] across
    categories so precision is guaranteed for whichever category is
    hardest to pin down, not just the average one.
    """
    z = {0.90: 1.645, 0.95: 1.960, 0.99: 2.576}.get(confidence, 1.960)
    n0 = (z ** 2 * variance) / (margin_of_error ** 2)
    n = n0 / (1 + (n0 - 1) / population)
    return min(math.ceil(n), population)


def load_curve_variance_lookup(json_path: str) -> Optional[dict]:
    """
    Loads the national_curve_class_proportions.json produced by
    tools/pull_national_curve_distribution.py. Returns None (triggering a
    fallback to the conservative p=0.5 default in get_cochran_variance)
    if the file is missing or fails to parse -- this is an OPTIONAL
    enhancement, not a hard requirement, so its absence shouldn't break
    calibration for anyone who hasn't run the one-time pull script yet.
    """
    if not json_path or not os.path.exists(json_path):
        logging.warning(f"Curve distribution JSON not found at {json_path} -- "
                        f"Cochran sample sizing will use the conservative "
                        f"p=0.5 default for every state/FS. Run "
                        f"tools/pull_national_curve_distribution.py to "
                        f"generate this file for more precise sample sizing.")
        return None
    try:
        with open(json_path, "r") as f:
            return json.load(f)
    except Exception as e:
        logging.warning(f"Failed to load curve distribution JSON at "
                        f"{json_path}: {e} -- falling back to conservative "
                        f"p=0.5 variance for Cochran sample sizing.")
        return None


def get_cochran_variance(lookup: Optional[dict], state_fips: str, f_sys: int) -> float:
    """
    Resolves the multi-category variance term for Cochran's sample-size
    formula -- max_k[p_k*(1-p_k)] across HPMS-reported curve severity
    classes (A-F) -- using STATE-level proportions if that state/FS has
    enough reporting panels (see the JSON's "usable" flag), NATIONAL
    proportions otherwise. Falls back to the conservative single-
    proportion default (p=0.5 -> variance=0.25) if no curve distribution
    data exists for this FS at either level -- this preserves the
    ORIGINAL Cochran behavior as an ultimate safety net rather than
    silently producing a wrong answer from missing data.
    """
    FALLBACK_VARIANCE = 0.25  # p=0.5, the original conservative default

    if lookup is None:
        return FALLBACK_VARIANCE

    fs_key = str(f_sys)
    state_entry = lookup.get("by_state", {}).get(state_fips, {}).get(fs_key)
    if state_entry and state_entry.get("usable"):
        props = state_entry["proportions"]
        source = "state"
    else:
        national_entry = lookup.get("national", {}).get(fs_key)
        if national_entry:
            props = national_entry["proportions"]
            source = "national"
        else:
            logging.warning(f"[{state_fips}] FS {f_sys}: no curve distribution "
                            f"data at state OR national level -- falling back "
                            f"to conservative p=0.5 variance.")
            return FALLBACK_VARIANCE

    if not props:
        return FALLBACK_VARIANCE

    variance = max(p * (1 - p) for p in props.values())
    worst_cat = max(props, key=props.get)
    logging.info(f"[{state_fips}] FS {f_sys}: Cochran variance from {source}-level "
                f"curve distribution = {variance:.4f} "
                f"(largest-variance category: {worst_cat}={props[worst_cat]:.3f})")
    return variance


def generate_1mile_chunks(wkt_list: list, start_mps: list = None, end_mps: list = None,
                           shape_ids: list = None, join_tol_ft: float = 100.0) -> list:
    """Stitches a route's WKT segments and slices into ~1-mile LineStrings.

    Includes complete 1-mile chunks plus any tail segment >= 0.5 miles (800m);
    a line shorter than a mile but at least 800 m long is one piece.
    This ensures the population for Cochran sampling is not biased toward longer
    routes and includes the full geometric diversity of the network.

    Uses the same ShapeId-aware stitching as build_route_continuity_chunks
    (this file) when shape_ids is given and every value is present --
    walking segments in true digitization order rather than guessing
    adjacency from spatial proximity alone. Falls back to
    stitch_linestrings_ordered otherwise. join_tol_ft (default 100 ft, as
    CHUNK_JOIN_TOLERANCE_FT) keeps either path from bridging a real gap
    several hundred feet long, which would add curvature that is not on
    the ground.
    """
    use_shapeid = shape_ids is not None and not any(pd.isna(s) for s in shape_ids)
    if use_shapeid:
        lines = stitch_linestrings_by_shapeid(
            wkt_list, shape_ids, start_mps=start_mps, end_mps=end_mps,
            coord_tol_ft=join_tol_ft)
    else:
        snap_tol_deg = join_tol_ft / 330000.0  # rough ft per degree, as in stitch_linestrings_ordered
        lines = stitch_linestrings_ordered(wkt_list, snap_tol=snap_tol_deg,
                                            start_mps=start_mps, end_mps=end_mps)
    chunks = []
    for line in lines:
        if line.is_empty:
            continue
        lon, lat = line.coords[0]
        utm      = get_appropriate_utm_zone(lon, lat)
        fwd      = Transformer.from_crs("EPSG:4326", f"EPSG:{utm}", always_xy=True)
        coords_m = [fwd.transform(x, y) for x, y in line.coords]
        l_m      = LineString(coords_m).length
        if l_m < 800.0:
            continue    # shorter than the shortest piece kept (below)

        # Generate complete 1-mile chunks
        num_full_chunks = int(l_m / 1_609.34)  # Floor: how many full miles fit
        for i in range(num_full_chunks):
            chunks.append(substring(line,
                                    (i * 1_609.34) / l_m,
                                    ((i + 1) * 1_609.34) / l_m,
                                    normalized=True))

        # Include a tail segment of at least 0.5 miles (800 m), so the
        # sample is not biased toward longer routes.
        tail_length_m = l_m - (num_full_chunks * 1_609.34)
        MIN_TAIL_M = 800.0  # 0.5 miles minimum

        if tail_length_m >= MIN_TAIL_M:
            chunks.append(substring(line,
                                    (num_full_chunks * 1_609.34) / l_m,
                                    1.0,  # End of line
                                    normalized=True))

    return chunks


def _preload_chunk_for_h_sweep(args) -> Optional[dict]:
    """
    Called once per chunk before an H sweep begins. Returns a dict with
    the projected, densified coordinates the horizontal Viterbi sweep
    needs (no elevation), or None if the chunk can't be prepared.
    """
    chunk_geom, base_params = args
    try:
        lon, lat = chunk_geom.coords[0]
        utm = get_appropriate_utm_zone(lon, lat)
        fwd = Transformer.from_crs("EPSG:4326", f"EPSG:{utm}", always_xy=True)

        coords_m  = [fwd.transform(x, y) for x, y in chunk_geom.coords]
        coords_m  = densify_coords_line(coords_m, base_params["DENSIFY_SPACING_M"])
        if len(coords_m) < base_params.get("H_MIN_CHUNK_POINTS", 23):
            return None

        x_raw     = np.array([c[0] for c in coords_m])
        y_raw     = np.array([c[1] for c in coords_m])
        spacing_m = LineString(coords_m).length / (len(coords_m) - 1)
        d_axis    = np.arange(len(coords_m)) * spacing_m

        return {
            "x_raw":          x_raw,
            "y_raw":          y_raw,
            "d_axis":         d_axis,
            "spacing_m":      spacing_m,
            "utm_epsg":       utm,
        }
    except Exception:
        return None


def fetch_socrata_state(
    state_fips: str,
    token: str = "",
    facility_type_filter: list = None,
    fsystem_filter: list = None,
    extra_cols: list = None,
    progress_callback=None,
    url: str = None,
    include_missing_facility: bool = False,
    include_missing_fsystem: bool = False,
) -> pd.DataFrame:
    """
    Downloads HPMS records for a single state from Socrata, with the
    facility-type and functional-system filters applied server-side via
    the $where clause (so unwanted rows -- e.g. ramps, non-inventory
    direction -- are never pulled over the network in the first place).

    url, if provided, overrides the SOCRATA_DEFAULT endpoint (e.g. for
    pointing at a different dataset/resource ID).

    extra_cols is accepted for callers that list the HPMS fields they need
    (hpms_4d_enricher_cli.py). The query does not use $select, so every
    field is returned and extra_cols has no effect on the request.

    progress_callback, if provided, is called after each page is fetched
    as progress_callback(rows_fetched_so_far) -- e.g. for a GUI status bar.

    include_missing_facility / include_missing_fsystem also select records
    with no facility type / no functional system (see normalize_fsystem).
    With no functional system filter, every coded functional system is
    returned.
    """
    ft = facility_type_filter or [1, 2]
    ft_parts = ["facility_type IN (" + ", ".join(f"'{v}'" for v in ft) + ")"]
    if include_missing_facility:
        ft_parts.append("facility_type IS NULL")
    ft_clause = "AND (" + " OR ".join(ft_parts) + ")"

    # Records with no functional system come only when selected
    # (Missouri's are dropped on loading).
    if fsystem_filter:
        fs_parts = ["f_system IN (" + ", ".join(f"'{v}'" for v in fsystem_filter) + ")"]
        if include_missing_fsystem:
            fs_parts.append("f_system IS NULL")
        fs_clause = "AND (" + " OR ".join(fs_parts) + ")"
    else:
        fs_clause = "" if include_missing_fsystem else "AND f_system IS NOT NULL"

    where_clause = f"stateid='{state_fips}' {ft_clause} {fs_clause}".strip()
    # Paging with $offset needs an explicit $order for a stable row order.
    # ShapeId follows each state's submission (digitization) order, which
    # is the order stitch_linestrings_by_shapeid() expects.
    params = {"$limit": 100_000, "$offset": 0, "$where": where_clause, "$order": "shapeid"}

    logging.info(f"Fetching Socrata data for State FIPS {state_fips}...")
    rows = _socrata_rows(url or SOCRATA_DEFAULT, token, params, progress_callback)
    if not rows:
        raise ValueError(f"No records found for State {state_fips}.")
    return _socrata_frame(rows)


def fetch_socrata_nearby(
    state_fips: str,
    bbox: tuple,
    token: str = "",
    url: str = None,
) -> pd.DataFrame:
    """
    Every HPMS record in one state whose geometry falls in bbox
    (minx, miny, maxx, maxy, EPSG:4326), of any facility type or
    functional system: the non-inventory direction, ramps, frontage roads
    and cross streets around a plan/profile section. Returns the same
    columns as fetch_socrata_state(); empty if nothing is found.
    """
    socrata_url = url or SOCRATA_DEFAULT
    probe = _socrata_rows(socrata_url, token, {"$limit": 1, "$where": f"stateid='{state_fips}'"})
    if not probe:
        return pd.DataFrame()
    geom_col = next((k for k, v in probe[0].items() if isinstance(v, dict) and "type" in v), None)
    if geom_col is None:
        raise ValueError("Could not identify geometry column in Socrata response.")
    minx, miny, maxx, maxy = bbox
    box = (f"'POLYGON(({minx} {miny}, {maxx} {miny}, {maxx} {maxy}, "
           f"{minx} {maxy}, {minx} {miny}))'")
    # intersects() keeps lines that only pass through the box (a long
    # opposite-direction section reaching miles beyond it); within_box() is
    # the fallback for endpoints without it.
    rows = None
    for test in (f"intersects({geom_col}, {box})",
                 f"within_box({geom_col}, {maxy}, {minx}, {miny}, {maxx})"):
        try:
            rows = _socrata_rows(socrata_url, token,
                                 {"$limit": 100_000, "$offset": 0, "$order": "shapeid",
                                  "$where": f"stateid='{state_fips}' AND {test}"})
            break
        except requests.RequestException as e:
            logging.warning(f"Socrata spatial query {test.split('(')[0]}() failed ({e}).")
    return _socrata_frame(rows) if rows else pd.DataFrame()


def _socrata_rows(socrata_url: str, token: str, params: dict, progress_callback=None) -> list:
    """All rows for one SoQL query, paging by $offset when $limit is
    100,000, with retries on temporary errors."""
    headers = {"X-App-Token": token} if token else {}
    params = dict(params)
    rows = []

    max_attempts = 6
    # Reuse one TCP+TLS session for all paginated requests to the same
    # Socrata host rather than opening a fresh connection per page.
    with requests.Session() as session:
        session.headers.update(headers)
        while True:
            r = None
            for attempt in range(1, max_attempts + 1):
                try:
                    r = session.get(socrata_url, params=params, timeout=120)
                    if r.status_code in (429, 500, 502, 503, 504):
                        if attempt == max_attempts:
                            raise RuntimeError(
                                f"Socrata returned {r.status_code} on all {max_attempts} attempts.")
                        wait_s = _jittered(min(60, 2 ** attempt))
                        logging.warning(
                            f"Socrata temporary error {r.status_code}. "
                            f"Attempt {attempt}/{max_attempts}. Retrying in {wait_s:.0f}s..."
                        )
                        time.sleep(wait_s)
                        continue
                    r.raise_for_status()
                    break
                except requests.RequestException as ex:
                    status = getattr(getattr(ex, "response", None), "status_code", None)
                    if attempt == max_attempts or (status is not None and 400 <= status < 500):
                        raise   # a rejected query won't succeed on retry
                    wait_s = _jittered(min(60, 2 ** attempt))
                    logging.warning(
                        f"Socrata request failed ({ex}). "
                        f"Attempt {attempt}/{max_attempts}. Retrying in {wait_s:.0f}s..."
                    )
                    time.sleep(wait_s)

            if r is None:
                raise RuntimeError("Failed to get Socrata response.")

            data = r.json()
            if not data:
                break
            rows.extend(data)

            if progress_callback:
                try:
                    progress_callback(len(rows))
                except TypeError as e:
                    logging.warning(f"Progress callback failed: invalid argument type - {e}")
                except Exception as e:
                    logging.warning(f"Progress callback error (data fetch will continue): {type(e).__name__}: {e}")

            if len(data) < 100_000:
                break
            params["$offset"] += 100_000

    return rows


# Records with no functional system or no facility type. Some States leave
# F_System blank on their local roads instead of coding them 7, and some
# leave Facility_Type blank as well. Such records are processed only when
# the run selects them (the "Missing" filter boxes; FSYSTEM_INCLUDE_MISSING
# and FACILITY_TYPE_INCLUDE_MISSING), as functional class 7 and facility
# type 2 (two-way roadway). FSystem_Missing and Facility_Type_Missing mark
# them. Missouri (29) also submits railroad lines with no functional
# system, so its records with no functional system are always dropped.
NULL_FSYSTEM_CLASS = 7
NULL_FACILITY_TYPE = 2
NULL_FSYSTEM_EXCLUDED_STATES = {29: "railroad lines"}

_STATE_COLUMNS = ("stateid", "state_id", "state_fips", "statefips", "state")


def _state_int(v):
    """State FIPS code as an int, or None."""
    try:
        return int(float(str(v).strip()))
    except (TypeError, ValueError):
        return None


def _missing_flag(df: pd.DataFrame, col: str, flag_col: str) -> pd.Series:
    """True where col is blank, or where flag_col (kept from an earlier
    load, e.g. in a saved Socrata extract) already marks the value as
    filled in."""
    missing = pd.to_numeric(df[col], errors="coerce").isna()
    if flag_col in df.columns:
        missing |= df[flag_col].astype(str).str.strip().str.lower().isin(["true", "1"])
    return missing


def normalize_fsystem(df: pd.DataFrame) -> pd.DataFrame:
    """
    FSystem as int. Records with no functional system are marked in
    FSystem_Missing and given NULL_FSYSTEM_CLASS, except in
    NULL_FSYSTEM_EXCLUDED_STATES, where they are dropped. The State comes
    from the record's State column.
    """
    missing = _missing_flag(df, "FSystem", "FSystem_Missing")
    if missing.any():
        state_col = next((c for c in df.columns if c.lower() in _STATE_COLUMNS), None)
        if state_col is not None:
            states = pd.to_numeric(df[state_col], errors="coerce")
            drop = missing & states.isin(list(NULL_FSYSTEM_EXCLUDED_STATES))
        else:
            drop = pd.Series(False, index=df.index)
            logging.warning(
                "HPMS data has records with no functional system and no State "
                "column; the State exclusions cannot be applied to them."
            )
        n_drop = int(drop.sum())
        if n_drop:
            logging.info(f"Dropped {n_drop:,} records with no functional system "
                         f"in State(s) where those records are not roads.")
        df = df[~drop].copy()
        missing = missing[~drop]
        n_missing = int(missing.sum())
        if n_missing:
            logging.info(f"{n_missing:,} records have no functional system (processed as "
                         f"functional class {NULL_FSYSTEM_CLASS} when selected).")
    fs = pd.to_numeric(df["FSystem"], errors="coerce")
    df["FSystem"] = fs.where(~missing, NULL_FSYSTEM_CLASS).fillna(NULL_FSYSTEM_CLASS).astype(int)
    df["FSystem_Missing"] = missing.astype(bool)
    return df


def normalize_facility_type(df: pd.DataFrame) -> pd.DataFrame:
    """Facility_Type as int. Records with no facility type are marked in
    Facility_Type_Missing and given NULL_FACILITY_TYPE."""
    missing = _missing_flag(df, "Facility_Type", "Facility_Type_Missing")
    n_missing = int(missing.sum())
    if n_missing:
        logging.info(f"{n_missing:,} records have no facility type (processed as facility "
                     f"type {NULL_FACILITY_TYPE} when selected).")
    ft = pd.to_numeric(df["Facility_Type"], errors="coerce")
    df["Facility_Type"] = ft.where(~missing, NULL_FACILITY_TYPE).fillna(NULL_FACILITY_TYPE).astype(int)
    df["Facility_Type_Missing"] = missing.astype(bool)
    return df


def _socrata_frame(rows: list) -> pd.DataFrame:
    """HPMS rows from Socrata as a DataFrame with a WKT geometry column and
    the suite's standard column names and types."""
    df = pd.DataFrame(rows)
    geom_col = next((c for c in df.columns if c.lower() in ["line", "geometry", "the_geom"]), None)

    if geom_col is None:
        raise ValueError("Could not identify geometry column in Socrata response.")

    def geom_to_wkt(v):
        if isinstance(v, dict):
            try:
                return shape(v).wkt
            except ValueError as e:
                logging.debug(f"Invalid geometry dict structure: {e}")
                return None
            except KeyError as e:
                logging.debug(f"Geometry dict missing required field {e}")
                return None
            except Exception as e:
                logging.debug(f"Failed to convert geometry to WKT: {type(e).__name__}")
                return None
        return str(v)

    df["WKT"] = df[geom_col].apply(geom_to_wkt)

    # Drop the raw geometry column now that WKT has been built from it, so a
    # saved extract has one geometry column and load_local_hpms() cannot
    # pick the unparseable dict-string column by mistake.
    df.drop(columns=[geom_col], inplace=True)

    col_map = {
        "route_id":    "RouteId",
        "begin_point": "Start_MP",
        "end_point":   "End_MP",
        "f_system":    "FSystem",
        "facility_type": "Facility_Type",
        "structure_type": "Structure_Type",
        "shapeid":     "ShapeId",
        "county_id":   "County_ID",
    }
    df.rename(columns=col_map, inplace=True)

    if "Start_MP" not in df.columns: df["Start_MP"] = 0.0
    if "End_MP" not in df.columns: df["End_MP"] = 0.0
    # Socrata leaves out a field that is null on every row returned, so a
    # pull of records with no functional system has no f_system column
    # (the same for facility_type).
    if "FSystem" not in df.columns: df["FSystem"] = np.nan
    if "Facility_Type" not in df.columns: df["Facility_Type"] = np.nan
    if "Structure_Type" not in df.columns: df["Structure_Type"] = 0
    if "ShapeId" not in df.columns:
        logging.warning(
            "Socrata response did not include a shapeid field -- "
            "stitch_linestrings_by_shapeid() will fall back to spatial "
            "stitching for every route in this state."
        )
        df["ShapeId"] = np.nan
    if "County_ID" not in df.columns: df["County_ID"] = None

    df["Start_MP"] = pd.to_numeric(df["Start_MP"], errors="coerce").fillna(0.0)
    df["End_MP"]   = pd.to_numeric(df["End_MP"],   errors="coerce").fillna(0.0)
    df = normalize_fsystem(df)
    df = normalize_facility_type(df)
    df["Structure_Type"] = pd.to_numeric(df["Structure_Type"], errors="coerce").fillna(0).astype(int)
    df["ShapeId"] = pd.to_numeric(df["ShapeId"], errors="coerce")
    df = df[df["WKT"].notna() & (df["WKT"] != "")].copy()
    return df


def load_local_hpms(path: str) -> pd.DataFrame:
    if not os.path.exists(path):
        raise FileNotFoundError(path)
    if path.lower().endswith(".csv"):
        file_size_gb = os.path.getsize(path) / (1024 ** 3)
        # RouteId is text in HPMS. If every value in a file looks numeric
        # ("001"), read_csv would parse it as a number and drop the leading
        # zeros, so the column is read as text. Its name is not yet
        # standardized here (route_id, Route, id, ...), so the header is read
        # first to find it.
        route_id_candidates = ["route_id", "routeid", "route", "id"]
        header_only = pd.read_csv(path, nrows=0)
        route_id_source_col = None
        for col in header_only.columns:
            if col.lower() in route_id_candidates:
                route_id_source_col = col
                break
        forced_dtype = {route_id_source_col: str} if route_id_source_col else None
        if file_size_gb > 2.0:
            # Large national extracts are read in chunks, with numeric columns
            # downcast per chunk to limit the memory pd.concat needs. Text
            # columns stay as objects: category dtype behaves inconsistently
            # across chunks with different category sets.
            logging.info(
                f"Large CSV detected ({file_size_gb:.1f} GB) -- "
                f"reading in chunks with numeric downcasting to reduce peak memory usage..."
            )
            chunks = []
            chunk_size = 500_000
            for i, chunk in enumerate(pd.read_csv(path, low_memory=False, chunksize=chunk_size, dtype=forced_dtype)):
                for col in chunk.select_dtypes(include=["float64"]).columns:
                    chunk[col] = pd.to_numeric(chunk[col], downcast="float")
                for col in chunk.select_dtypes(include=["int64"]).columns:
                    chunk[col] = pd.to_numeric(chunk[col], downcast="integer")
                chunks.append(chunk)
                if (i + 1) % 10 == 0:
                    logging.info(f"  Read {(i + 1) * chunk_size:,} rows...")
            logging.info(f"  Concatenating {len(chunks)} chunks ({(i + 1) * chunk_size:,}+ rows)...")
            df = pd.concat(chunks, ignore_index=True, copy=False)
            del chunks
        else:
            df = pd.read_csv(path, low_memory=False, dtype=forced_dtype)
        # Geometry column candidates in priority order (WKT first, a raw
        # Socrata "line" column last), whatever their order in the file.
        geom_col = None
        for candidate in ["wkt", "wkt_zm", "geometry", "shape", "the_geom", "line"]:
            matches = [c for c in df.columns if c.lower() == candidate]
            if matches:
                geom_col = matches[0]
                break
        if geom_col and geom_col != "WKT":
            df["WKT"] = df[geom_col]
    else:
        gdf = gpd.read_file(path)
        if gdf.crs and gdf.crs.to_epsg() != 4326:
            logging.info("Reprojecting local GIS file to EPSG:4326...")
            gdf = gdf.to_crs(epsg=4326)
        df = pd.DataFrame(gdf.drop(columns="geometry"))
        df["WKT"] = gdf["geometry"].apply(lambda g: g.wkt if g else None)

    col_map = {}
    for col in df.columns:
        c = col.lower()
        if c in ["route_id", "routeid", "route", "id"]: col_map[col] = "RouteId"
        elif c in ["begin_point", "start_mp", "bmp", "begin", "beg_mp"]: col_map[col] = "Start_MP"
        elif c in ["end_point", "end_mp", "emp", "end"]: col_map[col] = "End_MP"
        elif c in ["f_system", "fsystem", "func_sys"]: col_map[col] = "FSystem"
        elif c in ["facility_type", "fac_type", "facility_typ", "facilitytype"]: col_map[col] = "Facility_Type"
        elif c in ["structure_type", "structuretype", "struct_typ"]: col_map[col] = "Structure_Type"
        elif c in ["shapeid", "shape_id"]: col_map[col] = "ShapeId"
        elif c in ["county_id", "countyid", "county"]: col_map[col] = "County_ID"
    df.rename(columns=col_map, inplace=True)

    if "RouteId" not in df.columns: raise ValueError("Missing RouteId column.")
    if "WKT" not in df.columns: raise ValueError("Missing geometry/WKT column.")

    if "Start_MP" not in df.columns: df["Start_MP"] = 0.0
    if "End_MP" not in df.columns: df["End_MP"] = 0.0
    if "FSystem" not in df.columns:
        logging.warning("Local HPMS file has no functional system column; every record "
                        "is read as having none.")
        df["FSystem"] = np.nan
    if "Facility_Type" not in df.columns:
        logging.warning("Local HPMS file has no facility type column; every record "
                        "is read as having none.")
        df["Facility_Type"] = np.nan
    if "Structure_Type" not in df.columns: df["Structure_Type"] = 0
    if "ShapeId" not in df.columns:
        logging.warning(
            "Local HPMS file has no ShapeId/shape_id column -- "
            "stitch_linestrings_by_shapeid() will fall back to spatial "
            "stitching for every route loaded from this file."
        )
        df["ShapeId"] = np.nan
    if "County_ID" not in df.columns: df["County_ID"] = None

    df["RouteId"] = df["RouteId"].astype(str).str.strip().str.upper()
    df["Start_MP"] = pd.to_numeric(df["Start_MP"], errors="coerce").fillna(0.0)
    df["End_MP"] = pd.to_numeric(df["End_MP"], errors="coerce").fillna(0.0)
    df = normalize_fsystem(df)
    df = normalize_facility_type(df)
    df["Structure_Type"] = pd.to_numeric(df["Structure_Type"], errors="coerce").fillna(0).astype(int)
    df["ShapeId"] = pd.to_numeric(df["ShapeId"], errors="coerce")
    df = df[df["WKT"].notna()].copy()
    df["WKT"] = df["WKT"].astype(str).str.strip()
    df = df[~df["WKT"].str.lower().isin(["", "nan", "none"])].copy()
    return df

# -------------------------
# Download National Bridge and Tunnel data
# -------------------------
def compute_padded_bbox(geometries, pad_deg: float = 0.1) -> tuple:
    """
    Returns a padded (minx, miny, maxx, maxy) bounding box, in the same CRS
    as the input geometries (typically EPSG:4326), from any iterable of
    shapely geometries. Used to limit the national NBI/NTI points to a
    state's or route's working area.

    Default 0.1 deg padding (~11 km at mid latitudes) so bridges/tunnels
    just outside a state or route's own footprint -- which can still be the
    nearest match for a route hugging a state line -- aren't dropped.
    """
    minx, miny, maxx, maxy = gpd.GeoSeries(list(geometries)).total_bounds
    return (minx - pad_deg, miny - pad_deg, maxx + pad_deg, maxy + pad_deg)


# NBI/NTI structures for each worker process of a ProcessPoolExecutor, set
# once per process by init_worker_nbi() instead of being sent with every task
# (thousands of tasks per state would each carry a copy).
#
# Usage from a calling CLI:
#   from core.rat_core import init_worker_nbi
#   with ProcessPoolExecutor(..., initializer=init_worker_nbi,
#                            initargs=(nbi_nti_gdf,)) as executor:
#       ...
# Then, inside the worker function body (NOT at module import time, so the
# fresh per-process value set by the initializer is actually picked up):
#   from core.rat_core import WORKER_NBI_NTI_GDF
WORKER_NBI_NTI_GDF = None


def init_worker_nbi(nbi_nti_gdf):
    """
    ProcessPoolExecutor initializer. Runs once when each worker process
    starts, setting the module-level NBI/NTI reference for that process
    only. This means the structures GeoDataFrame is pickled and sent
    exactly max_workers times (once per process) rather than once per task
    submission.
    """
    global WORKER_NBI_NTI_GDF
    WORKER_NBI_NTI_GDF = nbi_nti_gdf


def run_bounded_parallel(work_items, submit_fn, on_done, max_in_flight):
    """
    Runs a list of work items through a ProcessPoolExecutor-style submit_fn
    in a bounded rolling window, rather than submitting every task upfront.
    A large state can have hundreds of thousands of tasks (California about
    394,000); keeping a small multiple of the worker count in flight bounds
    memory while keeping every worker busy.

    Parameters
    ----------
    work_items : list
        One entry per task. Consumed via iter(), so any list-like
        sequence works.
    submit_fn : callable(item) -> concurrent.futures.Future
        Called once per work item to actually submit it (typically wraps
        executor.submit(worker_func, *args_derived_from_item)).
    on_done : callable(item, future) -> None
        Called once per completed future, in the main process, one at a
        time -- so it's safe to mutate shared aggregation lists/dicts or
        update a progress bar from here without extra locking. Must call
        future.result() itself and handle/log any exception. An exception
        raised by on_done itself cancels the tasks not yet started and
        propagates (used to stop a run when the DEM folder is lost).
    max_in_flight : int
        Maximum number of futures allowed pending at once. Callers
        typically pass something like max_workers * 4.

    Returns
    -------
    None. All results are handled via on_done's side effects.
    """
    from concurrent.futures import wait, FIRST_COMPLETED

    work_iter = iter(work_items)
    fut_to_item = {}

    def _submit_next():
        try:
            item = next(work_iter)
        except StopIteration:
            return None
        fut = submit_fn(item)
        fut_to_item[fut] = item
        return fut

    pending = set()
    for _ in range(max_in_flight):
        fut = _submit_next()
        if fut is None:
            break
        pending.add(fut)

    try:
        while pending:
            done, pending = wait(pending, return_when=FIRST_COMPLETED)
            for fut in done:
                item = fut_to_item.pop(fut)
                on_done(item, fut)
                new_fut = _submit_next()
                if new_fut is not None:
                    pending.add(new_fut)
    except BaseException:
        # on_done raised (e.g. DemCacheUnavailable): cancel the tasks not
        # yet started so the run stops promptly.
        for fut in pending:
            fut.cancel()
        raise


def fetch_nbi_nti_state(state_fips: str, bbox: tuple = None) -> gpd.GeoDataFrame:
    """
    NBI bridge and NTI tunnel locations from the files in the suite's NBI
    folder (any .txt, .csv or .xml), as points in EPSG:4326, with the kind
    of structure ("kind": 1 bridge, 2 tunnel) and its length in meters
    ("length_m": NBI Item 49, or NTI item G1 converted from feet; NaN if
    not given). Only NBI main records (Item 5A = 1) are kept; the other
    records of a structure repeat its location and carry no length.

    bbox, if given, is (minx, miny, maxx, maxy) in EPSG:4326, typically the
    state's or route's bounds padded by compute_padded_bbox(). The files are
    national, so the points are limited to bbox after loading. state_fips
    labels the log message.
    """
    points = []

    # The NBI folder sits next to core/ in the suite folder.
    core_dir = os.path.dirname(os.path.abspath(__file__))
    suite_dir = os.path.dirname(core_dir)
    nbi_folder = os.path.join(suite_dir, "NBI")

    if os.path.exists(nbi_folder):
        local_files = glob.glob(os.path.join(nbi_folder, "*.*"))
        for file_path in local_files:
            if not (file_path.lower().endswith(".csv") or file_path.lower().endswith(".txt") or file_path.lower().endswith(".xml")):
                continue

            logging.info(f"Loading bridge/tunnel locations from {os.path.basename(file_path)}...")
            try:
                if file_path.lower().endswith(".csv") or file_path.lower().endswith(".txt"):
                    df = pd.read_csv(file_path, dtype=str)
                else:
                    # The NTI XML can contain bare ampersands; escape them before parsing.
                    import xml.etree.ElementTree as ET
                    import re

                    with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
                        xml_str = f.read()

                    xml_str = re.sub(r'&(?![A-Za-z0-9#]+;)', '&amp;', xml_str)

                    root = ET.fromstring(xml_str)
                    xml_data = []
                    for child in root:
                        row = {sub.tag: sub.text for sub in child}
                        xml_data.append(row)
                    df = pd.DataFrame(xml_data)

                df.columns = [str(c).upper() for c in df.columns]

                is_nbi = "LAT_016" in df.columns
                if is_nbi and "RECORD_TYPE_005A" in df.columns:
                    df = df[df["RECORD_TYPE_005A"].astype(str).str.strip() == "1"]
                for _, row in df.iterrows():
                    try:
                        # Look for Bridge Coordinates (LAT_016) OR Tunnel Coordinates (I13)
                        raw_lat = row.get("LAT_016", row.get("I13", row.get("LATITUDE", row.get("LAT", 0))))
                        raw_lon = row.get("LONG_017", row.get("I14", row.get("LONGITUDE", row.get("LONG", row.get("LON", 0)))))

                        if pd.isna(raw_lat) or pd.isna(raw_lon) or raw_lat == 0 or raw_lon == 0:
                            continue

                        lat, lon = float(raw_lat), float(raw_lon)

                        # Convert NBI DDMMSS format if necessary (Tunnels are already decimal)
                        if abs(lat) > 90.0:
                            lat_s = str(raw_lat).replace('.', '').zfill(8)
                            lat = float(lat_s[:2]) + float(lat_s[2:4])/60.0 + float(lat_s[4:6] + "." + lat_s[6:])/3600.0
                        if lon > 0 and lon > 180.0:
                            lon_s = str(raw_lon).replace('.', '').zfill(9)
                            lon = -(float(lon_s[:3]) + float(lon_s[3:5])/60.0 + float(lon_s[5:7] + "." + lon_s[7:])/3600.0)

                        if is_nbi:
                            kind, length_m = 1, pd.to_numeric(row.get("STRUCTURE_LEN_MT_049"), errors="coerce")
                        else:
                            kind, length_m = 2, pd.to_numeric(row.get("G1"), errors="coerce") / FEET_PER_METER
                        points.append({"geometry": Point(float(lon), float(lat)), "kind": kind,
                                       "length_m": float(length_m) if pd.notna(length_m) and length_m > 0 else np.nan})
                    except (ValueError, TypeError) as e:
                        logging.debug(f"Skipping structure with invalid coordinates: {e}")
                        continue
                    except Exception as e:
                        logging.debug(f"Skipping structure record: {type(e).__name__}")
                        continue
            except Exception as e:
                logging.warning(f"Failed to read local file {file_path}: {e}")

    if points:
        gdf = gpd.GeoDataFrame(points, crs="EPSG:4326")
        if bbox is not None:
            minx, miny, maxx, maxy = bbox
            n_before = len(gdf)
            gdf = gdf.cx[minx:maxx, miny:maxy]
            logging.info(
                f"Loaded {n_before} national bridges/tunnels; "
                f"{len(gdf)} fall within state {state_fips}'s bounding box."
            )
        else:
            logging.info(f"Successfully mapped {len(gdf)} local bridges/tunnels (no bbox filter applied).")
        return gdf.reset_index(drop=True)

    logging.warning(f"No NBI/NTI structures loaded. Put the NBI and NTI files in {nbi_folder} "
                    f"(see download_nbi.bat there). Without them, spans are kept only where an HPMS "
                    f"structure record supports them, or for short bridges with steep sides.")
    return gpd.GeoDataFrame({"kind": [], "length_m": []}, geometry=[], crs="EPSG:4326")

# -------------------------
# Download 1 meter USGS DEMs
# -------------------------
HIGH_RES_TILE_PIXELS = 2400
HIGH_RES_CELL_SIZE_DEG = 0.025


def get_hires_tile_name(lon: float, lat: float, cell_size: float = HIGH_RES_CELL_SIZE_DEG) -> str:
    """
    Returns the high-resolution ("1m") tile name for the sub-degree grid
    cell containing (lon, lat). Cells are named by a signed floor-division
    index pair.
    """
    lon_idx = math.floor(lon / cell_size)
    lat_idx = math.floor(lat / cell_size)
    return f"hr{lat_idx}_{lon_idx}"


def hires_cell_bounds(lon_idx: int, lat_idx: int, cell_size: float = HIGH_RES_CELL_SIZE_DEG) -> tuple:
    """Returns (minx, miny, maxx, maxy) for a high-res grid cell index pair."""
    minx = lon_idx * cell_size
    miny = lat_idx * cell_size
    return (minx, miny, minx + cell_size, miny + cell_size)


def get_needed_dem_cells(geometries, cell_size: float = HIGH_RES_CELL_SIZE_DEG):
    """
    Returns the set of (lon_idx, lat_idx) high-resolution grid cells (see
    get_hires_tile_name()/hires_cell_bounds()) needed to cover the given
    geometries, computed per geometry rather than from one combined
    bounding box. A combined box would span nearly every longitude for a
    route crossing the antimeridian (western Aleutians) and would take in
    the open ocean between islands (Hawaii, Puerto Rico).
    """
    needed = set()
    for geom in geometries:
        if geom is None or geom.is_empty:
            continue
        minx, miny, maxx, maxy = geom.bounds
        if maxx - minx > 180:
            # Crosses the antimeridian: take the cells from the vertices.
            if hasattr(geom, "coords"):
                coord_iter = geom.coords
            elif hasattr(geom, "geoms"):
                coord_iter = [pt for part in geom.geoms for pt in part.coords]
            else:
                coord_iter = []
            for lon, lat in coord_iter:
                needed.add((int(math.floor(lon / cell_size)), int(math.floor(lat / cell_size))))
            continue
        lat_start = int(math.floor(miny / cell_size))
        lat_end   = int(math.ceil(maxy / cell_size))
        lon_start = int(math.floor(minx / cell_size))
        lon_end   = int(math.ceil(maxx / cell_size))
        for lat_idx in range(lat_start, max(lat_end, lat_start + 1)):
            for lon_idx in range(lon_start, max(lon_end, lon_start + 1)):
                needed.add((lon_idx, lat_idx))
    return needed


# Margin kept around a line when finding the DEM cells it needs (degrees,
# about 20 m). The elevations are read at points interpolated along the
# line in UTM, which can sit a little off the straight lon/lat segment.
LINE_CELL_MARGIN_DEG = 0.0002


def get_line_dem_cells(geometries, cell_size: float = HIGH_RES_CELL_SIZE_DEG,
                       margin_deg: float = LINE_CELL_MARGIN_DEG):
    """
    Returns the set of (lon_idx, lat_idx) high-resolution grid cells that
    the given lines actually pass through, within margin_deg. Use it where
    elevations are read only along the lines themselves (the alignment and
    4D enrichment tools). get_needed_dem_cells() takes every cell in each
    geometry's bounding box instead, which for a long diagonal piece of
    road is many cells the road never enters.

    Each segment is sampled at intervals of margin_deg, and every cell
    within margin_deg of a sample is taken. A segment that crosses the
    antimeridian contributes only its end points. Geometries that are not
    points or lines fall back to get_needed_dem_cells().
    """
    needed = set()
    step = max(margin_deg, 1e-6)
    for geom in geometries:
        if geom is None or geom.is_empty:
            continue
        gtype = geom.geom_type
        if gtype in ("LineString", "LinearRing"):
            parts = [geom]
        elif gtype == "MultiLineString":
            parts = list(geom.geoms)
        elif gtype == "Point":
            parts = [LineString([geom.coords[0], geom.coords[0]])]
        else:
            needed |= get_needed_dem_cells([geom], cell_size)
            continue
        for part in parts:
            xy = np.asarray(part.coords, dtype=float)[:, :2]
            if len(xy) == 0:
                continue
            pts = [xy[:1]]
            for (x0, y0), (x1, y1) in zip(xy[:-1], xy[1:]):
                if abs(x1 - x0) > 180:
                    pts.append(np.array([[x1, y1]]))
                    continue
                n = int(math.ceil(math.hypot(x1 - x0, y1 - y0) / step))
                if n > 1:
                    t = np.linspace(0.0, 1.0, n + 1)[1:, None]
                    pts.append(np.array([[x0, y0]]) + t * np.array([[x1 - x0, y1 - y0]]))
                else:
                    pts.append(np.array([[x1, y1]]))
            p = np.vstack(pts)
            for dx in (-margin_deg, margin_deg):
                for dy in (-margin_deg, margin_deg):
                    cells = np.unique(np.column_stack((
                        np.floor((p[:, 0] + dx) / cell_size),
                        np.floor((p[:, 1] + dy) / cell_size))).astype(np.int64), axis=0)
                    needed.update((int(a), int(b)) for a, b in cells)
    return needed


def download_dem_cell(lon_idx: int, lat_idx: int, cache_dir: str) -> str:
    """
    Downloads the single high-resolution grid cell tile covering index
    (lon_idx, lat_idx), as produced by get_needed_dem_cells(). The cell's
    center point is passed to download_high_res_dem_tile(), since its full
    span would also touch the neighboring cells on its edges.
    """
    minx, miny, maxx, maxy = hires_cell_bounds(lon_idx, lat_idx)
    cx, cy = (minx + maxx) / 2, (miny + maxy) / 2
    return download_high_res_dem_tile((cx, cy, cx, cy), cache_dir)


def claim_output_version(folder: str, name_pattern: str) -> Tuple[int, str]:
    """
    Picks the run's output version and claims it at once by creating the
    file name_pattern.format(v=n) for the lowest free n (an empty
    placeholder, replaced when the run writes that file). Another run
    started later, even one still going in the background, then gets the
    next number and cannot overwrite this run's files. Returns (n, path).
    release_output_version() removes the placeholder if the run ends
    without writing the file.
    """
    os.makedirs(folder, exist_ok=True)
    n = 1
    while True:
        path = os.path.join(folder, name_pattern.format(v=n))
        try:
            with open(path, "x"):
                return n, path
        except FileExistsError:
            n += 1


def release_output_version(path: str):
    """Removes a version placeholder left empty (the run wrote nothing to
    it), so it is not mistaken for an output."""
    try:
        if os.path.exists(path) and os.path.getsize(path) == 0:
            os.remove(path)
    except OSError:
        pass


class DemCacheUnavailable(RuntimeError):
    """The DEM cache folder cannot be reached, for example because the
    network drive it is on has disconnected. Every task needs the folder,
    so a run stops on this instead of failing task by task."""


# Waits (seconds) between attempts to reach the DEM cache folder, so a
# network drive that drops for a minute or so does not end a long run.
DEM_CACHE_RETRY_WAITS_S = (3, 6, 12, 24, 48)


def dem_cache_reachable(cache_dir: str) -> bool:
    """True if the DEM cache folder exists and can be reached now."""
    try:
        return os.path.isdir(cache_dir)
    except OSError:
        return False


def require_dem_cache(cache_dir: str) -> str:
    """
    Creates the DEM cache folder if needed and confirms it can be reached,
    retrying for about 1.5 minutes (DEM_CACHE_RETRY_WAITS_S). Raises
    DemCacheUnavailable if it still cannot be reached. Returns cache_dir.
    """
    last_error = None
    for wait_s in (0,) + tuple(DEM_CACHE_RETRY_WAITS_S):
        if wait_s:
            time.sleep(wait_s)
        try:
            os.makedirs(cache_dir, exist_ok=True)
            return cache_dir
        except OSError as e:
            # Another process may have created it at the same moment.
            if dem_cache_reachable(cache_dir):
                return cache_dir
            last_error = e
            logging.debug(f"DEM folder {cache_dir} not reachable: {e}")
    raise DemCacheUnavailable(f"The DEM folder {cache_dir} cannot be reached ({last_error}).")


def raise_if_dem_cache_lost(cache_dir: str, error: Exception = None):
    """Called by a worker after a task fails: raises DemCacheUnavailable if
    the failure was (or coincides with) losing the DEM cache folder, so the
    run stops instead of every remaining task failing the same way."""
    if isinstance(error, DemCacheUnavailable):
        raise error
    if not dem_cache_reachable(cache_dir):
        detail = f" ({error})" if error is not None else ""
        raise DemCacheUnavailable(f"The DEM folder {cache_dir} cannot be reached{detail}.")


def ensure_dem_cells_cached(geometries, cache_dir: str, log_label: str = None,
                            along_lines: bool = False) -> str:
    """
    Ensures every 1m DEM cell touched by `geometries` is downloaded into
    cache_dir (creating it first), using download_dem_cell(). The cells are
    those in each geometry's bounding box (get_needed_dem_cells), or with
    along_lines=True only those the lines pass through (get_line_dem_cells),
    for tools that read elevations only along the lines. Downloads run in
    parallel threads.

    log_label, if given, logs "Pre-fetching N DEM tile(s) for
    <log_label>..." before downloading; pass None (the default) to stay
    silent, e.g. for a per-chunk worker call where that would just add
    noise across thousands of calls.

    Returns cache_dir, for convenient chaining at the call site.
    """
    require_dem_cache(cache_dir)

    needed_cells = get_line_dem_cells(geometries) if along_lines else get_needed_dem_cells(geometries)
    if log_label:
        logging.info(f"Pre-fetching {len(needed_cells)} DEM tile(s) for {log_label}...")

    # A state can need tens of thousands of tiles (Nebraska 17,752), so
    # downloads run in threads with periodic progress messages.
    from concurrent.futures import ThreadPoolExecutor, as_completed
    cells = sorted(needed_cells)
    n_cells = len(cells)
    n_done = 0
    with ThreadPoolExecutor(max_workers=6) as ex:
        futures = {ex.submit(download_dem_cell, lon_idx, lat_idx, cache_dir): (lon_idx, lat_idx)
                   for lon_idx, lat_idx in cells}
        for fut in as_completed(futures):
            lon_idx, lat_idx = futures[fut]
            try:
                fut.result()
            except Exception as e:
                logging.warning(f"DEM tile ({lon_idx},{lat_idx}) failed to download: {e}")
            n_done += 1
            if log_label and (n_done % 200 == 0 or n_done == n_cells):
                logging.info(f"  ...downloaded {n_done:,}/{n_cells:,} DEM tiles for {log_label}")
    return cache_dir


# USGS 3DEP throttling. Under heavy request volume the exportImage service
# answers some requests with 403 (or 429) while still serving others -- a
# temporary refusal, not missing data. All download threads in a process
# share one pause, so a refusal slows the whole process down instead of each
# thread immediately retrying.
USGS_THROTTLE_BASE_WAIT_S = 30.0
USGS_THROTTLE_MAX_WAIT_S = 300.0
USGS_THROTTLE_ALERT_AFTER = 25   # consecutive throttled tiles before a loud warning
USGS_RESUME_STAGGER_S = 10.0     # each thread waits a random 0..this after a shared pause ends
_usgs_throttle_lock = threading.Lock()
_usgs_throttle_until = 0.0
_usgs_consecutive_throttled_tiles = 0


def _jittered(base_s: float) -> float:
    """A random wait between 50% and 150% of base_s, so retries from many
    threads and processes spread out instead of arriving together."""
    return base_s * random.uniform(0.5, 1.5)


def _wait_for_usgs_throttle() -> None:
    paused = False
    while True:
        with _usgs_throttle_lock:
            remaining = _usgs_throttle_until - time.time()
        if remaining <= 0:
            break
        paused = True
        time.sleep(min(remaining, 5.0))
    if paused:
        # Threads released by the same pause resume at random offsets
        # rather than all requesting at once.
        time.sleep(random.uniform(0, USGS_RESUME_STAGGER_S))


def _start_usgs_throttle(wait_s: float) -> None:
    global _usgs_throttle_until
    with _usgs_throttle_lock:
        _usgs_throttle_until = max(_usgs_throttle_until, time.time() + wait_s)


def _record_usgs_tile_outcome(throttled: bool) -> None:
    global _usgs_consecutive_throttled_tiles
    with _usgs_throttle_lock:
        if not throttled:
            _usgs_consecutive_throttled_tiles = 0
            return
        _usgs_consecutive_throttled_tiles += 1
        n = _usgs_consecutive_throttled_tiles
    if n == USGS_THROTTLE_ALERT_AFTER:
        logging.error(
            f"USGS has refused {n} tiles in a row (403/429). It may be blocking "
            f"requests from this machine or network. Elevation results for areas "
            f"needing those tiles will be incomplete until downloads succeed."
        )


def download_high_res_dem_tile(bbox: tuple, cache_dir: str) -> str:
    """
    Downloads the USGS 3DEP high-resolution DEM tile(s) covering the given
    bounding box to cache_dir, skipping any tile already present.

    Tiles are HIGH_RES_CELL_SIZE_DEG degrees per side at
    HIGH_RES_TILE_PIXELS x HIGH_RES_TILE_PIXELS (about 1 m per pixel).

    Returns the path to the primary tile file (for the SW-most cell of
    the bbox). Multiple tiles are downloaded if the bbox spans more than
    one cell.
    """
    import os
    import time
    import logging
    import requests

    minx, miny, maxx, maxy = bbox

    USGS_REST_URL = (
        "https://elevation.nationalmap.gov/arcgis/rest/services"
        "/3DEPElevation/ImageServer/exportImage"
    )

    # A network drive (VPN, SMB) can drop for a few seconds; require_dem_cache
    # retries before giving up.
    require_dem_cache(cache_dir)

    # Every grid cell the bbox covers, not just those at its corners.
    lat_start = int(math.floor(miny / HIGH_RES_CELL_SIZE_DEG))
    lat_end   = int(math.ceil(maxy / HIGH_RES_CELL_SIZE_DEG))
    lon_start = int(math.floor(minx / HIGH_RES_CELL_SIZE_DEG))
    lon_end   = int(math.ceil(maxx / HIGH_RES_CELL_SIZE_DEG))
    needed = set()
    for lat_idx in range(lat_start, max(lat_end, lat_start + 1)):
        for lon_idx in range(lon_start, max(lon_end, lon_start + 1)):
            needed.add((lon_idx, lat_idx))

    primary_path = None
    # One TCP+TLS session shared across all tiles needed by this bbox
    # rather than a new connection per tile.
    with requests.Session() as usgs_session:
        for lon_idx, lat_idx in sorted(needed):
            tile_name = f"hr{lat_idx}_{lon_idx}"
            tile_path = os.path.join(cache_dir, f"tile_1m_{tile_name}.tif")
            if primary_path is None:
                primary_path = tile_path
            if os.path.exists(tile_path):
                logging.debug(f"1m tile cache HIT: {tile_name}")
                continue

            t_minx, t_miny, t_maxx, t_maxy = hires_cell_bounds(lon_idx, lat_idx)

            # HIGH_RES_TILE_PIXELS square over one HIGH_RES_CELL_SIZE_DEG cell.
            tile_size = HIGH_RES_TILE_PIXELS
            bbox_str = f"{t_minx},{t_miny},{t_maxx},{t_maxy}"
            params = {
                "bbox":      bbox_str,
                "bboxSR":    "4326",
                "size":      f"{tile_size},{tile_size}",
                "imageSR":   "4326",
                "format":    "tiff",
                "pixelType": "F32",
                "f":         "image",
            }

            lock_path = tile_path + ".lock"
            failcount_path = tile_path + ".failcount"
            MAX_TOTAL_FAILURES = 5
            max_retries = 4
            success = False

            if os.path.exists(tile_path + ".missing"):
                logging.debug(f"1m tile {tile_name} previously confirmed unavailable -- skipping.")
                continue

            # A failure count kept on disk across calls stops a tile with a
            # lasting USGS-side problem from being retried for hours.
            prior_failures = 0
            if os.path.exists(failcount_path):
                try:
                    with open(failcount_path, 'r') as ff:
                        prior_failures = int(ff.read().strip() or "0")
                except ValueError as e:
                    logging.debug(f"Failcount file contains non-integer value: {e}")
                    prior_failures = 0
                except IOError as e:
                    logging.debug(f"Could not read failcount file: {e}")
                    prior_failures = 0
                except Exception as e:
                    logging.warning(f"Unexpected error reading failcount for {tile_name}: {type(e).__name__}")
                    prior_failures = 0
                if prior_failures >= MAX_TOTAL_FAILURES:
                    logging.error(
                        f"  1m tile {tile_name} has failed {prior_failures} times -- "
                        f"marking unavailable for this run rather than retrying again."
                    )
                    try:
                        with open(tile_path + ".missing", 'w') as mf:
                            mf.write(f"gave up after {prior_failures} failures")
                    except IOError as e:
                        logging.debug(f"Could not write .missing marker for {tile_name}: {e}")
                    except Exception as e:
                        logging.debug(f"Error writing .missing marker: {type(e).__name__}")
                    continue

            # If another worker is already downloading this tile, wait for it.
            # The wait is judged by the lock file's age.
            STALE_LOCK_SECONDS_1M = 600  # comfortably longer than the worst-case
                                          # full retry cycle below (4 attempts x
                                          # 120s timeout + up to 2+4+8=14s backoff)
            waited_any = False
            while os.path.exists(lock_path) and not os.path.exists(tile_path):
                if not waited_any:
                    logging.info(f"  Waiting for concurrent download of {tile_name}...")
                    waited_any = True
                try:
                    lock_age = time.time() - os.path.getmtime(lock_path)
                except OSError:
                    break
                if lock_age > STALE_LOCK_SECONDS_1M:
                    logging.warning(f"  Lock for {tile_name} is {lock_age:.0f}s old -- treating as abandoned, attempting own download")
                    try:
                        os.remove(lock_path)
                    except FileNotFoundError:
                        pass  # Already removed by another worker
                    except OSError as e:
                        logging.debug(f"Could not remove stale lock: {e}")
                    except Exception as e:
                        logging.debug(f"Unexpected error removing lock: {type(e).__name__}")
                    break
                time.sleep(2)

            # Re-check after waiting -- another worker may have completed it
            if os.path.exists(tile_path):
                logging.debug(f"1m tile cache HIT (post-wait): {tile_name}")
                success = True
            else:
                # Claim the download with a lock file. O_CREAT | O_EXCL makes
                # the create-if-not-exists check atomic, so two workers cannot
                # both claim it.
                try:
                    fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                    with os.fdopen(fd, 'w') as lf:
                        lf.write(str(os.getpid()))
                    won_lock = True
                except (FileExistsError, OSError):
                    # Catch OSError to handle Windows SMB network drive locking faults
                    won_lock = False
                except Exception as e:
                    logging.debug(f"Non-fatal error creating lock {lock_path}: {e}")
                    won_lock = True

                if not won_lock:
                    while os.path.exists(lock_path) and not os.path.exists(tile_path):
                        try:
                            lock_age = time.time() - os.path.getmtime(lock_path)
                        except OSError:
                            break
                        if lock_age > STALE_LOCK_SECONDS_1M:
                            logging.warning(f"  Lock for {tile_name} is {lock_age:.0f}s old (after losing race) -- treating as abandoned, attempting own download")
                            try:
                                os.remove(lock_path)
                            except FileNotFoundError:
                                pass  # Already removed
                            except OSError as e:
                                logging.debug(f"Could not remove stale lock (post-race): {e}")
                            except Exception as e:
                                logging.debug(f"Unexpected error removing lock (post-race): {type(e).__name__}")
                            break
                        time.sleep(2)
                    if os.path.exists(tile_path):
                        logging.debug(f"1m tile cache HIT (post-second-wait): {tile_name}")
                        success = True

                throttled = False
                attempts_made = 0
                # What the last attempt ended with, which decides how a
                # failure is recorded (see below): "no_coverage" (USGS has no
                # elevation data there), "network" (the server could not be
                # reached or was overloaded), or "failed" (a bad or rejected
                # response).
                last_outcome = None
                no_coverage_in_a_row = 0
                if not success:
                    for attempt in range(1, max_retries + 1):
                        try:
                            _wait_for_usgs_throttle()
                            attempts_made = attempt
                            throttled = False
                            logging.info(f"CACHE MISS: Downloading 1m tile {tile_name} (attempt {attempt}/{max_retries})...")
                            r = usgs_session.get(USGS_REST_URL, params=params, timeout=120)

                            if r.status_code == 200:
                                content = r.content
                                # Where 3DEP has no coverage (water, gaps) the
                                # endpoint returns HTTP 200 with every pixel the
                                # same. File size cannot tell that from a small
                                # real sliver of land (a coastal road), so the
                                # pixels are checked for any variation.
                                stage_path = tile_path + ".tmp"
                                content_valid = False
                                no_coverage = False
                                if len(content) > 0:
                                    with open(stage_path, 'wb') as f:
                                        f.write(content)
                                    try:
                                        with rasterio.open(stage_path) as test_src:
                                            data = test_src.read(1)
                                        finite = data[np.isfinite(data)]
                                        if finite.size > 0 and np.ptp(finite) > 0:
                                            content_valid = True
                                        else:
                                            no_coverage = True
                                    except Exception as e:
                                        logging.warning(
                                            f"Downloaded content for {tile_name} failed "
                                            f"raster validation: {type(e).__name__}: {e}"
                                        )
                                else:
                                    logging.warning(f"USGS returned HTTP 200 for {tile_name} with an empty body.")

                                if not content_valid:
                                    try:
                                        if os.path.exists(stage_path):
                                            os.remove(stage_path)
                                    except Exception:
                                        pass
                                    # Every pixel the same (water, or outside
                                    # 3DEP coverage). Two such answers in a
                                    # row settle it; no need to keep asking.
                                    last_outcome = "no_coverage" if no_coverage else "failed"
                                    no_coverage_in_a_row = no_coverage_in_a_row + 1 if no_coverage else 0
                                    if no_coverage_in_a_row >= 2:
                                        break
                                    if attempt < max_retries:
                                        base_wait = 2
                                        wait_time = (base_wait ** attempt) + random.uniform(0, 1)
                                        logging.info(
                                            f"No usable elevation data in the response for {tile_name}, "
                                            f"asking again ({attempt}/{max_retries}) in {wait_time:.1f}s..."
                                        )
                                        time.sleep(wait_time)
                                    continue

                                # If another worker finished this tile meanwhile, use
                                # its copy rather than racing on the rename (Windows
                                # raises WinError 32 if two processes touch it at once).
                                if os.path.exists(tile_path):
                                    try:
                                        os.remove(stage_path)
                                    except FileNotFoundError:
                                        pass  # Already removed
                                    except OSError as e:
                                        logging.debug(f"Could not remove temporary stage file: {e}")
                                    except Exception as e:
                                        logging.debug(f"Unexpected error removing stage file: {type(e).__name__}")
                                else:
                                    try:
                                        os.replace(stage_path, tile_path)
                                    except (FileExistsError, PermissionError, OSError):
                                        # Another worker won the race -- our download is
                                        # redundant. Clean up and proceed using the winner's file.
                                        try:
                                            os.remove(stage_path)
                                        except Exception:
                                            pass
                                logging.info(f"  Downloaded: {tile_name} ({len(content)/1024/1024:.1f} MB)")
                                success = True
                                try:
                                    if os.path.exists(failcount_path):
                                        os.remove(failcount_path)
                                except Exception:
                                    pass
                                break

                            elif r.status_code in [500, 502, 503, 504]:
                                # Server overload - use exponential backoff with jitter
                                last_outcome = "network"
                                no_coverage_in_a_row = 0
                                base_wait = 2
                                wait_time = (base_wait ** attempt) + random.uniform(0, 1)
                                logging.warning(
                                    f"USGS server overloaded ({r.status_code}). "
                                    f"Retry {attempt}/{max_retries} in {wait_time:.1f}s..."
                                )
                                time.sleep(wait_time)

                            elif r.status_code in (403, 429):
                                # Throttled (see USGS_THROTTLE_* above). Pause
                                # every download thread in this process, not
                                # just this one, then retry.
                                throttled = True
                                wait_time = _jittered(min(USGS_THROTTLE_MAX_WAIT_S,
                                                          USGS_THROTTLE_BASE_WAIT_S * 2 ** (attempt - 1)))
                                if attempt < max_retries:
                                    logging.warning(
                                        f"USGS refused {tile_name} (HTTP {r.status_code}, likely throttling). "
                                        f"Pausing downloads {wait_time:.0f}s before retry {attempt + 1}/{max_retries}..."
                                    )
                                    _start_usgs_throttle(wait_time)

                            else:
                                last_outcome = "failed"
                                logging.warning(f"USGS rejected the request for 1m tile {tile_name}: HTTP {r.status_code}")
                                break

                        except requests.Timeout as e:
                            # Connection timeout or read timeout
                            last_outcome = "network"
                            no_coverage_in_a_row = 0
                            if attempt < max_retries:
                                base_wait = 2
                                wait_time = (base_wait ** attempt) + random.uniform(0, 1)
                                logging.warning(
                                    f"USGS connection timeout (attempt {attempt}/{max_retries}): {type(e).__name__}. "
                                    f"Retrying in {wait_time:.1f}s..."
                                )
                                time.sleep(wait_time)
                            else:
                                logging.warning(f"USGS timeout on final attempt for {tile_name}: {e}")

                        except requests.ConnectionError as e:
                            # Network/DNS error
                            last_outcome = "network"
                            no_coverage_in_a_row = 0
                            if attempt < max_retries:
                                base_wait = 2
                                wait_time = (base_wait ** attempt) + random.uniform(0, 1)
                                logging.warning(
                                    f"USGS connection error (attempt {attempt}/{max_retries}): {type(e).__name__}. "
                                    f"Retrying in {wait_time:.1f}s..."
                                )
                                time.sleep(wait_time)
                            else:
                                logging.warning(f"USGS connection failed on final attempt for {tile_name}: {e}")

                        except requests.HTTPError as e:
                            # HTTP error (non-200 status)
                            last_outcome = "network"
                            no_coverage_in_a_row = 0
                            if attempt < max_retries:
                                base_wait = 2
                                wait_time = (base_wait ** attempt) + random.uniform(0, 1)
                                logging.warning(
                                    f"USGS HTTP error (attempt {attempt}/{max_retries}): {e}. "
                                    f"Retrying in {wait_time:.1f}s..."
                                )
                                time.sleep(wait_time)
                            else:
                                logging.warning(f"USGS HTTP error on final attempt for {tile_name}: {e}")

                        except requests.RequestException as e:
                            # Catch-all for other requests library exceptions
                            last_outcome = "network"
                            no_coverage_in_a_row = 0
                            if attempt < max_retries:
                                base_wait = 2
                                wait_time = (base_wait ** attempt) + random.uniform(0, 1)
                                logging.warning(
                                    f"USGS request error (attempt {attempt}/{max_retries}): {type(e).__name__}: {e}. "
                                    f"Retrying in {wait_time:.1f}s..."
                                )
                                time.sleep(wait_time)
                            else:
                                logging.warning(f"USGS request failed on final attempt for {tile_name}: {type(e).__name__}: {e}")

                        except Exception as e:
                            # Unexpected error during download. A file error
                            # because the cache folder has gone away stops
                            # the run (DemCacheUnavailable).
                            if isinstance(e, OSError):
                                raise_if_dem_cache_lost(cache_dir, e)
                            last_outcome = "failed"
                            logging.warning(f"Unexpected error downloading {tile_name}: {type(e).__name__}: {e}")
                            break

                # Release lock regardless of outcome
                try:
                    if os.path.exists(lock_path):
                        os.remove(lock_path)
                except FileNotFoundError:
                    pass  # Already removed by another worker
                except OSError as e:
                    logging.debug(f"Could not remove lock file {lock_path}: {e}")
                except Exception as e:
                    logging.debug(f"Unexpected error removing lock: {type(e).__name__}")

                if attempts_made:
                    _record_usgs_tile_outcome(throttled and not success)

                if not success and throttled:
                    # Throttling says nothing about whether the tile exists,
                    # so it never counts toward the permanent "unavailable"
                    # marker -- the tile is simply tried again next time.
                    logging.error(
                        f"1m tile {tile_name} not downloaded: USGS still refusing after "
                        f"{attempts_made} attempt(s) (throttling). Will retry next time it is needed."
                    )
                elif not success and last_outcome == "no_coverage":
                    # USGS answered, with no elevation data: water or outside
                    # 3DEP coverage. Marked so it is not requested again.
                    # Points there have no elevation and are filled from
                    # their neighbors along the route.
                    try:
                        with open(tile_path + ".missing", 'w') as mf:
                            mf.write("no elevation data from USGS (water or outside coverage)")
                        if os.path.exists(failcount_path):
                            os.remove(failcount_path)
                    except Exception as e:
                        logging.debug(f"Could not write .missing marker for {tile_name}: {e}")
                    logging.warning(
                        f"1m tile {tile_name}: USGS has no elevation data here (water or outside "
                        f"3DEP coverage); marked unavailable."
                    )
                elif not success and last_outcome == "network":
                    # Could not reach USGS (network or DNS failure, timeout,
                    # server overload). Says nothing about the tile itself,
                    # so it does not count toward marking it unavailable.
                    logging.error(
                        f"1m tile {tile_name} not downloaded: could not reach the USGS elevation "
                        f"service after {attempts_made} attempt(s). Will retry next time it is needed; "
                        f"elevations there are missing from this run."
                    )
                elif not success:
                    new_count = prior_failures + 1
                    try:
                        with open(failcount_path, 'w') as ff:
                            ff.write(str(new_count))
                    except IOError as e:
                        logging.warning(f"Could not write failcount for {tile_name}: {e}")
                    except Exception as e:
                        logging.warning(f"Unexpected error writing failcount: {type(e).__name__}")
                    logging.error(
                        f"Failed to download 1m tile {tile_name} after {attempts_made} attempt(s) "
                        f"({new_count}/{MAX_TOTAL_FAILURES} total failures so far)."
                    )
                    # Brief backoff before releasing lock so waiting workers
                    # don't all retry simultaneously after a failure
                    time.sleep(5)

    return primary_path or ""

# -------------------------
# Geometry helpers
# -------------------------
def get_appropriate_utm_zone(lon: float, lat: float) -> int:
    zone = int((lon + 180) / 6) + 1
    return (32600 if lat >= 0 else 32700) + zone

def densify_coords_line(coords: List[Tuple[float, float]], spacing_m: float) -> List[Tuple[float, float]]:
    line = LineString(coords)
    if line.length == 0:
        return coords
    num = int(math.ceil(line.length / spacing_m)) + 1
    return [line.interpolate(d).coords[0] for d in np.linspace(0, line.length, num)]

def stitch_linestrings_ordered(wkt_list: List[str], snap_tol=1e-3, return_indices: bool = False,
                                start_mps=None, end_mps=None):
    """
    Stitches route segments. snap_tol=1e-3 (~330 ft) allows the tool to bridge
    minor GIS digitization gaps. It forces termini to match by averaging
    overlapping points into a single joint, preventing "sawtooth" spline
    kinks and elevation whips.

    Fully spatial and direction-agnostic: state DOTs don't always digitize
    adjacent segments in the same physical direction (segment A drawn
    north-to-south, segment B right next to it drawn south-to-north). This
    tests all four endpoint combinations (tail-to-head, tail-to-tail,
    head-to-tail, head-to-head) and reverses a segment's coordinates when
    needed to fit, regardless of submission order or original digitization
    direction.

    start_mps/end_mps, if provided (parallel lists aligned with wkt_list),
    break ties when several candidates lie within snap_tol of the same chain
    end, as happens on streets divided into short segments (80-280 ft) that
    are closer together than snap_tol. The nearest candidate can then be
    the wrong neighbor, making the chain weave back and forth (seen on
    Q Street NW in DC); the candidate whose milepost range best continues
    the chain's is preferred instead.

    Once a chain's two ends meet (a closed ring, e.g. the roadway around a
    traffic circle), it stops growing rather than absorbing more pieces --
    a small loop brings many of its own points close together by nature,
    so a leftover piece's endpoint can easily land near some OTHER point
    along the ring instead of the true remaining gap. This only applies
    once a chain has grown from more than one segment; a single freshly
    popped segment's own head-to-tail distance is not a meaningful
    closure signal (and routinely false-triggers on short segments whose
    own length is under snap_tol).

    Geometry chaining here is otherwise purely spatial -- it has no notion
    of which physical direction is "increasing milepost."

    If return_indices is True, also returns a parallel list of lists, one
    per output LineString, giving the indices into wkt_list that
    contributed to that chunk, ordered to match true head-to-tail
    geometric position along the stitched chain (not submission order).
    """
    segments = []
    for idx, ws in enumerate(wkt_list):
        if not ws:
            continue
        try:
            g = loads(ws)
        except Exception as e:
            # Log the exception and a snippet of the broken string for tracking
            snippet = str(ws)[:50] + "..." if len(str(ws)) > 50 else str(ws)
            logging.warning(f"Failed to parse WKT at segment index {idx}: {e} | WKT: {snippet}")
            continue
        parts = list(g.geoms) if g.geom_type == 'MultiLineString' else [g] if g.geom_type == 'LineString' else []
        for part in parts:
            coords = list(part.coords)
            if len(coords) >= 2:
                seg = {"coords": coords, "idx": idx}
                if start_mps is not None:
                    seg["mp_lo"] = min(start_mps[idx], end_mps[idx])
                    seg["mp_hi"] = max(start_mps[idx], end_mps[idx])
                segments.append(seg)

    stitched_segments = []
    stitched_indices = []
    unassigned = segments.copy()

    def _pt_dist(p1, p2):
        return math.hypot(p1[0] - p2[0], p1[1] - p2[1])

    while unassigned:
        first = unassigned.pop(0)
        current_coords = first["coords"].copy()
        current_indices = [first["idx"]]
        if start_mps is not None:
            chain_mp_lo, chain_mp_hi = first["mp_lo"], first["mp_hi"]

        grown = True
        while grown and unassigned:
            grown = False
            head_chain = current_coords[0]
            tail_chain = current_coords[-1]

            # Collect every candidate within snap_tol, in every orientation,
            # so the milepost tie-break below can choose among them.
            qualifying = []  # (i, orientation, dist, candidate)
            for i, candidate in enumerate(unassigned):
                cand_coords = candidate["coords"]
                head_cand = cand_coords[0]
                tail_cand = cand_coords[-1]

                d_th = _pt_dist(tail_chain, head_cand)
                if d_th < snap_tol:
                    qualifying.append((i, "th", d_th, candidate))
                d_tt = _pt_dist(tail_chain, tail_cand)
                if d_tt < snap_tol:
                    qualifying.append((i, "tt", d_tt, candidate))
                d_ht = _pt_dist(head_chain, tail_cand)
                if d_ht < snap_tol:
                    qualifying.append((i, "ht", d_ht, candidate))
                d_hh = _pt_dist(head_chain, head_cand)
                if d_hh < snap_tol:
                    qualifying.append((i, "hh", d_hh, candidate))

            if not qualifying:
                continue

            is_closed_ring = (
                len(current_indices) > 1
                and _pt_dist(current_coords[0], current_coords[-1]) < snap_tol
            )

            if start_mps is not None:
                def _mp_gap(item):
                    _, _, _, c = item
                    lo, hi = c["mp_lo"], c["mp_hi"]
                    if hi < chain_mp_lo:
                        return chain_mp_lo - hi
                    if lo > chain_mp_hi:
                        return lo - chain_mp_hi
                    return 0.0
                qualifying.sort(key=lambda item: (_mp_gap(item), item[2]))

                # A candidate that continues the chain's milepost range
                # (mp_gap == 0) is always added, even if the chain's ends are
                # already within snap_tol: on a small loop (Dupont Circle)
                # that happens after one or two segments, well before the
                # ring is complete.
                if _mp_gap(qualifying[0]) > 0 and is_closed_ring:
                    break
            else:
                qualifying.sort(key=lambda item: item[2])
                if is_closed_ring:
                    break

            i, orient, dist, candidate = qualifying[0]
            cand_coords = candidate["coords"]
            cand_idx = candidate["idx"]
            head_cand = cand_coords[0]
            tail_cand = cand_coords[-1]

            if orient == "th":
                mid = ((tail_chain[0] + head_cand[0]) / 2.0, (tail_chain[1] + head_cand[1]) / 2.0)
                current_coords[-1] = mid
                current_coords.extend(cand_coords[1:])
                current_indices.append(cand_idx)
            elif orient == "tt":
                mid = ((tail_chain[0] + tail_cand[0]) / 2.0, (tail_chain[1] + tail_cand[1]) / 2.0)
                current_coords[-1] = mid
                current_coords.extend(cand_coords[-2::-1])
                current_indices.append(cand_idx)
            elif orient == "ht":
                mid = ((head_chain[0] + tail_cand[0]) / 2.0, (head_chain[1] + tail_cand[1]) / 2.0)
                current_coords[0] = mid
                current_coords = cand_coords[:-1] + current_coords
                current_indices.insert(0, cand_idx)
            else:  # hh
                mid = ((head_chain[0] + head_cand[0]) / 2.0, (head_chain[1] + head_cand[1]) / 2.0)
                current_coords[0] = mid
                current_coords = cand_coords[:0:-1] + current_coords
                current_indices.insert(0, cand_idx)

            unassigned.pop(i)
            grown = True
            if start_mps is not None:
                chain_mp_lo = min(chain_mp_lo, candidate["mp_lo"])
                chain_mp_hi = max(chain_mp_hi, candidate["mp_hi"])

        if len(current_coords) >= 2:
            stitched_segments.append(LineString(current_coords))
            stitched_indices.append(current_indices)

    if return_indices:
        return stitched_segments, stitched_indices
    return stitched_segments


def _haversine_ft(p1: Tuple[float, float], p2: Tuple[float, float]) -> float:
    R_ft = 3958.8 * 5280
    lon1, lat1 = p1
    lon2, lat2 = p2
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = phi2 - phi1
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlmb / 2) ** 2
    return 2 * R_ft * math.asin(math.sqrt(a))


def stitch_linestrings_by_shapeid(
    wkt_list: List[str],
    shapeids: List[float],
    start_mps: List[float] = None,
    end_mps: List[float] = None,
    coord_tol_ft: float = 100.0,
    return_indices: bool = False,
    route_id: str = None,
):
    """
    Stitches a route's segments in ShapeId order (each state's submission
    order). In the Iowa and Washington extracts checked with
    audit_route_ordering(), consecutive segments in that order meet end to
    start (0.0 ft apart), so they are simply joined in sequence instead of
    searched for the nearest neighbor as stitch_linestrings_ordered() does.

    A gap larger than coord_tol_ft ends the current chunk and starts a new
    one: a route can have more than one physically separate piece.

    Falls back to stitch_linestrings_ordered() for the whole route if any
    segment lacks a usable ShapeId or a WKT fails to parse, since the order
    cannot then be trusted. Each fallback is logged.
    """
    n = len(wkt_list)
    if n == 0:
        return ([], []) if return_indices else []

    def _fallback(reason: str):
        logging.warning(
            f"stitch_linestrings_by_shapeid: {reason} for route "
            f"{route_id!r} -- falling back to stitch_linestrings_ordered()."
        )
        # The same join tolerance as the ShapeId path, in degrees.
        return stitch_linestrings_ordered(
            wkt_list, snap_tol=coord_tol_ft / 330000.0, return_indices=return_indices,
            start_mps=start_mps, end_mps=end_mps,
        )

    shapeids_s = pd.to_numeric(pd.Series(shapeids), errors="coerce")
    if shapeids_s.isna().any():
        return _fallback("missing/invalid ShapeId on one or more segments")

    order = shapeids_s.sort_values(kind="mergesort").index.tolist()

    parsed = []  # list of (orig_idx, coords)
    for idx in order:
        ws = wkt_list[idx]
        if not ws:
            continue
        try:
            g = loads(ws)
        except Exception as e:
            return _fallback(f"WKT parse failure at index {idx} ({e})")
        parts = list(g.geoms) if g.geom_type == 'MultiLineString' else [g] if g.geom_type == 'LineString' else []
        for part in parts:
            coords = list(part.coords)
            if len(coords) >= 2:
                parsed.append((idx, coords))

    if not parsed:
        return ([], []) if return_indices else []

    stitched_segments = []
    stitched_indices = []

    chain_coords = list(parsed[0][1])
    chain_indices = [parsed[0][0]]

    for k in range(len(parsed) - 1):
        nxt_idx, nxt_coords = parsed[k + 1]
        tail = chain_coords[-1]
        head = nxt_coords[0]
        dist_ft = _haversine_ft(tail, head)

        if dist_ft <= coord_tol_ft:
            mid = ((tail[0] + head[0]) / 2.0, (tail[1] + head[1]) / 2.0)
            chain_coords[-1] = mid
            chain_coords.extend(nxt_coords[1:])
            chain_indices.append(nxt_idx)
        else:
            # End of this physical piece: start a new chain.
            if len(chain_coords) >= 2:
                stitched_segments.append(LineString(chain_coords))
                stitched_indices.append(chain_indices)
            chain_coords = list(nxt_coords)
            chain_indices = [nxt_idx]

    if len(chain_coords) >= 2:
        stitched_segments.append(LineString(chain_coords))
        stitched_indices.append(chain_indices)

    if return_indices:
        return stitched_segments, stitched_indices
    return stitched_segments


def audit_route_ordering(
    df: pd.DataFrame,
    coord_tol_ft: float = 5.0,
) -> pd.DataFrame:
    """
    Validates the ShapeId-ordering assumption that
    stitch_linestrings_by_shapeid() relies on, so routes that don't fit
    it are visible instead of silently mis-joined. Runs four checks per
    route, walking its segments in ShapeId order:

      1. Milepost continuity      -- End_MP of segment i vs Start_MP of
                                      segment i+1
      2. Endpoint coordinate match -- physical distance (ft) between the
                                      candidate endpoints; > coord_tol_ft
                                      counts as a mismatch
      3. Join orientation          -- confirms the closest match is
                                      consistently end-of-i -> start-of-
                                      (i+1); a flipped orientation is
                                      flagged even when the distance is
                                      within tolerance
      4. County_ID changes         -- informational only, not a
                                      violation by itself; a route with
                                      county changes but no MP/coordinate/
                                      orientation issues is healthy

    Returns one row per RouteId. Any_Violation is True if check 1, 2, or
    3 found a problem anywhere along the route (County_Changes never sets
    it on its own).
    """
    required = {"RouteId", "WKT", "Start_MP", "End_MP", "ShapeId"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"audit_route_ordering() missing required columns: {missing}")

    has_county = "County_ID" in df.columns
    state_col = next(
        (c for c in df.columns if c.lower() in ["state_id", "stateid", "state", "state_fips"]),
        None
    )

    rows_out = []

    for route_id, g in df.groupby("RouteId", sort=False):
        g = g.reset_index(drop=True)
        n_total = len(g)
        state_val = g[state_col].iloc[0] if state_col else None

        base_row = {
            "RouteId": route_id, "State_ID": state_val,
            "N_Segments": n_total, "N_Joins": max(n_total - 1, 0),
            "MP_Discontinuities": None, "Coord_Mismatches": None,
            "Orientation_Flags": None, "County_Changes": None,
            "Max_Coord_Mismatch_Ft": None, "Any_Violation": True,
            "Violation_Reason": "",
        }

        if g["ShapeId"].isna().any():
            base_row["Violation_Reason"] = "Missing ShapeId -- cannot verify order"
            rows_out.append(base_row)
            continue

        if n_total < 2:
            base_row.update({
                "MP_Discontinuities": 0, "Coord_Mismatches": 0,
                "Orientation_Flags": 0, "County_Changes": 0,
                "Max_Coord_Mismatch_Ft": 0.0, "Any_Violation": False,
            })
            rows_out.append(base_row)
            continue

        g = g.sort_values("ShapeId", kind="mergesort").reset_index(drop=True)

        try:
            geoms = [loads(w) for w in g["WKT"]]
        except Exception as e:
            base_row["Violation_Reason"] = f"WKT parse failure: {e}"
            rows_out.append(base_row)
            continue

        starts = [list(gm.coords)[0] for gm in geoms]
        ends = [list(gm.coords)[-1] for gm in geoms]

        mp_disc = coord_mismatch = orientation_flags = county_changes = 0
        max_mismatch = 0.0

        for i in range(n_total - 1):
            ep_i = float(g.loc[i, "End_MP"])
            bp_j = float(g.loc[i + 1, "Start_MP"])
            if abs(ep_i - bp_j) > 1e-6:
                mp_disc += 1

            if has_county:
                c_i, c_j = g.loc[i, "County_ID"], g.loc[i + 1, "County_ID"]
                if pd.notna(c_i) and pd.notna(c_j) and c_i != c_j:
                    county_changes += 1

            combos = {
                "end-start":   _haversine_ft(ends[i], starts[i + 1]),
                "end-end":     _haversine_ft(ends[i], ends[i + 1]),
                "start-start": _haversine_ft(starts[i], starts[i + 1]),
                "start-end":   _haversine_ft(starts[i], ends[i + 1]),
            }
            best_key = min(combos, key=combos.get)
            best_dist = combos[best_key]
            max_mismatch = max(max_mismatch, best_dist)

            if best_dist > coord_tol_ft:
                coord_mismatch += 1
            elif best_key != "end-start":
                orientation_flags += 1

        base_row.update({
            "MP_Discontinuities": mp_disc,
            "Coord_Mismatches": coord_mismatch,
            "Orientation_Flags": orientation_flags,
            "County_Changes": county_changes,
            "Max_Coord_Mismatch_Ft": round(max_mismatch, 2),
            "Any_Violation": (mp_disc > 0 or coord_mismatch > 0 or orientation_flags > 0),
        })
        rows_out.append(base_row)

    return pd.DataFrame(rows_out)


def _fsystem_miles(records: pd.DataFrame) -> Dict[int, float]:
    """{functional system: miles} over the given HPMS records, from each
    record's milepost length (End_MP - Start_MP)."""
    fs = pd.to_numeric(records["FSystem"], errors="coerce")
    miles = (pd.to_numeric(records["End_MP"], errors="coerce")
             - pd.to_numeric(records["Start_MP"], errors="coerce")).abs()
    ok = fs.notna() & miles.notna()
    return {int(k): float(v) for k, v in miles[ok].groupby(fs[ok].astype(int)).sum().items()}


def _predominant_fsystem(fs_miles: Dict[int, float], fallback: int) -> int:
    """The functional system with the most miles. A tie goes to the lower
    (higher-class) number; with no usable mileage, returns fallback."""
    if not fs_miles or max(fs_miles.values()) <= 0:
        return fallback
    return min(fs_miles, key=lambda k: (-fs_miles[k], k))


_RP_JUMP_TOL_MI = 0.001     # reference points closer than this count as continuous


def _scaled_line(wkt: str, kx: float) -> Optional[LineString]:
    """A WKT line (a multi-line's parts joined in order) with longitude
    multiplied by kx; None if it cannot be read."""
    try:
        g = loads(str(wkt))
    except Exception:
        return None
    parts = list(g.geoms) if hasattr(g, "geoms") else [g]
    pts = [c[:2] for p in parts if not p.is_empty and hasattr(p, "coords") for c in p.coords]
    if len(pts) < 2:
        return None
    a = np.asarray(pts, dtype=float)
    return LineString(np.c_[a[:, 0] * kx, a[:, 1]])


def _split_at_rp_jumps(chunk: dict, subset: pd.DataFrame, route_f_sys: int) -> List[dict]:
    """
    The chunk, cut wherever two records that meet along it do not continue
    each other's reference points and the reference points between them are
    carried by some other record of the route. Reference points are
    interpolated along a chunk from its lowest to its highest, so such a
    jump would give the chunk the other record's reference points as well
    (two pieces claiming the same range) and stretch or squeeze the rest.
    Small reference point breaks that no other record covers are left
    joined. Returns [chunk] when nothing is cut.

    Each record is placed along the chunk by projecting its midpoint onto
    the chunk's line (longitude scaled by the cosine of the latitude), its
    two ends half its length either side, and its direction by projecting
    its quarter points, so its reference points can be read in the chunk's
    direction.
    """
    idx = chunk.get("rec_idx")
    g = chunk["geom"]
    if not idx or len(idx) < 2 or g is None or g.is_empty:
        return [chunk]
    coords = np.asarray(g.coords, dtype=float)[:, :2]
    kx = math.cos(math.radians(float(np.mean(coords[:, 1]))))
    line = LineString(np.c_[coords[:, 0] * kx, coords[:, 1]])
    total = line.length
    if total <= 0:
        return [chunk]

    s_mp = pd.to_numeric(subset["Start_MP"], errors="coerce").to_numpy(dtype=float)
    e_mp = pd.to_numeric(subset["End_MP"], errors="coerce").to_numpy(dtype=float)
    wkts = subset["WKT"].tolist()
    recs = []      # (near distance, far distance, RP at near end, RP at far end, position in subset)
    for i in idx:
        rec = _scaled_line(wkts[i], kx)
        if rec is None or not (np.isfinite(s_mp[i]) and np.isfinite(e_mp[i])):
            return [chunk]
        # Interior points place the record: an end point can touch the chunk
        # at two places (a loop closing on itself, a cul-de-sac).
        d25, d50, d75 = (line.project(rec.interpolate(f, normalized=True)) for f in (0.25, 0.5, 0.75))
        half = 0.5 * rec.length
        da, db = d50 - half, d50 + half
        recs.append((da, db, s_mp[i], e_mp[i], i) if d25 <= d75 else (da, db, e_mp[i], s_mp[i], i))
    recs.sort(key=lambda r: (r[0], r[1]))

    lo_all = np.minimum(s_mp, e_mp)
    hi_all = np.maximum(s_mp, e_mp)
    groups, cuts = [[recs[0]]], []
    for a, b in zip(recs[:-1], recs[1:]):
        if abs(a[3] - b[2]) > _RP_JUMP_TOL_MI:
            lo, hi = sorted((a[3], b[2]))
            others = np.ones(len(s_mp), dtype=bool)
            others[[a[4], b[4]]] = False
            cover = np.clip(np.minimum(hi_all, hi) - np.maximum(lo_all, lo), 0.0, None)
            cover = cover[others & np.isfinite(cover)]
            d_cut = 0.5 * (min(a[1], total) + max(b[0], 0.0))
            if cover.size and cover.max() > _RP_JUMP_TOL_MI and 0.0 < d_cut < total:
                cuts.append(d_cut)
                groups.append([])
        groups[-1].append(b)
    if not cuts:
        return [chunk]

    bounds = [0.0] + cuts + [total]
    pieces = []
    for (d0, d1), grp in zip(zip(bounds[:-1], bounds[1:]), groups):
        if d1 - d0 <= 0:
            continue
        part = substring(line, d0, d1)
        pc = np.asarray(part.coords, dtype=float)
        if len(pc) < 2:
            continue
        contrib = subset.iloc[[r[4] for r in grp]]
        fs_miles = _fsystem_miles(contrib)
        pieces.append(dict(
            chunk,
            geom=LineString(np.c_[pc[:, 0] / kx, pc[:, 1]]),
            chunk_low_mp=float(contrib[["Start_MP", "End_MP"]].min().min()),
            chunk_high_mp=float(contrib[["Start_MP", "End_MP"]].max().max()),
            fs_miles=fs_miles,
            f_sys=_predominant_fsystem(fs_miles, route_f_sys),
            rec_idx=[r[4] for r in grp],
        ))
    return pieces if len(pieces) > 1 else [chunk]


def merge_close_chunks(chunks: List[dict], merge_tol_ft: float = 500.0, route_id: str = None) -> List[dict]:
    """
    Second-pass cleanup for chunks built from stitch_linestrings_ordered().

    Each chunk dict must have a "geom" key (a LineString already oriented so
    its coordinates run from low milepost to high milepost) and
    "chunk_low_mp"/"chunk_high_mp" keys.

    stitch_linestrings_ordered() only joins segments whose endpoints lie
    within its snap_tol (~330 ft). Some routes miss by a little more than
    that, most often loops around a traffic circle whose start and end are
    digitized a few hundred feet apart, splitting one ring into 2-3 chunks.

    This repeatedly merges the closest pair of chunk endpoints, in any of
    the four orientations (tail-head, tail-tail, head-tail, head-head), while
    they are within merge_tol_ft. Separate pieces of a route (county-line
    resets, usually miles apart) are never affected. A head-to-head or
    tail-to-tail merge can join a Y-junction or out-and-back spur into one
    chunk; that is rare.

    route_id, if provided, labels the log line listing gaps left unmerged
    (raw distances, before densifying and smoothing).
    """
    chunks = list(chunks)
    if len(chunks) < 2:
        return chunks

    merged_any = True
    while merged_any and len(chunks) > 1:
        merged_any = False
        best = None
        for i in range(len(chunks)):
            for j in range(len(chunks)):
                if i == j:
                    continue
                coords_i = chunks[i]["geom"].coords
                coords_j = chunks[j]["geom"].coords

                d_th = _haversine_ft(coords_i[-1], coords_j[0])
                if best is None or d_th < best[0]:
                    best = (d_th, i, j, "th")
                d_tt = _haversine_ft(coords_i[-1], coords_j[-1])
                if d_tt < best[0]:
                    best = (d_tt, i, j, "tt")
                d_ht = _haversine_ft(coords_i[0], coords_j[-1])
                if d_ht < best[0]:
                    best = (d_ht, i, j, "ht")
                d_hh = _haversine_ft(coords_i[0], coords_j[0])
                if d_hh < best[0]:
                    best = (d_hh, i, j, "hh")

        if best and best[0] <= merge_tol_ft:
            d, i, j, m_type = best
            ci, cj = chunks[i], chunks[j]
            coords_i = list(ci["geom"].coords)
            coords_j = list(cj["geom"].coords)

            if m_type == "th":
                new_coords = coords_i + coords_j[1:]
            elif m_type == "tt":
                new_coords = coords_i + coords_j[-2::-1]
            elif m_type == "ht":
                new_coords = coords_j[:-1] + coords_i
            else:  # "hh"
                new_coords = coords_j[:0:-1] + coords_i

            merged_chunk = dict(ci)
            merged_chunk["geom"] = LineString(new_coords)
            merged_chunk["chunk_low_mp"] = min(ci["chunk_low_mp"], cj["chunk_low_mp"])
            merged_chunk["chunk_high_mp"] = max(ci["chunk_high_mp"], cj["chunk_high_mp"])
            if "rec_idx" in ci and "rec_idx" in cj:
                merged_chunk["rec_idx"] = list(ci["rec_idx"]) + list(cj["rec_idx"])
            if "fs_miles" in ci and "fs_miles" in cj:
                # The merged piece takes the functional system with the most
                # miles across both pieces.
                fs_miles = dict(ci["fs_miles"])
                for k, v in cj["fs_miles"].items():
                    fs_miles[k] = fs_miles.get(k, 0.0) + v
                merged_chunk["fs_miles"] = fs_miles
                merged_chunk["f_sys"] = _predominant_fsystem(fs_miles, ci["f_sys"])
            chunks = [c for k, c in enumerate(chunks) if k not in (i, j)]
            chunks.append(merged_chunk)
            merged_any = True

    if len(chunks) > 1:
        # Log the closest raw gap left between chunks (any orientation).
        closest = None
        for i in range(len(chunks)):
            for j in range(len(chunks)):
                if i == j:
                    continue
                coords_i = chunks[i]["geom"].coords
                coords_j = chunks[j]["geom"].coords
                for pt_i in (coords_i[0], coords_i[-1]):
                    for pt_j in (coords_j[0], coords_j[-1]):
                        d = _haversine_ft(pt_i, pt_j)
                        if closest is None or d < closest[0]:
                            closest = (d, i, j)
        if closest:
            d, i, j = closest
            label = f"route {route_id}" if route_id else "route"
            logging.info(
                f"merge_close_chunks: {label} left with {len(chunks)} unmerged chunks "
                f"(tol={merge_tol_ft}ft). Closest remaining raw gap: {d:.1f}ft between "
                f"chunk MP[{chunks[i]['chunk_low_mp']:.4f}-{chunks[i]['chunk_high_mp']:.4f}] "
                f"and chunk MP[{chunks[j]['chunk_low_mp']:.4f}-{chunks[j]['chunk_high_mp']:.4f}]."
            )
    return chunks


# -------------------------
# Route-level continuity chunking (shared by rat_alignment_cli.py and
# hpms_4d_enricher_cli.py)
# -------------------------
def write_gdf_with_retry(gdf, path: str, driver: str, max_attempts: int = 5, wait_s: float = 3.0) -> None:
    """
    gdf.to_file(path, driver=driver), retrying on PermissionError/OSError.

    OneDrive, antivirus or Windows Search can briefly lock a file just as
    it is written ("being used by another process"). This runs at the end
    of a long state run, so it retries for a few seconds before giving up.
    """
    for attempt in range(max_attempts):
        try:
            gdf.to_file(path, driver=driver)
            return
        except (PermissionError, OSError) as e:
            if attempt == max_attempts - 1:
                raise
            logging.warning(
                f"Could not write {path} (attempt {attempt + 1}/{max_attempts}): "
                f"{e}. Retrying in {wait_s}s..."
            )
            time.sleep(wait_s)


def route_spans_several_fsystems(chunks: List[dict]) -> bool:
    """True if a route's chunks (build_route_continuity_chunks) cover more
    than one functional system, within a piece or between pieces."""
    return (len({c["f_sys"] for c in chunks}) > 1
            or any(sum(v > 0 for v in c.get("fs_miles", {}).values()) > 1 for c in chunks))


def log_fsystem_summary(n_mixed: int, n_routes: int) -> None:
    """Run Log line counting the routes that span more than one functional
    system (the per-piece choices are logged at DEBUG level)."""
    if n_mixed:
        logging.info(
            f"{n_mixed:,} of {n_routes:,} routes span more than one functional system; each "
            f"continuous piece uses the smoothing factor of the system covering most of its "
            f"miles (LOG_FSYSTEM_DETAIL in core/rat_core.py DEFAULTS lists each piece).")


def build_route_continuity_chunks(route_id: str, subset: pd.DataFrame, params: dict,
                                  log_detail: bool = False) -> List[dict]:
    """
    A route's continuity chunks, built once from all of its records: each
    chunk is one physically continuous piece of the route, so chunk
    boundaries fall at real breaks.

    Returns a list of chunk dicts, each with:
        geom, f_sys, fs_miles, state_id, chunk_low_mp, chunk_high_mp,
        length_m, global_start_dist_m
    chunk_low_mp is reported as "Part": it identifies the piece.

    f_sys, which selects the chunk's horizontal smoothing factor, is the
    functional system with the most miles among the chunk's own records
    (fs_miles), so a route that changes class takes the right factor on
    each piece. This matches the calibration, which fits each factor to
    records of that functional system.

    Where the route is not a single functional system, the choice for each
    piece is logged: in the Run Log with log_detail=True (a single
    plan/profile route) or LOG_FSYSTEM_DETAIL, otherwise at DEBUG level,
    with statewide runs logging one summary line (log_fsystem_summary).

    Downloads no DEM tiles; callers fetch the 1 m tiles they need
    (ensure_dem_cells_cached).
    """
    # subset is not sorted by Start_MP: several states restart mileposts at
    # county lines, and sorting would scramble the segments. Each chunk's
    # milepost range comes from its own records, and its geometry keeps the
    # direction in which the segments were stitched.
    # Fallback where a chunk's records carry no usable mileposts.
    route_f_sys = int(pd.to_numeric(subset["FSystem"], errors="coerce").mode()[0])

    state_val = "LOCAL"
    for col in subset.columns:
        if col.lower() in ["state", "state_fips", "state_id", "stateid"]:
            state_val = str(subset[col].iloc[0])
            break

    if state_val == "LOCAL":
        state_val = str(params.get("STATE_FIPS", "LOCAL"))

    all_chunks = []
    # Segments are joined in ShapeId order where possible, falling back to
    # spatial stitching (logged) where ShapeId is missing or unusable; see
    # audit_route_ordering(). CHUNK_JOIN_TOLERANCE_FT covers digitizing gaps
    # at intersections and interchanges.
    join_tol_ft = float(params.get("CHUNK_JOIN_TOLERANCE_FT", 100.0))
    if "ShapeId" in subset.columns:
        lines, chunk_indices = stitch_linestrings_by_shapeid(
            subset["WKT"].tolist(), subset["ShapeId"].tolist(),
            start_mps=subset["Start_MP"].tolist(), end_mps=subset["End_MP"].tolist(),
            coord_tol_ft=join_tol_ft, return_indices=True, route_id=route_id,
        )
    else:
        # join_tol_ft in degrees, at the ~330,000 ft per degree that
        # stitch_linestrings_ordered's snap_tol assumes.
        snap_tol_deg = join_tol_ft / 330000.0
        lines, chunk_indices = stitch_linestrings_ordered(
            subset["WKT"].tolist(), snap_tol=snap_tol_deg, return_indices=True,
            start_mps=subset["Start_MP"].tolist(), end_mps=subset["End_MP"].tolist(),
        )
    for g, idxs in zip(lines, chunk_indices):
        contrib = subset.iloc[idxs]
        chunk_low_mp  = float(contrib[["Start_MP", "End_MP"]].min().min())
        chunk_high_mp = float(contrib[["Start_MP", "End_MP"]].max().max())
        fs_miles = _fsystem_miles(contrib)

        all_chunks.append({
            "geom": g, "f_sys": _predominant_fsystem(fs_miles, route_f_sys),
            "fs_miles": fs_miles, "state_id": state_val,
            "chunk_low_mp": chunk_low_mp, "chunk_high_mp": chunk_high_mp,
            "rec_idx": list(idxs),
        })

    if not all_chunks:
        return []

    # Join chunks left apart by a little more than the join tolerance,
    # such as loops around a traffic circle (see merge_close_chunks).
    all_chunks = merge_close_chunks(all_chunks, merge_tol_ft=500.0, route_id=route_id)

    # Reference points are interpolated along each chunk from its lowest to
    # its highest reference point, which holds only while the chunk's
    # records follow one another. Where they jump over reference points that
    # other records of the route carry, the chunk is cut there.
    cut_chunks = []
    for c in all_chunks:
        pieces = _split_at_rp_jumps(c, subset, route_f_sys)
        if len(pieces) > 1:
            logging.log(logging.INFO if log_detail else logging.DEBUG,
                f"Route {route_id}: piece MP {c['chunk_low_mp']:.3f}-{c['chunk_high_mp']:.3f} "
                f"cut into {len(pieces)} where its records jump over reference points of "
                f"other records: " + ", ".join(
                    f"MP {p['chunk_low_mp']:.3f}-{p['chunk_high_mp']:.3f}" for p in pieces))
        cut_chunks += pieces
    all_chunks = cut_chunks

    # Ascending milepost order, so Dist_Mi accumulates along the route.
    all_chunks.sort(key=lambda c: c["chunk_low_mp"])

    # Log the choice wherever the route is not a single functional system.
    mixed = {id(c) for c in all_chunks if sum(v > 0 for v in c["fs_miles"].values()) > 1}
    if len({c["f_sys"] for c in all_chunks}) > 1 or mixed:
        detail = log_detail or bool(params.get("LOG_FSYSTEM_DETAIL", False))
        logging.log(logging.INFO if detail else logging.DEBUG,
            f"Route {route_id}: functional system by piece (most miles): "
            + "; ".join(
                f"MP {c['chunk_low_mp']:.3f}-{c['chunk_high_mp']:.3f} FS {c['f_sys']}"
                + (" (" + ", ".join(f"FS {k} {v:.2f} mi" for k, v in sorted(c["fs_miles"].items())) + ")"
                   if id(c) in mixed else "")
                for c in all_chunks)
        )

    total_stitch_len_m = 0.0
    for info in all_chunks:
        lon, lat = info["geom"].coords[0]
        utm = get_appropriate_utm_zone(lon, lat)
        proj = Transformer.from_crs("EPSG:4326", f"EPSG:{utm}", always_xy=True)
        coords_m = [proj.transform(x, y) for x, y in info["geom"].coords]
        info["length_m"] = LineString(coords_m).length
        total_stitch_len_m += info["length_m"]

    if total_stitch_len_m == 0:
        total_stitch_len_m = 1.0
    route_s = float(subset["Start_MP"].min())

    # Cumulative distance along the route's chunks, starting at the route's
    # first milepost. Splitting a chunk for parallel work does not change it.
    global_continuous_dist_m = route_s * 1609.344
    for info in all_chunks:
        info["global_start_dist_m"] = global_continuous_dist_m
        global_continuous_dist_m += info["length_m"]

    return all_chunks


def split_chunks_for_parallelism(route_id: str, all_chunks: List[dict], params: dict) -> List[dict]:
    """
    Turn a route's true continuity chunks into the actual units of
    parallel work. A chunk longer than MAX_CHUNK_SPLIT_MILES is cut into
    sub-pieces so a single worker isn't handed an entire multi-hundred-
    mile Interstate.

    Each sub-piece reads CHUNK_SPLIT_PADDING_FT of extra geometry beyond
    its true boundary purely so smoothing/curvature math has enough
    context near the cut -- but is expected to only use/emit output inside
    its own true (unpadded) MP range ["emit_start_mp", "emit_end_mp"].
    Because emit ranges never overlap between sub-pieces, callers that
    respect this boundary need no deduplication step afterward.

    Returns a flat list of work-item dicts, one per parallel task:
        geom            -- geometry to process (padded, for split pieces;
                            the full chunk geometry, unpadded, otherwise)
        calib_start_mp,
        calib_end_mp    -- MP at the start/end of `geom`
        emit_start_mp,
        emit_end_mp     -- true MP range this item is responsible for
                            (equal to calib_start/end_mp for non-split
                            chunks)
        part_id         -- the PARENT chunk's chunk_low_mp -- shared by
                            every sub-piece of one chunk, so "Part" still
                            identifies the genuine physical piece rather
                            than the parallelism split
        f_sys, state_id, global_start_dist_m -- as on the parent chunk,
                            with global_start_dist_m offset for sub-pieces
        is_split        -- True if this item came from splitting a long
                            chunk
    """
    max_len_m = float(params.get("MAX_CHUNK_SPLIT_MILES", 25.0)) * 1609.344
    pad_m     = float(params.get("CHUNK_SPLIT_PADDING_FT", 20000.0)) / FEET_PER_METER

    work_items = []
    for info in all_chunks:
        total_len_m = info["length_m"]
        part_id     = info["chunk_low_mp"]

        if total_len_m <= max_len_m:
            work_items.append({
                "geom": info["geom"],
                "calib_start_mp": info["chunk_low_mp"],
                "calib_end_mp": info["chunk_high_mp"],
                "emit_start_mp": info["chunk_low_mp"],
                "emit_end_mp": info["chunk_high_mp"],
                "part_id": part_id,
                "f_sys": info["f_sys"],
                "state_id": info["state_id"],
                "global_start_dist_m": info["global_start_dist_m"],
                "is_split": False,
            })
            continue

        # Project once to a local UTM CRS so substring() can cut by real
        # meter distances along the chunk (matches how length_m itself was
        # computed above).
        lon0, lat0 = info["geom"].coords[0]
        utm = get_appropriate_utm_zone(lon0, lat0)
        to_utm = Transformer.from_crs("EPSG:4326", f"EPSG:{utm}", always_xy=True)
        to_wgs = Transformer.from_crs(f"EPSG:{utm}", "EPSG:4326", always_xy=True)
        geom_utm = LineString([to_utm.transform(x, y) for x, y in info["geom"].coords])

        mp_span = info["chunk_high_mp"] - info["chunk_low_mp"]
        mp_per_m = mp_span / total_len_m if total_len_m > 0 else 0.0

        n_pieces = int(math.ceil(total_len_m / max_len_m))
        piece_len_m = total_len_m / n_pieces

        for i in range(n_pieces):
            emit_start_m = i * piece_len_m
            emit_end_m   = total_len_m if i == n_pieces - 1 else (i + 1) * piece_len_m
            padded_start_m = max(0.0, emit_start_m - pad_m)
            padded_end_m   = min(total_len_m, emit_end_m + pad_m)

            geom_piece_utm = substring(geom_utm, padded_start_m, padded_end_m)
            geom_piece = LineString([to_wgs.transform(x, y) for x, y in geom_piece_utm.coords])

            work_items.append({
                "geom": geom_piece,
                "calib_start_mp": info["chunk_low_mp"] + padded_start_m * mp_per_m,
                "calib_end_mp": info["chunk_low_mp"] + padded_end_m * mp_per_m,
                "emit_start_mp": info["chunk_low_mp"] + emit_start_m * mp_per_m,
                "emit_end_mp": info["chunk_low_mp"] + emit_end_m * mp_per_m,
                "part_id": part_id,
                "f_sys": info["f_sys"],
                "state_id": info["state_id"],
                "global_start_dist_m": info["global_start_dist_m"] + padded_start_m,
                "is_split": True,
            })

        logging.info(
            f"Route {route_id}: chunk Part {part_id:.4f} "
            f"({total_len_m / 1609.344:.1f} mi) split into {n_pieces} "
            f"sub-piece(s) for parallel processing (threshold "
            f"{params.get('MAX_CHUNK_SPLIT_MILES', 25.0)} mi)."
        )

    return work_items


# -------------------------
# Signal/math helpers
# -------------------------
def calculate_iri_proxy(z_raw: np.ndarray, z_smooth: np.ndarray, spacing_m: float) -> np.ndarray:
    """
    Pavement roughness proxy (Micro_Jitter_Inches): the difference between
    the repaired DEM profile and the smoothed profile, high-pass filtered
    (30 m cutoff) to remove the longer undulations, then a moving RMS over
    30 m. The result is a height in inches, not a slope statistic, so it
    cannot be compared with IRI (in/mi) directly.
    """
    # 1. Isolate the residual (error)
    residuals = z_raw - z_smooth

    # 2. Handle NaNs in residuals
    res_series = pd.Series(residuals).interpolate(limit_direction='both').fillna(0).to_numpy()
    n = len(res_series)

    if n < 15:
        return np.zeros(n)

    # 3. High-pass filter to remove DEM macro-drift (cutoff at ~30 meters / 100 ft)
    fs = 1.0 / spacing_m
    nyquist = 0.5 * fs
    cutoff = 1.0 / 30.0  # 30-meter wavelength cutoff

    try:
        if cutoff >= nyquist:
            # Fallback: rolling standard deviation of residuals over a ~30m window
            window_pts = max(3, int(30.0 / spacing_m))
            jitter = pd.Series(res_series).rolling(window=window_pts, center=True, min_periods=1).std().fillna(0).to_numpy()
        else:
            # Apply Butterworth filter
            b, a = butter(2, cutoff / nyquist, btype='highpass')
            high_freq_noise = filtfilt(b, a, res_series)
            # Roughness proxy is the moving RMS of the high-frequency noise
            window_pts = max(3, int(30.0 / spacing_m))
            jitter = pd.Series(high_freq_noise**2).rolling(window=window_pts, center=True, min_periods=1).mean().apply(np.sqrt).fillna(0).to_numpy()
    except Exception as e:
        # Failsafe if signal math crashes on weird edge-case geometry
        logging.debug(f"IRI proxy Butterworth filter failed (fallback to zeros): {e}")
        jitter = np.zeros(n)

    # Meters to inches.
    return jitter * FEET_PER_METER * 12.0

def calculate_available_sight_distance(z_smooth: np.ndarray, spacing_m: float) -> np.ndarray:
    """
    Available sight distance (ASD) at each point: how far ahead a 2.0 ft
    object stays continuously visible from a 3.5 ft eye height (AASHTO), up
    to 1,500 ft. The distance ends at the last object position before the
    first one hidden by the road ahead, so an object that drops out of view
    beyond a crest (a hidden dip) and reappears farther on is not counted.
    Near the end of the profile, where the view is clear to the last point
    but shorter than 1,500 ft, the sight distance is unknown (NaN).

    Each point scans forward using the running maximum slope to the road
    surface, which keeps the cost low at statewide vertex counts.
    """
    n = len(z_smooth)
    asd_ft = np.zeros(n)

    h_eye_m = 3.5 / FEET_PER_METER
    h_obj_m = 2.0 / FEET_PER_METER
    max_search_m = 1500.0 / FEET_PER_METER
    search_pts = int(max_search_m / spacing_m)

    for i in range(n):
        eye_z = z_smooth[i] + h_eye_m
        end_search = min(n, i + search_pts + 1)

        if end_search <= i + 1:
            asd_ft[i] = np.nan      # last point: nothing ahead to see
            continue

        # Distances to the points ahead in the search window
        ahead = np.arange(1, end_search - i)
        dists = ahead * spacing_m

        # Slope from the eye to an object at each point ahead, and the
        # steepest slope from the eye to the road surface up to each point.
        # An object is visible when its sight line clears every road point
        # before it.
        slopes_to_target = (z_smooth[i + 1:end_search] + h_obj_m - eye_z) / dists
        terrain_slopes = (z_smooth[i + 1:end_search] - eye_z) / dists
        cum_max_obstruction = np.maximum.accumulate(terrain_slopes)
        clear = slopes_to_target >= cum_max_obstruction

        blocked = np.flatnonzero(~clear)
        if blocked.size:
            first_blocked = int(blocked[0])
            asd_ft[i] = dists[first_blocked - 1] * FEET_PER_METER if first_blocked > 0 else 0.0
        elif end_search == n and end_search - i <= search_pts:
            # Clear to the end of the data short of the full search
            # distance: the sight distance is not known, not short.
            asd_ft[i] = np.nan
        else:
            asd_ft[i] = dists[-1] * FEET_PER_METER

    return asd_ft

def calculate_headings(coords_m: List[Tuple[float, float]]) -> np.ndarray:
    pts = np.array(coords_m)
    diffs = pts[1:] - pts[:-1]
    headings = np.arctan2(diffs[:, 1], diffs[:, 0])
    headings_deg = np.degrees(headings)
    headings_deg = np.append(headings_deg, headings_deg[-1])
    return np.unwrap(headings_deg, period=360)

def get_tangent_grade(z_vals: np.ndarray, idx: int, spacing_m: float, window_m: float) -> float:
    """Grade (percent) at idx: slope of a least-squares line through the
    points within window_m on each side (cut short at the ends)."""
    half_w = int(window_m / spacing_m)
    start = max(0, idx - half_w)
    end = min(len(z_vals), idx + half_w + 1)
    if end - start < 3:
        return 0.0
    xs = np.arange(start, end) * spacing_m
    ys = z_vals[start:end]
    res = linregress(xs, ys)
    return res.slope * 100.0

# -------------------------
# Classification helpers
# -------------------------
def classify_bin(deg_per_100ft: float) -> str:
    if deg_per_100ft < 3.5: return 'A'
    if deg_per_100ft < 5.5: return 'B'
    if deg_per_100ft < 8.5: return 'C'
    if deg_per_100ft < 14.0: return 'D'
    if deg_per_100ft < 28.0: return 'E'
    return 'F'

def classify_grade_bin(pct: float) -> str:
    val = abs(pct)
    if val < 0.5: return 'A'
    if val < 2.5: return 'B'
    if val < 4.5: return 'C'
    if val < 6.5: return 'D'
    if val < 8.5: return 'E'
    return 'F'


def sight_distance_provided(length_ft: float, alg_diff_pct: float, curve_type: str) -> float:
    """
    Stopping sight distance (ft) a vertical curve provides (AASHTO). Crest:
    3.5 ft eye height, 2.0 ft object height. Sag: 2.0 ft headlight height,
    1 degree upward beam. Each is solved first for S < L and, if that S
    exceeds L, for S > L. np.inf where the curve does not limit sight
    distance (a sag with A <= 1.75%).
    """
    L = float(length_ft)
    A = abs(float(alg_diff_pct))
    if A <= 0.0 or L <= 0.0:
        return np.inf
    if curve_type == "CREST":
        S = math.sqrt(2158.0 * L / A)
        return S if S <= L else (L + 2158.0 / A) / 2.0
    S = (3.5 * L + math.sqrt((3.5 * L) ** 2 + 1600.0 * A * L)) / (2.0 * A)
    if S <= L:
        return S
    if 2.0 * A <= 3.5:
        return np.inf
    return (A * L + 400.0) / (2.0 * A - 3.5)


def design_speed_supported(ssd_ft: float) -> int:
    """Highest design speed (mph) in SSD_TABLE_FT whose stopping sight
    distance is no more than ssd_ft; 0 below the 15 mph value."""
    supported = 0
    for mph, ssd in sorted(SSD_TABLE_FT.items()):
        if ssd <= ssd_ft:
            supported = mph
    return supported


def vertical_curve_class(speed_mph: float) -> str:
    """Vertical curve class A-F from the design speed supported."""
    for cls in "ABCDE":
        if speed_mph >= VC_CLASS_MIN_SPEED_MPH[cls]:
            return cls
    return "F"


def build_grade_segments(z_smooth: np.ndarray, spacing_m: float, curves: List[Dict]) -> List[Dict]:
    """
    Grade segments of one smoothed profile: the pieces between the chunk
    start, each vertical curve's VPI, and the chunk end, with the grade of
    the straight line joining their elevations and its Item 45 class.
    curves are those returned by analyze_vertical_parabolic() for the same
    profile (chunk-local VPI_Dist in m, VPI_Elev_Ft). The segments cover
    the profile from 0 to (n - 1) * spacing_m with no gaps or overlaps.
    """
    n = len(z_smooth)
    if n < 2:
        return []
    x_end = (n - 1) * spacing_m
    xs, zs = [0.0], [float(z_smooth[0])]
    for c in sorted(curves, key=lambda c: c["VPI_Dist"]):
        x = float(c["VPI_Dist"])
        if xs[-1] < x < x_end:
            xs.append(x)
            zs.append(float(c["VPI_Elev_Ft"]) / FEET_PER_METER)
    xs.append(x_end)
    zs.append(float(z_smooth[-1]))

    segments = []
    for j in range(len(xs) - 1):
        length = xs[j + 1] - xs[j]
        grade = 100.0 * (zs[j + 1] - zs[j]) / length
        segments.append({
            "Start_Dist": xs[j],
            "End_Dist": xs[j + 1],
            "Length_m": length,
            "Length_ft": length * FEET_PER_METER,
            "Grade_Pct": grade,
            "Grade_Bin": classify_grade_bin(grade),
        })
    return segments


def label_vertices_by_segments(n_pts: int, spacing_m: float, segments: List[Dict]) -> Tuple[np.ndarray, np.ndarray]:
    """Grade (%) and grade class of the segment containing each of n_pts
    profile points (chunk-local distances, as from build_grade_segments)."""
    if not segments:
        return np.full(n_pts, np.nan), np.full(n_pts, "A", dtype=object)
    starts = np.array([s["Start_Dist"] for s in segments])
    idx = np.clip(np.searchsorted(starts, np.arange(n_pts) * spacing_m, side="right") - 1, 0, len(segments) - 1)
    grades = np.array([s["Grade_Pct"] for s in segments])[idx]
    bins = np.array([s["Grade_Bin"] for s in segments], dtype=object)[idx]
    return grades, bins


def _speed_limit_column(df: pd.DataFrame) -> Optional[str]:
    """Name of the HPMS speed limit column (Socrata speed_limit, or a local
    export's SpeedLimit / SPEED_LIMIT / SpeedLimit_VN), if present."""
    for c in df.columns:
        if str(c).lower().replace("_", "") in ("speedlimit", "speedlimitvn"):
            return c
    return None


def add_speed_reference(curves: List[Dict], hpms_sub: Optional[pd.DataFrame], f_sys: int) -> None:
    """
    Adds Speed_Ref_Mph, Speed_Ref_Source and Meets_Speed_Ref to each
    vertical curve (Calibrated_Start_MP / Calibrated_End_MP and
    Design_Speed_Supported_Mph already set). The reference is the HPMS
    speed limit of the record containing the curve's midpoint, when
    reported (not 999); otherwise DEFAULT_SPEED_BY_FS_MPH for the record's
    functional system (f_sys where no record matches).
    """
    starts = ends = speeds = fsys = None
    if hpms_sub is not None and not hpms_sub.empty:
        starts = pd.to_numeric(hpms_sub["Start_MP"], errors="coerce").to_numpy()
        ends = pd.to_numeric(hpms_sub["End_MP"], errors="coerce").to_numpy()
        col = _speed_limit_column(hpms_sub)
        speeds = (pd.to_numeric(hpms_sub[col], errors="coerce").to_numpy()
                  if col else np.full(len(hpms_sub), np.nan))
        fsys = (pd.to_numeric(hpms_sub["FSystem"], errors="coerce").to_numpy()
                if "FSystem" in hpms_sub.columns else np.full(len(hpms_sub), np.nan))

    for c in curves:
        ref, source, fs = None, "Default by FS", f_sys
        if starts is not None:
            mid = (c["Calibrated_Start_MP"] + c["Calibrated_End_MP"]) / 2.0
            hit = np.flatnonzero((starts <= mid) & (ends >= mid))
            valid = [i for i in hit if np.isfinite(speeds[i]) and 0 < speeds[i] < 999]
            if valid:
                ref, source = float(speeds[valid[0]]), "HPMS SPEED_LIMIT"
            elif len(hit) and np.isfinite(fsys[hit[0]]):
                fs = int(fsys[hit[0]])
        if ref is None:
            ref = DEFAULT_SPEED_BY_FS_MPH.get(int(fs), np.nan) if fs is not None else np.nan
        c["Speed_Ref_Mph"] = ref
        c["Speed_Ref_Source"] = source
        c["Meets_Speed_Ref"] = bool(np.isfinite(ref) and c["Design_Speed_Supported_Mph"] >= ref)

# -------------------------
# Shared 3-state Viterbi decoder for curve/tangent classification
# -------------------------
def _viterbi_3state(signal_signed: np.ndarray, thresh: float, stickiness: float) -> np.ndarray:
    """
    Labels each vertex with the most probable state sequence along the
    whole route (Viterbi decoding of a hidden Markov model, the principle
    OSRM map matching uses), rather than testing each vertex against a
    threshold on its own (abs(signal) > thresh, the legacy method).

    States (returned as ints):
      0 = NEUTRAL   (tangent -- no curve)
      1 = NEGATIVE  (right turn for horizontal / crest for vertical)
      2 = POSITIVE  (left turn for horizontal / sag for vertical)

    signal_signed: the signed per-vertex signal driving classification
        (kappa_signed for horizontal curvature, gchg for vertical grade
        change). Sign carries the direction; magnitude carries strength.
    thresh: the magnitude at which the threshold method changes state
        (kappa_thresh / V_VC_THRESHOLD). Emission cost is 0 at this
        boundary, so with stickiness=0 the result equals the threshold
        method point for point.
    stickiness: flat cost of switching state between adjacent vertices.
        Higher values need more sustained evidence to change state. It
        takes the place of V_GAP_TOLERANCE's gap bridging and reduces the
        on/off flicker the threshold method leaves at its boundary.

    Returns an int array the same length as signal_signed. O(n) --
    3 states means each step only compares 3x3 transitions.
    """
    n = len(signal_signed)
    if n == 0:
        return np.zeros(0, dtype=int)
    thresh = max(float(thresh), 1e-12)

    s = np.asarray(signal_signed, dtype=float)
    # Emission cost per state, lower = better fit. Each cost is 0 wherever
    # the threshold method would pick that state and rises linearly outside
    # it, so at stickiness=0 the argmin at every point matches the
    # threshold method:
    # cost_neutral is 0 for |s|<=thresh; cost_positive is 0 for s>=thresh;
    # cost_negative is 0 for s<=-thresh.
    e0 = (np.maximum(0.0, np.abs(s) - thresh) / thresh).tolist()
    e1 = (np.maximum(0.0, s + thresh) / thresh).tolist()
    e2 = (np.maximum(0.0, thresh - s) / thresh).tolist()

    stick = float(stickiness)
    v0, v1, v2 = e0[0], e1[0], e2[0]
    bp0 = [0] * n
    bp1 = [0] * n
    bp2 = [0] * n

    for t in range(1, n):
        c00, c10, c20 = v0, v1 + stick, v2 + stick
        c01, c11, c21 = v0 + stick, v1, v2 + stick
        c02, c12, c22 = v0 + stick, v1 + stick, v2

        # Tie-break order 2, 1, 0 (neutral loses ties), matching the
        # inclusive `kappa >= kappa_thresh` test of the threshold method.
        if c20 <= c10 and c20 <= c00:
            n0, p0 = c20, 2
        elif c10 <= c00:
            n0, p0 = c10, 1
        else:
            n0, p0 = c00, 0

        if c21 <= c11 and c21 <= c01:
            n1, p1 = c21, 2
        elif c11 <= c01:
            n1, p1 = c11, 1
        else:
            n1, p1 = c01, 0

        if c22 <= c12 and c22 <= c02:
            n2, p2 = c22, 2
        elif c12 <= c02:
            n2, p2 = c12, 1
        else:
            n2, p2 = c02, 0

        v0, v1, v2 = n0 + e0[t], n1 + e1[t], n2 + e2[t]
        bp0[t], bp1[t], bp2[t] = p0, p1, p2

    path = [0] * n
    if v2 <= v1 and v2 <= v0:
        path[-1] = 2
    elif v1 <= v0:
        path[-1] = 1
    else:
        path[-1] = 0

    bp = (bp0, bp1, bp2)
    for t in range(n - 1, 0, -1):
        path[t - 1] = bp[path[t]][t]

    return np.array(path, dtype=int)


# -------------------------
# Horizontal curve analysis (curvature-based)
# -------------------------
def analyze_horizontal_curvature(
        coords_m_smooth: List[Tuple[float, float]],
        spacing_m: float,
        params: Dict
    ) -> List[Dict]:
    xs = np.array([c[0] for c in coords_m_smooth], dtype=float)
    ys = np.array([c[1] for c in coords_m_smooth], dtype=float)
    n = len(xs)
    if n < 5:
        return []

    dx = np.gradient(xs, spacing_m)
    dy = np.gradient(ys, spacing_m)
    ddx = np.gradient(dx, spacing_m)
    ddy = np.gradient(dy, spacing_m)
    num = np.abs(dx * ddy - dy * ddx)
    den = (dx**2 + dy**2)**1.5

    kappa = np.zeros_like(num)
    valid = den > 1e-12
    kappa[valid] = num[valid] / den[valid]

    signed_num = (dx * ddy - dy * ddx)
    direction_sign = np.sign(signed_num)

    kappa_thresh = 1.0 / max(params['H_MAX_RADIUS'], 1e-6)
    trim_thresh = kappa_thresh * 1.05

    min_len = params['H_MIN_CURVE_LENGTH_M']
    min_delta = params['H_MIN_DELTA']

    headings_array = calculate_headings(list(zip(xs, ys)))

    # ------------------------------------------------------------------
    # STEP 1: candidate curve runs as (start_idx, end_idx, sign), found by
    # either method (H_USE_HMM_CLASSIFICATION). Both feed the same STEP 2.
    # ------------------------------------------------------------------
    runs = []  # list of (s, e, sgn)

    if params.get('H_USE_HMM_CLASSIFICATION', False):
        # HMM/Viterbi method (see _viterbi_3state). kappa above is unsigned,
        # so the signed curvature is rebuilt to tell left from right.
        den_safe = np.where(den > 1e-12, den, np.inf)
        kappa_signed = signed_num / den_safe

        stickiness = params.get('H_HMM_STICKINESS', 1.0)
        states = _viterbi_3state(kappa_signed, kappa_thresh, stickiness)

        i = 0
        while i < n:
            if states[i] == 0:
                i += 1
                continue
            s = i
            cur_state = states[i]
            while i < n and states[i] == cur_state:
                i += 1
            e = i - 1
            sgn = 1 if cur_state == 2 else -1
            runs.append((s, e, sgn))
    else:
        # Threshold method: each vertex tested on its own. Kept as a
        # baseline for the HMM method.
        is_curve = kappa >= kappa_thresh
        i = 0
        while i < n:
            if not is_curve[i]:
                i += 1
                continue
            s = i
            sgn = direction_sign[i] if direction_sign[i] != 0 else 1
            while i < n and is_curve[i] and (direction_sign[i] == 0 or np.sign(direction_sign[i]) == np.sign(sgn)):
                i += 1
            e = i - 1
            runs.append((s, e, sgn))

    # ------------------------------------------------------------------
    # STEP 2: measure each candidate run, whichever method found it.
    # ------------------------------------------------------------------
    curves = []
    for s, e, sgn in runs:
        if params.get('TRIM_CURVE_ENDPOINTS', True):
            while s < e and kappa[s] < trim_thresh:
                s += 1
            while e > s and kappa[e] < trim_thresh:
                e -= 1

        if e <= s:
            continue

        length = (e - s) * spacing_m
        if length < min_len:
            continue

        delta = abs(headings_array[e] - headings_array[s])
        if delta < min_delta:
            continue

        seg_kappa = kappa[s:e+1]
        max_k = np.nanmax(seg_kappa) if len(seg_kappa) > 0 else 0.0
        min_radius = (1.0 / max_k) if max_k > 1e-12 else 99999.0
        radius = min_radius
        if radius > params['H_MAX_RADIUS']:
            continue

        deg_per_100 = METRIC_R_TO_IMPERIAL_D / radius if radius > 0 else 0
        cbin = classify_bin(deg_per_100)
        direction = "Left" if sgn > 0 else "Right"

        # Spiral detection: find the central circular curve (SC to CS)
        peak_idx = s + int(np.argmax(seg_kappa))
        max_k = seg_kappa[peak_idx - s]

        # A central circular curve maintains a near-constant maximum curvature.
        # The central curve is where curvature is at least 85% of the peak.
        core_threshold = max_k * 0.85
        core_indices = np.where(seg_kappa >= core_threshold)[0]

        if len(core_indices) > 0:
            sc_idx_local = core_indices[0]
            cs_idx_local = core_indices[-1]
        else:
            sc_idx_local = peak_idx - s
            cs_idx_local = peak_idx - s

        dist_to_sc_ft = sc_idx_local * spacing_m * FEET_PER_METER
        dist_from_cs_ft = (len(seg_kappa) - 1 - cs_idx_local) * spacing_m * FEET_PER_METER

        is_spiral = "Spiral" if (dist_to_sc_ft > 150.0 or dist_from_cs_ft > 150.0) else "Simple"

        r_ft = radius * FEET_PER_METER

        curves.append({
            'Start_Dist': s * spacing_m,
            'End_Dist': e * spacing_m,
            'SC_Dist': (s + sc_idx_local) * spacing_m,
            'CS_Dist': (s + cs_idx_local) * spacing_m,
            'Length_m': length,
            'Length_ft': length * FEET_PER_METER,
            'Radius_m': radius,
            'Radius_ft': r_ft,
            'Min_Radius_m': min_radius,
            'Delta': float(delta),
            'Dir': direction,
            'Bin': cbin,
            'Merge_Status': 'Simple',
            'Transition_Type': is_spiral
        })
    return curves

def merge_horizontal_curves(curves: List[Dict], params: Dict) -> List[Dict]:
    if not curves:
        return []
    curves = sorted(curves, key=lambda c: c['Start_Dist'])
    merged = [curves[0].copy()]
    for nxt in curves[1:]:
        cur = merged[-1]
        gap_ft = (nxt['Start_Dist'] - cur['End_Dist']) * FEET_PER_METER

        # The merge gap scales with the shorter of the two curves, so a
        # long curve alone cannot justify merging across a large gap into a
        # small neighbor. MERGE_GAP_FLOOR_FT sets a minimum for very short
        # curves.
        ref_length_ft = min(cur['Length_m'], nxt['Length_m']) * FEET_PER_METER
        gap_threshold_ft = max(params['MERGE_GAP_FLOOR_FT'], params['MERGE_GAP_PCT'] * ref_length_ft)

        if nxt['Dir'] == cur['Dir'] and gap_ft < gap_threshold_ft:
            # Radius weighted by each curve's own length (taken before the
            # merged length replaces cur's).
            l1 = max(cur['Length_m'], 1e-6)
            l2 = max(nxt['Length_m'], 1e-6)
            cur['Radius_m'] = (cur['Radius_m'] * l1 + nxt['Radius_m'] * l2) / (l1 + l2)
            cur['Radius_ft'] = cur['Radius_m'] * FEET_PER_METER
            cur['End_Dist'] = nxt['End_Dist']
            cur['Length_m'] = cur['End_Dist'] - cur['Start_Dist']
            cur['Length_ft'] = cur['Length_m'] * FEET_PER_METER
            cur['Min_Radius_m'] = min(cur.get('Min_Radius_m', 99999), nxt.get('Min_Radius_m', 99999))
            cur['Delta'] += nxt['Delta']
            cur['Merge_Status'] = 'Compound'

            # Update the end of the central circular curve to the end of the merged compound curve
            if 'CS_Dist' in nxt:
                cur['CS_Dist'] = nxt['CS_Dist']

            # Preserve spiral status if either constituent curve was a spiral
            if cur.get('Transition_Type') == 'Spiral' or nxt.get('Transition_Type') == 'Spiral':
                cur['Transition_Type'] = 'Spiral'
        else:
            merged.append(nxt.copy())
    for c in merged:
        r = c.get('Min_Radius_m', c['Radius_m'])
        c['Bin'] = classify_bin(METRIC_R_TO_IMPERIAL_D / r) if (r > 0 and r < 99999) else 'A'
    return merged

# -------------------------
# Vertical curve analysis (parabolic fit)
# -------------------------
def analyze_vertical_parabolic(
        z_smooth: np.ndarray,
        spacing_m: float,
        params: Dict
    ) -> List[Dict]:
    grads = np.gradient(z_smooth, spacing_m) * 100.0
    gchg = np.gradient(grads, spacing_m)

    trim_thresh = params['V_VC_THRESHOLD'] * 1.10

    min_len = params['V_MIN_CURVE_LENGTH']
    min_g_change = params['V_MIN_GRADE_CHANGE']

    n = len(z_smooth)

    # ------------------------------------------------------------------
    # STEP 1: candidate curve runs as (start_idx, end_idx), found by either
    # method (V_USE_HMM_CLASSIFICATION). Both feed the same STEP 2.
    # ------------------------------------------------------------------
    runs = []  # list of (s, e)

    if params.get('V_USE_HMM_CLASSIFICATION', False):
        # HMM/Viterbi method (see _viterbi_3state); its stickiness does the
        # job of V_GAP_TOLERANCE's gap bridging.
        stickiness = params.get('V_HMM_STICKINESS', 1.0)
        states = _viterbi_3state(gchg, params['V_VC_THRESHOLD'], stickiness)

        i = 0
        while i < n:
            if states[i] == 0:
                i += 1
                continue
            s = i
            cur_state = states[i]
            while i < n and states[i] == cur_state:
                i += 1
            e = i - 1
            runs.append((s, e))
    else:
        # Threshold method: each vertex tested on its own, with gaps of up
        # to V_GAP_TOLERANCE points bridged. Kept as a baseline.
        is_vc = np.abs(gchg) > params['V_VC_THRESHOLD']
        i = 0
        while i < n:
            if not is_vc[i]:
                i += 1
                continue

            s = i
            sign0 = np.sign(gchg[s]) if gchg[s] != 0 else 1
            gap = 0
            j = i + 1
            while j < n:
                if is_vc[j]:
                    sgn = np.sign(gchg[j])
                    if sgn != 0 and sign0 != 0 and sgn != sign0:
                        break
                    gap = 0
                else:
                    gap += 1
                    if gap > params['V_GAP_TOLERANCE']:
                        j -= gap
                        break
                j += 1
            e = min(j, n - 1)
            i = max(e + 1, i + 1)
            runs.append((s, e))

    # ------------------------------------------------------------------
    # STEP 2: fit each candidate run, whichever method found it.
    # ------------------------------------------------------------------
    curves = []
    for s, e in runs:
        if params.get('TRIM_CURVE_ENDPOINTS', True):
            while s < e and abs(gchg[s]) < trim_thresh:
                s += 1
            while e > s and abs(gchg[e]) < trim_thresh:
                e -= 1

        if e <= s:
            continue

        length = (e - s) * spacing_m
        if length < min_len:
            continue

        x = np.arange(s, e + 1) * spacing_m
        z = z_smooth[s:e + 1]
        if len(x) < 5:
            continue

        try:
            a, b, c = np.polyfit(x, z, 2)
        except Exception:
            continue

        g1 = (2 * a * x[0] + b) * 100.0
        g2 = (2 * a * x[-1] + b) * 100.0
        A = g2 - g1
        if abs(A) < min_g_change:
            continue

        length_ft = length * FEET_PER_METER
        K = length_ft / abs(A)
        E_m = abs(A * length / 800.0)
        if E_m * FEET_PER_METER < params['V_MIN_OFFSET_FT']:
            continue

        mid_idx = len(x) // 2
        z_mid = z[mid_idx]
        z_chord = z[0] + ((x[mid_idx] - x[0]) / max((x[-1] - x[0]), 1e-9)) * (z[-1] - z[0])
        vtype = "CREST" if z_mid > z_chord else "SAG"

        # VPI of the fitted (symmetric) parabola, from its value at the
        # curve start and the incoming grade.
        z_start = a * x[0] ** 2 + b * x[0] + c
        vpi_dist = (x[0] + x[-1]) / 2.0
        vpi_elev = z_start + (g1 / 100.0) * length / 2.0

        ssd = sight_distance_provided(length_ft, A, vtype)
        speed = design_speed_supported(ssd)

        curves.append({
            'Start_Dist': s * spacing_m,
            'End_Dist': e * spacing_m,
            'Length_m': length,
            'Length_ft': length_ft,
            'Grade_In': g1,
            'Grade_Out': g2,
            'Alg_Diff': A,
            'K_Value': K,
            'Type': vtype,
            'E': E_m,
            'VPI_Dist': vpi_dist,
            'VPI_Elev_Ft': vpi_elev * FEET_PER_METER,
            # Blank where the curve does not limit sight distance.
            'SSD_Provided_Ft': round(ssd) if np.isfinite(ssd) else np.nan,
            'Design_Speed_Supported_Mph': speed,
            'VC_Class': vertical_curve_class(speed),
        })
    return curves

# -------------------------
# Shared route processing
# -------------------------
def profile_result_ok(res) -> bool:
    """
    True if a dict returned by smooth_plan_profile_from_linestring()
    represents a successful chunk (safe to index into for z_raw, z_smooth,
    spacing_m, etc.), False if it's a None result or one of that
    function's _fail() failure payloads.

    Use this rather than `if res:`: a failure payload is a non-empty dict
    (it carries the route, chunk and reason for logging), so it is truthy.
    """
    return res is not None and bool(res.get("ok", True))


def smooth_plan_profile_from_linestring(
        line: LineString,
        dem_dir: str,
        params: dict,
        f_sys: int = 1,
        route_id: str = None,
        chunk_s_mp: float = 0.0,
        chunk_e_mp: float = 0.0,
        hpms_subset: pd.DataFrame = None,
        nbi_nti_gdf: gpd.GeoDataFrame = None
        ) -> dict:

    line_wgs = line
    lon, lat = line_wgs.coords[0]
    utm = get_appropriate_utm_zone(lon, lat)
    fwd = Transformer.from_crs("EPSG:4326", f"EPSG:{utm}", always_xy=True)
    rev = Transformer.from_crs(f"EPSG:{utm}", "EPSG:4326", always_xy=True)

    def _fail(reason: str) -> dict:
        """Failure payload naming the route, chunk and reason (res['error']),
        so failures in parallel workers can be traced. Test results with
        profile_result_ok()."""
        logging.debug(
            f"smooth_plan_profile_from_linestring skipping "
            f"route={route_id!r} chunk=[{chunk_s_mp:.4f},{chunk_e_mp:.4f}]: {reason}"
        )
        return {
            "ok": False,
            "route_id": route_id,
            "chunk_s_mp": chunk_s_mp,
            "chunk_e_mp": chunk_e_mp,
            "error": reason,
        }

    coords_m = [fwd.transform(x, y) for x, y in line_wgs.coords]
    coords_m = densify_coords_line(coords_m, params['DENSIFY_SPACING_M'])
    if len(coords_m) < params['H_MIN_CHUNK_POINTS']:
        return _fail(f"too few densified points ({len(coords_m)}; "
                     f"H_MIN_CHUNK_POINTS is {params['H_MIN_CHUNK_POINTS']})")

    x_raw = np.array([c[0] for c in coords_m])
    y_raw = np.array([c[1] for c in coords_m])

    s_factor_h = params.get("H_SMOOTH_FACTOR", NATIONAL_DEFAULTS["H"][1])
    if f_sys in range(2, 8):
        s_factor_h = params.get(f"H_SMOOTH_FACTOR_FS{f_sys}", s_factor_h)
    s_factor_v = params.get("V_SMOOTH_FACTOR", NATIONAL_V_SMOOTH_FACTOR)

    spacing_m = LineString(coords_m).length / (len(coords_m) - 1)
    d_axis = np.arange(len(coords_m)) * spacing_m

    s_h = spline_s(s_factor_h, len(d_axis))
    sx = UnivariateSpline(d_axis, x_raw, s=s_h)
    sy = UnivariateSpline(d_axis, y_raw, s=s_h)
    coords_m_smooth = list(zip(sx(d_axis), sy(d_axis)))
    coords_wgs_smooth = [rev.transform(x, y) for x, y in coords_m_smooth]
    coords_wgs_raw = [rev.transform(x, y) for x, y in coords_m]  # raw, pre-spline

    import rasterio

    # Group coordinates by 1 m tile, open each tile once, and sample all
    # of its coordinates in one call.
    tile_to_coords = {}
    for i, (lon, lat) in enumerate(coords_wgs_raw):
        tile_name = get_hires_tile_name(lon, lat)
        if tile_name not in tile_to_coords:
            tile_to_coords[tile_name] = []
        tile_to_coords[tile_name].append((i, (lon, lat)))

    z_raw_list = [np.nan] * len(coords_wgs_raw)

    for tile_name, indexed_pts in tile_to_coords.items():
        tile_path = os.path.join(dem_dir, f"tile_1m_{tile_name}.tif")
        lock_path = tile_path + ".lock"

        # If the tile is being written, wait: on a network drive (SMB) a
        # reader can see a file part-way through the rename.
        if os.path.exists(lock_path):
            for _ in range(30):  # up to ~30s, well under the downloader's own stale-lock threshold
                time.sleep(1)
                if not os.path.exists(lock_path):
                    break

        if os.path.exists(tile_path):
            try:
                with rasterio.open(tile_path) as src:
                    coords_to_sample = [pt[1] for pt in indexed_pts]
                    samples = src.sample(coords_to_sample)
                    for (original_idx, _), val_array in zip(indexed_pts, samples):
                        val = val_array[0]
                        z_raw_list[original_idx] = float(val) if val > -1000 else np.nan
            except Exception as e:
                logging.warning(f"Failed to process 1m tile {tile_name} in bulk: {e}")
                # A tile that cannot be read is corrupt and would fail
                # the same way in every later run. Delete it so it is
                # downloaded again when next needed.
                try:
                    os.remove(tile_path)
                    logging.warning(f"  Removed corrupted tile {tile_name} -- will be re-downloaded on next need.")
                except FileNotFoundError:
                    pass
                except Exception as del_err:
                    logging.debug(f"Could not remove corrupted tile {tile_name}: {del_err}")
        else:
            logging.debug(f"Tile {tile_name} not found on disk.")

    z_raw = np.array(z_raw_list, dtype=float)

    # Share of points with no DEM value (missing tiles, nodata). Those points
    # are filled by straight-line interpolation below; callers that need
    # genuine elevation (e.g. calibration) can reject chunks above a limit.
    elev_missing_frac = float(np.mean(np.isnan(z_raw))) if len(z_raw) else 1.0
    z_filled = pd.Series(z_raw).interpolate(limit_direction='both').to_numpy()

    # SciPy's FITPACK crashes the process if it is given NaNs, so a chunk
    # with elevations still missing after interpolation is skipped.
    if np.isnan(z_filled).any():
        remaining_nans = np.sum(np.isnan(z_filled))
        total_vertices = len(z_filled)
        pct_nans = 100.0 * remaining_nans / total_vertices if total_vertices > 0 else 0.0
        warning_msg = (
            f"NaNs remained in elevation array after interpolation ({remaining_nans}/{total_vertices} vertices, "
            f"{pct_nans:.1f}%). Skipping chunk to prevent FITPACK segfault. "
            + (f"Check DEM coverage near route {route_id!r} MP range [{chunk_s_mp:.2f}, {chunk_e_mp:.2f}]."
               if route_id is not None else
               f"Check DEM coverage near lon/lat ({coords_wgs_raw[0][0]:.5f}, {coords_wgs_raw[0][1]:.5f}).")
        )
        logging.warning(warning_msg)
        return _fail(warning_msg)

    structure_mask = None
    structure_records = None
    if route_id is not None:
        structure_records = []
        total_len = max(d_axis[-1], 1e-9)
        mileposts = chunk_s_mp + (d_axis / total_len) * (chunk_e_mp - chunk_s_mp)

        structure_mask = build_structure_mask(
            mileposts=mileposts,
            coords_wgs=coords_wgs_smooth,
            route_id=route_id,
            hpms_structures=hpms_subset,
            nbi_nti_gdf=nbi_nti_gdf,
            spatial_tolerance_deg=0.00015,
            records_out=structure_records
        )

    z_fixed, structure_tier_out, structure_kind_out = repair_structure_spans(
        z_filled, spacing_m, params, structure_mask, structure_records
    )

    # Structure spans are weighted heavily so the spline follows each span
    # line, then held exactly to it. The weight tapers in from each end so
    # the vertical curve that usually straddles a bridge end or tunnel
    # portal can form there instead of being forced into a kink.
    spanned = structure_kind_out > 0
    depth_ft = distance_transform_edt(spanned) * spacing_m * FEET_PER_METER
    v_weights = np.where(spanned, 1.0 + (_SPAN_SPLINE_WEIGHT - 1.0) * np.clip(depth_ft / _SPAN_WEIGHT_TAPER_FT, 0, 1), 1.0)
    sz = UnivariateSpline(d_axis, z_fixed, w=v_weights, s=spline_s(s_factor_v, len(d_axis)))
    z_smooth = sz(d_axis)
    held = spanned & (v_weights >= _SPAN_SPLINE_WEIGHT)
    z_smooth[held] = z_fixed[held]

    # Roughness is measured against the repaired profile so a structure's
    # physical clearance isn't read as pavement roughness.
    iri_proxy = calculate_iri_proxy(z_fixed, z_smooth, spacing_m)
    asd_array = calculate_available_sight_distance(z_smooth, spacing_m)

    return {
        "ok": True,
        "spacing_m": spacing_m,
        "d_axis": d_axis,
        "coords_m_smooth": coords_m_smooth,
        "coords_wgs_smooth": coords_wgs_smooth,
        "z_smooth": z_smooth,
        # Raw coordinates in projected meters, for calculate_headings()
        # (headings computed from lon/lat would be distorted).
        "coords_m_raw": list(zip(x_raw, y_raw)),
        "coords_wgs_raw": [rev.transform(x, y) for x, y in zip(x_raw, y_raw)],
        "z_raw": z_filled,
        "elev_missing_frac": elev_missing_frac,
        "structure_tier": structure_tier_out,
        "structure_kind": structure_kind_out,
        # HPMS/NBI record matches at each point, whether or not a span was
        # detected there (e.g. a bridge over this route).
        "structure_record": structure_mask if structure_mask is not None else np.zeros(len(d_axis), dtype=int),
        "iri_proxy_in": iri_proxy,
        "available_sight_dist_ft": asd_array
    }

# -------------------------
# KDTree helper for 4D macro assignment
# -------------------------
def build_metric_kdtree(coords_wgs: List[Tuple[float, float]]):
    lon, lat = coords_wgs[0]
    utm = get_appropriate_utm_zone(lon, lat)
    tx = Transformer.from_crs("EPSG:4326", f"EPSG:{utm}", always_xy=True)
    pts_m = np.array([tx.transform(x, y) for x, y in coords_wgs], dtype=float)
    tree = cKDTree(pts_m)
    return tree, tx

def query_metric_kdtree(tree: cKDTree, tx: Transformer, query_coords_wgs: List[Tuple[float, float]],
                        return_dist: bool = False):
    """Index of the nearest tree point to each query point (and, with
    return_dist, the distance in meters)."""
    q_m = np.array([tx.transform(x, y) for x, y in query_coords_wgs], dtype=float)
    dist, idx = tree.query(q_m)
    return (idx, dist) if return_dist else idx

# ---------------------------------------------------------------------------
# Floating Progress UI
# ---------------------------------------------------------------------------
class GridProgressWindow:
    def __init__(self, title, total_tasks):
        self.total = total_tasks
        self.active = False
        try:
            import tkinter as tk
            from tkinter import ttk
            self.root = tk.Tk()
            self.root.title(title)
            self.root.geometry("380x130")
            self.root.attributes("-topmost", True)  # keep above other windows
            self.root.resizable(False, False)

            self.lbl_title = ttk.Label(self.root, text="Processing 1m Spatial Grid...", font=("Arial", 10, "bold"))
            self.lbl_title.pack(pady=(15, 5))

            self.progress = ttk.Progressbar(self.root, orient="horizontal", length=320, mode="determinate", maximum=self.total)
            self.progress.pack(pady=5)

            self.lbl_status = ttk.Label(self.root, text=f"0 / {self.total} Tiles Completed")
            self.lbl_status.pack(pady=5)

            self.root.update()
            self.active = True
        except Exception as e:
            import logging
            logging.warning(f"Could not initialize floating progress bar: {e}")

    def update(self, completed):
        if self.active:
            try:
                self.progress["value"] = completed
                self.lbl_status.config(text=f"{completed} / {self.total} Tiles Completed")
                self.root.update()
            except Exception:
                self.active = False

    def close(self):
        if self.active:
            try:
                self.root.destroy()
            except Exception:
                pass