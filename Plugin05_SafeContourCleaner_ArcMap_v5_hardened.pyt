# -*- coding: utf-8 -*-
"""
Plugin 05 - Safe Contour Cleaner (ArcMap / Python 2.7)  v5 HARDENED (MASTER RULES)
==================================================================================
Toolbox containing TWO tools:

  Tool 1: AOI Brush Builder
    Build an AOI polygon from "brush stroke" inputs (polylines or
    polygons) using buffer + union/erase, optionally clipped to a
    frame/neatline polygon.

  Tool 2: Safe Contour Cleaner
    Build a cleaned COPY of contour line layers for cartographic
    output, leaving the originals untouched. Removes dense /
    overlapping contour segments inside an AOI while protecting a
    safety band inside the frame border.

Hardened in v5 (MASTER RULES revision):
  * EXCEPTION HANDLING: narrow except blocks at GP calls
    (arcpy.ExecuteError, RuntimeError). MemoryError / OSError
    propagate and crash loudly.
  * RAM MANAGEMENT: no big geometry caches in dicts; cursors stream.
  * SELECTION HYGIENE: _resolve_full_source preserved, ignore_selection
    is the default behavior.
  * GP ENVIRONMENT: arcpy.env.extent / mask / outputCoordinateSystem /
    workspace / scratchWorkspace snapshotted at execute() entry, set
    to None, restored in finally.
  * IN_MEMORY HYGIENE: every intermediate gets explicit
    arcpy.Delete_management in finally; final
    arcpy.Delete_management("in_memory") flush at end of execute().
  * SINGLE-CLIP STRATEGY: AOI is dissolved into one multipart polygon
    and a SINGLE arcpy.analysis.Clip is run on the whole dataset
    (no per-feature Clip loop).
  * AOI OPTIONALITY: parameter `allow_full_map_processing` (default
    False). If AOI is empty and this is False -> error. If True ->
    fall back to active map's extent and process the full map with
    a loud warning.
  * SLIVER REMOVAL: after clip, remove polygons/segments smaller than
    the dataset XYTolerance.

Author: Ali Mirjafari + Kiro
Version: 5.1 (ArcMap / Python 2.7) - Master Rules
"""

from __future__ import division

import arcpy
import os
import csv
import uuid
import traceback
import datetime
import gc

# =============================================================================
# 0. Compatibility / messaging
# =============================================================================

def _to_unicode(v):
    """Best-effort unicode for ArcMap (Py2.7) without crashing."""
    if v is None:
        return u""
    try:
        if isinstance(v, unicode):  # noqa: F821 (Py2)
            return v
    except NameError:
        # Py3 fallback (should not occur in ArcMap, but harmless)
        if isinstance(v, str):
            return v
    try:
        return unicode(v, "utf-8")  # noqa: F821
    except (UnicodeDecodeError, TypeError):
        try:
            return unicode(v, "cp1256")  # noqa: F821
        except (UnicodeDecodeError, TypeError):
            try:
                return unicode(str(v), "utf-8", "ignore")  # noqa: F821
            except (UnicodeDecodeError, TypeError):
                return u""

def _ascii_safe(u):
    uu = _to_unicode(u)
    try:
        return uu.encode("ascii", "replace")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return "?"

def _msg(s):
    try:
        arcpy.AddMessage(_ascii_safe(s))
    except (RuntimeError, arcpy.ExecuteError):
        pass

def _warn(s):
    try:
        arcpy.AddWarning(_ascii_safe(s))
    except (RuntimeError, arcpy.ExecuteError):
        pass

def _err(s):
    try:
        arcpy.AddError(_ascii_safe(s))
    except (RuntimeError, arcpy.ExecuteError):
        pass

def _diag(s):
    _msg(u"[DIAG] " + _to_unicode(s))

def _is_empty(v):
    if v is None:
        return True
    s = _to_unicode(v).strip()
    return s == u"" or s == u"#"

def _as_bool(v, default=False):
    if v is None:
        return default
    if isinstance(v, bool):
        return v
    try:
        s = _to_unicode(v).strip().lower()
    except (UnicodeError, TypeError):
        s = u""
    if s in (u"true", u"1", u"yes", u"y", u"t", u"on"):
        return True
    if s in (u"false", u"0", u"no", u"n", u"f", u"off"):
        return False
    return default

def _unique(prefix="tmp"):
    return "{0}_{1}".format(prefix, uuid.uuid4().hex[:10])

# =============================================================================
# 1. GP environment snapshot / restore (MASTER RULE 4)
# =============================================================================

def _snapshot_gp_env():
    """Capture critical arcpy.env values at execute() entry."""
    snap = {
        "extent":                 arcpy.env.extent,
        "mask":                   arcpy.env.mask,
        "outputCoordinateSystem": arcpy.env.outputCoordinateSystem,
        "workspace":              arcpy.env.workspace,
        "scratchWorkspace":       arcpy.env.scratchWorkspace,
        "overwriteOutput":        arcpy.env.overwriteOutput,
    }
    # Neutralize for clean GP execution
    arcpy.env.extent = None
    arcpy.env.mask = None
    arcpy.env.outputCoordinateSystem = None
    # workspace / scratchWorkspace intentionally left as-is here; tools below
    # use scratchGDB explicitly. We snapshot them so they can be restored if a
    # downstream call mutated them.
    return snap

def _restore_gp_env(snap):
    if not snap:
        return
    try:
        arcpy.env.extent = snap.get("extent")
    except (RuntimeError, arcpy.ExecuteError):
        pass
    try:
        arcpy.env.mask = snap.get("mask")
    except (RuntimeError, arcpy.ExecuteError):
        pass
    try:
        arcpy.env.outputCoordinateSystem = snap.get("outputCoordinateSystem")
    except (RuntimeError, arcpy.ExecuteError):
        pass
    try:
        arcpy.env.workspace = snap.get("workspace")
    except (RuntimeError, arcpy.ExecuteError):
        pass
    try:
        arcpy.env.scratchWorkspace = snap.get("scratchWorkspace")
    except (RuntimeError, arcpy.ExecuteError):
        pass
    try:
        arcpy.env.overwriteOutput = snap.get("overwriteOutput", True)
    except (RuntimeError, arcpy.ExecuteError):
        pass

# =============================================================================
# 2. Selection-bypass: resolve any layer to its on-disk source
# =============================================================================

def _selection_info(layer_or_path):
    """Return (selected_count, total_count, name)."""
    try:
        d = arcpy.Describe(layer_or_path)
    except (RuntimeError, IOError, arcpy.ExecuteError):
        return (None, None, _to_unicode(layer_or_path))
    name = getattr(d, "name", _to_unicode(layer_or_path))
    fidset = getattr(d, "FIDSet", "") or ""
    total = None
    try:
        total = int(arcpy.GetCount_management(layer_or_path).getOutput(0))
    except (RuntimeError, arcpy.ExecuteError, ValueError):
        total = None
    if fidset.strip() == "":
        return (0, total, name)
    sel_count = len([t for t in fidset.split(";") if t.strip() != ""])
    return (sel_count, total, name)

def _resolve_full_source(layer_or_path, ignore_selection=True):
    """
    Return on-disk catalogPath for a layer; pass-through if already a path.
    Selection on the source layer is IGNORED by default - the returned path is
    the full dataset.
    """
    if not layer_or_path:
        return layer_or_path
    try:
        d = arcpy.Describe(layer_or_path)
        cp = getattr(d, "catalogPath", None)
        if cp:
            return cp
    except (RuntimeError, IOError, arcpy.ExecuteError):
        pass
    return layer_or_path

def _announce_selection(label, layer_or_path):
    sel, total, name = _selection_info(layer_or_path)
    if sel and sel > 0:
        _warn(u"{lbl}: '{n}' has an active selection ({s} of {t}). Ignoring selection - processing FULL dataset.".format(
            lbl=label, n=name, s=sel,
            t=(total if total is not None else u"?")))
    else:
        _diag(u"{lbl}: '{n}' total={t}, no active selection.".format(
            lbl=label, n=name,
            t=(total if total is not None else u"?")))

# =============================================================================
# 3. Path / workspace helpers
# =============================================================================

def _get_count(fc):
    try:
        return int(arcpy.management.GetCount(fc).getOutput(0))
    except (RuntimeError, arcpy.ExecuteError, ValueError):
        return 0

def _scratch_unique(prefix):
    return arcpy.CreateUniqueName(_unique(prefix), arcpy.env.scratchGDB)

def _normalize_output_path(out_ws, name):
    """Folder => shapefile, GDB / SDE => feature class name."""
    out_ws_low = (_to_unicode(out_ws) or u"").lower()
    is_gdb = (out_ws_low.endswith(u".gdb")
              or u".gdb" in out_ws_low
              or out_ws_low.endswith(u".sde"))
    if is_gdb:
        return os.path.join(out_ws, name)
    if not name.lower().endswith(".shp"):
        name = name + ".shp"
    return os.path.join(out_ws, name)

def _safe_delete(path):
    """Best-effort delete; narrow exception handling."""
    if not path:
        return
    try:
        if arcpy.Exists(path):
            arcpy.management.Delete(path)
    except (arcpy.ExecuteError, RuntimeError):
        pass

def _ensure_scratch():
    sgdb = arcpy.env.scratchGDB
    if not sgdb or not arcpy.Exists(sgdb):
        sgdb = arcpy.env.scratchWorkspace
    if not sgdb or not arcpy.Exists(sgdb):
        raise arcpy.ExecuteError(
            u"No scratch GDB available. Set arcpy.env.scratchGDB.")
    return sgdb

def _flush_in_memory():
    """Final scratch flush. MASTER RULE 6."""
    try:
        arcpy.management.Delete("in_memory")
    except (arcpy.ExecuteError, RuntimeError):
        pass

# =============================================================================
# 4. ArcMap dataframe helpers (mm -> map units, extent fallback, add layers)
# =============================================================================

