# tools/rat_structure_span_check.py

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
RAT STRUCTURE SPAN CHECK
--------------------------------------------------------------------------------
Re-runs the core bridge/tunnel span detection (core.rat_core.
repair_structure_spans) on the raw elevations in existing plan_profile
vertices CSVs, with no pipeline run or downloads. Useful for checking a
route after a code or parameter change, or for trying other settings through
PARAM_OVERRIDES. Writes the same span list and PDF as the pipeline's
Structure Span Report (core.rat_structure_report); the PDF's relief maps
are drawn when DEM_DIR points at the cached 1 m tiles.

HPMS/NBI labels come from the CSV's Structure_Record column (Structure_Tier
in CSVs without it), so they reflect the run that made the CSV.

Usage. Edit the CONFIGURATION block, then from the RAT Suite root:
    python tools/rat_structure_span_check.py

CREATED BY: Federal Highway Administration, Office of Highway Policy Information.
CREATED ON: 10/14/2026
"""
import os
import sys

import numpy as np
import pandas as pd

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
RAT_SUITE_DIR = os.path.dirname(THIS_DIR)
if RAT_SUITE_DIR not in sys.path:
    sys.path.insert(0, RAT_SUITE_DIR)

from core.rat_core import (
    FEET_PER_METER,
    build_params,
    repair_structure_spans,
    STRUCTURE_KIND_LABELS,
    STRUCTURE_TIER_LABELS,
)
from core.rat_structure_report import build_structure_span_table, render_structure_span_pdf

# ===========================================================================
# CONFIGURATION
# ===========================================================================
VERTICES_CSVS = [
    r"output/plan_profile_080_MP_299.980_to_320.040_vertices.csv",
]
OUTDIR = "output/structure_span_check"

# Folder holding the cached 1 m tiles (tile_1m_hr*.tif) for the relief maps,
# usually <DEM cache>/align_1m_cache. Set to None to skip the maps.
DEM_DIR = None

# Any core parameter can be overridden here to test a different setting,
# e.g. {"DIP_THRESHOLD_FT": 6.5, "STRUCTURE_EDGE_GRADE_PCT": 15.0}
PARAM_OVERRIDES = {}
# ===========================================================================


def _source_mask(df: pd.DataFrame):
    if "Structure_Record" in df.columns:
        return df["Structure_Record"].map({"HPMS": 1, "NBI": 2}).fillna(0).astype(int).to_numpy()
    if "Structure_Tier" in df.columns:
        return df["Structure_Tier"].map({"1_HPMS": 1, "2_NBI": 2}).fillna(0).astype(int).to_numpy()
    return None


def check_file(csv_path: str, params: dict) -> pd.DataFrame:
    df = pd.read_csv(csv_path, keep_default_na=False, dtype={"RouteId": str})
    dist_ft = df["Dist_Ft"].to_numpy(dtype=float)
    z_raw_m = df["Elev_Raw_Ft"].to_numpy(dtype=float) / FEET_PER_METER
    spacing_m = float(np.median(np.diff(dist_ft))) / FEET_PER_METER

    z_fixed_m, tier, kind = repair_structure_spans(z_raw_m, spacing_m, params, _source_mask(df))

    chk = df.copy()
    chk["Elev_Ft"] = z_fixed_m * FEET_PER_METER
    chk["Structure_Kind"] = [STRUCTURE_KIND_LABELS[int(k)] for k in kind]
    chk["Structure_Tier"] = [STRUCTURE_TIER_LABELS[int(t)] for t in tier]

    spans = build_structure_span_table(chk)
    spans.insert(0, "File", os.path.basename(csv_path))
    stem = os.path.splitext(os.path.basename(csv_path))[0]
    if not spans.empty:
        pdf_path = os.path.join(OUTDIR, f"{stem}_structure_spans.pdf")
        render_structure_span_pdf(chk, spans.drop(columns="File"), pdf_path, dem_dir=DEM_DIR,
                                  title="Span check --", profile_label="Span repair (before smoothing)")
        print(f"{os.path.basename(csv_path)}: {len(spans)} span(s) -> {pdf_path}")
    else:
        print(f"{os.path.basename(csv_path)}: no spans found")
    return spans


def main():
    os.makedirs(OUTDIR, exist_ok=True)
    params = build_params(PARAM_OVERRIDES)
    all_spans = [check_file(p, params) for p in VERTICES_CSVS]
    out_csv = os.path.join(OUTDIR, "structure_spans.csv")
    pd.concat(all_spans, ignore_index=True).to_csv(out_csv, index=False)
    print(f"Wrote {out_csv}")


if __name__ == "__main__":
    main()