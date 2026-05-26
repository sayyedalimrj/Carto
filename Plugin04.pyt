# -*- coding: utf-8 -*-
"""
Elevation Text Deconflict (ArcMap / Python 2.7) - Two Input Modes
FINAL FIX:
- Mode B writes movement ONLY into annotation move-related fields:
  XOffset, YOffset (and optionally Angle if you want; default: keep Angle)
- Outputs are created in SAME GDB / SAME Feature Dataset as input annotation
  (no asking user for output workspace; no scratch mismatch).

Mode A:
- Does NOT move input points (as requested). Outputs label positions (POINT FC).

Mode B:
- Transaction safe (no Editor session needed), but outputs are in same database
  so you can edit/move them immediately.
"""

import arcpy
import math
import os
import traceback
import datetime
import uuid

# ------------------------------------------------------------
# Helpers
# ------------------------------------------------------------
def to_unicode(v):
    try:
        if v is None:
            return u""
        if isinstance(v, unicode):
            return v
        try:
            return unicode(v, "utf-8")
        except:
            try:
                return unicode(v, "cp1256")
            except:
                return unicode(str(v), "utf-8", "ignore")
    except:
        return u""

def ascii_safe(u):
    uu = to_unicode(u)
    try:
        return uu.encode("ascii", "replace")
    except:
        return "?"

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
    except:
        pass

def safe_delete(path, log=None):
    try:
        if path and arcpy.Exists(path):
            arcpy.management.Delete(path)
            if log:
                log.verbose("Deleted: {0}".format(path))
    except:
        pass

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
                        "ElevationTextDeconflict_log_{0}.txt".format(datetime.datetime.now().strftime("%Y%m%d_%H%M%S"))
                    )
            except:
                self.path = None

    def _write_file(self, msg):
        if not self.path:
            return
        try:
            d = os.path.dirname(self.path)
            ensure_dir(d)
        except:
            pass
        try:
            with open(self.path, "ab") as f:
                if isinstance(msg, unicode):
                    b = msg.encode("utf-8", "replace")
                else:
                    b = str(msg)
                f.write(b)
                if not b.endswith("\n"):
                    f.write("\n")
        except:
            pass

    def _msg(self, s):
        if self.report_text_mode == "ASCII_SAFE_REPLACE":
            try:
                return ascii_safe(s)
            except:
                return "?"
        return to_unicode(s).encode("utf-8", "ignore")

    def info(self, msg):
        if self.level in ("BASIC", "VERBOSE"):
            m = "[{0}] INFO  {1}".format(now_str(), msg)
            arcpy.AddMessage(self._msg(m))
            self._write_file(m)

    def warn(self, msg):
        if self.level in ("BASIC", "VERBOSE"):
            m = "[{0}] WARN  {1}".format(now_str(), msg)
            arcpy.AddWarning(self._msg(m))
            self._write_file(m)

    def error(self, msg):
        m = "[{0}] ERROR {1}".format(now_str(), msg)
        arcpy.AddError(self._msg(m))
        self._write_file(m)

    def verbose(self, msg):
        if self.level == "VERBOSE":
            m = "[{0}] DEBUG {1}".format(now_str(), msg)
            arcpy.AddMessage(self._msg(m))
            self._write_file(m)

# ------------------------------------------------------------
# Toolbox
# ------------------------------------------------------------
class Toolbox(object):
    def __init__(self):
        self.label = "Elevation Text Tools (EN) - 2 Modes (FINAL)"
        self.alias = "elevtext_2mode_final"
        self.tools = [ElevationTextDeconflict_2Mode_EN_FINAL]

