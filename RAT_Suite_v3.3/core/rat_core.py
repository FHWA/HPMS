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
from dataclasses import dataclass
from typing import Dict, List, Tuple, Optional
import math
import os
import json
import logging
import time
import random
import numpy as np
import pandas as pd
import geopandas as gpd
import rasterio
import requests
from shapely.geometry import LineString, Point, MultiLineString, shape
from shapely.wkt import loads
from shapely.ops import linemerge
from scipy.interpolate import UnivariateSpline
from scipy.signal import savgol_filter, butter, filtfilt
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
    'DENSIFY_SPACING_FT':          5.0,
    'H_BASE_SMOOTH_WINDOW':        21,
    'H_MIN_HEAD_CHANGE':           0.003,
    'H_LOOKAHEAD_DIST_M':          10.0,
    'TRIM_CURVE_ENDPOINTS':        True,

    # --- Horizontal curve detection ---
    'H_MIN_DELTA':                 3.5,
    'H_MIN_CURVE_LENGTH_FT':       100.0,
    'H_MAX_RADIUS_FT':             165000.0,

    # --- Vertical curve detection ---
    'V_VC_THRESHOLD':              0.002,
    'V_MIN_CURVE_LENGTH_FT':       200.0,
    'V_GAP_TOLERANCE':             5,
    'V_MIN_GRADE_CHANGE':          1.5,
    'V_MIN_OFFSET_FT':             0.10,
    'V_REVERSAL_TOLERANCE':        0.02,
    'REGRESSION_WINDOW_FT':        500.0,

    # --- Profile bridging ---
    'TREND_WINDOW_FT':             1000.0, 
    'DIP_THRESHOLD_FT':            6.5,
    'BRIDGE_MAX_LEN_FT':           8200.0,

    # --- Merge controls ---
    'ENABLE_MERGE':                False,
    'MERGE_GAP_FT':                600.0,
    'V_MERGE_GAP_FT':              1500.0,

    # --- National smoothing factor defaults (FS 1 baseline) ---
    'H_SMOOTH_FACTOR':    400,   
    'V_SMOOTH_FACTOR':    1400,  

    # --- Per-functional-system smoothing overrides ---
    'H_SMOOTH_FACTOR_FS2': 200,   
    'V_SMOOTH_FACTOR_FS2': 1400,
    'H_SMOOTH_FACTOR_FS3': 400,   
    'V_SMOOTH_FACTOR_FS3': 1400,
    'H_SMOOTH_FACTOR_FS4': 400,   
    'V_SMOOTH_FACTOR_FS4': 1400,
    'H_SMOOTH_FACTOR_FS5': 200,   
    'V_SMOOTH_FACTOR_FS5': 1000,
    'H_SMOOTH_FACTOR_FS6': 200,   
    'V_SMOOTH_FACTOR_FS6': 1000,
    'H_SMOOTH_FACTOR_FS7': 400,   
    'V_SMOOTH_FACTOR_FS7': 1000,
}

# -------------------------
# Build integer mask array identifying vertices that fall within a bridge or tunnel
# 0 = None, 1 = HPMS (Tier 1), 2 = NBI/NTI (Tier 2), 3 = Algorithmic Dip (Tier 3)
# -------------------------
def build_structure_mask(
    mileposts: np.ndarray,
    coords_wgs: List[Tuple[float, float]],
    route_id: str,
    hpms_structures: pd.DataFrame = None,
    nbi_nti_gdf: gpd.GeoDataFrame = None,
    spatial_tolerance_deg: float = 0.00015
) -> np.ndarray:
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
                tier_mask[(mileposts >= s_mp) & (mileposts <= e_mp)] = 1

    # TIER 2: NBI/NTI Spatial Fallback
    # Previously this created a shapely Point for every unmasked vertex,
    # copied + buffered the full structures GeoDataFrame, and ran gpd.sjoin().
    # At 100M+ vertices that pattern is not viable -- the Point list alone
    # exhausts RAM before sjoin even starts. Replaced with a projected KDTree:
    # project structure centroids and route vertices to UTM once, query by
    # radius, no shapely objects or GeoDataFrame copies needed at all.
    if nbi_nti_gdf is not None and not nbi_nti_gdf.empty:
        unmasked_indices = np.where(tier_mask == 0)[0]
        if len(unmasked_indices) > 0 and len(coords_wgs) > 0:
            # Determine UTM zone from the first route coordinate
            lon0, lat0 = coords_wgs[0]
            utm_epsg = get_appropriate_utm_zone(lon0, lat0)
            fwd = Transformer.from_crs("EPSG:4326", f"EPSG:{utm_epsg}", always_xy=True)

            # Project structure centroids to UTM (done once, no copy of gdf needed)
            struct_xy = np.array([
                fwd.transform(geom.x, geom.y)
                for geom in nbi_nti_gdf.geometry
            ], dtype=float)

            # Filter out any points that resulted in Infinity or NaN during projection
            valid_mask = np.isfinite(struct_xy).all(axis=1)
            struct_xy = struct_xy[valid_mask]

            if len(struct_xy) > 0:
                struct_tree = cKDTree(struct_xy)

                # Convert the degree tolerance to approximate metres at this latitude
                tol_m = spatial_tolerance_deg * 111_320.0

                # Project only the unmasked route vertices -- not all 100M of them
                unmasked_xy = np.array([
                    fwd.transform(coords_wgs[i][0], coords_wgs[i][1])
                    for i in unmasked_indices
                ], dtype=float)

                # query_ball_point in chunks to keep peak memory bounded
                CHUNK = 50_000
                for start in range(0, len(unmasked_indices), CHUNK):
                    end = min(start + CHUNK, len(unmasked_indices))
                    hits = struct_tree.query_ball_point(unmasked_xy[start:end], r=tol_m)
                    for local_i, hit_list in enumerate(hits):
                        if hit_list:
                            tier_mask[unmasked_indices[start + local_i]] = 2

    return tier_mask

