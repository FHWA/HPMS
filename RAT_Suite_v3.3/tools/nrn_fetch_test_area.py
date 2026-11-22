# tools/nrn_fetch_test_area.py
#
# This software was developed by the Federal Highway Administration (FHWA),
# an agency of the U.S. Department of Transportation (U.S. DOT), and is
# dedicated to the public domain under the CC0 1.0 Universal Public Domain
# Dedication. See the LICENSE file in the RAT Suite folder.

"""
NATIONAL ROAD NETWORK: TEST-AREA DATA DOWNLOAD
--------------------------------------------------------------------------------
Downloads the source data for one National Road Network (NRN) test area:

  1. HPMS records for one route and reference point range (the ARNOLD line
     work), plus every other HPMS line nearby (the other direction of a
     divided highway, ramps, cross streets).
  2. A corridor around the route, CORRIDOR_HALF_WIDTH_FT to each side.
  3. NAIP aerial imagery for the corridor, from the Microsoft Planetary
     Computer, each image clipped to the corridor. By default the newest
     image for each part of the corridor.
  4. USGS 3DEP lidar points in the corridor. The projects are found through
     The National Map; by default the newest project for each part of the
     corridor is used. The points come from the copy USGS publishes in
     Amazon's public cloud, which can be read for just the corridor, with
     whole USGS tiles as the fallback. The Amazon copy is checked against
     one original tile, and gaps in coverage are filled from older projects.
  5. NBI bridge and NTI tunnel points in the area, from the RAT Suite's NBI
     folder.

The script reuses the RAT Suite's HPMS loader (core/rat_core.py) and lidar
functions (core/rat_lidar.py), so it lives in the RAT Suite's tools/ folder.

HOW TO RUN
  1. Edit the CONFIGURATION block below.
  2. Leave DRY_RUN = True for the first run. It gets the HPMS data, builds
     the corridor, and lists the lidar tiles and images it would download
     with their sizes, but downloads no lidar or imagery.
  3. Look at the listing (lidar_tiles_available.csv, naip_items_available.csv)
     and the total size in the log. Then set DRY_RUN = False and run again.
     Files already downloaded are skipped, so a stopped run can be restarted.

  From the RAT Suite folder:
      python tools/nrn_fetch_test_area.py

PACKAGES (beyond the RAT Suite's own)
      pip install "laspy[lazrs]" pystac-client planetary-computer

OUTPUT (in OUTPUT_DIR/AREA_NAME/)
  hpms/route_records.csv      the route's HPMS records in the range
  hpms/nearby_records.csv     every other HPMS record in the corridor's box
  hpms/structures.csv         NBI/NTI points in the corridor's box
  hpms/test_area.gpkg         layers: route, nearby, corridor (open in QGIS/ArcGIS)
  lidar/lidar_tiles_available.csv   every USGS tile found, its project, size, and whether chosen
  lidar/aws/<name>_corridor.laz     corridor points from the Amazon copy (Web Mercator, EPSG:3857)
  lidar/aws/<name>_gapfill.laz      points from an older project filling gaps
  lidar/aws/<name>_check.json       comparison of the Amazon copy with one original tile
  lidar/clipped/*.laz         whole USGS tiles clipped to the corridor (projects not in the Amazon copy)
  lidar/raw/*.laz             full tiles (deleted after clipping unless KEEP_RAW_LIDAR)
  lidar/lidar_project_results.csv   what was used from each project
  lidar/coverage_gaps.gpkg    parts of the corridor with no lidar points (only written
                              when there are gaps)
  naip/naip_items_available.csv     every image found, its year, and whether chosen
  naip/<year>/*.tif           chosen images clipped to the corridor
  manifest.json               settings, sources, counts, and times for this run
  fetch_log.txt               everything shown on screen
"""

# ===========================================================================
# CONFIGURATION -- edit these values, then run the script
# ===========================================================================

# A short name for the test area. Used for the output folder.
AREA_NAME = "RI_138_Newport"

# HPMS route: State FIPS code, Route ID, and reference point range.
STATE_FIPS = "44"
ROUTE_ID = "40112200B"
RP_START = 29.95
RP_END = 32.25

# Functional class(es) of the route, used only to narrow the statewide HPMS
# download (the route is then picked out by ROUTE_ID). None downloads every
# functional class in the State, which is slower.
HPMS_FSYSTEM_FILTER = []

# Records with no functional class, or no facility type, are left out of the
# download unless these are True. Some States leave both blank on local and
# Forest Service roads (the Idaho test route 18086AFD000, for example).
HPMS_INCLUDE_MISSING_FSYSTEM = True
HPMS_INCLUDE_MISSING_FACILITY = True

# Socrata app token. Leave blank to use the RAT_SOCRATA_TOKEN environment
# variable (or no token, which is slower and more often throttled).
SOCRATA_TOKEN = ""

# Corridor half-width: how far to each side of the HPMS line data is
# downloaded, in feet. Wide enough to take in both roadways of a divided
# highway and an old alignment beside the road.
CORRIDOR_HALF_WIDTH_FT = 300.0

# Where results go. The script makes OUTPUT_DIR/AREA_NAME/.
OUTPUT_DIR = r"output/nrn_test_areas"

# First run: True. Lists what would be downloaded, downloads no lidar or imagery.
# Set to False to download.
DRY_RUN = False

# --- Lidar --------------------------------------------------------------------
DOWNLOAD_LIDAR = True

# Where the lidar comes from:
#   "aws"  -- the copy of 3DEP that USGS publishes in Amazon's public cloud.
#             Only the points inside the corridor are downloaded, a small
#             share of the whole tiles. Projects not in that copy are
#             downloaded as whole USGS tiles instead.
#   "usgs" -- whole tiles from USGS (rockyweb), each clipped to the corridor.
LIDAR_SOURCE = "aws"

