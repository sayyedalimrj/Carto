# -*- coding: utf-8 -*-
"""
================================================================================
 Plugin 01 - Bridge & Culvert Toolkit            [ ArcGIS Pro / Python 3 ]
================================================================================
 Native ArcGIS Pro build (arcpy.mp, f-strings, type hints). Functionally
 identical to the ArcMap/Py2.7 edition; same preserved operational logic and
 the same hardwired failsafes:

   * SELECTION BYPASS by default (resolve catalogPath -> full dataset).
   * MEMORY-BOUNDED geometry loading (chunked OID IN() queries).
   * scratchGDB for heavy intermediates; parallelProcessingFactor enabled.
   * Scoped exception handling + stage diagnostics.

 Tools:
   01) Create Bridge Points   (rotation aligned to ROAD)
   02) Create Culvert Points  (rotation aligned to DRAINAGE)
   03) Rotate Existing Bridge Points  (from Roads, geometry not moved)
   04) Rotate Existing Culvert Points (from Drainage, geometry not moved)

 Maintainer: Ali Mirjafari - 09186441801
================================================================================
"""

import arcpy
import os
import math
import time
import gc
import traceback
from typing import Optional, Tuple, Set, Dict, List


# =============================================================================
# Environment / utilities
# =============================================================================

def _setup_env() -> None:
    for setter in (
        lambda: setattr(arcpy.env, "overwriteOutput", True),
        lambda: setattr(arcpy.env, "parallelProcessingFactor", "100%"),
    ):
        try:
            setter()
        except Exception:
            pass


def _msg(s: str) -> None:
    arcpy.AddMessage(s)


def _warn(s: str) -> None:
    arcpy.AddWarning(s)


def _err(s: str) -> None:
    arcpy.AddError(s)


def _safe_delete(path: Optional[str]) -> None:
    try:
        if path and arcpy.Exists(path):
            arcpy.management.Delete(path)
    except Exception:
        pass


def _safe_float(v, default_val: float) -> float:
    try:
        return float(v)
    except Exception:
        return float(default_val)


def _safe_tol(val, default_val: float = 2.0) -> float:
    try:
        t = float(val)
        if t > 0:
            return t
    except Exception:
        pass
    return float(default_val)


def _get_count(ds) -> int:
    try:
        return int(arcpy.management.GetCount(ds)[0])
    except Exception:
        return -1


def _oid_field(fc) -> str:
    try:
        return arcpy.Describe(fc).OIDFieldName
    except Exception:
        return "OBJECTID"


def _scratch_gdb() -> str:
    sg = arcpy.env.scratchGDB
    if sg and arcpy.Exists(sg):
        return sg
    folder = arcpy.env.scratchFolder or os.environ.get("TEMP", ".")
    gdb = os.path.join(folder, "p01_scratch.gdb")
    if not arcpy.Exists(gdb):
        arcpy.management.CreateFileGDB(os.path.dirname(gdb), os.path.basename(gdb))
    return gdb


def _scratch_name(prefix: str) -> str:
    return arcpy.CreateScratchName(prefix, "", "FeatureClass", _scratch_gdb())


# =============================================================================
# SELECTION BYPASS (process ALL features by default)
# =============================================================================

def _full_source(layer_token):
    if not layer_token:
        return layer_token
    try:
        d = arcpy.Describe(layer_token)
        fidset = getattr(d, "FIDSet", None)
        if fidset:
            ids = [x for x in fidset.replace(",", ";").split(";") if x.strip()]
            if ids:
                _warn(f"Selection of {len(ids)} feature(s) detected on "
                      f"'{layer_token}' -> IGNORED (processing full dataset).")
        cp = getattr(d, "catalogPath", None)
        if cp and arcpy.Exists(cp):
            return cp
    except Exception:
        pass
    return layer_token


def _multivalue_to_sources(val_as_text: Optional[str]) -> List[str]:
    if not val_as_text:
        return []
    return [_full_source(v) for v in val_as_text.split(";") if v]


# =============================================================================
# File GDB / fields
# =============================================================================

