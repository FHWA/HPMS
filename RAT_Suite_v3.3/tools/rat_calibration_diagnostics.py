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

"""
RAT CALIBRATION DIAGNOSTICS
--------------------------------------------------------------------------------
ROLE: Reads the audit CSV from a national calibration run
(tools/rat_national_calibration.py) and reports how healthy the results are.

The calibration repeats each state/FS N_REPEATS_PER_STATE times and keeps the
median; a result is left blank when too few repeats converge or when they
disagree by more than MAX_SPREAD_RATIO. These diagnostics therefore look at
whether the repeats agree, not at how close a run sits to a threshold. They
answer three questions:

  1. How often does calibration fail to reach a stable answer at all?
     (rows where stabilized == False, i.e. the final value is BLANK)

  2. Among calibrations that DID stabilize, how close did they come to the
     inconsistency threshold (MAX_SPREAD_RATIO) that would have made them
     fail? (borderline passes deserve a second look)

  3. How much does the selected factor vary across states for a given
     functional system? (extremely high variance suggests noise, not signal)

OUTPUTS (written to OUTDIR):
  diagnostics_summary.txt        -- plain-text summary of all findings
  stabilization_breakdown.csv    -- per-FS/mode counts of stabilized vs. blank
  spread_proximity.csv           -- how close stabilized results were to the
                                     spread-ratio threshold
  factor_stability.csv           -- factor variance across states per FS
  plots/                         -- PNG charts (requires matplotlib)

Usage. Edit the CONFIGURATION block, then from the RAT Suite root:
    python tools/rat_calibration_diagnostics.py

CREATED BY: Federal Highway Administration, Office of Highway Policy Information.
CREATED ON: 10/14/2026
"""

import os
import sys
import csv
import statistics
from collections import defaultdict

# matplotlib is optional -- charts are skipped if it is not installed
try:
    import matplotlib
    matplotlib.use("Agg")          # non-interactive backend, safe for scripts
    import matplotlib.pyplot as plt
    HAVE_MPL = True
except ImportError:
    HAVE_MPL = False

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
RAT_SUITE_DIR = os.path.dirname(THIS_DIR)
if RAT_SUITE_DIR not in sys.path:
    sys.path.insert(0, RAT_SUITE_DIR)

# The calibration's own settings, so the two scripts cannot disagree.
from tools.rat_national_calibration import (
    MAX_SPREAD_RATIO,          # (max-min)/median across repeats above which a result is blank
    N_REPEATS_PER_STATE,       # independent random samples per state/FS
    MIN_CONVERGED_FRACTION,    # share of repeats that must converge
)

# ===========================================================================
# CONFIGURATION
# ===========================================================================
AUDIT_CSV = "output/national_cv/calibration_audit_CV_national.csv"
OUTDIR = "output/diagnostics"
# ===========================================================================

# ---------------------------------------------------------------------------
# Diagnostic thresholds. Starting values; revisit once several national
# runs' results are available.
# ---------------------------------------------------------------------------
# Rate of BLANK (unstabilized) final results above which we flag a problem
BLANK_WARNING_RATE = 0.20   # 20%
BLANK_ALERT_RATE   = 0.35   # 35%

# How close a passing (stabilized) result's spread came to MAX_SPREAD_RATIO,
# expressed as a percentage of that ratio -- flags borderline passes
SPREAD_PROXIMITY_WARNING = 70.0   # % of MAX_SPREAD_RATIO

# Coefficient of variation of selected_factor across states, per FS/mode
FACTOR_CV_WARNING = 30.0   # %
FACTOR_CV_ALERT   = 50.0   # %


# ===========================================================================
# DATA LOADING
# ===========================================================================
def load_audit(path: str) -> list[dict]:
    """Reads calibration_audit_CV_national.csv and coerces numeric columns."""
    if not os.path.exists(path):
        sys.exit(f"ERROR: Audit file not found: {path}")

    numeric_cols = {"f_sys", "n_chunks_sampled", "selected_factor", "diagnostic_value"}
    bool_cols = {"stabilized"}

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
            # "repeat" is intentionally left as a string: either an integer
            # 1..N_REPEATS_PER_STATE for individual runs, or "MEDIAN" for
            # the aggregated row that actually feeds the output JSON.
            rows.append(row)

    print(f"Loaded {len(rows)} audit rows from {path}")
    return rows


