# -*- coding: utf-8 -*-
"""
Spring Rotation - Comparison Suite  (ArcMap 10.x, Python 2.7 / best-effort ArcGIS Pro)
v4 HARDENED

Original maintainer:
    Ali Mirjafari - 09186441801

What changed vs v3 (and WHY it now processes ALL springs):
    1. SELECTION TRAP FIX (main cause of "works but only on some springs").
       In ArcMap, when a *feature layer* with an active selection is passed to a
       geoprocessing tool, only the SELECTED features are processed. v3 copied the
       input layer directly, so any leftover selection silently limited the run.
       v4 detects a selection (Describe().FIDSet), reports it, and -- when
       "Process ALL features" is on (default) -- resolves the layer to its
       catalogPath so the FULL dataset is always used.
    2. STAGE-BY-STAGE DIAGNOSTICS. The tool now prints how many springs survive
       each stage: input -> (selection) -> AOI -> got NEAR_FID -> rotated OK.
       This makes any future "limit" visible instead of mysterious.
    3. Py2.7 safety: `from __future__ import division`, OID field resolved
       dynamically (no hard-coded !OBJECTID! against shapefiles), defensive encode.
    4. Memory/scale notes for big sheets (32-bit ArcMap ~2 GB in stand-alone,
       ~4 GB in-process). Contour geometry cache stays NEAR_ONLY by default.

Rotation convention: GEOGRAPHIC degrees, 0 = North, clockwise.
"""

from __future__ import division   # Py2.7: make "/" float division everywhere

import os
import math
import hashlib
import traceback
import time
import arcpy


# --------------------------
# Small utilities
# --------------------------

def _to_bytes_utf8(s):
    """Py2/Py3 safe -> utf-8 bytes for hashlib."""
    try:
        if isinstance(s, bytes):
            return s
        return s.encode("utf-8")
    except Exception:
        try:
            return str(s).encode("utf-8")
        except Exception:
            return b"x"

def _wrap360(a):
    if a is None:
        return None
    a = float(a) % 360.0
    if a < 0:
        a += 360.0
    return a

def _azimuth_geo_deg(dx, dy):
    """Geographic azimuth degrees: 0=N, 90=E, 180=S, 270=W."""
    if dx == 0 and dy == 0:
        return None
    ang = math.degrees(math.atan2(dx, dy))  # atan2(E, N)
    if ang < 0:
        ang += 360.0
    return ang

def _safe_delete(path):
    try:
        if path and arcpy.Exists(path):
            arcpy.management.Delete(path)
    except Exception:
        pass

def _desc(fc):
    try:
        return arcpy.Describe(fc)
    except Exception:
        return None

def _desc_sr(fc):
    d = _desc(fc)
    try:
        return d.spatialReference if d else None
    except Exception:
        return None

def _oid_field(fc):
    d = _desc(fc)
    try:
        return d.OIDFieldName if d else "OBJECTID"
    except Exception:
        return "OBJECTID"

def _is_projected_meter(sr):
    try:
        if sr is None:
            return False
        return (sr.type == "Projected") and \
               ("meter" in sr.linearUnitName.lower() or "metre" in sr.linearUnitName.lower())
    except Exception:
        return False

def _utm_sr_for_lonlat(lon, lat):
    zone = int((lon + 180.0) / 6.0) + 1
    epsg = (32600 + zone) if lat >= 0 else (32700 + zone)
    return arcpy.SpatialReference(epsg)

def _pick_work_sr(spr_fc, mode):
    sr_in = _desc_sr(spr_fc)
    if mode == "USE_INPUT":
        return sr_in if sr_in else arcpy.SpatialReference(3857)
    if _is_projected_meter(sr_in):
        return sr_in
    try:
        wgs84 = arcpy.SpatialReference(4326)
        ext = arcpy.Describe(spr_fc).extent
        cx = (ext.XMin + ext.XMax) / 2.0
        cy = (ext.YMin + ext.YMax) / 2.0
        center = arcpy.PointGeometry(arcpy.Point(cx, cy), sr_in)
        center_wgs = center.projectAs(wgs84)
        lon = center_wgs.firstPoint.X
        lat = center_wgs.firstPoint.Y
        return _utm_sr_for_lonlat(lon, lat)
    except Exception:
        return arcpy.SpatialReference(3857)

def _ensure_field(fc, name, ftype, length=255):
    names = [f.name for f in arcpy.ListFields(fc)]
    if name in names:
        return
    if ftype.upper() == "TEXT":
        arcpy.management.AddField(fc, name, ftype, field_length=length)
    else:
        arcpy.management.AddField(fc, name, ftype)

def _color_from_code(code_str):
    try:
        h = hashlib.md5(_to_bytes_utf8(code_str)).hexdigest()
        r = 80 + (int(h[0:2], 16) % 176)
        g = 80 + (int(h[2:4], 16) % 176)
        b = 80 + (int(h[4:6], 16) % 176)
        return {"RGB": [r, g, b, 100]}
    except Exception:
        return {"RGB": [0, 0, 0, 100]}

def _validate_output_name(name, workspace):
    try:
        if workspace:
            return arcpy.ValidateTableName(name, workspace)
    except Exception:
        pass
    if not name:
        return "springs_rotation_suite"
    return "".join([c if (c.isalnum() or c == "_") else "_" for c in name])

def _field_is_numeric(layer, field_name):
    try:
        flds = arcpy.ListFields(layer, field_name)
        if not flds:
            return False
        t = (flds[0].type or "").lower()
        return t in ("double", "single", "integer", "smallinteger")
    except Exception:
        return False

def _shape_type(layer):
    try:
        return (arcpy.Describe(layer).shapeType or "").lower()
    except Exception:
        return ""

def _make_layer_name(prefix, seed):
    try:
        h = hashlib.md5(_to_bytes_utf8(seed or "x")).hexdigest()[:8]
    except Exception:
        h = "tmp"
    return "{0}_{1}".format(prefix, h)

def _get_count(ds):
    try:
        return int(arcpy.management.GetCount(ds)[0])
    except Exception:
        return -1

def _profile_msg(enabled, label, t0):
    if enabled:
        arcpy.AddMessage("[PROFILE] {0}: {1:.3f}s".format(label, time.time() - t0))


# --------------------------
# Selection handling (the important fix)
# --------------------------

