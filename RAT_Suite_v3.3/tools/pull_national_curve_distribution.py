# tools/pull_national_curve_distribution.py

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
RAT NATIONAL CURVE-CLASS DISTRIBUTION
--------------------------------------------------------------------------------
ROLE: Prepares the curve-class proportions the national calibration uses to
size its samples. Run once, or again when a new HPMS release is out.

DESCRIPTION:
Cochran's sample-size formula, n0 = Z^2 p q / e^2, is written for a yes/no
outcome. The calibration samples are really estimating a curve severity
class (A-F), so p q is replaced by the largest category variance,
max_k[p_k (1 - p_k)], which gives the required precision for every class.
This script computes the p_k from the HPMS-reported curve inventory
(CURVES_B to CURVES_F), by state and functional system and nationally; the
calibration script reads the result and sets the sample sizes.

Reporting panels. HPMS reports curve inventory per sample panel
(SAMPLE_ID), which can span many miles. States place the values on the
panel's records in different ways:
  - repeated on every record the panel covers (Washington; summing them
    record by record overcounts the curves up to 200 times),
  - on one or a few records only, the others left blank,
  - split among the records, each record carrying its own curves.
Records are grouped by SAMPLE_ID. Where every record that carries curve
values carries the same values, they are taken once; where the values
differ between records, they are summed. (A panel split among records that
happen to carry identical values is therefore counted once; it cannot be
told apart from a repeated panel.) The panel length is the sum of
SectionLength over all of the panel's records, with or without curve
values. A panel that reports more curve mileage than its length (by more
than PANEL_LENGTH_TOL_MI) is inconsistent: it is kept in the panel table,
flagged, and left out of the proportions.

Class A is everything not reported as B-F, tangents included, so it is not
pulled: CURVES_A = panel length - sum(CURVES_B..F).

Curve inventory is reported on sample panels, and Local roads (FS7) are not
in the HPMS sample, so proportions are computed for FS1 through FS6 only.
The calibration sizes its FS7 samples with the largest variance, 0.25
(NO_CURVE_DATA_FS in tools/rat_national_calibration.py). A state and
functional system with fewer than MIN_PANELS_FOR_STATE_ESTIMATE panels
is marked unusable, and the calibration uses the national figure for it.
Some states report no curve data at all (Nebraska).

The field names are checked against a pull for Washington (53), which
reports curve data, before all states are pulled.

OUTPUT:
  core/national_curve_class_proportions.json (read by the calibration script)
  OUTPUT_DIR/panel_level_raw.csv             (one row per panel, all states)
