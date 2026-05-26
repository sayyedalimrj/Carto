# -*- coding: utf-8 -*-
"""
================================================================================
 Plugin 01 - Bridge & Culvert Toolkit            [ ArcGIS Pro / Python 3 ]
                                                  REWRITE under MASTER RULES
================================================================================
 Native ArcGIS Pro build (arcpy.mp, f-strings, type hints).

 Tools:
   01) Create Bridge Points   (rotation aligned to ROAD)
   02) Create Culvert Points  (rotation aligned to DRAINAGE)
   03) Rotate Existing Bridge Points  (from Roads, geometry not moved)
   04) Rotate Existing Culvert Points (from Drainage, geometry not moved)

 MASTER RULES enforced:
   1. Narrow exception handling at GP-call sites (arcpy.ExecuteError,
      RuntimeError). MemoryError / OSError are NEVER caught. No bare excepts.
   2. No large geometry caches in RAM. Cursors stream inline.
   3. Selection hygiene: _resolve_full_source() preserves the
      ignore_selection contract.
   4. GP environment snapshot/restore in every execute().
   5. Pro-native: range(), native str, arcpy.mp, "memory" workspace.
   6. All cursors inside `with` blocks; scratch datasets / layer views
      are cleaned in `finally`.
   7. arcpy.SetProgressor on every long loop; honours cancellation.
   8. Deterministic iteration order via ORDER BY OBJECTID.

 Specific fixes vs prior revision:
   F1. Removed geom_cache dictionaries; geometries stream via cursors.
   F2. Endpoint multiplicity uses FindIdentical with XY tolerance,
       not integer grid rounding (round(x/t)*t).
   F3. Tangents from positionAlongLine(d).firstPoint only.
       segmentAlongLine() is explicitly forbidden in this module.
   F4. arcpy.SetProgressor on every main loop.

 Maintainer: Ali Mirjafari
================================================================================
"""

import arcpy
import os
import math
import time
import gc
import traceback
import contextlib
from typing import Optional, Tuple, List, Dict


# =============================================================================
# Environment snapshot / restore (Master Rule 4)
# =============================================================================

_ENV_KEYS = (
    "extent",
    "mask",
    "outputCoordinateSystem",
    "workspace",
    "scratchWorkspace",
    "parallelProcessingFactor",
    "overwriteOutput",
    "autoCancelling",
)


def _snapshot_env() -> dict:
    snap = {}
    for k in _ENV_KEYS:
        snap[k] = getattr(arcpy.env, k, None)
    return snap


def _restore_env(snap: dict) -> None:
    for k, v in snap.items():
        try:
            setattr(arcpy.env, k, v)
        except (arcpy.ExecuteError, RuntimeError) as ex:
            arcpy.AddWarning(f"Could not restore arcpy.env.{k}: {ex}")


def _prime_env() -> None:
    """Set the env state we want during execute(); always after _snapshot_env."""
    arcpy.env.extent = None
    arcpy.env.mask = None
    arcpy.env.outputCoordinateSystem = None
    arcpy.env.overwriteOutput = True
    arcpy.env.parallelProcessingFactor = "100%"
    arcpy.env.autoCancelling = True


# =============================================================================
# Logging shortcuts
# =============================================================================

def _msg(s: str) -> None:
    arcpy.AddMessage(s)


def _warn(s: str) -> None:
    arcpy.AddWarning(s)


def _err(s: str) -> None:
    arcpy.AddError(s)


# =============================================================================
# Cleanup helpers (Master Rule 6)
# =============================================================================

def _safe_delete(path: Optional[str]) -> None:
    """Delete a scratch dataset / layer view; narrow exceptions only."""
    if not path:
        return
    try:
        if arcpy.Exists(path):
            arcpy.management.Delete(path)
    except (arcpy.ExecuteError, RuntimeError) as ex:
        _warn(f"Could not delete '{path}': {ex}")


@contextlib.contextmanager
def _temp_paths(*paths: str):
    """Guarantee deletion of scratch paths even on MemoryError / OSError."""
    try:
        yield paths
    finally:
        for p in paths:
            _safe_delete(p)


# =============================================================================
# Small numeric helpers
# =============================================================================

def _safe_float(v, default_val: float) -> float:
    if v is None:
        return float(default_val)
    try:
        return float(v)
    except (TypeError, ValueError):
        return float(default_val)


def _safe_tol(val, default_val: float = 2.0) -> float:
    if val is None:
        return float(default_val)
    try:
        t = float(val)
    except (TypeError, ValueError):
        return float(default_val)
    return t if t > 0 else float(default_val)


def _get_count(ds) -> int:
    try:
        return int(arcpy.management.GetCount(ds)[0])
    except (arcpy.ExecuteError, RuntimeError):
        return -1


