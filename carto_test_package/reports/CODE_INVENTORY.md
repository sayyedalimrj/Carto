# Code Inventory - Carto Plugins

This inventory is the result of a full read of every current `.pyt` in the
Carto repository (ArcMap Py2.7 "hardened" builds and ArcGIS Pro "native"
builds), plus the project docs (`PROJECT_MAP.md`, `ARCHITECTURE_SUMMARY.md`,
`UI_GUIDE.md`). Legacy/reference builds under `organized/.../Legacy` are not
exercised by the test suite.

All plugins are **Python Toolboxes** (`.pyt`). Each toolbox `Toolbox.tools`
lists one or more tool classes. The test harness invokes a tool by importing
the toolbox (`arcpy.ImportToolbox`) and calling
`getattr(arcpy, "<ToolClass>_<alias>")(*positional_inputs)`.

| Plugin | Tool class(es) | ArcMap file / alias | Pro file / alias |
|--------|----------------|---------------------|------------------|
| 01 Bridge & Culvert | `BuildBridgePoints`, `BuildCulvertPoints`, `RotateExistingBridgePoints`, `RotateExistingCulvertPoints` | `Plugin01_BridgeCulvert_ArcMap_py27.pyt` / `bridgeCulvertArcMap` | `Plugin01_BridgeCulvert_Pro_py3.pyt` / `bridgeCulvertPro` |
| 02 Road Deconflict | `RoadDeconflictTool` | `Plugin02_RoadDeconflict_ArcMap_v5_hardened.pyt` / `plugin2_road_deconflict_arcmap_v6` | `Plugin02_RoadDeconflict_Pro_v5_native.pyt` / `plugin2_road_deconflict_pro` |
| 03 Contour Label Optimizer | `OptimizeContourLabelAnchorsV4`, `ValidateLabelAnchors`, `CurvatureHeatmap`, `AutoGenerateAnnotation` (ArcMap) / `ConvertLabelsToAnnotationPro` (Pro), `RunUnitTests` | `Plugin03_ContourLabelOptimizer_ArcMap_v4_hardened.pyt` / `contourlabelopt5_arcmap` | `Plugin03_ContourLabelOptimizer_Pro_v4_native.pyt` / `contourlabelopt4_pro` |
| 04 Elevation Text Deconflict | `ElevationTextDeconflictV5` | `Plugin04_ElevationTextDeconflict_ArcMap_v5_hardened.pyt` / `elevtext_v6_arcmap` | `Plugin04_ElevationTextDeconflict_Pro_v5_native.pyt` / `elevtext_pro` |
| 05 Safe Contour Cleaner | `AOIBrushBuilder`, `SafeContourCleaner` | `Plugin05_SafeContourCleaner_ArcMap_v5_hardened.pyt` / `carto_auto_arcmap_v5` | `Plugin05_SafeContourCleaner_Pro_v5_native.pyt` / `carto_auto_pro_v5` |
| 06 Spring Rotation | `SpringRotationFinalSuiteTool` | `Plugin06_SpringRotation_ArcMap_v4_hardened.pyt` / `SpringRotationSuiteV4` | `Plugin06_SpringRotation_Pro_v4_native.pyt` / `SpringRotationSuiteProV4` |
| 07 Batch Grid Builder | `BatchGridBuilder07` | `Plugin07_BatchGridBuilder_ArcMap_v6_hardened.pyt` / `plugin07_batch_grid_v6` | `Plugin07_BatchGridBuilder_Pro_v6_native.pyt` / `plugin07_batch_grid_pro_v6` |

---

## Plugin 01 - Bridge & Culvert Type & Angle Correction

* **Execution type:** Python toolbox, 4 tools (2 Create, 2 Rotate-existing).
* **Create tools** (`BuildBridgePoints` ref=ROAD, `BuildCulvertPoints` ref=DRAIN):
  * Inputs: `roads` (multi), `drains` (multi), `out_ws`, `out_name`,
    `sample_m` (8.0), `rot_field`, `rot_type` (GEOGRAPHIC), `add_map`,
    `tmpl_lyr`, `end_tol` (2.0).
  * Logic: merge reference lines in `scratchGDB` -> build TRUE road x drainage
    crossing points (`_build_crossing_points`) -> filter end-touch / T-junction
    points (`_filter_endtouch`) -> add `ROT_RAW` + rotation field ->
    `_apply_rotation` (snap to host line). Output is a NEW point FC.
  * Creates fields: `ROT_RAW` (DOUBLE), rotation field (default `ROTATION`).
  * **Does not delete or edit existing bridge/culvert features.**
