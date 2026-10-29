# HPMS Roadway Alignment Tool (RAT) Suite v3.3
## User Manual

Federal Highway Administration, Office of Highway Policy Information

This manual explains how to install the RAT Suite, run it from the Unified GUI, and read its outputs. The methods, formulas and full parameter reference are in the Technical Manual (`docs/RAT_Suite_v3_3_Technical_Manual.md`).

---

## Contents

1. What the suite does
2. Installation
3. Starting the suite
4. The Unified GUI, section by section
5. Running a job
6. Outputs
7. Reading the results
8. Tools for calibration and checking
9. Troubleshooting
10. Data security

---

## 1. What the suite does

The RAT Suite takes Highway Performance Monitoring System (HPMS) line work, drapes it over the USGS 3D Elevation Program (3DEP) 1-meter elevation model, and smooths the result to find and measure horizontal curves, vertical curves and grades. It runs in three modes:

| Mode | What it processes | Main products |
| :--- | :--- | :--- |
| **Alignment** | A whole State, or every State | Curve and grade tables, section scores, interactive maps, a summary dashboard |
| **Plan/Profile** | One route, or part of one | Engineering plan and profile sheets (PDF), cross sections, curve and grade tables, an analytics dashboard |
| **4D Enrichment** | A whole State, or every State | HPMS line work with elevation and reference point on every vertex (`LINESTRING ZM`), for GIS and 3D tools |

The suite finds bridges and tunnels in the elevation data itself and replaces each span with a straight line between its ends, since a bare-earth elevation model shows the ground under a bridge rather than the deck. HPMS structure records and the National Bridge and Tunnel Inventories (NBI/NTI) are used to label the spans.

RAT results are derived from observed geometry. They are suited to screening, inventory and data review, not to design.

---

## 2. Installation

The suite needs **Python 3.10 or later** and runs on Windows, macOS and Linux. The GUI uses Tkinter, which comes with the standard Python installers.

### 2.1 Get the code

Clone the repository or download it as a ZIP file and extract it to a folder of your choice. That folder is called the *suite folder* below.

### 2.2 Install the Python packages

**Conda (recommended on Windows).** Conda installs the geospatial libraries most reliably:

```bash
conda create -n rat_suite_env -c conda-forge python=3.12 geopandas rasterio fiona pyproj scipy matplotlib plotly folium contextily requests
conda activate rat_suite_env
```

**venv and pip.** From the suite folder:

```bash
python -m venv venv
venv\Scripts\activate        # Windows
source venv/bin/activate     # macOS / Linux
pip install .
```

`pip install .` installs the required packages only. The suite itself always runs from the suite folder.

### 2.3 Bridge and tunnel inventories (optional)

On Windows, double-click `NBI/download_nbi.bat`. It downloads the current National Bridge Inventory delimited file (about 150 MB) and the National Tunnel Inventory file from FHWA into the `NBI` folder. To use another year, change `YEAR` at the top of the batch file. Keep only one year's files in the folder, since every `.txt`, `.csv` and `.xml` file there is read.

The inventories are optional. Without them the suite still finds bridges and tunnels from the elevation data, logs a warning, and labels the spans from HPMS records only.

### 2.4 Socrata app token (optional)

The HPMS data are downloaded from the U.S. DOT data portal (Socrata). The 2024 HPMS data for all roads is available at: https://data.transportation.gov/Roadways-and-Bridges/HPMS-Spatial-All-Sections-2024/42um-tgh5. Downloads work without a token, but large ones are throttled. A free app token can be requested from the portal and entered in the GUI. Scripts run outside the GUI read the token from the `RAT_SOCRATA_TOKEN` environment variable.

---

## 3. Starting the suite

* **Windows:** double-click `Run_RAT_Suite.bat` in the suite folder. If you installed with conda, run the batch file from an Anaconda Prompt in which `rat_suite_env` is active, or the packages will not be found.
* **Any system:** from a terminal in the suite folder, with the environment active:

  ```bash
  python apps/rat_unified_gui.py
  ```

When the GUI opens it looks for the settings from the last run in `output/run_params.json` under the suite folder. If it finds the file, it restores those settings and lists in the Run Log any that differ from the current defaults. If a saved value is a default from an earlier version, it offers to update it. Settings saved anywhere else can be loaded with **Load Config**.

---

## 4. The Unified GUI, section by section

