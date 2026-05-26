# -*- coding: utf-8 -*-
"""
Plugin 07 - Batch Grid / Graticule Builder (ArcMap / Python 2.7)  v6 HARDENED
==============================================================================
Build grid + graticule layers in batch over MXDs or AOI features. Two engines:

  ESRI_XML
    Wraps arcpy.cartography.MakeGridsAndGraticulesLayer with an XML
    template. Best for production cartography styles. Loops over the
    AOI features so the official tool's "first feature only" limitation
    doesn't apply. Optional XML AutoFix nudges ancillary corner labels
    out by a few mm to reduce UTM-vs-Lat/Lon corner collisions.

  SMART_FEATURE
    Pure-arcpy engine writing Tick/Label/GridLine FCs into a feature
    dataset. Better when the user wants Maplex/Label-engine control or
    repeatable batch geometry. Generates projected (UTM-style) ticks
    and an optional Lat/Lon graticule by sampling the projected edges.
    Has a corner de-overlap pass to push graticule corner labels off
    the projected corner labels.

Hardened in v6 (vs v5 ULTIMATE):
  * SELECTION-BYPASS HARDWIRED. AOI / current-MXD layers are resolved
    to their on-disk catalogPath. Active selections are warned about
    and bypassed; the FULL dataset is processed. (Internally we still
    use SELECT->Run->CLEAR around the AOI layer for the ESRI XML
    engine, since the official tool requires the selection.)
  * NARROW EXCEPTIONS. Every "except:" is now "except Exception:".
    v3 had ~40 bare excepts. Tracebacks are printed on failures.
  * MEMORY DISCIPLINE for batch MXD loops. Each MXD is closed (del +
    gc.collect + ClearWorkspaceCache) per iteration so the 32-bit
    address space cannot leak across sheets.
  * Bug fixes:
      - updateParameters / execute were out of the class scope in v5
        (an indentation bug in the original file). Both are now
        proper methods.
      - Parameter index mismatch in v5 (46 params declared, 48 read)
        is corrected by adding two missing params and remapping all
        indices via named constants.
      - Hour-of-day MXD lock issues mitigated by cycling the data
        frame's mxd handle between sheets.
  * STAGE-BY-STAGE [DIAG] LOGGING: per MXD: total sheets ->
    selection state -> sheets processed; per sheet: ticks proj X /
    proj Y, ticks geo, labels geo, grid lines, corner-deoverlap moves.
  * Py2.7 hygiene: from __future__ import division, unicode-safe
    helpers, parallelProcessingFactor=100% for GP tools.

Author: Ali Mirjafari + Kiro
Version: 6.0 (ArcMap / Python 2.7)
"""

from __future__ import division

import arcpy
import os
import re
import math
import time
import uuid
import gc
import traceback

# Py2/Py3 unicode shim (file is Py2.7-targeted; the shim makes static
# tools that run under Py3 not crash on import-time unicode references).
try:
    unicode
except NameError:
    unicode = str

try:
    import xml.etree.ElementTree as ET
except Exception:
    ET = None

try:
    from lxml import etree as LET
except Exception:
    LET = None

try:
    import msvcrt
except Exception:
    msvcrt = None


# =============================================================================
# 0. Messaging / utility helpers
# =============================================================================

def _now_str():
    return time.strftime("%Y-%m-%d %H:%M:%S")

def _add_msg(msg):
    try:
        arcpy.AddMessage(msg)
    except Exception:
        pass

def _add_warn(msg):
    try:
        arcpy.AddWarning(msg)
    except Exception:
        pass

def _add_err(msg):
    try:
        arcpy.AddError(msg)
    except Exception:
        pass

def _diag(msg):
    _add_msg(u"[DIAG] " + (msg if isinstance(msg, unicode) else unicode(str(msg), "utf-8", "ignore")))

def _raise(msg):
    _add_err(msg)
    raise arcpy.ExecuteError(msg)

def _safe_float(v, default=0.0):
    try:
        if v is None:
            return default
        return float(v)
    except Exception:
        return default

def _safe_int(v, default=0):
    try:
        if v is None:
            return default
        return int(v)
    except Exception:
        return default

def _mkdir(folder):
    try:
        if folder and (not os.path.isdir(folder)):
            os.makedirs(folder)
    except Exception:
        pass

def _is_gdb(path):
    return bool(path and path.lower().endswith(".gdb") and os.path.isdir(path))

def _ensure_file_gdb(workspace_or_folder, gdb_name="grid_output.gdb"):
    if not workspace_or_folder:
        _raise(u"Output workspace is empty.")
    ws = workspace_or_folder
    if _is_gdb(ws):
        return ws
    if not os.path.isdir(ws):
        _raise(u"Output folder not found: {0}".format(ws))
    gdb = os.path.join(ws, gdb_name)
    if not os.path.isdir(gdb):
        _add_msg(u"Creating GDB: {0}".format(gdb))
        arcpy.management.CreateFileGDB(ws, os.path.basename(gdb))
    return gdb

def _ensure_feature_dataset(gdb, fds_name, spatial_ref):
    if not fds_name:
        fds_name = "Grids"
    fds_name = _validate_name(fds_name, gdb)
    fds = os.path.join(gdb, fds_name)
    if not arcpy.Exists(fds):
        arcpy.management.CreateFeatureDataset(gdb, fds_name, spatial_ref)
    return fds

def _validate_name(name, workspace):
    if name is None:
        name = "GRID"
    if isinstance(name, unicode):
        raw = name
    else:
        try:
            raw = unicode(name)
        except Exception:
            raw = unicode(str(name), "utf-8", "ignore")
    raw = raw.strip()
    if not raw:
        raw = u"GRID"
    raw = re.sub(u"[^0-9A-Za-z_]+", u"_", raw)
    raw = raw.strip(u"_")
    if not raw:
        raw = u"GRID"
    try:
        v = arcpy.ValidateTableName(raw, workspace)
        if v:
            raw = v
    except Exception:
        pass
    if re.match(u"^[0-9]", raw):
        raw = u"X_" + raw
    if len(raw) > 64:
        raw = raw[:64]
    return raw

def _product_level():
    try:
        return arcpy.ProductInfo()
    except Exception:
        return None

def _require_cartography_level():
    """MakeGridsAndGraticulesLayer requires Standard or Advanced."""
    lvl = _product_level()
    if not lvl:
        return
    if (lvl or "").lower() in ("basic", "arcview"):
        _raise(u"Cartography tool requires Standard/Advanced license. "
               u"Current product level: {0}".format(lvl))


