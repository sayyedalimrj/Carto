# -*- coding: utf-8 -*-
"""
Plugin 07 (ArcMap / Python 2.7)
Batch Grid / Graticule Builder (ESRI XML engine + SMART Feature engine)

Goals:
- Build Grid/Graticule for a large number of map sheets (16/24/...) in batch mode
- Prevent/reduce label collisions (especially UTM + Lat/Lon in the corners)
- Compatible with ArcMap (arcpy.mapping) and Python 2.7

Two engines:
1) ESRI_XML:
   - Uses arcpy.cartography.MakeGridsAndGraticulesLayer (Cartography toolbox)
   - Loops over a multi-feature AOI (because the official tool only uses the "first" feature/selection)
2) SMART_FEATURE:
   - Generates Tick/Label/GridLine as features inside a FeatureDataset
   - Advantage: better control with Maplex/Label Engine and symbology, and easy to batch-process

Robustness design notes:
- Avoid bare `except: pass` (do not silently hide errors)
- Check license/product level for Cartography tools
- Protect against division by zero and invalid inputs
- Clean up temporary outputs and reduce memory leaks when processing multiple MXDs
"""

from __future__ import division

import os
import re
import math
import time
import uuid
import gc

import arcpy

try:
    unicode
except NameError:
    unicode = str

try:
    import xml.etree.ElementTree as ET
except:
    ET = None

# Optional faster XML (if installed)
try:
    from lxml import etree as LET
except:
    LET = None

# Optional lock on Windows
try:
    import msvcrt
except:
    msvcrt = None


# --------------------------------------------------------------------------------------
# Helpers (logging, validation, safe names, etc.)
# --------------------------------------------------------------------------------------

def _now_str():
    return time.strftime("%Y-%m-%d %H:%M:%S")

def _add_msg(msg):
    try:
        arcpy.AddMessage(msg)
    except:
        pass

def _add_warn(msg):
    try:
        arcpy.AddWarning(msg)
    except:
        pass

def _add_err(msg):
    try:
        arcpy.AddError(msg)
    except:
        pass

def _raise(msg):
    _add_err(msg)
    raise RuntimeError(msg)

def _safe_float(v, default=0.0):
    try:
        if v is None:
            return default
        return float(v)
    except:
        return default

def _safe_int(v, default=0):
    try:
        if v is None:
            return default
        return int(v)
    except:
        return default

def _mkdir(folder):
    if folder and (not os.path.isdir(folder)):
        os.makedirs(folder)

def _is_gdb(path):
    return path and path.lower().endswith(".gdb") and os.path.isdir(path)

def _ensure_file_gdb(workspace_or_folder, gdb_name="grid_output.gdb"):
    """
    If the input path is a file geodatabase (.gdb), return it as-is.
    If it is a folder, create/use a .gdb inside it.
    """
    if not workspace_or_folder:
        _raise("Output workspace is empty.")
    ws = workspace_or_folder
    if _is_gdb(ws):
        return ws

    if not os.path.isdir(ws):
        _raise("Output folder not found: {0}".format(ws))

    gdb = os.path.join(ws, gdb_name)
    if not os.path.isdir(gdb):
        _add_msg("Creating GDB: {0}".format(gdb))
        arcpy.CreateFileGDB_management(ws, os.path.basename(gdb))
    return gdb

def _ensure_feature_dataset(gdb, fds_name, spatial_ref):
    if not fds_name:
        fds_name = "Grids"
    fds_name = _validate_name(fds_name, gdb)
    fds = os.path.join(gdb, fds_name)
    if not arcpy.Exists(fds):
        arcpy.CreateFeatureDataset_management(gdb, fds_name, spatial_ref)
    return fds

def _validate_name(name, workspace):
    """
    Use ValidateTableName to prevent invalid names (e.g., starting with a number).
    """
    if name is None:
        name = "GRID"
    if isinstance(name, unicode):
        raw = name
    else:
        try:
            raw = unicode(name)
        except:
            raw = unicode(str(name), "utf-8", "ignore")
    raw = raw.strip()
    if not raw:
        raw = u"GRID"
    # normalize
    raw = re.sub(u"[^0-9A-Za-z_]+", u"_", raw)
    raw = raw.strip(u"_")
    if not raw:
        raw = u"GRID"
    # ArcGIS validation
    try:
        v = arcpy.ValidateTableName(raw, workspace)
        if v:
            raw = v
    except:
        pass
    # Ensure starts with letter
    if re.match(u"^[0-9]", raw):
        raw = u"X_" + raw
    # length cap (FGDB: 160, but keep 64)
    if len(raw) > 64:
        raw = raw[:64]
    return raw

def _product_level():
    try:
        return arcpy.ProductInfo()  # 'Basic'/'Standard'/'Advanced' or 'ArcInfo' depending
    except:
        return None

def _require_cartography_level():
    """
    Make Grids And Graticules Layer is in the Cartography toolbox and, due to ArcMap licensing,
    it requires at least Standard (Basic does not support it).
    """
    lvl = _product_level()
    if not lvl:
        return
    # Normalize older names
    l = lvl.lower()
    if l in ["basic", "arcview"]:
        _raise("Cartography tool requires Standard/Advanced license. Current product level: {0}".format(lvl))

def _lock_write_line(fp, line_unicode):
    """
    log write with best-effort locking on Windows.
    """
    if not fp:
        return
    try:
        _mkdir(os.path.dirname(fp))
        # open binary for consistent locking
        f = open(fp, "ab")
        try:
            if msvcrt:
                try:
                    msvcrt.locking(f.fileno(), msvcrt.LK_LOCK, 1)
                except:
                    pass
            data = (line_unicode + u"\n")
            try:
                b = data.encode("utf-8")
            except:
                b = str(data)
            f.write(b)
        finally:
            try:
                if msvcrt:
                    try:
                        msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
                    except:
                        pass
            finally:
                f.close()
    except:
        # do not crash on logging
        pass

def _mm_to_map_units(mm, scale_denom, spatial_ref, allow_geo_approx=False, approx_lat_deg=None):
    """
    Convert millimeters on paper to map units.
    For linear units: mm * scale / 1000 -> meters (approx.)
    For Geographic CRS: default is ERROR (degrees are not linear).
    """
    mm = _safe_float(mm, 0.0)
    scale_denom = _safe_float(scale_denom, 0.0)
    if mm <= 0 or scale_denom <= 0:
        return 0.0

    if spatial_ref and getattr(spatial_ref, "type", None) == "Geographic":
        if not allow_geo_approx:
            _raise("Spatial Reference is Geographic (degrees). mm->map units is not reliable. "
                   "Project the data frame to a projected CRS (e.g., UTM).")
        # approximate: convert meters to degrees using latitude
        # 1 degree latitude ~ 111320 m ; longitude scales by cos(lat)
        lat = _safe_float(approx_lat_deg, 0.0)
        meters = (mm * scale_denom) / 1000.0
        meters_per_degree = 111320.0 * max(0.1, abs(math.cos(math.radians(lat))))
        return meters / meters_per_degree

    # Linear units: use metersPerUnit when available (handles feet, US survey feet, etc.)
    meters = (mm * scale_denom) / 1000.0
    mpu = None
    if spatial_ref is not None:
        try:
            mpu = float(getattr(spatial_ref, "metersPerUnit", None))
        except:
            mpu = None
    if mpu and mpu > 0:
        return meters / mpu
    return meters

def _get_df(mxd, df_name=None):
    dfs = arcpy.mapping.ListDataFrames(mxd)
    if not dfs:
        _raise("No data frames found in MXD.")
    if df_name:
        for d in dfs:
            if d.name == df_name:
                return d
        _add_warn("Data frame not found by name; using first data frame.")
    return dfs[0]

def _list_mxds(folder, recursive=False):
    out = []
    for root, dirs, files in os.walk(folder):
        for fn in files:
            if fn.lower().endswith(".mxd"):
                out.append(os.path.join(root, fn))
        if not recursive:
            break
    out.sort()
    return out

def _rotate_xy(x, y, cx, cy, ang_deg):
    """
    rotate point (x,y) around center (cx,cy) by ang_deg.
    """
    if not ang_deg:
        return (x, y)
    th = math.radians(ang_deg)
    dx = x - cx
    dy = y - cy
    xr = cx + (dx * math.cos(th) - dy * math.sin(th))
    yr = cy + (dx * math.sin(th) + dy * math.cos(th))
    return (xr, yr)