The GUI collects the settings, saves them to `run_params.json` in the output folder, and runs each processing script as a separate process. Progress appears in the **Run Log** at the bottom of the window. Statewide runs also open a small progress window.

### 4.1 Input Source

| Control | Use |
| :--- | :--- |
| **Use Socrata URL / Use Local File** | Choose where the HPMS data come from. |
| **Socrata URL** | The HPMS dataset on the U.S. DOT data portal. The default, `https://datahub.transportation.gov/resource/42um-tgh5.json`, is the 2024 national HPMS release. |
| **Reuse existing socrata_input_extract.csv** | Skips the download and reuses the extract saved in the output folder by the previous run. The State, source, and filters of each download are saved beside the extract (`socrata_input_extract_filters.json`); if they differ from the current settings, the extract is downloaded again. |
| **Local File** | A `.csv`, `.shp` or `.geojson` file of HPMS sections (see 4.1.1). |
| **State FIPS** | Two-digit State code (`53` for Washington, `11` for the District of Columbia). Leave blank, or enter `ALL`, to run every State. |
| **Reload State Factors** | Entering a State FIPS code fills the horizontal smoothing factor fields with that State's calibrated values (or the national defaults where the State has none). This button fills them again after they have been edited. Statewide runs apply each State's factors automatically. |
| **Socrata Token** | Optional app token (see 2.4). |
| **Functional System Filters** | Functional classes to process. FS 1–6 are on by default; Local roads (FS 7) are off. **Missing** adds records with no functional system, processed as Local (FS 7); it is off by default. In Missouri these records are always left out, because they include railroad lines. At least one functional system must be selected. |
| **Facility Type Filters** | HPMS facility types to process. The default, types 1 and 2, is mainline roadway only. **Missing** adds records with no facility type, processed as two-way roadway (2); it is off by default. At least one facility type must be selected. |

#### 4.1.1 Local files

A local file must have a route identifier column and line geometry. Common HPMS column names are recognized whatever their case:

| Field | Accepted names |
| :--- | :--- |
| Route | `route_id`, `RouteId`, `route`, `id` |
| Begin reference point | `begin_point`, `start_mp`, `bmp`, `begin`, `beg_mp` |
| End reference point | `end_point`, `end_mp`, `emp`, `end` |
| Functional system | `f_system`, `fsystem`, `func_sys` |
| Facility type | `facility_type`, `fac_type`, `facilitytype` |
| Structure type | `structure_type`, `struct_typ` |
| Shape ID | `shapeid`, `shape_id` |
| Geometry (CSV) | `WKT`, `geometry`, `shape`, `the_geom`, `line` (well-known text) |

Shapefiles and GeoJSON files in another coordinate system are reprojected to WGS 84 when read. Enter the State FIPS code for a local file whenever it is known; it selects the State's smoothing factors and bridge records. Without it, outputs are labeled `LOCAL` and national factors are used.

### 4.2 Directories

| Control | Use |
| :--- | :--- |
| **Output Directory** | Where results are written. Statewide outputs go to a subfolder `Output_State_XX`. |
| **DEM Directory** | Where elevation tiles are cached (default `elevation_cache` in the folder the GUI was started from). Keep this folder between runs: downloading tiles is the slowest part of a large run, and cached tiles are reused. The 1 m tiles are stored in its `align_1m_cache` subfolder. |

### 4.3 Output / Mode Selection

| Control | Use |
| :--- | :--- |
| **Run Alignment** | Statewide curve and grade analysis. |
| **Run Plan/Profile** | One route, set by **Plan/Profile Route ID**. **Load Routes** fills the list from the selected input. **Begin RP** and **End RP** (miles) limit the run to part of the route; leave them blank for the whole route. |
| **Run 4D Enrichment** | Elevation and reference point on every HPMS vertex. **Customize Output Fields** chooses which HPMS fields are carried into the output. |
| **Replace HPMS curve/grade classifications** | Available when Alignment and 4D Enrichment are both checked. The 4D output's CURVES_A–F and GRADES_A–F fields are filled from the RAT curve and grade tables instead of the values reported to HPMS. |

The modes can be combined. They run in the order Alignment, Plan/Profile, 4D Enrichment.

### 4.4 Parameters

The defaults suit most work. Change one group at a time and check the effect on a known route before a statewide run.

