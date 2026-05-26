# -*- coding: utf-8 -*-
"""
Plugin 07 - Batch Grid / Graticule Builder (ArcGIS Pro / Python 3)  v6 NATIVE
==============================================================================
Native Pro rewrite. Same hardening philosophy as the ArcMap v6 build
(selection bypass, narrow exceptions, [DIAG] logging, Pro-only memory
discipline). Differences vs the ArcMap version:

  * Uses arcpy.mp.ArcGISProject instead of arcpy.mapping.MapDocument.
  * Iterates layouts in the project (or "CURRENT" for the active project)
    and extracts each layout's first map frame extent, instead of
    .mxd files. The "FOLDER_OF_APRX" mode walks .aprx files in a folder.
  * Uses arcpy.management / arcpy.analysis namespaces consistently.
  * Map integration: addDataFromPath on the active map (not AddLayer).
  * Pro-only "Add outputs to current map" toggle.

Two engines (same as ArcMap):

  ESRI_XML
    Wraps arcpy.cartography.MakeGridsAndGraticulesLayer with an XML
    template. Best for production cartography styles. Loops the AOI
    features. Optional XML AutoFix nudges ancillary corner labels by
    a few mm to reduce UTM-vs-Lat/Lon corner collisions.

  SMART_FEATURE
    Pure-arcpy engine writing Tick / Label / GridLine FCs into a
    feature dataset, with optional Lat/Lon graticule via projected
    edge sampling and a corner de-overlap pass.

Author: Ali Mirjafari + Kiro
Version: 6.0 (Pro / Python 3)
"""

from __future__ import annotations

import os
import re
import math
import time
import uuid
import gc
import traceback
from typing import Iterable, List, Optional, Tuple

import arcpy

try:
    import xml.etree.ElementTree as ET
except Exception:
    ET = None

try:
    from lxml import etree as LET
except Exception:
    LET = None


# =============================================================================
# 0. Messaging / utility helpers
# =============================================================================

def _now_str() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")

def _add_msg(msg) -> None:
    try:
        arcpy.AddMessage(str(msg))
    except Exception:
        pass

def _add_warn(msg) -> None:
    try:
        arcpy.AddWarning(str(msg))
    except Exception:
        pass

def _add_err(msg) -> None:
    try:
        arcpy.AddError(str(msg))
    except Exception:
        pass

def _diag(msg) -> None:
    _add_msg(f"[DIAG] {msg}")

def _raise(msg: str):
    _add_err(msg)
    raise arcpy.ExecuteError(msg)

def _safe_float(v, default: float = 0.0) -> float:
    try:
        if v is None:
            return default
        return float(v)
    except Exception:
        return default

def _safe_int(v, default: int = 0) -> int:
    try:
        if v is None:
            return default
        return int(v)
    except Exception:
        return default

def _mkdir(folder: Optional[str]) -> None:
    try:
        if folder and not os.path.isdir(folder):
            os.makedirs(folder, exist_ok=True)
    except Exception:
        pass

def _is_gdb(path: Optional[str]) -> bool:
    return bool(path and path.lower().endswith(".gdb") and os.path.isdir(path))

def _ensure_file_gdb(workspace_or_folder: str,
                      gdb_name: str = "grid_output.gdb") -> str:
    if not workspace_or_folder:
        _raise("Output workspace is empty.")
    ws = workspace_or_folder
    if _is_gdb(ws):
        return ws
    if not os.path.isdir(ws):
        _raise(f"Output folder not found: {ws}")
    gdb = os.path.join(ws, gdb_name)
    if not os.path.isdir(gdb):
        _add_msg(f"Creating GDB: {gdb}")
        arcpy.management.CreateFileGDB(ws, os.path.basename(gdb))
    return gdb

def _ensure_feature_dataset(gdb: str, fds_name: str, spatial_ref) -> str:
    if not fds_name:
        fds_name = "Grids"
    fds_name = _validate_name(fds_name, gdb)
    fds = os.path.join(gdb, fds_name)
    if not arcpy.Exists(fds):
        arcpy.management.CreateFeatureDataset(gdb, fds_name, spatial_ref)
    return fds

def _validate_name(name, workspace) -> str:
    if name is None:
        name = "GRID"
    raw = str(name).strip()
    if not raw:
        raw = "GRID"
    raw = re.sub(r"[^0-9A-Za-z_]+", "_", raw)
    raw = raw.strip("_")
    if not raw:
        raw = "GRID"
    try:
        v = arcpy.ValidateTableName(raw, workspace)
        if v:
            raw = v
    except Exception:
        pass
    if re.match(r"^[0-9]", raw):
        raw = "X_" + raw
    if len(raw) > 64:
        raw = raw[:64]
    return raw

def _product_level() -> Optional[str]:
    try:
        return arcpy.ProductInfo()
    except Exception:
        return None

def _require_cartography_level() -> None:
    lvl = _product_level()
    if not lvl:
        return
    if (lvl or "").lower() in ("basic", "arcview"):
        _raise(f"Cartography tool requires Standard/Advanced license. "
               f"Current product level: {lvl}")


# =============================================================================
# 1. Selection-bypass: resolve any layer to its on-disk source
# =============================================================================

def _selection_info(layer_or_path):
    try:
        d = arcpy.Describe(layer_or_path)
    except Exception:
        return (None, None, str(layer_or_path))
    name = getattr(d, "name", str(layer_or_path))
    fidset = getattr(d, "FIDSet", "") or ""
    try:
        total = int(arcpy.management.GetCount(layer_or_path).getOutput(0))
    except Exception:
        total = None
    if not fidset.strip():
        return (0, total, name)
    sel = len([t for t in fidset.split(";") if t.strip() != ""])
    return (sel, total, name)

def _resolve_full_source(layer_or_path):
    if not layer_or_path:
        return layer_or_path
    try:
        d = arcpy.Describe(layer_or_path)
        cp = getattr(d, "catalogPath", None)
        if cp:
            return cp
    except Exception:
        pass
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
# 2. File logging (Pro: simple no-lock open)
# =============================================================================

def _log_line(fp: Optional[str], line: str) -> None:
    if not fp:
        return
    try:
        _mkdir(os.path.dirname(fp))
    except Exception:
        pass
    try:
        with open(fp, "a", encoding="utf-8", errors="replace") as f:
            f.write(line)
            if not line.endswith("\n"):
                f.write("\n")
    except Exception:
        pass


# =============================================================================
# 3. Geometry + unit helpers
# =============================================================================

def _mm_to_map_units(mm: float, scale_denom: float, spatial_ref,
                     allow_geo_approx: bool = False,
                     approx_lat_deg: Optional[float] = None) -> float:
    mm = _safe_float(mm, 0.0)
    scale_denom = _safe_float(scale_denom, 0.0)
    if mm <= 0 or scale_denom <= 0:
        return 0.0
    if spatial_ref and getattr(spatial_ref, "type", None) == "Geographic":
        if not allow_geo_approx:
            _raise("Spatial Reference is Geographic (degrees). mm->map units "
                   "is not reliable. Project to a projected CRS (e.g., UTM).")
        lat = _safe_float(approx_lat_deg, 0.0)
        meters = (mm * scale_denom) / 1000.0
        meters_per_degree = 111320.0 * max(0.1, abs(math.cos(math.radians(lat))))
        return meters / meters_per_degree
    meters = (mm * scale_denom) / 1000.0
    mpu = None
    if spatial_ref is not None:
        try:
            mpu = float(getattr(spatial_ref, "metersPerUnit", None))
        except Exception:
            mpu = None
    if mpu and mpu > 0:
        return meters / mpu
    return meters

def _rotate_xy(x, y, cx, cy, ang_deg):
    if not ang_deg:
        return (x, y)
    th = math.radians(ang_deg)
    dx = x - cx
    dy = y - cy
    xr = cx + (dx * math.cos(th) - dy * math.sin(th))
    yr = cy + (dx * math.sin(th) + dy * math.cos(th))
    return (xr, yr)

def _extent_edges_display(ext, respect_rotation: bool = True,
                           rotation_deg: float = 0.0) -> dict:
    xmin, ymin, xmax, ymax = ext.XMin, ext.YMin, ext.XMax, ext.YMax
    cx = (xmin + xmax) / 2.0
    cy = (ymin + ymax) / 2.0
    return {
        "BOTTOM": ((xmin, ymin), (xmax, ymin)),
        "TOP":    ((xmin, ymax), (xmax, ymax)),
        "LEFT":   ((xmin, ymin), (xmin, ymax)),
        "RIGHT":  ((xmax, ymin), (xmax, ymax)),
        "XMIN": xmin, "YMIN": ymin, "XMAX": xmax, "YMAX": ymax,
        "CX": cx, "CY": cy,
        "ROT": (rotation_deg if (respect_rotation and rotation_deg) else 0.0),
    }

