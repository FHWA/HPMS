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
routes to extract geometric curves. Run from the Unified GUI, which passes
the run's settings in run_params.json, or from the command line.

Each route is stitched into continuity chunks (core.rat_core.
build_route_continuity_chunks); very long chunks are split into padded
pieces for parallel processing, and each piece reports only the curves,
grades and vertices within its own milepost range, so no two pieces report
the same output. Elevations come from the USGS 3DEP 1 m DEM, cached in
align_1m_cache under the DEM directory.

CREATED BY: Federal Highway Administration, Office of Highway Policy Information.
VERSION: 3.3, October 29, 2026
"""
import os
import glob
import warnings
import sys
import json
import time
import logging
import argparse
from datetime import datetime
import numpy as np
import pandas as pd
import geopandas as gpd
import fiona
import fiona.crs
from shapely.geometry import LineString
import folium
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

# ----------------------------
# Path bootstrap for core import
# ----------------------------
THIS_DIR = os.path.dirname(os.path.abspath(__file__))
RAT_SUITE_DIR = os.path.dirname(THIS_DIR)
if RAT_SUITE_DIR not in sys.path:
    sys.path.insert(0, RAT_SUITE_DIR)

from core.rat_core import (
    build_params,
    SOCRATA_TOKEN as ENV_SOCRATA_TOKEN,
    write_gdf_with_retry,
    audit_route_ordering,
    build_route_continuity_chunks,
    route_spans_several_fsystems,
    log_fsystem_summary,
    describe_smoothing_factors,
    split_chunks_for_parallelism,
    smooth_plan_profile_from_linestring,
    profile_result_ok,
    analyze_horizontal_curvature,
    analyze_vertical_parabolic,
    build_grade_segments,
    label_vertices_by_segments,
    add_speed_reference,
    merge_horizontal_curves,
    fetch_socrata_state,
    load_local_hpms,
    filter_local_df_to_state,
    apply_facility_fsystem_filters,
    FEET_PER_METER,
    calculate_headings,
    get_tangent_grade,
    fetch_nbi_nti_state,
    compute_padded_bbox,
    init_worker_nbi,
    run_bounded_parallel,
    DemCacheUnavailable,
    require_dem_cache,
    claim_output_version,
    release_output_version,
    raise_if_dem_cache_lost,
    STRUCTURE_TIER_LABELS,
    STRUCTURE_KIND_LABELS,
    STRUCTURE_RECORD_LABELS,
)
from core.rat_param_summary import build_run_param_summary, render_param_panel_html
from core.rat_structure_report import build_structure_span_table
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s: %(message)s")
# matplotlib reports at INFO when chart labels look like numbers (FS "1",
# "2", ...); that is intended here and only clutters the Run Log.
logging.getLogger("matplotlib.category").setLevel(logging.WARNING)

# ===========================================================================
# VERTICES TABLE (same columns as rat_plan_profile_cli.build_vertices_df)
# ===========================================================================
def build_vertices_df(
    res: dict,
    route_id: str,
    f_sys: int,
    state_id: str,
    chunk_s_mp: float,
    chunk_e_mp: float,
    global_start_dist_m: float,
    params: dict,
) -> pd.DataFrame:
    """
    Build a per-point vertex table for one stitched chunk using vectorized arrays.
    """
    coords_wgs      = np.array(res["coords_wgs_smooth"])
    z               = np.array(res["z_smooth"])
    spacing         = res["spacing_m"]
    coords_wgs_raw  = np.array(res["coords_wgs_raw"])
    z_raw           = np.array(res["z_raw"])
    coords_m_smooth = res["coords_m_smooth"]

    n_pts           = len(z)
    structure_tier  = res.get("structure_tier", np.zeros(n_pts, dtype=int))
    structure_kind  = res.get("structure_kind", np.zeros(n_pts, dtype=int))
    structure_rec   = res.get("structure_record", np.zeros(n_pts, dtype=int))
    iri_proxy       = res.get("iri_proxy_in", np.zeros(n_pts, dtype=float))
    asd             = res.get("available_sight_dist_ft", np.zeros(n_pts, dtype=float))

    tier_labels = np.array([STRUCTURE_TIER_LABELS[i] for i in range(4)])
    kind_labels = np.array([STRUCTURE_KIND_LABELS[i] for i in range(3)])
    rec_labels  = np.array([STRUCTURE_RECORD_LABELS[i] for i in range(3)])

    headings_unwrapped = calculate_headings(coords_m_smooth)

    # Calculate tangent grades
    grads = np.array([get_tangent_grade(z, i, spacing, params["REGRESSION_WINDOW_M"]) for i in range(n_pts)])
    total_len = max(float(res["d_axis"][-1]), 1.0)

    # Vectorize distance and milepost math
    local_dist_m  = np.arange(n_pts) * spacing
    frac          = local_dist_m / total_len
    mps           = chunk_s_mp + frac * (chunk_e_mp - chunk_s_mp)
    dist_mi       = (global_start_dist_m + local_dist_m) / 1609.344

    return pd.DataFrame({
        "State_ID":                 state_id,
        "RouteId":                  route_id,
        "FSystem":                  f_sys,
        "Milepost":                  np.round(mps, 4),
        "Dist_Mi":                  np.round(dist_mi, 4),
        "Lon":                      coords_wgs[:, 0],
        "Lat":                      coords_wgs[:, 1],
        "Elev_Ft":                  np.round(z * FEET_PER_METER, 3),
        "Raw_Lon":                  coords_wgs_raw[:, 0],
        "Raw_Lat":                  coords_wgs_raw[:, 1],
        "Elev_Raw_Ft":              np.round(z_raw * FEET_PER_METER, 3),
        "Grade_Pct":                np.round(grads, 3),
        "Heading_Deg_Unwrapped":    np.round(headings_unwrapped, 5),
        "Structure_Tier":           tier_labels[np.clip(structure_tier, 0, 3)],
        "Structure_Kind":           kind_labels[np.clip(structure_kind, 0, 2)],
        "Structure_Record":         rec_labels[np.clip(structure_rec, 0, 2)],
        "Micro_Jitter_Inches":      np.round(iri_proxy, 4),
        "Available_Sight_Dist_Ft":  np.round(asd, 1),
    })


# ===========================================================================
# SECTION-SCORE HELPER
# ===========================================================================
def assign_section_scores(
    df_input: pd.DataFrame,
    df_h: pd.DataFrame,
    df_g: pd.DataFrame,
) -> pd.DataFrame:
    """
    Return df_input with two new columns:
        H_Curve_Bin   - predominant horizontal curve class (A-F)
        V_Grade_Bin   - predominant grade class (A-F)

    The predominant class is the one covering the most length of the
    section. Horizontal curves cover only part of a route; the rest of the
    section is tangent and counts as class A. Grade segments (df_g) cover
    the whole route; any part of a section they do not reach also counts
    as A. Where two classes cover equal length, the more severe is kept.
    Sections overlapping nothing are A.
    """
    bins = list("ABCDEF")
    df_out = df_input.copy()
    df_out["H_Curve_Bin"] = "A"
    df_out["V_Grade_Bin"] = "A"
    df_out["_orig_idx"] = np.arange(len(df_out))

    def _apply_predominant_bin(df_items, bin_col, out_col):
        required = {"RouteId", "Calibrated_Start_MP", "Calibrated_End_MP", bin_col}
        if df_items is None or df_items.empty or not required.issubset(df_items.columns):
            return
        left = df_out[["_orig_idx", "RouteId", "Start_MP", "End_MP"]]
        right = df_items[["RouteId", "Calibrated_Start_MP", "Calibrated_End_MP", bin_col]]
        merged = left.merge(right, on="RouteId", how="inner")
        if merged.empty:
            return
        overlap = (np.minimum(merged["End_MP"], merged["Calibrated_End_MP"])
                   - np.maximum(merged["Start_MP"], merged["Calibrated_Start_MP"]))
        merged = merged.assign(_len=overlap.clip(lower=0.0))
        merged = merged[merged["_len"] > 0]
        if merged.empty:
            return
        lengths = (merged.pivot_table(index="_orig_idx", columns=bin_col, values="_len",
                                      aggfunc="sum", fill_value=0.0)
                   .reindex(columns=bins, fill_value=0.0))
        sec = df_out.set_index("_orig_idx").loc[lengths.index]
        sec_len = (sec["End_MP"] - sec["Start_MP"]).clip(lower=0.0).to_numpy()
        non_a = lengths[bins[1:]].sum(axis=1).to_numpy()
        lengths["A"] = np.maximum(sec_len - non_a, lengths["A"].to_numpy())
        # Reverse the columns so idxmax resolves ties toward the more severe class.
        best = lengths[bins[::-1]].idxmax(axis=1)
        df_out.loc[df_out["_orig_idx"].isin(best.index), out_col] = (
            df_out.loc[df_out["_orig_idx"].isin(best.index), "_orig_idx"].map(best).to_numpy())

    _apply_predominant_bin(df_h, "Bin", "H_Curve_Bin")
    _apply_predominant_bin(df_g, "Grade_Bin", "V_Grade_Bin")
    return df_out.drop(columns=["_orig_idx"])


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
def process_chunk(route_id: str, item: dict, dem_dir: str, params: dict,
                   subset: pd.DataFrame, nbi_nti_gdf: gpd.GeoDataFrame = None):
    """
    Smoothing, curve and grade detection, and the vertex table for one
    work item: a whole continuity chunk, or one padded piece of a long
    chunk (see split_chunks_for_parallelism).

    For a split piece, the output is cut to the piece's own (unpadded)
    milepost range, so adjacent pieces never report the same curve or
    vertex.
    """
    h_all, v_all, g_all, health_stats, vtx_dfs = [], [], [], [], []

    calib_start_mp = item["calib_start_mp"]
    calib_end_mp   = item["calib_end_mp"]
    emit_start_mp  = item["emit_start_mp"]
    emit_end_mp    = item["emit_end_mp"]

    res = smooth_plan_profile_from_linestring(
        item["geom"], dem_dir, params, item["f_sys"],
        route_id=route_id, chunk_s_mp=calib_start_mp, chunk_e_mp=calib_end_mp,
        hpms_subset=subset, nbi_nti_gdf=nbi_nti_gdf
    )
    if not profile_result_ok(res):
        return h_all, v_all, g_all, health_stats, vtx_dfs

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
                "FSystem": item["f_sys"],
                "RMSE_V_ft": rmse_v_ft,
                "RMSE_H_ft": rmse_h_ft
            })
    except ValueError as e:
        logging.debug(f"Could not compute RMSE health stats for route {route_id}: {e}")
    except KeyError as e:
        logging.warning(f"Result dictionary missing field {e} for route {route_id}")
    except Exception as e:
        logging.error(f"Unexpected error computing RMSE stats for route {route_id}: {type(e).__name__}: {e}")

    spacing_m = res["spacing_m"]
    coords_wgs_smooth = res["coords_wgs_smooth"]
    z_smooth = res["z_smooth"]
    n_pts = len(z_smooth)
    total_len = max(res["d_axis"][-1], 1.0)
    mp_span = calib_end_mp - calib_start_mp
    offset_m = item["global_start_dist_m"]

    h_curves = analyze_horizontal_curvature(res["coords_m_smooth"], spacing_m, params)
    if params.get("ENABLE_MERGE", False):
        h_curves = merge_horizontal_curves(h_curves, params)
    v_curves = analyze_vertical_parabolic(z_smooth, spacing_m, params)
    segments = build_grade_segments(z_smooth, spacing_m, v_curves)

    pad_ft = float(params.get("CHUNK_SPLIT_PADDING_FT", 20000.0))

    def _mp(dist_m):
        return calib_start_mp + (dist_m / total_len) * mp_span

    def _geometry(d0, d1, kind):
        i0 = max(0, int(d0 / spacing_m))
        i1 = min(n_pts - 1, int(d1 / spacing_m))
        if i1 > i0:
            return LineString(coords_wgs_smooth[i0:i1 + 1])
        logging.debug(f"No geometry for {kind} on route {route_id}: {d0:.1f}-{d1:.1f} m")
        return None

    def _finalize_curves(curves, kind: str):
        kept = []
        for c in curves:
            c["RouteId"] = route_id
            c["Part"] = item["part_id"]
            c["Calibrated_Start_MP"] = _mp(c["Start_Dist"])
            c["Calibrated_End_MP"] = _mp(c["End_Dist"])
            c["FSystem"] = item["f_sys"]
            c["geometry"] = _geometry(c["Start_Dist"], c["End_Dist"], f"{kind} curve")

            # A curve crossing a split boundary belongs to the piece that
            # holds its midpoint. For unsplit chunks this keeps everything.
            mid_mp = (c["Calibrated_Start_MP"] + c["Calibrated_End_MP"]) / 2.0
            if not (emit_start_mp <= mid_mp <= emit_end_mp):
                continue

            if item.get("is_split", False):
                curve_len_ft = (c["End_Dist"] - c["Start_Dist"]) * FEET_PER_METER
                if curve_len_ft >= 0.9 * pad_ft:
                    logging.warning(
                        f"Route {route_id} Part {item['part_id']:.4f}: {kind} curve "
                        f"length {curve_len_ft:.0f} ft is within 90% of "
                        f"CHUNK_SPLIT_PADDING_FT ({pad_ft:.0f} ft). Consider raising "
                        f"CHUNK_SPLIT_PADDING_FT so long curves near a split boundary "
                        f"aren't at risk of truncation."
                    )
            kept.append(c)
        return kept

    def _finalize_segments(segs):
        # Segments are clipped to the emit range (not assigned by midpoint)
        # so the pieces of a split chunk meet exactly at their boundaries.
        if mp_span > 0:
            d_lo = (emit_start_mp - calib_start_mp) / mp_span * total_len
            d_hi = (emit_end_mp - calib_start_mp) / mp_span * total_len
        else:
            d_lo, d_hi = 0.0, total_len
        kept = []
        for sg in segs:
            d0, d1 = max(sg["Start_Dist"], d_lo), min(sg["End_Dist"], d_hi)
            if d1 <= d0:
                continue
            kept.append({
                "RouteId": route_id,
                "Part": item["part_id"],
                "FSystem": item["f_sys"],
                "Start_Dist": d0,
                "End_Dist": d1,
                "Calibrated_Start_MP": _mp(d0),
                "Calibrated_End_MP": _mp(d1),
                "Length_ft": (d1 - d0) * FEET_PER_METER,
                "Grade_Pct": sg["Grade_Pct"],
                "Grade_Bin": sg["Grade_Bin"],
                "geometry": _geometry(d0, d1, "grade segment"),
            })
        return kept

    vtx_df = None
    try:
        vtx_df = build_vertices_df(
            res, route_id, item["f_sys"], item["state_id"],
            calib_start_mp, calib_end_mp, offset_m, params,
        )
        vtx_df["Part"] = item["part_id"]

        # Vertex labels come from every curve found in this (possibly
        # padded) piece, by point index, before the vertices are cut to
        # the emit range below.
        h_type = np.full(n_pts, "Tangent", dtype=object)
        h_bin = np.full(n_pts, "A", dtype=object)
        v_type = np.full(n_pts, "Tangent", dtype=object)
        v_class = np.full(n_pts, "A", dtype=object)
        bridge = (vtx_df["Structure_Kind"] == "Bridge").to_numpy()
        for c in h_curves:
            i0, i1 = int(round(c["Start_Dist"] / spacing_m)), int(round(c["End_Dist"] / spacing_m))
            h_type[i0:i1 + 1] = c["Dir"]
            h_bin[i0:i1 + 1] = c["Bin"]
        for c in v_curves:
            i0, i1 = int(round(c["Start_Dist"] / spacing_m)), int(round(c["End_Dist"] / spacing_m))
            v_type[i0:i1 + 1] = c["Type"]
            v_class[i0:i1 + 1] = c["VC_Class"]
            # A SAG curve lying on a bridge span means the span line didn't hold.
            c["False_Valley_Warning"] = bool(c["Type"] == "SAG" and bridge[i0:i1 + 1].any())
        seg_grade, seg_bin = label_vertices_by_segments(n_pts, spacing_m, segments)

        vtx_df["H_Curve_Type"] = h_type
        vtx_df["V_Curve_Type"] = v_type
        vtx_df["H_Curve_Bin"] = h_bin
        vtx_df["V_Curve_Class"] = v_class
        vtx_df["Seg_Grade_Pct"] = np.round(seg_grade, 3)
        vtx_df["V_Grade_Bin"] = seg_bin

        vtx_df['IRI_Reported'] = np.nan
        iri_col = next((c for c in subset.columns if c.upper() == 'IRI'), None)
        if iri_col:
            for _, row in subset.iterrows():
                mask = (vtx_df["Milepost"] >= row["Start_MP"]) & (vtx_df["Milepost"] <= row["End_MP"])
                vtx_df.loc[mask, "IRI_Reported"] = pd.to_numeric(row[iri_col], errors='coerce')

        # Keep only the vertices in this item's own (unpadded) MP range.
        vtx_df = vtx_df[(vtx_df["Milepost"] >= emit_start_mp) & (vtx_df["Milepost"] <= emit_end_mp)].copy()
    except KeyError as e:
        logging.warning(f"Vertices build failed for route {route_id} Part {item['part_id']:.4f}: missing field {e}")
        vtx_df = None
    except ValueError as e:
        logging.warning(f"Vertices build failed for route {route_id} Part {item['part_id']:.4f}: invalid data - {e}")
        vtx_df = None
    except Exception as exc:
        logging.error(f"Unexpected error building vertices for route {route_id} Part {item['part_id']:.4f}: {type(exc).__name__}: {exc}")
        vtx_df = None

    h_curves = _finalize_curves(h_curves, "horizontal")
    v_curves = _finalize_curves(v_curves, "vertical")
    add_speed_reference(v_curves, subset, item["f_sys"])
    g_all.extend(_finalize_segments(segments))

    # Distances along the route, on the same basis as the vertices' Dist_Mi.
    for c in h_curves + v_curves + g_all:
        for key in ("Start_Dist", "End_Dist", "SC_Dist", "CS_Dist", "VPI_Dist"):
            if key in c and c[key] is not None and not pd.isna(c[key]):
                c[key] += offset_m
    h_all.extend(h_curves)
    v_all.extend(v_curves)
    if vtx_df is not None:
        vtx_dfs.append(vtx_df)

    return h_all, v_all, g_all, health_stats, vtx_dfs

# ---------------------------------------------------------------------------
# Output Generators
# ---------------------------------------------------------------------------
# Grades map: falling grades in blues, rising grades in reds, class A gray.
GRADE_SIGN_COLORS = {
    "-F": "#0a2a52", "-E": "#174a8f", "-D": "#2a78d6", "-C": "#6aa6e8", "-B": "#b5d3f4",
    "A": "#9a9a96",
    "+B": "#f6b7b3", "+C": "#ee7a72", "+D": "#d93a32", "+E": "#9e1d1d", "+F": "#5c0a0a",
}
GRADE_LEGEND_ROWS = [
    ("+F", "+8.5% or more (F)"), ("+E", "+6.5 to +8.4% (E)"), ("+D", "+4.5 to +6.4% (D)"),
    ("+C", "+2.5 to +4.4% (C)"), ("+B", "+0.5 to +2.4% (B)"), ("A", "under 0.5% (A)"),
    ("-B", "-0.5 to -2.4% (B)"), ("-C", "-2.5 to -4.4% (C)"), ("-D", "-4.5 to -6.4% (D)"),
    ("-E", "-6.5 to -8.4% (E)"), ("-F", "-8.5% or more (F)"),
]

def export_intersections_gpkg(df_vtx: pd.DataFrame, out_path: str):
    """
    Extracts deduplicated intersection nodes (Route A x Route B) and saves
    them to a spatial GeoPackage (GPKG) for desktop GIS software.
    """
    if df_vtx.empty or 'Topology_Node' not in df_vtx.columns:
        return

    # Unknown Clearance crossings are included so they can be reviewed.
    intersections = df_vtx[df_vtx['Topology_Node'].isin(["At-Grade", "Grade-Separated", "Unknown Clearance"])]
    if intersections.empty:
        return

    # Deduplicate to keep one point per crossing pair
    unique_intersections = intersections.drop_duplicates(subset=['RouteId', 'Intersecting_Route'])
    combined_rows = []
    seen_pairs = set()
    uix = unique_intersections.set_index(['RouteId', 'Intersecting_Route'], drop=False)

    for key, row in unique_intersections.set_index(['RouteId', 'Intersecting_Route'], drop=False).iterrows():
        route_a, route_b = key
        if key in seen_pairs:
            continue
        seen_pairs.add(key)

        # Check for the mirror record (Route B x Route A) to pair their mileposts
        mirror_key = (route_b, route_a)

        base_data = {
            "Topology_Node": row["Topology_Node"],
            "Elev_A_Ft": row["Elev_Ft"],
            "Elev_Diff_Ft": row["Elev_Diff_Ft"],
            "Lon": row["Lon"],
            "Lat": row["Lat"],
            "Route_A": route_a,
            "MP_A": row["Milepost"],
            "Route_B": route_b,
        }

        if mirror_key in uix.index and mirror_key not in seen_pairs:
            mirror_row = uix.loc[mirror_key]
            if isinstance(mirror_row, pd.DataFrame):
                mirror_row = mirror_row.iloc[0]
            seen_pairs.add(mirror_key)
            base_data["MP_B"] = mirror_row["Milepost"]
            base_data["Elev_B_Ft"] = mirror_row["Elev_Ft"]
        else:
            base_data["MP_B"] = None
            base_data["Elev_B_Ft"] = None

        combined_rows.append(base_data)

    # Build the GeoDataFrame and export
    df_int = pd.DataFrame(combined_rows)
    if not df_int.empty:
        gdf_int = gpd.GeoDataFrame(
            df_int,
            geometry=gpd.points_from_xy(df_int.Lon, df_int.Lat),
            crs="EPSG:4326"
        )
        write_gdf_with_retry(gdf_int, out_path, driver="GPKG")
        logging.info(f"Saved Intersections GPKG to: {out_path}")

def _save_map_if_under_limit(m, out_path: str, max_size_mb: float, map_label: str) -> bool:
    """
    Renders a folium.Map to HTML in memory and writes it only if it is
    under max_size_mb, so an oversized map never fills the disk.

    The HTML maps are previews made after the CSV/GeoJSON/GPKG exports are
    saved, so any rendering failure is logged and skipped rather than
    ending a long run at its last step.

    Returns True if the file was written, False if it was skipped (either
    for size or because rendering itself failed).
    """
    try:
        html_str = m.get_root().render()
    except Exception as e:
        logging.warning(
            f"Could not render {map_label}: {type(e).__name__}: {e}\n"
            f"  The same data is still available in the CSV/GeoJSON/GPKG "
            f"exports for this state -- only this HTML preview was skipped."
        )
        return False

    size_mb = len(html_str.encode("utf-8")) / 1e6

    if size_mb > max_size_mb:
        logging.warning(
            f"Skipped saving {map_label} ({size_mb:.0f} MB, exceeds "
            f"MAX_MAP_HTML_SIZE_MB={max_size_mb:.0f}): {out_path}\n"
            f"  The same data is still available in the CSV/GeoJSON/GPKG "
            f"exports for this state -- only this HTML preview was skipped."
        )
        return False

    try:
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(html_str)
        logging.info(f"Saved {map_label} ({size_mb:.0f} MB) to: {out_path}")
        return True
    except OSError as e:
        logging.warning(
            f"Could not write {map_label} to {out_path}: {e}\n"
            f"  The same data is still available in the CSV/GeoJSON/GPKG "
            f"exports for this state -- only this HTML preview was skipped."
        )
        return False


def generate_html_map(df_h, df_v, df_g, df_vtx, out_html, params):
    """
    Generates three separate HTML map files instead of one combined map:
    - out_html: horizontal/vertical curves only.
    - out_html with "_grades" inserted before the extension: grade
      segments colored by signed grade.
    - out_html with "_intersections" inserted before the extension:
      at-grade and grade-separated (interchange) markers only.

    The intersections are on their own map because on dense state
    networks they would make a combined file too large to open in a
    browser.
    """
    import numpy as np
    out_html_curves = out_html
    base, ext = os.path.splitext(out_html)
    out_html_intersections = f"{base}_intersections{ext}"
    out_html_grades = f"{base}_grades{ext}"

    color_map = {'A': 'green', 'B': '#a6d96a', 'C': '#fdae61', 'D': '#d7191c', 'E': '#9e0142', 'F': 'purple'}

    # ============================================================
    # BASEMAP
    # ============================================================
    # Esri World Light Gray Base: free, needs no key, and a muted
    # background the class colors read well against. The OpenStreetMap
    # tile servers do not allow programmatic use at this scale, and CARTO's
    # free tiles now require a key. Another source can be set with
    # params["BASEMAP_TILE_URL"] and params["BASEMAP_ATTR"]. A full URL is
    # used rather than a provider name, whose spelling varies between
    # folium/xyzservices versions.
    #
    # This service has tiles only through zoom 16; past that it returns a
    # "Map data not yet available" image. MAX_NATIVE_ZOOM tells Leaflet to
    # enlarge the zoom-16 tiles instead, and MAX_ZOOM is set higher so the
    # curve and intersection layers can still be zoomed in further.
    default_tile_url = "https://services.arcgisonline.com/ArcGIS/rest/services/Canvas/World_Light_Gray_Base/MapServer/tile/{z}/{y}/{x}"
    default_tile_attr = 'Tiles &copy; Esri &mdash; Esri, HERE, Garmin, &copy; OpenStreetMap contributors, and the GIS user community'
    basemap_tile_url = params.get("BASEMAP_TILE_URL", default_tile_url)
    basemap_attr = params.get("BASEMAP_ATTR", default_tile_attr)
    basemap_max_native_zoom = params.get("BASEMAP_MAX_NATIVE_ZOOM", 16)
    basemap_max_zoom = params.get("BASEMAP_MAX_ZOOM", 19)

    def _add_basemap(m):
        """Attach the configured basemap tile layer to a fresh folium.Map
        that was created with tiles=None, using max_native_zoom so deep
        zoom never hits a "Map data not yet available" placeholder tile."""
        folium.TileLayer(
            tiles=basemap_tile_url,
            attr=basemap_attr,
            name="Basemap",
            max_zoom=basemap_max_zoom,
            max_native_zoom=basemap_max_native_zoom,
            control=False,
        ).add_to(m)

    # ============================================================
    # RUN-PARAMETERS CORNER PANEL (core.rat_param_summary), on every map
    # ============================================================
    route_ids_present = None
    if not df_h.empty and "RouteId" in df_h.columns:
        route_ids_present = df_h["RouteId"].nunique()
    elif not df_v.empty and "RouteId" in df_v.columns:
        route_ids_present = df_v["RouteId"].nunique()
    route_label = None
    if route_ids_present == 1:
        route_label = str((df_h["RouteId"].iloc[0] if not df_h.empty else df_v["RouteId"].iloc[0]))
    elif route_ids_present and route_ids_present > 1:
        route_label = f"{route_ids_present:,} routes"
    param_summary = build_run_param_summary(params, route_id=route_label)
    param_panel_html = render_param_panel_html(param_summary)

    # ============================================================
    # MAP 1: CURVES
    # ============================================================
    m_curves = folium.Map(
        location=[39.5, -98.35], zoom_start=6,
        tiles=None, max_zoom=basemap_max_zoom,
    )
    _add_basemap(m_curves)
    bounds_list = []

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
        ).add_to(m_curves)

    if not df_v.empty and "geometry" in df_v.columns:
        gdf_v = gpd.GeoDataFrame(df_v, geometry="geometry", crs="EPSG:4326")
        if params.get("SIMPLIFY_GEOMETRY", True):
            gdf_v["geometry"] = gdf_v["geometry"].simplify(tolerance=0.00005, preserve_topology=True)
        bounds_list.append(gdf_v.total_bounds)
        folium.GeoJson(
            gdf_v, name="Vertical Curves",
            style_function=lambda f: {"color": color_map.get(f['properties'].get('VC_Class', 'A'), 'gray'), "weight": 4},
            tooltip=folium.GeoJsonTooltip(fields=[c for c in ["RouteId", "Calibrated_Start_MP", "Calibrated_End_MP", "Type", "VC_Class", "Design_Speed_Supported_Mph", "SSD_Provided_Ft", "K_Value", "Alg_Diff"] if c in gdf_v.columns])
        ).add_to(m_curves)

    if bounds_list:
        b = np.vstack(bounds_list)
        minx, miny, maxx, maxy = np.min(b[:, 0]), np.min(b[:, 1]), np.max(b[:, 2]), np.max(b[:, 3])
        m_curves.fit_bounds([[miny, minx], [maxy, maxx]])

    legend_html = '''<div style="position: fixed; bottom: 50px; right: 50px; width: 160px; border:2px solid grey; z-index:9999; background:white; padding: 10px; font-family: sans-serif; font-size: 12px;">
    <b>Curve Class</b><br><i style="background:green; width:10px; height:10px; display:inline-block;"></i> A<br><i style="background:#a6d96a; width:10px; height:10px; display:inline-block;"></i> B<br><i style="background:#fdae61; width:10px; height:10px; display:inline-block;"></i> C<br><i style="background:#d7191c; width:10px; height:10px; display:inline-block;"></i> D<br><i style="background:#9e0142; width:10px; height:10px; display:inline-block;"></i> E<br><i style="background:purple; width:10px; height:10px; display:inline-block;"></i> F<br>
    <hr style="margin: 5px 0; border: 0; border-top: 1px solid #ccc;"><b>Curve Type</b><br><i style="border-top: 3px solid black; width:15px; display:inline-block; margin-bottom: 3px;"></i> Simple / Vertical<br><i style="border-top: 3px dashed black; width:15px; display:inline-block; margin-bottom: 3px;"></i> Compound (H)<br>
    <hr style="margin: 5px 0; border: 0; border-top: 1px solid #ccc;">Horizontal: degree of curve.<br>Vertical: design speed supported<br>(A 70+ mph ... F 25 mph or less).</div>'''
    m_curves.get_root().html.add_child(folium.Element(legend_html))
    m_curves.get_root().html.add_child(folium.Element(param_panel_html))
    folium.LayerControl().add_to(m_curves)
    max_map_mb = params.get("MAX_MAP_HTML_SIZE_MB", 500.0)
    _save_map_if_under_limit(m_curves, out_html_curves, max_map_mb, "Curves Map")

    # ============================================================
    # MAP 2: GRADES
    # ============================================================
    if df_g is not None and not df_g.empty and "geometry" in df_g.columns:
        m_grades = folium.Map(
            location=[39.5, -98.35], zoom_start=6,
            tiles=None, max_zoom=basemap_max_zoom,
        )
        _add_basemap(m_grades)
        gdf_g = gpd.GeoDataFrame(df_g[df_g["geometry"].notna()], geometry="geometry", crs="EPSG:4326")
        if params.get("SIMPLIFY_GEOMETRY", True):
            gdf_g["geometry"] = gdf_g["geometry"].simplify(tolerance=0.00005, preserve_topology=True)
        gdf_g["Grade_Label"] = gdf_g["Grade_Pct"].map(lambda g: f"{g:+.2f}%")
        gdf_g["Signed_Class"] = [
            "A" if b == "A" else ("+" if g > 0 else "-") + b
            for g, b in zip(gdf_g["Grade_Pct"], gdf_g["Grade_Bin"])
        ]
        folium.GeoJson(
            gdf_g[["RouteId", "Calibrated_Start_MP", "Calibrated_End_MP", "Length_ft",
                   "Grade_Label", "Grade_Bin", "Signed_Class", "geometry"]],
            name="Grades",
            style_function=lambda f: {"color": GRADE_SIGN_COLORS.get(f['properties'].get('Signed_Class'), 'gray'),
                                      "weight": 4},
            tooltip=folium.GeoJsonTooltip(
                fields=["RouteId", "Calibrated_Start_MP", "Calibrated_End_MP", "Grade_Label", "Grade_Bin", "Length_ft"],
                aliases=["Route", "Start RP", "End RP", "Grade", "Grade Class", "Length (ft)"],
            ),
        ).add_to(m_grades)
        minx, miny, maxx, maxy = gdf_g.total_bounds
        m_grades.fit_bounds([[miny, minx], [maxy, maxx]])

        rows = []
        for key, label in GRADE_LEGEND_ROWS:
            rows.append(f'<i style="background:{GRADE_SIGN_COLORS[key]}; width:10px; height:10px; '
                        f'display:inline-block;"></i> {label}<br>')
        grade_legend_html = (
            '<div style="position: fixed; bottom: 50px; right: 50px; width: 190px; border:2px solid grey; '
            'z-index:9999; background:white; padding: 10px; font-family: sans-serif; font-size: 12px;">'
            '<b>Grade</b><br>' + "".join(rows) +
            '<hr style="margin: 5px 0; border: 0; border-top: 1px solid #ccc;">'
            '+ rises, - falls, in the direction of increasing reference point.</div>'
        )
        m_grades.get_root().html.add_child(folium.Element(grade_legend_html))
        m_grades.get_root().html.add_child(folium.Element(param_panel_html))
        folium.LayerControl().add_to(m_grades)
        _save_map_if_under_limit(m_grades, out_html_grades, max_map_mb, "Grades Map")

    # ============================================================
    # MAP 3: INTERSECTIONS (At-Grade + Grade-Separated)
    # ============================================================
    m_intersections = folium.Map(
        location=[39.5, -98.35], zoom_start=6,
        tiles=None, max_zoom=basemap_max_zoom,
    )
    _add_basemap(m_intersections)
    marker_bounds = []

    if not df_vtx.empty and 'Topology_Node' in df_vtx.columns:
        # Unknown Clearance (5-10 ft elevation difference) gets its own
        # layer so those crossings can be reviewed.
        intersections = df_vtx[df_vtx['Topology_Node'].isin(["At-Grade", "Grade-Separated", "Unknown Clearance"])]

        # One point per pair of crossing routes keeps the map responsive.
        if not intersections.empty:
            # Deduplicated on the route pair, not the crossing route alone:
            # the latter would merge every crossing of one route (every
            # street crossing 14th St NW in DC, say) into a single marker.
            unique_intersections = intersections.drop_duplicates(subset=['RouteId', 'Intersecting_Route'])

            # Combine the two markers that represent the same physical
            # crossing (RouteId=A/Intersecting_Route=B and RouteId=B/
            # Intersecting_Route=A) into a single marker showing both
            # routes' own reference points, instead of one marker per
            # route per crossing.
            combined_rows = []
            seen_pairs = set()
            uix = unique_intersections.set_index(['RouteId', 'Intersecting_Route'], drop=False)
            for key, row in unique_intersections.set_index(['RouteId', 'Intersecting_Route'], drop=False).iterrows():
                route_a, route_b = key
                if key in seen_pairs:
                    continue
                seen_pairs.add(key)
                mirror_key = (route_b, route_a)
                if mirror_key in uix.index and mirror_key not in seen_pairs:
                    mirror_row = uix.loc[mirror_key]
                    if isinstance(mirror_row, pd.DataFrame):
                        mirror_row = mirror_row.iloc[0]
                    seen_pairs.add(mirror_key)
                    combined_rows.append({
                        "Topology_Node": row["Topology_Node"],
                        "elev_a": row["Elev_Ft"],
                        "elev_b": mirror_row["Elev_Ft"],
                        "elev_diff": row["Elev_Diff_Ft"],
                        "Lat": row["Lat"], "Lon": row["Lon"],
                        "route_a": route_a, "mp_a": row["Milepost"],
                        "route_b": route_b, "mp_b": mirror_row["Milepost"],
                        "lat_b": mirror_row["Lat"], "lon_b": mirror_row["Lon"],
                    })
                else:
                    combined_rows.append({
                        "Topology_Node": row["Topology_Node"],
                        "elev_a": row["Elev_Ft"],
                        "elev_b": None,
                        "elev_diff": None,
                        "Lat": row["Lat"], "Lon": row["Lon"],
                        "route_a": route_a, "mp_a": row["Milepost"],
                        "route_b": route_b, "mp_b": None,
                        "lat_b": None, "lon_b": None,
                    })

            # GeoJson circle-marker layers rather than individual
            # folium.Marker objects, each of which carries its own icon and
            # popup HTML. At California scale (about 656,000 crossings) the
            # Marker version filled the disk; this is about 74% smaller.
            # The points do not cluster at low zoom.
            df_int = pd.DataFrame(combined_rows)
            gdf_int = gpd.GeoDataFrame(
                df_int,
                geometry=gpd.points_from_xy(df_int["Lon"], df_int["Lat"]),
                crs="EPSG:4326",
            )

            for node_type, color, layer_name in [
                ("At-Grade", "green", "At-Grade Intersections"),
                ("Grade-Separated", "orange", "Interchanges (Grade-Separated)"),
                ("Unknown Clearance", "gray", "Unknown Clearance (Needs Review)"),
            ]:
                subset = gdf_int[gdf_int["Topology_Node"] == node_type]
                if subset.empty:
                    continue
                folium.GeoJson(
                    subset,
                    name=layer_name,
                    marker=folium.CircleMarker(radius=4, fill=True, fill_opacity=0.85, weight=1),
                    style_function=lambda f, c=color: {"color": c, "fillColor": c},
                    tooltip=folium.GeoJsonTooltip(
                        fields=["Topology_Node", "route_a", "route_b"],
                        aliases=["Type", "Route A", "Route B"],
                    ),
                    popup=folium.GeoJsonPopup(
                        fields=["Topology_Node", "route_a", "mp_a", "elev_a", "route_b", "mp_b", "elev_b", "elev_diff"],
                        aliases=["Node Type", "Route A", "MP A", "Elev A (ft)", "Route B", "MP B", "Elev B (ft)", "Elev Diff (ft)"],
                    ),
                ).add_to(m_intersections)
                marker_bounds.extend(zip(subset["Lat"], subset["Lon"]))

    if marker_bounds:
        lats = [p[0] for p in marker_bounds]
        lons = [p[1] for p in marker_bounds]
        m_intersections.fit_bounds([[min(lats), min(lons)], [max(lats), max(lons)]])

    m_intersections.get_root().html.add_child(folium.Element(param_panel_html))
    folium.LayerControl().add_to(m_intersections)
    _save_map_if_under_limit(m_intersections, out_html_intersections, max_map_mb, "Intersections Map")

def generate_dashboard(df_h, df_v, df_g, df_health, out_html, out_dir, state_fips="", df_vtx=None, params=None):
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

    # --- Chart 2: Vertical curve classes ---
    chart_v = os.path.join(out_dir, "chart_vertical_bins.png")
    if not df_v.empty and "VC_Class" in df_v.columns:
        vc = df_v["VC_Class"].value_counts().reindex(BIN_ORDER, fill_value=0)
        fig, ax = plt.subplots(figsize=(6, 4))
        ax.bar(BIN_ORDER, vc.values, color=BIN_COLORS)
        ax.set_title("Vertical Curve Count by Vertical Curve Class\n(design speed supported)")
        ax.set_xlabel("Vertical Curve Class"); ax.set_ylabel("Count")
        _save(fig, chart_v)

    # --- Chart 2b: Miles by grade class ---
    chart_g = os.path.join(out_dir, "chart_grade_miles.png")
    if df_g is not None and not df_g.empty and "Grade_Bin" in df_g.columns:
        miles = (df_g.groupby("Grade_Bin")["Length_ft"].sum() / 5280.0).reindex(BIN_ORDER, fill_value=0)
        fig, ax = plt.subplots(figsize=(6, 4))
        ax.bar(BIN_ORDER, miles.values, color=BIN_COLORS)
        ax.set_title("Miles by Grade Class")
        ax.set_xlabel("Grade Class"); ax.set_ylabel("Route-Miles")
        _save(fig, chart_g)

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
    if not df_v.empty and "VC_Class" in df_v.columns and "FSystem" in df_v.columns:
        ct = pd.crosstab(df_v["FSystem"], df_v["VC_Class"]).reindex(columns=BIN_ORDER, fill_value=0)
        fig, ax = plt.subplots(figsize=(8, 5))
        ct.plot(kind="bar", stacked=True, color=BIN_COLORS, ax=ax)
        ax.set_title("Vertical Curves by Functional System")
        ax.set_ylabel("Count"); ax.set_xlabel(FS_LABEL)
        ax.legend(title="Vertical Curve Class", bbox_to_anchor=(1.05, 1), loc="upper left")
        ax.tick_params(axis="x", rotation=0)
        _save(fig, chart_fs_v)

    # --- Chart 4b: Grade class miles by functional system ---
    chart_fs_g = os.path.join(out_dir, "chart_fsystem_grades.png")
    if df_g is not None and not df_g.empty and "Grade_Bin" in df_g.columns and "FSystem" in df_g.columns:
        ct = (df_g.pivot_table(index="FSystem", columns="Grade_Bin", values="Length_ft",
                               aggfunc="sum", fill_value=0.0)
              .reindex(columns=BIN_ORDER, fill_value=0.0) / 5280.0)
        fig, ax = plt.subplots(figsize=(8, 5))
        ct.plot(kind="bar", stacked=True, color=BIN_COLORS, ax=ax)
        ax.set_title("Grade Class by Functional System")
        ax.set_ylabel("Route-Miles"); ax.set_xlabel(FS_LABEL)
        ax.legend(title="Grade Class", bbox_to_anchor=(1.05, 1), loc="upper left")
        ax.tick_params(axis="x", rotation=0)
        _save(fig, chart_fs_g)

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
        df_v["K_Value"].clip(upper=1500).plot(kind="hist", bins=40, color="teal",
                                               edgecolor="black", ax=ax)
        ax.set_title("Vertical Curve Diagnostics: K-Value Distribution (capped at 1,500)")
        ax.set_xlabel("K-Value (ft per % grade change)"); ax.set_ylabel("Frequency")
        ax.grid(True, linestyle="--", alpha=0.5)
        _save(fig, chart_hist_v)

    # --- Chart 7b: IRI Reported vs. Micro Jitter ---
    # IRI_Reported is the HPMS section value (in/mi), drawn as a step across
    # each section. Micro_Jitter_Inches is the RAT roughness proxy at each
    # vertex: a height in inches (filtered difference between the repaired
    # DEM profile and the smoothed profile), not a slope statistic like IRI.
    # The two have different units, so each gets its own axis.
    chart_iri_jitter = os.path.join(out_dir, "chart_iri_jitter.png")
    try:
        if df_vtx is not None and not df_vtx.empty:
            has_iri     = "IRI_Reported"      in df_vtx.columns
            has_jitter  = "Micro_Jitter_Inches" in df_vtx.columns
            has_mp      = "Milepost"           in df_vtx.columns

            if has_mp and (has_iri or has_jitter):
                fig, ax = plt.subplots(figsize=(12, 5))
                handles = []

                if has_iri:
                    iri_vals = pd.to_numeric(df_vtx["IRI_Reported"], errors="coerce")
                    handles += ax.step(
                        df_vtx["Milepost"], iri_vals,
                        color="darkorange", linewidth=1.4, alpha=0.85, where="post",
                        label="IRI Reported (HPMS section value, in/mi)",
                    )
                    ax.set_ylabel("IRI Reported (in/mi)", color="darkorange")
                    ax.tick_params(axis="y", colors="darkorange")

                if has_jitter:
                    ax_j = ax.twinx() if has_iri else ax
                    jitter_vals = pd.to_numeric(df_vtx["Micro_Jitter_Inches"], errors="coerce")
                    handles += ax_j.plot(
                        df_vtx["Milepost"], jitter_vals,
                        color="steelblue", linewidth=0.9, alpha=0.75,
                        label="Micro Jitter (RAT roughness proxy, in)",
                    )
                    ax_j.set_ylabel("Micro Jitter (in)", color="steelblue")
                    ax_j.tick_params(axis="y", colors="steelblue")

                ax.set_title("Ride Quality: HPMS IRI and RAT Micro Jitter by Milepost")
                ax.set_xlabel("Milepost (mi)")
                # Legend on the top axis so the second line is not drawn over it.
                (ax_j if has_jitter else ax).legend(handles=handles, loc="upper right")
                ax.grid(True, linestyle="--", alpha=0.4)
                _save(fig, chart_iri_jitter)
    except Exception:
        chart_iri_jitter = None

    # --- Chart 8: Curve density per 100 route-miles ---
    chart_density = os.path.join(out_dir, "chart_curve_density.png")
    try:
        # Route-miles come from the grade segments, which cover every
        # processed mile once.
        if (not df_h.empty and "FSystem" in df_h.columns
                and not df_v.empty and "FSystem" in df_v.columns
                and df_g is not None and not df_g.empty and "FSystem" in df_g.columns):
            route_miles    = df_g.groupby("FSystem")["Length_ft"].sum() / 5280.0
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
        if not df_v.empty and "VC_Class" in df_v.columns:
            v_num  = df_v["VC_Class"].map(bin_to_num).dropna()
            counts = [((v_num <= vv).sum() / len(v_num)) * 100 for vv in range(1, 7)]
            axes[1].step(list("ABCDEF"), counts, where="post", color="steelblue", linewidth=2)
            axes[1].set_ylim(0, 105)
            axes[1].yaxis.set_major_formatter(mticker.PercentFormatter())
            axes[1].set_title("Cumulative Vertical Curve Class Distribution")
            axes[1].set_xlabel("Vertical Curve Class (A = Mildest)")
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
            # Numeric positions with FS labels: matplotlib logs a message for
            # every axis of text labels that look like numbers.
            ax.bar(np.arange(len(fs_v)), [pct.get(f, 0) for f in fs_v],
                   color="slategray", alpha=0.85)
            ax.set_xticks(np.arange(len(fs_v)))
            ax.set_xticklabels([str(f) for f in fs_v])
            ax.yaxis.set_major_formatter(mticker.PercentFormatter())
            ax.set_title("Compound Horizontal Curve Percentage by Functional System")
            ax.set_ylabel("% of Curves Classified as Compound")
            ax.set_xlabel(FS_LABEL)
            ax.grid(True, linestyle="--", alpha=0.4, axis="y")
            _save(fig, chart_compound)
    except Exception:
        chart_compound = None

    # --- Chart 12: Raw DEM vs. Smoothed Elevation by Milepost ---
    # Plots both the smoothed elevation profile and the raw DEM sample at each
    # vertex. The raw DEM line uses a warm coral red at reduced weight so it
    # reads clearly as reference data beneath the smoothed profile.
    chart_elev = os.path.join(out_dir, "chart_elevation_profile.png")
    try:
        if df_vtx is not None and not df_vtx.empty:
            has_elev     = "Elev_Ft"     in df_vtx.columns
            has_elev_raw = "Elev_Raw_Ft" in df_vtx.columns
            has_mp       = "Milepost"    in df_vtx.columns

            if has_mp and (has_elev or has_elev_raw):
                fig, ax = plt.subplots(figsize=(14, 5))

                if has_elev_raw:
                    ax.plot(
                        df_vtx["Milepost"],
                        pd.to_numeric(df_vtx["Elev_Raw_Ft"], errors="coerce"),
                        color="#e05050", linewidth=0.8, alpha=0.50,
                        label="Raw DEM Elevation (ft)",
                        zorder=2,
                    )

                if has_elev:
                    ax.plot(
                        df_vtx["Milepost"],
                        pd.to_numeric(df_vtx["Elev_Ft"], errors="coerce"),
                        color="steelblue", linewidth=1.5, alpha=0.90,
                        label="Smoothed Elevation (ft)",
                        zorder=3,
                    )

                ax.set_title("Elevation Profile: Smoothed vs. Raw DEM")
                ax.set_xlabel("Milepost (mi)")
                ax.set_ylabel("Elevation (ft)")
                ax.legend(loc="upper right")
                ax.grid(True, linestyle="--", alpha=0.4)
                _save(fig, chart_elev)
    except Exception:
        chart_elev = None
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
  <p><strong>Route-miles in grade segments:</strong> {(df_g["Length_ft"].sum() / 5280.0 if df_g is not None and not df_g.empty else 0):,.1f}</p>
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
<p style="color:#6c757d; max-width:800px;">Percentage of curves at or below each class.
Vertical curves are classed by the highest design speed their stopping sight distance supports
(A = 70 mph or more, F = 25 mph or less).</p>
{_img("chart_severity_cdf.png", "900px")}

<h3 style="margin-top:40px;">Severity by Functional System</h3>
<div style="display:flex; flex-wrap:wrap; gap:20px;">
  {_img("chart_fsystem_horizontal.png")} {_img("chart_fsystem_vertical.png")}
</div>

<h3 style="margin-top:40px;">Grades</h3>
<p style="color:#6c757d; max-width:800px;">Route-miles in each HPMS grade class. Grades are
measured between vertical points of intersection.</p>
<div style="display:flex; flex-wrap:wrap; gap:20px;">
  {_img("chart_grade_miles.png")} {_img("chart_fsystem_grades.png")}
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

<h3 style="margin-top:40px;">Ride Quality: IRI and Micro Jitter</h3>
<p style="color:#6c757d; max-width:800px;">Orange step line (left axis) shows the HPMS-reported IRI value
per section, in inches per mile. Blue line (right axis) shows Micro Jitter, the RAT roughness proxy:
the high-pass filtered difference between the repaired DEM profile and the smoothed profile, as a
moving RMS height in inches. The two have different units and are not directly comparable; Micro
Jitter is for spotting relative changes along the route. Where IRI data is absent the orange line
will not appear.</p>
{_img("chart_iri_jitter.png", "900px")}

<h3 style="margin-top:40px;">Elevation Profile</h3>
<p style="color:#6c757d; max-width:800px;">Blue line is the smoothed elevation profile used
for all curve and grade analysis. Coral red line is the raw USGS DEM sample at each original
vertex, shown for reference. Divergence between the two indicates bridge structures, noise,
or sections where the smoother significantly modified the input geometry.</p>
{_img("chart_elevation_profile.png", "900px")}

<h3 style="margin-top:40px;">Advanced Diagnostics</h3>
<div style="display:flex; flex-wrap:wrap; gap:20px;">
  {_img("chart_scatter_horizontal.png")} {_img("chart_hist_vertical.png")}
</div>

</body></html>"""

    if params is not None:
        # Run-parameters corner panel (core.rat_param_summary), as on the maps.
        try:
            param_summary = build_run_param_summary(params, route_id=None)
            html = html.replace("</body></html>", render_param_panel_html(param_summary) + "\n</body></html>")
        except Exception as e:
            logging.warning(f"Could not render run-parameters panel on dashboard: {e}")

    with open(out_html, "w", encoding="utf-8") as f:
        f.write(html)
    logging.info(f"Saved Dashboard to: {out_html}")

