# -*- coding: utf-8 -*-
"""
Generate_TitanWorld_Pro.py  ::  Titan World 2.0 (Topologically Logical Pipeline)
================================================================================

Procedural stress-test world generator for the "Carto" toolbox suite
(ArcGIS Pro 3.x / Python 3.9+).

ARCHITECTURE
------------
This is **not** a flat collection of independent layers. It is a strict
cartographic pipeline in which every layer is derived from -- or dependent
on -- the layers generated before it:

    STAGE 1  Terrain Oracle  --  global get_elevation(x, y) = compound
                                 sine/cosine pseudo-Perlin noise. Yields
                                 mountains, valleys, and a central flat
                                 plain. Generates Elevation_Points,
                                 Contours, and embeds the Titan Ridge into
                                 a designated mountain peak.

    STAGE 2  Hydrology       --  Drainage / Rivers descend from peaks
                                 along the negative gradient of the
                                 elevation field, settling into valley
                                 floors.

    STAGE 3  Infrastructure  --  Roads laid on flat-terrain corridors
                                 (low |grad E|, low absolute elevation),
                                 with logical bridge crossings over the
                                 rivers (90 deg / 45 deg). T-junction
                                 end-touches and collinear overlaps are
                                 injected at specific river crossings to
                                 exercise Plugin 01.

    STAGE 4  Megacity        --  10,000+ Buildings clustered tightly
             & Utilities         along the road network on flat plain
                                 only (rejecting any candidate too close
                                 to a road centerline or a river).
                                 Gas_Pipes run perfectly parallel under
                                 the roads. Power_Lines mostly parallel
                                 but cross at acute angles in defined
                                 conflict zones to stress Plugin 02.

    STAGE 5  Geological      --  Springs only at the *base* of the
             & Cartographic       mountains (within a narrow elevation
             Edges                window). Five "Fault Line" springs are
                                 forced exactly collinear along a steep
                                 slope contour to provoke SVD singularity
                                 in Plugin 06. Map_Frame and Custom_AOI
                                 polygons are emitted; the AOI is given a
                                 sawtooth edge that bites into the dense
                                 mountainous contours, producing the sub-
                                 decimeter slivers that exercise Plugin 05.

Everything still uses ``arcpy.da.InsertCursor``, ``math``, and ``random``
exclusively, with True Curves authored via the Pro JSON geometry spec for
the highway interchange arcs (Plugin 02 stress).

Usage
-----

    > propy Generate_TitanWorld_Pro.py [--out C:\\path\\to\\workspace]

The script is idempotent: an existing TitanWorld_Pro.gdb at the target
location is deleted and regenerated.
"""

from __future__ import annotations

import argparse
import math
import os
import random
import sys
import time
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

try:
    import arcpy
except ImportError:  # pragma: no cover - script is arcpy-only
    sys.stderr.write(
        "[FATAL] This generator requires arcpy. Run with ArcGIS Pro's "
        "'propy' interpreter.\n"
    )
    raise


# ---------------------------------------------------------------------------
# Global configuration
# ---------------------------------------------------------------------------

GDB_NAME = "TitanWorld_Pro.gdb"

# Projected coordinate system: UTM Zone 11N / NAD 1983 (well-known WKID 26911).
PROJECTED_WKID = 26911
GEOGRAPHIC_WKID = 4326  # GCS_WGS_1984 -- used to trigger Plugin 07's GCS warning.

# 20 km x 20 km region in UTM meters.
X_MIN = 500_000.0
X_MAX = 520_000.0
Y_MIN = 3_900_000.0
Y_MAX = 3_920_000.0

WIDTH = X_MAX - X_MIN
HEIGHT = Y_MAX - Y_MIN

# Reproducibility.
RANDOM_SEED = 20260527
random.seed(RANDOM_SEED)

# ----- Stage scale knobs ----------------------------------------------------
ELEVATION_GRID_NX = 200             # 200 x 200 sample grid for elevation field
ELEVATION_GRID_NY = 200
CONTOUR_INTERVAL = 25.0             # meters
RIDGE_VERTICES = 500_000            # mandated by spec (500k spiral)

DRAINAGE_STREAMS = 60               # streams seeded along ridges
DRAINAGE_MAX_STEPS = 4500           # max integration steps per stream

ROAD_PRIMARY_COUNT = 6              # arterial corridors in the plain
ROAD_SECONDARY_COUNT = 14           # secondary connectors
ROAD_LOCAL_GRID_SPACING = 220.0     # meters between local grid lines in plain
ROAD_T_JUNCTIONS = 200              # injected end-touch stubs
ROAD_COLLINEAR_OVERLAPS = 120       # injected collinear overlap stubs

BUILDINGS_TARGET = 10_500           # mandated 10,000+
BUILDING_BAND_WIDTH = 35.0          # max distance from a road centerline
GAS_PIPE_OFFSET = 4.0               # meters perpendicular under the road
POWER_LINE_OFFSET = 8.0             # meters perpendicular alongside the road
POWER_LINE_CONFLICT_ZONES = 5       # acute-angle crossings stressing Plugin 02

SPRINGS_BASE_COUNT = 350            # ambient base-of-mountain springs
LABEL_BOXES = 6_500                 # heavily overlapping AABBs

INDEX_GRID_CELLS_PER_AXIS = 250
INDEX_GRID_GCS_CELLS_PER_AXIS = 60
HUGE_GRID_TICKS = 25_000            # MAX_TICKS_PER_AXIS guard driver

# ----- Elevation classification thresholds (meters) ------------------------
PLAIN_MAX_ELEV = 1080.0    # below this is "plain" (and central plate)
PLAIN_MAX_SLOPE = 0.020    # |grad E| ceiling for the plain
MOUNTAIN_MIN_ELEV = 1450.0
SPRING_BASE_ELEV_LO = 1200.0
SPRING_BASE_ELEV_HI = 1380.0  # springs sit in this elevation band

# Spatial reference handles.
SR_PROJECTED = arcpy.SpatialReference(PROJECTED_WKID)
SR_GEOGRAPHIC = arcpy.SpatialReference(GEOGRAPHIC_WKID)


# ---------------------------------------------------------------------------
# Logging helpers
# ---------------------------------------------------------------------------

_T0 = time.time()


def log(msg: str) -> None:
    line = f"[+{time.time() - _T0:7.2f}s] {msg}"
    try:
        arcpy.AddMessage(line)
    except Exception:
        pass
    print(line, flush=True)


def warn(msg: str) -> None:
    line = f"[WARN] {msg}"
    try:
        arcpy.AddWarning(line)
    except Exception:
        pass
    print(line, flush=True)


# ---------------------------------------------------------------------------
# Geometry helpers
# ---------------------------------------------------------------------------

def lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * t


def clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def in_aoi(x: float, y: float, margin: float = 0.0) -> bool:
    return (X_MIN + margin) <= x <= (X_MAX - margin) and \
           (Y_MIN + margin) <= y <= (Y_MAX - margin)


def make_polyline(coords: Sequence[Tuple[float, float]],
                  sr: arcpy.SpatialReference = SR_PROJECTED) -> arcpy.Polyline:
    arr = arcpy.Array()
    for x, y in coords:
        arr.add(arcpy.Point(x, y))
    return arcpy.Polyline(arr, sr)


def make_polygon(rings: Sequence[Sequence[Tuple[float, float]]],
                 sr: arcpy.SpatialReference = SR_PROJECTED) -> arcpy.Polygon:
    outer = arcpy.Array()
    for ring in rings:
        ring_arr = arcpy.Array()
        for x, y in ring:
            ring_arr.add(arcpy.Point(x, y))
        if ring and ring[0] != ring[-1]:
            ring_arr.add(arcpy.Point(ring[0][0], ring[0][1]))
        outer.add(ring_arr)
    return arcpy.Polygon(outer, sr)


def make_point(x: float, y: float,
               sr: arcpy.SpatialReference = SR_PROJECTED) -> arcpy.PointGeometry:
    return arcpy.PointGeometry(arcpy.Point(x, y), sr)


# ---------------------------------------------------------------------------
# Workspace bootstrap
# ---------------------------------------------------------------------------

def build_gdb(out_dir: str) -> str:
    out_dir = os.path.abspath(out_dir)
    os.makedirs(out_dir, exist_ok=True)
    gdb_path = os.path.join(out_dir, GDB_NAME)
    if arcpy.Exists(gdb_path):
        log(f"Removing existing {gdb_path}")
        arcpy.management.Delete(gdb_path)
    log(f"Creating {gdb_path}")
    arcpy.management.CreateFileGDB(out_dir, GDB_NAME)
    arcpy.env.workspace = gdb_path
    arcpy.env.overwriteOutput = True
    return gdb_path


def create_fc(name: str,
              geom_type: str,
              fields: Sequence[Tuple[str, str, int]] = (),
              sr: arcpy.SpatialReference = SR_PROJECTED) -> str:
    arcpy.management.CreateFeatureclass(
        out_path=arcpy.env.workspace,
        out_name=name,
        geometry_type=geom_type,
        spatial_reference=sr,
    )
    fc_path = os.path.join(arcpy.env.workspace, name)
    for fname, ftype, flen in fields:
        if ftype.upper() == "TEXT":
            arcpy.management.AddField(fc_path, fname, "TEXT", field_length=flen)
        else:
            arcpy.management.AddField(fc_path, fname, ftype)
    return fc_path



# ===========================================================================
# STAGE 1 :: The Terrain Oracle
# ===========================================================================
#
# A deterministic, analytic elevation field built from compound sinusoids
# (a cheap pseudo-Perlin substitute requiring only the standard library).
# Designed so that:
#
#   * Two anchored mountain peaks rise above 1700 m on the west and NE
#     of the AOI -- one of them ("Mt. Titan") hosts the 500k-vertex Ridge.
#   * A central plain sits at ~1000 m with very low slope, providing a
#     natural corridor for the road network and the megacity.
#   * Valleys carve through the terrain between peaks, offering deterministic
#     drainage paths for Stage 2.
#
# All downstream stages query the same get_elevation() function, so every
# layer is genuinely topologically dependent on the terrain.

# Mountain peaks (cx, cy, radius_m, peak_offset_m).
PEAKS: List[Tuple[float, float, float, float]] = [
    # Mt. Titan -- the dominant SW peak; will host the Titan Ridge.
    (X_MIN + 0.27 * WIDTH, Y_MIN + 0.78 * HEIGHT, 3200.0, 850.0),
    # NE peak -- companion massif.
    (X_MIN + 0.82 * WIDTH, Y_MIN + 0.83 * HEIGHT, 2600.0, 700.0),
    # Southern massif -- secondary range.
    (X_MIN + 0.15 * WIDTH, Y_MIN + 0.15 * HEIGHT, 2400.0, 580.0),
    # SE outlier hill.
    (X_MIN + 0.85 * WIDTH, Y_MIN + 0.20 * HEIGHT, 1800.0, 420.0),
]

# The central plain is a depression centered here.
PLAIN_CENTER = (X_MIN + 0.55 * WIDTH, Y_MIN + 0.45 * HEIGHT)
PLAIN_RADIUS = 4500.0

# Mt. Titan reference (peak 0) -- exposed for the Titan Ridge generator.
MT_TITAN_CX, MT_TITAN_CY, _, _ = PEAKS[0]


def _peak_contribution(x: float, y: float,
                       cx: float, cy: float,
                       radius: float, peak: float) -> float:
    """Bell-shaped peak contribution (Gaussian-like)."""
    dx, dy = x - cx, y - cy
    d2 = dx * dx + dy * dy
    sigma2 = (radius * 0.55) ** 2
    return peak * math.exp(-d2 / (2.0 * sigma2))


