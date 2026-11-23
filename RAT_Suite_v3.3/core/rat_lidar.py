# core/rat_lidar.py
#
# This software was developed by the Federal Highway Administration (FHWA),
# an agency of the U.S. Department of Transportation (U.S. DOT), and is
# dedicated to the public domain under the CC0 1.0 Universal Public Domain
# Dedication. See the LICENSE file in the RAT Suite folder.

"""
USGS 3DEP LIDAR: DOWNLOAD AND BRIDGE DECK ELEVATIONS
--------------------------------------------------------------------------------
Reading 3DEP lidar point clouds, shared by the RAT Suite tools and the
National Road Network (NRN) scripts:

  - finding the lidar projects that cover an area (The National Map),
    choosing the newest for each part of it;
  - reading the points inside an area from the copy of 3DEP that USGS
    publishes in Amazon's public cloud (Entwine Point Tiles), or from whole
    USGS tiles clipped to the area, working out a tile's coordinate system
    when the file does not state it;
  - checking the Amazon copy against an original tile, and finding parts of
    an area with no points;
  - bridge deck elevations for the RAT Suite's profiles
    (lidar_bridge_decks): the measured deck in place of the straight line
    between abutments that the bare-earth DEM leaves.

Needs laspy with a LAZ backend: pip install "laspy[lazrs]". Without it the
RAT Suite keeps the straight lines and says so in the Run Log.

CREATED BY: FHWA OHPI.
VERSION: 3.3, November 23, 2026
"""

import os
import re
import json
import time
import logging
from datetime import datetime
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
import requests
import shapely
from shapely.geometry import shape, mapping, box, LineString
from pyproj import CRS, Transformer
from scipy.spatial import cKDTree

TNM_PRODUCTS_URL = "https://tnmaccess.nationalmap.gov/api/v1/products"
TNM_LIDAR_DATASET = "Lidar Point Cloud (LPC)"


# ===========================================================================
# Finding and reading 3DEP lidar
# ===========================================================================

# A source is used where it adds more than this share of the corridor that
# newer sources do not cover (see choose_newest_by_area).
MIN_NEW_COVERAGE = 0.01


def reproject(geom, transformer: Transformer):
    """geom with its coordinates converted by a pyproj Transformer (shapely 2)."""
    return shapely.transform(
        geom, lambda xy: np.column_stack(transformer.transform(xy[:, 0], xy[:, 1])))



# An empty lidar listing from The National Map is asked again this many times.
TNM_EMPTY_RETRIES = 3
TNM_EMPTY_WAIT_S = 30.0


def _tnm_get(params: dict, session: requests.Session, attempts: int = 5) -> dict:
    for attempt in range(1, attempts + 1):
        try:
            r = session.get(TNM_PRODUCTS_URL, params=params, timeout=120)
            if r.status_code in (429, 500, 502, 503, 504) and attempt < attempts:
                wait = min(60, 5 * 2 ** (attempt - 1))
                logging.warning(f"The National Map returned {r.status_code}; retrying in {wait}s...")
                time.sleep(wait)
                continue
            r.raise_for_status()
            return r.json()
        except requests.RequestException as e:
            if attempt == attempts:
                raise
            wait = min(60, 5 * 2 ** (attempt - 1))
            logging.warning(f"The National Map request failed ({e}); retrying in {wait}s...")
            time.sleep(wait)
    return {}


def query_tnm_lidar(bounds_wgs: tuple, empty_retries: int = TNM_EMPTY_RETRIES,
                    empty_wait_s: float = TNM_EMPTY_WAIT_S, attempts: int = 5) -> list:
    """Every lidar point cloud product in the box, from The National Map.
    The National Map sometimes answers with no products for an area it
    lists at other times, so an empty answer is asked again empty_retries
    times, empty_wait_s apart, before it is taken as final. A request that
    fails is tried up to `attempts` times."""
    minx, miny, maxx, maxy = bounds_wgs
    for attempt in range(1, empty_retries + 2):
        items, offset, page = [], 0, 500
        with requests.Session() as session:
            while True:
                params = {"datasets": TNM_LIDAR_DATASET, "bbox": f"{minx},{miny},{maxx},{maxy}",
                          "prodFormats": "LAZ", "max": page, "offset": offset, "outputFormat": "JSON"}
                data = _tnm_get(params, session, attempts=attempts)
                batch = data.get("items", []) or []
                items.extend(batch)
                total = int(data.get("total", len(items)) or 0)
                offset += len(batch)
                if not batch or offset >= total:
                    break
        if items or attempt > empty_retries:
            return items
        logging.info(f"The National Map listed no lidar for the area; asking again in "
                     f"{empty_wait_s:.0f}s (try {attempt + 1} of {empty_retries + 1})...")
        time.sleep(empty_wait_s)
    return []


def _tile_url(item: dict) -> str:
    return item.get("downloadLazURL") or item.get("downloadURL") or ""


def _tile_bounds(item: dict):
    bb = item.get("boundingBox") or {}
    try:
        return (float(bb["minX"]), float(bb["minY"]), float(bb["maxX"]), float(bb["maxY"]))
    except (KeyError, TypeError, ValueError):
        return None


def _project_name(item: dict) -> str:
    """The lidar project a tile belongs to, from its download address
    (.../Projects/<project>/<work unit>/LAZ/<tile>.laz), else from its title."""
    url = _tile_url(item)
    parts = url.split("/")
    if "Projects" in parts:
        i = parts.index("Projects")
        # Up to two folder levels after Projects/ (e.g. legacy/IA_STATEWIDE_2008,
        # or IA_FullState_2019/IA_FullState_B1_2019), stopping at the LAZ folder.
        segs = [p for p in parts[i + 1:i + 3] if p and p.upper() != "LAZ" and not p.lower().endswith(".laz")]
        if segs:
            return "/".join(segs)
    title = item.get("title", "") or ""
    return title.rsplit(" ", 1)[0] if " " in title else (title or "unknown")


def _project_year(name: str, pub_date: str) -> int:
    """Collection year from a project name such as IA_FullState_2019, else the
    publication year."""
    import re
    years = [int(y) for y in re.findall(r"(?<!\d)(19[89]\d|20[0-4]\d)(?!\d)", name)]
    if years:
        return max(years)
    try:
        return int(str(pub_date)[:4])
    except (TypeError, ValueError):
        return 0


def list_lidar_tiles(items: list, corridor_wgs) -> pd.DataFrame:
    """One row per tile that touches the corridor, with its project and size."""
    rows = []
    for it in items:
        url = _tile_url(it)
        if not url.lower().endswith(".laz"):
            continue
        b = _tile_bounds(it)
        if b is not None and not box(*b).intersects(corridor_wgs):
            continue
        project = _project_name(it)
        rows.append({
            "project": project,
            "year": _project_year(project, it.get("publicationDate", "")),
            "tile": os.path.basename(url.split("?")[0]),
            "size_mb": round(float(it.get("sizeInBytes") or 0) / 1e6, 1),
            "publication_date": it.get("publicationDate", ""),
            "metadata_url": it.get("metaUrl", ""),
            "url": url,
            "bounds_checked": b is not None,
            # The tile's extent in longitude/latitude as USGS lists it. Used to
            # work out the coordinate system of a tile that does not state one.
            "min_lon": b[0] if b else np.nan, "min_lat": b[1] if b else np.nan,
            "max_lon": b[2] if b else np.nan, "max_lat": b[3] if b else np.nan,
        })
    df = pd.DataFrame(rows)
    if not df.empty:
        df = df.drop_duplicates(subset="url").reset_index(drop=True)
    return df


def choose_newest_by_area(entries: list, corridor_utm, to_utm: Transformer) -> tuple:
    """
    Picks, for each part of the corridor, the newest source that covers it.
    entries: (key, sort_key, footprint in EPSG:4326), where a larger sort_key
    is newer. Going from newest to oldest, a source is kept if it covers more
    than MIN_NEW_COVERAGE of the corridor that the newer sources kept so far
    do not. Returns (keys kept, newest first; share of the corridor left
    uncovered).
    """
    total = corridor_utm.area
    remaining = corridor_utm
    chosen = []
    for key, _, footprint in sorted(entries, key=lambda e: e[1], reverse=True):
        if footprint is None or footprint.is_empty:
            continue
        fp_utm = reproject(footprint, to_utm).buffer(0)
        if remaining.intersection(fp_utm).area > MIN_NEW_COVERAGE * total:
            chosen.append(key)
            remaining = remaining.difference(fp_utm)
    return chosen, (remaining.area / total if total else 0.0)


def _choice_list(choice) -> list:
    """A project choice as a list of texts to look for in project names."""
    if isinstance(choice, str):
        return [choice]
    return [str(c) for c in choice]


def choose_lidar_projects(tiles: pd.DataFrame, choice, corridor_utm, to_utm: Transformer) -> pd.DataFrame:
    """Marks the tiles to download in a 'chosen' column. choice: "newest" (for
    each part of the area, the newest project covering it), "all", or text
    found in project names (one text or a list). ValueError if no project
    matches the text."""
    tiles = tiles.copy()
    if tiles.empty:
        tiles["chosen"] = []
        return tiles
    if isinstance(choice, str) and choice == "all":
        tiles["chosen"] = True
    elif isinstance(choice, str) and choice == "newest":
        entries = []
        for project, g in tiles.groupby("project"):
            boxes = [box(r.min_lon, r.min_lat, r.max_lon, r.max_lat) for r in g.itertuples()
                     if not pd.isna(r.min_lon)]
            fp = shapely.union_all(boxes) if boxes else None
            entries.append((project, (int(g["year"].max()), str(g["publication_date"].max())), fp))
        keep, left = choose_newest_by_area(entries, corridor_utm, to_utm)
        tiles["chosen"] = tiles["project"].isin(keep)
        if left > MIN_NEW_COVERAGE:
            logging.warning(f"The lidar projects found leave {left:.0%} of the corridor without coverage "
                            f"(by their tile outlines).")
    else:
        wanted = _choice_list(choice)
        tiles["chosen"] = tiles["project"].apply(lambda p: any(w.lower() in p.lower() for w in wanted))
        if not tiles["chosen"].any():
            raise ValueError(f"No lidar project matches {choice!r}. "
                             f"Projects found: {sorted(tiles['project'].unique())}")
    return tiles


