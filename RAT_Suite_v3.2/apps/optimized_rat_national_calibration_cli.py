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

"""
RAT NATIONAL CALIBRATION ENGINE v3.2
--------------------------------------------------------------------------------
ROLE: Automated national smoothing factor estimator.
DESCRIPTION:
Downloads HPMS data, extracts samples for each Functional System,
and finds optimal smoothing factors via a full sweep + elbow detection.
Outputs a master JSON dictionary mapped by State FIPS, and a detailed
audit CSV for manual review and model fine-tuning.

CHANGES FROM v3.1:
  - Added detailed per-search audit CSV output (calibration_audit.csv).

    The CSV contains one row per functional system per mode (H/V) per state,
    capturing everything needed to identify weak results, recommend manual
    overrides, and tune thresholds for future runs. New columns include:

    CONTEXT
      state_fips, f_sys, mode, timestamp
      is_mountain_state
      total_chunks, sample_chunks
      v_rmse_ceiling, h_rmse_ceiling, maxv_ceiling, maxh_ceiling

    SWEEP RESULTS
      n_evaluated          -- how many factors were actually evaluated
      n_passing            -- how many passed Gate 1
      passing_factors      -- comma-separated list of passing factor values
      rmse_rise            -- total RMSE rise across passing factors (ft)
      early_exit_factor    -- factor at which early exit triggered (if any)

    SELECTION
      selection_method     -- elbow | flat_curve | highest_safe | composite_fallback | absolute_fallback
      selected_factor
      peak_elbow_distance  -- 0.0 if elbow didn't run
      elbow_distance_profile -- full per-factor distance string

    METRICS AT KEY POINTS
      *_at_baseline        -- metrics at factor 100 (lowest evaluated)
      *_at_selected        -- metrics at the selected factor
      *_at_last_passing    -- metrics at the highest passing factor

    INTER-CHUNK VARIANCE (new -- measures sample reliability)
      std_v_rmse_at_selected, std_h_rmse_at_selected
      A high std relative to the mean indicates the sample is noisy and
      the aggregate metrics may not be representative.

    QUALITY INDICATORS (derived)
      national_default_factor  -- recommended national default for this FS/mode
      deviation_from_default   -- selected / default (ratio; >2.0 or <0.5 = outlier)
      ceiling_proximity_pct    -- selected RMSE as % of ceiling (>90% = marginal)
      confidence_score         -- 0-100 composite quality score
      override_recommended     -- True when confidence < 40 or fallback was used

  - _aggregate_metrics now also returns Std_V_RMSE and Std_H_RMSE.
  - _find_optimal_factor now returns a detailed audit dict alongside the factor.
  - find_optimal_factors collects audit dicts and passes them to main().
  - main() accumulates audit rows and writes calibration_audit.csv on completion.
"""

import os
import sys
import csv
import json
import math
import random
import logging
import argparse
import requests
import numpy as np
import pandas as pd
from datetime import datetime
import multiprocessing
from concurrent.futures import ThreadPoolExecutor, ProcessPoolExecutor, as_completed
from shapely.geometry import LineString
from shapely.wkt import loads
from shapely.ops import substring
from pyproj import Transformer

# --- Path bootstrap for core import ---
THIS_DIR = os.path.dirname(os.path.abspath(__file__))
RAT_SUITE_DIR = os.path.dirname(THIS_DIR)
if RAT_SUITE_DIR not in sys.path:
    sys.path.insert(0, RAT_SUITE_DIR)