def _oid_field(fc) -> str:
    return arcpy.Describe(fc).OIDFieldName


# =============================================================================
# Scratch workspace
# =============================================================================

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


def _mem_name(prefix: str) -> str:
    """Pro-native in-memory dataset (Rule 5: 'memory', no backslashes)."""
    return arcpy.CreateUniqueName(prefix, "memory")


# =============================================================================
# Selection hygiene (Master Rule 3) - _resolve_full_source preserved
# =============================================================================

def _resolve_full_source(layer_token, ignore_selection: bool = True):
    """
    If ignore_selection is True (the documented default contract),
    resolve the layer to its catalogPath so the GP tool processes ALL
    features rather than only the on-map selection.

    If ignore_selection is False, return the token unchanged so the
    user's selection is honoured.
    """
    if not layer_token:
        return layer_token
    if not ignore_selection:
        return layer_token
    try:
        d = arcpy.Describe(layer_token)
    except (arcpy.ExecuteError, RuntimeError):
        return layer_token
    fidset = getattr(d, "FIDSet", None)
    if fidset:
        ids = [x for x in fidset.replace(",", ";").split(";") if x.strip()]
        if ids:
            _warn(
                f"Selection of {len(ids)} feature(s) detected on "
                f"'{layer_token}' -> IGNORED (processing full dataset)."
            )
    cp = getattr(d, "catalogPath", None)
    if cp and arcpy.Exists(cp):
        return cp
    return layer_token


# Backwards-compat alias (some helpers still reference _full_source)
_full_source = _resolve_full_source


def _multivalue_to_sources(val_as_text: Optional[str],
                           ignore_selection: bool = True) -> List[str]:
    if not val_as_text:
        return []
    return [_resolve_full_source(v, ignore_selection)
            for v in val_as_text.split(";") if v]


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


def ensure_field(fc, name: str, ftype: str = "DOUBLE",
                 length: Optional[int] = None) -> None:
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
    except (arcpy.ExecuteError, RuntimeError) as ex:
        _warn(f"RepairGeometry skipped for '{tag}': {ex}")
    single = _scratch_name(f"{tag}_single")
    _safe_delete(single)
    try:
        arcpy.management.MultipartToSinglepart(merged, single)
    except (arcpy.ExecuteError, RuntimeError) as ex:
        _warn(f"MultipartToSinglepart failed for '{tag}': {ex}")
        _safe_delete(single)
        return merged
    _safe_delete(merged)
    return single


# =============================================================================
# Geometry / projection helpers
# =============================================================================

def _is_geographic(sr) -> bool:
    if not sr:
        return False
    t = getattr(sr, "type", None)
    return bool(t) and str(t).lower() == "geographic"


def _planar_sr_from(sr):
    if sr and not _is_geographic(sr):
        return sr
    return arcpy.SpatialReference(3857)


def _project_geom_safe(geom, target_sr):
    """Project only if SRs differ. Narrow exceptions; geometric ops can raise."""
    if geom is None or target_sr is None:
        return geom
    gsr = getattr(geom, "spatialReference", None)
    if gsr is not None and getattr(gsr, "factoryCode", None) == \
            getattr(target_sr, "factoryCode", None):
        return geom
    try:
        return geom.projectAs(target_sr)
    except (arcpy.ExecuteError, RuntimeError) as ex:
        _warn(f"projectAs failed; using original geometry: {ex}")
        return geom


def _two_point_angle(dx: float, dy: float, mode: str,
                     angle_offset: float = 0.0) -> Optional[float]:
    """Convert a tangent vector (dx, dy) to an angle in the requested mode."""
    if abs(dx) < 1e-12 and abs(dy) < 1e-12:
        return None
    ang_e = (math.degrees(math.atan2(dy, dx)) + angle_offset) % 360.0
    if mode.upper() == "NORTH_CW":
        return (450.0 - ang_e) % 360.0
    return ang_e


def _collect_window_points(line_geom, d_center: float,
                           window_m: float, n: int = 7) -> List[Tuple[float, float]]:
    """
    Sample n points along line_geom centred on d_center spanning +/- window_m.

    NOTE (Fix F3): we use positionAlongLine(d).firstPoint exclusively.
    arcpy.Polyline.segmentAlongLine() is forbidden in this module: its
    returned polyline orientation is not a guaranteed local tangent.
    """
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
    pts: List[Tuple[float, float]] = []
    for i in range(n):
        pg = line_geom.positionAlongLine(a + i * step, False)
        if pg is not None:
            pts.append((pg.firstPoint.X, pg.firstPoint.Y))
    uniq: List[Tuple[float, float]] = []
    for pp in pts:
        if not uniq or abs(pp[0] - uniq[-1][0]) > 1e-9 or \
                abs(pp[1] - uniq[-1][1]) > 1e-9:
            uniq.append(pp)
    return uniq