| Group | Field | Default | Effect |
| :--- | :--- | :---: | :--- |
| Horizontal | Densify Spacing (ft) | 5 | Point spacing along the route before smoothing. |
| | H Smooth Factor (FS 1) | State value | Horizontal smoothing for Interstates. Values for FS 2–7 are under Advanced Settings. |
| | H Min Delta (deg) | 3.5 | Smallest deflection angle kept as a curve. |
| | H Min Curve Length (ft) | 100 | Shortest horizontal curve kept. |
| Vertical | V Smooth Factor (all FS) | 10 | Vertical smoothing; one national value. |
| | V Min Curve Length (ft) | 200 | Shortest vertical curve kept. |
| | V Min Grade Change (%) | 0.5 | Smallest algebraic grade difference kept as a vertical curve. |
| Merge & Runtime | Enable Merging | off | Joins adjacent same-direction horizontal curves into compound curves. |
| | H Merge Gap Floor (ft), H Merge Gap (fraction of curve length) | 50, 0.25 | Largest gaps that merging bridges. |
| | Run validation automatically | on | Runs the validator after an alignment run (see 8.4). |
| | Open output folder when complete | on | |
| | Trim Curve Endpoints | on | Moves each detected curve's ends inward to where curvature becomes significant. |

**Advanced Settings** holds the remaining parameters: horizontal factors for FS 2–7, curve detection thresholds, the bridge and tunnel settings (`DIP_THRESHOLD_FT`, `STRUCTURE_EDGE_GRADE_PCT`, `BRIDGE_MAX_LEN_FT`, `TUNNEL_MAX_LEN_FT`, `STRUCTURE_MATCH_TOL_FT`), and the settings that divide long routes for parallel processing. Each is described in the Technical Manual. **Restore Defaults** in either window returns that window's fields to their defaults.

### 4.5 Output Formats

| Option | Produced by | Contents |
| :--- | :--- | :--- |
| CSV | all modes | Always produced. |
| GeoJSON | Alignment | Spatial copies of the curve and grade tables. |
| GeoPackage (GPKG) | Alignment, 4D Enrichment | Alignment: spatial copies of the curve and grade tables, and an intersections GeoPackage. 4D: the enriched sections with 3D geometry. |
| Shapefile | 4D Enrichment | The 4D output projected to UTM, for Blender and similar 3D tools. |
| Interactive HTML Map | Alignment | Three maps: curves, grades, and intersections. |
| Summary Dashboard HTML | Alignment | Charts summarizing the State's results. |
| Plan/Profile PDF | Plan/Profile | Engineering sheets (see 7.6). |
| Vertices Spatial File (large) | Alignment | Every densified point as a GeoPackage. Large for a whole State. |
| Structure Span Report | Alignment, Plan/Profile | Every bridge and tunnel span found: a CSV, and for Plan/Profile a PDF with a profile and a shaded-relief map of each span. |
| Profile Elevations every … ft | Plan/Profile | Prints profile and ground elevations under each profile view at the interval given. |
| Simplify Web Geometry | Alignment | Simplifies lines in the HTML maps (about 5 m) so large States open in a browser. The GeoJSON and GeoPackage files keep full detail. |

### 4.6 Buttons

* **Run Selected** starts the run.
* **Save Config** writes the current settings to `run_params.json` in the output folder without running.
* **Load Config** restores settings from any saved `run_params.json`.
* **Advanced Settings** and **Restore Defaults** are described in 4.4.

---

## 5. Running a job

### 5.1 One route: Plan/Profile

1. Choose the input and enter the State FIPS code.
2. Set the Output and DEM directories.
3. Check **Run Plan/Profile** only, click **Load Routes**, and pick the route. Enter Begin and End RP to limit the section.
4. Under Output Formats, check **Plan/Profile PDF** and, if wanted, **Structure Span Report** and **Profile Elevations**.
5. Click **Run Selected**.

A 20-mile Interstate section takes a few minutes once its elevation tiles are cached. The first run in an area also downloads the 1 m tiles it needs, so it can take 10-20 minutes or longer.

### 5.2 One State: Alignment

1. Choose the input and enter the State FIPS code.
2. Check **Run Alignment**.
3. Choose the output formats.
4. Click **Run Selected**.

The run downloads the State's HPMS sections, downloads every elevation tile the State needs, then processes the routes in parallel. The first run for a large State can take over a day, most of it on the first download of elevation tiles. Subsequent runs for a large State can still take several hours to complete. How many functional classes are selected and which types of runs are selected can have a significant impact on the length of the run. The validator runs at the end if that option is on.

