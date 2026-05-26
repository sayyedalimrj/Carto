# -*- coding: utf-8 -*-
"""
Plugin 06 - Spring Rotation Comparison Suite (ArcGIS Pro / Python 3) v4 NATIVE
==============================================================================
Native Pro rewrite of Plugin06_SpringRotation_ArcMap_v4_hardened.pyt.

Same math, same parameters, same defaults, same [DIAG] messages, same output
schema. This file is behaviorally equivalent to the ArcMap v4 hardened build
but uses modern Pro idioms: type hints, f-strings, arcpy.mp only, and
arcpy.CreateUniqueName for scratch staging.

Methods (rotation in GEOGRAPHIC degrees; 0=N, clockwise):
  01 NearTangent  - tangent at the nearest point on the closest contour
  02 HighLow      - high->low vector across K nearest contour samples
  03 NearNormal   - spring -> nearest contour point
  04 PlaneFit     - least-squares plane on K samples; gradient direction
  05 CentroidHL   - centroid(high) -> centroid(low) using median split

Original maintainer:
    Ali Mirjafari - 09186441801
Pro port:
    Kiro
"""

from __future__ import annotations

import os
import math
import time
import hashlib
import traceback
import uuid
from typing import Dict, Iterable, List, Optional, Tuple

import arcpy


# =============================================================================
# 0. Messaging + small utilities
# =============================================================================

def _safe_str(v) -> str:
    if v is None:
        return ""
    try:
        if isinstance(v, str):
            return v
        return str(v)
    except Exception:
        try:
            return repr(v)
        except Exception:
            return ""


def _msg(s) -> None:
    try:
        arcpy.AddMessage(_safe_str(s))
    except Exception:
        pass


def _warn(s) -> None:
    try:
        arcpy.AddWarning(_safe_str(s))
    except Exception:
        pass


def _err(s) -> None:
    try:
        arcpy.AddError(_safe_str(s))
    except Exception:
        pass


def _diag(s) -> None:
    _msg(f"[DIAG] {s}")


def _to_bytes_utf8(s) -> bytes:
    try:
        if isinstance(s, bytes):
            return s
        return str(s).encode("utf-8")
    except Exception:
        return b"x"


def _unique(prefix: str = "tmp") -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


def _wrap360(a: Optional[float]) -> Optional[float]:
    if a is None:
        return None
    a = float(a) % 360.0
    if a < 0:
        a += 360.0
    return a


def _azimuth_geo_deg(dx: float, dy: float) -> Optional[float]:
    """Geographic azimuth in degrees: 0=N, 90=E, 180=S, 270=W."""
    if dx == 0 and dy == 0:
        return None
    ang = math.degrees(math.atan2(dx, dy))  # atan2(E, N)
    if ang < 0:
        ang += 360.0
    return ang


def _safe_delete(path: Optional[str]) -> None:
    try:
        if path and arcpy.Exists(path):
            arcpy.management.Delete(path)
    except Exception:
        pass


def _profile_msg(enabled: bool, label: str, t0: float) -> None:
    if enabled:
        _msg(f"[PROFILE] {label}: {time.time() - t0:.3f}s")


# =============================================================================
# 1. Describe / SR helpers
# =============================================================================

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


def _shape_type(layer) -> str:
    try:
        return (arcpy.Describe(layer).shapeType or "").lower()
    except Exception:
        return ""


def _is_projected_meter(sr) -> bool:
    try:
        if sr is None:
            return False
        unit = (sr.linearUnitName or "").lower()
        return (sr.type == "Projected") and ("meter" in unit or "metre" in unit)
    except Exception:
        return False


def _utm_sr_for_lonlat(lon: float, lat: float) -> arcpy.SpatialReference:
    zone = int((lon + 180.0) / 6.0) + 1
    epsg = (32600 + zone) if lat >= 0 else (32700 + zone)
    return arcpy.SpatialReference(epsg)


def _pick_work_sr(spr_fc, mode: str) -> arcpy.SpatialReference:
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
        return _utm_sr_for_lonlat(center_wgs.firstPoint.X, center_wgs.firstPoint.Y)
    except Exception:
        return arcpy.SpatialReference(3857)


def _ensure_field(fc, name: str, ftype: str, length: int = 255) -> None:
    names = [f.name for f in arcpy.ListFields(fc)]
    if name in names:
        return
    if ftype.upper() == "TEXT":
        arcpy.management.AddField(fc, name, ftype, field_length=length)
    else:
        arcpy.management.AddField(fc, name, ftype)


def _field_is_numeric(layer, field_name: str) -> bool:
    try:
        flds = arcpy.ListFields(layer, field_name)
        if not flds:
            return False
        t = (flds[0].type or "").lower()
        return t in ("double", "single", "integer", "smallinteger")
    except Exception:
        return False


def _validate_output_name(name: str, workspace: Optional[str]) -> str:
    try:
        if workspace:
            return arcpy.ValidateTableName(name, workspace)
    except Exception:
        pass
    if not name:
        return "springs_rotation_suite"
    return "".join([c if (c.isalnum() or c == "_") else "_" for c in name])


def _color_from_code(code_str: str) -> dict:
    try:
        h = hashlib.md5(_to_bytes_utf8(code_str)).hexdigest()
        r = 80 + (int(h[0:2], 16) % 176)
        g = 80 + (int(h[2:4], 16) % 176)
        b = 80 + (int(h[4:6], 16) % 176)
        return {"RGB": [r, g, b, 100]}
    except Exception:
        return {"RGB": [0, 0, 0, 100]}


def _make_layer_name(prefix: str, seed: str) -> str:
    try:
        h = hashlib.md5(_to_bytes_utf8(seed or "x")).hexdigest()[:8]
    except Exception:
        h = "tmp"
    return f"{prefix}_{h}"