def _display_to_data_xy(x, y, edges):
    rot = edges.get("ROT", 0.0)
    if not rot:
        return (x, y)
    return _rotate_xy(x, y, edges["CX"], edges["CY"], -rot)

def _data_to_display_xy(x, y, edges):
    rot = edges.get("ROT", 0.0)
    if not rot:
        return (x, y)
    return _rotate_xy(x, y, edges["CX"], edges["CY"], rot)

def _ceil_to_interval(v, interval):
    return math.ceil(v / float(interval)) * float(interval)

def _floor_to_interval(v, interval):
    return math.floor(v / float(interval)) * float(interval)

def _compute_values(minv, maxv, interval, max_count: int = 20000):
    if interval <= 0:
        return []
    a = _ceil_to_interval(minv, interval)
    b = _floor_to_interval(maxv, interval)
    if b < a:
        return []
    n = int(round((b - a) / float(interval))) + 1
    if n > max_count:
        _raise(f"Too many ticks/grid lines ({n}). Increase interval or set "
               f"a larger max_ticks.")
    return [a + i * interval for i in range(n)]

def _format_proj(val, divisor, fmt_mode: str = "INT",
                 unit_suffix: str = "", pad3: bool = False) -> str:
    v = val / float(divisor) if (divisor and divisor != 0) else val
    if fmt_mode == "FLOAT":
        s = f"{v:.3f}"
    else:
        try:
            iv = int(round(v))
        except Exception:
            iv = int(v)
        s = f"{iv:03d}" if pad3 else str(iv)
    if unit_suffix:
        s = s + unit_suffix
    return s

def _format_dms(deg: Optional[float], is_lon: bool = True,
                show_hemi: bool = False, decimals: int = 0) -> str:
    if deg is None:
        return ""
    sign = -1.0 if deg < 0 else 1.0
    d = abs(deg)
    dd = int(d)
    mm_f = (d - dd) * 60.0
    mm = int(mm_f)
    ss_f = (mm_f - mm) * 60.0
    if decimals and decimals > 0:
        ss = round(ss_f, decimals)
    else:
        ss = int(round(ss_f))
    if ss >= 60:
        ss = 0; mm += 1
    if mm >= 60:
        mm = 0; dd += 1
    if decimals and decimals > 0:
        ss_str = f"{ss:0{2 + 1 + decimals}.{decimals}f}"
    else:
        ss_str = f"{int(ss):02d}"
    s = f"{dd}\u00b0{mm:02d}'{ss_str}\""
    if show_hemi:
        hemi = ("E" if sign >= 0 else "W") if is_lon else ("N" if sign >= 0 else "S")
        s = s + hemi
    elif sign < 0:
        s = "-" + s
    return s

def _project_points(points_xy, in_sr, out_sr, continue_on_error: bool = False):
    out = []
    for (x, y) in points_xy:
        pg = arcpy.PointGeometry(arcpy.Point(x, y), in_sr)
        try:
            gg = pg.projectAs(out_sr)
            p = gg.firstPoint
            out.append((p.X, p.Y))
        except Exception as e:
            if continue_on_error:
                out.append((None, None))
            else:
                _raise(f"Projection failed: {e}")
    return out

def _delete_by_sheet(fc, sheet_value):
    if (not fc) or (not arcpy.Exists(fc)):
        return
    lyr = "lyr_" + uuid.uuid4().hex[:8]
    try:
        arcpy.management.MakeFeatureLayer(fc, lyr)
        wc = f"SHEET = '{sheet_value.replace(chr(39), chr(39)+chr(39))}'"
        arcpy.management.SelectLayerByAttribute(lyr, "NEW_SELECTION", wc)
        arcpy.management.DeleteFeatures(lyr)
    finally:
        try:
            arcpy.management.Delete(lyr)
        except Exception:
            pass


# =============================================================================
# 4. XML AutoFix (best-effort)
# =============================================================================

_OFFSET_KEYS = ["offset", "labeloffset", "xoffset", "yoffset", "anchor"]

def _xml_autofix_temp(xml_path: Optional[str], temp_folder: str,
                      delta_primary: float = 0.0,
                      delta_ancillary: float = 2.0,
                      only_ancillary: bool = True,
                      verbose: bool = False) -> Optional[str]:
    if (not xml_path) or (not os.path.isfile(xml_path)):
        return xml_path
    _mkdir(temp_folder)
    out_xml = os.path.join(
        temp_folder,
        f"patched_{uuid.uuid4().hex[:6]}_{os.path.basename(xml_path)}")

    def _is_offset_key(s):
        s = (s or "").lower()
        return any(k in s for k in _OFFSET_KEYS)

    def _is_ancillary_node(tag, attrib):
        if "ancillary" in (tag or "").lower():
            return True
        for k in attrib.keys():
            if "ancillary" in (k or "").lower():
                return True
        for v in attrib.values():
            if "ancillary" in (str(v) if v is not None else "").lower():
                return True
        return False

    def _try_float(s):
        try:
            return float(s)
        except Exception:
            return None

    if LET is not None:
        try:
            parser = LET.XMLParser(remove_blank_text=True, recover=True)
            root = LET.parse(xml_path, parser).getroot()
            touched = 0
            for elem in root.iter():
                is_anc = _is_ancillary_node(elem.tag, elem.attrib)
                for ak in list(elem.attrib.keys()):
                    if _is_offset_key(ak):
                        v = _try_float(elem.attrib.get(ak))
                        if v is None:
                            continue
                        if is_anc:
                            elem.attrib[ak] = str(v + float(delta_ancillary)); touched += 1
                        elif (not only_ancillary) and delta_primary:
                            elem.attrib[ak] = str(v + float(delta_primary)); touched += 1
                if elem.text and _is_offset_key(elem.tag):
                    v = _try_float(elem.text)
                    if v is not None:
                        if is_anc:
                            elem.text = str(v + float(delta_ancillary)); touched += 1
                        elif (not only_ancillary) and delta_primary:
                            elem.text = str(v + float(delta_primary)); touched += 1
            if verbose:
                _diag(f"XML AutoFix: modified {touched} offset values (lxml)")
            LET.ElementTree(root).write(
                out_xml, encoding="utf-8",
                xml_declaration=True, pretty_print=True)
            return out_xml
        except Exception:
            _add_warn(f"lxml XML AutoFix failed; falling back to ET. "
                      f"{traceback.format_exc()}")

    if ET is None:
        _add_warn("xml.etree not available; skipping XML AutoFix.")
        return xml_path

    try:
        tree = ET.parse(xml_path)
        root = tree.getroot()
        touched = 0
        for elem in root.iter():
            is_anc = _is_ancillary_node(elem.tag, elem.attrib)
            for ak in list(elem.attrib.keys()):
                if _is_offset_key(ak):
                    v = _try_float(elem.attrib.get(ak))
                    if v is None:
                        continue
                    if is_anc:
                        elem.attrib[ak] = str(v + float(delta_ancillary)); touched += 1
                    elif (not only_ancillary) and delta_primary:
                        elem.attrib[ak] = str(v + float(delta_primary)); touched += 1
            if elem.text and _is_offset_key(elem.tag):
                v = _try_float(elem.text)
                if v is not None:
                    if is_anc:
                        elem.text = str(v + float(delta_ancillary)); touched += 1
                    elif (not only_ancillary) and delta_primary:
                        elem.text = str(v + float(delta_primary)); touched += 1
        if verbose:
            _diag(f"XML AutoFix: modified {touched} offset values")
        tree.write(out_xml, encoding="utf-8")
        return out_xml
    except Exception:
        _add_warn(f"XML AutoFix failed; using original template. "
                  f"{traceback.format_exc()}")
        return xml_path



# =============================================================================
# 5. SMART_FEATURE engine
# =============================================================================

def _ensure_fc(fds, name, geom_type, spatial_ref, fields):
    fc = os.path.join(fds, name)
    if not arcpy.Exists(fc):
        arcpy.management.CreateFeatureclass(
            fds, name, geom_type, spatial_reference=spatial_ref)
        for (fn, ft, flen) in fields:
            if ft.upper() == "TEXT":
                arcpy.management.AddField(fc, fn, ft, field_length=flen)
            else:
                arcpy.management.AddField(fc, fn, ft)
    return fc

