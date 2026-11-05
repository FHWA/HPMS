# Table of Contents
1. Summary
2. Dataset Summary
3. Files in this Package
4. Provenance & Source
5. Spatial Reference / CRS
6. Geometry
7. Attributes / Metadata Fields
8. Data Specifications
9. Data Types & Casting
10. Usage
11. Known Limitations & Caveats
12. Disclaimers
13. Reproducibility / Script Reference
14. Contact
15. Citation
16. License

# Summary
This README describes the 2024_HPMS_All.geojson export produced by an R script that extracts all State Highway Performance Monitoring System (HPMS) records for DataYear = 2024 from an SQL Server database, converts the WKB/EWKB geometry to an sf object, and writes spatial outputs (GeoJSON, KML, KMZ). This README accompanies the GeoJSON output and documents metadata, provenance, usage, and known limitations.

# Dataset summary
- URL: https://datahub.transportation.gov/Roadways-and-Bridges/HPMS-Spatial-All-Sections-2024/42um-tgh5/
- Feature count: 19,573,739 observations (rows)
- File size: 49,034,981,675 bytes
- Generated: 2025-11-10
- Version: 2024_HPMS_All v1
- SHA-256 checksum of GeoJSON: a730042bb766978cde6773b558354c55efd71db1744faafe38894db6d81b2713

# Files in this package
2024_HPMS_All.geojson - primary GeoJSON file containing HPMS features (LINESTRING) and attributes.

# Provenance & source
- Source database: HPMS9 (SQL Server)
- Source table: Gold.FullSpatialJoins (as queried by the R script)
- Query filter: DataYear = 2024 
- Query ordering: ORDER BY StateId, RouteId, BeginPoint
- Geometry source: Shape.STAsBinary() (WKB/EWKB) and Shape.STSrid (SRID) from the database

# Spatial reference / CRS
CRS used when writing GeoJSON:
If SRID was provided and consistent, the export script attempts to use the SRID found in the source data and, if present, preserves an SRID attribute for each feature. If SRID was missing or inconsistent across rows the script defaults to EPSG:4326. Verify the SRID attribute and reproject as needed for projection-sensitive analysis.

# Geometry
- Input geometry: Source HPMS geometry are M-enabled ESRI polylines (e.g., PolylineM / LINESTRINGM).
- Output geometry: LINESTRING (GeoJSON output does not include M values).
- Empty/invalid geometries: Rows with empty or invalid geometries are excluded from the GeoJSON output. These rows are dropped silently.
- WKB handling: EWKB-aware parsing is used (st_as_sfc(..., EWKB = TRUE)); the script handles raw WKB and hex-encoded WKB text.
- Z values: HPMS data does not contain z values.
- Coordinate Reference System (CRS): WGS84 (EPSG:4326)

Note: HPMS source data contains M values (measures) used for linear referencing; those M values are not included in this GeoJSON. For M-enabled state level HPMS geodatabase/feature-class files, see: https://data.transportation.gov/stories/s/3uu4-47sa

