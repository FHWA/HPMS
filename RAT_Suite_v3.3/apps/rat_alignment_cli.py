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
import fiona
import fiona.crs
from shapely.geometry import LineString, Point
import folium
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from concurrent.futures import ProcessPoolExecutor, as_completed

# Module-level global for NBI/NTI structures. Set once per worker process via
# _init_worker_nbi() rather than passed as an argument to every task submission.
# Re-pickling a 743,677-record GeoDataFrame for each of potentially millions of
# grid cell tasks causes severe memory pressure and IPC overhead at national scale.
_WORKER_NBI_NTI_GDF = None


def _init_worker_nbi(nbi_nti_gdf):
    """
    ProcessPoolExecutor initializer. Runs once when each worker process starts,
    setting the module-level NBI/NTI reference for that process only. This means
    the structures GeoDataFrame is pickled and sent exactly max_workers times
    (once per process) rather than once per task submission.
    """
    global _WORKER_NBI_NTI_GDF
    _WORKER_NBI_NTI_GDF = nbi_nti_gdf

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
    merge_close_chunks,
    smooth_plan_profile_from_linestring,
    analyze_horizontal_curvature,
    analyze_vertical_parabolic,
    merge_horizontal_curves,
    get_appropriate_utm_zone,
    download_dems,
    fetch_socrata_state,   
    load_local_hpms,       
    filter_local_df_to_state,
    apply_facility_fsystem_filters,
    FEET_PER_METER,
    calculate_headings,
    get_tangent_grade,
    fetch_nbi_nti_state,
)
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s: %(message)s")

