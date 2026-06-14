# Running the Carto tests in ArcGIS Pro (Python 3.x)

This harness targets **ArcGIS Pro** with the `arcpy` Pro runtime (Python 3,
`arcgispro-py3`). It uses `arcpy.mp` for any map interaction.

## 1. Which Python executable

Use the ArcGIS Pro conda environment interpreter, typically:

```
C:\Program Files\ArcGIS\Pro\bin\Python\envs\arcgispro-py3\python.exe
```

(`python -c "import arcpy"` must succeed. If you use a cloned env, use that
env's `python.exe`.)

## 2. Standalone run (recommended for batch)

```bat
"C:\Program Files\ArcGIS\Pro\bin\Python\envs\arcgispro-py3\python.exe" ^
  "<pkg>\tests_pro\run_all_carto_tests_pro.py" ^
  --input-gdb "C:\data\Test_Cartography1.gdb" ^
  --carto-repo "C:\code\Carto" ^
  --output "C:\data\carto_test_output"
```

* `--input-gdb`  : geodatabase to test against (read-only). Use the unzipped
  `Test_Cartography1.gdb`.
* `--carto-repo` : folder containing the `Plugin0x_*_Pro_*.pyt` files.
* `--output`     : where the timestamped run folder is created.

### Optional flags

| Flag | Purpose |
|------|---------|
| `--plugins Plugin01,Plugin06` | run only some plugins |
| `--test-types smoke,functional` | run only some test types |
| `--mxd-folder "C:\projects"` | enable Plugin 07 `FOLDER_OF_MXDS` mode (folder of `.aprx`) |
| `--role-map-csv "C:\my_roles.csv"` | manual layer-role override (columns `role,layer_name,notes`); example at `common\layer_role_override_example.csv` |
| `--role-map-json "C:\my_roles.json"` | manual layer-role override via JSON |
| `--unsafe` | (not recommended) disable the safe-mode guard |

### Manual layer-role override

The corrected defaults select `Bridge_P`, `Watercourse`, `Track_Road`,
`Dirt_Road`, `Asphalt_Road1_lin`, and synthesize `T00_TestFrame`. To force a
different layer:

```bat
... run_all_carto_tests_pro.py --input-gdb ... --carto-repo ... --output ... ^
  --role-map-csv "<pkg>\common\layer_role_override_example.csv"
```

## 3. Toolbox run (inside Pro, with a project/map - needed for full Plugin 07)

1. In the Catalog pane, add a Toolbox -> browse to
   `tests_pro\Carto_Pro_TestHarness.pyt`.
2. Open **Run Carto Plugin Tests (Pro)**.
3. Fill: Input Geodatabase, Carto Repository Path, Output Folder, Platform
   (`ArcGISPro`), optional Plugin/Test-Type selection, Safe Mode (on), Overwrite.
4. (For Plugin 07) running inside Pro provides a `CURRENT` project, so
   `AOI_LAYER_IN_CURRENT_MXD` works with `T07_test_sheets` added to the active map.

## 4. Outputs

Identical structure to the ArcMap run: `<output>\Carto_Test_Run_<timestamp>\`
with `test_data.gdb`, `result_data.gdb`, `reports\`, `snapshots_before\`,
`snapshots_after\`, `qa_layers\`, `logs\`. Start with
`reports\ALL_TEST_SUMMARY.md`.

## 5. Interpreting PASS / FAIL / WARN / SKIP

Same semantics as ArcMap (see README). The Pro and ArcMap flavors are kept
behaviorally equivalent, so results should match aside from platform-specific
tools (Plugin 03 annotation; Plugin 07 export).

## 6. Pro-specific notes

* Plugin 03's annotation tool in Pro is `ConvertLabelsToAnnotationPro`
  (`arcpy.mp`); the suite tests the shared `OptimizeContourLabelAnchorsV4`.
* Plugin 05 frame-edge protection is checked (FAIL if frame-edge contours are
  removed), plus a "protected contours are never removed" regression that
  validates the protected-SQL mechanism used to guard index contours.
* **Plugin 07 needs a map context.** In a standalone run it is reported as SKIP
  with an explicit message. To run it: (1) open this `.pyt` harness
  (`Carto_Pro_TestHarness.pyt`) inside ArcGIS Pro with `T07_test_sheets` added
  to the active map (AOI_LAYER mode), or (2) pass `--mxd-folder` pointing to a
  folder of `.aprx` projects (FOLDER_OF_MXDS mode).
* `arcpy.mp` is used only for map detection/add; tool runs are headless-safe
  (`auto_symbology`/`add_to_map` disabled).
