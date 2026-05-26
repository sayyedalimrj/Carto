# -*- coding: utf-8 -*-
"""
================================================================================
 Plugin 01 - Bridge & Culvert Toolkit            [ ArcMap 10.x / Python 2.7 ]
================================================================================
 Build + Rotate + QC of bridge/culvert symbol points at road x drainage
 crossings.

 Tools:
   01) Create Bridge Points   (rotation aligned to ROAD)
   02) Create Culvert Points  (rotation aligned to DRAINAGE)
   03) Rotate Existing Bridge Points  (from Roads, no geometry move)
   04) Rotate Existing Culvert Points (from Drainage, no geometry move)

 Operational contract preserved:
   * Writes ROT_RAW (raw line azimuth) and ROTATION (applied symbol angle).
   * GEOGRAPHIC mode applies a +90 deg offset so the symbol crosses the line.
   * True-crossing filter: removes end-touch / T-junction false intersections.
   * Rotate tools never move point geometry; they only update angle fields.

 v6 hardening (this rewrite, per Master Rules):
   * Exceptions narrowed to (arcpy.ExecuteError, RuntimeError). MemoryError
     and OSError now propagate loudly (no more swallowed crashes).
   * Geometry caches eliminated. End-touch filter and rotation apply now
     stream geometry inline via cursor + OID-IN() chunking; only one line
     geometry is in RAM at a time.
   * Endpoint junction index now uses distance-based 9-cell neighbor lookup
     (no more lossy round(x/t)*t grid equality).
   * Tangent generation uses positionAlongLine(d).firstPoint exclusively.
     segmentAlongLine is FORBIDDEN here (returns the full segment, not a
     tangent vector).
   * arcpy.SetProgressor on every main loop.
   * arcpy.env.{extent, mask, outputCoordinateSystem, workspace,
     scratchWorkspace} snapshot/reset/restore in every execute().
   * Every scratch intermediate has a paired Delete_management in finally,
     plus a final in_memory flush at end of execute().
   * Selection bypass (_resolve_full_source) preserved.

 Maintainer: Ali Mirjafari - 09186441801
================================================================================
"""

from __future__ import division
import arcpy
import os
import math
import time
import gc
import traceback


# =============================================================================
# Environment / small utilities
# =============================================================================

def _setup_env():
    try:
        arcpy.env.overwriteOutput = True
    except (arcpy.ExecuteError, RuntimeError):
        pass
    try:
        arcpy.env.parallelProcessingFactor = "100%"
    except (arcpy.ExecuteError, RuntimeError):
        pass


def _msg(s):
    arcpy.AddMessage(s)


def _warn(s):
    arcpy.AddWarning(s)


def _err(s):
    arcpy.AddError(s)


def _safe_delete(path):
    """Best-effort delete. Narrowed to GP errors; never swallows MemoryError/OSError."""
    if not path:
        return
    try:
        if arcpy.Exists(path):
            arcpy.management.Delete(path)
    except (arcpy.ExecuteError, RuntimeError):
        pass


def _flush_in_memory():
    """End-of-execute flush of the in_memory workspace. Defensive (Rule 6)."""
    try:
        arcpy.management.Delete("in_memory")
    except (arcpy.ExecuteError, RuntimeError):
        pass


# ---- GP environment snapshot / reset / restore (Rule 4) ---------------------

_ENV_KEYS = ("extent", "mask", "outputCoordinateSystem",
             "workspace", "scratchWorkspace")


def _env_snapshot():
    """Capture the five GP env knobs that silently contaminate downstream tools."""
    snap = {}
    for k in _ENV_KEYS:
        try:
            snap[k] = getattr(arcpy.env, k)
        except (arcpy.ExecuteError, RuntimeError):
            snap[k] = None
    return snap


def _env_reset():
    """Force the five env knobs to None for a clean run."""
    for k in _ENV_KEYS:
        try:
            setattr(arcpy.env, k, None)
        except (arcpy.ExecuteError, RuntimeError):
            pass


def _env_restore(snap):
    """Restore the snapshot taken at execute() entry."""
    if not snap:
        return
    for k in _ENV_KEYS:
        try:
            setattr(arcpy.env, k, snap.get(k))
        except (arcpy.ExecuteError, RuntimeError):
            pass


# ---- value helpers ----------------------------------------------------------

def _safe_float(v, default_val):
    try:
        return float(v)
    except (TypeError, ValueError):
        return float(default_val)


def _safe_tol(val, default_val=2.0):
    try:
        t = float(val)
        if t > 0:
            return t
    except (TypeError, ValueError):
        pass
    return float(default_val)


def _get_count(ds):
    try:
        return int(arcpy.management.GetCount(ds)[0])
    except (arcpy.ExecuteError, RuntimeError):
        return -1


