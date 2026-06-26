# -*- coding: utf-8 -*-
"""
Plugin 02 - Road Deconflict (ArcGIS Pro / Python 3) - Master Rules rewrite
==========================================================================
Moves nearby Point/Line/Polygon features away from a roads barrier to
enforce a clearance distance, without mutating the inputs.

MASTER RULES enforced:
  1. Narrow exceptions at GP-call sites: (arcpy.ExecuteError, RuntimeError).
     MemoryError / OSError are NEVER caught. No bare `except` or
     `except Exception`.
  2. No bulk geometry caches in RAM. Cursors stream inline. The only
     persisted lookup is the near-table dict of small float tuples
     keyed by source OID.
  3. Selection hygiene: _resolve_full_source(ignore_selection=True) by
     default; always processes the full dataset.
  4. arcpy.env snapshot/reset/restore in every execute().
  5. Pro-native: f-strings, native str, arcpy.mp, "memory" workspace
     (no backslashes; never "in_memory").
  6. All cursors inside `with` blocks; scratch datasets and layer views
     cleaned in `finally`.
  7. arcpy.SetProgressor on every long loop.
  8. Deterministic iteration order via ORDER BY OBJECTID.

Specific fixes vs prior revision:
  F1. Replaced O(N x M) nested distance work and the chunked Near
      helper with ONE call to arcpy.analysis.GenerateNearTable per
      target layer (closest_count=1). The result is read once into a
      small {in_fid: (nx, ny, nd, near_fid)} dict and used for the
      whole feature loop. No mutation of NEAR_* fields on the output FC.
  F2. _translate_geometry now preserves True Curve segments. Instead of
      decomposing each Polyline/Polygon to vertex arrays, we shift the
      Esri JSON representation in place (paths/rings AND curvePaths/
      curveRings, including the control points inside `c`/`a`/`q`
      curve segments) and rebuild via arcpy.AsShape(esri_json=True).
      Bezier arcs, circular arcs, and elliptic arcs survive intact.
  F3. _get_count no longer swallows failures. On GetCount error it logs
      a warning AND raises arcpy.ExecuteError so the caller fails fast
      rather than silently treating a locked / missing FC as empty.

Author: Ali Mirjafari + Kiro
Rotation/azimuth convention: 0 = North, clockwise, degrees.
"""

from __future__ import annotations

import math
import os
import csv
import gc
import time
import uuid
import json
import contextlib
import traceback
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

import arcpy


# =============================================================================
# 0. Logging shortcuts (narrow exceptions only)
# =============================================================================

def _msg(s: str) -> None:
    arcpy.AddMessage(str(s))


def _warn(s: str) -> None:
    arcpy.AddWarning(str(s))


def _err(s: str) -> None:
    arcpy.AddError(str(s))


def _diag(s: str) -> None:
    _msg(f"[DIAG] {s}")


def _safe_float(v, default=None):
    if v is None:
        return default
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _safe_int(v, default=None):
    if v is None:
        return default
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


# =============================================================================
# 1. Environment snapshot / restore (Master Rule 4)
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
            _warn(f"Could not restore arcpy.env.{k}: {ex}")


def _prime_env() -> None:
    """Reset env to a known state for the run."""
    arcpy.env.extent = None
    arcpy.env.mask = None
    arcpy.env.outputCoordinateSystem = None
    arcpy.env.overwriteOutput = True
    arcpy.env.parallelProcessingFactor = "100%"
    arcpy.env.autoCancelling = True


# =============================================================================
# 2. Selection hygiene (Master Rule 3) - _resolve_full_source preserved
# =============================================================================

def _selection_info(layer_or_path) -> Tuple[Optional[int], Optional[int], str]:
    """Return (selected_count, total_count, name)."""
    try:
        d = arcpy.Describe(layer_or_path)
    except (arcpy.ExecuteError, RuntimeError):
        return (None, None, str(layer_or_path))
    name = getattr(d, "name", str(layer_or_path))
    fidset = getattr(d, "FIDSet", "") or ""
    try:
        total = int(arcpy.management.GetCount(layer_or_path).getOutput(0))
    except (arcpy.ExecuteError, RuntimeError):
        total = None
    if not fidset.strip():
        return (0, total, name)
    sel = len([t for t in fidset.split(";") if t.strip() != ""])
    return (sel, total, name)


def _resolve_full_source(layer_or_path, ignore_selection: bool = True):
    """
    Return on-disk catalogPath so geoprocessing tools always see the FULL
    dataset when ignore_selection is True (the documented default).
    If ignore_selection is False, return the token unchanged so the user's
    on-map selection is honoured.
    """
    if not layer_or_path:
        return layer_or_path
    if not ignore_selection:
        return layer_or_path
    try:
        d = arcpy.Describe(layer_or_path)
    except (arcpy.ExecuteError, RuntimeError):
        return layer_or_path
    cp = getattr(d, "catalogPath", None)
    if cp:
        return cp
    return layer_or_path


def _announce_selection(label: str, layer_or_path) -> None:
    sel, total, name = _selection_info(layer_or_path)
    if sel and sel > 0:
        _warn(
            f"{label}: '{name}' has an active selection ({sel} of "
            f"{total if total is not None else '?'}). Ignoring selection - "
            f"processing FULL dataset."
        )
    else:
        _diag(
            f"{label}: '{name}' total={total if total is not None else '?'}, "
            f"no active selection."
        )


# =============================================================================
# 3. GP retry wrapper (narrow exceptions; backoff only on retryable errors)
# =============================================================================

def _gp_try(func, args, kwargs=None, retries: int = 3, sleep_s: float = 2.0):
    """
    Run a GP function with limited retries on transient lock errors.
    Only catches arcpy.ExecuteError / RuntimeError. MemoryError and
    OSError propagate immediately so we never paper over real crashes.
    """
    if kwargs is None:
        kwargs = {}
    last_err: Optional[Exception] = None
    for i in range(retries + 1):
        try:
            return func(*args, **kwargs)
        except (arcpy.ExecuteError, RuntimeError) as e:
            last_err = e
            try:
                arcpy.management.ClearWorkspaceCache()
            except (arcpy.ExecuteError, RuntimeError):
                pass
            if i >= retries:
                raise
            time.sleep(sleep_s * (i + 1))
    if last_err is not None:
        raise last_err


# =============================================================================
# 4. Naming
# =============================================================================

def _sanitize_name(name: str, workspace: str) -> str:
    name = (name or "output").replace(" ", "_").replace("-", "_")
    name = name.replace(".lyr", "").replace(".shp", "").replace(".lyrx", "")
    try:
        return arcpy.ValidateTableName(name, workspace)
    except (arcpy.ExecuteError, RuntimeError):
        return name


def _new_name(base: str, suffix: str, workspace: str) -> str:
    base = _sanitize_name(base, workspace)
    suffix = (suffix or "").replace(" ", "_")
    try:
        cand = arcpy.ValidateTableName(base + suffix, workspace)
    except (arcpy.ExecuteError, RuntimeError):
        cand = base + suffix
    if arcpy.Exists(os.path.join(workspace, cand)):
        try:
            cand = arcpy.ValidateTableName(
                cand + "_" + uuid.uuid4().hex[:6], workspace)
        except (arcpy.ExecuteError, RuntimeError):
            cand = cand + "_" + uuid.uuid4().hex[:6]
    return cand


def _copy_or_project(in_layer_or_path, out_fc: str, target_sr) -> None:
    src = _resolve_full_source(in_layer_or_path)
    try:
        d = arcpy.Describe(src)
    except (arcpy.ExecuteError, RuntimeError) as ex:
        _warn(f"Describe failed; using CopyFeatures: {ex}")
        _gp_try(arcpy.management.CopyFeatures, [src, out_fc])
        return
    sr_in = getattr(d, "spatialReference", None)
    if (sr_in and target_sr
            and getattr(sr_in, "name", None)
            and getattr(target_sr, "name", None)
            and sr_in.name != target_sr.name):
        _msg(f"Projecting '{d.name}' to roads SR -> {out_fc}")
        try:
            _gp_try(arcpy.management.Project, [src, out_fc, target_sr])
            return
        except (arcpy.ExecuteError, RuntimeError):
            _warn(f"Project failed for '{d.name}'; falling back to CopyFeatures.")
    _gp_try(arcpy.management.CopyFeatures, [src, out_fc])


# =============================================================================
# 5. SR / count (Fix F3: _get_count raises on failure)
# =============================================================================

def _is_projected(fc_or_layer) -> Tuple[bool, object]:
    desc = arcpy.Describe(fc_or_layer)
    sr = getattr(desc, "spatialReference", None)
    if sr is None or sr.name in (None, "", "Unknown"):
        return (False, sr)
    if sr.type != "Projected":
        return (False, sr)
    return (True, sr)


def _get_count(fc_or_layer) -> int:
    """
    Return feature count. F3: on GetCount failure, log a warning AND raise
    arcpy.ExecuteError. We never silently return 0 / -1 - that would let
    a locked, missing, or schema-broken FC look like an empty FC.
    """
    try:
        return int(arcpy.management.GetCount(fc_or_layer).getOutput(0))
    except (arcpy.ExecuteError, RuntimeError) as ex:
        _warn(f"GetCount failed on '{fc_or_layer}': {ex}")
        raise arcpy.ExecuteError(
            f"GetCount failed on '{fc_or_layer}'. The dataset may be locked, "
            f"missing, or have a corrupt schema. Aborting to avoid silent "
            f"empty-input behavior. Original error: {ex}"
        )


# =============================================================================
# 6. AOI helpers
# =============================================================================

def _update_extent(minx, miny, maxx, maxy, ext):
    if ext is None:
        return (minx, miny, maxx, maxy)
    if minx is None:
        return (ext.XMin, ext.YMin, ext.XMax, ext.YMax)
    return (min(minx, ext.XMin), min(miny, ext.YMin),
            max(maxx, ext.XMax), max(maxy, ext.YMax))


def _extent_from_layers(layers: Iterable[str]) -> Optional[Tuple[float, float, float, float]]:
    minx = miny = maxx = maxy = None
    for lyr in layers:
        try:
            d = arcpy.Describe(_resolve_full_source(lyr))
        except (arcpy.ExecuteError, RuntimeError) as ex:
            _warn(f"Describe failed for '{lyr}': {ex}")
            continue
        ext = getattr(d, "extent", None)
        if ext:
            minx, miny, maxx, maxy = _update_extent(minx, miny, maxx, maxy, ext)
    if minx is None:
        return None
    return (minx, miny, maxx, maxy)


def _extent_polygon_geom(ext_tuple, sr, margin: float):
    (minx, miny, maxx, maxy) = ext_tuple
    minx -= margin
    miny -= margin
    maxx += margin
    maxy += margin
    arr = arcpy.Array([
        arcpy.Point(minx, miny),
        arcpy.Point(maxx, miny),
        arcpy.Point(maxx, maxy),
        arcpy.Point(minx, maxy),
        arcpy.Point(minx, miny),
    ])
    return arcpy.Polygon(arr, sr)


def _extent_polygon_fc(ext_geom, scratch_ws: str) -> str:
    fc = os.path.join(scratch_ws, "rdcl_aoi_" + uuid.uuid4().hex[:6])
    _gp_try(arcpy.management.CreateFeatureclass,
            [scratch_ws, os.path.basename(fc), "POLYGON"],
            {"spatial_reference": ext_geom.spatialReference})
    with arcpy.da.InsertCursor(fc, ["SHAPE@"]) as ic:
        ic.insertRow([ext_geom])
    return fc


def _clip_roads_if_needed(in_roads_path: str, aoi_fc: Optional[str],
                          scratch_ws: str) -> str:
    if not aoi_fc:
        return in_roads_path
    out_fc = os.path.join(scratch_ws, "rdcl_roadsclip_" + uuid.uuid4().hex[:6])
    try:
        _gp_try(arcpy.analysis.Clip, [in_roads_path, aoi_fc, out_fc])
    except (arcpy.ExecuteError, RuntimeError):
        _warn("AOI clip failed; using full roads layer instead.")
        _warn(traceback.format_exc())
        return in_roads_path
    if _get_count(out_fc) > 0:
        return out_fc
    _warn("AOI clip produced 0 road features; using full roads layer instead.")
    return in_roads_path


# =============================================================================
# 7. Geometry math (basic)
# =============================================================================

def _azimuth_deg(dx: float, dy: float) -> float:
    ang = math.degrees(math.atan2(dx, dy))
    if ang < 0:
        ang += 360.0
    return ang


def _unit_normal_from_tangent(tx: float, ty: float, side: str) -> Tuple[float, float]:
    if abs(tx) < 1e-12 and abs(ty) < 1e-12:
        return (1.0, 0.0)
    nx, ny = (ty, -tx) if side == "RIGHT" else (-ty, tx)
    n = math.sqrt(nx * nx + ny * ny)
    if n < 1e-12:
        return (1.0, 0.0)
    return (nx / n, ny / n)


def _rotate_unit(ux: float, uy: float, deg: float) -> Tuple[float, float]:
    try:
        r = math.radians(float(deg))
    except (TypeError, ValueError):
        return (ux, uy)
    c = math.cos(r)
    s = math.sin(r)
    return (ux * c - uy * s, ux * s + uy * c)


