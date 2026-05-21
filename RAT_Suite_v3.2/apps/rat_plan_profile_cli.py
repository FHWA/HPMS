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
RAT PLAN & PROFILE CLI v3.2 (Data Pre-Processor)
--------------------------------------------------------------------------------
ROLE: Route-specific data extractor for engineering sheets.
DESCRIPTION: 
Isolates a single route (or specific milepost bounds), stitches the geometry, 
and applies the core smoothing engine. It calculates exact continuous distances 
across disjointed chunks to prevent overlapping, and exports the raw and smoothed 
vertices to CSVs to be consumed by the downstream PDF rendering script.
CHANGES FROM:
  - Stripped out redundant load_local_hpms (now imported from core).
  - Fixed Facility_Type KeyError risk by centralizing data loading logic.
CREATED BY: Federal Highway Administration, Office of Highway Policy Information.
CREATED ON: 5/14/2026
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
    get_appropriate_utm_zone,
    load_local_hpms      # <--- Imported from core
)
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s: %(message)s")


def build_vertices_df(res: dict, route_id: str, chunk_s_mp: float, chunk_e_mp: float, global_start_dist_m: float, params: dict) -> pd.DataFrame:
    coords_wgs = res["coords_wgs_smooth"]
    coords_m = res["coords_m_smooth"]
    z = res["z_smooth"]
    spacing = res["spacing_m"]
    coords_wgs_raw = res["coords_wgs_raw"]
    z_raw = res["z_raw"]
    headings_unwrapped = calculate_headings(coords_m)
    grads = np.array([get_tangent_grade(z, i, spacing, params["REGRESSION_WINDOW_M"]) for i in range(len(z))])
    total_len = max(float(res["d_axis"][-1]), 1.0)
    rows = []
    for i, (xy, zz) in enumerate(zip(coords_wgs, z)):
        local_dist_m = i * spacing
        frac = local_dist_m / total_len
        mp = chunk_s_mp + frac * (chunk_e_mp - chunk_s_mp)
        
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

