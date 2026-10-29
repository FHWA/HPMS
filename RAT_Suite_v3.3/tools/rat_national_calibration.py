# tools/rat_national_calibration.py

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
RAT NATIONAL CALIBRATION -- HORIZONTAL SMOOTHING FACTORS
--------------------------------------------------------------------------------
Calibrates the horizontal smoothing factor (H_SMOOTH_FACTOR and its FS2-FS7
variants) for each state and functional system from the HPMS line work alone.
No DEM data is downloaded.

Method. Each state/FS is sliced into 1-mile chunks densified at 10 ft (about
528 points, the reference length of core.rat_core.spline_s, so the selected
spline s is the smoothing factor itself). A Cochran-sized random sample of
chunks is smoothed at every value in VITERBI_S_GRID, curvature is classified
with RAT Suite's production Viterbi decoder, and the curve runs at each s are
compared with those at the previous s (length-weighted overlap). The selected
factor is the smallest s that begins FIXED_DURABLE_WINDOW consecutive
transitions at or above the FS threshold, capped at VITERBI_MAX_SELECTABLE_S.

Fidelity. The smoothed line must stay close to the HPMS line: at each s the
99th percentile of the sample's departures (smoothed vs. HPMS point, ft) at
the points the decoder labels as curve is measured (tangent points barely
move and would dilute it), and a factor is selectable only if it is within
MAX_DEPARTURE_P99_FT (half a 12 ft lane). Where the stable factor exceeds
that, the largest grid value that meets it is selected instead (dense urban
grids, where the classifier only settles once short curves and corners are
smoothed away, e.g. DC FS3 at 600 cut the corners of reverse curves by up to
25 ft). Every repeat also records that largest faithful grid value (its
fidelity cap), whether or not the classifier settled.

Repeats. Each state/FS is calibrated N_REPEATS_PER_STATE times with fresh
samples. At least MIN_CONVERGED_FRACTION of the repeats must converge. The
DROP_FRACTION of converged repeats that agree least are set aside and the
spread of the rest, (max - min) / median, must not exceed MAX_SPREAD_RATIO.
The reported value is the lower median of the kept repeats, so it is always
a value a repeat selected (a grid value). Inconsistent or unconverged cells
are left blank, and a second, _filled output fills them with the cross-state
lower median for that FS, or the state's own fidelity cap for that FS (lower
median of its repeats) where that is smaller.

Vertical. With one elevation source nationwide (USGS 3DEP), the vertical
smoothing factor is a single national value, core.rat_core.
NATIONAL_V_SMOOTH_FACTOR, and is not calibrated here. The _filled output
can be copied to core/national_smoothing_factors.json as is.

Outputs (OUTDIR). Each run starts a new audit CSV and factors JSON; files
from an earlier run are renamed with that run's file date.
  calibration_audit_CV_national.csv          every repeat plus a MEDIAN row
                                             per state/FS (spread ratio in
                                             diagnostic_value)
  national_smoothing_factors_CV_national.json        blanks as null
  national_smoothing_factors_CV_national_filled.json blanks filled
  calibration_run_<start>.log

Usage. Edit the CONFIGURATION block, then from the RAT Suite root:
    python tools/rat_national_calibration.py