def _selection_info(layer):
    """
    Returns (has_selection, n_selected) for a feature layer.
    For a feature class on disk FIDSet is empty/None -> treated as no selection.
    """
    try:
        d = arcpy.Describe(layer)
        fidset = getattr(d, "FIDSet", None)
        if fidset:  # non-empty string like "4; 6; 7"
            ids = [x for x in fidset.replace(",", ";").split(";") if x.strip() != ""]
            return (len(ids) > 0, len(ids))
    except Exception:
        pass
    return (False, 0)

def _resolve_full_source(layer):
    """
    Return the on-disk catalogPath of a layer so the FULL dataset is used,
    bypassing any active selection. Falls back to the layer itself.
    """
    try:
        d = arcpy.Describe(layer)
        cp = getattr(d, "catalogPath", None)
        if cp and arcpy.Exists(cp):
            return cp
    except Exception:
        pass
    return layer


# --------------------------
# ArcMap adding + auto symbology
# --------------------------

def _add_to_current_map(dataset_path, method_code, symbology_source_layer, auto_symbology, symbol_size):
    # ArcGIS Pro
    try:
        if hasattr(arcpy, "mp"):
            aprx = arcpy.mp.ArcGISProject("CURRENT")
            m = aprx.activeMap
            if m:
                m.addDataFromPath(dataset_path)
            return
    except Exception:
        pass
    # ArcMap
    try:
        import arcpy.mapping as mapping
        mxd = mapping.MapDocument("CURRENT")
        dfs = mapping.ListDataFrames(mxd)
        if not dfs:
            return
        df = dfs[0]
        lyr = mapping.Layer(dataset_path)
        if symbology_source_layer:
            try:
                arcpy.management.ApplySymbologyFromLayer(lyr, symbology_source_layer)
            except Exception:
                arcpy.AddWarning("ApplySymbologyFromLayer failed for: {0}".format(dataset_path))
        elif auto_symbology:
            try:
                if lyr.supports("ROTATION"):
                    lyr.rotationField = "ROT"
                    try:
                        lyr.rotationType = "GEOGRAPHIC"
                    except Exception:
                        pass
            except Exception:
                pass
            try:
                sym = lyr.symbology
                if hasattr(sym, "renderer") and hasattr(sym.renderer, "symbol"):
                    sym.renderer.symbol.size = float(symbol_size)
                    sym.renderer.symbol.color = _color_from_code(method_code)
                lyr.symbology = sym
            except Exception:
                pass
        mapping.AddLayer(df, lyr, "TOP")
    except Exception:
        return


# --------------------------
# AOI helpers
# --------------------------

def _project_and_buffer_aoi(aoi_fc, sr_work, buffer_dist, scratch_gdb, profile=False):
    t0 = time.time()
    aoi_p = os.path.join(scratch_gdb, "rot_tmp_aoi_p")
    aoi_b = os.path.join(scratch_gdb, "rot_tmp_aoi_buf")
    _safe_delete(aoi_p); _safe_delete(aoi_b)
    try:
        arcpy.management.Project(aoi_fc, aoi_p, sr_work)
    except Exception:
        return None, "AOI_PROJECT_FAIL"
    try:
        bd = float(buffer_dist) if buffer_dist is not None else 0.0
    except Exception:
        bd = 0.0
    if bd > 0:
        try:
            arcpy.analysis.Buffer(aoi_p, aoi_b, "{0}".format(bd), "FULL", "ROUND", "ALL")
            _profile_msg(profile, "AOI project+buffer", t0)
            return aoi_b, "OK_BUF"
        except Exception:
            _profile_msg(profile, "AOI project+buffer (buffer failed)", t0)
            return aoi_p, "OK_NO_BUF"
    else:
        _profile_msg(profile, "AOI project", t0)
        return aoi_p, "OK_NO_BUF"

def _filter_springs_by_aoi(spr_fc, aoi_fc, out_fc, profile=False):
    t0 = time.time()
    _safe_delete(out_fc)
    spr_lyr = _make_layer_name("lyr_spr", spr_fc)
    arcpy.management.MakeFeatureLayer(spr_fc, spr_lyr)
    arcpy.management.SelectLayerByLocation(spr_lyr, "INTERSECT", aoi_fc)
    arcpy.management.CopyFeatures(spr_lyr, out_fc)
    try:
        arcpy.management.Delete(spr_lyr)
    except Exception:
        pass
    _profile_msg(profile, "AOI filter springs", t0)
    return out_fc

def _filter_contours_by_aoi(con_fc_proj, aoi_fc_proj_or_buf, out_fc, profile=False):
    t0 = time.time()
    _safe_delete(out_fc)
    con_lyr = _make_layer_name("lyr_con", con_fc_proj)
    arcpy.management.MakeFeatureLayer(con_fc_proj, con_lyr)
    arcpy.management.SelectLayerByLocation(con_lyr, "INTERSECT", aoi_fc_proj_or_buf)
    arcpy.management.CopyFeatures(con_lyr, out_fc)
    try:
        arcpy.management.Delete(con_lyr)
    except Exception:
        pass
    _profile_msg(profile, "AOI filter contours (projected)", t0)
    return out_fc


# --------------------------
# Near + geometry helpers
# --------------------------

def _cache_contours_geom(contours_fc_proj, oid_subset=None):
    d = {}
    if not oid_subset:
        with arcpy.da.SearchCursor(contours_fc_proj, ["OID@", "SHAPE@"]) as cur:
            for oid, geom in cur:
                d[int(oid)] = geom
        return d
    oid_field = arcpy.Describe(contours_fc_proj).OIDFieldName
    oids = sorted([int(x) for x in oid_subset])
    if not oids:
        return d
    delim_oid = arcpy.AddFieldDelimiters(contours_fc_proj, oid_field)
    CHUNK = 500
    for i in range(0, len(oids), CHUNK):
        chunk = oids[i:i + CHUNK]
        where = "{0} IN ({1})".format(delim_oid, ",".join([str(x) for x in chunk]))
        with arcpy.da.SearchCursor(contours_fc_proj, ["OID@", "SHAPE@"], where_clause=where) as cur:
            for oid, geom in cur:
                d[int(oid)] = geom
    return d

def _tangent_az_at_near(line_geom, near_pt_geom, step):
    try:
        step = float(step)
        if step <= 0:
            step = 5.0
        m = line_geom.measureOnLine(near_pt_geom)
        if m is None:
            return None
        total = line_geom.length
        m1 = max(0.0, m - step)
        m2 = min(total, m + step)
        p1 = line_geom.positionAlongLine(m1).firstPoint
        p2 = line_geom.positionAlongLine(m2).firstPoint
        return _azimuth_geo_deg(p2.X - p1.X, p2.Y - p1.Y)
    except Exception:
        return None

