# apps/rat_national_calibration_cli.py

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

# Examples: Run from the RAT Suite root directory:
#
#   Single state (Nebraska):
#     python tools/rat_national_calibration_cli.py --outdir output/calibration \
#       --demdir /path/to/dem/cache --state 31
#
#   All states with parallelization (30 cores, reserve 2):
#     python tools/rat_national_calibration_cli.py --outdir output/calibration \
#       --demdir /path/to/dem/cache --state ALL --total-cores 30 --reserved-cores 2
#
#   With logging to file (PowerShell):
#     python tools/rat_national_calibration_cli.py --outdir output/calibration \
#       --demdir /path/to/dem/cache --state ALL --total-cores 30 --reserved-cores 2 | Tee-Object -FilePath output/calibration_run.log
"""
RAT NATIONAL CALIBRATION ENGINE v3.3
--------------------------------------------------------------------------------
ROLE: Automated national smoothing factor estimator with in-memory caching.
DESCRIPTION:
Downloads HPMS data, extracts random samples for each Functional System,
and determines optimal smoothing parameters via nested parameter sweeps.

Performance features:
  - Parallel tile downloads (ThreadPoolExecutor, I/O-bound)
  - H sweep: in-memory tile cache + ProcessPoolExecutor (CPU-bound spline work)
  - V sweep: RAM-spline cache (pre-computed z_fixed arrays, near-instant sweep)
  - Dynamic core allocation split between state-level and sweep-level workers
  - Early exit once 3 consecutive failures with rising RMSE confirm elbow region
"""

import os
import sys
import csv
import json
import math
import glob
import random
import logging
import argparse
import tempfile
import shutil
from datetime import datetime
import multiprocessing
from typing import Dict, List, Tuple, Optional, Any
from concurrent.futures import ThreadPoolExecutor, ProcessPoolExecutor, as_completed

import numpy as np
import pandas as pd
import requests
from scipy.interpolate import UnivariateSpline
from shapely.geometry import LineString, shape
from shapely.wkt import loads
from shapely.ops import substring
from pyproj import Transformer

# --- Path bootstrap for core import ---
THIS_DIR    = os.path.dirname(os.path.abspath(__file__))
RAT_SUITE_DIR = os.path.dirname(THIS_DIR)
if RAT_SUITE_DIR not in sys.path:
    sys.path.insert(0, RAT_SUITE_DIR)

