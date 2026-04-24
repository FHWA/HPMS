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
RAT RESULTS VALIDATOR (QA/QC Module)
--------------------------------------------------------------------------------
ROLE: Automated quality assurance checker for RAT Suite outputs.
DESCRIPTION: 
Scans generated CSVs and 4D outputs to ensure data integrity. Checks for 
missing columns, invalid distances, missing WKT_ZM values, and flags abnormal 
elevation data (like un-interpolated NaNs) before final production delivery.
CREATED BY: FHWA, Office of Highway Policy Information using Google Gemini and
ChatGPT.
CREATED ON: 4/23/2026
"""
import os
import argparse
import numpy as np
import pandas as pd
REQUIRED_H = ["RouteId", "Start_Dist", "End_Dist", "Length_m"]
REQUIRED_V = ["RouteId", "Start_Dist", "End_Dist", "Length_m"]
H_OPTIONAL_CHECKS = ["Radius_m", "Delta", "Dir", "Bin"]
V_OPTIONAL_CHECKS = ["Grade_In", "Grade_Out", "Alg_Diff", "K_Value", "Type", "Grade_Bin"]

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

def validate_horizontal(df: pd.DataFrame):
    issues = []
    warnings = []
    for c in REQUIRED_H:
        if c not in df.columns:
            issues.append(f"Missing required column: {c}")
    if issues:
        return issues, warnings
    # Core integrity checks
    bad_len = (pd.to_numeric(df["Length_m"], errors="coerce") <= 0).sum()
    if bad_len > 0:
        issues.append(f"Rows with Length_m <= 0: {int(bad_len)}")
    bad_dist = (
        pd.to_numeric(df["End_Dist"], errors="coerce")
        <= pd.to_numeric(df["Start_Dist"], errors="coerce")
    ).sum()
    if bad_dist > 0:
        issues.append(f"Rows where End_Dist <= Start_Dist: {int(bad_dist)}")
    # Optional checks (only if columns exist)
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
    # Core integrity checks
    bad_len = (pd.to_numeric(df["Length_m"], errors="coerce") <= 0).sum()
    if bad_len > 0:
        issues.append(f"Rows with Length_m <= 0: {int(bad_len)}")
    bad_dist = (
        pd.to_numeric(df["End_Dist"], errors="coerce")
        <= pd.to_numeric(df["Start_Dist"], errors="coerce")
    ).sum()
    if bad_dist > 0:
        issues.append(f"Rows where End_Dist <= Start_Dist: {int(bad_dist)}")
    # Optional checks
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
    parser.add_argument("--enriched_csv", default=None, help="Path to 4D enriched CSV (optional)")
    args = parser.parse_args()
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
                for i in issues:
                    print(f"- {i}")
                had_error = True
            else:
                print("\nPASS: Required integrity checks passed.")
            if warnings:
                print("\nWARNINGS:")
                for w in warnings:
                    print(f"- {w}")
            # useful stats
            r = summarize_numeric(h, "Radius_m")
            if r:
                print(f"\nRadius_m min/mean/max: {r[0]:.2f} / {r[1]:.2f} / {r[2]:.2f}")
            l = summarize_numeric(h, "Length_m")
            if l:
                print(f"Length_m min/mean/max: {l[0]:.2f} / {l[1]:.2f} / {l[2]:.2f}")
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
                for i in issues:
                    print(f"- {i}")
                had_error = True
            else:
                print("\nPASS: Required integrity checks passed.")
            if warnings:
                print("\nWARNINGS:")
                for w in warnings:
                    print(f"- {w}")
            k = summarize_numeric(v, "K_Value")
            if k:
                print(f"\nK_Value min/mean/max: {k[0]:.2f} / {k[1]:.2f} / {k[2]:.2f}")
            l = summarize_numeric(v, "Length_m")
            if l:
                print(f"Length_m min/mean/max: {l[0]:.2f} / {l[1]:.2f} / {l[2]:.2f}")
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
    if not any_checked:
        print("No files supplied. Use --horizontal_csv and/or --vertical_csv and/or --enriched_csv")
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