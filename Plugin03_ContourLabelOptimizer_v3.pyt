# -*- coding: ascii -*-
# ContourLabelOptimizer_v3_FIXED_UIUX.pyt
#
# ArcGIS Desktop / ArcMap Python Toolbox (Python 2.7)
#
# IMPORTANT FIXES (UI/Structure):
#   - Correct arcpy.Parameter(displayName, name, ...) order (ArcMap required)
#   - Added Toolbox.getTools()
#   - Added isLicensed() to tools
#   - Safer UI toggles + robust cursor cleanup
#
# Core behavior is unchanged.

import os
import math
import time
import datetime
import logging
import unittest

import arcpy

PT_TO_MM = 0.3527777778  # 1 point = 0.352777... mm


# ----------------------------------------------------------------------
# Logging helpers
# ----------------------------------------------------------------------

def _setup_logger(out_ws, tool_tag):
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = os.path.join(out_ws, "contour_opt_%s_%s.log" % (tool_tag, ts))

    logger_name = "ContourLabelOptimizer_%s_%s" % (tool_tag, ts)
    logger = logging.getLogger(logger_name)
    logger.setLevel(logging.DEBUG)

    fh = logging.FileHandler(log_path)
    fh.setLevel(logging.DEBUG)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    logger.info("Logger initialized. Log file: %s", log_path)
    return logger, log_path


def _shutdown_logger(logger):
    # Close handlers to avoid file locks / handle leaks in ArcMap session
    try:
        if not logger:
            return
        handlers = list(logger.handlers)
        for h in handlers:
            try:
                h.flush()
                h.close()
            except Exception:
                pass
            try:
                logger.removeHandler(h)
            except Exception:
                pass
    except Exception:
        pass


def _log_msg(messages, logger, level, text):
    # Log to file
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
    except Exception:
        pass

    # Log to ArcGIS messages
    try:
        if messages:
            if level == "ERROR":
                messages.addErrorMessage(text)
            elif level == "WARN":
                messages.addWarningMessage(text)
            else:
                messages.addMessage(text)
    except Exception:
        pass


# ----------------------------------------------------------------------
# Geometry + math helpers
# ----------------------------------------------------------------------

def _is_geographic(sr):
    try:
        return sr and sr.type == "Geographic"
    except Exception:
        return False


def _linear_unit_name(sr):
    try:
        if sr and hasattr(sr, "linearUnitName"):
            return sr.linearUnitName
    except Exception:
        pass
    return "unknown"


def _meters_per_unit(sr):
    try:
        mpu = float(sr.metersPerUnit)
        if mpu > 0:
            return mpu
    except Exception:
        pass
    return None


def _safe_mm_to_units(mm_on_map, map_scale, meters_per_unit):
    mm = float(mm_on_map)
    ms = float(map_scale)
    mpu = float(meters_per_unit)
    safe_m = (mm * ms) / 1000.0
    return safe_m / mpu


def _meters_to_units(meters, meters_per_unit):
    return float(meters) / float(meters_per_unit)


def _deg(rad):
    return rad * 180.0 / math.pi


def _clamp(v, vmin, vmax):
    return max(vmin, min(vmax, v))


def _dist2d(p1, p2):
    dx = (p2.X - p1.X)
    dy = (p2.Y - p1.Y)
    return math.sqrt(dx * dx + dy * dy)


def _tangent_angle_at_distance(polyline, dist_along, eps_units):
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
    return math.atan2((p1.Y - p0.Y), (p1.X - p0.X))


def _make_oriented_rect(center_pt, angle_rad, half_len, half_h, sr):
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


def _sample_points_along(polyline, step_units):
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


# Curvature methods (lower is better)
def _curv_chord_ratio(seg):
    try:
        L = float(seg.length)
        if L <= 0:
            return 0.0
        p0 = seg.firstPoint
        p1 = seg.lastPoint
        C = _dist2d(p0, p1)
        return max(0.0, 1.0 - (C / L))
    except Exception:
        return 0.0


def _curv_max_deflection(seg, sample_units):
    try:
        pts = _sample_points_along(seg, sample_units)
        if len(pts) < 3:
            return 0.0
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
    except Exception:
        return 0.0


def _curv_energy(seg, sample_units):
    try:
        pts = _sample_points_along(seg, sample_units)
        if len(pts) < 3:
            return 0.0
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
        L = float(seg.length)
        if L <= 0:
            return 0.0
        return total_turn / L
    except Exception:
        return 0.0


def _compute_curvature(seg, method, sample_units, w_cr, w_md, w_ce):
    if method == "ChordRatio":
        return _curv_chord_ratio(seg)
    if method == "MaxDeflection":
        return _curv_max_deflection(seg, sample_units)
    if method == "CurvatureEnergy":
        return _curv_energy(seg, sample_units)

    cr = _curv_chord_ratio(seg)                    # ~0..1
    md = _curv_max_deflection(seg, sample_units)   # 0..pi
    ce = _curv_energy(seg, sample_units)           # ~0..?
    mdn = md / math.pi
    cen = min(1.0, ce * 2.0)

    ws = float(w_cr) + float(w_md) + float(w_ce)
    if ws <= 0:
        return 0.0
    return ((float(w_cr) * cr) + (float(w_md) * mdn) + (float(w_ce) * cen)) / ws


def _intersect_area(poly_a, poly_b, logger):
    if poly_a is None or poly_b is None:
        return 0.0
    try:
        if poly_a.disjoint(poly_b):
            return 0.0
        inter = poly_a.intersect(poly_b, 4)
        return float(inter.area) if inter else 0.0
    except arcpy.ExecuteError as e:
        logger.error("Intersection failed: %s", str(e))
        return 1e30
    except Exception as e:
        logger.error("Intersection failed (generic): %s", str(e))
        return 1e30


def _union_fc_to_geometry(fc, logger):
    geom = None
    try:
        with arcpy.da.SearchCursor(fc, ["SHAPE@"]) as cur:
            for (g,) in cur:
                if g is None:
                    continue
                if geom is None:
                    geom = g
                else:
                    try:
                        geom = geom.union(g)
                    except Exception as e:
                        logger.warning("Union part failed: %s", str(e))
    except Exception as e:
        logger.error("Union FC failed: %s", str(e))
    return geom


def _create_fc(workspace, name, geom_type, sr, logger):
    out_path = os.path.join(workspace, name)
    if arcpy.Exists(out_path):
        try:
            arcpy.Delete_management(out_path)
        except Exception as e:
            logger.error("Failed deleting existing output: %s", str(e))
            raise
    arcpy.CreateFeatureclass_management(workspace, name, geom_type, "", "DISABLED", "DISABLED", sr)
    return out_path


def _add_fields(fc, defs):
    for d in defs:
        if len(d) == 2:
            arcpy.AddField_management(fc, d[0], d[1])
        else:
            arcpy.AddField_management(fc, d[0], d[1], field_length=d[2])