def fix_profile_by_deviation(
    z_vals: np.ndarray, 
    spacing_m: float, 
    params: Dict, 
    structure_mask: np.ndarray = None
) -> Tuple[np.ndarray, np.ndarray]:
    z_fixed = np.copy(z_vals)
    n = len(z_fixed)
    tier_out = np.copy(structure_mask) if structure_mask is not None else np.zeros(n, dtype=int)
    
    # 1. Calculate the rolling trend FIRST, but MASK known bridges so the trend doesn't fall into massive gorges
    window_pts = int(params['TREND_WINDOW_M'] / spacing_m)
    if window_pts < 3: window_pts = 3
    if window_pts % 2 == 0: window_pts += 1
        
    z_for_trend = np.copy(z_fixed)
    if structure_mask is not None:
        # Identify Tiers 1 and 2
        known_structs = (structure_mask == 1) | (structure_mask == 2)
        if np.any(known_structs):
            # Delete the gorge elevations and draw a straight interpolation across the void
            z_for_trend[known_structs] = np.nan
            z_for_trend = pd.Series(z_for_trend).interpolate(limit_direction='both').bfill().ffill().to_numpy()
            
    z_trend = pd.Series(z_for_trend).rolling(window=window_pts, center=True, min_periods=1).median().to_numpy()
    
    # Calculate the deviations from our new, gorge-proof trend line
    deviation = z_fixed - z_trend
    is_dip = deviation < -params['DIP_THRESHOLD_M']
    
    # 2. Combine all structures: HPMS (1), NBI (2), and Algorithmic Dips (is_dip)
    bool_mask = (tier_out > 0) | is_dip
    
    i = 0
    while i < n:
        if not bool_mask[i]:
            i += 1
            continue
            
        start_idx = i
        while i < n and bool_mask[i]:
            i += 1
        end_idx = i - 1
        
        # 3. UNIVERSAL ABUTMENT ANCHORING
        # Walk backward to find the true top of the gorge
        anchor_start = max(0, start_idx - 1)
        while anchor_start > 0 and (z_fixed[anchor_start] < z_trend[anchor_start] - 0.15):
            anchor_start -= 1
            
        # Walk forward to find the true top of the gorge on the other side
        anchor_end = min(n - 1, end_idx + 1)
        while anchor_end < n - 1 and (z_fixed[anchor_end] < z_trend[anchor_end] - 0.15):
            anchor_end += 1
            
        span_len = (anchor_end - anchor_start) * spacing_m
        if span_len < params['BRIDGE_MAX_LEN_M'] and anchor_end > anchor_start:
            z1, z2 = z_fixed[anchor_start], z_fixed[anchor_end]
            interp = np.linspace(z1, z2, anchor_end - anchor_start + 1)
            z_fixed[anchor_start:anchor_end + 1] = interp
            
            # Tag the extended abutment approaches as Tier 3 (Dip) if they weren't already HPMS/NBI
            tier_out[anchor_start:anchor_end + 1] = np.where(
                tier_out[anchor_start:anchor_end + 1] == 0, 
                3, 
                tier_out[anchor_start:anchor_end + 1]
            )
            
        i = max(i, anchor_end + 1)
        
    return z_fixed, tier_out


# -------------------------
# Parameter normalization
# -------------------------
def build_params(user_params: Optional[Dict] = None) -> Dict:
    p = DEFAULTS.copy()
    
    # 1. Apply user params first (From GUI)
    if user_params:
        for k, v in user_params.items():
            if v is not None and str(v).strip() != "":
                p[k] = v

    # Cast numerics
    for k, v in list(p.items()):
        if k in ["STATE_FIPS", "OUTPUT_DIR", "INPUT_URL", "SOCRATA_TOKEN", "PP_ROUTE_ID", "DEM_DIR"]:
            continue
        if isinstance(v, str):
            try:
                pass  # No elevation processing needed for string values
            except FileNotFoundError as e:
                logging.warning(f"DEM file not found: {e}")
            except ValueError as e:
                logging.warning(f"Invalid elevation data format: {e}")
            except Exception as e:
                logging.error(f"Unexpected error reading DEM data: {type(e).__name__}: {e}")

    # 2. Apply National Smoothing Factors over the top IF it's a batch run
    if p.get('STATE_FIPS'):
        state_fips = str(p['STATE_FIPS']).zfill(2)
        if state_fips not in ["00", "AL"]:
            # If IGNORE_GUI_SMOOTHING_FACTORS is explicitly False, we DO NOT load JSON (trust the single-state GUI numbers).
            # Otherwise (True, or missing from a raw CLI call), we DO load JSON.
            force_json = True
            if user_params and "IGNORE_GUI_SMOOTHING_FACTORS" in user_params:
                if not user_params["IGNORE_GUI_SMOOTHING_FACTORS"]:
                    force_json = False
            
            if force_json:
                json_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'national_smoothing_factors.json')
                if os.path.exists(json_path):
                    try:
                        with open(json_path, 'r') as f:
                            master_data = json.load(f)
                        if state_fips in master_data:
                            for key, val in master_data[state_fips].items():
                                if val is not None:
                                    p[key] = val
                    except Exception as e:
                        logging.error(f"Failed to read national smoothing factors JSON: {e}")

    p['H_SMOOTH_FACTOR'] = int(p.get('H_SMOOTH_FACTOR', 400))
    p['V_SMOOTH_FACTOR'] = int(p.get('V_SMOOTH_FACTOR', 1400))
    p['H_BASE_SMOOTH_WINDOW'] = int(p.get('H_BASE_SMOOTH_WINDOW', 21))
    if p['H_BASE_SMOOTH_WINDOW'] < 5: p['H_BASE_SMOOTH_WINDOW'] = 5

    p['DENSIFY_SPACING_M'] = p.get('DENSIFY_SPACING_FT', 5.0) / FEET_PER_METER
    p['H_MIN_CURVE_LENGTH_M'] = p.get('H_MIN_CURVE_LENGTH_FT', 100.0) / FEET_PER_METER
    p['H_MAX_RADIUS'] = p.get('H_MAX_RADIUS_FT', 165000.0) / FEET_PER_METER
    p['V_MIN_CURVE_LENGTH'] = p.get('V_MIN_CURVE_LENGTH_FT', 200.0) / FEET_PER_METER
    p['REGRESSION_WINDOW_M'] = p.get('REGRESSION_WINDOW_FT', 500.0) / FEET_PER_METER
    p['TREND_WINDOW_M'] = p.get('TREND_WINDOW_FT', 1000.0) / FEET_PER_METER
    p['DIP_THRESHOLD_M'] = p.get('DIP_THRESHOLD_FT', 6.5) / FEET_PER_METER
    p['BRIDGE_MAX_LEN_M'] = p.get('BRIDGE_MAX_LEN_FT', 8200.0) / FEET_PER_METER
    return p

