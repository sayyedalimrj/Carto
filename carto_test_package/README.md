# Carto Plugin Test Package

A complete, runnable automated test framework for **all seven Carto cartography
plugins**, for **both ArcMap (Python 2.7)** and **ArcGIS Pro (Python 3.x)**.

It scans the map geodatabase, detects which layers belong to each plugin, stages
**safe copies** of the data, auto-fills each plugin's parameters, runs
smoke / functional / edge / regression tests, and writes PASS / FAIL / WARN /
SKIP reports with before/after QA.

> **Safety:** the source geodatabase is opened **read-only**. Every plugin runs
> against copies in a per-run `test_data.gdb`, and writes to `result_data.gdb`.
> The source data, map documents and projects are never modified.

---

## Package layout

```
carto_test_package/
  README.md                     <- this file
  RUN_ARCMAP_TESTS.md           <- ArcMap how-to
  RUN_ARCGIS_PRO_TESTS.md       <- ArcGIS Pro how-to

  common/                       <- shared, Py2.7/3.x-safe engine
    carto_test_core.py          <- inventory, role detection, prep, QA, runner, T01..T07
    report_writer.py            <- JSON / CSV / Markdown reports
    test_schema.json            <- per-test-case record schema
    layer_role_mapping_template.csv
    plugin_parameter_template.json

  tests_arcmap/                 <- ArcMap (Python 2.7) harness
    run_all_carto_tests_arcmap.py
    Carto_ArcMap_TestHarness.pyt
    arcmap_test_utils.py

  tests_pro/                    <- ArcGIS Pro (Python 3.x) harness
    run_all_carto_tests_pro.py
    Carto_Pro_TestHarness.pyt
    pro_test_utils.py

  reports/                      <- STATIC analysis (code + data inspection)
    MAP_DATA_INVENTORY.md / .csv
    FIELD_CANDIDATES_BY_LAYER.csv
    LAYER_ROLE_MAPPING.md / .csv
    CODE_INVENTORY.md
    PLUGIN_PARAMETER_SCHEMA.csv
    PLUGIN_FUNCTION_MAP.md
    ARCMAP_PRO_COMPATIBILITY_NOTES.md
    RISKY_CODE_PATHS.md
    AUTO_FILLED_PLUGIN_PARAMETERS.json
    TEST_DATA_PREPARATION_REPORT.md
    BEFORE_AFTER_SUMMARY.md            (placeholder; real one written per run)
    ALL_TEST_SUMMARY.md / ALL_TEST_RESULTS.json / .csv  (placeholders)
```

The `reports/` folder here is the **static** analysis produced by reading the
code and the geodatabase metadata. Each **run** of the harness creates its own
timestamped folder with *live* reports (real counts, real PASS/FAIL):

```
<output>/Carto_Test_Run_<YYYYmmdd_HHMMSS>/
  test_data.gdb        result_data.gdb
  logs/  reports/  snapshots_before/  snapshots_after/  qa_layers/
```

---

## Which plugins are tested

| # | Plugin | Tool tested | Test prefix |
|---|--------|-------------|-------------|
| 01 | Bridge & Culvert | `BuildBridgePoints`, `RotateExistingBridgePoints` | `T01_` |
| 02 | Road Deconflict | `RoadDeconflictTool` | `T02_` |
| 03 | Contour Label Optimizer | `OptimizeContourLabelAnchorsV4` | `T03_` |
| 04 | Elevation Text Deconflict | `ElevationTextDeconflictV5` (Mode A) | `T04_` |
| 05 | Safe Contour Cleaner | `SafeContourCleaner` | `T05_` |
| 06 | Spring Rotation | `SpringRotationFinalSuiteTool` | `T06_` |
| 07 | Batch Grid Builder | `BatchGridBuilder07` | `T07_` |

> Bridge symbol *rotation* is tested under **T01** (Plugin 01's rotate tools);
> spring symbol *rotation* is tested under **T06** (Plugin 06).

---

## Quick start

ArcMap (Python 2.7):

```bat
C:\Python27\ArcGIS10.x\python.exe tests_arcmap\run_all_carto_tests_arcmap.py ^
  --input-gdb "C:\data\Test_Cartography1.gdb" ^
  --carto-repo "C:\code\Carto" ^
  --output "C:\data\carto_test_output"
```

ArcGIS Pro (Python 3):

```bat
"C:\Program Files\ArcGIS\Pro\bin\Python\envs\arcgispro-py3\python.exe" ^
  tests_pro\run_all_carto_tests_pro.py ^
  --input-gdb "C:\data\Test_Cartography1.gdb" ^
  --carto-repo "C:\code\Carto" ^
  --output "C:\data\carto_test_output"
```

See `RUN_ARCMAP_TESTS.md` and `RUN_ARCGIS_PRO_TESTS.md` for full details.

---

## Interpreting results

Open `<run_dir>/reports/ALL_TEST_SUMMARY.md`.

| Status | Meaning | What to do |
|--------|---------|------------|
| **PASS** | Tool ran and all assertions held. | Nothing. |
| **FAIL** | Assertion failed or tool crashed. | Read `error_message`, `traceback`, `arcpy_messages` in `ALL_TEST_RESULTS.json`. |
| **WARN** | Ran, but a soft check missed (e.g. conflicts not reduced, angle slightly out of band). | Inspect the metric in `success_metrics`; may be data-dependent. |
| **SKIP** | Pre-conditions not met (missing layer/field, or no active map for Plugin 07). | Read `skip_reason`; provide the layer/param or run inside the app. |

---

## Overriding layer detection

The harness auto-detects roles (see `reports/LAYER_ROLE_MAPPING.md`). To force a
specific layer for a role:

1. Copy `common/layer_role_mapping_template.csv`, fill `role,selected_layer`.
2. Pass it: `--role-map-csv "C:\path\my_roles.csv"`.

Valid roles include: `road_any`, `road_asphalt`, `road_dirt`, `road_track`,
`watercourse`, `canal`, `river_line`, `drainage_any`, `contour_interval`,
`contour_index`, `elevation_points`, `bridge_existing`, `spring_continual`,
`powerline`, `building_poly`, `point_obstacle`, `aoi_frame`, `dem_raster`.

## Overriding plugin parameters

Tool parameters are positional; see `reports/PLUGIN_PARAMETER_SCHEMA.csv` for the
exact order and `common/plugin_parameter_template.json` for the override shape.
To change a default, edit the relevant constant/args in
`common/carto_test_core.py` (the per-plugin `_pNN_args` builders), or run a single
plugin and adjust.

## Run only part of the suite

* One plugin: `--plugins Plugin06`
* A few: `--plugins Plugin01,Plugin02,Plugin06`
* Only smoke tests: `--test-types smoke`
* Functional + edge: `--test-types functional,edge`
* ArcMap only / Pro only: just run the corresponding runner.

## What if a layer or parameter is not detected?

* The affected test is marked **SKIP** with a precise `skip_reason` (e.g.
  "Elevation field 'Ortho_Hght' missing on contour layer"). The suite continues.
* Provide the missing layer with `--role-map-csv`, or confirm the field exists in
  `reports/FIELD_CANDIDATES_BY_LAYER.csv`.

## Requirements

* ArcMap 10.x with `arcpy` (Python 2.7) **or** ArcGIS Pro with `arcpy` (Python 3).
* Standard library only beyond `arcpy` (no pandas/geopandas/shapely).
* Plugin 05 `Segment Erase` and Plugin 07 grid creation may need Standard/Advanced
  licenses; the suite degrades gracefully and reports clearly when a license is missing.
