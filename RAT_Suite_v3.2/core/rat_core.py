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
RAT CORE ENGINE v3.5 (Mathematical & Geospatial Backend)
--------------------------------------------------------------------------------
ROLE: Central processing engine for the Roadway Alignment Tool (RAT) Suite.
DESCRIPTION:
This module contains no GUI or plotting code. It provides the mathematical and
geospatial processing functions used by all suite modules, including UTM
coordinate projection, USGS DEM elevation retrieval, UnivariateSpline smoothing,
calculus-based curvature analysis (kappa), and KDTree spatial indexing for
4D geometry generation.

CHANGES FROM v3.4:
  - Updated DEFAULTS smoothing factors to match national calibration results
    derived from the RAT National Calibration Engine (v3.8). Prior defaults
    were pre-calibration placeholders. Updated values represent the recommended
    national fallback factors when no state-specific JSON entry is available.
  - Updated build_params() integer fallback values for H_SMOOTH_FACTOR and
    V_SMOOTH_FACTOR from 4500 to 400 and 1000 respectively, consistent with
    updated DEFAULTS.
  - Replaced the FS2-7 smoothing factor if/elif chain in
    smooth_plan_profile_from_linestring() with a dict lookup. Functionally
    equivalent; reduces maintenance overhead as additional FS-specific
    parameters are added.
  - Removed inline comment tag from geopandas import (cosmetic only).

