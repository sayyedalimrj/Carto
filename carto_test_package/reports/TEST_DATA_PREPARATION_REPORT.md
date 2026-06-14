# Test Data Preparation Report (plan)

This describes the safe test data the harness stages before running any plugin.
The harness writes a run-specific copy of this report (with actual feature
counts) to `<run_dir>/reports/TEST_DATA_PREPARATION_REPORT.md`.

## Safety model

* The source geodatabase (`Test_Cartography1.gdb`) is opened **read-only**.
* Every plugin runs against **copies** in `<run_dir>/test_data.gdb` (prefixes
  `T01_`..`T07_`). Outputs land in `<run_dir>/result_data.gdb`.
* Origin tags: `REAL_DATA`, `REAL_DATA_SUBSET`, `SYNTHETIC_CONTROLLED`,
  `SYNTHETIC_DERIVED_FROM_REAL_EXTENT`.

## Staged layers per plugin

| Test layer | Origin | Source (detected role) | Purpose |
|------------|--------|------------------------|---------|
| `T00_TestFrame` | SYNTHETIC_DERIVED_FROM_REAL_EXTENT | union extent of dataset | Polygon frame/neatline (dataset `AOI` is a **polyline**, so a polygon frame is synthesized, inset 8% so contours fall both inside and near the edge). Used by Plugin 05 (and available to 03/04). |
| `T01_roads` | REAL_DATA | road_any (Dirt_Road/Track_Road) | Plugin 01 road reference |
| `T01_drains` | REAL_DATA | drainage_any (Watercourse/Canal/River_L) | Plugin 01 drainage reference |
| `T01_bridge_before` | REAL_DATA | bridge_existing (Bridge_P, populated) | Plugin 01 rotate-existing regression |
| `T01_syn_road`,`T01_syn_drain` | SYNTHETIC_CONTROLLED | parallel non-crossing lines | Plugin 01 "no intersections" edge case |
| `T02_roads` | REAL_DATA | road_any | Plugin 02 barrier |
| `T02_points` | REAL_DATA | point_obstacle (Tower/Mine/Well) | Plugin 02 point targets |
| `T02_lines` | REAL_DATA | powerline (HV_Line/Power_Trans_Line) | Plugin 02 line targets |
| `T02_polys` | REAL_DATA | building_poly (Building_Area) | Plugin 02 polygon targets |
| `T02_empty_points` | SYNTHETIC_CONTROLLED | empty point FC | Plugin 02 empty-target edge case |
| `T03_contour_interval` / `T03_contour_index` | REAL_DATA | contour_interval / contour_index | Plugin 03 inputs |
| `T03_obstacles_road` / `T03_obstacles_water` | REAL_DATA | road / watercourse | Plugin 03 obstacles |
| `T04_elevation_text_before` | REAL_DATA | elevation_points (Elevation_Points) | Plugin 04 Mode A points |
| `T04_contours` / `T04_contour_index` | REAL_DATA | contours | Plugin 04 obstacles |
| `T05_dense_contours_before` | REAL_DATA | contour_interval | Plugin 05 input contours |
| `T05_contour_index` | REAL_DATA | contour_index | Plugin 05 protected (index) contours |
| `T06_spring_before` | REAL_DATA | spring_continual (Continual_Spring) | Plugin 06 springs |
| `T06_contours` | REAL_DATA | contour_interval | Plugin 06 elevation context |
| `T07_test_sheets` | SYNTHETIC_DERIVED_FROM_REAL_EXTENT | 2x2 polygons inset from extent | Plugin 07 multiple sheets |

## Synthetic-data rules applied when real situations are sparse

* **No polygon AOI** in the dataset (`AOI` is a CAD-derived polyline) ->
  synthesize `T00_TestFrame` (and `T07_test_sheets`) from the data extent.
* **Plugin 01 "no intersection" edge** -> two parallel synthetic lines that do
  not cross, derived from the dataset centre, so the tool's zero-crossing branch
  is exercised.
* **Plugin 04 overlap** -> if too few real elevation-point/contour overlaps
  exist, the harness can place synthetic points on contour vertices
  (`make_synthetic_points_on_lines`) to force a measurable conflict.
* **Empty target** (`T02_empty_points`) -> exercises the empty-input branch.

All synthetic layers inherit WKID 32638 (the dataset SR), so distances/angles
stay in meters/degrees consistent with the real data.
