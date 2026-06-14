# Layer Role Mapping

Generated: 2026-06-14T01:06:50

Confidence in [0,1]. Override any row via common/layer_role_mapping_template.csv or --role-map.

| Role | Selected | Fallback | Conf | Geom | Count | Reason |
|------|----------|----------|-----:|------|------:|--------|
| aoi_frame |  |  | 0.0 | Polygon |  | no matching layer |
| bridge_existing | Bridge | Bridge_P | 0.9 | Point |  | name 'Bridge' matched role hints ['bridge']; geometry=Point; count=None |
| building_point | Single_Building |  | 0.9 | Point |  | name 'Single_Building' matched role hints ['single_building', 'building']; geometry=Point; count=None |
| building_poly | Building_Area |  | 0.9 | Polygon |  | name 'Building_Area' matched role hints ['building_area', 'single_building']; geometry=Polygon; count=None |
| canal | Canal |  | 0.9 | Polyline |  | name 'Canal' matched role hints ['canal']; geometry=Polyline; count=None |
| contour_any | Contour_Approx | Contour_Index | 0.75 | Polyline |  | name 'Contour_Approx' matched role hints ['contour']; geometry=Polyline; count=None |
| contour_index | Contour_Index |  | 0.9 | Polyline |  | name 'Contour_Index' matched role hints ['contour_index']; geometry=Polyline; count=None |
| contour_index_anno | Contour_IndexAnno |  | 0.9 | Polygon |  | name 'Contour_IndexAnno' matched role hints ['contour_indexanno', 'contour_index_anno']; geometry=Polygon; count=None |
| contour_interval | Contour_Interval |  | 0.9 | Polyline |  | name 'Contour_Interval' matched role hints ['contour_interval']; geometry=Polyline; count=None |
| dem_raster |  |  | 0.0 | Raster |  | no matching layer |
| drainage_any | Canal | River_L | 0.9 | Polyline |  | name 'Canal' matched role hints ['watercourse', 'canal', 'river_l', 'qanat', 'stream', 'ditch', 'floodway']; geometry=Polyline; count=None |
| elevation_points | Elevation_Points |  | 0.75 | Point |  | name 'Elevation_Points' matched role hints ['elevation_point', 'spot']; geometry=Point; count=None |
| elevation_text_anno | Elevation_PointsAnno |  | 0.9 | Polygon |  | name 'Elevation_PointsAnno' matched role hints ['elevation_pointsanno', 'elevation_points_anno']; geometry=Polygon; count=None |
| point_obstacle | Mine | Well | 0.9 | Point |  | name 'Mine' matched role hints ['tower', 'well', 'mine', 'tank', 'post', 'station']; geometry=Point; count=None |
| powerline | HV_Line | Power_Trans_Line | 0.9 | Polyline |  | name 'HV_Line' matched role hints ['power_trans', 'hv_line', 'power_trans_line']; geometry=Polyline; count=None |
| river_line | River_L | Qanat_Stream | 0.9 | Polyline |  | name 'River_L' matched role hints ['river_l', 'seasonal_river_l', 'qanat', 'stream']; geometry=Polyline; count=None |
| road_any | Dirt_Road | Track_Road | 0.9 | Polyline |  | name 'Dirt_Road' matched role hints ['asphalt_road', 'dirt_road', 'track_road', 'gravel_road', 'freeway', 'highway', 'road']; geometry=Polyline; count=None |
| road_asphalt | Asphalt_Road1_lin | Asphalt_Road2_lin | 0.75 | Polyline |  | name 'Asphalt_Road1_lin' matched role hints ['asphalt_road', 'freeway', 'highway']; geometry=Polyline; count=None |
| road_dirt | Dirt_Road |  | 0.9 | Polyline |  | name 'Dirt_Road' matched role hints ['dirt_road']; geometry=Polyline; count=None |
| road_gravel | Gravel_Road_Lin |  | 0.75 | Polyline |  | name 'Gravel_Road_Lin' matched role hints ['gravel_road']; geometry=Polyline; count=None |
| road_track | Path_Lin | Track_Road | 0.9 | Polyline |  | name 'Path_Lin' matched role hints ['track_road', 'path_lin']; geometry=Polyline; count=None |
| spring | Continual_Spring | Seasonal_Spring | 0.6 | Point |  | name 'Continual_Spring' matched role hints ['spring']; geometry=Point; count=None |
| spring_continual | Continual_Spring |  | 0.9 | Point |  | name 'Continual_Spring' matched role hints ['continual_spring']; geometry=Point; count=None |
| spring_seasonal | Seasonal_Spring |  | 0.9 | Point |  | name 'Seasonal_Spring' matched role hints ['seasonal_spring']; geometry=Point; count=None |
| watercourse | Watercourse |  | 0.9 | Polyline |  | name 'Watercourse' matched role hints ['watercourse']; geometry=Polyline; count=None |