def compute_angle_on_line(line_geom, pt_geom, sample_m: float,
                          angle_offset: float = 0.0,
                          mode: str = "NORTH_CW"):
    """Returns (angle_degrees_or_None, snapped_pt_in_orig_sr)."""
    if line_geom is None:
        return None, pt_geom
    sr_l = getattr(line_geom, "spatialReference", None)
    calc_sr = _planar_sr_from(sr_l) if _is_geographic(sr_l) else sr_l
    line_calc = _project_geom_safe(line_geom, calc_sr) if calc_sr else line_geom
    pt_calc = _project_geom_safe(pt_geom, calc_sr) if calc_sr else pt_geom

    try:
        _q, d_center, _r, _s = line_calc.queryPointAndDistance(
            pt_calc.firstPoint, False)
    except (arcpy.ExecuteError, RuntimeError):
        return None, pt_geom

    L = line_calc.length
    if not L or L <= 0:
        return None, pt_geom

    window_m = max(sample_m, L * 0.01)
    pts = _collect_window_points(line_calc, d_center, window_m)

    ang: Optional[float] = None
    if len(pts) >= 2:
        n = float(len(pts))
        idx_range = list(range(len(pts)))
        s_i = sum(idx_range)
        s_i2 = sum(i * i for i in idx_range)
        s_x = sum(p[0] for p in pts)
        s_y = sum(p[1] for p in pts)
        s_ix = sum(i * pts[i][0] for i in idx_range)
        s_iy = sum(i * pts[i][1] for i in idx_range)
        den = (n * s_i2 - s_i * s_i)
        if abs(den) >= 1e-12:
            mx = (n * s_ix - s_i * s_x) / den
            my = (n * s_iy - s_i * s_y) / den
            ang = _two_point_angle(mx, my, mode, angle_offset)
        if ang is None:
            best, best_len2 = None, -1.0
            for i in range(len(pts) - 1):
                vx = pts[i + 1][0] - pts[i][0]
                vy = pts[i + 1][1] - pts[i][1]
                l2 = vx * vx + vy * vy
                if l2 > best_len2:
                    best_len2, best = l2, (vx, vy)
            if best is not None:
                ang = _two_point_angle(best[0], best[1], mode, angle_offset)

    if ang is None:
        a = max(0.0, d_center - sample_m)
        b = min(L, d_center + sample_m)
        pa = line_calc.positionAlongLine(a, False)
        pb = line_calc.positionAlongLine(b, False)
        if pa is None or pb is None:
            return None, pt_geom
        ang = _two_point_angle(
            pb.firstPoint.X - pa.firstPoint.X,
            pb.firstPoint.Y - pa.firstPoint.Y,
            mode, angle_offset,
        )

    if ang is None:
        return None, pt_geom

    snapped = line_calc.positionAlongLine(d_center, False)
    if sr_l is not None and snapped is not None:
        try:
            if getattr(snapped.spatialReference, "factoryCode", None) != \
                    getattr(sr_l, "factoryCode", None):
                snapped = snapped.projectAs(sr_l)
        except (arcpy.ExecuteError, RuntimeError):
            pass
    return ang, snapped



# =============================================================================
# Endpoint multiplicity (Fix F2) - geometry-based dedupe via FindIdentical
# =============================================================================
#
# Why this exists:
#   An end-touch / T-junction crossing point coincides with an endpoint
#   of one of the lines AND no other line shares that endpoint. If the
#   endpoint is shared by 2+ lines, the crossing is a real junction and
#   must be kept.
#
# Old approach (deleted): bucket endpoints by int(round(x/t)) and
# int(round(y/t)). Fragile near grid boundaries: two points 0.01 m apart
# can land in different buckets if they straddle a grid line.
#
# New approach: write all endpoints to a scratch point FC (in the line's
# native SR), then run FindIdentical with the user's XY tolerance.
# FindIdentical assigns a FEAT_SEQ to every group of coincident features.
# Counting FEAT_SEQ frequencies gives true geometric multiplicity.

