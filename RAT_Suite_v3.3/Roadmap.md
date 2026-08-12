# RAT Suite Future Roadmap

This document outlines planned features, algorithmic enhancements, and structural improvements under consideration for future releases (v3.4 and beyond). 

*Note: This roadmap is a living document and does not represent a binding commitment to development timelines.*

## Planned for Version 3.4

### Resume logic for national enrichment runs

Before processing each state, check whether Output_State_XX already contains a production CSV from the current run date. If so, skip that state and log it as already complete. This makes interrupted national alignment and enrichment runs resumable without starting over.

### Rural/urban bifurcation in calibration

Introduce area type as a second dimension in the smoothing factor framework, allowing separate H and V factors for rural and urban alignments within the same functional class. Requires changes to rat_national_calibration_cli.py, rat_core.py, and the GUI.

### FS2 H deviation ceiling adjustment

Raise MAX_H_DEV_FT[2] from 15 ft to 18 ft and run a sixth national calibration to address the persistent 25% fallback rate on FS2. Low effort, high confidence given the pattern observed across FS3/FS4.

### Imagery-guided lateral correction prototype

Sidney, NE test case using USGS NAIP orthoimagery to quantify GPS offset magnitude as a first step toward automated centerline correction for HPMS/ARNOLD cleaning.

### Curvature threshold trimming for PC/PT accuracy

Identify the precise coordinate where instantaneous curvature crosses a significance threshold to eliminate endpoint displacement caused by the global spline transition.

## Long-Term Backlog (Future Exploration)

* *Add your ideas here...*