def export_vertices_gpkg_batched(df_vtx: pd.DataFrame, out_path: str, batch_size: int = 50_000) -> None:
    """
    Write the vertices DataFrame to a GPKG file in row batches without ever
    building a full GeoDataFrame from the entire table.

    The normal geopandas path (gpd.GeoDataFrame -> .to_file) internally
    consolidates every numeric column into one contiguous array before
    writing anything. At 141M rows x 14 float64 columns that requires
    ~15GB of contiguous RAM and crashes even on well-provisioned machines.

    This function opens the GPKG file once with fiona, then feeds rows
    through batch_size at a time -- creating Point objects and writing
    only for the current slice, then discarding it before loading the next.
    Peak memory is bounded to one batch (~50k rows) regardless of the
    total vertex count.

    Parameters
    ----------
    df_vtx : pd.DataFrame
        The full vertices dataframe. Must contain 'Lon' and 'Lat' columns.
        Must NOT already have a 'geometry' column (this function manages
        geometry creation internally).
    out_path : str
        Full path to the output .gpkg file (including extension).
    batch_size : int
        Number of rows to process at a time. Default 50,000 is a safe
        balance between memory use and write overhead.
    """
    if df_vtx.empty or "Lon" not in df_vtx.columns or "Lat" not in df_vtx.columns:
        logging.warning("export_vertices_gpkg_batched: df_vtx is empty or missing Lon/Lat columns -- skipping.")
        return

    # Build the fiona schema from the dataframe columns.
    # Every column except Lon/Lat gets included as a property;
    # Lon and Lat are captured in the geometry instead.
    prop_cols = [c for c in df_vtx.columns if c not in ("Lon", "Lat")]

    def _fiona_type(dtype):
        """Map a numpy/pandas dtype to a fiona field type string."""
        if pd.api.types.is_integer_dtype(dtype):
            return "int"
        if pd.api.types.is_float_dtype(dtype):
            return "float"
        return "str"

    schema = {
        "geometry": "Point",
        "properties": {col: _fiona_type(df_vtx[col].dtype) for col in prop_cols},
    }

    with warnings.catch_warnings():
        # Some fiona 1.9 releases warn here ("removed in version 2.0");
        # the file is written correctly either way.
        warnings.filterwarnings("ignore", message=".*removed in version 2.0.*")
        crs = fiona.crs.CRS.from_epsg(4326)
    n_total = len(df_vtx)

    logging.info(
        f"Writing {n_total:,} vertices to GPKG in batches of {batch_size:,}: {out_path}"
    )

    # Retries if OneDrive, antivirus or an indexer briefly locks the file
    # (as write_gdf_with_retry does in rat_core.py). mode="w" starts a new
    # file each time, so the whole batch loop can safely be repeated.
    max_attempts = 5
    for attempt in range(max_attempts):
        try:
            with warnings.catch_warnings():
                # As above, for the CRS handling inside fiona.open.
                warnings.filterwarnings("ignore", message=".*removed in version 2.0.*")
                dst = fiona.open(out_path, mode="w", driver="GPKG", schema=schema, crs=crs)
            with dst:
                n_written = 0
                for start in range(0, n_total, batch_size):
                    end = min(start + batch_size, n_total)
                    batch = df_vtx.iloc[start:end]

                    lons = batch["Lon"].to_numpy()
                    lats = batch["Lat"].to_numpy()

                    # Build property arrays once per column rather than once per row
                    prop_arrays = {}
                    for col in prop_cols:
                        arr = batch[col].to_numpy()
                        prop_arrays[col] = arr

                    records = []
                    for i in range(len(batch)):
                        geom = {"type": "Point", "coordinates": (float(lons[i]), float(lats[i]))}
                        props = {}
                        for col in prop_cols:
                            val = prop_arrays[col][i]
                            if val is None or (isinstance(val, float) and np.isnan(val)):
                                props[col] = None
                            elif schema["properties"][col] == "int":
                                props[col] = int(val)
                            elif schema["properties"][col] == "float":
                                props[col] = float(val)
                            else:
                                props[col] = str(val) if val is not None else None
                        records.append({"geometry": geom, "properties": props})

                    dst.writerecords(records)
                    n_written += (end - start)

                    if n_written % 500_000 == 0 or n_written == n_total:
                        logging.info(f"  ...wrote {n_written:,}/{n_total:,} vertices")
            break
        except (PermissionError, OSError) as e:
            if attempt == max_attempts - 1:
                raise
            logging.warning(
                f"Could not write {out_path} (attempt {attempt + 1}/{max_attempts}): "
                f"{e}. Retrying in 3s..."
            )
            time.sleep(3)

    logging.info(f"Saved Vertices GPKG to: {out_path}")