CREATED BY: Federal Highway Administration, Office of Highway Policy Information.
CREATED ON: 10/14/2026
"""

import os
import sys
import csv
import json
import time
import random
import logging
import logging.handlers
import statistics
from datetime import datetime
import multiprocessing
from typing import Optional
from concurrent.futures import ThreadPoolExecutor, ProcessPoolExecutor, as_completed

import numpy as np
from scipy.interpolate import UnivariateSpline

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
RAT_SUITE_DIR = os.path.dirname(THIS_DIR)
if RAT_SUITE_DIR not in sys.path:
    sys.path.insert(0, RAT_SUITE_DIR)


# ===========================================================================
# CONFIGURATION
# ===========================================================================
OUTDIR = "output/national_cv"
SOCRATA_TOKEN = ""               # blank = the RAT_SOCRATA_TOKEN environment variable
                                 # (clear this before sharing or publishing the script)
STATES = "ALL"                   # "ALL", or a comma-separated list like "31,11"
TOTAL_CORES = None               # None = os.cpu_count()
RESERVED_CORES = 2
CURVE_DISTRIBUTION_JSON = None   # None = core/national_curve_class_proportions.json
NO_CURVE_DATA_FS = [7]           # classes with no HPMS curve data (local roads are not in the
                                 # HPMS sample): sample sized with the largest variance, 0.25
INCLUDE_MISSING_FSYSTEM = False  # records with no functional system, as class 7 (in Idaho
                                 # these are forest roads with approximate line work)
INCLUDE_MISSING_FACILITY = False # records with no facility type, as type 2

N_REPEATS_PER_STATE = 5          # independent random samples per state/FS
MIN_CONVERGED_FRACTION = 0.5     # share of repeats that must converge
MIN_FOR_DROP_ONE = 4             # drop repeats only when at least this many converged
DROP_FRACTION = 0.20             # share of converged repeats set aside before the spread check
MAX_SPREAD_RATIO = 1.0           # (max - min) / median above this: inconsistent (blank)

VITERBI_S_GRID = [5, 10, 15, 20, 30, 40, 50, 75, 100, 150, 200, 300, 400, 600,
                  800, 1200, 1800, 2500, 3500, 5000, 7500, 10000,
                  15000, 20000, 30000, 50000]
VITERBI_GAP_BRIDGE_M = 150.0     # same-sign curve runs this close are one curve (spirals)
VITERBI_STABILITY_THRESHOLD_BY_FS = {1: 0.90, 2: 0.90, 3: 0.85, 4: 0.85, 5: 0.85, 6: 0.85, 7: 0.85}
VITERBI_STABILITY_THRESHOLD_DEFAULT = 0.90
FIXED_DURABLE_WINDOW = 8         # consecutive passing transitions required
VITERBI_MAX_SELECTABLE_S = 2000  # larger values visibly distort curves (checked on plan views)
MAX_DEPARTURE_P99_FT = 6.0       # smoothed line within this of the HPMS line (99th percentile
                                 # of the departures at points labeled as curve)

from core.rat_core import (
    _viterbi_3state,
    fetch_socrata_state,
    build_params,
    generate_1mile_chunks,
    cochran_sample_size,
    spline_s,
    get_cochran_variance,
    load_curve_variance_lookup,
    _preload_chunk_for_h_sweep,
    ALL_FIPS,
    BASE_ENGINE_PARAMS,
    FEET_PER_METER,
    SOCRATA_TOKEN as ENV_SOCRATA_TOKEN,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s: %(message)s")

AUDIT_FILENAME = "calibration_audit_CV_national.csv"
JSON_FILENAME = "national_smoothing_factors_CV_national.json"
AUDIT_FIELDNAMES = [
    "timestamp", "state_fips", "f_sys", "mode", "repeat", "n_chunks_sampled",
    "selected_factor", "stabilized", "diagnostic_value", "diagnostic_units",
    "departure_p99_ft", "fidelity_cap",
]


# ===========================================================================
# HORIZONTAL: VITERBI CLASSIFIER STABILITY
# ===========================================================================
def _signed_kappa_from_xy(xs: np.ndarray, ys: np.ndarray, ds: float) -> np.ndarray:
    """Signed curvature (1/m), as in core.rat_core.analyze_horizontal_curvature."""
    dx = np.gradient(xs, ds); dy = np.gradient(ys, ds)
    ddx = np.gradient(dx, ds); ddy = np.gradient(dy, ds)
    num = dx * ddy - dy * ddx
    den = (dx ** 2 + dy ** 2) ** 1.5
    kappa = np.zeros_like(num)
    ok = den > 1e-12
    kappa[ok] = num[ok] / den[ok]
    return kappa


def _extract_runs(states: np.ndarray, d_axis: np.ndarray) -> list:
    """(start_d, end_d, sign) of every curve run."""
    runs, i, n = [], 0, len(states)
    while i < n:
        if states[i] == 0:
            i += 1
            continue
        j, cur = i, states[i]
        while i < n and states[i] == cur:
            i += 1
        runs.append([d_axis[j], d_axis[i - 1], int(cur)])
    return runs


def _bridge_same_sign_gaps(runs: list, gap_m: float) -> list:
    if not runs:
        return runs
    merged = [runs[0][:]]
    for s, e, sgn in runs[1:]:
        last = merged[-1]
        if sgn == last[2] and (s - last[1]) <= gap_m:
            last[1] = e
        else:
            merged.append([s, e, sgn])
    return merged


def _h_viterbi_runs_one_chunk(args) -> Optional[tuple]:
    """(curve runs, departures of the smoothed line from the HPMS points in
    ft, mask of the points the decoder labels as curve) for one chunk at
    spline s, or None."""
    chunk, s, kappa_thresh, stickiness = args
    try:
        d_axis = chunk["d_axis"]
        # The factor is scaled to the number of points (Eq. 3.5), as in
        # production, so end pieces shorter than a mile are smoothed alike.
        s_pts = spline_s(s, len(d_axis))
        sx = UnivariateSpline(d_axis, chunk["x_raw"], s=s_pts)
        sy = UnivariateSpline(d_axis, chunk["y_raw"], s=s_pts)
        xs, ys = sx(d_axis), sy(d_axis)
        ds = float(d_axis[1] - d_axis[0]) if len(d_axis) > 1 else 1.0
        states = _viterbi_3state(_signed_kappa_from_xy(xs, ys, ds), kappa_thresh, stickiness)
        runs = _bridge_same_sign_gaps(_extract_runs(states, d_axis), VITERBI_GAP_BRIDGE_M)
        departure_ft = np.hypot(xs - chunk["x_raw"], ys - chunk["y_raw"]) * FEET_PER_METER
        return runs, departure_ft.astype(np.float32), np.asarray(states) != 0
    except Exception as e:
        logging.warning(f"_h_viterbi_runs_one_chunk failed at s={s}: {e}")
        return None


def _length_weighted_overlap(runs_a: list, runs_b: list, min_len_m: float) -> tuple:
    """Length-weighted IoU of runs_a against runs_b (runs >= min_len_m).
    Returns (weighted_sum, weight_sum)."""
    runs_a = [r for r in runs_a if r[1] - r[0] >= min_len_m]
    runs_b = [r for r in runs_b if r[1] - r[0] >= min_len_m]
    matched, weighted, weight = set(), 0.0, 0.0
    for a_s, a_e, a_sgn in runs_a:
        a_len = a_e - a_s
        best_ov, best_j, best_union = 0.0, None, a_len
        for j, (b_s, b_e, b_sgn) in enumerate(runs_b):
            if b_sgn != a_sgn or j in matched:
                continue
            ov = max(0.0, min(a_e, b_e) - max(a_s, b_s))
            if ov > best_ov:
                best_ov, best_j, best_union = ov, j, a_len + (b_e - b_s) - ov
        if best_j is not None:
            matched.add(best_j)
        weighted += (best_ov / best_union if best_union > 0 else 0.0) * a_len
        weight += a_len
    return weighted, weight


def _curve_departure_p99(results: list) -> float:
    """99th percentile of the departures at the points labeled as curve,
    where smoothing moves the line most; tangent points, which barely move,
    would otherwise dilute the percentile. All points if none is a curve."""
    if not results:
        return float("nan")
    curve = np.concatenate([r[1][r[2]] for r in results])
    if curve.size == 0:
        curve = np.concatenate([r[1] for r in results])
    return float(np.percentile(curve, 99))


def _h_viterbi_stability_search(chunks: list, kappa_thresh: float, stickiness: float,
                                min_len_m: float, sweep_workers: int,
                                state_fips: str, f_sys: int) -> tuple:
    """Returns (selected_s or None, overlap at selection, trace), where trace
    is {"transitions": [(prev_s, s, overlap)], "departure_p99_ft": {s: ft},
    "fidelity_cap": largest grid s within MAX_DEPARTURE_P99_FT}."""
    threshold = VITERBI_STABILITY_THRESHOLD_BY_FS.get(f_sys, VITERBI_STABILITY_THRESHOLD_DEFAULT)
    prev, prev_s, transitions, departure = None, None, [], {}
    for s in VITERBI_S_GRID:
        args = [(c, s, kappa_thresh, stickiness) for c in chunks]
        with ThreadPoolExecutor(max_workers=sweep_workers) as ex:
            results = list(ex.map(_h_viterbi_runs_one_chunk, args))
        runs = [r[0] if r is not None else None for r in results]
        departure[s] = _curve_departure_p99([r for r in results if r is not None])
        if prev is not None:
            tw, tt = 0.0, 0.0
            for a, b in zip(prev, runs):
                if a is None or b is None:
                    continue
                w, t = _length_weighted_overlap(a, b, min_len_m)
                tw += w
                tt += t
            overlap = tw / tt if tt > 0 else float("nan")
            transitions.append((prev_s, s, overlap))
            logging.info(f"  [{state_fips}] FS {f_sys} [H] s={s:>6.0f}  "
                         f"overlap_vs_prev={overlap:.4f}  (threshold={threshold})  "
                         f"departure_p99={departure[s]:.1f} ft")
        prev, prev_s = runs, s
    faithful_all = [g for g in VITERBI_S_GRID
                    if g <= VITERBI_MAX_SELECTABLE_S and departure[g] <= MAX_DEPARTURE_P99_FT]
    trace = {"transitions": transitions, "departure_p99_ft": departure,
             "fidelity_cap": faithful_all[-1] if faithful_all else VITERBI_S_GRID[0]}

    for i in range(len(transitions) - FIXED_DURABLE_WINDOW + 1):
        if all(ov >= threshold for _, _, ov in transitions[i:i + FIXED_DURABLE_WINDOW]):
            selected, overlap = transitions[i][0], transitions[i][2]
            if selected > VITERBI_MAX_SELECTABLE_S:
                logging.warning(f"  [{state_fips}] FS {f_sys} [H] stable only from s={selected}, "
                                f"above the {VITERBI_MAX_SELECTABLE_S} ceiling -- left blank.")
                return None, overlap, trace
            if not departure[selected] <= MAX_DEPARTURE_P99_FT:
                faithful = [g for g in VITERBI_S_GRID if g < selected and departure[g] <= MAX_DEPARTURE_P99_FT]
                limited = faithful[-1] if faithful else VITERBI_S_GRID[0]
                logging.info(f"  [{state_fips}] FS {f_sys} [H] stable from s={selected}, but the line "
                             f"departs {departure[selected]:.1f} ft (p99) there; limited to s={limited} "
                             f"({departure[limited]:.1f} ft)")
                selected = limited
            logging.info(f"  [{state_fips}] FS {f_sys} [H] selected s={selected}")
            return selected, overlap, trace
    logging.warning(f"  [{state_fips}] FS {f_sys} [H] no {FIXED_DURABLE_WINDOW}-transition "
                    f"stable window in the grid -- left blank.")
    return None, (transitions[-1][2] if transitions else float("nan")), trace


# ===========================================================================
# REPEATS AND CROSS-STATE FILL
# ===========================================================================
def _aggregate_repeats(values: list, n_repeats: int) -> tuple:
    """values: the repeats' selected factors (None where a repeat did not
    converge). Returns (value or None, spread_ratio or None)."""
    v = sorted(x for x in values if x is not None)
    n = len(v)
    if n_repeats == 0 or n < n_repeats * MIN_CONVERGED_FRACTION:
        return None, None
    k = max(min(round(n * DROP_FRACTION), n - 3), 0) if n >= MIN_FOR_DROP_ONE else 0
    best = None
    for start in range(k + 1):
        w = v[start:start + n - k]
        m = statistics.median(w)
        ratio = (w[-1] - w[0]) / m if m > 0 else float("inf")
        if best is None or ratio < best[0]:
            best = (ratio, w)
    ratio, w = best
    if ratio > MAX_SPREAD_RATIO:
        return None, ratio
    return statistics.median_low(w), ratio


def _fs_key(prefix: str, f_sys: int) -> str:
    return prefix if f_sys == 1 else f"{prefix}_FS{f_sys}"


def _fidelity_caps(audit_csv_path: str) -> dict:
    """{(state_fips, f_sys): lower median of the repeats' fidelity caps}
    from this run's audit CSV."""
    caps = {}
    if not os.path.exists(audit_csv_path):
        return caps
    with open(audit_csv_path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if row.get("repeat") == "MEDIAN" or not row.get("fidelity_cap"):
                continue
            caps.setdefault((row["state_fips"].zfill(2), int(row["f_sys"])), []).append(float(row["fidelity_cap"]))
    return {k: statistics.median_low(v) for k, v in caps.items()}


def fill_blanks(master: dict, caps: Optional[dict] = None) -> dict:
    """Blank cells take the cross-state lower median for that key, or the
    state's own fidelity cap for that FS where it is smaller; a state's own
    calibrated value is never replaced."""
    caps = caps or {}
    keys = sorted({k for sv in master.values() for k in sv})
    fill = {}
    for key in keys:
        vals = [sv[key] for sv in master.values() if sv.get(key) is not None]
        if vals:
            fill[key] = statistics.median_low(vals)
            logging.info(f"  Cross-state fill {key} = {fill[key]} (n={len(vals)} states)")
    out = {}
    for st, sv in master.items():
        new = dict(sv)
        for k, v in sv.items():
            if v is not None or k not in fill:
                continue
            f_sys = int(k.rsplit("_FS", 1)[1]) if "_FS" in k else 1
            cap = caps.get((st, f_sys))
            new[k] = min(fill[k], cap) if cap is not None else fill[k]
            note = f" (state fidelity cap {cap:g})" if cap is not None and cap < fill[k] else ""
            logging.info(f"  Filled [{st}] {k}: blank -> {new[k]:g}{note}")
        out[st] = new
    return out


# ===========================================================================
# STATE WORKER
# ===========================================================================
_write_lock, _network_lock = None, None


def _pool_init(w_lock, n_lock, log_queue):
    global _write_lock, _network_lock
    _write_lock, _network_lock = w_lock, n_lock
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(logging.INFO)
    root.addHandler(logging.handlers.QueueHandler(log_queue))


def _write_with_retry(write_fn, state_fips: str, max_attempts: int = 8, base_delay: float = 0.5):
    """Retries on PermissionError (transient OneDrive sync locks)."""
    for attempt in range(1, max_attempts + 1):
        try:
            write_fn()
            return
        except PermissionError as e:
            if attempt == max_attempts:
                logging.error(f"[{state_fips}] File write still blocked after {max_attempts} "
                              f"attempts; this state's results were NOT saved: {e}")
                raise
            delay = base_delay * 2 ** (attempt - 1)
            logging.warning(f"[{state_fips}] File write blocked (attempt {attempt}), "
                            f"retrying in {delay:.1f}s: {e}")
            time.sleep(delay)


def process_state(state_fips: str, base_params: dict, audit_csv_path: str,
                  output_json_path: str, socrata_token: str, sweep_workers: int = 8,
                  curve_variance_lookup: Optional[dict] = None) -> tuple:
    logging.info(f"\n{'=' * 60}\n=== NATIONAL H CALIBRATION: STATE FIPS {state_fips} ===\n{'=' * 60}")
    run_timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        if _network_lock is not None:
            with _network_lock:
                df = fetch_socrata_state(state_fips, socrata_token,
                                         include_missing_facility=INCLUDE_MISSING_FACILITY,
                                         include_missing_fsystem=INCLUDE_MISSING_FSYSTEM)
        else:
            df = fetch_socrata_state(state_fips, socrata_token,
                                     include_missing_facility=INCLUDE_MISSING_FACILITY,
                                     include_missing_fsystem=INCLUDE_MISSING_FSYSTEM)
    except Exception as e:
        logging.error(f"[{state_fips}] Socrata fetch failed: {e}")
        return state_fips, {}, []

    kappa_thresh = 1.0 / max(base_params.get("H_MAX_RADIUS", 165000.0 / FEET_PER_METER), 1e-6)
    stickiness = base_params.get("H_HMM_STICKINESS", 1.0)
    min_len_m = base_params.get("H_MIN_CURVE_LENGTH_M", 30.48)
    state_results, audit_rows = {}, []

    for f_sys in range(1, 8):
        sub = df[df["FSystem"] == f_sys]
        if sub.empty:
            continue
        all_chunks = []
        # Records stay in submission order (the Socrata query is ordered by
        # shapeid), as in production; generate_1mile_chunks joins them.
        for _, rg in sub.groupby("RouteId", sort=False):
            all_chunks.extend(generate_1mile_chunks(
                rg["WKT"].tolist(), start_mps=rg["Start_MP"].tolist(),
                end_mps=rg["End_MP"].tolist(),
                shape_ids=rg["ShapeId"].tolist() if "ShapeId" in rg.columns else None))
        if not all_chunks:
            continue
        variance = (0.25 if f_sys in NO_CURVE_DATA_FS
                    else get_cochran_variance(curve_variance_lookup, state_fips, f_sys))
        sample_size = cochran_sample_size(len(all_chunks), variance=variance)
        logging.info(f"[{state_fips}] FS {f_sys}: {len(all_chunks)} chunks, sample {sample_size}")

        repeat_values = []
        for rep in range(1, N_REPEATS_PER_STATE + 1):
            sampled = random.sample(all_chunks, sample_size)
            with ThreadPoolExecutor(max_workers=min(8, sweep_workers)) as ex:
                chunks = [c for c in ex.map(_preload_chunk_for_h_sweep,
                                            [(g, base_params) for g in sampled]) if c is not None]
            selected, overlap, trace = _h_viterbi_stability_search(
                chunks, kappa_thresh, stickiness, min_len_m, sweep_workers, state_fips, f_sys)
            dep = trace["departure_p99_ft"].get(selected) if selected is not None else None
            repeat_values.append(selected)
            audit_rows.append({
                "timestamp": run_timestamp, "state_fips": state_fips, "f_sys": f_sys,
                "mode": "H", "repeat": rep, "n_chunks_sampled": len(chunks),
                "selected_factor": selected if selected is not None else "",
                "stabilized": selected is not None,
                "diagnostic_value": round(overlap, 4) if overlap == overlap else "",
                "diagnostic_units": "length_weighted_overlap",
                "departure_p99_ft": round(dep, 2) if dep is not None else "",
                "fidelity_cap": trace["fidelity_cap"],
            })
            logging.info(f"  [{state_fips}] FS {f_sys} repeat {rep}/{N_REPEATS_PER_STATE}: "
                         f"H={selected if selected is not None else 'blank'}")

        final, ratio = _aggregate_repeats(repeat_values, N_REPEATS_PER_STATE)
        audit_rows.append({
            "timestamp": run_timestamp, "state_fips": state_fips, "f_sys": f_sys,
            "mode": "H", "repeat": "MEDIAN", "n_chunks_sampled": sample_size,
            "selected_factor": final if final is not None else "",
            "stabilized": final is not None,
            "diagnostic_value": round(ratio, 4) if ratio is not None else "",
            "diagnostic_units": "spread_ratio_across_repeats",
        })
        logging.info(f"  [{state_fips}] FS {f_sys} FINAL: "
                     f"H={final if final is not None else 'blank'} (spread ratio={ratio})")
        state_results[_fs_key("H_SMOOTH_FACTOR", f_sys)] = final

    def _write():
        master = {}
        if os.path.exists(output_json_path):
            with open(output_json_path) as f:
                try:
                    master = json.load(f)
                except json.JSONDecodeError:
                    pass
        master[state_fips] = state_results
        with open(output_json_path, "w") as f:
            json.dump(master, f, indent=4)
        new_file = not os.path.exists(audit_csv_path)
        with open(audit_csv_path, "a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=AUDIT_FIELDNAMES, extrasaction="ignore")
            if new_file:
                writer.writeheader()
            writer.writerows(audit_rows)

    if _write_lock is not None:
        with _write_lock:
            _write_with_retry(_write, state_fips)
    else:
        _write_with_retry(_write, state_fips)
    return state_fips, state_results, audit_rows


# ===========================================================================
# ENTRY POINT
# ===========================================================================
def _archive(path: str) -> None:
    """Renames an output left by an earlier run, adding its file date."""
    if os.path.exists(path):
        stamp = datetime.fromtimestamp(os.path.getmtime(path)).strftime("%Y%m%d_%H%M%S")
        root, ext = os.path.splitext(path)
        os.replace(path, f"{root}_{stamp}{ext}")


def main():
    os.makedirs(OUTDIR, exist_ok=True)
    audit_csv_path = os.path.join(OUTDIR, AUDIT_FILENAME)
    output_json_path = os.path.join(OUTDIR, JSON_FILENAME)
    filled_json_path = output_json_path.replace(".json", "_filled.json")
    for path in (audit_csv_path, output_json_path, filled_json_path):
        _archive(path)

    run_started = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = os.path.join(OUTDIR, f"calibration_run_{run_started}.log")
    fmt = logging.Formatter("%(asctime)s - %(levelname)s: %(message)s")
    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setFormatter(fmt)
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(fmt)

    manager = multiprocessing.Manager()
    write_lock, network_lock, log_queue = manager.Lock(), manager.Lock(), manager.Queue(-1)
    listener = logging.handlers.QueueListener(log_queue, file_handler, console_handler)
    listener.start()
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(logging.INFO)
    root.addHandler(logging.handlers.QueueHandler(log_queue))

    try:
        logging.info(f"Run log -> {log_path}")
        available = max(1, (TOTAL_CORES or os.cpu_count()) - RESERVED_CORES)
        states = ALL_FIPS if STATES.upper() == "ALL" else [s.strip().zfill(2) for s in STATES.split(",")]
        n_state_workers = 1 if len(states) == 1 else max(1, available // 5)
        n_sweep_workers = max(1, available // n_state_workers)
        base_params = build_params(BASE_ENGINE_PARAMS)
        curve_json = CURVE_DISTRIBUTION_JSON or os.path.join(
            RAT_SUITE_DIR, "core", "national_curve_class_proportions.json")
        curve_lookup = load_curve_variance_lookup(curve_json)

        logging.info(f"National H calibration: {len(states)} states, {N_REPEATS_PER_STATE} repeats "
                     f"per state/FS, {n_state_workers} state workers x {n_sweep_workers} sweep workers")

        with ProcessPoolExecutor(max_workers=n_state_workers, initializer=_pool_init,
                                 initargs=(write_lock, network_lock, log_queue)) as ex:
            futures = {ex.submit(process_state, fips, base_params, audit_csv_path, output_json_path,
                                 SOCRATA_TOKEN or ENV_SOCRATA_TOKEN, n_sweep_workers, curve_lookup): fips for fips in states}
            for fut in as_completed(futures):
                fips = futures[fut]
                try:
                    sf, _, rows = fut.result()
                    if rows:
                        logging.info(f"[{sf}] State complete ({len(rows)} audit rows).")
                    else:
                        logging.warning(f"[{sf}] State produced no audit rows (see Socrata errors above).")
                except Exception as e:
                    logging.error(f"[{fips}] State worker failed: {e}")

        if os.path.exists(output_json_path):
            with open(output_json_path) as f:
                master = json.load(f)
            with open(filled_json_path, "w") as f:
                json.dump(fill_blanks(master, _fidelity_caps(audit_csv_path)), f, indent=4)
            logging.info(f"Filled smoothing factors -> {filled_json_path}")
        logging.info("National H calibration complete.")
    finally:
        listener.stop()
        file_handler.close()


if __name__ == "__main__":
    main()