# ---------------------------------------------------------------------------
# Centralized Data Loaders (Socrata & Local)
# ---------------------------------------------------------------------------
def filter_local_df_to_state(df: pd.DataFrame, state_fips: str) -> pd.DataFrame:
    """
    Filters a locally cached HPMS extract (e.g. the national "reuse Socrata
    cache" CSV) down to a single state, mirroring the server-side
    stateid='{fips}' filter that fetch_socrata_state() applies on a live
    Socrata query.

    This exists because the cached-CSV load path in rat_alignment_cli.py and
    hpms_4d_enricher_cli.py had no equivalent filter -- "reuse cache" loaded
    and processed the FULL national extract regardless of the State FIPS
    field, which is why state-scoped runs were silently processing
    national-scale grid counts. Centralized here per the v3 design direction
    of shared logic in core rather than duplicated per-script.

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
    # Normalize the state column into a temporary series rather than
    # copying the entire (potentially national-scale) dataframe first.
    # df.copy() before filtering could duplicate hundreds of MB just to
    # mutate one column before discarding most rows.
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
) -> pd.DataFrame:
    """
    Applies the standard Facility_Type / FSystem allow-list filters to an
    already-loaded HPMS dataframe (e.g. from load_local_hpms(), or a
    Socrata extract that was downloaded without server-side filtering).

    This is the same filtering logic fetch_socrata_state() applies via its
    $where clause -- centralized here so every script that consumes a local
    or cached dataframe (rat_alignment_cli.py, hpms_4d_enricher_cli.py,
    rat_plan_profile_cli.py) applies it identically, rather than each script
    re-implementing its own isin() check. (hpms_4d_enricher_cli.py's
    local-dataframe path previously skipped this filter entirely.)

    Pass an empty/None filter list to skip that particular filter (matches
    the "no restriction" behavior used elsewhere in the suite).
    """
    before = len(df)

    if facility_filter and "Facility_Type" in df.columns:
        df = df[df["Facility_Type"].isin(facility_filter)]

    if fsystem_filter and "FSystem" in df.columns:
        df = df[df["FSystem"].isin(fsystem_filter)]

    after = len(df)
    if after != before:
        logging.info(
            f"Facility Type / FSystem filter: {before:,} -> {after:,} rows."
        )
    return df


def fetch_socrata_state(
    state_fips: str,
    token: str = "",
    facility_type_filter: list = None,
    fsystem_filter: list = None,
    extra_cols: list = None,
    progress_callback=None,
    url: str = None,
) -> pd.DataFrame:
    """
    Downloads HPMS records for a single state from Socrata, with the
    facility-type and functional-system filters applied server-side via
    the $where clause (so unwanted rows -- e.g. ramps, non-inventory
    direction -- are never pulled over the network in the first place).

    url, if provided, overrides the SOCRATA_DEFAULT endpoint (e.g. for
    pointing at a different dataset/resource ID).

    extra_cols is currently accepted for forward compatibility with
    callers that want to note additional HPMS fields of interest (e.g.
    hpms_4d_enricher_cli.py); the Socrata query does not use $select, so
    all fields are returned regardless, and extra_cols has no effect on
    the request today.

    progress_callback, if provided, is called after each page is fetched
    as progress_callback(rows_fetched_so_far) -- e.g. for a GUI status bar.
    """
    socrata_url = url or SOCRATA_DEFAULT
    headers = {"X-App-Token": token} if token else {}

    ft = facility_type_filter or [1, 2]
    ft_clause = "AND facility_type IN (" + ", ".join(f"'{v}'" for v in ft) + ")"

    fs_clause = ""
    if fsystem_filter:
        fs_clause = "AND f_system IN (" + ", ".join(f"'{v}'" for v in fsystem_filter) + ")"

    where_clause = f"stateid='{state_fips}' {ft_clause} {fs_clause}".strip()
    params = {"$limit": 100_000, "$offset": 0, "$where": where_clause}

    rows = []
    logging.info(f"Fetching Socrata data for State FIPS {state_fips}...")

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
                        wait_s = min(60, 2 ** attempt)
                        logging.warning(
                            f"Socrata temporary error {r.status_code}. "
                            f"Attempt {attempt}/{max_attempts}. Retrying in {wait_s}s..."
                        )
                        time.sleep(wait_s)
                        continue
                    r.raise_for_status()
                    break
                except requests.RequestException as ex:
                    if attempt == max_attempts:
                        raise
                    wait_s = min(60, 2 ** attempt)
                    logging.warning(
                        f"Socrata request failed ({ex}). "
                        f"Attempt {attempt}/{max_attempts}. Retrying in {wait_s}s..."
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

    # Drop the raw geometry column now that WKT has been built from it.
    # Leaving it in place is what let load_local_hpms() pick up the raw,
    # un-parseable dict-string column instead of the proper WKT column when
    # this dataframe was saved to CSV and re-read later -- dropping it here
    # removes the ambiguity at the source instead of relying solely on
    # load_local_hpms()'s column-priority fix to compensate for it. It also
    # keeps the saved socrata_input_extract.csv from carrying a redundant
    # second copy of every route's geometry.
    df.drop(columns=[geom_col], inplace=True)

    col_map = {
        "route_id":    "RouteId",
        "begin_point": "Start_MP",
        "end_point":   "End_MP",
        "f_system":    "FSystem",
        "facility_type": "Facility_Type",
        "structure_type": "Structure_Type"
    }
    df.rename(columns=col_map, inplace=True)
    
    if "Start_MP" not in df.columns: df["Start_MP"] = 0.0
    if "End_MP" not in df.columns: df["End_MP"] = 0.0
    if "FSystem" not in df.columns: df["FSystem"] = 1
    if "Facility_Type" not in df.columns: df["Facility_Type"] = 2
    if "Structure_Type" not in df.columns: df["Structure_Type"] = 0

    df["Start_MP"] = pd.to_numeric(df["Start_MP"], errors="coerce").fillna(0.0)
    df["End_MP"]   = pd.to_numeric(df["End_MP"],   errors="coerce").fillna(0.0)
    df["FSystem"]  = pd.to_numeric(df["FSystem"],  errors="coerce").fillna(1).astype(int)
    df["Facility_Type"] = pd.to_numeric(df["Facility_Type"], errors="coerce").fillna(2).astype(int)
    df["Structure_Type"] = pd.to_numeric(df["Structure_Type"], errors="coerce").fillna(0).astype(int)
    df = df[df["WKT"].notna() & (df["WKT"] != "")].copy()
    return df


def load_local_hpms(path: str) -> pd.DataFrame:
    if not os.path.exists(path):
        raise FileNotFoundError(path)
    if path.lower().endswith(".csv"):
        file_size_gb = os.path.getsize(path) / (1024 ** 3)
        # NOTE ON RouteId DTYPE: pd.read_csv() infers each column's dtype
        # independently. If a RouteId-like column's values happen to be ALL
        # purely numeric (e.g. "001"), pandas silently infers int64 and
        # drops the leading zeros -- and by the time the .astype(str) call
        # further down in this function runs, that information is already
        # gone (str(1) == "1", not "001"). HPMS defines RouteId as a text
        # field, so this must be forced at read time, before pandas ever
        # gets a chance to parse it as a number. The raw column may not be
        # named "RouteId" yet at this point (the rename to "RouteId" happens
        # later below) -- it could be "route_id", "ROUTE_ID", "Route", or
        # "id" -- so peek at just the header first to find the real column
        # name, using the same case-insensitive candidate list used for the
        # rename further down.
        route_id_candidates = ["route_id", "routeid", "route", "id"]
        header_only = pd.read_csv(path, nrows=0)
        route_id_source_col = None
        for col in header_only.columns:
            if col.lower() in route_id_candidates:
                route_id_source_col = col
                break
        forced_dtype = {route_id_source_col: str} if route_id_source_col else None
        if file_size_gb > 2.0:
            # Large national extracts can exceed available RAM with a single
            # read_csv call, and even chunked reading can still blow up at the
            # final pd.concat() step if numeric columns are unnecessarily wide
            # (e.g. all inferred as float64/int64 by default). Downcasting per
            # chunk before concatenation reduces the size of the final
            # contiguous arrays that concat must allocate. Text columns are
            # left as-is (object dtype) rather than converted to category --
            # category dtype can behave inconsistently across pd.concat when
            # chunks have differing category sets, and downstream groupby on
            # RouteId in particular needs predictable object-dtype behavior.
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
        # Check candidates in priority order (WKT/WKT_ZM first, raw "line"
        # last) rather than in df.columns' file order. The previous version
        # iterated df.columns and returned the first column matching ANY
        # candidate -- so if a raw Socrata "line" column (a stringified
        # geometry dict, e.g. from fetch_socrata_state()'s saved extract)
        # happened to appear earlier in the file than the already-correct
        # "WKT" column, it would win and overwrite the valid WKT text with
        # the raw dict string, causing shapely.wkt.loads() to fail downstream.
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
    df.rename(columns=col_map, inplace=True)

    if "RouteId" not in df.columns: raise ValueError("Missing RouteId column.")
    if "WKT" not in df.columns: raise ValueError("Missing geometry/WKT column.")

    if "Start_MP" not in df.columns: df["Start_MP"] = 0.0
    if "End_MP" not in df.columns: df["End_MP"] = 0.0
    if "FSystem" not in df.columns: df["FSystem"] = 1
    if "Facility_Type" not in df.columns: df["Facility_Type"] = 2
    if "Structure_Type" not in df.columns: df["Structure_Type"] = 0

    df["RouteId"] = df["RouteId"].astype(str).str.strip().str.upper()
    df["Start_MP"] = pd.to_numeric(df["Start_MP"], errors="coerce").fillna(0.0)
    df["End_MP"] = pd.to_numeric(df["End_MP"], errors="coerce").fillna(0.0)
    df["FSystem"] = pd.to_numeric(df["FSystem"], errors="coerce").fillna(1).astype(int)
    df["Facility_Type"] = pd.to_numeric(df["Facility_Type"], errors="coerce").fillna(2).astype(int)
    df["Structure_Type"] = pd.to_numeric(df["Structure_Type"], errors="coerce").fillna(0).astype(int)
    df["WKT"] = df["WKT"].astype(str).str.strip()
    df = df[df["WKT"].notna() & (df["WKT"] != "")].copy()
    return df

# -------------------------
# Download National Bridge and Tunnel data
# -------------------------
def fetch_nbi_nti_state(
    state_fips: str, 
    token: str = "", 
    nbi_url: str = None,
    nti_url: str = None
) -> gpd.GeoDataFrame:
    
    import os
    import glob
    import pandas as pd
    import geopandas as gpd
    from shapely.geometry import Point
    import logging
    
    points = []
    
    # ==============================================================
    # THE ULTIMATE OFFLINE AIRGAP: Auto-ingest from the NBI folder
    # ==============================================================
    # Dynamically find the NBI folder relative to this script
    core_dir = os.path.dirname(os.path.abspath(__file__))
    suite_dir = os.path.dirname(core_dir)
    nbi_folder = os.path.join(suite_dir, "NBI")
    
    if os.path.exists(nbi_folder):
        local_files = glob.glob(os.path.join(nbi_folder, "*.*"))
        for file_path in local_files:
            if not (file_path.lower().endswith(".csv") or file_path.lower().endswith(".txt") or file_path.lower().endswith(".xml")):
                continue
                
            logging.info(f"AIRGAP Auto-Load: Ingesting structures from {os.path.basename(file_path)}...")
            try:
                if file_path.lower().endswith(".csv") or file_path.lower().endswith(".txt"):
                    df = pd.read_csv(file_path, dtype=str)
                else:
                    # BYPASS PANDAS & DIRTY XML: Sanitize the raw text before parsing
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
                        
                        # Extract any variation of the NBI vertical clearance columns safely
                        clearance_keys = ["VERTICAL_CLEARANCE_47", "MINIMUM_VERTICAL_CLEARANCE", "CLR_V_UNDER", "VERT_CLR", "CLEARANCE"]
                        clearance_val = None
                        for k in clearance_keys:
                            if k in row:
                                clearance_val = row[k]
                                break
                            
                        points.append({
                            "geometry": Point(float(lon), float(lat)),
                            "vertical_clearance": clearance_val
                        })
                    except (ValueError, TypeError) as e:
                        # Invalid coordinate or clearance value
                        logging.debug(f"Skipping structure due to invalid coordinates/clearance: {e}")
                        continue
                    except Exception as e:
                        logging.debug(f"Skipping structure record: {type(e).__name__}")
                        continue
            except Exception as e:
                logging.warning(f"Failed to read local file {file_path}: {e}")
                
    if points:
        logging.info(f"Successfully mapped {len(points)} local bridges/tunnels.")
        return gpd.GeoDataFrame(points, crs="EPSG:4326")
        
    logging.warning("No local NBI/NTI structures loaded. Make sure CSV/XML files are in the RAT_Suite v3.3/NBI folder.")
    return gpd.GeoDataFrame(geometry=[], crs="EPSG:4326")

# -------------------------
# Download 1 meter USGS DEMs
# -------------------------
def get_1deg_tile_name(lon: float, lat: float) -> str:
    """
    Returns the standard 1-degree tile name for a given coordinate.
    Matches the naming convention used by USGS 3DEP staged products.
    e.g. lon=-122.3, lat=47.6 -> 'n48w123'
    """
    ns = 'n' if lat >= 0 else 's'
    ew = 'w' if lon < 0 else 'e'
    # Ceiling for lat (n48 covers 47-48), ceiling of abs for lon (w123 covers 122-123)
    lat_deg = int(math.ceil(abs(lat)))
    lon_deg = int(math.ceil(abs(lon)))
    return f"{ns}{lat_deg:02d}{ew}{lon_deg:03d}"


def prepare_1m_tile_for_worker(bbox: tuple, base_dem_dir: str, params: dict) -> str:
    """
    Shared grid-worker initialization step used by both the alignment and
    4D enrichment pipelines: downloads the 1-degree 1m tile(s) covering
    this grid cell's bbox into a single shared, persistent cache
    directory, and sets HIGH_RES_MODE=True on params so downstream
    processing functions sample from the 1m raster.

    Previously each pipeline had its own near-identical 2-line version of
    this with its own separate cache subfolder (align_1m_cache vs
    enrich_4d_1m_cache) -- since both download the exact same underlying
    USGS 1m tiles, that meant every tile got downloaded twice whenever
    both pipelines ran for the same state (the common case). Using one
    shared cache folder means whichever pipeline runs first populates the
    cache for the other.

    Returns the shared cache directory path.
    """
    shared_cache_dir = os.path.join(base_dem_dir, "align_1m_cache")
    os.makedirs(shared_cache_dir, exist_ok=True)
    download_high_res_dem_tile(bbox, shared_cache_dir)
    params["HIGH_RES_MODE"] = True
    return shared_cache_dir


def download_high_res_dem_tile(bbox: tuple, cache_dir: str) -> str:
    """
    Downloads the 1-degree USGS 3DEP 1-meter tile(s) covering the given bounding
    box to cache_dir, skipping any tile already present.

    Switched from per-cell custom bounding box downloads to standard 1-degree tile
    caching. This reduces storage from ~23 MB per 0.02-degree cell to ~23 MB per
    1-degree tile -- a 2,500x reduction for a dense grid over a large state.

    Returns the path to the primary tile file (for the SW corner of the bbox).
    Multiple tiles are downloaded if the bbox crosses a degree boundary.
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

    os.makedirs(cache_dir, exist_ok=True)

    # Determine which 1-degree tiles cover this bbox
    needed = set()
    for lat in [miny, maxy]:
        for lon in [minx, maxx]:
            needed.add(get_1deg_tile_name(lon, lat))

    primary_path = None
    # One TCP+TLS session shared across all tiles needed by this bbox
    # rather than a new connection per tile.
    with requests.Session() as usgs_session:
        for tile_name in sorted(needed):
            tile_path = os.path.join(cache_dir, f"tile_1m_{tile_name}.tif")
            if primary_path is None:
                primary_path = tile_path
            if os.path.exists(tile_path):
                logging.debug(f"1m tile cache HIT: {tile_name}")
                continue

            # Construct 1-degree bounding box for this tile
            # tile n48w123 covers lon -123 to -122, lat 47 to 48
            ns = tile_name[0]
            lat_deg = int(tile_name[1:3])
            ew = tile_name[3]
            lon_deg = int(tile_name[4:7])
            t_miny = (lat_deg - 1) if ns == 'n' else -(lat_deg)
            t_maxy = lat_deg        if ns == 'n' else -(lat_deg - 1)
            t_minx = -lon_deg       if ew == 'w' else (lon_deg - 1)
            t_maxx = -(lon_deg - 1) if ew == 'w' else lon_deg

            # Request at 120x120 pixels per 0.02-degree cell = 6000x6000 for 1 degree
            # 2400x2400 provides ~1m resolution over a 1-degree cell at mid-latitudes
            tile_size = 2400
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

            # Check persistent cross-call failure count before trying again --
            # prevents a tile with a sustained USGS-side problem from being
            # retried forever across thousands of grid cells (this is what
            # caused an 18-hour stall on a single Hawaii tile in production).
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

            # If another worker is already downloading this tile, wait for it
            # rather than hammering USGS with duplicate requests.
            #
            # Waits based on the lock file's AGE, not on how long THIS worker
            # has personally been waiting -- see the detailed comment in
            # download_dems() for why a per-waiter elapsed-time timeout
            # guarantees an eventual duplicate attempt under sustained
            # parallelism, regardless of how generous the timeout is.
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
                # the create-if-not-exists check atomic at the OS level, closing
                # the millisecond race window that a plain open(path, 'w') has
                # between two workers both passing the earlier os.path.exists()
                # check and then both believing they won the lock.
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

                if not success:
                    for attempt in range(1, max_retries + 1):
                        try:
                            logging.info(f"CACHE MISS: Downloading 1m tile {tile_name} (attempt {attempt}/{max_retries})...")
                            r = usgs_session.get(USGS_REST_URL, params=params, timeout=120)
                            
                            if r.status_code == 200:
                                stage_path = tile_path + ".tmp"
                                with open(stage_path, 'wb') as f:
                                    f.write(r.content)
                                # If another worker already completed this tile while we
                                # were downloading, discard our copy and use theirs rather
                                # than racing on the rename (Windows raises WinError 32 if
                                # two processes touch the destination at the same instant).
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
                                logging.info(f"  Downloaded: {tile_name} ({len(r.content)/1024/1024:.1f} MB)")
                                success = True
                                try:
                                    if os.path.exists(failcount_path):
                                        os.remove(failcount_path)
                                except Exception:
                                    pass
                                break
                            
                            elif r.status_code in [500, 502, 503, 504]:
                                # Server overload - use exponential backoff with jitter
                                base_wait = 2
                                wait_time = (base_wait ** attempt) + random.uniform(0, 1)
                                logging.warning(
                                    f"USGS server overloaded ({r.status_code}). "
                                    f"Retry {attempt}/{max_retries} in {wait_time:.1f}s..."
                                )
                                time.sleep(wait_time)
                            
                            elif r.status_code == 429:
                                # Rate limited - longer backoff
                                base_wait = 4
                                wait_time = (base_wait ** attempt) + random.uniform(0, 2)
                                logging.warning(
                                    f"USGS rate limit reached. "
                                    f"Retry {attempt}/{max_retries} in {wait_time:.1f}s..."
                                )
                                time.sleep(wait_time)
                            
                            else:
                                logging.error(f"USGS 1m tile request rejected: {r.status_code}")
                                break
                        
                        except requests.Timeout as e:
                            # Connection timeout or read timeout
                            if attempt < max_retries:
                                base_wait = 2
                                wait_time = (base_wait ** attempt) + random.uniform(0, 1)
                                logging.warning(
                                    f"USGS connection timeout (attempt {attempt}/{max_retries}): {type(e).__name__}. "
                                    f"Retrying in {wait_time:.1f}s..."
                                )
                                time.sleep(wait_time)
                            else:
                                logging.error(f"USGS timeout on final attempt: {e}")
                        
                        except requests.ConnectionError as e:
                            # Network/DNS error
                            if attempt < max_retries:
                                base_wait = 2
                                wait_time = (base_wait ** attempt) + random.uniform(0, 1)
                                logging.warning(
                                    f"USGS connection error (attempt {attempt}/{max_retries}): {type(e).__name__}. "
                                    f"Retrying in {wait_time:.1f}s..."
                                )
                                time.sleep(wait_time)
                            else:
                                logging.error(f"USGS connection failed on final attempt: {e}")
                        
                        except requests.HTTPError as e:
                            # HTTP error (non-200 status)
                            if attempt < max_retries:
                                base_wait = 2
                                wait_time = (base_wait ** attempt) + random.uniform(0, 1)
                                logging.warning(
                                    f"USGS HTTP error (attempt {attempt}/{max_retries}): {e}. "
                                    f"Retrying in {wait_time:.1f}s..."
                                )
                                time.sleep(wait_time)
                            else:
                                logging.error(f"USGS HTTP error on final attempt: {e}")
                        
                        except requests.RequestException as e:
                            # Catch-all for other requests library exceptions
                            if attempt < max_retries:
                                base_wait = 2
                                wait_time = (base_wait ** attempt) + random.uniform(0, 1)
                                logging.warning(
                                    f"USGS request error (attempt {attempt}/{max_retries}): {type(e).__name__}: {e}. "
                                    f"Retrying in {wait_time:.1f}s..."
                                )
                                time.sleep(wait_time)
                            else:
                                logging.error(f"USGS request failed on final attempt: {type(e).__name__}: {e}")
                        
                        except Exception as e:
                            # Unexpected error during download
                            logging.error(f"Unexpected error downloading {tile_name}: {type(e).__name__}: {e}")
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

                if not success:
                    new_count = prior_failures + 1
                    try:
                        with open(failcount_path, 'w') as ff:
                            ff.write(str(new_count))
                    except IOError as e:
                        logging.warning(f"Could not write failcount for {tile_name}: {e}")
                    except Exception as e:
                        logging.warning(f"Unexpected error writing failcount: {type(e).__name__}")
                    logging.error(
                        f"Failed to download 1m tile {tile_name} after {max_retries} attempts "
                        f"({new_count}/{MAX_TOTAL_FAILURES} total failures so far)."
                    )
                    # Brief backoff before releasing lock so waiting workers
                    # don't all retry simultaneously after a failure
                    time.sleep(5)

    return primary_path or ""

