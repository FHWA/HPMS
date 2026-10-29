# apps/hpms_4d_enricher_cli.py

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
RAT 4D ENRICHER ENGINE v3.3 (National Batch Processor)
--------------------------------------------------------------------------------
ROLE: Upgrades 2D HPMS tables into 3D geometries with linear referencing.
DESCRIPTION:
Builds each route's smoothed 3D profile the way rat_alignment_cli.py does
(continuity chunks, long chunks split into padded pieces processed in
parallel, 1 m DEM tiles from the shared align_1m_cache), then uses a metric
KDTree to place every vertex of the route's HPMS records on that profile.
Each record is placed once, against its whole route. Outputs WKT_ZM strings
containing Longitude, Latitude, Elevation (Z, meters) and Milepost (M) for
GIS and 3D applications.

Raw DEM fallback. Where a route cannot be smoothed (usually because its
geometry is too short for the spline), its segments are enriched by
sampling the cached 1 m DEM directly at each original vertex, with mileposts
interpolated from Start_MP and End_MP. Only segments with no result from
the smoothed pipeline are filled this way. The Elevation_Source column
reads "smoothed" or "raw_fallback" so the two can be told apart.

CREATED BY: Federal Highway Administration, Office of Highway Policy Information.
VERSION: 3.3, October 29, 2026
"""
import os
import sys
import json
import logging
import math
import warnings
from datetime import datetime
import numpy as np
import pandas as pd
import geopandas as gpd
from shapely.wkt import loads
from shapely.geometry import shape, LineString
from pyproj import Transformer
from concurrent.futures import ProcessPoolExecutor

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
RAT_SUITE_DIR = os.path.dirname(THIS_DIR)
if RAT_SUITE_DIR not in sys.path:
    sys.path.insert(0, RAT_SUITE_DIR)

from core.rat_core import (
    build_params,
    build_route_continuity_chunks,
    route_spans_several_fsystems,
    log_fsystem_summary,
    describe_smoothing_factors,
    split_chunks_for_parallelism,
    ensure_dem_cells_cached,
    smooth_plan_profile_from_linestring,
    profile_result_ok,
    build_metric_kdtree,
    query_metric_kdtree,
    fetch_socrata_state,
    load_local_hpms,
    filter_local_df_to_state,
    apply_facility_fsystem_filters,
    fetch_nbi_nti_state,
    SOCRATA_DEFAULT,
    SOCRATA_TOKEN as ENV_SOCRATA_TOKEN,
    compute_padded_bbox,
    get_appropriate_utm_zone,
    init_worker_nbi,
    run_bounded_parallel,
    DemCacheUnavailable,
    dem_cache_reachable,
    claim_output_version,
    FEET_PER_METER,
    release_output_version,
    raise_if_dem_cache_lost,
)
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s: %(message)s")


# ===========================================================================
# CONFIGURATION
# ===========================================================================

# List of 2-digit State FIPS codes to process. Use ["ALL"] to process the US.
# OR, leave empty [] and provide a LOCAL_FILE_PATH below to process a single CSV.
STATES_TO_PROCESS = ["31"]

LOCAL_FILE_PATH = ""
OUTPUT_DIR = ""   # Set to your output directory, e.g. r"C:\Output\RAT"
DEM_DIR    = ""   # Set to your DEM cache directory

SOCRATA_TOKEN = ""                 # blank = the RAT_SOCRATA_TOKEN environment variable
SOCRATA_URL   = SOCRATA_DEFAULT    # HPMS dataset on the U.S. DOT data portal

# Facility type and functional system filters, applied to the Socrata query
# or to a local file (the same filters the alignment applies).
# HPMS facility types: 1 = One-Way Roadway, 2 = Two-Way Roadway, 4 = Ramp,
# 5 = Non-Mainline, 6 = Non-Inventory Direction, 7 = Planned/Unbuilt.
# Default includes mainline roadways only (types 1 and 2).
FACILITY_TYPE_FILTER = [1, 2]
FSYSTEM_FILTER       = []     # Empty list = all functional systems
# Records with no facility type / no functional system are processed only
# when these are True, as facility type 2 / functional class 7. The output
# keeps them blank, as submitted.
FACILITY_TYPE_INCLUDE_MISSING = False
FSYSTEM_INCLUDE_MISSING       = False

# User-selected field subset from GUI (None = full pass-through)
SELECTED_HPMS_FIELDS = None

# When True, all HPMS source attributes are preserved in the enriched output.
# Overridden at runtime by INCLUDE_HPMS_FIELDS from run_params.json when
# invoked by the GUI.
INCLUDE_ALL_HPMS_FIELDS = True

# Paths to RAT alignment outputs for curve/grade field replacement:
# alignment_horizontal_*.csv for CURVES_A–F and alignment_grades_*.csv for
# GRADES_A–F. Leave empty to keep the HPMS-reported values.
HORIZONTAL_CSV = ""
GRADES_CSV     = ""

# Spatial outputs besides the CSV (set by the GUI's GeoPackage and
# Shapefile boxes when run from the GUI): a GeoPackage with 3D geometry, and
# a shapefile projected to UTM for Blender and other 3D tools.
OUT_GPKG = True
OUT_SHP  = True

# Records that get no elevations (no smoothed profile and no DEM values) are
# left out of the outputs. The count is always logged; above this percent of
# the state's records it is logged as an ERROR.
MAX_UNENRICHED_PCT = 5.0

# A record vertex farther than this from every point of its route's smoothed
# profile is on a piece of the route that could not be smoothed (the same
# distance as the chunk join tolerance); the record then gets raw DEM
# elevations instead (enrich_segment_raw_dem).
PROFILE_MAX_DIST_FT = 100.0

# Number of CPU cores to use for parallel route enrichment.
# Reduce if your computer becomes unresponsive during processing.
MAX_WORKERS = max(1, os.cpu_count() - 2)

# ===========================================================================

ALL_FIPS = [
    "01", "02", "04", "05", "06", "08", "09", "10", "11", "12",
    "13", "15", "16", "17", "18", "19", "20", "21", "22", "23",
    "24", "25", "26", "27", "28", "29", "30", "31", "32", "33",
    "34", "35", "36", "37", "38", "39", "40", "41", "42", "44",
    "45", "46", "47", "48", "49", "50", "51", "53", "54", "55", "56", "72"
]

# Settings read from run_params.json when run from the GUI (smoothing
# factors, chunking and the other core parameters); empty otherwise.
RUN_PARAMS = {}

# Bin label sets for horizontal and vertical replacement
# HPMS field names (lower case, as Socrata returns them). The RAT values are
# written into these same fields, matched without regard to case.
H_BINS = ["curves_a", "curves_b", "curves_c", "curves_d", "curves_e", "curves_f"]
V_BINS = ["grades_a", "grades_b", "grades_c", "grades_d", "grades_e", "grades_f"]


# ---------------------------------------------------------------------------
# Curve / Grade Field Replacement
# ---------------------------------------------------------------------------
def replace_curve_grade_fields(
    out_df: pd.DataFrame,
    horizontal_csv: str,
    grades_csv:     str,
) -> pd.DataFrame:
    """
    Replaces HPMS-reported CURVES_A–F and GRADES_A–F fields with values
    derived from the RAT alignment output. The values go into the HPMS
    fields themselves (matched without regard to case; added in lower case
    if the data has none), so the output never carries both the HPMS and
    the RAT versions of a field.

    For each HPMS segment the function finds all RAT horizontal curves (or
    grade segments) whose calibrated milepost range overlaps [Start_MP,
    End_MP], clips the overlap to the segment bounds, and apportions the
    length in miles to the class. CURVES_A receives the remainder after the
    B–F curves, so tangents count as class A. Grade segments cover the
    whole route, so GRADES_A is their own class A length plus any part of
    the section they do not reach.

    The sum of CURVES_A–F (and of GRADES_A–F) for any segment equals the
    section length (End_MP – Start_MP) to floating-point precision.

    Parameters
    ----------
    out_df          : enriched HPMS DataFrame with Start_MP and End_MP columns
    horizontal_csv  : path to RAT horizontal curve CSV
    grades_csv      : path to RAT grade segment CSV

    Returns
    -------
    (out_df, replaced) : out_df with CURVES_A–F and/or GRADES_A–F replaced,
    and the names of the columns that were replaced.
    """
    replaced = []

    def _apply_replacement(df: pd.DataFrame, curves: pd.DataFrame,
                           bin_col: str, bins: list) -> pd.DataFrame:
        """
        Overlap apportionment shared by curves and grades, matched within
        each RouteId.
        bin_col : column in curves holding the class letter
        bins    : output column names [CLASS_A, CLASS_B, ... CLASS_F]
        """
        bin_letters = ["A", "B", "C", "D", "E", "F"]
        existing = {c.lower(): c for c in df.columns}
        bins = [existing.get(b.lower(), b) for b in bins]
        bin_map = dict(zip(bin_letters, bins))
        for col in bins:
            df[col] = 0.0
        replaced.extend(bins)

        by_route = {}
        all_items = None
        use_route = "RouteId" in curves.columns and "RouteId" in df.columns
        if use_route:
            for rid, grp in curves.groupby("RouteId"):
                by_route[rid] = (grp["Calibrated_Start_MP"].to_numpy(dtype=float),
                                 grp["Calibrated_End_MP"].to_numpy(dtype=float),
                                 grp[bin_col].astype(str).to_numpy())
        else:
            all_items = (curves["Calibrated_Start_MP"].to_numpy(dtype=float),
                         curves["Calibrated_End_MP"].to_numpy(dtype=float),
                         curves[bin_col].astype(str).to_numpy())

        for idx, row in df.iterrows():
            s_mp = float(row["Start_MP"])
            e_mp = float(row["End_MP"])
            seg_len = e_mp - s_mp
            if seg_len <= 0:
                continue
            items = by_route.get(row["RouteId"]) if use_route else all_items
            totals = {col: 0.0 for col in bins}
            if items is not None:
                c_start, c_end, c_bin = items
                overlaps = np.maximum(0.0, np.minimum(c_end, e_mp) - np.maximum(c_start, s_mp))
                for letter in bin_letters[1:]:
                    totals[bin_map[letter]] = float(overlaps[c_bin == letter].sum())
            non_a_total = sum(totals[bin_map[x]] for x in bin_letters[1:])
            totals[bins[0]] = max(0.0, seg_len - non_a_total)
            for col, val in totals.items():
                df.at[idx, col] = round(val, 6)

        return df

    # --- Horizontal replacement ---
    if horizontal_csv and os.path.exists(horizontal_csv):
        try:
            h_df = pd.read_csv(horizontal_csv, low_memory=False)
            required_h = {"Calibrated_Start_MP", "Calibrated_End_MP", "Bin"}
            if required_h.issubset(set(h_df.columns)):
                # Filter to routes present in out_df if RouteId is available
                if "RouteId" in h_df.columns and "RouteId" in out_df.columns:
                    h_df = h_df[h_df["RouteId"].isin(out_df["RouteId"].unique())]
                out_df = _apply_replacement(out_df, h_df, "Bin", H_BINS)
                logging.info(
                    f"Horizontal curve fields replaced using RAT output: "
                    f"{os.path.basename(horizontal_csv)}"
                )
            else:
                missing = required_h - set(h_df.columns)
                logging.warning(
                    f"Horizontal CSV missing required columns {missing}. "
                    "CURVES_A–F not replaced."
                )
        except Exception as e:
            logging.error(f"Failed to apply horizontal curve replacement: {e}")

    # --- Grade replacement ---
    if grades_csv and os.path.exists(grades_csv):
        try:
            g_df = pd.read_csv(grades_csv, low_memory=False)
            required_g = {"Calibrated_Start_MP", "Calibrated_End_MP", "Grade_Bin"}
            if required_g.issubset(set(g_df.columns)):
                if "RouteId" in g_df.columns and "RouteId" in out_df.columns:
                    g_df = g_df[g_df["RouteId"].isin(out_df["RouteId"].unique())]
                out_df = _apply_replacement(out_df, g_df, "Grade_Bin", V_BINS)
                logging.info(
                    f"Grade fields replaced using RAT output: "
                    f"{os.path.basename(grades_csv)}"
                )
            else:
                missing = required_g - set(g_df.columns)
                logging.warning(
                    f"Grades CSV missing required columns {missing}. "
                    "GRADES_A–F not replaced. Use the alignment_grades_*.csv file."
                )
        except Exception as e:
            logging.error(f"Failed to apply grade replacement: {e}")

    return out_df, replaced


# ---------------------------------------------------------------------------
# Worker: smoothed profile of one work item
# ---------------------------------------------------------------------------
def process_4d_item(route_id: str, item: dict, item_subset: pd.DataFrame, cache_dir: str, params: dict):
    """
    Smoothed 3D profile of one work item: a whole continuity chunk, or one
    padded piece of a long chunk (core.rat_core.split_chunks_for_parallelism),
    as the alignment does. The profile is cut to the item's own (unpadded)
    milepost range, so the pieces of a route do not overlap.

    Returns (route_id, coords_wgs, z_m), or None if the item could not be
    smoothed. NBI/NTI structures come from core.rat_core.WORKER_NBI_NTI_GDF,
    set once per process by init_worker_nbi().
    """
    from core.rat_core import download_dem_cell, get_line_dem_cells, WORKER_NBI_NTI_GDF
    try:
        # Normally already cached by the state-wide download.
        for lon_idx, lat_idx in get_line_dem_cells([item["geom"]]):
            download_dem_cell(lon_idx, lat_idx, cache_dir)
        res = smooth_plan_profile_from_linestring(
            item["geom"], cache_dir, params, item["f_sys"],
            route_id=route_id, chunk_s_mp=item["calib_start_mp"], chunk_e_mp=item["calib_end_mp"],
            hpms_subset=item_subset, nbi_nti_gdf=WORKER_NBI_NTI_GDF,
        )
    except Exception as e:
        # A lost DEM folder stops the run (DemCacheUnavailable reaches the
        # main process) rather than failing every remaining task.
        raise_if_dem_cache_lost(cache_dir, e)
        logging.error(f"Worker failed on route {route_id} Part {item.get('part_id')}: {e}")
        return None
    if not profile_result_ok(res):
        # A missing folder makes elevation reads fail quietly.
        raise_if_dem_cache_lost(cache_dir)
        return None

    d_axis = np.asarray(res["d_axis"], dtype=float)
    total_len = max(float(d_axis[-1]), 1.0)
    mp = item["calib_start_mp"] + d_axis / total_len * (item["calib_end_mp"] - item["calib_start_mp"])
    keep = (mp >= item["emit_start_mp"]) & (mp <= item["emit_end_mp"])
    if not keep.any():
        return None
    coords = [c for c, k in zip(res["coords_wgs_smooth"], keep) if k]
    z_m = np.asarray(res["z_smooth"], dtype=float)[keep]
    return route_id, coords, z_m


# ---------------------------------------------------------------------------
# Worker: HPMS records of one route placed on its profile
# ---------------------------------------------------------------------------
def map_route_to_profile(sub: pd.DataFrame, coords_wgs: list, z_m: np.ndarray) -> dict:
    """
    Gives every vertex of each HPMS record of one route the elevation of
    the nearest point of the route's smoothed profile (metric KDTree), and
    a milepost interpolated from the record's Start_MP and End_MP by
    distance along the record. Each record is placed once, against the
    whole route's profile. A record with a vertex more than
    PROFILE_MAX_DIST_FT from every smoothed point lies on a piece of the
    route that could not be smoothed; it is left for the raw DEM fallback
    rather than given elevations from another piece of the route.

    Returns {row index: {"geometry_3d": LineString Z, "WKT_ZM": str}}.
    """
    results = {}
    tree, tx = build_metric_kdtree(coords_wgs)

    for row_idx, row in sub.iterrows():
        try:
            g = loads(row["WKT"]) if isinstance(row["WKT"], str) else shape(row["WKT"])
            if g.is_empty:
                continue

            parts = list(g.geoms) if g.geom_type == "MultiLineString" else [g]

            total_geom_len_m = 0.0
            for part in parts:
                coords = list(part.coords)
                for i in range(1, len(coords)):
                    x1, y1 = tx.transform(*coords[i-1][:2])
                    x2, y2 = tx.transform(*coords[i][:2])
                    total_geom_len_m += math.hypot(x2 - x1, y2 - y1)

            if total_geom_len_m == 0:
                total_geom_len_m = 1e-9

            current_len_m = 0.0
            row_s_mp      = float(row["Start_MP"])
            row_e_mp      = float(row["End_MP"])

            xyz      = []
            xyzm_txt = []

            for part in parts:
                raw_coords = [c[:2] for c in part.coords]
                q_idx, q_dist = query_metric_kdtree(tree, tx, raw_coords, return_dist=True)
                if np.max(q_dist) * FEET_PER_METER > PROFILE_MAX_DIST_FT:
                    raise _OffProfile
                z_assigned = z_m[q_idx]

                for i, (lon, lat) in enumerate(raw_coords):
                    if i > 0:
                        prev_lon, prev_lat = raw_coords[i-1]
                        curr_x,  curr_y   = tx.transform(lon, lat)
                        prev_x,  prev_y   = tx.transform(prev_lon, prev_lat)
                        current_len_m    += math.hypot(curr_x - prev_x, curr_y - prev_y)

                    f = current_len_m / total_geom_len_m
                    m = row_s_mp + f * (row_e_mp - row_s_mp)
                    z = z_assigned[i]

                    xyz.append((lon, lat, float(z)))
                    xyzm_txt.append(f"{lon:.7f} {lat:.7f} {float(z):.2f} {m:.4f}")

            results[row_idx] = {
                "geometry_3d": LineString(xyz),
                "WKT_ZM":      f"LINESTRING ZM ({', '.join(xyzm_txt)})",
            }
        except _OffProfile:
            continue
        except Exception:
            continue

    return results


class _OffProfile(Exception):
    """A record not covered by the route's smoothed profile."""