def _extent_edges_display(ext, respect_rotation=True, rotation_deg=0.0):
    """
    Return the four frame edges in "display-rectangle" coordinates (axis-aligned).
    If respect_rotation=True and rotation!=0:
      - We assume data is rotated by `rotation` around the extent center in display.
      - To place ticks on the page-aligned rectangle, we later rotate generated display points by -rotation.
    """
    xmin, ymin, xmax, ymax = ext.XMin, ext.YMin, ext.XMax, ext.YMax
    cx = (xmin + xmax) / 2.0
    cy = (ymin + ymax) / 2.0

    # display rectangle is axis-aligned; use extent directly as display domain
    edges = {
        "BOTTOM": ((xmin, ymin), (xmax, ymin)),
        "TOP": ((xmin, ymax), (xmax, ymax)),
        "LEFT": ((xmin, ymin), (xmin, ymax)),
        "RIGHT": ((xmax, ymin), (xmax, ymax)),
        "XMIN": xmin, "YMIN": ymin, "XMAX": xmax, "YMAX": ymax,
        "CX": cx, "CY": cy,
        "ROT": rotation_deg if (respect_rotation and rotation_deg) else 0.0
    }
    return edges

def _display_to_data_xy(x, y, edges):
    """
    If Data Frame rotation is enabled, convert a display point back to data coordinates (rotate -ROT).
    """
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
        _raise("Too many ticks/grid lines ({0}). Increase interval or set a larger max_ticks.".format(n))
    vals = [a + i * interval for i in range(n)]
    return vals

def _format_proj(val, divisor, fmt_mode=u"INT", unit_suffix=u"", pad3=False):
    """
    val in map units (e.g., meters). divisor=1000 -> show km-like.
    fmt_mode: INT / INT_M / FLOAT
    """
    if divisor and divisor != 0:
        v = val / float(divisor)
    else:
        v = val
    if fmt_mode == u"FLOAT":
        s = u"{0:.3f}".format(v)
    else:
        # INT / INT_M
        try:
            iv = int(round(v))
        except:
            iv = int(v)
        if pad3:
            s = u"{0:03d}".format(iv)
        else:
            s = unicode(iv)
    if unit_suffix:
        s = s + unit_suffix
    return s

def _format_dms(deg, is_lon=True, show_hemi=False, decimals=0):
    """
    deg: float degrees
    """
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
    # normalize 60
    if ss >= 60:
        ss = 0
        mm += 1
    if mm >= 60:
        mm = 0
        dd += 1

    if decimals and decimals > 0:
        ss_str = (u"{0:0" + unicode(2 + 1 + decimals) + u"." + unicode(decimals) + u"f}").format(ss)
    else:
        ss_str = u"{0:02d}".format(int(ss))
    s = u"{0:d}°{1:02d}\'{2}\"".format(dd, mm, ss_str)

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
    """
    points_xy: list[(x,y)]
    returns list[(x_out,y_out)] same length. None on failure.
    """
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
                _raise("Projection failed: {0}".format(e))
    return out

def _delete_by_sheet(fc, sheet_value):
    """
    delete features where SHEET == sheet_value, using DeleteFeatures for speed.
    """
    if (not fc) or (not arcpy.Exists(fc)):
        return
    lyr = "lyr_" + uuid.uuid4().hex[:8]
    arcpy.MakeFeatureLayer_management(fc, lyr)
    wc = "SHEET = '{0}'".format(sheet_value.replace("'", "''"))
    arcpy.SelectLayerByAttribute_management(lyr, "NEW_SELECTION", wc)
    # If selection empty, DeleteFeatures is no-op
    arcpy.DeleteFeatures_management(lyr)
    arcpy.Delete_management(lyr)

def _apply_symbology(layer, layerfile_path, strict=False):
    if not layerfile_path:
        return
    if not os.path.isfile(layerfile_path):
        if strict:
            _raise("Layer file not found: {0}".format(layerfile_path))
        _add_warn("Layer file not found: {0}".format(layerfile_path))
        return
    try:
        arcpy.ApplySymbologyFromLayer_management(layer, layerfile_path)
    except arcpy.ExecuteError:
        msg = arcpy.GetMessages(2)
        if strict:
            _raise("ApplySymbology failed: {0}".format(msg))
        _add_warn("ApplySymbology failed: {0}".format(msg))
    except Exception as e:
        if strict:
            _raise("ApplySymbology failed: {0}".format(e))
        _add_warn("ApplySymbology failed: {0}".format(e))


def _norm_path(p):
    try:
        return os.path.normcase(os.path.normpath(p))
    except:
        return p

def _df_existing_datasources(mxd, df):
    """Return a set of normalized datasources currently in the data frame."""
    ds = set()
    try:
        layers = arcpy.mapping.ListLayers(mxd, "", df)
    except:
        layers = []
    for lyr in layers:
        try:
            if lyr and lyr.supports("DATASOURCE"):
                ds.add(_norm_path(lyr.dataSource))
        except:
            pass
    return ds

def _add_fc_layer_if_missing(mxd, df, fc_path, position="TOP", symbology_layerfile=None):
    """Add a feature class as a layer to the TOC only if it's not already present."""
    if not fc_path or (not arcpy.Exists(fc_path)):
        return
    existing = _df_existing_datasources(mxd, df)
    if _norm_path(fc_path) in existing:
        return
    lyr = arcpy.mapping.Layer(fc_path)
    arcpy.mapping.AddLayer(df, lyr, position)
    if symbology_layerfile:
        _apply_symbology(lyr, symbology_layerfile, strict=False)

def _add_smart_outputs_to_mxd(mxd, df, fds_path, symbology_layerfile=None):
    """Add SMART_FEATURE output layers (ticks/labels/gridlines) once."""
    for fc_name in ["TKS_Ticks", "LBL_Labels", "GLN_GridLines"]:
        fc = os.path.join(fds_path, fc_name)
        _add_fc_layer_if_missing(mxd, df, fc, position="TOP", symbology_layerfile=symbology_layerfile)

# --------------------------------------------------------------------------------------
# XML AutoFix (best-effort)  (try lxml first)
# --------------------------------------------------------------------------------------

_OFFSET_KEYS = [u"offset", u"labeloffset", u"xoffset", u"yoffset", u"anchor"]

def _xml_autofix_temp(xml_path, temp_folder,
                      delta_primary=0.0,
                      delta_ancillary=2.0,
                      only_ancillary=True,
                      verbose=False):
    """
    Best-effort: whenever a numeric offset is found, slightly increase it in the ancillary subtree.
    (to reduce label overlap in corners)
    """
    if (not xml_path) or (not os.path.isfile(xml_path)):
        return xml_path

    _mkdir(temp_folder)
    out_xml = os.path.join(temp_folder, "patched_{0}_{1}.xml".format(uuid.uuid4().hex[:6], os.path.basename(xml_path)))

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
        except:
            return None

    if LET:
        parser = LET.XMLParser(remove_blank_text=True, recover=True)
        root = LET.parse(xml_path, parser).getroot()
        touched = 0
        for elem in root.iter():
            is_anc = _is_ancillary_node(elem.tag, elem.attrib)
            # attributes
            for ak in list(elem.attrib.keys()):
                if _is_offset_key(ak):
                    v = _try_float(elem.attrib.get(ak))
                    if v is None:
                        continue
                    if is_anc:
                        elem.attrib[ak] = str(v + float(delta_ancillary))
                        touched += 1
                    elif (not only_ancillary) and delta_primary:
                        elem.attrib[ak] = str(v + float(delta_primary))
                        touched += 1
            # text node
            if elem.text and _is_offset_key(elem.tag):
                v = _try_float(elem.text)
                if v is not None:
                    if is_anc:
                        elem.text = str(v + float(delta_ancillary)); touched += 1
                    elif (not only_ancillary) and delta_primary:
                        elem.text = str(v + float(delta_primary)); touched += 1
        if verbose:
            _add_msg("XML AutoFix: modified {0} offset values".format(touched))
        LET.ElementTree(root).write(out_xml, encoding="utf-8", xml_declaration=True, pretty_print=True)
        return out_xml

    if not ET:
        _add_warn("xml.etree not available; skipping XML AutoFix.")
        return xml_path

    tree = ET.parse(xml_path)
    root = tree.getroot()

    touched = 0
    for elem in root.iter():
        tag = elem.tag
        attrib = elem.attrib
        is_anc = _is_ancillary_node(tag, attrib)

        # attributes
        for ak in list(attrib.keys()):
            if _is_offset_key(ak):
                v = _try_float(attrib.get(ak))
                if v is None:
                    continue
                if is_anc:
                    attrib[ak] = str(v + float(delta_ancillary)); touched += 1
                elif (not only_ancillary) and delta_primary:
                    attrib[ak] = str(v + float(delta_primary)); touched += 1

        # element text if tag itself implies offset
        if elem.text and _is_offset_key(tag):
            v = _try_float(elem.text)
            if v is not None:
                if is_anc:
                    elem.text = str(v + float(delta_ancillary)); touched += 1
                elif (not only_ancillary) and delta_primary:
                    elem.text = str(v + float(delta_primary)); touched += 1

    if verbose:
        _add_msg("XML AutoFix: modified {0} offset values".format(touched))
    tree.write(out_xml, encoding="utf-8")
    return out_xml


