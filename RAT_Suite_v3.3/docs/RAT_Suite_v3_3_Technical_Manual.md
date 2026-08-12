# HPMS Roadway Alignment Tool (RAT) Suite v3.3
## Technical Manual

**Developed by:** Federal Highway Administration (FHWA), Office of Highway Policy Information

**Version:** 3.3

**Date:** July 2, 2026

**Status:** Public Domain - CC0 1.0 Universal

---

## Executive Summary

The HPMS Roadway Alignment Tool (RAT) Suite v3.3 is a production-scale geospatial analytics system for deriving roadway alignment characteristics from HPMS geometry and USGS elevation data. The suite is designed for statewide processing and can handle tens of thousands of fragmented route segments while producing the geometric detail required for route-level engineering review.

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
| Running national calibration |National Calibration Engine, Running the Calibration Script |
| Interpreting calibration results | Interpreting the Calibration Audit, Appendix A.6 |
| Program management and policy review | Executive Summary, Practical Tuning Guidance, Limitations |

**Scope and Intended Use**

The RAT Suite is an engineering analytics and data-derivation tool for decision support, screening, and HPMS quality review. It is not a replacement for design standards, signed and sealed design deliverables, or final field survey controls. Apply appropriate validation when using RAT outputs in high-consequence applications.

---

## RAT Suite v3.3 - File repository structure

```
RAT_Suite_v3.3/
├── apps/
│   ├── rat_unified_gui.py
│   ├── rat_alignment_cli.py
│   ├── rat_plan_profile_cli.py
│   ├── rat_plan_profile_report_pdf.py
│   └── hpms_4d_enricher_cli.py
├── core/
│   ├── rat_core.py
│   ├── national_smoothing_factors.json
│   └── calibration_audit.csv
├── tools/
│   ├── rat_national_calibration_cli.py
│   ├── rat_calibration_diagnostics.py
│   └── rat_results_validator.py
├── NBI/
│   ├── 2025AllRecordsDelimitedAllStates.txt
│   └── 2025NTI.xml
├── docs/
│   ├── images/
│   │    └── ceiling_proximity_heatmap_V_run5.png
│   ├── RAT_Suite_v3_3_Technical_Manual.md
│   ├── RAT_Suite_v3_3_Technical_Manual.html
│   └── RAT_Suite_v3_3_Research_Paper.docx
├── LICENSE
├── README.md
├── DISCLAIMER.md
├── Run_RAT_Suite.bat
├── pyproject.toml
└── Roadmap.md
```

---

## Table of Contents

