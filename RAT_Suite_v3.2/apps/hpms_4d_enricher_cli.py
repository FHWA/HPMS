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
RAT 4D ENRICHER ENGINE v3.2 (National Batch Processor)
--------------------------------------------------------------------------------
ROLE: Upgrades 2D HPMS tables into 3D geometries with linear referencing.
DESCRIPTION: 
Calculates a smoothed, continuous "macro-profile" for a route, then uses a 
Metric KDTree to snap fragmented HPMS tabular rows back to the smoothed alignment. 
Outputs WKT_ZM strings containing Longitude, Latitude, Elevation (Z), and 
Milepost (M) for advanced 4D digital twin modeling.
CHANGES:
  - Stripped out redundant fetch_socrata_state and load_local_hpms (now imported from core).
  - Fixed O(n^2) bottleneck: Replaced DataFrame filtering inside the ProcessPool loop with Pandas groupby().
CREATED BY: Federal Highway Administration, Office of Highway Policy Information.
CREATED ON: 5/14/2026
"""
import argparse
import os
import sys
import json
import logging
import math
from datetime import datetime
import numpy as np
import pandas as pd
import geopandas as gpd
from shapely.wkt import loads
from shapely.geometry import shape, LineString
from concurrent.futures import ProcessPoolExecutor, as_completed

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
    fetch_socrata_state,   # <--- Imported from core
    load_local_hpms,       # <--- Imported from core
    FEET_PER_METER
)
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s: %(message)s")


# ===========================================================================
# CONSTANTS & DEFAULTS
# ===========================================================================
SOCRATA_DEFAULT = "https://datahub.transportation.gov/resource/42um-tgh5.json"

ALL_FIPS = [
    "01", "02", "04", "05", "06", "08", "09", "10", "11", "12",
    "13", "15", "16", "17", "18", "19", "20", "21", "22", "23",
    "24", "25", "26", "27", "28", "29", "30", "31", "32", "33",
    "34", "35", "36", "37", "38", "39", "40", "41", "42", "44",
    "45", "46", "47", "48", "49", "50", "51", "53", "54", "55", "56", "72"
]


# ---------------------------------------------------------------------------
# Multiprocessing Worker Function
# ---------------------------------------------------------------------------
def process_4d_route(route_id: str, sub: pd.DataFrame, dem_dir: str, params: dict):
    results = {}
    download_dems(sub["WKT"].tolist(), dem_dir)
    
    predominant_f_sys = int(sub["FSystem"].mode()[0])
    
    macro_coords_wgs = []
    macro_z_vals = []
    
    lines = stitch_linestrings_ordered(sub["WKT"].tolist())
    for line in lines:
        chunk_res = smooth_plan_profile_from_linestring(line, dem_dir, params, predominant_f_sys)
        if chunk_res is not None:
            macro_coords_wgs.extend(chunk_res["coords_wgs_smooth"])
            macro_z_vals.extend(chunk_res["z_smooth"])
        
    if not macro_coords_wgs:
        return results
        
    tree, tx = build_metric_kdtree(macro_coords_wgs)
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
                    
            if total_geom_len_m == 0: total_geom_len_m = 1e-9
            
            current_len_m = 0.0
            row_s_mp = float(row["Start_MP"])
            row_e_mp = float(row["End_MP"])
            
            xyz = []
            xyzm_txt = []
            
            for part in parts:
                raw_coords = list(part.coords)
                q_idx = query_metric_kdtree(tree, tx, raw_coords)
                z_assigned = macro_z_array[q_idx]
                
                for i, (lon, lat) in enumerate(raw_coords):
                    if i > 0:
                        prev_lon, prev_lat = raw_coords[i-1]
                        curr_x, curr_y = tx.transform(lon, lat)
                        prev_x, prev_y = tx.transform(prev_lon, prev_lat)
                        current_len_m += math.hypot(curr_x - prev_x, curr_y - prev_y)
                        
                    f = current_len_m / total_geom_len_m
                    m = row_s_mp + f * (row_e_mp - row_s_mp)
                    
                    z = z_assigned[i]
                    xyz.append((lon, lat, float(z)))
                    xyzm_txt.append(f"{lon:.7f} {lat:.7f} {float(z):.2f} {m:.4f}")
                    
            geom3d = LineString(xyz)
            wkt_zm = f"LINESTRING ZM ({', '.join(xyzm_txt)})"
            
            results[row_idx] = {
                "geometry_3d": geom3d,
                "WKT_ZM": wkt_zm
            }
        except Exception:
            continue
            
    return results


# ---------------------------------------------------------------------------
# Core Execution Loop
# ---------------------------------------------------------------------------
def run_state_enrichment(state_fips: str, out_dir: str, dem_dir: str, user_params: dict, local_df: pd.DataFrame = None):
    state_out_dir = os.path.join(out_dir, f"Output_State_{state_fips}")
    os.makedirs(state_out_dir, exist_ok=True)
    
    stamp = datetime.now().strftime("%Y%m%d")
    csv_out = os.path.join(state_out_dir, f"hpms_4d_production_{state_fips}_{stamp}.csv")
    gpkg_out = os.path.join(state_out_dir, f"hpms_4d_production_{state_fips}_{stamp}.gpkg")
    blender_out = os.path.join(state_out_dir, f"hpms_4d_blender_export_{state_fips}_{stamp}.shp")

    # --- RESUME GUARD ---
    if os.path.exists(csv_out):
        logging.info(f"Skipping State {state_fips} - Already enriched ({csv_out} exists).")
        return

    logging.info(f"\n{'='*60}\n=== 4D ENRICHMENT: STATE FIPS {state_fips} ===\n{'='*60}")

    params = build_params(user_params)

    try:
        if local_df is not None:
            df = local_df.copy()
        else:
            df = fetch_socrata_state(state_fips, user_params.get("SOCRATA_TOKEN", ""))
    except Exception as e:
        logging.error(f"Failed to fetch data for State {state_fips}: {e}")
        return

    routes = df["RouteId"].dropna().unique().tolist()
    logging.info(f"Loaded {len(df):,} segments across {len(routes):,} routes.")

    master_results = {}
    completed = 0
    
    max_workers = user_params.get("MAX_WORKERS", max(1, os.cpu_count() - 2))
    logging.info(f"Spinning up {max_workers} CPU cores for parallel enrichment...")
    with ProcessPoolExecutor(max_workers=max_workers) as executor:
        
        # --- FIXED O(n^2) BOTTLENECK ---
        # Group by RouteId instantly using Pandas instead of filtering the whole df in a loop
        futures = {}
        for rid, subset in df.groupby("RouteId"):
            futures[executor.submit(process_4d_route, rid, subset, dem_dir, params)] = rid
        
        for fut in as_completed(futures):
            rid = futures[fut]
            completed += 1
            if completed % 100 == 0:
                logging.info(f"  ...Enriched {completed}/{len(routes)} routes")
            try:
                res = fut.result()
                master_results.update(res)
            except Exception as e:
                logging.error(f"Route {rid} crashed during enrichment: {e}")

    df["geometry_3d"] = df.index.map(lambda i: master_results[i]["geometry_3d"] if i in master_results else None)
    df["WKT_ZM"] = df.index.map(lambda i: master_results[i]["WKT_ZM"] if i in master_results else None)
    
    out_df = df[df["geometry_3d"].notna()].copy()
    if out_df.empty:
        logging.error("No 4D geometry generated. Run failed.")
        return

    # 1. Output the Master CSV 
    out_df.drop(columns=["WKT", "geometry_3d"], errors="ignore").to_csv(csv_out, index=False)
    
    # 2. Output the Master GeoPackage
    try:
        gdf = gpd.GeoDataFrame(out_df.drop(columns=["WKT"], errors="ignore"), geometry="geometry_3d", crs="EPSG:4326")
        gdf.to_file(gpkg_out, driver="GPKG")
    except Exception as e:
        logging.error(f"Failed to save GPKG: {e}")

    # 3. Output the Blender-specific Shapefile
    try:
        utm_crs = gdf.estimate_utm_crs()
        gdf_blender = gdf.to_crs(utm_crs)
        cols_to_drop = [c for c in gdf_blender.columns if c.upper() in ["WKT", "WKT_ZM"]]
        gdf_blender = gdf_blender.drop(columns=cols_to_drop, errors="ignore")
        gdf_blender.to_file(blender_out)
        logging.info(f"Saved Blender SHP: {os.path.basename(blender_out)} (Projected to {utm_crs.name})")
    except Exception as e:
        logging.error(f"Failed to save Blender shapefile: {e}")

    logging.info(f"Finished State {state_fips}!")


def main():
    parser = argparse.ArgumentParser(description="RAT 4D Enricher Engine CLI")
    parser.add_argument("--input",       default=None,  help="Local HPMS file (CSV or GeoPackage)")
    parser.add_argument("--outdir",      required=True, help="Output directory")
    parser.add_argument("--demdir",      required=True, help="DEM cache directory")
    parser.add_argument("--state",       default=None,  help="State FIPS code (or ALL)")
    parser.add_argument("--params_json", default=None,  help="Path to run_params.json for parameter overrides")
    args = parser.parse_args()

    user_params = {}
    if args.params_json and os.path.exists(args.params_json):
        with open(args.params_json, "r") as f:
            user_params = json.load(f)

    user_params.update({"OUTPUT_DIR": args.outdir, "DEM_DIR": args.demdir})
    if args.state:
        user_params["STATE_FIPS"] = args.state

    os.makedirs(args.outdir, exist_ok=True)
    os.makedirs(args.demdir, exist_ok=True)

    if args.input:
        logging.info(f"Processing local file: {args.input}")
        df = load_local_hpms(args.input)
        state_id = str(user_params.get("STATE_FIPS", "")).strip()
        if state_id == "00" or not state_id:
            state_id = "LOCAL"
        run_state_enrichment(state_id, args.outdir, args.demdir, user_params, local_df=df)
    elif args.state:
        states = ALL_FIPS if args.state.upper() == "ALL" else [args.state.zfill(2)]
        for s in states:
            run_state_enrichment(s, args.outdir, args.demdir, user_params)
    else:
        logging.error("No input specified. Use --input for a local file or --state for Socrata fetch.")

if __name__ == "__main__":
    main()