def export_geo(df, out_base, params):
    if df.empty or "geometry" not in df.columns: return
    gdf = gpd.GeoDataFrame(df, geometry="geometry", crs="EPSG:4326")
    if params.get("OUT_GEOJSON"):
        write_gdf_with_retry(gdf, out_base + ".geojson", driver="GeoJSON")
        logging.info(f"Saved GeoJSON to: {out_base}.geojson")
    if params.get("OUT_GPKG"):
        write_gdf_with_retry(gdf, out_base + ".gpkg", driver="GPKG")
        logging.info(f"Saved GPKG to: {out_base}.gpkg")
    # No shapefile export, whatever OUT_SHP says: shapefiles cut field
    # names to 10 characters (Calibrated_Start_MP and Calibrated_End_MP
    # collide) and limit the .dbf to 2 GB. GPKG has neither limit. The
    # Blender shapefile from hpms_4d_enricher_cli.py is separate.

# ---------------------------------------------------------------------------
# Parallel worker
# ---------------------------------------------------------------------------
def process_chunk_worker(route_id: str, item: dict, route_subset: pd.DataFrame, base_dem_dir: str, params: dict):
    """
    Runs one work item (a continuity chunk, or one padded piece of a long
    chunk) in a worker process.

    Makes sure the 1 m DEM cells the item touches are in the shared cache;
    normally run_state_alignment() has already downloaded them.

    NBI/NTI structures come from core.rat_core.WORKER_NBI_NTI_GDF, set once
    per process by init_worker_nbi(), so the structures table is not sent
    with every task.
    """
    import os
    import logging
    from core.rat_core import download_dem_cell, get_line_dem_cells, WORKER_NBI_NTI_GDF
    from apps.rat_alignment_cli import process_chunk

    h_curves, v_curves, segments, health_stats, vtx_dfs = [], [], [], [], []
    shared_cache_dir = os.path.join(base_dem_dir, "align_1m_cache")

    try:
        require_dem_cache(shared_cache_dir)
        for lon_deg, lat_deg in get_line_dem_cells([item["geom"]]):
            download_dem_cell(lon_deg, lat_deg, shared_cache_dir)

        h, v, g, health, vtx = process_chunk(
            route_id, item, shared_cache_dir, params, route_subset,
            nbi_nti_gdf=WORKER_NBI_NTI_GDF,
        )
        h_curves.extend(h)
        v_curves.extend(v)
        segments.extend(g)
        health_stats.extend(health)
        vtx_dfs.extend(vtx)
    except Exception as e:
        # A lost DEM folder stops the run (DemCacheUnavailable reaches the
        # main process) rather than failing every remaining task.
        raise_if_dem_cache_lost(shared_cache_dir, e)
        logging.error(f"Worker failed on route {route_id} Part {item.get('part_id')}: {e}")

    if not h_curves and not v_curves and not vtx_dfs:
        # Nothing produced: check the folder is still there (a missing
        # folder makes elevation reads fail quietly).
        raise_if_dem_cache_lost(shared_cache_dir)

    return h_curves, v_curves, segments, health_stats, vtx_dfs

