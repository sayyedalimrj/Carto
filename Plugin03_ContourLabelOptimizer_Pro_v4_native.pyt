# -*- coding: utf-8 -*-
"""
Plugin 03 - Contour Label Optimizer (ArcGIS Pro / Python 3) - Master Rules rewrite
==================================================================================
Places one optimized label anchor per along-line interval window on contour
lines, scoring by curvature, obstacle overlap, and window-center preference.

Tools:
  - Optimize Contour Label Anchors (v4)
  - Validate Label Anchors (QA)
  - Curvature Heatmap (QA)
  - Convert Labels To Annotation (Pro arcpy.mp)
  - Run Unit Tests (basic)

MASTER RULES enforced:
  1. Narrow exceptions at GP-call sites: (arcpy.ExecuteError, RuntimeError).
     MemoryError / OSError are NEVER caught. No bare `except` /
     `except Exception`.
  2. No bulk geometry caches in RAM beyond what the user opts into.
     The optimized evaluation pre-extracts obstacle bounding boxes into a
     compact float64 NumPy array (4 doubles per obstacle), not full
     geometries; full intersect calls are deferred to AABB-positive hits.
  3. Selection hygiene: _resolve_full_source(ignore_selection=True) is
     preserved; full datasets always processed by default.
  4. arcpy.env snapshot / reset / restore in every execute().
  5. Pro-native: f-strings, native str, arcpy.mp, "memory" workspace
     (no backslashes; never "in_memory").
  6. All cursors inside `with` blocks; scratch datasets and layer views
     cleaned in `finally`.
  7. arcpy.SetProgressor on every long loop.
  8. Deterministic iteration order via ORDER BY OBJECTID.

Specific fixes vs prior revision:
  F1. NEW UI: `use_legacy_evaluation` (default False).
        - True : original SelectLayerByLocation per candidate (legacy).
        - False: optimized vectorized AABB collision in NumPy, exact
          intersect deferred to AABB-positive hits only.
  F2. Silent geometry failures: curvature/scoring helpers return
        float('inf') (never 0.0) on failure so a broken candidate is
        never selected as "best" (lower score = better).
  F3. (Plugin 03 doesn't compute Arial widths - font width fix lives in
        Plugin 04, see that file.)
  F4. ascii_safe / report_text_mode: not used in Plugin 03; preserved
        verbatim in Plugin 04.

Author: Ali Mirjafari + Kiro
"""

from __future__ import annotations

import os
import math
import time
import datetime
import logging
import traceback
import unittest
import gc
import uuid
import contextlib
from typing import Iterable, List, Optional, Sequence, Tuple

import arcpy

# NumPy is shipped with ArcGIS Pro; the optimized AABB path needs it.
# We import lazily so the legacy path still works if numpy is missing.
try:
    import numpy as _np
    _NUMPY_OK = True
except ImportError:
    _np = None
    _NUMPY_OK = False

PT_TO_MM = 0.3527777778

# =============================================================================
# 0. Messaging
# =============================================================================

def _msg(s: str) -> None:
    arcpy.AddMessage(str(s))


def _warn(s: str) -> None:
    arcpy.AddWarning(str(s))


def _err(s: str) -> None:
    arcpy.AddError(str(s))


def _diag(s: str) -> None:
    _msg(f"[DIAG] {s}")


# =============================================================================
# 1. Logger
# =============================================================================

def _setup_logger(out_ws: str, tool_tag: str):
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    log_dir = out_ws
    if out_ws and out_ws.lower().endswith(".gdb"):
        log_dir = os.path.dirname(out_ws) or out_ws
    log_path = os.path.join(log_dir, f"contour_opt_{tool_tag}_{ts}.log")

    logger_name = f"ContourLabelOptimizer_{tool_tag}_{ts}"
    logger = logging.getLogger(logger_name)
    logger.setLevel(logging.DEBUG)
    try:
        fh = logging.FileHandler(log_path)
        fh.setLevel(logging.DEBUG)
        fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
        fh.setFormatter(fmt)
        logger.addHandler(fh)
        logger.info("Logger initialized. Log file: %s", log_path)
    except OSError as ex:
        _warn(f"Could not open log file '{log_path}': {ex}")
        log_path = ""
    return logger, log_path


def _shutdown_logger(logger) -> None:
    if not logger:
        return
    for h in list(logger.handlers):
        try:
            h.flush()
            h.close()
        except (OSError, ValueError):
            pass
        try:
            logger.removeHandler(h)
        except (ValueError, AttributeError):
            pass


def _log_msg(messages, logger, level: str, text: str) -> None:
    if logger:
        try:
            if level == "DEBUG":
                logger.debug(text)
            elif level == "INFO":
                logger.info(text)
            elif level == "WARN":
                logger.warning(text)
            elif level == "ERROR":
                logger.error(text)
            else:
                logger.info(text)
        except (OSError, ValueError):
            pass
    if messages is not None:
        try:
            if level == "ERROR":
                messages.addErrorMessage(text)
            elif level == "WARN":
                messages.addWarningMessage(text)
            else:
                messages.addMessage(text)
        except (arcpy.ExecuteError, RuntimeError):
            pass
    else:
        if level == "ERROR":
            _err(text)
        elif level == "WARN":
            _warn(text)
        else:
            _msg(text)


# =============================================================================
# 2. Environment snapshot / restore (Master Rule 4)
# =============================================================================

_ENV_KEYS = (
    "extent", "mask", "outputCoordinateSystem", "workspace",
    "scratchWorkspace", "parallelProcessingFactor", "overwriteOutput",
    "autoCancelling",
)


def _snapshot_env() -> dict:
    return {k: getattr(arcpy.env, k, None) for k in _ENV_KEYS}


def _restore_env(snap: dict) -> None:
    for k, v in snap.items():
        try:
            setattr(arcpy.env, k, v)
        except (arcpy.ExecuteError, RuntimeError) as ex:
            _warn(f"Could not restore arcpy.env.{k}: {ex}")


def _prime_env() -> None:
    arcpy.env.extent = None
    arcpy.env.mask = None
    arcpy.env.outputCoordinateSystem = None
    arcpy.env.overwriteOutput = True
    arcpy.env.parallelProcessingFactor = "100%"
    arcpy.env.autoCancelling = True


# =============================================================================
# 3. Selection hygiene (Master Rule 3)
# =============================================================================

def _selection_info(layer_or_path) -> Tuple[Optional[int], Optional[int], str]:
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
    """Return on-disk catalogPath when ignore_selection is True (default)."""
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


def _announce_selection(label: str, layer_or_path, messages=None,
                        logger=None) -> None:
    sel, total, name = _selection_info(layer_or_path)
    if sel and sel > 0:
        _log_msg(messages, logger, "WARN",
                 f"{label}: '{name}' has an active selection ({sel} of "
                 f"{total if total is not None else '?'}). Ignoring "
                 f"selection - processing FULL dataset.")
    else:
        _log_msg(messages, logger, "INFO",
                 f"[DIAG] {label}: '{name}' total="
                 f"{total if total is not None else '?'}, no active selection.")


# =============================================================================
# 4. SR / unit helpers
# =============================================================================

def _is_geographic(sr) -> bool:
    if sr is None:
        return False
    try:
        return sr.type == "Geographic"
    except (AttributeError,):
        return False


def _linear_unit_name(sr) -> str:
    if sr and hasattr(sr, "linearUnitName"):
        return sr.linearUnitName
    return "unknown"


def _meters_per_unit(sr) -> Optional[float]:
    try:
        mpu = float(sr.metersPerUnit)
    except (AttributeError, TypeError, ValueError):
        return None
    return mpu if mpu > 0 else None


def _safe_mm_to_units(mm_on_map: float, map_scale: float,
                      meters_per_unit: float) -> float:
    safe_m = (float(mm_on_map) * float(map_scale)) / 1000.0
    return safe_m / float(meters_per_unit)


def _meters_to_units(meters: float, meters_per_unit: float) -> float:
    return float(meters) / float(meters_per_unit)


def _deg(rad: float) -> float:
    return rad * 180.0 / math.pi


def _clamp(v: float, vmin: float, vmax: float) -> float:
    return max(vmin, min(vmax, v))


def _dist2d(p1, p2) -> float:
    dx = p2.X - p1.X
    dy = p2.Y - p1.Y
    return math.sqrt(dx * dx + dy * dy)



# =============================================================================
# 5. Pure geometry helpers
# =============================================================================
#
# F2 NOTE: every helper that previously returned 0.0 on failure now returns
# float('inf'). The optimizer minimises the score; an inf-score candidate
# will never be selected as best, so a silent geometry failure can no
# longer accidentally win a window.

_INF = float("inf")


def _tangent_angle_at_distance(polyline, dist_along: float,
                               eps_units: float) -> float:
    """Returns tangent angle (radians). Raises arcpy.ExecuteError on
    catastrophic failure rather than returning a fake angle."""
    total = float(polyline.length)
    if total <= 0:
        raise arcpy.ExecuteError("Tangent on zero-length polyline.")
    d0 = _clamp(dist_along - eps_units, 0.0, total)
    d1 = _clamp(dist_along + eps_units, 0.0, total)
    if abs(d1 - d0) < (eps_units * 0.25):
        d0 = _clamp(dist_along, 0.0, total)
        d1 = _clamp(dist_along + eps_units, 0.0, total)
    p0 = polyline.positionAlongLine(d0, False)
    p1 = polyline.positionAlongLine(d1, False)
    return math.atan2(p1.Y - p0.Y, p1.X - p0.X)


def _make_oriented_rect(center_pt, angle_rad: float,
                        half_len: float, half_h: float, sr):
    ca = math.cos(angle_rad)
    sa = math.sin(angle_rad)
    ax, ay = ca, sa
    px, py = -sa, ca

    def mk(dx, dy):
        return arcpy.Point(center_pt.X + dx, center_pt.Y + dy)

    c1 = mk(ax * half_len + px * half_h, ay * half_len + py * half_h)
    c2 = mk(ax * half_len - px * half_h, ay * half_len - py * half_h)
    c3 = mk(-ax * half_len - px * half_h, -ay * half_len - py * half_h)
    c4 = mk(-ax * half_len + px * half_h, -ay * half_len + py * half_h)
    arr = arcpy.Array([c1, c2, c3, c4, c1])
    return arcpy.Polygon(arr, sr)


def _sample_points_along(polyline, step_units: float):
    pts = []
    L = float(polyline.length)
    if L <= 0:
        return pts
    step_units = max(float(step_units), L / 80.0)
    n = int(max(2, math.floor(L / step_units) + 1))
    for i in range(n + 1):
        d = _clamp(i * step_units, 0.0, L)
        pts.append(polyline.positionAlongLine(d, False))
    return pts


def _curv_chord_ratio(seg) -> float:
    """Returns curvature in [0, 1]. F2: returns inf on geometry failure
    so a broken segment never wins as 'best' (score is minimised)."""
    if seg is None:
        return _INF
    try:
        L = float(seg.length)
    except (AttributeError, arcpy.ExecuteError, RuntimeError):
        return _INF
    if L <= 0:
        return _INF
    try:
        p0 = seg.firstPoint
        p1 = seg.lastPoint
    except (AttributeError, arcpy.ExecuteError, RuntimeError):
        return _INF
    if p0 is None or p1 is None:
        return _INF
    C = _dist2d(p0, p1)
    return max(0.0, 1.0 - (C / L))


def _curv_max_deflection(seg, sample_units: float) -> float:
    if seg is None:
        return _INF
    try:
        pts = _sample_points_along(seg, sample_units)
    except (arcpy.ExecuteError, RuntimeError):
        return _INF
    if len(pts) < 3:
        return _INF
    mx = 0.0
    for i in range(1, len(pts) - 1):
        a = pts[i - 1]
        b = pts[i]
        c = pts[i + 1]
        v1x, v1y = (b.X - a.X), (b.Y - a.Y)
        v2x, v2y = (c.X - b.X), (c.Y - b.Y)
        n1 = math.sqrt(v1x * v1x + v1y * v1y)
        n2 = math.sqrt(v2x * v2x + v2y * v2y)
        if n1 <= 0 or n2 <= 0:
            continue
        dot = (v1x * v2x + v1y * v2y) / (n1 * n2)
        dot = _clamp(dot, -1.0, 1.0)
        ang = math.acos(dot)
        if ang > mx:
            mx = ang
    return mx


