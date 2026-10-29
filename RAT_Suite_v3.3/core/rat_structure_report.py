# core/rat_structure_report.py

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
RAT STRUCTURE SPAN REPORT
--------------------------------------------------------------------------------
Lists every bridge and tunnel span the core engine repaired (see
core.rat_core.repair_structure_spans) and, for plan/profile runs, renders a
PDF with one page per span: the raw DEM profile against the final profile,
and a shaded-relief map of the 1 m DEM around the structure with the route
drawn on it.

Works from a vertices table (plan/profile or alignment), using the
Structure_Kind / Structure_Tier / Structure_Record columns, so the same code
serves rat_plan_profile_cli.py, rat_alignment_cli.py (CSV only) and
tools/rat_structure_span_check.py.

CREATED BY: Federal Highway Administration, Office of Highway Policy Information.
VERSION: 3.3, October 29, 2026
"""
import math
import logging
from typing import Optional

import numpy as np
import pandas as pd

from core.rat_dem_relief import read_dem_window, draw_relief

FEET_PER_METER = 3.28084
CHUNK_BREAK_FT = 50.0        # a larger along-route jump between vertices starts a new span
PLOT_MARGIN_FT = 300.0       # road shown on each side of a span
PDF_DPI = 200
RELIEF_MIN_HALF_WIDTH_M = 250.0   # relief map is at least 500 m across, for context

SPAN_COLUMNS = [
    "RouteId", "Kind", "Source_Tier", "Records_Nearby", "Start_MP", "End_MP",
    "Span_Ft", "Z_Start_Ft", "Z_End_Ft", "Span_Grade_Pct", "Max_Raw_Departure_Ft", "Open_End",
    "Start_Lon", "Start_Lat", "End_Lon", "End_Lat",
]


def _distance_ft(vtx: pd.DataFrame) -> np.ndarray:
    if "Dist_Ft" in vtx.columns:
        return vtx["Dist_Ft"].to_numpy(dtype=float)
    if "Dist_Mi" in vtx.columns:
        return vtx["Dist_Mi"].to_numpy(dtype=float) * 5280.0
    raise ValueError("Vertices table needs a Dist_Ft or Dist_Mi column.")


def _span_runs(vtx: pd.DataFrame) -> list:
    """(first_row, last_row) positions of each contiguous span in vtx."""
    if "Structure_Kind" not in vtx.columns or vtx.empty:
        return []
    kind = vtx["Structure_Kind"].to_numpy()
    on = np.flatnonzero(kind != "None")
    if on.size == 0:
        return []
    tier = vtx["Structure_Tier"].to_numpy() if "Structure_Tier" in vtx.columns else np.full(len(vtx), "")
    route = vtx["RouteId"].astype(str).to_numpy() if "RouteId" in vtx.columns else np.full(len(vtx), "")
    dist = _distance_ft(vtx)

    breaks = np.ones(on.size, dtype=bool)
    if on.size > 1:
        a, b = on[:-1], on[1:]
        breaks[1:] = ((b - a) != 1) | (kind[b] != kind[a]) | (tier[b] != tier[a]) \
            | (route[b] != route[a]) | (np.abs(dist[b] - dist[a]) > CHUNK_BREAK_FT)
    starts = np.flatnonzero(breaks)
    ends = np.r_[starts[1:] - 1, on.size - 1]
    return [(int(on[s]), int(on[e])) for s, e in zip(starts, ends)]


def build_structure_span_table(vtx: pd.DataFrame) -> pd.DataFrame:
    """One row per bridge/tunnel span found in a vertices table."""
    runs = _span_runs(vtx)
    if not runs:
        return pd.DataFrame(columns=SPAN_COLUMNS)
    dist = _distance_ft(vtx)
    elev = vtx["Elev_Ft"].to_numpy(dtype=float)
    raw = vtx["Elev_Raw_Ft"].to_numpy(dtype=float)
    rec = vtx["Structure_Record"].to_numpy() if "Structure_Record" in vtx.columns else None
    route = vtx["RouteId"].astype(str).to_numpy() if "RouteId" in vtx.columns else np.full(len(vtx), "")
    part = vtx["Part"].to_numpy() if "Part" in vtx.columns else np.zeros(len(vtx))
    n = len(vtx)

    def _profile_edge(i, j):
        """True if row i is the first/last row of its profile (j = neighbor)."""
        return (j < 0 or j >= n or route[j] != route[i] or part[j] != part[i]
                or abs(dist[j] - dist[i]) > CHUNK_BREAK_FT)

    rows = []
    for a, b in runs:
        span_ft = abs(dist[b] - dist[a])
        records = ""
        if rec is not None:
            found = sorted(set(rec[a:b + 1]) - {"None"})
            records = ",".join(found)
        rows.append({
            "RouteId": vtx["RouteId"].iloc[a] if "RouteId" in vtx.columns else "",
            "Kind": vtx["Structure_Kind"].iloc[a],
            "Source_Tier": vtx["Structure_Tier"].iloc[a] if "Structure_Tier" in vtx.columns else "",
            "Records_Nearby": records,
            "Start_MP": round(float(vtx["Milepost"].iloc[a]), 4),
            "End_MP": round(float(vtx["Milepost"].iloc[b]), 4),
            "Span_Ft": round(span_ft, 1),
            "Z_Start_Ft": round(float(elev[a]), 2),
            "Z_End_Ft": round(float(elev[b]), 2),
            "Span_Grade_Pct": round((elev[b] - elev[a]) / span_ft * 100.0, 3) if span_ft > 0 else 0.0,
            "Max_Raw_Departure_Ft": round(float(np.max(np.abs(elev[a:b + 1] - raw[a:b + 1]))), 1),
            # Bridge running off the end of the profile: only one abutment
            # was in the data, and the deck is held level at its elevation.
            "Open_End": "Start" if _profile_edge(a, a - 1) else ("End" if _profile_edge(b, b + 1) else ""),
            "Start_Lon": round(float(vtx["Lon"].iloc[a]), 7),
            "Start_Lat": round(float(vtx["Lat"].iloc[a]), 7),
            "End_Lon": round(float(vtx["Lon"].iloc[b]), 7),
            "End_Lat": round(float(vtx["Lat"].iloc[b]), 7),
        })
    return pd.DataFrame(rows, columns=SPAN_COLUMNS)


# ---------------------------------------------------------------------------
# PDF
# ---------------------------------------------------------------------------
def render_structure_span_pdf(vtx: pd.DataFrame, spans: pd.DataFrame, pdf_path: str,
                              dem_dir: Optional[str] = None, title: str = "",
                              profile_label: str = "Final profile") -> str:
    """One landscape page per span: profile on the left, DEM relief on the
    right (if dem_dir holds the tiles). Returns pdf_path."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.backends.backend_pdf import PdfPages

    runs = _span_runs(vtx)
    dist = _distance_ft(vtx)
    lon_all = vtx["Lon"].to_numpy(dtype=float)
    lat_all = vtx["Lat"].to_numpy(dtype=float)
    route_all = vtx["RouteId"].astype(str).to_numpy() if "RouteId" in vtx.columns else np.full(len(vtx), "")

    with PdfPages(pdf_path) as pdf:
        for (a, b), (_, sp) in zip(runs, spans.iterrows()):
            same_route = route_all == route_all[a]
            win = same_route & (dist >= dist[a] - PLOT_MARGIN_FT) & (dist <= dist[b] + PLOT_MARGIN_FT)
            idx = np.flatnonzero(win)
            if idx.size < 2:
                continue

            fig = plt.figure(figsize=(17, 11))
            head = (f"{title}  " if title else "") + \
                f"Route {sp['RouteId']} -- {sp['Kind']} ({sp['Source_Tier']})   " \
                f"MP {sp['Start_MP']:.3f} - {sp['End_MP']:.3f}"
            fig.text(0.04, 0.95, head, fontsize=15, fontweight="bold")
            detail = (f"Span {sp['Span_Ft']:.0f} ft    Ends {sp['Z_Start_Ft']:.2f} -> {sp['Z_End_Ft']:.2f} ft    "
                      f"Grade {sp['Span_Grade_Pct']:+.2f}%    Max raw DEM departure {sp['Max_Raw_Departure_Ft']:.1f} ft")
            if sp.get("Records_Nearby"):
                detail += f"    Records: {sp['Records_Nearby']}"
            fig.text(0.04, 0.915, detail, fontsize=11, color="#333333")

            # Profile
            ax_p = fig.add_axes([0.05, 0.10, 0.45, 0.76])
            mp = vtx["Milepost"].to_numpy(dtype=float)
            ax_p.plot(mp[idx], vtx["Elev_Raw_Ft"].to_numpy()[idx], ".", color="#c0392b", ms=2.5, label="Raw 1 m DEM")
            ax_p.plot(mp[idx], vtx["Elev_Ft"].to_numpy()[idx], "-", color="#7f8c8d", lw=1.5, label=profile_label)
            ax_p.plot(mp[a:b + 1], vtx["Elev_Ft"].to_numpy()[a:b + 1], "-", color="#1f4e9c", lw=3, label="Structure span")
            ax_p.set_xlabel("Milepost")
            ax_p.set_ylabel("Elevation (ft)")
            ax_p.set_title("Profile", fontweight="bold")
            ax_p.grid(alpha=0.3)
            ax_p.legend(fontsize=9, loc="best")
            ax_p.ticklabel_format(useOffset=False, style="plain", axis="x")

            # Relief
            ax_m = fig.add_axes([0.55, 0.10, 0.40, 0.76])
            ax_m.set_title("Shaded relief, 1 m DEM", fontweight="bold")
            relief = None
            if dem_dir:
                lon_w, lat_w = lon_all[idx], lat_all[idx]
                mid_lat = float(np.mean(lat_w))
                k = math.cos(math.radians(mid_lat))
                half_m = max((lon_w.max() - lon_w.min()) * 111320.0 * k,
                             (lat_w.max() - lat_w.min()) * 111320.0) / 2 + 60.0
                half_m = max(half_m, RELIEF_MIN_HALF_WIDTH_M)
                clon, clat = (lon_w.max() + lon_w.min()) / 2, (lat_w.max() + lat_w.min()) / 2
                dlat, dlon = half_m / 111320.0, half_m / (111320.0 * k)
                try:
                    relief = read_dem_window(dem_dir, clon - dlon, clat - dlat, clon + dlon, clat + dlat)
                except Exception as e:
                    logging.warning(f"Structure report: could not read DEM for MP {sp['Start_MP']:.3f}: {e}")
            if relief is not None:
                elev_m, extent = relief
                mappable = draw_relief(ax_m, elev_m, extent)
                cb = fig.colorbar(mappable, ax=ax_m, fraction=0.04, pad=0.02)
                cb.set_label("Elevation (ft)")
                in_map = same_route & (lon_all >= extent[0]) & (lon_all <= extent[1]) \
                    & (lat_all >= extent[2]) & (lat_all <= extent[3])
                route_pts = np.where(in_map, lon_all, np.nan)
                ax_m.plot(route_pts, np.where(in_map, lat_all, np.nan), "-", color="black", lw=1.2, alpha=0.8)
                ax_m.plot(lon_all[a:b + 1], lat_all[a:b + 1], "-", color="#ff2fd0", lw=2.5, label="Structure span")
                ax_m.legend(fontsize=8, loc="upper right")
                ax_m.set_xlim(extent[0], extent[1])
                ax_m.set_ylim(extent[2], extent[3])
                ax_m.set_xticks([]); ax_m.set_yticks([])
            else:
                ax_m.text(0.5, 0.5, "DEM tiles not available", ha="center", va="center",
                          transform=ax_m.transAxes, color="#888888", style="italic")
                ax_m.set_xticks([]); ax_m.set_yticks([])

            pdf.savefig(fig, dpi=PDF_DPI)
            plt.close(fig)
    return pdf_path