# ---------------------------------------------------------------------------
# Raw DEM fallback
# ---------------------------------------------------------------------------
def enrich_segment_raw_dem(row: pd.Series, dem) -> dict | None:
    """
    Fallback for an HPMS record on a piece of route that could not be
    smoothed (usually a very short piece), or on a route with no smoothed
    profile at all. Elevations are sampled directly from the
    cached 1 m DEM tiles (dem: core.rat_cross_section.DemTiles) at each
    original vertex; mileposts are interpolated from Start_MP and End_MP.
    Gaps in the DEM are filled along the record. Returns a result in the
    same form as map_route_to_profile(), or None if the geometry cannot be
    read or no DEM value is found.
    """
    try:
        g = loads(row["WKT"]) if isinstance(row["WKT"], str) else shape(row["WKT"])
        if g.is_empty:
            return None

        parts = list(g.geoms) if g.geom_type == "MultiLineString" else [g]

        all_coords = []
        part_starts = set()
        for part in parts:
            part_starts.add(len(all_coords))
            all_coords.extend(c[:2] for c in part.coords)

        if len(all_coords) < 2:
            return None

        lon_arr = np.array([c[0] for c in all_coords], dtype=float)
        lat_arr = np.array([c[1] for c in all_coords], dtype=float)
        # Distance along the record in meters (UTM), so mileposts are
        # spaced by true length whatever the record's direction.
        utm = get_appropriate_utm_zone(lon_arr[0], lat_arr[0])
        x_m, y_m = Transformer.from_crs("EPSG:4326", f"EPSG:{utm}", always_xy=True).transform(lon_arr, lat_arr)
        # The jump between the parts of a multipart record is not counted,
        # as on the smoothed path (distance along the line work only).
        steps = np.hypot(np.diff(x_m), np.diff(y_m))
        for k in part_starts - {0}:
            steps[k - 1] = 0.0
        cum_dists = np.concatenate([[0.0], np.cumsum(steps)])

        # Gaps in the DEM are filled by straight-line interpolation along the
        # record by distance; vertices before the first or after the last
        # value take that value.
        z_vals = np.asarray(dem.sample(lon_arr, lat_arr), dtype=float)
        ok = np.isfinite(z_vals)
        if not ok.any():
            return None
        if not ok.all():
            z_vals = np.interp(cum_dists, cum_dists[ok], z_vals[ok])

        row_s_mp = float(row["Start_MP"])
        row_e_mp = float(row["End_MP"])

        total_dist = cum_dists[-1] if cum_dists[-1] > 0 else 1e-9

        xyz      = []
        xyzm_txt = []

        for i, (lon, lat) in enumerate(all_coords):
            f = cum_dists[i] / total_dist
            m = row_s_mp + f * (row_e_mp - row_s_mp)
            z = float(z_vals[i])
            xyz.append((lon, lat, z))
            xyzm_txt.append(f"{lon:.7f} {lat:.7f} {z:.2f} {m:.4f}")

        return {
            "geometry_3d": LineString(xyz),
            "WKT_ZM":      f"LINESTRING ZM ({', '.join(xyzm_txt)})",
        }

    except Exception:
        return None


