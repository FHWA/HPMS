# tools/rat_calibration_diagnostics.py

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

# to run:  (delete this before posting to GitHub)
# cd "C:\Users\David.Winter\OneDrive - DOT OST\HPPI - Working Documents (HPPI)\Shared Code\Other Code\Python\Test\RAT_Suite v3.3"  (delete this before posting to GitHub)
# py tools/rat_calibration_diagnostics.py --audit "core/calibration_audit.csv" --outdir "output/diagnostics"  (delete this before posting to GitHub)
# Example: Run from the RAT Suite root directory:
#   python tools/rat_calibration_diagnostics.py --audit core/calibration_audit.csv --outdir output/diagnostics


"""
RAT CALIBRATION THRESHOLD DIAGNOSTICS v1.0
--------------------------------------------------------------------------------
ROLE: Post-run audit CSV analyzer for threshold health assessment.
DESCRIPTION:
Reads a completed calibration_audit.csv and produces a set of diagnostic
reports and charts that answer two questions:

  1. Are the current thresholds causing too many fallback selections?
     (evidence that thresholds are too tight)

  2. Are elbow selections occurring in very flat RMSE curves with low
     confidence scores?
     (evidence that thresholds are too loose)

OUTPUTS (written to --outdir):
  diagnostics_summary.txt       -- plain-text summary of all findings
  selection_method_breakdown.csv -- per-FS counts of elbow / fallback / etc.
  threshold_proximity.csv        -- how close each selection was to the ceiling
  factor_stability.csv           -- factor variance across states per FS
  plots/                         -- PNG charts (requires matplotlib)
"""

import os
import sys
import csv
import argparse
import statistics
from collections import defaultdict

# matplotlib is optional -- charts are skipped gracefully if not installed
try:
    import matplotlib
    matplotlib.use("Agg")          # non-interactive backend, safe for scripts
    import matplotlib.pyplot as plt
    import matplotlib.ticker as ticker
    HAVE_MPL = True
except ImportError:
    HAVE_MPL = False

# ---------------------------------------------------------------------------
# Threshold constants -- must match rat_national_calibration_cli.py exactly
# ---------------------------------------------------------------------------
V_RMSE_THRESHOLDS   = {1: 4.0, 2: 4.5, 3: 3.5, 4: 3.5, 5: 4.0, 6: 4.0, 7: 4.0}
MAX_H_RMSE_FT       = {1: 3.5, 2: 3.5, 3: 3.8, 4: 3.8, 5: 5.0, 6: 5.5, 7: 5.5}
MAX_V_DEV_FT        = {1: 35.0, 2: 30.0, 3: 15.0, 4: 12.0, 5: 15.0, 6: 15.0, 7: 15.0}
MAX_H_DEV_FT        = {1: 15.0, 2: 15.0, 3: 18.0, 4: 18.0, 5: 15.0, 6: 15.0, 7: 20.0}
MOUNTAIN_BONUS_FT   = 8.0
V_MIN_RMSE_RANGE_FT = 0.05   # below this, flat_terrain_default is used for V

MOUNTAIN_STATES = {
    "02", "04", "06", "08", "16", "30", "32", "35", "41", "49", "53", "56",
    "13", "21", "23", "33", "36", "37", "42", "47", "50", "51", "54"
}

# Selection methods considered "good" (elbow found, curve was clear, or
# flat_terrain_default where national default is intentionally used because
# terrain is too flat for V calibration to produce a meaningful signal)
GOOD_METHODS    = {"elbow", "flat_curve", "flat_terrain_default"}
# Selection methods that indicate a threshold problem
FALLBACK_METHODS = {"highest_safe", "composite_fallback", "absolute_fallback"}

# Fraction of fallback selections above which we flag a threshold problem
FALLBACK_WARNING_RATE = 0.20   # 20 %
FALLBACK_ALERT_RATE   = 0.35   # 35 %

# Confidence score below which an elbow selection is considered "weak"
WEAK_ELBOW_THRESHOLD = 40

# Ceiling proximity above which we flag "too close to ceiling" (% of ceiling)
CEILING_PROXIMITY_WARNING = 85.0