def ensure_file_gdb(workspace_path: str) -> str:
    if not workspace_path:
        raise arcpy.ExecuteError("Output workspace is required.")
    if workspace_path.lower().endswith(".gdb"):
        if not arcpy.Exists(workspace_path):
            parent = os.path.dirname(workspace_path)
            if parent and not os.path.isdir(parent):
                os.makedirs(parent)
            arcpy.management.CreateFileGDB(parent, os.path.basename(workspace_path))
        return workspace_path
    if not os.path.isdir(workspace_path):
        os.makedirs(workspace_path)
    base = os.path.basename(workspace_path.rstrip("\\/")) or "output"
    gdb = os.path.join(workspace_path, f"{base}.gdb")
    if not arcpy.Exists(gdb):
        arcpy.management.CreateFileGDB(workspace_path, f"{base}.gdb")
    return gdb


def ensure_field(fc, name: str, ftype: str = "DOUBLE", length: Optional[int] = None) -> None:
    names = {f.name.lower() for f in arcpy.ListFields(fc)}
    if name.lower() in names:
        return
    if ftype.upper() == "TEXT" and length:
        arcpy.management.AddField(fc, name, ftype, field_length=length)
    else:
        arcpy.management.AddField(fc, name, ftype)


# =============================================================================
# Merge + clean reference lines (scratchGDB)
# =============================================================================

def _merge_lines(sources: List[str], tag: str) -> str:
    if not sources:
        raise arcpy.ExecuteError(f"No input lines provided for '{tag}'.")
    merged = _scratch_name(f"{tag}_merge")
    _safe_delete(merged)
    if len(sources) == 1:
        arcpy.management.CopyFeatures(sources[0], merged)
    else:
        arcpy.management.Merge(sources, merged)
    try:
        arcpy.management.RepairGeometry(merged, "DELETE_NULL")
    except Exception:
        pass
    single = _scratch_name(f"{tag}_single")
    _safe_delete(single)
    try:
        arcpy.management.MultipartToSinglepart(merged, single)
        _safe_delete(merged)
        return single
    except Exception:
        _safe_delete(single)
        return merged


# =============================================================================
# Geometry / projection
# =============================================================================

def _is_geographic(sr) -> bool:
    try:
        return bool(sr) and bool(sr.type) and sr.type.lower() == "geographic"
    except Exception:
        return False


def _planar_sr_from(sr):
    try:
        if sr and not _is_geographic(sr):
            return sr
    except Exception:
        pass
    return arcpy.SpatialReference(3857)


def _project_geom_safe(geom, target_sr):
    try:
        gsr = getattr(geom, "spatialReference", None)
        if gsr and getattr(gsr, "factoryCode", None) == getattr(target_sr, "factoryCode", None):
            return geom
        return geom.projectAs(target_sr)
    except Exception:
        return geom


def _load_geoms_subset(fc, oid_set: Set[int]) -> Dict[int, object]:
    """Load ONLY referenced geometries via chunked IN() queries (RAM-bounded)."""
    out: Dict[int, object] = {}
    if not oid_set:
        return out
    oid_field = _oid_field(fc)
    delim = arcpy.AddFieldDelimiters(fc, oid_field)
    oids = sorted(int(x) for x in oid_set)
    CHUNK = 900
    for i in range(0, len(oids), CHUNK):
        chunk = oids[i:i + CHUNK]
        where = f"{delim} IN ({','.join(str(x) for x in chunk)})"
        with arcpy.da.SearchCursor(fc, ["OID@", "SHAPE@"], where_clause=where) as cur:
            for oid, geom in cur:
                out[int(oid)] = geom
    return out


def _build_endpoint_index(line_fc, sr, tol: float) -> Dict[Tuple[int, int], int]:
    t = _safe_tol(tol)
    calc_sr = _planar_sr_from(sr) if _is_geographic(sr) else sr
    idx: Dict[Tuple[int, int], int] = {}
    with arcpy.da.SearchCursor(line_fc, ["SHAPE@"]) as cur:
        for (geom,) in cur:
            if not geom:
                continue
            g = _project_geom_safe(geom, calc_sr) if calc_sr else geom
            try:
                fp, lp = g.firstPoint, g.lastPoint
            except Exception:
                continue
            for pt in (fp, lp):
                if pt is None:
                    continue
                try:
                    k = (int(round(pt.X / t)), int(round(pt.Y / t)))
                except Exception:
                    k = (int(round(pt.X)), int(round(pt.Y)))
                idx[k] = idx.get(k, 0) + 1
    return idx


