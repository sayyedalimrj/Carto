# -*- coding: utf-8 -*-
"""
Spring Rotation - Comparison Suite  (ArcGIS Pro / Python 3)  v4 NATIVE
=======================================================================
Native Pro rewrite of the Spring Rotation tool. Same hardening philosophy
as the ArcMap v4 build (selection bypass, stage-by-stage [DIAG] logging,
dynamic OID field, `from __future__ import division`-equivalent safety)
but in modern Python 3 with f-strings, type hints, arcpy.management/analysis
namespaces, and arcpy.mp for map integration.

Five rotation methods (lower-is-better):
  01 - Near Tangent (tangent at nearest point on contour)
  02 - High→Low (direction from higher to lower neighbour contours)
  03 - Near Normal (spring→nearest-point direction)
  04 - Plane Fit (least-squares plane gradient from K neighbours)
  05 - Centroid High/Low (centroid of high vs low neighbours)

Rotation convention: GEOGRAPHIC degrees, 0 = North, clockwise.

Author: Ali Mirjafari + Kiro
Version: 4.0 (Pro / Python 3)
"""

from __future__ import annotations

import os
import math
import hashlib
import traceback
import time
from typing import Dict, List, Optional, Tuple

import arcpy


# =============================================================================
# 0. Utilities
# =============================================================================

def _to_bytes_utf8(s) -> bytes:
    try:
        if isinstance(s, bytes):
            return s
        return str(s).encode("utf-8")
    except Exception:
        return b"x"

def _wrap360(a) -> Optional[float]:
    if a is None:
        return None
    a = float(a) % 360.0
    if a < 0:
        a += 360.0
    return a

def _azimuth_geo_deg(dx: float, dy: float) -> Optional[float]:
    """Geographic azimuth: 0=N, 90=E, 180=S, 270=W."""
    if dx == 0 and dy == 0:
        return None
    ang = math.degrees(math.atan2(dx, dy))
    if ang < 0:
        ang += 360.0
    return ang

def _safe_delete(path) -> None:
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

def _oid_field(fc) -> str:
    d = _desc(fc)
    try:
        return d.OIDFieldName if d else "OBJECTID"
    except Exception:
        return "OBJECTID"

def _is_projected_meter(sr) -> bool:
    try:
        if sr is None:
            return False
        return (sr.type == "Projected") and \
               ("meter" in sr.linearUnitName.lower() or "metre" in sr.linearUnitName.lower())
    except Exception:
        return False

def _utm_sr_for_lonlat(lon: float, lat: float):
    zone = int((lon + 180.0) / 6.0) + 1
    epsg = (32600 + zone) if lat >= 0 else (32700 + zone)
    return arcpy.SpatialReference(epsg)

def _pick_work_sr(spr_fc, mode: str):
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

def _ensure_field(fc: str, name: str, ftype: str, length: int = 255) -> None:
    names = [f.name for f in arcpy.ListFields(fc)]
    if name in names:
        return
    if ftype.upper() == "TEXT":
        arcpy.management.AddField(fc, name, ftype, field_length=length)
    else:
        arcpy.management.AddField(fc, name, ftype)

def _validate_output_name(name, workspace) -> str:
    try:
        if workspace:
            return arcpy.ValidateTableName(name, workspace)
    except Exception:
        pass
    if not name:
        return "springs_rotation_suite"
    return "".join([c if (c.isalnum() or c == "_") else "_" for c in name])

def _get_count(ds) -> int:
    try:
        return int(arcpy.management.GetCount(ds).getOutput(0))
    except Exception:
        return -1

def _make_layer_name(prefix: str, seed) -> str:
    try:
        h = hashlib.md5(_to_bytes_utf8(seed or "x")).hexdigest()[:8]
    except Exception:
        h = "tmp"
    return f"{prefix}_{h}"

def _msg(s) -> None:
    try:
        arcpy.AddMessage(str(s))
    except Exception:
        pass

def _warn(s) -> None:
    try:
        arcpy.AddWarning(str(s))
    except Exception:
        pass

def _diag(s) -> None:
    _msg(f"[DIAG] {s}")

def _profile_msg(enabled: bool, label: str, t0: float) -> None:
    if enabled:
        _msg(f"[PROFILE] {label}: {time.time() - t0:.3f}s")


# =============================================================================
# 1. Selection-bypass
# =============================================================================

def _selection_info(layer) -> Tuple[bool, int]:
    try:
        d = arcpy.Describe(layer)
        fidset = getattr(d, "FIDSet", None)
        if fidset:
            ids = [x for x in fidset.replace(",", ";").split(";") if x.strip() != ""]
            return (len(ids) > 0, len(ids))
    except Exception:
        pass
    return (False, 0)