* **Rotate-existing tools** (`RotateExistingBridgePoints`,
  `RotateExistingCulvertPoints`):
  * Inputs: `in_pts`, `ref` (multi lines), `upd_mode`
    (`COPY_TO_OUTPUT` default / `UPDATE_IN_PLACE`), `out_ws`, `out_name`,
    `sample_m`, `rot_field`, `rot_type`, `add_map`, `tmpl_lyr`.
  * Logic: optionally copy input -> add `ROT_RAW`/rotation field ->
    `_apply_rotation(..., snap_to_line=False)`. **Geometry is never moved.**
  * `UPDATE_IN_PLACE` writes the rotation field back into the **input** layer
    (destructive to schema/values) - see RISKY_CODE_PATHS.
* **Errors:** `arcpy.ExecuteError` re-raised after logging `arcpy.GetMessages(2)`;
  `MemoryError`/`OSError` intentionally propagate. Scratch intermediates are
  deleted in `finally`.

## Plugin 02 - Road Deconflict

* **Execution type:** Python toolbox, single tool `RoadDeconflictTool`.
* **Inputs (23):** `in_roads` (barrier), `clearance` (6.0), `in_points`/`in_lines`/
  `in_polygons` (multi targets to move), `out_gdb`, `name_suffix` (`_RDCL`),
  `aoi_poly`, `line_strategy` (LOCAL_PUSH/WHOLE_OFFSET), `offset_side`,
  `densify_step`, `preserve_endpoints`, `smooth_iters`, `max_shift`, `max_iter`
  (8), `max_deflection_deg` (25), `use_near`, `lock_field`, `create_errors`,
  `create_vectors`, `write_csv`, `keep_near_fields`, `near_chunk_size` (50000).
* **Logic:** copy targets to `scratchGDB` -> optional AOI clip of roads ->
  `GenerateNearTable` / per-feature push so features clear the road symbol
  width -> write **moved copies** named `<inputName><name_suffix>` to `out_gdb`.
  Optional error FCs, displacement vectors and CSV report.
* **Outputs:** one moved FC per input target, optional `*_errors`,
  `*_vectors`, CSV.
* **Safety:** reads inputs read-only; all edits happen on copies.

## Plugin 03 - Contour Label Optimizer

* **Execution type:** Python toolbox, primary tool `OptimizeContourLabelAnchorsV4`
  (+ QA tools `ValidateLabelAnchors`, `CurvatureHeatmap`, `RunUnitTests`, and an
  annotation generator that differs per platform).
* **Inputs (34 + 5 derived):** `in_contours`, `elev_field`, `selection_mode`
  (ALL/MAJOR_INTERVAL), `major_interval`, `interval_m` (500), `safe_mm` (2),
  `halo_mm`, `map_scale` (25000), `obstacles` (multi), `anno_layer`,
  text-metric params, curvature method + weights, scoring weights, thresholds
  (`min_contour_m`, `short_policy`), `out_ws`, `out_segments_name`
  (`ContourLabelSegments`), `out_points_name` (`ContourLabelPoints`),
  footprint/stats toggles + names, `max_tries` (11), `use_legacy_evaluation`.
* **Logic:** sample candidate label anchors along contour parts, score each by
  curvature/overlap/centering, avoid obstacle masks (spatial-indexed mask FC in
  scratchGDB), pick best per window. Outputs label points + segments
  (+ optional footprints, stats table).
* **Annotation:** `AutoGenerateAnnotation` (ArcMap) requires `arcpy.mapping`;
  the Pro build replaces it with `ConvertLabelsToAnnotationPro` (`arcpy.mp`).

## Plugin 04 - Elevation Text Deconflict

* **Execution type:** Python toolbox, single tool `ElevationTextDeconflictV5`,
  two modes.
* **Mode A** (`POINT_LAYER_WITH_TEXT_FIELD`): `in_points` + `text_field` +
  `obstacle_layers` (required, multi). Builds a label box per point, searches
  ring/direction candidate offsets, and writes a **moved copy** so labels clear
  obstacles (contours, grid, frame...).
* **Mode B** (`ANNOTATION_LAYER_AND_ANCHOR_POINTS`): operates on a GDB
  annotation FC + anchor points; can write an Angle/rotation field; `preview_only`
  guards the Mode-B copy.
* **Inputs (34 + 6 derived):** input_mode, mode-A/B inputs, `rings` ("2 4 6"),
  `directions` (16), `obstacle_layers`, `conflict_test_mode`, scale/font,
  reporting/debug, search pattern/bias, leader-line options,
  `use_legacy_evaluation`.