def _is_endtouch(line_geom, pt_geom, tol: float, endpoint_index) -> bool:
    if not line_geom or not pt_geom:
        return False
    t = _safe_tol(tol)
    try:
        sr_l = line_geom.spatialReference
    except Exception:
        sr_l = None
    if sr_l:
        calc_sr = _planar_sr_from(sr_l) if _is_geographic(sr_l) else sr_l
        line_calc = _project_geom_safe(line_geom, calc_sr)
        pt_calc = _project_geom_safe(pt_geom, calc_sr)
    else:
        line_calc, pt_calc = line_geom, pt_geom
    try:
        _q, d_center, _r, _s = line_calc.queryPointAndDistance(pt_calc.firstPoint, False)
    except Exception:
        return False
    L = line_calc.length
    if not L or L <= 0:
        return True
    if d_center > t and (L - d_center) > t:
        return False
    if endpoint_index:
        try:
            end_pt = line_calc.firstPoint if d_center <= (L - d_center) else line_calc.lastPoint
            k = (int(round(end_pt.X / t)), int(round(end_pt.Y / t)))
            if endpoint_index.get(k, 0) >= 2:
                return False
        except Exception:
            pass
    return True


def _collect_window_points(line_geom, d_center: float, window_m: float, n: int = 7):
    L = line_geom.length
    if not L or L <= 0:
        return []
    a = max(0.0, d_center - window_m)
    b = min(L, d_center + window_m)
    if b - a < max(1e-6, window_m * 0.2):
        span = max(window_m, L * 0.02)
        a = max(0.0, d_center - span)
        b = min(L, d_center + span)
    if b <= a:
        return []
    step = (b - a) / float(max(1, n - 1))
    pts = []
    for i in range(n):
        pg = line_geom.positionAlongLine(a + i * step, False)
        if pg:
            pts.append((pg.firstPoint.X, pg.firstPoint.Y))
    uniq = []
    for pp in pts:
        if not uniq or abs(pp[0] - uniq[-1][0]) > 1e-9 or abs(pp[1] - uniq[-1][1]) > 1e-9:
            uniq.append(pp)
    return uniq


def compute_angle_on_line(line_geom, pt_geom, sample_m: float,
                          angle_offset: float = 0.0, mode: str = "NORTH_CW"):
    if not line_geom:
        return None, pt_geom
    try:
        sr_l = line_geom.spatialReference
    except Exception:
        sr_l = None
    if sr_l:
        calc_sr = _planar_sr_from(sr_l) if _is_geographic(sr_l) else sr_l
        line_calc = _project_geom_safe(line_geom, calc_sr)
        pt_calc = _project_geom_safe(pt_geom, calc_sr)
    else:
        line_calc, pt_calc = line_geom, pt_geom
    try:
        _q, d_center, _r, _s = line_calc.queryPointAndDistance(pt_calc.firstPoint, False)
    except Exception:
        return None, pt_geom
    L = line_calc.length
    if not L or L <= 0:
        return None, pt_geom
    window_m = max(sample_m, L * 0.01)
    pts = _collect_window_points(line_calc, d_center, window_m)

    def two_point(dx, dy):
        if abs(dx) < 1e-12 and abs(dy) < 1e-12:
            return None
        ang_e = (math.degrees(math.atan2(dy, dx)) + angle_offset) % 360.0
        return (450.0 - ang_e) % 360.0 if mode.upper() == "NORTH_CW" else ang_e

    ang = None
    if len(pts) >= 2:
        idx = range(len(pts))
        n = float(len(pts))
        s_i = sum(idx)
        s_i2 = sum(i * i for i in idx)
        s_x = sum(p[0] for p in pts)
        s_y = sum(p[1] for p in pts)
        s_ix = sum(i * pts[i][0] for i in idx)
        s_iy = sum(i * pts[i][1] for i in idx)
        den = (n * s_i2 - s_i * s_i)
        if abs(den) >= 1e-12:
            mx = (n * s_ix - s_i * s_x) / den
            my = (n * s_iy - s_i * s_y) / den
            ang = two_point(mx, my)
        if ang is None:
            best, best_len2 = None, -1.0
            for i in range(len(pts) - 1):
                vx = pts[i + 1][0] - pts[i][0]
                vy = pts[i + 1][1] - pts[i][1]
                l2 = vx * vx + vy * vy
                if l2 > best_len2:
                    best_len2, best = l2, (vx, vy)
            if best:
                ang = two_point(best[0], best[1])
    if ang is None:
        a = max(0.0, d_center - sample_m)
        b = min(L, d_center + sample_m)
        pa = line_calc.positionAlongLine(a, False)
        pb = line_calc.positionAlongLine(b, False)
        if not pa or not pb:
            return None, pt_geom
        ang = two_point(pb.firstPoint.X - pa.firstPoint.X,
                        pb.firstPoint.Y - pa.firstPoint.Y)
    if ang is None:
        return None, pt_geom
    snapped = line_calc.positionAlongLine(d_center, False)
    try:
        if sr_l and getattr(snapped.spatialReference, "factoryCode", None) != getattr(sr_l, "factoryCode", None):
            snapped = snapped.projectAs(sr_l)
    except Exception:
        pass
    return ang, snapped