def _median_rows(rows: list[dict]) -> list[dict]:
    """Filters to the MEDIAN rows -- the final per-state/FS/mode decision."""
    return [r for r in rows if r.get("repeat") == "MEDIAN"]


# ===========================================================================
# DIAGNOSTIC 1: STABILIZATION RATE BREAKDOWN
# ===========================================================================
def analyze_stabilization_rates(rows: list[dict]) -> list[dict]:
    """
    Among the final (MEDIAN) results per state/FS/mode, counts how often
    calibration failed to reach a stable, trustworthy answer (stabilized
    == False, i.e. BLANK in the output JSON).

    A high blank rate is the primary signal that a functional system/mode
    is hard for this method to calibrate reliably -- either the underlying
    signal is genuinely weak for that road type, or N_REPEATS_PER_STATE /
    MAX_SPREAD_RATIO need revisiting for it.
    """
    median_rows = _median_rows(rows)
    totals  = defaultdict(lambda: defaultdict(int))
    blanks  = defaultdict(lambda: defaultdict(int))

    for row in median_rows:
        f_sys = int(row["f_sys"]) if row["f_sys"] is not None else 0
        mode  = row.get("mode", "?")
        totals[f_sys][mode] += 1
        if not row.get("stabilized", False):
            blanks[f_sys][mode] += 1

    findings = []
    for f_sys in sorted(totals):
        for mode in sorted(totals[f_sys]):
            total = totals[f_sys][mode]
            blank = blanks[f_sys].get(mode, 0)
            blank_rate = blank / total if total else 0.0

            flag = ""
            if blank_rate >= BLANK_ALERT_RATE:
                flag = "ALERT: high rate of unstabilized (BLANK) results"
            elif blank_rate >= BLANK_WARNING_RATE:
                flag = "WARNING: elevated rate of unstabilized results"

            findings.append({
                "f_sys": f_sys,
                "mode": mode,
                "total_states": total,
                "stabilized": total - blank,
                "blank": blank,
                "blank_rate_pct": round(blank_rate * 100, 1),
                "flag": flag,
            })

    return findings


# ===========================================================================
# DIAGNOSTIC 2: SPREAD PROXIMITY (borderline passes)
# ===========================================================================
def analyze_spread_proximity(rows: list[dict]) -> list[dict]:
    """
    For MEDIAN rows that DID stabilize, checks how close the repeat spread
    came to MAX_SPREAD_RATIO -- the threshold that would have made them
    fail. A result that passed at 95% of the threshold is much less
    trustworthy than one that passed at 10% of it, even though both are
    labeled "stabilized".

    """
    median_rows = _median_rows(rows)
    by_fs_mode = defaultdict(lambda: defaultdict(list))

    for row in median_rows:
        if not row.get("stabilized", False):
            continue
        spread = row.get("diagnostic_value")
        if spread is None:
            continue
        f_sys = int(row["f_sys"]) if row["f_sys"] is not None else 0
        mode  = row.get("mode", "?")
        proximity_pct = (spread / MAX_SPREAD_RATIO) * 100
        by_fs_mode[f_sys][mode].append(proximity_pct)

    findings = []
    for f_sys in sorted(by_fs_mode):
        for mode in sorted(by_fs_mode[f_sys]):
            vals = by_fs_mode[f_sys][mode]
            if not vals:
                continue
            mean_prox   = statistics.mean(vals)
            median_prox = statistics.median(vals)
            pct_near    = sum(1 for v in vals if v >= SPREAD_PROXIMITY_WARNING) / len(vals) * 100

            flag = ""
            if pct_near >= 50:
                flag = "ALERT: >50% of stabilized results are borderline (close to the inconsistency threshold)"
            elif pct_near >= 25:
                flag = "WARNING: elevated share of borderline-stabilized results"

            findings.append({
                "f_sys": f_sys,
                "mode": mode,
                "n_stabilized": len(vals),
                "mean_proximity_pct": round(mean_prox, 1),
                "median_proximity_pct": round(median_prox, 1),
                "pct_borderline": round(pct_near, 1),
                "flag": flag,
            })

    return findings


