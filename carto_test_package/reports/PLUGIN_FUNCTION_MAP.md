# Plugin Function Map

Internal helper functions per plugin (ArcMap `hardened` builds; the Pro
`native` builds keep the same public surface and equivalent helpers, swapping
Py2 idioms for Py3 and `arcpy.mapping` for `arcpy.mp`). Shared helper *names*
that appear across most plugins implement the common architecture rules
(selection bypass, scratch staging, env snapshot/restore, `[DIAG]` logging).

## Cross-cutting helpers (present in most plugins)
* `_msg` / `_warn` / `_err` / `_diag` - staged, greppable logging (the `[DIAG]`
  lines are part of the public contract).
* `_env_snapshot` / `_env_reset` / `_env_restore` (a.k.a. `_snapshot_gp_env` /
  `_restore_gp_env`) - neutralize and restore `arcpy.env` around a run.
* `_selection_info` / `_resolve_full_source` / `_announce_selection` - the
  "selection trap" fix: resolve a layer with an active selection back to its
  full on-disk source.
* `_safe_delete` / `_flush_in_memory` - clean up scratch intermediates.
* `_get_count` - feature count with diagnostics.

## Plugin 01 - Bridge & Culvert
* `_merge_lines` - merge multi-value reference lines into one scratch FC.
* `_build_crossing_points` - compute TRUE road x drainage crossings.
* `_filter_endtouch` - drop end-touch / T-junction false crossings.
* `_apply_rotation` - compute rotation from host line tangent (snap optional).
* `ensure_file_gdb`, `ensure_field` - output workspace / field guards.
* `_EndpointIndex` (class) - fast endpoint lookup.
* Map: `add_layer_with_symbology` (arcpy.mapping in ArcMap / arcpy.mp in Pro).

## Plugin 02 - Road Deconflict
* `_copy_or_project`, `_is_projected`, `_update_extent` - staging + SR handling.
* `GenerateNearTable`-driven push (in `execute`) with `near_chunk_size` chunking.
* `_sanitize_name` / `_new_name` - output naming with `name_suffix`.
* `_gp_try` - defensive GP call wrapper.
* QC: error FCs, displacement vectors, CSV report writers.

## Plugin 03 - Contour Label Optimizer
* `_tangent_angle_at_distance`, `_make_oriented_rect`, `_dist2d`, `_clamp` -
  geometry math for label boxes.
* `_MaskAABBIndex` (class) + `_PlacedCache` (class) - obstacle mask (spatial
  index) and placed-label cache.
* `_safe_mm_to_units` / `_meters_to_units` / `_meters_per_unit` - map-scale math.
* Curvature scoring (`Hybrid`/`ChordRatio`/`MaxDeflection`/`CurvatureEnergy`).

## Plugin 04 - Elevation Text Deconflict
* `_ObstacleStore` (class) + `_build_aabb_cache` - obstacle index.
* `_conflict_in_box_legacy` / `_conflict_in_box_aabb` - two conflict-test paths
  (the `use_legacy_evaluation` switch and `conflict_test_mode`).
* `_envelope_polygon` - label footprint geometry.
* `ascii_safe` / `to_unicode` / `to_utf8` - report text encoding (`report_text_mode`).
* `_PlacedCache` (class) - label-label conflict avoidance (Mode A).

## Plugin 05 - Safe Contour Cleaner
* `_split_multivalue` / `_combine_where` - input + SQL handling.
* `_get_df_scale` / `_get_df_extent_polygon` / `_mm_to_mapunits` - scale math.
* `_normalize_output_path`, `_ensure_scratch`, `_scratch_unique` - output/scratch.
* Dense-cluster detection (`GenerateNearTable` within `dense_threshold`),
  frame-edge protection band, mask build, segment Erase / whole-feature delete.
* `AOIBrushBuilder` tool: create/add/subtract AOI polygons (uses `Erase`).

## Plugin 06 - Spring Rotation
* `_pick_work_sr` / `_utm_sr_for_lonlat` / `_is_projected_meter` - auto working SR.
* `_azimuth_geo_deg` / `_wrap360` - rotation math (0..360).
* `_project_and_buffer_aoi` / `_filter_springs_by_aoi` / `_filter_contours_by_aoi`
  - AOI handling.
* Method engines 01..05 (NearTangent, HighLow, NearNormal, PlaneFit, CentroidHL).
* `_ensure_field`, `_validate_output_name`, `_field_is_numeric` - output guards.
* Writes `SPR_TMPID`, `ROT`, `OK`, `NOTE`; summary `METHOD,N_TOTAL,N_OK,N_FAIL,OK_PCT`.

## Plugin 07 - Batch Grid Builder
* `_rotate_xy`, `_extent_edges_display`, `_display_to_data_xy`,
  `_data_to_display_xy`, `_ceil_to_interval` - grid/graticule geometry, respects
  data-frame rotation.
* `_ensure_feature_dataset`, `_is_gdb`, `_validate_name` - output structure.
* `_product_level` / `_require_cartography_level` - license gating.
* `_mm_to_map_units` - tick/label sizing.
* Map iteration: `arcpy.mapping.ListDataFrames/ListLayers/MapDocument` (ArcMap)
  vs `arcpy.mp.ArcGISProject` + layouts (Pro). Per-sheet processing loop with
  per-sheet success/failure logging; optional PDF/PNG/JPEG export (ArcMap).
