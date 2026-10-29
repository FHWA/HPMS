# RAT Suite Future Roadmap

This document outlines planned features, algorithmic enhancements, and structural improvements under consideration for future releases (v3.4 and beyond).

*Note: This roadmap is a living document and does not represent a binding commitment to development timelines.*

## Planned for Version 3.4

### Resume logic for national runs

Before processing each state, check whether Output_State_XX already contains a production CSV from the current run date. If so, skip that state and log it as already complete. This makes interrupted national alignment and enrichment runs resumable without starting over.

### Rural/urban bifurcation in calibration

Introduce area type as a second dimension in the smoothing factor framework, allowing separate horizontal factors for rural and urban alignments within the same functional class. Requires changes to the calibration tool (`tools/rat_national_calibration.py`), `rat_core.py`, and the GUI.

### Centerline correction using OpenStreetMap

Use map matching against OpenStreetMap road geometry to correct the horizontal position of HPMS/ARNOLD centerlines. A proof of concept on I-80 in western Nebraska found ARNOLD offset from OSM by under 1 ft to about 44 ft, drifting smoothly within each digitized piece and stepping at the seams between pieces. It also found that the matched OSM path should replace the segment's geometry rather than nudge individual vertices. Next step: test on a longer rural interstate stretch and a dense urban arterial (e.g., Massachusetts Avenue in DC) before deciding on implementation. This would also address lines on old alignments, such as I-80 in Council Bluffs, Iowa, where plan/profile runs now flag the stretch (`Line_Off_Road`) but cannot correct it.

### Bridges at state lines

A bridge that runs off the end of a state's data (I-5 over the Columbia River, I-80 over the Missouri River) is held level at the one abutment in the data. Use the neighboring state's geometry and DEM to find the far abutment.

### Steep approach meeting a straight bridge deck

Each bridge deck is drawn as a straight line between its two abutments and the profile is held to that line. Where the approach grade differs sharply from the deck line, the smoothing rounds the corner on the approach side and pulls the profile below the DEM there, which can produce a false steep grade and a short sag or crest at the bridge end. Example: I-5 in Washington at the East Fork Lewis River bridge (MP 18.215), where a -5.0% approach meets a -0.9% deck; the profile runs 2.3 ft below the DEM, giving a -6.6% grade (class E) and a K 61 sag (class E, 35 mph) flagged False_Valley_Warning. The same pattern appears on I-70 at MP 206.2 (1.8 ft), on Iowa I-80 at MP 5.18 (2.3 ft; the K 12 class F crest at MP 5.11) and on I-5 at MP 4.33 (1.6 ft). Approaches tried in October 2026, none adopted: a different span weight taper or a lower vertical smoothing factor (the whole run is smoothed at once, so either moves results at other bridges and elsewhere, while the dip stays near 2 ft); a parabolic vertical curve inserted at the bridge end after smoothing (local, but the profile ends up farther from the DEM, about 3 ft above it at the abutment); and a curved deck matching each abutment's elevation and approach grade (follows the DEM at the abutments but guesses the deck shape and humps on long bridges, 6.8 ft on the 3,390 ft Missouri River bridge). A fix probably needs the deck shape and the bridge-end vertical curves solved together and locally.

### Bridge record limits at the bottom of a gap

Where the DEM edges miss a bridge, a span is built from the HPMS bridge record's limits, and each end is then moved out to road level by fitting the road line 50-200 ft beyond it. That step assumes the end is already near the abutment. When the record's limits fall on the slope or at the bottom of the gap, the road line is fitted partly over the slope, the end stops partway up, and the span can fail the test that the ground lies below the deck. Example: RI-37 E (route 40112620A) at MP 2.17-2.24, where the record starts at the bottom of a 38 ft gap. The DEM edges now find that bridge, so the result is right there, but a bridge with gentler sides and record limits inside the gap would still be missed. A first attempt (moving each record end up the slope to road level before fitting) changed bridges that were already correct on I-5, Iowa I-80 and RI-138 and was not adopted.

### Separate rows for touching bridge spans

The structure span list is built from the vertices, one row for each unbroken run of points on a bridge or tunnel. Two spans that meet end to end are therefore listed as one. Example: Glenbridge Avenue in Providence (route 40057520A), where two bridges about 100 ft apart, each with its own HPMS record, are drawn as two deck lines meeting at MP 0.288 but listed as one 459 ft span. List each span separately, carrying the span limits from the profile step instead of reading them back from the vertices.

### Sight distance warning tied to design speed

The plan/profile sheets and dashboards flag available sight distance under a fixed 400 ft. Compare it instead with the stopping sight distance for the section's speed (`Speed_Ref_Mph`).

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