# ===========================================================================
# DATA LOADING
# ===========================================================================
def load_audit(path: str) -> list[dict]:
    """Reads calibration_audit.csv and coerces numeric columns."""
    if not os.path.exists(path):
        sys.exit(f"ERROR: Audit file not found: {path}")

    numeric_cols = {
        "f_sys", "total_chunks", "sample_chunks", "n_evaluated", "n_passing",
        "rmse_rise", "selected_factor", "peak_elbow_distance",
        "v_rmse_at_baseline", "h_rmse_at_baseline",
        "maxv_at_baseline", "maxh_at_baseline",
        "v_rmse_at_selected", "h_rmse_at_selected",
        "maxv_at_selected", "maxh_at_selected",
        "std_v_rmse_at_selected", "std_h_rmse_at_selected",
        "national_default_factor", "deviation_from_default",
        "ceiling_proximity_pct", "confidence_score",
        "v_rmse_ceiling", "h_rmse_ceiling", "maxv_ceiling", "maxh_ceiling",
    }
    bool_cols = {"is_mountain_state", "override_recommended"}

    rows = []
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            for col in numeric_cols:
                raw = row.get(col, "")
                try:
                    row[col] = float(raw) if raw != "" else None
                except ValueError:
                    row[col] = None
            for col in bool_cols:
                raw = row.get(col, "").strip().lower()
                row[col] = raw in ("true", "1", "yes")
            rows.append(row)

    print(f"Loaded {len(rows)} audit rows from {path}")
    return rows


# ===========================================================================
# DIAGNOSTIC 1: SELECTION METHOD BREAKDOWN
# ===========================================================================
def analyze_selection_methods(rows: list[dict]) -> dict:
    """
    Counts how often each selection method was used, broken down by
    functional system and mode (H / V).

    A high fallback rate is the primary signal that thresholds are too tight.
    """
    # structure: results[f_sys][mode] = {method: count}
    results = defaultdict(lambda: defaultdict(lambda: defaultdict(int)))
    totals  = defaultdict(lambda: defaultdict(int))

    for row in rows:
        f_sys  = int(row["f_sys"]) if row["f_sys"] is not None else 0
        mode   = row.get("mode", "?")
        method = row.get("selection_method", "unknown")
        results[f_sys][mode][method] += 1
        totals[f_sys][mode] += 1

    # Compute fallback rates and flag problems
    findings = []
    for f_sys in sorted(results):
        for mode in sorted(results[f_sys]):
            counts = results[f_sys][mode]
            total  = totals[f_sys][mode]
            fallback_n = sum(counts.get(m, 0) for m in FALLBACK_METHODS)
            fallback_rate = fallback_n / total if total else 0.0

            flag = ""
            if fallback_rate >= FALLBACK_ALERT_RATE:
                flag = "ALERT: thresholds likely TOO TIGHT"
            elif fallback_rate >= FALLBACK_WARNING_RATE:
                flag = "WARNING: elevated fallback rate"

            findings.append({
                "f_sys": f_sys,
                "mode": mode,
                "total": total,
                "elbow": counts.get("elbow", 0),
                "flat_curve": counts.get("flat_curve", 0),
                "highest_safe": counts.get("highest_safe", 0),
                "composite_fallback": counts.get("composite_fallback", 0),
                "absolute_fallback": counts.get("absolute_fallback", 0),
                "fallback_rate_pct": round(fallback_rate * 100, 1),
                "flag": flag,
            })

    return findings