def _insert_tick(cur, sheet, typ, side, val, geom, is_major=1):
    cur.insertRow((geom, sheet, typ, side, float(val), int(is_major)))

def _insert_label(cur, sheet, typ, side, text, geom, rot=0.0):
    cur.insertRow((geom, sheet, typ, side, text, float(rot)))

def _build_ticks_and_labels_for_extent(
        ext, sheet_key, fds, sr, scale_denom, df_rotation,
        spacing_proj, divisor_proj,
        tick_len_mm, label_offset_mm,
        create_grid_lines=False,
        enable_graticule=False,
        graticule_interval_deg=0.0,
        graticule_mode="TRUE_INTERVAL",
        geo_wkid=4326,
        graticule_label_offset_mm=3.0,
        graticule_show_hemi=False,
        proj_label_mode="INT",
        proj_unit_suffix="",
        proj_pad3=False,
        deoverlap_corners=True,
        min_sep_mm=1.5,
        corner_extra_mm=1.0,
        respect_df_rotation=True,
        allow_geo_approx=False,
        max_ticks=20000,
        continue_on_error=False,
        log_file=None,
        dry_run=False,
        cleanup_sheet=True):
    spacing_proj = _safe_float(spacing_proj, 0.0)
    if spacing_proj <= 0:
        _raise("spacing_proj must be > 0")
    divisor_proj = _safe_float(divisor_proj, 1.0)
    if divisor_proj == 0:
        _raise("divisor_proj must not be 0")
    scale_denom = _safe_float(scale_denom, 0.0)
    if scale_denom <= 0:
        _raise("Reference scale denominator must be > 0 (e.g., 25000).")
    if getattr(sr, "type", None) == "Geographic":
        _raise("SMART_FEATURE engine requires Projected CRS for the data frame.")

    rot = _safe_float(df_rotation, 0.0)
    edges = _extent_edges_display(ext, respect_rotation=respect_df_rotation,
                                   rotation_deg=rot)

    approx_lat = None
    if allow_geo_approx:
        try:
            geo_sr = arcpy.SpatialReference(4326)
            mid_xy = [((edges["XMIN"] + edges["XMAX"]) / 2.0,
                       (edges["YMIN"] + edges["YMAX"]) / 2.0)]
            mid_ll = _project_points(mid_xy, sr, geo_sr, continue_on_error=True)
            approx_lat = mid_ll[0][1]
        except Exception:
            approx_lat = 0.0

    tick_len = _mm_to_map_units(tick_len_mm, scale_denom, sr,
                                 allow_geo_approx, approx_lat_deg=approx_lat)
    lab_off  = _mm_to_map_units(label_offset_mm, scale_denom, sr,
                                 allow_geo_approx, approx_lat_deg=approx_lat)
    grat_off = _mm_to_map_units(graticule_label_offset_mm, scale_denom, sr,
                                 allow_geo_approx, approx_lat_deg=approx_lat)
    min_sep  = _mm_to_map_units(min_sep_mm, scale_denom, sr,
                                 allow_geo_approx, approx_lat_deg=approx_lat)
    corner_extra = _mm_to_map_units(corner_extra_mm, scale_denom, sr,
                                     allow_geo_approx, approx_lat_deg=approx_lat)

    xmin, ymin = edges["XMIN"], edges["YMIN"]
    xmax, ymax = edges["XMAX"], edges["YMAX"]

    xs = _compute_values(xmin, xmax, spacing_proj, max_count=max_ticks)
    ys = _compute_values(ymin, ymax, spacing_proj, max_count=max_ticks)

    fields_tick = [("SHEET", "TEXT", 64), ("TYPE", "TEXT", 12),
                   ("SIDE", "TEXT", 8), ("VAL", "DOUBLE", 0),
                   ("IS_MAJOR", "SHORT", 0)]
    fields_lbl  = [("SHEET", "TEXT", 64), ("TYPE", "TEXT", 12),
                   ("SIDE", "TEXT", 8), ("TEXT", "TEXT", 64),
                   ("ROT", "DOUBLE", 0)]
    fields_gl   = [("SHEET", "TEXT", 64), ("TYPE", "TEXT", 12),
                   ("VAL", "DOUBLE", 0), ("AXIS", "TEXT", 4)]

    fc_ticks = _ensure_fc(fds, "TKS_Ticks", "POLYLINE", sr, fields_tick)
    fc_lbls  = _ensure_fc(fds, "LBL_Labels", "POINT", sr, fields_lbl)
    fc_glines = None
    if create_grid_lines:
        fc_glines = _ensure_fc(fds, "GLN_GridLines", "POLYLINE", sr, fields_gl)

    if cleanup_sheet and (not dry_run):
        _delete_by_sheet(fc_ticks, sheet_key)
        _delete_by_sheet(fc_lbls, sheet_key)
        if fc_glines:
            _delete_by_sheet(fc_glines, sheet_key)

    if dry_run:
        _diag(f"DRY RUN sheet={sheet_key} ticksX={len(xs)} ticksY={len(ys)}")
        _log_line(log_file, f"[{_now_str()}] DRY RUN sheet={sheet_key} "
                            f"ticksX={len(xs)} ticksY={len(ys)}")
        return {"ticks_proj": len(xs) + len(ys),
                "labels_proj": len(xs) + len(ys),
                "grid_lines": 0}

    tick_cur = arcpy.da.InsertCursor(
        fc_ticks, ["SHAPE@", "SHEET", "TYPE", "SIDE", "VAL", "IS_MAJOR"])
    lbl_cur = arcpy.da.InsertCursor(
        fc_lbls, ["SHAPE@", "SHEET", "TYPE", "SIDE", "TEXT", "ROT"])
    gl_cur = None
    if fc_glines:
        gl_cur = arcpy.da.InsertCursor(
            fc_glines, ["SHAPE@", "SHEET", "TYPE", "VAL", "AXIS"])

    try:
        try:
            arcpy.SetProgressor("step", "Creating projected ticks/labels...",
                                  0, len(xs) + len(ys), 1)
        except Exception:
            pass

        # Projected ticks along X (bottom/top)
        for x in xs:
            try:
                pB  = _display_to_data_xy(x, ymin, edges)
                pT  = _display_to_data_xy(x, ymax, edges)
                pB2 = _display_to_data_xy(x, ymin - tick_len, edges)
                pT2 = _display_to_data_xy(x, ymax + tick_len, edges)
                geomB = arcpy.Polyline(arcpy.Array(
                    [arcpy.Point(pB[0], pB[1]), arcpy.Point(pB2[0], pB2[1])]), sr)
                _insert_tick(tick_cur, sheet_key, "PROJ", "BOTTOM", x, geomB, 1)
                geomT = arcpy.Polyline(arcpy.Array(
                    [arcpy.Point(pT[0], pT[1]), arcpy.Point(pT2[0], pT2[1])]), sr)
                _insert_tick(tick_cur, sheet_key, "PROJ", "TOP", x, geomT, 1)

                pLb = _display_to_data_xy(x, ymin - (tick_len + lab_off), edges)
                pLt = _display_to_data_xy(x, ymax + (tick_len + lab_off), edges)
                txt = _format_proj(x, divisor_proj, fmt_mode=proj_label_mode,
                                    unit_suffix=proj_unit_suffix, pad3=proj_pad3)
                _insert_label(lbl_cur, sheet_key, "PROJ", "BOTTOM", txt,
                              arcpy.PointGeometry(arcpy.Point(pLb[0], pLb[1]), sr), 0.0)
                _insert_label(lbl_cur, sheet_key, "PROJ", "TOP", txt,
                              arcpy.PointGeometry(arcpy.Point(pLt[0], pLt[1]), sr), 0.0)
                if gl_cur:
                    p1 = _display_to_data_xy(x, ymin, edges)
                    p2 = _display_to_data_xy(x, ymax, edges)
                    gl = arcpy.Polyline(arcpy.Array(
                        [arcpy.Point(p1[0], p1[1]),
                         arcpy.Point(p2[0], p2[1])]), sr)
                    gl_cur.insertRow((gl, sheet_key, "PROJ", float(x), "X"))
            except Exception:
                if continue_on_error:
                    _log_line(log_file,
                        f"[{_now_str()}] WARN sheet={sheet_key} "
                        f"projX tick failed: {traceback.format_exc()}")
                else:
                    raise
            try:
                arcpy.SetProgressorPosition()
            except Exception:
                pass

        # Projected ticks along Y (left/right)
        for y in ys:
            try:
                pL  = _display_to_data_xy(xmin, y, edges)
                pR  = _display_to_data_xy(xmax, y, edges)
                pL2 = _display_to_data_xy(xmin - tick_len, y, edges)
                pR2 = _display_to_data_xy(xmax + tick_len, y, edges)
                geomL = arcpy.Polyline(arcpy.Array(
                    [arcpy.Point(pL[0], pL[1]), arcpy.Point(pL2[0], pL2[1])]), sr)
                _insert_tick(tick_cur, sheet_key, "PROJ", "LEFT", y, geomL, 1)
                geomR = arcpy.Polyline(arcpy.Array(
                    [arcpy.Point(pR[0], pR[1]), arcpy.Point(pR2[0], pR2[1])]), sr)
                _insert_tick(tick_cur, sheet_key, "PROJ", "RIGHT", y, geomR, 1)

                pLl = _display_to_data_xy(xmin - (tick_len + lab_off), y, edges)
                pLr = _display_to_data_xy(xmax + (tick_len + lab_off), y, edges)
                txt = _format_proj(y, divisor_proj, fmt_mode=proj_label_mode,
                                    unit_suffix=proj_unit_suffix, pad3=proj_pad3)
                _insert_label(lbl_cur, sheet_key, "PROJ", "LEFT", txt,
                              arcpy.PointGeometry(arcpy.Point(pLl[0], pLl[1]), sr), 0.0)
                _insert_label(lbl_cur, sheet_key, "PROJ", "RIGHT", txt,
                              arcpy.PointGeometry(arcpy.Point(pLr[0], pLr[1]), sr), 0.0)
                if gl_cur:
                    p1 = _display_to_data_xy(xmin, y, edges)
                    p2 = _display_to_data_xy(xmax, y, edges)
                    gl = arcpy.Polyline(arcpy.Array(
                        [arcpy.Point(p1[0], p1[1]),
                         arcpy.Point(p2[0], p2[1])]), sr)
                    gl_cur.insertRow((gl, sheet_key, "PROJ", float(y), "Y"))
            except Exception:
                if continue_on_error:
                    _log_line(log_file,
                        f"[{_now_str()}] WARN sheet={sheet_key} "
                        f"projY tick failed: {traceback.format_exc()}")
                else:
                    raise
            try:
                arcpy.SetProgressorPosition()
            except Exception:
                pass
        try:
            arcpy.ResetProgressor()
        except Exception:
            pass

        res = {
            "ticks_proj": len(xs) + len(ys),
            "labels_proj": len(xs) + len(ys),
            "ticks_geo": 0, "labels_geo": 0,
            "grid_lines": (len(xs) + len(ys)) if gl_cur else 0,
        }

        # Graticule
        if enable_graticule and graticule_interval_deg and graticule_interval_deg > 0:
            geo_sr = arcpy.SpatialReference(int(geo_wkid))
            if graticule_mode == "TRUE_INTERVAL":
                w = abs(xmax - xmin)
                n_base = int(max(80, min(2000, (w / float(spacing_proj)) * 50)))

                def _densify(p0, p1, n):
                    pts = []
                    for k in range(n):
                        t = float(k) / float(n - 1)
                        pts.append((p0[0] + (p1[0] - p0[0]) * t,
                                    p0[1] + (p1[1] - p0[1]) * t))
                    return pts

                display_edges = {
                    "BOTTOM": ((xmin, ymin), (xmax, ymin)),
                    "TOP":    ((xmin, ymax), (xmax, ymax)),
                    "LEFT":   ((xmin, ymin), (xmin, ymax)),
                    "RIGHT":  ((xmax, ymin), (xmax, ymax)),
                }

                edge_geo = {}
                for side, (p0d, p1d) in display_edges.items():
                    pts_disp = _densify(p0d, p1d, n_base)
                    pts_data = [_display_to_data_xy(p[0], p[1], edges)
                                for p in pts_disp]
                    ll = _project_points(pts_data, sr, geo_sr,
                                         continue_on_error=True)
                    edge_geo[side] = {
                        "disp": pts_disp, "data": pts_data,
                        "lon": [p[0] for p in ll],
                        "lat": [p[1] for p in ll],
                    }

                lon_all, lat_all = [], []
                for side in edge_geo:
                    lon_all += [a for a in edge_geo[side]["lon"] if a is not None]
                    lat_all += [a for a in edge_geo[side]["lat"] if a is not None]
                if lon_all and lat_all:
                    lon_min, lon_max = min(lon_all), max(lon_all)
                    lat_min, lat_max = min(lat_all), max(lat_all)
                    lon_targets = _compute_values(
                        lon_min, lon_max, graticule_interval_deg,
                        max_count=max_ticks)
                    lat_targets = _compute_values(
                        lat_min, lat_max, graticule_interval_deg,
                        max_count=max_ticks)

                    def _find_cross(target, vals, coords_data, coords_disp):
                        for k in range(len(vals) - 1):
                            v0, v1 = vals[k], vals[k + 1]
                            if v0 is None or v1 is None:
                                continue
                            if (v0 <= target <= v1) or (v1 <= target <= v0):
                                if v1 == v0:
                                    return (coords_data[k], coords_disp[k])
                                t = (target - v0) / float(v1 - v0)
                                xd = (coords_data[k][0] +
                                      (coords_data[k + 1][0] - coords_data[k][0]) * t)
                                yd = (coords_data[k][1] +
                                      (coords_data[k + 1][1] - coords_data[k][1]) * t)
                                xdp = (coords_disp[k][0] +
                                       (coords_disp[k + 1][0] - coords_disp[k][0]) * t)
                                ydp = (coords_disp[k][1] +
                                       (coords_disp[k + 1][1] - coords_disp[k][1]) * t)
                                return ((xd, yd), (xdp, ydp))
                        return None

                    for lon in lon_targets:
                        for side in ["BOTTOM", "TOP"]:
                            rec = _find_cross(lon, edge_geo[side]["lon"],
                                              edge_geo[side]["data"],
                                              edge_geo[side]["disp"])
                            if not rec:
                                continue
                            (xd, yd), (xdisp, ydisp) = rec
                            if side == "BOTTOM":
                                p2 = _display_to_data_xy(xdisp, ydisp - tick_len, edges)
                                labp = _display_to_data_xy(
                                    xdisp, ydisp - (tick_len + grat_off), edges)
                            else:
                                p2 = _display_to_data_xy(xdisp, ydisp + tick_len, edges)
                                labp = _display_to_data_xy(
                                    xdisp, ydisp + (tick_len + grat_off), edges)
                            geom = arcpy.Polyline(arcpy.Array(
                                [arcpy.Point(xd, yd),
                                 arcpy.Point(p2[0], p2[1])]), sr)
                            _insert_tick(tick_cur, sheet_key, "GEO", side, lon, geom, 1)
                            txt = _format_dms(lon, is_lon=True,
                                               show_hemi=graticule_show_hemi)
                            _insert_label(lbl_cur, sheet_key, "GEO", side, txt,
                                          arcpy.PointGeometry(
                                              arcpy.Point(labp[0], labp[1]), sr), 0.0)
                            res["ticks_geo"] += 1
                            res["labels_geo"] += 1

                    for lat in lat_targets:
                        for side in ["LEFT", "RIGHT"]:
                            rec = _find_cross(lat, edge_geo[side]["lat"],
                                              edge_geo[side]["data"],
                                              edge_geo[side]["disp"])
                            if not rec:
                                continue
                            (xd, yd), (xdisp, ydisp) = rec
                            if side == "LEFT":
                                p2 = _display_to_data_xy(xdisp - tick_len, ydisp, edges)
                                labp = _display_to_data_xy(
                                    xdisp - (tick_len + grat_off), ydisp, edges)
                            else:
                                p2 = _display_to_data_xy(xdisp + tick_len, ydisp, edges)
                                labp = _display_to_data_xy(
                                    xdisp + (tick_len + grat_off), ydisp, edges)
                            geom = arcpy.Polyline(arcpy.Array(
                                [arcpy.Point(xd, yd),
                                 arcpy.Point(p2[0], p2[1])]), sr)
                            _insert_tick(tick_cur, sheet_key, "GEO", side, lat, geom, 1)
                            txt = _format_dms(lat, is_lon=False,
                                               show_hemi=graticule_show_hemi)
                            _insert_label(lbl_cur, sheet_key, "GEO", side, txt,
                                          arcpy.PointGeometry(
                                              arcpy.Point(labp[0], labp[1]), sr), 0.0)
                            res["ticks_geo"] += 1
                            res["labels_geo"] += 1
                else:
                    _add_warn("Graticule enabled but projection produced no valid samples.")
            else:
                geo_sr = arcpy.SpatialReference(int(geo_wkid))
                for x in xs:
                    for side in ["BOTTOM", "TOP"]:
                        y = ymin if side == "BOTTOM" else ymax
                        xd, yd = _display_to_data_xy(x, y, edges)
                        ll = _project_points([(xd, yd)], sr, geo_sr,
                                              continue_on_error=True)[0]
                        lon = ll[0]
                        if lon is None:
                            continue
                        if side == "BOTTOM":
                            labp = _display_to_data_xy(x, y - (tick_len + grat_off), edges)
                        else:
                            labp = _display_to_data_xy(x, y + (tick_len + grat_off), edges)
                        txt = _format_dms(lon, is_lon=True, show_hemi=graticule_show_hemi)
                        _insert_label(lbl_cur, sheet_key, "GEO", side, txt,
                                      arcpy.PointGeometry(
                                          arcpy.Point(labp[0], labp[1]), sr), 0.0)
                        res["labels_geo"] += 1
                for y in ys:
                    for side in ["LEFT", "RIGHT"]:
                        x = xmin if side == "LEFT" else xmax
                        xd, yd = _display_to_data_xy(x, y, edges)
                        ll = _project_points([(xd, yd)], sr, geo_sr,
                                              continue_on_error=True)[0]
                        lat = ll[1]
                        if lat is None:
                            continue
                        if side == "LEFT":
                            labp = _display_to_data_xy(x - (tick_len + grat_off), y, edges)
                        else:
                            labp = _display_to_data_xy(x + (tick_len + grat_off), y, edges)
                        txt = _format_dms(lat, is_lon=False, show_hemi=graticule_show_hemi)
                        _insert_label(lbl_cur, sheet_key, "GEO", side, txt,
                                      arcpy.PointGeometry(
                                          arcpy.Point(labp[0], labp[1]), sr), 0.0)
                        res["labels_geo"] += 1

        # Corner de-overlap
        moves = 0
        if deoverlap_corners and (min_sep > 0) and (corner_extra > 0):
            try:
                proj_pts = []
                with arcpy.da.SearchCursor(
                        fc_lbls, ["SHAPE@XY", "TYPE"],
                        f"SHEET = '{sheet_key.replace(chr(39), chr(39)+chr(39))}'") as sc:
                    for (xy, typ) in sc:
                        if typ == "PROJ":
                            proj_pts.append(xy)
                if proj_pts:
                    lyr = "lbl_geo_" + uuid.uuid4().hex[:6]
                    try:
                        arcpy.management.MakeFeatureLayer(
                            fc_lbls, lyr,
                            f"SHEET = '{sheet_key.replace(chr(39), chr(39)+chr(39))}' "
                            f"AND TYPE = 'GEO'")
                        with arcpy.da.UpdateCursor(
                                lyr, ["SHAPE@XY", "SIDE", "TEXT", "TYPE"]) as uc:
                            for row in uc:
                                (x0, y0) = row[0]
                                xdisp, ydisp = _data_to_display_xy(x0, y0, edges)
                                corner = None
                                if abs(xdisp - xmin) < min_sep and abs(ydisp - ymin) < min_sep:
                                    corner = "BL"
                                elif abs(xdisp - xmax) < min_sep and abs(ydisp - ymin) < min_sep:
                                    corner = "BR"
                                elif abs(xdisp - xmin) < min_sep and abs(ydisp - ymax) < min_sep:
                                    corner = "TL"
                                elif abs(xdisp - xmax) < min_sep and abs(ydisp - ymax) < min_sep:
                                    corner = "TR"
                                if not corner:
                                    continue
                                too_close = False
                                for (xp, yp) in proj_pts:
                                    if abs(xp - x0) < min_sep and abs(yp - y0) < min_sep:
                                        too_close = True
                                        break
                                if not too_close:
                                    continue
                                dx = (-corner_extra if "L" in corner else corner_extra)
                                dy = (-corner_extra if "B" in corner else corner_extra)
                                xdisp2 = xdisp + dx
                                ydisp2 = ydisp + dy
                                x2, y2 = _display_to_data_xy(xdisp2, ydisp2, edges)
                                row[0] = (x2, y2)
                                uc.updateRow(row)
                                moves += 1
                    finally:
                        try:
                            arcpy.management.Delete(lyr)
                        except Exception:
                            pass
                if moves:
                    _diag(f"sheet={sheet_key} corner-deoverlap moves={moves}")
            except Exception:
                if continue_on_error:
                    _add_warn(f"Corner de-overlap failed: {traceback.format_exc()}")
                else:
                    raise

        _diag(f"sheet={sheet_key} proj={len(xs)}+{len(ys)} "
              f"geo={res.get('ticks_geo', 0)}/{res.get('labels_geo', 0)} "
              f"glines={res.get('grid_lines', 0)}")
        _log_line(
            log_file,
            f"[{_now_str()}] OK SMART_FEATURE sheet={sheet_key} "
            f"proj={len(xs)}+{len(ys)} "
            f"geo={res.get('ticks_geo', 0)}/{res.get('labels_geo', 0)} "
            f"glines={res.get('grid_lines', 0)} corner_moves={moves}")
        res["corner_moves"] = moves
        return res

    finally:
        try:
            del tick_cur
        except Exception:
            pass
        try:
            del lbl_cur
        except Exception:
            pass
        try:
            if gl_cur:
                del gl_cur
        except Exception:
            pass