class DownloadPacer:
    """Spaces out requests to one server. Each request starts at least `pause`
    seconds after the previous one. When the server says it is getting too many
    requests (HTTP 429 or 503), the pause doubles (up to max_pause); after
    several successes in a row it eases back toward the configured pause."""

    def __init__(self, pause_s: float, max_pause_s: float = 180.0):
        self.base = max(0.0, float(pause_s))
        self.pause = self.base
        self.max_pause = max(max_pause_s, self.base)
        self.last_start = 0.0
        self.ok_in_a_row = 0

    def wait_turn(self) -> None:
        wait = self.last_start + self.pause - time.time()
        if wait > 0:
            time.sleep(wait)
        self.last_start = time.time()

    def throttled(self) -> None:
        self.ok_in_a_row = 0
        self.pause = min(self.max_pause, max(self.pause * 2, self.base, 10.0))

    def succeeded(self) -> None:
        self.ok_in_a_row += 1
        if self.ok_in_a_row >= 3 and self.pause > self.base:
            self.pause = max(self.base, self.pause / 2)
            self.ok_in_a_row = 0


class ThrottledError(RuntimeError):
    """The server kept refusing with 'too many requests' for every attempt."""


def _retry_after_s(response) -> Optional[float]:
    """Seconds to wait from a Retry-After header (a number of seconds or a date)."""
    value = response.headers.get("Retry-After") if response is not None else None
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        try:
            from email.utils import parsedate_to_datetime
            return max(0.0, (parsedate_to_datetime(value) - datetime.now(tz=parsedate_to_datetime(value).tzinfo)).total_seconds())
        except Exception:
            return None


def download_file(url: str, dest: str, session: requests.Session, pacer: DownloadPacer,
                  attempts: int = 8) -> str:
    """Downloads url to dest (via a .part file). Requests are spaced by pacer.
    On 'too many requests' (429/503) it waits as the server asks (Retry-After),
    else 30 s, 60 s, 120 s ... up to 10 minutes; on other errors 10 s, 20 s, ...
    Raises ThrottledError if every attempt was refused for too many requests."""
    tmp = dest + ".part"
    throttled_every_time = True
    for attempt in range(1, attempts + 1):
        pacer.wait_turn()
        response = None
        try:
            with session.get(url, stream=True, timeout=300) as response:
                if response.status_code in (429, 503):
                    raise requests.HTTPError(f"{response.status_code} {response.reason}", response=response)
                response.raise_for_status()
                with open(tmp, "wb") as f:
                    for block in response.iter_content(chunk_size=4 * 1024 * 1024):
                        f.write(block)
            os.replace(tmp, dest)
            pacer.succeeded()
            return dest
        except (requests.RequestException, OSError) as e:
            status = getattr(getattr(e, "response", None), "status_code", None)
            busy = status in (429, 503)
            if busy:
                pacer.throttled()
                wait = _retry_after_s(e.response)
                if wait is None:
                    wait = min(600.0, 30.0 * 2 ** (attempt - 1))
                reason = "USGS is limiting requests (too many requests)"
            else:
                throttled_every_time = False
                if status is not None and 400 <= status < 500:
                    raise          # not found, forbidden: retrying will not help
                wait = 10.0 * attempt
                reason = f"{type(e).__name__}: {e}"
            if attempt == attempts:
                if busy and throttled_every_time:
                    raise ThrottledError(f"still refused after {attempts} tries ({reason})")
                raise
            logging.warning(f"    {os.path.basename(dest)}: {reason}. Try {attempt + 1} of {attempts} "
                            f"in {wait:.0f}s (requests now spaced {pacer.pause:.0f}s apart).")
            time.sleep(wait)
    return dest


def _horizontal_crs(crs: CRS) -> CRS:
    """The horizontal part of a compound (horizontal + vertical) CRS."""
    if crs is not None and crs.is_compound:
        for sub in crs.sub_crs_list:
            if sub.is_projected or sub.is_geographic:
                return sub
    return crs


def read_tile_crs(path: str):
    """The horizontal coordinate system stated in a LAS/LAZ file, or None."""
    import laspy
    try:
        with laspy.open(path) as reader:
            return _horizontal_crs(reader.header.parse_crs())
    except Exception:
        return None


# A coordinate system worked out from a tile's extent must place the tile
# within this share of its own width of where USGS lists it.
CRS_MATCH_TOLERANCE = 0.05


