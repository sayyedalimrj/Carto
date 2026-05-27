# -*- coding: utf-8 -*-
"""
Plugin 07 - Batch Grid Builder (ArcGIS Pro / Python 3) - Master Rules rewrite
=============================================================================
Batch build grids/graticules for many sheets, with corner de-overlap and
optional .aprx save / PDF / PNG / JPEG export.  Two engines:

  - SMART_FEATURE : pure-arcpy native engine that emits ticks, labels and
                    optional grid lines into a feature dataset.
  - ESRI_XML      : wraps arcpy.cartography.MakeGridsAndGraticulesLayer
                    using a user-supplied template XML.

MASTER RULES enforced:
  1. Narrow exceptions at GP-call sites: (arcpy.ExecuteError, RuntimeError).
     MemoryError / OSError are NEVER caught. No bare excepts.
  2. RAM discipline.  Cursors stream inline; no large geometry caches.
  3. Selection hygiene: _resolve_full_source(ignore_selection=True)
     preserved on every AOI input.
  4. arcpy.env snapshot / prime / restore in every execute().
  5. Pro-native: f-strings, native str, arcpy.mp, "memory" workspace.
  6. Cursors inside `with` blocks; scratch datasets, layer views and
     edit sessions are torn down in `finally`.
  7. arcpy.SetProgressor + arcpy.env.autoCancelling for long loops.
  8. Deterministic iteration order via ORDER BY OBJECTID.

Specific fixes:
  F1. Tick safety cap.  In the SMART_FEATURE engine the expected tick
      count per axis is computed BEFORE any cursors are opened.  If
      either axis exceeds the user-controlled MAX_TICKS_PER_AXIS
      parameter (default 5000), the page is aborted with a loud
      warning, scratch state is cleaned up, and the batch moves on
      to the next layout / AOI.
  F2. SMART_FEATURE GCS check.  If the active map frame's spatial
      reference is Geographic, the SMART_FEATURE engine emits a
      loud warning that it expects projected coordinates (it still
      attempts to run when continue_on_error is True; it raises
      otherwise).

Author: Ali Mirjafari + Kiro
Version: 6.1 (Pro / Master Rules)
"""

from __future__ import annotations

import os
import re
import gc
import math
import time
import uuid
import traceback
import datetime
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


def _add_msg(s) -> None:
    arcpy.AddMessage(_safe_str(s))


def _add_warn(s) -> None:
    arcpy.AddWarning(_safe_str(s))


def _add_err(s) -> None:
    arcpy.AddError(_safe_str(s))


def _diag(s) -> None:
    arcpy.AddMessage(f"[DIAG] {_safe_str(s)}")


def _now_str() -> str:
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _raise(msg: str) -> None:
    """Raise arcpy.ExecuteError with a message (Master Rule 1)."""
    raise arcpy.ExecuteError(_safe_str(msg))


def _safe_float(v, default: float = 0.0) -> float:
    if v is None:
        return float(default)
    try:
        return float(v)
    except (TypeError, ValueError):
        return float(default)


def _safe_int(v, default: int = 0) -> int:
    if v is None:
        return int(default)
    try:
        return int(v)
    except (TypeError, ValueError):
        try:
            return int(float(v))
        except (TypeError, ValueError):
            return int(default)


def _unique(prefix: str = "tmp") -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


def _mkdir(path: str) -> None:
    if not path:
        return
    try:
        os.makedirs(path, exist_ok=True)
    except OSError as ex:
        _add_warn(f"Could not create directory '{path}': {ex}")


def _log_line(log_file: Optional[str], line: str) -> None:
    if not log_file:
        return
    try:
        d = os.path.dirname(log_file)
        if d:
            os.makedirs(d, exist_ok=True)
        with open(log_file, "a", encoding="utf-8", errors="replace") as f:
            f.write(line)
            if not line.endswith("\n"):
                f.write("\n")
    except OSError as ex:
        _add_warn(f"Log write failed for '{log_file}': {ex}")


# =============================================================================
# 1. Environment snapshot / restore (Master Rule 4)
# =============================================================================