def _build_endpoint_multiplicity(line_fc: str, sr,
                                 tol: float) -> Dict[Tuple[float, float], int]:
    """
    Return a dict keyed by (round_x, round_y) -> count, where rounding is
    only used for fast lookup AFTER geometric clustering by FindIdentical.

    The dict's keys correspond to the FIRST endpoint observed in each
    FEAT_SEQ cluster, so lookups must use the same rounding granularity.
    A small lookup tolerance (tol) is applied at query time too.
    """
    t = _safe_tol(tol)
    calc_sr = _planar_sr_from(sr) if _is_geographic(sr) else sr

    ep_fc = _scratch_name("p01_endpoints")
    _safe_delete(ep_fc)

    sr_for_create = calc_sr if calc_sr else arcpy.SpatialReference(3857)
    arcpy.management.CreateFeatureclass(
        os.path.dirname(ep_fc), os.path.basename(ep_fc),
        "POINT", spatial_reference=sr_for_create,
    )

    try:
        # Stream every endpoint into the scratch FC.
        # Master Rule 8: deterministic order via OBJECTID.
        oid_fld = _oid_field(line_fc)
        sql_clause = (None, f"ORDER BY {oid_fld}")
        total = _get_count(line_fc)

        arcpy.SetProgressor("step", "Indexing line endpoints...", 0,
                            max(1, total), 1)
        n_seen = 0
        with arcpy.da.SearchCursor(line_fc, ["SHAPE@"],
                                   sql_clause=sql_clause) as src_cur, \
                arcpy.da.InsertCursor(ep_fc, ["SHAPE@"]) as ins_cur:
            for (geom,) in src_cur:
                n_seen += 1
                arcpy.SetProgressorPosition(n_seen)
                if geom is None:
                    continue
                g = _project_geom_safe(geom, calc_sr) if calc_sr else geom
                fp = getattr(g, "firstPoint", None)
                lp = getattr(g, "lastPoint", None)
                if fp is not None:
                    ins_cur.insertRow([arcpy.PointGeometry(fp, calc_sr)])
                if lp is not None:
                    ins_cur.insertRow([arcpy.PointGeometry(lp, calc_sr)])
        arcpy.ResetProgressor()

        # FindIdentical clusters coincident points within XY tolerance.
        ident_tbl = _scratch_name("p01_endpoints_id")
        _safe_delete(ident_tbl)
        try:
            arcpy.management.FindIdentical(
                ep_fc, ident_tbl, ["Shape"],
                xy_tolerance=f"{t} Unknown",
                output_record_option="ALL",
            )
        except (arcpy.ExecuteError, RuntimeError) as ex:
            _warn(f"FindIdentical failed; endpoint multiplicity unavailable: {ex}")
            _safe_delete(ident_tbl)
            return {}

        # Count occurrences per FEAT_SEQ.
        # FindIdentical only writes rows where IN_FID belongs to an identical
        # group; with output_record_option=ALL every input row is written.
        seq_counts: Dict[int, int] = {}
        with arcpy.da.SearchCursor(ident_tbl, ["FEAT_SEQ"]) as cur:
            for (seq,) in cur:
                if seq is None:
                    continue
                seq_counts[int(seq)] = seq_counts.get(int(seq), 0) + 1

        # Map IN_FID -> FEAT_SEQ, then read each endpoint's coords via OID@
        # and store its multiplicity. Lookup later uses rounded coords with
        # the same rounding granularity.
        fid_to_seq: Dict[int, int] = {}
        with arcpy.da.SearchCursor(ident_tbl, ["IN_FID", "FEAT_SEQ"]) as cur:
            for in_fid, seq in cur:
                if in_fid is None or seq is None:
                    continue
                fid_to_seq[int(in_fid)] = int(seq)

        idx: Dict[Tuple[float, float], int] = {}
        with arcpy.da.SearchCursor(ep_fc, ["OID@", "SHAPE@XY"]) as cur:
            for oid, xy in cur:
                if xy is None:
                    continue
                count = seq_counts.get(fid_to_seq.get(int(oid), -1), 1)
                k = (round(xy[0] / t) * t, round(xy[1] / t) * t)
                # Keep the maximum count seen for the bucket - safer near
                # bucket boundaries where two clusters can fall adjacent.
                idx[k] = max(idx.get(k, 0), count)

        _safe_delete(ident_tbl)
        return idx
    finally:
        _safe_delete(ep_fc)


def _endpoint_multiplicity_at(point, idx: Dict[Tuple[float, float], int],
                              tol: float) -> int:
    """Look up multiplicity for a point, checking the bucket and 4 neighbours."""
    if point is None or not idx:
        return 0
    t = _safe_tol(tol)
    bx = round(point.X / t) * t
    by = round(point.Y / t) * t
    best = 0
    for dx in (-t, 0.0, t):
        for dy in (-t, 0.0, t):
            v = idx.get((bx + dx, by + dy), 0)
            if v > best:
                best = v
    return best