def _tangent_at_distance(polyline, dist_along: float) -> Tuple[float, float]:
    """Use positionAlongLine(d).firstPoint exclusively; never segmentAlongLine."""
    try:
        total = polyline.length
        eps = max(total * 1e-6, 0.01)
        d0 = max(0.0, min(total, dist_along - eps))
        d1 = max(0.0, min(total, dist_along + eps))
        p0 = polyline.positionAlongLine(d0, False).firstPoint
        p1 = polyline.positionAlongLine(d1, False).firstPoint
    except (arcpy.ExecuteError, RuntimeError):
        return (1.0, 0.0)
    tx = p1.X - p0.X
    ty = p1.Y - p0.Y
    n = math.sqrt(tx * tx + ty * ty)
    if n < 1e-12:
        return (1.0, 0.0)
    return (tx / n, ty / n)


def _nearest_point_and_side(road_geom, pt_geom):
    out = road_geom.queryPointAndDistance(pt_geom, False)
    p_on = out[0]
    dist_along = out[1]
    dist_from = out[2]
    side = out[3] if len(out) > 3 else None
    if side not in ("LEFT", "RIGHT"):
        side = None
    return (p_on, dist_along, dist_from, side)


def _mk_point(x, y, z=None, m=None):
    try:
        return arcpy.Point(x, y, z, m)
    except (TypeError, ValueError):
        p = arcpy.Point(x, y)
        if z is not None:
            try:
                p.Z = z
            except (TypeError, ValueError, AttributeError):
                pass
        if m is not None:
            try:
                p.M = m
            except (TypeError, ValueError, AttributeError):
                pass
        return p


def _blend_zm(p0, p1, t: float):
    z = m = None
    try:
        if p0.Z is not None and p1.Z is not None:
            z = p0.Z + (p1.Z - p0.Z) * t
    except (TypeError, ValueError, AttributeError):
        z = None
    try:
        if p0.M is not None and p1.M is not None:
            m = p0.M + (p1.M - p0.M) * t
    except (TypeError, ValueError, AttributeError):
        m = None
    return z, m


def _unique_consecutive(points, tol: float = 1e-9):
    if not points:
        return []
    out = [points[0]]
    for p in points[1:]:
        if p is None:
            continue
        q = out[-1]
        if abs(p.X - q.X) > tol or abs(p.Y - q.Y) > tol:
            out.append(p)
    return out



# =============================================================================
# 8. True-curve-preserving translation (Fix F2)
# =============================================================================
#
# Why this exists:
#   The previous _translate_geometry walked geom.parts and called
#   arcpy.Point(p.X+dx, p.Y+dy, ...) for every vertex, then rebuilt
#   the geometry from a vertex Array. That is fine for densified
#   feature classes, but it DESTROYS true-curve segments: an Esri
#   polyline with a `c` (Bezier) or `a` (circular arc) curvePath
#   becomes a straight chord between the curve's start and end.
#
# Fix:
#   Translate the geometry's Esri JSON directly. Esri JSON encodes
#   curves as objects inside `curvePaths` / `curveRings`. Each curve
#   segment carries its own control point coordinates. We shift every
#   coordinate pair we encounter, including those nested inside curve
#   segments. Then arcpy.AsShape(json_dict, esri_json=True) rebuilds
#   the geometry with all curves intact.
#
# Reference: ArcGIS REST geometry JSON spec (`paths`, `rings`,
# `curvePaths`, `curveRings`; segment kinds `c` Bezier, `a` arc, `q`
# quadratic, plain [x, y] vertex).

_CURVE_KEYS = ("c", "a", "q", "b")  # Bezier, circular arc, quadratic, b-spline


def _shift_coord_list(coord_list, dx: float, dy: float) -> None:
    """In-place shift of a list of [x, y, ...] coordinate arrays."""
    for c in coord_list:
        if not isinstance(c, list) or len(c) < 2:
            continue
        c[0] = c[0] + dx
        c[1] = c[1] + dy
        # leave Z (index 2) and M (index 3) untouched


def _shift_curve_segment(seg: dict, dx: float, dy: float) -> None:
    """
    A curve segment in Esri JSON is a dict like:
        {"c": [[endX, endY, z?, m?], [interiorX, interiorY, z?, m?]]}
        {"a": [[endX, endY, z?, m?], [centerX, centerY], minor, clockwise,
               rotation, axis]}
        {"q": [[endX, endY, z?, m?], [interiorX, interiorY]]}
    We shift every nested [x, y] coordinate pair we find inside.
    For 'a' we also shift the center.
    """
    for key in _CURVE_KEYS:
        if key not in seg:
            continue
        payload = seg[key]
        if not isinstance(payload, list):
            continue
        # Element 0 is always the segment endpoint coord.
        if isinstance(payload[0], list) and len(payload[0]) >= 2:
            payload[0][0] = payload[0][0] + dx
            payload[0][1] = payload[0][1] + dy
        if key == "c" or key == "q":
            # Element 1 is the interior control coord.
            if (len(payload) > 1
                    and isinstance(payload[1], list)
                    and len(payload[1]) >= 2):
                payload[1][0] = payload[1][0] + dx
                payload[1][1] = payload[1][1] + dy
        elif key == "a":
            # Element 1 is the center coord.
            if (len(payload) > 1
                    and isinstance(payload[1], list)
                    and len(payload[1]) >= 2):
                payload[1][0] = payload[1][0] + dx
                payload[1][1] = payload[1][1] + dy
        return  # only one curve key per segment dict


def _shift_curve_path(path, dx: float, dy: float) -> None:
    """A curvePath/curveRing is a list whose elements are either
    plain [x, y] vertex coords OR curve-segment dicts."""
    for elem in path:
        if isinstance(elem, dict):
            _shift_curve_segment(elem, dx, dy)
        elif isinstance(elem, list) and len(elem) >= 2:
            elem[0] = elem[0] + dx
            elem[1] = elem[1] + dy


def _translate_geometry(geom, dx: float, dy: float):
    """
    Curve-aware geometry translation.

    Points are shifted directly (no JSON round-trip needed).
    Polylines and polygons go through Esri JSON so true-curve segments
    in `curvePaths` / `curveRings` are preserved exactly.
    """
    if geom is None:
        return None
    sr = geom.spatialReference
    gtype = geom.type.lower() if geom.type else ""

    if gtype == "point":
        p = geom.firstPoint
        z = getattr(p, "Z", None)
        m = getattr(p, "M", None)
        return arcpy.PointGeometry(arcpy.Point(p.X + dx, p.Y + dy, z, m), sr)

    if gtype == "multipoint":
        # Multipoints have no curves; translate via JSON anyway.
        try:
            j = json.loads(geom.JSON)
        except (TypeError, ValueError):
            return geom
        pts = j.get("points")
        if isinstance(pts, list):
            _shift_coord_list(pts, dx, dy)
        return arcpy.AsShape(j, True)

    if gtype not in ("polyline", "polygon"):
        return geom

    # Polyline / Polygon - shift JSON in place, preserving curves.
    try:
        j = json.loads(geom.JSON)
    except (TypeError, ValueError) as ex:
        _warn(f"Geometry JSON parse failed; falling back to vertex shift: {ex}")
        return _translate_geometry_vertex_fallback(geom, dx, dy)

    if "paths" in j and isinstance(j["paths"], list):
        for path in j["paths"]:
            if isinstance(path, list):
                _shift_coord_list(path, dx, dy)
    if "rings" in j and isinstance(j["rings"], list):
        for ring in j["rings"]:
            if isinstance(ring, list):
                _shift_coord_list(ring, dx, dy)
    if "curvePaths" in j and isinstance(j["curvePaths"], list):
        for path in j["curvePaths"]:
            if isinstance(path, list):
                _shift_curve_path(path, dx, dy)
    if "curveRings" in j and isinstance(j["curveRings"], list):
        for ring in j["curveRings"]:
            if isinstance(ring, list):
                _shift_curve_path(ring, dx, dy)

    try:
        return arcpy.AsShape(j, True)
    except (arcpy.ExecuteError, RuntimeError, ValueError) as ex:
        _warn(f"AsShape failed on shifted JSON; falling back: {ex}")
        return _translate_geometry_vertex_fallback(geom, dx, dy)


def _translate_geometry_vertex_fallback(geom, dx: float, dy: float):
    """Last-resort vertex-decomposition translator. Used only if JSON
    round-trip fails. Loses true curves - documented and warned."""
    if geom is None:
        return None
    sr = geom.spatialReference
    gtype = geom.type.lower() if geom.type else ""
    arr = arcpy.Array()
    for part in geom:
        part_arr = arcpy.Array()
        for p in part:
            if p is None:
                part_arr.add(None)
            else:
                z = getattr(p, "Z", None)
                m = getattr(p, "M", None)
                part_arr.add(arcpy.Point(p.X + dx, p.Y + dy, z, m))
        arr.add(part_arr)
    if gtype == "polyline":
        try:
            return arcpy.Polyline(arr, sr, geom.hasZ, geom.hasM)
        except TypeError:
            return arcpy.Polyline(arr, sr)
    if gtype == "polygon":
        try:
            return arcpy.Polygon(arr, sr, geom.hasZ, geom.hasM)
        except TypeError:
            return arcpy.Polygon(arr, sr)
    return geom


# =============================================================================
# 9. Densify / smooth / deflection cap (vertex-level helpers for line push)
# =============================================================================

def _densify_polyline_points(points: Sequence, step: Optional[float]):
    if step is None or step <= 0:
        return list(points)
    out = []
    for i in range(len(points) - 1):
        p0 = points[i]
        p1 = points[i + 1]
        if i == 0:
            out.append(p0)
        dx = p1.X - p0.X
        dy = p1.Y - p0.Y
        seg_len = math.sqrt(dx * dx + dy * dy)
        if seg_len < 1e-12:
            out.append(p1)
            continue
        n = int(seg_len / float(step))
        if n <= 0:
            out.append(p1)
            continue
        ux = dx / seg_len
        uy = dy / seg_len
        for k in range(1, n + 1):
            dist = k * float(step)
            if dist >= seg_len - 1e-9:
                break
            t = dist / seg_len
            z, m = _blend_zm(p0, p1, t)
            out.append(_mk_point(p0.X + ux * dist, p0.Y + uy * dist, z, m))
        out.append(p1)
    return _unique_consecutive(out)


def _chaikin_smooth(points: Sequence, iterations: int, preserve_ends: bool = True):
    pts = list(points)
    for _ in range(int(iterations or 0)):
        if len(pts) < 3:
            break
        new_pts = []
        if preserve_ends:
            new_pts.append(pts[0])
        for i in range(len(pts) - 1):
            p0 = pts[i]
            p1 = pts[i + 1]
            qx = 0.75 * p0.X + 0.25 * p1.X
            qy = 0.75 * p0.Y + 0.25 * p1.Y
            rx = 0.25 * p0.X + 0.75 * p1.X
            ry = 0.25 * p0.Y + 0.75 * p1.Y
            qz = qm = rz = rm = None
            try:
                if p0.Z is not None and p1.Z is not None:
                    qz = 0.75 * p0.Z + 0.25 * p1.Z
                    rz = 0.25 * p0.Z + 0.75 * p1.Z
            except (TypeError, ValueError, AttributeError):
                pass
            try:
                if p0.M is not None and p1.M is not None:
                    qm = 0.75 * p0.M + 0.25 * p1.M
                    rm = 0.25 * p0.M + 0.75 * p1.M
            except (TypeError, ValueError, AttributeError):
                pass
            new_pts.append(_mk_point(qx, qy, qz, qm))
            new_pts.append(_mk_point(rx, ry, rz, rm))
        if preserve_ends:
            new_pts.append(pts[-1])
        pts = _unique_consecutive(new_pts)
    return pts


def _angle_deg(v1x, v1y, v2x, v2y) -> float:
    n1 = math.sqrt(v1x * v1x + v1y * v1y)
    n2 = math.sqrt(v2x * v2x + v2y * v2y)
    if n1 < 1e-12 or n2 < 1e-12:
        return 0.0
    dot = (v1x * v2x + v1y * v2y) / (n1 * n2)
    dot = max(-1.0, min(1.0, dot))
    return math.degrees(math.acos(dot))


def _cap_deflection(prev_p, orig_p, next_p, cand_p, max_delta_deg):
    if max_delta_deg is None or max_delta_deg <= 0:
        return cand_p
    if prev_p is None or next_p is None:
        return cand_p
    a0 = _angle_deg(orig_p.X - prev_p.X, orig_p.Y - prev_p.Y,
                    next_p.X - orig_p.X, next_p.Y - orig_p.Y)
    a1 = _angle_deg(cand_p.X - prev_p.X, cand_p.Y - prev_p.Y,
                    next_p.X - cand_p.X, next_p.Y - cand_p.Y)
    if abs(a1 - a0) <= max_delta_deg:
        return cand_p
    lo = 0.0
    hi = 1.0
    best = orig_p
    for _ in range(8):
        mid = 0.5 * (lo + hi)
        tx = orig_p.X + (cand_p.X - orig_p.X) * mid
        ty = orig_p.Y + (cand_p.Y - orig_p.Y) * mid
        z, m = _blend_zm(orig_p, cand_p, mid)
        test = _mk_point(tx, ty, z, m)
        at = _angle_deg(test.X - prev_p.X, test.Y - prev_p.Y,
                        next_p.X - test.X, next_p.Y - test.Y)
        if abs(at - a0) <= max_delta_deg:
            best = test
            lo = mid
        else:
            hi = mid
    return best