# =============================================================================
# 1. Selection-bypass: resolve any layer to its on-disk source
# =============================================================================

def _selection_info(layer_or_path):
    try:
        d = arcpy.Describe(layer_or_path)
    except Exception:
        return (None, None, layer_or_path)
    name = getattr(d, "name", layer_or_path)
    fidset = getattr(d, "FIDSet", "") or ""
    total = None
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

def _announce_selection(label, layer_or_path):
    sel, total, name = _selection_info(layer_or_path)
    if sel and sel > 0:
        _add_warn(
            u"{lbl}: '{n}' has an active selection ({s} of {t}). "
            u"Ignoring selection - processing FULL dataset.".format(
                lbl=label, n=name, s=sel,
                t=(total if total is not None else u"?")))
    else:
        _diag(u"{lbl}: '{n}' total={t}, no active selection.".format(
            lbl=label, n=name,
            t=(total if total is not None else u"?")))


# =============================================================================
# 2. File logging (best-effort lock)
# =============================================================================

def _lock_write_line(fp, line_unicode):
    if not fp:
        return
    try:
        _mkdir(os.path.dirname(fp))
    except Exception:
        pass
    try:
        f = open(fp, "ab")
        try:
            if msvcrt is not None:
                try:
                    msvcrt.locking(f.fileno(), msvcrt.LK_LOCK, 1)
                except Exception:
                    pass
            data = (line_unicode + u"\n")
            try:
                b = data.encode("utf-8")
            except Exception:
                b = str(data)
            f.write(b)
        finally:
            try:
                if msvcrt is not None:
                    try:
                        msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
                    except Exception:
                        pass
            finally:
                f.close()
    except Exception:
        pass


# =============================================================================
# 3. Geometry + unit helpers
# =============================================================================

def _mm_to_map_units(mm, scale_denom, spatial_ref,
                     allow_geo_approx=False, approx_lat_deg=None):
    """mm on paper -> map units. Geographic CRS only via approx (warned)."""
    mm = _safe_float(mm, 0.0)
    scale_denom = _safe_float(scale_denom, 0.0)
    if mm <= 0 or scale_denom <= 0:
        return 0.0
    if spatial_ref and getattr(spatial_ref, "type", None) == "Geographic":
        if not allow_geo_approx:
            _raise(u"Spatial Reference is Geographic (degrees). mm->map units "
                   u"is not reliable. Project the data frame to a projected CRS "
                   u"(e.g., UTM).")
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

def _extent_edges_display(ext, respect_rotation=True, rotation_deg=0.0):
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

def _compute_values(minv, maxv, interval, max_count=20000):
    if interval <= 0:
        return []
    a = _ceil_to_interval(minv, interval)
    b = _floor_to_interval(maxv, interval)
    if b < a:
        return []
    n = int(round((b - a) / float(interval))) + 1
    if n > max_count:
        _raise(u"Too many ticks/grid lines ({0}). Increase interval or set "
               u"a larger max_ticks.".format(n))
    return [a + i * interval for i in range(n)]

def _format_proj(val, divisor, fmt_mode=u"INT", unit_suffix=u"", pad3=False):
    if divisor and divisor != 0:
        v = val / float(divisor)
    else:
        v = val
    if fmt_mode == u"FLOAT":
        s = u"{0:.3f}".format(v)
    else:
        try:
            iv = int(round(v))
        except Exception:
            iv = int(v)
        if pad3:
            s = u"{0:03d}".format(iv)
        else:
            s = unicode(iv)
    if unit_suffix:
        s = s + unit_suffix
    return s

def _format_dms(deg, is_lon=True, show_hemi=False, decimals=0):
    if deg is None:
        return u""
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
        ss_str = (u"{0:0" + unicode(2 + 1 + decimals) +
                  u"." + unicode(decimals) + u"f}").format(ss)
    else:
        ss_str = u"{0:02d}".format(int(ss))
    s = u"{0:d}\u00b0{1:02d}\'{2}\"".format(dd, mm, ss_str)
    if show_hemi:
        if is_lon:
            hemi = u"E" if sign >= 0 else u"W"
        else:
            hemi = u"N" if sign >= 0 else u"S"
        s = s + hemi
    else:
        if sign < 0:
            s = u"-" + s
    return s

def _project_points(points_xy, in_sr, out_sr, continue_on_error=False):
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
                _raise(u"Projection failed: {0}".format(e))
    return out

def _delete_by_sheet(fc, sheet_value):
    if (not fc) or (not arcpy.Exists(fc)):
        return
    lyr = "lyr_" + uuid.uuid4().hex[:8]
    try:
        arcpy.management.MakeFeatureLayer(fc, lyr)
        wc = u"SHEET = '{0}'".format(sheet_value.replace("'", "''"))
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

_OFFSET_KEYS = [u"offset", u"labeloffset", u"xoffset", u"yoffset", u"anchor"]

def _xml_autofix_temp(xml_path, temp_folder,
                      delta_primary=0.0,
                      delta_ancillary=2.0,
                      only_ancillary=True,
                      verbose=False):
    if (not xml_path) or (not os.path.isfile(xml_path)):
        return xml_path
    _mkdir(temp_folder)
    out_xml = os.path.join(
        temp_folder,
        "patched_{0}_{1}".format(uuid.uuid4().hex[:6], os.path.basename(xml_path)))

    def _is_offset_key(s):
        s = (s or u"").lower()
        for k in _OFFSET_KEYS:
            if k in s:
                return True
        return False

    def _is_ancillary_node(tag, attrib):
        t = (tag or u"").lower()
        if u"ancillary" in t:
            return True
        for k in attrib.keys():
            if u"ancillary" in (k or u"").lower():
                return True
        for v in attrib.values():
            if u"ancillary" in (unicode(v) if v is not None else u"").lower():
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
                _diag(u"XML AutoFix: modified {0} offset values (lxml)".format(touched))
            LET.ElementTree(root).write(
                out_xml, encoding="utf-8",
                xml_declaration=True, pretty_print=True)
            return out_xml
        except Exception:
            _add_warn(u"lxml XML AutoFix failed; falling back to ET. {0}".format(
                traceback.format_exc()))

    if ET is None:
        _add_warn(u"xml.etree not available; skipping XML AutoFix.")
        return xml_path

    try:
        tree = ET.parse(xml_path)
        root = tree.getroot()
        touched = 0
        for elem in root.iter():
            tag = elem.tag
            attrib = elem.attrib
            is_anc = _is_ancillary_node(tag, attrib)
            for ak in list(attrib.keys()):
                if _is_offset_key(ak):
                    v = _try_float(attrib.get(ak))
                    if v is None:
                        continue
                    if is_anc:
                        attrib[ak] = str(v + float(delta_ancillary)); touched += 1
                    elif (not only_ancillary) and delta_primary:
                        attrib[ak] = str(v + float(delta_primary)); touched += 1
            if elem.text and _is_offset_key(tag):
                v = _try_float(elem.text)
                if v is not None:
                    if is_anc:
                        elem.text = str(v + float(delta_ancillary)); touched += 1
                    elif (not only_ancillary) and delta_primary:
                        elem.text = str(v + float(delta_primary)); touched += 1
        if verbose:
            _diag(u"XML AutoFix: modified {0} offset values".format(touched))
        tree.write(out_xml, encoding="utf-8")
        return out_xml
    except Exception:
        _add_warn(u"XML AutoFix failed; using original template. {0}".format(
            traceback.format_exc()))
        return xml_path