def _resolve_full_source(layer):
    try:
        d = arcpy.Describe(layer)
        cp = getattr(d, "catalogPath", None)
        if cp and arcpy.Exists(cp):
            return cp
    except Exception:
        pass
    return layer

def _announce_selection(label: str, layer) -> None:
    has_sel, n_sel = _selection_info(layer)
    if has_sel:
        _warn(f"{label}: layer has an active selection ({n_sel} features). "
              f"Ignoring selection - processing FULL dataset.")
    else:
        total = _get_count(layer)
        _diag(f"{label}: total={total}, no active selection.")



# =============================================================================
# 2. AOI helpers
# =============================================================================

def _project_and_buffer_aoi(aoi_fc, sr_work, buffer_dist, scratch_gdb,
                             profile: bool = False):
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
            arcpy.analysis.Buffer(aoi_p, aoi_b, f"{bd}", "FULL", "ROUND", "ALL")
            _profile_msg(profile, "AOI project+buffer", t0)
            return aoi_b, "OK_BUF"
        except Exception:
            _profile_msg(profile, "AOI project+buffer (buffer failed)", t0)
            return aoi_p, "OK_NO_BUF"
    else:
        _profile_msg(profile, "AOI project", t0)
        return aoi_p, "OK_NO_BUF"

def _filter_springs_by_aoi(spr_fc, aoi_fc, out_fc, profile: bool = False):
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

def _filter_contours_by_aoi(con_fc_proj, aoi_fc_proj_or_buf, out_fc,
                              profile: bool = False):
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


# =============================================================================
# 3. Near + geometry helpers
# =============================================================================

def _cache_contours_geom(contours_fc_proj, oid_subset=None) -> Dict[int, object]:
    d: Dict[int, object] = {}
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
        where = f"{delim_oid} IN ({','.join([str(x) for x in chunk])})"
        with arcpy.da.SearchCursor(contours_fc_proj, ["OID@", "SHAPE@"],
                                    where_clause=where) as cur:
            for oid, geom in cur:
                d[int(oid)] = geom
    return d

def _tangent_az_at_near(line_geom, near_pt_geom, step: float) -> Optional[float]:
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
        method=near_method)

def _run_near_table(spr_fc_proj, con_fc_proj, out_tbl, k: int,
                     near_method, search_radius):
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
        method=near_method)
    return out_tbl


# =============================================================================
# 4. Rotation methods (01..05)
# =============================================================================

def _method_01_neartangent(spr_fc_proj, con_geom, sr_work,
                            tangent_step: float, offset: float) -> Dict[int, tuple]:
    out: Dict[int, tuple] = {}
    with arcpy.da.SearchCursor(spr_fc_proj,
            ["SPR_TMPID", "NEAR_FID", "NEAR_X", "NEAR_Y"]) as cur:
        for sid, nf, nx, ny in cur:
            sid = int(sid)
            if nf is None or nx is None or ny is None or int(nf) < 0:
                out[sid] = (None, 0, "NO_NEAR"); continue
            line = con_geom.get(int(nf))
            if line is None:
                out[sid] = (None, 0, "NO_LINE"); continue
            pt = arcpy.PointGeometry(arcpy.Point(nx, ny), sr_work)
            az = _tangent_az_at_near(line, pt, tangent_step)
            if az is None:
                out[sid] = (None, 0, "TAN_FAIL")
            else:
                out[sid] = (_wrap360(az + offset), 1, "OK")
    return out

def _method_03_nearnormal(spr_fc_proj, offset: float) -> Dict[int, tuple]:
    out: Dict[int, tuple] = {}
    with arcpy.da.SearchCursor(spr_fc_proj,
            ["SPR_TMPID", "SHAPE@XY", "NEAR_X", "NEAR_Y"]) as cur:
        for sid, (sx, sy), nx, ny in cur:
            sid = int(sid)
            if nx is None or ny is None:
                out[sid] = (None, 0, "NO_NEAR"); continue
            az = _azimuth_geo_deg(nx - sx, ny - sy)
            if az is None:
                out[sid] = (None, 0, "ZERO")
            else:
                out[sid] = (_wrap360(az + offset), 1, "OK")
    return out

def _near_table_id_field(near_tbl) -> str:
    names = [f.name for f in arcpy.ListFields(near_tbl)]
    return "SPR_TMPID" if "SPR_TMPID" in names else "IN_FID"