# =============================================================================
# 10. Roads preprocess
# =============================================================================

def _dissolve_to_single_geom(in_roads_path: str, scratch_ws: str):
    out_fc = os.path.join(scratch_ws, "rdcl_diss_" + uuid.uuid4().hex[:6])
    _gp_try(arcpy.management.Dissolve,
            [in_roads_path, out_fc, "", "", "MULTI_PART", "DISSOLVE_LINES"])
    geom = None
    with arcpy.da.SearchCursor(out_fc, ["SHAPE@"]) as cur:
        for row in cur:
            geom = row[0]
            break
    return out_fc, geom


def _buffer_fc(in_fc: str, out_fc: str, dist_map_units,
               force_units: str = "MAP_UNITS") -> str:
    if force_units and force_units != "MAP_UNITS":
        dist_str = f"{dist_map_units} {force_units}"
    else:
        dist_str = f"{dist_map_units}"
    _gp_try(arcpy.analysis.Buffer,
            [in_fc, out_fc, dist_str, "FULL", "ROUND", "ALL"])
    return out_fc


# =============================================================================
# 11. Field helpers
# =============================================================================

def _ensure_fields(fc: str, field_specs):
    existing = [f.name.upper() for f in arcpy.ListFields(fc)]
    for (fname, ftype, flen) in field_specs:
        if fname.upper() in existing:
            continue
        if ftype.upper() == "TEXT":
            _gp_try(arcpy.management.AddField, [fc, fname, ftype],
                    {"field_length": flen or 255})
        else:
            _gp_try(arcpy.management.AddField, [fc, fname, ftype])


# =============================================================================
# 12. Near table (Fix F1) - run GenerateNearTable ONCE per target layer
# =============================================================================

def _build_near_table(target_layer: str, road_fc: str,
                      scratch_ws: str) -> Dict[int, Tuple[float, float, float, int]]:
    """
    Run arcpy.analysis.GenerateNearTable(in=target_layer, near=road_fc,
    closest_count=1, location=YES) once and return a small dict:

        { in_fid : (near_x, near_y, near_dist, near_fid) }

    Memory profile: one entry per target feature; only floats and ints,
    no geometries. Streams directly out of the GenerateNearTable result
    into the dict via a SearchCursor; the result table is written to
    the in-Pro 'memory' workspace (Master Rule 5) and deleted after read.

    F1 rationale: the prior implementation called arcpy.analysis.Near
    in chunks AND mutated the target FC by adding NEAR_X / NEAR_Y /
    NEAR_DIST fields, requiring an AddField/DeleteField shuffle and
    re-reading those fields inside the per-feature UpdateCursor. The
    inner loop was therefore O(N) GP calls per chunk; with the
    'fall back to per-feature distanceTo' code path it could degrade
    to O(N x M). GenerateNearTable runs the spatial-index match ONCE
    for all features and never touches the input schema.
    """
    n_target = _get_count(target_layer)
    if n_target <= 0:
        return {}

    near_tbl = arcpy.CreateUniqueName("rdcl_near_tbl", "memory")
    out: Dict[int, Tuple[float, float, float, int]] = {}
    try:
        _gp_try(
            arcpy.analysis.GenerateNearTable,
            [target_layer, [road_fc], near_tbl],
            {
                "search_radius": "",
                "location": "LOCATION",
                "angle": "NO_ANGLE",
                "closest": "CLOSEST",
                "closest_count": 1,
                "method": "PLANAR",
            },
        )
        with arcpy.da.SearchCursor(
                near_tbl,
                ["IN_FID", "NEAR_FID", "NEAR_DIST", "NEAR_X", "NEAR_Y"]) as cur:
            for in_fid, near_fid, near_dist, near_x, near_y in cur:
                if in_fid is None or near_x is None or near_y is None:
                    continue
                out[int(in_fid)] = (
                    float(near_x),
                    float(near_y),
                    float(near_dist) if near_dist is not None else 0.0,
                    int(near_fid) if near_fid is not None else -1,
                )
    finally:
        try:
            if arcpy.Exists(near_tbl):
                arcpy.management.Delete(near_tbl)
        except (arcpy.ExecuteError, RuntimeError) as ex:
            _warn(f"Could not delete near table '{near_tbl}': {ex}")
    return out


# =============================================================================
# 13. Point displacement
# =============================================================================

def _push_point_to_clearance(pt_geom, road_geom, clearance,
                             max_shift=None, prefer_side=None):
    p_on, dist_along, dist_from, side = _nearest_point_and_side(road_geom, pt_geom)
    if dist_from >= clearance:
        return (pt_geom, False, 0.0, None, "OK (no move)")
    dx = pt_geom.firstPoint.X - p_on.firstPoint.X
    dy = pt_geom.firstPoint.Y - p_on.firstPoint.Y
    d = math.sqrt(dx * dx + dy * dy)
    if d < 1e-9:
        tx, ty = _tangent_at_distance(road_geom, dist_along)
        chosen_side = prefer_side or side or "LEFT"
        ux, uy = _unit_normal_from_tangent(tx, ty, chosen_side)
        note = f"Point on road; used normal direction ({chosen_side})"
    else:
        ux, uy = (dx / d, dy / d)
        note = "Used road->point direction"
    desired_x = p_on.firstPoint.X + ux * clearance
    desired_y = p_on.firstPoint.Y + uy * clearance
    if max_shift is not None and max_shift > 0:
        cap = float(max_shift)
        sdx = desired_x - pt_geom.firstPoint.X
        sdy = desired_y - pt_geom.firstPoint.Y
        sdist = math.sqrt(sdx * sdx + sdy * sdy)
        if sdist > cap and sdist > 1e-12:
            ux2, uy2 = (sdx / sdist, sdy / sdist)
            desired_x = pt_geom.firstPoint.X + ux2 * cap
            desired_y = pt_geom.firstPoint.Y + uy2 * cap
            note += " | CAPPED by MaxShift"
    new_geom = arcpy.PointGeometry(arcpy.Point(desired_x, desired_y),
                                   pt_geom.spatialReference)
    sx = new_geom.firstPoint.X - pt_geom.firstPoint.X
    sy = new_geom.firstPoint.Y - pt_geom.firstPoint.Y
    sdist = math.sqrt(sx * sx + sy * sy)
    return (new_geom, True, sdist, _azimuth_deg(sx, sy), note)


def _push_point_to_clearance_from_near(pt_geom, near_x, near_y, near_dist,
                                       clearance, road_geom=None,
                                       max_shift=None):
    """Use precomputed NEAR_* values from GenerateNearTable."""
    try:
        dist_from = float(near_dist)
    except (TypeError, ValueError):
        dist_from = None
    if dist_from is None:
        return _push_point_to_clearance(pt_geom, road_geom, clearance,
                                        max_shift=max_shift)
    if dist_from >= clearance:
        return (pt_geom, False, 0.0, None, "OK (no move)")
    dx = pt_geom.firstPoint.X - float(near_x)
    dy = pt_geom.firstPoint.Y - float(near_y)
    d = math.sqrt(dx * dx + dy * dy)
    if d < 1e-9:
        if road_geom is not None:
            return _push_point_to_clearance(pt_geom, road_geom, clearance,
                                            max_shift=max_shift)
        ux, uy = (1.0, 0.0)
        note = "Near dir undefined; used default normal"
    else:
        ux, uy = (dx / d, dy / d)
        note = "Used NEAR_* direction"
    desired_x = float(near_x) + ux * clearance
    desired_y = float(near_y) + uy * clearance
    if max_shift is not None and max_shift > 0:
        cap = float(max_shift)
        sdx = desired_x - pt_geom.firstPoint.X
        sdy = desired_y - pt_geom.firstPoint.Y
        sdist = math.sqrt(sdx * sdx + sdy * sdy)
        if sdist > cap and sdist > 1e-12:
            ux2, uy2 = (sdx / sdist, sdy / sdist)
            desired_x = pt_geom.firstPoint.X + ux2 * cap
            desired_y = pt_geom.firstPoint.Y + uy2 * cap
            note += " | CAPPED by MaxShift"
    new_geom = arcpy.PointGeometry(arcpy.Point(desired_x, desired_y),
                                   pt_geom.spatialReference)
    sx = new_geom.firstPoint.X - pt_geom.firstPoint.X
    sy = new_geom.firstPoint.Y - pt_geom.firstPoint.Y
    sdist = math.sqrt(sx * sx + sy * sy)
    return (new_geom, True, sdist, _azimuth_deg(sx, sy), note)


# =============================================================================
# 14. Polygon translation refinement (uses curve-preserving _translate_geometry)
# =============================================================================

def _try_translate_with_refinement(geom, road_geom, clearance, ux, uy,
                                   dist0, max_shift, max_iter):
    try:
        base = float(dist0) if dist0 is not None else 0.0
    except (TypeError, ValueError):
        base = 0.0
    total_shift = float(max(0.0, clearance - base))
    capped = False
    if max_shift is not None and max_shift > 0 and total_shift > max_shift:
        total_shift = float(max_shift)
        capped = True
    new_geom = _translate_geometry(geom, ux * total_shift, uy * total_shift)
    for _ in range(int(max_iter or 0)):
        try:
            d1 = road_geom.distanceTo(new_geom)
        except (arcpy.ExecuteError, RuntimeError):
            d1 = clearance
        if d1 >= clearance:
            break
        extra = clearance - d1
        if extra <= 0:
            break
        if (max_shift is not None and max_shift > 0
                and (total_shift + extra) > max_shift):
            extra = max(0.0, float(max_shift) - total_shift)
        if extra <= 0:
            break
        new_geom = _translate_geometry(new_geom, ux * extra, uy * extra)
        total_shift += extra
    try:
        still = (road_geom.distanceTo(new_geom) < clearance)
    except (arcpy.ExecuteError, RuntimeError):
        still = False
    return new_geom, total_shift, still, capped


def _best_polygon_translation(geom, road_geom, clearance, ux, uy, dist0,
                              dist_along=None, max_shift=None, max_iter=0,
                              side=None):
    best_geom, best_shift, best_still, _ = _try_translate_with_refinement(
        geom, road_geom, clearance, ux, uy, dist0, max_shift, max_iter)
    best_dir = (ux, uy)
    if not best_still:
        return best_geom, best_shift, best_still, "Translated polygon"
    dirs: List[Tuple[float, float]] = []
    if dist_along is not None:
        tx, ty = _tangent_at_distance(road_geom, dist_along)
        dirs.append(_unit_normal_from_tangent(tx, ty, "LEFT"))
        dirs.append(_unit_normal_from_tangent(tx, ty, "RIGHT"))
        if side in ("LEFT", "RIGHT"):
            d_pref = _unit_normal_from_tangent(tx, ty, side)
            dirs = [d_pref] + [d for d in dirs if d != d_pref]
    for ang in (15, -15, 30, -30, 45, -45, 60, -60, 90, -90):
        dirs.append(_rotate_unit(ux, uy, ang))
    cleared = []
    best_dist = -1.0
    for (dux, duy) in dirs:
        if (abs(dux - best_dir[0]) < 1e-6
                and abs(duy - best_dir[1]) < 1e-6):
            continue
        g2, sh2, still2, _c = _try_translate_with_refinement(
            geom, road_geom, clearance, dux, duy, dist0, max_shift, max_iter)
        try:
            d2 = road_geom.distanceTo(g2)
        except (arcpy.ExecuteError, RuntimeError):
            d2 = 0.0
        if not still2 and d2 >= clearance:
            cleared.append((sh2, g2, (dux, duy), d2))
        if d2 > best_dist:
            best_dist = d2
            best_geom = g2
            best_shift = sh2
            best_still = still2
    if cleared:
        cleared.sort(key=lambda t: t[0])
        best_shift, best_geom, _bd, _ = cleared[0]
        return best_geom, best_shift, False, "Translated polygon (refined direction)"
    return best_geom, best_shift, best_still, "Translated polygon (best-effort)"


# =============================================================================
# 15. Polyline displacement
# =============================================================================

