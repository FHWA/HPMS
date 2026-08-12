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
Calculates a smoothed, continuous "macro-profile" for a route, then uses a
Metric KDTree to snap fragmented HPMS tabular rows back to the smoothed alignment.
Outputs WKT_ZM strings containing Longitude, Latitude, Elevation (Z), and
Milepost (M) for advanced 4D digital twin modeling.

CHANGES:
  - Raw DEM fallback for segments that could not be enriched via the smoothed
    macro-profile pipeline. Routes or chunks that fail the UnivariateSpline
    step (typically because the geometry is too short to satisfy the minimum
    point requirement) previously produced no output for any HPMS segments
    on that route. The fallback samples USGS DEM elevation directly at each
    original vertex coordinate using get_elevations(), bypassing the smoothing
    and KDTree steps entirely. Milepost values are still interpolated
    proportionally from Start_MP and End_MP.

    Fallback records are tagged with Elevation_Source = "raw_fallback" in the
    output; successfully smoothed records carry Elevation_Source = "smoothed".
    This column allows downstream users to distinguish the two populations.

    The fallback is applied after the main enrichment loop. Only segments with
    no result from the primary pipeline are candidates; segments that were
    enriched normally are never overwritten by the fallback.

CREATED BY: Federal Highway Administration, Office of Highway Policy Information.
CREATED ON: 5/14/2026
"""
import os
import sys
import json
import logging
import math
from datetime import datetime
import numpy as np
import pandas as pd
import geopandas as gpd
import argparse
from shapely.wkt import loads
from shapely.geometry import shape, LineString
from concurrent.futures import ProcessPoolExecutor, as_completed

# Module-level global for NBI/NTI structures. Set once per worker process via
# _init_worker_nbi() rather than passed as an argument to every task submission.
# Re-pickling a 743,677-record GeoDataFrame for each of potentially millions of
# grid cell tasks causes severe memory pressure and IPC overhead at national scale.
_WORKER_NBI_NTI_GDF = None
GLOBAL_PARAMS_JSON = {}

def _init_worker_nbi(nbi_nti_gdf):
    """
    ProcessPoolExecutor initializer. Runs once when each worker process starts,
    setting the module-level NBI/NTI reference for that process only.
    """
    global _WORKER_NBI_NTI_GDF
    _WORKER_NBI_NTI_GDF = nbi_nti_gdf

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
RAT_SUITE_DIR = os.path.dirname(THIS_DIR)
if RAT_SUITE_DIR not in sys.path:
    sys.path.insert(0, RAT_SUITE_DIR)

from core.rat_core import (
    build_params,
    stitch_linestrings_ordered,
    smooth_plan_profile_from_linestring,
    build_metric_kdtree,
    query_metric_kdtree,
    download_dems,
    get_elevations,
    fetch_socrata_state,
    load_local_hpms,
    filter_local_df_to_state,
    apply_facility_fsystem_filters,
    FEET_PER_METER,
    fetch_nbi_nti_state
)
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s: %(message)s")


# ===========================================================================
# USER CONFIGURATION BLOCK
# ===========================================================================

# Facility type and functional system filters applied at the Socrata query level.
# HPMS facility types: 1 = One-Way Roadway, 2 = Two-Way Roadway, 4 = Ramp,
# 5 = Non-Mainline, 6 = Non-Inventory Direction, 7 = Planned/Unbuilt.
# Default includes mainline roadways only (types 1 and 2).
FACILITY_TYPE_FILTER = [1, 2]
FSYSTEM_FILTER       = []     # Empty list = all functional systems

# User-selected field subset from GUI (None = full pass-through)
SELECTED_HPMS_FIELDS = None

# When True, all HPMS source attributes are preserved in the enriched output.
# Overridden at runtime by INCLUDE_HPMS_FIELDS from run_params.json when
# invoked by the GUI.
INCLUDE_ALL_HPMS_FIELDS = True

# Paths to RAT alignment output CSVs for curve/grade field replacement.
# When provided, CURVES_A–F and GRADES_A–F are replaced with RAT-derived
# values. Leave empty to preserve original HPMS-reported values.
HORIZONTAL_CSV = ""
VERTICAL_CSV   = ""

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

# Bin label sets for horizontal and vertical replacement
H_BINS = ["CURVES_A", "CURVES_B", "CURVES_C", "CURVES_D", "CURVES_E", "CURVES_F"]
V_BINS = ["GRADES_A", "GRADES_B", "GRADES_C", "GRADES_D", "GRADES_E", "GRADES_F"]


# ---------------------------------------------------------------------------
# Curve / Grade Field Replacement
# ---------------------------------------------------------------------------
def replace_curve_grade_fields(
    out_df: pd.DataFrame,
    horizontal_csv: str,
    vertical_csv:   str,
) -> pd.DataFrame:
    """
    Replaces HPMS-reported CURVES_A–F and GRADES_A–F fields with values
    derived from the RAT alignment output.

    For each HPMS segment the function finds all RAT curves whose calibrated
    milepost range overlaps [Start_MP, End_MP], clips the overlap to the
    segment bounds, and apportions the length in miles to the appropriate
    bin class. CURVES_A and GRADES_A receive the section length remainder
    after all higher-class curves are accounted for, correctly representing
    tangent sections as Class A geometry.

    A single RAT curve may contribute overlapping length to multiple adjacent
    HPMS segments. The sum of CURVES_A–F for any segment will equal the
    section length (End_MP – Start_MP) to floating-point precision.

    Parameters
    ----------
    out_df          : enriched HPMS DataFrame with Start_MP and End_MP columns
    horizontal_csv  : path to RAT horizontal curve CSV
    vertical_csv    : path to RAT vertical curve CSV

    Returns
    -------
    out_df with CURVES_A–F and/or GRADES_A–F columns replaced in place.
    """
    # NOTE ON RouteId DTYPE: horizontal_csv/vertical_csv are read fresh from
    # disk below with plain pd.read_csv(), which infers each column's dtype
    # independently. If a given curve file's RouteId values happen to be
    # ALL purely numeric (e.g. "001"), pandas silently infers int64 and
    # drops leading zeros -- while out_df's RouteId (built from the raw HPMS
    # extract, which mixes numeric and alphanumeric route names like
    # "18P144") stays as text. A later comparison between an int64 column
    # and a text column doesn't raise on .isin() -- it just silently
    # matches nothing, meaning every segment on that route silently gets
    # marked as having NO curves/grades, with no error or warning. Forcing
    # RouteId to string here, and again right after each CSV read below,
    # eliminates this regardless of which side would otherwise have been
    # inferred as numeric.
    if "RouteId" in out_df.columns:
        out_df = out_df.copy()
        out_df["RouteId"] = out_df["RouteId"].astype(str)

    def _apply_replacement(df: pd.DataFrame, curves: pd.DataFrame,
                           bin_col: str, bins: list) -> pd.DataFrame:
        """
        Core overlap apportionment logic shared by horizontal and vertical.
        bin_col : column in curves DataFrame containing the bin class letter
        bins    : list of output column names [CLASS_A, CLASS_B, ... CLASS_F]

        NOTE: The previous implementation looped over every row of df with
        .iterrows() and, for each row, computed overlap against EVERY curve
        in `curves` with no RouteId check at all. Two problems with that:
          1. Performance: O(segments * curves) with an inner Python-level
             enumerate() loop per segment -- effectively unbounded on a
             state-wide segment table.
          2. Correctness: because there was no RouteId match, a segment on
             one route could be assigned curve/grade bin length from a
             completely different route's curve, just because the milepost
             ranges happened to numerically overlap. This silently corrupts
             CURVES_A-F / GRADES_A-F for any state where two routes share
             overlapping milepost ranges (extremely common, since mileposts
             reset per route).
        Replaced with a single vectorized merge scoped by RouteId, so a
        segment can only be apportioned length from curves on its own
        route, and the overlap/summation work is fully vectorized.
        """
        for col in bins:
            df[col] = 0.0

        required = {"RouteId", "Calibrated_Start_MP", "Calibrated_End_MP", bin_col}
        has_curves = (
            not curves.empty
            and required.issubset(curves.columns)
            and "RouteId" in df.columns
        )

        if has_curves:
            df["_orig_idx"] = df.index
            left = df[["_orig_idx", "RouteId", "Start_MP", "End_MP"]]
            right = curves[["RouteId", "Calibrated_Start_MP", "Calibrated_End_MP", bin_col]]
            merged = left.merge(right, on="RouteId", how="inner")
            if not merged.empty:
                overlap = (
                    np.minimum(merged["End_MP"], merged["Calibrated_End_MP"])
                    - np.maximum(merged["Start_MP"], merged["Calibrated_Start_MP"])
                )
                merged = merged.assign(overlap=overlap.clip(lower=0))
                merged = merged[(merged["overlap"] > 0) & (merged[bin_col] != "A")]

                bin_letters = ["A", "B", "C", "D", "E", "F"]
                bin_map = dict(zip(bin_letters, bins))
                merged = merged.assign(out_col=merged[bin_col].map(bin_map)).dropna(subset=["out_col"])

                if not merged.empty:
                    pivot = merged.groupby(["_orig_idx", "out_col"])["overlap"].sum().unstack(fill_value=0.0)
                    for col in pivot.columns:
                        df.loc[pivot.index, col] = pivot[col].values
            df = df.drop(columns=["_orig_idx"])

        # CLASS_A / GRADES_A gets the remainder of the section length after
        # all higher-class bins are accounted for (tangent/no-curve length).
        seg_len = (df["End_MP"] - df["Start_MP"]).clip(lower=0)
        non_a_total = df[bins[1:]].sum(axis=1)
        df[bins[0]] = (seg_len - non_a_total).clip(lower=0)
        for col in bins:
            df[col] = df[col].round(6)

        return df

    # Strip any existing curves/grades fields (case-insensitive) from the input 
    # to prevent duplicate column schema errors when exporting to GeoPackage.
    all_bins = [b.upper() for b in H_BINS + V_BINS]
    drop_cols = [c for c in out_df.columns if c.upper() in all_bins]
    if drop_cols:
        out_df.drop(columns=drop_cols, inplace=True)

    # --- Horizontal replacement ---
    if horizontal_csv and os.path.exists(horizontal_csv):
        try:
            h_df = pd.read_csv(horizontal_csv, low_memory=False, dtype={"RouteId": str})
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

    # --- Vertical replacement ---
    if vertical_csv and os.path.exists(vertical_csv):
        try:
            v_df = pd.read_csv(vertical_csv, low_memory=False, dtype={"RouteId": str})
            required_v = {"Calibrated_Start_MP", "Calibrated_End_MP", "Grade_Bin"}
            if required_v.issubset(set(v_df.columns)):
                if "RouteId" in v_df.columns and "RouteId" in out_df.columns:
                    v_df = v_df[v_df["RouteId"].isin(out_df["RouteId"].unique())]
                out_df = _apply_replacement(out_df, v_df, "Grade_Bin", V_BINS)
                logging.info(
                    f"Vertical grade fields replaced using RAT output: "
                    f"{os.path.basename(vertical_csv)}"
                )
            else:
                missing = required_v - set(v_df.columns)
                logging.warning(
                    f"Vertical CSV missing required columns {missing}. "
                    "GRADES_A–F not replaced."
                )
        except Exception as e:
            logging.error(f"Failed to apply vertical grade replacement: {e}")

    return out_df


# ---------------------------------------------------------------------------
# Multiprocessing Worker Function
# ---------------------------------------------------------------------------
def process_4d_route(route_id: str, sub: pd.DataFrame, dem_dir: str, params: dict, nbi_nti_gdf: gpd.GeoDataFrame = None):
    results = {}
    predominant_f_sys = int(sub["FSystem"].mode()[0])

    macro_coords_wgs = []
    macro_z_vals     = []

    lines = stitch_linestrings_ordered(sub["WKT"].tolist())
    for line in lines:
        chunk_res = smooth_plan_profile_from_linestring(
            line, dem_dir, params, predominant_f_sys,
            route_id=route_id, hpms_subset=sub, nbi_nti_gdf=nbi_nti_gdf
        )
        if chunk_res is not None and chunk_res.get("ok", True):
            macro_coords_wgs.extend(chunk_res["coords_wgs_smooth"])
            macro_z_vals.extend(chunk_res["z_smooth"])

    if not macro_coords_wgs:
        return results

    tree, tx      = build_metric_kdtree(macro_coords_wgs)
    macro_z_array = np.array(macro_z_vals)

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
                    x1, y1 = tx.transform(*coords[i-1])
                    x2, y2 = tx.transform(*coords[i])
                    total_geom_len_m += math.hypot(x2 - x1, y2 - y1)

            if total_geom_len_m == 0:
                total_geom_len_m = 1e-9

            current_len_m = 0.0
            row_s_mp      = float(row["Start_MP"])
            row_e_mp      = float(row["End_MP"])

            xyz      = []
            xyzm_txt = []

            for part in parts:
                raw_coords = list(part.coords)
                q_idx      = query_metric_kdtree(tree, tx, raw_coords)
                z_assigned = macro_z_array[q_idx]

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

            geom3d = LineString(xyz)
            wkt_zm = f"LINESTRING ZM ({', '.join(xyzm_txt)})"

            results[row_idx] = {
                "geometry_3d": geom3d,
                "WKT_ZM":      wkt_zm,
            }
        except Exception:
            continue

    return results


# ---------------------------------------------------------------------------
# Raw DEM Fallback Enrichment
# ---------------------------------------------------------------------------
def enrich_segment_raw_dem(row: pd.Series, dem_dir: str) -> dict | None:
    """
    Fallback enrichment for a single HPMS segment when the smoothed
    macro-profile pipeline produced no result for its route.

    Samples USGS DEM elevation directly at each original vertex coordinate
    using get_elevations(), bypassing the UnivariateSpline smoother and
    KDTree entirely. Milepost values are interpolated proportionally from
    Start_MP and End_MP using cumulative geometric distance.

    Returns a result dict in the same format as process_4d_route(), or None
    if the geometry cannot be parsed or no DEM coverage exists.
    """
    try:
        g = loads(row["WKT"]) if isinstance(row["WKT"], str) else shape(row["WKT"])
        if g.is_empty:
            return None

        parts = list(g.geoms) if g.geom_type == "MultiLineString" else [g]

        all_coords = []
        for part in parts:
            all_coords.extend(list(part.coords))

        if len(all_coords) < 2:
            return None

        # Ensure the 10-meter DEM tiles for this segment are downloaded before sampling
        download_dems([row["WKT"]], dem_dir)

        # Sample raw DEM elevation at each vertex
        z_vals = get_elevations([(lon, lat) for lon, lat in all_coords], dem_dir)

        # Compute cumulative distances for milepost interpolation
        row_s_mp = float(row["Start_MP"])
        row_e_mp = float(row["End_MP"])

        cum_dists = [0.0]
        for i in range(1, len(all_coords)):
            lon1, lat1 = all_coords[i-1]
            lon2, lat2 = all_coords[i]
            d = math.hypot(lon2 - lon1, lat2 - lat1)
            cum_dists.append(cum_dists[-1] + d)

        total_dist = cum_dists[-1] if cum_dists[-1] > 0 else 1e-9

        xyz      = []
        xyzm_txt = []

        for i, (lon, lat) in enumerate(all_coords):
            f = cum_dists[i] / total_dist
            m = row_s_mp + f * (row_e_mp - row_s_mp)
            z = float(z_vals[i]) if not np.isnan(z_vals[i]) else 0.0
            xyz.append((lon, lat, z))
            xyzm_txt.append(f"{lon:.7f} {lat:.7f} {z:.2f} {m:.4f}")

        return {
            "geometry_3d": LineString(xyz),
            "WKT_ZM":      f"LINESTRING ZM ({', '.join(xyzm_txt)})",
        }

    except Exception:
        return None


# ===========================================================================
# V3.4 HIGH-RESOLUTION 1M EPHEMERAL GRID WORKER ENGINE
# ===========================================================================

def process_4d_enrichment_tile_worker(bbox: tuple, subset_df: pd.DataFrame, base_dem_dir: str, params: dict):
    """
    V3.4 Multiprocessing Grid Worker.
    Ingests an explicit bounding box, downloads a temporary 1-meter raster tile,
    snaps crossing 2D segments to derived 3D/4D profiles using the local macro-profile
    smoother, deletes the raster file, and returns results.

    NBI/NTI structures are read from the module-level _WORKER_NBI_NTI_GDF global,
    set once per process by _init_worker_nbi() rather than passed per task.
    """
    import os
    import logging
    import pandas as pd
    from core.rat_core import prepare_1m_tile_for_worker
    from apps.hpms_4d_enricher_cli import process_4d_route, _WORKER_NBI_NTI_GDF

    # Download 1-degree tile(s) covering this cell if not already cached.
    # Shares a cache folder with the alignment pipeline's grid worker (see
    # prepare_1m_tile_for_worker's docstring) -- previously this used its
    # own separate "enrich_4d_1m_cache" folder, so the exact same USGS 1m
    # tiles got downloaded twice whenever both pipelines ran for the same
    # state, which is the common case.
    shared_cache_dir = prepare_1m_tile_for_worker(bbox, base_dem_dir, params)

    tile_results = {}
    try:
        for rid, route_subset in subset_df.groupby("RouteId"):
            res = process_4d_route(rid, route_subset, shared_cache_dir, params, _WORKER_NBI_NTI_GDF)
            if res:
                tile_results.update(res)
    except Exception as e:
        logging.error(f"Worker process failed on grid tile {bbox}: {e}")

    return tile_results


def run_state_enrichment(state_fips: str, local_df: pd.DataFrame = None, display_fips: str = None):
    """
    Upgraded Core 4D Orchestrator. Slices the target state network footprint into 
    0.02 degree spatial cells, distributes them to parallel workers using an
    ephemeral 1m data pipeline, and applies the raw DEM fallback seamlessly.
    """
    import shutil
    from shapely.wkt import loads
    from shapely.geometry import box
    from concurrent.futures import ProcessPoolExecutor, as_completed

    label = display_fips if display_fips else state_fips

    state_out_dir = os.path.join(OUTPUT_DIR, f"Output_State_{label}")
    os.makedirs(state_out_dir, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d")

    version = 1
    while True:
        csv_out     = os.path.join(state_out_dir, f"hpms_4d_production_{label}_{stamp}_v{version}.csv")
        gpkg_out    = os.path.join(state_out_dir, f"hpms_4d_production_{label}_{stamp}_v{version}.gpkg")
        blender_out = os.path.join(state_out_dir, f"hpms_4d_blender_export_{label}_{stamp}_v{version}.shp")
        if not os.path.exists(csv_out):
            break
        version += 1

    logging.info(f"\n{'='*60}\n=== V3.4 GRID 4D ENRICHMENT: STATE FIPS {label} (v{version}) ===\n{'='*60}")

    user_params = GLOBAL_PARAMS_JSON.copy()
    user_params["OUTPUT_DIR"] = OUTPUT_DIR
    user_params["STATE_FIPS"] = state_fips

    params = build_params(user_params)

    if SELECTED_HPMS_FIELDS is not None:
        include_fields = SELECTED_HPMS_FIELDS
    elif INCLUDE_ALL_HPMS_FIELDS:
        include_fields = None
    else:
        include_fields = []

    params = build_params(user_params)

    try:
        if local_df is not None:
            df = local_df.copy()
            # Shared helper in rat_core.py -- same filter logic used by
            # rat_alignment_cli.py, applies the state filter the live
            # Socrata query would have applied server-side.
            df = filter_local_df_to_state(df, state_fips)
            # NOTE: previously missing -- the facility-type/fsystem filters
            # were only ever applied in the fetch_socrata_state() branch
            # below, which never runs when a cached/local extract is
            # supplied. This let ramps and non-inventory-direction rows
            # through to 4D enrichment output (shapefile/GPKG/CSV) even
            # though FACILITY_TYPE_FILTER was correctly set to [1, 2].
            df = apply_facility_fsystem_filters(
                df,
                facility_filter=FACILITY_TYPE_FILTER,
                fsystem_filter=FSYSTEM_FILTER,
            )
        else:
            df = fetch_socrata_state(
                state_fips,
                SOCRATA_TOKEN,
                facility_type_filter=FACILITY_TYPE_FILTER or None,
                fsystem_filter=FSYSTEM_FILTER or None,
                extra_cols=include_fields if include_fields else []
            )
    except Exception as e:
        logging.error(f"Failed to fetch data for State {state_fips}: {e}")
        return

    routes = df["RouteId"].dropna().unique().tolist()
    logging.info(f"Loaded {len(df):,} segments across {len(routes):,} routes.")

    nbi_url = user_params.get("NBI_URL", None)
    nti_url = user_params.get("NTI_URL", None)
    nbi_nti_gdf = fetch_nbi_nti_state(state_fips, "", nbi_url, nti_url)

    # ------------------------------------------------------------------
    # MAP FOOTPRINT TO GEOGRAPHIC EXTENT CELLS
    # ------------------------------------------------------------------
    logging.info("Calculating network boundaries and slicing spatial cells...")
    df['geom_obj'] = df['WKT'].apply(loads)
    total_bounds = gpd.GeoSeries(df['geom_obj']).total_bounds
    minx, miny, maxx, maxy = total_bounds

    # Pre-fetch every DEM tile this run will need, in one serial call,
    # before any parallel worker starts -- same fix and same reasoning as
    # the alignment pipeline's prefetch (see run_state_alignment). Without
    # this, every worker discovering its own tile need on demand creates
    # contention whenever a state's footprint is geographically
    # concentrated onto few tiles. Shares a cache folder with the
    # alignment pipeline, so if that already ran for this state, this is
    # typically close to a no-op.
    shared_cache_dir = os.path.join(DEM_DIR, "align_1m_cache")
    os.makedirs(shared_cache_dir, exist_ok=True)
    logging.info(
        f"Pre-fetching DEM tile(s) for the full state footprint "
        f"({minx:.2f},{miny:.2f}) to ({maxx:.2f},{maxy:.2f}) before starting parallel processing..."
    )
    from core.rat_core import download_high_res_dem_tile
    lat_start, lat_end = int(np.floor(miny)), int(np.ceil(maxy))
    lon_start, lon_end = int(np.floor(minx)), int(np.ceil(maxx))
    for lat_deg in range(lat_start, lat_end):
        for lon_deg in range(lon_start, lon_end):
            download_high_res_dem_tile((lon_deg, lat_deg, lon_deg + 1, lat_deg + 1), shared_cache_dir)
    logging.info("Pre-fetch complete -- no DEM downloads should be needed during parallel processing.")
    
    # Partition space into 0.02 degree grid chunks (~1.3 mile squares)
    x_ticks = np.arange(minx, maxx + 0.02, 0.02)
    y_ticks = np.arange(miny, maxy + 0.02, 0.02)

    grid_cells = []
    for x in x_ticks:
        for y in y_ticks:
            grid_cells.append((x, y, x + 0.02, y + 0.02))

    # Build STRtree spatial index for fast cell-to-segment intersection.
    # Replaces O(n*m) row-by-row lambda with O(n log m) indexed queries --
    # critical for large states and national runs with millions of segments.
    from shapely.strtree import STRtree
    logging.info(f"  Building spatial index over {len(df):,} segments...")
    geom_list = df['geom_obj'].tolist()
    tree = STRtree(geom_list)
    df_index = df.index.tolist()

    spatial_tasks = []
    for cell_bbox in grid_cells:
        cell_box = box(*cell_bbox)
        candidate_positions = tree.query(cell_box)
        if len(candidate_positions) == 0:
            continue
        matched_idx = [
            df_index[pos] for pos in candidate_positions
            if geom_list[pos].intersects(cell_box)
        ]
        if matched_idx:
            spatial_tasks.append((cell_bbox, df.loc[matched_idx].copy()))

    logging.info(f"State network mapped into {len(spatial_tasks):,} active intersecting grid tasks.")

    master_results = {}
    completed = 0
    
    # Fire up parallel workers over the geographic grid array
    logging.info(f"Spinning up {MAX_WORKERS} CPU cores for parallel grid processing...")
    
    # Trigger the floating progress window
    from core.rat_core import GridProgressWindow
    progress_ui = GridProgressWindow(f"RAT 4D Engine: State {label}", len(spatial_tasks))
    
    with ProcessPoolExecutor(
        max_workers=MAX_WORKERS,
        initializer=_init_worker_nbi,
        initargs=(nbi_nti_gdf,)
    ) as executor:
        futures = {
            executor.submit(process_4d_enrichment_tile_worker, bbox, frame, DEM_DIR, params): bbox 
            for bbox, frame in spatial_tasks
        }

        for fut in as_completed(futures):
            completed += 1
            progress_ui.update(completed)  # Update the visual bar
            
            if completed % 50 == 0:
                logging.info(f"   ...Enriched {completed}/{len(spatial_tasks)} geographic tiles")
            try:
                res = fut.result()
                if res:
                    master_results.update(res)
            except Exception as e:
                logging.error(f"Grid box task {futures[fut]} failed during enrichment: {e}")
                
    progress_ui.close()  # Destroy the pop-up when the state finishes

    # Map output lookups back to the master tracking dataframe

    # Map output lookups back to the master tracking dataframe
    df["geometry_3d"] = df.index.map(lambda i: master_results[i]["geometry_3d"] if i in master_results else None)
    df["WKT_ZM"] = df.index.map(lambda i: master_results[i]["WKT_ZM"] if i in master_results else None)

    # ------------------------------------------------------------------
    # RAW DEM FALLBACK
    # ------------------------------------------------------------------
    missing_idx = df[df["geometry_3d"].isna()].index.tolist()
    if missing_idx:
        logging.info(f"Applying raw DEM fallback for {len(missing_idx):,} segments on routes that could not be smoothed...")
        fallback_count = 0
        for idx in missing_idx:
            result = enrich_segment_raw_dem(df.loc[idx], DEM_DIR)
            if result:
                df.at[idx, "geometry_3d"] = result["geometry_3d"]
                df.at[idx, "WKT_ZM"]      = result["WKT_ZM"]
                fallback_count += 1
        logging.info(f"Raw DEM fallback enriched {fallback_count:,} of {len(missing_idx):,} remaining segments.")

    # Tag records with elevation sources for downstream analysis
    df["Elevation_Source"] = df.index.map(
        lambda i: (
            "smoothed"     if i in master_results else
            "raw_fallback" if df.at[i, "geometry_3d"] is not None else
            None
        )
    )

    out_df = df[df["geometry_3d"].notna()].copy()
    if out_df.empty:
        logging.error("No 4D geometry generated. Run failed.")
        return

    # Apply curve and grade value field replacements
    if HORIZONTAL_CSV or VERTICAL_CSV:
        out_df = replace_curve_grade_fields(out_df, HORIZONTAL_CSV, VERTICAL_CSV)

    # Handle selective column drop constraints
    if 'geom_obj' in out_df.columns:
        out_df = out_df.drop(columns=['geom_obj'])

    if include_fields is None:
        csv_drop_cols = ["WKT", "geometry_3d"]
        logging.info(f"Full attribute pass-through enabled. Retaining fields in output CSV.")
    elif len(include_fields) == 0:
        keep_cols     = ["RouteId", "FSystem", "Start_MP", "End_MP", "WKT_ZM"]
        csv_drop_cols = [c for c in out_df.columns if c not in keep_cols]
        logging.info("Minimal 4D output enabled.")
    else:
        core_keep     = {"RouteId", "FSystem", "Start_MP", "End_MP", "WKT_ZM"}
        keep_cols     = core_keep | {f for f in include_fields if f in out_df.columns}
        csv_drop_cols = [c for c in out_df.columns if c not in keep_cols]
        logging.info(f"Custom field selection subset tracking configured.")

    # 1. Output the Production CSV
    out_df.drop(columns=csv_drop_cols, errors="ignore").to_csv(csv_out, index=False)
    logging.info(f"Saved enriched CSV: {os.path.basename(csv_out)}")

    # 2. Output the Master GeoPackage
    try:
        gdf = gpd.GeoDataFrame(
            out_df.drop(columns=["WKT"], errors="ignore"),
            geometry="geometry_3d",
            crs="EPSG:4326",
        )
        gdf.to_file(gpkg_out, driver="GPKG")
        logging.info(f"Saved GeoPackage: {os.path.basename(gpkg_out)}")
    except Exception as e:
        logging.error(f"Failed to save GPKG: {e}")

    # 3. Output the Blender-specific Shapefile
    try:
        utm_crs     = gdf.estimate_utm_crs()
        gdf_blender = gdf.to_crs(utm_crs)
        cols_to_drop = [c for c in gdf_blender.columns if c.upper() in ["WKT", "WKT_ZM"]]
        gdf_blender  = gdf_blender.drop(columns=cols_to_drop, errors="ignore")
        gdf_blender.to_file(blender_out)
        logging.info(f"Saved Blender SHP: {os.path.basename(blender_out)} (Projected to {utm_crs.name})")
    except Exception as e:
        logging.error(f"Failed to save Blender shapefile: {e}")

    logging.info(f"Finished State {label}!")



def main():
    # REPLACE the argparse setup (lines 815-824) with this improved version:
    parser = argparse.ArgumentParser(
        description="RAT 4D Enrichment Pipeline",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Process with Unified GUI config
  python hpms_4d_enricher_cli.py --params_json config.json --outdir output --demdir /dems
  
  # Process Nebraska from Socrata API
  python hpms_4d_enricher_cli.py --outdir output --demdir /dems --state 31
  
  # Process from local CSV file
  python hpms_4d_enricher_cli.py --input local_hpms.csv --outdir output --demdir /dems
  
  # With Socrata token (for high-volume requests)
  python hpms_4d_enricher_cli.py --outdir output --demdir /dems --state ALL --socrata-token YOUR_TOKEN
        """
    )
    parser.add_argument("--input",          default=None, 
                        help="Path to local HPMS CSV file (overrides --state)")
    parser.add_argument("--state",          default="31",
                        help="State FIPS code or 'ALL' (default: 31, ignored if --input provided)")
    parser.add_argument("--outdir",         required=True,
                        help="Output directory for 4D enriched data")
    parser.add_argument("--demdir",         required=True,
                        help="Path to DEM cache directory")
    parser.add_argument("--params_json",    default=None, 
                        help="Path to run_params.json from Unified GUI (overrides other args)")
    parser.add_argument("--socrata-token",  default="",
                        help="Socrata API token (optional)")
    parser.add_argument("--horizontal-csv", default=None,
                        help="Path to RAT horizontal curve CSV for field replacement")
    parser.add_argument("--vertical-csv",   default=None,
                        help="Path to RAT vertical curve CSV for field replacement")
    parser.add_argument("--debug", action="store_true",
                        help="Enable debug-level logging for troubleshooting")
    
    args = parser.parse_args()
    
    # Set logging level based on --debug flag
    if args.debug:
        logging.basicConfig(level=logging.DEBUG, 
                           format="%(asctime)s - %(levelname)s - %(funcName)s: %(message)s")
        logging.debug("Debug logging enabled - verbose output mode")
    else:
        logging.basicConfig(level=logging.INFO, 
                           format="%(asctime)s - %(levelname)s: %(message)s")
    
    # Now use args instead of global variables
    global OUTPUT_DIR, DEM_DIR, STATES_TO_PROCESS, FACILITY_TYPE_FILTER
    global FSYSTEM_FILTER, INCLUDE_ALL_HPMS_FIELDS, SELECTED_HPMS_FIELDS
    global HORIZONTAL_CSV, VERTICAL_CSV
    
    # Define defaults
    STATES_TO_PROCESS = ["31"]
    
    if args.params_json and os.path.exists(args.params_json):
        # GUI invocation
        with open(args.params_json, "r", encoding="utf-8") as f:
            p = json.load(f)
            global GLOBAL_PARAMS_JSON
            GLOBAL_PARAMS_JSON = p    
        OUTPUT_DIR             = args.outdir or p.get("OUTPUT_DIR", "")
        DEM_DIR                = args.demdir or p.get("DEM_DIR", "")
        FACILITY_TYPE_FILTER   = p.get("FACILITY_TYPE_FILTER", FACILITY_TYPE_FILTER)
        FSYSTEM_FILTER         = p.get("FSYSTEM_FILTER",       FSYSTEM_FILTER)
        gui_fields = p.get("INCLUDE_HPMS_FIELDS", "NOT_SET")
        if gui_fields != "NOT_SET":
            INCLUDE_ALL_HPMS_FIELDS = (gui_fields is None)
            SELECTED_HPMS_FIELDS    = gui_fields
        state_fips = str(p.get("STATE_FIPS", "")).zfill(2)
        STATES_TO_PROCESS = [state_fips] if state_fips and state_fips != "00" else STATES_TO_PROCESS
    else:
        # CLI invocation
        OUTPUT_DIR = args.outdir
        DEM_DIR = args.demdir
        if args.state and args.state.upper() == "ALL":
            STATES_TO_PROCESS = ALL_FIPS
        else:
            STATES_TO_PROCESS = [str(args.state).zfill(2)]
    
    # Validate required directories
    if not OUTPUT_DIR:
        logging.error("--outdir is required")
        raise SystemExit(1)
    if not DEM_DIR:
        logging.error("--demdir is required")
        raise SystemExit(1)
    
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    os.makedirs(DEM_DIR, exist_ok=True)
    
    # Process input
    if args.input:
        logging.info(f"Processing local file: {args.input}")
        df = load_local_hpms(args.input)
        display_fips = STATES_TO_PROCESS[0] if len(STATES_TO_PROCESS) == 1 else None
        run_state_enrichment("LOCAL", local_df=df, display_fips=display_fips)
    else:
        for state in STATES_TO_PROCESS:
            run_state_enrichment(state)
    
if __name__ == "__main__":
    main()