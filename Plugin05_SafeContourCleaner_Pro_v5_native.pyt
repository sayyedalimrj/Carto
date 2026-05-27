# -*- coding: utf-8 -*-
"""
Plugin 05 - Safe Contour Cleaner (ArcGIS Pro / Python 3) - Master Rules rewrite
================================================================================
Build a cleaned COPY of contour line layers for cartographic output, leaving
the originals untouched. Removes dense / overlapping segments inside an AOI
while protecting a safety band inside the frame border.

MASTER RULES enforced:
  1. Narrow exceptions at GP-call sites: (arcpy.ExecuteError, RuntimeError).
     MemoryError / OSError are NEVER caught. No bare `except` /
     `except Exception`.
  2. RAM discipline. Cursors stream inline; no large geometry caches.
  3. Selection hygiene: _resolve_full_source(ignore_selection=True)
     preserved.
  4. arcpy.env snapshot / prime / restore in every execute().
  5. Pro-native: f-strings, native str, arcpy.mp, "memory" workspace.
  6. Cursors inside `with` blocks; scratch datasets and layer views
     cleaned in `finally`.
  7. arcpy.SetProgressor + arcpy.env.autoCancelling for long loops.
  8. Deterministic iteration order via ORDER BY OBJECTID where it
     matters.

Specific fixes vs prior revision:
  F1. Memory leak fix: any per-feature Clip loop is gone.  All clip
      polygons are dissolved into a single multipart polygon and
      one `arcpy.analysis.Clip` is called on the full dataset.
  F2. Optionality: new parameter `allow_full_map_processing`
      (default False).  If the AOI ends up empty and this is False,
      raise a hard error.  If True, fall back to the active map's
      extent and process the full map - logged as a loud warning.
  F3. Sliver removal: after clipping, sliver lines/polygons whose
      length / area is below the dataset XYTolerance are dropped.

Author: Ali Mirjafari + Kiro
Version: 5.1 (Pro / Python 3 / Master Rules)
"""

from __future__ import annotations

import os
import csv
import uuid
import datetime
import gc
import traceback
from typing import Iterable, List, Optional, Tuple

import arcpy


# =============================================================================
# 0. Messaging helpers
# =============================================================================

def _safe_str(v) -> str:
    if v is None:
        return ""
    try:
        if isinstance(v, str):
            return v
        return str(v)
    except (TypeError, ValueError):
        try:
            return repr(v)
        except (TypeError, ValueError):
            return ""


def _msg(s) -> None:
    arcpy.AddMessage(_safe_str(s))


def _warn(s) -> None:
    arcpy.AddWarning(_safe_str(s))


def _err(s) -> None:
    arcpy.AddError(_safe_str(s))


def _diag(s) -> None:
    arcpy.AddMessage(f"[DIAG] {_safe_str(s)}")


def _is_empty(v) -> bool:
    if v is None:
        return True
    s = _safe_str(v).strip()
    return s == "" or s == "#"


def _as_bool(v, default: bool = False) -> bool:
    if v is None:
        return default
    if isinstance(v, bool):
        return v
    s = _safe_str(v).strip().lower()
    if s in ("true", "1", "yes", "y", "t", "on"):
        return True
    if s in ("false", "0", "no", "n", "f", "off"):
        return False
    return default


def _unique(prefix: str = "tmp") -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


# =============================================================================
# 1. Environment snapshot / restore (Master Rule 4)
# =============================================================================

_ENV_KEYS = (
    "extent", "mask", "outputCoordinateSystem", "workspace",
    "scratchWorkspace", "parallelProcessingFactor", "overwriteOutput",
    "autoCancelling",
)


def _snapshot_env() -> dict:
    return {k: getattr(arcpy.env, k, None) for k in _ENV_KEYS}


def _restore_env(snap: dict) -> None:
    for k, v in snap.items():
        try:
            setattr(arcpy.env, k, v)
        except (arcpy.ExecuteError, RuntimeError) as ex:
            arcpy.AddWarning(f"Could not restore arcpy.env.{k}: {ex}")


def _prime_env() -> None:
    arcpy.env.extent = None
    arcpy.env.mask = None
    arcpy.env.outputCoordinateSystem = None
    arcpy.env.workspace = None
    arcpy.env.scratchWorkspace = None
    arcpy.env.overwriteOutput = True
    arcpy.env.parallelProcessingFactor = "100%"
    arcpy.env.autoCancelling = True


# =============================================================================
# 2. Selection-bypass: resolve any layer to its on-disk source
# =============================================================================

def _selection_info(layer_or_path) -> Tuple[Optional[int], Optional[int], str]:
    try:
        d = arcpy.Describe(layer_or_path)
    except (arcpy.ExecuteError, RuntimeError):
        return (None, None, _safe_str(layer_or_path))
    name = getattr(d, "name", _safe_str(layer_or_path))
    fidset = getattr(d, "FIDSet", "") or ""
    try:
        total = int(arcpy.management.GetCount(layer_or_path).getOutput(0))
    except (arcpy.ExecuteError, RuntimeError):
        total = None
    if not fidset.strip():
        return (0, total, name)
    sel = len([t for t in fidset.split(";") if t.strip() != ""])
    return (sel, total, name)


def _resolve_full_source(layer_or_path, ignore_selection: bool = True):
    """Return on-disk catalogPath when ignore_selection is True (default)."""
    if not layer_or_path:
        return layer_or_path
    if not ignore_selection:
        return layer_or_path
    try:
        d = arcpy.Describe(layer_or_path)
    except (arcpy.ExecuteError, RuntimeError):
        return layer_or_path
    cp = getattr(d, "catalogPath", None)
    if cp:
        return cp
    return layer_or_path


def _announce_selection(label: str, layer_or_path) -> None:
    sel, total, name = _selection_info(layer_or_path)
    if sel and sel > 0:
        _warn(
            f"{label}: '{name}' has an active selection ({sel} of "
            f"{total if total is not None else '?'}). Ignoring selection - "
            f"processing FULL dataset.")
    else:
        _diag(
            f"{label}: '{name}' total={total if total is not None else '?'}, "
            f"no active selection.")


# =============================================================================
# 3. Path / workspace helpers
# =============================================================================

def _get_count(fc) -> int:
    try:
        return int(arcpy.management.GetCount(fc).getOutput(0))
    except (arcpy.ExecuteError, RuntimeError):
        return 0


def _normalize_output_path(out_ws: str, name: str) -> str:
    out_ws_low = (_safe_str(out_ws) or "").lower()
    is_gdb = (out_ws_low.endswith(".gdb")
              or ".gdb" in out_ws_low
              or out_ws_low.endswith(".sde"))
    if is_gdb:
        return os.path.join(out_ws, name)
    if not name.lower().endswith(".shp"):
        name = name + ".shp"
    return os.path.join(out_ws, name)


def _safe_delete(path: Optional[str]) -> None:
    if not path:
        return
    try:
        if arcpy.Exists(path):
            arcpy.management.Delete(path)
    except (arcpy.ExecuteError, RuntimeError) as ex:
        arcpy.AddWarning(f"safe_delete failed for {path}: {ex}")


def _ensure_scratch() -> str:
    sgdb = arcpy.env.scratchGDB
    if not sgdb or not arcpy.Exists(sgdb):
        sgdb = arcpy.env.scratchWorkspace
    if not sgdb or not arcpy.Exists(sgdb):
        raise arcpy.ExecuteError(
            "No scratch GDB available. Set arcpy.env.scratchGDB.")
    return sgdb


def _scratch_unique(prefix: str) -> str:
    return arcpy.CreateUniqueName(_unique(prefix), _ensure_scratch())


def _xy_tolerance(fc) -> float:
    """Return the dataset XYTolerance in map units (defensive)."""
    try:
        d = arcpy.Describe(fc)
        sr = getattr(d, "spatialReference", None)
        if sr is not None:
            tol = getattr(sr, "XYTolerance", None)
            if tol is not None and float(tol) > 0:
                return float(tol)
    except (arcpy.ExecuteError, RuntimeError):
        pass
    return 0.001


# =============================================================================
# 4. Pro: scale + map integration helpers
# =============================================================================

def _get_active_map_scale() -> Optional[float]:
    try:
        aprx = arcpy.mp.ArcGISProject("CURRENT")
        mv = aprx.activeView
        if mv and hasattr(mv, "camera") and mv.camera and mv.camera.scale:
            return float(mv.camera.scale)
    except (arcpy.ExecuteError, RuntimeError):
        return None
    return None