# --------------------------------------------------------------------------------------
# SMART_FEATURE engine: create features for ticks/labels/gridlines
# --------------------------------------------------------------------------------------

def _ensure_fc(fds, name, geom_type, spatial_ref, fields):
    """
    fields: list of tuples (name, type, length)
    """
    fc = os.path.join(fds, name)
    if not arcpy.Exists(fc):
        arcpy.CreateFeatureclass_management(fds, name, geom_type, spatial_reference=spatial_ref)
        for (fn, ft, flen) in fields:
            if ft.upper() == "TEXT":
                arcpy.AddField_management(fc, fn, ft, field_length=flen)
            else:
                arcpy.AddField_management(fc, fn, ft)
    return fc

def _insert_tick(cur, sheet, typ, side, val, geom, is_major=1):
    cur.insertRow((geom, sheet, typ, side, float(val), int(is_major)))

def _insert_label(cur, sheet, typ, side, text, geom, rot=0.0):
    cur.insertRow((geom, sheet, typ, side, text, float(rot)))

def _build_ticks_and_labels_for_extent(df, ext, sheet_key,
                                       fds, sr, scale_denom,
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
    """
    Generate tick/label/gridlines into feature classes with a SHEET field.
    """
    # Input validation
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
        _raise("SMART_FEATURE engine requires Projected coordinate system for the data frame.")

    # Rotation
    rot = 0.0
    try:
        rot = float(df.rotation)
    except:
        rot = 0.0
    edges = _extent_edges_display(ext, respect_rotation=respect_df_rotation, rotation_deg=rot)

    # mm offsets to map units
    approx_lat = None
    if allow_geo_approx:
        # approximate latitude as mid of projected extent reprojected to WGS84
        try:
            geo_sr = arcpy.SpatialReference(4326)
            mid_xy = [((edges["XMIN"] + edges["XMAX"]) / 2.0, (edges["YMIN"] + edges["YMAX"]) / 2.0)]
            mid_ll = _project_points(mid_xy, sr, geo_sr, continue_on_error=True)
            approx_lat = mid_ll[0][1]
        except:
            approx_lat = 0.0

    tick_len = _mm_to_map_units(tick_len_mm, scale_denom, sr, allow_geo_approx, approx_lat_deg=approx_lat)
    lab_off = _mm_to_map_units(label_offset_mm, scale_denom, sr, allow_geo_approx, approx_lat_deg=approx_lat)
    grat_off = _mm_to_map_units(graticule_label_offset_mm, scale_denom, sr, allow_geo_approx, approx_lat_deg=approx_lat)
    min_sep = _mm_to_map_units(min_sep_mm, scale_denom, sr, allow_geo_approx, approx_lat_deg=approx_lat)
    corner_extra = _mm_to_map_units(corner_extra_mm, scale_denom, sr, allow_geo_approx, approx_lat_deg=approx_lat)

    xmin, ymin, xmax, ymax = edges["XMIN"], edges["YMIN"], edges["XMAX"], edges["YMAX"]

    # Build values for projected grid
    xs = _compute_values(xmin, xmax, spacing_proj, max_count=max_ticks)
    ys = _compute_values(ymin, ymax, spacing_proj, max_count=max_ticks)

    # Create/ensure FCs
    fields_tick = [("SHEET", "TEXT", 64), ("TYPE", "TEXT", 12), ("SIDE", "TEXT", 8), ("VAL", "DOUBLE", 0), ("IS_MAJOR", "SHORT", 0)]
    fields_lbl  = [("SHEET", "TEXT", 64), ("TYPE", "TEXT", 12), ("SIDE", "TEXT", 8), ("TEXT", "TEXT", 64), ("ROT", "DOUBLE", 0)]
    fields_gl   = [("SHEET", "TEXT", 64), ("TYPE", "TEXT", 12), ("VAL", "DOUBLE", 0), ("AXIS", "TEXT", 4)]

    fc_ticks = _ensure_fc(fds, "TKS_Ticks", "POLYLINE", sr, fields_tick)
    fc_lbls  = _ensure_fc(fds, "LBL_Labels", "POINT", sr, fields_lbl)
    fc_glines = None
    if create_grid_lines:
        fc_glines = _ensure_fc(fds, "GLN_GridLines", "POLYLINE", sr, fields_gl)

    # Cleanup old features of this sheet (fast)
    if cleanup_sheet and (not dry_run):
        _delete_by_sheet(fc_ticks, sheet_key)
        _delete_by_sheet(fc_lbls, sheet_key)
        if fc_glines:
            _delete_by_sheet(fc_glines, sheet_key)

    if dry_run:
        _lock_write_line(log_file, u"[{0}] DRY RUN sheet={1} ticksX={2} ticksY={3}".format(_now_str(), sheet_key, len(xs), len(ys)))
        return {"ticks_proj": len(xs) + len(ys), "labels_proj": len(xs) + len(ys), "grid_lines": 0}

    # Insert cursors
    tick_cur = arcpy.da.InsertCursor(fc_ticks, ["SHAPE@", "SHEET", "TYPE", "SIDE", "VAL", "IS_MAJOR"])
    lbl_cur  = arcpy.da.InsertCursor(fc_lbls,  ["SHAPE@", "SHEET", "TYPE", "SIDE", "TEXT", "ROT"])
    gl_cur = None
    if fc_glines:
        gl_cur = arcpy.da.InsertCursor(fc_glines, ["SHAPE@", "SHEET", "TYPE", "VAL", "AXIS"])

    try:
        # progressor
        try:
            total = len(xs) + len(ys)
            arcpy.SetProgressor("step", "Creating projected ticks/labels...", 0, total, 1)
        except:
            pass

        # Projected ticks along X (bottom/top)
        for i, x in enumerate(xs):
            try:
                # display points (axis-aligned)
                pB = _display_to_data_xy(x, ymin, edges)
                pT = _display_to_data_xy(x, ymax, edges)

                # Bottom tick line (downwards in display => -Y)
                pB2d = _display_to_data_xy(x, ymin - tick_len, edges)
                geom = arcpy.Polyline(arcpy.Array([arcpy.Point(pB[0], pB[1]), arcpy.Point(pB2d[0], pB2d[1])]), sr)
                _insert_tick(tick_cur, sheet_key, "PROJ", "BOTTOM", x, geom, 1)

                # Label point below tick end
                pLb = _display_to_data_xy(x, ymin - (tick_len + lab_off), edges)
                txt = _format_proj(x, divisor_proj, fmt_mode=proj_label_mode, unit_suffix=proj_unit_suffix, pad3=proj_pad3)
                _insert_label(lbl_cur, sheet_key, "PROJ", "BOTTOM", txt, arcpy.PointGeometry(arcpy.Point(pLb[0], pLb[1]), sr), 0.0)

                # Top tick line (upwards in display => +Y)
                pT2d = _display_to_data_xy(x, ymax + tick_len, edges)
                geomT = arcpy.Polyline(arcpy.Array([arcpy.Point(pT[0], pT[1]), arcpy.Point(pT2d[0], pT2d[1])]), sr)
                _insert_tick(tick_cur, sheet_key, "PROJ", "TOP", x, geomT, 1)

                pLt = _display_to_data_xy(x, ymax + (tick_len + lab_off), edges)
                _insert_label(lbl_cur, sheet_key, "PROJ", "TOP", txt, arcpy.PointGeometry(arcpy.Point(pLt[0], pLt[1]), sr), 0.0)

                if gl_cur:
                    # vertical grid line within extent
                    p1 = _display_to_data_xy(x, ymin, edges)
                    p2 = _display_to_data_xy(x, ymax, edges)
                    gl = arcpy.Polyline(arcpy.Array([arcpy.Point(p1[0], p1[1]), arcpy.Point(p2[0], p2[1])]), sr)
                    gl_cur.insertRow((gl, sheet_key, "PROJ", float(x), "X"))

            except Exception as e:
                if continue_on_error:
                    _lock_write_line(log_file, u"[{0}] WARN sheet={1} projX tick failed: {2}".format(_now_str(), sheet_key, e))
                else:
                    raise
            try:
                arcpy.SetProgressorPosition()
            except:
                pass

        # Projected ticks along Y (left/right)
        for j, y in enumerate(ys):
            try:
                pL = _display_to_data_xy(xmin, y, edges)
                pR = _display_to_data_xy(xmax, y, edges)

                # Left tick (to left in display => -X)
                pL2d = _display_to_data_xy(xmin - tick_len, y, edges)
                geom = arcpy.Polyline(arcpy.Array([arcpy.Point(pL[0], pL[1]), arcpy.Point(pL2d[0], pL2d[1])]), sr)
                _insert_tick(tick_cur, sheet_key, "PROJ", "LEFT", y, geom, 1)

                pLl = _display_to_data_xy(xmin - (tick_len + lab_off), y, edges)
                txt = _format_proj(y, divisor_proj, fmt_mode=proj_label_mode, unit_suffix=proj_unit_suffix, pad3=proj_pad3)
                _insert_label(lbl_cur, sheet_key, "PROJ", "LEFT", txt, arcpy.PointGeometry(arcpy.Point(pLl[0], pLl[1]), sr), 0.0)

                # Right tick (+X)
                pR2d = _display_to_data_xy(xmax + tick_len, y, edges)
                geomR = arcpy.Polyline(arcpy.Array([arcpy.Point(pR[0], pR[1]), arcpy.Point(pR2d[0], pR2d[1])]), sr)
                _insert_tick(tick_cur, sheet_key, "PROJ", "RIGHT", y, geomR, 1)

                pLr = _display_to_data_xy(xmax + (tick_len + lab_off), y, edges)
                _insert_label(lbl_cur, sheet_key, "PROJ", "RIGHT", txt, arcpy.PointGeometry(arcpy.Point(pLr[0], pLr[1]), sr), 0.0)

                if gl_cur:
                    p1 = _display_to_data_xy(xmin, y, edges)
                    p2 = _display_to_data_xy(xmax, y, edges)
                    gl = arcpy.Polyline(arcpy.Array([arcpy.Point(p1[0], p1[1]), arcpy.Point(p2[0], p2[1])]), sr)
                    gl_cur.insertRow((gl, sheet_key, "PROJ", float(y), "Y"))

            except Exception as e:
                if continue_on_error:
                    _lock_write_line(log_file, u"[{0}] WARN sheet={1} projY tick failed: {2}".format(_now_str(), sheet_key, e))
                else:
                    raise
            try:
                arcpy.SetProgressorPosition()
            except:
                pass
        try:
            arcpy.ResetProgressor()
        except:
            pass

        # Graticule (Lat/Lon)
        res = {"ticks_proj": len(xs) + len(ys), "labels_proj": len(xs) + len(ys), "ticks_geo": 0, "labels_geo": 0, "grid_lines": (len(xs)+len(ys)) if gl_cur else 0}
        if enable_graticule and graticule_interval_deg and graticule_interval_deg > 0:
            geo_sr = arcpy.SpatialReference(int(geo_wkid))

            # Build edge samples for TRUE_INTERVAL mode
            if graticule_mode == u"TRUE_INTERVAL":
                # determine sample resolution based on extent size and interval
                # target about 10 samples per interval, clamp
                w = abs(xmax - xmin)
                h = abs(ymax - ymin)
                # avoid huge arrays
                n_base = int(max(80, min(2000, (w / float(spacing_proj)) * 50)))  # based on proj spacing
                # For each edge we need arrays
                def _densify(p0, p1, n):
                    pts = []
                    for k in range(n):
                        t = float(k) / float(n - 1)
                        pts.append((p0[0] + (p1[0]-p0[0])*t, p0[1] + (p1[1]-p0[1])*t))
                    return pts

                # Edges in display coords then map to data coords
                display_edges = {
                    "BOTTOM": ((xmin, ymin), (xmax, ymin)),
                    "TOP": ((xmin, ymax), (xmax, ymax)),
                    "LEFT": ((xmin, ymin), (xmin, ymax)),
                    "RIGHT": ((xmax, ymin), (xmax, ymax)),
                }

                # For each edge: densify in display, convert to data, project to geo.
                edge_geo = {}
                for side, (p0d, p1d) in display_edges.items():
                    pts_disp = _densify(p0d, p1d, n_base)
                    pts_data = [_display_to_data_xy(p[0], p[1], edges) for p in pts_disp]
                    ll = _project_points(pts_data, sr, geo_sr, continue_on_error=True)
                    edge_geo[side] = {"disp": pts_disp, "data": pts_data, "lon": [p[0] for p in ll], "lat": [p[1] for p in ll]}

                # Determine targets from min/max lon/lat on edges
                # Compute min/max using existing samples (ignore None)
                def _minmax(arr):
                    v = [a for a in arr if a is not None]
                    if not v:
                        return (None, None)
                    return (min(v), max(v))

                lon_all = []
                lat_all = []
                for side in edge_geo:
                    lon_all += [a for a in edge_geo[side]["lon"] if a is not None]
                    lat_all += [a for a in edge_geo[side]["lat"] if a is not None]
                if lon_all and lat_all:
                    lon_min, lon_max = min(lon_all), max(lon_all)
                    lat_min, lat_max = min(lat_all), max(lat_all)
                    # target values at interval
                    lon_targets = _compute_values(lon_min, lon_max, graticule_interval_deg, max_count=max_ticks)
                    lat_targets = _compute_values(lat_min, lat_max, graticule_interval_deg, max_count=max_ticks)

                    # crossing helper
                    def _find_cross(target, vals, coords_data, coords_disp):
                        for k in range(len(vals) - 1):
                            v0, v1 = vals[k], vals[k+1]
                            if v0 is None or v1 is None:
                                continue
                            if (v0 <= target <= v1) or (v1 <= target <= v0):
                                if v1 == v0:
                                    return (coords_data[k], coords_disp[k])
                                t = (target - v0) / float(v1 - v0)
                                xd = coords_data[k][0] + (coords_data[k+1][0] - coords_data[k][0]) * t
                                yd = coords_data[k][1] + (coords_data[k+1][1] - coords_data[k][1]) * t
                                xdp = coords_disp[k][0] + (coords_disp[k+1][0] - coords_disp[k][0]) * t
                                ydp = coords_disp[k][1] + (coords_disp[k+1][1] - coords_disp[k][1]) * t
                                return ((xd, yd), (xdp, ydp))
                        return None

                    # Create geo labels/ticks at crossings:
                    # Longitude targets: bottom/top edges
                    for lon in lon_targets:
                        for side in ["BOTTOM", "TOP"]:
                            rec = _find_cross(lon, edge_geo[side]["lon"], edge_geo[side]["data"], edge_geo[side]["disp"])
                            if not rec:
                                continue
                            (xd, yd), (xdisp, ydisp) = rec
                            # tick: small line outward
                            if side == "BOTTOM":
                                p2 = _display_to_data_xy(xdisp, ydisp - tick_len, edges)
                                labp = _display_to_data_xy(xdisp, ydisp - (tick_len + grat_off), edges)
                            else:
                                p2 = _display_to_data_xy(xdisp, ydisp + tick_len, edges)
                                labp = _display_to_data_xy(xdisp, ydisp + (tick_len + grat_off), edges)
                            geom = arcpy.Polyline(arcpy.Array([arcpy.Point(xd, yd), arcpy.Point(p2[0], p2[1])]), sr)
                            _insert_tick(tick_cur, sheet_key, "GEO", side, lon, geom, 1)
                            txt = _format_dms(lon, is_lon=True, show_hemi=graticule_show_hemi)
                            _insert_label(lbl_cur, sheet_key, "GEO", side, txt, arcpy.PointGeometry(arcpy.Point(labp[0], labp[1]), sr), 0.0)
                            res["ticks_geo"] += 1
                            res["labels_geo"] += 1

                    # Latitude targets: left/right edges
                    for lat in lat_targets:
                        for side in ["LEFT", "RIGHT"]:
                            rec = _find_cross(lat, edge_geo[side]["lat"], edge_geo[side]["data"], edge_geo[side]["disp"])
                            if not rec:
                                continue
                            (xd, yd), (xdisp, ydisp) = rec
                            if side == "LEFT":
                                p2 = _display_to_data_xy(xdisp - tick_len, ydisp, edges)
                                labp = _display_to_data_xy(xdisp - (tick_len + grat_off), ydisp, edges)
                            else:
                                p2 = _display_to_data_xy(xdisp + tick_len, ydisp, edges)
                                labp = _display_to_data_xy(xdisp + (tick_len + grat_off), ydisp, edges)
                            geom = arcpy.Polyline(arcpy.Array([arcpy.Point(xd, yd), arcpy.Point(p2[0], p2[1])]), sr)
                            _insert_tick(tick_cur, sheet_key, "GEO", side, lat, geom, 1)
                            txt = _format_dms(lat, is_lon=False, show_hemi=graticule_show_hemi)
                            _insert_label(lbl_cur, sheet_key, "GEO", side, txt, arcpy.PointGeometry(arcpy.Point(labp[0], labp[1]), sr), 0.0)
                            res["ticks_geo"] += 1
                            res["labels_geo"] += 1

                else:
                    _add_warn("Graticule enabled but projection to geo produced no valid samples.")
            else:
                # SAMPLE_AT_PROJECTED_TICKS:
                geo_sr = arcpy.SpatialReference(int(geo_wkid))
                # sample at projected tick anchor points (fast)
                # bottom/top longitude, left/right latitude
                for x in xs:
                    for side in ["BOTTOM","TOP"]:
                        y = ymin if side=="BOTTOM" else ymax
                        xd, yd = _display_to_data_xy(x, y, edges)
                        ll = _project_points([(xd, yd)], sr, geo_sr, continue_on_error=True)[0]
                        lon = ll[0]
                        if lon is None:
                            continue
                        if side=="BOTTOM":
                            labp = _display_to_data_xy(x, y - (tick_len + grat_off), edges)
                        else:
                            labp = _display_to_data_xy(x, y + (tick_len + grat_off), edges)
                        txt = _format_dms(lon, is_lon=True, show_hemi=graticule_show_hemi)
                        _insert_label(lbl_cur, sheet_key, "GEO", side, txt, arcpy.PointGeometry(arcpy.Point(labp[0], labp[1]), sr), 0.0)
                        res["labels_geo"] += 1
                for y in ys:
                    for side in ["LEFT","RIGHT"]:
                        x = xmin if side=="LEFT" else xmax
                        xd, yd = _display_to_data_xy(x, y, edges)
                        ll = _project_points([(xd, yd)], sr, geo_sr, continue_on_error=True)[0]
                        lat = ll[1]
                        if lat is None:
                            continue
                        if side=="LEFT":
                            labp = _display_to_data_xy(x - (tick_len + grat_off), y, edges)
                        else:
                            labp = _display_to_data_xy(x + (tick_len + grat_off), y, edges)
                        txt = _format_dms(lat, is_lon=False, show_hemi=graticule_show_hemi)
                        _insert_label(lbl_cur, sheet_key, "GEO", side, txt, arcpy.PointGeometry(arcpy.Point(labp[0], labp[1]), sr), 0.0)
                        res["labels_geo"] += 1

        # De-overlap corners (GEO labels vs PROJ labels) best-effort
        if deoverlap_corners and (min_sep > 0) and (corner_extra > 0):
            try:
                # collect proj label points per sheet
                proj_pts = []
                with arcpy.da.SearchCursor(fc_lbls, ["SHAPE@XY", "TYPE"], "SHEET = '{0}'".format(sheet_key.replace("'", "''"))) as sc:
                    for (xy, typ) in sc:
                        if typ == "PROJ":
                            proj_pts.append(xy)

                if proj_pts:
                    lyr = "lbl_geo_" + uuid.uuid4().hex[:6]
                    arcpy.MakeFeatureLayer_management(fc_lbls, lyr, "SHEET = '{0}' AND TYPE = 'GEO'".format(sheet_key.replace("'", "''")))
                    with arcpy.da.UpdateCursor(lyr, ["SHAPE@XY", "SIDE", "TEXT", "TYPE"]) as uc:
                        for row in uc:
                            (x0, y0) = row[0]
                            # use display coords to detect corners
                            xdisp, ydisp = _data_to_display_xy(x0, y0, edges)
                            # near which corner?
                            corner = None
                            if abs(xdisp - xmin) < min_sep and abs(ydisp - ymin) < min_sep: corner = "BL"
                            elif abs(xdisp - xmax) < min_sep and abs(ydisp - ymin) < min_sep: corner = "BR"
                            elif abs(xdisp - xmin) < min_sep and abs(ydisp - ymax) < min_sep: corner = "TL"
                            elif abs(xdisp - xmax) < min_sep and abs(ydisp - ymax) < min_sep: corner = "TR"
                            if not corner:
                                continue
                            # if too close to any proj label -> shift outward along both axes
                            too_close = False
                            for (xp, yp) in proj_pts:
                                if (abs(xp - x0) < min_sep) and (abs(yp - y0) < min_sep):
                                    too_close = True
                                    break
                            if not too_close:
                                continue

                            # shift in display outward then transform back
                            dx = (-corner_extra if "L" in corner else corner_extra)
                            dy = (-corner_extra if "B" in corner else corner_extra)
                            xdisp2 = xdisp + dx
                            ydisp2 = ydisp + dy
                            x2, y2 = _display_to_data_xy(xdisp2, ydisp2, edges)
                            row[0] = (x2, y2)
                            uc.updateRow(row)
                    arcpy.Delete_management(lyr)
            except Exception as e:
                if continue_on_error:
                    _add_warn("Corner de-overlap failed: {0}".format(e))
                else:
                    raise

        _lock_write_line(log_file, u"[{0}] OK SMART_FEATURE sheet={1} proj={2}+{3} geo={4}/{5} glines={6}".format(
            _now_str(), sheet_key, len(xs), len(ys), res.get("ticks_geo",0), res.get("labels_geo",0), res.get("grid_lines",0)
        ))
        return res

    finally:
        try:
            del tick_cur
        except:
            pass
        try:
            del lbl_cur
        except:
            pass
        try:
            if gl_cur:
                del gl_cur
        except:
            pass


# --------------------------------------------------------------------------------------
# ESRI_XML engine wrapper
# --------------------------------------------------------------------------------------

def _run_esri_make_grids(template_xml, aoi, fds, out_layer_name, grid_name=None,
                         refscale=None, rotation=None, mask_mm=None, primary_sr=None,
                         configure_layout=False):
    """
    arcpy.cartography.MakeGridsAndGraticulesLayer wrapper.
    """
    _require_cartography_level()

    if not hasattr(arcpy, "cartography") or (not hasattr(arcpy.cartography, "MakeGridsAndGraticulesLayer")):
        _raise("Cartography toolbox not available in this ArcMap installation.")

    # mask_size parameter expects linear unit
    mask_val = None
    if mask_mm and _safe_float(mask_mm, 0.0) > 0:
        mask_val = "{0} Millimeters".format(_safe_float(mask_mm))

    # configure layout only works in layout view (per docs)
    conf = "CONFIGURELAYOUT" if configure_layout else "NO_CONFIGURELAYOUT"

    # Ensure env cartographicCoordinateSystem (if provided)
    if primary_sr is not None:
        try:
            arcpy.env.cartographicCoordinateSystem = primary_sr
        except:
            pass

    args = [template_xml, aoi, fds, out_layer_name]

    # optional args following tool signature (ArcMap docs):
    # name, refscale, rotation, mask_size, xy_tolerance, primary_cs, configure_layout, ancillary_cs1..4
    if grid_name not in [None, ""]:
        args.append(grid_name)
    if refscale not in [None, ""]:
        args.append(float(refscale))
    if rotation not in [None, ""]:
        args.append(float(rotation))
    if mask_val is not None:
        args.append(mask_val)
    # xy_tolerance (optional) - leave default
    args.append(None)
    if primary_sr is not None:
        args.append(primary_sr)
    else:
        args.append(None)
    args.append(conf)

    try:
        return arcpy.cartography.MakeGridsAndGraticulesLayer(*args)
    except arcpy.ExecuteError:
        _raise("MakeGridsAndGraticulesLayer failed: {0}".format(arcpy.GetMessages(2)))
    except Exception as e:
        _raise("MakeGridsAndGraticulesLayer failed: {0}".format(e))


# --------------------------------------------------------------------------------------
# Toolbox classes
# --------------------------------------------------------------------------------------

class Toolbox(object):
    def __init__(self):
        self.label = u"Cartographic Automation (ArcMap) - Plugin 07"
        self.alias = u"plugin07_batch_grid"
        self.tools = [BatchGridBuilder07]


class BatchGridBuilder07(object):
    def __init__(self):
        self.label = u"07) Batch Grid Builder (ArcMap 2.7) - ESRI_XML + SMART_FEATURE"
        self.description = u"Batch build grids/graticules for multiple sheets, with corner de-overlap and export."
        self.canRunInBackground = False

    def getParameterInfo(self):
        p = []

        # Mode
        p0 = arcpy.Parameter("Mode", "mode", "GPString", "Required", "Input")
        p0.filter.type = "ValueList"
        p0.filter.list = ["FOLDER_OF_MXDS", "AOI_LAYER_IN_CURRENT_MXD"]
        p0.value = "FOLDER_OF_MXDS"
        p.append(p0)

        p1 = arcpy.Parameter("MXD Folder (Mode=FOLDER_OF_MXDS)", "mxd_folder", "DEFolder", "Optional", "Input")
        p.append(p1)

        p2 = arcpy.Parameter("Include Subfolders", "recursive", "GPBoolean", "Optional", "Input")
        p2.value = False
        p.append(p2)

        p3 = arcpy.Parameter("Data Frame Name (optional; default=first DF)", "data_frame_name", "GPString", "Optional", "Input")
        p.append(p3)

        p4 = arcpy.Parameter("AOI Layer (Mode=AOI_LAYER_IN_CURRENT_MXD)", "aoi_layer", "GPFeatureLayer", "Optional", "Input")
        p.append(p4)

        p5 = arcpy.Parameter("AOI Name Field (Mode=AOI_LAYER_IN_CURRENT_MXD)", "aoi_name_field", "Field", "Optional", "Input")
        p5.parameterDependencies = [p4.name]
        p.append(p5)

        # Engine
        p6 = arcpy.Parameter("Engine", "engine", "GPString", "Required", "Input")
        p6.filter.type = "ValueList"
        p6.filter.list = ["SMART_FEATURE", "ESRI_XML"]
        p6.value = "SMART_FEATURE"
        p.append(p6)

        # ESRI XML template
        p7 = arcpy.Parameter("Grid Template XML (Engine=ESRI_XML)", "grid_xml", "DEFile", "Optional", "Input")
        p7.filter.list = ["xml"]
        p.append(p7)

        # Output workspace
        p8 = arcpy.Parameter("Output Workspace (.gdb or folder)", "out_ws", "DEWorkspace", "Required", "Input")
        p.append(p8)

        p9 = arcpy.Parameter("Feature Dataset Name (for SMART_FEATURE outputs)", "fds_name", "GPString", "Optional", "Input")
        p9.value = "Grids"
        p.append(p9)

        # Reference scale
        p10 = arcpy.Parameter("Reference Scale Denominator (e.g., 25000)", "refscale_denom", "GPDouble", "Required", "Input")
        p10.value = 25000
        p.append(p10)

        # Rotation respect
        p11r = arcpy.Parameter("(SMART_FEATURE) Respect Data Frame Rotation", "respect_df_rotation", "GPBoolean", "Optional", "Input")
        p11r.value = True
        p.append(p11r)

        # SMART_FEATURE options
        p14 = arcpy.Parameter("(SMART_FEATURE) Projected Interval (map units, e.g., 1000 for UTM)", "spacing_proj", "GPDouble", "Optional", "Input")
        p14.value = 1000.0
        p.append(p14)

        p15 = arcpy.Parameter("(SMART_FEATURE) Projected Label Divisor (1000 => show 295 instead of 295000)", "divisor_proj", "GPDouble", "Optional", "Input")
        p15.value = 1000.0
        p.append(p15)

        p16 = arcpy.Parameter("(SMART_FEATURE) Tick Length (mm)", "tick_mm", "GPDouble", "Optional", "Input")
        p16.value = 1.5
        p.append(p16)

        p17 = arcpy.Parameter("(SMART_FEATURE) Label Offset (mm)", "label_mm", "GPDouble", "Optional", "Input")
        p17.value = 3.0
        p.append(p17)

        p18a = arcpy.Parameter("(SMART_FEATURE) Create Grid Lines (full)", "create_grid_lines", "GPBoolean", "Optional", "Input")
        p18a.value = True
        p.append(p18a)

        # Label formatting
        p18b = arcpy.Parameter("(SMART_FEATURE) Projected Label Format", "proj_label_mode", "GPString", "Optional", "Input")
        p18b.filter.type = "ValueList"
        p18b.filter.list = ["INT", "FLOAT"]
        p18b.value = "INT"
        p.append(p18b)

        p18c = arcpy.Parameter("(SMART_FEATURE) Projected Label Unit Suffix (e.g., ' m' or ' km')", "proj_unit_suffix", "GPString", "Optional", "Input")
        p18c.value = ""
        p.append(p18c)

        p18d = arcpy.Parameter("(SMART_FEATURE) Projected Label Pad to 3 digits (e.g., 295)", "proj_pad3", "GPBoolean", "Optional", "Input")
        p18d.value = True
        p.append(p18d)

        # Graticule
        p19 = arcpy.Parameter("(SMART_FEATURE) Enable Graticule (Lat/Lon)", "enable_graticule", "GPBoolean", "Optional", "Input")
        p19.value = True
        p.append(p19)
        p20 = arcpy.Parameter("(SMART_FEATURE) Graticule Interval (minutes) (e.g., 2.5 => 2m30s)", "grat_minutes", "GPDouble", "Optional", "Input")
        p20.value = 2.5
        p.append(p20)

        p20m = arcpy.Parameter("(SMART_FEATURE) Graticule Mode", "grat_mode", "GPString", "Optional", "Input")
        p20m.filter.type = "ValueList"
        p20m.filter.list = ["TRUE_INTERVAL", "SAMPLE_AT_PROJECTED_TICKS"]
        p20m.value = "TRUE_INTERVAL"
        p.append(p20m)

        p21 = arcpy.Parameter("(SMART_FEATURE) Geographic WKID for Graticule (default=4326)", "geo_wkid", "GPLong", "Optional", "Input")
        p21.value = 4326
        p.append(p21)

        p22 = arcpy.Parameter("(SMART_FEATURE) Graticule Label Offset (mm)", "grat_label_mm", "GPDouble", "Optional", "Input")
        p22.value = 3.5
        p.append(p22)

        p23 = arcpy.Parameter("(SMART_FEATURE) Show Hemisphere (N/S/E/W) in DMS", "grat_hemi", "GPBoolean", "Optional", "Input")
        p23.value = False
        p.append(p23)

        # De-overlap
        p24 = arcpy.Parameter("(SMART_FEATURE) Auto De-overlap at Corners", "deoverlap_corners", "GPBoolean", "Optional", "Input")
        p24.value = True
        p.append(p24)

        p25 = arcpy.Parameter("(SMART_FEATURE) Minimum Separation (mm) for corner de-overlap", "min_sep_mm", "GPDouble", "Optional", "Input")
        p25.value = 1.5
        p.append(p25)

        p26 = arcpy.Parameter("(SMART_FEATURE) Corner Extra Shift (mm) for graticule labels", "corner_extra_mm", "GPDouble", "Optional", "Input")
        p26.value = 1.0
        p.append(p26)

        # Safety
        p27 = arcpy.Parameter("Continue On Error (best-effort batch)", "continue_on_error", "GPBoolean", "Optional", "Input")
        p27.value = True
        p.append(p27)

        p28 = arcpy.Parameter("Max ticks/gridlines per edge (safety)", "max_ticks", "GPLong", "Optional", "Input")
        p28.value = 20000
        p.append(p28)

        # Cleanup
        p29 = arcpy.Parameter("Clean old grids for sheet before creating new", "cleanup_sheet", "GPBoolean", "Optional", "Input")
        p29.value = True
        p.append(p29)

        p30 = arcpy.Parameter("Dry Run (no outputs written)", "dry_run", "GPBoolean", "Optional", "Input")
        p30.value = False
        p.append(p30)

        # Symbology
        p31 = arcpy.Parameter("Apply Symbology From Layerfile (optional)", "symbology_layerfile", "DEFile", "Optional", "Input")
        p31.filter.list = ["lyr", "lyrx"]
        p.append(p31)

        # ESRI_XML options
        p32 = arcpy.Parameter("(ESRI_XML) Mask Size (mm)", "mask_mm", "GPDouble", "Optional", "Input")
        p32.value = 5.0
        p.append(p32)

        p33 = arcpy.Parameter("(ESRI_XML) XML AutoFix (best-effort)", "xml_autofix", "GPBoolean", "Optional", "Input")
        p33.value = True
        p.append(p33)

        p34 = arcpy.Parameter("(ESRI_XML) Ancillary Offset Delta", "xml_delta_anc", "GPDouble", "Optional", "Input")
        p34.value = 2.0
        p.append(p34)

        # Export / persistence (FOLDER_OF_MXDS)
        p35 = arcpy.Parameter("Add outputs to MXD", "add_to_mxd", "GPBoolean", "Optional", "Input")
        p35.value = True
        p.append(p35)

        p36 = arcpy.Parameter("Save MXD copy (Mode=FOLDER_OF_MXDS)", "save_mxd_copy", "GPBoolean", "Optional", "Input")
        p36.value = True
        p.append(p36)

        p37 = arcpy.Parameter("Output MXD Folder (if Save MXD copy = True)", "out_mxd_folder", "DEFolder", "Optional", "Input")
        p.append(p37)

        p38 = arcpy.Parameter("Export PDF (Mode=FOLDER_OF_MXDS)", "export_pdf", "GPBoolean", "Optional", "Input")
        p38.value = False
        p.append(p38)

        p39 = arcpy.Parameter("Output PDF Folder (if Export PDF = True)", "out_pdf_folder", "DEFolder", "Optional", "Input")
        p.append(p39)

        p40 = arcpy.Parameter("PDF Resolution (DPI)", "pdf_dpi", "GPLong", "Optional", "Input")
        p40.value = 300
        p.append(p40)

        p41 = arcpy.Parameter("Export PNG", "export_png", "GPBoolean", "Optional", "Input")
        p41.value = False
        p.append(p41)

        p42 = arcpy.Parameter("Export JPEG", "export_jpeg", "GPBoolean", "Optional", "Input")
        p42.value = False
        p.append(p42)

        p43 = arcpy.Parameter("Output Image Folder (PNG/JPEG)", "out_img_folder", "DEFolder", "Optional", "Input")
        p.append(p43)

        p44 = arcpy.Parameter("Image Resolution (DPI)", "img_dpi", "GPLong", "Optional", "Input")
        p44.value = 300
        p.append(p44)

        p45 = arcpy.Parameter("Log file (optional). If empty, tool creates a per-run log in output folder.", "log_file", "DEFile", "Optional", "Input")
        p.append(p45)

        return p
    
def updateParameters(self, parameters):
    mode = parameters[0].valueAsText
    engine = parameters[6].valueAsText

    # Mode toggles
    parameters[1].enabled = (mode == "FOLDER_OF_MXDS")  # mxd_folder
    parameters[2].enabled = (mode == "FOLDER_OF_MXDS")  # recursive
    parameters[4].enabled = (mode == "AOI_LAYER_IN_CURRENT_MXD")  # aoi_layer
    parameters[5].enabled = (mode == "AOI_LAYER_IN_CURRENT_MXD")  # aoi_name_field

    # Engine toggles
    is_xml = (engine == "ESRI_XML")
    parameters[7].enabled = is_xml  # grid_xml

    # SMART_FEATURE-only params: indices 11..33
    for idx in range(11, 34):
        parameters[idx].enabled = (not is_xml)

    # ESRI_XML-only params: indices 34..36
    for idx in [34, 35, 36]:
        parameters[idx].enabled = is_xml

    # Folder-batch exports (only meaningful in FOLDER_OF_MXDS):
    # Keep "Add outputs to MXD" (37) and "Log file" (47) enabled in both modes.
    parameters[37].enabled = True
    for idx in range(38, 47):
        parameters[idx].enabled = (mode == "FOLDER_OF_MXDS")
    parameters[47].enabled = True

    # Dependent folders for folder mode
    parameters[39].enabled = (mode == "FOLDER_OF_MXDS") and bool(parameters[38].value)  # out_mxd_folder
    parameters[41].enabled = (mode == "FOLDER_OF_MXDS") and bool(parameters[40].value)  # out_pdf_folder
    parameters[45].enabled = (mode == "FOLDER_OF_MXDS") and (bool(parameters[43].value) or bool(parameters[44].value))  # out_img_folder

    return

    def execute(self, parameters, messages):

        mode = parameters[0].valueAsText
        mxd_folder = parameters[1].valueAsText
        recursive = bool(parameters[2].value)
        df_name = parameters[3].valueAsText
        aoi_layer = parameters[4].valueAsText
        aoi_name_field = parameters[5].valueAsText
        engine = parameters[6].valueAsText
        grid_xml = parameters[7].valueAsText
        out_ws = parameters[8].valueAsText
        fds_name = parameters[9].valueAsText
        refscale = _safe_float(parameters[10].value, 0.0)

        # SMART_FEATURE params
        respect_rot = bool(parameters[11].value)
        spacing_proj = _safe_float(parameters[12].value, 0.0)
        divisor_proj = _safe_float(parameters[13].value, 1.0)
        tick_mm = _safe_float(parameters[14].value, 1.5)
        label_mm = _safe_float(parameters[15].value, 3.0)
        create_grid_lines = bool(parameters[16].value)
        proj_label_mode = parameters[17].valueAsText or "INT"
        proj_unit_suffix = parameters[18].valueAsText or ""
        proj_pad3 = bool(parameters[19].value)

        enable_grat = bool(parameters[20].value)
        grat_minutes = _safe_float(parameters[21].value, 0.0)
        grat_mode = parameters[22].valueAsText or "TRUE_INTERVAL"
        geo_wkid = _safe_int(parameters[23].value, 4326)
        grat_label_mm = _safe_float(parameters[24].value, 3.5)
        grat_hemi = bool(parameters[25].value)

        deover = bool(parameters[26].value)
        min_sep_mm = _safe_float(parameters[27].value, 1.5)
        corner_extra_mm = _safe_float(parameters[28].value, 1.0)

        continue_on_error = bool(parameters[29].value)
        max_ticks = _safe_int(parameters[30].value, 20000)
        cleanup_sheet = bool(parameters[31].value)
        dry_run = bool(parameters[32].value)
        symbology_layerfile = parameters[33].valueAsText

        # ESRI_XML params
        mask_mm = _safe_float(parameters[34].value, 5.0)
        xml_autofix = bool(parameters[35].value)
        xml_delta_anc = _safe_float(parameters[36].value, 2.0)

        # exports (FOLDER_OF_MXDS)
        add_to_mxd = bool(parameters[37].value)
        save_mxd_copy = bool(parameters[38].value)
        out_mxd_folder = parameters[39].valueAsText
        export_pdf = bool(parameters[40].value)
        out_pdf_folder = parameters[41].valueAsText
        pdf_dpi = _safe_int(parameters[42].value, 300)

        export_png = bool(parameters[43].value)
        export_jpeg = bool(parameters[44].value)
        out_img_folder = parameters[45].valueAsText
        img_dpi = _safe_int(parameters[46].value, 300)

        log_file = parameters[47].valueAsText


        # Prepare output
        gdb = _ensure_file_gdb(out_ws, "grid_output.gdb")

        if not log_file:
            # per-run log in output folder
            lf = os.path.join(os.path.dirname(gdb), "grid_batch_{0}.log".format(time.strftime("%Y%m%d_%H%M%S")))
            log_file = lf

        _lock_write_line(log_file, u"==== BatchGridBuilder07 start {0} engine={1} mode={2} ====".format(_now_str(), engine, mode))

        # Validate refscale
        if refscale <= 0:
            _raise("Reference scale denominator must be > 0.")

        # Mode logic
        if mode == "FOLDER_OF_MXDS":
            if not mxd_folder or not os.path.isdir(mxd_folder):
                _raise("MXD folder not found.")
            mxds = _list_mxds(mxd_folder, recursive)
            if not mxds:
                _raise("No MXD files found in folder.")
        else:
            mxds = [None]  # current MXD context

        # ESRI_XML: validate template
        if engine == "ESRI_XML":
            if not grid_xml or (not os.path.isfile(grid_xml)):
                _raise("Grid Template XML not found.")
            # best-effort patch
            if xml_autofix:
                temp_dir = os.path.join(arcpy.env.scratchFolder or os.path.dirname(gdb), "_grid_xml_patch")
                grid_xml = _xml_autofix_temp(grid_xml, temp_dir, delta_primary=0.0, delta_ancillary=xml_delta_anc, only_ancillary=True, verbose=True)

        # Create FDS for SMART_FEATURE
        fds = None

        # Process each MXD
        for idx_mxd, mxd_path in enumerate(mxds):
            mxd = None
            try:
                if mode == "FOLDER_OF_MXDS":
                    mxd = arcpy.mapping.MapDocument(mxd_path)
                else:
                    mxd = arcpy.mapping.MapDocument("CURRENT")

                df = _get_df(mxd, df_name)
                sr = df.spatialReference

                if engine == "ESRI_XML" and getattr(sr, "type", None) == "Geographic":
                    _raise("ESRI_XML engine requires a projected coordinate system for the data frame (per ArcGIS graticule/grid requirements).")

                # Prepare FDS for SMART_FEATURE (SR-based)
                if engine == "SMART_FEATURE":
                    fds = _ensure_feature_dataset(gdb, fds_name, sr)

                # Determine extents/sheets
                # Each sheet tuple: (sheet_name, extent, oid)
                sheets = []
                oid_field = None
                if mode == "FOLDER_OF_MXDS":
                    sheet_name = os.path.splitext(os.path.basename(mxd_path))[0]
                    sheets = [(sheet_name, df.extent, None)]
                else:
                    if not aoi_layer:
                        _raise("AOI layer is required for AOI_LAYER_IN_CURRENT_MXD mode.")

                    # Read AOI features (single pass)
                    oid_field = arcpy.Describe(aoi_layer).OIDFieldName
                    name_field = aoi_name_field

                    fields = ["OID@", "SHAPE@"]
                    if name_field:
                        fields.insert(1, name_field)

                    with arcpy.da.SearchCursor(aoi_layer, fields) as sc:
                        for row in sc:
                            oid = row[0]
                            geom = row[-1]
                            ext = geom.extent if geom else None
                            if not ext:
                                continue

                            if name_field:
                                nm = unicode(row[1])
                            else:
                                nm = unicode(oid)

                            sheets.append((nm, ext, oid))

                    if not sheets:
                        _raise("AOI layer has no valid features/extents.")

                # Progressor for batch
                try:
                    arcpy.SetProgressor("step", "Processing sheets...", 0, len(sheets), 1)
                except:
                    pass

                for (sheet_name, ext, oid) in sheets:
                    sheet_key = _validate_name(sheet_name, gdb)
                    _lock_write_line(log_file, u"[{0}] START sheet={1}".format(_now_str(), sheet_key))

                    if engine == "SMART_FEATURE":
                        # graticule interval minutes -> degrees
                        grat_deg = (grat_minutes / 60.0) if grat_minutes and grat_minutes > 0 else 0.0

                        _build_ticks_and_labels_for_extent(
                            df=df, ext=ext, sheet_key=sheet_key, fds=fds, sr=sr,
                            scale_denom=refscale,
                            spacing_proj=spacing_proj,
                            divisor_proj=divisor_proj,
                            tick_len_mm=tick_mm,
                            label_offset_mm=label_mm,
                            create_grid_lines=create_grid_lines,
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
                            continue_on_error=continue_on_error,
                            log_file=log_file,
                            dry_run=dry_run,
                            cleanup_sheet=cleanup_sheet
                        )
                    else:
                        # ESRI_XML engine (MakeGridsAndGraticulesLayer)
                        # AOI can be Extent (string) OR a feature layer with a single selected feature.
                        out_layer_name = "GRID_{0}_{1}".format(sheet_key, (oid if oid is not None else "EXT"))
                        extent_str = "{0} {1} {2} {3}".format(ext.XMin, ext.YMin, ext.XMax, ext.YMax)

                        # Ensure feature dataset exists (tool requires output feature dataset path)
                        fds_path = _ensure_feature_dataset(gdb, fds_name, sr)

                        # If AOI mode: select the current feature and pass the layer as AOI.
                        aoi_arg = extent_str
                        grid_name_arg = sheet_key
                        if (mode != "FOLDER_OF_MXDS") and (oid is not None) and aoi_layer:
                            try:
                                # NEW_SELECTION on the AOI layer (tool uses only selected features)
                                arcpy.SelectLayerByAttribute_management(aoi_layer, "NEW_SELECTION", "{0} = {1}".format(oid_field, oid))
                                aoi_arg = aoi_layer
                                # If a name field exists, ESRI tool can use it as the grid name source
                                if aoi_name_field:
                                    grid_name_arg = aoi_name_field
                            except:
                                # fallback to extent
                                aoi_arg = extent_str
                                grid_name_arg = sheet_key

                        _run_esri_make_grids(
                            template_xml=grid_xml,
                            aoi=aoi_arg,
                            fds=fds_path,
                            out_layer_name=out_layer_name,
                            grid_name=grid_name_arg,
                            refscale=refscale,
                            rotation=(df.rotation if respect_rot else 0.0),
                            mask_mm=mask_mm,
                            primary_sr=sr,
                            configure_layout=False
                        )

                        # Clear selection (avoid impacting user's map)
                        if (mode != "FOLDER_OF_MXDS") and aoi_layer:
                            try:
                                arcpy.SelectLayerByAttribute_management(aoi_layer, "CLEAR_SELECTION")
                            except:
                                pass

                        _add_msg("Created ESRI grid layer: {0}".format(out_layer_name))

                    _lock_write_line(log_file, u"[{0}] DONE sheet={1}".format(_now_str(), sheet_key))
                    try:
                        arcpy.SetProgressorPosition()
                    except:
                        pass


                # Add SMART_FEATURE layers once (avoid duplicates when multiple sheets are processed)
                if (engine == "SMART_FEATURE") and add_to_mxd and (not dry_run):
                    try:
                        _add_smart_outputs_to_mxd(mxd, df, fds, symbology_layerfile=symbology_layerfile)
                    except Exception as e:
                        _add_warn("Failed to add output layers to MXD: {0}".format(e))

                try:
                    arcpy.ResetProgressor()
                except:
                    pass

                # Save/Export per MXD
                if mode == "FOLDER_OF_MXDS" and (not dry_run):
                    base = os.path.splitext(os.path.basename(mxd_path))[0]
                    if save_mxd_copy:
                        if not out_mxd_folder:
                            _raise("Output MXD Folder is required when Save MXD copy is True.")
                        _mkdir(out_mxd_folder)
                        out_mxd = os.path.join(out_mxd_folder, base + "_grid.mxd")
                        mxd.saveACopy(out_mxd)
                        _add_msg("Saved MXD copy: {0}".format(out_mxd))

                    if export_pdf:
                        if not out_pdf_folder:
                            _raise("Output PDF Folder is required when Export PDF is True.")
                        _mkdir(out_pdf_folder)
                        out_pdf = os.path.join(out_pdf_folder, base + ".pdf")
                        arcpy.mapping.ExportToPDF(mxd, out_pdf, resolution=pdf_dpi)
                        _add_msg("Exported PDF: {0}".format(out_pdf))

                    if export_png or export_jpeg:
                        if not out_img_folder:
                            _raise("Output Image Folder is required for PNG/JPEG export.")
                        _mkdir(out_img_folder)
                        if export_png:
                            out_png = os.path.join(out_img_folder, base + ".png")
                            arcpy.mapping.ExportToPNG(mxd, out_png, resolution=img_dpi)
                            _add_msg("Exported PNG: {0}".format(out_png))
                        if export_jpeg:
                            out_jpg = os.path.join(out_img_folder, base + ".jpg")
                            arcpy.mapping.ExportToJPEG(mxd, out_jpg, resolution=img_dpi)
                            _add_msg("Exported JPEG: {0}".format(out_jpg))

            except Exception as e:
                _lock_write_line(log_file, u"[{0}] ERROR mxd={1} err={2}".format(_now_str(), mxd_path or "CURRENT", e))
                if not continue_on_error:
                    raise
                _add_warn("Failed on {0}: {1}".format(mxd_path or "CURRENT", e))
            finally:
                try:
                    if mxd:
                        del mxd
                except:
                    pass
                try:
                    arcpy.ClearWorkspaceCache_management()
                except:
                    pass
                try:
                    gc.collect()
                except:
                    pass

        _lock_write_line(log_file, u"==== BatchGridBuilder07 finished {0} ====".format(_now_str()))
        _add_msg("Done. Log: {0}".format(log_file))