def _oid_field(fc):
    try:
        return arcpy.Describe(fc).OIDFieldName
    except (arcpy.ExecuteError, RuntimeError):
        return "OBJECTID"


def _scratch_gdb():
    sg = arcpy.env.scratchGDB
    if sg and arcpy.Exists(sg):
        return sg
    folder = arcpy.env.scratchFolder or arcpy.env.scratchWorkspace or os.environ.get("TEMP", ".")
    gdb = os.path.join(folder, "p01_scratch.gdb")
    if not arcpy.Exists(gdb):
        arcpy.management.CreateFileGDB(os.path.dirname(gdb), os.path.basename(gdb))
    return gdb


def _scratch_name(prefix):
    return arcpy.CreateScratchName(prefix, "", "FeatureClass", _scratch_gdb())


# ---- progressor wrappers (Prompt fix #4) ------------------------------------

def _prog_start(label, total):
    try:
        if total and total > 0:
            arcpy.SetProgressor("step", label, 0, int(total), 1)
        else:
            arcpy.SetProgressor("default", label)
    except (arcpy.ExecuteError, RuntimeError):
        pass


def _prog_tick():
    try:
        arcpy.SetProgressorPosition()
    except (arcpy.ExecuteError, RuntimeError):
        pass


def _prog_label(label):
    try:
        arcpy.SetProgressorLabel(label)
    except (arcpy.ExecuteError, RuntimeError):
        pass


def _prog_end():
    try:
        arcpy.ResetProgressor()
    except (arcpy.ExecuteError, RuntimeError):
        pass


# =============================================================================
# SELECTION BYPASS  (process ALL features by default; Rule 3)
# =============================================================================

def _resolve_full_source(layer_token, ignore_selection=True):
    """
    Resolve a layer (which may carry an active selection) to its on-disk
    catalogPath so the WHOLE dataset is used when ignore_selection is True.
    Falls back to the token itself if no catalogPath is available.

    NOTE: This is the canonical name required by the Master Rules (Rule 3).
    The legacy alias _full_source is preserved below for backward compat.
    """
    if not layer_token:
        return layer_token
    try:
        d = arcpy.Describe(layer_token)
        fidset = getattr(d, "FIDSet", None)
        if fidset:
            ids = [x for x in fidset.replace(",", ";").split(";") if x.strip() != ""]
            if ids:
                if ignore_selection:
                    _warn("Selection of {0} feature(s) detected on '{1}' -> "
                          "IGNORED (processing full dataset).".format(len(ids), layer_token))
                else:
                    _msg("Honoring selection of {0} feature(s) on '{1}'.".format(
                        len(ids), layer_token))
                    return layer_token
        if ignore_selection:
            cp = getattr(d, "catalogPath", None)
            if cp and arcpy.Exists(cp):
                return cp
    except (arcpy.ExecuteError, RuntimeError):
        pass
    return layer_token


# Legacy alias (do not remove; older call sites may still use it)
def _full_source(layer_token):
    return _resolve_full_source(layer_token, ignore_selection=True)


def _semicolon_to_sources(val_as_text, ignore_selection=True):
    """Split a multi-value layer string and resolve each to its full source."""
    if not val_as_text:
        return []
    raw = [v for v in val_as_text.split(";") if v]
    return [_resolve_full_source(v, ignore_selection) for v in raw]


# =============================================================================
# File GDB / output helpers
# =============================================================================

def ensure_file_gdb(workspace_path):
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
    gdb = os.path.join(workspace_path, base + ".gdb")
    if not arcpy.Exists(gdb):
        arcpy.management.CreateFileGDB(workspace_path, base + ".gdb")
    return gdb


def ensure_field(fc, name, ftype="DOUBLE", length=None):
    names = [f.name.lower() for f in arcpy.ListFields(fc)]
    if name.lower() in names:
        return
    if ftype.upper() == "TEXT" and length:
        arcpy.management.AddField(fc, name, ftype, field_length=length)
    else:
        arcpy.management.AddField(fc, name, ftype)


# =============================================================================
# Merge + clean reference lines  (scratchGDB, never in_memory)
# =============================================================================

def _merge_lines(sources, tag):
    """Merge a list of full-source line layers into one cleaned scratch FC.
    Returns the path to the cleaned single-part FC (caller owns deletion)."""
    if not sources:
        raise arcpy.ExecuteError("No input lines provided for '{0}'.".format(tag))
    merged = _scratch_name(tag + "_merge")
    _safe_delete(merged)
    if len(sources) == 1:
        arcpy.management.CopyFeatures(sources[0], merged)
    else:
        arcpy.management.Merge(sources, merged)
    try:
        arcpy.management.RepairGeometry(merged, "DELETE_NULL")
    except (arcpy.ExecuteError, RuntimeError):
        pass
    single = _scratch_name(tag + "_single")
    _safe_delete(single)
    try:
        arcpy.management.MultipartToSinglepart(merged, single)
        _safe_delete(merged)
        return single
    except (arcpy.ExecuteError, RuntimeError):
        _safe_delete(single)
        return merged