CREATED BY: Federal Highway Administration, Office of Highway Policy Information.
CREATED ON: 5/14/2026
"""
from dataclasses import dataclass
from typing import Dict, List, Tuple, Optional
import math
import os
import json
import logging
import numpy as np
import pandas as pd
import geopandas as gpd
import rasterio
import requests
from shapely.geometry import LineString, Point, MultiLineString, shape
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
SOCRATA_DEFAULT = "https://datahub.transportation.gov/resource/42um-tgh5.json"

DEFAULTS = {
    # --- Geometry preprocessing ---
    'DENSIFY_SPACING_FT':          10.0,
    'H_BASE_SMOOTH_WINDOW':        21,
    'H_MIN_HEAD_CHANGE':           0.003,
    'H_LOOKAHEAD_DIST_M':          10.0,

    # --- Horizontal curve detection ---
    'H_MIN_DELTA':                 3.5,
    'H_MIN_CURVE_LENGTH_FT':       100.0,
    'H_MAX_RADIUS_FT':             165000.0,
    'H_MIN_CURVE_LENGTH_URBAN_FT': 50.0,
    'H_MIN_DELTA_URBAN':           5.0,

    # --- Vertical curve detection ---
    'V_VC_THRESHOLD':              0.002,
    'V_MIN_CURVE_LENGTH_FT':       200.0,
    'V_GAP_TOLERANCE':             5,
    'V_MIN_GRADE_CHANGE':          0.5,
    'V_MIN_OFFSET_FT':             0.10,
    'V_REVERSAL_TOLERANCE':        0.02,
    'REGRESSION_WINDOW_FT':        500.0,
    'V_MIN_CURVE_LENGTH_URBAN_FT': 80.0,
    'V_MIN_GRADE_CHANGE_URBAN':    1.0,

    # --- Profile bridging ---
    'TREND_WINDOW_FT':             2000.0, # was 1000
    'DIP_THRESHOLD_FT':            6.5,
    'BRIDGE_MAX_LEN_FT':           8200.0,

    # --- Merge controls ---
    'ENABLE_MERGE':                False,
    'MERGE_GAP_FT':                600.0,
    'V_MERGE_GAP_FT':              1500.0,

    # --- National smoothing factor defaults (FS 1 baseline) ---
    # These values are the recommended national fallback factors derived from
    # the RAT National Calibration Engine v3.8 full 50-state sweep. They are
    # applied when no state-specific entry exists in national_smoothing_factors.json.
    # State-specific values from the JSON always take precedence over these defaults.
    'H_SMOOTH_FACTOR':    400,   # FS 1 (Interstate)
    'V_SMOOTH_FACTOR':    1000,  # FS 1 (Interstate)

    # --- Per-functional-system smoothing overrides ---
    'H_SMOOTH_FACTOR_FS2': 200,   # Other Freeways & Expressways
    'V_SMOOTH_FACTOR_FS2': 1000,
    'H_SMOOTH_FACTOR_FS3': 200,   # Other Principal Arterial
    'V_SMOOTH_FACTOR_FS3': 1200,
    'H_SMOOTH_FACTOR_FS4': 200,   # Minor Arterial
    'V_SMOOTH_FACTOR_FS4': 400,
    'H_SMOOTH_FACTOR_FS5': 200,   # Major Collector
    'V_SMOOTH_FACTOR_FS5': 600,
    'H_SMOOTH_FACTOR_FS6': 200,   # Minor Collector
    'V_SMOOTH_FACTOR_FS6': 400,
    'H_SMOOTH_FACTOR_FS7': 200,   # Local
    'V_SMOOTH_FACTOR_FS7': 400,
}

# -------------------------
# Parameter normalization
# -------------------------
def build_params(user_params: Optional[Dict] = None) -> Dict:
    """
    Constructs the full parameter dict for a processing run.

    Resolution order (later entries take precedence):
      1. DEFAULTS  -- national calibration fallback values
      2. national_smoothing_factors.json  -- state-specific calibrated factors,
         applied when STATE_FIPS is present in user_params
      3. user_params  -- explicit overrides from the GUI or CLI caller

    All Imperial-unit parameters are converted to metric equivalents and
    appended to the returned dict for use by the core engine.
    """
    p = DEFAULTS.copy()
    
    if user_params and user_params.get('STATE_FIPS') and user_params.get('OUTPUT_DIR'):
        state_fips = str(user_params.get('STATE_FIPS')).zfill(2)
        json_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'national_smoothing_factors.json')
        
        if os.path.exists(json_path):
            try:
                with open(json_path, 'r') as f:
                    master_data = json.load(f)
                if state_fips in master_data:
                    state_factors = master_data[state_fips]
                    for key, val in state_factors.items():
                        if val is not None:
                            p[key] = val
            except Exception as e:
                logging.error(f"Failed to read national smoothing factors JSON: {e}")

    if user_params:
        for k, v in user_params.items():
            if v is not None and str(v).strip() != "":
                p[k] = v

    for k, v in list(p.items()):
        if k in ["STATE_FIPS", "OUTPUT_DIR", "INPUT_URL", "SOCRATA_TOKEN", "PP_ROUTE_ID", "DEM_DIR"]:
            continue
        if isinstance(v, str):
            try:
                p[k] = float(v.replace(',', '').strip())
            except Exception:
                pass

    p['H_SMOOTH_FACTOR'] = int(p.get('H_SMOOTH_FACTOR', 400))
    p['V_SMOOTH_FACTOR'] = int(p.get('V_SMOOTH_FACTOR', 1000))
    p['H_BASE_SMOOTH_WINDOW'] = int(p.get('H_BASE_SMOOTH_WINDOW', 21))
    if p['H_BASE_SMOOTH_WINDOW'] < 5:
        p['H_BASE_SMOOTH_WINDOW'] = 5

    p['DENSIFY_SPACING_M'] = p.get('DENSIFY_SPACING_FT', 10.0) / FEET_PER_METER
    p['H_MIN_CURVE_LENGTH_M'] = p.get('H_MIN_CURVE_LENGTH_FT', 100.0) / FEET_PER_METER
    p['H_MIN_CURVE_LENGTH_URBAN_M'] = p.get('H_MIN_CURVE_LENGTH_URBAN_FT', 50.0) / FEET_PER_METER
    p['H_MAX_RADIUS'] = p.get('H_MAX_RADIUS_FT', 165000.0) / FEET_PER_METER
    p['V_MIN_CURVE_LENGTH'] = p.get('V_MIN_CURVE_LENGTH_FT', 200.0) / FEET_PER_METER
    p['V_MIN_CURVE_LENGTH_URBAN'] = p.get('V_MIN_CURVE_LENGTH_URBAN_FT', 80.0) / FEET_PER_METER
    p['REGRESSION_WINDOW_M'] = p.get('REGRESSION_WINDOW_FT', 500.0) / FEET_PER_METER
    p['TREND_WINDOW_M'] = p.get('TREND_WINDOW_FT', 2000.0) / FEET_PER_METER # was 1000
    p['DIP_THRESHOLD_M'] = p.get('DIP_THRESHOLD_FT', 6.5) / FEET_PER_METER
    p['BRIDGE_MAX_LEN_M'] = p.get('BRIDGE_MAX_LEN_FT', 8200.0) / FEET_PER_METER
    return p

# ---------------------------------------------------------------------------
# Centralized Data Loaders (Socrata & Local)
# ---------------------------------------------------------------------------
def fetch_socrata_state(state_fips: str, token: str = "") -> pd.DataFrame:
    headers = {"X-App-Token": token} if token else {}
    where_clause = f"stateid='{state_fips}' AND facility_type IN ('1', '2')"
    params = {"$limit": 100_000, "$offset": 0, "$where": where_clause}

    rows = []
    logging.info(f"Fetching Socrata data for State FIPS {state_fips}...")
    while True:
        r = requests.get(SOCRATA_DEFAULT, params=params, headers=headers, timeout=120)
        r.raise_for_status()
        data = r.json()
        if not data:
            break
        rows.extend(data)
        if len(data) < 100_000:
            break
        params["$offset"] += 100_000

    if not rows:
        raise ValueError(f"No records found for State {state_fips}.")

    df = pd.DataFrame(rows)
    geom_col = next((c for c in df.columns if c.lower() in ["line", "geometry", "the_geom"]), None)

    if geom_col is None:
        raise ValueError("Could not identify geometry column in Socrata response.")

    def geom_to_wkt(v):
        if isinstance(v, dict):
            try:
                return shape(v).wkt
            except Exception:
                return None
        return str(v)

    df["WKT"] = df[geom_col].apply(geom_to_wkt)

    col_map = {
        "route_id":    "RouteId",
        "begin_point": "Start_MP",
        "end_point":   "End_MP",
        "f_system":    "FSystem",
        "urban_code":  "UrbanID",
        "facility_type": "Facility_Type"
    }
    df.rename(columns=col_map, inplace=True)
    
    if "Start_MP" not in df.columns: df["Start_MP"] = 0.0
    if "End_MP" not in df.columns: df["End_MP"] = 0.0
    if "FSystem" not in df.columns: df["FSystem"] = 1
    if "UrbanID" not in df.columns: df["UrbanID"] = 99999
    if "Facility_Type" not in df.columns: df["Facility_Type"] = 2

    df["Start_MP"] = pd.to_numeric(df["Start_MP"], errors="coerce").fillna(0.0)
    df["End_MP"]   = pd.to_numeric(df["End_MP"],   errors="coerce").fillna(0.0)
    df["FSystem"]  = pd.to_numeric(df["FSystem"],  errors="coerce").fillna(1).astype(int)
    df["UrbanID"] = pd.to_numeric(df["UrbanID"], errors="coerce").fillna(99999)
    df["Facility_Type"] = pd.to_numeric(df["Facility_Type"], errors="coerce").fillna(2).astype(int)
    
    df["Is_Urban"] = (df["UrbanID"] != 99999) & (df["UrbanID"] != 0)

    df = df[df["WKT"].notna() & (df["WKT"] != "")].copy()
    return df


def load_local_hpms(path: str) -> pd.DataFrame:
    if not os.path.exists(path):
        raise FileNotFoundError(path)
    if path.lower().endswith(".csv"):
        df = pd.read_csv(path, low_memory=False)
        geom_col = next((c for c in df.columns if c.lower() in ["wkt", "wkt_zm", "geometry", "shape", "the_geom", "line"]), None)
        if geom_col:
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
        elif c in ["urban_id", "urbanid", "urban_code"]: col_map[col] = "UrbanID"
        elif c in ["facility_type", "fac_type", "facility_typ", "facilitytype"]: col_map[col] = "Facility_Type"
    df.rename(columns=col_map, inplace=True)

    if "RouteId" not in df.columns: raise ValueError("Missing RouteId column.")
    if "WKT" not in df.columns: raise ValueError("Missing geometry/WKT column.")

    if "Start_MP" not in df.columns: df["Start_MP"] = 0.0
    if "End_MP" not in df.columns: df["End_MP"] = 0.0
    if "FSystem" not in df.columns: df["FSystem"] = 1
    if "UrbanID" not in df.columns: df["UrbanID"] = 99999
    if "Facility_Type" not in df.columns: df["Facility_Type"] = 2

    df["RouteId"] = df["RouteId"].astype(str).str.strip().str.upper()
    df["Start_MP"] = pd.to_numeric(df["Start_MP"], errors="coerce").fillna(0.0)
    df["End_MP"] = pd.to_numeric(df["End_MP"], errors="coerce").fillna(0.0)
    df["FSystem"] = pd.to_numeric(df["FSystem"], errors="coerce").fillna(1).astype(int)
    df["Facility_Type"] = pd.to_numeric(df["Facility_Type"], errors="coerce").fillna(2).astype(int)
    
    df["UrbanID"] = pd.to_numeric(df["UrbanID"], errors="coerce").fillna(99999)
    df["Is_Urban"] = (df["UrbanID"] != 99999) & (df["UrbanID"] != 0)

    df["WKT"] = df["WKT"].astype(str).str.strip()
    df = df[df["WKT"].notna() & (df["WKT"] != "")].copy()
    return df

# -------------------------
# Download USGS DEMs
# -------------------------
def download_dems(wkt_list: list, out_dir: str):
    if not wkt_list: return
    needed_tiles = set()
    for wkt_str in wkt_list:
        try:
            geom = loads(wkt_str) if isinstance(wkt_str, str) else wkt_str
            if geom.is_empty: continue
            minx, miny, maxx, maxy = geom.bounds
            for lat in range(int(math.floor(miny)), int(math.ceil(maxy)) + 1):
                for lon in range(int(math.floor(minx)), int(math.ceil(maxx)) + 1):
                    ns = 'n' if lat >= 0 else 's'
                    ew = 'e' if lon >= 0 else 'w'
                    needed_tiles.add(f"{ns}{abs(lat):02d}{ew}{abs(lon):03d}")
        except: pass

    ELEVATION_SOURCE_URL = "https://prd-tnm.s3.amazonaws.com/StagedProducts/Elevation/13/TIFF/current"
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
        ns = 'n' if lat >= 0 else 's'
        ew = 'e' if lon >= 0 else 'w'
        tile = f"{ns}{int(math.ceil(abs(lat))):02d}{ew}{int(math.ceil(abs(lon))):03d}"
        
        if tile not in dem_cache:
            path = os.path.join(dem_folder, f"USGS_13_{tile}.tif")
            if os.path.exists(path):
                try:
                    dem_cache[tile] = rasterio.open(path)
                except Exception as e:
                    logging.warning(f"Failed to open DEM {tile}: {e}")
                    dem_cache[tile] = None
            else:
                dem_cache[tile] = None

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
        w = n if n % 2 != 0 else n - 1
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
        while anchor_start > 0 and (z_fixed[anchor_start] < z_trend[anchor_start] - 0.15): # Threshold was 0.5
            anchor_start -= 1
        anchor_end = min(n - 1, end_dip)
        while anchor_end < n - 1 and (z_fixed[anchor_end] < z_trend[anchor_end] - 0.15): # Threshold was 0.5
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
    
    signed_num = (dx * ddy - dy * ddx)
    direction_sign = np.sign(signed_num)
    
    kappa_thresh = 1.0 / max(params['H_MAX_RADIUS'], 1e-6)
    is_curve = kappa >= kappa_thresh
    
    min_len = params.get('H_MIN_CURVE_LENGTH_URBAN_M', 15.24) if is_urban else params['H_MIN_CURVE_LENGTH_M']
    min_delta = params.get('H_MIN_DELTA_URBAN', 5.0) if is_urban else params['H_MIN_DELTA']
    
    # Calculate headings globally before looping
    headings_array = calculate_headings(list(zip(xs, ys)))
    
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
        f_sys: int = 1
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
    
    s_factor_h = params.get("H_SMOOTH_FACTOR", 400)
    s_factor_v = params.get("V_SMOOTH_FACTOR", 1000)

    # Per-functional-system smoothing overrides.
    # A dict lookup replaces the prior if/elif chain for maintainability.
    _fs_suffix = {2: "FS2", 3: "FS3", 4: "FS4", 5: "FS5", 6: "FS6", 7: "FS7"}
    if f_sys in _fs_suffix:
        suffix     = _fs_suffix[f_sys]
        s_factor_h = params.get(f"H_SMOOTH_FACTOR_{suffix}", s_factor_h)
        s_factor_v = params.get(f"V_SMOOTH_FACTOR_{suffix}", s_factor_v)

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
# KDTree helper for 4D macro assignment
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
