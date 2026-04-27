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
RAT UNIFIED GUI (Graphical User Interface)
--------------------------------------------------------------------------------
ROLE: The primary user-facing orchestrator for the RAT Suite.
DESCRIPTION:
Provides a Tkinter-based interface for users to configure alignment, plan/profile,
and 4D enrichment parameters. It does not perform mathematical processing; instead,
it collects inputs, packages them into a JSON payload, and uses the subprocess
module to safely execute the suite's CLI scripts in isolated memory environments.
CREATED BY: FHWA, Office of Highway Policy Information using Google Gemini and
ChatGPT.
CREATED ON: 4/23/2026
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

SOCRATA_DEFAULT = "https://datahub.transportation.gov/resource/42um-tgh5.json"
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s: %(message)s")


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
        self.root.title("RAT Unified GUI Runner")
        # Fit window to screen size with some margin
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
            "use_local": tk.BooleanVar(value=False),

            "densify_spacing_ft": tk.StringVar(value="10"),
            "h_smooth_factor": tk.StringVar(value="4500"),
            "v_smooth_factor": tk.StringVar(value="4500"),
            "h_min_delta": tk.StringVar(value="3.5"),
            "h_min_curve_length_ft": tk.StringVar(value="100"),
            "v_min_curve_length_ft": tk.StringVar(value="200"),
            "v_min_grade_change": tk.StringVar(value="0.5"),

            "enable_merge": tk.BooleanVar(value=False),
            "merge_gap_ft": tk.StringVar(value="600"),
            "v_merge_gap_ft": tk.StringVar(value="1500"),

            "auto_validate": tk.BooleanVar(value=True),
            "open_output_when_done": tk.BooleanVar(value=True),

            "pp_route_id": tk.StringVar(),
            "pp_start_rp": tk.StringVar(value="0"),
            "pp_end_rp": tk.StringVar(value="10"),

            "do_alignment": tk.BooleanVar(value=True),
            "do_plan_profile": tk.BooleanVar(value=False),
            "do_4d": tk.BooleanVar(value=False),

            "out_csv": tk.BooleanVar(value=True),
            "out_geojson": tk.BooleanVar(value=True),
            "out_gpkg": tk.BooleanVar(value=False),
            "out_shp": tk.BooleanVar(value=False),
            "out_html_map": tk.BooleanVar(value=True),
            "out_dashboard": tk.BooleanVar(value=True),
            "out_qa_exceptions": tk.BooleanVar(value=True),
            "out_pdf": tk.BooleanVar(value=False),
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
            "H_SMOOTH_FACTOR_FS3": tk.IntVar(value="4000"),
            "V_SMOOTH_FACTOR_FS3": tk.IntVar(value="4000"),
            "H_SMOOTH_FACTOR_FS45": tk.IntVar(value="2500"),
            "V_SMOOTH_FACTOR_FS45": tk.IntVar(value="2500"),
            "H_SMOOTH_FACTOR_FS67": tk.IntVar(value="1000"),
            "V_SMOOTH_FACTOR_FS67": tk.IntVar(value="1000"),
        }

        self._build_ui()
        self._setup_logger()

    def _setup_logger(self):
        self.logger = logging.getLogger()
        self.logger.handlers = []
        self.logger.addHandler(TextHandler(self.log_text))
        self.logger.setLevel(logging.INFO)

    def _build_ui(self):
        # Scrollable canvas wrapper
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

        # Mouse wheel scrolling
        def on_mousewheel(event):
            canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
        canvas.bind_all("<MouseWheel>", on_mousewheel)

        # 1) Input source
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

        ttk.Label(lf_input, text="Local File:").grid(row=2, column=0, sticky="e", pady=4)
        self.ent_local = ttk.Entry(lf_input, textvariable=self.vars["input_local"], width=74, state="disabled")
        self.ent_local.grid(row=2, column=1, columnspan=3, sticky="ew", padx=6)
        self.btn_local = ttk.Button(lf_input, text="Browse", command=self._browse_local, state="disabled")
        self.btn_local.grid(row=2, column=4, padx=4)

        ttk.Label(lf_input, text="State FIPS:").grid(row=3, column=0, sticky="e", pady=4)
        self.ent_state = ttk.Entry(lf_input, textvariable=self.vars["state_fips"], width=12)
        self.ent_state.grid(row=3, column=1, sticky="w")

        ttk.Label(lf_input, text="Socrata Token (optional):").grid(row=3, column=2, sticky="e", pady=4)
        ttk.Entry(lf_input, textvariable=self.vars["socrata_token"], width=35).grid(row=3, column=3, sticky="w")

        lf_fsys = ttk.LabelFrame(lf_input, text="Functional System Filters", padding=8)
        lf_fsys.grid(row=4, column=0, columnspan=5, sticky="ew", pady=8)

        fs_labels = [
            "1: Interstate", "2: PA Freeways", "3: PA Other",
            "4: Minor Arterial", "5: Major Collector", "6: Minor Collector", "7: Local"
        ]
        for i in range(1, 8):
            ttk.Checkbutton(lf_fsys, text=fs_labels[i - 1], variable=self.fsys_vars[i]).grid(
                row=0, column=i - 1, padx=4, sticky="w"
            )

        # 2) Directories
        lf_dirs = ttk.LabelFrame(main, text="2) Directories", padding=12)
        lf_dirs.pack(fill=tk.X, pady=6)

        ttk.Label(lf_dirs, text="Output Directory:").grid(row=0, column=0, sticky="e", pady=4)
        ttk.Entry(lf_dirs, textvariable=self.vars["output_dir"], width=85).grid(row=0, column=1, sticky="ew", padx=6)
        ttk.Button(lf_dirs, text="Browse", command=self._browse_out).grid(row=0, column=2)

        ttk.Label(lf_dirs, text="DEM Directory:").grid(row=1, column=0, sticky="e", pady=4)
        ttk.Entry(lf_dirs, textvariable=self.vars["dem_dir"], width=85).grid(row=1, column=1, sticky="ew", padx=6)
        ttk.Button(lf_dirs, text="Browse", command=self._browse_dem).grid(row=1, column=2)

        # 3) Output / mode selection
        lf_modes = ttk.LabelFrame(main, text="3) Output / Mode Selection", padding=12)
        lf_modes.pack(fill=tk.X, pady=6)

        ttk.Checkbutton(lf_modes, text="Run Alignment", variable=self.vars["do_alignment"]).grid(row=0, column=0, sticky="w")
        ttk.Checkbutton(lf_modes, text="Run Plan/Profile", variable=self.vars["do_plan_profile"]).grid(row=0, column=1, sticky="w", padx=15)
        ttk.Checkbutton(lf_modes, text="Run 4D Enrichment", variable=self.vars["do_4d"]).grid(row=0, column=2, sticky="w", padx=15)

        ttk.Label(lf_modes, text="Plan/Profile Route ID:").grid(row=1, column=0, sticky="e", pady=4)
        self.route_combo = ttk.Combobox(lf_modes, textvariable=self.vars["pp_route_id"], width=30, state="readonly")
        self.route_combo.grid(row=1, column=1, sticky="w", padx=6)
        ttk.Button(lf_modes, text="Load Routes", command=self._load_routes_thread).grid(row=1, column=2, sticky="w")

        ttk.Label(lf_modes, text="Begin RP (mi):").grid(row=2, column=0, sticky="e", pady=4)
        ttk.Entry(lf_modes, textvariable=self.vars["pp_start_rp"], width=12).grid(row=2, column=1, sticky="w")
        ttk.Label(lf_modes, text="End RP (mi):").grid(row=2, column=2, sticky="e", pady=4)
        ttk.Entry(lf_modes, textvariable=self.vars["pp_end_rp"], width=12).grid(row=2, column=3, sticky="w")

        # 4) Parameters
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
        ttk.Label(grp_h, text="H Smooth Factor (FS 1-2)(ft):").grid(row=1, column=0, sticky="e", pady=2)
        ttk.Entry(grp_h, textvariable=self.vars["h_smooth_factor"], width=12).grid(row=1, column=1, sticky="w")
        ttk.Label(grp_h, text="H Min Delta (deg):").grid(row=2, column=0, sticky="e", pady=2)
        ttk.Entry(grp_h, textvariable=self.vars["h_min_delta"], width=12).grid(row=2, column=1, sticky="w")
        ttk.Label(grp_h, text="H Min Curve Length (ft):").grid(row=3, column=0, sticky="e", pady=2)
        ttk.Entry(grp_h, textvariable=self.vars["h_min_curve_length_ft"], width=12).grid(row=3, column=1, sticky="w")

        grp_v = ttk.LabelFrame(left, text="Vertical", padding=10)
        grp_v.grid(row=0, column=1, sticky="nsew", padx=6)
        ttk.Label(grp_v, text="V Smooth Factor (FS 1-2)(ft):").grid(row=0, column=0, sticky="e", pady=2)
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
       
        # 5) Output formats
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

        # 6) Buttons
        btn_frame = ttk.Frame(main)
        btn_frame.pack(fill=tk.X, pady=8)
       
        ttk.Button(btn_frame, text="Advanced Settings...", command=self._open_advanced_settings).pack(side=tk.LEFT, padx=4)
        ttk.Button(btn_frame, text="Restore Defaults", command=self._restore_main_defaults).pack(side=tk.LEFT, padx=4)
       
        self.btn_run = ttk.Button(btn_frame, text="Run Selected", command=self._run_thread)
        self.btn_run.pack(side=tk.RIGHT, padx=4)
       
        ttk.Button(btn_frame, text="Save Config", command=self._save_config).pack(side=tk.RIGHT, padx=4)
        ttk.Button(btn_frame, text="Load Config", command=self._load_config).pack(side=tk.RIGHT, padx=4)

        # 7) Log
        lf_log = ttk.LabelFrame(main, text="Run Log", padding=8)
        lf_log.pack(fill=tk.BOTH, expand=True, pady=6)
        self.log_text = tk.Text(lf_log, height=14, state="disabled")
        self.log_text.pack(fill=tk.BOTH, expand=True)

        """
        # 7) Background activity panel - there isn't enough room on the screen for this
        lf_status = ttk.LabelFrame(main, text="Background Activity", padding=8)
        lf_status.pack(fill=tk.X, pady=6)
        self.status_phase = tk.StringVar(value="Idle")
        self.status_detail = tk.StringVar(value="No active tasks.")
        self.status_progress = ttk.Progressbar(lf_status, mode="indeterminate")
        ttk.Label(lf_status, text="Phase:").grid(row=0, column=0, sticky="w")
        ttk.Label(lf_status, textvariable=self.status_phase).grid(row=0, column=1, sticky="w", padx=(6, 20))
        ttk.Label(lf_status, text="Detail:").grid(row=1, column=0, sticky="w")
        ttk.Label(lf_status, textvariable=self.status_detail, wraplength=900).grid(row=1, column=1, sticky="w", padx=(6, 20))
        self.status_progress.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(8, 2))
        lf_status.columnconfigure(1, weight=1)
        """
    # ------------------------- UI helpers -------------------------
    def _toggle_source(self):
        use_local = self.vars["use_local"].get()
        self.ent_local.configure(state="normal" if use_local else "disabled")
        self.btn_local.configure(state="normal" if use_local else "disabled")
        self.ent_url.configure(state="disabled" if use_local else "normal")

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
        win.geometry("850x520")
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
            "H_SMOOTH_FACTOR_FS3", "H_SMOOTH_FACTOR_FS45", "H_SMOOTH_FACTOR_FS67"
        ]
       
        adv_v_keys = [
            "V_VC_THRESHOLD", "V_GAP_TOLERANCE", "V_MIN_OFFSET_FT", "V_REVERSAL_TOLERANCE",
            "REGRESSION_WINDOW_FT", "TREND_WINDOW_FT", "DIP_THRESHOLD_FT", "BRIDGE_MAX_LEN_FT",
            "V_SMOOTH_FACTOR_FS3", "V_SMOOTH_FACTOR_FS45", "V_SMOOTH_FACTOR_FS67"
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

    # ------------------------- data prep -------------------------
    def _collect_params_json(self, out_dir):
        fsys = [k for k, v in self.fsys_vars.items() if v.get()]
        data = {
            "DENSIFY_SPACING_FT": float(self.vars["densify_spacing_ft"].get()),
            "H_SMOOTH_FACTOR": int(float(self.vars["h_smooth_factor"].get())),
            "V_SMOOTH_FACTOR": int(float(self.vars["v_smooth_factor"].get())),
            "H_MIN_DELTA": float(self.vars["h_min_delta"].get()),
            "H_MIN_CURVE_LENGTH_FT": float(self.vars["h_min_curve_length_ft"].get()),
            "V_MIN_CURVE_LENGTH_FT": float(self.vars["v_min_curve_length_ft"].get()),
            "V_MIN_GRADE_CHANGE": float(self.vars["v_min_grade_change"].get()),

            "ENABLE_MERGE": bool(self.vars["enable_merge"].get()),
            "MERGE_GAP_FT": float(self.vars["merge_gap_ft"].get()),
            "V_MERGE_GAP_FT": float(self.vars["v_merge_gap_ft"].get()),

            "FSYSTEM_FILTER": fsys,
            "STATE_FIPS": self.vars["state_fips"].get().strip(),
            "SOCRATA_TOKEN": self.vars["socrata_token"].get().strip(),
            "INPUT_URL": self.vars["input_url"].get().strip(),

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
        }

        data["PP_ROUTE_ID"] = self.vars["pp_route_id"].get().strip()
        data["PP_START_RP"] = float(self.vars["pp_start_rp"].get())
        data["PP_END_RP"] = float(self.vars["pp_end_rp"].get())

        for k, var in self.advanced_defaults.items():
            val = str(var.get()).strip()
            try:
                if "." in val or "e" in val.lower():
                    data[k] = float(val)
                else:
                    data[k] = int(val)
            except Exception:
                data[k] = val

        params_path = os.path.join(out_dir, "run_params.json")
        with open(params_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        return params_path

    def _save_config(self):
        out_dir = self.vars["output_dir"].get().strip()
        if not out_dir:
            messagebox.showwarning("Missing Output", "Please set output directory first.")
            return
        os.makedirs(out_dir, exist_ok=True)
        p = self._collect_params_json(out_dir)
        messagebox.showinfo("Saved", f"Config saved:\n{p}")

    def _load_config(self):
        """Loads a saved run_params.json file and populates the GUI."""
        p = filedialog.askopenfilename(
            title="Select Configuration File",
            filetypes=[("JSON Config", "*.json"), ("All Files", "*.*")]
        )
        if not p:
            return
           
        try:
            with open(p, "r", encoding="utf-8") as f:
                data = json.load(f)
            if "OUTPUT_DIR" in data:
                self.vars["output_dir"].set(data["OUTPUT_DIR"])
            if "DEM_DIR" in data:
                self.vars["dem_dir"].set(data["DEM_DIR"])
            # Create a reverse map so we can match JSON caps (e.g. OUT_CSV) to GUI lowercase vars (out_csv)
            var_map = {k.upper(): k for k in self.vars.keys()}
           
            for k, v in data.items():
                if k in self.advanced_defaults:
                    self.advanced_defaults[k].set(str(v))
                elif k == "FSYSTEM_FILTER":
                    # Clear all checkboxes first, then check the loaded ones
                    for i in range(1, 8):
                        self.fsys_vars[i].set(False)
                    for val in v:
                        if int(val) in self.fsys_vars:
                            self.fsys_vars[int(val)].set(True)
                elif k in var_map:
                    # Tkinter variables handle bool/str/int conversions cleanly via .set()
                    self.vars[var_map[k]].set(v)
                   
            # Auto-toggle the source boxes depending on what was loaded
            self._toggle_source()
           
            logging.info(f"Successfully loaded configuration from: {p}")
            messagebox.showinfo("Loaded", "Configuration successfully loaded!")
           
        except Exception as e:
            logging.error(f"Failed to load config: {e}")
            messagebox.showerror("Load Error", f"Failed to read the configuration file:\n{e}")

    def _restore_main_defaults(self):
        """Restores the main UI parameter boxes to their standard defaults."""
        defaults = {
            "densify_spacing_ft": "10",
            "h_smooth_factor": "4500",
            "v_smooth_factor": "4500",
            "h_min_delta": "3.5",
            "h_min_curve_length_ft": "100",
            "v_min_curve_length_ft": "200",
            "v_min_grade_change": "0.5",
            "enable_merge": False,
            "merge_gap_ft": "600",
            "v_merge_gap_ft": "1500"
        }
        for k, v in defaults.items():
            self.vars[k].set(v)
        logging.info("Main parameters restored to default.")

    def _restore_advanced_defaults(self):
        """Restores the advanced UI parameter boxes to their standard defaults."""
        adv_defaults = {
            "H_BASE_SMOOTH_WINDOW": "21",
            "H_MIN_HEAD_CHANGE": "0.003",
            "H_MAX_RADIUS_FT": "165000",
            "V_VC_THRESHOLD": "0.002",
            "V_GAP_TOLERANCE": "5",
            "V_MIN_OFFSET_FT": "0.10",
            "V_REVERSAL_TOLERANCE": "0.02",
            "REGRESSION_WINDOW_FT": "500",
            "TREND_WINDOW_FT": "1000",
            "DIP_THRESHOLD_FT": "6.5",
            "BRIDGE_MAX_LEN_FT": "8200",
            "H_SMOOTH_FACTOR_FS3": "4000",
            "V_SMOOTH_FACTOR_FS3": "4000",
            "H_SMOOTH_FACTOR_FS45": "2500",
            "V_SMOOTH_FACTOR_FS45": "2500",
            "H_SMOOTH_FACTOR_FS67": "1000",
            "V_SMOOTH_FACTOR_FS67": "1000"
        }
        for k, v in adv_defaults.items():
            if k in self.advanced_defaults:
                self.advanced_defaults[k].set(v)
        logging.info("Advanced parameters restored to default.")

    # ------------------------- routes list -------------------------
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
               
                data = r.json()
                routes = sorted([d["route_id"] for d in data if "route_id" in d])

            self.route_combo["values"] = routes
            if routes:
                self.route_combo.current(0)
            logging.info(f"Loaded {len(routes)} routes.")
        except Exception as e:
            logging.error(f"Load routes failed: {e}")

    # ------------------------- run orchestration -------------------------
    def _run_thread(self):
        self.btn_run.configure(state="disabled")
        threading.Thread(target=self._run, daemon=True).start()

    def _run_validator(self, out_dir):
        script_dir = os.path.dirname(os.path.abspath(__file__))
        validator_script = os.path.join(script_dir, "rat_results_validator.py")

        if not os.path.exists(validator_script):
            return 1, "Validator script not found."

        h_files = sorted(glob.glob(os.path.join(out_dir, "alignment_horizontal_*.csv")))
        v_files = sorted(glob.glob(os.path.join(out_dir, "alignment_vertical_*.csv")))

        if not h_files and not v_files:
            return 1, "No alignment outputs found for validation."

        cmd = [sys.executable, validator_script]
        if h_files:
            cmd += ["--horizontal_csv", h_files[-1]]
        if v_files:
            cmd += ["--vertical_csv", v_files[-1]]

        p = subprocess.run(cmd, capture_output=True, text=True)
        msg = (p.stdout or "") + ("\n" + p.stderr if p.stderr else "")
        return p.returncode, msg.strip()

    def _latest_plan_profile_outputs(self, out_dir, route_id):
        import re
        safe_route = re.sub(r'[<>:"/\\|?*]+', "-", str(route_id)).strip().upper()
        base = f"plan_profile_{safe_route}"

        vtx = sorted(glob.glob(os.path.join(out_dir, f"{base}_vertices.csv")))
        hor = sorted(glob.glob(os.path.join(out_dir, f"{base}_horizontal.csv")))
        ver = sorted(glob.glob(os.path.join(out_dir, f"{base}_vertical.csv")))

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

            params_json = self._collect_params_json(out_dir)
            logging.info(f"Params saved: {params_json}")

            script_dir = os.path.dirname(os.path.abspath(__file__))
            alignment_script = os.path.join(script_dir, "rat_alignment_cli.py")
            pp_script = os.path.join(script_dir, "rat_plan_profile_cli.py")
            enrich_script = os.path.join(script_dir, "hpms_4d_enricher_cli.py")
            legacy_pdf_script = os.path.join(script_dir, "rat_plan_profile_legacy_pdf_runner.py")
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
                self._set_status("Input", "Downloading data from Socrata...", busy=True)
                logging.info("Downloading Socrata data with selected filters...")

                url = self.vars["input_url"].get().strip() or SOCRATA_DEFAULT
                state = self.vars["state_fips"].get().strip()
                fsys = [str(k) for k, v in self.fsys_vars.items() if v.get()]
                token = self.vars["socrata_token"].get().strip()

                where_parts = []
                if state:
                    where_parts.append(f"stateid='{state}'")
                if fsys:
                    fs = ",".join([f"'{x}'" for x in fsys])
                    where_parts.append(f"f_system IN ({fs})")
                where_clause = " AND ".join(where_parts) if where_parts else ""

                headers = {"X-App-Token": token} if token else {}
                rows, offset, limit = [], 0, 50000

                while True:
                    self._set_status("Input", f"Fetching Socrata rows... currently {len(rows):,}", busy=True)
                    params = {"$limit": limit, "$offset": offset}
                    if where_clause:
                        params["$where"] = where_clause

                    max_attempts = 6
                    r = None
                    for attempt in range(1, max_attempts + 1):
                        try:
                            r = requests.get(url, params=params, headers=headers, timeout=120)
                            if r.status_code in (429, 500, 502, 503, 504):
                                wait_s = min(60, 2 ** attempt)
                                logging.warning(
                                    f"Socrata temporary error {r.status_code}. "
                                    f"Attempt {attempt}/{max_attempts}. Retrying in {wait_s}s..."
                                )
                                time.sleep(wait_s)
                                continue
                            r.raise_for_status()
                            break
                        except requests.RequestException as ex:
                            if attempt == max_attempts:
                                raise
                            wait_s = min(60, 2 ** attempt)
                            logging.warning(
                                f"Socrata request failed ({ex}). "
                                f"Attempt {attempt}/{max_attempts}. Retrying in {wait_s}s..."
                            )
                            time.sleep(wait_s)

                    if r is None:
                        raise RuntimeError("Failed to get Socrata response.")

                    data = r.json()
                    if not data:
                        break

                    rows.extend(data)
                    if len(data) < limit:
                        break

                    offset += limit
                    logging.info(f"Fetched {len(rows):,} rows...")

                if not rows:
                    self._set_status("Error", "No records returned from Socrata with current filters.", busy=False)
                    messagebox.showerror("No Data", "No records returned from Socrata with current filters.")
                    return

                df = pd.DataFrame(rows)
                geom_col = next((c for c in df.columns if c.lower() in ["line", "the_geom", "geometry", "shape"]), None)
                if not geom_col:
                    self._set_status("Error", "No geometry column found in Socrata response.", busy=False)
                    messagebox.showerror("Geometry Error", "No geometry column found in Socrata response.")
                    return

                def geom_to_wkt(v):
                    if isinstance(v, dict):
                        try:
                            return shape(v).wkt
                        except Exception:
                            return None
                    return v

                df["WKT"] = df[geom_col].apply(geom_to_wkt)
                rename_map = {
                    "route_id": "RouteId",
                    "begin_point": "Start_MP",
                    "end_point": "End_MP",
                    "f_system": "FSystem",
                    "urban_id": "UrbanID"
                }
                df.rename(columns=rename_map, inplace=True)
                keep_cols = [c for c in ["RouteId", "Start_MP", "End_MP", "FSystem", "UrbanID", "WKT"] if c in df.columns]
                df = df[keep_cols].dropna(subset=["WKT"]).copy()

                input_path = os.path.join(out_dir, "socrata_input_extract.csv")
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
                p = subprocess.run(cmd, capture_output=True, text=True)
                if p.stdout:
                    logging.info(p.stdout.strip())
                if p.stderr:
                    logging.info(p.stderr.strip())
                if p.returncode != 0:
                    self._set_status("Error", f"Alignment failed (exit code {p.returncode}).", busy=False)
                    messagebox.showerror("Run Failed", f"Alignment failed. Exit code {p.returncode}")
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
                    "--params_json", params_json,
                    "--start", self.vars["pp_start_rp"].get(),
                    "--end", self.vars["pp_end_rp"].get()
                ]
                p = subprocess.run(cmd, capture_output=True, text=True)
                if p.stdout:
                    logging.info(p.stdout.strip())
                if p.stderr:
                    logging.info(p.stderr.strip())
                if p.returncode != 0:
                    self._set_status("Error", f"Plan/Profile failed (exit code {p.returncode}).", busy=False)
                    messagebox.showerror("Run Failed", f"Plan/Profile failed. Exit code {p.returncode}")
                    return

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
                            p_pdf = subprocess.run(cmd_pdf, capture_output=True, text=True)
                    elif os.path.exists(simple_pdf_script):
                        self._set_status("Plan/Profile", "Generating simple Plan/Profile PDF...", busy=True)
                        vertices_csv, horizontal_csv, vertical_csv = self._latest_plan_profile_outputs(out_dir, route_id)
                        if not vertices_csv or not horizontal_csv or not vertical_csv:
                            logging.warning("Plan/Profile CSV outputs not found; cannot generate PDF.")
                            p_pdf = None
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
                            p_pdf = subprocess.run(cmd_pdf, capture_output=True, text=True)
                    else:
                        p_pdf = None
                        logging.warning("No PDF report script found (legacy or simple).")

                    if p_pdf is not None:
                        if p_pdf.stdout:
                            logging.info(p_pdf.stdout.strip())
                        if p_pdf.stderr:
                            logging.info(p_pdf.stderr.strip())
                        if p_pdf.returncode != 0:
                            self._set_status("Error", f"PDF generation failed (exit code {p_pdf.returncode}).", busy=False)
                            messagebox.showerror("PDF Failed", f"Plan/Profile PDF failed. Exit code {p_pdf.returncode}")
                            return

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
                p = subprocess.run(cmd, capture_output=True, text=True)
                if p.stdout:
                    logging.info(p.stdout.strip())
                if p.stderr:
                    logging.info(p.stderr.strip())
                if p.returncode != 0:
                    self._set_status("Error", f"4D Enricher failed (exit code {p.returncode}).", busy=False)
                    messagebox.showerror("Run Failed", f"4D Enricher failed. Exit code {p.returncode}")
                    return

            if not run_any:
                self._set_status("Idle", "No mode selected. Please pick at least one output mode.", busy=False)
                messagebox.showwarning("No Mode Selected", "Please select at least one output mode.")
                return

            validation_ok = True
            if self.vars["auto_validate"].get() and self.vars["do_alignment"].get():
                self._set_status("Validation", "Running validator checks...", busy=True)
                rc, vmsg = self._run_validator(out_dir)
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
