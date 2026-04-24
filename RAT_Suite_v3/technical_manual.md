# HPMS Roadway Alignment Tool (RAT) Suite v3
**Technical Manual (Detailed Practitioner Edition)**

## Executive Summary
The HPMS Roadway Alignment Tool (RAT) Suite v3 is a production-oriented geospatial analytics system that derives roadway alignment intelligence from HPMS geometry and elevation data. It is designed for statewide scale while still supporting route-level engineering review. 

The suite provides three operational capabilities:
* Network-wide alignment analytics (horizontal and vertical curve detection and severity classification)
* Route-level Plan & Profile generation (engineering-style visual review artifacts)
* 4D enrichment (Z/M-enabled geometry for GIS/CAD/3D downstream use)

RAT v3 was developed to address a practical challenge common to DOT datasets: HPMS roadway geometry is often fragmented, noisy, and inconsistent across jurisdictions and functional classes. The suite is therefore designed to be resilient to disjointed geometry and schema variability, while maintaining transparent analytical methods and repeatable outputs.

### How to Use This Manual
This manual is written for practitioners running the suite and technical reviewers validating its methods. To find what you need quickly, follow the matrix below.

**Quick Reference Guide**

| Your Role / Goal | Recommended Reading Path |
| :--- | :--- |
| **First-Time Setup & General Operation** | Overview, Step-by-Step Workflows, Output Columns, Troubleshooting |
| **Validating the Math & Analytics** | Core Geometric Methods, Units, Output Fields, QA/Validator Checks |
| **Generating Plan/Profile Sheets** | Plan/Profile Workflows, PDF Annotation Interpretation |
| **Creating 3D/4D Geometry** | 4D Workflows, 4D Output Fields, Downstream Usage |
| **Program Management & High-Level Review**| Executive Summary, Workflows, Limitations & Intended Use |

**Document Conventions**
To make the tool operationally friendly, parameter tuning guides in the Appendix are written in practical terms (*"Increase when..."*, *"Decrease when..."*). This allows analysts to adjust the suite's behavior without needing to reverse-engineer the underlying Python codebase.

**Scope and Intended Use Reminder**
RAT v3 is an engineering analytics and data-derivation suite intended for decision support, screening, and HPMS quality review. **It is not a replacement for:**
* Design standards
* Signed/sealed design deliverables
* Final field survey controls

Always apply appropriate validation when using RAT outputs for high-consequence applications.

---

## 1. Overview
The HPMS Roadway Alignment Tool (RAT) Suite v3 is an enterprise geospatial processing pipeline for deriving roadway alignment intelligence from HPMS geometry. It is designed to process fragmented roadway networks at scale, detect horizontal and vertical curves, generate plan/profile engineering sheets, and produce 4D-enriched geometry suitable for downstream workflows.

Unlike a design-authoring system, RAT is an **analytical derivation tool**: it estimates curve attributes from observed geometry and elevation data.

---

## 2. What Changed from v2 to v3 and Why It Matters
Version 3 moves from "multiple independently evolving tools" to a unified architecture with a shared core engine and a single orchestrating GUI. 

### 2.1 Architectural & Computational Efficiencies
To process tens of thousands of fragmented routes at a statewide scale, the v3 architecture introduces several critical computer science optimizations that drastically reduce processing time and hardware strain.

* **Subprocess Memory Isolation (Bypassing the GIL):** Python is historically limited by the Global Interpreter Lock (GIL) and struggles to release memory when processing massive DataFrames. v3 solves this by completely decoupling the interface from the math. The Unified GUI acts only as a lightweight dispatcher that triggers the CLI modules in temporary, isolated memory environments. Once a batch finishes processing, that memory block is immediately dumped, eliminating memory-leak crashes.
* **Algorithmic Time Complexity (cKDTree Spatial Indexing):** In the 4D Enricher, mapping thousands of fragmented 2D vertices to a continuous 3D elevation profile requires massive data correlation. v3 utilizes a Metric `cKDTree` (a highly optimized spatial indexing algorithm). This organizes the geometry into a mathematical search tree, dropping the search time complexity and instantly snapping coordinates to their nearest 3D neighbor.
* **Unified Analytical Core (Compute-Once Architecture):** v3 routes all modules through a single, shared analytical core (`rat_core.py`). A route's raw geometry is projected to a flat UTM plane, densified, and smoothed via splines exactly once, cutting required processing overhead in half.

