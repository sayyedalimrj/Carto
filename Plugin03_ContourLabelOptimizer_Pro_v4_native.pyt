# -*- coding: utf-8 -*-
"""
Plugin 03 - Contour Label Optimizer (ArcGIS Pro / Python 3)  v4 NATIVE
=======================================================================
Native Pro rewrite of the Contour Label Optimizer. Same hardening
philosophy as the ArcMap v4 build (selection bypass, scratchGDB-resident
obstacle mask FC with spatial index, AABB-cached self-overlap, narrow
exceptions, [DIAG] logging) but in modern Python 3 with f-strings, type
hints, the arcpy.management/analysis namespaces, and arcpy.mp for the
annotation generator.

Tools:
  - Optimize Contour Label Anchors (v4)
  - Validate Label Anchors (QA)
  - Curvature Heatmap (QA)
  - Convert Labels To Annotation (Pro arcpy.mp)
  - Run Unit Tests (basic)

Author: Ali Mirjafari + Kiro
Version: 4.0 (Pro / Python 3)
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
from typing import Iterable, List, Optional, Sequence, Tuple

import arcpy

PT_TO_MM = 0.3527777778

# =============================================================================
# 0. Messaging
# =============================================================================

def _msg(s: str) -> None:
    try:
        arcpy.AddMessage(str(s))
    except Exception:
        pass

def _warn(s: str) -> None:
    try:
        arcpy.AddWarning(str(s))
    except Exception:
        pass

def _err(s: str) -> None:
    try:
        arcpy.AddError(str(s))
    except Exception:
        pass

def _diag(s: str) -> None:
    _msg(f"[DIAG] {s}")

# =============================================================================
# 1. Logger
# =============================================================================

def _setup_logger(out_ws: str, tool_tag: str):
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    log_dir = out_ws
    try:
        if out_ws and out_ws.lower().endswith(".gdb"):
            log_dir = os.path.dirname(out_ws) or out_ws
    except Exception:
        pass
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
    except Exception:
        log_path = ""
    return logger, log_path

def _shutdown_logger(logger) -> None:
    try:
        if not logger:
            return
        for h in list(logger.handlers):
            try:
                h.flush(); h.close()
            except Exception:
                pass
            try:
                logger.removeHandler(h)
            except Exception:
                pass
    except Exception:
        pass

def _log_msg(messages, logger, level: str, text: str) -> None:
    try:
        if logger:
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
    except Exception:
        pass
    try:
        if messages is not None:
            if level == "ERROR":
                messages.addErrorMessage(text)
            elif level == "WARN":
                messages.addWarningMessage(text)
            else:
                messages.addMessage(text)
        else:
            if level == "ERROR":
                _err(text)
            elif level == "WARN":
                _warn(text)
            else:
                _msg(text)
    except Exception:
        pass

# =============================================================================
# 2. Selection-bypass
# =============================================================================

def _selection_info(layer_or_path) -> Tuple[Optional[int], Optional[int], str]:
    try:
        d = arcpy.Describe(layer_or_path)
    except Exception:
        return (None, None, str(layer_or_path))
    name = getattr(d, "name", str(layer_or_path))
    fidset = getattr(d, "FIDSet", "") or ""
    try:
        total = int(arcpy.management.GetCount(layer_or_path).getOutput(0))
    except Exception:
        total = None
    if not fidset.strip():
        return (0, total, name)
    sel = len([t for t in fidset.split(";") if t.strip() != ""])
    return (sel, total, name)

def _resolve_full_source(layer_or_path):
    if not layer_or_path:
        return layer_or_path
    try:
        d = arcpy.Describe(layer_or_path)
        cp = getattr(d, "catalogPath", None)
        if cp:
            return cp
    except Exception:
        pass
    return layer_or_path

def _announce_selection(label: str, layer_or_path, messages=None, logger=None) -> None:
    sel, total, name = _selection_info(layer_or_path)
    if sel and sel > 0:
        _log_msg(messages, logger, "WARN",
                 f"{label}: '{name}' has an active selection ({sel} of "
                 f"{total if total is not None else '?'}). Ignoring selection - "
                 f"processing FULL dataset.")
    else:
        _log_msg(messages, logger, "INFO",
                 f"[DIAG] {label}: '{name}' total={total if total is not None else '?'}, no active selection.")

# =============================================================================
# 3. SR / unit helpers
# =============================================================================

def _is_geographic(sr) -> bool:
    try:
        return sr is not None and sr.type == "Geographic"
    except Exception:
        return False

def _linear_unit_name(sr) -> str:
    try:
        if sr and hasattr(sr, "linearUnitName"):
            return sr.linearUnitName
    except Exception:
        pass
    return "unknown"

def _meters_per_unit(sr) -> Optional[float]:
    try:
        mpu = float(sr.metersPerUnit)
        if mpu > 0:
            return mpu
    except Exception:
        pass
    return None

def _safe_mm_to_units(mm_on_map: float, map_scale: float, meters_per_unit: float) -> float:
    safe_m = (float(mm_on_map) * float(map_scale)) / 1000.0
    return safe_m / float(meters_per_unit)

def _meters_to_units(meters: float, meters_per_unit: float) -> float:
    return float(meters) / float(meters_per_unit)

def _deg(rad: float) -> float:
    return rad * 180.0 / math.pi

def _clamp(v: float, vmin: float, vmax: float) -> float:
    return max(vmin, min(vmax, v))

def _dist2d(p1, p2) -> float:
    dx = p2.X - p1.X; dy = p2.Y - p1.Y
    return math.sqrt(dx*dx + dy*dy)

# =============================================================================
# 4. Pure geometry helpers
# =============================================================================

def _tangent_angle_at_distance(polyline, dist_along: float, eps_units: float) -> float:
    total = float(polyline.length)
    if total <= 0:
        return 0.0
    d0 = _clamp(dist_along - eps_units, 0.0, total)
    d1 = _clamp(dist_along + eps_units, 0.0, total)
    if abs(d1 - d0) < (eps_units * 0.25):
        d0 = _clamp(dist_along, 0.0, total)
        d1 = _clamp(dist_along + eps_units, 0.0, total)
    p0 = polyline.positionAlongLine(d0, False)
    p1 = polyline.positionAlongLine(d1, False)
    return math.atan2(p1.Y - p0.Y, p1.X - p0.X)

def _make_oriented_rect(center_pt, angle_rad: float, half_len: float, half_h: float, sr):
    ca = math.cos(angle_rad); sa = math.sin(angle_rad)
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
    try:
        L = float(seg.length)
        if L <= 0:
            return 0.0
        p0 = seg.firstPoint; p1 = seg.lastPoint
        C = _dist2d(p0, p1)
        return max(0.0, 1.0 - (C / L))
    except Exception:
        return 0.0

def _curv_max_deflection(seg, sample_units: float) -> float:
    try:
        pts = _sample_points_along(seg, sample_units)
        if len(pts) < 3:
            return 0.0
        mx = 0.0
        for i in range(1, len(pts) - 1):
            a = pts[i-1]; b = pts[i]; c = pts[i+1]
            v1x, v1y = (b.X - a.X), (b.Y - a.Y)
            v2x, v2y = (c.X - b.X), (c.Y - b.Y)
            n1 = math.sqrt(v1x*v1x + v1y*v1y)
            n2 = math.sqrt(v2x*v2x + v2y*v2y)
            if n1 <= 0 or n2 <= 0:
                continue
            dot = (v1x*v2x + v1y*v2y) / (n1 * n2)
            dot = _clamp(dot, -1.0, 1.0)
            ang = math.acos(dot)
            if ang > mx:
                mx = ang
        return mx
    except Exception:
        return 0.0

def _curv_energy(seg, sample_units: float) -> float:
    try:
        pts = _sample_points_along(seg, sample_units)
        if len(pts) < 3:
            return 0.0
        total_turn = 0.0
        for i in range(1, len(pts) - 1):
            a = pts[i-1]; b = pts[i]; c = pts[i+1]
            v1x, v1y = (b.X - a.X), (b.Y - a.Y)
            v2x, v2y = (c.X - b.X), (c.Y - b.Y)
            n1 = math.sqrt(v1x*v1x + v1y*v1y)
            n2 = math.sqrt(v2x*v2x + v2y*v2y)
            if n1 <= 0 or n2 <= 0:
                continue
            dot = (v1x*v2x + v1y*v2y) / (n1 * n2)
            dot = _clamp(dot, -1.0, 1.0)
            ang = math.acos(dot)
            total_turn += abs(ang)
        L = float(seg.length)
        if L <= 0:
            return 0.0
        return total_turn / L
    except Exception:
        return 0.0

def _compute_curvature(seg, method: str, sample_units: float,
                       w_cr: float, w_md: float, w_ce: float) -> float:
    if method == "ChordRatio":
        return _curv_chord_ratio(seg)
    if method == "MaxDeflection":
        return _curv_max_deflection(seg, sample_units)
    if method == "CurvatureEnergy":
        return _curv_energy(seg, sample_units)
    cr = _curv_chord_ratio(seg)
    md = _curv_max_deflection(seg, sample_units)
    ce = _curv_energy(seg, sample_units)
    mdn = md / math.pi
    cen = min(1.0, ce * 2.0)
    ws = float(w_cr) + float(w_md) + float(w_ce)
    if ws <= 0:
        return 0.0
    return ((float(w_cr) * cr) + (float(w_md) * mdn) + (float(w_ce) * cen)) / ws

def _iter_parts(polyline_geom, sr):
    if not polyline_geom:
        return
    try:
        if not polyline_geom.isMultipart:
            yield 0, polyline_geom
            return
        part_id = 0
        for part in polyline_geom.getPart():
            arr = arcpy.Array()
            for p in part:
                if p:
                    arr.add(p)
            if arr.count > 1:
                yield part_id, arcpy.Polyline(arr, sr)
            part_id += 1
    except Exception:
        yield 0, polyline_geom



# =============================================================================
# 5. Output / FC helpers
# =============================================================================

def _create_fc(workspace: str, name: str, geom_type: str, sr, logger) -> str:
    out_path = os.path.join(workspace, name)
    if arcpy.Exists(out_path):
        try:
            arcpy.management.Delete(out_path)
        except Exception as e:
            if logger:
                logger.error("Failed deleting existing output: %s", str(e))
            raise
    arcpy.management.CreateFeatureclass(workspace, name, geom_type, "", "DISABLED", "DISABLED", sr)
    return out_path

def _add_fields(fc: str, defs) -> None:
    for d in defs:
        if len(d) == 2:
            arcpy.management.AddField(fc, d[0], d[1])
        else:
            arcpy.management.AddField(fc, d[0], d[1], field_length=d[2])

def _scratch_path(scratch_ws: str, prefix: str) -> str:
    return os.path.join(scratch_ws, f"{prefix}_{uuid.uuid4().hex[:8]}")

# =============================================================================
# 6. Text metrics
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
            logger.warning("Annotation text field not found (expected TextString/TEXT).")
        return None, None
    heights = []; cws = []; n = 0
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
                h = float(ext.height); w = float(ext.width)
                if h <= 0 or w <= 0:
                    continue
                cw = w / (h * float(len(s)))
                if cw <= 0:
                    continue
                heights.append(h); cws.append(cw); n += 1
                if n >= max_samples:
                    break
    except Exception:
        if logger:
            logger.warning("Failed reading annotation samples: %s", traceback.format_exc())
    if not heights:
        if logger:
            logger.warning("No usable annotation samples.")
        return None, None
    heights.sort(); cws.sort()
    return heights[len(heights) // 2], cws[len(cws) // 2]

def _estimate_text_metrics_units(text_value, map_scale: float, mpu: float, pad_units: float,
                                 derive_metrics: bool, derived_h_units, derived_cw,
                                 font_size_pt: float, char_w_factor: float):
    s = "" if text_value is None else str(text_value)
    n = max(1, len(s))
    if derive_metrics and derived_h_units and float(derived_h_units) > 0:
        h_units = float(derived_h_units)
        cw = float(derived_cw) if (derived_cw and float(derived_cw) > 0) else float(char_w_factor)
        w_units = float(n) * cw * h_units
    else:
        h_mm_map = float(font_size_pt) * PT_TO_MM
        h_m_ground = (h_mm_map * float(map_scale)) / 1000.0
        h_units = h_m_ground / float(mpu)
        w_units = float(n) * float(char_w_factor) * h_units
    return h_units, w_units, float(pad_units)

# =============================================================================
# 7. Obstacle mask FC (scratchGDB-resident, spatial-indexed)
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
            arcpy.analysis.Buffer(lyr, tmp_buf, str(safe_units), dissolve_option="ALL")
            buf_fcs.append(tmp_buf)
        except Exception:
            _log_msg(messages, logger, "WARN",
                     f"Obstacle buffer failed for a layer: {traceback.format_exc()}")

    if not buf_fcs:
        return None

    merged = _scratch_path(scratch_gdb, "obs_merge")
    try:
        arcpy.management.Merge(buf_fcs, merged)
    except Exception:
        _log_msg(messages, logger, "ERROR", f"Obstacle merge failed: {traceback.format_exc()}")
        return None

    dissolved = _scratch_path(scratch_gdb, "ObstacleMask")
    try:
        arcpy.management.Dissolve(merged, dissolved)
    except Exception:
        _log_msg(messages, logger, "ERROR", f"Obstacle dissolve failed: {traceback.format_exc()}")
        return None

    try:
        arcpy.management.AddSpatialIndex(dissolved)
    except Exception:
        pass

    for fc in buf_fcs + [merged]:
        try:
            arcpy.management.Delete(fc)
        except Exception:
            pass
    return dissolved

def _make_mask_layer(mask_fc: Optional[str]) -> Optional[str]:
    if not mask_fc:
        return None
    name = "obs_mask_lyr_" + uuid.uuid4().hex[:6]
    try:
        arcpy.management.MakeFeatureLayer(mask_fc, name)
        return name
    except Exception:
        return None

# =============================================================================
# 8. AABB cache
# =============================================================================

class _PlacedCache:
    __slots__ = ("items",)
    def __init__(self):
        self.items: List[Tuple[float, float, float, float, object]] = []

    def add(self, geom) -> None:
        if geom is None:
            return
        ext = geom.extent
        if ext is None:
            return
        self.items.append((ext.XMin, ext.YMin, ext.XMax, ext.YMax, geom))

    def overlap_area(self, foot_geom, logger=None) -> float:
        if foot_geom is None or not self.items:
            return 0.0
        ea = foot_geom.extent
        if ea is None:
            return 0.0
        axmin, aymin, axmax, aymax = ea.XMin, ea.YMin, ea.XMax, ea.YMax
        total = 0.0
        for (bxmin, bymin, bxmax, bymax, g) in self.items:
            if axmax < bxmin or axmin > bxmax or aymax < bymin or aymin > bymax:
                continue
            try:
                if foot_geom.disjoint(g):
                    continue
                inter = foot_geom.intersect(g, 4)
                if inter:
                    total += float(inter.area)
            except Exception:
                if logger:
                    logger.warning("Self-overlap intersect failed: %s", traceback.format_exc())
        return total

# =============================================================================
# 9. Mask overlap via SelectLayerByLocation
# =============================================================================

def _mask_overlap_area(foot_geom, mask_layer: Optional[str], mask_fc: Optional[str],
                        logger=None) -> float:
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
                    except Exception:
                        pass
        except Exception:
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
                    except Exception:
                        if logger:
                            logger.warning("Mask intersect failed: %s", traceback.format_exc())
        finally:
            try:
                arcpy.management.SelectLayerByAttribute(mask_layer, "CLEAR_SELECTION")
            except Exception:
                pass
    except Exception:
        if logger:
            logger.warning("SelectLayerByLocation on mask failed: %s", traceback.format_exc())
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
                    except Exception:
                        pass
        except Exception:
            pass
    return total

# =============================================================================
# 10. Annotation seeding
# =============================================================================

def _find_seed_from_annotation(anno_layer_or_path, part_geom,
                                win_start: float, win_end: float,
                                search_pad_units: float, logger):
    if not anno_layer_or_path:
        return None
    src = _resolve_full_source(anno_layer_or_path)
    try:
        seg = part_geom.segmentAlongLine(win_start, win_end, False)
        if not seg:
            return None
        env = seg.extent
        if not env:
            return None
        xmin = env.XMin - search_pad_units
        xmax = env.XMax + search_pad_units
        ymin = env.YMin - search_pad_units
        ymax = env.YMax + search_pad_units
        best_d = None; best_dist = None
        with arcpy.da.SearchCursor(src, ["SHAPE@"]) as cur:
            for (g,) in cur:
                if not g:
                    continue
                ge = g.extent
                if not ge:
                    continue
                if ge.XMax < xmin or ge.XMin > xmax or ge.YMax < ymin or ge.YMin > ymax:
                    continue
                cpt = g.centroid
                try:
                    near_pt = part_geom.snapToLine(cpt)
                    d = part_geom.measureOnLine(near_pt, False)
                except Exception:
                    continue
                if d < win_start or d > win_end:
                    continue
                dist = float(cpt.distanceTo(near_pt))
                if best_dist is None or dist < best_dist:
                    best_dist = dist
                    best_d = float(d)
        return best_d
    except Exception:
        if logger:
            logger.warning("Seed from annotation failed: %s", traceback.format_exc())
        return None

# =============================================================================
# 11. Scoring engine
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

def _score_candidate(part_geom, center_d: float, win_start: float, win_end: float,
                     foot_len: float, foot_h: float, eps_units: float,
                     curv_method: str, curv_sample_units: float,
                     w_cr: float, w_md: float, w_ce: float,
                     w_curv: float, w_ovlp: float, w_center: float,
                     max_ovlp, max_curv,
                     mask_layer, mask_fc, placed_cache: _PlacedCache,
                     sr, logger):
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
    except Exception:
        if logger:
            logger.warning("segmentAlongLine failed (center %0.2f): %s",
                           center_d, traceback.format_exc())
        return None
    if not seg_geom or float(seg_geom.length) <= 0:
        return None
    ang = _tangent_angle_at_distance(part_geom, center_d, eps_units)
    rot_deg = _deg(ang)
    center_pt = part_geom.positionAlongLine(center_d, False)
    foot_geom = _make_oriented_rect(center_pt, ang, 0.5 * foot_len, 0.5 * foot_h, sr)
    curv = _compute_curvature(seg_geom, curv_method, curv_sample_units, w_cr, w_md, w_ce)
    ovlp = 0.0
    if mask_fc:
        ovlp += _mask_overlap_area(foot_geom, mask_layer, mask_fc, logger)
    if placed_cache is not None:
        ovlp += placed_cache.overlap_area(foot_geom, logger)
    if max_ovlp is not None and ovlp > float(max_ovlp):
        return None
    if max_curv is not None and curv > float(max_curv):
        return None
    win_len = max(1e-9, float(win_end - win_start))
    win_center = win_start + 0.5 * win_len
    center_pen = abs(center_d - win_center) / win_len
    center_pen = math.pow(center_pen, 1.5)
    score = (float(w_curv) * curv) + (float(w_ovlp) * ovlp) + (float(w_center) * center_pen)
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
        x = float(val); mi = float(major_interval)
        if mi <= 0:
            return True
        r = abs(x % mi)
        return (r < 1e-6) or (abs(r - mi) < 1e-6)
    except Exception:
        return False



# =============================================================================
# 12. Toolbox + tools
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
            "v4 hardening:\n"
            " - SELECTION-BYPASS hardwired (full datasets always processed)\n"
            " - Obstacle mask lives in scratchGDB on disk with spatial index\n"
            " - Per-candidate overlap uses SelectLayerByLocation (not a giant union)\n"
            " - Stage-by-stage [DIAG] logging"
        )
        self.canRunInBackground = True

    def isLicensed(self):
        return True

    def getParameterInfo(self):
        in_contours = arcpy.Parameter(displayName="Contour lines", name="in_contours",
            datatype="GPFeatureLayer", parameterType="Required", direction="Input")
        elev_field = arcpy.Parameter(displayName="Elevation field (label text source)",
            name="elev_field", datatype="Field", parameterType="Required", direction="Input")
        elev_field.parameterDependencies = [in_contours.name]
        selection_mode = arcpy.Parameter(displayName="Selection mode", name="selection_mode",
            datatype="GPString", parameterType="Required", direction="Input")
        selection_mode.filter.type = "ValueList"
        selection_mode.filter.list = ["ALL", "MAJOR_INTERVAL"]
        selection_mode.value = "ALL"; selection_mode.category = "Selection"
        major_interval = arcpy.Parameter(displayName="Major interval (e.g., 100 for elev%100==0)",
            name="major_interval", datatype="GPDouble", parameterType="Optional", direction="Input")
        major_interval.value = 100.0; major_interval.enabled = False
        major_interval.category = "Selection"
        interval_m = arcpy.Parameter(displayName="Along-line interval (meters)", name="interval_m",
            datatype="GPDouble", parameterType="Required", direction="Input")
        interval_m.value = 500.0
        safe_mm = arcpy.Parameter(displayName="Safe distance from obstacles (mm on map)",
            name="safe_mm", datatype="GPDouble", parameterType="Required", direction="Input")
        safe_mm.value = 2.0
        halo_mm = arcpy.Parameter(displayName="Extra halo/mask margin (mm on map)",
            name="halo_mm", datatype="GPDouble", parameterType="Optional", direction="Input")
        halo_mm.value = 0.0
        map_scale = arcpy.Parameter(displayName="Map scale denominator (e.g., 25000 for 1:25000)",
            name="map_scale", datatype="GPDouble", parameterType="Required", direction="Input")
        map_scale.value = 25000.0
        obstacles = arcpy.Parameter(displayName="Obstacle layers (lines, polygons, points)",
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
        derive_metrics.value = True; derive_metrics.category = "Text Metrics"
        font_size_pt = arcpy.Parameter(displayName="Font size (points) if metrics not derived",
            name="font_size_pt", datatype="GPDouble", parameterType="Optional", direction="Input")
        font_size_pt.value = 8.0; font_size_pt.category = "Text Metrics"
        char_w_factor = arcpy.Parameter(
            displayName="Average character width factor if metrics not derived",
            name="char_w_factor", datatype="GPDouble",
            parameterType="Optional", direction="Input")
        char_w_factor.value = 0.6; char_w_factor.category = "Text Metrics"
        curv_method = arcpy.Parameter(displayName="Curvature method", name="curv_method",
            datatype="GPString", parameterType="Required", direction="Input")
        curv_method.filter.type = "ValueList"
        curv_method.filter.list = ["Hybrid", "ChordRatio", "MaxDeflection", "CurvatureEnergy"]
        curv_method.value = "Hybrid"; curv_method.category = "Curvature"
        curv_sample_m = arcpy.Parameter(displayName="Curvature sampling step (meters)",
            name="curv_sample_m", datatype="GPDouble", parameterType="Required", direction="Input")
        curv_sample_m.value = 5.0; curv_sample_m.category = "Curvature"
        w_cr = arcpy.Parameter(displayName="Hybrid weight: chord ratio", name="w_cr",
            datatype="GPDouble", parameterType="Required", direction="Input")
        w_cr.value = 0.5; w_cr.category = "Curvature"
        w_md = arcpy.Parameter(displayName="Hybrid weight: max deflection", name="w_md",
            datatype="GPDouble", parameterType="Required", direction="Input")
        w_md.value = 0.3; w_md.category = "Curvature"
        w_ce = arcpy.Parameter(displayName="Hybrid weight: curvature energy", name="w_ce",
            datatype="GPDouble", parameterType="Required", direction="Input")
        w_ce.value = 0.2; w_ce.category = "Curvature"
        w_curv = arcpy.Parameter(displayName="Weight: curvature", name="w_curv",
            datatype="GPDouble", parameterType="Required", direction="Input")
        w_curv.value = 1.0; w_curv.category = "Scoring"
        w_ovlp = arcpy.Parameter(displayName="Weight: overlap area", name="w_ovlp",
            datatype="GPDouble", parameterType="Required", direction="Input")
        w_ovlp.value = 5.0; w_ovlp.category = "Scoring"
        w_center = arcpy.Parameter(displayName="Weight: window-center preference",
            name="w_center", datatype="GPDouble",
            parameterType="Required", direction="Input")
        w_center.value = 0.25; w_center.category = "Scoring"
        max_ovlp = arcpy.Parameter(displayName="Max allowed overlap area (linear_unit^2)",
            name="max_ovlp", datatype="GPDouble", parameterType="Optional", direction="Input")
        max_ovlp.category = "Thresholds"
        max_curv = arcpy.Parameter(displayName="Max allowed curvature score", name="max_curv",
            datatype="GPDouble", parameterType="Optional", direction="Input")
        max_curv.category = "Thresholds"
        min_contour_m = arcpy.Parameter(displayName="Minimum contour part length (meters)",
            name="min_contour_m", datatype="GPDouble", parameterType="Optional", direction="Input")
        min_contour_m.value = 0.0; min_contour_m.category = "Thresholds"
        short_policy = arcpy.Parameter(displayName="Short part policy", name="short_policy",
            datatype="GPString", parameterType="Required", direction="Input")
        short_policy.filter.type = "ValueList"
        short_policy.filter.list = ["PLACE_CENTER", "SKIP"]
        short_policy.value = "PLACE_CENTER"; short_policy.category = "Thresholds"
        out_ws = arcpy.Parameter(
            displayName="Output workspace (file geodatabase recommended)", name="out_ws",
            datatype="DEWorkspace", parameterType="Required", direction="Input")
        out_segments_name = arcpy.Parameter(displayName="Output segments name",
            name="out_segments_name", datatype="GPString",
            parameterType="Required", direction="Input")
        out_segments_name.value = "ContourLabelSegments"
        out_points_name = arcpy.Parameter(displayName="Output points name",
            name="out_points_name", datatype="GPString",
            parameterType="Required", direction="Input")
        out_points_name.value = "ContourLabelPoints"
        make_footprints = arcpy.Parameter(displayName="Create QA footprints (polygons)",
            name="make_footprints", datatype="GPBoolean",
            parameterType="Optional", direction="Input")
        make_footprints.value = False; make_footprints.category = "Outputs"
        out_footprints_name = arcpy.Parameter(displayName="QA footprints name (if enabled)",
            name="out_footprints_name", datatype="GPString",
            parameterType="Optional", direction="Input")
        out_footprints_name.value = "ContourLabelFootprints"
        out_footprints_name.enabled = False; out_footprints_name.category = "Outputs"
        make_stats = arcpy.Parameter(displayName="Create statistics table",
            name="make_stats", datatype="GPBoolean",
            parameterType="Optional", direction="Input")
        make_stats.value = True; make_stats.category = "Outputs"
        out_stats_name = arcpy.Parameter(displayName="Statistics table name (if enabled)",
            name="out_stats_name", datatype="GPString",
            parameterType="Optional", direction="Input")
        out_stats_name.value = "ContourLabelStats"
        out_stats_name.enabled = True; out_stats_name.category = "Outputs"
        max_tries = arcpy.Parameter(
            displayName="Internal tries per window (refinement around seed)",
            name="max_tries", datatype="GPLong",
            parameterType="Required", direction="Input")
        max_tries.value = 11; max_tries.category = "Advanced"
        add_to_map = arcpy.Parameter(displayName="Add outputs to current map",
            name="add_to_map", datatype="GPBoolean",
            parameterType="Optional", direction="Input")
        add_to_map.value = True; add_to_map.category = "Advanced"

        out_segments = arcpy.Parameter(displayName="Output segments", name="out_segments",
            datatype="DEFeatureClass", parameterType="Derived", direction="Output")
        out_points = arcpy.Parameter(displayName="Output points", name="out_points",
            datatype="DEFeatureClass", parameterType="Derived", direction="Output")
        out_footprints = arcpy.Parameter(displayName="Output footprints",
            name="out_footprints", datatype="DEFeatureClass",
            parameterType="Derived", direction="Output")
        out_stats = arcpy.Parameter(displayName="Output statistics table",
            name="out_stats", datatype="DETable",
            parameterType="Derived", direction="Output")
        out_log = arcpy.Parameter(displayName="Log file path", name="out_log",
            datatype="GPString", parameterType="Derived", direction="Output")

        return [
            in_contours, elev_field,                       # 0,1
            selection_mode, major_interval,                # 2,3
            interval_m, safe_mm, halo_mm, map_scale,       # 4,5,6,7
            obstacles, anno_layer,                         # 8,9
            derive_metrics, font_size_pt, char_w_factor,   # 10,11,12
            curv_method, curv_sample_m, w_cr, w_md, w_ce,  # 13..17
            w_curv, w_ovlp, w_center,                      # 18..20
            max_ovlp, max_curv, min_contour_m, short_policy, # 21..24
            out_ws, out_segments_name, out_points_name,    # 25..27
            make_footprints, out_footprints_name,          # 28,29
            make_stats, out_stats_name,                    # 30,31
            max_tries, add_to_map,                         # 32,33
            out_segments, out_points, out_footprints, out_stats, out_log  # 34..38
        ]

    def updateParameters(self, parameters):
        try:
            sel_mode = parameters[2].valueAsText
            parameters[3].enabled = (sel_mode == "MAJOR_INTERVAL")
            parameters[29].enabled = bool(parameters[28].value)
            parameters[31].enabled = bool(parameters[30].value)
        except Exception:
            pass

    def updateMessages(self, parameters):
        try:
            contours = parameters[0].valueAsText
            if contours:
                sr = arcpy.Describe(contours).spatialReference
                if _is_geographic(sr):
                    parameters[0].setErrorMessage("Input contours must be projected (linear units).")
                else:
                    unit_name = _linear_unit_name(sr)
                    if parameters[21].value is not None:
                        parameters[21].setWarningMessage(f"max_ovlp is in {unit_name}^2.")
            sel_mode = parameters[2].valueAsText
            if sel_mode == "MAJOR_INTERVAL":
                if parameters[3].value is None or float(parameters[3].value) <= 0:
                    parameters[3].setErrorMessage(
                        "Major interval must be > 0 when Selection mode is MAJOR_INTERVAL.")
        except Exception:
            pass

    def _add_to_active_map(self, paths):
        try:
            aprx = arcpy.mp.ArcGISProject("CURRENT")
            m = aprx.activeMap
            if m is None:
                return
            for p in paths:
                if not p:
                    continue
                try:
                    m.addDataFromPath(p)
                except Exception:
                    _warn(f"Could not add {p} to active map.")
        except Exception:
            pass

    def execute(self, parameters, messages):
        arcpy.env.overwriteOutput = True
        try:
            arcpy.env.parallelProcessingFactor = "100%"
        except Exception:
            pass

        logger = None
        seg_ins = pt_ins = foot_ins = stats_ins = None
        mask_layer = None

        try:
            in_contours_layer = parameters[0].valueAsText
            elev_field = parameters[1].valueAsText
            selection_mode = parameters[2].valueAsText
            major_interval = parameters[3].value
            interval_m = float(parameters[4].value)
            safe_mm = float(parameters[5].value)
            halo_mm = float(parameters[6].value) if parameters[6].value is not None else 0.0
            map_scale = float(parameters[7].value)
            obstacles_text = parameters[8].valueAsText
            obstacle_layers = [s.strip() for s in obstacles_text.split(";")] if obstacles_text else []
            anno_layer = parameters[9].valueAsText if parameters[9].valueAsText else None
            derive_metrics = bool(parameters[10].value)
            font_size_pt = float(parameters[11].value) if parameters[11].value else 8.0
            char_w_factor = float(parameters[12].value) if parameters[12].value else 0.6
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
            min_contour_m = float(parameters[23].value) if parameters[23].value else 0.0
            short_policy = parameters[24].valueAsText
            out_ws = parameters[25].valueAsText
            out_segments_name = parameters[26].valueAsText
            out_points_name = parameters[27].valueAsText
            make_footprints = bool(parameters[28].value)
            out_footprints_name = parameters[29].valueAsText if make_footprints else None
            make_stats = bool(parameters[30].value)
            out_stats_name = parameters[31].valueAsText if make_stats else None
            max_tries = int(parameters[32].value)
            add_to_map = bool(parameters[33].value)

            logger, log_path = _setup_logger(out_ws, "opt")
            _log_msg(messages, logger, "INFO", "Starting OptimizeContourLabelAnchorsV4 (Pro)")

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
                raise arcpy.ExecuteError("Projected coordinate system is required.")
            mpu = _meters_per_unit(sr)
            if not mpu or mpu <= 0:
                raise arcpy.ExecuteError("Could not determine meters-per-unit.")

            unit_name = _linear_unit_name(sr)
            _log_msg(messages, logger, "INFO",
                     f"Linear unit: {unit_name} (metersPerUnit={mpu})")
            _log_msg(messages, logger, "INFO", f"max_ovlp unit is {unit_name}^2")

            safe_units = _safe_mm_to_units(safe_mm, map_scale, mpu)
            halo_units = _safe_mm_to_units(halo_mm, map_scale, mpu) if halo_mm > 0 else 0.0
            pad_units = safe_units + halo_units
            interval_units = _meters_to_units(interval_m, mpu)
            curv_sample_units = _meters_to_units(curv_sample_m, mpu)
            min_contour_units = _meters_to_units(min_contour_m, mpu)

            _log_msg(messages, logger, "INFO",
                     f"interval_units={interval_units:.4f} safe_units={safe_units:.4f} "
                     f"halo_units={halo_units:.4f} pad_units={pad_units:.4f}")

            scratch_gdb = arcpy.env.scratchGDB
            if not scratch_gdb or not arcpy.Exists(scratch_gdb):
                scratch_gdb = arcpy.env.scratchWorkspace
            if not scratch_gdb or not arcpy.Exists(scratch_gdb):
                raise arcpy.ExecuteError("No scratch GDB available.")
            _log_msg(messages, logger, "INFO", f"Scratch (disk): {scratch_gdb}")

            derived_h_units, derived_cw = (None, None)
            if derive_metrics and anno_layer:
                _log_msg(messages, logger, "INFO", "Deriving text metrics from annotation...")
                derived_h_units, derived_cw = _derive_text_metrics_from_annotation(anno_layer, logger)
                if derived_h_units and derived_cw:
                    _log_msg(messages, logger, "INFO",
                             f"Derived: height_units={derived_h_units:.4f} char_w={derived_cw:.4f}")
                else:
                    _log_msg(messages, logger, "WARN", "Could not derive metrics; using font estimate.")

            mask_fc = _build_obstacle_mask_fc(obstacle_layers, anno_layer, safe_units,
                                              scratch_gdb, logger, messages)
            if mask_fc:
                _log_msg(messages, logger, "INFO", f"[DIAG] Obstacle mask FC: {mask_fc}")
                mask_layer = _make_mask_layer(mask_fc)
            else:
                _log_msg(messages, logger, "INFO", "[DIAG] No obstacle mask.")

            out_segments_fc = _create_fc(out_ws, out_segments_name, "POLYLINE", sr, logger)
            out_points_fc = _create_fc(out_ws, out_points_name, "POINT", sr, logger)
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
                out_foot_fc = _create_fc(out_ws, out_footprints_name, "POLYGON", sr, logger)
                _add_fields(out_foot_fc, [("SRCID", "LONG"), ("PARTID", "LONG"),
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

            seg_ins = arcpy.da.InsertCursor(out_segments_fc,
                ["SHAPE@", "SRCID", "PARTID", "ELEV", "WSTART", "WEND", "CDIST",
                 "ANG", "CURV", "OVLP", "SCORE", "TEXT"])
            pt_ins = arcpy.da.InsertCursor(out_points_fc,
                ["SHAPE@", "SRCID", "PARTID", "ELEV", "WSTART", "WEND", "CDIST",
                 "ANG", "CURV", "OVLP", "SCORE", "TEXT", "ROT"])
            if out_foot_fc:
                foot_ins = arcpy.da.InsertCursor(out_foot_fc,
                    ["SHAPE@", "SRCID", "PARTID", "TEXT", "SCORE", "OVLP", "CURV"])
            if out_stats_tbl:
                stats_ins = arcpy.da.InsertCursor(out_stats_tbl,
                    ["SRCID", "PARTID", "WINCNT", "PLACED", "AVGSC", "MAXSC",
                     "AVGOV", "MAXOV", "AVGCURV", "MAXCURV", "SECS"])

            total_features = int(arcpy.management.GetCount(in_contours).getOutput(0))
            _log_msg(messages, logger, "INFO", f"[DIAG] Contours total: {total_features}")
            arcpy.SetProgressor("step", "Optimizing contour label anchors...",
                                0, max(1, total_features), 1)

            placed_cache = _PlacedCache()
            eps_units = max(0.001 * interval_units, 0.5 * curv_sample_units)
            considered = 0
            placed_total = 0

            fields = ["OID@", "SHAPE@", elev_field]
            with arcpy.da.SearchCursor(in_contours, fields) as cur:
                for oid, geom, elev in cur:
                    t0 = time.time()
                    arcpy.SetProgressorLabel(f"Processing contour OID {oid}")
                    arcpy.SetProgressorPosition()

                    if not geom:
                        _log_msg(messages, logger, "WARN", f"OID {oid} has null geometry; skipping.")
                        continue

                    if selection_mode == "MAJOR_INTERVAL":
                        if not _is_major_value(elev, major_interval):
                            continue
                    considered += 1

                    label_text = "" if elev is None else str(elev)
                    try:
                        text_h_units, text_w_units, _pad = _estimate_text_metrics_units(
                            label_text, map_scale, mpu, pad_units,
                            derive_metrics, derived_h_units, derived_cw,
                            font_size_pt, char_w_factor)
                    except Exception:
                        _log_msg(messages, logger, "ERROR",
                                 f"Text metric estimate failed for OID {oid}: {traceback.format_exc()}")
                        continue

                    foot_len = max(text_w_units + 2.0 * _pad, 2.0 * _pad)
                    foot_h = max(text_h_units + 2.0 * _pad, 2.0 * _pad)
                    internal_step = max(0.25 * foot_len, 0.05 * interval_units)
                    internal_step = min(internal_step, 0.25 * interval_units)
                    internal_step = max(internal_step, 0.5 * curv_sample_units)
                    offsets = _build_offsets(internal_step, max_tries)

                    for part_id, part_geom in _iter_parts(geom, sr):
                        if not part_geom:
                            continue
                        total_len = float(part_geom.length)
                        if total_len <= 0:
                            continue
                        if min_contour_units > 0 and total_len < min_contour_units:
                            if short_policy == "SKIP":
                                continue

                        win_count = 0; placed_count = 0
                        scores = []; ovlps = []; curvs = []

                        win_start = 0.0
                        while win_start < total_len:
                            win_end = min(win_start + interval_units, total_len)
                            win_len = win_end - win_start
                            if win_len <= 0:
                                break
                            win_count += 1

                            seed_d = None
                            if anno_layer:
                                seed_d = _find_seed_from_annotation(
                                    anno_layer, part_geom, win_start, win_end,
                                    safe_units, logger)
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
                                    mask_layer, mask_fc, placed_cache,
                                    sr, logger)
                                if not res:
                                    continue
                                if best is None or res["score"] < best["score"]:
                                    best = res

                            if best is None and (max_ovlp is not None or max_curv is not None):
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
                                        mask_layer, mask_fc, placed_cache,
                                        sr, logger)
                                    if not res:
                                        continue
                                    if best is None or res["score"] < best["score"]:
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
                                    mask_layer, mask_fc, placed_cache,
                                    sr, logger)

                            if best:
                                placed_count += 1
                                placed_total += 1
                                scores.append(best["score"])
                                ovlps.append(best["ovlp"])
                                curvs.append(best["curv"])
                                seg_ins.insertRow([
                                    best["seg_geom"], oid, part_id, label_text,
                                    win_start, win_end, best["center_d"],
                                    best["ang_deg"], best["curv"], best["ovlp"],
                                    best["score"], label_text])
                                pt = part_geom.positionAlongLine(best["center_d"], False)
                                pt_ins.insertRow([
                                    pt, oid, part_id, label_text,
                                    win_start, win_end, best["center_d"],
                                    best["ang_deg"], best["curv"], best["ovlp"],
                                    best["score"], label_text, best["ang_deg"]])
                                if foot_ins and best["foot_geom"]:
                                    foot_ins.insertRow([
                                        best["foot_geom"], oid, part_id, label_text,
                                        best["score"], best["ovlp"], best["curv"]])
                                if best["foot_geom"]:
                                    placed_cache.add(best["foot_geom"])
                            else:
                                if logger:
                                    logger.warning(
                                        "No placement for OID %s part %s window [%0.2f,%0.2f].",
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
                                avg_sc = max_sc = avg_ov = max_ov = avg_cv = max_cv = 0.0
                            stats_ins.insertRow([
                                oid, part_id, win_count, placed_count,
                                avg_sc, max_sc, avg_ov, max_ov,
                                avg_cv, max_cv, secs])

                    if (considered % 100) == 0:
                        gc.collect()

            _log_msg(messages, logger, "INFO",
                     f"[DIAG] Contours considered: {considered}, anchors placed: {placed_total}")
            _log_msg(messages, logger, "INFO", "Completed optimization.")
            _log_msg(messages, logger, "INFO", f"Segments: {out_segments_fc}")
            _log_msg(messages, logger, "INFO", f"Points: {out_points_fc}")

            parameters[34].value = out_segments_fc
            parameters[35].value = out_points_fc
            parameters[36].value = out_foot_fc if out_foot_fc else ""
            parameters[37].value = out_stats_tbl if out_stats_tbl else ""
            parameters[38].value = log_path

            if add_to_map:
                paths = [out_segments_fc, out_points_fc]
                if out_foot_fc:
                    paths.append(out_foot_fc)
                self._add_to_active_map(paths)
            return

        finally:
            for ins in (seg_ins, pt_ins, foot_ins, stats_ins):
                try:
                    if ins:
                        del ins
                except Exception:
                    pass
            if mask_layer:
                try:
                    arcpy.management.Delete(mask_layer)
                except Exception:
                    pass
            _shutdown_logger(logger)


# ----------------------------------------------------------------------
# Validate Label Anchors
# ----------------------------------------------------------------------

class ValidateLabelAnchors(object):
    def __init__(self):
        self.label = "Validate Label Anchors (QA, hardened)"
        self.description = "Checks overlaps for anchor footprints and outputs a QA table."
        self.canRunInBackground = True

    def isLicensed(self):
        return True

    def getParameterInfo(self):
        in_points = arcpy.Parameter(displayName="Anchor points (output from optimizer)",
            name="in_points", datatype="GPFeatureLayer",
            parameterType="Required", direction="Input")
        text_field = arcpy.Parameter(displayName="Text field", name="text_field",
            datatype="Field", parameterType="Required", direction="Input")
        text_field.parameterDependencies = [in_points.name]
        safe_mm = arcpy.Parameter(displayName="Safe distance from obstacles (mm on map)",
            name="safe_mm", datatype="GPDouble", parameterType="Required", direction="Input")
        safe_mm.value = 2.0
        halo_mm = arcpy.Parameter(displayName="Extra halo/mask margin (mm on map)",
            name="halo_mm", datatype="GPDouble", parameterType="Optional", direction="Input")
        halo_mm.value = 0.0
        map_scale = arcpy.Parameter(displayName="Map scale denominator", name="map_scale",
            datatype="GPDouble", parameterType="Required", direction="Input")
        map_scale.value = 25000.0
        font_size_pt = arcpy.Parameter(displayName="Font size (points) for footprint estimate",
            name="font_size_pt", datatype="GPDouble",
            parameterType="Required", direction="Input")
        font_size_pt.value = 8.0
        char_w_factor = arcpy.Parameter(displayName="Average character width factor",
            name="char_w_factor", datatype="GPDouble",
            parameterType="Required", direction="Input")
        char_w_factor.value = 0.6
        obstacles = arcpy.Parameter(displayName="Obstacle layers (lines, polygons, points)",
            name="obstacles", datatype="GPFeatureLayer",
            parameterType="Optional", direction="Input", multiValue=True)
        out_ws = arcpy.Parameter(displayName="Output workspace", name="out_ws",
            datatype="DEWorkspace", parameterType="Required", direction="Input")
        out_table_name = arcpy.Parameter(displayName="QA report table name",
            name="out_table_name", datatype="GPString",
            parameterType="Required", direction="Input")
        out_table_name.value = "LabelAnchorQA"
        out_table = arcpy.Parameter(displayName="QA report table", name="out_table",
            datatype="DETable", parameterType="Derived", direction="Output")
        out_log = arcpy.Parameter(displayName="Log file path", name="out_log",
            datatype="GPString", parameterType="Derived", direction="Output")
        return [in_points, text_field, safe_mm, halo_mm, map_scale,
                font_size_pt, char_w_factor, obstacles, out_ws, out_table_name,
                out_table, out_log]

    def updateMessages(self, parameters):
        try:
            in_points = parameters[0].valueAsText
            if in_points:
                sr = arcpy.Describe(in_points).spatialReference
                if _is_geographic(sr):
                    parameters[0].setErrorMessage("Input points must be projected (linear units).")
        except Exception:
            pass

    def execute(self, parameters, messages):
        arcpy.env.overwriteOutput = True
        try:
            arcpy.env.parallelProcessingFactor = "100%"
        except Exception:
            pass
        logger = None; ins = None; mask_layer = None
        try:
            in_points_layer = parameters[0].valueAsText
            text_field = parameters[1].valueAsText
            safe_mm = float(parameters[2].value)
            halo_mm = float(parameters[3].value) if parameters[3].value is not None else 0.0
            map_scale = float(parameters[4].value)
            font_size_pt = float(parameters[5].value)
            char_w_factor = float(parameters[6].value)
            obstacles_text = parameters[7].valueAsText
            obstacle_layers = [s.strip() for s in obstacles_text.split(";")] if obstacles_text else []
            out_ws = parameters[8].valueAsText
            out_table_name = parameters[9].valueAsText

            logger, log_path = _setup_logger(out_ws, "validate")
            _log_msg(messages, logger, "INFO", "Starting ValidateLabelAnchors (Pro)")

            _announce_selection("Anchor points", in_points_layer, messages, logger)
            for lyr in obstacle_layers:
                _announce_selection("Obstacle", lyr, messages, logger)

            in_points = _resolve_full_source(in_points_layer)

            desc = arcpy.Describe(in_points)
            sr = desc.spatialReference
            if _is_geographic(sr):
                raise arcpy.ExecuteError("Projected coordinate system is required.")
            mpu = _meters_per_unit(sr)
            if not mpu or mpu <= 0:
                raise arcpy.ExecuteError("Could not determine meters-per-unit.")

            safe_units = _safe_mm_to_units(safe_mm, map_scale, mpu)
            halo_units = _safe_mm_to_units(halo_mm, map_scale, mpu) if halo_mm > 0 else 0.0
            pad_units = safe_units + halo_units

            scratch_gdb = arcpy.env.scratchGDB
            if not scratch_gdb or not arcpy.Exists(scratch_gdb):
                scratch_gdb = arcpy.env.scratchWorkspace
            mask_fc = _build_obstacle_mask_fc(obstacle_layers, None, safe_units,
                                              scratch_gdb, logger, messages)
            mask_layer = _make_mask_layer(mask_fc) if mask_fc else None

            out_tbl = os.path.join(out_ws, out_table_name)
            if arcpy.Exists(out_tbl):
                arcpy.management.Delete(out_tbl)
            arcpy.management.CreateTable(out_ws, out_table_name)
            _add_fields(out_tbl, [
                ("OID", "LONG"), ("TEXT", "TEXT", 128),
                ("OVLP", "DOUBLE"), ("SELFOV", "DOUBLE"), ("FLAG", "TEXT", 32)])
            ins = arcpy.da.InsertCursor(out_tbl, ["OID", "TEXT", "OVLP", "SELFOV", "FLAG"])

            cache = _PlacedCache()
            fields = ["OID@", "SHAPE@", text_field]
            fld_names = [f.name for f in arcpy.ListFields(in_points)]
            has_rot = ("ROT" in fld_names)
            if has_rot:
                fields.append("ROT")

            total = 0
            with arcpy.da.SearchCursor(in_points, fields) as cur:
                for row in cur:
                    oid = row[0]; pt = row[1]; txt = row[2]
                    rot = row[3] if has_rot else 0.0
                    if not pt:
                        continue
                    txt = "" if txt is None else str(txt)
                    h_mm_map = float(font_size_pt) * PT_TO_MM
                    h_m_ground = (h_mm_map * float(map_scale)) / 1000.0
                    h_units = h_m_ground / float(mpu)
                    w_units = max(1, len(txt)) * float(char_w_factor) * h_units
                    foot_len = w_units + 2.0 * pad_units
                    foot_h = h_units + 2.0 * pad_units
                    ang = math.radians(float(rot)) if rot is not None else 0.0
                    foot = _make_oriented_rect(pt, ang, 0.5 * foot_len, 0.5 * foot_h, sr)

                    ovlp = 0.0
                    if mask_fc:
                        ovlp = _mask_overlap_area(foot, mask_layer, mask_fc, logger)
                    selfov = cache.overlap_area(foot, logger)

                    flag = "OK"
                    if ovlp > 0.0:
                        flag = "OBSTACLE"
                    if selfov > 0.0:
                        flag = "SELF"
                    if ovlp > 0.0 and selfov > 0.0:
                        flag = "BOTH"

                    ins.insertRow([oid, txt, ovlp, selfov, flag])
                    cache.add(foot)
                    total += 1
                    if (total % 200) == 0:
                        gc.collect()

            _log_msg(messages, logger, "INFO", f"[DIAG] Anchors checked: {total}")
            _log_msg(messages, logger, "INFO", f"QA table: {out_tbl}")
            parameters[10].value = out_tbl
            parameters[11].value = log_path
            return
        finally:
            try:
                if ins:
                    del ins
            except Exception:
                pass
            if mask_layer:
                try:
                    arcpy.management.Delete(mask_layer)
                except Exception:
                    pass
            _shutdown_logger(logger)


# ----------------------------------------------------------------------
# Curvature Heatmap
# ----------------------------------------------------------------------

class CurvatureHeatmap(object):
    def __init__(self):
        self.label = "Curvature Heatmap (QA)"
        self.description = "Creates line segments with curvature values for visualization."
        self.canRunInBackground = True

    def isLicensed(self):
        return True

    def getParameterInfo(self):
        in_contours = arcpy.Parameter(displayName="Contour lines", name="in_contours",
            datatype="GPFeatureLayer", parameterType="Required", direction="Input")
        step_m = arcpy.Parameter(displayName="Step (meters) along contour", name="step_m",
            datatype="GPDouble", parameterType="Required", direction="Input")
        step_m.value = 20.0
        seg_len_m = arcpy.Parameter(displayName="Segment length for curvature (meters)",
            name="seg_len_m", datatype="GPDouble",
            parameterType="Required", direction="Input")
        seg_len_m.value = 60.0
        curv_method = arcpy.Parameter(displayName="Curvature method", name="curv_method",
            datatype="GPString", parameterType="Required", direction="Input")
        curv_method.filter.type = "ValueList"
        curv_method.filter.list = ["Hybrid", "ChordRatio", "MaxDeflection", "CurvatureEnergy"]
        curv_method.value = "Hybrid"
        curv_sample_m = arcpy.Parameter(displayName="Curvature sampling step (meters)",
            name="curv_sample_m", datatype="GPDouble",
            parameterType="Required", direction="Input")
        curv_sample_m.value = 5.0
        w_cr = arcpy.Parameter(displayName="Hybrid weight: chord ratio", name="w_cr",
            datatype="GPDouble", parameterType="Required", direction="Input")
        w_cr.value = 0.5
        w_md = arcpy.Parameter(displayName="Hybrid weight: max deflection", name="w_md",
            datatype="GPDouble", parameterType="Required", direction="Input")
        w_md.value = 0.3
        w_ce = arcpy.Parameter(displayName="Hybrid weight: curvature energy", name="w_ce",
            datatype="GPDouble", parameterType="Required", direction="Input")
        w_ce.value = 0.2
        out_ws = arcpy.Parameter(displayName="Output workspace", name="out_ws",
            datatype="DEWorkspace", parameterType="Required", direction="Input")
        out_name = arcpy.Parameter(displayName="Output heatmap segments name",
            name="out_name", datatype="GPString",
            parameterType="Required", direction="Input")
        out_name.value = "ContourCurvatureHeat"
        out_fc = arcpy.Parameter(displayName="Output heatmap segments", name="out_fc",
            datatype="DEFeatureClass", parameterType="Derived", direction="Output")
        out_log = arcpy.Parameter(displayName="Log file path", name="out_log",
            datatype="GPString", parameterType="Derived", direction="Output")
        return [in_contours, step_m, seg_len_m, curv_method, curv_sample_m,
                w_cr, w_md, w_ce, out_ws, out_name, out_fc, out_log]

    def execute(self, parameters, messages):
        arcpy.env.overwriteOutput = True
        try:
            arcpy.env.parallelProcessingFactor = "100%"
        except Exception:
            pass
        logger = None; ins = None
        try:
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
            _log_msg(messages, logger, "INFO", "Starting CurvatureHeatmap (Pro)")

            _announce_selection("Contours", in_contours_layer, messages, logger)
            in_contours = _resolve_full_source(in_contours_layer)

            desc = arcpy.Describe(in_contours)
            sr = desc.spatialReference
            if _is_geographic(sr):
                raise arcpy.ExecuteError("Projected coordinate system is required.")
            mpu = _meters_per_unit(sr)
            if not mpu or mpu <= 0:
                raise arcpy.ExecuteError("Could not determine meters-per-unit.")

            step_units = _meters_to_units(step_m, mpu)
            seg_len_units = _meters_to_units(seg_len_m, mpu)
            curv_sample_units = _meters_to_units(curv_sample_m, mpu)

            out_fc = _create_fc(out_ws, out_name, "POLYLINE", sr, logger)
            _add_fields(out_fc, [("SRCID", "LONG"), ("PARTID", "LONG"),
                                  ("D", "DOUBLE"), ("CURV", "DOUBLE")])
            ins = arcpy.da.InsertCursor(out_fc, ["SHAPE@", "SRCID", "PARTID", "D", "CURV"])

            count = 0
            with arcpy.da.SearchCursor(in_contours, ["OID@", "SHAPE@"]) as cur:
                for oid, geom in cur:
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
                                seg = part_geom.segmentAlongLine(d0, d1, False)
                            except Exception:
                                seg = None
                            if seg and float(seg.length) > 0:
                                curv = _compute_curvature(seg, curv_method, curv_sample_units, w_cr, w_md, w_ce)
                                ins.insertRow([seg, oid, part_id, d, curv])
                                count += 1
                            d += step_units
                    if (count % 500) == 0:
                        gc.collect()

            _log_msg(messages, logger, "INFO", f"[DIAG] Heatmap segments written: {count}")
            parameters[10].value = out_fc
            parameters[11].value = log_path
            return
        finally:
            try:
                if ins:
                    del ins
            except Exception:
                pass
            _shutdown_logger(logger)


# ----------------------------------------------------------------------
# Convert Labels To Annotation (Pro arcpy.mp)
# ----------------------------------------------------------------------

class ConvertLabelsToAnnotationPro(object):
    """
    Pro replacement for the ArcMap-only AutoGenerateAnnotation tool.

    Uses arcpy.cartography.ConvertLabelsToAnnotation against the active
    map (or a saved .aprx). This is the modern Pro equivalent of
    TiledLabelsToAnnotation_cartography. It works at the map's reference
    scale (or one supplied here) on a single tile covering the active
    extent.
    """
    def __init__(self):
        self.label = "Convert Labels To Annotation (Pro)"
        self.description = (
            "Generates annotation from layer labels via "
            "arcpy.cartography.ConvertLabelsToAnnotation. Works on the active "
            "map by default; pass a project file to target a saved .aprx."
        )
        self.canRunInBackground = False

    def isLicensed(self):
        return True

    def getParameterInfo(self):
        project = arcpy.Parameter(displayName="Project (.aprx path or CURRENT)",
            name="project", datatype="GPString",
            parameterType="Required", direction="Input")
        project.value = "CURRENT"
        map_name = arcpy.Parameter(displayName="Map name (use first if blank)",
            name="map_name", datatype="GPString",
            parameterType="Optional", direction="Input")
        out_gdb = arcpy.Parameter(displayName="Output geodatabase",
            name="out_gdb", datatype="DEWorkspace",
            parameterType="Required", direction="Input")
        anno_suffix = arcpy.Parameter(displayName="Annotation suffix",
            name="anno_suffix", datatype="GPString",
            parameterType="Required", direction="Input")
        anno_suffix.value = "Anno"
        reference_scale = arcpy.Parameter(displayName="Reference scale (optional)",
            name="reference_scale", datatype="GPDouble",
            parameterType="Optional", direction="Input")
        which_layers = arcpy.Parameter(displayName="Layers source",
            name="which_layers", datatype="GPString",
            parameterType="Required", direction="Input")
        which_layers.filter.type = "ValueList"
        which_layers.filter.list = ["ALL_LAYERS", "VISIBLE_LAYERS"]
        which_layers.value = "ALL_LAYERS"
        feature_linked = arcpy.Parameter(displayName="Feature linked",
            name="feature_linked", datatype="GPString",
            parameterType="Required", direction="Input")
        feature_linked.filter.type = "ValueList"
        feature_linked.filter.list = ["STANDARD", "FEATURE_LINKED"]
        feature_linked.value = "STANDARD"
        generate_unplaced = arcpy.Parameter(displayName="Generate unplaced annotation",
            name="generate_unplaced", datatype="GPString",
            parameterType="Required", direction="Input")
        generate_unplaced.filter.type = "ValueList"
        generate_unplaced.filter.list = ["NOT_GENERATE_UNPLACED_ANNOTATION", "GENERATE_UNPLACED_ANNOTATION"]
        generate_unplaced.value = "GENERATE_UNPLACED_ANNOTATION"
        out_workspace = arcpy.Parameter(displayName="Output workspace (derived)",
            name="out_workspace", datatype="DEWorkspace",
            parameterType="Derived", direction="Output")
        out_log = arcpy.Parameter(displayName="Log file path", name="out_log",
            datatype="GPString", parameterType="Derived", direction="Output")
        return [project, map_name, out_gdb, anno_suffix, reference_scale,
                which_layers, feature_linked, generate_unplaced,
                out_workspace, out_log]

    def execute(self, parameters, messages):
        arcpy.env.overwriteOutput = True
        logger = None
        try:
            project = parameters[0].valueAsText
            map_name = parameters[1].valueAsText
            out_gdb = parameters[2].valueAsText
            anno_suffix = parameters[3].valueAsText
            reference_scale = parameters[4].value
            which_layers = parameters[5].valueAsText
            feature_linked = parameters[6].valueAsText
            generate_unplaced = parameters[7].valueAsText

            logger, log_path = _setup_logger(out_gdb, "anno_pro")
            _log_msg(messages, logger, "INFO", "Starting ConvertLabelsToAnnotation (Pro)")

            try:
                aprx = arcpy.mp.ArcGISProject(project)
            except Exception:
                raise arcpy.ExecuteError(f"Could not open project: {project}")

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
                    ref_scale_val = float(m.referenceScale) if m.referenceScale else 0.0
                except Exception:
                    ref_scale_val = 0.0
            if not ref_scale_val:
                ref_scale_val = 25000.0
                _log_msg(messages, logger, "WARN",
                         f"No reference scale on map; defaulting to {ref_scale_val}.")

            # ConvertLabelsToAnnotation in Pro:
            # arcpy.cartography.ConvertLabelsToAnnotation(
            #   input_map, conversion_scale, output_geodatabase,
            #   anno_suffix, extent, generate_unplaced_annotation,
            #   require_symbol_id, feature_linked, auto_create,
            #   update_on_shape_change, output_group_layer, which_layers,
            #   single_label_class, multiple_feature_classes, merge_label_classes)
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
            _log_msg(messages, logger, "INFO", "Completed Convert Labels To Annotation (Pro).")
            return
        finally:
            _shutdown_logger(logger)


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
        arr = arcpy.Array([arcpy.Point(0, 0), arcpy.Point(50, 50), arcpy.Point(100, 0)])
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
        self.description = "Runs a small set of automated checks for core helpers."
        self.canRunInBackground = False

    def isLicensed(self):
        return True

    def getParameterInfo(self):
        out_ws = arcpy.Parameter(displayName="Output workspace", name="out_ws",
            datatype="DEWorkspace", parameterType="Required", direction="Input")
        out_name = arcpy.Parameter(displayName="Test report table name", name="out_name",
            datatype="GPString", parameterType="Required", direction="Input")
        out_name.value = "ContourOptUnitTestReport"
        out_table = arcpy.Parameter(displayName="Test report table", name="out_table",
            datatype="DETable", parameterType="Derived", direction="Output")
        out_log = arcpy.Parameter(displayName="Log file path", name="out_log",
            datatype="GPString", parameterType="Derived", direction="Output")
        return [out_ws, out_name, out_table, out_log]

    def execute(self, parameters, messages):
        arcpy.env.overwriteOutput = True
        logger = None; ins = None
        try:
            out_ws = parameters[0].valueAsText
            out_name = parameters[1].valueAsText
            logger, log_path = _setup_logger(out_ws, "tests")
            _log_msg(messages, logger, "INFO", "Starting unit tests...")
            out_tbl = os.path.join(out_ws, out_name)
            if arcpy.Exists(out_tbl):
                arcpy.management.Delete(out_tbl)
            arcpy.management.CreateTable(out_ws, out_name)
            _add_fields(out_tbl, [("TEST", "TEXT", 128), ("STATUS", "TEXT", 16),
                                  ("DETAILS", "TEXT", 255)])
            ins = arcpy.da.InsertCursor(out_tbl, ["TEST", "STATUS", "DETAILS"])
            suite = unittest.TestLoader().loadTestsFromTestCase(_GeomTestCase)
            result = unittest.TestResult()
            suite.run(result)
            failed = set([t.id() for t, _ in result.failures] + [t.id() for t, _ in result.errors])
            all_tests = []
            for t in suite:
                all_tests.append(t.id())
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
                     f"Unit tests done. Failures={len(result.failures)} Errors={len(result.errors)}")
            parameters[2].value = out_tbl
            parameters[3].value = log_path
            return
        finally:
            try:
                if ins:
                    del ins
            except Exception:
                pass
            _shutdown_logger(logger)