from core.rat_core import (
    build_params,
    stitch_linestrings_ordered,
    smooth_plan_profile_from_linestring,
    download_dems,
    FEET_PER_METER,
    get_appropriate_utm_zone,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s: %(message)s")
SOCRATA_DEFAULT = "https://datahub.transportation.gov/resource/42um-tgh5.json"


# ===========================================================================
# CONSTANTS & DEFAULTS
# ===========================================================================
SOCRATA_DEFAULT = "https://datahub.transportation.gov/resource/42um-tgh5.json"

MAX_PARALLEL_STATES = max(1, __import__("os").cpu_count() - 2)  # Tune as needed

BASE_ENGINE_PARAMS = {
    "DENSIFY_SPACING_FT":    10.0,
    "H_MIN_DELTA":            3.5,
    "H_MIN_CURVE_LENGTH_FT": 100.0,
    "V_MIN_CURVE_LENGTH_FT": 200.0,
    "V_MIN_GRADE_CHANGE":     0.5,
    "ENABLE_MERGE":           False,
    "MERGE_GAP_FT":          600.0,
    "V_MERGE_GAP_FT":       1500.0,
}

# ===========================================================================
# STATE LISTS
# ===========================================================================

ALL_FIPS = [
    "01", "02", "04", "05", "06", "08", "09", "10", "11", "12", "13", "15", "16",
    "17", "18", "19", "20", "21", "22", "23", "24", "25", "26", "27", "28", "29",
    "30", "31", "32", "33", "34", "35", "36", "37", "38", "39", "40", "41", "42",
    "44", "45", "46", "47", "48", "49", "50", "51", "53", "54", "55", "56", "72"
]

MOUNTAIN_STATES = [
    "02", "04", "06", "08", "16", "30", "32", "35", "41", "49", "53", "56",  # Rocky Mountain
    "13", "21", "23", "33", "36", "37", "42", "47", "50", "51", "54",         # Appalachian
]

# ===========================================================================
# CALIBRATION THRESHOLDS
# ===========================================================================

V_RMSE_THRESHOLDS = {
    1: 4.0,
    2: 4.5,
    3: 3.5,
    4: 3.5,
    5: 4.0,
    6: 4.0,
    7: 4.0,
}
MAX_H_RMSE_FT = 3.5
MAX_V_DEV_FT  = {1: 35.0, 2: 30.0, 3: 15.0, 4: 12.0, 5: 15.0, 6: 15.0, 7: 15.0}
MAX_H_DEV_FT  = {1: 15.0, 2: 15.0, 3: 12.0, 4: 12.0, 5: 15.0, 6: 15.0, 7: 15.0}
MAX_CURVE_VAR = 0.05

ELBOW_FLAT_THRESHOLD_FT = 0.15

SWEEP_FACTORS = [
    100, 200, 400, 600, 800, 1000, 1200, 1400, 1600, 1800,
    2000, 2500, 3000, 4000, 4500,
]

# ---------------------------------------------------------------------------
# National default factors used for deviation calculation in the audit CSV.
# These represent the recommended fallback values derived from the full
# national sweep and should be updated if the defaults are revised.
# ---------------------------------------------------------------------------
NATIONAL_DEFAULTS = {
    "H": {1: 400, 2: 200, 3: 200, 4: 200, 5: 200, 6: 200, 7: 200},
    "V": {1: 1000, 2: 1000, 3: 1200, 4: 400, 5: 600, 6: 400, 7: 400},
}

# ===========================================================================
# AUDIT CSV SCHEMA
# ===========================================================================

AUDIT_FIELDNAMES = [
    # Context
    "timestamp", "state_fips", "f_sys", "mode",
    "is_mountain_state",
    "total_chunks", "sample_chunks",
    "v_rmse_ceiling", "h_rmse_ceiling", "maxv_ceiling", "maxh_ceiling",
    # Sweep results
    "n_evaluated", "n_passing",
    "passing_factors", "rmse_rise",
    "early_exit_factor",
    # Selection
    "selection_method", "selected_factor",
    "peak_elbow_distance", "elbow_distance_profile",
    # Metrics at baseline (factor 100)
    "v_rmse_at_baseline", "h_rmse_at_baseline",
    "maxv_at_baseline", "maxh_at_baseline", "curvevar_at_baseline",
    # Metrics at selected factor
    "v_rmse_at_selected", "h_rmse_at_selected",
    "maxv_at_selected", "maxh_at_selected", "curvevar_at_selected",
    "std_v_rmse_at_selected", "std_h_rmse_at_selected",
    # Metrics at last passing factor
    "last_passing_factor",
    "v_rmse_at_last_passing", "h_rmse_at_last_passing",
    "maxv_at_last_passing", "maxh_at_last_passing",
    # Quality indicators
    "national_default_factor", "deviation_from_default",
    "ceiling_proximity_pct", "confidence_score", "override_recommended",
]


# ===========================================================================
# STATISTICAL SAMPLE SIZING
# ===========================================================================

Z_SCORES = {0.90: 1.645, 0.95: 1.960, 0.99: 2.576}

def cochran_sample_size(population, confidence=0.95, margin_of_error=0.05, p=0.5):
    z  = Z_SCORES.get(confidence, 1.960)
    q  = 1.0 - p
    n0 = (z**2 * p * q) / (margin_of_error**2)
    n  = n0 / (1 + (n0 - 1) / population)
    return min(math.ceil(n), population)


# ===========================================================================
# SOCRATA FETCH
# ===========================================================================

def fetch_socrata_state(state_fips: str, token: str = "") -> pd.DataFrame:
    headers      = {"X-App-Token": token} if token else {}
    where_clause = f"stateid='{state_fips}' AND facility_type IN ('1', '2')"
    params       = {"$limit": 100_000, "$offset": 0, "$where": where_clause}

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

    df       = pd.DataFrame(rows)
    geom_col = next((c for c in df.columns if c.lower() in ["line", "geometry", "the_geom"]), None)

    if geom_col is None:
        raise ValueError("Could not identify geometry column in Socrata response.")

    def geom_to_wkt(v):
        if isinstance(v, dict):
            from shapely.geometry import shape
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
    }
    df.rename(columns=col_map, inplace=True)

    df["Start_MP"] = pd.to_numeric(df["Start_MP"], errors="coerce").fillna(0.0)
    df["End_MP"]   = pd.to_numeric(df["End_MP"],   errors="coerce").fillna(0.0)
    df["FSystem"]  = pd.to_numeric(df["FSystem"],  errors="coerce").fillna(1).astype(int)

    df = df[df["WKT"].notna() & (df["WKT"] != "")].copy()
    return df


