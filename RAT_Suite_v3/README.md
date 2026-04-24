# Roadway Alignment Tool (RAT) Suite v3.0

## Overview
The Roadway Alignment Tool (RAT) suite is a collection of Python-based geospatial applications developed by the Federal Highway Administration (FHWA). The suite ingests 2D Highway Performance Monitoring System (HPMS) linework, drapes it over USGS 3DEP Digital Elevation Models (DEMs), and uses Univariate Spline mathematics to detect, analyze, and visualize horizontal and vertical roadway curves.

This version marks a major architectural upgrade, shifting to a modular, decoupled environment. A single Unified GUI now orchestrates safe, isolated Command Line Interface (CLI) scripts to prevent memory crashes during statewide batch processing.

## Suite Components
The suite has been refactored into distinct, specialized modules:
1. **Unified GUI (`rat_unified_gui.py`):** The central dashboard. Collects user inputs and safely dispatches tasks to the CLI processors.
2. **Core Engine (`rat_core.py`):** The math and geospatial backend. Handles UTM projections, USGS DEM fetching, spline smoothing, and KDTree spatial indexing.
3. **Bulk Analyzer (`rat_alignment_cli.py`):** Batch processes statewide datasets to extract geometric curves, outputting tabular data, Folium maps, and dashboards.
4. **Plan & Profile Generators (`rat_plan_profile_cli.py` & `rat_plan_profile_report_pdf.py`):** Isolates specific routes to generate continuous engineering math, then renders professional, multi-page PDF engineering schematics with aerial basemaps.
5. **HPMS 4D Enricher (`hpms_4d_enricher_cli.py`):** Transforms flat 2D HPMS linework into continuous 3D/4D linestrings (XYZM) by snapping tabular rows to a macro-profile, accurately bridging rivers and gaps.

## Installation
1. Extract the RAT Suite folder to your local machine.
2. Ensure you have Python 3.8+ installed. *(Note: Due to the heavy C++ geospatial libraries required, using **Miniconda** or **Anaconda** is highly recommended on Windows).*
3. Install the required dependencies using pip:
   ```bash
   pip install -r requirements.txt
   ```

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
* **Input File:** Click `Browse` and select your local HPMS data. The suite accepts standard `.csv`, `.shp`, or `.geojson` formats. *(Note: The file must contain a `RouteId` column and valid geometry/WKT).*

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
* **Summary Dashboard HTML:** Creates an HTML-based list of the top twenty sharpest horizontal and vertical curves, along with distribution charts for both.
* **QA Exceptions CSV:** Generates an automated diagnostic report that acts as a safety net, filtering and flagging any mathematically anomalous or physically impossible curve calculations (e.g., negative radii or inverted geometry) that occurred during processing.
* **Plan/Profile PDF:** Generates a multi-page, visual engineering schematic for specific, targeted routes. It plots the mathematically smoothed centerline against the raw input geometry, rendering 1,500-foot sequential segments of the roadway per page with full curve annotations.

### 6. Run the Analysis!
Click the **RUN** button. 
The GUI will lock its inputs and silently spin up the CLI scripts in the background. It will automatically download the required USGS DEMs, stitch the topography together, and generate your requested outputs. Check the log window at the bottom of the GUI to track its progress!

## Documentation
For deep-dive methodology, mathematical formulas, and engine parameters (such as the KDTree snapping logic and Univariate Spline tolerances), please refer to the **Technical Manual** located in the `docs/` folder.

## License and Disclaimer
This software is dedicated to the public domain under the CC0 1.0 Universal Public Domain Dedication.
Please read the DISCLAIMER.md file for more information regarding the use and limitations of this software.

***
