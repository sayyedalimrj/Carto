# Plugin 06 - Spring Rotation - Status

Last updated: 2026-05-26

## Files

| File | Status | Target |
|------|--------|--------|
| `Plugin06_SpringRotation_ArcMap_v4_hardened.pyt` | Working | ArcMap 10.x / Python 2.7 (best-effort Pro) |
| `Plugin06_SpringRotation_Pro_v4_native.pyt`      | Complete (Parts 3a + 3b) | ArcGIS Pro / Python 3 native |

### Pro file structure (sections in the same file)

```
0  Messaging + small utilities       (_msg/_warn/_err/_diag, _wrap360, ...)
1  Describe / SR helpers             (_pick_work_sr, _ensure_field, ...)
2  Selection-bypass                  (_selection_info, _resolve_full_source)
3  Pro map integration               (_add_to_current_map -- arcpy.mp only)
4  AOI helpers                       (_project_and_buffer_aoi, _filter_*)
5  Near + geometry helpers           (_cache_contours_geom, _tangent_az_at_near)
6  Methods 01..05                    (_method_01..05_*)
7  Output writers                    (_write_separate, _write_single_fields, _write_summary_table)
8  Main runner                       (_run_suite)
9  Python Toolbox (Pro UI)           (Toolbox, SpringRotationFinalSuiteTool)
```

### Pro vs Py2.7 parity (verified, post UI-alignment pass)

- Classes: `Toolbox`, `SpringRotationFinalSuiteTool` (same names).
- Parameter count: 27 (26 input + 1 derived) -- matches Py2.7.
- Parameter order, names, defaults, ValueLists, categories: identical.
- AST-level audit: 0 mismatches across 9 attributes x 27 params
  (`displayName`, `name`, `datatype`, `parameterType`, `direction`,
  `category`, `value`, `filter.list`, `parameterDependencies`).
- Output schema: `SPR_TMPID, ROT, OK, NOTE` (separate);
  `ROT, ROT_<m>, OK_<m>, NOTE_<m>` (single fields); summary unchanged.
- `[DIAG]` messages: same wording, same stages.

### UI alignment changes (this pass)

Three labels were aligned across flavors. Names and indices unchanged:

| Param | Old Py2.7 label | Aligned label (both flavors) |
|---|---|---|
| `auto_symbology` | "Auto-apply simple symbology (ArcMap only; if no template)" | "Auto-apply simple symbology (if no template)" |
| `symbol_size`    | "Symbol size (points; ArcMap auto-symbology)"               | "Symbol size (points; auto-symbology)" |
| `aoi_sketch`     | "AOI sketch"                                                | "AOI sketch (polygon)" |

The first two were updated because the Pro build now also implements
auto-symbology, so the "ArcMap only" qualifier was misleading. The third
adopts the more informative Pro label.

### Pro-only changes vs Py2.7

- No `arcpy.mapping` fallback. Map add via `arcpy.mp.ArcGISProject("CURRENT").activeMap.addDataFromPath(...)`.
- Scratch staging via `arcpy.CreateUniqueName(_unique(prefix), arcpy.env.scratchGDB)`.
- `CalculateField` uses `"PYTHON3"` unconditionally.
- f-strings, type hints, no `from __future__ import division`.

## What the plugin does

Computes a per-spring rotation angle ("which way does this spring face downhill?") in
**geographic degrees** (`0 = North`, clockwise) by analyzing nearby contour lines.

Produces 1..5 rotation results per spring, each from a different method, and writes
them as either:
- `SEPARATE_LAYERS`: one output FC per method (`<base>_<method_code>`), or
- `SINGLE_FIELDS`:  one output FC with `ROT`, `ROT_<m>`, `OK_<m>`, `NOTE_<m>` fields.

Optionally writes a per-method summary table (`<base>_Summary`).

## Methods

| Code | Name        | Logic |
|------|-------------|-------|
| 01   | NearTangent | Tangent direction along nearest contour at the closest point |
| 02   | HighLow     | High->low vector across K nearest contour samples (uses elevation) |
| 03   | NearNormal  | Spring -> nearest contour point vector |
| 04   | PlaneFit    | Least-squares plane fit on K samples; gradient direction |
| 05   | CentroidHL  | Centroid(high samples) -> Centroid(low samples) using median split |

01 and 03 use the single-row `Near` output. 02/04/05 use `GenerateNearTable` (K nearest).

## Pipeline (single shared runner: `_run_suite`)

1. Selection-trap guard (`_selection_info`, `_resolve_full_source`) -- always default
   to processing the FULL on-disk dataset.