def _get_count(ds) -> int:
    try:
        return int(arcpy.management.GetCount(ds).getOutput(0))
    except Exception:
        return -1


def _ensure_scratch() -> str:
    sgdb = arcpy.env.scratchGDB
    if not sgdb or not arcpy.Exists(sgdb):
        sgdb = arcpy.env.scratchWorkspace
    if not sgdb or not arcpy.Exists(sgdb):
        raise arcpy.ExecuteError(
            "No scratch GDB available. Set arcpy.env.scratchGDB.")
    return sgdb


def _scratch_unique(prefix: str) -> str:
    return arcpy.CreateUniqueName(_unique(prefix), arcpy.env.scratchGDB)


# =============================================================================
# 2. Selection-bypass (the "selection trap" fix)
# =============================================================================

def _selection_info(layer) -> Tuple[bool, int]:
    """Return (has_selection, n_selected) for a feature layer."""
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
    """Return on-disk catalogPath of a layer so the FULL dataset is processed."""
    try:
        d = arcpy.Describe(layer)
        cp = getattr(d, "catalogPath", None)
        if cp and arcpy.Exists(cp):
            return cp
    except Exception:
        pass
    return layer


# =============================================================================
# 3. Pro map integration
# =============================================================================

def _add_to_current_map(dataset_path: str,
                        method_code: str,
                        symbology_source_layer: Optional[str],
                        auto_symbology: bool,
                        symbol_size: float) -> None:
    """Add a dataset to the active Pro map. (No arcpy.mapping fallback.)"""
    try:
        aprx = arcpy.mp.ArcGISProject("CURRENT")
    except Exception:
        return
    m = getattr(aprx, "activeMap", None)
    if m is None:
        return
    try:
        added = m.addDataFromPath(dataset_path)
    except Exception:
        _warn(f"Could not add to map: {dataset_path}")
        return

    # Apply symbology template if provided
    if symbology_source_layer:
        try:
            arcpy.management.ApplySymbologyFromLayer(added, symbology_source_layer)
        except Exception:
            _warn(f"ApplySymbologyFromLayer failed for: {dataset_path}")
        return

    # Optional auto symbology: rotation field + simple symbol size/color
    if auto_symbology:
        try:
            sym = added.symbology
            if hasattr(sym, "renderer") and hasattr(sym.renderer, "symbol"):
                try:
                    sym.renderer.symbol.size = float(symbol_size)
                except Exception:
                    pass
                try:
                    sym.renderer.symbol.color = _color_from_code(method_code)
                except Exception:
                    pass
                added.symbology = sym
        except Exception:
            pass


# =============================================================================
# 4. AOI helpers
# =============================================================================

def _project_and_buffer_aoi(aoi_fc,
                            sr_work,
                            buffer_dist: Optional[float],
                            scratch_gdb: str,
                            profile: bool = False) -> Tuple[Optional[str], str]:
    t0 = time.time()
    aoi_p = _scratch_unique("rot_aoi_p")
    aoi_b = _scratch_unique("rot_aoi_buf")
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
    _profile_msg(profile, "AOI project", t0)
    return aoi_p, "OK_NO_BUF"


def _filter_springs_by_aoi(spr_fc, aoi_fc, out_fc: str, profile: bool = False) -> str:
    t0 = time.time()
    _safe_delete(out_fc)
    spr_lyr = _make_layer_name("lyr_spr", spr_fc)
    arcpy.management.MakeFeatureLayer(spr_fc, spr_lyr)
    try:
        arcpy.management.SelectLayerByLocation(spr_lyr, "INTERSECT", aoi_fc)
        arcpy.management.CopyFeatures(spr_lyr, out_fc)
    finally:
        try:
            arcpy.management.Delete(spr_lyr)
        except Exception:
            pass
    _profile_msg(profile, "AOI filter springs", t0)
    return out_fc


def _filter_contours_by_aoi(con_fc_proj, aoi_fc_proj_or_buf,
                            out_fc: str, profile: bool = False) -> str:
    t0 = time.time()
    _safe_delete(out_fc)
    con_lyr = _make_layer_name("lyr_con", con_fc_proj)
    arcpy.management.MakeFeatureLayer(con_fc_proj, con_lyr)
    try:
        arcpy.management.SelectLayerByLocation(con_lyr, "INTERSECT", aoi_fc_proj_or_buf)
        arcpy.management.CopyFeatures(con_lyr, out_fc)
    finally:
        try:
            arcpy.management.Delete(con_lyr)
        except Exception:
            pass
    _profile_msg(profile, "AOI filter contours (projected)", t0)
    return out_fc


# =============================================================================
# 5. Near + geometry helpers
# =============================================================================

def _cache_contours_geom(contours_fc_proj,
                         oid_subset: Optional[Iterable[int]] = None) -> Dict[int, object]:
    d: Dict[int, object] = {}
    if not oid_subset:
        with arcpy.da.SearchCursor(contours_fc_proj, ["OID@", "SHAPE@"]) as cur:
            for oid, geom in cur:
                d[int(oid)] = geom
        return d
    oid_field = arcpy.Describe(contours_fc_proj).OIDFieldName
    oids = sorted({int(x) for x in oid_subset})
    if not oids:
        return d
    delim_oid = arcpy.AddFieldDelimiters(contours_fc_proj, oid_field)
    CHUNK = 500
    for i in range(0, len(oids), CHUNK):
        chunk = oids[i:i + CHUNK]
        where = f"{delim_oid} IN ({','.join(str(x) for x in chunk)})"
        with arcpy.da.SearchCursor(contours_fc_proj, ["OID@", "SHAPE@"],
                                   where_clause=where) as cur:
            for oid, geom in cur:
                d[int(oid)] = geom
    return d


def _tangent_az_at_near(line_geom, near_pt_geom, step: float) -> Optional[float]:
    try:
        step = float(step) if step and float(step) > 0 else 5.0
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