# ===========================================================================
# GEOMETRY HELPERS
# ===========================================================================

def generate_1mile_chunks(wkt_list: list) -> list:
    """Stitches a route's WKT segments and slices them into ~1-mile LineStrings."""
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
        num_miles = int(l_m / 1_609.34)
        for i in range(num_miles):
            start_frac = (i       * 1_609.34) / l_m
            end_frac   = ((i + 1) * 1_609.34) / l_m
            sub_geom   = substring(line, start_frac, end_frac, normalized=True)
            chunks.append(sub_geom)
    return chunks


# ===========================================================================
# PER-CHUNK METRIC COMPUTATION
# ===========================================================================

def _metrics_for_chunk(geom, demdir: str, test_params: dict, f_sys: int) -> dict | None:
    """Runs the smoothing pipeline on one chunk and returns deviation statistics."""
    res = smooth_plan_profile_from_linestring(geom, demdir, test_params, f_sys)
    if not res:
        return None
    try:
        v_dev_ft = np.abs(res["z_raw"] - res["z_smooth"]) * FEET_PER_METER

        lon_raw = np.array([pt[0] for pt in res["coords_wgs_raw"]])
        lat_raw = np.array([pt[1] for pt in res["coords_wgs_raw"]])
        lon_sm  = np.array([pt[0] for pt in res["coords_wgs_smooth"]])
        lat_sm  = np.array([pt[1] for pt in res["coords_wgs_smooth"]])

        lat_dev_ft = (lat_raw - lat_sm) * 364_000
        lon_dev_ft = (lon_raw - lon_sm) * (364_000 * np.cos(np.radians(lat_sm)))
        h_dev_ft   = np.sqrt(lat_dev_ft**2 + lon_dev_ft**2)

        d_lon      = np.diff(lon_sm)
        d_lat      = np.diff(lat_sm)
        headings   = np.degrees(np.arctan2(d_lat, d_lon))
        heading_ch = np.abs(np.diff(np.unwrap(np.radians(headings))))
        curve_var  = float(np.mean(np.degrees(heading_ch)))

        return {
            "v_rmse":    float(np.sqrt(np.mean(v_dev_ft**2))),
            "h_rmse":    float(np.sqrt(np.mean(h_dev_ft**2))),
            "max_v_dev": float(np.max(v_dev_ft)),
            "max_h_dev": float(np.max(h_dev_ft)),
            "curve_var": curve_var,
        }
    except Exception:
        return None


def _build_test_params(base_params: dict, h_factor: int, v_factor: int) -> dict:
    """Injects H/V smoothing factors into a copy of the base parameter dict."""
    p = base_params.copy()
    p["H_SMOOTH_FACTOR"] = h_factor
    p["V_SMOOTH_FACTOR"] = v_factor
    for i in range(2, 8):
        p[f"H_SMOOTH_FACTOR_FS{i}"] = h_factor
        p[f"V_SMOOTH_FACTOR_FS{i}"] = v_factor
    return p


def _aggregate_metrics(
    chunks: list, demdir: str, test_params: dict, f_sys: int, max_workers: int = 8
) -> dict | None:
    """
    Runs _metrics_for_chunk in parallel and returns aggregate statistics.
    Now includes Std_V_RMSE and Std_H_RMSE to capture inter-chunk variance,
    which indicates how reliable the aggregate means are as calibration signals.
    """
    raw: list[dict] = []
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = {ex.submit(_metrics_for_chunk, g, demdir, test_params, f_sys): g for g in chunks}
        for fut in as_completed(futures):
            result = fut.result()
            if result:
                raw.append(result)

    if len(raw) < 3:
        return None

    v_rmses = [r["v_rmse"] for r in raw]
    h_rmses = [r["h_rmse"] for r in raw]

    return {
        "V_RMSE":      float(np.mean(v_rmses)),
        "H_RMSE":      float(np.mean(h_rmses)),
        "Max_V_Dev":   float(np.percentile([r["max_v_dev"] for r in raw], 80)),
        "Max_H_Dev":   float(np.percentile([r["max_h_dev"] for r in raw], 80)),
        "Curve_Var":   float(np.mean([r["curve_var"] for r in raw])),
        "Std_V_RMSE":  float(np.std(v_rmses)),
        "Std_H_RMSE":  float(np.std(h_rmses)),
    }


# ===========================================================================
# THRESHOLD HELPERS
# ===========================================================================

def _effective_max_v_dev(f_sys: int, state_fips: str) -> float:
    base = MAX_V_DEV_FT.get(f_sys, 15.0)
    return base + 8.0 if state_fips in MOUNTAIN_STATES else base

def _effective_max_h_dev(f_sys: int, state_fips: str) -> float:
    return MAX_H_DEV_FT.get(f_sys, 15.0)

