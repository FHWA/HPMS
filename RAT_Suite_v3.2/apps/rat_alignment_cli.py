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
RAT BULK ALIGNMENT ENGINE v3.3
--------------------------------------------------------------------------------
ROLE: National batch processor for statewide horizontal and vertical curve detection.
DESCRIPTION:
Ingests HPMS datasets (via Socrata API or local files) and iterates through
routes to extract geometric curves. Now supports CLI argument overrides and 
standardized parameter keys from the Unified GUI.
"""
import os
import sys
import json
import logging
import argparse
from datetime import datetime
import numpy as np
import pandas as pd
import geopandas as gpd
from shapely.geometry import LineString
import folium
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from concurrent.futures import ProcessPoolExecutor, as_completed

# ----------------------------
# Path bootstrap for core import
# ----------------------------
THIS_DIR = os.path.dirname(os.path.abspath(__file__))
RAT_SUITE_DIR = os.path.dirname(THIS_DIR)
if RAT_SUITE_DIR not in sys.path:
    sys.path.insert(0, RAT_SUITE_DIR)
    
from pyproj import Transformer
from core.rat_core import (
    build_params,
    stitch_linestrings_ordered,
    smooth_plan_profile_from_linestring,
    analyze_horizontal_curvature,
    analyze_vertical_parabolic,
    merge_horizontal_curves,
    get_appropriate_utm_zone,
    download_dems,
    fetch_socrata_state,   
    load_local_hpms,       
    FEET_PER_METER,
    calculate_headings,
    get_tangent_grade,
)
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s: %(message)s")

# ===========================================================================
# VERTICES HELPER  (mirrors rat_plan_profile_cli.build_vertices_df)
# ===========================================================================
def build_vertices_df(
    res: dict,
    route_id: str,
    f_sys: int,
    chunk_s_mp: float,
    chunk_e_mp: float,
    global_start_dist_m: float,
    params: dict,
) -> pd.DataFrame:
    """
    Build a per-point vertex table for one stitched chunk.

    Columns mirror the plan & profile vertices CSV so the two outputs
    can be used interchangeably for QC and downstream rendering:
        RouteId, FSystem, Milepost, Dist_Mi,
        Lon, Lat, Elev_Ft,
        Raw_Lon, Raw_Lat, Elev_Raw_Ft,
        Grade_Pct, Heading_Deg_Unwrapped
    """
    coords_wgs      = res["coords_wgs_smooth"]
    z               = res["z_smooth"]
    spacing         = res["spacing_m"]
    coords_wgs_raw  = res["coords_wgs_raw"]
    z_raw           = res["z_raw"]
    coords_m_smooth = res["coords_m_smooth"]

    headings_unwrapped = calculate_headings(coords_m_smooth)
    grads = np.array(
        [get_tangent_grade(z, i, spacing, params["REGRESSION_WINDOW_M"]) for i in range(len(z))]
    )
    total_len = max(float(res["d_axis"][-1]), 1.0)
    rows = []
    for i, (xy, zz) in enumerate(zip(coords_wgs, z)):
        local_dist_m  = i * spacing
        frac          = local_dist_m / total_len
        mp            = chunk_s_mp + frac * (chunk_e_mp - chunk_s_mp)
        continuous_ft = (global_start_dist_m + local_dist_m) * FEET_PER_METER
        rows.append({
            "RouteId":                  route_id,
            "FSystem":                  f_sys,
            "Milepost":                  round(mp, 4),
            "Dist_Mi":                  round((global_start_dist_m + local_dist_m) / 1609.344, 4),
            "Lon":                      xy[0],
            "Lat":                      xy[1],
            "Elev_Ft":                  round(float(zz) * FEET_PER_METER, 3),
            "Raw_Lon":                  coords_wgs_raw[i][0],
            "Raw_Lat":                  coords_wgs_raw[i][1],
            "Elev_Raw_Ft":              round(float(z_raw[i]) * FEET_PER_METER, 3),
            "Grade_Pct":                round(float(grads[i]), 3),
            "Heading_Deg_Unwrapped":    round(float(headings_unwrapped[i]), 5),
        })
    return pd.DataFrame(rows)


# ===========================================================================
# SECTION-SCORE HELPER
# ===========================================================================
def assign_section_scores(
    df_input: pd.DataFrame,
    df_h: pd.DataFrame,
    df_v: pd.DataFrame,
) -> pd.DataFrame:
    """
    Return df_input with two new columns:
        H_Curve_Bin   – HPMS horizontal curve score (A–F); 'A' when no curve detected
        V_Grade_Bin   – HPMS vertical grade score   (A–F); 'A' when no curve detected

    Logic:
      • Every input section starts with score 'A'.
      • For each detected curve, any input section whose [Start_MP, End_MP]
        interval overlaps the curve's [Calibrated_Start_MP, Calibrated_End_MP]
        receives the curve's bin — keeping the worst (highest letter) bin when
        multiple curves overlap a section.
      • Sections with no overlap keep 'A', satisfying the HPMS requirement that
        every section carry a score.
    """
    BIN_RANK = {b: i for i, b in enumerate("ABCDEF")}

    df_out = df_input.copy()
    df_out["H_Curve_Bin"] = "A"
    df_out["V_Grade_Bin"] = "A"

    # --- Horizontal ---
    if not df_h.empty and {"RouteId", "Calibrated_Start_MP", "Calibrated_End_MP", "Bin"}.issubset(df_h.columns):
        for _, curve in df_h.iterrows():
            mask = (
                (df_out["RouteId"]  == curve["RouteId"])
                & (df_out["End_MP"]   > curve["Calibrated_Start_MP"])
                & (df_out["Start_MP"] < curve["Calibrated_End_MP"])
            )
            if mask.any():
                cur_rank  = df_out.loc[mask, "H_Curve_Bin"].map(BIN_RANK)
                new_rank  = BIN_RANK.get(curve["Bin"], 0)
                worse     = cur_rank < new_rank
                df_out.loc[mask & worse.reindex(df_out.index, fill_value=False), "H_Curve_Bin"] = curve["Bin"]

    # --- Vertical ---
    if not df_v.empty and {"RouteId", "Calibrated_Start_MP", "Calibrated_End_MP", "Grade_Bin"}.issubset(df_v.columns):
        for _, curve in df_v.iterrows():
            mask = (
                (df_out["RouteId"]  == curve["RouteId"])
                & (df_out["End_MP"]   > curve["Calibrated_Start_MP"])
                & (df_out["Start_MP"] < curve["Calibrated_End_MP"])
            )
            if mask.any():
                cur_rank  = df_out.loc[mask, "V_Grade_Bin"].map(BIN_RANK)
                new_rank  = BIN_RANK.get(curve["Grade_Bin"], 0)
                worse     = cur_rank < new_rank
                df_out.loc[mask & worse.reindex(df_out.index, fill_value=False), "V_Grade_Bin"] = curve["Grade_Bin"]

    return df_out



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
def process_route(route_id: str, subset: pd.DataFrame, dem_dir: str, params: dict):
    subset = subset.sort_values("Start_MP")
    download_dems(subset["WKT"].tolist(), dem_dir)
    
    f_sys = int(subset["FSystem"].mode()[0])
    
    all_chunks = []
    lines = stitch_linestrings_ordered(subset["WKT"].tolist())
    for g in lines:
        all_chunks.append({"geom": g, "f_sys": f_sys})

    if not all_chunks:
        return [], [], [], []

    h_all, v_all, health_stats, vtx_dfs = [], [], [], []
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
    global_continuous_dist_m = route_s * 1609.344
    
    for part_idx, info in enumerate(all_chunks):
        chunk_length_m = info["length_m"]
        chunk_start_mp = route_s + ((cumulative_stitch_dist_m / total_stitch_len_m) * (route_e - route_s))
        cumulative_stitch_dist_m += chunk_length_m
        chunk_end_mp = route_s + ((cumulative_stitch_dist_m / total_stitch_len_m) * (route_e - route_s))
        
        res = smooth_plan_profile_from_linestring(info["geom"], dem_dir, params, info["f_sys"])
        if res is None:
            continue
            
        try:
            if "z_raw" in res and "z_smooth" in res:
                v_dev = np.abs(res["z_raw"] - res["z_smooth"])
                rmse_v_ft = np.sqrt(np.mean(v_dev**2)) * FEET_PER_METER
                
                lon_raw = np.array([pt[0] for pt in res["coords_wgs_raw"]])
                lat_raw = np.array([pt[1] for pt in res["coords_wgs_raw"]])
                lon_sm = np.array([pt[0] for pt in res["coords_wgs_smooth"]])
                lat_sm = np.array([pt[1] for pt in res["coords_wgs_smooth"]])
                
                lat_dev_ft = (lat_raw - lat_sm) * 364000
                lon_dev_ft = (lon_raw - lon_sm) * (364000 * np.cos(np.radians(lat_sm)))
                rmse_h_ft = np.sqrt(np.mean(lat_dev_ft**2 + lon_dev_ft**2))

                health_stats.append({
                    "RouteId": route_id,
                    "FSystem": info["f_sys"],
                    "RMSE_V_ft": rmse_v_ft,
                    "RMSE_H_ft": rmse_h_ft
                })
        except Exception:
            pass
            
        spacing_m = res["spacing_m"]
        coords_m_smooth = res["coords_m_smooth"]
        coords_wgs_smooth = res["coords_wgs_smooth"]
        z_smooth = res["z_smooth"]
        total_len = max(res["d_axis"][-1], 1.0)
        
        h_curves = analyze_horizontal_curvature(coords_m_smooth, spacing_m, params)
        if params.get("ENABLE_MERGE", False):
            h_curves = merge_horizontal_curves(h_curves, params)
        v_curves = analyze_vertical_parabolic(z_smooth, spacing_m, params)
        
        for c in h_curves:
            p0 = c["Start_Dist"] / total_len
            p1 = c["End_Dist"] / total_len
            c["RouteId"] = route_id
            c["Part"] = part_idx + 1
            c["Calibrated_Start_MP"] = chunk_start_mp + p0 * (chunk_end_mp - chunk_start_mp)
            c["Calibrated_End_MP"] = chunk_start_mp + p1 * (chunk_end_mp - chunk_start_mp)
            c["FSystem"] = info["f_sys"]
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
            c["Calibrated_Start_MP"] = chunk_start_mp + p0 * (chunk_end_mp - chunk_start_mp)
            c["Calibrated_End_MP"] = chunk_start_mp + p1 * (chunk_end_mp - chunk_start_mp)
            c["FSystem"] = info["f_sys"]
            try:
                i0 = max(0, int(c["Start_Dist"] / spacing_m))
                i1 = min(len(coords_wgs_smooth) - 1, int(c["End_Dist"] / spacing_m))
                if i1 > i0:
                    c["geometry"] = LineString(coords_wgs_smooth[i0:i1+1])
            except Exception:
                pass
                
        h_all.extend(h_curves)
        v_all.extend(v_curves)

        # ── Vertices ────────────────────────────────────────────────────────
        try:
            vtx_df = build_vertices_df(
                res, route_id, info["f_sys"],
                chunk_start_mp, chunk_end_mp,
                global_continuous_dist_m, params,
            )
            # Tag each vertex with the curve type and bin at that point
            vtx_df["H_Curve_Type"] = "Tangent"
            vtx_df["V_Curve_Type"] = "Tangent"
            vtx_df["H_Curve_Bin"]  = "A"
            vtx_df["V_Grade_Bin"]  = "A"
            for c in h_curves:
                s_mi = c["Start_Dist"] / 1609.344
                e_mi = c["End_Dist"]   / 1609.344
                mask = (vtx_df["Dist_Mi"] >= s_mi) & (vtx_df["Dist_Mi"] <= e_mi)
                vtx_df.loc[mask, "H_Curve_Type"] = c["Dir"]
                vtx_df.loc[mask, "H_Curve_Bin"]  = c["Bin"]
            for c in v_curves:
                s_mi = c["Start_Dist"] / 1609.344
                e_mi = c["End_Dist"]   / 1609.344
                mask = (vtx_df["Dist_Mi"] >= s_mi) & (vtx_df["Dist_Mi"] <= e_mi)
                vtx_df.loc[mask, "V_Curve_Type"] = c["Type"]
                vtx_df.loc[mask, "V_Grade_Bin"]  = c["Grade_Bin"]
            vtx_dfs.append(vtx_df)
        except Exception as exc:
            logging.warning(f"Vertices build failed for route {route_id} part {part_idx+1}: {exc}")
        # ────────────────────────────────────────────────────────────────────

        global_continuous_dist_m += total_len
        
    return h_all, v_all, health_stats, vtx_dfs

# ---------------------------------------------------------------------------
# Output Generators
# ---------------------------------------------------------------------------
def generate_html_map(df_h, df_v, out_html, params): 
    import numpy as np
    m = folium.Map(location=[39.5, -98.35], zoom_start=6, tiles="CartoDBPositron")
    bounds_list = []
    color_map = {'A': 'green', 'B': '#a6d96a', 'C': '#fdae61', 'D': '#d7191c', 'E': '#9e0142', 'F': 'purple'}
    
    if not df_h.empty and "geometry" in df_h.columns:
        gdf_h = gpd.GeoDataFrame(df_h, geometry="geometry", crs="EPSG:4326")
        if params.get("SIMPLIFY_GEOMETRY", True):
            gdf_h["geometry"] = gdf_h["geometry"].simplify(tolerance=0.00005, preserve_topology=True)
        bounds_list.append(gdf_h.total_bounds)
        folium.GeoJson(
            gdf_h, name="Horizontal Curves",
            style_function=lambda f: {
                "color": color_map.get(f['properties'].get('Bin', 'A'), 'gray'),
                "weight": 4, "dashArray": '10, 10' if f['properties'].get('Merge_Status') == 'Compound' else ''
            },
            tooltip=folium.GeoJsonTooltip(fields=[c for c in ["RouteId", "Calibrated_Start_MP", "Calibrated_End_MP", "Bin", "Radius_m", "Delta", "Merge_Status"] if c in gdf_h.columns])
        ).add_to(m)

    if not df_v.empty and "geometry" in df_v.columns:
        gdf_v = gpd.GeoDataFrame(df_v, geometry="geometry", crs="EPSG:4326")
        if params.get("SIMPLIFY_GEOMETRY", True):
            gdf_v["geometry"] = gdf_v["geometry"].simplify(tolerance=0.00005, preserve_topology=True)
        bounds_list.append(gdf_v.total_bounds)
        folium.GeoJson(
            gdf_v, name="Vertical Curves",
            style_function=lambda f: {"color": color_map.get(f['properties'].get('Grade_Bin', 'A'), 'gray'), "weight": 4},
            tooltip=folium.GeoJsonTooltip(fields=[c for c in ["RouteId", "Calibrated_Start_MP", "Calibrated_End_MP", "Type", "Grade_Bin", "K_Value", "Alg_Diff"] if c in gdf_v.columns])
        ).add_to(m)

    if bounds_list:
        b = np.vstack(bounds_list)
        minx, miny, maxx, maxy = np.min(b[:,0]), np.min(b[:,1]), np.max(b[:,2]), np.max(b[:,3])
        m.fit_bounds([[miny, minx], [maxy, maxx]])
    
    legend_html = '''<div style="position: fixed; bottom: 50px; right: 50px; width: 160px; border:2px solid grey; z-index:9999; background:white; padding: 10px; font-family: sans-serif; font-size: 12px;">
    <b>Curve / Grade Bin</b><br><i style="background:green; width:10px; height:10px; display:inline-block;"></i> A<br><i style="background:#a6d96a; width:10px; height:10px; display:inline-block;"></i> B<br><i style="background:#fdae61; width:10px; height:10px; display:inline-block;"></i> C<br><i style="background:#d7191c; width:10px; height:10px; display:inline-block;"></i> D<br><i style="background:#9e0142; width:10px; height:10px; display:inline-block;"></i> E<br><i style="background:purple; width:10px; height:10px; display:inline-block;"></i> F<br>
    <hr style="margin: 5px 0; border: 0; border-top: 1px solid #ccc;"><b>Curve Type</b><br><i style="border-top: 3px solid black; width:15px; display:inline-block; margin-bottom: 3px;"></i> Simple / Vertical<br><i style="border-top: 3px dashed black; width:15px; display:inline-block; margin-bottom: 3px;"></i> Compound (H)<br></div>'''
    m.get_root().html.add_child(folium.Element(legend_html))
    folium.LayerControl().add_to(m)
    m.save(out_html)
    logging.info(f"Saved Map to: {out_html}")

def generate_dashboard(df_h, df_v, df_health, out_html, out_dir, state_fips=""):
    import matplotlib.ticker as mticker

    def _save(fig, path):
        try:
            fig.tight_layout()
            fig.savefig(path, dpi=120)
            plt.close(fig)
            return path
        except Exception:
            plt.close(fig)
            return None

    BIN_ORDER  = list("ABCDEF")
    BIN_COLORS = ["#1a9641", "#a6d96a", "#fdae61", "#d7191c", "#9e0142", "#4d0055"]
    FS_LABEL   = "Functional System (1 = Interstate, 7 = Local)"

    # --- Chart 1: Horizontal curve bins ---
    chart_h = os.path.join(out_dir, "chart_horizontal_bins.png")
    if not df_h.empty and "Bin" in df_h.columns:
        vc = df_h["Bin"].value_counts().reindex(BIN_ORDER, fill_value=0)
        fig, ax = plt.subplots(figsize=(6, 4))
        ax.bar(BIN_ORDER, vc.values, color=BIN_COLORS)
        ax.set_title("Horizontal Curve Count by Severity Class")
        ax.set_xlabel("Severity Class"); ax.set_ylabel("Count")
        _save(fig, chart_h)

    # --- Chart 2: Vertical grade bins ---
    chart_v = os.path.join(out_dir, "chart_vertical_bins.png")
    if not df_v.empty and "Grade_Bin" in df_v.columns:
        vc = df_v["Grade_Bin"].value_counts().reindex(BIN_ORDER, fill_value=0)
        fig, ax = plt.subplots(figsize=(6, 4))
        ax.bar(BIN_ORDER, vc.values, color=BIN_COLORS)
        ax.set_title("Vertical Curve Count by Grade Class")
        ax.set_xlabel("Grade Class"); ax.set_ylabel("Count")
        _save(fig, chart_v)

    # --- Chart 3: Horizontal curves by functional system ---
    chart_fs_h = os.path.join(out_dir, "chart_fsystem_horizontal.png")
    if not df_h.empty and "Bin" in df_h.columns and "FSystem" in df_h.columns:
        ct = pd.crosstab(df_h["FSystem"], df_h["Bin"]).reindex(columns=BIN_ORDER, fill_value=0)
        fig, ax = plt.subplots(figsize=(8, 5))
        ct.plot(kind="bar", stacked=True, color=BIN_COLORS, ax=ax)
        ax.set_title("Horizontal Curves by Functional System")
        ax.set_ylabel("Count"); ax.set_xlabel(FS_LABEL)
        ax.legend(title="Severity Class", bbox_to_anchor=(1.05, 1), loc="upper left")
        ax.tick_params(axis="x", rotation=0)
        _save(fig, chart_fs_h)

    # --- Chart 4: Vertical curves by functional system ---
    chart_fs_v = os.path.join(out_dir, "chart_fsystem_vertical.png")
    if not df_v.empty and "Grade_Bin" in df_v.columns and "FSystem" in df_v.columns:
        ct = pd.crosstab(df_v["FSystem"], df_v["Grade_Bin"]).reindex(columns=BIN_ORDER, fill_value=0)
        fig, ax = plt.subplots(figsize=(8, 5))
        ct.plot(kind="bar", stacked=True, color=BIN_COLORS, ax=ax)
        ax.set_title("Vertical Curves by Functional System")
        ax.set_ylabel("Count"); ax.set_xlabel(FS_LABEL)
        ax.legend(title="Grade Class", bbox_to_anchor=(1.05, 1), loc="upper left")
        ax.tick_params(axis="x", rotation=0)
        _save(fig, chart_fs_v)

    # --- Chart 5: System Health RMSE with P50/P80/P95 bands ---
    chart_health = os.path.join(out_dir, "chart_health_rmse.png")
    if not df_health.empty and "FSystem" in df_health.columns:
        grp_h  = df_health.groupby("FSystem")["RMSE_H_ft"]
        grp_v  = df_health.groupby("FSystem")["RMSE_V_ft"]
        fs_vals = sorted(df_health["FSystem"].unique())
        x, width = np.arange(len(fs_vals)), 0.35

        def _pct(grp, f, q):
            return grp.get_group(f).quantile(q) if f in grp.groups else 0

        h_p50 = [_pct(grp_h, f, 0.50) for f in fs_vals]
        h_p80 = [_pct(grp_h, f, 0.80) for f in fs_vals]
        h_p95 = [_pct(grp_h, f, 0.95) for f in fs_vals]
        v_p50 = [_pct(grp_v, f, 0.50) for f in fs_vals]
        v_p80 = [_pct(grp_v, f, 0.80) for f in fs_vals]
        v_p95 = [_pct(grp_v, f, 0.95) for f in fs_vals]

        fig, ax = plt.subplots(figsize=(10, 5))
        ax.bar(x - width/2, h_p50, width, label="H Drift (P50)", color="coral",          alpha=0.9)
        ax.bar(x + width/2, v_p50, width, label="V Drift (P50)", color="mediumseagreen", alpha=0.9)
        ax.errorbar(x - width/2, h_p80,
                    yerr=[np.zeros(len(fs_vals)), np.array(h_p95) - np.array(h_p80)],
                    fmt="none", color="darkred",   capsize=4, label="H P80–P95")
        ax.errorbar(x + width/2, v_p80,
                    yerr=[np.zeros(len(fs_vals)), np.array(v_p95) - np.array(v_p80)],
                    fmt="none", color="darkgreen", capsize=4, label="V P80–P95")
        ax.set_title("System Health: Spline Deviation by Functional System\n"
                     "(Bars = median; error bars extend from 80th to 95th percentile)")
        ax.set_ylabel("RMSE Deviation (ft)"); ax.set_xlabel(FS_LABEL)
        ax.set_xticks(x); ax.set_xticklabels(fs_vals); ax.legend()
        _save(fig, chart_health)

    # --- Chart 6: Horizontal scatter — Length vs. Radius ---
    chart_scatter_h = os.path.join(out_dir, "chart_scatter_horizontal.png")
    if not df_h.empty and "Radius_m" in df_h.columns and "Length_m" in df_h.columns:
        fig, ax = plt.subplots(figsize=(8, 5))
        ax.scatter(df_h["Radius_m"] * FEET_PER_METER, df_h["Length_m"] * FEET_PER_METER,
                   alpha=0.4, c="darkorange", edgecolor="black", s=18)
        ax.set_title("Horizontal Curve Diagnostics: Length vs. Radius")
        ax.set_xlabel("Radius (ft)"); ax.set_ylabel("Curve Length (ft)")
        ax.set_xlim(0, 15000); ax.grid(True, linestyle="--", alpha=0.5)
        _save(fig, chart_scatter_h)

    # --- Chart 7: Vertical K-Value distribution ---
    chart_hist_v = os.path.join(out_dir, "chart_hist_vertical.png")
    if not df_v.empty and "K_Value" in df_v.columns:
        fig, ax = plt.subplots(figsize=(8, 5))
        df_v["K_Value"].clip(upper=500).plot(kind="hist", bins=40, color="teal",
                                              edgecolor="black", ax=ax)
        ax.set_title("Vertical Curve Diagnostics: K-Value Distribution (capped at 500)")
        ax.set_xlabel("K-Value"); ax.set_ylabel("Frequency")
        ax.grid(True, linestyle="--", alpha=0.5)
        _save(fig, chart_hist_v)

    # --- Chart 8: Curve density per 100 route-miles ---
    chart_density = os.path.join(out_dir, "chart_curve_density.png")
    try:
        if (not df_h.empty and "FSystem" in df_h.columns and "Length_m" in df_h.columns
                and not df_v.empty and "FSystem" in df_v.columns and "Length_m" in df_v.columns):
            all_fs         = pd.concat([df_h[["FSystem","Length_m"]], df_v[["FSystem","Length_m"]]])
            route_miles    = all_fs.groupby("FSystem")["Length_m"].sum() / 1609.34
            h_count        = df_h.groupby("FSystem").size()
            v_count        = df_v.groupby("FSystem").size()
            density_h      = (h_count / route_miles * 100).fillna(0)
            density_v      = (v_count / route_miles * 100).fillna(0)
            fs_vals        = sorted(set(density_h.index) | set(density_v.index))
            x = np.arange(len(fs_vals))
            fig, ax = plt.subplots(figsize=(8, 5))
            ax.bar(x - 0.2, [density_h.get(f, 0) for f in fs_vals], 0.4,
                   label="Horizontal", color="darkorange", alpha=0.85)
            ax.bar(x + 0.2, [density_v.get(f, 0) for f in fs_vals], 0.4,
                   label="Vertical",   color="steelblue",  alpha=0.85)
            ax.set_title("Curve Density by Functional System\n(Curves per 100 Route-Miles)")
            ax.set_ylabel("Curves per 100 Route-Miles"); ax.set_xlabel(FS_LABEL)
            ax.set_xticks(x); ax.set_xticklabels(fs_vals); ax.legend()
            _save(fig, chart_density)
    except Exception:
        chart_density = None

    # --- Chart 9: Cumulative severity CDF ---
    chart_cdf = os.path.join(out_dir, "chart_severity_cdf.png")
    try:
        fig, axes = plt.subplots(1, 2, figsize=(12, 5))
        bin_to_num = {b: i + 1 for i, b in enumerate("ABCDEF")}
        if not df_h.empty and "Bin" in df_h.columns:
            h_num  = df_h["Bin"].map(bin_to_num).dropna()
            counts = [((h_num <= v).sum() / len(h_num)) * 100 for v in range(1, 7)]
            axes[0].step(list("ABCDEF"), counts, where="post", color="darkorange", linewidth=2)
            axes[0].set_ylim(0, 105)
            axes[0].yaxis.set_major_formatter(mticker.PercentFormatter())
            axes[0].set_title("Cumulative Horizontal Severity Distribution")
            axes[0].set_xlabel("Severity Class (A = Mildest)")
            axes[0].set_ylabel("Cumulative % of Curves")
            axes[0].grid(True, linestyle="--", alpha=0.5)
        if not df_v.empty and "Grade_Bin" in df_v.columns:
            v_num  = df_v["Grade_Bin"].map(bin_to_num).dropna()
            counts = [((v_num <= vv).sum() / len(v_num)) * 100 for vv in range(1, 7)]
            axes[1].step(list("ABCDEF"), counts, where="post", color="steelblue", linewidth=2)
            axes[1].set_ylim(0, 105)
            axes[1].yaxis.set_major_formatter(mticker.PercentFormatter())
            axes[1].set_title("Cumulative Vertical Severity Distribution")
            axes[1].set_xlabel("Grade Class (A = Mildest)")
            axes[1].set_ylabel("Cumulative % of Curves")
            axes[1].grid(True, linestyle="--", alpha=0.5)
        _save(fig, chart_cdf)
    except Exception:
        chart_cdf = None

    # --- Chart 10: CREST vs. SAG by functional system ---
    chart_crest_sag = os.path.join(out_dir, "chart_crest_sag.png")
    try:
        if not df_v.empty and "Type" in df_v.columns and "FSystem" in df_v.columns:
            ct = pd.crosstab(df_v["FSystem"], df_v["Type"])
            for col in ["CREST", "SAG"]:
                if col not in ct.columns:
                    ct[col] = 0
            ct = ct[["CREST", "SAG"]]
            fig, ax = plt.subplots(figsize=(8, 5))
            ct.plot(kind="bar", color=["orange", "mediumpurple"], ax=ax)
            ax.set_title("Vertical Curve Type by Functional System (CREST vs. SAG)")
            ax.set_ylabel("Count"); ax.set_xlabel(FS_LABEL)
            ax.tick_params(axis="x", rotation=0); ax.legend(title="Type")
            _save(fig, chart_crest_sag)
    except Exception:
        chart_crest_sag = None

    # --- Chart 11: Compound curve percentage by functional system ---
    chart_compound = os.path.join(out_dir, "chart_compound_pct.png")
    try:
        if not df_h.empty and "Merge_Status" in df_h.columns and "FSystem" in df_h.columns:
            grp = df_h.groupby("FSystem")
            pct = (grp["Merge_Status"].apply(lambda s: (s == "Compound").sum()) /
                   grp.size() * 100).fillna(0)
            fs_v = sorted(pct.index)
            fig, ax = plt.subplots(figsize=(8, 5))
            ax.bar([str(f) for f in fs_v], [pct.get(f, 0) for f in fs_v],
                   color="slategray", alpha=0.85)
            ax.yaxis.set_major_formatter(mticker.PercentFormatter())
            ax.set_title("Compound Horizontal Curve Percentage by Functional System")
            ax.set_ylabel("% of Curves Classified as Compound")
            ax.set_xlabel(FS_LABEL)
            ax.grid(True, linestyle="--", alpha=0.4, axis="y")
            _save(fig, chart_compound)
    except Exception:
        chart_compound = None

    # --- HTML assembly ---
    def _img(fname, max_w="500px"):
        path = os.path.join(out_dir, fname)
        return (f"<img src='{fname}' style='max-width:{max_w}; border:1px solid #ccc;'>"
                if os.path.exists(path) else "")

    html = f"""