def _run_near_location(spr_fc_proj, con_fc_proj, near_method, search_radius):
    arcpy.analysis.Near(
        in_features=spr_fc_proj,
        near_features=con_fc_proj,
        search_radius=search_radius,
        location="LOCATION",
        angle="NO_ANGLE",
        method=near_method
    )

def _run_near_table(spr_fc_proj, con_fc_proj, out_tbl, k, near_method, search_radius):
    _safe_delete(out_tbl)
    arcpy.analysis.GenerateNearTable(
        in_features=spr_fc_proj,
        near_features=con_fc_proj,
        out_table=out_tbl,
        search_radius=search_radius,
        location="LOCATION",
        angle="NO_ANGLE",
        closest="ALL",
        closest_count=int(k),
        method=near_method
    )
    return out_tbl


# --------------------------
# Methods (01..05)
# --------------------------

def _method_01_neartangent(spr_fc_proj, con_geom, sr_work, tangent_step, offset):
    out = {}
    with arcpy.da.SearchCursor(spr_fc_proj, ["SPR_TMPID", "NEAR_FID", "NEAR_X", "NEAR_Y"]) as cur:
        for sid, nf, nx, ny in cur:
            sid = int(sid)
            if nf is None or nx is None or ny is None or int(nf) < 0:
                out[sid] = (None, 0, "NO_NEAR")
                continue
            line = con_geom.get(int(nf))
            if line is None:
                out[sid] = (None, 0, "NO_LINE")
                continue
            pt = arcpy.PointGeometry(arcpy.Point(nx, ny), sr_work)
            az = _tangent_az_at_near(line, pt, tangent_step)
            if az is None:
                out[sid] = (None, 0, "TAN_FAIL")
            else:
                out[sid] = (_wrap360(az + offset), 1, "OK")
    return out

def _method_03_nearnormal(spr_fc_proj, offset):
    out = {}
    with arcpy.da.SearchCursor(spr_fc_proj, ["SPR_TMPID", "SHAPE@XY", "NEAR_X", "NEAR_Y"]) as cur:
        for sid, (sx, sy), nx, ny in cur:
            sid = int(sid)
            if nx is None or ny is None:
                out[sid] = (None, 0, "NO_NEAR")
                continue
            az = _azimuth_geo_deg(nx - sx, ny - sy)
            if az is None:
                out[sid] = (None, 0, "ZERO")
            else:
                out[sid] = (_wrap360(az + offset), 1, "OK")
    return out

def _near_table_id_field(near_tbl):
    names = [f.name for f in arcpy.ListFields(near_tbl)]
    return "SPR_TMPID" if "SPR_TMPID" in names else "IN_FID"

def _method_02_highlow(near_tbl, elev_lu, spr_fc_proj, offset):
    fallback = _method_03_nearnormal(spr_fc_proj, offset)
    id_field = _near_table_id_field(near_tbl)
    recs = {}
    with arcpy.da.SearchCursor(near_tbl, [id_field, "NEAR_FID", "NEAR_X", "NEAR_Y"]) as cur:
        for sid, nf, nx, ny in cur:
            if sid is None or nf is None or nx is None or ny is None:
                continue
            sid = int(sid)
            z = elev_lu.get(int(nf))
            if z is None:
                continue
            recs.setdefault(sid, []).append((float(z), float(nx), float(ny)))
    out = {}
    for sid, fb in fallback.items():
        rows = recs.get(sid, [])
        if len(rows) < 2:
            out[sid] = (fb[0], 0, "FALLBACK_NEAR_NRM")
            continue
        rows_sorted = sorted(rows, key=lambda t: t[0])
        low = rows_sorted[0]
        high = rows_sorted[-1]
        if high[0] == low[0]:
            out[sid] = (fb[0], 0, "FLAT_FALLBACK")
            continue
        az = _azimuth_geo_deg(low[1] - high[1], low[2] - high[2])
        if az is None:
            out[sid] = (fb[0], 0, "DEGEN_FALLBACK")
        else:
            out[sid] = (_wrap360(az + offset), 1, "OK")
    return out

def _method_04_planefit(near_tbl, elev_lu, offset):
    id_field = _near_table_id_field(near_tbl)
    samples = {}
    with arcpy.da.SearchCursor(near_tbl, [id_field, "NEAR_FID", "NEAR_X", "NEAR_Y"]) as cur:
        for sid, nf, nx, ny in cur:
            if sid is None or nf is None or nx is None or ny is None:
                continue
            sid = int(sid)
            z = elev_lu.get(int(nf))
            if z is None:
                continue
            samples.setdefault(sid, []).append((float(nx), float(ny), float(z)))

    def det3(a11, a12, a13, a21, a22, a23, a31, a32, a33):
        return (a11 * (a22 * a33 - a23 * a32)
                - a12 * (a21 * a33 - a23 * a31)
                + a13 * (a21 * a32 - a22 * a31))

    out = {}
    for sid, pts in samples.items():
        if len(pts) < 3:
            out[sid] = (None, 0, "NEED_3PTS")
            continue
        sxx = syy = sxy = sx = sy = 0.0
        sxz = syz = sz = 0.0
        n = 0.0
        for x, y, z in pts:
            n += 1.0
            sxx += x * x; syy += y * y; sxy += x * y
            sx += x; sy += y
            sxz += x * z; syz += y * z; sz += z
        D = det3(sxx, sxy, sx, sxy, syy, sy, sx, sy, n)
        if abs(D) < 1e-9:
            out[sid] = (None, 0, "SINGULAR")
            continue
        Da = det3(sxz, sxy, sx, syz, syy, sy, sz, sy, n)
        Db = det3(sxx, sxz, sx, sxy, syz, sy, sx, sz, n)
        a = Da / D
        b = Db / D
        az = _azimuth_geo_deg(-a, -b)
        if az is None:
            out[sid] = (None, 0, "ZERO_GRAD")
        else:
            out[sid] = (_wrap360(az + offset), 1, "OK")
    return out

