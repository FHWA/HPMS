# HPMS Roadway Alignment Tool (RAT) Suite v3.2
## Technical Manual

**Developed by:** Federal Highway Administration (FHWA), Office of Highway Policy Information

**Version:** 3.2

**Status:** Public Domain - CC0 1.0 Universal

---

## Executive Summary

The HPMS Roadway Alignment Tool (RAT) Suite v3.2 is a production-scale geospatial analytics system for deriving roadway alignment characteristics from HPMS geometry and USGS elevation data. The suite is designed for statewide processing and can handle tens of thousands of fragmented route segments while producing the geometric detail required for route-level engineering review.

The suite provides three operational capabilities:

* **Network-Wide Alignment Analytics.** Calculus-based horizontal and vertical curve detection, parameterized by functional system, with severity classifications and summary dashboards.
* **Route-Level Plan and Profile Generation.** Automated production of annotated engineering-style review sheets for corridor studies and spatial quality assurance.
* **4D Geometry Enrichment.** Conversion of 2D HPMS linework into Z/M-enabled geometry for integration with GIS, CAD, and 3D digital twin workflows.

HPMS roadway geometry is frequently fragmented, noisy, and geometrically inconsistent. The RAT Suite addresses these characteristics through a shared metric processing core, context-aware per-functional-system smoothing calibrated from a national parameter sweep, dynamic gap-bridging for bridge and water-body crossings, and pre-processing filters for facility type and functional system. These mechanisms produce repeatable, mathematically consistent outputs while preserving sensitivity to legitimate geometric variation.

### How to Use This Manual

| Goal | Recommended Sections |
| :--- | :--- |
| First-time setup and operation | Overview, Practical Tuning Guidance, Output Columns, Known Algorithmic Anomalies |
| Validating methods and mathematics | Core Geometric Methods, Units, Output Fields, QA/Validator |
| Generating plan and profile sheets | Plan/Profile Workflows, PDF Annotation Interpretation |
| Creating 3D/4D geometry | 4D Workflows, 4D Output Fields |
| National calibration and smoothing factors | Section 3.7, Appendix A.6 |
| Program management and policy review | Executive Summary, Practical Tuning Guidance, Limitations |

**Scope and Intended Use**

The RAT Suite is an engineering analytics and data-derivation tool for decision support, screening, and HPMS quality review. It is not a replacement for design standards, signed and sealed design deliverables, or final field survey controls. Apply appropriate validation when using RAT outputs in high-consequence applications.

---

## RAT Suite v3.2 - File repository structure

```
RAT_Suite_v3.2/
├── apps/
│   ├── rat_unified_gui.py
│   ├── rat_alignment_cli.py
│   ├── rat_plan_profile_cli.py
│   ├── rat_plan_profile_report_pdf.py
│   ├── hpms_4d_enricher_cli.py
│   ├── rat_national_calibration_cli.py
│   └── rat_results_validator.py
├── core/
│   ├── rat_core.py
│   ├── national_smoothing_factors.json
│   └── calibration_audit.csv
├── docs/
│   └── RAT_Suite_v3_2_Technical_Manual.md
├── LICENSE
├── README.md
├── DISCLAIMER.md
├── Run_RAT_Suite.bat
└── pyproject.toml
```

---

## Table of Contents

