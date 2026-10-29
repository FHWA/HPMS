# core/rat_param_summary.py

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
RAT RUN-PARAMETER SUMMARY HELPERS
--------------------------------------------------------------------------------
ROLE: Shared helpers that show the smoothing and detection parameters a run
used on the HTML maps and dashboards and the Plan & Profile PDF.
DESCRIPTION:
build_run_param_summary() turns a build_params()-normalized params dict into
a small, display-ready structure (a "core" set always shown, a collapsible
"advanced" set, and any per-functional-system H/V smoothing overrides that
are actually in effect for this run). render_param_panel_html() turns that
structure into a small, collapsed-by-default corner panel (HTML/CSS/JS) that
can be dropped into any folium map or Plotly dashboard HTML page.
save_run_param_summary_json()/load_run_param_summary_json() let a run's
params travel as a small JSON sidecar file to a separate downstream renderer
(rat_plan_profile_report_pdf.py) that never sees the live params dict -- it
only reads CSVs by path.

Both rat_alignment_cli.py and rat_plan_profile_cli.py use these, so every
output shows the run parameters in the same format.
CREATED BY: Federal Highway Administration, Office of Highway Policy Information.
CREATED ON: 9/25/2026
"""
import datetime
import html as _html_mod
import json
import logging
import os
from typing import Dict, Optional

# --- Core parameters: always visible in the panel/cover page. ---
CORE_PARAM_SPECS = [
    ("DENSIFY_SPACING_FT",      "Densify Spacing (ft)"),
    ("H_SMOOTH_FACTOR",         "H Smooth Factor (FS1)"),
    ("V_SMOOTH_FACTOR",         "V Smooth Factor (all FS)"),
    ("H_MIN_DELTA",             "H Min Delta (deg)"),
    ("H_MIN_CURVE_LENGTH_FT",   "H Min Curve Length (ft)"),
    ("V_MIN_CURVE_LENGTH_FT",   "V Min Curve Length (ft)"),
    ("V_MIN_GRADE_CHANGE",      "V Min Grade Change (%)"),
    ("TRIM_CURVE_ENDPOINTS",    "Trim Curve Endpoints"),
    ("ENABLE_MERGE",            "Curve Merge Enabled"),
    ("MERGE_GAP_FLOOR_FT",      "Merge Gap Floor (ft)"),
    ("MERGE_GAP_PCT",           "Merge Gap (fraction of curve length)"),
]

# --- Advanced parameters: shown behind a collapsed toggle. ---
ADVANCED_PARAM_SPECS = [
    ("H_MIN_CHUNK_POINTS",       "Min Piece Length (pts)"),
    ("H_MAX_RADIUS_FT",          "H Max Radius (ft)"),
    ("H_USE_HMM_CLASSIFICATION", "H Viterbi/HMM Classification"),
    ("H_HMM_STICKINESS",         "H HMM Stickiness"),
    ("V_VC_THRESHOLD",           "V VC Threshold"),
    ("V_GAP_TOLERANCE",          "V Gap Tolerance (pts)"),
    ("V_MIN_OFFSET_FT",          "V Min Offset (ft)"),
    ("V_USE_HMM_CLASSIFICATION", "V Viterbi/HMM Classification"),
    ("V_HMM_STICKINESS",         "V HMM Stickiness"),
    ("REGRESSION_WINDOW_FT",     "Grade Regression Window (ft)"),
    ("DIP_THRESHOLD_FT",         "Structure Min Depth (ft)"),
    ("STRUCTURE_EDGE_GRADE_PCT", "Structure Edge Grade (%)"),
    ("BRIDGE_MAX_LEN_FT",        "Bridge Max Length (ft)"),
    ("TUNNEL_MAX_LEN_FT",        "Tunnel Max Length (ft)"),
    ("STRUCTURE_MATCH_TOL_FT",   "HPMS/NBI Match Tolerance (ft)"),
    ("CHUNK_JOIN_TOLERANCE_FT",  "Chunk Join Tolerance (ft)"),
    ("MAX_CHUNK_SPLIT_MILES",    "Max Chunk Split Length (mi)"),
    ("CHUNK_SPLIT_PADDING_FT",   "Chunk Split Padding (ft)"),
]

_FS_RANGE = range(2, 8)


def _fmt_value(v) -> str:
    """Formats a param value for display: booleans as Yes/No, whole-number
    floats without a trailing .0, everything else via str()."""
    if isinstance(v, bool):
        return "Yes" if v else "No"
    if isinstance(v, float):
        if v == int(v):
            return str(int(v))
        return f"{v:g}"
    if v is None:
        return "-"
    return str(v)


def build_run_param_summary(
    params: Dict,
    route_id: Optional[str] = None,
    mp_start: Optional[float] = None,
    mp_end: Optional[float] = None,
    run_date: Optional[str] = None,
) -> Dict:
    """
    Builds a display-ready summary of the parameters a run actually used.

    params is expected to be the dict returned by core.rat_core.build_params()
    (i.e. already merged with DEFAULTS and, for state/batch runs, with any
    national_smoothing_factors.json overrides), not the raw user_params
    from the GUI or a CLI, so it records what ran rather than what was
    requested.

    Returns a dict with:
        identity      -- route/state id, MP range, run date/time
        core          -- list of {key, label, value} for CORE_PARAM_SPECS
        fs_overrides  -- list of {fs, h} for the FS2-FS7 horizontal smoothing
                         factors present in params (the vertical factor is
                         national and listed under core)
        advanced      -- list of {key, label, value} for ADVANCED_PARAM_SPECS
    """
    core = [
        {"key": key, "label": label, "value": _fmt_value(params[key])}
        for key, label in CORE_PARAM_SPECS if key in params
    ]

    fs_overrides = []
    for fs in _FS_RANGE:
        h_val = params.get(f"H_SMOOTH_FACTOR_FS{fs}")
        if h_val is not None:
            fs_overrides.append({"fs": fs, "h": _fmt_value(h_val)})

    advanced = [
        {"key": key, "label": label, "value": _fmt_value(params[key])}
        for key, label in ADVANCED_PARAM_SPECS if key in params
    ]

    identity = {
        "route_id": route_id,
        "state_fips": params.get("STATE_FIPS"),
        "run_date": run_date or datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
        "mp_start": mp_start,
        "mp_end": mp_end,
    }

    return {"identity": identity, "core": core, "fs_overrides": fs_overrides, "advanced": advanced}


def save_run_param_summary_json(summary: Dict, out_path: str) -> None:
    """
    Writes a run-parameter summary to a JSON sidecar file so a separate
    downstream renderer (e.g. rat_plan_profile_report_pdf.py, which never
    sees the live params dict -- it only reads CSVs by path) can pick up
    exactly what parameters produced a given CSV set.
    """
    try:
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(summary, f, indent=2)
    except OSError as e:
        logging.warning(f"Could not write run-parameter summary JSON to {out_path}: {e}")


def load_run_param_summary_json(path: str) -> Optional[Dict]:
    """
    Reads a run-parameter summary JSON sidecar written by
    save_run_param_summary_json(). Returns None (rather than raising) if the
    file is missing or unreadable, so callers can fall back gracefully --
    this is supplementary provenance information, not required input.
    """
    if not path or not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logging.warning(f"Could not read run-parameter summary JSON at {path}: {e}")
        return None


def render_param_panel_html(summary: Dict, panel_id: str = "rat-params-panel") -> str:
    """
    Renders a small, collapsed-by-default corner panel (HTML/CSS/JS, no
    external dependencies) showing a run's identity + core parameters, with
    an "Advanced parameters" section that expands on click. Styled to match
    the existing severity-legend box used elsewhere in the suite's HTML
    outputs (white box, thin border, sans-serif, small font).

    Meant to be appended into an existing HTML page -- e.g. via
    folium.Element() for a folium map, or a string .replace("</body>", ...)
    for a Plotly-generated page. Does not cover any map/chart area by
    default since the collapsed toggle button is small and fixed-position,
    in the opposite corner from the existing severity legend.

    panel_id keeps element ids unique if a page has more than one panel.
    """
    esc = _html_mod.escape
    ident = summary.get("identity", {})

    id_lines = []
    if ident.get("route_id"):
        id_lines.append(f"<div><b>Route:</b> {esc(str(ident['route_id']))}</div>")
    if ident.get("state_fips") and str(ident.get("state_fips")) not in ("", "00", "LOCAL"):
        id_lines.append(f"<div><b>State FIPS:</b> {esc(str(ident['state_fips']))}</div>")
    if ident.get("mp_start") is not None and ident.get("mp_end") is not None:
        try:
            id_lines.append(
                f"<div><b>MP Range:</b> {float(ident['mp_start']):.3f} - {float(ident['mp_end']):.3f}</div>"
            )
        except (TypeError, ValueError):
            pass
    if ident.get("run_date"):
        id_lines.append(f"<div><b>Run Date:</b> {esc(str(ident['run_date']))}</div>")
    identity_html = "\n".join(id_lines)

    def _rows(items):
        return "\n".join(
            f"<tr><td style='padding:2px 8px 2px 0; color:#555;'>{esc(it['label'])}</td>"
            f"<td style='padding:2px 0; font-weight:bold;'>{esc(it['value'])}</td></tr>"
            for it in items
        )

    core_rows_html = _rows(summary.get("core", []))

    fs_html = ""
    fs_overrides = summary.get("fs_overrides", [])
    if fs_overrides:
        fs_body = "\n".join(
            f"<tr><td style='padding:2px 8px;'>FS{o['fs']}</td>"
            f"<td style='padding:2px 8px;'>{esc(o['h'])}</td></tr>"
            for o in fs_overrides
        )
        fs_html = f"""
        <div style="margin-top:8px; font-weight:bold; color:#2c3e50;">Horizontal Smoothing by FS</div>
        <table style="border-collapse:collapse; font-size:11px;">
            <tr><th style="text-align:left; padding:2px 8px;">FS</th>
                <th style="text-align:left; padding:2px 8px;">H</th></tr>
            {fs_body}
        </table>"""

    advanced_rows_html = _rows(summary.get("advanced", []))

    return f"""