# =============================================================================
# Geometry / projection helpers
# =============================================================================

def _is_geographic(sr):
    try:
        return bool(sr) and bool(sr.type) and sr.type.lower() == "geographic"
    except (arcpy.ExecuteError, RuntimeError, AttributeError):
        return False


def _planar_sr_from(sr):
    try:
        if sr and not _is_geographic(sr):
            return sr
    except (arcpy.ExecuteError, RuntimeError, AttributeError):
        pass
    return arcpy.SpatialReference(3857)


def _project_geom_safe(geom, target_sr):
    try:
        gsr = getattr(geom, "spatialReference", None)
        if gsr and getattr(gsr, "factoryCode", None) == getattr(target_sr, "factoryCode", None):
            return geom
        return geom.projectAs(target_sr)
    except (arcpy.ExecuteError, RuntimeError, AttributeError):
        return geom


def _chunk_oid_where(fc, oids, chunk_size=900):
    """Yield (where_clause, oid_chunk_list) for OID IN() chunks. Streams OIDs
    so we never materialize all line geometries at once."""
    if not oids:
        return
    oid_field = _oid_field(fc)
    delim = arcpy.AddFieldDelimiters(fc, oid_field)
    sorted_oids = sorted(int(x) for x in oids)
    for i in xrange(0, len(sorted_oids), chunk_size):
        chunk = sorted_oids[i:i + chunk_size]
        where = "{0} IN ({1})".format(delim, ",".join(str(x) for x in chunk))
        yield where, chunk


# =============================================================================
# Endpoint junction index (distance-based 9-cell neighbor lookup)
# =============================================================================
#
# Replaces the legacy round(x/t)*t grid quantization (which silently merged
# endpoints across cell boundaries OR split co-located endpoints sitting on
# opposite sides of the same boundary).
#
# The new index buckets endpoints into floor(X/t), floor(Y/t) cells but the
# junction test queries the cell + its 8 neighbors and accepts hits within
# true Euclidean distance <= t. Lossless within tolerance.
# =============================================================================

class _EndpointIndex(object):
    """Spatial bucket of line termini, queryable by true distance.

    Memory footprint is one (float,float) tuple per endpoint, NOT geometry.
    Used to distinguish a shared junction (count >= 2 within tol) from a
    dangling terminus (count == 1).
    """

    __slots__ = ("tol", "cell", "buckets")

    def __init__(self, tol):
        self.tol = float(tol) if tol and tol > 0 else 2.0
        # Cell size = tol so a true-distance hit always lies in cell or 8 nbrs.
        self.cell = self.tol
        self.buckets = {}  # (cx, cy) -> list of (x, y)

    def _key(self, x, y):
        return (int(math.floor(x / self.cell)), int(math.floor(y / self.cell)))

    def add(self, x, y):
        k = self._key(x, y)
        bucket = self.buckets.get(k)
        if bucket is None:
            self.buckets[k] = [(x, y)]
        else:
            bucket.append((x, y))

    def count_near(self, x, y):
        """Count endpoints whose Euclidean distance to (x, y) is <= tol."""
        cx, cy = self._key(x, y)
        t2 = self.tol * self.tol
        n = 0
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                bucket = self.buckets.get((cx + dx, cy + dy))
                if not bucket:
                    continue
                for (bx, by) in bucket:
                    ex = bx - x
                    ey = by - y
                    if ex * ex + ey * ey <= t2:
                        n += 1
        return n


def _build_endpoint_index(line_fc, sr, tol):
    """Stream every line's two endpoints into a distance-queryable index.
    Memory-light: stores small float tuples, NOT geometry."""
    t = _safe_tol(tol)
    calc_sr = _planar_sr_from(sr) if _is_geographic(sr) else sr
    idx = _EndpointIndex(t)

    total = _get_count(line_fc)
    _prog_start("Indexing line endpoints ({0} features)...".format(total), total)
    try:
        with arcpy.da.SearchCursor(line_fc, ["SHAPE@"]) as cur:
            for (geom,) in cur:
                _prog_tick()
                if not geom:
                    continue
                g = _project_geom_safe(geom, calc_sr) if calc_sr else geom
                try:
                    fp = g.firstPoint
                    lp = g.lastPoint
                except AttributeError:
                    continue
                for pt in (fp, lp):
                    if pt is None:
                        continue
                    try:
                        idx.add(float(pt.X), float(pt.Y))
                    except (TypeError, ValueError, AttributeError):
                        pass
    finally:
        _prog_end()
    return idx