from core.rat_core import (
    build_params,
    stitch_linestrings_ordered,
    smooth_plan_profile_from_linestring,
    download_dems,
    download_high_res_dem_tile,
    densify_coords_line,
    get_appropriate_utm_zone,
    fix_profile_by_deviation,
    FEET_PER_METER,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s: %(message)s")
SOCRATA_DEFAULT = "https://datahub.transportation.gov/resource/42um-tgh5.json"

# ===========================================================================
# CONSTANTS & DEFAULTS
# ===========================================================================
SOCRATA_TOKEN        = os.environ.get("RAT_SOCRATA_TOKEN", "")  
MAX_PARALLEL_STATES  = max(1, os.cpu_count() - 2)

BASE_ENGINE_PARAMS = {
    "DENSIFY_SPACING_FT":    10.0,
    "HIGH_RES_MODE":          True,   # use 1m REST API tiles
    "H_MIN_DELTA":            3.5,
    "H_MIN_CURVE_LENGTH_FT": 100.0,
    "V_MIN_CURVE_LENGTH_FT": 200.0,
    "V_MIN_GRADE_CHANGE":     0.5,
    "ENABLE_MERGE":           False,
    "MERGE_GAP_FT":          600.0,
    "V_MERGE_GAP_FT":       1500.0,
}

ALL_FIPS = [
    "01","02","04","05","06","08","09","10","11","12","13","15","16",
    "17","18","19","20","21","22","23","24","25","26","27","28","29",
    "30","31","32","33","34","35","36","37","38","39","40","41","42",
    "44","45","46","47","48","49","50","51","53","54","55","56","72"
]

MOUNTAIN_STATES = {
    "02","04","06","08","16","30","32","35","41","49","53","56",
    "13","21","23","33","36","37","42","47","50","51","54"
}

V_RMSE_THRESHOLDS               = {1:4.0, 2:4.5, 3:3.5, 4:3.5, 5:4.0, 6:4.0, 7:4.0}
MAX_H_RMSE_FT                   = {1: 3.5, 2: 3.5, 3: 3.8, 4: 3.8, 5: 5.0, 6: 5.5, 7: 5.5}
V_MIN_RMSE_RANGE_FT             = 0.05
MAX_V_DEV_FT                    = {1:35.0, 2:30.0, 3:15.0, 4:12.0, 5:15.0, 6:15.0, 7:15.0}
MAX_H_DEV_FT                    = {1: 15.0, 2: 15.0, 3: 22.0, 4: 22.0, 5: 15.0, 6: 15.0, 7: 20.0}
MAX_CURVE_VAR                   = 0.05
ELBOW_FLAT_THRESHOLD_FT         = 0.15
EARLY_EXIT_CONSECUTIVE_FAILS    = 3

SWEEP_FACTORS = [
    100, 200, 400, 600, 800, 1000, 1200, 1400, 1600, 1800,
    2000, 2500, 3000, 4000, 4500,
]

NATIONAL_DEFAULTS = {
    "H": {1: 400, 2: 200, 3: 400, 4: 400, 5: 200, 6: 200, 7: 400},
    "V": {1: 1400, 2: 1400, 3: 1400, 4: 1400, 5: 1000, 6: 1000, 7: 1000},
}

AUDIT_FIELDNAMES = [
    "timestamp","state_fips","f_sys","mode","is_mountain_state",
    "total_chunks","sample_chunks","v_rmse_ceiling","h_rmse_ceiling",
    "maxv_ceiling","maxh_ceiling","n_evaluated","n_passing",
    "passing_factors","rmse_rise","early_exit_factor","selection_method",
    "selected_factor","peak_elbow_distance","elbow_distance_profile",
    "v_rmse_at_baseline","h_rmse_at_baseline","maxv_at_baseline",
    "maxh_at_baseline","curvevar_at_baseline","v_rmse_at_selected",
    "h_rmse_at_selected","maxv_at_selected","maxh_at_selected",
    "curvevar_at_selected","std_v_rmse_at_selected","std_h_rmse_at_selected",
    "last_passing_factor","v_rmse_at_last_passing","h_rmse_at_last_passing",
    "maxv_at_last_passing","maxh_at_last_passing","national_default_factor",
    "deviation_from_default","ceiling_proximity_pct","confidence_score",
    "override_recommended",
]

# ===========================================================================
# BACKEND UTILITIES
# ===========================================================================
def get_dir_size_gb(directory: str) -> float:
    """Calculates the cumulative size of all files in a directory tree in GB."""
    total = 0
    for root, _, files in os.walk(directory):
        for f in files:
            fp = os.path.join(root, f)
            if os.path.exists(fp):
                total += os.path.getsize(fp)
    return total / (1024 ** 3)

def enforce_cache_ceiling(cache_dir: str, max_size_gb: float = 5.0) -> None:
    """Prunes least-recently-modified rasters to keep disk workspace bounded."""
    try:
        current_size = get_dir_size_gb(cache_dir)
        if current_size <= max_size_gb:
            return
        logging.info(f"LRU Cache ceiling reached ({current_size:.2f} GB). Evicting expired rasters...")
        tiles = [(f, os.path.getmtime(f))
                 for f in glob.glob(os.path.join(cache_dir, "*.tif")) if os.path.isfile(f)]
        tiles.sort(key=lambda x: x[1])
        for tile_path, _ in tiles:
            try:
                os.remove(tile_path)
            except OSError:
                continue
            if get_dir_size_gb(cache_dir) <= (max_size_gb * 0.9):
                break
        logging.info(f"Storage footprint compressed to: {get_dir_size_gb(cache_dir):.2f} GB")
    except Exception as exc:
        logging.error(f"Failed to complete disk pruning: {exc}")

def cochran_sample_size(population: int, confidence: float = 0.95,
                        margin_of_error: float = 0.05, p: float = 0.5) -> int:
    """Cochran's formula with finite population correction."""
    z = {0.90:1.645, 0.95:1.960, 0.99:2.576}.get(confidence, 1.960)
    q  = 1.0 - p
    n0 = (z**2 * p * q) / (margin_of_error**2)
    n  = n0 / (1 + (n0 - 1) / population)
    return min(math.ceil(n), population)

def fetch_socrata_state(state_fips: str, token: str = "") -> pd.DataFrame:
    """Pages through the Socrata API for all HPMS segments in a state."""
    headers = {"X-App-Token": token} if token else {}
    where   = f"stateid='{state_fips}' AND facility_type IN ('1', '2')"
    params  = {
        "$select": "route_id,begin_point,end_point,f_system,facility_type,line",
        "$limit":   100_000,
        "$offset":  0,
        "$where":   where,
    }
    rows, page = [], 1
    logging.info(f"Fetching Socrata segments for State FIPS {state_fips}...")
    while True:
        r = requests.get(SOCRATA_DEFAULT, params=params, headers=headers, timeout=120)
        r.raise_for_status()
        data = r.json()
        if not data:
            break
        rows.extend(data)
        logging.info(f"  [{state_fips}] Socrata page {page}: {len(data):,} rows "
                     f"({len(rows):,} total so far)")
        page += 1
        if len(data) < 100_000:
            break
        params["$offset"] += 100_000

    if not rows:
        raise ValueError(f"No Socrata rows found for FIPS {state_fips}.")
    logging.info(f"  [{state_fips}] Socrata fetch complete: {len(rows):,} rows in {page-1} page(s)")

    df = pd.DataFrame(rows)
    geom_col = next((c for c in df.columns if c.lower() in ["line","geometry","the_geom"]), None)
    if geom_col is None:
        raise ValueError("Geometry column not found in Socrata response.")

    df["WKT"] = df[geom_col].apply(lambda v: shape(v).wkt if isinstance(v, dict) else str(v))
    df.rename(columns={"route_id":"RouteId","begin_point":"Start_MP",
                        "end_point":"End_MP","f_system":"FSystem"}, inplace=True)
    df["Start_MP"] = pd.to_numeric(df["Start_MP"], errors="coerce").fillna(0.0)
    df["End_MP"]   = pd.to_numeric(df["End_MP"],   errors="coerce").fillna(0.0)
    df["FSystem"]  = pd.to_numeric(df["FSystem"],  errors="coerce").fillna(1).astype(int)
    return df[df["WKT"].notna() & (df["WKT"] != "")].copy()

def generate_1mile_chunks(wkt_list: list) -> list:
    """Stitches a route's WKT segments and slices into ~1-mile LineStrings.
    
    Includes complete 1-mile chunks plus any tail segment >= 0.5 miles (800m).
    This ensures the population for Cochran sampling is not biased toward longer
    routes and includes the full geometric diversity of the network.
    """
    lines  = stitch_linestrings_ordered(wkt_list)
    chunks = []
    for line in lines:
        if line.is_empty:
            continue
        lon, lat = line.coords[0]
        utm      = get_appropriate_utm_zone(lon, lat)
        fwd      = Transformer.from_crs("EPSG:4326", f"EPSG:{utm}", always_xy=True)
        coords_m = [fwd.transform(x, y) for x, y in line.coords]
        l_m      = LineString(coords_m).length
        if l_m < 1_600:
            continue
        
        # Generate complete 1-mile chunks
        num_full_chunks = int(l_m / 1_609.34)  # Floor: how many full miles fit
        for i in range(num_full_chunks):
            chunks.append(substring(line,
                                    (i * 1_609.34) / l_m,
                                    ((i + 1) * 1_609.34) / l_m,
                                    normalized=True))
        
        # Include tail segment if it's >= 0.5 miles (800 meters)
        # This prevents sampling bias toward longer routes by including
        # the partial mile at the end of each route.
        tail_length_m = l_m - (num_full_chunks * 1_609.34)
        MIN_TAIL_M = 800.0  # 0.5 miles minimum
        
        if tail_length_m >= MIN_TAIL_M:
            chunks.append(substring(line,
                                    (num_full_chunks * 1_609.34) / l_m,
                                    1.0,  # End of line
                                    normalized=True))
    
    return chunks

# ===========================================================================
# H SWEEP: IN-MEMORY TILE CACHE
# ===========================================================================
def _preload_chunk_for_h_sweep(args):
    """
    Called once per chunk before the H sweep begins.
    Returns a dict with raw coordinate arrays and the full in-memory tile band,
    or None if the chunk or tile cannot be loaded.
    """
    chunk_geom, demdir, base_params = args
    try:
        import rasterio

        lon, lat = chunk_geom.coords[0]
        utm = get_appropriate_utm_zone(lon, lat)
        fwd = Transformer.from_crs("EPSG:4326", f"EPSG:{utm}", always_xy=True)

        coords_m  = [fwd.transform(x, y) for x, y in chunk_geom.coords]
        coords_m  = densify_coords_line(coords_m, base_params["DENSIFY_SPACING_M"])
        min_pts   = max(base_params["H_BASE_SMOOTH_WINDOW"] + 2, 8)
        if len(coords_m) < min_pts:
            return None

        x_raw     = np.array([c[0] for c in coords_m])
        y_raw     = np.array([c[1] for c in coords_m])
        spacing_m = LineString(coords_m).length / (len(coords_m) - 1)
        d_axis    = np.arange(len(coords_m)) * spacing_m

        # Each chunk has its own tile named by its bbox
        minx_b, miny_b, maxx_b, maxy_b = chunk_geom.bounds
        expected_tile = os.path.join(demdir, f"tile_1m_{minx_b:.4f}_{miny_b:.4f}.tif")

        if not os.path.exists(expected_tile):
            tif_files = glob.glob(os.path.join(demdir, "tile_1m_*.tif"))
            if not tif_files:
                return None
            expected_tile = tif_files[0]

        with rasterio.open(expected_tile) as src:
            tile_data      = src.read(1)
            tile_transform = src.transform
            tile_nodata    = src.nodata

        return {
            "x_raw":          x_raw,
            "y_raw":          y_raw,
            "d_axis":         d_axis,
            "spacing_m":      spacing_m,
            "tile_data":      tile_data,
            "tile_transform": tile_transform,
            "tile_nodata":    tile_nodata,
            "utm_epsg":       utm,
        }
    except Exception:
        return None


def _h_metrics_one_chunk(args):
    """
    Computes H sweep metrics for one chunk at one factor using the pre-loaded
    in-memory tile cache. No disk I/O during the sweep.
    """
    cache_item, h_factor = args
    try:
        from rasterio.transform import rowcol as rc_rowcol

        d_axis         = cache_item["d_axis"]
        x_raw          = cache_item["x_raw"]
        y_raw          = cache_item["y_raw"]
        tile_data      = cache_item["tile_data"]
        tile_transform = cache_item["tile_transform"]
        tile_nodata    = cache_item["tile_nodata"]
        utm_epsg       = cache_item["utm_epsg"]

        # Reconstruct transformer locally -- pyproj Transformer objects are not
        # thread-safe when shared across workers
        rev = Transformer.from_crs(f"EPSG:{utm_epsg}", "EPSG:4326", always_xy=True)

        sx = UnivariateSpline(d_axis, x_raw, s=h_factor)
        sy = UnivariateSpline(d_axis, y_raw, s=h_factor)
        coords_m_smooth   = list(zip(sx(d_axis), sy(d_axis)))
        coords_wgs_smooth = [rev.transform(x, y) for x, y in coords_m_smooth]
        coords_wgs_raw    = [rev.transform(x, y) for x, y in zip(x_raw, y_raw)]

        def _sample(lon, lat):
            try:
                row, col = rc_rowcol(tile_transform, lon, lat)
                if 0 <= row < tile_data.shape[0] and 0 <= col < tile_data.shape[1]:
                    val = tile_data[row, col]
                    if (tile_nodata is None or val != tile_nodata) and val > -1000:
                        return float(val)
            except Exception:
                pass
            return np.nan

        z_smooth = np.array([_sample(lon, lat) for lon, lat in coords_wgs_smooth])
        z_raw    = np.array([_sample(lon, lat) for lon, lat in coords_wgs_raw])

        if np.sum(~np.isnan(z_smooth)) < 10 or np.sum(~np.isnan(z_raw)) < 10:
            return None

        lon_raw = np.array([pt[0] for pt in coords_wgs_raw])
        lat_raw = np.array([pt[1] for pt in coords_wgs_raw])
        lon_sm  = np.array([pt[0] for pt in coords_wgs_smooth])
        lat_sm  = np.array([pt[1] for pt in coords_wgs_smooth])

        lat_dev_ft = (lat_raw - lat_sm) * 364_000
        lon_dev_ft = (lon_raw - lon_sm) * (364_000 * np.cos(np.radians(lat_sm)))
        h_dev_ft   = np.sqrt(lat_dev_ft**2 + lon_dev_ft**2)

        headings  = np.degrees(np.arctan2(np.diff(lat_sm), np.diff(lon_sm)))
        curve_var = float(np.mean(np.degrees(np.abs(np.diff(np.unwrap(np.radians(headings)))))))

        v_dev_ft = np.abs(z_raw - z_smooth) * FEET_PER_METER
        v_valid  = v_dev_ft[~np.isnan(v_dev_ft)]
        h_valid  = h_dev_ft[~np.isnan(h_dev_ft)]

        return {
            "v_rmse":    float(np.sqrt(np.mean(v_valid**2))) if len(v_valid) else 0.0,
            "h_rmse":    float(np.sqrt(np.mean(h_valid**2))) if len(h_valid) else 0.0,
            "max_v_dev": float(np.max(v_valid))              if len(v_valid) else 0.0,
            "max_h_dev": float(np.max(h_valid))              if len(h_valid) else 0.0,
            "curve_var": curve_var,
        }
    except Exception as e:
        logging.warning(f"_h_metrics_one_chunk failed at factor {h_factor}: {e}")
        return None


# ===========================================================================
# SHARED METRIC AGGREGATION
# ===========================================================================
def _collate_raw_metrics(raw: list) -> Optional[dict]:
    """Aggregates per-chunk metric dicts into a single stats dict."""
    if len(raw) < 3:
        return None
    return {
        "V_RMSE":     float(np.mean([r["v_rmse"]    for r in raw])),
        "H_RMSE":     float(np.mean([r["h_rmse"]    for r in raw])),
        "Max_V_Dev":  float(np.percentile([r["max_v_dev"] for r in raw], 80)),
        "Max_H_Dev":  float(np.percentile([r["max_h_dev"] for r in raw], 80)),
        "Curve_Var":  float(np.mean([r["curve_var"] for r in raw])),
        "Std_V_RMSE": float(np.std([r["v_rmse"]     for r in raw])),
        "Std_H_RMSE": float(np.std([r["h_rmse"]     for r in raw])),
    }


def _aggregate_metrics_h(chunk_caches: list, h_factor: int,
                          sweep_workers: int) -> Optional[dict]:
    raw  = []
    args = [(c, h_factor) for c in chunk_caches if c is not None]
    with ThreadPoolExecutor(max_workers=sweep_workers) as ex:
        for res in ex.map(_h_metrics_one_chunk, args):
            if res:
                raw.append(res)
    return _collate_raw_metrics(raw)


def _aggregate_metrics_v(sweep_items: list, demdir: str,
                          test_params: dict, f_sys: int,
                          sweep_workers: int = 8) -> Optional[dict]:
    """V sweep aggregation - chunks are RAM-cached so threads are sufficient."""
    raw = []
    with ThreadPoolExecutor(max_workers=sweep_workers) as ex:
        futures = {ex.submit(_v_metrics_one_chunk, g, test_params): g
                   for g in sweep_items}
        for fut in as_completed(futures):
            res = fut.result()
            if res:
                raw.append(res)
    return _collate_raw_metrics(raw)


def _v_metrics_one_chunk(geom: Any, test_params: dict) -> Optional[dict]:
    """V sweep metrics for one cached chunk. No disk I/O."""
    try:
        if not (isinstance(geom, dict) and "z_fixed" in geom):
            return None

        res        = geom["res_base"].copy()
        s_factor_v = test_params.get("V_SMOOTH_FACTOR", 1000)

        if len(geom["z_fixed"]) != len(geom["d_axis"]):
            return None

        sz = UnivariateSpline(geom["d_axis"], geom["z_fixed"], s=s_factor_v)
        res["z_smooth"] = sz(geom["d_axis"])

        v_dev_ft = np.abs(res["z_raw"] - res["z_smooth"]) * FEET_PER_METER
        lon_raw  = np.array([pt[0] for pt in res["coords_wgs_raw"]])
        lat_raw  = np.array([pt[1] for pt in res["coords_wgs_raw"]])
        lon_sm   = np.array([pt[0] for pt in res["coords_wgs_smooth"]])
        lat_sm   = np.array([pt[1] for pt in res["coords_wgs_smooth"]])

        lat_dev_ft = (lat_raw - lat_sm) * 364_000
        lon_dev_ft = (lon_raw - lon_sm) * (364_000 * np.cos(np.radians(lat_sm)))
        h_dev_ft   = np.sqrt(lat_dev_ft**2 + lon_dev_ft**2)

        headings  = np.degrees(np.arctan2(np.diff(lat_sm), np.diff(lon_sm)))
        curve_var = float(np.mean(np.degrees(np.abs(np.diff(np.unwrap(np.radians(headings)))))))

        return {
            "v_rmse":    float(np.sqrt(np.mean(v_dev_ft**2))),
            "h_rmse":    float(np.sqrt(np.mean(h_dev_ft**2))),
            "max_v_dev": float(np.max(v_dev_ft)),
            "max_h_dev": float(np.max(h_dev_ft)),
            "curve_var": curve_var,
        }
    except Exception:
        return None


# ===========================================================================
# ELBOW, CEILING, SCORING
# ===========================================================================
def _find_elbow(factors: list, rmse_vals: list) -> Tuple[int, float, str]:
    """Kneedle elbow detection."""
    if len(factors) < 3:
        return factors[0], 0.0, ""
    x = np.array([float(f) for f in factors])
    y = np.array(rmse_vals, dtype=float)
    xr = x[-1] - x[0]
    yr = y[-1] - y[0]
    xn = (x - x[0]) / xr if xr else np.zeros_like(x)
    yn = (y - y[0]) / yr if yr else np.zeros_like(y)
    distances  = np.abs(yn - xn) / np.sqrt(2)
    elbow_idx  = int(np.argmax(distances))
    dist_profile = "  ".join(f"F{factors[i]}:{distances[i]:.4f}" for i in range(len(factors)))
    logging.info(f"    [elbow distances] {dist_profile}")
    return factors[elbow_idx], float(distances[elbow_idx]), dist_profile

def _eff_max_h_rmse(f_sys: int) -> float:
    """Returns the per-FS H RMSE ceiling."""
    return MAX_H_RMSE_FT.get(f_sys, 5.0) if isinstance(MAX_H_RMSE_FT, dict) else MAX_H_RMSE_FT

def _passes_ceiling(stats: dict, max_v_rmse: float, f_sys: int,
                    state_fips: str, mode: str) -> bool:
    v_pass = (stats["V_RMSE"]   <= max_v_rmse and
              stats["Max_V_Dev"] <= _eff_max_v_dev(f_sys, state_fips))
    h_pass = (stats["H_RMSE"]   <= _eff_max_h_rmse(f_sys) and
              stats["Max_H_Dev"] <= _eff_max_h_dev(f_sys, state_fips))
    return h_pass if mode == "H" else v_pass if mode == "V" else v_pass and h_pass

def _eff_max_v_dev(f_sys: int, state_fips: str) -> float:
    return MAX_V_DEV_FT.get(f_sys, 15.0) + (8.0 if state_fips in MOUNTAIN_STATES else 0.0)

def _eff_max_h_dev(f_sys: int, state_fips: str) -> float:
    return MAX_H_DEV_FT.get(f_sys, 15.0)

def _confidence_score(selection_method: str, n_passing: int,
                       rmse_rise: float, peak_elbow_distance: float) -> int:
    elbow_score  = min(50, int((peak_elbow_distance / 0.50) * 50))
    sample_score = min(30, int((min(n_passing, 10) / 10) * 30))
    curve_score  = min(20, int((min(rmse_rise, 1.0) / 1.0) * 20))
    raw = elbow_score + sample_score + curve_score
    if selection_method == "absolute_fallback":              return min(raw, 5)
    if selection_method == "composite_fallback":             return min(raw, 25)
    if selection_method in ("flat_curve", "highest_safe"):   return min(raw, 50)
    return raw

def _build_test_params(base_params: dict, h_factor: int, v_factor: int) -> dict:
    p = base_params.copy()
    p.update({"H_SMOOTH_FACTOR": h_factor, "V_SMOOTH_FACTOR": v_factor})
    for i in range(2, 8):
        p.update({f"H_SMOOTH_FACTOR_FS{i}": h_factor,
                  f"V_SMOOTH_FACTOR_FS{i}": v_factor})
    return p


# ===========================================================================
# AUDIT ROW
# ===========================================================================
def _build_audit_row(state_fips, f_sys, mode, is_mountain, total_chunks,
                     sample_chunks, max_v_rmse, cache, passing, valid_factors,
                     selection_method, selected_factor, peak_elbow_dist,
                     elbow_dist_profile, early_exit_factor, timestamp) -> dict:
    rmse_key    = "H_RMSE" if mode == "H" else "V_RMSE"
    ceiling_val = _eff_max_h_rmse(f_sys) if mode == "H" else max_v_rmse
    def sg(f, k): return round(cache[f][k], 4) if f in cache and cache[f] else ""

    baseline_f  = 100 if 100 in cache else (valid_factors[0] if valid_factors else None)
    last_pass_f = passing[-1] if passing else None
    nat_default = NATIONAL_DEFAULTS.get(mode, {}).get(f_sys, 400)
    sel_rmse    = sg(selected_factor, rmse_key)
    ceil_prox   = round((sel_rmse / ceiling_val) * 100, 1) if (sel_rmse != "" and ceiling_val) else ""
    rmse_rise   = (cache[passing[-1]][rmse_key] - cache[passing[0]][rmse_key]) if len(passing) >= 2 else 0.0

    return {
        "timestamp": timestamp, "state_fips": state_fips, "f_sys": f_sys,
        "mode": mode, "is_mountain_state": is_mountain,
        "total_chunks": total_chunks, "sample_chunks": sample_chunks,
        "v_rmse_ceiling": max_v_rmse, "h_rmse_ceiling": _eff_max_h_rmse(f_sys),
        "maxv_ceiling": _eff_max_v_dev(f_sys, state_fips),
        "maxh_ceiling": _eff_max_h_dev(f_sys, state_fips),
        "n_evaluated": len(valid_factors), "n_passing": len(passing),
        "passing_factors": "|".join(str(f) for f in passing),
        "rmse_rise": round(rmse_rise, 4),
        "early_exit_factor": early_exit_factor or "",
        "selection_method": selection_method,
        "selected_factor": selected_factor,
        "peak_elbow_distance": round(peak_elbow_dist, 4),
        "elbow_distance_profile": elbow_dist_profile,
        "v_rmse_at_baseline":    sg(baseline_f,      "V_RMSE"),
        "h_rmse_at_baseline":    sg(baseline_f,      "H_RMSE"),
        "maxv_at_baseline":      sg(baseline_f,      "Max_V_Dev"),
        "maxh_at_baseline":      sg(baseline_f,      "Max_H_Dev"),
        "curvevar_at_baseline":  sg(baseline_f,      "Curve_Var"),
        "v_rmse_at_selected":    sg(selected_factor, "V_RMSE"),
        "h_rmse_at_selected":    sg(selected_factor, "H_RMSE"),
        "maxv_at_selected":      sg(selected_factor, "Max_V_Dev"),
        "maxh_at_selected":      sg(selected_factor, "Max_H_Dev"),
        "curvevar_at_selected":  sg(selected_factor, "Curve_Var"),
        "std_v_rmse_at_selected":sg(selected_factor, "Std_V_RMSE"),
        "std_h_rmse_at_selected":sg(selected_factor, "Std_H_RMSE"),
        "last_passing_factor":      last_pass_f or "",
        "v_rmse_at_last_passing":   sg(last_pass_f, "V_RMSE")    if last_pass_f else "",
        "h_rmse_at_last_passing":   sg(last_pass_f, "H_RMSE")    if last_pass_f else "",
        "maxv_at_last_passing":     sg(last_pass_f, "Max_V_Dev") if last_pass_f else "",
        "maxh_at_last_passing":     sg(last_pass_f, "Max_H_Dev") if last_pass_f else "",
        "national_default_factor":  nat_default,
        "deviation_from_default":   round(selected_factor / nat_default, 3) if nat_default else "",
        "ceiling_proximity_pct":    ceil_prox,
        "confidence_score": _confidence_score(selection_method, len(passing),
                                               rmse_rise, peak_elbow_dist),
        "override_recommended": selection_method not in ["elbow", "flat_curve"],
    }


# ===========================================================================
# OPTIMAL FACTOR FINDER
# ===========================================================================
def _find_optimal_factor(chunks, chunk_caches, demdir, base_params, f_sys,
                          mode, max_v_rmse, fixed_other, state_fips,
                          total_chunks, sample_chunks, timestamp,
                          sweep_workers) -> Tuple[int, dict]:
    """
    Runs the full factor sweep for one mode (H or V).

    H mode: uses in-memory tile caches + ThreadPoolExecutor
    V mode: uses RAM-spline cache + ThreadPoolExecutor (already near-instant)

    Early exit: once EARLY_EXIT_CONSECUTIVE_FAILS factors have failed in a row
    AND RMSE is still rising, the elbow is confirmed to be in the passing range
    and the remaining factors are skipped.
    """
    cache    = {}
    rmse_key = "H_RMSE" if mode == "H" else "V_RMSE"
    peak_elbow_dist, elbow_dist_profile = 0.0, ""

    # ---- V mode: build RAM-spline cache ----
    sweep_items = chunks
    if mode == "V":
        logging.info(f"  [{state_fips}] FS {f_sys} [V] Building RAM-spline cache "
                     f"for {len(chunks)} chunks...")
        sweep_items   = []
        base_params_v = _build_test_params(base_params, fixed_other, 1000)
        for c in chunks:
            res = smooth_plan_profile_from_linestring(c, demdir, base_params_v, f_sys)
            if res:
                z_filled = pd.Series(res["z_raw"]).interpolate(
                    limit_direction="both").to_numpy()
                z_fixed, _ = fix_profile_by_deviation(
                    z_filled, res["spacing_m"], base_params_v, structure_mask=None)
                d_axis = res["d_axis"]
                if len(z_fixed) != len(d_axis):
                    logging.warning(f"  [{state_fips}] FS {f_sys} [V] Skipping chunk: "
                                    f"z_fixed len {len(z_fixed)} != d_axis len {len(d_axis)}")
                    continue
                sweep_items.append({"res_base": res, "z_fixed": z_fixed, "d_axis": d_axis})
        logging.info(f"  [{state_fips}] FS {f_sys} [V] RAM-spline cache ready: "
                     f"{len(sweep_items)}/{len(chunks)} chunks cached successfully")
        if len(sweep_items) < 3:
            sweep_items = chunks

    logging.info(f"  [{state_fips}] FS {f_sys} [{mode}] Starting factor sweep "
                 f"({len(SWEEP_FACTORS)} factors, "
                 f"{len(chunk_caches) if mode == 'H' else len(sweep_items)} chunks, "
                 f"{sweep_workers} workers)...")

    last_passing        = None
    consecutive_fails   = 0
    prev_rmse           = None
    did_early_exit      = False

    for factor_idx, factor in enumerate(SWEEP_FACTORS, 1):
        h = factor if mode == "H" else fixed_other
        v = factor if mode == "V" else fixed_other

        if mode == "H":
            stats = _aggregate_metrics_h(chunk_caches, h, sweep_workers)
        else:
            test_params = _build_test_params(base_params, h, v)
            stats = _aggregate_metrics_v(sweep_items, demdir, test_params,
                                          f_sys, sweep_workers)

        cache[factor] = stats

        if stats:
            passes   = _passes_ceiling(stats, max_v_rmse, f_sys, state_fips, mode)
            cur_rmse = stats[rmse_key]
            dev_key  = "Max_V_Dev" if mode == "V" else "Max_H_Dev"
            status   = "PASS" if passes else "FAIL"

            logging.info(
                f"  [{state_fips}] FS {f_sys} [{mode}] "
                f"Factor {factor_idx:2d}/{len(SWEEP_FACTORS)} "
                f"f={factor:4d}  {rmse_key}={cur_rmse:.3f}  "
                f"MaxDev={stats[dev_key]:.2f}  [{status}]"
            )

            if passes:
                last_passing      = factor
                consecutive_fails = 0
            else:
                # Count consecutive failures only when RMSE is still rising --
                # a flat or falling RMSE after a failure would be unusual and
                # worth continuing to investigate
                rmse_rising = (prev_rmse is None or cur_rmse >= prev_rmse)
                if rmse_rising:
                    consecutive_fails += 1
                else:
                    consecutive_fails = 0

            prev_rmse = cur_rmse

            # Early exit: elbow region is confirmed once we have seen enough
            # passing factors to run elbow detection AND enough consecutive
            # failures to know RMSE won't improve further
            if (last_passing is not None and
                    consecutive_fails >= EARLY_EXIT_CONSECUTIVE_FAILS):
                logging.info(
                    f"  [{state_fips}] FS {f_sys} [{mode}] "
                    f"Early exit after factor {factor_idx}/{len(SWEEP_FACTORS)} "
                    f"({consecutive_fails} consecutive failures with rising RMSE -- "
                    f"elbow region confirmed)"
                )
                did_early_exit = True
                break
        else:
            logging.info(
                f"  [{state_fips}] FS {f_sys} [{mode}] "
                f"Factor {factor_idx:2d}/{len(SWEEP_FACTORS)} "
                f"f={factor:4d}  [NO DATA]"
            )

    early_exit_factor = last_passing
    passing       = [f for f in SWEEP_FACTORS
                     if cache.get(f) and _passes_ceiling(
                         cache[f], max_v_rmse, f_sys, state_fips, mode)]
    valid_factors = [f for f in SWEEP_FACTORS if cache.get(f) is not None]

    # ---- V flat-terrain detection ----
    # If the V RMSE range across the entire sweep is below V_MIN_RMSE_RANGE_FT,
    # the terrain is too flat to produce a meaningful elbow. Any factor would
    # give essentially the same output, so use the national default rather than
    # a spurious elbow selection driven by floating-point noise.
    if mode == "V" and valid_factors:
        v_rmse_vals = [cache[f]["V_RMSE"] for f in valid_factors if cache[f] is not None]
        v_rmse_range = max(v_rmse_vals) - min(v_rmse_vals) if v_rmse_vals else 0.0
        if v_rmse_range < V_MIN_RMSE_RANGE_FT:
            nat_default_v = NATIONAL_DEFAULTS.get("V", {}).get(f_sys, 1000)
            logging.info(
                f"  [{state_fips}] FS {f_sys} [V] Flat terrain detected "
                f"(V_RMSE range = {v_rmse_range:.4f} ft < {V_MIN_RMSE_RANGE_FT} ft threshold) -- "
                f"using national default factor {nat_default_v} instead of elbow selection"
            )
            audit_row = _build_audit_row(
                state_fips, f_sys, mode, state_fips in MOUNTAIN_STATES,
                total_chunks, sample_chunks, max_v_rmse, cache, passing,
                valid_factors, "flat_terrain_default", nat_default_v, 0.0, "",
                early_exit_factor, timestamp)
            return nat_default_v, audit_row

    if len(passing) >= 3:
        rmse_vals = [cache[f][rmse_key] for f in passing]
        best, peak_elbow_dist, elbow_dist_profile = _find_elbow(passing, rmse_vals)
        selection_method = "elbow"
    else:
        best, selection_method = ((passing[-1], "highest_safe") if passing
                                  else (100, "composite_fallback"))

    logging.info(
        f"  [{state_fips}] FS {f_sys} [{mode}] Sweep complete: "
        f"{len(passing)}/{len(valid_factors)} factors passed  -->  "
        f"selected factor = {best}  (method: {selection_method})"
    )

    audit_row = _build_audit_row(
        state_fips, f_sys, mode, state_fips in MOUNTAIN_STATES,
        total_chunks, sample_chunks, max_v_rmse, cache, passing,
        valid_factors, selection_method, best, peak_elbow_dist,
        elbow_dist_profile, early_exit_factor, timestamp)
    return best, audit_row


# ===========================================================================
# MULTIPROCESSING ORCHESTRATION
# ===========================================================================
_write_lock, _network_lock, _sweep_workers = None, None, 8

def _pool_init(lock, n_lock, sweep_workers):
    global _write_lock, _network_lock, _sweep_workers
    _write_lock, _network_lock, _sweep_workers = lock, n_lock, sweep_workers

def process_state(state_fips: str, base_params: dict, master_json_path: str,
                  audit_csv_path: str, dem_dir: str, socrata_token: str,
                  sweep_workers: int = 8) -> Tuple[str, dict, list]:

    logging.info(f"\n{'='*60}\n=== CALIBRATION ENGINE ROUTING: STATE FIPS {state_fips} ==="
                 f"\n{'='*60}")
    run_timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    try:
        df = fetch_socrata_state(state_fips, socrata_token)
    except Exception as e:
        logging.error(f"[{state_fips}] Socrata fetch failed: {e}")
        return state_fips, {}, []

    worker_dem_dir = tempfile.mkdtemp(prefix=f"dem_{state_fips}_", dir=dem_dir)

    try:
        state_results, state_audit = {}, []

        for f_sys in range(1, 8):
            sub = df[df["FSystem"] == f_sys]
            if sub.empty:
                continue

            logging.info(f"[{state_fips}] FS {f_sys}: Slicing geometry into 1-mile traces...")
            all_chunks = []
            for _, route_group in sub.groupby("RouteId"):
                all_chunks.extend(
                    generate_1mile_chunks(
                        route_group.sort_values("Start_MP")["WKT"].tolist()))

            total_chunks = len(all_chunks)
            if not all_chunks:
                continue

            sample_size    = cochran_sample_size(total_chunks)
            sampled_chunks = random.sample(all_chunks, sample_size)

            # ---- Parallel tile downloads ----
            with _network_lock:
                logging.info(f"[{state_fips}] Downloading 1m DEM tiles "
                             f"for {sample_size} sample chunks...")

                def _download_one(args):
                    idx, geom = args
                    try:
                        download_high_res_dem_tile(geom.bounds, worker_dem_dir)
                        return idx, True
                    except Exception as e:
                        logging.warning(f"[{state_fips}] Tile failed for chunk {idx}: {e}")
                        return idx, False

                downloaded, failed, completed = 0, 0, 0
                with ThreadPoolExecutor(max_workers=6) as dl_ex:
                    futures = {dl_ex.submit(_download_one, (idx, g)): idx
                               for idx, g in enumerate(sampled_chunks, 1)}
                    for fut in as_completed(futures):
                        idx, ok = fut.result()
                        completed += 1
                        if ok: downloaded += 1
                        else:  failed    += 1
                        if completed % 25 == 0 or completed == sample_size:
                            logging.info(
                                f"  [{state_fips}] DEM tiles: {completed}/{sample_size} "
                                f"completed ({downloaded} ok, {failed} failed)")

                logging.info(f"  [{state_fips}] DEM sync complete: "
                             f"{downloaded} downloaded, {failed} failed")

            # ---- Pre-load H sweep tile caches ----
            logging.info(f"[{state_fips}] FS {f_sys}: Pre-loading in-memory tile caches "
                         f"for H sweep ({sample_size} chunks)...")
            preload_args = [(g, worker_dem_dir, base_params) for g in sampled_chunks]
            chunk_caches = []
            with ThreadPoolExecutor(max_workers=min(8, sweep_workers)) as pl_ex:
                for cache_item in pl_ex.map(_preload_chunk_for_h_sweep, preload_args):
                    chunk_caches.append(cache_item)
            valid_caches = sum(1 for c in chunk_caches if c is not None)
            logging.info(f"  [{state_fips}] FS {f_sys}: {valid_caches}/{sample_size} "
                         f"chunks pre-loaded into memory")

            # ---- Run sweeps ----
            max_v_rmse = V_RMSE_THRESHOLDS.get(f_sys, 3.5)
            logging.info(f"[{state_fips}] FS {f_sys}: Commencing parameter sweeps...")

            best_h, h_audit = _find_optimal_factor(
                sampled_chunks, chunk_caches, worker_dem_dir, base_params,
                f_sys, "H", max_v_rmse, 1000, state_fips,
                total_chunks, sample_size, run_timestamp, sweep_workers)

            best_v, v_audit = _find_optimal_factor(
                sampled_chunks, chunk_caches, worker_dem_dir, base_params,
                f_sys, "V", max_v_rmse, best_h, state_fips,
                total_chunks, sample_size, run_timestamp, sweep_workers)

            state_audit.extend([h_audit, v_audit])
            suffix = "" if f_sys == 1 else f"_FS{f_sys}"
            state_results.update({
                f"H_SMOOTH_FACTOR{suffix}": best_h,
                f"V_SMOOTH_FACTOR{suffix}": best_v,
            })

        # ---- Write outputs ----
        with _write_lock:
            master_data = {}
            if os.path.exists(master_json_path):
                with open(master_json_path, "r") as f:
                    try: master_data = json.load(f)
                    except json.JSONDecodeError: pass
            master_data[state_fips] = state_results
            with open(master_json_path, "w") as f:
                json.dump(master_data, f, indent=4)

            existing_rows = []
            if os.path.exists(audit_csv_path):
                with open(audit_csv_path, newline="", encoding="utf-8") as f:
                    existing_rows = list(csv.DictReader(f))
            existing_rows.extend(state_audit)
            with open(audit_csv_path, "w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=AUDIT_FIELDNAMES,
                                        extrasaction="ignore")
                writer.writeheader()
                writer.writerows(existing_rows)

            enforce_cache_ceiling(dem_dir, max_size_gb=5.0)

    finally:
        try:
            shutil.rmtree(worker_dem_dir, ignore_errors=True)
        except Exception:
            pass

    return state_fips, state_results, state_audit


# ===========================================================================
# ENTRY POINT
# ===========================================================================
def main():
    parser = argparse.ArgumentParser(description="RAT National Calibration Engine")
    parser.add_argument("--outdir",          required=True,
                        help="Output folder for results")
    parser.add_argument("--demdir",          required=True,
                        help="Shared DEM tile cache directory")
    parser.add_argument("--state",           default="ALL",
                        help="2-digit state FIPS code or ALL")
    parser.add_argument("--total-cores",     type=int, default=os.cpu_count(),
                        help="Total logical cores available (default: all detected)")
    parser.add_argument("--reserved-cores",  type=int, default=2,
                        help="Cores to hold back for OS/other work (default: 2)")
    args = parser.parse_args()

    os.makedirs(args.outdir, exist_ok=True)
    os.makedirs(args.demdir, exist_ok=True)

    available_cores = max(1, args.total_cores - args.reserved_cores)
    states_list     = ALL_FIPS if args.state.upper() == "ALL" else [args.state.zfill(2)]
    base_params     = build_params(BASE_ENGINE_PARAMS)

    if len(states_list) == 1:
        n_state_workers = 1
        n_sweep_workers = available_cores
    else:
        n_state_workers = max(1, available_cores // 5)
        n_sweep_workers = max(1, available_cores // n_state_workers)

    logging.info(f"Core allocation: {available_cores} available  -->  "
                 f"{n_state_workers} state worker(s) × "
                 f"{n_sweep_workers} sweep workers each")

    suite_root       = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    master_json_path = os.path.join(suite_root, "core", "national_smoothing_factors.json")
    audit_csv_path   = os.path.join(suite_root, "core", "calibration_audit.csv")

    _manager     = multiprocessing.Manager()
    write_lock   = _manager.Lock()
    network_lock = _manager.Lock()

    with ProcessPoolExecutor(
        max_workers=n_state_workers,
        initializer=_pool_init,
        initargs=(write_lock, network_lock, n_sweep_workers)
    ) as executor:
        futures = {
            executor.submit(
                process_state, fips, base_params, master_json_path,
                audit_csv_path, args.demdir, SOCRATA_TOKEN, n_sweep_workers
            ): fips for fips in states_list
        }
        for fut in as_completed(futures):
            fips = futures[fut]
            try:
                sf, _, _ = fut.result()
                logging.info(f"[{sf}] Calibration complete.")
            except Exception as e:
                logging.error(f"[{fips}] State worker failed: {e}")


if __name__ == "__main__":
    main()