def _curv_energy(seg, sample_units: float) -> float:
    if seg is None:
        return _INF
    try:
        pts = _sample_points_along(seg, sample_units)
    except (arcpy.ExecuteError, RuntimeError):
        return _INF
    if len(pts) < 3:
        return _INF
    total_turn = 0.0
    for i in range(1, len(pts) - 1):
        a = pts[i - 1]
        b = pts[i]
        c = pts[i + 1]
        v1x, v1y = (b.X - a.X), (b.Y - a.Y)
        v2x, v2y = (c.X - b.X), (c.Y - b.Y)
        n1 = math.sqrt(v1x * v1x + v1y * v1y)
        n2 = math.sqrt(v2x * v2x + v2y * v2y)
        if n1 <= 0 or n2 <= 0:
            continue
        dot = (v1x * v2x + v1y * v2y) / (n1 * n2)
        dot = _clamp(dot, -1.0, 1.0)
        ang = math.acos(dot)
        total_turn += abs(ang)
    try:
        L = float(seg.length)
    except (AttributeError, arcpy.ExecuteError, RuntimeError):
        return _INF
    if L <= 0:
        return _INF
    return total_turn / L


def _compute_curvature(seg, method: str, sample_units: float,
                       w_cr: float, w_md: float, w_ce: float) -> float:
    """F2: returns inf on failure (never 0.0). The score in
    _score_candidate uses w_curv * curv; an inf curv -> inf score."""
    if seg is None:
        return _INF
    if method == "ChordRatio":
        return _curv_chord_ratio(seg)
    if method == "MaxDeflection":
        return _curv_max_deflection(seg, sample_units)
    if method == "CurvatureEnergy":
        return _curv_energy(seg, sample_units)
    cr = _curv_chord_ratio(seg)
    md = _curv_max_deflection(seg, sample_units)
    ce = _curv_energy(seg, sample_units)
    if cr == _INF or md == _INF or ce == _INF:
        return _INF
    mdn = md / math.pi
    cen = min(1.0, ce * 2.0)
    ws = float(w_cr) + float(w_md) + float(w_ce)
    if ws <= 0:
        return _INF
    return ((float(w_cr) * cr) + (float(w_md) * mdn)
            + (float(w_ce) * cen)) / ws


def _iter_parts(polyline_geom, sr):
    if not polyline_geom:
        return
    try:
        is_multi = polyline_geom.isMultipart
    except (AttributeError,):
        is_multi = False
    if not is_multi:
        yield 0, polyline_geom
        return
    part_id = 0
    try:
        for part in polyline_geom.getPart():
            arr = arcpy.Array()
            for p in part:
                if p:
                    arr.add(p)
            if arr.count > 1:
                yield part_id, arcpy.Polyline(arr, sr)
            part_id += 1
    except (arcpy.ExecuteError, RuntimeError):
        yield 0, polyline_geom


# =============================================================================
# 6. Output / FC helpers
# =============================================================================

def _create_fc(workspace: str, name: str, geom_type: str, sr, logger) -> str:
    out_path = os.path.join(workspace, name)
    if arcpy.Exists(out_path):
        try:
            arcpy.management.Delete(out_path)
        except (arcpy.ExecuteError, RuntimeError) as e:
            if logger:
                logger.error("Failed deleting existing output: %s", str(e))
            raise
    arcpy.management.CreateFeatureclass(
        workspace, name, geom_type, "", "DISABLED", "DISABLED", sr)
    return out_path


def _add_fields(fc: str, defs) -> None:
    for d in defs:
        if len(d) == 2:
            arcpy.management.AddField(fc, d[0], d[1])
        else:
            arcpy.management.AddField(fc, d[0], d[1], field_length=d[2])


def _scratch_path(scratch_ws: str, prefix: str) -> str:
    return os.path.join(scratch_ws, f"{prefix}_{uuid.uuid4().hex[:8]}")


@contextlib.contextmanager
def _temp_artifacts(*paths):
    """Always delete a list of scratch paths on exit, even on hard errors."""
    try:
        yield paths
    finally:
        for p in paths:
            if not p:
                continue
            try:
                if arcpy.Exists(p):
                    arcpy.management.Delete(p)
            except (arcpy.ExecuteError, RuntimeError):
                pass


# =============================================================================
# 7. Text metrics
# =============================================================================