def _run_near_location(spr_fc_proj, con_fc_proj,
                       near_method: str,
                       search_radius: Optional[str]) -> None:
    arcpy.analysis.Near(
        in_features=spr_fc_proj,
        near_features=con_fc_proj,
        search_radius=search_radius,
        location="LOCATION",
        angle="NO_ANGLE",
        method=near_method,
    )


def _run_near_table(spr_fc_proj, con_fc_proj, out_tbl: str,
                    k: int, near_method: str,
                    search_radius: Optional[str]) -> str:
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
        method=near_method,
    )
    return out_tbl


def _near_table_id_field(near_tbl) -> str:
    names = [f.name for f in arcpy.ListFields(near_tbl)]
    return "SPR_TMPID" if "SPR_TMPID" in names else "IN_FID"


# =============================================================================
# 6. Methods 01..05
# =============================================================================

def _method_01_neartangent(spr_fc_proj, con_geom: Dict[int, object],
                           sr_work, tangent_step: float,
                           offset: float) -> Dict[int, Tuple[Optional[float], int, str]]:
    out: Dict[int, Tuple[Optional[float], int, str]] = {}
    with arcpy.da.SearchCursor(spr_fc_proj,
                               ["SPR_TMPID", "NEAR_FID", "NEAR_X", "NEAR_Y"]) as cur:
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


def _method_03_nearnormal(spr_fc_proj, offset: float
                          ) -> Dict[int, Tuple[Optional[float], int, str]]:
    out: Dict[int, Tuple[Optional[float], int, str]] = {}
    with arcpy.da.SearchCursor(spr_fc_proj,
                               ["SPR_TMPID", "SHAPE@XY", "NEAR_X", "NEAR_Y"]) as cur:
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


def _method_02_highlow(near_tbl, elev_lu: Dict[int, float],
                       spr_fc_proj, offset: float
                       ) -> Dict[int, Tuple[Optional[float], int, str]]:
    fallback = _method_03_nearnormal(spr_fc_proj, offset)
    id_field = _near_table_id_field(near_tbl)
    recs: Dict[int, List[Tuple[float, float, float]]] = {}
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
    out: Dict[int, Tuple[Optional[float], int, str]] = {}
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


def _method_04_planefit(near_tbl, elev_lu: Dict[int, float],
                        offset: float
                        ) -> Dict[int, Tuple[Optional[float], int, str]]:
    id_field = _near_table_id_field(near_tbl)
    samples: Dict[int, List[Tuple[float, float, float]]] = {}
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

    def det3(a11, a12, a13, a21, a22, a23, a31, a32, a33) -> float:
        return (a11 * (a22 * a33 - a23 * a32)
                - a12 * (a21 * a33 - a23 * a31)
                + a13 * (a21 * a32 - a22 * a31))

    out: Dict[int, Tuple[Optional[float], int, str]] = {}
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


def _method_05_centroidhl(near_tbl, elev_lu: Dict[int, float],
                          offset: float
                          ) -> Dict[int, Tuple[Optional[float], int, str]]:
    id_field = _near_table_id_field(near_tbl)
    samples: Dict[int, List[Tuple[float, float, float]]] = {}
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
    out: Dict[int, Tuple[Optional[float], int, str]] = {}
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


# =============================================================================
# 7. Output writers
# =============================================================================

def _copy_output_base(spr_fc_tmp, out_gdb: str, out_name: str) -> str:
    out_fc = os.path.join(out_gdb, out_name)
    _safe_delete(out_fc)
    arcpy.management.CopyFeatures(spr_fc_tmp, out_fc)
    return out_fc


def _write_separate(out_base_fc: str, out_gdb: str, out_base_name: str,
                    method_code: str,
                    results: Dict[int, Tuple[Optional[float], int, str]]) -> str:
    out_fc = os.path.join(out_gdb, f"{out_base_name}_{method_code}")
    _safe_delete(out_fc)
    arcpy.management.CopyFeatures(out_base_fc, out_fc)
    _ensure_field(out_fc, "ROT", "DOUBLE")
    _ensure_field(out_fc, "OK", "SHORT")
    _ensure_field(out_fc, "NOTE", "TEXT", length=60)
    with arcpy.da.UpdateCursor(out_fc, ["SPR_TMPID", "ROT", "OK", "NOTE"]) as cur:
        for sid, _r, _o, _n in cur:
            sid = int(sid)
            r, o, n = results.get(sid, (None, 0, "NO_DATA"))
            cur.updateRow([sid, r, o, n])
    return out_fc


def _write_single_fields(out_fc: str,
                         results_by_method: Dict[str, Dict[int, Tuple[Optional[float], int, str]]]
                         ) -> None:
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
            active: Optional[float] = None
            idx = 2
            for m in methods:
                rot, ok, note = results_by_method[m].get(int(row[0]), (None, 0, "NO_DATA"))
                row[idx] = rot
                row[idx + 1] = ok
                row[idx + 2] = note
                if active is None and rot is not None:
                    active = rot
                idx += 3
            row[1] = active
            cur.updateRow(row)


def _write_summary_table(out_gdb: str, base_name: str,
                         n_total: int,
                         results_by_method: Dict[str, Dict[int, Tuple[Optional[float], int, str]]]
                         ) -> str:
    tbl_name = _validate_output_name(base_name + "_Summary", out_gdb)
    tbl = os.path.join(out_gdb, tbl_name)
    _safe_delete(tbl)
    arcpy.management.CreateTable(out_gdb, os.path.basename(tbl))
    arcpy.management.AddField(tbl, "METHOD", "TEXT", field_length=40)
    arcpy.management.AddField(tbl, "N_TOTAL", "LONG")
    arcpy.management.AddField(tbl, "N_OK", "LONG")
    arcpy.management.AddField(tbl, "N_FAIL", "LONG")
    arcpy.management.AddField(tbl, "OK_PCT", "DOUBLE")
    with arcpy.da.InsertCursor(
            tbl, ["METHOD", "N_TOTAL", "N_OK", "N_FAIL", "OK_PCT"]) as ic:
        for m, mres in results_by_method.items():
            ok = sum(1 for v in mres.values() if int(v[1]) == 1)
            pct = (100.0 * ok / float(n_total)) if n_total > 0 else 0.0
            ic.insertRow([m, int(n_total), int(ok), int(n_total - ok), float(pct)])
    return tbl