- [Executive Summary](#executive-summary)
- [Repository Structure](#rat-suite-v33---file-repository-structure)
- [1. Overview](#1-overview)
- [2. What Changed in Version 3.3](#2-what-changed-in-version-33)
  - [2.1 Geographic Footprint Slicing](#21-geographic-footprint-slicing)
  - [2.2 Multi-Tier DEM Tile Management & Prefetching](#22-multi-tier-dem-tile-management--prefetching)
  - [2.3 Upgraded Stitching Engine](#23-upgraded-stitching-engine-stitch_linestrings_ordered)
  - [2.4 National Smoothing Factor Calibration](#24-national-smoothing-factor-calibration-major-change)
  - [2.5 Updated Default Smoothing Factors](#25-updated-default-smoothing-factors)
  - [2.6 Enhanced Summary Dashboard](#26-enhanced-summary-dashboard)
  - [2.7 Architectural and Code Quality Improvements](#27-architectural-and-code-quality-improvements)
  - [2.8 Switch to 1-Meter DEMs](#28-switch-to-1-meter-dems)
- [3. Suite Components](#3-suite-components)
  - [3.1 Unified GUI](#31-unified-gui-rat_unified_guipy)
  - [3.2 Mathematical and Geospatial Core](#32-mathematical-and-geospatial-core-rat_corepy)
  - [3.3 Alignment CLI](#33-alignment-cli-rat_alignment_clipy)
  - [3.4 Plan and Profile CLI](#34-plan-and-profile-cli-rat_plan_profile_clipy)
  - [3.5 Plan and Profile PDF Renderer](#35-plan-and-profile-pdf-renderer-rat_plan_profile_report_pdfpy)
  - [3.6 4D Enricher](#36-4d-enricher-hpms_4d_enricher_clipy)
  - [3.7 National Calibration Engine](#37-national-calibration-engine-rat_national_calibration_clipy)
  - [3.8 Calibration Diagnostics](#38-calibration-diagnostics-rat_calibration_diagnosticspy)
  - [3.9 Validator](#39-validator-rat_results_validatorpy)
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
  - [5.7 Spiral Transition Detection](#57-spiral-transition-detection)
  - [5.8 Minimum Apex Radius and Severity Classification](#58-minimum-apex-radius-and-severity-classification)
  - [5.9 Linear Reference Proportional Calibration](#59-linear-reference-proportional-calibration)
  - [5.10 Directionality and Compound Curves](#510-directionality-and-compound-curves)
  - [5.11 Savitzky-Golay Buffer Requirement](#511-savitzky-golay-buffer-requirement)
  - [5.12 Calculus-Based Horizontal Curvature](#512-calculus-based-horizontal-curvature)
  - [5.13 Vertical Parabolic Fitting](#513-vertical-parabolic-fitting)
  - [5.14 Functional System Scaling](#514-functional-system-scaling)
  - [5.15 High-Resolution LiDAR Integration](#515-high-resolution-lidar-integration)
- [6. Experimental Features](#6-experimental-features)
  - [6.1 Micro-Jitter Pavement Roughness Proxy](#61-micro-jitter-pavement-roughness-proxy-iri-proxy)
  - [6.2 3D Stopping Sight Distance Simulation](#62-3d-stopping-sight-distance-ssd-simulation)
  - [6.3 Network-Level Interchange Topology Mapping](#63-network-level-interchange-topology-mapping)
  - [6.4 AASHTO Superelevation Heuristic](#64-aashto-superelevation-heuristic)
- [7. Units of Measurement](#7-units-of-measurement)
  - [7.1 Metric Core](#71-metric-core)
  - [7.2 Imperial User Interface](#72-imperial-user-interface)
- [8. Outputs and Intended Use](#8-outputs-and-intended-use)
  - [8.1 Alignment Outputs](#81-alignment-outputs)
  - [8.2 Plan and Profile Outputs](#82-plan-and-profile-outputs)
  - [8.3 4D Enrichment Outputs](#83-4d-enrichment-outputs)
  - [8.4 Calibration Outputs](#84-calibration-outputs)
- [9. Practical Tuning Guidance](#9-practical-tuning-guidance)
  - [9.1 Symptom-Based Adjustments](#91-symptom-based-adjustments)
  - [9.2 Plan View and Profile View Display Adjustment](#92-plan-view-and-profile-view-display-adjustment)
- [10. QA/QC and Validation Workflow](#10-qaqc-and-validation-workflow)
- [11. Output Column Reference](#11-output-column-reference)
  - [11.1 Universal Fields](#111-universal-fields)
  - [11.2 Horizontal Curve Fields](#112-horizontal-curve-fields)
  - [11.3 Vertical Curve Fields](#113-vertical-curve-fields)
  - [11.4 Plan and Profile Vertex Fields](#114-plan-and-profile-vertex-fields)
  - [11.5 4D Enrichment Fields](#115-4d-enrichment-fields)
  - [11.6 Calibration Audit Fields](#116-calibration-audit-fields)
- [12. Interactive Map and Dashboard](#12-interactive-map-and-dashboard)
  - [12.1 HTML Alignment Map](#121-html-alignment-map)
  - [12.2 Summary Dashboard](#122-summary-dashboard)
  - [12.3 Plan and Profile Sensitivity Dashboard](#123-plan-and-profile-sensitivity-dashboard)
- [13. Known Algorithmic Anomalies](#13-known-algorithmic-anomalies)
  - [13.1 Orthogonal Stair-Step Digitization](#131-orthogonal-stair-step-digitization)
  - [13.2 GPS Multipath Jitter](#132-gps-multipath-jitter)
  - [13.3 LiDAR Water-Body Artifacts](#133-lidar-water-body-artifacts)
  - [13.4 Collinear Vertex Redundancy](#134-collinear-vertex-redundancy)
  - [13.5 Overpass Z-Spikes](#135-overpass-z-spikes)
  - [13.6 Curve Endpoint Displacement](#136-curve-endpoint-displacement)
  - [13.7 DEM Micro-Undulations](#137-dem-micro-undulations)
  - [13.8 Vertical Curve Classification at Bridge Anchor Points](#138-vertical-curve-classification-at-bridge-anchor-points)
  - [13.9 Validator Coverage for New Output Files](#139-validator-coverage-for-new-output-files)
  - [13.10 V Calibration Signal Absence on Flat Terrain](#1310-v-calibration-signal-absence-on-flat-terrain)
- [14. Running the Calibration Script](#14-running-the-calibration-script)
  - [14.1 Prerequisites](#141-prerequisites)
  - [14.2 Running the Calibration Engine](#142-running-the-calibration-engine)
  - [14.3 Monitoring the Run](#143-monitoring-the-run)
  - [14.4 Running the Diagnostics Tool](#144-running-the-diagnostics-tool)
  - [14.5 Interpreting Diagnostic Output](#145-interpreting-diagnostic-output)
  - [14.6 The Iterative Calibration Process](#146-the-iterative-calibration-process)
  - [14.7 Updating Defaults](#147-updating-defaults)
- [15. Interpreting the Calibration Audit](#15-interpreting-the-calibration-audit)
  - [15.1 Where to Start](#151-where-to-start)
  - [15.2 Understanding Selection Methods](#152-understanding-selection-methods)
  - [15.3 Reading the Confidence Score](#153-reading-the-confidence-score)
  - [15.4 Identifying Outliers with Deviation from Default](#154-identifying-outliers-with-deviation-from-default)
  - [15.5 Using the RMSE Columns for Validation](#155-using-the-rmse-columns-for-validation)
  - [15.6 Making Manual Overrides](#156-making-manual-overrides)
  - [15.7 Cross-State Consistency Check](#157-cross-state-consistency-check)
- [Appendix A. Parameter Reference](#appendix-a-parameter-reference)
  - [A.6 National Smoothing Factor Calibration](#a6-national-smoothing-factor-calibration)
  - [A.7 Parameter Tuning Guidance](#a7-parameter-tuning-guidance)
  - [A.8 Additional Adjustments](#a8-additional-adjustments)
- [Appendix B. Tuning Playbooks](#appendix-b-tuning-playbooks)
- [Appendix C. Glossary](#appendix-c-glossary)

---

## 1. Overview

The RAT Suite is an analytical derivation tool. It estimates operational curve attributes from observed, real-world geometry and elevation data rather than generating geometry from proposed design criteria. The suite is designed to ingest variable, human-digitized spatial networks and programmatically separate legitimate highway geometry from GPS multipath errors, digitization artifacts, and topographic noise.

Version 3.3 consolidates two prior development cycles into a stable, unified architecture. Version 3.0 established the shared core engine and orchestrating GUI. Version 3.1 attempted a rural/urban smoothing bifurcation that was superseded in 3.2 by a data-driven national calibration approach. Version 3.3 introduces the RAT National Calibration Engine and a nationally derived smoothing parameter dictionary, replacing the manual default values used in prior versions.

---

## 2. What Changed in Version 3.3

### 2.1 Geographic Footprint Slicing
The bulk processing engine shifts from sequential route-by-route processing to a localized Geographic Grid Slicing Architecture. Both `rat_alignment_cli.py` and `hpms_4d_enricher_cli.py` project the entire state network footprint into a bounding box, then divide space into uniform $0.02^\circ \times 0.02^\circ$ grid increments (approximately $1.3\text{ mile} \times 1.3\text{ mile}$ cells).

```
[State Footprint Bounding Box]
│
▼ (STRtree Spatial Index Query)
┌───────────────────────┐
│ 0.02° x 0.02° Grid    │ ──► Dispatched via ProcessPoolExecutor
│ Spatial Cell Tasks    │     (Bounded Peak RAM via 30 Parallel Workers)
└───────────────────────┘
```

Tasks are matched via a high-performance `shapely.strtree.STRtree` spatial index rather than $O(n \times m)$ linear lambda queries. Intersecting route segments are batched and dispatched across parallel execution threads via ProcessPoolExecutor.

### 2.2 Multi-Tier DEM Tile Management & Prefetching
To mitigate high-concurrency I/O stalls and race conditions during parallel processing, the DEM ingestion architecture has been completely decoupled from the runtime execution loop:
- **High-Resolution Mode (1m Data):** Enabled globally during processing sweeps (HIGH_RES_MODE = True). The system queries the USGS 3DEP ImageServer REST API to build localized, persistent 1-degree tiles.
- **Shared Persistent Workspace Cache:** Both the Alignment and 4D Enrichment pipelines point to a unified directory ({dem_dir}/align_1m_cache/). Whichever pipeline executes first populates the cache for the other.
- **Serial State-Wide Prefetching:** Before spinning up parallel worker processes, run_state_alignment() and run_state_enrichment() perform a serial pre-download of all 1-degree tiles covering the state bounding box. This eliminates runtime file-locking friction and overlapping network requests, completely mitigating multi-hour processing bottlenecks in geographically small, dense regions (e.g., Delaware).
- **Low-Resolution Fallback (10m Data):** `download_dems()` manages the 1/3 arc-second TIFF pipeline. This path is explicitly gated behind if not params.get("HIGH_RES_MODE") and functions strictly as an un-smoothed analytical fallback.

### 2.3 Upgraded Stitching Engine (`stitch_linestrings_ordered`)
The route assembly engine is fully direction-agnostic and spatial-relationship independent. It resolves geometric errors from disparate state DOT digitization streams using three core protections:
- **Direction-Agnostic Quad-Orientation Matching:** Evaluates all four vector terminus junctions to stitch adjacent linestrings properly, even if digitized in opposing directions:$$\text{Orientations} = \{\text{Tail-to-Head}, \text{Tail-to-Tail}, \text{Head-to-Tail}, \text{Head-to-Head}\}$$
- **Linear Referencing (MP) Disambiguation:** On dense, subdivided urban corridors where multiple candidate lines sit within the spatial tolerance threshold ($\text{snap\_tol} \approx 330\text{ ft}$), the engine halts nearest-neighbor selection. It selects the segment whose Milepost ($MP$) range forms the closest mathematical continuation of the existing chain's low/high bounds. Call sites must pass start_mps and end_mps parameters.
- **Closed-Ring Geometric Guard:** Stops tracing a vector chain once its head and tail close within the spatial tolerance distance (e.g., roundabouts or traffic circles), preventing multi-lap winding artifacts. This block is gated to execute only after more than one segment is added ($len(\text{current\_indices}) > 1$), allowing short individual segments to process seamlessly.

### 2.4 National Smoothing Factor Calibration (Major Change)

Prior versions used manually estimated smoothing factors. Version 3.3 introduces the **RAT National Calibration Engine** (`rat_national_calibration_cli.py`), which automatically derives optimal horizontal and vertical smoothing factors for each state and functional system through a data-driven sweep against HPMS geometry and USGS elevation data.

The calibration engine evaluates candidate factors across a defined sweep range and applies a two-stage selection algorithm:

* **Gate 1 (Geometric Safety Ceiling).** A candidate factor must not cause the smoothed alignment to deviate beyond defined RMSE and maximum-deviation thresholds for that functional system. These thresholds vary by functional system and are relaxed for states in mountainous terrain.
* **Elbow Detection (Diminishing Returns).** Among factors that pass Gate 1, the engine identifies the factor at the point of diminishing returns using the normalized Kneedle algorithm, the point of maximum perpendicular distance from the line connecting the first and last passing values. This selects the minimum stiffness that produces a geometrically stable alignment, avoiding both under-smoothing (noise retained) and over-smoothing (legitimate curves suppressed or curve endpoints displaced). The RMSE ($y$) and smoothing factor ($x$) arrays are min-max normalized ($x_n, y_n$) to a [0, 1] scale. The elbow is identified at the maximum perpendicular distance ($D$) from the baseline connecting the first and last passing values:

$$D = \frac{|y_n - x_n|}{\sqrt{2}}$$

Results are stored in `national_smoothing_factors.json` in the `core/` directory. The `build_params()` function in `rat_core.py` automatically loads state-specific factors when a State FIPS code is provided, falling back to national defaults when no state entry is available.

A **calibration audit file** (`calibration_audit.csv`) accompanies the JSON and documents the selection method, confidence score, RMSE metrics, and override recommendations for each state/functional system combination.

The following refinements were introduced during the v3.3 development cycle based on results from the first national calibration run:

* **Per-functional-system H RMSE ceilings.** The Gate 1 horizontal RMSE ceiling is now differentiated by functional system rather than using a single national value. Functional systems FS3–7 have progressively looser ceilings, and FS1–2 share a tighter ceiling of 3.5 ft, reflecting the greater geometric complexity of collectors and local roads relative to controlled-access highways, where a single tight threshold caused fallback selection rates of 47–77% on the first national run.
* **Early exit optimization.** The factor sweep exits early once three consecutive factors have failed Gate 1 with rising RMSE, confirming that the elbow region has been passed. This reduces sweep time significantly for functional systems where the passing range is narrow.
* **Flat terrain V calibration.** When the V RMSE range across the full sweep is below 0.05 ft, the terrain lacks sufficient vertical signal to produce a meaningful elbow selection. The engine records `flat_terrain_default` as the selection method and applies the national default factor for that functional system rather than returning a spurious elbow driven by floating-point noise.
* **Calibration Diagnostics Tool.** A companion script (`rat_calibration_diagnostics.py`) analyzes the completed `calibration_audit.csv` and produces threshold health reports, fallback rate charts, factor stability analysis, and per-functional-system recommendations. Running the diagnostics tool after each calibration run is the recommended practice for identifying threshold adjustments before a subsequent run. See Section 14.

### 2.5 Updated Default Smoothing Factors

The DEFAULTS block in `rat_core.py` has been updated to reflect nationally calibrated median values derived from the fifth national calibration run. Default values represent the recommended fallback when no state-specific JSON entry exists, for example, when processing a state that has not yet been calibrated, or when `flat_terrain_default` has been applied and the national default is used in place of an elbow selection.

Prior to the first national run these values were manually estimated placeholders. The values below reflect the empirical medians from the calibration audit, with FS1 vertical updated to 1,400 based on the median selected factor across mountain states where the V sweep produced meaningful signal.

| Functional System | H Default | V Default |
| :--- | :---: | :---: |
| FS 1 - Interstate | 400 | 1,400 |
| FS 2 - Other Freeways and Expressways | 200 | 1,400 |
| FS 3 - Other Principal Arterial | 400 | 1,400 |
| FS 4 - Minor Arterial | 400 | 1,400 |
| FS 5 - Major Collector | 200 | 1,000 |
| FS 6 - Minor Collector | 200 | 1,000 |
| FS 7 - Local | 400 | 1,000 |

**Note on V defaults for FS2.** FS2 remains at a warning-level fallback rate of 25 percent after five national calibration runs, with the binding constraint appearing to be the horizontal deviation ceiling rather than the RMSE ceiling. A targeted adjustment to FS2 MAX_H_DEV_FT is identified as the priority for a future calibration run.

### 2.6 Enhanced Summary Dashboard

The alignment CLI dashboard now includes four additional charts and improved system health reporting:

* **Curve density by functional system:** Curves per 100 route-miles, normalizing for network size differences across functional classes.
* **Cumulative severity distribution:** CDF curves showing the percentage of curves at or below each severity class for horizontal and vertical separately.
* **CREST vs. SAG breakdown by functional system:** Vertical curve type counts relevant to sight-distance screening.
* **Compound curve percentage by functional system:** The fraction of horizontal curves classified as compound, useful for identifying over-merging or legitimate spiral transitions.
* **System Health percentile bands:** The RMSE chart now shows P50, P80, and P95 per functional system rather than only the mean, distinguishing states where most routes are clean from those where outlier routes are driving the aggregate.

### 2.7 Architectural and Code Quality Improvements

- Centralized `fetch_socrata_state()` and `load_local_hpms()` functions in `rat_core.py`, eliminating duplicate implementations across CLI modules.
- Replaced the per-functional-system smoothing factor `if/elif` chain with a dict lookup for maintainability.
- Fixed O(n²) route filtering bottleneck in the alignment and enricher CLIs; replaced with `DataFrame.groupby()`.
- Corrected `SIMPLIFY_GEOMETRY` / `simplify_geometry` key casing inconsistency between the GUI and export functions.
- Corrected `page_mp_span` unbound variable risk in the PDF renderer.
- Added `geopandas` import required for local file processing in `rat_core.py`.
- `fetch_socrata_state()` now accepts `facility_type_filter` and `fsystem_filter` parameters, building the Socrata where clause dynamically from user selections rather than using a hardcoded filter. All CLI modules pass these parameters from `run_params.json`.
- Added malformed JSON protection in `rat_unified_gui.py` for Socrata paginated responses; truncated server responses now trigger a retry with exponential backoff rather than crashing the run.

### 2.8 Switch to 1-Meter DEMs

**The problem.** While 1/3 arc-second (approximately 10-meter) USGS DEMs provide sufficient resolution for statewide macro-analysis, they lack the pixel density required to accurately sample elevations along densified route geometry.

**The approach.** Version 3.3 introduces a grid-based tile engine that enables unlimited statewide scaling at 1-meter resolution. The calibration engine uses ephemeral per-worker tiles to minimize storage overhead; the alignment and enrichment pipelines use a persistent shared tile cache that eliminates redundant downloads across parallel workers.

* **Spatial grid partitioning.** Rather than downloading elevation data route by route, the engine maps the full route network onto a uniform geographic grid. Each grid cell covers approximately 0.02 degrees (~1.3 miles) and is assigned to an isolated parallel worker.

* **Tile downloads.** The calibration engine and the alignment/enrichment pipelines use different tile strategies. For the calibration engine, each worker requests a custom 1-meter GeoTIFF covering only its assigned 0.02-degree grid cell and deletes it immediately after use, keeping storage overhead near zero during calibration runs. For alignment and enrichment pipelines, the engine downloads standard 1-degree tiles at 2,400 by 2,400 pixels (~1m/px) and stores them in a persistent shared cache directory named `align_1m_cache/` or `enrich_4d_1m_cache/`. All 827 grid cells covering a state like Washington FS1 map to only 6 unique 1-degree tiles, reducing the tile cache from tens of gigabytes to under 200 MB for a single state. For a full national run the complete 1-degree cache for the continental US requires approximately 32 GB. The cache persists between runs; a second national run downloads nothing if the cache is intact.

* **Per-worker directory isolation.** Each parallel worker writes its tiles to an isolated temporary subdirectory, preventing tile name collisions when multiple states are processed simultaneously. The temporary directory is removed automatically when the worker completes, whether successfully or due to an error.

* **REST API retry logic.** The tile download utility applies a four-attempt exponential backoff loop (2s, 4s, 8s, 16s) to handle `502 Bad Gateway` and `504 Gateway Timeout` responses from the USGS server gracefully without failing the run.

* **NaN intercept.** If a tile request returns a data void, for example, at coastal boundaries or offshore geometry, the engine intercepts the resulting NaN arrays before they reach SciPy's `UnivariateSpline` backend, preventing fatal segmentation faults in the parallel worker pool.

The net effect is that 1-meter LiDAR-quality elevation data is available for every route in the network without the storage or memory requirements that would otherwise make statewide processing impractical.

---

## 3. Suite Components

### 3.1 Unified GUI (`rat_unified_gui.py`)

The GUI is the primary user interface. It does not perform geometric processing; instead, it collects configuration inputs, writes a `run_params.json` parameter payload, and launches CLI modules as isolated subprocesses. Passing parameters through a JSON payload to a disconnected subprocess isolates the memory required for statewide processing from the user interface and ensures the interface remains responsive during long batch runs.

The GUI loads state-specific smoothing factors from `national_smoothing_factors.json` at run time and injects them into the parameter payload before invoking CLI modules.

### 3.1.1 Standalone Process Tracking (GridProgressWindow)

Because the V3.3 spatial grid generates thousands of localized tiles (creating excessive console log noise), a standalone Tkinter class (`GridProgressWindow`) is injected directly into `rat_core.py`. When the GUI triggers a statewide bulk alignment or 4D enrichment subprocess, this floating UI is instantiated by the CLI script to provide a real-time, deterministic progress bar that floats above all other windows and safely updates as child threads complete their spatial tasks.

### 3.2 Mathematical and Geospatial Core (`rat_core.py`)

The shared processing engine for the entire suite. This module contains no GUI or plotting code. It provides UTM coordinate projection, USGS DEM elevation retrieval, UnivariateSpline smoothing, calculus-based curvature analysis, vertical parabolic regression, KDTree spatial indexing, and the `build_params()` parameter resolution function.

`build_params()` resolves parameters in the following order, with later entries taking precedence:

1. `DEFAULTS`: National calibration fallback values
2. `national_smoothing_factors.json`: State-specific calibrated factors, applied when STATE_FIPS is provided
3. `user_params`: Explicit overrides from the GUI or CLI caller

### 3.3 Alignment CLI (`rat_alignment_cli.py`)

The batch processing module for network-wide curve detection. It feeds fragmented HPMS geometries into the core engine, aggregates horizontal and vertical curve results, and produces CSV output, optional spatial formats (GeoJSON, GPKG, SHP), an interactive HTML alignment map, and an HTML summary dashboard.

**Multiprocessing Architecture:** To achieve rapid statewide scaling, this module utilizes a `ProcessPoolExecutor` driven by spatial bounding boxes (`bbox`) rather than Route IDs. By distributing geographic grid cells to parallel workers, the engine prevents adjacent routes from triggering redundant API downloads and evenly distributes the physical processing load across all available CPU cores.

### 3.4 Plan and Profile CLI (`rat_plan_profile_cli.py`)

The route-specific data pre-processor for engineering sheets. It isolates a single route, stitches fragmented geometry, tracks cumulative linear distance across disjointed segments to prevent overlapping, and exports smoothed and raw vertex tables to CSV for consumption by the PDF renderer.

The CLI also generates an HTML sensitivity analysis dashboard by slicing the route into 1-mile segments and evaluating multiple smoothing factors to assist with parameter adjustment.

### 3.5 Plan and Profile PDF Renderer (`rat_plan_profile_report_pdf.py`)

Generates multi-page engineering plan and profile PDFs from pre-processed vertex and curve CSVs. The renderer fetches USGS aerial basemap tiles, applies affine rotation so that the alignment flows left-to-right across each sheet, and uses `Matplotlib GridSpec` to produce stacked plan (top-down) and profile (elevation) views with full curve annotations. Pages are scaled to a 1,500-foot plan length.

### 3.6 4D Enricher (`hpms_4d_enricher_cli.py`)

Upgrades 2D HPMS geometry to Z/M-enabled `LINESTRING ZM` format. The enricher constructs a continuous 3D macro-profile for each route using the core smoothing engine, then uses a metric `cKDTree` to snap fragmented 2D segments to the smoothed 3D profile. Output includes a production CSV with `WKT_ZM` values, a GeoPackage with 3D geometry, and a projected SHP for Blender and CAD workflows.

**Multiprocessing Architecture:** To achieve rapid statewide scaling, this module utilizes a `ProcessPoolExecutor` driven by spatial bounding boxes (`bbox`) rather than Route IDs. By distributing geographic grid cells to parallel workers, the engine prevents adjacent routes from triggering redundant API downloads and evenly distributes the physical processing load across all available CPU cores.

### 3.7 National Calibration Engine (`rat_national_calibration_cli.py`)

Derives optimal smoothing factors for each state and functional system from HPMS geometry and USGS elevation data. The engine downloads HPMS data via the Socrata API, slices routes into 1-mile segments, draws a statistically representative sample using Cochran's formula, and evaluates each candidate factor against Gate 1 deviation ceilings. Elbow detection identifies the factor at the point of diminishing returns among the passing candidates. An early exit optimization terminates the sweep once the elbow region is confirmed, reducing run time on functional systems where the passing range is narrow.

The statistical sample size ($n$) for 1-mile chunks is drawn using Cochran's formula for finite populations, targeting a 95% confidence level ($Z = 1.96$) and a 5% margin of error ($e = 0.05$), assuming maximum variability ($p = 0.5$):

$$n_0 = \frac{Z^2 pq}{e^2}$$

$$n = \frac{n_0}{1 + \frac{n_0 - 1}{N}}$$

Where $N$ is the total number of 1-mile chunks available for that functional system.

The engine produces two outputs:

* **`national_smoothing_factors.json`:** State and FS-keyed smoothing factor dictionary loaded automatically by `build_params()`.
* **`calibration_audit.csv`:** Per-search audit record documenting selection method, confidence score, RMSE metrics at the selected factor, deviation from national default, and an override recommendation flag.

The script is located in `tools/`. See Section 14 for a complete walkthrough of the calibration workflow, including command syntax, runtime expectations, and the recommended iterative process for refining threshold values.

### 3.8 Calibration Diagnostics (`rat_calibration_diagnostics.py`)

Analyzes a completed `calibration_audit.csv` and produces a set of diagnostic reports and charts that assess the health of the threshold configuration used during the calibration run. The tool answers two questions: whether the Gate 1 ceilings are causing too many fallback selections (thresholds too tight), and whether elbow selections are occurring in flat RMSE curves with low confidence scores (thresholds too loose).

Outputs include a plain-text summary with per-functional-system threshold assessments and actionable recommendations, CSV reports covering selection method breakdown, ceiling proximity, confidence score distribution, and factor stability across states, and a set of PNG charts for visual review.

The script is located in `tools/`. See Section 14.4 and 14.5 for usage instructions and guidance on interpreting the diagnostic output.

### 3.9 Validator (`rat_results_validator.py`)

Automated QA/QC module. Scans alignment CSV and 4D enriched outputs for required column presence, mathematically invalid records (negative lengths, inverted distance ranges), and invalid categorical values. Reports hard failures and warning-level anomalies to the console with a final PASS/FAIL summary. The script is located in `tools/`.

---

## 4. Input Data and File Handling

### 4.1 FHWA Socrata API

The primary input mode. The GUI connects to the FHWA HPMS Socrata database and filters by State FIPS code, functional system, and facility type before downloading data. The client applies a 120-second timeout per request and raises an error on non-2xx responses. The URL for the API is: `https://datahub.transportation.gov/resource/42um-tgh5.json`.

**Note:** HPMS facility type codes follow the HPMS Field Manual definitions: 1 = One-Way Roadway, 2 = Two-Way Roadway, 4 = Ramp, 5 = Non-Mainline, 6 = Non-Inventory Direction, 7 = Planned/Unbuilt. The default filter includes types 1 and 2 (mainline roadways only). Including type 4 will add interchange ramps to the output, which will produce short curves and atypical geometry in the alignment results.

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

4. Where a gap between segments exceeds the snap tolerance, indicating a genuine discontinuity such as a missing segment, a route split at a state line, or an unbridged water crossing,  the stitcher begins a new LineString rather than forcing a connection. The resulting output may therefore contain multiple LineStrings for a single route, each representing a contiguous portion of the alignment. Downstream processing treats each portion independently and assigns a `Part` index to all output records so that results from disjointed geometry can be distinguished.

**Why this matters for curve detection.** A smoothing spline fitted to a properly stitched continuous LineString produces heading and curvature values that reflect the actual roadway geometry. The same spline fitted to a raw fragmented segment produces values that are partially artifacts of where the segment happened to start and end. Route stitching is the prerequisite that makes all subsequent analysis geometrically meaningful.

**Relationship to linear referencing.** After stitching, the engine uses the Start_MP and End_MP values from the source segments to establish proportional linear reference positions along the stitched geometry. This mapping is described in Section 5.9.

**Note.** If routes are being split into an unexpectedly large number of parts, the snap tolerance may be too tight for the precision of the source data. This value can be adjusted by modifying the `snap_tol` default in `stitch_linestrings_ordered()` in `rat_core.py`.

### 5.2 Spline Smoothing

**The problem.** HPMS geometry is often digitized from screen clicks or GPS collection and contains micro-scale positional noise that would produce false-positive curves if fed directly to a curvature algorithm.

**The approach.** The core engine applies a `scipy.interpolate.UnivariateSpline` to the UTM-projected coordinates. Horizontal (X, Y) and vertical (Z) smoothing are decoupled, allowing independent stiffness control. The spline smoothing factor controls the trade-off between fidelity to the raw geometry and geometric smoothness. State and functional-system-specific factors derived by the National Calibration Engine replace the uniform defaults used in prior versions.

The smoothing spline $S(x)$ minimizes the penalized least-squares error between the raw coordinate $Z_i$ and the smoothed curve. The smoothing factor ($s$) acts as an upper bound on this residual variance:

$$\sum_{i=1}^{n} (Z_i - S(x_i))^2 \leq s$$

A larger $s$ value allows the spline to deviate further from the raw, noisy vertices to maintain a continuous, fluid alignment, whereas a smaller $s$ forces the spline to strictly trace the input data.

### 5.3 Heading Unwrapping

**The problem.** A road curving near true North will show a heading transition from approximately 359° to 1°, producing a false 358° deflection angle.

**The approach.** The engine applies heading unwrapping, allowing the compass value to accumulate continuously (e.g., 359°, 360°, 361°) rather than resetting at 360°. Deflection angles are computed from the unwrapped heading series.

### 5.4 Bridge and Water-Body Profile Repair

**The problem.** USGS DEMs represent bare earth. Where a highway crosses a bridge, the DEM surface drops to the streambed or valley floor, producing artificial dips in the elevation profile.

**The approach.** The core engine applies a valley test: if the road profile drops more than `DIP_THRESHOLD_FT` below the local topographic trend (estimated over `TREND_WINDOW_FT`), the engine suspends the road in the air and interpolates linearly across the gap. The maximum interpolation span is controlled by `BRIDGE_MAX_LEN_FT`. All downstream modules benefit from this repair because it is applied in the core before curve detection or 4D mapping.

The engine computes a baseline topographic trend ($\widetilde{Z}_{trend}$) using a rolling median of the raw elevations over the TREND_WINDOW_FT. The vertical deviation at any given vertex is:

$$Z_{deviation} = Z_{raw} - \widetilde{Z}_{trend}$$

The bridge repair algorithm suspends the profile and interpolates across the gap whenever the deviation violates the dip threshold:

$$Z_{deviation} < -DIP_{threshold}$$

### 5.5 Curve Endpoint Detection

**Horizontal.** The engine calculates the road's compass heading at the `DENSIFY_SPACING_FT` interval. When the heading rate of change exceeds `H_MIN_HEAD_CHANGE`, a curve candidate is initiated.

**Vertical.** The engine monitors the second derivative of the elevation profile. When the curvature of the grade exceeds `V_VC_THRESHOLD`, a vertical curve candidate is initiated.

### 5.6 Curve Geometry Computation

**Horizontal radius.** The engine computes instantaneous curvature $κ$ at every point using the first and second spatial derivatives of the smoothed coordinates. Radius is derived as $R = 1/κ$. The reported `Radius_m` is the representative value across the curve span; `Min_Radius_m` is the minimum (apex) radius, which drives severity classification.

**Deflection angle.** Total deflection angle (Delta) is the absolute change in unwrapped heading from PC to PT.

**Vertical K-value.** The engine uses linear regression over the `REGRESSION_WINDOW_FT` window on each side of the vertical curve to estimate incoming grade $G1$ and outgoing grade $G2$ and the calculated curve length $L$. Algebraic difference $A = G2 − G1$ and $K = L / |A|$.

**External offset.** The engine evaluates vertical significance using the external offset ($E$) at the PVI, calculated as:

$$E = \frac{|A|L}{800}$$

### 5.7 Spiral Transition Detection

**The problem.** Many highway curves include clothoid (Euler spiral) transition sections between the tangent and the central circular arc. On a simple curve the curvature steps
immediately from zero to a constant value at the PC. On a spiralized curve the curvature ramps gradually from zero at the TS (tangent-to-spiral) to its maximum at the SC
(spiral-to-curve), holds constant across the central arc, then ramps back to zero at the CS (curve-to-spiral) and ST (spiral-to-tangent). Treating a spiralized curve as a simple
curve overstates the PC and PT positions and misrepresents the alignment geometry.

**The approach.** After the curvature array for each detected horizontal curve is computed, the engine identifies the central circular arc by bracketing the region where
instantaneous curvature is at or above 85% of the peak curvature value. The distance from the detected curve start to the entry of this central region is the entering spiral
tail length (TS to SC); the distance from the exit of the central region to the detected curve end is the exiting spiral tail length (CS to ST).

If either tail is longer than 150 ft, the curve is classified as `Spiral` in the `Transition_Type` output field. Otherwise it is classified as `Simple`. The 150 ft
threshold reflects the minimum spiral length that is geometrically meaningful at highway speeds; shorter apparent tails are treated as curvature ramp artifacts of the spline
rather than designed transitions.

For spiral curves, four additional linear reference point fields are populated in the output:
`SC_Dist`, `CS_Dist`, `Calibrated_SC_MP`, and `Calibrated_CS_MP`, identifying the boundaries of the central circular arc. For simple curves these fields are not populated.

### 5.8 Minimum Apex Radius and Severity Classification

**The problem.** Real-world curves include spiral transitions. Averaging radius across a curve dilutes the severity of the apex.

**The approach.** While engineers work with radius in meters internationally, U.S. design standards rely heavily on the Degree of Curvature ($D$). `rat_core.py` uses a conversion constant (`METRIC_R_TO_IMPERIAL_D = 1746.38`) to convert metric radius to imperial degree of curvature for binning. Severity bins (A–F) are assigned based on the minimum instantaneous radius (`Min_Radius_m`), not the mean radius. This approach is consistent with road safety analysis practices that identify the most restrictive geometric condition along the curve.

The metric apex radius ($R_m$) is converted to an imperial Degree of Curvature ($D$) using the 100-foot arc definition prior to severity binning:

$$D = \frac{1746.38}{R_m}$$

### 5.9 Linear Reference Proportional Calibration

Spline smoothing physically shortens the line. After smoothing, the engine maps curve positions back to the original linear reference system by computing what fraction of the total smoothed length each curve start and end position represents, then applying that fraction to the original `Start_MP` and `End_MP` range.

### 5.10 Directionality and Compound Curves

**Left/Right.** A positive deflection angle from PC to PT (heading increasing) corresponds to a Right curve.

**Compound curves.** When merge is enabled, the engine evaluates the gap between adjacent same-direction curves against `MERGE_GAP_FT` and fuses them into a compound curve. Reverse (S-curve) sequences are always kept separate to preserve the inflection point.

### 5.11 Savitzky-Golay Buffer Requirement

The Savitzky-Golay filter (`scipy.signal.savgol_filter`) used in heading smoothing requires that the input have at least `H_BASE_SMOOTH_WINDOW + 2` points. The engine enforces this constraint by skipping route chunks with insufficient geometry rather than adjusting the window to avoid degraded results.

The general equation for the Savitzky-Golay filter can be expressed as follows:

$$y_t = \sum_{j=-n}^{n} b_j x_{t+j}$$

**Where:**

* $y_t$ is the smoothed value at time $t$.
* $x_{t+j}$ are the neighboring data points around $t$ (from $t-n$ to $t+n$).
* $b_j$ are the convolution coefficients determined by fitting a polynomial of degree $k$ to the data points.

**Polynomial Fitting**

The coefficients $b_j$ are calculated by minimizing the least-squares error between the actual data points and the polynomial fit. The polynomial can be of any degree $k$, and the choice of $k$ affects the smoothness of the output. The filter operates over a window of $2n+1$ points, where $n$ is the number of points on either side of the central point being smoothed.

### 5.12 Calculus-Based Horizontal Curvature

Curvature $\kappa$ is derived from the first and second spatial derivatives of the smoothed coordinate sequence ($x, y$) with respect to arc length.

$$\kappa = \frac{|x'y'' - y'x''|}{(x'^2 + y'^2)^{3/2}}$$

Where $x'$ and $y'$ are the first derivatives, and $x''$ and $y''$ are the second derivatives. The radius is then simply extracted as $R = 1/\kappa$. This approach produces a continuous curvature function from which both apex and mean radius can be extracted.

### 5.13 Vertical Parabolic Fitting

The engine applies second-degree polynomial regression (`numpy.polyfit`) to the smoothed elevation profile within each detected vertical curve span, formatted as:

$$Z(x) = ax^2 + bx + c$$

Instantaneous grade is extracted via the first derivative:

$$G = \frac{dZ}{dx} = 2ax + b$$

Grade values at the PVC and PVT are derived from the first derivative of the fitted parabola. This produces grade values consistent with the parabolic assumption used in highway geometric design.

### 5.14 Functional System Scaling

Design standards and operating characteristics differ substantially across functional systems. An Interstate corridor has different smoothing requirements than a local collector. The engine selects horizontal and vertical smoothing factors based on the predominant functional system of each route. In version 3.3, these factors are loaded from `national_smoothing_factors.json` when available, providing state-specific calibration rather than uniform national defaults.

### 5.15 High-Resolution LiDAR Integration (Upgraded in v3.3)

**The problem.** While 1/3 arc-second (approximately 10-meter) USGS DEMs provide sufficient resolution for statewide macro-analysis, they lack the pixel density required to accurately sample elevations along densified route geometry. Prior versions were limited to 10-meter resolution by memory constraints that prevented statewide 1-meter processing.

**Tile downloads.** For the calibration engine, each worker requests a custom 1-meter GeoTIFF covering only its assigned grid cell and deletes it immediately after use, keeping storage overhead near zero during calibration runs. For alignment and enrichment pipelines, completed tiles are stored in a persistent shared cache directory so that adjacent cells sharing the same underlying terrain don't re-download the same data. The cache persists between runs, making repeated or multi-state runs significantly faster.

The engine includes retry logic for USGS server errors and intercepts NaN arrays at coastal boundaries before they reach the spline backend. Per-worker directory isolation prevents tile name collisions during parallel state processing.

See Section 2.5 for a full description of the grid tile architecture and its design rationale.

---

## 6. Experimental Features

The following capabilities are included in the v3.3 codebase and produce output, but have not yet been validated against field measurements or independent benchmarks. Known limitations are described for each feature. Output from these features should be treated as preliminary and should not be used as the basis for engineering decisions without independent verification. These features are under active development and are expected to be promoted to production status in a future release.

### 6.1 Micro-Jitter Pavement Roughness Proxy (IRI Proxy)

**The Concept.** Pavement roughness can be characterized by isolating high-frequency vertical oscillations (chatter) from the long-wavelength topographic trend (hills/valleys).

**The Math.** The engine isolates the vertical residual ($Z_{res} = Z_{raw} - Z_{smooth}$) and applies a **second-order Butterworth High-Pass Filter**.

* **Filter Cutoff.** The filter uses a 30-meter wavelength cutoff to strip out macro-topography.
* **High-Pass Logic.** The transfer function $H(s)$ isolates high-frequency noise:

$$|H(j\omega)| = \frac{1}{\sqrt{1 + (\frac{\omega_c}{\omega})^{2n}}}$$

* **Metric Output.** The proxy is reported as a moving Root Mean Square (RMS) of the filtered high-frequency noise ($Z_{filtered}$) over the evaluation window, converted to inches:

$$Jitter_{RMS} = \sqrt{\frac{1}{N} \sum_{i=1}^{N} (Z_{filtered, i})^2}$$

Where $N$ is the number of vertices within the 30-meter moving window. While not a replacement for laser profilometer data, it provides a relative "Roughness Index" for screening pavement health.

**Status.** The micro-jitter determination is working, but when compared to IRI, the correlation is weak and the magnitude is significantly less than the IRI for the same location. Further testing and calibration is required before this feature can be reliably used.

### 6.2 3D Stopping Sight Distance (SSD) Simulation
**The Concept.** To automate safety screening for crest curves, the engine performs a vertical ray-casting simulation at every vertex along the roadway spline.

**The Methodology.** The simulator uses a mathematical line-of-sight projection based on AASHTO A Policy on Geometric Design of Highways and Streets standards:
* **Driver Eye Height ($h_1$)**: 3.50 feet above the smoothed Z-spline.
* **Object Height ($h_2$)**: 2.00 feet above the smoothed Z-spline (representing tail-lights or small hazards).
* **Ray-Casting Algorithm.** For vertex $i$, the engine projects a vector to every subsequent vertex $j$ up to a 1,500-foot search radius. If any intermediate vertex $k$ exists where $Z_{road} > Z_{ray}$, the sightline is considered obstructed.
* **Formula.** The Available Sight Distance ($S$) is the distance to the furthest vertex $j$ where the following condition remains true:

$$Z_{k} \leq Z_{eye} + \left( \frac{Z_{target} - Z_{eye}}{D_{ij}} \right) \cdot D_{ik}$$

Where $D$ is the continuous distance along the route.

**Status.** The stopping sight distance determination appears to be working correctly but requires further testing. One noted issue is impact of DEM variability especially when coupled with lower vertical smoothing factors. Please exercise caution if using this for safety or planning purposes.

### 6.3 Network-Level Interchange Topology Mapping

**The Problem.** Linear HPMS data typically treats overpasses and at-grade intersections as identical X/Y crossing points, complicating network routing and vertical clearance analysis.

**The Approach.** The engine utilizes a statewide $c$-dimensional spatial tree (cKDTree) to evaluate the 3D topology of the entire processed network.
1. **Collision Detection.** The engine identifies all points where two different RouteId strings fall within a 15-foot spatial radius.
2. **Vertical Delta Evaluation.** For every collision pair, the engine calculates the absolute elevation difference ($\Delta Z$).
3. **Topology Classification.** 

    - **Grade-Separated ($\Delta Z > 10.0 \text{ ft}$):** Identified as an overpass/underpass structure.
    - **At-Grade ($\Delta Z \leq 5.0 \text{ ft}$):** Identified as a physical intersection.
    - **Unknown ($5.0 \text{ ft} < \Delta Z \leq 10.0 \text{ ft}$):** Marginal clearance requiring engineering review.

**Status.** This feature needs further development and testing. The model is correctly identifying many intersections and interchanges, but there is a high number of false positives, false negatives, and misclassifications. Users who want to test this feature are encouraged to select all functional systems and all facility types, or at least 1 - 6, since the model only evaluates routes that have been downloaded from Socrata.

### 6.4 AASHTO Superelevation Heuristic

**The problem.** True superelevation (banking) is rarely included in raw linear referenced datasets or 2D HPMS geometry, yet it is a critical component for kinematic evaluations and safety screening.

**The approach.** Because the tool cannot measure physical cross-slope from a 2D centerline, the engine applies a conservative, rules-based heuristic to estimate the design superelevation rate ($e$) based on the derived curve radius. This logic mirrors standard AASHTO design guidelines, assuming that tighter curves are banked to their maximum practical limits, while broad, sweeping curves simply maintain a normal pavement crown.  

After the metric radius is calculated and converted to feet ($R_{ft}$), the engine applies the following step function to assign a superelevation percentage:  

$$e = \begin{cases} 8.0\% & \text{if } R_{ft} < 1500 \\ 6.0\% & \text{if } 1500 \leq R_{ft} < 3000 \\ 4.0\% & \text{if } 3000 \leq R_{ft} < 6000 \\ 2.0\% & \text{if } 6000 \leq R_{ft} < 99999 \\ 0.0\% & \text{if } R_{ft} \geq 99999 \end{cases}$$

* **Tight curves** ($R_{ft} < 1500$) are assigned an $8.0\%$ superelevation, representing maximum typical banking.  
* **Broad curves** ($6000 \leq R_{ft} < 99999$) are assigned a $2.0\%$ superelevation, representing a standard normal crown.  
* **Tangent sections** ($R_{ft} \geq 99999$) receive a $0.0\%$ assignment, establishing a flat baseline.  

When adjacent same-direction curves are fused via the ENABLE_MERGE logic, the engine recalculates the superelevation assignment based on the newly blended curve radius.

**Status.** This feature has been fully implemented but the results have not been tested and verified. Please exercise caution if using this for safety or planning purposes.

## 7. Units of Measurement

### 7.1 Metric Core

All internal geometry computations are performed in meters on a flat UTM projection. WGS84 coordinates are projected to the appropriate UTM zone at the start of each route's processing and back-projected to WGS84 for output. Using a consistent metric core prevents unit conversion rounding errors during iterative calculations.

### 7.2 Imperial User Interface

User-facing configuration parameters (spacing, curve length thresholds, merge gaps, smoothing factors) are specified in feet. HPMS route measures are read and reported in miles. The plan and profile PDF outputs use foot-based stationing. The conversion constant `FEET_PER_METER = 3.28084` is applied at the input and output boundaries.

---

## 8. Outputs and Intended Use

### 8.1 Alignment Outputs

- Horizontal and vertical curve tables (CSV)
- Alignment vertices file (CSV) uses `Dist_Mi`; plan/profile vertices file uses `Dist_Ft`
- Optional spatial exports (GeoJSON, GeoPackage, Shapefile)
- Interactive HTML map with severity-classified curve styling
- HTML summary dashboard including system health, density, severity distribution, and diagnostic charts

### 8.2 Plan and Profile Outputs

File names follow the convention `plan_profile_<RouteId>_MP_<begin>_to_<end>`.

- Vertex table (CSV) with smoothed and raw coordinates, elevation, grade, and curve type at each point
- Horizontal and vertical curve tables (CSV)
- Multi-page annotated PDF plan and profile sheets
- HTML sensitivity analysis dashboard

**Spiral transition display.** When a horizontal curve is classified as `Spiral`, the plan view renders the three geometric components of the curve in visually distinct styles.
The central circular arc (SC to CS) is drawn as a solid thick line in the curve's severity color. The entering spiral tail (TS to SC) and exiting spiral tail (CS to ST)
are drawn as dashed lines in the same color at slightly reduced opacity, indicating the transition zones where curvature is ramping rather than constant. Simple curves are drawn
as a single solid thick line with no distinction between sections.

The annotation box for a spiral curve is prefixed with `[SPIRAL]` and reports four reference points, TS, SC, CS, and ST, as calibrated route linear reference point, in place of the
PC and PT reference points reported for simple curves.

### 8.3 4D Enrichment Outputs

- Production CSV with `WKT_ZM` column
- GeoPackage with 3D geometry in WGS84
- Projected SHP in local UTM for Blender and CAD interoperability

### 8.4 Calibration Outputs

- `national_smoothing_factors.json`: State and FS-specific smoothing parameters
- `calibration_audit.csv`: Per-search audit record for manual review and override decisions

---

## 9. Practical Tuning Guidance

### 9.1 Symptom-Based Adjustments

| Symptom | Parameter Adjustments |
| :--- | :--- |
| Too many small horizontal curves | Increase `H_SMOOTH_FACTOR`; increase `H_MIN_DELTA`; increase `H_MIN_CURVE_LENGTH_FT` |
| Legitimate tight curves being missed | Decrease `H_SMOOTH_FACTOR`; decrease `H_MIN_CURVE_LENGTH_FT`; decrease `H_MIN_DELTA` |
| Noisy vertical output | Increase `V_SMOOTH_FACTOR`; increase `V_MIN_CURVE_LENGTH_FT`; increase `V_MIN_GRADE_CHANGE` |
| Bridge dips remain in profile | Increase `TREND_WINDOW_FT`; decrease `DIP_THRESHOLD_FT`; increase `BRIDGE_MAX_LEN_FT` |
| Overpass spikes appear as false CREST curves | Increase `V_SMOOTH_FACTOR` |
| Ramps or non-mainline geometry appearing in output | Verify FACILITY_TYPE_FILTER in the GUI includes only types 1 and 2; type 4 is ramps |

### 9.2 Plan View and Profile View Display Adjustment

When producing plan and profile sheets for urban areas with sharp turning geometry, the plan view alignment may extend outside the default ±200 ft offset window. The y-axis limits can be expanded by modifying `ax_plan.set_ylim(-200, 200)` in `rat_plan_profile_report_pdf.py`. For mountainous routes where the profile exceeds the default ±100 ft elevation window, adjust `ax_prof.set_ylim(avg_elev - 100, avg_elev + 100)` accordingly. Reducing these limits (e.g., ±50 ft for the profile) can make the offset between the smoothed and raw alignment easier to detect during accuracy review.

---

## 10. QA/QC and Validation Workflow

The validator (`rat_results_validator.py`) evaluates alignment and 4D outputs against defined integrity criteria.

**Hard failure conditions:**
- Required columns absent
- Curve lengths at or below zero
- End distance not greater than start distance

The automated validator enforces strict geometric and linear referencing logic. Hard failures are triggered if the output violates any of the following physical constraints: 

* **Curve Length Validity:** The physical length of any derived curve must be strictly positive ($L > 0$).
* **Spatial Directionality:** The ending distance must strictly exceed the starting distance ($D_{end} > D_{start}$).
* **Linear Reference Point Continuity:** Route vertices must maintain monotonically increasing linear reference measures, where $M_{i+1} \geq M_i$, ensuring no spatial folding or reverse-digitization artifacts persist in the final output.

**Warning-level conditions:**
- Nonpositive radius or K-values
- Invalid categorical fields (Dir, Type, Bin, Grade_Bin)

**4D output checks:**
- `WKT_ZM` column presence and parseability
- Z and M value range summaries
- NaN Z-value detection

Recommended practice: run the alignment module, run the validator, review exception outputs before publishing.

---

## 11. Output Column Reference

### 11.1 Universal Fields

| Field | Description |
| :--- | :--- |
| `RouteId` | Route identifier from source dataset, normalized to uppercase. |
| `Start_Dist` / `End_Dist` | Curve start and end positions along the processed route axis, in meters. Used for internal QA; not intended as a primary delivery field. |
| `Length_m` | Computed curve length in meters. |
| `Calibrated_Start_MP` / `Calibrated_End_MP` | Linear reference positions mapped back from the smoothed geometry to the original route measure range. |
| `Part` | Index of the disjoint geometry chunk from which the curve was derived. Routes with gaps in HPMS data may produce multiple parts. |
| `FSystem` | Functional system code from the source data. |

### 11.2 Horizontal Curve Fields

| Field | Description |
| :--- | :--- |
| `Radius_m` | Representative radius for the curve segment, in meters. |
| `Min_Radius_m` | Minimum (apex) instantaneous radius along the segment. Severity classification is based on this value. |
| `Delta` | Total deflection angle across the curve span, in degrees. |
| `Dir` | Curve direction relative to route digitization direction: `Left` or `Right`. |
| `Bin` | Severity class A through F, based on `Min_Radius_m`. |
| `Merge_Status` | `Simple` for individual curves; `Compound` when merge logic has fused adjacent same-direction curves. |
| `Transition_Type` | `Simple` for standard circular curves. `Spiral` when either the entering or exiting curvature transition tail exceeds 150 ft, indicating a clothoid spiral transition. |
| `SC_Dist` / `CS_Dist` | Distance along the route axis to the spiral-to-curve (SC) and curve-to-spiral (CS) points, in meters. Populated only when `Transition_Type` is `Spiral`. |
| `Calibrated_SC_MP` / `Calibrated_CS_MP` | Linear reference points for the SC and CS points, mapped back to the original HPMS route measure range. Populated only when `Transition_Type` is `Spiral`. |

> `Bin` is a severity classification, not a design speed rating. Direction depends on digitization direction, which may not correspond to the direction of travel.

### 11.3 Vertical Curve Fields

| Field | Description |
| :--- | :--- |
| `Grade_In` / `Grade_Out` | Estimated incoming and outgoing grades in percent. |
| `Alg_Diff` | Algebraic grade difference (G2 − G1), signed. |
| `K_Value` | Approximate K-value: curve length divided by the absolute algebraic difference. |
| `Type` | `CREST` or `SAG`. |
| `E` | Vertical offset metric used in significance filtering. |
| `Grade_Bin` | Severity class A through F based on algebraic difference magnitude. |

> K-value interpretation depends on the route class, design speed environment, and terrain context.

### 11.4 Plan and Profile Vertex Fields

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

### 11.5 4D Enrichment Fields

| Field | Description |
| :--- | :--- |
| `WKT_ZM` | Well-known text representation of the 3D/4D geometry: `LINESTRING ZM (lon lat elev_m milepost, ...)`. |
| `geometry_3d` | In-memory Shapely LineString with Z coordinate (written to GeoPackage; not in CSV). |

### 11.6 Calibration Audit Fields

Key fields in `calibration_audit.csv` for manual review:

| Field | Description |
| :--- | :--- |
| `selection_method` | One of: `elbow`, `flat_curve`, `flat_terrain_default`, `highest_safe`, `composite_fallback`, `absolute_fallback`. See Section 15.2 for the meaning and reliability implications of each method. |
| `confidence_score` | 0–100 composite quality score. Values below 40 trigger `override_recommended = True`. |
| `override_recommended` | `True` when selection method is a fallback or confidence is low. |
| `deviation_from_default` | Ratio of selected factor to national default. Values above 2.0 or below 0.5 are outliers. |
| `ceiling_proximity_pct` | Selected RMSE as a percentage of the applicable ceiling. Values above 90% indicate a marginal result. |
| `n_passing` | Number of sweep factors that passed Gate 1. Low values indicate limited calibration data. |
| `std_v_rmse_at_selected` / `std_h_rmse_at_selected` | Standard deviation of RMSE across sampled chunks at the selected factor. High values indicate a noisy or heterogeneous sample. |

---

## 12. Interactive Map and Dashboard

### 12.1 HTML Alignment Map

The interactive map provides a curve geometry layer for statewide pattern review and stakeholder communication. Curves are color-coded by severity bin (A = green through F = purple). Compound horizontal curves are displayed with a dashed line style. Tooltips show RouteId, calibrated linear reference point, severity class, and curve geometry attributes.

**Note:** The geometry embedded in the interactive HTML map undergoes simplification at a tolerance of 0.00005 degrees (approximately 5 meters) to prevent web browser memory limits from being exceeded on large statewide networks. This simplification is not visible at typical map zoom levels. It does not affect the standalone GeoJSON, GeoPackage, or Shapefile exports, which always contain full-resolution geometry. Uncheck "Simplify Web Geometry" in the GUI to disable simplification in the HTML map as well.

### 12.2 Summary Dashboard

The summary dashboard (`alignment_dashboard_<state>_<date>.html`) provides the following charts:

* **System Health:** RMSE deviation by functional system with P50, P80, and P95 percentile bands.
* **Curve Density:** Horizontal and vertical curves per 100 route-miles by functional system.
* **Cumulative Severity Distribution:** CDF of horizontal and vertical severity classes.
* **Severity by Functional System:** Stacked bar charts showing severity class distribution per FS.
* **CREST vs. SAG:** Vertical curve type counts by functional system.
* **Compound Curve Percentage:** Fraction of horizontal curves classified as compound by FS.
* **Advanced Diagnostics:** Curve length vs. radius scatter plot and K-value distribution histogram.

### 12.3 Plan and Profile Sensitivity Dashboard

The `rat_plan_profile_cli.py` produces an HTML sensitivity analysis dashboard by slicing the target route into 1-mile segments and evaluating a set of candidate smoothing factors. The dashboard reports horizontal curvature variance and vertical RMSE, maximum deviation, and grade variance for each factor. Use the dashboard to identify the factor at which the variance and RMSE metrics stabilize; additional stiffness beyond that point produces diminishing returns while increasing the risk of displacing curve endpoints.

---

## 13. Known Algorithmic Anomalies

### 13.1 Orthogonal Stair-Step Digitization

Some digitizers represent curved roads with abrupt orthogonal line segments. The engine may interpret these as alternating reverse curves. Increase `H_SMOOTH_FACTOR` or use merge logic to consolidate the resulting micro-curves.

### 13.2 GPS Multipath Jitter

Dense urban environments can produce GPS multipath errors that manifest as vibrating linework. The second derivative of this pattern produces false-positive high-severity curves. Increase `H_SMOOTH_FACTOR` if the output contains an implausible density of short curves.

### 13.3 LiDAR Water-Body Artifacts

DEM artifacts over large water bodies where LiDAR returns were scattered can create artificial pits in the bare-earth surface. If these pits exceed `BRIDGE_MAX_LEN_FT`, the bridge repair logic will not span them. Increase `TREND_WINDOW_FT` to anchor the baseline trendline to the approach geometry rather than the water surface, or increase `BRIDGE_MAX_LEN_FT` to allow wider span interpolation.

### 13.4 Collinear Vertex Redundancy

Some datasets contain densely packed collinear vertices on straight segments. The curvature denominator approaches zero in these cases, creating numerical instability. The `densify_coords_line` function redistributes vertices at consistent intervals, eliminating collinear redundancy before curvature calculations are performed.

### 13.5 Overpass Z-Spikes

Wide overpasses are occasionally misclassified as solid ground by the LiDAR processing algorithm, producing upward elevation spikes of 20 to 50 feet. The engine will attempt to fit a sharp CREST curve to this artifact. Increase `V_SMOOTH_FACTOR` to give the vertical spline sufficient stiffness to pass through the artifact without fitting a curve to it.

### 13.6 Curve Endpoint Displacement

The smoothing spline fits a globally continuous function across the full route rather than processing each curve in isolation. As a result, the spline begins responding to approaching curvature slightly before the physical curve starts, causing the detected PC and PT (or PVC and PVT for vertical curves) to extend into the adjacent tangent sections. The effect is more pronounced at higher smoothing factors and on curves with gradual transitions, and less pronounced on sharp curves where the curvature signal is strong relative to the spline window.

This is a known limitation of spline-based alignment derivation from observed geometry. The plan and profile sheets display both the smoothed centerline and the raw input geometry simultaneously, which allows the analyst to visually assess the extent of endpoint displacement for any given curve.

A correction method based on curvature threshold trimming is under development for a future release. The approach would identify the point along each detected curve where instantaneous curvature first exceeds a minimum significance threshold and report that trimmed position as the PC or PT, moving the endpoints inward from the spline-extended positions to where the curve is geometrically meaningful. Until that correction is implemented, reported `Calibrated_Start_MP` and `Calibrated_End_MP` values should be understood as conservative estimates that may slightly overstate curve length, particularly on lower-speed networks where shorter smoothing factors reduce but do not eliminate the effect.

### 13.7 DEM Micro-Undulations (Clustered Vertical Curves)

In very flat terrain, 1/3 arc-second USGS DEMs contain sub-foot surface noise, and minor real-world features like box culverts or farm underpasses present as "micro-undulations." 

If the `V_MIN_GRADE_CHANGE` is set too low (e.g., 0.5%), the highly accurate vertical smoothing spline will mathematically trace these 1-foot humps, generating massive clusters of false-positive "micro-curves" along otherwise flat corridors. Ensuring `V_MIN_GRADE_CHANGE` is set to the recommended default of 1.5% (or higher) filters out this surface noise and correctly consolidates the macro-profile.

### 13.8 Vertical Curve Classification at Bridge Anchor Points

The bridge detection algorithm in `fix_profile_by_deviation()` corrects the smoothed vertical profile at river crossings and other underpasses by interpolating a flat or gently graded line between anchor points on either side of the span. While the corrected profile elevation is geometrically accurate, the transition between the interpolated bridge line and the surrounding road grade can produce short SAG curve classifications at the anchor points. These are mathematical artifacts of the spline fitting at the boundary between the corrected and uncorrected profile segments, not genuine sag curves in the road geometry.

These artifacts are most likely to appear at bridge crossings where the approach grades are gentle and the valley is wide, as these conditions produce the most gradual anchor point transitions. They do not affect the accuracy of the corrected elevation profile, and the associated curve lengths are typically short. However, they may appear in the vertical curve output and should be interpreted with caution at locations known to cross water features or other underpasses.

A future refinement may suppress vertical curve classifications that fall within or immediately adjacent to detected bridge spans.

### 13.9 Validator Coverage for New Output Files

The Results Validator (`rat_results_validator.py`) currently checks the horizontal and vertical curve CSVs and the 4D enriched output. The alignment vertices file and section scores file introduced in v3.3 are not yet covered. A future update will add integrity checks for these outputs, including milepost continuity, bin value validity, and coordinate bounds.

### 13.10 V Calibration Signal Absence on Flat Terrain

In flat or gently rolling terrain, the vertical RMSE produced by the V sweep is nearly identical across all candidate smoothing factors. This occurs because `fix_profile_by_deviation()` linearizes bridge dips and other profile anomalies before the vertical spline is fitted, leaving little residual variation for the spline to respond to on roads with minimal grade change. When this happens, the elbow algorithm has no meaningful curve to analyze and will select an arbitrary factor driven by floating-point noise rather than genuine geometric signal.

The calibration engine detects this condition by computing the range of V RMSE values across the full sweep. If the range is below 0.05 ft, the engine records `flat_terrain_default` as the selection method and applies the national default factor for that functional system instead of an elbow selection. A log message is written identifying the state, functional system, and measured RMSE range so the condition is visible in the run log.

This behavior is not a failure. It correctly reflects that the vertical smoothing factor has negligible effect on alignment quality for that road class in that state. Any factor in the sweep range would produce essentially the same output, so the national default is as good as any calibrated value. States and functional systems where `flat_terrain_default` is recorded consistently, typically FS2–7 in the Great Plains, Gulf Coast, and mid-Atlantic lowlands, should be treated as using the national default rather than a state-specific calibration for their V factor.

The condition is most prevalent on lower functional systems (FS3–7) where road geometry tends to follow the terrain closely with limited engineered grade transitions, and on flat states regardless of functional system. Mountainous states and Interstate corridors with significant engineered grade changes will typically produce real V RMSE variation and elbow selections even on otherwise flat terrain.

---

## 14. Running the Calibration Script

The calibration script derives state and functional-system-specific smoothing factors from HPMS geometry and USGS elevation data. It can be run for a single state during development or threshold testing, or for the full national network to produce a complete `national_smoothing_factors.json`. This section covers everything needed to run the script, monitor the run, diagnose results, and decide when the calibration is ready for production use.

### 14.1 Prerequisites

Before running the calibration script, confirm the following:

**Python environment.** All RAT Suite dependencies must be installed. The calibration script requires `numpy`, `pandas`, `scipy`, `shapely`, `pyproj`, `requests`, `rasterio`, and `geopandas` in addition to the standard library.

**Socrata app token.** The script pages through the FHWA HPMS Socrata API to retrieve route geometry. Unauthenticated requests are rate-limited and will produce fetch times of 5–10 minutes per state. Register for a free app token at `https://datahub.transportation.gov` and enter it in the `SOCRATA_TOKEN` constant near the top of `rat_national_calibration_cli.py`.

**DEM cache directory.** The calibration engine downloads 1-meter DEM tiles ephemerally; each worker downloads a custom tile for its assigned grid cell, uses it, and deletes it immediately. Peak temporary storage during a national calibration run is approximately 2–5 GB. Tiles do not accumulate between calibration runs. The alignment and enrichment pipelines use a different strategy: standard 1-degree tiles are downloaded once to a persistent shared cache (`align_1m_cache/` or `enrich_4d_1m_cache/`) and reused by all workers. The full 1-degree cache for a national alignment run requires approximately 32 GB and persists between runs, making subsequent runs significantly faster. Designate a directory on a drive with at least 40 GB free to accommodate both the calibration temporary tiles and the persistent alignment/enrichment cache.

**Output directory.** The `--outdir` argument specifies where the run log is written if `Tee-Object` is used. The two primary outputs, `national_smoothing_factors.json` and `calibration_audit.csv`, are always written to the `core/` directory regardless of `--outdir`.

**Archive prior results.** Before a national run, move the existing `national_smoothing_factors.json` and `calibration_audit.csv` from `core/` to an archive folder. This preserves the prior run for comparison and prevents the new run from appending to the existing audit file.

### 14.2 Running the Calibration Engine

The script is invoked from the RAT Suite root directory. The minimum required arguments are `--outdir` and `--demdir`.

**Single state:**

py tools/rat_national_calibration_cli.py
--outdir  "path/to/output"
--demdir  "path/to/dem/cache"
--state   31

**All states with core allocation and log capture:**

py tools/rat_national_calibration_cli.py
--outdir        "path/to/output"
--demdir        "path/to/dem/cache"
--state         ALL
--total-cores   30
--reserved-cores 2
2>&1 | Tee-Object -FilePath "path/to/output/calibration_run_log.txt"

**Arguments:**

| Argument | Required | Description |
| :--- | :---: | :--- |
| `--outdir` | Yes | Output folder for the run log. |
| `--demdir` | Yes | Directory for temporary DEM tile downloads. |
| `--state` | No | Two-digit state FIPS code, or `ALL` for a full national run. Defaults to `ALL`. |
| `--total-cores` | No | Total logical CPU cores available. Defaults to all detected cores. |
| `--reserved-cores` | No | Cores to hold back for OS and other work. Defaults to 2. |

**Core allocation.** When running a single state, all available cores are assigned to the inner factor sweep. When running multiple states, cores are split between state-level parallel workers and per-state sweep workers. With 30 cores and 2 reserved, the engine allocates 5 state workers × 5 sweep workers each by default. Adjust `--total-cores` and `--reserved-cores` to match your hardware.

**Log capture.** The `2>&1 | Tee-Object` construct captures both stdout and stderr to a log file while also displaying output in the terminal. PowerShell displays logging output in red because Python's logging module writes to stderr by default; this is cosmetic and does not indicate an error. On Windows PowerShell 5.1 the `-Encoding utf8` parameter is not supported and should be omitted.

### 14.3 Monitoring the Run

The script logs progress at each stage of processing. A healthy run produces output in the following pattern for each state:

1. **Socrata fetch** - one log line per page of 100,000 rows retrieved, followed by a fetch complete summary.
2. **Geometry slicing** - one log line per functional system as 1-mile chunks are generated.
3. **DEM tile downloads** - progress reported every 25 tiles. Occasional `USGS server overloaded` warnings are normal; the retry logic handles them automatically.
4. **H sweep** - one log line per factor evaluated, showing H_RMSE, MaxDev, and PASS/FAIL status. An early exit message appears when the elbow region is confirmed.
5. **V sweep** - either a flat terrain detection message (for states and functional systems with insufficient vertical signal) or one log line per factor evaluated.
6. **Calibration complete** - confirmation that results have been written to `national_smoothing_factors.json` and `calibration_audit.csv`.

Multiple states will interleave in the log when running in parallel. The state FIPS code at the start of each log line identifies which state produced it.

**Signs of a healthy run:**

* H sweeps triggering early exit after 6–11 factors rather than always running all 15
* V sweeps logging `flat_terrain_default` for flat states and completing in under 2 seconds
* Consistent PASS results at low factors transitioning to FAIL at higher factors with rising RMSE
* Occasional 502/504 USGS warnings followed by successful retries

**Signs that warrant attention:**

* H sweeps with `[NO DATA]` on every factor, indicates the in-memory tile cache failed to load for those chunks; check the DEM directory path and USGS API availability
* `composite_fallback` or `absolute_fallback` selection methods appearing frequently, may indicate Gate 1 thresholds are too tight; run the diagnostics tool after the run completes
* A state worker throwing a critical exception, the state will be skipped; re-run that state individually to investigate

**Estimated runtime.** A full national run with 30 cores, 5 state workers, and 5 sweep workers typically completes in 20–30 hours depending on Socrata fetch times, USGS API response times, and the number of functional systems with real geometry in each state. Single-state runs for mid-sized states typically complete in 2–4 hours.

### 14.4 Running the Diagnostics Tool

After a calibration run completes, run the diagnostics tool against the new `calibration_audit.csv` before accepting the results for production use.

py tools/rat_calibration_diagnostics.py
--audit   "core/calibration_audit.csv"
--outdir  "path/to/diagnostics/output"

The tool requires `matplotlib` for chart generation. If `matplotlib` is not installed, all diagnostic reports and CSV outputs are still produced; only the PNG charts are skipped.

**Outputs written to `--outdir`:**

| File | Contents |
| :--- | :--- |
| `diagnostics_summary.txt` | Plain-text report with per-functional-system threshold assessments and recommendations |
| `selection_method_breakdown.csv` | Fallback rates by functional system and mode |
| `threshold_proximity.csv` | Ceiling proximity statistics for elbow selections |
| `confidence_scores.csv` | Confidence score distribution for elbow selections |
| `factor_stability.csv` | Factor variance across states with outlier state list |
| `plots/fallback_rate_by_fs.png` | Bar chart of H and V fallback rates with warning and alert reference lines |
| `plots/confidence_scores_by_fs.png` | Box plots of confidence scores by functional system |
| `plots/selected_factors_by_fs.png` | Box plots of selected factors by functional system |
| `plots/ceiling_proximity_heatmap_V.png` | State × functional system heatmap of V ceiling proximity |

### 14.5 Interpreting Diagnostic Output

The `diagnostics_summary.txt` file is the primary deliverable. It contains five sections:

**Section 1 - Selection Method Breakdown.** Reports the fraction of selections that used a fallback method (anything other than `elbow`, `flat_curve`, or `flat_terrain_default`) for each functional class and mode. A fallback rate above 20% triggers a warning; above 35% triggers an alert indicating the Gate 1 thresholds are likely too tight for that functional system.

**Section 2 - Ceiling Proximity.** For elbow selections only, reports how close the selected factor's RMSE was to the Gate 1 ceiling. If more than 50% of elbow selections are within 15% of the ceiling, the thresholds may be too loose; the passing range is so narrow that the elbow is being forced into a corner rather than found at a natural bend in the RMSE curve.

**Section 3 - Confidence Scores.** Reports the distribution of confidence scores for elbow selections. A high rate of weak-confidence elbow selections (below 40) can indicate that thresholds are too loose, allowing nearly all factors to pass and producing a flat RMSE curve with no real elbow.

**Section 4 - Factor Stability.** Reports the coefficient of variation (standard deviation divided by mean) of selected factors across states for each functional system and mode. High variance (above 50%) suggests the calibration is responding to sampling noise rather than genuine regional geometry differences, which often traces back to thresholds that make the passing list highly sensitive to small metric changes.

**Section 5 - Per-Functional-System Assessment.** Combines signals from Sections 1–4 into a plain-English verdict for each functional class: thresholds appear too tight, too loose, conflicting signals, or approximately correct. Each verdict includes a specific recommendation identifying which threshold constant to adjust and by how much.

The threshold values in effect during the run are printed at the bottom of the summary for reference.

### 14.6 The Iterative Calibration Process

A single calibration run rarely produces fully optimized thresholds, particularly the first time the script is run against a new HPMS vintage or after significant code changes. The recommended process is:

1. **Run the calibration** for all states or a representative subset (one flat state, one mountainous state, one mixed).
2. **Run the diagnostics tool** against the completed audit CSV.
3. **Review `diagnostics_summary.txt`** for threshold alerts. Focus on functional systems with fallback rates above 35% (thresholds too tight) or high rates of weak-confidence elbow selections (thresholds potentially too loose).
4. **Adjust thresholds** in `rat_national_calibration_cli.py`. The primary levers are `MAX_H_RMSE_FT` (per-FS dict) and `MAX_H_DEV_FT` for horizontal calibration, and `V_RMSE_THRESHOLDS` and `MAX_V_DEV_FT` for vertical calibration. Adjust one functional system at a time and re-run on a representative subset before committing to a full national rerun.
5. **Re-run the full national calibration** once threshold adjustments produce fallback rates below 20% for all functional systems on the representative subset.
6. **Archive the audit CSV** from each run for longitudinal comparison. Consistent factor selections across successive runs indicate stable, reliable calibration.

Keep the threshold constants in `rat_calibration_diagnostics.py` synchronized with those in `rat_national_calibration_cli.py`. The diagnostics tool uses its own local copy of the thresholds to compute ceiling proximity and generate the reference table in `diagnostics_summary.txt`.

### 14.7 Updating Defaults

The `NATIONAL_DEFAULTS` dict in `rat_national_calibration_cli.py` and the `DEFAULTS` block in `rat_core.py` should be updated after each completed national run to reflect the empirical medians from the new audit. These defaults are used in three places:

* As the fallback factor when `flat_terrain_default` is triggered during calibration
* As the starting point for the `deviation_from_default` audit field
* By `build_params()` in `rat_core.py` when no state-specific JSON entry exists for a state or functional system

To derive updated defaults from a completed audit:

1. Open `calibration_audit.csv` and filter to rows where `selection_method` is `elbow` or `flat_curve`; these are the reliable calibration results.
2. For each functional system and mode, compute the median selected factor across all states.
3. For V factors, compute the median separately for mountain states and flat states. Use the mountain state median as the default for FS1, since flat states produce `flat_terrain_default` and their V factor selections carry less information about the true optimal value.
4. Update `NATIONAL_DEFAULTS` in `rat_national_calibration_cli.py` and the corresponding values in `rat_core.py` DEFAULTS block.
5. Update the defaults table in Section 2.2 of this manual to reflect the new values.

## 15. Interpreting the Calibration Audit

The `calibration_audit.csv` file produced by the calibration engine documents the selection process for every state, functional system, and mode combination. This section explains how to read the audit, identify results that warrant manual review, and make informed override decisions.

### 15.1 Where to Start

Open `calibration_audit.csv` in Excel or a similar tool and apply the following sort and filter sequence:

1. Filter `override_recommended` to `True`. This surfaces all results where the algorithm flagged low confidence or used a fallback selection method.
2. Within that filtered set, sort by `confidence_score` ascending. The lowest scores represent the weakest calibration results and should be reviewed first.
3. Note the `selection_method` column for each flagged row. The meaning of each method is described in Section 15.2.

For results where `override_recommended` is `False`, spot-check any row where `deviation_from_default` is greater than 2.0 or less than 0.5. These are outliers relative to the national median and may reflect genuine regional geometry or may indicate a sampling artifact.

### 15.2 Understanding Selection Methods

The `selection_method` column records how the factor was chosen. Each method carries different implications for result reliability.

**`elbow`:** The algorithm evaluated three or more passing factors and identified a clear point of diminishing returns. This is the intended outcome and generally produces the most reliable results. Confidence scores above 60 with this method indicate a well-defined elbow and a trustworthy selection.

**`flat_curve`:** Three or more factors passed Gate 1, but the RMSE rise across them was less than the flat threshold (0.15 ft). This means the smoothing factor has negligible effect on alignment quality for this road class in this state. The lowest passing factor is returned as a conservative default. `confidence_score` will be capped at 50. This is not a failure, it indicates that the geometry is either unusually uniform or that the factor range tested does not produce meaningful differentiation for this combination.

**`flat_terrain_default`:** The V RMSE range across the full sweep was below 0.05 ft, indicating that the terrain lacks sufficient vertical signal to produce a meaningful elbow selection. The national default factor for that functional system was applied rather than a calibrated value. This is not a failure, it correctly reflects that any factor in the sweep range would produce essentially the same output for this state and functional system. See Section 13.10 for a full discussion. `flat_terrain_default` results do not trigger `override_recommended = True` and do not require manual review.

**`highest_safe`:** Fewer than three factors passed Gate 1, so the elbow algorithm had insufficient data to run. The highest passing factor is returned. When `rmse_rise` is 0.0, this typically means both passing factors (usually 100 and 200) produced identical RMSE, confirming insensitivity rather than a data problem. When `rmse_rise` is nonzero with only two passing factors, the result is geometrically safe but the elbow location is uncertain; the true optimum may lie between the two passing values or just beyond the last one. Confidence is capped at 50. A high rate of `highest_safe` results across a functional system is a signal that the Gate 1 H RMSE ceiling may be too tight for that road class; see Section 14.5.

**`composite_fallback`:** No factor passed Gate 1. The algorithm returned the factor with the lowest composite penalty score. This result means the HPMS geometry for this state and functional system consistently exceeded the deviation ceilings at every smoothing level tested. Common causes include small sample sizes, bridge-heavy urban networks, or extreme terrain. `confidence_score` is capped at 25. These results should be reviewed and manually overridden where possible.

**`absolute_fallback`:** No valid metrics were computed at all. The returned factor of 100 is a placeholder, not a calibrated value. `confidence_score` is capped at 5. This should be treated as a missing result rather than a calibration outcome.

### 15.3 Reading the Confidence Score

The confidence score (0–100) is a composite measure derived from three components:

* **Elbow sharpness (up to 50 points):** The `peak_elbow_distance` value normalized against a reference of 0.50. A peak distance above 0.35 indicates a well-defined elbow; below 0.10 indicates a weak or ambiguous inflection.

* **Sample richness (up to 30 points):** The number of passing factors normalized against 10. Results with 10 or more passing factors receive full points; results with 2–3 passing factors receive proportionally fewer.

* **RMSE curve meaningfulness (up to 20 points):** The `rmse_rise` value normalized against 1.0 ft. A rise below 0.15 ft indicates the smoothing factor has little effect on the RMSE curve.

A score of 70 or above indicates a reliable, well-supported result. Scores between 40 and 70 are usable but should be validated against known geometry in the state before full production use. Scores below 40 trigger `override_recommended = True` and should be reviewed before relying on the result.

### 15.4 Identifying Outliers with Deviation from Default

The `deviation_from_default` column expresses the selected factor as a ratio of the national default for that functional system and mode. A value of 1.0 means the state matches the national default exactly. Values above 2.0 or below 0.5 are outliers worth examining.

To assess whether the calibration engine is capturing genuine regional geometric trends rather than statistical noise, the diagnostics tool evaluates factor stability across states for each functional class. It calculates the Coefficient of Variation ($CV$) as a percentage:

$$CV = \left( \frac{\sigma}{\mu} \right) \times 100$$

Where $\sigma$ is the standard deviation and $\mu$ is the mean of the selected factors. A $CV \geq 50\%$ triggers an alert that the calibration may be noise-driven. Furthermore, individual states are flagged for manual review as statistical outliers if their selected factor ($f$) deviates from the mean by more than two standard deviations:

$$|f - \mu| > 2\sigma$$

**High deviation (> 2.0)** may indicate:

* Genuinely atypical geometry for the state and road class (e.g., Great Plains states FS1 H on unusually straight Interstate corridors, or mountain state FS3 V on steep terrain)
* A small or unrepresentative Cochran sample that happened to draw from an atypical corridor
* A sampling artifact where the 1-mile chunk population was dominated by a single geometric type

**Low deviation (< 0.5)** may indicate:

* A composite or absolute fallback result that selected the minimum factor
* Geometry that is more variable or complex than the national median for that class

When deviation is high and confidence is also high, the result is more likely to reflect genuine regional geometry. When deviation is high and confidence is low, treat the result with caution.

### 15.5 Using the RMSE Columns for Validation

The metrics at the selected factor (`v_rmse_at_selected`, `h_rmse_at_selected`, `maxv_at_selected`, `maxh_at_selected`) provide a direct measure of how much the smoothed alignment deviates from the raw geometry at the chosen factor.

Compare these against the ceiling values (`v_rmse_ceiling`, `h_rmse_ceiling`, `maxv_ceiling`, `maxh_ceiling`) using the `ceiling_proximity_pct` column. A proximity above 90% means the selected factor's RMSE is very close to the acceptance limit, leaving little margin. Results in this range are geometrically valid but represent the boundary of what the calibration considers acceptable; if the route geometry in production is slightly more complex than the sampled chunks, the actual RMSE may exceed the ceiling.

The `std_v_rmse_at_selected` and `std_h_rmse_at_selected` columns measure variability across the sampled chunks. A standard deviation greater than 50% of the mean RMSE indicates a heterogeneous sample; some chunks produced much higher deviation than others. This is common in states with mixed terrain or where the Cochran sample drew from both urban and rural corridors. High standard deviation does not invalidate the result but suggests that the mean RMSE may not be representative of all geometry in the state.

### 15.6 Making Manual Overrides

When a result warrants manual correction, edit the relevant entry directly in `national_smoothing_factors.json` in the `core/` directory. The JSON is read at run time by `build_params()`; no code changes are required.

Common override scenarios and recommended approaches:

**Small-sample urban states (e.g., DC FS1):** Interstate geometry in small urban jurisdictions is often dominated by elevated structures, tunnels, and interchange ramps. The calibration sample is too small and geometrically atypical to produce reliable results. Override to the national default for the functional system.

**`highest_safe` results with `rmse_rise = 0.0`:** These indicate factor insensitivity, not a problem. H=200 returned by this method on FS4–7 is appropriate and does not require override. Document as expected behavior.

**`flat_terrain_default` results:** These do not require override. The national default has been applied correctly. If the national default itself appears inappropriate for a specific state, for example, a state that straddles flat and mountainous terrain, a manual override to a state-specific value is acceptable but should be validated against known geometry before applying.

**High-deviation elbow results with strong confidence:** These are likely genuine regional characteristics. Validate on a known benchmark corridor before accepting or overriding. If the plan and profile output looks correct for that state and functional system, accept the calibrated value.

**`composite_fallback` results in mountain states:** These typically reflect extreme terrain variance exceeding the deviation ceilings. Consider whether the `MAX_V_DEV_FT` ceiling for the relevant functional system should be relaxed for that state in `rat_national_calibration_cli.py`, or accept the fallback and note it in the production documentation.

### 15.7 Cross-State Consistency Check

After completing a calibration run, a useful validation step is to compare results for neighboring states with similar terrain. States within the same physiographic region should produce broadly consistent smoothing factors for the same functional system. Large unexplained discontinuities between neighboring states, particularly for V factors on FS1, are candidates for further review.

The following regional groupings are a useful starting point for consistency checks:

* **Great Plains** (Kansas, Nebraska, Iowa, South Dakota, North Dakota) - expect higher H and V factors on FS1 due to straight, rolling corridor geometry
* **Mountain West** (Colorado, Wyoming, Montana, Idaho, Utah) - expect moderate H and higher V factors; mountain state relaxation on MaxV is active for all
* **Southeast** (Alabama, Mississippi, Georgia, Tennessee) - expect moderate H and V across all functional systems
* **Mid-Atlantic Urban** (DC, Maryland, Delaware, New Jersey) - expect small sample sizes, frequent `highest_safe` results, and lower confidence scores on higher functional systems due to dense interchange geometry

---

## Appendix A. Parameter Reference

*Adjust one parameter group at a time. Validate against benchmark routes before a full statewide rerun.*

### Table 1. Core Spacing and Smoothing

| Parameter | Default | Units | Effect |
| :--- | :---: | :---: | :--- |
| `DENSIFY_SPACING_FT` | 10 | ft | Vertex interpolation interval before analytics. Decrease for higher geometric resolution; increase to reduce processing time on dense geometry. |
| `H_SMOOTH_FACTOR` | 400 | ft | Horizontal spline stiffness for FS 1. Higher values produce a straighter smoothed line; lower values allow more lateral flexibility. State-specific values from `national_smoothing_factors.json` override this default at run time. |
| `V_SMOOTH_FACTOR` | 1400 | ft | Vertical spline stiffness for FS 1. State-specific values override this default at run time. |
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

Version 3.3 provides a nationally calibrated set of smoothing factors in `national_smoothing_factors.json`. Each entry is keyed by two-digit State FIPS code and contains FS-specific horizontal and vertical smoothing factors derived by the RAT Calibration Engine.

**Calibration process**

Factors are derived using the following process:

1. HPMS data for each state are retrieved via the Socrata API using a `$select` projection that fetches only the columns needed for geometry processing, reducing payload size and fetch time.
2. Routes are segmented into complete 1-mile chunks per functional system, plus any tail segment >= 0.5 miles (800 meters). This prevents sampling bias that would result from excluding shorter route segments.
3. A statistically representative sample is drawn from each functional system's complete chunk population using Cochran's formula (95% confidence, 5% margin of error).
4. 1-meter USGS DEM tiles are downloaded in parallel for the sampled geometry, with completed tiles stored to a shared persistent cache directory, with each worker using an isolated temporary subdirectory only during the download itself to prevent partial-file collisions.
5. For the H sweep, tile data is pre-loaded into memory once per chunk before the sweep begins, eliminating repeated disk reads across the 15 candidate factors.
6. Candidate smoothing factors from the sweep range [100, 200, 400, 600, 800, 1000, 1200, 1400, 1600, 1800, 2000, 2500, 3000, 4000, 4500] are evaluated against Gate 1 ceilings. The sweep exits early once three consecutive factors fail Gate 1 with rising RMSE, confirming the elbow region has been passed.
7. Gate 1 H RMSE ceilings vary by functional system: 3.5 ft for FS1–2, 4.5 ft for FS3, 5.0 ft for FS4–5, and 5.5 ft for FS6–7. A single 3.5 ft ceiling applied to all functional systems produced fallback rates of 47–77% on lower functional systems during the first national run, reflecting the greater geometric complexity of collectors and local roads relative to controlled-access highways.
8. Among factors passing Gate 1, the Kneedle elbow algorithm identifies the factor at the point of diminishing returns.
9. For the V sweep, if the RMSE range across all evaluated factors is below 0.05 ft, the terrain lacks sufficient vertical signal to produce a meaningful elbow. The national default factor is applied and `flat_terrain_default` is recorded as the selection method.
10. For mountain states, the maximum vertical deviation ceiling (`MAX_V_DEV_FT`) is relaxed by 8 ft to accommodate bridge approach geometry and engineered grade transitions on steep terrain.

**Tail Segment Inclusion (v3.3 and later)**
 
The population N in Cochran's formula includes:
- All complete 1-mile chunks for each route
- Any remaining tail segment >= 0.5 miles (800 meters)
 
This ensures the calibration population is not biased toward longer routes. Route segments shorter than 0.5 miles are excluded to maintain a minimum geometric resolution and avoid overfitting on very short fragments. Testing on Delaware (small state, ~800 route miles) found that tail segment inclusion increased the represented network geometry by approximately 3–7% per functional system, improving the representativeness of the sampled chunks.

**Mountain states**

The mountain state designation applies to the following states, which receive the 8 ft vertical deviation bonus:

*Western mountain states:* Alaska, Arizona, California, Colorado, Idaho, Montana, Nevada, New Mexico, Oregon, Utah, Washington, Wyoming

*Appalachian and eastern mountain states:* Georgia, Kentucky, Maine, New Hampshire, New York, North Carolina, Pennsylvania, Tennessee, Vermont, Virginia, West Virginia

**Threshold constants**

The Gate 1 ceilings, early exit threshold, and flat terrain detection threshold are defined as named constants at the top of `rat_national_calibration_cli.py` and can be adjusted independently before a rerun:

| Constant | Default | Description |
| :--- | :---: | :--- |
| `MAX_H_RMSE_FT` | `{1:3.5, 2:3.5, 3:3.8, 4:3.8, 5:5.0, 6:5.5, 7:5.5}` | Per-FS H RMSE ceiling in feet |
| `MAX_H_DEV_FT` | `{1:15, 2:15, 3:22, 4:22, 5:15, 6:15, 7:20}` | Per-FS maximum H deviation in feet |
| `V_RMSE_THRESHOLDS` | `{1:4.0, 2:4.5, 3:3.5, 4:3.5, 5:4.0, 6:4.0, 7:4.0}` | Per-FS V RMSE ceiling in feet |
| `MAX_V_DEV_FT` | `{1:35, 2:30, 3:15, 4:12, 5:15, 6:15, 7:15}` | Per-FS maximum V deviation in feet |
| `EARLY_EXIT_CONSECUTIVE_FAILS` | `3` | Consecutive Gate 1 failures required to trigger early exit |
| `V_MIN_RMSE_RANGE_FT` | `0.05` | Minimum V RMSE range below which flat_terrain_default is applied |

**Manual overrides**

For states or functional systems where the calibrated result may be unreliable (indicated by `override_recommended = True` in `calibration_audit.csv`), manual review and override of the JSON entry is recommended before statewide production processing. Manual overrides are applied directly to the relevant state entry in `national_smoothing_factors.json`. The JSON is read at run time by `build_params()`; no code changes are required. See Section 15.6 for common override scenarios and recommended approaches.

**Iterative refinement**

The Gate 1 thresholds shown above reflect values derived through five iterative national calibration runs and diagnostic reviews. They should be treated as calibrated starting points rather than fixed values. After each national run, run `rat_calibration_diagnostics.py` against the completed audit CSV and review the per-functional-system threshold assessments in `diagnostics_summary.txt`. See Section 14.6 for the full iterative calibration workflow.

<img src="images/ceiling_proximity_heatmap_V_run5.png" width="40%" alt="Calibration Ceiling Proximity Heatmap">

*Figure A.1. V mode ceiling proximity by state and functional class, fifth national calibration run.*

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

**Playbook 1 - Too many statewide horizontal curves**
1. Increase `H_SMOOTH_FACTOR`
2. Increase `H_MIN_DELTA`
3. Increase `H_MIN_CURVE_LENGTH_FT`
4. Revalidate on sample corridors before full rerun

**Playbook 2 - Curves missing on lower-speed networks**
1. Decrease `DENSIFY_SPACING_FT`
2. Decrease `H_SMOOTH_FACTOR` or apply a lower FS-specific override
3. Decrease `H_MIN_CURVE_LENGTH_FT`
4. Validate against known geometry

**Playbook 3 - Bridge dips remaining in vertical outputs**
1. Increase `TREND_WINDOW_FT`
2. Decrease `DIP_THRESHOLD_FT`
3. Review `BRIDGE_MAX_LEN_FT`
4. Re-run targeted bridge corridors and inspect profiles

**Playbook 4 - State-specific calibration result needs review**
1. Open `calibration_audit.csv` and filter `override_recommended = True`
2. Review `selection_method`, `confidence_score`, `n_passing`, and `deviation_from_default` for flagged entries
3. Compare `v_rmse_at_selected` and `h_rmse_at_selected` against neighboring states with similar terrain
4. Edit the relevant entry in `national_smoothing_factors.json` with the corrected values
5. Re-run alignment on benchmark routes for the affected state to confirm

**Playbook 5 - Ramps or unexpected facility types in output**
1. Check `FACILITY_TYPE_FILTER` in the GUI, confirm it contains only `[1, 2]` for mainline-only processing
2. If running via CLI directly, verify `run_params.json` has `"FACILITY_TYPE_FILTER": [1, 2]`
3. Re-run the affected state; the filter is applied at the Socrata query level so no post-processing is needed
4. If local file input is being used, apply a pre-filter on the `Facility_Type` column before passing to the suite

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
| **WKT_ZM** | Well-known text geometry string with Z (elevation) and M (m_value) ordinates: `LINESTRING ZM (lon lat elev m_value, ...)`. |

---

## Appendix D. State-Specific Smoothing Factors

This appendix provides a human-readable reference for the calibrated smoothing factors in `national_smoothing_factors.json`. The authoritative values for production processing
are always those in the JSON file; if a discrepancy exists between this table and the JSON, the JSON takes precedence.

**How to read this appendix**

* Mountain states are marked with an asterisk (*).
* The **FS1 table** shows all states since FS1 H calibration is the most reliable and most referenced result from each national run.
* The **FS2–7 tables** show only states where the selected H factor differs from the national default (200 ft). States not listed are at the national default for that
  functional system.
* V factors for FS2–7 are at the national default for all states unless noted. The national default is 1,400 ft for FS2–4 and 1,000 ft for FS5–7, reflecting empirical V elbow selections from mountainous states across five national calibration runs. FS5–7 remain at 1,000 ft as no reliable V elbow data has been produced for those functional classes.
* A dagger (†) marks any entry that has been manually overridden from the calibrated value.

Note on FS1 V factors for small urban states. FS1 V values for DC, Delaware, New Jersey, and Rhode Island should be treated with caution. These jurisdictions have small Interstate networks dominated by elevated structures, tunnels, and interchange geometry that produces atypical vertical profiles. Sample sizes are small and the calibrated V factors may not be representative of the broader Interstate alignment in those states. Where `override_recommended = True` appears in `calibration_audit.csv` for these entries, the national default of 1,400 ft is a reasonable substitute.

**Most recent national calibration run:** Run 5 (June 2026). 

---

### FS 1 - Interstate (All States)

| State | H | V |
| :--- | :---: | :---: |
| Alabama (01) | 400 | 400 |
| Alaska* (02) | 200 | 1,400 |
| Arizona* (04) | 400 | 1,400 |
| Arkansas (05) | 400 | 1,600 |
| California* (06) | 400 | 1,800 |
| Colorado* (08) | 400 | 1,400 |
| Connecticut (09) | 200 | 600 |
| Delaware (10) | 200 | 4,500 |
| District of Columbia (11) | 200 | 1,600 |
| Florida (12) | 400 | 400 |
| Georgia* (13) | 400 | 1,600 |
| Hawaii (15) | 200 | 1,800 |
| Idaho* (16) | 200 | 1,400 |
| Illinois (17) | 400 | 2,000 |
| Indiana (18) | 400 | 1,600 |
| Iowa (19) | 400 | 1,800 |
| Kansas (20) | 600 | 1,400 |
| Kentucky* (21) | 400 | 600 |
| Louisiana (22) | 400 | 600 |
| Maine* (23) | 400 | 1,400 |
| Maryland (24) | 200 | 1,800 |
| Massachusetts (25) | 200 | 2,000 |
| Michigan (26) | 400 | 1,200 |
| Minnesota (27) | 400 | 1,600 |
| Mississippi (28) | 400 | 1,400 |
| Missouri (29) | 400 | 1,400 |
| Montana* (30) | 400 | 400 |
| Nebraska (31) | 1,200 | 1,600 |
| Nevada* (32) | 400 | 1,200 |
| New Hampshire* (33) | 200 | 400 |
| New Jersey (34) | 200 | 4,000 |
| New Mexico* (35) | 400 | 1,400 |
| New York* (36) | 200 | 800 |
| North Carolina* (37) | 200 | 400 |
| North Dakota (38) | 400 | 1,400 |
| Ohio (39) | 400 | 600 |
| Oklahoma (40) | 1,200 | 1,800 |
| Oregon* (41) | 400 | 2,000 |
| Pennsylvania* (42) | 200 | 1,400 |
| Rhode Island (44) | 200 | 400 |
| South Carolina (45) | 400 | 1,400 |
| South Dakota (46) | 800 | 3,000 |
| Tennessee* (47) | 400 | 3,000 |
| Texas (48) | 400 | 1,800 |
| Utah* (49) | 400 | 1,600 |
| Vermont* (50) | 200 | 2,500 |
| Virginia* (51) | 400 | 400 |
| Washington* (53) | 400 | 1,400 |
| West Virginia* (54) | 200 | 3,000 |
| Wisconsin (55) | 400 | 4,000 |
| Wyoming* (56) | 400 | 1,400 |
| Puerto Rico (72) | 200 | 3,000 |

---

### FS 2–7 - States with Non-Default H Factors

States not listed are at the national H default for that functional system (200 ft for FS2–7). V factors for FS2–7 are at the national default (1,000 ft) for all states unless noted.

#### FS 2 - Other Freeways and Expressways (default H = 200)

| State | H |
| :--- | :---: |
| Arizona* (04) | 400 |
| Arkansas (05) | 400 |
| Florida (12) | 400 |
| Indiana (18) | 400 |
| Kansas (20) | 400 |
| Kentucky* (21) | 400 |
| Louisiana (22) | 400 |
| Michigan (26) | 400 |
| Mississippi (28) | 400 |
| Missouri (29) | 400 |
| Nebraska (31) | 400 |
| New Mexico* (35) | 1,800 |
| Oklahoma (40) | 400 |
| Oregon* (41) | 400 |
| South Dakota (46) | 400 |
| Washington* (53) | 400 |
| Wisconsin (55) | 400 |
| Wyoming* (56) | 100 |

#### FS 3 - Other Principal Arterial (default H = 200)

| State | H |
| :--- | :---: |
| District of Columbia (11) | 100 |
| Kansas (20) | 400 |
| Nevada* (32) | 400 |
| North Dakota (38) | 400 |

#### FS 4 - Minor Arterial (default H = 200)

| State | H |
| :--- | :---: |
| Nebraska (31) | 400 |
| New Jersey (34) | 100 |
| North Dakota (38) | 400 |
| South Dakota (46) | 400 |

#### FS 5 - Major Collector (default H = 200)

| State | H |
| :--- | :---: |
| Iowa (19) | 400 |
| Kansas (20) | 400 |
| Montana* (30) | 400 |
| Nevada* (32) | 400 |
| North Dakota (38) | 400 |
| South Dakota (46) | 400 |
| Texas (48) | 400 |

#### FS 6 - Minor Collector (default H = 200)

| State | H |
| :--- | :---: |
| Iowa (19) | 400 |
| Kansas (20) | 400 |
| South Dakota (46) | 400 |
| Wisconsin (55) | 100 |

#### FS 7 - Local (default H = 200)

| State | H |
| :--- | :---: |
| District of Columbia (11) | 100 |
| Iowa (19) | 400 |
| Kansas (20) | 1,400 |
| Nebraska (31) | 400 |
| North Dakota (38) | 800 |
| Oklahoma (40) | 400 |
| South Dakota (46) | 1,400 |
| Wisconsin (55) | 800 |