# ===========================================================================
# DIAGNOSTIC 2: CEILING PROXIMITY
# ===========================================================================
def analyze_ceiling_proximity(rows: list[dict]) -> dict:
    """
    For each elbow or flat_curve selection, checks how close the selected
    factor's RMSE was to the ceiling.

    If most elbow selections are sitting at 85-100% of the ceiling, the
    thresholds may be too loose -- the elbow is being forced into a corner
    of the passing range rather than found in a natural bend.

    If most selections are at < 50% of ceiling, thresholds are comfortable
    but may be set more tightly without losing elbow-quality selections.
    """
    # structure: by_fs_mode[f_sys][mode] = list of proximity pct values
    by_fs_mode = defaultdict(lambda: defaultdict(list))

    for row in rows:
        method = row.get("selection_method", "")
        prox   = row.get("ceiling_proximity_pct")
        if method not in GOOD_METHODS or prox is None:
            continue
        f_sys = int(row["f_sys"]) if row["f_sys"] is not None else 0
        mode  = row.get("mode", "?")
        by_fs_mode[f_sys][mode].append(prox)

    findings = []
    for f_sys in sorted(by_fs_mode):
        for mode in sorted(by_fs_mode[f_sys]):
            vals = by_fs_mode[f_sys][mode]
            if not vals:
                continue
            mean_prox   = statistics.mean(vals)
            median_prox = statistics.median(vals)
            pct_near    = sum(1 for v in vals if v >= CEILING_PROXIMITY_WARNING) / len(vals) * 100

            flag = ""
            if pct_near >= 50:
                flag = "ALERT: >50% of elbow selections are within 15% of ceiling -- thresholds may be TOO LOOSE"
            elif pct_near >= 25:
                flag = "WARNING: elevated ceiling proximity"

            findings.append({
                "f_sys": f_sys,
                "mode": mode,
                "n_elbow_selections": len(vals),
                "mean_proximity_pct": round(mean_prox, 1),
                "median_proximity_pct": round(median_prox, 1),
                "pct_near_ceiling": round(pct_near, 1),
                "flag": flag,
            })

    return findings


# ===========================================================================
# DIAGNOSTIC 3: CONFIDENCE SCORE DISTRIBUTION
# ===========================================================================
def analyze_confidence_scores(rows: list[dict]) -> dict:
    """
    Examines the confidence score distribution for elbow selections.

    Low confidence on elbow selections means the elbow algorithm found a
    maximum in a very flat distance profile -- the "elbow" wasn't a real
    bend, it was statistical noise. This can happen when thresholds are
    too loose and many factors pass, making the RMSE curve nearly flat.
    """
    by_fs_mode = defaultdict(lambda: defaultdict(list))

    for row in rows:
        method = row.get("selection_method", "")
        score  = row.get("confidence_score")
        if method != "elbow" or score is None:
            continue
        f_sys = int(row["f_sys"]) if row["f_sys"] is not None else 0
        mode  = row.get("mode", "?")
        by_fs_mode[f_sys][mode].append(score)

    findings = []
    for f_sys in sorted(by_fs_mode):
        for mode in sorted(by_fs_mode[f_sys]):
            vals = by_fs_mode[f_sys][mode]
            if not vals:
                continue
            mean_score   = statistics.mean(vals)
            median_score = statistics.median(vals)
            pct_weak     = sum(1 for v in vals if v < WEAK_ELBOW_THRESHOLD) / len(vals) * 100

            flag = ""
            if pct_weak >= 40:
                flag = "ALERT: >40% of elbow selections have low confidence -- thresholds may be TOO LOOSE"
            elif pct_weak >= 20:
                flag = "WARNING: elevated rate of weak-confidence elbow selections"

            findings.append({
                "f_sys": f_sys,
                "mode": mode,
                "n_elbow": len(vals),
                "mean_confidence": round(mean_score, 1),
                "median_confidence": round(median_score, 1),
                "pct_weak_confidence": round(pct_weak, 1),
                "flag": flag,
            })

    return findings