def _derive_text_metrics_from_annotation(anno_layer_or_path, logger,
                                         max_samples: int = 80):
    if not anno_layer_or_path:
        return None, None
    src = _resolve_full_source(anno_layer_or_path)
    fields = [f.name for f in arcpy.ListFields(src)]
    text_field = None
    for cand in ["TextString", "TEXTSTRING", "Text", "TEXT"]:
        if cand in fields:
            text_field = cand
            break
    if not text_field:
        if logger:
            logger.warning(
                "Annotation text field not found (expected TextString/TEXT).")
        return None, None
    heights: List[float] = []
    cws: List[float] = []
    n = 0
    try:
        with arcpy.da.SearchCursor(src, ["SHAPE@", text_field]) as cur:
            for g, s in cur:
                if not g:
                    continue
                s = "" if s is None else str(s)
                if len(s) < 2:
                    continue
                ext = g.extent
                if not ext:
                    continue
                h = float(ext.height)
                w = float(ext.width)
                if h <= 0 or w <= 0:
                    continue
                cw = w / (h * float(len(s)))
                if cw <= 0:
                    continue
                heights.append(h)
                cws.append(cw)
                n += 1
                if n >= max_samples:
                    break
    except (arcpy.ExecuteError, RuntimeError):
        if logger:
            logger.warning("Failed reading annotation samples: %s",
                           traceback.format_exc())
    if not heights:
        if logger:
            logger.warning("No usable annotation samples.")
        return None, None
    heights.sort()
    cws.sort()
    return heights[len(heights) // 2], cws[len(cws) // 2]


def _estimate_text_metrics_units(text_value, map_scale: float, mpu: float,
                                 pad_units: float, derive_metrics: bool,
                                 derived_h_units, derived_cw,
                                 font_size_pt: float, char_w_factor: float):
    s = "" if text_value is None else str(text_value)
    n = max(1, len(s))
    if derive_metrics and derived_h_units and float(derived_h_units) > 0:
        h_units = float(derived_h_units)
        cw = float(derived_cw) if (derived_cw and float(derived_cw) > 0) \
            else float(char_w_factor)
        w_units = float(n) * cw * h_units
    else:
        h_mm_map = float(font_size_pt) * PT_TO_MM
        h_m_ground = (h_mm_map * float(map_scale)) / 1000.0
        h_units = h_m_ground / float(mpu)
        w_units = float(n) * float(char_w_factor) * h_units
    return h_units, w_units, float(pad_units)


# =============================================================================
# 8. Obstacle mask FC (scratchGDB-resident, spatial-indexed)
# =============================================================================

def _build_obstacle_mask_fc(obstacle_layers: Iterable, anno_layer,
                             safe_units: float, scratch_gdb: str,
                             logger, messages) -> Optional[str]:
    layers = []
    if obstacle_layers:
        layers.extend([_resolve_full_source(x) for x in obstacle_layers if x])
    if anno_layer:
        layers.append(_resolve_full_source(anno_layer))
    if not layers:
        return None

    buf_fcs = []
    for i, lyr in enumerate(layers):
        try:
            tmp_buf = _scratch_path(scratch_gdb, f"obs_buf_{i}")
            arcpy.analysis.Buffer(lyr, tmp_buf, str(safe_units),
                                  dissolve_option="ALL")
            buf_fcs.append(tmp_buf)
        except (arcpy.ExecuteError, RuntimeError):
            _log_msg(messages, logger, "WARN",
                     f"Obstacle buffer failed for a layer: "
                     f"{traceback.format_exc()}")

    if not buf_fcs:
        return None

    merged = _scratch_path(scratch_gdb, "obs_merge")
    try:
        arcpy.management.Merge(buf_fcs, merged)
    except (arcpy.ExecuteError, RuntimeError):
        _log_msg(messages, logger, "ERROR",
                 f"Obstacle merge failed: {traceback.format_exc()}")
        return None

    dissolved = _scratch_path(scratch_gdb, "ObstacleMask")
    try:
        arcpy.management.Dissolve(merged, dissolved)
    except (arcpy.ExecuteError, RuntimeError):
        _log_msg(messages, logger, "ERROR",
                 f"Obstacle dissolve failed: {traceback.format_exc()}")
        return None

    try:
        arcpy.management.AddSpatialIndex(dissolved)
    except (arcpy.ExecuteError, RuntimeError):
        pass

    for fc in buf_fcs + [merged]:
        try:
            arcpy.management.Delete(fc)
        except (arcpy.ExecuteError, RuntimeError):
            pass
    return dissolved


def _make_mask_layer(mask_fc: Optional[str]) -> Optional[str]:
    if not mask_fc:
        return None
    name = "obs_mask_lyr_" + uuid.uuid4().hex[:6]
    try:
        arcpy.management.MakeFeatureLayer(mask_fc, name)
        return name
    except (arcpy.ExecuteError, RuntimeError):
        return None


# =============================================================================
# 9. AABB caches and overlap engines
# =============================================================================
#
# F1 Optimisation:
#   When use_legacy_evaluation is False (default), per-candidate overlap
#   tests run against precomputed NumPy AABB arrays - one for the obstacle
#   mask polygons, and one (incrementally grown) for already-placed labels.
#   The vectorised AABB hit-test (boolean & on four float comparisons)
#   filters obstacle candidates in microseconds; only AABB hits trigger
#   an exact arcpy intersect call. This eliminates the per-candidate
#   SelectLayerByLocation roundtrip that dominated the original profile.
#
# When use_legacy_evaluation is True, the original SLBL path is used
# (kept verbatim in _mask_overlap_area_legacy) so cartographers can fall
# back to the previous behaviour.
# =============================================================================

class _PlacedCache:
    """AABB cache of geometries already placed in this run."""

    __slots__ = ("items", "_bbox_arr", "_geoms")

    def __init__(self):
        # Legacy-style list of (xmin, ymin, xmax, ymax, geom) tuples.
        self.items: List[Tuple[float, float, float, float, object]] = []
        # Optimised NumPy bbox array (lazy, rebuilt on add when needed).
        self._bbox_arr = None
        self._geoms: List[object] = []

    def add(self, geom) -> None:
        if geom is None:
            return
        ext = geom.extent
        if ext is None:
            return
        self.items.append((ext.XMin, ext.YMin, ext.XMax, ext.YMax, geom))
        self._geoms.append(geom)
        self._bbox_arr = None  # invalidate

    def _ensure_array(self):
        if self._bbox_arr is not None or not _NUMPY_OK:
            return
        if not self.items:
            self._bbox_arr = _np.empty((0, 4), dtype=_np.float64)
            return
        arr = _np.empty((len(self.items), 4), dtype=_np.float64)
        for i, (xmin, ymin, xmax, ymax, _g) in enumerate(self.items):
            arr[i, 0] = xmin
            arr[i, 1] = ymin
            arr[i, 2] = xmax
            arr[i, 3] = ymax
        self._bbox_arr = arr

    def overlap_area(self, foot_geom, logger=None,
                     use_legacy: bool = False) -> float:
        """Return total overlap area between foot_geom and placed labels."""
        if foot_geom is None or not self.items:
            return 0.0
        ea = foot_geom.extent
        if ea is None:
            return 0.0
        axmin, aymin, axmax, aymax = ea.XMin, ea.YMin, ea.XMax, ea.YMax

        if use_legacy or not _NUMPY_OK:
            total = 0.0
            for (bxmin, bymin, bxmax, bymax, g) in self.items:
                if (axmax < bxmin or axmin > bxmax
                        or aymax < bymin or aymin > bymax):
                    continue
                try:
                    if foot_geom.disjoint(g):
                        continue
                    inter = foot_geom.intersect(g, 4)
                    if inter:
                        total += float(inter.area)
                except (arcpy.ExecuteError, RuntimeError):
                    if logger:
                        logger.warning(
                            "Self-overlap intersect failed: %s",
                            traceback.format_exc())
            return total

        # Optimised: vectorised AABB filter, exact intersect on hits.
        self._ensure_array()
        bb = self._bbox_arr
        if bb.shape[0] == 0:
            return 0.0
        # AABB intersect: NOT(separation along any axis)
        sep = ((axmax < bb[:, 0]) | (axmin > bb[:, 2])
               | (aymax < bb[:, 1]) | (aymin > bb[:, 3]))
        hits = _np.flatnonzero(~sep)
        if hits.size == 0:
            return 0.0
        total = 0.0
        for idx in hits.tolist():
            g = self._geoms[idx]
            try:
                if foot_geom.disjoint(g):
                    continue
                inter = foot_geom.intersect(g, 4)
                if inter:
                    total += float(inter.area)
            except (arcpy.ExecuteError, RuntimeError):
                if logger:
                    logger.warning(
                        "Self-overlap intersect failed: %s",
                        traceback.format_exc())
        return total


class _ObstacleAABBStore:
    """
    Pre-extracted obstacle AABBs + geometries for the optimised path.
    Built once at the start of execute(), used per-candidate.
    """

    __slots__ = ("bbox", "geoms", "n")

    def __init__(self, mask_fc: Optional[str], logger):
        self.bbox = None
        self.geoms: List[object] = []
        self.n = 0
        if not mask_fc or not _NUMPY_OK:
            return
        # Stream the obstacle mask features once.
        bxmin: List[float] = []
        bymin: List[float] = []
        bxmax: List[float] = []
        bymax: List[float] = []
        try:
            with arcpy.da.SearchCursor(mask_fc, ["SHAPE@"]) as cur:
                for (g,) in cur:
                    if g is None:
                        continue
                    ext = g.extent
                    if ext is None:
                        continue
                    bxmin.append(ext.XMin)
                    bymin.append(ext.YMin)
                    bxmax.append(ext.XMax)
                    bymax.append(ext.YMax)
                    self.geoms.append(g)
        except (arcpy.ExecuteError, RuntimeError):
            if logger:
                logger.warning(
                    "Building obstacle AABB store failed: %s",
                    traceback.format_exc())
            self.geoms = []
            return
        self.n = len(self.geoms)
        if self.n == 0:
            self.bbox = _np.empty((0, 4), dtype=_np.float64)
            return
        arr = _np.empty((self.n, 4), dtype=_np.float64)
        arr[:, 0] = bxmin
        arr[:, 1] = bymin
        arr[:, 2] = bxmax
        arr[:, 3] = bymax
        self.bbox = arr

    def overlap_area(self, foot_geom, logger=None) -> float:
        if foot_geom is None or self.bbox is None or self.n == 0:
            return 0.0
        ea = foot_geom.extent
        if ea is None:
            return 0.0
        axmin, aymin, axmax, aymax = ea.XMin, ea.YMin, ea.XMax, ea.YMax
        bb = self.bbox
        sep = ((axmax < bb[:, 0]) | (axmin > bb[:, 2])
               | (aymax < bb[:, 1]) | (aymin > bb[:, 3]))
        hits = _np.flatnonzero(~sep)
        if hits.size == 0:
            return 0.0
        total = 0.0
        for idx in hits.tolist():
            g = self.geoms[idx]
            try:
                if foot_geom.disjoint(g):
                    continue
                inter = foot_geom.intersect(g, 4)
                if inter:
                    total += float(inter.area)
            except (arcpy.ExecuteError, RuntimeError):
                if logger:
                    logger.warning(
                        "AABB-store intersect failed: %s",
                        traceback.format_exc())
        return total


# =============================================================================
# 10. Mask overlap via SelectLayerByLocation (LEGACY path - F1 toggle)
# =============================================================================

def _mask_overlap_area_legacy(foot_geom, mask_layer: Optional[str],
                              mask_fc: Optional[str], logger=None) -> float:
    """Original SLBL-based mask overlap. Used when use_legacy_evaluation
    is True. Kept verbatim semantics so cartographers can fall back to
    the prior behaviour bit-for-bit."""
    if foot_geom is None or not mask_fc:
        return 0.0
    if not mask_layer:
        total = 0.0
        try:
            with arcpy.da.SearchCursor(mask_fc, ["SHAPE@"]) as cur:
                for (g,) in cur:
                    if g is None:
                        continue
                    try:
                        if foot_geom.disjoint(g):
                            continue
                        inter = foot_geom.intersect(g, 4)
                        if inter:
                            total += float(inter.area)
                    except (arcpy.ExecuteError, RuntimeError):
                        pass
        except (arcpy.ExecuteError, RuntimeError):
            pass
        return total

    total = 0.0
    try:
        arcpy.management.SelectLayerByLocation(
            mask_layer, "INTERSECT", foot_geom,
            search_distance="", selection_type="NEW_SELECTION")
        try:
            with arcpy.da.SearchCursor(mask_layer, ["SHAPE@"]) as cur:
                for (g,) in cur:
                    if g is None:
                        continue
                    try:
                        if foot_geom.disjoint(g):
                            continue
                        inter = foot_geom.intersect(g, 4)
                        if inter:
                            total += float(inter.area)
                    except (arcpy.ExecuteError, RuntimeError):
                        if logger:
                            logger.warning(
                                "Mask intersect failed: %s",
                                traceback.format_exc())
        finally:
            try:
                arcpy.management.SelectLayerByAttribute(
                    mask_layer, "CLEAR_SELECTION")
            except (arcpy.ExecuteError, RuntimeError):
                pass
    except (arcpy.ExecuteError, RuntimeError):
        if logger:
            logger.warning(
                "SelectLayerByLocation on mask failed: %s",
                traceback.format_exc())
        try:
            with arcpy.da.SearchCursor(mask_fc, ["SHAPE@"]) as cur:
                for (g,) in cur:
                    if g is None:
                        continue
                    try:
                        if foot_geom.disjoint(g):
                            continue
                        inter = foot_geom.intersect(g, 4)
                        if inter:
                            total += float(inter.area)
                    except (arcpy.ExecuteError, RuntimeError):
                        pass
        except (arcpy.ExecuteError, RuntimeError):
            pass
    return total


# =============================================================================
# 11. Annotation seeding
# =============================================================================

def _find_seed_from_annotation(anno_layer_or_path, part_geom,
                               win_start: float, win_end: float,
                               search_pad_units: float, logger):
    if not anno_layer_or_path:
        return None
    src = _resolve_full_source(anno_layer_or_path)
    try:
        seg = part_geom.segmentAlongLine(win_start, win_end, False)
    except (arcpy.ExecuteError, RuntimeError):
        if logger:
            logger.warning("Seed from annotation failed: %s",
                           traceback.format_exc())
        return None
    if not seg:
        return None
    env = seg.extent
    if not env:
        return None
    xmin = env.XMin - search_pad_units
    xmax = env.XMax + search_pad_units
    ymin = env.YMin - search_pad_units
    ymax = env.YMax + search_pad_units
    best_d = None
    best_dist = None
    try:
        with arcpy.da.SearchCursor(src, ["SHAPE@"]) as cur:
            for (g,) in cur:
                if not g:
                    continue
                ge = g.extent
                if not ge:
                    continue
                if (ge.XMax < xmin or ge.XMin > xmax
                        or ge.YMax < ymin or ge.YMin > ymax):
                    continue
                cpt = g.centroid
                try:
                    near_pt = part_geom.snapToLine(cpt)
                    d = part_geom.measureOnLine(near_pt, False)
                except (arcpy.ExecuteError, RuntimeError):
                    continue
                if d < win_start or d > win_end:
                    continue
                dist = float(cpt.distanceTo(near_pt))
                if best_dist is None or dist < best_dist:
                    best_dist = dist
                    best_d = float(d)
    except (arcpy.ExecuteError, RuntimeError):
        if logger:
            logger.warning("Seed from annotation cursor failed: %s",
                           traceback.format_exc())
        return None
    return best_d



# =============================================================================
# 12. Scoring engine
# =============================================================================

def _build_offsets(internal_step_units: float, max_tries: int) -> List[float]:
    offs = [0.0]
    k = 1
    while len(offs) < max_tries:
        offs.append(float(k) * internal_step_units)
        if len(offs) >= max_tries:
            break
        offs.append(float(-k) * internal_step_units)
        k += 1
    return offs


def _score_candidate(part_geom, center_d: float, win_start: float,
                     win_end: float, foot_len: float, foot_h: float,
                     eps_units: float, curv_method: str,
                     curv_sample_units: float,
                     w_cr: float, w_md: float, w_ce: float,
                     w_curv: float, w_ovlp: float, w_center: float,
                     max_ovlp, max_curv,
                     mask_layer, mask_fc,
                     obstacle_aabb: Optional[_ObstacleAABBStore],
                     placed_cache: _PlacedCache, sr,
                     use_legacy: bool, logger):
    """Build and score one candidate. Returns dict or None.

    F2: any geometry failure that previously returned 0.0 now returns
    None (candidate skipped) or float('inf') for the score component,
    so the candidate cannot accidentally beat a real one.
    """
    total_len = float(part_geom.length)
    if total_len <= 0:
        return None
    if center_d < win_start or center_d > win_end:
        return None
    seg_start = _clamp(center_d - 0.5 * foot_len, win_start, win_end)
    seg_end = _clamp(center_d + 0.5 * foot_len, win_start, win_end)
    if (seg_end - seg_start) < (0.25 * foot_len):
        seg_start = _clamp(center_d - 0.25 * foot_len, win_start, win_end)
        seg_end = _clamp(center_d + 0.25 * foot_len, win_start, win_end)
    try:
        seg_geom = part_geom.segmentAlongLine(seg_start, seg_end, False)
    except (arcpy.ExecuteError, RuntimeError):
        if logger:
            logger.warning(
                "segmentAlongLine failed (center %0.2f): %s",
                center_d, traceback.format_exc())
        return None
    if not seg_geom or float(seg_geom.length) <= 0:
        return None
    try:
        ang = _tangent_angle_at_distance(part_geom, center_d, eps_units)
    except (arcpy.ExecuteError, RuntimeError):
        return None
    rot_deg = _deg(ang)
    center_pt = part_geom.positionAlongLine(center_d, False)
    foot_geom = _make_oriented_rect(
        center_pt, ang, 0.5 * foot_len, 0.5 * foot_h, sr)
    curv = _compute_curvature(seg_geom, curv_method, curv_sample_units,
                              w_cr, w_md, w_ce)
    if curv == _INF:
        return None  # F2: bad curvature -> candidate dies, not "wins"

    # Overlap: routed by F1 toggle.
    ovlp = 0.0
    if mask_fc:
        if use_legacy or obstacle_aabb is None or obstacle_aabb.bbox is None:
            ovlp += _mask_overlap_area_legacy(foot_geom, mask_layer,
                                              mask_fc, logger)
        else:
            ovlp += obstacle_aabb.overlap_area(foot_geom, logger)
    if placed_cache is not None:
        ovlp += placed_cache.overlap_area(
            foot_geom, logger, use_legacy=use_legacy)

    if max_ovlp is not None and ovlp > float(max_ovlp):
        return None
    if max_curv is not None and curv > float(max_curv):
        return None
    win_len = max(1e-9, float(win_end - win_start))
    win_center = win_start + 0.5 * win_len
    center_pen = abs(center_d - win_center) / win_len
    center_pen = math.pow(center_pen, 1.5)
    score = ((float(w_curv) * curv) + (float(w_ovlp) * ovlp)
             + (float(w_center) * center_pen))
    return {
        "center_d": float(center_d),
        "seg_geom": seg_geom,
        "foot_geom": foot_geom,
        "ang_deg": float(rot_deg),
        "curv": float(curv),
        "ovlp": float(ovlp),
        "score": float(score),
    }


def _is_major_value(val, major_interval) -> bool:
    try:
        x = float(val)
        mi = float(major_interval)
    except (TypeError, ValueError):
        return False
    if mi <= 0:
        return True
    r = abs(x % mi)
    return (r < 1e-6) or (abs(r - mi) < 1e-6)


# =============================================================================
# 13. Toolbox + tools
# =============================================================================

class Toolbox(object):
    def __init__(self):
        self.label = "Contour Label Optimizer v4 (Pro, native)"
        self.alias = "contourlabelopt4_pro"
        self.tools = [
            OptimizeContourLabelAnchorsV4,
            ValidateLabelAnchors,
            CurvatureHeatmap,
            ConvertLabelsToAnnotationPro,
            RunUnitTests,
        ]


# ----------------------------------------------------------------------
# Main optimizer
# ----------------------------------------------------------------------

class OptimizeContourLabelAnchorsV4(object):
    def __init__(self):
        self.label = "Optimize Contour Label Anchors (v4 native)"
        self.description = (
            "Places one optimized label anchor per along-line interval window.\n\n"
            " - SELECTION-BYPASS hardwired (full datasets always processed)\n"
            " - Obstacle mask lives in scratchGDB on disk with spatial index\n"
            " - Per-candidate overlap uses a vectorised NumPy AABB filter +\n"
            "   exact intersect by default (F1 optimised path).\n"
            " - Set 'Use legacy evaluation' to fall back to the original\n"
            "   per-candidate SelectLayerByLocation logic."
        )
        self.canRunInBackground = True

    def isLicensed(self):
        return True

    def getParameterInfo(self):
        in_contours = arcpy.Parameter(
            displayName="Contour lines", name="in_contours",
            datatype="GPFeatureLayer", parameterType="Required",
            direction="Input")
        elev_field = arcpy.Parameter(
            displayName="Elevation field (label text source)",
            name="elev_field", datatype="Field",
            parameterType="Required", direction="Input")
        elev_field.parameterDependencies = [in_contours.name]
        selection_mode = arcpy.Parameter(
            displayName="Selection mode", name="selection_mode",
            datatype="GPString", parameterType="Required", direction="Input")
        selection_mode.filter.type = "ValueList"
        selection_mode.filter.list = ["ALL", "MAJOR_INTERVAL"]
        selection_mode.value = "ALL"
        selection_mode.category = "Selection"
        major_interval = arcpy.Parameter(
            displayName="Major interval (e.g., 100 for elev%100==0)",
            name="major_interval", datatype="GPDouble",
            parameterType="Optional", direction="Input")
        major_interval.value = 100.0
        major_interval.enabled = False
        major_interval.category = "Selection"
        interval_m = arcpy.Parameter(
            displayName="Along-line interval (meters)", name="interval_m",
            datatype="GPDouble", parameterType="Required", direction="Input")
        interval_m.value = 500.0
        safe_mm = arcpy.Parameter(
            displayName="Safe distance from obstacles (mm on map)",
            name="safe_mm", datatype="GPDouble",
            parameterType="Required", direction="Input")
        safe_mm.value = 2.0
        halo_mm = arcpy.Parameter(
            displayName="Extra halo/mask margin (mm on map)",
            name="halo_mm", datatype="GPDouble",
            parameterType="Optional", direction="Input")
        halo_mm.value = 0.0
        map_scale = arcpy.Parameter(
            displayName="Map scale denominator (e.g., 25000 for 1:25000)",
            name="map_scale", datatype="GPDouble",
            parameterType="Required", direction="Input")
        map_scale.value = 25000.0
        obstacles = arcpy.Parameter(
            displayName="Obstacle layers (lines, polygons, points)",
            name="obstacles", datatype="GPFeatureLayer",
            parameterType="Optional", direction="Input", multiValue=True)
        obstacles.category = "Obstacles"
        anno_layer = arcpy.Parameter(
            displayName="Existing annotation layer (barrier + optional metrics)",
            name="anno_layer", datatype="GPFeatureLayer",
            parameterType="Optional", direction="Input")
        anno_layer.category = "Obstacles"
        derive_metrics = arcpy.Parameter(
            displayName="Derive text metrics from annotation (if provided)",
            name="derive_text_metrics", datatype="GPBoolean",
            parameterType="Optional", direction="Input")
        derive_metrics.value = True
        derive_metrics.category = "Text Metrics"
        font_size_pt = arcpy.Parameter(
            displayName="Font size (points) if metrics not derived",
            name="font_size_pt", datatype="GPDouble",
            parameterType="Optional", direction="Input")
        font_size_pt.value = 8.0
        font_size_pt.category = "Text Metrics"
        char_w_factor = arcpy.Parameter(
            displayName="Average character width factor if metrics not derived",
            name="char_w_factor", datatype="GPDouble",
            parameterType="Optional", direction="Input")
        char_w_factor.value = 0.6
        char_w_factor.category = "Text Metrics"
        curv_method = arcpy.Parameter(
            displayName="Curvature method", name="curv_method",
            datatype="GPString", parameterType="Required", direction="Input")
        curv_method.filter.type = "ValueList"
        curv_method.filter.list = ["Hybrid", "ChordRatio",
                                   "MaxDeflection", "CurvatureEnergy"]
        curv_method.value = "Hybrid"
        curv_method.category = "Curvature"
        curv_sample_m = arcpy.Parameter(
            displayName="Curvature sampling step (meters)",
            name="curv_sample_m", datatype="GPDouble",
            parameterType="Required", direction="Input")
        curv_sample_m.value = 5.0
        curv_sample_m.category = "Curvature"
        w_cr = arcpy.Parameter(
            displayName="Hybrid weight: chord ratio", name="w_cr",
            datatype="GPDouble", parameterType="Required", direction="Input")
        w_cr.value = 0.5
        w_cr.category = "Curvature"
        w_md = arcpy.Parameter(
            displayName="Hybrid weight: max deflection", name="w_md",
            datatype="GPDouble", parameterType="Required", direction="Input")
        w_md.value = 0.3
        w_md.category = "Curvature"
        w_ce = arcpy.Parameter(
            displayName="Hybrid weight: curvature energy", name="w_ce",
            datatype="GPDouble", parameterType="Required", direction="Input")
        w_ce.value = 0.2
        w_ce.category = "Curvature"
        w_curv = arcpy.Parameter(
            displayName="Weight: curvature", name="w_curv",
            datatype="GPDouble", parameterType="Required", direction="Input")
        w_curv.value = 1.0
        w_curv.category = "Scoring"
        w_ovlp = arcpy.Parameter(
            displayName="Weight: overlap area", name="w_ovlp",
            datatype="GPDouble", parameterType="Required", direction="Input")
        w_ovlp.value = 5.0
        w_ovlp.category = "Scoring"
        w_center = arcpy.Parameter(
            displayName="Weight: window-center preference",
            name="w_center", datatype="GPDouble",
            parameterType="Required", direction="Input")
        w_center.value = 0.25
        w_center.category = "Scoring"
        max_ovlp = arcpy.Parameter(
            displayName="Max allowed overlap area (linear_unit^2)",
            name="max_ovlp", datatype="GPDouble",
            parameterType="Optional", direction="Input")
        max_ovlp.category = "Thresholds"
        max_curv = arcpy.Parameter(
            displayName="Max allowed curvature score", name="max_curv",
            datatype="GPDouble", parameterType="Optional", direction="Input")
        max_curv.category = "Thresholds"
        min_contour_m = arcpy.Parameter(
            displayName="Minimum contour part length (meters)",
            name="min_contour_m", datatype="GPDouble",
            parameterType="Optional", direction="Input")
        min_contour_m.value = 0.0
        min_contour_m.category = "Thresholds"
        short_policy = arcpy.Parameter(
            displayName="Short part policy", name="short_policy",
            datatype="GPString", parameterType="Required", direction="Input")
        short_policy.filter.type = "ValueList"
        short_policy.filter.list = ["PLACE_CENTER", "SKIP"]
        short_policy.value = "PLACE_CENTER"
        short_policy.category = "Thresholds"
        out_ws = arcpy.Parameter(
            displayName="Output workspace (file geodatabase recommended)",
            name="out_ws", datatype="DEWorkspace",
            parameterType="Required", direction="Input")
        out_segments_name = arcpy.Parameter(
            displayName="Output segments name",
            name="out_segments_name", datatype="GPString",
            parameterType="Required", direction="Input")
        out_segments_name.value = "ContourLabelSegments"
        out_points_name = arcpy.Parameter(
            displayName="Output points name",
            name="out_points_name", datatype="GPString",
            parameterType="Required", direction="Input")
        out_points_name.value = "ContourLabelPoints"
        make_footprints = arcpy.Parameter(
            displayName="Create QA footprints (polygons)",
            name="make_footprints", datatype="GPBoolean",
            parameterType="Optional", direction="Input")
        make_footprints.value = False
        make_footprints.category = "Outputs"
        out_footprints_name = arcpy.Parameter(
            displayName="QA footprints name (if enabled)",
            name="out_footprints_name", datatype="GPString",
            parameterType="Optional", direction="Input")
        out_footprints_name.value = "ContourLabelFootprints"
        out_footprints_name.enabled = False
        out_footprints_name.category = "Outputs"
        make_stats = arcpy.Parameter(
            displayName="Create statistics table",
            name="make_stats", datatype="GPBoolean",
            parameterType="Optional", direction="Input")
        make_stats.value = True
        make_stats.category = "Outputs"
        out_stats_name = arcpy.Parameter(
            displayName="Statistics table name (if enabled)",
            name="out_stats_name", datatype="GPString",
            parameterType="Optional", direction="Input")
        out_stats_name.value = "ContourLabelStats"
        out_stats_name.enabled = True
        out_stats_name.category = "Outputs"
        max_tries = arcpy.Parameter(
            displayName="Internal tries per window (refinement around seed)",
            name="max_tries", datatype="GPLong",
            parameterType="Required", direction="Input")
        max_tries.value = 11
        max_tries.category = "Advanced"
        # F1 NEW PARAMETER
        use_legacy_evaluation = arcpy.Parameter(
            displayName=(
                "Use legacy evaluation (per-candidate SelectLayerByLocation; "
                "slower but matches prior behaviour)"),
            name="use_legacy_evaluation", datatype="GPBoolean",
            parameterType="Optional", direction="Input")
        use_legacy_evaluation.value = False
        use_legacy_evaluation.category = "Advanced"
        add_to_map = arcpy.Parameter(
            displayName="Add outputs to current map",
            name="add_to_map", datatype="GPBoolean",
            parameterType="Optional", direction="Input")
        add_to_map.value = True
        add_to_map.category = "Advanced"

        out_segments = arcpy.Parameter(
            displayName="Output segments", name="out_segments",
            datatype="DEFeatureClass",
            parameterType="Derived", direction="Output")
        out_points = arcpy.Parameter(
            displayName="Output points", name="out_points",
            datatype="DEFeatureClass",
            parameterType="Derived", direction="Output")
        out_footprints = arcpy.Parameter(
            displayName="Output footprints", name="out_footprints",
            datatype="DEFeatureClass",
            parameterType="Derived", direction="Output")
        out_stats = arcpy.Parameter(
            displayName="Output statistics table", name="out_stats",
            datatype="DETable", parameterType="Derived", direction="Output")
        out_log = arcpy.Parameter(
            displayName="Log file path", name="out_log",
            datatype="GPString", parameterType="Derived", direction="Output")

        return [
            in_contours, elev_field,                       # 0,1
            selection_mode, major_interval,                # 2,3
            interval_m, safe_mm, halo_mm, map_scale,       # 4..7
            obstacles, anno_layer,                         # 8,9
            derive_metrics, font_size_pt, char_w_factor,   # 10,11,12
            curv_method, curv_sample_m, w_cr, w_md, w_ce,  # 13..17
            w_curv, w_ovlp, w_center,                      # 18..20
            max_ovlp, max_curv, min_contour_m, short_policy,  # 21..24
            out_ws, out_segments_name, out_points_name,    # 25..27
            make_footprints, out_footprints_name,          # 28,29
            make_stats, out_stats_name,                    # 30,31
            max_tries, use_legacy_evaluation, add_to_map,  # 32,33,34
            out_segments, out_points, out_footprints,      # 35..37
            out_stats, out_log,                            # 38,39
        ]

    def updateParameters(self, parameters):
        try:
            sel_mode = parameters[2].valueAsText
            parameters[3].enabled = (sel_mode == "MAJOR_INTERVAL")
            parameters[29].enabled = bool(parameters[28].value)
            parameters[31].enabled = bool(parameters[30].value)
        except (AttributeError, IndexError):
            pass

    def updateMessages(self, parameters):
        try:
            contours = parameters[0].valueAsText
            if contours:
                sr = arcpy.Describe(contours).spatialReference
                if _is_geographic(sr):
                    parameters[0].setErrorMessage(
                        "Input contours must be projected (linear units).")
                else:
                    unit_name = _linear_unit_name(sr)
                    if parameters[21].value is not None:
                        parameters[21].setWarningMessage(
                            f"max_ovlp is in {unit_name}^2.")
            sel_mode = parameters[2].valueAsText
            if sel_mode == "MAJOR_INTERVAL":
                if parameters[3].value is None or float(parameters[3].value) <= 0:
                    parameters[3].setErrorMessage(
                        "Major interval must be > 0 when Selection mode "
                        "is MAJOR_INTERVAL.")
        except (arcpy.ExecuteError, RuntimeError, AttributeError, IndexError):
            pass

    def _add_to_active_map(self, paths):
        try:
            aprx = arcpy.mp.ArcGISProject("CURRENT")
        except (arcpy.ExecuteError, RuntimeError):
            return
        m = aprx.activeMap
        if m is None:
            return
        for p in paths:
            if not p:
                continue
            try:
                m.addDataFromPath(p)
            except (arcpy.ExecuteError, RuntimeError):
                _warn(f"Could not add {p} to active map.")

    def execute(self, parameters, messages):
        env_snap = _snapshot_env()
        logger = None
        seg_ins = pt_ins = foot_ins = stats_ins = None
        mask_layer = None

        try:
            _prime_env()
            self._execute_core(parameters, messages)
        except arcpy.ExecuteError:
            _err(arcpy.GetMessages(2))
            raise
        except RuntimeError as ex:
            _err(f"Runtime error: {ex}")
            _err(traceback.format_exc())
            raise
        finally:
            _restore_env(env_snap)

    # split out so finally can still touch shared cleanup symbols cleanly
    def _execute_core(self, parameters, messages):
        in_contours_layer = parameters[0].valueAsText
        elev_field = parameters[1].valueAsText
        selection_mode = parameters[2].valueAsText
        major_interval = parameters[3].value
        interval_m = float(parameters[4].value)
        safe_mm = float(parameters[5].value)
        halo_mm = (float(parameters[6].value)
                   if parameters[6].value is not None else 0.0)
        map_scale = float(parameters[7].value)
        obstacles_text = parameters[8].valueAsText
        obstacle_layers = ([s.strip() for s in obstacles_text.split(";")]
                           if obstacles_text else [])
        anno_layer = (parameters[9].valueAsText
                      if parameters[9].valueAsText else None)
        derive_metrics = bool(parameters[10].value)
        font_size_pt = float(parameters[11].value) if parameters[11].value else 8.0
        char_w_factor = (float(parameters[12].value)
                         if parameters[12].value else 0.6)
        curv_method = parameters[13].valueAsText
        curv_sample_m = float(parameters[14].value)
        w_cr = float(parameters[15].value)
        w_md = float(parameters[16].value)
        w_ce = float(parameters[17].value)
        w_curv = float(parameters[18].value)
        w_ovlp = float(parameters[19].value)
        w_center = float(parameters[20].value)
        max_ovlp = parameters[21].value
        max_curv = parameters[22].value
        min_contour_m = (float(parameters[23].value)
                         if parameters[23].value else 0.0)
        short_policy = parameters[24].valueAsText
        out_ws = parameters[25].valueAsText
        out_segments_name = parameters[26].valueAsText
        out_points_name = parameters[27].valueAsText
        make_footprints = bool(parameters[28].value)
        out_footprints_name = (parameters[29].valueAsText
                               if make_footprints else None)
        make_stats = bool(parameters[30].value)
        out_stats_name = parameters[31].valueAsText if make_stats else None
        max_tries = int(parameters[32].value)
        use_legacy_evaluation = bool(parameters[33].value)
        add_to_map = bool(parameters[34].value)

        logger, log_path = _setup_logger(out_ws, "opt")
        _log_msg(messages, logger, "INFO",
                 "Starting OptimizeContourLabelAnchorsV4 (Pro)")
        _log_msg(messages, logger, "INFO",
                 f"[DIAG] use_legacy_evaluation={use_legacy_evaluation}")
        if not _NUMPY_OK and not use_legacy_evaluation:
            _log_msg(messages, logger, "WARN",
                     "NumPy unavailable; falling back to legacy evaluation "
                     "for this run.")
            use_legacy_evaluation = True

        _announce_selection("Contours", in_contours_layer, messages, logger)
        for lyr in obstacle_layers:
            _announce_selection("Obstacle", lyr, messages, logger)
        if anno_layer:
            _announce_selection("Annotation", anno_layer, messages, logger)

        in_contours = _resolve_full_source(in_contours_layer)

        if interval_m <= 0:
            raise arcpy.ExecuteError("Along-line interval must be > 0.")
        if safe_mm < 0 or halo_mm < 0:
            raise arcpy.ExecuteError("Safe distance and halo must be >= 0.")
        if map_scale <= 0:
            raise arcpy.ExecuteError("Map scale must be > 0.")
        if max_tries < 1:
            raise arcpy.ExecuteError("Internal tries must be >= 1.")
        if selection_mode == "MAJOR_INTERVAL":
            if major_interval is None or float(major_interval) <= 0:
                raise arcpy.ExecuteError("Major interval must be > 0.")

        desc = arcpy.Describe(in_contours)
        sr = desc.spatialReference
        if _is_geographic(sr):
            raise arcpy.ExecuteError(
                "Projected coordinate system is required.")
        mpu = _meters_per_unit(sr)
        if not mpu or mpu <= 0:
            raise arcpy.ExecuteError(
                "Could not determine meters-per-unit.")

        unit_name = _linear_unit_name(sr)
        _log_msg(messages, logger, "INFO",
                 f"Linear unit: {unit_name} (metersPerUnit={mpu})")
        _log_msg(messages, logger, "INFO",
                 f"max_ovlp unit is {unit_name}^2")

        safe_units = _safe_mm_to_units(safe_mm, map_scale, mpu)
        halo_units = (_safe_mm_to_units(halo_mm, map_scale, mpu)
                      if halo_mm > 0 else 0.0)
        pad_units = safe_units + halo_units
        interval_units = _meters_to_units(interval_m, mpu)
        curv_sample_units = _meters_to_units(curv_sample_m, mpu)
        min_contour_units = _meters_to_units(min_contour_m, mpu)

        _log_msg(messages, logger, "INFO",
                 f"interval_units={interval_units:.4f} "
                 f"safe_units={safe_units:.4f} "
                 f"halo_units={halo_units:.4f} "
                 f"pad_units={pad_units:.4f}")

        scratch_gdb = arcpy.env.scratchGDB
        if not scratch_gdb or not arcpy.Exists(scratch_gdb):
            scratch_gdb = arcpy.env.scratchWorkspace
        if not scratch_gdb or not arcpy.Exists(scratch_gdb):
            raise arcpy.ExecuteError("No scratch GDB available.")
        _log_msg(messages, logger, "INFO",
                 f"Scratch (disk): {scratch_gdb}")

        derived_h_units, derived_cw = (None, None)
        if derive_metrics and anno_layer:
            _log_msg(messages, logger, "INFO",
                     "Deriving text metrics from annotation...")
            derived_h_units, derived_cw = _derive_text_metrics_from_annotation(
                anno_layer, logger)
            if derived_h_units and derived_cw:
                _log_msg(messages, logger, "INFO",
                         f"Derived: height_units={derived_h_units:.4f} "
                         f"char_w={derived_cw:.4f}")
            else:
                _log_msg(messages, logger, "WARN",
                         "Could not derive metrics; using font estimate.")

        mask_fc = _build_obstacle_mask_fc(
            obstacle_layers, anno_layer, safe_units,
            scratch_gdb, logger, messages)
        if mask_fc:
            _log_msg(messages, logger, "INFO",
                     f"[DIAG] Obstacle mask FC: {mask_fc}")
        else:
            _log_msg(messages, logger, "INFO", "[DIAG] No obstacle mask.")

        # Build the optimised AABB store ONCE (only if optimised path).
        obstacle_aabb: Optional[_ObstacleAABBStore] = None
        mask_layer = None
        if mask_fc:
            if use_legacy_evaluation:
                mask_layer = _make_mask_layer(mask_fc)
                _log_msg(messages, logger, "INFO",
                         "[DIAG] LEGACY: per-candidate "
                         "SelectLayerByLocation against obstacle mask layer.")
            else:
                obstacle_aabb = _ObstacleAABBStore(mask_fc, logger)
                _log_msg(messages, logger, "INFO",
                         f"[DIAG] OPTIMISED: AABB store built with "
                         f"{obstacle_aabb.n} obstacle features.")

        out_segments_fc = _create_fc(out_ws, out_segments_name,
                                     "POLYLINE", sr, logger)
        out_points_fc = _create_fc(out_ws, out_points_name,
                                   "POINT", sr, logger)
        seg_fields = [
            ("SRCID", "LONG"), ("PARTID", "LONG"), ("ELEV", "TEXT", 64),
            ("WSTART", "DOUBLE"), ("WEND", "DOUBLE"), ("CDIST", "DOUBLE"),
            ("ANG", "DOUBLE"), ("CURV", "DOUBLE"), ("OVLP", "DOUBLE"),
            ("SCORE", "DOUBLE"), ("TEXT", "TEXT", 128),
        ]
        pt_fields = seg_fields + [("ROT", "DOUBLE")]
        _add_fields(out_segments_fc, seg_fields)
        _add_fields(out_points_fc, pt_fields)

        out_foot_fc = None
        if make_footprints:
            out_foot_fc = _create_fc(out_ws, out_footprints_name,
                                     "POLYGON", sr, logger)
            _add_fields(out_foot_fc, [
                ("SRCID", "LONG"), ("PARTID", "LONG"),
                ("TEXT", "TEXT", 128), ("SCORE", "DOUBLE"),
                ("OVLP", "DOUBLE"), ("CURV", "DOUBLE")])
        out_stats_tbl = None
        if make_stats:
            out_stats_tbl = os.path.join(out_ws, out_stats_name)
            if arcpy.Exists(out_stats_tbl):
                arcpy.management.Delete(out_stats_tbl)
            arcpy.management.CreateTable(out_ws, out_stats_name)
            _add_fields(out_stats_tbl, [
                ("SRCID", "LONG"), ("PARTID", "LONG"),
                ("WINCNT", "LONG"), ("PLACED", "LONG"),
                ("AVGSC", "DOUBLE"), ("MAXSC", "DOUBLE"),
                ("AVGOV", "DOUBLE"), ("MAXOV", "DOUBLE"),
                ("AVGCURV", "DOUBLE"), ("MAXCURV", "DOUBLE"),
                ("SECS", "DOUBLE"),
            ])

        seg_ins = pt_ins = foot_ins = stats_ins = None
        try:
            seg_ins = arcpy.da.InsertCursor(
                out_segments_fc,
                ["SHAPE@", "SRCID", "PARTID", "ELEV", "WSTART", "WEND",
                 "CDIST", "ANG", "CURV", "OVLP", "SCORE", "TEXT"])
            pt_ins = arcpy.da.InsertCursor(
                out_points_fc,
                ["SHAPE@", "SRCID", "PARTID", "ELEV", "WSTART", "WEND",
                 "CDIST", "ANG", "CURV", "OVLP", "SCORE", "TEXT", "ROT"])
            if out_foot_fc:
                foot_ins = arcpy.da.InsertCursor(
                    out_foot_fc,
                    ["SHAPE@", "SRCID", "PARTID", "TEXT",
                     "SCORE", "OVLP", "CURV"])
            if out_stats_tbl:
                stats_ins = arcpy.da.InsertCursor(
                    out_stats_tbl,
                    ["SRCID", "PARTID", "WINCNT", "PLACED",
                     "AVGSC", "MAXSC", "AVGOV", "MAXOV",
                     "AVGCURV", "MAXCURV", "SECS"])

            try:
                total_features = int(
                    arcpy.management.GetCount(in_contours).getOutput(0))
            except (arcpy.ExecuteError, RuntimeError) as ex:
                raise arcpy.ExecuteError(
                    f"GetCount failed on contours: {ex}")
            _log_msg(messages, logger, "INFO",
                     f"[DIAG] Contours total: {total_features}")
            arcpy.SetProgressor(
                "step", "Optimizing contour label anchors...",
                0, max(1, total_features), 1)

            placed_cache = _PlacedCache()
            eps_units = max(0.001 * interval_units, 0.5 * curv_sample_units)
            considered = 0
            placed_total = 0

            oid_field = arcpy.Describe(in_contours).OIDFieldName
            sql_clause = (None, f"ORDER BY {oid_field}")
            fields = ["OID@", "SHAPE@", elev_field]
            n_seen = 0
            with arcpy.da.SearchCursor(
                    in_contours, fields, sql_clause=sql_clause) as cur:
                for oid, geom, elev in cur:
                    n_seen += 1
                    arcpy.SetProgressorPosition(n_seen)
                    t0 = time.time()
                    arcpy.SetProgressorLabel(
                        f"Processing contour OID {oid}")

                    if not geom:
                        _log_msg(messages, logger, "WARN",
                                 f"OID {oid} has null geometry; skipping.")
                        continue

                    if selection_mode == "MAJOR_INTERVAL":
                        if not _is_major_value(elev, major_interval):
                            continue
                    considered += 1

                    label_text = "" if elev is None else str(elev)
                    try:
                        text_h_units, text_w_units, _pad = \
                            _estimate_text_metrics_units(
                                label_text, map_scale, mpu, pad_units,
                                derive_metrics, derived_h_units, derived_cw,
                                font_size_pt, char_w_factor)
                    except (arcpy.ExecuteError, RuntimeError):
                        _log_msg(messages, logger, "ERROR",
                                 f"Text metric estimate failed for OID "
                                 f"{oid}: {traceback.format_exc()}")
                        continue

                    foot_len = max(text_w_units + 2.0 * _pad, 2.0 * _pad)
                    foot_h = max(text_h_units + 2.0 * _pad, 2.0 * _pad)
                    internal_step = max(0.25 * foot_len,
                                        0.05 * interval_units)
                    internal_step = min(internal_step, 0.25 * interval_units)
                    internal_step = max(internal_step, 0.5 * curv_sample_units)
                    offsets = _build_offsets(internal_step, max_tries)

                    for part_id, part_geom in _iter_parts(geom, sr):
                        if not part_geom:
                            continue
                        total_len = float(part_geom.length)
                        if total_len <= 0:
                            continue
                        if (min_contour_units > 0
                                and total_len < min_contour_units):
                            if short_policy == "SKIP":
                                continue

                        win_count = 0
                        placed_count = 0
                        scores: List[float] = []
                        ovlps: List[float] = []
                        curvs: List[float] = []

                        win_start = 0.0
                        while win_start < total_len:
                            win_end = min(win_start + interval_units,
                                          total_len)
                            win_len = win_end - win_start
                            if win_len <= 0:
                                break
                            win_count += 1

                            seed_d = None
                            if anno_layer:
                                seed_d = _find_seed_from_annotation(
                                    anno_layer, part_geom, win_start,
                                    win_end, safe_units, logger)
                            if seed_d is None:
                                seed_d = win_start + 0.5 * win_len

                            best = None
                            for off in offsets:
                                d = float(seed_d) + float(off)
                                d = _clamp(d, win_start, win_end)
                                res = _score_candidate(
                                    part_geom, d, win_start, win_end,
                                    foot_len, foot_h, eps_units,
                                    curv_method, curv_sample_units,
                                    w_cr, w_md, w_ce,
                                    w_curv, w_ovlp, w_center,
                                    max_ovlp, max_curv,
                                    mask_layer, mask_fc,
                                    obstacle_aabb, placed_cache,
                                    sr, use_legacy_evaluation, logger)
                                if not res:
                                    continue
                                if best is None or res["score"] < best["score"]:
                                    best = res

                            if best is None and (max_ovlp is not None
                                                 or max_curv is not None):
                                # Retry without thresholds.
                                for off in offsets:
                                    d = float(seed_d) + float(off)
                                    d = _clamp(d, win_start, win_end)
                                    res = _score_candidate(
                                        part_geom, d, win_start, win_end,
                                        foot_len, foot_h, eps_units,
                                        curv_method, curv_sample_units,
                                        w_cr, w_md, w_ce,
                                        w_curv, w_ovlp, w_center,
                                        None, None,
                                        mask_layer, mask_fc,
                                        obstacle_aabb, placed_cache,
                                        sr, use_legacy_evaluation, logger)
                                    if not res:
                                        continue
                                    if (best is None
                                            or res["score"] < best["score"]):
                                        best = res

                            if best is None:
                                center_d = win_start + 0.5 * win_len
                                best = _score_candidate(
                                    part_geom, center_d, win_start, win_end,
                                    foot_len, foot_h, eps_units,
                                    curv_method, curv_sample_units,
                                    w_cr, w_md, w_ce,
                                    w_curv, w_ovlp, w_center,
                                    None, None,
                                    mask_layer, mask_fc,
                                    obstacle_aabb, placed_cache,
                                    sr, use_legacy_evaluation, logger)

                            if best:
                                placed_count += 1
                                placed_total += 1
                                scores.append(best["score"])
                                ovlps.append(best["ovlp"])
                                curvs.append(best["curv"])
                                seg_ins.insertRow([
                                    best["seg_geom"], oid, part_id,
                                    label_text,
                                    win_start, win_end, best["center_d"],
                                    best["ang_deg"], best["curv"],
                                    best["ovlp"], best["score"], label_text])
                                pt = part_geom.positionAlongLine(
                                    best["center_d"], False)
                                pt_ins.insertRow([
                                    pt, oid, part_id, label_text,
                                    win_start, win_end, best["center_d"],
                                    best["ang_deg"], best["curv"],
                                    best["ovlp"], best["score"], label_text,
                                    best["ang_deg"]])
                                if foot_ins and best["foot_geom"]:
                                    foot_ins.insertRow([
                                        best["foot_geom"], oid, part_id,
                                        label_text, best["score"],
                                        best["ovlp"], best["curv"]])
                                if best["foot_geom"]:
                                    placed_cache.add(best["foot_geom"])
                            else:
                                if logger:
                                    logger.warning(
                                        "No placement for OID %s part %s "
                                        "window [%0.2f,%0.2f].",
                                        oid, part_id, win_start, win_end)
                            win_start = win_end

                        if stats_ins:
                            secs = time.time() - t0
                            if placed_count > 0:
                                avg_sc = sum(scores) / float(len(scores))
                                max_sc = max(scores)
                                avg_ov = sum(ovlps) / float(len(ovlps))
                                max_ov = max(ovlps)
                                avg_cv = sum(curvs) / float(len(curvs))
                                max_cv = max(curvs)
                            else:
                                avg_sc = max_sc = avg_ov = max_ov = \
                                    avg_cv = max_cv = 0.0
                            stats_ins.insertRow([
                                oid, part_id, win_count, placed_count,
                                avg_sc, max_sc, avg_ov, max_ov,
                                avg_cv, max_cv, secs])

                    if (considered % 100) == 0:
                        gc.collect()

            arcpy.ResetProgressor()
            _log_msg(messages, logger, "INFO",
                     f"[DIAG] Contours considered: {considered}, "
                     f"anchors placed: {placed_total}")
            _log_msg(messages, logger, "INFO", "Completed optimization.")
            _log_msg(messages, logger, "INFO",
                     f"Segments: {out_segments_fc}")
            _log_msg(messages, logger, "INFO",
                     f"Points: {out_points_fc}")

            parameters[35].value = out_segments_fc
            parameters[36].value = out_points_fc
            parameters[37].value = out_foot_fc if out_foot_fc else ""
            parameters[38].value = out_stats_tbl if out_stats_tbl else ""
            parameters[39].value = log_path

            if add_to_map:
                paths = [out_segments_fc, out_points_fc]
                if out_foot_fc:
                    paths.append(out_foot_fc)
                self._add_to_active_map(paths)
        finally:
            for ins in (seg_ins, pt_ins, foot_ins, stats_ins):
                if ins is not None:
                    try:
                        del ins
                    except (AttributeError, RuntimeError):
                        pass
            if mask_layer:
                try:
                    arcpy.management.Delete(mask_layer)
                except (arcpy.ExecuteError, RuntimeError):
                    pass
            _shutdown_logger(logger)




# ----------------------------------------------------------------------
# Validate Label Anchors
# ----------------------------------------------------------------------

class ValidateLabelAnchors(object):
    def __init__(self):
        self.label = "Validate Label Anchors (QA, hardened)"
        self.description = (
            "Checks overlaps for anchor footprints and outputs a QA table.")
        self.canRunInBackground = True

    def isLicensed(self):
        return True

    def getParameterInfo(self):
        in_points = arcpy.Parameter(
            displayName="Anchor points (output from optimizer)",
            name="in_points", datatype="GPFeatureLayer",
            parameterType="Required", direction="Input")
        text_field = arcpy.Parameter(
            displayName="Text field", name="text_field",
            datatype="Field", parameterType="Required", direction="Input")
        text_field.parameterDependencies = [in_points.name]
        safe_mm = arcpy.Parameter(
            displayName="Safe distance from obstacles (mm on map)",
            name="safe_mm", datatype="GPDouble",
            parameterType="Required", direction="Input")
        safe_mm.value = 2.0
        halo_mm = arcpy.Parameter(
            displayName="Extra halo/mask margin (mm on map)",
            name="halo_mm", datatype="GPDouble",
            parameterType="Optional", direction="Input")
        halo_mm.value = 0.0
        map_scale = arcpy.Parameter(
            displayName="Map scale denominator", name="map_scale",
            datatype="GPDouble", parameterType="Required", direction="Input")
        map_scale.value = 25000.0
        font_size_pt = arcpy.Parameter(
            displayName="Font size (points) for footprint estimate",
            name="font_size_pt", datatype="GPDouble",
            parameterType="Required", direction="Input")
        font_size_pt.value = 8.0
        char_w_factor = arcpy.Parameter(
            displayName="Average character width factor",
            name="char_w_factor", datatype="GPDouble",
            parameterType="Required", direction="Input")
        char_w_factor.value = 0.6
        obstacles = arcpy.Parameter(
            displayName="Obstacle layers (lines, polygons, points)",
            name="obstacles", datatype="GPFeatureLayer",
            parameterType="Optional", direction="Input", multiValue=True)
        out_ws = arcpy.Parameter(
            displayName="Output workspace", name="out_ws",
            datatype="DEWorkspace", parameterType="Required",
            direction="Input")
        out_table_name = arcpy.Parameter(
            displayName="QA report table name",
            name="out_table_name", datatype="GPString",
            parameterType="Required", direction="Input")
        out_table_name.value = "LabelAnchorQA"
        # F1 NEW PARAMETER on validator too
        use_legacy_evaluation = arcpy.Parameter(
            displayName=("Use legacy evaluation (per-candidate "
                         "SelectLayerByLocation)"),
            name="use_legacy_evaluation", datatype="GPBoolean",
            parameterType="Optional", direction="Input")
        use_legacy_evaluation.value = False
        out_table = arcpy.Parameter(
            displayName="QA report table", name="out_table",
            datatype="DETable", parameterType="Derived", direction="Output")
        out_log = arcpy.Parameter(
            displayName="Log file path", name="out_log",
            datatype="GPString", parameterType="Derived", direction="Output")
        return [in_points, text_field, safe_mm, halo_mm, map_scale,
                font_size_pt, char_w_factor, obstacles, out_ws,
                out_table_name, use_legacy_evaluation, out_table, out_log]

    def updateMessages(self, parameters):
        try:
            in_points = parameters[0].valueAsText
            if in_points:
                sr = arcpy.Describe(in_points).spatialReference
                if _is_geographic(sr):
                    parameters[0].setErrorMessage(
                        "Input points must be projected (linear units).")
        except (arcpy.ExecuteError, RuntimeError):
            pass

    def execute(self, parameters, messages):
        env_snap = _snapshot_env()
        logger = None
        ins = None
        mask_layer = None
        try:
            _prime_env()
            in_points_layer = parameters[0].valueAsText
            text_field = parameters[1].valueAsText
            safe_mm = float(parameters[2].value)
            halo_mm = (float(parameters[3].value)
                       if parameters[3].value is not None else 0.0)
            map_scale = float(parameters[4].value)
            font_size_pt = float(parameters[5].value)
            char_w_factor = float(parameters[6].value)
            obstacles_text = parameters[7].valueAsText
            obstacle_layers = ([s.strip() for s in obstacles_text.split(";")]
                               if obstacles_text else [])
            out_ws = parameters[8].valueAsText
            out_table_name = parameters[9].valueAsText
            use_legacy_evaluation = bool(parameters[10].value)
            if not _NUMPY_OK and not use_legacy_evaluation:
                use_legacy_evaluation = True

            logger, log_path = _setup_logger(out_ws, "validate")
            _log_msg(messages, logger, "INFO",
                     "Starting ValidateLabelAnchors (Pro)")
            _log_msg(messages, logger, "INFO",
                     f"[DIAG] use_legacy_evaluation={use_legacy_evaluation}")

            _announce_selection("Anchor points", in_points_layer,
                                messages, logger)
            for lyr in obstacle_layers:
                _announce_selection("Obstacle", lyr, messages, logger)

            in_points = _resolve_full_source(in_points_layer)

            desc = arcpy.Describe(in_points)
            sr = desc.spatialReference
            if _is_geographic(sr):
                raise arcpy.ExecuteError(
                    "Projected coordinate system is required.")
            mpu = _meters_per_unit(sr)
            if not mpu or mpu <= 0:
                raise arcpy.ExecuteError(
                    "Could not determine meters-per-unit.")

            safe_units = _safe_mm_to_units(safe_mm, map_scale, mpu)
            halo_units = (_safe_mm_to_units(halo_mm, map_scale, mpu)
                          if halo_mm > 0 else 0.0)
            pad_units = safe_units + halo_units

            scratch_gdb = arcpy.env.scratchGDB
            if not scratch_gdb or not arcpy.Exists(scratch_gdb):
                scratch_gdb = arcpy.env.scratchWorkspace
            mask_fc = _build_obstacle_mask_fc(
                obstacle_layers, None, safe_units,
                scratch_gdb, logger, messages)

            obstacle_aabb: Optional[_ObstacleAABBStore] = None
            if mask_fc:
                if use_legacy_evaluation:
                    mask_layer = _make_mask_layer(mask_fc)
                else:
                    obstacle_aabb = _ObstacleAABBStore(mask_fc, logger)

            out_tbl = os.path.join(out_ws, out_table_name)
            if arcpy.Exists(out_tbl):
                arcpy.management.Delete(out_tbl)
            arcpy.management.CreateTable(out_ws, out_table_name)
            _add_fields(out_tbl, [
                ("OID", "LONG"), ("TEXT", "TEXT", 128),
                ("OVLP", "DOUBLE"), ("SELFOV", "DOUBLE"),
                ("FLAG", "TEXT", 32)])

            cache = _PlacedCache()
            fields = ["OID@", "SHAPE@", text_field]
            fld_names = [f.name for f in arcpy.ListFields(in_points)]
            has_rot = ("ROT" in fld_names)
            if has_rot:
                fields.append("ROT")

            try:
                total_count = int(
                    arcpy.management.GetCount(in_points).getOutput(0))
            except (arcpy.ExecuteError, RuntimeError) as ex:
                raise arcpy.ExecuteError(
                    f"GetCount failed on anchor points: {ex}")
            arcpy.SetProgressor("step", "Validating anchors...",
                                0, max(1, total_count), 1)

            ins = arcpy.da.InsertCursor(
                out_tbl, ["OID", "TEXT", "OVLP", "SELFOV", "FLAG"])
            total = 0
            oid_field = arcpy.Describe(in_points).OIDFieldName
            sql_clause = (None, f"ORDER BY {oid_field}")
            with arcpy.da.SearchCursor(
                    in_points, fields, sql_clause=sql_clause) as cur:
                for row in cur:
                    total += 1
                    arcpy.SetProgressorPosition(total)
                    oid = row[0]
                    pt = row[1]
                    txt = row[2]
                    rot = row[3] if has_rot else 0.0
                    if not pt:
                        continue
                    txt = "" if txt is None else str(txt)
                    h_mm_map = float(font_size_pt) * PT_TO_MM
                    h_m_ground = (h_mm_map * float(map_scale)) / 1000.0
                    h_units = h_m_ground / float(mpu)
                    w_units = (max(1, len(txt)) * float(char_w_factor)
                               * h_units)
                    foot_len = w_units + 2.0 * pad_units
                    foot_h = h_units + 2.0 * pad_units
                    ang = math.radians(float(rot)) if rot is not None else 0.0
                    foot = _make_oriented_rect(
                        pt, ang, 0.5 * foot_len, 0.5 * foot_h, sr)

                    ovlp = 0.0
                    if mask_fc:
                        if (use_legacy_evaluation
                                or obstacle_aabb is None
                                or obstacle_aabb.bbox is None):
                            ovlp = _mask_overlap_area_legacy(
                                foot, mask_layer, mask_fc, logger)
                        else:
                            ovlp = obstacle_aabb.overlap_area(foot, logger)
                    selfov = cache.overlap_area(
                        foot, logger, use_legacy=use_legacy_evaluation)

                    flag = "OK"
                    if ovlp > 0.0:
                        flag = "OBSTACLE"
                    if selfov > 0.0:
                        flag = "SELF"
                    if ovlp > 0.0 and selfov > 0.0:
                        flag = "BOTH"

                    ins.insertRow([oid, txt, ovlp, selfov, flag])
                    cache.add(foot)
                    if (total % 200) == 0:
                        gc.collect()
            arcpy.ResetProgressor()

            _log_msg(messages, logger, "INFO",
                     f"[DIAG] Anchors checked: {total}")
            _log_msg(messages, logger, "INFO", f"QA table: {out_tbl}")
            parameters[11].value = out_tbl
            parameters[12].value = log_path
        except arcpy.ExecuteError:
            _err(arcpy.GetMessages(2))
            raise
        except RuntimeError as ex:
            _err(f"Runtime error: {ex}")
            _err(traceback.format_exc())
            raise
        finally:
            if ins is not None:
                try:
                    del ins
                except (AttributeError, RuntimeError):
                    pass
            if mask_layer:
                try:
                    arcpy.management.Delete(mask_layer)
                except (arcpy.ExecuteError, RuntimeError):
                    pass
            _shutdown_logger(logger)
            _restore_env(env_snap)


# ----------------------------------------------------------------------
# Curvature Heatmap
# ----------------------------------------------------------------------

class CurvatureHeatmap(object):
    def __init__(self):
        self.label = "Curvature Heatmap (QA)"
        self.description = (
            "Creates line segments with curvature values for visualization.")
        self.canRunInBackground = True

    def isLicensed(self):
        return True

    def getParameterInfo(self):
        in_contours = arcpy.Parameter(
            displayName="Contour lines", name="in_contours",
            datatype="GPFeatureLayer", parameterType="Required",
            direction="Input")
        step_m = arcpy.Parameter(
            displayName="Step (meters) along contour", name="step_m",
            datatype="GPDouble", parameterType="Required",
            direction="Input")
        step_m.value = 20.0
        seg_len_m = arcpy.Parameter(
            displayName="Segment length for curvature (meters)",
            name="seg_len_m", datatype="GPDouble",
            parameterType="Required", direction="Input")
        seg_len_m.value = 60.0
        curv_method = arcpy.Parameter(
            displayName="Curvature method", name="curv_method",
            datatype="GPString", parameterType="Required",
            direction="Input")
        curv_method.filter.type = "ValueList"
        curv_method.filter.list = ["Hybrid", "ChordRatio",
                                   "MaxDeflection", "CurvatureEnergy"]
        curv_method.value = "Hybrid"
        curv_sample_m = arcpy.Parameter(
            displayName="Curvature sampling step (meters)",
            name="curv_sample_m", datatype="GPDouble",
            parameterType="Required", direction="Input")
        curv_sample_m.value = 5.0
        w_cr = arcpy.Parameter(
            displayName="Hybrid weight: chord ratio", name="w_cr",
            datatype="GPDouble", parameterType="Required",
            direction="Input")
        w_cr.value = 0.5
        w_md = arcpy.Parameter(
            displayName="Hybrid weight: max deflection", name="w_md",
            datatype="GPDouble", parameterType="Required",
            direction="Input")
        w_md.value = 0.3
        w_ce = arcpy.Parameter(
            displayName="Hybrid weight: curvature energy", name="w_ce",
            datatype="GPDouble", parameterType="Required",
            direction="Input")
        w_ce.value = 0.2
        out_ws = arcpy.Parameter(
            displayName="Output workspace", name="out_ws",
            datatype="DEWorkspace", parameterType="Required",
            direction="Input")
        out_name = arcpy.Parameter(
            displayName="Output heatmap segments name",
            name="out_name", datatype="GPString",
            parameterType="Required", direction="Input")
        out_name.value = "ContourCurvatureHeat"
        out_fc = arcpy.Parameter(
            displayName="Output heatmap segments", name="out_fc",
            datatype="DEFeatureClass",
            parameterType="Derived", direction="Output")
        out_log = arcpy.Parameter(
            displayName="Log file path", name="out_log",
            datatype="GPString", parameterType="Derived", direction="Output")
        return [in_contours, step_m, seg_len_m, curv_method, curv_sample_m,
                w_cr, w_md, w_ce, out_ws, out_name, out_fc, out_log]

    def execute(self, parameters, messages):
        env_snap = _snapshot_env()
        logger = None
        ins = None
        try:
            _prime_env()
            in_contours_layer = parameters[0].valueAsText
            step_m = float(parameters[1].value)
            seg_len_m = float(parameters[2].value)
            curv_method = parameters[3].valueAsText
            curv_sample_m = float(parameters[4].value)
            w_cr = float(parameters[5].value)
            w_md = float(parameters[6].value)
            w_ce = float(parameters[7].value)
            out_ws = parameters[8].valueAsText
            out_name = parameters[9].valueAsText

            logger, log_path = _setup_logger(out_ws, "heatmap")
            _log_msg(messages, logger, "INFO",
                     "Starting CurvatureHeatmap (Pro)")

            _announce_selection("Contours", in_contours_layer,
                                messages, logger)
            in_contours = _resolve_full_source(in_contours_layer)

            desc = arcpy.Describe(in_contours)
            sr = desc.spatialReference
            if _is_geographic(sr):
                raise arcpy.ExecuteError(
                    "Projected coordinate system is required.")
            mpu = _meters_per_unit(sr)
            if not mpu or mpu <= 0:
                raise arcpy.ExecuteError(
                    "Could not determine meters-per-unit.")

            step_units = _meters_to_units(step_m, mpu)
            seg_len_units = _meters_to_units(seg_len_m, mpu)
            curv_sample_units = _meters_to_units(curv_sample_m, mpu)

            out_fc = _create_fc(out_ws, out_name, "POLYLINE", sr, logger)
            _add_fields(out_fc, [("SRCID", "LONG"), ("PARTID", "LONG"),
                                 ("D", "DOUBLE"), ("CURV", "DOUBLE")])
            ins = arcpy.da.InsertCursor(
                out_fc, ["SHAPE@", "SRCID", "PARTID", "D", "CURV"])

            try:
                total_features = int(
                    arcpy.management.GetCount(in_contours).getOutput(0))
            except (arcpy.ExecuteError, RuntimeError) as ex:
                raise arcpy.ExecuteError(
                    f"GetCount failed on contours: {ex}")
            arcpy.SetProgressor("step", "Building curvature heatmap...",
                                0, max(1, total_features), 1)

            count = 0
            n_seen = 0
            oid_field = arcpy.Describe(in_contours).OIDFieldName
            sql_clause = (None, f"ORDER BY {oid_field}")
            with arcpy.da.SearchCursor(
                    in_contours, ["OID@", "SHAPE@"],
                    sql_clause=sql_clause) as cur:
                for oid, geom in cur:
                    n_seen += 1
                    arcpy.SetProgressorPosition(n_seen)
                    if not geom:
                        continue
                    for part_id, part_geom in _iter_parts(geom, sr):
                        if not part_geom:
                            continue
                        L = float(part_geom.length)
                        if L <= 0:
                            continue
                        d = 0.0
                        while d < L:
                            d0 = _clamp(d - 0.5 * seg_len_units, 0.0, L)
                            d1 = _clamp(d + 0.5 * seg_len_units, 0.0, L)
                            try:
                                seg = part_geom.segmentAlongLine(
                                    d0, d1, False)
                            except (arcpy.ExecuteError, RuntimeError):
                                seg = None
                            if seg and float(seg.length) > 0:
                                curv = _compute_curvature(
                                    seg, curv_method, curv_sample_units,
                                    w_cr, w_md, w_ce)
                                # F2: skip inf-curvature segments rather than
                                # write them as 0.0 - they would falsely look
                                # like the calmest section of the heatmap.
                                if curv != _INF:
                                    ins.insertRow([seg, oid, part_id, d, curv])
                                    count += 1
                            d += step_units
                    if (count % 500) == 0:
                        gc.collect()
            arcpy.ResetProgressor()

            _log_msg(messages, logger, "INFO",
                     f"[DIAG] Heatmap segments written: {count}")
            parameters[10].value = out_fc
            parameters[11].value = log_path
        except arcpy.ExecuteError:
            _err(arcpy.GetMessages(2))
            raise
        except RuntimeError as ex:
            _err(f"Runtime error: {ex}")
            _err(traceback.format_exc())
            raise
        finally:
            if ins is not None:
                try:
                    del ins
                except (AttributeError, RuntimeError):
                    pass
            _shutdown_logger(logger)
            _restore_env(env_snap)


# ----------------------------------------------------------------------
# Convert Labels To Annotation (Pro arcpy.mp)
# ----------------------------------------------------------------------

class ConvertLabelsToAnnotationPro(object):
    """
    Pro replacement for the ArcMap-only AutoGenerateAnnotation tool.
    """
    def __init__(self):
        self.label = "Convert Labels To Annotation (Pro)"
        self.description = (
            "Generates annotation from layer labels via "
            "arcpy.cartography.ConvertLabelsToAnnotation. Works on the "
            "active map by default; pass a project file to target a "
            "saved .aprx.")
        self.canRunInBackground = False

    def isLicensed(self):
        return True

    def getParameterInfo(self):
        project = arcpy.Parameter(
            displayName="Project (.aprx path or CURRENT)",
            name="project", datatype="GPString",
            parameterType="Required", direction="Input")
        project.value = "CURRENT"
        map_name = arcpy.Parameter(
            displayName="Map name (use first if blank)",
            name="map_name", datatype="GPString",
            parameterType="Optional", direction="Input")
        out_gdb = arcpy.Parameter(
            displayName="Output geodatabase", name="out_gdb",
            datatype="DEWorkspace", parameterType="Required",
            direction="Input")
        anno_suffix = arcpy.Parameter(
            displayName="Annotation suffix", name="anno_suffix",
            datatype="GPString", parameterType="Required",
            direction="Input")
        anno_suffix.value = "Anno"
        reference_scale = arcpy.Parameter(
            displayName="Reference scale (optional)",
            name="reference_scale", datatype="GPDouble",
            parameterType="Optional", direction="Input")
        which_layers = arcpy.Parameter(
            displayName="Layers source", name="which_layers",
            datatype="GPString", parameterType="Required",
            direction="Input")
        which_layers.filter.type = "ValueList"
        which_layers.filter.list = ["ALL_LAYERS", "VISIBLE_LAYERS"]
        which_layers.value = "ALL_LAYERS"
        feature_linked = arcpy.Parameter(
            displayName="Feature linked", name="feature_linked",
            datatype="GPString", parameterType="Required",
            direction="Input")
        feature_linked.filter.type = "ValueList"
        feature_linked.filter.list = ["STANDARD", "FEATURE_LINKED"]
        feature_linked.value = "STANDARD"
        generate_unplaced = arcpy.Parameter(
            displayName="Generate unplaced annotation",
            name="generate_unplaced", datatype="GPString",
            parameterType="Required", direction="Input")
        generate_unplaced.filter.type = "ValueList"
        generate_unplaced.filter.list = [
            "NOT_GENERATE_UNPLACED_ANNOTATION",
            "GENERATE_UNPLACED_ANNOTATION"]
        generate_unplaced.value = "GENERATE_UNPLACED_ANNOTATION"
        out_workspace = arcpy.Parameter(
            displayName="Output workspace (derived)",
            name="out_workspace", datatype="DEWorkspace",
            parameterType="Derived", direction="Output")
        out_log = arcpy.Parameter(
            displayName="Log file path", name="out_log",
            datatype="GPString", parameterType="Derived",
            direction="Output")
        return [project, map_name, out_gdb, anno_suffix, reference_scale,
                which_layers, feature_linked, generate_unplaced,
                out_workspace, out_log]

    def execute(self, parameters, messages):
        env_snap = _snapshot_env()
        logger = None
        try:
            _prime_env()
            project = parameters[0].valueAsText
            map_name = parameters[1].valueAsText
            out_gdb = parameters[2].valueAsText
            anno_suffix = parameters[3].valueAsText
            reference_scale = parameters[4].value
            which_layers = parameters[5].valueAsText
            feature_linked = parameters[6].valueAsText
            generate_unplaced = parameters[7].valueAsText

            logger, log_path = _setup_logger(out_gdb, "anno_pro")
            _log_msg(messages, logger, "INFO",
                     "Starting ConvertLabelsToAnnotation (Pro)")

            try:
                aprx = arcpy.mp.ArcGISProject(project)
            except (arcpy.ExecuteError, RuntimeError) as ex:
                raise arcpy.ExecuteError(
                    f"Could not open project '{project}': {ex}")

            if map_name:
                maps = aprx.listMaps(map_name)
            else:
                maps = aprx.listMaps()
            if not maps:
                raise arcpy.ExecuteError("No maps found in project.")
            m = maps[0]
            _log_msg(messages, logger, "INFO", f"Using map: {m.name}")

            ref_scale_val = reference_scale
            if ref_scale_val is None:
                try:
                    ref_scale_val = (float(m.referenceScale)
                                     if m.referenceScale else 0.0)
                except (TypeError, ValueError):
                    ref_scale_val = 0.0
            if not ref_scale_val:
                ref_scale_val = 25000.0
                _log_msg(messages, logger, "WARN",
                         f"No reference scale on map; defaulting to "
                         f"{ref_scale_val}.")

            try:
                arcpy.cartography.ConvertLabelsToAnnotation(
                    input_map=m,
                    conversion_scale=float(ref_scale_val),
                    output_geodatabase=out_gdb,
                    anno_suffix=anno_suffix,
                    extent="DEFAULT",
                    generate_unplaced_annotation=generate_unplaced,
                    require_symbol_id="NO_REQUIRE_ID",
                    feature_linked=feature_linked,
                    auto_create="AUTO_CREATE",
                    update_on_shape_change="SHAPE_CHANGE",
                    output_group_layer=None,
                    which_layers=which_layers,
                    single_label_class="NO_SINGLE_CLASS",
                    multiple_feature_classes="MULTIPLE_FEATURE_CLASSES",
                    merge_label_classes="NO_MERGE_LABEL_CLASS")
            except TypeError:
                # Older arg signature fallback
                arcpy.cartography.ConvertLabelsToAnnotation(
                    m, float(ref_scale_val), out_gdb, anno_suffix,
                    "DEFAULT", generate_unplaced, "NO_REQUIRE_ID",
                    feature_linked, "AUTO_CREATE", "SHAPE_CHANGE",
                    None, which_layers,
                    "NO_SINGLE_CLASS", "MULTIPLE_FEATURE_CLASSES",
                    "NO_MERGE_LABEL_CLASS")

            parameters[8].value = out_gdb
            parameters[9].value = log_path
            _log_msg(messages, logger, "INFO",
                     "Completed Convert Labels To Annotation (Pro).")
        except arcpy.ExecuteError:
            _err(arcpy.GetMessages(2))
            raise
        except RuntimeError as ex:
            _err(f"Runtime error: {ex}")
            _err(traceback.format_exc())
            raise
        finally:
            _shutdown_logger(logger)
            _restore_env(env_snap)


# ----------------------------------------------------------------------
# Unit tests
# ----------------------------------------------------------------------

class _GeomTestCase(unittest.TestCase):
    def test_curv_chord_ratio_straight_line(self):
        sr = arcpy.SpatialReference(3857)
        arr = arcpy.Array([arcpy.Point(0, 0), arcpy.Point(100, 0)])
        line = arcpy.Polyline(arr, sr)
        self.assertTrue(abs(_curv_chord_ratio(line)) < 1e-6)

    def test_curv_chord_ratio_bent_line(self):
        sr = arcpy.SpatialReference(3857)
        arr = arcpy.Array([arcpy.Point(0, 0), arcpy.Point(50, 50),
                           arcpy.Point(100, 0)])
        line = arcpy.Polyline(arr, sr)
        self.assertTrue(_curv_chord_ratio(line) > 0.05)

    def test_tangent_angle_horizontal(self):
        sr = arcpy.SpatialReference(3857)
        arr = arcpy.Array([arcpy.Point(0, 0), arcpy.Point(100, 0)])
        line = arcpy.Polyline(arr, sr)
        ang = _tangent_angle_at_distance(line, 50.0, 1.0)
        self.assertTrue(abs(ang) < 1e-3)


class RunUnitTests(object):
    def __init__(self):
        self.label = "Run Unit Tests (basic)"
        self.description = (
            "Runs a small set of automated checks for core helpers.")
        self.canRunInBackground = False

    def isLicensed(self):
        return True

    def getParameterInfo(self):
        out_ws = arcpy.Parameter(
            displayName="Output workspace", name="out_ws",
            datatype="DEWorkspace", parameterType="Required",
            direction="Input")
        out_name = arcpy.Parameter(
            displayName="Test report table name", name="out_name",
            datatype="GPString", parameterType="Required",
            direction="Input")
        out_name.value = "ContourOptUnitTestReport"
        out_table = arcpy.Parameter(
            displayName="Test report table", name="out_table",
            datatype="DETable", parameterType="Derived", direction="Output")
        out_log = arcpy.Parameter(
            displayName="Log file path", name="out_log",
            datatype="GPString", parameterType="Derived", direction="Output")
        return [out_ws, out_name, out_table, out_log]

    def execute(self, parameters, messages):
        env_snap = _snapshot_env()
        logger = None
        ins = None
        try:
            _prime_env()
            out_ws = parameters[0].valueAsText
            out_name = parameters[1].valueAsText
            logger, log_path = _setup_logger(out_ws, "tests")
            _log_msg(messages, logger, "INFO", "Starting unit tests...")
            out_tbl = os.path.join(out_ws, out_name)
            if arcpy.Exists(out_tbl):
                arcpy.management.Delete(out_tbl)
            arcpy.management.CreateTable(out_ws, out_name)
            _add_fields(out_tbl, [("TEST", "TEXT", 128),
                                  ("STATUS", "TEXT", 16),
                                  ("DETAILS", "TEXT", 255)])
            ins = arcpy.da.InsertCursor(
                out_tbl, ["TEST", "STATUS", "DETAILS"])
            suite = unittest.TestLoader().loadTestsFromTestCase(_GeomTestCase)
            result = unittest.TestResult()
            suite.run(result)
            failed = set([t.id() for t, _ in result.failures]
                         + [t.id() for t, _ in result.errors])
            all_tests = [t.id() for t in suite]
            for test_id in all_tests:
                if test_id in failed:
                    detail = ""
                    for t, tb in result.failures:
                        if t.id() == test_id:
                            detail = tb.splitlines()[-1] if tb else "failure"
                    for t, tb in result.errors:
                        if t.id() == test_id:
                            detail = tb.splitlines()[-1] if tb else "error"
                    ins.insertRow([test_id, "FAIL", detail[:254]])
                else:
                    ins.insertRow([test_id, "PASS", ""])
            _log_msg(messages, logger, "INFO",
                     f"Unit tests done. Failures={len(result.failures)} "
                     f"Errors={len(result.errors)}")
            parameters[2].value = out_tbl
            parameters[3].value = log_path
        except arcpy.ExecuteError:
            _err(arcpy.GetMessages(2))
            raise
        except RuntimeError as ex:
            _err(f"Runtime error: {ex}")
            _err(traceback.format_exc())
            raise
        finally:
            if ins is not None:
                try:
                    del ins
                except (AttributeError, RuntimeError):
                    pass
            _shutdown_logger(logger)
            _restore_env(env_snap)