class ElevationTextDeconflict_2Mode_EN_FINAL(object):
    def __init__(self):
        self.label = "Elevation Text Deconflict (2 Input Modes) [EN] (FINAL)"
        self.description = "Deconflicts elevation text against obstacles; Mode A outputs label positions; Mode B writes XOffset/YOffset in same GDB/dataset."
        self.canRunInBackground = False

    # -----------------------------
    # Params (keep structure stable)
    # -----------------------------
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
        p3 = arcpy.Parameter(displayName="(Mode B) Annotation Layer (GDB Annotation FC)", name="anno_layer",
                            datatype="GPFeatureLayer", parameterType="Optional", direction="Input")
        params.append(p3)

        p4 = arcpy.Parameter(displayName="(Mode B) Anchor Points Layer [Point]", name="anchor_points",
                            datatype="GPFeatureLayer", parameterType="Optional", direction="Input")
        p4.filter.list = ["Point"]
        params.append(p4)

        p5 = arcpy.Parameter(displayName="(Mode B) Annotation-to-Anchor Link Method", name="link_method",
                            datatype="GPString", parameterType="Optional", direction="Input")
        p5.filter.type = "ValueList"
        p5.filter.list = ["NEAREST_POINT", "FEATUREID_MATCH"]
        p5.value = "NEAREST_POINT"
        params.append(p5)

        p6 = arcpy.Parameter(displayName="(Mode B) FeatureID Field (for FEATUREID_MATCH)", name="featureid_field",
                            datatype="Field", parameterType="Optional", direction="Input")
        p6.parameterDependencies = [p3.name]
        p6.value = "FeatureID"
        params.append(p6)

        p7 = arcpy.Parameter(displayName="(Mode B) Max Anchor Match Distance (map units) [blank=no limit]",
                            name="max_match_dist", datatype="GPString", parameterType="Optional", direction="Input")
        p7.value = ""
        params.append(p7)

        # Search settings
        p8 = arcpy.Parameter(displayName="Rings (map units) MultiValue e.g., 2 4 6", name="rings",
                            datatype="GPDouble", parameterType="Required", direction="Input", multiValue=True)
        p8.value = "2 4 6"
        params.append(p8)

        p9 = arcpy.Parameter(displayName="Directions (angles count)", name="directions",
                            datatype="GPLong", parameterType="Required", direction="Input")
        p9.filter.type = "ValueList"
        p9.filter.list = [8, 16, 24, 36]
        p9.value = 16
        params.append(p9)

        p10 = arcpy.Parameter(displayName="Obstacle Layers (MultiValue)", name="obstacle_layers",
                             datatype="GPFeatureLayer", parameterType="Required", direction="Input", multiValue=True)
        params.append(p10)

        p11 = arcpy.Parameter(displayName="Conflict Test Mode (speed vs accuracy)", name="conflict_test_mode",
                             datatype="GPString", parameterType="Required", direction="Input")
        p11.filter.type = "ValueList"
        p11.filter.list = ["FAST_EXTENT_ONLY", "BALANCED_EXTENT_THEN_GEOMETRY", "ACCURATE_GEOMETRY_ONLY"]
        p11.value = "BALANCED_EXTENT_THEN_GEOMETRY"
        params.append(p11)

        p12 = arcpy.Parameter(displayName="Max Features per Obstacle Layer (0 = no cap)", name="max_features_per_layer",
                             datatype="GPLong", parameterType="Optional", direction="Input")
        p12.value = 0
        params.append(p12)

        p13 = arcpy.Parameter(displayName="Padding (map units) [Mode A only]", name="padding",
                             datatype="GPDouble", parameterType="Optional", direction="Input")
        p13.value = 0.0
        params.append(p13)

        p14 = arcpy.Parameter(displayName="Extra Obstacle Search Distance (map units)", name="extra_search",
                             datatype="GPDouble", parameterType="Optional", direction="Input")
        p14.value = 0.0
        params.append(p14)

        # Scale/font for Mode A sizing (Mode B uses offsets -> needs ref scale too)
        p15 = arcpy.Parameter(displayName="Reference Scale (e.g., 25000 for 1:25000)", name="reference_scale",
                             datatype="GPLong", parameterType="Optional", direction="Input")
        p15.value = 25000
        params.append(p15)

        p16 = arcpy.Parameter(displayName="(Mode A) Font Size (pt)", name="font_size_pt",
                             datatype="GPDouble", parameterType="Optional", direction="Input")
        p16.value = 8.0
        params.append(p16)

        p17 = arcpy.Parameter(displayName="(Mode A) Character Width Factor k", name="char_width_factor",
                             datatype="GPDouble", parameterType="Optional", direction="Input")
        p17.value = 0.60
        params.append(p17)

        # Reporting / debug
        p18 = arcpy.Parameter(displayName="Report Text Encoding Mode", name="report_text_mode",
                             datatype="GPString", parameterType="Required", direction="Input")
        p18.filter.type = "ValueList"
        p18.filter.list = ["UNICODE_BEST_EFFORT", "ASCII_SAFE_REPLACE"]
        p18.value = "ASCII_SAFE_REPLACE"
        params.append(p18)

        p19 = arcpy.Parameter(displayName="Preview Only (do not modify input)", name="preview_only",
                             datatype="GPBoolean", parameterType="Optional", direction="Input")
        p19.value = False
        params.append(p19)

        p20 = arcpy.Parameter(displayName="Debug Level", name="debug_level",
                             datatype="GPString", parameterType="Optional", direction="Input")
        p20.filter.type = "ValueList"
        p20.filter.list = ["OFF", "BASIC", "VERBOSE"]
        p20.value = "BASIC"
        params.append(p20)

        p21 = arcpy.Parameter(displayName="Debug Log File (optional)", name="debug_log_file",
                             datatype="DEFile", parameterType="Optional", direction="Input")
        p21.value = ""
        params.append(p21)

        # Additional options (v2 - safe defaults)
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

        p29 = arcpy.Parameter(displayName="(SPIRAL) Step (map units) [0=auto]", name="spiral_step",
                             datatype="GPDouble", parameterType="Optional", direction="Input")
        p29.value = 0.0
        params.append(p29)

        p30 = arcpy.Parameter(displayName="Direction Bias", name="direction_bias",
                             datatype="GPString", parameterType="Optional", direction="Input")
        p30.filter.type = "ValueList"
        p30.filter.list = ["NONE", "CARDINAL_FIRST", "DIAGONAL_FIRST"]
        p30.value = "CARDINAL_FIRST"
        params.append(p30)

        p31 = arcpy.Parameter(displayName="(Mode A) Avoid Label-Label Conflicts", name="avoid_label_label",
                             datatype="GPBoolean", parameterType="Optional", direction="Input")
        p31.value = True
        params.append(p31)

        p32 = arcpy.Parameter(displayName="(Mode A) Use Rotated Conflict Box", name="modeA_rotated_box",
                             datatype="GPBoolean", parameterType="Optional", direction="Input")
        p32.value = True
        params.append(p32)

        p33 = arcpy.Parameter(displayName="(Mode B) Apply Rotation to 'Angle' Field", name="apply_rotation_modeB",
                             datatype="GPBoolean", parameterType="Optional", direction="Input")
        p33.value = False
        params.append(p33)

        p34 = arcpy.Parameter(displayName="(Mode B) Rotation Write Mode", name="rotation_write_mode",
                             datatype="GPString", parameterType="Optional", direction="Input")
        p34.filter.type = "ValueList"
        p34.filter.list = ["SET_ABSOLUTE", "ADD_DELTA"]
        p34.value = "SET_ABSOLUTE"
        params.append(p34)

        p35 = arcpy.Parameter(displayName="(Mode B) Create Label-Position Points Output", name="create_modeB_points",
                             datatype="GPBoolean", parameterType="Optional", direction="Input")
        p35.value = True
        params.append(p35)

        p36 = arcpy.Parameter(displayName="Create Leader Lines Output (Polyline)", name="create_leaderlines",
                             datatype="GPBoolean", parameterType="Optional", direction="Input")
        p36.value = False
        params.append(p36)

        p37 = arcpy.Parameter(displayName="Leader Lines: Moved Only", name="leaderlines_moved_only",
                             datatype="GPBoolean", parameterType="Optional", direction="Input")
        p37.value = True
        params.append(p37)

        # New parameter for reverse offsets
        p26 = arcpy.Parameter(displayName="Reverse Offset Direction (for Mode B if direction is unclear)", name="reverse_offsets",
                             datatype="GPBoolean", parameterType="Optional", direction="Input")
        p26.value = False
        params.append(p26)

        # Derived outputs
        p22 = arcpy.Parameter(displayName="Output Report (all items)", name="out_report_all",
                             datatype="DEFeatureClass", parameterType="Derived", direction="Output")
        params.append(p22)

        p23 = arcpy.Parameter(displayName="Output Report (unresolved only)", name="out_report_unresolved",
                             datatype="DEFeatureClass", parameterType="Derived", direction="Output")
        params.append(p23)

        p24 = arcpy.Parameter(displayName="Output Moved Copy (Mode A: label positions / Mode B: annotation copy)", name="out_moved_copy",
                             datatype="DEFeatureClass", parameterType="Derived", direction="Output")
        params.append(p24)

        p25 = arcpy.Parameter(displayName="Output Moved Only (only moved features)", name="out_moved_only",
                             datatype="DEFeatureClass", parameterType="Derived", direction="Output")
        params.append(p25)

        # Extra derived outputs (optional)
        p38 = arcpy.Parameter(displayName="Output Label Positions (Points) [Mode A output or Mode B computed]", name="out_label_positions",
                             datatype="DEFeatureClass", parameterType="Derived", direction="Output")
        params.append(p38)

        p39 = arcpy.Parameter(displayName="Output Leader Lines (Polyline)", name="out_leaderlines",
                             datatype="DEFeatureClass", parameterType="Derived", direction="Output")
        params.append(p39)

        return params

    def isLicensed(self):
        return True

    def updateParameters(self, parameters):
        # Enable/disable parameters by NAME (stable even if order changes)
        try:
            pmap = {}
            for p in parameters:
                try:
                    pmap[p.name] = p
                except:
                    pass
            mode = (pmap.get("input_mode").valueAsText if pmap.get("input_mode") else None) or "POINT_LAYER_WITH_TEXT_FIELD"
        except:
            return

        modeA_names = set(["in_points", "text_field", "padding", "font_size_pt", "char_width_factor",
                           "avoid_label_label", "modeA_rotated_box"])
        modeB_names = set(["anno_layer", "anchor_points", "link_method", "featureid_field", "max_match_dist",
                           "reverse_offsets", "apply_rotation_modeB", "rotation_write_mode", "create_modeB_points"])

        is_modeA = (mode == "POINT_LAYER_WITH_TEXT_FIELD")

        for n in modeA_names:
            if n in pmap:
                pmap[n].enabled = is_modeA

        for n in modeB_names:
            if n in pmap:
                pmap[n].enabled = (not is_modeA)

        return

    def updateMessages(self, parameters):
        mode = parameters[0].valueAsText or "POINT_LAYER_WITH_TEXT_FIELD"
        rings_txt = parameters[8].valueAsText
        if is_empty_gp(rings_txt):
            parameters[8].setErrorMessage("Rings must be provided.")
        else:
            rings = self._parse_multivalue_numbers(rings_txt)
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
        return

    # -----------------------------
    # Internal helpers
    # -----------------------------
    @staticmethod
    def _parse_multivalue_numbers(val):
        if val is None:
            return []
        s = to_unicode(val).replace(";", " ").replace(",", " ")
        parts = [p for p in s.split() if p.strip()]
        out = []
        for p in parts:
            try:
                out.append(float(p))
            except:
                pass
        return out

    @staticmethod
    def _angles(directions):
        step = 2.0 * math.pi / float(directions)
        return [i * step for i in range(directions)]

    @staticmethod
    def _extent_overlaps(ext1, ext2):
        return not (
            ext1.XMax < ext2.XMin or ext1.XMin > ext2.XMax or
            ext1.YMax < ext2.YMin or ext1.YMin > ext2.YMax
        )

    @staticmethod
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

    @staticmethod
    def _rect_polygon(sr, cx, cy, w, h):
        hw = w / 2.0
        hh = h / 2.0
        arr = arcpy.Array([
            arcpy.Point(cx - hw, cy - hh),
            arcpy.Point(cx + hw, cy - hh),
            arcpy.Point(cx + hw, cy + hh),
            arcpy.Point(cx - hw, cy + hh),
            arcpy.Point(cx - hw, cy - hh)
        ])
        return arcpy.Polygon(arr, sr)

    @staticmethod
    def _rect_polygon_rotated(sr, cx, cy, w, h, angle_rad):
        """
        Rotated rectangle polygon centered at (cx,cy) with width w and height h.
        angle_rad is counter-clockwise in radians.
        """
        try:
            hw = w / 2.0
            hh = h / 2.0
            ca = math.cos(angle_rad)
            sa = math.sin(angle_rad)

            # Local corners (x,y)
            corners = [(-hw, -hh), (hw, -hh), (hw, hh), (-hw, hh), (-hw, -hh)]
            pts = []
            for (x, y) in corners:
                xr = (x * ca) - (y * sa)
                yr = (x * sa) + (y * ca)
                pts.append(arcpy.Point(cx + xr, cy + yr))
            arr = arcpy.Array(pts)
            return arcpy.Polygon(arr, sr)
        except:
            # Fallback to axis-aligned
            return ElevationTextDeconflict_2Mode_EN_FINAL._rect_polygon(sr, cx, cy, w, h)

    @staticmethod
    def _biased_angles(angles, bias):
        """
        Reorder angles list based on a simple preference (still deterministic).
        bias: NONE | CARDINAL_FIRST | DIAGONAL_FIRST
        """
        if not angles:
            return angles
        try:
            b = (bias or "NONE").upper()
        except:
            b = "NONE"
        if b == "NONE":
            return angles

        def _score_card(a):
            # closeness to multiples of 90 deg
            deg = (a * 180.0 / math.pi) % 360.0
            targets = [0.0, 90.0, 180.0, 270.0]
            return min([abs(deg - t) for t in targets])

        def _score_diag(a):
            # closeness to multiples of 45 deg but not 90
            deg = (a * 180.0 / math.pi) % 360.0
            targets = [45.0, 135.0, 225.0, 315.0]
            return min([abs(deg - t) for t in targets])

        if b == "CARDINAL_FIRST":
            return sorted(angles, key=lambda a: (_score_card(a), a))
        if b == "DIAGONAL_FIRST":
            return sorted(angles, key=lambda a: (_score_diag(a), a))
        return angles

    @staticmethod
    def _iter_candidates(pattern, rings_sorted, angles, max_ring, spiral_step):
        """
        Yield (r, ang_rad, dx, dy) candidate offsets in map units.
        pattern: FIXED_RINGS | SPIRAL | GREEDY
        """
        pat = (pattern or "FIXED_RINGS").upper()

        if pat == "SPIRAL":
            # Spiral outward with small radius increments
            step = float(spiral_step) if spiral_step and float(spiral_step) > 0 else 0.0
            if step <= 0.0:
                try:
                    step = float(min(rings_sorted)) / 2.0
                except:
                    step = 1.0
            if step <= 0.0:
                step = 1.0

            # Angle step based on provided angles count
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

        # FIXED_RINGS or GREEDY (default)
        # GREEDY uses the same candidates but relies on ordering in rings/angles (closest first).
        for r in rings_sorted:
            for ang in angles:
                dx = float(r) * math.cos(ang)
                dy = float(r) * math.sin(ang)
                yield (float(r), float(ang), dx, dy)


    def _conflicts(self, shape_geom, obstacles, conflict_mode):
        if shape_geom is None:
            return True
        s_ext = shape_geom.extent
        for (g, ge) in obstacles:
            if not g:
                continue
            if conflict_mode in ("FAST_EXTENT_ONLY", "BALANCED_EXTENT_THEN_GEOMETRY"):
                if not self._extent_overlaps(s_ext, ge):
                    continue
                if conflict_mode == "FAST_EXTENT_ONLY":
                    return True
            if conflict_mode in ("BALANCED_EXTENT_THEN_GEOMETRY", "ACCURATE_GEOMETRY_ONLY"):
                try:
                    if not shape_geom.disjoint(g):
                        return True
                except:
                    return True
        return False

    # ------------------------------------------------------------
    # Obstacle cache (read-only)
    # ------------------------------------------------------------
    def _build_obstacle_cache(self, log, obstacle_layers, max_per_layer):
        log.info("OBSTACLE_CACHE: building in-memory cache for obstacle layers...")
        cache = []
        safety_cap = 20000

        for lyr in obstacle_layers:
            try:
                if not arcpy.Exists(lyr):
                    log.warn("OBSTACLE_CACHE: layer does not exist: {0}".format(lyr))
                    continue

                cap = max_per_layer if (max_per_layer and max_per_layer > 0) else safety_cap
                if (not max_per_layer) or max_per_layer == 0:
                    log.warn("OBSTACLE_CACHE: max_features_per_layer=0, using internal safety cap {0} for layer={1}".format(safety_cap, lyr))

                cnt = 0
                with arcpy.da.SearchCursor(lyr, ["SHAPE@"]) as sc:
                    for (g,) in sc:
                        if not g:
                            continue
                        cache.append((g, g.extent))
                        cnt += 1
                        if cap and cnt >= cap:
                            break
                log.info("OBSTACLE_CACHE: layer={0} cached={1}".format(lyr, cnt))
            except Exception as e:
                log.warn("OBSTACLE_CACHE: failed layer={0} err={1}".format(lyr, to_unicode(e)))

        log.info("OBSTACLE_CACHE: total cached obstacles={0}".format(len(cache)))
        return cache

    def _obstacles_near(self, obstacle_cache, anchor_x, anchor_y, search_dist):
        xmin = anchor_x - search_dist
        xmax = anchor_x + search_dist
        ymin = anchor_y - search_dist
        ymax = anchor_y + search_dist

        class _E(object):
            pass
        e = _E()
        e.XMin, e.XMax, e.YMin, e.YMax = xmin, xmax, ymin, ymax

        out = []
        for (g, ge) in obstacle_cache:
            if ge.XMax < e.XMin or ge.XMin > e.XMax or ge.YMax < e.YMin or ge.YMin > e.YMax:
                continue
            out.append((g, ge))
        return out

    # ------------------------------------------------------------
    # Mode B: anchor mapping (NEAR)
    # ------------------------------------------------------------
    def _create_temp_polygon_from_annotation(self, log, anno_fc, base_sr):
        scratch = arcpy.env.scratchGDB
        temp_fc = arcpy.CreateUniqueName("anno_as_polygon", scratch)

        arcpy.management.CreateFeatureclass(
            out_path=scratch,
            out_name=os.path.basename(temp_fc),
            geometry_type="POLYGON",
            spatial_reference=base_sr
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

        log.info("Temp polygon created. rows={0} path={1}".format(inserted, temp_fc))
        return temp_fc, poly2anno

    def _build_anchor_map_modeB(self, log, anno_fc, anchor_points, link_method,
                                featureid_field, max_match_dist_text, base_sr):
        log.info("BUILD_ANCHOR_MAP: link_method={0}".format(link_method))

        anchor_xy = {}
        with arcpy.da.SearchCursor(anchor_points, ["OID@", "SHAPE@XY"]) as sc:
            for oid, xy in sc:
                anchor_xy[int(oid)] = xy

        mapping = {}

        if link_method == "FEATUREID_MATCH":
            with arcpy.da.SearchCursor(anno_fc, ["OID@", featureid_field]) as sc:
                for aoid, fid in sc:
                    if fid is None:
                        continue
                    try:
                        fid_int = int(fid)
                    except:
                        continue
                    if fid_int in anchor_xy:
                        ax, ay = anchor_xy[fid_int]
                        mapping[int(aoid)] = (fid_int, ax, ay, 0.0)
            log.info("Anchor map built (FEATUREID_MATCH). matched={0}".format(len(mapping)))
            return mapping

        # NEAREST_POINT
        search_radius = ""
        t = to_unicode(max_match_dist_text or u"").strip()
        if t and t not in (u"0", u"0.0"):
            try:
                if float(t) > 0:
                    search_radius = t
            except:
                search_radius = ""

        temp_poly = None
        near_table = None

        try:
            temp_poly, poly2anno = self._create_temp_polygon_from_annotation(log, anno_fc, base_sr)
            scratch = arcpy.env.scratchGDB
            near_table = arcpy.CreateUniqueName("anno_anchor_near", scratch)

            log.info("GENERATE_NEAR_TABLE: radius={0}".format(search_radius if search_radius else "BLANK(no limit)"))
            arcpy.analysis.GenerateNearTable(
                in_features=temp_poly,
                near_features=anchor_points,
                out_table=near_table,
                search_radius=search_radius,
                location="NO_LOCATION",
                angle="NO_ANGLE",
                closest="CLOSEST",
                closest_count=1,
                method="PLANAR"
            )

            matched = 0
            with arcpy.da.SearchCursor(near_table, ["IN_FID", "NEAR_FID", "NEAR_DIST"]) as tc:
                for in_fid, near_fid, near_dist in tc:
                    if in_fid is None or near_fid is None:
                        continue
                    in_fid = int(in_fid)
                    near_fid = int(near_fid)
                    if in_fid not in poly2anno:
                        continue
                    anno_oid = poly2anno[in_fid]
                    if near_fid not in anchor_xy:
                        continue
                    ax, ay = anchor_xy[near_fid]
                    mapping[int(anno_oid)] = (near_fid, ax, ay, float(near_dist) if near_dist is not None else 0.0)
                    matched += 1

            log.info("Anchor map built (NEAREST_POINT). matched={0}".format(matched))
            return mapping
        finally:
            safe_delete(near_table, log)
            safe_delete(temp_poly, log)

    # ------------------------------------------------------------
    # Output location: SAME GDB / SAME Feature Dataset as input annotation
    # ------------------------------------------------------------
    def _get_output_container_for_annotation(self, anno_fc):
        d = arcpy.Describe(anno_fc)
        # d.path is the container path; for FC inside Feature Dataset, it is "...gdb\\DatasetName"
        return d.path

    def _ensure_field(self, fc, name, ftype, length=None):
        fields = [f.name.lower() for f in arcpy.ListFields(fc)]
        if name.lower() in fields:
            return
        if ftype.upper() == "TEXT" and length:
            arcpy.management.AddField(fc, name, ftype, field_length=length)
        else:
            arcpy.management.AddField(fc, name, ftype)

    # ------------------------------------------------------------
    # Execute
    # ------------------------------------------------------------
    def execute(self, parameters, messages):
        arcpy.env.overwriteOutput = True

        # ------------------------
        # Parameter map by NAME (safe even if parameter order changes)
        # ------------------------
        pmap = {}
        try:
            for p in parameters:
                try:
                    pmap[p.name] = p
                except:
                    pass
        except:
            pmap = {}

        def _p_text(name, default=None):
            try:
                if name in pmap:
                    v = pmap[name].valueAsText
                    if is_empty_gp(v):
                        return default
                    return v
            except:
                pass
            return default

        def _p_bool(name, default=False):
            try:
                if name in pmap and pmap[name].value is not None:
                    return bool(pmap[name].value)
            except:
                pass
            v = _p_text(name, None)
            if v is None:
                return default
            try:
                s = str(v).strip().lower()
                return s in ("true", "t", "1", "yes", "y", "on")
            except:
                return default

        def _p_int(name, default=0):
            try:
                if name in pmap and pmap[name].value is not None:
                    return int(pmap[name].value)
            except:
                pass
            try:
                v = _p_text(name, None)
                if v is None:
                    return default
                return int(float(v))
            except:
                return default

        def _p_float(name, default=0.0):
            try:
                if name in pmap and pmap[name].value is not None:
                    return float(pmap[name].value)
            except:
                pass
            try:
                v = _p_text(name, None)
                if v is None:
                    return default
                return float(v)
            except:
                return default

        mode = _p_text("input_mode", "POINT_LAYER_WITH_TEXT_FIELD")

        in_points = _p_text("in_points", "")
        text_field = _p_text("text_field", "")

        anno_layer = _p_text("anno_layer", "")
        anchor_points = _p_text("anchor_points", "")
        link_method = _p_text("link_method", "NEAREST_POINT")
        featureid_field = _p_text("featureid_field", "FeatureID")
        max_match_dist_text = _p_text("max_match_dist", "")

        rings = self._parse_multivalue_numbers(_p_text("rings", "2 4 6"))
        directions = _p_int("directions", 16)

        obstacle_layers_txt = _p_text("obstacle_layers", "")
        conflict_mode = _p_text("conflict_test_mode", "BALANCED_EXTENT_THEN_GEOMETRY")
        max_per_layer = _p_int("max_features_per_layer", 0)
        padding = _p_float("padding", 0.0)
        extra_search = _p_float("extra_search", 0.0)

        ref_scale = _p_int("reference_scale", 25000)
        font_pt = _p_float("font_size_pt", 8.0)
        k_fac = _p_float("char_width_factor", 0.60)

        report_text_mode = _p_text("report_text_mode", "ASCII_SAFE_REPLACE")
        preview_only = _p_bool("preview_only", False)
        debug_level = _p_text("debug_level", "OFF")
        debug_log_file = _p_text("debug_log_file", "")

        # New options (safe defaults)
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
        log.info("Tool start. mode={0} preview_only={1} reverse_offsets={2}".format(mode, preview_only, reverse_offsets))
        if log.path:
            log.info("Log file: {0}".format(log.path))

        # Validation
        if mode == "POINT_LAYER_WITH_TEXT_FIELD":
            if is_empty_gp(in_points) or is_empty_gp(text_field):
                raise arcpy.ExecuteError("Mode A selected but Input Point Layer / Text Field is empty.")
        else:
            if is_empty_gp(anno_layer) or is_empty_gp(anchor_points):
                raise arcpy.ExecuteError("Mode B selected but Annotation Layer / Anchor Points is empty.")

        if not rings:
            raise arcpy.ExecuteError("Rings must be provided (e.g., 2 4 6).")

        obstacle_layers = []
        if not is_empty_gp(obstacle_layers_txt):
            obstacle_layers = [s for s in to_unicode(obstacle_layers_txt).split(";") if s.strip()]
        if not obstacle_layers:
            raise arcpy.ExecuteError("No obstacle layers provided.")

        angles = self._biased_angles(self._angles(directions), direction_bias)
        rings_sorted = sorted(rings)
        max_ring = max(rings_sorted)

        max_attempts = len(rings_sorted) * directions * 2  # Safety limit to prevent infinite loops

        # Offset conversion helper (map units -> points at reference scale)
        meters_per_point = 0.0254 / 72.0
        ground_per_point = meters_per_point * float(ref_scale)
        if ground_per_point <= 0:
            ground_per_point = meters_per_point * 25000.0

        # Collect leader lines (optional)
        leader_rows = []
        label_positions_points = None
        leaderlines_fc = None


        # Spatial reference
        if mode == "POINT_LAYER_WITH_TEXT_FIELD":
            base_sr = arcpy.Describe(in_points).spatialReference
        else:
            base_sr = arcpy.Describe(anno_layer).spatialReference

        # Build obstacle cache (read-only)
        obstacle_cache = self._build_obstacle_cache(log, obstacle_layers, max_per_layer)

        # Reports -> always in scratch (QA layers ok anywhere)
        scratch = arcpy.env.scratchGDB
        out_all = arcpy.CreateUniqueName("elevtext_report_all", scratch)
        out_bad = arcpy.CreateUniqueName("elevtext_report_unresolved", scratch)

        arcpy.management.CreateFeatureclass(scratch, os.path.basename(out_all), "POINT", spatial_reference=base_sr)
        arcpy.management.AddField(out_all, "SrcOID", "LONG")
        arcpy.management.AddField(out_all, "Status", "TEXT", field_length=16)
        arcpy.management.AddField(out_all, "Ring", "DOUBLE")
        arcpy.management.AddField(out_all, "AngleDeg", "DOUBLE")
        arcpy.management.AddField(out_all, "Attempts", "LONG")
        arcpy.management.AddField(out_all, "AnchorOID", "LONG")
        arcpy.management.AddField(out_all, "AnchorDist", "DOUBLE")
        arcpy.management.AddField(out_all, "TextVal", "TEXT", field_length=128)  # Increased length

        arcpy.management.CreateFeatureclass(scratch, os.path.basename(out_bad), "POINT", spatial_reference=base_sr)
        arcpy.management.AddField(out_bad, "SrcOID", "LONG")
        arcpy.management.AddField(out_bad, "Reason", "TEXT", field_length=200)
        arcpy.management.AddField(out_bad, "AnchorOID", "LONG")
        arcpy.management.AddField(out_bad, "TextVal", "TEXT", field_length=128)  # Increased length

        report_all_rows = []
        report_bad_rows = []

        moved = unchanged = failed = skipped = 0
        moved_copy_final = None
        moved_only_final = None

        try:
            if mode == "POINT_LAYER_WITH_TEXT_FIELD":
                # ------------------------
                # MODE A: output label positions, DO NOT MOVE INPUT POINTS
                # ------------------------
                out_ws = arcpy.Describe(in_points).path  # container workspace of points
                out_name = "LabelPos_{0}".format(datetime.datetime.now().strftime("%H%M%S"))
                moved_copy_final = os.path.join(out_ws, out_name)

                log.info("MODE A: Creating output label-position FC: {0}".format(moved_copy_final))
                arcpy.management.CreateFeatureclass(out_ws, out_name, "POINT", spatial_reference=base_sr)

                self._ensure_field(moved_copy_final, "SrcOID", "LONG")
                self._ensure_field(moved_copy_final, "Status", "TEXT", length=16)
                self._ensure_field(moved_copy_final, "Ring", "DOUBLE")
                self._ensure_field(moved_copy_final, "AngleDeg", "DOUBLE")
                self._ensure_field(moved_copy_final, "Attempts", "LONG")
                self._ensure_field(moved_copy_final, "OrigX", "DOUBLE")
                self._ensure_field(moved_copy_final, "OrigY", "DOUBLE")
                self._ensure_field(moved_copy_final, "TextVal", "TEXT", length=128)  # Increased
                self._ensure_field(moved_copy_final, "ETD_MOVED", "SHORT")
                self._ensure_field(moved_copy_final, "DX_MU", "DOUBLE")
                self._ensure_field(moved_copy_final, "DY_MU", "DOUBLE")
                self._ensure_field(moved_copy_final, "DX_PT", "DOUBLE")
                self._ensure_field(moved_copy_final, "DY_PT", "DOUBLE")
                self._ensure_field(moved_copy_final, "AngleOut", "DOUBLE")
                self._ensure_field(moved_copy_final, "ETD_LEADER", "SHORT")


                with arcpy.da.SearchCursor(in_points, ["OID@", "SHAPE@", text_field]) as sc, \
                     arcpy.da.InsertCursor(moved_copy_final, ["SHAPE@", "SrcOID", "Status", "Ring", "AngleDeg", "Attempts", "OrigX", "OrigY", "TextVal", "ETD_MOVED", "DX_MU", "DY_MU", "DX_PT", "DY_PT", "AngleOut", "ETD_LEADER"]) as ic:

                    placed_label_boxes = []

                    for oid, geom, txt in sc:
                        if geom is None:
                            failed += 1
                            report_bad_rows.append((None, oid, "No geometry", -1, ""))
                            continue

                        ax, ay = geom.centroid.X, geom.centroid.Y
                        txt_u = to_unicode(txt)
                        txt_r = ascii_safe(txt_u) if report_text_mode == "ASCII_SAFE_REPLACE" else txt_u.encode("utf-8", "ignore")

                        w, h = self._text_extent_map_units(txt_u, font_pt, k_fac, ref_scale, padding)
                        half_diag = 0.5 * math.sqrt(w*w + h*h)
                        search_dist = max_ring + half_diag + extra_search
                        near_obs = self._obstacles_near(obstacle_cache, ax, ay, search_dist)

                        rect0 = self._rect_polygon(base_sr, ax, ay, w, h)
                        if not self._conflicts(rect0, near_obs, conflict_mode):
                            unchanged += 1
                            p = arcpy.PointGeometry(arcpy.Point(ax, ay), base_sr)
                            ic.insertRow((p, oid, "UNCHANGED", 0.0, 0.0, 0, ax, ay, txt_r, 0))
                            report_all_rows.append((p, oid, "UNCHANGED", 0.0, 0.0, 0, oid, 0.0, txt_r))
                            continue

                        placed = False
                        attempts = 0
                        chosen = (ax, ay, 0.0, 0.0)

                        for (r, ang, dx, dy) in self._iter_candidates(search_pattern, rings_sorted, angles, max_ring, spiral_step):
                            attempts += 1
                            if attempts > max_attempts:
                                break

                            cx = ax + dx
                            cy = ay + dy

                            # Conflict box (Mode A): rotated or axis-aligned
                            if modeA_rotated_box:
                                rect = self._rect_polygon_rotated(base_sr, cx, cy, w, h, ang)
                            else:
                                rect = self._rect_polygon(base_sr, cx, cy, w, h)

                            # Optionally prevent label-label overlap (greedy)
                            obs2 = near_obs
                            if avoid_label_label and placed_label_boxes:
                                obs2 = near_obs + placed_label_boxes

                            if not self._conflicts(rect, obs2, conflict_mode):
                                placed = True
                                chosen = (cx, cy, float(r), (ang * 180.0 / math.pi))
                                # Store accepted rectangle as obstacle for subsequent labels
                                if avoid_label_label:
                                    try:
                                        placed_label_boxes.append((rect, rect.extent))
                                    except:
                                        pass
                                break
                        if placed:
                            cx, cy, rr2, aa2 = chosen
                            moved += 1
                            p = arcpy.PointGeometry(arcpy.Point(cx, cy), base_sr)
                            ic.insertRow((p, oid, "MOVED", rr2, aa2, attempts, ax, ay, txt_r, 1, (cx-ax), (cy-ay), ((cx-ax)/float(ground_per_point)), ((cy-ay)/float(ground_per_point)), aa2, 1))
                            report_all_rows.append((p, oid, "MOVED", rr2, aa2, attempts, oid, 0.0, txt_r))
                            if create_leaderlines:
                                leader_rows.append((oid, ax, ay, cx, cy, rr2))
                        else:
                            failed += 1
                            p0 = arcpy.PointGeometry(arcpy.Point(ax, ay), base_sr)
                            ic.insertRow((p0, oid, "FAILED", 0.0, 0.0, attempts, ax, ay, txt_r, 0, 0.0, 0.0, 0.0, 0.0, 0.0, 0))
                            report_all_rows.append((p0, oid, "FAILED", 0.0, 0.0, attempts, oid, 0.0, txt_r))
                            report_bad_rows.append((p0, oid, "No free position found within rings/directions or max attempts exceeded", oid, txt_r))
                if create_moved_only:
                    moved_only_name = "LabelPos_MovedOnly_{0}".format(datetime.datetime.now().strftime("%H%M%S"))
                    moved_only_final = os.path.join(out_ws, moved_only_name)
                    lyr_name = "lyr_labelpos_{0}".format(uuid.uuid4().hex[:8])
                    arcpy.management.MakeFeatureLayer(moved_copy_final, lyr_name)
                    arcpy.management.SelectLayerByAttribute(lyr_name, "NEW_SELECTION", "ETD_MOVED = 1")
                    arcpy.management.CopyFeatures(lyr_name, moved_only_final)
                    arcpy.management.Delete(lyr_name)
                else:
                    moved_only_final = None

                # Optional: Leader lines output (Polyline) for moved labels (Mode A)
                if create_leaderlines:
                    try:
                        ll_name = "LeaderLines_{0}".format(datetime.datetime.now().strftime("%H%M%S"))
                        leaderlines_fc = os.path.join(out_ws, ll_name)
                        arcpy.management.CreateFeatureclass(out_ws, ll_name, "POLYLINE", spatial_reference=base_sr)
                        self._ensure_field(leaderlines_fc, "SrcOID", "LONG")
                        self._ensure_field(leaderlines_fc, "LenMU", "DOUBLE")

                        with arcpy.da.InsertCursor(leaderlines_fc, ["SHAPE@", "SrcOID", "LenMU"]) as il:
                            for (oid2, ax2, ay2, cx2, cy2, rr2) in leader_rows:
                                try:
                                    if leaderlines_moved_only and float(rr2) <= 0.0:
                                        continue
                                    arr = arcpy.Array([arcpy.Point(ax2, ay2), arcpy.Point(cx2, cy2)])
                                    geom = arcpy.Polyline(arr, base_sr)
                                    il.insertRow((geom, int(oid2), float(rr2)))
                                except:
                                    pass
                    except:
                        leaderlines_fc = None


            else:
                # ------------------------
                # MODE B: Create outputs in SAME container as input annotation
                # And apply movement ONLY via XOffset/YOffset (point units)
                # ------------------------
                out_container = self._get_output_container_for_annotation(anno_layer)
                log.info("Mode B: output container (same as input annotation) = {0}".format(out_container))

                stamp = datetime.datetime.now().strftime("%H%M%S")
                final_copy_name = "Annotation_Moved_{0}".format(stamp)
                final_moved_only_name = "Annotation_MovedOnly_{0}".format(stamp)

                moved_copy_final = os.path.join(out_container, final_copy_name)
                moved_only_final = os.path.join(out_container, final_moved_only_name)

                if not create_moved_only:
                    moved_only_final = None


                # Make an editable copy in same container (so it behaves like original in ArcMap)
                log.info("MODE B: CopyFeatures (same DB) input -> {0}".format(moved_copy_final))
                arcpy.management.CopyFeatures(anno_layer, moved_copy_final)

                # QA fields
                self._ensure_field(moved_copy_final, "ETD_MOVED", "SHORT")
                self._ensure_field(moved_copy_final, "DX_MU", "DOUBLE")
                self._ensure_field(moved_copy_final, "DY_MU", "DOUBLE")
                self._ensure_field(moved_copy_final, "DX_PT", "DOUBLE")
                self._ensure_field(moved_copy_final, "DY_PT", "DOUBLE")
                self._ensure_field(moved_copy_final, "AngleOut", "DOUBLE")
                self._ensure_field(moved_copy_final, "ETD_LEADER", "SHORT")

                self._ensure_field(moved_copy_final, "ETD_STATUS", "TEXT", length=16)
                self._ensure_field(moved_copy_final, "ETD_RING", "DOUBLE")
                self._ensure_field(moved_copy_final, "ETD_ANGLE", "DOUBLE")
                self._ensure_field(moved_copy_final, "ETD_ATT", "LONG")
                self._ensure_field(moved_copy_final, "ETD_AOID", "LONG")
                self._ensure_field(moved_copy_final, "ETD_ADIST", "DOUBLE")

                # anchor map built on the COPY (OID changes!)
                log.info("MODE B: Building anchor map on the COPIED annotation...")
                anchor_map = self._build_anchor_map_modeB(
                    log, moved_copy_final, anchor_points, link_method,
                    featureid_field, max_match_dist_text, base_sr
                )

                # Offset conversion: ground meters -> points at reference scale
                meters_per_point = 0.0254 / 72.0
                ground_per_point = meters_per_point * float(ref_scale)
                if ground_per_point <= 0:
                    ground_per_point = meters_per_point * 25000.0
                log.info("MODE B: offset conversion ground_per_point={0} (ref_scale={1})".format(ground_per_point, ref_scale))

                # Get workspace for Editor
                # Correctly derive the file geodatabase path for the Editor
                desc_anno = arcpy.Describe(anno_layer)
                workspace = desc_anno.catalogPath
                original_workspace = workspace
                while workspace and not os.path.basename(workspace).lower().endswith('.gdb'):
                    workspace = os.path.dirname(workspace)
                if not workspace:
                    raise arcpy.ExecuteError(
                        "Could not determine file geodatabase path from annotation layer: {}".format(original_workspace)
                    )
                log.info("Derived workspace (GDB) for editing: {}".format(workspace))

                editor = arcpy.da.Editor(workspace)
                editor.startEditing(False, True)  # no undo, multiuser
                editor.startOperation()

                # UpdateCursor on moved_copy_final
                # IMPORTANT: we only write XOffset/YOffset (+ QA fields)
                u_fields = ["OID@", "SHAPE@", "TextString", "XOffset", "YOffset",
                            "ETD_MOVED", "ETD_STATUS", "ETD_RING", "ETD_ANGLE", "ETD_ATT", "ETD_AOID", "ETD_ADIST"]
                with arcpy.da.UpdateCursor(moved_copy_final, u_fields) as ucur:
                    for aoid, ageom, textstr, xo, yo, mv, st, rr, aa, att, ao, ad in ucur:
                        if ageom is None:
                            failed += 1
                            report_bad_rows.append((None, aoid, "No geometry", -1, ""))
                            continue

                        txt_u = to_unicode(textstr)
                        txt_r = ascii_safe(txt_u) if report_text_mode == "ASCII_SAFE_REPLACE" else txt_u.encode("utf-8", "ignore")

                        if int(aoid) not in anchor_map:
                            skipped += 1
                            c = ageom.centroid
                            p = arcpy.PointGeometry(arcpy.Point(c.X, c.Y), base_sr)
                            report_all_rows.append((p, aoid, "SKIPPED", 0.0, 0.0, 0, -1, -1.0, txt_r))
                            ucur.updateRow((aoid, ageom, textstr, xo, yo, 0, "SKIPPED", 0.0, 0.0, 0, -1, -1.0))
                            continue

                        anchor_oid, ax, ay, adist = anchor_map[int(aoid)]

                        # Use current geometry extent as label box (as in your old code)
                        ext = ageom.extent
                        w = abs(ext.XMax - ext.XMin)
                        h = abs(ext.YMax - ext.YMin)
                        half_diag = 0.5 * math.sqrt(w*w + h*h)
                        search_dist = max_ring + half_diag + extra_search
                        near_obs = self._obstacles_near(obstacle_cache, ax, ay, search_dist)

                        # If current has no conflicts -> unchanged
                        if not self._conflicts(ageom, near_obs, conflict_mode):
                            unchanged += 1
                            c = ageom.centroid
                            p = arcpy.PointGeometry(arcpy.Point(c.X, c.Y), base_sr)
                            report_all_rows.append((p, aoid, "UNCHANGED", 0.0, 0.0, 0, anchor_oid, adist, txt_r))
                            ucur.updateRow((aoid, ageom, textstr, xo, yo, 0, "UNCHANGED", 0.0, 0.0, 0, anchor_oid, adist))
                            continue

                        # Current centroid as start, but movement must be applied as offsets relative to anchor
                        curr_c = ageom.centroid
                        placed = False
                        attempts = 0
                        chosen = (curr_c.X, curr_c.Y, 0.0, 0.0, 0.0, 0.0)

                        for (r, ang, dx, dy) in self._iter_candidates(search_pattern, rings_sorted, angles, max_ring, spiral_step):
                                attempts += 1
                                if attempts > max_attempts:
                                    break

                                cx = curr_c.X + dx
                                cy = curr_c.Y + dy

                                try:
                                    moved_geom = ageom
                                    # Apply movement to a temporary geometry for conflict checking ONLY
                                    parts_array = arcpy.Array()
                                    for part in ageom:
                                        part_array = arcpy.Array()
                                        for p in part:
                                            if p:
                                                part_array.add(arcpy.Point(p.X + dx, p.Y + dy))
                                        parts_array.add(part_array)
                                    moved_geom = arcpy.Polygon(parts_array, base_sr)
                                except:
                                    moved_geom = None

                                if moved_geom and (not self._conflicts(moved_geom, near_obs, conflict_mode)):
                                    placed = True
                                    chosen = (cx, cy, float(r), (ang * 180.0 / math.pi), dx, dy)
                                    break
                        if placed:
                            cx, cy, rr2, aa2, dx, dy = chosen

                            # Convert dx/dy in ground units to annotation offset units (points at ref scale)
                            dx_pt = float(dx) / float(ground_per_point)
                            dy_pt = float(dy) / float(ground_per_point)

                            # Reverse if checked
                            if reverse_offsets:
                                dx_pt = -dx_pt
                                dy_pt = -dy_pt

                            # Apply ONLY to XOffset/YOffset (core movement fields)
                            # Preserve existing xo/yo by adding delta (so manual edits remain meaningful)
                            new_xo = (float(xo) if xo is not None else 0.0) + dx_pt
                            new_yo = (float(yo) if yo is not None else 0.0) + dy_pt

                            if create_leaderlines:
                                try:
                                    # anchor -> final label point (estimated)
                                    gx = ax + float(new_xo) * float(ground_per_point)
                                    gy = ay + float(new_yo) * float(ground_per_point)
                                    leader_rows.append((aoid, ax, ay, gx, gy, rr2))
                                except:
                                    pass


                            moved += 1
                            p = arcpy.PointGeometry(arcpy.Point(cx, cy), base_sr)
                            report_all_rows.append((p, aoid, "MOVED", rr2, aa2, attempts, anchor_oid, adist, txt_r))
                            ucur.updateRow((aoid, ageom, textstr, new_xo, new_yo, 1, "MOVED", rr2, aa2, attempts, anchor_oid, adist))
                        else:
                            failed += 1
                            c = ageom.centroid
                            p = arcpy.PointGeometry(arcpy.Point(c.X, c.Y), base_sr)
                            report_all_rows.append((p, aoid, "FAILED", 0.0, 0.0, attempts, anchor_oid, adist, txt_r))
                            report_bad_rows.append((p, aoid, "No free position found within rings/directions or max attempts exceeded", anchor_oid, txt_r))
                            ucur.updateRow((aoid, ageom, textstr, xo, yo, 0, "FAILED", 0.0, 0.0, attempts, anchor_oid, adist))

                editor.stopOperation()
                editor.stopEditing(True)

                # Optional: Create a POINT FC output representing final label positions (Mode B)
                if create_modeB_points:
                    try:
                        pt_name = "LabelPos_FromAnno_{0}".format(stamp)
                        label_positions_points = os.path.join(out_container, pt_name)
                        arcpy.management.CreateFeatureclass(out_container, pt_name, "POINT", spatial_reference=base_sr)
                        self._ensure_field(label_positions_points, "SrcOID", "LONG")
                        self._ensure_field(label_positions_points, "Status", "TEXT", length=16)
                        self._ensure_field(label_positions_points, "XOff_PT", "DOUBLE")
                        self._ensure_field(label_positions_points, "YOff_PT", "DOUBLE")
                        self._ensure_field(label_positions_points, "AngleDeg", "DOUBLE")
                        self._ensure_field(label_positions_points, "AnchorOID", "LONG")
                        self._ensure_field(label_positions_points, "AnchorDist", "DOUBLE")

                        with arcpy.da.SearchCursor(moved_copy_final, ["OID@", "ETD_STATUS", "XOffset", "YOffset", "ETD_ANGLE", "ETD_AOID", "ETD_ADIST"]) as scp,                              arcpy.da.InsertCursor(label_positions_points, ["SHAPE@", "SrcOID", "Status", "XOff_PT", "YOff_PT", "AngleDeg", "AnchorOID", "AnchorDist"]) as icp:
                            for aoid, stt, xo2, yo2, ang2, ao2, ad2 in scp:
                                if int(aoid) in anchor_map:
                                    anchor_oid, ax, ay, adist = anchor_map[int(aoid)]
                                    gx = ax + (float(xo2) if xo2 is not None else 0.0) * float(ground_per_point)
                                    gy = ay + (float(yo2) if yo2 is not None else 0.0) * float(ground_per_point)
                                    p = arcpy.PointGeometry(arcpy.Point(gx, gy), base_sr)
                                    icp.insertRow((p, int(aoid), stt, float(xo2) if xo2 is not None else 0.0, float(yo2) if yo2 is not None else 0.0,
                                                   float(ang2) if ang2 is not None else 0.0, int(anchor_oid), float(adist)))
                                else:
                                    # fallback: centroid
                                    try:
                                        g = arcpy.Describe(moved_copy_final).spatialReference
                                        # Not reliable; use origin
                                        p = arcpy.PointGeometry(arcpy.Point(0, 0), base_sr)
                                    except:
                                        p = arcpy.PointGeometry(arcpy.Point(0, 0), base_sr)
                                    icp.insertRow((p, int(aoid), stt, float(xo2) if xo2 is not None else 0.0, float(yo2) if yo2 is not None else 0.0,
                                                   float(ang2) if ang2 is not None else 0.0, -1, -1.0))
                    except:
                        label_positions_points = None

                # Optional: Leader lines output (Polyline) for moved labels
                if create_leaderlines:
                    try:
                        ll_name = "LeaderLines_{0}".format(stamp)
                        leaderlines_fc = os.path.join(out_container, ll_name)
                        arcpy.management.CreateFeatureclass(out_container, ll_name, "POLYLINE", spatial_reference=base_sr)
                        self._ensure_field(leaderlines_fc, "SrcOID", "LONG")
                        self._ensure_field(leaderlines_fc, "LenMU", "DOUBLE")

                        with arcpy.da.InsertCursor(leaderlines_fc, ["SHAPE@", "SrcOID", "LenMU"]) as il:
                            for (oid2, ax2, ay2, cx2, cy2, rr2) in leader_rows:
                                try:
                                    arr = arcpy.Array([arcpy.Point(ax2, ay2), arcpy.Point(cx2, cy2)])
                                    geom = arcpy.Polyline(arr, base_sr)
                                    il.insertRow((geom, int(oid2), float(rr2)))
                                except:
                                    pass
                    except:
                        leaderlines_fc = None
  # Save edits

                # ---- FORCE ArcMap Annotation redraw / dirty flag ----
                # Some annotation layers don't visually update until they get "dirtied" or refreshed.
                try:
                    # Touch a field so ArcMap treats the FC as updated for rendering
                    arcpy.management.CalculateField(
                        moved_copy_final,
                        "ETD_MOVED",
                        "!ETD_MOVED!",
                        "PYTHON_9.3"
                    )
                except:
                    pass


                # Optional: Write rotation into the annotation "Angle" field (Mode B)
                if apply_rotation_modeB:
                    try:
                        # Detect Angle field in annotation copy
                        angle_field = None
                        flds = [f.name for f in arcpy.ListFields(moved_copy_final)]
                        for nm in ("Angle", "ANGLE"):
                            if nm in flds:
                                angle_field = nm
                                break
                        if angle_field:
                            lyr_rot = "lyr_rot_{0}".format(uuid.uuid4().hex[:8])
                            arcpy.management.MakeFeatureLayer(moved_copy_final, lyr_rot)
                            arcpy.management.SelectLayerByAttribute(lyr_rot, "NEW_SELECTION", "ETD_MOVED = 1")
                            if rotation_write_mode == "ADD_DELTA":
                                # newAngle = oldAngle + ETD_ANGLE
                                codeblock = "def addang(a,b):\n  try:\n    return (float(a) if a is not None else 0.0) + (float(b) if b is not None else 0.0)\n  except:\n    return float(b) if b is not None else 0.0"
                                arcpy.management.CalculateField(lyr_rot, angle_field, "addang(!{0}!, !ETD_ANGLE!)".format(angle_field), "PYTHON_9.3", codeblock)
                            else:
                                # Absolute
                                arcpy.management.CalculateField(lyr_rot, angle_field, "!ETD_ANGLE!", "PYTHON_9.3")
                            arcpy.management.Delete(lyr_rot)
                    except:
                        pass
                
                try:
                    # Clear caches to make display update more reliable
                    arcpy.management.ClearWorkspaceCache()
                except:
                    pass
                
                try:
                    # Only works when running inside ArcMap session (toolbox)
                    arcpy.RefreshActiveView()
                    arcpy.RefreshTOC()
                    arcpy.RefreshCatalog(out_container)
                except:
                    pass
                # ---- END FORCE REDRAW ----
                
                # moved-only output (ROBUST for Annotation): use FeatureClassToFeatureClass with where_clause
                try:
                    out_container = os.path.dirname(moved_only_final)
                    out_name = os.path.basename(moved_only_final)
                    arcpy.conversion.FeatureClassToFeatureClass(
                        moved_copy_final,
                        out_container,
                        out_name,
                        "ETD_MOVED = 1"
                    )
                except:
                    # fallback (if conversion fails for any reason)
                    lyr_name = "lyr_anno_copy_{0}".format(uuid.uuid4().hex[:8])
                    arcpy.management.MakeFeatureLayer(moved_copy_final, lyr_name)
                    arcpy.management.SelectLayerByAttribute(lyr_name, "NEW_SELECTION", "ETD_MOVED = 1")
                    arcpy.management.CopyFeatures(lyr_name, moved_only_final)
                    arcpy.management.Delete(lyr_name)
                

            # write reports
            with arcpy.da.InsertCursor(out_all, ["SHAPE@", "SrcOID", "Status", "Ring", "AngleDeg",
                                                 "Attempts", "AnchorOID", "AnchorDist", "TextVal"]) as ic_all:
                for row in report_all_rows:
                    ic_all.insertRow(row)

            with arcpy.da.InsertCursor(out_bad, ["SHAPE@", "SrcOID", "Reason", "AnchorOID", "TextVal"]) as ic_bad:
                for row in report_bad_rows:
                    ic_bad.insertRow(row)

        except Exception as e:
            log.error("Exception: {0}".format(to_unicode(e)))
            log.error("ArcPy messages (2): {0}".format(arcpy.GetMessages(2)))
            log.error(traceback.format_exc())
            if 'editor' in locals() and editor.isEditing:
                editor.abortOperation()
                editor.stopEditing(False)
            raise

        # Set outputs (by parameter NAME - stable)
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
                # Mode A: moved_copy_final is already label positions; Mode B: label_positions_points
                pmap["out_label_positions"].value = (label_positions_points if label_positions_points else moved_copy_final)
            if "out_leaderlines" in pmap:
                pmap["out_leaderlines"].value = leaderlines_fc
        except:
            pass

        log.info("Finished. MOVED={0} UNCHANGED={1} FAILED={2} SKIPPED={3}".format(moved, unchanged, failed, skipped))
        if mode == "ANNOTATION_LAYER_AND_ANCHOR_POINTS":
            log.warn("Mode B: movement applied via XOffset/YOffset on the output copy (same DB). Input is not modified.")
        else:
            log.warn("Mode A: input points are NOT modified. Output is label positions.")
        log.info("Outputs: moved_copy={0} moved_only={1}".format(moved_copy_final, moved_only_final))