# Attributes / Metadata fields
The SQL query casts and returns a comprehensive set of HPMS attributes. The GeoJSON contains the following attributes (names as produced by the script), descriptions are from the HPMS Field Manual (link below):
| Field Name             | Description                                                                                                               |
|------------------------|---------------------------------------------------------------------------------------------------------------------------|
| StateID                | The State Federal Information Processing Standard (FIPS) code.                                                            |
| YEAR_RECORD            | The calendar year for which the data are being reported.                                                                  |
| ROUTE_ID               | The unique identifier for a given roadway (i.e., route).                                                                  |
| BEGIN_POINT            | The point of origin for a given section of road.                                                                          |
| END_POINT              | The terminus point for a given section of road.                                                                           |
| F_SYSTEM               | The FHWA approved Functional Classification System.                                                                       |
| NHS                    | Roadway that is a component of the National Highway System (NHS).                                                         |
| STRAHNET_TYPE          | Roadway section that is a component of the Strategic Highway Network (STRAHNET).                                          |
| NN                     | Roadway section that is a component of the National Truck Network (NN) as defined by 23 CFR 658.                          |
| NHFN                   | Roadway section that is a component of the National Highway Freight Network as defined by the FAST Act (Public Law No. 114-94). |
| URBAN_ID               | The U.S. Census Urban Area Code.                                                                                          |
| FACILITY_TYPE          | The operational characteristic of the roadway.                                                                            |
| STRUCTURE_TYPE         | Roadway section that is a bridge, tunnel or causeway.                                                                     |
| OWNERSHIP              | The entity that has legal ownership of a roadway.                                                                         |
| COUNTY_ID              | The County Federal Information Processing Standard (FIPS) code.                                                           |
| MAINTENANCE_OPERATIONS | The legal entity that maintains and operates a roadway.                                                                   |
| IS_RESTRICTED          | Whether access is restricted.                                                                                             |
| THROUGH_LANES          | The number of lanes designated for through-traffic.                                                                       |
| MANAGED_LANES_TYPE     | The type of managed lane operations (e.g., HOV, HOT, ETL, etc.).                                                          |
| MANAGED_LANES          | Maximum number of lanes in both directions designated for managed lane operations.                                        |
| PEAK_LANES             | The number of lanes in the peak direction of flow during the peak period.                                                 |
| COUNTER_PEAK_LANES     | The number of lanes in the counter-peak direction of flow during the peak period.                                         |
| TOLL_ID                | Unique toll facility identifier, indicates the presence of special tolls (HOT lane(s) or other managed lanes).            |
| LANE_WIDTH             | The measure of existing lane width.                                                                                       |
| MEDIAN_TYPE            | The type of median.                                                                                                       |
| MEDIAN_WIDTH           | The existing median width.                                                                                                |
| SHOULDER_TYPE          | The type of shoulder.                                                                                                     |
| SHOULDER_WIDTH_R       | The existing right shoulder width.                                                                                        |
| SHOULDER_WIDTH_L       | The existing left shoulder width.                                                                                         |
| PEAK_PARKING           | Specific information about the presence of parking during the peak period.                                                |
| DIR_THROUGH_LANES      | The number of lanes designated for through-traffic, for a given direction of travel on a divided highway section.         |
| TURN_LANES_R           | The presence of right turn lanes at a typical intersection.                                                               |
| TURN_LANES_L           | The presence of left turn lanes at a typical intersection.                                                                |
| SIGNAL_TYPE            | The predominant type of signal system on a sample section.                                                                |
| PCT_GREEN_TIME         | The percent of green time allocated for through-traffic at intersections.                                                 |
| NUMBER_SIGNALS         | A count of at-grade intersections where traffic signals are present.                                                      |
| STOP_SIGNS             | A count of at-grade intersections where stop signs are present.                                                           |
| AT_GRADE_OTHER         | A count of at-grade intersections, where full sequence traffic signal or stop sign traffic control devices are not present, in the inventory direction. |
| AADT                   | Annual Average Daily Traffic.                                                                                             |
| AADT_D                 | Optional month and year data was collected.                                                                               |
| AADT_SINGLE_UNIT       | Annual Average Daily Traffic for single-unit trucks and buses.                                                            |
| AADT_COMBINATION       | Annual Average Daily Traffic for Combination Trucks.                                                                      |
| PCT_DH_SINGLE_UNIT     | Peak hour single-unit truck and bus volume as a percentage of total AADT.                                                 |
| PCT_DH_COMBINATION     | Peak hour combination truck volume as a percentage of total AADT.                                                         |
| K_FACTOR               | The design hour volume (30th largest hourly volume for a given calendar year) as a percentage of AADT.                    |
| DIR_FACTOR             | The percent of design hour volume flowing in the higher volume direction.                                                 |
| Future_AADT            | Forecasted AADT.                                                                                                          |
| FUTURE_AADT_YEAR       | Four-digit year for which the Future AADT has been forecasted.                                                            |
| ACCESS_CONTROL         | The degree of access control for a given section of road.                                                                 |
| SPEED_LIMIT            | The posted speed limit.                                                                                                   |
| IRI                    | IRI is the International Roughness Index, used to estimate the amount of roughness in a measured longitudinal profile.    |
| IRI_D                  | Optional month and year data was collected.                                                                               |
| PSR                    | Present Serviceability Rating (PSR) for pavement condition.                                                               |
| PSR_D                  | Optional month and year data was collected.                                                                               |
| SURFACE_TYPE           | Surface type on a given section.                                                                                          |
| RUTTING                | Average depth of rutting; defined as longitudinal surface depressions in asphalt pavement.                                |
| RUTTING_D              | Optional month and year data was collected.                                                                               |
| FAULTING               | Faulting is a vertical misalignment of pavement joints in Portland Cement Concrete Pavements.                             |
| FAULTING_D             | Optional month and year data was collected.                                                                               |
| CRACKING_PERCENT       | Percentage of pavement surface exhibiting cracking.                                                                       |
| CRACKING_PERCENT_D     | Optional month and year data was collected.                                                                               |
| YEAR_LAST_IMPROVEMENT  | The year in which the roadway surface was last improved.                                                                  |
| YEAR_LAST_CONSTRUCTION | The year in which the roadway was constructed or reconstructed.                                                           |
| LAST_OVERLAY_THICKNESS | Thickness of the most recent pavement overlay.                                                                            |
| THICKNESS_RIGID        | Thickness of rigid pavement.                                                                                              |
| THICKNESS_FLEXIBLE     | Thickness of the flexible pavement.                                                                                       |
| BASE_TYPE              | The base pavement type.                                                                                                   |
| BASE_THICKNESS         | The thickness of the base pavement.                                                                                       |
| SOIL_TYPE              | Soil type as defined by AASHTO soil classes.                                                                              |
| WIDENING_POTENTIAL     | The number of through lanes that could be potentially added.                                                              |
| WIDENING_OBSTACLE      | Obstacles that prevent widening of the existing roadway for additional through lanes.                                     |
| CURVES_A               | Total length of curves under 3.5 degrees (0.061 radians).                                                                 |
| CURVES_B               | Total length of curves 3.5 - 5.4 degrees (0.061 - 0.094 radians).                                                         |
| CURVES_C               | Total length of curves 5.5 - 8.4 degrees (0.096 - 0.147 radians).                                                         |
| CURVES_D               | Total length of curves 8.5 - 13.9 degrees (0.148 - 0.243 radians).                                                        |
| CURVES_E               | Total length of curves 14.0 - 27.9 degrees (0.244 - 0.487 radians).                                                       |
| CURVES_F               | Total length of curves 28 degrees (0.489 radians) or more.                                                                |
| TERRAIN_TYPE           | The type of terrain.                                                                                                      |
| GRADES_A               | Total length of grades with a percent grade of: 0.0 - 0.4.                                                                |
| GRADES_B               | Total length of grades with a percent grade of: 0.5 - 2.4.                                                                |
| GRADES_C               | Total length of grades with a percent grade of: 2.5 - 4.4.                                                                |
| GRADES_D               | Total length of grades with a percent grade of: 4.5 - 6.4.                                                                |
| GRADES_E               | Total length of grades with a percent grade of: 6.5 - 8.4.                                                                |
| GRADES_F               | Total length of grades with a percent grade of: 8.5 or greater.                                                           |
| PCT_PASS_SIGHT         | Percent of a Sample Panel section meeting the sight distance requirement for passing.                                     |
| TRAVEL_TIME_CODE       | Travel time code.                                                                                                         |
| ROUTE_QUALIFIER        | Route signing descriptive qualifier.                                                                                      |
| ROUTE_SIGNING          | Type of route signing.                                                                                                    |
| ROUTE_NUMBER           | Signed route number.                                                                                                      |
| SAMPLE_ID              | Unique identifier for all sample sections.                                                                                |
| RouteName              | Familiar, non-numeric designation for a route.                                                                            |
| SectionLength          | The true (measured) length for a given section of road.                                                                   |
| ShapeId                | Unique identifier for all shapes.                                                                                         |
| SRID                   | Spatial reference identifier.                                                                                             |
|------------------------|---------------------------------------------------------------------------------------------------------------------------|

