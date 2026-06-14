# Running the Carto tests in ArcMap (Python 2.7)

This harness targets **ArcMap 10.x** with the Desktop `arcpy` (Python 2.7). It
contains no Python 3 syntax (no f-strings, no `pathlib`, no `arcpy.mp`-only
APIs), so it loads cleanly in the ArcMap interpreter.

## 1. Which Python executable

Use the ArcMap Python 2.7 interpreter that ships with Desktop, e.g.:

```
C:\Python27\ArcGIS10.6\python.exe
```

(The exact `ArcGIS10.x` folder matches your install. `python -c "import arcpy"`
must succeed.)

## 2. Standalone run (recommended for batch)

```bat
C:\Python27\ArcGIS10.6\python.exe ^
  "<pkg>\tests_arcmap\run_all_carto_tests_arcmap.py" ^
  --input-gdb "C:\data\Test_Cartography1.gdb" ^
  --carto-repo "C:\code\Carto" ^
  --output "C:\data\carto_test_output"
```

* `--input-gdb`  : the geodatabase to test against (opened read-only). Point it
  at the unzipped `Test_Cartography1.gdb` (from `Map for test/`).
* `--carto-repo` : the folder that contains the `Plugin0x_*.pyt` files (the Carto
  repo root, or the `organized/` tree - any folder with the ArcMap `.pyt`s).
* `--output`     : where the timestamped run folder is created.

### Optional flags

| Flag | Purpose |
|------|---------|
| `--plugins Plugin01,Plugin06` | run only some plugins |
| `--test-types smoke,functional` | run only some test types (smoke/functional/edge/regression) |
| `--mxd-folder "C:\sheets"` | enable Plugin 07 `FOLDER_OF_MXDS` mode (folder of `.mxd`) |
| `--role-map-csv "C:\my_roles.csv"` | manual layer-role override (columns `role,layer_name,notes`); example at `common\layer_role_override_example.csv` |
| `--role-map-json "C:\my_roles.json"` | manual layer-role override via JSON |
| `--unsafe` | (not recommended) disable the safe-mode guard |

### Manual layer-role override

The corrected defaults select `Bridge_P`, `Watercourse`, `Track_Road`,
`Dirt_Road`, `Asphalt_Road1_lin`, and synthesize `T00_TestFrame`. To force a
different layer, pass `--role-map-csv` (columns `role,layer_name,notes`):

```bat
... run_all_carto_tests_arcmap.py --input-gdb ... --carto-repo ... --output ... ^
  --role-map-csv "<pkg>\common\layer_role_override_example.csv"
```

## 3. Toolbox run (inside ArcMap, with a map - needed for full Plugin 07)

1. In ArcCatalog / Catalog window, browse to
   `tests_arcmap\Carto_ArcMap_TestHarness.pyt`.
2. Open **Run Carto Plugin Tests (ArcMap)**.
3. Fill: Input Geodatabase, Carto Repository Path, Output Folder, Platform
   (`ArcMap`), optional Plugin/Test-Type selection, Safe Mode (on), Overwrite.
4. (For Plugin 07) add `test_data.gdb\T07_test_sheets` to the map after the first
   run, or just run with the AOI sheets present; running inside ArcMap provides a
   `CURRENT` map document so `AOI_LAYER_IN_CURRENT_MXD` works.

## 4. Outputs

A new folder `<output>\Carto_Test_Run_<timestamp>\` containing:

* `test_data.gdb`  - safe copies the tests run on (`T01_`..`T07_`, `T00_TestFrame`).
* `result_data.gdb` - tool outputs.
* `reports\` - `ALL_TEST_SUMMARY.md` (read first), `ALL_TEST_RESULTS.json/.csv`,
  per-plugin `T0x_*_test_report.json/.csv`, `MAP_DATA_INVENTORY.*`,
  `LAYER_ROLE_MAPPING.*`, `AUTO_FILLED_PLUGIN_PARAMETERS.json`,
  `TEST_DATA_PREPARATION_REPORT.md`, `BEFORE_AFTER_SUMMARY.*`.
* `snapshots_before\` / `snapshots_after\` - `snapshots.gdb` with before/after copies.
* `logs\test_run.log` - full run log.

## 5. Interpreting PASS / FAIL / WARN / SKIP

See the README table. In short: **PASS** = all checks held; **FAIL** = assertion
failed or crash (see `traceback`); **WARN** = ran but a soft check missed;
**SKIP** = missing layer/field/parameter or environment (e.g. Plugin 07 needs an
active map). The suite always continues to the next plugin after a failure.

## 6. ArcMap-specific notes

* Plugin 03's annotation tool (`AutoGenerateAnnotation`) is ArcMap-only and
  requires `arcpy.mapping`; the suite tests the platform-neutral optimizer tool.
* Plugin 05 `Segment Erase` needs an Advanced license; the suite auto-retries
  with `Delete Whole Features` and records which method ran. Frame-edge contours
  are checked for protection (FAIL if removed), and a "protected contours are
  never removed" regression validates the protected-SQL mechanism that also
  guards index contours.
* **Plugin 07 needs a map context.** In a standalone run it is reported as SKIP
  with an explicit message. To run it: (1) open this `.pyt` harness
  (`Carto_ArcMap_TestHarness.pyt`) inside ArcMap with `T07_test_sheets` added to
  the map (AOI_LAYER mode), or (2) pass `--mxd-folder` pointing to a folder of
  `.mxd` documents (FOLDER_OF_MXDS mode).
* Map add-ins / symbology are disabled (`add_map=False`) so the run is headless-safe.