def _local_push_polyline(line_geom, road_geom, road_buffer_geom, clearance,
                         densify_step=None, preserve_endpoints=True,
                         smooth_iters=1, max_shift=None, max_iter=3,
                         max_deflection_deg=None):
    sr = line_geom.spatialReference
    moved_any = False
    note = ""
    max_v_shift = 0.0
    current_geom = line_geom
    for _it in range(int(max_iter)):
        moved_this_iter = False
        new_parts = arcpy.Array()
        for part in current_geom:
            raw_pts = [p for p in part if p]
            if len(raw_pts) < 2:
                continue
            pts = _densify_polyline_points(raw_pts, densify_step)
            new_pts = []
            for idx, p in enumerate(pts):
                if preserve_endpoints and (idx == 0 or idx == len(pts) - 1):
                    new_pts.append(p)
                    continue
                pg = arcpy.PointGeometry(p, sr)
                inside = False
                try:
                    inside = road_buffer_geom.contains(pg)
                except (arcpy.ExecuteError, RuntimeError):
                    try:
                        inside = (road_geom.distanceTo(pg) < clearance)
                    except (arcpy.ExecuteError, RuntimeError):
                        inside = False
                if not inside:
                    new_pts.append(p)
                    continue
                new_pg, moved, sh, _az, _n = _push_point_to_clearance(
                    pg, road_geom, clearance, max_shift=max_shift)
                cand = new_pg.firstPoint
                prev_p = pts[idx - 1] if idx - 1 >= 0 else None
                next_p = pts[idx + 1] if idx + 1 < len(pts) else None
                if prev_p and next_p:
                    cand = _cap_deflection(prev_p, p, next_p, cand,
                                           max_deflection_deg)
                new_pts.append(cand)
                if moved:
                    moved_this_iter = True
                    moved_any = True
                    if sh > max_v_shift:
                        max_v_shift = sh
            new_pts = _unique_consecutive(new_pts)
            if smooth_iters and smooth_iters > 0 and len(new_pts) >= 3:
                new_pts = _chaikin_smooth(new_pts, smooth_iters,
                                          preserve_ends=True)
            if len(new_pts) < 2:
                new_pts = list(raw_pts)
            arr = arcpy.Array()
            for pp in new_pts:
                arr.add(pp)
            new_parts.add(arr)
        try:
            new_geom = arcpy.Polyline(new_parts, sr,
                                      current_geom.hasZ, current_geom.hasM)
        except TypeError:
            new_geom = arcpy.Polyline(new_parts, sr)
        try:
            conflict_left = (not road_buffer_geom.disjoint(new_geom))
        except (arcpy.ExecuteError, RuntimeError):
            try:
                conflict_left = (road_geom.distanceTo(new_geom) < clearance)
            except (arcpy.ExecuteError, RuntimeError):
                conflict_left = False
        current_geom = new_geom
        if not moved_this_iter:
            if conflict_left:
                note += "No more vertex moves; conflict may remain. "
            break
        if not conflict_left:
            break
    final_geom = current_geom
    try:
        still_conflict = (not road_buffer_geom.disjoint(final_geom))
    except (arcpy.ExecuteError, RuntimeError):
        try:
            still_conflict = (road_geom.distanceTo(final_geom) < clearance)
        except (arcpy.ExecuteError, RuntimeError):
            still_conflict = False
    note += "LocalPush applied" if moved_any else "OK (no move)"
    return (final_geom, moved_any, max_v_shift, note, still_conflict)


def _whole_offset_best_side(line_geom, road_buffer_geom, clearance,
                            force_side: str = "AUTO"):
    candidates = []
    notes = []
    sides = [force_side] if force_side in ("LEFT", "RIGHT") else ["LEFT", "RIGHT"]
    for side in sides:
        try:
            off = line_geom.parallelOffset(clearance, side, "ROUND", 1.0)
        except (arcpy.ExecuteError, RuntimeError):
            continue
        if off is None:
            continue
        candidates.append(off)
        notes.append(side)
    if not candidates:
        return (line_geom, False, "Offset failed (parallelOffset unavailable / license)")
    best = None
    best_score = None
    best_note = None
    for g, n in zip(candidates, notes):
        try:
            inter = g.intersect(road_buffer_geom, 2)
            score = inter.length if inter else 0.0
        except (arcpy.ExecuteError, RuntimeError):
            score = 1e18
        if best is None or score < best_score:
            best = g
            best_score = score
            best_note = n
    return (best, True, f"WholeOffset chosen: {best_note}")



# =============================================================================
# 15b. Cartographic / display-offset engine + mode-aware writers
# =============================================================================
#
# CONCEPT - DISPLAY/CARTOGRAPHIC DISPLACEMENT vs REAL COORDINATE EDITING
# ---------------------------------------------------------------------
# A symbol-thickened road covers nearby features on the *map*, even though the
# real-world coordinates do not overlap. The correct fix is a *cartographic*
# (display-only) displacement: the drawn symbol is nudged for legibility while
# the stored feature coordinates stay exactly where they are. This is
# fundamentally different from editing the real geometry.
#
# DISPLAY_ONLY_CARTO_OFFSETS (default) therefore NEVER writes SHAPE@ on the
# main output. It records the offset a renderer (CIM/lyrx symbol offset, etc.)
# would apply, in CARTO_* fields, and optionally builds a clearly-labelled
# preview-only geometry layer for visual QA. LEGACY_GEOMETRY_MOVE reproduces
# the old destructive behaviour for backward compatibility only.
# =============================================================================

MODE_DISPLAY_ONLY = "DISPLAY_ONLY_CARTO_OFFSETS"
MODE_PREVIEW_ONLY = "PREVIEW_GEOMETRY_ONLY"
MODE_LEGACY = "LEGACY_GEOMETRY_MOVE"
DECONFLICT_MODES = [MODE_DISPLAY_ONLY, MODE_PREVIEW_ONLY, MODE_LEGACY]

PLAT_PREFIX = "PRO_T02"
PREVIEW_WARNING_TEXT = "CARTOGRAPHIC DISPLAY PREVIEW ONLY - NOT REAL COORDINATES"


def _safe_delete(path):
    if not path:
        return
    try:
        if arcpy.Exists(path):
            arcpy.management.Delete(path)
    except (arcpy.ExecuteError, RuntimeError):
        pass


def _carto_common_specs():
    return [
        ("CARTO_MODE", "TEXT", 40),
        ("CARTO_MOVED", "SHORT", None),
        ("CARTO_DX", "DOUBLE", None),
        ("CARTO_DY", "DOUBLE", None),
        ("CARTO_SHIFT", "DOUBLE", None),
        ("CARTO_AZIMUTH", "DOUBLE", None),
        ("CARTO_NOTE", "TEXT", 255),
        ("CARTO_CONFLICT_BEFORE", "SHORT", None),
        ("CARTO_CONFLICT_AFTER_PREVIEW", "SHORT", None),
        ("CARTO_PREVIEW_FC", "TEXT", 255),
        ("SRC_LAYER", "TEXT", 120),
        ("SRC_OID", "LONG", None),
    ]


def _carto_point_specs():
    return _carto_common_specs() + [("ORIG_X", "DOUBLE", None),
                                    ("ORIG_Y", "DOUBLE", None)]


def _carto_centroid_specs():
    return _carto_common_specs() + [("ORIG_CENTROID_X", "DOUBLE", None),
                                    ("ORIG_CENTROID_Y", "DOUBLE", None)]


def _legacy_point_specs():
    return [("_RDCL_MOV", "SHORT", None), ("_RDCL_SD", "DOUBLE", None),
            ("_RDCL_AZ", "DOUBLE", None), ("_RDCL_NOTE", "TEXT", 255)]


def _legacy_line_specs():
    return [("_RDCL_MOV", "SHORT", None), ("_RDCL_SD", "DOUBLE", None),
            ("_RDCL_NOTE", "TEXT", 255)]


def _geom_conflicts(geom, road_buffer_geom, road_geom, clearance):
    """Real conflict test: does geom touch the symbol-clearance road buffer?"""
    if geom is None:
        return False
    try:
        return (not road_buffer_geom.disjoint(geom))
    except (arcpy.ExecuteError, RuntimeError, AttributeError):
        try:
            return (road_geom.distanceTo(geom) < clearance)
        except (arcpy.ExecuteError, RuntimeError, AttributeError):
            return False


def _t02_unique_name(parts, workspace):
    raw = "_".join([str(p) for p in parts if p not in (None, "")])
    name = _sanitize_name(raw, workspace)
    if arcpy.Exists(os.path.join(workspace, name)):
        try:
            name = arcpy.ValidateTableName(name + "_" + uuid.uuid4().hex[:6], workspace)
        except (arcpy.ExecuteError, RuntimeError):
            name = name + "_" + uuid.uuid4().hex[:6]
    return name


def _imap(fields):
    return {f: i for i, f in enumerate(fields)}


def _orig_xy(geom, kind):
    """Original anchor (point coords, or centroid for line/polygon)."""
    if geom is None:
        return (None, None)
    try:
        if kind == "POINT":
            p = geom.firstPoint
            return (p.X, p.Y)
        c = geom.centroid
        return (c.firstPoint.X, c.firstPoint.Y)
    except (AttributeError, RuntimeError):
        return (None, None)


# ---- Proposal engines: compute a displacement WITHOUT mutating SHAPE@ -------
# Each returns a dict:
#   moved, dx, dy, shift, azimuth (0=N cw or None), note,
#   preview_geom (displaced arcpy.Geometry), conflict_before, conflict_after.
# Pro near_rec layout is (near_x, near_y, near_dist, near_fid).

def _proposal_point(geom, near_rec, use_near, clearance, road_geom,
                    road_buffer_geom, max_shift):
    conflict_before = _geom_conflicts(geom, road_buffer_geom, road_geom, clearance)
    if use_near and near_rec is not None:
        nx, ny, nd, _nf = near_rec
        new_geom, moved, sh, az, note = _push_point_to_clearance_from_near(
            geom, nx, ny, nd, clearance, road_geom=road_geom, max_shift=max_shift)
    else:
        new_geom, moved, sh, az, note = _push_point_to_clearance(
            geom, road_geom, clearance, max_shift=max_shift)
    try:
        dx = new_geom.firstPoint.X - geom.firstPoint.X
        dy = new_geom.firstPoint.Y - geom.firstPoint.Y
    except (AttributeError, RuntimeError):
        dx = dy = 0.0
    conflict_after = _geom_conflicts(new_geom, road_buffer_geom, road_geom, clearance)
    return {"moved": bool(moved), "dx": dx, "dy": dy, "shift": float(sh or 0.0),
            "azimuth": az, "note": str(note), "preview_geom": new_geom,
            "conflict_before": conflict_before, "conflict_after": conflict_after}


def _proposal_line(geom, clearance, road_geom, road_buffer_geom, line_strategy,
                   offset_side, densify_step, preserve_endpoints, smooth_iters,
                   max_shift, max_iter, max_deflection_deg):
    conflict_before = _geom_conflicts(geom, road_buffer_geom, road_geom, clearance)
    moved = False
    still = False
    note = ""
    sd_val = 0.0
    new_geom = geom
    if line_strategy == "WHOLE_OFFSET":
        off_dist = clearance
        if max_shift is not None and max_shift > 0 and max_shift < clearance:
            off_dist = float(max_shift)
        new_geom, moved, note = _whole_offset_best_side(
            geom, road_buffer_geom, off_dist, force_side=offset_side)
        if not moved:
            new_geom, moved, max_v_shift, note2, still = _local_push_polyline(
                geom, road_geom, road_buffer_geom, clearance,
                densify_step=densify_step, preserve_endpoints=preserve_endpoints,
                smooth_iters=smooth_iters, max_shift=max_shift, max_iter=max_iter,
                max_deflection_deg=max_deflection_deg)
            note = note + " | Fallback->LocalPush: " + note2
            sd_val = float(max_v_shift) if moved else 0.0
        else:
            try:
                still = (not road_buffer_geom.disjoint(new_geom))
            except (arcpy.ExecuteError, RuntimeError, AttributeError):
                still = False
            sd_val = float(off_dist) if moved else 0.0
    else:
        new_geom, moved, max_v_shift, note, still = _local_push_polyline(
            geom, road_geom, road_buffer_geom, clearance,
            densify_step=densify_step, preserve_endpoints=preserve_endpoints,
            smooth_iters=smooth_iters, max_shift=max_shift, max_iter=max_iter,
            max_deflection_deg=max_deflection_deg)
        sd_val = float(max_v_shift) if moved else 0.0
    dx = dy = 0.0
    az = None
    try:
        p0 = geom.positionAlongLine(0.5, True).firstPoint
        p1 = new_geom.positionAlongLine(0.5, True).firstPoint
        dx = p1.X - p0.X
        dy = p1.Y - p0.Y
        if abs(dx) > 1e-12 or abs(dy) > 1e-12:
            az = _azimuth_deg(dx, dy)
    except (arcpy.ExecuteError, RuntimeError, AttributeError):
        pass
    conflict_after = bool(still) or _geom_conflicts(new_geom, road_buffer_geom, road_geom, clearance)
    return {"moved": bool(moved), "dx": dx, "dy": dy, "shift": float(sd_val or 0.0),
            "azimuth": az, "note": str(note), "preview_geom": new_geom,
            "conflict_before": conflict_before, "conflict_after": conflict_after}