def _plain_basin(x: float, y: float) -> float:
    """Negative bell that pulls the central plain down to a flat plate."""
    dx = x - PLAIN_CENTER[0]
    dy = y - PLAIN_CENTER[1]
    d2 = dx * dx + dy * dy
    sigma2 = (PLAIN_RADIUS * 0.9) ** 2
    return -120.0 * math.exp(-d2 / (2.0 * sigma2))


def get_elevation(x: float, y: float) -> float:
    """Global, deterministic elevation field in meters.

    Returns elevation at projected coordinate (x, y). This is *the*
    function from which all subsequent layers are derived.
    """
    u = (x - X_MIN) / WIDTH
    v = (y - Y_MIN) / HEIGHT

    # Base plate -- gentle continental tilt.
    z = 980.0 + 30.0 * u + 25.0 * v

    # Three octaves of compound sinusoids approximating Perlin noise.
    z += 80.0 * math.sin(2.0 * math.pi * (1.3 * u + 0.7 * v) + 0.3)
    z += 50.0 * math.cos(2.0 * math.pi * (2.2 * u - 1.4 * v) + 1.7)
    z += 30.0 * math.sin(2.0 * math.pi * (4.7 * u + 3.1 * v) + 2.6)
    z += 18.0 * math.cos(2.0 * math.pi * (8.3 * u - 5.9 * v) + 0.9)
    z += 9.0 * math.sin(2.0 * math.pi * (17.1 * u + 13.3 * v) + 4.1)

    # Mountains.
    for cx, cy, r, p in PEAKS:
        z += _peak_contribution(x, y, cx, cy, r, p)

    # Central plain depression that flattens the city corridor.
    z += _plain_basin(x, y)

    return z


def get_elevation_gradient(x: float, y: float, h: float = 5.0) -> Tuple[float, float]:
    """Central-difference gradient of the elevation field at (x, y)."""
    dz_dx = (get_elevation(x + h, y) - get_elevation(x - h, y)) / (2.0 * h)
    dz_dy = (get_elevation(x, y + h) - get_elevation(x, y - h)) / (2.0 * h)
    return dz_dx, dz_dy


def slope_magnitude(x: float, y: float) -> float:
    gx, gy = get_elevation_gradient(x, y)
    return math.hypot(gx, gy)


# ---- Cached sampled elevation grid (for contour extraction) ---------------

_ELEV_GRID: Optional[List[List[float]]] = None
_ELEV_NX = ELEVATION_GRID_NX
_ELEV_NY = ELEVATION_GRID_NY


def _elevation_grid() -> List[List[float]]:
    """Sample get_elevation onto a regular grid (Marching Squares input)."""
    global _ELEV_GRID
    if _ELEV_GRID is not None:
        return _ELEV_GRID
    log(f"  Sampling elevation grid {_ELEV_NX} x {_ELEV_NY}")
    grid: List[List[float]] = []
    for j in range(_ELEV_NY + 1):
        y = Y_MIN + j * (HEIGHT / _ELEV_NY)
        row = [get_elevation(X_MIN + i * (WIDTH / _ELEV_NX), y)
               for i in range(_ELEV_NX + 1)]
        grid.append(row)
    _ELEV_GRID = grid
    return grid


def _grid_xy(i: int, j: int) -> Tuple[float, float]:
    return (X_MIN + i * (WIDTH / _ELEV_NX),
            Y_MIN + j * (HEIGHT / _ELEV_NY))


# ---- Marching Squares contour extraction ----------------------------------

def _interp(p1: Tuple[float, float], v1: float,
            p2: Tuple[float, float], v2: float,
            level: float) -> Tuple[float, float]:
    if abs(v2 - v1) < 1e-12:
        return p1
    t = (level - v1) / (v2 - v1)
    return (p1[0] + t * (p2[0] - p1[0]),
            p1[1] + t * (p2[1] - p1[1]))


def _marching_squares_segments(level: float) -> List[Tuple[Tuple[float, float],
                                                            Tuple[float, float]]]:
    """Return a list of (a, b) line segments approximating the iso-line."""
    grid = _elevation_grid()
    segs: List[Tuple[Tuple[float, float], Tuple[float, float]]] = []
    for j in range(_ELEV_NY):
        for i in range(_ELEV_NX):
            v00 = grid[j][i]
            v10 = grid[j][i + 1]
            v11 = grid[j + 1][i + 1]
            v01 = grid[j + 1][i]
            p00 = _grid_xy(i, j)
            p10 = _grid_xy(i + 1, j)
            p11 = _grid_xy(i + 1, j + 1)
            p01 = _grid_xy(i, j + 1)
            idx = ((1 if v00 >= level else 0)
                   | (2 if v10 >= level else 0)
                   | (4 if v11 >= level else 0)
                   | (8 if v01 >= level else 0))
            if idx in (0, 15):
                continue
            eb = _interp(p00, v00, p10, v10, level)
            er = _interp(p10, v10, p11, v11, level)
            et = _interp(p01, v01, p11, v11, level)
            el = _interp(p00, v00, p01, v01, level)
            if idx in (1, 14):
                segs.append((eb, el))
            elif idx in (2, 13):
                segs.append((eb, er))
            elif idx in (3, 12):
                segs.append((el, er))
            elif idx in (4, 11):
                segs.append((er, et))
            elif idx in (6, 9):
                segs.append((eb, et))
            elif idx in (7, 8):
                segs.append((el, et))
            elif idx == 5:
                segs.append((el, et))
                segs.append((eb, er))
            elif idx == 10:
                segs.append((eb, el))
                segs.append((er, et))
    return segs


def _stitch_segments(segs: List[Tuple[Tuple[float, float],
                                      Tuple[float, float]]]
                     ) -> List[List[Tuple[float, float]]]:
    """Stitch unordered segments into polylines using endpoint hashing."""
    eps = 1e-3

    def key(p: Tuple[float, float]) -> Tuple[int, int]:
        return (int(round(p[0] / eps)), int(round(p[1] / eps)))

    # Map each endpoint key to the list of segment indices touching it,
    # plus the original coordinates we use when emitting polylines.
    coord_for: Dict[Tuple[int, int], Tuple[float, float]] = {}
    incident: Dict[Tuple[int, int], List[int]] = {}
    for s_idx, (a, b) in enumerate(segs):
        ka, kb = key(a), key(b)
        coord_for.setdefault(ka, a)
        coord_for.setdefault(kb, b)
        incident.setdefault(ka, []).append(s_idx)
        incident.setdefault(kb, []).append(s_idx)

    used = [False] * len(segs)
    polylines: List[List[Tuple[float, float]]] = []

    for s_idx in range(len(segs)):
        if used[s_idx]:
            continue
        used[s_idx] = True
        a, b = segs[s_idx]
        line = [a, b]

        # Extend forward from b.
        cur_k = key(b)
        while True:
            nxt = None
            for cand in incident.get(cur_k, []):
                if used[cand]:
                    continue
                ca, cb = segs[cand]
                if key(ca) == cur_k:
                    nxt = (cand, cb)
                    break
                if key(cb) == cur_k:
                    nxt = (cand, ca)
                    break
            if nxt is None:
                break
            cand_idx, next_pt = nxt
            used[cand_idx] = True
            line.append(next_pt)
            cur_k = key(next_pt)

        # Extend backward from a.
        cur_k = key(a)
        while True:
            nxt = None
            for cand in incident.get(cur_k, []):
                if used[cand]:
                    continue
                ca, cb = segs[cand]
                if key(ca) == cur_k:
                    nxt = (cand, cb)
                    break
                if key(cb) == cur_k:
                    nxt = (cand, ca)
                    break
            if nxt is None:
                break
            cand_idx, next_pt = nxt
            used[cand_idx] = True
            line.insert(0, next_pt)
            cur_k = key(next_pt)

        if len(line) >= 2:
            polylines.append(line)
    return polylines


# ---- Titan Ridge: 500k-vertex fractal spiral on Mt. Titan -----------------

def _titan_ridge_coords(n_vertices: int) -> List[Tuple[float, float]]:
    """A 500k-vertex log-spiral perched on Mt. Titan's summit."""
    cx = MT_TITAN_CX
    cy = MT_TITAN_CY
    coords: List[Tuple[float, float]] = []
    a = 1.5
    b = 0.058
    theta_max = 70.0
    for i in range(n_vertices):
        t = i / (n_vertices - 1)
        theta = t * theta_max
        r = a * math.exp(b * theta)
        wob = (
            0.07 * math.sin(11.0 * theta)
            + 0.035 * math.sin(37.0 * theta + 1.3)
            + 0.018 * math.sin(91.0 * theta + 2.7)
        )
        r_eff = r * (1.0 + wob)
        x = cx + r_eff * math.cos(theta)
        y = cy + r_eff * math.sin(theta)
        x = clamp(x, X_MIN + 1.0, X_MAX - 1.0)
        y = clamp(y, Y_MIN + 1.0, Y_MAX - 1.0)
        coords.append((x, y))
    return coords


def build_stage1_terrain(gdb: str) -> None:
    log("[Stage 1] Building Terrain Oracle: Elevation_Points + Contours")

    elev_pts_fc = create_fc(
        "Elevation_Points", "POINT",
        fields=(
            ("PointID", "LONG", 0),
            ("Elev_M", "DOUBLE", 0),
            ("SlopeMag", "DOUBLE", 0),
            ("Class", "TEXT", 24),
        ),
    )
    contours_fc = create_fc(
        "Contours", "POLYLINE",
        fields=(
            ("ContourID", "LONG", 0),
            ("Elevation", "DOUBLE", 0),
            ("Kind", "TEXT", 24),
        ),
    )

    log("  Inserting Elevation_Points (subsampled grid)")
    pid = 0
    sample_step = 4
    with arcpy.da.InsertCursor(
            elev_pts_fc,
            ["SHAPE@", "PointID", "Elev_M", "SlopeMag", "Class"]) as cur:
        for j in range(0, _ELEV_NY + 1, sample_step):
            for i in range(0, _ELEV_NX + 1, sample_step):
                x, y = _grid_xy(i, j)
                z = get_elevation(x, y)
                s = slope_magnitude(x, y)
                if z >= MOUNTAIN_MIN_ELEV:
                    cls = "MOUNTAIN"
                elif z <= PLAIN_MAX_ELEV and s <= PLAIN_MAX_SLOPE:
                    cls = "PLAIN"
                elif s >= 0.05:
                    cls = "SLOPE"
                else:
                    cls = "FOOTHILL"
                cur.insertRow([make_point(x, y), pid, z, s, cls])
                pid += 1
        del cur  # Force release of GDB edit lock

    log(f"  Generating contours from elevation field "
        f"(interval = {CONTOUR_INTERVAL:.0f} m)")
    grid = _elevation_grid()
    z_min = min(min(row) for row in grid)
    z_max = max(max(row) for row in grid)
    log(f"  Elevation field range: [{z_min:.1f}, {z_max:.1f}] m")

    levels: List[float] = []
    z = math.ceil(z_min / CONTOUR_INTERVAL) * CONTOUR_INTERVAL
    while z < z_max:
        levels.append(z)
        z += CONTOUR_INTERVAL

    cid = 0
    with arcpy.da.InsertCursor(
            contours_fc, ["SHAPE@", "ContourID", "Elevation", "Kind"]) as cur:

        log(f"  Inserting Titan Ridge ({RIDGE_VERTICES:,} vertices) on Mt. Titan")
        ridge_coords = _titan_ridge_coords(RIDGE_VERTICES)
        ridge_arr = arcpy.Array()
        for x, y in ridge_coords:
            ridge_arr.add(arcpy.Point(x, y))
        ridge = arcpy.Polyline(ridge_arr, SR_PROJECTED)
        cur.insertRow([
            ridge, cid,
            get_elevation(MT_TITAN_CX, MT_TITAN_CY),
            "TITAN_RIDGE",
        ])
        cid += 1

        for level in levels:
            segs = _marching_squares_segments(level)
            polylines = _stitch_segments(segs)
            kind = ("INDEX"
                    if abs(level % (CONTOUR_INTERVAL * 5)) < 1e-6
                    else "INTERMEDIATE")
            for poly in polylines:
                if len(poly) < 2:
                    continue
                cur.insertRow([
                    make_polyline(poly), cid, level, kind,
                ])
                cid += 1
        log(f"  Inserted {cid - 1} contour features across {len(levels)} levels")
        del cur  # Force release of GDB edit lock