# =============================================================================
# 5. SMART_FEATURE engine: ticks / labels / gridlines feature classes
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
        df, ext, sheet_key, fds, sr, scale_denom,
        spacing_proj, divisor_proj,
        tick_len_mm, label_offset_mm,
        create_grid_lines=False,
        enable_graticule=False,
        graticule_interval_deg=0.0,
        graticule_mode=u"TRUE_INTERVAL",
        geo_wkid=4326,
        graticule_label_offset_mm=3.0,
        graticule_show_hemi=False,
        proj_label_mode=u"INT",
        proj_unit_suffix=u"",
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
    """Generate tick/label/gridlines into FCs with a SHEET field."""
    spacing_proj = _safe_float(spacing_proj, 0.0)
    if spacing_proj <= 0:
        _raise(u"spacing_proj must be > 0")
    divisor_proj = _safe_float(divisor_proj, 1.0)
    if divisor_proj == 0:
        _raise(u"divisor_proj must not be 0")
    scale_denom = _safe_float(scale_denom, 0.0)
    if scale_denom <= 0:
        _raise(u"Reference scale denominator must be > 0 (e.g., 25000).")
    if getattr(sr, "type", None) == "Geographic":
        _raise(u"SMART_FEATURE engine requires Projected CRS for the data frame.")

    rot = 0.0
    try:
        rot = float(df.rotation)
    except Exception:
        rot = 0.0
    edges = _extent_edges_display(ext, respect_rotation=respect_df_rotation,
                                   rotation_deg=rot)

    # mm offsets to map units (latitude approx for geo CRS; not used here)
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
        _diag(u"DRY RUN sheet={0} ticksX={1} ticksY={2}".format(
            sheet_key, len(xs), len(ys)))
        _lock_write_line(log_file, u"[{0}] DRY RUN sheet={1} ticksX={2} ticksY={3}".format(
            _now_str(), sheet_key, len(xs), len(ys)))
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
                    _lock_write_line(log_file,
                        u"[{0}] WARN sheet={1} projX tick failed: {2}".format(
                            _now_str(), sheet_key, traceback.format_exc()))
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
                    _lock_write_line(log_file,
                        u"[{0}] WARN sheet={1} projY tick failed: {2}".format(
                            _now_str(), sheet_key, traceback.format_exc()))
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

        # Graticule (Lat/Lon)
        res = {
            "ticks_proj": len(xs) + len(ys),
            "labels_proj": len(xs) + len(ys),
            "ticks_geo": 0, "labels_geo": 0,
            "grid_lines": (len(xs) + len(ys)) if gl_cur else 0,
        }
        if enable_graticule and graticule_interval_deg and graticule_interval_deg > 0:
            geo_sr = arcpy.SpatialReference(int(geo_wkid))
            if graticule_mode == u"TRUE_INTERVAL":
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
                    _add_warn(u"Graticule enabled but projection produced no valid samples.")
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

        # Corner de-overlap (GEO labels vs PROJ labels)
        moves = 0
        if deoverlap_corners and (min_sep > 0) and (corner_extra > 0):
            try:
                proj_pts = []
                with arcpy.da.SearchCursor(
                        fc_lbls, ["SHAPE@XY", "TYPE"],
                        u"SHEET = '{0}'".format(sheet_key.replace("'", "''"))) as sc:
                    for (xy, typ) in sc:
                        if typ == "PROJ":
                            proj_pts.append(xy)
                if proj_pts:
                    lyr = "lbl_geo_" + uuid.uuid4().hex[:6]
                    try:
                        arcpy.management.MakeFeatureLayer(
                            fc_lbls, lyr,
                            u"SHEET = '{0}' AND TYPE = 'GEO'".format(
                                sheet_key.replace("'", "''")))
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
                    _diag(u"sheet={0} corner-deoverlap moves={1}".format(
                        sheet_key, moves))
            except Exception:
                if continue_on_error:
                    _add_warn(u"Corner de-overlap failed: {0}".format(
                        traceback.format_exc()))
                else:
                    raise

        _diag(u"sheet={0} proj={1}+{2} geo={3}/{4} glines={5}".format(
            sheet_key, len(xs), len(ys),
            res.get("ticks_geo", 0), res.get("labels_geo", 0),
            res.get("grid_lines", 0)))
        _lock_write_line(log_file, u"[{0}] OK SMART_FEATURE sheet={1} proj={2}+{3} geo={4}/{5} glines={6} corner_moves={7}".format(
            _now_str(), sheet_key, len(xs), len(ys),
            res.get("ticks_geo", 0), res.get("labels_geo", 0),
            res.get("grid_lines", 0), moves))
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

def _run_esri_make_grids(template_xml, aoi, fds, out_layer_name, grid_name=None,
                          refscale=None, rotation=None, mask_mm=None,
                          primary_sr=None, configure_layout=False):
    _require_cartography_level()
    if not hasattr(arcpy, "cartography") or \
       not hasattr(arcpy.cartography, "MakeGridsAndGraticulesLayer"):
        _raise(u"Cartography toolbox not available in this ArcMap installation.")

    mask_val = None
    if mask_mm and _safe_float(mask_mm, 0.0) > 0:
        mask_val = "{0} Millimeters".format(_safe_float(mask_mm))

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
    args.append(None)             # xy_tolerance default
    args.append(primary_sr)       # primary_cs (None ok)
    args.append(conf)

    try:
        return arcpy.cartography.MakeGridsAndGraticulesLayer(*args)
    except arcpy.ExecuteError:
        _raise(u"MakeGridsAndGraticulesLayer failed: {0}".format(
            arcpy.GetMessages(2)))
    except Exception as e:
        _raise(u"MakeGridsAndGraticulesLayer failed: {0}".format(e))