def _proposal_polygon(geom, near_rec, use_near, clearance, road_geom,
                      road_buffer_geom, max_shift, max_iter):
    conflict_before = _geom_conflicts(geom, road_buffer_geom, road_geom, clearance)
    dist0 = None
    nx = None
    ny = None
    if use_near and near_rec is not None:
        nx, ny, nd, _nf = near_rec
        if nd is not None:
            dist0 = float(nd)
    if dist0 is None:
        try:
            dist0 = road_geom.distanceTo(geom)
        except (arcpy.ExecuteError, RuntimeError, AttributeError):
            dist0 = 0.0
    if dist0 >= clearance:
        return {"moved": False, "dx": 0.0, "dy": 0.0, "shift": 0.0, "azimuth": None,
                "note": "OK (no move)", "preview_geom": geom,
                "conflict_before": conflict_before, "conflict_after": conflict_before}
    cent = geom.centroid
    cx = cent.firstPoint.X
    cy = cent.firstPoint.Y
    dist_along = None
    side = None
    if nx is None or ny is None:
        p_on, dist_along, _df, side = _nearest_point_and_side(road_geom, cent)
        try:
            nx = p_on.firstPoint.X
            ny = p_on.firstPoint.Y
        except (AttributeError, RuntimeError):
            nx = None
            ny = None
    vx = (cx - nx) if nx is not None else 0.0
    vy = (cy - ny) if ny is not None else 0.0
    vd = math.sqrt(vx * vx + vy * vy)
    if vd < 1e-9:
        if dist_along is None:
            _po, dist_along, _df, side = _nearest_point_and_side(road_geom, cent)
        tx, ty = _tangent_at_distance(road_geom, dist_along)
        ux, uy = _unit_normal_from_tangent(tx, ty, side or "LEFT")
    else:
        ux, uy = (vx / vd, vy / vd)
    new_geom, total_shift, still, note = _best_polygon_translation(
        geom, road_geom, clearance, ux, uy, dist0,
        dist_along=dist_along, max_shift=max_shift, max_iter=max_iter, side=side)
    note = str(note)
    if (max_shift is not None and max_shift > 0
            and total_shift >= (float(max_shift) - 1e-9)):
        note = note + " | CAPPED by MaxShift"
    dx = dy = 0.0
    az = None
    try:
        p0 = geom.centroid.firstPoint
        p1 = new_geom.centroid.firstPoint
        dx = p1.X - p0.X
        dy = p1.Y - p0.Y
        if abs(dx) > 1e-12 or abs(dy) > 1e-12:
            az = _azimuth_deg(dx, dy)
    except (arcpy.ExecuteError, RuntimeError, AttributeError):
        pass
    conflict_after = bool(still) or _geom_conflicts(new_geom, road_buffer_geom, road_geom, clearance)
    return {"moved": True, "dx": dx, "dy": dy, "shift": float(total_shift or 0.0),
            "azimuth": az, "note": note, "preview_geom": new_geom,
            "conflict_before": conflict_before, "conflict_after": conflict_after}


# ---- QA self-check: main output geometry must be byte-identical to source ---

def _assert_geometry_unchanged(ref_fc, out_fc, mode):
    """In DISPLAY_ONLY / PREVIEW_ONLY the main output must be geometrically
    identical to its pre-processing copy (ref_fc). Streams both feature classes
    positionally and raises DISPLAY_ONLY_GEOMETRY_CHANGED on any mismatch.
    Legacy mode is exempt (it intentionally moves geometry)."""
    if mode == MODE_LEGACY or not ref_fc:
        return
    rc = arcpy.da.SearchCursor(ref_fc, ["SHAPE@"])
    oc = arcpy.da.SearchCursor(out_fc, ["SHAPE@"])
    try:
        while True:
            try:
                rrow = next(rc)
            except StopIteration:
                rrow = None
            try:
                orow = next(oc)
            except StopIteration:
                orow = None
            if rrow is None and orow is None:
                break
            if rrow is None or orow is None:
                raise arcpy.ExecuteError(
                    f"DISPLAY_ONLY_GEOMETRY_CHANGED: output feature count differs "
                    f"from source in mode {mode}.")
            rg = rrow[0]
            og = orow[0]
            if rg is None and og is None:
                continue
            same = False
            try:
                same = bool(rg.equals(og))
            except (arcpy.ExecuteError, RuntimeError, AttributeError):
                same = (getattr(rg, "JSON", None) == getattr(og, "JSON", None))
            if not same:
                raise arcpy.ExecuteError(
                    f"DISPLAY_ONLY_GEOMETRY_CHANGED: main output geometry differs "
                    f"from source in mode {mode}.")
    finally:
        try:
            del rc
        except (NameError, RuntimeError):
            pass
        try:
            del oc
        except (NameError, RuntimeError):
            pass


# ---- Shared context + global QA feature classes -----------------------------

class _Ctx(object):
    """Lightweight shared context for the per-layer processor."""
    pass


def _make_global_qa_fcs(ctx, create_errors, create_vectors):
    ctx.vec_fc = None
    ctx.conf_before_fc = None
    ctx.conf_after_fc = None
    ctx.err_fc = None
    if create_vectors:
        vname = _t02_unique_name([ctx.plat_prefix, "DisplacementVectors_QA"], ctx.out_gdb)
        ctx.vec_fc = os.path.join(ctx.out_gdb, vname)
        _gp_try(arcpy.management.CreateFeatureclass,
                [ctx.out_gdb, vname, "POLYLINE"], {"spatial_reference": ctx.sr})
        _ensure_fields(ctx.vec_fc, [
            ("SRC_LAYER", "TEXT", 120), ("SRC_OID", "LONG", None),
            ("KIND", "TEXT", 20), ("SHIFT", "DOUBLE", None),
            ("AZIMUTH", "DOUBLE", None), ("CARTO_DX", "DOUBLE", None),
            ("CARTO_DY", "DOUBLE", None)])
    cbname = _t02_unique_name([ctx.plat_prefix, "Conflicts_Before"], ctx.out_gdb)
    ctx.conf_before_fc = os.path.join(ctx.out_gdb, cbname)
    _gp_try(arcpy.management.CreateFeatureclass,
            [ctx.out_gdb, cbname, "POINT"], {"spatial_reference": ctx.sr})
    _ensure_fields(ctx.conf_before_fc, [
        ("SRC_LAYER", "TEXT", 120), ("SRC_OID", "LONG", None),
        ("KIND", "TEXT", 20), ("STAGE", "TEXT", 20)])
    caname = _t02_unique_name([ctx.plat_prefix, "Conflicts_AfterPreview"], ctx.out_gdb)
    ctx.conf_after_fc = os.path.join(ctx.out_gdb, caname)
    _gp_try(arcpy.management.CreateFeatureclass,
            [ctx.out_gdb, caname, "POINT"], {"spatial_reference": ctx.sr})
    _ensure_fields(ctx.conf_after_fc, [
        ("SRC_LAYER", "TEXT", 120), ("SRC_OID", "LONG", None),
        ("KIND", "TEXT", 20), ("STAGE", "TEXT", 20)])
    if create_errors:
        ename = _t02_unique_name([ctx.plat_prefix, "Errors_QA"], ctx.out_gdb)
        ctx.err_fc = os.path.join(ctx.out_gdb, ename)
        _gp_try(arcpy.management.CreateFeatureclass,
                [ctx.out_gdb, ename, "POINT"], {"spatial_reference": ctx.sr})
        _ensure_fields(ctx.err_fc, [
            ("SRC_LAYER", "TEXT", 120), ("SRC_OID", "LONG", None),
            ("KIND", "TEXT", 20), ("ERR_CODE", "TEXT", 60),
            ("DETAIL", "TEXT", 255)])


def _make_report_table(ctx):
    rname = _t02_unique_name([ctx.plat_prefix, "Report"], ctx.out_gdb)
    ctx.report_fc = os.path.join(ctx.out_gdb, rname)
    _gp_try(arcpy.management.CreateTable, [ctx.out_gdb, rname])
    _ensure_fields(ctx.report_fc, [
        ("LAYER", "TEXT", 160), ("KIND", "TEXT", 20), ("MODE", "TEXT", 40),
        ("N_TOTAL", "LONG", None), ("N_CONFLICT_BEFORE", "LONG", None),
        ("N_CARTO_MOVED", "LONG", None), ("N_CONFLICT_AFTER_PREVIEW", "LONG", None),
        ("REAL_GEOMETRY_STATUS", "TEXT", 40), ("PREVIEW_FC", "TEXT", 255)])


# ---- The mode-aware per-layer processor -------------------------------------

