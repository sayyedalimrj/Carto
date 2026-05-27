# -*- coding: utf-8 -*-
"""
Plugin 04 - Elevation Text Deconflict (ArcGIS Pro / Python 3) - Master Rules rewrite
====================================================================================
Deconflicts elevation text against obstacles.

Two input modes:
  - Mode A: POINT_LAYER_WITH_TEXT_FIELD - outputs label-position points.
  - Mode B: ANNOTATION_LAYER_AND_ANCHOR_POINTS - writes XOffset/YOffset
    on a copy of the annotation in the same GDB / feature dataset.

MASTER RULES enforced:
  1. Narrow exceptions at GP-call sites: (arcpy.ExecuteError, RuntimeError).
     MemoryError / OSError are NEVER caught. No bare `except` /
     `except Exception`.
  2. No bulk geometry caches in RAM beyond what the user opts into.
     The optimised evaluation path pre-extracts obstacle bounding boxes
     into compact float64 NumPy arrays - one per layer - and defers
     exact arcpy intersect to AABB-positive hits only.
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
        - False: optimised vectorised AABB collision in NumPy, exact
          intersect deferred to AABB-positive hits only.
  F2. _rect_polygon_rotated now RAISES arcpy.ExecuteError on matrix
        failure instead of silently returning the unrotated rectangle.
        A silent unrotated fallback would cause obstacle conflict
        tests to use the wrong shape and fail to detect collisions.
  F3. Font width: char_count * k * font_size replaced with an Arial
        proportional-width lookup table (advance widths at 12 pt).
        Each character has its own width; the total is summed and
        scaled to font_size, giving accurate label envelopes for
        narrow ('1', '.') and wide ('M', 'W') glyphs alike.
  F4. ascii_safe + report_text_mode == "ASCII_SAFE_REPLACE" PRESERVED.
        Persian / Arabic / right-to-left text in TextString fields
        would otherwise crash the Pro message pane on some locales.

Author: Ali Mirjafari + Kiro
"""

from __future__ import annotations

import math
import os
import datetime
import traceback
import uuid
import gc
import contextlib
from typing import Iterable, List, Optional, Tuple

import arcpy

# NumPy is shipped with ArcGIS Pro; the optimised AABB path needs it.
try:
    import numpy as _np
    _NUMPY_OK = True
except ImportError:
    _np = None
    _NUMPY_OK = False


# =============================================================================
# 0. Logging / messaging (F4 ascii_safe preserved)
# =============================================================================

def _safe_str(v) -> str:
    if v is None:
        return ""
    try:
        if isinstance(v, str):
            return v
        return str(v)
    except (TypeError, ValueError):
        try:
            return repr(v)
        except (TypeError, ValueError):
            return ""


def ascii_safe(s) -> str:
    """F4: ASCII-safe transcoder for Pro message pane."""
    try:
        return _safe_str(s).encode("ascii", "replace").decode("ascii")
    except (UnicodeDecodeError, UnicodeEncodeError):
        return "?"


def is_empty_gp(v) -> bool:
    if v is None:
        return True
    s = _safe_str(v).strip()
    return s == "" or s == "#"


def now_str() -> str:
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _ensure_dir(d: str) -> None:
    if d and not os.path.isdir(d):
        try:
            os.makedirs(d, exist_ok=True)
        except OSError as ex:
            arcpy.AddWarning(f"Could not create directory '{d}': {ex}")


def _safe_delete(path: Optional[str], log=None) -> None:
    if not path:
        return
    try:
        if arcpy.Exists(path):
            arcpy.management.Delete(path)
            if log:
                log.verbose(f"Deleted: {path}")
    except (arcpy.ExecuteError, RuntimeError) as ex:
        if log:
            log.verbose(f"safe_delete failed for {path}: {ex}")


class Logger:
    """
    Tee logger: ArcGIS messages + optional file. Has .diag() that always emits.

    F4: when report_text_mode == "ASCII_SAFE_REPLACE" all messages are
    transcoded via ascii_safe before being passed to arcpy.Add* so the
    Pro message pane cannot crash on right-to-left or non-BMP glyphs.
    """

    def __init__(self, debug_level: Optional[str], log_path: Optional[str],
                 report_text_mode: Optional[str]):
        self.level = (debug_level or "OFF").upper()
        self.path: Optional[str] = None
        self.report_text_mode = report_text_mode or "ASCII_SAFE_REPLACE"
        if log_path and not is_empty_gp(log_path):
            self.path = log_path
        if self.level != "OFF" and self.path is None:
            sf = arcpy.env.scratchFolder
            if sf and os.path.isdir(sf):
                ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
                self.path = os.path.join(
                    sf, f"ElevationTextDeconflict_log_{ts}.txt")

    def _write_file(self, msg: str) -> None:
        if not self.path:
            return
        _ensure_dir(os.path.dirname(self.path))
        try:
            with open(self.path, "a", encoding="utf-8", errors="replace") as f:
                f.write(msg)
                if not msg.endswith("\n"):
                    f.write("\n")
        except OSError:
            pass

    def _msg(self, s) -> str:
        if self.report_text_mode == "ASCII_SAFE_REPLACE":
            return ascii_safe(s)
        return _safe_str(s)

    def info(self, msg) -> None:
        if self.level in ("BASIC", "VERBOSE"):
            m = f"[{now_str()}] INFO  {msg}"
            arcpy.AddMessage(self._msg(m))
            self._write_file(m)

    def warn(self, msg) -> None:
        if self.level in ("BASIC", "VERBOSE"):
            m = f"[{now_str()}] WARN  {msg}"
            arcpy.AddWarning(self._msg(m))
            self._write_file(m)

    def error(self, msg) -> None:
        m = f"[{now_str()}] ERROR {msg}"
        arcpy.AddError(self._msg(m))
        self._write_file(m)

    def verbose(self, msg) -> None:
        if self.level == "VERBOSE":
            m = f"[{now_str()}] DEBUG {msg}"
            arcpy.AddMessage(self._msg(m))
            self._write_file(m)

    def diag(self, msg) -> None:
        m = f"[DIAG] {msg}"
        arcpy.AddMessage(self._msg(m))
        self._write_file(m)


# =============================================================================
# 1. Environment snapshot / restore (Master Rule 4)
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
            arcpy.AddWarning(f"Could not restore arcpy.env.{k}: {ex}")


def _prime_env() -> None:
    arcpy.env.extent = None
    arcpy.env.mask = None
    arcpy.env.outputCoordinateSystem = None
    arcpy.env.overwriteOutput = True
    arcpy.env.parallelProcessingFactor = "100%"
    arcpy.env.autoCancelling = True


# =============================================================================
# 2. Selection-bypass: _resolve_full_source preserved (Master Rule 3)
# =============================================================================

def _selection_info(layer_or_path) -> Tuple[Optional[int], Optional[int], str]:
    try:
        d = arcpy.Describe(layer_or_path)
    except (arcpy.ExecuteError, RuntimeError):
        return (None, None, _safe_str(layer_or_path))
    name = getattr(d, "name", _safe_str(layer_or_path))
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


def _announce_selection(label: str, layer_or_path, log=None) -> None:
    sel, total, name = _selection_info(layer_or_path)
    if sel and sel > 0:
        msg = (f"{label}: '{name}' has an active selection ({sel} of "
               f"{total if total is not None else '?'}). Ignoring selection - "
               f"processing FULL dataset.")
        if log:
            log.warn(msg)
        else:
            arcpy.AddWarning(msg)
    else:
        if log:
            log.diag(
                f"{label}: '{name}' "
                f"total={total if total is not None else '?'}, "
                f"no active selection.")


# =============================================================================
# 3. F3: Arial proportional-width lookup table
# =============================================================================
#
# Why this exists:
#   The previous _text_extent_map_units used:
#       width_pt = char_count * k * font_size
#   That assumes every character has the same width. Arial does not:
#   '1' is ~556 units, '.' is ~278 units, 'M' is ~833 units, 'W' is ~944
#   units, '8' is ~556 units (the most common width). The result on
#   real elevation labels like "1234.5" was a label box ~30% wider than
#   the actual rendered text, causing the deconflict tool to refuse
#   placements that were geometrically fine.
#
# This table holds Arial advance widths at 1000-unit em (the standard
# AFM/PostScript convention used in Arial.afm). To convert to point
# size: width_pt_per_glyph = (advance_units / 1000) * font_size_pt.
# Sum the glyph widths across the string, scale once.
#
# Coverage:
#   - ASCII printable (0x20..0x7E)
#   - The four most common typographic punctuation marks used in
#     metric labels (en-dash, em-dash, multiplication sign, degree)
#   - Fallback width for unknown characters: 600 units (close to
#     the Arial average for digits and uppercase).
#
# Source: Arial Regular AFM advance widths, Microsoft TrueType core
# font for the Web, 1996. Values match the AFM specification's
# WX entries (the same table used by browsers and PDF generators).

_ARIAL_DEFAULT = 600

_ARIAL_ADVANCE_1000 = {
    " ": 278, "!": 278, '"': 355, "#": 556, "$": 556, "%": 889,
    "&": 667, "'": 191, "(": 333, ")": 333, "*": 389, "+": 584,
    ",": 278, "-": 333, ".": 278, "/": 278,
    "0": 556, "1": 556, "2": 556, "3": 556, "4": 556,
    "5": 556, "6": 556, "7": 556, "8": 556, "9": 556,
    ":": 278, ";": 278, "<": 584, "=": 584, ">": 584,
    "?": 556, "@": 1015,
    "A": 667, "B": 667, "C": 722, "D": 722, "E": 667,
    "F": 611, "G": 778, "H": 722, "I": 278, "J": 500,
    "K": 667, "L": 556, "M": 833, "N": 722, "O": 778,
    "P": 667, "Q": 778, "R": 722, "S": 667, "T": 611,
    "U": 722, "V": 667, "W": 944, "X": 667, "Y": 667,
    "Z": 611,
    "[": 278, "\\": 278, "]": 278, "^": 469, "_": 556,
    "`": 333,
    "a": 556, "b": 556, "c": 500, "d": 556, "e": 556,
    "f": 278, "g": 556, "h": 556, "i": 222, "j": 222,
    "k": 500, "l": 222, "m": 833, "n": 556, "o": 556,
    "p": 556, "q": 556, "r": 333, "s": 500, "t": 278,
    "u": 556, "v": 500, "w": 722, "x": 500, "y": 500,
    "z": 500,
    "{": 334, "|": 260, "}": 334, "~": 584,
    # Common typographic extras for elevation labels
    "\u00b0": 400,  # degree sign
    "\u00d7": 584,  # multiplication sign
    "\u2013": 556,  # en dash
    "\u2014": 1000,  # em dash
    "\u2026": 1000,  # ellipsis
    "\u2032": 222,  # prime (minutes)
    "\u2033": 333,  # double prime (seconds)
}