# ===========================================================================
# VERTICES HELPER  (mirrors rat_plan_profile_cli.build_vertices_df)
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
    iri_proxy       = res.get("iri_proxy_in", np.zeros(n_pts, dtype=float))
    asd             = res.get("available_sight_dist_ft", np.zeros(n_pts, dtype=float))
    
    # Map tier integers to strings efficiently
    tier_map = np.array(["None", "1_HPMS", "2_NBI", "3_DIP"])
    mapped_tiers = tier_map[np.clip(structure_tier, 0, 3)]

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
        "Structure_Tier":           mapped_tiers,
        "Micro_Jitter_Inches":      np.round(iri_proxy, 4),
        "Available_Sight_Dist_Ft":  np.round(asd, 1),
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

    # NOTE: This used to loop over every detected curve with .iterrows() and,
    # for EACH curve, build a boolean mask across the ENTIRE df_out segment
    # table (O(curves * segments), done twice). For a large state with tens
    # of thousands of segments and thousands of curves, that loop could run
    # for hours with no intermediate logging -- indistinguishable from a
    # hang. Replaced with a single vectorized merge, scoped by RouteId so
    # only segments/curves sharing a route are ever compared, then an
    # MP-overlap filter and a groupby-max to keep the worst (highest-letter)
    # bin per section. Same output semantics, no more per-curve Python loop.
    df_out["_orig_idx"] = df_out.index

    def _apply_worst_bin(df_curves, start_col, end_col, bin_col, out_col):
        required = {"RouteId", start_col, end_col, bin_col}
        if df_curves.empty or not required.issubset(df_curves.columns):
            return
        left = df_out[["_orig_idx", "RouteId", "Start_MP", "End_MP"]]
        right = df_curves[["RouteId", start_col, end_col, bin_col]]
        merged = left.merge(right, on="RouteId", how="inner")
        if merged.empty:
            return
        overlap = (merged["End_MP"] > merged[start_col]) & (merged["Start_MP"] < merged[end_col])
        merged = merged.loc[overlap]
        if merged.empty:
            return
        merged = merged.assign(_rank=merged[bin_col].map(BIN_RANK).fillna(0))
        worst_idx = merged.groupby("_orig_idx")["_rank"].idxmax()
        worst_bins = merged.loc[worst_idx, ["_orig_idx", bin_col]].set_index("_orig_idx")[bin_col]
        df_out.loc[worst_bins.index, out_col] = worst_bins.values

    # --- Horizontal ---
    _apply_worst_bin(df_h, "Calibrated_Start_MP", "Calibrated_End_MP", "Bin", "H_Curve_Bin")

    # --- Vertical ---
    _apply_worst_bin(df_v, "Calibrated_Start_MP", "Calibrated_End_MP", "Grade_Bin", "V_Grade_Bin")

    df_out = df_out.drop(columns=["_orig_idx"])
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
def process_route(route_id: str, subset: pd.DataFrame, dem_dir: str, params: dict, nbi_nti_gdf: gpd.GeoDataFrame = None):
    # NOTE: subset is intentionally NOT re-sorted by Start_MP here. Sorting
    # by Start_MP assumes milepost is monotonically increasing along the
    # full route, but several states reset their linear reference posts at
    # county boundaries -- for those routes, sorting by Start_MP scrambles
    # segments into county-local order rather than true physical/geographic
    # order, producing wildly out-of-sequence vertex chains (this was the
    # root cause of the I-5/I-90/etc. milepost-continuity failures in
    # Washington: 144 backward jumps on I-5 alone, none of them at real
    # intersections).
    #
    # Geometry stitching (stitch_linestrings_ordered) is spatial and
    # order-independent -- it chains segments by matching coordinates, not
    # by Start_MP. But milepost LABELING for each resulting chunk used to
    # assume chunk-processing order == ascending-milepost order, which holds
    # for states that submit segments ascending (WA) but not for states that
    # submit descending (DC: confirmed via raw Socrata extract, where
    # Begin_Point/End_Point run from each route's high end down to 0 across
    # the entire submission). That mismatched assumption was the cause of DC's
    # 795-route milepost-continuity failures, separate from the WA sort bug.
    #
    # Fix: each chunk's true milepost range and direction is now derived
    # from its own contributing rows' actual Start_MP/End_MP (via
    # stitch_linestrings_ordered's return_indices), then chunks are
    # processed in ascending chunk_low_mp order -- so the result is correct
    # regardless of submission order, ascending, descending, or otherwise.
    # NOTE: previously called unconditionally on every invocation of this
    # function -- once per route, per grid cell. process_alignment_tile_worker()
    # already downloads the correct 1-degree 1m tile for this cell via
    # download_high_res_dem_tile() and sets HIGH_RES_MODE=True before ever
    # calling this function, so this 10m fallback (a different tile, different
    # cache directory, different file naming) was being redundantly invoked
    # thousands of times across a single state run -- once per route in every
    # grid cell -- for a tile that was never actually needed. Gating this
    # behind HIGH_RES_MODE makes it a genuine fallback again, only used by
    # callers (e.g. the raw-DEM-fallback path) that don't set that flag.
    if not params.get("HIGH_RES_MODE", False):
        download_dems(subset["WKT"].tolist(), dem_dir)
    
    f_sys = int(subset["FSystem"].mode()[0])
    
    state_val = "LOCAL"
    for col in subset.columns:
        if col.lower() in ["state", "state_fips", "state_id", "stateid"]:
            state_val = str(subset[col].iloc[0])
            break
    
    if state_val == "LOCAL":
        state_val = str(params.get("STATE_FIPS", "LOCAL"))
    
    all_chunks = []
    lines, chunk_indices = stitch_linestrings_ordered(
        subset["WKT"].tolist(), return_indices=True,
        start_mps=subset["Start_MP"].tolist(), end_mps=subset["End_MP"].tolist(),
    )
    for g, idxs in zip(lines, chunk_indices):
        contrib = subset.iloc[idxs]
        chunk_low_mp  = float(contrib[["Start_MP", "End_MP"]].min().min())
        chunk_high_mp = float(contrib[["Start_MP", "End_MP"]].max().max())

        # Determine whether this chunk's geometry, as built by
        # stitch_linestrings_ordered() (which only knows about spatial
        # proximity, not milepost), happens to run from its low-MP end to
        # its high-MP end, or the reverse. Submission order is NOT a
        # reliable signal here -- some states submit ascending (WA),
        # others descending (DC) -- so this is decided per-chunk from the
        # contributing rows' own Start_MP/End_MP, not from processing order.
        first_row = contrib.iloc[0]
        last_row  = contrib.iloc[-1]
        first_mid = (float(first_row["Start_MP"]) + float(first_row["End_MP"])) / 2.0
        last_mid  = (float(last_row["Start_MP"])  + float(last_row["End_MP"]))  / 2.0
        if first_mid > last_mid:
            # This chunk was assembled high-MP-end-first -- reverse its
            # coordinate order so milepost increases through the output,
            # matching the convention every other chunk/route follows.
            g = LineString(list(g.coords)[::-1])

        all_chunks.append({
            "geom": g, "f_sys": f_sys, "state_id": state_val,
            "chunk_low_mp": chunk_low_mp, "chunk_high_mp": chunk_high_mp,
        })

    if not all_chunks:
        return [], [], [], []

    # Second pass: bridge any chunks left disconnected by a small margin
    # just over stitch_linestrings_ordered()'s snap_tol -- most visibly,
    # closed-loop routes (e.g. the roadway around a traffic circle) whose
    # digitized start/end point don't quite coincide, which otherwise
    # fracture into 2-3 overlapping chunks instead of one continuous ring.
    # Routes with genuinely separate physical pieces (e.g. county-line
    # resets, typically miles apart) are far outside this tolerance and
    # are unaffected.
    all_chunks = merge_close_chunks(all_chunks, merge_tol_ft=500.0, route_id=route_id)

    # Process chunks in true ascending-milepost order (not whatever order
    # stitch_linestrings_ordered() happened to assemble them in) so that
    # Dist_Mi accumulates correctly across chunk boundaries and Part
    # numbering reflects physical route order.
    all_chunks.sort(key=lambda c: c["chunk_low_mp"])

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
    route_s = float(subset["Start_MP"].min())
    route_e = float(subset["End_MP"].max())
    global_continuous_dist_m = route_s * 1609.344
    
    for part_idx, info in enumerate(all_chunks):
        chunk_length_m = info["length_m"]
        # Each chunk's milepost range comes directly from its own
        # constituent HPMS segments (set above from chunk_low_mp/
        # chunk_high_mp) -- not from this chunk's position in processing
        # order. This is what makes the result correct for both ascending
        # (WA) and descending (DC) submission order, and for routes with
        # multiple disjoint physical pieces.
        chunk_start_mp = info["chunk_low_mp"]
        chunk_end_mp   = info["chunk_high_mp"]
        
        # Capture cumulative distance at start of this chunk for vertex table
        global_start_dist_m = global_continuous_dist_m

        res = smooth_plan_profile_from_linestring(
            info["geom"], dem_dir, params, info["f_sys"],
            route_id=route_id, chunk_s_mp=chunk_start_mp, chunk_e_mp=chunk_end_mp,
            hpms_subset=subset, nbi_nti_gdf=nbi_nti_gdf
        )
        if res is None or not res.get("ok", True):
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
        except ValueError as e:
            # Missing elevation data or invalid coordinates
            logging.debug(f"Could not compute RMSE health stats for route {route_id}: {e}")
        except KeyError as e:
            # Result dict missing expected fields
            logging.warning(f"Result dictionary missing field {e} for route {route_id}")
        except Exception as e:
            # Unexpected error in RMSE calculation
            logging.error(f"Unexpected error computing RMSE stats for route {route_id}: {type(e).__name__}: {e}")
            
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
            c["Part"] = chunk_start_mp  # globally unique per chunk -- see note below
            c["Calibrated_Start_MP"] = chunk_start_mp + p0 * (chunk_end_mp - chunk_start_mp)
            c["Calibrated_End_MP"] = chunk_start_mp + p1 * (chunk_end_mp - chunk_start_mp)
            c["FSystem"] = info["f_sys"]
            try:
                i0 = max(0, int(c["Start_Dist"] / spacing_m))
                i1 = min(len(coords_wgs_smooth) - 1, int(c["End_Dist"] / spacing_m))
                if i1 > i0:
                    c["geometry"] = LineString(coords_wgs_smooth[i0:i1+1])
            except (TypeError, ValueError) as e:
                # Distance values can't be converted to integers
                logging.warning(f"Cannot create geometry for horizontal curve on route {route_id}: invalid distance values")
            except IndexError as e:
                # Indices out of bounds for coordinate array
                logging.warning(f"Coordinate index out of bounds for horizontal curve on route {route_id}")
            except Exception as e:
                logging.error(f"Unexpected error creating horizontal curve geometry for route {route_id}: {type(e).__name__}")
                
        for c in v_curves:
            p0 = c["Start_Dist"] / total_len
            p1 = c["End_Dist"] / total_len
            c["RouteId"] = route_id
            c["Part"] = chunk_start_mp  # globally unique per chunk -- see note below
            c["Calibrated_Start_MP"] = chunk_start_mp + p0 * (chunk_end_mp - chunk_start_mp)
            c["Calibrated_End_MP"] = chunk_start_mp + p1 * (chunk_end_mp - chunk_start_mp)
            c["FSystem"] = info["f_sys"]
            try:
                i0 = max(0, int(c["Start_Dist"] / spacing_m))
                i1 = min(len(coords_wgs_smooth) - 1, int(c["End_Dist"] / spacing_m))
                if i1 > i0:
                    c["geometry"] = LineString(coords_wgs_smooth[i0:i1+1])
            except (TypeError, ValueError) as e:
                # Distance values can't be converted to integers
                logging.warning(f"Cannot create geometry for vertical curve on route {route_id}: invalid distance values")
            except IndexError as e:
                # Indices out of bounds for coordinate array
                logging.warning(f"Coordinate index out of bounds for vertical curve on route {route_id}")
            except Exception as e:
                logging.error(f"Unexpected error creating vertical curve geometry for route {route_id}: {type(e).__name__}")
                
        h_all.extend(h_curves)
        v_all.extend(v_curves)

        try:
            vtx_df = build_vertices_df(
                res, route_id, info["f_sys"], info["state_id"],
                chunk_start_mp, chunk_end_mp,
                global_start_dist_m, params,
            )
            # ... lots of code ...
            vtx_dfs.append(vtx_df)
        except KeyError as e:
            # Missing required field in the result dictionary
            logging.warning(f"Vertices build failed for route {route_id} part {part_idx+1}: missing field {e}")
        except ValueError as e:
            # Invalid data type or value in geometry operations
            logging.warning(f"Vertices build failed for route {route_id} part {part_idx+1}: invalid data - {e}")
        except Exception as exc:
            logging.error(f"Unexpected error building vertices for route {route_id} part {part_idx+1}: {type(exc).__name__}: {exc}")

        global_continuous_dist_m += total_len
        
    return h_all, v_all, health_stats, vtx_dfs