---

## 3. Suite Components

### 3.1 Unified GUI (`rat_unified_gui.py`)
The GUI is the orchestrating command center. It does not perform heavy geometry math directly; instead, it gathers inputs, writes a `run_params.json` payload, and launches CLI modules in isolated subprocesses. By passing the parameters as a JSON payload to a disconnected subprocess, the suite strictly isolates the massive memory arrays required to process statewide datasets from the user interface.

### 3.2 Mathematical & Geospatial Core (`rat_core.py`)
This is the central processing brain of the suite. It contains zero GUI or plotting code. It manages the dynamic reprojection of WGS84 coordinates into localized, flat UTM zones for highly accurate distance calculations. It houses the `UnivariateSpline` decoupling logic, the calculus-based horizontal curvature extraction, and the vertical parabolic polynomial regression models.

### 3.3 Master Alignment CLI (`rat_alignment_cli.py`)
The bulk processing orchestrator for network-wide analytics. It feeds fragmented geometries into the Core engine, compiles horizontal and vertical curve dictionaries into master Pandas DataFrames, and serializes these arrays into standard CSVs and spatial formats (GeoJSON, GPKG, SHP).

### 3.4 Plan & Profile CLI + PDF Renderer
This two-part module generates highly detailed, route-specific engineering artifacts.
* **`rat_plan_profile_cli.py`:** Mathematically tracks cumulative linear distance across disjointed physical gaps in the HPMS geometry, ensuring heavily fragmented routes plot continuously.
* **`rat_plan_profile_report_pdf.py`:** Uses the pre-processed elevation data to graph the vertical profile, and queries live REST endpoints for USGS aerial basemaps, dynamically applying affine rotation matrices to ensure the roadway geometry always flows horizontally across the final PDF sheet.

### 3.5 4D Enricher (`hpms_4d_enricher_cli.py`)
The topology generation module. It upgrades flat 2D linework into 3D/4D spatial geometries (`LINESTRING ZM`). It uses a highly optimized `scipy.spatial.cKDTree` index to snap fragmented raw 2D vertices back to the mathematically perfected 3D elevation profile generated by the core.

### 3.6 Validator (`rat_results_validator.py`)
The automated QA/QC inspector. It programmatically scans generated outputs before production delivery, checking for mathematical impossibilities (e.g., negative curve lengths) and validating severity bin categorical integrity.

---

## 4. Input Data and File Handling

### 4.1 FHWA Socrata API (Automated)
The easiest way to process a state is to pull directly from the FHWA full 2024 HPMS Socrata database.
* **Target State FIPS Code:** Enter the 2-digit state FIPS code (e.g., 06 for California).
* **Functional System Filters:** Target specific F-Systems (e.g., 1 for Interstate only).

### 4.2 Local HPMS Files (.shp, .geojson, .csv)
If your state has internally modified HPMS data, you can process local files using the Use Local HPMS File option.
* **On-the-Fly Auto-Reprojection:** If your local shapefile is saved in a custom Coordinate Reference System (CRS), the engine will automatically reproject the data to WGS84 in memory.
* **"Fuzzy Matching" Attribute Columns:** The engine uses a fuzzy-matching dictionary to read your attribute table. The following variables map directly to the FHWA Socrata file requirements: `line`, `route_id`, `begin_point`, `end_point`, `f_system`, and `urban_id`.

---

## 5. Core Geometric and Analytical Methods (Detailed)