def _passes_ceiling(stats: dict, max_v_rmse: float, f_sys: int, state_fips: str, mode: str) -> bool:
    """Gate 1: All four hard deviation metrics must stay under their ceilings."""
    v_pass = (
        stats["V_RMSE"]    <= max_v_rmse
        and stats["Max_V_Dev"] <= _effective_max_v_dev(f_sys, state_fips)
    )
    h_pass = (
        stats["H_RMSE"]    <= MAX_H_RMSE_FT
        and stats["Max_H_Dev"] <= _effective_max_h_dev(f_sys, state_fips)
    )
    if mode == "H":   return h_pass
    if mode == "V":   return v_pass
    return v_pass and h_pass

def _composite_score(stats: dict, max_v_rmse: float, f_sys: int, state_fips: str) -> float:
    """Fallback scoring used only when no factor passes Gate 1."""
    curve_score     = (stats["Curve_Var"] / MAX_CURVE_VAR) * 100.0
    v_drift_penalty = (stats["V_RMSE"] / max_v_rmse) ** 2 * 20.0
    h_drift_penalty = (stats["H_RMSE"] / MAX_H_RMSE_FT) ** 2 * 20.0

    penalty = 0.0
    if stats["V_RMSE"]    > max_v_rmse:                               penalty += 500 * (stats["V_RMSE"]    - max_v_rmse)
    if stats["H_RMSE"]    > MAX_H_RMSE_FT:                           penalty += 500 * (stats["H_RMSE"]    - MAX_H_RMSE_FT)
    if stats["Max_V_Dev"] > _effective_max_v_dev(f_sys, state_fips): penalty += 500 * (stats["Max_V_Dev"] - _effective_max_v_dev(f_sys, state_fips))
    if stats["Max_H_Dev"] > _effective_max_h_dev(f_sys, state_fips): penalty += 500 * (stats["Max_H_Dev"] - _effective_max_h_dev(f_sys, state_fips))

    return curve_score + v_drift_penalty + h_drift_penalty + penalty


# ===========================================================================
# QUALITY SCORING  (for audit CSV)
# ===========================================================================

def _confidence_score(
    selection_method: str,
    n_passing: int,
    rmse_rise: float,
    peak_elbow_distance: float,
) -> int:
    """
    Composite 0-100 confidence score for a calibration result.

    Components:
      - Elbow sharpness (50 pts): how clearly defined the diminishing-returns
        point is. A peak distance of 0.50 is considered fully confident.
      - Sample richness  (30 pts): how many factors passed Gate 1.
        10+ passing factors = full confidence; 1 = minimal.
      - Curve meaningfulness (20 pts): how much the RMSE actually changes
        across the safe range. A 1.0 ft rise = full confidence.

    Fallback methods incur a hard penalty regardless of other scores:
      - composite_fallback: capped at 25
      - absolute_fallback:  capped at 5
      - flat_curve / highest_safe: capped at 50 (safe but limited data)
    """
    elbow_score   = min(50, int((peak_elbow_distance / 0.50) * 50))
    sample_score  = min(30, int((min(n_passing, 10) / 10) * 30))
    curve_score   = min(20, int((min(rmse_rise, 1.0) / 1.0) * 20))
    raw           = elbow_score + sample_score + curve_score

    if selection_method == "absolute_fallback":  return min(raw, 5)
    if selection_method == "composite_fallback": return min(raw, 25)
    if selection_method in ("flat_curve", "highest_safe"): return min(raw, 50)
    return raw  # elbow