Important: Field names are those produced by the R script. When exporting KML, field names are cleaned and truncated for compatibility; GeoJSON retains the attribute names as in the sf object at write-time.

# Data specifications
The HPMS Field Manual is the data collection guidance for the state data providers and also serves as the HPMS Data Dictionary and can be viewed at: https://www.fhwa.dot.gov/policyinformation/hpms/fieldmanual/. Please note that changes to the Field Manual are captured in an Errata Sheet which can be viewed at: https://www.fhwa.dot.gov/policyinformation/hpms/fieldmanual/page20.cfm.

# Data types & casting
The SQL in the script explicitly casts many fields to integer, decimal, or varchar types (see script for exact casts). Expect numeric columns (integers/doubles) and textual fields. If strict typing is required, inspect a sample of attributes in R or a GIS tool.

# Usage
Mapping and analyzing roadway inventory, attributes, conditions, and usage, of the Federal-Aid system.  The sample data are included and can be expanded and analyzed where full-extent data are not available. It is recommended that the user consult the HPMS Field Manual on how to do this, or reach out to the FHWA, Office of Highway Policy Information for assistance. It is generally, not advised to compare states, especially using the sampled data items since the collection and reporting of these data can vary from state to state.

Load in GIS software (QGIS, ArcGIS) or programmatically (GeoPandas, sf in R).

