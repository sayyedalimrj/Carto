# -*- coding: utf-8 -*-
"""
Plugin 04 - Elevation Text Deconflict (ArcMap / Python 2.7)  v5 HARDENED
=========================================================================
Two input modes for elevation-text deconfliction:

  Mode A: POINT_LAYER_WITH_TEXT_FIELD
    Inputs are spot-elevation points + a text field. The tool computes
    a non-overlapping label position per point and outputs them as a
    POINT feature class with QA fields. Inputs are NOT modified.

  Mode B: ANNOTATION_LAYER_AND_ANCHOR_POINTS
    Inputs are an annotation feature class + matching anchor points.
    The tool writes XOffset/YOffset (and optionally Angle) on a COPY
    of the annotation in the same GDB / feature dataset, so ArcMap
    can render it immediately.

Hardened in v5 (vs v3 fixedUIUX):
  * SELECTION-BYPASS HARDWIRED. Every input layer (points, annotation,
    anchors, obstacles) is resolved to its on-disk catalogPath. Active
    selections are warned about and ignored - the tool always operates
    on the FULL dataset.
  * SPATIAL-INDEXED OBSTACLE QUERIES. Each obstacle layer is wrapped in
    a feature-layer with a spatial index. Per-feature obstacle queries
    use SelectLayerByLocation against an envelope polygon, instead of
    pre-loading every obstacle geometry into RAM. RAM scales with what
    is *near* each label, not with the obstacle count.
  * On-disk staging: temporary annotation-as-polygon FC and the Near
    table both land in scratchGDB.
  * Placed-label cache for Mode A is AABB-indexed for fast self-overlap
    rejection (cheap aabb test before any geometry intersect).
  * NARROW EXCEPTIONS. Every "except:" is "except Exception:". v3 had
    ~44 bare excepts. Tracebacks are now logged.
  * STAGE-BY-STAGE [DIAG] LOGGING.
  * Py2.7 hygiene: from __future__ import division, _safe_unicode/
    to_utf8 / ascii_safe helpers (kept), guarded Editor session.

Author: Ali Mirjafari + Kiro
Version: 5.0 (ArcMap / Python 2.7)
"""

from __future__ import division

import arcpy
import math
import os
import traceback
import datetime
import uuid
import gc

# =============================================================================
# 0. Compatibility / messaging
# =============================================================================

def to_unicode(v):
    """Best-effort unicode for ArcMap (Py2.7) without crashing."""
    try:
        if v is None:
            return u""
        if isinstance(v, unicode):  # noqa: F821 (Py2)
            return v
        try:
            return unicode(v, "utf-8")  # noqa: F821
        except Exception:
            try:
                return unicode(v, "cp1256")  # noqa: F821
            except Exception:
                return unicode(str(v), "utf-8", "ignore")  # noqa: F821
    except Exception:
        return u""

def ascii_safe(u):
    uu = to_unicode(u)
    try:
        return uu.encode("ascii", "replace")
    except Exception:
        return "?"

def to_utf8(v):
    """UTF-8 bytes for CSV/log writing (Py2.7 safe)."""
    try:
        if isinstance(v, unicode):  # noqa: F821
            return v.encode("utf-8")
    except Exception:
        pass
    try:
        if isinstance(v, str):
            for enc in ("utf-8", "cp1256", "latin-1"):
                try:
                    return unicode(v, enc, "ignore").encode("utf-8")  # noqa: F821
                except Exception:
                    continue
            return v
    except Exception:
        pass
    try:
        return to_unicode(v).encode("utf-8")
    except Exception:
        try:
            return str(v)
        except Exception:
            return ""

def is_empty_gp(v):
    if v is None:
        return True
    s = to_unicode(v).strip()
    return (s == u"" or s == u"#")

def now_str():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def ensure_dir(d):
    try:
        if d and (not os.path.isdir(d)):
            os.makedirs(d)
    except Exception:
        pass

def safe_delete(path, log=None):
    try:
        if path and arcpy.Exists(path):
            arcpy.management.Delete(path)
            if log:
                log.verbose("Deleted: {0}".format(path))
    except Exception:
        if log:
            log.verbose("safe_delete failed for {0}: {1}".format(path, traceback.format_exc()))

# =============================================================================
# 1. Logger
# =============================================================================

class Logger(object):
    def __init__(self, debug_level, log_path, report_text_mode):
        self.level = (debug_level or "OFF").upper()
        self.path = None
        self.report_text_mode = report_text_mode or "ASCII_SAFE_REPLACE"
        if log_path and (not is_empty_gp(log_path)):
            self.path = log_path
        if self.level != "OFF" and (self.path is None):
            try:
                sf = arcpy.env.scratchFolder
                if sf and os.path.isdir(sf):
                    self.path = os.path.join(
                        sf,
                        "ElevationTextDeconflict_log_{0}.txt".format(
                            datetime.datetime.now().strftime("%Y%m%d_%H%M%S")))
            except Exception:
                self.path = None

    def _write_file(self, msg):
        if not self.path:
            return
        try:
            ensure_dir(os.path.dirname(self.path))
        except Exception:
            pass
        try:
            with open(self.path, "ab") as f:
                if isinstance(msg, unicode):  # noqa: F821
                    b = msg.encode("utf-8", "replace")
                else:
                    b = str(msg)
                f.write(b)
                if not b.endswith("\n"):
                    f.write("\n")
        except Exception:
            pass

    def _msg(self, s):
        if self.report_text_mode == "ASCII_SAFE_REPLACE":
            try:
                return ascii_safe(s)
            except Exception:
                return "?"
        return to_unicode(s).encode("utf-8", "ignore")

    def info(self, msg):
        if self.level in ("BASIC", "VERBOSE"):
            m = "[{0}] INFO  {1}".format(now_str(), msg)
            try:
                arcpy.AddMessage(self._msg(m))
            except Exception:
                pass
            self._write_file(m)

    def warn(self, msg):
        if self.level in ("BASIC", "VERBOSE"):
            m = "[{0}] WARN  {1}".format(now_str(), msg)
            try:
                arcpy.AddWarning(self._msg(m))
            except Exception:
                pass
            self._write_file(m)

    def error(self, msg):
        m = "[{0}] ERROR {1}".format(now_str(), msg)
        try:
            arcpy.AddError(self._msg(m))
        except Exception:
            pass
        self._write_file(m)

    def verbose(self, msg):
        if self.level == "VERBOSE":
            m = "[{0}] DEBUG {1}".format(now_str(), msg)
            try:
                arcpy.AddMessage(self._msg(m))
            except Exception:
                pass
            self._write_file(m)

    def diag(self, msg):
        # always emits, regardless of debug level - the production "what happened" log
        m = "[DIAG] {0}".format(msg)
        try:
            arcpy.AddMessage(self._msg(m))
        except Exception:
            pass
        self._write_file(m)

# =============================================================================
# 2. Selection-bypass: resolve any layer to its on-disk source
# =============================================================================

def _selection_info(layer_or_path):
    """Return (selected_count, total_count, name)."""
    try:
        d = arcpy.Describe(layer_or_path)
    except Exception:
        return (None, None, to_unicode(layer_or_path))
    name = getattr(d, "name", to_unicode(layer_or_path))
    fidset = getattr(d, "FIDSet", "") or ""
    total = None
    try:
        total = int(arcpy.GetCount_management(layer_or_path).getOutput(0))
    except Exception:
        total = None
    if fidset.strip() == "":
        return (0, total, name)
    sel_count = len([t for t in fidset.split(";") if t.strip() != ""])
    return (sel_count, total, name)

def _resolve_full_source(layer_or_path):
    """Return on-disk catalogPath for a layer; pass-through if already a path."""
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

def _announce_selection(label, layer_or_path, log=None):
    sel, total, name = _selection_info(layer_or_path)
    if sel and sel > 0:
        msg = u"{lbl}: '{n}' has an active selection ({s} of {t}). Ignoring selection - processing FULL dataset.".format(
            lbl=label, n=name, s=sel, t=(total if total is not None else u"?"))
        if log:
            log.warn(msg)
        else:
            try:
                arcpy.AddWarning(ascii_safe(msg))
            except Exception:
                pass
    else:
        if log:
            log.diag(u"{lbl}: '{n}' total={t}, no active selection.".format(
                lbl=label, n=name, t=(total if total is not None else u"?")))

# =============================================================================
# 3. Spatial-indexed obstacle store (replaces v3 in-RAM cache)
# =============================================================================