def _build_audit_row(
    state_fips:         str,
    f_sys:              int,
    mode:               str,
    is_mountain:        bool,
    total_chunks:       int,
    sample_chunks:      int,
    max_v_rmse:         float,
    cache:              dict,
    passing:            list,
    valid_factors:      list,
    selection_method:   str,
    selected_factor:    int,
    peak_elbow_dist:    float,
    elbow_dist_profile: str,
    early_exit_factor:  int | None,
    rmse_rise:          float,
    timestamp:          str,
) -> dict:
    """Assembles a complete audit row dict from sweep results."""

    rmse_key    = "H_RMSE" if mode == "H" else "V_RMSE"
    ceiling_val = MAX_H_RMSE_FT if mode == "H" else max_v_rmse

    def safe_get(factor, key, default=""):
        s = cache.get(factor)
        return round(s[key], 4) if s and key in s else default

    # Baseline metrics (factor 100 if evaluated, else first evaluated factor)
    baseline_f = 100 if 100 in cache and cache[100] else (valid_factors[0] if valid_factors else None)
    last_pass_f = passing[-1] if passing else None

    # Deviation from national default
    nat_default = NATIONAL_DEFAULTS.get(mode, {}).get(f_sys, 400)
    deviation   = round(selected_factor / nat_default, 3) if nat_default else ""

    # Ceiling proximity: selected RMSE as % of ceiling
    sel_rmse = safe_get(selected_factor, rmse_key)
    ceil_prox = round((sel_rmse / ceiling_val) * 100, 1) if sel_rmse != "" and ceiling_val else ""

    # Confidence score
    conf = _confidence_score(selection_method, len(passing), rmse_rise, peak_elbow_dist)
    override = conf < 40 or selection_method in ("composite_fallback", "absolute_fallback")

    return {
        "timestamp":               timestamp,
        "state_fips":              state_fips,
        "f_sys":                   f_sys,
        "mode":                    mode,
        "is_mountain_state":       is_mountain,
        "total_chunks":            total_chunks,
        "sample_chunks":           sample_chunks,
        "v_rmse_ceiling":          max_v_rmse,
        "h_rmse_ceiling":          MAX_H_RMSE_FT,
        "maxv_ceiling":            _effective_max_v_dev(f_sys, state_fips),
        "maxh_ceiling":            _effective_max_h_dev(f_sys, state_fips),
        "n_evaluated":             len(valid_factors),
        "n_passing":               len(passing),
        "passing_factors":         "|".join(str(f) for f in passing),
        "rmse_rise":               round(rmse_rise, 4),
        "early_exit_factor":       early_exit_factor if early_exit_factor else "",
        "selection_method":        selection_method,
        "selected_factor":         selected_factor,
        "peak_elbow_distance":     round(peak_elbow_dist, 4),
        "elbow_distance_profile":  elbow_dist_profile,
        # Baseline
        "v_rmse_at_baseline":      safe_get(baseline_f, "V_RMSE"),
        "h_rmse_at_baseline":      safe_get(baseline_f, "H_RMSE"),
        "maxv_at_baseline":        safe_get(baseline_f, "Max_V_Dev"),
        "maxh_at_baseline":        safe_get(baseline_f, "Max_H_Dev"),
        "curvevar_at_baseline":    safe_get(baseline_f, "Curve_Var"),
        # Selected
        "v_rmse_at_selected":      safe_get(selected_factor, "V_RMSE"),
        "h_rmse_at_selected":      safe_get(selected_factor, "H_RMSE"),
        "maxv_at_selected":        safe_get(selected_factor, "Max_V_Dev"),
        "maxh_at_selected":        safe_get(selected_factor, "Max_H_Dev"),
        "curvevar_at_selected":    safe_get(selected_factor, "Curve_Var"),
        "std_v_rmse_at_selected":  safe_get(selected_factor, "Std_V_RMSE"),
        "std_h_rmse_at_selected":  safe_get(selected_factor, "Std_H_RMSE"),
        # Last passing
        "last_passing_factor":     last_pass_f if last_pass_f else "",
        "v_rmse_at_last_passing":  safe_get(last_pass_f, "V_RMSE") if last_pass_f else "",
        "h_rmse_at_last_passing":  safe_get(last_pass_f, "H_RMSE") if last_pass_f else "",
        "maxv_at_last_passing":    safe_get(last_pass_f, "Max_V_Dev") if last_pass_f else "",
        "maxh_at_last_passing":    safe_get(last_pass_f, "Max_H_Dev") if last_pass_f else "",
        # Quality
        "national_default_factor": nat_default,
        "deviation_from_default":  deviation,
        "ceiling_proximity_pct":   ceil_prox,
        "confidence_score":        conf,
        "override_recommended":    override,
    }


# ===========================================================================
# ELBOW DETECTION  (Kneedle algorithm, normalized)
# ===========================================================================

def _find_elbow(factors: list[int], rmse_vals: list[float]) -> tuple[int, float, str]:
    """
    Identifies the elbow of a monotonically increasing RMSE curve using the
    normalized perpendicular distance method (Kneedle algorithm).

    Returns
    -------
    Tuple of (elbow_factor, peak_distance, distance_profile_string).
    Now returns all three values so the audit CSV can record them.
    """
    if len(factors) < 3:
        return factors[0], 0.0, ""

    x = np.array([float(f) for f in factors])
    y = np.array(rmse_vals, dtype=float)

    x_range = x[-1] - x[0]
    y_range = y[-1] - y[0]
    x_norm  = (x - x[0]) / x_range  if x_range != 0 else np.zeros_like(x)
    y_norm  = (y - y[0]) / y_range  if y_range != 0 else np.zeros_like(y)

    distances = np.abs(y_norm - x_norm) / np.sqrt(2)
    elbow_idx = int(np.argmax(distances))

    dist_profile = "  ".join(f"F{factors[i]}:{distances[i]:.4f}" for i in range(len(factors)))
    logging.info(f"    [elbow distances] {dist_profile}")
    logging.info(
        f"    [elbow] Peak distance at Factor {factors[elbow_idx]} "
        f"(distance={distances[elbow_idx]:.4f})"
    )

    return factors[elbow_idx], float(distances[elbow_idx]), dist_profile


# ===========================================================================
# FACTOR SELECTION  (Gate 1 ceiling + elbow detection)
# ===========================================================================

