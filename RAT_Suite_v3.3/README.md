# Roadway Alignment Tool (RAT) Suite v3.3

## Overview
The Roadway Alignment Tool (RAT) Suite is a collection of Python geospatial applications developed by the Federal Highway Administration (FHWA). It takes 2D Highway Performance Monitoring System (HPMS) linework, drapes it over USGS 3D Elevation Program (3DEP) 1-meter Digital Elevation Models (DEMs), and uses spline smoothing to detect, measure, and visualize horizontal and vertical curves.

Highlights of version 3.3:
* **1-meter elevation.** Profiles are sampled from USGS 3DEP 1 m DEM tiles, downloaded on demand and cached for reuse.
* **Bridge and tunnel repair.** Bare-earth DEMs remove bridge decks and show the ground over tunnels. RAT finds these spans directly in the elevation data and replaces each with a straight line between its observed ends. HPMS and National Bridge/Tunnel Inventory (NBI/NTI) records label the spans, but the DEM decides where they are. The exception is a long bridge whose approaches run over a sloping shore, which leaves no clear edge in the DEM: where an HPMS bridge record covers ground well below the road, the span follows the record's limits (up to 30,000 ft, `BRIDGE_RECORD_MAX_LEN_FT` in `core/rat_core.py`).
* **National smoothing factors.** Horizontal smoothing factors by State and functional class come from a national calibration (see *National Calibration Defaults* below) and load automatically for the selected State. One vertical smoothing factor applies nationwide.
* **Decoupled design.** The Unified GUI runs each processing script as a separate process, which keeps memory in check during statewide and national runs.

## Repository structure

```
RAT_Suite_v3.3/
├── apps/
│   ├── rat_unified_gui.py              Central GUI
│   ├── rat_alignment_cli.py            Statewide curve extraction
│   ├── rat_plan_profile_cli.py         Single-route plan & profile data
│   ├── rat_plan_profile_report_pdf.py  Plan & profile PDF sheets
│   └── hpms_4d_enricher_cli.py         3D/4D (XYZM) HPMS linework
├── core/
│   ├── rat_core.py                     Math and geospatial engine
│   ├── rat_cross_section.py            Cross sections and superelevation from the 1 m DEM
│   ├── rat_param_summary.py            Run-parameter panels for maps, dashboards, PDFs
│   ├── rat_structure_report.py         Bridge/tunnel span report
│   ├── rat_dem_relief.py               Shaded relief from cached 1 m DEM tiles
│   └── national_smoothing_factors.json National calibration results
├── tools/
│   ├── rat_national_calibration.py     National smoothing-factor calibration
│   ├── pull_national_curve_distribution.py  HPMS curve-class shares for calibration sample sizes
│   ├── rat_calibration_diagnostics.py  Charts and checks for calibration output
│   ├── rat_structure_span_check.py     Re-check bridge/tunnel spans in existing outputs
│   └── rat_results_validator.py        QA/QC checks on outputs
├── NBI/
│   ├── download_nbi.bat                Downloads the NBI and NTI files from FHWA
│   └── README.md
├── docs/
│   ├── RAT_Suite_v3_3_Technical_Manual.md
│   └── RAT_Suite_v3_3_User_Manual.md
├── LICENSE
├── README.md
├── DISCLAIMER.md
├── Roadmap.md
├── Run_RAT_Suite.bat
├── pyproject.toml
└── .gitignore
```