# ===========================================================================
# STAGE 2 :: Hydrology -- rivers descend the elevation gradient
# ===========================================================================

# Populated during Stage 2; consumed by Stage 3 (bridge alignment) and
# Stage 4 (building rejection).
RIVER_PATHS: List[List[Tuple[float, float]]] = []


def _trace_stream(start_x: float, start_y: float,
                  step: float = 18.0,
                  max_steps: int = DRAINAGE_MAX_STEPS,
                  ) -> List[Tuple[float, float]]:
    coords: List[Tuple[float, float]] = []
    x, y = start_x, start_y
    last_z = get_elevation(x, y)
    stuck = 0
    for _ in range(max_steps):
        if not in_aoi(x, y, margin=10.0):
            break
        gx, gy = get_elevation_gradient(x, y)
        gmag = math.hypot(gx, gy)
        if gmag < 1e-4:
            stuck += 1
            if stuck > 8:
                break
            x += random.uniform(-step, step)
            y += random.uniform(-step, step)
            continue
        # Negative gradient (downhill) with lateral meander.
        dx = -gx / gmag
        dy = -gy / gmag
        wob = math.sin(0.04 * len(coords)) * 0.35
        px, py = -dy, dx
        x += (dx + wob * px) * step
        y += (dy + wob * py) * step
        coords.append((x, y))
        z = get_elevation(x, y)
        if z < 980.0:
            break
        if z >= last_z - 1e-3:
            stuck += 1
            if stuck > 12:
                break
        else:
            stuck = 0
        last_z = z
    return coords


def build_stage2_hydrology(gdb: str) -> None:
    log("[Stage 2] Building Drainage + named hydrology layers "
        "(River_L / Seasonal_River_L / Canal / Abreez)")

    # Legacy aggregate layer (kept for backward compatibility with v2 workflows).
    drainage_fc = create_fc(
        "Drainage", "POLYLINE",
        fields=(
            ("DrainID", "LONG", 0),
            ("StreamType", "TEXT", 24),
            ("HeadElev_M", "DOUBLE", 0),
        ),
    )

    # ----- Named hydrology layers (v3): mirrors the project map sample ----
    # River_L           : main perennial channels (head elev > 1500 m).
    # Seasonal_River_L  : intermittent streams (head elev 1200-1500 m).
    # Abreez            : ephemeral wash / arroyo (head elev < 1200 m).
    # Canal             : low-slope plain channels carved straight along
    #                     primary plain ridges -- emitted procedurally.
    river_fc = create_fc(
        "River_L", "POLYLINE",
        fields=(("RiverID", "LONG", 0),
                ("Name", "TEXT", 48),
                ("HeadElev_M", "DOUBLE", 0)),
    )
    season_fc = create_fc(
        "Seasonal_River_L", "POLYLINE",
        fields=(("StreamID", "LONG", 0),
                ("Name", "TEXT", 48),
                ("HeadElev_M", "DOUBLE", 0)),
    )
    abreez_fc = create_fc(
        "Abreez", "POLYLINE",
        fields=(("AbreezID", "LONG", 0),
                ("Name", "TEXT", 48),
                ("HeadElev_M", "DOUBLE", 0)),
    )
    canal_fc = create_fc(
        "Canal", "POLYLINE",
        fields=(("CanalID", "LONG", 0),
                ("Name", "TEXT", 48),
                ("Width_M", "FLOAT", 0)),
    )

    seeds: List[Tuple[float, float]] = []
    for cx, cy, radius, _peak in PEAKS:
        n_seeds = max(6, int(radius / 220))
        for k in range(n_seeds):
            ang = (k + random.random()) * (2.0 * math.pi / n_seeds)
            r = radius * 0.45 * (0.9 + 0.2 * random.random())
            seeds.append((cx + r * math.cos(ang),
                          cy + r * math.sin(ang)))

    random.shuffle(seeds)
    seeds = seeds[:DRAINAGE_STREAMS]
    log(f"  Tracing {len(seeds)} streams from peak rings")

    did = riv_id = sea_id = abr_id = 0
    paths: List[List[Tuple[float, float]]] = []
    drainage_cur = arcpy.da.InsertCursor(
        drainage_fc, ["SHAPE@", "DrainID", "StreamType", "HeadElev_M"])
    river_cur = arcpy.da.InsertCursor(
        river_fc, ["SHAPE@", "RiverID", "Name", "HeadElev_M"])
    season_cur = arcpy.da.InsertCursor(
        season_fc, ["SHAPE@", "StreamID", "Name", "HeadElev_M"])
    abreez_cur = arcpy.da.InsertCursor(
        abreez_fc, ["SHAPE@", "AbreezID", "Name", "HeadElev_M"])
    try:
        for sx, sy in seeds:
            path = _trace_stream(sx, sy)
            if len(path) < 8:
                continue
            head_elev = get_elevation(path[0][0], path[0][1])
            shape = make_polyline(path)
            if head_elev > 1500.0:
                stype = "RIVER"
                river_cur.insertRow([
                    shape, riv_id, f"River_{riv_id:03d}", head_elev,
                ])
                riv_id += 1
            elif head_elev > 1200.0:
                stype = "STREAM"
                season_cur.insertRow([
                    shape, sea_id, f"Seasonal_{sea_id:03d}", head_elev,
                ])
                sea_id += 1
            else:
                stype = "CREEK"
                abreez_cur.insertRow([
                    shape, abr_id, f"Abreez_{abr_id:03d}", head_elev,
                ])
                abr_id += 1
            drainage_cur.insertRow([shape, did, stype, head_elev])
            paths.append(path)
            did += 1
    finally:
        del drainage_cur, river_cur, season_cur, abreez_cur

    # ----- Canals: straight engineered channels along the plain edges ----
    # We emit one canal per primary axis offset from the plain center to
    # provide low-slope linear water features that stay on the plain.
    log("  Inserting engineered canals across the plain")
    canal_count = 0
    with arcpy.da.InsertCursor(
            canal_fc, ["SHAPE@", "CanalID", "Name", "Width_M"]) as cur:
        n_canals = 4
        for k in range(n_canals):
            ang = math.pi * (k / n_canals) + math.radians(15.0)
            cx, cy = PLAIN_CENTER
            half_len = 0.45 * min(WIDTH, HEIGHT)
            x0 = clamp(cx - half_len * math.cos(ang),
                       X_MIN + 200, X_MAX - 200)
            y0 = clamp(cy - half_len * math.sin(ang),
                       Y_MIN + 200, Y_MAX - 200)
            x1 = clamp(cx + half_len * math.cos(ang),
                       X_MIN + 200, X_MAX - 200)
            y1 = clamp(cy + half_len * math.sin(ang),
                       Y_MIN + 200, Y_MAX - 200)
            steps = 80
            coords = []
            for s in range(steps + 1):
                t = s / steps
                x = lerp(x0, x1, t)
                y = lerp(y0, y1, t)
                # Only emit canal vertices that sit on the low-slope plain.
                if get_elevation(x, y) <= PLAIN_MAX_ELEV + 30:
                    coords.append((x, y))
            if len(coords) >= 4:
                cur.insertRow([
                    make_polyline(coords),
                    canal_count, f"Canal_{canal_count:02d}",
                    random.uniform(3.0, 8.0),
                ])
                # Canals also act as "rivers" for road bridge alignment.
                paths.append(coords)
                canal_count += 1
        del cur  # Force release of GDB edit lock

    log(f"  Inserted {did} drainage / {riv_id} River_L / {sea_id} "
        f"Seasonal_River_L / {abr_id} Abreez / {canal_count} Canal features")
    RIVER_PATHS.extend(paths)


# ===========================================================================
# STAGE 3 :: Infrastructure -- roads on the plain, bridging rivers
# ===========================================================================
#
# Roads are restricted to corridors satisfying:
#     z(x, y) <= PLAIN_MAX_ELEV       (low-elevation, central plain)
#     |grad z(x, y)| <= PLAIN_MAX_SLOPE  (low slope)
#
# Bridge alignment: at each river crossing, the local road segment is
# locally rotated to either 90 deg or 45 deg relative to the river axis.
# T-junction stubs and collinear overlap segments are anchored at river
# crossings so Plugin 01's true-crossing filter is genuinely exercised.

ROAD_PATHS: List[Tuple[List[Tuple[float, float]], str]] = []


def _is_plain(x: float, y: float) -> bool:
    if not in_aoi(x, y, margin=50.0):
        return False
    if get_elevation(x, y) > PLAIN_MAX_ELEV:
        return False
    if slope_magnitude(x, y) > PLAIN_MAX_SLOPE:
        return False
    return True


def _segment_intersection(a1, a2, b1, b2) -> Optional[Tuple[float, float, float, float]]:
    x1, y1 = a1
    x2, y2 = a2
    x3, y3 = b1
    x4, y4 = b2
    denom = (x1 - x2) * (y3 - y4) - (y1 - y2) * (x3 - x4)
    if abs(denom) < 1e-9:
        return None
    t = ((x1 - x3) * (y3 - y4) - (y1 - y3) * (x3 - x4)) / denom
    u = -((x1 - x2) * (y1 - y3) - (y1 - y2) * (x1 - x3)) / denom
    if 0.0 <= t <= 1.0 and 0.0 <= u <= 1.0:
        ix = x1 + t * (x2 - x1)
        iy = y1 + t * (y2 - y1)
        return ix, iy, t, u
    return None


def _build_primary_road(idx: int, n_total: int) -> List[Tuple[float, float]]:
    """A long, gently curved arterial threading through the plain."""
    angle = math.pi * (idx / n_total) + random.uniform(-0.25, 0.25)
    cx, cy = PLAIN_CENTER
    half_len = 0.55 * min(WIDTH, HEIGHT)
    x0 = clamp(cx - half_len * math.cos(angle), X_MIN + 100, X_MAX - 100)
    y0 = clamp(cy - half_len * math.sin(angle), Y_MIN + 100, Y_MAX - 100)
    x1 = clamp(cx + half_len * math.cos(angle), X_MIN + 100, X_MAX - 100)
    y1 = clamp(cy + half_len * math.sin(angle), Y_MIN + 100, Y_MAX - 100)

    coords: List[Tuple[float, float]] = []
    steps = 220
    for s in range(steps + 1):
        t = s / steps
        x = lerp(x0, x1, t)
        y = lerp(y0, y1, t)
        # Pull the arterial gently toward the plain center.
        bias = math.sin(math.pi * t)
        x += bias * (cx - x) * 0.15
        y += bias * (cy - y) * 0.15
        if _is_plain(x, y) or s in (0, steps):
            coords.append((x, y))
    return coords