# ---------------------------------------------------------------------------
# State run
# ---------------------------------------------------------------------------
def run_state_alignment(state_fips: str, out_dir: str, dem_dir: str, user_params: dict, local_df: pd.DataFrame = None):
    """
    Processes one state (or a local file). The output version is claimed
    when the run starts (core.rat_core.claim_output_version, on the
    horizontal CSV name), so two runs can never write to the same files;
    the placeholder is removed if the run ends without writing it.
    """
    state_out_dir = os.path.join(out_dir, f"Output_State_{state_fips}")
    stamp = datetime.now().strftime("%Y%m%d")
    version, claimed = claim_output_version(
        state_out_dir, f"alignment_horizontal_{state_fips}_{stamp}_v{{v}}.csv")
    try:
        _run_state_alignment(state_fips, out_dir, dem_dir, user_params, local_df, stamp, version)
    finally:
        others = [f for f in glob.glob(os.path.join(state_out_dir, f"alignment_*_{state_fips}_{stamp}_v{version}.*"))
                  if os.path.abspath(f) != os.path.abspath(claimed)]
        if others and os.path.exists(claimed) and os.path.getsize(claimed) == 0:
            # The run wrote its other files but found no horizontal curves:
            # keep the version taken, as an empty table.
            with open(claimed, "w", encoding="utf-8") as f:
                f.write("RouteId,Start_Dist,End_Dist,Length_m\n")
        else:
            release_output_version(claimed)