def _method_05_centroidhl(near_tbl, elev_lu, offset):
    id_field = _near_table_id_field(near_tbl)
    samples = {}
    with arcpy.da.SearchCursor(near_tbl, [id_field, "NEAR_FID", "NEAR_X", "NEAR_Y"]) as cur:
        for sid, nf, nx, ny in cur:
            if sid is None or nf is None or nx is None or ny is None:
                continue
            sid = int(sid)
            z = elev_lu.get(int(nf))
            if z is None:
                continue
            samples.setdefault(sid, []).append((float(nx), float(ny), float(z)))
    out = {}
    for sid, pts in samples.items():
        if len(pts) < 4:
            out[sid] = (None, 0, "NEED_4PTS")
            continue
        zs = sorted([p[2] for p in pts])
        med = zs[len(zs) // 2]
        high = [p for p in pts if p[2] >= med]
        low = [p for p in pts if p[2] <= med]
        if len(high) == 0 or len(low) == 0:
            out[sid] = (None, 0, "SPLIT_FAIL")
            continue
        hx = sum(p[0] for p in high) / float(len(high))
        hy = sum(p[1] for p in high) / float(len(high))
        lx = sum(p[0] for p in low) / float(len(low))
        ly = sum(p[1] for p in low) / float(len(low))
        az = _azimuth_geo_deg(lx - hx, ly - hy)
        if az is None:
            out[sid] = (None, 0, "DEGEN")
        else:
            out[sid] = (_wrap360(az + offset), 1, "OK")
    return out


# --------------------------
# Output writers
# --------------------------

def _copy_output_base(spr_fc_tmp, out_gdb, out_name):
    out_fc = os.path.join(out_gdb, out_name)
    _safe_delete(out_fc)
    arcpy.management.CopyFeatures(spr_fc_tmp, out_fc)
    return out_fc

def _write_separate(out_base_fc, out_gdb, out_base_name, method_code, results):
    out_fc = os.path.join(out_gdb, "{0}_{1}".format(out_base_name, method_code))
    _safe_delete(out_fc)
    arcpy.management.CopyFeatures(out_base_fc, out_fc)
    _ensure_field(out_fc, "ROT", "DOUBLE")
    _ensure_field(out_fc, "OK", "SHORT")
    _ensure_field(out_fc, "NOTE", "TEXT", length=60)
    with arcpy.da.UpdateCursor(out_fc, ["SPR_TMPID", "ROT", "OK", "NOTE"]) as cur:
        for sid, rot, ok, note in cur:
            sid = int(sid)
            r, o, n = results.get(sid, (None, 0, "NO_DATA"))
            cur.updateRow([sid, r, o, n])
    return out_fc

def _write_single_fields(out_fc, results_by_method):
    _ensure_field(out_fc, "ROT", "DOUBLE")
    methods = list(results_by_method.keys())
    for m in methods:
        _ensure_field(out_fc, "ROT_" + m, "DOUBLE")
        _ensure_field(out_fc, "OK_" + m, "SHORT")
        _ensure_field(out_fc, "NOTE_" + m, "TEXT", length=60)
    field_list = ["SPR_TMPID", "ROT"]
    for m in methods:
        field_list.extend(["ROT_" + m, "OK_" + m, "NOTE_" + m])
    with arcpy.da.UpdateCursor(out_fc, field_list) as cur:
        for row in cur:
            active = None
            idx = 2
            for m in methods:
                rot, ok, note = results_by_method[m].get(int(row[0]), (None, 0, "NO_DATA"))
                row[idx] = rot; row[idx + 1] = ok; row[idx + 2] = note
                if active is None and rot is not None:
                    active = rot
                idx += 3
            row[1] = active
            cur.updateRow(row)

def _write_summary_table(out_gdb, base_name, n_total, results_by_method):
    tbl = os.path.join(out_gdb, _validate_output_name(base_name + "_Summary", out_gdb))
    _safe_delete(tbl)
    arcpy.management.CreateTable(out_gdb, os.path.basename(tbl))
    arcpy.management.AddField(tbl, "METHOD", "TEXT", field_length=40)
    arcpy.management.AddField(tbl, "N_TOTAL", "LONG")
    arcpy.management.AddField(tbl, "N_OK", "LONG")
    arcpy.management.AddField(tbl, "N_FAIL", "LONG")
    arcpy.management.AddField(tbl, "OK_PCT", "DOUBLE")
    with arcpy.da.InsertCursor(tbl, ["METHOD", "N_TOTAL", "N_OK", "N_FAIL", "OK_PCT"]) as ic:
        for m in results_by_method.keys():
            ok = 0
            for sid, (rot, o, note) in results_by_method[m].items():
                if int(o) == 1:
                    ok += 1
            pct = (100.0 * ok / float(n_total)) if n_total > 0 else 0.0
            ic.insertRow([m, int(n_total), int(ok), int(n_total - ok), float(pct)])
    return tbl


# --------------------------
# Main runner
# --------------------------

def _run_suite(springs_layer, contours_layer, elev_field, out_gdb, out_base_name,
               output_mode, create_summary, symbology_source_layer, auto_symbology,
               symbol_size, work_sr_mode, near_method, search_radius, global_offset,
               k_near, tangent_step, aoi_layer, aoi_buffer, cache_mode, profile,
               ignore_selection, run_01, run_02, run_03, run_04, run_05):
    t_all = time.time()

    # ---- SELECTION TRAP CHECK (the key fix) -------------------------------
    spr_has_sel, spr_nsel = _selection_info(springs_layer)
    con_has_sel, con_nsel = _selection_info(contours_layer)
    if spr_has_sel:
        arcpy.AddWarning("Springs layer has an ACTIVE SELECTION ({0} features).".format(spr_nsel))
    if con_has_sel:
        arcpy.AddWarning("Contours layer has an ACTIVE SELECTION ({0} features).".format(con_nsel))

    springs_src = springs_layer
    contours_src = contours_layer
    if ignore_selection:
        springs_src = _resolve_full_source(springs_layer)
        contours_src = _resolve_full_source(contours_layer)
        if spr_has_sel or con_has_sel:
            arcpy.AddMessage("Process-ALL mode ON -> using full datasets on disk (selection ignored).")
    else:
        if spr_has_sel or con_has_sel:
            arcpy.AddWarning("Process-ALL mode OFF -> ONLY the selected features will be processed.")

    spr_total_src = _get_count(springs_src)
    arcpy.AddMessage("Springs available for processing: {0}".format(spr_total_src))

    sr_work = _pick_work_sr(springs_src, work_sr_mode)
    arcpy.AddMessage("Working SR: {0}".format(sr_work.name if sr_work else "Unknown"))
    arcpy.AddMessage("Rotation: GEOGRAPHIC degrees (0=N, clockwise).")

    scratch = arcpy.env.scratchGDB

    # Normalize search radius
    sradius = None
    try:
        if search_radius is not None and float(search_radius) > 0:
            sradius = str(float(search_radius))
            arcpy.AddWarning("Search radius = {0} (working SR units). Springs farther than this "
                             "from any contour will NOT be rotated.".format(sradius))
    except Exception:
        sradius = None

    # Copy inputs (now from full source when ignore_selection=True)
    t0 = time.time()
    spr_tmp = os.path.join(scratch, "spr_tmp_in")
    con_tmp = os.path.join(scratch, "con_tmp_in")
    _safe_delete(spr_tmp); _safe_delete(con_tmp)
    arcpy.management.CopyFeatures(springs_src, spr_tmp)
    arcpy.management.CopyFeatures(contours_src, con_tmp)
    _profile_msg(profile, "Copy inputs", t0)

    n_after_copy = _get_count(spr_tmp)
    arcpy.AddMessage("[DIAG] springs after copy: {0}".format(n_after_copy))

    # AOI filter (springs)
    aoi_tmp = None
    if aoi_layer and arcpy.Exists(aoi_layer) and _get_count(aoi_layer) > 0:
        if _shape_type(aoi_layer) != "polygon":
            arcpy.AddWarning("AOI is not polygon; AOI ignored.")
        else:
            t0 = time.time()
            aoi_tmp = os.path.join(scratch, "rot_tmp_aoi_in")
            _safe_delete(aoi_tmp)
            arcpy.management.CopyFeatures(aoi_layer, aoi_tmp)
            _profile_msg(profile, "Copy AOI", t0)
            spr_sel = os.path.join(scratch, "spr_tmp_in_aoi")
            _filter_springs_by_aoi(spr_tmp, aoi_tmp, spr_sel, profile=profile)
            _safe_delete(spr_tmp)
            spr_tmp = spr_sel
            n_spr = _get_count(spr_tmp)
            arcpy.AddMessage("[DIAG] springs inside AOI: {0}".format(n_spr))
            if n_spr == 0:
                raise arcpy.ExecuteError("AOI removed all springs. Check AOI position.")

    n_total = _get_count(spr_tmp)

    # Stable id - use the ACTUAL OID field name (shapefile-safe)
    t0 = time.time()
    _ensure_field(spr_tmp, "SPR_TMPID", "LONG")
    oidf = _oid_field(spr_tmp)
    expr_mode = "PYTHON3" if hasattr(arcpy, "mp") else "PYTHON_9.3"
    arcpy.management.CalculateField(spr_tmp, "SPR_TMPID", "!{0}!".format(oidf), expr_mode)
    _profile_msg(profile, "Add SPR_TMPID", t0)

    # Project
    t0 = time.time()
    spr_p = os.path.join(scratch, "spr_tmp_proj")
    con_p = os.path.join(scratch, "con_tmp_proj")
    _safe_delete(spr_p); _safe_delete(con_p)
    arcpy.management.Project(spr_tmp, spr_p, sr_work)
    arcpy.management.Project(con_tmp, con_p, sr_work)
    _profile_msg(profile, "Project to working SR", t0)

    # AOI filter contours (projected + buffered)
    if aoi_tmp:
        aoi_work, st = _project_and_buffer_aoi(aoi_tmp, sr_work, aoi_buffer, scratch, profile=profile)
        if aoi_work:
            con_sel = os.path.join(scratch, "con_tmp_proj_aoi")
            _filter_contours_by_aoi(con_p, aoi_work, con_sel, profile=profile)
            _safe_delete(con_p)
            con_p = con_sel
            arcpy.AddMessage("AOI contours filtered ({0}).".format(st))

    n_contours = _get_count(con_p)
    arcpy.AddMessage("[DIAG] contours used: {0}".format(n_contours))
    if n_contours <= 0:
        raise arcpy.ExecuteError("No contours available after filtering. Cannot compute rotations.")

    # Elev lookup
    t0 = time.time()
    elev_lu = {}
    n_bad_elev = 0
    with arcpy.da.SearchCursor(con_p, ["OID@", elev_field]) as cur:
        for oid, z in cur:
            if z is None:
                n_bad_elev += 1
                continue
            elev_lu[int(oid)] = float(z)
    _profile_msg(profile, "Build elevation lookup", t0)
    if n_bad_elev:
        arcpy.AddWarning("[DIAG] {0} contour(s) had NULL elevation -> ignored by elevation methods.".format(n_bad_elev))

    # Near (once)
    t0 = time.time()
    arcpy.AddMessage("Running Near (LOCATION) once...")
    _run_near_location(spr_p, con_p, near_method, sradius)
    _profile_msg(profile, "Near (LOCATION)", t0)

    # DIAG: how many springs actually got a near contour
    got_near = 0
    with arcpy.da.SearchCursor(spr_p, ["NEAR_FID"]) as cur:
        for (nf,) in cur:
            if nf is not None and int(nf) >= 0:
                got_near += 1
    arcpy.AddMessage("[DIAG] springs with a NEAR contour: {0} / {1}".format(got_near, n_total))
    if got_near < n_total:
        arcpy.AddWarning("[DIAG] {0} spring(s) found NO contour within the search radius "
                         "(they will not be rotated). Increase / clear the search radius if unexpected."
                         .format(n_total - got_near))

    # NearTable for K-based methods
    need_k = bool(run_02 or run_04 or run_05)
    near_tbl = os.path.join(scratch, "spr_near_tbl")
    if need_k:
        t0 = time.time()
        arcpy.AddMessage("Running GenerateNearTable (K={0})...".format(int(k_near)))
        _run_near_table(spr_p, con_p, near_tbl, int(k_near), near_method, sradius)
        _profile_msg(profile, "GenerateNearTable", t0)
        try:
            existing = [f.name for f in arcpy.ListFields(near_tbl)]
            if "SPR_TMPID" in existing:
                arcpy.management.DeleteField(near_tbl, ["SPR_TMPID"])
            arcpy.management.JoinField(near_tbl, "IN_FID", spr_p, _oid_field(spr_p), ["SPR_TMPID"])
        except Exception:
            arcpy.AddWarning("JoinField failed; K-based results may be less reliable.")

    # Cache contours geom (only for method 01)
    con_geom = {}
    if run_01:
        t0 = time.time()
        mode = (cache_mode or "NEAR_ONLY").upper()
        if mode == "NEAR_ONLY":
            needed = set()
            with arcpy.da.SearchCursor(spr_p, ["NEAR_FID"]) as cur:
                for (nf,) in cur:
                    if nf is not None and int(nf) >= 0:
                        needed.add(int(nf))
            con_geom = _cache_contours_geom(con_p, oid_subset=needed)
            arcpy.AddMessage("Cache NEAR_ONLY: {0} contours.".format(len(con_geom)))
        else:
            con_geom = _cache_contours_geom(con_p, oid_subset=None)
            arcpy.AddMessage("Cache ALL: {0} contours.".format(len(con_geom)))
        _profile_msg(profile, "Cache contours", t0)

    results = {}
    if run_01:
        t0 = time.time()
        arcpy.AddMessage("Computing 01_NearTangent ...")
        results["01_NearTangent"] = _method_01_neartangent(spr_p, con_geom, sr_work, tangent_step, global_offset)
        _profile_msg(profile, "Method 01", t0)
    if run_02 and need_k:
        t0 = time.time()
        arcpy.AddMessage("Computing 02_HighLow ...")
        results["02_HighLow"] = _method_02_highlow(near_tbl, elev_lu, spr_p, global_offset)
        _profile_msg(profile, "Method 02", t0)
    if run_03:
        t0 = time.time()
        arcpy.AddMessage("Computing 03_NearNormal ...")
        results["03_NearNormal"] = _method_03_nearnormal(spr_p, global_offset)
        _profile_msg(profile, "Method 03", t0)
    if run_04 and need_k:
        t0 = time.time()
        arcpy.AddMessage("Computing 04_PlaneFit ...")
        results["04_PlaneFit"] = _method_04_planefit(near_tbl, elev_lu, global_offset)
        _profile_msg(profile, "Method 04", t0)
    if run_05 and need_k:
        t0 = time.time()
        arcpy.AddMessage("Computing 05_CentroidHL ...")
        results["05_CentroidHL"] = _method_05_centroidhl(near_tbl, elev_lu, global_offset)
        _profile_msg(profile, "Method 05", t0)

    if not results:
        raise arcpy.ExecuteError("No method selected. Enable at least one of methods 01..05.")

    # DIAG: per-method OK counts
    for mname, mres in results.items():
        ok = sum(1 for v in mres.values() if int(v[1]) == 1)
        rotated = sum(1 for v in mres.values() if v[0] is not None)
        arcpy.AddMessage("[DIAG] {0}: rotated={1}  OK={2}  of {3}".format(mname, rotated, ok, n_total))

    # Output base
    t0 = time.time()
    out_base_fc = _copy_output_base(spr_tmp, out_gdb, out_base_name)
    _profile_msg(profile, "Write base output", t0)

    out_paths = []
    if output_mode == "SINGLE_FIELDS":
        arcpy.AddMessage("Writing SINGLE_FIELDS output...")
        _write_single_fields(out_base_fc, results)
        out_paths.append(out_base_fc)
    else:
        arcpy.AddMessage("Writing SEPARATE_LAYERS outputs...")
        for method_code in results.keys():
            out_fc = _write_separate(out_base_fc, out_gdb, out_base_name, method_code, results[method_code])
            out_paths.append(out_fc)
        _safe_delete(out_base_fc)

    if create_summary:
        try:
            tbl = _write_summary_table(out_gdb, out_base_name, n_total, results)
            out_paths.append(tbl)
            arcpy.AddMessage("Summary table created: {0}".format(tbl))
        except Exception:
            arcpy.AddWarning("Summary table failed (ignored).")

    # Add to map
    t0 = time.time()
    for pth in out_paths:
        if pth.lower().endswith(".dbf") or pth.lower().endswith(".csv"):
            continue
        try:
            if arcpy.Describe(pth).dataType.lower().find("feature") < 0:
                continue
        except Exception:
            continue
        method_code = os.path.basename(pth).replace(out_base_name + "_", "")
        _add_to_current_map(
            dataset_path=pth,
            method_code=method_code,
            symbology_source_layer=symbology_source_layer,
            auto_symbology=bool(auto_symbology) and (not symbology_source_layer),
            symbol_size=float(symbol_size) if symbol_size else 40.0
        )
    _profile_msg(profile, "Add outputs to map", t0)
    _profile_msg(profile, "TOTAL", t_all)
    return out_paths


# --------------------------
# Python Toolbox (UI)
# --------------------------

def _set_category(param, cat):
    try:
        param.category = cat
    except Exception:
        pass

def _make_aoi_featureset_schema():
    fs = arcpy.FeatureSet()
    try:
        scratch = arcpy.env.scratchGDB
        schema_fc = os.path.join(scratch, "rot_aoi_schema")
        _safe_delete(schema_fc)
        arcpy.management.CreateFeatureclass(
            out_path=scratch, out_name="rot_aoi_schema",
            geometry_type="POLYGON", spatial_reference=arcpy.SpatialReference(4326))
        fs.load(schema_fc)
    except Exception:
        pass
    return fs


class Toolbox(object):
    def __init__(self):
        self.label = "Spring Rotation - Comparison Suite (v4 hardened)"
        self.alias = "SpringRotationSuiteV4"
        self.tools = [SpringRotationFinalSuiteTool]


class SpringRotationFinalSuiteTool(object):
    def __init__(self):
        self.label = "Spring Rotation, Final Tool (v4 hardened), Mirjafari"
        self.description = "Compare spring rotations. Processes ALL springs by default. Maintainer: Ali Mirjafari, 09186441801"
        self.canRunInBackground = False

    def getParameterInfo(self):
        p = []
        CAT_IN = "01 Inputs"
        CAT_OUT = "02 Outputs"
        CAT_ADV = "03 Advanced"
        CAT_PROC = "04 Processing"
        CAT_SYM = "05 Symbology"
        CAT_AOI = "06 AOI"
        CAT_PERF = "07 Performance"
        CAT_METH = "08 Methods"

        p0 = arcpy.Parameter(displayName="Springs (Point layer)", name="springs",
                             datatype="GPFeatureLayer", parameterType="Required", direction="Input")
        _set_category(p0, CAT_IN)
        p1 = arcpy.Parameter(displayName="Contours (Polyline layer)", name="contours",
                             datatype="GPFeatureLayer", parameterType="Required", direction="Input")
        _set_category(p1, CAT_IN)
        p2 = arcpy.Parameter(displayName="Contour elevation field (numeric)", name="elev_field",
                             datatype="Field", parameterType="Required", direction="Input")
        p2.parameterDependencies = [p1.name]
        _set_category(p2, CAT_IN)

        # NEW: ignore-selection switch (the fix)
        p_sel = arcpy.Parameter(displayName="Process ALL features (ignore any active selection)",
                                name="ignore_selection", datatype="GPBoolean",
                                parameterType="Optional", direction="Input")
        p_sel.value = True
        _set_category(p_sel, CAT_IN)

        p3 = arcpy.Parameter(displayName="Output file geodatabase (*.gdb)", name="out_gdb",
                             datatype="DEWorkspace", parameterType="Required", direction="Input")
        _set_category(p3, CAT_OUT)
        p4 = arcpy.Parameter(displayName="Output feature class base name", name="out_base_name",
                             datatype="GPString", parameterType="Required", direction="Input")
        p4.value = "springs_rotation_suite"
        _set_category(p4, CAT_OUT)
        p5 = arcpy.Parameter(displayName="Output layout", name="output_mode",
                             datatype="GPString", parameterType="Optional", direction="Input")
        p5.filter.type = "ValueList"
        p5.filter.list = ["SEPARATE_LAYERS", "SINGLE_FIELDS"]
        p5.value = "SEPARATE_LAYERS"
        _set_category(p5, CAT_OUT)
        p6 = arcpy.Parameter(displayName="Create summary table (OK counts per method)", name="create_summary",
                             datatype="GPBoolean", parameterType="Optional", direction="Input")
        p6.value = False
        _set_category(p6, CAT_OUT)

        p7 = arcpy.Parameter(displayName="Symbology template layer (optional; from current map)", name="sym_layer",
                             datatype="GPFeatureLayer", parameterType="Optional", direction="Input")
        _set_category(p7, CAT_SYM)
        p8 = arcpy.Parameter(displayName="Auto-apply simple symbology (ArcMap only; if no template)", name="auto_symbology",
                             datatype="GPBoolean", parameterType="Optional", direction="Input")
        p8.value = True
        _set_category(p8, CAT_SYM)
        p9 = arcpy.Parameter(displayName="Symbol size (points; ArcMap auto-symbology)", name="symbol_size",
                             datatype="GPDouble", parameterType="Optional", direction="Input")
        p9.value = 40.0
        _set_category(p9, CAT_SYM)

        p10 = arcpy.Parameter(displayName="Working coordinate system (recommended: AUTO_UTM)", name="work_sr_mode",
                              datatype="GPString", parameterType="Optional", direction="Input")
        p10.filter.type = "ValueList"
        p10.filter.list = ["AUTO_UTM", "USE_INPUT"]
        p10.value = "AUTO_UTM"
        _set_category(p10, CAT_PROC)
        p11 = arcpy.Parameter(displayName="Near method (recommended: PLANAR with AUTO_UTM)", name="near_method",
                              datatype="GPString", parameterType="Optional", direction="Input")
        p11.filter.type = "ValueList"
        p11.filter.list = ["PLANAR", "GEODESIC"]
        p11.value = "PLANAR"
        _set_category(p11, CAT_PROC)
        p12 = arcpy.Parameter(displayName="Search radius (optional; working SR units; 0/empty = UNLIMITED)",
                              name="search_radius", datatype="GPDouble", parameterType="Optional", direction="Input")
        p12.value = None
        _set_category(p12, CAT_PROC)

        p13 = arcpy.Parameter(displayName="Global rotation offset (degrees; added to all methods)", name="global_offset",
                              datatype="GPDouble", parameterType="Optional", direction="Input")
        p13.value = 0.0
        _set_category(p13, CAT_ADV)
        p14 = arcpy.Parameter(displayName="K nearest contour hits (used by 02/04/05)", name="k_near",
                              datatype="GPLong", parameterType="Optional", direction="Input")
        p14.value = 8
        _set_category(p14, CAT_ADV)
        p15 = arcpy.Parameter(displayName="Tangent sampling step (map units; used by 01)", name="tangent_step",
                              datatype="GPDouble", parameterType="Optional", direction="Input")
        p15.value = 5.0
        _set_category(p15, CAT_ADV)

        p16 = arcpy.Parameter(displayName="AOI sketch", name="aoi_sketch",
                              datatype="GPFeatureRecordSetLayer", parameterType="Optional", direction="Input")
        p16.value = _make_aoi_featureset_schema()
        _set_category(p16, CAT_AOI)
        p17 = arcpy.Parameter(displayName="AOI buffer for contours (working SR units; optional)", name="aoi_buffer",
                              datatype="GPDouble", parameterType="Optional", direction="Input")
        p17.value = 0.0
        _set_category(p17, CAT_AOI)

        p18 = arcpy.Parameter(displayName="Contour cache mode (method 01 only)", name="cache_mode",
                              datatype="GPString", parameterType="Optional", direction="Input")
        p18.filter.type = "ValueList"
        p18.filter.list = ["NEAR_ONLY", "ALL"]
        p18.value = "NEAR_ONLY"
        _set_category(p18, CAT_PERF)
        p19 = arcpy.Parameter(displayName="Profiling messages (show timings)", name="profile",
                              datatype="GPBoolean", parameterType="Optional", direction="Input")
        p19.value = False
        _set_category(p19, CAT_PERF)

        m01 = arcpy.Parameter(displayName="01 NearTangent - tangent at nearest contour point", name="run_01",
                              datatype="GPBoolean", parameterType="Optional", direction="Input")
        m01.value = True
        _set_category(m01, CAT_METH)
        m02 = arcpy.Parameter(displayName="02 HighLow - high->low vector from K projected contour points", name="run_02",
                              datatype="GPBoolean", parameterType="Optional", direction="Input")
        m02.value = True
        _set_category(m02, CAT_METH)
        m03 = arcpy.Parameter(displayName="03 NearNormal - spring->nearest contour point", name="run_03",
                              datatype="GPBoolean", parameterType="Optional", direction="Input")
        m03.value = False
        _set_category(m03, CAT_METH)
        m04 = arcpy.Parameter(displayName="04 PlaneFit - least-squares plane from K samples", name="run_04",
                              datatype="GPBoolean", parameterType="Optional", direction="Input")
        m04.value = False
        _set_category(m04, CAT_METH)
        m05 = arcpy.Parameter(displayName="05 CentroidHL - centroid(high)->centroid(low)", name="run_05",
                              datatype="GPBoolean", parameterType="Optional", direction="Input")
        m05.value = False
        _set_category(m05, CAT_METH)

        outp = arcpy.Parameter(displayName="Outputs (semicolon-separated paths)", name="outputs",
                               datatype="GPString", parameterType="Derived", direction="Output")
        _set_category(outp, CAT_OUT)

        p.extend([p0, p1, p2, p_sel, p3, p4, p5, p6, p7, p8, p9, p10, p11, p12,
                  p13, p14, p15, p16, p17, p18, p19, m01, m02, m03, m04, m05, outp])
        return p

    def isLicensed(self):
        return True

    def updateParameters(self, parameters):
        # indices (note +1 shift after inserting ignore_selection at idx 3):
        # 0 springs,1 contours,2 elev,3 ignore_sel,
        # 4 out_gdb,5 name,6 outmode,7 summary,
        # 8 sym,9 autoSym,10 size,
        # 11 sr,12 near,13 radius,
        # 14 offset,15 K,16 step,
        # 17 AOI,18 AOIbuf,19 cache,20 profile,
        # 21 run01,22 run02,23 run03,24 run04,25 run05,26 outputs
        try:
            sym_layer = parameters[8].valueAsText
            has_template = bool(sym_layer)
            parameters[9].enabled = (not has_template)
            parameters[10].enabled = (not has_template)

            run_01 = bool(parameters[21].value)
            run_02 = bool(parameters[22].value)
            run_04 = bool(parameters[24].value)
            run_05 = bool(parameters[25].value)
            need_k = bool(run_02 or run_04 or run_05)

            parameters[15].enabled = need_k     # K
            parameters[16].enabled = run_01     # step
            parameters[19].enabled = run_01     # cache

            try:
                aoi_txt = parameters[17].valueAsText
                parameters[18].enabled = bool(aoi_txt)
            except Exception:
                parameters[18].enabled = True
        except Exception:
            pass
        return

    def updateMessages(self, parameters):
        try:
            springs = parameters[0].valueAsText
            contours = parameters[1].valueAsText
            elev_field = parameters[2].valueAsText
            out_gdb = parameters[4].valueAsText
            out_base_name = parameters[5].valueAsText

            if springs and _shape_type(springs) and _shape_type(springs) != "point":
                parameters[0].setWarningMessage("Springs should be POINT geometry.")
            if contours and _shape_type(contours) and _shape_type(contours) != "polyline":
                parameters[1].setWarningMessage("Contours should be POLYLINE geometry.")

            # Live selection warning so the user sees the trap before running
            try:
                if springs:
                    has_sel, n_sel = _selection_info(springs)
                    if has_sel:
                        parameters[0].setWarningMessage(
                            "This layer has {0} selected feature(s). With 'Process ALL features' ON "
                            "the whole dataset is used; turn it OFF only if you intend to limit to the "
                            "selection.".format(n_sel))
            except Exception:
                pass

            if contours and elev_field:
                if not _field_is_numeric(contours, elev_field):
                    parameters[2].setErrorMessage("Elevation field must be numeric (Integer/Float/Double).")

            if out_gdb:
                if not out_gdb.lower().endswith(".gdb"):
                    parameters[4].setWarningMessage("Recommended output is a File Geodatabase (*.gdb).")
                if not arcpy.Exists(out_gdb):
                    parameters[4].setErrorMessage("Output geodatabase does not exist: {0}".format(out_gdb))

            if out_gdb and out_base_name:
                v = _validate_output_name(out_base_name, out_gdb)
                if v != out_base_name:
                    parameters[5].setWarningMessage("Output name will be validated to: {0}".format(v))

            try:
                if parameters[15].enabled:
                    k = parameters[15].value
                    if k is not None and int(k) < 2:
                        parameters[15].setErrorMessage("K must be >= 2 for methods 02/04/05.")
            except Exception:
                pass
            try:
                if parameters[16].enabled:
                    st = parameters[16].value
                    if st is not None and float(st) <= 0:
                        parameters[16].setErrorMessage("Tangent step must be > 0.")
            except Exception:
                pass
            try:
                bd = parameters[18].value
                if bd is not None and float(bd) < 0:
                    parameters[18].setErrorMessage("AOI buffer must be >= 0.")
            except Exception:
                pass
            try:
                rad = parameters[13].value
                if rad is not None and float(rad) < 0:
                    parameters[13].setErrorMessage("Search radius must be >= 0.")
            except Exception:
                pass
        except Exception:
            pass
        return

    def execute(self, parameters, messages):
        springs = parameters[0].valueAsText
        contours = parameters[1].valueAsText
        elev_field = parameters[2].valueAsText
        ignore_selection = bool(parameters[3].value)

        out_gdb = parameters[4].valueAsText
        out_base_name_in = parameters[5].valueAsText
        output_mode = parameters[6].valueAsText
        create_summary = bool(parameters[7].value)

        sym_layer = parameters[8].valueAsText
        auto_symbology = parameters[9].value
        symbol_size = parameters[10].value

        work_sr_mode = parameters[11].valueAsText
        near_method = parameters[12].valueAsText
        search_radius = parameters[13].value

        global_offset = parameters[14].value
        k_near = parameters[15].value
        tangent_step = parameters[16].value

        aoi_layer = parameters[17].valueAsText
        aoi_buffer = parameters[18].value

        cache_mode = parameters[19].valueAsText
        profile = bool(parameters[20].value)

        run_01 = bool(parameters[21].value)
        run_02 = bool(parameters[22].value)
        run_03 = bool(parameters[23].value)
        run_04 = bool(parameters[24].value)
        run_05 = bool(parameters[25].value)

        out_base_name = _validate_output_name(out_base_name_in, out_gdb)
        if out_base_name_in != out_base_name:
            arcpy.AddWarning("Output base name validated: '{0}' -> '{1}'".format(out_base_name_in, out_base_name))

        try:
            arcpy.env.overwriteOutput = True
        except Exception:
            pass

        arcpy.AddMessage("Spring Rotation tool (v4 hardened) started.")
        arcpy.AddMessage("Maintainer: Ali Mirjafari - 09186441801")

        try:
            outs = _run_suite(
                springs_layer=springs, contours_layer=contours, elev_field=elev_field,
                out_gdb=out_gdb, out_base_name=out_base_name, output_mode=output_mode,
                create_summary=create_summary, symbology_source_layer=sym_layer,
                auto_symbology=auto_symbology, symbol_size=symbol_size,
                work_sr_mode=work_sr_mode, near_method=near_method, search_radius=search_radius,
                global_offset=float(global_offset) if global_offset is not None else 0.0,
                k_near=int(k_near) if k_near else 8,
                tangent_step=float(tangent_step) if tangent_step else 5.0,
                aoi_layer=aoi_layer,
                aoi_buffer=float(aoi_buffer) if aoi_buffer is not None else 0.0,
                cache_mode=cache_mode, profile=profile, ignore_selection=ignore_selection,
                run_01=run_01, run_02=run_02, run_03=run_03, run_04=run_04, run_05=run_05)
        except Exception:
            tb = traceback.format_exc()
            arcpy.AddError("Tool failed. Traceback:")
            arcpy.AddError(tb)
            raise

        parameters[26].value = ";".join([str(x) for x in outs])
        arcpy.AddMessage("Done. Created {0} output(s).".format(len(outs)))
