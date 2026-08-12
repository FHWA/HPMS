# apps/rat_unified_gui.py

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
RAT UNIFIED GUI v3.3 (Graphical User Interface)
--------------------------------------------------------------------------------
ROLE: The primary user-facing orchestrator for the RAT Suite.
DESCRIPTION:
Provides a Tkinter-based interface for users to configure alignment, plan/profile,
and 4D enrichment parameters. It does not perform mathematical processing; instead,
it collects inputs, packages them into a JSON payload, and uses the subprocess
module to safely execute the suite's CLI scripts in isolated memory environments.
"""

import os
import sys
import json
import glob
import threading
import logging
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

import requests
import pandas as pd
import geopandas as gpd
from shapely.geometry import shape
import subprocess
import time

# ----------------------------
# Path bootstrap for core import
# ----------------------------
THIS_DIR = os.path.dirname(os.path.abspath(__file__))
RAT_SUITE_DIR = os.path.dirname(THIS_DIR)
if RAT_SUITE_DIR not in sys.path:
    sys.path.insert(0, RAT_SUITE_DIR)

from core.rat_core import fetch_socrata_state

SOCRATA_DEFAULT = "https://datahub.transportation.gov/resource/42um-tgh5.json"
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s: %(message)s")

HPMS_FIELD_CATALOG = [
    ("stateid",                "State FIPS",                             True),
    ("year_record",            "Data Year",                              True),
    ("route_id",               "Route ID",                               True),
    ("begin_point",            "Begin Point (Start MP)",                 True),
    ("end_point",              "End Point (End MP)",                     True),
    ("f_system",               "Functional System",                      True),
    ("facility_type",          "Facility Type",                          True),
    ("urban_id",               "Urban Area Code",                        True),
    ("nhs",                    "NHS Status",                             True),
    ("strahnet_type",          "STRAHNET Type",                          False),
    ("nn",                     "National Truck Network",                 False),
    ("nhfn",                   "National Highway Freight Network",       False),
    ("ownership",              "Ownership",                              True),
    ("county_id",              "County FIPS",                            True),
    ("maintenance_operations", "Maintenance Operations",                 False),
    ("is_restricted",          "Access Restricted",                      True),
    ("route_signing",          "Route Signing",                          True),
    ("route_number",           "Route Number",                           True),
    ("route_qualifier",        "Route Qualifier",                        False),
    ("route_name",             "Route Name",                             False),
    ("sample_id",              "Sample ID",                              False),
    ("section_length",         "Section Length",                         True),
    ("shape_id",               "Shape ID",                               False),
    ("srid",                   "Spatial Reference ID",                   False),
    ("through_lanes",          "Through Lanes",                          True),
    ("dir_through_lanes",      "Directional Through Lanes",              False),
    ("managed_lanes_type",     "Managed Lanes Type",                     False),
    ("managed_lanes",          "Managed Lanes Count",                    False),
    ("peak_lanes",             "Peak Direction Lanes",                   False),
    ("counter_peak_lanes",     "Counter-Peak Direction Lanes",           False),
    ("peak_parking",           "Peak Parking",                           False),
    ("toll_id",                "Toll Facility ID",                       False),
    ("lane_width",             "Lane Width",                             False),
    ("median_type",            "Median Type",                            False),
    ("median_width",           "Median Width",                           False),
    ("shoulder_type",          "Shoulder Type",                          False),
    ("shoulder_width_r",       "Right Shoulder Width",                   False),
    ("shoulder_width_l",       "Left Shoulder Width",                    False),
    ("turn_lanes_r",           "Right Turn Lanes",                       False),
    ("turn_lanes_l",           "Left Turn Lanes",                        False),
    ("widening_potential",     "Widening Potential (Lanes)",             False),
    ("widening_obstacle",      "Widening Obstacle",                      False),
    ("access_control",         "Access Control",                         True),
    ("structure_type",         "Structure Type",                         True),
    ("bridge_id",              "Bridge ID (NBI Linkage)",                True),
    ("tunnel_id",              "Tunnel ID (NTI Linkage)",                True),
    ("signal_type",            "Signal Type",                            False),
    ("pct_green_time",         "Percent Green Time",                     False),
    ("number_signals",         "Number of Signals",                      False),
    ("stop_signs",             "Stop Signs",                             False),
    ("at_grade_other",         "At-Grade Other Intersections",           False),
    ("aadt",                   "AADT",                                   True),
    ("aadt_d",                 "AADT Collection Date",                   False),
    ("aadt_single_unit",       "AADT Single Unit Trucks",                True),
    ("aadt_combination",       "AADT Combination Trucks",                True),
    ("pct_dh_single_unit",     "% Peak Hour Single Unit Trucks",         False),
    ("pct_dh_combination",     "% Peak Hour Combination Trucks",         False),
    ("k_factor",               "K-Factor",                               False),
    ("dir_factor",             "Directional Distribution Factor",        False),
    ("future_aadt",            "Future AADT",                            False),
    ("future_aadt_year",       "Future AADT Year",                       False),
    ("speed_limit",            "Speed Limit",                            True),
    ("surface_type",           "Surface Type",                           False),
    ("iri",                    "IRI (Roughness)",                        False),
    ("iri_d",                  "IRI Collection Date",                    False),
    ("psr",                    "Pavement Serviceability Rating",         False),
    ("psr_d",                  "PSR Collection Date",                    False),
    ("rutting",                "Rutting",                                False),
    ("rutting_d",              "Rutting Collection Date",                False),
    ("faulting",               "Faulting",                               False),
    ("faulting_d",             "Faulting Collection Date",               False),
    ("cracking_percent",       "Cracking Percent",                       False),
    ("cracking_percent_d",     "Cracking Collection Date",               False),
    ("year_last_improvement",  "Year Last Improved",                     False),
    ("year_last_construction", "Year Last Constructed",                  False),
    ("last_overlay_thickness", "Last Overlay Thickness",                 False),
    ("thickness_rigid",        "Rigid Pavement Thickness",               False),
    ("thickness_flexible",     "Flexible Pavement Thickness",            False),
    ("base_type",              "Base Pavement Type",                     False),
    ("base_thickness",         "Base Thickness",                         False),
    ("soil_type",              "Soil Type (AASHTO)",                     False),
    ("curves_a",               "Curves A — Length (mi)",                 True),
    ("curves_b",               "Curves B — Length (mi)",                 True),
    ("curves_c",               "Curves C — Length (mi)",                 True),
    ("curves_d",               "Curves D — Length (mi)",                 True),
    ("curves_e",               "Curves E — Length (mi)",                 True),
    ("curves_f",               "Curves F — Length (mi)",                 True),
    ("terrain_type",           "Terrain Type",                           False),
    ("grades_a",               "Grades A — Length (mi)",                 True),
    ("grades_b",               "Grades B — Length (mi)",                 True),
    ("grades_c",               "Grades C — Length (mi)",                 True),
    ("grades_d",               "Grades D — Length (mi)",                 True),
    ("grades_e",               "Grades E — Length (mi)",                 True),
    ("grades_f",               "Grades F — Length (mi)",                 True),
    ("pct_pass_sight",         "Percent Passing Sight Distance",         False),
    ("travel_time_code",       "Travel Time Code",                       False),
]

class HMPSFieldSelectorDialog:
    def __init__(self, parent_root, current_selection):
        self.result = None

        self.win = tk.Toplevel(parent_root)
        self.win.title("4D Output Field Selection")
        self.win.geometry("640x580")
        self.win.transient(parent_root)
        self.win.grab_set()
        self.win.resizable(True, True)

        catalog_names = {f[0] for f in HPMS_FIELD_CATALOG}
        extra = [f for f in (current_selection or []) if f not in catalog_names]
        self._all_fields = list(HPMS_FIELD_CATALOG) + [
            (f, f, True) for f in extra
        ]

        if current_selection is None:
            self._vars = {
                name: tk.BooleanVar(value=default_on)
                for name, _, default_on in self._all_fields
            }
        else:
            self._vars = {
                name: tk.BooleanVar(value=(name in current_selection))
                for name, _, _ in self._all_fields
            }

        self._build(parent_root)

    def _build(self, parent_root):
        outer = ttk.Frame(self.win, padding=10)
        outer.pack(fill=tk.BOTH, expand=True)

        ttk.Label(
            outer,
            text=(
                "Select which HPMS fields to include in the 4D enriched output.\n"
                "Fields not checked will be dropped from the CSV and GeoPackage.\n"
                "Geometry (WKT) is always replaced by WKT_ZM and is never retained."
            ),
            justify="left",
            font=("Arial", 9, "italic"),
        ).pack(anchor="w", pady=(0, 6))

        search_frame = ttk.Frame(outer)
        search_frame.pack(fill=tk.X, pady=(0, 4))
        ttk.Label(search_frame, text="Search:").pack(side=tk.LEFT)
        self._search_var = tk.StringVar()
        self._search_var.trace_add("write", self._filter_list)
        ttk.Entry(search_frame, textvariable=self._search_var, width=30).pack(
            side=tk.LEFT, padx=6
        )

        ttk.Button(search_frame, text="Select All",   command=self._select_all).pack(side=tk.LEFT, padx=4)
        ttk.Button(search_frame, text="Deselect All", command=self._deselect_all).pack(side=tk.LEFT, padx=2)

        list_frame = ttk.Frame(outer, relief="sunken", borderwidth=1)
        list_frame.pack(fill=tk.BOTH, expand=True)

        canvas = tk.Canvas(list_frame, highlightthickness=0)
        sb     = ttk.Scrollbar(list_frame, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=sb.set)
        sb.pack(side=tk.RIGHT, fill=tk.Y)
        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        self._cb_frame = ttk.Frame(canvas)
        self._cb_window = canvas.create_window((0, 0), window=self._cb_frame, anchor="nw")
        self._cb_frame.bind(
            "<Configure>",
            lambda e: canvas.configure(scrollregion=canvas.bbox("all")),
        )
        canvas.bind(
            "<Configure>",
            lambda e: canvas.itemconfig(self._cb_window, width=e.width),
        )
        canvas.bind_all("<MouseWheel>", self._on_mousewheel)

        self._canvas    = canvas
        self._cb_rows   = []
        self._render_checkboxes()

        self._summary_var = tk.StringVar()
        ttk.Label(outer, textvariable=self._summary_var, font=("Arial", 9)).pack(
            anchor="w", pady=(4, 0)
        )
        self._update_summary()

        btn_frame = ttk.Frame(outer)
        btn_frame.pack(fill=tk.X, pady=(8, 0))
        ttk.Button(btn_frame, text="Cancel",  command=self._cancel).pack(side=tk.RIGHT, padx=4)
        ttk.Button(btn_frame, text="Confirm", command=self._confirm).pack(side=tk.RIGHT, padx=4)

        self.win.protocol("WM_DELETE_WINDOW", self._cancel)

    def _render_checkboxes(self):
        for w, _ in self._cb_rows:
            w.destroy()
        self._cb_rows.clear()

        term = self._search_var.get().lower() if hasattr(self, "_search_var") else ""

        for name, label, _ in self._all_fields:
            display = f"{label}  ({name})"
            if term and term not in display.lower():
                continue
            cb = ttk.Checkbutton(
                self._cb_frame,
                text=display,
                variable=self._vars[name],
                command=self._update_summary,
            )
            cb.pack(anchor="w", padx=8, pady=1)
            self._cb_rows.append((cb, name))

    def _filter_list(self, *_):
        self._render_checkboxes()

    def _select_all(self):
        term = self._search_var.get().lower()
        for name, label, _ in self._all_fields:
            if not term or term in f"{label}  ({name})".lower():
                self._vars[name].set(True)
        self._update_summary()

    def _deselect_all(self):
        term = self._search_var.get().lower()
        for name, label, _ in self._all_fields:
            if not term or term in f"{label}  ({name})".lower():
                self._vars[name].set(False)
        self._update_summary()

    def _update_summary(self):
        selected = sum(1 for v in self._vars.values() if v.get())
        total    = len(self._vars)
        self._summary_var.set(f"{selected} of {total} fields selected for output.")

    def _on_mousewheel(self, event):
        try:
            self._canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
        except tk.TclError:
            try:
                self._canvas.unbind_all("<MouseWheel>")
            except Exception:
                pass

    def _confirm(self):
        self.result = [name for name, _, _ in self._all_fields if self._vars[name].get()]
        try:
            self._canvas.unbind_all("<MouseWheel>")
        except Exception:
            pass
        self.win.destroy()

    def _cancel(self):
        self.result = None
        try:
            self._canvas.unbind_all("<MouseWheel>")
        except Exception:
            pass
        self.win.destroy()


class TextHandler(logging.Handler):
    def __init__(self, text_widget):
        super().__init__()
        self.text_widget = text_widget

    def emit(self, record):
        msg = self.format(record)
        def append():
            self.text_widget.configure(state="normal")
            self.text_widget.insert(tk.END, msg + "\n")
            self.text_widget.configure(state="disabled")
            self.text_widget.see(tk.END)
        self.text_widget.after(0, append)


class RATUnifiedGUI:
    def __init__(self, root):
        self.root = root
        self.root.title("RAT Unified GUI v3.3")
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        w = min(1120, sw - 40)
        h = min(980, sh - 80)
        self.root.geometry(f"{w}x{h}")
        self.root.resizable(True, True)
        self.root.configure(bg="#E0E0E0")

        self.vars = {
            "input_url": tk.StringVar(value=SOCRATA_DEFAULT),
            "input_local": tk.StringVar(),
            "output_dir": tk.StringVar(),
            "dem_dir": tk.StringVar(value=os.path.join(os.getcwd(), "elevation_cache")),
            "state_fips": tk.StringVar(),
            "socrata_token": tk.StringVar(),
            "nbi_url": tk.StringVar(),
            "nti_url": tk.StringVar(),
            "use_local": tk.BooleanVar(value=False),
            "reuse_socrata_cache": tk.BooleanVar(value=False),
            "simplify_geometry": tk.BooleanVar(value=True),

            "densify_spacing_ft": tk.StringVar(value="5"),
            "h_smooth_factor": tk.StringVar(value="400"),
            "v_smooth_factor": tk.StringVar(value="1400"),
            "h_min_delta": tk.StringVar(value="3.5"),
            "h_min_curve_length_ft": tk.StringVar(value="100"),
            "v_min_curve_length_ft": tk.StringVar(value="200"),
            "v_min_grade_change": tk.StringVar(value="0.5"),

            "enable_merge": tk.BooleanVar(value=False),
            "merge_gap_ft": tk.StringVar(value="600"),
            "v_merge_gap_ft": tk.StringVar(value="1500"),
            "trim_curve_endpoints": tk.BooleanVar(value=True),

            "fac_1": tk.BooleanVar(value=True),  
            "fac_2": tk.BooleanVar(value=True),  
            "fac_4": tk.BooleanVar(value=False), 
            "fac_5": tk.BooleanVar(value=False), 
            "fac_6": tk.BooleanVar(value=False), 
            "fac_7": tk.BooleanVar(value=False), 

            "auto_validate": tk.BooleanVar(value=True),
            "open_output_when_done": tk.BooleanVar(value=True),

            "pp_route_id": tk.StringVar(),
            "pp_start_rp": tk.StringVar(value=""),
            "pp_end_rp": tk.StringVar(value=""),

            "do_alignment": tk.BooleanVar(value=True),
            "do_plan_profile": tk.BooleanVar(value=False),
            "do_4d": tk.BooleanVar(value=False),
            "replace_curve_grade": tk.BooleanVar(value=False),

            "out_csv": tk.BooleanVar(value=True),
            "out_geojson": tk.BooleanVar(value=True),
            "out_gpkg": tk.BooleanVar(value=False),
            "out_shp": tk.BooleanVar(value=False),
            "out_html_map": tk.BooleanVar(value=True),
            "out_dashboard": tk.BooleanVar(value=True),
            "out_qa_exceptions": tk.BooleanVar(value=True),
            "out_pdf": tk.BooleanVar(value=False),
            "out_vtx_spatial": tk.BooleanVar(value=False),
        }

        self.fsys_vars = {i: tk.BooleanVar(value=True if i < 7 else False) for i in range(1, 8)}

        self.advanced_defaults = {
            "H_BASE_SMOOTH_WINDOW": tk.StringVar(value="21"),
            "H_MIN_HEAD_CHANGE": tk.StringVar(value="0.003"),
            "H_MAX_RADIUS_FT": tk.StringVar(value="165000"),
            "V_VC_THRESHOLD": tk.StringVar(value="0.002"),
            "V_GAP_TOLERANCE": tk.StringVar(value="5"),
            "V_MIN_OFFSET_FT": tk.StringVar(value="0.10"),
            "V_REVERSAL_TOLERANCE": tk.StringVar(value="0.02"),
            "REGRESSION_WINDOW_FT": tk.StringVar(value="500"),
            "TREND_WINDOW_FT": tk.StringVar(value="1000"),
            "DIP_THRESHOLD_FT": tk.StringVar(value="6.5"),
            "BRIDGE_MAX_LEN_FT": tk.StringVar(value="8200"),
            "H_SMOOTH_FACTOR_FS2": tk.IntVar(value=200),
            "V_SMOOTH_FACTOR_FS2": tk.IntVar(value=1400),
            "H_SMOOTH_FACTOR_FS3": tk.IntVar(value=400),
            "V_SMOOTH_FACTOR_FS3": tk.IntVar(value=1400),
            "H_SMOOTH_FACTOR_FS4": tk.IntVar(value=400),
            "V_SMOOTH_FACTOR_FS4": tk.IntVar(value=1400),
            "H_SMOOTH_FACTOR_FS5": tk.IntVar(value=200),
            "V_SMOOTH_FACTOR_FS5": tk.IntVar(value=1000),
            "H_SMOOTH_FACTOR_FS6": tk.IntVar(value=200),
            "V_SMOOTH_FACTOR_FS6": tk.IntVar(value=1000),
            "H_SMOOTH_FACTOR_FS7": tk.IntVar(value=400),
            "V_SMOOTH_FACTOR_FS7": tk.IntVar(value=1000),
        }

        self._build_ui()
        self._setup_logger()
        self.selected_4d_fields = None

        self.vars["do_4d"].trace_add("write", self._on_4d_toggle)
        self.vars["do_alignment"].trace_add("write", self._on_mode_toggle)
        self.vars["do_4d"].trace_add("write", self._on_mode_toggle)

    def _setup_logger(self):
        self.logger = logging.getLogger()
        self.logger.handlers = []
        self.logger.addHandler(TextHandler(self.log_text))

        # Auto-populate token and paths from last used run_params.json on startup
        self._auto_load_last_params()
        self.logger.setLevel(logging.INFO)

    def _build_ui(self):
        canvas = tk.Canvas(self.root, bg="#E0E0E0", highlightthickness=0)
        scrollbar = ttk.Scrollbar(self.root, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        main = ttk.Frame(canvas, padding=12)
        canvas_window = canvas.create_window((0, 0), window=main, anchor="nw")

        def on_frame_configure(event):
            canvas.configure(scrollregion=canvas.bbox("all"))

        def on_canvas_configure(event):
            canvas.itemconfig(canvas_window, width=event.width)

        main.bind("<Configure>", on_frame_configure)
        canvas.bind("<Configure>", on_canvas_configure)

        def on_mousewheel(event):
            canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
        canvas.bind_all("<MouseWheel>", on_mousewheel)

        lf_input = ttk.LabelFrame(main, text="1) Input Source", padding=12)
        lf_input.pack(fill=tk.X, pady=6)

        ttk.Radiobutton(
            lf_input, text="Use Socrata URL", variable=self.vars["use_local"], value=False,
            command=self._toggle_source
        ).grid(row=0, column=0, sticky="w")
        ttk.Radiobutton(
            lf_input, text="Use Local File", variable=self.vars["use_local"], value=True,
            command=self._toggle_source
        ).grid(row=0, column=1, sticky="w", padx=15)

        ttk.Label(lf_input, text="Socrata URL:").grid(row=1, column=0, sticky="e", pady=4)
        self.ent_url = ttk.Entry(lf_input, textvariable=self.vars["input_url"], width=90)
        self.ent_url.grid(row=1, column=1, columnspan=4, sticky="ew", padx=6)

        self.chk_reuse_cache = ttk.Checkbutton(
            lf_input,
            text="Reuse existing socrata_input_extract.csv if found (skip re-download)",
            variable=self.vars["reuse_socrata_cache"]
        )
        self.chk_reuse_cache.grid(row=1, column=5, sticky="w", padx=12)

        ttk.Label(lf_input, text="Local File:").grid(row=2, column=0, sticky="e", pady=4)
        self.ent_local = ttk.Entry(lf_input, textvariable=self.vars["input_local"], width=74, state="disabled")
        self.ent_local.grid(row=2, column=1, columnspan=3, sticky="ew", padx=6)
        self.btn_local = ttk.Button(lf_input, text="Browse", command=self._browse_local, state="disabled")
        self.btn_local.grid(row=2, column=4, padx=4)

        ttk.Label(lf_input, text="State FIPS:").grid(row=3, column=0, sticky="e", pady=4)
        frame_state = ttk.Frame(lf_input)
        frame_state.grid(row=3, column=1, sticky="w")
        self.ent_state = ttk.Entry(frame_state, textvariable=self.vars["state_fips"], width=12)
        self.ent_state.pack(side=tk.LEFT)
        self.btn_load_factors = ttk.Button(
            frame_state, 
            text="Use National Smoothing Factors", 
            command=self._load_national_factors, 
            state="disabled"
        )
        self.btn_load_factors.pack(side=tk.LEFT, padx=4)
        self.vars["state_fips"].trace_add("write", self._on_state_fips_change)

        ttk.Label(lf_input, text="Socrata Token (optional):").grid(row=3, column=2, sticky="e", pady=4)

        ttk.Label(lf_input, text="Socrata Token (optional):").grid(row=3, column=2, sticky="e", pady=4)
        ttk.Entry(lf_input, textvariable=self.vars["socrata_token"], width=35).grid(row=3, column=3, sticky="w")

        ttk.Label(lf_input, text="NBI URL (optional):").grid(row=4, column=0, sticky="e", pady=4)
        ttk.Entry(lf_input, textvariable=self.vars["nbi_url"], width=35).grid(row=4, column=1, sticky="w")

        ttk.Label(lf_input, text="NTI URL (optional):").grid(row=4, column=2, sticky="e", pady=4)
        ttk.Entry(lf_input, textvariable=self.vars["nti_url"], width=35).grid(row=4, column=3, sticky="w")

        lf_fsys = ttk.LabelFrame(lf_input, text="Functional System Filters", padding=8)
        lf_fsys.grid(row=5, column=0, columnspan=5, sticky="ew", pady=8)

        fs_labels = [
            "1: Interstate", "2: PA Freeways", "3: PA Other",
            "4: Minor Arterial", "5: Major Collector", "6: Minor Collector", "7: Local"
        ]
        for i in range(1, 8):
            ttk.Checkbutton(lf_fsys, text=fs_labels[i - 1], variable=self.fsys_vars[i]).grid(
                row=0, column=i - 1, padx=4, sticky="w"
            )

        lf_fac = ttk.LabelFrame(lf_input, text="Facility Type Filters", padding=8)
        lf_fac.grid(row=6, column=0, columnspan=5, sticky="ew", pady=8)

        fac_checks = [
            ("1: One-Way", "fac_1"), ("2: Two-Way", "fac_2"),
            ("4: Ramp", "fac_4"), ("5: Non-Mainline", "fac_5"),
            ("6: Non-Inventory", "fac_6"), ("7: Unbuilt", "fac_7")
        ]
        
        for i, (text, var_name) in enumerate(fac_checks):
            ttk.Checkbutton(lf_fac, text=text, variable=self.vars[var_name]).grid(
                row=0, column=i, padx=4, sticky="w"
            )

        lf_dirs = ttk.LabelFrame(main, text="2) Directories", padding=12)
        lf_dirs.pack(fill=tk.X, pady=6)

        ttk.Label(lf_dirs, text="Output Directory:").grid(row=0, column=0, sticky="e", pady=4)
        ttk.Entry(lf_dirs, textvariable=self.vars["output_dir"], width=85).grid(row=0, column=1, sticky="ew", padx=6)
        ttk.Button(lf_dirs, text="Browse", command=self._browse_out).grid(row=0, column=2)

        ttk.Label(lf_dirs, text="DEM Directory:").grid(row=1, column=0, sticky="e", pady=4)
        ttk.Entry(lf_dirs, textvariable=self.vars["dem_dir"], width=85).grid(row=1, column=1, sticky="ew", padx=6)
        ttk.Button(lf_dirs, text="Browse", command=self._browse_dem).grid(row=1, column=2)

        lf_modes = ttk.LabelFrame(main, text="3) Output / Mode Selection", padding=12)
        lf_modes.pack(fill=tk.X, pady=6)

        ttk.Checkbutton(lf_modes, text="Run Alignment", variable=self.vars["do_alignment"]).grid(row=0, column=0, sticky="w")
        ttk.Checkbutton(lf_modes, text="Run Plan/Profile", variable=self.vars["do_plan_profile"]).grid(row=0, column=1, sticky="w", padx=15)
        ttk.Checkbutton(lf_modes, text="Run 4D Enrichment", variable=self.vars["do_4d"]).grid(row=0, column=2, sticky="w", padx=15)
        self.btn_4d_fields = ttk.Button(
            lf_modes,
            text="Customize Output Fields...",
            command=self._open_4d_field_selector,
            state="disabled",
        )
        self.btn_4d_fields.grid(row=0, column=3, sticky="w", padx=8)
        self.lbl_4d_fields = ttk.Label(lf_modes, text="", foreground="#555555", font=("Arial", 8, "italic"))
        self.lbl_4d_fields.grid(row=0, column=4, sticky="w")

        self.chk_replace_curves = ttk.Checkbutton(
            lf_modes,
            text="Replace HPMS curve/grade classifications with RAT-derived values",
            variable=self.vars["replace_curve_grade"],
            state="disabled",
        )
        self.chk_replace_curves.grid(row=1, column=0, columnspan=5, sticky="w", pady=(6, 0))

        ttk.Label(lf_modes, text="Plan/Profile Route ID:").grid(row=2, column=0, sticky="e", pady=4)
        self.route_combo = ttk.Combobox(lf_modes, textvariable=self.vars["pp_route_id"], width=30, state="readonly")
        self.route_combo.grid(row=2, column=1, sticky="w", padx=6)
        ttk.Button(lf_modes, text="Load Routes", command=self._load_routes_thread).grid(row=2, column=2, sticky="w")

        ttk.Label(lf_modes, text="Begin RP (mi):").grid(row=3, column=0, sticky="e", pady=4)
        ttk.Entry(lf_modes, textvariable=self.vars["pp_start_rp"], width=12).grid(row=3, column=1, sticky="w")
        ttk.Label(lf_modes, text="End RP (mi):").grid(row=3, column=2, sticky="e", pady=4)
        ttk.Entry(lf_modes, textvariable=self.vars["pp_end_rp"], width=12).grid(row=3, column=3, sticky="w")

        lf_param_workspace = ttk.LabelFrame(main, text="4) Parameters", padding=12)
        lf_param_workspace.pack(fill=tk.X, pady=6)
        lf_param_workspace.columnconfigure(0, weight=1)

        left = ttk.Frame(lf_param_workspace)
        left.grid(row=0, column=0, sticky="ew")
        left.columnconfigure(0, weight=1)
        left.columnconfigure(1, weight=1)
        left.columnconfigure(2, weight=1)

        grp_h = ttk.LabelFrame(left, text="Horizontal", padding=10)
        grp_h.grid(row=0, column=0, sticky="nsew", padx=(0, 6))
        ttk.Label(grp_h, text="Densify Spacing (ft):").grid(row=0, column=0, sticky="e", pady=2)
        ttk.Entry(grp_h, textvariable=self.vars["densify_spacing_ft"], width=12).grid(row=0, column=1, sticky="w")
        ttk.Label(grp_h, text="H Smooth Factor (FS 1)(ft):").grid(row=1, column=0, sticky="e", pady=2)
        ttk.Entry(grp_h, textvariable=self.vars["h_smooth_factor"], width=12).grid(row=1, column=1, sticky="w")
        ttk.Label(grp_h, text="H Min Delta (deg):").grid(row=2, column=0, sticky="e", pady=2)
        ttk.Entry(grp_h, textvariable=self.vars["h_min_delta"], width=12).grid(row=2, column=1, sticky="w")
        ttk.Label(grp_h, text="H Min Curve Length (ft):").grid(row=3, column=0, sticky="e", pady=2)
        ttk.Entry(grp_h, textvariable=self.vars["h_min_curve_length_ft"], width=12).grid(row=3, column=1, sticky="w")

        grp_v = ttk.LabelFrame(left, text="Vertical", padding=10)
        grp_v.grid(row=0, column=1, sticky="nsew", padx=6)
        ttk.Label(grp_v, text="V Smooth Factor (FS 1)(ft):").grid(row=0, column=0, sticky="e", pady=2)
        ttk.Entry(grp_v, textvariable=self.vars["v_smooth_factor"], width=12).grid(row=0, column=1, sticky="w")
        ttk.Label(grp_v, text="V Min Curve Length (ft):").grid(row=1, column=0, sticky="e", pady=2)
        ttk.Entry(grp_v, textvariable=self.vars["v_min_curve_length_ft"], width=12).grid(row=1, column=1, sticky="w")
        ttk.Label(grp_v, text="V Min Grade Change (%):").grid(row=2, column=0, sticky="e", pady=2)
        ttk.Entry(grp_v, textvariable=self.vars["v_min_grade_change"], width=12).grid(row=2, column=1, sticky="w")

        grp_runtime = ttk.LabelFrame(left, text="Merge & Runtime", padding=10)
        grp_runtime.grid(row=0, column=2, sticky="nsew", padx=(6, 0))
        ttk.Checkbutton(grp_runtime, text="Enable Merging", variable=self.vars["enable_merge"]).grid(row=0, column=0, columnspan=2, sticky="w")
        ttk.Label(grp_runtime, text="H Merge Gap (ft):").grid(row=1, column=0, sticky="e", pady=2)
        ttk.Entry(grp_runtime, textvariable=self.vars["merge_gap_ft"], width=12).grid(row=1, column=1, sticky="w")
        ttk.Label(grp_runtime, text="V Merge Gap (ft):").grid(row=2, column=0, sticky="e", pady=2)
        ttk.Entry(grp_runtime, textvariable=self.vars["v_merge_gap_ft"], width=12).grid(row=2, column=1, sticky="w")
        ttk.Checkbutton(grp_runtime, text="Run validation automatically", variable=self.vars["auto_validate"]).grid(row=3, column=0, columnspan=2, sticky="w")
        ttk.Checkbutton(grp_runtime, text="Open output folder when complete", variable=self.vars["open_output_when_done"]).grid(row=4, column=0, columnspan=2, sticky="w")
        ttk.Checkbutton(grp_runtime, text="Trim Curve Endpoints", variable=self.vars["trim_curve_endpoints"]).grid(row=5, column=0, columnspan=2, sticky="w")
        
       
        lf_out = ttk.LabelFrame(main, text="5) Output Formats", padding=10)
        lf_out.pack(fill=tk.X, pady=6)
        ttk.Checkbutton(lf_out, text="CSV", variable=self.vars["out_csv"]).grid(row=0, column=0, sticky="w", padx=8)
        ttk.Checkbutton(lf_out, text="GeoJSON", variable=self.vars["out_geojson"]).grid(row=0, column=1, sticky="w", padx=8)
        ttk.Checkbutton(lf_out, text="GeoPackage (GPKG)", variable=self.vars["out_gpkg"]).grid(row=0, column=2, sticky="w", padx=8)
        ttk.Checkbutton(lf_out, text="Shapefile", variable=self.vars["out_shp"]).grid(row=0, column=3, sticky="w", padx=8)
        ttk.Checkbutton(lf_out, text="Interactive HTML Map", variable=self.vars["out_html_map"]).grid(row=1, column=0, sticky="w", padx=8)
        ttk.Checkbutton(lf_out, text="Summary Dashboard HTML", variable=self.vars["out_dashboard"]).grid(row=1, column=1, sticky="w", padx=8)
        ttk.Checkbutton(lf_out, text="QA Exceptions CSV", variable=self.vars["out_qa_exceptions"]).grid(row=1, column=2, sticky="w", padx=8)
        ttk.Checkbutton(lf_out, text="Plan/Profile PDF", variable=self.vars["out_pdf"]).grid(row=1, column=3, sticky="w", padx=8)
        ttk.Checkbutton(lf_out, text="Vertices Spatial File (large)", variable=self.vars["out_vtx_spatial"]).grid(row=2, column=2, sticky="w", padx=8)
        ttk.Checkbutton(lf_out, text="Simplify Web Geometry (Uncheck for raw GIS fidelity)", variable=self.vars["simplify_geometry"]).grid(row=2, column=0, columnspan=2, sticky="w", padx=8, pady=(8,0))

        btn_frame = ttk.Frame(main)
        btn_frame.pack(fill=tk.X, pady=8)
       
        ttk.Button(btn_frame, text="Advanced Settings...", command=self._open_advanced_settings).pack(side=tk.LEFT, padx=4)
        ttk.Button(btn_frame, text="Restore Defaults", command=self._restore_main_defaults).pack(side=tk.LEFT, padx=4)
       
        self.btn_run = ttk.Button(btn_frame, text="Run Selected", command=self._run_thread)
        self.btn_run.pack(side=tk.RIGHT, padx=4)
       
        ttk.Button(btn_frame, text="Save Config", command=self._save_config).pack(side=tk.RIGHT, padx=4)
        ttk.Button(btn_frame, text="Load Config", command=self._load_config).pack(side=tk.RIGHT, padx=4)

        lf_log = ttk.LabelFrame(main, text="Run Log", padding=8)
        lf_log.pack(fill=tk.BOTH, expand=True, pady=6)

        log_frame = tk.Frame(lf_log)
        log_frame.pack(fill=tk.BOTH, expand=True)

        log_scroll = ttk.Scrollbar(log_frame, orient=tk.VERTICAL)
        log_scroll.pack(side=tk.RIGHT, fill=tk.Y)

        self.log_text = tk.Text(log_frame, height=14, state="disabled",
                                yscrollcommand=log_scroll.set)
        self.log_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        log_scroll.config(command=self.log_text.yview)

        # When cursor enters log widget, capture mousewheel for log scrolling only
        def _log_mousewheel(event):
            self.log_text.yview_scroll(int(-1 * (event.delta / 120)), "units")
            return "break"  # prevent event from propagating to main canvas

        def _bind_log_scroll(event):
            self.log_text.bind("<MouseWheel>", _log_mousewheel)

        def _unbind_log_scroll(event):
            self.log_text.unbind("<MouseWheel>")

        self.log_text.bind("<Enter>", _bind_log_scroll)
        self.log_text.bind("<Leave>", _unbind_log_scroll)

    def _apply_params_dict(self, data: dict):
        """
        Restore GUI state from a loaded params dict. Used by both the
        silent auto-load-on-startup path and the explicit "Load Config"
        button, so there is exactly one place that knows how to map every
        saved field back onto a GUI control.

        Previously these were two separate, drifting implementations:
        auto-load only restored four hardcoded fields (token, output dir,
        DEM dir, input URL), and Load Config's more general var_map-based
        restoration had explicit handling for FSYSTEM_FILTER but never for
        FACILITY_TYPE_FILTER -- so Facility Type silently failed to
        restore either way a params file got loaded.
        """
        if data.get("OUTPUT_DIR"):
            self.vars["output_dir"].set(data["OUTPUT_DIR"])
        if data.get("DEM_DIR"):
            self.vars["dem_dir"].set(data["DEM_DIR"])

        var_map = {k.upper(): k for k in self.vars.keys()}

        for k, v in data.items():
            if v is None:
                v = ""

            if k in ("OUTPUT_DIR", "DEM_DIR"):
                continue  # already handled above
            elif k in self.advanced_defaults:
                self.advanced_defaults[k].set(str(v))
            elif k == "FSYSTEM_FILTER":
                allowed_fsys = set(int(val) for val in (v or []))
                for i in range(1, 8):
                    self.fsys_vars[i].set(i in allowed_fsys)
            elif k == "FACILITY_TYPE_FILTER":
                allowed_facs = set(v or [])
                for fac_val, var_name in [(1, "fac_1"), (2, "fac_2"), (4, "fac_4"), (5, "fac_5"), (6, "fac_6"), (7, "fac_7")]:
                    self.vars[var_name].set(fac_val in allowed_facs)
            elif k == "INCLUDE_HPMS_FIELDS":
                self.selected_4d_fields = v if v != "" else None
                if self.selected_4d_fields is not None:
                    n = len(self.selected_4d_fields)
                    total = len(HPMS_FIELD_CATALOG)
                    if n == total:
                        self.lbl_4d_fields.configure(text="All fields selected")
                    else:
                        self.lbl_4d_fields.configure(text=f"{n} of {total} fields selected")
                else:
                    self.lbl_4d_fields.configure(text="")
            elif k in var_map:
                try:
                    self.vars[var_map[k]].set(v)
                except Exception:
                    pass

        self._toggle_source()

    def _auto_load_last_params(self):
        """On startup, silently restore the full GUI state from the most
        recent run_params.json if one exists in the output directory."""
        # Check common locations for a previous run_params.json
        search_paths = []
        # Same directory as the GUI script
        gui_dir = os.path.dirname(os.path.abspath(__file__))
        search_paths.append(os.path.join(gui_dir, "..", "output", "run_params.json"))
        search_paths.append(os.path.join(gui_dir, "output", "run_params.json"))

        for candidate in search_paths:
            candidate = os.path.normpath(candidate)
            if os.path.exists(candidate):
                try:
                    with open(candidate, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    self._apply_params_dict(data)
                    logging.info(f"Auto-loaded settings from: {candidate}")
                    break
                except Exception:
                    pass  # Silent fail -- not critical

    def _toggle_source(self):
        use_local = self.vars["use_local"].get()
        self.ent_local.configure(state="normal" if use_local else "disabled")
        self.btn_local.configure(state="normal" if use_local else "disabled")
        self.ent_url.configure(state="disabled" if use_local else "normal")
        self.chk_reuse_cache.configure(state="disabled" if use_local else "normal")

    def _on_state_fips_change(self, *_):
        fips = self.vars["state_fips"].get().strip()
        if len(fips) > 0 and fips != "00" and fips.upper() != "ALL":
            self.btn_load_factors.configure(state="normal")
        else:
            self.btn_load_factors.configure(state="disabled")

    def _load_national_factors(self):
        fips = self.vars["state_fips"].get().strip().zfill(2)
        suite_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        json_path = os.path.join(suite_root, "core", "national_smoothing_factors.json")
        if os.path.exists(json_path):
            try:
                with open(json_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if fips in data:
                    factors = data[fips]
                    # Update main variables
                    if "H_SMOOTH_FACTOR" in factors and factors["H_SMOOTH_FACTOR"] is not None:
                        self.vars["h_smooth_factor"].set(str(factors["H_SMOOTH_FACTOR"]))
                    if "V_SMOOTH_FACTOR" in factors and factors["V_SMOOTH_FACTOR"] is not None:
                        self.vars["v_smooth_factor"].set(str(factors["V_SMOOTH_FACTOR"]))
                    
                    # Update advanced variables (FS2-FS7 overrides)
                    for k, v in factors.items():
                        if k in self.advanced_defaults and v is not None:
                            self.advanced_defaults[k].set(str(v))
                            
                    messagebox.showinfo("Loaded", f"Successfully loaded national smoothing factors for State {fips}.")
                else:
                    messagebox.showwarning("Not Found", f"No factors found for State FIPS {fips} in configuration file.")
            except Exception as e:
                messagebox.showerror("Error", f"Failed to load smoothing factors: {e}")
        else:
            messagebox.showerror("File Not Found", "national_smoothing_factors.json not found in the core directory.")
    
    def _on_4d_toggle(self, *_):
        if self.vars["do_4d"].get():
            self.btn_4d_fields.configure(state="normal")
        else:
            self.btn_4d_fields.configure(state="disabled")
            self.lbl_4d_fields.configure(text="")

    def _on_mode_toggle(self, *_):
        both = self.vars["do_alignment"].get() and self.vars["do_4d"].get()
        self.chk_replace_curves.configure(state="normal" if both else "disabled")
        if not both:
            self.vars["replace_curve_grade"].set(False)

    def _open_4d_field_selector(self):
        dlg = HMPSFieldSelectorDialog(self.root, self.selected_4d_fields)
        self.root.wait_window(dlg.win)
        if dlg.result is not None:
            self.selected_4d_fields = dlg.result
            n = len(self.selected_4d_fields)
            total = len(HPMS_FIELD_CATALOG)
            if n == total:
                self.lbl_4d_fields.configure(text="All fields selected")
            else:
                self.lbl_4d_fields.configure(text=f"{n} of {total} fields selected")
            logging.info(f"4D output field selection updated: {n} fields selected.")

    def _browse_local(self):
        p = filedialog.askopenfilename(filetypes=[("GIS/CSV", "*.shp *.geojson *.csv"), ("All Files", "*.*")])
        if p:
            self.vars["input_local"].set(p)

    def _browse_out(self):
        p = filedialog.askdirectory()
        if p:
            self.vars["output_dir"].set(p)

    def _browse_dem(self):
        p = filedialog.askdirectory()
        if p:
            self.vars["dem_dir"].set(p)

    def _open_output_folder(self, out_dir):
        try:
            if os.name == "nt":
                os.startfile(out_dir)
            elif sys.platform == "darwin":
                subprocess.run(["open", out_dir], check=False)
            else:
                subprocess.run(["xdg-open", out_dir], check=False)
        except Exception as e:
            logging.warning(f"Could not open output folder: {e}")

    def _set_status(self, phase, detail=None, busy=False):
        def update():
            if not hasattr(self, "status_phase") or not hasattr(self, "status_detail") or not hasattr(self, "status_progress"):
                return
            self.status_phase.set(phase)
            if detail is not None:
                self.status_detail.set(detail)
            if busy:
                self.status_progress.start(10)
            else:
                self.status_progress.stop()
        self.root.after(0, update)

    def _open_advanced_settings(self):
        win = tk.Toplevel(self.root)
        win.title("Advanced Settings")
        win.geometry("850x650")
        win.transient(self.root)
        win.grab_set()

        outer = ttk.Frame(win, padding=10)
        outer.pack(fill=tk.BOTH, expand=True)

        ttk.Label(
            outer,
            text="Edit advanced analysis defaults (saved into run_params.json when you run/save config).",
            font=("Arial", 10, "italic")
        ).pack(anchor="w", pady=(0, 8))

        canvas = tk.Canvas(outer, highlightthickness=0)
        scrollbar = ttk.Scrollbar(outer, orient="vertical", command=canvas.yview)
        form = ttk.Frame(canvas)

        form.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=form, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        adv_h_keys = [
            "H_BASE_SMOOTH_WINDOW", "H_MIN_HEAD_CHANGE", "H_MAX_RADIUS_FT",
            "H_SMOOTH_FACTOR_FS2", "H_SMOOTH_FACTOR_FS3", "H_SMOOTH_FACTOR_FS4", 
            "H_SMOOTH_FACTOR_FS5", "H_SMOOTH_FACTOR_FS6", "H_SMOOTH_FACTOR_FS7"
        ]
        
        adv_v_keys = [
            "V_VC_THRESHOLD", "V_GAP_TOLERANCE", "V_MIN_OFFSET_FT", "V_REVERSAL_TOLERANCE",
            "REGRESSION_WINDOW_FT", "TREND_WINDOW_FT", "DIP_THRESHOLD_FT", "BRIDGE_MAX_LEN_FT",
            "V_SMOOTH_FACTOR_FS2", "V_SMOOTH_FACTOR_FS3", "V_SMOOTH_FACTOR_FS4", 
            "V_SMOOTH_FACTOR_FS5", "V_SMOOTH_FACTOR_FS6", "V_SMOOTH_FACTOR_FS7"
        ]

        grp_h = ttk.LabelFrame(form, text="Advanced Horizontal", padding=10)
        grp_h.grid(row=0, column=0, sticky="nsew", padx=(0, 8), pady=(0, 8))
        grp_v = ttk.LabelFrame(form, text="Advanced Vertical / Analysis", padding=10)
        grp_v.grid(row=0, column=1, sticky="nsew", padx=(8, 0), pady=(0, 8))

        for r, k in enumerate(adv_h_keys):
            ttk.Label(grp_h, text=k).grid(row=r, column=0, sticky="e", padx=6, pady=3)
            ttk.Entry(grp_h, textvariable=self.advanced_defaults[k], width=16).grid(row=r, column=1, sticky="w", padx=6, pady=3)

        for r, k in enumerate(adv_v_keys):
            ttk.Label(grp_v, text=k).grid(row=r, column=0, sticky="e", padx=6, pady=3)
            ttk.Entry(grp_v, textvariable=self.advanced_defaults[k], width=16).grid(row=r, column=1, sticky="w", padx=6, pady=3)

        btns = ttk.Frame(outer)
        btns.pack(fill=tk.X, pady=(10, 0))
        ttk.Button(btns, text="Restore Defaults", command=self._restore_advanced_defaults).pack(side=tk.LEFT)
        ttk.Button(btns, text="Close", command=win.destroy).pack(side=tk.RIGHT)

    def _collect_params_json(self, out_dir):
        fsys = [k for k, v in self.fsys_vars.items() if v.get()]
        allowed_facs = []
        for fac_val, var_name in [(1, "fac_1"), (2, "fac_2"), (4, "fac_4"), (5, "fac_5"), (6, "fac_6"), (7, "fac_7")]:
            if self.vars[var_name].get():
                allowed_facs.append(fac_val)
                
        state_fips_input = self.vars["state_fips"].get().strip().zfill(2)
        
        def safe_float(val_str, default=0.0):
            v = str(val_str).strip()
            if not v or v.lower() == "none": return default
            try: return float(v)
            except: return default

        data = {
            "DENSIFY_SPACING_FT": safe_float(self.vars["densify_spacing_ft"].get()),
            "H_SMOOTH_FACTOR": int(safe_float(self.vars["h_smooth_factor"].get())),
            "V_SMOOTH_FACTOR": int(safe_float(self.vars["v_smooth_factor"].get())),
            "H_MIN_DELTA": safe_float(self.vars["h_min_delta"].get()),
            "H_MIN_CURVE_LENGTH_FT": safe_float(self.vars["h_min_curve_length_ft"].get()),
            "V_MIN_CURVE_LENGTH_FT": safe_float(self.vars["v_min_curve_length_ft"].get()),
            "V_MIN_GRADE_CHANGE": safe_float(self.vars["v_min_grade_change"].get()),

            "ENABLE_MERGE": bool(self.vars["enable_merge"].get()),
            "MERGE_GAP_FT": safe_float(self.vars["merge_gap_ft"].get()),
            "V_MERGE_GAP_FT": safe_float(self.vars["v_merge_gap_ft"].get()),
            "TRIM_CURVE_ENDPOINTS": bool(self.vars["trim_curve_endpoints"].get()),

            "FSYSTEM_FILTER": fsys,
            "FACILITY_TYPE_FILTER": allowed_facs,
            "STATE_FIPS": state_fips_input,
            "SOCRATA_TOKEN": self.vars["socrata_token"].get().strip(),
            "INPUT_URL": self.vars["input_url"].get().strip(),
            "NBI_URL": self.vars["nbi_url"].get().strip(),
            "NTI_URL": self.vars["nti_url"].get().strip(),

            "DO_ALIGNMENT": bool(self.vars["do_alignment"].get()),
            "DO_PLAN_PROFILE": bool(self.vars["do_plan_profile"].get()),
            "DO_4D": bool(self.vars["do_4d"].get()),
            
            "OUTPUT_DIR": out_dir,
            "DEM_DIR": self.vars["dem_dir"].get().strip(),

            "OUT_CSV": bool(self.vars["out_csv"].get()),
            "OUT_GEOJSON": bool(self.vars["out_geojson"].get()),
            "OUT_GPKG": bool(self.vars["out_gpkg"].get()),
            "OUT_SHP": bool(self.vars["out_shp"].get()),
            "OUT_HTML_MAP": bool(self.vars["out_html_map"].get()),
            "OUT_DASHBOARD": bool(self.vars["out_dashboard"].get()),
            "OUT_QA_EXCEPTIONS": bool(self.vars["out_qa_exceptions"].get()),
            "OUT_PDF": bool(self.vars["out_pdf"].get()),
            "OUT_VTX_SPATIAL": bool(self.vars["out_vtx_spatial"].get()),
            "SIMPLIFY_GEOMETRY": bool(self.vars["simplify_geometry"].get()),
            "INCLUDE_HPMS_FIELDS": self.selected_4d_fields,
            "REPLACE_CURVE_GRADE": bool(self.vars["replace_curve_grade"].get()),
        }

        data["PP_ROUTE_ID"] = self.vars["pp_route_id"].get().strip()
        start_val = self.vars["pp_start_rp"].get().strip()
        end_val = self.vars["pp_end_rp"].get().strip()
        
        data["PP_START_RP"] = float(start_val) if start_val and start_val.lower() != "none" else None
        data["PP_END_RP"] = float(end_val) if end_val and end_val.lower() != "none" else None

        for k, var in self.advanced_defaults.items():
            val = str(var.get()).strip()
            try:
                if val.lower() == "none" or not val:
                    continue
                if "." in val or "e" in val.lower():
                    data[k] = float(val)
                else:
                    data[k] = int(val)
            except Exception:
                data[k] = val

        suite_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

        is_national_run = (not state_fips_input or state_fips_input == "00" or state_fips_input.upper() == "ALL")
        data["IGNORE_GUI_SMOOTHING_FACTORS"] = is_national_run

        params_path = os.path.join(out_dir, "run_params.json")
        with open(params_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        return params_path, data

    def _save_config(self):
        out_dir = self.vars["output_dir"].get().strip()
        if not out_dir:
            messagebox.showwarning("Missing Output", "Please set output directory first.")
            return
        os.makedirs(out_dir, exist_ok=True)
        p, _ = self._collect_params_json(out_dir)
        messagebox.showinfo("Saved", f"Config saved:\n{p}")

    def _load_config(self):
        p = filedialog.askopenfilename(
            title="Select Configuration File",
            filetypes=[("JSON Config", "*.json"), ("All Files", "*.*")]
        )
        if not p:
            return

        try:
            with open(p, "r", encoding="utf-8") as f:
                data = json.load(f)
            self._apply_params_dict(data)

            logging.info(f"Successfully loaded configuration from: {p}")
            messagebox.showinfo("Loaded", "Configuration successfully loaded!")

        except Exception as e:
            logging.error(f"Failed to load config: {e}")
            messagebox.showerror("Load Error", f"Failed to read the configuration file:\n{e}")

    def _restore_main_defaults(self):
        defaults = {
            "densify_spacing_ft":    "5",
            "h_smooth_factor":       "400",
            "v_smooth_factor":       "1400",
            "h_min_delta":           "3.5",
            "h_min_curve_length_ft": "100",
            "v_min_curve_length_ft": "200",
            "v_min_grade_change":    "0.5",
            "enable_merge":          False,
            "merge_gap_ft":          "600",
            "v_merge_gap_ft":        "1500",
            "trim_curve_endpoints":  True,
        }
        for k, v in defaults.items():
            self.vars[k].set(v)
        logging.info("Main parameters restored to default.")

    def _restore_advanced_defaults(self):
        adv_defaults = {
            "H_BASE_SMOOTH_WINDOW":  "21",
            "H_MIN_HEAD_CHANGE":     "0.003",
            "H_MAX_RADIUS_FT":       "165000",
            "V_VC_THRESHOLD":        "0.002",
            "V_GAP_TOLERANCE":       "5",
            "V_MIN_OFFSET_FT":       "0.10",
            "V_REVERSAL_TOLERANCE":  "0.02",
            "REGRESSION_WINDOW_FT":  "500",
            "TREND_WINDOW_FT":       "1000", 
            "DIP_THRESHOLD_FT":      "6.5",
            "BRIDGE_MAX_LEN_FT":     "8200",
            "H_SMOOTH_FACTOR_FS2":   "200",
            "V_SMOOTH_FACTOR_FS2":   "1400",
            "H_SMOOTH_FACTOR_FS3":   "400",
            "V_SMOOTH_FACTOR_FS3":   "1400",
            "H_SMOOTH_FACTOR_FS4":   "400",
            "V_SMOOTH_FACTOR_FS4":   "1400",
            "H_SMOOTH_FACTOR_FS5":   "200",
            "V_SMOOTH_FACTOR_FS5":   "1000",
            "H_SMOOTH_FACTOR_FS6":   "200",
            "V_SMOOTH_FACTOR_FS6":   "1000",
            "H_SMOOTH_FACTOR_FS7":   "400",
            "V_SMOOTH_FACTOR_FS7":   "1000",
        }
        for k, v in adv_defaults.items():
            if k in self.advanced_defaults:
                self.advanced_defaults[k].set(v)
        logging.info("Advanced parameters restored to default.")

    def _load_routes_thread(self):
        threading.Thread(target=self._load_routes, daemon=True).start()

    def _load_routes(self):
        try:
            if self.vars["use_local"].get():
                local = self.vars["input_local"].get().strip()
                if not local or not os.path.exists(local):
                    logging.error("Select a valid local file first.")
                    return
                if local.lower().endswith(".csv"):
                    df = pd.read_csv(local, low_memory=False)
                else:
                    gdf = gpd.read_file(local)
                    df = pd.DataFrame(gdf.drop(columns="geometry", errors="ignore"))
                col = None
                for c in df.columns:
                    if c.lower() in ["routeid", "route_id", "route", "id"]:
                        col = c
                        break
                if not col:
                    logging.error("Route column not found in local file.")
                    return
                routes = sorted(df[col].dropna().astype(str).unique().tolist())
            else:
                url = self.vars["input_url"].get().strip() or SOCRATA_DEFAULT
                state = self.vars["state_fips"].get().strip()
                token = self.vars["socrata_token"].get().strip()
                headers = {"X-App-Token": token} if token else {}
                where = f"stateid='{state}'" if state else ""
                params = {"$select": "distinct route_id", "$order": "route_id", "$limit": 100000}
                if where:
                    params["$where"] = where
               
                import time

                max_attempts = 6
                r = None
                for attempt in range(1, max_attempts + 1):
                    try:
                        r = requests.get(url, params=params, headers=headers, timeout=120)
                        if r.status_code in (429, 500, 502, 503, 504):
                            wait_s = min(60, 2 ** attempt)
                            logging.warning(f"Socrata temporary error {r.status_code}. Attempt {attempt}/{max_attempts}. Retrying in {wait_s}s...")
                            time.sleep(wait_s)
                            continue
                        r.raise_for_status()
                        break
                    except requests.RequestException as ex:
                        if attempt == max_attempts:
                            raise
                        wait_s = min(60, 2 ** attempt)
                        logging.warning(f"Socrata request failed ({ex}). Attempt {attempt}/{max_attempts}. Retrying in {wait_s}s...")
                        time.sleep(wait_s)

                if r is None:
                    raise RuntimeError("Failed to get Socrata response.")
               
                try:
                    data = r.json()
                except Exception as parse_err:
                    logging.error(f"Socrata returned malformed JSON while loading routes: {parse_err}")
                    return
                routes = sorted([d["route_id"] for d in data if "route_id" in d])

            self.route_combo["values"] = routes
            if routes:
                self.route_combo.current(0)
            logging.info(f"Loaded {len(routes)} routes.")
        except Exception as e:
            logging.error(f"Load routes failed: {e}")

    def _run_thread(self):
        self.btn_run.configure(state="disabled")
        threading.Thread(target=self._run, daemon=True).start()

    def _run_validator(self, out_dir):
        # rat_unified_gui.py lives in apps/, but rat_results_validator.py
        # lives in tools/ alongside the calibration and diagnostics scripts
        # -- not apps/ alongside the GUI itself. Check tools/ first (the
        # correct, documented location), then fall back to apps/ and the
        # suite root in case of an unusual local layout, rather than
        # failing immediately on the first guess.
        script_dir = os.path.dirname(os.path.abspath(__file__))
        suite_root = os.path.dirname(script_dir)

        candidate_dirs = [
            os.path.join(suite_root, "tools"),
            script_dir,
            suite_root,
        ]
        validator_script = None
        for candidate_dir in candidate_dirs:
            candidate_path = os.path.join(candidate_dir, "rat_results_validator.py")
            if os.path.exists(candidate_path):
                validator_script = candidate_path
                break

        if validator_script is None:
            checked = ", ".join(os.path.join(d, "rat_results_validator.py") for d in candidate_dirs)
            return 1, f"Validator script not found. Checked: {checked}"

        h_files, v_files, e_files, vtx_files, score_files = [], [], [], [], []
        
        for root_path, _, files in os.walk(out_dir):
            for f in files:
                if f.startswith("alignment_horizontal_") and f.endswith(".csv"):
                    h_files.append(os.path.join(root_path, f))
                elif f.startswith("alignment_vertical_") and f.endswith(".csv"):
                    v_files.append(os.path.join(root_path, f))
                elif f.startswith("hpms_4d_production_") and f.endswith(".csv"):
                    e_files.append(os.path.join(root_path, f))
                elif f.startswith("alignment_vertices_") and f.endswith(".csv"):
                    vtx_files.append(os.path.join(root_path, f))
                elif f.startswith("alignment_section_scores_") and f.endswith(".csv"):
                    score_files.append(os.path.join(root_path, f))

        cmd = [sys.executable, validator_script]
        if h_files: cmd += ["--horizontal_csv", sorted(h_files)[-1]]
        if v_files: cmd += ["--vertical_csv", sorted(v_files)[-1]]
        if e_files: cmd += ["--enriched_csv", sorted(e_files)[-1]]
        if vtx_files: cmd += ["--vertices_csv", sorted(vtx_files)[-1]]
        if score_files: cmd += ["--scores_csv", sorted(score_files)[-1]]

        if len(cmd) == 2:  
            return 1, "No alignment or enrichment outputs found for validation."
 
        p = subprocess.run(cmd, capture_output=True, text=True, env=os.environ.copy())
        msg = (p.stdout or "") + ("\n" + p.stderr if p.stderr else "")
        return p.returncode, msg.strip()

    def _latest_plan_profile_outputs(self, out_dir, route_id):
        import re
        safe_route = re.sub(r'[<>:"/\\|?*]+', "-", str(route_id)).strip().upper()
        base = f"plan_profile_{safe_route}"

        vtx = sorted(glob.glob(os.path.join(out_dir, f"{base}*_vertices.csv")), key=os.path.getmtime)
        hor = sorted(glob.glob(os.path.join(out_dir, f"{base}*_horizontal.csv")), key=os.path.getmtime)
        ver = sorted(glob.glob(os.path.join(out_dir, f"{base}*_vertical.csv")), key=os.path.getmtime)

        if not vtx or not hor or not ver:
            return None, None, None
        return vtx[-1], hor[-1], ver[-1]

    def _run(self):
        try:
            self._set_status("Starting", "Validating inputs and preparing run...", busy=True)

            out_dir = self.vars["output_dir"].get().strip()
            dem_dir = self.vars["dem_dir"].get().strip()

            if not out_dir or not dem_dir:
                self._set_status("Error", "Missing Output or DEM directory.", busy=False)
                messagebox.showerror("Missing Paths", "Please set Output and DEM directories.")
                return

            os.makedirs(out_dir, exist_ok=True)
            os.makedirs(dem_dir, exist_ok=True)

            params_json, run_params = self._collect_params_json(out_dir)
            logging.info(f"Params saved: {params_json}")

            script_dir = os.path.dirname(os.path.abspath(__file__))
            alignment_script = os.path.join(script_dir, "rat_alignment_cli.py")
            pp_script = os.path.join(script_dir, "rat_plan_profile_cli.py")
            enrich_script = os.path.join(script_dir, "hpms_4d_enricher_cli.py")
            simple_pdf_script = os.path.join(script_dir, "rat_plan_profile_report_pdf.py")

            for pth in [alignment_script, pp_script, enrich_script]:
                if not os.path.exists(pth):
                    logging.warning(f"Optional script not found: {pth}")

            use_local = self.vars["use_local"].get()
            input_path = None

            self._set_status("Input", "Preparing source data...", busy=True)

            if use_local:
                input_path = self.vars["input_local"].get().strip()
                if not input_path:
                    self._set_status("Error", "Local input file was not selected.", busy=False)
                    messagebox.showerror("Missing Input", "Please select local input file.")
                    return
                if not os.path.exists(input_path):
                    self._set_status("Error", f"Local input file not found: {input_path}", busy=False)
                    messagebox.showerror("Input Not Found", f"File not found:\n{input_path}")
                    return
                logging.info(f"Using local input: {input_path}")
            else:
                input_path = os.path.join(out_dir, "socrata_input_extract.csv")
                use_cached = (
                    self.vars.get("reuse_socrata_cache") is not None
                    and self.vars["reuse_socrata_cache"].get()
                    and os.path.exists(input_path)
                )

                if use_cached:
                    cache_size_mb = os.path.getsize(input_path) / (1024 ** 2)
                    self._set_status("Input", "Reusing cached Socrata extract...", busy=True)
                    logging.info(
                        f"Reuse Socrata cache enabled -- skipping download. "
                        f"Using existing extract: {input_path} ({cache_size_mb:,.0f} MB)"
                    )
                else:
                    self._set_status("Input", "Downloading data from Socrata...", busy=True)
                    logging.info("Downloading Socrata data with selected filters...")

                    url = self.vars["input_url"].get().strip() or SOCRATA_DEFAULT
                    state = self.vars["state_fips"].get().strip()
                    token = self.vars["socrata_token"].get().strip()
                    facility_filter = run_params.get("FACILITY_TYPE_FILTER")
                    fsystem_filter = run_params.get("FSYSTEM_FILTER")

                    def _progress(rows_so_far):
                        self._set_status("Input", f"Fetching Socrata rows... currently {rows_so_far:,}", busy=True)

                    try:
                        df = fetch_socrata_state(
                            state,
                            token=token,
                            facility_type_filter=facility_filter,
                            fsystem_filter=fsystem_filter,
                            url=url,
                            progress_callback=_progress,
                        )
                    except ValueError:
                        self._set_status("Error", "No records returned from Socrata with current filters.", busy=False)
                        messagebox.showerror("No Data", "No records returned from Socrata with current filters.")
                        return
                    except Exception as ex:
                        self._set_status("Error", f"Socrata download failed: {ex}", busy=False)
                        messagebox.showerror("Download Failed", f"Socrata download failed:\n{ex}")
                        return

                    df.to_csv(input_path, index=False)
                    logging.info(f"Socrata extract saved: {input_path} ({len(df):,} rows)")
                    self._set_status("Input", f"Socrata extract ready ({len(df):,} rows).", busy=True)

            run_any = False

            if self.vars["do_alignment"].get() and os.path.exists(alignment_script):
                run_any = True
                self._set_status("Alignment", "Running alignment module...", busy=True)
                cmd = [
                    sys.executable, alignment_script,
                    "--input", input_path,
                    "--outdir", out_dir,
                    "--demdir", dem_dir,
                    "--params_json", params_json
                ]
   
                p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1, env=os.environ.copy())

                try:
                    for line in iter(p.stdout.readline, ''):
                        if not line:
                            break
                        logging.info(line.strip())
                    p.stdout.close()
                    p.wait(timeout=7200)
                except subprocess.TimeoutExpired:
                    p.kill()
                    logging.error("Alignment process timed out.")
                    self._set_status("Error", "Alignment timed out.", busy=False)
                    return
                
                if p.returncode != 0:
                    self._set_status("Error", f"Alignment failed (exit code {p.returncode}).", busy=False)
                    messagebox.showerror("Run Failed", "Alignment failed. Check the log for details.")
                    return

            if self.vars["do_plan_profile"].get() and os.path.exists(pp_script):
                run_any = True
                route_id = self.vars["pp_route_id"].get().strip()
                if not route_id:
                    self._set_status("Error", "Plan/Profile route ID is missing.", busy=False)
                    messagebox.showerror("Missing Route", "Select/enter a Route ID for Plan/Profile.")
                    return

                self._set_status("Plan/Profile", f"Running plan/profile for route {route_id}...", busy=True)
                cmd = [
                    sys.executable, pp_script,
                    "--input", input_path,
                    "--route", route_id,
                    "--outdir", out_dir,
                    "--demdir", dem_dir,
                    "--params_json", params_json
                ]

                start_val = self.vars["pp_start_rp"].get().strip()
                end_val = self.vars["pp_end_rp"].get().strip()
                if start_val: cmd.extend(["--start", start_val])
                if end_val: cmd.extend(["--end", end_val])

                p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1, env=os.environ.copy())
                
                try:
                    for line in iter(p.stdout.readline, ''):
                        if not line:
                            break
                        logging.info(line.strip())
                    p.stdout.close()
                    p.wait(timeout=7200)
                except subprocess.TimeoutExpired:
                    p.kill()
                    logging.error("Plan/Profile process timed out.")
                    self._set_status("Error", "Plan/Profile timed out.", busy=False)
                    return
                
                if p.returncode != 0:
                    self._set_status("Error", f"Plan/Profile failed (exit code {p.returncode}).", busy=False)
                    messagebox.showerror("Run Failed", "Plan/Profile failed. Check the log for details.")
                    return

                p_pdf = None
                if self.vars["out_pdf"].get():
                    if os.path.exists(simple_pdf_script):
                        self._set_status("Plan/Profile", "Generating Plan/Profile PDF...", busy=True)
                        vertices_csv, horizontal_csv, vertical_csv = self._latest_plan_profile_outputs(out_dir, route_id)
                        
                        if not vertices_csv or not horizontal_csv or not vertical_csv:
                            logging.warning("Plan/Profile CSV outputs not found; cannot generate PDF.")
                        else:
                            pdf_out = os.path.join(out_dir, f"plan_profile_{route_id.replace('/', '-')}.pdf")
                            cmd_pdf = [
                                sys.executable, simple_pdf_script,
                                "--vertices_csv", vertices_csv,
                                "--horizontal_csv", horizontal_csv,
                                "--vertical_csv", vertical_csv,
                                "--pdf_out", pdf_out,
                                "--route_id", route_id
                            ]
                            p_pdf = subprocess.run(cmd_pdf, capture_output=True, text=True, env=os.environ.copy())
                            
                            if p_pdf.stdout:
                                logging.info(p_pdf.stdout.strip())
                            if p_pdf.stderr:
                                logging.info(p_pdf.stderr.strip())
                            if p_pdf.returncode != 0:
                                self._set_status("Error", f"PDF generation failed (exit code {p_pdf.returncode}).", busy=False)
                                messagebox.showerror("PDF Failed", f"Plan/Profile PDF failed. Exit code {p_pdf.returncode}")
                                return
                    else:
                        logging.warning("No PDF report script found.")

            if self.vars["do_4d"].get() and os.path.exists(enrich_script):
                run_any = True
                self._set_status("4D Enrichment", "Running 4D enrichment module...", busy=True)
                cmd = [
                    sys.executable, enrich_script,
                    "--input", input_path,
                    "--outdir", out_dir,
                    "--demdir", dem_dir,
                    "--params_json", params_json
                ]
                
                if self.vars["replace_curve_grade"].get():
                    state_label = self.vars["state_fips"].get().strip().zfill(2)
                    state_out_dir = os.path.join(out_dir, f"Output_State_{state_label}") if state_label and state_label != "00" else out_dir
                    
                    h_files = sorted([
                        os.path.join(r, f)
                        for r, _, files in os.walk(state_out_dir)
                        for f in files
                        if f.startswith("alignment_horizontal_") and f.endswith(".csv")
                    ])
                    v_files = sorted([
                        os.path.join(r, f)
                        for r, _, files in os.walk(state_out_dir)
                        for f in files
                        if f.startswith("alignment_vertical_") and f.endswith(".csv")
                    ])
                    if h_files:
                        cmd += ["--horizontal-csv", h_files[-1]]
                        logging.info(f"Curve replacement: using {os.path.basename(h_files[-1])}")
                    else:
                        logging.warning("Curve/grade replacement requested but no horizontal CSV found. Skipping.")
                    if v_files:
                        cmd += ["--vertical-csv", v_files[-1]]
                        logging.info(f"Grade replacement: using {os.path.basename(v_files[-1])}")
                    else:
                        logging.warning("Curve/grade replacement requested but no vertical CSV found. Skipping.")

                p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1, env=os.environ.copy())
                
                try:
                    for line in iter(p.stdout.readline, ''):
                        if not line:
                            break
                        logging.info(line.strip())
                    p.stdout.close()
                    p.wait()
                except Exception as e:
                    p.kill()
                    logging.error(f"4D Enricher process error: {e}")
                    self._set_status("Error", "4D Enricher error.", busy=False)
                    return
                
                if p.returncode != 0:
                    self._set_status("Error", f"4D Enricher failed (exit code {p.returncode}).", busy=False)
                    messagebox.showerror("Run Failed", f"4D Enricher failed. Exit code {p.returncode}")
                    return

                # ----------------------------------------------------------
                # NATIONAL MERGE
                # When running all states (STATE_FIPS blank / "00"), merge
                # all per-state production CSVs into a single national file.
                # Per-state files are retained alongside the national output.
                # ----------------------------------------------------------
                # state_fips_input was a local variable inside
                # _collect_params_json(), not this method (_run) -- reading
                # it here raised NameError after every successful run that
                # reached this point, which is why "Run Selected" stayed
                # disabled even though alignment and enrichment had both
                # already completed. Read the same value directly from the
                # GUI variable instead.
                current_state_fips = self.vars["state_fips"].get().strip().zfill(2)
                if current_state_fips == "00":
                    self._set_status("4D Enrichment", "Merging per-state CSVs into national output...", busy=True)
                    logging.info("\n" + "="*60)
                    logging.info("=== NATIONAL MERGE: Combining per-state 4D CSVs ===")
                    logging.info("="*60)
                    stamp = datetime.now().strftime("%Y%m%d")
                    national_csv = os.path.join(out_dir, f"hpms_4d_national_{stamp}.csv")
                    all_fips = [
                        "01","02","04","05","06","08","09","10","11","12",
                        "13","15","16","17","18","19","20","21","22","23",
                        "24","25","26","27","28","29","30","31","32","33",
                        "34","35","36","37","38","39","40","41","42","44",
                        "45","46","47","48","49","50","51","53","54","55","56","72"
                    ]
                    frames, missing = [], []
                    for fips in all_fips:
                        state_dir = os.path.join(out_dir, f"Output_State_{fips}")
                        if not os.path.isdir(state_dir):
                            missing.append(fips)
                            continue
                        csv_files = sorted([
                            f for f in os.listdir(state_dir)
                            if f.startswith("hpms_4d_production_") and f.endswith(".csv")
                        ], reverse=True)
                        if not csv_files:
                            missing.append(fips)
                            logging.warning(f"  No production CSV found for state {fips} -- skipping")
                            continue
                        try:
                            df_state = pd.read_csv(
                                os.path.join(state_dir, csv_files[0]),
                                low_memory=False
                            )
                            frames.append(df_state)
                            logging.info(f"  Merged {fips}: {len(df_state):,} rows")
                        except Exception as merge_err:
                            logging.error(f"  Failed to load {fips}: {merge_err}")
                            missing.append(fips)
                    if frames:
                        national_df = pd.concat(frames, ignore_index=True)
                        national_df.to_csv(national_csv, index=False)
                        logging.info(f"\nNational merge complete: {len(national_df):,} total rows")
                        logging.info(f"Saved: {os.path.basename(national_csv)}")
                        if missing:
                            logging.warning(
                                f"States not included ({len(missing)}): {', '.join(missing)}"
                            )
                    else:
                        logging.error("National merge failed: no per-state CSV files found.")

            if not run_any:
                self._set_status("Idle", "No mode selected. Please pick at least one output mode.", busy=False)
                messagebox.showwarning("No Mode Selected", "Please select at least one output mode.")
                return

            validation_ok = True
            if self.vars["auto_validate"].get() and self.vars["do_alignment"].get():
                self._set_status("Validation", "Running validator checks...", busy=True)
                
                state_label = self.vars["state_fips"].get().strip().zfill(2)
                state_out_dir = os.path.join(out_dir, f"Output_State_{state_label}") \
                    if state_label and state_label != "00" else out_dir
                rc, vmsg = self._run_validator(state_out_dir)
                logging.info("=== Validation Report ===")
                logging.info(vmsg if vmsg else "No validator output.")
                validation_ok = (rc == 0)

            if validation_ok:
                self._set_status("Complete", "Run completed successfully.", busy=False)
                messagebox.showinfo("Complete", "Run completed successfully.")
            else:
                self._set_status("Complete", "Run completed, but validation reported issues.", busy=False)
                messagebox.showwarning("Complete with Warnings", "Run completed, but validation reported issues.")

            if self.vars["open_output_when_done"].get():
                self._open_output_folder(out_dir)

        except Exception as e:
            logging.exception(f"Run failed: {e}")
            self._set_status("Error", str(e), busy=False)
            messagebox.showerror("Error", str(e))
        finally:
            self.btn_run.configure(state="normal")
            if hasattr(self, "status_phase") and self.status_phase.get() not in ("Complete", "Error"):
                self._set_status("Idle", "No active tasks.", busy=False)


if __name__ == "__main__":
    root = tk.Tk()
    app = RATUnifiedGUI(root)
    root.mainloop()