# -------------------------
# Download 10 meter USGS DEMs
# -------------------------
def download_dems(wkt_list: list, out_dir: str):
    import time
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
    MAX_TOTAL_FAILURES = 5  # across all calls/workers, before giving up on a tile for this run
    # One TCP+TLS session shared across all tiles needed in this call
    # rather than opening a new connection per tile.
    with requests.Session() as dem_session:
        for tile in needed_tiles:
            path = os.path.join(out_dir, f"USGS_13_{tile}.tif")
            lock_path = path + ".lock"
            failcount_path = path + ".failcount"

            if os.path.exists(path):
                logging.debug(f"CACHE HIT: Tile {tile} verified on disk.")
                continue

            if os.path.exists(path + ".missing"):
                logging.debug(f"Tile {tile} previously confirmed unavailable -- skipping.")
                continue

            # If another worker is already downloading this tile, wait rather than
            # starting a duplicate request -- prevents the race condition where many
            # parallel workers all see a cache miss simultaneously and redundantly
            # re-download the same 10m tile.
            #
            # Waits based on the lock file's AGE, not on how long THIS worker has
            # personally been waiting. A per-waiter elapsed-time timeout guarantees
            # an eventual duplicate attempt whenever there's a long-enough stream
            # of staggered new workers needing the same tile (e.g. a small state
            # like Delaware, whose entire footprint maps to just 1-2 DEM tiles
            # across thousands of grid cells) -- each new arrival starts its own
            # clock and will eventually give up no matter how generous the
            # timeout, even while the real download is still legitimately in
            # progress. Checking the lock's actual mtime means every worker
            # agrees on the same fact regardless of when it personally started
            # waiting, and only a genuinely abandoned lock (e.g. from a crashed
            # worker) gets reclaimed.
            STALE_LOCK_SECONDS = 240  # comfortably longer than the ~204s worst-case
                                       # full retry cycle below (3 attempts x 60s
                                       # timeout + up to 2+4=6s backoff)
            while os.path.exists(lock_path) and not os.path.exists(path):
                try:
                    lock_age = time.time() - os.path.getmtime(lock_path)
                except OSError:
                    break  # lock disappeared between the exists() check and getmtime()
                if lock_age > STALE_LOCK_SECONDS:
                    logging.warning(f"  Lock for {tile} is {lock_age:.0f}s old -- treating as abandoned, attempting own download")
                    try:
                        os.remove(lock_path)
                    except Exception:
                        pass
                    break
                time.sleep(1)

            if os.path.exists(path):
                logging.debug(f"CACHE HIT (post-wait): Tile {tile} verified on disk.")
                continue

            # Check persistent cross-call failure count before trying again. A tile
            # that has already failed MAX_TOTAL_FAILURES times (timeouts, dropped
            # connections, 5xx) across any number of prior calls/workers is marked
            # permanently unavailable for this run rather than retried forever --
            # this is what prevented n21w157 from looping for 18 hours straight.
            prior_failures = 0
            if os.path.exists(failcount_path):
                try:
                    with open(failcount_path, 'r') as ff:
                        prior_failures = int(ff.read().strip() or "0")
                except Exception:
                    prior_failures = 0
                if prior_failures >= MAX_TOTAL_FAILURES:
                    logging.error(
                        f"  Tile {tile} has failed {prior_failures} times -- "
                        f"marking unavailable for this run rather than retrying again."
                    )
                    try:
                        with open(path + ".missing", 'w') as mf:
                            mf.write(f"gave up after {prior_failures} failures")
                    except Exception:
                        pass
                    continue

            try:
                # Atomic create -- see the matching comment in
                # download_high_res_dem_tile() for why this matters.
                fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                with os.fdopen(fd, 'w') as lf:
                    lf.write(str(os.getpid()))
                won_lock = True
            except FileExistsError:
                won_lock = False
            except Exception as e:
                logging.debug(f"Non-fatal error creating lock {lock_path}: {e}")
                won_lock = True  # unexpected error -- proceed rather than risk a deadlock

            if not won_lock:
                # Another worker already won the race for this tile. The
                # earlier wait loop only catches workers that arrive AFTER the
                # lock file exists -- but under heavy parallelism, many workers
                # routinely arrive in the same instant the lock doesn't exist
                # yet, race the atomic create, and all but one lose. Without
                # this second wait, every one of those losers fell straight
                # through into its own simultaneous download attempt for the
                # same tile.
                #
                # Same lock-age-based staleness check as above -- a fixed
                # per-waiter timeout here would still guarantee an eventual
                # duplicate attempt from a continuous stream of new arrivals,
                # even if the original winner's download is still legitimately
                # in progress.
                while os.path.exists(lock_path) and not os.path.exists(path):
                    try:
                        lock_age = time.time() - os.path.getmtime(lock_path)
                    except OSError:
                        break
                    if lock_age > STALE_LOCK_SECONDS:
                        logging.warning(f"  Lock for {tile} is {lock_age:.0f}s old (after losing race) -- treating as abandoned, attempting own download")
                        try:
                            os.remove(lock_path)
                        except Exception:
                            pass
                        break
                    time.sleep(1)
                if os.path.exists(path):
                    logging.debug(f"CACHE HIT (post-second-wait): Tile {tile} verified on disk.")
                    continue

            # Real retry loop with exponential backoff, mirroring the 1m tile
            # downloader. A single attempt per call (as before) meant every
            # timeout/IncompleteRead/5xx silently ended the call and left the
            # next grid cell to start the whole download over from scratch.
            success = False
            max_retries = 3
            for attempt in range(1, max_retries + 1):
                logging.info(f"CACHE MISS: Downloading required DEM tile from USGS: {tile}... (attempt {attempt}/{max_retries})")
                try:
                    r = dem_session.get(f"{ELEVATION_SOURCE_URL}/{tile}/USGS_13_{tile}.tif", stream=True, timeout=60)
                    if r.status_code == 200:
                        stage_path = path + ".tmp"
                        with open(stage_path, 'wb') as f:
                            for chunk in r.iter_content(chunk_size=8192):
                                f.write(chunk)
                        # If another worker already completed this tile while we were
                        # downloading, discard our copy rather than racing on the rename.
                        if os.path.exists(path):
                            try:
                                os.remove(stage_path)
                            except Exception:
                                pass
                        else:
                            try:
                                os.replace(stage_path, path)
                            except (FileExistsError, PermissionError, OSError):
                                try:
                                    os.remove(stage_path)
                                except Exception:
                                    pass
                        success = True
                        # Successful download clears any accumulated failure count
                        try:
                            if os.path.exists(failcount_path):
                                os.remove(failcount_path)
                        except Exception:
                            pass
                        break
                    elif r.status_code == 404:
                        # Tile genuinely does not exist in USGS staged products (common
                        # for offshore/remote geography, e.g. parts of the Aleutians).
                        logging.warning(f"  Tile {tile} not found at USGS (404) -- marking as unavailable.")
                        try:
                            with open(path + ".missing", 'w') as mf:
                                mf.write("404")
                        except Exception:
                            pass
                        success = True  # not a failure to retry -- a confirmed absence
                        break
                    elif r.status_code in (500, 502, 503, 504):
                        wait_time = 2 ** attempt
                        logging.warning(
                            f"  USGS server overloaded ({r.status_code}) for {tile}. "
                            f"Retry {attempt}/{max_retries} in {wait_time}s..."
                        )
                        time.sleep(wait_time)
                    else:
                        logging.error(f"  Unexpected response downloading {tile}: HTTP {r.status_code}")
                        break
                except Exception as e:
                    wait_time = 2 ** attempt
                    logging.error(
                        f"Failed to download {tile}: {e}. "
                        f"Retry {attempt}/{max_retries} in {wait_time}s..."
                    )
                    time.sleep(wait_time)

            if not success:
                # Exhausted retries this call. Increment the persistent failure
                # count so subsequent calls (possibly from other workers) know
                # how close this tile is to being given up on entirely.
                new_count = prior_failures + 1
                try:
                    with open(failcount_path, 'w') as ff:
                        ff.write(str(new_count))
                except Exception:
                    pass
                logging.error(
                    f"  Tile {tile}: exhausted {max_retries} retries this call "
                    f"({new_count}/{MAX_TOTAL_FAILURES} total failures so far)."
                )

            try:
                if os.path.exists(lock_path):
                    os.remove(lock_path)
            except Exception:
                pass

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
    are used to disambiguate when MULTIPLE candidates spatially qualify
    within snap_tol of the same chain endpoint -- which happens routinely
    on densely-subdivided streets where several short segments (e.g.
    80-280ft each) cluster closer together than snap_tol itself. Picking
    purely the closest spatial match in that situation can attach the
    wrong neighbor, producing a chain that weaves back and forth instead
    of running straight (confirmed on Q Street NW, route 11072862). When
    MP info is available, the candidate whose own milepost range is the
    closest continuation of the chain's accumulated range is preferred
    over one that's merely spatially closer.

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

            # Collect ALL spatially-qualifying candidates across every
            # remaining segment and orientation, rather than taking the
            # first match found -- needed so the MP tie-break below can
            # actually choose among real alternatives when more than one
            # candidate is within snap_tol.
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

                # A genuinely MP-adjacent candidate (an exact continuation,
                # mp_gap == 0) is always grown into the chain, even if the
                # chain's chord already happens to be within snap_tol --
                # which routinely happens after only 1-2 segments on a
                # small-radius loop (e.g. Dupont Circle), long before the
                # ring is actually complete. The closed-ring stop only
                # applies once there's no true next-neighbor left to add.
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