def _is_endtouch(line_geom, pt_geom, tol: float,
                 endpoint_index: Dict[Tuple[float, float], int]) -> bool:
    if not line_geom or not pt_geom:
        return False
    t = _safe_tol(tol)
    sr_l = getattr(line_geom, "spatialReference", None)
    calc_sr = _planar_sr_from(sr_l) if _is_geographic(sr_l) else sr_l
    line_calc = _project_geom_safe(line_geom, calc_sr) if calc_sr else line_geom
    pt_calc = _project_geom_safe(pt_geom, calc_sr) if calc_sr else pt_geom

    try:
        _q, d_center, _r, _s = line_calc.queryPointAndDistance(
            pt_calc.firstPoint, False)
    except (arcpy.ExecuteError, RuntimeError):
        return False

    L = line_calc.length
    if not L or L <= 0:
        return True
    if d_center > t and (L - d_center) > t:
        return False

    end_pt = line_calc.firstPoint if d_center <= (L - d_center) else line_calc.lastPoint
    if _endpoint_multiplicity_at(end_pt, endpoint_index, t) >= 2:
        return False
    return True


# =============================================================================
# Crossing point construction
# =============================================================================

def _build_crossing_points(roads_fc: str, drains_fc: str, host_fc: str,
                           out_fc: str) -> Tuple[Optional[str], Optional[str]]:
    pts = _scratch_name("bc_ix")
    _safe_delete(pts)
    arcpy.analysis.Intersect([roads_fc, drains_fc], pts, "ONLY_FID", "", "POINT")
    try:
        arcpy.management.DeleteIdentical(pts, ["Shape"])
    except (arcpy.ExecuteError, RuntimeError) as ex:
        _warn(f"DeleteIdentical(crossings) skipped: {ex}")

    try:
        host_sr = arcpy.Describe(host_fc).spatialReference
    except (arcpy.ExecuteError, RuntimeError):
        host_sr = None

    src, proj = pts, None
    if host_sr and host_sr.name not in ("Unknown", "", None):
        proj = _scratch_name("bc_ix_pr")
        _safe_delete(proj)
        try:
            arcpy.management.Project(pts, proj, host_sr)
            src = proj
        except (arcpy.ExecuteError, RuntimeError) as ex:
            _warn(f"Project(crossings) failed; using unprojected points: {ex}")
            src = pts

    _safe_delete(out_fc)
    arcpy.management.CopyFeatures(src, out_fc)
    _safe_delete(pts)
    _safe_delete(proj)

    fid_fields = [f.name for f in arcpy.ListFields(out_fc)
                  if f.name.upper().startswith("FID_")]
    road_fid = fid_fields[0] if len(fid_fields) >= 1 else None
    drain_fid = fid_fields[1] if len(fid_fields) >= 2 else None
    return road_fid, drain_fid


# =============================================================================
# End-touch filter (Fix F1: streaming, no full geom_cache)
# =============================================================================

def _filter_endtouch(out_fc: str, roads_fc: str, drains_fc: str,
                     road_fid_field: Optional[str],
                     drain_fid_field: Optional[str],
                     end_tol: float) -> int:
    """
    Remove crossing points that are mere end-touches / T-junctions on
    EITHER reference layer.

    Memory profile: at any moment we hold:
       - a small Set of needed road OIDs and drain OIDs (ints)
       - one road geometry and one drain geometry (cursor-streamed)
       - the endpoint multiplicity index (a dict of float pairs -> int)
    No bulk geometry dictionaries.
    """
    if not road_fid_field or not drain_fid_field:
        _warn("End-touch filter skipped (could not detect FID fields).")
        return 0

    # Pass 1 - collect referenced OIDs by streaming the crossings FC.
    needed_roads: set = set()
    needed_drains: set = set()
    with arcpy.da.SearchCursor(out_fc, [road_fid_field, drain_fid_field]) as sc:
        for rf, df in sc:
            if rf is not None:
                needed_roads.add(int(rf))
            if df is not None:
                needed_drains.add(int(df))

    if not needed_roads or not needed_drains:
        return 0

    # Endpoint multiplicity indices (per reference layer).
    sr_r = arcpy.Describe(roads_fc).spatialReference
    sr_d = arcpy.Describe(drains_fc).spatialReference
    road_idx = _build_endpoint_multiplicity(roads_fc, sr_r, end_tol)
    drain_idx = _build_endpoint_multiplicity(drains_fc, sr_d, end_tol)

    # Pass 2 - per crossing, fetch ONLY the two referenced line geometries
    # via narrow OID selection cursors. One geometry pair in RAM at a time.
    oid_fld_road = _oid_field(roads_fc)
    oid_fld_drain = _oid_field(drains_fc)
    delim_road = arcpy.AddFieldDelimiters(roads_fc, oid_fld_road)
    delim_drain = arcpy.AddFieldDelimiters(drains_fc, oid_fld_drain)

    total = _get_count(out_fc)
    arcpy.SetProgressor("step", "Filtering end-touch / T-junctions...",
                        0, max(1, total), 1)
    n_seen = 0
    removed = 0

    out_oid_fld = _oid_field(out_fc)
    sql_clause = (None, f"ORDER BY {out_oid_fld}")

    with arcpy.da.UpdateCursor(out_fc,
                               ["SHAPE@", road_fid_field, drain_fid_field],
                               sql_clause=sql_clause) as uc:
        for shp, rf, df in uc:
            n_seen += 1
            arcpy.SetProgressorPosition(n_seen)
            if rf is None or df is None:
                continue

            rg = _fetch_one_geom(roads_fc, oid_fld_road, delim_road, int(rf))
            dg = _fetch_one_geom(drains_fc, oid_fld_drain, delim_drain, int(df))

            try:
                if (_is_endtouch(rg, shp, end_tol, road_idx)
                        or _is_endtouch(dg, shp, end_tol, drain_idx)):
                    uc.deleteRow()
                    removed += 1
            except (arcpy.ExecuteError, RuntimeError) as ex:
                _warn(f"End-touch test failed at one row; kept: {ex}")

    arcpy.ResetProgressor()
    del road_idx, drain_idx
    gc.collect()
    return removed