def _process_target_layer(ctx, lyr, kind):
    """kind in {'POINT','LINE','POLYGON'}. Computes cartographic display offsets
    and writes mode-appropriate outputs. NEVER mutates the main output SHAPE@
    unless ctx.mode == LEGACY."""
    shape_req = {"POINT": "POINT", "LINE": "POLYLINE", "POLYGON": "POLYGON"}[kind]
    src = _resolve_full_source(lyr)
    desc = arcpy.Describe(src)
    if desc.shapeType.upper() != shape_req:
        _warn(f"Skipping (not {shape_req}): {lyr}")
        return None
    layer_base = os.path.basename(desc.catalogPath)

    main_kind_label = "LegacyGeometryMoved" if ctx.mode == MODE_LEGACY else "CartoOffsets"
    out_name = _t02_unique_name([ctx.plat_prefix, layer_base, main_kind_label], ctx.out_gdb)
    out_fc = os.path.join(ctx.out_gdb, out_name)
    _msg(f"Copy {kind.lower()} -> {out_fc}")
    _copy_or_project(src, out_fc, ctx.sr)
    if kind == "POINT":
        _ensure_fields(out_fc, _legacy_point_specs())
        _ensure_fields(out_fc, _carto_point_specs())
    else:
        _ensure_fields(out_fc, _legacy_line_specs())
        _ensure_fields(out_fc, _carto_centroid_specs())
    total = _get_count(out_fc)
    _diag(f"{kind} '{desc.name}': total={total}")

    snap_fc = None
    if ctx.mode != MODE_LEGACY:
        snap_fc = os.path.join(ctx.scratch_ws, "rdcl_snap_" + uuid.uuid4().hex[:6])
        try:
            _gp_try(arcpy.management.CopyFeatures, [out_fc, snap_fc])
        except (arcpy.ExecuteError, RuntimeError):
            snap_fc = None

    want_preview = (ctx.mode == MODE_PREVIEW_ONLY) or \
                   (ctx.mode == MODE_DISPLAY_ONLY and ctx.create_preview)
    preview_fc = None
    preview_name = ""
    if want_preview:
        preview_name = _t02_unique_name([ctx.plat_prefix, layer_base, "DisplayPreview"], ctx.out_gdb)
        preview_fc = os.path.join(ctx.out_gdb, preview_name)
        _gp_try(arcpy.management.CreateFeatureclass,
                [ctx.out_gdb, preview_name, shape_req], {"spatial_reference": ctx.sr})
        _ensure_fields(preview_fc, [
            ("PREVIEW_ONLY", "SHORT", None), ("PREVIEW_WARNING", "TEXT", 120),
            ("SRC_LAYER", "TEXT", 120), ("SRC_OID", "LONG", None),
            ("CARTO_DX", "DOUBLE", None), ("CARTO_DY", "DOUBLE", None),
            ("CARTO_SHIFT", "DOUBLE", None), ("CARTO_AZIMUTH", "DOUBLE", None),
            ("CARTO_CONFLICT_AFTER_PREVIEW", "SHORT", None)])

    # ---- Pass A: initialise CARTO/legacy fields for ALL features (no SHAPE@) -
    orig_field = ("ORIG_X" if kind == "POINT" else "ORIG_CENTROID_X")
    orig_field_y = ("ORIG_Y" if kind == "POINT" else "ORIG_CENTROID_Y")
    initA = ["OID@", "SHAPE@", "CARTO_MODE", "CARTO_MOVED", "CARTO_DX", "CARTO_DY",
             "CARTO_SHIFT", "CARTO_AZIMUTH", "CARTO_NOTE", "CARTO_CONFLICT_BEFORE",
             "CARTO_CONFLICT_AFTER_PREVIEW", "CARTO_PREVIEW_FC", "SRC_LAYER", "SRC_OID",
             orig_field, orig_field_y, "_RDCL_MOV", "_RDCL_SD", "_RDCL_NOTE"]
    if kind == "POINT":
        initA.append("_RDCL_AZ")
    ia = _imap(initA)
    with arcpy.da.UpdateCursor(out_fc, initA) as cur:
        for row in cur:
            oid = row[0]
            g = row[1]
            ox, oy = _orig_xy(g, kind)
            row[ia["CARTO_MODE"]] = ctx.mode
            row[ia["CARTO_MOVED"]] = 0
            row[ia["CARTO_DX"]] = 0.0
            row[ia["CARTO_DY"]] = 0.0
            row[ia["CARTO_SHIFT"]] = 0.0
            row[ia["CARTO_AZIMUTH"]] = None
            row[ia["CARTO_NOTE"]] = "OK (no conflict)"
            row[ia["CARTO_CONFLICT_BEFORE"]] = 0
            row[ia["CARTO_CONFLICT_AFTER_PREVIEW"]] = 0
            row[ia["CARTO_PREVIEW_FC"]] = preview_name
            row[ia["SRC_LAYER"]] = str(desc.name)
            row[ia["SRC_OID"]] = oid
            row[ia[orig_field]] = ox
            row[ia[orig_field_y]] = oy
            row[ia["_RDCL_MOV"]] = 0
            row[ia["_RDCL_SD"]] = 0.0
            row[ia["_RDCL_NOTE"]] = "OK (no conflict)"
            if kind == "POINT":
                row[ia["_RDCL_AZ"]] = None
            cur.updateRow(row)

    # ---- Candidate subset within the clearance buffer -----------------------
    tmp_lyr = "t02lyr_" + uuid.uuid4().hex[:6]
    _gp_try(arcpy.management.MakeFeatureLayer, [out_fc, tmp_lyr])
    _gp_try(arcpy.management.SelectLayerByLocation, [tmp_lyr, "INTERSECT", ctx.buf_fc])
    cand_count = _get_count(tmp_lyr)
    _diag(f"{kind} '{desc.name}': in clearance buffer={cand_count}")

    near_dict = {}
    if kind in ("POINT", "POLYGON") and ctx.use_near and cand_count > 0:
        try:
            near_dict = _build_near_table(tmp_lyr, ctx.diss_fc, arcpy.env.scratchGDB)
        except (arcpy.ExecuteError, RuntimeError) as ex:
            _warn(f"GenerateNearTable failed for {kind.lower()}; falling back to "
                  f"per-feature geometry queries: {ex}")
            near_dict = {}

    has_lock = bool(ctx.lock_field and arcpy.ListFields(out_fc, ctx.lock_field))
    fields = (["OID@", "SHAPE@"] + ([ctx.lock_field] if has_lock else []) + [
        "CARTO_MOVED", "CARTO_DX", "CARTO_DY", "CARTO_SHIFT", "CARTO_AZIMUTH",
        "CARTO_NOTE", "CARTO_CONFLICT_BEFORE", "CARTO_CONFLICT_AFTER_PREVIEW",
        "_RDCL_MOV", "_RDCL_SD", "_RDCL_NOTE"] +
        (["_RDCL_AZ"] if kind == "POINT" else []))
    fm = _imap(fields)
    i_shape = fm["SHAPE@"]
    i_lock = fm[ctx.lock_field] if has_lock else None

    moved_cnt = 0
    conflict_before_cnt = 0
    conflict_after_cnt = 0
    preview_rows = []
    vector_rows = []
    confb_rows = []
    confa_rows = []
    error_rows = []

    arcpy.SetProgressor("step", f"Display-deconfliction ({kind.lower()}): {desc.name}",
                        0, max(1, cand_count), 1)
    n_seen = 0
    try:
        with arcpy.da.UpdateCursor(tmp_lyr, fields,
                                   sql_clause=(None, "ORDER BY OBJECTID")) as cur:
            for row in cur:
                n_seen += 1
                arcpy.SetProgressorPosition(n_seen)
                oid = row[0]
                geom = row[i_shape]
                if geom is None:
                    error_rows.append((None, str(desc.name), oid, kind,
                                       "GEOM_NULL", "Null geometry"))
                    continue
                ox, oy = _orig_xy(geom, kind)

                if has_lock and row[i_lock] == 0:
                    row[fm["CARTO_MOVED"]] = 0
                    row[fm["CARTO_DX"]] = 0.0
                    row[fm["CARTO_DY"]] = 0.0
                    row[fm["CARTO_SHIFT"]] = 0.0
                    row[fm["CARTO_AZIMUTH"]] = None
                    row[fm["CARTO_NOTE"]] = "LOCKED"
                    row[fm["CARTO_CONFLICT_BEFORE"]] = 1
                    row[fm["CARTO_CONFLICT_AFTER_PREVIEW"]] = 1
                    row[fm["_RDCL_MOV"]] = 0
                    row[fm["_RDCL_SD"]] = 0.0
                    row[fm["_RDCL_NOTE"]] = "LOCKED (0)"
                    if kind == "POINT":
                        row[fm["_RDCL_AZ"]] = None
                    cur.updateRow(row)
                    conflict_before_cnt += 1
                    conflict_after_cnt += 1
                    confb_rows.append((ox, oy, str(desc.name), oid, kind, "BEFORE"))
                    confa_rows.append((ox, oy, str(desc.name), oid, kind, "AFTER_PREVIEW"))
                    ctx.audit(kind, desc.name, oid, False, 0.0, None,
                              "LOCKED", 1, 1, ctx.mode, preview_name)
                    continue

                if kind == "POINT":
                    prop = _proposal_point(geom, near_dict.get(int(oid)), ctx.use_near,
                                           ctx.clearance, ctx.road_geom,
                                           ctx.road_buffer_geom, ctx.max_shift)
                elif kind == "LINE":
                    prop = _proposal_line(geom, ctx.clearance, ctx.road_geom,
                                          ctx.road_buffer_geom, ctx.line_strategy,
                                          ctx.offset_side, ctx.densify_step,
                                          ctx.preserve_endpoints, ctx.smooth_iters,
                                          ctx.max_shift, ctx.max_iter, ctx.max_deflection_deg)
                else:
                    prop = _proposal_polygon(geom, near_dict.get(int(oid)), ctx.use_near,
                                             ctx.clearance, ctx.road_geom,
                                             ctx.road_buffer_geom, ctx.max_shift, ctx.max_iter)

                moved = prop["moved"]
                dx = prop["dx"]
                dy = prop["dy"]
                shift = prop["shift"]
                az = prop["azimuth"]
                note = prop["note"]
                cb = 1 if prop["conflict_before"] else 0
                ca = 1 if prop["conflict_after"] else 0

                row[fm["CARTO_MOVED"]] = 1 if moved else 0
                row[fm["CARTO_DX"]] = dx
                row[fm["CARTO_DY"]] = dy
                row[fm["CARTO_SHIFT"]] = float(shift)
                row[fm["CARTO_AZIMUTH"]] = az if az is not None else None
                row[fm["CARTO_NOTE"]] = note
                row[fm["CARTO_CONFLICT_BEFORE"]] = cb
                row[fm["CARTO_CONFLICT_AFTER_PREVIEW"]] = ca
                row[fm["_RDCL_MOV"]] = 1 if moved else 0
                row[fm["_RDCL_SD"]] = float(shift)
                row[fm["_RDCL_NOTE"]] = note
                if kind == "POINT":
                    row[fm["_RDCL_AZ"]] = az if az is not None else None

                if ctx.mode == MODE_LEGACY and moved and prop["preview_geom"] is not None:
                    row[i_shape] = prop["preview_geom"]

                cur.updateRow(row)

                if cb:
                    conflict_before_cnt += 1
                    confb_rows.append((ox, oy, str(desc.name), oid, kind, "BEFORE"))
                if ca:
                    conflict_after_cnt += 1
                    px = (ox + dx) if (ox is not None) else None
                    py = (oy + dy) if (oy is not None) else None
                    confa_rows.append((px, py, str(desc.name), oid, kind, "AFTER_PREVIEW"))
                if moved:
                    moved_cnt += 1
                    if shift > 0 and ox is not None:
                        vector_rows.append((ox, oy, ox + dx, oy + dy,
                                            str(desc.name), oid, kind,
                                            float(shift), az, dx, dy))
                    if preview_fc is not None and prop["preview_geom"] is not None:
                        preview_rows.append((prop["preview_geom"], str(desc.name),
                                             oid, dx, dy, float(shift), az, ca))
                if ca:
                    error_rows.append((ox, oy, str(desc.name), oid, kind,
                                       "STILL_CONFLICT", "Display offset could not clear road buffer"))
                ctx.audit(kind, desc.name, oid, moved, shift, az, note, cb, ca, ctx.mode, preview_name)
    finally:
        arcpy.ResetProgressor()
    _safe_delete(tmp_lyr)
    tmp_lyr = None

    # ---- Bulk-insert QA rows ------------------------------------------------
    if preview_fc is not None and preview_rows:
        with arcpy.da.InsertCursor(preview_fc, [
                "SHAPE@", "SRC_LAYER", "SRC_OID", "CARTO_DX", "CARTO_DY",
                "CARTO_SHIFT", "CARTO_AZIMUTH", "CARTO_CONFLICT_AFTER_PREVIEW",
                "PREVIEW_ONLY", "PREVIEW_WARNING"]) as ic:
            for (g, slyr, soid, pdx, pdy, psh, paz, pca) in preview_rows:
                ic.insertRow([g, slyr, soid, pdx, pdy, psh,
                              (paz if paz is not None else None), pca,
                              1, PREVIEW_WARNING_TEXT])
    if ctx.vec_fc and vector_rows:
        with arcpy.da.InsertCursor(ctx.vec_fc, [
                "SHAPE@", "SRC_LAYER", "SRC_OID", "KIND", "SHIFT", "AZIMUTH",
                "CARTO_DX", "CARTO_DY"]) as ic:
            for (x0, y0, x1, y1, slyr, soid, k, sh, az, ddx, ddy) in vector_rows:
                try:
                    vg = arcpy.Polyline(arcpy.Array([arcpy.Point(x0, y0),
                                                     arcpy.Point(x1, y1)]), ctx.sr)
                    ic.insertRow([vg, slyr, soid, k, sh,
                                  (az if az is not None else None), ddx, ddy])
                except (arcpy.ExecuteError, RuntimeError):
                    pass
    if ctx.conf_before_fc and confb_rows:
        with arcpy.da.InsertCursor(ctx.conf_before_fc, [
                "SHAPE@", "SRC_LAYER", "SRC_OID", "KIND", "STAGE"]) as ic:
            for (x, y, slyr, soid, k, stg) in confb_rows:
                if x is None or y is None:
                    continue
                ic.insertRow([arcpy.PointGeometry(arcpy.Point(x, y), ctx.sr), slyr, soid, k, stg])
    if ctx.conf_after_fc and confa_rows:
        with arcpy.da.InsertCursor(ctx.conf_after_fc, [
                "SHAPE@", "SRC_LAYER", "SRC_OID", "KIND", "STAGE"]) as ic:
            for (x, y, slyr, soid, k, stg) in confa_rows:
                if x is None or y is None:
                    continue
                ic.insertRow([arcpy.PointGeometry(arcpy.Point(x, y), ctx.sr), slyr, soid, k, stg])
    if ctx.err_fc and error_rows:
        with arcpy.da.InsertCursor(ctx.err_fc, [
                "SHAPE@", "SRC_LAYER", "SRC_OID", "KIND", "ERR_CODE", "DETAIL"]) as ic:
            for (x, y, slyr, soid, k, code, detail) in error_rows:
                geom = None
                if x is not None and y is not None:
                    geom = arcpy.PointGeometry(arcpy.Point(x, y), ctx.sr)
                ic.insertRow([geom, slyr, soid, k, code, detail])

    # ---- QA self-check: geometry must be unchanged in non-legacy modes ------
    try:
        _assert_geometry_unchanged(snap_fc, out_fc, ctx.mode)
    finally:
        _safe_delete(snap_fc)

    real_status = ("MOVED_LEGACY" if ctx.mode == MODE_LEGACY else "UNCHANGED")
    ctx.report_rows.append({
        "layer": str(desc.name), "kind": kind, "mode": ctx.mode,
        "total": int(total), "cb": int(conflict_before_cnt),
        "moved": int(moved_cnt), "ca": int(conflict_after_cnt),
        "real_status": real_status, "preview_fc": preview_name})
    _diag(f"{kind} '{desc.name}': conflict_before={conflict_before_cnt}, "
          f"carto_moved={moved_cnt}, conflict_after_preview={conflict_after_cnt}")
    return out_fc


# =============================================================================
# 16. Toolbox / Tool
# =============================================================================

class Toolbox(object):
    """ArcGIS Pro Python Toolbox container."""

    def __init__(self):
        self.label = "Plugin 2 - Road Deconflict (Pro)"
        self.alias = "plugin2_road_deconflict_pro"
        self.tools = [RoadDeconflictTool]