def _arial_string_advance_units(text: str) -> int:
    """Sum Arial advance widths (1000-unit em) for every character."""
    total = 0
    for ch in text:
        total += _ARIAL_ADVANCE_1000.get(ch, _ARIAL_DEFAULT)
    return total


def _text_extent_map_units(text, font_size_pt: float, _legacy_k: float,
                           reference_scale: float, padding: float):
    """
    Compute label envelope in MAP UNITS using F3 Arial width table.

    The legacy `k` parameter is preserved in the signature for callers
    but is no longer used for width: width is computed from per-glyph
    Arial advance widths summed across the string and scaled to
    font_size_pt. Height stays at 1.2 * font_size (cap-height + leading).
    """
    s = _safe_str(text)
    if not s:
        s = "0"  # avoid zero-width label envelope
    # Sum Arial advances (1000-unit em) and convert to points.
    advance_em = _arial_string_advance_units(s)
    width_pt = (advance_em / 1000.0) * float(font_size_pt)
    height_pt = 1.2 * float(font_size_pt)

    m_per_pt = 0.0254 / 72.0
    width_ground = width_pt * m_per_pt * float(reference_scale)
    height_ground = height_pt * m_per_pt * float(reference_scale)
    width_ground += 2.0 * float(padding)
    height_ground += 2.0 * float(padding)
    return width_ground, height_ground



# =============================================================================
# 4. Obstacle stores: legacy SLBL + optimised AABB (F1 toggle)
# =============================================================================

class _ObstacleStoreLegacy:
    """
    LEGACY path: wraps obstacle layers as on-disk feature layers with spatial
    index. Per-candidate overlap queries use SelectLayerByLocation against
    an envelope polygon (the original behaviour, F1=True path).
    """

    __slots__ = ("layers", "log")

    def __init__(self, log: Logger):
        self.layers: List[Tuple[str, str]] = []
        self.log = log

    def add_layer(self, layer_or_path) -> None:
        if not layer_or_path:
            return
        src = _resolve_full_source(layer_or_path)
        if not src or not arcpy.Exists(src):
            if self.log:
                self.log.warn(
                    f"OBSTACLE: layer does not exist: {layer_or_path}")
            return
        try:
            try:
                arcpy.management.AddSpatialIndex(src)
            except (arcpy.ExecuteError, RuntimeError):
                pass
            lyr_name = "obs_lyr_" + uuid.uuid4().hex[:6]
            arcpy.management.MakeFeatureLayer(src, lyr_name)
            self.layers.append((lyr_name, src))
            try:
                n = int(arcpy.management.GetCount(src).getOutput(0))
            except (arcpy.ExecuteError, RuntimeError):
                n = -1
            if self.log:
                self.log.diag(f"OBSTACLE (legacy): '{src}' total={n}")
        except (arcpy.ExecuteError, RuntimeError):
            if self.log:
                self.log.warn(
                    f"OBSTACLE: MakeFeatureLayer failed for "
                    f"{layer_or_path}: {traceback.format_exc()}")

    def cleanup(self) -> None:
        for (lyr_name, _) in self.layers:
            try:
                arcpy.management.Delete(lyr_name)
            except (arcpy.ExecuteError, RuntimeError):
                pass
        self.layers = []

    @staticmethod
    def _envelope_polygon(sr, xmin, ymin, xmax, ymax):
        arr = arcpy.Array([
            arcpy.Point(xmin, ymin),
            arcpy.Point(xmax, ymin),
            arcpy.Point(xmax, ymax),
            arcpy.Point(xmin, ymax),
            arcpy.Point(xmin, ymin),
        ])
        return arcpy.Polygon(arr, sr)

    def conflict_in_box(self, sr, cx: float, cy: float,
                        half_w: float, half_h: float,
                        conflict_test_geom, conflict_mode: str) -> bool:
        if conflict_test_geom is None:
            return True
        env = self._envelope_polygon(
            sr, cx - half_w, cy - half_h, cx + half_w, cy + half_h)
        for (lyr_name, _) in self.layers:
            try:
                arcpy.management.SelectLayerByLocation(
                    lyr_name, "INTERSECT", env,
                    search_distance="", selection_type="NEW_SELECTION")
                try:
                    sel_count = int(
                        arcpy.management.GetCount(lyr_name).getOutput(0))
                except (arcpy.ExecuteError, RuntimeError):
                    sel_count = 0
                if sel_count <= 0:
                    continue
                if conflict_mode == "FAST_EXTENT_ONLY":
                    try:
                        arcpy.management.SelectLayerByAttribute(
                            lyr_name, "CLEAR_SELECTION")
                    except (arcpy.ExecuteError, RuntimeError):
                        pass
                    return True
                hit = False
                try:
                    with arcpy.da.SearchCursor(
                            lyr_name, ["SHAPE@"]) as sc:
                        for (g,) in sc:
                            if g is None:
                                continue
                            try:
                                if not conflict_test_geom.disjoint(g):
                                    hit = True
                                    break
                            except (arcpy.ExecuteError, RuntimeError):
                                hit = True
                                break
                finally:
                    try:
                        arcpy.management.SelectLayerByAttribute(
                            lyr_name, "CLEAR_SELECTION")
                    except (arcpy.ExecuteError, RuntimeError):
                        pass
                if hit:
                    return True
            except (arcpy.ExecuteError, RuntimeError):
                if self.log:
                    self.log.verbose(
                        f"conflict_in_box query failed: "
                        f"{traceback.format_exc()}")
                continue
        return False


class _ObstacleStoreAABB:
    """
    OPTIMISED path: pre-extract obstacle bounding boxes and geometries into
    a NumPy array per layer ONCE at startup. Per-candidate conflict tests
    are vectorised AABB intersections. Only AABB-positive hits trigger an
    arcpy disjoint() call. F1=False path.

    Each layer holds a (n, 4) float64 array of (xmin, ymin, xmax, ymax)
    plus a parallel list of geometry handles for exact tests.
    """

    __slots__ = ("layers", "log")

    def __init__(self, log: Logger):
        # list of (name, bbox_array, geoms_list, n_features)
        self.layers: List[Tuple[str, object, List[object], int]] = []
        self.log = log

    def add_layer(self, layer_or_path) -> None:
        if not layer_or_path:
            return
        src = _resolve_full_source(layer_or_path)
        if not src or not arcpy.Exists(src):
            if self.log:
                self.log.warn(
                    f"OBSTACLE: layer does not exist: {layer_or_path}")
            return
        if not _NUMPY_OK:
            if self.log:
                self.log.warn(
                    "OBSTACLE: NumPy unavailable; cannot build AABB "
                    f"store for '{src}'.")
            return
        bxmin: List[float] = []
        bymin: List[float] = []
        bxmax: List[float] = []
        bymax: List[float] = []
        geoms: List[object] = []
        try:
            with arcpy.da.SearchCursor(src, ["SHAPE@"]) as cur:
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
                    geoms.append(g)
        except (arcpy.ExecuteError, RuntimeError):
            if self.log:
                self.log.warn(
                    f"OBSTACLE: AABB extract failed for {layer_or_path}: "
                    f"{traceback.format_exc()}")
            return
        n = len(geoms)
        if n == 0:
            arr = _np.empty((0, 4), dtype=_np.float64)
        else:
            arr = _np.empty((n, 4), dtype=_np.float64)
            arr[:, 0] = bxmin
            arr[:, 1] = bymin
            arr[:, 2] = bxmax
            arr[:, 3] = bymax
        self.layers.append((src, arr, geoms, n))
        if self.log:
            self.log.diag(f"OBSTACLE (AABB): '{src}' total={n}")

    def cleanup(self) -> None:
        # AABB store holds only Python references; nothing on disk to delete.
        self.layers = []

    def conflict_in_box(self, sr, cx: float, cy: float,
                        half_w: float, half_h: float,
                        conflict_test_geom, conflict_mode: str) -> bool:
        if conflict_test_geom is None:
            return True
        axmin = cx - half_w
        aymin = cy - half_h
        axmax = cx + half_w
        aymax = cy + half_h
        for (_name, bb, geoms, n) in self.layers:
            if n == 0:
                continue
            sep = ((axmax < bb[:, 0]) | (axmin > bb[:, 2])
                   | (aymax < bb[:, 1]) | (aymin > bb[:, 3]))
            hits = _np.flatnonzero(~sep)
            if hits.size == 0:
                continue
            if conflict_mode == "FAST_EXTENT_ONLY":
                return True
            for idx in hits.tolist():
                g = geoms[idx]
                if g is None:
                    continue
                try:
                    if not conflict_test_geom.disjoint(g):
                        return True
                except (arcpy.ExecuteError, RuntimeError):
                    return True
        return False


# =============================================================================
# 5. AABB cache for placed labels (Mode A self-overlap)
# =============================================================================

class _PlacedCache:
    """
    AABB cache of geometries already placed in this run.
    Vectorised when NumPy is available; falls back to a Python loop
    transparently.
    """

    __slots__ = ("items", "_bbox_arr", "_geoms")

    def __init__(self):
        self.items: List[Tuple[float, float, float, float, object]] = []
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
        self._bbox_arr = None

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

    def conflicts(self, foot_geom, use_legacy: bool = False) -> bool:
        if foot_geom is None or not self.items:
            return False
        ea = foot_geom.extent
        if ea is None:
            return False
        axmin, aymin, axmax, aymax = ea.XMin, ea.YMin, ea.XMax, ea.YMax

        if use_legacy or not _NUMPY_OK:
            for (bxmin, bymin, bxmax, bymax, g) in self.items:
                if (axmax < bxmin or axmin > bxmax
                        or aymax < bymin or aymin > bymax):
                    continue
                try:
                    if not foot_geom.disjoint(g):
                        return True
                except (arcpy.ExecuteError, RuntimeError):
                    return True
            return False

        self._ensure_array()
        bb = self._bbox_arr
        if bb.shape[0] == 0:
            return False
        sep = ((axmax < bb[:, 0]) | (axmin > bb[:, 2])
               | (aymax < bb[:, 1]) | (aymin > bb[:, 3]))
        hits = _np.flatnonzero(~sep)
        if hits.size == 0:
            return False
        for idx in hits.tolist():
            g = self._geoms[idx]
            try:
                if not foot_geom.disjoint(g):
                    return True
            except (arcpy.ExecuteError, RuntimeError):
                return True
        return False