<html><head><title>RAT Summary Dashboard</title></head>
<body style="font-family:Arial; margin:20px; background-color:#f8f9fa;">
<div style="background-color:white; padding:20px; border-radius:8px; box-shadow:0 4px 6px rgba(0,0,0,0.1);">
  <h2>Statewide Alignment Summary — State FIPS {state_fips}</h2>
  <p><strong>Horizontal curves detected:</strong> {len(df_h):,}</p>
  <p><strong>Vertical curves detected:</strong> {len(df_v):,}</p>
</div>

<h3 style="margin-top:40px;">System Health</h3>
<p style="color:#6c757d; max-width:800px;">Average spline deviation (RMSE) by functional system.
Bars show the median deviation; error bars extend from the 80th to the 95th percentile.
Refer to national_smoothing_factors.json for per-functional-system acceptance limits.</p>
{_img("chart_health_rmse.png", "700px")}

<h3 style="margin-top:40px;">Curve Density</h3>
<p style="color:#6c757d; max-width:800px;">Curves per 100 route-miles by functional system,
normalizing for differences in network size across functional classes.</p>
{_img("chart_curve_density.png", "600px")}

<h3 style="margin-top:40px;">Cumulative Severity Distribution</h3>
<p style="color:#6c757d; max-width:800px;">Percentage of curves at or below each severity class.</p>
{_img("chart_severity_cdf.png", "900px")}