class _ObstacleStore(object):
    """
    Wraps obstacle layers as on-disk feature layers with spatial index.
    Per-candidate overlap queries use SelectLayerByLocation against an
    envelope polygon, fetching ONLY the touching obstacles, instead of
    iterating every cached geometry.
    """
    __slots__ = ("layers", "log")

    def __init__(self, log):
        self.layers = []  # list of (lyr_name, fc_path)
        self.log = log

    def add_layer(self, layer_or_path):
        """Resolve to on-disk path, ensure spatial index, make feature layer."""
        if not layer_or_path:
            return
        src = _resolve_full_source(layer_or_path)
        if not src or not arcpy.Exists(src):
            if self.log:
                self.log.warn("OBSTACLE: layer does not exist: {0}".format(layer_or_path))
            return
        try:
            try:
                arcpy.AddSpatialIndex_management(src)
            except Exception:
                pass
            lyr_name = "obs_lyr_" + uuid.uuid4().hex[:6]
            arcpy.MakeFeatureLayer_management(src, lyr_name)
            self.layers.append((lyr_name, src))
            if self.log:
                try:
                    n = int(arcpy.GetCount_management(src).getOutput(0))
                except Exception:
                    n = -1
                self.log.diag("OBSTACLE: '{0}' total={1}".format(src, n))
        except Exception:
            if self.log:
                self.log.warn("OBSTACLE: MakeFeatureLayer failed for {0}: {1}".format(
                    layer_or_path, traceback.format_exc()))

    def cleanup(self):
        for (lyr_name, _) in self.layers:
            try:
                arcpy.management.Delete(lyr_name)
            except Exception:
                pass
        self.layers = []

    def _envelope_polygon(self, sr, xmin, ymin, xmax, ymax):
        arr = arcpy.Array([
            arcpy.Point(xmin, ymin),
            arcpy.Point(xmax, ymin),
            arcpy.Point(xmax, ymax),
            arcpy.Point(xmin, ymax),
            arcpy.Point(xmin, ymin),
        ])
        return arcpy.Polygon(arr, sr)

    def conflict_in_box(self, sr, cx, cy, half_w, half_h, conflict_test_geom,
                         conflict_mode):
        """
        Return True if any obstacle is in conflict with conflict_test_geom.
        Uses SelectLayerByLocation(INTERSECT) against an envelope polygon
        sized cx +/- (half_w + safety), cy +/- (half_h + safety) to fetch
        only candidates near the box.
        """
        if conflict_test_geom is None:
            return True
        env = self._envelope_polygon(sr,
                                      cx - half_w, cy - half_h,
                                      cx + half_w, cy + half_h)
        for (lyr_name, _) in self.layers:
            try:
                arcpy.management.SelectLayerByLocation(
                    lyr_name, "INTERSECT", env,
                    search_distance="", selection_type="NEW_SELECTION")
                try:
                    sel_count = int(arcpy.GetCount_management(lyr_name).getOutput(0))
                except Exception:
                    sel_count = 0
                if sel_count <= 0:
                    continue
                if conflict_mode == "FAST_EXTENT_ONLY":
                    try:
                        arcpy.management.SelectLayerByAttribute(lyr_name, "CLEAR_SELECTION")
                    except Exception:
                        pass
                    return True
                # BALANCED or ACCURATE: now do real disjoint test
                hit = False
                try:
                    with arcpy.da.SearchCursor(lyr_name, ["SHAPE@"]) as sc:
                        for (g,) in sc:
                            if g is None:
                                continue
                            try:
                                if not conflict_test_geom.disjoint(g):
                                    hit = True
                                    break
                            except Exception:
                                hit = True
                                break
                finally:
                    try:
                        arcpy.management.SelectLayerByAttribute(lyr_name, "CLEAR_SELECTION")
                    except Exception:
                        pass
                if hit:
                    return True
            except Exception:
                if self.log:
                    self.log.verbose("conflict_in_box query failed: {0}".format(
                        traceback.format_exc()))
                # Best-effort on failure: assume conflict (be safe)
                continue
        return False

# =============================================================================
# 4. AABB cache for placed labels (Mode A self-overlap)
# =============================================================================

class _PlacedCache(object):
    __slots__ = ("items",)
    def __init__(self):
        self.items = []  # list of (xmin, ymin, xmax, ymax, geom)

    def add(self, geom):
        if geom is None:
            return
        ext = geom.extent
        if ext is None:
            return
        self.items.append((ext.XMin, ext.YMin, ext.XMax, ext.YMax, geom))

    def conflicts(self, foot_geom):
        """Return True if foot_geom overlaps any cached placed label."""
        if foot_geom is None or not self.items:
            return False
        ea = foot_geom.extent
        if ea is None:
            return False
        axmin, aymin, axmax, aymax = ea.XMin, ea.YMin, ea.XMax, ea.YMax
        for (bxmin, bymin, bxmax, bymax, g) in self.items:
            if axmax < bxmin or axmin > bxmax or aymax < bymin or aymin > bymax:
                continue
            try:
                if not foot_geom.disjoint(g):
                    return True
            except Exception:
                return True
        return False



# =============================================================================
# 5. Geometry / candidate helpers
# =============================================================================

def _angles(directions):
    step = 2.0 * math.pi / float(directions)
    return [i * step for i in range(directions)]

def _biased_angles(angles, bias):
    if not angles:
        return angles
    try:
        b = (bias or "NONE").upper()
    except Exception:
        b = "NONE"
    if b == "NONE":
        return angles
    def _score_card(a):
        deg = (a * 180.0 / math.pi) % 360.0
        targets = [0.0, 90.0, 180.0, 270.0]
        return min([abs(deg - t) for t in targets])
    def _score_diag(a):
        deg = (a * 180.0 / math.pi) % 360.0
        targets = [45.0, 135.0, 225.0, 315.0]
        return min([abs(deg - t) for t in targets])
    if b == "CARDINAL_FIRST":
        return sorted(angles, key=lambda a: (_score_card(a), a))
    if b == "DIAGONAL_FIRST":
        return sorted(angles, key=lambda a: (_score_diag(a), a))
    return angles

def _iter_candidates(pattern, rings_sorted, angles, max_ring, spiral_step):
    """Yield (r, ang_rad, dx, dy) candidate offsets in map units."""
    pat = (pattern or "FIXED_RINGS").upper()
    if pat == "SPIRAL":
        step = float(spiral_step) if spiral_step and float(spiral_step) > 0 else 0.0
        if step <= 0.0:
            try:
                step = float(min(rings_sorted)) / 2.0
            except Exception:
                step = 1.0
        if step <= 0.0:
            step = 1.0
        n_ang = len(angles) if angles else 16
        ang_step = 2.0 * math.pi / float(n_ang)
        i = 1
        r = step
        while r <= float(max_ring):
            ang = (i * ang_step) % (2.0 * math.pi)
            dx = r * math.cos(ang)
            dy = r * math.sin(ang)
            yield (r, ang, dx, dy)
            i += 1
            r = step * float(i)
        return
    # FIXED_RINGS or GREEDY
    for r in rings_sorted:
        for ang in angles:
            dx = float(r) * math.cos(ang)
            dy = float(r) * math.sin(ang)
            yield (float(r), float(ang), dx, dy)

def _rect_polygon(sr, cx, cy, w, h):
    hw = w / 2.0; hh = h / 2.0
    arr = arcpy.Array([
        arcpy.Point(cx - hw, cy - hh),
        arcpy.Point(cx + hw, cy - hh),
        arcpy.Point(cx + hw, cy + hh),
        arcpy.Point(cx - hw, cy + hh),
        arcpy.Point(cx - hw, cy - hh),
    ])
    return arcpy.Polygon(arr, sr)

def _rect_polygon_rotated(sr, cx, cy, w, h, angle_rad):
    try:
        hw = w / 2.0; hh = h / 2.0
        ca = math.cos(angle_rad); sa = math.sin(angle_rad)
        corners = [(-hw, -hh), (hw, -hh), (hw, hh), (-hw, hh), (-hw, -hh)]
        pts = []
        for (x, y) in corners:
            xr = (x * ca) - (y * sa)
            yr = (x * sa) + (y * ca)
            pts.append(arcpy.Point(cx + xr, cy + yr))
        return arcpy.Polygon(arcpy.Array(pts), sr)
    except Exception:
        return _rect_polygon(sr, cx, cy, w, h)

def _text_extent_map_units(text, font_size_pt, k, reference_scale, padding):
    s = to_unicode(text)
    char_count = max(1, len(s))
    width_pt = char_count * float(k) * float(font_size_pt)
    height_pt = 1.2 * float(font_size_pt)
    m_per_pt = 0.0254 / 72.0
    width_ground = width_pt * m_per_pt * float(reference_scale)
    height_ground = height_pt * m_per_pt * float(reference_scale)
    width_ground += 2.0 * float(padding)
    height_ground += 2.0 * float(padding)
    return width_ground, height_ground

def _parse_multivalue_numbers(val):
    if val is None:
        return []
    s = to_unicode(val).replace(";", " ").replace(",", " ")
    parts = [p for p in s.split() if p.strip()]
    out = []
    for p in parts:
        try:
            out.append(float(p))
        except Exception:
            pass
    return out

def _ensure_field(fc, name, ftype, length=None):
    fields = [f.name.lower() for f in arcpy.ListFields(fc)]
    if name.lower() in fields:
        return
    if ftype.upper() == "TEXT" and length:
        arcpy.management.AddField(fc, name, ftype, field_length=length)
    else:
        arcpy.management.AddField(fc, name, ftype)

# =============================================================================
# 6. Toolbox + Tool class (parameters)
# =============================================================================

class Toolbox(object):
    def __init__(self):
        self.label = "Plugin 4 - Elevation Text Deconflict (ArcMap, v5 hardened)"
        self.alias = "elevtext_v5_arcmap"
        self.tools = [ElevationTextDeconflictV5]


