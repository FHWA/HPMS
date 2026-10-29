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
RAT PLAN & PROFILE CLI v3.3 (Data Pre-Processor)
--------------------------------------------------------------------------------
ROLE: Route-specific data extractor for engineering sheets.
DESCRIPTION:
Isolates a single route (or specific milepost bounds), stitches the geometry,
and applies the core smoothing engine. It calculates exact continuous distances
across disjointed chunks to prevent overlapping, and exports the raw and smoothed
vertices to CSVs to be consumed by the downstream PDF rendering script.

Elevations always come from the USGS 3DEP 1 m DEM, cached in the shared
align_1m_cache folder under the DEM directory (the same cache
rat_alignment_cli.py uses). The smoothing factors for the state are applied
by core.rat_core.build_params(). A run-parameters JSON is written beside the
CSVs so the PDF can show the settings the run used.

The analytics dashboard's ride-quality chart puts Micro Jitter (a height
in inches) on its own right-hand axis, separate from IRI Reported (in/mi)
on the left, because the two have different units.

CREATED BY: Federal Highway Administration, Office of Highway Policy Information.
VERSION: 3.3, October 29, 2026
"""
import os
import sys
import re
import json
import argparse
import logging
import numpy as np
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots
THIS_DIR = os.path.dirname(os.path.abspath(__file__))
RAT_SUITE_DIR = os.path.dirname(THIS_DIR)
if RAT_SUITE_DIR not in sys.path:
    sys.path.insert(0, RAT_SUITE_DIR)
from core.rat_core import (
    build_params,
    SOCRATA_TOKEN as ENV_SOCRATA_TOKEN,
    FEET_PER_METER,
    build_route_continuity_chunks,
    describe_smoothing_factors,
    smooth_plan_profile_from_linestring,
    profile_result_ok,
    analyze_horizontal_curvature,
    analyze_vertical_parabolic,
    build_grade_segments,
    label_vertices_by_segments,
    add_speed_reference,
    merge_horizontal_curves,
    calculate_headings,
    get_tangent_grade,
    load_local_hpms,
    apply_facility_fsystem_filters,
    fetch_nbi_nti_state,
    fetch_socrata_nearby,
    compute_padded_bbox,
    ensure_dem_cells_cached,
    DemCacheUnavailable,
    STRUCTURE_TIER_LABELS,
    STRUCTURE_KIND_LABELS,
    STRUCTURE_RECORD_LABELS,
)
from core.rat_cross_section import (
    build_cross_sections, classify_crossings, select_nearby_lines, line_off_road_ranges,
    NEARBY_SEARCH_FT,
)
from core.rat_structure_report import (
    build_structure_span_table,
    render_structure_span_pdf,
)
from core.rat_param_summary import (
    build_run_param_summary,
    render_param_panel_html,
    save_run_param_summary_json,
)
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s: %(message)s")

# ===========================================================================
# CONFIGURATION
# ===========================================================================
# Other HPMS lines around the section (the non-inventory direction, ramps,
# frontage roads) guide the cross sections. They come from the input file
# and, when True, also from Socrata (all facility types and functional
# systems within the section's bounding box), since the run's own extract is
# usually limited to Facility Types 1-2.
NEARBY_LINES_FROM_SOCRATA = True
# ===========================================================================


def build_vertices_df(res: dict, route_id: str, chunk_s_mp: float, chunk_e_mp: float, global_start_dist_m: float, params: dict) -> pd.DataFrame:
    coords_wgs = res["coords_wgs_smooth"]
    coords_m = res["coords_m_smooth"]
    z = res["z_smooth"]
    spacing = res["spacing_m"]
    coords_wgs_raw = res["coords_wgs_raw"]
    z_raw = res["z_raw"]
    structure_tier  = res.get("structure_tier", np.zeros(len(z), dtype=int))
    structure_kind  = res.get("structure_kind", np.zeros(len(z), dtype=int))
    structure_rec   = res.get("structure_record", np.zeros(len(z), dtype=int))
    iri_proxy       = res.get("iri_proxy_in", np.zeros(len(z), dtype=float))
    asd             = res.get("available_sight_dist_ft", np.zeros(len(z), dtype=float))
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
            "Structure_Tier":           STRUCTURE_TIER_LABELS.get(int(structure_tier[i]), "None"),
            "Structure_Kind":           STRUCTURE_KIND_LABELS.get(int(structure_kind[i]), "None"),
            "Structure_Record":         STRUCTURE_RECORD_LABELS.get(int(structure_rec[i]), "None"),
            "Micro_Jitter_Inches":      round(float(iri_proxy[i]), 4),
            "Available_Sight_Dist_Ft":  round(float(asd[i]), 1),
        })
    return pd.DataFrame(rows)

def gather_nearby_lines(df_all: pd.DataFrame, route_id: str, vtx_df: pd.DataFrame,
                        user_params: dict, state_fips: str) -> pd.DataFrame:
    """HPMS lines within NEARBY_SEARCH_FT of the plotted section, from the
    input file and (NEARBY_LINES_FROM_SOCRATA) Socrata."""
    sec = vtx_df[["Lon", "Lat"]].to_numpy()[::20]
    frames = [df_all]
    if NEARBY_LINES_FROM_SOCRATA and state_fips not in ("", "00"):
        pad = NEARBY_SEARCH_FT / FEET_PER_METER / 50000.0
        bbox = (sec[:, 0].min() - pad, sec[:, 1].min() - pad, sec[:, 0].max() + pad, sec[:, 1].max() + pad)
        try:
            frames.append(fetch_socrata_nearby(state_fips, bbox, user_params.get("SOCRATA_TOKEN") or ENV_SOCRATA_TOKEN,
                                               user_params.get("INPUT_URL") or None))
        except Exception as e:
            logging.warning(f"Could not fetch nearby HPMS lines from Socrata: {e}")
    cand = pd.concat([f for f in frames if f is not None and not f.empty], ignore_index=True)
    if cand.empty or "Facility_Type" not in cand.columns:
        return pd.DataFrame(columns=["RouteId", "Facility_Type", "WKT"])
    cand = cand.drop_duplicates(subset=["RouteId", "Start_MP", "End_MP", "WKT"])
    lines = select_nearby_lines(cand, route_id, sec)
    counts = lines.groupby("Facility_Type").size().to_dict() if not lines.empty else {}
    logging.info(f"Nearby HPMS lines within {NEARBY_SEARCH_FT:.0f} ft (by Facility_Type): {counts or 'none'}")
    return lines


GRADE_COLUMNS = ["RouteId", "Part", "FSystem", "Start_Dist", "End_Dist",
                 "Calibrated_Start_MP", "Calibrated_End_MP", "Length_ft", "Grade_Pct", "Grade_Bin",
                 "Line_Off_Road"]


def grades_table(segments: list) -> pd.DataFrame:
    """Grade segments (core.rat_core.build_grade_segments) as the output table."""
    df = pd.DataFrame(segments, columns=GRADE_COLUMNS)
    df["Length_ft"] = df["Length_ft"].round(2)
    df["Grade_Pct"] = df["Grade_Pct"].round(3)
    return df


def flag_line_off_road(ranges, h_curves, v_curves, segments, vtx_df):
    """Marks curves, grade segments and vertices that overlap a stretch
    where the cross sections show the HPMS line off the roadway
    (core.rat_cross_section.flag_line_off_road). Their geometry there
    describes the ground beside the road, not the road."""
    for rows in (h_curves, v_curves, segments):
        for r in rows:
            r["Line_Off_Road"] = any(r["Calibrated_Start_MP"] < hi and r["Calibrated_End_MP"] > lo
                                     for lo, hi in ranges)
    mp = vtx_df["Milepost"].to_numpy()
    on = np.zeros(len(mp), dtype=bool)
    for lo, hi in ranges:
        on |= (mp >= lo) & (mp <= hi)
    vtx_df["Line_Off_Road"] = on
    for lo, hi in ranges:
        logging.warning(f"QA/QC FLAG: the HPMS line appears to be off the roadway from MP {lo:.3f} to "
                        f"{hi:.3f} (cross sections find no lanes at the line and the centerline far "
                        f"from it). Curves and grades there follow the ground beside the road; check "
                        f"the line work against current imagery.")
    if not ranges:
        logging.info("QA/QC: Cross sections find the HPMS line on the roadway throughout.")


def safe_route_name(route_id: str) -> str:
    return re.sub(r'[<>:"/\\|?*]+', "-", str(route_id)).strip()

def generate_calibration_dashboard(sample_chunks, demdir, base_params, out_html_path, route_id, sweep_workers=None):
    """
    Single-route cross-check of the horizontal smoothing factor: runs the
    national calibration's Viterbi classifier-stability search
    (tools/rat_national_calibration.py) on this route's own sampled
    one-mile segments and reports the result beside the factor this run
    used. Informational only; it does not change the factors applied. The
    vertical factor is a single national value (core.rat_core.
    NATIONAL_V_SMOOTH_FACTOR) and is shown for reference. demdir is unused
    (the horizontal search needs only the HPMS geometry).
    """
    import collections
    from tools.rat_national_calibration import _h_viterbi_stability_search
    from core.rat_core import _preload_chunk_for_h_sweep

    if not sample_chunks:
        logging.warning("Calibration cross-check: no sample chunks provided -- skipping.")
        return

    sweep_workers = sweep_workers or min(6, os.cpu_count() or 4)
    predominant_f_sys = collections.Counter(c["f_sys"] for c in sample_chunks).most_common(1)[0][0]

    chunks = [c for c in (_preload_chunk_for_h_sweep((c["geom"], base_params)) for c in sample_chunks)
              if c is not None]
    kappa_thresh = 1.0 / max(base_params.get("H_MAX_RADIUS", 165000.0 / FEET_PER_METER), 1e-6)
    viterbi_h, viterbi_overlap, _ = _h_viterbi_stability_search(
        chunks, kappa_thresh, base_params.get("H_HMM_STICKINESS", 1.0),
        base_params.get("H_MIN_CURVE_LENGTH_M", 30.48), sweep_workers, route_id, predominant_f_sys)

    suffix = "" if predominant_f_sys == 1 else f"_FS{predominant_f_sys}"
    used_h = base_params.get(f"H_SMOOTH_FACTOR{suffix}", base_params.get("H_SMOOTH_FACTOR"))
    used_v = base_params.get("V_SMOOTH_FACTOR")

    def _fmt(val):
        return f"{val:g}" if val is not None else "BLANK (did not stabilize)"

    h_detail = f" (length-weighted overlap = {viterbi_overlap:.3f})" if viterbi_overlap == viterbi_overlap else ""

    html = f"""
    <html><head><title>Calibration Cross-Check - Route {route_id}</title>
    <style>
        body {{ font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; margin: 40px; background-color: #f8f9fa; color: #333; }}
        h2 {{ color: #2c3e50; border-bottom: 2px solid #deff9a; padding-bottom: 10px; display: inline-block; }}
        table {{ border-collapse: collapse; width: 90%; margin-top: 10px; background-color: white; box-shadow: 0 4px 6px rgba(0,0,0,0.1); border-radius: 8px; overflow: hidden; }}
        th, td {{ padding: 12px; text-align: center; border-bottom: 1px solid #e9ecef; }}
        th {{ background-color: #2c3e50; color: #deff9a; font-weight: bold; font-size: 15px; }}
        .note {{ font-style: italic; color: #6c757d; margin-top: 20px; line-height: 1.6; max-width: 90%; }}
    </style>
    </head><body>
    <h2>Calibration Cross-Check for Route {route_id}</h2>
    <p>Runs the Viterbi-stability search used to build the national horizontal
    smoothing factors against this route's own {len(sample_chunks)} randomly
    sampled one-mile segments (predominant functional system:
    FS{predominant_f_sys}). Informational only: it does not change the
    factors this run applied.</p>
    <table>
        <tr><th>Axis</th><th>Route Sample Result</th><th>Factor Used (this run)</th></tr>
        <tr><td><strong>Horizontal</strong></td><td>{_fmt(viterbi_h)}{h_detail}</td><td>{_fmt(used_h)}</td></tr>
        <tr><td><strong>Vertical</strong></td><td>national factor, not calibrated by route</td><td>{_fmt(used_v)}</td></tr>
    </table>
    <div class="note">A close match supports the state/FS horizontal factor
    for this route. A mismatch is worth a closer look at this route, but a
    single route's sample is much noisier evidence than the state runs the
    factors came from. "BLANK" means the search found no stable answer for
    this sample.</div>
    </body></html>
    """

    with open(out_html_path, "w", encoding="utf-8") as out_f:
        out_f.write(html)

def generate_interactive_profile(df_vtx, df_h, df_v, out_path, route_id, params=None):
    """
    Interactive HTML dashboard for the route: curvature, the vertical
    profile with sight distance, and ride quality, with a range slider on
    the milepost axis. A script added after writing keeps the elevation
    axis fitted to the zoomed range, which shared x-axes do not do.

    params, if given, adds the run-parameters corner panel
    (core.rat_param_summary).

    In the ride-quality row, IRI Reported (HPMS) uses the left axis and
    Micro Jitter (the RAT roughness proxy, a height in inches) its own
    right axis, since the two have different units. Only the
    display is affected. Each axis title is colored to match its trace.
    Without IRI data the row has one axis, for Micro Jitter: an axis with
    no trace on it stops plotly.js from drawing the page once the range
    slider is added.
    """
    has_iri = 'IRI_Reported' in df_vtx.columns and df_vtx['IRI_Reported'].notna().any()
    # Rows: 1 curvature, 2 elevation and sight distance, 3 ride quality
    # (with a second y-axis for Micro Jitter where IRI is shown).
    fig = make_subplots(
        rows=3, cols=1,
        shared_xaxes=True,
        vertical_spacing=0.05,
        row_heights=[0.25, 0.5, 0.25],
        specs=[[{"secondary_y": False}],
               [{"secondary_y": False}],
               [{"secondary_y": has_iri}]],
        subplot_titles=(f"Horizontal Curvature (Route {route_id})", "Vertical Profile & Sight Distance", "Ride Quality: Micro-Jitter vs. IRI Reported" if has_iri else "Ride Quality: Micro-Jitter")
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

    # Raw DEM elevation, to show what the smoothing removed
    fig.add_trace(go.Scatter(
        x=df_vtx['Milepost'], y=df_vtx['Elev_Raw_Ft'],
        mode='lines', name='Raw DEM',
        line=dict(color="#e01f1f", width=1.0, dash='dot')
    ), row=2, col=1)

    # Points where available sight distance is under 400 ft
    if 'Available_Sight_Dist_Ft' in df_vtx.columns:
        blind_mask = df_vtx['Available_Sight_Dist_Ft'] < 400
        fig.add_trace(go.Scatter(
            x=df_vtx.loc[blind_mask, 'Milepost'],
            y=df_vtx.loc[blind_mask, 'Elev_Ft'],
            mode='markers', name='Sight Obstruction (<400ft)',
            marker=dict(color='red', size=5, symbol='x')
        ), row=2, col=1)

    # --- ROW 3: Micro-Jitter (secondary/right axis) + IRI Reported (primary/left axis) ---
    if 'Micro_Jitter_Inches' in df_vtx.columns:
        fig.add_trace(go.Scatter(
            x=df_vtx['Milepost'], y=df_vtx['Micro_Jitter_Inches'],
            mode='lines', name='Micro Jitter (RAT roughness proxy, in)',
            line=dict(color='purple', width=1.5),
            fill='tozeroy'
        ), row=3, col=1, secondary_y=has_iri)

    if has_iri:
        fig.add_trace(go.Scatter(
            x=df_vtx['Milepost'], y=df_vtx['IRI_Reported'],
            mode='lines', name='IRI Reported (HPMS)',
            line=dict(color='darkorange', width=2.0),
            opacity=0.85
        ), row=3, col=1, secondary_y=False)

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

    # Row 3: IRI Reported on the left (orange), Micro Jitter on the right
    # (purple), each scaled to its own data; Micro Jitter alone on the left
    # where there is no IRI.
    if has_iri:
        fig.update_yaxes(
            title_text="IRI Reported (in/mi)", title_font=dict(color="darkorange"),
            tickfont=dict(color="darkorange"),
            row=3, col=1, secondary_y=False,
        )
    fig.update_yaxes(
        title_text="Micro Jitter (in)", title_font=dict(color="purple"),
        tickfont=dict(color="purple"),
        row=3, col=1, secondary_y=has_iri,
        fixedrange=False, autorange=True, showgrid=False,
    )

    # Milepost axis label and range slider
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

    # Run-parameters corner panel (core.rat_param_summary)
    if params is not None:
        try:
            mp_start = float(df_vtx["Milepost"].min()) if "Milepost" in df_vtx.columns and not df_vtx.empty else None
            mp_end = float(df_vtx["Milepost"].max()) if "Milepost" in df_vtx.columns and not df_vtx.empty else None
            param_summary = build_run_param_summary(params, route_id=route_id, mp_start=mp_start, mp_end=mp_end)
            panel_html = render_param_panel_html(param_summary)
            html_content = html_content.replace("</body>", panel_html + "\n</body>")
        except Exception as e:
            logging.warning(f"Could not render run-parameters panel on analytics dashboard: {e}")

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

    # The state's smoothing factors are applied by build_params(); the FIPS
    # code is also used below for the NBI/NTI and nearby-line lookups.
    state_fips_input = str(user_params.get("STATE_FIPS", "")).strip().zfill(2)

    params = build_params(user_params)
    logging.info(describe_smoothing_factors(params))

    shared_cache_dir = os.path.join(args.demdir, "align_1m_cache")

    logging.info("Loading input...")
    df = load_local_hpms(args.input)
    df_all = df

    # The facility type filter only; the route is chosen by its ID, so its
    # functional system is not filtered.
    initial_count = len(df)
    df = apply_facility_fsystem_filters(
        df,
        facility_filter=user_params.get("FACILITY_TYPE_FILTER", []),
        include_missing_facility=bool(user_params.get("FACILITY_TYPE_INCLUDE_MISSING", False)),
        include_missing_fsystem=True,
    ).copy()
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
        n_route = len(sub)
        sub = sub[(sub["Start_MP"] <= clip_e) & (sub["End_MP"] >= clip_s)].copy()
        if sub.empty:
            raise ValueError("No geometry overlaps the requested start/end RP range.")
        # Records overlapping the range are processed whole, so the run can
        # reach a little beyond it; say so, since a typo here silently
        # shortens the route.
        logging.info(
            f"Limited to RP {clip_s:g} to {clip_e:g} (Begin/End RP): {len(sub):,} of "
            f"{n_route:,} records for the route overlap that range; processing RP "
            f"{sub['Start_MP'].min():.3f} to {sub['End_MP'].max():.3f} (route runs RP "
            f"{df[df['RouteId'] == route_norm]['Start_MP'].min():.3f} to "
            f"{df[df['RouteId'] == route_norm]['End_MP'].max():.3f})."
        )

    logging.info(f"Rows for route {route_norm}: {len(sub):,}")

    all_chunks = build_route_continuity_chunks(route_norm, sub, user_params, log_detail=True)

    if not all_chunks:
        raise ValueError("No valid stitched geometry found.")

    # The national NBI/NTI tables are cut down to the route's padded
    # bounding box (see core.rat_core.compute_padded_bbox).
    route_bbox = compute_padded_bbox([info["geom"] for info in all_chunks])
    nbi_nti_gdf = fetch_nbi_nti_state(state_fips_input, bbox=route_bbox)

    # Download every 1 m DEM cell the route needs before processing.
    ensure_dem_cells_cached(
        [info["geom"] for info in all_chunks], shared_cache_dir,
        log_label=f"route {route_norm}",
    )

    all_h, all_v, all_g, all_vtx_dfs = [], [], [], []

    for idx, info in enumerate(all_chunks):
        chunk_s_mp = info["chunk_low_mp"]
        chunk_e_mp = info["chunk_high_mp"]

        # Normally already cached by the call above.
        ensure_dem_cells_cached([info["geom"]], shared_cache_dir)

        res = smooth_plan_profile_from_linestring(
            info["geom"], shared_cache_dir, params, info["f_sys"],
            route_id=route_norm, chunk_s_mp=chunk_s_mp, chunk_e_mp=chunk_e_mp,
            hpms_subset=sub, nbi_nti_gdf=nbi_nti_gdf
        )

        if not profile_result_ok(res):
            reason = res.get("error", "no result") if isinstance(res, dict) else "no result"
            logging.warning(
                f"Skipped the piece of route {route_norm} from RP {chunk_s_mp:.3f} to "
                f"{chunk_e_mp:.3f}: {reason}. It is missing from every output of this run."
            )
            continue

        spacing_m = res["spacing_m"]
        h = analyze_horizontal_curvature(res["coords_m_smooth"], spacing_m, params)
        if params.get("ENABLE_MERGE", False):
            h = merge_horizontal_curves(h, params)
        v = analyze_vertical_parabolic(res["z_smooth"], spacing_m, params)
        g = build_grade_segments(res["z_smooth"], spacing_m, v)
        seg_grade, seg_bin = label_vertices_by_segments(len(res["z_smooth"]), spacing_m, g)
        total_len = max(float(res["d_axis"][-1]), 1.0)

        for c in h:
            c["RouteId"] = route_norm
            c["Calibrated_Start_MP"] = chunk_s_mp + (c["Start_Dist"] / total_len) * (chunk_e_mp - chunk_s_mp)
            c["Calibrated_End_MP"] = chunk_s_mp + (c["End_Dist"] / total_len) * (chunk_e_mp - chunk_s_mp)
            for key in ("Start_Dist", "End_Dist", "SC_Dist", "CS_Dist"):
                if key in c:
                    c[key] += info["global_start_dist_m"]
        for c in v + g:
            c["RouteId"] = route_norm
            c["Part"] = chunk_s_mp
            c["FSystem"] = info["f_sys"]
            c["Calibrated_Start_MP"] = chunk_s_mp + (c["Start_Dist"] / total_len) * (chunk_e_mp - chunk_s_mp)
            c["Calibrated_End_MP"] = chunk_s_mp + (c["End_Dist"] / total_len) * (chunk_e_mp - chunk_s_mp)
            for key in ("Start_Dist", "End_Dist", "VPI_Dist"):
                if key in c:
                    c[key] += info["global_start_dist_m"]
        add_speed_reference(v, sub, info["f_sys"])

        all_h.extend(h)
        all_v.extend(v)
        all_g.extend(g)
        vtx_df = build_vertices_df(res, route_norm, chunk_s_mp, chunk_e_mp, info["global_start_dist_m"], params)
        vtx_df["Seg_Grade_Pct"] = np.round(seg_grade, 3)
        vtx_df["V_Grade_Bin"] = seg_bin
        all_vtx_dfs.append(vtx_df)

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
    g_csv = os.path.join(args.outdir, f"{base}_grades.csv")

    # --- RUN PARAMETERS JSON ---
    # rat_plan_profile_report_pdf.py finds it by the shared file name and
    # shows the parameters on the PDF cover page (core.rat_param_summary).
    params_json_path = os.path.join(args.outdir, f"{base}_run_parameters.json")
    try:
        param_summary = build_run_param_summary(
            params, route_id=route_norm,
            mp_start=float(global_mp_start), mp_end=float(global_mp_end),
        )
        param_summary.setdefault("identity", {})["dem_tile_dir"] = shared_cache_dir
        save_run_param_summary_json(param_summary, params_json_path)
        logging.info(f"Saved run-parameters summary: {params_json_path}")
    except Exception as e:
        logging.warning(f"Could not save run-parameters summary JSON: {e}")

    # --- CALIBRATION CROSS-CHECK ---
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
            generate_calibration_dashboard(sampled_chunks, shared_cache_dir, params, calibration_html, route_norm)
            logging.info(f"Saved calibration dashboard: {calibration_html}")
        else:
            logging.warning("Route too short to extract 1-mile test segments for calibration.")

    except Exception as e:
        logging.warning(f"Failed to generate calibration dashboard: {e}")

    final_vtx_df['H_Curve_Type'] = 'Tangent'
    final_vtx_df['V_Curve_Type'] = 'Tangent'
    final_vtx_df['V_Curve_Class'] = 'A'

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
        final_vtx_df.loc[mask, 'V_Curve_Class'] = v['VC_Class']

        # A SAG curve lying on a bridge span means the span line didn't hold.
        v["False_Valley_Warning"] = bool(
            v["Type"] == "SAG" and mask.any()
            and (final_vtx_df.loc[mask, "Structure_Kind"] == "Bridge").any()
        )
    final_v_df = pd.DataFrame(all_v)

    # Cross sections and measured superelevation from the 1 m DEM.
    try:
        nearby_lines = gather_nearby_lines(df_all, route_norm, final_vtx_df, user_params, state_fips_input)
        nearby_lines = classify_crossings(nearby_lines, final_vtx_df, shared_cache_dir, nbi_points=nbi_nti_gdf)
        if not nearby_lines.empty:
            nearby_csv = os.path.join(args.outdir, f"{base}_nearby_lines.csv")
            nearby_lines.to_csv(nearby_csv, index=False)
            logging.info(f"Saved {len(nearby_lines)} nearby HPMS lines: {nearby_csv}")
        xs_stations, xs_profiles = build_cross_sections(final_vtx_df, all_h, shared_cache_dir,
                                                        hpms_sub=sub, nearby_lines=nearby_lines)
        vd, vm = final_vtx_df["Dist_Ft"].to_numpy(), final_vtx_df["Milepost"].to_numpy()
        for h in all_h:
            for key in ("Start", "End"):
                d = h.get(f"Super_Full_{key}_Dist", np.nan)
                h[f"Super_Full_{key}_MP"] = round(float(np.interp(d * FEET_PER_METER, vd, vm)), 4) if pd.notna(d) else np.nan
        all_h.sort(key=lambda c: c["Start_Dist"])
        flag_line_off_road(line_off_road_ranges(xs_stations), all_h, all_v, all_g, final_vtx_df)
        final_h_df = pd.DataFrame(all_h)
        final_v_df = pd.DataFrame(all_v)
        xs_csv = os.path.join(args.outdir, f"{base}_cross_sections.csv")
        xs_prof_csv = os.path.join(args.outdir, f"{base}_cross_section_profiles.csv")
        xs_stations.to_csv(xs_csv, index=False)
        xs_profiles.to_csv(xs_prof_csv, index=False)
        logging.info(f"Saved {len(xs_stations)} cross sections: {xs_csv}")
    except Exception as e:
        logging.warning(f"Could not build cross sections: {e}")

    final_vtx_df.to_csv(vertices_csv, index=False)

    # Optional structure span report: CSV list plus one PDF page per
    # bridge/tunnel, with shaded relief from the cached 1 m DEM tiles.
    if params.get("OUT_STRUCTURE_REPORT", False):
        try:
            spans_df = build_structure_span_table(final_vtx_df)
            spans_csv = os.path.join(args.outdir, f"{base}_structure_spans.csv")
            spans_df.to_csv(spans_csv, index=False)
            logging.info(f"Saved structure span list ({len(spans_df)} span(s)): {spans_csv}")
            if not spans_df.empty:
                spans_pdf = os.path.join(args.outdir, f"{base}_structure_spans.pdf")
                render_structure_span_pdf(final_vtx_df, spans_df, spans_pdf,
                                          dem_dir=shared_cache_dir, title="RAT Structure Span Report --")
                logging.info(f"Saved structure span report: {spans_pdf}")
        except Exception as e:
            logging.warning(f"Could not build structure span report: {e}")

    # HPMS-reported IRI on the vertices, by milepost, for the dashboard
    # only. It is added after the vertices CSV is written because it is
    # not a RAT measurement.
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
        generate_interactive_profile(final_vtx_df, final_h_df, final_v_df, dashboard_html, route_norm, params=params)
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

    grades_table(all_g).to_csv(g_csv, index=False)

    # ==========================================
    # STRUCTURE SPAN REPORT
    # ==========================================
    logging.info("========================================")
    logging.info("=== STRUCTURE SPAN REPORT ===")
    logging.info("========================================")
    kinds = final_vtx_df["Structure_Kind"].to_numpy()
    tiers = final_vtx_df["Structure_Tier"].to_numpy()
    span_starts = np.flatnonzero((kinds != "None") & np.r_[True, (kinds[1:] != kinds[:-1]) | (tiers[1:] != tiers[:-1])])
    span_counts = pd.Series([f"{kinds[i]} ({tiers[i]})" for i in span_starts]).value_counts()
    if span_counts.empty:
        logging.info("No bridge or tunnel spans detected.")
    for label, count in span_counts.items():
        logging.info(f"{label}: {count}")

    if not final_v_df.empty and 'False_Valley_Warning' in final_v_df.columns:
        false_valleys = int(final_v_df['False_Valley_Warning'].sum())
        if false_valleys > 0:
            logging.warning(f"QA/QC FLAG: {false_valleys} SAG curve(s) overlap a bridge span.")
        else:
            logging.info("QA/QC: No SAG curves overlap a bridge span.")
    logging.info("========================================")

    logging.info(f"Saved vertices:   {vertices_csv}")
    logging.info(f"Saved horizontal: {h_csv}")
    logging.info(f"Saved vertical:   {v_csv}")
    logging.info(f"Saved grades:     {g_csv}")
    logging.info("Done.")

if __name__ == "__main__":
    try:
        main()
    except DemCacheUnavailable as e:
        logging.error(
            f"{e} The run was stopped. Check the connection to the DEM folder "
            f"(or use a folder on a local drive) and run it again."
        )
        raise SystemExit(1)