def generate_calibration_dashboard(sample_chunks, demdir, base_params, out_html_path, route_id):
    import copy
    import numpy as np
    
    factors = [200, 400, 600, 800, 1000, 1200]
    h_results = []
    v_results = []
    
    for f in factors:
        test_params = copy.deepcopy(base_params)
        for k in test_params:
            if "SMOOTH_FACTOR" in k:
                test_params[k] = f
                
        h_factor_var = []
        v_factor_rmse, v_factor_max, v_factor_var = [], [], []
        
        for chunk in sample_chunks:
            res = smooth_plan_profile_from_linestring(chunk["geom"], demdir, test_params, chunk["f_sys"])
            if not res: 
                continue
                
            try:
                if "headings_unwrapped_smooth_deg" in res and "spacing_m" in res:
                    headings_rad = np.radians(res["headings_unwrapped_smooth_deg"])
                    curvature = np.diff(headings_rad) / res["spacing_m"]
                    h_factor_var.append(np.var(curvature))
            except Exception as e:
                logging.warning(f"CALIBRATION DIAGNOSTIC: Horizontal math error: {e}")
                
            try:
                if "z_raw" in res and "z_smooth" in res:
                    v_dev = np.abs(res["z_raw"] - res["z_smooth"])
                    v_factor_rmse.append(np.sqrt(np.mean(v_dev**2)) * FEET_PER_METER)
                    v_factor_max.append(np.max(v_dev) * 3.28084)
                    grades = np.diff(res["z_smooth"]) / res["spacing_m"]
                    v_factor_var.append(np.var(grades))
            except Exception:
                pass
            
        if h_factor_var:
            h_results.append({"Factor": f"{f} ft", "Variance": f"{np.mean(h_factor_var):.2e}"})
            
        if v_factor_rmse:
            v_results.append({
                "Factor": f"{f} ft", "RMSE": round(np.mean(v_factor_rmse), 3),
                "Max": round(np.mean(v_factor_max), 3), "Variance": f"{np.mean(v_factor_var):.6f}"
            })
            
    if not h_results and not v_results:
        return
        
    html = f"""
    <html><head><title>Calibration Dashboard - Route {route_id}</title>
    <style>
        body {{ font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; margin: 40px; background-color: #f8f9fa; color: #333; }}
        h2 {{ color: #2c3e50; border-bottom: 2px solid #deff9a; padding-bottom: 10px; display: inline-block; }}
        h3 {{ color: #495057; margin-top: 40px; }}
        table {{ border-collapse: collapse; width: 90%; margin-top: 10px; background-color: white; box-shadow: 0 4px 6px rgba(0,0,0,0.1); border-radius: 8px; overflow: hidden; }}
        th, td {{ padding: 12px; text-align: center; border-bottom: 1px solid #e9ecef; }}
        th {{ background-color: #2c3e50; color: #deff9a; font-weight: bold; font-size: 15px; }}
        tr:hover {{ background-color: #f1f3f5; }}
        .note {{ font-style: italic; color: #6c757d; margin-top: 20px; line-height: 1.6; max-width: 90%; }}
    </style>
    </head><body>
    <h2>Sensitivity Analysis for Route {route_id}</h2>
    <p>This dashboard compares how different smoothing factors affect the alignment, averaged across a statistically significant random sample ({len(sample_chunks)} one-mile segments) of this route.</p>
    """
    if h_results:
        html += "<h3>1. Horizontal Calibration</h3><table><tr><th>H_SMOOTH_FACTOR</th><th>Curvature Variance (Smoothness)</th></tr>"
        for r in h_results: html += f"<tr><td><strong>{r['Factor']}</strong></td><td>{r['Variance']}</td></tr>"
        html += "</table>"
    if v_results:
        html += "<h3>2. Vertical Calibration</h3><table><tr><th>V_SMOOTH_FACTOR</th><th>Average Elevation Error (RMSE)</th><th>Max Vertical Deviation</th><th>Grade Variance (Smoothness)</th></tr>"
        for r in v_results: html += f"<tr><td><strong>{r['Factor']}</strong></td><td>{r['RMSE']}'</td><td>{r['Max']}'</td><td>{r['Variance']}</td></tr>"
        html += "</table>"
    html += """<div class="note"><strong>How to read this:</strong> Look for the "Elbow" in the Variance column.</div></body></html>"""
    
    with open(out_html_path, "w", encoding="utf-8") as out_f:
        out_f.write(html)

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
            
    # =========================================================
    # INJECT SMART CALIBRATION FACTORS
    # =========================================================
    state_fips_input = str(user_params.get("STATE_FIPS", "")).strip().zfill(2)
    suite_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    master_json_path = os.path.join(suite_root, "core", "national_smoothing_factors.json")
    
    if state_fips_input and os.path.exists(master_json_path):
        try:
            with open(master_json_path, "r", encoding="utf-8") as mj:
                master_dict = json.load(mj)
            if state_fips_input in master_dict:
                custom_factors = master_dict[state_fips_input]
                for key, custom_val in custom_factors.items():
                    if custom_val is not None:
                        user_params[key] = custom_val 
                logging.info(f"Loaded custom smoothing factors for State FIPS {state_fips_input} from master JSON.")
        except Exception as e:
            logging.error(f"Could not apply custom smoothing factors from master JSON: {e}")
    # =========================================================
            
    params = build_params(user_params)
    logging.info("Loading input...")
    df = load_local_hpms(args.input)
    
    allowed_facs = user_params.get("FACILITY_TYPE_FILTER", [])
    if allowed_facs and "Facility_Type" in df.columns:
        initial_count = len(df)
        df = df[df["Facility_Type"].isin(allowed_facs)].copy()
        logging.info(f"Dropped {initial_count - len(df):,} rows based on Facility Type filters.")
        
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
        
    predominant_f_sys = int(sub["FSystem"].mode()[0])
    
    all_chunks = []
    lines = stitch_linestrings_ordered(sub["WKT"].tolist())
    for g in lines:
        all_chunks.append({"geom": g, "f_sys": predominant_f_sys})

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
        
        res = smooth_plan_profile_from_linestring(info["geom"], args.demdir, params, info["f_sys"])
        if res is None:
            continue
        spacing_m = res["spacing_m"]
        h = analyze_horizontal_curvature(res["coords_m_smooth"], spacing_m, params)
        if params.get("ENABLE_MERGE", False):
            h = merge_horizontal_curves(h, params)
        v = analyze_vertical_parabolic(res["z_smooth"], spacing_m, params)
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
        vtx_df = build_vertices_df(res, route_norm, chunk_s_mp, chunk_e_mp, global_continuous_dist_m, params)
        all_vtx_dfs.append(vtx_df)
        
        global_continuous_dist_m += total_len
        
    if not all_vtx_dfs:
        raise RuntimeError("Smoothing failed for all route chunks (insufficient geometry points).")
        
    final_vtx_df = pd.concat(all_vtx_dfs, ignore_index=True)
    final_h_df = pd.DataFrame(all_h)
    final_v_df = pd.DataFrame(all_v)
    safe_route = safe_route_name(route_norm)
    global_mp_start = final_vtx_df["Milepost"].min()
    global_mp_end = final_vtx_df["Milepost"].max()
    
    base = f"plan_profile_{safe_route}_MP_{global_mp_start:.3f}_to_{global_mp_end:.3f}"

    vertices_csv = os.path.join(args.outdir, f"{base}_vertices.csv")
    h_csv = os.path.join(args.outdir, f"{base}_horizontal.csv")
    v_csv = os.path.join(args.outdir, f"{base}_vertical.csv")
    
    # --- CALIBRATION BLOCK ---
    calibration_html = os.path.join(args.outdir, f"{base}_calibration_dashboard.html")
    try:
        from shapely.ops import substring
        import random
        
        test_segments = []
        for chunk in all_chunks:
            l_m = chunk["length_m"]
            if l_m < 1600:
                continue
                
            num_miles = int(l_m / 1609.34)
            for i in range(num_miles):
                start_frac = (i * 1609.34) / l_m
                end_frac = ((i + 1) * 1609.34) / l_m
                sub_geom = substring(chunk["geom"], start_frac, end_frac, normalized=True)
                test_segments.append({"geom": sub_geom, "f_sys": chunk["f_sys"]})
                
        sample_size = max(3, int(len(test_segments) * 0.20))
        
        if test_segments:
            sampled_chunks = random.sample(test_segments, min(sample_size, len(test_segments)))
            logging.info(f"Running sensitivity sweep on {len(sampled_chunks)} random 1-mile segments...")
            generate_calibration_dashboard(sampled_chunks, args.demdir, params, calibration_html, route_norm)
            logging.info(f"Saved calibration dashboard: {calibration_html}")
        else:
            logging.warning("Route too short to extract 1-mile test segments for calibration.")
            
    except Exception as e:
        logging.warning(f"Failed to generate calibration dashboard: {e}")
    
    final_vtx_df['H_Curve_Type'] = 'Tangent'
    final_vtx_df['V_Curve_Type'] = 'Tangent'
    
    for h in all_h:
        start_ft = h['Start_Dist'] * FEET_PER_METER
        end_ft = h['End_Dist'] * FEET_PER_METER
        mask = (final_vtx_df['Dist_Ft'] >= start_ft) & (final_vtx_df['Dist_Ft'] <= end_ft)
        final_vtx_df.loc[mask, 'H_Curve_Type'] = h['Dir']
        
    for v in all_v:
        start_ft = v['Start_Dist'] * FEET_PER_METER
        end_ft = v['End_Dist'] * FEET_PER_METER
        mask = (final_vtx_df['Dist_Ft'] >= start_ft) & (final_vtx_df['Dist_Ft'] <= end_ft)
        final_vtx_df.loc[mask, 'V_Curve_Type'] = v['Type']

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