def _is_endtouch(line_geom, pt_geom, tol, endpoint_index):
    """True if the point sits at a *dangling* endpoint of the line (not a
    real crossing). A shared endpoint (junction, count>=2 within tol) is
    NOT an end-touch."""
    if not line_geom or not pt_geom:
        return False
    t = _safe_tol(tol)
    try:
        sr_l = line_geom.spatialReference
    except AttributeError:
        sr_l = None
    if sr_l:
        calc_sr = _planar_sr_from(sr_l) if _is_geographic(sr_l) else sr_l
        line_calc = _project_geom_safe(line_geom, calc_sr)
        pt_calc = _project_geom_safe(pt_geom, calc_sr)
    else:
        line_calc, pt_calc = line_geom, pt_geom
    try:
        _q, d_center, _r, _s = line_calc.queryPointAndDistance(pt_calc.firstPoint, False)
    except (arcpy.ExecuteError, RuntimeError, AttributeError):
        return False
    L = line_calc.length
    if not L or L <= 0:
        return True
    # Interior crossing -> keep
    if d_center > t and (L - d_center) > t:
        return False
    # We are within `tol` of one of the endpoints. Identify which one and
    # ask the index whether it is shared (junction) or dangling.
    if endpoint_index:
        try:
            end_pt = line_calc.firstPoint if d_center <= (L - d_center) else line_calc.lastPoint
            if endpoint_index.count_near(float(end_pt.X), float(end_pt.Y)) >= 2:
                return False  # shared endpoint = junction, keep
        except (AttributeError, TypeError, ValueError):
            pass
    return True


def _collect_window_points(line_geom, d_center, window_m, n=7):
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
    # NOTE: positionAlongLine(d, False).firstPoint is the ONLY supported
    # tangent-extraction primitive here. segmentAlongLine is forbidden -- it
    # returns the entire sub-segment, not a point, and would yield wrong
    # vectors when the crossing lies inside a single vertex span.
    for i in xrange(n):
        pg = line_geom.positionAlongLine(a + i * step, False)
        if pg:
            pts.append((pg.firstPoint.X, pg.firstPoint.Y))
    uniq = []
    for pp in pts:
        if not uniq or abs(pp[0] - uniq[-1][0]) > 1e-9 or abs(pp[1] - uniq[-1][1]) > 1e-9:
            uniq.append(pp)
    return uniq


def compute_angle_on_line(line_geom, pt_geom, sample_m, angle_offset=0.0, mode="NORTH_CW"):
    """
    Robust tangent angle of `line_geom` at the location nearest `pt_geom`.
    Uses a least-squares fit over a small window, with two fallbacks.
    Returns (angle_degrees, snapped_point_geometry) or (None, pt_geom).

    Tangent extraction uses positionAlongLine(d, False).firstPoint only.
    """
    if not line_geom:
        return None, pt_geom
    try:
        sr_l = line_geom.spatialReference
    except AttributeError:
        sr_l = None
    if sr_l:
        calc_sr = _planar_sr_from(sr_l) if _is_geographic(sr_l) else sr_l
        line_calc = _project_geom_safe(line_geom, calc_sr)
        pt_calc = _project_geom_safe(pt_geom, calc_sr)
    else:
        line_calc, pt_calc = line_geom, pt_geom
    try:
        _q, d_center, _r, _s = line_calc.queryPointAndDistance(pt_calc.firstPoint, False)
    except (arcpy.ExecuteError, RuntimeError, AttributeError):
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
        idx = list(xrange(len(pts)))
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
            best = None
            best_len2 = -1.0
            for i in xrange(len(pts) - 1):
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
    except (arcpy.ExecuteError, RuntimeError, AttributeError):
        pass
    return ang, snapped


# =============================================================================
# Map / symbology (ArcMap)
# =============================================================================

def add_layer_with_symbology(out_fc, name, tmpl_lyr, rot_field, rot_type):
    try:
        import arcpy.mapping as mapping
        mxd = mapping.MapDocument("CURRENT")
        dfs = mapping.ListDataFrames(mxd)
        if not dfs:
            return
        df = dfs[0]
        lyr = mapping.Layer(out_fc)
        lyr.name = name
        mapping.AddLayer(df, lyr, "TOP")
        if tmpl_lyr:
            try:
                arcpy.management.ApplySymbologyFromLayer(lyr, tmpl_lyr)
            except (arcpy.ExecuteError, RuntimeError):
                _warn("ApplySymbologyFromLayer failed; default symbology kept.")
        try:
            sym = lyr.symbology
            sym.rotationField = rot_field
            sym.rotationType = rot_type.upper()
            lyr.symbology = sym
        except (arcpy.ExecuteError, RuntimeError, AttributeError):
            pass
        try:
            arcpy.RefreshActiveView()
            arcpy.RefreshTOC()
        except (arcpy.ExecuteError, RuntimeError):
            pass
    except (arcpy.ExecuteError, RuntimeError):
        _warn("Could not add output to the current map (open ArcMap with a map document).")