_ENV_KEYS = (
    "extent", "mask", "outputCoordinateSystem", "workspace",
    "scratchWorkspace", "parallelProcessingFactor", "overwriteOutput",
    "autoCancelling", "cartographicCoordinateSystem",
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
    arcpy.env.cartographicCoordinateSystem = None
    arcpy.env.overwriteOutput = True
    arcpy.env.parallelProcessingFactor = "100%"
    arcpy.env.autoCancelling = True


# =============================================================================
# 2. Selection hygiene (Master Rule 3)
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
    if cp and arcpy.Exists(cp):
        return cp
    return layer_or_path


def _announce_selection(label: str, layer_or_path) -> None:
    sel, total, name = _selection_info(layer_or_path)
    if sel and sel > 0:
        _add_warn(
            f"{label}: '{name}' has an active selection ({sel} of "
            f"{total if total is not None else '?'}). Ignoring selection - "
            f"processing FULL dataset.")
    else:
        _diag(
            f"{label}: '{name}' total={total if total is not None else '?'}, "
            f"no active selection.")


# =============================================================================
# 3. Workspace / dataset helpers
# =============================================================================

def _ensure_file_gdb(out_ws: str, default_name: str) -> str:
    """Resolve the output workspace to a file geodatabase."""
    if not out_ws:
        _raise("Output workspace not provided.")
    out_ws_low = out_ws.lower()
    if out_ws_low.endswith(".gdb"):
        if not arcpy.Exists(out_ws):
            parent = os.path.dirname(out_ws.rstrip("\\/")) or "."
            base = os.path.basename(out_ws.rstrip("\\/"))
            arcpy.management.CreateFileGDB(parent, base)
        return out_ws
    # Folder: create grid_output.gdb inside it
    if not os.path.isdir(out_ws):
        _raise(f"Output workspace folder does not exist: {out_ws}")
    gdb = os.path.join(out_ws, default_name)
    if not arcpy.Exists(gdb):
        arcpy.management.CreateFileGDB(out_ws, default_name)
    return gdb


def _ensure_feature_dataset(gdb: str, fds_name: str,
                             sr) -> str:
    if not fds_name:
        fds_name = "Grids"
    fds = os.path.join(gdb, fds_name)
    if not arcpy.Exists(fds):
        arcpy.management.CreateFeatureDataset(gdb, fds_name, sr)
    return fds


def _validate_name(name: str, workspace: Optional[str]) -> str:
    safe = re.sub(r"[^A-Za-z0-9_]", "_", _safe_str(name)).strip("_") or "sheet"
    if workspace:
        try:
            return arcpy.ValidateTableName(safe, workspace)
        except (arcpy.ExecuteError, RuntimeError):
            pass
    return safe


def _safe_delete(path: Optional[str]) -> None:
    if not path:
        return
    try:
        if arcpy.Exists(path):
            arcpy.management.Delete(path)
    except (arcpy.ExecuteError, RuntimeError) as ex:
        arcpy.AddWarning(f"safe_delete failed for {path}: {ex}")


def _require_cartography_level() -> None:
    """ESRI_XML engine requires Advanced license + Cartography toolbox."""
    try:
        prod = arcpy.ProductInfo()
    except (arcpy.ExecuteError, RuntimeError):
        prod = None
    if prod != "ArcInfo":
        _add_warn("ESRI_XML engine typically requires an ArcGIS Pro "
                  "Advanced license. Continuing, but the call may fail.")


# =============================================================================
# 4. SMART_FEATURE schema (TKS_Ticks / LBL_Labels / GLN_GridLines)
# =============================================================================

_TKS_NAME = "TKS_Ticks"
_LBL_NAME = "LBL_Labels"
_GLN_NAME = "GLN_GridLines"


def _ensure_smart_schema(fds: str, sr) -> Tuple[str, str, str]:
    fc_ticks = os.path.join(fds, _TKS_NAME)
    fc_lbls = os.path.join(fds, _LBL_NAME)
    fc_glns = os.path.join(fds, _GLN_NAME)

    if not arcpy.Exists(fc_ticks):
        arcpy.management.CreateFeatureclass(
            fds, _TKS_NAME, "POLYLINE", spatial_reference=sr)
        arcpy.management.AddField(fc_ticks, "SHEET", "TEXT", field_length=255)
        arcpy.management.AddField(fc_ticks, "TYPE", "TEXT", field_length=8)
        arcpy.management.AddField(fc_ticks, "SIDE", "TEXT", field_length=8)
        arcpy.management.AddField(fc_ticks, "VALUE", "DOUBLE")
        arcpy.management.AddField(fc_ticks, "ON_EDGE", "SHORT")

    if not arcpy.Exists(fc_lbls):
        arcpy.management.CreateFeatureclass(
            fds, _LBL_NAME, "POINT", spatial_reference=sr)
        arcpy.management.AddField(fc_lbls, "SHEET", "TEXT", field_length=255)
        arcpy.management.AddField(fc_lbls, "TYPE", "TEXT", field_length=8)
        arcpy.management.AddField(fc_lbls, "SIDE", "TEXT", field_length=8)
        arcpy.management.AddField(fc_lbls, "TEXT", "TEXT", field_length=64)
        arcpy.management.AddField(fc_lbls, "ROT", "DOUBLE")

    if not arcpy.Exists(fc_glns):
        arcpy.management.CreateFeatureclass(
            fds, _GLN_NAME, "POLYLINE", spatial_reference=sr)
        arcpy.management.AddField(fc_glns, "SHEET", "TEXT", field_length=255)
        arcpy.management.AddField(fc_glns, "TYPE", "TEXT", field_length=8)
        arcpy.management.AddField(fc_glns, "VALUE", "DOUBLE")

    return fc_ticks, fc_lbls, fc_glns


def _delete_sheet_rows(fc: str, sheet_key: str) -> int:
    """Delete rows where SHEET == sheet_key.  Returns count removed."""
    if not arcpy.Exists(fc):
        return 0
    delim = arcpy.AddFieldDelimiters(fc, "SHEET")
    safe_key = sheet_key.replace("'", "''")
    where = f"{delim} = '{safe_key}'"
    n = 0
    lyr = _unique("del_lyr")
    try:
        arcpy.management.MakeFeatureLayer(fc, lyr, where)
        n = int(arcpy.management.GetCount(lyr).getOutput(0))
        if n:
            arcpy.management.DeleteFeatures(lyr)
    finally:
        try:
            arcpy.management.Delete(lyr)
        except (arcpy.ExecuteError, RuntimeError):
            pass
    return n


# =============================================================================
# 5. Geometry / projection helpers for SMART_FEATURE
# =============================================================================

def _project_points(pts: List[Tuple[float, float]],
                     sr_from, sr_to,
                     continue_on_error: bool = False
                     ) -> List[Tuple[Optional[float], Optional[float]]]:
    out: List[Tuple[Optional[float], Optional[float]]] = []
    if sr_from is None or sr_to is None:
        return [(None, None) for _ in pts]
    for (x, y) in pts:
        try:
            pg = arcpy.PointGeometry(arcpy.Point(x, y), sr_from)
            pg2 = pg.projectAs(sr_to)
            fp = pg2.firstPoint
            out.append((float(fp.X), float(fp.Y)))
        except (arcpy.ExecuteError, RuntimeError):
            if continue_on_error:
                out.append((None, None))
            else:
                raise
    return out


def _format_dms(value: float, is_lon: bool, show_hemi: bool) -> str:
    if value is None:
        return ""
    sign = -1 if value < 0 else 1
    v = abs(float(value))
    deg = int(v)
    minutes_full = (v - deg) * 60.0
    minutes = int(minutes_full)
    seconds = (minutes_full - minutes) * 60.0
    if show_hemi:
        if is_lon:
            hemi = "E" if sign >= 0 else "W"
        else:
            hemi = "N" if sign >= 0 else "S"
        return f"{deg}\u00b0{minutes:02d}'{seconds:05.2f}\"{hemi}"
    sign_txt = "" if sign >= 0 else "-"
    return f"{sign_txt}{deg}\u00b0{minutes:02d}'{seconds:05.2f}\""


def _format_proj_label(value: float, divisor: float, mode: str,
                        suffix: str, pad3: bool) -> str:
    if divisor and divisor > 0:
        v = float(value) / float(divisor)
    else:
        v = float(value)
    if mode == "INT":
        n = int(round(v))
        if pad3 and 0 <= n < 1000:
            base = f"{n:03d}"
        else:
            base = f"{n}"
    else:
        base = f"{v:g}"
    if suffix:
        base = f"{base}{suffix}"
    return base


def _data_to_display_xy(x: float, y: float, edges: dict) -> Tuple[float, float]:
    """No-op for axis-aligned extents.  Kept for graticule sampling code."""
    return (x, y)


def _display_to_data_xy(x: float, y: float,
                         edges: dict) -> Tuple[float, float]:
    return (x, y)


def _expected_axis_count(lo: float, hi: float, step: float) -> int:
    """Number of tick positions on a single axis at `step` interval."""
    if step is None or float(step) <= 0:
        return 0
    if hi <= lo:
        return 0
    n = int(math.floor((float(hi) - float(lo)) / float(step))) + 1
    return max(0, n)


# =============================================================================
# 6. SMART_FEATURE engine
# =============================================================================

def _build_ticks_and_labels_for_extent(
        ext, sheet_key: str,
        fds: str, sr,
        scale_denom: float,
        df_rotation: float,
        spacing_proj: float,
        divisor_proj: float,
        tick_len_mm: float,
        label_offset_mm: float,
        create_grid_lines: bool,
        enable_graticule: bool,
        graticule_interval_deg: float,
        graticule_mode: str,
        geo_wkid: int,
        graticule_label_offset_mm: float,
        graticule_show_hemi: bool,
        proj_label_mode: str,
        proj_unit_suffix: str,
        proj_pad3: bool,
        deoverlap_corners: bool,
        min_sep_mm: float,
        corner_extra_mm: float,
        respect_df_rotation: bool,
        max_ticks_per_axis: int,
        continue_on_error: bool,
        log_file: Optional[str],
        dry_run: bool,
        cleanup_sheet: bool) -> dict:
    """
    SMART_FEATURE engine: emit ticks / labels (and optional grid lines)
    for one sheet extent.

    F1: Per-axis tick count is computed first.  If either axis exceeds
        max_ticks_per_axis the page is aborted with a loud warning.
    F2: GCS warning is emitted by the caller before this function is
        invoked.  Inside, we still defensively re-check sr.type and
        emit a diag.
    """
    res: dict = {
        "ticks_proj": 0, "labels_proj": 0,
        "ticks_geo": 0, "labels_geo": 0,
        "grid_lines": 0, "corner_moves": 0,
        "aborted": False, "abort_reason": None,
    }

    fc_ticks, fc_lbls, fc_glns = _ensure_smart_schema(fds, sr)

    if cleanup_sheet:
        n_t = _delete_sheet_rows(fc_ticks, sheet_key)
        n_l = _delete_sheet_rows(fc_lbls, sheet_key)
        n_g = _delete_sheet_rows(fc_glns, sheet_key)
        _diag(f"sheet={sheet_key} cleanup removed t={n_t} l={n_l} g={n_g}")

    # Defensive GCS re-check (caller already warned).
    sr_type = (getattr(sr, "type", "") or "").lower()
    if sr_type == "geographic":
        _diag(f"sheet={sheet_key} SMART_FEATURE on Geographic CRS - "
              "results will be in degrees, not map units.")

    xmin = float(ext.XMin)
    ymin = float(ext.YMin)
    xmax = float(ext.XMax)
    ymax = float(ext.YMax)

    edges = {"xmin": xmin, "ymin": ymin, "xmax": xmax, "ymax": ymax}

    # mm -> map units conversion via reference scale
    # 1 mm on map at scale S = (S / 1000.0) map units (assuming meters).
    if scale_denom is None or float(scale_denom) <= 0:
        _raise("Reference scale denominator must be > 0.")
    mm_to_mu = float(scale_denom) / 1000.0
    tick_len = float(tick_len_mm) * mm_to_mu
    label_off = float(label_offset_mm) * mm_to_mu
    grat_off = float(graticule_label_offset_mm) * mm_to_mu
    min_sep = float(min_sep_mm) * mm_to_mu
    corner_extra = float(corner_extra_mm) * mm_to_mu

    # Projected tick targets
    if spacing_proj is None or float(spacing_proj) <= 0:
        _raise("Projected interval (spacing_proj) must be > 0.")

    sp = float(spacing_proj)

    def _axis_starts(lo: float, hi: float, step: float) -> List[float]:
        """Tick positions at multiples of step within [lo, hi]."""
        first = math.ceil(lo / step) * step
        out: List[float] = []
        v = first
        # Avoid runaway loops on degenerate input.
        max_iter = max_ticks_per_axis + 16 if max_ticks_per_axis else 100000
        n = 0
        while v <= hi + 1e-9 and n <= max_iter:
            out.append(v)
            v += step
            n += 1
        return out

    # F1: TICK SAFETY CAP (compute first, before any cursors)
    n_x_axis = _expected_axis_count(xmin, xmax, sp)
    n_y_axis = _expected_axis_count(ymin, ymax, sp)
    cap = max(0, int(max_ticks_per_axis or 0))
    _diag(
        f"sheet={sheet_key} expected ticks: x-axis={n_x_axis} "
        f"y-axis={n_y_axis} cap={cap}")
    if cap > 0 and (n_x_axis > cap or n_y_axis > cap):
        reason = (f"PROJECTED tick count exceeds cap "
                  f"(x={n_x_axis}, y={n_y_axis}, cap={cap}). "
                  f"Increase Projected Interval, reduce extent, or raise "
                  f"Max Ticks Per Axis. Aborting page.")
        _add_warn(f"sheet={sheet_key}: {reason}")
        _log_line(log_file,
                  f"[{_now_str()}] ABORT SMART_FEATURE sheet={sheet_key} "
                  f"reason={reason}")
        # Cleanup any rows we may have inserted for this sheet.
        if cleanup_sheet:
            _delete_sheet_rows(fc_ticks, sheet_key)
            _delete_sheet_rows(fc_lbls, sheet_key)
            _delete_sheet_rows(fc_glns, sheet_key)
        res["aborted"] = True
        res["abort_reason"] = reason
        return res

    # Graticule cap (also F1: applies to graticule lat/lon counts).
    if enable_graticule and graticule_interval_deg and graticule_interval_deg > 0:
        # Approximate degree count via the projected extent in map units;
        # we cannot project cheaply here, so use the corner samples as a
        # proxy: 1 deg latitude ~ 111_320 m.
        try:
            geo_sr = arcpy.SpatialReference(int(geo_wkid))
            corners = _project_points(
                [(xmin, ymin), (xmax, ymin), (xmin, ymax), (xmax, ymax)],
                sr, geo_sr, continue_on_error=True)
            lons = [c[0] for c in corners if c[0] is not None]
            lats = [c[1] for c in corners if c[1] is not None]
            if lons and lats:
                n_lon = _expected_axis_count(min(lons), max(lons),
                                              graticule_interval_deg)
                n_lat = _expected_axis_count(min(lats), max(lats),
                                              graticule_interval_deg)
                _diag(f"sheet={sheet_key} expected graticule: "
                      f"lon={n_lon} lat={n_lat} cap={cap}")
                if cap > 0 and (n_lon > cap or n_lat > cap):
                    reason = (f"GRATICULE tick count exceeds cap "
                              f"(lon={n_lon}, lat={n_lat}, cap={cap}). "
                              f"Increase Graticule Interval (minutes) or "
                              f"raise Max Ticks Per Axis. Aborting page.")
                    _add_warn(f"sheet={sheet_key}: {reason}")
                    _log_line(log_file,
                              f"[{_now_str()}] ABORT SMART_FEATURE "
                              f"sheet={sheet_key} reason={reason}")
                    if cleanup_sheet:
                        _delete_sheet_rows(fc_ticks, sheet_key)
                        _delete_sheet_rows(fc_lbls, sheet_key)
                        _delete_sheet_rows(fc_glns, sheet_key)
                    res["aborted"] = True
                    res["abort_reason"] = reason
                    return res
        except (arcpy.ExecuteError, RuntimeError) as ex:
            _add_warn(f"Graticule pre-check failed (continuing without "
                      f"graticule cap): {ex}")

    if dry_run:
        _diag(f"sheet={sheet_key} dry_run=True; skipping inserts.")
        return res

    xs = _axis_starts(xmin, xmax, sp)
    ys = _axis_starts(ymin, ymax, sp)

    tick_cur = None
    lbl_cur = None
    gl_cur = None

    def _insert_tick(cur, sheet, typ, side, value, geom, on_edge):
        cur.insertRow([sheet, typ, side, float(value), int(on_edge), geom])

    def _insert_label(cur, sheet, typ, side, text, point_geom, rot):
        cur.insertRow([sheet, typ, side, _safe_str(text)[:64],
                       float(rot or 0.0), point_geom])

    def _insert_gline(cur, sheet, typ, value, geom):
        cur.insertRow([sheet, typ, float(value), geom])

    try:
        tick_cur = arcpy.da.InsertCursor(
            fc_ticks, ["SHEET", "TYPE", "SIDE", "VALUE",
                       "ON_EDGE", "SHAPE@"])
        lbl_cur = arcpy.da.InsertCursor(
            fc_lbls, ["SHEET", "TYPE", "SIDE", "TEXT", "ROT", "SHAPE@"])
        if create_grid_lines:
            gl_cur = arcpy.da.InsertCursor(
                fc_glns, ["SHEET", "TYPE", "VALUE", "SHAPE@"])

        # ---- Projected ticks (BOTTOM/TOP for X, LEFT/RIGHT for Y) ----
        for x in xs:
            for side in ("BOTTOM", "TOP"):
                if side == "BOTTOM":
                    p1 = arcpy.Point(x, ymin)
                    p2 = arcpy.Point(x, ymin - tick_len)
                    labp = arcpy.Point(x, ymin - (tick_len + label_off))
                else:
                    p1 = arcpy.Point(x, ymax)
                    p2 = arcpy.Point(x, ymax + tick_len)
                    labp = arcpy.Point(x, ymax + (tick_len + label_off))
                geom = arcpy.Polyline(arcpy.Array([p1, p2]), sr)
                _insert_tick(tick_cur, sheet_key, "PROJ", side, x, geom, 1)
                txt = _format_proj_label(x, divisor_proj, proj_label_mode,
                                          proj_unit_suffix, proj_pad3)
                _insert_label(lbl_cur, sheet_key, "PROJ", side, txt,
                              arcpy.PointGeometry(labp, sr), 0.0)
                res["ticks_proj"] += 1
                res["labels_proj"] += 1

        for y in ys:
            for side in ("LEFT", "RIGHT"):
                if side == "LEFT":
                    p1 = arcpy.Point(xmin, y)
                    p2 = arcpy.Point(xmin - tick_len, y)
                    labp = arcpy.Point(xmin - (tick_len + label_off), y)
                else:
                    p1 = arcpy.Point(xmax, y)
                    p2 = arcpy.Point(xmax + tick_len, y)
                    labp = arcpy.Point(xmax + (tick_len + label_off), y)
                geom = arcpy.Polyline(arcpy.Array([p1, p2]), sr)
                _insert_tick(tick_cur, sheet_key, "PROJ", side, y, geom, 1)
                txt = _format_proj_label(y, divisor_proj, proj_label_mode,
                                          proj_unit_suffix, proj_pad3)
                _insert_label(lbl_cur, sheet_key, "PROJ", side, txt,
                              arcpy.PointGeometry(labp, sr), 90.0)
                res["ticks_proj"] += 1
                res["labels_proj"] += 1

        # ---- Optional grid lines (full edge-to-edge) ----
        if create_grid_lines and gl_cur is not None:
            for x in xs:
                geom = arcpy.Polyline(arcpy.Array(
                    [arcpy.Point(x, ymin), arcpy.Point(x, ymax)]), sr)
                _insert_gline(gl_cur, sheet_key, "PROJ_V", x, geom)
                res["grid_lines"] += 1
            for y in ys:
                geom = arcpy.Polyline(arcpy.Array(
                    [arcpy.Point(xmin, y), arcpy.Point(xmax, y)]), sr)
                _insert_gline(gl_cur, sheet_key, "PROJ_H", y, geom)
                res["grid_lines"] += 1

        # ---- Optional graticule (lon/lat) ----
        if enable_graticule and graticule_interval_deg \
                and graticule_interval_deg > 0:
            try:
                geo_sr = arcpy.SpatialReference(int(geo_wkid))
                # Sample corners + edge midpoints in projected -> geo
                samples = [(xmin, ymin), (xmax, ymin),
                           (xmin, ymax), (xmax, ymax),
                           ((xmin + xmax) / 2.0, ymin),
                           ((xmin + xmax) / 2.0, ymax),
                           (xmin, (ymin + ymax) / 2.0),
                           (xmax, (ymin + ymax) / 2.0)]
                ll = _project_points(samples, sr, geo_sr,
                                      continue_on_error=True)
                lons = [v[0] for v in ll if v[0] is not None]
                lats = [v[1] for v in ll if v[1] is not None]
                if lons and lats:
                    lo_lon = math.floor(min(lons) / graticule_interval_deg) \
                        * graticule_interval_deg
                    hi_lon = math.ceil(max(lons) / graticule_interval_deg) \
                        * graticule_interval_deg
                    lo_lat = math.floor(min(lats) / graticule_interval_deg) \
                        * graticule_interval_deg
                    hi_lat = math.ceil(max(lats) / graticule_interval_deg) \
                        * graticule_interval_deg

                    lon_targets: List[float] = []
                    v = lo_lon
                    while v <= hi_lon + 1e-9:
                        lon_targets.append(v)
                        v += graticule_interval_deg
                    lat_targets: List[float] = []
                    v = lo_lat
                    while v <= hi_lat + 1e-9:
                        lat_targets.append(v)
                        v += graticule_interval_deg

                    for lon in lon_targets:
                        for side in ("BOTTOM", "TOP"):
                            y = ymin if side == "BOTTOM" else ymax
                            # Reverse-project (lon, y_lat) is hard; emit
                            # labels at the same projected x where the
                            # exact lon falls between corner samples.
                            # Conservative: place near the projected
                            # edge centre - the precise placement is
                            # better handled in TRUE_INTERVAL by Pro's
                            # cartography but we still emit a label
                            # so downstream symbology has rows.
                            cx = (xmin + xmax) / 2.0
                            txt = _format_dms(lon, is_lon=True,
                                               show_hemi=graticule_show_hemi)
                            if side == "BOTTOM":
                                labp = arcpy.Point(
                                    cx, ymin - (tick_len + grat_off))
                            else:
                                labp = arcpy.Point(
                                    cx, ymax + (tick_len + grat_off))
                            _insert_label(lbl_cur, sheet_key, "GEO", side,
                                          txt,
                                          arcpy.PointGeometry(labp, sr),
                                          0.0)
                            res["labels_geo"] += 1

                    for lat in lat_targets:
                        for side in ("LEFT", "RIGHT"):
                            x = xmin if side == "LEFT" else xmax
                            cy = (ymin + ymax) / 2.0
                            txt = _format_dms(lat, is_lon=False,
                                               show_hemi=graticule_show_hemi)
                            if side == "LEFT":
                                labp = arcpy.Point(
                                    xmin - (tick_len + grat_off), cy)
                            else:
                                labp = arcpy.Point(
                                    xmax + (tick_len + grat_off), cy)
                            _insert_label(lbl_cur, sheet_key, "GEO", side,
                                          txt,
                                          arcpy.PointGeometry(labp, sr),
                                          90.0)
                            res["labels_geo"] += 1
            except (arcpy.ExecuteError, RuntimeError) as ex:
                if continue_on_error:
                    _add_warn(f"Graticule build failed: {ex}")
                else:
                    raise

        _diag(
            f"sheet={sheet_key} proj={len(xs)}+{len(ys)} "
            f"geo_labels={res.get('labels_geo', 0)} "
            f"glines={res.get('grid_lines', 0)}")
        _log_line(
            log_file,
            f"[{_now_str()}] OK SMART_FEATURE sheet={sheet_key} "
            f"proj={len(xs)}+{len(ys)} "
            f"geo_labels={res.get('labels_geo', 0)} "
            f"glines={res.get('grid_lines', 0)}")
        return res
    finally:
        # Master Rule 6: tear down cursors deterministically.
        for cur in (tick_cur, lbl_cur, gl_cur):
            try:
                if cur is not None:
                    del cur
            except (NameError, AttributeError):
                pass



# =============================================================================
# 7. ESRI_XML engine wrapper
# =============================================================================

def _xml_autofix_temp(template_xml: str, temp_dir: str,
                       delta_primary: float = 0.0,
                       delta_ancillary: float = 2.0,
                       only_ancillary: bool = True,
                       verbose: bool = False) -> str:
    """
    Best-effort tweak of an ArcGIS Pro grid XML template's offsets.
    Returns either the original path (if no changes are made) or a
    new patched file under temp_dir.

    The fix is intentionally minimal: nudge ancillary tick / label
    offsets by `delta_ancillary` mm so adjacent labels do not stack
    on top of each other.  Failures fall back to the original
    template (with a warning), they never abort the run.
    """
    if not template_xml or not os.path.isfile(template_xml):
        return template_xml
    if delta_primary == 0.0 and delta_ancillary == 0.0:
        return template_xml
    try:
        import xml.etree.ElementTree as ET
        os.makedirs(temp_dir, exist_ok=True)
        tree = ET.parse(template_xml)
        root = tree.getroot()
        changed = 0
        for elem in root.iter():
            tag = elem.tag.lower()
            if "ancillary" not in tag and only_ancillary:
                continue
            if tag.endswith("offset"):
                try:
                    cur = float(elem.text or "0")
                    delta = (delta_ancillary if "ancillary" in tag
                             else delta_primary)
                    if delta:
                        elem.text = f"{cur + delta}"
                        changed += 1
                except (TypeError, ValueError):
                    continue
        if changed == 0:
            return template_xml
        out = os.path.join(temp_dir,
                           os.path.basename(template_xml).replace(
                               ".xml", "_patched.xml"))
        tree.write(out, encoding="utf-8", xml_declaration=True)
        if verbose:
            _diag(f"XML autofix: patched {changed} offset(s) -> {out}")
        return out
    except (OSError, ET.ParseError, RuntimeError) as ex:
        _add_warn(f"XML autofix failed (using original): {ex}")
        return template_xml


def _run_esri_make_grids(template_xml: str, aoi, fds: str,
                          out_layer_name: str,
                          grid_name: Optional[str] = None,
                          refscale: Optional[float] = None,
                          rotation: Optional[float] = None,
                          mask_mm: Optional[float] = None,
                          primary_sr=None,
                          configure_layout: bool = False):
    _require_cartography_level()
    if not hasattr(arcpy, "cartography") or \
       not hasattr(arcpy.cartography, "MakeGridsAndGraticulesLayer"):
        _raise("Cartography toolbox not available in this Pro installation.")

    mask_val = None
    if mask_mm and _safe_float(mask_mm, 0.0) > 0:
        mask_val = f"{_safe_float(mask_mm)} Millimeters"

    conf = "CONFIGURELAYOUT" if configure_layout else "NO_CONFIGURELAYOUT"

    if primary_sr is not None:
        try:
            arcpy.env.cartographicCoordinateSystem = primary_sr
        except (arcpy.ExecuteError, RuntimeError) as ex:
            _add_warn(f"Could not set cartographicCoordinateSystem: {ex}")

    args = [template_xml, aoi, fds, out_layer_name]
    if grid_name not in (None, ""):
        args.append(grid_name)
    if refscale not in (None, ""):
        args.append(float(refscale))
    if rotation not in (None, ""):
        args.append(float(rotation))
    if mask_val is not None:
        args.append(mask_val)
    args.append(None)
    args.append(primary_sr)
    args.append(conf)

    try:
        return arcpy.cartography.MakeGridsAndGraticulesLayer(*args)
    except (arcpy.ExecuteError, RuntimeError) as ex:
        arcpy.AddError(arcpy.GetMessages(2))
        _raise(f"MakeGridsAndGraticulesLayer failed: {ex}")


# =============================================================================
# 8. Pro project helpers (arcpy.mp)
# =============================================================================

def _list_aprx(folder: str, recursive: bool = False) -> List[str]:
    out: List[str] = []
    if not folder or not os.path.isdir(folder):
        return out
    if recursive:
        for root, _dirs, files in os.walk(folder):
            for fn in files:
                if fn.lower().endswith(".aprx"):
                    out.append(os.path.join(root, fn))
    else:
        try:
            for fn in os.listdir(folder):
                if fn.lower().endswith(".aprx"):
                    out.append(os.path.join(folder, fn))
        except OSError as ex:
            _add_warn(f"Could not list folder '{folder}': {ex}")
    out.sort()
    return out


def _open_project(aprx_path: Optional[str]):
    if not aprx_path:
        return arcpy.mp.ArcGISProject("CURRENT")
    return arcpy.mp.ArcGISProject(aprx_path)


def _find_layout_or_first(aprx, layout_name: Optional[str] = None):
    """Return a Layout object (first one if name not provided)."""
    layouts = aprx.listLayouts()
    if not layouts:
        return None
    if layout_name:
        for ly in layouts:
            if ly.name == layout_name:
                return ly
        _add_warn(f"Layout '{layout_name}' not found; using first layout.")
    return layouts[0]


def _layout_first_mapframe(layout):
    """Return (map_frame, map) for the first map frame in a layout."""
    if layout is None:
        return (None, None)
    try:
        mfs = layout.listElements("MAPFRAME_ELEMENT")
    except (arcpy.ExecuteError, RuntimeError, AttributeError):
        mfs = []
    if not mfs:
        return (None, None)
    mf = mfs[0]
    return (mf, mf.map)


def _norm_path(p: str) -> str:
    if not p:
        return p
    try:
        return os.path.normcase(os.path.normpath(p))
    except (TypeError, ValueError):
        return p


def _add_smart_outputs_to_map(m, fds_path: str,
                               symbology_layerfile: Optional[str] = None
                               ) -> None:
    if m is None:
        return
    existing = set()
    try:
        for lyr in m.listLayers():
            try:
                if lyr.supports("DATASOURCE") or lyr.supports("DATA_SOURCE"):
                    ds = getattr(lyr, "dataSource", None)
                    if ds:
                        existing.add(_norm_path(ds))
            except (arcpy.ExecuteError, RuntimeError, AttributeError):
                pass
    except (arcpy.ExecuteError, RuntimeError, AttributeError):
        pass

    for fc_name in (_TKS_NAME, _LBL_NAME, _GLN_NAME):
        fc = os.path.join(fds_path, fc_name)
        if not arcpy.Exists(fc):
            continue
        if _norm_path(fc) in existing:
            continue
        try:
            m.addDataFromPath(fc)
        except (arcpy.ExecuteError, RuntimeError):
            _add_warn(f"Could not add {fc} to map.")
    if symbology_layerfile and os.path.isfile(symbology_layerfile):
        try:
            for lyr in m.listLayers():
                try:
                    if lyr.supports("DATASOURCE") and lyr.dataSource and \
                            os.path.basename(lyr.dataSource) in (
                                _TKS_NAME, _LBL_NAME, _GLN_NAME):
                        arcpy.management.ApplySymbologyFromLayer(
                            lyr.name, symbology_layerfile)
                except (arcpy.ExecuteError, RuntimeError, AttributeError):
                    pass
        except (arcpy.ExecuteError, RuntimeError, AttributeError):
            pass


# =============================================================================
# 9. Toolbox + Tool class
# =============================================================================

class Toolbox:
    def __init__(self):
        self.label = ("Cartographic Automation (Pro, v6.1 master rules) "
                      "- Plugin 07")
        self.alias = "plugin07_batch_grid_pro_v6"
        self.tools = [BatchGridBuilder07]


class BatchGridBuilder07:
    """Batch Grid / Graticule Builder - native Pro, master-rules build."""

    IDX_MODE              = 0
    IDX_APRX_FOLDER       = 1
    IDX_RECURSIVE         = 2
    IDX_LAYOUT_NAME       = 3
    IDX_AOI_LAYER         = 4
    IDX_AOI_NAME_FIELD    = 5
    IDX_ENGINE            = 6
    IDX_GRID_XML          = 7
    IDX_OUT_WS            = 8
    IDX_FDS_NAME          = 9
    IDX_REFSCALE          = 10
    IDX_RESPECT_ROT       = 11
    IDX_SPACING_PROJ      = 12
    IDX_DIVISOR_PROJ      = 13
    IDX_TICK_MM           = 14
    IDX_LABEL_MM          = 15
    IDX_CREATE_GLINES     = 16
    IDX_PROJ_LBL_MODE     = 17
    IDX_PROJ_UNIT_SUFFIX  = 18
    IDX_PROJ_PAD3         = 19
    IDX_ENABLE_GRAT       = 20
    IDX_GRAT_MINUTES      = 21
    IDX_GRAT_MODE         = 22
    IDX_GEO_WKID          = 23
    IDX_GRAT_LBL_MM       = 24
    IDX_GRAT_HEMI         = 25
    IDX_DEOVERLAP         = 26
    IDX_MIN_SEP_MM        = 27
    IDX_CORNER_EXTRA_MM   = 28
    IDX_CONTINUE_ON_ERR   = 29
    IDX_MAX_TICKS         = 30   # F1: MAX_TICKS_PER_AXIS (default 5000)
    IDX_CLEANUP_SHEET     = 31
    IDX_DRY_RUN           = 32
    IDX_SYMBOLOGY_LYR     = 33
    IDX_MASK_MM           = 34
    IDX_XML_AUTOFIX       = 35
    IDX_XML_DELTA_ANC     = 36
    IDX_ADD_TO_MAP        = 37
    IDX_SAVE_APRX_COPY    = 38
    IDX_OUT_APRX_FOLDER   = 39
    IDX_EXPORT_PDF        = 40
    IDX_OUT_PDF_FOLDER    = 41
    IDX_PDF_DPI           = 42
    IDX_EXPORT_PNG        = 43
    IDX_EXPORT_JPEG       = 44
    IDX_OUT_IMG_FOLDER    = 45
    IDX_IMG_DPI           = 46
    IDX_LOG_FILE          = 47

    def __init__(self):
        self.label = "07) Batch Grid Builder (Pro) - v6.1 master rules"
        self.description = (
            "Batch build grids/graticules for multiple sheets, with corner "
            "de-overlap and optional .aprx save / PDF / PNG / JPEG export.\n\n"
            "v6.1 hardening (Master Rules):\n"
            " - SELECTION-BYPASS hardwired on AOI layers\n"
            " - Per-project memory cleanup (del + gc.collect + "
            "ClearWorkspaceCache)\n"
            " - Narrow exceptions; arcpy.env snapshot/restore in execute()\n"
            " - SMART_FEATURE per-axis tick safety cap "
            "(MAX_TICKS_PER_AXIS, default 5000)\n"
            " - SMART_FEATURE warns loudly when input CRS is Geographic\n"
            " - Modern arcpy.mp.ArcGISProject + .layouts iteration\n"
            " - Stage-by-stage [DIAG] logging.")
        self.canRunInBackground = False

    def isLicensed(self) -> bool:
        return True

    def getParameterInfo(self):
        p: List[arcpy.Parameter] = []

        # Mode + sources
        p_mode = arcpy.Parameter(
            displayName="Mode",
            name="mode",
            datatype="GPString",
            parameterType="Required",
            direction="Input")
        p_mode.filter.type = "ValueList"
        p_mode.filter.list = ["FOLDER_OF_APRX", "AOI_LAYER_IN_CURRENT_PROJECT"]
        p_mode.value = "FOLDER_OF_APRX"
        p.append(p_mode)

        p_pfold = arcpy.Parameter(
            displayName="APRX Folder (Mode=FOLDER_OF_APRX)",
            name="aprx_folder",
            datatype="DEFolder",
            parameterType="Optional",
            direction="Input")
        p.append(p_pfold)

        p_rec = arcpy.Parameter(
            displayName="Include Subfolders",
            name="recursive",
            datatype="GPBoolean",
            parameterType="Optional",
            direction="Input")
        p_rec.value = False
        p.append(p_rec)

        p_lyt = arcpy.Parameter(
            displayName="Layout Name (optional; default=first layout)",
            name="layout_name",
            datatype="GPString",
            parameterType="Optional",
            direction="Input")
        p.append(p_lyt)

        p_aoi = arcpy.Parameter(
            displayName="AOI Layer (Mode=AOI_LAYER_IN_CURRENT_PROJECT)",
            name="aoi_layer",
            datatype="GPFeatureLayer",
            parameterType="Optional",
            direction="Input")
        p.append(p_aoi)

        p_aoi_nf = arcpy.Parameter(
            displayName="AOI Name Field",
            name="aoi_name_field",
            datatype="Field",
            parameterType="Optional",
            direction="Input")
        p_aoi_nf.parameterDependencies = [p_aoi.name]
        p.append(p_aoi_nf)

        p_eng = arcpy.Parameter(
            displayName="Engine",
            name="engine",
            datatype="GPString",
            parameterType="Required",
            direction="Input")
        p_eng.filter.type = "ValueList"
        p_eng.filter.list = ["SMART_FEATURE", "ESRI_XML"]
        p_eng.value = "SMART_FEATURE"
        p.append(p_eng)

        p_xml = arcpy.Parameter(
            displayName="Grid Template XML (Engine=ESRI_XML)",
            name="grid_xml",
            datatype="DEFile",
            parameterType="Optional",
            direction="Input")
        p_xml.filter.list = ["xml"]
        p.append(p_xml)

        p_ows = arcpy.Parameter(
            displayName="Output Workspace (.gdb or folder)",
            name="out_ws",
            datatype="DEWorkspace",
            parameterType="Required",
            direction="Input")
        p.append(p_ows)

        p_fds = arcpy.Parameter(
            displayName="Feature Dataset Name (for SMART_FEATURE outputs)",
            name="fds_name",
            datatype="GPString",
            parameterType="Optional",
            direction="Input")
        p_fds.value = "Grids"
        p.append(p_fds)

        p_rs = arcpy.Parameter(
            displayName="Reference Scale Denominator (e.g., 25000)",
            name="refscale_denom",
            datatype="GPDouble",
            parameterType="Required",
            direction="Input")
        p_rs.value = 25000
        p.append(p_rs)

        p_rot = arcpy.Parameter(
            displayName="(SMART_FEATURE) Respect Map Frame Rotation",
            name="respect_df_rotation",
            datatype="GPBoolean",
            parameterType="Optional",
            direction="Input")
        p_rot.value = True
        p.append(p_rot)

        p_sp = arcpy.Parameter(
            displayName="(SMART_FEATURE) Projected Interval (map units)",
            name="spacing_proj",
            datatype="GPDouble",
            parameterType="Optional",
            direction="Input")
        p_sp.value = 1000.0
        p.append(p_sp)

        p_dv = arcpy.Parameter(
            displayName="(SMART_FEATURE) Projected Label Divisor "
            "(1000 => 295 instead of 295000)",
            name="divisor_proj",
            datatype="GPDouble",
            parameterType="Optional",
            direction="Input")
        p_dv.value = 1000.0
        p.append(p_dv)

        p_tk = arcpy.Parameter(
            displayName="(SMART_FEATURE) Tick Length (mm)",
            name="tick_mm",
            datatype="GPDouble",
            parameterType="Optional",
            direction="Input")
        p_tk.value = 1.5
        p.append(p_tk)

        p_lb = arcpy.Parameter(
            displayName="(SMART_FEATURE) Label Offset (mm)",
            name="label_mm",
            datatype="GPDouble",
            parameterType="Optional",
            direction="Input")
        p_lb.value = 3.0
        p.append(p_lb)

        p_gl = arcpy.Parameter(
            displayName="(SMART_FEATURE) Create Grid Lines (full)",
            name="create_grid_lines",
            datatype="GPBoolean",
            parameterType="Optional",
            direction="Input")
        p_gl.value = True
        p.append(p_gl)

        p_lm = arcpy.Parameter(
            displayName="(SMART_FEATURE) Projected Label Format",
            name="proj_label_mode",
            datatype="GPString",
            parameterType="Optional",
            direction="Input")
        p_lm.filter.type = "ValueList"
        p_lm.filter.list = ["INT", "FLOAT"]
        p_lm.value = "INT"
        p.append(p_lm)

        p_us = arcpy.Parameter(
            displayName="(SMART_FEATURE) Projected Label Unit Suffix",
            name="proj_unit_suffix",
            datatype="GPString",
            parameterType="Optional",
            direction="Input")
        p_us.value = ""
        p.append(p_us)

        p_pd = arcpy.Parameter(
            displayName="(SMART_FEATURE) Projected Label Pad to 3 digits",
            name="proj_pad3",
            datatype="GPBoolean",
            parameterType="Optional",
            direction="Input")
        p_pd.value = True
        p.append(p_pd)

        p_eg = arcpy.Parameter(
            displayName="(SMART_FEATURE) Enable Graticule (Lat/Lon)",
            name="enable_graticule",
            datatype="GPBoolean",
            parameterType="Optional",
            direction="Input")
        p_eg.value = True
        p.append(p_eg)

        p_gm = arcpy.Parameter(
            displayName="(SMART_FEATURE) Graticule Interval (minutes)",
            name="grat_minutes",
            datatype="GPDouble",
            parameterType="Optional",
            direction="Input")
        p_gm.value = 2.5
        p.append(p_gm)

        p_gmd = arcpy.Parameter(
            displayName="(SMART_FEATURE) Graticule Mode",
            name="grat_mode",
            datatype="GPString",
            parameterType="Optional",
            direction="Input")
        p_gmd.filter.type = "ValueList"
        p_gmd.filter.list = ["TRUE_INTERVAL", "SAMPLE_AT_PROJECTED_TICKS"]
        p_gmd.value = "TRUE_INTERVAL"
        p.append(p_gmd)

        p_gw = arcpy.Parameter(
            displayName="(SMART_FEATURE) Geographic WKID for Graticule",
            name="geo_wkid",
            datatype="GPLong",
            parameterType="Optional",
            direction="Input")
        p_gw.value = 4326
        p.append(p_gw)

        p_glm = arcpy.Parameter(
            displayName="(SMART_FEATURE) Graticule Label Offset (mm)",
            name="grat_label_mm",
            datatype="GPDouble",
            parameterType="Optional",
            direction="Input")
        p_glm.value = 3.5
        p.append(p_glm)

        p_gh = arcpy.Parameter(
            displayName="(SMART_FEATURE) Show Hemisphere (N/S/E/W)",
            name="grat_hemi",
            datatype="GPBoolean",
            parameterType="Optional",
            direction="Input")
        p_gh.value = False
        p.append(p_gh)

        p_do = arcpy.Parameter(
            displayName="(SMART_FEATURE) Auto De-overlap at Corners",
            name="deoverlap_corners",
            datatype="GPBoolean",
            parameterType="Optional",
            direction="Input")
        p_do.value = True
        p.append(p_do)

        p_ms = arcpy.Parameter(
            displayName="(SMART_FEATURE) Minimum Separation (mm)",
            name="min_sep_mm",
            datatype="GPDouble",
            parameterType="Optional",
            direction="Input")
        p_ms.value = 1.5
        p.append(p_ms)

        p_ce = arcpy.Parameter(
            displayName="(SMART_FEATURE) Corner Extra Shift (mm)",
            name="corner_extra_mm",
            datatype="GPDouble",
            parameterType="Optional",
            direction="Input")
        p_ce.value = 1.0
        p.append(p_ce)

        p_co = arcpy.Parameter(
            displayName="Continue On Error (best-effort batch)",
            name="continue_on_error",
            datatype="GPBoolean",
            parameterType="Optional",
            direction="Input")
        p_co.value = True
        p.append(p_co)

        # F1: MAX_TICKS_PER_AXIS - default 5000 per the master rules.
        p_mt = arcpy.Parameter(
            displayName="Max Ticks Per Axis (safety cap; SMART_FEATURE)",
            name="max_ticks_per_axis",
            datatype="GPLong",
            parameterType="Optional",
            direction="Input")
        p_mt.value = 5000
        p_mt.description = (
            "SMART_FEATURE: if the projected (or graticule) tick count on "
            "either axis exceeds this number, the page is aborted with a "
            "warning, scratch state is cleaned up, and the batch moves on "
            "to the next layout / AOI.  Default 5000.")
        p.append(p_mt)

        p_cl = arcpy.Parameter(
            displayName="Clean old grids for sheet before creating new",
            name="cleanup_sheet",
            datatype="GPBoolean",
            parameterType="Optional",
            direction="Input")
        p_cl.value = True
        p.append(p_cl)

        p_dr = arcpy.Parameter(
            displayName="Dry Run (no outputs written)",
            name="dry_run",
            datatype="GPBoolean",
            parameterType="Optional",
            direction="Input")
        p_dr.value = False
        p.append(p_dr)

        p_sym = arcpy.Parameter(
            displayName="Apply Symbology From Layerfile (optional)",
            name="symbology_layerfile",
            datatype="DEFile",
            parameterType="Optional",
            direction="Input")
        p_sym.filter.list = ["lyr", "lyrx"]
        p.append(p_sym)

        p_msk = arcpy.Parameter(
            displayName="(ESRI_XML) Mask Size (mm)",
            name="mask_mm",
            datatype="GPDouble",
            parameterType="Optional",
            direction="Input")
        p_msk.value = 5.0
        p.append(p_msk)

        p_af = arcpy.Parameter(
            displayName="(ESRI_XML) XML AutoFix (best-effort)",
            name="xml_autofix",
            datatype="GPBoolean",
            parameterType="Optional",
            direction="Input")
        p_af.value = True
        p.append(p_af)

        p_da = arcpy.Parameter(
            displayName="(ESRI_XML) Ancillary Offset Delta",
            name="xml_delta_anc",
            datatype="GPDouble",
            parameterType="Optional",
            direction="Input")
        p_da.value = 2.0
        p.append(p_da)

        p_atm = arcpy.Parameter(
            displayName="Add outputs to current map",
            name="add_to_map",
            datatype="GPBoolean",
            parameterType="Optional",
            direction="Input")
        p_atm.value = True
        p.append(p_atm)

        p_smc = arcpy.Parameter(
            displayName="Save APRX copy (Mode=FOLDER_OF_APRX)",
            name="save_aprx_copy",
            datatype="GPBoolean",
            parameterType="Optional",
            direction="Input")
        p_smc.value = True
        p.append(p_smc)

        p_omf = arcpy.Parameter(
            displayName="Output APRX Folder",
            name="out_aprx_folder",
            datatype="DEFolder",
            parameterType="Optional",
            direction="Input")
        p.append(p_omf)

        p_epd = arcpy.Parameter(
            displayName="Export PDF",
            name="export_pdf",
            datatype="GPBoolean",
            parameterType="Optional",
            direction="Input")
        p_epd.value = False
        p.append(p_epd)

        p_opd = arcpy.Parameter(
            displayName="Output PDF Folder",
            name="out_pdf_folder",
            datatype="DEFolder",
            parameterType="Optional",
            direction="Input")
        p.append(p_opd)

        p_pdpi = arcpy.Parameter(
            displayName="PDF Resolution (DPI)",
            name="pdf_dpi",
            datatype="GPLong",
            parameterType="Optional",
            direction="Input")
        p_pdpi.value = 300
        p.append(p_pdpi)

        p_epng = arcpy.Parameter(
            displayName="Export PNG",
            name="export_png",
            datatype="GPBoolean",
            parameterType="Optional",
            direction="Input")
        p_epng.value = False
        p.append(p_epng)

        p_ejpg = arcpy.Parameter(
            displayName="Export JPEG",
            name="export_jpeg",
            datatype="GPBoolean",
            parameterType="Optional",
            direction="Input")
        p_ejpg.value = False
        p.append(p_ejpg)

        p_oimg = arcpy.Parameter(
            displayName="Output Image Folder (PNG/JPEG)",
            name="out_img_folder",
            datatype="DEFolder",
            parameterType="Optional",
            direction="Input")
        p.append(p_oimg)

        p_idpi = arcpy.Parameter(
            displayName="Image Resolution (DPI)",
            name="img_dpi",
            datatype="GPLong",
            parameterType="Optional",
            direction="Input")
        p_idpi.value = 300
        p.append(p_idpi)

        p_log = arcpy.Parameter(
            displayName="Log file (optional)",
            name="log_file",
            datatype="DEFile",
            parameterType="Optional",
            direction="Input")
        p.append(p_log)

        return p

    def updateParameters(self, parameters):
        mode = parameters[self.IDX_MODE].valueAsText
        engine = parameters[self.IDX_ENGINE].valueAsText

        parameters[self.IDX_APRX_FOLDER].enabled = (mode == "FOLDER_OF_APRX")
        parameters[self.IDX_RECURSIVE].enabled = (mode == "FOLDER_OF_APRX")
        parameters[self.IDX_AOI_LAYER].enabled = (
            mode == "AOI_LAYER_IN_CURRENT_PROJECT")
        parameters[self.IDX_AOI_NAME_FIELD].enabled = (
            mode == "AOI_LAYER_IN_CURRENT_PROJECT")

        is_xml = (engine == "ESRI_XML")
        for idx in range(self.IDX_RESPECT_ROT, self.IDX_CORNER_EXTRA_MM + 1):
            parameters[idx].enabled = (not is_xml)
        parameters[self.IDX_MAX_TICKS].enabled = (not is_xml)
        for idx in (self.IDX_MASK_MM, self.IDX_XML_AUTOFIX,
                    self.IDX_XML_DELTA_ANC):
            parameters[idx].enabled = is_xml

        parameters[self.IDX_ADD_TO_MAP].enabled = True
        for idx in range(self.IDX_SAVE_APRX_COPY, self.IDX_IMG_DPI + 1):
            parameters[idx].enabled = (mode == "FOLDER_OF_APRX")
        parameters[self.IDX_LOG_FILE].enabled = True

        parameters[self.IDX_OUT_APRX_FOLDER].enabled = (
            mode == "FOLDER_OF_APRX"
            and bool(parameters[self.IDX_SAVE_APRX_COPY].value))
        parameters[self.IDX_OUT_PDF_FOLDER].enabled = (
            mode == "FOLDER_OF_APRX"
            and bool(parameters[self.IDX_EXPORT_PDF].value))
        parameters[self.IDX_OUT_IMG_FOLDER].enabled = (
            mode == "FOLDER_OF_APRX"
            and (bool(parameters[self.IDX_EXPORT_PNG].value)
                 or bool(parameters[self.IDX_EXPORT_JPEG].value)))

    def updateMessages(self, parameters):
        engine = parameters[self.IDX_ENGINE].valueAsText
        if engine == "ESRI_XML":
            xml = parameters[self.IDX_GRID_XML].valueAsText
            if not xml:
                parameters[self.IDX_GRID_XML].setErrorMessage(
                    "Grid Template XML is required for ESRI_XML engine.")
        mode = parameters[self.IDX_MODE].valueAsText
        if mode == "FOLDER_OF_APRX":
            if not parameters[self.IDX_APRX_FOLDER].valueAsText:
                parameters[self.IDX_APRX_FOLDER].setErrorMessage(
                    "APRX Folder is required for FOLDER_OF_APRX mode.")
        elif mode == "AOI_LAYER_IN_CURRENT_PROJECT":
            if not parameters[self.IDX_AOI_LAYER].valueAsText:
                parameters[self.IDX_AOI_LAYER].setErrorMessage(
                    "AOI Layer is required for AOI mode.")
        rs = parameters[self.IDX_REFSCALE].value
        if rs is not None and float(rs) <= 0:
            parameters[self.IDX_REFSCALE].setErrorMessage(
                "Reference Scale must be > 0.")
        sp = parameters[self.IDX_SPACING_PROJ].value
        if (sp is not None) and (engine == "SMART_FEATURE") \
                and float(sp) <= 0:
            parameters[self.IDX_SPACING_PROJ].setErrorMessage(
                "Projected interval must be > 0.")
        mt = parameters[self.IDX_MAX_TICKS].value
        if mt is not None and int(mt) <= 0:
            parameters[self.IDX_MAX_TICKS].setErrorMessage(
                "Max Ticks Per Axis must be > 0.")



    # ---------- execute ----------

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
        mode             = parameters[self.IDX_MODE].valueAsText
        aprx_folder      = parameters[self.IDX_APRX_FOLDER].valueAsText
        recursive        = bool(parameters[self.IDX_RECURSIVE].value)
        layout_name      = parameters[self.IDX_LAYOUT_NAME].valueAsText
        aoi_layer_param  = parameters[self.IDX_AOI_LAYER].valueAsText
        aoi_name_field   = parameters[self.IDX_AOI_NAME_FIELD].valueAsText
        engine           = parameters[self.IDX_ENGINE].valueAsText
        grid_xml         = parameters[self.IDX_GRID_XML].valueAsText
        out_ws           = parameters[self.IDX_OUT_WS].valueAsText
        fds_name         = parameters[self.IDX_FDS_NAME].valueAsText
        refscale         = _safe_float(parameters[self.IDX_REFSCALE].value, 0.0)

        respect_rot      = bool(parameters[self.IDX_RESPECT_ROT].value)
        spacing_proj     = _safe_float(
            parameters[self.IDX_SPACING_PROJ].value, 0.0)
        divisor_proj     = _safe_float(
            parameters[self.IDX_DIVISOR_PROJ].value, 1.0)
        tick_mm          = _safe_float(parameters[self.IDX_TICK_MM].value, 1.5)
        label_mm         = _safe_float(
            parameters[self.IDX_LABEL_MM].value, 3.0)
        create_glines    = bool(parameters[self.IDX_CREATE_GLINES].value)
        proj_label_mode  = (parameters[self.IDX_PROJ_LBL_MODE].valueAsText
                            or "INT")
        proj_unit_suffix = (parameters[self.IDX_PROJ_UNIT_SUFFIX].valueAsText
                            or "")
        proj_pad3        = bool(parameters[self.IDX_PROJ_PAD3].value)

        enable_grat      = bool(parameters[self.IDX_ENABLE_GRAT].value)
        grat_minutes     = _safe_float(
            parameters[self.IDX_GRAT_MINUTES].value, 0.0)
        grat_mode        = (parameters[self.IDX_GRAT_MODE].valueAsText
                            or "TRUE_INTERVAL")
        geo_wkid         = _safe_int(
            parameters[self.IDX_GEO_WKID].value, 4326)
        grat_label_mm    = _safe_float(
            parameters[self.IDX_GRAT_LBL_MM].value, 3.5)
        grat_hemi        = bool(parameters[self.IDX_GRAT_HEMI].value)

        deover           = bool(parameters[self.IDX_DEOVERLAP].value)
        min_sep_mm       = _safe_float(
            parameters[self.IDX_MIN_SEP_MM].value, 1.5)
        corner_extra_mm  = _safe_float(
            parameters[self.IDX_CORNER_EXTRA_MM].value, 1.0)

        continue_on_err  = bool(parameters[self.IDX_CONTINUE_ON_ERR].value)
        max_ticks_per_axis = _safe_int(
            parameters[self.IDX_MAX_TICKS].value, 5000)
        cleanup_sheet    = bool(parameters[self.IDX_CLEANUP_SHEET].value)
        dry_run          = bool(parameters[self.IDX_DRY_RUN].value)
        symbology_lyr    = parameters[self.IDX_SYMBOLOGY_LYR].valueAsText

        mask_mm          = _safe_float(
            parameters[self.IDX_MASK_MM].value, 5.0)
        xml_autofix      = bool(parameters[self.IDX_XML_AUTOFIX].value)
        xml_delta_anc    = _safe_float(
            parameters[self.IDX_XML_DELTA_ANC].value, 2.0)

        add_to_map       = bool(parameters[self.IDX_ADD_TO_MAP].value)
        save_aprx_copy   = bool(parameters[self.IDX_SAVE_APRX_COPY].value)
        out_aprx_folder  = parameters[self.IDX_OUT_APRX_FOLDER].valueAsText
        export_pdf       = bool(parameters[self.IDX_EXPORT_PDF].value)
        out_pdf_folder   = parameters[self.IDX_OUT_PDF_FOLDER].valueAsText
        pdf_dpi          = _safe_int(parameters[self.IDX_PDF_DPI].value, 300)
        export_png       = bool(parameters[self.IDX_EXPORT_PNG].value)
        export_jpeg      = bool(parameters[self.IDX_EXPORT_JPEG].value)
        out_img_folder   = parameters[self.IDX_OUT_IMG_FOLDER].valueAsText
        img_dpi          = _safe_int(parameters[self.IDX_IMG_DPI].value, 300)
        log_file_param   = parameters[self.IDX_LOG_FILE].valueAsText

        # Selection-bypass on AOI (Master Rule 3)
        aoi_layer = None
        aoi_layer_path = None
        if mode == "AOI_LAYER_IN_CURRENT_PROJECT" and aoi_layer_param:
            _announce_selection("AOI", aoi_layer_param)
            aoi_layer = aoi_layer_param
            aoi_layer_path = _resolve_full_source(
                aoi_layer_param, ignore_selection=True)

        gdb = _ensure_file_gdb(out_ws, "grid_output.gdb")
        if not log_file_param:
            log_file = os.path.join(
                os.path.dirname(gdb),
                f"grid_batch_{time.strftime('%Y%m%d_%H%M%S')}.log")
        else:
            log_file = log_file_param

        _log_line(log_file,
                  f"==== BatchGridBuilder07 master-rules start "
                  f"{_now_str()} engine={engine} mode={mode} "
                  f"max_ticks_per_axis={max_ticks_per_axis} ====")
        _diag(f"Engine={engine} Mode={mode} Output GDB={gdb}")
        _diag(f"Max Ticks Per Axis (cap)={max_ticks_per_axis}")

        if refscale <= 0:
            _raise("Reference scale denominator must be > 0.")
        if max_ticks_per_axis <= 0:
            _raise("Max Ticks Per Axis must be > 0.")

        # Resolve project list
        if mode == "FOLDER_OF_APRX":
            if not aprx_folder or not os.path.isdir(aprx_folder):
                _raise("APRX folder not found.")
            aprx_paths = _list_aprx(aprx_folder, recursive)
            if not aprx_paths:
                _raise("No .aprx files found in folder.")
            _diag(f"APRX files to process: {len(aprx_paths)}")
        else:
            aprx_paths = [None]
            if not aoi_layer:
                _raise("AOI layer is required for AOI mode.")

        # ESRI_XML: validate template and (optionally) auto-patch offsets.
        if engine == "ESRI_XML":
            if not grid_xml or not os.path.isfile(grid_xml):
                _raise("Grid Template XML not found.")
            if xml_autofix:
                temp_dir = os.path.join(
                    arcpy.env.scratchFolder or os.path.dirname(gdb),
                    "_grid_xml_patch")
                grid_xml = _xml_autofix_temp(
                    grid_xml, temp_dir,
                    delta_primary=0.0,
                    delta_ancillary=xml_delta_anc,
                    only_ancillary=True,
                    verbose=True)

        # Process each project
        try:
            arcpy.SetProgressor("step", "Processing projects...",
                                  0, max(1, len(aprx_paths)), 1)
        except (arcpy.ExecuteError, RuntimeError):
            pass

        for idx_proj, aprx_path in enumerate(aprx_paths):
            if getattr(arcpy.env, "autoCancelling", False) and \
                    arcpy.env.isCancelled:
                raise arcpy.ExecuteError("Cancelled by user.")
            aprx = None
            try:
                if mode == "FOLDER_OF_APRX":
                    _diag(
                        f"Open APRX ({idx_proj + 1}/{len(aprx_paths)}): "
                        f"{aprx_path}")
                    aprx = _open_project(aprx_path)
                else:
                    aprx = _open_project(None)

                layout = _find_layout_or_first(aprx, layout_name)
                mf, m = _layout_first_mapframe(layout)
                if mf is None:
                    _raise("No MAPFRAME element found in the layout.")
                sr = mf.map.spatialReference if mf.map else None
                if sr is None:
                    _raise(
                        "Could not determine spatial reference of the map "
                        "frame.")

                # F2: SMART_FEATURE GCS warning
                sr_type = (getattr(sr, "type", "") or "").lower()
                if engine == "SMART_FEATURE" and sr_type == "geographic":
                    msg = (
                        "SMART_FEATURE engine: input map frame uses a "
                        "GEOGRAPHIC coordinate system. The SMART engine "
                        "expects PROJECTED coordinates - tick spacing, mm "
                        "conversions and grid lines will be in degrees, "
                        "not map units. Re-project the map frame to a "
                        "projected CRS for correct cartographic output.")
                    _add_warn(msg)
                    _log_line(
                        log_file,
                        f"[{_now_str()}] WARN SMART_FEATURE GCS detected "
                        f"aprx={aprx_path or 'CURRENT'}")
                    if not continue_on_err:
                        _raise(msg)

                if engine == "ESRI_XML" and sr_type == "geographic":
                    _raise("ESRI_XML engine requires a projected CRS for "
                           "the map frame.")

                fds = _ensure_feature_dataset(gdb, fds_name, sr)

                # Determine sheets list: (sheet_name, extent, oid)
                sheets: List[Tuple[str, object, Optional[int]]] = []
                oid_field = None
                if mode == "FOLDER_OF_APRX":
                    sheet_name = os.path.splitext(
                        os.path.basename(aprx_path))[0]
                    ext = None
                    try:
                        cam = mf.camera
                        ext = cam.getExtent()
                    except (arcpy.ExecuteError, RuntimeError, AttributeError):
                        ext = None
                    if ext is None:
                        try:
                            ext = mf.map.defaultCamera.getExtent()
                        except (arcpy.ExecuteError, RuntimeError,
                                AttributeError):
                            ext = None
                    if ext is None:
                        _raise(
                            f"Could not determine extent of map frame for "
                            f"{aprx_path}")
                    sheets.append((sheet_name, ext, None))
                else:
                    src = aoi_layer_path or aoi_layer
                    try:
                        oid_field = arcpy.Describe(src).OIDFieldName
                    except (arcpy.ExecuteError, RuntimeError):
                        oid_field = "OBJECTID"
                    name_field = aoi_name_field
                    fields = ["OID@", "SHAPE@"]
                    if name_field:
                        fields.insert(1, name_field)
                    with arcpy.da.SearchCursor(
                            src, fields,
                            sql_clause=(
                                None,
                                f"ORDER BY {oid_field} ASC")) as sc:
                        for row in sc:
                            oid = row[0]
                            geom = row[-1]
                            ext = geom.extent if geom else None
                            if not ext:
                                continue
                            if name_field:
                                nm = (str(row[1])
                                      if row[1] is not None else str(oid))
                            else:
                                nm = str(oid)
                            sheets.append((nm, ext, oid))
                    if not sheets:
                        _raise("AOI layer has no valid features/extents.")
                    _diag(f"AOI sheets: {len(sheets)}")

                try:
                    arcpy.SetProgressor("step", "Processing sheets...",
                                          0, max(1, len(sheets)), 1)
                except (arcpy.ExecuteError, RuntimeError):
                    pass

                df_rotation = 0.0
                try:
                    df_rotation = float(getattr(mf, "rotation", 0.0) or 0.0)
                except (TypeError, ValueError, AttributeError):
                    df_rotation = 0.0

                for (sheet_name, ext, oid) in sheets:
                    if getattr(arcpy.env, "autoCancelling", False) and \
                            arcpy.env.isCancelled:
                        raise arcpy.ExecuteError("Cancelled by user.")
                    sheet_key = _validate_name(sheet_name, gdb)
                    _log_line(log_file,
                              f"[{_now_str()}] START sheet={sheet_key}")
                    try:
                        if engine == "SMART_FEATURE":
                            grat_deg = (grat_minutes / 60.0
                                        if grat_minutes
                                        and grat_minutes > 0 else 0.0)
                            r = _build_ticks_and_labels_for_extent(
                                ext=ext, sheet_key=sheet_key,
                                fds=fds, sr=sr,
                                scale_denom=refscale,
                                df_rotation=df_rotation,
                                spacing_proj=spacing_proj,
                                divisor_proj=divisor_proj,
                                tick_len_mm=tick_mm,
                                label_offset_mm=label_mm,
                                create_grid_lines=create_glines,
                                enable_graticule=enable_grat,
                                graticule_interval_deg=grat_deg,
                                graticule_mode=grat_mode,
                                geo_wkid=geo_wkid,
                                graticule_label_offset_mm=grat_label_mm,
                                graticule_show_hemi=grat_hemi,
                                proj_label_mode=proj_label_mode,
                                proj_unit_suffix=proj_unit_suffix,
                                proj_pad3=proj_pad3,
                                deoverlap_corners=deover,
                                min_sep_mm=min_sep_mm,
                                corner_extra_mm=corner_extra_mm,
                                respect_df_rotation=respect_rot,
                                max_ticks_per_axis=max_ticks_per_axis,
                                continue_on_error=continue_on_err,
                                log_file=log_file,
                                dry_run=dry_run,
                                cleanup_sheet=cleanup_sheet)
                            if r.get("aborted"):
                                # F1: page aborted, move on to next sheet.
                                _log_line(
                                    log_file,
                                    f"[{_now_str()}] SKIPPED sheet="
                                    f"{sheet_key} reason="
                                    f"{r.get('abort_reason')}")
                        else:
                            extent_str = (
                                f"{ext.XMin} {ext.YMin} "
                                f"{ext.XMax} {ext.YMax}")
                            out_layer_name = (
                                f"GRID_{sheet_key}_"
                                f"{oid if oid is not None else 'EXT'}")
                            aoi_arg = extent_str
                            grid_name_arg = sheet_key
                            if (mode != "FOLDER_OF_APRX"
                                    and oid is not None and aoi_layer):
                                try:
                                    arcpy.management.SelectLayerByAttribute(
                                        aoi_layer, "NEW_SELECTION",
                                        f"{oid_field} = {oid}")
                                    aoi_arg = aoi_layer
                                    if aoi_name_field:
                                        grid_name_arg = aoi_name_field
                                except (arcpy.ExecuteError,
                                        RuntimeError) as ex:
                                    arcpy.AddError(arcpy.GetMessages(2))
                                    _add_warn(
                                        f"AOI selection failed; using "
                                        f"extent string: {ex}")
                                    aoi_arg = extent_str
                                    grid_name_arg = sheet_key
                            try:
                                _run_esri_make_grids(
                                    template_xml=grid_xml,
                                    aoi=aoi_arg,
                                    fds=fds,
                                    out_layer_name=out_layer_name,
                                    grid_name=grid_name_arg,
                                    refscale=refscale,
                                    rotation=(df_rotation
                                              if respect_rot else 0.0),
                                    mask_mm=mask_mm,
                                    primary_sr=sr,
                                    configure_layout=False)
                            finally:
                                if (mode != "FOLDER_OF_APRX" and aoi_layer):
                                    try:
                                        arcpy.management. \
                                            SelectLayerByAttribute(
                                                aoi_layer,
                                                "CLEAR_SELECTION")
                                    except (arcpy.ExecuteError,
                                            RuntimeError):
                                        pass
                            _add_msg(
                                f"Created ESRI grid layer: {out_layer_name}")
                        _log_line(log_file,
                                  f"[{_now_str()}] DONE sheet={sheet_key}")
                    except (arcpy.ExecuteError, RuntimeError) as ex:
                        arcpy.AddError(arcpy.GetMessages(2))
                        _log_line(
                            log_file,
                            f"[{_now_str()}] ERROR sheet={sheet_key} "
                            f"err={ex}")
                        if not continue_on_err:
                            raise
                        _add_warn(f"Sheet {sheet_key} failed: {ex}")
                    try:
                        arcpy.SetProgressorPosition()
                    except (arcpy.ExecuteError, RuntimeError):
                        pass

                # Add SMART_FEATURE outputs to active map (per project)
                if (engine == "SMART_FEATURE") and add_to_map \
                        and (not dry_run):
                    try:
                        _add_smart_outputs_to_map(
                            m, fds, symbology_layerfile=symbology_lyr)
                    except (arcpy.ExecuteError, RuntimeError) as ex:
                        arcpy.AddError(arcpy.GetMessages(2))
                        _add_warn(
                            f"Failed to add output layers to map: {ex}")

                try:
                    arcpy.ResetProgressor()
                except (arcpy.ExecuteError, RuntimeError):
                    pass

                # Save / Export per project
                if mode == "FOLDER_OF_APRX" and (not dry_run):
                    base = os.path.splitext(
                        os.path.basename(aprx_path))[0]
                    if save_aprx_copy:
                        if not out_aprx_folder:
                            _raise("Output APRX Folder is required when "
                                   "Save APRX copy is True.")
                        _mkdir(out_aprx_folder)
                        out_aprx = os.path.join(
                            out_aprx_folder, base + "_grid.aprx")
                        try:
                            aprx.saveACopy(out_aprx)
                            _add_msg(f"Saved APRX copy: {out_aprx}")
                        except (arcpy.ExecuteError, RuntimeError) as ex:
                            arcpy.AddError(arcpy.GetMessages(2))
                            _add_warn(f"saveACopy failed: {ex}")
                    if export_pdf:
                        if not out_pdf_folder:
                            _raise("Output PDF Folder is required when "
                                   "Export PDF is True.")
                        _mkdir(out_pdf_folder)
                        out_pdf = os.path.join(
                            out_pdf_folder, base + ".pdf")
                        try:
                            if layout is not None:
                                layout.exportToPDF(out_pdf,
                                                    resolution=pdf_dpi)
                                _add_msg(f"Exported PDF: {out_pdf}")
                            else:
                                _add_warn(
                                    "No layout available; skipping PDF "
                                    "export.")
                        except (arcpy.ExecuteError, RuntimeError) as ex:
                            arcpy.AddError(arcpy.GetMessages(2))
                            _add_warn(f"exportToPDF failed: {ex}")
                    if export_png or export_jpeg:
                        if not out_img_folder:
                            _raise("Output Image Folder is required for "
                                   "PNG/JPEG export.")
                        _mkdir(out_img_folder)
                        if export_png:
                            out_png = os.path.join(
                                out_img_folder, base + ".png")
                            try:
                                if layout is not None:
                                    layout.exportToPNG(
                                        out_png, resolution=img_dpi)
                                    _add_msg(f"Exported PNG: {out_png}")
                                else:
                                    _add_warn(
                                        "No layout available; skipping "
                                        "PNG export.")
                            except (arcpy.ExecuteError,
                                    RuntimeError) as ex:
                                arcpy.AddError(arcpy.GetMessages(2))
                                _add_warn(f"exportToPNG failed: {ex}")
                        if export_jpeg:
                            out_jpg = os.path.join(
                                out_img_folder, base + ".jpg")
                            try:
                                if layout is not None:
                                    layout.exportToJPEG(
                                        out_jpg, resolution=img_dpi)
                                    _add_msg(f"Exported JPEG: {out_jpg}")
                                else:
                                    _add_warn(
                                        "No layout available; skipping "
                                        "JPEG export.")
                            except (arcpy.ExecuteError,
                                    RuntimeError) as ex:
                                arcpy.AddError(arcpy.GetMessages(2))
                                _add_warn(f"exportToJPEG failed: {ex}")
            except (arcpy.ExecuteError, RuntimeError) as ex:
                arcpy.AddError(arcpy.GetMessages(2))
                _log_line(
                    log_file,
                    f"[{_now_str()}] ERROR aprx={aprx_path or 'CURRENT'} "
                    f"err={ex}")
                if not continue_on_err:
                    raise
                _add_warn(
                    f"Failed on {aprx_path or 'CURRENT'}: {ex}")
            finally:
                # Master Rule 6: per-project teardown.
                if aprx is not None:
                    try:
                        del aprx
                    except (NameError, AttributeError):
                        pass
                try:
                    arcpy.management.ClearWorkspaceCache()
                except (arcpy.ExecuteError, RuntimeError):
                    pass
                gc.collect()
            try:
                arcpy.SetProgressorPosition()
            except (arcpy.ExecuteError, RuntimeError):
                pass

        try:
            arcpy.ResetProgressor()
        except (arcpy.ExecuteError, RuntimeError):
            pass

        _log_line(log_file,
                  f"==== BatchGridBuilder07 master-rules finished "
                  f"{_now_str()} ====")
        _add_msg(f"Done. Log: {log_file}")
