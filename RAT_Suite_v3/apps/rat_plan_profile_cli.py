# apps/rat_plan_profile_cli.py

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
RAT PLAN & PROFILE CLI (Data Pre-Processor)
--------------------------------------------------------------------------------
ROLE: Route-specific data extractor for engineering sheets.
DESCRIPTION: 
Isolates a single route (or specific milepost bounds), stitches the geometry, 
and applies the core smoothing engine. It calculates exact continuous distances 
across disjointed chunks to prevent overlapping, and exports the raw and smoothed 
vertices to CSVs to be consumed by the downstream PDF rendering script.
CREATED BY: FHWA, Office of Highway Policy Information using Google Gemini and
ChatGPT.
CREATED ON: 4/23/2026
"""
import os
import sys
import re
import json
import argparse
import logging
import numpy as np
import pandas as pd
import geopandas as gpd
# --- ADDED IMPORTS FOR UTM MATH ---
from shapely.geometry import LineString
from pyproj import Transformer
THIS_DIR = os.path.dirname(os.path.abspath(__file__))
RAT_SUITE_DIR = os.path.dirname(THIS_DIR)
if RAT_SUITE_DIR not in sys.path:
    sys.path.insert(0, RAT_SUITE_DIR)
from core.rat_core import (
    build_params,
    FEET_PER_METER,
    stitch_linestrings_ordered,
    smooth_plan_profile_from_linestring,
    analyze_horizontal_curvature,
    analyze_vertical_parabolic,
    merge_horizontal_curves,
    calculate_headings,
    get_tangent_grade,
    get_appropriate_utm_zone # <-- ADDED IMPORT
)
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s: %(message)s")

def load_local_hpms(path: str) -> pd.DataFrame:
    """
    Load local CSV/SHP/GEOJSON and normalize required columns:
    RouteId, Start_MP, End_MP, WKT
    """
    if not os.path.exists(path):
        raise FileNotFoundError(path)
    if path.lower().endswith(".csv"):
        df = pd.read_csv(path, low_memory=False)
        geom_col = next(
            (c for c in df.columns if c.lower() in ["wkt", "wkt_zm", "geometry", "shape", "the_geom", "line"]),
            None
        )
        if geom_col:
            df["WKT"] = df[geom_col]
    else:
        gdf = gpd.read_file(path)
        if gdf.crs and gdf.crs.to_epsg() != 4326:
            logging.info("Reprojecting local GIS file to EPSG:4326...")
            gdf = gdf.to_crs(epsg=4326)
        df = pd.DataFrame(gdf.drop(columns="geometry"))
        df["WKT"] = gdf["geometry"].apply(lambda g: g.wkt if g else None)
    
    # fuzzy rename
    col_map = {}
    for col in df.columns:
        c = col.lower()
        if c in ["route_id", "routeid", "route", "id"]:
            col_map[col] = "RouteId"
        elif c in ["begin_point", "start_mp", "bmp", "begin", "beg_mp"]:
            col_map[col] = "Start_MP"
        elif c in ["end_point", "end_mp", "emp", "end"]:
            col_map[col] = "End_MP"
        elif c in ["f_system", "fsystem", "func_sys"]:
            col_map[col] = "FSystem"
        elif c in ["urban_id", "urbanid", "urban_code"]:
            col_map[col] = "UrbanID"
    df.rename(columns=col_map, inplace=True)

    # required fields
    if "RouteId" not in df.columns:
        raise ValueError("Missing RouteId column after normalization.")
    if "WKT" not in df.columns:
        raise ValueError("Missing geometry/WKT column after normalization.")

    # normalization & defaults
    if "Start_MP" not in df.columns: df["Start_MP"] = 0.0
    if "End_MP" not in df.columns: df["End_MP"] = 0.0
    if "FSystem" not in df.columns: df["FSystem"] = 1
    if "UrbanID" not in df.columns: df["UrbanID"] = 99999

    df["RouteId"] = df["RouteId"].astype(str).str.strip().str.upper()
    df["Start_MP"] = pd.to_numeric(df["Start_MP"], errors="coerce").fillna(0.0)
    df["End_MP"] = pd.to_numeric(df["End_MP"], errors="coerce").fillna(0.0)
    df["FSystem"] = pd.to_numeric(df["FSystem"], errors="coerce").fillna(1).astype(int)
    
    df["UrbanID"] = pd.to_numeric(df["UrbanID"], errors="coerce").fillna(99999)
    df["Is_Urban"] = (df["UrbanID"] != 99999) & (df["UrbanID"] != 0)

    # drop bad geometry
    df["WKT"] = df["WKT"].astype(str).str.strip()
    df = df[df["WKT"].notna() & (df["WKT"] != "")].copy()
    return df
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
def build_vertices_df(
    res: dict,
    route_id: str,
    chunk_s_mp: float,
    chunk_e_mp: float,
    global_start_dist_m: float,
    params: dict
) -> pd.DataFrame:
    """
    Builds the vertex dataframe for a single chunk, tracking the global distance
    so multiple chunks graph continuously on the X-Axis in the PDF.
    """
    coords_wgs = res["coords_wgs_smooth"]
    coords_m = res["coords_m_smooth"]
    z = res["z_smooth"]
    spacing = res["spacing_m"]
    coords_wgs_raw = res["coords_wgs_raw"]
    z_raw = res["z_raw"]
    headings_unwrapped = calculate_headings(coords_m)
    grads = np.array([
        get_tangent_grade(z, i, spacing, params["REGRESSION_WINDOW_M"])
        for i in range(len(z))
    ])
    total_len = max(float(res["d_axis"][-1]), 1.0)
    rows = []
    for i, (xy, zz) in enumerate(zip(coords_wgs, z)):
        local_dist_m = i * spacing
        frac = local_dist_m / total_len
        mp = chunk_s_mp + frac * (chunk_e_mp - chunk_s_mp)
        
        # Add global distance so disconnected chunks don't overlap on the PDF X-Axis
        continuous_dist_ft = (global_start_dist_m + local_dist_m) * FEET_PER_METER
        rows.append({
            "RouteId": route_id,
            "Milepost": round(mp, 4),
            "Dist_Ft": round(continuous_dist_ft, 3),
            "Lon": xy[0],
            "Lat": xy[1],
            "Elev_Ft": round(float(zz) * FEET_PER_METER, 3),
            "Raw_Lon": coords_wgs_raw[i][0],
            "Raw_Lat": coords_wgs_raw[i][1],
            "Elev_Raw_Ft": round(float(z_raw[i]) * FEET_PER_METER, 3),
            "Grade_Pct": round(float(grads[i]), 3),
            "Heading_Deg_Unwrapped": round(float(headings_unwrapped[i]), 5),
        })
    return pd.DataFrame(rows)

def safe_route_name(route_id: str) -> str:
    return re.sub(r'[<>:"/\\|?*]+', "-", str(route_id)).strip()

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True, help="Local HPMS file (.csv/.shp/.geojson)")
    parser.add_argument("--route", required=True, help="Route ID")
    parser.add_argument("--outdir", required=True, help="Output directory")
    parser.add_argument("--demdir", required=True, help="DEM cache directory")
    parser.add_argument("--params_json", default=None, help="Optional JSON parameter overrides")
    parser.add_argument("--start", type=float, default=None, help="Optional start RP override")
    parser.add_argument("--end", type=float, default=None, help="Optional end RP override")
    args = parser.parse_args()
    os.makedirs(args.outdir, exist_ok=True)
    os.makedirs(args.demdir, exist_ok=True)
    user_params = {}
    if args.params_json:
        try:
            with open(args.params_json, "r", encoding="utf-8") as f:
                user_params = json.load(f)
        except Exception as e:
            raise ValueError(f"Failed to parse params_json '{args.params_json}': {e}") from e
    params = build_params(user_params)
    logging.info("Loading input...")
    df = load_local_hpms(args.input)
    logging.info(f"Rows loaded: {len(df):,}")
    route_norm = str(args.route).strip().upper()
    sub = df[df["RouteId"] == route_norm].sort_values("Start_MP")
    if sub.empty:
        raise ValueError(f"No records found for route: {args.route}")
    if args.start is not None or args.end is not None:
        clip_s = float(args.start) if args.start is not None else float(sub["Start_MP"].min())
        clip_e = float(args.end) if args.end is not None else float(sub["End_MP"].max())
        if clip_e < clip_s:
            raise ValueError(f"Invalid RP range: end ({clip_e}) < start ({clip_s})")
        sub = sub[(sub["Start_MP"] <= clip_e) & (sub["End_MP"] >= clip_s)].copy()
        if sub.empty:
            raise ValueError("No geometry overlaps the requested start/end RP range.")
    logging.info(f"Rows for route {route_norm}: {len(sub):,}")
    download_dems(sub["WKT"].tolist(), args.demdir)
    
    # Identify contiguous blocks
    sub['block'] = (sub[['FSystem', 'Is_Urban']] != sub[['FSystem', 'Is_Urban']].shift()).any(axis=1).cumsum()
    
    all_chunks = []
    for block_id, chunk in sub.groupby('block'):
        f_sys = int(chunk["FSystem"].iloc[0])
        is_urban = bool(chunk["Is_Urban"].iloc[0])
        lines = stitch_linestrings_ordered(chunk["WKT"].tolist())
        for g in lines:
            all_chunks.append({"geom": g, "f_sys": f_sys, "is_urban": is_urban})

    if not all_chunks:
        raise ValueError("No valid stitched geometry found.")

    total_stitch_len_m = 0.0
    for info in all_chunks:
        lon, lat = info["geom"].coords[0]
        utm = get_appropriate_utm_zone(lon, lat)
        proj = Transformer.from_crs("EPSG:4326", f"EPSG:{utm}", always_xy=True)
        coords_m = [proj.transform(x, y) for x, y in info["geom"].coords]
        info["length_m"] = LineString(coords_m).length
        total_stitch_len_m += info["length_m"]

    if total_stitch_len_m == 0: total_stitch_len_m = 1.0
    
    route_s_mp = float(args.start) if args.start is not None else float(sub["Start_MP"].min())
    route_e_mp = float(args.end) if args.end is not None else float(sub["End_MP"].max())
    
    all_h, all_v, all_vtx_dfs = [], [], []
    cumulative_stitch_dist_m = 0.0
    global_continuous_dist_m = 0.0 

    for idx, info in enumerate(all_chunks):
        chunk_length_m = info["length_m"]
        chunk_s_mp = route_s_mp + ((cumulative_stitch_dist_m / total_stitch_len_m) * (route_e_mp - route_s_mp))
        cumulative_stitch_dist_m += chunk_length_m
        chunk_e_mp = route_s_mp + ((cumulative_stitch_dist_m / total_stitch_len_m) * (route_e_mp - route_s_mp))
        
        res = smooth_plan_profile_from_linestring(info["geom"], args.demdir, params, info["f_sys"], info["is_urban"])
        if res is None:
            continue
        spacing_m = res["spacing_m"]
        h = analyze_horizontal_curvature(res["coords_m_smooth"], spacing_m, params, info["is_urban"])
        if params.get("ENABLE_MERGE", False):
            h = merge_horizontal_curves(h, params)
        v = analyze_vertical_parabolic(res["z_smooth"], spacing_m, params, info["is_urban"])
        total_len = max(float(res["d_axis"][-1]), 1.0)
        for c in h:
            c["RouteId"] = route_norm
            c["Calibrated_Start_MP"] = chunk_s_mp + (c["Start_Dist"] / total_len) * (chunk_e_mp - chunk_s_mp)
            c["Calibrated_End_MP"] = chunk_s_mp + (c["End_Dist"] / total_len) * (chunk_e_mp - chunk_s_mp)
            c["Start_Dist"] += global_continuous_dist_m
            c["End_Dist"] += global_continuous_dist_m
        for c in v:
            c["RouteId"] = route_norm
            c["Calibrated_Start_MP"] = chunk_s_mp + (c["Start_Dist"] / total_len) * (chunk_e_mp - chunk_s_mp)
            c["Calibrated_End_MP"] = chunk_s_mp + (c["End_Dist"] / total_len) * (chunk_e_mp - chunk_s_mp)
            c["Start_Dist"] += global_continuous_dist_m
            c["End_Dist"] += global_continuous_dist_m
        all_h.extend(h)
        all_v.extend(v)
        # Build Vertices and increment plotting X-axis for the next chunk
        vtx_df = build_vertices_df(res, route_norm, chunk_s_mp, chunk_e_mp, global_continuous_dist_m, params)
        all_vtx_dfs.append(vtx_df)
        
        global_continuous_dist_m += total_len
    if not all_vtx_dfs:
        raise RuntimeError("Smoothing failed for all route chunks (insufficient geometry points).")
    final_vtx_df = pd.concat(all_vtx_dfs, ignore_index=True)
    final_h_df = pd.DataFrame(all_h)
    final_v_df = pd.DataFrame(all_v)
    safe_route = safe_route_name(route_norm)
    base = f"plan_profile_{safe_route}"
    vertices_csv = os.path.join(args.outdir, f"{base}_vertices.csv")
    h_csv = os.path.join(args.outdir, f"{base}_horizontal.csv")
    v_csv = os.path.join(args.outdir, f"{base}_vertical.csv")
    final_vtx_df.to_csv(vertices_csv, index=False)
    if not final_h_df.empty:
        final_h_df.to_csv(h_csv, index=False)
    else:
        pd.DataFrame(columns=["RouteId"]).to_csv(h_csv, index=False)
        
    if not final_v_df.empty:
        final_v_df.to_csv(v_csv, index=False)
    else:
        pd.DataFrame(columns=["RouteId"]).to_csv(v_csv, index=False)
    logging.info(f"Saved vertices:   {vertices_csv}")
    logging.info(f"Saved horizontal: {h_csv}")
    logging.info(f"Saved vertical:   {v_csv}")
    logging.info("Done.")
if __name__ == "__main__":
    main()