## Suite components
1. **Unified GUI (`apps/rat_unified_gui.py`):** Collects inputs and settings, saves them to `run_params.json`, and runs the selected processing scripts.
2. **Core engine (`core/rat_core.py`):** UTM projection, DEM download and sampling, bridge/tunnel span repair, spline smoothing, curve detection, and spatial indexing. Logic shared by two or more scripts lives here.
3. **Alignment (`apps/rat_alignment_cli.py`):** Processes a State's network and writes horizontal curve, vertical curve and grade segment tables, per-vertex tables, section scores, interactive maps (curves, grades, intersections), and a summary dashboard.
4. **Plan & Profile (`apps/rat_plan_profile_cli.py`, `apps/rat_plan_profile_report_pdf.py`):** Processes one route (optionally a milepost range) and writes vertex, curve and grade segment tables, an interactive analytics dashboard, and multi-page PDF sheets with USGS topographic and aerial basemaps.
5. **4D Enricher (`apps/hpms_4d_enricher_cli.py`):** Converts 2D HPMS linework into 3D/4D linestrings (longitude, latitude, elevation, milepost) for GIS and 3D applications.
6. **Calibration (`tools/rat_national_calibration.py`):** Derives horizontal smoothing factors for each State and functional class from the HPMS line work (no DEM downloads). Writes the factors, an audit CSV and a run log to `output/national_cv/`; the `_filled` factors file can be copied to `core/national_smoothing_factors.json`. `tools/pull_national_curve_distribution.py` (run once per HPMS release) supplies the curve-class shares used to size its samples, and `tools/rat_calibration_diagnostics.py` charts and checks its audit CSV.
7. **Validator (`tools/rat_results_validator.py`):** Checks outputs for required fields, invalid geometry, and anomalous values. The GUI runs it after each alignment run and saves its report as `qa_validation_report_<state>_<date>_v<n>.txt` beside the files it checked.
8. **Span check (`tools/rat_structure_span_check.py`):** Re-runs the bridge and tunnel detection on existing plan/profile vertex files, for example to try different settings without downloading anything.

The tools are run from the suite folder (for example `python tools/rat_national_calibration.py`) after editing the CONFIGURATION block at the top of each script.

## Installation

The suite is packaged with `pyproject.toml` and requires **Python 3.10 or later**.

1. **Get the code.** Clone the repository, or download it as a ZIP and extract it:
   ```bash
   git clone <repository_url>
   cd <repository_directory>
   ```
2. **Create an environment (recommended).** With conda, which installs the geospatial libraries most reliably on Windows:
   ```bash
   conda create -n rat_suite_env -c conda-forge python=3.12 geopandas rasterio fiona pyproj scipy matplotlib plotly folium contextily requests
   conda activate rat_suite_env
   ```
   Or with `venv` and pip:
   ```bash
   python -m venv venv
   venv\Scripts\activate        # Windows
   source venv/bin/activate     # macOS / Linux
   pip install .
   ```
3. **Optional: bridge and tunnel inventories.** Run `NBI/download_nbi.bat` (Windows) to download the National Bridge Inventory delimited file and the National Tunnel Inventory file from FHWA into the `NBI/` folder, or download them yourself (see `NBI/README.md`). RAT finds bridges and tunnels from the elevation data on its own; these files only label the spans.

## Quick start

### 1. Launch the suite
* Double-click `Run_RAT_Suite.bat` (Windows). If you use a conda environment, run it from a prompt where that environment is active, or
* From a terminal in the suite folder: `python apps/rat_unified_gui.py`

### 2. Input source
* **Socrata URL (default):** the HPMS dataset on the U.S. DOT data portal, `https://datahub.transportation.gov/resource/42um-tgh5.json`. Enter a two-digit **State FIPS** code, or leave it blank (or enter ALL) to run every State. A free Socrata app token is optional but reduces throttling on large downloads. Scripts run outside the GUI read the token from the `RAT_SOCRATA_TOKEN` environment variable.
* **Local file:** a `.csv`, `.shp`, or `.geojson` with a `RouteId` column and line geometry (WKT for CSV).
* **Functional System / Facility Type filters** limit which roads are processed.

### 3. Directories
* **Output Directory:** where results are written.
* **DEM Directory:** where elevation tiles are cached. Keep this folder between runs; re-downloading tiles is the slowest part of a large run. A tile where USGS has no elevation data (open water, or outside 3DEP coverage) is marked with a `.missing` file in `align_1m_cache` and not requested again; delete those files to have the tiles requested again (for example, after USGS adds new lidar). A tile that can't be downloaded because the network or the USGS service is down is logged as an error and tried again on the next run.

### 4. Modes
* **Run Alignment:** statewide curve tables, map, and dashboard.
* **Run Plan/Profile:** one route, chosen with **Plan/Profile Route ID** and optional **Begin/End RP**. Click **Load Routes** for the State's route list, then type any part of the Route ID; the matching routes are listed below the field and narrow as you type. Click one (or press Enter for the first, or the down arrow to move into the list).
* **Run 4D Enrichment:** 3D/4D HPMS linework.

