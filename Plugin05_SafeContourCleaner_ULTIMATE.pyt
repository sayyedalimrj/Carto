# -*- coding: utf-8 -*-
"""
SafeContourCleaner_ULTIMATE.pyt  (ArcMap / Python 2.7)

Toolbox includes TWO tools:
1) AOI Brush Builder (Photoshop-like concept for AOI creation)
   - User digitizes "brush strokes" as a Polyline/Polygon layer
   - Tool buffers/dissolves the strokes to build an AOI polygon
   - Supports Create / Add / Subtract / Replace operations
   - Optional clip to Frame/Neatline polygon
   - Output AOI is a polygon feature class for use in the Cleaner tool

2) Safe Contour Cleaner (Print-Ready)
   - Keeps ORIGINAL contours untouched
   - Writes a NEW cleaned contour polyline output
   - Fixes classic wrong deletions near frame via strict Frame Safe Margin
   - Multi-input contour layers supported
   - Auto dense-zone mask OR user AOI/mask
   - Segment Erase (preferred, Advanced) OR Whole-feature delete
   - Optional outputs: removed segments, review polygons, final mask polygon, CSV report

ArcMap + Python 2.7 + ArcPy
"""

import os
import csv
import uuid
import traceback
import datetime
import arcpy


# -------------------------
# Toolbox wrapper
# -------------------------
class Toolbox(object):
    def __init__(self):
        self.label = "Cartographic Automation (ArcMap)"
        self.alias = "carto_auto_arcmap"
        self.tools = [AOIBrushBuilder, SafeContourCleaner]