# =============================================================================
# 6. ESRI_XML engine wrapper
# =============================================================================

def _run_esri_make_grids(template_xml, aoi, fds, out_layer_name,
                          grid_name=None, refscale=None, rotation=None,
                          mask_mm=None, primary_sr=None,
                          configure_layout=False):
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
        except Exception:
            pass

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
    except arcpy.ExecuteError:
        _raise(f"MakeGridsAndGraticulesLayer failed: {arcpy.GetMessages(2)}")
    except Exception as e:
        _raise(f"MakeGridsAndGraticulesLayer failed: {e}")


# =============================================================================
# 7. Pro project helpers (arcpy.mp)
# =============================================================================

def _list_aprx(folder: str, recursive: bool = False) -> List[str]:
    out = []
    try:
        for root, dirs, files in os.walk(folder):
            for fn in files:
                if fn.lower().endswith(".aprx"):
                    out.append(os.path.join(root, fn))
            if not recursive:
                break
    except Exception:
        pass
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
    except Exception:
        mfs = []
    if not mfs:
        return (None, None)
    mf = mfs[0]
    return (mf, mf.map)

def _norm_path(p):
    try:
        return os.path.normcase(os.path.normpath(p))
    except Exception:
        return p

def _add_smart_outputs_to_map(m, fds_path: str,
                               symbology_layerfile: Optional[str] = None) -> None:
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
            except Exception:
                pass
    except Exception:
        pass

    for fc_name in ["TKS_Ticks", "LBL_Labels", "GLN_GridLines"]:
        fc = os.path.join(fds_path, fc_name)
        if not arcpy.Exists(fc):
            continue
        if _norm_path(fc) in existing:
            continue
        try:
            m.addDataFromPath(fc)
        except Exception:
            _add_warn(f"Could not add {fc} to map.")
    if symbology_layerfile and os.path.isfile(symbology_layerfile):
        try:
            for lyr in m.listLayers():
                try:
                    if lyr.supports("DATASOURCE") and lyr.dataSource and \
                       os.path.basename(lyr.dataSource) in (
                           "TKS_Ticks", "LBL_Labels", "GLN_GridLines"):
                        arcpy.management.ApplySymbologyFromLayer(
                            lyr.name, symbology_layerfile)
                except Exception:
                    pass
        except Exception:
            pass



