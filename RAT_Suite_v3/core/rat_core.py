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
RAT CORE ENGINE v3.0 (Mathematical & Geospatial Backend)
--------------------------------------------------------------------------------
ROLE: The central processing brain for the Roadway Alignment Tool (RAT) Suite.
DESCRIPTION: 
This module contains zero GUI or plotting code. It handles the raw mathematical 
and geospatial lifting for the entire suite. Features include UTM coordinate 
projections, USGS DEM elevation fetching, UnivariateSpline smoothing, rigorous 
curvature calculations (kappa), and KDTree spatial indexing for 4D geometries.
CREATED BY: FHWA, Office of Highway Policy Information using Google Gemini and
ChatGPT.
CREATED ON: 4/23/2026
"""
from dataclasses import dataclass
from typing import Dict, List, Tuple, Optional
import math
import os
import numpy as np
import pandas as pd
import rasterio
import requests
from shapely.geometry import LineString, Point, MultiLineString
from shapely.wkt import loads
from shapely.ops import linemerge
from scipy.interpolate import UnivariateSpline
from scipy.signal import savgol_filter
from scipy.stats import linregress
from pyproj import Transformer
from scipy.spatial import cKDTree
# -------------------------
# Constants
# -------------------------
FEET_PER_METER = 3.28084
METRIC_R_TO_IMPERIAL_D = 1746.38  # deg per 100 ft from radius(m): D = 1746.38/R(m)
DEFAULTS = {
    # canonical internals are metric
    'DENSIFY_SPACING_FT': 10.0,
    'H_SMOOTH_FACTOR': 4500,            # Functional systems 1 and 2
    'H_BASE_SMOOTH_WINDOW': 21,
    'H_MIN_HEAD_CHANGE': 0.003,
    'H_MIN_DELTA': 3.5,                 # Rural
    'H_MIN_CURVE_LENGTH_FT': 100.0,     # Rural
    'H_MAX_RADIUS_FT': 165000.0,
    'H_LOOKAHEAD_DIST_M': 10.0,
    'V_SMOOTH_FACTOR': 4500,            # Functional systems 1 and 2
    'V_VC_THRESHOLD': 0.002,
    'V_MIN_CURVE_LENGTH_FT': 200.0,     # Rural
    'V_GAP_TOLERANCE': 5,
    'V_MIN_GRADE_CHANGE': 0.5,          # Rural
    'V_MIN_OFFSET_FT': 0.10,
    'V_REVERSAL_TOLERANCE': 0.02,
    'REGRESSION_WINDOW_FT': 500.0,
    'TREND_WINDOW_FT': 1000.0,
    'DIP_THRESHOLD_FT': 6.5,
    'BRIDGE_MAX_LEN_FT': 8200.0,
    'ENABLE_MERGE': False,
    'MERGE_GAP_FT': 600.0,
    'V_MERGE_GAP_FT': 1500.0,
    'H_MIN_CURVE_LENGTH_URBAN_FT': 50.0,
    'H_MIN_DELTA_URBAN': 5.0,
    'V_MIN_CURVE_LENGTH_URBAN_FT': 80.0,
    'V_MIN_GRADE_CHANGE_URBAN': 1.0,
    'H_SMOOTH_FACTOR_FS12_URBAN': 4500,
    'V_SMOOTH_FACTOR_FS12_URBAN': 4500,
    'H_SMOOTH_FACTOR_FS3_URBAN': 500,
    'V_SMOOTH_FACTOR_FS3_URBAN': 500,
    'H_SMOOTH_FACTOR_FS45_URBAN': 100,
    'V_SMOOTH_FACTOR_FS45_URBAN': 100,
    'H_SMOOTH_FACTOR_FS67_URBAN': 50,
    'V_SMOOTH_FACTOR_FS67_URBAN': 50,
}
# -------------------------
# Parameter normalization
# -------------------------
def build_params(user_params: Optional[Dict] = None) -> Dict:
    p = DEFAULTS.copy()
    if user_params:
        p.update(user_params)
    # numeric cleanup
    for k, v in list(p.items()):
        if isinstance(v, str):
            try:
                p[k] = float(v.replace(',', '').strip())
            except Exception:
                pass
    # enforce ints
    p['H_SMOOTH_FACTOR'] = int(p.get('H_SMOOTH_FACTOR', 4500))
    p['V_SMOOTH_FACTOR'] = int(p.get('V_SMOOTH_FACTOR', 4500))
    p['H_BASE_SMOOTH_WINDOW'] = int(p.get('H_BASE_SMOOTH_WINDOW', 21))
    if p['H_BASE_SMOOTH_WINDOW'] < 5:
        p['H_BASE_SMOOTH_WINDOW'] = 5
    # convert to metric canonical keys
    p['DENSIFY_SPACING_M'] = p.get('DENSIFY_SPACING_FT', 10.0) / FEET_PER_METER
    p['H_MIN_CURVE_LENGTH_M'] = p.get('H_MIN_CURVE_LENGTH_FT', 100.0) / FEET_PER_METER
    p['H_MIN_CURVE_LENGTH_URBAN_M'] = p.get('H_MIN_CURVE_LENGTH_URBAN_FT', 50.0) / FEET_PER_METER
    p['H_MAX_RADIUS'] = p.get('H_MAX_RADIUS_FT', 165000.0) / FEET_PER_METER
    p['V_MIN_CURVE_LENGTH'] = p.get('V_MIN_CURVE_LENGTH_FT', 200.0) / FEET_PER_METER
    p['V_MIN_CURVE_LENGTH_URBAN'] = p.get('V_MIN_CURVE_LENGTH_URBAN_FT', 80.0) / FEET_PER_METER
    p['REGRESSION_WINDOW_M'] = p.get('REGRESSION_WINDOW_FT', 500.0) / FEET_PER_METER
    p['TREND_WINDOW_M'] = p.get('TREND_WINDOW_FT', 1000.0) / FEET_PER_METER
    p['DIP_THRESHOLD_M'] = p.get('DIP_THRESHOLD_FT', 6.5) / FEET_PER_METER
    p['BRIDGE_MAX_LEN_M'] = p.get('BRIDGE_MAX_LEN_FT', 8200.0) / FEET_PER_METER
    return p
# -------------------------
# Download USGS DEMs
# -------------------------
def download_dems(wkt_list: list, out_dir: str):
    """Downloads required 1x1 degree USGS DEM tiles to local disk."""
    if not wkt_list: return
    needed_tiles = set()
    for wkt_str in wkt_list:
        try:
            geom = loads(wkt_str) if isinstance(wkt_str, str) else wkt_str
            if geom.is_empty: continue
            minx, miny, maxx, maxy = geom.bounds
            for lat in range(int(math.floor(miny)), int(math.ceil(maxy)) + 1):
                for lon in range(int(math.floor(minx)), int(math.ceil(maxx)) + 1):
                    needed_tiles.add(f"n{lat:02d}w{abs(lon):03d}")
        except: pass
    ELEVATION_SOURCE_URL = "https://prd-tnm.s3.amazonaws.com/StagedProducts/Elevation/13/TIFF/current"
    import logging
    import requests
    for tile in needed_tiles:
        path = os.path.join(out_dir, f"USGS_13_{tile}.tif")
        if not os.path.exists(path):
            logging.info(f"Downloading required DEM tile: {tile}...")
            try:
                r = requests.get(f"{ELEVATION_SOURCE_URL}/{tile}/USGS_13_{tile}.tif", stream=True, timeout=60)
                if r.status_code == 200:
                    with open(path, 'wb') as f:
                        for chunk in r.iter_content(chunk_size=8192):
                            f.write(chunk)
            except Exception as e:
                logging.error(f"Failed to download {tile}: {e}")
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
def stitch_linestrings_ordered(wkt_list: List[str], snap_tol=1e-6) -> List[LineString]:
    stitched_segments = []
    current_coords = []
    for ws in wkt_list:
        if not ws:
            continue
        try:
            g = loads(ws)
        except Exception:
            continue
        parts = list(g.geoms) if g.geom_type == 'MultiLineString' else [g] if g.geom_type == 'LineString' else []
        for part in parts:
            coords = list(part.coords)
            if not coords:
                continue
            if not current_coords:
                current_coords.extend(coords)
            elif Point(current_coords[-1]).distance(Point(coords[0])) < snap_tol:
                current_coords.extend(coords[1:])
            else:
                if len(current_coords) >= 2:
                    stitched_segments.append(LineString(current_coords))
                current_coords = list(coords)
    if len(current_coords) >= 2:
        stitched_segments.append(LineString(current_coords))
    return stitched_segments
# -------------------------
# DEM sampling
# -------------------------
def get_elevations(coords_wgs: List[Tuple[float, float]], dem_folder: str) -> np.ndarray:
    dem_cache = {}
    vals = []
    for lon, lat in coords_wgs:
        tile = f"n{int(math.ceil(lat)):02d}w{int(math.ceil(abs(lon))):03d}"
        if tile not in dem_cache:
            path = os.path.join(dem_folder, f"USGS_13_{tile}.tif")
            dem_cache[tile] = rasterio.open(path) if os.path.exists(path) else None
        z = np.nan
        ds = dem_cache[tile]
        if ds is not None:
            try:
                z = next(ds.sample([(lon, lat)]))[0]
            except Exception:
                pass
            if z < -1000:
                z = np.nan
        vals.append(z)
    for ds in dem_cache.values():
        if ds is not None:
            ds.close()
    return np.asarray(vals, dtype=float)
# -------------------------
# Signal/math helpers
# -------------------------
def safe_savgol(signal: np.ndarray, window: int, polyorder: int = 2) -> np.ndarray:
    n = len(signal)
    if n <= polyorder + 1:
        return signal
    w = int(window)
    if w >= n:
        w = n - 1 if n % 2 == 0 else n
    if w % 2 == 0:
        w -= 1
    if w < polyorder + 2:
        w = polyorder + 2
        if w % 2 == 0:
            w += 1
    if w > n:
        return signal
    try:
        return savgol_filter(signal, w, polyorder)
    except Exception:
        return signal
def calculate_headings(coords_m: List[Tuple[float, float]]) -> np.ndarray:
    pts = np.array(coords_m)
    diffs = pts[1:] - pts[:-1]
    headings = np.arctan2(diffs[:, 1], diffs[:, 0])
    headings_deg = np.degrees(headings)
    headings_deg = np.append(headings_deg, headings_deg[-1])
    return np.unwrap(headings_deg, period=360)
def get_tangent_grade(z_vals: np.ndarray, idx: int, spacing_m: float, window_m: float) -> float:
    half_w = int(window_m / spacing_m)
    start = max(0, idx - half_w)
    end = min(len(z_vals), idx + half_w)
    if end - start < 3:
        return 0.0
    xs = np.arange(start, end) * spacing_m
    ys = z_vals[start:end]
    res = linregress(xs, ys)
    return res.slope * 100.0
def fix_profile_by_deviation(z_vals: np.ndarray, spacing_m: float, params: Dict) -> np.ndarray:
    z_fixed = np.copy(z_vals)
    n = len(z_fixed)
    window_pts = int(params['TREND_WINDOW_M'] / spacing_m)
    if window_pts < 3:
        window_pts = 3
    if window_pts % 2 == 0:
        window_pts += 1
    z_trend = pd.Series(z_fixed).rolling(window=window_pts, center=True, min_periods=1).median().to_numpy()
    deviation = z_fixed - z_trend
    is_dip = deviation < -params['DIP_THRESHOLD_M']
    i = 0
    while i < n:
        if not is_dip[i]:
            i += 1
            continue
        start_dip = i
        while i < n and is_dip[i]:
            i += 1
        end_dip = i
        anchor_start = max(0, start_dip - 1)
        while anchor_start > 0 and (z_fixed[anchor_start] < z_trend[anchor_start] - 0.5):
            anchor_start -= 1
        anchor_end = min(n - 1, end_dip)
        while anchor_end < n - 1 and (z_fixed[anchor_end] < z_trend[anchor_end] - 0.5):
            anchor_end += 1
        span_len = (anchor_end - anchor_start) * spacing_m
        if span_len < params['BRIDGE_MAX_LEN_M'] and anchor_end > anchor_start:
            z1, z2 = z_fixed[anchor_start], z_fixed[anchor_end]
            interp = np.linspace(z1, z2, anchor_end - anchor_start + 1)
            z_fixed[anchor_start:anchor_end + 1] = interp
        i = max(i, anchor_end + 1)
    return z_fixed
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
# -------------------------
# Horizontal curve analysis (curvature-based)
# -------------------------
def analyze_horizontal_curvature(
        coords_m_smooth: List[Tuple[float, float]], 
        spacing_m: float, 
        params: Dict,
        is_urban: bool = False
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
    # Direction sign from signed curvature surrogate
    signed_num = (dx * ddy - dy * ddx)
    direction_sign = np.sign(signed_num)
    # Curve if local radius below max radius
    kappa_thresh = 1.0 / max(params['H_MAX_RADIUS'], 1e-6)
    is_curve = kappa >= kappa_thresh
    # min_len = params['H_MIN_CURVE_LENGTH_M']
    min_len = params.get('H_MIN_CURVE_LENGTH_URBAN_M', 15.24) if is_urban else params['H_MIN_CURVE_LENGTH_M']
    min_delta = params.get('H_MIN_DELTA_URBAN', 5.0) if is_urban else params['H_MIN_DELTA']
    curves = []
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
        if e <= s:
            continue
        length = (e - s) * spacing_m
        if length < min_len:
            continue
        # Deflection via headings
        headings = calculate_headings(list(zip(xs, ys)))
        delta = abs(headings[e] - headings[s])
        if delta < min_delta:
            continue
        seg_kappa = kappa[s:e+1]
        max_k = np.nanmax(seg_kappa) if len(seg_kappa) > 0 else 0.0
        min_radius = (1.0 / max_k) if max_k > 1e-12 else 99999.0
        radius = min_radius  # curvature-based representative radius
        if radius > params['H_MAX_RADIUS']:
            continue
        deg_per_100 = METRIC_R_TO_IMPERIAL_D / radius if radius > 0 else 0
        cbin = classify_bin(deg_per_100)
        direction = "Right" if sgn > 0 else "Left"
        curves.append({
            'Start_Dist': s * spacing_m,
            'End_Dist': e * spacing_m,
            'Length_m': length,
            'Length': length,
            'Radius_m': radius,
            'Radius': radius,
            'Min_Radius_m': min_radius,
            'Delta': float(delta),
            'Dir': direction,
            'Bin': cbin,
            'Merge_Status': 'Simple'
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
        if nxt['Dir'] == cur['Dir'] and gap_ft < params['MERGE_GAP_FT']:
            cur['End_Dist'] = nxt['End_Dist']
            cur['Length_m'] = cur['End_Dist'] - cur['Start_Dist']
            cur['Length'] = cur['Length_m']
            # weighted radius
            l1 = max(cur['Length_m'], 1e-6)
            l2 = max(nxt['Length_m'], 1e-6)
            cur['Radius_m'] = (cur['Radius_m'] * l1 + nxt['Radius_m'] * l2) / (l1 + l2)
            cur['Radius'] = cur['Radius_m']
            cur['Min_Radius_m'] = min(cur.get('Min_Radius_m', 99999), nxt.get('Min_Radius_m', 99999))
            cur['Delta'] += nxt['Delta']
            cur['Merge_Status'] = 'Compound'
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
        params: Dict,
        is_urban: bool = False
    ) -> List[Dict]:
    grads = np.gradient(z_smooth, spacing_m) * 100.0
    gchg = np.gradient(grads, spacing_m)
    is_vc = np.abs(gchg) > params['V_VC_THRESHOLD']
    min_len = params.get('V_MIN_CURVE_LENGTH_URBAN', 24.38) if is_urban else params['V_MIN_CURVE_LENGTH']
    min_g_change = params.get('V_MIN_GRADE_CHANGE_URBAN', 1.0) if is_urban else params['V_MIN_GRADE_CHANGE']
    curves = []
    n = len(z_smooth)
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
        if e <= s:
            continue
        length = (e - s) * spacing_m
        if length < min_len:
            continue
        x = np.arange(s, e + 1) * spacing_m
        z = z_smooth[s:e + 1]
        if len(x) < 5:
            continue
        # quadratic fit: z = a x^2 + b x + c
        try:
            a, b, c = np.polyfit(x, z, 2)
        except Exception:
            continue
        g1 = (2 * a * x[0] + b) * 100.0
        g2 = (2 * a * x[-1] + b) * 100.0
        A = g2 - g1
        if abs(A) < min_g_change:
            continue
        K = length / abs(A) if abs(A) > 1e-6 else 999.0
        E_m = abs(A * length / 800.0)
        if E_m * FEET_PER_METER < params['V_MIN_OFFSET_FT']:
            continue
        mid_idx = len(x) // 2
        z_mid = z[mid_idx]
        z_chord = z[0] + ((x[mid_idx] - x[0]) / max((x[-1] - x[0]), 1e-9)) * (z[-1] - z[0])
        vtype = "CREST" if z_mid > z_chord else "SAG"
        curves.append({
            'Start_Dist': s * spacing_m,
            'End_Dist': e * spacing_m,
            'Length_m': length,
            'Length': length,
            'Grade_In': g1,
            'Grade_Out': g2,
            'Alg_Diff': A,
            'K_Value': K,
            'Type': vtype,
            'E': E_m,
            'Grade_Bin': classify_grade_bin(abs(A)),
        })
    return curves
# -------------------------
# Shared route processing
# -------------------------
def smooth_plan_profile_from_linestring(
        line: LineString, 
        dem_dir: str, 
        params: dict, 
        f_sys: int = 1,
        is_urban: bool = False
        ) -> dict:
    line_wgs = line
    lon, lat = line_wgs.coords[0]
    utm = get_appropriate_utm_zone(lon, lat)
    fwd = Transformer.from_crs("EPSG:4326", f"EPSG:{utm}", always_xy=True)
    rev = Transformer.from_crs(f"EPSG:{utm}", "EPSG:4326", always_xy=True)
    coords_m = [fwd.transform(x, y) for x, y in line_wgs.coords]
    coords_m = densify_coords_line(coords_m, params['DENSIFY_SPACING_M'])
    if len(coords_m) < max(params['H_BASE_SMOOTH_WINDOW'] + 2, 8):
        return None
    x_raw = np.array([c[0] for c in coords_m])
    y_raw = np.array([c[1] for c in coords_m])
    # --- VARIABLE STIFFNESS LOGIC ---
    if f_sys in [1, 2]:
        s_factor_h = params.get('H_SMOOTH_FACTOR_FS12_URBAN', 2500) if is_urban else params.get('H_SMOOTH_FACTOR', 4500)
        s_factor_v = params.get('V_SMOOTH_FACTOR_FS12_URBAN', 2500) if is_urban else params.get('V_SMOOTH_FACTOR', 4500)
    elif f_sys == 3:
        s_factor_h = params.get('H_SMOOTH_FACTOR_FS3_URBAN', 2000) if is_urban else params.get('H_SMOOTH_FACTOR_FS3', 4000)
        s_factor_v = params.get('V_SMOOTH_FACTOR_FS3_URBAN', 2000) if is_urban else params.get('V_SMOOTH_FACTOR_FS3', 4000)
    elif f_sys in [4, 5]:
        s_factor_h = params.get('H_SMOOTH_FACTOR_FS45_URBAN', 1200) if is_urban else params.get('H_SMOOTH_FACTOR_FS45', 2500)
        s_factor_v = params.get('V_SMOOTH_FACTOR_FS45_URBAN', 1200) if is_urban else params.get('V_SMOOTH_FACTOR_FS45', 2500)
    else:
        s_factor_h = params.get('H_SMOOTH_FACTOR_FS67_URBAN', 500) if is_urban else params.get('H_SMOOTH_FACTOR_FS67', 1000)
        s_factor_v = params.get('V_SMOOTH_FACTOR_FS67_URBAN', 500) if is_urban else params.get('V_SMOOTH_FACTOR_FS67', 1000)

    spacing_m = LineString(coords_m).length / (len(coords_m) - 1)
    d_axis = np.arange(len(coords_m)) * spacing_m
    sx = UnivariateSpline(d_axis, x_raw, s=s_factor_h)
    sy = UnivariateSpline(d_axis, y_raw, s=s_factor_h)
    coords_m_smooth = list(zip(sx(d_axis), sy(d_axis)))
    coords_wgs_smooth = [rev.transform(x, y) for x, y in coords_m_smooth]
    z_raw = get_elevations(coords_wgs_smooth, dem_dir)
    z_filled = pd.Series(z_raw).interpolate(limit_direction='both').to_numpy()
    z_fixed = fix_profile_by_deviation(z_filled, spacing_m, params)
    sz = UnivariateSpline(d_axis, z_fixed, s=s_factor_v)
    z_smooth = sz(d_axis)
    # optional heading smoothing (if needed elsewhere)
    h_unwrapped = calculate_headings(coords_m_smooth)
    h_sm = np.degrees(safe_savgol(np.radians(h_unwrapped), params['H_BASE_SMOOTH_WINDOW'], 2))
    return {
        "spacing_m": spacing_m,
        "d_axis": d_axis,
        "coords_m_smooth": coords_m_smooth,
        "coords_wgs_smooth": coords_wgs_smooth,
        "z_smooth": z_smooth,
        "headings_unwrapped_smooth_deg": h_sm,
        "coords_wgs_raw": [rev.transform(x, y) for x, y in zip(x_raw, y_raw)],
        "z_raw": z_filled
    }
# -------------------------
# KDTree helper for 4D macro assignment (metric-safe)
# -------------------------
def build_metric_kdtree(coords_wgs: List[Tuple[float, float]]):
    lon, lat = coords_wgs[0]
    utm = get_appropriate_utm_zone(lon, lat)
    tx = Transformer.from_crs("EPSG:4326", f"EPSG:{utm}", always_xy=True)
    pts_m = np.array([tx.transform(x, y) for x, y in coords_wgs], dtype=float)
    tree = cKDTree(pts_m)
    return tree, tx
def query_metric_kdtree(tree: cKDTree, tx: Transformer, query_coords_wgs: List[Tuple[float, float]]) -> np.ndarray:
    q_m = np.array([tx.transform(x, y) for x, y in query_coords_wgs], dtype=float)
    _, idx = tree.query(q_m)
    return idx