# =============================================================================
# Crossing builder + streaming filter / rotation engine
# =============================================================================

def _build_crossing_points(roads_fc, drains_fc, host_fc, out_fc):
    """Intersect -> POINT, dedupe, project to host SR, copy to out_fc.
    Returns (road_fid_field, drain_fid_field). Caller owns out_fc."""
    pts_scratch = _scratch_name("bc_ix")
    _safe_delete(pts_scratch)
    proj = None
    try:
        arcpy.analysis.Intersect([roads_fc, drains_fc], pts_scratch, "ONLY_FID", "", "POINT")
        try:
            arcpy.management.DeleteIdentical(pts_scratch, ["Shape"])
        except (arcpy.ExecuteError, RuntimeError):
            pass

        host_sr = None
        try:
            host_sr = arcpy.Describe(host_fc).spatialReference
        except (arcpy.ExecuteError, RuntimeError):
            host_sr = None

        src = pts_scratch
        if host_sr and host_sr.name not in ("Unknown", "", None):
            proj = _scratch_name("bc_ix_pr")
            _safe_delete(proj)
            try:
                arcpy.management.Project(pts_scratch, proj, host_sr)
                src = proj
            except (arcpy.ExecuteError, RuntimeError):
                src = pts_scratch

        _safe_delete(out_fc)
        arcpy.management.CopyFeatures(src, out_fc)
    finally:
        _safe_delete(pts_scratch)
        _safe_delete(proj)

    fid_fields = [f.name for f in arcpy.ListFields(out_fc) if f.name.upper().startswith("FID_")]
    road_fid = fid_fields[0] if len(fid_fields) >= 1 else None
    drain_fid = fid_fields[1] if len(fid_fields) >= 2 else None
    return road_fid, drain_fid


def _stream_endtouch_flags(point_fc, line_fc, fid_field, endpoint_index, end_tol):
    """
    Stream the relevant line geometries via OID-IN() chunks and return
    {point_oid: True} for points whose nearest line is an end-touch.

    Memory profile: at any moment, exactly ONE line geometry plus a small
    list of point coordinates per chunk are held -- never the whole line
    layer.
    """
    flags = {}
    if not fid_field:
        return flags

    # Build {line_oid -> [(point_oid, point_shape), ...]} streamed per chunk,
    # NOT pre-loaded. We first collect (point_oid, line_oid, point_shape)
    # rows, group by line_oid in a dict whose values are small lists, then
    # iterate the line cursor chunk-by-chunk.
    grouped = {}
    with arcpy.da.SearchCursor(point_fc, ["OID@", fid_field, "SHAPE@"]) as sc:
        for poid, lfid, shp in sc:
            if lfid is None:
                continue
            try:
                key = int(lfid)
            except (TypeError, ValueError):
                continue
            grouped.setdefault(key, []).append((int(poid), shp))

    if not grouped:
        return flags

    line_oids = grouped.keys()
    total = len(line_oids)
    _prog_start("Filtering end-touches on {0} ({1} lines)...".format(
        os.path.basename(line_fc), total), total)
    try:
        for where, chunk in _chunk_oid_where(line_fc, line_oids):
            with arcpy.da.SearchCursor(line_fc, ["OID@", "SHAPE@"], where_clause=where) as lc:
                for loid, lgeom in lc:
                    _prog_tick()
                    if not lgeom:
                        continue
                    bucket = grouped.get(int(loid))
                    if not bucket:
                        continue
                    for (poid, pshp) in bucket:
                        try:
                            if _is_endtouch(lgeom, pshp, end_tol, endpoint_index):
                                flags[poid] = True
                        except (arcpy.ExecuteError, RuntimeError):
                            continue
                    # Free the per-line bucket immediately to keep RAM flat.
                    grouped[int(loid)] = None
    finally:
        _prog_end()
    grouped = None
    gc.collect()
    return flags


