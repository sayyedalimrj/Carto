# ArcMap (Py2.7) vs ArcGIS Pro (Py3) Compatibility Notes

The repository ships **two behaviorally-equivalent flavors** of each plugin
(per `PROJECT_MAP.md` / `ARCHITECTURE_SUMMARY.md`): an ArcMap "hardened" build
(Python 2.7) and a Pro "native" build (Python 3). Parameters, defaults, output
schemas and `[DIAG]` messages are kept identical on purpose (the team enforces
this with an AST parity check; see `UI_GUIDE.md`). The test suite exploits this:
the same positional parameter vectors are used for both platforms.

## What differs between flavors (by design)

| Concern | ArcMap (Py2.7) | ArcGIS Pro (Py3) |
|---------|----------------|------------------|
| Division | `from __future__ import division` | native true division |
| Strings | defensive `bytes/str` (`_to_bytes_utf8`, `to_utf8`) | native `str`, f-strings |
| Map document API | `arcpy.mapping` (with an `arcpy.mp` *fallback* in some helpers) | `arcpy.mp` only (no `arcpy.mapping`) |
| Scratch naming | stable named temps (`spr_tmp_in`, ...) + `_safe_delete` | `arcpy.CreateUniqueName(..., "memory"/scratchGDB)` |
| GP namespaces | mixed `arcpy.management.*` | consistent `arcpy.management.*` / `arcpy.analysis.*` |
| Annotation (Plugin 03) | `AutoGenerateAnnotation` (requires `arcpy.mapping`) | `ConvertLabelsToAnnotationPro` (`arcpy.mp`) |

## Platform-specific tool surface differences detected

* **Plugin 03** exposes a different 4th tool per platform:
  * ArcMap: `AutoGenerateAnnotation` (imports `arcpy.mapping`; raises
    `arcpy.ExecuteError("arcpy.mapping is required (ArcMap)")` if unavailable).
  * Pro: `ConvertLabelsToAnnotationPro` (`arcpy.mp.ArcGISProject`).
  * The primary tool `OptimizeContourLabelAnchorsV4` is identical on both and is
    what the suite tests.
* **Plugin 07** is the most environment-dependent:
  * ArcMap uses `arcpy.mapping.MapDocument`, `ListDataFrames`, `ListLayers`,
    `Layer`, `AddLayer`, and `ExportToPDF/PNG/JPEG`.
  * Pro uses `arcpy.mp.ArcGISProject("CURRENT")` and `.layouts`.
  * Both require either an active map document (`AOI_LAYER_IN_CURRENT_MXD`) or a
    folder of MXDs/projects (`FOLDER_OF_MXDS`). Standalone scripts have no
    CURRENT map, so the suite SKIPs Plugin 07 there with an explicit reason and
    prepares `T07_test_sheets` for a `.pyt`-harness run inside the app.

## Test-harness compatibility design

* `common/carto_test_core.py` and `common/report_writer.py` are written in the
  **Python 2.7 / 3.x intersection** (no f-strings, no pathlib, no type hints,
  lazy arcpy import). They load unchanged under ArcMap 10.x and Pro.
* The ArcMap runner/harness use `arcpy.mapping` detection; the Pro
  runner/harness use `arcpy.mp` detection. This is the only platform-specific
  glue (`tests_arcmap/arcmap_test_utils.py`, `tests_pro/pro_test_utils.py`).
* Tool invocation uses `arcpy.ImportToolbox` + `getattr(arcpy,
  "<ToolClass>_<alias>")`, which works identically on both platforms.

## Potential compatibility pitfalls to watch when reading results

1. **License level** - Plugin 05 `Segment Erase` and Plugin 07 grid building can
   require Standard/Advanced. The suite catches license errors for Plugin 05 and
   retries with `Delete Whole Features`; Plugin 07 surfaces license errors in the
   report rather than hiding them.
2. **`arcpy.mp` vs `arcpy.mapping`** - if a Pro user accidentally loads an ArcMap
   `.pyt`, the ArcMap map helpers try `arcpy.mp` as a fallback, but grid export
   (Plugin 07 ArcMap) is ArcMap-only.
3. **Annotation feature classes** - `Contour_IndexAnno` / `Elevation_PointsAnno`
   are polygon-based GDB annotation; only Plugin 04 Mode B consumes annotation
   directly. The suite focuses Plugin 04 on Mode A (point + text field).
4. **Z/M geometry** - contours and elevation points carry Z (and contours M).
   The plugins copy/stage to scratch; the suite's geometry-validity check uses
   `CheckGeometry` which is Z/M aware.