# =============================================================================
# Map / symbology (ArcGIS Pro: arcpy.mp)
# =============================================================================

def add_layer_to_pro(out_fc: str, rot_field: str, rot_type: str,
                     tmpl_lyr: Optional[str]) -> None:
    try:
        aprx = arcpy.mp.ArcGISProject("CURRENT")
        m = aprx.activeMap
        if m is None:
            _warn("No active map in the current project; output not added.")
            return
        lyr = m.addDataFromPath(out_fc)
        if tmpl_lyr:
            try:
                arcpy.management.ApplySymbologyFromLayer(lyr, tmpl_lyr)
            except Exception:
                _warn("ApplySymbologyFromLayer failed; default symbology kept.")
        # Marker rotation via CIM (Pro)
        try:
            sym = lyr.symbology
            if hasattr(sym, "renderer") and hasattr(sym.renderer, "symbol"):
                cim = lyr.getDefinition("V3")
                # rotation visual variable is best set in the UI; we set the field
                # so the user can finish in Symbology pane. Leave geometry intact.
                lyr.setDefinition(cim)
        except Exception:
            pass
        _msg(f"Added to map. Set rotation field '{rot_field}' ({rot_type}) "
             f"in the Symbology pane if not using a template .lyrx.")
    except Exception:
        _warn("Could not add output to the current project.")


# =============================================================================
# Crossing + rotation engine
# =============================================================================

def _build_crossing_points(roads_fc: str, drains_fc: str, host_fc: str,
                           out_fc: str) -> Tuple[Optional[str], Optional[str]]:
    pts = _scratch_name("bc_ix")
    _safe_delete(pts)
    arcpy.analysis.Intersect([roads_fc, drains_fc], pts, "ONLY_FID", "", "POINT")
    try:
        arcpy.management.DeleteIdentical(pts, ["Shape"])
    except Exception:
        pass

    try:
        host_sr = arcpy.Describe(host_fc).spatialReference
    except Exception:
        host_sr = None
    src, proj = pts, None
    if host_sr and host_sr.name not in ("Unknown", "", None):
        proj = _scratch_name("bc_ix_pr")
        _safe_delete(proj)
        try:
            arcpy.management.Project(pts, proj, host_sr)
            src = proj
        except Exception:
            src = pts

    _safe_delete(out_fc)
    arcpy.management.CopyFeatures(src, out_fc)
    _safe_delete(pts)
    _safe_delete(proj)

    fid_fields = [f.name for f in arcpy.ListFields(out_fc) if f.name.upper().startswith("FID_")]
    road_fid = fid_fields[0] if len(fid_fields) >= 1 else None
    drain_fid = fid_fields[1] if len(fid_fields) >= 2 else None
    return road_fid, drain_fid