# ---------------------------------------------------------------------------
# Output Generators
# ---------------------------------------------------------------------------
def export_intersections_gpkg(df_vtx: pd.DataFrame, out_path: str):
    """
    Extracts deduplicated intersection nodes (Route A x Route B) and saves 
    them to a spatial GeoPackage (GPKG) for desktop GIS software.
    """
    if df_vtx.empty or 'Topology_Node' not in df_vtx.columns:
        return

    # Filter to only the calculated intersections
    intersections = df_vtx[df_vtx['Topology_Node'].isin(["At-Grade", "Grade-Separated"])]
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
            "Elev_Ft": row["Elev_Ft"],
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
        else:
            base_data["MP_B"] = None
            
        combined_rows.append(base_data)

    # Build the GeoDataFrame and export
    df_int = pd.DataFrame(combined_rows)
    if not df_int.empty:
        gdf_int = gpd.GeoDataFrame(
            df_int, 
            geometry=gpd.points_from_xy(df_int.Lon, df_int.Lat), 
            crs="EPSG:4326"
        )
        gdf_int.to_file(out_path, driver="GPKG")
        logging.info(f"Saved Intersections GPKG to: {out_path}")

def generate_html_map(df_h, df_v, df_vtx, out_html, params):
    """
    Generates two separate HTML map files instead of one combined map:
    - out_html: horizontal/vertical curves only.
    - out_html with "_intersections" inserted before the extension:
      at-grade and grade-separated (interchange) markers only.

    Previously both were drawn onto the same map. Once intersection-marker
    detection started correctly capturing every real crossing instead of
    silently collapsing thousands of them (see the dedup fix elsewhere in
    this file), marker counts on dense state networks grew enough that the
    combined file became too large to open reliably in a browser.
    """
    import numpy as np
    out_html_curves = out_html
    base, ext = os.path.splitext(out_html)
    out_html_intersections = f"{base}_intersections{ext}"

    color_map = {'A': 'green', 'B': '#a6d96a', 'C': '#fdae61', 'D': '#d7191c', 'E': '#9e0142', 'F': 'purple'}

    # ============================================================
    # MAP 1: CURVES
    # ============================================================
    m_curves = folium.Map(location=[39.5, -98.35], zoom_start=6, tiles="CartoDBPositron")
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
            style_function=lambda f: {"color": color_map.get(f['properties'].get('Grade_Bin', 'A'), 'gray'), "weight": 4},
            tooltip=folium.GeoJsonTooltip(fields=[c for c in ["RouteId", "Calibrated_Start_MP", "Calibrated_End_MP", "Type", "Grade_Bin", "K_Value", "Alg_Diff"] if c in gdf_v.columns])
        ).add_to(m_curves)

    if bounds_list:
        b = np.vstack(bounds_list)
        minx, miny, maxx, maxy = np.min(b[:, 0]), np.min(b[:, 1]), np.max(b[:, 2]), np.max(b[:, 3])
        m_curves.fit_bounds([[miny, minx], [maxy, maxx]])

    legend_html = '''<div style="position: fixed; bottom: 50px; right: 50px; width: 160px; border:2px solid grey; z-index:9999; background:white; padding: 10px; font-family: sans-serif; font-size: 12px;">
    <b>Curve / Grade Bin</b><br><i style="background:green; width:10px; height:10px; display:inline-block;"></i> A<br><i style="background:#a6d96a; width:10px; height:10px; display:inline-block;"></i> B<br><i style="background:#fdae61; width:10px; height:10px; display:inline-block;"></i> C<br><i style="background:#d7191c; width:10px; height:10px; display:inline-block;"></i> D<br><i style="background:#9e0142; width:10px; height:10px; display:inline-block;"></i> E<br><i style="background:purple; width:10px; height:10px; display:inline-block;"></i> F<br>
    <hr style="margin: 5px 0; border: 0; border-top: 1px solid #ccc;"><b>Curve Type</b><br><i style="border-top: 3px solid black; width:15px; display:inline-block; margin-bottom: 3px;"></i> Simple / Vertical<br><i style="border-top: 3px dashed black; width:15px; display:inline-block; margin-bottom: 3px;"></i> Compound (H)<br></div>'''
    m_curves.get_root().html.add_child(folium.Element(legend_html))
    folium.LayerControl().add_to(m_curves)
    m_curves.save(out_html_curves)
    logging.info(f"Saved Curves Map to: {out_html_curves}")

    # ============================================================
    # MAP 2: INTERSECTIONS (At-Grade + Grade-Separated)
    # ============================================================
    m_intersections = folium.Map(location=[39.5, -98.35], zoom_start=6, tiles="CartoDBPositron")
    marker_bounds = []

    if not df_vtx.empty and 'Topology_Node' in df_vtx.columns:
        # Only build markers for resolved crossings -- "Unknown Clearance"
        # (5-10ft elevation difference, ambiguous) previously fell through
        # to interchange_group along with every "None" row from the bug
        # above, both inflating the interchange count far beyond real
        # grade-separated crossings.
        intersections = df_vtx[df_vtx['Topology_Node'].isin(["At-Grade", "Grade-Separated"])]

        # Take a single representative point per host-route/crossing-route
        # pair to prevent map lag.
        if not intersections.empty:
            # NOTE: was previously deduped on subset=['Intersecting_Route']
            # alone. With only a few hundred routes in a typical state,
            # that collapsed every intersection sharing the same crossing
            # route into ONE marker, regardless of which host route or
            # physical location it was actually at -- e.g. every place
            # anything crosses 14th St NW in DC collapsed to a single
            # marker, silently dropping ~2,500 real, distinct intersections
            # (confirmed: this took the marker count from 334 to 2,864 on
            # the DC run that surfaced it). Deduping on (RouteId,
            # Intersecting_Route) instead keeps one marker per actual
            # crossing pair, which is what "prevent map lag" was meant to
            # achieve in the first place.
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
                        "Elev_Ft": row["Elev_Ft"],
                        "Lat": row["Lat"], "Lon": row["Lon"],
                        "route_a": route_a, "mp_a": row["Milepost"],
                        "route_b": route_b, "mp_b": mirror_row["Milepost"],
                        "lat_b": mirror_row["Lat"], "lon_b": mirror_row["Lon"],
                    })
                else:
                    combined_rows.append({
                        "Topology_Node": row["Topology_Node"],
                        "Elev_Ft": row["Elev_Ft"],
                        "Lat": row["Lat"], "Lon": row["Lon"],
                        "route_a": route_a, "mp_a": row["Milepost"],
                        "route_b": route_b, "mp_b": None,
                        "lat_b": None, "lon_b": None,
                    })

            # Separate feature groups so users can toggle interchanges and
            # at-grade intersections independently -- previously these were
            # added directly to the map with no way to turn them off, which
            # became unusable on dense networks with many markers.
            at_grade_group = folium.FeatureGroup(name="At-Grade Intersections", show=True)
            interchange_group = folium.FeatureGroup(name="Interchanges (Grade-Separated)", show=True)

            for row in combined_rows:
                is_at_grade = row['Topology_Node'] == "At-Grade"
                color = "green" if is_at_grade else "orange"
                icon_type = "info-sign" if is_at_grade else "road"

                if row["mp_b"] is not None:
                    popup_html = f"""
                    <div style='min-width: 220px;'>
                        <b>{row['Topology_Node']} Node</b><br>
                        <hr style='margin: 3px 0;'>
                        <b>Route A:</b> {row['route_a']} (MP {row['mp_a']:.3f})<br>
                        <b>Route B:</b> {row['route_b']} (MP {row['mp_b']:.3f})<br>
                        <b>Elevation:</b> {row['Elev_Ft']:.1f} ft
                    </div>
                    """
                    tooltip = f"{row['Topology_Node']}: {row['route_a']} x {row['route_b']}"
                else:
                    popup_html = f"""
                    <div style='min-width: 200px;'>
                        <b>{row['Topology_Node']} Node</b><br>
                        <hr style='margin: 3px 0;'>
                        <b>Route:</b> {row['route_a']}<br>
                        <b>Intersecting Route:</b> {row['route_b']}<br>
                        <b>Elevation:</b> {row['Elev_Ft']:.1f} ft<br>
                        <b>Milepost:</b> {row['mp_a']:.3f}
                    </div>
                    """
                    tooltip = f"{row['Topology_Node']}: {row['route_a']} x {row['route_b']}"

                marker = folium.Marker(
                    location=[row['Lat'], row['Lon']],
                    popup=folium.Popup(popup_html, max_width=300),
                    tooltip=tooltip,
                    icon=folium.Icon(color=color, icon=icon_type)
                )
                target_group = at_grade_group if is_at_grade else interchange_group
                marker.add_to(target_group)
                marker_bounds.append((row['Lat'], row['Lon']))

            at_grade_group.add_to(m_intersections)
            interchange_group.add_to(m_intersections)

    if marker_bounds:
        lats = [p[0] for p in marker_bounds]
        lons = [p[1] for p in marker_bounds]
        m_intersections.fit_bounds([[min(lats), min(lons)], [max(lats), max(lons)]])

    folium.LayerControl().add_to(m_intersections)
    m_intersections.save(out_html_intersections)
    logging.info(f"Saved Intersections Map to: {out_html_intersections}")