def run_state_enrichment(state_fips: str, local_df: pd.DataFrame = None, display_fips: str = None):
    """
    Enriches one state the way the alignment processes it: each route is
    built into continuity chunks once, long chunks are split into padded
    pieces, and the pieces are smoothed in parallel against the shared 1 m
    DEM cache. The HPMS records of each route are then placed on the
    route's profile, and records on routes that could not be smoothed take
    their elevations directly from the DEM.

    The output version is claimed when the run starts
    (core.rat_core.claim_output_version, on the CSV name), so two runs can
    never write to the same files; the placeholder is removed if the run
    ends without writing the CSV.
    """
    label = display_fips if display_fips else state_fips
    state_out_dir = os.path.join(OUTPUT_DIR, f"Output_State_{label}")
    stamp = datetime.now().strftime("%Y%m%d")
    version, csv_out = claim_output_version(
        state_out_dir, f"hpms_4d_production_{label}_{stamp}_v{{v}}.csv")
    try:
        _run_state_enrichment(state_fips, local_df, label, state_out_dir, stamp, version, csv_out)
    finally:
        release_output_version(csv_out)


def _run_state_enrichment(state_fips: str, local_df: pd.DataFrame, label: str,
                          state_out_dir: str, stamp: str, version: int, csv_out: str):
    from core.rat_core import GridProgressWindow
    from core.rat_cross_section import DemTiles

    gpkg_out    = os.path.join(state_out_dir, f"hpms_4d_production_{label}_{stamp}_v{version}.gpkg")
    blender_out = os.path.join(state_out_dir, f"hpms_4d_blender_export_{label}_{stamp}_v{version}.shp")

    logging.info(f"\n{'='*60}\n=== 4D ENRICHMENT: STATE FIPS {label} (v{version}) ===\n{'='*60}")

    # The run's settings (from the GUI's run_params.json when run from the
    # GUI), with this state's FIPS for the smoothing-factor lookup.
    user_params = dict(RUN_PARAMS)
    user_params.update({"OUTPUT_DIR": OUTPUT_DIR, "STATE_FIPS": label})

    if SELECTED_HPMS_FIELDS is not None:
        include_fields = SELECTED_HPMS_FIELDS
    elif INCLUDE_ALL_HPMS_FIELDS:
        include_fields = None
    else:
        include_fields = []

    params = build_params(user_params)
    logging.info(describe_smoothing_factors(params))

    try:
        if local_df is not None:
            # The same state, facility type and functional system filters a
            # Socrata query applies, as in the alignment.
            df = local_df.copy()
            df = filter_local_df_to_state(df, label)
            df = apply_facility_fsystem_filters(
                df,
                facility_filter=FACILITY_TYPE_FILTER or None,
                fsystem_filter=FSYSTEM_FILTER or None,
                include_missing_facility=FACILITY_TYPE_INCLUDE_MISSING,
                include_missing_fsystem=FSYSTEM_INCLUDE_MISSING,
            )
        else:
            df = fetch_socrata_state(
                state_fips,
                SOCRATA_TOKEN or ENV_SOCRATA_TOKEN,
                facility_type_filter=FACILITY_TYPE_FILTER or None,
                fsystem_filter=FSYSTEM_FILTER or None,
                include_missing_facility=FACILITY_TYPE_INCLUDE_MISSING,
                include_missing_fsystem=FSYSTEM_INCLUDE_MISSING,
                extra_cols=include_fields if include_fields else [],
                url=SOCRATA_URL,
            )
    except Exception as e:
        logging.error(f"Failed to fetch data for State {label}: {e}")
        return

    routes = df["RouteId"].dropna().unique().tolist()
    logging.info(f"Loaded {len(df):,} segments across {len(routes):,} routes.")

    if len(df) == 0:
        logging.error(
            f"No rows loaded for State FIPS {label}; stopping. If a cached "
            f"Socrata extract is being reused, it may be for a different "
            f"state. Turn off 'Reuse Socrata Cache' or delete the cached "
            f"extract to download State FIPS {label} again."
        )
        return

    # ------------------------------------------------------------------
    # NBI/NTI structures and DEM tiles
    # ------------------------------------------------------------------
    logging.info("Reading the state's geometry...")
    df['geom_obj'] = df['WKT'].apply(loads)

    # The national NBI/NTI tables are cut down to the state's padded
    # bounding box (see core.rat_core.compute_padded_bbox).
    state_bbox = compute_padded_bbox(df['geom_obj'])
    nbi_nti_gdf = fetch_nbi_nti_state(label, bbox=state_bbox)

    # Every 1 m tile the state needs, in the cache the alignment and
    # plan/profile runs share, downloaded before the workers start.
    cache_dir = os.path.join(DEM_DIR, "align_1m_cache")
    ensure_dem_cells_cached(df['geom_obj'], cache_dir, log_label=f"state {label}", along_lines=True)

    # ------------------------------------------------------------------
    # Continuity chunks and work items, as in rat_alignment_cli.py
    # ------------------------------------------------------------------
    logging.info(f"  Building route continuity chunks for {len(routes):,} routes...")
    work_items = []
    has_mp_cols = "Start_MP" in df.columns and "End_MP" in df.columns
    pad_mi = float(params.get("CHUNK_SPLIT_PADDING_FT", 20000.0)) / 5280.0
    n_routes, n_mixed = 0, 0
    for route_id, route_subset in df.groupby("RouteId"):
        all_chunks = build_route_continuity_chunks(route_id, route_subset, params)
        if not all_chunks:
            continue
        n_routes += 1
        n_mixed += route_spans_several_fsystems(all_chunks)
        for item in split_chunks_for_parallelism(route_id, all_chunks, params):
            # Each item gets only the route records within its milepost
            # window plus padding (used for the structure checks).
            if has_mp_cols:
                lo = item["calib_start_mp"] - pad_mi
                hi = item["calib_end_mp"] + pad_mi
                item_subset = route_subset[(route_subset["End_MP"] > lo) & (route_subset["Start_MP"] < hi)]
            else:
                item_subset = route_subset
            work_items.append((route_id, item, item_subset.drop(columns=["geom_obj"])))
    log_fsystem_summary(n_mixed, n_routes)
    logging.info(f"Network partitioned into {len(work_items):,} chunk-based parallel tasks.")

    profiles = {}   # route_id -> ([coords], [z arrays])
    master_results = {}
    completed = 0

    logging.info(f"Starting {MAX_WORKERS} worker processes...")
    progress_ui = GridProgressWindow(f"RAT 4D Engine: State {label}", len(work_items))

    with ProcessPoolExecutor(
        max_workers=MAX_WORKERS,
        initializer=init_worker_nbi,
        initargs=(nbi_nti_gdf,)
    ) as executor:
        # Tasks are submitted a few at a time (core.rat_core.run_bounded_parallel).
        MAX_IN_FLIGHT = MAX_WORKERS * 4

        # 1. Smoothed profile of each work item
        def _submit(task):
            route_id, item, item_subset = task
            return executor.submit(process_4d_item, route_id, item, item_subset, cache_dir, params)

        def _on_done(task, fut):
            nonlocal completed
            route_id, item, _ = task
            completed += 1
            progress_ui.update(completed)
            if completed % 50 == 0:
                logging.info(f"   ...Smoothed {completed}/{len(work_items)} route chunks")
            try:
                res = fut.result()
                if res:
                    rid, coords, z_m = res
                    entry = profiles.setdefault(rid, ([], []))
                    entry[0].extend(coords)
                    entry[1].append(z_m)
            except DemCacheUnavailable:
                raise
            except Exception as e:
                logging.error(f"Chunk task {(route_id, item.get('part_id'))} failed: {e}")

        run_bounded_parallel(work_items, _submit, _on_done, max_in_flight=MAX_IN_FLIGHT)

        # 2. HPMS records placed on their route's profile
        route_tasks = [
            (rid, sub[["WKT", "Start_MP", "End_MP"]], profiles[rid][0], np.concatenate(profiles[rid][1]))
            for rid, sub in df.groupby("RouteId") if rid in profiles
        ]
        logging.info(f"Placing HPMS records on {len(route_tasks):,} route profiles...")

        def _submit_map(task):
            _, sub, coords, z_m = task
            return executor.submit(map_route_to_profile, sub, coords, z_m)

        def _on_mapped(task, fut):
            try:
                master_results.update(fut.result())
            except Exception as e:
                logging.error(f"Placing records on route {task[0]} failed: {e}")

        run_bounded_parallel(route_tasks, _submit_map, _on_mapped, max_in_flight=MAX_IN_FLIGHT)

    progress_ui.close()

    df["geometry_3d"] = df.index.map(lambda i: master_results[i]["geometry_3d"] if i in master_results else None)
    df["WKT_ZM"] = df.index.map(lambda i: master_results[i]["WKT_ZM"] if i in master_results else None)

    # ------------------------------------------------------------------
    # Raw DEM fallback
    # ------------------------------------------------------------------
    missing = df[df["geometry_3d"].isna()]
    if not missing.empty and not dem_cache_reachable(cache_dir):
        raise DemCacheUnavailable(f"The DEM folder {cache_dir} cannot be reached.")
    if not missing.empty:
        logging.info(f"Applying raw DEM fallback for {len(missing):,} records with no smoothed profile...")
        # Sorted by location so neighbouring records reuse the same tiles.
        first_pt = missing["geom_obj"].apply(lambda g: g.representative_point())
        missing_idx = sorted(missing.index, key=lambda i: (round(first_pt[i].x, 2), round(first_pt[i].y, 2)))
        dem = DemTiles(cache_dir)
        fallback_count = 0
        for idx in missing_idx:
            result = enrich_segment_raw_dem(df.loc[idx], dem)
            if result:
                df.at[idx, "geometry_3d"] = result["geometry_3d"]
                df.at[idx, "WKT_ZM"]      = result["WKT_ZM"]
                fallback_count += 1
        logging.info(f"Raw DEM fallback enriched {fallback_count:,} of {len(missing_idx):,} remaining segments.")

    # Which method produced each record's elevations
    df["Elevation_Source"] = df.index.map(
        lambda i: (
            "smoothed"     if i in master_results else
            "raw_fallback" if df.at[i, "geometry_3d"] is not None else
            None
        )
    )

    out_df = df[df["geometry_3d"].notna()].copy()
    n_left_out = len(df) - len(out_df)
    if n_left_out:
        pct = 100.0 * n_left_out / max(len(df), 1)
        msg = (f"{n_left_out:,} of {len(df):,} records ({pct:.1f}%) got no elevations and are left out "
               f"of the 4D outputs (no smoothed profile and no DEM values along the record).")
        if pct > MAX_UNENRICHED_PCT:
            logging.error(msg)
        else:
            logging.warning(msg)
    if out_df.empty:
        logging.error("No 4D geometry generated. Run failed.")
        return

    # Replace the HPMS curve and grade fields with RAT values, if asked
    replaced_cols = []
    if HORIZONTAL_CSV or GRADES_CSV:
        out_df, replaced_cols = replace_curve_grade_fields(out_df, HORIZONTAL_CSV, GRADES_CSV)

    # Functional system and facility type as the State submitted them:
    # records processed with no value are written blank again.
    for col in ("FSystem", "Facility_Type"):
        flag = f"{col}_Missing"
        if flag in out_df.columns:
            if col in out_df.columns:
                out_df[col] = out_df[col].astype("Int64").mask(out_df[flag].astype(bool))
            out_df = out_df.drop(columns=[flag])

    # Columns to keep in the CSV
    if 'geom_obj' in out_df.columns:
        out_df = out_df.drop(columns=['geom_obj'])

    if include_fields is None:
        csv_drop_cols = ["WKT", "geometry_3d"]
        logging.info("All HPMS fields are kept in the output CSV.")
    elif len(include_fields) == 0:
        keep_cols     = ["RouteId", "FSystem", "Start_MP", "End_MP", "WKT_ZM", "Elevation_Source"]
        csv_drop_cols = [c for c in out_df.columns if c not in keep_cols]
        logging.info("Minimal 4D output enabled.")
    else:
        # Fields replaced with RAT values are kept even if not selected.
        core_keep     = {"RouteId", "FSystem", "Start_MP", "End_MP", "WKT_ZM", "Elevation_Source"}
        keep_cols     = core_keep | {f for f in include_fields if f in out_df.columns} | set(replaced_cols)
        csv_drop_cols = [c for c in out_df.columns if c not in keep_cols]
        logging.info("The selected HPMS fields are kept in the output CSV.")

    # 1. CSV
    out_df.drop(columns=csv_drop_cols, errors="ignore").to_csv(csv_out, index=False)
    logging.info(f"Saved enriched CSV: {os.path.basename(csv_out)}")

    if not (OUT_GPKG or OUT_SHP):
        logging.info(f"Finished State {label}.")
        return

    # The GeoPackage and shapefile carry the same fields as the CSV (WKT_ZM
    # is replaced by the 3D geometry).
    gis_drop_cols = [c for c in csv_drop_cols if c != "geometry_3d"] + ["WKT", "WKT_ZM"]
    # GeoPackage and shapefile field names ignore case, so two columns that
    # differ only in case cannot both be written; keep the first.
    seen, case_dups = set(), []
    for c in out_df.columns:
        if c in gis_drop_cols:
            continue
        if c.lower() in seen:
            case_dups.append(c)
        seen.add(c.lower())
    if case_dups:
        logging.warning(f"Columns left out of the GeoPackage/shapefile (same name as another "
                        f"column apart from case): {', '.join(case_dups)}")
        gis_drop_cols += case_dups
    gdf = gpd.GeoDataFrame(
        out_df.drop(columns=gis_drop_cols, errors="ignore"),
        geometry="geometry_3d",
        crs="EPSG:4326",
    )

    # 2. GeoPackage
    if OUT_GPKG:
        try:
            gdf.to_file(gpkg_out, driver="GPKG")
            logging.info(f"Saved GeoPackage: {os.path.basename(gpkg_out)}")
        except Exception as e:
            logging.error(f"Failed to save GPKG: {e}")

    # 3. Shapefile in UTM for Blender
    if OUT_SHP:
        try:
            utm_crs     = gdf.estimate_utm_crs()
            gdf_blender = gdf.to_crs(utm_crs)
            # Shapefile field names are limited to 10 characters. The
            # driver shortens longer names (and renames clashes) itself;
            # one log line lists the changes instead of a warning per field.
            long_names = [c for c in gdf_blender.columns
                          if c != gdf_blender.geometry.name and len(c) > 10]
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", UserWarning)
                warnings.simplefilter("ignore", RuntimeWarning)
                gdf_blender.to_file(blender_out)
            logging.info(f"Saved Blender SHP: {os.path.basename(blender_out)} (Projected to {utm_crs.name})")
            if long_names:
                logging.info(
                    f"  {len(long_names)} field names over 10 characters were shortened in the "
                    f"shapefile (e.g. {long_names[0]}); the CSV and GeoPackage keep the full names.")
        except Exception as e:
            logging.error(f"Failed to save Blender shapefile: {e}")

    logging.info(f"Finished State {label}.")