def _find_optimal_factor(
    chunks:       list,
    demdir:       str,
    base_params:  dict,
    f_sys:        int,
    mode:         str,
    max_v_rmse:   float,
    fixed_other:  int,
    state_fips:   str,
    total_chunks: int,
    sample_chunks: int,
    timestamp:    str,
) -> tuple[int, dict]:
    """
    Sweeps all SWEEP_FACTORS, filters to geometrically safe ones (Gate 1),
    then applies elbow detection on the relevant RMSE curve.

    Returns
    -------
    (selected_factor, audit_row_dict)
    """
    cache:   dict[int, dict] = {}
    rmse_key = "H_RMSE" if mode == "H" else "V_RMSE"

    early_exit_factor  = None
    peak_elbow_dist    = 0.0
    elbow_dist_profile = ""

    def evaluate(factor: int) -> dict | None:
        if factor in cache:
            return cache[factor]
        h           = factor if mode == "H" else fixed_other
        v           = factor if mode == "V" else fixed_other
        test_params = _build_test_params(base_params, h, v)
        stats       = _aggregate_metrics(chunks, demdir, test_params, f_sys)
        cache[factor] = stats

        if stats:
            passes = _passes_ceiling(stats, max_v_rmse, f_sys, state_fips, mode)
            if passes:
                tag = "PASS"
            else:
                reasons = []
                if mode in ("V", "BOTH") and stats["V_RMSE"] > max_v_rmse:
                    reasons.append(f"V_RMSE={stats['V_RMSE']:.2f}>{max_v_rmse}")
                if mode in ("V", "BOTH") and stats["Max_V_Dev"] > _effective_max_v_dev(f_sys, state_fips):
                    reasons.append(f"MaxV={stats['Max_V_Dev']:.2f}>{_effective_max_v_dev(f_sys, state_fips):.1f}")
                if mode in ("H", "BOTH") and stats["H_RMSE"] > MAX_H_RMSE_FT:
                    reasons.append(f"H_RMSE={stats['H_RMSE']:.2f}>{MAX_H_RMSE_FT}")
                if mode in ("H", "BOTH") and stats["Max_H_Dev"] > _effective_max_h_dev(f_sys, state_fips):
                    reasons.append(f"MaxH={stats['Max_H_Dev']:.2f}>{_effective_max_h_dev(f_sys, state_fips):.1f}")
                tag = "FAIL: " + ", ".join(reasons)

            logging.info(
                f"  [{mode}] Factor {factor:4d} -> "
                f"V_RMSE:{stats['V_RMSE']:.2f}' H_RMSE:{stats['H_RMSE']:.2f}' "
                f"MaxV:{stats['Max_V_Dev']:.2f}' MaxH:{stats['Max_H_Dev']:.2f}' "
                f"CurveVar:{stats['Curve_Var']:.4f}  [{tag}]"
            )
        return stats

    # --- Step 1: Sweep with early exit ---
    consecutive_failures = 0
    for factor in SWEEP_FACTORS:
        stats = evaluate(factor)
        if stats and not _passes_ceiling(stats, max_v_rmse, f_sys, state_fips, mode):
            consecutive_failures += 1
            if consecutive_failures >= 3:
                early_exit_factor = factor
                logging.info(f"  [{mode}] 3 consecutive failures -- stopping sweep early at factor {factor}.")
                break
        else:
            consecutive_failures = 0

    # --- Step 2: Classify ---
    passing       = [f for f in SWEEP_FACTORS if cache.get(f) and _passes_ceiling(cache[f], max_v_rmse, f_sys, state_fips, mode)]
    valid_factors = [f for f in SWEEP_FACTORS if cache.get(f) is not None]

    # --- Step 3: Elbow selection ---
    if len(passing) >= 3:
        rmse_vals = [cache[f][rmse_key] for f in passing]
        rmse_rise = rmse_vals[-1] - rmse_vals[0]

        if rmse_rise < ELBOW_FLAT_THRESHOLD_FT:
            best             = passing[0]
            selection_method = "flat_curve"
            logging.info(
                f"  [{mode}] RMSE rise across {len(passing)} passing factors is only "
                f"{rmse_rise:.3f} ft (< flat threshold {ELBOW_FLAT_THRESHOLD_FT} ft). "
                f"Curve is flat -- selecting lowest passing factor: {best}"
            )
        else:
            best, peak_elbow_dist, elbow_dist_profile = _find_elbow(passing, rmse_vals)
            selection_method = "elbow"
            logging.info(f"  [{mode}] --> SELECTED (elbow): Factor {best} for FS{f_sys}")
    else:
        rmse_rise = 0.0

    # --- Step 4: Fewer than 3 factors passed ---
    if len(passing) < 3:
        if passing:
            best             = max(passing)
            selection_method = "highest_safe"
            logging.warning(
                f"  [{mode}] Only {len(passing)} factor(s) passed Gate 1 for FS{f_sys}. "
                f"Insufficient data for elbow detection. Returning highest safe factor: {best}"
            )
        elif valid_factors:
            best             = min(valid_factors, key=lambda f: _composite_score(cache[f], max_v_rmse, f_sys, state_fips))
            selection_method = "composite_fallback"
            logging.warning(
                f"  [{mode}] No factor passed hard deviation limits for FS{f_sys}. "
                f"Returning least-bad composite score fallback: Factor {best}."
            )
        else:
            best             = 100
            selection_method = "absolute_fallback"
            logging.error(f"  [{mode}] No valid metrics computed for FS{f_sys}. Using absolute fallback of 100.")

    audit_row = _build_audit_row(
        state_fips         = state_fips,
        f_sys              = f_sys,
        mode               = mode,
        is_mountain        = state_fips in MOUNTAIN_STATES,
        total_chunks       = total_chunks,
        sample_chunks      = sample_chunks,
        max_v_rmse         = max_v_rmse,
        cache              = cache,
        passing            = passing,
        valid_factors      = valid_factors,
        selection_method   = selection_method,
        selected_factor    = best,
        peak_elbow_dist    = peak_elbow_dist,
        elbow_dist_profile = elbow_dist_profile,
        early_exit_factor  = early_exit_factor,
        rmse_rise          = rmse_rise,
        timestamp          = timestamp,
    )

    return best, audit_row