2. Pick working SR (`_pick_work_sr`): `AUTO_UTM` (default) or `USE_INPUT`.
3. Copy springs+contours into scratch GDB (`spr_tmp_in`, `con_tmp_in`).
4. Optional AOI clip on springs; optional AOI project + buffer + clip on contours.
5. Add `SPR_TMPID = OID` (shapefile-safe via dynamic OID name).
6. Project both to working SR (`spr_tmp_proj`, `con_tmp_proj`).
7. Build elevation lookup `{contour_OID -> Z}`.
8. Run `Near LOCATION` once on springs (drives 01, 03).
9. If any of 02/04/05 selected: run `GenerateNearTable(K)` and join `SPR_TMPID`.
10. For method 01: cache contour geometries (`NEAR_ONLY` by default).
11. Compute selected methods.
12. Write outputs (separate layers or single fields), optional summary table.
13. Add to current map (ArcMap `mapping` OR Pro `arcpy.mp`) with optional
    symbology template / auto symbology.

## Public utility surface (Py2.7 file)

- `_to_bytes_utf8`, `_wrap360`, `_azimuth_geo_deg`, `_safe_delete`
- `_desc`, `_desc_sr`, `_oid_field`, `_is_projected_meter`
- `_utm_sr_for_lonlat`, `_pick_work_sr`, `_ensure_field`, `_color_from_code`
- `_validate_output_name`, `_field_is_numeric`, `_shape_type`
- `_make_layer_name`, `_get_count`, `_profile_msg`
- `_selection_info`, `_resolve_full_source`
- `_add_to_current_map` (dual-mode: arcpy.mp first, else arcpy.mapping)
- `_project_and_buffer_aoi`, `_filter_springs_by_aoi`, `_filter_contours_by_aoi`
- `_cache_contours_geom`, `_tangent_az_at_near`
- `_run_near_location`, `_run_near_table`
- `_method_01_neartangent`, `_method_02_highlow`, `_method_03_nearnormal`,
  `_method_04_planefit`, `_method_05_centroidhl`
- `_copy_output_base`, `_write_separate`, `_write_single_fields`,
  `_write_summary_table`
- `_run_suite` (the main orchestrator)
- Toolbox + `SpringRotationFinalSuiteTool` (UI)

## Toolbox / UI (Py2.7)

Single tool `SpringRotationFinalSuiteTool` with parameters grouped into
8 categories (`01 Inputs`, ..., `08 Methods`). Index map (after the
`ignore_selection` toggle was inserted at position 3):

```
0  springs            (GPFeatureLayer, point)
1  contours           (GPFeatureLayer, polyline)
2  elev_field         (Field, depends on contours)
3  ignore_selection   (GPBoolean, default True)
4  out_gdb            (DEWorkspace)
5  out_base_name      (GPString)
6  output_mode        (SEPARATE_LAYERS | SINGLE_FIELDS)
7  create_summary     (GPBoolean)
8  sym_layer          (GPFeatureLayer, optional)
9  auto_symbology     (GPBoolean)
10 symbol_size        (GPDouble)
11 work_sr_mode       (AUTO_UTM | USE_INPUT)
12 near_method        (PLANAR | GEODESIC)
13 search_radius      (GPDouble, optional)
14 global_offset      (GPDouble)
15 k_near             (GPLong, default 8)
16 tangent_step       (GPDouble, default 5.0)
17 aoi_sketch         (GPFeatureRecordSetLayer, polygon schema)
18 aoi_buffer         (GPDouble)
19 cache_mode         (NEAR_ONLY | ALL)
20 profile            (GPBoolean)
21 run_01..25 run_05  (GPBoolean toggles per method)
26 outputs            (Derived GPString, ;-joined paths)
```

`updateParameters` enables/disables K, tangent_step, cache, AOI buffer, sym
fields based on selections. `updateMessages` validates geometry, OID/field
types, GDB existence, K>=2, step>0, radius>=0, AOI buffer>=0.

## What the Pro v4 native build must preserve

- Identical method math (01..05) and identical default values.
- Identical parameter UI (same categories, names, defaults), with the
  same indices so a saved tool history works.
- Selection bypass behavior (`ignore_selection=True` by default).
- All `[DIAG]` messages so the operator sees stage-by-stage counts.
- Output schemas: `SPR_TMPID`, `ROT`, `OK`, `NOTE` (separate layers); plus
  per-method `ROT_<m>/OK_<m>/NOTE_<m>` (single fields).
- Summary table schema: `METHOD, N_TOTAL, N_OK, N_FAIL, OK_PCT`.

## What the Pro v4 native build should change

- Drop `from __future__ import division`; require Py3.
- `arcpy.mp` only (no `arcpy.mapping` fallback). `addDataFromPath` for map add.
- Use `arcpy.management.*` / `arcpy.analysis.*` namespaces consistently
  (already in use in v4, keep).
- f-strings for messages.
- `typing` annotations on helpers.
- Use `arcpy.CreateUniqueName` for scratch staging like Plugin 5 Pro does.
- Keep helper names identical so the UI module / tests can be shared.

## Acceptance criteria for Pro file

1. Loads as a `.pyt` in ArcGIS Pro with no syntax warnings.
2. Tool dialog shows the same 8 categories and 26 input parameters in the
   same order.
3. Running on a small test set produces the same `ROT` values (within
   floating tolerance) as the Py2.7 build.
4. With an active selection on springs, `Process ALL = True` still
   processes the entire dataset (the trap fix is preserved).
5. Outputs are added to the active Pro map.
