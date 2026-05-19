# Roadway Alignment Tool (RAT) Suite v3.2

## Overview
The Roadway Alignment Tool (RAT) suite is a collection of Python-based geospatial applications developed by the Federal Highway Administration (FHWA). The suite ingests 2D Highway Performance Monitoring System (HPMS) linework, drapes it over USGS 3DEP Digital Elevation Models (DEMs), and uses Univariate Spline mathematics to detect, analyze, and visualize horizontal and vertical roadway curves.

Version 3.2 introduces a nationally calibrated smoothing parameter system derived from a full 50-state HPMS data sweep, replacing the manually estimated defaults used in prior versions. The suite continues to use a modular, decoupled architecture in which the Unified GUI orchestrates isolated CLI scripts to prevent memory issues during statewide batch processing.

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

## Suite Components
The suite has been refactored into distinct, specialized modules:
1. **Unified GUI (`rat_unified_gui.py`):** The central dashboard. Collects user inputs and safely dispatches tasks to the CLI processors.
2. **Core Engine (`rat_core.py`):** The math and geospatial backend. Handles UTM projections, USGS DEM fetching, spline smoothing, and KDTree spatial indexing.
3. **Bulk Analyzer (`rat_alignment_cli.py`):** Batch processes statewide datasets to extract geometric curves, outputting tabular data, Folium maps, and dashboards.
4. **Plan & Profile Generators (`rat_plan_profile_cli.py` & `rat_plan_profile_report_pdf.py`):** Isolates specific routes to generate continuous engineering math, then renders professional, multi-page PDF engineering schematics with aerial basemaps.
5. **HPMS 4D Enricher (`hpms_4d_enricher_cli.py`):** Transforms flat 2D HPMS linework into continuous 3D/4D linestrings (XYZM) by snapping tabular rows to a macro-profile, accurately bridging rivers and gaps.
6. **National Calibration Engine (`rat_national_calibration_cli.py`):** Derives optimal horizontal and vertical smoothing factors for each state and functional system through an automated parameter sweep against HPMS geometry and USGS elevation data. Outputs `national_smoothing_factors.json` and `calibration_audit.csv`.
7. **Validator (`rat_results_validator.py`):** Automated QA/QC module. Checks alignment and 4D outputs for required fields, invalid geometry, and anomalous values before production delivery.

## Installation

The RAT Suite uses modern Python packaging via `pyproject.toml`. To install the suite and its dependencies, follow these steps:

1. **Clone the repository:**
   ```bash
   git clone <repository_url>
   cd <repository_directory>
   ```

2. **Create a virtual environment (Recommended):**
   ```bash
   python -m venv venv
   ```
   * **On macOS and Linux:**
     ```bash
     source venv/bin/activate
     ```
   * **On Windows:**
     ```bash
     venv\Scripts\activate
     ```

3. **Install the suite:**
   ```bash
   pip install .
   ```

**Python version:** Python 3.10 or later is required.

## Quick Start Guide
Ready to run your first alignment analysis? Follow these steps to process a local HPMS dataset.

### 1. Launch the Suite
You can launch the new centralized dashboard in one of two ways:
* **The Easy Way:** Double-click the `Run_RAT_Suite.bat` file in the root directory.
* **Via Terminal:** Open your command prompt, navigate to the suite folder, and execute:
  ```bash
  python apps/rat_unified_gui.py
  ```

### 2. Configure Your Input Data
Once the Unified GUI opens, set up your data source:
* **Input File:** The suite defaults to the 2024 HPMS data on `https://data.transportation.gov/` at: `https://datahub.transportation.gov/resource/42um-tgh5.json`. To run using a different file, click `Browse` and select your local HPMS data. The suite accepts standard `.csv`, `.shp`, or `.geojson` formats. *(Note: The file must contain a `RouteId` column and valid geometry/WKT).*

### 3. Set Your Directories
The tool needs to know where to save your reports and where to cache the heavy elevation data.
* **Output Directory:** Click `Browse` and select a folder where you want your maps, CSVs, and shapefiles saved.
* **DEM Cache Directory:** Click `Browse` and select a dedicated folder for USGS elevation tiles. *(Tip: Keep this folder safe so you don't have to re-download heavy DEM tiles for future runs in the same state!)*

### 4. Select Your Modules
Check the boxes for the tools you want to run:
* **Bulk Alignment:** Extracts statewide curve tables and HTML maps.
* **Plan & Profile:** Generates PDFs for specific routes.
* **4D Enricher:** Builds a Blender-ready 3D/4D GeoPackage.

### 5. Select Your Output
Check the boxes for the output you want:
* **CSV:** Creates two CSV files for the horizontal curves and vertical curves.
* **GeoJSON:** Creates a GeoJSON file for the mode selected.
* **GeoPackage (GPKG):** Creates a GeoPackage for the mode selected.
* **Shapefile:** Creates a standard shapefile in meters and degrees for Alignment and Plan/Profile runs. For the 4D Enricher, it creates a Universal Transverse Mercator (UTM) based shapefile in meters for easy visualization in 3D applications like Blender.
* **Interactive HTML Map:** Creates an interactive Folium-based map of all horizontal and vertical curves.
* **Summary Dashboard HTML:** Creates an HTML dashboard with system health RMSE charts by functional system, curve density per 100 route-miles, cumulative severity distributions, CREST/SAG breakdown, compound curve percentage, and advanced diagnostic scatter plots.
* **QA Exceptions CSV:** Generates an automated diagnostic report that acts as a safety net, filtering and flagging any mathematically anomalous or physically impossible curve calculations (e.g., negative radii or inverted geometry) that occurred during processing.
* **Plan/Profile PDF:** Generates a multi-page, visual engineering schematic for specific, targeted routes. It plots the mathematically smoothed centerline against the raw input geometry, rendering 1,500-foot sequential segments of the roadway per page with full curve annotations.

### 6. Run the Analysis!
Click the **RUN** button. 
The GUI will lock its inputs and silently spin up the CLI scripts in the background. It will automatically download the required USGS DEMs, stitch the topography together, and generate your requested outputs. Check the log window at the bottom of the GUI to track its progress!

## Documentation
For methodology, mathematical formulas, and parameter reference, see the **Technical Manual** (`docs/RAT_Suite_v3_2_Technical_Manual.md`). The `core/calibration_audit.csv` file documents the confidence scores and selection rationale for each state and functional system in the national smoothing parameter dictionary.

## License and Disclaimer
This software is dedicated to the public domain under the CC0 1.0 Universal Public Domain Dedication.
Please read the DISCLAIMER.md file for more information regarding the use and limitations of this software.

*RAT Suite v3.2 — Federal Highway Administration, Office of Highway Policy Information*
***