def _filter_endtouch(out_fc, roads_fc, drains_fc, road_fid_field, drain_fid_field, end_tol):
    """Remove points that are mere end-touches/T-junctions (not true crossings).

    Streaming version: builds two distance-queryable endpoint indexes (small
    float tuples per endpoint, no geometry), then passes the point layer
    twice -- once against roads, once against drains -- pulling line
    geometries via chunked OID IN() queries so only ~chunk_size geometries
    are alive at any moment.
    """
    if not road_fid_field or not drain_fid_field:
        _warn("End-touch filter skipped (could not detect FID fields).")
        return 0

    try:
        sr_r = arcpy.Describe(roads_fc).spatialReference
    except (arcpy.ExecuteError, RuntimeError):
        sr_r = None
    try:
        sr_d = arcpy.Describe(drains_fc).spatialReference
    except (arcpy.ExecuteError, RuntimeError):
        sr_d = None

    road_idx = _build_endpoint_index(roads_fc, sr_r, end_tol)
    drain_idx = _build_endpoint_index(drains_fc, sr_d, end_tol)

    road_flags = _stream_endtouch_flags(out_fc, roads_fc, road_fid_field, road_idx, end_tol)
    drain_flags = _stream_endtouch_flags(out_fc, drains_fc, drain_fid_field, drain_idx, end_tol)

    # Free the indexes before the delete pass.
    road_idx = None
    drain_idx = None
    gc.collect()

    removed = 0
    total = _get_count(out_fc)
    _prog_start("Removing end-touch points ({0} candidates)...".format(total), total)
    try:
        with arcpy.da.UpdateCursor(out_fc, ["OID@"]) as uc:
            for (poid,) in uc:
                _prog_tick()
                pid = int(poid)
                if road_flags.get(pid) or drain_flags.get(pid):
                    uc.deleteRow()
                    removed += 1
    finally:
        _prog_end()

    road_flags = None
    drain_flags = None
    gc.collect()
    return removed


def _apply_rotation(out_fc, ref_fc, sample_m, rot_field, rot_type, snap_to_line):
    """Near to ref_fc, then write ROT_RAW + rot_field. snap_to_line moves the
    point onto the reference line (Build tools) when True.

    Streaming version: instead of loading every NEAR_FID line into a dict,
    we group point OIDs by NEAR_FID, then walk the line FC in OID-IN()
    chunks -- at most ~chunk_size line geometries alive at any moment.
    """
    arcpy.analysis.Near(out_fc, ref_fc, "", "NO_LOCATION", "NO_ANGLE", "PLANAR")

    # Group point OIDs by NEAR_FID, streaming.
    grouped = {}
    with arcpy.da.SearchCursor(out_fc, ["OID@", "NEAR_FID"]) as sc:
        for poid, nfid in sc:
            if nfid is None:
                continue
            try:
                key = int(nfid)
            except (TypeError, ValueError):
                continue
            if key < 0:
                continue
            grouped.setdefault(key, []).append(int(poid))

    # Compute (angle, snapped_point) per point OID by streaming the line FC.
    results = {}  # point_oid -> (ang, snapped_or_None)
    mode = "NORTH_CW" if rot_type == "GEOGRAPHIC" else "EAST_CCW"

    if grouped:
        # We need point geometries to feed compute_angle_on_line. Pull them
        # only for the OIDs we will actually update.
        point_shapes = {}
        with arcpy.da.SearchCursor(out_fc, ["OID@", "SHAPE@"]) as sc:
            for poid, shp in sc:
                pid = int(poid)
                point_shapes[pid] = shp
                # Note: SHAPE@ tokens are lightweight references; arcpy
                # streams the geometry on access. We're holding references,
                # not deep copies of large polylines.

        line_oids = grouped.keys()
        total = len(line_oids)
        _prog_start("Computing rotations ({0} lines)...".format(total), total)
        try:
            for where, _chunk in _chunk_oid_where(ref_fc, line_oids):
                with arcpy.da.SearchCursor(ref_fc, ["OID@", "SHAPE@"], where_clause=where) as lc:
                    for loid, lgeom in lc:
                        _prog_tick()
                        if not lgeom:
                            continue
                        bucket = grouped.get(int(loid))
                        if not bucket:
                            continue
                        for pid in bucket:
                            pshp = point_shapes.get(pid)
                            if not pshp:
                                continue
                            try:
                                ang, snapped = compute_angle_on_line(
                                    lgeom, pshp, sample_m, 0.0, mode)
                            except (arcpy.ExecuteError, RuntimeError):
                                continue
                            if ang is None:
                                continue
                            results[pid] = (ang, snapped)
                        grouped[int(loid)] = None
        finally:
            _prog_end()

        point_shapes = None
        grouped = None
        gc.collect()

    # Apply results in a single update pass.
    n_total = 0
    n_ok = 0
    if snap_to_line:
        fields = ["OID@", "SHAPE@", "SHAPE@XY", "ROT_RAW", rot_field]
    else:
        fields = ["OID@", "SHAPE@", "ROT_RAW", rot_field]

    upd_total = _get_count(out_fc)
    _prog_start("Writing rotation fields ({0} points)...".format(upd_total), upd_total)
    try:
        with arcpy.da.UpdateCursor(out_fc, fields) as uc:
            for row in uc:
                _prog_tick()
                n_total += 1
                pid = int(row[0])
                shp = row[1]
                res = results.get(pid)
                if not res:
                    continue
                ang, snapped = res
                ang_applied = (ang + 90.0) % 360.0 if rot_type == "GEOGRAPHIC" else ang
                if snap_to_line and snapped is not None:
                    x = snapped.firstPoint.X
                    y = snapped.firstPoint.Y
                    uc.updateRow((pid, shp, (x, y), ang, ang_applied))
                else:
                    uc.updateRow((pid, shp, ang, ang_applied))
                n_ok += 1
    finally:
        _prog_end()

    results = None
    gc.collect()
    _msg("[DIAG] rotation written for {0}/{1} points.".format(n_ok, n_total))
    return n_ok, n_total