### 5.1 The "Jagged GPS" Problem: Spline Smoothing
* **The Issue:** Raw roadway geometry in HPMS is often digitized by clicking points on a screen. If the model tried to calculate the radius of a curve using raw data, the math would interpret every tiny zigzag between GPS points as a separate hairpin turn.
* **The Solution (Decoupled Splines):** The RAT engine mathematically "irons out" the road using a Spline. It "decouples" the horizontal (X, Y) and vertical (Z) smoothing, meaning it can aggressively iron out GPS jitters side-to-side without flattening out a legitimate hill.

### 5.2 The "North Crossing" Problem: Deflection Angle (Delta) Unwrapping
* **The Issue:** While the RAT v3 engine uses calculus to calculate the radius of a curve, it must still look at the compass heading to calculate the Total Deflection Angle (Delta). If a road curving gently to the right crosses true North, its heading instantly jumps from 359° back to 1°, creating a false 358-degree deflection angle.
* **The Solution (Unwrapped Headings):** The engine uses "unwrapping." Instead of resetting at 360°, the math allows the compass to keep counting upward (e.g., 358°, 359°, 360°, 361°), calculating a perfectly accurate deflection angle.

### 5.3 The "River Dip" Problem: Topo Draping and Bridging
* **The Issue:** The RAT Suite generates vertical data by "draping" it over a 3D USGS Digital Elevation Model (DEM). However, DEMs represent "bare earth." When a highway crosses a bridge, the bare earth drops away. 
* **The Solution (Core Bridging & KDTree Mapping):**
  1. **Core Bridging (`rat_core.py`):** The core engine performs a "Valley Test." If the ground suddenly drops far below the topographic trendline (triggered by the `DIP_THRESHOLD_FT` deviation), the engine mathematically suspends the road in the air, interpolating a smooth bridge. Because this mathematical repair occurs centrally, all downstream modules automatically benefit.
  2. **4D KDTree Mapping (`hpms_4d_enricher_cli.py`):** The 4D Enricher uses a "Macro KDTree" to spatially index the perfect, bridged 3D profile generated by the core, and snaps fragmented 2D highway segments up onto that continuous profile.

### 5.4 Finding the Start and End Points: The "Steering Wheel" Concept
* **Horizontal:** The algorithm calculates the road's compass heading at user-defined intervals (default 10 feet). If the heading changes by more than the `H_MIN_HEAD_CHANGE` threshold, it drops a "Start RP" pin.
* **Vertical:** The algorithm looks at the uphill/downhill slope (Grade). When the slope starts bending, it drops the "Start RP" (PVC).

### 5.5 Calculating the Curve Geometrics
* **Horizontal:** It calculates mathematically perfect Radius using the circular arc formula: `R = (180 × Length) / (π × Δ)`.
* **Vertical:** It uses linear regression to measure the straight road leading into the curve (G1) and out of it (G2). It subtracts these to find the Algebraic Difference (A), and divides the Length by A to find the K-Value.

### 5.6 The "Diluted Average" Problem: Minimum Apex Radius
* **The Issue:** Real-world roads have spiral transitions. If the engine only calculates average radius, flat entry/exit transitions will dilute the curve's severity.
* **The Solution (Instantaneous Apex Radius):** The engine calculates instantaneous curvature at every 10-foot interval. It isolates the single highest curvature value to calculate the `Min_Radius_m` (Apex Radius). Severity Bins (A-F) are based exclusively on this sharpest apex.

### 5.7 Preserving the Ground Truth: LRS Proportional Calibration
* **The Solution:** After mathematical smoothing, the line becomes physically shorter. The engine calculates what percentage of the way along the smoothed line a curve exists, and maps those exact percentages back onto the original `Start_MP` and `End_MP` boundaries.

### 5.8 Directionality, Grades, and S-Curves
* **Horizontal Left vs. Right:** If the deflection angle from Point of Curvature to Point of Tangent is positive, it is a "Right" curve.
* **Compound and Reverse (S-Curves):** **If the user toggles the "Enable Merging" parameter**, the engine evaluates the `MERGE_GAP_FT` and fuses adjacent curves of the same direction. If disabled, curves are kept separate. S-Curves are always kept separate to preserve the inflection point.