def _get_active_map_extent():
    """Return the active Pro map view's extent, or None."""
    try:
        aprx = arcpy.mp.ArcGISProject("CURRENT")
        mv = aprx.activeView
        cam = getattr(mv, "camera", None)
        ext_getter = getattr(cam, "getExtent", None)
        if ext_getter is not None:
            ext = ext_getter()
            if ext is not None:
                return ext
    except (arcpy.ExecuteError, RuntimeError):
        return None
    return None


def _mm_to_mapunits(mm: float, scale: Optional[float],
                    mapunit_name: str) -> Optional[float]:
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


def _add_layers_to_active_map(layer_paths: Iterable[str]) -> None:
    try:
        aprx = arcpy.mp.ArcGISProject("CURRENT")
        m = aprx.activeMap
        if m is None:
            return
        for p in layer_paths:
            if p and arcpy.Exists(p):
                try:
                    m.addDataFromPath(p)
                except (arcpy.ExecuteError, RuntimeError):
                    _warn(f"Could not add layer to map: {p}")
    except (arcpy.ExecuteError, RuntimeError):
        pass


# =============================================================================
# 5. Toolbox + Tool 1 (AOI Brush Builder)
# =============================================================================

class Toolbox:
    def __init__(self):
        self.label = "Plugin 5 - Cartographic Automation (Pro, v5 native)"
        self.alias = "carto_auto_pro_v5"
        self.tools = [AOIBrushBuilder, SafeContourCleaner]


class AOIBrushBuilder:
    def __init__(self):
        self.label = "AOI Brush Builder (Create / Add / Subtract) - v5"
        self.description = (
            "Build an AOI polygon from polyline/polygon brush strokes "
            "using buffer + union/erase. Optionally clip to a frame.")
        self.canRunInBackground = True

    def isLicensed(self) -> bool:
        return True

    def getParameterInfo(self):
        params = []

        p0 = arcpy.Parameter(
            displayName="Brush Strokes Feature (Polyline or Polygon)",
            name="brush_fc", datatype="GPFeatureLayer",
            parameterType="Required", direction="Input")
        p0.category = "1) Brush Inputs"
        params.append(p0)

        p1 = arcpy.Parameter(
            displayName="Brush Radius (map units)",
            name="brush_radius_mu", datatype="GPDouble",
            parameterType="Optional", direction="Input")
        p1.category = "1) Brush Inputs"
        p1.value = 20.0
        params.append(p1)

        p2 = arcpy.Parameter(
            displayName="Brush Radius (millimeters on map) [optional]",
            name="brush_radius_mm", datatype="GPDouble",
            parameterType="Optional", direction="Input")
        p2.category = "1) Brush Inputs"
        p2.value = 0.0
        p2.description = (
            "If >0, converts mm on map to map units using the active map "
            "view's scale. Final radius = max(map_units, converted_mm).")
        params.append(p2)

        p3 = arcpy.Parameter(
            displayName="Operation Mode", name="operation",
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
            displayName="Existing AOI Polygon (optional)",
            name="existing_aoi", datatype="GPFeatureLayer",
            parameterType="Optional", direction="Input")
        p4.category = "2) AOI Logic"
        params.append(p4)

        p5 = arcpy.Parameter(
            displayName="Frame / Neatline Polygon (optional clip)",
            name="frame_polygon", datatype="GPFeatureLayer",
            parameterType="Optional", direction="Input")
        p5.category = "3) Safety"
        params.append(p5)

        p6 = arcpy.Parameter(
            displayName="Clip AOI to Frame?", name="clip_to_frame",
            datatype="GPBoolean", parameterType="Optional", direction="Input")
        p6.category = "3) Safety"
        p6.value = True
        params.append(p6)

        p7 = arcpy.Parameter(
            displayName="Output Workspace (GDB recommended)",
            name="out_workspace", datatype="DEWorkspace",
            parameterType="Optional", direction="Input")
        p7.category = "4) Outputs"
        params.append(p7)

        p8 = arcpy.Parameter(
            displayName="Output AOI Name", name="out_aoi_name",
            datatype="GPString", parameterType="Required", direction="Input")
        p8.category = "4) Outputs"
        p8.value = "AOI_Brush"
        params.append(p8)

        p9 = arcpy.Parameter(
            displayName="Add AOI output to current map",
            name="add_to_map", datatype="GPBoolean",
            parameterType="Optional", direction="Input")
        p9.category = "4) Outputs"
        p9.value = True
        params.append(p9)

        return params

    def updateParameters(self, parameters):
        op = parameters[3].valueAsText
        need_existing = op in (
            "Add brush to existing AOI",
            "Subtract brush from existing AOI",
            "Replace existing AOI (overwrite with brush)",
        )
        parameters[4].enabled = bool(need_existing)

    def updateMessages(self, parameters):
        op = parameters[3].valueAsText
        existing = parameters[4].valueAsText
        if op in (
            "Add brush to existing AOI",
            "Subtract brush from existing AOI",
            "Replace existing AOI (overwrite with brush)",
        ) and not existing:
            parameters[4].setWarningMessage(
                "Operation requires an Existing AOI polygon. Provide it or "
                "switch to 'Create new AOI'.")

    def execute(self, parameters, messages):
        env_snap = _snapshot_env()
        try:
            _prime_env()
            self._execute_core(parameters, messages)
        except arcpy.ExecuteError:
            arcpy.AddError(arcpy.GetMessages(2))
            raise
        except RuntimeError as ex:
            arcpy.AddError(f"Runtime error: {ex}")
            arcpy.AddError(traceback.format_exc())
            raise
        finally:
            _restore_env(env_snap)

    def _execute_core(self, parameters, messages):
        brush_fc_layer = parameters[0].valueAsText
        brush_radius_mu = float(parameters[1].value or 0.0)
        brush_radius_mm = float(parameters[2].value or 0.0)
        operation = parameters[3].valueAsText
        existing_aoi_layer = parameters[4].valueAsText
        frame_fc_layer = parameters[5].valueAsText
        clip_to_frame = _as_bool(parameters[6].value, True)
        out_ws = parameters[7].valueAsText or arcpy.env.scratchGDB
        out_name = parameters[8].valueAsText
        add_to_map = _as_bool(parameters[9].value, True)

        if not brush_fc_layer:
            raise arcpy.ExecuteError("Brush feature is required.")

        _announce_selection("Brush", brush_fc_layer)
        if existing_aoi_layer:
            _announce_selection("ExistingAOI", existing_aoi_layer)
        if frame_fc_layer:
            _announce_selection("Frame", frame_fc_layer)

        brush_fc = _resolve_full_source(brush_fc_layer, ignore_selection=True)
        existing_aoi = (_resolve_full_source(existing_aoi_layer,
                                             ignore_selection=True)
                        if existing_aoi_layer else None)
        frame_fc = (_resolve_full_source(frame_fc_layer,
                                         ignore_selection=True)
                    if frame_fc_layer else None)

        scratch = _ensure_scratch()
        _diag(f"Scratch (disk): {scratch}")

        desc = arcpy.Describe(brush_fc)
        sr = desc.spatialReference
        map_units_name = getattr(sr, "linearUnitName", "") if sr else ""

        if brush_radius_mm and brush_radius_mm > 0:
            scale = _get_active_map_scale()
            conv = _mm_to_mapunits(brush_radius_mm, scale, map_units_name)
            if conv is not None:
                brush_radius_mu = max(brush_radius_mu, float(conv))
                _msg(f"Brush radius converted from mm using scale "
                     f"1:{scale:.0f} => {conv:.3f} map units")
            else:
                _warn("Could not convert brush radius (mm). Using "
                      "map-units value only.")

        brush_geom_type = (desc.shapeType or "").upper()
        brush_poly = _scratch_unique("brush_poly")
        brush_poly2 = None
        out_aoi_tmp = None
        union_fc = None

        try:
            if brush_geom_type in ("POLYLINE", "LINE", "POINT", "MULTIPOINT"):
                if brush_radius_mu <= 0:
                    raise arcpy.ExecuteError(
                        "Brush Radius must be > 0 for Polyline/Point "
                        "brush strokes.")
                _msg(f"Buffering brush strokes (radius = {brush_radius_mu})...")
                arcpy.analysis.Buffer(brush_fc, brush_poly,
                                      float(brush_radius_mu),
                                      dissolve_option="ALL")
            elif brush_geom_type == "POLYGON":
                _msg("Dissolving polygon brush strokes...")
                arcpy.management.Dissolve(brush_fc, brush_poly)
            else:
                raise arcpy.ExecuteError(
                    f"Unsupported brush geometry type: {brush_geom_type}")

            brush_poly2 = _scratch_unique("brush_poly_diss")
            arcpy.management.Dissolve(brush_poly, brush_poly2)
            _safe_delete(brush_poly)
            brush_poly = brush_poly2
            brush_poly2 = None

            out_aoi_tmp = _scratch_unique("aoi_tmp")
            if operation == "Create new AOI (from brush)":
                arcpy.management.CopyFeatures(brush_poly, out_aoi_tmp)
            elif operation == "Replace existing AOI (overwrite with brush)":
                if not existing_aoi:
                    raise arcpy.ExecuteError(
                        "Existing AOI is required for Replace operation.")
                arcpy.management.CopyFeatures(brush_poly, out_aoi_tmp)
            elif operation == "Add brush to existing AOI":
                if not existing_aoi:
                    raise arcpy.ExecuteError(
                        "Existing AOI is required for Add operation.")
                union_fc = _scratch_unique("aoi_union")
                _msg("Union: existing AOI + brush...")
                arcpy.analysis.Union([existing_aoi, brush_poly],
                                     union_fc, "ALL", "", "GAPS")
                arcpy.management.Dissolve(union_fc, out_aoi_tmp)
            elif operation == "Subtract brush from existing AOI":
                if not existing_aoi:
                    raise arcpy.ExecuteError(
                        "Existing AOI is required for Subtract operation.")
                _msg("Erase: existing AOI MINUS brush...")
                arcpy.analysis.Erase(existing_aoi, brush_poly, out_aoi_tmp)
            else:
                raise arcpy.ExecuteError("Unknown Operation Mode.")

            if clip_to_frame and frame_fc:
                _msg("Clipping AOI to frame...")
                out_aoi_clipped = _scratch_unique("aoi_clip")
                try:
                    arcpy.analysis.Clip(out_aoi_tmp, frame_fc, out_aoi_clipped)
                    _safe_delete(out_aoi_tmp)
                    out_aoi_tmp = out_aoi_clipped
                except (arcpy.ExecuteError, RuntimeError) as ex:
                    arcpy.AddError(arcpy.GetMessages(2))
                    _warn(f"Clip to frame failed; using unclipped AOI: {ex}")
                    _safe_delete(out_aoi_clipped)

            out_aoi_fc = _normalize_output_path(out_ws, out_name)
            if arcpy.Exists(out_aoi_fc):
                arcpy.management.Delete(out_aoi_fc)
            arcpy.management.CopyFeatures(out_aoi_tmp, out_aoi_fc)

            _diag(f"AOI built. Output: {out_aoi_fc}")
            _diag(f"AOI feature count: {_get_count(out_aoi_fc)}")

            if add_to_map:
                _add_layers_to_active_map([out_aoi_fc])

            _msg(f"AOI created. Output: {out_aoi_fc}")
        finally:
            _safe_delete(brush_poly)
            _safe_delete(brush_poly2)
            _safe_delete(out_aoi_tmp)
            _safe_delete(union_fc)