# ===========================================================================
# DIAGNOSTIC 4: FACTOR STABILITY ACROSS STATES
# ===========================================================================
def analyze_factor_stability(rows: list[dict]) -> dict:
    """
    For each functional system and mode, computes the variance in selected
    factors across states.

    Some variance is expected (roads differ by state), but extremely high
    variance suggests the calibration is noise-driven rather than signal-driven,
    which is often caused by threshold settings that make the passing list
    very sensitive to small metric changes.

    Also flags states where the selected factor deviates far from the
    national default -- those may warrant manual review.
    """
    by_fs_mode = defaultdict(lambda: defaultdict(list))   # [f_sys][mode] -> list of (state, factor)

    for row in rows:
        f_sys  = int(row["f_sys"]) if row["f_sys"] is not None else 0
        mode   = row.get("mode", "?")
        factor = row.get("selected_factor")
        state  = row.get("state_fips", "??")
        if factor is None:
            continue
        by_fs_mode[f_sys][mode].append((state, int(factor)))

    findings = []
    for f_sys in sorted(by_fs_mode):
        for mode in sorted(by_fs_mode[f_sys]):
            pairs  = by_fs_mode[f_sys][mode]
            vals   = [p[1] for p in pairs]
            if len(vals) < 2:
                continue

            mean_f  = statistics.mean(vals)
            stdev_f = statistics.stdev(vals)
            cv      = (stdev_f / mean_f * 100) if mean_f else 0.0   # coefficient of variation

            # Identify outlier states (> 2 standard deviations from mean)
            outliers = [(s, f) for s, f in pairs if abs(f - mean_f) > 2 * stdev_f]

            flag = ""
            if cv >= 50:
                flag = "ALERT: very high factor variance across states (CV >= 50%) -- calibration may be noise-driven"
            elif cv >= 30:
                flag = "WARNING: elevated factor variance across states"

            findings.append({
                "f_sys": f_sys,
                "mode": mode,
                "n_states": len(vals),
                "mean_factor": round(mean_f, 0),
                "stdev_factor": round(stdev_f, 0),
                "coeff_variation_pct": round(cv, 1),
                "min_factor": min(vals),
                "max_factor": max(vals),
                "outlier_states": ", ".join(f"{s}:{f}" for s, f in outliers) if outliers else "",
                "flag": flag,
            })

    return findings


# ===========================================================================
# DIAGNOSTIC 5: THRESHOLD SENSITIVITY SUMMARY
# ===========================================================================
def analyze_threshold_sensitivity(rows: list[dict]) -> list[str]:
    """
    Produces a plain-English paragraph for each functional system summarizing
    whether the thresholds appear tight, loose, or approximately correct,
    and what direction any adjustment should go.
    """
    # Aggregate key signals by f_sys
    by_fs = defaultdict(lambda: {
        "fallback_n": 0, "total": 0,
        "proximity_vals": [], "confidence_vals": [],
        "factor_vals": [], "override_n": 0,
    })

    for row in rows:
        f_sys = int(row["f_sys"]) if row["f_sys"] is not None else 0
        d = by_fs[f_sys]
        d["total"] += 1
        method = row.get("selection_method", "")
        if method in FALLBACK_METHODS:
            d["fallback_n"] += 1
        prox = row.get("ceiling_proximity_pct")
        if prox is not None and method in GOOD_METHODS:
            d["proximity_vals"].append(prox)
        conf = row.get("confidence_score")
        if conf is not None and method == "elbow":
            d["confidence_vals"].append(conf)
        factor = row.get("selected_factor")
        if factor is not None:
            d["factor_vals"].append(factor)
        if row.get("override_recommended"):
            d["override_n"] += 1

    lines = []
    fs_names = {
        1: "Interstate", 2: "Other Freeways & Expressways",
        3: "Other Principal Arterial", 4: "Minor Arterial",
        5: "Major Collector", 6: "Minor Collector", 7: "Local"
    }

    for f_sys in sorted(by_fs):
        d     = by_fs[f_sys]
        total = d["total"]
        if total == 0:
            continue

        fallback_rate = d["fallback_n"] / total
        mean_prox     = statistics.mean(d["proximity_vals"]) if d["proximity_vals"] else None
        mean_conf     = statistics.mean(d["confidence_vals"]) if d["confidence_vals"] else None
        override_rate = d["override_n"] / total

        # Classify the threshold health for this FS
        signals_tight = 0
        signals_loose = 0

        if fallback_rate >= FALLBACK_WARNING_RATE:    signals_tight += 2
        if fallback_rate >= FALLBACK_ALERT_RATE:      signals_tight += 1
        if mean_prox is not None and mean_prox >= 80: signals_loose += 1
        if mean_conf is not None and mean_conf < WEAK_ELBOW_THRESHOLD: signals_loose += 1
        if override_rate >= 0.30:                     signals_tight += 1

        if signals_tight >= 2 and signals_loose == 0:
            verdict = "thresholds appear TOO TIGHT"
            action  = (f"High fallback rate is driven by H mode (MAX_H_RMSE_FT[{f_sys}] and/or "
                       f"MAX_H_DEV_FT[{f_sys}]). Consider loosening MAX_H_RMSE_FT[{f_sys}] "
                       f"by ~0.5-1.0 ft and re-running calibration on a representative subset of states. "
                       f"V thresholds (V_RMSE_THRESHOLDS[{f_sys}], MAX_V_DEV_FT[{f_sys}]) are secondary.")
        elif signals_loose >= 2 and signals_tight == 0:
            verdict = "thresholds appear TOO LOOSE"
            action  = (f"Consider tightening MAX_H_RMSE_FT[{f_sys}] by ~0.5 ft and/or "
                       f"V_RMSE_THRESHOLDS[{f_sys}] by ~10-20%. "
                       f"Verify that elbow selections shift to lower, more stable factors.")
        elif signals_tight >= 1 and signals_loose >= 1:
            verdict = "conflicting signals -- thresholds may need per-mode adjustment"
            action  = (f"H and V thresholds may need to be tuned independently. "
                       f"Run the diagnostic split by mode to isolate which direction each needs.")
        else:
            verdict = "thresholds appear APPROXIMATELY CORRECT"
            action  = "No immediate action required. Continue monitoring after the next national run."

        summary_parts = [
            f"  Fallback rate:       {fallback_rate*100:.1f}%  (warning >= {FALLBACK_WARNING_RATE*100:.0f}%, alert >= {FALLBACK_ALERT_RATE*100:.0f}%)",
            f"  Mean ceiling prox:  {f'{mean_prox:.1f}%' if mean_prox is not None else 'n/a'}  (concern >= 80%)",
            f"  Mean confidence:    {f'{mean_conf:.1f}' if mean_conf is not None else 'n/a'}  (weak < {WEAK_ELBOW_THRESHOLD})",
            f"  Override rate:      {override_rate*100:.1f}%",
        ]

        lines.append(
            f"FS {f_sys} ({fs_names.get(f_sys, '?')}):\n"
            f"  Assessment: {verdict}\n"
            + "\n".join(summary_parts) + "\n"
            f"  Recommendation: {action}"
        )

    return lines