# =============================================================================
# 8. Toolbox + Tool class
# =============================================================================

class Toolbox:
    def __init__(self):
        self.label = "Cartographic Automation (Pro, v6 native) - Plugin 07"
        self.alias = "plugin07_batch_grid_pro_v6"
        self.tools = [BatchGridBuilder07]


class BatchGridBuilder07:
    """Batch Grid / Graticule Builder - native Pro v6."""

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
    IDX_MAX_TICKS         = 30
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
        self.label = "07) Batch Grid Builder (Pro) - v6 native"
        self.description = (
            "Batch build grids/graticules for multiple sheets, with corner "
            "de-overlap and optional .aprx save / PDF / PNG / JPEG export.\n\n"
            "v6 native:\n"
            " - SELECTION-BYPASS hardwired on AOI layers\n"
            " - Per-project memory cleanup (del + gc.collect + ClearWorkspaceCache)\n"
            " - Narrow exceptions; tracebacks printed on failure\n"
            " - Modern arcpy.mp.ArcGISProject + .layouts iteration\n"
            " - Add SMART_FEATURE outputs to active map via addDataFromPath\n"
            " - Stage-by-stage [DIAG] logging.")
        self.canRunInBackground = False

    def isLicensed(self) -> bool:
        return True

    def getParameterInfo(self):
        p = []

        # Mode + sources
        p_mode = arcpy.Parameter("Mode", "mode", "GPString", "Required", "Input")
        p_mode.filter.type = "ValueList"
        p_mode.filter.list = ["FOLDER_OF_APRX", "AOI_LAYER_IN_CURRENT_PROJECT"]
        p_mode.value = "FOLDER_OF_APRX"
        p.append(p_mode)

        p_pfold = arcpy.Parameter("APRX Folder (Mode=FOLDER_OF_APRX)",
                                   "aprx_folder",
                                   "DEFolder", "Optional", "Input")
        p.append(p_pfold)
        p_rec = arcpy.Parameter("Include Subfolders", "recursive",
                                 "GPBoolean", "Optional", "Input")
        p_rec.value = False
        p.append(p_rec)
        p_lyt = arcpy.Parameter("Layout Name (optional; default=first layout)",
                                 "layout_name",
                                 "GPString", "Optional", "Input")
        p.append(p_lyt)

        p_aoi = arcpy.Parameter(
            "AOI Layer (Mode=AOI_LAYER_IN_CURRENT_PROJECT)",
            "aoi_layer",
            "GPFeatureLayer", "Optional", "Input")
        p.append(p_aoi)
        p_aoi_nf = arcpy.Parameter("AOI Name Field", "aoi_name_field",
                                    "Field", "Optional", "Input")
        p_aoi_nf.parameterDependencies = [p_aoi.name]
        p.append(p_aoi_nf)

        p_eng = arcpy.Parameter("Engine", "engine",
                                 "GPString", "Required", "Input")
        p_eng.filter.type = "ValueList"
        p_eng.filter.list = ["SMART_FEATURE", "ESRI_XML"]
        p_eng.value = "SMART_FEATURE"
        p.append(p_eng)

        p_xml = arcpy.Parameter("Grid Template XML (Engine=ESRI_XML)",
                                 "grid_xml", "DEFile", "Optional", "Input")
        p_xml.filter.list = ["xml"]
        p.append(p_xml)

        p_ows = arcpy.Parameter("Output Workspace (.gdb or folder)",
                                 "out_ws",
                                 "DEWorkspace", "Required", "Input")
        p.append(p_ows)
        p_fds = arcpy.Parameter("Feature Dataset Name (for SMART_FEATURE outputs)",
                                 "fds_name",
                                 "GPString", "Optional", "Input")
        p_fds.value = "Grids"
        p.append(p_fds)

        p_rs = arcpy.Parameter("Reference Scale Denominator (e.g., 25000)",
                                "refscale_denom",
                                "GPDouble", "Required", "Input")
        p_rs.value = 25000
        p.append(p_rs)
        p_rot = arcpy.Parameter("(SMART_FEATURE) Respect Map Frame Rotation",
                                 "respect_df_rotation",
                                 "GPBoolean", "Optional", "Input")
        p_rot.value = True
        p.append(p_rot)

        p_sp = arcpy.Parameter("(SMART_FEATURE) Projected Interval (map units)",
                                "spacing_proj", "GPDouble", "Optional", "Input")
        p_sp.value = 1000.0
        p.append(p_sp)
        p_dv = arcpy.Parameter(
            "(SMART_FEATURE) Projected Label Divisor "
            "(1000 => 295 instead of 295000)",
            "divisor_proj", "GPDouble", "Optional", "Input")
        p_dv.value = 1000.0
        p.append(p_dv)
        p_tk = arcpy.Parameter("(SMART_FEATURE) Tick Length (mm)",
                                "tick_mm", "GPDouble", "Optional", "Input")
        p_tk.value = 1.5
        p.append(p_tk)
        p_lb = arcpy.Parameter("(SMART_FEATURE) Label Offset (mm)",
                                "label_mm", "GPDouble", "Optional", "Input")
        p_lb.value = 3.0
        p.append(p_lb)
        p_gl = arcpy.Parameter("(SMART_FEATURE) Create Grid Lines (full)",
                                "create_grid_lines",
                                "GPBoolean", "Optional", "Input")
        p_gl.value = True
        p.append(p_gl)

        p_lm = arcpy.Parameter("(SMART_FEATURE) Projected Label Format",
                                "proj_label_mode",
                                "GPString", "Optional", "Input")
        p_lm.filter.type = "ValueList"
        p_lm.filter.list = ["INT", "FLOAT"]
        p_lm.value = "INT"
        p.append(p_lm)
        p_us = arcpy.Parameter("(SMART_FEATURE) Projected Label Unit Suffix",
                                "proj_unit_suffix",
                                "GPString", "Optional", "Input")
        p_us.value = ""
        p.append(p_us)
        p_pd = arcpy.Parameter("(SMART_FEATURE) Projected Label Pad to 3 digits",
                                "proj_pad3",
                                "GPBoolean", "Optional", "Input")
        p_pd.value = True
        p.append(p_pd)

        p_eg = arcpy.Parameter("(SMART_FEATURE) Enable Graticule (Lat/Lon)",
                                "enable_graticule",
                                "GPBoolean", "Optional", "Input")
        p_eg.value = True
        p.append(p_eg)
        p_gm = arcpy.Parameter("(SMART_FEATURE) Graticule Interval (minutes)",
                                "grat_minutes",
                                "GPDouble", "Optional", "Input")
        p_gm.value = 2.5
        p.append(p_gm)
        p_gmd = arcpy.Parameter("(SMART_FEATURE) Graticule Mode",
                                 "grat_mode",
                                 "GPString", "Optional", "Input")
        p_gmd.filter.type = "ValueList"
        p_gmd.filter.list = ["TRUE_INTERVAL", "SAMPLE_AT_PROJECTED_TICKS"]
        p_gmd.value = "TRUE_INTERVAL"
        p.append(p_gmd)
        p_gw = arcpy.Parameter("(SMART_FEATURE) Geographic WKID for Graticule",
                                "geo_wkid", "GPLong", "Optional", "Input")
        p_gw.value = 4326
        p.append(p_gw)
        p_glm = arcpy.Parameter("(SMART_FEATURE) Graticule Label Offset (mm)",
                                 "grat_label_mm",
                                 "GPDouble", "Optional", "Input")
        p_glm.value = 3.5
        p.append(p_glm)
        p_gh = arcpy.Parameter("(SMART_FEATURE) Show Hemisphere (N/S/E/W)",
                                "grat_hemi",
                                "GPBoolean", "Optional", "Input")
        p_gh.value = False
        p.append(p_gh)

        p_do = arcpy.Parameter("(SMART_FEATURE) Auto De-overlap at Corners",
                                "deoverlap_corners",
                                "GPBoolean", "Optional", "Input")
        p_do.value = True
        p.append(p_do)
        p_ms = arcpy.Parameter("(SMART_FEATURE) Minimum Separation (mm)",
                                "min_sep_mm",
                                "GPDouble", "Optional", "Input")
        p_ms.value = 1.5
        p.append(p_ms)
        p_ce = arcpy.Parameter("(SMART_FEATURE) Corner Extra Shift (mm)",
                                "corner_extra_mm",
                                "GPDouble", "Optional", "Input")
        p_ce.value = 1.0
        p.append(p_ce)

        p_co = arcpy.Parameter("Continue On Error (best-effort batch)",
                                "continue_on_error",
                                "GPBoolean", "Optional", "Input")
        p_co.value = True
        p.append(p_co)
        p_mt = arcpy.Parameter("Max ticks/gridlines per edge (safety)",
                                "max_ticks", "GPLong", "Optional", "Input")
        p_mt.value = 20000
        p.append(p_mt)
        p_cl = arcpy.Parameter("Clean old grids for sheet before creating new",
                                "cleanup_sheet",
                                "GPBoolean", "Optional", "Input")
        p_cl.value = True
        p.append(p_cl)
        p_dr = arcpy.Parameter("Dry Run (no outputs written)",
                                "dry_run",
                                "GPBoolean", "Optional", "Input")
        p_dr.value = False
        p.append(p_dr)

        p_sym = arcpy.Parameter("Apply Symbology From Layerfile (optional)",
                                 "symbology_layerfile",
                                 "DEFile", "Optional", "Input")
        p_sym.filter.list = ["lyr", "lyrx"]
        p.append(p_sym)

        p_msk = arcpy.Parameter("(ESRI_XML) Mask Size (mm)",
                                 "mask_mm", "GPDouble", "Optional", "Input")
        p_msk.value = 5.0
        p.append(p_msk)
        p_af = arcpy.Parameter("(ESRI_XML) XML AutoFix (best-effort)",
                                "xml_autofix",
                                "GPBoolean", "Optional", "Input")
        p_af.value = True
        p.append(p_af)
        p_da = arcpy.Parameter("(ESRI_XML) Ancillary Offset Delta",
                                "xml_delta_anc",
                                "GPDouble", "Optional", "Input")
        p_da.value = 2.0
        p.append(p_da)

        p_atm = arcpy.Parameter("Add outputs to current map",
                                 "add_to_map",
                                 "GPBoolean", "Optional", "Input")
        p_atm.value = True
        p.append(p_atm)
        p_smc = arcpy.Parameter("Save APRX copy (Mode=FOLDER_OF_APRX)",
                                 "save_aprx_copy",
                                 "GPBoolean", "Optional", "Input")
        p_smc.value = True
        p.append(p_smc)
        p_omf = arcpy.Parameter("Output APRX Folder",
                                 "out_aprx_folder",
                                 "DEFolder", "Optional", "Input")
        p.append(p_omf)
        p_epd = arcpy.Parameter("Export PDF",
                                 "export_pdf",
                                 "GPBoolean", "Optional", "Input")
        p_epd.value = False
        p.append(p_epd)
        p_opd = arcpy.Parameter("Output PDF Folder",
                                 "out_pdf_folder",
                                 "DEFolder", "Optional", "Input")
        p.append(p_opd)
        p_pdpi = arcpy.Parameter("PDF Resolution (DPI)",
                                  "pdf_dpi", "GPLong", "Optional", "Input")
        p_pdpi.value = 300
        p.append(p_pdpi)
        p_epng = arcpy.Parameter("Export PNG",
                                  "export_png", "GPBoolean", "Optional", "Input")
        p_epng.value = False
        p.append(p_epng)
        p_ejpg = arcpy.Parameter("Export JPEG",
                                  "export_jpeg", "GPBoolean", "Optional", "Input")
        p_ejpg.value = False
        p.append(p_ejpg)
        p_oimg = arcpy.Parameter("Output Image Folder (PNG/JPEG)",
                                  "out_img_folder",
                                  "DEFolder", "Optional", "Input")
        p.append(p_oimg)
        p_idpi = arcpy.Parameter("Image Resolution (DPI)",
                                  "img_dpi", "GPLong", "Optional", "Input")
        p_idpi.value = 300
        p.append(p_idpi)
        p_log = arcpy.Parameter("Log file (optional)",
                                 "log_file",
                                 "DEFile", "Optional", "Input")
        p.append(p_log)

        return p

    def updateParameters(self, parameters):
        try:
            mode = parameters[self.IDX_MODE].valueAsText
            engine = parameters[self.IDX_ENGINE].valueAsText

            parameters[self.IDX_APRX_FOLDER].enabled = (mode == "FOLDER_OF_APRX")
            parameters[self.IDX_RECURSIVE].enabled = (mode == "FOLDER_OF_APRX")
            parameters[self.IDX_AOI_LAYER].enabled = (
                mode == "AOI_LAYER_IN_CURRENT_PROJECT")
            parameters[self.IDX_AOI_NAME_FIELD].enabled = (
                mode == "AOI_LAYER_IN_CURRENT_PROJECT")

            is_xml = (engine == "ESRI_XML")
            parameters[self.IDX_GRID_XML].enabled = is_xml
            for idx in range(self.IDX_RESPECT_ROT, self.IDX_CORNER_EXTRA_MM + 1):
                parameters[idx].enabled = (not is_xml)
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
        except Exception:
            pass

    def updateMessages(self, parameters):
        try:
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
            if (sp is not None) and (engine == "SMART_FEATURE") and float(sp) <= 0:
                parameters[self.IDX_SPACING_PROJ].setErrorMessage(
                    "Projected interval must be > 0.")
        except Exception:
            pass

    # ---------- execute ----------
    def execute(self, parameters, messages):
        arcpy.env.overwriteOutput = True
        try:
            arcpy.env.parallelProcessingFactor = "100%"
        except Exception:
            pass

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
        spacing_proj     = _safe_float(parameters[self.IDX_SPACING_PROJ].value, 0.0)
        divisor_proj     = _safe_float(parameters[self.IDX_DIVISOR_PROJ].value, 1.0)
        tick_mm          = _safe_float(parameters[self.IDX_TICK_MM].value, 1.5)
        label_mm         = _safe_float(parameters[self.IDX_LABEL_MM].value, 3.0)
        create_glines    = bool(parameters[self.IDX_CREATE_GLINES].value)
        proj_label_mode  = parameters[self.IDX_PROJ_LBL_MODE].valueAsText or "INT"
        proj_unit_suffix = parameters[self.IDX_PROJ_UNIT_SUFFIX].valueAsText or ""
        proj_pad3        = bool(parameters[self.IDX_PROJ_PAD3].value)

        enable_grat      = bool(parameters[self.IDX_ENABLE_GRAT].value)
        grat_minutes     = _safe_float(parameters[self.IDX_GRAT_MINUTES].value, 0.0)
        grat_mode        = parameters[self.IDX_GRAT_MODE].valueAsText or "TRUE_INTERVAL"
        geo_wkid         = _safe_int(parameters[self.IDX_GEO_WKID].value, 4326)
        grat_label_mm    = _safe_float(parameters[self.IDX_GRAT_LBL_MM].value, 3.5)
        grat_hemi        = bool(parameters[self.IDX_GRAT_HEMI].value)

        deover           = bool(parameters[self.IDX_DEOVERLAP].value)
        min_sep_mm       = _safe_float(parameters[self.IDX_MIN_SEP_MM].value, 1.5)
        corner_extra_mm  = _safe_float(parameters[self.IDX_CORNER_EXTRA_MM].value, 1.0)

        continue_on_err  = bool(parameters[self.IDX_CONTINUE_ON_ERR].value)
        max_ticks        = _safe_int(parameters[self.IDX_MAX_TICKS].value, 20000)
        cleanup_sheet    = bool(parameters[self.IDX_CLEANUP_SHEET].value)
        dry_run          = bool(parameters[self.IDX_DRY_RUN].value)
        symbology_lyr    = parameters[self.IDX_SYMBOLOGY_LYR].valueAsText

        mask_mm          = _safe_float(parameters[self.IDX_MASK_MM].value, 5.0)
        xml_autofix      = bool(parameters[self.IDX_XML_AUTOFIX].value)
        xml_delta_anc    = _safe_float(parameters[self.IDX_XML_DELTA_ANC].value, 2.0)

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

        # Selection-bypass on AOI
        aoi_layer = None
        aoi_layer_path = None
        if mode == "AOI_LAYER_IN_CURRENT_PROJECT" and aoi_layer_param:
            _announce_selection("AOI", aoi_layer_param)
            aoi_layer = aoi_layer_param
            aoi_layer_path = _resolve_full_source(aoi_layer_param)

        gdb = _ensure_file_gdb(out_ws, "grid_output.gdb")
        if not log_file_param:
            log_file = os.path.join(
                os.path.dirname(gdb),
                f"grid_batch_{time.strftime('%Y%m%d_%H%M%S')}.log")
        else:
            log_file = log_file_param

        _log_line(log_file,
            f"==== BatchGridBuilder07 v6 Pro start {_now_str()} "
            f"engine={engine} mode={mode} ====")
        _diag(f"Engine={engine} Mode={mode} Output GDB={gdb}")

        if refscale <= 0:
            _raise("Reference scale denominator must be > 0.")

        # Mode logic
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

        # ESRI_XML: validate template
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
        for idx_proj, aprx_path in enumerate(aprx_paths):
            aprx = None
            try:
                if mode == "FOLDER_OF_APRX":
                    _diag(f"Open APRX ({idx_proj + 1}/{len(aprx_paths)}): {aprx_path}")
                    aprx = _open_project(aprx_path)
                else:
                    aprx = _open_project(None)

                layout = _find_layout_or_first(aprx, layout_name)
                mf, m = _layout_first_mapframe(layout)
                if mf is None:
                    _raise("No MAPFRAME element found in the layout.")
                sr = mf.map.spatialReference if mf.map else None
                if sr is None:
                    _raise("Could not determine spatial reference of the map frame.")

                if engine == "ESRI_XML" and getattr(sr, "type", None) == "Geographic":
                    _raise("ESRI_XML engine requires a projected CRS for the map frame.")

                fds = _ensure_feature_dataset(gdb, fds_name, sr)

                # Determine sheets list: (sheet_name, extent, oid)
                sheets = []
                oid_field = None
                if mode == "FOLDER_OF_APRX":
                    sheet_name = os.path.splitext(os.path.basename(aprx_path))[0]
                    try:
                        # camera extent on the map frame
                        cam = mf.camera
                        ext = cam.getExtent()
                    except Exception:
                        ext = None
                    if ext is None:
                        # Fall back to map default extent
                        try:
                            ext = mf.map.defaultCamera.getExtent()
                        except Exception:
                            ext = None
                    if ext is None:
                        _raise(f"Could not determine extent of map frame for {aprx_path}")
                    sheets.append((sheet_name, ext, None))
                else:
                    src = aoi_layer_path or aoi_layer
                    try:
                        oid_field = arcpy.Describe(src).OIDFieldName
                    except Exception:
                        oid_field = "OBJECTID"
                    name_field = aoi_name_field
                    fields = ["OID@", "SHAPE@"]
                    if name_field:
                        fields.insert(1, name_field)
                    with arcpy.da.SearchCursor(src, fields) as sc:
                        for row in sc:
                            oid = row[0]
                            geom = row[-1]
                            ext = geom.extent if geom else None
                            if not ext:
                                continue
                            if name_field:
                                nm = str(row[1]) if row[1] is not None else str(oid)
                            else:
                                nm = str(oid)
                            sheets.append((nm, ext, oid))
                    if not sheets:
                        _raise("AOI layer has no valid features/extents.")
                    _diag(f"AOI sheets: {len(sheets)}")

                try:
                    arcpy.SetProgressor("step", "Processing sheets...",
                                          0, len(sheets), 1)
                except Exception:
                    pass

                df_rotation = 0.0
                try:
                    df_rotation = float(getattr(mf, "rotation", 0.0) or 0.0)
                except Exception:
                    df_rotation = 0.0

                for (sheet_name, ext, oid) in sheets:
                    sheet_key = _validate_name(sheet_name, gdb)
                    _log_line(log_file, f"[{_now_str()}] START sheet={sheet_key}")
                    try:
                        if engine == "SMART_FEATURE":
                            grat_deg = (grat_minutes / 60.0
                                         if grat_minutes and grat_minutes > 0 else 0.0)
                            _build_ticks_and_labels_for_extent(
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
                                max_ticks=max_ticks,
                                continue_on_error=continue_on_err,
                                log_file=log_file,
                                dry_run=dry_run,
                                cleanup_sheet=cleanup_sheet)
                        else:
                            extent_str = f"{ext.XMin} {ext.YMin} {ext.XMax} {ext.YMax}"
                            out_layer_name = f"GRID_{sheet_key}_{oid if oid is not None else 'EXT'}"
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
                                except Exception:
                                    aoi_arg = extent_str
                                    grid_name_arg = sheet_key
                            _run_esri_make_grids(
                                template_xml=grid_xml,
                                aoi=aoi_arg,
                                fds=fds,
                                out_layer_name=out_layer_name,
                                grid_name=grid_name_arg,
                                refscale=refscale,
                                rotation=(df_rotation if respect_rot else 0.0),
                                mask_mm=mask_mm,
                                primary_sr=sr,
                                configure_layout=False)
                            if (mode != "FOLDER_OF_APRX" and aoi_layer):
                                try:
                                    arcpy.management.SelectLayerByAttribute(
                                        aoi_layer, "CLEAR_SELECTION")
                                except Exception:
                                    pass
                            _add_msg(f"Created ESRI grid layer: {out_layer_name}")
                        _log_line(log_file, f"[{_now_str()}] DONE sheet={sheet_key}")
                    except Exception:
                        _log_line(log_file,
                            f"[{_now_str()}] ERROR sheet={sheet_key} "
                            f"err={traceback.format_exc()}")
                        if not continue_on_err:
                            raise
                        _add_warn(f"Sheet {sheet_key} failed: {traceback.format_exc()}")
                    try:
                        arcpy.SetProgressorPosition()
                    except Exception:
                        pass

                # Add SMART_FEATURE outputs to active map (per project)
                if (engine == "SMART_FEATURE") and add_to_map and (not dry_run):
                    try:
                        _add_smart_outputs_to_map(m, fds, symbology_layerfile=symbology_lyr)
                    except Exception:
                        _add_warn(
                            f"Failed to add output layers to map: "
                            f"{traceback.format_exc()}")

                try:
                    arcpy.ResetProgressor()
                except Exception:
                    pass

                # Save / Export per project
                if mode == "FOLDER_OF_APRX" and (not dry_run):
                    base = os.path.splitext(os.path.basename(aprx_path))[0]
                    if save_aprx_copy:
                        if not out_aprx_folder:
                            _raise("Output APRX Folder is required when "
                                   "Save APRX copy is True.")
                        _mkdir(out_aprx_folder)
                        out_aprx = os.path.join(out_aprx_folder, base + "_grid.aprx")
                        try:
                            aprx.saveACopy(out_aprx)
                            _add_msg(f"Saved APRX copy: {out_aprx}")
                        except Exception:
                            _add_warn(f"saveACopy failed: {traceback.format_exc()}")
                    if export_pdf:
                        if not out_pdf_folder:
                            _raise("Output PDF Folder is required when "
                                   "Export PDF is True.")
                        _mkdir(out_pdf_folder)
                        out_pdf = os.path.join(out_pdf_folder, base + ".pdf")
                        try:
                            if layout is not None:
                                layout.exportToPDF(out_pdf, resolution=pdf_dpi)
                                _add_msg(f"Exported PDF: {out_pdf}")
                            else:
                                _add_warn("No layout available; skipping PDF export.")
                        except Exception:
                            _add_warn(f"exportToPDF failed: {traceback.format_exc()}")
                    if export_png or export_jpeg:
                        if not out_img_folder:
                            _raise("Output Image Folder is required for "
                                   "PNG/JPEG export.")
                        _mkdir(out_img_folder)
                        if export_png:
                            out_png = os.path.join(out_img_folder, base + ".png")
                            try:
                                if layout is not None:
                                    layout.exportToPNG(out_png, resolution=img_dpi)
                                    _add_msg(f"Exported PNG: {out_png}")
                                else:
                                    _add_warn("No layout available; skipping PNG export.")
                            except Exception:
                                _add_warn(f"exportToPNG failed: {traceback.format_exc()}")
                        if export_jpeg:
                            out_jpg = os.path.join(out_img_folder, base + ".jpg")
                            try:
                                if layout is not None:
                                    layout.exportToJPEG(out_jpg, resolution=img_dpi)
                                    _add_msg(f"Exported JPEG: {out_jpg}")
                                else:
                                    _add_warn("No layout available; skipping JPEG export.")
                            except Exception:
                                _add_warn(f"exportToJPEG failed: {traceback.format_exc()}")
            except Exception:
                _log_line(log_file,
                    f"[{_now_str()}] ERROR aprx={aprx_path or 'CURRENT'} "
                    f"err={traceback.format_exc()}")
                if not continue_on_err:
                    raise
                _add_warn(f"Failed on {aprx_path or 'CURRENT'}: "
                          f"{traceback.format_exc()}")
            finally:
                try:
                    if aprx is not None:
                        del aprx
                except Exception:
                    pass
                try:
                    arcpy.management.ClearWorkspaceCache()
                except Exception:
                    pass
                gc.collect()

        _log_line(log_file,
            f"==== BatchGridBuilder07 v6 Pro finished {_now_str()} ====")
        _add_msg(f"Done. Log: {log_file}")