### 5.3 Every State

Leave State FIPS blank, or enter `ALL`. Each State is downloaded and processed in turn into its own `Output_State_XX` folder. With 4D Enrichment selected, the per-State 4D files are also merged into one national file at the end. A national run takes several days.

### 5.4 Settings files

Every run writes `run_params.json` to the output folder. The processing scripts read their settings from it, and it records what a run used. Runs also write their parameters onto the maps, dashboards and PDF cover pages.

---

## 6. Outputs

`XX` is the State FIPS code, `YYYYMMDD` the run date, and `vN` a version number that increases when a run is repeated on the same day.

### 6.1 Alignment (`Output_State_XX/`)

| File | Contents |
| :--- | :--- |
| `alignment_horizontal_XX_YYYYMMDD_vN.csv` | One row per horizontal curve. |
| `alignment_vertical_XX_…csv` | One row per vertical curve. |
| `alignment_grades_XX_…csv` | Grade segments between vertical points of intersection, covering every processed mile. |
| `alignment_vertices_XX_…csv` | Every densified point, with elevation, grade, curve status and structure labels. |
| `alignment_section_scores_XX_…csv` | Each HPMS section with its predominant curve and grade class. |
| `alignment_order_audit_XX_…csv` | One row for every route, with counts of reference point breaks, gaps between records, and records that do not join end to start, and whether the route needs review. For review only. |
| `alignment_structure_spans_XX_…csv` | Bridge and tunnel spans (Structure Span Report). |
| `.geojson` / `.gpkg` copies | Spatial versions of the curve and grade tables. |
| `alignment_intersections_XX_…gpkg` | Crossing points between routes, classed At-Grade, Grade-Separated, or Unknown Clearance. |
| `alignment_map_XX_…html`, `…_grades.html`, `…_intersections.html` | Interactive maps. |
| `alignment_dashboard_XX_…html` and `chart_*.png` | Summary dashboard. |
| `qa_validation_report_XX_YYYYMMDD_vN.txt` | Validator report, named after the files it checked. |

### 6.2 Plan/Profile (output folder)

All files share the name `plan_profile_<route>_MP_<begin>_to_<end>`:

| Suffix | Contents |
| :--- | :--- |
| `_vertices.csv` | Every point along the route (about every 5 ft). |
| `_horizontal.csv`, `_vertical.csv`, `_grades.csv` | Curve and grade tables. |
| `_cross_sections.csv`, `_cross_section_profiles.csv` | Cross-section measurements and the ground profile of each section. |
| `_nearby_lines.csv` | Other HPMS lines within about 400 ft (other direction, ramps, crossing roads). |
| `_structure_spans.csv`, `_structure_spans.pdf` | Bridge and tunnel spans. |
| `_analytics_dashboard.html` | Interactive charts of curvature, profile, sight distance and ride quality. |
| `_calibration_dashboard.html` | A check of the horizontal smoothing factor on this route's own sample. Informational only. |
| `_run_parameters.json` | The parameters the run used. |
| `.pdf` | Plan and profile sheets. |

### 6.3 4D Enrichment (`Output_State_XX/`)

| File | Contents |
| :--- | :--- |
| `hpms_4d_production_XX_…csv` | The HPMS sections with a `WKT_ZM` column (longitude, latitude, elevation, reference point at every vertex) and `Elevation_Source`. |
| `hpms_4d_production_XX_…gpkg` | The same with 3D geometry (GeoPackage option). |
| `hpms_4d_blender_export_XX_…shp` | Shapefile in UTM, for 3D tools (Shapefile option). |
| `hpms_4d_national_YYYYMMDD.csv` | All States merged (national runs only). |

`Elevation_Source` is `smoothed` for sections taken from the smoothed route profile and `raw_fallback` where the route could not be smoothed (usually very short pieces) and the elevation model was sampled directly.

---

## 7. Reading the results

### 7.1 Classes

Every A–F scale runs from A (mildest) to F (most severe).