def _get_df_scale():
    """Return active DataFrame scale in ArcMap if running inside the UI."""
    try:
        import arcpy.mapping as mapping
        mxd = mapping.MapDocument("CURRENT")
        df = mxd.activeDataFrame
        if df and df.scale:
            return float(df.scale)
    except (RuntimeError, AttributeError, arcpy.ExecuteError):
        return None
    return None

def _get_df_extent_polygon(out_fc, sr):
    """Return on-disk polygon FC matching the active DataFrame extent."""
    import arcpy.mapping as mapping
    mxd = mapping.MapDocument("CURRENT")
    df = mxd.activeDataFrame
    ext = df.extent
    arcpy.management.CreateFeatureclass(
        os.path.dirname(out_fc), os.path.basename(out_fc),
        "POLYGON", spatial_reference=sr)
    arr = arcpy.Array([
        arcpy.Point(ext.XMin, ext.YMin),
        arcpy.Point(ext.XMax, ext.YMin),
        arcpy.Point(ext.XMax, ext.YMax),
        arcpy.Point(ext.XMin, ext.YMax),
        arcpy.Point(ext.XMin, ext.YMin),
    ])
    poly = arcpy.Polygon(arr, sr)
    with arcpy.da.InsertCursor(out_fc, ["SHAPE@"]) as ic:
        ic.insertRow([poly])
    return out_fc

def _mm_to_mapunits(mm, scale, mapunit_name):
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

def _add_layers_to_map(layer_paths):
    try:
        import arcpy.mapping as mapping
        mxd = mapping.MapDocument("CURRENT")
        df = mxd.activeDataFrame
        for p in layer_paths:
            if p and arcpy.Exists(p):
                try:
                    mapping.AddLayer(df, mapping.Layer(p), "TOP")
                except (RuntimeError, arcpy.ExecuteError):
                    _warn(u"Could not add layer to map: {0}".format(p))
        try:
            arcpy.RefreshActiveView()
            arcpy.RefreshTOC()
        except (RuntimeError, arcpy.ExecuteError):
            pass
    except (RuntimeError, AttributeError, arcpy.ExecuteError):
        _warn(u"Run inside ArcMap to enable 'Add to map'.")

def _get_xy_tolerance(fc, fallback=0.001):
    """Return XYTolerance for the dataset's spatial reference."""
    try:
        sr = arcpy.Describe(fc).spatialReference
        if sr is not None:
            tol = getattr(sr, "XYTolerance", None)
            if tol and float(tol) > 0:
                return float(tol)
    except (RuntimeError, AttributeError, arcpy.ExecuteError):
        pass
    return float(fallback)



# =============================================================================
# 5. Toolbox + Tool 1 (AOI Brush Builder)
# =============================================================================

class Toolbox(object):
    def __init__(self):
        self.label = u"Plugin 5 - Cartographic Automation (ArcMap, v5 hardened)"
        self.alias = "carto_auto_arcmap_v5"
        self.tools = [AOIBrushBuilder, SafeContourCleaner]


class AOIBrushBuilder(object):
    def __init__(self):
        self.label = u"AOI Brush Builder (Create / Add / Subtract) - v5"
        self.description = (
            u"Build an AOI polygon from polyline/polygon brush strokes "
            u"using buffer + union/erase. Optionally clip to a frame.\n\n"
            u"v5 master-rules: selection-bypass hardwired, scratchGDB-resident "
            u"intermediates, narrow exceptions, in_memory hygiene, env "
            u"snapshot/restore."
        )
        self.canRunInBackground = True

    def isLicensed(self):
        return True

    def getParameterInfo(self):
        params = []

        p0 = arcpy.Parameter(
            displayName=u"Brush Strokes Feature (Polyline or Polygon)",
            name="brush_fc", datatype="GPFeatureLayer",
            parameterType="Required", direction="Input")
        p0.category = "1) Brush Inputs"
        params.append(p0)

        p1 = arcpy.Parameter(
            displayName=u"Brush Radius (map units) [used for Polyline/Point strokes]",
            name="brush_radius_mu", datatype="GPDouble",
            parameterType="Optional", direction="Input")
        p1.category = "1) Brush Inputs"; p1.value = 20.0
        params.append(p1)

        p2 = arcpy.Parameter(
            displayName=u"Brush Radius (millimeters on map) [optional override]",
            name="brush_radius_mm", datatype="GPDouble",
            parameterType="Optional", direction="Input")
        p2.category = "1) Brush Inputs"; p2.value = 0.0
        p2.description = (
            u"If >0, converts mm on map to map units using the current "
            u"ArcMap DataFrame scale. Final radius = "
            u"max(map_units_value, converted_mm_value).")
        params.append(p2)

        p3 = arcpy.Parameter(
            displayName=u"Operation Mode", name="operation",
            datatype="GPString", parameterType="Required", direction="Input")
        p3.category = "2) AOI Logic"
        p3.filter.type = "ValueList"
        p3.filter.list = [
            "Create new AOI (from brush)",
            "Add brush to existing AOI",
            "Subtract brush from existing AOI",
            "Replace existing AOI (overwrite with brush)",
        ]
        p3.value = "Create new AOI (from brush)"
        params.append(p3)

        p4 = arcpy.Parameter(
            displayName=u"Existing AOI Polygon (optional)",
            name="existing_aoi", datatype="GPFeatureLayer",
            parameterType="Optional", direction="Input")
        p4.category = "2) AOI Logic"
        params.append(p4)

        p5 = arcpy.Parameter(
            displayName=u"Frame / Neatline Polygon (optional clip)",
            name="frame_polygon", datatype="GPFeatureLayer",
            parameterType="Optional", direction="Input")
        p5.category = "3) Safety"
        params.append(p5)

        p6 = arcpy.Parameter(
            displayName=u"Clip AOI to Frame?", name="clip_to_frame",
            datatype="GPBoolean", parameterType="Optional", direction="Input")
        p6.category = "3) Safety"; p6.value = True
        params.append(p6)

        p7 = arcpy.Parameter(
            displayName=u"Output Workspace (GDB recommended)",
            name="out_workspace", datatype="DEWorkspace",
            parameterType="Optional", direction="Input")
        p7.category = "4) Outputs"
        params.append(p7)

        p8 = arcpy.Parameter(
            displayName=u"Output AOI Name", name="out_aoi_name",
            datatype="GPString", parameterType="Required", direction="Input")
        p8.category = "4) Outputs"; p8.value = "AOI_Brush"
        params.append(p8)

        p9 = arcpy.Parameter(
            displayName=u"Add AOI output to current map (ArcMap)",
            name="add_to_map", datatype="GPBoolean",
            parameterType="Optional", direction="Input")
        p9.category = "4) Outputs"; p9.value = True
        params.append(p9)

        return params

    def updateParameters(self, parameters):
        try:
            op = parameters[3].valueAsText
            need_existing = op in (
                "Add brush to existing AOI",
                "Subtract brush from existing AOI",
                "Replace existing AOI (overwrite with brush)",
            )
            parameters[4].enabled = bool(need_existing)
        except (RuntimeError, AttributeError, arcpy.ExecuteError):
            pass

    def updateMessages(self, parameters):
        try:
            op = parameters[3].valueAsText
            existing = parameters[4].valueAsText
            if op in (
                "Add brush to existing AOI",
                "Subtract brush from existing AOI",
                "Replace existing AOI (overwrite with brush)",
            ) and not existing:
                parameters[4].setWarningMessage(
                    u"Operation requires an Existing AOI polygon. Provide it "
                    u"or switch to 'Create new AOI'.")
        except (RuntimeError, AttributeError, arcpy.ExecuteError):
            pass

    def execute(self, parameters, messages):
        # MASTER RULE 4: snapshot env, neutralize, restore in finally.
        env_snap = _snapshot_gp_env()
        arcpy.env.overwriteOutput = True
        try:
            arcpy.env.parallelProcessingFactor = "100%"
        except (RuntimeError, arcpy.ExecuteError):
            pass

        # Track intermediates so finally can clean them up. MASTER RULE 6.
        intermediates = []

        try:
            brush_fc_layer       = parameters[0].valueAsText
            brush_radius_mu      = float(parameters[1].value or 0.0)
            brush_radius_mm      = float(parameters[2].value or 0.0)
            operation            = parameters[3].valueAsText
            existing_aoi_layer   = parameters[4].valueAsText
            frame_fc_layer       = parameters[5].valueAsText
            clip_to_frame        = _as_bool(parameters[6].value, True)
            out_ws               = parameters[7].valueAsText or arcpy.env.scratchGDB
            out_name             = parameters[8].valueAsText
            add_to_map           = _as_bool(parameters[9].value, True)

            if not brush_fc_layer:
                raise arcpy.ExecuteError(u"Brush feature is required.")

            # Selection-bypass announcements + resolve to on-disk paths
            _announce_selection(u"Brush", brush_fc_layer)
            if existing_aoi_layer:
                _announce_selection(u"ExistingAOI", existing_aoi_layer)
            if frame_fc_layer:
                _announce_selection(u"Frame", frame_fc_layer)

            brush_fc     = _resolve_full_source(brush_fc_layer)
            existing_aoi = _resolve_full_source(existing_aoi_layer) if existing_aoi_layer else None
            frame_fc     = _resolve_full_source(frame_fc_layer) if frame_fc_layer else None

            scratch = _ensure_scratch()
            _diag(u"Scratch (disk): {0}".format(scratch))

            desc = arcpy.Describe(brush_fc)
            sr = desc.spatialReference
            map_units_name = getattr(sr, "linearUnitName", "") if sr else ""

            # mm -> map units
            if brush_radius_mm and brush_radius_mm > 0:
                scale = _get_df_scale()
                conv = _mm_to_mapunits(brush_radius_mm, scale, map_units_name)
                if conv is not None:
                    brush_radius_mu = max(brush_radius_mu, float(conv))
                    _msg(u"Brush radius converted from mm using scale 1:{0:.0f} => {1:.3f} map units".format(
                        scale, conv))
                else:
                    _warn(u"Could not convert brush radius (mm). Using map-units radius only.")

            brush_geom_type = (desc.shapeType or "").upper()
            brush_poly = _scratch_unique("brush_poly")
            intermediates.append(brush_poly)

            if brush_geom_type in ("POLYLINE", "LINE", "POINT", "MULTIPOINT"):
                if brush_radius_mu <= 0:
                    raise arcpy.ExecuteError(
                        u"Brush Radius must be > 0 for Polyline/Point brush strokes.")
                _msg(u"Buffering brush strokes (radius = {0})...".format(brush_radius_mu))
                try:
                    arcpy.analysis.Buffer(
                        brush_fc, brush_poly,
                        float(brush_radius_mu), dissolve_option="ALL")
                except (arcpy.ExecuteError, RuntimeError):
                    raise
            elif brush_geom_type == "POLYGON":
                _msg(u"Dissolving polygon brush strokes...")
                try:
                    arcpy.management.Dissolve(brush_fc, brush_poly)
                except (arcpy.ExecuteError, RuntimeError):
                    raise
            else:
                raise arcpy.ExecuteError(
                    u"Unsupported brush geometry type: {0}".format(brush_geom_type))

            brush_poly2 = _scratch_unique("brush_poly_diss")
            intermediates.append(brush_poly2)
            try:
                arcpy.management.Dissolve(brush_poly, brush_poly2)
            except (arcpy.ExecuteError, RuntimeError):
                raise
            _safe_delete(brush_poly)
            brush_poly = brush_poly2

            out_aoi_tmp = _scratch_unique("aoi_tmp")
            intermediates.append(out_aoi_tmp)
            if operation == "Create new AOI (from brush)":
                arcpy.management.CopyFeatures(brush_poly, out_aoi_tmp)
            elif operation == "Replace existing AOI (overwrite with brush)":
                if not existing_aoi:
                    raise arcpy.ExecuteError(u"Existing AOI is required for Replace operation.")
                arcpy.management.CopyFeatures(brush_poly, out_aoi_tmp)
            elif operation == "Add brush to existing AOI":
                if not existing_aoi:
                    raise arcpy.ExecuteError(u"Existing AOI is required for Add operation.")
                union_fc = _scratch_unique("aoi_union")
                intermediates.append(union_fc)
                _msg(u"Union: existing AOI + brush...")
                try:
                    arcpy.analysis.Union(
                        [existing_aoi, brush_poly], union_fc, "ALL", "", "GAPS")
                    arcpy.management.Dissolve(union_fc, out_aoi_tmp)
                except (arcpy.ExecuteError, RuntimeError):
                    raise
            elif operation == "Subtract brush from existing AOI":
                if not existing_aoi:
                    raise arcpy.ExecuteError(u"Existing AOI is required for Subtract operation.")
                try:
                    prod = arcpy.ProductInfo()
                except (RuntimeError, arcpy.ExecuteError):
                    prod = None
                if prod != "ArcInfo":
                    _warn(u"Advanced license not detected. Subtract uses Erase and may fail.")
                _msg(u"Erase: existing AOI MINUS brush...")
                try:
                    arcpy.analysis.Erase(existing_aoi, brush_poly, out_aoi_tmp)
                except (arcpy.ExecuteError, RuntimeError):
                    raise
            else:
                raise arcpy.ExecuteError(u"Unknown Operation Mode.")

            if clip_to_frame and frame_fc:
                _msg(u"Clipping AOI to frame...")
                out_aoi_clipped = _scratch_unique("aoi_clip")
                intermediates.append(out_aoi_clipped)
                try:
                    arcpy.analysis.Clip(out_aoi_tmp, frame_fc, out_aoi_clipped)
                    _safe_delete(out_aoi_tmp)
                    out_aoi_tmp = out_aoi_clipped
                except (arcpy.ExecuteError, RuntimeError):
                    _warn(u"Clip to frame failed; using unclipped AOI. {0}".format(
                        traceback.format_exc()))

            out_aoi_fc = _normalize_output_path(out_ws, out_name)
            if arcpy.Exists(out_aoi_fc):
                arcpy.management.Delete(out_aoi_fc)
            arcpy.management.CopyFeatures(out_aoi_tmp, out_aoi_fc)

            _diag(u"AOI built. Output: {0}".format(out_aoi_fc))
            try:
                _diag(u"AOI feature count: {0}".format(_get_count(out_aoi_fc)))
            except (RuntimeError, arcpy.ExecuteError):
                pass

            if add_to_map:
                _add_layers_to_map([out_aoi_fc])

            _msg(u"AOI created. Output: {0}".format(out_aoi_fc))

        except (arcpy.ExecuteError, RuntimeError):
            _err(traceback.format_exc())
            raise
        finally:
            # MASTER RULE 6: explicit cleanup of every intermediate.
            for p in intermediates:
                _safe_delete(p)
            _flush_in_memory()
            gc.collect()
            _restore_gp_env(env_snap)