def generate_dashboard(df_h, df_v, df_health, out_html, out_dir, state_fips="", df_vtx=None):
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

    # --- Chart 7b: IRI Reported vs. Micro Jitter (combined) ---
    # Both metrics share the same unit (inches/mile equivalent) and milepost
    # axis so they are plotted together for direct comparison. IRI_Reported
    # is the HPMS-submitted section value stepped across the section; Micro
    # Jitter is the RAT-derived per-vertex proxy computed from the vertical
    # curvature of the smoothed profile.
    chart_iri_jitter = os.path.join(out_dir, "chart_iri_jitter.png")
    try:
        if df_vtx is not None and not df_vtx.empty:
            has_iri     = "IRI_Reported"      in df_vtx.columns
            has_jitter  = "Micro_Jitter_Inches" in df_vtx.columns
            has_mp      = "Milepost"           in df_vtx.columns

            if has_mp and (has_iri or has_jitter):
                fig, ax = plt.subplots(figsize=(12, 5))

                if has_jitter:
                    jitter_vals = pd.to_numeric(df_vtx["Micro_Jitter_Inches"], errors="coerce")
                    ax.plot(
                        df_vtx["Milepost"], jitter_vals,
                        color="steelblue", linewidth=0.9, alpha=0.75,
                        label="Micro Jitter (RAT-derived, in/mi equivalent)",
                    )

                if has_iri:
                    iri_vals = pd.to_numeric(df_vtx["IRI_Reported"], errors="coerce")
                    # IRI is reported per HPMS section — plot as a step line so
                    # the section boundaries are visually clear.
                    ax.step(
                        df_vtx["Milepost"], iri_vals,
                        color="darkorange", linewidth=1.4, alpha=0.85, where="post",
                        label="IRI Reported (HPMS section value, in/mi)",
                    )

                ax.set_title("Ride Quality: HPMS IRI vs. RAT Micro Jitter by Milepost")
                ax.set_xlabel("Milepost (mi)")
                ax.set_ylabel("Roughness (in/mi equivalent)")
                ax.legend(loc="upper right")
                ax.grid(True, linestyle="--", alpha=0.4)
                _save(fig, chart_iri_jitter)
    except Exception:
        chart_iri_jitter = None

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