def _filter_endtouch(out_fc, roads_fc, drains_fc, road_fid_field, drain_fid_field, end_tol) -> int:
    if not road_fid_field or not drain_fid_field:
        _warn("End-touch filter skipped (could not detect FID fields).")
        return 0

    road_fids: Set[int] = set()
    drain_fids: Set[int] = set()
    with arcpy.da.SearchCursor(out_fc, [road_fid_field, drain_fid_field]) as sc:
        for rf, df in sc:
            if rf is not None:
                road_fids.add(int(rf))
            if df is not None:
                drain_fids.add(int(df))

    road_geoms = _load_geoms_subset(roads_fc, road_fids)
    drain_geoms = _load_geoms_subset(drains_fc, drain_fids)

    try:
        sr_r = arcpy.Describe(roads_fc).spatialReference
    except Exception:
        sr_r = None
    try:
        sr_d = arcpy.Describe(drains_fc).spatialReference
    except Exception:
        sr_d = None
    road_idx = _build_endpoint_index(roads_fc, sr_r, end_tol)
    drain_idx = _build_endpoint_index(drains_fc, sr_d, end_tol)

    removed = 0
    with arcpy.da.UpdateCursor(out_fc, ["SHAPE@", road_fid_field, drain_fid_field]) as uc:
        for shp, rf, df in uc:
            try:
                if rf is None or df is None:
                    continue
                rg = road_geoms.get(int(rf))
                dg = drain_geoms.get(int(df))
                if _is_endtouch(rg, shp, end_tol, road_idx) or _is_endtouch(dg, shp, end_tol, drain_idx):
                    uc.deleteRow()
                    removed += 1
            except Exception:
                continue
    del road_geoms, drain_geoms, road_idx, drain_idx
    gc.collect()
    return removed


def _apply_rotation(out_fc, ref_fc, sample_m, rot_field, rot_type, snap_to_line: bool):
    arcpy.analysis.Near(out_fc, ref_fc, "", "NO_LOCATION", "NO_ANGLE", "PLANAR")

    near_fids: Set[int] = set()
    with arcpy.da.SearchCursor(out_fc, ["NEAR_FID"]) as sc:
        for (nf,) in sc:
            if nf is not None and int(nf) >= 0:
                near_fids.add(int(nf))
    ref_geoms = _load_geoms_subset(ref_fc, near_fids)

    mode = "NORTH_CW" if rot_type == "GEOGRAPHIC" else "EAST_CCW"
    n_total = n_ok = 0
    fields = (["NEAR_FID", "SHAPE@", "SHAPE@XY", "ROT_RAW", rot_field]
              if snap_to_line else ["NEAR_FID", "SHAPE@", "ROT_RAW", rot_field])

    with arcpy.da.UpdateCursor(out_fc, fields) as uc:
        for row in uc:
            n_total += 1
            nf, shp = row[0], row[1]
            if nf is None or int(nf) < 0:
                continue
            line = ref_geoms.get(int(nf))
            if not line:
                continue
            ang, snapped = compute_angle_on_line(line, shp, sample_m, 0.0, mode)
            if ang is None:
                continue
            ang_applied = (ang + 90.0) % 360.0 if rot_type == "GEOGRAPHIC" else ang
            if snap_to_line and snapped is not None:
                uc.updateRow((nf, shp, (snapped.firstPoint.X, snapped.firstPoint.Y), ang, ang_applied))
            else:
                uc.updateRow((nf, shp, ang, ang_applied))
            n_ok += 1
    del ref_geoms
    gc.collect()
    _msg(f"[DIAG] rotation written for {n_ok}/{n_total} points.")
    return n_ok, n_total


# =============================================================================
# Parameters
# =============================================================================