# ======================================================================
# TOOL 1: AOI Brush Builder
# ======================================================================
class AOIBrushBuilder(object):
    def __init__(self):
        self.label = "AOI Brush Builder (Create / Add / Subtract)"
        self.description = (
            "Creates an AOI polygon using a 'brush-like' workflow: digitize brush strokes (Polyline/Polygon), "
            "buffer + dissolve them, then optionally add/subtract them from an existing AOI. "
            "Output AOI can be used directly in Safe Contour Cleaner."
        )
        self.canRunInBackground = False

    # ---------- Messaging helpers ----------
    def _msg(self, s):
        try:
            arcpy.AddMessage(s)
        except:
            pass

    def _warn(self, s):
        try:
            arcpy.AddWarning(s)
        except:
            pass

    def _err(self, s):
        try:
            arcpy.AddError(s)
        except:
            pass

    def _unique(self, prefix="tmp"):
        return "{}_{}".format(prefix, uuid.uuid4().hex[:10])

    def _as_bool(self, v):
        if v is None:
            return False
        if isinstance(v, bool):
            return v
        try:
            s = unicode(v).strip().lower()
        except:
            s = str(v).strip().lower()
        return s in (u"true", u"1", u"yes", u"y", u"t")

    def _get_df_scale(self):
        """Return active DataFrame scale in ArcMap, if running inside ArcMap UI."""
        try:
            import arcpy.mapping as mapping
            mxd = mapping.MapDocument("CURRENT")
            df = mxd.activeDataFrame
            if df and df.scale:
                return float(df.scale)
        except:
            return None
        return None

    def _mm_to_mapunits(self, mm, scale, mapunit_name):
        """
        Convert millimeters-on-map to map units (best effort).
        ground_distance_m = (mm / 1000) * scale
        """
        if mm is None or mm <= 0:
            return None
        if scale is None or scale <= 0:
            return None

        meters = (float(mm) / 1000.0) * float(scale)
        mu = (mapunit_name or "").lower()

        if "meter" in mu or mu in ("m", "meters"):
            return meters
        if "foot" in mu or "feet" in mu:
            return meters * 3.280839895
        if "kilometer" in mu or "kilometre" in mu or mu in ("km",):
            return meters / 1000.0
        return None

    def _normalize_output_path(self, out_ws, name):
        """Folder => shapefile, GDB => feature class name."""
        out_ws_low = (out_ws or "").lower()
        is_gdb = out_ws_low.endswith(".gdb") or (".gdb" in out_ws_low) or out_ws_low.endswith(".sde")
        if is_gdb:
            return os.path.join(out_ws, name)
        if not name.lower().endswith(".shp"):
            name = name + ".shp"
        return os.path.join(out_ws, name)

    def _add_layers_to_map(self, layer_paths):
        try:
            import arcpy.mapping as mapping
            mxd = mapping.MapDocument("CURRENT")
            df = mxd.activeDataFrame
            for p in layer_paths:
                if p and arcpy.Exists(p):
                    mapping.AddLayer(df, mapping.Layer(p), "TOP")
            self._msg("AOI output added to current map.")
        except:
            self._warn("Could not add AOI output to the map (run inside ArcMap to enable).")

    # ---------- GP Interface ----------
    def getParameterInfo(self):
        params = []

        p0 = arcpy.Parameter(
            displayName="Brush Strokes Feature (Polyline or Polygon)",
            name="brush_fc",
            datatype="GPFeatureLayer",
            parameterType="Required",
            direction="Input"
        )
        p0.category = "1) Brush Inputs"
        p0.description = (
            "Digitize your brush strokes as Polyline (recommended) or Polygon features. "
            "Polyline strokes will be buffered into a brush area."
        )
        params.append(p0)

        p1 = arcpy.Parameter(
            displayName="Brush Radius (map units) [used for Polyline/Point strokes]",
            name="brush_radius_mu",
            datatype="GPDouble",
            parameterType="Optional",
            direction="Input"
        )
        p1.category = "1) Brush Inputs"
        p1.value = 20.0
        p1.description = "Brush thickness as buffer distance (map units)."
        params.append(p1)

        p2 = arcpy.Parameter(
            displayName="Brush Radius (millimeters on map) [optional override]",
            name="brush_radius_mm",
            datatype="GPDouble",
            parameterType="Optional",
            direction="Input"
        )
        p2.category = "1) Brush Inputs"
        p2.value = 0.0
        p2.description = (
            "If >0, converts mm on map to map units using current ArcMap DataFrame scale. "
            "Final radius = max(map_units_value, converted_mm_value)."
        )
        params.append(p2)

        p3 = arcpy.Parameter(
            displayName="Operation Mode",
            name="operation",
            datatype="GPString",
            parameterType="Required",
            direction="Input"
        )
        p3.category = "2) AOI Logic"
        p3.filter.type = "ValueList"
        p3.filter.list = [
            "Create new AOI (from brush)",
            "Add brush to existing AOI",
            "Subtract brush from existing AOI",
            "Replace existing AOI (overwrite with brush)"
        ]
        p3.value = "Create new AOI (from brush)"
        p3.description = "How to combine the brush area with an existing AOI."
        params.append(p3)

        p4 = arcpy.Parameter(
            displayName="Existing AOI Polygon (optional)",
            name="existing_aoi",
            datatype="GPFeatureLayer",
            parameterType="Optional",
            direction="Input"
        )
        p4.category = "2) AOI Logic"
        p4.description = "Optional AOI polygon used when Operation Mode is Add/Subtract/Replace."
        params.append(p4)

        p5 = arcpy.Parameter(
            displayName="Frame / Neatline Polygon (optional clip)",
            name="frame_polygon",
            datatype="GPFeatureLayer",
            parameterType="Optional",
            direction="Input"
        )
        p5.category = "3) Safety"
        p5.description = "Optional polygon used to clip the final AOI (keeps AOI inside the frame)."
        params.append(p5)

        p6 = arcpy.Parameter(
            displayName="Clip AOI to Frame?",
            name="clip_to_frame",
            datatype="GPBoolean",
            parameterType="Optional",
            direction="Input"
        )
        p6.category = "3) Safety"
        p6.value = True
        params.append(p6)

        p7 = arcpy.Parameter(
            displayName="Output Workspace (GDB recommended)",
            name="out_workspace",
            datatype="DEWorkspace",
            parameterType="Optional",
            direction="Input"
        )
        p7.category = "4) Outputs"
        p7.description = "Workspace for output AOI. If empty, scratch GDB is used."
        params.append(p7)

        p8 = arcpy.Parameter(
            displayName="Output AOI Name",
            name="out_aoi_name",
            datatype="GPString",
            parameterType="Required",
            direction="Input"
        )
        p8.category = "4) Outputs"
        p8.value = "AOI_Brush"
        params.append(p8)

        p9 = arcpy.Parameter(
            displayName="Add AOI output to current map (ArcMap)",
            name="add_to_map",
            datatype="GPBoolean",
            parameterType="Optional",
            direction="Input"
        )
        p9.category = "4) Outputs"
        p9.value = True
        params.append(p9)

        return params

    def isLicensed(self):
        return True

    def updateParameters(self, parameters):
        # Enable/disable existing AOI based on operation
        try:
            op = parameters[3].valueAsText
            need_existing = op in ("Add brush to existing AOI", "Subtract brush from existing AOI", "Replace existing AOI (overwrite with brush)")
            parameters[4].enabled = bool(need_existing)
        except:
            pass

    def updateMessages(self, parameters):
        try:
            op = parameters[3].valueAsText
            existing = parameters[4].valueAsText
            if op in ("Add brush to existing AOI", "Subtract brush from existing AOI", "Replace existing AOI (overwrite with brush)") and not existing:
                parameters[4].setWarningMessage("Operation requires an Existing AOI polygon. Provide it or switch to 'Create new AOI'.")
        except:
            pass

    def execute(self, parameters, messages):
        arcpy.env.overwriteOutput = True
        try:
            brush_fc = parameters[0].valueAsText
            brush_radius_mu = float(parameters[1].value or 0.0)
            brush_radius_mm = float(parameters[2].value or 0.0)
            operation = parameters[3].valueAsText
            existing_aoi = parameters[4].valueAsText
            frame_fc = parameters[5].valueAsText
            clip_to_frame = self._as_bool(parameters[6].value)
            out_ws = parameters[7].valueAsText or arcpy.env.scratchGDB
            out_name = parameters[8].valueAsText
            add_to_map = self._as_bool(parameters[9].value)

            if not brush_fc:
                raise arcpy.ExecuteError("Brush feature is required.")

            # Get spatial reference / units
            desc = arcpy.Describe(brush_fc)
            sr = desc.spatialReference
            map_units_name = getattr(sr, "linearUnitName", "") if sr else ""

            # Convert mm to map units if given
            if brush_radius_mm and brush_radius_mm > 0:
                scale = self._get_df_scale()
                conv = self._mm_to_mapunits(brush_radius_mm, scale, map_units_name)
                if conv is not None:
                    brush_radius_mu = max(brush_radius_mu, float(conv))
                    self._msg("Brush radius converted from mm using scale 1:{0:.0f} => {1:.3f} map units".format(scale, conv))
                else:
                    self._warn("Could not convert brush radius (mm). Using map-units radius only.")

            scratch = arcpy.env.scratchGDB

            # Make brush polygon
            brush_geom_type = (desc.shapeType or "").upper()
            brush_poly = arcpy.CreateUniqueName(self._unique("brush_poly"), scratch)

            if brush_geom_type in ("POLYLINE", "LINE", "POINT", "MULTIPOINT"):
                if brush_radius_mu <= 0:
                    raise arcpy.ExecuteError("Brush Radius must be > 0 for Polyline/Point brush strokes.")
                self._msg("Buffering brush strokes (radius = {0})...".format(brush_radius_mu))
                arcpy.analysis.Buffer(brush_fc, brush_poly, float(brush_radius_mu), dissolve_option="ALL")
            elif brush_geom_type == "POLYGON":
                # If polygon strokes, dissolve them (optional buffer not required)
                self._msg("Dissolving polygon brush strokes...")
                arcpy.management.Dissolve(brush_fc, brush_poly)
            else:
                raise arcpy.ExecuteError("Unsupported brush geometry type: {0}".format(brush_geom_type))

            # Clean brush polygon (multipart ok) => Dissolve again for safety
            brush_poly2 = arcpy.CreateUniqueName(self._unique("brush_poly_diss"), scratch)
            arcpy.management.Dissolve(brush_poly, brush_poly2)
            brush_poly = brush_poly2

            # Build final AOI polygon
            out_aoi_tmp = arcpy.CreateUniqueName(self._unique("aoi_tmp"), scratch)

            if operation == "Create new AOI (from brush)":
                arcpy.management.CopyFeatures(brush_poly, out_aoi_tmp)

            elif operation == "Replace existing AOI (overwrite with brush)":
                if not existing_aoi:
                    raise arcpy.ExecuteError("Existing AOI is required for Replace operation.")
                arcpy.management.CopyFeatures(brush_poly, out_aoi_tmp)

            elif operation == "Add brush to existing AOI":
                if not existing_aoi:
                    raise arcpy.ExecuteError("Existing AOI is required for Add operation.")
                # Union + dissolve
                union_fc = arcpy.CreateUniqueName(self._unique("aoi_union"), scratch)
                self._msg("Union: existing AOI + brush...")
                arcpy.analysis.Union([existing_aoi, brush_poly], union_fc, "ALL", "", "GAPS")
                arcpy.management.Dissolve(union_fc, out_aoi_tmp)

            elif operation == "Subtract brush from existing AOI":
                if not existing_aoi:
                    raise arcpy.ExecuteError("Existing AOI is required for Subtract operation.")
                # Erase existing minus brush (Advanced)
                prod = None
                try:
                    prod = arcpy.ProductInfo()
                except:
                    prod = None
                if prod != "ArcInfo":
                    self._warn("Advanced license not detected. Subtract uses Erase and may fail.")
                self._msg("Erase: existing AOI MINUS brush...")
                arcpy.analysis.Erase(existing_aoi, brush_poly, out_aoi_tmp)

            else:
                raise arcpy.ExecuteError("Unknown Operation Mode.")

            # Optional clip to frame
            if clip_to_frame and frame_fc:
                self._msg("Clipping AOI to frame...")
                out_aoi_clipped = arcpy.CreateUniqueName(self._unique("aoi_clip"), scratch)
                arcpy.analysis.Clip(out_aoi_tmp, frame_fc, out_aoi_clipped)
                out_aoi_tmp = out_aoi_clipped

            # Write final output
            out_aoi_fc = self._normalize_output_path(out_ws, out_name)
            if arcpy.Exists(out_aoi_fc):
                arcpy.management.Delete(out_aoi_fc)
            arcpy.management.CopyFeatures(out_aoi_tmp, out_aoi_fc)

            if add_to_map:
                self._add_layers_to_map([out_aoi_fc])

            self._msg("AOI created ✅  Output: {0}".format(out_aoi_fc))

        except Exception:
            self._err(traceback.format_exc())
            raise