<div id="{panel_id}" style="position: fixed; bottom: 20px; left: 10px; z-index: 9999;
     font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; font-size: 12px; color:#333;">
  <div id="{panel_id}-toggle" style="background: white; border: 2px solid #2c3e50; border-radius: 6px;
       padding: 6px 12px; cursor: pointer; box-shadow: 0 2px 6px rgba(0,0,0,0.3); color:#2c3e50; font-weight:bold;
       user-select:none; display:inline-block;">
    &#9881; Run Parameters
  </div>
  <div id="{panel_id}-body" style="display:none; background: white; border: 2px solid #2c3e50; border-radius: 6px;
       padding: 10px 12px; margin-top:4px; max-width: 340px; max-height: 420px; overflow-y:auto;
       box-shadow: 0 2px 6px rgba(0,0,0,0.3);">
    {identity_html}
    <div style="margin-top:8px; font-weight:bold; color:#2c3e50;">Core Parameters</div>
    <table style="border-collapse:collapse; font-size:11px;">
      {core_rows_html}
    </table>
    {fs_html}
    <div id="{panel_id}-adv-toggle" style="cursor:pointer; color:#2c6ea4; text-decoration:underline; margin-top:10px; user-select:none;">
      &#9656; Advanced parameters
    </div>
    <div id="{panel_id}-adv-body" style="display:none; margin-top:6px;">
      <table style="border-collapse:collapse; font-size:11px;">
        {advanced_rows_html}
      </table>
    </div>
  </div>
</div>
<script>
(function() {{
    var toggle = document.getElementById('{panel_id}-toggle');
    var body = document.getElementById('{panel_id}-body');
    if (toggle && body) {{
        toggle.addEventListener('click', function() {{
            body.style.display = (body.style.display === 'none' || body.style.display === '') ? 'block' : 'none';
        }});
    }}
    var advToggle = document.getElementById('{panel_id}-adv-toggle');
    var advBody = document.getElementById('{panel_id}-adv-body');
    if (advToggle && advBody) {{
        advToggle.addEventListener('click', function() {{
            var showing = (advBody.style.display === 'block');
            advBody.style.display = showing ? 'none' : 'block';
            advToggle.innerHTML = showing ? '&#9656; Advanced parameters' : '&#9662; Advanced parameters';
        }});
    }}
}})();
</script>
"""