# =============================================================================
# 7. MXD helpers (ArcMap mapping module)
# =============================================================================

def _get_df(mxd, df_name=None):
    dfs = arcpy.mapping.ListDataFrames(mxd)
    if not dfs:
        _raise(u"No data frames found in MXD.")
    if df_name:
        for d in dfs:
            if d.name == df_name:
                return d
        _add_warn(u"Data frame '{0}' not found; using first data frame.".format(
            df_name))
    return dfs[0]

def _list_mxds(folder, recursive=False):
    out = []
    try:
        for root, dirs, files in os.walk(folder):
            for fn in files:
                if fn.lower().endswith(".mxd"):
                    out.append(os.path.join(root, fn))
            if not recursive:
                break
    except Exception:
        pass
    out.sort()
    return out

def _norm_path(p):
    try:
        return os.path.normcase(os.path.normpath(p))
    except Exception:
        return p

def _df_existing_datasources(mxd, df):
    ds = set()
    try:
        layers = arcpy.mapping.ListLayers(mxd, "", df)
    except Exception:
        layers = []
    for lyr in layers:
        try:
            if lyr and lyr.supports("DATASOURCE"):
                ds.add(_norm_path(lyr.dataSource))
        except Exception:
            pass
    return ds

def _apply_symbology(layer, layerfile_path, strict=False):
    if not layerfile_path:
        return
    if not os.path.isfile(layerfile_path):
        if strict:
            _raise(u"Layer file not found: {0}".format(layerfile_path))
        _add_warn(u"Layer file not found: {0}".format(layerfile_path))
        return
    try:
        arcpy.management.ApplySymbologyFromLayer(layer, layerfile_path)
    except Exception:
        msg = arcpy.GetMessages(2) or traceback.format_exc()
        if strict:
            _raise(u"ApplySymbology failed: {0}".format(msg))
        _add_warn(u"ApplySymbology failed: {0}".format(msg))

def _add_fc_layer_if_missing(mxd, df, fc_path,
                              position="TOP", symbology_layerfile=None):
    if not fc_path or (not arcpy.Exists(fc_path)):
        return
    existing = _df_existing_datasources(mxd, df)
    if _norm_path(fc_path) in existing:
        return
    try:
        lyr = arcpy.mapping.Layer(fc_path)
        arcpy.mapping.AddLayer(df, lyr, position)
        if symbology_layerfile:
            _apply_symbology(lyr, symbology_layerfile, strict=False)
    except Exception:
        _add_warn(u"AddLayer failed for {0}: {1}".format(
            fc_path, traceback.format_exc()))

def _add_smart_outputs_to_mxd(mxd, df, fds_path, symbology_layerfile=None):
    for fc_name in ["TKS_Ticks", "LBL_Labels", "GLN_GridLines"]:
        fc = os.path.join(fds_path, fc_name)
        _add_fc_layer_if_missing(mxd, df, fc,
                                  position="TOP",
                                  symbology_layerfile=symbology_layerfile)



# =============================================================================
# 8. Toolbox + Tool class
# =============================================================================

class Toolbox(object):
    def __init__(self):
        self.label = u"Cartographic Automation (ArcMap, v6 hardened) - Plugin 07"
        self.alias = "plugin07_batch_grid_v6"
        self.tools = [BatchGridBuilder07]