def find_optimal_factors(
    chunks:        list,
    demdir:        str,
    base_params:   dict,
    f_sys:         int,
    max_v_rmse:    float,
    state_fips:    str,
    total_chunks:  int,
    sample_chunks: int,
    timestamp:     str,
) -> tuple[int, int, list[dict]]:
    """
    Finds the best H and V smoothing factors for one functional system.

    Returns
    -------
    (best_h, best_v, [h_audit_row, v_audit_row])
    """
    logging.info(f"  Searching H factor for FS{f_sys}...")
    best_h, h_audit = _find_optimal_factor(
        chunks, demdir, base_params, f_sys,
        mode="H", max_v_rmse=max_v_rmse, fixed_other=1000,
        state_fips=state_fips, total_chunks=total_chunks,
        sample_chunks=sample_chunks, timestamp=timestamp,
    )

    logging.info(f"  Searching V factor for FS{f_sys} (H fixed at {best_h})...")
    best_v, v_audit = _find_optimal_factor(
        chunks, demdir, base_params, f_sys,
        mode="V", max_v_rmse=max_v_rmse, fixed_other=best_h,
        state_fips=state_fips, total_chunks=total_chunks,
        sample_chunks=sample_chunks, timestamp=timestamp,
    )

    return best_h, best_v, [h_audit, v_audit]


# ===========================================================================
# MAIN
# ===========================================================================

# ---------------------------------------------------------------------------
# Pool initializer — injects a shared multiprocessing.Lock into each worker
# process so file writes are serialised without pickling the lock object.
# ---------------------------------------------------------------------------
_write_lock: multiprocessing.Lock = None  # set by _pool_init in each worker

def _pool_init(lock: multiprocessing.Lock) -> None:
    global _write_lock
    _write_lock = lock