# Which lidar projects to use where more than one covers the corridor:
#   "newest" -- for each part of the corridor, the newest project covering it
#   "all"    -- every project (more data, and more downloading)
#   or text found in the project names, e.g. "IA_Western_3_2020" or a list
#   such as ["IA_Western_3_2020", "NE_PostSpringFlood"]
LIDAR_PROJECT_CHOICE = "newest"

# Compare the Amazon copy of each project with one original USGS tile, point
# by point, to confirm the copy's coordinates match the original data.
VERIFY_AWS_LIDAR = True

# Fill places in the corridor with no lidar points from older projects in
# the Amazon copy (LIDAR_SOURCE = "aws" only).
FILL_LIDAR_GAPS = True

# Keep the full downloaded USGS tiles after clipping them to the corridor.
# False saves disk space.
KEEP_RAW_LIDAR = False

# Stop (before downloading) if the whole USGS tiles to download add up to
# more than this many gigabytes.
MAX_LIDAR_DOWNLOAD_GB = 25.0

# USGS limits how fast files can be requested ("429 Too Many Requests").
# The script waits at least this many seconds between tile downloads, and
# waits longer (and slows down) whenever USGS asks it to.
DOWNLOAD_PAUSE_S = 20.0
# Tries per tile before it is set aside. Tiles set aside are tried once
# more at the end of the run, and again on the next run.
DOWNLOAD_ATTEMPTS = 8

# --- Imagery ------------------------------------------------------------------
DOWNLOAD_NAIP = True

# Which NAIP imagery to download:
#   "latest" -- for each part of the corridor, the newest image covering it
#   or the years wanted, e.g. [2022, 2023] (or "2022, 2023")
NAIP_YEARS = "latest"

# --- Structures ---------------------------------------------------------------
# Save the NBI/NTI points in the area (from the RAT Suite's NBI folder).
SAVE_STRUCTURES = True

# Path to the RAT Suite folder (the one holding core/). None = the folder
# above the one this script is in, which is right when the script is in tools/.
RAT_SUITE_DIR = None

# ===========================================================================
# END OF CONFIGURATION
# ===========================================================================

import os
import re
import sys
import json
import logging
from datetime import datetime

import numpy as np
import pandas as pd
import geopandas as gpd
import requests
from shapely.geometry import shape, mapping, box
import shapely
from shapely.ops import unary_union
from shapely.wkt import loads as wkt_loads
from pyproj import Transformer

FEET_PER_METER = 3.28084

# The lidar functions (finding projects, reading the Amazon copy and USGS
# tiles, the copy check, coverage gaps) are shared with the RAT Suite, in
# core/rat_lidar.py.
_SUITE_DIR = RAT_SUITE_DIR or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SUITE_DIR not in sys.path:
    sys.path.insert(0, _SUITE_DIR)
try:
    from core.rat_lidar import (  # noqa: E402
        MIN_NEW_COVERAGE, reproject, query_tnm_lidar, list_lidar_tiles, choose_newest_by_area,
        choose_lidar_projects as _choose_lidar_projects, DownloadPacer, read_tile_crs,
        find_ept, ept_crs, ept_nodes_in, fetch_ept_area, coverage_gaps, GAP_CELL_M, AWS_EPT_ROOT,
        fetch_tiles as _fetch_tiles, check_aws_copy as _check_aws_copy,
    )
except ImportError as e:
    raise SystemExit(f"Could not import core/rat_lidar.py from {_SUITE_DIR} ({e}). Put this script in "
                     f"the RAT Suite's tools/ folder, or set RAT_SUITE_DIR in the CONFIGURATION block.")


def choose_lidar_projects(tiles, choice, corridor_utm, to_utm):
    """core.rat_lidar.choose_lidar_projects, stopping the run when no project
    matches LIDAR_PROJECT_CHOICE."""
    try:
        return _choose_lidar_projects(tiles, choice, corridor_utm, to_utm)
    except ValueError as e:
        raise SystemExit(f"{e} (LIDAR_PROJECT_CHOICE = {choice!r})")


def fetch_tiles(rows: list, lidar_dir: str, corridor_wgs, pacer) -> dict:
    """Whole USGS tiles, clipped to the corridor (core.rat_lidar.fetch_tiles)."""
    return _fetch_tiles(rows, lidar_dir, corridor_wgs, pacer, attempts=DOWNLOAD_ATTEMPTS,
                        keep_raw=KEEP_RAW_LIDAR)


def check_aws_copy(name: str, project_tiles, ept_path: str, lidar_dir: str, corridor_wgs, pacer) -> dict:
    """Amazon copy against an original tile (core.rat_lidar.check_aws_copy)."""
    return _check_aws_copy(name, project_tiles, ept_path, lidar_dir, corridor_wgs, pacer,
                           attempts=DOWNLOAD_ATTEMPTS, keep_raw=KEEP_RAW_LIDAR)

TNM_PRODUCTS_URL = "https://tnmaccess.nationalmap.gov/api/v1/products"
TNM_LIDAR_DATASET = "Lidar Point Cloud (LPC)"
PLANETARY_COMPUTER_STAC = "https://planetarycomputer.microsoft.com/api/stac/v1"
# Tolerance (degrees, about 50 m) for the simplified corridor outline sent to the
# imagery search.
NAIP_SEARCH_SIMPLIFY_DEG = 0.0005


# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------

def parse_naip_years(value):
    """NAIP_YEARS as "latest" or a sorted list of int years. Accepts a list
    ([2022, 2023]), one year (2023), or text ("2022, 2023" or "2022 2023")."""
    if isinstance(value, str):
        text = value.strip()
        if text.lower() == "latest":
            return "latest"
        parts = text.replace(",", " ").split()
    elif isinstance(value, (int, np.integer)):
        parts = [value]
    else:
        parts = list(value)
    years = []
    for p in parts:
        try:
            y = int(str(p).strip())
        except ValueError:
            raise SystemExit(f'NAIP_YEARS = {value!r} is not understood. Use "latest" or years '
                             f'such as [2022, 2023].')
        if not 2000 <= y <= 2100:
            raise SystemExit(f"NAIP_YEARS has {y}, which is not a year.")
        years.append(y)
    if not years:
        raise SystemExit('NAIP_YEARS is empty. Use "latest" or years such as [2022, 2023].')
    return sorted(set(years))