| Item | Basis | A | B | C | D | E | F |
| :--- | :--- | :---: | :---: | :---: | :---: | :---: | :---: |
| Horizontal curve (`Bin`) | Degree of curve, HPMS thresholds | < 3.5° | 3.5–5.4° | 5.5–8.4° | 8.5–13.9° | 14.0–27.9° | ≥ 28° |
| Grade (`Grade_Bin`) | Percent grade, HPMS Item 45 | < 0.5 | 0.5–2.4 | 2.5–4.4 | 4.5–6.4 | 6.5–8.4 | ≥ 8.5 |
| Vertical curve (`VC_Class`) | Design speed supported by stopping sight distance (mph) | ≥ 70 | 60–65 | 50–55 | 40–45 | 30–35 | ≤ 25 |

Vertical curves are classed with the AASHTO stopping sight distance model: a 3.5 ft eye height and 2.0 ft object over crests, and 2.0 ft headlights with a 1° beam on sags. `SSD_Provided_Ft` is blank where the curve does not limit sight distance. `Speed_Ref_Mph` is the HPMS speed limit at the curve, or a default by functional system where none is reported, and `Meets_Speed_Ref` compares it with the supported design speed. The comparison does not change the class.

### 7.2 Positions

* `Calibrated_Start_MP` and `Calibrated_End_MP` give positions in HPMS reference points.
* `Start_Dist` and `End_Dist` are distances along the processed route in meters, used internally and for checking.
* `Part` identifies a continuous piece of a route. A route with a gap in its line work is processed as separate parts. A piece whose records jump over reference points that other records of the route carry (a record joined out of sequence) is also cut there into separate parts, so no two parts share reference points.
* `Grade_Pct` is positive where the road rises in the direction of increasing reference point.
* Left and Right curve directions are as seen traveling in the direction of increasing reference point.

### 7.3 Section scores

Each HPMS section gets the class covering most of its length (`H_Curve_Bin`, `V_Grade_Bin`). Tangent length counts as class A. Where two classes cover equal length, the more severe class is kept.

### 7.4 Bridges and tunnels

The Structure Span Report lists each span with its reference point limits, length, end elevations and grade across the span. `Source_Tier` records what supported it: an HPMS structure record (`1_HPMS`), an NBI/NTI record (`2_NBI`), or the elevation data alone (`3_DIP`). `Max_Raw_Departure_Ft` is the largest difference between the span line and the bare ground under a bridge or over a tunnel. `Open_End` marks a bridge that runs off the end of the route's data, for example at a State line; the deck is then held level at the one abutment in the data.

### 7.5 Flags to review

| Flag | Where | Meaning |
| :--- | :--- | :--- |
| `False_Valley_Warning` | Vertical curves | A sag curve lies on a bridge span. It may be real (a bridge in a valley) or a sign that the span was not fully repaired. Check the profile sheet. |
| `Line_Off_Road` | Plan/Profile tables and cross sections | Two or more cross sections in a row find no pavement at the HPMS line, and the road centerline far from it. The line probably follows an old alignment. Curves and grades there describe the ground beside the road; check the line against current imagery. The log gives the reference point range and the PDF marks the stretch "LINE OFF ROAD?". |
| Unknown Clearance | Intersections | Two routes cross with 5–10 ft of elevation difference: not clearly at grade or grade-separated. |
| Sight distance under 400 ft | Plan/Profile sheets and dashboards | Points where available sight distance is under a fixed 400 ft. |

### 7.6 Plan and profile sheets

The PDF opens with a cover page listing the route, reference point range, run date and every parameter the run used, with an overview map. When the 1 m tiles are cached, a shaded-relief terrain overview follows, with section pages for long routes.

Each sheet covers 1,500 ft of road:

* **Plan view (top).** True scale, the route turned to run left to right, with shaded relief behind it. It shows the smoothed centerline (black), the HPMS line (red dashes), curves (blue left, red right) with call-outs, reference point ticks every 100 ft, cross-section stations, other HPMS routes labeled by Route ID ("over" or "under" where they cross), a north arrow and a scale bar. The inset shows the sheet's location on aerial imagery.
* **Profile view (bottom).** The repaired, smoothed profile (black) over the raw elevation model (red dashes), with vertical curves (orange crests, purple sags) and their call-outs: type and class, K-value, length, grades in and out, sight distance and supported speed, and PVC/PVT reference points.

Cross-section sheets follow at the back, four sections per sheet, at the same horizontal and vertical scale. They show the bare ground across the road, measured lane slopes (arrows point downhill), foreslopes, and the measured superelevation through each curve. Lane slopes that may be side slope rather than pavement are shown in gray with "?" (`Inv_Note` and `Opp_Note` = Doubtful in the cross-section table). Where HPMS has a separate line for the other direction, the section is centered between the two lines' roadways even if HPMS reports a narrow median or none.