def _method_02_highlow(near_tbl, elev_lu, spr_fc_proj,
                        offset: float) -> Dict[int, tuple]:
    fallback = _method_03_nearnormal(spr_fc_proj, offset)
    id_field = _near_table_id_field(near_tbl)
    recs: Dict[int, List] = {}
    with arcpy.da.SearchCursor(near_tbl,
            [id_field, "NEAR_FID", "NEAR_X", "NEAR_Y"]) as cur:
        for sid, nf, nx, ny in cur:
            if sid is None or nf is None or nx is None or ny is None:
                continue
            sid = int(sid)
            z = elev_lu.get(int(nf))
            if z is None:
                continue
            recs.setdefault(sid, []).append((float(z), float(nx), float(ny)))
    out: Dict[int, tuple] = {}
    for sid, fb in fallback.items():
        rows = recs.get(sid, [])
        if len(rows) < 2:
            out[sid] = (fb[0], 0, "FALLBACK_NEAR_NRM"); continue
        rows_sorted = sorted(rows, key=lambda t: t[0])
        low = rows_sorted[0]; high = rows_sorted[-1]
        if high[0] == low[0]:
            out[sid] = (fb[0], 0, "FLAT_FALLBACK"); continue
        az = _azimuth_geo_deg(low[1] - high[1], low[2] - high[2])
        if az is None:
            out[sid] = (fb[0], 0, "DEGEN_FALLBACK")
        else:
            out[sid] = (_wrap360(az + offset), 1, "OK")
    return out

def _method_04_planefit(near_tbl, elev_lu, offset: float) -> Dict[int, tuple]:
    id_field = _near_table_id_field(near_tbl)
    samples: Dict[int, List] = {}
    with arcpy.da.SearchCursor(near_tbl,
            [id_field, "NEAR_FID", "NEAR_X", "NEAR_Y"]) as cur:
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

    out: Dict[int, tuple] = {}
    for sid, pts in samples.items():
        if len(pts) < 3:
            out[sid] = (None, 0, "NEED_3PTS"); continue
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
            out[sid] = (None, 0, "SINGULAR"); continue
        Da = det3(sxz, sxy, sx, syz, syy, sy, sz, sy, n)
        Db = det3(sxx, sxz, sx, sxy, syz, sy, sx, sz, n)
        a = Da / D; b = Db / D
        az = _azimuth_geo_deg(-a, -b)
        if az is None:
            out[sid] = (None, 0, "ZERO_GRAD")
        else:
            out[sid] = (_wrap360(az + offset), 1, "OK")
    return out