def merge_close_chunks(chunks: List[dict], merge_tol_ft: float = 500.0, route_id: str = None) -> List[dict]:
    """
    Second-pass cleanup for chunks built from stitch_linestrings_ordered().

    Each chunk dict must have a "geom" key (a LineString already oriented so
    its coordinates run from low milepost to high milepost) and
    "chunk_low_mp"/"chunk_high_mp" keys.

    stitch_linestrings_ordered() only bridges segments whose endpoints land
    within its snap_tol (~330 ft) of each other. Real routes occasionally
    fail to bridge by a small margin just over that tolerance -- most
    visibly on closed-loop routes (e.g. the roadway around a traffic
    circle), where the route's own start and end point are digitized a few
    hundred feet apart instead of coinciding, splitting what should be one
    continuous ring into 2-3 separate chunks that each get their own
    (overlapping) milepost range.

    Like stitch_linestrings_ordered, this tests all four endpoint
    combinations (tail-to-head, tail-to-tail, head-to-tail, head-to-head)
    rather than only tail-to-head -- the earlier tail-to-head-only version
    would, on a reverse-digitized chunk, draw a straight line across the
    gap to the wrong end instead of bridging through the correct geometry,
    producing a visible zigzag in the output (most noticeably on routes
    like the Logan Circle loop).

    This repeatedly looks for the closest remaining pair of chunk endpoints
    (in any of the 4 orientations) and merges them if within merge_tol_ft,
    continuing until no pair qualifies. It only touches chunks of the same
    route that are already this close -- routes with genuinely separate
    physical pieces (e.g. county-line resets, typically miles apart) are
    far outside merge_tol_ft and are never affected.

    Note: merging via a head-to-X orientation can in principle produce a
    chunk whose path is not a simple straight low-to-high line (e.g. a
    real Y-junction or out-and-back spur, where both chunks' high ends
    meet at a shared point). This is an inherent property of order-
    independent spatial stitching rather than a defect introduced here,
    and is expected to be rare relative to the bug this fixes.

    route_id, if provided, is only used to label the diagnostic log line
    emitted when chunks remain unmerged -- it logs the actual raw gap
    distance this function evaluated (pre-densification/pre-smoothing),
    since that distance can differ from a gap measured later against the
    smoothed/densified output coordinates.
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
            chunks = [c for k, c in enumerate(chunks) if k not in (i, j)]
            chunks.append(merged_chunk)
            merged_any = True

    if len(chunks) > 1:
        # Diagnostic: report the actual closest raw gap (across all 4
        # endpoint orientations) among whatever chunks are left, so it's
        # possible to tell whether this function correctly declined to
        # merge (gap genuinely > merge_tol_ft) or something else is going
        # on, without guessing from smoothed output coordinates after the
        # fact.
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
            except Exception as e:
                logging.debug(f"DEM sampling error at ({lon}, {lat}): {e}")
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

def calculate_iri_proxy(z_raw: np.ndarray, z_smooth: np.ndarray, spacing_m: float) -> np.ndarray:
    """
    Calculates a continuous pavement roughness proxy (micro-jitter)
    by applying a Butterworth high-pass filter to the elevation residuals.
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
        
    # Return the raw jitter amplitude converted to inches (standard IRI metric)
    return jitter * FEET_PER_METER * 12.0