def _run_state_alignment(state_fips: str, out_dir: str, dem_dir: str, user_params: dict,
                         local_df: pd.DataFrame, stamp: str, version: int):
    from shapely.wkt import loads
    from concurrent.futures import ProcessPoolExecutor

    state_out_dir = os.path.join(out_dir, f"Output_State_{state_fips}")

    h_csv     = os.path.join(state_out_dir, f"alignment_horizontal_{state_fips}_{stamp}_v{version}.csv")
    v_csv     = os.path.join(state_out_dir, f"alignment_vertical_{state_fips}_{stamp}_v{version}.csv")
    g_csv     = os.path.join(state_out_dir, f"alignment_grades_{state_fips}_{stamp}_v{version}.csv")
    vtx_csv   = os.path.join(state_out_dir, f"alignment_vertices_{state_fips}_{stamp}_v{version}.csv")
    score_csv = os.path.join(state_out_dir, f"alignment_section_scores_{state_fips}_{stamp}_v{version}.csv")
    spans_csv = os.path.join(state_out_dir, f"alignment_structure_spans_{state_fips}_{stamp}_v{version}.csv")
    order_audit_csv = os.path.join(state_out_dir, f"alignment_order_audit_{state_fips}_{stamp}_v{version}.csv")
    logging.info(f"Output version: v{version}")

    logging.info(f"\n{'='*60}\n=== ALIGNMENT PROCESSING: STATE {state_fips} ===\n{'='*60}")

    user_params_local = user_params.copy()
    user_params_local["STATE_FIPS"] = state_fips
    params = build_params(user_params_local)
    logging.info(describe_smoothing_factors(params))

    try:
        if local_df is not None:
            df = local_df.copy()
            # The same state and facility filters a Socrata query applies.
            df = filter_local_df_to_state(df, state_fips)
            df = apply_facility_fsystem_filters(
                df,
                facility_filter=user_params.get("FACILITY_TYPE_FILTER"),
                fsystem_filter=user_params.get("FSYSTEM_FILTER"),
                include_missing_facility=bool(user_params.get("FACILITY_TYPE_INCLUDE_MISSING", False)),
                include_missing_fsystem=bool(user_params.get("FSYSTEM_INCLUDE_MISSING", False)),
            )
        else:
            df = fetch_socrata_state(
                state_fips,
                user_params.get("SOCRATA_TOKEN") or ENV_SOCRATA_TOKEN,
                facility_type_filter=user_params.get("FACILITY_TYPE_FILTER"),
                fsystem_filter=user_params.get("FSYSTEM_FILTER"),
                include_missing_facility=bool(user_params.get("FACILITY_TYPE_INCLUDE_MISSING", False)),
                include_missing_fsystem=bool(user_params.get("FSYSTEM_INCLUDE_MISSING", False)),
            )
    except Exception as e:
        logging.error(f"Failed to fetch dataset coordinates: {e}")
        return

    routes = df["RouteId"].dropna().unique().tolist()
    logging.info(f"Loaded {len(df):,} segments across {len(routes):,} routes.")

    if len(df) == 0:
        logging.error(
            f"No rows loaded for State FIPS {state_fips}; stopping. If a cached "
            f"Socrata extract is being reused, it may be for a different "
            f"state. Turn off 'Reuse Socrata Cache' or delete the cached "
            f"extract to download State FIPS {state_fips} again."
        )
        return

    # ------------------------------------------------------------------
    # SHAPEID ORDERING AUDIT
    # ------------------------------------------------------------------
    # Lists the routes whose ShapeId order could not be used, up front.
    # Those routes are still processed, in ShapeId order, with a new chunk
    # at each gap larger than the join tolerance. Only routes with a missing
    # ShapeId or unreadable geometry are joined by location instead
    # (stitch_linestrings_by_shapeid logs each). The table is for review only.
    try:
        order_audit = audit_route_ordering(df)
        n_flagged = int(order_audit["Any_Violation"].sum())
        order_audit.to_csv(order_audit_csv, index=False)
        if n_flagged > 0:
            logging.warning(
                f"ShapeId ordering audit: {n_flagged} of {len(order_audit)} routes "
                f"in state {state_fips} have reference point breaks, gaps between records, or "
                f"records that do not join end to start (still processed, in ShapeId order). "
                f"See {order_audit_csv} for details."
            )
        else:
            logging.info(
                f"ShapeId ordering audit: all {len(order_audit)} routes in state "
                f"{state_fips} passed cleanly. See {order_audit_csv}."
            )
    except Exception as e:
        logging.warning(f"ShapeId ordering audit failed to run: {e}")

    # ------------------------------------------------------------------
    # NBI/NTI structures and DEM tiles
    # ------------------------------------------------------------------
    logging.info("Reading the state's geometry...")
    df['geom_obj'] = df['WKT'].apply(loads)

    # The national NBI/NTI tables are cut down to the state's padded
    # bounding box (see core.rat_core.compute_padded_bbox).
    state_bbox = compute_padded_bbox(df['geom_obj'])
    nbi_nti_gdf = fetch_nbi_nti_state(state_fips, bbox=state_bbox)

    # Every DEM tile the run needs is downloaded here, one at a time,
    # before the workers start. The tiles are the ones the roads pass
    # through (get_line_dem_cells), not those in a bounding box, which for a
    # long diagonal road takes in many tiles it never enters, would break
    # across the antimeridian (western Aleutians) and would pull in
    # open-ocean tiles around Hawaii and Puerto Rico.
    shared_cache_dir = os.path.join(dem_dir, "align_1m_cache")
    require_dem_cache(shared_cache_dir)
    from core.rat_core import download_dem_cell, get_line_dem_cells
    needed_cells = get_line_dem_cells(df['geom_obj'])
    logging.info(f"Pre-fetching {len(needed_cells)} DEM tile(s) for the state footprint "
                 f"before starting parallel processing...")
    for lon_deg, lat_deg in sorted(needed_cells):
        download_dem_cell(lon_deg, lat_deg, shared_cache_dir)
    logging.info("DEM download complete.")

    # ------------------------------------------------------------------
    # Continuity chunks, built once per route from all of its records.
    # A chunk (a "Part") ends only where the route itself breaks, or where
    # a very long chunk is split for parallel processing
    # (split_chunks_for_parallelism), whose pieces do not overlap.
    # ------------------------------------------------------------------
    logging.info(f"  Building route continuity chunks for {df['RouteId'].nunique():,} routes...")
    work_items = []      # list of (route_id, work_item_dict, item_subset)
    has_mp_cols = "Start_MP" in df.columns and "End_MP" in df.columns
    pad_mi = float(params.get("CHUNK_SPLIT_PADDING_FT", 20000.0)) / 5280.0

    if not has_mp_cols:
        # Without Start_MP/End_MP each work item gets all of its route's
        # records (see the trimming below), which can run out of memory.
        logging.warning(
            "Start_MP/End_MP columns not found -- per-item subset trimming is "
            "disabled and every work item will receive its FULL route subset. "
            "This can reintroduce OOM risk on states with long, heavily-split "
            "routes (e.g. California)."
        )

    n_routes, n_mixed = 0, 0
    for route_id, route_subset in df.groupby("RouteId"):
        all_chunks = build_route_continuity_chunks(route_id, route_subset, params)
        if not all_chunks:
            continue
        n_routes += 1
        n_mixed += route_spans_several_fsystems(all_chunks)
        for item in split_chunks_for_parallelism(route_id, all_chunks, params):
            # Each item gets only the route records within its milepost
            # window plus padding. The structure checks
            # (repair_structure_spans) use only records overlapping the
            # item, so the result is the same, but sending every piece of
            # a long route all of the route's records runs out of memory
            # on states like California.
            if has_mp_cols:
                lo = item["calib_start_mp"] - pad_mi
                hi = item["calib_end_mp"] + pad_mi
                item_subset = route_subset[
                    (route_subset["End_MP"] > lo) & (route_subset["Start_MP"] < hi)
                ]
            else:
                item_subset = route_subset
            work_items.append((route_id, item, item_subset))

    log_fsystem_summary(n_mixed, n_routes)
    logging.info(f"Network partitioned into {len(work_items):,} chunk-based parallel tasks.")

    all_h, all_v, all_g, all_health, all_vtx = [], [], [], [], []
    max_workers = user_params.get("MAX_WORKERS", max(1, os.cpu_count() - 2))

    from core.rat_core import GridProgressWindow
    progress_ui = GridProgressWindow(f"RAT v3.3 Engine: State {state_fips}", len(work_items))

    # init_worker_nbi (core.rat_core) gives each worker process the
    # state's NBI/NTI structures once, not once per task.
    with ProcessPoolExecutor(
        max_workers=max_workers,
        initializer=init_worker_nbi,
        initargs=(nbi_nti_gdf,)
    ) as executor:
        # Tasks are submitted a few at a time (core.rat_core.run_bounded_parallel),
        # which keeps memory in check on large states like California.
        MAX_IN_FLIGHT = max_workers * 4
        completed = 0

        def _submit(work_item):
            route_id, item, item_subset = work_item
            return executor.submit(process_chunk_worker, route_id, item, item_subset, dem_dir, params)

        def _on_done(work_item, fut):
            nonlocal completed
            route_id, item, item_subset = work_item
            completed += 1
            progress_ui.update(completed)

            if completed % 50 == 0:
                logging.info(f"  ...Processed {completed}/{len(work_items)} route chunks")
            try:
                h, v, g, health, vtx_dfs = fut.result()
                all_h.extend(h); all_v.extend(v); all_g.extend(g); all_health.extend(health)
                all_vtx.extend(vtx_dfs)
            except DemCacheUnavailable:
                raise
            except Exception as e:
                logging.error(f"Chunk task {(route_id, item.get('part_id'))} failed: {e}")

        run_bounded_parallel(work_items, _submit, _on_done, max_in_flight=MAX_IN_FLIGHT)

    progress_ui.close()

    df_h = pd.DataFrame(all_h)
    df_v = pd.DataFrame(all_v)
    df_g = pd.DataFrame(all_g)
    df_health = pd.DataFrame(all_health)
    df_vtx = pd.concat(all_vtx, ignore_index=True) if all_vtx else pd.DataFrame()

    # Results arrive in any order, so they are sorted. Work items do not
    # overlap (see process_chunk); drop_duplicates is only a safeguard.
    if not df_vtx.empty:
        df_vtx.drop_duplicates(subset=["RouteId", "Milepost"], keep="first", inplace=True)
        if "Part" not in df_vtx.columns:
            df_vtx["Part"] = 0.0
            logging.warning("Part column missing from vertices - routes may have no detected curves. Adding default Part values.")
        df_vtx.sort_values(by=["RouteId", "Part", "Milepost"], inplace=True)

    if not df_h.empty:
        df_h.drop_duplicates(subset=["RouteId", "Calibrated_Start_MP", "Calibrated_End_MP"], keep="first", inplace=True)
        df_h.sort_values(by=["RouteId", "Calibrated_Start_MP"], inplace=True)

    if not df_v.empty:
        df_v.drop_duplicates(subset=["RouteId", "Calibrated_Start_MP", "Calibrated_End_MP"], keep="first", inplace=True)
        df_v.sort_values(by=["RouteId", "Calibrated_Start_MP"], inplace=True)

    if not df_g.empty:
        df_g.sort_values(by=["RouteId", "Part", "Calibrated_Start_MP"], inplace=True)
        df_g["Length_ft"] = df_g["Length_ft"].round(2)
        df_g["Grade_Pct"] = df_g["Grade_Pct"].round(3)

    # ==================================================================
    # INTERCHANGE TOPOLOGY MAPPER (Network-Level KDTree)
    # ==================================================================
    if not df_vtx.empty and "Lon" in df_vtx.columns:
        logging.info(f"[{state_fips}] Building KDTree to map interchange topology for {len(df_vtx):,} vertices...")
        from scipy.spatial import cKDTree

        # NaN, not the text "None", so points with no crossing stay null
        # before any CSV round trip.
        df_vtx["Topology_Node"] = np.nan
        df_vtx["Intersecting_Route"] = np.nan

        pts = df_vtx[["Lon", "Lat"]].to_numpy()
        tree = cKDTree(pts)

        routes = df_vtx["RouteId"].to_numpy()
        elevs = df_vtx["Elev_Ft"].to_numpy()
        # A point is on a structure if a bridge/tunnel span was detected
        # there or an HPMS/NBI record matches it (e.g. a bridge over it).
        on_structure = np.zeros(len(df_vtx), dtype=bool)
        if "Structure_Kind" in df_vtx.columns:
            on_structure |= df_vtx["Structure_Kind"].to_numpy() != "None"
        if "Structure_Record" in df_vtx.columns:
            on_structure |= df_vtx["Structure_Record"].to_numpy() != "None"

        topology = np.full(len(df_vtx), np.nan, dtype=object)
        intersecting = np.full(len(df_vtx), np.nan, dtype=object)
        elev_diff = np.full(len(df_vtx), np.nan, dtype=float)

        interchange_count = 0
        at_grade_count = 0
        unknown_count = 0

        # query_ball_point() in blocks rather than query_pairs(), which
        # builds every pair at once and runs out of memory on a large
        # state (98.8 million vertices).
        CHUNK = 10_000
        n = len(pts)
        for start in range(0, n, CHUNK):
            end = min(start + CHUNK, n)
            neighbors_list = tree.query_ball_point(pts[start:end], r=0.000045)
            for local_idx, neighbors in enumerate(neighbors_list):
                i = start + local_idx
                for j in neighbors:
                    if j <= i:   # process each pair once
                        continue
                    r1, r2 = routes[i], routes[j]
                    if r1 == r2:
                        continue
                    dz = abs(elevs[i] - elevs[j])

                    # A structure at either point (a detected span or an
                    # HPMS/NBI record) makes the crossing grade-separated,
                    # whatever the elevation difference. At some stacked
                    # interchanges (I-5 x I-210, the I-10 x I-110 Four
                    # Level) the matched points' elevations do not show the
                    # deck height.
                    has_structure = on_structure[i] or on_structure[j]

                    if has_structure or dz > 10.0:
                        topo_type = "Grade-Separated"
                        interchange_count += 1
                    elif dz <= 5.0:
                        topo_type = "At-Grade"
                        at_grade_count += 1
                    else:
                        # 5-10 ft: could be an at-grade crossing with DEM
                        # noise, road crown or a slight offset between the
                        # two centerlines. Reported for review.
                        topo_type = "Unknown Clearance"
                        unknown_count += 1

                    topology[i] = topo_type
                    topology[j] = topo_type
                    intersecting[i] = r2
                    intersecting[j] = r1
                    elev_diff[i] = dz
                    elev_diff[j] = dz

        df_vtx["Topology_Node"] = topology
        df_vtx["Intersecting_Route"] = intersecting
        df_vtx["Elev_Diff_Ft"] = elev_diff
        logging.info(
            f"[{state_fips}] Topology mapped: {interchange_count} grade-separated nodes, "
            f"{at_grade_count} at-grade nodes, {unknown_count} unknown-clearance nodes."
        )

    # Vertices CSV
    if params.get("OUT_CSV", True) and not df_vtx.empty:
        df_vtx.to_csv(vtx_csv, index=False)
        logging.info(f"Saved Vertices CSV to: {vtx_csv}")

    # Optional structure span list (CSV only for statewide runs; the PDF
    # version is produced by plan/profile runs).
    if params.get("OUT_STRUCTURE_REPORT", False) and not df_vtx.empty:
        try:
            spans_df = build_structure_span_table(df_vtx)
            spans_df.to_csv(spans_csv, index=False)
            logging.info(f"Saved structure span list ({len(spans_df):,} span(s)) to: {spans_csv}")
        except Exception as e:
            logging.warning(f"Could not build structure span list: {e}")

    if params.get("OUT_CSV", True):
        score_cols = ["RouteId", "FSystem", "Start_MP", "End_MP"]
        available  = [c for c in score_cols if c in df.columns]
        df_scores  = assign_section_scores(df[available].copy(), df_h, df_g)
        df_scores.to_csv(score_csv, index=False)
        logging.info(f"Saved Section Scores CSV to: {score_csv}")

    if params.get("OUT_CSV", True):
        if not df_h.empty:
            df_h.to_csv(h_csv, index=False)
            logging.info(f"Saved Horizontal CSV to: {h_csv}")
        if not df_v.empty:
            df_v.to_csv(v_csv, index=False)
            logging.info(f"Saved Vertical CSV to: {v_csv}")
        if not df_g.empty:
            df_g.drop(columns=["geometry"], errors="ignore").to_csv(g_csv, index=False)
            logging.info(f"Saved Grades CSV to: {g_csv}")

    export_geo(df_h, os.path.join(state_out_dir, f"alignment_horizontal_{state_fips}_{stamp}_v{version}"), params)
    export_geo(df_v, os.path.join(state_out_dir, f"alignment_vertical_{state_fips}_{stamp}_v{version}"), params)
    export_geo(df_g, os.path.join(state_out_dir, f"alignment_grades_{state_fips}_{stamp}_v{version}"), params)

    # Vertices GPKG (OUT_VTX_SPATIAL), written 50,000 rows at a time with
    # fiona. Writing it through geopandas runs out of memory on large
    # states (141 million rows for Nebraska, all functional systems).
    if params.get("OUT_VTX_SPATIAL", False) and not df_vtx.empty \
            and "Lon" in df_vtx.columns and "Lat" in df_vtx.columns:
        vtx_gpkg_path = os.path.join(
            state_out_dir, f"alignment_vertices_{state_fips}_{stamp}_v{version}.gpkg"
        )
        export_vertices_gpkg_batched(df_vtx, vtx_gpkg_path)

    # Intersections GPKG
    if params.get("OUT_GPKG", True) and not df_vtx.empty and 'Topology_Node' in df_vtx.columns:
        intersections_gpkg_path = os.path.join(
            state_out_dir, f"alignment_intersections_{state_fips}_{stamp}_v{version}.gpkg"
        )
        export_intersections_gpkg(df_vtx, intersections_gpkg_path)

    if params.get("OUT_HTML_MAP", True) and not df_h.empty:
        generate_html_map(df_h, df_v, df_g, df_vtx, os.path.join(state_out_dir, f"alignment_map_{state_fips}_{stamp}_v{version}.html"), params)
    if params.get("OUT_DASHBOARD", True) and not df_h.empty:
        generate_dashboard(df_h, df_v, df_g, df_health, os.path.join(state_out_dir, f"alignment_dashboard_{state_fips}_{stamp}_v{version}.html"), state_out_dir, state_fips=state_fips, df_vtx=df_vtx, params=params)

    # Structure span report
    if not df_vtx.empty and "Structure_Kind" in df_vtx.columns:
        kinds = df_vtx["Structure_Kind"].to_numpy()
        tiers = df_vtx["Structure_Tier"].to_numpy()
        new_span = np.r_[True, (kinds[1:] != kinds[:-1]) | (tiers[1:] != tiers[:-1])]
        span_starts = np.flatnonzero((kinds != "None") & new_span)
        span_counts = pd.Series([f"{kinds[i]} ({tiers[i]})" for i in span_starts]).value_counts()
        logging.info("========================================")
        logging.info("=== STRUCTURE SPAN REPORT ===")
        logging.info("========================================")
        if span_counts.empty:
            logging.info("No bridge or tunnel spans detected.")
        for label, count in span_counts.items():
            logging.info(f"{label}: {count}")

    if not df_v.empty and 'False_Valley_Warning' in df_v.columns:
        false_valleys = int(df_v['False_Valley_Warning'].sum())
        if false_valleys > 0:
            logging.warning(f"QA/QC FLAG: {false_valleys} SAG curve(s) overlap a bridge span.")
        else:
            logging.info("QA/QC: No SAG curves overlap a bridge span.")

    # ==========================================
    # SUMMARY
    # ==========================================
    logging.info("========================================")
    logging.info("=== SUMMARY ===")
    logging.info("========================================")

    if not df_vtx.empty and 'Topology_Node' in df_vtx.columns:
        topo_counts = df_vtx['Topology_Node'].value_counts()
        grade_sep = topo_counts.get('Grade-Separated', 0)
        at_grade = topo_counts.get('At-Grade', 0)
        unknown = topo_counts.get('Unknown Clearance', 0)
        logging.info(
            f"Network Topology: {grade_sep:,} Grade-Separated points, {at_grade:,} At-Grade points, "
            f"{unknown:,} Unknown Clearance points (needs review)."
        )

    if not df_vtx.empty and 'Available_Sight_Dist_Ft' in df_vtx.columns:
        total_pts = len(df_vtx)
        blind_pts = (df_vtx['Available_Sight_Dist_Ft'] < 400).sum()
        pct_blind = (blind_pts / total_pts * 100) if total_pts > 0 else 0
        logging.info(f"Safety Profile:   {pct_blind:.1f}% of the network has an Available Sight Distance under 400 ft.")

    if not df_h.empty and 'Transition_Type' in df_h.columns:
        spirals = (df_h['Transition_Type'] == 'Spiral').sum()
        logging.info(f"Design Geometry:  {spirals:,} Engineered Spiral Transition curves detected.")

    logging.info("========================================")
    logging.info(f"Finished State {state_fips}.")