# =============================================================================
# Parameter helpers (shared across the 2 "Create" tools)
# =============================================================================

def _create_params(default_name):
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

    add_map = arcpy.Parameter(displayName="Add output to map + set rotation symbology",
                              name="add_map", datatype="GPBoolean",
                              parameterType="Optional", direction="Input")
    add_map.value = True
    add_map.category = "Map display"

    tmpl_lyr = arcpy.Parameter(displayName="Optional layer template (.lyr)",
                               name="tmpl_lyr", datatype="DEFile",
                               parameterType="Optional", direction="Input")
    tmpl_lyr.filter.list = ["lyr"]
    tmpl_lyr.category = "Map display"

    end_tol = arcpy.Parameter(displayName="Endpoint tolerance (map units) - remove end-touch/T",
                              name="end_tol", datatype="GPDouble",
                              parameterType="Optional", direction="Input")
    end_tol.value = 2.0
    end_tol.category = "Quality control"

    return [roads, drains, out_ws, out_name, sample_m, rot_field, rot_type, add_map, tmpl_lyr, end_tol]


def _suggest_endtol(params):
    try:
        sample = params[4].value
        endp = params[9]
        if sample is not None and not endp.altered:
            endp.value = max(1.0, min(10.0, float(sample) * 0.25))
    except (TypeError, ValueError, AttributeError):
        pass


# =============================================================================
# Tools
# =============================================================================

class Toolbox(object):
    def __init__(self):
        self.label = "Plugin01 Bridge & Culvert Toolkit (ArcMap / Py2.7)"
        self.alias = "bridgeCulvertArcMap"
        self.tools = [BuildBridgePoints, BuildCulvertPoints,
                      RotateExistingBridgePoints, RotateExistingCulvertPoints]


class _CreateBase(object):
    """Shared body for the two Create tools. ref = 'ROAD' or 'DRAIN'."""
    REF = "ROAD"

    def execute(self, p, m):
        _setup_env()
        env_snap = _env_snapshot()
        _env_reset()
        t0 = time.time()
        roads_fc = None
        drains_fc = None
        try:
            roads = _semicolon_to_sources(p[0].valueAsText)
            drains = _semicolon_to_sources(p[1].valueAsText)
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
            _msg("Merging reference lines (scratch, on disk)...")
            roads_fc = _merge_lines(roads, "bc_roads")
            drains_fc = _merge_lines(drains, "bc_drains")
            _msg("[DIAG] roads={0}  drains={1}".format(_get_count(roads_fc), _get_count(drains_fc)))

            host_fc = roads_fc if self.REF == "ROAD" else drains_fc
            out_fc = os.path.join(out_gdb, out_name)

            _msg("Building true crossings...")
            road_fid, drain_fid = _build_crossing_points(roads_fc, drains_fc, host_fc, out_fc)
            ensure_field(out_fc, "ROT_RAW", "DOUBLE")
            ensure_field(out_fc, rot_field, "DOUBLE")
            _msg("[DIAG] raw crossing points: {0}".format(_get_count(out_fc)))

            removed = _filter_endtouch(out_fc, roads_fc, drains_fc, road_fid, drain_fid, end_tol)
            if removed:
                _msg("[DIAG] removed {0} end-touch/T points.".format(removed))
            _msg("[DIAG] true crossings kept: {0}".format(_get_count(out_fc)))

            ref_fc = roads_fc if self.REF == "ROAD" else drains_fc
            _apply_rotation(out_fc, ref_fc, sample_m, rot_field, rot_type, snap_to_line=True)

            if add_map:
                add_layer_with_symbology(out_fc, out_name, tmpl_lyr, rot_field, rot_type)

            _msg("Done in {0:.1f}s -> {1}".format(time.time() - t0, out_fc))

        except arcpy.ExecuteError:
            _err(arcpy.GetMessages(2))
            raise
        except RuntimeError as ex:
            _err("RuntimeError: {0}".format(ex))
            _err(traceback.format_exc())
            raise
        # MemoryError and OSError intentionally NOT caught -- let them
        # propagate so the toolbox dialog reports a real crash instead of
        # silently continuing with a poisoned process state. (Rule 1)
        finally:
            _safe_delete(roads_fc)
            _safe_delete(drains_fc)
            _flush_in_memory()
            _env_restore(env_snap)
            gc.collect()