def _local_state_label() -> str:
    """State FIPS for a local input file: the single state in
    STATES_TO_PROCESS, or "LOCAL" when it names none or several. The FIPS
    drives the smoothing-factor lookup, the NBI/NTI selection and the
    output names."""
    if len(STATES_TO_PROCESS) == 1 and STATES_TO_PROCESS[0].upper() != "ALL":
        return STATES_TO_PROCESS[0]
    return "LOCAL"


def main():
    import argparse

    ap = argparse.ArgumentParser(description="RAT 4D Enricher CLI")
    ap.add_argument("--input",          default=None, help="Path to local input file (optional)")
    ap.add_argument("--outdir",         default=None, help="Output directory")
    ap.add_argument("--demdir",         default=None, help="DEM cache directory")
    ap.add_argument("--params_json",    default=None, help="Path to run_params.json from GUI")
    ap.add_argument("--horizontal_csv", default=None, help="Path to RAT horizontal curve CSV for curve field replacement")
    ap.add_argument("--grades_csv",     default=None, help="Path to RAT grade segment CSV for grade field replacement")
    args = ap.parse_args()

    global OUTPUT_DIR, DEM_DIR, STATES_TO_PROCESS, FACILITY_TYPE_FILTER
    global FSYSTEM_FILTER, INCLUDE_ALL_HPMS_FIELDS, SELECTED_HPMS_FIELDS
    global FACILITY_TYPE_INCLUDE_MISSING, FSYSTEM_INCLUDE_MISSING
    global HORIZONTAL_CSV, GRADES_CSV, SOCRATA_TOKEN, SOCRATA_URL, OUT_GPKG, OUT_SHP, RUN_PARAMS

    if args.params_json and os.path.exists(args.params_json):
        with open(args.params_json, "r", encoding="utf-8") as f:
            p = json.load(f)
        RUN_PARAMS             = p
        OUTPUT_DIR             = args.outdir  or p.get("OUTPUT_DIR", OUTPUT_DIR)
        DEM_DIR                = args.demdir  or p.get("DEM_DIR",    DEM_DIR)
        FACILITY_TYPE_FILTER   = p.get("FACILITY_TYPE_FILTER", FACILITY_TYPE_FILTER)
        FSYSTEM_FILTER         = p.get("FSYSTEM_FILTER",       FSYSTEM_FILTER)
        FACILITY_TYPE_INCLUDE_MISSING = bool(p.get("FACILITY_TYPE_INCLUDE_MISSING",
                                                   FACILITY_TYPE_INCLUDE_MISSING))
        FSYSTEM_INCLUDE_MISSING       = bool(p.get("FSYSTEM_INCLUDE_MISSING",
                                                   FSYSTEM_INCLUDE_MISSING))
        gui_fields = p.get("INCLUDE_HPMS_FIELDS", "NOT_SET")
        if gui_fields != "NOT_SET":
            INCLUDE_ALL_HPMS_FIELDS = (gui_fields is None)
            SELECTED_HPMS_FIELDS    = gui_fields
        SOCRATA_TOKEN          = p.get("SOCRATA_TOKEN") or SOCRATA_TOKEN
        SOCRATA_URL            = p.get("INPUT_URL") or SOCRATA_URL
        OUT_GPKG               = bool(p.get("OUT_GPKG", OUT_GPKG))
        OUT_SHP                = bool(p.get("OUT_SHP", OUT_SHP))
        # From the GUI, a blank State FIPS (or ALL) means every state.
        state_fips = str(p.get("STATE_FIPS", "")).strip().upper().zfill(2)
        STATES_TO_PROCESS = ["ALL"] if state_fips in ("00", "ALL") else [state_fips]
    else:
        if args.outdir:  OUTPUT_DIR = args.outdir
        if args.demdir:  DEM_DIR    = args.demdir

    # Command-line paths take precedence over the CONFIGURATION block.
    if args.horizontal_csv: HORIZONTAL_CSV = args.horizontal_csv
    if args.grades_csv:     GRADES_CSV     = args.grades_csv

    if not OUTPUT_DIR:
        logging.error("OUTPUT_DIR is not set. Provide --outdir or set OUTPUT_DIR in the config block.")
        raise SystemExit(1)
    if not DEM_DIR:
        logging.error("DEM_DIR is not set. Provide --demdir or set DEM_DIR in the config block.")
        raise SystemExit(1)

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    os.makedirs(DEM_DIR,    exist_ok=True)

    try:
        if args.input:
            logging.info(f"Processing local file: {args.input}")
            df = load_local_hpms(args.input)
            resolved_fips = _local_state_label()
            run_state_enrichment(resolved_fips, local_df=df, display_fips=resolved_fips)
        elif STATES_TO_PROCESS:
            states = ALL_FIPS if "ALL" in [s.upper() for s in STATES_TO_PROCESS] else STATES_TO_PROCESS
            for state in states:
                run_state_enrichment(state)
        elif LOCAL_FILE_PATH:
            logging.info(f"Processing local file: {LOCAL_FILE_PATH}")
            df = load_local_hpms(LOCAL_FILE_PATH)
            resolved_fips = _local_state_label()
            run_state_enrichment(resolved_fips, local_df=df, display_fips=resolved_fips)
        else:
            logging.error("No input specified. Set STATES_TO_PROCESS or LOCAL_FILE_PATH in the config block.")
            raise SystemExit(1)

    except DemCacheUnavailable as e:
        # One message instead of one per task. Nothing is written for the
        # state that was running; states already finished keep their files.
        logging.error(
            f"{e} The run was stopped and no outputs were written for the state in progress. "
            f"Check the connection to the DEM folder (or use a folder on a local drive) and run it again."
        )
        raise SystemExit(1)


if __name__ == "__main__":
    main()