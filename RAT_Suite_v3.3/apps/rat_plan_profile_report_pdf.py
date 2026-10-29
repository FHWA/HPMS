# apps/rat_plan_profile_report_pdf.py

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
RAT PLAN & PROFILE PDF RENDERER v3.3 (Matplotlib Generator)
--------------------------------------------------------------------------------
ROLE: Standalone visual engineering plan and profile sheet generator.
DESCRIPTION:
Reads pre-processed vertex and curve CSVs to generate professional, multi-page
engineering PDFs. Expects the plan/profile vertices CSV format (which uses
Dist_Ft); not compatible with alignment vertices CSVs (which use Dist_Mi).
It draws USGS basemaps (The National Map, public domain), rotates geometries so the
alignment flows left-to-right, and uses Matplotlib GridSpec to stack the Plan
(top-down) and Profile (elevation) views with full curve annotations.

The cover page lists the route, milepost range, run date and the run's
parameters, read from the *_run_parameters.json file rat_plan_profile_cli.py
writes beside its CSVs (core.rat_param_summary). Without that file the cover
page shows only the route, milepost range and date. Terrain pages and
plan-view relief come from the cached 1 m DEM tiles, and cross-section
sheets from the *_cross_sections.csv files, when present.

CREATED BY: Federal Highway Administration, Office of Highway Policy Information.
CREATED ON: 5/14/2026
"""
import os
import sys
import argparse
import math
import datetime
import traceback
import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.gridspec import GridSpec
from matplotlib.backends.backend_pdf import PdfPages
from matplotlib.patches import Rectangle
from pyproj import Transformer
try:
    import contextily as cx
except ImportError:
    cx = None

# ----------------------------
# Path bootstrap for core import (cover page and terrain helpers)
# ----------------------------
THIS_DIR = os.path.dirname(os.path.abspath(__file__))
RAT_SUITE_DIR = os.path.dirname(THIS_DIR)
if RAT_SUITE_DIR not in sys.path:
    sys.path.insert(0, RAT_SUITE_DIR)
try:
    from core.rat_param_summary import load_run_param_summary_json
except Exception:
    # The PDF can still be made from the CSVs alone; the cover page then
    # omits the run parameters.
    load_run_param_summary_json = None
try:
    from core.rat_dem_relief import (
        read_dem_window, hillshade_rgb, draw_relief, padded_lonlat_box, tiles_cover_route,
        cached_tiles_bounds, fit_box_to_aspect,
    )
except Exception as _relief_err:
    # Terrain pages need core/rat_dem_relief.py; report why they're missing.
    print(f"WARNING: terrain pages disabled -- could not load core.rat_dem_relief ({_relief_err!r}).")
    read_dem_window = None

# ===========================================================================
# CONFIGURATION -- terrain (shaded relief) from the cached 1 m DEM tiles.
# The DEM folder comes from --dem_dir, else DEM_DIR below, else the folder
# recorded by the plan/profile run in its *_run_parameters.json file.
# ===========================================================================
DEM_DIR = None
TERRAIN_OVERVIEW_PAGE = True    # whole section as shaded relief, after the cover page
TERRAIN_SECTION_PAGES = True    # extra relief pages for routes longer than one section
TERRAIN_SECTION_MILES = 5.0     # length of each section page (miles)
PLAN_VIEW_RELIEF = True         # shaded relief behind each sheet's plan view
PLAN_VIEW_RELIEF_ALPHA = 0.85   # 1.0 = opaque relief; lower lets the grid show through
TERRAIN_OVERVIEW_MAX_PX = 3000  # overview image size (longer side, pixels)
TERRAIN_MAP_ASPECT = 1.55       # width/height of the map area on a terrain page

# Cross sections (from the *_cross_sections.csv files rat_plan_profile_cli.py
# writes). Drawn with the same horizontal and vertical scale; every section
# shows the same width and height.
CROSS_SECTION_SHEETS = True     # cross-section sheets after the plan/profile sheets
XS_PER_SHEET = 4
XS_HEIGHT_FT = 50.0             # height of every section window
XS_BELOW_ROADWAY_FT = 35.0      # how much of that height lies below the (lower) roadway
XS_TOP_CLEARANCE_FT = 8.0       # least room above the higher roadway
XS_MARK_PLAN_VIEW = True        # mark cross-section stations on the plan views
PLAN_SHOW_OTHER_ROUTES = True   # draw and label the other HPMS routes around the section
                                # (other direction, ramps, crossing and frontage roads)
                                # from the *_nearby_lines.csv rat_plan_profile_cli.py writes

# Elevation strip under each profile: profile and ground elevations every
# PROFILE_ELEV_INTERVAL_FT along the route (None = off). The GUI sets this
# with --profile_elev_interval_ft.
PROFILE_ELEV_INTERVAL_FT = None
# ===========================================================================

# --- Constants ---
FEET_PER_METER = 3.28084
PAGE_LENGTH_FT = 1500.0
PLAN_HALF_HEIGHT_FT = 200.0   # plan view shows this far either side of the route's chord
PLAN_EDGE_MARGIN_FT = 15.0    # route kept at least this far inside the plan view's edges
PLAN_TICK_FT = 100.0          # milepost ticks along the plan-view centerline
PLAN_LABEL_EVERY = 5          # label every 5th tick (500 ft) with its milepost
PDF_DPI = 200
TILE_DPI = 96   # basemap zoom is chosen so tile pixels print at about screen size

# USGS The National Map tile services (public domain), Web Mercator tiles.
USGS_TOPO_TILES = "https://basemap.nationalmap.gov/arcgis/rest/services/USGSTopo/MapServer/tile/{z}/{y}/{x}"
USGS_IMAGERY_TILES = "https://basemap.nationalmap.gov/arcgis/rest/services/USGSImageryOnly/MapServer/tile/{z}/{y}/{x}"
USGS_MAX_ZOOM = 16
BASEMAP_CREDIT = "Basemap: USGS The National Map"

_TO_MERC = Transformer.from_crs("EPSG:4326", "EPSG:3857", always_xy=True)
_MERC_M_PER_PX_Z0 = 156543.03392804097   # Web Mercator ground resolution at zoom 0 (per 256-px tile pixel)


def to_web_mercator(lon, lat):
    x, y = _TO_MERC.transform(np.asarray(lon, dtype=float), np.asarray(lat, dtype=float))
    return np.asarray(x), np.asarray(y)


def fit_extent_to_aspect(x_min, x_max, y_min, y_max, width_in, height_in):
    """Grows a Web Mercator box around its center so its shape matches the
    axes it will fill, so the map fills the panel without being stretched."""
    cx_, cy_ = (x_min + x_max) / 2, (y_min + y_max) / 2
    w, h = x_max - x_min, y_max - y_min
    target = width_in / height_in
    if w / h > target:
        h = w / target
    else:
        w = h * target
    return cx_ - w / 2, cx_ + w / 2, cy_ - h / 2, cy_ + h / 2


def fetch_basemap_tiles(x_min, x_max, y_min, y_max, width_in, source):
    """
    Mosaics Web Mercator tiles covering the box. The zoom is chosen so one
    tile pixel prints at about screen size (TILE_DPI) -- basemap labels are
    designed for screens, so this keeps them legible on paper; the PDF itself
    is still saved at PDF_DPI so nothing is lost. Returns
    (image, (w, e, s, n)) or None if tiles can't be fetched.
    """
    if cx is None:
        print("WARNING: contextily not installed -- basemaps omitted.")
        return None
    target_px = max(width_in * TILE_DPI, 256)
    m_per_px = (x_max - x_min) / target_px
    zoom = int(round(math.log2(_MERC_M_PER_PX_Z0 / max(m_per_px, 1e-6))))
    zoom = max(1, min(zoom, USGS_MAX_ZOOM))
    try:
        img, ext = cx.bounds2img(x_min, y_min, x_max, y_max, zoom=zoom, source=source, ll=False)
        return img, ext
    except Exception as e:
        print(f"WARNING: basemap tiles could not be fetched ({e!r}); continuing without basemap.")
        return None


def get_utm_zone(lon, lat):
    zone = int((lon + 180) / 6) + 1
    return (32600 if lat >= 0 else 32700) + zone

def rotate_points(x_coords, y_coords, origin_x, origin_y, angle_rad):
    px = x_coords - origin_x
    py = y_coords - origin_y
    qx = px * np.cos(angle_rad) - py * np.sin(angle_rad)
    qy = px * np.sin(angle_rad) + py * np.cos(angle_rad)
    return qx, qy

def find_run_parameters_sidecar(vertices_csv: str) -> str:
    """
    Path of the *_run_parameters.json file rat_plan_profile_cli.py writes
    beside its CSVs. Its outputs share one base name ("{base}_vertices.csv",
    "{base}_run_parameters.json", ...), so the path follows from the
    vertices CSV.
    """
    d = os.path.dirname(vertices_csv)
    fname = os.path.basename(vertices_csv)
    if fname.endswith("_vertices.csv"):
        base = fname[: -len("_vertices.csv")]
        return os.path.join(d, f"{base}_run_parameters.json")
    # Fallback for an unexpected filename: swap just the extension.
    root, _ext = os.path.splitext(vertices_csv)
    return root + "_run_parameters.json"


def find_cross_section_csvs(vertices_csv: str):
    """(stations_csv, profiles_csv) written next to the vertices CSV, or
    (None, None) if the run produced none."""
    d = os.path.dirname(vertices_csv)
    fname = os.path.basename(vertices_csv)
    base = fname[: -len("_vertices.csv")] if fname.endswith("_vertices.csv") else os.path.splitext(fname)[0]
    st = os.path.join(d, f"{base}_cross_sections.csv")
    pr = os.path.join(d, f"{base}_cross_section_profiles.csv")
    if os.path.exists(st) and os.path.exists(pr):
        return st, pr
    return None, None


def find_nearby_lines_csv(vertices_csv: str):
    """The *_nearby_lines.csv written next to the vertices CSV, or None."""
    d = os.path.dirname(vertices_csv)
    fname = os.path.basename(vertices_csv)
    base = fname[: -len("_vertices.csv")] if fname.endswith("_vertices.csv") else os.path.splitext(fname)[0]
    path = os.path.join(d, f"{base}_nearby_lines.csv")
    return path if os.path.exists(path) else None


# Line styles for other HPMS routes on the plan view, by Facility_Type.
_OTHER_ROUTE_STYLE = {
    6: dict(color="#1f78b4", lw=1.6, ls="-", label="Other direction (HPMS)"),
    4: dict(color="#e66101", lw=1.2, ls="-", label="Ramp (HPMS)"),
}
_OTHER_ROUTE_DEFAULT = dict(color="#4d4d4d", lw=1.2, ls="-", label="Other route (HPMS)")


def load_other_routes(path: str, wgs_to_utm) -> list:
    """[(label, facility_type, x_ft, y_ft)] for each part of each line in a
    *_nearby_lines.csv, in the sheet's UTM feet. The label is the Route ID,
    with "(over)" or "(under)" where the line crosses the route."""
    from shapely import wkt as shapely_wkt
    out = []
    for _, row in pd.read_csv(path, dtype={"RouteId": str}).iterrows():
        try:
            g = shapely_wkt.loads(str(row["WKT"]))
        except Exception:
            continue
        parts = list(g.geoms) if g.geom_type == "MultiLineString" else [g]
        ft = pd.to_numeric(row.get("Facility_Type"), errors="coerce")
        label = str(row["RouteId"])
        crossing = row.get("Crossing")
        if isinstance(crossing, str) and crossing:
            label += f" ({crossing.lower()})"
        for part in parts:
            xy = np.asarray(part.coords)[:, :2]
            if len(xy) < 2:
                continue
            x, y = wgs_to_utm.transform(xy[:, 0], xy[:, 1])
            x, y = np.asarray(x) * FEET_PER_METER, np.asarray(y) * FEET_PER_METER
            # Densified to 10 ft so a label can sit anywhere along the line.
            s_ft = np.r_[0.0, np.cumsum(np.hypot(np.diff(x), np.diff(y)))]
            d = np.linspace(0.0, s_ft[-1], max(2, int(s_ft[-1] / 10.0) + 1))
            out.append((label, int(ft) if pd.notna(ft) else None,
                        np.interp(d, s_ft, x), np.interp(d, s_ft, y)))
    return out


def draw_other_routes(ax, routes, origin_x, origin_y, angle, y_shift, x_lim, half_h, route_x, route_y):
    """Draws the other HPMS routes on a plan view and labels each route once
    with its RouteId: the other direction a fifth of the way across the
    sheet (clear of the curve callouts), the rest at their visible point
    farthest from the route's centerline, clear of the legend and scale
    bar."""
    used_styles, labeled, placed = set(), set(), []
    # Lines with an over/under label first, so that label is the one shown.
    for label, ft, x, y in sorted(routes, key=lambda r: "(" not in r[0]):
        route_id = label.split(" (")[0]
        rx, ry = rotate_points(x, y, origin_x, origin_y, -angle)
        ry = ry - y_shift
        inside = (rx >= x_lim[0]) & (rx <= x_lim[1]) & (np.abs(ry) <= half_h)
        if not inside.any():
            continue
        style = _OTHER_ROUTE_STYLE.get(ft, _OTHER_ROUTE_DEFAULT)
        legend_label = style["label"] if style["label"] not in used_styles else None
        used_styles.add(style["label"])
        ax.plot(rx, ry, color=style["color"], lw=style["lw"], ls=style["ls"], zorder=1.5,
                label=legend_label, solid_capstyle="round")
        if route_id in labeled:
            continue
        labeled.add(route_id)
        k = np.flatnonzero(inside)
        span = x_lim[1] - x_lim[0]
        legend = (rx[k] > x_lim[1] - 0.15 * span) & (ry[k] < -0.3 * half_h)
        scale_bar = (rx[k] < x_lim[0] + 0.2 * span) & (ry[k] < -0.6 * half_h)
        margin = ((np.abs(ry[k]) <= half_h - 15) & (rx[k] >= x_lim[0] + 30) & (rx[k] <= x_lim[1] - 30)
                  & ~legend & ~scale_bar)
        k = k[margin] if margin.any() else k
        if ft == 6:
            order = k[np.argsort(np.abs(rx[k] - (x_lim[0] + 0.2 * span)))]
        else:
            gap = np.min(np.hypot(rx[k, None] - route_x[None, ::5], ry[k, None] - route_y[None, ::5]), axis=1)
            order = k[np.argsort(-gap)]
        # First candidate clear of labels already placed on this sheet.
        clear_x, clear_y = 0.08 * span, 0.08 * half_h
        j = next((c for c in order if all(abs(rx[c] - px) > clear_x or abs(ry[c] - py) > clear_y
                                          for px, py in placed)), order[0])
        placed.append((rx[j], ry[j]))
        ax.text(rx[j], ry[j], label, fontsize=6.5, color=style["color"], ha="center", va="center",
                zorder=6, clip_on=True,
                bbox=dict(boxstyle="round,pad=0.15", fc="white", ec=style["color"], lw=0.5, alpha=0.85))


def _slope_label(ax, x0, x1, z_at, slope_pct, y_top, doubtful=False):
    """Slope value with an arrow pointing downhill, above a platform
    (gray with "?" where the lane fit is doubtful)."""
    if not np.isfinite(slope_pct) or x1 - x0 < 4:
        return
    xm = (x0 + x1) / 2
    down = 1 if slope_pct < 0 else -1
    y = z_at(xm) + 2.5
    if y > y_top - 3:
        return
    color = "#888888" if doubtful else "#1f4e9c"
    ax.annotate("", xy=(xm + 4.5 * down, y), xytext=(xm - 4.5 * down, y),
                arrowprops=dict(arrowstyle="->", lw=0.9, color=color))
    ax.text(xm, y + 0.8, f"{abs(slope_pct):.1f}%" + ("?" if doubtful else ""),
            ha="center", va="bottom", fontsize=7, color=color)


def _draw_cross_section(ax, st, prof, curve_info):
    o = prof["Offset_Ft"].to_numpy(dtype=float)
    z = prof["Elev_Ft"].to_numpy(dtype=float)
    zc = float(st["Centerline_Elev_Ft"])
    levels = [float(st[k]) for k in ("Inv_Elev_Ft", "Opp_Elev_Ft") if pd.notna(st.get(k, np.nan))] or [zc]
    # Window below the lower roadway, raised where needed to keep the higher
    # one XS_TOP_CLEARANCE_FT inside the top.
    y0 = max(min(levels) - XS_BELOW_ROADWAY_FT, max(levels) + XS_TOP_CLEARANCE_FT - XS_HEIGHT_FT)
    y1 = y0 + XS_HEIGHT_FT
    zplot = np.clip(z, y0, y1)
    ax.fill_between(o, zplot, y0, where=np.isfinite(z), color="#e8e2d0", zorder=1)
    ax.plot(o, z, "-", color="#3b3b3b", lw=1.0, zorder=3)
    ax.axvline(0, color="#c0392b", lw=0.8, ls="--", zorder=2)
    ax.text(0, y1 - 0.8, " CL", color="#c0392b", fontsize=7, va="top", ha="left")
    hpms_x = -float(st["Center_Offset_Ft"])
    if abs(hpms_x) > 0.5:
        ax.plot([hpms_x], [np.interp(hpms_x, o, z) + 1.2], "v", color="#555555", ms=5, zorder=4)
    opp_x = st.get("Opp_Line_Ft", np.nan)
    if pd.notna(opp_x) and -250 <= opp_x <= 250:
        ax.plot([opp_x], [np.interp(opp_x, o, z) + 1.2], "v", mfc="none", mec="#555555", ms=5, zorder=4)

    def z_at(x):
        return float(np.interp(x, o, z))

    half = float(st.get("Lane_Half_Width_Ft", 12.0))
    for side in ("Inv", "Opp"):
        c = st.get(f"{side}_Center_Ft")
        if pd.isna(c):
            continue
        c = float(c)
        doubtful = st.get(f"{side}_Note") == "Doubtful"
        if st.get(f"{side}_Form") == "Crowned" and pd.notna(st.get(f"{side}_Crown_Ft")):
            k = float(st[f"{side}_Crown_Ft"])
            _slope_label(ax, c - half, k, z_at, float(st[f"{side}_Left_Slope_Pct"]), y1, doubtful)
            _slope_label(ax, k, c + half, z_at, float(st[f"{side}_Right_Slope_Pct"]), y1, doubtful)
        else:
            _slope_label(ax, c - half, c + half, z_at, float(st.get(f"{side}_Slope_Pct", np.nan)), y1, doubtful)

    for edge, hv, sgn in ((st.get("Left_Edge_Ft"), st.get("Foreslope_Left_HV"), -1),
                          (st.get("Right_Edge_Ft"), st.get("Foreslope_Right_HV"), 1)):
        if pd.notna(edge) and pd.notna(hv):
            x = float(edge) + sgn * 12
            ax.text(x, min(z_at(x) + 2.0, y1 - 2), f"{hv:.1f}:1", fontsize=7, color="#7a4b00",
                    ha="left" if sgn > 0 else "right", va="bottom")

    ax.set_xlim(-250, 250)
    ax.set_ylim(y0, y1)
    ax.set_aspect("equal", adjustable="box")
    ax.set_xticks(np.arange(-250, 251, 50))
    ax.set_xticks(np.arange(-250, 251, 10), minor=True)
    first = math.ceil(y0 / 10) * 10
    ax.set_yticks(np.arange(first, y1 + 0.1, 10))
    ax.grid(True, which="major", color="#bbbbbb", lw=0.5)
    ax.grid(True, which="minor", axis="x", color="#e5e5e5", lw=0.4)
    ax.tick_params(labelsize=7)

    head = f"{st['Station_ID']}   MP {st['Milepost']:.3f}   {st['Station_Type']}"
    if pd.notna(st.get("Curve_No")):
        head += f", curve {int(st['Curve_No'])}"
        if curve_info:
            head += f" ({curve_info})"
    if st.get("Curve_Super_Note") == "NC":
        head += "   curve: normal crown (NC)"
    elif pd.notna(st.get("Curve_Superelevation_Pct")):
        head += f"   curve e = {st['Curve_Superelevation_Pct']:.1f}% (median through curve)"
    if _is_true(st.get("Line_Off_Road")):
        head += "   HPMS LINE OFF ROADWAY?"
    ax.set_title(head, loc="left", fontsize=9, fontweight="bold")
    basis = st.get("Center_Basis", "")
    note = f"CL elev {zc:.1f} ft   CL from {basis}"
    if abs(hpms_x) > 0.5:
        note += f";  HPMS line (\u25bc) {abs(hpms_x):.0f} ft {'right' if hpms_x > 0 else 'left'} of CL"
    if pd.notna(opp_x):
        note += f";  opposing HPMS line (\u25bd) {abs(opp_x):.0f} ft {'right' if opp_x > 0 else 'left'}"
    ax.set_title(note, loc="right", fontsize=7.5, color="#444444")


def add_cross_section_sheets(pdf, stations, profiles, route_id, hc, date_str):
    """Cross-section sheets, XS_PER_SHEET sections per sheet, in milepost order."""
    curve_info = {}
    if hc is not None and not hc.empty and "Curve_No" in hc.columns:
        for _, c in hc.iterrows():
            r = c.get("Min_Radius_m", c.get("Radius_m", np.nan)) * FEET_PER_METER
            curve_info[int(c["Curve_No"])] = f"{c.get('Dir', '')}, R {r:,.0f} ft"
    groups = {sid: g for sid, g in profiles.groupby("Station_ID")}
    stations = stations.sort_values("Dist_Ft").reset_index(drop=True)
    n_sheets = int(math.ceil(len(stations) / XS_PER_SHEET))
    for k in range(n_sheets):
        chunk = stations.iloc[k * XS_PER_SHEET:(k + 1) * XS_PER_SHEET]
        fig = plt.figure(figsize=(17, 11))
        fig.add_artist(Rectangle((0.02, 0.02), 0.96, 0.96, linewidth=2, edgecolor='black',
                                 facecolor='none', transform=fig.transFigure))
        fig.text(0.05, 0.94, f"CROSS SECTIONS: {route_id}", fontsize=14, fontweight='bold')
        fig.text(0.05, 0.915, "Bare-earth ground from the USGS 3DEP 1 m DEM, looking in the direction of "
                 "increasing milepost. Horizontal and vertical scales are equal; offsets in feet from the "
                 "roadway centerline (CL).", fontsize=8.5, color="#444444")
        fig.text(0.85, 0.05, f"XS SHEET {k + 1} OF {n_sheets}", fontsize=14, fontweight='bold')
        fig.text(0.85, 0.035, f"DATE: {date_str}", fontsize=10)
        fig.text(0.05, 0.04, f"Lane slopes: % with arrow pointing downhill (two per roadway where crowned; gray with ? where the opposing "
                 f"lanes may be side slope).  Foreslopes: horizontal:vertical.  "
                 f"Each section is {XS_HEIGHT_FT:.0f} ft tall; ground outside that window is clipped.",
                 fontsize=8, color="#444444")
        slot_h = 0.80 / XS_PER_SHEET
        for i, (_, st) in enumerate(chunk.iterrows()):
            ax = fig.add_axes([0.07, 0.86 - (i + 1) * slot_h + 0.02, 0.88, slot_h - 0.05])
            prof = groups.get(st["Station_ID"])
            if prof is None or prof.empty:
                ax.set_axis_off()
                continue
            _draw_cross_section(ax, st, prof, curve_info.get(int(st["Curve_No"])) if pd.notna(st.get("Curve_No")) else None)
        pdf.savefig(fig, dpi=PDF_DPI)
        plt.close(fig)
    return n_sheets


def add_cover_page(pdf: PdfPages, route_id: str, mp_start: float, mp_end: float,
                    num_pages: int, date_str: str, param_summary: dict = None,
                    overview_lon: np.ndarray = None, overview_lat: np.ndarray = None) -> None:
    """
    Cover page (first page, landscape): route, milepost range, run date,
    sheet count and, when available, the run's parameters on the left; an
    overview map of the route on the right. The sheets that follow then
    need not repeat the settings.

    param_summary is the dict from
    core.rat_param_summary.build_run_param_summary() (identity/core/
    fs_overrides/advanced). If None (no run-parameters file found), the
    cover page shows only the route, milepost range, date and sheet count.

    overview_lon/overview_lat is the route centerline for the right-hand
    overview map, drawn over the USGS Topo basemap in Web Mercator. If the
    basemap can't be fetched the centerline is drawn alone; if there is no
    centerline a placeholder is shown.
    """
    fig = plt.figure(figsize=(17, 11))
    fig.patch.set_facecolor("white")

    rect = Rectangle((0.02, 0.02), 0.96, 0.96, linewidth=2, edgecolor='black',
                      facecolor='none', transform=fig.transFigure)
    fig.add_artist(rect)

    fig.text(0.5, 0.94, "RAT PLAN & PROFILE REPORT", fontsize=22, fontweight='bold', ha='center')
    fig.text(0.5, 0.905, "Roadway Alignment Tool (RAT) Suite", fontsize=12, ha='center', color='#555555')

    # ------------------------------------------------------------------
    # LEFT HALF: route and run parameters
    # ------------------------------------------------------------------
    LX = 0.05        # left text column start
    LABEL_W = 0.15   # width reserved for a label before its value
    y = 0.85
    fig.text(LX, y, "Route:", fontsize=13, fontweight='bold'); fig.text(LX + LABEL_W, y, str(route_id), fontsize=13)
    y -= 0.032
    fig.text(LX, y, "Milepost Range:", fontsize=13, fontweight='bold')
    fig.text(LX + LABEL_W, y, f"{mp_start:.3f} to {mp_end:.3f}", fontsize=13)
    y -= 0.032
    fig.text(LX, y, "Run Date:", fontsize=13, fontweight='bold'); fig.text(LX + LABEL_W, y, str(date_str), fontsize=13)
    y -= 0.032
    fig.text(LX, y, "Total Sheets:", fontsize=13, fontweight='bold'); fig.text(LX + LABEL_W, y, str(num_pages), fontsize=13)

    y -= 0.045
    fig.text(LX, y, "-" * 62, fontsize=9, color='#999999')

    if param_summary:
        core_items = param_summary.get("core", [])
        adv_items = param_summary.get("advanced", [])
        fs_overrides = param_summary.get("fs_overrides", [])

        y -= 0.04
        fig.text(LX, y, "Run Parameters", fontsize=15, fontweight='bold')
        y -= 0.032
        fig.text(LX, y, "Core Parameters", fontsize=12, fontweight='bold', color='#2c3e50')
        y -= 0.03

        # Two columns, kept within the left half so nothing runs into the map.
        col_x = [LX, LX + 0.235]
        col = 0
        for item in core_items:
            fig.text(col_x[col], y, f"{item['label']}:", fontsize=9, color='#444444')
            fig.text(col_x[col] + 0.15, y, item['value'], fontsize=9, fontweight='bold')
            if col == 0:
                col = 1
            else:
                col = 0
                y -= 0.024
        if col == 1:
            y -= 0.024

        if fs_overrides:
            y -= 0.022
            fig.text(LX, y, "Horizontal Smoothing by FS", fontsize=12,
                     fontweight='bold', color='#2c3e50')
            y -= 0.028
            fig.text(LX, y, "FS", fontsize=9, fontweight='bold')
            fig.text(LX + 0.08, y, "H Factor", fontsize=9, fontweight='bold')
            y -= 0.022
            for o in fs_overrides:
                fig.text(LX, y, f"FS{o['fs']}", fontsize=9)
                fig.text(LX + 0.08, y, o['h'], fontsize=9)
                y -= 0.022

        if adv_items:
            y -= 0.022
            fig.text(LX, y, "Advanced Parameters", fontsize=12, fontweight='bold', color='#2c3e50')
            y -= 0.028
            col = 0
            for item in adv_items:
                if y < 0.06:
                    # Out of room on the page; say so rather than drop them silently.
                    fig.text(LX, y, "(additional parameters omitted -- see run_parameters.json)",
                             fontsize=8, style='italic', color='#888888')
                    break
                fig.text(col_x[col], y, f"{item['label']}:", fontsize=8.5, color='#444444')
                fig.text(col_x[col] + 0.15, y, item['value'], fontsize=8.5, fontweight='bold')
                if col == 0:
                    col = 1
                else:
                    col = 0
                    y -= 0.022
    else:
        y -= 0.04
        fig.text(LX, y, "Run parameters unavailable for this CSV set", fontsize=10,
                 style='italic', color='#888888')
        fig.text(LX, y - 0.026,
                 "(no *_run_parameters.json file found next to the input", fontsize=8.5, color='#888888')
        fig.text(LX, y - 0.048,
                 "CSVs).", fontsize=8.5, color='#888888')

    fig.text(0.27, 0.045, "FHWA Office of Highway Policy Information", fontsize=8, ha='center', color='#999999')

    # ------------------------------------------------------------------
    # RIGHT HALF: overview map of the whole route in this PDF
    # ------------------------------------------------------------------
    fig.text(0.75, 0.87, "Route Overview", fontsize=13, fontweight='bold', ha='center')
    map_rect = [0.55, 0.10, 0.40, 0.74]
    ax_map = fig.add_axes(map_rect)
    panel_w_in = map_rect[2] * fig.get_figwidth()
    panel_h_in = map_rect[3] * fig.get_figheight()

    def _placeholder():
        ax_map.clear()
        ax_map.text(0.5, 0.5, "Route overview unavailable", ha='center', va='center',
                    fontsize=10, style='italic', color='#888888', transform=ax_map.transAxes)
        ax_map.set_xticks([]); ax_map.set_yticks([])
        for spine in ax_map.spines.values():
            spine.set_edgecolor('#cccccc')

    try:
        if overview_lon is not None and overview_lat is not None and len(overview_lon) >= 2:
            mx, my = to_web_mercator(overview_lon, overview_lat)
            pad = max(mx.max() - mx.min(), my.max() - my.min()) * 0.08 + 500.0
            x0, x1, y0, y1 = fit_extent_to_aspect(mx.min() - pad, mx.max() + pad,
                                                  my.min() - pad, my.max() + pad,
                                                  panel_w_in, panel_h_in)
            tiles = fetch_basemap_tiles(x0, x1, y0, y1, panel_w_in, USGS_TOPO_TILES)
            if tiles is not None:
                img, (bw, be, bs, bn) = tiles
                ax_map.imshow(img, extent=(bw, be, bs, bn), interpolation='lanczos', zorder=0)
                ax_map.text(0.995, 0.005, BASEMAP_CREDIT, transform=ax_map.transAxes, fontsize=6,
                            ha='right', va='bottom', color='#333333',
                            bbox=dict(boxstyle="square,pad=0.2", fc="white", ec="none", alpha=0.7))

            ax_map.plot(mx, my, color='#1f5fd1', linewidth=2.2, zorder=2)
            ax_map.scatter([mx[0]], [my[0]], color='#1a9641', s=45, zorder=3,
                           edgecolor='black', linewidth=0.6, label=f"Start (RP {mp_start:.3f})")
            ax_map.scatter([mx[-1]], [my[-1]], color='#d7191c', s=45, zorder=3,
                           edgecolor='black', linewidth=0.6, label=f"End (RP {mp_end:.3f})")
            ax_map.legend(loc='upper right', fontsize=7, framealpha=0.9)
            ax_map.set_xlim(x0, x1)
            ax_map.set_ylim(y0, y1)
            ax_map.set_aspect('equal')
            ax_map.set_xticks([]); ax_map.set_yticks([])
            for spine in ax_map.spines.values():
                spine.set_edgecolor('#999999')
        else:
            _placeholder()
    except Exception as e:
        # The overview map is never a reason to lose the rest of the document.
        print(f"WARNING: cover-page overview map failed to render ({e!r}); "
              f"using placeholder instead.")
        _placeholder()

    pdf.savefig(fig, dpi=PDF_DPI)
    plt.close(fig)


def resolve_tile_dir(dem_dir):
    """Accepts either the folder holding tile_1m_*.tif files or its parent
    DEM cache folder (which holds align_1m_cache)."""
    if not dem_dir:
        print("NOTE: no DEM folder found (--dem_dir, DEM_DIR, or the run-parameters file); terrain pages omitted.")
        return None
    if not os.path.isdir(dem_dir):
        print(f"WARNING: DEM folder not found: {dem_dir}; terrain pages omitted.")
        return None
    sub = os.path.join(dem_dir, "align_1m_cache")
    if os.path.isdir(sub):
        return sub
    return dem_dir


def _milepost_ticks(mps, max_ticks=12):
    """Round milepost values to label along a route, at a sensible interval."""
    lo, hi = float(np.nanmin(mps)), float(np.nanmax(mps))
    for step in (0.1, 0.25, 0.5, 1, 2, 5, 10, 20, 50, 100):
        first = math.ceil(lo / step) * step
        ticks = np.arange(first, hi + 1e-9, step)
        if len(ticks) <= max_ticks:
            return ticks
    return np.array([lo, hi])


def _scale_bar(ax, extent, mid_lat):
    """Simple scale bar in the lower-left corner of a lon/lat map."""
    min_lon, max_lon, min_lat, max_lat = extent
    k = max(math.cos(math.radians(mid_lat)), 0.15)
    width_mi = (max_lon - min_lon) * 111320.0 * k / 1609.344
    for length_mi, label in ((0.1, "500 ft"), (0.25, "0.25 mi"), (0.5, "0.5 mi"), (1, "1 mi"),
                             (2, "2 mi"), (5, "5 mi"), (10, "10 mi"), (20, "20 mi")):
        if length_mi >= width_mi / 8:
            break
    if label == "500 ft":
        length_mi = 500 / 5280.0
    dlon = length_mi * 1609.344 / (111320.0 * k)
    x0 = min_lon + (max_lon - min_lon) * 0.04
    y0 = min_lat + (max_lat - min_lat) * 0.04
    ax.plot([x0, x0 + dlon], [y0, y0], color="black", lw=4, solid_capstyle="butt", zorder=6)
    ax.plot([x0, x0 + dlon], [y0, y0], color="white", lw=2, solid_capstyle="butt", zorder=7)
    ax.text(x0 + dlon / 2, y0 + (max_lat - min_lat) * 0.012, label, ha="center", va="bottom",
            fontsize=8, zorder=7, bbox=dict(boxstyle="round,pad=0.15", fc="white", ec="none", alpha=0.7))


def terrain_map_box(tile_dir, lons, lats):
    """Area shown on a terrain page: the route plus padding, grown toward the
    page's shape but only as far as the cached tiles reach (beyond them there
    is no elevation to show)."""
    box = padded_lonlat_box(lons, lats)
    return fit_box_to_aspect(box, TERRAIN_MAP_ASPECT, limit=cached_tiles_bounds(tile_dir, lons, lats))


def add_terrain_page(pdf, title, subtitle, lons, lats, mps, kinds, tile_dir,
                     max_px, sections=None):
    """
    One landscape page of shaded relief from the cached 1 m tiles, with the
    route, milepost labels, start/end markers, bridge/tunnel spans and
    (optionally) outlines of the section pages that follow.
    Returns False if no cached tile covers the area.
    """
    box = terrain_map_box(tile_dir, lons, lats)
    relief = read_dem_window(tile_dir, *box, max_px=max_px)
    if relief is None:
        print(f"WARNING: no cached DEM tiles for terrain page '{subtitle}'; page skipped.")
        return False
    elev_m, extent = relief

    fig = plt.figure(figsize=(17, 11))
    fig.add_artist(Rectangle((0.02, 0.02), 0.96, 0.96, linewidth=2, edgecolor='black',
                             facecolor='none', transform=fig.transFigure))
    fig.text(0.05, 0.94, title, fontsize=16, fontweight='bold')
    fig.text(0.05, 0.915, subtitle, fontsize=11, color='#444444')

    ax = fig.add_axes([0.05, 0.07, 0.82, 0.82])
    mappable = draw_relief(ax, elev_m, extent)
    cax = fig.add_axes([0.895, 0.20, 0.012, 0.55])
    cb = fig.colorbar(mappable, cax=cax)
    cb.set_label("Elevation (ft)")

    ax.plot(lons, lats, '-', color='black', lw=1.8, zorder=3, label="Route centerline")
    if kinds is not None:
        on = kinds != "None"
        if on.any():
            ax.plot(np.where(on, lons, np.nan), np.where(on, lats, np.nan), '-', color='#ff2fd0',
                    lw=4, zorder=4, label="Bridge / tunnel span")
    ax.scatter([lons[0]], [lats[0]], color='#1a9641', s=60, zorder=5, edgecolor='black',
               label=f"Start (RP {mps[0]:.3f})")
    ax.scatter([lons[-1]], [lats[-1]], color='#d7191c', s=60, zorder=5, edgecolor='black',
               label=f"End (RP {mps[-1]:.3f})")

    for t in _milepost_ticks(mps):
        i = int(np.argmin(np.abs(mps - t)))
        ax.plot(lons[i], lats[i], 'o', color='white', ms=5, mec='black', zorder=5)
        ax.annotate(f"{t:g}", (lons[i], lats[i]), xytext=(5, 5), textcoords='offset points',
                    fontsize=8, zorder=6,
                    bbox=dict(boxstyle="round,pad=0.15", fc="white", ec="none", alpha=0.75))

    if sections:
        for label, (b_min_lon, b_min_lat, b_max_lon, b_max_lat) in sections:
            ax.add_patch(Rectangle((b_min_lon, b_min_lat), b_max_lon - b_min_lon, b_max_lat - b_min_lat,
                                   fill=False, ls='--', lw=1.0, ec='#333333', zorder=4))
            ax.text(b_min_lon, b_max_lat, f" {label}", fontsize=8, va='top', zorder=6,
                    bbox=dict(boxstyle="round,pad=0.1", fc="white", ec="none", alpha=0.7))

    _scale_bar(ax, extent, float(np.mean(lats)))
    ax.set_xlim(extent[0], extent[1])
    ax.set_ylim(extent[2], extent[3])
    ax.set_xticks([]); ax.set_yticks([])
    # Legend in the white space at the lower right of the sheet, below the
    # elevation scale, so it does not cover the map.
    handles, labels = ax.get_legend_handles_labels()
    fig.legend(handles, labels, loc='lower right', bbox_to_anchor=(0.975, 0.035),
               fontsize=7.5, frameon=False, borderaxespad=0.0)
    ax.text(0.995, 0.005, "Shaded relief: USGS 3DEP 1 m DEM (north up)", transform=ax.transAxes,
            fontsize=7, ha='right', va='bottom',
            bbox=dict(boxstyle="square,pad=0.2", fc="white", ec="none", alpha=0.7))

    pdf.savefig(fig, dpi=PDF_DPI)
    plt.close(fig)
    return True


def fit_plan_view(sub_x, sub_y):
    """
    Rotation for a sheet's plan view. Normally the line from the sheet's
    first point to its last runs left to right along offset 0. When a sharp
    bend takes the route beyond PLAN_HALF_HEIGHT_FT of that line, the sheet
    is instead turned to the direction in which the route is narrowest and
    centered on it (plan_x_limits then widens the view if it still doesn't
    fit). Returns (angle, y_shift_ft); plotted offset = rotated offset -
    y_shift_ft.
    """
    x0, y0 = sub_x[0], sub_y[0]
    chord = math.atan2(sub_y[-1] - y0, sub_x[-1] - x0)
    _, ry = rotate_points(sub_x, sub_y, x0, y0, -chord)
    limit = PLAN_HALF_HEIGHT_FT - PLAN_EDGE_MARGIN_FT
    if np.max(np.abs(ry)) <= limit:
        return chord, 0.0

    step = max(1, len(sub_x) // 400)
    xs, ys = sub_x[::step], sub_y[::step]
    best = None
    for deg in np.arange(0.0, 180.0, 0.5):
        a = math.radians(deg)
        _, r = rotate_points(xs, ys, x0, y0, -a)
        width = float(r.max() - r.min())
        if best is None or width < best[0]:
            best = (width, a)
    angle = best[1]
    rx, ry = rotate_points(sub_x, sub_y, x0, y0, -angle)
    if rx[-1] < rx[0]:
        angle += math.pi
        rx, ry = rotate_points(sub_x, sub_y, x0, y0, -angle)
    y_shift = float((ry.max() + ry.min()) / 2.0)
    return angle, y_shift


def draw_plan_view_relief(ax, tile_dir, origin_x_ft, origin_y_ft, angle, utm_to_wgs,
                          nx=1400, y_shift_ft=0.0):
    """
    Shaded relief behind a sheet's plan view. The plan view is the route at
    true scale, rotated about its first point and shifted by y_shift_ft, so
    each pixel of the axes is mapped back to ground coordinates and sampled
    from the 1 m tiles. Returns True if drawn.
    """
    from scipy.ndimage import map_coordinates

    x_lim, y_lim = ax.get_xlim(), ax.get_ylim()
    ny = max(80, int(nx * abs(y_lim[1] - y_lim[0]) / abs(x_lim[1] - x_lim[0])))
    gx_ft = np.linspace(x_lim[0], x_lim[1], nx)
    gy_ft = np.linspace(y_lim[1], y_lim[0], ny)          # top row first

    rx, ry = np.meshgrid(gx_ft, gy_ft + y_shift_ft)
    x_ft = origin_x_ft + rx * math.cos(angle) - ry * math.sin(angle)
    y_ft = origin_y_ft + rx * math.sin(angle) + ry * math.cos(angle)
    lon, lat = utm_to_wgs.transform(x_ft / FEET_PER_METER, y_ft / FEET_PER_METER)
    lon, lat = np.asarray(lon), np.asarray(lat)

    pad = 1e-4
    relief = read_dem_window(tile_dir, lon.min() - pad, lat.min() - pad, lon.max() + pad, lat.max() + pad)
    if relief is None:
        return False
    elev, (e_min_lon, e_max_lon, e_min_lat, e_max_lat) = relief
    h, w = elev.shape
    col = (lon - e_min_lon) / (e_max_lon - e_min_lon) * w - 0.5
    row = (e_max_lat - lat) / (e_max_lat - e_min_lat) * h - 0.5
    z = map_coordinates(elev, [row, col], order=1, mode='nearest')
    outside = (row < -0.5) | (row > h - 0.5) | (col < -0.5) | (col > w - 0.5)
    z = np.where(outside, np.nan, z)
    if not np.isfinite(z).any():
        return False

    dx_m = abs(x_lim[1] - x_lim[0]) / (nx - 1) / FEET_PER_METER
    dy_m = abs(y_lim[1] - y_lim[0]) / (ny - 1) / FEET_PER_METER
    rgba, _ = hillshade_rgb(z * FEET_PER_METER, dx_m, dy_m)
    rgba[..., 3] *= PLAN_VIEW_RELIEF_ALPHA
    ax.imshow(rgba, extent=(x_lim[0], x_lim[1], y_lim[0], y_lim[1]), origin='upper',
              aspect='auto', interpolation='bilinear', zorder=0)
    ax.set_xlim(x_lim); ax.set_ylim(y_lim)
    return True


def plan_x_limits(rot_x, rot_y, aspect, aligned=True):
    """Horizontal limits (ft) of a sheet's true-scale plan view. Normally
    0 to PAGE_LENGTH_FT, so the plan lines up with the profile below. A sheet
    turned for a sharp bend (aligned=False), or one whose route doesn't fit,
    is centered on the route and widened as needed."""
    m = PLAN_EDGE_MARGIN_FT
    x_lo, x_hi = float(np.min(rot_x)), float(np.max(rot_x))
    need_h = 2.0 * (float(np.max(np.abs(rot_y))) + m)
    width = max(PAGE_LENGTH_FT, x_hi - x_lo + 2 * m, need_h * aspect)
    if aligned and width == PAGE_LENGTH_FT and x_lo >= -m and x_hi <= PAGE_LENGTH_FT + m:
        return 0.0, PAGE_LENGTH_FT
    mid = (x_lo + x_hi) / 2.0
    return mid - width / 2.0, mid + width / 2.0


def _right_normal(rx, ry, j, reach=5):
    """Unit vector to the right of travel at point j of a plan-view line."""
    a, b = max(j - reach, 0), min(j + reach, len(rx) - 1)
    tx, ty = rx[b] - rx[a], ry[b] - ry[a]
    tl = math.hypot(tx, ty) or 1.0
    return ty / tl, -tx / tl


def draw_milepost_ticks(ax, rot_x, rot_y, sub_dist, sub_mp, dist_origin):
    """Ticks across the centerline every PLAN_TICK_FT (counted from the route
    start), with the milepost written at every PLAN_LABEL_EVERY-th tick."""
    first = math.ceil((sub_dist[0] - dist_origin) / PLAN_TICK_FT)
    last = math.floor((sub_dist[-1] - dist_origin) / PLAN_TICK_FT)
    for k in range(first, last + 1):
        d = dist_origin + k * PLAN_TICK_FT
        j = int(np.clip(np.searchsorted(sub_dist, d), 0, len(sub_dist) - 1))
        x, y = rot_x[j], rot_y[j]
        nx_, ny_ = _right_normal(rot_x, rot_y, j)
        major = k % PLAN_LABEL_EVERY == 0
        t = 14.0 if major else 7.0
        ax.plot([x - t * nx_, x + t * nx_], [y - t * ny_, y + t * ny_], color='black',
                lw=1.0 if major else 0.7, zorder=4, solid_capstyle='butt')
        if major:
            mp = float(np.interp(d, sub_dist, sub_mp))
            ax.text(x - 34 * nx_, y - 34 * ny_, f"{mp:.3f}", fontsize=7, ha='center', va='center',
                    zorder=6, clip_on=True,
                    bbox=dict(boxstyle="round,pad=0.12", fc="white", ec="none", alpha=0.75))


def _is_true(val) -> bool:
    """True for True / 'True' as read back from a CSV."""
    return str(val).strip().lower() == "true"


def off_road_line(row) -> str:
    """Leading callout line where the HPMS line appears to be off the road."""
    return "LINE OFF ROAD?\n" if _is_true(row.get("Line_Off_Road")) else ""


def vc_class_line(v) -> str:
    """'  Class B' for a vertical curve row, or '' if the CSV has no class."""
    cls = v.get('VC_Class')
    return f"  Class {cls}" if isinstance(cls, str) and cls else ""


def vc_ssd_line(v) -> str:
    """Sight distance the curve provides and the design speed it supports."""
    ssd = pd.to_numeric(v.get('SSD_Provided_Ft'), errors='coerce')
    speed = pd.to_numeric(v.get('Design_Speed_Supported_Mph'), errors='coerce')
    if pd.isna(speed):
        return ""
    if pd.isna(ssd):
        return "\nSSD not limited by curve"
    speed_txt = "<15 mph" if speed == 0 else ("80+ mph" if speed >= 80 else f"{speed:.0f} mph")
    return f"\nSSD={ssd:.0f}' ({speed_txt})"


def place_callouts(fig, ax, callouts, obstacles=(), line=None):
    """Draws curve callouts (text, (x, y) anchor in data units, preferred
    side +1 above / -1 below) inside ax, each placed at the first of a
    series of offsets above and below its anchor (then to either side) that
    keeps the box clear of the obstacles (artists: legend, inset map), of
    callouts already placed, of the anchor itself and, as far as possible,
    of the line (x, y arrays in data units) and other text on the axes.
    Boxes are shifted to stay within the axes."""
    if not callouts:
        return
    from matplotlib.text import Text
    fig.canvas.draw()
    renderer = fig.canvas.get_renderer()
    to_pt = 72.0 / fig.dpi
    frame = ax.get_window_extent(renderer)
    margin = 4.0
    hard = [o.get_window_extent(renderer) for o in obstacles if o is not None]
    soft = [t.get_window_extent(renderer) for t in ax.texts if t.get_visible() and t.get_text().strip()]
    line_px = ax.transData.transform(np.c_[line[0], line[1]]) if line is not None else np.empty((0, 2))

    def overlap(a, b):
        w = min(a.x1, b.x1) - max(a.x0, b.x0)
        h = min(a.y1, b.y1) - max(a.y0, b.y0)
        return w * h if w > 0 and h > 0 else 0.0

    for text, (x, y), side in sorted(callouts, key=lambda c: c[1][0]):
        ann = ax.annotate(text, (x, y), xytext=(0, 0), textcoords='offset points', ha='center',
                          arrowprops=dict(arrowstyle="->"), bbox=dict(boxstyle="round", fc="white"),
                          zorder=7, annotation_clip=False)
        anchor = ax.transData.transform((x, y))
        best = None
        rises = [side * d for d in (30, 55, 80, 110, 140)] + [-side * d for d in (30, 55, 80, 110, 140)]
        offsets = [(dx, dy) for dx in (0, -120, 120, -240, 240, -360, 360) for dy in rises]
        for k, (dx, dy) in enumerate(offsets):
            ann.set_va('bottom' if dy > 0 else 'top')
            ann.xyann = (dx, dy)
            ann.update_positions(renderer)
            box = Text.get_window_extent(ann, renderer).padded(6)
            dx_px = max(frame.x0 + margin - box.x0, 0.0) - max(box.x1 - (frame.x1 - margin), 0.0)
            dy_px = max(frame.y0 + margin - box.y0, 0.0) - max(box.y1 - (frame.y1 - margin), 0.0)
            box = box.translated(dx_px, dy_px)
            covers_anchor = (box.x0 - 4 <= anchor[0] <= box.x1 + 4) and (box.y0 - 4 <= anchor[1] <= box.y1 + 4)
            # Penalties: covering the anchor, then overlapping obstacles or
            # placed callouts (px^2), then other text and the line, then distance.
            cost = 1e9 * covers_anchor + 100.0 * sum(overlap(box, h) for h in hard)
            cost += sum(overlap(box, t) for t in soft)
            if len(line_px):
                inside = ((line_px[:, 0] >= box.x0) & (line_px[:, 0] <= box.x1)
                          & (line_px[:, 1] >= box.y0) & (line_px[:, 1] <= box.y1))
                cost += 2000.0 * inside.mean()
            cost += 50.0 * k
            if best is None or cost < best[0]:
                best = (cost, (dx + dx_px * to_pt, dy + dy_px * to_pt), box, ann.get_va())
        _, xyann, box, va = best
        ann.set_va(va)
        ann.xyann = xyann
        hard.append(box)


def _plan_scale_bar(ax, x0, x1, half_h):
    """Scale bar in the lower-left corner of a true-scale plan view."""
    width = x1 - x0
    length = next((L for L in (500, 200, 100, 50) if L <= width / 6), 50)
    bx = x0 + 0.02 * width
    by = -half_h + 0.10 * half_h
    ax.plot([bx, bx + length], [by, by], color="black", lw=4, solid_capstyle="butt", zorder=6)
    ax.plot([bx, bx + length / 2], [by, by], color="white", lw=2, solid_capstyle="butt", zorder=7)
    ax.text(bx + length / 2, by + 0.06 * half_h, f"{length} ft", ha="center", va="bottom", fontsize=7.5,
            zorder=7, bbox=dict(boxstyle="round,pad=0.15", fc="white", ec="none", alpha=0.75))


def _north_arrow(ax, angle, x0, x1, half_h):
    """North arrow in the upper-left corner; the sheet is rotated by angle."""
    vx, vy = math.sin(angle), math.cos(angle)
    L = 0.45 * half_h
    cx, cy = x0 + 0.035 * (x1 - x0), 0.45 * half_h
    ax.annotate("", xy=(cx + vx * L / 2, cy + vy * L / 2), xytext=(cx - vx * L / 2, cy - vy * L / 2),
                arrowprops=dict(arrowstyle="-|>", lw=1.4, color="black"), zorder=7)
    ax.text(cx + vx * (L / 2 + 0.12 * half_h), cy + vy * (L / 2 + 0.12 * half_h), "N", fontsize=9,
            fontweight="bold", ha="center", va="center", zorder=7)


def draw_elevation_strip(ax_strip, ax_prof, sub_dist, sub_mp, sub_elev, sub_raw_elev,
                         dist_origin, interval_ft):
    """Profile and ground elevations every interval_ft (measured from the
    route start) in a band under the profile view, with a light line marking
    each station on the profile."""
    first = dist_origin + math.ceil((sub_dist[0] - dist_origin) / interval_ft) * interval_ft
    stations = np.arange(first, sub_dist[-1] + 1e-6, interval_ft)
    ax_strip.set_ylim(0, 1)
    ax_strip.set_yticks([0.75, 0.25])
    ax_strip.set_yticklabels(["Profile", "Ground"], fontsize=7.5)
    ax_strip.tick_params(axis="y", length=0)
    ax_strip.axhline(0.5, color="#bbbbbb", lw=0.6)
    x0, x1 = ax_prof.get_xlim()
    for d in stations:
        mp = float(np.interp(d, sub_dist, sub_mp))
        if mp - x0 < 0.012 * (x1 - x0):
            continue
        grade = float(np.interp(d, sub_dist, sub_elev))
        ground = float(np.interp(d, sub_dist, sub_raw_elev))
        ax_prof.axvline(mp, color="#9e9e9e", lw=0.4, alpha=0.6, zorder=0)
        ax_strip.axvline(mp, color="#dddddd", lw=0.4, zorder=0)
        ax_strip.text(mp, 0.75, f"{grade:.2f}", rotation=90, ha="center", va="center", fontsize=6.5)
        ax_strip.text(mp, 0.25, f"{ground:.2f}", rotation=90, ha="center", va="center", fontsize=6.5,
                      color="#c0392b")


def make_pdf(vertices_csv, horizontal_csv, vertical_csv, pdf_out, route_id, dem_dir=None,
             profile_elev_interval_ft=None):
    vtx = pd.read_csv(vertices_csv)
    hc = pd.read_csv(horizontal_csv) if os.path.exists(horizontal_csv) else pd.DataFrame()
    vc = pd.read_csv(vertical_csv) if os.path.exists(vertical_csv) else pd.DataFrame()

    required = {"Dist_Ft", "Elev_Ft", "Milepost", "Lon", "Lat", "Raw_Lon", "Raw_Lat", "Elev_Raw_Ft"}
    missing = required - set(vtx.columns)
    if missing:
        raise ValueError(f"Vertices CSV missing required columns: {sorted(missing)}")

    lon_start, lat_start = vtx['Lon'].iloc[0], vtx['Lat'].iloc[0]
    utm = get_utm_zone(lon_start, lat_start)
    trans = Transformer.from_crs("EPSG:4326", f"EPSG:{utm}", always_xy=True)
    utm_to_wgs = Transformer.from_crs(f"EPSG:{utm}", "EPSG:4326", always_xy=True)

    utm_coords = np.array([trans.transform(lon, lat) for lon, lat in zip(vtx['Lon'], vtx['Lat'])])
    x_ft = utm_coords[:, 0] * FEET_PER_METER
    y_ft = utm_coords[:, 1] * FEET_PER_METER

    raw_utm_coords = np.array([trans.transform(lon, lat) for lon, lat in zip(vtx['Raw_Lon'], vtx['Raw_Lat'])])
    raw_x_ft = raw_utm_coords[:, 0] * FEET_PER_METER
    raw_y_ft = raw_utm_coords[:, 1] * FEET_PER_METER
    raw_elev_ft = vtx["Elev_Raw_Ft"].to_numpy()

    dist_ft = vtx["Dist_Ft"].to_numpy()
    elev_ft = vtx["Elev_Ft"].to_numpy()
    mp_array = vtx["Milepost"].to_numpy()

    elev_interval = profile_elev_interval_ft or PROFILE_ELEV_INTERVAL_FT
    if elev_interval is not None and elev_interval <= 0:
        elev_interval = None

    other_routes = []
    if PLAN_SHOW_OTHER_ROUTES:
        nl_csv = find_nearby_lines_csv(vertices_csv)
        if nl_csv:
            try:
                other_routes = load_other_routes(nl_csv, trans)
            except Exception as e:
                print(f"WARNING: could not read nearby HPMS lines ({e!r}).")

    xs_stations = xs_profiles = None
    if CROSS_SECTION_SHEETS or XS_MARK_PLAN_VIEW:
        st_csv, pr_csv = find_cross_section_csvs(vertices_csv)
        if st_csv:
            xs_stations = pd.read_csv(st_csv)
            xs_profiles = pd.read_csv(pr_csv)

    total_dist = dist_ft[-1] - dist_ft[0]
    num_pages = max(1, int(math.ceil(total_dist / PAGE_LENGTH_FT)))
    date_str = datetime.datetime.now().strftime("%Y-%m-%d")

    global_mp_start = mp_array.min()
    global_mp_end = mp_array.max()

    base_dir = os.path.dirname(pdf_out)
    base_name, ext = os.path.splitext(os.path.basename(pdf_out))
    new_pdf_out = os.path.join(base_dir, f"{base_name}_MP_{global_mp_start:.3f}_to_{global_mp_end:.3f}{ext}")

    os.makedirs(os.path.dirname(new_pdf_out) or ".", exist_ok=True)

    # --- Run-parameters file (for the cover page) ---
    param_summary = None
    cover_date_str = date_str
    if load_run_param_summary_json is not None:
        sidecar_path = find_run_parameters_sidecar(vertices_csv)
        param_summary = load_run_param_summary_json(sidecar_path)
        if param_summary:
            sidecar_run_date = param_summary.get("identity", {}).get("run_date")
            if sidecar_run_date:
                # The date the CSVs were made, which differs from today's
                # date when the PDF is made later from existing CSVs.
                cover_date_str = sidecar_run_date

    # DEM tiles: --dem_dir, else DEM_DIR above, else the folder the
    # plan/profile run recorded in its run-parameters file.
    if read_dem_window is None:
        tile_dir = None
    else:
        recorded = (param_summary or {}).get("identity", {}).get("dem_tile_dir")
        tile_dir = resolve_tile_dir(dem_dir or DEM_DIR or recorded)
    if tile_dir and not tiles_cover_route(tile_dir, vtx['Lon'].to_numpy(), vtx['Lat'].to_numpy()):
        print(f"NOTE: no cached 1 m DEM tiles found for this route in {tile_dir}; terrain pages omitted.")
        tile_dir = None

    # --- Overview centerline for the cover page (decimated; a small panel
    # doesn't need every 5-ft vertex) ---
    overview_lon = overview_lat = None
    full_lon = vtx["Lon"].to_numpy()
    full_lat = vtx["Lat"].to_numpy()
    if len(full_lon) >= 2:
        idx = np.linspace(0, len(full_lon) - 1, min(len(full_lon), 3000)).astype(int)
        overview_lon, overview_lat = full_lon[idx], full_lat[idx]

    with PdfPages(new_pdf_out) as pdf:
        add_cover_page(pdf, route_id, global_mp_start, global_mp_end, num_pages, cover_date_str, param_summary,
                        overview_lon=overview_lon, overview_lat=overview_lat)

        if tile_dir and (TERRAIN_OVERVIEW_PAGE or TERRAIN_SECTION_PAGES):
            try:
                all_lon = vtx['Lon'].to_numpy(dtype=float)
                all_lat = vtx['Lat'].to_numpy(dtype=float)
                kinds = (np.where(vtx['Structure_Kind'].isin(['Bridge', 'Tunnel']), 'Span', 'None')
                         if 'Structure_Kind' in vtx.columns else None)

                sections = []
                if TERRAIN_SECTION_PAGES and (global_mp_end - global_mp_start) > TERRAIN_SECTION_MILES * 1.2:
                    n_sec = int(math.ceil((global_mp_end - global_mp_start) / TERRAIN_SECTION_MILES))
                    for k_sec in range(n_sec):
                        s0 = global_mp_start + k_sec * TERRAIN_SECTION_MILES
                        s1 = min(global_mp_end, s0 + TERRAIN_SECTION_MILES)
                        m = (mp_array >= s0) & (mp_array <= s1)
                        if m.sum() >= 2:
                            sections.append((f"{chr(65 + k_sec) if k_sec < 26 else k_sec + 1}: MP {s0:.2f}-{s1:.2f}",
                                             m, terrain_map_box(tile_dir, all_lon[m], all_lat[m])))

                if TERRAIN_OVERVIEW_PAGE:
                    sub_t = (f"MP {global_mp_start:.3f} to {global_mp_end:.3f}"
                             + (f"   --   dashed boxes show the {len(sections)} section pages that follow" if sections else ""))
                    add_terrain_page(pdf, f"TERRAIN OVERVIEW: {route_id}", sub_t, all_lon, all_lat,
                                     mp_array, kinds, tile_dir, TERRAIN_OVERVIEW_MAX_PX,
                                     sections=[(lbl, bx) for lbl, _, bx in sections])
                for lbl, m, _ in sections:
                    add_terrain_page(pdf, f"TERRAIN, SECTION {lbl.split(':')[0]}: {route_id}", lbl.split(': ', 1)[1],
                                     all_lon[m], all_lat[m], mp_array[m],
                                     kinds[m] if kinds is not None else None, tile_dir, 2500)
            except Exception as e:
                print(f"WARNING: terrain pages failed ({e!r}); continuing without them.")

        # Sheets are measured from dist_ft[0]. Dist_Ft does not start at
        # zero: it follows the route's starting milepost (a route starting
        # at MP 299.98 begins near 1.58 million ft).
        dist_origin = dist_ft[0]
        for p in range(num_pages):
            page_start_dist = dist_origin + p * PAGE_LENGTH_FT
            page_end_dist = dist_origin + (p + 1) * PAGE_LENGTH_FT

            mask = (dist_ft >= page_start_dist) & (dist_ft < page_end_dist)
            if not mask.any(): continue

            sub_x = x_ft[mask]
            sub_y = y_ft[mask]
            sub_raw_x = raw_x_ft[mask]
            sub_raw_y = raw_y_ft[mask]
            sub_raw_elev = raw_elev_ft[mask]
            sub_mp = mp_array[mask]
            sub_dist = dist_ft[mask]
            sub_elev = elev_ft[mask]
            sub_lon = vtx['Lon'].to_numpy()[mask]
            sub_lat = vtx['Lat'].to_numpy()[mask]

            if len(sub_x) < 2: continue

            # Each sheet is drawn inside its own try block, so a problem on
            # one sheet (odd data, a failed basemap fetch) skips that sheet
            # instead of ending the PDF there.
            fig = None
            try:
                avg_elev = np.mean(sub_elev)

                p_start_x, p_start_y = sub_x[0], sub_y[0]
                angle, y_shift = fit_plan_view(sub_x, sub_y)

                rot_x, rot_y = rotate_points(sub_x, sub_y, p_start_x, p_start_y, -angle)
                raw_rot_x, raw_rot_y = rotate_points(sub_raw_x, sub_raw_y, p_start_x, p_start_y, -angle)
                rot_y = rot_y - y_shift
                raw_rot_y = raw_rot_y - y_shift

                mp_min, mp_max = sub_mp.min(), sub_mp.max()
                page_mp_span = mp_max - mp_min
                page_dist = sub_dist[-1] - sub_dist[0]
                if page_dist > 0:
                    page_mp_span = (PAGE_LENGTH_FT / page_dist) * (mp_max - mp_min)

                fig = plt.figure(figsize=(17, 11))
                rect = Rectangle((0.02, 0.02), 0.96, 0.96, linewidth=2, edgecolor='black', facecolor='none', transform=fig.transFigure)
                fig.add_artist(rect)
                fig.text(0.05, 0.94, f"PROJECT: {route_id}", fontsize=14, fontweight='bold')
                fig.text(0.85, 0.05, f"SHEET {p+1} OF {num_pages}", fontsize=14, fontweight='bold')
                fig.text(0.85, 0.035, f"DATE: {date_str}", fontsize=10)
                if elev_interval:
                    gs = GridSpec(3, 1, height_ratios=[1, 0.88, 0.14], hspace=0.3,
                                  left=0.08, right=0.92, top=0.88, bottom=0.10)
                else:
                    gs = GridSpec(2, 1, height_ratios=[1, 1], hspace=0.3, left=0.08, right=0.92, top=0.88, bottom=0.08)

                # TOP: PLAN VIEW -- true scale (feet both ways), route rotated
                # to run left to right; mileposts are marked along the centerline.
                ax_plan = fig.add_subplot(gs[0])
                pos = ax_plan.get_position()
                plan_aspect = (pos.width * fig.get_figwidth()) / (pos.height * fig.get_figheight())
                px0, px1 = plan_x_limits(rot_x, rot_y, plan_aspect, aligned=(y_shift == 0.0))
                half_h = (px1 - px0) / plan_aspect / 2.0
                ax_plan.set_xlim(px0, px1)
                ax_plan.set_ylim(-half_h, half_h)
                ax_plan.set_aspect('equal', adjustable='box')

                ax_plan.plot(rot_x, rot_y, 'k-', linewidth=2.5, label="Centerline", zorder=2)
                ax_plan.plot(raw_rot_x, raw_rot_y, color='red', linestyle='--', linewidth=1.0, label="Raw Input Geom", zorder=1)

                plan_callouts = []
                colors_h = {'Left': 'blue', 'Right': 'red'}
                if not hc.empty:
                    for _, c in hc.iterrows():
                        c_start_ft = c['Start_Dist'] * FEET_PER_METER
                        c_end_ft = c['End_Dist'] * FEET_PER_METER
                        c_mask = (sub_dist >= c_start_ft) & (sub_dist <= c_end_ft)

                        if c_mask.any():
                            c_x = rot_x[c_mask]
                            c_y = rot_y[c_mask]
                            ax_plan.plot(c_x, c_y, color=colors_h.get(c['Dir'], 'green'), linewidth=6, alpha=0.5, zorder=3)

                            mid_i = len(c_x) // 2
                            label_x = c_x[mid_i]

                            min_r_ft = c.get('Min_Radius_m', c.get('Radius_m', 0)) * FEET_PER_METER
                            is_spiral = c.get('Transition_Type', 'Simple') == 'Spiral'
                            start_label = "TS" if is_spiral else "PC"
                            end_label = "ST" if is_spiral else "PT"
                            spiral_tag = "[SPIRAL]\n" if is_spiral else ""

                            e_meas = c.get('Superelevation_Meas_Pct', np.nan)
                            if c.get('Superelevation_Note', '') == "NC":
                                e_line = "\ne=NC (DEM)"
                            else:
                                e_line = f"\ne={e_meas:.1f}% (DEM)" if pd.notna(e_meas) else ""
                            curve_tag = f"Curve {int(c['Curve_No'])}  " if pd.notna(c.get('Curve_No', np.nan)) else ""
                            annot = f"{off_road_line(c)}{spiral_tag}{curve_tag}{c.get('Dir', '')}\nClass {c.get('Bin', 'A')}\nMin R={min_r_ft:.0f}'\nΔ={c['Delta']:.1f}°\nL={c.get('Length_m', 0)*FEET_PER_METER:.0f}'{e_line}\n{start_label}: RP {c.get('Calibrated_Start_MP', 0):.3f}\n{end_label}: RP {c.get('Calibrated_End_MP', 0):.3f}"

                            plan_callouts.append((annot, (label_x, c_y[mid_i]), 1 if c['Dir'] == "Right" else -1))

                ax_plan.set_title("PLAN VIEW", fontweight='bold')

                if tile_dir and PLAN_VIEW_RELIEF:
                    try:
                        draw_plan_view_relief(ax_plan, tile_dir, p_start_x, p_start_y, angle, utm_to_wgs,
                                              y_shift_ft=y_shift)
                    except Exception as e:
                        print(f"WARNING: plan-view relief failed on sheet {p + 1} ({e!r}).")

                draw_milepost_ticks(ax_plan, rot_x, rot_y, sub_dist, sub_mp, dist_origin)

                if other_routes:
                    draw_other_routes(ax_plan, other_routes, p_start_x, p_start_y, angle, y_shift,
                                      (px0, px1), half_h, rot_x, rot_y)

                if XS_MARK_PLAN_VIEW and xs_stations is not None:
                    on_page = xs_stations[(xs_stations["Dist_Ft"] >= sub_dist[0]) & (xs_stations["Dist_Ft"] <= sub_dist[-1])]
                    for _, st in on_page.iterrows():
                        # Section line drawn square to the route at the station.
                        j = int(np.argmin(np.abs(sub_dist - st["Dist_Ft"])))
                        nx_, ny_ = _right_normal(rot_x, rot_y, j)
                        c_off = float(st["Center_Offset_Ft"])
                        ends = [(c_off + d) for d in (-150.0, 150.0)]
                        px = [rot_x[j] + e * nx_ for e in ends]
                        py = [rot_y[j] + e * ny_ for e in ends]
                        ax_plan.plot(px, py, color='#6a3d9a', lw=0.9, ls='-.', zorder=4)
                        k = int(np.argmin(py))
                        ax_plan.text(px[k], max(py[k], -half_h + 5), st["Station_ID"], color='#6a3d9a',
                                     fontsize=6.5, ha='center', va='top', zorder=6, clip_on=True,
                                     bbox=dict(boxstyle="round,pad=0.1", fc="white", ec="none", alpha=0.7))

                ax_plan.set_xticks(np.arange(math.ceil(px0 / 100.0) * 100.0, px1, 100.0))
                ax_plan.set_yticks(np.arange(math.ceil(-half_h / 100.0) * 100.0, half_h, 100.0))
                ax_plan.set_xlim(px0, px1)
                ax_plan.set_ylim(-half_h, half_h)
                ax_plan.tick_params(labelbottom=False, labelleft=False, length=0)
                ax_plan.grid(True, color='#9e9e9e', linewidth=0.4, alpha=0.35)
                _plan_scale_bar(ax_plan, px0, px1, half_h)
                _north_arrow(ax_plan, angle, px0, px1, half_h)
                plan_legend = ax_plan.legend(loc='lower right', fontsize=8)

                # Aerial locator inset: this sheet's centerline over USGS
                # imagery, in Web Mercator so the two line up exactly.
                inset_rect = [0.85, 0.80, 0.1, 0.15]
                inset_w_in = inset_rect[2] * fig.get_figwidth()
                inset_h_in = inset_rect[3] * fig.get_figheight()
                smx, smy = to_web_mercator(sub_lon, sub_lat)
                half_span = max(smx.max() - smx.min(), smy.max() - smy.min()) / 2 * 1.25 + 100.0
                ccx, ccy = (smx.max() + smx.min()) / 2, (smy.max() + smy.min()) / 2
                ix0, ix1, iy0, iy1 = fit_extent_to_aspect(ccx - half_span, ccx + half_span,
                                                          ccy - half_span, ccy + half_span,
                                                          inset_w_in, inset_h_in)
                inset_tiles = fetch_basemap_tiles(ix0, ix1, iy0, iy1, inset_w_in, USGS_IMAGERY_TILES)
                ax_inset = None
                if inset_tiles is not None:
                    ax_inset = fig.add_axes(inset_rect, anchor='NE', zorder=1)
                    img, (bw, be, bs, bn) = inset_tiles
                    ax_inset.imshow(img, extent=(bw, be, bs, bn), interpolation='lanczos')
                    ax_inset.plot(smx, smy, color='#ffde17', linewidth=1.5)
                    ax_inset.set_xlim(ix0, ix1)
                    ax_inset.set_ylim(iy0, iy1)
                    ax_inset.set_aspect('equal')
                    ax_inset.set_xticks([]); ax_inset.set_yticks([])
                    ax_inset.text(0.5, -0.04, BASEMAP_CREDIT, transform=ax_inset.transAxes,
                                  fontsize=5, ha='center', va='top', color='#555555')

                # BOTTOM: PROFILE VIEW
                ax_prof = fig.add_subplot(gs[1])
                ax_prof.plot(sub_mp, sub_elev, 'k-', linewidth=2.5, label="Proposed Profile", zorder=2)
                ax_prof.plot(sub_mp, sub_raw_elev, color='red', linestyle='--', linewidth=1.0, label="Raw Ground", zorder=1)
                ax_prof.fill_between(sub_mp, sub_elev, min(sub_elev)-20, color='#e0e0e0', alpha=0.5, zorder=1)
                # SIGHT DISTANCE SAFETY BAND
                if 'Available_Sight_Dist_Ft' in vtx.columns:
                    sub_asd = vtx.loc[mask, 'Available_Sight_Dist_Ft'].to_numpy()
                    y_min_limit = avg_elev - 95 # Bottom of the Y-axis
                    ax_prof.plot(sub_mp, [y_min_limit] * len(sub_mp), color='gray', linewidth=4, alpha=0.3, zorder=0)

                    blind_mask_local = sub_asd < 400
                    if blind_mask_local.any():
                        ax_prof.scatter(sub_mp[blind_mask_local], [y_min_limit] * np.sum(blind_mask_local),
                                        color='red', s=10, marker='s', label='Sight Warning (<400ft)', zorder=4)
                # INTERCHANGE TOPOLOGY MARKERS
                if 'Topology_Node' in vtx.columns:
                    topo_subset = vtx.loc[mask & (vtx['Topology_Node'] != 'None')].copy()
                    if not topo_subset.empty:
                        unique_topo = topo_subset.drop_duplicates(subset=['Intersecting_Route'])
                        for _, row in unique_topo.iterrows():
                            l_color = 'orange' if row['Topology_Node'] == 'Grade-Separated' else 'green'
                            ax_prof.axvline(x=row['Milepost'], color=l_color, linestyle='--', linewidth=1.5, alpha=0.7, zorder=5)
                            ax_prof.text(row['Milepost'], avg_elev + 85, f"{row['Topology_Node']}\n{row['Intersecting_Route']}",
                                        color=l_color, fontsize=7, fontweight='bold', ha='center', va='top', rotation=90,
                                        bbox=dict(boxstyle="round,pad=0.2", fc="white", ec=l_color, alpha=0.8))

                prof_callouts = []
                colors_v = {'CREST': 'orange', 'SAG': 'purple'}
                if not vc.empty:
                    for _, v in vc.iterrows():
                        v_start_ft = v['Start_Dist'] * FEET_PER_METER
                        v_end_ft = v['End_Dist'] * FEET_PER_METER
                        v_mask = (sub_dist >= v_start_ft) & (sub_dist <= v_end_ft)

                        if v_mask.any():
                            v_d = sub_mp[v_mask]
                            v_e = sub_elev[v_mask]
                            ax_prof.plot(v_d, v_e, color=colors_v.get(v['Type'], 'gray'), linewidth=4, zorder=3)

                            mid_i = len(v_d) // 2
                            label_x = v_d[mid_i]

                            annot = (f"{off_road_line(v)}{v['Type']}{vc_class_line(v)}\nK={v['K_Value']:.0f}  L={v.get('Length_m', 0)*FEET_PER_METER:.0f}'"
                                     f"\nG1={v['Grade_In']:.2f}%  G2={v['Grade_Out']:.2f}%{vc_ssd_line(v)}"
                                     f"\nPVC: RP {v.get('Calibrated_Start_MP', 0):.3f}\nPVT: RP {v.get('Calibrated_End_MP', 0):.3f}")

                            prof_callouts.append((annot, (label_x, v_e[mid_i]), -1 if v['Type'] == "CREST" else 1))

                ax_prof.set_title("PROFILE VIEW", fontweight='bold')
                ax_prof.set_ylabel("Elevation (ft)")
                ax_prof.set_xlabel("Reference Point")

                ax_prof.set_xlim(mp_min, mp_min + page_mp_span)
                ax_prof.set_ylim(avg_elev - 100, avg_elev + 100)
                ax_prof.grid(True, linestyle='--', alpha=0.5)

                ax_plan.ticklabel_format(useOffset=False, style='plain', axis='x')
                ax_prof.ticklabel_format(useOffset=False, style='plain', axis='x')

                prof_legend = ax_prof.legend(loc='lower right', fontsize=8)

                if elev_interval:
                    ax_strip = fig.add_subplot(gs[2], sharex=ax_prof)
                    fig.subplots_adjust(hspace=0.3)
                    pos_p, pos_s = ax_prof.get_position(), ax_strip.get_position()
                    ax_strip.set_position([pos_s.x0, pos_s.y0, pos_s.width, pos_p.y0 - pos_s.y0 - 0.005])
                    ax_prof.set_xlabel("")
                    ax_prof.tick_params(axis="x", labelbottom=False)
                    draw_elevation_strip(ax_strip, ax_prof, sub_dist, sub_mp, sub_elev, sub_raw_elev,
                                         dist_origin, elev_interval)
                    ax_strip.set_xlabel("Reference Point")
                    ax_strip.ticklabel_format(useOffset=False, style='plain', axis='x')
                    ax_strip.set_xlim(mp_min, mp_min + page_mp_span)

                place_callouts(fig, ax_plan, plan_callouts, (plan_legend, ax_inset), (rot_x, rot_y))
                place_callouts(fig, ax_prof, prof_callouts, (prof_legend,), (sub_mp, sub_elev))
                pdf.savefig(fig, dpi=PDF_DPI)
                plt.close(fig)
            except Exception as e:
                print(f"WARNING: sheet {p + 1} of {num_pages} failed to render "
                      f"({e!r}); skipping this sheet and continuing.")
                traceback.print_exc()
            finally:
                if fig is not None:
                    plt.close(fig)

        if CROSS_SECTION_SHEETS and xs_stations is not None and not xs_stations.empty:
            try:
                n_xs = add_cross_section_sheets(pdf, xs_stations, xs_profiles, route_id, hc, date_str)
                print(f"Added {n_xs} cross-section sheet(s) ({len(xs_stations)} sections).")
            except Exception as e:
                print(f"WARNING: cross-section sheets failed ({e!r}); continuing without them.")
                traceback.print_exc()

    return new_pdf_out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--vertices_csv", required=True)
    ap.add_argument("--horizontal_csv", required=True)
    ap.add_argument("--vertical_csv", required=True)
    ap.add_argument("--pdf_out", required=True)
    ap.add_argument("--route_id", required=True)
    ap.add_argument("--dem_dir", default=None,
                    help="DEM cache folder (or its align_1m_cache subfolder) for the terrain pages")
    ap.add_argument("--profile_elev_interval_ft", type=float, default=None,
                    help="print profile and ground elevations under each profile at this interval (ft)")
    args = ap.parse_args()

    final_pdf_path = make_pdf(
        vertices_csv=args.vertices_csv,
        horizontal_csv=args.horizontal_csv,
        vertical_csv=args.vertical_csv,
        pdf_out=args.pdf_out,
        route_id=args.route_id,
        dem_dir=args.dem_dir,
        profile_elev_interval_ft=args.profile_elev_interval_ft,
    )
    print(f"Saved PDF: {final_pdf_path}")


if __name__ == "__main__":
    main()