# RAT Suite Future Roadmap

This document outlines planned features, algorithmic enhancements, and structural improvements under consideration for future releases (v3.3 and beyond). 

*Note: This roadmap is a living document and does not represent a binding commitment to development timelines.*

## Planned for Version 3.3

### Algorithmic Refinements
* **Curve Endpoint Trimming:** Implement a curvature threshold cutoff to correct spline-induced endpoint displacement (pulling PC/PT inward from the tangent extensions).
* **Bridge Anchor Point Filtering:** Add logic to suppress artificial `SAG` curve classifications that occur at the transition boundaries of interpolated bridge spans.

### Architecture & Configuration
* **JSON Threshold Exposure:** Move hardcoded thresholds (like `V_MIN_GRADE_CHANGE`) into `national_smoothing_factors.json` to allow state-by-state override capabilities for extreme terrain.

### Output & Validation
* **Expanded Validator Coverage:** Update `rat_results_validator.py` to include integrity checks for the `alignment_vertices` and `section_scores` CSV outputs (e.g., milepost continuity and bounding box checks).

## Long-Term Backlog (Future Exploration)
* *Add your ideas here...*