# =============================================================================
# 8. Main runner
# =============================================================================

def _run_suite(springs_layer, contours_layer, elev_field: str,
               out_gdb: str, out_base_name: str,
               output_mode: str, create_summary: bool,
               symbology_source_layer: Optional[str], auto_symbology: bool,
               symbol_size: Optional[float],
               work_sr_mode: str, near_method: str,
               search_radius: Optional[float],
               global_offset: float, k_near: int, tangent_step: float,
               aoi_layer: Optional[str], aoi_buffer: Optional[float],
               cache_mode: str, profile: bool, ignore_selection: bool,
               run_01: bool, run_02: bool, run_03: bool,
               run_04: bool, run_05: bool) -> List[str]:
    t_all = time.time()

    # ---- Selection-trap guard --------------------------------------------
    spr_has_sel, spr_nsel = _selection_info(springs_layer)
    con_has_sel, con_nsel = _selection_info(contours_layer)
    if spr_has_sel:
        _warn(f"Springs layer has an ACTIVE SELECTION ({spr_nsel} features).")
    if con_has_sel:
        _warn(f"Contours layer has an ACTIVE SELECTION ({con_nsel} features).")

    if ignore_selection:
        springs_src = _resolve_full_source(springs_layer)
        contours_src = _resolve_full_source(contours_layer)
        if spr_has_sel or con_has_sel:
            _msg("Process-ALL mode ON -> using full datasets on disk (selection ignored).")
    else:
        springs_src = springs_layer
        contours_src = contours_layer
        if spr_has_sel or con_has_sel:
            _warn("Process-ALL mode OFF -> ONLY the selected features will be processed.")

    spr_total_src = _get_count(springs_src)
    _msg(f"Springs available for processing: {spr_total_src}")

    sr_work = _pick_work_sr(springs_src, work_sr_mode)
    _msg(f"Working SR: {sr_work.name if sr_work else 'Unknown'}")
    _msg("Rotation: GEOGRAPHIC degrees (0=N, clockwise).")

    _ensure_scratch()
    scratch = arcpy.env.scratchGDB

    # Normalize search radius
    sradius: Optional[str] = None
    try:
        if search_radius is not None and float(search_radius) > 0:
            sradius = str(float(search_radius))
            _warn(f"Search radius = {sradius} (working SR units). Springs farther than "
                  "this from any contour will NOT be rotated.")
    except Exception:
        sradius = None

    # Copy inputs (full source when ignore_selection=True)
    t0 = time.time()
    spr_tmp = _scratch_unique("spr_tmp_in")
    con_tmp = _scratch_unique("con_tmp_in")
    arcpy.management.CopyFeatures(springs_src, spr_tmp)
    arcpy.management.CopyFeatures(contours_src, con_tmp)
    _profile_msg(profile, "Copy inputs", t0)
    _diag(f"springs after copy: {_get_count(spr_tmp)}")

    # AOI filter on springs
    aoi_tmp: Optional[str] = None
    if aoi_layer and arcpy.Exists(aoi_layer) and _get_count(aoi_layer) > 0:
        if _shape_type(aoi_layer) != "polygon":
            _warn("AOI is not polygon; AOI ignored.")
        else:
            t0 = time.time()
            aoi_tmp = _scratch_unique("rot_tmp_aoi_in")
            arcpy.management.CopyFeatures(aoi_layer, aoi_tmp)
            _profile_msg(profile, "Copy AOI", t0)
            spr_sel = _scratch_unique("spr_tmp_in_aoi")
            _filter_springs_by_aoi(spr_tmp, aoi_tmp, spr_sel, profile=profile)
            _safe_delete(spr_tmp)
            spr_tmp = spr_sel
            n_spr = _get_count(spr_tmp)
            _diag(f"springs inside AOI: {n_spr}")
            if n_spr == 0:
                raise arcpy.ExecuteError("AOI removed all springs. Check AOI position.")

    n_total = _get_count(spr_tmp)

    # SPR_TMPID -- shapefile-safe via dynamic OID
    t0 = time.time()
    _ensure_field(spr_tmp, "SPR_TMPID", "LONG")
    oidf = _oid_field(spr_tmp)
    arcpy.management.CalculateField(spr_tmp, "SPR_TMPID", f"!{oidf}!", "PYTHON3")
    _profile_msg(profile, "Add SPR_TMPID", t0)

    # Project to working SR
    t0 = time.time()
    spr_p = _scratch_unique("spr_tmp_proj")
    con_p = _scratch_unique("con_tmp_proj")
    arcpy.management.Project(spr_tmp, spr_p, sr_work)
    arcpy.management.Project(con_tmp, con_p, sr_work)
    _profile_msg(profile, "Project to working SR", t0)

    # AOI filter contours (projected + buffered)
    if aoi_tmp:
        aoi_work, st = _project_and_buffer_aoi(aoi_tmp, sr_work, aoi_buffer,
                                               scratch, profile=profile)
        if aoi_work:
            con_sel = _scratch_unique("con_tmp_proj_aoi")
            _filter_contours_by_aoi(con_p, aoi_work, con_sel, profile=profile)
            _safe_delete(con_p)
            con_p = con_sel
            _msg(f"AOI contours filtered ({st}).")

    n_contours = _get_count(con_p)
    _diag(f"contours used: {n_contours}")
    if n_contours <= 0:
        raise arcpy.ExecuteError(
            "No contours available after filtering. Cannot compute rotations.")

    # Elevation lookup
    t0 = time.time()
    elev_lu: Dict[int, float] = {}
    n_bad_elev = 0
    with arcpy.da.SearchCursor(con_p, ["OID@", elev_field]) as cur:
        for oid, z in cur:
            if z is None:
                n_bad_elev += 1
                continue
            elev_lu[int(oid)] = float(z)
    _profile_msg(profile, "Build elevation lookup", t0)
    if n_bad_elev:
        _warn(f"[DIAG] {n_bad_elev} contour(s) had NULL elevation -> ignored by elevation methods.")

    # Near (single) for methods 01 / 03
    t0 = time.time()
    _msg("Running Near (LOCATION) once...")
    _run_near_location(spr_p, con_p, near_method, sradius)
    _profile_msg(profile, "Near (LOCATION)", t0)

    # DIAG: how many springs got a near contour
    got_near = 0
    with arcpy.da.SearchCursor(spr_p, ["NEAR_FID"]) as cur:
        for (nf,) in cur:
            if nf is not None and int(nf) >= 0:
                got_near += 1
    _diag(f"springs with a NEAR contour: {got_near} / {n_total}")
    if got_near < n_total:
        _warn(f"[DIAG] {n_total - got_near} spring(s) found NO contour within the "
              "search radius (they will not be rotated). Increase / clear the "
              "search radius if unexpected.")

    # GenerateNearTable for K-based methods
    need_k = bool(run_02 or run_04 or run_05)
    near_tbl = _scratch_unique("spr_near_tbl")
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

    # Cache contour geometries (method 01)
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

    # Compute methods
    results: Dict[str, Dict[int, Tuple[Optional[float], int, str]]] = {}
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
        results["04_PlaneFit"] = _method_04_planefit(
            near_tbl, elev_lu, global_offset)
        _profile_msg(profile, "Method 04", t0)
    if run_05 and need_k:
        t0 = time.time()
        _msg("Computing 05_CentroidHL ...")
        results["05_CentroidHL"] = _method_05_centroidhl(
            near_tbl, elev_lu, global_offset)
        _profile_msg(profile, "Method 05", t0)

    if not results:
        raise arcpy.ExecuteError(
            "No method selected. Enable at least one of methods 01..05.")

    for mname, mres in results.items():
        ok = sum(1 for v in mres.values() if int(v[1]) == 1)
        rotated = sum(1 for v in mres.values() if v[0] is not None)
        _diag(f"{mname}: rotated={rotated}  OK={ok}  of {n_total}")

    # Output base
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
        for method_code, mres in results.items():
            out_fc = _write_separate(out_base_fc, out_gdb, out_base_name,
                                     method_code, mres)
            out_paths.append(out_fc)
        _safe_delete(out_base_fc)

    if create_summary:
        try:
            tbl = _write_summary_table(out_gdb, out_base_name, n_total, results)
            out_paths.append(tbl)
            _msg(f"Summary table created: {tbl}")
        except Exception:
            _warn("Summary table failed (ignored).")

    # Add to active map
    t0 = time.time()
    for pth in out_paths:
        low = pth.lower()
        if low.endswith(".dbf") or low.endswith(".csv"):
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
            symbol_size=float(symbol_size) if symbol_size else 40.0,
        )
    _profile_msg(profile, "Add outputs to map", t0)
    _profile_msg(profile, "TOTAL", t_all)
    return out_paths