def _method_05_centroidhl(near_tbl, elev_lu, offset: float) -> Dict[int, tuple]:
    id_field = _near_table_id_field(near_tbl)
    samples: Dict[int, List] = {}
    with arcpy.da.SearchCursor(near_tbl,
            [id_field, "NEAR_FID", "NEAR_X", "NEAR_Y"]) as cur:
        for sid, nf, nx, ny in cur:
            if sid is None or nf is None or nx is None or ny is None:
                continue
            sid = int(sid)
            z = elev_lu.get(int(nf))
            if z is None:
                continue
            samples.setdefault(sid, []).append((float(nx), float(ny), float(z)))
    out: Dict[int, tuple] = {}
    for sid, pts in samples.items():
        if len(pts) < 4:
            out[sid] = (None, 0, "NEED_4PTS"); continue
        zs = sorted([p[2] for p in pts])
        med = zs[len(zs) // 2]
        high = [p for p in pts if p[2] >= med]
        low = [p for p in pts if p[2] <= med]
        if not high or not low:
            out[sid] = (None, 0, "SPLIT_FAIL"); continue
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


# =============================================================================
# 5. Output writers
# =============================================================================

def _copy_output_base(spr_fc_tmp, out_gdb, out_name) -> str:
    out_fc = os.path.join(out_gdb, out_name)
    _safe_delete(out_fc)
    arcpy.management.CopyFeatures(spr_fc_tmp, out_fc)
    return out_fc

def _write_separate(out_base_fc, out_gdb, out_base_name, method_code,
                     results) -> str:
    out_fc = os.path.join(out_gdb, f"{out_base_name}_{method_code}")
    _safe_delete(out_fc)
    arcpy.management.CopyFeatures(out_base_fc, out_fc)
    _ensure_field(out_fc, "ROT", "DOUBLE")
    _ensure_field(out_fc, "OK", "SHORT")
    _ensure_field(out_fc, "NOTE", "TEXT", length=60)
    with arcpy.da.UpdateCursor(out_fc,
            ["SPR_TMPID", "ROT", "OK", "NOTE"]) as cur:
        for row in cur:
            sid = int(row[0])
            r, o, n = results.get(sid, (None, 0, "NO_DATA"))
            cur.updateRow([sid, r, o, n])
    return out_fc

def _write_single_fields(out_fc, results_by_method: dict) -> None:
    _ensure_field(out_fc, "ROT", "DOUBLE")
    methods = list(results_by_method.keys())
    for m in methods:
        _ensure_field(out_fc, f"ROT_{m}", "DOUBLE")
        _ensure_field(out_fc, f"OK_{m}", "SHORT")
        _ensure_field(out_fc, f"NOTE_{m}", "TEXT", length=60)
    field_list = ["SPR_TMPID", "ROT"]
    for m in methods:
        field_list.extend([f"ROT_{m}", f"OK_{m}", f"NOTE_{m}"])
    with arcpy.da.UpdateCursor(out_fc, field_list) as cur:
        for row in cur:
            active = None
            idx = 2
            for m in methods:
                rot, ok, note = results_by_method[m].get(int(row[0]),
                                                          (None, 0, "NO_DATA"))
                row[idx] = rot; row[idx + 1] = ok; row[idx + 2] = note
                if active is None and rot is not None:
                    active = rot
                idx += 3
            row[1] = active
            cur.updateRow(row)

def _write_summary_table(out_gdb, base_name, n_total, results_by_method) -> str:
    tbl = os.path.join(out_gdb, _validate_output_name(base_name + "_Summary", out_gdb))
    _safe_delete(tbl)
    arcpy.management.CreateTable(out_gdb, os.path.basename(tbl))
    arcpy.management.AddField(tbl, "METHOD", "TEXT", field_length=40)
    arcpy.management.AddField(tbl, "N_TOTAL", "LONG")
    arcpy.management.AddField(tbl, "N_OK", "LONG")
    arcpy.management.AddField(tbl, "N_FAIL", "LONG")
    arcpy.management.AddField(tbl, "OK_PCT", "DOUBLE")
    with arcpy.da.InsertCursor(tbl,
            ["METHOD", "N_TOTAL", "N_OK", "N_FAIL", "OK_PCT"]) as ic:
        for m, res in results_by_method.items():
            ok = sum(1 for sid, (rot, o, note) in res.items() if int(o) == 1)
            pct = (100.0 * ok / float(n_total)) if n_total > 0 else 0.0
            ic.insertRow([m, int(n_total), int(ok),
                          int(n_total - ok), float(pct)])
    return tbl



# =============================================================================
# 6. Main runner
# =============================================================================

def _shape_type(layer) -> str:
    try:
        return (arcpy.Describe(layer).shapeType or "").lower()
    except Exception:
        return ""

def _field_is_numeric(layer, field_name) -> bool:
    try:
        flds = arcpy.ListFields(layer, field_name)
        if not flds:
            return False
        t = (flds[0].type or "").lower()
        return t in ("double", "single", "integer", "smallinteger")
    except Exception:
        return False

def _add_to_current_map(dataset_path: str) -> None:
    try:
        aprx = arcpy.mp.ArcGISProject("CURRENT")
        m = aprx.activeMap
        if m:
            m.addDataFromPath(dataset_path)
    except Exception:
        pass

def _run_suite(springs_layer, contours_layer, elev_field, out_gdb, out_base_name,
               output_mode, create_summary, work_sr_mode, near_method,
               search_radius, global_offset, k_near, tangent_step,
               aoi_layer, aoi_buffer, cache_mode, profile,
               ignore_selection, run_01, run_02, run_03, run_04, run_05):
    t_all = time.time()

    # Selection-bypass
    _announce_selection("Springs", springs_layer)
    _announce_selection("Contours", contours_layer)

    springs_src = springs_layer
    contours_src = contours_layer
    if ignore_selection:
        springs_src = _resolve_full_source(springs_layer)
        contours_src = _resolve_full_source(contours_layer)
        _msg("Process-ALL mode ON -> using full datasets on disk (selection ignored).")

    spr_total_src = _get_count(springs_src)
    _msg(f"Springs available for processing: {spr_total_src}")

    sr_work = _pick_work_sr(springs_src, work_sr_mode)
    _msg(f"Working SR: {sr_work.name if sr_work else 'Unknown'}")
    _msg("Rotation: GEOGRAPHIC degrees (0=N, clockwise).")

    scratch = arcpy.env.scratchGDB

    sradius = None
    try:
        if search_radius is not None and float(search_radius) > 0:
            sradius = str(float(search_radius))
            _warn(f"Search radius = {sradius} (working SR units). Springs farther "
                  f"than this from any contour will NOT be rotated.")
    except Exception:
        sradius = None

    # Copy inputs
    t0 = time.time()
    spr_tmp = os.path.join(scratch, "spr_tmp_in")
    con_tmp = os.path.join(scratch, "con_tmp_in")
    _safe_delete(spr_tmp); _safe_delete(con_tmp)
    arcpy.management.CopyFeatures(springs_src, spr_tmp)
    arcpy.management.CopyFeatures(contours_src, con_tmp)
    _profile_msg(profile, "Copy inputs", t0)

    n_after_copy = _get_count(spr_tmp)
    _diag(f"springs after copy: {n_after_copy}")

    # AOI filter
    aoi_tmp = None
    if aoi_layer and arcpy.Exists(aoi_layer) and _get_count(aoi_layer) > 0:
        if _shape_type(aoi_layer) != "polygon":
            _warn("AOI is not polygon; AOI ignored.")
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
            _diag(f"springs inside AOI: {n_spr}")
            if n_spr == 0:
                raise arcpy.ExecuteError("AOI removed all springs. Check AOI position.")

    n_total = _get_count(spr_tmp)

    # Stable ID
    t0 = time.time()
    _ensure_field(spr_tmp, "SPR_TMPID", "LONG")
    oidf = _oid_field(spr_tmp)
    arcpy.management.CalculateField(spr_tmp, "SPR_TMPID", f"!{oidf}!", "PYTHON3")
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
        aoi_work, st = _project_and_buffer_aoi(aoi_tmp, sr_work, aoi_buffer,
                                                scratch, profile=profile)
        if aoi_work:
            con_sel = os.path.join(scratch, "con_tmp_proj_aoi")
            _filter_contours_by_aoi(con_p, aoi_work, con_sel, profile=profile)
            _safe_delete(con_p)
            con_p = con_sel
            _msg(f"AOI contours filtered ({st}).")

    n_contours = _get_count(con_p)
    _diag(f"contours used: {n_contours}")
    if n_contours <= 0:
        raise arcpy.ExecuteError("No contours available after filtering.")

    # Elevation lookup
    t0 = time.time()
    elev_lu: Dict[int, float] = {}
    n_bad_elev = 0
    with arcpy.da.SearchCursor(con_p, ["OID@", elev_field]) as cur:
        for oid, z in cur:
            if z is None:
                n_bad_elev += 1; continue
            elev_lu[int(oid)] = float(z)
    _profile_msg(profile, "Build elevation lookup", t0)
    if n_bad_elev:
        _warn(f"{n_bad_elev} contour(s) had NULL elevation -> ignored by elevation methods.")

    # Near (once)
    t0 = time.time()
    _msg("Running Near (LOCATION) once...")
    _run_near_location(spr_p, con_p, near_method, sradius)
    _profile_msg(profile, "Near (LOCATION)", t0)

    got_near = 0
    with arcpy.da.SearchCursor(spr_p, ["NEAR_FID"]) as cur:
        for (nf,) in cur:
            if nf is not None and int(nf) >= 0:
                got_near += 1
    _diag(f"springs with a NEAR contour: {got_near} / {n_total}")
    if got_near < n_total:
        _warn(f"{n_total - got_near} spring(s) found NO contour within the search radius.")

    # NearTable for K-based methods
    need_k = bool(run_02 or run_04 or run_05)
    near_tbl = os.path.join(scratch, "spr_near_tbl")
    if need_k:
        t0 = time.time()
        _msg(f"Running GenerateNearTable (K={int(k_near)})...")
        _run_near_table(spr_p, con_p, near_tbl, int(k_near), near_method, sradius)
        _profile_msg(profile, "GenerateNearTable", t0)
        try:
            existing = [f.name for f in arcpy.ListFields(near_tbl)]
            if "SPR_TMPID" in existing:
                arcpy.management.DeleteField(near_tbl, ["SPR_TMPID"])
            arcpy.management.JoinField(near_tbl, "IN_FID", spr_p,
                                        _oid_field(spr_p), ["SPR_TMPID"])
        except Exception:
            _warn("JoinField failed; K-based results may be less reliable.")

    # Cache contours geom (method 01)
    con_geom: Dict[int, object] = {}
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
            _msg(f"Cache NEAR_ONLY: {len(con_geom)} contours.")
        else:
            con_geom = _cache_contours_geom(con_p, oid_subset=None)
            _msg(f"Cache ALL: {len(con_geom)} contours.")
        _profile_msg(profile, "Cache contours", t0)

    # Run methods
    results: Dict[str, Dict[int, tuple]] = {}
    if run_01:
        t0 = time.time()
        _msg("Computing 01_NearTangent ...")
        results["01_NearTangent"] = _method_01_neartangent(
            spr_p, con_geom, sr_work, tangent_step, global_offset)
        _profile_msg(profile, "Method 01", t0)
    if run_02 and need_k:
        t0 = time.time()
        _msg("Computing 02_HighLow ...")
        results["02_HighLow"] = _method_02_highlow(
            near_tbl, elev_lu, spr_p, global_offset)
        _profile_msg(profile, "Method 02", t0)
    if run_03:
        t0 = time.time()
        _msg("Computing 03_NearNormal ...")
        results["03_NearNormal"] = _method_03_nearnormal(spr_p, global_offset)
        _profile_msg(profile, "Method 03", t0)
    if run_04 and need_k:
        t0 = time.time()
        _msg("Computing 04_PlaneFit ...")
        results["04_PlaneFit"] = _method_04_planefit(near_tbl, elev_lu, global_offset)
        _profile_msg(profile, "Method 04", t0)
    if run_05 and need_k:
        t0 = time.time()
        _msg("Computing 05_CentroidHL ...")
        results["05_CentroidHL"] = _method_05_centroidhl(near_tbl, elev_lu, global_offset)
        _profile_msg(profile, "Method 05", t0)

    if not results:
        raise arcpy.ExecuteError("No method selected. Enable at least one.")

    for mname, mres in results.items():
        ok = sum(1 for v in mres.values() if int(v[1]) == 1)
        _diag(f"{mname}: OK={ok} of {n_total}")

    # Output
    t0 = time.time()
    out_base_fc = _copy_output_base(spr_tmp, out_gdb, out_base_name)
    _profile_msg(profile, "Write base output", t0)

    out_paths: List[str] = []
    if output_mode == "SINGLE_FIELDS":
        _msg("Writing SINGLE_FIELDS output...")
        _write_single_fields(out_base_fc, results)
        out_paths.append(out_base_fc)
    else:
        _msg("Writing SEPARATE_LAYERS outputs...")
        for method_code, method_results in results.items():
            out_fc = _write_separate(out_base_fc, out_gdb, out_base_name,
                                      method_code, method_results)
            out_paths.append(out_fc)
        _safe_delete(out_base_fc)

    if create_summary:
        try:
            tbl = _write_summary_table(out_gdb, out_base_name, n_total, results)
            out_paths.append(tbl)
            _msg(f"Summary table: {tbl}")
        except Exception:
            _warn("Summary table failed (ignored).")

    # Add to map
    for pth in out_paths:
        try:
            if arcpy.Describe(pth).dataType.lower().find("feature") < 0:
                continue
        except Exception:
            continue
        _add_to_current_map(pth)

    _profile_msg(profile, "TOTAL", t_all)
    return out_paths


# =============================================================================
# 7. Toolbox + Tool class
# =============================================================================

def _set_category(param, cat):
    try:
        param.category = cat
    except Exception:
        pass


class Toolbox:
    def __init__(self):
        self.label = "Spring Rotation - Comparison Suite (Pro v4 native)"
        self.alias = "SpringRotationSuiteV4Pro"
        self.tools = [SpringRotationSuiteToolPro]


class SpringRotationSuiteToolPro:
    def __init__(self):
        self.label = "Spring Rotation Suite (Pro v4 native)"
        self.description = (
            "Compare spring rotations using 5 methods. Processes ALL springs "
            "by default (selection-bypass hardwired). Native Pro build.\n"
            "Maintainer: Ali Mirjafari")
        self.canRunInBackground = True

    def isLicensed(self) -> bool:
        return True

    def getParameterInfo(self):
        p = []
        CAT_IN = "01 Inputs"
        CAT_OUT = "02 Outputs"
        CAT_ADV = "03 Advanced"
        CAT_PROC = "04 Processing"
        CAT_AOI = "05 AOI"
        CAT_PERF = "06 Performance"
        CAT_METH = "07 Methods"

        p0 = arcpy.Parameter(displayName="Springs (Point layer)", name="springs",
                             datatype="GPFeatureLayer", parameterType="Required",
                             direction="Input")
        _set_category(p0, CAT_IN)
        p1 = arcpy.Parameter(displayName="Contours (Polyline layer)", name="contours",
                             datatype="GPFeatureLayer", parameterType="Required",
                             direction="Input")
        _set_category(p1, CAT_IN)
        p2 = arcpy.Parameter(displayName="Contour elevation field (numeric)",
                             name="elev_field", datatype="Field",
                             parameterType="Required", direction="Input")
        p2.parameterDependencies = [p1.name]
        _set_category(p2, CAT_IN)
        p_sel = arcpy.Parameter(
            displayName="Process ALL features (ignore any active selection)",
            name="ignore_selection", datatype="GPBoolean",
            parameterType="Optional", direction="Input")
        p_sel.value = True
        _set_category(p_sel, CAT_IN)

        p3 = arcpy.Parameter(displayName="Output file geodatabase (*.gdb)",
                             name="out_gdb", datatype="DEWorkspace",
                             parameterType="Required", direction="Input")
        _set_category(p3, CAT_OUT)
        p4 = arcpy.Parameter(displayName="Output feature class base name",
                             name="out_base_name", datatype="GPString",
                             parameterType="Required", direction="Input")
        p4.value = "springs_rotation_suite"
        _set_category(p4, CAT_OUT)
        p5 = arcpy.Parameter(displayName="Output layout", name="output_mode",
                             datatype="GPString", parameterType="Optional",
                             direction="Input")
        p5.filter.type = "ValueList"
        p5.filter.list = ["SEPARATE_LAYERS", "SINGLE_FIELDS"]
        p5.value = "SEPARATE_LAYERS"
        _set_category(p5, CAT_OUT)
        p6 = arcpy.Parameter(displayName="Create summary table",
                             name="create_summary", datatype="GPBoolean",
                             parameterType="Optional", direction="Input")
        p6.value = False
        _set_category(p6, CAT_OUT)

        p10 = arcpy.Parameter(displayName="Working coordinate system",
                              name="work_sr_mode", datatype="GPString",
                              parameterType="Optional", direction="Input")
        p10.filter.type = "ValueList"
        p10.filter.list = ["AUTO_UTM", "USE_INPUT"]
        p10.value = "AUTO_UTM"
        _set_category(p10, CAT_PROC)
        p11 = arcpy.Parameter(displayName="Near method", name="near_method",
                              datatype="GPString", parameterType="Optional",
                              direction="Input")
        p11.filter.type = "ValueList"
        p11.filter.list = ["PLANAR", "GEODESIC"]
        p11.value = "PLANAR"
        _set_category(p11, CAT_PROC)
        p12 = arcpy.Parameter(
            displayName="Search radius (working SR units; 0/empty = UNLIMITED)",
            name="search_radius", datatype="GPDouble",
            parameterType="Optional", direction="Input")
        p12.value = None
        _set_category(p12, CAT_PROC)

        p13 = arcpy.Parameter(displayName="Global rotation offset (degrees)",
                              name="global_offset", datatype="GPDouble",
                              parameterType="Optional", direction="Input")
        p13.value = 0.0
        _set_category(p13, CAT_ADV)
        p14 = arcpy.Parameter(displayName="K nearest contour hits (02/04/05)",
                              name="k_near", datatype="GPLong",
                              parameterType="Optional", direction="Input")
        p14.value = 8
        _set_category(p14, CAT_ADV)
        p15 = arcpy.Parameter(displayName="Tangent sampling step (map units; 01)",
                              name="tangent_step", datatype="GPDouble",
                              parameterType="Optional", direction="Input")
        p15.value = 5.0
        _set_category(p15, CAT_ADV)

        p16 = arcpy.Parameter(displayName="AOI polygon (optional)",
                              name="aoi_layer", datatype="GPFeatureLayer",
                              parameterType="Optional", direction="Input")
        _set_category(p16, CAT_AOI)
        p17 = arcpy.Parameter(
            displayName="AOI buffer for contours (working SR units)",
            name="aoi_buffer", datatype="GPDouble",
            parameterType="Optional", direction="Input")
        p17.value = 0.0
        _set_category(p17, CAT_AOI)

        p18 = arcpy.Parameter(displayName="Contour cache mode (method 01)",
                              name="cache_mode", datatype="GPString",
                              parameterType="Optional", direction="Input")
        p18.filter.type = "ValueList"
        p18.filter.list = ["NEAR_ONLY", "ALL"]
        p18.value = "NEAR_ONLY"
        _set_category(p18, CAT_PERF)
        p19 = arcpy.Parameter(displayName="Profiling messages",
                              name="profile", datatype="GPBoolean",
                              parameterType="Optional", direction="Input")
        p19.value = False
        _set_category(p19, CAT_PERF)

        m01 = arcpy.Parameter(displayName="01 NearTangent", name="run_01",
                              datatype="GPBoolean", parameterType="Optional",
                              direction="Input")
        m01.value = True
        _set_category(m01, CAT_METH)
        m02 = arcpy.Parameter(displayName="02 HighLow", name="run_02",
                              datatype="GPBoolean", parameterType="Optional",
                              direction="Input")
        m02.value = True
        _set_category(m02, CAT_METH)
        m03 = arcpy.Parameter(displayName="03 NearNormal", name="run_03",
                              datatype="GPBoolean", parameterType="Optional",
                              direction="Input")
        m03.value = False
        _set_category(m03, CAT_METH)
        m04 = arcpy.Parameter(displayName="04 PlaneFit", name="run_04",
                              datatype="GPBoolean", parameterType="Optional",
                              direction="Input")
        m04.value = False
        _set_category(m04, CAT_METH)
        m05 = arcpy.Parameter(displayName="05 CentroidHL", name="run_05",
                              datatype="GPBoolean", parameterType="Optional",
                              direction="Input")
        m05.value = False
        _set_category(m05, CAT_METH)

        p.extend([p0, p1, p2, p_sel, p3, p4, p5, p6,
                  p10, p11, p12, p13, p14, p15,
                  p16, p17, p18, p19,
                  m01, m02, m03, m04, m05])
        return p

    def updateParameters(self, parameters):
        try:
            run_01 = bool(parameters[18].value)
            run_02 = bool(parameters[19].value)
            run_04 = bool(parameters[21].value)
            run_05 = bool(parameters[22].value)
            need_k = bool(run_02 or run_04 or run_05)
            parameters[12].enabled = need_k      # K
            parameters[13].enabled = run_01      # tangent step
            parameters[16].enabled = run_01      # cache mode
        except Exception:
            pass

    def updateMessages(self, parameters):
        try:
            springs = parameters[0].valueAsText
            contours = parameters[1].valueAsText
            elev_field = parameters[2].valueAsText
            out_gdb = parameters[4].valueAsText

            if springs and _shape_type(springs) and _shape_type(springs) != "point":
                parameters[0].setWarningMessage("Springs should be POINT geometry.")
            if contours and _shape_type(contours) and _shape_type(contours) != "polyline":
                parameters[1].setWarningMessage("Contours should be POLYLINE geometry.")
            if springs:
                has_sel, n_sel = _selection_info(springs)
                if has_sel:
                    parameters[0].setWarningMessage(
                        f"Layer has {n_sel} selected feature(s). With 'Process ALL' ON "
                        f"the whole dataset is used.")
            if contours and elev_field:
                if not _field_is_numeric(contours, elev_field):
                    parameters[2].setErrorMessage("Elevation field must be numeric.")
            if out_gdb:
                if not out_gdb.lower().endswith(".gdb"):
                    parameters[4].setWarningMessage("Output should be a File GDB (*.gdb).")
                if not arcpy.Exists(out_gdb):
                    parameters[4].setErrorMessage(f"Output GDB not found: {out_gdb}")
        except Exception:
            pass

    def execute(self, parameters, messages):
        arcpy.env.overwriteOutput = True
        try:
            arcpy.env.parallelProcessingFactor = "100%"
        except Exception:
            pass

        springs = parameters[0].valueAsText
        contours = parameters[1].valueAsText
        elev_field = parameters[2].valueAsText
        ignore_selection = bool(parameters[3].value)
        out_gdb = parameters[4].valueAsText
        out_base_name_in = parameters[5].valueAsText
        output_mode = parameters[6].valueAsText or "SEPARATE_LAYERS"
        create_summary = bool(parameters[7].value)
        work_sr_mode = parameters[8].valueAsText or "AUTO_UTM"
        near_method = parameters[9].valueAsText or "PLANAR"
        search_radius = parameters[10].value
        global_offset = float(parameters[11].value or 0.0)
        k_near = int(parameters[12].value or 8)
        tangent_step = float(parameters[13].value or 5.0)
        aoi_layer = parameters[14].valueAsText
        aoi_buffer = parameters[15].value
        cache_mode = parameters[16].valueAsText or "NEAR_ONLY"
        profile = bool(parameters[17].value)
        run_01 = bool(parameters[18].value)
        run_02 = bool(parameters[19].value)
        run_03 = bool(parameters[20].value)
        run_04 = bool(parameters[21].value)
        run_05 = bool(parameters[22].value)

        out_base_name = _validate_output_name(out_base_name_in, out_gdb)

        _run_suite(
            springs_layer=springs,
            contours_layer=contours,
            elev_field=elev_field,
            out_gdb=out_gdb,
            out_base_name=out_base_name,
            output_mode=output_mode,
            create_summary=create_summary,
            work_sr_mode=work_sr_mode,
            near_method=near_method,
            search_radius=search_radius,
            global_offset=global_offset,
            k_near=k_near,
            tangent_step=tangent_step,
            aoi_layer=aoi_layer,
            aoi_buffer=aoi_buffer,
            cache_mode=cache_mode,
            profile=profile,
            ignore_selection=ignore_selection,
            run_01=run_01,
            run_02=run_02,
            run_03=run_03,
            run_04=run_04,
            run_05=run_05,
        )