# =============================================================================
# 6. Tool 2: Safe Contour Cleaner
# =============================================================================

class SafeContourCleaner(object):
    """Build a cleaned COPY of contour line layers for cartographic output."""

    # Parameter indices
    IDX_IN_CONTOURS         = 0
    IDX_FRAME_POLY          = 1
    IDX_SAFE_MU             = 2
    IDX_SAFE_MM             = 3
    IDX_DENSE_TH            = 4
    IDX_MIN_NEIGHBORS       = 5
    IDX_AOI_MODE            = 6
    IDX_CUSTOM_AOI          = 7
    IDX_MASK_MODE           = 8
    IDX_EXTERNAL_MASK       = 9
    IDX_ELIGIBLE_SQL        = 10
    IDX_PROTECTED_SQL       = 11
    IDX_REMOVAL_METHOD      = 12
    IDX_MIN_SEG_LEN         = 13
    IDX_OUT_WS              = 14
    IDX_OUT_NAME            = 15
    IDX_CREATE_REMOVED      = 16
    IDX_CREATE_REVIEW       = 17
    IDX_CREATE_MASK_OUT     = 18
    IDX_WRITE_REPORT        = 19
    IDX_ADD_TO_MAP          = 20
    IDX_DRY_RUN             = 21
    IDX_NEAR_CHUNK          = 22
    IDX_ALLOW_FULL_MAP      = 23

    def __init__(self):
        self.label = u"Safe Contour Cleaner (Print-Ready) - v5 hardened"
        self.description = (
            u"Build a cleaned COPY of contour line layers for print, leaving "
            u"the originals untouched. Removes dense / overlapping segments "
            u"inside an AOI while protecting a safety band inside the frame.\n\n"
            u"v5 master-rules:\n"
            u" - Narrow exceptions; MemoryError / OSError propagate.\n"
            u" - Selection-bypass hardwired (FULL datasets always processed).\n"
            u" - All intermediates land in scratchGDB on disk.\n"
            u" - Single dissolve+clip strategy (no per-feature clip loop).\n"
            u" - AOI optionality: 'Allow Full-Map Processing' fallback.\n"
            u" - Sliver removal at XYTolerance after clip.\n"
            u" - GP env snapshot/restore + in_memory flush in finally."
        )
        self.canRunInBackground = True

    def isLicensed(self):
        return True

    # --- Helpers -----------------------------------------------------------

    def _split_multivalue(self, mv_text):
        if not mv_text:
            return []
        return [p.strip() for p in _to_unicode(mv_text).split(u";") if p.strip()]

    def _combine_where(self, a, b):
        a = (a or u"").strip()
        b = (b or u"").strip()
        if a and b:
            return u"({0}) AND ({1})".format(a, b)
        return a or b

    def _make_where_not(self, sql):
        sql = (sql or u"").strip()
        if not sql:
            return u""
        return u"NOT ({0})".format(sql)

    def _extent_to_polygon_fc(self, extent, out_fc, sr):
        """Create a single-polygon FC representing a rectangle extent."""
        arcpy.management.CreateFeatureclass(
            os.path.dirname(out_fc), os.path.basename(out_fc),
            "POLYGON", spatial_reference=sr)
        arr = arcpy.Array([
            arcpy.Point(extent.XMin, extent.YMin),
            arcpy.Point(extent.XMax, extent.YMin),
            arcpy.Point(extent.XMax, extent.YMax),
            arcpy.Point(extent.XMin, extent.YMax),
            arcpy.Point(extent.XMin, extent.YMin),
        ])
        poly = arcpy.Polygon(arr, sr)
        with arcpy.da.InsertCursor(out_fc, ["SHAPE@"]) as ic:
            ic.insertRow([poly])
        return out_fc

    def _dissolve_to_single_multipart(self, in_fc, out_fc):
        """
        MASTER RULE: dissolve any polygon FC down to ONE multipart polygon.
        This is the canonical clip-mask geometry: a single feature with all
        AOI islands as parts. Allows a single arcpy.analysis.Clip call.
        """
        try:
            arcpy.management.Dissolve(in_fc, out_fc, multi_part="MULTI_PART")
        except (arcpy.ExecuteError, RuntimeError):
            # Fallback: copy as-is (caller may still pass it to Clip).
            _warn(u"Dissolve to multipart failed; using raw AOI.")
            arcpy.management.CopyFeatures(in_fc, out_fc)
        return out_fc

    def _single_clip(self, in_fc, clip_multipart_fc, out_fc):
        """
        Single arcpy.analysis.Clip call against a pre-dissolved multipart
        polygon. NO per-feature looping. MASTER RULE for the memory leak fix.
        """
        try:
            arcpy.analysis.Clip(in_fc, clip_multipart_fc, out_fc)
            return out_fc
        except (arcpy.ExecuteError, RuntimeError):
            _warn(u"Single Clip failed; falling back to "
                  u"SelectByLocation+CopyFeatures (no looping). {0}".format(
                      traceback.format_exc()))
            lyr = _unique("lyr_clipfb")
            try:
                arcpy.management.MakeFeatureLayer(in_fc, lyr)
                arcpy.management.SelectLayerByLocation(
                    lyr, "INTERSECT", clip_multipart_fc)
                arcpy.management.CopyFeatures(lyr, out_fc)
            finally:
                try:
                    arcpy.management.Delete(lyr)
                except (arcpy.ExecuteError, RuntimeError):
                    pass
            return out_fc

    def _remove_slivers(self, fc, xy_tolerance):
        """
        Remove tiny artifacts shorter/smaller than XYTolerance. Runs inline
        with a SearchCursor (no big dict / no per-feature GP). MASTER RULE.
        """
        if not fc or not arcpy.Exists(fc):
            return 0
        if xy_tolerance is None or float(xy_tolerance) <= 0:
            return 0
        tol = float(xy_tolerance)
        try:
            shape_type = (arcpy.Describe(fc).shapeType or "").upper()
        except (RuntimeError, arcpy.ExecuteError):
            return 0
        oid_field = arcpy.Describe(fc).OIDFieldName

        # Stream through with a cursor; only collect short OIDs, not geometry.
        sliver_oids = []
        if shape_type in ("POLYLINE", "LINE"):
            with arcpy.da.SearchCursor(fc, [oid_field, "SHAPE@LENGTH"]) as cur:
                for oidv, lng in cur:
                    if lng is None:
                        sliver_oids.append(int(oidv))
                    elif float(lng) < tol:
                        sliver_oids.append(int(oidv))
        elif shape_type == "POLYGON":
            with arcpy.da.SearchCursor(fc, [oid_field, "SHAPE@AREA"]) as cur:
                for oidv, area in cur:
                    if area is None:
                        sliver_oids.append(int(oidv))
                    elif float(area) < (tol * tol):
                        sliver_oids.append(int(oidv))
        else:
            return 0

        if not sliver_oids:
            return 0

        lyr = _unique("sliver_lyr")
        try:
            arcpy.management.MakeFeatureLayer(fc, lyr)
            arcpy.management.SelectLayerByAttribute(lyr, "CLEAR_SELECTION")
            chunks = [sliver_oids[i:i + 999]
                      for i in xrange(0, len(sliver_oids), 999)]  # noqa: F821
            first = True
            for ch in chunks:
                where = u"{0} IN ({1})".format(
                    arcpy.AddFieldDelimiters(lyr, oid_field),
                    u",".join([_to_unicode(x) for x in ch]))
                arcpy.management.SelectLayerByAttribute(
                    lyr,
                    "NEW_SELECTION" if first else "ADD_TO_SELECTION",
                    where)
                first = False
            try:
                arcpy.management.DeleteFeatures(lyr)
            except (arcpy.ExecuteError, RuntimeError):
                _warn(u"DeleteFeatures failed for slivers.")
        finally:
            try:
                arcpy.management.Delete(lyr)
            except (arcpy.ExecuteError, RuntimeError):
                pass
        _diag(u"Slivers removed (< {0:.6f}): {1}".format(tol, len(sliver_oids)))
        return len(sliver_oids)

    def _safe_zone_from_frame(self, frame_fc, safe_margin, out_safe_zone_fc,
                              intermediates):
        """Build the no-delete polygon band INSIDE the frame."""
        scratch = arcpy.env.scratchGDB
        dissolved = arcpy.CreateUniqueName(_unique("frame_diss"), scratch)
        intermediates.append(dissolved)
        try:
            arcpy.management.Dissolve(frame_fc, dissolved, multi_part="MULTI_PART")
        except (arcpy.ExecuteError, RuntimeError):
            arcpy.management.CopyFeatures(frame_fc, dissolved)

        if safe_margin is None or safe_margin <= 0:
            arcpy.management.CopyFeatures(dissolved, out_safe_zone_fc)
            return out_safe_zone_fc

        frame_line = arcpy.CreateUniqueName(_unique("frame_line"), scratch)
        intermediates.append(frame_line)
        try:
            arcpy.management.PolygonToLine(dissolved, frame_line)
        except (arcpy.ExecuteError, RuntimeError):
            _warn(u"PolygonToLine failed; using full frame as safe zone.")
            arcpy.management.CopyFeatures(dissolved, out_safe_zone_fc)
            return out_safe_zone_fc

        band = arcpy.CreateUniqueName(_unique("frame_band"), scratch)
        intermediates.append(band)
        try:
            arcpy.analysis.Buffer(
                frame_line, band, abs(float(safe_margin)),
                dissolve_option="ALL")
        except (arcpy.ExecuteError, RuntimeError):
            _warn(u"Buffer failed for safe zone; using full frame as safe zone.")
            arcpy.management.CopyFeatures(dissolved, out_safe_zone_fc)
            return out_safe_zone_fc

        try:
            arcpy.analysis.Clip(band, dissolved, out_safe_zone_fc)
        except (arcpy.ExecuteError, RuntimeError):
            _warn(u"Clip failed for safe zone; using full frame as safe zone.")
            arcpy.management.CopyFeatures(dissolved, out_safe_zone_fc)
        return out_safe_zone_fc



    def _build_dense_mask(self, eligible_fc, threshold, min_neighbors,
                           out_mask_fc, aoi_fc=None, near_chunk=50000,
                           intermediates=None):
        """
        Build dense-zone mask from eligible contours using GenerateNearTable
        with optional OID-bounded chunking for huge eligible sets.
        Selection on inputs is bypassed (caller passes catalogPaths).
        """
        if intermediates is None:
            intermediates = []

        if threshold is None or float(threshold) <= 0:
            raise arcpy.ExecuteError(u"Dense threshold must be > 0.")
        if min_neighbors is None or int(min_neighbors) < 1:
            min_neighbors = 1
        min_neighbors = int(min_neighbors)

        scratch = arcpy.env.scratchGDB

        # Optional clip-to-AOI for speed (still single-clip, dissolved AOI
        # provided by caller).
        working_fc = eligible_fc
        local_clip = None
        if aoi_fc:
            local_clip = arcpy.CreateUniqueName(_unique("eligible_clip"), scratch)
            intermediates.append(local_clip)
            working_fc = self._single_clip(eligible_fc, aoi_fc, local_clip)

        sr = arcpy.Describe(eligible_fc).spatialReference
        count = _get_count(working_fc)
        _diag(u"Dense pass: eligible-in-AOI count = {0}".format(count))

        if count == 0:
            arcpy.management.CreateFeatureclass(
                os.path.dirname(out_mask_fc), os.path.basename(out_mask_fc),
                "POLYGON", spatial_reference=sr)
            return out_mask_fc

        # Spatial index for downstream operations
        try:
            arcpy.management.AddSpatialIndex(working_fc)
        except (arcpy.ExecuteError, RuntimeError):
            pass

        try:
            oid_field = arcpy.Describe(working_fc).OIDFieldName
        except (RuntimeError, arcpy.ExecuteError):
            oid_field = "OBJECTID"

        from collections import defaultdict
        neighbor_counts = defaultdict(int)

        def _process_near_table(near_tbl_path):
            with arcpy.da.SearchCursor(
                    near_tbl_path, ["IN_FID", "NEAR_FID", "NEAR_DIST"]) as cur:
                for in_fid, near_fid, dist in cur:
                    if in_fid is None or near_fid is None:
                        continue
                    if int(in_fid) == int(near_fid):
                        continue
                    if dist is None:
                        continue
                    if float(dist) <= float(threshold):
                        neighbor_counts[int(in_fid)] += 1

        if count <= near_chunk:
            near_tbl = arcpy.CreateUniqueName(_unique("near"), scratch)
            try:
                arcpy.analysis.GenerateNearTable(
                    working_fc, working_fc, near_tbl,
                    search_radius=float(threshold),
                    location="NO_LOCATION", angle="NO_ANGLE",
                    closest="CLOSEST", closest_count=min_neighbors + 1)
                _process_near_table(near_tbl)
            except (arcpy.ExecuteError, RuntimeError):
                _warn(u"GenerateNearTable failed: {0}".format(traceback.format_exc()))
            finally:
                _safe_delete(near_tbl)
        else:
            # Enumerate OIDs in order
            oids = []
            with arcpy.da.SearchCursor(working_fc, [oid_field]) as cur:
                for r in cur:
                    oids.append(int(r[0]))
            oids.sort()
            n = len(oids)
            _diag(u"Chunked Near: {0} features, chunk={1}".format(n, near_chunk))
            i = 0
            chunk_idx = 0
            while i < n:
                chunk = oids[i:i + near_chunk]
                i += near_chunk
                chunk_idx += 1
                if not chunk:
                    continue
                lo = chunk[0]; hi = chunk[-1]
                where = u"{0} >= {1} AND {0} <= {2}".format(oid_field, lo, hi)
                sel_lyr = _unique("near_chunk")
                near_tbl = arcpy.CreateUniqueName(_unique("near"), scratch)
                try:
                    arcpy.management.MakeFeatureLayer(working_fc, sel_lyr, where)
                    arcpy.analysis.GenerateNearTable(
                        sel_lyr, working_fc, near_tbl,
                        search_radius=float(threshold),
                        location="NO_LOCATION", angle="NO_ANGLE",
                        closest="CLOSEST", closest_count=min_neighbors + 1)
                    _process_near_table(near_tbl)
                    _diag(u"  Near chunk {0}: OIDs {1}..{2} ({3} feats)".format(
                        chunk_idx, lo, hi, len(chunk)))
                except (arcpy.ExecuteError, RuntimeError):
                    _warn(u"Near chunk {0} failed: {1}".format(
                        chunk_idx, traceback.format_exc()))
                finally:
                    _safe_delete(near_tbl)
                    try:
                        arcpy.management.Delete(sel_lyr)
                    except (arcpy.ExecuteError, RuntimeError):
                        pass
                    gc.collect()

        dense_oids = [fid for fid, cnt in neighbor_counts.items()
                      if cnt >= min_neighbors]
        _diag(u"Dense features detected: {0} (of {1})".format(
            len(dense_oids), count))

        if not dense_oids:
            arcpy.management.CreateFeatureclass(
                os.path.dirname(out_mask_fc), os.path.basename(out_mask_fc),
                "POLYGON", spatial_reference=sr)
            return out_mask_fc

        # Select dense features -> buffer -> copy to mask
        dense_layer = _unique("dense_lyr")
        try:
            arcpy.management.MakeFeatureLayer(working_fc, dense_layer)
            arcpy.management.SelectLayerByAttribute(dense_layer, "CLEAR_SELECTION")
            chunks = [dense_oids[i:i + 999]
                      for i in xrange(0, len(dense_oids), 999)]  # noqa: F821
            first = True
            for ch in chunks:
                where = u"{0} IN ({1})".format(
                    arcpy.AddFieldDelimiters(dense_layer, oid_field),
                    u",".join([_to_unicode(x) for x in ch]))
                arcpy.management.SelectLayerByAttribute(
                    dense_layer,
                    "NEW_SELECTION" if first else "ADD_TO_SELECTION",
                    where)
                first = False

            tmp_buf = arcpy.CreateUniqueName(_unique("dense_buf"), scratch)
            try:
                arcpy.analysis.Buffer(
                    dense_layer, tmp_buf, float(threshold) / 2.0,
                    dissolve_option="ALL")
                arcpy.management.CopyFeatures(tmp_buf, out_mask_fc)
            finally:
                _safe_delete(tmp_buf)
        finally:
            try:
                arcpy.management.Delete(dense_layer)
            except (arcpy.ExecuteError, RuntimeError):
                pass

        return out_mask_fc

    def _remove_small_segments(self, in_lines_fc, min_length, out_lines_fc):
        """Remove segments shorter than min_length using a single Delete pass."""
        arcpy.management.CopyFeatures(in_lines_fc, out_lines_fc)
        if min_length is None or float(min_length) <= 0:
            return out_lines_fc

        oid_field = arcpy.Describe(out_lines_fc).OIDFieldName
        lyr = _unique("short_lyr")
        try:
            arcpy.management.MakeFeatureLayer(out_lines_fc, lyr)
            short_oids = []
            with arcpy.da.SearchCursor(out_lines_fc, [oid_field, "SHAPE@LENGTH"]) as cur:
                for oidv, lng in cur:
                    if lng is None:
                        continue
                    if float(lng) < float(min_length):
                        short_oids.append(int(oidv))
            if not short_oids:
                return out_lines_fc
            chunks = [short_oids[i:i + 999]
                      for i in xrange(0, len(short_oids), 999)]  # noqa: F821
            arcpy.management.SelectLayerByAttribute(lyr, "CLEAR_SELECTION")
            first = True
            for ch in chunks:
                where = u"{0} IN ({1})".format(
                    arcpy.AddFieldDelimiters(lyr, oid_field),
                    u",".join([_to_unicode(x) for x in ch]))
                arcpy.management.SelectLayerByAttribute(
                    lyr,
                    "NEW_SELECTION" if first else "ADD_TO_SELECTION",
                    where)
                first = False
            try:
                arcpy.management.DeleteFeatures(lyr)
            except (arcpy.ExecuteError, RuntimeError):
                _warn(u"DeleteFeatures failed for short segments.")
        finally:
            try:
                arcpy.management.Delete(lyr)
            except (arcpy.ExecuteError, RuntimeError):
                pass
        return out_lines_fc

    def _approx_total_length(self, fc):
        if not fc or not arcpy.Exists(fc):
            return 0.0
        total = 0.0
        try:
            with arcpy.da.SearchCursor(fc, ["SHAPE@LENGTH"]) as cur:
                for r in cur:
                    if r[0] is not None:
                        total += float(r[0])
        except (RuntimeError, arcpy.ExecuteError):
            pass
        return total

    def _report_csv(self, out_ws, base_name,
                    total_in, eligible_count, noneligible_count,
                    dense_th, min_neighbors, safe_mu,
                    aoi_mode, mask_mode, removal_method,
                    min_seg_length, removed_len,
                    out_clean_fc, out_removed_fc, out_review_fc, out_mask_fc,
                    full_map_fallback_used, sliver_count):
        out_ws_low = (_to_unicode(out_ws) or u"").lower()
        if u".gdb" in out_ws_low:
            parent = os.path.dirname(_to_unicode(out_ws).rstrip(u"\\/"))
            report_path = os.path.join(parent, base_name + "_Report.csv")
        else:
            report_path = os.path.join(out_ws, base_name + "_Report.csv")
        with open(report_path, "wb") as f:
            w = csv.writer(f)
            w.writerow(["Tool", "Safe Contour Cleaner (v5 hardened, master rules)"])
            w.writerow(["DateTime", str(datetime.datetime.now())])
            w.writerow([])
            w.writerow(["InputContoursCount", total_in])
            w.writerow(["EligibleCount", eligible_count])
            w.writerow(["ProtectedOrOtherCount", noneligible_count])
            w.writerow(["DenseThreshold", dense_th])
            w.writerow(["MinNeighbors", min_neighbors])
            w.writerow(["SafeMargin_MapUnits", safe_mu])
            w.writerow(["AOIMode", _ascii_safe(aoi_mode)])
            w.writerow(["MaskMode", _ascii_safe(mask_mode)])
            w.writerow(["RemovalMethod", _ascii_safe(removal_method)])
            w.writerow(["MinSegmentLength", min_seg_length])
            w.writerow(["RemovedLengthApprox", removed_len])
            w.writerow(["FullMapFallbackUsed", bool(full_map_fallback_used)])
            w.writerow(["SliversRemoved", sliver_count])
            w.writerow([])
            w.writerow(["OutputCleanFC", _ascii_safe(out_clean_fc)])
            w.writerow(["OutputRemovedFC", _ascii_safe(out_removed_fc or "")])
            w.writerow(["OutputReviewFC", _ascii_safe(out_review_fc or "")])
            w.writerow(["OutputMaskFC", _ascii_safe(out_mask_fc or "")])
        return report_path



    # --- Parameters --------------------------------------------------------

    def getParameterInfo(self):
        params = []

        p0 = arcpy.Parameter(
            displayName=u"Input Contour Layers (one or more)",
            name="in_contours", datatype="GPFeatureLayer",
            parameterType="Required", direction="Input", multiValue=True)
        p0.category = "1) Inputs"
        params.append(p0)

        p1 = arcpy.Parameter(
            displayName=u"Frame / Neatline Polygon (recommended)",
            name="frame_polygon", datatype="GPFeatureLayer",
            parameterType="Optional", direction="Input")
        p1.category = "1) Inputs"
        params.append(p1)

        p2 = arcpy.Parameter(
            displayName=u"Safe Margin INSIDE Frame (map units)",
            name="safe_margin_mu", datatype="GPDouble",
            parameterType="Optional", direction="Input")
        p2.category = "2) Safety"; p2.value = 0.0
        params.append(p2)

        p3 = arcpy.Parameter(
            displayName=u"Safe Margin INSIDE Frame (millimeters on map) [optional]",
            name="safe_margin_mm", datatype="GPDouble",
            parameterType="Optional", direction="Input")
        p3.category = "2) Safety"; p3.value = 0.0
        params.append(p3)

        p4 = arcpy.Parameter(
            displayName=u"Dense Threshold Distance (map units)",
            name="dense_threshold", datatype="GPDouble",
            parameterType="Required", direction="Input")
        p4.category = "3) Dense Zone Detection"; p4.value = 20.0
        params.append(p4)

        p5 = arcpy.Parameter(
            displayName=u"Minimum Neighbors for Dense (>=1)",
            name="min_neighbors", datatype="GPLong",
            parameterType="Optional", direction="Input")
        p5.category = "3) Dense Zone Detection"; p5.value = 1
        params.append(p5)

        p6 = arcpy.Parameter(
            displayName=u"AOI Mode (Where to clean)", name="aoi_mode",
            datatype="GPString", parameterType="Required", direction="Input")
        p6.category = "4) AOI / Target Area"
        p6.filter.type = "ValueList"
        p6.filter.list = [
            "Frame only (default)",
            "Custom AOI only",
            "Frame AND Custom AOI",
            "Entire dataset (no AOI)",
        ]
        p6.value = "Frame only (default)"
        params.append(p6)

        p7 = arcpy.Parameter(
            displayName=u"Custom AOI Polygon (optional)", name="custom_aoi",
            datatype="GPFeatureLayer", parameterType="Optional", direction="Input")
        p7.category = "4) AOI / Target Area"
        params.append(p7)

        p8 = arcpy.Parameter(
            displayName=u"Mask Mode (How to create removal mask)",
            name="mask_mode", datatype="GPString",
            parameterType="Required", direction="Input")
        p8.category = "5) Mask Generation"
        p8.filter.type = "ValueList"
        p8.filter.list = [
            "Auto mask from dense zones (recommended)",
            "Use AOI polygon as mask (no auto)",
            "Use external mask polygon",
        ]
        p8.value = "Auto mask from dense zones (recommended)"
        params.append(p8)

        p9 = arcpy.Parameter(
            displayName=u"External Mask Polygon (optional)",
            name="external_mask", datatype="GPFeatureLayer",
            parameterType="Optional", direction="Input")
        p9.category = "5) Mask Generation"
        params.append(p9)

        p10 = arcpy.Parameter(
            displayName=u"Eligible Contours SQL (CAN be cleaned)",
            name="eligible_sql", datatype="GPString",
            parameterType="Optional", direction="Input")
        p10.category = "6) Rules"; p10.value = "1=1"
        p10.description = u"SQL to choose eligible contours."
        params.append(p10)

        p11 = arcpy.Parameter(
            displayName=u"Protected Contours SQL (MUST NEVER be cleaned)",
            name="protected_sql", datatype="GPString",
            parameterType="Optional", direction="Input")
        p11.category = "6) Rules"; p11.value = ""
        params.append(p11)

        p12 = arcpy.Parameter(
            displayName=u"Removal Method", name="removal_method",
            datatype="GPString", parameterType="Required", direction="Input")
        p12.category = "7) Cleaning Strategy"
        p12.filter.type = "ValueList"
        p12.filter.list = [
            "Segment Erase (recommended)",
            "Delete Whole Features",
        ]
        p12.value = "Segment Erase (recommended)"
        params.append(p12)

        p13 = arcpy.Parameter(
            displayName=u"Min segment length to delete after erase (map units)",
            name="min_seg_length", datatype="GPDouble",
            parameterType="Optional", direction="Input")
        p13.category = "7) Cleaning Strategy"; p13.value = 0.0
        params.append(p13)

        p14 = arcpy.Parameter(
            displayName=u"Output Workspace (GDB recommended)",
            name="out_workspace", datatype="DEWorkspace",
            parameterType="Optional", direction="Input")
        p14.category = "8) Outputs"
        params.append(p14)

        p15 = arcpy.Parameter(
            displayName=u"Output Clean Contours Name",
            name="out_clean_name", datatype="GPString",
            parameterType="Required", direction="Input")
        p15.category = "8) Outputs"; p15.value = "Contours_CartoClean"
        params.append(p15)

        p16 = arcpy.Parameter(
            displayName=u"Create Removed Segments Layer",
            name="create_removed", datatype="GPBoolean",
            parameterType="Optional", direction="Input")
        p16.category = "8) Outputs"; p16.value = True
        params.append(p16)

        p17 = arcpy.Parameter(
            displayName=u"Create Review Layer (dense but protected)",
            name="create_review", datatype="GPBoolean",
            parameterType="Optional", direction="Input")
        p17.category = "8) Outputs"; p17.value = True
        params.append(p17)

        p18 = arcpy.Parameter(
            displayName=u"Create Mask Output Layer",
            name="create_mask_out", datatype="GPBoolean",
            parameterType="Optional", direction="Input")
        p18.category = "8) Outputs"; p18.value = True
        params.append(p18)

        p19 = arcpy.Parameter(
            displayName=u"Write CSV Report", name="write_report",
            datatype="GPBoolean", parameterType="Optional", direction="Input")
        p19.category = "8) Outputs"; p19.value = True
        params.append(p19)

        p20 = arcpy.Parameter(
            displayName=u"Add outputs to current map (ArcMap)",
            name="add_to_map", datatype="GPBoolean",
            parameterType="Optional", direction="Input")
        p20.category = "8) Outputs"; p20.value = True
        params.append(p20)

        p21 = arcpy.Parameter(
            displayName=u"Dry Run / Preview Mode (no cleaned output)",
            name="dry_run", datatype="GPBoolean",
            parameterType="Optional", direction="Input")
        p21.category = "8) Outputs"; p21.value = False
        params.append(p21)

        p22 = arcpy.Parameter(
            displayName=u"Near Chunk Size (features per Near pass)",
            name="near_chunk_size", datatype="GPLong",
            parameterType="Optional", direction="Input")
        p22.category = "8) Outputs"; p22.value = 50000
        p22.description = u"For very large eligible sets, GenerateNearTable runs in OID-bounded chunks of this size."
        params.append(p22)

        p23 = arcpy.Parameter(
            displayName=u"Allow Full-Map Processing if AOI is Empty",
            name="allow_full_map_processing", datatype="GPBoolean",
            parameterType="Optional", direction="Input")
        p23.category = "9) Safety Overrides"; p23.value = False
        p23.description = (
            u"If False (default): empty AOI raises an error. "
            u"If True: tool falls back to the active map's extent and "
            u"processes the full map, with a loud warning. Off by default "
            u"to prevent accidental whole-dataset edits.")
        params.append(p23)

        return params

    def updateParameters(self, parameters):
        try:
            aoi_mode = parameters[self.IDX_AOI_MODE].valueAsText
            mask_mode = parameters[self.IDX_MASK_MODE].valueAsText
            parameters[self.IDX_CUSTOM_AOI].enabled = aoi_mode in (
                "Custom AOI only", "Frame AND Custom AOI")
            parameters[self.IDX_EXTERNAL_MASK].enabled = (
                mask_mode == "Use external mask polygon")
        except (RuntimeError, AttributeError, arcpy.ExecuteError):
            pass

    def updateMessages(self, parameters):
        try:
            aoi_mode = parameters[self.IDX_AOI_MODE].valueAsText
            frame = parameters[self.IDX_FRAME_POLY].valueAsText
            allow_full = _as_bool(
                parameters[self.IDX_ALLOW_FULL_MAP].value, False)
            if aoi_mode and "Frame" in aoi_mode and not frame and not allow_full:
                parameters[self.IDX_FRAME_POLY].setWarningMessage(
                    u"AOI Mode uses Frame but Frame/Neatline polygon is not "
                    u"provided. Tool will fail at execute() unless you set "
                    u"'Allow Full-Map Processing if AOI is Empty' to True.")
            dense_th = parameters[self.IDX_DENSE_TH].value
            safe_mu = parameters[self.IDX_SAFE_MU].value or 0.0
            if dense_th and float(dense_th) < float(safe_mu):
                parameters[self.IDX_DENSE_TH].setWarningMessage(
                    u"Dense threshold is smaller than safe margin. Cleaning "
                    u"near the frame may be very limited.")
            ncs = parameters[self.IDX_NEAR_CHUNK].value
            if ncs is not None and int(ncs) < 1000:
                parameters[self.IDX_NEAR_CHUNK].setWarningMessage(
                    u"Very small chunk size will be slow; recommend >= 5000.")
        except (RuntimeError, AttributeError, arcpy.ExecuteError, ValueError):
            pass



    # --- Execute -----------------------------------------------------------

    def execute(self, parameters, messages):
        # MASTER RULE 4: snapshot env at entry, neutralize, restore in finally.
        env_snap = _snapshot_gp_env()
        arcpy.env.overwriteOutput = True
        try:
            arcpy.env.parallelProcessingFactor = "100%"
        except (RuntimeError, arcpy.ExecuteError):
            pass

        # MASTER RULE 6: track every intermediate so finally can delete them.
        intermediates = []
        full_map_fallback_used = False
        sliver_count = 0

        try:
            # Read parameters
            in_contours_mv          = parameters[self.IDX_IN_CONTOURS].valueAsText
            frame_layer             = parameters[self.IDX_FRAME_POLY].valueAsText
            safe_mu                 = float(parameters[self.IDX_SAFE_MU].value or 0.0)
            safe_mm                 = float(parameters[self.IDX_SAFE_MM].value or 0.0)
            dense_threshold         = float(parameters[self.IDX_DENSE_TH].value)
            min_neighbors           = int(parameters[self.IDX_MIN_NEIGHBORS].value or 1)
            aoi_mode                = parameters[self.IDX_AOI_MODE].valueAsText
            custom_aoi_layer        = parameters[self.IDX_CUSTOM_AOI].valueAsText
            mask_mode               = parameters[self.IDX_MASK_MODE].valueAsText
            external_mask_layer     = parameters[self.IDX_EXTERNAL_MASK].valueAsText
            eligible_sql            = parameters[self.IDX_ELIGIBLE_SQL].valueAsText or "1=1"
            protected_sql           = parameters[self.IDX_PROTECTED_SQL].valueAsText or ""
            removal_method          = parameters[self.IDX_REMOVAL_METHOD].valueAsText
            min_seg_length          = float(parameters[self.IDX_MIN_SEG_LEN].value or 0.0)
            out_ws                  = parameters[self.IDX_OUT_WS].valueAsText or arcpy.env.scratchGDB
            out_clean_name          = parameters[self.IDX_OUT_NAME].valueAsText
            create_removed          = _as_bool(parameters[self.IDX_CREATE_REMOVED].value, True)
            create_review           = _as_bool(parameters[self.IDX_CREATE_REVIEW].value, True)
            create_maskout          = _as_bool(parameters[self.IDX_CREATE_MASK_OUT].value, True)
            write_report            = _as_bool(parameters[self.IDX_WRITE_REPORT].value, True)
            add_to_map              = _as_bool(parameters[self.IDX_ADD_TO_MAP].value, True)
            dry_run                 = _as_bool(parameters[self.IDX_DRY_RUN].value, False)
            near_chunk              = int(parameters[self.IDX_NEAR_CHUNK].value or 50000)
            allow_full_map_processing = _as_bool(
                parameters[self.IDX_ALLOW_FULL_MAP].value, False)

            # Validate inputs
            in_contour_layers = self._split_multivalue(in_contours_mv)
            if not in_contour_layers:
                raise arcpy.ExecuteError(u"No input contours provided.")

            scratch = _ensure_scratch()
            _diag(u"Scratch (disk): {0}".format(scratch))

            # Selection-bypass announcements + resolve to on-disk paths
            for lyr in in_contour_layers:
                _announce_selection(u"Contours", lyr)
            if frame_layer:
                _announce_selection(u"Frame", frame_layer)
            if custom_aoi_layer:
                _announce_selection(u"CustomAOI", custom_aoi_layer)
            if external_mask_layer:
                _announce_selection(u"ExternalMask", external_mask_layer)

            in_contours = [_resolve_full_source(l) for l in in_contour_layers]
            frame_fc = _resolve_full_source(frame_layer) if frame_layer else None
            custom_aoi_fc = _resolve_full_source(custom_aoi_layer) if custom_aoi_layer else None
            external_mask_fc = _resolve_full_source(external_mask_layer) if external_mask_layer else None

            # SR / units from first contour
            desc0 = arcpy.Describe(in_contours[0])
            sr = desc0.spatialReference
            map_units_name = getattr(sr, "linearUnitName", "") if sr else ""
            if sr and hasattr(sr, "type") and sr.type == "Geographic":
                _warn(u"Geographic coordinate system detected. Distances may be "
                      u"inaccurate. Project to a projected CRS for best results.")

            # mm -> map units for safe margin
            if safe_mm and safe_mm > 0:
                scale = _get_df_scale()
                conv = _mm_to_mapunits(safe_mm, scale, map_units_name)
                if conv is not None:
                    _msg(u"Safe Margin converted from {0} mm @ 1:{1:.0f} => {2:.3f} map units".format(
                        safe_mm, scale, conv))
                    safe_mu = max(safe_mu, conv)
                else:
                    _warn(u"Could not convert Safe Margin (mm). Using map-units value only.")

            # ----------------------------------------------------------------
            # AOI RESOLUTION (with optional full-map fallback)
            # ----------------------------------------------------------------
            # Compute the candidate AOI from the chosen AOI Mode WITHOUT
            # falling back to anything yet. If the result is empty, we apply
            # the allow_full_map_processing rule.
            # ----------------------------------------------------------------

            aoi_fc = None  # will hold the dissolved single-multipart AOI

            # First, determine if the chosen AOI mode demands a polygon:
            if aoi_mode == "Frame only (default)":
                if frame_fc and _get_count(frame_fc) > 0:
                    aoi_raw = frame_fc
                else:
                    aoi_raw = None
            elif aoi_mode == "Custom AOI only":
                if custom_aoi_fc and _get_count(custom_aoi_fc) > 0:
                    aoi_raw = custom_aoi_fc
                else:
                    aoi_raw = None
            elif aoi_mode == "Frame AND Custom AOI":
                if (frame_fc and custom_aoi_fc
                        and _get_count(frame_fc) > 0
                        and _get_count(custom_aoi_fc) > 0):
                    aoi_int = arcpy.CreateUniqueName(_unique("aoi_int"), scratch)
                    intermediates.append(aoi_int)
                    try:
                        arcpy.analysis.Intersect(
                            [frame_fc, custom_aoi_fc], aoi_int,
                            join_attributes="ONLY_FID")
                        if _get_count(aoi_int) > 0:
                            aoi_raw = aoi_int
                        else:
                            aoi_raw = None
                    except (arcpy.ExecuteError, RuntimeError):
                        _warn(u"Intersect of Frame AND Custom AOI failed.")
                        aoi_raw = None
                else:
                    aoi_raw = None
            elif aoi_mode == "Entire dataset (no AOI)":
                aoi_raw = None
            else:
                aoi_raw = None

            # Apply optionality rule: empty AOI handling
            if aoi_raw is None and aoi_mode != "Entire dataset (no AOI)":
                if not allow_full_map_processing:
                    raise arcpy.ExecuteError(
                        u"AOI is empty for AOI Mode '{0}'. Provide a Frame "
                        u"and/or Custom AOI, OR set 'Allow Full-Map "
                        u"Processing if AOI is Empty' to True to fall back "
                        u"to the active map extent.".format(
                            _ascii_safe(aoi_mode)))
                # Loud warning + fall back to active DataFrame extent
                _warn(u"#" * 70)
                _warn(u"WARNING: AOI is empty. 'Allow Full-Map Processing' is True.")
                _warn(u"Falling back to the ACTIVE MAP EXTENT and processing the FULL MAP.")
                _warn(u"This will edit contours across the entire visible extent. ")
                _warn(u"Re-run with a proper AOI for safer cartographic edits.")
                _warn(u"#" * 70)
                try:
                    fallback_fc = arcpy.CreateUniqueName(
                        _unique("aoi_full_map_fallback"), scratch)
                    intermediates.append(fallback_fc)
                    _get_df_extent_polygon(fallback_fc, sr)
                    aoi_raw = fallback_fc
                    full_map_fallback_used = True
                except (RuntimeError, arcpy.ExecuteError, AttributeError):
                    raise arcpy.ExecuteError(
                        u"Full-map fallback requested, but no active "
                        u"DataFrame extent available. Run inside ArcMap or "
                        u"provide a Frame/Custom AOI.")

            # Dissolve AOI to a single multipart polygon for a SINGLE clip pass.
            if aoi_raw is not None:
                aoi_fc = arcpy.CreateUniqueName(_unique("aoi_multipart"), scratch)
                intermediates.append(aoi_fc)
                self._dissolve_to_single_multipart(aoi_raw, aoi_fc)
                if _get_count(aoi_fc) == 0:
                    # Treat as empty AOI; same fallback logic
                    if not allow_full_map_processing:
                        raise arcpy.ExecuteError(
                            u"Resolved AOI is empty after dissolve. Set "
                            u"'Allow Full-Map Processing if AOI is Empty' "
                            u"to True to process the full map.")
                    _warn(u"Dissolved AOI is empty. Falling back to full map extent.")
                    _safe_delete(aoi_fc)
                    aoi_fc = arcpy.CreateUniqueName(
                        _unique("aoi_full_map_fallback"), scratch)
                    intermediates.append(aoi_fc)
                    try:
                        _get_df_extent_polygon(aoi_fc, sr)
                        full_map_fallback_used = True
                    except (RuntimeError, arcpy.ExecuteError, AttributeError):
                        raise arcpy.ExecuteError(
                            u"Could not build full-map fallback AOI.")

            # Build safe zone (unchanged conceptually; uses the original
            # frame_fc if provided)
            safe_zone_fc = None
            if frame_fc and _get_count(frame_fc) > 0:
                safe_zone_fc = arcpy.CreateUniqueName(_unique("safe_zone"), scratch)
                intermediates.append(safe_zone_fc)
                self._safe_zone_from_frame(
                    frame_fc, safe_mu, safe_zone_fc, intermediates)



            # ----------------------------------------------------------------
            # MERGE INPUTS + SINGLE CLIP TO AOI
            # ----------------------------------------------------------------
            # Memory-leak fix: aoi_fc is a SINGLE multipart polygon. We do
            # ONE arcpy.analysis.Clip on the whole merged dataset. No loops.
            # ----------------------------------------------------------------
            merged_fc = arcpy.CreateUniqueName(_unique("contours_merge"), scratch)
            intermediates.append(merged_fc)
            _msg(u"Merging input contours...")
            try:
                arcpy.management.Merge(in_contours, merged_fc)
            except (arcpy.ExecuteError, RuntimeError):
                raise
            _diag(u"Merged contour count: {0}".format(_get_count(merged_fc)))

            working_all = merged_fc
            if aoi_fc:
                clipped_all = arcpy.CreateUniqueName(_unique("contours_clip"), scratch)
                intermediates.append(clipped_all)
                _msg(u"Single-pass Clip of merged contours to dissolved AOI...")
                working_all = self._single_clip(merged_fc, aoi_fc, clipped_all)

                # Sliver removal at XYTolerance, immediately after clip.
                xy_tol = _get_xy_tolerance(working_all, fallback=0.001)
                _diag(u"Removing slivers shorter than XYTolerance = {0}".format(xy_tol))
                sliver_count = self._remove_slivers(working_all, xy_tol)

            total_in = _get_count(working_all)
            _diag(u"Contours in AOI: {0}".format(total_in))

            # Eligible = (eligible_sql) AND NOT (protected_sql)
            not_prot = self._make_where_not(protected_sql)
            where_eligible = self._combine_where(eligible_sql, not_prot)

            eligible_fc = arcpy.CreateUniqueName(_unique("eligible"), scratch)
            intermediates.append(eligible_fc)
            noneligible_fc = arcpy.CreateUniqueName(_unique("noneligible"), scratch)
            intermediates.append(noneligible_fc)

            lyr_all = _unique("all_lyr")
            try:
                arcpy.management.MakeFeatureLayer(working_all, lyr_all)
                arcpy.management.SelectLayerByAttribute(
                    lyr_all, "NEW_SELECTION", where_eligible)
                arcpy.management.CopyFeatures(lyr_all, eligible_fc)

                arcpy.management.SelectLayerByAttribute(lyr_all, "SWITCH_SELECTION")
                arcpy.management.CopyFeatures(lyr_all, noneligible_fc)
            finally:
                try:
                    arcpy.management.Delete(lyr_all)
                except (arcpy.ExecuteError, RuntimeError):
                    pass

            eligible_count = _get_count(eligible_fc)
            noneligible_count = _get_count(noneligible_fc)
            _diag(u"Eligible: {0} | Protected/Other: {1}".format(
                eligible_count, noneligible_count))

            # Build removal mask
            mask_fc = arcpy.CreateUniqueName(_unique("mask"), scratch)
            intermediates.append(mask_fc)
            if mask_mode == "Use external mask polygon":
                if not external_mask_fc:
                    raise arcpy.ExecuteError(
                        u"Mask Mode is 'Use external mask polygon' but no external "
                        u"mask was provided.")
                arcpy.management.CopyFeatures(external_mask_fc, mask_fc)
            elif mask_mode == "Use AOI polygon as mask (no auto)":
                if not aoi_fc:
                    raise arcpy.ExecuteError(
                        u"AOI is required to use AOI as mask (choose a Frame or Custom AOI mode "
                        u"or enable 'Allow Full-Map Processing').")
                arcpy.management.CopyFeatures(aoi_fc, mask_fc)
            else:
                _msg(u"Building dense-zone mask (Near Table method)...")
                self._build_dense_mask(
                    eligible_fc, dense_threshold, min_neighbors,
                    mask_fc, aoi_fc=aoi_fc, near_chunk=near_chunk,
                    intermediates=intermediates)

            mask_count = _get_count(mask_fc)
            _diag(u"Mask polygons: {0}".format(mask_count))
            if mask_count == 0:
                _warn(u"Mask is empty. No contour segments will be removed (output becomes copy).")

            # Apply frame safety: final_mask = mask - safe_zone
            final_mask_fc = mask_fc
            if (safe_zone_fc and mask_count > 0
                    and _get_count(safe_zone_fc) > 0):
                final_mask_fc = arcpy.CreateUniqueName(_unique("mask_safe"), scratch)
                intermediates.append(final_mask_fc)
                try:
                    arcpy.analysis.Erase(mask_fc, safe_zone_fc, final_mask_fc)
                except (arcpy.ExecuteError, RuntimeError):
                    _warn(u"Erase failed while subtracting Safe Zone from mask. Using raw mask.")
                    _safe_delete(final_mask_fc)
                    final_mask_fc = mask_fc

            final_mask_count = _get_count(final_mask_fc)
            _diag(u"Final mask polygons after frame safety: {0}".format(final_mask_count))
            if final_mask_count == 0 and mask_count > 0:
                _warn(u"After frame safety, final mask is empty (all dense zones were within Safe Margin).")

            # Review layer (dense zones inside safe zone)
            review_fc = None
            if create_review and safe_zone_fc and mask_count > 0:
                review_fc = arcpy.CreateUniqueName(_unique("review"), scratch)
                intermediates.append(review_fc)
                try:
                    arcpy.analysis.Intersect(
                        [mask_fc, safe_zone_fc], review_fc,
                        output_type="POLYGON")
                except (arcpy.ExecuteError, RuntimeError):
                    _warn(u"Could not build review intersect.")
                    _safe_delete(review_fc)
                    review_fc = None

            # Mask output FC (persisted into out_ws)
            out_mask_fc = None
            if create_maskout and mask_count > 0:
                out_mask_fc = _normalize_output_path(out_ws, out_clean_name + "_Mask")
                if arcpy.Exists(out_mask_fc):
                    arcpy.management.Delete(out_mask_fc)
                arcpy.management.CopyFeatures(final_mask_fc, out_mask_fc)

            # Dry run: build preview outputs and exit
            if dry_run:
                out_removed_fc = None
                if create_removed and final_mask_count > 0:
                    tmp_removed = arcpy.CreateUniqueName(_unique("removed_preview"), scratch)
                    intermediates.append(tmp_removed)
                    try:
                        arcpy.analysis.Intersect(
                            [eligible_fc, final_mask_fc], tmp_removed,
                            output_type="LINE")
                        out_removed_fc = _normalize_output_path(
                            out_ws, out_clean_name + "_Removed_Preview")
                        if arcpy.Exists(out_removed_fc):
                            arcpy.management.Delete(out_removed_fc)
                        arcpy.management.CopyFeatures(tmp_removed, out_removed_fc)
                    except (arcpy.ExecuteError, RuntimeError):
                        _warn(u"Dry-run Intersect for removed preview failed.")

                out_review_fc = None
                if review_fc and arcpy.Exists(review_fc):
                    out_review_fc = _normalize_output_path(
                        out_ws, out_clean_name + "_Review")
                    if arcpy.Exists(out_review_fc):
                        arcpy.management.Delete(out_review_fc)
                    arcpy.management.CopyFeatures(review_fc, out_review_fc)

                _diag(u"DRY RUN done. inputs={0} eligible={1} mask={2} final_mask={3}".format(
                    total_in, eligible_count, mask_count, final_mask_count))
                if add_to_map:
                    _add_layers_to_map([out_mask_fc, out_removed_fc, out_review_fc])
                _msg(u"Dry Run complete (Mask/Preview layers created).")
                return

            # License check for Erase
            if removal_method == "Segment Erase (recommended)":
                try:
                    prod = arcpy.ProductInfo()
                except (RuntimeError, arcpy.ExecuteError):
                    prod = None
                if prod != "ArcInfo":
                    _warn(u"Advanced license not detected (Erase may fail). "
                          u"Falling back to 'Delete Whole Features'.")
                    removal_method = "Delete Whole Features"

            cleaned_eligible_fc = arcpy.CreateUniqueName(_unique("eligible_clean"), scratch)
            intermediates.append(cleaned_eligible_fc)
            removed_fc = None

            if final_mask_count == 0:
                _msg(u"Final mask has no features. Skipping removal.")
                arcpy.management.CopyFeatures(eligible_fc, cleaned_eligible_fc)
            else:
                if removal_method == "Segment Erase (recommended)":
                    _msg(u"Cleaning: Segment Erase...")
                    try:
                        arcpy.analysis.Erase(
                            eligible_fc, final_mask_fc, cleaned_eligible_fc)
                    except (arcpy.ExecuteError, RuntimeError):
                        _warn(u"Erase failed; falling back to Delete Whole Features.")
                        _safe_delete(cleaned_eligible_fc)
                        # Fall through to whole-feature path
                        removal_method = "Delete Whole Features"
                    if create_removed and removal_method == "Segment Erase (recommended)":
                        removed_fc = arcpy.CreateUniqueName(_unique("removed"), scratch)
                        intermediates.append(removed_fc)
                        try:
                            arcpy.analysis.Intersect(
                                [eligible_fc, final_mask_fc], removed_fc,
                                output_type="LINE")
                        except (arcpy.ExecuteError, RuntimeError):
                            _warn(u"Could not build removed-segments layer.")
                            _safe_delete(removed_fc)
                            removed_fc = None

                if removal_method == "Delete Whole Features":
                    _msg(u"Cleaning: Delete Whole Features...")
                    lyr_elig = _unique("eligible_lyr")
                    try:
                        arcpy.management.MakeFeatureLayer(eligible_fc, lyr_elig)
                        arcpy.management.SelectLayerByLocation(
                            lyr_elig, "INTERSECT", final_mask_fc)
                        if create_removed:
                            removed_fc = arcpy.CreateUniqueName(_unique("removed"), scratch)
                            intermediates.append(removed_fc)
                            arcpy.management.CopyFeatures(lyr_elig, removed_fc)
                        arcpy.management.SelectLayerByAttribute(
                            lyr_elig, "SWITCH_SELECTION")
                        arcpy.management.CopyFeatures(lyr_elig, cleaned_eligible_fc)
                    finally:
                        try:
                            arcpy.management.Delete(lyr_elig)
                        except (arcpy.ExecuteError, RuntimeError):
                            pass

            # Optional: tiny leftover segments cleanup
            if min_seg_length and min_seg_length > 0:
                _msg(u"Removing tiny leftover segments < {0} ...".format(min_seg_length))
                cleaned2 = arcpy.CreateUniqueName(_unique("eligible_clean2"), scratch)
                intermediates.append(cleaned2)
                self._remove_small_segments(cleaned_eligible_fc, min_seg_length, cleaned2)
                _safe_delete(cleaned_eligible_fc)
                cleaned_eligible_fc = cleaned2

            # Merge cleaned + protected -> final output
            merged_out = arcpy.CreateUniqueName(_unique("merged_out"), scratch)
            intermediates.append(merged_out)
            _msg(u"Merging cleaned eligible + untouched protected contours...")
            arcpy.management.Merge([cleaned_eligible_fc, noneligible_fc], merged_out)

            out_clean_fc = _normalize_output_path(out_ws, out_clean_name)
            if arcpy.Exists(out_clean_fc):
                arcpy.management.Delete(out_clean_fc)
            arcpy.management.CopyFeatures(merged_out, out_clean_fc)

            out_removed_fc = None
            if create_removed and removed_fc and arcpy.Exists(removed_fc):
                out_removed_fc = _normalize_output_path(out_ws, out_clean_name + "_Removed")
                if arcpy.Exists(out_removed_fc):
                    arcpy.management.Delete(out_removed_fc)
                arcpy.management.CopyFeatures(removed_fc, out_removed_fc)

            out_review_fc = None
            if create_review and review_fc and arcpy.Exists(review_fc):
                out_review_fc = _normalize_output_path(out_ws, out_clean_name + "_Review")
                if arcpy.Exists(out_review_fc):
                    arcpy.management.Delete(out_review_fc)
                arcpy.management.CopyFeatures(review_fc, out_review_fc)

            # Diagnostics
            kept_count = _get_count(out_clean_fc)
            removed_len_approx = self._approx_total_length(removed_fc)
            _diag(u"Output kept count: {0}, approx removed length: {1:.2f}".format(
                kept_count, removed_len_approx))

            # Report
            report_path = None
            if write_report:
                try:
                    report_path = self._report_csv(
                        out_ws, out_clean_name,
                        total_in, eligible_count, noneligible_count,
                        dense_threshold, min_neighbors, safe_mu,
                        aoi_mode, mask_mode, removal_method,
                        min_seg_length, removed_len_approx,
                        out_clean_fc, out_removed_fc, out_review_fc, out_mask_fc,
                        full_map_fallback_used, sliver_count)
                except (IOError, OSError):
                    # OSError must propagate per MASTER RULE 1; re-raise.
                    raise
                except (arcpy.ExecuteError, RuntimeError):
                    _warn(u"Failed to write CSV report. {0}".format(traceback.format_exc()))

            if add_to_map:
                _add_layers_to_map([out_clean_fc, out_removed_fc, out_review_fc, out_mask_fc])

            _msg(u"Done. Output: {0}".format(out_clean_fc))
            if report_path:
                _msg(u"Report: {0}".format(report_path))

        except (arcpy.ExecuteError, RuntimeError):
            _err(traceback.format_exc())
            raise
        # MASTER RULE 1: MemoryError and OSError are NOT caught here. They
        # propagate up and crash loudly so ArcMap's 32-bit RAM ceiling is
        # never silently swallowed.
        finally:
            # MASTER RULE 6: explicit Delete for every intermediate, plus the
            # final in_memory flush.
            for p in intermediates:
                _safe_delete(p)
            _flush_in_memory()
            gc.collect()
            _restore_gp_env(env_snap)
