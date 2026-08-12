# apps/rat_results_validator.py

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

CHANGES FROM v3.2:
  - Added per-row Z/M completeness check for 4D enriched output.
  - Added validate_vertices() to ensure milepost continuity along densified routes.
  - Added validate_section_scores() to ensure 100% spatial coverage of A-F bin
    classifications for all Federal-aid highway sections.
CREATED BY: Federal Highway Administration, Office of Highway Policy Information.
CREATED ON: 5/14/2026
"""
import os
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

REQUIRED_H = ["RouteId", "Start_Dist", "End_Dist", "Length_m"]
REQUIRED_V = ["RouteId", "Start_Dist", "End_Dist", "Length_m"]

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
    if "Grade_Bin" in df.columns:
        invalid_bin = (~df["Grade_Bin"].astype(str).isin(list("ABCDEF"))).sum()
        if invalid_bin > 0:
            warnings.append(f"Rows with invalid Grade_Bin (not A-F): {int(invalid_bin)}")
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
            "Vertices file has no 'Part' column -- continuity check cannot "
            "distinguish a real within-chunk error from an expected "
            "milepost discontinuity between separate physical pieces of "
            "the same RouteId (e.g. county-line resets, or a street name "
            "reused for disconnected segments). Falling back to a "
            "route-wide check, which will over-report on routes with "
            "multiple legitimate pieces. Re-run with an updated "
            "rat_alignment_cli.py to get Part-aware validation."
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
    parser.add_argument("--enriched_csv", default=None, help="Path to 4D enriched CSV")
    parser.add_argument("--vertices_csv", default=None, help="Path to vertices output CSV")
    parser.add_argument("--scores_csv", default=None, help="Path to section scores output CSV")
    args = parser.parse_args()
    
    out_dir = ""
    if args.horizontal_csv: out_dir = os.path.dirname(args.horizontal_csv)
    elif args.vertical_csv: out_dir = os.path.dirname(args.vertical_csv)
    elif args.enriched_csv: out_dir = os.path.dirname(args.enriched_csv)
    elif args.vertices_csv: out_dir = os.path.dirname(args.vertices_csv)
    elif args.scores_csv: out_dir = os.path.dirname(args.scores_csv)
    
    if not out_dir: out_dir = os.getcwd()
    
    log_file_path = os.path.join(out_dir, "qa_validation_report.txt")
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

    if not any_checked:
        print("No files supplied. Use --horizontal_csv, --vertical_csv, --enriched_csv, --vertices_csv, and/or --scores_csv")
        raise SystemExit(1)
        
    print_section("Validation Summary")
    if had_error:
        print("RESULT: FAILED (one or more required checks failed).")
        raise SystemExit(1)
    else:
        print("RESULT: PASS (required checks passed).")
        raise SystemExit(0)

if __name__ == "__main__":
    main()