- [Executive Summary](#executive-summary)
- [Repository Structure](#rat-suite-v32---file-repository-structure)
- [1. Overview](#1-overview)
- [2. What Changed in Version 3.2](#2-what-changed-in-version-32)
  - [2.1 National Smoothing Factor Calibration](#21-national-smoothing-factor-calibration-major-change)
  - [2.2 Updated Default Smoothing Factors](#22-updated-default-smoothing-factors)
  - [2.3 Enhanced Summary Dashboard](#23-enhanced-summary-dashboard)
  - [2.4 Architectural and Code Quality Improvements](#24-architectural-and-code-quality-improvements)
- [3. Suite Components](#3-suite-components)
  - [3.1 Unified GUI](#31-unified-gui-rat_unified_guipy)
  - [3.2 Mathematical and Geospatial Core](#32-mathematical-and-geospatial-core-rat_corepy)
  - [3.3 Alignment CLI](#33-alignment-cli-rat_alignment_clipy)
  - [3.4 Plan and Profile CLI](#34-plan-and-profile-cli-rat_plan_profile_clipy)
  - [3.5 Plan and Profile PDF Renderer](#35-plan-and-profile-pdf-renderer-rat_plan_profile_report_pdfpy)
  - [3.6 4D Enricher](#36-4d-enricher-hpms_4d_enricher_clipy)
  - [3.7 National Calibration Engine](#37-national-calibration-engine-rat_national_calibration_clipy)
  - [3.8 Validator](#38-validator-rat_results_validatorpy)
- [4. Input Data and File Handling](#4-input-data-and-file-handling)
  - [4.1 FHWA Socrata API](#41-fhwa-socrata-api)
  - [4.2 Local HPMS Files](#42-local-hpms-files-shp-geojson-csv)
- [5. Core Geometric and Analytical Methods](#5-core-geometric-and-analytical-methods)
  - [5.1 Route Stitching and Geometry Consolidation](#51-route-stitching-and-geometry-consolidation)
  - [5.2 Spline Smoothing](#52-spline-smoothing)
  - [5.3 Heading Unwrapping](#53-heading-unwrapping)
  - [5.4 Bridge and Water-Body Profile Repair](#54-bridge-and-water-body-profile-repair)
  - [5.5 Curve Endpoint Detection](#55-curve-endpoint-detection)
  - [5.6 Curve Geometry Computation](#56-curve-geometry-computation)
  - [5.7 Minimum Apex Radius and Severity Classification](#57-minimum-apex-radius-and-severity-classification)
  - [5.8 Linear Reference Proportional Calibration](#58-linear-reference-proportional-calibration)
  - [5.9 Directionality and Compound Curves](#59-directionality-and-compound-curves)
  - [5.10 Savitzky-Golay Buffer Requirement](#510-savitzky-golay-buffer-requirement)
  - [5.11 Calculus-Based Horizontal Curvature](#511-calculus-based-horizontal-curvature)
  - [5.12 Vertical Parabolic Fitting](#512-vertical-parabolic-fitting)
  - [5.13 Functional System Scaling](#513-functional-system-scaling)
- [6. Units of Measurement](#6-units-of-measurement)
  - [6.1 Metric Core](#61-metric-core)
  - [6.2 Imperial User Interface](#62-imperial-user-interface)
- [7. Outputs and Intended Use](#7-outputs-and-intended-use)
  - [7.1 Alignment Outputs](#71-alignment-outputs)
  - [7.2 Plan and Profile Outputs](#72-plan-and-profile-outputs)
  - [7.3 4D Enrichment Outputs](#73-4d-enrichment-outputs)
  - [7.4 Calibration Outputs](#74-calibration-outputs)
- [8. Practical Tuning Guidance](#8-practical-tuning-guidance)
  - [8.1 Symptom-Based Adjustments](#81-symptom-based-adjustments)
  - [8.2 Plan View and Profile View Display Adjustment](#82-plan-view-and-profile-view-display-adjustment)
- [9. QA/QC and Validation Workflow](#9-qaqc-and-validation-workflow)
- [10. Output Column Reference](#10-output-column-reference)
  - [10.1 Universal Fields](#101-universal-fields)
  - [10.2 Horizontal Curve Fields](#102-horizontal-curve-fields)
  - [10.3 Vertical Curve Fields](#103-vertical-curve-fields)
  - [10.4 Plan and Profile Vertex Fields](#104-plan-and-profile-vertex-fields)
  - [10.5 4D Enrichment Fields](#105-4d-enrichment-fields)
  - [10.6 Calibration Audit Fields](#106-calibration-audit-fields)
- [11. Interactive Map and Dashboard](#11-interactive-map-and-dashboard)
  - [11.1 HTML Alignment Map](#111-html-alignment-map)
  - [11.2 Summary Dashboard](#112-summary-dashboard)
  - [11.3 Plan and Profile Sensitivity Dashboard](#113-plan-and-profile-sensitivity-dashboard)
- [12. Known Algorithmic Anomalies](#12-known-algorithmic-anomalies)
  - [12.1 Orthogonal Stair-Step Digitization](#121-orthogonal-stair-step-digitization)
  - [12.2 GPS Multipath Jitter](#122-gps-multipath-jitter)
  - [12.3 LiDAR Water-Body Artifacts](#123-lidar-water-body-artifacts)
  - [12.4 Collinear Vertex Redundancy](#124-collinear-vertex-redundancy)
  - [12.5 Overpass Z-Spikes](#125-overpass-z-spikes)
  - [12.6 Curve Endpoint Displacement](#126-curve-endpoint-displacement)
  - [12.7 DEM Micro-Undulations](#127-dem-micro-undulations)
  - [12.8 Vertical Curve Classification at Bridge Anchor Points](#128-vertical-curve-classification-at-bridge-anchor-points)
  - [12.9 Validator Coverage for New Output Files](#129-validator-coverage-for-new-output-files)
- [13. Interpreting the Calibration Audit](#13-interpreting-the-calibration-audit)
  - [13.1 Where to Start](#131-where-to-start)
  - [13.2 Understanding Selection Methods](#132-understanding-selection-methods)
  - [13.3 Reading the Confidence Score](#133-reading-the-confidence-score)
  - [13.4 Identifying Outliers with Deviation from Default](#134-identifying-outliers-with-deviation-from-default)
  - [13.5 Using the RMSE Columns for Validation](#135-using-the-rmse-columns-for-validation)
  - [13.6 Making Manual Overrides](#136-making-manual-overrides)
  - [13.7 Cross-State Consistency Check](#137-cross-state-consistency-check)
- [Appendix A. Parameter Reference](#appendix-a-parameter-reference)
  - [A.6 National Smoothing Factor Calibration](#a6-national-smoothing-factor-calibration)
  - [A.7 Parameter Tuning Guidance](#a7-parameter-tuning-guidance)
  - [A.8 Additional Adjustments](#a8-additional-adjustments)
- [Appendix B. Tuning Playbooks](#appendix-b-tuning-playbooks)
- [Appendix C. Glossary](#appendix-c-glossary)

---

## 1. Overview

The RAT Suite is an analytical derivation tool. It estimates operational curve attributes from observed, real-world geometry and elevation data rather than generating geometry from proposed design criteria. The suite is designed to ingest variable, human-digitized spatial networks and programmatically separate legitimate highway geometry from GPS multipath errors, digitization artifacts, and topographic noise.

Version 3.2 consolidates two prior development cycles into a stable, unified architecture. Version 3.0 established the shared core engine and orchestrating GUI. Version 3.1 attempted a rural/urban smoothing bifurcation that was superseded in 3.2 by a data-driven national calibration approach. Version 3.2 introduces the RAT National Calibration Engine and a nationally derived smoothing parameter dictionary, replacing the manual default values used in prior versions.

---

## 2. What Changed in Version 3.2

### 2.1 National Smoothing Factor Calibration (Major Change)

Prior versions used manually estimated smoothing factors. Version 3.2 introduces the **RAT National Calibration Engine** (`rat_national_calibration_cli.py`), which automatically derives optimal horizontal and vertical smoothing factors for each state and functional system through a data-driven sweep against HPMS geometry and USGS elevation data.

The calibration engine evaluates candidate factors across a defined sweep range and applies a two-stage selection algorithm:

* **Gate 1 (Geometric Safety Ceiling).** A candidate factor must not cause the smoothed alignment to deviate beyond defined RMSE and maximum-deviation thresholds for that functional system. These thresholds vary by functional system and are relaxed for states in mountainous terrain.
* **Elbow Detection (Diminishing Returns).** Among factors that pass Gate 1, the engine identifies the factor at the point of diminishing returns using the normalized Kneedle algorithm, the point of maximum perpendicular distance from the line connecting the first and last passing values. This selects the minimum stiffness that produces a geometrically stable alignment, avoiding both under-smoothing (noise retained) and over-smoothing (legitimate curves suppressed or curve endpoints displaced).

Results are stored in `national_smoothing_factors.json` in the `core/` directory. The `build_params()` function in `rat_core.py` automatically loads state-specific factors when a State FIPS code is provided, falling back to national defaults when no state entry is available.

A **calibration audit file** (`calibration_audit.csv`) accompanies the JSON and documents the selection method, confidence score, RMSE metrics, and override recommendations for each state/functional system combination.

### 2.2 Updated Default Smoothing Factors

The DEFAULTS block in `rat_core.py` has been updated to reflect calibrated national median values rather than pre-calibration placeholders. Default values now represent the recommended fallback when no state-specific JSON entry exists.

| Functional System | H Default | V Default |
| :--- | :---: | :---: |
| FS 1 — Interstate | 400 | 1000 |
| FS 2 — Other Freeways and Expressways | 200 | 1000 |
| FS 3 — Other Principal Arterial | 200 | 1200 |
| FS 4 — Minor Arterial | 200 | 400 |
| FS 5 — Major Collector | 200 | 600 |
| FS 6 — Minor Collector | 200 | 400 |
| FS 7 — Local | 200 | 400 |

### 2.3 Enhanced Summary Dashboard

The alignment CLI dashboard now includes four additional charts and improved system health reporting:

* **Curve density by functional system:** Curves per 100 route-miles, normalizing for network size differences across functional classes.
* **Cumulative severity distribution:** CDF curves showing the percentage of curves at or below each severity class for horizontal and vertical separately.
* **CREST vs. SAG breakdown by functional system:** Vertical curve type counts relevant to sight-distance screening.
* **Compound curve percentage by functional system:** The fraction of horizontal curves classified as compound, useful for identifying over-merging or legitimate spiral transitions.
* **System Health percentile bands:** The RMSE chart now shows P50, P80, and P95 per functional system rather than only the mean, distinguishing states where most routes are clean from those where outlier routes are driving the aggregate.

### 2.4 Architectural and Code Quality Improvements

- Centralized `fetch_socrata_state()` and `load_local_hpms()` functions in `rat_core.py`, eliminating duplicate implementations across CLI modules.
- Replaced the per-functional-system smoothing factor `if/elif` chain with a dict lookup for maintainability.
- Fixed O(n²) route filtering bottleneck in the alignment and enricher CLIs; replaced with `DataFrame.groupby()`.
- Corrected `SIMPLIFY_GEOMETRY` / `simplify_geometry` key casing inconsistency between the GUI and export functions.
- Corrected `page_mp_span` unbound variable risk in the PDF renderer.
- Added `geopandas` import required for local file processing in `rat_core.py`.

---

## 3. Suite Components

### 3.1 Unified GUI (`rat_unified_gui.py`)

The GUI is the primary user interface. It does not perform geometric processing; instead, it collects configuration inputs, writes a `run_params.json` parameter payload, and launches CLI modules as isolated subprocesses. Passing parameters through a JSON payload to a disconnected subprocess isolates the memory required for statewide processing from the user interface and ensures the interface remains responsive during long batch runs.

The GUI loads state-specific smoothing factors from `national_smoothing_factors.json` at run time and injects them into the parameter payload before invoking CLI modules.

### 3.2 Mathematical and Geospatial Core (`rat_core.py`)

The shared processing engine for the entire suite. This module contains no GUI or plotting code. It provides UTM coordinate projection, USGS DEM elevation retrieval, UnivariateSpline smoothing, calculus-based curvature analysis, vertical parabolic regression, KDTree spatial indexing, and the `build_params()` parameter resolution function.

`build_params()` resolves parameters in the following order, with later entries taking precedence:

1. `DEFAULTS`: National calibration fallback values
2. `national_smoothing_factors.json`: State-specific calibrated factors, applied when STATE_FIPS is provided
3. `user_params`: Explicit overrides from the GUI or CLI caller

### 3.3 Alignment CLI (`rat_alignment_cli.py`)

The batch processing module for network-wide curve detection. It feeds fragmented HPMS geometries into the core engine, aggregates horizontal and vertical curve results, and produces CSV output, optional spatial formats (GeoJSON, GPKG, SHP), an interactive HTML alignment map, and an HTML summary dashboard.

### 3.4 Plan and Profile CLI (`rat_plan_profile_cli.py`)

The route-specific data pre-processor for engineering sheets. It isolates a single route, stitches fragmented geometry, tracks cumulative linear distance across disjointed segments to prevent overlapping, and exports smoothed and raw vertex tables to CSV for consumption by the PDF renderer.

The CLI also generates an HTML sensitivity analysis dashboard by slicing the route into 1-mile segments and evaluating multiple smoothing factors to assist with parameter adjustment.

### 3.5 Plan and Profile PDF Renderer (`rat_plan_profile_report_pdf.py`)

Generates multi-page engineering plan and profile PDFs from pre-processed vertex and curve CSVs. The renderer fetches USGS aerial basemap tiles, applies affine rotation so that the alignment flows left-to-right across each sheet, and uses `Matplotlib GridSpec` to produce stacked plan (top-down) and profile (elevation) views with full curve annotations. Pages are scaled to a 1,500-foot plan length.

### 3.6 4D Enricher (`hpms_4d_enricher_cli.py`)

Upgrades 2D HPMS geometry to Z/M-enabled `LINESTRING ZM` format. The enricher constructs a continuous 3D macro-profile for each route using the core smoothing engine, then uses a metric `cKDTree` to snap fragmented 2D segments to the smoothed 3D profile. Output includes a production CSV with `WKT_ZM` values, a GeoPackage with 3D geometry, and a projected SHP for Blender and CAD workflows.

### 3.7 National Calibration Engine (`rat_national_calibration_cli.py`)

Derives optimal smoothing factors for each state and functional system from HPMS geometry and USGS elevation data. The engine downloads HPMS data via the Socrata API, slices routes into 1-mile segments, draws a statistically representative sample using Cochran's formula, and evaluates each candidate factor against Gate 1 deviation ceilings. Elbow detection identifies the factor at the point of diminishing returns among the passing candidates.

The engine produces two outputs:

* **`national_smoothing_factors.json`:** State and FS-keyed smoothing factor dictionary loaded automatically by `build_params()`.
* **`calibration_audit.csv`:** Per-search audit record documenting selection method, confidence score, RMSE metrics at the selected factor, deviation from national default, and an override recommendation flag.

### 3.8 Validator (`rat_results_validator.py`)

Automated QA/QC module. Scans alignment CSV and 4D enriched outputs for required column presence, mathematically invalid records (negative lengths, inverted distance ranges), and invalid categorical values. Reports hard failures and warning-level anomalies to the console with a final PASS/FAIL summary.

---

## 4. Input Data and File Handling

### 4.1 FHWA Socrata API

The primary input mode. The GUI connects to the FHWA HPMS Socrata database and filters by State FIPS code, functional system, and facility type before downloading data. The client applies a 120-second timeout per request and raises an error on non-2xx responses. The URL for the API is: `https://datahub.transportation.gov/resource/42um-tgh5.json`.

### 4.2 Local HPMS Files (.shp, .geojson, .csv)

Local files are supported through the Use Local File option. If a shapefile is stored in a non-WGS84 CRS, the engine automatically reprojects to WGS84 in memory. The column name mapper in `load_local_hpms()` uses a fuzzy-match dictionary to resolve common variations in attribute names for RouteId, Start_MP, End_MP, FSystem, UrbanID, and Facility_Type.

---

## 5. Core Geometric and Analytical Methods

### 5.1 Route Stitching and Geometry Consolidation

**The problem.** HPMS geometry is stored as a collection of independent tabular segments, each representing a discrete section of a route with its own `Start_MP`, `End_MP`, and geometry. A single Interstate route may be represented by thousands of individual segments. Processing these segments in isolation makes reliable curve detection impractical for two reasons:

* **Segment length.** It isn't unusual for there to be segments that are 0.10 miles or shorter. A segment of this length contains insufficient geometry for the smoothing spline to establish a stable alignment. The spline requires a meaningful run of coordinated vertices on either side of any given point to correctly characterize the heading at that point, without that context, the computed curvature reflects the local geometry of a single short segment rather than the continuous arc of the road.

* **Vertex distribution.** HPMS linework frequently exhibits uneven vertex spacing and localized vertex clumping within individual segments. The curvature algorithm relies on consistent vertex intervals to produce stable derivative estimates. Consolidating segments into a continuous route and redistributing vertices at a uniform interval (controlled by `DENSIFY_SPACING_FT`) eliminates both sparse sections where the spline has insufficient resolution and dense clusters where redundant collinear vertices produce near-zero curvature denominators.

**The approach.** Before any smoothing or curvature analysis is performed, the `stitch_linestrings_ordered()` function in `rat_core.py` consolidates all segments for a route into one or more continuous LineStrings. The only condition that causes a route to break into separate LineStrings is a spatial gap between consecutive segment endpoints. The snap tolerance used to determine whether two endpoints are close enough to join is `1e-6 degrees` (approximately 0.1 meters). Gaps larger than that result in a new LineString, and all downstream output for that route will carry a `Part` index to distinguish results from each contiguous portion. The stitching process:

1. Collects all segment geometries for the route and sorts them by Start_MP to establish the expected spatial order.

2. Evaluates the endpoint proximity of each segment against its neighbor. If the end of one segment is within a configurable snap tolerance of the start of the next, the two are joined.

3. Resolves directional inconsistency by comparing the endpoint of the preceding segment to both the start and end of the candidate segment. If the candidate's end point is closer than its start point, the segment is reversed before joining.

4. Where a gap between segments exceeds the snap tolerance — indicating a genuine discontinuity such as a missing segment, a route split at a state line, or an unbridged water crossing — the stitcher begins a new LineString rather than forcing a connection. The resulting output may therefore contain multiple LineStrings for a single route, each representing a contiguous portion of the alignment. Downstream processing treats each portion independently and assigns a `Part` index to all output records so that results from disjointed geometry can be distinguished.

**Why this matters for curve detection.** A smoothing spline fitted to a properly stitched continuous LineString produces heading and curvature values that reflect the actual roadway geometry. The same spline fitted to a raw fragmented segment produces values that are partially artifacts of where the segment happened to start and end. Route stitching is the prerequisite that makes all subsequent analysis geometrically meaningful.

**Relationship to linear referencing.** After stitching, the engine uses the Start_MP and End_MP values from the source segments to establish proportional linear reference positions along the stitched geometry. This mapping is described in Section 5.7.

**Note.** If routes are being split into an unexpectedly large number of parts, the snap tolerance may be too tight for the precision of the source data. This value can be adjusted by modifying the `snap_tol` default in `stitch_linestrings_ordered()` in `rat_core.py`.

### 5.2 Spline Smoothing

**The problem.** HPMS geometry is often digitized from screen clicks or GPS collection and contains micro-scale positional noise that would produce false-positive curves if fed directly to a curvature algorithm.

**The approach.** The core engine applies a `scipy.interpolate.UnivariateSpline` to the UTM-projected coordinates. Horizontal (X, Y) and vertical (Z) smoothing are decoupled, allowing independent stiffness control. The spline smoothing factor controls the trade-off between fidelity to the raw geometry and geometric smoothness. State and functional-system-specific factors derived by the National Calibration Engine replace the uniform defaults used in prior versions.

### 5.3 Heading Unwrapping

**The problem.** A road curving near true North will show a heading transition from approximately 359° to 1°, producing a false 358° deflection angle.

**The approach.** The engine applies heading unwrapping, allowing the compass value to accumulate continuously (e.g., 359°, 360°, 361°) rather than resetting at 360°. Deflection angles are computed from the unwrapped heading series.

### 5.4 Bridge and Water-Body Profile Repair

**The problem.** USGS DEMs represent bare earth. Where a highway crosses a bridge, the DEM surface drops to the streambed or valley floor, producing artificial dips in the elevation profile.

**The approach.** The core engine applies a valley test: if the road profile drops more than `DIP_THRESHOLD_FT` below the local topographic trend (estimated over `TREND_WINDOW_FT`), the engine suspends the road in the air and interpolates linearly across the gap. The maximum interpolation span is controlled by `BRIDGE_MAX_LEN_FT`. All downstream modules benefit from this repair because it is applied in the core before curve detection or 4D mapping.

### 5.5 Curve Endpoint Detection

**Horizontal.** The engine calculates the road's compass heading at the `DENSIFY_SPACING_FT` interval. When the heading rate of change exceeds `H_MIN_HEAD_CHANGE`, a curve candidate is initiated.

**Vertical.** The engine monitors the second derivative of the elevation profile. When the curvature of the grade exceeds `V_VC_THRESHOLD`, a vertical curve candidate is initiated.

### 5.6 Curve Geometry Computation

**Horizontal radius.** The engine computes instantaneous curvature κ at every point using the first and second spatial derivatives of the smoothed coordinates. Radius is derived as R = 1/κ. The reported `Radius_m` is the representative value across the curve span; `Min_Radius_m` is the minimum (apex) radius, which drives severity classification.

**Deflection angle.** Total deflection angle (Delta) is the absolute change in unwrapped heading from PC to PT.

**Vertical K-value.** The engine uses linear regression over the `REGRESSION_WINDOW_FT` window on each side of the vertical curve to estimate incoming grade G1 and outgoing grade G2. Algebraic difference A = G2 − G1. K-value = curve length / |A|.

### 5.7 Minimum Apex Radius and Severity Classification

**The problem.** Real-world curves include spiral transitions. Averaging radius across a curve dilutes the severity of the apex.

**The approach.** Severity bins (A–F) are assigned based on the minimum instantaneous radius (`Min_Radius_m`), not the mean radius. This approach is consistent with road safety analysis practices that identify the most restrictive geometric condition along the curve.

### 5.8 Linear Reference Proportional Calibration

Spline smoothing physically shortens the line. After smoothing, the engine maps curve positions back to the original linear reference system by computing what fraction of the total smoothed length each curve start and end position represents, then applying that fraction to the original `Start_MP` and `End_MP` range.

### 5.9 Directionality and Compound Curves

**Left/Right.** A positive deflection angle from PC to PT (heading increasing) corresponds to a Right curve.

**Compound curves.** When merge is enabled, the engine evaluates the gap between adjacent same-direction curves against `MERGE_GAP_FT` and fuses them into a compound curve. Reverse (S-curve) sequences are always kept separate to preserve the inflection point.

### 5.10 Savitzky-Golay Buffer Requirement

The Savitzky-Golay filter (`scipy.signal.savgol_filter`) used in heading smoothing requires that the input have at least `H_BASE_SMOOTH_WINDOW + 2` points. The engine enforces this constraint by skipping route chunks with insufficient geometry rather than adjusting the window to avoid degraded results.

### 5.11 Calculus-Based Horizontal Curvature

Curvature κ is derived from the first and second spatial derivatives of the smoothed coordinate sequence with respect to arc length. Radius R = 1/κ at each point. This approach produces a continuous curvature function from which both apex and mean radius can be extracted.

### 5.12 Vertical Parabolic Fitting

The engine applies second-degree polynomial regression (`numpy.polyfit`) to the smoothed elevation profile within each detected vertical curve span. Grade values at the PVC and PVT are derived from the first derivative of the fitted parabola. This produces grade values consistent with the parabolic assumption used in highway geometric design.

### 5.13 Functional System Scaling

Design standards and operating characteristics differ substantially across functional systems. An Interstate corridor has different smoothing requirements than a local collector. The engine selects horizontal and vertical smoothing factors based on the predominant functional system of each route. In version 3.2, these factors are loaded from `national_smoothing_factors.json` when available, providing state-specific calibration rather than uniform national defaults.

---

## 6. Units of Measurement

### 6.1 Metric Core

All internal geometry computations are performed in meters on a flat UTM projection. WGS84 coordinates are projected to the appropriate UTM zone at the start of each route's processing and back-projected to WGS84 for output. Using a consistent metric core prevents unit conversion rounding errors during iterative calculations.

### 6.2 Imperial User Interface

User-facing configuration parameters (spacing, curve length thresholds, merge gaps, smoothing factors) are specified in feet. HPMS route measures are read and reported in miles. The plan and profile PDF outputs use foot-based stationing. The conversion constant `FEET_PER_METER = 3.28084` is applied at the input and output boundaries.

---

## 7. Outputs and Intended Use

### 7.1 Alignment Outputs

- Horizontal and vertical curve tables (CSV)
- Alignment vertices file (CSV) — uses `Dist_Mi`; plan/profile vertices file uses `Dist_Ft`
- Optional spatial exports (GeoJSON, GeoPackage, Shapefile)
- Interactive HTML map with severity-classified curve styling
- HTML summary dashboard including system health, density, severity distribution, and diagnostic charts

### 7.2 Plan and Profile Outputs

File names follow the convention `plan_profile_<RouteId>_MP_<begin>_to_<end>`.

- Vertex table (CSV) with smoothed and raw coordinates, elevation, grade, and curve type at each point
- Horizontal and vertical curve tables (CSV)
- Multi-page annotated PDF plan and profile sheets
- HTML sensitivity analysis dashboard

### 7.3 4D Enrichment Outputs

- Production CSV with `WKT_ZM` column
- GeoPackage with 3D geometry in WGS84
- Projected SHP in local UTM for Blender and CAD interoperability

### 7.4 Calibration Outputs

- `national_smoothing_factors.json`: State and FS-specific smoothing parameters
- `calibration_audit.csv`: Per-search audit record for manual review and override decisions

---

## 8. Practical Tuning Guidance

### 8.1 Symptom-Based Adjustments

| Symptom | Parameter Adjustments |
| :--- | :--- |
| Too many small horizontal curves | Increase `H_SMOOTH_FACTOR`; increase `H_MIN_DELTA`; increase `H_MIN_CURVE_LENGTH_FT` |
| Legitimate tight curves being missed | Decrease `H_SMOOTH_FACTOR`; decrease `H_MIN_CURVE_LENGTH_FT`; decrease `H_MIN_DELTA` |
| Noisy vertical output | Increase `V_SMOOTH_FACTOR`; increase `V_MIN_CURVE_LENGTH_FT`; increase `V_MIN_GRADE_CHANGE` |
| Bridge dips remain in profile | Increase `TREND_WINDOW_FT`; decrease `DIP_THRESHOLD_FT`; increase `BRIDGE_MAX_LEN_FT` |
| Overpass spikes appear as false CREST curves | Increase `V_SMOOTH_FACTOR` |

### 8.2 Plan View and Profile View Display Adjustment

When producing plan and profile sheets for urban areas with sharp turning geometry, the plan view alignment may extend outside the default ±200 ft offset window. The y-axis limits can be expanded by modifying `ax_plan.set_ylim(-200, 200)` in `rat_plan_profile_report_pdf.py`. For mountainous routes where the profile exceeds the default ±100 ft elevation window, adjust `ax_prof.set_ylim(avg_elev - 100, avg_elev + 100)` accordingly. Reducing these limits (e.g., ±50 ft for the profile) can make the offset between the smoothed and raw alignment easier to detect during accuracy review.

---

## 9. QA/QC and Validation Workflow

The validator (`rat_results_validator.py`) evaluates alignment and 4D outputs against defined integrity criteria.

**Hard failure conditions:**
- Required columns absent
- Curve lengths at or below zero
- End distance not greater than start distance

**Warning-level conditions:**
- Nonpositive radius or K-values
- Invalid categorical fields (Dir, Type, Bin, Grade_Bin)

**4D output checks:**
- `WKT_ZM` column presence and parseability
- Z and M value range summaries
- NaN Z-value detection

Recommended practice: run the alignment module, run the validator, review exception outputs before publishing.

---

## 10. Output Column Reference

### 10.1 Universal Fields

| Field | Description |
| :--- | :--- |
| `RouteId` | Route identifier from source dataset, normalized to uppercase. |
| `Start_Dist` / `End_Dist` | Curve start and end positions along the processed route axis, in meters. Used for internal QA; not intended as a primary delivery field. |
| `Length_m` | Computed curve length in meters. |
| `Calibrated_Start_MP` / `Calibrated_End_MP` | Linear reference positions mapped back from the smoothed geometry to the original route measure range. |
| `Part` | Index of the disjoint geometry chunk from which the curve was derived. Routes with gaps in HPMS data may produce multiple parts. |
| `FSystem` | Functional system code from the source data. |

### 10.2 Horizontal Curve Fields

| Field | Description |
| :--- | :--- |
| `Radius_m` | Representative radius for the curve segment, in meters. |
| `Min_Radius_m` | Minimum (apex) instantaneous radius along the segment. Severity classification is based on this value. |
| `Delta` | Total deflection angle across the curve span, in degrees. |
| `Dir` | Curve direction relative to route digitization direction: `Left` or `Right`. |
| `Bin` | Severity class A through F, based on `Min_Radius_m`. |
| `Merge_Status` | `Simple` for individual curves; `Compound` when merge logic has fused adjacent same-direction curves. |

> `Bin` is a severity classification, not a design speed rating. Direction depends on digitization direction, which may not correspond to the direction of travel.

### 10.3 Vertical Curve Fields

| Field | Description |
| :--- | :--- |
| `Grade_In` / `Grade_Out` | Estimated incoming and outgoing grades in percent. |
| `Alg_Diff` | Algebraic grade difference (G2 − G1), signed. |
| `K_Value` | Approximate K-value: curve length divided by the absolute algebraic difference. |
| `Type` | `CREST` or `SAG`. |
| `E` | Vertical offset metric used in significance filtering. |
| `Grade_Bin` | Severity class A through F based on algebraic difference magnitude. |

> K-value interpretation depends on the route class, design speed environment, and terrain context.

### 10.4 Plan and Profile Vertex Fields

| Field | Description |
| :--- | :--- |
| `Milepost` | Calibrated linear reference measure interpolated across the route bounds. |
| `Dist_Ft` | Continuous distance axis in feet. Used in the plan/profile vertices file for PDF page layout. |
| `Dist_Mi` | Continuous distance axis in miles. Used in the alignment vertices file. Seeded from the route's actual starting milepost so it reflects position on the state network rather than resetting to zero. |
| `Lon` / `Lat` | Smoothed coordinate (WGS84). |
| `Elev_Ft` | Smoothed elevation in feet. |
| `Raw_Lon` / `Raw_Lat` | Pre-smoothed coordinate reference. |
| `Elev_Raw_Ft` | Raw DEM-based elevation before profile smoothing. |
| `Grade_Pct` | Estimated grade at this vertex in percent. |
| `H_Curve_Type` | Geometric status: `Tangent`, `Left`, or `Right`. |
| `V_Curve_Type` | Vertical status: `Tangent`, `CREST`, or `SAG`. |

### 10.5 4D Enrichment Fields

| Field | Description |
| :--- | :--- |
| `WKT_ZM` | Well-known text representation of the 3D/4D geometry: `LINESTRING ZM (lon lat elev_m milepost, ...)`. |
| `geometry_3d` | In-memory Shapely LineString with Z coordinate (written to GeoPackage; not in CSV). |

### 10.6 Calibration Audit Fields

Key fields in `calibration_audit.csv` for manual review:

| Field | Description |
| :--- | :--- |
| `selection_method` | One of: `elbow`, `flat_curve`, `highest_safe`, `composite_fallback`, `absolute_fallback`. |
| `confidence_score` | 0–100 composite quality score. Values below 40 trigger `override_recommended = True`. |
| `override_recommended` | `True` when selection method is a fallback or confidence is low. |
| `deviation_from_default` | Ratio of selected factor to national default. Values above 2.0 or below 0.5 are outliers. |
| `ceiling_proximity_pct` | Selected RMSE as a percentage of the applicable ceiling. Values above 90% indicate a marginal result. |
| `n_passing` | Number of sweep factors that passed Gate 1. Low values indicate limited calibration data. |
| `std_v_rmse_at_selected` / `std_h_rmse_at_selected` | Standard deviation of RMSE across sampled chunks at the selected factor. High values indicate a noisy or heterogeneous sample. |

---

## 11. Interactive Map and Dashboard

### 11.1 HTML Alignment Map

The interactive map provides a curve geometry layer for statewide pattern review and stakeholder communication. Curves are color-coded by severity bin (A = green through F = purple). Compound horizontal curves are displayed with a dashed line style. Tooltips show RouteId, calibrated mileposts, severity class, and curve geometry attributes.

**Note:** The geometry embedded in the interactive HTML map undergoes simplification at a tolerance of 0.00005 degrees (approximately 5 meters) to prevent web browser memory limits from being exceeded on large statewide networks. This simplification is not visible at typical map zoom levels. It does not affect the standalone GeoJSON, GeoPackage, or Shapefile exports, which always contain full-resolution geometry. Uncheck "Simplify Web Geometry" in the GUI to disable simplification in the HTML map as well.

### 11.2 Summary Dashboard

The summary dashboard (`alignment_dashboard_<state>_<date>.html`) provides the following charts:

* **System Health:** RMSE deviation by functional system with P50, P80, and P95 percentile bands.
* **Curve Density:** Horizontal and vertical curves per 100 route-miles by functional system.
* **Cumulative Severity Distribution:** CDF of horizontal and vertical severity classes.
* **Severity by Functional System:** Stacked bar charts showing severity class distribution per FS.
* **CREST vs. SAG:** Vertical curve type counts by functional system.
* **Compound Curve Percentage:** Fraction of horizontal curves classified as compound by FS.
* **Advanced Diagnostics:* Curve length vs. radius scatter plot and K-value distribution histogram.

### 11.3 Plan and Profile Sensitivity Dashboard

The `rat_plan_profile_cli.py` produces an HTML sensitivity analysis dashboard by slicing the target route into 1-mile segments and evaluating a set of candidate smoothing factors. The dashboard reports horizontal curvature variance and vertical RMSE, maximum deviation, and grade variance for each factor. Use the dashboard to identify the factor at which the variance and RMSE metrics stabilize — additional stiffness beyond that point produces diminishing returns while increasing the risk of displacing curve endpoints.

---

## 12. Known Algorithmic Anomalies

### 12.1 Orthogonal Stair-Step Digitization

Some digitizers represent curved roads with abrupt orthogonal line segments. The engine may interpret these as alternating reverse curves. Increase `H_SMOOTH_FACTOR` or use merge logic to consolidate the resulting micro-curves.

### 12.2 GPS Multipath Jitter

Dense urban environments can produce GPS multipath errors that manifest as vibrating linework. The second derivative of this pattern produces false-positive high-severity curves. Increase `H_SMOOTH_FACTOR` if the output contains an implausible density of short curves.

### 12.3 LiDAR Water-Body Artifacts

DEM artifacts over large water bodies where LiDAR returns were scattered can create artificial pits in the bare-earth surface. If these pits exceed `BRIDGE_MAX_LEN_FT`, the bridge repair logic will not span them. Increase `TREND_WINDOW_FT` to anchor the baseline trendline to the approach geometry rather than the water surface, or increase `BRIDGE_MAX_LEN_FT` to allow wider span interpolation.

### 12.4 Collinear Vertex Redundancy

Some datasets contain densely packed collinear vertices on straight segments. The curvature denominator approaches zero in these cases, creating numerical instability. The `densify_coords_line` function redistributes vertices at consistent intervals, eliminating collinear redundancy before curvature calculations are performed.

### 12.5 Overpass Z-Spikes

Wide overpasses are occasionally misclassified as solid ground by the LiDAR processing algorithm, producing upward elevation spikes of 20 to 50 feet. The engine will attempt to fit a sharp CREST curve to this artifact. Increase `V_SMOOTH_FACTOR` to give the vertical spline sufficient stiffness to pass through the artifact without fitting a curve to it.

### 12.6 Curve Endpoint Displacement

The smoothing spline fits a globally continuous function across the full route rather than processing each curve in isolation. As a result, the spline begins responding to approaching curvature slightly before the physical curve starts, causing the detected PC and PT (or PVC and PVT for vertical curves) to extend into the adjacent tangent sections. The effect is more pronounced at higher smoothing factors and on curves with gradual transitions, and less pronounced on sharp curves where the curvature signal is strong relative to the spline window.

This is a known limitation of spline-based alignment derivation from observed geometry. The plan and profile sheets display both the smoothed centerline and the raw input geometry simultaneously, which allows the analyst to visually assess the extent of endpoint displacement for any given curve.

A correction method based on curvature threshold trimming is under development for a future release. The approach would identify the point along each detected curve where instantaneous curvature first exceeds a minimum significance threshold and report that trimmed position as the PC or PT, moving the endpoints inward from the spline-extended positions to where the curve is geometrically meaningful. Until that correction is implemented, reported `Calibrated_Start_MP` and `Calibrated_End_MP` values should be understood as conservative estimates that may slightly overstate curve length, particularly on lower-speed networks where shorter smoothing factors reduce but do not eliminate the effect.

### 12.7 DEM Micro-Undulations (Clustered Vertical Curves)

In very flat terrain, 1/3 arc-second USGS DEMs contain sub-foot surface noise, and minor real-world features like box culverts or farm underpasses present as "micro-undulations." 

If the `V_MIN_GRADE_CHANGE` is set too low (e.g., 0.5%), the highly accurate vertical smoothing spline will mathematically trace these 1-foot humps, generating massive clusters of false-positive "micro-curves" along otherwise flat corridors. Ensuring `V_MIN_GRADE_CHANGE` is set to the recommended default of 1.5% (or higher) filters out this surface noise and correctly consolidates the macro-profile.

### 12.8 Vertical Curve Classification at Bridge Anchor Points

The bridge detection algorithm in `fix_profile_by_deviation()` corrects the smoothed vertical profile at river crossings and other underpasses by interpolating a flat or gently graded line between anchor points on either side of the span. While the corrected profile elevation is geometrically accurate, the transition between the interpolated bridge line and the surrounding road grade can produce short SAG curve classifications at the anchor points. These are mathematical artifacts of the spline fitting at the boundary between the corrected and uncorrected profile segments, not genuine sag curves in the road geometry.

These artifacts are most likely to appear at bridge crossings where the approach grades are gentle and the valley is wide, as these conditions produce the most gradual anchor point transitions. They do not affect the accuracy of the corrected elevation profile, and the associated curve lengths are typically short. However, they may appear in the vertical curve output and should be interpreted with caution at locations known to cross water features or other underpasses.

A future refinement may suppress vertical curve classifications that fall within or immediately adjacent to detected bridge spans.

### 12.9 Validator Coverage for New Output Files

The Results Validator (`rat_results_validator.py`) currently checks the horizontal and vertical curve CSVs and the 4D enriched output. The alignment vertices file and section scores file introduced in v3.2 are not yet covered. A future update will add integrity checks for these outputs, including milepost continuity, bin value validity, and coordinate bounds.

---

## 13. Interpreting the Calibration Audit

The `calibration_audit.csv` file produced by the National Calibration Engine documents the selection process for every state, functional system, and mode combination. This section explains how to read the audit, identify results that warrant manual review, and make informed override decisions.

### 13.1 Where to Start

Open `calibration_audit.csv` in Excel or a similar tool and apply the following sort and filter sequence:

1. Filter `override_recommended` to `True`. This surfaces all results where the algorithm flagged low confidence or used a fallback selection method.
2. Within that filtered set, sort by `confidence_score` ascending. The lowest scores represent the weakest calibration results and should be reviewed first.
3. Note the `selection_method` column for each flagged row. The meaning of each method is described in Section 13.2.

For results where `override_recommended` is `False`, spot-check any row where `deviation_from_default` is greater than 2.0 or less than 0.5. These are outliers relative to the national median and may reflect genuine regional geometry or may indicate a sampling artifact.

### 13.2 Understanding Selection Methods

The `selection_method` column records how the factor was chosen. Each method carries different implications for result reliability.

**`elbow`:** The algorithm evaluated three or more passing factors and identified a clear point of diminishing returns. This is the intended outcome and generally produces the most reliable results. Confidence scores above 60 with this method indicate a well-defined elbow and a trustworthy selection.

**`flat_curve`:** Three or more factors passed Gate 1, but the RMSE rise across them was less than the flat threshold (0.15 ft). This means the smoothing factor has negligible effect on alignment quality for this road class in this state. The lowest passing factor is returned as a conservative default. `confidence_score` will be capped at 50. This is not a failure, it indicates that the geometry is either unusually uniform or that the factor range tested does not produce meaningful differentiation for this combination.

**`highest_safe`:** Fewer than three factors passed Gate 1, so the elbow algorithm had insufficient data to run. The highest passing factor is returned. When `rmse_rise` is 0.0, this typically means both passing factors (usually 100 and 200) produced identical RMSE, confirming insensitivity rather than a data problem. When `rmse_rise` is nonzero with only two passing factors, the result is geometrically safe but the elbow location is uncertain, the true optimum may lie between the two passing values or just beyond the last one. Confidence is capped at 50.

**`composite_fallback`:** No factor passed Gate 1. The algorithm returned the factor with the lowest composite penalty score. This result means the HPMS geometry for this state and functional system consistently exceeded the deviation ceilings at every smoothing level tested. Common causes include small sample sizes, bridge-heavy urban networks, or extreme terrain. `confidence_score` is capped at 25. These results should be reviewed and manually overridden where possible.

**`absolute_fallback`:** No valid metrics were computed at all. The returned factor of 100 is a placeholder, not a calibrated value. `confidence_score` is capped at 5. This should be treated as a missing result rather than a calibration outcome.

### 13.3 Reading the Confidence Score

The confidence score (0–100) is a composite measure derived from three components:

* **Elbow sharpness (up to 50 points):** The `peak_elbow_distance` value normalized against a reference of 0.50. A peak distance above 0.35 indicates a well-defined elbow; below 0.10 indicates a weak or ambiguous inflection.

* **Sample richness (up to 30 points):** The number of passing factors normalized against 10. Results with 10 or more passing factors receive full points; results with 2-3 passing factors receive proportionally fewer.

* **RMSE curve meaningfulness (up to 20 points):** The `rmse_rise` value normalized against 1.0 ft. A rise below 0.15 ft indicates the smoothing factor has little effect on the RMSE curve.

A score of 70 or above indicates a reliable, well-supported result. Scores between 40 and 70 are usable but should be validated against known geometry in the state before full production use. Scores below 40 trigger `override_recommended = True` and should be reviewed before relying on the result.

### 13.4 Identifying Outliers with Deviation from Default

The `deviation_from_default` column expresses the selected factor as a ratio of the national default for that functional system and mode. A value of 1.0 means the state matches the national default exactly. Values above 2.0 or below 0.5 are outliers worth examining.

**High deviation (> 2.0)** may indicate:

* Genuinely atypical geometry for the state and road class (e.g., Great Plains states FS1 H on unusually straight Interstate corridors, or mountain state FS3 V on steep terrain)

* A small or unrepresentative Cochran sample that happened to draw from an atypical corridor

* A sampling artifact where the 1-mile chunk population was dominated by a single geometric type

**Low deviation (< 0.5)** may indicate:

* A composite or absolute fallback result that selected the minimum factor
* Geometry that is more variable or complex than the national median for that class

When deviation is high and confidence is also high, the result is more likely to reflect genuine regional geometry. When deviation is high and confidence is low, treat the result with caution.

### 13.5 Using the RMSE Columns for Validation

The metrics at the selected factor (`v_rmse_at_selected`, `h_rmse_at_selected`, `maxv_at_selected`, `maxh_at_selected`) provide a direct measure of how much the smoothed alignment deviates from the raw geometry at the chosen factor.

Compare these against the ceiling values (`v_rmse_ceiling`, `h_rmse_ceiling`, `maxv_ceiling`, `maxh_ceiling`) using the `ceiling_proximity_pct` column. A proximity above 90% means the selected factor's RMSE is very close to the acceptance limit, leaving little margin. Results in this range are geometrically valid but represent the boundary of what the calibration considers acceptable — if the route geometry in production is slightly more complex than the sampled chunks, the actual RMSE may exceed the ceiling.

The `std_v_rmse_at_selected` and `std_h_rmse_at_selected` columns measure variability across the sampled chunks. A standard deviation greater than 50% of the mean RMSE indicates a heterogeneous sample — some chunks produced much higher deviation than others. This is common in states with mixed terrain or where the Cochran sample drew from both urban and rural corridors. High standard deviation does not invalidate the result but suggests that the mean RMSE may not be representative of all geometry in the state.

### 13.6 Making Manual Overrides

When a result warrants manual correction, edit the relevant entry directly in `national_smoothing_factors.json`. The JSON is read at run time by `build_params()`; no code changes are required.

Common override scenarios and recommended approaches:

**Small-sample urban states (e.g., DC FS1):** Interstate geometry in small urban jurisdictions is often dominated by elevated structures, tunnels, and interchange ramps. The calibration sample is too small and geometrically atypical to produce reliable results. Override to the national default for the functional system.

**`highest_safe` results with `rmse_rise = 0.0`:** These indicate factor insensitivity, not a problem. H=200 returned by this method on FS4-7 is appropriate and does not require override. Document as expected behavior.

**High-deviation elbow results with strong confidence:** These are likely genuine regional characteristics. Validate on a known benchmark corridor before accepting or overriding. If the plan and profile output looks correct for that state and functional system, accept the calibrated value.

**`composite_fallback` results in mountain states:** These typically reflect extreme terrain variance exceeding the deviation ceilings. Consider whether the maxv_ceiling for the relevant functional system should be relaxed for that state, or accept the fallback and note it in the production documentation.

### 13.7 Cross-State Consistency Check

After completing a national run, a useful validation step is to compare results for neighboring states with similar terrain. States within the same physiographic region should produce broadly consistent smoothing factors for the same functional system. Large unexplained discontinuities between neighboring states — particularly for V factors on FS1 — are candidates for further review.

The following regional groupings are a useful starting point for consistency checks:

* **Great Plains** (Kansas, Nebraska, Iowa, South Dakota, North Dakota) — expect higher H and V factors on FS1 due to straight, rolling corridor geometry
* **Mountain West** (Colorado, Wyoming, Montana, Idaho, Utah) — expect moderate H and higher V factors; mountain state relaxation on MaxV is active for all
* **Southeast** (Alabama, Mississippi, Georgia, Tennessee) — expect moderate H and V across all functional systems
* **Mid-Atlantic Urban** (DC, Maryland, Delaware, New Jersey) — expect small sample sizes, frequent `highest_safe` results, and lower confidence scores on higher functional systems due to dense interchange geometry

---

## Appendix A. Parameter Reference

*Adjust one parameter group at a time. Validate against benchmark routes before a full statewide rerun.*

### Table 1. Core Spacing and Smoothing

| Parameter | Default | Units | Effect |
| :--- | :---: | :---: | :--- |
| `DENSIFY_SPACING_FT` | 10 | ft | Vertex interpolation interval before analytics. Decrease for higher geometric resolution; increase to reduce processing time on dense geometry. |
| `H_SMOOTH_FACTOR` | 400 | ft | Horizontal spline stiffness for FS 1. Higher values produce a straighter smoothed line; lower values allow more lateral flexibility. State-specific values from `national_smoothing_factors.json` override this default at run time. |
| `V_SMOOTH_FACTOR` | 1000 | ft | Vertical spline stiffness for FS 1. State-specific values override this default at run time. |
| `H_BASE_SMOOTH_WINDOW` | 21 | points | Heading smoothing window size for the Savitzky-Golay pre-filter. Must be odd; minimum effective value is 5. |

### Table 2. Horizontal Curve Detection

| Parameter | Default | Units | Effect |
| :--- | :---: | :---: | :--- |
| `H_MIN_DELTA` | 3.5 | deg | Minimum total deflection angle required to retain a curve. Increase to suppress gentle, sweeping bends on high-speed corridors. Decrease for lower-speed networks where minor deflections are safety-relevant. |
| `H_MIN_CURVE_LENGTH_FT` | 100 | ft | Minimum horizontal curve length. Curves shorter than this value are discarded as noise. |
| `H_MAX_RADIUS_FT` | 165,000 | ft | Upper radius bound for curve classification. Curves with computed radius above this value are treated as tangent sections. |

### Table 3. Vertical Curve Detection

| Parameter | Default | Units | Effect |
| :--- | :---: | :---: | :--- |
| `V_MIN_CURVE_LENGTH_FT` | 200 | ft | Minimum vertical curve length. |
| `V_MIN_GRADE_CHANGE` | 1.5 | % | Minimum algebraic grade difference required to retain a vertical curve. |
| `V_VC_THRESHOLD` | 0.002 | rate | Curvature sensitivity threshold for initiating vertical curve candidates. |
| `V_MIN_OFFSET_FT` | 0.10 | ft | Minimum vertical offset significance filter. |

**A Note on `V_MIN_GRADE_CHANGE` (1.5% Default):**
This threshold acts as a critical physical low-pass filter. The suite relies on 1/3 arc-second USGS DEMs, which contain a baseline level of surface "noise." In flat terrain, this DEM chatter, along with minor real-world features like box culverts or road crowning, presents as sub-foot "micro-undulations." Enforcing a 1.5% minimum (roughly 1.5 to 2.0 feet of physical elevation change over a standard 250-foot curve) ensures the engine confidently ignores DEM noise and outputs near design-grade highway geometry suitable for macro-level HPMS reporting.

### Table 4. Bridge and Profile Repair

| Parameter | Default | Units | Effect |
| :--- | :---: | :---: | :--- |
| `TREND_WINDOW_FT` | 1,000 | ft | Window length for establishing the local topographic trendline. Increase for wide valleys or large water bodies where a short window is dragged down by the feature being bridged. |
| `DIP_THRESHOLD_FT` | 6.5 | ft | Deviation below the trendline that triggers bridge interpolation. Decrease to bridge over small culverts and shallow drainage features. |
| `BRIDGE_MAX_LEN_FT` | 8,200 | ft | Maximum span that the bridge interpolation will cover. The default of approximately 1.5 miles accommodates large river crossings and major interchange structures. Decrease if the engine is incorrectly bridging over wide topographic valleys. |

### Table 5. Merge and Post-Processing

| Parameter | Default | Units | Effect |
| :--- | :---: | :---: | :--- |
| `ENABLE_MERGE` | False | bool | Enables merging of adjacent same-direction horizontal curves across short tangent gaps. |
| `MERGE_GAP_FT` | 600 | ft | Maximum tangent gap between adjacent horizontal curves eligible for merging. |
| `V_MERGE_GAP_FT` | 1,500 | ft | Maximum gap between adjacent vertical curves eligible for merging. |

### A.6 National Smoothing Factor Calibration

Version 3.2 provides a nationally calibrated set of smoothing factors in `national_smoothing_factors.json`. Each entry is keyed by two-digit State FIPS code and contains FS-specific horizontal and vertical smoothing factors derived by the RAT National Calibration Engine.

Factors were derived using the following process:

1. HPMS data for each state were retrieved via the Socrata API and segmented into 1-mile chunks per route.
2. A statistically representative sample was drawn using Cochran's formula (95% confidence, 5% margin of error).
3. USGS DEM tiles were downloaded for the sampled geometry.
4. Candidate smoothing factors from the sweep range [100, 200, 400, 600, 800, 1000, 1200, 1400, 1600, 1800, 2000, 2500, 3000, 4000, 4500] were evaluated against Gate 1 ceilings (RMSE and maximum deviation limits by functional system).
5. Among factors passing Gate 1, the Kneedle elbow algorithm identified the factor at the point of diminishing returns.
6. For mountain states (Alaska, Arizona, California, Colorado, Idaho, Montana, Nevada, New Mexico, Oregon, Utah, Washington, Wyoming, and Appalachian states including Georgia, Kentucky, Maine, New Hampshire, New York, North Carolina, Pennsylvania, Tennessee, Vermont, Virginia, and West Virginia), the maximum vertical deviation ceiling is relaxed by 8 ft to accommodate bridge approach geometry.

For states or functional systems where the calibrated result may be unreliable (indicated by `override_recommended = True` in `calibration_audit.csv`), manual review and override of the JSON entry is recommended before statewide production processing.

Manual overrides are applied directly to the relevant state entry in `national_smoothing_factors.json`. The JSON is read at run time; no code changes are required.

### A.7 Parameter Tuning Guidance

**Horizontal Smoothing Factor (`H_SMOOTH_FACTOR`)**

Controls the lateral stiffness of the smoothing spline.

- Increase when output contains an implausibly high density of short, high-severity curves, indicating digitization noise or GPS multipath errors.
- Decrease when legitimate tight geometry (interchange ramps, roundabouts, mountain switchbacks) is being suppressed.

**Deflection and Length Thresholds (`H_MIN_DELTA`, `H_MIN_CURVE_LENGTH_FT`)**

Control minimum significance criteria for horizontal curve retention.

- Increase when long, gently sweeping interstate curves are being reported as multiple short curves, or when digitization drift is being detected.
- Decrease when processing lower-speed networks where minor deflections or short curves are analytically significant.

**Vertical Bridging (`TREND_WINDOW_FT`, `DIP_THRESHOLD_FT`)**

Control the bridge repair trigger and span.

- Increase `TREND_WINDOW_FT` when wide valleys cause the trendline to follow the terrain down into the crossing rather than remaining anchored to the approaching grades.
- Decrease `DIP_THRESHOLD_FT` when shallow crossings (small bridges, culverts) are not being bridged.

**Vertical Smoothing Factor (`V_SMOOTH_FACTOR`)**

Controls the stiffness of the elevation profile spline.

- Increase when overpass Z-spikes produce false CREST curves in the output. A stiffer spline passes through the artifact without fitting a curve to it.

### A.8 Additional Adjustments

**Plan view offset window.** For urban routes with sharp turns, the smoothed alignment may extend beyond the default ±200 ft offset range in the plan view. Adjust `ax_plan.set_ylim(-200, 200)` in `rat_plan_profile_report_pdf.py` to expand the window.

**Profile view elevation window.** For mountain routes with large elevation changes, the profile may extend beyond the default ±100 ft range. Adjust `ax_prof.set_ylim(avg_elev - 100, avg_elev + 100)` accordingly. Reducing the window to ±50 ft makes the deviation between the smoothed profile and raw DEM elevation more visible during accuracy review.

---

## Appendix B. Tuning Playbooks

**Playbook 1 — Too many statewide horizontal curves**
1. Increase `H_SMOOTH_FACTOR`
2. Increase `H_MIN_DELTA`
3. Increase `H_MIN_CURVE_LENGTH_FT`
4. Revalidate on sample corridors before full rerun

**Playbook 2 — Curves missing on lower-speed networks**
1. Decrease `DENSIFY_SPACING_FT`
2. Decrease `H_SMOOTH_FACTOR` or apply a lower FS-specific override
3. Decrease `H_MIN_CURVE_LENGTH_FT`
4. Validate against known geometry

**Playbook 3 — Bridge dips remaining in vertical outputs**
1. Increase `TREND_WINDOW_FT`
2. Decrease `DIP_THRESHOLD_FT`
3. Review `BRIDGE_MAX_LEN_FT`
4. Re-run targeted bridge corridors and inspect profiles

**Playbook 4 — State-specific calibration result needs review**
1. Open `calibration_audit.csv` and filter `override_recommended = True`
2. Review `selection_method`, `confidence_score`, `n_passing`, and `deviation_from_default` for flagged entries
3. Compare `v_rmse_at_selected` and `h_rmse_at_selected` against neighboring states with similar terrain
4. Edit the relevant entry in `national_smoothing_factors.json` with the corrected values
5. Re-run alignment on benchmark routes for the affected state to confirm

---

## Appendix C. Glossary

| Term | Definition |
| :--- | :--- |
| **Algebraic Difference (A)** | The signed difference between outgoing and incoming grades at a vertical curve: A = G2 − G1. |
| **Apex Radius** | The minimum instantaneous radius along a horizontal curve, used for severity classification. |
| **Calibrated Milepost** | A linear reference position derived by proportionally mapping the smoothed curve location back to the original HPMS Start_MP / End_MP range. |
| **CREST Curve** | A vertical curve where the grade changes from positive to negative (hilltop). |
| **Deflection Angle (Delta, Δ)** | The total change in compass heading from the point of curvature (PC) to the point of tangency (PT). |
| **Elbow Detection** | The Kneedle algorithm used by the calibration engine to identify the factor at the point of diminishing returns on the RMSE curve. |
| **Gate 1** | The geometric safety ceiling check in the calibration engine: a candidate factor must not cause RMSE or maximum deviation to exceed defined thresholds. |
| **K-Value** | Vertical curve length divided by the absolute algebraic difference. Larger K-values indicate gentler vertical transitions. |
| **KDTree** | A spatial indexing structure used by the 4D enricher to snap 2D vertices to the nearest point on the 3D macro-profile. |
| **LRS** | Linear Referencing System. The route measure system (miles) used in HPMS. |
| **Macro-Profile** | The continuous 3D elevation profile for a full route, constructed by the 4D enricher before snapping individual HPMS segments to it. |
| **SAG Curve** | A vertical curve where the grade changes from negative to positive (valley). |
| **Smoothing Factor** | The `s` parameter of the `scipy.interpolate.UnivariateSpline` function. Controls the trade-off between fidelity to raw data and geometric smoothness. |
| **UnivariateSpline** | The SciPy spline interpolation function used for horizontal and vertical smoothing. |
| **UTM** | Universal Transverse Mercator. A metric planar projection used for all internal distance and curvature calculations. |
| **WKT_ZM** | Well-known text geometry string with Z (elevation) and M (milepost) ordinates: `LINESTRING ZM (lon lat elev milepost, ...)`. |
