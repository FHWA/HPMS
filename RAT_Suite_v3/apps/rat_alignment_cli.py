# apps/rat_alignment_cli.py

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
RAT BULK ALIGNMENT CLI
--------------------------------------------------------------------------------
ROLE: Batch processor for statewide horizontal and vertical curve detection.
DESCRIPTION: 
Ingests HPMS datasets (via Socrata API or local files) and iterates through 
routes to extract geometric curves. Bridges gaps in disjointed data, handles 
proportional milepost assignments, and outputs comprehensive curve tables, 
GeoJSON/Shapefiles, HTML Folium maps, and analytical dashboards.
CREATED BY: FHWA, Office of Highway Policy Information using Google Gemini and
ChatGPT.
CREATED ON: 4/23/2026
"""
import os
import sys
import json
import argparse
import logging
from datetime import datetime
import pandas as pd
import geopandas as gpd
from shapely.geometry import LineString
import folium
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import shapely
import pyproj
# ----------------------------
# Path bootstrap for core import
# ----------------------------
THIS_DIR = os.path.dirname(os.path.abspath(__file__))
RAT_SUITE_DIR = os.path.dirname(THIS_DIR)
if RAT_SUITE_DIR not in sys.path:
    sys.path.insert(0, RAT_SUITE_DIR)
from pyproj import Transformer
from shapely.geometry import LineString
from core.rat_core import (
    build_params,
    stitch_linestrings_ordered,
    smooth_plan_profile_from_linestring,
    analyze_horizontal_curvature,
    analyze_vertical_parabolic,
    merge_horizontal_curves,
    get_appropriate_utm_zone,
    download_dems
)
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s: %(message)s")

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
        df["WKT"] = gdf["geometry"].apply(lambda geom: geom.wkt if geom else None)
    
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

def process_route(route_id: str, subset: pd.DataFrame, dem_dir: str, params: dict):
    subset = subset.sort_values("Start_MP")
    download_dems(subset["WKT"].tolist(), dem_dir)
    
    # Identify contiguous blocks of FSystem and Is_Urban
    subset['block'] = (subset[['FSystem', 'Is_Urban']] != subset[['FSystem', 'Is_Urban']].shift()).any(axis=1).cumsum()
    
    all_chunks = []
    for block_id, chunk in subset.groupby('block'):
        f_sys = int(chunk["FSystem"].iloc[0])
        is_urban = bool(chunk["Is_Urban"].iloc[0])
        lines = stitch_linestrings_ordered(chunk["WKT"].tolist())
        for g in lines:
            all_chunks.append({"geom": g, "f_sys": f_sys, "is_urban": is_urban})

    if not all_chunks:
        return [], []

    h_all, v_all = [], []
    total_stitch_len_m = 0.0
    
    for info in all_chunks:
        lon, lat = info["geom"].coords[0]
        utm = get_appropriate_utm_zone(lon, lat)
        proj = Transformer.from_crs("EPSG:4326", f"EPSG:{utm}", always_xy=True)
        coords_m = [proj.transform(x, y) for x, y in info["geom"].coords]
        info["length_m"] = LineString(coords_m).length
        total_stitch_len_m += info["length_m"]
        
    if total_stitch_len_m == 0: total_stitch_len_m = 1.0
    cumulative_stitch_dist_m = 0.0
    route_s = float(subset["Start_MP"].min())
    route_e = float(subset["End_MP"].max())
    
    for part_idx, info in enumerate(all_chunks):
        chunk_length_m = info["length_m"]
        chunk_start_mp = route_s + ((cumulative_stitch_dist_m / total_stitch_len_m) * (route_e - route_s))
        cumulative_stitch_dist_m += chunk_length_m
        chunk_end_mp = route_s + ((cumulative_stitch_dist_m / total_stitch_len_m) * (route_e - route_s))
        
        # Pass the block's specific fs/urban flags
        res = smooth_plan_profile_from_linestring(info["geom"], dem_dir, params, info["f_sys"], info["is_urban"])
        if res is None:
            continue
            
        spacing_m = res["spacing_m"]
        coords_m_smooth = res["coords_m_smooth"]
        coords_wgs_smooth = res["coords_wgs_smooth"]
        z_smooth = res["z_smooth"]
        h_curves = analyze_horizontal_curvature(coords_m_smooth, spacing_m, params, info["is_urban"])
        if params.get("ENABLE_MERGE", False):
            h_curves = merge_horizontal_curves(h_curves, params)
        v_curves = analyze_vertical_parabolic(z_smooth, spacing_m, params, info["is_urban"])
        total_len = max(res["d_axis"][-1], 1.0)
        for c in h_curves:
            p0 = c["Start_Dist"] / total_len
            p1 = c["End_Dist"] / total_len
            c["RouteId"] = route_id
            c["Part"] = part_idx + 1
            # --- FIX: Apply the calculated chunk boundaries ---
            c["Calibrated_Start_MP"] = chunk_start_mp + p0 * (chunk_end_mp - chunk_start_mp)
            c["Calibrated_End_MP"] = chunk_start_mp + p1 * (chunk_end_mp - chunk_start_mp)
            try:
                i0 = max(0, int(c["Start_Dist"] / spacing_m))
                i1 = min(len(coords_wgs_smooth) - 1, int(c["End_Dist"] / spacing_m))
                if i1 > i0:
                    c["geometry"] = LineString(coords_wgs_smooth[i0:i1+1])
            except Exception:
                pass
        for c in v_curves:
            p0 = c["Start_Dist"] / total_len
            p1 = c["End_Dist"] / total_len
            c["RouteId"] = route_id
            c["Part"] = part_idx + 1
            # --- FIX: Apply the calculated chunk boundaries ---
            c["Calibrated_Start_MP"] = chunk_start_mp + p0 * (chunk_end_mp - chunk_start_mp)
            c["Calibrated_End_MP"] = chunk_start_mp + p1 * (chunk_end_mp - chunk_start_mp)
            try:
                i0 = max(0, int(c["Start_Dist"] / spacing_m))
                i1 = min(len(coords_wgs_smooth) - 1, int(c["End_Dist"] / spacing_m))
                if i1 > i0:
                    c["geometry"] = LineString(coords_wgs_smooth[i0:i1+1])
            except Exception:
                pass
        h_all.extend(h_curves)
        v_all.extend(v_curves)
    return h_all, v_all

def generate_html_map(df_h, df_v, out_html):
    import numpy as np
    
    # Default center, will be overridden by fit_bounds
    m = folium.Map(location=[39.5, -98.35], zoom_start=6, tiles="CartoDBPositron")
    bounds_list = []
    color_map = {'A': 'green', 'B': '#a6d96a', 'C': '#fdae61', 'D': '#d7191c', 'E': '#9e0142', 'F': 'purple'}
    if not df_h.empty and "geometry" in df_h.columns:
        gdf_h = gpd.GeoDataFrame(df_h, geometry="geometry", crs="EPSG:4326")
        bounds_list.append(gdf_h.total_bounds)
        
        folium.GeoJson(
            gdf_h,
            name="Horizontal Curves",
            style_function=lambda f: {
                "color": color_map.get(f['properties'].get('Bin', 'A'), 'gray'),
                "weight": 4,
                "dashArray": '10, 10' if f['properties'].get('Merge_Status') == 'Compound' else ''
            },
            tooltip=folium.GeoJsonTooltip(fields=[c for c in ["RouteId", "Bin", "Radius_m", "Delta", "Merge_Status"] if c in gdf_h.columns])
        ).add_to(m)
    if not df_v.empty and "geometry" in df_v.columns:
        gdf_v = gpd.GeoDataFrame(df_v, geometry="geometry", crs="EPSG:4326")
        bounds_list.append(gdf_v.total_bounds)
        
        folium.GeoJson(
            gdf_v,
            name="Vertical Curves",
            style_function=lambda f: {
                "color": color_map.get(f['properties'].get('Grade_Bin', 'A'), 'gray'),
                "weight": 4
            },
            tooltip=folium.GeoJsonTooltip(fields=[c for c in ["RouteId", "Type", "Grade_Bin", "K_Value", "Alg_Diff"] if c in gdf_v.columns])
        ).add_to(m)
    # 1. Fit Bounds (Auto-Zoom to the data)
    if bounds_list:
        b = np.vstack(bounds_list)
        minx, miny, maxx, maxy = np.min(b[:,0]), np.min(b[:,1]), np.max(b[:,2]), np.max(b[:,3])
        m.fit_bounds([[miny, minx], [maxy, maxx]])
    # 2. Add Legend
    legend_html = '''
    <div style="position: fixed; bottom: 50px; right: 50px; width: 160px; border:2px solid grey; z-index:9999; background:white; padding: 10px; font-family: sans-serif; font-size: 12px;">
    <b>Curve / Grade Bin</b><br>
    <i style="background:green; width:10px; height:10px; display:inline-block;"></i> A<br>
    <i style="background:#a6d96a; width:10px; height:10px; display:inline-block;"></i> B<br>
    <i style="background:#fdae61; width:10px; height:10px; display:inline-block;"></i> C<br>
    <i style="background:#d7191c; width:10px; height:10px; display:inline-block;"></i> D<br>
    <i style="background:#9e0142; width:10px; height:10px; display:inline-block;"></i> E<br>
    <i style="background:purple; width:10px; height:10px; display:inline-block;"></i> F<br>
    <hr style="margin: 5px 0; border: 0; border-top: 1px solid #ccc;">
    <b>Curve Type</b><br>
    <i style="border-top: 3px solid black; width:15px; display:inline-block; margin-bottom: 3px;"></i> Simple / Vertical<br>
    <i style="border-top: 3px dashed black; width:15px; display:inline-block; margin-bottom: 3px;"></i> Compound (H)<br>
    </div>'''
    
    m.get_root().html.add_child(folium.Element(legend_html))
    folium.LayerControl().add_to(m)
    m.save(out_html)

def generate_dashboard(df_h, df_v, out_html, out_dir):
    chart_h = os.path.join(out_dir, "chart_horizontal_bins.png")
    chart_v = os.path.join(out_dir, "chart_vertical_bins.png")
    if not df_h.empty and "Bin" in df_h.columns:
        vc = df_h["Bin"].value_counts().reindex(list("ABCDEF"), fill_value=0)
        plt.figure(figsize=(6, 4))
        vc.plot(kind="bar", color="tomato")
        plt.title("Horizontal Curve Bins")
        plt.tight_layout()
        plt.savefig(chart_h, dpi=120)
        plt.close()
    if not df_v.empty and "Grade_Bin" in df_v.columns:
        vc = df_v["Grade_Bin"].value_counts().reindex(list("ABCDEF"), fill_value=0)
        plt.figure(figsize=(6, 4))
        vc.plot(kind="bar", color="steelblue")
        plt.title("Vertical Grade Bins")
        plt.tight_layout()
        plt.savefig(chart_v, dpi=120)
        plt.close()
    html = f"""
    <html><head><title>RAT Summary Dashboard</title></head>
    <body style="font-family:Arial; margin:20px;">
    <h2>RAT Summary Dashboard</h2>
    <p>Horizontal curves: {len(df_h):,}</p>
    <p>Vertical curves: {len(df_v):,}</p>
    <h3>Top Sharpest Horizontal (smallest Radius_m)</h3>
    {df_h.sort_values("Radius_m").head(20).to_html(index=False) if not df_h.empty and "Radius_m" in df_h.columns else "<p>No data</p>"}
    <h3>Top Vertical by |Alg_Diff|</h3>
    {df_v.reindex(df_v["Alg_Diff"].abs().sort_values(ascending=False).index).head(20).to_html(index=False) if not df_v.empty and "Alg_Diff" in df_v.columns else "<p>No data</p>"}
    <h3>Charts</h3>
    {"<img src='chart_horizontal_bins.png' style='max-width:700px;'><br>" if os.path.exists(chart_h) else ""}
    {"<img src='chart_vertical_bins.png' style='max-width:700px;'><br>" if os.path.exists(chart_v) else ""}
    </body></html>
    """
    with open(out_html, "w", encoding="utf-8") as f:
        f.write(html)

def export_qa_exceptions(df_h, df_v, out_csv):
    issues = []
    if not df_h.empty:
        d = df_h.copy()
        if "Length_m" in d.columns:
            issues.append(d[d["Length_m"] <= 0].assign(QA_Reason="H: Length_m <= 0"))
        if "Radius_m" in d.columns:
            issues.append(d[d["Radius_m"] <= 0].assign(QA_Reason="H: Radius_m <= 0"))
        if "Start_Dist" in d.columns and "End_Dist" in d.columns:
            issues.append(d[d["End_Dist"] <= d["Start_Dist"]].assign(QA_Reason="H: End_Dist <= Start_Dist"))
    if not df_v.empty:
        d = df_v.copy()
        if "Length_m" in d.columns:
            issues.append(d[d["Length_m"] <= 0].assign(QA_Reason="V: Length_m <= 0"))
        if "K_Value" in d.columns:
            issues.append(d[d["K_Value"] <= 0].assign(QA_Reason="V: K_Value <= 0"))
        if "Start_Dist" in d.columns and "End_Dist" in d.columns:
            issues.append(d[d["End_Dist"] <= d["Start_Dist"]].assign(QA_Reason="V: End_Dist <= Start_Dist"))
    if issues:
        out = pd.concat(issues, ignore_index=True).drop_duplicates()
    else:
        out = pd.DataFrame(columns=["QA_Reason"])
    out.to_csv(out_csv, index=False)

def export_geo(df, out_base, flags):
    """Optional geospatial exports (GeoJSON/GPKG/SHP) if geometry exists."""
    if df.empty or "geometry" not in df.columns:
        return
    try:
        gdf = gpd.GeoDataFrame(df, geometry="geometry", crs="EPSG:4326")
    except Exception:
        return
    if flags["geojson"]:
        gdf.to_file(out_base + ".geojson", driver="GeoJSON")
    if flags["gpkg"]:
        gdf.to_file(out_base + ".gpkg", driver="GPKG")
    if flags["shp"]:
        gdf.to_file(out_base + ".shp")

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True, help="Local HPMS file (.csv/.shp/.geojson)")
    parser.add_argument("--outdir", required=True, help="Output directory")
    parser.add_argument("--demdir", required=True, help="DEM cache directory")
    parser.add_argument("--params_json", default=None, help="Optional JSON file of parameter overrides")
    args = parser.parse_args()
    
    os.makedirs(args.outdir, exist_ok=True)
    user_params = {}
    if args.params_json:
        with open(args.params_json, "r", encoding="utf-8") as f:
            user_params = json.load(f)
    params = build_params(user_params)
    logging.info("Loading input...")
    df = load_local_hpms(args.input)
    logging.info(f"Rows loaded: {len(df):,}")
    all_h, all_v = [], []
    routes = df["RouteId"].dropna().unique().tolist()
    logging.info(f"Routes found: {len(routes):,}")
    for i, rid in enumerate(routes, start=1):
        if i % 100 == 0 or i == 1:
            logging.info(f"Processing route {i}/{len(routes)}: {rid}")
        subset = df[df["RouteId"] == rid]
        h, v = process_route(rid, subset, args.demdir, params)
        all_h.extend(h)
        all_v.extend(v)
    flags = {
        "csv": bool(user_params.get("OUT_CSV", True)),
        "geojson": bool(user_params.get("OUT_GEOJSON", True)),
        "gpkg": bool(user_params.get("OUT_GPKG", False)),
        "shp": bool(user_params.get("OUT_SHP", False)),
        "html_map": bool(user_params.get("OUT_HTML_MAP", True)),
        "dashboard": bool(user_params.get("OUT_DASHBOARD", True)),
        "qa_ex": bool(user_params.get("OUT_QA_EXCEPTIONS", True)),
    }
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    h_csv = os.path.join(args.outdir, f"alignment_horizontal_{stamp}.csv")
    v_csv = os.path.join(args.outdir, f"alignment_vertical_{stamp}.csv")
    df_h = pd.DataFrame(all_h)
    df_v = pd.DataFrame(all_v)
    if flags["csv"]:
        df_h.to_csv(h_csv, index=False)
        df_v.to_csv(v_csv, index=False)
        logging.info(f"Saved horizontal curves: {h_csv}")
        logging.info(f"Saved vertical curves:   {v_csv}")
    # optional geospatial exports
    export_geo(df_h, os.path.join(args.outdir, f"alignment_horizontal_{stamp}"), flags)
    export_geo(df_v, os.path.join(args.outdir, f"alignment_vertical_{stamp}"), flags)
    # optional html map
    if flags["html_map"]:
        out_map = os.path.join(args.outdir, f"alignment_map_{stamp}.html")
        generate_html_map(df_h, df_v, out_map)
        logging.info(f"Saved HTML map: {out_map}")
    # optional dashboard
    if flags["dashboard"]:
        out_dash = os.path.join(args.outdir, f"alignment_dashboard_{stamp}.html")
        generate_dashboard(df_h, df_v, out_dash, args.outdir)
        logging.info(f"Saved dashboard: {out_dash}")
    logging.info("Done.")
if __name__ == "__main__":
    main()