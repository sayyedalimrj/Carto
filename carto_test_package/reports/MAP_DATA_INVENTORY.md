# Map Data Inventory (static, from GDB_Items metadata)

Source geodatabase: `Test_Cartography1.gdb`
Spatial reference: **WGS_1984_UTM_Zone_38N (WKID 32638, meters)** for all feature classes.
Datasets: **136** feature classes across feature datasets (Structures, Transportation, Hydrology, Building, Boundary, Urban_Facilities, Elevation_Features, Unknown_Features).

> NOTE: geometry type, Z/M, fields, nullability, parent dataset and spatial reference below are AUTHORITATIVE (read directly from the file geodatabase system table `GDB_Items`). **Record counts and sample records are computed at run time** by the harness (`arcpy.GetCount` + `SearchCursor`) and written into each run's `reports/MAP_DATA_INVENTORY.csv`; they are left blank here because arcpy was not available at authoring time.

| Name | Parent dataset | Geom | Z | M | Anno | Elevation fields | Angle fields | Type/Code fields |
|------|----------------|------|---|---|------|------------------|--------------|------------------|
| Barbed_Wire | Boundary | Polyline |  |  |  |  |  | Code |
| Border_Rod | Boundary | Point |  |  |  |  |  | Code |
| Cemetery | Boundary | Point |  |  |  |  |  | Code |
| Fence | Boundary | Polyline |  |  |  |  |  | Code;Kind |
| HV_Post | Boundary | Point |  |  |  |  |  | Code |
| Historical_Site | Boundary | Point |  |  |  |  |  | Code |
| Industrial_Center | Boundary | Point |  |  |  |  |  | Code;Type;Facil_Type |
| Internation_Bound | Boundary | Polyline |  |  |  |  |  | Code;Kind |
| Limit | Boundary | Polyline |  |  |  |  |  | Code |
| Military_Area | Boundary | Polyline |  |  |  |  |  | Code |
| Mine | Boundary | Point |  |  |  |  |  | Code |
| Oil_Gas_Tank | Boundary | Point |  |  |  |  |  | Code;Type |
| Oil_Refinery | Boundary | Point |  |  |  |  |  | Code;Kind |
| Other_Cemetery | Boundary | Point |  |  |  |  |  | Code |
| Park | Boundary | Polygon |  |  |  |  |  | Code;Kind |
| Power_Regularization | Boundary | Point |  |  |  |  |  | Code;Fluid_Type |
| Power_Station | Boundary | Point |  |  |  |  |  | Code |
| Prison | Boundary | Point |  |  |  |  |  | Code |
| Ruins | Boundary | Polyline |  |  |  |  |  | Code |
| Seaport | Boundary | Polyline |  |  |  |  |  | Code;Type |
| Seaport_L | Boundary | Polyline |  |  |  |  |  | Code;Type |
| Stadium | Boundary | Point |  |  |  |  |  | Code;Kind |
| Tent | Boundary | Point |  |  |  |  |  | Code |
| Wall | Boundary | Polyline |  |  |  |  |  | Code;Type |
| Barn | Building | Point |  |  |  |  |  | Code |
| Building_Area | Building | Polygon |  |  |  |  |  | Code |
| Church | Building | Point |  |  |  |  |  | Code |
| Educational_Center | Building | Point |  |  |  |  |  | Code;Kind |
| Feuling_Station | Building | Point |  |  |  |  |  | Code |
| Hospital | Building | Point |  |  |  |  |  | Code |
| Livestock_Poultry | Building | Point |  |  |  |  |  | Code;Kind;Type |
| Medical_Center_Pnt | Building | Point |  |  |  |  |  | Code;Kind |
| Mill | Building | Point |  |  |  |  |  | Code;Kind |
| Mosque | Building | Point |  |  |  |  |  | Code |
| PTT_Office | Building | Point |  |  |  |  |  | Code;Kind |
| Pantheon | Building | Point |  |  |  |  |  | Code |
| Police_Station | Building | Point |  |  |  |  |  | Code |
| Single_Building | Building | Point |  |  |  |  |  | Code |
| Soole | Building | Point |  |  |  |  |  | Code |
| Tomb | Building | Point |  |  |  |  |  | Code |
| Water_Storage | Building | Point |  |  |  |  |  | Code |
| GPS_Activ_Network | Control_Points | Point |  |  |  |  |  | Code |
| Altered_Areas | Elevation_Features | Polygon |  |  |  |  |  | Code |
| Break | Elevation_Features | Polyline |  |  |  |  |  | Code |
| Cliff | Elevation_Features | Polygon |  |  |  |  |  | Code |
| Contour_Approx | Elevation_Features | Polyline | Y |  |  | Ortho_Hght |  | Code |
| Contour_Index | Elevation_Features | Polyline | Y | Y |  | Ortho_Hght;MIN_Height_OnContour;MAX_Height_OnContour;Maybe_Correct_Height |  | Code;CHECK_Contour_Type;Maybe_Correct_Contour_Type |
| Contour_IndexAnno | Elevation_Features | Polygon |  |  | Y | ZOrder;HorizontalAlignment |  | AnnotationClassID;SymbolID |
| Contour_Interval | Elevation_Features | Polyline | Y | Y |  | Ortho_Hght;MIN_Height_OnContour;MAX_Height_OnContour;Maybe_Correct_Height |  | Code;CHECK_Contour_Type;Maybe_Correct_Contour_Type |
| Desert | Elevation_Features | Point |  |  |  |  |  | Code |
| Elevation_Points | Elevation_Features | Point | Y |  |  | Ortho_Hght;MIN_Height_OnContour;MAX_Height_OnContour;TYPE_Spot_Points |  | Code;TYPE_Spot_Points |
| Elevation_PointsAnno | Elevation_Features | Polygon |  |  | Y | ZOrder;HorizontalAlignment |  | AnnotationClassID;SymbolID |
| Embankment | Elevation_Features | Polyline |  |  |  |  |  | Code |
| Mountain | Elevation_Features | Point |  |  |  |  |  | Code;Kind |
| Pit | Elevation_Features | Polyline |  |  |  |  |  | Code |
| Plains | Elevation_Features | Point |  |  |  |  |  | Code |
| Reverse_Contour | Elevation_Features | Polyline | Y |  |  | Ortho_Hght |  | Code |
| Trench_Up | Elevation_Features | Polyline |  |  |  |  |  | Code |
| Valley | Elevation_Features | Point |  |  |  |  |  | Code |
| Artificial_Lake | Hydrology | Point |  |  |  |  |  | Code |
| Canal | Hydrology | Polyline |  |  |  |  |  | Code;Fluid_Type;Type |
| Continual_Lake | Hydrology | Polygon |  |  |  |  |  | Code |
| Continual_Spring | Hydrology | Point |  |  |  |  |  | Code |
| Drought_Qanat_Stream | Hydrology | Polyline |  |  |  |  |  | Code |
| Estuary | Hydrology | Point |  |  |  |  |  | Code |
| Flooding_Bound | Hydrology | Polygon |  |  |  |  |  | Code |
| Floodway | Hydrology | Polygon |  |  |  |  |  | Code |
| Island | Hydrology | Point |  |  |  |  |  | Code |
| Lagoon | Hydrology | Point |  |  |  |  |  | Code |
| Mangroves | Hydrology | Polygon |  |  |  |  |  | Code |
| Mud | Hydrology | Polygon |  |  |  |  |  | Code |
| Pool_A | Hydrology | Polygon |  |  |  |  |  | Code |
| Pool_P | Hydrology | Point |  |  |  |  |  | Code |
| Qanat_Stream | Hydrology | Polyline |  |  |  |  |  | Code |
| River_A | Hydrology | Polygon |  |  |  |  |  | Code |
| River_L | Hydrology | Polyline |  |  |  |  |  | Code |
| Sea | Hydrology | Polygon |  |  |  |  |  | Code |
| Seasonal_Lake | Hydrology | Polygon |  |  |  |  |  | Code |
| Seasonal_River_A | Hydrology | Polygon |  |  |  |  |  | Code |
| Seasonal_River_L | Hydrology | Polyline |  |  |  |  |  | Code |
| Seasonal_Spring | Hydrology | Point |  |  |  |  |  | Code |
| Seasonal_Well | Hydrology | Point |  |  |  | Height |  | Kind;Code |
| Stream_Ditch | Hydrology | Polyline |  |  |  |  |  | Code |
| Swamp_Land | Hydrology | Polygon |  |  |  |  |  | Code |
| Water_Pump | Hydrology | Point |  |  |  |  |  | Code |
| Watercourse | Hydrology | Polyline |  |  |  |  |  | Code |
| Well | Hydrology | Point |  |  |  | Height |  | Code;Kind |
| Wetland | Hydrology | Polygon |  |  |  |  |  | Code |
| Bush | Land_Cover | Polygon |  |  |  |  |  | Code |
| Cultivation | Land_Cover | Polygon |  |  |  |  |  | Code;Kind |
| Forest | Land_Cover | Polygon |  |  |  |  |  | Code;Type |
| Grassland | Land_Cover | Polygon |  |  |  |  |  | Code |
| Orchard | Land_Cover | Polygon |  |  |  |  |  | Code |
| Palm | Land_Cover | Polygon |  |  |  |  |  | Code |
| Rice | Land_Cover | Polygon |  |  |  |  |  | Code |
| Salt_Marsh | Land_Cover | Polygon |  |  |  |  |  | Code |
| Sand_Dunes | Land_Cover | Polygon |  |  |  |  |  | Code;Type |
| Spread_Forest | Land_Cover | Polygon |  |  |  |  |  | Code |
| Tea | Land_Cover | Polygon |  |  |  |  |  | Code |
| Vineyard | Land_Cover | Polygon |  |  |  |  |  | Code |
| Asphalt_Airport | Structures | Polyline |  |  |  |  |  | Code |
| Breakwater | Structures | Polyline |  |  |  |  |  | Code;Type |
| Bridge | Structures | Point |  |  |  | Height |  | Code;Type;Kind |
| Bridge_P | Structures | Point |  |  |  | Height |  | Code;Type;Kind |
| Concrete_Dam | Structures | Polyline |  |  |  |  |  | Code;Kind |
| Dam | Structures | Polyline |  |  |  |  |  | Code;Kind;Type |
| Dirt_Dam | Structures | Polyline |  |  |  |  |  | Code;Kind |
| Gravel_Airport | Structures | Polyline |  |  |  |  |  | Code |
| Pharos | Structures | Point |  |  |  |  |  | Code |
| Pire | Structures | Polyline |  |  |  |  |  | Code |
| Refinery | Structures | Point |  |  |  |  |  | Code;Kind |
| Tele_Tower | Structures | Point |  |  |  |  |  | Code;CoSta_Code;CoLin_Code;Tower_Code |
| Tower | Structures | Point |  |  |  |  |  | Code |
| Tunnel_L | Structures | Polyline |  |  |  | Height |  | Code |
| Tunnel_P | Structures | Point |  |  |  | Height |  | Code |
| Asphalt_Road1_lin | Transportation | Polyline |  |  |  |  |  | Code |
| Asphalt_Road2_lin | Transportation | Polyline |  |  |  |  |  | Code |
| Dirt_Road | Transportation | Polyline |  |  |  |  |  | Code |
| Freeway_Highway | Transportation | Polyline |  |  |  |  |  | Code |
| Gravel_Road_Lin | Transportation | Polyline |  |  |  |  |  | Code |
| Path_Lin | Transportation | Polyline |  |  |  |  |  | Code |
| Railway | Transportation | Polyline |  |  |  |  |  | Code;Kind |
| Square | Transportation | Point |  |  |  |  |  | Code |
| Track_Road | Transportation | Polyline |  |  |  |  |  | Code |
| Under_Const_Railway | Transportation | Polyline |  |  |  |  |  | Code |
| AOI | Unknown_Features | Polyline | Y | Y |  | Elevation |  | Linetype |
| Electricity_Post | Urban_Facilities | Point |  |  |  |  |  | Code |
| Gas_Well | Urban_Facilities | Point |  |  |  |  |  | Code;Storg_Type |
| Grnd_Water_Wreser | Urban_Facilities | Point |  |  |  |  |  | Code;Type;Kind |
| HV_Line | Urban_Facilities | Polyline |  |  |  |  |  | Code |
| OilGas_Pipe_Line | Urban_Facilities | Polyline |  |  |  |  |  | Code |
| Oil_Reservoir | Urban_Facilities | Point |  |  |  |  |  | Code;Type;Kind |
| Oil_Well | Urban_Facilities | Point |  |  |  |  |  | Code;Storg_Type |
| Power_Trans_Line | Urban_Facilities | Polyline |  |  |  |  |  | Code;Kind |
| Telecabin_Line | Urban_Facilities | Polyline |  |  |  |  |  | Code |
| Water_Pipe_Line | Urban_Facilities | Polyline |  |  |  |  |  | Code |