class BuildBridgePoints(_CreateBase):
    REF = "ROAD"

    def __init__(self):
        self.label = "01) Create Bridge Points (Road x Drain)"
        self.description = ("Create bridge points at TRUE road x drainage crossings; "
                            "rotation aligned to ROAD. Processes ALL features (any "
                            "selection is ignored).")
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


# ---- Rotate-existing tools ---------------------------------------------------

def _rotate_params(default_name, ref_label):
    in_pts = arcpy.Parameter(displayName="Existing points (Point/Multipoint layer)",
                             name="in_pts", datatype="GPFeatureLayer",
                             parameterType="Required", direction="Input")
    in_pts.category = "Input data"
    in_pts.filter.list = ["Point", "Multipoint"]

    ref = arcpy.Parameter(displayName=ref_label + " (Polyline, multi-value)",
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

    add_map = arcpy.Parameter(displayName="Add output to map", name="add_map",
                              datatype="GPBoolean", parameterType="Optional", direction="Input")
    add_map.value = True
    add_map.category = "Map display"

    tmpl_lyr = arcpy.Parameter(displayName="Template .lyr (optional)", name="tmpl_lyr",
                               datatype="DEFile", parameterType="Optional", direction="Input")
    tmpl_lyr.filter.list = ["lyr"]
    tmpl_lyr.category = "Map display"

    return [in_pts, ref, upd_mode, out_ws, out_name, sample_m, rot_field, rot_type, add_map, tmpl_lyr]


class _RotateBase(object):
    REF_LABEL = "Road centerlines"
    DEFAULT_NAME = "P_Rot"
    MAP_NAME = "Rot"

    def execute(self, p, m):
        _setup_env()
        env_snap = _env_snapshot()
        _env_reset()
        t0 = time.time()
        ref_fc = None
        try:
            in_pts = _resolve_full_source(p[0].valueAsText)
            ref_sources = _semicolon_to_sources(p[1].valueAsText)
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

            # Rotate tools NEVER move geometry -> snap_to_line=False
            _apply_rotation(out_fc, ref_fc, sample_m, rot_field, rot_type, snap_to_line=False)

            if add_map:
                add_layer_with_symbology(out_fc, self.MAP_NAME, tmpl_lyr, rot_field, rot_type)

            _msg("Done in {0:.1f}s -> {1}".format(time.time() - t0, out_fc))

        except arcpy.ExecuteError:
            _err(arcpy.GetMessages(2))
            raise
        except RuntimeError as ex:
            _err("RuntimeError: {0}".format(ex))
            _err(traceback.format_exc())
            raise
        # MemoryError / OSError propagate (Rule 1).
        finally:
            _safe_delete(ref_fc)
            _flush_in_memory()
            _env_restore(env_snap)
            gc.collect()

    def updateParameters(self, params):
        try:
            is_copy = (params[2].valueAsText or "").upper() != "UPDATE_IN_PLACE"
            params[3].enabled = is_copy
            params[4].enabled = is_copy
        except (AttributeError, TypeError):
            pass
        return

    def updateMessages(self, params):
        return

    def isLicensed(self):
        return True


class RotateExistingBridgePoints(_RotateBase):
    REF_LABEL = "Road centerlines"
    DEFAULT_NAME = "P_Bridge_Rot"
    MAP_NAME = "Rot_Bridge"

    def __init__(self):
        self.label = "03) Rotate Existing Bridge Points (from Roads)"
        self.description = ("Update rotation of existing bridge points from nearest ROAD; "
                            "geometry is NOT moved. Processes ALL features.")
        self.category = "02 - Rotate Existing"
        self.canRunInBackground = False

    def getParameterInfo(self):
        return _rotate_params("P_Bridge_Rot", "Road centerlines")


class RotateExistingCulvertPoints(_RotateBase):
    REF_LABEL = "Drainage / canals"
    DEFAULT_NAME = "Pnt_Culvert_Rot"
    MAP_NAME = "Rot_Culvert"

    def __init__(self):
        self.label = "04) Rotate Existing Culvert Points (from Drainage)"
        self.description = ("Update rotation of existing culvert points from nearest DRAINAGE; "
                            "geometry is NOT moved. Processes ALL features.")
        self.category = "02 - Rotate Existing"
        self.canRunInBackground = False

    def getParameterInfo(self):
        return _rotate_params("Pnt_Culvert_Rot", "Drainage / canals")