class BatchGridBuilder07(object):
    """Batch Grid / Graticule Builder - hardened v6."""

    # Parameter indices (single source of truth; UI + execute share them)
    IDX_MODE              = 0
    IDX_MXD_FOLDER        = 1
    IDX_RECURSIVE         = 2
    IDX_DF_NAME           = 3
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
    IDX_ADD_TO_MXD        = 37
    IDX_SAVE_MXD_COPY     = 38
    IDX_OUT_MXD_FOLDER    = 39
    IDX_EXPORT_PDF        = 40
    IDX_OUT_PDF_FOLDER    = 41
    IDX_PDF_DPI           = 42
    IDX_EXPORT_PNG        = 43
    IDX_EXPORT_JPEG       = 44
    IDX_OUT_IMG_FOLDER    = 45
    IDX_IMG_DPI           = 46
    IDX_LOG_FILE          = 47

    def __init__(self):
        self.label = u"07) Batch Grid Builder (ArcMap 2.7) - v6 hardened"
        self.description = (
            u"Batch build grids/graticules for multiple sheets, with corner "
            u"de-overlap, optional MXD save and PDF/PNG/JPEG export.\n\n"
            u"v6 hardening:\n"
            u" - SELECTION-BYPASS hardwired on AOI layers\n"
            u" - Per-MXD memory cleanup (del + gc.collect + ClearWorkspaceCache)\n"
            u" - Narrow exceptions; tracebacks printed on failure\n"
            u" - Bug fix: updateParameters & execute are now class methods\n"
            u" - Bug fix: parameter indices remapped via constants\n"
            u" - Stage-by-stage [DIAG] logging."
        )
        self.canRunInBackground = False

    def isLicensed(self):
        return True

    # ---------- Parameters ----------
    def getParameterInfo(self):
        p = []

        # Mode + sources
        p_mode = arcpy.Parameter("Mode", "mode", "GPString", "Required", "Input")
        p_mode.filter.type = "ValueList"
        p_mode.filter.list = ["FOLDER_OF_MXDS", "AOI_LAYER_IN_CURRENT_MXD"]
        p_mode.value = "FOLDER_OF_MXDS"
        p.append(p_mode)

        p_mfold = arcpy.Parameter(u"MXD Folder (Mode=FOLDER_OF_MXDS)", "mxd_folder",
                                   "DEFolder", "Optional", "Input")
        p.append(p_mfold)
        p_rec = arcpy.Parameter(u"Include Subfolders", "recursive",
                                "GPBoolean", "Optional", "Input")
        p_rec.value = False
        p.append(p_rec)
        p_df = arcpy.Parameter(u"Data Frame Name (optional; default=first DF)",
                                "data_frame_name",
                                "GPString", "Optional", "Input")
        p.append(p_df)

        p_aoi = arcpy.Parameter(u"AOI Layer (Mode=AOI_LAYER_IN_CURRENT_MXD)",
                                 "aoi_layer",
                                 "GPFeatureLayer", "Optional", "Input")
        p.append(p_aoi)
        p_aoi_nf = arcpy.Parameter(u"AOI Name Field", "aoi_name_field",
                                    "Field", "Optional", "Input")
        p_aoi_nf.parameterDependencies = [p_aoi.name]
        p.append(p_aoi_nf)

        # Engine
        p_eng = arcpy.Parameter(u"Engine", "engine",
                                 "GPString", "Required", "Input")
        p_eng.filter.type = "ValueList"
        p_eng.filter.list = ["SMART_FEATURE", "ESRI_XML"]
        p_eng.value = "SMART_FEATURE"
        p.append(p_eng)

        p_xml = arcpy.Parameter(u"Grid Template XML (Engine=ESRI_XML)",
                                 "grid_xml", "DEFile", "Optional", "Input")
        p_xml.filter.list = ["xml"]
        p.append(p_xml)

        # Output
        p_ows = arcpy.Parameter(u"Output Workspace (.gdb or folder)", "out_ws",
                                 "DEWorkspace", "Required", "Input")
        p.append(p_ows)
        p_fds = arcpy.Parameter(u"Feature Dataset Name (for SMART_FEATURE outputs)",
                                 "fds_name", "GPString", "Optional", "Input")
        p_fds.value = "Grids"
        p.append(p_fds)

        # Reference scale + rotation
        p_rs = arcpy.Parameter(u"Reference Scale Denominator (e.g., 25000)",
                                "refscale_denom",
                                "GPDouble", "Required", "Input")
        p_rs.value = 25000
        p.append(p_rs)
        p_rot = arcpy.Parameter(u"(SMART_FEATURE) Respect Data Frame Rotation",
                                 "respect_df_rotation",
                                 "GPBoolean", "Optional", "Input")
        p_rot.value = True
        p.append(p_rot)

        # SMART_FEATURE
        p_sp = arcpy.Parameter(u"(SMART_FEATURE) Projected Interval (map units)",
                                "spacing_proj", "GPDouble", "Optional", "Input")
        p_sp.value = 1000.0
        p.append(p_sp)
        p_dv = arcpy.Parameter(u"(SMART_FEATURE) Projected Label Divisor "
                                u"(1000 => 295 instead of 295000)",
                                "divisor_proj", "GPDouble", "Optional", "Input")
        p_dv.value = 1000.0
        p.append(p_dv)
        p_tk = arcpy.Parameter(u"(SMART_FEATURE) Tick Length (mm)",
                                "tick_mm", "GPDouble", "Optional", "Input")
        p_tk.value = 1.5
        p.append(p_tk)
        p_lb = arcpy.Parameter(u"(SMART_FEATURE) Label Offset (mm)",
                                "label_mm", "GPDouble", "Optional", "Input")
        p_lb.value = 3.0
        p.append(p_lb)
        p_gl = arcpy.Parameter(u"(SMART_FEATURE) Create Grid Lines (full)",
                                "create_grid_lines",
                                "GPBoolean", "Optional", "Input")
        p_gl.value = True
        p.append(p_gl)

        p_lm = arcpy.Parameter(u"(SMART_FEATURE) Projected Label Format",
                                "proj_label_mode",
                                "GPString", "Optional", "Input")
        p_lm.filter.type = "ValueList"
        p_lm.filter.list = ["INT", "FLOAT"]
        p_lm.value = "INT"
        p.append(p_lm)
        p_us = arcpy.Parameter(u"(SMART_FEATURE) Projected Label Unit Suffix",
                                "proj_unit_suffix",
                                "GPString", "Optional", "Input")
        p_us.value = ""
        p.append(p_us)
        p_pd = arcpy.Parameter(u"(SMART_FEATURE) Projected Label Pad to 3 digits",
                                "proj_pad3",
                                "GPBoolean", "Optional", "Input")
        p_pd.value = True
        p.append(p_pd)

        # Graticule
        p_eg = arcpy.Parameter(u"(SMART_FEATURE) Enable Graticule (Lat/Lon)",
                                "enable_graticule",
                                "GPBoolean", "Optional", "Input")
        p_eg.value = True
        p.append(p_eg)
        p_gm = arcpy.Parameter(u"(SMART_FEATURE) Graticule Interval (minutes)",
                                "grat_minutes",
                                "GPDouble", "Optional", "Input")
        p_gm.value = 2.5
        p.append(p_gm)
        p_gmd = arcpy.Parameter(u"(SMART_FEATURE) Graticule Mode",
                                 "grat_mode",
                                 "GPString", "Optional", "Input")
        p_gmd.filter.type = "ValueList"
        p_gmd.filter.list = ["TRUE_INTERVAL", "SAMPLE_AT_PROJECTED_TICKS"]
        p_gmd.value = "TRUE_INTERVAL"
        p.append(p_gmd)
        p_gw = arcpy.Parameter(u"(SMART_FEATURE) Geographic WKID for Graticule",
                                "geo_wkid", "GPLong", "Optional", "Input")
        p_gw.value = 4326
        p.append(p_gw)
        p_glm = arcpy.Parameter(u"(SMART_FEATURE) Graticule Label Offset (mm)",
                                 "grat_label_mm",
                                 "GPDouble", "Optional", "Input")
        p_glm.value = 3.5
        p.append(p_glm)
        p_gh = arcpy.Parameter(u"(SMART_FEATURE) Show Hemisphere (N/S/E/W)",
                                "grat_hemi",
                                "GPBoolean", "Optional", "Input")
        p_gh.value = False
        p.append(p_gh)

        # Corner de-overlap
        p_do = arcpy.Parameter(u"(SMART_FEATURE) Auto De-overlap at Corners",
                                "deoverlap_corners",
                                "GPBoolean", "Optional", "Input")
        p_do.value = True
        p.append(p_do)
        p_ms = arcpy.Parameter(u"(SMART_FEATURE) Minimum Separation (mm)",
                                "min_sep_mm",
                                "GPDouble", "Optional", "Input")
        p_ms.value = 1.5
        p.append(p_ms)
        p_ce = arcpy.Parameter(u"(SMART_FEATURE) Corner Extra Shift (mm)",
                                "corner_extra_mm",
                                "GPDouble", "Optional", "Input")
        p_ce.value = 1.0
        p.append(p_ce)

        # Safety
        p_co = arcpy.Parameter(u"Continue On Error (best-effort batch)",
                                "continue_on_error",
                                "GPBoolean", "Optional", "Input")
        p_co.value = True
        p.append(p_co)
        p_mt = arcpy.Parameter(u"Max ticks/gridlines per edge (safety)",
                                "max_ticks", "GPLong", "Optional", "Input")
        p_mt.value = 20000
        p.append(p_mt)

        # Cleanup
        p_cl = arcpy.Parameter(u"Clean old grids for sheet before creating new",
                                "cleanup_sheet",
                                "GPBoolean", "Optional", "Input")
        p_cl.value = True
        p.append(p_cl)
        p_dr = arcpy.Parameter(u"Dry Run (no outputs written)",
                                "dry_run",
                                "GPBoolean", "Optional", "Input")
        p_dr.value = False
        p.append(p_dr)

        # Symbology
        p_sym = arcpy.Parameter(u"Apply Symbology From Layerfile (optional)",
                                 "symbology_layerfile",
                                 "DEFile", "Optional", "Input")
        p_sym.filter.list = ["lyr", "lyrx"]
        p.append(p_sym)

        # ESRI_XML
        p_msk = arcpy.Parameter(u"(ESRI_XML) Mask Size (mm)",
                                 "mask_mm", "GPDouble", "Optional", "Input")
        p_msk.value = 5.0
        p.append(p_msk)
        p_af = arcpy.Parameter(u"(ESRI_XML) XML AutoFix (best-effort)",
                                "xml_autofix",
                                "GPBoolean", "Optional", "Input")
        p_af.value = True
        p.append(p_af)
        p_da = arcpy.Parameter(u"(ESRI_XML) Ancillary Offset Delta",
                                "xml_delta_anc",
                                "GPDouble", "Optional", "Input")
        p_da.value = 2.0
        p.append(p_da)

        # Export / persistence
        p_atm = arcpy.Parameter(u"Add outputs to MXD",
                                 "add_to_mxd",
                                 "GPBoolean", "Optional", "Input")
        p_atm.value = True
        p.append(p_atm)
        p_smc = arcpy.Parameter(u"Save MXD copy (Mode=FOLDER_OF_MXDS)",
                                 "save_mxd_copy",
                                 "GPBoolean", "Optional", "Input")
        p_smc.value = True
        p.append(p_smc)
        p_omf = arcpy.Parameter(u"Output MXD Folder",
                                 "out_mxd_folder",
                                 "DEFolder", "Optional", "Input")
        p.append(p_omf)
        p_epd = arcpy.Parameter(u"Export PDF",
                                 "export_pdf",
                                 "GPBoolean", "Optional", "Input")
        p_epd.value = False
        p.append(p_epd)
        p_opd = arcpy.Parameter(u"Output PDF Folder",
                                 "out_pdf_folder",
                                 "DEFolder", "Optional", "Input")
        p.append(p_opd)
        p_pdpi = arcpy.Parameter(u"PDF Resolution (DPI)",
                                  "pdf_dpi",
                                  "GPLong", "Optional", "Input")
        p_pdpi.value = 300
        p.append(p_pdpi)
        p_epng = arcpy.Parameter(u"Export PNG",
                                  "export_png",
                                  "GPBoolean", "Optional", "Input")
        p_epng.value = False
        p.append(p_epng)
        p_ejpg = arcpy.Parameter(u"Export JPEG",
                                  "export_jpeg",
                                  "GPBoolean", "Optional", "Input")
        p_ejpg.value = False
        p.append(p_ejpg)
        p_oimg = arcpy.Parameter(u"Output Image Folder (PNG/JPEG)",
                                  "out_img_folder",
                                  "DEFolder", "Optional", "Input")
        p.append(p_oimg)
        p_idpi = arcpy.Parameter(u"Image Resolution (DPI)",
                                  "img_dpi",
                                  "GPLong", "Optional", "Input")
        p_idpi.value = 300
        p.append(p_idpi)
        p_log = arcpy.Parameter(u"Log file (optional)",
                                 "log_file",
                                 "DEFile", "Optional", "Input")
        p.append(p_log)

        return p

    # ---------- updateParameters (now correctly inside the class) ----------
    def updateParameters(self, parameters):
        try:
            mode = parameters[self.IDX_MODE].valueAsText
            engine = parameters[self.IDX_ENGINE].valueAsText

            # Mode toggles
            parameters[self.IDX_MXD_FOLDER].enabled = (mode == "FOLDER_OF_MXDS")
            parameters[self.IDX_RECURSIVE].enabled = (mode == "FOLDER_OF_MXDS")
            parameters[self.IDX_AOI_LAYER].enabled = (mode == "AOI_LAYER_IN_CURRENT_MXD")
            parameters[self.IDX_AOI_NAME_FIELD].enabled = (
                mode == "AOI_LAYER_IN_CURRENT_MXD")

            # Engine toggles
            is_xml = (engine == "ESRI_XML")
            parameters[self.IDX_GRID_XML].enabled = is_xml

            # SMART_FEATURE-only params (RESPECT_ROT through CORNER_EXTRA_MM)
            for idx in range(self.IDX_RESPECT_ROT, self.IDX_CORNER_EXTRA_MM + 1):
                parameters[idx].enabled = (not is_xml)

            # ESRI_XML-only params
            for idx in (self.IDX_MASK_MM, self.IDX_XML_AUTOFIX,
                        self.IDX_XML_DELTA_ANC):
                parameters[idx].enabled = is_xml

            # Folder-batch exports only meaningful in FOLDER_OF_MXDS
            parameters[self.IDX_ADD_TO_MXD].enabled = True
            for idx in range(self.IDX_SAVE_MXD_COPY, self.IDX_IMG_DPI + 1):
                parameters[idx].enabled = (mode == "FOLDER_OF_MXDS")
            parameters[self.IDX_LOG_FILE].enabled = True

            # Dependent folders
            parameters[self.IDX_OUT_MXD_FOLDER].enabled = (
                mode == "FOLDER_OF_MXDS"
                and bool(parameters[self.IDX_SAVE_MXD_COPY].value))
            parameters[self.IDX_OUT_PDF_FOLDER].enabled = (
                mode == "FOLDER_OF_MXDS"
                and bool(parameters[self.IDX_EXPORT_PDF].value))
            parameters[self.IDX_OUT_IMG_FOLDER].enabled = (
                mode == "FOLDER_OF_MXDS"
                and (bool(parameters[self.IDX_EXPORT_PNG].value)
                     or bool(parameters[self.IDX_EXPORT_JPEG].value)))
        except Exception:
            pass

    # ---------- updateMessages ----------
    def updateMessages(self, parameters):
        try:
            engine = parameters[self.IDX_ENGINE].valueAsText
            if engine == "ESRI_XML":
                xml = parameters[self.IDX_GRID_XML].valueAsText
                if not xml:
                    parameters[self.IDX_GRID_XML].setErrorMessage(
                        u"Grid Template XML is required for ESRI_XML engine.")
            mode = parameters[self.IDX_MODE].valueAsText
            if mode == "FOLDER_OF_MXDS":
                if not parameters[self.IDX_MXD_FOLDER].valueAsText:
                    parameters[self.IDX_MXD_FOLDER].setErrorMessage(
                        u"MXD Folder is required for FOLDER_OF_MXDS mode.")
            elif mode == "AOI_LAYER_IN_CURRENT_MXD":
                if not parameters[self.IDX_AOI_LAYER].valueAsText:
                    parameters[self.IDX_AOI_LAYER].setErrorMessage(
                        u"AOI Layer is required for AOI mode.")
            rs = parameters[self.IDX_REFSCALE].value
            if rs is not None and float(rs) <= 0:
                parameters[self.IDX_REFSCALE].setErrorMessage(
                    u"Reference Scale must be > 0.")
            sp = parameters[self.IDX_SPACING_PROJ].value
            if (sp is not None) and (engine == "SMART_FEATURE") and float(sp) <= 0:
                parameters[self.IDX_SPACING_PROJ].setErrorMessage(
                    u"Projected interval must be > 0.")
        except Exception:
            pass



    # ---------- execute (now correctly inside the class) ----------
    def execute(self, parameters, messages):
        arcpy.env.overwriteOutput = True
        try:
            arcpy.env.parallelProcessingFactor = "100%"
        except Exception:
            pass

        # Read parameters via named indices (no more drift)
        mode             = parameters[self.IDX_MODE].valueAsText
        mxd_folder       = parameters[self.IDX_MXD_FOLDER].valueAsText
        recursive        = bool(parameters[self.IDX_RECURSIVE].value)
        df_name          = parameters[self.IDX_DF_NAME].valueAsText
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
        proj_unit_suffix = parameters[self.IDX_PROJ_UNIT_SUFFIX].valueAsText or u""
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

        add_to_mxd       = bool(parameters[self.IDX_ADD_TO_MXD].value)
        save_mxd_copy    = bool(parameters[self.IDX_SAVE_MXD_COPY].value)
        out_mxd_folder   = parameters[self.IDX_OUT_MXD_FOLDER].valueAsText
        export_pdf       = bool(parameters[self.IDX_EXPORT_PDF].value)
        out_pdf_folder   = parameters[self.IDX_OUT_PDF_FOLDER].valueAsText
        pdf_dpi          = _safe_int(parameters[self.IDX_PDF_DPI].value, 300)
        export_png       = bool(parameters[self.IDX_EXPORT_PNG].value)
        export_jpeg      = bool(parameters[self.IDX_EXPORT_JPEG].value)
        out_img_folder   = parameters[self.IDX_OUT_IMG_FOLDER].valueAsText
        img_dpi          = _safe_int(parameters[self.IDX_IMG_DPI].value, 300)
        log_file_param   = parameters[self.IDX_LOG_FILE].valueAsText

        # Selection-bypass announcement on AOI layer
        aoi_layer = None
        aoi_layer_path = None
        if mode == "AOI_LAYER_IN_CURRENT_MXD" and aoi_layer_param:
            _announce_selection(u"AOI", aoi_layer_param)
            aoi_layer = aoi_layer_param
            aoi_layer_path = _resolve_full_source(aoi_layer_param)

        # Output workspace -> ensure file GDB
        gdb = _ensure_file_gdb(out_ws, "grid_output.gdb")

        # Log file: default per-run inside the GDB folder
        if not log_file_param:
            log_file = os.path.join(
                os.path.dirname(gdb),
                "grid_batch_{0}.log".format(time.strftime("%Y%m%d_%H%M%S")))
        else:
            log_file = log_file_param

        _lock_write_line(
            log_file,
            u"==== BatchGridBuilder07 v6 start {0} engine={1} mode={2} ====".format(
                _now_str(), engine, mode))
        _diag(u"Engine={0} Mode={1} Output GDB={2}".format(engine, mode, gdb))

        if refscale <= 0:
            _raise(u"Reference scale denominator must be > 0.")

        # Mode logic
        if mode == "FOLDER_OF_MXDS":
            if not mxd_folder or not os.path.isdir(mxd_folder):
                _raise(u"MXD folder not found.")
            mxds = _list_mxds(mxd_folder, recursive)
            if not mxds:
                _raise(u"No MXD files found in folder.")
            _diag(u"MXDs to process: {0}".format(len(mxds)))
        else:
            mxds = [None]
            if not aoi_layer:
                _raise(u"AOI layer is required for AOI_LAYER_IN_CURRENT_MXD mode.")

        # ESRI_XML: validate template
        if engine == "ESRI_XML":
            if not grid_xml or not os.path.isfile(grid_xml):
                _raise(u"Grid Template XML not found.")
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

        # Process each MXD
        for idx_mxd, mxd_path in enumerate(mxds):
            mxd = None
            try:
                if mode == "FOLDER_OF_MXDS":
                    _diag(u"Open MXD ({0}/{1}): {2}".format(
                        idx_mxd + 1, len(mxds), mxd_path))
                    mxd = arcpy.mapping.MapDocument(mxd_path)
                else:
                    mxd = arcpy.mapping.MapDocument("CURRENT")

                df = _get_df(mxd, df_name)
                sr = df.spatialReference

                if engine == "ESRI_XML" and getattr(sr, "type", None) == "Geographic":
                    _raise(u"ESRI_XML engine requires a projected CRS for the data frame.")

                if engine == "SMART_FEATURE":
                    fds = _ensure_feature_dataset(gdb, fds_name, sr)
                else:
                    fds = _ensure_feature_dataset(gdb, fds_name, sr)

                # Determine sheets list: (sheet_name, extent, oid)
                sheets = []
                oid_field = None
                if mode == "FOLDER_OF_MXDS":
                    sheet_name = os.path.splitext(os.path.basename(mxd_path))[0]
                    sheets.append((sheet_name, df.extent, None))
                else:
                    # AOI mode: read FROM ON-DISK source (selection-bypass)
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
                                nm = unicode(row[1]) if row[1] is not None else unicode(oid)
                            else:
                                nm = unicode(oid)
                            sheets.append((nm, ext, oid))
                    if not sheets:
                        _raise(u"AOI layer has no valid features/extents.")
                    _diag(u"AOI sheets: {0}".format(len(sheets)))

                try:
                    arcpy.SetProgressor("step", "Processing sheets...",
                                          0, len(sheets), 1)
                except Exception:
                    pass

                for (sheet_name, ext, oid) in sheets:
                    sheet_key = _validate_name(sheet_name, gdb)
                    _lock_write_line(
                        log_file,
                        u"[{0}] START sheet={1}".format(_now_str(), sheet_key))

                    try:
                        if engine == "SMART_FEATURE":
                            grat_deg = (grat_minutes / 60.0
                                         if grat_minutes and grat_minutes > 0 else 0.0)
                            _build_ticks_and_labels_for_extent(
                                df=df, ext=ext, sheet_key=sheet_key,
                                fds=fds, sr=sr,
                                scale_denom=refscale,
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
                            # ESRI_XML engine
                            extent_str = "{0} {1} {2} {3}".format(
                                ext.XMin, ext.YMin, ext.XMax, ext.YMax)
                            out_layer_name = "GRID_{0}_{1}".format(
                                sheet_key,
                                (oid if oid is not None else "EXT"))
                            aoi_arg = extent_str
                            grid_name_arg = sheet_key
                            if (mode != "FOLDER_OF_MXDS"
                                    and oid is not None and aoi_layer):
                                # Tool requires the layer with selection. Use the
                                # original layer (we only resolve to source for
                                # iteration; for the tool itself we set selection
                                # then clear).
                                try:
                                    arcpy.management.SelectLayerByAttribute(
                                        aoi_layer, "NEW_SELECTION",
                                        u"{0} = {1}".format(oid_field, oid))
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
                                rotation=(df.rotation if respect_rot else 0.0),
                                mask_mm=mask_mm,
                                primary_sr=sr,
                                configure_layout=False)

                            if (mode != "FOLDER_OF_MXDS" and aoi_layer):
                                try:
                                    arcpy.management.SelectLayerByAttribute(
                                        aoi_layer, "CLEAR_SELECTION")
                                except Exception:
                                    pass
                            _add_msg(u"Created ESRI grid layer: {0}".format(
                                out_layer_name))

                        _lock_write_line(
                            log_file,
                            u"[{0}] DONE sheet={1}".format(_now_str(), sheet_key))
                    except Exception:
                        _lock_write_line(
                            log_file,
                            u"[{0}] ERROR sheet={1} err={2}".format(
                                _now_str(), sheet_key, traceback.format_exc()))
                        if not continue_on_err:
                            raise
                        _add_warn(u"Sheet {0} failed: {1}".format(
                            sheet_key, traceback.format_exc()))
                    try:
                        arcpy.SetProgressorPosition()
                    except Exception:
                        pass

                # Add SMART_FEATURE outputs once per MXD
                if (engine == "SMART_FEATURE") and add_to_mxd and (not dry_run):
                    try:
                        _add_smart_outputs_to_mxd(
                            mxd, df, fds, symbology_layerfile=symbology_lyr)
                    except Exception:
                        _add_warn(u"Failed to add output layers to MXD: {0}".format(
                            traceback.format_exc()))

                try:
                    arcpy.ResetProgressor()
                except Exception:
                    pass

                # Save / Export per MXD
                if mode == "FOLDER_OF_MXDS" and (not dry_run):
                    base = os.path.splitext(os.path.basename(mxd_path))[0]
                    if save_mxd_copy:
                        if not out_mxd_folder:
                            _raise(u"Output MXD Folder is required when "
                                   u"Save MXD copy is True.")
                        _mkdir(out_mxd_folder)
                        out_mxd = os.path.join(out_mxd_folder, base + "_grid.mxd")
                        try:
                            mxd.saveACopy(out_mxd)
                            _add_msg(u"Saved MXD copy: {0}".format(out_mxd))
                        except Exception:
                            _add_warn(u"saveACopy failed: {0}".format(
                                traceback.format_exc()))
                    if export_pdf:
                        if not out_pdf_folder:
                            _raise(u"Output PDF Folder is required when "
                                   u"Export PDF is True.")
                        _mkdir(out_pdf_folder)
                        out_pdf = os.path.join(out_pdf_folder, base + ".pdf")
                        try:
                            arcpy.mapping.ExportToPDF(mxd, out_pdf, resolution=pdf_dpi)
                            _add_msg(u"Exported PDF: {0}".format(out_pdf))
                        except Exception:
                            _add_warn(u"ExportToPDF failed: {0}".format(
                                traceback.format_exc()))
                    if export_png or export_jpeg:
                        if not out_img_folder:
                            _raise(u"Output Image Folder is required for "
                                   u"PNG/JPEG export.")
                        _mkdir(out_img_folder)
                        if export_png:
                            out_png = os.path.join(out_img_folder, base + ".png")
                            try:
                                arcpy.mapping.ExportToPNG(mxd, out_png, resolution=img_dpi)
                                _add_msg(u"Exported PNG: {0}".format(out_png))
                            except Exception:
                                _add_warn(u"ExportToPNG failed: {0}".format(
                                    traceback.format_exc()))
                        if export_jpeg:
                            out_jpg = os.path.join(out_img_folder, base + ".jpg")
                            try:
                                arcpy.mapping.ExportToJPEG(mxd, out_jpg, resolution=img_dpi)
                                _add_msg(u"Exported JPEG: {0}".format(out_jpg))
                            except Exception:
                                _add_warn(u"ExportToJPEG failed: {0}".format(
                                    traceback.format_exc()))

            except Exception:
                _lock_write_line(
                    log_file,
                    u"[{0}] ERROR mxd={1} err={2}".format(
                        _now_str(),
                        (mxd_path if mxd_path else u"CURRENT"),
                        traceback.format_exc()))
                if not continue_on_err:
                    raise
                _add_warn(u"Failed on {0}: {1}".format(
                    mxd_path or u"CURRENT", traceback.format_exc()))
            finally:
                # Per-MXD memory cleanup (32-bit ArcMap leak mitigation)
                try:
                    if mxd is not None:
                        del mxd
                except Exception:
                    pass
                try:
                    arcpy.management.ClearWorkspaceCache()
                except Exception:
                    pass
                gc.collect()

        _lock_write_line(
            log_file,
            u"==== BatchGridBuilder07 v6 finished {0} ====".format(_now_str()))
        _add_msg(u"Done. Log: {0}".format(log_file))