"""
import os
import sys
import json
import logging
import time

import numpy as np
import pandas as pd

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
RAT_SUITE_DIR = os.path.dirname(THIS_DIR)
if RAT_SUITE_DIR not in sys.path:
    sys.path.insert(0, RAT_SUITE_DIR)

from core.rat_core import ALL_FIPS, SOCRATA_DEFAULT, SOCRATA_TOKEN as ENV_SOCRATA_TOKEN, _socrata_rows

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s: %(message)s")

# ===========================================================================
# CONFIGURATION
# ===========================================================================
OUTPUT_DIR = "output/curve_dist"   # panel-level audit CSV
PROPORTIONS_JSON = None            # None = core/national_curve_class_proportions.json
STATES = "ALL"                     # "ALL", or a comma-separated list like "31,53"
SOCRATA_TOKEN = ""                 # blank = the RAT_SOCRATA_TOKEN environment variable
MIN_PANELS_FOR_STATE_ESTIMATE = 10 # fewer panels: the national figure is used
PANEL_LENGTH_TOL_MI = 0.01         # curve miles may exceed the panel length by this much
# ===========================================================================

CURVE_FIELDS = ["curves_b", "curves_c", "curves_d", "curves_e", "curves_f"]
PANEL_COLUMNS = (["state_fips", "sample_id", "f_system", "panel_length"] + CURVE_FIELDS
                 + ["curves_a_implied", "curve_values", "consistent"])


def _fetch_state_curve_fields(state_fips: str, token: str) -> pd.DataFrame:
    """One state's records with the fields needed for the panel totals,
    limited to Facility Types 1 and 2 like the calibration sample. Socrata
    names the HPMS SectionLength field "sectionlength"."""
    select_fields = ["sample_id", "sectionlength", "f_system", "stateid"] + CURVE_FIELDS
    params = {
        "$select": ",".join(select_fields),
        "$limit": 100_000,
        "$offset": 0,
        "$where": f"stateid='{state_fips}' AND facility_type IN ('1', '2')",
        "$order": "shapeid",
    }
    rows = _socrata_rows(SOCRATA_DEFAULT, token, params)
    if not rows:
        return pd.DataFrame(columns=["sample_id", "section_length", "f_system", "stateid"] + CURVE_FIELDS)
    return pd.DataFrame(rows).rename(columns={"sectionlength": "section_length"})


def _verify_field_names(token: str) -> None:
    """Pulls Washington (53), which reports curve data, and stops with the
    fields actually returned if any expected field is missing. (Socrata
    leaves out fields that are null on every row, so a state without curve
    data cannot be used for this check.)"""
    logging.info("Checking field names against a pull for Washington (53)...")
    test_df = _fetch_state_curve_fields("53", token)
    if test_df.empty:
        raise RuntimeError(
            "The test pull for Washington (53) returned no rows. Check the field names "
            "(sample_id, sectionlength, curves_b..curves_f) against the dataset's column list."
        )
    expected = {"sample_id", "section_length", "f_system", "stateid"} | set(CURVE_FIELDS)
    missing = expected - set(test_df.columns)
    if missing:
        raise RuntimeError(
            f"The test pull is missing fields {sorted(missing)}. "
            f"Fields returned: {sorted(test_df.columns)}."
        )
    logging.info(f"Field names OK ({len(test_df):,} rows in the test pull).")


def _aggregate_to_panels(df: pd.DataFrame, state_fips: str) -> pd.DataFrame:
    """One row per reporting panel (SAMPLE_ID) with its length, its curve
    mileage by class, class A as the remainder, how the curve values were
    placed on its records ("repeated" or "split"), and whether the curve
    mileage fits in the panel length. See the module docstring."""
    if df.empty:
        return pd.DataFrame(columns=PANEL_COLUMNS)

    # Socrata leaves out a field that is null on every row returned (a State
    # with no class F curves anywhere has no curves_f), so absent fields are
    # added as blank rather than losing the State's other classes.
    for col in ["sample_id", "f_system", "section_length"] + CURVE_FIELDS:
        if col not in df.columns:
            df[col] = np.nan
    for col in ["section_length"] + CURVE_FIELDS:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df[df["sample_id"].notna()].copy()

    has_curves = df[CURVE_FIELDS].notna().any(axis=1)
    reporting_ids = df.loc[has_curves, "sample_id"].unique()
    if len(reporting_ids) == 0:
        return pd.DataFrame(columns=PANEL_COLUMNS)
    df = df[df["sample_id"].isin(reporting_ids)]
    has_curves = df[CURVE_FIELDS].notna().any(axis=1)

    fsys_check = df.groupby("sample_id")["f_system"].nunique()
    inconsistent_fs = fsys_check[fsys_check > 1]
    if len(inconsistent_fs):
        logging.warning(f"[{state_fips}] {len(inconsistent_fs)} panel(s) span more than one "
                        f"F_SYSTEM; the first value is used.")

    # Panel length over all of the panel's records.
    panels = df.groupby("sample_id").agg(
        f_system=("f_system", "first"),
        panel_length=("section_length", "sum"),
    )

    # Curve values: once if every record that carries them carries the
    # same values, summed otherwise.
    carrying = df.loc[has_curves, ["sample_id"] + CURVE_FIELDS].copy()
    carrying[CURVE_FIELDS] = carrying[CURVE_FIELDS].fillna(0.0)
    key = carrying[CURVE_FIELDS].round(4).astype(str).agg("|".join, axis=1)
    repeated = key.groupby(carrying["sample_id"]).nunique() == 1
    once = carrying.groupby("sample_id")[CURVE_FIELDS].first()
    summed = carrying.groupby("sample_id")[CURVE_FIELDS].sum()
    curves = once.where(repeated, summed, axis=0)
    panels = panels.join(curves)
    panels["curve_values"] = np.where(repeated.reindex(panels.index), "repeated", "split")

    remainder = panels["panel_length"] - panels[CURVE_FIELDS].sum(axis=1)
    panels["consistent"] = remainder >= -PANEL_LENGTH_TOL_MI
    n_bad = int((~panels["consistent"]).sum())
    if n_bad:
        logging.warning(f"[{state_fips}] {n_bad} of {len(panels)} panel(s) report more curve "
                        f"mileage than their length; left out of the proportions.")
    panels["curves_a_implied"] = remainder.clip(lower=0.0)
    panels = panels.reset_index()
    panels["state_fips"] = state_fips
    return panels[PANEL_COLUMNS]


def _proportions_from_panels(panels: pd.DataFrame) -> dict:
    """Mileage-weighted class proportions: class miles summed over the
    panels divided by total panel miles (not an average of panel shares)."""
    total_len = panels["panel_length"].sum()
    if total_len <= 0:
        return {}
    props = {"A": float(panels["curves_a_implied"].sum() / total_len)}
    for field, cat in zip(CURVE_FIELDS, ["B", "C", "D", "E", "F"]):
        props[cat] = float(panels[field].fillna(0).sum() / total_len)
    return props


def main():
    token = SOCRATA_TOKEN or ENV_SOCRATA_TOKEN
    json_path = PROPORTIONS_JSON or os.path.join(RAT_SUITE_DIR, "core", "national_curve_class_proportions.json")
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    _verify_field_names(token)

    states = ALL_FIPS if STATES.upper() == "ALL" else [s.strip().zfill(2) for s in STATES.split(",")]

    all_panels = []
    for i, state_fips in enumerate(states, 1):
        logging.info(f"[{i}/{len(states)}] Fetching curve data for state {state_fips}...")
        try:
            raw = _fetch_state_curve_fields(state_fips, token)
            panels = _aggregate_to_panels(raw, state_fips)
            logging.info(f"  [{state_fips}] {len(panels)} reporting panels "
                         f"({raw['sample_id'].nunique() if not raw.empty else 0} sample_ids in the pull)")
            all_panels.append(panels)
        except Exception as e:
            logging.error(f"  [{state_fips}] failed: {e}. The calibration will use the "
                          f"national figures for this state.")
        time.sleep(0.2)

    panel_df = pd.concat(all_panels, ignore_index=True) if all_panels else pd.DataFrame(columns=PANEL_COLUMNS)
    panel_csv_path = os.path.join(OUTPUT_DIR, "panel_level_raw.csv")
    panel_df.to_csv(panel_csv_path, index=False)
    logging.info(f"Panel table written: {panel_csv_path} ({len(panel_df):,} panels)")

    curve_fs = range(1, 7)   # FS7 is not in the HPMS sample (see the docstring)

    result = {"by_state": {}, "national": {}, "meta": {
        "min_panels_for_state_estimate": MIN_PANELS_FOR_STATE_ESTIMATE,
        "note": "national is used where a state/FS has fewer than "
                "min_panels_for_state_estimate panels",
        "fs_covered": "FS1-FS6; FS7 (local roads) is not in the HPMS sample",
    }}

    if panel_df.empty:
        logging.warning("No panel data from any state; the output is empty.")
    else:
        panel_df["f_system"] = pd.to_numeric(panel_df["f_system"], errors="coerce")
        n_all = len(panel_df)
        panel_df = panel_df[panel_df["consistent"].astype(bool)]
        logging.info(f"{len(panel_df):,} of {n_all:,} panels are consistent and are used "
                     f"for the proportions.")

        for fs in curve_fs:
            grp = panel_df[panel_df["f_system"] == fs]
            if grp.empty:
                continue
            result["national"][str(fs)] = {"proportions": _proportions_from_panels(grp),
                                           "n_panels": int(len(grp))}

        for state_fips in sorted(panel_df["state_fips"].dropna().unique()):
            state_panels = panel_df[panel_df["state_fips"] == state_fips]
            for fs in curve_fs:
                grp = state_panels[state_panels["f_system"] == fs]
                if grp.empty:
                    continue
                result["by_state"].setdefault(state_fips, {})[str(fs)] = {
                    "proportions": _proportions_from_panels(grp),
                    "n_panels": int(len(grp)),
                    "usable": len(grp) >= MIN_PANELS_FOR_STATE_ESTIMATE,
                }

    with open(json_path, "w") as f:
        json.dump(result, f, indent=2)
    logging.info(f"Curve-class proportions written: {json_path}")


if __name__ == "__main__":
    main()