def calculate_available_sight_distance(z_smooth: np.ndarray, spacing_m: float) -> np.ndarray:
    """
    Vectorized Available Sight Distance (ASD) calculation.
    Uses AASHTO standards: 3.5 ft driver eye height, 2.0 ft object height.
    Projects forward up to a maximum search distance of 1500 ft.

    Replaces the original double-loop implementation which allocated new
    numpy arrays (k_indices, dist_to_k, ray_z) inside the inner loop for
    every point pair. At 100M+ vertices with a ~300-point search window that
    created enormous temp-array churn and was the dominant per-vertex CPU
    cost in the pipeline. This version scans forward from each source point
    using a running maximum obstruction slope -- no per-iteration allocation.
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
            continue

        # Distances and elevation of all points ahead in the search window
        ahead = np.arange(1, end_search - i)
        dists = ahead * spacing_m
        target_z = z_smooth[i + 1:end_search] + h_obj_m

        # Slope from eye to each potential target
        slopes_to_target = (target_z - eye_z) / dists

        # Slope from eye to each intermediate terrain point (same array --
        # the terrain point and target point are the same here because we
        # test obstruction incrementally: the sight line is blocked as soon
        # as the required slope to see the terrain point exceeds the
        # current line-of-sight slope to the target).
        # Use cumulative max of terrain slopes to find the first blockage.
        terrain_slopes = (z_smooth[i + 1:end_search] - eye_z) / dists
        cum_max_obstruction = np.maximum.accumulate(terrain_slopes)

        # Sight line to target j is clear if slopes_to_target[j] >=
        # cum_max_obstruction[j] (the line of sight clears all intermediate
        # terrain). Find the last j where this holds.
        clear = slopes_to_target >= cum_max_obstruction
        if np.any(clear):
            last_clear = int(np.where(clear)[0][-1])
            asd_ft[i] = dists[last_clear] * FEET_PER_METER

    return asd_ft

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
    is_curve = kappa >= kappa_thresh
    
    trim_thresh = kappa_thresh * 1.05 
    
    min_len = params['H_MIN_CURVE_LENGTH_M']
    min_delta = params['H_MIN_DELTA']
    
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
        direction = "Right" if sgn > 0 else "Left"
        
        # CLOTHOID DETECTOR: Isolate the central circular curve (SC to CS)
        peak_idx = s + int(np.argmax(seg_kappa))
        max_k = seg_kappa[peak_idx - s]
        
        # A central circular curve maintains a near-constant maximum curvature.
        # We bracket the central curve where curvature is >= 85% of peak.
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
        
        deg_per_100 = METRIC_R_TO_IMPERIAL_D / radius if radius > 0 else 0
        cbin = classify_bin(deg_per_100)
        direction = "Right" if sgn > 0 else "Left"
        
        # AASHTO Heuristic: Estimate Superelevation based on imperial radius
        r_ft = radius * FEET_PER_METER
        if r_ft < 1500: e_pct = 8.0         # Tight curve, max banking
        elif r_ft < 3000: e_pct = 6.0
        elif r_ft < 6000: e_pct = 4.0
        elif r_ft < 99999: e_pct = 2.0      # Broad curve, normal crown
        else: e_pct = 0.0                   # Tangent

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
            'Superelevation_Pct': e_pct,
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

            # Update the end of the central circular curve to the end of the merged compound curve
            if 'CS_Dist' in nxt:
                cur['CS_Dist'] = nxt['CS_Dist']
            
            # Preserve spiral status if either constituent curve was a spiral
            if cur.get('Transition_Type') == 'Spiral' or nxt.get('Transition_Type') == 'Spiral':
                cur['Transition_Type'] = 'Spiral'
            
            # Recalculate AASHTO banking based on the new, blended radius
            r_ft_merged = cur['Radius_m'] * FEET_PER_METER
            if r_ft_merged < 1500: cur['Superelevation_Pct'] = 8.0
            elif r_ft_merged < 3000: cur['Superelevation_Pct'] = 6.0
            elif r_ft_merged < 6000: cur['Superelevation_Pct'] = 4.0
            elif r_ft_merged < 99999: cur['Superelevation_Pct'] = 2.0
            else: cur['Superelevation_Pct'] = 0.0
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
    
    is_vc = np.abs(gchg) > params['V_VC_THRESHOLD']
    trim_thresh = params['V_VC_THRESHOLD'] * 1.10
    
    min_len = params['V_MIN_CURVE_LENGTH']
    min_g_change = params['V_MIN_GRADE_CHANGE']
    
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
            'Length_ft': length * FEET_PER_METER,
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
        """Return a structured failure payload instead of silent None.
        Callers that previously checked `if res is None` will still work
        since they can also check `if not res` or `if res is None`, but
        now any caller can also log res['error'] and identify which route
        and chunk caused the failure -- critical for post-parallel diagnosis.
        """
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
    if len(coords_m) < max(params['H_BASE_SMOOTH_WINDOW'] + 2, 8):
        return _fail(f"too few densified points ({len(coords_m)}) for smoothing window")
        
    x_raw = np.array([c[0] for c in coords_m])
    y_raw = np.array([c[1] for c in coords_m])
    
    s_factor_h = params.get("H_SMOOTH_FACTOR", 400)
    s_factor_v = params.get("V_SMOOTH_FACTOR", 1400)

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
    coords_wgs_raw = [rev.transform(x, y) for x, y in coords_m]  # raw, pre-spline

    if params.get("HIGH_RES_MODE", False):
        import rasterio

        # Group coordinates by their required 1-degree tile, open each tile
        # exactly once, and sample all of that tile's coordinates in a single
        # bulk call rather than one rasterio.sample() call per coordinate.
        # This was the major perf win applied 2026-06-22 -- re-opening the
        # same tile file and re-seeking for every single point was the
        # dominant cost on dense route geometry.
        tile_to_coords = {}
        for i, (lon, lat) in enumerate(coords_wgs_raw):
            tile_name = get_1deg_tile_name(lon, lat)
            if tile_name not in tile_to_coords:
                tile_to_coords[tile_name] = []
            tile_to_coords[tile_name].append((i, (lon, lat)))

        z_raw_list = [np.nan] * len(coords_wgs_raw)

        for tile_name, indexed_pts in tile_to_coords.items():
            tile_path = os.path.join(dem_dir, f"tile_1m_{tile_name}.tif")
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
            else:
                logging.debug(f"Tile {tile_name} not found on disk.")

        z_raw = np.array(z_raw_list, dtype=float)

    else:
        z_raw = get_elevations(coords_wgs_raw, dem_dir)   # ← raw coords here too

    z_filled = pd.Series(z_raw).interpolate(limit_direction='both').to_numpy()
    
    # =========================================================
    # PREVENT FATAL WORKER CRASH 
    # SciPy's Fortran FITPACK backend will segfault and nuke the
    # process if it attempts to smooth an array containing NaNs.
    # =========================================================
    if np.isnan(z_filled).any():
        remaining_nans = np.sum(np.isnan(z_filled))
        total_vertices = len(z_filled)
        pct_nans = 100.0 * remaining_nans / total_vertices if total_vertices > 0 else 0.0
        warning_msg = (
            f"NaNs remained in elevation array after interpolation ({remaining_nans}/{total_vertices} vertices, "
            f"{pct_nans:.1f}%). Skipping chunk to prevent FITPACK segfault. "
            f"Check DEM coverage near route {route_id!r} MP range [{chunk_s_mp:.2f}, {chunk_e_mp:.2f}]."
        )
        logging.warning(warning_msg)
        return _fail(warning_msg)
    
    structure_mask = None
    if route_id is not None:
        total_len = max(d_axis[-1], 1e-9)
        mileposts = chunk_s_mp + (d_axis / total_len) * (chunk_e_mp - chunk_s_mp)
        
        structure_mask = build_structure_mask(
            mileposts=mileposts,
            coords_wgs=coords_wgs_smooth,
            route_id=route_id,
            hpms_structures=hpms_subset,
            nbi_nti_gdf=nbi_nti_gdf,
            spatial_tolerance_deg=0.00015
        )
    
    z_fixed, structure_tier_out = fix_profile_by_deviation(z_filled, spacing_m, params, structure_mask)
    
    # ====================================================================
    # NBI VERTICAL CLEARANCE PROFILE CONSTRAINT ANCHORS
    # ====================================================================
    if nbi_nti_gdf is not None and not nbi_nti_gdf.empty and "vertical_clearance" in nbi_nti_gdf.columns:
        nbi_indices = np.where(structure_tier_out == 2)[0]
        if len(nbi_indices) > 0:
            nbi_pts = [Point(coords_wgs_smooth[idx]) for idx in nbi_indices]
            vtx_gdf = gpd.GeoDataFrame(index=nbi_indices, geometry=nbi_pts, crs="EPSG:4326")
            
            # Find closest structure point within a tight spatial threshold (max ~70 feet tolerance)
            joined = gpd.sjoin_nearest(vtx_gdf, nbi_nti_gdf, how="left", max_distance=0.0002)
            
            for idx, row in joined.iterrows():
                v_clr = row.get("vertical_clearance")
                if pd.notna(v_clr):
                    try:
                        v_clr_str = str(v_clr).strip().replace('.', '')
                        # Parse official NBI format (4-character strings representing meters, e.g., '0435' = 4.35m)
                        if len(v_clr_str) == 4 and v_clr_str.isdigit():
                            v_clr_ft = (float(v_clr_str) / 100.0) * FEET_PER_METER
                        else:
                            v_clr_ft = float(v_clr)
                            
                        if v_clr_ft > 0:
                            # Target deck elevation = Bottom of gorge ground + structural clearance + 4ft deck thickness thickness
                            crossroad_ground_m = z_filled[idx]
                            clearance_m = v_clr_ft / FEET_PER_METER
                            deck_thickness_m = 4.0 / FEET_PER_METER
                            
                            target_deck_z_m = crossroad_ground_m + clearance_m + deck_thickness_m
                            z_fixed[idx] = target_deck_z_m
                    except Exception:
                        pass
    # ====================================================================
    
    sz = UnivariateSpline(d_axis, z_fixed, s=s_factor_v)
    z_smooth = sz(d_axis)
    
    h_unwrapped = calculate_headings(coords_m_smooth)
    h_sm = np.degrees(safe_savgol(np.radians(h_unwrapped), params['H_BASE_SMOOTH_WINDOW'], 2))
    iri_proxy = calculate_iri_proxy(z_filled, z_smooth, spacing_m)
    asd_array = calculate_available_sight_distance(z_smooth, spacing_m)

    return {
        "ok": True,
        "spacing_m": spacing_m,
        "d_axis": d_axis,
        "coords_m_smooth": coords_m_smooth,
        "coords_wgs_smooth": coords_wgs_smooth,
        "z_smooth": z_smooth,
        "headings_unwrapped_smooth_deg": h_sm,
        "coords_wgs_raw": [rev.transform(x, y) for x, y in zip(x_raw, y_raw)],
        "z_raw": z_filled,
        "structure_tier": structure_tier_out,
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

def query_metric_kdtree(tree: cKDTree, tx: Transformer, query_coords_wgs: List[Tuple[float, float]]) -> np.ndarray:
    q_m = np.array([tx.transform(x, y) for x, y in query_coords_wgs], dtype=float)
    _, idx = tree.query(q_m)
    return idx

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
            self.root.attributes("-topmost", True)  # Forces it to float above other windows
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