# ===========================================================================
# DIAGNOSTIC 3: FACTOR STABILITY ACROSS STATES
# ===========================================================================
def analyze_factor_stability(rows: list[dict]) -> list[dict]:
    """
    For each functional system and mode, computes the variance in the
    final (MEDIAN, stabilized) selected factor across states.

    Some variance is expected (roads differ by state), but extremely high
    variance suggests the calibration is noise-driven rather than
    signal-driven for that FS/mode.
    """
    median_rows = _median_rows(rows)
    by_fs_mode = defaultdict(lambda: defaultdict(list))   # [f_sys][mode] -> [(state, factor)]

    for row in median_rows:
        if not row.get("stabilized", False):
            continue
        factor = row.get("selected_factor")
        if factor is None:
            continue
        f_sys = int(row["f_sys"]) if row["f_sys"] is not None else 0
        mode  = row.get("mode", "?")
        state = row.get("state_fips", "??")
        by_fs_mode[f_sys][mode].append((state, factor))

    findings = []
    for f_sys in sorted(by_fs_mode):
        for mode in sorted(by_fs_mode[f_sys]):
            pairs = by_fs_mode[f_sys][mode]
            vals  = [p[1] for p in pairs]
            if len(vals) < 2:
                continue

            mean_f  = statistics.mean(vals)
            stdev_f = statistics.stdev(vals)
            cv      = (stdev_f / mean_f * 100) if mean_f else 0.0

            outliers = [(s, f) for s, f in pairs if abs(f - mean_f) > 2 * stdev_f]

            flag = ""
            if cv >= FACTOR_CV_ALERT:
                flag = "ALERT: very high factor variance across states -- calibration may be noise-driven"
            elif cv >= FACTOR_CV_WARNING:
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
                "outlier_states": ", ".join(f"{s}:{f:.0f}" for s, f in outliers) if outliers else "",
                "flag": flag,
            })

    return findings