# =============================================================================
# 9. Toolbox + Tool class go in Part 3b (next response).
# =============================================================================



# =============================================================================
# 9. Python Toolbox (Pro UI)
# =============================================================================
#
# Parameter index map (kept identical to the ArcMap Py2.7 v4 build so saved
# tool history stays compatible):
#
#  0 springs            (GPFeatureLayer, point)        cat 01 Inputs
#  1 contours           (GPFeatureLayer, polyline)     cat 01 Inputs
#  2 elev_field         (Field, depends on contours)   cat 01 Inputs
#  3 ignore_selection   (GPBoolean, default True)      cat 01 Inputs
#  4 out_gdb            (DEWorkspace)                  cat 02 Outputs
#  5 out_base_name      (GPString)                     cat 02 Outputs
#  6 output_mode        (SEPARATE_LAYERS|SINGLE_FIELDS)cat 02 Outputs
#  7 create_summary     (GPBoolean)                    cat 02 Outputs
#  8 sym_layer          (GPFeatureLayer, optional)     cat 05 Symbology
#  9 auto_symbology     (GPBoolean)                    cat 05 Symbology
# 10 symbol_size        (GPDouble)                     cat 05 Symbology
# 11 work_sr_mode       (AUTO_UTM|USE_INPUT)           cat 04 Processing
# 12 near_method        (PLANAR|GEODESIC)              cat 04 Processing
# 13 search_radius      (GPDouble, optional)           cat 04 Processing
# 14 global_offset      (GPDouble)                     cat 03 Advanced
# 15 k_near             (GPLong, default 8)            cat 03 Advanced
# 16 tangent_step       (GPDouble, default 5.0)        cat 03 Advanced
# 17 aoi_sketch         (GPFeatureRecordSetLayer)      cat 06 AOI
# 18 aoi_buffer         (GPDouble)                     cat 06 AOI
# 19 cache_mode         (NEAR_ONLY|ALL)                cat 07 Performance
# 20 profile            (GPBoolean)                    cat 07 Performance
# 21 run_01             (GPBoolean, True)              cat 08 Methods
# 22 run_02             (GPBoolean, True)              cat 08 Methods
# 23 run_03             (GPBoolean, False)             cat 08 Methods
# 24 run_04             (GPBoolean, False)             cat 08 Methods
# 25 run_05             (GPBoolean, False)             cat 08 Methods
# 26 outputs            (Derived GPString)             cat 02 Outputs


