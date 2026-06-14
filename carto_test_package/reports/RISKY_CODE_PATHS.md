# Risky Code Paths

Identified by reading every current `.pyt`. "Risky" means: could modify or
delete data, depends on the runtime environment, has hardcoded assumptions, or
has a known platform incompatibility. For each, the test framework's mitigation
is noted. **The framework never lets a plugin touch the source geodatabase** -
every plugin runs against copies in `test_data.gdb` and writes to
`result_data.gdb`.

## Destructive / data-modifying operations

| Plugin | Path | Risk | Mitigation in tests |
|--------|------|------|---------------------|
| 01 | Rotate tools with `upd_mode = UPDATE_IN_PLACE` | Writes `ROT_RAW`/rotation fields back into the **input** layer (schema + values changed). | Suite always uses `COPY_TO_OUTPUT` (the default) and points outputs at `result_data.gdb`. Inputs are copies anyway. |
| 02 | `arcpy.da.UpdateCursor` on moved targets | Edits geometry of target features. | Targets are copies (`T02_points`/`T02_lines`/`T02_polys`); originals untouched; feature-count delta asserted = 0. |
| 05 | `removal_method = "Delete Whole Features"` + `arcpy.management.DeleteFeatures` | Deletes contour features. | Operates on the **output clean copy**, not the input. Suite verifies input copy count unchanged and frame-edge band preserved. |
| 05 | `arcpy.analysis.Erase` (Segment Erase) | Needs Advanced license; can fail. | Suite catches license/Erase errors and retries with `Delete Whole Features`; records which method ran. |
| 05 | `allow_full_map_processing` fallback to map extent | Could process the whole dataset if AOI empty. | Default `False`; suite asserts an empty-AOI run is refused, and always supplies `T00_TestFrame`. |
| 03/04/06/07 | `arcpy.da.UpdateCursor` | Used only on scratch/output copies. | Confirmed by reading code; outputs go to `result_data.gdb`. |
| all | `arcpy.management.Delete` / `Delete_management("in_memory")` | Deletes scratch intermediates in `finally`. | Targets are scratch/`in_memory` only; safe. |

## Environment / platform dependencies

| Plugin | Path | Risk | Mitigation |
|--------|------|------|------------|
| 07 | `arcpy.mapping.MapDocument("CURRENT")` / `arcpy.mp.ArcGISProject("CURRENT")` | Requires an active map document; fails headless. | SKIP in standalone with explicit reason; runnable via `.pyt` harness inside the app or with `--mxd-folder`. |
| 07 | `arcpy.mapping.ExportToPDF/PNG/JPEG` | ArcMap-only export. | Not exercised by the suite (grid-feature creation is the tested behavior). |
| 03 | `AutoGenerateAnnotation` imports `arcpy.mapping` | ArcMap-only; raises if missing. | Suite tests the platform-neutral `OptimizeContourLabelAnchorsV4`; annotation tools are noted, not invoked, by default. |
| 05/06/01 | add-to-map via `arcpy.mapping`/`arcpy.mp` | Needs a map; can warn headless. | Suite sets `add_map`/`add_to_map`/`auto_symbology` to `False`. |

## Hardcoded assumptions

| Where | Assumption | Note |
|-------|-----------|------|
| 06 | `out_gdb` must already EXIST and end with `.gdb` (validated in `updateMessages`). | Suite passes `result_data.gdb` (created up-front). |
| 03/04 | Default `map_scale`/`reference_scale` = 25000; `safe_mm`=2; rings `2 4 6`. | mm->map-unit conversion depends on scale; suite keeps defaults and reports metrics. |
| 02/05 | Distances are **map units** (meters here, WKID 32638). | Dataset SR is projected meters, so defaults are sensible. |
| all | Field names are NOT hardcoded against the input - tools read fields dynamically; **but** Plugin 04 Mode B defaults `featureid_field="FeatureID"` and Plugin 06 writes fixed output field names (`ROT`,`OK`,`NOTE`,`SPR_TMPID`). | Output field names are by design; suite asserts their presence. |

## Python 2 / 3 incompatibilities

* No cross-contamination found: ArcMap builds avoid f-strings/`arcpy.mp`-only
  APIs; Pro builds avoid `arcpy.mapping`. The test framework's shared modules
  are deliberately written in the 2.7/3.x intersection (verified: no f-strings,
  no pathlib, no annotated defs).
* Plugin 03's 4th tool name differs by platform (`AutoGenerateAnnotation` vs
  `ConvertLabelsToAnnotationPro`); the suite only calls the shared primary tool.

## Error handling observed (good signals)

* Plugins re-raise `arcpy.ExecuteError` after logging `arcpy.GetMessages(2)`.
* `MemoryError`/`OSError` intentionally propagate (Plugin 01) instead of being
  swallowed.
* `updateMessages` validates types/ranges/GDB existence but never raises.
* The test framework captures `error_message`, `traceback`, `arcpy_messages`,
  `parameters_used`, `active_workspace`, `platform`, and `plugin_path` for every
  failing case, and continues to the next plugin.