Typical R example:
- library(sf)
- sf <- st_read("2024_HPMS_All.geojson")

Typical Python (GeoPandas) example:
- import geopandas as gpd
- gdf = gpd.read_file("2024_HPMS_All.geojson")

A typical SQL query usually includes the following:
- Facility_Type in (1, 2)
- Urban_ID > 0
- Is_Restricted is NULL

# Known limitations & caveats
- M values: M values are not calculated in the GeoJSON export.
- Attribute name modifications: the KML export applies name cleaning/truncation. The GeoJSON should retain original names, but verify if your workflow requires exact original column names.
- Geospatial overlaps are known to exist in some state provided data and are not changed or corrected by HPMS. While this is not an extensive problem, it may be present in this file.
- Users wanting to replicate the data in the FHWA publication Highway Statistics, should be aware that some data items are not provided for rural Minor Collectors and all Local roads.  These data will need to be pulled from the appropriate summary table available on the Data Access for HPMS site at: https://data.transportation.gov/stories/s/3uu4-47sa.
- The data may contain physical spatial gaps. These gaps may be errors created at the source (e.g. during the original digitization of the ARNOLD network) or during data processing in the HPMS application. Gaps created during data processing can occur when there is no reported tabular data to support the ARNOLD network at the location of the gap.
- The data may contain overlapping ARNOLD network (geometry) features that contain different Route IDs, which are not changed or corrected by HPMS. These overlapping features are most prevalent on roadways not owned by a state agency and locations with concurrently running highway numbers. There are also locations where states provide overlapping features that represent the inventory and non-inventory direction.
- Facility Type on Interstates in Iowa do not follow the coding guidance stated within the HPMS Field Manual. Interstates in Iowa in the inventory direction provide Facility Type as equal to “1” instead of “2” where the highway operates with traffic moving in both directions during non-peak periods.
- Curve Classification, Grade Classification, counts of intersection types, and any data item describing intersection geometry or operations should be used with caution when analyzing the Full Join. These items describe the entire sample section or the controlling intersection within that sample as indicated by the associated Sample ID. When these data items are intersected and normalized to create the Full Join, their values will appear within a single homogeneous record that is only a piece of the overall Sample Section. However, they represent the entire Sample Section, which can consist of multiple homogeneous records. Treat the Full Join values for the described data items as aggregates that summarize across the full Sample Section rather than as a single, indivisible observation.
- Missing substantial portions of data for the following states: North Dakota and New Jersey.