<h3 style="margin-top:40px;">Ride Quality: IRI vs. Micro Jitter</h3>
<p style="color:#6c757d; max-width:800px;">Orange step line shows the HPMS-reported IRI value
per section. Blue line shows the RAT-derived micro jitter proxy computed from the vertical
curvature of the smoothed profile. Where IRI data is absent the orange line will not appear.</p>
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

    crs = fiona.crs.from_epsg(4326)
    n_total = len(df_vtx)
    n_written = 0

    logging.info(
        f"Writing {n_total:,} vertices to GPKG in batches of {batch_size:,}: {out_path}"
    )

    with fiona.open(out_path, mode="w", driver="GPKG", schema=schema, crs=crs) as dst:
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

    logging.info(f"Saved Vertices GPKG to: {out_path}")


def export_geo(df, out_base, params):
    if df.empty or "geometry" not in df.columns: return
    gdf = gpd.GeoDataFrame(df, geometry="geometry", crs="EPSG:4326")
    if params.get("OUT_GEOJSON"): 
        gdf.to_file(out_base + ".geojson", driver="GeoJSON")
        logging.info(f"Saved GeoJSON to: {out_base}.geojson")
    if params.get("OUT_GPKG"): 
        gdf.to_file(out_base + ".gpkg", driver="GPKG")
        logging.info(f"Saved GPKG to: {out_base}.gpkg")
    # SHP export intentionally disabled, regardless of the OUT_SHP params
    # value (the GUI checkbox's own default of False keeps getting
    # overridden by old saved run_params.json values, so a code-level
    # default change alone wouldn't actually take effect). GPKG is the
    # preferred format -- no field name length limit (SHP silently
    # truncates/collides names beyond 10 characters, e.g.
    # Calibrated_Start_MP and Calibrated_End_MP both reduce toward
    # Calibrated/Calibrat_1), no practical file size ceiling (SHP's
    # paired .dbf file has a hard 2GB limit, which WA's 94.5M-row
    # vertices file would badly exceed), and loads faster. This does not
    # affect the separate UTM "Blender" SHP export in
    # hpms_4d_enricher_cli.py, which is still actively used.

# ---------------------------------------------------------------------------
# Parallel Spatial Grid Tile Worker Companion (Ephemeral Engine)
# ---------------------------------------------------------------------------
def process_alignment_tile_worker(bbox: tuple, subset_df: pd.DataFrame, base_dem_dir: str, params: dict):
    """
    Executes in parallel. Downloads a 1-meter raster tile for the cell bounding box,
    extracts elevations for crossing route geometries, and returns data arrays to the
    central collector thread.

    Tile caching strategy:
    - Completed tiles are stored persistently in base_dem_dir/align_1m_cache/ so that
      adjacent cells sharing the same 1-degree tile don't re-download it.
    - Downloads are staged to a per-worker temp directory to prevent partial-file
      collisions when multiple workers download the same tile simultaneously.
    - Once a download completes successfully it is moved atomically to the shared cache.

    NBI/NTI structures are read from the module-level _WORKER_NBI_NTI_GDF global,
    set once per process by _init_worker_nbi() rather than passed per task -- see
    note at top of file for why this matters at national scale.
    """
    import os
    import logging
    from core.rat_core import prepare_1m_tile_for_worker
    from apps.rat_alignment_cli import process_route, _WORKER_NBI_NTI_GDF

    h_curves, v_curves, health_stats, vtx_dfs = [], [], [], []

    try:
        # Download 1-degree tile(s) covering this cell if not already cached
        # (shared with the 4D enrichment pipeline's cache -- see
        # prepare_1m_tile_for_worker's docstring).
        shared_cache_dir = prepare_1m_tile_for_worker(bbox, base_dem_dir, params)

        # Process routes -- get_1deg_tile_name in HIGH_RES_MODE finds the right tile
        for rid, route_frame in subset_df.groupby("RouteId"):
            h, v, health, vtx = process_route(rid, route_frame, shared_cache_dir, params, nbi_nti_gdf=_WORKER_NBI_NTI_GDF)
            h_curves.extend(h)
            v_curves.extend(v)
            health_stats.extend(health)
            vtx_dfs.extend(vtx)
    except Exception as e:
        logging.error(f"Worker failed on bbox {bbox}: {e}")

    return h_curves, v_curves, health_stats, vtx_dfs