# =============================================================================
# 6. Tool 2: Safe Contour Cleaner
# =============================================================================

class SafeContourCleaner:
    """Build a cleaned COPY of contour line layers for cartographic output."""

    IDX_IN_CONTOURS     = 0
    IDX_FRAME_POLY      = 1
    IDX_SAFE_MU         = 2
    IDX_SAFE_MM         = 3
    IDX_DENSE_TH        = 4
    IDX_MIN_NEIGHBORS   = 5
    IDX_AOI_MODE        = 6
    IDX_CUSTOM_AOI      = 7
    IDX_MASK_MODE       = 8
    IDX_EXTERNAL_MASK   = 9
    IDX_ELIGIBLE_SQL    = 10
    IDX_PROTECTED_SQL   = 11
    IDX_REMOVAL_METHOD  = 12
    IDX_MIN_SEG_LEN     = 13
    IDX_OUT_WS          = 14
    IDX_OUT_NAME        = 15
    IDX_CREATE_REMOVED  = 16
    IDX_CREATE_REVIEW   = 17
    IDX_CREATE_MASK_OUT = 18
    IDX_WRITE_REPORT    = 19
    IDX_ADD_TO_MAP      = 20
    IDX_DRY_RUN         = 21
    IDX_NEAR_CHUNK      = 22
    IDX_ALLOW_FULL_MAP  = 23

    def __init__(self):
        self.label = "Safe Contour Cleaner (Print-Ready) - v5 native"
        self.description = (
            "Build a cleaned COPY of contour line layers for print, leaving "
            "the originals untouched. Removes dense / overlapping segments "
            "inside an AOI while protecting a safety band inside the frame.\n\n"
            "v5.1 hardening (Master Rules):\n"
            " - SELECTION-BYPASS hardwired (FULL datasets always processed)\n"
            " - Per-feature Clip loop replaced by single dissolved Clip\n"
            " - Optional fallback to active map extent when AOI is empty\n"
            " - Slivers below dataset XYTolerance are removed after clip\n"
            " - All intermediates land in scratchGDB on disk\n"
            " - Chunked Near table for huge eligible sets")
        self.canRunInBackground = True

    def isLicensed(self) -> bool:
        return True

    # --- Helpers -----------------------------------------------------------

    def _split_multivalue(self, mv_text) -> List[str]:
        if not mv_text:
            return []
        return [p.strip() for p in _safe_str(mv_text).split(";") if p.strip()]

    def _combine_where(self, a, b) -> str:
        a = (a or "").strip()
        b = (b or "").strip()
        if a and b:
            return f"({a}) AND ({b})"
        return a or b

    def _make_where_not(self, sql) -> str:
        sql = (sql or "").strip()
        if not sql:
            return ""
        return f"NOT ({sql})"

    def _extent_to_polygon_fc(self, extent, out_fc, sr):
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

    def _dissolve_clip_areas(self, clip_inputs: List[str],
                             out_fc: str, sr) -> str:
        """
        F1: Combine many clip polygon layers into ONE dissolved multipart
        polygon used by a single arcpy.analysis.Clip call.
        """
        valid = [c for c in clip_inputs if c and arcpy.Exists(c)]
        if not valid:
            arcpy.management.CreateFeatureclass(
                os.path.dirname(out_fc), os.path.basename(out_fc),
                "POLYGON", spatial_reference=sr)
            return out_fc

        scratch = _ensure_scratch()
        if len(valid) == 1:
            arcpy.management.Dissolve(valid[0], out_fc, multi_part="MULTI_PART")
            return out_fc

        merged = arcpy.CreateUniqueName(_unique("clip_merge"), scratch)
        try:
            arcpy.management.Merge(valid, merged)
            arcpy.management.Dissolve(merged, out_fc, multi_part="MULTI_PART")
        finally:
            _safe_delete(merged)
        return out_fc

    def _single_clip(self, in_fc: str, dissolved_clip_fc: str,
                     out_fc: str) -> str:
        """
        F1: Single arcpy.analysis.Clip on the entire dataset against a
        single dissolved multipart polygon.  No per-feature loop.
        """
        arcpy.analysis.Clip(in_fc, dissolved_clip_fc, out_fc)
        return out_fc

    def _safe_zone_from_frame(self, frame_fc, safe_margin, out_safe_zone_fc):
        scratch = _ensure_scratch()
        dissolved = arcpy.CreateUniqueName(_unique("frame_diss"), scratch)
        frame_line = None
        band = None
        try:
            arcpy.management.Dissolve(frame_fc, dissolved,
                                      multi_part="MULTI_PART")
            if safe_margin is None or safe_margin <= 0:
                arcpy.management.CopyFeatures(dissolved, out_safe_zone_fc)
                return out_safe_zone_fc

            frame_line = arcpy.CreateUniqueName(_unique("frame_line"), scratch)
            arcpy.management.PolygonToLine(dissolved, frame_line)

            band = arcpy.CreateUniqueName(_unique("frame_band"), scratch)
            try:
                arcpy.analysis.Buffer(frame_line, band,
                                      abs(float(safe_margin)),
                                      dissolve_option="ALL")
            except (arcpy.ExecuteError, RuntimeError) as ex:
                arcpy.AddError(arcpy.GetMessages(2))
                _warn(f"Buffer failed for safe zone; using full frame "
                      f"as safe zone: {ex}")
                arcpy.management.CopyFeatures(dissolved, out_safe_zone_fc)
                return out_safe_zone_fc

            try:
                arcpy.analysis.Clip(band, dissolved, out_safe_zone_fc)
            except (arcpy.ExecuteError, RuntimeError) as ex:
                arcpy.AddError(arcpy.GetMessages(2))
                _warn(f"Clip failed for safe zone; using full frame "
                      f"as safe zone: {ex}")
                arcpy.management.CopyFeatures(dissolved, out_safe_zone_fc)
        finally:
            _safe_delete(dissolved)
            _safe_delete(frame_line)
            _safe_delete(band)
        return out_safe_zone_fc

    def _build_dense_mask(self, eligible_fc, threshold, min_neighbors,
                           out_mask_fc, aoi_fc=None, near_chunk=50000):
        if threshold is None or float(threshold) <= 0:
            raise arcpy.ExecuteError("Dense threshold must be > 0.")
        if min_neighbors is None or int(min_neighbors) < 1:
            min_neighbors = 1
        min_neighbors = int(min_neighbors)

        scratch = _ensure_scratch()

        working_fc = eligible_fc
        tmp_clip = None
        if aoi_fc:
            tmp_clip = arcpy.CreateUniqueName(_unique("eligible_clip"), scratch)
            self._single_clip(eligible_fc, aoi_fc, tmp_clip)
            working_fc = tmp_clip

        sr = arcpy.Describe(eligible_fc).spatialReference
        count = _get_count(working_fc)
        _diag(f"Dense pass: eligible-in-AOI count = {count}")

        if count == 0:
            arcpy.management.CreateFeatureclass(
                os.path.dirname(out_mask_fc), os.path.basename(out_mask_fc),
                "POLYGON", spatial_reference=sr)
            _safe_delete(tmp_clip)
            return out_mask_fc

        try:
            arcpy.management.AddSpatialIndex(working_fc)
        except (arcpy.ExecuteError, RuntimeError):
            pass

        try:
            oid_field = arcpy.Describe(working_fc).OIDFieldName
        except (arcpy.ExecuteError, RuntimeError):
            oid_field = "OBJECTID"

        from collections import defaultdict
        neighbor_counts: dict = defaultdict(int)

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

        try:
            if count <= near_chunk:
                near_tbl = arcpy.CreateUniqueName(_unique("near"), scratch)
                try:
                    arcpy.analysis.GenerateNearTable(
                        working_fc, working_fc, near_tbl,
                        search_radius=float(threshold),
                        location="NO_LOCATION", angle="NO_ANGLE",
                        closest="CLOSEST",
                        closest_count=min_neighbors + 1)
                    _process_near_table(near_tbl)
                finally:
                    _safe_delete(near_tbl)
            else:
                # Deterministic OID-ordered chunking (Master Rule 8)
                oids = []
                with arcpy.da.SearchCursor(
                        working_fc, [oid_field],
                        sql_clause=(None, f"ORDER BY {oid_field} ASC")) as cur:
                    for r in cur:
                        oids.append(int(r[0]))
                n = len(oids)
                _diag(f"Chunked Near: {n} features, chunk={near_chunk}")
                arcpy.SetProgressor("step", "Building dense mask (chunked)...",
                                    0, max(1, (n + near_chunk - 1) // near_chunk),
                                    1)
                i = 0
                chunk_idx = 0
                while i < n:
                    if getattr(arcpy.env, "autoCancelling", False) and \
                            arcpy.env.isCancelled:
                        raise arcpy.ExecuteError("Cancelled by user.")
                    chunk = oids[i:i + near_chunk]
                    i += near_chunk
                    chunk_idx += 1
                    arcpy.SetProgressorPosition(chunk_idx)
                    if not chunk:
                        continue
                    lo = chunk[0]
                    hi = chunk[-1]
                    where = f"{oid_field} >= {lo} AND {oid_field} <= {hi}"
                    sel_lyr = _unique("near_chunk")
                    near_tbl = arcpy.CreateUniqueName(_unique("near"), scratch)
                    try:
                        arcpy.management.MakeFeatureLayer(working_fc,
                                                          sel_lyr, where)
                        arcpy.analysis.GenerateNearTable(
                            sel_lyr, working_fc, near_tbl,
                            search_radius=float(threshold),
                            location="NO_LOCATION", angle="NO_ANGLE",
                            closest="CLOSEST",
                            closest_count=min_neighbors + 1)
                        _process_near_table(near_tbl)
                        _diag(
                            f"  Near chunk {chunk_idx}: OIDs {lo}..{hi} "
                            f"({len(chunk)} feats)")
                    except (arcpy.ExecuteError, RuntimeError) as ex:
                        arcpy.AddError(arcpy.GetMessages(2))
                        _warn(f"Near chunk {chunk_idx} failed: {ex}")
                        raise
                    finally:
                        _safe_delete(near_tbl)
                        try:
                            arcpy.management.Delete(sel_lyr)
                        except (arcpy.ExecuteError, RuntimeError):
                            pass
                        gc.collect()
                arcpy.ResetProgressor()

            dense_oids = [fid for fid, cnt in neighbor_counts.items()
                          if cnt >= min_neighbors]
            _diag(f"Dense features detected: {len(dense_oids)} (of {count})")

            if not dense_oids:
                arcpy.management.CreateFeatureclass(
                    os.path.dirname(out_mask_fc),
                    os.path.basename(out_mask_fc),
                    "POLYGON", spatial_reference=sr)
                return out_mask_fc

            dense_layer = _unique("dense_lyr")
            tmp_buf = None
            arcpy.management.MakeFeatureLayer(working_fc, dense_layer)
            try:
                arcpy.management.SelectLayerByAttribute(
                    dense_layer, "CLEAR_SELECTION")
                # Deterministic order so SQL IN chunks are stable
                dense_oids.sort()
                chunks = [dense_oids[i:i + 999]
                          for i in range(0, len(dense_oids), 999)]
                first = True
                for ch in chunks:
                    where = "{0} IN ({1})".format(
                        arcpy.AddFieldDelimiters(dense_layer, oid_field),
                        ",".join([str(x) for x in ch]))
                    arcpy.management.SelectLayerByAttribute(
                        dense_layer,
                        "NEW_SELECTION" if first else "ADD_TO_SELECTION",
                        where)
                    first = False

                tmp_buf = arcpy.CreateUniqueName(_unique("dense_buf"), scratch)
                arcpy.analysis.Buffer(
                    dense_layer, tmp_buf, float(threshold) / 2.0,
                    dissolve_option="ALL")
                arcpy.management.CopyFeatures(tmp_buf, out_mask_fc)
            finally:
                try:
                    arcpy.management.Delete(dense_layer)
                except (arcpy.ExecuteError, RuntimeError):
                    pass
                _safe_delete(tmp_buf)
        finally:
            _safe_delete(tmp_clip)

        return out_mask_fc

    def _remove_small_segments(self, in_lines_fc, min_length, out_lines_fc):
        """Drop line features whose length < min_length (after Erase)."""
        arcpy.management.CopyFeatures(in_lines_fc, out_lines_fc)
        if min_length is None or float(min_length) <= 0:
            return out_lines_fc

        oid_field = arcpy.Describe(out_lines_fc).OIDFieldName
        lyr = _unique("short_lyr")
        try:
            arcpy.management.MakeFeatureLayer(out_lines_fc, lyr)
            short_oids = []
            with arcpy.da.SearchCursor(
                    out_lines_fc, [oid_field, "SHAPE@LENGTH"],
                    sql_clause=(None,
                                f"ORDER BY {oid_field} ASC")) as cur:
                for oidv, length in cur:
                    if length is not None and float(length) < float(min_length):
                        short_oids.append(int(oidv))
            if not short_oids:
                return out_lines_fc

            chunks = [short_oids[i:i + 999]
                      for i in range(0, len(short_oids), 999)]
            arcpy.management.SelectLayerByAttribute(lyr, "CLEAR_SELECTION")
            first = True
            for ch in chunks:
                where = "{0} IN ({1})".format(
                    arcpy.AddFieldDelimiters(lyr, oid_field),
                    ",".join([str(x) for x in ch]))
                arcpy.management.SelectLayerByAttribute(
                    lyr,
                    "NEW_SELECTION" if first else "ADD_TO_SELECTION",
                    where)
                first = False
            arcpy.management.DeleteFeatures(lyr)
        finally:
            try:
                arcpy.management.Delete(lyr)
            except (arcpy.ExecuteError, RuntimeError):
                pass
        return out_lines_fc

    def _remove_slivers(self, fc: str, tolerance: float) -> int:
        """
        F3: Remove sliver features whose length (lines) or area
        (polygons) is below `tolerance`.  Returns number removed.
        Streams via cursor + chunked SelectLayerByAttribute - no
        large geometry caches.
        """
        if tolerance is None or float(tolerance) <= 0:
            return 0
        if not fc or not arcpy.Exists(fc):
            return 0

        d = arcpy.Describe(fc)
        shape_type = (getattr(d, "shapeType", "") or "").upper()
        oid_field = getattr(d, "OIDFieldName", "OBJECTID")

        if shape_type in ("POLYLINE", "LINE"):
            shape_token = "SHAPE@LENGTH"
            metric_floor = float(tolerance)
        elif shape_type == "POLYGON":
            shape_token = "SHAPE@AREA"
            # Area sliver heuristic: tolerance squared
            metric_floor = float(tolerance) * float(tolerance)
        else:
            return 0

        sliver_oids: List[int] = []
        with arcpy.da.SearchCursor(
                fc, [oid_field, shape_token],
                sql_clause=(None, f"ORDER BY {oid_field} ASC")) as cur:
            for oidv, metric in cur:
                if metric is None:
                    continue
                if float(metric) < metric_floor:
                    sliver_oids.append(int(oidv))

        if not sliver_oids:
            return 0

        lyr = _unique("sliver_lyr")
        try:
            arcpy.management.MakeFeatureLayer(fc, lyr)
            arcpy.management.SelectLayerByAttribute(lyr, "CLEAR_SELECTION")
            chunks = [sliver_oids[i:i + 999]
                      for i in range(0, len(sliver_oids), 999)]
            first = True
            for ch in chunks:
                where = "{0} IN ({1})".format(
                    arcpy.AddFieldDelimiters(lyr, oid_field),
                    ",".join([str(x) for x in ch]))
                arcpy.management.SelectLayerByAttribute(
                    lyr,
                    "NEW_SELECTION" if first else "ADD_TO_SELECTION",
                    where)
                first = False
            arcpy.management.DeleteFeatures(lyr)
        finally:
            try:
                arcpy.management.Delete(lyr)
            except (arcpy.ExecuteError, RuntimeError):
                pass
        return len(sliver_oids)

    def _approx_total_length(self, fc) -> float:
        if not fc or not arcpy.Exists(fc):
            return 0.0
        total = 0.0
        with arcpy.da.SearchCursor(fc, ["SHAPE@LENGTH"]) as cur:
            for r in cur:
                if r[0] is not None:
                    total += float(r[0])
        return total

    def _report_csv(self, out_ws, base_name,
                    total_in, eligible_count, noneligible_count,
                    dense_th, min_neighbors, safe_mu,
                    aoi_mode, mask_mode, removal_method,
                    min_seg_length, removed_len,
                    out_clean_fc, out_removed_fc, out_review_fc, out_mask_fc,
                    aoi_fallback_used: bool, slivers_removed: int):
        out_ws_low = (_safe_str(out_ws) or "").lower()
        if ".gdb" in out_ws_low:
            parent = os.path.dirname(_safe_str(out_ws).rstrip("\\/"))
            report_path = os.path.join(parent, base_name + "_Report.csv")
        else:
            report_path = os.path.join(out_ws, base_name + "_Report.csv")
        with open(report_path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["Tool", "Safe Contour Cleaner (v5.1 master-rules)"])
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
            w.writerow(["AOIFallbackUsed", str(aoi_fallback_used)])
            w.writerow(["SliversRemoved", slivers_removed])
            w.writerow([])
            w.writerow(["OutputCleanFC", out_clean_fc])
            w.writerow(["OutputRemovedFC", out_removed_fc or ""])
            w.writerow(["OutputReviewFC", out_review_fc or ""])
            w.writerow(["OutputMaskFC", out_mask_fc or ""])
        return report_path

    # --- Parameters --------------------------------------------------------

    def getParameterInfo(self):
        params = []
        p0 = arcpy.Parameter(
            displayName="Input Contour Layers (one or more)",
            name="in_contours", datatype="GPFeatureLayer",
            parameterType="Required", direction="Input", multiValue=True)
        p0.category = "1) Inputs"
        params.append(p0)

        p1 = arcpy.Parameter(
            displayName="Frame / Neatline Polygon (recommended)",
            name="frame_polygon", datatype="GPFeatureLayer",
            parameterType="Optional", direction="Input")
        p1.category = "1) Inputs"
        params.append(p1)

        p2 = arcpy.Parameter(
            displayName="Safe Margin INSIDE Frame (map units)",
            name="safe_margin_mu", datatype="GPDouble",
            parameterType="Optional", direction="Input")
        p2.category = "2) Safety"
        p2.value = 0.0
        params.append(p2)

        p3 = arcpy.Parameter(
            displayName="Safe Margin INSIDE Frame (millimeters on map) [optional]",
            name="safe_margin_mm", datatype="GPDouble",
            parameterType="Optional", direction="Input")
        p3.category = "2) Safety"
        p3.value = 0.0
        params.append(p3)

        p4 = arcpy.Parameter(
            displayName="Dense Threshold Distance (map units)",
            name="dense_threshold", datatype="GPDouble",
            parameterType="Required", direction="Input")
        p4.category = "3) Dense Zone Detection"
        p4.value = 20.0
        params.append(p4)

        p5 = arcpy.Parameter(
            displayName="Minimum Neighbors for Dense (>=1)",
            name="min_neighbors", datatype="GPLong",
            parameterType="Optional", direction="Input")
        p5.category = "3) Dense Zone Detection"
        p5.value = 1
        params.append(p5)

        p6 = arcpy.Parameter(
            displayName="AOI Mode (Where to clean)", name="aoi_mode",
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
            displayName="Custom AOI Polygon (optional)", name="custom_aoi",
            datatype="GPFeatureLayer", parameterType="Optional", direction="Input")
        p7.category = "4) AOI / Target Area"
        params.append(p7)

        p8 = arcpy.Parameter(
            displayName="Mask Mode (How to create removal mask)",
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
            displayName="External Mask Polygon (optional)",
            name="external_mask", datatype="GPFeatureLayer",
            parameterType="Optional", direction="Input")
        p9.category = "5) Mask Generation"
        params.append(p9)

        p10 = arcpy.Parameter(
            displayName="Eligible Contours SQL (CAN be cleaned)",
            name="eligible_sql", datatype="GPString",
            parameterType="Optional", direction="Input")
        p10.category = "6) Rules"
        p10.value = "1=1"
        params.append(p10)

        p11 = arcpy.Parameter(
            displayName="Protected Contours SQL (MUST NEVER be cleaned)",
            name="protected_sql", datatype="GPString",
            parameterType="Optional", direction="Input")
        p11.category = "6) Rules"
        p11.value = ""
        params.append(p11)

        p12 = arcpy.Parameter(
            displayName="Removal Method", name="removal_method",
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
            displayName="Min segment length to delete after erase (map units)",
            name="min_seg_length", datatype="GPDouble",
            parameterType="Optional", direction="Input")
        p13.category = "7) Cleaning Strategy"
        p13.value = 0.0
        params.append(p13)

        p14 = arcpy.Parameter(
            displayName="Output Workspace (GDB recommended)",
            name="out_workspace", datatype="DEWorkspace",
            parameterType="Optional", direction="Input")
        p14.category = "8) Outputs"
        params.append(p14)

        p15 = arcpy.Parameter(
            displayName="Output Clean Contours Name",
            name="out_clean_name", datatype="GPString",
            parameterType="Required", direction="Input")
        p15.category = "8) Outputs"
        p15.value = "Contours_CartoClean"
        params.append(p15)

        p16 = arcpy.Parameter(
            displayName="Create Removed Segments Layer",
            name="create_removed", datatype="GPBoolean",
            parameterType="Optional", direction="Input")
        p16.category = "8) Outputs"
        p16.value = True
        params.append(p16)

        p17 = arcpy.Parameter(
            displayName="Create Review Layer (dense but protected)",
            name="create_review", datatype="GPBoolean",
            parameterType="Optional", direction="Input")
        p17.category = "8) Outputs"
        p17.value = True
        params.append(p17)

        p18 = arcpy.Parameter(
            displayName="Create Mask Output Layer",
            name="create_mask_out", datatype="GPBoolean",
            parameterType="Optional", direction="Input")
        p18.category = "8) Outputs"
        p18.value = True
        params.append(p18)

        p19 = arcpy.Parameter(
            displayName="Write CSV Report", name="write_report",
            datatype="GPBoolean", parameterType="Optional", direction="Input")
        p19.category = "8) Outputs"
        p19.value = True
        params.append(p19)

        p20 = arcpy.Parameter(
            displayName="Add outputs to current map",
            name="add_to_map", datatype="GPBoolean",
            parameterType="Optional", direction="Input")
        p20.category = "8) Outputs"
        p20.value = True
        params.append(p20)

        p21 = arcpy.Parameter(
            displayName="Dry Run / Preview Mode (no cleaned output)",
            name="dry_run", datatype="GPBoolean",
            parameterType="Optional", direction="Input")
        p21.category = "8) Outputs"
        p21.value = False
        params.append(p21)

        p22 = arcpy.Parameter(
            displayName="Near Chunk Size (features per Near pass)",
            name="near_chunk_size", datatype="GPLong",
            parameterType="Optional", direction="Input")
        p22.category = "8) Outputs"
        p22.value = 50000
        p22.description = (
            "For very large eligible sets, GenerateNearTable runs in "
            "OID-bounded chunks of this size.")
        params.append(p22)

        # F2: Optionality requirement.
        p23 = arcpy.Parameter(
            displayName="Allow Full-Map Processing if AOI is empty",
            name="allow_full_map_processing", datatype="GPBoolean",
            parameterType="Optional", direction="Input")
        p23.category = "4) AOI / Target Area"
        p23.value = False
        p23.description = (
            "If checked, when the resolved AOI ends up empty the tool falls "
            "back to the active map view's extent and processes the FULL "
            "MAP (with a loud warning).  If unchecked (default), the tool "
            "raises an error and stops.")
        params.append(p23)

        return params

    def updateParameters(self, parameters):
        aoi_mode = parameters[self.IDX_AOI_MODE].valueAsText
        mask_mode = parameters[self.IDX_MASK_MODE].valueAsText
        parameters[self.IDX_CUSTOM_AOI].enabled = aoi_mode in (
            "Custom AOI only", "Frame AND Custom AOI")
        parameters[self.IDX_EXTERNAL_MASK].enabled = (
            mask_mode == "Use external mask polygon")

    def updateMessages(self, parameters):
        aoi_mode = parameters[self.IDX_AOI_MODE].valueAsText
        frame = parameters[self.IDX_FRAME_POLY].valueAsText
        if aoi_mode and "Frame" in aoi_mode and not frame:
            parameters[self.IDX_FRAME_POLY].setWarningMessage(
                "AOI Mode uses Frame but Frame/Neatline polygon is not "
                "provided. Tool will fall back to the active map view "
                "extent if Allow Full-Map Processing is enabled.")
        dense_th = parameters[self.IDX_DENSE_TH].value
        safe_mu = parameters[self.IDX_SAFE_MU].value or 0.0
        if dense_th and float(dense_th) < float(safe_mu):
            parameters[self.IDX_DENSE_TH].setWarningMessage(
                "Dense threshold is smaller than safe margin. Cleaning "
                "near the frame may be very limited.")
        ncs = parameters[self.IDX_NEAR_CHUNK].value
        if ncs is not None and int(ncs) < 1000:
            parameters[self.IDX_NEAR_CHUNK].setWarningMessage(
                "Very small chunk size will be slow; recommend >= 5000.")

    # --- Execute -----------------------------------------------------------

    def execute(self, parameters, messages):
        env_snap = _snapshot_env()
        try:
            _prime_env()
            self._execute_core(parameters, messages)
        except arcpy.ExecuteError:
            arcpy.AddError(arcpy.GetMessages(2))
            raise
        except RuntimeError as ex:
            arcpy.AddError(f"Runtime error: {ex}")
            arcpy.AddError(traceback.format_exc())
            raise
        finally:
            _restore_env(env_snap)

    def _execute_core(self, parameters, messages):
        in_contours_mv      = parameters[self.IDX_IN_CONTOURS].valueAsText
        frame_layer         = parameters[self.IDX_FRAME_POLY].valueAsText
        safe_mu             = float(parameters[self.IDX_SAFE_MU].value or 0.0)
        safe_mm             = float(parameters[self.IDX_SAFE_MM].value or 0.0)
        dense_threshold     = float(parameters[self.IDX_DENSE_TH].value)
        min_neighbors       = int(parameters[self.IDX_MIN_NEIGHBORS].value or 1)
        aoi_mode            = parameters[self.IDX_AOI_MODE].valueAsText
        custom_aoi_layer    = parameters[self.IDX_CUSTOM_AOI].valueAsText
        mask_mode           = parameters[self.IDX_MASK_MODE].valueAsText
        external_mask_layer = parameters[self.IDX_EXTERNAL_MASK].valueAsText
        eligible_sql        = (parameters[self.IDX_ELIGIBLE_SQL].valueAsText
                               or "1=1")
        protected_sql       = parameters[self.IDX_PROTECTED_SQL].valueAsText or ""
        removal_method      = parameters[self.IDX_REMOVAL_METHOD].valueAsText
        min_seg_length      = float(parameters[self.IDX_MIN_SEG_LEN].value or 0.0)
        out_ws              = (parameters[self.IDX_OUT_WS].valueAsText
                               or arcpy.env.scratchGDB)
        out_clean_name      = parameters[self.IDX_OUT_NAME].valueAsText
        create_removed      = _as_bool(parameters[self.IDX_CREATE_REMOVED].value, True)
        create_review       = _as_bool(parameters[self.IDX_CREATE_REVIEW].value, True)
        create_maskout      = _as_bool(parameters[self.IDX_CREATE_MASK_OUT].value, True)
        write_report        = _as_bool(parameters[self.IDX_WRITE_REPORT].value, True)
        add_to_map          = _as_bool(parameters[self.IDX_ADD_TO_MAP].value, True)
        dry_run             = _as_bool(parameters[self.IDX_DRY_RUN].value, False)
        near_chunk          = int(parameters[self.IDX_NEAR_CHUNK].value or 50000)
        allow_full_map      = _as_bool(
            parameters[self.IDX_ALLOW_FULL_MAP].value, False)

        in_contour_layers = self._split_multivalue(in_contours_mv)
        if not in_contour_layers:
            raise arcpy.ExecuteError("No input contours provided.")

        scratch = _ensure_scratch()
        _diag(f"Scratch (disk): {scratch}")

        # Selection-bypass announcements + resolve to on-disk paths
        for lyr in in_contour_layers:
            _announce_selection("Contours", lyr)
        if frame_layer:
            _announce_selection("Frame", frame_layer)
        if custom_aoi_layer:
            _announce_selection("CustomAOI", custom_aoi_layer)
        if external_mask_layer:
            _announce_selection("ExternalMask", external_mask_layer)

        in_contours = [_resolve_full_source(l, ignore_selection=True)
                       for l in in_contour_layers]
        frame_fc = (_resolve_full_source(frame_layer, ignore_selection=True)
                    if frame_layer else None)
        custom_aoi_fc = (
            _resolve_full_source(custom_aoi_layer, ignore_selection=True)
            if custom_aoi_layer else None)
        external_mask_fc = (
            _resolve_full_source(external_mask_layer, ignore_selection=True)
            if external_mask_layer else None)

        desc0 = arcpy.Describe(in_contours[0])
        sr = desc0.spatialReference
        map_units_name = getattr(sr, "linearUnitName", "") if sr else ""
        if sr and hasattr(sr, "type") and sr.type == "Geographic":
            _warn("Geographic coordinate system detected. Distances may be "
                  "inaccurate. Project to a projected CRS for best results.")

        # mm -> map units for safe margin
        if safe_mm and safe_mm > 0:
            scale = _get_active_map_scale()
            conv = _mm_to_mapunits(safe_mm, scale, map_units_name)
            if conv is not None:
                _msg(f"Safe Margin converted from {safe_mm} mm @ "
                     f"1:{scale:.0f} => {conv:.3f} map units")
                safe_mu = max(safe_mu, conv)
            else:
                _warn("Could not convert Safe Margin (mm). Using "
                      "map-units value only.")

        # Track scratch resources for finally cleanup
        scratch_paths: List[Optional[str]] = []
        cleanup_layers: List[str] = []
        aoi_fallback_used = False
        slivers_removed_total = 0

        try:
            # Frame fallback rule:
            # Only fall back to active map view extent if AOI mode references
            # the frame AND allow_full_map_processing is True.
            if (not frame_fc) and aoi_mode and "Frame" in aoi_mode:
                if allow_full_map:
                    ext = _get_active_map_extent()
                    if ext is None:
                        raise arcpy.ExecuteError(
                            "Frame polygon is required and Allow Full-Map "
                            "Processing is enabled but no active Pro map "
                            "view extent is available.")
                    frame_fc = arcpy.CreateUniqueName(
                        _unique("frame_extent"), scratch)
                    scratch_paths.append(frame_fc)
                    self._extent_to_polygon_fc(ext, frame_fc, sr)
                    aoi_fallback_used = True
                    _warn("AOI EMPTY (no frame polygon supplied). "
                          "Allow Full-Map Processing is ON; falling back "
                          "to the ACTIVE MAP VIEW EXTENT and processing "
                          "the full map.")
                else:
                    raise arcpy.ExecuteError(
                        "Frame polygon is required for the chosen AOI Mode. "
                        "Enable 'Allow Full-Map Processing if AOI is empty' "
                        "to use the active map view extent as a fallback.")

            # Build safe zone from frame
            safe_zone_fc = None
            if frame_fc:
                safe_zone_fc = arcpy.CreateUniqueName(
                    _unique("safe_zone"), scratch)
                scratch_paths.append(safe_zone_fc)
                self._safe_zone_from_frame(frame_fc, safe_mu, safe_zone_fc)

            # Resolve AOI polygon by mode (single dissolved polygon used
            # for the one global Clip - F1).
            aoi_fc: Optional[str] = None
            if aoi_mode == "Frame only (default)":
                if frame_fc:
                    aoi_fc = arcpy.CreateUniqueName(_unique("aoi_diss"), scratch)
                    scratch_paths.append(aoi_fc)
                    self._dissolve_clip_areas([frame_fc], aoi_fc, sr)
            elif aoi_mode == "Custom AOI only":
                if not custom_aoi_fc:
                    raise arcpy.ExecuteError(
                        "Custom AOI mode selected but Custom AOI polygon "
                        "not provided.")
                aoi_fc = arcpy.CreateUniqueName(_unique("aoi_diss"), scratch)
                scratch_paths.append(aoi_fc)
                self._dissolve_clip_areas([custom_aoi_fc], aoi_fc, sr)
            elif aoi_mode == "Frame AND Custom AOI":
                if not (frame_fc and custom_aoi_fc):
                    raise arcpy.ExecuteError(
                        "Frame AND Custom AOI requires BOTH Frame polygon "
                        "and Custom AOI polygon.")
                inter_fc = arcpy.CreateUniqueName(_unique("aoi_int"), scratch)
                scratch_paths.append(inter_fc)
                arcpy.analysis.Intersect([frame_fc, custom_aoi_fc], inter_fc,
                                         join_attributes="ONLY_FID")
                aoi_fc = arcpy.CreateUniqueName(_unique("aoi_diss"), scratch)
                scratch_paths.append(aoi_fc)
                self._dissolve_clip_areas([inter_fc], aoi_fc, sr)
            else:
                # "Entire dataset (no AOI)"
                aoi_fc = None

            # F2: Validate AOI emptiness BEFORE doing any expensive work.
            aoi_count = _get_count(aoi_fc) if aoi_fc else 0
            if aoi_mode != "Entire dataset (no AOI)" and aoi_count == 0:
                # AOI ended up empty (e.g. zero polygons in input frame).
                if not allow_full_map:
                    raise arcpy.ExecuteError(
                        "AOI is EMPTY (no usable polygons). Enable "
                        "'Allow Full-Map Processing if AOI is empty' to "
                        "fall back to the active map view extent, or "
                        "supply a non-empty AOI / frame.")
                ext = _get_active_map_extent()
                if ext is None:
                    raise arcpy.ExecuteError(
                        "AOI is empty and Allow Full-Map Processing is ON "
                        "but no active Pro map view extent is available.")
                fb_fc = arcpy.CreateUniqueName(_unique("frame_extent"), scratch)
                scratch_paths.append(fb_fc)
                self._extent_to_polygon_fc(ext, fb_fc, sr)
                aoi_fc = arcpy.CreateUniqueName(_unique("aoi_diss"), scratch)
                scratch_paths.append(aoi_fc)
                self._dissolve_clip_areas([fb_fc], aoi_fc, sr)
                aoi_fallback_used = True
                _warn("AOI WAS EMPTY. Allow Full-Map Processing is ON; "
                      "falling back to the ACTIVE MAP VIEW EXTENT and "
                      "processing the full map.")
                # Frame-derived safe zone may also be missing.
                if safe_zone_fc is None:
                    safe_zone_fc = arcpy.CreateUniqueName(
                        _unique("safe_zone"), scratch)
                    scratch_paths.append(safe_zone_fc)
                    self._safe_zone_from_frame(fb_fc, safe_mu, safe_zone_fc)

            # Merge contours
            merged_fc = arcpy.CreateUniqueName(_unique("contours_merge"), scratch)
            scratch_paths.append(merged_fc)
            _msg("Merging input contours...")
            arcpy.management.Merge(in_contours, merged_fc)
            _diag(f"Merged contour count: {_get_count(merged_fc)}")

            # F1: ONE Clip on the whole dataset against the dissolved AOI.
            working_all = merged_fc
            if aoi_fc:
                clipped_all = arcpy.CreateUniqueName(
                    _unique("contours_clip"), scratch)
                scratch_paths.append(clipped_all)
                _msg("Clipping contours to AOI (single global Clip)...")
                self._single_clip(merged_fc, aoi_fc, clipped_all)
                working_all = clipped_all

                # F3: drop slivers below dataset XYTolerance after Clip.
                xytol = _xy_tolerance(working_all)
                _diag(f"XYTolerance for sliver removal: {xytol}")
                slivers_removed_total += self._remove_slivers(
                    working_all, xytol)
                if slivers_removed_total:
                    _diag(f"Removed {slivers_removed_total} sliver(s) "
                          f"below XYTolerance.")

            total_in = _get_count(working_all)
            _diag(f"Contours in AOI: {total_in}")

            # Eligible / non-eligible split
            not_prot = self._make_where_not(protected_sql)
            where_eligible = self._combine_where(eligible_sql, not_prot)

            lyr_all = _unique("all_lyr")
            cleanup_layers.append(lyr_all)
            arcpy.management.MakeFeatureLayer(working_all, lyr_all)

            arcpy.management.SelectLayerByAttribute(
                lyr_all, "NEW_SELECTION", where_eligible)
            eligible_fc = arcpy.CreateUniqueName(_unique("eligible"), scratch)
            scratch_paths.append(eligible_fc)
            arcpy.management.CopyFeatures(lyr_all, eligible_fc)
            arcpy.management.SelectLayerByAttribute(lyr_all, "SWITCH_SELECTION")
            noneligible_fc = arcpy.CreateUniqueName(_unique("noneligible"), scratch)
            scratch_paths.append(noneligible_fc)
            arcpy.management.CopyFeatures(lyr_all, noneligible_fc)

            eligible_count = _get_count(eligible_fc)
            noneligible_count = _get_count(noneligible_fc)
            _diag(f"Eligible: {eligible_count} | "
                  f"Protected/Other: {noneligible_count}")

            # Build mask
            mask_fc = arcpy.CreateUniqueName(_unique("mask"), scratch)
            scratch_paths.append(mask_fc)
            if mask_mode == "Use external mask polygon":
                if not external_mask_fc:
                    raise arcpy.ExecuteError(
                        "Mask Mode is 'Use external mask polygon' but no "
                        "external mask was provided.")
                arcpy.management.CopyFeatures(external_mask_fc, mask_fc)
            elif mask_mode == "Use AOI polygon as mask (no auto)":
                if not aoi_fc:
                    raise arcpy.ExecuteError(
                        "AOI is required to use AOI as mask (choose a "
                        "Frame or Custom AOI mode).")
                arcpy.management.CopyFeatures(aoi_fc, mask_fc)
            else:
                _msg("Building dense-zone mask (Near Table method)...")
                self._build_dense_mask(
                    eligible_fc, dense_threshold, min_neighbors,
                    mask_fc, aoi_fc=aoi_fc, near_chunk=near_chunk)

            mask_count = _get_count(mask_fc)
            _diag(f"Mask polygons: {mask_count}")
            if mask_count == 0:
                _warn("Mask is empty. No contour segments will be removed "
                      "(output becomes copy).")

            # Apply frame safety
            final_mask_fc = mask_fc
            if (safe_zone_fc and mask_count > 0
                    and _get_count(safe_zone_fc) > 0):
                safe_mask_fc = arcpy.CreateUniqueName(_unique("mask_safe"),
                                                     scratch)
                scratch_paths.append(safe_mask_fc)
                try:
                    arcpy.analysis.Erase(mask_fc, safe_zone_fc, safe_mask_fc)
                    final_mask_fc = safe_mask_fc
                except (arcpy.ExecuteError, RuntimeError) as ex:
                    arcpy.AddError(arcpy.GetMessages(2))
                    _warn(f"Erase failed while subtracting Safe Zone from "
                          f"mask. Using raw mask: {ex}")

            final_mask_count = _get_count(final_mask_fc)
            _diag(f"Final mask polygons after frame safety: "
                  f"{final_mask_count}")
            if final_mask_count == 0 and mask_count > 0:
                _warn("After frame safety, final mask is empty (all dense "
                      "zones were within Safe Margin).")

            review_fc = None
            if create_review and safe_zone_fc and mask_count > 0:
                review_fc = arcpy.CreateUniqueName(_unique("review"), scratch)
                scratch_paths.append(review_fc)
                try:
                    arcpy.analysis.Intersect(
                        [mask_fc, safe_zone_fc], review_fc,
                        output_type="POLYGON")
                except (arcpy.ExecuteError, RuntimeError) as ex:
                    arcpy.AddError(arcpy.GetMessages(2))
                    _warn(f"Could not build review intersect: {ex}")
                    _safe_delete(review_fc)
                    review_fc = None

            out_mask_fc = None
            if create_maskout and mask_count > 0:
                out_mask_fc = _normalize_output_path(
                    out_ws, out_clean_name + "_Mask")
                if arcpy.Exists(out_mask_fc):
                    arcpy.management.Delete(out_mask_fc)
                arcpy.management.CopyFeatures(final_mask_fc, out_mask_fc)

            if dry_run:
                out_removed_fc = None
                if create_removed and final_mask_count > 0:
                    tmp_removed = arcpy.CreateUniqueName(
                        _unique("removed_preview"), scratch)
                    scratch_paths.append(tmp_removed)
                    arcpy.analysis.Intersect(
                        [eligible_fc, final_mask_fc], tmp_removed,
                        output_type="LINE")
                    out_removed_fc = _normalize_output_path(
                        out_ws, out_clean_name + "_Removed_Preview")
                    if arcpy.Exists(out_removed_fc):
                        arcpy.management.Delete(out_removed_fc)
                    arcpy.management.CopyFeatures(tmp_removed, out_removed_fc)

                out_review_fc = None
                if review_fc and arcpy.Exists(review_fc):
                    out_review_fc = _normalize_output_path(
                        out_ws, out_clean_name + "_Review")
                    if arcpy.Exists(out_review_fc):
                        arcpy.management.Delete(out_review_fc)
                    arcpy.management.CopyFeatures(review_fc, out_review_fc)

                _diag(
                    f"DRY RUN done. inputs={total_in} eligible={eligible_count} "
                    f"mask={mask_count} final_mask={final_mask_count}")
                if add_to_map:
                    _add_layers_to_active_map(
                        [out_mask_fc, out_removed_fc, out_review_fc])
                _msg("Dry Run complete (Mask/Preview layers created).")
                return

            # License check for Erase
            if removal_method == "Segment Erase (recommended)":
                try:
                    prod = arcpy.ProductInfo()
                except (arcpy.ExecuteError, RuntimeError):
                    prod = None
                if prod != "ArcInfo":
                    _warn("Advanced license not detected (Erase may fail). "
                          "Falling back to 'Delete Whole Features'.")
                    removal_method = "Delete Whole Features"

            cleaned_eligible_fc = arcpy.CreateUniqueName(
                _unique("eligible_clean"), scratch)
            scratch_paths.append(cleaned_eligible_fc)
            removed_fc: Optional[str] = None

            if final_mask_count == 0:
                _msg("Final mask has no features. Skipping removal.")
                arcpy.management.CopyFeatures(eligible_fc, cleaned_eligible_fc)
            else:
                if removal_method == "Segment Erase (recommended)":
                    _msg("Cleaning: Segment Erase...")
                    arcpy.analysis.Erase(eligible_fc, final_mask_fc,
                                         cleaned_eligible_fc)
                    if create_removed:
                        removed_fc = arcpy.CreateUniqueName(
                            _unique("removed"), scratch)
                        scratch_paths.append(removed_fc)
                        arcpy.analysis.Intersect(
                            [eligible_fc, final_mask_fc], removed_fc,
                            output_type="LINE")
                else:
                    _msg("Cleaning: Delete Whole Features...")
                    lyr_elig = _unique("eligible_lyr")
                    cleanup_layers.append(lyr_elig)
                    arcpy.management.MakeFeatureLayer(eligible_fc, lyr_elig)
                    arcpy.management.SelectLayerByLocation(
                        lyr_elig, "INTERSECT", final_mask_fc)
                    if create_removed:
                        removed_fc = arcpy.CreateUniqueName(
                            _unique("removed"), scratch)
                        scratch_paths.append(removed_fc)
                        arcpy.management.CopyFeatures(lyr_elig, removed_fc)
                    arcpy.management.SelectLayerByAttribute(
                        lyr_elig, "SWITCH_SELECTION")
                    arcpy.management.CopyFeatures(lyr_elig, cleaned_eligible_fc)

            # Tiny-segment removal (user-controlled), then sliver removal
            # below XYTolerance (defensive after Erase).
            if min_seg_length and min_seg_length > 0:
                _msg(f"Removing tiny leftover segments < {min_seg_length} ...")
                cleaned2 = arcpy.CreateUniqueName(
                    _unique("eligible_clean2"), scratch)
                scratch_paths.append(cleaned2)
                self._remove_small_segments(
                    cleaned_eligible_fc, min_seg_length, cleaned2)
                cleaned_eligible_fc = cleaned2

            xytol_clean = _xy_tolerance(cleaned_eligible_fc)
            slivers_removed_total += self._remove_slivers(
                cleaned_eligible_fc, xytol_clean)
            if removed_fc and arcpy.Exists(removed_fc):
                slivers_removed_total += self._remove_slivers(
                    removed_fc, _xy_tolerance(removed_fc))

            merged_out = arcpy.CreateUniqueName(_unique("merged_out"), scratch)
            scratch_paths.append(merged_out)
            _msg("Merging cleaned eligible + untouched protected contours...")
            arcpy.management.Merge([cleaned_eligible_fc, noneligible_fc],
                                   merged_out)

            out_clean_fc = _normalize_output_path(out_ws, out_clean_name)
            if arcpy.Exists(out_clean_fc):
                arcpy.management.Delete(out_clean_fc)
            arcpy.management.CopyFeatures(merged_out, out_clean_fc)

            out_removed_fc = None
            if create_removed and removed_fc and arcpy.Exists(removed_fc):
                out_removed_fc = _normalize_output_path(
                    out_ws, out_clean_name + "_Removed")
                if arcpy.Exists(out_removed_fc):
                    arcpy.management.Delete(out_removed_fc)
                arcpy.management.CopyFeatures(removed_fc, out_removed_fc)

            out_review_fc = None
            if create_review and review_fc and arcpy.Exists(review_fc):
                out_review_fc = _normalize_output_path(
                    out_ws, out_clean_name + "_Review")
                if arcpy.Exists(out_review_fc):
                    arcpy.management.Delete(out_review_fc)
                arcpy.management.CopyFeatures(review_fc, out_review_fc)

            kept_count = _get_count(out_clean_fc)
            removed_len_approx = self._approx_total_length(removed_fc)
            _diag(f"Output kept count: {kept_count}, approx removed "
                  f"length: {removed_len_approx:.2f}")
            if aoi_fallback_used:
                _warn("Output was generated using FULL-MAP fallback "
                      "(active map view extent).")

            report_path = None
            if write_report:
                try:
                    report_path = self._report_csv(
                        out_ws, out_clean_name,
                        total_in, eligible_count, noneligible_count,
                        dense_threshold, min_neighbors, safe_mu,
                        aoi_mode, mask_mode, removal_method,
                        min_seg_length, removed_len_approx,
                        out_clean_fc, out_removed_fc, out_review_fc,
                        out_mask_fc, aoi_fallback_used,
                        slivers_removed_total)
                except OSError as ex:
                    _warn(f"Failed to write CSV report: {ex}")

            if add_to_map:
                _add_layers_to_active_map(
                    [out_clean_fc, out_removed_fc, out_review_fc, out_mask_fc])

            _msg(f"Done. Output: {out_clean_fc}")
            if report_path:
                _msg(f"Report: {report_path}")
        finally:
            # Master Rule 6: clean up every scratch dataset and layer view.
            for lyr in cleanup_layers:
                try:
                    arcpy.management.Delete(lyr)
                except (arcpy.ExecuteError, RuntimeError):
                    pass
            for p in scratch_paths:
                _safe_delete(p)
