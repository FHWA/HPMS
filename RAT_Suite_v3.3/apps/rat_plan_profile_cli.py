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
import plotly.graph_objects as go
from plotly.subplots import make_subplots
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
    merge_close_chunks,
    smooth_plan_profile_from_linestring,
    analyze_horizontal_curvature,
    analyze_vertical_parabolic,
    merge_horizontal_curves,
    calculate_headings,
    get_tangent_grade,
    get_appropriate_utm_zone,
    load_local_hpms,
    apply_facility_fsystem_filters,
    fetch_nbi_nti_state,
)
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s: %(message)s")


def build_vertices_df(res: dict, route_id: str, chunk_s_mp: float, chunk_e_mp: float, global_start_dist_m: float, params: dict) -> pd.DataFrame:
    coords_wgs      = np.array(res["coords_wgs_smooth"])
    coords_m        = res["coords_m_smooth"]
    z               = np.array(res["z_smooth"])
    spacing         = res["spacing_m"]
    coords_wgs_raw  = np.array(res["coords_wgs_raw"])
    z_raw           = np.array(res["z_raw"])
    
    n_pts           = len(z)
    structure_tier  = res.get("structure_tier", np.zeros(n_pts, dtype=int))
    iri_proxy       = res.get("iri_proxy_in", np.zeros(n_pts, dtype=float))
    asd             = res.get("available_sight_dist_ft", np.zeros(n_pts, dtype=float))
    
    # Map tier integers to strings efficiently
    tier_map = np.array(["None", "1_HPMS", "2_NBI", "3_DIP"])
    mapped_tiers = tier_map[np.clip(structure_tier, 0, 3)]

    headings_unwrapped = calculate_headings(coords_m)
    
    # Calculate tangent grades
    grads = np.array([get_tangent_grade(z, i, spacing, params["REGRESSION_WINDOW_M"]) for i in range(n_pts)])
    total_len = max(float(res["d_axis"][-1]), 1.0)
    
    # Vectorize distance and milepost math
    local_dist_m  = np.arange(n_pts) * spacing
    frac          = local_dist_m / total_len
    mps           = chunk_s_mp + frac * (chunk_e_mp - chunk_s_mp)
    dist_ft       = (global_start_dist_m + local_dist_m) * FEET_PER_METER

    return pd.DataFrame({
        "RouteId":                  route_id,
        "Milepost":                 np.round(mps, 4),
        "Dist_Ft":                  np.round(dist_ft, 3),
        "Lon":                      coords_wgs[:, 0],
        "Lat":                      coords_wgs[:, 1],
        "Elev_Ft":                  np.round(z * FEET_PER_METER, 3),
        "Raw_Lon":                  coords_wgs_raw[:, 0],
        "Raw_Lat":                  coords_wgs_raw[:, 1],
        "Elev_Raw_Ft":              np.round(z_raw * FEET_PER_METER, 3),
        "Grade_Pct":                np.round(grads, 3),
        "Heading_Deg_Unwrapped":    np.round(headings_unwrapped, 5),
        "Structure_Tier":           mapped_tiers,
        "Micro_Jitter_Inches":      np.round(iri_proxy, 4),
        "Available_Sight_Dist_Ft":  np.round(asd, 1),
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
            if res is None or not res.get("ok", True):
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

def generate_interactive_profile(df_vtx, df_h, df_v, out_path, route_id):
    """
    Generates a high-fidelity interactive HTML dashboard for corridor analysis.
    Upgraded to reference Mileposts and include an interactive range-slider zoom at the bottom.
    """
    # Create subplots: 1. Curvature, 2. Elevation + Sight Distance, 3. Roughness
    fig = make_subplots(
        rows=3, cols=1, 
        shared_xaxes=True, 
        vertical_spacing=0.05,
        row_heights=[0.25, 0.5, 0.25],
        subplot_titles=(f"Horizontal Curvature (Route {route_id})", "Vertical Profile & Sight Distance", "Ride Quality: Micro-Jitter vs. IRI Reported")
    )

    # --- ROW 1: Horizontal Curvature ---
    fig.add_trace(go.Scatter(
        x=df_vtx['Milepost'], y=df_vtx['Heading_Deg_Unwrapped'].diff(),
        mode='lines', name='Curvature Change',
        line=dict(color='teal', width=1.5)
    ), row=1, col=1)

    # --- ROW 2: Vertical Profile (Elevation) ---
    # Draw the main profile
    fig.add_trace(go.Scatter(
        x=df_vtx['Milepost'], y=df_vtx['Elev_Ft'],
        mode='lines', name='Smooth Profile',
        line=dict(color='blue', width=2.5)
    ), row=2, col=1)

    # Overlay Raw Elevation (to see the "noise" we smoothed out)
    fig.add_trace(go.Scatter(
        x=df_vtx['Milepost'], y=df_vtx['Elev_Raw_Ft'],
        mode='lines', name='Raw DEM',
        line=dict(color="#e01f1f", width=1.0, dash='dot')
        # , opacity=0.5
    ), row=2, col=1)

    # HIGHLIGHT: Available Sight Distance Heatmap
    if 'Available_Sight_Dist_Ft' in df_vtx.columns:
        blind_mask = df_vtx['Available_Sight_Dist_Ft'] < 400
        fig.add_trace(go.Scatter(
            x=df_vtx.loc[blind_mask, 'Milepost'],
            y=df_vtx.loc[blind_mask, 'Elev_Ft'],
            mode='markers', name='Sight Obstruction (<400ft)',
            marker=dict(color='red', size=5, symbol='x')
        ), row=2, col=1)

    # --- ROW 3: Micro-Jitter + IRI Reported ---
    if 'Micro_Jitter_Inches' in df_vtx.columns:
        fig.add_trace(go.Scatter(
            x=df_vtx['Milepost'], y=df_vtx['Micro_Jitter_Inches'],
            mode='lines', name='Micro Jitter (RAT-derived)',
            line=dict(color='purple', width=1.5),
            fill='tozeroy'
        ), row=3, col=1)

    if 'IRI_Reported' in df_vtx.columns and df_vtx['IRI_Reported'].notna().any():
        fig.add_trace(go.Scatter(
            x=df_vtx['Milepost'], y=df_vtx['IRI_Reported'],
            mode='lines', name='IRI Reported (HPMS)',
            line=dict(color='darkorange', width=2.0),
            opacity=0.85
        ), row=3, col=1)

    # Formatting Layout
    fig.update_layout(
        height=950,
        template="plotly_white",
        showlegend=True,
        title_text=f"RAT Corridor Analytics: {route_id}",
        hovermode="x unified",
        uirevision="constant",
    )
    fig.update_yaxes(title_text="Delta Deg", row=1, col=1)
    fig.update_yaxes(title_text="Elevation (ft)", row=2, col=1,
                     fixedrange=False, autorange=True)
    fig.update_yaxes(title_text="Roughness (in/mi)", row=3, col=1)
    
    # Apply Milepost labeling and append the interactive range slider to the base timeline
    fig.update_xaxes(
        title_text="Calibrated Reference Post (Milepost)", 
        rangeslider=dict(visible=True), 
        row=3, col=1
    )

    fig.write_html(out_path)

    # Post-process the HTML to inject a JavaScript listener that rescales the
    # elevation y-axis (Row 2) whenever the x-axis zoom range changes. Plotly's
    # shared_xaxes only syncs the x-axis; it does not automatically rescale
    # linked y-axes to the visible data window. The script below listens for
    # the 'plotly_relayout' event, finds all elevation trace data points within
    # the new x-range, and updates yaxis2 to fit the visible data with a 5%
    # margin on each side.
    js = """
<script>
(function() {
    var plotDiv = document.getElementsByClassName('plotly-graph-div')[0];
    if (!plotDiv) return;

    plotDiv.on('plotly_relayout', function(eventData) {
        // Detect x-axis zoom on any of the shared axes
        var xMin = null, xMax = null;
        for (var key in eventData) {
            // 1. Standard drag zoom sends 'xaxis.range[0]' and 'xaxis.range[1]'
            if (key.match(/^xaxis[0-9]*\\.range\\[0\\]$/)) {
                xMin = eventData[key];
            } else if (key.match(/^xaxis[0-9]*\\.range\\[1\\]$/)) {
                xMax = eventData[key];
            } 
            // 2. Rangeslider zoom sends a single array 'xaxis.range'
            else if (key.match(/^xaxis[0-9]*\\.range$/) && Array.isArray(eventData[key])) {
                xMin = eventData[key][0];
                xMax = eventData[key][1];
            }
        }
        
        // Also handle autorange reset (e.g., double-click)
        var isReset = false;
        for (var key in eventData) {
            if (key.match(/^xaxis[0-9]*\\.autorange$/)) {
                isReset = true;
            }
        }
        if (isReset) {
            Plotly.relayout(plotDiv, {'yaxis2.autorange': true});
            return;
        }
        
        // Abort if the x-axis wasn't the axis being adjusted
        if (xMin === null || xMax === null) return;

        xMin = parseFloat(xMin);
        xMax = parseFloat(xMax);

        // Find traces on row 2 (yaxis2) — Smooth Profile and Raw DEM
        var yVals = [];
        plotDiv.data.forEach(function(trace) {
            if (trace.yaxis !== 'y2') return; // We only want row 2 (Elevation)
            // Only consider elevation traces (not markers)
            if (trace.mode && trace.mode.indexOf('markers') !== -1) return;
            if (!trace.x || !trace.y) return;
            for (var i = 0; i < trace.x.length; i++) {
                var xv = parseFloat(trace.x[i]);
                if (xv >= xMin && xv <= xMax) {
                    var yv = parseFloat(trace.y[i]);
                    if (!isNaN(yv)) yVals.push(yv);
                }
            }
        });

        if (yVals.length === 0) return;

        var yMin = Math.min.apply(null, yVals);
        var yMax = Math.max.apply(null, yVals);
        var margin = (yMax - yMin) * 0.05 || 5;

        // Apply new bounds to yaxis2
        Plotly.relayout(plotDiv, {
            'yaxis2.range': [yMin - margin, yMax + margin],
            'yaxis2.autorange': false
        });
    });
})();
</script>
"""
    with open(out_path, "r", encoding="utf-8") as f:
        html_content = f.read()
    html_content = html_content.replace("</body>", js + "\n</body>")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html_content)

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True, help="Local HPMS file (.csv/.shp/.geojson)")
    parser.add_argument("--route", required=True, help="Route ID")
    parser.add_argument("--outdir", required=True, help="Output directory")
    parser.add_argument("--demdir", required=True, help="DEM cache directory")
    parser.add_argument("--params_json", default=None, help="Optional JSON parameter overrides")
    parser.add_argument("--start", type=float, default=None, help="Optional start RP override")
    parser.add_argument("--end", type=float, default=None, help="Optional end RP override")
    parser.add_argument("--high_res",   action="store_true", help="Use 1-meter high-resolution DEMs (15-mile limit)")
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
    
    allowed_facs = user_params.get("FACILITY_TYPE_FILTER", [])
    if allowed_facs:
        initial_count = len(df)
        df = apply_facility_fsystem_filters(df, facility_filter=allowed_facs)
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
            
    if args.high_res:
        route_length_miles = float(sub["End_MP"].max()) - float(sub["Start_MP"].min())
        logging.info(f"*** HIGH-RES MODE ACTIVATED *** Processing ~{route_length_miles:.1f} miles at 1-meter resolution.")
        user_params["HIGH_RES_MODE"] = True
            
    logging.info(f"Rows for route {route_norm}: {len(sub):,}")
        
    predominant_f_sys = int(sub["FSystem"].mode()[0])
    
    all_chunks = []
    lines, chunk_indices = stitch_linestrings_ordered(
        sub["WKT"].tolist(), return_indices=True,
        start_mps=sub["Start_MP"].tolist(), end_mps=sub["End_MP"].tolist(),
    )
    for g, idxs in zip(lines, chunk_indices):
        contrib = sub.iloc[idxs]
        chunk_low_mp  = float(contrib[["Start_MP", "End_MP"]].min().min())
        chunk_high_mp = float(contrib[["Start_MP", "End_MP"]].max().max())

        # Same fix as rat_alignment_cli.py's process_route(): determine each
        # chunk's true direction from its own contributing rows' Start_MP/
        # End_MP, not from chunk-processing order (which is unreliable --
        # some states submit ascending, some descending).
        first_row = contrib.iloc[0]
        last_row  = contrib.iloc[-1]
        first_mid = (float(first_row["Start_MP"]) + float(first_row["End_MP"])) / 2.0
        last_mid  = (float(last_row["Start_MP"])  + float(last_row["End_MP"]))  / 2.0
        if first_mid > last_mid:
            g = LineString(list(g.coords)[::-1])

        all_chunks.append({
            "geom": g, "f_sys": predominant_f_sys,
            "chunk_low_mp": chunk_low_mp, "chunk_high_mp": chunk_high_mp,
        })

    if not all_chunks:
        raise ValueError("No valid stitched geometry found.")

    # Bridge chunks left disconnected by a small margin just over snap_tol
    # (e.g. closed-loop routes) -- same fix as rat_alignment_cli.py.
    all_chunks = merge_close_chunks(all_chunks, merge_tol_ft=500.0, route_id=route_norm)

    # Process chunks in true ascending-milepost order, not raw processing order.
    all_chunks.sort(key=lambda c: c["chunk_low_mp"])

    total_stitch_len_m = 0.0
    for info in all_chunks:
        lon, lat = info["geom"].coords[0]
        utm = get_appropriate_utm_zone(lon, lat)
        proj = Transformer.from_crs("EPSG:4326", f"EPSG:{utm}", always_xy=True)
        coords_m = [proj.transform(x, y) for x, y in info["geom"].coords]
        info["length_m"] = LineString(coords_m).length
        total_stitch_len_m += info["length_m"]

    if total_stitch_len_m == 0: total_stitch_len_m = 1.0
    
    nbi_url = user_params.get("NBI_URL", None)
    nti_url = user_params.get("NTI_URL", None)
    
    state_fips_input = str(user_params.get("STATE_FIPS", "")).strip()
    
    nbi_nti_gdf = fetch_nbi_nti_state(state_fips_input, user_params.get("SOCRATA_TOKEN", ""), nbi_url, nti_url)

    all_h, all_v, all_vtx_dfs = [], [], []
    global_continuous_dist_m = 0.0 

    for idx, info in enumerate(all_chunks):
        chunk_length_m = info["length_m"]
        # Each chunk's milepost range comes directly from its own
        # constituent rows (chunk_low_mp/chunk_high_mp), not from a
        # cumulative-distance proportion over chunk-processing order.
        chunk_s_mp = info["chunk_low_mp"]
        chunk_e_mp = info["chunk_high_mp"]
        
        # ====================================================================
        # V3.3 HIGH-RES EPHEMERAL GRID ROUTER
        # ====================================================================
        if args.high_res:
            logging.info(f"Route {route_norm}: Running 1-meter map-first grid extraction...")
            import shutil
            import math
            
            # Establish localized thread directories to keep cache lookups segregated
            worker_id = os.getpid()
            local_tile_dir = os.path.join(args.demdir, f"p_p_1m_worker_{worker_id}")
            os.makedirs(local_tile_dir, exist_ok=True)
            
            # Slice route geometry into 0.02 degree grid increments (~1.3 miles)
            minx, miny, maxx, maxy = info["geom"].bounds
            x_ticks = np.arange(minx, maxx + 0.02, 0.02)
            y_ticks = np.arange(miny, maxy + 0.02, 0.02)
            
            try:
                for x in x_ticks:
                    for y in y_ticks:
                        # Define target tile tile bounds
                        tile_bbox = (x, y, x + 0.02, y + 0.02)
                        
                        # Only fetch data if the route linestring actually crosses this tile cell
                        from shapely.geometry import box
                        if info["geom"].intersects(box(*tile_bbox)):
                            # Download custom 1-meter slice from USGS directly to the temporary folder
                            from core.rat_core import download_high_res_dem_tile
                            download_high_res_dem_tile(tile_bbox, local_tile_dir)
                
                # Point the core smoothing function to our localized temporary 1m folder
                params["HIGH_RES_MODE"] = True
                current_dem_dir = local_tile_dir
                
            except Exception as e:
                logging.error(f"1m Tile loop processing error: {e}")
                current_dem_dir = args.demdir
        else:
            current_dem_dir = args.demdir
        
        # RE-INJECTED PROFILE ENGINE CALL (Points to current_dem_dir)
        res = smooth_plan_profile_from_linestring(
            info["geom"], current_dem_dir, params, info["f_sys"],
            route_id=route_norm, chunk_s_mp=chunk_s_mp, chunk_e_mp=chunk_e_mp,
            hpms_subset=sub, nbi_nti_gdf=nbi_nti_gdf
        )
        
        if res is None or not res.get("ok", True):
            if res and res.get("error"):
                logging.debug(f"Skipping chunk [{chunk_s_mp:.4f},{chunk_e_mp:.4f}]: {res['error']}")
            if args.high_res and os.path.exists(local_tile_dir):
                shutil.rmtree(local_tile_dir)
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
            if "SC_Dist" in c:
                c["Calibrated_SC_MP"] = chunk_s_mp + (c["SC_Dist"] / total_len) * (chunk_e_mp - chunk_s_mp)
                c["Calibrated_CS_MP"] = chunk_s_mp + (c["CS_Dist"] / total_len) * (chunk_e_mp - chunk_s_mp)
                c["SC_Dist"] += global_continuous_dist_m
                c["CS_Dist"] += global_continuous_dist_m
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
        
        # EPHEMERAL PURGE CLEANUP BLOCK
        if args.high_res and os.path.exists(local_tile_dir):
            shutil.rmtree(local_tile_dir)
            
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
        
        # False Valley Detector
        v["False_Valley_Warning"] = False
        if v["Type"] == "SAG" and mask.any():
            overlapping_tiers = final_vtx_df.loc[mask, "Structure_Tier"].unique()
            if "1_HPMS" in overlapping_tiers or "2_NBI" in overlapping_tiers:
                v["False_Valley_Warning"] = True

    final_vtx_df.to_csv(vertices_csv, index=False)

    # Map HPMS-reported IRI onto the vertex table by milepost overlap so it
    # can be plotted alongside the RAT-derived micro jitter in the dashboard.
    # Each HPMS section's IRI value is assigned to all vertices within
    # [Start_MP, End_MP] for that section.
    final_vtx_df["IRI_Reported"] = np.nan
    iri_col = next((c for c in sub.columns if c.upper() == "IRI"), None)
    if iri_col:
        for _, row in sub.iterrows():
            iri_val = pd.to_numeric(row[iri_col], errors="coerce")
            if pd.notna(iri_val):
                mask = (
                    (final_vtx_df["Milepost"] >= float(row["Start_MP"])) &
                    (final_vtx_df["Milepost"] <= float(row["End_MP"]))
                )
                final_vtx_df.loc[mask, "IRI_Reported"] = iri_val
    
    dashboard_html = os.path.join(args.outdir, f"{base}_analytics_dashboard.html")
    try:
        logging.info("Generating interactive analytics dashboard...")
        generate_interactive_profile(final_vtx_df, final_h_df, final_v_df, dashboard_html, route_norm)
        logging.info(f"Dashboard saved: {dashboard_html}")
    except Exception as e:
        logging.warning(f"Could not generate dashboard: {e}")
    
    if not final_h_df.empty:
        final_h_df.to_csv(h_csv, index=False)
    else:
        pd.DataFrame(columns=["RouteId"]).to_csv(h_csv, index=False)
        
    if not final_v_df.empty:
        final_v_df.to_csv(v_csv, index=False)
    else:
        pd.DataFrame(columns=["RouteId"]).to_csv(v_csv, index=False)

    # ==========================================
    # BRIDGE TELEMETRY DIAGNOSTIC REPORT
    # ==========================================
    if 'Structure_Tier' in final_vtx_df.columns:
        counts = final_vtx_df['Structure_Tier'].value_counts()
        t1 = counts.get("1_HPMS", 0)
        t2 = counts.get("2_NBI", 0)
        t3 = counts.get("3_DIP", 0)
        
        logging.info("========================================")
        logging.info("=== BRIDGE MATCH RATE & GAP REPORT ===")
        logging.info("========================================")
        logging.info(f"Vertices caught by Tier 1 (HPMS):    {t1:,}")
        logging.info(f"Vertices caught by Tier 2 (NBI):     {t2:,}")
        logging.info(f"Vertices caught by Tier 3 (Dip):     {t3:,}")
        
    if not final_v_df.empty and 'False_Valley_Warning' in final_v_df.columns:
        false_valleys = final_v_df['False_Valley_Warning'].sum()
        if false_valleys > 0:
            logging.warning(f"QA/QC FLAG: Detected {false_valleys} vertical SAG curves perfectly overlapping a bridge. Spline sag may be present.")
        else:
            logging.info("QA/QC: Zero false valleys detected at bridge crossings.")
    logging.info("========================================")

    logging.info(f"Saved vertices:   {vertices_csv}")
    logging.info(f"Saved horizontal: {h_csv}")
    logging.info(f"Saved vertical:   {v_csv}")
    logging.info("Done.")

if __name__ == "__main__":
    main()