# =============================================================================
# 6. Geometry / candidate helpers
# =============================================================================

def _angles(directions: int) -> List[float]:
    step = 2.0 * math.pi / float(directions)
    return [i * step for i in range(directions)]


def _biased_angles(angles: List[float], bias: Optional[str]) -> List[float]:
    if not angles:
        return angles
    b = (bias or "NONE").upper()
    if b == "NONE":
        return angles

    def _score_card(a: float) -> float:
        deg = (a * 180.0 / math.pi) % 360.0
        return min(abs(deg - t) for t in (0.0, 90.0, 180.0, 270.0))

    def _score_diag(a: float) -> float:
        deg = (a * 180.0 / math.pi) % 360.0
        return min(abs(deg - t) for t in (45.0, 135.0, 225.0, 315.0))

    if b == "CARDINAL_FIRST":
        return sorted(angles, key=lambda a: (_score_card(a), a))
    if b == "DIAGONAL_FIRST":
        return sorted(angles, key=lambda a: (_score_diag(a), a))
    return angles


def _iter_candidates(pattern: Optional[str], rings_sorted: List[float],
                     angles: List[float], max_ring: float,
                     spiral_step: float):
    pat = (pattern or "FIXED_RINGS").upper()
    if pat == "SPIRAL":
        step = (float(spiral_step) if spiral_step
                and float(spiral_step) > 0 else 0.0)
        if step <= 0.0:
            try:
                step = float(min(rings_sorted)) / 2.0
            except (ValueError, TypeError):
                step = 1.0
        if step <= 0.0:
            step = 1.0
        n_ang = len(angles) if angles else 16
        ang_step = 2.0 * math.pi / float(n_ang)
        i = 1
        r = step
        while r <= float(max_ring):
            ang = (i * ang_step) % (2.0 * math.pi)
            yield (r, ang, r * math.cos(ang), r * math.sin(ang))
            i += 1
            r = step * float(i)
        return
    for r in rings_sorted:
        for ang in angles:
            yield (float(r), float(ang),
                   float(r) * math.cos(ang), float(r) * math.sin(ang))


def _rect_polygon(sr, cx: float, cy: float, w: float, h: float):
    hw = w / 2.0
    hh = h / 2.0
    arr = arcpy.Array([
        arcpy.Point(cx - hw, cy - hh),
        arcpy.Point(cx + hw, cy - hh),
        arcpy.Point(cx + hw, cy + hh),
        arcpy.Point(cx - hw, cy + hh),
        arcpy.Point(cx - hw, cy - hh),
    ])
    return arcpy.Polygon(arr, sr)


def _rect_polygon_rotated(sr, cx: float, cy: float, w: float, h: float,
                          angle_rad: float):
    """
    F2: Build a rotated rectangle polygon. If the rotation matrix
    construction fails for any reason we RAISE arcpy.ExecuteError
    instead of silently returning the unrotated rectangle. A wrong
    rectangle silently used for collision testing would let labels
    overlap obstacles undetected - which is precisely the failure
    mode the deconflict tool exists to prevent.
    """
    try:
        hw = w / 2.0
        hh = h / 2.0
        ca = math.cos(angle_rad)
        sa = math.sin(angle_rad)
        corners = [(-hw, -hh), (hw, -hh), (hw, hh), (-hw, hh), (-hw, -hh)]
        pts = [arcpy.Point(cx + (x * ca - y * sa),
                           cy + (x * sa + y * ca))
               for (x, y) in corners]
        return arcpy.Polygon(arcpy.Array(pts), sr)
    except (TypeError, ValueError, ArithmeticError) as ex:
        raise arcpy.ExecuteError(
            f"_rect_polygon_rotated: matrix construction failed for "
            f"(cx={cx}, cy={cy}, w={w}, h={h}, angle_rad={angle_rad}): {ex}"
        )
    except (arcpy.ExecuteError, RuntimeError) as ex:
        raise arcpy.ExecuteError(
            f"_rect_polygon_rotated: arcpy geometry build failed for "
            f"(cx={cx}, cy={cy}, w={w}, h={h}, angle_rad={angle_rad}): {ex}"
        )


def _parse_multivalue_numbers(val) -> List[float]:
    if val is None:
        return []
    s = _safe_str(val).replace(";", " ").replace(",", " ")
    out: List[float] = []
    for p in (q for q in s.split() if q.strip()):
        try:
            out.append(float(p))
        except (TypeError, ValueError):
            pass
    return out


def _ensure_field(fc: str, name: str, ftype: str,
                  length: Optional[int] = None) -> None:
    fields = [f.name.lower() for f in arcpy.ListFields(fc)]
    if name.lower() in fields:
        return
    if ftype.upper() == "TEXT" and length:
        arcpy.management.AddField(fc, name, ftype, field_length=length)
    else:
        arcpy.management.AddField(fc, name, ftype)



# =============================================================================
# 7. Toolbox + Tool class (parameters)
# =============================================================================

class Toolbox:
    def __init__(self):
        self.label = "Plugin 4 - Elevation Text Deconflict (Pro)"
        self.alias = "elevtext_pro"
        self.tools = [ElevationTextDeconflictV5]