### 5.9 Savitzky-Golay Buffer (+2 requirement)
The requirement of adding exactly two points beyond the `H_BASE_SMOOTH_WINDOW` is a strict mathematical constraint of the Savitzky-Golay filter (`scipy.signal`). It guarantees that even fragmented road segments possess enough coordinates to satisfy the mathematical requirement without crashing.

### 5.10 Calculus-Based Curvature (Horizontal Analysis)
The engine (`rat_core.py`) calculates the first and second gradients (derivatives) with respect to the spacing interval. The instantaneous curvature (κ) is calculated, allowing the engine to calculate a mathematically perfect radius (R = 1/κ) for every meter of the road.

### 5.11 Vertical Parabolic Fitting (Vertical Analysis)
The engine applies a second-degree polynomial regression (`numpy.polyfit`) to fit a true mathematical parabola. It derives the exact tangent slopes (Grades) at the precise Point of Vertical Curvature (PVC) and Point of Vertical Tangency (PVT) using the first derivative.

## 6. Units of Measurement – Metric Core
When configuring parameters and analyzing the output data, you will notice a mix of Metric (meters) and Imperial (feet/miles) units. This hybrid approach is intentional, designed to merge strict geospatial standards with standard US highway engineering practices.

### 6.1 The Metric Core
Under the hood, standard geographic information systems (GIS) and planar projections like UTM (Universal Transverse Mercator) natively operate using meters. When the RAT Suite processes a route, it temporarily projects the WGS84 Latitude/Longitude coordinates into a flat UTM plane to perform highly accurate distance and curvature math. Because the underlying Python spatial libraries (`shapely`, `scipy`, `pyproj`) expect metric inputs for these planar projections, all internal mathematics—including spline smoothing, curve radii calculations, and spatial distance checks—are executed strictly in meters. Keeping the core engine entirely metric prevents the compounding rounding errors that occur when constantly converting back and forth during heavy computations.

### 6.2 Imperial Units "On the Edges"
While the engine computes in metric, the suite is built to interact with users using standard US Imperial units "on the edges" (the inputs and outputs):
* **Inputs:** HPMS route measures (`Begin_Point` and `End_Point`) are always read and processed as Miles. All of the user-facing tuning parameters, such as the `MERGE_GAP_FT` or `V_MIN_OFFSET_FT`, are defined in feet so analysts can configure the tool using familiar highway engineering thresholds.
* **Outputs:** The Plan & Profile Generator automatically converts the metric geometry at the very end of the process to draw classic 100-foot stationing labels on the PDFs. Vertical curve K-Values and Algebraic Grade Differences are calculated to align with standard percentage and Imperial expectations. The scripts utilize hardcoded, high-precision conversion constants to seamlessly translate between the user’s Imperial inputs and the engine's Metric core.

---

## 7. Outputs and Their Intended Use

### 7.1 Alignment Outputs
* Horizontal and vertical curve tables (CSV)
* Optional geospatial exports (GeoJSON/GPKG/SHP)
* Interactive HTML map with curve/grade styling
* Dashboard HTML with summary tables/charts

### 7.2 Plan/Profile Outputs
* Vertex-by-vertex processed table
* Horizontal/vertical route curve tables
* Multi-page annotated PDF sheets

### 7.3 4D Outputs
* Production CSV with `WKT_ZM`
* GPKG with 3D geometry
* Projected SHP for Blender/CAD interoperability use cases

---

## 8. Practical Tuning Guidance (What to Adjust and Why)

* **Symptom: Too many tiny horizontal curves**
  * Increase horizontal smoothing factor
  * Increase minimum deflection threshold
  * Increase minimum curve length
* **Symptom: Real sharp curves are being under-detected**
  * Decrease horizontal smoothing stiffness
  * Decrease densify spacing
  * Lower minimum deflection/length thresholds