# ======================================================================
# TOOL 2: Safe Contour Cleaner (Ultimate)
# ======================================================================
class SafeContourCleaner(object):

    # Parameter indexes (prevents off-by-one mistakes)
    IDX_IN_CONTOURS      = 0
    IDX_FRAME_POLY       = 1
    IDX_SAFE_MU          = 2
    IDX_SAFE_MM          = 3
    IDX_DENSE_TH         = 4
    IDX_MIN_NEIGHBORS    = 5
    IDX_AOI_MODE         = 6
    IDX_CUSTOM_AOI       = 7
    IDX_MASK_MODE        = 8
    IDX_EXTERNAL_MASK    = 9
    IDX_ELIGIBLE_SQL     = 10
    IDX_PROTECTED_SQL    = 11
    IDX_REMOVAL_METHOD   = 12
    IDX_MIN_SEG_LEN      = 13
    IDX_OUT_WS           = 14
    IDX_OUT_NAME         = 15
    IDX_CREATE_REMOVED   = 16
    IDX_CREATE_REVIEW    = 17
    IDX_CREATE_MASK_OUT  = 18
    IDX_WRITE_REPORT     = 19
    IDX_ADD_TO_MAP       = 20
    IDX_DRY_RUN          = 21

    def __init__(self):
        self.label = "Safe Contour Cleaner (Print-Ready)"
        self.description = (
            "Creates a cleaned copy of contour lines for cartographic output, WITHOUT editing the original data. "
            "Removes dense/overlapping contour segments safely (especially near the frame) to improve print readability."
        )
        self.canRunInBackground = False

    # -------------------------
    # Messaging helpers
    # -------------------------
    def _msg(self, s):
        try:
            arcpy.AddMessage(s)
        except:
            pass

    def _warn(self, s):
        try:
            arcpy.AddWarning(s)
        except:
            pass

    def _err(self, s):
        try:
            arcpy.AddError(s)
        except:
            pass

    # -------------------------
    # Small utility helpers
    # -------------------------
    def _unique(self, prefix="tmp"):
        return "{}_{}".format(prefix, uuid.uuid4().hex[:10])

    def _as_bool(self, v):
        if v is None:
            return False
        if isinstance(v, bool):
            return v
        try:
            s = unicode(v).strip().lower()
        except:
            s = str(v).strip().lower()
        return s in (u"true", u"1", u"yes", u"y", u"t")

    def _split_multivalue(self, mv_text):
        if not mv_text:
            return []
        parts = [p.strip() for p in mv_text.split(";") if p.strip()]
        return parts

    def _combine_where(self, a, b):
        a = (a or "").strip()
        b = (b or "").strip()
        if a and b:
            return u"({0}) AND ({1})".format(a, b)
        return a or b

    def _make_where_not(self, sql):
        sql = (sql or "").strip()
        if not sql:
            return ""
        return u"NOT ({0})".format(sql)

    # -------------------------
    # DataFrame scale helpers
    # -------------------------
    def _get_df_scale(self):
        """Return active DataFrame scale in ArcMap, if running inside ArcMap UI."""
        try:
            import arcpy.mapping as mapping
            mxd = mapping.MapDocument("CURRENT")
            df = mxd.activeDataFrame
            if df and df.scale:
                return float(df.scale)
        except:
            return None
        return None

    def _mm_to_mapunits(self, mm, scale, mapunit_name):
        """
        Convert millimeters-on-map to map units (best effort).
        Assumes: ground_distance_m = (mm / 1000) * scale
        """
        if mm is None or mm <= 0:
            return None
        if scale is None or scale <= 0:
            return None
        meters = (float(mm) / 1000.0) * float(scale)
        mu = (mapunit_name or "").lower()
        if "meter" in mu or mu in ("m", "meters"):
            return meters
        if "foot" in mu or "feet" in mu:
            return meters * 3.280839895
        if "kilometer" in mu or "kilometre" in mu or mu in ("km",):
            return meters / 1000.0
        return None

    # -------------------------
    # Geometry / FeatureClass helpers
    # -------------------------
    def _extent_to_polygon_fc(self, extent, out_fc, spatial_ref):
        """Create a single-polygon FC representing a rectangle extent."""
        arcpy.management.CreateFeatureclass(os.path.dirname(out_fc), os.path.basename(out_fc),
                                            "POLYGON", spatial_reference=spatial_ref)
        arr = arcpy.Array([
            arcpy.Point(extent.XMin, extent.YMin),
            arcpy.Point(extent.XMax, extent.YMin),
            arcpy.Point(extent.XMax, extent.YMax),
            arcpy.Point(extent.XMin, extent.YMax),
            arcpy.Point(extent.XMin, extent.YMin)
        ])
        poly = arcpy.Polygon(arr, spatial_ref)
        with arcpy.da.InsertCursor(out_fc, ["SHAPE@"]) as ic:
            ic.insertRow([poly])
        return out_fc

    def _clip_like(self, in_fc, clip_fc, out_fc):
        """Clip with fallback to SelectByLocation+CopyFeatures."""
        try:
            arcpy.analysis.Clip(in_fc, clip_fc, out_fc)
            return out_fc
        except:
            lyr = self._unique("lyr")
            arcpy.management.MakeFeatureLayer(in_fc, lyr)
            arcpy.management.SelectLayerByLocation(lyr, "INTERSECT", clip_fc)
            arcpy.management.CopyFeatures(lyr, out_fc)
            return out_fc

    def _safe_zone_from_frame(self, frame_fc, safe_margin, out_safe_zone_fc):
        """
        Build a "no-delete" polygon band INSIDE the frame:
        - Dissolve frame polygon
        - PolygonToLine => frame border
        - Buffer border line by safe_margin
        - Clip band with frame polygon => keep inside band only
        """
        dissolved = arcpy.CreateUniqueName(self._unique("frame_diss"), arcpy.env.scratchGDB)
        arcpy.management.Dissolve(frame_fc, dissolved)

        if safe_margin is None or safe_margin <= 0:
            arcpy.management.CopyFeatures(dissolved, out_safe_zone_fc)
            return out_safe_zone_fc

        frame_line = arcpy.CreateUniqueName(self._unique("frame_line"), arcpy.env.scratchGDB)
        arcpy.management.PolygonToLine(dissolved, frame_line)

        band = arcpy.CreateUniqueName(self._unique("frame_band"), arcpy.env.scratchGDB)
        try:
            arcpy.analysis.Buffer(frame_line, band, abs(float(safe_margin)), dissolve_option="ALL")
        except:
            self._warn("Buffer failed for safe zone; using full frame as safe zone.")
            arcpy.management.CopyFeatures(dissolved, out_safe_zone_fc)
            return out_safe_zone_fc

        try:
            arcpy.analysis.Clip(band, dissolved, out_safe_zone_fc)
        except:
            self._warn("Clip failed for safe zone; using full frame as safe zone.")
            arcpy.management.CopyFeatures(dissolved, out_safe_zone_fc)

        return out_safe_zone_fc

    def _build_dense_mask_near(self, eligible_fc, threshold, min_neighbors, out_mask_fc, aoi_fc=None):
        """
        Auto dense mask builder (fast + safe):
        - Optional clip to AOI for speed
        - GenerateNearTable with CLOSEST neighbors only (avoids huge tables)
        - Dense feature if it has >= min_neighbors neighbors within threshold
        - Buffer dense features and dissolve => mask polygon
        """
        if threshold is None or float(threshold) <= 0:
            raise Exception("Dense threshold must be > 0.")

        if min_neighbors is None or int(min_neighbors) < 1:
            min_neighbors = 1
        min_neighbors = int(min_neighbors)

        working_fc = eligible_fc
        if aoi_fc:
            tmp_clip = arcpy.CreateUniqueName(self._unique("eligible_clip"), arcpy.env.scratchGDB)
            working_fc = self._clip_like(eligible_fc, aoi_fc, tmp_clip)

        count = int(arcpy.management.GetCount(working_fc).getOutput(0))
        sr = arcpy.Describe(eligible_fc).spatialReference

        if count == 0:
            arcpy.management.CreateFeatureclass(os.path.dirname(out_mask_fc), os.path.basename(out_mask_fc),
                                                "POLYGON", spatial_reference=sr)
            return out_mask_fc

        near_tbl = arcpy.CreateUniqueName(self._unique("near"), arcpy.env.scratchGDB)

        arcpy.analysis.GenerateNearTable(working_fc, working_fc, near_tbl,
                                         search_radius=float(threshold),
                                         location="NO_LOCATION", angle="NO_ANGLE",
                                         closest="CLOSEST", closest_count=min_neighbors + 1)

        from collections import defaultdict
        neighbor_counts = defaultdict(int)

        with arcpy.da.SearchCursor(near_tbl, ["IN_FID", "NEAR_FID", "NEAR_DIST"]) as cur:
            for in_fid, near_fid, dist in cur:
                if in_fid == near_fid:
                    continue
                if dist is None:
                    continue
                if float(dist) <= float(threshold):
                    neighbor_counts[int(in_fid)] += 1

        dense_oids = [fid for fid, cnt in neighbor_counts.items() if cnt >= min_neighbors]

        if not dense_oids:
            arcpy.management.CreateFeatureclass(os.path.dirname(out_mask_fc), os.path.basename(out_mask_fc),
                                                "POLYGON", spatial_reference=sr)
            return out_mask_fc

        oid_field = arcpy.Describe(working_fc).OIDFieldName
        dense_layer = self._unique("dense_lyr")
        arcpy.management.MakeFeatureLayer(working_fc, dense_layer)

        chunks = [dense_oids[i:i+999] for i in range(0, len(dense_oids), 999)]
        arcpy.management.SelectLayerByAttribute(dense_layer, "CLEAR_SELECTION")

        first = True
        for ch in chunks:
            where = u"{0} IN ({1})".format(
                arcpy.AddFieldDelimiters(dense_layer, oid_field),
                u",".join([unicode(x) for x in ch])
            )
            arcpy.management.SelectLayerByAttribute(dense_layer,
                                                   "NEW_SELECTION" if first else "ADD_TO_SELECTION",
                                                   where)
            first = False

        tmp_buf = arcpy.CreateUniqueName(self._unique("dense_buf"), arcpy.env.scratchGDB)
        arcpy.analysis.Buffer(dense_layer, tmp_buf, float(threshold) / 2.0, dissolve_option="ALL")
        arcpy.management.CopyFeatures(tmp_buf, out_mask_fc)
        return out_mask_fc

    def _remove_small_segments(self, in_lines_fc, min_length, out_lines_fc):
        """Optional cleanup: remove segments shorter than min_length."""
        arcpy.management.CopyFeatures(in_lines_fc, out_lines_fc)

        if min_length is None or float(min_length) <= 0:
            return out_lines_fc

        oid_field = arcpy.Describe(out_lines_fc).OIDFieldName
        lyr = self._unique("short_lyr")
        arcpy.management.MakeFeatureLayer(out_lines_fc, lyr)

        short_oids = []
        with arcpy.da.SearchCursor(out_lines_fc, [oid_field, "SHAPE@"]) as cur:
            for oidv, geom in cur:
                if geom and geom.length < float(min_length):
                    short_oids.append(int(oidv))

        if not short_oids:
            return out_lines_fc

        chunks = [short_oids[i:i+999] for i in range(0, len(short_oids), 999)]
        arcpy.management.SelectLayerByAttribute(lyr, "CLEAR_SELECTION")

        first = True
        for ch in chunks:
            where = u"{0} IN ({1})".format(
                arcpy.AddFieldDelimiters(lyr, oid_field),
                u",".join([unicode(x) for x in ch])
            )
            arcpy.management.SelectLayerByAttribute(lyr,
                                                   "NEW_SELECTION" if first else "ADD_TO_SELECTION",
                                                   where)
            first = False

        arcpy.management.DeleteFeatures(lyr)
        return out_lines_fc

    def _normalize_output_path(self, out_ws, name):
        """Folder => shapefile, GDB => feature class name."""
        out_ws_low = (out_ws or "").lower()
        is_gdb = out_ws_low.endswith(".gdb") or (".gdb" in out_ws_low) or out_ws_low.endswith(".sde")
        if is_gdb:
            return os.path.join(out_ws, name)
        if not name.lower().endswith(".shp"):
            name = name + ".shp"
        return os.path.join(out_ws, name)

    def _add_layers_to_map(self, layer_paths):
        try:
            import arcpy.mapping as mapping
            mxd = mapping.MapDocument("CURRENT")
            df = mxd.activeDataFrame
            for p in layer_paths:
                if p and arcpy.Exists(p):
                    mapping.AddLayer(df, mapping.Layer(p), "TOP")
            self._msg("Outputs added to current map.")
        except:
            self._warn("Could not add outputs to current map. (Run inside ArcMap to enable this.)")

    def _report_csv(self, out_ws, base_name,
                    total_in, eligible_count, noneligible_count,
                    dense_th, min_neighbors, safe_mu,
                    aoi_mode, mask_mode, removal_method,
                    min_seg_length, removed_len,
                    out_clean_fc, out_removed_fc, out_review_fc, out_mask_fc):

        out_ws_low = (out_ws or "").lower()
        if ".gdb" in out_ws_low:
            parent = os.path.dirname(out_ws.rstrip("\\/"))
            report_path = os.path.join(parent, base_name + "_Report.csv")
        else:
            report_path = os.path.join(out_ws, base_name + "_Report.csv")

        with open(report_path, "wb") as f:
            w = csv.writer(f)
            w.writerow(["Tool", "Safe Contour Cleaner (Print-Ready) ULTIMATE"])
            w.writerow(["DateTime", str(datetime.datetime.now())])
            w.writerow([])
            w.writerow(["InputContoursCount", total_in])
            w.writerow(["EligibleCount", eligible_count])
            w.writerow(["ProtectedOrOtherCount", noneligible_count])
            w.writerow(["DenseThreshold", dense_th])
            w.writerow(["MinNeighbors", min_neighbors])
            w.writerow(["SafeMargin_MapUnits", safe_mu])
            w.writerow(["AOIMode", aoi_mode])
            w.writerow(["MaskMode", mask_mode])
            w.writerow(["RemovalMethod", removal_method])
            w.writerow(["MinSegmentLength", min_seg_length])
            w.writerow(["RemovedLengthApprox", removed_len])
            w.writerow([])
            w.writerow(["OutputCleanFC", out_clean_fc])
            w.writerow(["OutputRemovedFC", out_removed_fc or ""])
            w.writerow(["OutputReviewFC", out_review_fc or ""])
            w.writerow(["OutputMaskFC", out_mask_fc or ""])

        return report_path

    # -------------------------
    # GP Interface
    # -------------------------
    def getParameterInfo(self):
        params = []

        p0 = arcpy.Parameter(
            displayName="Input Contour Layers (one or more)",
            name="in_contours",
            datatype="GPFeatureLayer",
            parameterType="Required",
            direction="Input",
            multiValue=True
        )
        p0.category = "1) Inputs"
        p0.description = "One or more contour polyline layers/feature classes. Original data will NOT be modified."
        params.append(p0)

        p1 = arcpy.Parameter(
            displayName="Frame / Neatline Polygon (recommended)",
            name="frame_polygon",
            datatype="GPFeatureLayer",
            parameterType="Optional",
            direction="Input"
        )
        p1.category = "1) Inputs"
        p1.description = "Polygon representing the map frame/neatline. Used to protect the border area."
        params.append(p1)

        p2 = arcpy.Parameter(
            displayName="Safe Margin INSIDE Frame (map units)",
            name="safe_margin_mu",
            datatype="GPDouble",
            parameterType="Optional",
            direction="Input"
        )
        p2.category = "2) Safety"
        p2.value = 0.0
        p2.description = "Protected band INSIDE the frame. Any contour segments inside this band will NOT be removed."
        params.append(p2)

        p3 = arcpy.Parameter(
            displayName="Safe Margin INSIDE Frame (millimeters on map) [optional]",
            name="safe_margin_mm",
            datatype="GPDouble",
            parameterType="Optional",
            direction="Input"
        )
        p3.category = "2) Safety"
        p3.value = 0.0
        p3.description = "If >0, converts mm on map to map units using current ArcMap DataFrame scale."
        params.append(p3)

        p4 = arcpy.Parameter(
            displayName="Dense Threshold Distance (map units)",
            name="dense_threshold",
            datatype="GPDouble",
            parameterType="Required",
            direction="Input"
        )
        p4.category = "3) Dense Zone Detection"
        p4.value = 20.0
        p4.description = "Contours closer than this distance are considered dense."
        params.append(p4)

        p5 = arcpy.Parameter(
            displayName="Minimum Neighbors for Dense (>=1)",
            name="min_neighbors",
            datatype="GPLong",
            parameterType="Optional",
            direction="Input"
        )
        p5.category = "3) Dense Zone Detection"
        p5.value = 1
        p5.description = "Minimum neighbors within threshold to mark a contour as dense."
        params.append(p5)

        p6 = arcpy.Parameter(
            displayName="AOI Mode (Where to clean)",
            name="aoi_mode",
            datatype="GPString",
            parameterType="Required",
            direction="Input"
        )
        p6.category = "4) AOI / Target Area"
        p6.filter.type = "ValueList"
        p6.filter.list = [
            "Frame only (default)",
            "Custom AOI only",
            "Frame AND Custom AOI",
            "Entire dataset (no AOI)"
        ]
        p6.value = "Frame only (default)"
        p6.description = "Defines where cleaning is allowed to happen."
        params.append(p6)

        p7 = arcpy.Parameter(
            displayName="Custom AOI Polygon (optional)",
            name="custom_aoi",
            datatype="GPFeatureLayer",
            parameterType="Optional",
            direction="Input"
        )
        p7.category = "4) AOI / Target Area"
        p7.description = "Custom AOI polygon (you can create it with AOI Brush Builder)."
        params.append(p7)

        p8 = arcpy.Parameter(
            displayName="Mask Mode (How to create removal mask)",
            name="mask_mode",
            datatype="GPString",
            parameterType="Required",
            direction="Input"
        )
        p8.category = "5) Mask Generation"
        p8.filter.type = "ValueList"
        p8.filter.list = [
            "Auto mask from dense zones (recommended)",
            "Use AOI polygon as mask (no auto)",
            "Use external mask polygon"
        ]
        p8.value = "Auto mask from dense zones (recommended)"
        params.append(p8)

        p9 = arcpy.Parameter(
            displayName="External Mask Polygon (optional)",
            name="external_mask",
            datatype="GPFeatureLayer",
            parameterType="Optional",
            direction="Input"
        )
        p9.category = "5) Mask Generation"
        p9.description = "Used only if Mask Mode = 'Use external mask polygon'."
        params.append(p9)

        p10 = arcpy.Parameter(
            displayName="Eligible Contours SQL (CAN be cleaned)",
            name="eligible_sql",
            datatype="GPString",
            parameterType="Optional",
            direction="Input"
        )
        p10.category = "6) Rules"
        p10.value = "1=1"
        p10.description = "SQL to choose eligible contours. Example: CONTOURTYPE = 'MINOR'"
        params.append(p10)

        p11 = arcpy.Parameter(
            displayName="Protected Contours SQL (MUST NEVER be cleaned)",
            name="protected_sql",
            datatype="GPString",
            parameterType="Optional",
            direction="Input"
        )
        p11.category = "6) Rules"
        p11.value = ""
        p11.description = "SQL to protect contours. Example: MOD(CONTOUR, 100) = 0"
        params.append(p11)

        p12 = arcpy.Parameter(
            displayName="Removal Method",
            name="removal_method",
            datatype="GPString",
            parameterType="Required",
            direction="Input"
        )
        p12.category = "7) Cleaning Strategy"
        p12.filter.type = "ValueList"
        p12.filter.list = [
            "Segment Erase (recommended)",
            "Delete Whole Features"
        ]
        p12.value = "Segment Erase (recommended)"
        params.append(p12)

        p13 = arcpy.Parameter(
            displayName="Min segment length to delete after erase (optional, map units)",
            name="min_seg_length",
            datatype="GPDouble",
            parameterType="Optional",
            direction="Input"
        )
        p13.category = "7) Cleaning Strategy"
        p13.value = 0.0
        p13.description = "Optional: removes tiny leftover segments after erase."
        params.append(p13)

        p14 = arcpy.Parameter(
            displayName="Output Workspace (GDB recommended)",
            name="out_workspace",
            datatype="DEWorkspace",
            parameterType="Optional",
            direction="Input"
        )
        p14.category = "8) Outputs"
        p14.description = "Workspace to write outputs. If empty, scratch GDB is used."
        params.append(p14)

        p15 = arcpy.Parameter(
            displayName="Output Clean Contours Name",
            name="out_clean_name",
            datatype="GPString",
            parameterType="Required",
            direction="Input"
        )
        p15.category = "8) Outputs"
        p15.value = "Contours_CartoClean"
        params.append(p15)

        p16 = arcpy.Parameter(
            displayName="Create Removed Segments Layer",
            name="create_removed",
            datatype="GPBoolean",
            parameterType="Optional",
            direction="Input"
        )
        p16.category = "8) Outputs"
        p16.value = True
        params.append(p16)

        p17 = arcpy.Parameter(
            displayName="Create Review Layer (dense but protected)",
            name="create_review",
            datatype="GPBoolean",
            parameterType="Optional",
            direction="Input"
        )
        p17.category = "8) Outputs"
        p17.value = True
        params.append(p17)

        p18 = arcpy.Parameter(
            displayName="Create Mask Output Layer",
            name="create_mask_out",
            datatype="GPBoolean",
            parameterType="Optional",
            direction="Input"
        )
        p18.category = "8) Outputs"
        p18.value = True
        params.append(p18)

        p19 = arcpy.Parameter(
            displayName="Write CSV Report",
            name="write_report",
            datatype="GPBoolean",
            parameterType="Optional",
            direction="Input"
        )
        p19.category = "8) Outputs"
        p19.value = True
        params.append(p19)

        p20 = arcpy.Parameter(
            displayName="Add outputs to current map (ArcMap)",
            name="add_to_map",
            datatype="GPBoolean",
            parameterType="Optional",
            direction="Input"
        )
        p20.category = "8) Outputs"
        p20.value = True
        params.append(p20)

        p21 = arcpy.Parameter(
            displayName="Dry Run / Preview Mode (no cleaned output)",
            name="dry_run",
            datatype="GPBoolean",
            parameterType="Optional",
            direction="Input"
        )
        p21.category = "8) Outputs"
        p21.value = False
        p21.description = "If True, produces mask/removed/review only (useful for QA)."
        params.append(p21)

        return params

    def isLicensed(self):
        return True

    def updateParameters(self, parameters):
        """Enable/disable parameters depending on selections."""
        try:
            aoi_mode = parameters[self.IDX_AOI_MODE].valueAsText
            mask_mode = parameters[self.IDX_MASK_MODE].valueAsText

            # Custom AOI parameter
            if aoi_mode in ("Custom AOI only", "Frame AND Custom AOI"):
                parameters[self.IDX_CUSTOM_AOI].enabled = True
            else:
                parameters[self.IDX_CUSTOM_AOI].enabled = False

            # External mask parameter
            if mask_mode == "Use external mask polygon":
                parameters[self.IDX_EXTERNAL_MASK].enabled = True
            else:
                parameters[self.IDX_EXTERNAL_MASK].enabled = False
        except:
            pass

    def updateMessages(self, parameters):
        """User-friendly warnings."""
        try:
            aoi_mode = parameters[self.IDX_AOI_MODE].valueAsText
            frame = parameters[self.IDX_FRAME_POLY].valueAsText

            if "Frame" in (aoi_mode or "") and not frame:
                parameters[self.IDX_FRAME_POLY].setWarningMessage(
                    "AOI Mode uses Frame but Frame/Neatline polygon is not provided. "
                    "Tool will try to use active DataFrame extent, but Neatline polygon is recommended."
                )

            dense_th = parameters[self.IDX_DENSE_TH].value
            safe_mu = parameters[self.IDX_SAFE_MU].value or 0.0
            if dense_th and float(dense_th) < float(safe_mu):
                parameters[self.IDX_DENSE_TH].setWarningMessage(
                    "Dense threshold is smaller than safe margin. Cleaning near the frame may be very limited."
                )
        except:
            pass

    def execute(self, parameters, messages):
        arcpy.env.overwriteOutput = True

        try:
            # Read parameters
            in_contours_mv = parameters[self.IDX_IN_CONTOURS].valueAsText
            frame_fc = parameters[self.IDX_FRAME_POLY].valueAsText

            safe_mu = float(parameters[self.IDX_SAFE_MU].value or 0.0)
            safe_mm = float(parameters[self.IDX_SAFE_MM].value or 0.0)

            dense_threshold = float(parameters[self.IDX_DENSE_TH].value)
            min_neighbors = int(parameters[self.IDX_MIN_NEIGHBORS].value or 1)

            aoi_mode = parameters[self.IDX_AOI_MODE].valueAsText
            custom_aoi_fc = parameters[self.IDX_CUSTOM_AOI].valueAsText

            mask_mode = parameters[self.IDX_MASK_MODE].valueAsText
            external_mask_fc = parameters[self.IDX_EXTERNAL_MASK].valueAsText

            eligible_sql = parameters[self.IDX_ELIGIBLE_SQL].valueAsText or "1=1"
            protected_sql = parameters[self.IDX_PROTECTED_SQL].valueAsText or ""

            removal_method = parameters[self.IDX_REMOVAL_METHOD].valueAsText
            min_seg_length = float(parameters[self.IDX_MIN_SEG_LEN].value or 0.0)

            out_ws = parameters[self.IDX_OUT_WS].valueAsText or arcpy.env.scratchGDB
            out_clean_name = parameters[self.IDX_OUT_NAME].valueAsText

            create_removed = self._as_bool(parameters[self.IDX_CREATE_REMOVED].value)
            create_review  = self._as_bool(parameters[self.IDX_CREATE_REVIEW].value)
            create_maskout = self._as_bool(parameters[self.IDX_CREATE_MASK_OUT].value)

            write_report = self._as_bool(parameters[self.IDX_WRITE_REPORT].value)
            add_to_map   = self._as_bool(parameters[self.IDX_ADD_TO_MAP].value)
            dry_run      = self._as_bool(parameters[self.IDX_DRY_RUN].value)

            # Validate input contours
            in_contours = self._split_multivalue(in_contours_mv)
            if not in_contours:
                raise arcpy.ExecuteError("No input contours provided.")

            desc0 = arcpy.Describe(in_contours[0])
            sr = desc0.spatialReference
            map_units_name = getattr(sr, "linearUnitName", "") if sr else ""

            if sr and hasattr(sr, "type") and sr.type == "Geographic":
                self._warn("Geographic coordinate system detected. Distances may be inaccurate. Consider projecting to a projected CRS.")

            # Convert safe mm => map units (if possible)
            if safe_mm and safe_mm > 0:
                scale = self._get_df_scale()
                safe_conv = self._mm_to_mapunits(safe_mm, scale, map_units_name)
                if safe_conv is not None:
                    self._msg("Safe Margin converted from {0} mm @ 1:{1:.0f} => {2:.3f} map units".format(safe_mm, scale, safe_conv))
                    safe_mu = max(safe_mu, safe_conv)
                else:
                    self._warn("Could not convert Safe Margin (mm). Please use Safe Margin in map units instead.")

            scratch = arcpy.env.scratchGDB

            # Frame fallback: use DataFrame extent
            if (not frame_fc) and ("Frame" in (aoi_mode or "")):
                try:
                    import arcpy.mapping as mapping
                    mxd = mapping.MapDocument("CURRENT")
                    df = mxd.activeDataFrame
                    ext = df.extent
                    frame_fc = arcpy.CreateUniqueName(self._unique("frame_extent"), scratch)
                    self._extent_to_polygon_fc(ext, frame_fc, sr)
                    self._warn("Frame polygon not provided. Using active DataFrame extent as frame.")
                except:
                    raise arcpy.ExecuteError("Frame polygon is required OR run the tool inside ArcMap with an active DataFrame.")

            # Build Safe Zone (no-delete band) inside frame
            safe_zone_fc = None
            if frame_fc:
                safe_zone_fc = arcpy.CreateUniqueName(self._unique("safe_zone"), scratch)
                safe_zone_fc = self._safe_zone_from_frame(frame_fc, safe_mu, safe_zone_fc)

            # Compute AOI polygon by mode
            aoi_fc = None
            if aoi_mode == "Frame only (default)":
                aoi_fc = frame_fc
            elif aoi_mode == "Custom AOI only":
                if not custom_aoi_fc:
                    raise arcpy.ExecuteError("Custom AOI mode selected but Custom AOI polygon not provided.")
                aoi_fc = custom_aoi_fc
            elif aoi_mode == "Frame AND Custom AOI":
                if not (frame_fc and custom_aoi_fc):
                    raise arcpy.ExecuteError("Frame AND Custom AOI requires BOTH Frame polygon and Custom AOI polygon.")
                aoi_fc = arcpy.CreateUniqueName(self._unique("aoi_int"), scratch)
                arcpy.analysis.Intersect([frame_fc, custom_aoi_fc], aoi_fc, join_attributes="ONLY_FID")
            else:
                aoi_fc = None  # entire dataset

            # Merge all contour inputs (processing copy)
            merged_fc = arcpy.CreateUniqueName(self._unique("contours_merge"), scratch)
            self._msg("Merging input contours...")
            arcpy.management.Merge(in_contours, merged_fc)

            # Clip to AOI for print-ready scope (optional)
            working_all = merged_fc
            if aoi_fc:
                clipped_all = arcpy.CreateUniqueName(self._unique("contours_clip"), scratch)
                self._msg("Clipping contours to AOI...")
                working_all = self._clip_like(merged_fc, aoi_fc, clipped_all)

            total_in = int(arcpy.management.GetCount(working_all).getOutput(0))
            self._msg("Contours in AOI: {0}".format(total_in))

            # Eligible = eligible_sql AND NOT protected_sql
            not_prot = self._make_where_not(protected_sql)
            where_eligible = self._combine_where(eligible_sql, not_prot)

            # Split eligible / non-eligible
            lyr_all = self._unique("all_lyr")
            arcpy.management.MakeFeatureLayer(working_all, lyr_all)

            arcpy.management.SelectLayerByAttribute(lyr_all, "NEW_SELECTION", where_eligible)
            eligible_fc = arcpy.CreateUniqueName(self._unique("eligible"), scratch)
            arcpy.management.CopyFeatures(lyr_all, eligible_fc)

            arcpy.management.SelectLayerByAttribute(lyr_all, "SWITCH_SELECTION")
            noneligible_fc = arcpy.CreateUniqueName(self._unique("noneligible"), scratch)
            arcpy.management.CopyFeatures(lyr_all, noneligible_fc)

            eligible_count = int(arcpy.management.GetCount(eligible_fc).getOutput(0))
            noneligible_count = int(arcpy.management.GetCount(noneligible_fc).getOutput(0))
            self._msg("Eligible: {0} | Protected/Other: {1}".format(eligible_count, noneligible_count))

            # Build removal mask
            mask_fc = arcpy.CreateUniqueName(self._unique("mask"), scratch)

            if mask_mode == "Use external mask polygon":
                if not external_mask_fc:
                    raise arcpy.ExecuteError("Mask Mode is 'Use external mask polygon' but no external mask was provided.")
                arcpy.management.CopyFeatures(external_mask_fc, mask_fc)

            elif mask_mode == "Use AOI polygon as mask (no auto)":
                if not aoi_fc:
                    raise arcpy.ExecuteError("AOI is required to use AOI as mask (choose a Frame or Custom AOI mode).")
                arcpy.management.CopyFeatures(aoi_fc, mask_fc)

            else:
                self._msg("Building dense-zone mask (Near Table method)...")
                self._build_dense_mask_near(eligible_fc, dense_threshold, min_neighbors, mask_fc, aoi_fc=aoi_fc)

            mask_count = int(arcpy.management.GetCount(mask_fc).getOutput(0))
            if mask_count == 0:
                self._warn("Mask is empty. No contour segments will be removed (output becomes copy).")

            # Apply frame safety: final_mask = mask - safe_zone
            final_mask_fc = mask_fc
            if safe_zone_fc and mask_count > 0 and int(arcpy.management.GetCount(safe_zone_fc).getOutput(0)) > 0:
                final_mask_fc = arcpy.CreateUniqueName(self._unique("mask_safe"), scratch)
                try:
                    arcpy.analysis.Erase(mask_fc, safe_zone_fc, final_mask_fc)
                except:
                    self._warn("Erase failed while subtracting Safe Zone from mask. Using raw mask (be careful).")
                    final_mask_fc = mask_fc

            final_mask_count = int(arcpy.management.GetCount(final_mask_fc).getOutput(0))
            if final_mask_count == 0 and mask_count > 0:
                self._warn("After frame safety, final mask is empty (all dense zones were within Safe Margin).")

            # Review layer: dense zones that were inside the safe zone (for manual QA)
            review_fc = None
            if create_review and safe_zone_fc and mask_count > 0:
                review_fc = arcpy.CreateUniqueName(self._unique("review"), scratch)
                try:
                    arcpy.analysis.Intersect([mask_fc, safe_zone_fc], review_fc, output_type="POLYGON")
                except:
                    review_fc = None

            # Output mask if requested (FINAL mask)
            out_mask_fc = None
            if create_maskout and mask_count > 0:
                out_mask_fc = self._normalize_output_path(out_ws, out_clean_name + "_Mask")
                if arcpy.Exists(out_mask_fc):
                    arcpy.management.Delete(out_mask_fc)
                arcpy.management.CopyFeatures(final_mask_fc, out_mask_fc)

            # Dry run
            if dry_run:
                out_removed_fc = None
                if create_removed and final_mask_count > 0:
                    tmp_removed = arcpy.CreateUniqueName(self._unique("removed_preview"), scratch)
                    arcpy.analysis.Intersect([eligible_fc, final_mask_fc], tmp_removed, output_type="LINE")
                    out_removed_fc = self._normalize_output_path(out_ws, out_clean_name + "_Removed_Preview")
                    if arcpy.Exists(out_removed_fc):
                        arcpy.management.Delete(out_removed_fc)
                    arcpy.management.CopyFeatures(tmp_removed, out_removed_fc)

                out_review_fc = None
                if review_fc and arcpy.Exists(review_fc):
                    out_review_fc = self._normalize_output_path(out_ws, out_clean_name + "_Review")
                    if arcpy.Exists(out_review_fc):
                        arcpy.management.Delete(out_review_fc)
                    arcpy.management.CopyFeatures(review_fc, out_review_fc)

                if add_to_map:
                    self._add_layers_to_map([out_mask_fc, out_removed_fc, out_review_fc])

                self._msg("Dry Run complete ✅ (Mask/Preview layers created).")
                return

            # License check for Erase (Segment mode)
            if removal_method == "Segment Erase (recommended)":
                try:
                    prod = arcpy.ProductInfo()  # "ArcInfo" for Advanced
                except:
                    prod = None
                if prod != "ArcInfo":
                    self._warn("Advanced license not detected (Erase may fail). Falling back to 'Delete Whole Features'.")
                    removal_method = "Delete Whole Features"

            # Perform cleaning
            cleaned_eligible_fc = arcpy.CreateUniqueName(self._unique("eligible_clean"), scratch)
            removed_fc = None

            # If final mask empty => copy eligible
            if final_mask_count == 0:
                self._msg("Final mask has no features. Skipping removal.")
                arcpy.management.CopyFeatures(eligible_fc, cleaned_eligible_fc)

            else:
                if removal_method == "Segment Erase (recommended)":
                    self._msg("Cleaning: Segment Erase...")
                    arcpy.analysis.Erase(eligible_fc, final_mask_fc, cleaned_eligible_fc)

                    if create_removed:
                        removed_fc = arcpy.CreateUniqueName(self._unique("removed"), scratch)
                        arcpy.analysis.Intersect([eligible_fc, final_mask_fc], removed_fc, output_type="LINE")

                else:
                    self._msg("Cleaning: Delete Whole Features...")
                    lyr_elig = self._unique("eligible_lyr")
                    arcpy.management.MakeFeatureLayer(eligible_fc, lyr_elig)
                    arcpy.management.SelectLayerByLocation(lyr_elig, "INTERSECT", final_mask_fc)

                    if create_removed:
                        removed_fc = arcpy.CreateUniqueName(self._unique("removed"), scratch)
                        arcpy.management.CopyFeatures(lyr_elig, removed_fc)

                    arcpy.management.SelectLayerByAttribute(lyr_elig, "SWITCH_SELECTION")
                    arcpy.management.CopyFeatures(lyr_elig, cleaned_eligible_fc)

            # Optional: remove tiny leftover segments
            if min_seg_length and min_seg_length > 0:
                self._msg("Removing tiny leftover segments < {0} ...".format(min_seg_length))
                cleaned2 = arcpy.CreateUniqueName(self._unique("eligible_clean2"), scratch)
                self._remove_small_segments(cleaned_eligible_fc, min_seg_length, cleaned2)
                cleaned_eligible_fc = cleaned2

            # Merge cleaned eligible + untouched protected contours
            merged_out = arcpy.CreateUniqueName(self._unique("merged_out"), scratch)
            self._msg("Merging cleaned eligible + untouched protected contours...")
            arcpy.management.Merge([cleaned_eligible_fc, noneligible_fc], merged_out)

            # Write final output FC
            out_clean_fc = self._normalize_output_path(out_ws, out_clean_name)
            if arcpy.Exists(out_clean_fc):
                arcpy.management.Delete(out_clean_fc)
            arcpy.management.CopyFeatures(merged_out, out_clean_fc)

            # Optional outputs
            out_removed_fc = None
            if create_removed and removed_fc and arcpy.Exists(removed_fc):
                out_removed_fc = self._normalize_output_path(out_ws, out_clean_name + "_Removed")
                if arcpy.Exists(out_removed_fc):
                    arcpy.management.Delete(out_removed_fc)
                arcpy.management.CopyFeatures(removed_fc, out_removed_fc)

            out_review_fc = None
            if create_review and review_fc and arcpy.Exists(review_fc):
                out_review_fc = self._normalize_output_path(out_ws, out_clean_name + "_Review")
                if arcpy.Exists(out_review_fc):
                    arcpy.management.Delete(out_review_fc)
                arcpy.management.CopyFeatures(review_fc, out_review_fc)

            # Report CSV (approx removed length is not computed here to keep it lightweight)
            report_path = None
            if write_report:
                try:
                    report_path = self._report_csv(
                        out_ws, out_clean_name,
                        total_in, eligible_count, noneligible_count,
                        dense_threshold, min_neighbors, safe_mu,
                        aoi_mode, mask_mode, removal_method,
                        min_seg_length, 0.0,
                        out_clean_fc, out_removed_fc, out_review_fc, out_mask_fc
                    )
                except:
                    self._warn("Failed to write CSV report.")

            # Add to map
            if add_to_map:
                self._add_layers_to_map([out_clean_fc, out_removed_fc, out_review_fc, out_mask_fc])

            self._msg("Done ✅  Output: {0}".format(out_clean_fc))
            if report_path:
                self._msg("Report: {0}".format(report_path))

        except Exception:
            self._err(traceback.format_exc())
            raise