def validate_config() -> None:
    """Checks the CONFIGURATION values before anything is downloaded."""
    global NAIP_YEARS
    if DOWNLOAD_NAIP:
        NAIP_YEARS = parse_naip_years(NAIP_YEARS)
    if LIDAR_SOURCE not in ("aws", "usgs"):
        raise SystemExit('LIDAR_SOURCE must be "aws" or "usgs".')
    choice = LIDAR_PROJECT_CHOICE
    if isinstance(choice, str):
        ok = bool(choice.strip())
    else:
        ok = isinstance(choice, (list, tuple)) and len(choice) > 0 and all(isinstance(c, str) and c.strip() for c in choice)
    if not ok:
        raise SystemExit('LIDAR_PROJECT_CHOICE must be "newest", "all", part of a project name, '
                         'or a list of such names.')
    if float(RP_END) <= float(RP_START):
        raise SystemExit(f"RP_END ({RP_END}) must be greater than RP_START ({RP_START}).")
    if float(CORRIDOR_HALF_WIDTH_FT) <= 0:
        raise SystemExit("CORRIDOR_HALF_WIDTH_FT must be greater than 0.")




def _area_dir() -> str:
    return os.path.join(OUTPUT_DIR, AREA_NAME)


def setup_logging(area_dir: str) -> None:
    os.makedirs(area_dir, exist_ok=True)
    log_path = os.path.join(area_dir, "fetch_log.txt")
    fmt = logging.Formatter("%(asctime)s - %(levelname)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    for h in list(root.handlers):
        root.removeHandler(h)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    fh = logging.FileHandler(log_path, mode="a", encoding="utf-8")
    fh.setFormatter(fmt)
    root.addHandler(sh)
    root.addHandler(fh)


def check_packages() -> None:
    """Stops with the pip command to run if a package the run needs is missing."""
    missing = []
    if DOWNLOAD_LIDAR:
        try:
            import laspy  # noqa: F401
            # LAZ needs a compression backend.
            if not laspy.LazBackend.detect_available():
                missing.append('"laspy[lazrs]"')
        except ImportError:
            missing.append('"laspy[lazrs]"')
    if DOWNLOAD_NAIP:
        for mod, pkg in (("pystac_client", "pystac-client"), ("planetary_computer", "planetary-computer")):
            try:
                __import__(mod)
            except ImportError:
                missing.append(pkg)
        try:
            import rasterio  # noqa: F401
        except ImportError:
            missing.append("rasterio")
    if missing:
        raise SystemExit("Missing packages. Install them with:\n    pip install " + " ".join(missing))


def import_rat_core():
    """The RAT Suite's core module, from RAT_SUITE_DIR."""
    suite_dir = RAT_SUITE_DIR or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if suite_dir not in sys.path:
        sys.path.insert(0, suite_dir)
    try:
        from core import rat_core
    except ImportError as e:
        raise SystemExit(
            f"Could not import core/rat_core.py from {suite_dir} ({e}). Put this script in the "
            f"RAT Suite's tools/ folder, or set RAT_SUITE_DIR in the CONFIGURATION block."
        )
    for name in ("fetch_socrata_state", "fetch_socrata_nearby", "fetch_nbi_nti_state",
                 "get_appropriate_utm_zone"):
        if not hasattr(rat_core, name):
            raise SystemExit(f"core/rat_core.py has no {name}(). This script needs the v3.3 rat_core.py.")
    return rat_core


# ---------------------------------------------------------------------------
# Step 1: HPMS records
# ---------------------------------------------------------------------------

def select_route_records(df: pd.DataFrame, route_id: str, rp_start: float, rp_end: float) -> pd.DataFrame:
    """The route's records whose reference point range overlaps rp_start..rp_end,
    in their submitted (ShapeId) order."""
    rid = df["RouteId"].astype(str).str.strip().str.upper()
    sub = df[rid == route_id.strip().upper()].copy()
    if sub.empty:
        return sub
    lo = np.minimum(sub["Start_MP"].astype(float), sub["End_MP"].astype(float))
    hi = np.maximum(sub["Start_MP"].astype(float), sub["End_MP"].astype(float))
    sub = sub[(hi > rp_start) & (lo < rp_end)]
    if "ShapeId" in sub.columns:
        sub = sub.sort_values("ShapeId", kind="mergesort")
    return sub


def to_gdf(df: pd.DataFrame) -> gpd.GeoDataFrame:
    geoms = df["WKT"].apply(lambda w: wkt_loads(w) if isinstance(w, str) and w else None)
    out = gpd.GeoDataFrame(df.drop(columns=["WKT"]), geometry=geoms, crs="EPSG:4326")
    return out[out.geometry.notna()]


def fetch_hpms(rat_core, hpms_dir: str):
    """Route records and nearby records as GeoDataFrames (EPSG:4326)."""
    token = SOCRATA_TOKEN or getattr(rat_core, "SOCRATA_TOKEN", "")
    logging.info(f"Downloading HPMS records for State {STATE_FIPS} "
                 f"(functional class filter {HPMS_FSYSTEM_FILTER}"
                 f"{', with blank functional class' if HPMS_INCLUDE_MISSING_FSYSTEM else ''}"
                 f"{', with blank facility type' if HPMS_INCLUDE_MISSING_FACILITY else ''})...")
    state_df = rat_core.fetch_socrata_state(
        STATE_FIPS, token=token, fsystem_filter=HPMS_FSYSTEM_FILTER,
        include_missing_fsystem=HPMS_INCLUDE_MISSING_FSYSTEM,
        include_missing_facility=HPMS_INCLUDE_MISSING_FACILITY)
    route_df = select_route_records(state_df, ROUTE_ID, RP_START, RP_END)
    if route_df.empty:
        ids = sorted(state_df["RouteId"].astype(str).unique())
        # Compare letters and digits only, so "070_A" finds "070A".
        def key(r):
            return re.sub(r"[^A-Z0-9]", "", str(r).upper())
        want = key(ROUTE_ID)
        same = [r for r in ids if key(r) == want]
        if str(ROUTE_ID) in ids or same:
            rid = str(ROUTE_ID) if str(ROUTE_ID) in ids else same[0]
            rec = state_df[state_df["RouteId"].astype(str) == rid]
            lo = min(rec["Start_MP"].min(), rec["End_MP"].min())
            hi = max(rec["Start_MP"].max(), rec["End_MP"].max())
            raise SystemExit(
                f"Route {ROUTE_ID} was not found, but route {rid} is in the HPMS data, from RP "
                f"{lo:.3f} to {hi:.3f}. Set ROUTE_ID = \"{rid}\" and an RP range inside that.")
        hint = [r for r in ids if want[-5:] and want[-5:] in key(r)][:20]
        raise SystemExit(
            f"No records for route {ROUTE_ID} between RP {RP_START} and {RP_END} in State {STATE_FIPS} "
            f"with functional class filter {HPMS_FSYSTEM_FILTER}. "
            + (f"Route IDs that look similar: {hint}" if hint else
               "Check ROUTE_ID, the RP range, HPMS_FSYSTEM_FILTER (try None), and "
               "HPMS_INCLUDE_MISSING_FSYSTEM / HPMS_INCLUDE_MISSING_FACILITY "
               "(try True if the route has no functional class or facility type).")
        )
    route_gdf = to_gdf(route_df)
    lo = np.minimum(route_df["Start_MP"], route_df["End_MP"]).min()
    hi = np.maximum(route_df["Start_MP"], route_df["End_MP"]).max()
    logging.info(f"Route {ROUTE_ID}: {len(route_gdf)} records, RP {lo:.3f} to {hi:.3f}. "
                 f"Whole records are kept, so the ends may reach a little past RP {RP_START} to {RP_END}.")
    route_df.to_csv(os.path.join(hpms_dir, "route_records.csv"), index=False)
    return route_gdf, token


def fetch_nearby(rat_core, token: str, corridor_wgs, route_gdf: gpd.GeoDataFrame, hpms_dir: str):
    """Every HPMS record in the corridor's bounding box, other than the route's own
    records, clipped to those that touch the corridor."""
    minx, miny, maxx, maxy = corridor_wgs.bounds
    logging.info("Downloading the other HPMS lines in the corridor (other direction, ramps, cross streets)...")
    try:
        near_df = rat_core.fetch_socrata_nearby(STATE_FIPS, (minx, miny, maxx, maxy), token=token)
    except Exception as e:
        logging.warning(f"Could not download nearby HPMS lines ({e}). Continuing without them.")
        return gpd.GeoDataFrame(geometry=[], crs="EPSG:4326")
    if near_df is None or near_df.empty:
        logging.info("No other HPMS lines found in the corridor.")
        return gpd.GeoDataFrame(geometry=[], crs="EPSG:4326")
    near_gdf = to_gdf(near_df)
    near_gdf = near_gdf[near_gdf.intersects(corridor_wgs)]
    # Drop the route's own records in the range (they are in route_records.csv).
    own = set(zip(route_gdf["RouteId"].astype(str), route_gdf["Start_MP"].round(6), route_gdf["End_MP"].round(6)))
    keys = list(zip(near_gdf["RouteId"].astype(str), near_gdf["Start_MP"].round(6), near_gdf["End_MP"].round(6)))
    near_gdf = near_gdf[[k not in own for k in keys]]
    pd.DataFrame(near_gdf.drop(columns="geometry").assign(WKT=near_gdf.geometry.to_wkt())).to_csv(
        os.path.join(hpms_dir, "nearby_records.csv"), index=False)
    n_routes = near_gdf["RouteId"].nunique() if len(near_gdf) else 0
    logging.info(f"Nearby HPMS lines: {len(near_gdf)} records on {n_routes} routes.")
    return near_gdf


# ---------------------------------------------------------------------------
# Step 2: corridor
# ---------------------------------------------------------------------------

def build_corridor(route_gdf: gpd.GeoDataFrame, half_width_ft: float, utm_epsg: int):
    """Corridor polygon around the route's lines: (polygon in EPSG:4326, polygon in UTM)."""
    to_utm = Transformer.from_crs("EPSG:4326", f"EPSG:{utm_epsg}", always_xy=True)
    to_wgs = Transformer.from_crs(f"EPSG:{utm_epsg}", "EPSG:4326", always_xy=True)
    lines_utm = [reproject(g, to_utm) for g in route_gdf.geometry]
    corridor_utm = unary_union(lines_utm).buffer(half_width_ft / FEET_PER_METER, cap_style=1)
    corridor_utm = corridor_utm.simplify(1.0)
    # Vertices every CORRIDOR_VERTEX_SPACING_M along the outline, so its
    # shape holds when converted to other coordinate systems: a straight edge
    # in one system is slightly curved in another, by up to decimeters over a
    # long edge.
    corridor_utm = shapely.segmentize(corridor_utm, CORRIDOR_VERTEX_SPACING_M)
    corridor_wgs = reproject(corridor_utm, to_wgs)
    return corridor_wgs, corridor_utm


CORRIDOR_VERTEX_SPACING_M = 5.0




def fetch_lidar(corridor_wgs, corridor_utm, utm_epsg: int, lidar_dir: str, manifest: dict) -> None:
    to_utm = Transformer.from_crs("EPSG:4326", f"EPSG:{utm_epsg}", always_xy=True)
    to_wgs = Transformer.from_crs(f"EPSG:{utm_epsg}", "EPSG:4326", always_xy=True)
    logging.info("Finding USGS 3DEP lidar that covers the corridor...")
    items = query_tnm_lidar(corridor_wgs.bounds)
    tiles = list_lidar_tiles(items, corridor_wgs)
    if tiles.empty:
        logging.warning("No lidar point cloud tiles found for the corridor.")
        manifest["lidar"] = {"tiles_found": 0}
        return
    if not tiles["bounds_checked"].all():
        logging.warning("Some tiles came back without a bounding box, so they could not be checked "
                        "against the corridor; they are listed and may lie outside it.")
    tiles = choose_lidar_projects(tiles, LIDAR_PROJECT_CHOICE, corridor_utm, to_utm)
    tiles = tiles.sort_values(["year", "publication_date", "tile"], ascending=[False, False, True])
    tiles.to_csv(os.path.join(lidar_dir, "lidar_tiles_available.csv"), index=False)

    summary = tiles.groupby("project").agg(year=("year", "max"), tiles=("tile", "count"),
                                           size_gb=("size_mb", lambda s: round(s.sum() / 1000, 2)),
                                           chosen=("chosen", "any")).sort_values("year", ascending=False)
    logging.info("Lidar projects covering the corridor:\n" + summary.to_string())
    chosen_projects = [p for p in summary.index if summary.loc[p, "chosen"]]
    others = [p for p in summary.index if not summary.loc[p, "chosen"]]
    manifest["lidar"] = {"source": LIDAR_SOURCE, "tiles_found": int(len(tiles)),
                         "projects": summary.reset_index().to_dict(orient="records"),
                         "projects_chosen": chosen_projects}

    # Where each chosen project comes from.
    ept = {}
    if LIDAR_SOURCE == "aws":
        with requests.Session() as s:
            for p in chosen_projects + (others if FILL_LIDAR_GAPS else []):
                name, info = find_ept(p, s)
                if name:
                    ept[p] = (name, info)
    tile_projects = [p for p in chosen_projects if p not in ept]
    for p in chosen_projects:
        if p in ept:
            logging.info(f"  {p}: from the Amazon copy ({ept[p][0]}), corridor points only.")
        else:
            why = "" if LIDAR_SOURCE == "usgs" else " (not in the Amazon copy)"
            logging.info(f"  {p}: whole USGS tiles{why}, {summary.loc[p, 'size_gb']:.2f} GB.")
    manifest["lidar"]["from_aws"] = {p: ept[p][0] for p in chosen_projects if p in ept}
    manifest["lidar"]["from_usgs_tiles"] = tile_projects

    tile_rows = [r for r in tiles[tiles["project"].isin(tile_projects)].itertuples(index=False)]
    tile_gb = sum(r.size_mb for r in tile_rows) / 1000

    if DRY_RUN:
        with requests.Session() as s:
            for p in chosen_projects:
                if p in ept:
                    name, info = ept[p]
                    area = reproject(corridor_wgs, Transformer.from_crs("EPSG:4326", ept_crs(info), always_xy=True))
                    nodes = ept_nodes_in(s, name, info, area)
                    pts = sum(nodes.values())
                    logging.info(f"  {name}: {len(nodes):,} index cells touch the corridor, {pts:,} points "
                                 f"before clipping (roughly {pts * 8 / 1e6:,.0f} MB to download).")
        if tile_rows:
            logging.info(f"  Whole USGS tiles: {len(tile_rows)} tiles, {tile_gb:.2f} GB.")
        logging.info("DRY_RUN: no lidar downloaded. See lidar/lidar_tiles_available.csv.")
        return
    if tile_gb > MAX_LIDAR_DOWNLOAD_GB:
        raise SystemExit(f"The USGS tiles to download total {tile_gb:.1f} GB, more than "
                         f"MAX_LIDAR_DOWNLOAD_GB ({MAX_LIDAR_DOWNLOAD_GB}). Raise it or choose a project.")

    pacer = DownloadPacer(DOWNLOAD_PAUSE_S)
    aws_dir = os.path.join(lidar_dir, "aws")
    os.makedirs(aws_dir, exist_ok=True)
    project_results = []
    files_used = []

    # Amazon copy: the corridor's points, then a check against one original tile.
    for p in chosen_projects:
        if p not in ept:
            continue
        name, info = ept[p]
        out_path = os.path.join(aws_dir, f"{name}_corridor.laz")
        rec = {"project": p, "source": "aws", "name": name, "file": os.path.relpath(out_path, lidar_dir)}
        try:
            if os.path.exists(out_path):
                logging.info(f"  {name}: already downloaded, skipped.")
            else:
                logging.info(f"  {name}: reading the corridor's points from the Amazon copy...")
                rec.update(fetch_ept_area(name, info, corridor_wgs, out_path))
                logging.info(f"    {rec['points_out']:,} points kept, {rec['mb_downloaded']:,.0f} MB downloaded.")
            if os.path.exists(out_path):
                files_used.append(out_path)
                check_path = os.path.join(aws_dir, f"{name}_check.json")
                # A saved check that paired no points (no usable tile, or none
                # with points in the corridor) is tried again on a rerun.
                redo = not os.path.exists(check_path)
                if not redo:
                    try:
                        with open(check_path, encoding="utf-8") as f:
                            redo = not json.load(f).get("pairs", 0)
                    except (OSError, ValueError):
                        redo = True
                if VERIFY_AWS_LIDAR and redo:
                    try:
                        chk = check_aws_copy(name, tiles[tiles["project"] == p], out_path, lidar_dir,
                                             corridor_wgs, pacer)
                        with open(check_path, "w", encoding="utf-8") as f:
                            json.dump(chk, f, indent=2, default=str)
                    except Exception as e:
                        # The points are still good; only the check is missing.
                        logging.warning(f"    The check of {name} against an original tile could not be "
                                        f"done ({type(e).__name__}: {e}). Its points were kept; run the "
                                        f"script again later to do the check.")
                        rec["check_error"] = str(e)
                if os.path.exists(check_path):
                    with open(check_path, encoding="utf-8") as f:
                        rec["check"] = json.load(f)
        except Exception as e:
            logging.error(f"  {name} failed: {type(e).__name__}: {e}")
            rec["error"] = str(e)
        project_results.append(rec)

    # USGS tiles for the rest, newest project first.
    if tile_rows:
        logging.info(f"Downloading {len(tile_rows)} whole USGS tile(s) for "
                     f"{', '.join(tile_projects)}...")
        res = fetch_tiles(tile_rows, lidar_dir, corridor_wgs, pacer)
        clip_dir = os.path.join(lidar_dir, "clipped")
        for r in tile_rows:
            f = os.path.join(clip_dir, r.tile.replace(".laz", "_corridor.laz"))
            if os.path.exists(f) and read_tile_crs(f) is not None:
                files_used.append(f)
        n_failed = sum(1 for r in tile_rows
                       if str(res.get(r.tile, {}).get("error") or "") not in ("", "nan"))
        for p in tile_projects:
            project_results.append({"project": p, "source": "usgs tiles",
                                    "tiles": int((tiles["project"] == p).sum())})
        manifest["lidar"]["tiles_failed"] = n_failed
        if n_failed:
            logging.warning(f"{n_failed} USGS tile(s) not done; see lidar/lidar_clip_results.csv. "
                            f"Run the script again to retry them (finished tiles are skipped).")

    # Gaps in coverage, filled from older projects in the Amazon copy.
    if files_used:
        gaps, gap_m2 = coverage_gaps(files_used, corridor_utm, utm_epsg)
        share = gap_m2 / corridor_utm.area
        logging.info(f"Lidar coverage: {share:.1%} of the corridor has no points "
                     f"(cells of {GAP_CELL_M:.0f} m; water usually shows as a gap).")
        if gaps is not None and FILL_LIDAR_GAPS and LIDAR_SOURCE == "aws":
            for p in [o for o in others if o in ept]:
                name, info = ept[p]
                out_path = os.path.join(aws_dir, f"{name}_gapfill.laz")
                if not os.path.exists(out_path):
                    gaps_wgs = reproject(gaps.buffer(GAP_CELL_M), to_wgs)
                    logging.info(f"  Filling gaps from the older project {name}...")
                    try:
                        r = fetch_ept_area(name, info, gaps_wgs, out_path, label="gap fill: ")
                        project_results.append({"project": p, "source": "aws gap fill", "name": name, **r})
                    except Exception as e:
                        logging.error(f"  Gap fill from {name} failed: {type(e).__name__}: {e}")
                        continue
                if os.path.exists(out_path):
                    files_used.append(out_path)
                    gaps, gap_m2 = coverage_gaps(files_used, corridor_utm, utm_epsg)
                    logging.info(f"    After filling: {gap_m2 / corridor_utm.area:.1%} of the corridor has no points.")
                if gaps is None:
                    break
        manifest["lidar"]["gap_share"] = round(gap_m2 / corridor_utm.area, 4)
        gap_gpkg = os.path.join(lidar_dir, "coverage_gaps.gpkg")
        if os.path.exists(gap_gpkg):
            os.remove(gap_gpkg)
        if gaps is not None:
            gpd.GeoDataFrame({"area_m2": [gap_m2]}, geometry=[reproject(gaps, to_wgs)],
                             crs="EPSG:4326").to_file(gap_gpkg, layer="gaps", driver="GPKG")

    manifest["lidar"]["files"] = [os.path.relpath(f, lidar_dir) for f in files_used]
    manifest["lidar"]["project_results"] = project_results
    pd.DataFrame([{k: v for k, v in r.items() if k != "check"} for r in project_results]).to_csv(
        os.path.join(lidar_dir, "lidar_project_results.csv"), index=False)


# ---------------------------------------------------------------------------
# Step 4: NAIP imagery
# ---------------------------------------------------------------------------

def search_naip(corridor_wgs) -> list:
    import pystac_client
    import planetary_computer
    catalog = pystac_client.Client.open(PLANETARY_COMPUTER_STAC, modifier=planetary_computer.sign_inplace)
    # The corridor has a vertex every 5 m, too many for the search request on
    # a long route ("Request body is too large"). A simplified outline,
    # widened by the simplifying tolerance so that it still holds the whole
    # corridor, finds the same images.
    tol = NAIP_SEARCH_SIMPLIFY_DEG
    search_area = corridor_wgs.simplify(tol, preserve_topology=True).buffer(tol)
    search = catalog.search(collections=["naip"], intersects=mapping(search_area))
    return list(search.items())


def _naip_year(item) -> int:
    y = item.properties.get("naip:year")
    if y:
        return int(y)
    return int(str(item.properties.get("datetime", "0000"))[:4])


def _naip_gsd(item) -> float:
    """Pixel size of an image in meters (99 if not given)."""
    try:
        return float(item.properties.get("gsd"))
    except (TypeError, ValueError):
        return 99.0


def list_naip_items(items: list, years, corridor_utm, to_utm: Transformer) -> pd.DataFrame:
    """One row per image, with 'chosen' set from NAIP_YEARS: "latest" picks, for
    each part of the corridor, the newest image covering it (the finer one
    where two images of the same year overlap); a list of years picks every
    image from those years."""
    rows = [{"id": it.id, "year": _naip_year(it), "state": it.properties.get("naip:state", ""),
             "gsd_m": it.properties.get("gsd", ""), "date": str(it.properties.get("datetime", ""))[:10]}
            for it in items]
    df = pd.DataFrame(rows)
    if df.empty:
        df["chosen"] = []
        return df
    years = parse_naip_years(years)
    if years == "latest":
        # Newest year first; within a year the finer image first (where two
        # States' images cover the same ground, as in the District of
        # Columbia), then the later date.
        entries = [(it.id, (_naip_year(it), -_naip_gsd(it), str(it.properties.get("datetime", ""))),
                    shape(it.geometry) if getattr(it, "geometry", None) else None) for it in items]
        keep, left = choose_newest_by_area(entries, corridor_utm, to_utm)
        df["chosen"] = df["id"].isin(keep)
        if left > MIN_NEW_COVERAGE:
            logging.warning(f"The NAIP images found leave {left:.0%} of the corridor without imagery.")
    else:
        df["chosen"] = df["year"].isin(years)
        missing = sorted(set(years) - set(df["year"]))
        if missing:
            logging.warning(f"No NAIP imagery for {missing} covers the corridor. "
                            f"Years available: {sorted(df['year'].unique())}.")
    return df.sort_values(["year", "date"], ascending=False).reset_index(drop=True)


def clip_raster_to_corridor(src_path: str, dst_path: str, corridor_wgs) -> dict:
    """Writes the part of an image inside the corridor's bounding box to dst_path,
    with pixels outside the corridor set to 0 (nodata), compressed."""
    import rasterio
    from rasterio.windows import from_bounds, Window
    from rasterio.features import geometry_mask

    with rasterio.open(src_path) as src:
        to_img = Transformer.from_crs("EPSG:4326", src.crs, always_xy=True)
        poly = reproject(corridor_wgs, to_img)
        inter = poly.intersection(box(*src.bounds))
        if inter.is_empty:
            return {"written": False, "reason": "image does not touch the corridor"}
        # Whole pixels covering the corridor's part of the image (computed
        # directly; rasterio's window rounding methods differ between versions).
        w = from_bounds(*inter.bounds, transform=src.transform)
        c0 = max(0, int(np.floor(w.col_off)))
        r0 = max(0, int(np.floor(w.row_off)))
        c1 = min(src.width, int(np.ceil(w.col_off + w.width)))
        r1 = min(src.height, int(np.ceil(w.row_off + w.height)))
        if c1 <= c0 or r1 <= r0:
            return {"written": False, "reason": "image does not touch the corridor"}
        win = Window(c0, r0, c1 - c0, r1 - r0)
        data = src.read(window=win)
        w_transform = src.window_transform(win)
        outside = geometry_mask([mapping(inter)], out_shape=(int(win.height), int(win.width)),
                                transform=w_transform, invert=False)
        data[:, outside] = 0
        profile = src.profile.copy()
        profile.update(driver="GTiff", height=int(win.height), width=int(win.width),
                       transform=w_transform, nodata=0, compress="deflate", tiled=True,
                       blockxsize=512, blockysize=512, BIGTIFF="IF_SAFER")
        tmp = dst_path + ".part"
        with rasterio.open(tmp, "w", **profile) as dst:
            dst.write(data)
        os.replace(tmp, dst_path)
        return {"written": True, "width": int(win.width), "height": int(win.height),
                "bands": int(data.shape[0]), "crs": src.crs.to_string(),
                "pixel_m": round(abs(src.transform.a), 3)}


def fetch_naip(corridor_wgs, corridor_utm, utm_epsg: int, naip_dir: str, manifest: dict) -> None:
    logging.info("Finding NAIP imagery that touches the corridor (Microsoft Planetary Computer)...")
    items = search_naip(corridor_wgs)
    to_utm = Transformer.from_crs("EPSG:4326", f"EPSG:{utm_epsg}", always_xy=True)
    listing = list_naip_items(items, NAIP_YEARS, corridor_utm, to_utm)
    if listing.empty:
        if str(STATE_FIPS).zfill(2) in ("02", "15"):
            logging.warning("No NAIP imagery found for the corridor. NAIP covers the lower 48 "
                            "States only, not Alaska or Hawaii.")
        else:
            logging.warning("No NAIP imagery found for the corridor.")
        manifest["naip"] = {"items_found": 0}
        return
    listing.to_csv(os.path.join(naip_dir, "naip_items_available.csv"), index=False)
    by_year = listing.groupby(["year", "state"]).agg(images=("id", "count"), chosen=("chosen", "sum"))
    logging.info("NAIP imagery covering the corridor (chosen = images used):\n"
                 + by_year.sort_index(ascending=False).to_string())
    chosen_ids = set(listing.loc[listing["chosen"], "id"])
    manifest["naip"] = {"items_found": int(len(listing)),
                        "years": sorted(int(y) for y in listing["year"].unique()),
                        "items_chosen": len(chosen_ids)}
    if DRY_RUN:
        logging.info("DRY_RUN: no imagery downloaded. See naip/naip_items_available.csv.")
        return

    results = []
    chosen = [it for it in items if it.id in chosen_ids]
    for k, it in enumerate(chosen, start=1):
        year_dir = os.path.join(naip_dir, str(_naip_year(it)))
        os.makedirs(year_dir, exist_ok=True)
        dst = os.path.join(year_dir, f"{it.id}_corridor.tif")
        if os.path.exists(dst):
            logging.info(f"[{k}/{len(chosen)}] {it.id}: already clipped, skipped.")
            continue
        logging.info(f"[{k}/{len(chosen)}] Clipping {it.id}...")
        try:
            # The image is read over the network; only the corridor's part is fetched.
            res = clip_raster_to_corridor(it.assets["image"].href, dst, corridor_wgs)
            results.append({"id": it.id, **res})
        except Exception as e:
            logging.error(f"    {it.id} failed: {type(e).__name__}: {e}")
            results.append({"id": it.id, "error": str(e)})
    if results:
        pd.DataFrame(results).to_csv(os.path.join(naip_dir, "naip_clip_results.csv"), index=False)
    manifest["naip"]["items_processed"] = len(results)
    manifest["naip"]["items_failed"] = sum(1 for r in results if "error" in r)


# ---------------------------------------------------------------------------
# Step 5: structures
# ---------------------------------------------------------------------------

def save_structures(rat_core, corridor_wgs, hpms_dir: str, manifest: dict) -> None:
    gdf = rat_core.fetch_nbi_nti_state(STATE_FIPS, bbox=corridor_wgs.bounds)
    if gdf is None or gdf.empty:
        manifest["structures"] = 0
        return
    gdf = gdf[gdf.intersects(corridor_wgs)]
    out = pd.DataFrame({"kind": gdf["kind"].map({1: "Bridge", 2: "Tunnel"}),
                        "length_m": gdf["length_m"], "lon": gdf.geometry.x, "lat": gdf.geometry.y})
    out.to_csv(os.path.join(hpms_dir, "structures.csv"), index=False)
    logging.info(f"NBI/NTI structures in the corridor: {len(out)}.")
    manifest["structures"] = int(len(out))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    area_dir = _area_dir()
    setup_logging(area_dir)
    validate_config()
    check_packages()
    started = datetime.now()
    logging.info(f"NRN test-area download: {AREA_NAME} (route {ROUTE_ID}, RP {RP_START} to {RP_END}, "
                 f"State {STATE_FIPS}). DRY_RUN = {DRY_RUN}.")
    rat_core = import_rat_core()

    hpms_dir = os.path.join(area_dir, "hpms")
    lidar_dir = os.path.join(area_dir, "lidar")
    naip_dir = os.path.join(area_dir, "naip")
    for d in (hpms_dir, lidar_dir, naip_dir):
        os.makedirs(d, exist_ok=True)

    manifest = {
        "area_name": AREA_NAME, "state_fips": STATE_FIPS, "route_id": ROUTE_ID,
        "rp_start": RP_START, "rp_end": RP_END, "corridor_half_width_ft": CORRIDOR_HALF_WIDTH_FT,
        "hpms_fsystem_filter": HPMS_FSYSTEM_FILTER,
        "hpms_include_missing_fsystem": HPMS_INCLUDE_MISSING_FSYSTEM,
        "hpms_include_missing_facility": HPMS_INCLUDE_MISSING_FACILITY,
        "dry_run": DRY_RUN, "lidar_source": LIDAR_SOURCE, "lidar_project_choice": LIDAR_PROJECT_CHOICE,
        "naip_years": NAIP_YEARS,
        "started": started.isoformat(timespec="seconds"),
        "sources": {"hpms": getattr(rat_core, "SOCRATA_DEFAULT", ""), "lidar": TNM_PRODUCTS_URL,
                    "lidar_aws": AWS_EPT_ROOT,
                    "naip": PLANETARY_COMPUTER_STAC},
    }

    # 1. HPMS route, 2. corridor, then the other HPMS lines in it.
    route_gdf, token = fetch_hpms(rat_core, hpms_dir)
    geoms = route_gdf.geometry
    c = (geoms.union_all() if hasattr(geoms, "union_all") else geoms.unary_union).centroid
    utm_epsg = rat_core.get_appropriate_utm_zone(c.x, c.y)
    corridor_wgs, corridor_utm = build_corridor(route_gdf, CORRIDOR_HALF_WIDTH_FT, utm_epsg)
    area_mi2 = corridor_utm.area / (1609.344 ** 2)
    logging.info(f"Corridor: {CORRIDOR_HALF_WIDTH_FT:.0f} ft each side, {area_mi2:.2f} square miles "
                 f"(UTM EPSG:{utm_epsg}).")
    manifest["corridor_area_sq_mi"] = round(area_mi2, 3)
    manifest["utm_epsg"] = utm_epsg
    manifest["route_records"] = int(len(route_gdf))

    near_gdf = fetch_nearby(rat_core, token, corridor_wgs, route_gdf, hpms_dir)
    manifest["nearby_records"] = int(len(near_gdf))

    gpkg = os.path.join(hpms_dir, "test_area.gpkg")
    if os.path.exists(gpkg):
        os.remove(gpkg)
    route_gdf.to_file(gpkg, layer="route", driver="GPKG")
    if len(near_gdf):
        near_gdf.to_file(gpkg, layer="nearby", driver="GPKG")
    gpd.GeoDataFrame({"area": [AREA_NAME], "half_width_ft": [CORRIDOR_HALF_WIDTH_FT]},
                     geometry=[corridor_wgs], crs="EPSG:4326").to_file(gpkg, layer="corridor", driver="GPKG")

    if SAVE_STRUCTURES:
        try:
            save_structures(rat_core, corridor_wgs, hpms_dir, manifest)
        except Exception as e:
            logging.warning(f"Could not read NBI/NTI structures ({e}).")

    # 3. Imagery, 4. lidar. A failure in one does not stop the other.
    if DOWNLOAD_NAIP:
        try:
            fetch_naip(corridor_wgs, corridor_utm, utm_epsg, naip_dir, manifest)
        except Exception as e:
            logging.error(f"Imagery step failed: {type(e).__name__}: {e}")
            manifest.setdefault("naip", {})["error"] = str(e)
    if DOWNLOAD_LIDAR:
        try:
            fetch_lidar(corridor_wgs, corridor_utm, utm_epsg, lidar_dir, manifest)
        except SystemExit:
            raise
        except Exception as e:
            logging.error(f"Lidar step failed: {type(e).__name__}: {e}")
            manifest.setdefault("lidar", {})["error"] = str(e)

    manifest["finished"] = datetime.now().isoformat(timespec="seconds")
    with open(os.path.join(area_dir, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2, default=str)
    logging.info(f"Done in {(datetime.now() - started).total_seconds() / 60:.1f} minutes. "
                 f"Results in {os.path.abspath(area_dir)}")


if __name__ == "__main__":
    main()