# ---------------------------------------------------------------------------
# Core Execution Loop (Upgraded Grid Orchestrator)
# ---------------------------------------------------------------------------
def run_state_alignment(state_fips: str, out_dir: str, dem_dir: str, user_params: dict, local_df: pd.DataFrame = None):
    import shutil
    from shapely.wkt import loads
    from shapely.geometry import box
    from concurrent.futures import ProcessPoolExecutor, as_completed
    
    state_out_dir = os.path.join(out_dir, f"Output_State_{state_fips}")
    os.makedirs(state_out_dir, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d")

    version = 1
    while os.path.exists(os.path.join(state_out_dir, f"alignment_horizontal_{state_fips}_{stamp}_v{version}.csv")):
        version += 1

    h_csv     = os.path.join(state_out_dir, f"alignment_horizontal_{state_fips}_{stamp}_v{version}.csv")
    v_csv     = os.path.join(state_out_dir, f"alignment_vertical_{state_fips}_{stamp}_v{version}.csv")
    vtx_csv   = os.path.join(state_out_dir, f"alignment_vertices_{state_fips}_{stamp}_v{version}.csv")
    score_csv = os.path.join(state_out_dir, f"alignment_section_scores_{state_fips}_{stamp}_v{version}.csv")
    logging.info(f"Output version: v{version}")
        
    logging.info(f"\n{'='*60}\n=== V3.4 GRID ALIGNMENT PROCESSING: STATE {state_fips} ===\n{'='*60}")
    
    user_params_local = user_params.copy()
    user_params_local["STATE_FIPS"] = state_fips
    params = build_params(user_params_local)
    
    try:
        if local_df is not None:
            df = local_df.copy()
            # Shared helper in rat_core.py -- applies the same state filter
            # the live Socrata query would have applied server-side.
            df = filter_local_df_to_state(df, state_fips)
            df = apply_facility_fsystem_filters(
                df,
                facility_filter=user_params.get("FACILITY_TYPE_FILTER"),
                fsystem_filter=user_params.get("FSYSTEM_FILTER"),
            )
        else:
            df = fetch_socrata_state(
                state_fips,
                user_params.get("SOCRATA_TOKEN", ""),
                facility_type_filter=user_params.get("FACILITY_TYPE_FILTER"),
                fsystem_filter=user_params.get("FSYSTEM_FILTER"),
            )
    except Exception as e:
        logging.error(f"Failed to fetch dataset coordinates: {e}")
        return

    routes = df["RouteId"].dropna().unique().tolist()
    logging.info(f"Loaded {len(df):,} segments across {len(routes):,} routes.")
    
    nbi_url = user_params.get("NBI_URL", None)
    nti_url = user_params.get("NTI_URL", None)
    nbi_nti_gdf = fetch_nbi_nti_state(state_fips, user_params.get("SOCRATA_TOKEN", ""), nbi_url, nti_url)

    # ------------------------------------------------------------------
    # MAP FOOTPRINT TO GEOGRAPHIC EXTENT CELLS
    # ------------------------------------------------------------------
    logging.info("Calculating statewide boundary limits and slicing spatial cells...")
    df['geom_obj'] = df['WKT'].apply(loads)
    total_bounds = gpd.GeoSeries(df['geom_obj']).total_bounds
    minx, miny, maxx, maxy = total_bounds

    # Pre-fetch every DEM tile this run will ever need, in one serial call,
    # before any parallel worker starts. download_high_res_dem_tile() already
    # downloads every 1-degree tile intersecting whatever bbox it's given
    # (handling tiles that span a degree boundary internally), so passing
    # the full state-wide bbox here downloads the complete set this run
    # requires. With this done upfront, every worker's own later call to
    # download_high_res_dem_tile() for its own small cell bbox will simply
    # find the tile already cached -- there is no longer any contention to
    # resolve, rather than relying on lock-based coordination between many
    # workers racing for the same handful of tiles (which is what produced
    # the repeated stall on Delaware: a small state's whole footprint maps
    # to only 1-2 tiles, so nearly all ~1,360 grid tasks needed the same
    # tile).
    shared_cache_dir = os.path.join(dem_dir, "align_1m_cache")
    os.makedirs(shared_cache_dir, exist_ok=True)
    logging.info(
        f"Pre-fetching DEM tile(s) for the full state footprint "
        f"({minx:.2f},{miny:.2f}) to ({maxx:.2f},{maxy:.2f}) before starting parallel processing..."
    )
    from core.rat_core import download_high_res_dem_tile
    # download_high_res_dem_tile() only checks a given bbox's 4 corners to
    # determine which 1-degree tile(s) it needs -- correct for the single
    # small (0.02-degree) grid cell it was originally always called with,
    # but it would silently miss interior tiles if handed this function's
    # full state-wide bbox directly for any state wider than ~1-2 degrees.
    # Looping over every 1x1-degree cell within the footprint and calling
    # it once per cell keeps each individual call unambiguous regardless
    # of how large the overall footprint is.
    lat_start, lat_end = int(np.floor(miny)), int(np.ceil(maxy))
    lon_start, lon_end = int(np.floor(minx)), int(np.ceil(maxx))
    for lat_deg in range(lat_start, lat_end):
        for lon_deg in range(lon_start, lon_end):
            download_high_res_dem_tile((lon_deg, lat_deg, lon_deg + 1, lat_deg + 1), shared_cache_dir)
    logging.info("Pre-fetch complete -- no DEM downloads should be needed during parallel processing.")
    
    # Slice full network footprint into 0.02 degree grid cells (~1.3 mile squares)
    x_ticks = np.arange(minx, maxx + 0.02, 0.02)
    y_ticks = np.arange(miny, maxy + 0.02, 0.02)
    
    grid_cells = []
    for x in x_ticks:
        for y in y_ticks:
            grid_cells.append((x, y, x + 0.02, y + 0.02))

    # STRtree spatial index -- replaces O(n*m) row-by-row lambda with O(n log m)
    from shapely.strtree import STRtree
    logging.info(f"  Building spatial index over {len(df):,} segments...")
    geom_list = df['geom_obj'].tolist()
    tree = STRtree(geom_list)
    df_index = df.index.tolist()

    spatial_tasks = []
    for cell_bbox in grid_cells:
        cell_box = box(*cell_bbox)
        candidate_positions = tree.query(cell_box)
        if len(candidate_positions) == 0:
            continue
        matched_idx = [
            df_index[pos] for pos in candidate_positions
            if geom_list[pos].intersects(cell_box)
        ]
        if matched_idx:
            spatial_tasks.append((cell_bbox, df.loc[matched_idx].copy()))
            
    logging.info(f"Footprint successfully partitioned into {len(spatial_tasks):,} active intersecting grid tasks.")

    all_h, all_v, all_health, all_vtx = [], [], [], []
    max_workers = user_params.get("MAX_WORKERS", max(1, os.cpu_count() - 2))
    
    # Trigger the floating progress window
    from core.rat_core import GridProgressWindow
    progress_ui = GridProgressWindow(f"RAT v3.4 Engine: State {state_fips}", len(spatial_tasks))
    
    # Ingesting files via the multi-threaded grid engine workflow.
    # initializer=_init_worker_nbi loads the NBI/NTI structures GeoDataFrame
    # exactly once per worker process (max_workers times total) rather than
    # once per task submission -- critical at national scale with millions
    # of grid cells, where re-pickling 743,677 records per task would cause
    # severe memory pressure and IPC overhead.
    with ProcessPoolExecutor(
        max_workers=max_workers,
        initializer=_init_worker_nbi,
        initargs=(nbi_nti_gdf,)
    ) as executor:
        futures = {
            executor.submit(process_alignment_tile_worker, bbox, frame, dem_dir, params): bbox 
            for bbox, frame in spatial_tasks
        }
        completed = 0
        for fut in as_completed(futures):
            completed += 1
            progress_ui.update(completed)  # Update the visual bar
            
            if completed % 50 == 0: 
                logging.info(f"  ...Processed {completed}/{len(spatial_tasks)} geographic 1m grid tiles")
            try:
                h, v, health, vtx_dfs = fut.result()
                all_h.extend(h); all_v.extend(v); all_health.extend(health)
                all_vtx.extend(vtx_dfs)
            except Exception as e: 
                logging.error(f"Grid bounding box task {futures[fut]} failed: {e}")
                
    progress_ui.close()  # Destroy the pop-up when the state finishes

    df_h = pd.DataFrame(all_h)
    df_v = pd.DataFrame(all_v)
    df_health = pd.DataFrame(all_health)
    df_vtx = pd.concat(all_vtx, ignore_index=True) if all_vtx else pd.DataFrame()

    # Sort and deduplicate to fix random multiprocessing return order and
    # grid boundary overlaps (a route whose geometry spans more than one
    # spatial grid tile gets processed by each tile's worker independently;
    # the same physical point can land in more than one tile's subset, and
    # the order results come back in across workers is not deterministic).
    if not df_vtx.empty:
        df_vtx.drop_duplicates(subset=["RouteId", "Milepost"], keep="first", inplace=True)
        # Check if Part column exists; if not, create it with default value
        # (This handles cases where no curves were detected and Part was never set)
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

    # ==================================================================
    # INTERCHANGE TOPOLOGY MAPPER (Network-Level KDTree)
    # ==================================================================
    if not df_vtx.empty and "Lon" in df_vtx.columns:
        logging.info(f"[{state_fips}] Building KDTree to map interchange topology for {len(df_vtx):,} vertices...")
        from scipy.spatial import cKDTree
        
        # NOTE: previously initialized with the literal string "None"
        # rather than actual NaN. pd.notna("None") is True -- a string is
        # never null -- so every vertex that was never near a different
        # route's point (the vast majority, state-wide) kept that literal
        # "None" value and passed straight through the marker-building
        # filter below as if it were a real, unresolved interchange. This
        # only didn't show up before a CSV round-trip, since reading a
        # saved CSV back in auto-converts the text "None" to real NaN --
        # but the map is built in the same run, in memory, before that
        # ever happens.
        df_vtx["Topology_Node"] = np.nan
        df_vtx["Intersecting_Route"] = np.nan
        
        pts = df_vtx[["Lon", "Lat"]].to_numpy()
        tree = cKDTree(pts)

        routes = df_vtx["RouteId"].to_numpy()
        elevs = df_vtx["Elev_Ft"].to_numpy()

        topology = np.full(len(df_vtx), np.nan, dtype=object)
        intersecting = np.full(len(df_vtx), np.nan, dtype=object)

        interchange_count = 0
        at_grade_count = 0

        # Process in chunks rather than calling query_pairs() which
        # materialises every pair into one giant set simultaneously.
        # With 98.8M vertices that exhausts available RAM before any
        # route-filtering can happen -- query_ball_point() in small
        # chunks keeps peak memory bounded regardless of vertex count.
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
                    if dz > 10.0:
                        topo_type = "Grade-Separated"
                        interchange_count += 1
                    elif dz <= 5.0:
                        topo_type = "At-Grade"
                        at_grade_count += 1
                    else:
                        topo_type = "Unknown Clearance"
                        continue   # leave as NaN -- same as before

                    topology[i] = topo_type
                    topology[j] = topo_type
                    intersecting[i] = r2
                    intersecting[j] = r1
                
        df_vtx["Topology_Node"] = topology
        df_vtx["Intersecting_Route"] = intersecting
        logging.info(f"[{state_fips}] Topology mapped: {interchange_count} grade-separated nodes, {at_grade_count} at-grade nodes.")

    # Save calculated relational metrics back down to CSV tracking frames
    if params.get("OUT_CSV", True) and not df_vtx.empty:
        df_vtx.to_csv(vtx_csv, index=False)
        logging.info(f"Saved Vertices CSV to: {vtx_csv}")

    if params.get("OUT_CSV", True):
        score_cols = ["RouteId", "FSystem", "Start_MP", "End_MP"]
        available  = [c for c in score_cols if c in df.columns]
        df_scores  = assign_section_scores(df[available].copy(), df_h, df_v)
        df_scores.to_csv(score_csv, index=False)
        logging.info(f"Saved Section Scores CSV to: {score_csv}")

    if params.get("OUT_CSV", True):
        if not df_h.empty: 
            df_h.to_csv(h_csv, index=False)
            logging.info(f"Saved Horizontal CSV to: {h_csv}")
        if not df_v.empty: 
            df_v.to_csv(v_csv, index=False)
            logging.info(f"Saved Vertical CSV to: {v_csv}")
    
    export_geo(df_h, os.path.join(state_out_dir, f"alignment_horizontal_{state_fips}_{stamp}_v{version}"), params)
    export_geo(df_v, os.path.join(state_out_dir, f"alignment_vertical_{state_fips}_{stamp}_v{version}"), params)

    # Vertices spatial export (point geometry) -- opt-in via OUT_VTX_SPATIAL.
    # Uses a batched fiona writer rather than geopandas so that geometry is
    # created and written 50k rows at a time, keeping peak memory bounded
    # regardless of total vertex count (141M rows for Nebraska all-FS,
    # 94M for WA all-FS). The normal geopandas path crashes on these states
    # because it tries to consolidate all numeric columns into one contiguous
    # array before writing anything.
    if params.get("OUT_VTX_SPATIAL", False) and not df_vtx.empty \
            and "Lon" in df_vtx.columns and "Lat" in df_vtx.columns \
            and params.get("OUT_GPKG", True):
        vtx_gpkg_path = os.path.join(
            state_out_dir, f"alignment_vertices_{state_fips}_{stamp}_v{version}.gpkg"
        )
        export_vertices_gpkg_batched(df_vtx, vtx_gpkg_path)
    
    # Export dedicated Intersections GPKG if GPKG output is enabled
    if params.get("OUT_GPKG", True) and not df_vtx.empty and 'Topology_Node' in df_vtx.columns:
        intersections_gpkg_path = os.path.join(
            state_out_dir, f"alignment_intersections_{state_fips}_{stamp}_v{version}.gpkg"
        )
        export_intersections_gpkg(df_vtx, intersections_gpkg_path)
    
    if params.get("OUT_HTML_MAP", True) and not df_h.empty:
        generate_html_map(df_h, df_v, df_vtx, os.path.join(state_out_dir, f"alignment_map_{state_fips}_{stamp}_v{version}.html"), params)
    if params.get("OUT_DASHBOARD", True) and not df_h.empty:
        generate_dashboard(df_h, df_v, df_health, os.path.join(state_out_dir, f"alignment_dashboard_{state_fips}_{stamp}_v{version}.html"), state_out_dir, state_fips=state_fips, df_vtx=df_vtx)

    # Bridge structure parsing diagnostics
    if not df_vtx.empty and 'Structure_Tier' in df_vtx.columns:
        counts = df_vtx['Structure_Tier'].value_counts()
        t1 = counts.get("1_HPMS", 0)
        t2 = counts.get("2_NBI", 0)
        t3 = counts.get("3_DIP", 0)
        
        logging.info("========================================")
        logging.info("=== BRIDGE MATCH RATE & GAP REPORT ===")
        logging.info("========================================")
        logging.info(f"Vertices caught by Tier 1 (HPMS):    {t1:,}")
        logging.info(f"Vertices caught by Tier 2 (NBI):     {t2:,}")
        logging.info(f"Vertices caught by Tier 3 (Dip):     {t3:,}")
        
    if not df_v.empty and 'False_Valley_Warning' in df_v.columns:
        false_valleys = df_v['False_Valley_Warning'].sum()
        if false_valleys > 0:
            logging.warning(f"QA/QC FLAG: Detected {false_valleys} vertical SAG curves perfectly overlapping a bridge. Spline sag may be present.")
        else:
            logging.info("QA/QC: Zero false valleys detected at bridge crossings.")

    # ==========================================
    # EXECUTIVE TELEMETRY SUMMARY
    # ==========================================
    logging.info("========================================")
    logging.info("=== V3.4 EXECUTIVE SUMMARY ===")
    logging.info("========================================")
    
    if not df_vtx.empty and 'Topology_Node' in df_vtx.columns:
        topo_counts = df_vtx['Topology_Node'].value_counts()
        grade_sep = topo_counts.get('Grade-Separated', 0)
        at_grade = topo_counts.get('At-Grade', 0)
        logging.info(f"Network Topology: {grade_sep:,} Grade-Separated points, {at_grade:,} At-Grade points.")
    
    if not df_vtx.empty and 'Available_Sight_Dist_Ft' in df_vtx.columns:
        total_pts = len(df_vtx)
        blind_pts = (df_vtx['Available_Sight_Dist_Ft'] < 400).sum()
        pct_blind = (blind_pts / total_pts * 100) if total_pts > 0 else 0
        logging.info(f"Safety Profile:   {pct_blind:.1f}% of the network has an Available Sight Distance under 400 ft.")
        
    if not df_h.empty and 'Transition_Type' in df_h.columns:
        spirals = (df_h['Transition_Type'] == 'Spiral').sum()
        logging.info(f"Design Geometry:  {spirals:,} Engineered Spiral Transition curves detected.")
        
    logging.info("========================================")
    logging.info(f"Finished State {state_fips}!")

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
    
    # Set logging level based on --debug flag
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