def _fetch_one_geom(fc: str, oid_field: str, delim: str, oid_val: int):
    """Fetch a single geometry by OID. Cursor closes immediately."""
    where = f"{delim} = {int(oid_val)}"
    with arcpy.da.SearchCursor(fc, ["SHAPE@"], where_clause=where) as cur:
        for (geom,) in cur:
            return geom
    return None


# =============================================================================
# Rotation engine (Fix F1: streaming, no full geom_cache)
# =============================================================================

def _apply_rotation(out_fc: str, ref_fc: str, sample_m: float,
                    rot_field: str, rot_type: str,
                    snap_to_line: bool) -> Tuple[int, int]:
    """
    Compute and write rotation for every point in out_fc, aligned to its
    nearest line in ref_fc. NEAR_FID is computed via arcpy.analysis.Near;
    each line geometry is fetched on demand from the reference FC, NOT
    pre-cached.
    """
    arcpy.analysis.Near(out_fc, ref_fc, "", "NO_LOCATION", "NO_ANGLE", "PLANAR")

    oid_fld_ref = _oid_field(ref_fc)
    delim_ref = arcpy.AddFieldDelimiters(ref_fc, oid_fld_ref)

    mode = "NORTH_CW" if rot_type == "GEOGRAPHIC" else "EAST_CCW"

    fields = (["NEAR_FID", "SHAPE@", "SHAPE@XY", "ROT_RAW", rot_field]
              if snap_to_line else ["NEAR_FID", "SHAPE@", "ROT_RAW", rot_field])

    total = _get_count(out_fc)
    arcpy.SetProgressor("step", "Computing rotations...", 0, max(1, total), 1)

    out_oid_fld = _oid_field(out_fc)
    sql_clause = (None, f"ORDER BY {out_oid_fld}")

    n_total = n_ok = 0
    n_seen = 0
    with arcpy.da.UpdateCursor(out_fc, fields, sql_clause=sql_clause) as uc:
        for row in uc:
            n_seen += 1
            arcpy.SetProgressorPosition(n_seen)
            n_total += 1

            nf = row[0]
            shp = row[1]
            if nf is None or int(nf) < 0:
                continue
            line = _fetch_one_geom(ref_fc, oid_fld_ref, delim_ref, int(nf))
            if line is None:
                continue
            try:
                ang, snapped = compute_angle_on_line(line, shp, sample_m,
                                                     0.0, mode)
            except (arcpy.ExecuteError, RuntimeError) as ex:
                _warn(f"compute_angle_on_line failed at OID {nf}: {ex}")
                continue
            if ang is None:
                continue

            ang_applied = (ang + 90.0) % 360.0 if rot_type == "GEOGRAPHIC" else ang
            if snap_to_line and snapped is not None:
                uc.updateRow((nf, shp,
                              (snapped.firstPoint.X, snapped.firstPoint.Y),
                              ang, ang_applied))
            else:
                if snap_to_line:
                    uc.updateRow((nf, shp, shp[0] if shp else None,
                                  ang, ang_applied))
                else:
                    uc.updateRow((nf, shp, ang, ang_applied))
            n_ok += 1

    arcpy.ResetProgressor()
    _msg(f"[DIAG] rotation written for {n_ok}/{n_total} points.")
    return n_ok, n_total


# =============================================================================
# Map / symbology (ArcGIS Pro: arcpy.mp)
# =============================================================================

