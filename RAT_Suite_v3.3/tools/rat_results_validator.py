# tools/rat_results_validator.py

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
RAT RESULTS VALIDATOR v3.3 (QA/QC Module)
--------------------------------------------------------------------------------
ROLE: Automated quality assurance checker for RAT Suite outputs.
DESCRIPTION:
Scans generated CSVs and 4D outputs to ensure data integrity. Checks for
missing columns, invalid distances, missing WKT_ZM values, and flags abnormal
elevation data (like un-interpolated NaNs) before final production delivery.

Checks by file:
  horizontal / vertical  required columns, positive lengths, valid classes
  grades                 valid classes, lengths, no gaps or overlaps in a chunk
  4D enriched            WKT_ZM present and parseable, no all-zero Z or M rows,
                         CURVES_A-F and GRADES_A-F summing to the section length
  vertices               mileposts increasing within each chunk (RouteId + Part)
  section scores         an A-F class on every section
  4D vs section scores   every HPMS record the alignment scored is in the 4D
                         output (both have one row per record)

Run by the Unified GUI after an alignment run, or from the command line with
the file arguments below. Writes qa_validation_report.txt beside the files.

CREATED BY: Federal Highway Administration, Office of Highway Policy Information.
CREATED ON: 5/14/2026
"""
import os
import re
import sys
import argparse
import numpy as np
import pandas as pd

class Tee(object):
    def __init__(self, *files):
        self.files = files
    def write(self, obj):
        for f in self.files:
            f.write(obj)
            f.flush()
    def flush(self):
        for f in self.files:
            f.flush()

# Share of the alignment's HPMS records that may be missing from the 4D
# output before the check fails (records with no elevations are left out).
MAX_4D_MISSING_PCT = 5.0
MAX_LISTED_ROWS = 10           # rows listed under the CURVES/GRADES A-F sum warning

REQUIRED_H = ["RouteId", "Start_Dist", "End_Dist", "Length_m"]
REQUIRED_V = ["RouteId", "Start_Dist", "End_Dist", "Length_m"]
REQUIRED_G = ["RouteId", "Start_Dist", "End_Dist", "Calibrated_Start_MP", "Calibrated_End_MP",
              "Length_ft", "Grade_Pct", "Grade_Bin"]
# Largest gap or overlap (ft) allowed between consecutive grade segments.
GRADE_JOIN_TOL_FT = 0.1

def parse_wkt_zm_stats(series):
    zvals = []
    mvals = []
    for w in series.dropna():
        s = str(w)
        if "LINESTRING ZM" not in s.upper():
            continue
        inner = s[s.find("(")+1:s.rfind(")")]
        for pt in inner.split(","):
            parts = pt.strip().split()
            if len(parts) >= 4:
                try:
                    zvals.append(float(parts[2]))
                    mvals.append(float(parts[3]))
                except Exception:
                    pass
    return zvals, mvals

def check_zm_completeness(series):
    zero_z_rows = 0
    zero_m_rows = 0

    for w in series.dropna():
        s = str(w)
        if "LINESTRING ZM" not in s.upper():
            continue
        inner = s[s.find("(")+1:s.rfind(")")]
        pts   = [p.strip().split() for p in inner.split(",")]

        z_vals = []
        m_vals = []
        for p in pts:
            if len(p) >= 4:
                try:
                    z_vals.append(float(p[2]))
                    m_vals.append(float(p[3]))
                except Exception:
                    pass

        if z_vals and all(z == 0.0 for z in z_vals):
            zero_z_rows += 1
        if m_vals and all(m == 0.0 for m in m_vals):
            zero_m_rows += 1

    return zero_z_rows, zero_m_rows

def validate_horizontal(df: pd.DataFrame):
    issues = []
    warnings = []
    for c in REQUIRED_H:
        if c not in df.columns:
            issues.append(f"Missing required column: {c}")
    if issues:
        return issues, warnings
    
    bad_len = (pd.to_numeric(df["Length_m"], errors="coerce") <= 0).sum()
    if bad_len > 0:
        issues.append(f"Rows with Length_m <= 0: {int(bad_len)}")
    bad_dist = (
        pd.to_numeric(df["End_Dist"], errors="coerce")
        <= pd.to_numeric(df["Start_Dist"], errors="coerce")
    ).sum()
    if bad_dist > 0:
        issues.append(f"Rows where End_Dist <= Start_Dist: {int(bad_dist)}")
    
    if "Radius_m" in df.columns:
        bad_radius = (pd.to_numeric(df["Radius_m"], errors="coerce") <= 0).sum()
        if bad_radius > 0:
            warnings.append(f"Rows with Radius_m <= 0: {int(bad_radius)}")
    if "Dir" in df.columns:
        invalid_dir = (~df["Dir"].astype(str).isin(["Left", "Right"])).sum()
        if invalid_dir > 0:
            warnings.append(f"Rows with invalid Dir (not Left/Right): {int(invalid_dir)}")
    if "Bin" in df.columns:
        invalid_bin = (~df["Bin"].astype(str).isin(list("ABCDEF"))).sum()
        if invalid_bin > 0:
            warnings.append(f"Rows with invalid Bin (not A-F): {int(invalid_bin)}")
    return issues, warnings

def validate_vertical(df: pd.DataFrame):
    issues = []
    warnings = []
    for c in REQUIRED_V:
        if c not in df.columns:
            issues.append(f"Missing required column: {c}")
    if issues:
        return issues, warnings
    
    bad_len = (pd.to_numeric(df["Length_m"], errors="coerce") <= 0).sum()
    if bad_len > 0:
        issues.append(f"Rows with Length_m <= 0: {int(bad_len)}")
    bad_dist = (
        pd.to_numeric(df["End_Dist"], errors="coerce")
        <= pd.to_numeric(df["Start_Dist"], errors="coerce")
    ).sum()
    if bad_dist > 0:
        issues.append(f"Rows where End_Dist <= Start_Dist: {int(bad_dist)}")
    
    if "K_Value" in df.columns:
        bad_k = (pd.to_numeric(df["K_Value"], errors="coerce") <= 0).sum()
        if bad_k > 0:
            warnings.append(f"Rows with K_Value <= 0: {int(bad_k)}")
    if "Type" in df.columns:
        invalid_type = (~df["Type"].astype(str).isin(["CREST", "SAG"])).sum()
        if invalid_type > 0:
            warnings.append(f"Rows with invalid Type (not CREST/SAG): {int(invalid_type)}")
    if "VC_Class" in df.columns:
        invalid_bin = (~df["VC_Class"].astype(str).isin(list("ABCDEF"))).sum()
        if invalid_bin > 0:
            warnings.append(f"Rows with invalid VC_Class (not A-F): {int(invalid_bin)}")
    elif "Grade_Bin" in df.columns:
        warnings.append("Vertical CSV has Grade_Bin but no VC_Class. Grade classes belong in "
                        "the grades CSV; re-run the alignment.")
    return issues, warnings

def validate_grades(df: pd.DataFrame):
    """Grade segments: valid classes, lengths that match their distances,
    and, within each continuity chunk (RouteId + Part), reference points
    that run on with no gaps or overlaps, so the segments cover the chunk."""
    issues = []
    warnings = []
    for c in REQUIRED_G:
        if c not in df.columns:
            issues.append(f"Missing required column: {c}")
    if issues:
        return issues, warnings

    invalid_bin = (~df["Grade_Bin"].astype(str).isin(list("ABCDEF"))).sum()
    if invalid_bin > 0:
        issues.append(f"Rows with invalid Grade_Bin (not A-F): {int(invalid_bin)}")

    start = pd.to_numeric(df["Start_Dist"], errors="coerce")
    end = pd.to_numeric(df["End_Dist"], errors="coerce")
    length = pd.to_numeric(df["Length_ft"], errors="coerce")
    bad_dist = (end <= start).sum()
    if bad_dist > 0:
        issues.append(f"Rows where End_Dist <= Start_Dist: {int(bad_dist)}")
    bad_len = ((end - start) * 3.28084 - length).abs() > GRADE_JOIN_TOL_FT
    if bad_len.any():
        issues.append(f"Rows where Length_ft does not match End_Dist - Start_Dist: {int(bad_len.sum())}")

    tol_mi = GRADE_JOIN_TOL_FT / 5280.0
    gap_chunks, overlap_chunks = [], []
    group_cols = ["RouteId", "Part"] if "Part" in df.columns else ["RouteId"]
    work = df.assign(_s=pd.to_numeric(df["Calibrated_Start_MP"], errors="coerce"),
                     _e=pd.to_numeric(df["Calibrated_End_MP"], errors="coerce"))
    for key, grp in work.groupby(group_cols):
        grp = grp.sort_values("_s")
        steps = grp["_s"].to_numpy()[1:] - grp["_e"].to_numpy()[:-1]
        if (steps > tol_mi).any():
            gap_chunks.append(key)
        if (steps < -tol_mi).any():
            overlap_chunks.append(key)

    def _names(keys):
        return ", ".join(str(k) for k in keys[:5]) + ("..." if len(keys) > 5 else "")

    if gap_chunks:
        issues.append(f"Chunks with gaps between grade segments: {len(gap_chunks)} ({_names(gap_chunks)})")
    if overlap_chunks:
        issues.append(f"Chunks with overlapping grade segments: {len(overlap_chunks)} ({_names(overlap_chunks)})")
    return issues, warnings

def validate_vertices(df: pd.DataFrame):
    issues = []
    warnings = []
    req_cols = ["RouteId", "Milepost"]
    for c in req_cols:
        if c not in df.columns:
            issues.append(f"Missing required column: {c}")
    if issues:
        return issues, warnings

    has_part = "Part" in df.columns
    if not has_part:
        warnings.append(
            "Vertices file has no 'Part' column, so mileposts are checked "
            "route-wide. That over-reports on routes made of separate pieces "
            "(county-line milepost resets, a street name used for disconnected "
            "segments). Re-run the alignment to check each piece on its own."
        )

    bad_mp_routes = []
    group_cols = ["RouteId", "Part"] if has_part else ["RouteId"]
    for key, grp in df.groupby(group_cols):
        rid = key[0] if has_part else key
        mp_diff = pd.to_numeric(grp["Milepost"], errors="coerce").diff().dropna()
        if (mp_diff < 0).any():
            bad_mp_routes.append(str(rid))

    bad_mp_routes = sorted(set(bad_mp_routes))
    if bad_mp_routes:
        scope = "within a single continuous chunk" if has_part else "route-wide"
        issues.append(
            f"Milepost continuity errors (M_i+1 < M_i, {scope}) found on "
            f"{len(bad_mp_routes)} routes."
        )
        warnings.append(f"Routes with MP continuity issues: {', '.join(bad_mp_routes[:5])}" + ("..." if len(bad_mp_routes)>5 else ""))

    return issues, warnings

def validate_section_scores(df: pd.DataFrame):
    issues = []
    warnings = []
    req_cols = ["RouteId", "Start_MP", "End_MP", "H_Curve_Bin", "V_Grade_Bin"]
    for c in req_cols:
        if c not in df.columns:
            issues.append(f"Missing required column: {c}")
    if issues:
        return issues, warnings

    bad_dist = (pd.to_numeric(df["End_MP"], errors="coerce") <= pd.to_numeric(df["Start_MP"], errors="coerce")).sum()
    if bad_dist > 0:
        issues.append(f"Rows where End_MP <= Start_MP: {int(bad_dist)}")

    valid_bins = set(list("ABCDEF"))
    h_invalid = (~df["H_Curve_Bin"].astype(str).isin(valid_bins)).sum()
    v_invalid = (~df["V_Grade_Bin"].astype(str).isin(valid_bins)).sum()
    
    if h_invalid > 0:
        issues.append(f"Rows with missing or invalid H_Curve_Bin: {int(h_invalid)}")
    if v_invalid > 0:
        issues.append(f"Rows with missing or invalid V_Grade_Bin: {int(v_invalid)}")

    return issues, warnings

def compare_4d_with_scores(e: pd.DataFrame, scores: pd.DataFrame):
    """
    The 4D output and the section scores both have one row per HPMS record
    of the run. Returns (issues, warnings, lines): records the alignment
    scored that are missing from the 4D output fail the check above
    MAX_4D_MISSING_PCT; extra 4D records (different filters) are a warning.
    """
    issues, warnings, lines = [], [], []
    key = ["RouteId", "Start_MP", "End_MP"]
    if not set(key).issubset(e.columns) or not set(key).issubset(scores.columns):
        warnings.append("RouteId/Start_MP/End_MP not in both files; record counts not compared.")
        return issues, warnings, lines

    def _keys(df):
        k = df[key].copy()
        k["RouteId"] = k["RouteId"].astype(str).str.strip().str.upper()
        for c in ("Start_MP", "End_MP"):
            k[c] = pd.to_numeric(k[c], errors="coerce").round(4)
        return set(map(tuple, k.itertuples(index=False, name=None)))

    k4, ks = _keys(e), _keys(scores)
    missing = ks - k4
    extra = k4 - ks
    lines.append(f"Section score records: {len(ks):,}; 4D records: {len(k4):,}")
    if missing:
        pct = 100.0 * len(missing) / max(len(ks), 1)
        msg = f"{len(missing):,} of {len(ks):,} records ({pct:.1f}%) the alignment scored are missing from the 4D output."
        if pct > MAX_4D_MISSING_PCT:
            issues.append(msg)
        else:
            warnings.append(msg)
    else:
        lines.append("Every record the alignment scored is in the 4D output.")
    if extra:
        warnings.append(
            f"{len(extra):,} 4D records are not in the section scores "
            f"(the runs may have used different filters or input)."
        )
    return issues, warnings, lines


def print_section(title: str):
    print("\n" + "=" * 70)
    print(title)
    print("=" * 70)

def summarize_numeric(df: pd.DataFrame, col: str):
    if col not in df.columns:
        return None
    vals = pd.to_numeric(df[col], errors="coerce").dropna()
    if len(vals) == 0:
        return None
    return vals.min(), vals.mean(), vals.max()

def main():
    parser = argparse.ArgumentParser(description="Validate RAT output files.")
    parser.add_argument("--horizontal_csv", default=None, help="Path to horizontal output CSV")
    parser.add_argument("--vertical_csv", default=None, help="Path to vertical output CSV")
    parser.add_argument("--grades_csv", default=None, help="Path to grade segment output CSV")
    parser.add_argument("--enriched_csv", default=None, help="Path to 4D enriched CSV")
    parser.add_argument("--vertices_csv", default=None, help="Path to vertices output CSV")
    parser.add_argument("--scores_csv", default=None, help="Path to section scores output CSV")
    args = parser.parse_args()
    
    out_dir = ""
    if args.horizontal_csv: out_dir = os.path.dirname(args.horizontal_csv)
    elif args.vertical_csv: out_dir = os.path.dirname(args.vertical_csv)
    elif args.grades_csv: out_dir = os.path.dirname(args.grades_csv)
    elif args.enriched_csv: out_dir = os.path.dirname(args.enriched_csv)
    elif args.vertices_csv: out_dir = os.path.dirname(args.vertices_csv)
    elif args.scores_csv: out_dir = os.path.dirname(args.scores_csv)
    
    if not out_dir: out_dir = os.getcwd()

    # Named after the files checked (state, date and version), e.g.
    # alignment_horizontal_10_20261015_v1.csv -> qa_validation_report_10_20261015_v1.txt,
    # so a rerun does not overwrite an earlier report.
    report_name = "qa_validation_report.txt"
    for path in (args.horizontal_csv, args.vertical_csv, args.grades_csv,
                 args.vertices_csv, args.scores_csv, args.enriched_csv):
        m = re.search(r"_([A-Za-z0-9]+_\d{8}_v\d+)\.csv$", os.path.basename(path or ""))
        if m:
            report_name = f"qa_validation_report_{m.group(1)}.txt"
            break
    log_file_path = os.path.join(out_dir, report_name)
    log_file = open(log_file_path, "w")
    sys.stdout = Tee(sys.stdout, log_file)
    
    had_error = False
    any_checked = False
    
    # ---------------- Horizontal ----------------
    if args.horizontal_csv:
        any_checked = True
        print_section("Horizontal Validation")
        if not os.path.exists(args.horizontal_csv):
            print(f"ERROR: File not found: {args.horizontal_csv}")
            had_error = True
        else:
            h = pd.read_csv(args.horizontal_csv, low_memory=False)
            print(f"File: {args.horizontal_csv}")
            print(f"Rows: {len(h):,}")
            issues, warnings = validate_horizontal(h)
            if issues:
                print("\nFAIL:")
                for i in issues: print(f"- {i}")
                had_error = True
            else:
                print("\nPASS: Required integrity checks passed.")
            if warnings:
                print("\nWARNINGS:")
                for w in warnings: print(f"- {w}")
            r = summarize_numeric(h, "Radius_m")
            if r: print(f"\nRadius_m min/mean/max: {r[0]:.2f} / {r[1]:.2f} / {r[2]:.2f}")
            l = summarize_numeric(h, "Length_m")
            if l: print(f"Length_m min/mean/max: {l[0]:.2f} / {l[1]:.2f} / {l[2]:.2f}")
            
    # ---------------- Vertical ----------------
    if args.vertical_csv:
        any_checked = True
        print_section("Vertical Validation")
        if not os.path.exists(args.vertical_csv):
            print(f"ERROR: File not found: {args.vertical_csv}")
            had_error = True
        else:
            v = pd.read_csv(args.vertical_csv, low_memory=False)
            print(f"File: {args.vertical_csv}")
            print(f"Rows: {len(v):,}")
            issues, warnings = validate_vertical(v)
            if issues:
                print("\nFAIL:")
                for i in issues: print(f"- {i}")
                had_error = True
            else:
                print("\nPASS: Required integrity checks passed.")
            if warnings:
                print("\nWARNINGS:")
                for w in warnings: print(f"- {w}")
            k = summarize_numeric(v, "K_Value")
            if k: print(f"\nK_Value min/mean/max: {k[0]:.2f} / {k[1]:.2f} / {k[2]:.2f}")
            l = summarize_numeric(v, "Length_m")
            if l: print(f"Length_m min/mean/max: {l[0]:.2f} / {l[1]:.2f} / {l[2]:.2f}")
            
    # ---------------- Grades ----------------
    if args.grades_csv:
        any_checked = True
        print_section("Grade Segment Validation")
        if not os.path.exists(args.grades_csv):
            print(f"ERROR: File not found: {args.grades_csv}")
            had_error = True
        else:
            g = pd.read_csv(args.grades_csv, low_memory=False)
            print(f"File: {args.grades_csv}")
            print(f"Rows: {len(g):,}")
            issues, warnings = validate_grades(g)
            if issues:
                print("\nFAIL:")
                for i in issues: print(f"- {i}")
                had_error = True
            else:
                print("\nPASS: Required integrity checks passed.")
            if warnings:
                print("\nWARNINGS:")
                for w in warnings: print(f"- {w}")
            if "Grade_Bin" in g.columns and "Length_ft" in g.columns:
                miles = (g.groupby("Grade_Bin")["Length_ft"].sum() / 5280.0).reindex(list("ABCDEF"), fill_value=0.0)
                print("\nMiles by grade class: " + ", ".join(f"{k} {v:,.1f}" for k, v in miles.items()))

    # ---------------- 4D Enriched ----------------
    if args.enriched_csv:
        any_checked = True
        print_section("4D Enriched Validation")
        if not os.path.exists(args.enriched_csv):
            print(f"ERROR: File not found: {args.enriched_csv}")
            had_error = True
        else:
            e = pd.read_csv(args.enriched_csv, low_memory=False)
            print(f"File: {args.enriched_csv}")
            print(f"Rows: {len(e):,}")
            if "WKT_ZM" not in e.columns:
                print("FAIL: Missing WKT_ZM column.")
                had_error = True
            else:
                missing = e["WKT_ZM"].isna().sum()
                print(f"Missing WKT_ZM rows: {missing:,}")
                zvals, mvals = parse_wkt_zm_stats(e["WKT_ZM"])
                if not zvals:
                    print("FAIL: Could not parse Z/M from WKT_ZM values.")
                    had_error = True
                else:
                    z = np.array(zvals, dtype=float)
                    m = np.array(mvals, dtype=float)
                    print(f"Z min/mean/max: {np.nanmin(z):.2f} / {np.nanmean(z):.2f} / {np.nanmax(z):.2f}")
                    print(f"M min/max: {np.nanmin(m):.4f} / {np.nanmax(m):.4f}")
                    nan_z = np.isnan(z).sum()
                    if nan_z > 0:
                        print(f"WARNING: NaN Z values: {int(nan_z)}")

                    zero_z, zero_m = check_zm_completeness(e["WKT_ZM"])
                    if zero_z > 0:
                        print(f"WARNING: {zero_z:,} rows have all-zero Z values.")
                    if zero_m > 0:
                        print(f"WARNING: {zero_m:,} rows have all-zero M values.")
            lower_cols = {c.lower(): c for c in e.columns}
            for prefix in ("CURVES", "GRADES"):
                cols = [lower_cols.get(f"{prefix}_{b}".lower()) for b in "ABCDEF"]
                if None in cols or not {"Start_MP", "End_MP"}.issubset(e.columns):
                    continue
                # Values RAT wrote (curve and grade replacement) are filled on
                # every record; HPMS-reported values are blank off the sample
                # panels and repeat panel totals on them, so they are not checked.
                if e[cols].isna().any().any():
                    print(f"{prefix}_A-F are the HPMS-reported values; sum check skipped.")
                    continue
                sec_len = pd.to_numeric(e["End_MP"], errors="coerce") - pd.to_numeric(e["Start_MP"], errors="coerce")
                total = e[cols].apply(pd.to_numeric, errors="coerce").sum(axis=1)
                off = ((total - sec_len).abs() > 1e-4) & (sec_len > 0)
                if off.any():
                    print(f"WARNING: {int(off.sum()):,} rows where {prefix}_A-F do not sum to the section length.")
                    rows = e.loc[off].assign(_sum=total[off], _len=sec_len[off])
                    rid = "RouteId" if "RouteId" in rows.columns else None
                    for _, r in rows.head(MAX_LISTED_ROWS).iterrows():
                        print(f"   {r[rid] if rid else '':<20} MP {float(r['Start_MP']):.3f}-{float(r['End_MP']):.3f}: "
                              f"sum {r['_sum']:.4f}, section length {r['_len']:.4f}")
                    if len(rows) > MAX_LISTED_ROWS:
                        print(f"   ... and {len(rows) - MAX_LISTED_ROWS:,} more")
                else:
                    print(f"{prefix}_A-F sum to the section length on every row.")

    # ---------------- Vertices ----------------
    if args.vertices_csv:
        any_checked = True
        print_section("Vertices Validation")
        if not os.path.exists(args.vertices_csv):
            print(f"ERROR: File not found: {args.vertices_csv}")
            had_error = True
        else:
            vtx = pd.read_csv(args.vertices_csv, low_memory=False)
            print(f"File: {args.vertices_csv}")
            print(f"Rows: {len(vtx):,}")
            issues, warnings = validate_vertices(vtx)
            if issues:
                print("\nFAIL:")
                for i in issues: print(f"- {i}")
                had_error = True
            else:
                print("\nPASS: Required integrity checks passed.")
            if warnings:
                print("\nWARNINGS:")
                for w in warnings: print(f"- {w}")

    # ---------------- Section Scores ----------------
    if args.scores_csv:
        any_checked = True
        print_section("Section Scores Validation")
        if not os.path.exists(args.scores_csv):
            print(f"ERROR: File not found: {args.scores_csv}")
            had_error = True
        else:
            scores = pd.read_csv(args.scores_csv, low_memory=False)
            print(f"File: {args.scores_csv}")
            print(f"Rows: {len(scores):,}")
            issues, warnings = validate_section_scores(scores)
            if issues:
                print("\nFAIL:")
                for i in issues: print(f"- {i}")
                had_error = True
            else:
                print("\nPASS: Required integrity checks passed.")
            if warnings:
                print("\nWARNINGS:")
                for w in warnings: print(f"- {w}")

    # ---------------- 4D vs Section Scores ----------------
    if args.enriched_csv and args.scores_csv and os.path.exists(args.enriched_csv) and os.path.exists(args.scores_csv):
        print_section("4D Record Count")
        issues, warnings, lines = compare_4d_with_scores(e, scores)
        for line in lines:
            print(line)
        if issues:
            print("\nFAIL:")
            for i in issues: print(f"- {i}")
            had_error = True
        else:
            print("\nPASS: Record count check passed.")
        if warnings:
            print("\nWARNINGS:")
            for w in warnings: print(f"- {w}")

    if not any_checked:
        print("No files supplied. Use --horizontal_csv, --vertical_csv, --grades_csv, --enriched_csv, --vertices_csv, and/or --scores_csv")
        raise SystemExit(1)
        
    print_section("Validation Summary")
    print(f"Report saved: {log_file_path}")
    if had_error:
        print("RESULT: FAILED (one or more required checks failed).")
        raise SystemExit(1)
    else:
        print("RESULT: PASS (required checks passed).")
        raise SystemExit(0)

if __name__ == "__main__":
    main()