def main():
    parser = argparse.ArgumentParser(description="RAT Bulk Alignment Engine CLI")
    parser.add_argument("--input", default=None, help="Local HPMS file")
    parser.add_argument("--outdir", required=True, help="Output Directory")
    parser.add_argument("--demdir", required=True, help="DEM Directory")
    parser.add_argument("--state", default=None, help="State FIPS")
    parser.add_argument("--params_json", default=None, help="JSON overrides")
    parser.add_argument("--debug", action="store_true",
                        help="Enable debug-level logging for troubleshooting")
    args = parser.parse_args()

    if args.debug:
        logging.basicConfig(level=logging.DEBUG,
                           format="%(asctime)s - %(levelname)s - %(funcName)s: %(message)s")
        logging.debug("Debug logging enabled - verbose output mode")
    else:
        logging.basicConfig(level=logging.INFO,
                           format="%(asctime)s - %(levelname)s: %(message)s")

    user_params = {}
    if args.params_json and os.path.exists(args.params_json):
        with open(args.params_json, 'r') as f: user_params = json.load(f)

    user_params.update({"OUTPUT_DIR": args.outdir, "DEM_DIR": args.demdir})
    if args.state: user_params["STATE_FIPS"] = args.state

    try:
        if args.input:
            logging.info(f"Processing local file: {args.input}")
            df = load_local_hpms(args.input)

            # State FIPS from the GUI settings, or "LOCAL"
            state_id = str(user_params.get("STATE_FIPS", "")).strip()
            if state_id == "00" or not state_id:
                state_id = "LOCAL"

            run_state_alignment(state_id, args.outdir, args.demdir, user_params, local_df=df)
        elif args.state:
            states = ALL_FIPS if args.state.upper() == "ALL" else [args.state.zfill(2)]
            for s in states: run_state_alignment(s, args.outdir, args.demdir, user_params)
        else:
            logging.error("No input specified. Give --input or --state.")
    except DemCacheUnavailable as e:
        # One message instead of one per task. Nothing is written for the
        # state that was running; states already finished keep their files.
        logging.error(
            f"{e} The run was stopped and no outputs were written for the state in progress. "
            f"Check the connection to the DEM folder (or use a folder on a local drive) and run it again."
        )
        raise SystemExit(1)


if __name__ == "__main__":
    main()