### 5. Output formats
* **CSV:** always produced.
* **GeoJSON / GeoPackage / Shapefile:** spatial copies of the outputs (the Shapefile is the 4D Enricher's projected export for 3D tools such as Blender).
* **Interactive HTML Map** and **Summary Dashboard HTML:** alignment results. Three maps are written: curves (`alignment_map_*.html`), grades (`*_grades.html`, colored by signed percent grade), and intersections (`*_intersections.html`).
* **Plan/Profile PDF:** engineering sheets, 1,500 ft of road per page, with a cover page listing the run parameters. When the 1 m DEM tiles are cached, the PDF also includes a shaded-relief terrain overview of the whole section (plus section pages for longer routes) and relief behind each sheet's plan view. These options are set in the configuration block at the top of `apps/rat_plan_profile_report_pdf.py`. Check **Profile Elevations every … ft** to print profile and ground elevations under each profile view (default every 100 ft). Cross-section sheets follow the plan/profile sheets. For the cross sections, plan/profile also downloads the other HPMS lines within about 400 ft of the section (the other direction of travel, ramps, frontage roads), of any facility type or functional class, and uses the other direction's line to place the opposing roadway where the DEM median does not agree with it within 25 ft (set `NEARBY_LINES_FROM_SOCRATA` at the top of `apps/rat_plan_profile_cli.py`). Opposing lane slopes that may be side slope rather than pavement are shown in gray with a "?" (`Opp_Note` = Doubtful in the cross-sections CSV). Where two or more cross sections in a row find no lanes at the HPMS line and a centerline far from it, and the ground along the line there is rough rather than pavement-smooth, the line is probably off the roadway (an old alignment where the DEM shows new construction). Those stations, and the curves, grade segments and vertices around them, carry `Line_Off_Road` = True; the run log gives the milepost range, and the sheets mark them "LINE OFF ROAD?". Their geometry describes the ground beside the road and should be checked against current imagery. The same lines are drawn on each plan view and labeled with their Route ID and, where they cross the route, "over" or "under" (from the route's bridge spans, the NBI/NTI structure points, and the DEM) (`PLAN_SHOW_OTHER_ROUTES` in the PDF configuration block).
* **Structure Span Report:** every bridge and tunnel span RAT repaired. Alignment runs write a CSV; Plan/Profile runs also write a PDF with each span's profile and a shaded-relief map of the 1 m DEM around it.
* **Vertices Spatial File (large):** per-vertex points as a spatial file.

### 6. Run
Click **Run Selected**. Closing the GUI while a run is in progress asks first and then stops the run, including its worker processes. Each run's output files get their own version number (`_v1`, `_v2`, …), taken when the run starts. Progress appears in the Run Log, which is also saved as `Run_Log_<state>_<date>_v<n>.txt` in the State's output folder (the output folder for a national run). If any errors were logged, the GUI says so at the end of the run; search the Run Log for "ERROR". Each State's section of the Run Log begins by listing the smoothing factors used for each functional class, marked state (calibrated), national, or custom. Settings are saved to `run_params.json` in the output folder and restored the next time the GUI opens. **Note:** `run_params.json` contains your Socrata token and local paths, so don't share or commit it.

## Curve and grade classes
Every A–F scale in the suite runs from A (mildest) to F (most severe).

* **Horizontal curves** (`Bin`) are classed by degree of curve, using the HPMS curve class thresholds.
* **Grade segments** (`alignment_grades_*.csv`, `{base}_grades.csv`) are the pieces of road between the vertical points of intersection (VPIs) of the detected vertical curves, plus the chunk ends. Each has a signed grade (`Grade_Pct`, positive rising in the direction of increasing reference point) and an HPMS Item 45 class (`Grade_Bin`: A under 0.5%, B 0.5–2.4, C 2.5–4.4, D 4.5–6.4, E 6.5–8.4, F 8.5 or more). The segments cover each chunk with no gaps or overlaps.
* **Vertical curves** (`VC_Class`) are classed by the highest design speed their stopping sight distance supports (AASHTO, *A Policy on Geometric Design of Highways and Streets*, 7th Edition, 2018, Table 3-1; 3.5 ft eye and 2.0 ft object over crests, 2.0 ft headlights with a 1° beam on sags): A 70 mph or more, B 60–65, C 50–55, D 40–45, E 30–35, F 25 or less. The table also gives `SSD_Provided_Ft` (blank where the curve does not limit sight distance), `Design_Speed_Supported_Mph`, and `K_Value` in feet per percent of grade change.
* **Speed comparison.** `Speed_Ref_Mph` is the HPMS speed limit of the section containing the curve's midpoint, or a default by functional class where none is reported (FS1 75, FS2 65, FS3 55, FS4 45, FS5 35, FS6 30, FS7 25 mph; `DEFAULT_SPEED_BY_FS_MPH` in `core/rat_core.py`). `Meets_Speed_Ref` is True when the supported design speed is at least that speed. The comparison never changes the class.
* **Vertices** carry `V_Grade_Bin` and `Seg_Grade_Pct` (the grade segment containing the point) and `V_Curve_Class` (A on tangents).
* **Section scores** give each HPMS section the class covering most of its length (`H_Curve_Bin`, `V_Grade_Bin`). With **Replace HPMS curve/grade classifications** checked, the 4D Enricher replaces the HPMS CURVES_A–F values with lengths from the horizontal curve table and GRADES_A–F with lengths from the grades table, in the same fields; each set adds up to the section length.

## National calibration defaults
`core/national_smoothing_factors.json` holds the horizontal (H) smoothing factors for each State, DC, and Puerto Rico, by functional class. Entering a State FIPS code in the GUI fills in that State's values, where they can be reviewed or adjusted; **Reload State Factors** restores them after editing. Where a State has no calibrated value for a functional class, the national value is used. A national ("ALL" States) run applies each State's own values automatically. Each continuous piece of a route uses the factor for the functional class that covers the most miles of that piece.

The horizontal factors were produced by `tools/rat_national_calibration.py`:
* **Sampling.** For each State and functional class, a sample of one-mile road segments (sized with Cochran's formula) is drawn five times.
* **Selection.** H is the smallest smoothing factor at which RAT's own curve classifier stops changing its decisions as smoothing increases, provided the smoothed line stays within 6 ft of the HPMS line (99th percentile); otherwise the largest factor that does.
* **Aggregation.** The five results are combined by a consistency check and the lower median, so each value is one a repeat selected. Combinations that never settle are filled with the lower median of the other States, or with the State's own largest factor that stays within 6 ft where that is smaller.

**Vertical.** All elevations come from one source, the USGS 3DEP 1 m DEM, so a single vertical smoothing factor (10, set in `core/rat_core.py` as `NATIONAL_V_SMOOTH_FACTOR`) applies to every State and functional class. It allows the profile an RMS departure of about 0.45 ft from the DEM. It was chosen by re-smoothing the raw DEM profiles of test routes over a range of factors: the number of detected vertical curves falls as the factor rises (DEM undulations otherwise read as short curves) and holds steady from about 8.5–10 to 20–25, after which real curves begin to merge. 10 is the start of that stable range. The GUI's V Smooth Factor field can override it for a single run.

The calibration's audit CSV and run log record the per-repeat results behind each value. You can rerun the calibration for your own States if your HPMS data differs significantly from the national release.

## Known limitations
* **HPMS line off the roadway.** Where the HPMS line follows an old alignment, curves and grades there describe the ground beside the road. Plan/profile runs flag these stretches (`Line_Off_Road`); alignment runs do not.
* **Bridges at the end of the data.** A bridge that runs off the end of the route's data (at a State line, for example) is held level at the one abutment in the data.
* **Bridge ends on steep approaches.** Each bridge deck is drawn as a straight line between its abutments. Where a steep approach meets a much flatter deck, the smoothing can dip a foot or two below the ground just before the bridge, giving a grade one class steeper than the road and a sharp sag or crest at the bridge end. Check class D–F vertical curves and steep grades at bridge ends (especially sags flagged `False_Valley_Warning`) against the raw elevation line on the profile sheets.

See `Roadmap.md` for planned work.

## Documentation
See `docs/RAT_Suite_v3_3_User_Manual.md` for running the suite and reading its outputs, and `docs/RAT_Suite_v3_3_Technical_Manual.md` for methods, formulas, and the full parameter reference.

## License and disclaimer
This software is dedicated to the public domain under the CC0 1.0 Universal Public Domain Dedication (see `LICENSE`). Please read `DISCLAIMER.md` regarding use and limitations.

*RAT Suite v3.3 — Federal Highway Administration, Office of Highway Policy Information*