def add_layer_to_pro(out_fc: str, rot_field: str, rot_type: str,
                     tmpl_lyr: Optional[str]) -> None:
    try:
        aprx = arcpy.mp.ArcGISProject("CURRENT")
    except (arcpy.ExecuteError, RuntimeError) as ex:
        _warn(f"No active ArcGIS Pro project; output not added: {ex}")
        return
    m = aprx.activeMap
    if m is None:
        _warn("No active map in the current project; output not added.")
        return
    try:
        lyr = m.addDataFromPath(out_fc)
    except (arcpy.ExecuteError, RuntimeError) as ex:
        _warn(f"Could not add layer to map: {ex}")
        return
    if tmpl_lyr:
        try:
            arcpy.management.ApplySymbologyFromLayer(lyr, tmpl_lyr)
        except (arcpy.ExecuteError, RuntimeError):
            _warn("ApplySymbologyFromLayer failed; default symbology kept.")
    _msg(f"Added to map. Set rotation field '{rot_field}' ({rot_type}) "
         f"in the Symbology pane if not using a template .lyrx.")


# =============================================================================
# Parameter builders
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
                               datatype="GPString",
                               parameterType="Required", direction="Input")
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

    end_tol = arcpy.Parameter(
        displayName="Endpoint tolerance (map units) - remove end-touch/T",
        name="end_tol", datatype="GPDouble",
        parameterType="Optional", direction="Input")
    end_tol.value = 2.0
    end_tol.category = "Quality control"

    return [roads, drains, out_ws, out_name, sample_m, rot_field,
            rot_type, add_map, tmpl_lyr, end_tol]


def _suggest_endtol(params) -> None:
    sample = params[4].value
    endp = params[9]
    if sample is None or endp.altered:
        return
    try:
        endp.value = max(1.0, min(10.0, float(sample) * 0.25))
    except (TypeError, ValueError):
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
                               datatype="GPString",
                               parameterType="Required", direction="Input")
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

    rot_field = arcpy.Parameter(displayName="Rotation field name",
                                name="rot_field", datatype="GPString",
                                parameterType="Required", direction="Input")
    rot_field.value = "ROTATION"
    rot_field.category = "Rotation"

    rot_type = arcpy.Parameter(displayName="Rotation type", name="rot_type",
                               datatype="GPString",
                               parameterType="Required", direction="Input")
    rot_type.filter.list = ["ARITHMETIC", "GEOGRAPHIC"]
    rot_type.value = "GEOGRAPHIC"
    rot_type.category = "Rotation"

    add_map = arcpy.Parameter(displayName="Add output to current map",
                              name="add_map", datatype="GPBoolean",
                              parameterType="Optional", direction="Input")
    add_map.value = True
    add_map.category = "Map display"

    tmpl_lyr = arcpy.Parameter(displayName="Template .lyrx (optional)",
                               name="tmpl_lyr", datatype="DEFile",
                               parameterType="Optional", direction="Input")
    tmpl_lyr.filter.list = ["lyrx", "lyr"]
    tmpl_lyr.category = "Map display"

    return [in_pts, ref, upd_mode, out_ws, out_name, sample_m, rot_field,
            rot_type, add_map, tmpl_lyr]



# =============================================================================
# Toolbox + Tools
# =============================================================================

class Toolbox(object):
    def __init__(self):
        self.label = "Plugin01 Bridge & Culvert Toolkit (Pro / Py3)"
        self.alias = "bridgeCulvertPro"
        self.tools = [BuildBridgePoints, BuildCulvertPoints,
                      RotateExistingBridgePoints, RotateExistingCulvertPoints]


# -----------------------------------------------------------------------------
# Create-points base (tools 01 and 02)
# -----------------------------------------------------------------------------

class _CreateBase(object):
    REF = "ROAD"  # overridden by subclasses

    def execute(self, p, m):
        # MASTER RULE 4: snapshot env at entry; reset; restore in finally.
        env_snap = _snapshot_env()
        t0 = time.time()
        roads_fc: Optional[str] = None
        drains_fc: Optional[str] = None
        try:
            _prime_env()

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
                raise arcpy.ExecuteError(
                    "Provide at least one Road and one Drainage layer.")

            out_gdb = ensure_file_gdb(out_ws)

            _msg("Merging reference lines (scratch)...")
            roads_fc = _merge_lines(roads, "bc_roads")
            drains_fc = _merge_lines(drains, "bc_drains")
            _msg(f"[DIAG] roads={_get_count(roads_fc)}  "
                 f"drains={_get_count(drains_fc)}")

            host_fc = roads_fc if self.REF == "ROAD" else drains_fc
            out_fc = os.path.join(out_gdb, out_name)

            _msg("Building true crossings...")
            road_fid, drain_fid = _build_crossing_points(
                roads_fc, drains_fc, host_fc, out_fc)
            ensure_field(out_fc, "ROT_RAW", "DOUBLE")
            ensure_field(out_fc, rot_field, "DOUBLE")
            _msg(f"[DIAG] raw crossing points: {_get_count(out_fc)}")

            removed = _filter_endtouch(out_fc, roads_fc, drains_fc,
                                       road_fid, drain_fid, end_tol)
            if removed:
                _msg(f"[DIAG] removed {removed} end-touch/T points.")
            _msg(f"[DIAG] true crossings kept: {_get_count(out_fc)}")

            ref_fc = roads_fc if self.REF == "ROAD" else drains_fc
            _apply_rotation(out_fc, ref_fc, sample_m,
                            rot_field, rot_type, snap_to_line=True)

            if add_map:
                add_layer_to_pro(out_fc, rot_field, rot_type, tmpl_lyr)

            _msg(f"Done in {time.time() - t0:.1f}s -> {out_fc}")

        except arcpy.ExecuteError:
            _err(arcpy.GetMessages(2))
            raise
        except RuntimeError as ex:
            _err(f"Runtime error: {ex}")
            _err(traceback.format_exc())
            raise
        finally:
            _safe_delete(roads_fc)
            _safe_delete(drains_fc)
            _restore_env(env_snap)

    def isLicensed(self):
        return True

    def updateParameters(self, params):
        _suggest_endtol(params)
        return

    def updateMessages(self, params):
        return