def process_state(
    state_fips:       str,
    base_params:      dict,
    master_json_path: str,
    audit_csv_path:   str,
) -> tuple[str, dict, list[dict]]:
    """
    Calibrates all functional systems for one state.

    Runs entirely independently — no shared mutable state.  When finished
    it acquires _write_lock (a global multiprocessing.Lock injected via
    the pool initializer) once to append results to the master JSON and
    audit CSV, then releases the lock immediately.

    Returns (state_fips, state_results, audit_rows) for logging in main().
    """
    logging.info(f"\n{'='*60}\n=== CALIBRATION: STATE FIPS {state_fips} ===\n{'='*60}")
    run_timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    try:
        df = fetch_socrata_state(state_fips, SOCRATA_TOKEN)
    except Exception as e:
        logging.error(f"[{state_fips}] Failed to fetch data: {e}")
        return state_fips, {}, []

    state_results  = {}
    state_audit    = []

    for f_sys in range(1, 8):
        sub = df[df["FSystem"] == f_sys]
        if sub.empty:
            logging.info(f"[{state_fips}] FS {f_sys}: No data found, skipping.")
            continue

        logging.info(f"[{state_fips}] FS {f_sys}: Slicing geometry into 1-mile chunks...")
        all_chunks = []
        for _, route_group in sub.groupby("RouteId"):
            route_chunks = generate_1mile_chunks(
                route_group.sort_values("Start_MP")["WKT"].tolist()
            )
            all_chunks.extend(route_chunks)

        total_chunks = len(all_chunks)

        if not all_chunks:
            logging.info(f"[{state_fips}] FS {f_sys}: Not enough contiguous geometry for a 1-mile sample.")
            continue

        sample_size    = cochran_sample_size(total_chunks, confidence=0.95, margin_of_error=0.05)
        sampled_chunks = random.sample(all_chunks, sample_size)

        download_dems([g.wkt for g in sampled_chunks], DEM_DIR)

        max_v_rmse = V_RMSE_THRESHOLDS.get(f_sys, 3.5)

        logging.info(
            f"[{state_fips}] FS {f_sys}: Optimising over {sample_size} chunks "
            f"(V_RMSE ceiling={max_v_rmse} ft, H_RMSE ceiling={MAX_H_RMSE_FT} ft, "
            f"elbow flat threshold={ELBOW_FLAT_THRESHOLD_FT} ft)..."
        )

        best_h, best_v, audit_rows = find_optimal_factors(
            chunks        = sampled_chunks,
            demdir        = DEM_DIR,
            base_params   = base_params,
            f_sys         = f_sys,
            max_v_rmse    = max_v_rmse,
            state_fips    = state_fips,
            total_chunks  = total_chunks,
            sample_chunks = sample_size,
            timestamp     = run_timestamp,
        )

        state_audit.extend(audit_rows)

        logging.info(f"[{state_fips}] FS {f_sys}: --> FINAL: H={best_h}, V={best_v}")

        if f_sys == 1:
            state_results["H_SMOOTH_FACTOR"] = best_h
            state_results["V_SMOOTH_FACTOR"] = best_v
        else:
            state_results[f"H_SMOOTH_FACTOR_FS{f_sys}"] = best_h
            state_results[f"V_SMOOTH_FACTOR_FS{f_sys}"] = best_v

    # --- Persist results under lock so parallel states don't collide ---
    with _write_lock:
        # Re-read JSON so we merge cleanly with any states that finished first
        master_data = {}
        if os.path.exists(master_json_path):
            with open(master_json_path, "r") as f:
                try:
                    master_data = json.load(f)
                except json.JSONDecodeError:
                    master_data = {}

        master_data[state_fips] = state_results
        with open(master_json_path, "w") as f:
            json.dump(master_data, f, indent=4)
        logging.info(f"[{state_fips}] Appended to {master_json_path}")

        # Append new audit rows to the CSV (read existing, extend, rewrite)
        existing_rows = []
        if os.path.exists(audit_csv_path):
            with open(audit_csv_path, newline="", encoding="utf-8") as f:
                existing_rows = list(csv.DictReader(f))
        existing_rows.extend(state_audit)
        with open(audit_csv_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=AUDIT_FIELDNAMES, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(existing_rows)
        logging.info(f"[{state_fips}] Audit CSV updated: {audit_csv_path}")

    return state_fips, state_results, state_audit


def main():
    parser = argparse.ArgumentParser(description="RAT National Calibration Engine")
    parser.add_argument("--outdir",      required=True, help="Output directory")
    parser.add_argument("--demdir",      required=True, help="DEM cache directory")
    parser.add_argument("--state",       default="ALL", help="State FIPS code or ALL")
    parser.add_argument("--params_json", default=None,  help="Path to run_params.json")
    args = parser.parse_args()

    user_params = {}
    if args.params_json and os.path.exists(args.params_json):
        with open(args.params_json, "r") as f:
            user_params = json.load(f)

    user_params.update({"OUTPUT_DIR": args.outdir, "DEM_DIR": args.demdir})

    os.makedirs(args.outdir, exist_ok=True)
    os.makedirs(args.demdir, exist_ok=True)

    base_params = build_params(BASE_ENGINE_PARAMS)

    states_list = ALL_FIPS if args.state.upper() == "ALL" else [args.state.zfill(2)]
    suite_root       = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    master_json_path = os.path.join(suite_root, "core", "national_smoothing_factors.json")
    audit_csv_path   = os.path.join(suite_root, "core", "calibration_audit.csv")

    # Determine which states still need processing
    master_data = {}
    if os.path.exists(master_json_path):
        with open(master_json_path, "r") as f:
            try:
                master_data = json.load(f)
            except json.JSONDecodeError:
                master_data = {}

    pending = [s for s in states_list if s not in master_data]
    skipped = len(states_list) - len(pending)
    if skipped:
        logging.info(f"Skipping {skipped} state(s) already in master JSON.")
    if not pending:
        logging.info("All states already calibrated. Nothing to do.")
        return

    logging.info(
        f"Processing {len(pending)} state(s) with up to {MAX_PARALLEL_STATES} parallel worker(s)."
    )

    write_lock = multiprocessing.Manager().Lock()

    with ProcessPoolExecutor(
        max_workers=MAX_PARALLEL_STATES,
        initializer=_pool_init,
        initargs=(write_lock,),
    ) as executor:
        futures = {
            executor.submit(
                process_state,
                fips, base_params, master_json_path, audit_csv_path,
            ): fips
            for fips in pending
        }
        completed = 0
        for fut in as_completed(futures):
            fips = futures[fut]
            completed += 1
            try:
                state_fips, state_results, _ = fut.result()
                logging.info(
                    f"[{state_fips}] Finished ({completed}/{len(pending)} states complete)."
                )
            except Exception as e:
                logging.error(f"[{fips}] Worker raised an exception: {e}")

    logging.info("\n=== NATIONAL CALIBRATION SWEEP COMPLETE ===")
    logging.info(f"Master JSON: {master_json_path}")
    logging.info(f"Audit CSV:   {audit_csv_path}")


if __name__ == "__main__":
    main()