* **Symptom: Vertical output is noisy**
  * Increase vertical smoothing
  * Increase minimum vertical curve length
  * Increase minimum grade-change threshold
* **Symptom: Bridge dips remain in profile**
  * Tune trend window / dip threshold / max bridge span parameters

---

## 9. QA/QC and Validation Workflow
The validator (`rat_results_validator.py`) checks for both hard failures and warning-level anomalies.

* **Required checks (fail conditions):**
  * Required columns present
  * Positive lengths
  * `End_Point` > `Begin_Point` consistency
* **Warning-level checks:**
  * Nonpositive radius/K values
  * Invalid categorical fields (Dir, Type, Bin classes)
* **4D checks:**
  * `WKT_ZM` presence and parsability
  * Z/M value range summaries
  * NaN detection warnings

**Recommended practice:** Run alignment, run validator, review exceptions before publishing outputs.

---

## 10. Deep Dive into Output Columns and Deliverables
This chapter explains what the outputs mean, how to interpret them, and how to use them in workflows. It distinguishes between **core engineering indicators** (used for analysis decisions) and **diagnostic/support fields** (used for QA and context).

### 10.1 Alignment Outputs (Network-Scale)
The Alignment module produces route-wide horizontal and vertical curve datasets. Primary artifacts are `Alignment_horizontal_<timestamp>.csv` and `Alignment_vertical_<timestamp>.csv`. 

### 10.2 Universal Fields You Will See Often
* **`RouteId`:** Route identifier from source dataset after normalization.
* **`Start_Dist` / `End_Dist`:** Curve start/end positions along the processed route axis. *Note: These are expressed in meters, as they represent the internal metric geometric extent used for QA checks.*
* **`Length_m`:** Computed curve length in meters.
* **`Calibrated_Start_MP` / `Calibrated_End_MP`:** Reference point calibrated start/end values mapped back from chunk-relative distance.
* **`Part`:** Indicates disjoint route chunk index when route geometry is fragmented.

### 10.3 Horizontal Curve Output Fields
* **`Radius_m`:** Representative radius estimate for the detected curve segment.
* **`Min_Radius_m`:** Sharpest local radius along the segment (apex behavior).
* **`Delta`:** Deflection angle across curve span (degrees).
* **`Dir`:** Direction relative to route digitization (`Left` or `Right`).
* **`Bin`:** Severity class (A–F), derived from curvature/radius classification logic.
* **`Merge_Status`:** `Simple` or `Compound`, depending on whether merge logic combined adjacent spans.

> **Interpretation Note:** High Delta combined with low `Radius_m` generally indicates sharper, more operationally significant curves. `Bin` is categorical, not design speed. Direction depends on original digitization direction.

### 10.4 Vertical Curve Output Fields
* **`Grade_In` / `Grade_Out`:** Estimated incoming and outgoing grades (%).
* **`Alg_Diff`:** Algebraic grade difference (signed).
* **`K_Value`:** Approximate K-value.
* **`Type`:** `CREST` or `SAG`.
* **`E`:** Vertical offset metric used in significance filtering logic.
* **`Grade_Bin`:** Severity classification (A–F) based on grade-change magnitude logic.

> **Interpretation Note:** Higher absolute `Alg_Diff` generally indicates a stronger vertical transition. K_Value interpretation depends on context (route class, speed environment, terrain).

### 10.5 Plan/Profile Vertices Output
* **`Reference point`:** Reference point value interpolated across selected route/chunk bounds.
* **`Dist_Ft`:** Continuous distance axis in feet for plotting.
* **`Lon` / `Lat`:** Smoothed coordinate output (WGS84).
* **`Elev_Ft`:** Smoothed elevation profile (feet).
* **`Raw_Lon` / `Raw_Lat`:** Raw (pre-smoothed) coordinate reference.
* **`Elev_Raw_Ft`:** Raw DEM-based elevation before full profile smoothing.