# ===========================================================================
# DIAGNOSTIC 4: CALIBRATION HEALTH SUMMARY
# ===========================================================================
def analyze_calibration_health(stab_findings, spread_findings, factor_findings) -> list[str]:
    """
    Produces a plain-English paragraph for each functional system/mode
    combining the three diagnostics above into an overall health verdict.
    """
    fs_names = {
        1: "Interstate", 2: "Other Freeways & Expressways",
        3: "Other Principal Arterial", 4: "Minor Arterial",
        5: "Major Collector", 6: "Minor Collector", 7: "Local"
    }

    stab_by_key   = {(r["f_sys"], r["mode"]): r for r in stab_findings}
    spread_by_key = {(r["f_sys"], r["mode"]): r for r in spread_findings}
    factor_by_key = {(r["f_sys"], r["mode"]): r for r in factor_findings}

    keys = sorted(set(stab_by_key) | set(spread_by_key) | set(factor_by_key))
    lines = []

    for f_sys, mode in keys:
        stab   = stab_by_key.get((f_sys, mode))
        spread = spread_by_key.get((f_sys, mode))
        factor = factor_by_key.get((f_sys, mode))

        concerns = []
        if stab and stab["blank_rate_pct"] >= BLANK_WARNING_RATE * 100:
            concerns.append("frequent BLANK results")
        if spread and spread["pct_borderline"] >= 25:
            concerns.append("many borderline-stabilized results")
        if factor and factor["coeff_variation_pct"] >= FACTOR_CV_WARNING:
            concerns.append("high cross-state factor variance")

        if not concerns:
            verdict = "appears RELIABLE"
            action  = "No immediate action required."
        elif len(concerns) == 1:
            verdict = "shows ONE area of concern"
            action  = f"Investigate: {concerns[0]}. Consider increasing N_REPEATS_PER_STATE for this FS/mode."
        else:
            verdict = "shows MULTIPLE areas of concern"
            action  = ("Consider increasing N_REPEATS_PER_STATE and/or reviewing sample size for this "
                        f"FS/mode. Concerns: {', '.join(concerns)}.")

        summary_parts = []
        if stab:
            summary_parts.append(f"  Blank rate:         {stab['blank_rate_pct']}%  ({stab['blank']}/{stab['total_states']} states)")
        if spread:
            summary_parts.append(f"  Borderline rate:    {spread['pct_borderline']}%  (mean proximity to threshold: {spread['mean_proximity_pct']}%)")
        if factor:
            summary_parts.append(f"  Factor variance:    CV={factor['coeff_variation_pct']}%  (range {factor['min_factor']:.0f}-{factor['max_factor']:.0f})")

        lines.append(
            f"FS {f_sys} ({fs_names.get(f_sys, '?')}) - Mode {mode}:\n"
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

    import numpy as np

    plots_dir = os.path.join(out_dir, "plots")
    os.makedirs(plots_dir, exist_ok=True)

    fs_labels = {1: "FS1\nInterstates", 2: "FS2\nFwy/Exp", 3: "FS3\nOPA",
                 4: "FS4\nMin Art", 5: "FS5\nMaj Col", 6: "FS6\nMin Col", 7: "FS7\nLocal"}
    fs_list = sorted(fs_labels.keys())
    median_rows = _median_rows(rows)
    modes = [m for m in ("H", "V") if any(r.get("mode") == m for r in median_rows)] or ["H"]

    # ---- Chart 1: Blank (unstabilized) rate by FS and mode ----
    fig, axes = plt.subplots(1, len(modes), figsize=(6.5 * len(modes), 5), sharey=True, squeeze=False)
    axes = axes[0]
    fig.suptitle("Unstabilized (BLANK) Rate by Functional System", fontsize=13, fontweight="bold")

    for ax, mode in zip(axes, modes):
        counts = defaultdict(lambda: {"blank": 0, "total": 0})
        for row in median_rows:
            if row.get("mode") != mode:
                continue
            f_sys = int(row["f_sys"]) if row["f_sys"] is not None else 0
            counts[f_sys]["total"] += 1
            if not row.get("stabilized", False):
                counts[f_sys]["blank"] += 1

        rates = [counts[fs]["blank"] / counts[fs]["total"] * 100
                 if counts[fs]["total"] else 0 for fs in fs_list]
        colors = ["#d62728" if r >= BLANK_ALERT_RATE * 100
                  else "#ff7f0e" if r >= BLANK_WARNING_RATE * 100
                  else "#2ca02c" for r in rates]

        bars = ax.bar([fs_labels[fs] for fs in fs_list], rates, color=colors, edgecolor="white", linewidth=0.5)
        ax.axhline(BLANK_WARNING_RATE * 100, color="#ff7f0e", linestyle="--", linewidth=1.0,
                   label=f"Warning ({BLANK_WARNING_RATE*100:.0f}%)")
        ax.axhline(BLANK_ALERT_RATE * 100, color="#d62728", linestyle="--", linewidth=1.0,
                   label=f"Alert ({BLANK_ALERT_RATE*100:.0f}%)")
        ax.set_title(f"{'Horizontal' if mode == 'H' else 'Vertical'} Smoothing (Mode {mode})")
        ax.set_ylabel("Blank Rate (%)")
        ax.set_ylim(0, 100)
        ax.legend(fontsize=8)
        for bar, rate in zip(bars, rates):
            if rate > 0:
                ax.text(bar.get_x() + bar.get_width() / 2, bar.get_height() + 1,
                        f"{rate:.0f}%", ha="center", va="bottom", fontsize=8)

    plt.tight_layout()
    chart1_path = os.path.join(plots_dir, "blank_rate_by_fs.png")
    plt.savefig(chart1_path, dpi=150, bbox_inches="tight")
    plt.close()
    print(f"  Saved: {chart1_path}")

    # ---- Chart 2: Spread distribution by FS (stabilized MEDIAN rows only) ----
    fig, axes = plt.subplots(1, len(modes), figsize=(6.5 * len(modes), 5), sharey=True, squeeze=False)
    axes = axes[0]
    fig.suptitle("Repeat Spread Distribution (Stabilized Results Only)", fontsize=13, fontweight="bold")

    for ax, mode in zip(axes, modes):
        by_fs = defaultdict(list)
        for row in median_rows:
            if row.get("mode") != mode or not row.get("stabilized", False):
                continue
            spread = row.get("diagnostic_value")
            if spread is None:
                continue
            f_sys = int(row["f_sys"]) if row["f_sys"] is not None else 0
            by_fs[f_sys].append(spread)

        data = [by_fs.get(fs, [0]) for fs in fs_list]
        bp = ax.boxplot(data, patch_artist=True, medianprops=dict(color="black", linewidth=1.5))
        ax.set_xticklabels([fs_labels[fs] for fs in fs_list])
        for patch in bp["boxes"]:
            patch.set_facecolor("#aec7e8")
        ax.axhline(MAX_SPREAD_RATIO, color="#d62728", linestyle="--", linewidth=1.0,
                   label=f"Inconsistency threshold ({MAX_SPREAD_RATIO})")
        ax.set_title(f"{'Horizontal' if mode == 'H' else 'Vertical'} Smoothing (Mode {mode})")
        ax.set_ylabel("Spread Across Repeats")
        ax.legend(fontsize=8)

    plt.tight_layout()
    chart2_path = os.path.join(plots_dir, "spread_distribution_by_fs.png")
    plt.savefig(chart2_path, dpi=150, bbox_inches="tight")
    plt.close()
    print(f"  Saved: {chart2_path}")

    # ---- Chart 3: Selected factor distribution by FS (stabilized MEDIAN rows) ----
    fig, axes = plt.subplots(1, len(modes), figsize=(6.5 * len(modes), 5), sharey=True, squeeze=False)
    axes = axes[0]
    fig.suptitle("Selected Smoothing Factor Distribution by Functional System", fontsize=13, fontweight="bold")

    for ax, mode in zip(axes, modes):
        by_fs = defaultdict(list)
        for row in median_rows:
            if row.get("mode") != mode or not row.get("stabilized", False):
                continue
            factor = row.get("selected_factor")
            if factor is not None:
                by_fs[f_sys := int(row["f_sys"])].append(factor)

        data = [by_fs.get(fs, [0]) for fs in fs_list]
        bp = ax.boxplot(data, patch_artist=True, medianprops=dict(color="black", linewidth=1.5))
        ax.set_xticklabels([fs_labels[fs] for fs in fs_list])
        for patch in bp["boxes"]:
            patch.set_facecolor("#98df8a")
        ax.set_title(f"{'Horizontal' if mode == 'H' else 'Vertical'} Smoothing (Mode {mode})")
        ax.set_ylabel("Selected Factor")

    plt.tight_layout()
    chart3_path = os.path.join(plots_dir, "selected_factors_by_fs.png")
    plt.savefig(chart3_path, dpi=150, bbox_inches="tight")
    plt.close()
    print(f"  Saved: {chart3_path}")

    # ---- Chart 4: Spread-proximity heatmaps (state x FS), H and V ----
    for mode in modes:
        state_fs_prox = defaultdict(lambda: defaultdict(list))
        for row in median_rows:
            if row.get("mode") != mode or not row.get("stabilized", False):
                continue
            spread = row.get("diagnostic_value")
            if spread is None:
                continue
            state = row.get("state_fips", "??")
            f_sys = int(row["f_sys"]) if row["f_sys"] is not None else 0
            state_fs_prox[state][f_sys].append((spread / MAX_SPREAD_RATIO) * 100)

        states = sorted(state_fs_prox.keys())
        if not states:
            continue

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
        mode_label = "Horizontal" if mode == "H" else "Vertical"
        ax.set_title(f"Mean Spread Proximity to Threshold % -- {mode_label} Mode (Stabilized Results)\n"
                     f"Green = comfortable margin, Red = near inconsistency threshold", fontsize=11)
        plt.colorbar(im, ax=ax, label="Spread Proximity (%)")
        plt.tight_layout()
        chart4_path = os.path.join(plots_dir, f"spread_proximity_heatmap_{mode}.png")
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
    out_dir = OUTDIR
    os.makedirs(out_dir, exist_ok=True)

    print("\n" + "=" * 60)
    print("  RAT CALIBRATION DIAGNOSTICS")
    print("=" * 60 + "\n")

    rows = load_audit(AUDIT_CSV)

    # ---- Run all diagnostics ----
    print("\n[1/4] Analyzing stabilization rates...")
    stab_findings = analyze_stabilization_rates(rows)

    print("[2/4] Analyzing spread proximity (borderline passes)...")
    spread_findings = analyze_spread_proximity(rows)

    print("[3/4] Analyzing factor stability across states...")
    factor_findings = analyze_factor_stability(rows)

    print("[4/4] Generating calibration health summary...")
    health_lines = analyze_calibration_health(stab_findings, spread_findings, factor_findings)

    # ---- Write CSV outputs ----
    print("\nWriting output files...")

    write_csv(
        os.path.join(out_dir, "stabilization_breakdown.csv"),
        ["f_sys", "mode", "total_states", "stabilized", "blank", "blank_rate_pct", "flag"],
        stab_findings,
    )

    write_csv(
        os.path.join(out_dir, "spread_proximity.csv"),
        ["f_sys", "mode", "n_stabilized", "mean_proximity_pct", "median_proximity_pct",
         "pct_borderline", "flag"],
        spread_findings,
    )

    write_csv(
        os.path.join(out_dir, "factor_stability.csv"),
        ["f_sys", "mode", "n_states", "mean_factor", "stdev_factor",
         "coeff_variation_pct", "min_factor", "max_factor", "outlier_states", "flag"],
        factor_findings,
    )

    # ---- Write plain-text summary ----
    summary_path = os.path.join(out_dir, "diagnostics_summary.txt")
    with open(summary_path, "w", encoding="utf-8") as f:
        f.write("RAT CALIBRATION DIAGNOSTICS -- SUMMARY\n")
        f.write("=" * 60 + "\n\n")

        f.write("SECTION 1: STABILIZATION RATE BREAKDOWN\n")
        f.write("-" * 40 + "\n")
        alerts = [r for r in stab_findings if r["flag"]]
        if alerts:
            for r in alerts:
                f.write(f"  FS {r['f_sys']} Mode {r['mode']}: {r['flag']}  "
                        f"(blank rate = {r['blank_rate_pct']}%)\n")
        else:
            f.write("  No stabilization concerns detected.\n")

        f.write("\nSECTION 2: SPREAD PROXIMITY (Borderline Passes)\n")
        f.write("-" * 40 + "\n")
        alerts = [r for r in spread_findings if r["flag"]]
        if alerts:
            for r in alerts:
                f.write(f"  FS {r['f_sys']} Mode {r['mode']}: {r['flag']}  "
                        f"(mean proximity = {r['mean_proximity_pct']}%)\n")
        else:
            f.write("  No spread proximity concerns detected.\n")

        f.write("\nSECTION 3: FACTOR STABILITY ACROSS STATES\n")
        f.write("-" * 40 + "\n")
        alerts = [r for r in factor_findings if r["flag"]]
        if alerts:
            for r in alerts:
                f.write(f"  FS {r['f_sys']} Mode {r['mode']}: {r['flag']}  "
                        f"(CV = {r['coeff_variation_pct']}%)\n")
                if r["outlier_states"]:
                    f.write(f"    Outlier states: {r['outlier_states']}\n")
        else:
            f.write("  No factor stability concerns detected.\n")

        f.write("\nSECTION 4: PER-FS/MODE CALIBRATION HEALTH\n")
        f.write("-" * 40 + "\n")
        for line in health_lines:
            f.write(line + "\n\n")

        f.write("\nCURRENT METHOD PARAMETERS (for reference)\n")
        f.write("-" * 40 + "\n")
        f.write(f"  N_REPEATS_PER_STATE:     {N_REPEATS_PER_STATE}\n")
        f.write(f"  MAX_SPREAD_RATIO:        {MAX_SPREAD_RATIO}  (spread above this = inconsistent/BLANK)\n")
        f.write(f"  MIN_CONVERGED_FRACTION:  {MIN_CONVERGED_FRACTION}\n")

    print(f"  Saved: {summary_path}")

    # ---- Generate charts ----
    print("\nGenerating charts...")
    make_charts(rows, out_dir)

    # ---- Print summary to console ----
    print("\n" + "=" * 60)
    print("  CALIBRATION HEALTH SUMMARY")
    print("=" * 60)
    for line in health_lines:
        print("\n" + line)

    print("\n" + "=" * 60)
    print(f"  All outputs written to: {out_dir}")
    print("=" * 60 + "\n")


if __name__ == "__main__":
    main()