<h3 style="margin-top:40px;">Severity by Functional System</h3>
<div style="display:flex; flex-wrap:wrap; gap:20px;">
  {_img("chart_fsystem_horizontal.png")} {_img("chart_fsystem_vertical.png")}
</div>

<h3 style="margin-top:40px;">Vertical Curve Type Distribution</h3>
<p style="color:#6c757d; max-width:800px;">CREST and SAG curve counts by functional system.</p>
{_img("chart_crest_sag.png", "600px")}

<h3 style="margin-top:40px;">Compound Horizontal Curve Percentage</h3>
<p style="color:#6c757d; max-width:800px;">Percentage of horizontal curves classified as compound by functional system.</p>
{_img("chart_compound_pct.png", "600px")}

<h3 style="margin-top:40px;">Statewide Totals</h3>
<div style="display:flex; flex-wrap:wrap; gap:20px;">
  {_img("chart_horizontal_bins.png")} {_img("chart_vertical_bins.png")}
</div>

<h3 style="margin-top:40px;">Advanced Diagnostics</h3>
<div style="display:flex; flex-wrap:wrap; gap:20px;">
  {_img("chart_scatter_horizontal.png")} {_img("chart_hist_vertical.png")}
</div>

</body></html>"""

    with open(out_html, "w", encoding="utf-8") as f:
        f.write(html)
    logging.info(f"Saved Dashboard to: {out_html}")

def export_geo(df, out_base, params):
    if df.empty or "geometry" not in df.columns: return
    gdf = gpd.GeoDataFrame(df, geometry="geometry", crs="EPSG:4326")
    if params.get("OUT_GEOJSON"): 
        gdf.to_file(out_base + ".geojson", driver="GeoJSON")
        logging.info(f"Saved GeoJSON to: {out_base}.geojson")
    if params.get("OUT_GPKG"): 
        gdf.to_file(out_base + ".gpkg", driver="GPKG")
        logging.info(f"Saved GPKG to: {out_base}.gpkg")
    if params.get("OUT_SHP"): 
        gdf.to_file(out_base + ".shp")
        logging.info(f"Saved SHP to: {out_base}.shp")

# ---------------------------------------------------------------------------
# Core Execution Loop
# ---------------------------------------------------------------------------
def run_state_alignment(state_fips: str, out_dir: str, dem_dir: str, user_params: dict, local_df: pd.DataFrame = None):
    state_out_dir = os.path.join(out_dir, f"Output_State_{state_fips}")
    os.makedirs(state_out_dir, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d")

    # Find the next available version number so re-runs on the same date
    # never silently skip processing.  v1 is used on the first run of the
    # day; subsequent runs increment to v2, v3, etc.
    version = 1
    while os.path.exists(
        os.path.join(state_out_dir, f"alignment_horizontal_{state_fips}_{stamp}_v{version}.csv")
    ):
        version += 1

    h_csv     = os.path.join(state_out_dir, f"alignment_horizontal_{state_fips}_{stamp}_v{version}.csv")
    v_csv     = os.path.join(state_out_dir, f"alignment_vertical_{state_fips}_{stamp}_v{version}.csv")
    vtx_csv   = os.path.join(state_out_dir, f"alignment_vertices_{state_fips}_{stamp}_v{version}.csv")
    score_csv = os.path.join(state_out_dir, f"alignment_section_scores_{state_fips}_{stamp}_v{version}.csv")
    logging.info(f"Output version: v{version}")
        
    logging.info(f"\n{'='*60}\n=== ALIGNMENT PROCESSING: STATE {state_fips} ===\n{'='*60}")
    params = build_params(user_params)
    try:
        df = local_df.copy() if local_df is not None else fetch_socrata_state(state_fips, user_params.get("SOCRATA_TOKEN", ""))
    except Exception as e:
        logging.error(f"Failed to fetch data: {e}"); return

    routes = df["RouteId"].dropna().unique().tolist()
    logging.info(f"Loaded {len(df):,} segments across {len(routes):,} routes.")
    
    all_h, all_v, all_health, all_vtx = [], [], [], []
    max_workers = user_params.get("MAX_WORKERS", max(1, os.cpu_count() - 2))
    
    with ProcessPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(process_route, rid, subset, dem_dir, params): rid for rid, subset in df.groupby("RouteId")}
        completed = 0
        for fut in as_completed(futures):
            completed += 1
            if completed % 100 == 0: logging.info(f"  ...Processed {completed}/{len(routes)} routes")
            try:
                h, v, health, vtx_dfs = fut.result()
                all_h.extend(h); all_v.extend(v); all_health.extend(health)
                all_vtx.extend(vtx_dfs)
            except Exception as e: logging.error(f"Route {futures[fut]} failed: {e}")

    df_h      = pd.DataFrame(all_h)
    df_v      = pd.DataFrame(all_v)
    df_health = pd.DataFrame(all_health)
    df_vtx    = pd.concat(all_vtx, ignore_index=True) if all_vtx else pd.DataFrame()

    # ------------------------------------------------------------------
    # Vertices CSV  (one row per smoothed point, all routes/FS combined)
    # ------------------------------------------------------------------
    if params.get("OUT_CSV", True) and not df_vtx.empty:
        df_vtx.to_csv(vtx_csv, index=False)
        logging.info(f"Saved Vertices CSV to: {vtx_csv}")

    # ------------------------------------------------------------------
    # Section Scores CSV
    # Assigns H_Curve_Bin / V_Grade_Bin to every input segment.
    # Sections with no detected curve receive 'A' per HPMS reporting rules.
    # ------------------------------------------------------------------
    if params.get("OUT_CSV", True):
        score_cols = ["RouteId", "FSystem", "Start_MP", "End_MP"]
        available  = [c for c in score_cols if c in df.columns]
        df_scores  = assign_section_scores(df[available].copy(), df_h, df_v)
        df_scores.to_csv(score_csv, index=False)
        logging.info(f"Saved Section Scores CSV to: {score_csv}")

    # Save curve CSVs based on GUI checkbox
    if params.get("OUT_CSV", True):
        if not df_h.empty: 
            df_h.to_csv(h_csv, index=False)
            logging.info(f"Saved Horizontal CSV to: {h_csv}")
        if not df_v.empty: 
            df_v.to_csv(v_csv, index=False)
            logging.info(f"Saved Vertical CSV to: {v_csv}")
    
    export_geo(df_h, os.path.join(state_out_dir, f"alignment_horizontal_{state_fips}_{stamp}_v{version}"), params)
    export_geo(df_v, os.path.join(state_out_dir, f"alignment_vertical_{state_fips}_{stamp}_v{version}"), params)
    
    if params.get("OUT_HTML_MAP", True) and not df_h.empty:
        generate_html_map(df_h, df_v, os.path.join(state_out_dir, f"alignment_map_{state_fips}_{stamp}_v{version}.html"), params)
    if params.get("OUT_DASHBOARD", True) and not df_h.empty:
        generate_dashboard(df_h, df_v, df_health, os.path.join(state_out_dir, f"alignment_dashboard_{state_fips}_{stamp}_v{version}.html"), state_out_dir, state_fips=state_fips)
    logging.info(f"Finished State {state_fips}!")

def main():
    parser = argparse.ArgumentParser(description="RAT Bulk Alignment Engine CLI")
    parser.add_argument("--input", default=None, help="Local HPMS file")
    parser.add_argument("--outdir", required=True, help="Output Directory")
    parser.add_argument("--demdir", required=True, help="DEM Directory")
    parser.add_argument("--state", default=None, help="State FIPS")
    parser.add_argument("--params_json", default=None, help="JSON overrides")
    args = parser.parse_args()

    user_params = {}
    if args.params_json and os.path.exists(args.params_json):
        with open(args.params_json, 'r') as f: user_params = json.load(f)
    
    user_params.update({"OUTPUT_DIR": args.outdir, "DEM_DIR": args.demdir})
    if args.state: user_params["STATE_FIPS"] = args.state

    if args.input:
        logging.info(f"Processing local file: {args.input}")
        df = load_local_hpms(args.input)
        
        # Check GUI for state FIPS, otherwise fallback to "LOCAL"
        state_id = str(user_params.get("STATE_FIPS", "")).strip()
        if state_id == "00" or not state_id:
            state_id = "LOCAL"
            
        run_state_alignment(state_id, args.outdir, args.demdir, user_params, local_df=df)
    elif args.state:
        states = ALL_FIPS if args.state.upper() == "ALL" else [args.state.zfill(2)]
        for s in states: run_state_alignment(s, args.outdir, args.demdir, user_params)
    else:
        logging.error("No input specified!")

if __name__ == "__main__":
    main()