class BuildBridgePoints(_CreateBase):
    REF = "ROAD"

    def __init__(self):
        self.label = "01) Create Bridge Points (Road x Drain)"
        self.description = ("Create bridge points at TRUE road x drainage "
                            "crossings; rotation aligned to ROAD. "
                            "Processes ALL features.")
        self.category = "01 - Create Points"
        self.canRunInBackground = False

    def getParameterInfo(self):
        return _create_params("P_Bridge")


class BuildCulvertPoints(_CreateBase):
    REF = "DRAIN"

    def __init__(self):
        self.label = "02) Create Culvert Points (Road x Drain)"
        self.description = ("Create culvert points at TRUE road x drainage "
                            "crossings; rotation aligned to DRAINAGE. "
                            "Processes ALL features.")
        self.category = "01 - Create Points"
        self.canRunInBackground = False

    def getParameterInfo(self):
        return _create_params("Pnt_Culvert")


# -----------------------------------------------------------------------------
# Rotate-existing base (tools 03 and 04)
# -----------------------------------------------------------------------------

class _RotateBase(object):
    DEFAULT_NAME = "P_Rot"

    def execute(self, p, m):
        env_snap = _snapshot_env()
        t0 = time.time()
        ref_fc: Optional[str] = None
        try:
            _prime_env()

            in_pts = _resolve_full_source(p[0].valueAsText)
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
                raise arcpy.ExecuteError(
                    "Provide existing points and at least one reference "
                    "line layer.")

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

            _apply_rotation(out_fc, ref_fc, sample_m,
                            rot_field, rot_type, snap_to_line=False)

            if add_map:
                add_layer_to_pro(out_fc, rot_field, rot_type, tmpl_lyr)

            _msg(f"Done in {time.time() - t0:.1f}s -> {out_fc}")

        except arcpy.ExecuteError:
            _err(arcpy.GetMessages(2))
            raise
        except RuntimeError as ex:
            _err(f"Runtime error: {ex}")
            _err(traceback.format_exc())
            raise
        finally:
            _safe_delete(ref_fc)
            _restore_env(env_snap)

    def updateParameters(self, params):
        is_copy = (params[2].valueAsText or "").upper() != "UPDATE_IN_PLACE"
        params[3].enabled = is_copy
        params[4].enabled = is_copy
        return

    def updateMessages(self, params):
        return

    def isLicensed(self):
        return True


class RotateExistingBridgePoints(_RotateBase):
    DEFAULT_NAME = "P_Bridge_Rot"

    def __init__(self):
        self.label = "03) Rotate Existing Bridge Points (from Roads)"
        self.description = ("Update rotation of existing bridge points "
                            "from nearest ROAD; geometry NOT moved. "
                            "Processes ALL features.")
        self.category = "02 - Rotate Existing"
        self.canRunInBackground = False

    def getParameterInfo(self):
        return _rotate_params("P_Bridge_Rot", "Road centerlines")


class RotateExistingCulvertPoints(_RotateBase):
    DEFAULT_NAME = "Pnt_Culvert_Rot"

    def __init__(self):
        self.label = "04) Rotate Existing Culvert Points (from Drainage)"
        self.description = ("Update rotation of existing culvert points "
                            "from nearest DRAINAGE; geometry NOT moved. "
                            "Processes ALL features.")
        self.category = "02 - Rotate Existing"
        self.canRunInBackground = False

    def getParameterInfo(self):
        return _rotate_params("Pnt_Culvert_Rot", "Drainage / canals")