def _crs_candidates(lon: float, lat: float, known: list) -> list:
    """Coordinate systems to try for a tile at lon/lat that states none: those
    already read from other tiles of the same project first, then the NAD83
    UTM zone (meters and US survey feet), then the NAD83 State Plane zones
    that cover the point (meters and feet). Only NAD83 systems are tried, since
    an older datum (NAD27) would place the tile only tens of meters off and
    could pass the extent test by mistake."""
    from pyproj.database import query_crs_info
    from pyproj.aoi import AreaOfInterest
    out, seen = [], set()

    def add(crs):
        if crs is None:
            return
        key = crs.to_wkt()
        if key not in seen:
            seen.add(key)
            out.append(crs)

    for crs in known:
        add(crs)
    zone = int((lon + 180) // 6) + 1
    utm_m = CRS.from_epsg(26900 + zone) if 1 <= zone <= 23 else None
    add(utm_m)
    if utm_m is not None:
        add(CRS.from_proj4(f"+proj=utm +zone={zone} +datum=NAD83 +units=us-ft +no_defs"))
    try:
        infos = query_crs_info(auth_name="EPSG", pj_types="PROJECTED_CRS",
                               area_of_interest=AreaOfInterest(lon, lat, lon, lat), contains=True)
    except Exception:
        infos = []
    for info in infos:
        name = info.name
        if not name.startswith("NAD83 /") or "UTM" in name:
            continue                      # NAD83 only; UTM handled above
        if any(s in name for s in ("(HARN)", "(CSRS)", "(NSRS2007)", "(2011)", "(CORS96)", "(PA11)", "(MA11)")):
            continue                      # same positions within about a meter as plain NAD83
        try:
            add(CRS.from_epsg(int(info.code)))
        except Exception:
            pass
    return out


def infer_tile_crs(path: str, lonlat_bounds: tuple, known: list):
    """
    Works out the coordinate system of a tile that does not state one, by
    finding the candidate (see _crs_candidates) that places the tile's own
    extent (from its header) where USGS lists the tile in longitude/latitude.
    Returns (crs, mismatch as a share of the tile's width) or (None, best
    mismatch) when no candidate places it within CRS_MATCH_TOLERANCE.
    """
    import laspy
    if lonlat_bounds is None or any(pd.isna(v) for v in lonlat_bounds):
        return None, np.nan
    with laspy.open(path) as reader:
        h = reader.header
        x0, y0, x1, y1 = float(h.mins[0]), float(h.mins[1]), float(h.maxs[0]), float(h.maxs[1])
    lo0, la0, lo1, la1 = lonlat_bounds
    lon_c, lat_c = (lo0 + lo1) / 2, (la0 + la1) / 2
    m_per_deg_lat = 111_320.0
    m_per_deg_lon = 111_320.0 * np.cos(np.radians(lat_c))
    width_m = max((lo1 - lo0) * m_per_deg_lon, (la1 - la0) * m_per_deg_lat, 1.0)
    best = (None, np.inf)
    for crs in _crs_candidates(lon_c, lat_c, known):
        try:
            to_ll = Transformer.from_crs(crs, "EPSG:4326", always_xy=True)
            xs, ys = to_ll.transform([x0, x1, x0, x1], [y0, y0, y1, y1])
        except Exception:
            continue
        if not np.all(np.isfinite(xs)) or not np.all(np.isfinite(ys)):
            continue
        err = max(abs(min(xs) - lo0) * m_per_deg_lon, abs(max(xs) - lo1) * m_per_deg_lon,
                  abs(min(ys) - la0) * m_per_deg_lat, abs(max(ys) - la1) * m_per_deg_lat) / width_m
        if err < best[1]:
            best = (crs, err)
    if best[0] is not None and best[1] <= CRS_MATCH_TOLERANCE:
        return best
    return None, best[1]


def clip_laz_to_corridor(src_path: str, dst_path: str, corridor_wgs, crs=None,
                         crs_source: str = "file", chunk_points: int = 2_000_000) -> dict:
    """Writes the points of src_path that fall inside the corridor to dst_path.
    Reads in chunks, so a large tile does not need to fit in memory. crs is the
    tile's coordinate system (read from the file if None). When it was worked
    out rather than read (crs_source "inferred"), it is written into the
    clipped file's header so later steps can read it."""
    import copy
    import laspy

    if crs is None:
        crs = read_tile_crs(src_path)
    if crs is None:
        raise ValueError("no coordinate system")
    with laspy.open(src_path) as reader:
        header = reader.header
        out_header = copy.deepcopy(header)
        if crs_source != "file":
            out_header.add_crs(crs)
        to_tile = Transformer.from_crs("EPSG:4326", crs, always_xy=True)
        poly = reproject(corridor_wgs, to_tile)
        shapely.prepare(poly)
        minx, miny, maxx, maxy = poly.bounds
        n_out = 0
        tmp = dst_path + ".part"
        with laspy.open(tmp, mode="w", header=out_header) as writer:
            for pts in reader.chunk_iterator(chunk_points):
                x = np.asarray(pts.x)
                y = np.asarray(pts.y)
                keep = (x >= minx) & (x <= maxx) & (y >= miny) & (y <= maxy)
                if keep.any():
                    idx = np.flatnonzero(keep)
                    inside = shapely.contains_xy(poly, x[idx], y[idx])
                    keep[:] = False
                    keep[idx[inside]] = True
                if keep.any():
                    writer.write_points(pts[keep])
                    n_out += int(keep.sum())
        os.replace(tmp, dst_path)
        return {"points_in": int(header.point_count), "points_out": n_out,
                "crs": crs.to_string(), "crs_source": crs_source, "clipped": True}


# ---------------------------------------------------------------------------
# Lidar from the Amazon copy of 3DEP (Entwine Point Tiles)
# ---------------------------------------------------------------------------
# USGS publishes most 3DEP lidar projects in Amazon's public cloud
# (s3://usgs-lidar-public) as Entwine Point Tiles (EPT): each project is an
# octree of small LAZ files, so the points inside the corridor can be read
# without downloading whole tiles. A project's description is in
# <root>/<name>/ept.json; its octree index in ept-hierarchy/<key>.json, where
# each entry "D-X-Y-Z" gives the point count of that node, or -1 if the
# entries below it are in their own file ept-hierarchy/D-X-Y-Z.json; and its
# points in ept-data/<key>.laz. Node D-X-Y-Z covers 1/2^D of the project's
# cube along each axis, at index X, Y, Z. The points are stored in Web
# Mercator (EPSG:3857), converted by USGS from each project's own system.

AWS_EPT_ROOT = "https://s3-us-west-2.amazonaws.com/usgs-lidar-public"
AWS_THREADS = 8


def _http_get(session: requests.Session, url: str, as_json: bool = False, attempts: int = 6):
    """GET with retries on temporary errors. Returns the JSON (as_json) or the
    bytes, or None if the file does not exist (403/404 from the bucket)."""
    for attempt in range(1, attempts + 1):
        try:
            r = session.get(url, timeout=120)
            if r.status_code in (403, 404):
                return None
            if r.status_code in (429, 500, 502, 503, 504):
                raise requests.HTTPError(f"{r.status_code}", response=r)
            r.raise_for_status()
            return r.json() if as_json else r.content
        except (requests.RequestException, ValueError):
            if attempt == attempts:
                raise
            time.sleep(min(60.0, 2.0 ** attempt))
    return None


def _ept_name_candidates(project: str) -> list:
    """Names the Amazon copy may use for a USGS project path such as
    'IA_WesternIA_2020_D21/IA_Western_3_2020' (usually the work unit, the last
    folder), most likely first."""
    parts = [p for p in project.split("/") if p and p.lower() != "legacy"]
    return list(dict.fromkeys(reversed(parts)))


def find_ept(project: str, session: requests.Session):
    """(name, ept.json contents) of a project's Amazon copy, or (None, None)."""
    for name in _ept_name_candidates(project):
        try:
            info = _http_get(session, f"{AWS_EPT_ROOT}/{name}/ept.json", as_json=True)
        except Exception as e:
            logging.warning(f"Could not read the Amazon copy's description for {name} ({e}).")
            info = None
        if isinstance(info, dict) and "bounds" in info:
            return name, info
    return None, None


def ept_crs(info: dict) -> CRS:
    srs = info.get("srs") or {}
    if srs.get("wkt"):
        return _horizontal_crs(CRS.from_wkt(srs["wkt"]))
    return CRS.from_authority(srs.get("authority", "EPSG"), str(srs.get("horizontal", "3857")))


def _ept_node_box(key: str, cube: list):
    d, x, y, _ = (int(v) for v in key.split("-"))
    n = 2 ** d
    w = (cube[3] - cube[0]) / n
    h = (cube[4] - cube[1]) / n
    return box(cube[0] + x * w, cube[1] + y * h, cube[0] + (x + 1) * w, cube[1] + (y + 1) * h)


def ept_depth_for_resolution(info: dict, resolution_m: Optional[float]) -> Optional[int]:
    """Deepest octree level needed for points about resolution_m apart, or
    None for every level. A node at depth D holds about span x span points
    over 1/2^D of the project's width (span from ept.json), so its points are
    about width / 2^D / span apart; deeper levels add finer points."""
    if not resolution_m or resolution_m <= 0:
        return None
    try:
        cube = info["bounds"]
        width = max(cube[3] - cube[0], cube[4] - cube[1])
        span = float(info.get("span", 128)) or 128.0
    except (KeyError, TypeError, IndexError, ValueError):
        return None
    d = 0
    while width / (2 ** d) / span > resolution_m and d < 30:
        d += 1
    return d


def ept_nodes_in(session: requests.Session, name: str, info: dict, area_ept,
                 max_depth: Optional[int] = None) -> dict:
    """{node key: point count} for every node whose cell touches area_ept (a
    polygon in the EPT's coordinates), down to max_depth (all levels if
    None). Sub-index files are read only for cells that touch the area,
    since a node's children lie inside it."""
    shapely.prepare(area_ept)
    cube = info["bounds"]
    todo, seen, nodes = ["0-0-0-0"], set(), {}
    while todo:
        key = todo.pop()
        if key in seen:
            continue
        seen.add(key)
        h = _http_get(session, f"{AWS_EPT_ROOT}/{name}/ept-hierarchy/{key}.json", as_json=True)
        if h is None:
            raise RuntimeError(f"index file {key}.json is missing from the Amazon copy of {name}")
        for k, v in h.items():
            if max_depth is not None and int(k.split("-")[0]) > max_depth:
                continue
            if not shapely.intersects(area_ept, _ept_node_box(k, cube)):
                continue
            if v == -1:
                todo.append(k)
            elif v > 0:
                nodes[k] = int(v)
    return nodes


def _points_in(las, area_ept):
    """Mask of the points of a LasData inside area_ept (prepared)."""
    x = np.asarray(las.x)
    y = np.asarray(las.y)
    minx, miny, maxx, maxy = area_ept.bounds
    keep = (x >= minx) & (x <= maxx) & (y >= miny) & (y <= maxy)
    if keep.any():
        idx = np.flatnonzero(keep)
        inside = shapely.contains_xy(area_ept, x[idx], y[idx])
        keep[:] = False
        keep[idx[inside]] = True
    return keep


def fetch_ept_area(name: str, info: dict, area_wgs, out_path: str, label: str = "",
                   max_depth: Optional[int] = None, quiet: bool = False) -> dict:
    """Writes the points of an EPT project inside area_wgs (EPSG:4326) to
    out_path (LAZ, in the EPT's coordinates, with its coordinate system in the
    header). Nodes are downloaded in parallel, down to max_depth (every level
    if None; see ept_depth_for_resolution). quiet logs no progress lines.
    Nothing is written when no point falls inside the area."""
    import copy
    import io
    import threading
    import laspy
    from concurrent.futures import ThreadPoolExecutor, as_completed

    crs = ept_crs(info)
    area_ept = reproject(area_wgs, Transformer.from_crs("EPSG:4326", crs, always_xy=True))
    with requests.Session() as s:
        nodes = ept_nodes_in(s, name, info, area_ept, max_depth=max_depth)
    if not nodes:
        return {"nodes": 0, "points_out": 0, "mb_downloaded": 0.0, "crs": crs.to_string()}
    log = logging.debug if quiet else logging.info
    log(f"    {label}{name}: {len(nodes):,} index cells touch the area "
                 f"({sum(nodes.values()):,} points before clipping). Downloading...")

    local = threading.local()

    def worker(key):
        if not hasattr(local, "session"):
            local.session = requests.Session()
        data = _http_get(local.session, f"{AWS_EPT_ROOT}/{name}/ept-data/{key}.laz")
        if data is None:
            raise RuntimeError(f"data file {key}.laz is missing from the Amazon copy of {name}")
        las = laspy.read(io.BytesIO(data))
        keep = _points_in(las, area_ept)
        return key, len(data), las.header, (las.points[keep] if keep.any() else None)

    tmp = out_path + ".part"
    writer, out_header = None, None
    n_out, n_bytes, done = 0, 0, 0
    step = max(1, len(nodes) // 10)
    try:
        with ThreadPoolExecutor(max_workers=AWS_THREADS) as pool:
            futures = [pool.submit(worker, k) for k in nodes]
            for fut in as_completed(futures):
                key, size, header, pts = fut.result()
                n_bytes += size
                done += 1
                if pts is not None and len(pts):
                    if writer is None:
                        out_header = copy.deepcopy(header)
                        try:
                            has_crs = out_header.parse_crs() is not None
                        except Exception:
                            has_crs = False
                        if not has_crs:
                            out_header.add_crs(crs)
                        writer = laspy.open(tmp, mode="w", header=out_header)
                    if (np.allclose(header.scales, out_header.scales)
                            and np.allclose(header.offsets, out_header.offsets)
                            and header.point_format.id == out_header.point_format.id):
                        writer.write_points(pts)
                    else:
                        rec = laspy.ScaleAwarePointRecord.zeros(len(pts), header=out_header)
                        for dim in rec.point_format.dimension_names:
                            if dim not in ("X", "Y", "Z") and dim in pts.point_format.dimension_names:
                                rec[dim] = pts[dim]
                        rec.x, rec.y, rec.z = pts.x, pts.y, pts.z
                        writer.write_points(rec)
                    n_out += len(pts)
                if done % step == 0 or done == len(nodes):
                    log(f"      {done:,} of {len(nodes):,} cells, {n_bytes / 1e6:,.0f} MB, "
                                 f"{n_out:,} points kept")
    finally:
        if writer is not None:
            writer.close()
    if writer is not None:
        os.replace(tmp, out_path)
    return {"nodes": len(nodes), "points_out": n_out, "mb_downloaded": round(n_bytes / 1e6, 1),
            "crs": crs.to_string()}


def _tile_units(crs) -> tuple:
    """(meters per horizontal unit, meters per vertical unit, where the
    vertical unit came from) for a tile's coordinate system."""
    def axis_factor(c):
        try:
            return float(c.axis_info[0].unit_conversion_factor)
        except (AttributeError, IndexError, TypeError, ValueError):
            return 1.0
    horiz = crs
    vert = None
    if getattr(crs, "is_compound", False):
        subs = crs.sub_crs_list
        horiz = subs[0]
        vert = subs[1] if len(subs) > 1 else None
    if getattr(horiz, "is_bound", False) and getattr(horiz, "source_crs", None) is not None:
        horiz = horiz.source_crs
    h = axis_factor(horiz) if not getattr(horiz, "is_geographic", False) else 1.0
    if vert is not None:
        return h, axis_factor(vert), "vertical coordinate system"
    return h, h, "same as horizontal (no vertical coordinate system stated)"


def compare_with_original_tile(ept_path: str, tile_path: str, corridor_wgs, project_crs=None,
                               max_pairs: int = 500_000) -> dict:
    """
    Checks that the Amazon copy puts points where the original USGS tile
    does. Points are paired by GPS time and return number (and scan channel
    where both have it), which identify each laser return exactly, and the
    Amazon copy's points are converted into the tile's own coordinate system.
    Returns the number of pairs and the median, 95th-percentile and largest
    differences in x, y and z (meters).
    """
    import laspy
    tile_crs = project_crs or read_tile_crs(tile_path)
    if tile_crs is None:
        return {"pairs": 0, "note": "the original tile states no coordinate system"}

    def keys_of(pts, fields):
        return pd.DataFrame({f: np.asarray(pts[f]) for f in fields})

    with laspy.open(tile_path) as r:
        dims_t = set(r.header.point_format.dimension_names)
        poly_t = reproject(corridor_wgs, Transformer.from_crs("EPSG:4326", tile_crs, always_xy=True))
        shapely.prepare(poly_t)
        parts = []
        for pts in r.chunk_iterator(2_000_000):
            keep = shapely.contains_xy(poly_t, np.asarray(pts.x), np.asarray(pts.y))
            if keep.any():
                p = pts[keep]
                fields = ["gps_time", "return_number"] + (["scanner_channel"] if "scanner_channel" in dims_t else [])
                df = keys_of(p, fields)
                df["xt"], df["yt"], df["zt"] = np.asarray(p.x), np.asarray(p.y), np.asarray(p.z)
                parts.append(df)
    if not parts:
        return {"pairs": 0, "note": "the original tile has no points in the corridor"}
    tile_df = pd.concat(parts, ignore_index=True)
    with laspy.open(ept_path) as r:
        dims_e = set(r.header.point_format.dimension_names)
        ept_crs_ = _horizontal_crs(r.header.parse_crs())
        keys = ["gps_time", "return_number"] + (
            ["scanner_channel"] if "scanner_channel" in dims_t and "scanner_channel" in dims_e else [])
        tile_df = tile_df.drop_duplicates(subset=keys, keep=False)
        to_t = Transformer.from_crs(ept_crs_, tile_crs, always_xy=True)
        pairs = []
        n = 0
        for pts in r.chunk_iterator(2_000_000):
            df = keys_of(pts, keys)
            df["xe"], df["ye"], df["ze"] = np.asarray(pts.x), np.asarray(pts.y), np.asarray(pts.z)
            m = tile_df.merge(df, on=keys, how="inner")
            if len(m):
                m["xe"], m["ye"] = to_t.transform(m["xe"].to_numpy(), m["ye"].to_numpy())
                pairs.append(m)
                n += len(m)
                if n >= max_pairs:
                    break
    if not pairs:
        return {"pairs": 0, "note": "no points could be paired by GPS time"}
    m = pd.concat(pairs, ignore_index=True)
    # A key found twice in the Amazon copy (two returns with the same GPS
    # time, return number and channel) pairs a tile point with the wrong
    # return; such pairs are dropped.
    n_all = len(m)
    m = m.drop_duplicates(subset=keys, keep=False)
    out = {"pairs": int(len(m)), "pairs_dropped_ambiguous": int(n_all - len(m)),
           "tile_crs": tile_crs.to_string()}
    if m.empty:
        out["note"] = "no points could be paired unambiguously"
        return out
    # The differences are taken in meters. The tile's x and y are in its
    # coordinate system's unit (US survey feet in many State Plane tiles), and
    # its z in the vertical unit it states, else, by USGS practice, the same
    # unit as x and y. The Amazon copy's z is in meters.
    h_to_m, v_to_m, v_note = _tile_units(tile_crs)
    out["tile_xy_unit_m"] = h_to_m
    out["tile_z_unit_m"] = v_to_m
    out["tile_z_unit_source"] = v_note
    m["xe"], m["xt"] = m["xe"] * h_to_m, m["xt"] * h_to_m
    m["ye"], m["yt"] = m["ye"] * h_to_m, m["yt"] * h_to_m
    m["zt"] = m["zt"] * v_to_m
    for a, (c1, c2) in {"x": ("xe", "xt"), "y": ("ye", "yt"), "z": ("ze", "zt")}.items():
        d = (m[c1] - m[c2]).to_numpy()
        out[f"d{a}_median_m"] = round(float(np.median(d)), 4)
        out[f"d{a}_p95_abs_m"] = round(float(np.percentile(np.abs(d), 95)), 4)
        out[f"d{a}_max_abs_m"] = round(float(np.max(np.abs(d))), 4)
    # Share of pairs more than 5 cm apart in any direction.
    far = ((m["xe"] - m["xt"]).abs() > 0.05) | ((m["ye"] - m["yt"]).abs() > 0.05) | ((m["ze"] - m["zt"]).abs() > 0.05)
    out["share_over_5cm"] = round(float(far.mean()), 6)
    return out


# A point-free cell this size (meters) inside the corridor counts as a gap
# in the lidar coverage; gaps smaller than GAP_MIN_AREA_M2 are ignored.
GAP_CELL_M = 10.0
GAP_MIN_AREA_M2 = 2000.0


def coverage_gaps(laz_paths: list, corridor_utm, utm_epsg: int):
    """Parts of the corridor (UTM polygon) with no lidar points in any of the
    files: GAP_CELL_M cells with no point, joined, keeping gaps of at least
    GAP_MIN_AREA_M2. Water usually shows as a gap (it returns few pulses)."""
    import laspy
    from rasterio.features import geometry_mask, shapes
    from rasterio.transform import from_origin
    minx, miny, maxx, maxy = corridor_utm.bounds
    ncol = int(np.ceil((maxx - minx) / GAP_CELL_M))
    nrow = int(np.ceil((maxy - miny) / GAP_CELL_M))
    tr = from_origin(minx, maxy, GAP_CELL_M, GAP_CELL_M)
    hit = np.zeros((nrow, ncol), dtype=bool)
    for path in laz_paths:
        with laspy.open(path) as r:
            crs = _horizontal_crs(r.header.parse_crs())
            to_utm = Transformer.from_crs(crs, f"EPSG:{utm_epsg}", always_xy=True)
            for pts in r.chunk_iterator(2_000_000):
                x, y = to_utm.transform(np.asarray(pts.x), np.asarray(pts.y))
                c = ((x - minx) // GAP_CELL_M).astype(np.int64)
                rr = ((maxy - y) // GAP_CELL_M).astype(np.int64)
                ok = (c >= 0) & (c < ncol) & (rr >= 0) & (rr < nrow)
                hit[rr[ok], c[ok]] = True
    inside = geometry_mask([mapping(corridor_utm)], out_shape=(nrow, ncol), transform=tr, invert=True)
    empty = inside & ~hit
    polys = [shape(g) for g, v in shapes(empty.astype(np.uint8), mask=empty, transform=tr) if v == 1]
    polys = [p for p in polys if p.area >= GAP_MIN_AREA_M2]
    if not polys:
        return None, 0.0
    gaps = shapely.union_all(polys).intersection(corridor_utm)
    return gaps, float(gaps.area)


def fetch_tiles(rows: list, lidar_dir: str, corridor_wgs, pacer: DownloadPacer,
                attempts: int = 8, keep_raw: bool = False) -> dict:
    """
    Downloads whole USGS tiles (rows of the tile listing) one at a time and
    clips each to the corridor, into lidar/clipped/. Used for projects not in
    the Amazon copy, or for all projects when whole tiles are wanted. Results
    are kept in lidar/lidar_clip_results.csv across runs. Returns
    {tile: result}.
    """
    raw_dir = os.path.join(lidar_dir, "raw")
    clip_dir = os.path.join(lidar_dir, "clipped")
    unclipped_dir = os.path.join(lidar_dir, "unclipped")
    for d in (raw_dir, clip_dir):
        os.makedirs(d, exist_ok=True)

    results_path = os.path.join(lidar_dir, "lidar_clip_results.csv")
    results = {}
    if os.path.exists(results_path):
        try:
            for r in pd.read_csv(results_path).to_dict(orient="records"):
                results[r["tile"]] = r
        except Exception:
            results = {}

    # Coordinate systems read from each project's tiles, used first when
    # working out the system of a tile in the same project that states none.
    project_crs = {}

    def clip_one(row, src_path, clip_path):
        crs, source, mismatch = read_tile_crs(src_path), "file", 0.0
        if crs is not None:
            project_crs.setdefault(row.project, [])
            if all(crs != c for c in project_crs[row.project]):
                project_crs[row.project].append(crs)
        else:
            bounds = (row.min_lon, row.min_lat, row.max_lon, row.max_lat)
            crs, mismatch = infer_tile_crs(src_path, bounds, project_crs.get(row.project, []))
            source = "inferred"
            if crs is None:
                os.makedirs(unclipped_dir, exist_ok=True)
                os.replace(src_path, os.path.join(unclipped_dir, row.tile))
                raise ValueError(f"states no coordinate system, and none of the NAD83 systems tried "
                                 f"places it where USGS lists it (closest was off by "
                                 f"{mismatch:.0%} of its width). Kept whole in lidar/unclipped/.")
            logging.info(f"    No coordinate system stated in the file; {crs.name} places the tile "
                         f"where USGS lists it (within {mismatch:.1%} of its width). Using it.")
        res = clip_laz_to_corridor(src_path, clip_path, corridor_wgs, crs=crs, crs_source=source)
        res["crs_match"] = round(float(mismatch), 4)
        return res

    def process(k, n, row, session):
        clip_path = os.path.join(clip_dir, row.tile.replace(".laz", "_corridor.laz"))
        raw_path = os.path.join(raw_dir, row.tile)
        if os.path.exists(os.path.join(unclipped_dir, row.tile)):
            logging.info(f"[{k}/{n}] {row.tile}: in lidar/unclipped/ from an earlier run (its coordinate "
                         f"system could not be worked out); skipped. Delete it there to try again.")
            return
        if os.path.exists(clip_path):
            if read_tile_crs(clip_path) is not None:
                logging.info(f"[{k}/{n}] {row.tile}: already clipped, skipped.")
                return
            # An earlier version kept tiles with no stated coordinate system
            # whole, under the clipped name. Clip them now.
            logging.info(f"[{k}/{n}] {row.tile}: kept whole by an earlier run; clipping it now.")
            os.replace(clip_path, raw_path)
        elif os.path.exists(raw_path) and not os.path.exists(raw_path + ".part"):
            logging.info(f"[{k}/{n}] {row.tile}: downloaded earlier; clipping.")
        else:
            logging.info(f"[{k}/{n}] Downloading {row.tile} ({row.size_mb:.0f} MB)...")
            download_file(row.url, raw_path, session, pacer, attempts=attempts)
        res = clip_one(row, raw_path, clip_path)
        share = res["points_out"] / max(res["points_in"], 1)
        logging.info(f"    kept {res['points_out']:,} of {res['points_in']:,} points ({share:.0%}).")
        results[row.tile] = {"tile": row.tile, "project": row.project, **res, "error": ""}
        if not keep_raw and os.path.exists(raw_path):
            os.remove(raw_path)

    def save_results():
        if results:
            pd.DataFrame(list(results.values())).to_csv(results_path, index=False)

    failed = []
    with requests.Session() as session:
        for k, row in enumerate(rows, start=1):
            try:
                process(k, len(rows), row, session)
            except ThrottledError as e:
                logging.warning(f"    {row.tile} set aside: {e}. It will be tried again at the end.")
                failed.append(row)
                results[row.tile] = {"tile": row.tile, "project": row.project, "error": str(e)}
            except Exception as e:
                logging.error(f"    {row.tile} failed: {type(e).__name__}: {e}")
                results[row.tile] = {"tile": row.tile, "project": row.project, "error": str(e)}
            save_results()

        if failed:
            logging.info(f"Trying the {len(failed)} tile(s) set aside again, after a 5-minute pause...")
            time.sleep(300)
            for k, row in enumerate(failed, start=1):
                try:
                    process(k, len(failed), row, session)
                except Exception as e:
                    logging.error(f"    {row.tile} failed again: {type(e).__name__}: {e}")
                    results[row.tile] = {"tile": row.tile, "project": row.project, "error": str(e)}
                save_results()
    return results


# Original tiles tried, at most, for the check of an Amazon copy.
CHECK_TILES_TRIED = 3
# Size limits (MB) for the original tile downloaded for that check.
CHECK_TILE_MAX_MB = 120
CHECK_TILE_MAX_MB_FALLBACK = 300


def check_aws_copy(name: str, project_tiles: pd.DataFrame, ept_path: str, lidar_dir: str,
                   corridor_wgs, pacer: DownloadPacer, attempts: int = 8, keep_raw: bool = False) -> dict:
    """Downloads an original USGS tile of the project that overlaps the
    corridor (CHECK_TILE_MAX_MB or less where possible, the largest overlap first) and compares it with
    the Amazon copy point by point (compare_with_original_tile)."""
    t = project_tiles.dropna(subset=["min_lon"]).copy()
    # Tiles up to CHECK_TILE_MAX_MB are used; where a project has none that
    # small (the 2020 DRCOG tiles in Colorado are 120 to 270 MB), up to
    # CHECK_TILE_MAX_MB_FALLBACK.
    small = t[t["size_mb"] <= CHECK_TILE_MAX_MB]
    t = small if not small.empty else t[t["size_mb"] <= CHECK_TILE_MAX_MB_FALLBACK]
    if t.empty:
        return {"pairs": 0, "note": "no original tile of a usable size"}
    t["overlap"] = [box(r.min_lon, r.min_lat, r.max_lon, r.max_lat).intersection(corridor_wgs).area
                    for r in t.itertuples()]
    check_dir = os.path.join(lidar_dir, "aws_check")
    os.makedirs(check_dir, exist_ok=True)
    # The tile overlapping the corridor most is tried first. A tile can still
    # have no points in the corridor (water, or a tile outline larger than its
    # points), so up to CHECK_TILES_TRIED tiles are tried until one pairs.
    tried = []
    for _, row in t.sort_values("overlap", ascending=False).head(CHECK_TILES_TRIED).iterrows():
        tile_path = os.path.join(check_dir, row["tile"])
        logging.info(f"    Checking the Amazon copy of {name} against the original USGS tile {row['tile']} "
                     f"({row['size_mb']:.0f} MB)...")
        if not os.path.exists(tile_path):
            with requests.Session() as session:
                download_file(row["url"], tile_path, session, pacer, attempts=attempts)
        tile_crs = read_tile_crs(tile_path)
        if tile_crs is None:
            tile_crs, _ = infer_tile_crs(tile_path, (row["min_lon"], row["min_lat"], row["max_lon"], row["max_lat"]), [])
        res = compare_with_original_tile(ept_path, tile_path, corridor_wgs, project_crs=tile_crs)
        res["tile"] = row["tile"]
        if not keep_raw:
            try:
                os.remove(tile_path)
            except OSError:
                pass
        if res.get("pairs", 0) and "dx_median_m" in res:
            break
        tried.append(f"{row['tile']}: {res.get('note', 'no pairs')}")
        logging.info(f"      {res.get('note', 'no points paired')}; trying the next tile.")
    if tried:
        res["tiles_without_pairs"] = tried
    if res.get("pairs", 0) and "dx_median_m" in res:
        logging.info(f"    {res['pairs']:,} points paired. Amazon copy minus original, median: "
                     f"x {res['dx_median_m']:+.3f} m, y {res['dy_median_m']:+.3f} m, z {res['dz_median_m']:+.3f} m; "
                     f"95% of points within {max(res['dx_p95_abs_m'], res['dy_p95_abs_m']):.3f} m horizontally; "
                     f"{res['share_over_5cm']:.4%} of points more than 5 cm apart "
                     f"({res['pairs_dropped_ambiguous']:,} pairs with a repeated GPS time left out).")
        if max(abs(res["dx_median_m"]), abs(res["dy_median_m"]), abs(res["dz_median_m"])) > 0.05 or \
                max(res["dx_p95_abs_m"], res["dy_p95_abs_m"], res["dz_p95_abs_m"]) > 0.10:
            logging.warning(f"    The Amazon copy of {name} differs from the original tiles by more than "
                            f"5 cm (z median {res['dz_median_m']:+.3f} m). Its points are not used for "
                            f"accuracy work until this is settled.")
            res["shift_over_5cm"] = True
        else:
            res["shift_over_5cm"] = False
    else:
        logging.warning(f"    Could not compare the Amazon copy of {name} with an original tile "
                        f"({len(tried)} tried): {res.get('note', '')}")
    return res

# ===========================================================================
# Bridge deck elevations from lidar
# ===========================================================================
# The 3DEP 1 m DEM is a bare-earth surface: bridge decks are removed, and
# rat_core.repair_structure_spans() draws each bridge as a straight line
# between its abutments. The lidar points the DEM is made from still hold
# the deck. lidar_bridge_decks() reads them in a short window around each
# bridge span and replaces the straight line with the measured deck.
#
# Lidar surveys often stop at the shoreline, so the deck over open water may
# have no points (Mount Hope and Newport bridges, Rhode Island). There the
# deck is a straight line between the last measured points ("gap line"),
# raised where needed to the NBI navigation clearance ("clearance floor"):
# the deck cannot be lower than the water plus the clearance, so each
# measured approach grade is carried into the gap until it reaches that
# height, and the deck runs level between. The floor only raises the line.

# Vertex elevation sources (Elev_Source in the vertices tables).
ELEV_SOURCE_DEM = 0         # the DEM (no structure)
ELEV_SOURCE_SPAN = 1        # straight line across a structure span (no lidar used)
ELEV_SOURCE_LIDAR = 2       # lidar deck
ELEV_SOURCE_GAP = 3         # straight line across a stretch of deck with no lidar points
ELEV_SOURCE_FLOOR = 4       # raised to the NBI navigation clearance
ELEV_SOURCE_LABELS = {0: "DEM", 1: "Span line", 2: "Lidar deck", 3: "Gap line", 4: "Clearance floor"}
# Sources that are not measured: vertical curves touching them are left out
# of the curve tables (rat_core.vertical_curves_on_measured).
ELEV_SOURCES_UNMEASURED = (ELEV_SOURCE_GAP, ELEV_SOURCE_FLOOR)

# Window read around each bridge span: the span plus LIDAR_WINDOW_PAD_FT of
# road at each end (ground there is compared with the DEM), to
# LIDAR_WINDOW_HALF_WIDTH_FT each side of the HPMS line.
LIDAR_WINDOW_PAD_FT = 300.0
LIDAR_WINDOW_HALF_WIDTH_FT = 60.0
# Deck points: within LIDAR_DECK_HALF_WIDTH_FT of the line (the travel
# lanes; on the Mount Hope bridge the deck center is 3-7 ft right of the
# line), median per LIDAR_BIN_FT after dropping points more than
# LIDAR_BIN_OUTLIER_FT from the bin's median (vehicles, railings), at least
# LIDAR_MIN_BIN_POINTS points. A bin more than LIDAR_SPIKE_FT from the median
# of the bins around it is dropped.
LIDAR_DECK_HALF_WIDTH_FT = 20.0
LIDAR_BIN_FT = 25.0
LIDAR_BIN_OUTLIER_FT = 3.0
LIDAR_MIN_BIN_POINTS = 5
LIDAR_SPIKE_FT = 2.0
# ASPRS classes: 2 ground, 17 bridge deck. Projects that do not classify
# decks: unclassified points (0, 1) at least LIDAR_ABOVE_GROUND_FT above the
# DEM ground are taken instead.
LIDAR_CLASS_GROUND = 2
LIDAR_CLASS_DECK = 17
LIDAR_CLASSES_UNCLASSIFIED = (0, 1)
LIDAR_ABOVE_GROUND_FT = 3.0
# Vertical check against the DEM: lidar ground within LIDAR_GROUND_OFFSET_FT
# of the line, off the span, minus the DEM. The median (at least
# LIDAR_MIN_GROUND_POINTS points) is removed from the deck elevations; a
# project whose median is more than LIDAR_DATUM_MAX_FT is not used.
LIDAR_GROUND_OFFSET_FT = 6.0
LIDAR_MIN_GROUND_POINTS = 30
LIDAR_DATUM_MAX_FT = 3.0
# Measured deck near a span end must be within LIDAR_END_MATCH_FT of the
# span's end elevation from the DEM (else the points belong to another
# structure, such as a parallel bridge, and the lidar is not used).
LIDAR_END_CHECK_FT = 150.0
LIDAR_END_MATCH_FT = 5.0
# Bins up to LIDAR_MAX_INTERP_FT apart are joined as measured deck; longer
# stretches with no bins are gaps.
LIDAR_MAX_INTERP_FT = 100.0
# Approach grade carried into a gap: fitted to the measured deck within
# LIDAR_GRADE_FIT_FT of the gap, at most LIDAR_MAX_GRADE.
LIDAR_GRADE_FIT_FT = 200.0
LIDAR_MAX_GRADE = 0.15
# NBI Item 39 values above this (m) are not a clearance (999.9 and the like).
LIDAR_MAX_NAV_CLEARANCE_M = 100.0
# Downloads: octree levels down to points about LIDAR_EPT_RESOLUTION_M apart;
# at most LIDAR_MAX_PROJECTS projects per window, newest first (older ones
# fill bins the newer ones lack); projects not in the Amazon copy are read
# from whole USGS tiles (LIDAR_TILE_FALLBACK) when they total no more than
# LIDAR_MAX_TILE_MB, requests LIDAR_TILE_PAUSE_S apart.
LIDAR_EPT_RESOLUTION_M = 0.5
LIDAR_MAX_PROJECTS = 3
LIDAR_TILE_FALLBACK = True
LIDAR_MAX_TILE_MB = 600.0
LIDAR_TILE_PAUSE_S = 5.0
# After this many bridges in a row fail to download (no network, or the
# sources are down), lidar is not tried again in this process.
LIDAR_MAX_NETWORK_FAILURES = 3
# An empty lidar listing from The National Map is asked again this many
# times, this many seconds apart (it sometimes lists nothing for an area it
# lists at other times).
LIDAR_TNM_EMPTY_RETRIES = 3
LIDAR_TNM_EMPTY_WAIT_S = 20.0

_FT_PER_M = 3.28084
_network_failures = 0
_disabled_reason = None


def lidar_package_problem() -> Optional[str]:
    """None if laspy and a LAZ backend are installed, else what is missing."""
    try:
        import laspy
    except ImportError:
        return 'the laspy package is not installed (pip install "laspy[lazrs]")'
    try:
        if not laspy.LazBackend.detect_available():
            return 'laspy has no LAZ backend (pip install "laspy[lazrs]")'
    except Exception:
        return 'laspy has no LAZ backend (pip install "laspy[lazrs]")'
    return None


def lidar_disabled_reason() -> Optional[str]:
    """Why lidar is not being used in this process, or None."""
    return _disabled_reason


def _disable(reason: str) -> None:
    global _disabled_reason
    if _disabled_reason is None:
        _disabled_reason = reason
        logging.warning(f"Bridge decks from lidar are off for the rest of this run: {reason}. "
                        f"Bridges keep the straight line between their abutments.")


def _safe_name(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", str(text)).strip("_") or "project"


def _window_key(bounds: tuple) -> str:
    """Cache key for a window: its bounds rounded to about 10 m."""
    return "_".join(f"{v:.4f}" for v in bounds).replace("-", "m")


def _write_json_atomic(path: str, data) -> None:
    tmp = path + ".part"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1, default=str)
    os.replace(tmp, path)


def fetch_window_points(area_wgs, cache_dir: str, label: str = "") -> List[Dict]:
    """
    Lidar point files for an area (EPSG:4326 polygon), newest project first:
    [{"project", "year", "source" ("aws" or "usgs tiles"), "files": [...]}].
    Projects are found through The National Map; points come from the Amazon
    copy of 3DEP, read for the area only, else from whole USGS tiles clipped
    to it (LIDAR_TILE_FALLBACK, LIDAR_MAX_TILE_MB). Results are kept in
    cache_dir (an index file per area and one LAZ file per project), so a
    later run reads them from disk. An empty listing is not kept: The National
    Map sometimes lists nothing for an area it lists at other times, so the
    next run asks again.
    A project that cannot be downloaded is listed with an "error" and the
    other projects are still returned; the index is then not saved, so the
    next run tries that project again (the others are read from disk).
    Network errors are raised to the caller only when no project could be
    read (or The National Map cannot be reached).
    """
    key = _window_key(area_wgs.bounds)
    index_dir = os.path.join(cache_dir, "windows")
    os.makedirs(index_dir, exist_ok=True)
    index_path = os.path.join(index_dir, f"{key}.json")
    if os.path.exists(index_path):
        try:
            with open(index_path, encoding="utf-8") as f:
                entries = json.load(f)
            if entries and all(os.path.exists(p) for e in entries for p in e.get("files", [])):
                return entries
        except (OSError, ValueError):
            pass

    items = query_tnm_lidar(area_wgs.bounds, empty_retries=LIDAR_TNM_EMPTY_RETRIES,
                            empty_wait_s=LIDAR_TNM_EMPTY_WAIT_S, attempts=2)
    tiles = list_lidar_tiles(items, area_wgs)
    entries, failures = [], []
    if not tiles.empty:
        order = (tiles.groupby("project").agg(year=("year", "max"), pub=("publication_date", "max"),
                                             mb=("size_mb", "sum"))
                 .sort_values(["year", "pub"], ascending=False))
        pacer = DownloadPacer(LIDAR_TILE_PAUSE_S)
        with requests.Session() as s:
            for project in list(order.index)[:LIDAR_MAX_PROJECTS]:
                pdir = os.path.join(cache_dir, _safe_name(project))
                os.makedirs(pdir, exist_ok=True)
                entry = {"project": project, "year": int(order.loc[project, "year"]), "files": []}
                try:
                    _fetch_project(entry, project, tiles, order, s, pacer, pdir, key, area_wgs, cache_dir, label)
                except Exception as e:
                    entry["error"] = f"{type(e).__name__}: {str(e)[:160]}"
                    failures.append(e)
                entries.append(entry)
    if failures and not any(e.get("files") for e in entries):
        raise failures[0]
    for e in entries:
        if e.get("error"):
            logging.warning(f"Lidar project {e['project']} for a bridge on {label} could not be downloaded "
                            f"({e['error']}); the deck is built from the other projects. Run again to add it.")
    if entries and not failures:
        _write_json_atomic(index_path, entries)
    return entries


def _fetch_project(entry: Dict, project: str, tiles, order, s, pacer, pdir: str, key: str,
                   area_wgs, cache_dir: str, label: str) -> None:
    """Reads one project's points for the area into pdir (see
    fetch_window_points), filling entry["source"] and entry["files"].
    Network errors are raised."""
    name, info = find_ept(project, s)
    if name:
        entry["source"] = "aws"
        out = os.path.join(pdir, f"{key}.laz")
        if not os.path.exists(out):
            fetch_ept_area(name, info, area_wgs, out, label=label,
                           max_depth=ept_depth_for_resolution(info, LIDAR_EPT_RESOLUTION_M),
                           quiet=True)
        if os.path.exists(out):
            entry["files"].append(out)
    elif LIDAR_TILE_FALLBACK and float(order.loc[project, "mb"]) <= LIDAR_MAX_TILE_MB:
        entry["source"] = "usgs tiles"
        raw_dir = os.path.join(cache_dir, "_raw")
        os.makedirs(raw_dir, exist_ok=True)
        for row in tiles[tiles["project"] == project].itertuples(index=False):
            out = os.path.join(pdir, f"{key}_{row.tile}")
            if not os.path.exists(out):
                raw = os.path.join(raw_dir, row.tile)
                download_file(row.url, raw, s, pacer, attempts=4)
                crs = read_tile_crs(raw)
                source = "file"
                if crs is None:
                    crs, _ = infer_tile_crs(raw, (row.min_lon, row.min_lat, row.max_lon, row.max_lat), [])
                    source = "inferred"
                if crs is not None:
                    clip_laz_to_corridor(raw, out, area_wgs, crs=crs, crs_source=source)
                try:
                    os.remove(raw)
                except OSError:
                    pass
            if os.path.exists(out):
                entry["files"].append(out)
    else:
        entry["source"] = "not read (whole tiles over the size limit)"


def _load_points(path: str, to_utm_from) -> Optional[Dict[str, np.ndarray]]:
    """x, y (UTM m), z (m) and class of the points in a LAS/LAZ file.
    to_utm_from(crs) returns a Transformer from the file's system to UTM."""
    import laspy
    las = laspy.read(path)
    try:
        crs_full = las.header.parse_crs()
    except Exception:
        crs_full = None
    if crs_full is None or len(las.points) == 0:
        return None
    crs = _horizontal_crs(crs_full)
    _, z_to_m, _ = _tile_units(crs_full)
    x, y = to_utm_from(crs).transform(np.asarray(las.x), np.asarray(las.y))
    return {"x": np.asarray(x), "y": np.asarray(y), "z": np.asarray(las.z) * z_to_m,
            "cls": np.asarray(las.classification).astype(int)}


def _robust_bins(bin_idx: np.ndarray, z_ft: np.ndarray) -> Dict[int, float]:
    """Median deck elevation (ft) per bin, after dropping points more than
    LIDAR_BIN_OUTLIER_FT from the bin median, then bins that stand out from
    their neighbors (spikes)."""
    out = {}
    if len(bin_idx) == 0:
        return out
    order = np.argsort(bin_idx, kind="mergesort")
    b_sorted, z_sorted = bin_idx[order], z_ft[order]
    starts = np.r_[0, np.flatnonzero(np.diff(b_sorted)) + 1]
    ends = np.r_[starts[1:], len(b_sorted)]
    for s, e in zip(starts, ends):
        zz = z_sorted[s:e]
        med = np.median(zz)
        keep = zz[np.abs(zz - med) <= LIDAR_BIN_OUTLIER_FT]
        if len(keep) >= LIDAR_MIN_BIN_POINTS:
            out[int(b_sorted[s])] = float(np.median(keep))
    keys = sorted(out)
    vals = np.array([out[k] for k in keys])
    bad = set()
    for j, k in enumerate(keys):
        near = [vals[i] for i in range(max(0, j - 2), min(len(keys), j + 3))
                if i != j and abs(keys[i] - k) <= 3]
        if len(near) >= 2 and abs(vals[j] - np.median(near)) > LIDAR_SPIKE_FT:
            bad.add(k)
    return {k: v for k, v in out.items() if k not in bad}


def _approach_grade(pos: np.ndarray, z: np.ndarray, at: int, side: str, spacing_m: float) -> Optional[float]:
    """Grade (rise per run, toward the gap) of the measured deck within
    LIDAR_GRADE_FIT_FT of a gap end at index `at`; None with too few points."""
    reach = LIDAR_GRADE_FIT_FT / _FT_PER_M / spacing_m
    m = (pos <= at) & (pos >= at - reach) if side == "before" else (pos >= at) & (pos <= at + reach)
    if m.sum() < 2:
        return None
    slope = np.polyfit(pos[m] * spacing_m, z[m], 1)[0]
    g = slope if side == "before" else -slope
    return float(np.clip(g, -LIDAR_MAX_GRADE, LIDAR_MAX_GRADE))


def _clearance_floor(p: int, q: int, zp: float, zq: float, g_in: Optional[float], g_out: Optional[float],
                     floor_m: float, spacing_m: float) -> Optional[np.ndarray]:
    """
    Deck from index p to q (both measured ends) under the clearance floor
    rule. Each approach grade that climbs into the gap is carried on until it
    reaches floor_m; the deck is level between the two points where they
    reach it. Where only one approach reaches the floor within the gap, the
    deck runs straight from that point to the other end; where the two lines
    meet below the floor, the deck follows them to where they meet. The
    result is never below the straight line from p to q. None when the floor
    is no higher than both ends or neither approach reaches it.
    """
    if floor_m <= max(zp, zq):
        return None
    d = (np.arange(p, q + 1) - p) * spacing_m
    L = d[-1]
    if L <= 0:
        return None
    line = zp + (zq - zp) * d / L
    r_in = (floor_m - zp) / g_in if (g_in is not None and g_in > 0) else np.inf
    r_out = L - (floor_m - zq) / g_out if (g_out is not None and g_out > 0) else -np.inf
    use_in, use_out = r_in < L, r_out > 0
    if use_in and use_out:
        prof = np.minimum.reduce([zp + g_in * d, zq + g_out * (L - d), np.full_like(d, floor_m)])
    elif use_in:
        prof = np.where(d <= r_in, zp + g_in * d, floor_m + (zq - floor_m) * (d - r_in) / (L - r_in))
    elif use_out:
        prof = np.where(d >= r_out, zq + g_out * (L - d), zp + (floor_m - zp) * d / r_out)
    else:
        return None
    return np.maximum(prof, line)


def _span_runs(kind: np.ndarray, which: int = 1) -> List[Tuple[int, int]]:
    m = np.asarray(kind) == which
    if not m.any():
        return []
    edges = np.diff(np.r_[0, m.astype(int), 0])
    return list(zip(np.flatnonzero(edges == 1), np.flatnonzero(edges == -1) - 1))


def lidar_bridge_decks(z_fixed: np.ndarray, z_raw: np.ndarray, kind: np.ndarray,
                       coords_m_raw: List[Tuple[float, float]], spacing_m: float, utm_epsg: int,
                       cache_dir: str, params: Dict, records: Optional[List[Dict]] = None,
                       label: str = "", point_source=None) -> Tuple[np.ndarray, np.ndarray, List[Dict]]:
    """
    Replaces the straight line across each bridge span of a profile with the
    deck measured from lidar (see the notes above this function).

    z_fixed: profile after rat_core.repair_structure_spans (m); z_raw: DEM
    elevations along the line (m); kind: structure kind per point (1 bridge,
    2 tunnel); coords_m_raw: the densified HPMS line in UTM (utm_epsg);
    records: structure records from rat_core.build_structure_mask (NBI points
    carry "nav_clear_m"). Bridges shorter than LIDAR_MIN_SPAN_FT (params) are
    left as they are. point_source(area_wgs) can replace the download (it
    returns the same list as fetch_window_points); used for tests.

    Returns (z, source, notes): the profile, the elevation source of each
    point (ELEV_SOURCE_*), and one note per bridge tried.
    """
    z_out = np.array(z_fixed, dtype=float, copy=True)
    n = len(z_out)
    source = np.where(np.asarray(kind) > 0, ELEV_SOURCE_SPAN, ELEV_SOURCE_DEM).astype(int)
    notes = []
    runs = [(a, b) for a, b in _span_runs(kind, 1)
            if (b - a) * spacing_m * _FT_PER_M >= float(params.get("LIDAR_MIN_SPAN_FT", 300.0))]
    if not runs:
        return z_out, source, notes
    global _network_failures
    if point_source is None:
        if _disabled_reason is not None:
            return z_out, source, notes
        problem = lidar_package_problem()
        if problem:
            _disable(problem)
            return z_out, source, notes

    xy = np.asarray(coords_m_raw, dtype=float)
    tx, ty = np.gradient(xy[:, 0]), np.gradient(xy[:, 1])
    nrm = np.maximum(np.hypot(tx, ty), 1e-9)
    tx, ty = tx / nrm, ty / nrm
    tree = cKDTree(xy)
    to_wgs = Transformer.from_crs(f"EPSG:{utm_epsg}", "EPSG:4326", always_xy=True)
    transformers = {}

    def to_utm_from(crs):
        k = crs.to_wkt()
        if k not in transformers:
            transformers[k] = Transformer.from_crs(crs, f"EPSG:{utm_epsg}", always_xy=True)
        return transformers[k]

    pad = int(round(LIDAR_WINDOW_PAD_FT / _FT_PER_M / spacing_m))
    bin_pts = max(1, int(round(LIDAR_BIN_FT / _FT_PER_M / spacing_m)))
    interp_pts = LIDAR_MAX_INTERP_FT / _FT_PER_M / spacing_m
    end_pts = LIDAR_END_CHECK_FT / _FT_PER_M / spacing_m
    tol_pts = float(params.get("STRUCTURE_MATCH_TOL_FT", 300.0)) / _FT_PER_M / spacing_m

    for a, b in runs:
        if point_source is None and _disabled_reason is not None:
            break
        note = {"label": label, "start": int(a), "end": int(b),
                "length_ft": round((b - a) * spacing_m * _FT_PER_M), "used": False}
        notes.append(note)
        a0, b0 = max(0, a - pad), min(n - 1, b + pad)
        area_utm = LineString(xy[a0:b0 + 1]).buffer(LIDAR_WINDOW_HALF_WIDTH_FT / _FT_PER_M)
        area_wgs = reproject(area_utm, to_wgs)
        try:
            entries = point_source(area_wgs) if point_source else fetch_window_points(area_wgs, cache_dir, label)
            _network_failures = 0
        except Exception as e:
            why = f"{type(e).__name__}: {str(e)[:160]}"
            note["reason"] = f"download failed ({why})"
            if point_source is None:
                _network_failures += 1
                logging.warning(f"Lidar for a bridge on {label} could not be read ({why}).")
                if _network_failures >= LIDAR_MAX_NETWORK_FAILURES:
                    _disable(f"{_network_failures} bridges in a row could not be downloaded")
            continue
        if not entries:
            note["reason"] = "no lidar covers the bridge"
            continue

        merged: Dict[int, float] = {}
        used = []
        missing = [e["project"] for e in entries if e.get("error") and not e.get("files")]
        partly = [e["project"] for e in entries if e.get("error") and e.get("files")]
        if missing:
            note["missing"] = missing
        if partly:
            note["partly"] = partly
        for entry in entries:
            parts = [p for p in (_load_points(f, to_utm_from) for f in entry.get("files", [])) if p]
            if not parts:
                continue
            P = {k: np.concatenate([p[k] for p in parts]) for k in ("x", "y", "z", "cls")}
            dist, idx = tree.query(np.c_[P["x"], P["y"]])
            idx = np.clip(idx, 0, n - 1)
            off_ft = ((P["x"] - xy[idx, 0]) * ty[idx] - (P["y"] - xy[idx, 1]) * tx[idx]) * _FT_PER_M
            z_ft = P["z"] * _FT_PER_M
            ground_ft = z_raw[idx] * _FT_PER_M
            inside = (idx >= a) & (idx <= b)
            g = ((P["cls"] == LIDAR_CLASS_GROUND) & (np.abs(off_ft) <= LIDAR_GROUND_OFFSET_FT)
                 & ~inside & (idx >= a0) & (idx <= b0))
            shift = 0.0
            if g.sum() >= LIDAR_MIN_GROUND_POINTS:
                shift = float(np.median(z_ft[g] - ground_ft[g]))
                if abs(shift) > LIDAR_DATUM_MAX_FT:
                    note.setdefault("rejected", []).append(
                        f"{entry['project']}: ground {shift:+.1f} ft from the DEM")
                    continue
            band = inside & (np.abs(off_ft) <= LIDAR_DECK_HALF_WIDTH_FT)
            deck = band & (P["cls"] == LIDAR_CLASS_DECK)
            bins = _robust_bins((idx[deck] - a) // bin_pts, z_ft[deck] - shift)
            # Bins with no deck-class points: unclassified points well above
            # the DEM ground (projects that classify decks only in part).
            other = (band & np.isin(P["cls"], LIDAR_CLASSES_UNCLASSIFIED)
                     & (z_ft - ground_ft >= LIDAR_ABOVE_GROUND_FT))
            if other.any():
                for k, val in _robust_bins((idx[other] - a) // bin_pts, z_ft[other] - shift).items():
                    bins.setdefault(k, val)
            new = {k: v for k, v in bins.items() if k not in merged}
            if new:
                merged.update(new)
                used.append(f"{entry['project']} ({len(new)} bins, ground {shift:+.2f} ft)")
        if not merged:
            note["reason"] = "no deck points in the lidar"
            continue

        keys = np.array(sorted(merged))
        pos = np.minimum(a + keys * bin_pts + bin_pts / 2.0, b)
        zb = np.array([merged[k] for k in keys]) / _FT_PER_M        # m
        za, zb_end = z_fixed[a], z_fixed[b]
        bad_end = []
        for at, z_end in ((a, za), (b, zb_end)):
            near = np.abs(pos - at) <= end_pts
            if near.any():
                j = np.argmin(np.abs(pos - at))
                if abs(zb[j] - z_end) * _FT_PER_M > LIDAR_END_MATCH_FT:
                    bad_end.append(f"{(zb[j] - z_end) * _FT_PER_M:+.1f} ft")
        if bad_end:
            note["reason"] = f"lidar deck does not meet the span ends ({', '.join(bad_end)})"
            continue

        # Known points: span ends (DEM) and deck bins; join or bridge each pair.
        kp = np.r_[a, pos, b]
        kz = np.r_[za, zb, zb_end]
        nav = [r.get("nav_clear_m") for r in (records or [])
               if r.get("source") == 2 and r.get("kind") == 1 and a - tol_pts <= r.get("at", -1e9) <= b + tol_pts
               and r.get("nav_clear_m") and np.isfinite(r.get("nav_clear_m"))
               and 0 < r.get("nav_clear_m") <= LIDAR_MAX_NAV_CLEARANCE_M]
        nav_m = max(nav) if nav else None
        seg = z_out.copy()
        src = source.copy()
        gap_ft = floor_ft = 0.0
        for j in range(len(kp) - 1):
            p, q = int(round(kp[j])), int(round(kp[j + 1]))
            if q <= p:
                continue
            line = np.linspace(kz[j], kz[j + 1], q - p + 1)
            if (kp[j + 1] - kp[j]) <= interp_pts:
                seg[p:q + 1] = line
                src[p:q + 1] = ELEV_SOURCE_LIDAR
                continue
            seg[p:q + 1] = line
            src[p + 1:q] = ELEV_SOURCE_GAP
            gap_ft += (q - p) * spacing_m * _FT_PER_M
            if nav_m is not None and 0 < j < len(kp) - 2:
                water = float(np.nanpercentile(z_raw[p:q + 1], 5))
                g_in = _approach_grade(pos, zb, p, "before", spacing_m)
                g_out = _approach_grade(pos, zb, q, "after", spacing_m)
                fl = _clearance_floor(p, q, kz[j], kz[j + 1], g_in, g_out, water + nav_m, spacing_m)
                if fl is not None:
                    raised = fl > line + 0.003
                    seg[p:q + 1] = fl
                    src[p:q + 1][raised] = ELEV_SOURCE_FLOOR
                    floor_ft = max(floor_ft, (water + nav_m) * _FT_PER_M)
        z_out[a:b + 1] = seg[a:b + 1]
        source[a:b + 1] = src[a:b + 1]
        # Span ends and deck bins left as span line are measured points.
        rem = source[a:b + 1] == ELEV_SOURCE_SPAN
        source[a:b + 1][rem] = ELEV_SOURCE_LIDAR
        measured_ft = float((src[a:b + 1] == ELEV_SOURCE_LIDAR).sum() * spacing_m * _FT_PER_M)
        note.update(used=True, projects=used, bins=int(len(keys)), measured_ft=round(measured_ft),
                    gap_ft=round(gap_ft), floor_ft=round(floor_ft, 1) if floor_ft else None,
                    nav_clearance_ft=round(nav_m * _FT_PER_M, 1) if nav_m else None,
                    max_deck_ft=round(float(np.max(z_out[a:b + 1][src[a:b + 1] == ELEV_SOURCE_LIDAR]))
                                      * _FT_PER_M, 1) if measured_ft else None)
    return z_out, source, notes


def describe_deck_note(note: Dict) -> str:
    """One Run Log line for a bridge note from lidar_bridge_decks."""
    where = f"{note.get('label', '')} bridge ({note.get('length_ft', 0):,} ft)"
    if not note.get("used"):
        extra = f"; {'; '.join(note['rejected'])}" if note.get("rejected") else ""
        extra += _download_note(note)
        return f"Lidar deck not used on {where}: {note.get('reason', 'no deck points')}{extra}."
    text = (f"Lidar deck on {where}: {note['measured_ft']:,} ft measured, highest measured point "
            f"{note['max_deck_ft'] or 0:,.1f} ft, from {'; '.join(note['projects'])}")
    if note.get("gap_ft"):
        text += f"; {note['gap_ft']:,} ft with no deck points drawn as a straight line"
        if note.get("floor_ft"):
            text += (f", raised to {note['floor_ft']:,.1f} ft (water plus the NBI navigation clearance "
                     f"of {note['nav_clearance_ft']:,.1f} ft)")
    return text + _download_note(note) + "."


def _download_note(note: Dict) -> str:
    """Run Log text for projects that were not downloaded, or only in part."""
    parts = []
    if note.get("missing"):
        parts.append(f"not downloaded: {', '.join(note['missing'])}")
    if note.get("partly"):
        parts.append(f"only partly downloaded: {', '.join(note['partly'])}")
    return f"; {'; '.join(parts)} (run again to complete)" if parts else ""