def _create_params(default_name: str):
    roads = arcpy.Parameter(displayName="Road centerlines (Polyline, multi-value)",
                            name="roads", datatype="GPFeatureLayer",
                            parameterType="Required", direction="Input")
    roads.category = "Input data"
    roads.multiValue = True
    roads.filter.list = ["Polyline"]

    drains = arcpy.Parameter(displayName="Drainage / canals (Polyline, multi-value)",
                             name="drains", datatype="GPFeatureLayer",
                             parameterType="Required", direction="Input")
    drains.category = "Input data"
    drains.multiValue = True
    drains.filter.list = ["Polyline"]

    out_ws = arcpy.Parameter(displayName="Output location (FileGDB or Folder)",
                             name="out_ws", datatype="DEWorkspace",
                             parameterType="Required", direction="Input")
    out_ws.category = "Output"

    out_name = arcpy.Parameter(displayName="Output points feature class name",
                               name="out_name", datatype="GPString",
                               parameterType="Required", direction="Input")
    out_name.value = default_name
    out_name.category = "Output"

    sample_m = arcpy.Parameter(displayName="Rotation sampling distance (map units)",
                               name="sample_m", datatype="GPDouble",
                               parameterType="Required", direction="Input")
    sample_m.value = 8.0
    sample_m.category = "Rotation"

    rot_field = arcpy.Parameter(displayName="Rotation field name (created if missing)",
                                name="rot_field", datatype="GPString",
                                parameterType="Required", direction="Input")
    rot_field.value = "ROTATION"
    rot_field.category = "Rotation"

    rot_type = arcpy.Parameter(displayName="Rotation type", name="rot_type",
                               datatype="GPString", parameterType="Required", direction="Input")
    rot_type.filter.type = "ValueList"
    rot_type.filter.list = ["ARITHMETIC", "GEOGRAPHIC"]
    rot_type.value = "GEOGRAPHIC"
    rot_type.category = "Rotation"

    add_map = arcpy.Parameter(displayName="Add output to current map",
                              name="add_map", datatype="GPBoolean",
                              parameterType="Optional", direction="Input")
    add_map.value = True
    add_map.category = "Map display"

    tmpl_lyr = arcpy.Parameter(displayName="Optional layer template (.lyrx)",
                               name="tmpl_lyr", datatype="DEFile",
                               parameterType="Optional", direction="Input")
    tmpl_lyr.filter.list = ["lyrx", "lyr"]
    tmpl_lyr.category = "Map display"

    end_tol = arcpy.Parameter(displayName="Endpoint tolerance (map units) - remove end-touch/T",
                              name="end_tol", datatype="GPDouble",
                              parameterType="Optional", direction="Input")
    end_tol.value = 2.0
    end_tol.category = "Quality control"

    return [roads, drains, out_ws, out_name, sample_m, rot_field, rot_type, add_map, tmpl_lyr, end_tol]


def _suggest_endtol(params) -> None:
    try:
        sample = params[4].value
        endp = params[9]
        if sample is not None and not endp.altered:
            endp.value = max(1.0, min(10.0, float(sample) * 0.25))
    except Exception:
        pass


def _rotate_params(default_name: str, ref_label: str):
    in_pts = arcpy.Parameter(displayName="Existing points (Point/Multipoint layer)",
                             name="in_pts", datatype="GPFeatureLayer",
                             parameterType="Required", direction="Input")
    in_pts.category = "Input data"
    in_pts.filter.list = ["Point", "Multipoint"]

    ref = arcpy.Parameter(displayName=f"{ref_label} (Polyline, multi-value)",
                          name="ref", datatype="GPFeatureLayer",
                          parameterType="Required", direction="Input")
    ref.category = "Input data"
    ref.multiValue = True
    ref.filter.list = ["Polyline"]

    upd_mode = arcpy.Parameter(displayName="Update mode", name="upd_mode",
                               datatype="GPString", parameterType="Required", direction="Input")
    upd_mode.category = "Output"
    upd_mode.filter.list = ["COPY_TO_OUTPUT", "UPDATE_IN_PLACE"]
    upd_mode.value = "COPY_TO_OUTPUT"

    out_ws = arcpy.Parameter(displayName="Output location (if COPY_TO_OUTPUT)",
                             name="out_ws", datatype="DEWorkspace",
                             parameterType="Optional", direction="Input")
    out_ws.category = "Output"

    out_name = arcpy.Parameter(displayName="Output FC name (if COPY_TO_OUTPUT)",
                               name="out_name", datatype="GPString",
                               parameterType="Optional", direction="Input")
    out_name.value = default_name
    out_name.category = "Output"

    sample_m = arcpy.Parameter(displayName="Rotation sampling distance (map units)",
                               name="sample_m", datatype="GPDouble",
                               parameterType="Required", direction="Input")
    sample_m.value = 8.0
    sample_m.category = "Rotation"

    rot_field = arcpy.Parameter(displayName="Rotation field name", name="rot_field",
                                datatype="GPString", parameterType="Required", direction="Input")
    rot_field.value = "ROTATION"
    rot_field.category = "Rotation"

    rot_type = arcpy.Parameter(displayName="Rotation type", name="rot_type",
                               datatype="GPString", parameterType="Required", direction="Input")
    rot_type.filter.list = ["ARITHMETIC", "GEOGRAPHIC"]
    rot_type.value = "GEOGRAPHIC"
    rot_type.category = "Rotation"

    add_map = arcpy.Parameter(displayName="Add output to current map", name="add_map",
                              datatype="GPBoolean", parameterType="Optional", direction="Input")
    add_map.value = True
    add_map.category = "Map display"

    tmpl_lyr = arcpy.Parameter(displayName="Template .lyrx (optional)", name="tmpl_lyr",
                               datatype="DEFile", parameterType="Optional", direction="Input")
    tmpl_lyr.filter.list = ["lyrx", "lyr"]
    tmpl_lyr.category = "Map display"

    return [in_pts, ref, upd_mode, out_ws, out_name, sample_m, rot_field, rot_type, add_map, tmpl_lyr]