# ===========================================================================
# CHART GENERATION
# ===========================================================================
def make_charts(rows: list[dict], out_dir: str) -> None:
    """Generates diagnostic PNG charts into out_dir/plots/."""
    if not HAVE_MPL:
        print("  (matplotlib not installed -- skipping charts)")
        return

    plots_dir = os.path.join(out_dir, "plots")
    os.makedirs(plots_dir, exist_ok=True)

    fs_labels = {1:"FS1\nInterstates", 2:"FS2\nFwy/Exp", 3:"FS3\nOPA",
                 4:"FS4\nMin Art", 5:"FS5\nMaj Col", 6:"FS6\nMin Col", 7:"FS7\nLocal"}
    fs_list = sorted(fs_labels.keys())

    # ---- Chart 1: Fallback rate by FS and mode ----
    fig, axes = plt.subplots(1, 2, figsize=(13, 5), sharey=True)
    fig.suptitle("Fallback Selection Rate by Functional System", fontsize=13, fontweight="bold")

    for ax, mode in zip(axes, ["H", "V"]):
        counts = defaultdict(lambda: {"fallback": 0, "total": 0})
        for row in rows:
            if row.get("mode") != mode: continue
            f_sys = int(row["f_sys"]) if row["f_sys"] is not None else 0
            counts[f_sys]["total"] += 1
            if row.get("selection_method") in FALLBACK_METHODS:
                counts[f_sys]["fallback"] += 1

        rates = [counts[fs]["fallback"] / counts[fs]["total"] * 100
                 if counts[fs]["total"] else 0 for fs in fs_list]
        colors = ["#d62728" if r >= FALLBACK_ALERT_RATE * 100
                  else "#ff7f0e" if r >= FALLBACK_WARNING_RATE * 100
                  else "#2ca02c" for r in rates]

        bars = ax.bar([fs_labels[fs] for fs in fs_list], rates, color=colors, edgecolor="white", linewidth=0.5)
        ax.axhline(FALLBACK_WARNING_RATE * 100, color="#ff7f0e", linestyle="--", linewidth=1.0, label=f"Warning ({FALLBACK_WARNING_RATE*100:.0f}%)")
        ax.axhline(FALLBACK_ALERT_RATE   * 100, color="#d62728", linestyle="--", linewidth=1.0, label=f"Alert ({FALLBACK_ALERT_RATE*100:.0f}%)")
        ax.set_title(f"{'Horizontal' if mode == 'H' else 'Vertical'} Smoothing (Mode {mode})")
        ax.set_ylabel("Fallback Rate (%)")
        ax.set_ylim(0, 100)
        ax.yaxis.set_major_formatter(ticker.PercentFormatter())
        ax.legend(fontsize=8)
        for bar, rate in zip(bars, rates):
            if rate > 0:
                ax.text(bar.get_x() + bar.get_width() / 2, bar.get_height() + 1,
                        f"{rate:.0f}%", ha="center", va="bottom", fontsize=8)

    plt.tight_layout()
    chart1_path = os.path.join(plots_dir, "fallback_rate_by_fs.png")
    plt.savefig(chart1_path, dpi=150, bbox_inches="tight")
    plt.close()
    print(f"  Saved: {chart1_path}")

    # ---- Chart 2: Confidence score distribution by FS (elbow selections only) ----
    fig, axes = plt.subplots(1, 2, figsize=(13, 5), sharey=True)
    fig.suptitle("Confidence Score Distribution (Elbow Selections Only)", fontsize=13, fontweight="bold")

    for ax, mode in zip(axes, ["H", "V"]):
        by_fs = defaultdict(list)
        for row in rows:
            if row.get("mode") != mode: continue
            if row.get("selection_method") != "elbow": continue
            f_sys = int(row["f_sys"]) if row["f_sys"] is not None else 0
            conf  = row.get("confidence_score")
            if conf is not None:
                by_fs[f_sys].append(conf)

        data   = [by_fs.get(fs, [0]) for fs in fs_list]
        bp = ax.boxplot(data, labels=[fs_labels[fs] for fs in fs_list], patch_artist=True,
                        medianprops=dict(color="black", linewidth=1.5))
        for patch in bp["boxes"]:
            patch.set_facecolor("#aec7e8")
        ax.axhline(WEAK_ELBOW_THRESHOLD, color="#d62728", linestyle="--",
                   linewidth=1.0, label=f"Weak threshold ({WEAK_ELBOW_THRESHOLD})")
        ax.set_title(f"{'Horizontal' if mode == 'H' else 'Vertical'} Smoothing (Mode {mode})")
        ax.set_ylabel("Confidence Score (0-100)")
        ax.set_ylim(0, 105)
        ax.legend(fontsize=8)

    plt.tight_layout()
    chart2_path = os.path.join(plots_dir, "confidence_scores_by_fs.png")
    plt.savefig(chart2_path, dpi=150, bbox_inches="tight")
    plt.close()
    print(f"  Saved: {chart2_path}")

    # ---- Chart 3: Selected factor distribution by FS ----
    fig, axes = plt.subplots(1, 2, figsize=(13, 5), sharey=True)
    fig.suptitle("Selected Smoothing Factor Distribution by Functional System", fontsize=13, fontweight="bold")

    for ax, mode in zip(axes, ["H", "V"]):
        by_fs = defaultdict(list)
        for row in rows:
            if row.get("mode") != mode: continue
            f_sys  = int(row["f_sys"]) if row["f_sys"] is not None else 0
            factor = row.get("selected_factor")
            if factor is not None:
                by_fs[f_sys].append(factor)

        data = [by_fs.get(fs, [0]) for fs in fs_list]
        bp = ax.boxplot(data, labels=[fs_labels[fs] for fs in fs_list], patch_artist=True,
                        medianprops=dict(color="black", linewidth=1.5))
        for patch in bp["boxes"]:
            patch.set_facecolor("#98df8a")
        ax.set_title(f"{'Horizontal' if mode == 'H' else 'Vertical'} Smoothing (Mode {mode})")
        ax.set_ylabel("Selected Factor")
        ax.set_ylim(0, 5000)

    plt.tight_layout()
    chart3_path = os.path.join(plots_dir, "selected_factors_by_fs.png")
    plt.savefig(chart3_path, dpi=150, bbox_inches="tight")
    plt.close()
    print(f"  Saved: {chart3_path}")

    # ---- Chart 4: Ceiling proximity heatmap (state x FS, V mode) ----
    # Collect mean ceiling proximity per state per FS for V mode
    state_fs_prox = defaultdict(lambda: defaultdict(list))
    for row in rows:
        if row.get("mode") != "V": continue
        if row.get("selection_method") not in GOOD_METHODS: continue
        state = row.get("state_fips", "??")
        f_sys = int(row["f_sys"]) if row["f_sys"] is not None else 0
        prox  = row.get("ceiling_proximity_pct")
        if prox is not None:
            state_fs_prox[state][f_sys].append(prox)

    states = sorted(state_fs_prox.keys())
    if states:
        import numpy as np
        matrix = np.full((len(states), len(fs_list)), np.nan)
        for i, state in enumerate(states):
            for j, fs in enumerate(fs_list):
                vals = state_fs_prox[state].get(fs)
                if vals:
                    matrix[i, j] = statistics.mean(vals)

        fig, ax = plt.subplots(figsize=(10, max(6, len(states) * 0.22)))
        im = ax.imshow(matrix, aspect="auto", cmap="RdYlGn_r", vmin=0, vmax=100)
        ax.set_xticks(range(len(fs_list)))
        ax.set_xticklabels([f"FS{fs}" for fs in fs_list])
        ax.set_yticks(range(len(states)))
        ax.set_yticklabels(states, fontsize=7)
        ax.set_title("Mean Ceiling Proximity % — Vertical Mode (Elbow Selections)\nGreen = comfortable margin, Red = near ceiling", fontsize=11)
        plt.colorbar(im, ax=ax, label="Ceiling Proximity (%)")
        plt.tight_layout()
        chart4_path = os.path.join(plots_dir, "ceiling_proximity_heatmap_V.png")
        plt.savefig(chart4_path, dpi=150, bbox_inches="tight")
        plt.close()
        print(f"  Saved: {chart4_path}")