def _set_category(param, cat: str) -> None:
    try:
        param.category = cat
    except Exception:
        pass


def _make_aoi_featureset_schema() -> "arcpy.FeatureSet":
    """Build an empty polygon FeatureSet so the AOI sketch parameter
    pre-populates with a polygon schema in the Pro dialog."""
    fs = arcpy.FeatureSet()
    try:
        scratch = arcpy.env.scratchGDB
        schema_fc = os.path.join(scratch, "rot_aoi_schema")
        _safe_delete(schema_fc)
        arcpy.management.CreateFeatureclass(
            out_path=scratch,
            out_name="rot_aoi_schema",
            geometry_type="POLYGON",
            spatial_reference=arcpy.SpatialReference(4326),
        )
        fs.load(schema_fc)
    except Exception:
        pass
    return fs


class Toolbox(object):
    def __init__(self):
        self.label = "Spring Rotation - Comparison Suite (Pro v4 native)"
        self.alias = "SpringRotationSuiteProV4"
        self.tools = [SpringRotationFinalSuiteTool]


class SpringRotationFinalSuiteTool(object):
    def __init__(self):
        self.label = "Spring Rotation, Final Tool (Pro v4 native), Mirjafari"
        self.description = (
            "Compare spring rotations using up to 5 methods. Processes ALL "
            "springs by default. Native ArcGIS Pro / Python 3 build. "
            "Maintainer: Ali Mirjafari, 09186441801"
        )
        self.canRunInBackground = False

    def isLicensed(self) -> bool:
        return True

    # -------------------------------------------------------------------------
    # Parameters
    # -------------------------------------------------------------------------
    def getParameterInfo(self):
        CAT_IN   = "01 Inputs"
        CAT_OUT  = "02 Outputs"
        CAT_ADV  = "03 Advanced"
        CAT_PROC = "04 Processing"
        CAT_SYM  = "05 Symbology"
        CAT_AOI  = "06 AOI"
        CAT_PERF = "07 Performance"
        CAT_METH = "08 Methods"

        # 0 springs
        p0 = arcpy.Parameter(
            displayName="Springs (Point layer)", name="springs",
            datatype="GPFeatureLayer", parameterType="Required",
            direction="Input")
        _set_category(p0, CAT_IN)

        # 1 contours
        p1 = arcpy.Parameter(
            displayName="Contours (Polyline layer)", name="contours",
            datatype="GPFeatureLayer", parameterType="Required",
            direction="Input")
        _set_category(p1, CAT_IN)

        # 2 elev_field (depends on contours)
        p2 = arcpy.Parameter(
            displayName="Contour elevation field (numeric)", name="elev_field",
            datatype="Field", parameterType="Required", direction="Input")
        p2.parameterDependencies = [p1.name]
        _set_category(p2, CAT_IN)

        # 3 ignore_selection (the selection-trap fix)
        p3 = arcpy.Parameter(
            displayName="Process ALL features (ignore any active selection)",
            name="ignore_selection", datatype="GPBoolean",
            parameterType="Optional", direction="Input")
        p3.value = True
        _set_category(p3, CAT_IN)

        # 4 out_gdb
        p4 = arcpy.Parameter(
            displayName="Output file geodatabase (*.gdb)", name="out_gdb",
            datatype="DEWorkspace", parameterType="Required", direction="Input")
        _set_category(p4, CAT_OUT)

        # 5 out_base_name
        p5 = arcpy.Parameter(
            displayName="Output feature class base name", name="out_base_name",
            datatype="GPString", parameterType="Required", direction="Input")
        p5.value = "springs_rotation_suite"
        _set_category(p5, CAT_OUT)

        # 6 output_mode
        p6 = arcpy.Parameter(
            displayName="Output layout", name="output_mode",
            datatype="GPString", parameterType="Optional", direction="Input")
        p6.filter.type = "ValueList"
        p6.filter.list = ["SEPARATE_LAYERS", "SINGLE_FIELDS"]
        p6.value = "SEPARATE_LAYERS"
        _set_category(p6, CAT_OUT)

        # 7 create_summary
        p7 = arcpy.Parameter(
            displayName="Create summary table (OK counts per method)",
            name="create_summary", datatype="GPBoolean",
            parameterType="Optional", direction="Input")
        p7.value = False
        _set_category(p7, CAT_OUT)

        # 8 sym_layer
        p8 = arcpy.Parameter(
            displayName="Symbology template layer (optional; from current map)",
            name="sym_layer", datatype="GPFeatureLayer",
            parameterType="Optional", direction="Input")
        _set_category(p8, CAT_SYM)

        # 9 auto_symbology (Pro: simple symbol size/color when no template)
        p9 = arcpy.Parameter(
            displayName="Auto-apply simple symbology (if no template)",
            name="auto_symbology", datatype="GPBoolean",
            parameterType="Optional", direction="Input")
        p9.value = True
        _set_category(p9, CAT_SYM)

        # 10 symbol_size
        p10 = arcpy.Parameter(
            displayName="Symbol size (points; auto-symbology)",
            name="symbol_size", datatype="GPDouble",
            parameterType="Optional", direction="Input")
        p10.value = 40.0
        _set_category(p10, CAT_SYM)

        # 11 work_sr_mode
        p11 = arcpy.Parameter(
            displayName="Working coordinate system (recommended: AUTO_UTM)",
            name="work_sr_mode", datatype="GPString",
            parameterType="Optional", direction="Input")
        p11.filter.type = "ValueList"
        p11.filter.list = ["AUTO_UTM", "USE_INPUT"]
        p11.value = "AUTO_UTM"
        _set_category(p11, CAT_PROC)

        # 12 near_method
        p12 = arcpy.Parameter(
            displayName="Near method (recommended: PLANAR with AUTO_UTM)",
            name="near_method", datatype="GPString",
            parameterType="Optional", direction="Input")
        p12.filter.type = "ValueList"
        p12.filter.list = ["PLANAR", "GEODESIC"]
        p12.value = "PLANAR"
        _set_category(p12, CAT_PROC)

        # 13 search_radius
        p13 = arcpy.Parameter(
            displayName="Search radius (optional; working SR units; "
                        "0/empty = UNLIMITED)",
            name="search_radius", datatype="GPDouble",
            parameterType="Optional", direction="Input")
        p13.value = None
        _set_category(p13, CAT_PROC)

        # 14 global_offset
        p14 = arcpy.Parameter(
            displayName="Global rotation offset (degrees; added to all methods)",
            name="global_offset", datatype="GPDouble",
            parameterType="Optional", direction="Input")
        p14.value = 0.0
        _set_category(p14, CAT_ADV)

        # 15 k_near
        p15 = arcpy.Parameter(
            displayName="K nearest contour hits (used by 02/04/05)",
            name="k_near", datatype="GPLong",
            parameterType="Optional", direction="Input")
        p15.value = 8
        _set_category(p15, CAT_ADV)

        # 16 tangent_step
        p16 = arcpy.Parameter(
            displayName="Tangent sampling step (map units; used by 01)",
            name="tangent_step", datatype="GPDouble",
            parameterType="Optional", direction="Input")
        p16.value = 5.0
        _set_category(p16, CAT_ADV)

        # 17 aoi_sketch
        p17 = arcpy.Parameter(
            displayName="AOI sketch (polygon)", name="aoi_sketch",
            datatype="GPFeatureRecordSetLayer",
            parameterType="Optional", direction="Input")
        p17.value = _make_aoi_featureset_schema()
        _set_category(p17, CAT_AOI)

        # 18 aoi_buffer
        p18 = arcpy.Parameter(
            displayName="AOI buffer for contours (working SR units; optional)",
            name="aoi_buffer", datatype="GPDouble",
            parameterType="Optional", direction="Input")
        p18.value = 0.0
        _set_category(p18, CAT_AOI)

        # 19 cache_mode
        p19 = arcpy.Parameter(
            displayName="Contour cache mode (method 01 only)",
            name="cache_mode", datatype="GPString",
            parameterType="Optional", direction="Input")
        p19.filter.type = "ValueList"
        p19.filter.list = ["NEAR_ONLY", "ALL"]
        p19.value = "NEAR_ONLY"
        _set_category(p19, CAT_PERF)

        # 20 profile
        p20 = arcpy.Parameter(
            displayName="Profiling messages (show timings)",
            name="profile", datatype="GPBoolean",
            parameterType="Optional", direction="Input")
        p20.value = False
        _set_category(p20, CAT_PERF)

        # 21..25 method toggles
        m01 = arcpy.Parameter(
            displayName="01 NearTangent - tangent at nearest contour point",
            name="run_01", datatype="GPBoolean",
            parameterType="Optional", direction="Input")
        m01.value = True
        _set_category(m01, CAT_METH)

        m02 = arcpy.Parameter(
            displayName="02 HighLow - high->low vector from K projected contour points",
            name="run_02", datatype="GPBoolean",
            parameterType="Optional", direction="Input")
        m02.value = True
        _set_category(m02, CAT_METH)

        m03 = arcpy.Parameter(
            displayName="03 NearNormal - spring->nearest contour point",
            name="run_03", datatype="GPBoolean",
            parameterType="Optional", direction="Input")
        m03.value = False
        _set_category(m03, CAT_METH)

        m04 = arcpy.Parameter(
            displayName="04 PlaneFit - least-squares plane from K samples",
            name="run_04", datatype="GPBoolean",
            parameterType="Optional", direction="Input")
        m04.value = False
        _set_category(m04, CAT_METH)

        m05 = arcpy.Parameter(
            displayName="05 CentroidHL - centroid(high)->centroid(low)",
            name="run_05", datatype="GPBoolean",
            parameterType="Optional", direction="Input")
        m05.value = False
        _set_category(m05, CAT_METH)

        # 26 derived output
        outp = arcpy.Parameter(
            displayName="Outputs (semicolon-separated paths)",
            name="outputs", datatype="GPString",
            parameterType="Derived", direction="Output")
        _set_category(outp, CAT_OUT)

        return [p0, p1, p2, p3, p4, p5, p6, p7, p8, p9, p10,
                p11, p12, p13, p14, p15, p16, p17, p18, p19, p20,
                m01, m02, m03, m04, m05, outp]

    # -------------------------------------------------------------------------
    # Parameter UI behavior
    # -------------------------------------------------------------------------
    def updateParameters(self, parameters):
        # Indices match getParameterInfo above.
        try:
            sym_layer = parameters[8].valueAsText
            has_template = bool(sym_layer)
            parameters[9].enabled = (not has_template)   # auto_symbology
            parameters[10].enabled = (not has_template)  # symbol_size

            run_01 = bool(parameters[21].value)
            run_02 = bool(parameters[22].value)
            run_04 = bool(parameters[24].value)
            run_05 = bool(parameters[25].value)
            need_k = bool(run_02 or run_04 or run_05)

            parameters[15].enabled = need_k    # k_near
            parameters[16].enabled = run_01    # tangent_step
            parameters[19].enabled = run_01    # cache_mode

            try:
                aoi_txt = parameters[17].valueAsText
                parameters[18].enabled = bool(aoi_txt)  # aoi_buffer
            except Exception:
                parameters[18].enabled = True
        except Exception:
            pass
        return

    def updateMessages(self, parameters):
        try:
            springs       = parameters[0].valueAsText
            contours      = parameters[1].valueAsText
            elev_field    = parameters[2].valueAsText
            out_gdb       = parameters[4].valueAsText
            out_base_name = parameters[5].valueAsText

            # Geometry-type guards
            if springs and _shape_type(springs) and _shape_type(springs) != "point":
                parameters[0].setWarningMessage("Springs should be POINT geometry.")
            if contours and _shape_type(contours) and _shape_type(contours) != "polyline":
                parameters[1].setWarningMessage("Contours should be POLYLINE geometry.")

            # Live selection warning
            try:
                if springs:
                    has_sel, n_sel = _selection_info(springs)
                    if has_sel:
                        parameters[0].setWarningMessage(
                            f"This layer has {n_sel} selected feature(s). With "
                            "'Process ALL features' ON the whole dataset is used; "
                            "turn it OFF only if you intend to limit to the "
                            "selection.")
            except Exception:
                pass

            # Elevation field must be numeric
            if contours and elev_field:
                if not _field_is_numeric(contours, elev_field):
                    parameters[2].setErrorMessage(
                        "Elevation field must be numeric (Integer/Float/Double).")

            # Output GDB checks
            if out_gdb:
                if not out_gdb.lower().endswith(".gdb"):
                    parameters[4].setWarningMessage(
                        "Recommended output is a File Geodatabase (*.gdb).")
                if not arcpy.Exists(out_gdb):
                    parameters[4].setErrorMessage(
                        f"Output geodatabase does not exist: {out_gdb}")

            # Output name normalization preview
            if out_gdb and out_base_name:
                v = _validate_output_name(out_base_name, out_gdb)
                if v != out_base_name:
                    parameters[5].setWarningMessage(
                        f"Output name will be validated to: {v}")

            # K guard
            try:
                if parameters[15].enabled:
                    k = parameters[15].value
                    if k is not None and int(k) < 2:
                        parameters[15].setErrorMessage(
                            "K must be >= 2 for methods 02/04/05.")
            except Exception:
                pass

            # tangent_step guard
            try:
                if parameters[16].enabled:
                    st = parameters[16].value
                    if st is not None and float(st) <= 0:
                        parameters[16].setErrorMessage(
                            "Tangent step must be > 0.")
            except Exception:
                pass

            # AOI buffer guard
            try:
                bd = parameters[18].value
                if bd is not None and float(bd) < 0:
                    parameters[18].setErrorMessage(
                        "AOI buffer must be >= 0.")
            except Exception:
                pass

            # search_radius guard
            try:
                rad = parameters[13].value
                if rad is not None and float(rad) < 0:
                    parameters[13].setErrorMessage(
                        "Search radius must be >= 0.")
            except Exception:
                pass
        except Exception:
            pass
        return

    # -------------------------------------------------------------------------
    # Execution
    # -------------------------------------------------------------------------
    def execute(self, parameters, messages):
        springs          = parameters[0].valueAsText
        contours         = parameters[1].valueAsText
        elev_field       = parameters[2].valueAsText
        ignore_selection = bool(parameters[3].value)

        out_gdb          = parameters[4].valueAsText
        out_base_name_in = parameters[5].valueAsText
        output_mode      = parameters[6].valueAsText
        create_summary   = bool(parameters[7].value)

        sym_layer      = parameters[8].valueAsText
        auto_symbology = parameters[9].value
        symbol_size    = parameters[10].value

        work_sr_mode  = parameters[11].valueAsText
        near_method   = parameters[12].valueAsText
        search_radius = parameters[13].value

        global_offset = parameters[14].value
        k_near        = parameters[15].value
        tangent_step  = parameters[16].value

        aoi_layer  = parameters[17].valueAsText
        aoi_buffer = parameters[18].value

        cache_mode = parameters[19].valueAsText
        profile    = bool(parameters[20].value)

        run_01 = bool(parameters[21].value)
        run_02 = bool(parameters[22].value)
        run_03 = bool(parameters[23].value)
        run_04 = bool(parameters[24].value)
        run_05 = bool(parameters[25].value)

        out_base_name = _validate_output_name(out_base_name_in, out_gdb)
        if out_base_name_in != out_base_name:
            _warn(f"Output base name validated: "
                  f"'{out_base_name_in}' -> '{out_base_name}'")

        try:
            arcpy.env.overwriteOutput = True
        except Exception:
            pass

        _msg("Spring Rotation tool (Pro v4 native) started.")
        _msg("Maintainer: Ali Mirjafari - 09186441801")

        try:
            outs = _run_suite(
                springs_layer=springs,
                contours_layer=contours,
                elev_field=elev_field,
                out_gdb=out_gdb,
                out_base_name=out_base_name,
                output_mode=output_mode,
                create_summary=create_summary,
                symbology_source_layer=sym_layer,
                auto_symbology=auto_symbology,
                symbol_size=symbol_size,
                work_sr_mode=work_sr_mode,
                near_method=near_method,
                search_radius=search_radius,
                global_offset=float(global_offset) if global_offset is not None else 0.0,
                k_near=int(k_near) if k_near else 8,
                tangent_step=float(tangent_step) if tangent_step else 5.0,
                aoi_layer=aoi_layer,
                aoi_buffer=float(aoi_buffer) if aoi_buffer is not None else 0.0,
                cache_mode=cache_mode,
                profile=profile,
                ignore_selection=ignore_selection,
                run_01=run_01, run_02=run_02, run_03=run_03,
                run_04=run_04, run_05=run_05,
            )
        except Exception:
            tb = traceback.format_exc()
            _err("Tool failed. Traceback:")
            _err(tb)
            raise

        parameters[26].value = ";".join([str(x) for x in outs])
        _msg(f"Done. Created {len(outs)} output(s).")
