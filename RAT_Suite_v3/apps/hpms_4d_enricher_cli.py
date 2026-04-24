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
RAT 4D ENRICHER CLI (Z & M Coordinate Generator)
--------------------------------------------------------------------------------
ROLE: Upgrades 2D HPMS tables into 3D geometries with linear referencing.
DESCRIPTION: 
Calculates a smoothed, continuous "macro-profile" for a route, then uses a 
Metric KDTree to snap fragmented HPMS tabular rows back to the smoothed alignment. 
Outputs WKT_ZM strings containing Longitude, Latitude, Elevation (Z), and 
Milepost (M) for advanced 4D digital twin modeling.
CREATED BY: FHWA, Office of Highway Policy Information using Google Gemini and
ChatGPT.
CREATED ON: 4/23/2026
"""
import os
import sys
import json
import argparse
import logging
import math
import numpy as np
import pandas as pd
import geopandas as gpd
from shapely.wkt import loads
from shapely.geometry import shape, LineString
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
    download_dems
)
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s: %(message)s")

def load_local_hpms(path: str) -> pd.DataFrame:
    if path.lower().endswith(".csv"):
        df = pd.read_csv(path, low_memory=False)
        geom_col = next((c for c in df.columns if c.lower() in ["wkt", "wkt_zm", "geometry", "shape", "the_geom", "line"]), None)
        if geom_col:
            df["WKT"] = df[geom_col]
    else:
        gdf = gpd.read_file(path)
        if gdf.crs and gdf.crs.to_epsg() != 4326:
            gdf = gdf.to_crs(epsg=4326)
        df = pd.DataFrame(gdf.drop(columns="geometry"))
        df["WKT"] = gdf["geometry"].apply(lambda g: g.wkt if g else None)
    col_map = {}
    for col in df.columns:
        c = col.lower()
        if c in ["route_id", "routeid", "route", "id"]:
            col_map[col] = "RouteId"
        elif c in ["begin_point", "start_mp", "bmp", "begin", "beg_mp"]:
            col_map[col] = "Start_MP"
        elif c in ["end_point", "end_mp", "emp", "end"]:
            col_map[col] = "End_MP"
    df.rename(columns=col_map, inplace=True)
    if "RouteId" not in df.columns:
        raise ValueError("No RouteId field found.")
    if "Start_MP" not in df.columns:
        df["Start_MP"] = 0.0
    if "End_MP" not in df.columns:
        df["End_MP"] = 0.0
    df["RouteId"] = df["RouteId"].astype(str).str.strip()
    df["Start_MP"] = pd.to_numeric(df["Start_MP"], errors="coerce").fillna(0.0)
    df["End_MP"] = pd.to_numeric(df["End_MP"], errors="coerce").fillna(0.0)
    df = df.dropna(subset=["WKT"]).copy()
    return df

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True)
    parser.add_argument("--outdir", required=True)
    parser.add_argument("--demdir", required=True)
    parser.add_argument("--params_json", default=None)
    args = parser.parse_args()
    os.makedirs(args.outdir, exist_ok=True)
    user_params = {}
    if args.params_json:
        with open(args.params_json, "r", encoding="utf-8") as f:
            user_params = json.load(f)
    params = build_params(user_params)
    df = load_local_hpms(args.input)
    routes = df["RouteId"].dropna().unique().tolist()
    logging.info(f"Loaded {len(df):,} rows, {len(routes):,} routes.")
    results = {}
    for idx, rid in enumerate(routes, start=1):
        if idx % 100 == 0 or idx == 1:
            logging.info(f"Route {idx}/{len(routes)}: {rid}")
        sub = df[df["RouteId"] == rid].copy()
        
        # --- 1. Download DEMs for the route ---
        download_dems(sub["WKT"].tolist(), args.demdir)
        
        # --- 2. Build the smooth Macro Profile ---
        lines = stitch_linestrings_ordered(sub["WKT"].tolist())
        if not lines:
            continue
        macro_coords_wgs = []
        macro_z_vals = []
        for line in lines:
            chunk_res = smooth_plan_profile_from_linestring(line, args.demdir, params)
            if chunk_res is not None:
                macro_coords_wgs.extend(chunk_res["coords_wgs_smooth"])
                macro_z_vals.extend(chunk_res["z_smooth"])
        if not macro_coords_wgs:
            continue
        tree, tx = build_metric_kdtree(macro_coords_wgs)
        macro_z_array = np.array(macro_z_vals)
        # --- 3. Map RAW vertices to the Macro Profile ---
        for row_idx, row in sub.iterrows():
            try:
                g = loads(row["WKT"]) if isinstance(row["WKT"], str) else shape(row["WKT"])
                if g.is_empty:
                    continue
                    
                parts = list(g.geoms) if g.geom_type == "MultiLineString" else [g]
                
                # Calculate total raw length to proportion M-values accurately
                total_geom_len = sum([p.length for p in parts])
                if total_geom_len == 0: total_geom_len = 1e-9
                
                current_len = 0.0
                row_s_mp = float(row["Start_MP"])
                row_e_mp = float(row["End_MP"])
                
                xyz = []
                xyzm_txt = []
                
                for part in parts:
                    raw_coords = list(part.coords)
                    
                    # Query KDTree using raw coordinates
                    q_idx = query_metric_kdtree(tree, tx, raw_coords)
                    z_assigned = macro_z_array[q_idx]
                    
                    for i, (lon, lat) in enumerate(raw_coords):
                        if i > 0:
                            prev_lon, prev_lat = raw_coords[i-1]
                            current_len += math.hypot(lon - prev_lon, lat - prev_lat)
                            
                        f = current_len / total_geom_len
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
    df["geometry_3d"] = df.index.map(lambda i: results[i]["geometry_3d"] if i in results else None)
    df["WKT_ZM"] = df.index.map(lambda i: results[i]["WKT_ZM"] if i in results else None)
    out_df = df[df["geometry_3d"].notna()].copy()
    if out_df.empty:
        raise RuntimeError("No 4D geometry generated.")
    csv_out = os.path.join(args.outdir, "hpms_4d_production.csv")
    gpkg_out = os.path.join(args.outdir, "hpms_4d_production.gpkg")
    out_df.drop(columns=["WKT"], errors="ignore").to_csv(csv_out, index=False)
    gdf = gpd.GeoDataFrame(out_df.drop(columns=["WKT"], errors="ignore"), geometry="geometry_3d", crs="EPSG:4326")
    gdf.to_file(gpkg_out, driver="GPKG")
    # --- BLENDER SPECIFIC SHAPEFILE EXPORT ---
    blender_out = os.path.join(args.outdir, "hpms_4d_blender_export.shp")
    try:
        # 1. Project to UTM so X, Y, and Z are all measured in meters
        utm_crs = gdf.estimate_utm_crs()
        gdf_blender = gdf.to_crs(utm_crs)
        
        # 2. Shapefiles have strict character limits. Drop massive text fields that cause crashes.
        cols_to_drop = [c for c in gdf_blender.columns if c.upper() in ["WKT", "WKT_ZM"]]
        gdf_blender = gdf_blender.drop(columns=cols_to_drop, errors="ignore")
        
        # 3. Save the 3D Shapefile
        gdf_blender.to_file(blender_out)
        logging.info(f"Saved Blender SHP: {blender_out} (Projected to {utm_crs.name})")
    except Exception as e:
        logging.error(f"Failed to save Blender shapefile: {e}")
    logging.info(f"Saved CSV:  {csv_out}")
    logging.info(f"Saved GPKG: {gpkg_out}")
    logging.info("Done.")
if __name__ == "__main__":
    main()