# Disclaimers
- Unless otherwise stated, all data, metadata and related materials are considered to satisfy the quality standards relative to the purpose for which the data were collected. Although these data and associated metadata have been reviewed for accuracy and completeness and approved for release by the Federal Highway Administration (FHWA), no warranty expressed or implied is made regarding the display or utility of the data for other purposes, nor on all computer systems, nor shall the act of distribution constitute any such warranty.
- This data has been approved for release by the Federal Highway Administration (FHWA). Although this data has been subjected to rigorous review and is substantially complete, the FHWA reserves the right to revise the data pursuant to further analysis and review. Furthermore, the data is released on condition that neither the FHWA nor the U.S. Government shall be held liable for any damages resulting from its authorized or unauthorized use.

# Reproducibility / script reference
The outputs were produced by an R script that:
- Connects via DBI + odbc to SQL Server
- Runs the SQL SELECT query (explicit casts) pulling Shape.STAsBinary() and Shape.STSrid
- Converts WKB to sfc using st_as_sfc(..., EWKB = TRUE) (handling raw and hex encodings)
- Removes empty geometries and casts to LINESTRING
- Writes GeoJSON using st_write(..., driver = "GeoJSON")

# Contact
FHWA, Office of Highway Policy Information. For dataset-specific questions, contact the Highway System Performance Division at: PolicyInfoFeedback@dot.gov.  

# Citation
If you use these HPMS data files in a publication, report, or presentation, please cite them as follows.

## Recommended citation
- U.S. Department of Transportation, Federal Highway Administration, Office of Highway Policy Information: 2024 Highway Performance Monitoring System (HPMS) Interstate Sections (Washington, DC: 2025). Data set: 2024_HPMS_All.geojson (Version 2024_HPMS_All v1). Generated 2025-11-10. Public domain. SHA-256: a730042bb766978cde6773b558354c55efd71db1744faafe38894db6d81b2713. Contact: PolicyInfoFeedback@dot.gov

## Formal citations
- APA 

	U.S. Federal Highway Administration, Office of Highway Policy Information. (2025). 2024_HPMS_All.geojson (Version 2024_HPMS_All v1) [Data set]. Generated 2025-11-10. Public domain. SHA-256: a730042bb766978cde6773b558354c55efd71db1744faafe38894db6d81b2713. Contact: PolicyInfoFeedback@dot.gov

- MLA 

	U.S. Federal Highway Administration, Office of Highway Policy Information. 2024_HPMS_All.geojson. Version 2024_HPMS_All v1, U.S. Federal Highway Administration, 10 Nov. 2025. Public domain. SHA-256: a730042bb766978cde6773b558354c55efd71db1744faafe38894db6d81b2713. Accessed [DATE]. PolicyInfoFeedback@dot.gov .

- Chicago (Author-Date) 

	U.S. Federal Highway Administration, Office of Highway Policy Information. 2025. "2024_HPMS_All.geojson." Version 2024_HPMS_All v1. U.S. Federal Highway Administration. Generated November 10, 2025. Public domain. SHA-256: a730042bb766978cde6773b558354c55efd71db1744faafe38894db6d81b2713. (Accessed [Month Day, Year]).

# License

Public Domain U.S. Government (http://www.usa.gov/publicdomain/label/1.0/). All data contained in the described file are in the public domain and may be used without special permission; citation as to source is required.