# ===========================================================================
# CSV OUTPUT HELPERS
# ===========================================================================
def write_csv(path: str, fieldnames: list[str], rows: list[dict]) -> None:
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    print(f"  Saved: {path}")


# ===========================================================================
# MAIN
# ===========================================================================
def main():
    parser = argparse.ArgumentParser(description="RAT Calibration Threshold Diagnostics")
    parser.add_argument("--audit",  required=True, help="Path to calibration_audit.csv")
    parser.add_argument("--outdir", required=True, help="Output folder for diagnostic reports")
    args = parser.parse_args()

    os.makedirs(args.outdir, exist_ok=True)

    print("\n" + "=" * 60)
    print("  RAT CALIBRATION THRESHOLD DIAGNOSTICS")
    print("=" * 60 + "\n")

    rows = load_audit(args.audit)

    # ---- Run all diagnostics ----
    print("\n[1/5] Analyzing selection method breakdown...")
    method_findings = analyze_selection_methods(rows)

    print("[2/5] Analyzing ceiling proximity...")
    proximity_findings = analyze_ceiling_proximity(rows)

    print("[3/5] Analyzing confidence score distribution...")
    confidence_findings = analyze_confidence_scores(rows)

    print("[4/5] Analyzing factor stability across states...")
    stability_findings = analyze_factor_stability(rows)

    print("[5/5] Generating threshold sensitivity summary...")
    sensitivity_lines = analyze_threshold_sensitivity(rows)

    # ---- Write CSV outputs ----
    print("\nWriting output files...")

    write_csv(
        os.path.join(args.outdir, "selection_method_breakdown.csv"),
        ["f_sys", "mode", "total", "elbow", "flat_curve", "highest_safe",
         "composite_fallback", "absolute_fallback", "fallback_rate_pct", "flag"],
        method_findings,
    )

    write_csv(
        os.path.join(args.outdir, "threshold_proximity.csv"),
        ["f_sys", "mode", "n_elbow_selections", "mean_proximity_pct",
         "median_proximity_pct", "pct_near_ceiling", "flag"],
        proximity_findings,
    )

    write_csv(
        os.path.join(args.outdir, "confidence_scores.csv"),
        ["f_sys", "mode", "n_elbow", "mean_confidence", "median_confidence",
         "pct_weak_confidence", "flag"],
        confidence_findings,
    )

    write_csv(
        os.path.join(args.outdir, "factor_stability.csv"),
        ["f_sys", "mode", "n_states", "mean_factor", "stdev_factor",
         "coeff_variation_pct", "min_factor", "max_factor", "outlier_states", "flag"],
        stability_findings,
    )

    # ---- Write plain-text summary ----
    summary_path = os.path.join(args.outdir, "diagnostics_summary.txt")
    with open(summary_path, "w", encoding="utf-8") as f:
        f.write("RAT CALIBRATION THRESHOLD DIAGNOSTICS -- SUMMARY\n")
        f.write("=" * 60 + "\n\n")

        f.write("SECTION 1: SELECTION METHOD BREAKDOWN\n")
        f.write("-" * 40 + "\n")
        alerts = [r for r in method_findings if r["flag"]]
        if alerts:
            for r in alerts:
                f.write(f"  FS {r['f_sys']} Mode {r['mode']}: {r['flag']}  "
                        f"(fallback rate = {r['fallback_rate_pct']}%)\n")
        else:
            f.write("  No fallback rate concerns detected.\n")

        f.write("\nSECTION 2: CEILING PROXIMITY (Elbow Selections)\n")
        f.write("-" * 40 + "\n")
        alerts = [r for r in proximity_findings if r["flag"]]
        if alerts:
            for r in alerts:
                f.write(f"  FS {r['f_sys']} Mode {r['mode']}: {r['flag']}  "
                        f"(mean proximity = {r['mean_proximity_pct']}%)\n")
        else:
            f.write("  No ceiling proximity concerns detected.\n")

        f.write("\nSECTION 3: CONFIDENCE SCORES (Elbow Selections)\n")
        f.write("-" * 40 + "\n")
        alerts = [r for r in confidence_findings if r["flag"]]
        if alerts:
            for r in alerts:
                f.write(f"  FS {r['f_sys']} Mode {r['mode']}: {r['flag']}  "
                        f"(mean confidence = {r['mean_confidence']})\n")
        else:
            f.write("  No confidence score concerns detected.\n")

        f.write("\nSECTION 4: FACTOR STABILITY ACROSS STATES\n")
        f.write("-" * 40 + "\n")
        alerts = [r for r in stability_findings if r["flag"]]
        if alerts:
            for r in alerts:
                f.write(f"  FS {r['f_sys']} Mode {r['mode']}: {r['flag']}  "
                        f"(CV = {r['coeff_variation_pct']}%)\n")
                if r["outlier_states"]:
                    f.write(f"    Outlier states: {r['outlier_states']}\n")
        else:
            f.write("  No factor stability concerns detected.\n")

        f.write("\nSECTION 5: PER-FUNCTIONAL-SYSTEM THRESHOLD ASSESSMENT\n")
        f.write("-" * 40 + "\n")
        for line in sensitivity_lines:
            f.write(line + "\n\n")

        f.write("\nCURRENT THRESHOLD VALUES (for reference)\n")
        f.write("-" * 40 + "\n")
        f.write(f"  MOUNTAIN_BONUS:       +{MOUNTAIN_BONUS_FT} ft on Max_V_Dev\n")
        f.write(f"  V_MIN_RMSE_RANGE_FT:  {V_MIN_RMSE_RANGE_FT} ft  (below this, flat_terrain_default used)\n\n")
        for fs in sorted(V_RMSE_THRESHOLDS):
            h_ceil = MAX_H_RMSE_FT[fs] if isinstance(MAX_H_RMSE_FT, dict) else MAX_H_RMSE_FT
            f.write(f"  FS {fs}:  H_RMSE <= {h_ceil} ft  |  "
                    f"V_RMSE <= {V_RMSE_THRESHOLDS[fs]} ft  |  "
                    f"Max_V_Dev <= {MAX_V_DEV_FT[fs]} ft  |  "
                    f"Max_H_Dev <= {MAX_H_DEV_FT[fs]} ft\n")

    print(f"  Saved: {summary_path}")

    # ---- Generate charts ----
    print("\nGenerating charts...")
    make_charts(rows, args.outdir)

    # ---- Print summary to console ----
    print("\n" + "=" * 60)
    print("  THRESHOLD ASSESSMENT SUMMARY")
    print("=" * 60)
    for line in sensitivity_lines:
        print("\n" + line)

    print("\n" + "=" * 60)
    print(f"  All outputs written to: {args.outdir}")
    print("=" * 60 + "\n")


if __name__ == "__main__":
    main()