* **Derived outputs:** report-all, report-unresolved, moved-copy, moved-only,
  label-position points, leader lines (staged via `CreateUniqueName`).

## Plugin 05 - Safe Contour Cleaner

* **Execution type:** Python toolbox, tools `AOIBrushBuilder`, `SafeContourCleaner`.
* **SafeContourCleaner inputs (24):** `in_contours` (multi), `frame_polygon`,
  `safe_margin_mu`, `safe_margin_mm`, `dense_threshold` (20, required),
  `min_neighbors` (1), `aoi_mode` ("Frame only (default)" / "Custom AOI only" /
  "Frame AND Custom AOI" / "Entire dataset (no AOI)"), `custom_aoi`, `mask_mode`,
  `external_mask`, `eligible_sql` ("1=1"), `protected_sql` (""), `removal_method`
  ("Segment Erase (recommended)" / "Delete Whole Features"), `min_seg_length`,
  `out_workspace`, `out_clean_name` (`Contours_CartoClean`), output toggles,
  `dry_run`, `near_chunk_size` (50000), `allow_full_map_processing` (False).
* **Logic:** resolve AOI from frame/custom polygon, detect dense clusters
  (`GenerateNearTable` within threshold), build a removal mask, **copy contours
  to the output**, then erase/delete dense eligible segments while protecting
  the frame-edge safe margin and any `protected_sql` selection. The input layer
  is never edited (a clean copy is produced).
* **Safety:** empty AOI with `allow_full_map_processing=False` raises (prevents
  accidental whole-dataset edits). `Segment Erase` uses `arcpy.analysis.Erase`
  which needs an Advanced license.

## Plugin 06 - Spring Rotation

* **Execution type:** Python toolbox, single tool `SpringRotationFinalSuiteTool`.
* **Inputs (26 + 1 derived):** `springs` (point), `contours` (polyline),
  `elev_field` (numeric), `ignore_selection` (True), `out_gdb` (must EXIST,
  `*.gdb`), `out_base_name` (`springs_rotation_suite`), `output_mode`
  (SEPARATE_LAYERS / SINGLE_FIELDS), `create_summary`, symbology params,
  `work_sr_mode` (AUTO_UTM), `near_method` (PLANAR), `search_radius`,
  `global_offset`, `k_near` (8), `tangent_step` (5), `aoi_sketch` (polygon
  FeatureSet), `aoi_buffer`, `cache_mode`, `profile`, method toggles
  `run_01`..`run_05` (01/02 on by default).
* **Logic:** copy springs to scratch -> auto-pick UTM working SR -> Near to
  contours -> compute a rotation per method (NearTangent, HighLow, etc.) ->
  write `ROT`/`OK`/`NOTE` fields. **Geometry is never moved**; only rotation
  attributes are computed. Output schema fields: `SPR_TMPID`, `ROT`, `OK`,
  `NOTE` (+ summary table `METHOD,N_TOTAL,N_OK,N_FAIL,OK_PCT`).
* **Note:** Plugin 06 rotates **spring** symbols. *Bridge* symbol rotation is
  provided by Plugin 01's Rotate-existing tools; the test suite covers bridge
  rotation under T01 and spring rotation under T06.

## Plugin 07 - Batch Grid Builder

* **Execution type:** Python toolbox, single tool `BatchGridBuilder07`.
* **Modes:** `FOLDER_OF_MXDS` (iterate map documents/projects in a folder) and
  `AOI_LAYER_IN_CURRENT_MXD` (iterate AOI/sheet polygons of a layer in the
  active map). Engines: `SMART_FEATURE` (build grid/graticule features) or
  `ESRI_XML` (apply a grid template XML).
* **Key inputs:** `mode`, `mxd_folder`, `aoi_layer`, `aoi_name_field`, `engine`,
  `grid_xml`, `out_ws`, `fds_name` (`Grids`), `refscale_denom` (25000),
  `respect_df_rotation`, `spacing_proj` (1000), label/tick options, graticule
  options (interval, mode, WKID 4326, hemisphere).
* **Environment dependency:** requires an active map document
  (`arcpy.mapping` in ArcMap, `arcpy.mp` in Pro) for `AOI_LAYER` mode, or a
  folder of MXDs/projects for `FOLDER_OF_MXDS`. The standalone runners cannot
  provide a CURRENT map, so Plugin 07 is **SKIP**ped there with a clear reason;
  run it from the `.pyt` TestHarness inside ArcMap/Pro (with `T07_test_sheets`
  in the map) or pass `--mxd-folder`.
