# RAT Suite Future Roadmap

This document outlines planned features, algorithmic enhancements, and structural improvements under consideration for future releases (v3.4 and beyond).

*Note: This roadmap is a living document and does not represent a binding commitment to development timelines.*

## Planned for Version 3.4

### Resume logic for national runs

Before processing each state, check whether Output_State_XX already contains a production CSV from the current run date. If so, skip that state and log it as already complete. This makes interrupted national alignment and enrichment runs resumable without starting over.

### Rural/urban bifurcation in calibration

Introduce area type as a second dimension in the smoothing factor framework, allowing separate horizontal factors for rural and urban alignments within the same functional class. Requires changes to the calibration tool (`tools/rat_national_calibration.py`), `rat_core.py`, and the GUI.

### Line correction and connectivity (National Road Network work)

Add the line correction and connectivity methods developed for the National Road Network (NRN) to the suite, so that a State, county or city can correct the position of its network and check its connections as well as add elevations to it. The NRN work places each line from USGS 3DEP lidar and aerial imagery: two directional lines at the center of the through lanes on a divided road, and one line at the center of the pavement on an undivided road. It then connects the corrected lines at intersections, ramps and state lines, using the alignment tool's at-grade and grade-separated results, and writes nodes, links and a review list. It has been tested so far on short pieces in Virginia, Iowa, Nebraska, Tennessee, Colorado, Alaska, Idaho and the District of Columbia; the divided-road lane model and the connectivity step are still being settled. Points to resolve before it is added: the order of the tools (curves and elevations would be better measured on the corrected line than on the HPMS line); the size and run time of lidar downloads compared with the DEM; a shared cache for lidar and imagery; the added packages it needs; and licensed imagery (Vantor imagery may be used to correct a network but may not be published, so the method must work without it). Map matching against OpenStreetMap was considered first and set aside: a proof of concept on I-80 in western Nebraska found ARNOLD offset from OSM by under 1 ft to about 44 ft, but on small local roads the OSM and other commercial linework is no better placed than HPMS. Correcting lines would also address lines on old alignments, such as I-80 in Council Bluffs, Iowa, where plan/profile runs now flag the stretch (`Line_Off_Road`) but cannot correct it.

### Same-direction curves with short tangents

Two curves that turn the same way with a short tangent between them are split where the smoothed line's curvature dips between them, but where the smoothing rounds the tangent into a curve nearly as sharp as the two arcs they stay one curve, with a radius larger than either arc. On the Mt. Idaho Grade Road (Idaho) the as-built plans show 50 to 160 ft tangents between same-direction curves; 9 of 10 such pairs stayed joined at the State smoothing factor. The HPMS line cannot show these tangents where its points are farther apart than the tangent (about 40 ft on average there, and up to 190 ft within curves). Corrected centerline geometry would (see Line correction and connectivity).

### Design-equivalent curve ends with a spiral allowance

Curve ends are reported where the smoothed line's curvature starts and ends, which runs long because the smoothing rounds the curve into its tangents (WFL Idaho as-builts: 1.17 of plan length; OR 140: 1.22). Ends computed from the PI as PC = PI - T, PT = PC + R*Delta fix simple curves but cut the spirals off spiral curves (OR 140: 0.77 of plan length). Including a spiral of length Ls, PC = PI - (R tan(Delta/2) + Ls/2) and PT = PC + R*Delta + Ls, with Ls the AASHTO desirable length (2 s of travel at the speed limit) where RAT flags a spiral, gave curve ends within a median 23-45 ft (PC) and 34-60 ft (PT) of the plans on both plan sets (36 curves; measured ends 38-77 and 47-126 ft) and lengths 0.95-1.00 of plan. On 18 test routes at a uniform speed, however, the added spirals made 135-312 of 1,367 design curves overlap their neighbors. Before adopting: use the speed of each section (`Speed_Ref_Mph`), keep each design end within the tangent available, and test on Interstate as-built plans.

### Bridges at state lines

A bridge that runs off the end of a state's data (I-5 over the Columbia River, I-80 over the Missouri River) is held level at the one abutment in the data. Use the neighboring state's geometry and DEM to find the far abutment.

### Steep approach meeting a straight bridge deck

Each bridge deck is drawn as a straight line between its two abutments and the profile is held to that line. Where the approach grade differs sharply from the deck line, the smoothing rounds the corner on the approach side and pulls the profile below the DEM there, which can produce a false steep grade and a short sag or crest at the bridge end. Example: I-5 in Washington at the East Fork Lewis River bridge (MP 18.215), where a -5.0% approach meets a -0.9% deck; the profile runs 2.3 ft below the DEM, giving a -6.6% grade (class E) and a K 61 sag (class E, 35 mph) flagged False_Valley_Warning. The same pattern appears on I-70 at MP 206.2 (1.8 ft), on Iowa I-80 at MP 5.18 (2.3 ft; the K 12 class F crest at MP 5.11) and on I-5 at MP 4.33 (1.6 ft). Approaches tried in October 2026, none adopted: a different span weight taper or a lower vertical smoothing factor (the whole run is smoothed at once, so either moves results at other bridges and elsewhere, while the dip stays near 2 ft); a parabolic vertical curve inserted at the bridge end after smoothing (local, but the profile ends up farther from the DEM, about 3 ft above it at the abutment); and a curved deck matching each abutment's elevation and approach grade (follows the DEM at the abutments but guesses the deck shape and humps on long bridges, 6.8 ft on the 3,390 ft Missouri River bridge). A fix probably needs the deck shape and the bridge-end vertical curves solved together and locally.

### Bridge deck elevations from lidar