Several display options, such as relief, terrain pages, cross-section sheets and other routes, are set in the CONFIGURATION block at the top of `apps/rat_plan_profile_report_pdf.py`.

---

## 8. Tools for calibration and checking

The scripts in `tools/` are run from a terminal in the suite folder. Each has a CONFIGURATION block at the top; edit it, save, and run the script, for example:

```bash
python tools/rat_national_calibration.py
```

### 8.1 National calibration (`rat_national_calibration.py`)

Derives horizontal smoothing factors for each State and functional system from the HPMS line work, without downloading elevation data. It writes an audit CSV, the factors, a `_filled` copy of the factors with gaps filled, and a run log to `output/national_cv/`. To use new factors, copy the `_filled` file over `core/national_smoothing_factors.json`. A national run takes many hours.

### 8.2 Curve-class shares (`pull_national_curve_distribution.py`)

Run once per HPMS release, before calibrating. It computes the share of each curve class reported to HPMS by State and functional system, which the calibration uses to size its samples, and writes `core/national_curve_class_proportions.json`. Without that file the calibration uses larger, conservative samples.

### 8.3 Calibration diagnostics (`rat_calibration_diagnostics.py`)

Reads the calibration audit CSV and reports how often combinations failed to settle, how close settled results came to the consistency limit, and how much factors vary between States, with charts.

### 8.4 Results validator (`rat_results_validator.py`)

Checks output files for missing columns, impossible values, gaps or overlaps in grade segments, reference point order and complete section scores. The GUI runs it after alignment runs. It writes `qa_validation_report_XX_YYYYMMDD_vN.txt` beside the files it checked and ends with PASS or FAILED. Where RAT replaced the 4D curve and grade fields and a record's CURVES_A–F or GRADES_A–F do not add up to its section length, the report lists the first 10 such records (`MAX_LISTED_ROWS` near the top of the script).

### 8.5 Span check (`rat_structure_span_check.py`)

Re-runs the bridge and tunnel detection on existing plan/profile vertex files. It is useful for trying other bridge settings on a route without rerunning it or downloading anything.

---

## 9. Troubleshooting

| Symptom | Cause and remedy |
| :--- | :--- |
| The GUI does not open, or reports a missing module | The Python environment with the suite's packages is not active. Activate it, or start the GUI from a prompt where it is active (see 3). |
| "No rows loaded for State FIPS …" | The filters exclude every record for the State, or a local file has no records for it. Check the functional system and facility type selections (including **Missing**). |
| "No NBI/NTI structures loaded" | No inventory files in `NBI/`. Spans are still found; see 2.3 to add the files. |
| Socrata errors 429 or 5xx in the log | The portal is throttling or busy. The suite retries automatically; an app token reduces throttling. |
| "Skipped saving … Map" | An HTML map would exceed its size limit (500 MB). The same data are in the CSV and spatial files. Leave **Simplify Web Geometry** on. |
| "Could not write …, retrying" | OneDrive, antivirus or indexing briefly locked the file. The suite retries; output folders outside synced locations avoid this. |
| A route breaks into several `Part` values | The HPMS line work has gaps. Each piece is processed separately. |
| Too many short horizontal curves | Raise the H Smooth Factor for that functional system, or H Min Delta or H Min Curve Length, and check on a known route. |
| A bridge dip remains in a profile | See the bridge settings in Advanced Settings and the Technical Manual. `tools/rat_structure_span_check.py` tries other settings quickly. |
| A run stops partway | Check the Run Log for the last State or route. Rerun that State; cached elevation tiles are reused. |
| "Plan/Profile PDF failed. Exit code 1" (the Run Log ends with "PermissionError: [Errno 13] Permission denied" and the PDF path) | Check to see if the PDF from a previous run for that route is still open. |

---

## 10. Data security

`run_params.json` holds the Socrata token and local folder paths in plain text. Do not share it or add it to a repository; the suite's `.gitignore` excludes it, along with output folders and elevation tiles. When sending results to others, send the output files, not the settings file.

---

*RAT Suite v3.3 — Federal Highway Administration, Office of Highway Policy Information. Released to the public domain under CC0 1.0 Universal; see `LICENSE` and `DISCLAIMER.md`.*