### 10.6 Plan/Profile PDF Content Interpretation
The PDF renderer (`rat_plan_profile_report_pdf.py`) overlays analytical results onto engineering-style sheets. Use the PDF output for verifying the results from the Alignment process or for route-level engineering screening where visual cross-checking is required.

### 10.7 4D Enrichment Outputs (Z/M-Enabled Geometry)
The 4D module generates geometry with elevation (Z) and measure (M) semantics. `WKT_ZM` is the most portable serialized artifact for downstream parsing. Blender/CAD workflows generally perform better with projected metric geometry (hence the dedicated SHP export path).

### 10.8 Interactive HTML Map & Dashboard
The HTML map provides a fast QA/communication layer, ideal for quick statewide pattern scans and stakeholder visualization. The Dashboard provides summary charts for executive/management briefing snapshots and quick quality pulses.

### 10.9 QA Exceptions Output
Treat the QA Exception export as a triage list to investigate clusters and distinguish data-quality issues from parameter-induced artifacts.
* **`FAIL`:** Required integrity checks failed.
* **`PASS with warnings`:** Required checks pass, but warning-level anomalies exist.
* **`PASS clean`:** Required checks pass with no warning indicators.

---

## 11. Data and Algorithmic Anomalies
Because the RAT Suite applies strict mathematical models to highly variable, human-digitized HPMS inputs, certain geometric digitization practices will predictably trigger algorithmic anomalies. 

### 11.1 Orthogonal "Stair-Step" Digitization
* **The Anomaly:** Digitizers occasionally draw curved local roads using abrupt, 90-degree orthogonal clicks.
* **Algorithmic Response:** The engine may interpret severe stair-steps as a series of alternating left/right reverse curves rather than a single sweeping arc.
* **Analytical Mitigation:** Significantly lower the `H_SMOOTH_FACTOR` (e.g., to 500) to allow the spline maximum flexibility, or rely on the `MERGE_GAP_FT` logic to fuse the resulting micro-curves.

### 11.2 GPS Multipath Jitter and "Micro-Curves"
* **The Anomaly:** Mobile data collection rigs driving through dense urban canyons frequently experience GPS multipath errors, resulting in linework that visually "vibrates".
* **Algorithmic Response:** The second derivative of a vibrating line will trigger dozens of microscopic, high-severity (Class F) false-positive curves.
* **Analytical Mitigation:** Increase `H_SMOOTH_FACTOR` if an output dataset contains an improbable density of curves under 30 meters.

### 11.3 LiDAR Water-Body Artifacts (False Valleys)
* **The Anomaly:** DEM artifacts over large bodies of water where LiDAR pulses were scattered create artificial, deep "pits" in the bare-earth model.
* **Algorithmic Response:** If the artificial pit exceeds the `BRIDGE_MAX_LEN_FT`, the engine mathematically plunges the roadway profile into the water.
* **Analytical Mitigation:** Increase the `TREND_WINDOW_FT` to ensure the trendline remains anchored to the shorelines rather than the water surface.

### 11.4 Collinear Redundancy
* **The Anomaly:** Some datasets feature hundreds of vertices densely packed along a perfectly straight line.
* **Algorithmic Response:** The denominator of the curvature equation approaches zero, risking a "Divide by Zero" crash.
* **Analytical Mitigation:** The RAT Core utilizes a strictly constrained `densify_coords_line` function that redistributes vertices at mathematically guaranteed intervals, sanitizing collinear redundancies.

### 11.5 The "Overpass Z-Spike" (False Crests)
* **The Anomaly:** Exceptionally wide overpasses are occasionally misinterpreted by the LiDAR classification algorithm as solid ground. Raw elevation data will violently spike upward by 20 to 50 feet.
* **Algorithmic Response:** The engine will attempt to fit an extremely sharp, high-severity CREST curve to the spike.
* **Analytical Mitigation:** Increase the `V_SMOOTH_FACTOR`. This stiffer tension forces the spline to "punch straight through" the artificial DEM artifact, ignoring the overpass entirely.