The elevations come from the USGS 3DEP 1 m DEM, a bare-earth surface with bridge decks removed, so a bridge shows as a gap and its deck is drawn as a straight line between the two abutments. The lidar point cloud the DEM is made from still has the deck. Keep the DEM as the elevation source for the whole route, and at each bridge found in the DEM or shown by an HPMS or NBI record, read the lidar points in a short window around the span to get the deck's actual profile. This would give the real deck shape in place of the straight line (which bears on the item above), the actual clearance over the road or water below, and a check on spans now accepted from the DEM gap alone (tier 3). Lidar is not proposed for the rest of the route: on ordinary road it gives the same elevations as the DEM at many times the data volume and run time. Two cautions: lidar projects label bridge decks inconsistently, so the deck may have to be found from the points above the gap, not from their class; and the lidar is no newer than the DEM, so a bridge built after the survey shows in neither (eastbound I-80 in Council Bluffs, Iowa: 2020 lidar, bridges built since). The work would go in `rat_core.py`, so the alignment, plan/profile and 4D tools and the National Road Network work use the same bridge elevations. To be taken up after the National Road Network process, which already downloads and reads the lidar along each route, is settled.

### Bridge record limits at the bottom of a gap

Where the DEM edges miss a bridge, a span is built from the HPMS bridge record's limits, and each end is then moved out to road level by fitting the road line 50-200 ft beyond it. That step assumes the end is already near the abutment. When the record's limits fall on the slope or at the bottom of the gap, the road line is fitted partly over the slope, the end stops partway up, and the span can fail the test that the ground lies below the deck. Example: RI-37 E (route 40112620A) at MP 2.17-2.24, where the record starts at the bottom of a 38 ft gap. The DEM edges now find that bridge, so the result is right there, but a bridge with gentler sides and record limits inside the gap would still be missed. A first attempt (moving each record end up the slope to road level before fitting) changed bridges that were already correct on I-5, Iowa I-80 and RI-138 and was not adopted.

### Separate rows for touching bridge spans

The structure span list is built from the vertices, one row for each unbroken run of points on a bridge or tunnel. Two spans that meet end to end are therefore listed as one. Example: Glenbridge Avenue in Providence (route 40057520A), where two bridges about 100 ft apart, each with its own HPMS record, are drawn as two deck lines meeting at MP 0.288 but listed as one 459 ft span. List each span separately, carrying the span limits from the profile step instead of reading them back from the vertices.

### Sight distance warning by section speed

A plan/profile run can take one speed limit for the whole run (**Speed Limit (mph)** in the GUI); the sheets and dashboard then flag available sight distance under the AASHTO stopping sight distance for that speed, and otherwise under a fixed 400 ft. Allow a speed limit for each part of the route (for example, a table of reference point ranges and speeds), and use `Speed_Ref_Mph` for the warning in alignment runs.

### Lines off the road on two-lane roads

The plan/profile off-road flag (`Line_Off_Road`) needs the centerline to fall well away from its usual offset, or a divided-road centerline method, before a station counts; on a two-lane road the centerline is placed within about 15 ft of the line, so a line on the hillside beside the road is not flagged when the ground there is smooth. Seen on two forest roads in Idaho (lanes found at about 5% of stations) and on Snohomish County, Washington, route 700002800I at RP 11.84-11.97, where the line cuts the inside of a curve on a hillside about 30 ft above the road; the cross sections there found no lanes at the line and, at one, a flat strip at road level 110-130 ft to the side. A test for a line on steep ground with no road strip beside it caught the Idaho cases but also flagged about 9 miles of the Seward Highway, where the DEM is coarse. Two further cross-section tests were tried in November 2026 on 18 test routes: a road-level strip beside the line at the elevation expected from the neighboring stations, and a road-like strip found only away from the line. Neither caught route 700002800I, and both flagged many stations on the Seward Highway and the Pikes Peak Highway. Cross sections alone are unlikely to separate these cases; the better path is checking the line against other road geometry (see Line correction and connectivity).

### Elevation source resolution

The 3DEP Elevation service fills areas without 1 m lidar from coarser data (1/3 arc-second, about 10 m, or coarser in parts of Alaska), resampled to the same 1 m grid, and the cached tiles do not record which was used. Query the service for the source resolution of each tile, store it with the tile, and report it on the vertices and curve tables so results from 10 m areas can be identified.

### Smoothing factor changes within a piece of road

Each continuous piece of a route is smoothed with the factor for the functional system covering the most miles of that piece, so a short stretch of a different class (an urban section of a mostly rural route) uses the other class's factor. Allow the factor to change along the piece at functional system boundaries, so each stretch is smoothed as it was calibrated.

### QA exceptions file

Write a CSV of curves with physically implausible values (radius, length, K-value, grade) for review, and restore its output option in the GUI.

### Gaps between HPMS geometry pieces

Where a route's digitized geometry has a physical gap that is joined as if continuous (e.g., Massachusetts Avenue around Mount Vernon Square in DC, about 610 ft), the profile jumps at the join. Detect such gaps and either split the route there or bridge them explicitly.

### Milepost placement check

On a 20-mile Nebraska I-80 test, HPMS bridge limits fell a consistent 0.04 to 0.05 mi past the bridges seen in the DEM. Determine whether this comes from HPMS data or from how mileposts are interpolated along stitched geometry, since the same effect would shift reported curve mileposts.

### Curvature threshold trimming for PC/PT accuracy

Identify the precise coordinate where instantaneous curvature crosses a significance threshold to eliminate endpoint displacement caused by the global spline transition.

## Long-Term Backlog (Future Exploration)

* **Cloud processing.** Run national alignment and calibration in a cloud environment (Databricks) to shorten multi-day runs.