class RoadDeconflictTool(object):
    """Main GP tool - Master Rules rewrite."""

    def __init__(self):
        self.label = ("Deconflict Roads vs Nearby Features "
                      "(Display/Cartographic Offsets)")
        self.description = (
            "Computes cartographic/display displacement instructions for "
            "road-symbol deconfliction. In the default mode "
            "(DISPLAY_ONLY_CARTO_OFFSETS) it PRESERVES real feature geometry "
            "and writes offset fields (CARTO_*) plus QA outputs. It can "
            "optionally create preview-only displaced geometry layers. Display "
            "displacement is NOT real coordinate editing.\n\n"
            "Deconflict Output Mode:\n"
            " - DISPLAY_ONLY_CARTO_OFFSETS (default, recommended): main output "
            "geometry is never changed; only CARTO_* offset fields are written.\n"
            " - PREVIEW_GEOMETRY_ONLY: also build clearly-labelled preview-only "
            "displaced geometry feature classes for visual QA.\n"
            " - LEGACY_GEOMETRY_MOVE: legacy/destructive behaviour that "
            "physically moves output SHAPE@ (opt-in, NOT recommended).\n\n"
            " - SELECTION-BYPASS hardwired: full datasets always processed.\n"
            " - Near distances computed via GenerateNearTable (one call per "
            "layer), not per-feature.\n"
            " - True curves preserved on preview polylines/polygons."
        )
        self.canRunInBackground = True

    # ---------- Parameters ----------
    def getParameterInfo(self):
        p0 = arcpy.Parameter(displayName="Roads (Polyline) - Barrier",
                             name="in_roads", datatype="GPFeatureLayer",
                             parameterType="Required", direction="Input")
        p0.category = "Inputs"

        p1 = arcpy.Parameter(displayName="Clearance Distance (map units)",
                             name="clearance", datatype="GPDouble",
                             parameterType="Required", direction="Input")
        p1.category = "Inputs"
        p1.value = 6.0

        p2 = arcpy.Parameter(displayName="Point Layers to Display-Deconflict (optional)",
                             name="in_points", datatype="GPFeatureLayer",
                             parameterType="Optional", direction="Input",
                             multiValue=True)
        p2.category = "Inputs"

        p3 = arcpy.Parameter(displayName="Line Layers to Display-Deconflict (optional)",
                             name="in_lines", datatype="GPFeatureLayer",
                             parameterType="Optional", direction="Input",
                             multiValue=True)
        p3.category = "Inputs"

        p4 = arcpy.Parameter(displayName="Polygon Layers to Display-Deconflict (optional)",
                             name="in_polygons", datatype="GPFeatureLayer",
                             parameterType="Optional", direction="Input",
                             multiValue=True)
        p4.category = "Inputs"

        p7 = arcpy.Parameter(displayName="Processing AOI (Polygon) - optional",
                             name="aoi_poly", datatype="GPFeatureLayer",
                             parameterType="Optional", direction="Input")
        p7.category = "Inputs"

        p5 = arcpy.Parameter(displayName="Output Geodatabase",
                             name="out_gdb", datatype="DEWorkspace",
                             parameterType="Required", direction="Input")
        p5.category = "Outputs"

        p6 = arcpy.Parameter(displayName="Output Name Suffix",
                             name="name_suffix", datatype="GPString",
                             parameterType="Optional", direction="Input")
        p6.category = "Outputs"
        p6.value = "_RDCL"

        p8 = arcpy.Parameter(displayName="Line Strategy",
                             name="line_strategy", datatype="GPString",
                             parameterType="Optional", direction="Input")
        p8.category = "Line Options"
        p8.filter.type = "ValueList"
        p8.filter.list = ["LOCAL_PUSH", "WHOLE_OFFSET"]
        p8.value = "LOCAL_PUSH"

        p9 = arcpy.Parameter(displayName="WHOLE_OFFSET Side (only if WHOLE_OFFSET)",
                             name="offset_side", datatype="GPString",
                             parameterType="Optional", direction="Input")
        p9.category = "Line Options"
        p9.filter.type = "ValueList"
        p9.filter.list = ["AUTO", "LEFT", "RIGHT"]
        p9.value = "AUTO"
        p9.enabled = False

        p10 = arcpy.Parameter(displayName="Densify Step for Lines (map units; 0 = no densify)",
                              name="densify_step", datatype="GPDouble",
                              parameterType="Optional", direction="Input")
        p10.category = "Line Options"
        p10.value = 0.0

        p11 = arcpy.Parameter(displayName="Preserve Line Endpoints (recommended)",
                              name="preserve_endpoints", datatype="GPBoolean",
                              parameterType="Optional", direction="Input")
        p11.category = "Line Options"
        p11.value = True

        p12 = arcpy.Parameter(displayName="Smoothing Iterations (Chaikin; 0 = off)",
                              name="smooth_iters", datatype="GPLong",
                              parameterType="Optional", direction="Input")
        p12.category = "Line Options"
        p12.value = 0

        p15 = arcpy.Parameter(displayName="Max Deflection Delta at Line Vertices (degrees; 0 = off)",
                              name="max_deflection_deg", datatype="GPDouble",
                              parameterType="Optional", direction="Input")
        p15.category = "Line Options"
        p15.value = 25.0

        p13 = arcpy.Parameter(displayName="Max Shift (cap movement; 0 = no cap)",
                              name="max_shift", datatype="GPDouble",
                              parameterType="Optional", direction="Input")
        p13.category = "Advanced"
        p13.value = 0.0

        p14 = arcpy.Parameter(displayName="Max Iterations (line relaxation / polygon refinement)",
                              name="max_iter", datatype="GPLong",
                              parameterType="Optional", direction="Input")
        p14.category = "Advanced"
        p14.value = 8

        p16 = arcpy.Parameter(displayName="Use GenerateNearTable for Points/Polygons (recommended)",
                              name="use_near", datatype="GPBoolean",
                              parameterType="Optional", direction="Input")
        p16.category = "Advanced"
        p16.value = True

        p17 = arcpy.Parameter(displayName="Lock Field (optional; value 0 locks feature from moving)",
                              name="lock_field", datatype="GPString",
                              parameterType="Optional", direction="Input")
        p17.category = "Advanced"

        p18 = arcpy.Parameter(displayName="Create Error Feature Classes",
                              name="create_errors", datatype="GPBoolean",
                              parameterType="Optional", direction="Input")
        p18.category = "QC / Reporting"
        p18.value = True

        p19 = arcpy.Parameter(displayName="Create Displacement Vectors (visual QC)",
                              name="create_vectors", datatype="GPBoolean",
                              parameterType="Optional", direction="Input")
        p19.category = "QC / Reporting"
        p19.value = False

        p20 = arcpy.Parameter(displayName="Write CSV Report (in output GDB folder)",
                              name="write_csv", datatype="GPBoolean",
                              parameterType="Optional", direction="Input")
        p20.category = "QC / Reporting"
        p20.value = True

        p23 = arcpy.Parameter(displayName="Add outputs to current map",
                              name="add_to_map", datatype="GPBoolean",
                              parameterType="Optional", direction="Input")
        p23.category = "QC / Reporting"
        p23.value = True

        # ---- NEW (cartographic/display) parameters appended at the end so
        # existing parameter indices are preserved for saved models/history. --
        p_mode = arcpy.Parameter(displayName="Deconflict Output Mode",
                                 name="deconflict_mode", datatype="GPString",
                                 parameterType="Required", direction="Input")
        p_mode.category = "Inputs"
        p_mode.filter.type = "ValueList"
        p_mode.filter.list = [MODE_DISPLAY_ONLY, MODE_PREVIEW_ONLY, MODE_LEGACY]
        p_mode.value = MODE_DISPLAY_ONLY

        p_preview = arcpy.Parameter(
            displayName="Create Preview-Only Displaced Geometry Layers (visual QA)",
            name="create_preview", datatype="GPBoolean",
            parameterType="Optional", direction="Input")
        p_preview.category = "QC / Reporting"
        p_preview.value = False

        return [p0, p1, p2, p3, p4, p5, p6, p7, p8, p9,
                p10, p11, p12, p13, p14, p15, p16, p17,
                p18, p19, p20, p23, p_mode, p_preview]

    def isLicensed(self):
        return True

    # ---------- updateParameters ----------
    def updateParameters(self, parameters):
        line_strategy = parameters[8].valueAsText or "LOCAL_PUSH"
        has_lines = bool(parameters[3].valueAsText)
        has_points = bool(parameters[2].valueAsText)
        has_polys = bool(parameters[4].valueAsText)
        for idx in (8, 9, 10, 11, 12, 15):
            try:
                parameters[idx].enabled = has_lines
            except (AttributeError, IndexError):
                pass
        try:
            parameters[9].enabled = (has_lines and line_strategy == "WHOLE_OFFSET")
            if not parameters[9].enabled:
                parameters[9].value = "AUTO"
        except (AttributeError, IndexError):
            pass
        try:
            parameters[10].enabled = (has_lines and line_strategy == "LOCAL_PUSH")
            parameters[11].enabled = (has_lines and line_strategy == "LOCAL_PUSH")
            parameters[12].enabled = (has_lines and line_strategy == "LOCAL_PUSH")
            parameters[15].enabled = (has_lines and line_strategy == "LOCAL_PUSH")
            if not parameters[10].enabled:
                parameters[10].value = 0.0
            if not parameters[12].enabled:
                parameters[12].value = 0
        except (AttributeError, IndexError):
            pass
        try:
            parameters[16].enabled = (has_points or has_polys)
        except (AttributeError, IndexError):
            pass
        # New: Create-Preview flag only meaningful in DISPLAY_ONLY mode.
        try:
            mode_val = parameters[22].valueAsText or MODE_DISPLAY_ONLY
            parameters[23].enabled = (mode_val == MODE_DISPLAY_ONLY)
            if mode_val == MODE_PREVIEW_ONLY:
                parameters[23].value = True
            elif mode_val == MODE_LEGACY:
                parameters[23].value = False
        except (AttributeError, IndexError):
            pass
        c = _safe_float(parameters[1].value, None)
        if c is not None and c <= 0:
            parameters[1].setErrorMessage("Clearance must be > 0 (map units).")
        ms = _safe_float(parameters[13].value, None)
        if ms is not None and ms < 0:
            parameters[13].setErrorMessage("Max Shift cannot be negative.")
        di = _safe_float(parameters[10].value, None)
        if di is not None and di < 0:
            parameters[10].setErrorMessage("Densify Step cannot be negative.")
        it = _safe_int(parameters[14].value, None)
        if it is not None and it < 0:
            parameters[14].setErrorMessage("Max Iterations cannot be negative.")
        sm = _safe_int(parameters[12].value, None)
        if sm is not None and sm < 0:
            parameters[12].setErrorMessage("Smoothing Iterations cannot be negative.")
        md = _safe_float(parameters[15].value, None)
        if md is not None and md < 0:
            parameters[15].setErrorMessage("Max Deflection cannot be negative.")

    # ---------- updateMessages ----------
    def updateMessages(self, parameters):
        in_roads = parameters[0].valueAsText
        clearance = _safe_float(parameters[1].value, None)
        in_pts = parameters[2].valueAsText
        in_lns = parameters[3].valueAsText
        in_pol = parameters[4].valueAsText
        out_gdb = parameters[5].valueAsText
        lock_field = parameters[17].valueAsText
        max_iter = _safe_int(parameters[14].value, 8) or 8
        if not (in_pts or in_lns or in_pol):
            parameters[2].setWarningMessage(
                "No target layers provided. Add at least one Point/Line/"
                "Polygon layer to move.")
        if clearance is None:
            parameters[1].setErrorMessage("Clearance Distance is required.")
        elif clearance <= 0:
            parameters[1].setErrorMessage("Clearance must be > 0 (map units).")
        if in_roads:
            try:
                d = arcpy.Describe(in_roads)
                if getattr(d, "shapeType", "").upper() != "POLYLINE":
                    parameters[0].setErrorMessage(
                        "Roads input must be a Polyline feature layer.")
                sr = getattr(d, "spatialReference", None)
                if sr and sr.type != "Projected":
                    parameters[0].setErrorMessage(
                        "Roads must be in a PROJECTED coordinate system "
                        "(meters/feet).")
            except (arcpy.ExecuteError, RuntimeError):
                pass
        if out_gdb:
            try:
                d = arcpy.Describe(out_gdb)
                if hasattr(d, "workspaceType"):
                    if str(d.workspaceType).lower() not in ("localdatabase", "file"):
                        parameters[5].setWarningMessage(
                            "Output is not a File GDB. A File GDB is recommended.")
            except (arcpy.ExecuteError, RuntimeError):
                pass
        if in_pol and max_iter < 5:
            parameters[14].setWarningMessage(
                "Polygons selected: consider Max Iterations >= 5 for "
                "complex shapes.")
        if lock_field:
            missing = []
            for mv_txt in (in_pts, in_lns, in_pol):
                if not mv_txt:
                    continue
                for lyr in [t.strip() for t in mv_txt.split(";") if t.strip()]:
                    try:
                        if not arcpy.ListFields(lyr, lock_field):
                            missing.append(os.path.basename(lyr))
                    except (arcpy.ExecuteError, RuntimeError):
                        pass
            if missing:
                parameters[17].setWarningMessage(
                    f"Lock Field not found in: {', '.join(missing[:5])}"
                    f"{'...' if len(missing) > 5 else ''}")

    # ---------- map integration ----------
    def _add_layers_to_active_map(self, fc_paths):
        try:
            aprx = arcpy.mp.ArcGISProject("CURRENT")
        except (arcpy.ExecuteError, RuntimeError) as ex:
            _warn(f"No active ArcGIS Pro project; outputs not added: {ex}")
            return
        m = aprx.activeMap
        if m is None:
            return
        for p in fc_paths:
            try:
                m.addDataFromPath(p)
            except (arcpy.ExecuteError, RuntimeError):
                _warn(f"Could not add {p} to active map.")

    # ---------- execute ----------
    def execute(self, parameters, messages):
        env_snap = _snapshot_env()
        scratch_artifacts: List[str] = []
        try:
            _prime_env()
            self._execute_core(parameters, scratch_artifacts)
        except arcpy.ExecuteError:
            _err(arcpy.GetMessages(2))
            raise
        except RuntimeError as ex:
            _err(f"Runtime error: {ex}")
            _err(traceback.format_exc())
            raise
        finally:
            for path in scratch_artifacts:
                try:
                    if path and arcpy.Exists(path):
                        arcpy.management.Delete(path)
                except (arcpy.ExecuteError, RuntimeError) as ex:
                    _warn(f"Could not delete scratch '{path}': {ex}")
            _restore_env(env_snap)

    # ---------- the actual work, factored out so finally can clean up ----------
    def _execute_core(self, parameters, scratch_artifacts: List[str]):
        in_roads_layer = parameters[0].valueAsText
        clearance = _safe_float(parameters[1].value, None)
        in_points_txt = parameters[2].valueAsText
        in_lines_txt = parameters[3].valueAsText
        in_polys_txt = parameters[4].valueAsText
        out_gdb = parameters[5].valueAsText
        suffix = parameters[6].valueAsText or "_RDCL"
        aoi_lyr = parameters[7].valueAsText
        line_strategy = parameters[8].valueAsText or "LOCAL_PUSH"
        offset_side = parameters[9].valueAsText or "AUTO"
        densify_step = _safe_float(parameters[10].value, 0.0)
        preserve_endpoints = bool(parameters[11].value)
        smooth_iters = _safe_int(parameters[12].value, 0) or 0
        max_shift = _safe_float(parameters[13].value, None)
        if max_shift is not None and max_shift <= 0:
            max_shift = None
        max_iter = _safe_int(parameters[14].value, 8)
        max_deflection_deg = _safe_float(parameters[15].value, None)
        if max_deflection_deg is not None and max_deflection_deg <= 0:
            max_deflection_deg = None
        use_near = bool(parameters[16].value)
        lock_field = parameters[17].valueAsText
        create_errors = bool(parameters[18].value)
        create_vectors = bool(parameters[19].value)
        write_csv = bool(parameters[20].value)
        add_to_map = bool(parameters[21].value)
        mode = parameters[22].valueAsText or MODE_DISPLAY_ONLY
        if mode not in DECONFLICT_MODES:
            mode = MODE_DISPLAY_ONLY
        create_preview = bool(parameters[23].value)

        if clearance is None or clearance <= 0:
            raise arcpy.ExecuteError("Clearance must be > 0")
        if not out_gdb or not arcpy.Exists(out_gdb):
            raise arcpy.ExecuteError("Output Geodatabase does not exist.")

        _announce_selection("Roads", in_roads_layer)
        in_roads = _resolve_full_source(in_roads_layer)
        if _get_count(in_roads) <= 0:
            raise arcpy.ExecuteError("Roads input is empty.")

        ok_proj, sr = _is_projected(in_roads)
        if not ok_proj:
            raise arcpy.ExecuteError(
                "Roads layer must be in a PROJECTED coordinate system "
                "with known linear units.")
        _msg(f"Roads SR: {sr.name}")
        try:
            _msg(f"Linear units: {sr.linearUnitName}")
        except (AttributeError,):
            pass

        _msg(f"Deconflict Output Mode: {mode}")
        if mode == MODE_LEGACY:
            _warn("LEGACY_GEOMETRY_MOVE selected: output SHAPE@ will be physically "
                  "moved. This is legacy/destructive behaviour and is NOT recommended.")
        else:
            _msg("Main output geometry will be PRESERVED; display-only offsets are "
                 "written to CARTO_* fields (cartographic displacement is NOT real "
                 "coordinate editing).")

        def _mv(text):
            if text is None:
                return []
            t = str(text).strip()
            if not t:
                return []
            return [p.strip() for p in t.split(";") if p.strip()]

        point_layers = _mv(in_points_txt)
        line_layers = _mv(in_lines_txt)
        poly_layers = _mv(in_polys_txt)

        for lyr in point_layers:
            _announce_selection("Points", lyr)
        for lyr in line_layers:
            _announce_selection("Lines", lyr)
        for lyr in poly_layers:
            _announce_selection("Polygons", lyr)
        if aoi_lyr:
            _announce_selection("AOI", aoi_lyr)

        all_targets = point_layers + line_layers + poly_layers
        if not all_targets:
            _warn("No point/line/polygon layers provided; nothing to do.")
            return

        scratch_ws = arcpy.env.scratchGDB
        if not scratch_ws or not arcpy.Exists(scratch_ws):
            scratch_ws = arcpy.env.scratchWorkspace
        if not scratch_ws or not arcpy.Exists(scratch_ws):
            raise arcpy.ExecuteError(
                "No scratch GDB available. Set arcpy.env.scratchGDB.")
        _msg(f"Scratch (disk): {scratch_ws}")

        # AOI
        aoi_fc = None
        if aoi_lyr and arcpy.Exists(aoi_lyr):
            aoi_fc = _resolve_full_source(aoi_lyr)
            _msg(f"Using provided AOI for clipping (full dataset): {aoi_fc}")
        else:
            ext = _extent_from_layers(all_targets)
            if ext:
                margin = max(clearance * 5.0, 1.0)
                ext_geom = _extent_polygon_geom(ext, sr, margin)
                try:
                    aoi_fc = _extent_polygon_fc(ext_geom, scratch_ws)
                    scratch_artifacts.append(aoi_fc)
                    _msg(f"Auto AOI from targets extent (margin={margin} map units).")
                except (arcpy.ExecuteError, RuntimeError) as ex:
                    _warn(f"AOI build failed; continuing without AOI: {ex}")
                    aoi_fc = None

        roads_for_work = _clip_roads_if_needed(in_roads, aoi_fc, scratch_ws)
        if roads_for_work != in_roads:
            scratch_artifacts.append(roads_for_work)
        _diag(f"Roads working count: {_get_count(roads_for_work)}")

        _msg("Dissolving roads (workset) ...")
        diss_fc, road_geom = _dissolve_to_single_geom(roads_for_work, scratch_ws)
        scratch_artifacts.append(diss_fc)
        if road_geom is None:
            raise arcpy.ExecuteError("Failed to read dissolved roads geometry.")

        _msg(f"Buffering roads (clearance = {clearance} map units) ...")
        buf_fc = os.path.join(scratch_ws, "rdcl_buf_" + uuid.uuid4().hex[:6])
        _buffer_fc(diss_fc, buf_fc, clearance, force_units="MAP_UNITS")
        scratch_artifacts.append(buf_fc)
        road_buffer_geom = None
        with arcpy.da.SearchCursor(buf_fc, ["SHAPE@"]) as cur:
            for row in cur:
                road_buffer_geom = row[0]
                break
        if road_buffer_geom is None:
            raise arcpy.ExecuteError("Failed to read road buffer geometry.")

        # ---- Shared context for the mode-aware per-layer processor ----
        ctx = _Ctx()
        ctx.out_gdb = out_gdb
        ctx.sr = sr
        ctx.scratch_ws = scratch_ws
        ctx.plat_prefix = PLAT_PREFIX
        ctx.clearance = clearance
        ctx.road_geom = road_geom
        ctx.road_buffer_geom = road_buffer_geom
        ctx.diss_fc = diss_fc
        ctx.buf_fc = buf_fc
        ctx.mode = mode
        ctx.create_preview = create_preview
        ctx.use_near = use_near
        ctx.max_shift = max_shift
        ctx.max_iter = max_iter
        ctx.lock_field = lock_field
        ctx.line_strategy = line_strategy
        ctx.offset_side = offset_side
        ctx.densify_step = densify_step
        ctx.preserve_endpoints = preserve_endpoints
        ctx.smooth_iters = smooth_iters
        ctx.max_deflection_deg = max_deflection_deg
        ctx.report_rows = []
        ctx.audit_rows = []

        def _audit(kind, layer, oid, moved, shift, az, note,
                   cb=0, ca=0, mode_val=mode, preview_name=""):
            ctx.audit_rows.append({
                "kind": kind, "layer": str(layer), "oid": oid,
                "moved": int(1 if moved else 0), "shift": float(shift or 0.0),
                "azimuth": "" if az is None else float(az), "note": str(note),
                "conflict_before": int(cb), "conflict_after_preview": int(ca),
                "mode": str(mode_val), "preview_fc": str(preview_name),
            })
        ctx.audit = _audit

        _make_global_qa_fcs(ctx, create_errors, create_vectors)
        _make_report_table(ctx)

        start_ts = time.time()
        out_point_fcs: List[str] = []
        out_line_fcs: List[str] = []
        out_poly_fcs: List[str] = []

        # =====================================================================
        # POINTS (display-deconfliction)
        # =====================================================================
        if point_layers:
            _msg("---- POINT layers (display-deconfliction) ----")
        for lyr in point_layers:
            try:
                out_fc = _process_target_layer(ctx, lyr, "POINT")
                if out_fc:
                    out_point_fcs.append(out_fc)
                gc.collect()
            except arcpy.ExecuteError as ex:
                if "DISPLAY_ONLY_GEOMETRY_CHANGED" in str(ex):
                    raise
                _err(arcpy.GetMessages(2))
                raise
            except RuntimeError as ex:
                _warn(f"Point layer failed: {lyr} | {ex}")
                _warn(traceback.format_exc())

        # =====================================================================
        # LINES (display-deconfliction)
        # =====================================================================
        if line_layers:
            _msg("---- LINE layers (display-deconfliction) ----")
        for lyr in line_layers:
            try:
                out_fc = _process_target_layer(ctx, lyr, "LINE")
                if out_fc:
                    out_line_fcs.append(out_fc)
                gc.collect()
            except arcpy.ExecuteError as ex:
                if "DISPLAY_ONLY_GEOMETRY_CHANGED" in str(ex):
                    raise
                _err(arcpy.GetMessages(2))
                raise
            except RuntimeError as ex:
                _warn(f"Line layer failed: {lyr} | {ex}")
                _warn(traceback.format_exc())

        # =====================================================================
        # POLYGONS (display-deconfliction)
        # =====================================================================
        if poly_layers:
            _msg("---- POLYGON layers (display-deconfliction) ----")
        for lyr in poly_layers:
            try:
                out_fc = _process_target_layer(ctx, lyr, "POLYGON")
                if out_fc:
                    out_poly_fcs.append(out_fc)
                gc.collect()
            except arcpy.ExecuteError as ex:
                if "DISPLAY_ONLY_GEOMETRY_CHANGED" in str(ex):
                    raise
                _err(arcpy.GetMessages(2))
                raise
            except RuntimeError as ex:
                _warn(f"Polygon layer failed: {lyr} | {ex}")
                _warn(traceback.format_exc())

        # ---- Report table (geodatabase) ----
        try:
            with arcpy.da.InsertCursor(ctx.report_fc, [
                    "LAYER", "KIND", "MODE", "N_TOTAL", "N_CONFLICT_BEFORE",
                    "N_CARTO_MOVED", "N_CONFLICT_AFTER_PREVIEW",
                    "REAL_GEOMETRY_STATUS", "PREVIEW_FC"]) as ic:
                for r in ctx.report_rows:
                    ic.insertRow([r["layer"], r["kind"], r["mode"], r["total"],
                                  r["cb"], r["moved"], r["ca"],
                                  r["real_status"], r["preview_fc"]])
            _msg(f"Report table: {ctx.report_fc}")
        except (arcpy.ExecuteError, RuntimeError) as ex:
            _warn(f"Failed to write report table: {ex}")

        # ---- CSV report (separates REAL-geometry status from CARTO/preview) ----
        if write_csv:
            try:
                out_folder = os.path.dirname(out_gdb)
                ts = time.strftime("%Y%m%d_%H%M%S")
                csv_path = os.path.join(out_folder, f"{PLAT_PREFIX}_Report_{ts}.csv")
                real_status = ("MOVED_LEGACY" if mode == MODE_LEGACY else "UNCHANGED")
                with open(csv_path, "w", newline="", encoding="utf-8") as f:
                    writer = csv.writer(f)
                    writer.writerow(
                        ["kind", "layer", "oid", "mode", "real_geometry_status",
                         "carto_moved", "carto_shift", "carto_azimuth",
                         "conflict_before", "conflict_after_preview",
                         "preview_fc", "note"])
                    for r in ctx.audit_rows:
                        writer.writerow([
                            r.get("kind", ""), r.get("layer", ""), r.get("oid", ""),
                            r.get("mode", ""), real_status,
                            r.get("moved", ""), r.get("shift", ""),
                            r.get("azimuth", ""), r.get("conflict_before", ""),
                            r.get("conflict_after_preview", ""),
                            r.get("preview_fc", ""), r.get("note", ""),
                        ])
                _msg(f"CSV report: {csv_path}")
            except OSError as ex:
                _warn(f"Failed to write CSV report: {ex}")

        if add_to_map:
            paths: List[str] = []
            paths.extend(out_point_fcs)
            paths.extend(out_line_fcs)
            paths.extend(out_poly_fcs)
            if ctx.vec_fc:
                paths.append(ctx.vec_fc)
            if ctx.conf_before_fc:
                paths.append(ctx.conf_before_fc)
            if ctx.conf_after_fc:
                paths.append(ctx.conf_after_fc)
            if ctx.err_fc:
                paths.append(ctx.err_fc)
            if paths:
                self._add_layers_to_active_map(paths)

        elapsed = time.time() - start_ts
        _msg("==== SUMMARY ====")
        _msg(f"Mode: {mode}")
        _msg(f"Point   CartoOffsets outputs: {len(out_point_fcs)}")
        _msg(f"Line    CartoOffsets outputs: {len(out_line_fcs)}")
        _msg(f"Polygon CartoOffsets outputs: {len(out_poly_fcs)}")
        if mode != MODE_LEGACY:
            _msg("Main output geometry preserved (DISPLAY_ONLY_GEOMETRY self-check passed).")
        if ctx.conf_before_fc:
            _msg(f"Conflicts_Before: {ctx.conf_before_fc}")
        if ctx.conf_after_fc:
            _msg(f"Conflicts_AfterPreview: {ctx.conf_after_fc}")
        if ctx.err_fc:
            _msg(f"Errors_QA: {ctx.err_fc}")
        if ctx.vec_fc:
            _msg(f"DisplacementVectors_QA: {ctx.vec_fc}")
        _msg(f"Elapsed: {elapsed:.1f}s")
        _msg("Done.")