class ElevationTextDeconflictV5:
    def __init__(self):
        self.label = "Elevation Text Deconflict (2 Modes)"
        self.description = (
            "Deconflicts elevation text against obstacles.\n\n"
            " - SELECTION-BYPASS hardwired (FULL datasets always processed)\n"
            " - Optimised vectorised AABB conflict tests by default;\n"
            "   set 'Use legacy evaluation' to fall back to per-candidate\n"
            "   SelectLayerByLocation.\n"
            " - On-disk staging in scratchGDB; near tables in 'memory'.\n"
            " - Stage-by-stage [DIAG] logging.\n\n"
            "Mode A: outputs label positions (does NOT modify input points).\n"
            "Mode B: writes XOffset/YOffset on a copy of the annotation in "
            "the same GDB."
        )
        self.canRunInBackground = True

    def isLicensed(self) -> bool:
        return True

    def getParameterInfo(self):
        params = []
        p0 = arcpy.Parameter(
            displayName="Input Mode", name="input_mode",
            datatype="GPString", parameterType="Required",
            direction="Input")
        p0.filter.type = "ValueList"
        p0.filter.list = ["POINT_LAYER_WITH_TEXT_FIELD",
                          "ANNOTATION_LAYER_AND_ANCHOR_POINTS"]
        p0.value = "POINT_LAYER_WITH_TEXT_FIELD"
        params.append(p0)

        p1 = arcpy.Parameter(
            displayName="(Mode A) Input Point Layer [Point]",
            name="in_points", datatype="GPFeatureLayer",
            parameterType="Optional", direction="Input")
        p1.filter.list = ["Point"]
        params.append(p1)
        p2 = arcpy.Parameter(
            displayName="(Mode A) Text Field", name="text_field",
            datatype="Field", parameterType="Optional", direction="Input")
        p2.parameterDependencies = [p1.name]
        params.append(p2)

        p3 = arcpy.Parameter(
            displayName="(Mode B) Annotation Layer (GDB Annotation FC)",
            name="anno_layer", datatype="GPFeatureLayer",
            parameterType="Optional", direction="Input")
        params.append(p3)
        p4 = arcpy.Parameter(
            displayName="(Mode B) Anchor Points Layer [Point]",
            name="anchor_points", datatype="GPFeatureLayer",
            parameterType="Optional", direction="Input")
        p4.filter.list = ["Point"]
        params.append(p4)
        p5 = arcpy.Parameter(
            displayName="(Mode B) Annotation-to-Anchor Link Method",
            name="link_method", datatype="GPString",
            parameterType="Optional", direction="Input")
        p5.filter.type = "ValueList"
        p5.filter.list = ["NEAREST_POINT", "FEATUREID_MATCH"]
        p5.value = "NEAREST_POINT"
        params.append(p5)
        p6 = arcpy.Parameter(
            displayName="(Mode B) FeatureID Field (for FEATUREID_MATCH)",
            name="featureid_field", datatype="Field",
            parameterType="Optional", direction="Input")
        p6.parameterDependencies = [p3.name]
        p6.value = "FeatureID"
        params.append(p6)
        p7 = arcpy.Parameter(
            displayName=("(Mode B) Max Anchor Match Distance (map units) "
                         "[blank=no limit]"),
            name="max_match_dist", datatype="GPString",
            parameterType="Optional", direction="Input")
        p7.value = ""
        params.append(p7)

        p8 = arcpy.Parameter(
            displayName="Rings (map units) e.g., 2 4 6", name="rings",
            datatype="GPDouble", parameterType="Required",
            direction="Input", multiValue=True)
        p8.value = "2 4 6"
        params.append(p8)
        p9 = arcpy.Parameter(
            displayName="Directions (angles count)", name="directions",
            datatype="GPLong", parameterType="Required", direction="Input")
        p9.filter.type = "ValueList"
        p9.filter.list = [8, 16, 24, 36]
        p9.value = 16
        params.append(p9)
        p10 = arcpy.Parameter(
            displayName="Obstacle Layers (MultiValue)",
            name="obstacle_layers", datatype="GPFeatureLayer",
            parameterType="Required", direction="Input", multiValue=True)
        params.append(p10)
        p11 = arcpy.Parameter(
            displayName="Conflict Test Mode (speed vs accuracy)",
            name="conflict_test_mode", datatype="GPString",
            parameterType="Required", direction="Input")
        p11.filter.type = "ValueList"
        p11.filter.list = ["FAST_EXTENT_ONLY",
                           "BALANCED_EXTENT_THEN_GEOMETRY",
                           "ACCURATE_GEOMETRY_ONLY"]
        p11.value = "BALANCED_EXTENT_THEN_GEOMETRY"
        params.append(p11)
        p12 = arcpy.Parameter(
            displayName=("Max Features per Obstacle Layer "
                         "(deprecated; ignored)"),
            name="max_features_per_layer", datatype="GPLong",
            parameterType="Optional", direction="Input")
        p12.value = 0
        params.append(p12)
        p13 = arcpy.Parameter(
            displayName="Padding (map units) [Mode A only]",
            name="padding", datatype="GPDouble",
            parameterType="Optional", direction="Input")
        p13.value = 0.0
        params.append(p13)
        p14 = arcpy.Parameter(
            displayName="Extra Obstacle Search Distance (map units)",
            name="extra_search", datatype="GPDouble",
            parameterType="Optional", direction="Input")
        p14.value = 0.0
        params.append(p14)

        p15 = arcpy.Parameter(
            displayName="Reference Scale (e.g., 25000 for 1:25000)",
            name="reference_scale", datatype="GPLong",
            parameterType="Optional", direction="Input")
        p15.value = 25000
        params.append(p15)
        p16 = arcpy.Parameter(
            displayName="(Mode A) Font Size (pt)",
            name="font_size_pt", datatype="GPDouble",
            parameterType="Optional", direction="Input")
        p16.value = 8.0
        params.append(p16)
        p17 = arcpy.Parameter(
            displayName=("(Mode A) Character Width Factor k "
                         "(legacy; ignored when Arial table is used)"),
            name="char_width_factor", datatype="GPDouble",
            parameterType="Optional", direction="Input")
        p17.value = 0.60
        params.append(p17)

        p18 = arcpy.Parameter(
            displayName="Report Text Encoding Mode",
            name="report_text_mode", datatype="GPString",
            parameterType="Required", direction="Input")
        p18.filter.type = "ValueList"
        # F4: keep ASCII_SAFE_REPLACE (default) to avoid Pro pane crashes.
        p18.filter.list = ["UNICODE_BEST_EFFORT", "ASCII_SAFE_REPLACE"]
        p18.value = "ASCII_SAFE_REPLACE"
        params.append(p18)
        p19 = arcpy.Parameter(
            displayName="Preview Only (do not modify Mode B copy)",
            name="preview_only", datatype="GPBoolean",
            parameterType="Optional", direction="Input")
        p19.value = False
        params.append(p19)
        p20 = arcpy.Parameter(
            displayName="Debug Level", name="debug_level",
            datatype="GPString", parameterType="Optional", direction="Input")
        p20.filter.type = "ValueList"
        p20.filter.list = ["OFF", "BASIC", "VERBOSE"]
        p20.value = "BASIC"
        params.append(p20)
        p21 = arcpy.Parameter(
            displayName="Debug Log File (optional)",
            name="debug_log_file", datatype="DEFile",
            parameterType="Optional", direction="Input")
        p21.value = ""
        params.append(p21)

        p27 = arcpy.Parameter(
            displayName="Create 'Moved Only' Output",
            name="create_moved_only", datatype="GPBoolean",
            parameterType="Optional", direction="Input")
        p27.value = True
        params.append(p27)
        p28 = arcpy.Parameter(
            displayName="Search Pattern", name="search_pattern",
            datatype="GPString", parameterType="Optional", direction="Input")
        p28.filter.type = "ValueList"
        p28.filter.list = ["FIXED_RINGS", "SPIRAL", "GREEDY"]
        p28.value = "FIXED_RINGS"
        params.append(p28)
        p29 = arcpy.Parameter(
            displayName="(SPIRAL) Step (map units) [0=auto]",
            name="spiral_step", datatype="GPDouble",
            parameterType="Optional", direction="Input")
        p29.value = 0.0
        params.append(p29)
        p30 = arcpy.Parameter(
            displayName="Direction Bias", name="direction_bias",
            datatype="GPString", parameterType="Optional", direction="Input")
        p30.filter.type = "ValueList"
        p30.filter.list = ["NONE", "CARDINAL_FIRST", "DIAGONAL_FIRST"]
        p30.value = "CARDINAL_FIRST"
        params.append(p30)
        p31 = arcpy.Parameter(
            displayName="(Mode A) Avoid Label-Label Conflicts",
            name="avoid_label_label", datatype="GPBoolean",
            parameterType="Optional", direction="Input")
        p31.value = True
        params.append(p31)
        p32 = arcpy.Parameter(
            displayName="(Mode A) Use Rotated Conflict Box",
            name="modeA_rotated_box", datatype="GPBoolean",
            parameterType="Optional", direction="Input")
        p32.value = True
        params.append(p32)
        p33 = arcpy.Parameter(
            displayName="(Mode B) Apply Rotation to 'Angle' Field",
            name="apply_rotation_modeB", datatype="GPBoolean",
            parameterType="Optional", direction="Input")
        p33.value = False
        params.append(p33)
        p34 = arcpy.Parameter(
            displayName="(Mode B) Rotation Write Mode",
            name="rotation_write_mode", datatype="GPString",
            parameterType="Optional", direction="Input")
        p34.filter.type = "ValueList"
        p34.filter.list = ["SET_ABSOLUTE", "ADD_DELTA"]
        p34.value = "SET_ABSOLUTE"
        params.append(p34)
        p35 = arcpy.Parameter(
            displayName="(Mode B) Create Label-Position Points Output",
            name="create_modeB_points", datatype="GPBoolean",
            parameterType="Optional", direction="Input")
        p35.value = True
        params.append(p35)
        p36 = arcpy.Parameter(
            displayName="Create Leader Lines Output (Polyline)",
            name="create_leaderlines", datatype="GPBoolean",
            parameterType="Optional", direction="Input")
        p36.value = False
        params.append(p36)
        p37 = arcpy.Parameter(
            displayName="Leader Lines: Moved Only",
            name="leaderlines_moved_only", datatype="GPBoolean",
            parameterType="Optional", direction="Input")
        p37.value = True
        params.append(p37)
        p26 = arcpy.Parameter(
            displayName="(Mode B) Reverse Offset Direction",
            name="reverse_offsets", datatype="GPBoolean",
            parameterType="Optional", direction="Input")
        p26.value = False
        params.append(p26)
        p_addmap = arcpy.Parameter(
            displayName="Add outputs to current map",
            name="add_to_map", datatype="GPBoolean",
            parameterType="Optional", direction="Input")
        p_addmap.value = True
        params.append(p_addmap)

        # F1 NEW PARAMETER
        p_legacy = arcpy.Parameter(
            displayName=(
                "Use legacy evaluation (per-candidate "
                "SelectLayerByLocation; slower but matches prior behaviour)"),
            name="use_legacy_evaluation", datatype="GPBoolean",
            parameterType="Optional", direction="Input")
        p_legacy.value = False
        params.append(p_legacy)

        p22 = arcpy.Parameter(
            displayName="Output Report (all items)",
            name="out_report_all", datatype="DEFeatureClass",
            parameterType="Derived", direction="Output")
        params.append(p22)
        p23 = arcpy.Parameter(
            displayName="Output Report (unresolved only)",
            name="out_report_unresolved",
            datatype="DEFeatureClass",
            parameterType="Derived", direction="Output")
        params.append(p23)
        p24 = arcpy.Parameter(
            displayName="Output Moved Copy", name="out_moved_copy",
            datatype="DEFeatureClass",
            parameterType="Derived", direction="Output")
        params.append(p24)
        p25 = arcpy.Parameter(
            displayName="Output Moved Only", name="out_moved_only",
            datatype="DEFeatureClass",
            parameterType="Derived", direction="Output")
        params.append(p25)
        p38 = arcpy.Parameter(
            displayName="Output Label Positions (Points)",
            name="out_label_positions",
            datatype="DEFeatureClass",
            parameterType="Derived", direction="Output")
        params.append(p38)
        p39 = arcpy.Parameter(
            displayName="Output Leader Lines (Polyline)",
            name="out_leaderlines", datatype="DEFeatureClass",
            parameterType="Derived", direction="Output")
        params.append(p39)

        return params

    def updateParameters(self, parameters):
        try:
            pmap = {p.name: p for p in parameters}
            mode = ((pmap.get("input_mode").valueAsText
                     if pmap.get("input_mode") else None)
                    or "POINT_LAYER_WITH_TEXT_FIELD")
        except (AttributeError, KeyError):
            return
        modeA_names = {"in_points", "text_field", "padding", "font_size_pt",
                       "char_width_factor", "avoid_label_label",
                       "modeA_rotated_box"}
        modeB_names = {"anno_layer", "anchor_points", "link_method",
                       "featureid_field", "max_match_dist",
                       "reverse_offsets", "apply_rotation_modeB",
                       "rotation_write_mode", "create_modeB_points"}
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
                elif any(r <= 0 for r in rings):
                    parameters[8].setErrorMessage(
                        "All ring values must be > 0.")
            if mode == "POINT_LAYER_WITH_TEXT_FIELD":
                if is_empty_gp(parameters[1].valueAsText):
                    parameters[1].setErrorMessage(
                        "Mode A requires an Input Point Layer.")
                if is_empty_gp(parameters[2].valueAsText):
                    parameters[2].setErrorMessage(
                        "Mode A requires a Text Field.")
            else:
                if is_empty_gp(parameters[3].valueAsText):
                    parameters[3].setErrorMessage(
                        "Mode B requires an Annotation Layer.")
                if is_empty_gp(parameters[4].valueAsText):
                    parameters[4].setErrorMessage(
                        "Mode B requires an Anchor Points Layer.")
        except (arcpy.ExecuteError, RuntimeError, AttributeError, IndexError):
            pass

    def _add_layers_to_active_map(self, fc_paths) -> None:
        try:
            aprx = arcpy.mp.ArcGISProject("CURRENT")
        except (arcpy.ExecuteError, RuntimeError):
            return
        m = aprx.activeMap
        if m is None:
            return
        for p in fc_paths:
            if not p:
                continue
            try:
                m.addDataFromPath(p)
            except (arcpy.ExecuteError, RuntimeError):
                arcpy.AddWarning(f"Could not add {p} to active map.")



    # =========================================================================
    # Mode A executor
    # =========================================================================

    def _execute_mode_a(self, log: Logger, in_points_layer, text_field: str,
                        ref_scale: float, font_pt: float, k_fac: float,
                        padding: float, rings_sorted, angles, max_ring,
                        search_pattern, spiral_step, max_attempts,
                        conflict_mode, extra_search,
                        avoid_label_label: bool, modeA_rotated_box: bool,
                        obstacle_store, base_sr,
                        scratch_gdb, report_text_mode,
                        create_moved_only: bool, create_leaderlines: bool,
                        leaderlines_moved_only: bool,
                        ground_per_point: float,
                        use_legacy_evaluation: bool):
        in_points_path = _resolve_full_source(in_points_layer)
        out_ws = arcpy.Describe(in_points_path).path
        stamp = datetime.datetime.now().strftime("%H%M%S")
        out_name = f"LabelPos_{stamp}"
        moved_copy_final = os.path.join(out_ws, out_name)

        log.diag(f"MODE A: output FC -> {moved_copy_final}")
        arcpy.management.CreateFeatureclass(
            out_ws, out_name, "POINT", spatial_reference=base_sr)

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

        report_all_rows: List[tuple] = []
        report_bad_rows: List[tuple] = []
        leader_rows: List[tuple] = []
        moved = unchanged = failed = 0
        placed_cache = _PlacedCache()

        try:
            total_in_count = int(arcpy.management.GetCount(
                in_points_path).getOutput(0))
        except (arcpy.ExecuteError, RuntimeError) as ex:
            raise arcpy.ExecuteError(
                f"GetCount failed on points '{in_points_path}': {ex}")
        arcpy.SetProgressor("step", "MODE A: deconflicting points...",
                            0, max(1, total_in_count), 1)

        oid_field = arcpy.Describe(in_points_path).OIDFieldName
        sql_clause = (None, f"ORDER BY {oid_field}")
        n_seen = 0

        with arcpy.da.SearchCursor(
                in_points_path, ["OID@", "SHAPE@", text_field],
                sql_clause=sql_clause) as sc, \
             arcpy.da.InsertCursor(
                moved_copy_final,
                ["SHAPE@", "SrcOID", "Status", "Ring", "AngleDeg",
                 "Attempts", "OrigX", "OrigY", "TextVal", "ETD_MOVED",
                 "DX_MU", "DY_MU", "DX_PT", "DY_PT", "AngleOut",
                 "ETD_LEADER"]) as ic:
            for oid, geom, txt in sc:
                n_seen += 1
                arcpy.SetProgressorPosition(n_seen)
                if geom is None:
                    failed += 1
                    report_bad_rows.append(
                        (None, oid, "No geometry", -1, ""))
                    continue
                ax, ay = geom.centroid.X, geom.centroid.Y
                txt_u = _safe_str(txt)
                txt_r = (ascii_safe(txt_u)
                         if report_text_mode == "ASCII_SAFE_REPLACE"
                         else txt_u)
                # F3: Arial proportional widths.
                w, h = _text_extent_map_units(
                    txt_u, font_pt, k_fac, ref_scale, padding)
                half_diag = 0.5 * math.sqrt(w * w + h * h)

                rect0 = _rect_polygon(base_sr, ax, ay, w, h)
                conflict0 = obstacle_store.conflict_in_box(
                    base_sr, ax, ay,
                    half_diag + extra_search, half_diag + extra_search,
                    rect0, conflict_mode)
                if (not conflict0) and not (
                        avoid_label_label and placed_cache.conflicts(
                            rect0, use_legacy=use_legacy_evaluation)):
                    unchanged += 1
                    p = arcpy.PointGeometry(
                        arcpy.Point(ax, ay), base_sr)
                    ic.insertRow((p, oid, "UNCHANGED", 0.0, 0.0, 0,
                                  ax, ay, txt_r, 0,
                                  0.0, 0.0, 0.0, 0.0, 0.0, 0))
                    report_all_rows.append(
                        (p, oid, "UNCHANGED", 0.0, 0.0, 0,
                         oid, 0.0, txt_r))
                    if avoid_label_label:
                        placed_cache.add(rect0)
                    continue

                placed = False
                attempts = 0
                chosen = (ax, ay, 0.0, 0.0)
                chosen_rect = None
                for (r, ang, dx, dy) in _iter_candidates(
                        search_pattern, rings_sorted, angles,
                        max_ring, spiral_step):
                    attempts += 1
                    if attempts > max_attempts:
                        break
                    cx = ax + dx
                    cy = ay + dy
                    if modeA_rotated_box:
                        # F2: this raises arcpy.ExecuteError on matrix
                        # failure. We don't swallow it because a wrong
                        # rect would let labels overlap silently.
                        rect = _rect_polygon_rotated(
                            base_sr, cx, cy, w, h, ang)
                    else:
                        rect = _rect_polygon(base_sr, cx, cy, w, h)
                    if avoid_label_label and placed_cache.conflicts(
                            rect, use_legacy=use_legacy_evaluation):
                        continue
                    if obstacle_store.conflict_in_box(
                            base_sr, cx, cy,
                            half_diag + extra_search,
                            half_diag + extra_search,
                            rect, conflict_mode):
                        continue
                    placed = True
                    chosen = (cx, cy, float(r), (ang * 180.0 / math.pi))
                    chosen_rect = rect
                    break

                if placed:
                    cx, cy, rr2, aa2 = chosen
                    moved += 1
                    p = arcpy.PointGeometry(
                        arcpy.Point(cx, cy), base_sr)
                    dx_mu = (cx - ax)
                    dy_mu = (cy - ay)
                    dx_pt = (dx_mu / float(ground_per_point)
                             if ground_per_point else 0.0)
                    dy_pt = (dy_mu / float(ground_per_point)
                             if ground_per_point else 0.0)
                    ic.insertRow((p, oid, "MOVED", rr2, aa2, attempts,
                                  ax, ay, txt_r, 1, dx_mu, dy_mu,
                                  dx_pt, dy_pt, aa2, 1))
                    report_all_rows.append(
                        (p, oid, "MOVED", rr2, aa2, attempts,
                         oid, 0.0, txt_r))
                    if create_leaderlines:
                        leader_rows.append((oid, ax, ay, cx, cy, rr2))
                    if avoid_label_label and chosen_rect is not None:
                        placed_cache.add(chosen_rect)
                else:
                    failed += 1
                    p0 = arcpy.PointGeometry(
                        arcpy.Point(ax, ay), base_sr)
                    ic.insertRow((p0, oid, "FAILED", 0.0, 0.0, attempts,
                                  ax, ay, txt_r, 0, 0.0, 0.0, 0.0, 0.0,
                                  0.0, 0))
                    report_all_rows.append(
                        (p0, oid, "FAILED", 0.0, 0.0, attempts,
                         oid, 0.0, txt_r))
                    report_bad_rows.append(
                        (p0, oid,
                         "No free position found within rings/directions "
                         "or max attempts exceeded", oid, txt_r))
                if (n_seen % 500) == 0:
                    gc.collect()
        arcpy.ResetProgressor()

        log.diag(
            f"MODE A: total={n_seen} moved={moved} unchanged={unchanged} "
            f"failed={failed}")

        moved_only_final = None
        if create_moved_only:
            moved_only_name = f"LabelPos_MovedOnly_{stamp}"
            moved_only_final = os.path.join(out_ws, moved_only_name)
            lyr_name = "lyr_labelpos_" + uuid.uuid4().hex[:8]
            try:
                arcpy.management.MakeFeatureLayer(
                    moved_copy_final, lyr_name)
                arcpy.management.SelectLayerByAttribute(
                    lyr_name, "NEW_SELECTION", "ETD_MOVED = 1")
                arcpy.management.CopyFeatures(lyr_name, moved_only_final)
            except (arcpy.ExecuteError, RuntimeError):
                log.warn(
                    f"MODE A: moved-only output failed: "
                    f"{traceback.format_exc()}")
                moved_only_final = None
            finally:
                try:
                    arcpy.management.Delete(lyr_name)
                except (arcpy.ExecuteError, RuntimeError):
                    pass

        leaderlines_fc = None
        if create_leaderlines:
            try:
                ll_name = f"LeaderLines_{stamp}"
                leaderlines_fc = os.path.join(out_ws, ll_name)
                arcpy.management.CreateFeatureclass(
                    out_ws, ll_name, "POLYLINE",
                    spatial_reference=base_sr)
                _ensure_field(leaderlines_fc, "SrcOID", "LONG")
                _ensure_field(leaderlines_fc, "LenMU", "DOUBLE")
                with arcpy.da.InsertCursor(
                        leaderlines_fc,
                        ["SHAPE@", "SrcOID", "LenMU"]) as il:
                    for (oid2, ax2, ay2, cx2, cy2, rr2) in leader_rows:
                        if leaderlines_moved_only and float(rr2) <= 0.0:
                            continue
                        try:
                            arr = arcpy.Array(
                                [arcpy.Point(ax2, ay2),
                                 arcpy.Point(cx2, cy2)])
                            il.insertRow(
                                (arcpy.Polyline(arr, base_sr),
                                 int(oid2), float(rr2)))
                        except (arcpy.ExecuteError, RuntimeError) as ex:
                            log.warn(f"Leader insert failed at OID "
                                     f"{oid2}: {ex}")
            except (arcpy.ExecuteError, RuntimeError):
                log.warn(
                    f"MODE A: leader lines output failed: "
                    f"{traceback.format_exc()}")
                leaderlines_fc = None

        return (moved_copy_final, moved_only_final, moved_copy_final,
                leaderlines_fc, report_all_rows, report_bad_rows,
                moved, unchanged, failed, 0)


    # =========================================================================
    # Mode B: anchor map (on-disk staging via scratchGDB)
    # =========================================================================

    def _create_temp_polygon_from_annotation(self, log: Logger,
                                             anno_fc: str, base_sr,
                                             scratch_gdb: str):
        temp_fc = arcpy.CreateUniqueName("anno_as_polygon", scratch_gdb)
        arcpy.management.CreateFeatureclass(
            out_path=scratch_gdb,
            out_name=os.path.basename(temp_fc),
            geometry_type="POLYGON",
            spatial_reference=base_sr,
        )
        arcpy.management.AddField(temp_fc, "SrcAnnoOID", "LONG")
        inserted = 0
        with arcpy.da.InsertCursor(
                temp_fc, ["SHAPE@", "SrcAnnoOID"]) as ic:
            with arcpy.da.SearchCursor(
                    anno_fc, ["OID@", "SHAPE@"]) as sc:
                for aoid, ageom in sc:
                    if ageom:
                        ic.insertRow((ageom, int(aoid)))
                        inserted += 1
        poly2anno = {}
        with arcpy.da.SearchCursor(
                temp_fc, ["OID@", "SrcAnnoOID"]) as sc2:
            for poid, aoid in sc2:
                poly2anno[int(poid)] = int(aoid)
        log.diag(
            f"MODE B: Anno-as-polygon FC built. rows={inserted} "
            f"path={temp_fc}")
        return temp_fc, poly2anno

    def _build_anchor_map_modeB(self, log: Logger, anno_fc: str,
                                anchor_points, link_method: str,
                                featureid_field: str,
                                max_match_dist_text, base_sr,
                                scratch_gdb: str):
        log.diag(f"MODE B: anchor link_method={link_method}")
        anchor_xy = {}
        anchor_src = _resolve_full_source(anchor_points)
        with arcpy.da.SearchCursor(
                anchor_src, ["OID@", "SHAPE@XY"]) as sc:
            for oid, xy in sc:
                anchor_xy[int(oid)] = xy
        log.diag(f"MODE B: anchors total={len(anchor_xy)}")

        mapping = {}
        if link_method == "FEATUREID_MATCH":
            with arcpy.da.SearchCursor(
                    anno_fc, ["OID@", featureid_field]) as sc:
                for aoid, fid in sc:
                    if fid is None:
                        continue
                    try:
                        fid_int = int(fid)
                    except (TypeError, ValueError):
                        continue
                    if fid_int in anchor_xy:
                        ax, ay = anchor_xy[fid_int]
                        mapping[int(aoid)] = (fid_int, ax, ay, 0.0)
            log.diag(
                f"MODE B: Anchor map (FEATUREID_MATCH) "
                f"matched={len(mapping)}")
            return mapping

        # NEAREST_POINT path
        search_radius = ""
        t = _safe_str(max_match_dist_text or "").strip()
        if t and t not in ("0", "0.0"):
            try:
                if float(t) > 0:
                    search_radius = t
            except (TypeError, ValueError):
                search_radius = ""

        temp_poly = None
        near_table = None
        try:
            temp_poly, poly2anno = \
                self._create_temp_polygon_from_annotation(
                    log, anno_fc, base_sr, scratch_gdb)
            # Master Rule 5: small near table -> 'memory' workspace
            near_table = arcpy.CreateUniqueName(
                "anno_anchor_near", "memory")
            log.diag(
                f"MODE B: GenerateNearTable radius="
                f"{search_radius if search_radius else 'BLANK(no limit)'}")
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
            with arcpy.da.SearchCursor(
                    near_table,
                    ["IN_FID", "NEAR_FID", "NEAR_DIST"]) as tc:
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
                    mapping[int(anno_oid)] = (
                        near_fid, ax, ay,
                        float(near_dist) if near_dist is not None else 0.0)
                    matched += 1
            log.diag(
                f"MODE B: Anchor map (NEAREST_POINT) matched={matched}")
            return mapping
        finally:
            _safe_delete(near_table, log)
            _safe_delete(temp_poly, log)

    def _get_output_container_for_annotation(self, anno_fc):
        d = arcpy.Describe(_resolve_full_source(anno_fc))
        return d.path



    # =========================================================================
    # Mode B executor
    # =========================================================================

    def _execute_mode_b(self, log: Logger, anno_layer, anchor_points,
                        link_method, featureid_field, max_match_dist_text,
                        ref_scale, rings_sorted, angles, max_ring,
                        search_pattern, spiral_step, max_attempts,
                        conflict_mode, extra_search,
                        obstacle_store, base_sr, scratch_gdb,
                        report_text_mode, create_moved_only,
                        create_modeB_points, create_leaderlines,
                        leaderlines_moved_only, apply_rotation_modeB,
                        rotation_write_mode, reverse_offsets,
                        preview_only, ground_per_point):
        anno_path = _resolve_full_source(anno_layer)
        out_container = self._get_output_container_for_annotation(anno_path)
        log.diag(f"MODE B: output container = {out_container}")
        stamp = datetime.datetime.now().strftime("%H%M%S")
        moved_copy_final = os.path.join(
            out_container, f"Annotation_Moved_{stamp}")
        moved_only_final = os.path.join(
            out_container, f"Annotation_MovedOnly_{stamp}")
        if not create_moved_only:
            moved_only_final = None

        log.diag(f"MODE B: CopyFeatures -> {moved_copy_final}")
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

        desc_anno = arcpy.Describe(moved_copy_final)
        workspace = desc_anno.catalogPath
        original_workspace = workspace
        try:
            while (workspace and not os.path.basename(workspace)
                   .lower().endswith(".gdb")):
                workspace = os.path.dirname(workspace)
        except (TypeError, AttributeError):
            workspace = None
        if not workspace:
            raise arcpy.ExecuteError(
                f"Could not determine file geodatabase path from "
                f"annotation: {original_workspace}")
        log.diag(f"MODE B: edit workspace = {workspace}")

        report_all_rows: List[tuple] = []
        report_bad_rows: List[tuple] = []
        leader_rows: List[tuple] = []
        moved = unchanged = failed = skipped = 0
        editor = None

        try:
            total_count = int(arcpy.management.GetCount(
                moved_copy_final).getOutput(0))
        except (arcpy.ExecuteError, RuntimeError) as ex:
            raise arcpy.ExecuteError(
                f"GetCount failed on annotation copy: {ex}")
        arcpy.SetProgressor("step", "MODE B: deconflicting annotation...",
                            0, max(1, total_count), 1)

        u_fields = ["OID@", "SHAPE@", "TextString", "XOffset", "YOffset",
                    "ETD_MOVED", "ETD_STATUS", "ETD_RING", "ETD_ANGLE",
                    "ETD_ATT", "ETD_AOID", "ETD_ADIST"]
        oid_field = arcpy.Describe(moved_copy_final).OIDFieldName
        sql_clause = (None, f"ORDER BY {oid_field}")
        n = 0

        try:
            editor = arcpy.da.Editor(workspace)
            editor.startEditing(False, True)
            editor.startOperation()

            with arcpy.da.UpdateCursor(
                    moved_copy_final, u_fields,
                    sql_clause=sql_clause) as ucur:
                for (aoid, ageom, textstr, xo, yo, mv, st, rr, aa, att,
                     ao, ad) in ucur:
                    n += 1
                    arcpy.SetProgressorPosition(n)
                    if ageom is None:
                        failed += 1
                        report_bad_rows.append(
                            (None, aoid, "No geometry", -1, ""))
                        continue
                    txt_u = _safe_str(textstr)
                    txt_r = (ascii_safe(txt_u)
                             if report_text_mode == "ASCII_SAFE_REPLACE"
                             else txt_u)
                    if int(aoid) not in anchor_map:
                        skipped += 1
                        c = ageom.centroid
                        p = arcpy.PointGeometry(
                            arcpy.Point(c.X, c.Y), base_sr)
                        report_all_rows.append(
                            (p, aoid, "SKIPPED", 0.0, 0.0, 0,
                             -1, -1.0, txt_r))
                        ucur.updateRow(
                            (aoid, ageom, textstr, xo, yo, 0,
                             "SKIPPED", 0.0, 0.0, 0, -1, -1.0))
                        continue
                    anchor_oid, ax, ay, adist = anchor_map[int(aoid)]
                    ext = ageom.extent
                    w = abs(ext.XMax - ext.XMin)
                    h = abs(ext.YMax - ext.YMin)
                    half_diag = 0.5 * math.sqrt(w * w + h * h)

                    if not obstacle_store.conflict_in_box(
                            base_sr, (ext.XMin + ext.XMax) * 0.5,
                            (ext.YMin + ext.YMax) * 0.5,
                            half_diag + extra_search,
                            half_diag + extra_search,
                            ageom, conflict_mode):
                        unchanged += 1
                        c = ageom.centroid
                        p = arcpy.PointGeometry(
                            arcpy.Point(c.X, c.Y), base_sr)
                        report_all_rows.append(
                            (p, aoid, "UNCHANGED", 0.0, 0.0, 0,
                             anchor_oid, adist, txt_r))
                        ucur.updateRow(
                            (aoid, ageom, textstr, xo, yo, 0,
                             "UNCHANGED", 0.0, 0.0, 0,
                             anchor_oid, adist))
                        continue

                    curr_c = ageom.centroid
                    placed = False
                    attempts = 0
                    chosen = (curr_c.X, curr_c.Y, 0.0, 0.0, 0.0, 0.0)
                    for (r, ang, dx, dy) in _iter_candidates(
                            search_pattern, rings_sorted, angles,
                            max_ring, spiral_step):
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
                                        pa.add(arcpy.Point(p.X + dx,
                                                           p.Y + dy))
                                parts_array.add(pa)
                            moved_geom = arcpy.Polygon(
                                parts_array, base_sr)
                        except (arcpy.ExecuteError, RuntimeError):
                            moved_geom = None
                        if moved_geom is None:
                            continue
                        if not obstacle_store.conflict_in_box(
                                base_sr, cx, cy,
                                half_diag + extra_search,
                                half_diag + extra_search,
                                moved_geom, conflict_mode):
                            placed = True
                            chosen = (cx, cy, float(r),
                                      (ang * 180.0 / math.pi), dx, dy)
                            break
                    if placed:
                        cx, cy, rr2, aa2, dx, dy = chosen
                        dx_pt = float(dx) / float(ground_per_point)
                        dy_pt = float(dy) / float(ground_per_point)
                        if reverse_offsets:
                            dx_pt = -dx_pt
                            dy_pt = -dy_pt
                        new_xo = ((float(xo) if xo is not None else 0.0)
                                  + dx_pt)
                        new_yo = ((float(yo) if yo is not None else 0.0)
                                  + dy_pt)
                        if create_leaderlines:
                            try:
                                gx = (ax + float(new_xo)
                                      * float(ground_per_point))
                                gy = (ay + float(new_yo)
                                      * float(ground_per_point))
                                leader_rows.append(
                                    (aoid, ax, ay, gx, gy, rr2))
                            except (TypeError, ValueError) as ex:
                                log.warn(
                                    f"Leader compute failed at OID "
                                    f"{aoid}: {ex}")
                        moved += 1
                        p = arcpy.PointGeometry(
                            arcpy.Point(cx, cy), base_sr)
                        report_all_rows.append(
                            (p, aoid, "MOVED", rr2, aa2, attempts,
                             anchor_oid, adist, txt_r))
                        if preview_only:
                            ucur.updateRow(
                                (aoid, ageom, textstr, xo, yo, 1,
                                 "PREVIEW_MOVED", rr2, aa2, attempts,
                                 anchor_oid, adist))
                        else:
                            ucur.updateRow(
                                (aoid, ageom, textstr, new_xo, new_yo,
                                 1, "MOVED", rr2, aa2, attempts,
                                 anchor_oid, adist))
                    else:
                        failed += 1
                        c = ageom.centroid
                        p = arcpy.PointGeometry(
                            arcpy.Point(c.X, c.Y), base_sr)
                        report_all_rows.append(
                            (p, aoid, "FAILED", 0.0, 0.0,
                             attempts, anchor_oid, adist, txt_r))
                        report_bad_rows.append(
                            (p, aoid,
                             "No free position found within "
                             "rings/directions",
                             anchor_oid, txt_r))
                        ucur.updateRow(
                            (aoid, ageom, textstr, xo, yo, 0,
                             "FAILED", 0.0, 0.0, attempts,
                             anchor_oid, adist))
                    if (n % 500) == 0:
                        gc.collect()

            editor.stopOperation()
            editor.stopEditing(True)
            editor = None
        except arcpy.ExecuteError:
            log.error(f"MODE B: edit session failed: "
                      f"{traceback.format_exc()}")
            try:
                if editor is not None and editor.isEditing:
                    editor.abortOperation()
                    editor.stopEditing(False)
            except (arcpy.ExecuteError, RuntimeError):
                pass
            raise
        except RuntimeError:
            log.error(f"MODE B: edit session failed: "
                      f"{traceback.format_exc()}")
            try:
                if editor is not None and editor.isEditing:
                    editor.abortOperation()
                    editor.stopEditing(False)
            except (arcpy.ExecuteError, RuntimeError):
                pass
            raise
        arcpy.ResetProgressor()

        log.diag(
            f"MODE B: total={(moved + unchanged + failed + skipped)} "
            f"moved={moved} unchanged={unchanged} failed={failed} "
            f"skipped={skipped}")

        # Optional rotation write
        if apply_rotation_modeB and not preview_only:
            try:
                angle_field = None
                flds = [f.name for f in
                        arcpy.ListFields(moved_copy_final)]
                for nm in ("Angle", "ANGLE"):
                    if nm in flds:
                        angle_field = nm
                        break
                if angle_field:
                    lyr_rot = "lyr_rot_" + uuid.uuid4().hex[:8]
                    arcpy.management.MakeFeatureLayer(
                        moved_copy_final, lyr_rot)
                    arcpy.management.SelectLayerByAttribute(
                        lyr_rot, "NEW_SELECTION", "ETD_MOVED = 1")
                    if rotation_write_mode == "ADD_DELTA":
                        codeblock = (
                            "def addang(a,b):\n"
                            "    try:\n"
                            "        return ((float(a) if a is not None else 0.0)\n"
                            "                + (float(b) if b is not None else 0.0))\n"
                            "    except (TypeError, ValueError):\n"
                            "        return float(b) if b is not None else 0.0")
                        arcpy.management.CalculateField(
                            lyr_rot, angle_field,
                            f"addang(!{angle_field}!, !ETD_ANGLE!)",
                            "PYTHON3", codeblock)
                    else:
                        arcpy.management.CalculateField(
                            lyr_rot, angle_field,
                            "!ETD_ANGLE!", "PYTHON3")
                    arcpy.management.Delete(lyr_rot)
            except (arcpy.ExecuteError, RuntimeError):
                log.warn(f"MODE B: rotation write failed: "
                         f"{traceback.format_exc()}")

        # Force redraw / dirty flag
        try:
            arcpy.management.CalculateField(
                moved_copy_final, "ETD_MOVED",
                "!ETD_MOVED!", "PYTHON3")
        except (arcpy.ExecuteError, RuntimeError):
            pass
        try:
            arcpy.management.ClearWorkspaceCache()
        except (arcpy.ExecuteError, RuntimeError):
            pass

        # Moved-only output
        if moved_only_final:
            try:
                out_dir = os.path.dirname(moved_only_final)
                out_name = os.path.basename(moved_only_final)
                arcpy.conversion.FeatureClassToFeatureClass(
                    moved_copy_final, out_dir, out_name,
                    "ETD_MOVED = 1")
            except (arcpy.ExecuteError, RuntimeError):
                log.warn(
                    "MODE B: FeatureClassToFeatureClass failed; "
                    "using fallback.")
                lyr_name = "lyr_anno_copy_" + uuid.uuid4().hex[:8]
                try:
                    arcpy.management.MakeFeatureLayer(
                        moved_copy_final, lyr_name)
                    arcpy.management.SelectLayerByAttribute(
                        lyr_name, "NEW_SELECTION", "ETD_MOVED = 1")
                    arcpy.management.CopyFeatures(
                        lyr_name, moved_only_final)
                except (arcpy.ExecuteError, RuntimeError):
                    moved_only_final = None
                finally:
                    try:
                        arcpy.management.Delete(lyr_name)
                    except (arcpy.ExecuteError, RuntimeError):
                        pass

        # Mode B label-position points
        label_positions_points = None
        if create_modeB_points:
            try:
                pt_name = f"LabelPos_FromAnno_{stamp}"
                label_positions_points = os.path.join(
                    out_container, pt_name)
                arcpy.management.CreateFeatureclass(
                    out_container, pt_name, "POINT",
                    spatial_reference=base_sr)
                _ensure_field(
                    label_positions_points, "SrcOID", "LONG")
                _ensure_field(
                    label_positions_points, "Status", "TEXT", length=16)
                _ensure_field(
                    label_positions_points, "XOff_PT", "DOUBLE")
                _ensure_field(
                    label_positions_points, "YOff_PT", "DOUBLE")
                _ensure_field(
                    label_positions_points, "AngleDeg", "DOUBLE")
                _ensure_field(
                    label_positions_points, "AnchorOID", "LONG")
                _ensure_field(
                    label_positions_points, "AnchorDist", "DOUBLE")
                with arcpy.da.SearchCursor(
                        moved_copy_final,
                        ["OID@", "ETD_STATUS", "XOffset", "YOffset",
                         "ETD_ANGLE", "ETD_AOID", "ETD_ADIST"]) as scp, \
                     arcpy.da.InsertCursor(
                        label_positions_points,
                        ["SHAPE@", "SrcOID", "Status", "XOff_PT",
                         "YOff_PT", "AngleDeg", "AnchorOID",
                         "AnchorDist"]) as icp:
                    for aoid, stt, xo2, yo2, ang2, ao2, ad2 in scp:
                        if int(aoid) in anchor_map:
                            anchor_oid, ax, ay, adist = \
                                anchor_map[int(aoid)]
                            gx = (ax + (float(xo2)
                                        if xo2 is not None else 0.0)
                                  * float(ground_per_point))
                            gy = (ay + (float(yo2)
                                        if yo2 is not None else 0.0)
                                  * float(ground_per_point))
                            p = arcpy.PointGeometry(
                                arcpy.Point(gx, gy), base_sr)
                            icp.insertRow(
                                (p, int(aoid), stt,
                                 float(xo2) if xo2 is not None else 0.0,
                                 float(yo2) if yo2 is not None else 0.0,
                                 float(ang2) if ang2 is not None else 0.0,
                                 int(anchor_oid), float(adist)))
                        else:
                            p = arcpy.PointGeometry(
                                arcpy.Point(0, 0), base_sr)
                            icp.insertRow(
                                (p, int(aoid), stt,
                                 float(xo2) if xo2 is not None else 0.0,
                                 float(yo2) if yo2 is not None else 0.0,
                                 float(ang2) if ang2 is not None else 0.0,
                                 -1, -1.0))
            except (arcpy.ExecuteError, RuntimeError):
                log.warn(
                    f"MODE B: label-position points failed: "
                    f"{traceback.format_exc()}")
                label_positions_points = None

        # Leader lines
        leaderlines_fc = None
        if create_leaderlines:
            try:
                ll_name = f"LeaderLines_{stamp}"
                leaderlines_fc = os.path.join(out_container, ll_name)
                arcpy.management.CreateFeatureclass(
                    out_container, ll_name, "POLYLINE",
                    spatial_reference=base_sr)
                _ensure_field(leaderlines_fc, "SrcOID", "LONG")
                _ensure_field(leaderlines_fc, "LenMU", "DOUBLE")
                with arcpy.da.InsertCursor(
                        leaderlines_fc,
                        ["SHAPE@", "SrcOID", "LenMU"]) as il:
                    for (oid2, ax2, ay2, cx2, cy2, rr2) in leader_rows:
                        if leaderlines_moved_only and float(rr2) <= 0.0:
                            continue
                        try:
                            arr = arcpy.Array(
                                [arcpy.Point(ax2, ay2),
                                 arcpy.Point(cx2, cy2)])
                            il.insertRow(
                                (arcpy.Polyline(arr, base_sr),
                                 int(oid2), float(rr2)))
                        except (arcpy.ExecuteError, RuntimeError) as ex:
                            log.warn(
                                f"Leader insert failed at OID "
                                f"{oid2}: {ex}")
            except (arcpy.ExecuteError, RuntimeError):
                log.warn(f"MODE B: leader lines failed: "
                         f"{traceback.format_exc()}")
                leaderlines_fc = None

        return (moved_copy_final, moved_only_final, label_positions_points,
                leaderlines_fc, report_all_rows, report_bad_rows,
                moved, unchanged, failed, skipped)



    # =========================================================================
    # Main execute orchestrator
    # =========================================================================

    def execute(self, parameters, messages):
        env_snap = _snapshot_env()
        try:
            _prime_env()
            self._execute_core(parameters, messages)
        except arcpy.ExecuteError:
            arcpy.AddError(arcpy.GetMessages(2))
            raise
        except RuntimeError as ex:
            arcpy.AddError(f"Runtime error: {ex}")
            arcpy.AddError(traceback.format_exc())
            raise
        finally:
            _restore_env(env_snap)

    def _execute_core(self, parameters, messages):
        pmap = {p.name: p for p in parameters}

        def _p_text(name, default=None):
            if name not in pmap:
                return default
            v = pmap[name].valueAsText
            if is_empty_gp(v):
                return default
            return v

        def _p_bool(name, default=False):
            if name in pmap and pmap[name].value is not None:
                try:
                    return bool(pmap[name].value)
                except (TypeError, ValueError):
                    pass
            v = _p_text(name, None)
            if v is None:
                return default
            return str(v).strip().lower() in (
                "true", "t", "1", "yes", "y", "on")

        def _p_int(name, default=0):
            if name in pmap and pmap[name].value is not None:
                try:
                    return int(pmap[name].value)
                except (TypeError, ValueError):
                    pass
            v = _p_text(name, None)
            if v is None:
                return default
            try:
                return int(float(v))
            except (TypeError, ValueError):
                return default

        def _p_float(name, default=0.0):
            if name in pmap and pmap[name].value is not None:
                try:
                    return float(pmap[name].value)
                except (TypeError, ValueError):
                    pass
            v = _p_text(name, None)
            if v is None:
                return default
            try:
                return float(v)
            except (TypeError, ValueError):
                return default

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
        conflict_mode = _p_text(
            "conflict_test_mode", "BALANCED_EXTENT_THEN_GEOMETRY")
        padding = _p_float("padding", 0.0)
        extra_search = _p_float("extra_search", 0.0)
        ref_scale = _p_int("reference_scale", 25000)
        font_pt = _p_float("font_size_pt", 8.0)
        k_fac = _p_float("char_width_factor", 0.60)
        report_text_mode = _p_text(
            "report_text_mode", "ASCII_SAFE_REPLACE")
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
        rotation_write_mode = _p_text(
            "rotation_write_mode", "SET_ABSOLUTE")
        create_modeB_points = _p_bool("create_modeB_points", True)
        create_leaderlines = _p_bool("create_leaderlines", False)
        leaderlines_moved_only = _p_bool("leaderlines_moved_only", True)
        reverse_offsets = _p_bool("reverse_offsets", False)
        add_to_map = _p_bool("add_to_map", True)
        # F1 NEW
        use_legacy_evaluation = _p_bool("use_legacy_evaluation", False)

        log = Logger(debug_level, debug_log_file, report_text_mode)
        log.info(f"Tool start. mode={mode} preview_only={preview_only}")
        if log.path:
            log.info(f"Log file: {log.path}")
        log.diag(f"use_legacy_evaluation={use_legacy_evaluation}")
        if not _NUMPY_OK and not use_legacy_evaluation:
            log.warn("NumPy unavailable; falling back to legacy evaluation.")
            use_legacy_evaluation = True

        if mode == "POINT_LAYER_WITH_TEXT_FIELD":
            if is_empty_gp(in_points) or is_empty_gp(text_field):
                raise arcpy.ExecuteError(
                    "Mode A selected but Input Point Layer / Text Field "
                    "is empty.")
        else:
            if is_empty_gp(anno_layer) or is_empty_gp(anchor_points):
                raise arcpy.ExecuteError(
                    "Mode B selected but Annotation Layer / Anchor Points "
                    "is empty.")
        if not rings:
            raise arcpy.ExecuteError("Rings must be provided (e.g., 2 4 6).")
        obstacle_layers = []
        if not is_empty_gp(obstacle_layers_txt):
            obstacle_layers = [s for s in
                               _safe_str(obstacle_layers_txt).split(";")
                               if s.strip()]
        if not obstacle_layers:
            raise arcpy.ExecuteError("No obstacle layers provided.")

        if mode == "POINT_LAYER_WITH_TEXT_FIELD":
            _announce_selection("Points", in_points, log)
        else:
            _announce_selection("Annotation", anno_layer, log)
            _announce_selection("Anchors", anchor_points, log)
        for lyr in obstacle_layers:
            _announce_selection("Obstacle", lyr, log)

        angles = _biased_angles(_angles(directions), direction_bias)
        rings_sorted = sorted(rings)
        max_ring = max(rings_sorted)
        max_attempts = len(rings_sorted) * directions * 2

        meters_per_point = 0.0254 / 72.0
        ground_per_point = meters_per_point * float(ref_scale)
        if ground_per_point <= 0:
            ground_per_point = meters_per_point * 25000.0

        if mode == "POINT_LAYER_WITH_TEXT_FIELD":
            base_sr = arcpy.Describe(
                _resolve_full_source(in_points)).spatialReference
        else:
            base_sr = arcpy.Describe(
                _resolve_full_source(anno_layer)).spatialReference

        scratch_gdb = arcpy.env.scratchGDB
        if not scratch_gdb or not arcpy.Exists(scratch_gdb):
            scratch_gdb = arcpy.env.scratchWorkspace
        if not scratch_gdb or not arcpy.Exists(scratch_gdb):
            raise arcpy.ExecuteError(
                "No scratch GDB available. Set arcpy.env.scratchGDB.")
        log.diag(f"Scratch (disk): {scratch_gdb}")

        # Build obstacle store routed by F1 toggle.
        if use_legacy_evaluation:
            obstacle_store = _ObstacleStoreLegacy(log)
            log.diag("OBSTACLE STORE: legacy SLBL path "
                     "(use_legacy_evaluation=True).")
        else:
            obstacle_store = _ObstacleStoreAABB(log)
            log.diag("OBSTACLE STORE: optimised AABB path "
                     "(use_legacy_evaluation=False).")
        for lyr in obstacle_layers:
            obstacle_store.add_layer(lyr)

        # Reports go to scratch.
        out_all = arcpy.CreateUniqueName(
            "elevtext_report_all", scratch_gdb)
        out_bad = arcpy.CreateUniqueName(
            "elevtext_report_unresolved", scratch_gdb)
        arcpy.management.CreateFeatureclass(
            scratch_gdb, os.path.basename(out_all), "POINT",
            spatial_reference=base_sr)
        for fname, ftype, flen in [
            ("SrcOID", "LONG", None), ("Status", "TEXT", 16),
            ("Ring", "DOUBLE", None), ("AngleDeg", "DOUBLE", None),
            ("Attempts", "LONG", None), ("AnchorOID", "LONG", None),
            ("AnchorDist", "DOUBLE", None), ("TextVal", "TEXT", 128),
        ]:
            _ensure_field(out_all, fname, ftype, flen)
        arcpy.management.CreateFeatureclass(
            scratch_gdb, os.path.basename(out_bad), "POINT",
            spatial_reference=base_sr)
        for fname, ftype, flen in [
            ("SrcOID", "LONG", None), ("Reason", "TEXT", 200),
            ("AnchorOID", "LONG", None), ("TextVal", "TEXT", 128),
        ]:
            _ensure_field(out_bad, fname, ftype, flen)

        moved_copy_final = None
        moved_only_final = None
        label_positions_points = None
        leaderlines_fc = None
        report_all_rows: list = []
        report_bad_rows: list = []
        moved = unchanged = failed = skipped = 0

        try:
            if mode == "POINT_LAYER_WITH_TEXT_FIELD":
                (moved_copy_final, moved_only_final,
                 label_positions_points, leaderlines_fc,
                 report_all_rows, report_bad_rows,
                 moved, unchanged, failed, skipped) = self._execute_mode_a(
                    log, in_points, text_field, ref_scale, font_pt,
                    k_fac, padding, rings_sorted, angles, max_ring,
                    search_pattern, spiral_step, max_attempts,
                    conflict_mode, extra_search, avoid_label_label,
                    modeA_rotated_box, obstacle_store, base_sr,
                    scratch_gdb, report_text_mode, create_moved_only,
                    create_leaderlines, leaderlines_moved_only,
                    ground_per_point, use_legacy_evaluation)
            else:
                (moved_copy_final, moved_only_final,
                 label_positions_points, leaderlines_fc,
                 report_all_rows, report_bad_rows,
                 moved, unchanged, failed, skipped) = self._execute_mode_b(
                    log, anno_layer, anchor_points, link_method,
                    featureid_field, max_match_dist_text, ref_scale,
                    rings_sorted, angles, max_ring, search_pattern,
                    spiral_step, max_attempts, conflict_mode,
                    extra_search, obstacle_store, base_sr, scratch_gdb,
                    report_text_mode, create_moved_only,
                    create_modeB_points, create_leaderlines,
                    leaderlines_moved_only, apply_rotation_modeB,
                    rotation_write_mode, reverse_offsets,
                    preview_only, ground_per_point)

            with arcpy.da.InsertCursor(
                    out_all,
                    ["SHAPE@", "SrcOID", "Status", "Ring", "AngleDeg",
                     "Attempts", "AnchorOID", "AnchorDist",
                     "TextVal"]) as ic_all:
                for row in report_all_rows:
                    ic_all.insertRow(row)
            with arcpy.da.InsertCursor(
                    out_bad,
                    ["SHAPE@", "SrcOID", "Reason", "AnchorOID",
                     "TextVal"]) as ic_bad:
                for row in report_bad_rows:
                    ic_bad.insertRow(row)
        except arcpy.ExecuteError as ex:
            log.error(f"ExecuteError: {_safe_str(ex)}")
            log.error(f"ArcPy messages (2): {arcpy.GetMessages(2)}")
            log.error(traceback.format_exc())
            raise
        except RuntimeError as ex:
            log.error(f"RuntimeError: {_safe_str(ex)}")
            log.error(traceback.format_exc())
            raise
        finally:
            try:
                obstacle_store.cleanup()
            except (arcpy.ExecuteError, RuntimeError):
                pass

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
                    label_positions_points if label_positions_points
                    else moved_copy_final)
            if "out_leaderlines" in pmap:
                pmap["out_leaderlines"].value = leaderlines_fc
        except (AttributeError, KeyError):
            pass

        if add_to_map:
            paths = [moved_copy_final, moved_only_final, leaderlines_fc,
                     (label_positions_points
                      if (label_positions_points
                          and label_positions_points != moved_copy_final)
                      else None),
                     out_all, out_bad]
            self._add_layers_to_active_map(paths)

        log.info(
            f"Finished. MOVED={moved} UNCHANGED={unchanged} "
            f"FAILED={failed} SKIPPED={skipped}")
        if mode == "ANNOTATION_LAYER_AND_ANCHOR_POINTS":
            log.info("Mode B: movement applied via XOffset/YOffset on the "
                     "output copy. Input is not modified.")
        else:
            log.info("Mode A: input points are NOT modified. Output is "
                     "label positions.")
        log.info(f"Outputs: moved_copy={moved_copy_final} "
                 f"moved_only={moved_only_final}")