class ElevationTextDeconflictV5(object):
    def __init__(self):
        self.label = "Elevation Text Deconflict (2 Modes) - v5 hardened"
        self.description = (
            "Deconflicts elevation text against obstacles.\n\n"
            "v5 hardening:\n"
            " - SELECTION-BYPASS hardwired (FULL datasets always processed)\n"
            " - Spatial-indexed obstacle queries via SelectLayerByLocation\n"
            " - On-disk staging in scratchGDB (no in_memory)\n"
            " - Stage-by-stage [DIAG] logging\n\n"
            "Mode A: outputs label positions (does NOT modify input points).\n"
            "Mode B: writes XOffset/YOffset on a copy of the annotation in the same GDB."
        )
        self.canRunInBackground = True

    def isLicensed(self):
        return True

    def getParameterInfo(self):
        params = []

        p0 = arcpy.Parameter(displayName="Input Mode", name="input_mode",
                             datatype="GPString", parameterType="Required", direction="Input")
        p0.filter.type = "ValueList"
        p0.filter.list = ["POINT_LAYER_WITH_TEXT_FIELD", "ANNOTATION_LAYER_AND_ANCHOR_POINTS"]
        p0.value = "POINT_LAYER_WITH_TEXT_FIELD"
        params.append(p0)

        # Mode A
        p1 = arcpy.Parameter(displayName="(Mode A) Input Point Layer [Point]", name="in_points",
                             datatype="GPFeatureLayer", parameterType="Optional", direction="Input")
        p1.filter.list = ["Point"]
        params.append(p1)
        p2 = arcpy.Parameter(displayName="(Mode A) Text Field", name="text_field",
                             datatype="Field", parameterType="Optional", direction="Input")
        p2.parameterDependencies = [p1.name]
        params.append(p2)

        # Mode B
        p3 = arcpy.Parameter(displayName="(Mode B) Annotation Layer (GDB Annotation FC)",
                             name="anno_layer", datatype="GPFeatureLayer",
                             parameterType="Optional", direction="Input")
        params.append(p3)
        p4 = arcpy.Parameter(displayName="(Mode B) Anchor Points Layer [Point]",
                             name="anchor_points", datatype="GPFeatureLayer",
                             parameterType="Optional", direction="Input")
        p4.filter.list = ["Point"]
        params.append(p4)
        p5 = arcpy.Parameter(displayName="(Mode B) Annotation-to-Anchor Link Method",
                             name="link_method", datatype="GPString",
                             parameterType="Optional", direction="Input")
        p5.filter.type = "ValueList"
        p5.filter.list = ["NEAREST_POINT", "FEATUREID_MATCH"]
        p5.value = "NEAREST_POINT"
        params.append(p5)
        p6 = arcpy.Parameter(displayName="(Mode B) FeatureID Field (for FEATUREID_MATCH)",
                             name="featureid_field", datatype="Field",
                             parameterType="Optional", direction="Input")
        p6.parameterDependencies = [p3.name]
        p6.value = "FeatureID"
        params.append(p6)
        p7 = arcpy.Parameter(displayName="(Mode B) Max Anchor Match Distance (map units) [blank=no limit]",
                             name="max_match_dist", datatype="GPString",
                             parameterType="Optional", direction="Input")
        p7.value = ""
        params.append(p7)

        # Search settings
        p8 = arcpy.Parameter(displayName="Rings (map units) e.g., 2 4 6", name="rings",
                             datatype="GPDouble", parameterType="Required",
                             direction="Input", multiValue=True)
        p8.value = "2 4 6"
        params.append(p8)
        p9 = arcpy.Parameter(displayName="Directions (angles count)", name="directions",
                             datatype="GPLong", parameterType="Required", direction="Input")
        p9.filter.type = "ValueList"
        p9.filter.list = [8, 16, 24, 36]
        p9.value = 16
        params.append(p9)
        p10 = arcpy.Parameter(displayName="Obstacle Layers (MultiValue)", name="obstacle_layers",
                              datatype="GPFeatureLayer", parameterType="Required",
                              direction="Input", multiValue=True)
        params.append(p10)
        p11 = arcpy.Parameter(displayName="Conflict Test Mode (speed vs accuracy)",
                              name="conflict_test_mode", datatype="GPString",
                              parameterType="Required", direction="Input")
        p11.filter.type = "ValueList"
        p11.filter.list = ["FAST_EXTENT_ONLY", "BALANCED_EXTENT_THEN_GEOMETRY", "ACCURATE_GEOMETRY_ONLY"]
        p11.value = "BALANCED_EXTENT_THEN_GEOMETRY"
        params.append(p11)
        p12 = arcpy.Parameter(displayName="Max Features per Obstacle Layer (deprecated; ignored in v5)",
                              name="max_features_per_layer", datatype="GPLong",
                              parameterType="Optional", direction="Input")
        p12.value = 0
        params.append(p12)
        p13 = arcpy.Parameter(displayName="Padding (map units) [Mode A only]", name="padding",
                              datatype="GPDouble", parameterType="Optional", direction="Input")
        p13.value = 0.0
        params.append(p13)
        p14 = arcpy.Parameter(displayName="Extra Obstacle Search Distance (map units)",
                              name="extra_search", datatype="GPDouble",
                              parameterType="Optional", direction="Input")
        p14.value = 0.0
        params.append(p14)

        # Scale / font
        p15 = arcpy.Parameter(displayName="Reference Scale (e.g., 25000 for 1:25000)",
                              name="reference_scale", datatype="GPLong",
                              parameterType="Optional", direction="Input")
        p15.value = 25000
        params.append(p15)
        p16 = arcpy.Parameter(displayName="(Mode A) Font Size (pt)", name="font_size_pt",
                              datatype="GPDouble", parameterType="Optional", direction="Input")
        p16.value = 8.0
        params.append(p16)
        p17 = arcpy.Parameter(displayName="(Mode A) Character Width Factor k",
                              name="char_width_factor", datatype="GPDouble",
                              parameterType="Optional", direction="Input")
        p17.value = 0.60
        params.append(p17)

        # Reporting / debug
        p18 = arcpy.Parameter(displayName="Report Text Encoding Mode", name="report_text_mode",
                              datatype="GPString", parameterType="Required", direction="Input")
        p18.filter.type = "ValueList"
        p18.filter.list = ["UNICODE_BEST_EFFORT", "ASCII_SAFE_REPLACE"]
        p18.value = "ASCII_SAFE_REPLACE"
        params.append(p18)
        p19 = arcpy.Parameter(displayName="Preview Only (do not modify Mode B copy)",
                              name="preview_only", datatype="GPBoolean",
                              parameterType="Optional", direction="Input")
        p19.value = False
        params.append(p19)
        p20 = arcpy.Parameter(displayName="Debug Level", name="debug_level",
                              datatype="GPString", parameterType="Optional", direction="Input")
        p20.filter.type = "ValueList"
        p20.filter.list = ["OFF", "BASIC", "VERBOSE"]
        p20.value = "BASIC"
        params.append(p20)
        p21 = arcpy.Parameter(displayName="Debug Log File (optional)",
                              name="debug_log_file", datatype="DEFile",
                              parameterType="Optional", direction="Input")
        p21.value = ""
        params.append(p21)

        # Additional options
        p27 = arcpy.Parameter(displayName="Create 'Moved Only' Output", name="create_moved_only",
                              datatype="GPBoolean", parameterType="Optional", direction="Input")
        p27.value = True
        params.append(p27)
        p28 = arcpy.Parameter(displayName="Search Pattern", name="search_pattern",
                              datatype="GPString", parameterType="Optional", direction="Input")
        p28.filter.type = "ValueList"
        p28.filter.list = ["FIXED_RINGS", "SPIRAL", "GREEDY"]
        p28.value = "FIXED_RINGS"
        params.append(p28)
        p29 = arcpy.Parameter(displayName="(SPIRAL) Step (map units) [0=auto]",
                              name="spiral_step", datatype="GPDouble",
                              parameterType="Optional", direction="Input")
        p29.value = 0.0
        params.append(p29)
        p30 = arcpy.Parameter(displayName="Direction Bias", name="direction_bias",
                              datatype="GPString", parameterType="Optional", direction="Input")
        p30.filter.type = "ValueList"
        p30.filter.list = ["NONE", "CARDINAL_FIRST", "DIAGONAL_FIRST"]
        p30.value = "CARDINAL_FIRST"
        params.append(p30)
        p31 = arcpy.Parameter(displayName="(Mode A) Avoid Label-Label Conflicts",
                              name="avoid_label_label", datatype="GPBoolean",
                              parameterType="Optional", direction="Input")
        p31.value = True
        params.append(p31)
        p32 = arcpy.Parameter(displayName="(Mode A) Use Rotated Conflict Box",
                              name="modeA_rotated_box", datatype="GPBoolean",
                              parameterType="Optional", direction="Input")
        p32.value = True
        params.append(p32)
        p33 = arcpy.Parameter(displayName="(Mode B) Apply Rotation to 'Angle' Field",
                              name="apply_rotation_modeB", datatype="GPBoolean",
                              parameterType="Optional", direction="Input")
        p33.value = False
        params.append(p33)
        p34 = arcpy.Parameter(displayName="(Mode B) Rotation Write Mode",
                              name="rotation_write_mode", datatype="GPString",
                              parameterType="Optional", direction="Input")
        p34.filter.type = "ValueList"
        p34.filter.list = ["SET_ABSOLUTE", "ADD_DELTA"]
        p34.value = "SET_ABSOLUTE"
        params.append(p34)
        p35 = arcpy.Parameter(displayName="(Mode B) Create Label-Position Points Output",
                              name="create_modeB_points", datatype="GPBoolean",
                              parameterType="Optional", direction="Input")
        p35.value = True
        params.append(p35)
        p36 = arcpy.Parameter(displayName="Create Leader Lines Output (Polyline)",
                              name="create_leaderlines", datatype="GPBoolean",
                              parameterType="Optional", direction="Input")
        p36.value = False
        params.append(p36)
        p37 = arcpy.Parameter(displayName="Leader Lines: Moved Only",
                              name="leaderlines_moved_only", datatype="GPBoolean",
                              parameterType="Optional", direction="Input")
        p37.value = True
        params.append(p37)
        p26 = arcpy.Parameter(displayName="(Mode B) Reverse Offset Direction",
                              name="reverse_offsets", datatype="GPBoolean",
                              parameterType="Optional", direction="Input")
        p26.value = False
        params.append(p26)

        # Derived outputs
        p22 = arcpy.Parameter(displayName="Output Report (all items)", name="out_report_all",
                              datatype="DEFeatureClass", parameterType="Derived", direction="Output")
        params.append(p22)
        p23 = arcpy.Parameter(displayName="Output Report (unresolved only)", name="out_report_unresolved",
                              datatype="DEFeatureClass", parameterType="Derived", direction="Output")
        params.append(p23)
        p24 = arcpy.Parameter(displayName="Output Moved Copy", name="out_moved_copy",
                              datatype="DEFeatureClass", parameterType="Derived", direction="Output")
        params.append(p24)
        p25 = arcpy.Parameter(displayName="Output Moved Only", name="out_moved_only",
                              datatype="DEFeatureClass", parameterType="Derived", direction="Output")
        params.append(p25)
        p38 = arcpy.Parameter(displayName="Output Label Positions (Points)", name="out_label_positions",
                              datatype="DEFeatureClass", parameterType="Derived", direction="Output")
        params.append(p38)
        p39 = arcpy.Parameter(displayName="Output Leader Lines (Polyline)", name="out_leaderlines",
                              datatype="DEFeatureClass", parameterType="Derived", direction="Output")
        params.append(p39)

        return params

    def updateParameters(self, parameters):
        try:
            pmap = {}
            for p in parameters:
                try:
                    pmap[p.name] = p
                except Exception:
                    pass
            mode = (pmap.get("input_mode").valueAsText if pmap.get("input_mode") else None) \
                   or "POINT_LAYER_WITH_TEXT_FIELD"
        except Exception:
            return
        modeA_names = set(["in_points", "text_field", "padding", "font_size_pt",
                           "char_width_factor", "avoid_label_label", "modeA_rotated_box"])
        modeB_names = set(["anno_layer", "anchor_points", "link_method", "featureid_field",
                           "max_match_dist", "reverse_offsets", "apply_rotation_modeB",
                           "rotation_write_mode", "create_modeB_points"])
        is_modeA = (mode == "POINT_LAYER_WITH_TEXT_FIELD")
        for n in modeA_names:
            if n in pmap:
                pmap[n].enabled = is_modeA
        for n in modeB_names:
            if n in pmap:
                pmap[n].enabled = (not is_modeA)

    def updateMessages(self, parameters):
        try:
            mode = parameters[0].valueAsText or "POINT_LAYER_WITH_TEXT_FIELD"
            rings_txt = parameters[8].valueAsText
            if is_empty_gp(rings_txt):
                parameters[8].setErrorMessage("Rings must be provided.")
            else:
                rings = _parse_multivalue_numbers(rings_txt)
                if not rings:
                    parameters[8].setErrorMessage("Rings must be numeric.")
                elif any([r <= 0 for r in rings]):
                    parameters[8].setErrorMessage("All ring values must be > 0.")
            if mode == "POINT_LAYER_WITH_TEXT_FIELD":
                if is_empty_gp(parameters[1].valueAsText):
                    parameters[1].setErrorMessage("Mode A requires an Input Point Layer.")
                if is_empty_gp(parameters[2].valueAsText):
                    parameters[2].setErrorMessage("Mode A requires a Text Field.")
            else:
                if is_empty_gp(parameters[3].valueAsText):
                    parameters[3].setErrorMessage("Mode B requires an Annotation Layer.")
                if is_empty_gp(parameters[4].valueAsText):
                    parameters[4].setErrorMessage("Mode B requires an Anchor Points Layer.")
        except Exception:
            pass

    # =========================================================================
    # 6.1 Mode A executor
    # =========================================================================

    def _execute_mode_a(self, log, in_points_layer, text_field, ref_scale, font_pt,
                        k_fac, padding, rings_sorted, angles, max_ring,
                        search_pattern, spiral_step, max_attempts, conflict_mode,
                        extra_search, avoid_label_label, modeA_rotated_box,
                        obstacle_store, base_sr, scratch_gdb, report_text_mode,
                        create_moved_only, create_leaderlines, leaderlines_moved_only,
                        ground_per_point):
        """
        Mode A: read points (from full source), write a label-position FC
        in the same workspace as the points. Returns:
          (moved_copy_path, moved_only_path or None, label_positions_path,
           leaderlines_path or None, report_all_rows, report_bad_rows,
           moved, unchanged, failed, skipped).
        """
        in_points_path = _resolve_full_source(in_points_layer)
        out_ws = arcpy.Describe(in_points_path).path
        stamp = datetime.datetime.now().strftime("%H%M%S")
        out_name = "LabelPos_{0}".format(stamp)
        moved_copy_final = os.path.join(out_ws, out_name)

        log.diag("MODE A: output FC -> {0}".format(moved_copy_final))
        arcpy.management.CreateFeatureclass(out_ws, out_name, "POINT", spatial_reference=base_sr)

        for fname, ftype, flen in [
            ("SrcOID", "LONG", None), ("Status", "TEXT", 16),
            ("Ring", "DOUBLE", None), ("AngleDeg", "DOUBLE", None),
            ("Attempts", "LONG", None), ("OrigX", "DOUBLE", None),
            ("OrigY", "DOUBLE", None), ("TextVal", "TEXT", 128),
            ("ETD_MOVED", "SHORT", None), ("DX_MU", "DOUBLE", None),
            ("DY_MU", "DOUBLE", None), ("DX_PT", "DOUBLE", None),
            ("DY_PT", "DOUBLE", None), ("AngleOut", "DOUBLE", None),
            ("ETD_LEADER", "SHORT", None),
        ]:
            _ensure_field(moved_copy_final, fname, ftype, flen)

        report_all_rows = []
        report_bad_rows = []
        leader_rows = []
        moved = unchanged = failed = 0
        placed_cache = _PlacedCache()

        total_in = 0
        with arcpy.da.SearchCursor(in_points_path, ["OID@", "SHAPE@", text_field]) as sc, \
                arcpy.da.InsertCursor(moved_copy_final,
                    ["SHAPE@", "SrcOID", "Status", "Ring", "AngleDeg", "Attempts",
                     "OrigX", "OrigY", "TextVal", "ETD_MOVED", "DX_MU", "DY_MU",
                     "DX_PT", "DY_PT", "AngleOut", "ETD_LEADER"]) as ic:
            for oid, geom, txt in sc:
                total_in += 1
                if geom is None:
                    failed += 1
                    report_bad_rows.append((None, oid, "No geometry", -1, ""))
                    continue
                ax, ay = geom.centroid.X, geom.centroid.Y
                txt_u = to_unicode(txt)
                txt_r = ascii_safe(txt_u) if report_text_mode == "ASCII_SAFE_REPLACE" \
                        else txt_u.encode("utf-8", "ignore")
                w, h = _text_extent_map_units(txt_u, font_pt, k_fac, ref_scale, padding)
                half_diag = 0.5 * math.sqrt(w * w + h * h)

                # Test "no movement" first
                rect0 = _rect_polygon(base_sr, ax, ay, w, h)
                conflict0 = obstacle_store.conflict_in_box(
                    base_sr, ax, ay, half_diag + extra_search, half_diag + extra_search,
                    rect0, conflict_mode)
                if (not conflict0) and (not (avoid_label_label and placed_cache.conflicts(rect0))):
                    unchanged += 1
                    p = arcpy.PointGeometry(arcpy.Point(ax, ay), base_sr)
                    ic.insertRow((p, oid, "UNCHANGED", 0.0, 0.0, 0, ax, ay, txt_r,
                                  0, 0.0, 0.0, 0.0, 0.0, 0.0, 0))
                    report_all_rows.append((p, oid, "UNCHANGED", 0.0, 0.0, 0, oid, 0.0, txt_r))
                    if avoid_label_label:
                        placed_cache.add(rect0)
                    continue

                placed = False
                attempts = 0
                chosen = (ax, ay, 0.0, 0.0)
                chosen_rect = None
                for (r, ang, dx, dy) in _iter_candidates(
                        search_pattern, rings_sorted, angles, max_ring, spiral_step):
                    attempts += 1
                    if attempts > max_attempts:
                        break
                    cx = ax + dx
                    cy = ay + dy
                    if modeA_rotated_box:
                        rect = _rect_polygon_rotated(base_sr, cx, cy, w, h, ang)
                    else:
                        rect = _rect_polygon(base_sr, cx, cy, w, h)
                    if avoid_label_label and placed_cache.conflicts(rect):
                        continue
                    if obstacle_store.conflict_in_box(
                            base_sr, cx, cy, half_diag + extra_search, half_diag + extra_search,
                            rect, conflict_mode):
                        continue
                    placed = True
                    chosen = (cx, cy, float(r), (ang * 180.0 / math.pi))
                    chosen_rect = rect
                    break

                if placed:
                    cx, cy, rr2, aa2 = chosen
                    moved += 1
                    p = arcpy.PointGeometry(arcpy.Point(cx, cy), base_sr)
                    dx_mu = (cx - ax); dy_mu = (cy - ay)
                    dx_pt = dx_mu / float(ground_per_point) if ground_per_point else 0.0
                    dy_pt = dy_mu / float(ground_per_point) if ground_per_point else 0.0
                    ic.insertRow((p, oid, "MOVED", rr2, aa2, attempts,
                                  ax, ay, txt_r, 1, dx_mu, dy_mu, dx_pt, dy_pt, aa2, 1))
                    report_all_rows.append((p, oid, "MOVED", rr2, aa2, attempts, oid, 0.0, txt_r))
                    if create_leaderlines:
                        leader_rows.append((oid, ax, ay, cx, cy, rr2))
                    if avoid_label_label and chosen_rect is not None:
                        placed_cache.add(chosen_rect)
                else:
                    failed += 1
                    p0 = arcpy.PointGeometry(arcpy.Point(ax, ay), base_sr)
                    ic.insertRow((p0, oid, "FAILED", 0.0, 0.0, attempts,
                                  ax, ay, txt_r, 0, 0.0, 0.0, 0.0, 0.0, 0.0, 0))
                    report_all_rows.append((p0, oid, "FAILED", 0.0, 0.0, attempts, oid, 0.0, txt_r))
                    report_bad_rows.append((p0, oid,
                        "No free position found within rings/directions or max attempts exceeded",
                        oid, txt_r))
                if (total_in % 500) == 0:
                    gc.collect()

        log.diag("MODE A: total={0} moved={1} unchanged={2} failed={3}".format(
            total_in, moved, unchanged, failed))

        # Moved-only output
        moved_only_final = None
        if create_moved_only:
            moved_only_name = "LabelPos_MovedOnly_{0}".format(stamp)
            moved_only_final = os.path.join(out_ws, moved_only_name)
            lyr_name = "lyr_labelpos_" + uuid.uuid4().hex[:8]
            try:
                arcpy.management.MakeFeatureLayer(moved_copy_final, lyr_name)
                arcpy.management.SelectLayerByAttribute(lyr_name, "NEW_SELECTION", "ETD_MOVED = 1")
                arcpy.management.CopyFeatures(lyr_name, moved_only_final)
            except Exception:
                log.warn("MODE A: moved-only output failed: {0}".format(traceback.format_exc()))
                moved_only_final = None
            finally:
                try:
                    arcpy.management.Delete(lyr_name)
                except Exception:
                    pass

        # Leader lines output
        leaderlines_fc = None
        if create_leaderlines:
            try:
                ll_name = "LeaderLines_{0}".format(stamp)
                leaderlines_fc = os.path.join(out_ws, ll_name)
                arcpy.management.CreateFeatureclass(out_ws, ll_name, "POLYLINE", spatial_reference=base_sr)
                _ensure_field(leaderlines_fc, "SrcOID", "LONG")
                _ensure_field(leaderlines_fc, "LenMU", "DOUBLE")
                with arcpy.da.InsertCursor(leaderlines_fc, ["SHAPE@", "SrcOID", "LenMU"]) as il:
                    for (oid2, ax2, ay2, cx2, cy2, rr2) in leader_rows:
                        try:
                            if leaderlines_moved_only and float(rr2) <= 0.0:
                                continue
                            arr = arcpy.Array([arcpy.Point(ax2, ay2), arcpy.Point(cx2, cy2)])
                            il.insertRow((arcpy.Polyline(arr, base_sr), int(oid2), float(rr2)))
                        except Exception:
                            pass
            except Exception:
                log.warn("MODE A: leader lines output failed: {0}".format(traceback.format_exc()))
                leaderlines_fc = None

        return (moved_copy_final, moved_only_final, moved_copy_final, leaderlines_fc,
                report_all_rows, report_bad_rows, moved, unchanged, failed, 0)



    # =========================================================================
    # 6.2 Mode B: anchor map (on-disk staging via scratchGDB)
    # =========================================================================

    def _create_temp_polygon_from_annotation(self, log, anno_fc, base_sr, scratch_gdb):
        temp_fc = arcpy.CreateUniqueName("anno_as_polygon", scratch_gdb)
        arcpy.management.CreateFeatureclass(
            out_path=scratch_gdb,
            out_name=os.path.basename(temp_fc),
            geometry_type="POLYGON",
            spatial_reference=base_sr,
        )
        arcpy.management.AddField(temp_fc, "SrcAnnoOID", "LONG")
        inserted = 0
        with arcpy.da.InsertCursor(temp_fc, ["SHAPE@", "SrcAnnoOID"]) as ic:
            with arcpy.da.SearchCursor(anno_fc, ["OID@", "SHAPE@"]) as sc:
                for aoid, ageom in sc:
                    if ageom:
                        ic.insertRow((ageom, int(aoid)))
                        inserted += 1
        poly2anno = {}
        with arcpy.da.SearchCursor(temp_fc, ["OID@", "SrcAnnoOID"]) as sc2:
            for poid, aoid in sc2:
                poly2anno[int(poid)] = int(aoid)
        log.diag("MODE B: Anno-as-polygon FC built. rows={0} path={1}".format(inserted, temp_fc))
        return temp_fc, poly2anno

    def _build_anchor_map_modeB(self, log, anno_fc, anchor_points, link_method,
                                 featureid_field, max_match_dist_text, base_sr, scratch_gdb):
        log.diag("MODE B: anchor link_method={0}".format(link_method))

        anchor_xy = {}
        anchor_src = _resolve_full_source(anchor_points)
        with arcpy.da.SearchCursor(anchor_src, ["OID@", "SHAPE@XY"]) as sc:
            for oid, xy in sc:
                anchor_xy[int(oid)] = xy
        log.diag("MODE B: anchors total={0}".format(len(anchor_xy)))

        mapping = {}
        if link_method == "FEATUREID_MATCH":
            with arcpy.da.SearchCursor(anno_fc, ["OID@", featureid_field]) as sc:
                for aoid, fid in sc:
                    if fid is None:
                        continue
                    try:
                        fid_int = int(fid)
                    except Exception:
                        continue
                    if fid_int in anchor_xy:
                        ax, ay = anchor_xy[fid_int]
                        mapping[int(aoid)] = (fid_int, ax, ay, 0.0)
            log.diag("MODE B: Anchor map (FEATUREID_MATCH) matched={0}".format(len(mapping)))
            return mapping

        # NEAREST_POINT
        search_radius = ""
        t = to_unicode(max_match_dist_text or u"").strip()
        if t and t not in (u"0", u"0.0"):
            try:
                if float(t) > 0:
                    search_radius = t
            except Exception:
                search_radius = ""

        temp_poly = None
        near_table = None
        try:
            temp_poly, poly2anno = self._create_temp_polygon_from_annotation(
                log, anno_fc, base_sr, scratch_gdb)
            near_table = arcpy.CreateUniqueName("anno_anchor_near", scratch_gdb)
            log.diag("MODE B: GenerateNearTable radius={0}".format(
                search_radius if search_radius else "BLANK(no limit)"))
            arcpy.analysis.GenerateNearTable(
                in_features=temp_poly,
                near_features=anchor_src,
                out_table=near_table,
                search_radius=search_radius,
                location="NO_LOCATION",
                angle="NO_ANGLE",
                closest="CLOSEST",
                closest_count=1,
                method="PLANAR",
            )
            matched = 0
            with arcpy.da.SearchCursor(near_table, ["IN_FID", "NEAR_FID", "NEAR_DIST"]) as tc:
                for in_fid, near_fid, near_dist in tc:
                    if in_fid is None or near_fid is None:
                        continue
                    in_fid = int(in_fid); near_fid = int(near_fid)
                    if in_fid not in poly2anno:
                        continue
                    anno_oid = poly2anno[in_fid]
                    if near_fid not in anchor_xy:
                        continue
                    ax, ay = anchor_xy[near_fid]
                    mapping[int(anno_oid)] = (
                        near_fid, ax, ay,
                        float(near_dist) if near_dist is not None else 0.0)
                    matched += 1
            log.diag("MODE B: Anchor map (NEAREST_POINT) matched={0}".format(matched))
            return mapping
        finally:
            safe_delete(near_table, log)
            safe_delete(temp_poly, log)

    def _get_output_container_for_annotation(self, anno_fc):
        d = arcpy.Describe(_resolve_full_source(anno_fc))
        return d.path

    # =========================================================================
    # 6.3 Mode B executor
    # =========================================================================

    def _execute_mode_b(self, log, anno_layer, anchor_points, link_method,
                        featureid_field, max_match_dist_text, ref_scale,
                        rings_sorted, angles, max_ring, search_pattern, spiral_step,
                        max_attempts, conflict_mode, extra_search,
                        obstacle_store, base_sr, scratch_gdb, report_text_mode,
                        create_moved_only, create_modeB_points,
                        create_leaderlines, leaderlines_moved_only,
                        apply_rotation_modeB, rotation_write_mode,
                        reverse_offsets, preview_only, ground_per_point):
        anno_path = _resolve_full_source(anno_layer)
        out_container = self._get_output_container_for_annotation(anno_path)
        log.diag("MODE B: output container = {0}".format(out_container))
        stamp = datetime.datetime.now().strftime("%H%M%S")
        moved_copy_final = os.path.join(out_container, "Annotation_Moved_{0}".format(stamp))
        moved_only_final = os.path.join(out_container, "Annotation_MovedOnly_{0}".format(stamp))
        if not create_moved_only:
            moved_only_final = None

        log.diag("MODE B: CopyFeatures -> {0}".format(moved_copy_final))
        arcpy.management.CopyFeatures(anno_path, moved_copy_final)

        for fname, ftype, flen in [
            ("ETD_MOVED", "SHORT", None), ("DX_MU", "DOUBLE", None),
            ("DY_MU", "DOUBLE", None), ("DX_PT", "DOUBLE", None),
            ("DY_PT", "DOUBLE", None), ("AngleOut", "DOUBLE", None),
            ("ETD_LEADER", "SHORT", None),
            ("ETD_STATUS", "TEXT", 16), ("ETD_RING", "DOUBLE", None),
            ("ETD_ANGLE", "DOUBLE", None), ("ETD_ATT", "LONG", None),
            ("ETD_AOID", "LONG", None), ("ETD_ADIST", "DOUBLE", None),
        ]:
            _ensure_field(moved_copy_final, fname, ftype, flen)

        log.diag("MODE B: building anchor map on copy ...")
        anchor_map = self._build_anchor_map_modeB(
            log, moved_copy_final, anchor_points, link_method,
            featureid_field, max_match_dist_text, base_sr, scratch_gdb)

        # Workspace for editor session
        desc_anno = arcpy.Describe(moved_copy_final)
        workspace = desc_anno.catalogPath
        original_workspace = workspace
        try:
            while workspace and not os.path.basename(workspace).lower().endswith(".gdb"):
                workspace = os.path.dirname(workspace)
        except Exception:
            workspace = None
        if not workspace:
            raise arcpy.ExecuteError(
                "Could not determine file geodatabase path from annotation: {0}".format(
                    original_workspace))
        log.diag("MODE B: edit workspace = {0}".format(workspace))

        report_all_rows = []
        report_bad_rows = []
        leader_rows = []
        moved = unchanged = failed = skipped = 0
        editor = None

        try:
            editor = arcpy.da.Editor(workspace)
            editor.startEditing(False, True)  # no undo, multiuser
            editor.startOperation()

            u_fields = ["OID@", "SHAPE@", "TextString", "XOffset", "YOffset",
                        "ETD_MOVED", "ETD_STATUS", "ETD_RING", "ETD_ANGLE",
                        "ETD_ATT", "ETD_AOID", "ETD_ADIST"]
            n = 0
            with arcpy.da.UpdateCursor(moved_copy_final, u_fields) as ucur:
                for aoid, ageom, textstr, xo, yo, mv, st, rr, aa, att, ao, ad in ucur:
                    n += 1
                    if ageom is None:
                        failed += 1
                        report_bad_rows.append((None, aoid, "No geometry", -1, ""))
                        continue
                    txt_u = to_unicode(textstr)
                    txt_r = ascii_safe(txt_u) if report_text_mode == "ASCII_SAFE_REPLACE" \
                            else txt_u.encode("utf-8", "ignore")
                    if int(aoid) not in anchor_map:
                        skipped += 1
                        c = ageom.centroid
                        p = arcpy.PointGeometry(arcpy.Point(c.X, c.Y), base_sr)
                        report_all_rows.append((p, aoid, "SKIPPED", 0.0, 0.0, 0,
                                                -1, -1.0, txt_r))
                        ucur.updateRow((aoid, ageom, textstr, xo, yo, 0,
                                        "SKIPPED", 0.0, 0.0, 0, -1, -1.0))
                        continue
                    anchor_oid, ax, ay, adist = anchor_map[int(aoid)]
                    ext = ageom.extent
                    w = abs(ext.XMax - ext.XMin); h = abs(ext.YMax - ext.YMin)
                    half_diag = 0.5 * math.sqrt(w * w + h * h)

                    # Current position conflict check
                    if not obstacle_store.conflict_in_box(
                            base_sr, (ext.XMin + ext.XMax) * 0.5,
                            (ext.YMin + ext.YMax) * 0.5,
                            half_diag + extra_search, half_diag + extra_search,
                            ageom, conflict_mode):
                        unchanged += 1
                        c = ageom.centroid
                        p = arcpy.PointGeometry(arcpy.Point(c.X, c.Y), base_sr)
                        report_all_rows.append((p, aoid, "UNCHANGED", 0.0, 0.0, 0,
                                                anchor_oid, adist, txt_r))
                        ucur.updateRow((aoid, ageom, textstr, xo, yo, 0,
                                        "UNCHANGED", 0.0, 0.0, 0, anchor_oid, adist))
                        continue

                    curr_c = ageom.centroid
                    placed = False
                    attempts = 0
                    chosen = (curr_c.X, curr_c.Y, 0.0, 0.0, 0.0, 0.0)
                    for (r, ang, dx, dy) in _iter_candidates(
                            search_pattern, rings_sorted, angles, max_ring, spiral_step):
                        attempts += 1
                        if attempts > max_attempts:
                            break
                        cx = curr_c.X + dx
                        cy = curr_c.Y + dy
                        moved_geom = None
                        try:
                            parts_array = arcpy.Array()
                            for part in ageom:
                                pa = arcpy.Array()
                                for p in part:
                                    if p:
                                        pa.add(arcpy.Point(p.X + dx, p.Y + dy))
                                parts_array.add(pa)
                            moved_geom = arcpy.Polygon(parts_array, base_sr)
                        except Exception:
                            moved_geom = None
                        if moved_geom is None:
                            continue
                        if not obstacle_store.conflict_in_box(
                                base_sr, cx, cy,
                                half_diag + extra_search, half_diag + extra_search,
                                moved_geom, conflict_mode):
                            placed = True
                            chosen = (cx, cy, float(r), (ang * 180.0 / math.pi), dx, dy)
                            break
                    if placed:
                        cx, cy, rr2, aa2, dx, dy = chosen
                        dx_pt = float(dx) / float(ground_per_point)
                        dy_pt = float(dy) / float(ground_per_point)
                        if reverse_offsets:
                            dx_pt = -dx_pt; dy_pt = -dy_pt
                        new_xo = (float(xo) if xo is not None else 0.0) + dx_pt
                        new_yo = (float(yo) if yo is not None else 0.0) + dy_pt
                        if create_leaderlines:
                            try:
                                gx = ax + float(new_xo) * float(ground_per_point)
                                gy = ay + float(new_yo) * float(ground_per_point)
                                leader_rows.append((aoid, ax, ay, gx, gy, rr2))
                            except Exception:
                                pass
                        moved += 1
                        p = arcpy.PointGeometry(arcpy.Point(cx, cy), base_sr)
                        report_all_rows.append((p, aoid, "MOVED", rr2, aa2, attempts,
                                                anchor_oid, adist, txt_r))
                        if preview_only:
                            ucur.updateRow((aoid, ageom, textstr, xo, yo, 1,
                                            "PREVIEW_MOVED", rr2, aa2, attempts,
                                            anchor_oid, adist))
                        else:
                            ucur.updateRow((aoid, ageom, textstr, new_xo, new_yo, 1,
                                            "MOVED", rr2, aa2, attempts,
                                            anchor_oid, adist))
                    else:
                        failed += 1
                        c = ageom.centroid
                        p = arcpy.PointGeometry(arcpy.Point(c.X, c.Y), base_sr)
                        report_all_rows.append((p, aoid, "FAILED", 0.0, 0.0, attempts,
                                                anchor_oid, adist, txt_r))
                        report_bad_rows.append((p, aoid,
                            "No free position found within rings/directions",
                            anchor_oid, txt_r))
                        ucur.updateRow((aoid, ageom, textstr, xo, yo, 0,
                                        "FAILED", 0.0, 0.0, attempts,
                                        anchor_oid, adist))
                    if (n % 500) == 0:
                        gc.collect()

            editor.stopOperation()
            editor.stopEditing(True)
            editor = None
        except Exception:
            log.error("MODE B: edit session failed: {0}".format(traceback.format_exc()))
            try:
                if editor is not None:
                    if editor.isEditing:
                        editor.abortOperation()
                        editor.stopEditing(False)
            except Exception:
                pass
            raise

        log.diag("MODE B: total={0} moved={1} unchanged={2} failed={3} skipped={4}".format(
            (moved + unchanged + failed + skipped), moved, unchanged, failed, skipped))

        # Optional rotation write
        if apply_rotation_modeB and not preview_only:
            try:
                angle_field = None
                flds = [f.name for f in arcpy.ListFields(moved_copy_final)]
                for nm in ("Angle", "ANGLE"):
                    if nm in flds:
                        angle_field = nm
                        break
                if angle_field:
                    lyr_rot = "lyr_rot_" + uuid.uuid4().hex[:8]
                    arcpy.management.MakeFeatureLayer(moved_copy_final, lyr_rot)
                    arcpy.management.SelectLayerByAttribute(
                        lyr_rot, "NEW_SELECTION", "ETD_MOVED = 1")
                    if rotation_write_mode == "ADD_DELTA":
                        codeblock = (
                            "def addang(a,b):\n"
                            "  try:\n"
                            "    return (float(a) if a is not None else 0.0) + "
                            "(float(b) if b is not None else 0.0)\n"
                            "  except Exception:\n"
                            "    return float(b) if b is not None else 0.0")
                        arcpy.management.CalculateField(
                            lyr_rot, angle_field,
                            "addang(!{0}!, !ETD_ANGLE!)".format(angle_field),
                            "PYTHON_9.3", codeblock)
                    else:
                        arcpy.management.CalculateField(
                            lyr_rot, angle_field, "!ETD_ANGLE!", "PYTHON_9.3")
                    arcpy.management.Delete(lyr_rot)
            except Exception:
                log.warn("MODE B: rotation write failed: {0}".format(traceback.format_exc()))

        # Force redraw / dirty flag
        try:
            arcpy.management.CalculateField(
                moved_copy_final, "ETD_MOVED", "!ETD_MOVED!", "PYTHON_9.3")
        except Exception:
            pass
        try:
            arcpy.management.ClearWorkspaceCache()
        except Exception:
            pass
        try:
            arcpy.RefreshActiveView()
            arcpy.RefreshTOC()
            arcpy.RefreshCatalog(out_container)
        except Exception:
            pass

        # Moved-only output
        if moved_only_final:
            try:
                out_dir = os.path.dirname(moved_only_final)
                out_name = os.path.basename(moved_only_final)
                arcpy.conversion.FeatureClassToFeatureClass(
                    moved_copy_final, out_dir, out_name, "ETD_MOVED = 1")
            except Exception:
                log.warn("MODE B: FeatureClassToFeatureClass failed; using fallback.")
                lyr_name = "lyr_anno_copy_" + uuid.uuid4().hex[:8]
                try:
                    arcpy.management.MakeFeatureLayer(moved_copy_final, lyr_name)
                    arcpy.management.SelectLayerByAttribute(
                        lyr_name, "NEW_SELECTION", "ETD_MOVED = 1")
                    arcpy.management.CopyFeatures(lyr_name, moved_only_final)
                except Exception:
                    moved_only_final = None
                finally:
                    try:
                        arcpy.management.Delete(lyr_name)
                    except Exception:
                        pass

        # Mode B label-position points
        label_positions_points = None
        if create_modeB_points:
            try:
                pt_name = "LabelPos_FromAnno_{0}".format(stamp)
                label_positions_points = os.path.join(out_container, pt_name)
                arcpy.management.CreateFeatureclass(
                    out_container, pt_name, "POINT", spatial_reference=base_sr)
                _ensure_field(label_positions_points, "SrcOID", "LONG")
                _ensure_field(label_positions_points, "Status", "TEXT", length=16)
                _ensure_field(label_positions_points, "XOff_PT", "DOUBLE")
                _ensure_field(label_positions_points, "YOff_PT", "DOUBLE")
                _ensure_field(label_positions_points, "AngleDeg", "DOUBLE")
                _ensure_field(label_positions_points, "AnchorOID", "LONG")
                _ensure_field(label_positions_points, "AnchorDist", "DOUBLE")
                with arcpy.da.SearchCursor(moved_copy_final,
                    ["OID@", "ETD_STATUS", "XOffset", "YOffset",
                     "ETD_ANGLE", "ETD_AOID", "ETD_ADIST"]) as scp, \
                     arcpy.da.InsertCursor(label_positions_points,
                    ["SHAPE@", "SrcOID", "Status", "XOff_PT", "YOff_PT",
                     "AngleDeg", "AnchorOID", "AnchorDist"]) as icp:
                    for aoid, stt, xo2, yo2, ang2, ao2, ad2 in scp:
                        if int(aoid) in anchor_map:
                            anchor_oid, ax, ay, adist = anchor_map[int(aoid)]
                            gx = ax + (float(xo2) if xo2 is not None else 0.0) \
                                 * float(ground_per_point)
                            gy = ay + (float(yo2) if yo2 is not None else 0.0) \
                                 * float(ground_per_point)
                            p = arcpy.PointGeometry(arcpy.Point(gx, gy), base_sr)
                            icp.insertRow((p, int(aoid), stt,
                                           float(xo2) if xo2 is not None else 0.0,
                                           float(yo2) if yo2 is not None else 0.0,
                                           float(ang2) if ang2 is not None else 0.0,
                                           int(anchor_oid), float(adist)))
                        else:
                            p = arcpy.PointGeometry(arcpy.Point(0, 0), base_sr)
                            icp.insertRow((p, int(aoid), stt,
                                           float(xo2) if xo2 is not None else 0.0,
                                           float(yo2) if yo2 is not None else 0.0,
                                           float(ang2) if ang2 is not None else 0.0,
                                           -1, -1.0))
            except Exception:
                log.warn("MODE B: label-position points failed: {0}".format(
                    traceback.format_exc()))
                label_positions_points = None

        # Leader lines
        leaderlines_fc = None
        if create_leaderlines:
            try:
                ll_name = "LeaderLines_{0}".format(stamp)
                leaderlines_fc = os.path.join(out_container, ll_name)
                arcpy.management.CreateFeatureclass(
                    out_container, ll_name, "POLYLINE", spatial_reference=base_sr)
                _ensure_field(leaderlines_fc, "SrcOID", "LONG")
                _ensure_field(leaderlines_fc, "LenMU", "DOUBLE")
                with arcpy.da.InsertCursor(
                        leaderlines_fc, ["SHAPE@", "SrcOID", "LenMU"]) as il:
                    for (oid2, ax2, ay2, cx2, cy2, rr2) in leader_rows:
                        try:
                            if leaderlines_moved_only and float(rr2) <= 0.0:
                                continue
                            arr = arcpy.Array(
                                [arcpy.Point(ax2, ay2), arcpy.Point(cx2, cy2)])
                            il.insertRow((arcpy.Polyline(arr, base_sr),
                                          int(oid2), float(rr2)))
                        except Exception:
                            pass
            except Exception:
                log.warn("MODE B: leader lines failed: {0}".format(traceback.format_exc()))
                leaderlines_fc = None

        return (moved_copy_final, moved_only_final, label_positions_points,
                leaderlines_fc, report_all_rows, report_bad_rows,
                moved, unchanged, failed, skipped)

    # =========================================================================
    # 6.4 Main execute orchestrator
    # =========================================================================

    def execute(self, parameters, messages):
        arcpy.env.overwriteOutput = True
        try:
            arcpy.env.parallelProcessingFactor = "100%"
        except Exception:
            pass

        # Parameter map by NAME
        pmap = {}
        try:
            for p in parameters:
                try:
                    pmap[p.name] = p
                except Exception:
                    pass
        except Exception:
            pmap = {}

        def _p_text(name, default=None):
            try:
                if name in pmap:
                    v = pmap[name].valueAsText
                    if is_empty_gp(v):
                        return default
                    return v
            except Exception:
                pass
            return default

        def _p_bool(name, default=False):
            try:
                if name in pmap and pmap[name].value is not None:
                    return bool(pmap[name].value)
            except Exception:
                pass
            v = _p_text(name, None)
            if v is None:
                return default
            try:
                s = str(v).strip().lower()
                return s in ("true", "t", "1", "yes", "y", "on")
            except Exception:
                return default

        def _p_int(name, default=0):
            try:
                if name in pmap and pmap[name].value is not None:
                    return int(pmap[name].value)
            except Exception:
                pass
            try:
                v = _p_text(name, None)
                if v is None:
                    return default
                return int(float(v))
            except Exception:
                return default

        def _p_float(name, default=0.0):
            try:
                if name in pmap and pmap[name].value is not None:
                    return float(pmap[name].value)
            except Exception:
                pass
            try:
                v = _p_text(name, None)
                if v is None:
                    return default
                return float(v)
            except Exception:
                return default

        # Parameters
        mode = _p_text("input_mode", "POINT_LAYER_WITH_TEXT_FIELD")
        in_points = _p_text("in_points", "")
        text_field = _p_text("text_field", "")
        anno_layer = _p_text("anno_layer", "")
        anchor_points = _p_text("anchor_points", "")
        link_method = _p_text("link_method", "NEAREST_POINT")
        featureid_field = _p_text("featureid_field", "FeatureID")
        max_match_dist_text = _p_text("max_match_dist", "")
        rings = _parse_multivalue_numbers(_p_text("rings", "2 4 6"))
        directions = _p_int("directions", 16)
        obstacle_layers_txt = _p_text("obstacle_layers", "")
        conflict_mode = _p_text("conflict_test_mode", "BALANCED_EXTENT_THEN_GEOMETRY")
        padding = _p_float("padding", 0.0)
        extra_search = _p_float("extra_search", 0.0)
        ref_scale = _p_int("reference_scale", 25000)
        font_pt = _p_float("font_size_pt", 8.0)
        k_fac = _p_float("char_width_factor", 0.60)
        report_text_mode = _p_text("report_text_mode", "ASCII_SAFE_REPLACE")
        preview_only = _p_bool("preview_only", False)
        debug_level = _p_text("debug_level", "BASIC")
        debug_log_file = _p_text("debug_log_file", "")
        create_moved_only = _p_bool("create_moved_only", True)
        search_pattern = _p_text("search_pattern", "FIXED_RINGS")
        spiral_step = _p_float("spiral_step", 0.0)
        direction_bias = _p_text("direction_bias", "CARDINAL_FIRST")
        avoid_label_label = _p_bool("avoid_label_label", True)
        modeA_rotated_box = _p_bool("modeA_rotated_box", True)
        apply_rotation_modeB = _p_bool("apply_rotation_modeB", False)
        rotation_write_mode = _p_text("rotation_write_mode", "SET_ABSOLUTE")
        create_modeB_points = _p_bool("create_modeB_points", True)
        create_leaderlines = _p_bool("create_leaderlines", False)
        leaderlines_moved_only = _p_bool("leaderlines_moved_only", True)
        reverse_offsets = _p_bool("reverse_offsets", False)

        log = Logger(debug_level, debug_log_file, report_text_mode)
        log.info("Tool start. mode={0} preview_only={1}".format(mode, preview_only))
        if log.path:
            log.info("Log file: {0}".format(log.path))

        # Validation
        if mode == "POINT_LAYER_WITH_TEXT_FIELD":
            if is_empty_gp(in_points) or is_empty_gp(text_field):
                raise arcpy.ExecuteError(
                    "Mode A selected but Input Point Layer / Text Field is empty.")
        else:
            if is_empty_gp(anno_layer) or is_empty_gp(anchor_points):
                raise arcpy.ExecuteError(
                    "Mode B selected but Annotation Layer / Anchor Points is empty.")
        if not rings:
            raise arcpy.ExecuteError("Rings must be provided (e.g., 2 4 6).")
        obstacle_layers = []
        if not is_empty_gp(obstacle_layers_txt):
            obstacle_layers = [s for s in to_unicode(obstacle_layers_txt).split(";") if s.strip()]
        if not obstacle_layers:
            raise arcpy.ExecuteError("No obstacle layers provided.")

        # Selection-bypass announcements
        if mode == "POINT_LAYER_WITH_TEXT_FIELD":
            _announce_selection(u"Points", in_points, log)
        else:
            _announce_selection(u"Annotation", anno_layer, log)
            _announce_selection(u"Anchors", anchor_points, log)
        for lyr in obstacle_layers:
            _announce_selection(u"Obstacle", lyr, log)

        angles = _biased_angles(_angles(directions), direction_bias)
        rings_sorted = sorted(rings)
        max_ring = max(rings_sorted)
        max_attempts = len(rings_sorted) * directions * 2

        meters_per_point = 0.0254 / 72.0
        ground_per_point = meters_per_point * float(ref_scale)
        if ground_per_point <= 0:
            ground_per_point = meters_per_point * 25000.0

        # Spatial reference from primary input
        if mode == "POINT_LAYER_WITH_TEXT_FIELD":
            base_sr = arcpy.Describe(_resolve_full_source(in_points)).spatialReference
        else:
            base_sr = arcpy.Describe(_resolve_full_source(anno_layer)).spatialReference

        # scratchGDB hardwired
        scratch_gdb = arcpy.env.scratchGDB
        if not scratch_gdb or not arcpy.Exists(scratch_gdb):
            scratch_gdb = arcpy.env.scratchWorkspace
        if not scratch_gdb or not arcpy.Exists(scratch_gdb):
            raise arcpy.ExecuteError("No scratch GDB available. Set arcpy.env.scratchGDB.")
        log.diag("Scratch (disk): {0}".format(scratch_gdb))

        # Build spatial-indexed obstacle store
        obstacle_store = _ObstacleStore(log)
        for lyr in obstacle_layers:
            obstacle_store.add_layer(lyr)

        # Reports always go to scratch
        out_all = arcpy.CreateUniqueName("elevtext_report_all", scratch_gdb)
        out_bad = arcpy.CreateUniqueName("elevtext_report_unresolved", scratch_gdb)
        arcpy.management.CreateFeatureclass(
            scratch_gdb, os.path.basename(out_all), "POINT", spatial_reference=base_sr)
        arcpy.management.AddField(out_all, "SrcOID", "LONG")
        arcpy.management.AddField(out_all, "Status", "TEXT", field_length=16)
        arcpy.management.AddField(out_all, "Ring", "DOUBLE")
        arcpy.management.AddField(out_all, "AngleDeg", "DOUBLE")
        arcpy.management.AddField(out_all, "Attempts", "LONG")
        arcpy.management.AddField(out_all, "AnchorOID", "LONG")
        arcpy.management.AddField(out_all, "AnchorDist", "DOUBLE")
        arcpy.management.AddField(out_all, "TextVal", "TEXT", field_length=128)
        arcpy.management.CreateFeatureclass(
            scratch_gdb, os.path.basename(out_bad), "POINT", spatial_reference=base_sr)
        arcpy.management.AddField(out_bad, "SrcOID", "LONG")
        arcpy.management.AddField(out_bad, "Reason", "TEXT", field_length=200)
        arcpy.management.AddField(out_bad, "AnchorOID", "LONG")
        arcpy.management.AddField(out_bad, "TextVal", "TEXT", field_length=128)

        moved_copy_final = None
        moved_only_final = None
        label_positions_points = None
        leaderlines_fc = None
        report_all_rows = []
        report_bad_rows = []
        moved = unchanged = failed = skipped = 0

        try:
            if mode == "POINT_LAYER_WITH_TEXT_FIELD":
                (moved_copy_final, moved_only_final, label_positions_points,
                 leaderlines_fc, report_all_rows, report_bad_rows,
                 moved, unchanged, failed, skipped) = self._execute_mode_a(
                    log, in_points, text_field, ref_scale, font_pt, k_fac, padding,
                    rings_sorted, angles, max_ring, search_pattern, spiral_step,
                    max_attempts, conflict_mode, extra_search, avoid_label_label,
                    modeA_rotated_box, obstacle_store, base_sr, scratch_gdb,
                    report_text_mode, create_moved_only, create_leaderlines,
                    leaderlines_moved_only, ground_per_point)
            else:
                (moved_copy_final, moved_only_final, label_positions_points,
                 leaderlines_fc, report_all_rows, report_bad_rows,
                 moved, unchanged, failed, skipped) = self._execute_mode_b(
                    log, anno_layer, anchor_points, link_method, featureid_field,
                    max_match_dist_text, ref_scale, rings_sorted, angles, max_ring,
                    search_pattern, spiral_step, max_attempts, conflict_mode,
                    extra_search, obstacle_store, base_sr, scratch_gdb,
                    report_text_mode, create_moved_only, create_modeB_points,
                    create_leaderlines, leaderlines_moved_only,
                    apply_rotation_modeB, rotation_write_mode,
                    reverse_offsets, preview_only, ground_per_point)

            # Write report rows
            with arcpy.da.InsertCursor(
                    out_all, ["SHAPE@", "SrcOID", "Status", "Ring", "AngleDeg",
                              "Attempts", "AnchorOID", "AnchorDist", "TextVal"]) as ic_all:
                for row in report_all_rows:
                    ic_all.insertRow(row)
            with arcpy.da.InsertCursor(
                    out_bad, ["SHAPE@", "SrcOID", "Reason", "AnchorOID", "TextVal"]) as ic_bad:
                for row in report_bad_rows:
                    ic_bad.insertRow(row)
        except Exception as e:
            log.error("Exception: {0}".format(to_unicode(e)))
            log.error("ArcPy messages (2): {0}".format(arcpy.GetMessages(2)))
            log.error(traceback.format_exc())
            raise
        finally:
            try:
                obstacle_store.cleanup()
            except Exception:
                pass

        # Set derived outputs
        try:
            if "out_report_all" in pmap:
                pmap["out_report_all"].value = out_all
            if "out_report_unresolved" in pmap:
                pmap["out_report_unresolved"].value = out_bad
            if "out_moved_copy" in pmap:
                pmap["out_moved_copy"].value = moved_copy_final
            if "out_moved_only" in pmap:
                pmap["out_moved_only"].value = moved_only_final
            if "out_label_positions" in pmap:
                pmap["out_label_positions"].value = (
                    label_positions_points if label_positions_points else moved_copy_final)
            if "out_leaderlines" in pmap:
                pmap["out_leaderlines"].value = leaderlines_fc
        except Exception:
            pass

        log.info("Finished. MOVED={0} UNCHANGED={1} FAILED={2} SKIPPED={3}".format(
            moved, unchanged, failed, skipped))
        if mode == "ANNOTATION_LAYER_AND_ANCHOR_POINTS":
            log.info("Mode B: movement applied via XOffset/YOffset on the output copy. "
                     "Input is not modified.")
        else:
            log.info("Mode A: input points are NOT modified. Output is label positions.")
        log.info("Outputs: moved_copy={0} moved_only={1}".format(
            moved_copy_final, moved_only_final))