# =============================================================================
# Tools
# =============================================================================

class Toolbox(object):
    def __init__(self):
        self.label = "Plugin01 Bridge & Culvert Toolkit (Pro / Py3)"
        self.alias = "bridgeCulvertPro"
        self.tools = [BuildBridgePoints, BuildCulvertPoints,
                      RotateExistingBridgePoints, RotateExistingCulvertPoints]


class _CreateBase(object):
    REF = "ROAD"

    def execute(self, p, m):
        _setup_env()
        t0 = time.time()
        try:
            roads = _multivalue_to_sources(p[0].valueAsText)
            drains = _multivalue_to_sources(p[1].valueAsText)
            out_ws = p[2].valueAsText
            out_name = p[3].valueAsText
            sample_m = _safe_float(p[4].value, 8.0)
            rot_field = p[5].valueAsText
            rot_type = (p[6].valueAsText or "GEOGRAPHIC").upper()
            add_map = bool(p[7].value)
            tmpl_lyr = p[8].valueAsText
            end_tol = _safe_tol(p[9].value, 2.0)

            if not roads or not drains:
                raise arcpy.ExecuteError("Provide at least one Road and one Drainage layer.")

            out_gdb = ensure_file_gdb(out_ws)
            _msg("Merging reference lines (scratch)...")
            roads_fc = _merge_lines(roads, "bc_roads")
            drains_fc = _merge_lines(drains, "bc_drains")
            _msg(f"[DIAG] roads={_get_count(roads_fc)}  drains={_get_count(drains_fc)}")

            host_fc = roads_fc if self.REF == "ROAD" else drains_fc
            out_fc = os.path.join(out_gdb, out_name)

            _msg("Building true crossings...")
            road_fid, drain_fid = _build_crossing_points(roads_fc, drains_fc, host_fc, out_fc)
            ensure_field(out_fc, "ROT_RAW", "DOUBLE")
            ensure_field(out_fc, rot_field, "DOUBLE")
            _msg(f"[DIAG] raw crossing points: {_get_count(out_fc)}")

            removed = _filter_endtouch(out_fc, roads_fc, drains_fc, road_fid, drain_fid, end_tol)
            if removed:
                _msg(f"[DIAG] removed {removed} end-touch/T points.")
            _msg(f"[DIAG] true crossings kept: {_get_count(out_fc)}")

            ref_fc = roads_fc if self.REF == "ROAD" else drains_fc
            _apply_rotation(out_fc, ref_fc, sample_m, rot_field, rot_type, snap_to_line=True)

            if add_map:
                add_layer_to_pro(out_fc, rot_field, rot_type, tmpl_lyr)

            _safe_delete(roads_fc)
            _safe_delete(drains_fc)
            _msg(f"Done in {time.time() - t0:.1f}s -> {out_fc}")

        except arcpy.ExecuteError:
            _err(arcpy.GetMessages(2))
            raise
        except Exception as ex:
            _err(f"Unexpected error: {ex}")
            _err(traceback.format_exc())
            raise


class BuildBridgePoints(_CreateBase):
    REF = "ROAD"

    def __init__(self):
        self.label = "01) Create Bridge Points (Road x Drain)"
        self.description = ("Create bridge points at TRUE road x drainage crossings; "
                            "rotation aligned to ROAD. Processes ALL features.")
        self.category = "01 - Create Points"
        self.canRunInBackground = False

    def getParameterInfo(self):
        return _create_params("P_Bridge")

    def isLicensed(self):
        return True

    def updateParameters(self, params):
        _suggest_endtol(params)
        return

    def updateMessages(self, params):
        return