def _derive_text_metrics_from_annotation(anno_fc, logger, max_samples=80):
    if not anno_fc:
        return None, None

    fields = [f.name for f in arcpy.ListFields(anno_fc)]
    text_field = None
    for cand in ["TextString", "TEXTSTRING", "Text", "TEXT"]:
        if cand in fields:
            text_field = cand
            break
    if not text_field:
        logger.warning("Annotation text field not found (expected TextString/TEXT).")
        return None, None

    heights = []
    cws = []
    n = 0
    with arcpy.da.SearchCursor(anno_fc, ["SHAPE@", text_field]) as cur:
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

    if not heights:
        logger.warning("No usable annotation samples to derive text metrics.")
        return None, None

    heights.sort()
    cws.sort()
    return heights[len(heights) // 2], cws[len(cws) // 2]


def _estimate_text_metrics_units(text_value, map_scale, mpu,
                                pad_units,
                                derive_metrics, derived_h_units, derived_cw,
                                font_size_pt, char_w_factor):
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


def _build_obstacle_mask(obstacle_layers, anno_layer, safe_units, scratch_gdb, logger, messages):
    layers = []
    if obstacle_layers:
        layers.extend([x for x in obstacle_layers if x])
    if anno_layer:
        layers.append(anno_layer)

    if not layers:
        return None, None

    buf_fcs = []
    for i, lyr in enumerate(layers):
        try:
            tmp_buf = os.path.join("in_memory", "buf_%d" % i)
            if arcpy.Exists(tmp_buf):
                arcpy.Delete_management(tmp_buf)
            arcpy.Buffer_analysis(lyr, tmp_buf, str(safe_units), dissolve_option="ALL")
            buf_fcs.append(tmp_buf)
        except Exception as e:
            _log_msg(messages, logger, "WARN", "Obstacle buffer failed for a layer: %s" % str(e))

    if not buf_fcs:
        return None, None

    merged = os.path.join("in_memory", "obs_merge")
    if arcpy.Exists(merged):
        arcpy.Delete_management(merged)
    arcpy.Merge_management(buf_fcs, merged)

    dissolved = os.path.join(scratch_gdb, "ObstacleMask")
    if arcpy.Exists(dissolved):
        arcpy.Delete_management(dissolved)
    arcpy.Dissolve_management(merged, dissolved)

    mask_geom = _union_fc_to_geometry(dissolved, logger)
    return dissolved, mask_geom


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


def _find_seed_from_annotation(anno_fc, part_geom, win_start, win_end, search_pad_units, logger):
    if not anno_fc:
        return None
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

        best_d = None
        best_dist = None
        with arcpy.da.SearchCursor(anno_fc, ["SHAPE@"]) as cur:
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
    except Exception as e:
        logger.warning("Seed from annotation failed: %s", str(e))
        return None


def _build_offsets(internal_step_units, max_tries):
    offs = [0.0]
    k = 1
    while len(offs) < max_tries:
        offs.append(float(k) * internal_step_units)
        if len(offs) >= max_tries:
            break
        offs.append(float(-k) * internal_step_units)
        k += 1
    return offs


def _score_candidate(part_geom, center_d, win_start, win_end,
                    foot_len, foot_h, eps_units,
                    curv_method, curv_sample_units,
                    w_cr, w_md, w_ce,
                    w_curv, w_ovlp, w_center,
                    max_ovlp, max_curv,
                    mask_geom, placed_footprints,
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
        if (seg_end - seg_start) < (0.10 * foot_len):
            logger.warning("Very short label-under segment (win [%0.2f,%0.2f], center %0.2f, foot_len %0.2f).",
                           win_start, win_end, center_d, foot_len)

    try:
        seg_geom = part_geom.segmentAlongLine(seg_start, seg_end, False)
    except Exception as e:
        logger.warning("segmentAlongLine failed (center %0.2f): %s", center_d, str(e))
        return None

    if not seg_geom or float(seg_geom.length) <= 0:
        logger.warning("Empty segment geometry produced (win [%0.2f,%0.2f], center %0.2f).", win_start, win_end, center_d)
        return None

    ang = _tangent_angle_at_distance(part_geom, center_d, eps_units)
    rot_deg = _deg(ang)

    center_pt = part_geom.positionAlongLine(center_d, False)
    foot_geom = _make_oriented_rect(center_pt, ang, 0.5 * foot_len, 0.5 * foot_h, sr)

    curv = _compute_curvature(seg_geom, curv_method, curv_sample_units, w_cr, w_md, w_ce)

    ovlp = 0.0
    if mask_geom:
        ovlp += _intersect_area(foot_geom, mask_geom, logger)

    if placed_footprints:
        ext_a = foot_geom.extent
        for pf in placed_footprints:
            if not pf:
                continue
            ext_b = pf.extent
            if ext_a and ext_b:
                if (ext_a.XMax < ext_b.XMin or ext_a.XMin > ext_b.XMax or
                        ext_a.YMax < ext_b.YMin or ext_a.YMin > ext_b.YMax):
                    continue
            ovlp += _intersect_area(foot_geom, pf, logger)

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
        "score": float(score)
    }


def _is_major_value(val, major_interval):
    try:
        x = float(val)
        mi = float(major_interval)
        if mi <= 0:
            return True
        r = abs(x % mi)
        return (r < 1e-6) or (abs(r - mi) < 1e-6)
    except Exception:
        return False


# ----------------------------------------------------------------------
# Toolbox and tools
# ----------------------------------------------------------------------

class Toolbox(object):
    def __init__(self):
        self.label = "Contour Label Optimizer v3"
        self.alias = "contourlabelopt3"
        self.tools = [
            OptimizeContourLabelAnchorsV3,
            ValidateLabelAnchors,
            CurvatureHeatmap,
            AutoGenerateAnnotation,
            RunUnitTests
        ]

    def getTools(self):
        return self.tools


class OptimizeContourLabelAnchorsV3(object):
    def __init__(self):
        self.label = "Optimize Contour Label Anchors (v3)"
        self.description = "Places one optimized label anchor per along-line interval window."
        self.canRunInBackground = False

    def isLicensed(self):
        return True

    def getParameterInfo(self):
        p = []

        in_contours = arcpy.Parameter(
            displayName="Contour lines",
            name="in_contours",
            datatype="GPFeatureLayer",
            parameterType="Required",
            direction="Input"
        )

        elev_field = arcpy.Parameter(
            displayName="Elevation field (label text source)",
            name="elev_field",
            datatype="Field",
            parameterType="Required",
            direction="Input"
        )
        elev_field.parameterDependencies = [in_contours.name]

        selection_mode = arcpy.Parameter(
            displayName="Selection mode",
            name="selection_mode",
            datatype="GPString",
            parameterType="Required",
            direction="Input"
        )
        selection_mode.filter.type = "ValueList"
        selection_mode.filter.list = ["ALL", "MAJOR_INTERVAL"]
        selection_mode.value = "ALL"
        selection_mode.category = "Selection"

        major_interval = arcpy.Parameter(
            displayName="Major interval (e.g., 100 for elev%100==0)",
            name="major_interval",
            datatype="GPDouble",
            parameterType="Optional",
            direction="Input"
        )
        major_interval.value = 100.0
        major_interval.enabled = False
        major_interval.category = "Selection"

        interval_m = arcpy.Parameter(
            displayName="Along-line interval (meters)",
            name="interval_m",
            datatype="GPDouble",
            parameterType="Required",
            direction="Input"
        )
        interval_m.value = 500.0

        safe_mm = arcpy.Parameter(
            displayName="Safe distance from obstacles (mm on map)",
            name="safe_mm",
            datatype="GPDouble",
            parameterType="Required",
            direction="Input"
        )
        safe_mm.value = 2.0

        halo_mm = arcpy.Parameter(
            displayName="Extra halo/mask margin (mm on map)",
            name="halo_mm",
            datatype="GPDouble",
            parameterType="Optional",
            direction="Input"
        )
        halo_mm.value = 0.0

        map_scale = arcpy.Parameter(
            displayName="Map scale denominator (e.g., 25000 for 1:25000)",
            name="map_scale",
            datatype="GPDouble",
            parameterType="Required",
            direction="Input"
        )
        map_scale.value = 25000.0

        obstacles = arcpy.Parameter(
            displayName="Obstacle layers (lines, polygons, points)",
            name="obstacles",
            datatype="GPFeatureLayer",
            parameterType="Optional",
            direction="Input",
            multiValue=True
        )
        obstacles.category = "Obstacles"

        anno_layer = arcpy.Parameter(
            displayName="Existing annotation layer (barrier + optional metrics)",
            name="anno_layer",
            datatype="GPFeatureLayer",
            parameterType="Optional",
            direction="Input"
        )
        anno_layer.category = "Obstacles"

        derive_metrics = arcpy.Parameter(
            displayName="Derive text metrics from annotation (if provided)",
            name="derive_text_metrics",
            datatype="GPBoolean",
            parameterType="Optional",
            direction="Input"
        )
        derive_metrics.value = True
        derive_metrics.category = "Text Metrics"

        font_size_pt = arcpy.Parameter(
            displayName="Font size (points) if metrics not derived",
            name="font_size_pt",
            datatype="GPDouble",
            parameterType="Optional",
            direction="Input"
        )
        font_size_pt.value = 8.0
        font_size_pt.category = "Text Metrics"

        char_w_factor = arcpy.Parameter(
            displayName="Average character width factor if metrics not derived",
            name="char_w_factor",
            datatype="GPDouble",
            parameterType="Optional",
            direction="Input"
        )
        char_w_factor.value = 0.6
        char_w_factor.category = "Text Metrics"

        curv_method = arcpy.Parameter(
            displayName="Curvature method",
            name="curv_method",
            datatype="GPString",
            parameterType="Required",
            direction="Input"
        )
        curv_method.filter.type = "ValueList"
        curv_method.filter.list = ["Hybrid", "ChordRatio", "MaxDeflection", "CurvatureEnergy"]
        curv_method.value = "Hybrid"
        curv_method.category = "Curvature"

        curv_sample_m = arcpy.Parameter(
            displayName="Curvature sampling step (meters)",
            name="curv_sample_m",
            datatype="GPDouble",
            parameterType="Required",
            direction="Input"
        )
        curv_sample_m.value = 5.0
        curv_sample_m.category = "Curvature"

        w_cr = arcpy.Parameter(
            displayName="Hybrid weight: chord ratio",
            name="w_cr",
            datatype="GPDouble",
            parameterType="Required",
            direction="Input"
        )
        w_cr.value = 0.5
        w_cr.category = "Curvature"

        w_md = arcpy.Parameter(
            displayName="Hybrid weight: max deflection",
            name="w_md",
            datatype="GPDouble",
            parameterType="Required",
            direction="Input"
        )
        w_md.value = 0.3
        w_md.category = "Curvature"

        w_ce = arcpy.Parameter(
            displayName="Hybrid weight: curvature energy",
            name="w_ce",
            datatype="GPDouble",
            parameterType="Required",
            direction="Input"
        )
        w_ce.value = 0.2
        w_ce.category = "Curvature"

        w_curv = arcpy.Parameter(
            displayName="Weight: curvature",
            name="w_curv",
            datatype="GPDouble",
            parameterType="Required",
            direction="Input"
        )
        w_curv.value = 1.0
        w_curv.category = "Scoring"

        w_ovlp = arcpy.Parameter(
            displayName="Weight: overlap area",
            name="w_ovlp",
            datatype="GPDouble",
            parameterType="Required",
            direction="Input"
        )
        w_ovlp.value = 5.0
        w_ovlp.category = "Scoring"

        w_center = arcpy.Parameter(
            displayName="Weight: window-center preference",
            name="w_center",
            datatype="GPDouble",
            parameterType="Required",
            direction="Input"
        )
        w_center.value = 0.25
        w_center.category = "Scoring"

        max_ovlp = arcpy.Parameter(
            displayName="Max allowed overlap area (linear_unit^2)",
            name="max_ovlp",
            datatype="GPDouble",
            parameterType="Optional",
            direction="Input"
        )
        max_ovlp.category = "Thresholds"

        max_curv = arcpy.Parameter(
            displayName="Max allowed curvature score",
            name="max_curv",
            datatype="GPDouble",
            parameterType="Optional",
            direction="Input"
        )
        max_curv.category = "Thresholds"

        min_contour_m = arcpy.Parameter(
            displayName="Minimum contour part length (meters)",
            name="min_contour_m",
            datatype="GPDouble",
            parameterType="Optional",
            direction="Input"
        )
        min_contour_m.value = 0.0
        min_contour_m.category = "Thresholds"

        short_policy = arcpy.Parameter(
            displayName="Short part policy",
            name="short_policy",
            datatype="GPString",
            parameterType="Required",
            direction="Input"
        )
        short_policy.filter.type = "ValueList"
        short_policy.filter.list = ["PLACE_CENTER", "SKIP"]
        short_policy.value = "PLACE_CENTER"
        short_policy.category = "Thresholds"

        out_ws = arcpy.Parameter(
            displayName="Output workspace (file geodatabase recommended)",
            name="out_ws",
            datatype="DEWorkspace",
            parameterType="Required",
            direction="Input"
        )

        out_segments_name = arcpy.Parameter(
            displayName="Output segments name",
            name="out_segments_name",
            datatype="GPString",
            parameterType="Required",
            direction="Input"
        )
        out_segments_name.value = "ContourLabelSegments"

        out_points_name = arcpy.Parameter(
            displayName="Output points name",
            name="out_points_name",
            datatype="GPString",
            parameterType="Required",
            direction="Input"
        )
        out_points_name.value = "ContourLabelPoints"

        make_footprints = arcpy.Parameter(
            displayName="Create QA footprints (polygons)",
            name="make_footprints",
            datatype="GPBoolean",
            parameterType="Optional",
            direction="Input"
        )
        make_footprints.value = False
        make_footprints.category = "Outputs"

        out_footprints_name = arcpy.Parameter(
            displayName="QA footprints name (if enabled)",
            name="out_footprints_name",
            datatype="GPString",
            parameterType="Optional",
            direction="Input"
        )
        out_footprints_name.value = "ContourLabelFootprints"
        out_footprints_name.enabled = False
        out_footprints_name.category = "Outputs"

        make_stats = arcpy.Parameter(
            displayName="Create statistics table",
            name="make_stats",
            datatype="GPBoolean",
            parameterType="Optional",
            direction="Input"
        )
        make_stats.value = True
        make_stats.category = "Outputs"

        out_stats_name = arcpy.Parameter(
            displayName="Statistics table name (if enabled)",
            name="out_stats_name",
            datatype="GPString",
            parameterType="Optional",
            direction="Input"
        )
        out_stats_name.value = "ContourLabelStats"
        out_stats_name.enabled = True
        out_stats_name.category = "Outputs"

        max_tries = arcpy.Parameter(
            displayName="Internal tries per window (refinement around seed)",
            name="max_tries",
            datatype="GPLong",
            parameterType="Required",
            direction="Input"
        )
        max_tries.value = 11
        max_tries.category = "Advanced"

        # Derived outputs
        out_segments = arcpy.Parameter(
            displayName="Output segments",
            name="out_segments",
            datatype="DEFeatureClass",
            parameterType="Derived",
            direction="Output"
        )
        out_points = arcpy.Parameter(
            displayName="Output points",
            name="out_points",
            datatype="DEFeatureClass",
            parameterType="Derived",
            direction="Output"
        )
        out_footprints = arcpy.Parameter(
            displayName="Output footprints",
            name="out_footprints",
            datatype="DEFeatureClass",
            parameterType="Derived",
            direction="Output"
        )
        out_stats = arcpy.Parameter(
            displayName="Output statistics table",
            name="out_stats",
            datatype="DETable",
            parameterType="Derived",
            direction="Output"
        )
        out_log = arcpy.Parameter(
            displayName="Log file path",
            name="out_log",
            datatype="GPString",
            parameterType="Derived",
            direction="Output"
        )

        p.extend([
            in_contours, elev_field,
            selection_mode, major_interval,
            interval_m, safe_mm, halo_mm, map_scale,
            obstacles, anno_layer,
            derive_metrics, font_size_pt, char_w_factor,
            curv_method, curv_sample_m, w_cr, w_md, w_ce,
            w_curv, w_ovlp, w_center,
            max_ovlp, max_curv, min_contour_m, short_policy,
            out_ws, out_segments_name, out_points_name,
            make_footprints, out_footprints_name,
            make_stats, out_stats_name,
            max_tries,
            out_segments, out_points, out_footprints, out_stats, out_log
        ])
        return p

    def updateParameters(self, parameters):
        # Indices based on getParameterInfo order above
        IDX_SEL_MODE = 2
        IDX_MAJOR = 3
        IDX_MAKE_FOOT = 28
        IDX_FOOT_NAME = 29
        IDX_MAKE_STATS = 30
        IDX_STATS_NAME = 31

        sel_mode = parameters[IDX_SEL_MODE].valueAsText
        parameters[IDX_MAJOR].enabled = (sel_mode == "MAJOR_INTERVAL")

        parameters[IDX_FOOT_NAME].enabled = bool(parameters[IDX_MAKE_FOOT].value)
        parameters[IDX_STATS_NAME].enabled = bool(parameters[IDX_MAKE_STATS].value)
        return

    def updateMessages(self, parameters):
        try:
            contours = parameters[0].valueAsText
            if contours:
                sr = arcpy.Describe(contours).spatialReference
                if _is_geographic(sr):
                    parameters[0].setErrorMessage("Input contours must be projected (linear units).")
                else:
                    unit_name = _linear_unit_name(sr)
                    # warn about overlap units
                    if parameters[21].value is not None:
                        parameters[21].setWarningMessage("max_ovlp is in %s^2." % unit_name)

            sel_mode = parameters[2].valueAsText
            if sel_mode == "MAJOR_INTERVAL":
                if parameters[3].value is None or float(parameters[3].value) <= 0:
                    parameters[3].setErrorMessage("Major interval must be > 0 when Selection mode is MAJOR_INTERVAL.")
        except Exception:
            pass
        return

    def execute(self, parameters, messages):
        arcpy.env.overwriteOutput = True

        logger = None
        seg_ins = None
        pt_ins = None
        foot_ins = None
        stats_ins = None

        try:
            in_contours = parameters[0].valueAsText
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

            logger, log_path = _setup_logger(out_ws, "opt")
            _log_msg(messages, logger, "INFO", "Starting OptimizeContourLabelAnchorsV3")

            # Validate basics
            if interval_m <= 0:
                raise Exception("Along-line interval must be > 0.")
            if safe_mm < 0 or halo_mm < 0:
                raise Exception("Safe distance and halo must be >= 0.")
            if map_scale <= 0:
                raise Exception("Map scale must be > 0.")
            if max_tries < 1:
                raise Exception("Internal tries must be >= 1.")
            if selection_mode == "MAJOR_INTERVAL":
                if major_interval is None or float(major_interval) <= 0:
                    raise Exception("Major interval must be > 0 for MAJOR_INTERVAL mode.")

            desc = arcpy.Describe(in_contours)
            sr = desc.spatialReference
            if _is_geographic(sr):
                raise Exception("Projected coordinate system is required.")
            mpu = _meters_per_unit(sr)
            if not mpu or mpu <= 0:
                raise Exception("Could not determine meters-per-unit from spatial reference.")

            unit_name = _linear_unit_name(sr)
            _log_msg(messages, logger, "INFO", "Linear unit: %s (metersPerUnit=%s)" % (unit_name, str(mpu)))
            _log_msg(messages, logger, "INFO", "max_ovlp unit is %s^2" % unit_name)

            safe_units = _safe_mm_to_units(safe_mm, map_scale, mpu)
            halo_units = _safe_mm_to_units(halo_mm, map_scale, mpu) if halo_mm > 0 else 0.0
            pad_units = safe_units + halo_units

            interval_units = _meters_to_units(interval_m, mpu)
            curv_sample_units = _meters_to_units(curv_sample_m, mpu)
            min_contour_units = _meters_to_units(min_contour_m, mpu)

            _log_msg(messages, logger, "INFO",
                     "interval_units=%0.4f safe_units=%0.4f halo_units=%0.4f pad_units=%0.4f" %
                     (interval_units, safe_units, halo_units, pad_units))

            derived_h_units, derived_cw = (None, None)
            if derive_metrics and anno_layer:
                _log_msg(messages, logger, "INFO", "Deriving text metrics from annotation...")
                derived_h_units, derived_cw = _derive_text_metrics_from_annotation(anno_layer, logger, max_samples=80)
                if derived_h_units and derived_cw:
                    _log_msg(messages, logger, "INFO", "Derived: height_units=%0.4f char_w=%0.4f" %
                             (derived_h_units, derived_cw))
                else:
                    _log_msg(messages, logger, "WARN", "Could not derive metrics; using font estimate.")
                    derived_h_units, derived_cw = (None, None)

            scratch_gdb = arcpy.env.scratchGDB
            mask_fc, mask_geom = _build_obstacle_mask(obstacle_layers, anno_layer, safe_units, scratch_gdb, logger, messages)
            if mask_geom:
                _log_msg(messages, logger, "INFO", "Obstacle mask created.")
            else:
                _log_msg(messages, logger, "INFO", "No obstacle mask (no obstacles/annotation or buffer failed).")

            out_segments_fc = _create_fc(out_ws, out_segments_name, "POLYLINE", sr, logger)
            out_points_fc = _create_fc(out_ws, out_points_name, "POINT", sr, logger)

            seg_fields = [
                ("SRCID", "LONG"),
                ("PARTID", "LONG"),
                ("ELEV", "TEXT", 64),
                ("WSTART", "DOUBLE"),
                ("WEND", "DOUBLE"),
                ("CDIST", "DOUBLE"),
                ("ANG", "DOUBLE"),
                ("CURV", "DOUBLE"),
                ("OVLP", "DOUBLE"),
                ("SCORE", "DOUBLE"),
                ("TEXT", "TEXT", 128),
            ]
            pt_fields = seg_fields + [("ROT", "DOUBLE")]

            _add_fields(out_segments_fc, seg_fields)
            _add_fields(out_points_fc, pt_fields)

            out_foot_fc = None
            if make_footprints:
                out_foot_fc = _create_fc(out_ws, out_footprints_name, "POLYGON", sr, logger)
                _add_fields(out_foot_fc, [("SRCID", "LONG"), ("PARTID", "LONG"), ("TEXT", "TEXT", 128),
                                          ("SCORE", "DOUBLE"), ("OVLP", "DOUBLE"), ("CURV", "DOUBLE")])

            out_stats_tbl = None
            if make_stats:
                out_stats_tbl = os.path.join(out_ws, out_stats_name)
                if arcpy.Exists(out_stats_tbl):
                    arcpy.Delete_management(out_stats_tbl)
                arcpy.CreateTable_management(out_ws, out_stats_name)
                _add_fields(out_stats_tbl, [
                    ("SRCID", "LONG"),
                    ("PARTID", "LONG"),
                    ("WINCNT", "LONG"),
                    ("PLACED", "LONG"),
                    ("AVGSC", "DOUBLE"),
                    ("MAXSC", "DOUBLE"),
                    ("AVGOV", "DOUBLE"),
                    ("MAXOV", "DOUBLE"),
                    ("AVGCURV", "DOUBLE"),
                    ("MAXCURV", "DOUBLE"),
                    ("SECS", "DOUBLE")
                ])

            seg_ins = arcpy.da.InsertCursor(out_segments_fc,
                                            ["SHAPE@", "SRCID", "PARTID", "ELEV", "WSTART", "WEND", "CDIST",
                                             "ANG", "CURV", "OVLP", "SCORE", "TEXT"])
            pt_ins = arcpy.da.InsertCursor(out_points_fc,
                                           ["SHAPE@", "SRCID", "PARTID", "ELEV", "WSTART", "WEND", "CDIST",
                                            "ANG", "CURV", "OVLP", "SCORE", "TEXT", "ROT"])
            if out_foot_fc:
                foot_ins = arcpy.da.InsertCursor(out_foot_fc, ["SHAPE@", "SRCID", "PARTID", "TEXT", "SCORE", "OVLP", "CURV"])
            if out_stats_tbl:
                stats_ins = arcpy.da.InsertCursor(out_stats_tbl,
                                                  ["SRCID", "PARTID", "WINCNT", "PLACED",
                                                   "AVGSC", "MAXSC", "AVGOV", "MAXOV",
                                                   "AVGCURV", "MAXCURV", "SECS"])

            total_features = int(arcpy.GetCount_management(in_contours).getOutput(0))
            arcpy.SetProgressor("step", "Optimizing contour label anchors...", 0, max(1, total_features), 1)

            placed_footprints = []
            eps_units = max(0.001 * interval_units, 0.5 * curv_sample_units)

            fields = ["OID@", "SHAPE@", elev_field]
            with arcpy.da.SearchCursor(in_contours, fields) as cur:
                for oid, geom, elev in cur:
                    t0 = time.time()
                    arcpy.SetProgressorLabel("Processing contour OID %s" % str(oid))
                    arcpy.SetProgressorPosition()

                    if not geom:
                        _log_msg(messages, logger, "WARN", "OID %s has null geometry; skipping." % str(oid))
                        continue

                    if selection_mode == "MAJOR_INTERVAL":
                        if not _is_major_value(elev, major_interval):
                            continue

                    label_text = "" if elev is None else str(elev)

                    try:
                        text_h_units, text_w_units, _pad = _estimate_text_metrics_units(
                            label_text, map_scale, mpu,
                            pad_units,
                            derive_metrics, derived_h_units, derived_cw,
                            font_size_pt, char_w_factor
                        )
                    except Exception as e:
                        _log_msg(messages, logger, "ERROR", "Text metric estimate failed for OID %s: %s" % (str(oid), str(e)))
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
                            _log_msg(messages, logger, "WARN", "OID %s part %s has zero length; skipping." %
                                     (str(oid), str(part_id)))
                            continue

                        if min_contour_units > 0 and total_len < min_contour_units:
                            if short_policy == "SKIP":
                                _log_msg(messages, logger, "INFO", "OID %s part %s shorter than minimum; skipped." %
                                         (str(oid), str(part_id)))
                                continue
                            _log_msg(messages, logger, "WARN", "OID %s part %s shorter than minimum; placing center." %
                                     (str(oid), str(part_id)))

                        win_count = 0
                        placed_count = 0
                        scores = []
                        ovlps = []
                        curvs = []

                        win_start = 0.0
                        while win_start < total_len:
                            win_end = min(win_start + interval_units, total_len)
                            win_len = win_end - win_start
                            if win_len <= 0:
                                break
                            win_count += 1

                            seed_d = None
                            if anno_layer:
                                seed_d = _find_seed_from_annotation(anno_layer, part_geom, win_start, win_end, safe_units, logger)
                            if seed_d is None:
                                seed_d = win_start + 0.5 * win_len

                            best = None

                            # Pass 1: respect thresholds
                            for off in offsets:
                                d = float(seed_d) + float(off)
                                d = _clamp(d, win_start, win_end)
                                res = _score_candidate(part_geom, d, win_start, win_end,
                                                       foot_len, foot_h, eps_units,
                                                       curv_method, curv_sample_units,
                                                       w_cr, w_md, w_ce,
                                                       w_curv, w_ovlp, w_center,
                                                       max_ovlp, max_curv,
                                                       mask_geom, placed_footprints,
                                                       sr, logger)
                                if not res:
                                    continue
                                if best is None or res["score"] < best["score"]:
                                    best = res

                            # Pass 2: ignore thresholds if needed
                            if best is None and (max_ovlp is not None or max_curv is not None):
                                for off in offsets:
                                    d = float(seed_d) + float(off)
                                    d = _clamp(d, win_start, win_end)
                                    res = _score_candidate(part_geom, d, win_start, win_end,
                                                           foot_len, foot_h, eps_units,
                                                           curv_method, curv_sample_units,
                                                           w_cr, w_md, w_ce,
                                                           w_curv, w_ovlp, w_center,
                                                           None, None,
                                                           mask_geom, placed_footprints,
                                                           sr, logger)
                                    if not res:
                                        continue
                                    if best is None or res["score"] < best["score"]:
                                        best = res

                            # Final fallback: center only
                            if best is None:
                                center_d = win_start + 0.5 * win_len
                                best = _score_candidate(part_geom, center_d, win_start, win_end,
                                                        foot_len, foot_h, eps_units,
                                                        curv_method, curv_sample_units,
                                                        w_cr, w_md, w_ce,
                                                        w_curv, w_ovlp, w_center,
                                                        None, None,
                                                        mask_geom, placed_footprints,
                                                        sr, logger)

                            if best:
                                placed_count += 1
                                scores.append(best["score"])
                                ovlps.append(best["ovlp"])
                                curvs.append(best["curv"])

                                seg_geom = best["seg_geom"]
                                ang_deg = best["ang_deg"]
                                cd = best["center_d"]

                                seg_ins.insertRow([seg_geom, oid, part_id, label_text, win_start, win_end,
                                                   cd, ang_deg, best["curv"], best["ovlp"], best["score"], label_text])

                                pt = part_geom.positionAlongLine(cd, False)
                                pt_ins.insertRow([pt, oid, part_id, label_text, win_start, win_end,
                                                  cd, ang_deg, best["curv"], best["ovlp"], best["score"], label_text, ang_deg])

                                if foot_ins and best["foot_geom"]:
                                    foot_ins.insertRow([best["foot_geom"], oid, part_id, label_text, best["score"], best["ovlp"], best["curv"]])

                                if best["foot_geom"]:
                                    placed_footprints.append(best["foot_geom"])
                            else:
                                logger.warning("No placement found for OID %s part %s window [%0.2f,%0.2f].",
                                               str(oid), str(part_id), win_start, win_end)

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
                                avg_sc = 0.0
                                max_sc = 0.0
                                avg_ov = 0.0
                                max_ov = 0.0
                                avg_cv = 0.0
                                max_cv = 0.0
                            stats_ins.insertRow([oid, part_id, win_count, placed_count,
                                                 avg_sc, max_sc, avg_ov, max_ov, avg_cv, max_cv, secs])

                    _log_msg(messages, logger, "INFO", "Processed OID %s in %0.2f sec" % (str(oid), time.time() - t0))

            _log_msg(messages, logger, "INFO", "Completed optimization.")
            _log_msg(messages, logger, "INFO", "Segments: %s" % out_segments_fc)
            _log_msg(messages, logger, "INFO", "Points: %s" % out_points_fc)

            parameters[33].value = out_segments_fc
            parameters[34].value = out_points_fc
            parameters[35].value = out_foot_fc if out_foot_fc else ""
            parameters[36].value = out_stats_tbl if out_stats_tbl else ""
            parameters[37].value = log_path
            return

        finally:
            try:
                if seg_ins:
                    del seg_ins
                if pt_ins:
                    del pt_ins
                if foot_ins:
                    del foot_ins
                if stats_ins:
                    del stats_ins
            except Exception:
                pass
            _shutdown_logger(logger)


# -------------------- QA / Other Tools --------------------

class ValidateLabelAnchors(object):
    def __init__(self):
        self.label = "Validate Label Anchors (QA)"
        self.description = "Checks overlaps for anchor footprints and outputs a QA table."
        self.canRunInBackground = False

    def isLicensed(self):
        return True

    def getParameterInfo(self):
        p = []

        in_points = arcpy.Parameter(
            displayName="Anchor points (output from optimizer)",
            name="in_points",
            datatype="GPFeatureLayer",
            parameterType="Required",
            direction="Input"
        )

        text_field = arcpy.Parameter(
            displayName="Text field",
            name="text_field",
            datatype="Field",
            parameterType="Required",
            direction="Input"
        )
        text_field.parameterDependencies = [in_points.name]

        safe_mm = arcpy.Parameter(
            displayName="Safe distance from obstacles (mm on map)",
            name="safe_mm",
            datatype="GPDouble",
            parameterType="Required",
            direction="Input"
        )
        safe_mm.value = 2.0

        halo_mm = arcpy.Parameter(
            displayName="Extra halo/mask margin (mm on map)",
            name="halo_mm",
            datatype="GPDouble",
            parameterType="Optional",
            direction="Input"
        )
        halo_mm.value = 0.0

        map_scale = arcpy.Parameter(
            displayName="Map scale denominator",
            name="map_scale",
            datatype="GPDouble",
            parameterType="Required",
            direction="Input"
        )
        map_scale.value = 25000.0

        font_size_pt = arcpy.Parameter(
            displayName="Font size (points) for footprint estimate",
            name="font_size_pt",
            datatype="GPDouble",
            parameterType="Required",
            direction="Input"
        )
        font_size_pt.value = 8.0

        char_w_factor = arcpy.Parameter(
            displayName="Average character width factor",
            name="char_w_factor",
            datatype="GPDouble",
            parameterType="Required",
            direction="Input"
        )
        char_w_factor.value = 0.6

        obstacles = arcpy.Parameter(
            displayName="Obstacle layers (lines, polygons, points)",
            name="obstacles",
            datatype="GPFeatureLayer",
            parameterType="Optional",
            direction="Input",
            multiValue=True
        )

        out_ws = arcpy.Parameter(
            displayName="Output workspace",
            name="out_ws",
            datatype="DEWorkspace",
            parameterType="Required",
            direction="Input"
        )

        out_table_name = arcpy.Parameter(
            displayName="QA report table name",
            name="out_table_name",
            datatype="GPString",
            parameterType="Required",
            direction="Input"
        )
        out_table_name.value = "LabelAnchorQA"

        out_table = arcpy.Parameter(
            displayName="QA report table",
            name="out_table",
            datatype="DETable",
            parameterType="Derived",
            direction="Output"
        )

        out_log = arcpy.Parameter(
            displayName="Log file path",
            name="out_log",
            datatype="GPString",
            parameterType="Derived",
            direction="Output"
        )

        p.extend([in_points, text_field, safe_mm, halo_mm, map_scale, font_size_pt, char_w_factor,
                  obstacles, out_ws, out_table_name, out_table, out_log])
        return p

    def updateMessages(self, parameters):
        in_points = parameters[0].valueAsText
        if in_points:
            try:
                sr = arcpy.Describe(in_points).spatialReference
                if _is_geographic(sr):
                    parameters[0].setErrorMessage("Input points must be projected (linear units).")
            except Exception:
                pass
        return

    def execute(self, parameters, messages):
        arcpy.env.overwriteOutput = True

        logger = None
        ins = None
        try:
            in_points = parameters[0].valueAsText
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
            _log_msg(messages, logger, "INFO", "Starting ValidateLabelAnchors")

            desc = arcpy.Describe(in_points)
            sr = desc.spatialReference
            if _is_geographic(sr):
                raise Exception("Projected coordinate system is required.")
            mpu = _meters_per_unit(sr)
            if not mpu or mpu <= 0:
                raise Exception("Could not determine meters-per-unit from spatial reference.")

            safe_units = _safe_mm_to_units(safe_mm, map_scale, mpu)
            halo_units = _safe_mm_to_units(halo_mm, map_scale, mpu) if halo_mm > 0 else 0.0
            pad_units = safe_units + halo_units

            scratch_gdb = arcpy.env.scratchGDB
            mask_fc, mask_geom = _build_obstacle_mask(obstacle_layers, None, safe_units, scratch_gdb, logger, messages)

            out_tbl = os.path.join(out_ws, out_table_name)
            if arcpy.Exists(out_tbl):
                arcpy.Delete_management(out_tbl)
            arcpy.CreateTable_management(out_ws, out_table_name)
            _add_fields(out_tbl, [
                ("OID", "LONG"),
                ("TEXT", "TEXT", 128),
                ("OVLP", "DOUBLE"),
                ("SELFOV", "DOUBLE"),
                ("FLAG", "TEXT", 32)
            ])

            ins = arcpy.da.InsertCursor(out_tbl, ["OID", "TEXT", "OVLP", "SELFOV", "FLAG"])

            footprints = []
            fields = ["OID@", "SHAPE@", text_field]
            fld_names = [f.name for f in arcpy.ListFields(in_points)]
            has_rot = ("ROT" in fld_names)
            if has_rot:
                fields.append("ROT")

            with arcpy.da.SearchCursor(in_points, fields) as cur:
                for row in cur:
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
                    w_units = max(1, len(txt)) * float(char_w_factor) * h_units

                    foot_len = w_units + 2.0 * pad_units
                    foot_h = h_units + 2.0 * pad_units

                    ang = math.radians(float(rot)) if rot is not None else 0.0
                    foot = _make_oriented_rect(pt, ang, 0.5 * foot_len, 0.5 * foot_h, sr)

                    ovlp = 0.0
                    if mask_geom:
                        ovlp = _intersect_area(foot, mask_geom, logger)

                    selfov = 0.0
                    ext_a = foot.extent
                    for pf in footprints:
                        ext_b = pf.extent
                        if ext_a and ext_b:
                            if (ext_a.XMax < ext_b.XMin or ext_a.XMin > ext_b.XMax or
                                    ext_a.YMax < ext_b.YMin or ext_a.YMin > ext_b.YMax):
                                continue
                        selfov += _intersect_area(foot, pf, logger)

                    flag = "OK"
                    if ovlp > 0.0:
                        flag = "OBSTACLE"
                    if selfov > 0.0:
                        flag = "SELF"
                    if ovlp > 0.0 and selfov > 0.0:
                        flag = "BOTH"

                    ins.insertRow([oid, txt, ovlp, selfov, flag])
                    footprints.append(foot)

            _log_msg(messages, logger, "INFO", "QA table: %s" % out_tbl)

            parameters[10].value = out_tbl
            parameters[11].value = log_path
            return

        finally:
            try:
                if ins:
                    del ins
            except Exception:
                pass
            _shutdown_logger(logger)


class CurvatureHeatmap(object):
    def __init__(self):
        self.label = "Curvature Heatmap (QA)"
        self.description = "Creates line segments with curvature values for visualization."
        self.canRunInBackground = False

    def isLicensed(self):
        return True

    def getParameterInfo(self):
        p = []

        in_contours = arcpy.Parameter(
            displayName="Contour lines",
            name="in_contours",
            datatype="GPFeatureLayer",
            parameterType="Required",
            direction="Input"
        )

        step_m = arcpy.Parameter(
            displayName="Step (meters) along contour",
            name="step_m",
            datatype="GPDouble",
            parameterType="Required",
            direction="Input"
        )
        step_m.value = 20.0

        seg_len_m = arcpy.Parameter(
            displayName="Segment length for curvature (meters)",
            name="seg_len_m",
            datatype="GPDouble",
            parameterType="Required",
            direction="Input"
        )
        seg_len_m.value = 60.0

        curv_method = arcpy.Parameter(
            displayName="Curvature method",
            name="curv_method",
            datatype="GPString",
            parameterType="Required",
            direction="Input"
        )
        curv_method.filter.type = "ValueList"
        curv_method.filter.list = ["Hybrid", "ChordRatio", "MaxDeflection", "CurvatureEnergy"]
        curv_method.value = "Hybrid"

        curv_sample_m = arcpy.Parameter(
            displayName="Curvature sampling step (meters)",
            name="curv_sample_m",
            datatype="GPDouble",
            parameterType="Required",
            direction="Input"
        )
        curv_sample_m.value = 5.0

        w_cr = arcpy.Parameter(
            displayName="Hybrid weight: chord ratio",
            name="w_cr",
            datatype="GPDouble",
            parameterType="Required",
            direction="Input"
        )
        w_cr.value = 0.5

        w_md = arcpy.Parameter(
            displayName="Hybrid weight: max deflection",
            name="w_md",
            datatype="GPDouble",
            parameterType="Required",
            direction="Input"
        )
        w_md.value = 0.3

        w_ce = arcpy.Parameter(
            displayName="Hybrid weight: curvature energy",
            name="w_ce",
            datatype="GPDouble",
            parameterType="Required",
            direction="Input"
        )
        w_ce.value = 0.2

        out_ws = arcpy.Parameter(
            displayName="Output workspace",
            name="out_ws",
            datatype="DEWorkspace",
            parameterType="Required",
            direction="Input"
        )

        out_name = arcpy.Parameter(
            displayName="Output heatmap segments name",
            name="out_name",
            datatype="GPString",
            parameterType="Required",
            direction="Input"
        )
        out_name.value = "ContourCurvatureHeat"

        out_fc = arcpy.Parameter(
            displayName="Output heatmap segments",
            name="out_fc",
            datatype="DEFeatureClass",
            parameterType="Derived",
            direction="Output"
        )

        out_log = arcpy.Parameter(
            displayName="Log file path",
            name="out_log",
            datatype="GPString",
            parameterType="Derived",
            direction="Output"
        )

        p.extend([in_contours, step_m, seg_len_m, curv_method, curv_sample_m, w_cr, w_md, w_ce,
                  out_ws, out_name, out_fc, out_log])
        return p

    def execute(self, parameters, messages):
        arcpy.env.overwriteOutput = True

        logger = None
        ins = None

        try:
            in_contours = parameters[0].valueAsText
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
            _log_msg(messages, logger, "INFO", "Starting CurvatureHeatmap")

            desc = arcpy.Describe(in_contours)
            sr = desc.spatialReference
            if _is_geographic(sr):
                raise Exception("Projected coordinate system is required.")
            mpu = _meters_per_unit(sr)
            if not mpu or mpu <= 0:
                raise Exception("Could not determine meters-per-unit from spatial reference.")

            step_units = _meters_to_units(step_m, mpu)
            seg_len_units = _meters_to_units(seg_len_m, mpu)
            curv_sample_units = _meters_to_units(curv_sample_m, mpu)

            out_fc = _create_fc(out_ws, out_name, "POLYLINE", sr, logger)
            _add_fields(out_fc, [("SRCID", "LONG"), ("PARTID", "LONG"), ("D", "DOUBLE"), ("CURV", "DOUBLE")])

            ins = arcpy.da.InsertCursor(out_fc, ["SHAPE@", "SRCID", "PARTID", "D", "CURV"])

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
                            d += step_units

            _log_msg(messages, logger, "INFO", "Heatmap output: %s" % out_fc)

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


class AutoGenerateAnnotation(object):
    def __init__(self):
        self.label = "Auto-Generate Annotation (Cartography)"
        self.description = "Converts labels to annotation using TiledLabelsToAnnotation_cartography."
        self.canRunInBackground = False

    def isLicensed(self):
        return True

    def getParameterInfo(self):
        p = []

        map_document = arcpy.Parameter(
            displayName="Map document (MXD path or CURRENT)",
            name="map_document",
            datatype="GPString",
            parameterType="Required",
            direction="Input"
        )
        map_document.value = "CURRENT"

        data_frame = arcpy.Parameter(
            displayName="Data frame name",
            name="data_frame",
            datatype="GPString",
            parameterType="Required",
            direction="Input"
        )
        data_frame.value = "Layers"

        out_gdb = arcpy.Parameter(
            displayName="Output geodatabase/feature dataset",
            name="out_gdb",
            datatype="DEWorkspace",
            parameterType="Required",
            direction="Input"
        )

        out_layer = arcpy.Parameter(
            displayName="Output group layer name",
            name="out_layer",
            datatype="GPString",
            parameterType="Required",
            direction="Input"
        )
        out_layer.value = "AnnoGroup"

        anno_suffix = arcpy.Parameter(
            displayName="Annotation suffix",
            name="anno_suffix",
            datatype="GPString",
            parameterType="Required",
            direction="Input"
        )
        anno_suffix.value = "Anno"

        reference_scale = arcpy.Parameter(
            displayName="Reference scale value (optional)",
            name="reference_scale",
            datatype="GPDouble",
            parameterType="Optional",
            direction="Input"
        )

        feature_linked = arcpy.Parameter(
            displayName="Feature linked",
            name="feature_linked",
            datatype="GPString",
            parameterType="Required",
            direction="Input"
        )
        feature_linked.filter.type = "ValueList"
        feature_linked.filter.list = ["STANDARD", "FEATURE_LINKED"]
        feature_linked.value = "STANDARD"

        generate_unplaced = arcpy.Parameter(
            displayName="Generate unplaced annotation",
            name="generate_unplaced",
            datatype="GPString",
            parameterType="Required",
            direction="Input"
        )
        generate_unplaced.filter.type = "ValueList"
        generate_unplaced.filter.list = ["NOT_GENERATE_UNPLACED_ANNOTATION", "GENERATE_UNPLACED_ANNOTATION"]
        generate_unplaced.value = "GENERATE_UNPLACED_ANNOTATION"

        out_workspace = arcpy.Parameter(
            displayName="Output workspace (derived)",
            name="out_workspace",
            datatype="DEWorkspace",
            parameterType="Derived",
            direction="Output"
        )

        out_log = arcpy.Parameter(
            displayName="Log file path",
            name="out_log",
            datatype="GPString",
            parameterType="Derived",
            direction="Output"
        )

        p.extend([map_document, data_frame, out_gdb, out_layer, anno_suffix, reference_scale,
                  feature_linked, generate_unplaced, out_workspace, out_log])
        return p

    def execute(self, parameters, messages):
        arcpy.env.overwriteOutput = True

        logger = None
        try:
            map_document = parameters[0].valueAsText
            data_frame = parameters[1].valueAsText
            out_gdb = parameters[2].valueAsText
            out_layer = parameters[3].valueAsText
            anno_suffix = parameters[4].valueAsText
            reference_scale = parameters[5].value
            feature_linked = parameters[6].valueAsText
            generate_unplaced = parameters[7].valueAsText

            logger, log_path = _setup_logger(out_gdb, "anno")
            _log_msg(messages, logger, "INFO", "Starting AutoGenerateAnnotation")

            try:
                import arcpy.mapping as mapping
            except Exception:
                mapping = None
            if mapping is None:
                raise Exception("arcpy.mapping is required (ArcMap).")

            if map_document.upper() == "CURRENT":
                mxd = mapping.MapDocument("CURRENT")
            else:
                if not os.path.exists(map_document):
                    raise Exception("MXD not found: %s" % map_document)
                mxd = mapping.MapDocument(map_document)

            gp_mxd = map_document
            if map_document.upper() == "CURRENT":
                tmp_mxd = os.path.join(arcpy.env.scratchFolder, "ContourOpt_tmp_%s.mxd" %
                                       datetime.datetime.now().strftime("%Y%m%d_%H%M%S"))
                try:
                    mxd.saveACopy(tmp_mxd)
                    gp_mxd = tmp_mxd
                    _log_msg(messages, logger, "INFO", "Saved CURRENT to temp MXD: %s" % tmp_mxd)
                except Exception as e:
                    _log_msg(messages, logger, "WARN", "Failed saving temp MXD; using CURRENT: %s" % str(e))
                    gp_mxd = "CURRENT"

            dfs = mapping.ListDataFrames(mxd, data_frame)
            if not dfs:
                raise Exception("Data frame not found: %s" % data_frame)
            df = dfs[0]
            ext = df.extent
            if not ext:
                raise Exception("Could not determine data frame extent.")

            sr = df.spatialReference
            if _is_geographic(sr):
                raise Exception("Projected data frame is required for stable annotation.")

            tile_fc = os.path.join(arcpy.env.scratchGDB, "AnnoTiles")
            if arcpy.Exists(tile_fc):
                arcpy.Delete_management(tile_fc)

            arcpy.CreateFeatureclass_management(arcpy.env.scratchGDB, "AnnoTiles", "POLYGON", "", "DISABLED", "DISABLED", sr)
            _add_fields(tile_fc, [("TileID", "LONG")])

            arr = arcpy.Array([
                arcpy.Point(ext.XMin, ext.YMin),
                arcpy.Point(ext.XMax, ext.YMin),
                arcpy.Point(ext.XMax, ext.YMax),
                arcpy.Point(ext.XMin, ext.YMax),
                arcpy.Point(ext.XMin, ext.YMin)
            ])
            poly = arcpy.Polygon(arr, sr)

            with arcpy.da.InsertCursor(tile_fc, ["SHAPE@", "TileID"]) as ins:
                ins.insertRow([poly, 1])

            ref_scale_val = "" if reference_scale is None else float(reference_scale)
            ref_scale_field = ""
            tile_id_field = "TileID"
            coord_sys_field = ""
            map_rotation_field = ""

            _log_msg(messages, logger, "INFO", "Running TiledLabelsToAnnotation_cartography...")

            arcpy.TiledLabelsToAnnotation_cartography(
                gp_mxd, data_frame, tile_fc, out_gdb, out_layer, anno_suffix,
                ref_scale_val, ref_scale_field, tile_id_field,
                coord_sys_field, map_rotation_field,
                feature_linked, generate_unplaced
            )

            parameters[8].value = out_gdb
            parameters[9].value = log_path
            _log_msg(messages, logger, "INFO", "Completed AutoGenerateAnnotation.")
            return

        finally:
            _shutdown_logger(logger)


# ----------------------------------------------------------------------
# Unit tests (basic)
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
        p = []
        out_ws = arcpy.Parameter(
            displayName="Output workspace",
            name="out_ws",
            datatype="DEWorkspace",
            parameterType="Required",
            direction="Input"
        )
        out_name = arcpy.Parameter(
            displayName="Test report table name",
            name="out_name",
            datatype="GPString",
            parameterType="Required",
            direction="Input"
        )
        out_name.value = "ContourOptUnitTestReport"

        out_table = arcpy.Parameter(
            displayName="Test report table",
            name="out_table",
            datatype="DETable",
            parameterType="Derived",
            direction="Output"
        )
        out_log = arcpy.Parameter(
            displayName="Log file path",
            name="out_log",
            datatype="GPString",
            parameterType="Derived",
            direction="Output"
        )
        p.extend([out_ws, out_name, out_table, out_log])
        return p

    def execute(self, parameters, messages):
        arcpy.env.overwriteOutput = True

        logger = None
        ins = None

        try:
            out_ws = parameters[0].valueAsText
            out_name = parameters[1].valueAsText

            logger, log_path = _setup_logger(out_ws, "tests")
            _log_msg(messages, logger, "INFO", "Starting unit tests...")

            out_tbl = os.path.join(out_ws, out_name)
            if arcpy.Exists(out_tbl):
                arcpy.Delete_management(out_tbl)
            arcpy.CreateTable_management(out_ws, out_name)
            _add_fields(out_tbl, [("TEST", "TEXT", 128), ("STATUS", "TEXT", 16), ("DETAILS", "TEXT", 255)])

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

            _log_msg(messages, logger, "INFO", "Unit tests done. Failures=%d Errors=%d" %
                     (len(result.failures), len(result.errors)))

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
