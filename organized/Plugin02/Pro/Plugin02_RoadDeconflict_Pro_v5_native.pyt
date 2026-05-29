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
                      "(Points/Lines/Polygons)")
        self.description = (
            "Moves nearby features away from roads to enforce a clearance "
            "distance.\n\n"
            " - SELECTION-BYPASS hardwired: full datasets always processed.\n"
            " - Heavy intermediates land in scratchGDB; near-tables in "
            "the 'memory' workspace.\n"
            " - Near distances computed via GenerateNearTable (one call "
            "per layer), not per-feature.\n"
            " - True curves preserved on translated polylines/polygons "
            "(JSON shift, no vertex decomposition).\n"
            " - Stage-by-stage [DIAG] logging."
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

        p2 = arcpy.Parameter(displayName="Point Layers to Move (optional)",
                             name="in_points", datatype="GPFeatureLayer",
                             parameterType="Optional", direction="Input",
                             multiValue=True)
        p2.category = "Inputs"

        p3 = arcpy.Parameter(displayName="Line Layers to Move (optional)",
                             name="in_lines", datatype="GPFeatureLayer",
                             parameterType="Optional", direction="Input",
                             multiValue=True)
        p3.category = "Inputs"

        p4 = arcpy.Parameter(displayName="Polygon Layers to Move (optional)",
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

        return [p0, p1, p2, p3, p4, p5, p6, p7, p8, p9,
                p10, p11, p12, p13, p14, p15, p16, p17,
                p18, p19, p20, p23]

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

        # Error / vector / audit setup
        err_pts_fc = err_lns_fc = err_pol_fc = None
        if create_errors:
            err_pts_name = _new_name("RDCL_ErrPoints", suffix, out_gdb)
            err_lns_name = _new_name("RDCL_ErrLines", suffix, out_gdb)
            err_pol_name = _new_name("RDCL_ErrPolys", suffix, out_gdb)
            err_pts_fc = os.path.join(out_gdb, err_pts_name)
            err_lns_fc = os.path.join(out_gdb, err_lns_name)
            err_pol_fc = os.path.join(out_gdb, err_pol_name)
            _gp_try(arcpy.management.CreateFeatureclass,
                    [out_gdb, err_pts_name, "POINT"],
                    {"spatial_reference": sr})
            _gp_try(arcpy.management.CreateFeatureclass,
                    [out_gdb, err_lns_name, "POLYLINE"],
                    {"spatial_reference": sr})
            _gp_try(arcpy.management.CreateFeatureclass,
                    [out_gdb, err_pol_name, "POLYGON"],
                    {"spatial_reference": sr})
            for fc in (err_pts_fc, err_lns_fc, err_pol_fc):
                _ensure_fields(fc, [
                    ("SRC_LAYER", "TEXT", 120),
                    ("SRC_OID", "LONG", None),
                    ("ERR_CODE", "TEXT", 60),
                    ("DETAIL", "TEXT", 255),
                ])

        vec_fc = None
        if create_vectors:
            vec_name = _new_name("RDCL_DisplacementVectors", suffix, out_gdb)
            vec_fc = os.path.join(out_gdb, vec_name)
            _gp_try(arcpy.management.CreateFeatureclass,
                    [out_gdb, vec_name, "POLYLINE"],
                    {"spatial_reference": sr})
            _ensure_fields(vec_fc, [
                ("SRC_LAYER", "TEXT", 120),
                ("SRC_OID", "LONG", None),
                ("SHIFT", "DOUBLE", None),
                ("AZIMUTH", "DOUBLE", None),
                ("KIND", "TEXT", 20),
            ])

        audit_rows: List[dict] = []
        start_ts = time.time()

        def _audit(kind, layer, oid, moved, shift, az, note):
            audit_rows.append({
                "kind": kind,
                "layer": str(layer),
                "oid": oid,
                "moved": int(1 if moved else 0),
                "shift": float(shift or 0.0),
                "azimuth": "" if az is None else float(az),
                "note": str(note),
            })

        out_point_fcs: List[str] = []
        out_line_fcs: List[str] = []
        out_poly_fcs: List[str] = []

        # =====================================================================
        # POINTS
        # =====================================================================
        if point_layers:
            _msg("---- POINT layers ----")
            arcpy.SetProgressor("step", "Point layers...",
                                0, max(1, len(point_layers)), 1)
        for lyr_idx, lyr in enumerate(point_layers, start=1):
            arcpy.SetProgressorPosition(lyr_idx)
            try:
                self._process_point_layer(
                    lyr, sr, suffix, out_gdb, road_geom, road_buffer_geom,
                    diss_fc, buf_fc, clearance, max_shift, use_near,
                    lock_field, create_errors, err_pts_fc, vec_fc,
                    out_point_fcs, _audit,
                )
            except arcpy.ExecuteError:
                _err(arcpy.GetMessages(2))
                raise
            except RuntimeError as ex:
                _warn(f"Point layer failed: {lyr} | {ex}")
                _warn(traceback.format_exc())
        if point_layers:
            arcpy.ResetProgressor()

        # =====================================================================
        # LINES
        # =====================================================================
        if line_layers:
            _msg("---- LINE layers ----")
            arcpy.SetProgressor("step", "Line layers...",
                                0, max(1, len(line_layers)), 1)
        for lyr_idx, lyr in enumerate(line_layers, start=1):
            arcpy.SetProgressorPosition(lyr_idx)
            try:
                self._process_line_layer(
                    lyr, sr, suffix, out_gdb, road_geom, road_buffer_geom,
                    buf_fc, clearance, max_shift, max_iter, line_strategy,
                    offset_side, densify_step, preserve_endpoints,
                    smooth_iters, max_deflection_deg, lock_field,
                    create_errors, err_lns_fc, vec_fc, out_line_fcs, _audit,
                )
            except arcpy.ExecuteError:
                _err(arcpy.GetMessages(2))
                raise
            except RuntimeError as ex:
                _warn(f"Line layer failed: {lyr} | {ex}")
                _warn(traceback.format_exc())
        if line_layers:
            arcpy.ResetProgressor()

        # =====================================================================
        # POLYGONS
        # =====================================================================
        if poly_layers:
            _msg("---- POLYGON layers ----")
            arcpy.SetProgressor("step", "Polygon layers...",
                                0, max(1, len(poly_layers)), 1)
        for lyr_idx, lyr in enumerate(poly_layers, start=1):
            arcpy.SetProgressorPosition(lyr_idx)
            try:
                self._process_poly_layer(
                    lyr, sr, suffix, out_gdb, road_geom, road_buffer_geom,
                    diss_fc, buf_fc, clearance, max_shift, max_iter,
                    use_near, lock_field, create_errors, err_pol_fc,
                    vec_fc, out_poly_fcs, _audit,
                )
            except arcpy.ExecuteError:
                _err(arcpy.GetMessages(2))
                raise
            except RuntimeError as ex:
                _warn(f"Polygon layer failed: {lyr} | {ex}")
                _warn(traceback.format_exc())
        if poly_layers:
            arcpy.ResetProgressor()

        # CSV report
        if write_csv:
            try:
                out_folder = os.path.dirname(out_gdb)
                ts = time.strftime("%Y%m%d_%H%M%S")
                csv_path = os.path.join(out_folder, f"RDCL_Report_{ts}.csv")
                with open(csv_path, "w", newline="", encoding="utf-8") as f:
                    writer = csv.writer(f)
                    writer.writerow(
                        ["kind", "layer", "oid", "moved", "shift",
                         "azimuth", "note"])
                    for r in audit_rows:
                        writer.writerow([
                            r.get("kind", ""),
                            r.get("layer", ""),
                            r.get("oid", ""),
                            r.get("moved", ""),
                            r.get("shift", ""),
                            r.get("azimuth", ""),
                            r.get("note", ""),
                        ])
                _msg(f"CSV report: {csv_path}")
            except OSError as ex:
                _warn(f"Failed to write CSV report: {ex}")

        if add_to_map:
            paths: List[str] = []
            paths.extend(out_point_fcs)
            paths.extend(out_line_fcs)
            paths.extend(out_poly_fcs)
            if vec_fc:
                paths.append(vec_fc)
            if err_pts_fc:
                paths.extend([err_pts_fc, err_lns_fc, err_pol_fc])
            if paths:
                self._add_layers_to_active_map(paths)

        elapsed = time.time() - start_ts
        _msg("==== SUMMARY ====")
        _msg(f"Points outputs: {len(out_point_fcs)}")
        _msg(f"Lines outputs : {len(out_line_fcs)}")
        _msg(f"Polys outputs : {len(out_poly_fcs)}")
        if create_errors:
            _msg(f"Error FCs: {err_pts_fc}, {err_lns_fc}, {err_pol_fc}")
        if vec_fc:
            _msg(f"Vectors FC: {vec_fc}")
        _msg(f"Elapsed: {elapsed:.1f}s")
        _msg("Done.")



    # =========================================================================
    # Per-layer processors
    # =========================================================================

    def _process_point_layer(self, lyr, sr, suffix, out_gdb, road_geom,
                             road_buffer_geom, diss_fc, buf_fc, clearance,
                             max_shift, use_near, lock_field, create_errors,
                             err_pts_fc, vec_fc, out_point_fcs, _audit):
        src = _resolve_full_source(lyr)
        desc = arcpy.Describe(src)
        if desc.shapeType.upper() != "POINT":
            _warn(f"Skipping (not POINT): {lyr}")
            return
        base = os.path.basename(desc.catalogPath)
        out_name = _new_name(base, suffix, out_gdb)
        out_fc = os.path.join(out_gdb, out_name)
        _msg(f"Copy points -> {out_fc}")
        _copy_or_project(src, out_fc, sr)
        _ensure_fields(out_fc, [
            ("_RDCL_MOV", "SHORT", None),
            ("_RDCL_SD", "DOUBLE", None),
            ("_RDCL_AZ", "DOUBLE", None),
            ("_RDCL_NOTE", "TEXT", 255),
        ])
        total = _get_count(out_fc)
        _diag(f"POINTS '{desc.name}': total={total}")

        # Filter to features actually inside the clearance buffer.
        # MakeFeatureLayer + SelectLayerByLocation gives a view we can
        # iterate over. Master Rule 6: clean up in finally.
        tmp_lyr = "ptlyr_" + uuid.uuid4().hex[:6]
        try:
            _gp_try(arcpy.management.MakeFeatureLayer, [out_fc, tmp_lyr])
            _gp_try(arcpy.management.SelectLayerByLocation,
                    [tmp_lyr, "INTERSECT", buf_fc])
            cand_count = _get_count(tmp_lyr)
            _diag(f"POINTS '{desc.name}': in clearance buffer={cand_count}")

            # F1: ONE GenerateNearTable call for the whole layer.
            near_dict: Dict[int, Tuple[float, float, float, int]] = {}
            if use_near and cand_count > 0:
                try:
                    near_dict = _build_near_table(tmp_lyr, diss_fc,
                                                  arcpy.env.scratchGDB)
                    _diag(f"POINTS '{desc.name}': near table rows={len(near_dict)}")
                except (arcpy.ExecuteError, RuntimeError) as ex:
                    _warn(f"GenerateNearTable failed for points; "
                          f"falling back to per-feature distanceTo: {ex}")
                    near_dict = {}

            has_lock = bool(lock_field
                            and arcpy.ListFields(out_fc, lock_field))

            fields = (["OID@", "SHAPE@"]
                      + ([lock_field] if has_lock else [])
                      + ["_RDCL_MOV", "_RDCL_SD", "_RDCL_AZ", "_RDCL_NOTE"])
            idx_shape = fields.index("SHAPE@")
            idx_mov = fields.index("_RDCL_MOV")
            idx_sd = fields.index("_RDCL_SD")
            idx_az = fields.index("_RDCL_AZ")
            idx_note = fields.index("_RDCL_NOTE")
            idx_lock = fields.index(lock_field) if has_lock else None

            moved_cnt = 0
            err_cnt = 0
            arcpy.SetProgressor("step",
                                f"POINTS '{desc.name}' - moving features...",
                                0, max(1, cand_count), 1)
            n_seen = 0
            with arcpy.da.UpdateCursor(
                    tmp_lyr, fields,
                    sql_clause=(None, "ORDER BY OBJECTID")) as cur:
                for row in cur:
                    n_seen += 1
                    arcpy.SetProgressorPosition(n_seen)
                    oid = row[0]
                    geom = row[idx_shape]
                    if geom is None:
                        if create_errors and err_pts_fc:
                            with arcpy.da.InsertCursor(
                                    err_pts_fc,
                                    ["SHAPE@", "SRC_LAYER", "SRC_OID",
                                     "ERR_CODE", "DETAIL"]) as ic:
                                ic.insertRow([None, str(desc.name), oid,
                                              "GEOM_NULL", "Null geometry"])
                        continue
                    if has_lock and row[idx_lock] == 0:
                        row[idx_mov] = 0
                        row[idx_sd] = 0.0
                        row[idx_az] = None
                        row[idx_note] = "LOCKED (0)"
                        cur.updateRow(row)
                        _audit("POINT", desc.name, oid, False, 0.0,
                               None, "LOCKED (0)")
                        continue
                    old_geom = geom
                    near_row = near_dict.get(int(oid)) if near_dict else None
                    if near_row is not None:
                        nx, ny, nd, _nf = near_row
                        new_geom, moved, sh, az, note = \
                            _push_point_to_clearance_from_near(
                                geom, nx, ny, nd, clearance,
                                road_geom=road_geom, max_shift=max_shift)
                    else:
                        new_geom, moved, sh, az, note = \
                            _push_point_to_clearance(
                                geom, road_geom, clearance,
                                max_shift=max_shift)
                    try:
                        still = (road_buffer_geom.contains(new_geom)
                                 or road_geom.distanceTo(new_geom) < clearance)
                    except (arcpy.ExecuteError, RuntimeError):
                        still = False
                    row[idx_shape] = new_geom
                    row[idx_mov] = 1 if moved else 0
                    row[idx_sd] = float(sh)
                    row[idx_az] = az if az is not None else None
                    row[idx_note] = note + (" | STILL_CONFLICT"
                                            if still else "")
                    cur.updateRow(row)
                    if moved:
                        moved_cnt += 1
                        _audit("POINT", desc.name, oid, True, sh, az, note)
                        if vec_fc:
                            try:
                                arr = arcpy.Array([old_geom.firstPoint,
                                                   new_geom.firstPoint])
                                vgeom = arcpy.Polyline(arr, sr)
                                with arcpy.da.InsertCursor(
                                        vec_fc,
                                        ["SHAPE@", "SRC_LAYER", "SRC_OID",
                                         "SHIFT", "AZIMUTH", "KIND"]) as ic:
                                    ic.insertRow([
                                        vgeom, str(desc.name), oid,
                                        float(sh),
                                        float(az) if az is not None else 0.0,
                                        "POINT"])
                            except (arcpy.ExecuteError, RuntimeError) as ex:
                                _warn(f"Vector write failed at OID {oid}: {ex}")
                    else:
                        _audit("POINT", desc.name, oid, False, 0.0, None, note)
                    if still:
                        err_cnt += 1
                        if create_errors and err_pts_fc:
                            with arcpy.da.InsertCursor(
                                    err_pts_fc,
                                    ["SHAPE@", "SRC_LAYER", "SRC_OID",
                                     "ERR_CODE", "DETAIL"]) as ic:
                                ic.insertRow([
                                    old_geom, str(desc.name), oid,
                                    "STILL_CONFLICT",
                                    "Could not clear to distance"])
            arcpy.ResetProgressor()
            out_point_fcs.append(out_fc)
            _diag(f"POINTS '{desc.name}': moved_OK={moved_cnt}, "
                  f"still_conflict={err_cnt}")
            gc.collect()
        finally:
            try:
                if arcpy.Exists(tmp_lyr):
                    arcpy.management.Delete(tmp_lyr)
            except (arcpy.ExecuteError, RuntimeError) as ex:
                _warn(f"Could not delete temp layer '{tmp_lyr}': {ex}")

    # -------------------------------------------------------------------------
    def _process_line_layer(self, lyr, sr, suffix, out_gdb, road_geom,
                            road_buffer_geom, buf_fc, clearance, max_shift,
                            max_iter, line_strategy, offset_side,
                            densify_step, preserve_endpoints, smooth_iters,
                            max_deflection_deg, lock_field, create_errors,
                            err_lns_fc, vec_fc, out_line_fcs, _audit):
        src = _resolve_full_source(lyr)
        desc = arcpy.Describe(src)
        if desc.shapeType.upper() != "POLYLINE":
            _warn(f"Skipping (not POLYLINE): {lyr}")
            return
        base = os.path.basename(desc.catalogPath)
        out_name = _new_name(base, suffix, out_gdb)
        out_fc = os.path.join(out_gdb, out_name)
        _msg(f"Copy lines -> {out_fc}")
        _copy_or_project(src, out_fc, sr)
        _ensure_fields(out_fc, [
            ("_RDCL_MOV", "SHORT", None),
            ("_RDCL_SD", "DOUBLE", None),
            ("_RDCL_NOTE", "TEXT", 255),
        ])
        total = _get_count(out_fc)
        _diag(f"LINES '{desc.name}': total={total}")

        tmp_lyr = "lnlyr_" + uuid.uuid4().hex[:6]
        try:
            _gp_try(arcpy.management.MakeFeatureLayer, [out_fc, tmp_lyr])
            _gp_try(arcpy.management.SelectLayerByLocation,
                    [tmp_lyr, "INTERSECT", buf_fc])
            cand_count = _get_count(tmp_lyr)
            _diag(f"LINES '{desc.name}': in clearance buffer={cand_count}")

            has_lock = bool(lock_field
                            and arcpy.ListFields(out_fc, lock_field))
            fields = (["OID@", "SHAPE@"]
                      + ([lock_field] if has_lock else [])
                      + ["_RDCL_MOV", "_RDCL_SD", "_RDCL_NOTE"])
            idx_shape = fields.index("SHAPE@")
            idx_mov = fields.index("_RDCL_MOV")
            idx_sd = fields.index("_RDCL_SD")
            idx_note = fields.index("_RDCL_NOTE")
            idx_lock = fields.index(lock_field) if has_lock else None

            moved_cnt = 0
            err_cnt = 0
            arcpy.SetProgressor("step",
                                f"LINES '{desc.name}' - moving features...",
                                0, max(1, cand_count), 1)
            n_seen = 0
            with arcpy.da.UpdateCursor(
                    tmp_lyr, fields,
                    sql_clause=(None, "ORDER BY OBJECTID")) as cur:
                for row in cur:
                    n_seen += 1
                    arcpy.SetProgressorPosition(n_seen)
                    oid = row[0]
                    geom = row[idx_shape]
                    if geom is None:
                        if create_errors and err_lns_fc:
                            with arcpy.da.InsertCursor(
                                    err_lns_fc,
                                    ["SHAPE@", "SRC_LAYER", "SRC_OID",
                                     "ERR_CODE", "DETAIL"]) as ic:
                                ic.insertRow([None, str(desc.name), oid,
                                              "GEOM_NULL", "Null geometry"])
                        continue
                    if has_lock and row[idx_lock] == 0:
                        row[idx_mov] = 0
                        row[idx_sd] = 0.0
                        row[idx_note] = "LOCKED (0)"
                        cur.updateRow(row)
                        _audit("LINE", desc.name, oid, False, 0.0,
                               None, "LOCKED (0)")
                        continue
                    old_geom = geom
                    moved = False
                    still = False
                    note = ""
                    sd_val = 0.0
                    new_geom = geom
                    if line_strategy == "WHOLE_OFFSET":
                        off_dist = clearance
                        if (max_shift is not None
                                and max_shift > 0
                                and max_shift < clearance):
                            off_dist = float(max_shift)
                        new_geom, moved, note = _whole_offset_best_side(
                            geom, road_buffer_geom, off_dist,
                            force_side=offset_side)
                        if not moved:
                            new_geom, moved, max_v_shift, note2, still = \
                                _local_push_polyline(
                                    geom, road_geom, road_buffer_geom,
                                    clearance, densify_step=densify_step,
                                    preserve_endpoints=preserve_endpoints,
                                    smooth_iters=smooth_iters,
                                    max_shift=max_shift, max_iter=max_iter,
                                    max_deflection_deg=max_deflection_deg)
                            note = note + " | Fallback->LocalPush: " + note2
                            sd_val = float(max_v_shift) if moved else 0.0
                        else:
                            try:
                                still = (not road_buffer_geom.disjoint(new_geom))
                            except (arcpy.ExecuteError, RuntimeError):
                                still = False
                            sd_val = float(off_dist) if moved else 0.0
                    else:
                        new_geom, moved, max_v_shift, note, still = \
                            _local_push_polyline(
                                geom, road_geom, road_buffer_geom, clearance,
                                densify_step=densify_step,
                                preserve_endpoints=preserve_endpoints,
                                smooth_iters=smooth_iters,
                                max_shift=max_shift, max_iter=max_iter,
                                max_deflection_deg=max_deflection_deg)
                        sd_val = float(max_v_shift) if moved else 0.0
                    row[idx_shape] = new_geom
                    row[idx_mov] = 1 if moved else 0
                    row[idx_sd] = sd_val
                    row[idx_note] = (str(note)
                                     + (" | STILL_CONFLICT" if still else ""))
                    cur.updateRow(row)
                    if moved:
                        moved_cnt += 1
                        _audit("LINE", desc.name, oid, True, sd_val,
                               None, note)
                        if vec_fc:
                            try:
                                p0 = old_geom.positionAlongLine(
                                    0.5, True).firstPoint
                                p1 = new_geom.positionAlongLine(
                                    0.5, True).firstPoint
                                dx = p1.X - p0.X
                                dy = p1.Y - p0.Y
                                sh = math.sqrt(dx * dx + dy * dy)
                                az = _azimuth_deg(dx, dy)
                                arr = arcpy.Array([p0, p1])
                                vgeom = arcpy.Polyline(arr, sr)
                                with arcpy.da.InsertCursor(
                                        vec_fc,
                                        ["SHAPE@", "SRC_LAYER", "SRC_OID",
                                         "SHIFT", "AZIMUTH", "KIND"]) as ic:
                                    ic.insertRow([
                                        vgeom, str(desc.name), oid,
                                        float(sh), float(az), "LINE"])
                            except (arcpy.ExecuteError, RuntimeError) as ex:
                                _warn(f"Vector write failed at OID {oid}: {ex}")
                    else:
                        _audit("LINE", desc.name, oid, False, 0.0, None, note)
                    if still:
                        err_cnt += 1
                        if create_errors and err_lns_fc:
                            with arcpy.da.InsertCursor(
                                    err_lns_fc,
                                    ["SHAPE@", "SRC_LAYER", "SRC_OID",
                                     "ERR_CODE", "DETAIL"]) as ic:
                                ic.insertRow([
                                    old_geom, str(desc.name), oid,
                                    "STILL_CONFLICT",
                                    "Could not clear to distance"])
            arcpy.ResetProgressor()
            out_line_fcs.append(out_fc)
            _diag(f"LINES '{desc.name}': moved_OK={moved_cnt}, "
                  f"still_conflict={err_cnt}")
            gc.collect()
        finally:
            try:
                if arcpy.Exists(tmp_lyr):
                    arcpy.management.Delete(tmp_lyr)
            except (arcpy.ExecuteError, RuntimeError) as ex:
                _warn(f"Could not delete temp layer '{tmp_lyr}': {ex}")

    # -------------------------------------------------------------------------
    def _process_poly_layer(self, lyr, sr, suffix, out_gdb, road_geom,
                            road_buffer_geom, diss_fc, buf_fc, clearance,
                            max_shift, max_iter, use_near, lock_field,
                            create_errors, err_pol_fc, vec_fc,
                            out_poly_fcs, _audit):
        src = _resolve_full_source(lyr)
        desc = arcpy.Describe(src)
        if desc.shapeType.upper() != "POLYGON":
            _warn(f"Skipping (not POLYGON): {lyr}")
            return
        base = os.path.basename(desc.catalogPath)
        out_name = _new_name(base, suffix, out_gdb)
        out_fc = os.path.join(out_gdb, out_name)
        _msg(f"Copy polygons -> {out_fc}")
        _copy_or_project(src, out_fc, sr)
        _ensure_fields(out_fc, [
            ("_RDCL_MOV", "SHORT", None),
            ("_RDCL_SD", "DOUBLE", None),
            ("_RDCL_NOTE", "TEXT", 255),
        ])
        total = _get_count(out_fc)
        _diag(f"POLYGONS '{desc.name}': total={total}")

        tmp_lyr = "polylr_" + uuid.uuid4().hex[:6]
        try:
            _gp_try(arcpy.management.MakeFeatureLayer, [out_fc, tmp_lyr])
            _gp_try(arcpy.management.SelectLayerByLocation,
                    [tmp_lyr, "INTERSECT", buf_fc])
            cand_count = _get_count(tmp_lyr)
            _diag(f"POLYGONS '{desc.name}': in clearance buffer={cand_count}")

            # F1: ONE GenerateNearTable call for the whole layer.
            near_dict: Dict[int, Tuple[float, float, float, int]] = {}
            if use_near and cand_count > 0:
                try:
                    near_dict = _build_near_table(tmp_lyr, diss_fc,
                                                  arcpy.env.scratchGDB)
                    _diag(f"POLYGONS '{desc.name}': near table rows={len(near_dict)}")
                except (arcpy.ExecuteError, RuntimeError) as ex:
                    _warn(f"GenerateNearTable failed for polygons; "
                          f"falling back to centroid direction: {ex}")
                    near_dict = {}

            has_lock = bool(lock_field
                            and arcpy.ListFields(out_fc, lock_field))
            fields = (["OID@", "SHAPE@"]
                      + ([lock_field] if has_lock else [])
                      + ["_RDCL_MOV", "_RDCL_SD", "_RDCL_NOTE"])
            idx_shape = fields.index("SHAPE@")
            idx_mov = fields.index("_RDCL_MOV")
            idx_sd = fields.index("_RDCL_SD")
            idx_note = fields.index("_RDCL_NOTE")
            idx_lock = fields.index(lock_field) if has_lock else None

            moved_cnt = 0
            err_cnt = 0
            arcpy.SetProgressor("step",
                                f"POLYGONS '{desc.name}' - moving features...",
                                0, max(1, cand_count), 1)
            n_seen = 0
            with arcpy.da.UpdateCursor(
                    tmp_lyr, fields,
                    sql_clause=(None, "ORDER BY OBJECTID")) as cur:
                for row in cur:
                    n_seen += 1
                    arcpy.SetProgressorPosition(n_seen)
                    oid = row[0]
                    geom = row[idx_shape]
                    if geom is None:
                        if create_errors and err_pol_fc:
                            with arcpy.da.InsertCursor(
                                    err_pol_fc,
                                    ["SHAPE@", "SRC_LAYER", "SRC_OID",
                                     "ERR_CODE", "DETAIL"]) as ic:
                                ic.insertRow([None, str(desc.name), oid,
                                              "GEOM_NULL", "Null geometry"])
                        continue
                    if has_lock and row[idx_lock] == 0:
                        row[idx_mov] = 0
                        row[idx_sd] = 0.0
                        row[idx_note] = "LOCKED (0)"
                        cur.updateRow(row)
                        _audit("POLYGON", desc.name, oid, False, 0.0,
                               None, "LOCKED (0)")
                        continue
                    old_geom = geom
                    near_row = near_dict.get(int(oid)) if near_dict else None
                    if near_row is not None:
                        try:
                            dist0 = float(near_row[2])
                        except (TypeError, ValueError):
                            dist0 = None
                    else:
                        dist0 = None
                    if dist0 is None:
                        try:
                            dist0 = road_geom.distanceTo(geom)
                        except (arcpy.ExecuteError, RuntimeError):
                            dist0 = 0.0
                    if dist0 >= clearance:
                        row[idx_mov] = 0
                        row[idx_sd] = 0.0
                        row[idx_note] = "OK (no move)"
                        cur.updateRow(row)
                        _audit("POLYGON", desc.name, oid, False, 0.0,
                               None, "OK (no move)")
                        continue
                    cent = geom.centroid
                    cx = cent.firstPoint.X
                    cy = cent.firstPoint.Y
                    p_on, dist_along, dist_from, side = _nearest_point_and_side(
                        road_geom, cent)
                    nx = p_on.firstPoint.X
                    ny = p_on.firstPoint.Y
                    vx = cx - nx
                    vy = cy - ny
                    vd = math.sqrt(vx * vx + vy * vy)
                    if vd < 1e-9:
                        tx, ty = _tangent_at_distance(road_geom, dist_along)
                        ux, uy = _unit_normal_from_tangent(tx, ty,
                                                           side or "LEFT")
                    else:
                        ux, uy = (vx / vd, vy / vd)
                    new_geom, total_shift, still, note = \
                        _best_polygon_translation(
                            geom, road_geom, clearance, ux, uy, dist0,
                            dist_along=dist_along, max_shift=max_shift,
                            max_iter=max_iter, side=side)
                    note = str(note)
                    if (max_shift is not None and max_shift > 0
                            and total_shift >= (float(max_shift) - 1e-9)):
                        note += " | CAPPED by MaxShift"
                    row[idx_shape] = new_geom
                    row[idx_mov] = 1
                    row[idx_sd] = float(total_shift)
                    row[idx_note] = note + (" | STILL_CONFLICT"
                                            if still else "")
                    cur.updateRow(row)
                    moved_cnt += 1
                    _audit("POLYGON", desc.name, oid, True, total_shift,
                           None, note)
                    if vec_fc:
                        try:
                            p0 = old_geom.centroid.firstPoint
                            p1 = new_geom.centroid.firstPoint
                            dx = p1.X - p0.X
                            dy = p1.Y - p0.Y
                            sh = math.sqrt(dx * dx + dy * dy)
                            az = _azimuth_deg(dx, dy)
                            arr = arcpy.Array([p0, p1])
                            vgeom = arcpy.Polyline(arr, sr)
                            with arcpy.da.InsertCursor(
                                    vec_fc,
                                    ["SHAPE@", "SRC_LAYER", "SRC_OID",
                                     "SHIFT", "AZIMUTH", "KIND"]) as ic:
                                ic.insertRow([
                                    vgeom, str(desc.name), oid,
                                    float(sh), float(az), "POLYGON"])
                        except (arcpy.ExecuteError, RuntimeError) as ex:
                            _warn(f"Vector write failed at OID {oid}: {ex}")
                    if still:
                        err_cnt += 1
                        if create_errors and err_pol_fc:
                            with arcpy.da.InsertCursor(
                                    err_pol_fc,
                                    ["SHAPE@", "SRC_LAYER", "SRC_OID",
                                     "ERR_CODE", "DETAIL"]) as ic:
                                ic.insertRow([
                                    old_geom, str(desc.name), oid,
                                    "STILL_CONFLICT",
                                    "Could not clear to distance"])
            arcpy.ResetProgressor()
            out_poly_fcs.append(out_fc)
            _diag(f"POLYGONS '{desc.name}': moved_OK={moved_cnt}, "
                  f"still_conflict={err_cnt}")
            gc.collect()
        finally:
            try:
                if arcpy.Exists(tmp_lyr):
                    arcpy.management.Delete(tmp_lyr)
            except (arcpy.ExecuteError, RuntimeError) as ex:
                _warn(f"Could not delete temp layer '{tmp_lyr}': {ex}")