---

## Appendix A. Parameter Reference & Tuning Guide (v3)
*How to use this appendix: Start with defaults. Tune only one group at a time, validate on benchmark routes, then scale to statewide processing.*

**Table 1: Core Spacing & Smoothing**

| Parameter | Default | Units | Primary Effect |
| :--- | :--- | :--- | :--- |
| `DENSIFY_SPACING_FT` | 10 | ft | Interpolation interval before analytics |
| `H_SMOOTH_FACTOR` | 4500 | factor | Horizontal stiffness (FS 1–2 baseline) |
| `V_SMOOTH_FACTOR` | 4500 | factor | Vertical smoothing stiffness (FS 1–2 baseline) |
| `H_BASE_SMOOTH_WINDOW` | 21 | points | Heading smoothing window |

**Table 2: Horizontal Curve Detection**

| Parameter | Default | Units | Primary Effect |
| :--- | :--- | :--- | :--- |
| `H_MIN_DELTA` | 3.5 | deg | Minimum total deflection to keep curve |
| `H_MIN_CURVE_LENGTH_FT`| 100 | ft | Minimum horizontal curve length |
| `H_MAX_RADIUS_FT` | 165000| ft | Upper radius considered as curve |

**Table 3: Vertical Curve Detection**

| Parameter | Default | Units | Primary Effect |
| :--- | :--- | :--- | :--- |
| `V_MIN_CURVE_LENGTH_FT`| 200 | ft | Minimum vertical curve length |
| `V_MIN_GRADE_CHANGE` | 0.5 | % | Minimum algebraic grade difference |
| `V_VC_THRESHOLD` | 0.002 | rate | Trigger sensitivity for VC candidates |
| `V_MIN_OFFSET_FT` | 0.10 | ft | Minimum vertical offset significance |

**Table 4: Bridging / Profile Repair Controls**

| Parameter | Default | Units | Primary Effect |
| :--- | :--- | :--- | :--- |
| `TREND_WINDOW_FT` | 1000 | ft | Baseline window for profile trend |
| `DIP_THRESHOLD_FT` | 6.5 | ft | Deviation below trend that flags valley dip |
| `BRIDGE_MAX_LEN_FT` | 8200 | ft | Max interpolation span for bridge correction |

**Table 5: Merge and Post-Processing**

| Parameter | Default | Units | Primary Effect |
| :--- | :--- | :--- | :--- |
| `ENABLE_MERGE` | False | bool | Merge adjacent same-type curves |
| `MERGE_GAP_FT` | 600 | ft | Horizontal merge gap tolerance |
| `V_MERGE_GAP_FT` | 1500 | ft | Vertical merge gap tolerance |

**A.6 Functional-System-Specific Smoothing Overrides**
RAT v3 supports different smoothing defaults by functional system class:
* **FS 1–2:** higher stiffness
* **FS 3:** moderate-high
* **FS 4–5:** moderate
* **FS 6–7:** lower stiffness / higher flexibility

---

## Appendix B. Quick Tuning Playbooks (Recommended)

**Playbook 1: “Too many curves statewide”**
* Increase `H_SMOOTH_FACTOR`
* Increase `H_MIN_DELTA`
* Increase `H_MIN_CURVE_LENGTH_FT`
* Revalidate on sample corridors before full rerun

**Playbook 2: “Missing curves on lower-speed networks”**
* Decrease `DENSIFY_SPACING_FT`
* Decrease `H_SMOOTH_FACTOR` (or FS-specific override)
* Decrease `H_MIN_CURVE_LENGTH_FT`
* Spot-check against known geometry

**Playbook 3: “Bridge dips in vertical/profile outputs”**
* Increase `TREND_WINDOW_FT`
* Decrease `DIP_THRESHOLD_FT` modestly
* Review `BRIDGE_MAX_LEN_FT`
* Re-run targeted bridge corridors and inspect profiles