def _build_secondary_road(primaries: List[List[Tuple[float, float]]]
                          ) -> List[Tuple[float, float]]:
    """Connect two primaries with a curved corridor entirely on the plain."""
    if len(primaries) < 2:
        return []
    pa = random.choice(primaries)
    others = [p for p in primaries if p is not pa]
    pb = random.choice(others) if others else pa
    a = pa[random.randint(len(pa) // 4, 3 * len(pa) // 4)]
    b = pb[random.randint(len(pb) // 4, 3 * len(pb) // 4)]
    coords: List[Tuple[float, float]] = []
    steps = 90
    bx, by = (a[0] + b[0]) / 2, (a[1] + b[1]) / 2
    cx_, cy_ = lerp(bx, PLAIN_CENTER[0], 0.4), lerp(by, PLAIN_CENTER[1], 0.4)
    for s in range(steps + 1):
        t = s / steps
        x = (1 - t) ** 2 * a[0] + 2 * (1 - t) * t * cx_ + t * t * b[0]
        y = (1 - t) ** 2 * a[1] + 2 * (1 - t) * t * cy_ + t * t * b[1]
        if _is_plain(x, y) or s in (0, steps):
            coords.append((x, y))
    return coords


def _build_local_grid() -> List[List[Tuple[float, float]]]:
    """A street grid clipped to the central plain corridor."""
    grids: List[List[Tuple[float, float]]] = []
    spacing = ROAD_LOCAL_GRID_SPACING

    y = Y_MIN + spacing
    while y < Y_MAX - spacing:
        run: List[Tuple[float, float]] = []
        x = X_MIN + 50.0
        while x < X_MAX - 50.0:
            if _is_plain(x, y):
                run.append((x, y))
            else:
                if len(run) >= 6:
                    grids.append(run)
                run = []
            x += 25.0
        if len(run) >= 6:
            grids.append(run)
        y += spacing

    x = X_MIN + spacing
    while x < X_MAX - spacing:
        run = []
        y = Y_MIN + 50.0
        while y < Y_MAX - 50.0:
            if _is_plain(x, y):
                run.append((x, y))
            else:
                if len(run) >= 6:
                    grids.append(run)
                run = []
            y += 25.0
        if len(run) >= 6:
            grids.append(run)
        x += spacing
    return grids


def _bridge_align(road: List[Tuple[float, float]],
                  rivers: List[List[Tuple[float, float]]]
                  ) -> List[Tuple[float, float]]:
    """Locally rotate road segments around river crossings to 90/45 deg."""
    if len(road) < 4:
        return road
    out = list(road)
    bx_min = min(p[0] for p in out)
    bx_max = max(p[0] for p in out)
    by_min = min(p[1] for p in out)
    by_max = max(p[1] for p in out)
    for ri, river in enumerate(rivers):
        if len(river) < 2:
            continue
        rx_min = min(p[0] for p in river) - 20
        rx_max = max(p[0] for p in river) + 20
        ry_min = min(p[1] for p in river) - 20
        ry_max = max(p[1] for p in river) + 20
        if (bx_max < rx_min or bx_min > rx_max or
                by_max < ry_min or by_min > ry_max):
            continue
        found = False
        for i in range(len(out) - 1):
            if found:
                break
            for j in range(len(river) - 1):
                hit = _segment_intersection(out[i], out[i + 1],
                                            river[j], river[j + 1])
                if hit is None:
                    continue
                ix, iy, _t, _u = hit
                rdx = river[j + 1][0] - river[j][0]
                rdy = river[j + 1][1] - river[j][1]
                rmag = math.hypot(rdx, rdy)
                if rmag < 1e-6:
                    continue
                rdx /= rmag
                rdy /= rmag
                snap_deg = 90 if ((ri + i) % 3) else 45
                ang = math.radians(snap_deg)
                bx = rdx * math.cos(ang) - rdy * math.sin(ang)
                by = rdx * math.sin(ang) + rdy * math.cos(ang)
                bridge_half = 18.0
                p_before = (ix - bx * bridge_half, iy - by * bridge_half)
                p_after = (ix + bx * bridge_half, iy + by * bridge_half)
                out[i] = p_before
                out[i + 1] = p_after
                found = True
                break
    return out


def _inject_t_and_overlap(roads: List[List[Tuple[float, float]]],
                          rivers: List[List[Tuple[float, float]]]
                          ) -> Tuple[List[List[Tuple[float, float]]],
                                     List[List[Tuple[float, float]]]]:
    """T-junction stubs *at river crossings* + collinear overlap segments."""
    t_stubs: List[List[Tuple[float, float]]] = []
    overlaps: List[List[Tuple[float, float]]] = []

    candidate_anchors: List[Tuple[float, float]] = []
    for road in roads:
        for vx, vy in road:
            close = False
            for river in rivers:
                for k in range(0, len(river), 12):
                    rx, ry = river[k]
                    if (vx - rx) ** 2 + (vy - ry) ** 2 < 60.0 ** 2:
                        close = True
                        break
                if close:
                    break
            if close:
                candidate_anchors.append((vx, vy))
        if len(candidate_anchors) > ROAD_T_JUNCTIONS * 4:
            break

    random.shuffle(candidate_anchors)
    for anchor in candidate_anchors[:ROAD_T_JUNCTIONS]:
        ang = random.uniform(0, 2 * math.pi)
        length = random.uniform(40, 120)
        end = (anchor[0] + length * math.cos(ang),
               anchor[1] + length * math.sin(ang))
        t_stubs.append([anchor, end])

    for _ in range(ROAD_COLLINEAR_OVERLAPS):
        if not roads:
            break
        road = random.choice(roads)
        if len(road) < 5:
            continue
        i = random.randint(0, len(road) - 4)
        overlaps.append(list(road[i:i + 3]))

    return t_stubs, overlaps


def build_stage3_roads(gdb: str) -> None:
    log("[Stage 3] Building Roads + named layers (Freeway/Highway/Parkway/"
        "Asphalt_Road/Gravel) with logical river bridges")

    # Aggregate / generic Roads layer (kept for v2 callers).
    roads_fc = create_fc(
        "Roads", "POLYLINE",
        fields=(
            ("RoadID", "LONG", 0),
            ("RoadClass", "TEXT", 32),
            ("Origin", "TEXT", 24),
        ),
    )

    # ----- Named v3 road class layers ------------------------------------
    freeway_fc = create_fc(
        "Freeway", "POLYLINE",
        fields=(("FwyID", "LONG", 0),
                ("Name", "TEXT", 32),
                ("HasTrueCurve", "SHORT", 0)),
    )
    highway_fc = create_fc(
        "Highway", "POLYLINE",
        fields=(("HwyID", "LONG", 0),
                ("Name", "TEXT", 32),
                ("HasTrueCurve", "SHORT", 0)),
    )
    parkway_fc = create_fc(
        "Parkway", "POLYLINE",
        fields=(("PkwyID", "LONG", 0),
                ("Name", "TEXT", 32),
                ("Switchback", "SHORT", 0)),
    )
    asphalt_fc = create_fc(
        "Asphalt_Road", "POLYLINE",
        fields=(("RoadID", "LONG", 0), ("Name", "TEXT", 32)),
    )
    gravel_fc = create_fc(
        "Gravel", "POLYLINE",
        fields=(("RoadID", "LONG", 0), ("Name", "TEXT", 32)),
    )

    # Point layers seeded at river crossings.
    bridge_fc = create_fc(
        "Bridge_P", "POINT",
        fields=(("BridgeID", "LONG", 0),
                ("RoadClass", "TEXT", 24),
                ("RiverIdx", "LONG", 0)),
    )
    culvert_fc = create_fc(
        "Culvert_Pnt", "POINT",
        fields=(("CulvertID", "LONG", 0),
                ("RoadClass", "TEXT", 24),
                ("RiverIdx", "LONG", 0)),
    )

    log(f"  Building {ROAD_PRIMARY_COUNT} primary arterials")
    primaries: List[List[Tuple[float, float]]] = []
    for i in range(ROAD_PRIMARY_COUNT):
        coords = _build_primary_road(i, ROAD_PRIMARY_COUNT)
        if len(coords) >= 4:
            coords = _bridge_align(coords, RIVER_PATHS)
            primaries.append(coords)

    log(f"  Building {ROAD_SECONDARY_COUNT} secondary connectors")
    secondaries: List[List[Tuple[float, float]]] = []
    for _ in range(ROAD_SECONDARY_COUNT):
        coords = _build_secondary_road(primaries)
        if len(coords) >= 4:
            coords = _bridge_align(coords, RIVER_PATHS)
            secondaries.append(coords)

    log("  Building local plain grid")
    locals_grid = _build_local_grid()
    locals_grid = [_bridge_align(c, RIVER_PATHS) for c in locals_grid]
    log(f"  Local grid produced {len(locals_grid)} segments on plain")

    log("  Injecting T-junctions and collinear overlaps at river crossings")
    all_real_roads = primaries + secondaries + locals_grid
    t_stubs, overlaps = _inject_t_and_overlap(all_real_roads, RIVER_PATHS)

    rid = 0
    with arcpy.da.InsertCursor(
            roads_fc, ["SHAPE@", "RoadID", "RoadClass", "Origin"]) as cur:
        for coords in primaries:
            cur.insertRow([make_polyline(coords), rid, "PRIMARY", "ARTERIAL"])
            ROAD_PATHS.append((coords, "PRIMARY"))
            rid += 1
        for coords in secondaries:
            cur.insertRow([
                make_polyline(coords), rid, "SECONDARY", "CONNECTOR",
            ])
            ROAD_PATHS.append((coords, "SECONDARY"))
            rid += 1
        for coords in locals_grid:
            cur.insertRow([make_polyline(coords), rid, "LOCAL", "GRID"])
            ROAD_PATHS.append((coords, "LOCAL"))
            rid += 1
        for coords in t_stubs:
            cur.insertRow([
                make_polyline(coords), rid, "LOCAL", "T_JUNCTION",
            ])
            rid += 1
        for coords in overlaps:
            cur.insertRow([
                make_polyline(coords), rid, "LOCAL", "COLLINEAR_OVERLAP",
            ])
            rid += 1
        del cur  # Force release of GDB edit lock

    log(f"  Inserted {rid} aggregate Roads features ("
        f"{len(primaries)} primary, "
        f"{len(secondaries)} secondary, "
        f"{len(locals_grid)} local, "
        f"{len(t_stubs)} T-stubs, "
        f"{len(overlaps)} overlaps)")

    # ===== v3 named-class road layers =====================================
    # Freeway: long primary arterials, with True-Curve interchanges spliced
    # into the geometry where feasible (Pro JSON circular arcs).
    log("  Emitting Freeway / Highway / Parkway / Asphalt_Road / Gravel")
    fwy_id = hwy_id = pkwy_id = asph_id = grv_id = 0
    bid = cid = 0

    crossings_for_layer: List[Tuple[List[Tuple[float, float]], str]] = []

    with arcpy.da.InsertCursor(
            freeway_fc, ["SHAPE@", "FwyID", "Name", "HasTrueCurve"]) as fwy:
        # Treat the longest 50% of primaries as Freeways.
        primaries_sorted = sorted(primaries, key=len, reverse=True)
        n_fwy = max(1, len(primaries_sorted) // 2)
        for coords in primaries_sorted[:n_fwy]:
            # Insert as a True-Curve polyline if the first/last/middle are
            # well-separated, otherwise as a dense polyline.
            try:
                tc = _truecurve_polyline(
                    coords[0], coords[-1], coords[len(coords) // 2])
                fwy.insertRow([
                    tc, fwy_id, f"Fwy_{fwy_id:02d}", 1,
                ])
                # Mirror the densely-vertexed approximation into the
                # aggregate ROAD_PATHS so downstream stages still see it
                # as a discrete polyline they can offset / cross.
                ROAD_PATHS.append((coords, "FREEWAY"))
                crossings_for_layer.append((coords, "FREEWAY"))
            except Exception:
                fwy.insertRow([
                    make_polyline(coords), fwy_id,
                    f"Fwy_{fwy_id:02d}", 0,
                ])
                ROAD_PATHS.append((coords, "FREEWAY"))
                crossings_for_layer.append((coords, "FREEWAY"))
            fwy_id += 1
        del fwy  # Force release of GDB edit lock

    with arcpy.da.InsertCursor(
            highway_fc, ["SHAPE@", "HwyID", "Name", "HasTrueCurve"]) as hwy:
        # Remaining primaries become Highways, and we splice True-Curve
        # interchange ramps into them at midpoints.
        for coords in primaries_sorted[n_fwy:]:
            try:
                tc = _truecurve_polyline(
                    coords[0], coords[-1], coords[len(coords) // 2])
                hwy.insertRow([tc, hwy_id, f"Hwy_{hwy_id:02d}", 1])
            except Exception:
                hwy.insertRow([
                    make_polyline(coords), hwy_id, f"Hwy_{hwy_id:02d}", 0,
                ])
            ROAD_PATHS.append((coords, "HIGHWAY"))
            crossings_for_layer.append((coords, "HIGHWAY"))
            hwy_id += 1

        # Plus a few standalone True-Curve highway ramps (sharp arcs).
        for _ in range(8):
            cx0 = clamp(PLAIN_CENTER[0] + random.uniform(-3500, 3500),
                        X_MIN + 500, X_MAX - 500)
            cy0 = clamp(PLAIN_CENTER[1] + random.uniform(-3500, 3500),
                        Y_MIN + 500, Y_MAX - 500)
            r = random.uniform(220, 500)
            a0 = random.uniform(0, 2 * math.pi)
            sweep = random.uniform(math.pi / 4, math.pi / 2)
            sp = (cx0 + r * math.cos(a0), cy0 + r * math.sin(a0))
            ep = (cx0 + r * math.cos(a0 + sweep),
                  cy0 + r * math.sin(a0 + sweep))
            mp = (cx0 + r * math.cos(a0 + sweep / 2),
                  cy0 + r * math.sin(a0 + sweep / 2))
            try:
                hwy.insertRow([
                    _truecurve_polyline(sp, ep, mp),
                    hwy_id, f"HwyRamp_{hwy_id:02d}", 1,
                ])
                hwy_id += 1
            except Exception:
                pass
        del hwy  # Force release of GDB edit lock

    # Parkway: a dense-vertex switchback up the foothill of Mt. Titan.
    with arcpy.da.InsertCursor(
            parkway_fc,
            ["SHAPE@", "PkwyID", "Name", "Switchback"]) as pkwy:
        switchback_coords: List[Tuple[float, float]] = []
        # Anchor at the foot of Mt. Titan, climb in tight switchbacks
        # toward the summit using thousands of densely-packed vertices.
        cx0 = MT_TITAN_CX + 1500.0
        cy0 = MT_TITAN_CY - 1500.0
        cx1 = MT_TITAN_CX + 200.0
        cy1 = MT_TITAN_CY - 200.0
        n_legs = 9
        verts_per_leg = 220
        for leg in range(n_legs):
            tA = leg / n_legs
            tB = (leg + 1) / n_legs
            ax = lerp(cx0, cx1, tA)
            ay = lerp(cy0, cy1, tA)
            bx = lerp(cx0, cx1, tB)
            by = lerp(cy0, cy1, tB)
            # Alternate side offset for the switchback.
            side = 1.0 if leg % 2 == 0 else -1.0
            for s in range(verts_per_leg):
                t = s / (verts_per_leg - 1)
                x = lerp(ax, bx, t)
                y = lerp(ay, by, t)
                # Perpendicular bow that flips at midpoint of the leg.
                bow_amp = 60.0 * math.sin(math.pi * t) * side
                # Compute perpendicular at this leg.
                ldx = bx - ax
                ldy = by - ay
                lmag = math.hypot(ldx, ldy) or 1.0
                nx_ = -ldy / lmag
                ny_ = ldx / lmag
                x += nx_ * bow_amp
                y += ny_ * bow_amp
                switchback_coords.append((x, y))
        pkwy.insertRow([
            make_polyline(switchback_coords),
            pkwy_id, "Mt_Titan_Switchback", 1,
        ])
        ROAD_PATHS.append((switchback_coords, "PARKWAY"))
        crossings_for_layer.append((switchback_coords, "PARKWAY"))
        pkwy_id += 1

        # A handful of straight shorter parkways across the plain.
        for _ in range(3):
            coords = _build_secondary_road(primaries)
            if len(coords) >= 4:
                pkwy.insertRow([
                    make_polyline(coords),
                    pkwy_id, f"Pkwy_{pkwy_id:02d}", 0,
                ])
                ROAD_PATHS.append((coords, "PARKWAY"))
                crossings_for_layer.append((coords, "PARKWAY"))
                pkwy_id += 1
        del pkwy  # Force release of GDB edit lock

    # Asphalt_Road: the local plain grid + secondary connectors.
    with arcpy.da.InsertCursor(
            asphalt_fc, ["SHAPE@", "RoadID", "Name"]) as asph:
        for coords in secondaries:
            asph.insertRow([
                make_polyline(coords), asph_id, f"Asph_{asph_id:04d}",
            ])
            crossings_for_layer.append((coords, "ASPHALT_ROAD"))
            asph_id += 1
        for coords in locals_grid:
            asph.insertRow([
                make_polyline(coords), asph_id, f"AsphGrid_{asph_id:04d}",
            ])
            crossings_for_layer.append((coords, "ASPHALT_ROAD"))
            asph_id += 1
        del asph  # Force release of GDB edit lock

    # Gravel: T-stubs are a great natural fit for unpaved stubs, plus a
    # handful of additional rural tracks meandering off the plain edges.
    with arcpy.da.InsertCursor(
            gravel_fc, ["SHAPE@", "RoadID", "Name"]) as grv:
        for coords in t_stubs:
            grv.insertRow([
                make_polyline(coords), grv_id, f"Gravel_{grv_id:04d}",
            ])
            crossings_for_layer.append((coords, "GRAVEL"))
            grv_id += 1
        for _ in range(20):
            x0, y0 = (clamp(random.uniform(X_MIN + 200, X_MAX - 200),
                            X_MIN + 200, X_MAX - 200),
                      clamp(random.uniform(Y_MIN + 200, Y_MAX - 200),
                            Y_MIN + 200, Y_MAX - 200))
            ang = random.uniform(0, 2 * math.pi)
            length = random.uniform(180, 500)
            steps = 60
            cx_, cy_ = x0, y0
            coords = [(x0, y0)]
            for s in range(1, steps + 1):
                t = s / steps
                cx_ += (length / steps) * math.cos(ang) + random.uniform(-3, 3)
                cy_ += (length / steps) * math.sin(ang) + random.uniform(-3, 3)
                coords.append((cx_, cy_))
            grv.insertRow([
                make_polyline(coords), grv_id, f"Gravel_{grv_id:04d}",
            ])
            crossings_for_layer.append((coords, "GRAVEL"))
            grv_id += 1
        del grv  # Force release of GDB edit lock

    log(f"  Named layers inserted: "
        f"Freeway={fwy_id} Highway={hwy_id} Parkway={pkwy_id} "
        f"Asphalt_Road={asph_id} Gravel={grv_id}")

    # ----- Bridge_P / Culvert_Pnt at every road x river intersection -----
    log("  Computing Bridge_P / Culvert_Pnt at named-road x river crossings")
    with arcpy.da.InsertCursor(
            bridge_fc, ["SHAPE@", "BridgeID", "RoadClass", "RiverIdx"]) as bp, \
         arcpy.da.InsertCursor(
            culvert_fc,
            ["SHAPE@", "CulvertID", "RoadClass", "RiverIdx"]) as cp:
        for coords, rcls in crossings_for_layer:
            for ri, river in enumerate(RIVER_PATHS):
                if len(river) < 2:
                    continue
                # Cheap bbox rejection.
                rx_min = min(p[0] for p in river)
                rx_max = max(p[0] for p in river)
                ry_min = min(p[1] for p in river)
                ry_max = max(p[1] for p in river)
                cx_min = min(p[0] for p in coords)
                cx_max = max(p[0] for p in coords)
                cy_min = min(p[1] for p in coords)
                cy_max = max(p[1] for p in coords)
                if (cx_max < rx_min or cx_min > rx_max or
                        cy_max < ry_min or cy_min > ry_max):
                    continue
                first_hit = None
                for i in range(len(coords) - 1):
                    if first_hit is not None:
                        break
                    for j in range(len(river) - 1):
                        hit = _segment_intersection(
                            coords[i], coords[i + 1],
                            river[j], river[j + 1])
                        if hit is None:
                            continue
                        first_hit = hit
                        break
                if first_hit is None:
                    continue
                ix, iy, _t, _u = first_hit
                # Major roads -> Bridge_P, minor roads -> Culvert_Pnt.
                if rcls in ("FREEWAY", "HIGHWAY", "PARKWAY"):
                    bp.insertRow([make_point(ix, iy), bid, rcls, ri])
                    bid += 1
                else:
                    cp.insertRow([make_point(ix, iy), cid, rcls, ri])
                    cid += 1
        del bp, cp  # Force release of GDB edit locks

    log(f"  Inserted Bridge_P={bid}, Culvert_Pnt={cid} at river crossings")



# ===========================================================================
# STAGE 4 :: Megacity & Utilities (everything depends on the road network)
# ===========================================================================

ARC_CENTER: Tuple[float, float] = PLAIN_CENTER
ARC_RADIUS: float = 800.0
ARC_START_ANGLE: float = math.radians(35.0)
ARC_END_ANGLE: float = ARC_START_ANGLE + (1000.0 / ARC_RADIUS)


def _arc_point(t: float) -> Tuple[float, float]:
    a = lerp(ARC_START_ANGLE, ARC_END_ANGLE, t)
    return (ARC_CENTER[0] + ARC_RADIUS * math.cos(a),
            ARC_CENTER[1] + ARC_RADIUS * math.sin(a))


def _truecurve_polyline(start_xy, end_xy, mid_xy) -> arcpy.Polyline:
    payload = {
        "hasZ": False,
        "hasM": False,
        "curvePaths": [[
            [start_xy[0], start_xy[1]],
            {"c": [[end_xy[0], end_xy[1]], [mid_xy[0], mid_xy[1]]]},
        ]],
        "spatialReference": {"wkid": PROJECTED_WKID},
    }
    return arcpy.AsShape(payload, True)


def _parallel_polyline(coords: Sequence[Tuple[float, float]],
                       offset: float) -> List[Tuple[float, float]]:
    """Polyline parallel to *coords*, offset perpendicular to the centerline."""
    if len(coords) < 2:
        return list(coords)
    out: List[Tuple[float, float]] = []
    for i in range(len(coords)):
        if i == 0:
            dx = coords[1][0] - coords[0][0]
            dy = coords[1][1] - coords[0][1]
        elif i == len(coords) - 1:
            dx = coords[-1][0] - coords[-2][0]
            dy = coords[-1][1] - coords[-2][1]
        else:
            dx = coords[i + 1][0] - coords[i - 1][0]
            dy = coords[i + 1][1] - coords[i - 1][1]
        mag = math.hypot(dx, dy) or 1.0
        nx = -dy / mag
        ny = dx / mag
        out.append((coords[i][0] + nx * offset,
                    coords[i][1] + ny * offset))
    return out


def _point_to_segment_dist2(px: float, py: float,
                            a: Tuple[float, float],
                            b: Tuple[float, float]) -> float:
    ax, ay = a
    bx, by = b
    dx = bx - ax
    dy = by - ay
    if dx == 0.0 and dy == 0.0:
        return (px - ax) ** 2 + (py - ay) ** 2
    t = ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)
    t = clamp(t, 0.0, 1.0)
    qx = ax + t * dx
    qy = ay + t * dy
    return (px - qx) ** 2 + (py - qy) ** 2


def _min_dist_to_polyline(px: float, py: float,
                          coords: Sequence[Tuple[float, float]]) -> float:
    if len(coords) < 2:
        return float("inf")
    d2_best = float("inf")
    for i in range(len(coords) - 1):
        d2 = _point_to_segment_dist2(px, py, coords[i], coords[i + 1])
        if d2 < d2_best:
            d2_best = d2
    return math.sqrt(d2_best)


def build_stage4_megacity_and_utilities(gdb: str) -> None:
    log("[Stage 4] Building Buildings, Gas_Pipes, Power_Lines, Trees, "
        "Highways_TrueCurve")

    bldg_fc = create_fc(
        "Buildings", "POLYGON",
        fields=(
            ("BldgID", "LONG", 0),
            ("Category", "TEXT", 24),
            ("HeightM", "FLOAT", 0),
        ),
    )
    # v3: rename to Gas_Pipe_L / Power_Line_L per the project map sample.
    gas_fc = create_fc(
        "Gas_Pipe_L", "POLYLINE",
        fields=(("Diameter_in", "SHORT", 0),
                ("PipeID", "LONG", 0),
                ("ShiftMeters", "FLOAT", 0),
                ("Profile", "TEXT", 24)),
    )
    power_fc = create_fc(
        "Power_Line_L", "POLYLINE",
        fields=(("Voltage_kV", "SHORT", 0),
                ("LineID", "LONG", 0),
                ("Profile", "TEXT", 24),
                ("CrossAngleDeg", "FLOAT", 0)),
    )
    trees_fc = create_fc(
        "Trees", "POINT",
        fields=(("TreeID", "LONG", 0), ("Species", "TEXT", 24)),
    )
    highway_fc = create_fc(
        "Highways_TrueCurve", "POLYLINE",
        fields=(
            ("HwyID", "LONG", 0),
            ("Designation", "TEXT", 24),
            ("HasTrueCurve", "SHORT", 0),
        ),
    )

    seed_roads = sorted(ROAD_PATHS, key=lambda rp: len(rp[0]), reverse=True)
    seed_roads = seed_roads[:max(1, int(len(seed_roads) * 0.6))]
    if not seed_roads:
        warn("No road paths available for building seeding!")
        return

    # ---- Buildings -- clustered along roads, plain only ------------------
    log(f"  Inserting {BUILDINGS_TARGET:,} buildings clustered along roads "
        f"(plain only, rejecting river/road conflicts)")

    bld_id = 0
    inserted = 0
    attempts = 0
    max_attempts = BUILDINGS_TARGET * 8
    river_reject_dist2 = 30.0 ** 2
    road_reject_dist2 = 6.0 ** 2

    cats = ("RESIDENTIAL", "COMMERCIAL", "INDUSTRIAL", "MIXED")

    with arcpy.da.InsertCursor(
            bldg_fc, ["SHAPE@", "BldgID", "Category", "HeightM"]) as cur:
        while inserted < BUILDINGS_TARGET and attempts < max_attempts:
            attempts += 1
            road_coords, _cls = random.choice(seed_roads)
            i = random.randint(0, len(road_coords) - 2)
            a = road_coords[i]
            b = road_coords[i + 1]
            t = random.random()
            cx_road = lerp(a[0], b[0], t)
            cy_road = lerp(a[1], b[1], t)
            dx = b[0] - a[0]
            dy = b[1] - a[1]
            mag = math.hypot(dx, dy) or 1.0
            nx = -dy / mag
            ny = dx / mag
            side = random.choice((-1.0, 1.0))
            off = side * random.uniform(8.0, BUILDING_BAND_WIDTH)
            cx = cx_road + nx * off
            cy = cy_road + ny * off

            if not _is_plain(cx, cy):
                continue
            if _min_dist_to_polyline(cx, cy, road_coords) ** 2 < road_reject_dist2:
                continue
            too_close_to_river = False
            for river in RIVER_PATHS:
                if _min_dist_to_polyline(cx, cy, river) ** 2 < river_reject_dist2:
                    too_close_to_river = True
                    break
            if too_close_to_river:
                continue

            w = random.uniform(8.0, 22.0)
            h = random.uniform(8.0, 22.0)
            ang = math.atan2(dy, dx)  # align with road
            ca, sa = math.cos(ang), math.sin(ang)
            corners_local = [(-w / 2, -h / 2), (w / 2, -h / 2),
                             (w / 2, h / 2), (-w / 2, h / 2)]
            ring = [(cx + lx * ca - ly * sa, cy + lx * sa + ly * ca)
                    for lx, ly in corners_local]
            cur.insertRow([
                make_polygon([ring]),
                bld_id,
                random.choice(cats),
                random.uniform(4.0, 35.0),
            ])
            bld_id += 1
            inserted += 1
        del cur  # Force release of GDB edit lock

    log(f"  Inserted {inserted:,} along-road buildings after "
        f"{attempts:,} attempts")

    # ---- Gas_Pipe_L -- tangent overlap with road edges (P02 nightmare) --
    # Pipes are co-located with the road centerline, shifted by exactly
    # 0.0 to 0.5 m -- close enough that they appear to *overlap* the road
    # edge in any sane symbology, which is the precise stress condition
    # Plugin 02 tries to detect.
    log("  Inserting Gas_Pipe_L (0.0-0.5 m tangent overlap of major roads)")
    pipe_id = 0
    with arcpy.da.InsertCursor(
            gas_fc, ["SHAPE@", "Diameter_in", "PipeID",
                     "ShiftMeters", "Profile"]) as cur:
        for coords, cls in ROAD_PATHS:
            if cls not in ("PRIMARY", "SECONDARY", "FREEWAY", "HIGHWAY"):
                continue
            shift = random.uniform(0.0, 0.5)
            parallel = _parallel_polyline(coords, shift)
            # Plain polyline is sufficient for tangent overlap; the
            # micro-shift creates a near-coincident geometry that *should*
            # be flagged as a deconfliction problem by Plugin 02.
            cur.insertRow([
                make_polyline(parallel),
                random.choice([8, 12, 16, 24, 36]),
                pipe_id, shift,
                "TANGENT_OVERLAP",
            ])
            pipe_id += 1

        # A separate set of "true-curve mirror" pipes attached to the
        # Highway True-Curve shapes -- these are emitted as Pro JSON
        # circular arcs offset by sub-meter, so even arc-aware
        # deconfliction must catch them.
        primaries_for_arc = [c for c, cls in ROAD_PATHS
                             if cls in ("FREEWAY", "HIGHWAY")][:8]
        for coords in primaries_for_arc:
            if len(coords) < 6:
                continue
            shift = random.uniform(0.0, 0.5)
            offset = _parallel_polyline(coords, shift)
            try:
                tc = _truecurve_polyline(
                    offset[0], offset[-1], offset[len(offset) // 2])
                cur.insertRow([
                    tc, random.choice([24, 36]),
                    pipe_id, shift, "TRUECURVE_TANGENT",
                ])
                pipe_id += 1
            except Exception:
                pass
        del cur  # Force release of GDB edit lock
    log(f"  Inserted {pipe_id} Gas_Pipe_L features (tangent + true-curve)")

    # ---- Power_Line_L -- zig-zag crossing True-Curve highways at 5-10 deg
    log("  Inserting Power_Line_L (5-10 deg acute zig-zags across "
        "True-Curve highways)")
    line_id = 0
    primaries_only = [c for c, cls in ROAD_PATHS
                      if cls in ("PRIMARY", "FREEWAY", "HIGHWAY")]
    n_conflicts = min(POWER_LINE_CONFLICT_ZONES, len(primaries_only))
    conflict_indices = (set(random.sample(range(len(primaries_only)),
                                          n_conflicts))
                        if primaries_only else set())
    with arcpy.da.InsertCursor(
            power_fc,
            ["SHAPE@", "Voltage_kV", "LineID", "Profile", "CrossAngleDeg"]) as cur:
        # Baseline: long parallel power lines on the *opposite* side from
        # the gas pipe, with a healthy clearance.
        for coords, cls in ROAD_PATHS:
            if cls not in ("PRIMARY", "SECONDARY", "FREEWAY", "HIGHWAY"):
                continue
            parallel = _parallel_polyline(coords, -POWER_LINE_OFFSET)
            cur.insertRow([
                make_polyline(parallel),
                random.choice([69, 115, 230, 345]),
                line_id, "PARALLEL", 0.0,
            ])
            line_id += 1

        # Acute-angle conflict zones: extremely shallow (5-10 deg)
        # zig-zag transmission lines that thread along, and repeatedly
        # *brush against*, the sweeping True-Curve highway.
        for k, coords in enumerate(primaries_only):
            if k not in conflict_indices or len(coords) < 12:
                continue
            cross_deg = random.uniform(5.0, 10.0)
            mid_idx = len(coords) // 2
            mid = coords[mid_idx]
            nxt = coords[mid_idx + 1]
            ang_road = math.atan2(nxt[1] - mid[1], nxt[0] - mid[0])
            # Build a zig-zag that travels broadly in the *same* direction
            # as the road but flips back and forth across it at 5-10 deg.
            ang_pos = ang_road + math.radians(cross_deg)
            ang_neg = ang_road - math.radians(cross_deg)
            leg_len = 350.0
            n_legs = 14
            zig: List[Tuple[float, float]] = []
            x, y = (mid[0] - 0.5 * n_legs * leg_len * math.cos(ang_road),
                    mid[1] - 0.5 * n_legs * leg_len * math.sin(ang_road))
            zig.append((x, y))
            for leg in range(n_legs):
                a = ang_pos if leg % 2 == 0 else ang_neg
                steps = 12
                for s in range(1, steps + 1):
                    t = s / steps
                    nx_ = x + leg_len * t * math.cos(a)
                    ny_ = y + leg_len * t * math.sin(a)
                    zig.append((nx_, ny_))
                x = zig[-1][0]
                y = zig[-1][1]
            cur.insertRow([
                make_polyline(zig),
                500, line_id, "ACUTE_ZIGZAG", cross_deg,
            ])
            line_id += 1
        del cur  # Force release of GDB edit lock
    log(f"  Inserted {line_id} Power_Line_L features "
        f"({n_conflicts} acute-angle conflict zones)")

    # ---- True-Curve highway interchanges --------------------------------
    log("  Inserting True-Curve highway interchanges (test arc + ramps)")
    hwy_id = 0
    with arcpy.da.InsertCursor(
            highway_fc, ["SHAPE@", "HwyID", "Designation", "HasTrueCurve"]) as cur:
        s = _arc_point(0.0)
        e = _arc_point(1.0)
        m = _arc_point(0.5)
        cur.insertRow([
            _truecurve_polyline(s, e, m),
            hwy_id, "INTERCHANGE_TEST_ARC", 1,
        ])
        hwy_id += 1
        for _ in range(12):
            cx = clamp(PLAIN_CENTER[0] + random.uniform(-2500, 2500),
                       X_MIN + 500, X_MAX - 500)
            cy = clamp(PLAIN_CENTER[1] + random.uniform(-2500, 2500),
                       Y_MIN + 500, Y_MAX - 500)
            r = random.uniform(120, 400)
            a0 = random.uniform(0, 2 * math.pi)
            sweep = random.uniform(math.pi / 3, 2 * math.pi / 3)
            sp = (cx + r * math.cos(a0), cy + r * math.sin(a0))
            ep = (cx + r * math.cos(a0 + sweep),
                  cy + r * math.sin(a0 + sweep))
            mp = (cx + r * math.cos(a0 + sweep / 2),
                  cy + r * math.sin(a0 + sweep / 2))
            cur.insertRow([
                _truecurve_polyline(sp, ep, mp),
                hwy_id, "INTERCHANGE_RAMP", 1,
            ])
            hwy_id += 1
        del cur  # Force release of GDB edit lock

    # ---- Arc-band buildings (10k+ within 2-15 m of the test arc) --------
    log("  Adding arc-band buildings around the test arc "
        "(GenerateNearTable stress)")
    extra_arc_buildings = 10500
    with arcpy.da.InsertCursor(
            bldg_fc, ["SHAPE@", "BldgID", "Category", "HeightM"]) as cur:
        for _ in range(extra_arc_buildings):
            t = random.random()
            cx_arc, cy_arc = _arc_point(t)
            angle = lerp(ARC_START_ANGLE, ARC_END_ANGLE, t)
            side = random.choice((-1.0, 1.0))
            offset = side * random.uniform(2.0, 15.0)
            cx = cx_arc + math.cos(angle) * offset
            cy = cy_arc + math.sin(angle) * offset
            w = random.uniform(4, 12)
            h = random.uniform(4, 12)
            ring = [(cx - w / 2, cy - h / 2), (cx + w / 2, cy - h / 2),
                    (cx + w / 2, cy + h / 2), (cx - w / 2, cy + h / 2)]
            cur.insertRow([
                make_polygon([ring]),
                bld_id,
                "ARC_BAND",
                random.uniform(3.0, 18.0),
            ])
            bld_id += 1
        del cur  # Force release of GDB edit lock

    # ---- Trees: along arc band + foothills -------------------------------
    log("  Inserting Trees (arc band + foothills 1100-1400 m)")
    species = ("OAK", "PINE", "MAPLE", "JUNIPER", "COTTONWOOD")
    tid = 0
    with arcpy.da.InsertCursor(trees_fc, ["SHAPE@", "TreeID", "Species"]) as cur:
        for _ in range(4500):
            t = random.random()
            cx_arc, cy_arc = _arc_point(t)
            angle = lerp(ARC_START_ANGLE, ARC_END_ANGLE, t)
            side = random.choice((-1.0, 1.0))
            offset = side * random.uniform(2.0, 15.0)
            x = cx_arc + math.cos(angle) * offset
            y = cy_arc + math.sin(angle) * offset
            cur.insertRow([make_point(x, y), tid, random.choice(species)])
            tid += 1
        for _ in range(2500):
            x = random.uniform(X_MIN + 100, X_MAX - 100)
            y = random.uniform(Y_MIN + 100, Y_MAX - 100)
            z = get_elevation(x, y)
            if 1100 < z < 1400:
                cur.insertRow([make_point(x, y), tid, random.choice(species)])
                tid += 1
        del cur  # Force release of GDB edit lock
    log(f"  Inserted {tid} trees")



# ===========================================================================
# STAGE 5 :: Geological anomalies + cartographic edges
# ===========================================================================

# Fault Line: the elevation level along whose contour we anchor the 5
# perfectly collinear springs.
FAULT_LINE_TARGET_ELEVATION = 1280.0


def _find_steep_slope_segment(level: float,
                              min_slope: float = 0.18,
                              min_length: float = 800.0
                              ) -> Optional[List[Tuple[float, float]]]:
    """Return a polyline of the iso-level whose mean slope is steep and
    whose chord is long enough to host the Fault Line."""
    segs = _marching_squares_segments(level)
    polys = _stitch_segments(segs)
    best: Optional[List[Tuple[float, float]]] = None
    best_score = 0.0
    for poly in polys:
        if len(poly) < 4:
            continue
        sample_step = max(1, len(poly) // 12)
        slopes = [slope_magnitude(x, y) for x, y in poly[::sample_step]]
        if not slopes:
            continue
        mean_slope = sum(slopes) / len(slopes)
        if mean_slope < min_slope:
            continue
        chord = math.hypot(poly[-1][0] - poly[0][0],
                           poly[-1][1] - poly[0][1])
        if chord < min_length:
            continue
        score = mean_slope * chord
        if score > best_score:
            best_score = score
            best = poly
    return best


def build_stage5_anomalies_and_edges(gdb: str) -> None:
    log("[Stage 5] Building Spring_Pnt, Map_Frame, Custom_AOI, Label boxes")

    # --- Spring_Pnt -- v3: strict steep-slope placement only -------------
    # Plugin 06 derives "downhill" rotation from the local terrain
    # gradient. Springs must therefore live on real slopes, never on the
    # flat plain (where |grad E| -> 0 produces a degenerate rotation).
    springs_fc = create_fc(
        "Spring_Pnt", "POINT",
        fields=(
            ("SpringID", "LONG", 0),
            ("FlowGPM", "FLOAT", 0),
            ("Kind", "TEXT", 24),
            ("Elev_M", "DOUBLE", 0),
            ("SlopeMag", "DOUBLE", 0),
        ),
    )

    # Strict slope thresholds for v3 -- significantly tighter than v2.
    SLOPE_MIN_FOR_SPRING = 0.10  # |grad z| must exceed 10 cm/m

    sid = 0
    with arcpy.da.InsertCursor(
            springs_fc,
            ["SHAPE@", "SpringID", "FlowGPM", "Kind", "Elev_M",
             "SlopeMag"]) as cur:

        # Fault Line: 5 perfectly collinear springs along a steep slope
        # contour (preserved from v2 -- needed to provoke SVD singular).
        fault_poly = _find_steep_slope_segment(FAULT_LINE_TARGET_ELEVATION)
        if fault_poly is None:
            warn("Could not locate steep contour for Fault Line; "
                 "falling back to a synthetic line.")
            fault_a = (X_MIN + 0.30 * WIDTH, Y_MIN + 0.30 * HEIGHT)
            fault_b = (X_MIN + 0.40 * WIDTH, Y_MIN + 0.55 * HEIGHT)
        else:
            fault_a = fault_poly[len(fault_poly) // 4]
            fault_b = fault_poly[(3 * len(fault_poly)) // 4]
            chord = math.hypot(fault_b[0] - fault_a[0],
                               fault_b[1] - fault_a[1])
            log("  Fault Line anchored on steep slope contour at "
                f"~{FAULT_LINE_TARGET_ELEVATION:.0f} m, chord={chord:.1f} m")

        for k in range(5):
            t = k / 4.0
            x = lerp(fault_a[0], fault_b[0], t)
            y = lerp(fault_a[1], fault_b[1], t)
            z = get_elevation(x, y)
            sm = slope_magnitude(x, y)
            cur.insertRow([
                make_point(x, y), sid,
                random.uniform(2.0, 14.0),
                "FAULT_LINE", z, sm,
            ])
            sid += 1

        # Isolated Spring -- in a steep slope zone but topologically
        # disconnected from the contour cluster. Kept as Plugin 06's
        # "no-neighbour" edge case; v3 still demands a non-zero gradient.
        iso_xy: Optional[Tuple[float, float]] = None
        for _ in range(2000):
            xx = random.uniform(X_MIN + 200, X_MAX - 200)
            yy = random.uniform(Y_MIN + 200, Y_MAX - 200)
            if (slope_magnitude(xx, yy) >= SLOPE_MIN_FOR_SPRING and
                    1500.0 < get_elevation(xx, yy) < 1800.0):
                iso_xy = (xx, yy)
                break
        if iso_xy is None:
            iso_xy = (MT_TITAN_CX + 1200.0, MT_TITAN_CY + 800.0)
        sm_iso = slope_magnitude(*iso_xy)
        cur.insertRow([
            make_point(*iso_xy), sid, 0.6, "ISOLATED",
            get_elevation(*iso_xy), sm_iso,
        ])
        sid += 1

        # Ambient steep-slope springs (v3 strict gating).
        log(f"  Seeding ambient springs strictly on steep slopes "
            f"(|grad z| >= {SLOPE_MIN_FOR_SPRING:.2f}, elev in "
            f"[{SPRING_BASE_ELEV_LO:.0f}, {SPRING_BASE_ELEV_HI:.0f}] m)")
        ambient = 0
        attempts = 0
        max_attempts = SPRINGS_BASE_COUNT * 60
        while ambient < SPRINGS_BASE_COUNT and attempts < max_attempts:
            attempts += 1
            x = random.uniform(X_MIN + 80, X_MAX - 80)
            y = random.uniform(Y_MIN + 80, Y_MAX - 80)
            z = get_elevation(x, y)
            if not (SPRING_BASE_ELEV_LO <= z <= SPRING_BASE_ELEV_HI):
                continue
            sm = slope_magnitude(x, y)
            if sm < SLOPE_MIN_FOR_SPRING:
                continue
            cur.insertRow([
                make_point(x, y), sid,
                random.uniform(0.8, 28.0),
                "BASE", z, sm,
            ])
            sid += 1
            ambient += 1
        log(f"  Inserted {ambient} ambient steep-slope springs after "
            f"{attempts} attempts")
        del cur  # Force release of GDB edit lock

    # --- Map_Frame --------------------------------------------------------
    frame_fc = create_fc(
        "Map_Frame", "POLYGON",
        fields=(("FrameName", "TEXT", 32),),
    )
    frame_ring = [(X_MIN, Y_MIN), (X_MAX, Y_MIN),
                  (X_MAX, Y_MAX), (X_MIN, Y_MAX)]
    with arcpy.da.InsertCursor(frame_fc, ["SHAPE@", "FrameName"]) as cur:
        cur.insertRow([make_polygon([frame_ring]), "TITAN_FRAME"])
        del cur  # Force release of GDB edit lock

    # --- Custom_AOI: sawtooth boundary biting into mountain contours -----
    aoi_fc = create_fc(
        "Custom_AOI", "POLYGON",
        fields=(("AOIName", "TEXT", 32), ("Notes", "TEXT", 128)),
    )
    log("  Constructing Custom_AOI with sawtooth boundary "
        "(sub-decimeter slivers vs. mountain contours)")
    aoi_cx, aoi_cy = MT_TITAN_CX, MT_TITAN_CY
    aoi_radius = 2200.0
    teeth = 480  # high-frequency sawtooth -> many tiny slivers
    sawtooth: List[Tuple[float, float]] = []
    for k in range(teeth):
        a = 2 * math.pi * (k / teeth)
        # Microscopic radial wobble (< 0.1 m peak) -> sub-decimeter slivers.
        radial_wob = random.uniform(-0.08, 0.08)
        # Plus a small alternating zig (~0.5 m) so the sawtooth pattern is
        # also visible at human scale, in addition to the microscopic
        # slivers carried by `radial_wob`.
        zig = 0.5 if (k % 2 == 0) else -0.5
        r = aoi_radius + radial_wob + zig
        x = clamp(aoi_cx + r * math.cos(a), X_MIN + 1.0, X_MAX - 1.0)
        y = clamp(aoi_cy + r * math.sin(a), Y_MIN + 1.0, Y_MAX - 1.0)
        sawtooth.append((x, y))
    with arcpy.da.InsertCursor(aoi_fc, ["SHAPE@", "AOIName", "Notes"]) as cur:
        cur.insertRow([
            make_polygon([sawtooth]),
            "TITAN_AOI",
            "Sawtooth boundary on Mt. Titan flank -> <0.1 m clip slivers",
        ])
        del cur  # Force release of GDB edit lock

    # --- Label_Candidate_Boxes (Plugin 03/04 AABB stress) ----------------
    label_fc = create_fc(
        "Label_Candidate_Boxes", "POLYGON",
        fields=(
            ("LabelID", "LONG", 0),
            ("LabelText", "TEXT", 64),
            ("Priority", "SHORT", 0),
            ("ClusterKind", "TEXT", 16),
        ),
    )
    log(f"  Inserting {LABEL_BOXES:,} overlapping label candidate boxes "
        "(clustered on contour vertices + V-apex stress points)")

    # Cluster centers sampled from contour vertices: the AABB stress is also
    # topologically tied to the terrain.
    cluster_centers: List[Tuple[float, float]] = []
    levels = [PLAIN_MAX_ELEV + 50.0, 1200.0, 1350.0, 1500.0]
    for lvl in levels:
        polys = _stitch_segments(_marching_squares_segments(lvl))
        for poly in polys:
            if len(poly) >= 6:
                cluster_centers.append(poly[len(poly) // 2])
    if not cluster_centers:
        cluster_centers.append(PLAIN_CENTER)

    # ----- V-apex hot spots (P03/P04 nightmare) -------------------------
    # Detect contour vertices whose interior angle is < 60 deg -- those
    # are the apexes of valley/creek "V" shapes. Pack many label boxes
    # exactly on top of each one to overload collision detection.
    log("  Detecting V-apex contour vertices for label cluster overload")
    v_apex_centers: List[Tuple[float, float]] = []
    apex_levels = [1100.0, 1180.0, 1260.0, 1340.0, 1420.0, 1500.0, 1580.0]
    for lvl in apex_levels:
        polys = _stitch_segments(_marching_squares_segments(lvl))
        for poly in polys:
            if len(poly) < 5:
                continue
            for i in range(2, len(poly) - 2):
                p0 = poly[i - 2]
                p1 = poly[i]
                p2 = poly[i + 2]
                v1x = p0[0] - p1[0]
                v1y = p0[1] - p1[1]
                v2x = p2[0] - p1[0]
                v2y = p2[1] - p1[1]
                m1 = math.hypot(v1x, v1y)
                m2 = math.hypot(v2x, v2y)
                if m1 < 1e-6 or m2 < 1e-6:
                    continue
                cos_int = max(-1.0, min(1.0,
                                        (v1x * v2x + v1y * v2y) / (m1 * m2)))
                ang_int = math.degrees(math.acos(cos_int))
                if ang_int < 60.0:  # tight V apex
                    v_apex_centers.append(p1)
    log(f"  Found {len(v_apex_centers):,} V-apex hot spots")

    if not v_apex_centers:
        v_apex_centers = list(cluster_centers)

    n_apex_boxes = LABEL_BOXES // 2  # half the labels stack on V-apexes
    n_general = LABEL_BOXES - n_apex_boxes

    with arcpy.da.InsertCursor(
            label_fc,
            ["SHAPE@", "LabelID", "LabelText", "Priority",
             "ClusterKind"]) as cur:
        # General contour-vertex clusters (kept from v2, slightly shrunk).
        for k in range(n_general):
            cx_, cy_ = random.choice(cluster_centers)
            cx_ += random.uniform(-50, 50)
            cy_ += random.uniform(-50, 50)
            w = random.uniform(6, 40)
            h = random.uniform(3, 12)
            ring = [(cx_ - w / 2, cy_ - h / 2),
                    (cx_ + w / 2, cy_ - h / 2),
                    (cx_ + w / 2, cy_ + h / 2),
                    (cx_ - w / 2, cy_ + h / 2)]
            cur.insertRow([
                make_polygon([ring]),
                k,
                f"LBL_{k:06d}",
                random.randint(1, 9),
                "CONTOUR",
            ])

        # V-apex stress clusters: piles of small AABBs at each apex.
        for k in range(n_apex_boxes):
            apex_id = k % len(v_apex_centers)
            cx_, cy_ = v_apex_centers[apex_id]
            cx_ += random.uniform(-6, 6)   # very tight pile around apex
            cy_ += random.uniform(-6, 6)
            w = random.uniform(8, 28)
            h = random.uniform(4, 10)
            ring = [(cx_ - w / 2, cy_ - h / 2),
                    (cx_ + w / 2, cy_ - h / 2),
                    (cx_ + w / 2, cy_ + h / 2),
                    (cx_ - w / 2, cy_ + h / 2)]
            cur.insertRow([
                make_polygon([ring]),
                n_general + k,
                f"VAPEX_{n_general + k:06d}",
                random.randint(7, 9),  # apex labels are high priority
                "VAPEX",
            ])
        del cur  # Force release of GDB edit lock


# ===========================================================================
# Plugin 07 :: Index_Grid + GCS variant + Huge extent driver
# ===========================================================================

def _grid_cells(x_min: float, y_min: float, x_max: float, y_max: float,
                nx: int, ny: int
                ) -> Iterable[Tuple[int, int, List[Tuple[float, float]]]]:
    dx = (x_max - x_min) / nx
    dy = (y_max - y_min) / ny
    for i in range(nx):
        for j in range(ny):
            x0 = x_min + i * dx
            x1 = x0 + dx
            y0 = y_min + j * dy
            y1 = y0 + dy
            ring = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
            yield i, j, ring


def build_p07_index_grid(gdb: str) -> None:
    log("[P07] Building Index_Sheet block (16-24 sheets) + Index_Grid + GCS "
        "+ Huge_Grid_Extent")

    # ----- v3 Index_Sheet: contiguous block of 16-24 polygon sheets ------
    # The grid builder plugin's "happy path" is producing a small
    # contiguous index of named map sheets. We pick a 5x4 = 20 layout
    # by default; the count parameter is biased to land in [16, 24].
    sheet_fc = create_fc(
        "Index_Sheet", "POLYGON",
        fields=(
            ("SheetID", "LONG", 0),
            ("Col", "LONG", 0),
            ("Row", "LONG", 0),
            ("SheetName", "TEXT", 32),
        ),
        sr=SR_PROJECTED,
    )
    sheet_layouts = [(4, 4), (4, 5), (5, 4), (5, 5), (6, 3),
                     (3, 6), (4, 6), (6, 4)]
    sheet_layouts = [
        (nx, ny) for nx, ny in sheet_layouts if 16 <= nx * ny <= 24
    ]
    sheet_nx, sheet_ny = random.choice(sheet_layouts)
    # Pad inwards from the AOI so the sheet block is clearly contiguous
    # but does not touch the AOI border.
    pad = 1500.0
    sx0 = X_MIN + pad
    sy0 = Y_MIN + pad
    sx1 = X_MAX - pad
    sy1 = Y_MAX - pad
    log(f"  Sheet block layout: {sheet_nx} cols x {sheet_ny} rows = "
        f"{sheet_nx * sheet_ny} sheets")
    sheet_id = 0
    with arcpy.da.InsertCursor(
            sheet_fc,
            ["SHAPE@", "SheetID", "Col", "Row", "SheetName"]) as cur:
        for i, j, ring in _grid_cells(sx0, sy0, sx1, sy1,
                                      sheet_nx, sheet_ny):
            cur.insertRow([
                make_polygon([ring], sr=SR_PROJECTED),
                sheet_id, i, j,
                f"Sheet_R{j:02d}C{i:02d}",
            ])
            sheet_id += 1
        del cur  # Force release of GDB edit lock

    grid_fc = create_fc(
        "Index_Grid", "POLYGON",
        fields=(
            ("CellID", "LONG", 0),
            ("Col", "LONG", 0),
            ("Row", "LONG", 0),
            ("Label", "TEXT", 24),
        ),
        sr=SR_PROJECTED,
    )
    cells = INDEX_GRID_CELLS_PER_AXIS
    log(f"  Inserting projected grid: {cells} x {cells} = "
        f"{cells*cells:,} cells")
    cell_id = 0
    with arcpy.da.InsertCursor(
            grid_fc, ["SHAPE@", "CellID", "Col", "Row", "Label"]) as cur:
        for i, j, ring in _grid_cells(X_MIN, Y_MIN, X_MAX, Y_MAX, cells, cells):
            cur.insertRow([
                make_polygon([ring], sr=SR_PROJECTED),
                cell_id, i, j, f"R{j:04d}C{i:04d}",
            ])
            cell_id += 1
        del cur  # Force release of GDB edit lock

    grid_gcs_fc = create_fc(
        "Index_Grid_GCS", "POLYGON",
        fields=(
            ("CellID", "LONG", 0),
            ("Col", "LONG", 0),
            ("Row", "LONG", 0),
        ),
        sr=SR_GEOGRAPHIC,
    )
    gcs_xmin, gcs_ymin = -116.20, 35.20
    gcs_xmax, gcs_ymax = -116.00, 35.40
    n = INDEX_GRID_GCS_CELLS_PER_AXIS
    log(f"  Inserting GCS_WGS_1984 grid: {n} x {n} = {n*n:,} cells "
        "(triggers Plugin 07's GCS warning)")
    cid = 0
    with arcpy.da.InsertCursor(
            grid_gcs_fc, ["SHAPE@", "CellID", "Col", "Row"]) as cur:
        for i, j, ring in _grid_cells(gcs_xmin, gcs_ymin,
                                      gcs_xmax, gcs_ymax, n, n):
            cur.insertRow([
                make_polygon([ring], sr=SR_GEOGRAPHIC),
                cid, i, j,
            ])
            cid += 1
        del cur  # Force release of GDB edit lock

    huge_fc = create_fc(
        "Huge_Grid_Extent", "POLYGON",
        fields=(
            ("Name", "TEXT", 32),
            ("RequestedTicksX", "LONG", 0),
            ("RequestedTicksY", "LONG", 0),
            ("Notes", "TEXT", 128),
        ),
        sr=SR_PROJECTED,
    )
    huge_ring = [(X_MIN, Y_MIN), (X_MAX, Y_MIN),
                 (X_MAX, Y_MAX), (X_MIN, Y_MAX)]
    with arcpy.da.InsertCursor(
            huge_fc,
            ["SHAPE@", "Name", "RequestedTicksX", "RequestedTicksY", "Notes"]) as cur:
        cur.insertRow([
            make_polygon([huge_ring]),
            "TITAN_HUGE_EXTENT",
            HUGE_GRID_TICKS, HUGE_GRID_TICKS,
            "Drives MAX_TICKS_PER_AXIS guard in Plugin07",
        ])
        del cur  # Force release of GDB edit lock


# ===========================================================================
# Orchestration
# ===========================================================================

def _summarise(gdb: str) -> None:
    arcpy.env.workspace = gdb
    log("--- TitanWorld_Pro.gdb summary ---")
    for fc in sorted(arcpy.ListFeatureClasses() or []):
        try:
            n = int(arcpy.management.GetCount(fc).getOutput(0))
        except Exception:
            n = -1
        log(f"  {fc:30s} : {n:>10,} features")


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Generate the TitanWorld_Pro 3.0 stress-test GDB.",
    )
    p.add_argument(
        "--out",
        default=os.getcwd(),
        help="Directory in which to create TitanWorld_Pro.gdb (default: cwd).",
    )
    p.add_argument(
        "--skip",
        nargs="*",
        default=[],
        choices=["S1", "S2", "S3", "S4", "S5", "P07"],
        help="Optional list of pipeline stages to skip.",
    )
    return p.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    log("=" * 72)
    log(" TitanWorld_Pro 3.0 :: Topologically Logical Pipeline + "
        "named-class layers + V-apex labels")
    log(f" Random seed     : {RANDOM_SEED}")
    log(f" AOI extent      : X[{X_MIN}, {X_MAX}]  Y[{Y_MIN}, {Y_MAX}]  "
        f"({WIDTH/1000:.1f} km x {HEIGHT/1000:.1f} km)")
    log(f" Projected SR    : EPSG:{PROJECTED_WKID}  ({SR_PROJECTED.name})")
    log(f" Output workspace: {args.out}")
    log("=" * 72)

    gdb = build_gdb(args.out)

    if "S1" not in args.skip:
        build_stage1_terrain(gdb)
    if "S2" not in args.skip:
        build_stage2_hydrology(gdb)
    if "S3" not in args.skip:
        build_stage3_roads(gdb)
    if "S4" not in args.skip:
        build_stage4_megacity_and_utilities(gdb)
    if "S5" not in args.skip:
        build_stage5_anomalies_and_edges(gdb)
    if "P07" not in args.skip:
        build_p07_index_grid(gdb)

    _summarise(gdb)
    log("DONE.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
