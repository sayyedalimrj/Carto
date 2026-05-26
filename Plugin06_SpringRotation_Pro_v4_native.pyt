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