class BuildCulvertPoints(_CreateBase):
    REF = "DRAIN"

    def __init__(self):
        self.label = "02) Create Culvert Points (Road x Drain)"
        self.description = ("Create culvert points at TRUE road x drainage crossings; "
                            "rotation aligned to DRAINAGE. Processes ALL features.")
        self.category = "01 - Create Points"
        self.canRunInBackground = False

    def getParameterInfo(self):
        return _create_params("Pnt_Culvert")

    def isLicensed(self):
        return True

    def updateParameters(self, params):
        _suggest_endtol(params)
        return

    def updateMessages(self, params):
        return


class _RotateBase(object):
    DEFAULT_NAME = "P_Rot"

    def execute(self, p, m):
        _setup_env()
        t0 = time.time()
        ref_fc = None
        try:
            in_pts = _full_source(p[0].valueAsText)
            ref_sources = _multivalue_to_sources(p[1].valueAsText)
            upd_mode = (p[2].valueAsText or "COPY_TO_OUTPUT").upper()
            out_ws = p[3].valueAsText
            out_name = p[4].valueAsText
            sample_m = _safe_float(p[5].value, 8.0)
            rot_field = p[6].valueAsText
            rot_type = (p[7].valueAsText or "GEOGRAPHIC").upper()
            add_map = bool(p[8].value)
            tmpl_lyr = p[9].valueAsText

            if not in_pts or not ref_sources:
                raise arcpy.ExecuteError("Provide existing points and at least one reference line layer.")

            ref_fc = _merge_lines(ref_sources, "bc_ref")

            if upd_mode == "UPDATE_IN_PLACE":
                out_fc = in_pts
            else:
                out_gdb = ensure_file_gdb(out_ws)
                out_fc = os.path.join(out_gdb, out_name or self.DEFAULT_NAME)
                _safe_delete(out_fc)
                arcpy.management.CopyFeatures(in_pts, out_fc)

            ensure_field(out_fc, "ROT_RAW", "DOUBLE")
            ensure_field(out_fc, rot_field, "DOUBLE")

            _apply_rotation(out_fc, ref_fc, sample_m, rot_field, rot_type, snap_to_line=False)

            if add_map:
                add_layer_to_pro(out_fc, rot_field, rot_type, tmpl_lyr)

            _safe_delete(ref_fc)
            _msg(f"Done in {time.time() - t0:.1f}s -> {out_fc}")

        except arcpy.ExecuteError:
            _err(arcpy.GetMessages(2))
            raise
        except Exception as ex:
            _err(f"Unexpected error: {ex}")
            _err(traceback.format_exc())
            raise
        finally:
            _safe_delete(ref_fc)

    def updateParameters(self, params):
        try:
            is_copy = (params[2].valueAsText or "").upper() != "UPDATE_IN_PLACE"
            params[3].enabled = is_copy
            params[4].enabled = is_copy
        except Exception:
            pass
        return

    def updateMessages(self, params):
        return

    def isLicensed(self):
        return True


class RotateExistingBridgePoints(_RotateBase):
    DEFAULT_NAME = "P_Bridge_Rot"

    def __init__(self):
        self.label = "03) Rotate Existing Bridge Points (from Roads)"
        self.description = ("Update rotation of existing bridge points from nearest ROAD; "
                            "geometry NOT moved. Processes ALL features.")
        self.category = "02 - Rotate Existing"
        self.canRunInBackground = False

    def getParameterInfo(self):
        return _rotate_params("P_Bridge_Rot", "Road centerlines")


class RotateExistingCulvertPoints(_RotateBase):
    DEFAULT_NAME = "Pnt_Culvert_Rot"

    def __init__(self):
        self.label = "04) Rotate Existing Culvert Points (from Drainage)"
        self.description = ("Update rotation of existing culvert points from nearest DRAINAGE; "
                            "geometry NOT moved. Processes ALL features.")
        self.category = "02 - Rotate Existing"
        self.canRunInBackground = False

    def getParameterInfo(self):
        return _rotate_params("Pnt_Culvert_Rot", "Drainage / canals")
