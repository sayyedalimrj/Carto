# -*- coding: utf-8 -*-
"""
Generate_TitanWorld_ArcMap.py
=============================

TITAN WORLD 3.0 - Topologically Dependent Procedural Generator
(ArcMap 10.x / Python 2.7 / arcpy)

Built on the v2 topology pipeline, the v3 enhancements inject
real-world cartographic challenges drawn from the project proposal:

  STAGE 2  Hydrology now emits explicit sub-classes:
           River_L (perennial), Seasonal_River_L, Abreez (mountain
           rills), Canal (man-made plain channels).  All carry a
           HydroClass field.  Lines are densified with Catmull-Rom
           splines so they meander organically.

  STAGE 3  Roads emit explicit sub-classes via the RoadClass field:
           Freeway, Highway, Parkway, Asphalt_Road, Gravel.  Trunks
           are spline-densified, so they curve organically and cross
           rivers at varying angles.  Bridge_P (highway-class) and
           Culvert_Pnt (lower-class) point feature classes are
           generated EXACTLY at every road/river crossing so a
           downstream rotation tool has anchors.

  STAGE 4  Gas_Pipe_L lays a tangent overlay along meandering trunks
           by shifting each road vertex perpendicular to the local
           tangent by 0.0 .. 0.5 m, so pipes run on the road edge.
           Power_Line_L generates long zig-zag lines crossing
           Highway-class trunks at 5..10 deg deviations.
           Label_Candidate_Boxes are clustered on contour V-apex
           points (where contours form tight valley creeks).

  STAGE 5  Spring_Pnt features must be on STEEP slopes (slope >
           SPRING_SLOPE_MIN), never on plains or peak summits.  The
           5 perfectly collinear springs still ride a steep slope
           contour for the SVD failure test.

  STAGE 5b Index_Grid emits a contiguous block of 16..24 sheets
           (default 5 x 4 = 20 sheets) with TW-A1..TW-Ex sheet codes.

The script remains self-contained: only ``arcpy``, ``math``, ``os``,
``sys``, ``time``, and ``random``.

Usage::

    C:\\Python27\\ArcGIS10.8\\python.exe Generate_TitanWorld_ArcMap.py [out_dir]

If ``out_dir`` is omitted the gdb is written next to the script.
"""

import os
import sys
import math
import time
import random

try:
    import arcpy
except ImportError:  # pragma: no cover - this script targets ArcMap only
    sys.stderr.write(
        "arcpy is not available. Run this script from an ArcMap 10.x "
        "Python 2.7 environment.\n"
    )
    raise


# ---------------------------------------------------------------------------
# Global configuration
# ---------------------------------------------------------------------------

RANDOM_SEED = 20260527
random.seed(RANDOM_SEED)

# 20 km x 20 km projected bounding box (UTM-style coordinates).
XMIN = 500000.0
YMIN = 3900000.0
XMAX = 520000.0
YMAX = 3920000.0
WIDTH = XMAX - XMIN     # 20000 m
HEIGHT = YMAX - YMIN    # 20000 m

# Projected CS: WGS 1984 UTM Zone 11N (WKID 32611).  Geographic CS: WGS 84.
PROJECTED_WKID = 32611
GEOGRAPHIC_WKID = 4326

GDB_NAME = "TitanWorld_ArcMap.gdb"

# ----- Terrain (the Oracle) -----
# Elevation range targeted: ~ 800 .. 2400 m.
ELEV_BASE = 1500.0
ELEV_AMPLITUDE = 800.0       # peak vertical scale per dominant octave
ELEV_PLAIN_LEVEL = 1450.0    # the central plain elevation
ELEV_PLAIN_BAND = 80.0       # +/- band considered "flat plain"
ELEV_MOUNTAIN_FOOT = 1700.0  # spring band lower bound
ELEV_MOUNTAIN_BAND = 200.0   # spring band width

# Designated mountain peak centres (used for the Titan Ridge anchoring
# and for elevation amplification).  Coordinates are normalized 0..1
# inside the bbox.
PEAKS_NORM = [
    (0.18, 0.78, 1.00),  # NW peak (Titan Ridge anchor)
    (0.85, 0.82, 0.80),  # NE peak
    (0.82, 0.18, 0.85),  # SE peak
    (0.15, 0.20, 0.75),  # SW peak
]

# Central plain (flat, plain elevation, hosts the megacity)
PLAIN_CENTER_NORM = (0.50, 0.50)
PLAIN_RADIUS_M = 4500.0      # plain "radius" for soft mask

# ----- Layer counts -----
N_ELEVATION_POINTS = 6000
N_CONTOUR_LEVELS = 22                # number of distinct elevation bands
N_CONTOURS_PER_LEVEL_MAX = 80        # cap per level to keep total bounded
TITAN_RIDGE_VERTICES = 500000        # half-million-vertex stress feature

# Hydrology sub-classes (Stage 2 enhanced for P01 challenges).
# Names mirror the cartographic deliverable layers.
N_RIVERS_PERENNIAL = 50              # River_L
N_RIVERS_SEASONAL = 20               # Seasonal_River_L
N_ABREEZ = 12                        # Abreez (mountain rills)
N_CANALS = 6                         # Canal (man-made, in plain)
N_RIVERS = N_RIVERS_PERENNIAL + N_RIVERS_SEASONAL + N_ABREEZ + N_CANALS

# Road sub-classes (Stage 3 enhanced for P01 challenges).
N_FREEWAY = 4                        # Freeway (longest, gentlest curves)
N_HIGHWAY = 10                       # Highway
N_PARKWAY = 8                        # Parkway
N_ASPHALT = 18                       # Asphalt_Road
N_GRAVEL = 30                        # Gravel
N_ROAD_TRUNK_LINES = (N_FREEWAY + N_HIGHWAY + N_PARKWAY +
                      N_ASPHALT + N_GRAVEL)
N_ROAD_LOCAL_PER_TRUNK = 35          # local connectors hanging off trunks

# Spline density: 25..40 vertices per 1 km of road (organic curves).
ROAD_VERTICES_PER_KM = 32

N_T_JUNCTIONS = 1500
N_COLLINEAR_OVERLAPS = 400

N_BUILDINGS_CLUSTER = 10500          # tight cluster along city road
N_BUILDINGS_BACKGROUND = 4000        # diffuse plain buildings

# Gas_Pipe_L: tangent-to-road overlay (0.0 .. 0.5 m offset).
N_GAS_PIPES_TANGENT = 700
N_GAS_PIPES_BACKGROUND = 800
GAS_TANGENT_OFFSET_MIN = 0.0
GAS_TANGENT_OFFSET_MAX = 0.5

# Power_Line_L: long zig-zag lines crossing highways at 5-10 deg.
N_POWER_LINES_PARALLEL = 600
N_POWER_LINES_ACUTE = 400
N_POWER_LINES_BACKGROUND = 200
POWER_ACUTE_DEG_MIN = 5.0
POWER_ACUTE_DEG_MAX = 10.0
POWER_ZIGZAG_SEGMENTS = 9            # zig-zag vertices per acute line

N_LABEL_BOXES = 12000
# Label boxes piled on contour V-apex points (P03/P04 stress).
N_LABEL_VAPEX_HOTSPOTS = 14

N_AOI_SAWTOOTH_TEETH = 4000

N_SPRINGS_RANDOM = 1800
N_SPRINGS_FAULT_LINE = 5             # collinear (SVD singular)
SPRING_SLOPE_MIN = 0.20              # only place springs where slope > this

# Grid stress: large enough to trip a MAX_TICKS_PER_AXIS cap (~5000).
HUGE_GRID_TICKS = 6000
HUGE_GRID_SAMPLE_LIMIT = 250000

# P07 contiguous Index_Grid: produce a block of 16..24 sheets.
INDEX_SHEET_BLOCK_COLS = 5           # 5 x 4 = 20 sheets (within 16..24)
INDEX_SHEET_BLOCK_ROWS = 4

GCS_GRID_ROWS = 20
GCS_GRID_COLS = 20


# ---------------------------------------------------------------------------
# Logging helpers
# ---------------------------------------------------------------------------

_T0 = time.time()


def log(msg):
    elapsed = time.time() - _T0
    line = "[{0:8.2f}s] {1}".format(elapsed, msg)
    try:
        arcpy.AddMessage(line)
    except Exception:
        pass
    print(line)


# ---------------------------------------------------------------------------
# Utility helpers
# ---------------------------------------------------------------------------

def clamp(v, lo, hi):
    if v < lo:
        return lo
    if v > hi:
        return hi
    return v


def jitter(amount):
    return (random.random() * 2.0 - 1.0) * amount


def make_polyline(coords, sr):
    arr = arcpy.Array([arcpy.Point(x, y) for (x, y) in coords])
    return arcpy.Polyline(arr, sr)


def make_polygon(coords, sr):
    arr = arcpy.Array([arcpy.Point(x, y) for (x, y) in coords])
    return arcpy.Polygon(arr, sr)


def make_point(x, y, sr):
    return arcpy.PointGeometry(arcpy.Point(x, y), sr)


def in_bbox(x, y, pad=0.0):
    return (XMIN + pad) <= x <= (XMAX - pad) and \
           (YMIN + pad) <= y <= (YMAX - pad)


# ---------------------------------------------------------------------------
# Spline densification helpers (Stage 3 / Stage 2 organic curves)
# ---------------------------------------------------------------------------

def _catmull_rom_segment(p0, p1, p2, p3, n):
    """Sample n points along the Catmull-Rom segment p1..p2 (inclusive of p1,
    exclusive of p2).  p0 and p3 are the surrounding control points used
    only to derive smooth tangents.
    """
    out = []
    for i in range(n):
        t = i / float(n)
        t2 = t * t
        t3 = t2 * t
        # Standard Catmull-Rom basis (uniform).
        x = 0.5 * (
            (2.0 * p1[0])
            + (-p0[0] + p2[0]) * t
            + (2.0 * p0[0] - 5.0 * p1[0] + 4.0 * p2[0] - p3[0]) * t2
            + (-p0[0] + 3.0 * p1[0] - 3.0 * p2[0] + p3[0]) * t3
        )
        y = 0.5 * (
            (2.0 * p1[1])
            + (-p0[1] + p2[1]) * t
            + (2.0 * p0[1] - 5.0 * p1[1] + 4.0 * p2[1] - p3[1]) * t2
            + (-p0[1] + 3.0 * p1[1] - 3.0 * p2[1] + p3[1]) * t3
        )
        out.append((x, y))
    return out


def densify_polyline_spline(coords, vertices_per_km=ROAD_VERTICES_PER_KM):
    """Catmull-Rom densify a polyline so it carries roughly
    ``vertices_per_km`` vertices per kilometre, producing organic curves
    instead of sharp polylines.  Endpoints are preserved.
    """
    if coords is None or len(coords) < 2:
        return list(coords) if coords else []
    # Mirror endpoints to give Catmull-Rom phantom controls.
    pts = [coords[0]] + list(coords) + [coords[-1]]
    out = []
    for i in range(1, len(pts) - 2):
        p0, p1, p2, p3 = pts[i - 1], pts[i], pts[i + 1], pts[i + 2]
        seg_len = math.hypot(p2[0] - p1[0], p2[1] - p1[1])
        # samples per segment proportional to length
        n = max(2, int((seg_len / 1000.0) * vertices_per_km))
        out.extend(_catmull_rom_segment(p0, p1, p2, p3, n))
    out.append(coords[-1])
    # Clamp to bbox to be safe.
    out = [(clamp(cx, XMIN + 1.0, XMAX - 1.0),
            clamp(cy, YMIN + 1.0, YMAX - 1.0)) for (cx, cy) in out]
    return out


# ---------------------------------------------------------------------------
# STAGE 1 - The Oracle: get_elevation(x, y) and helpers
# ---------------------------------------------------------------------------
#
# Pseudo-Perlin: a sum of sine/cosine octaves (deterministic, no numpy
# required).  We then add explicit Gaussian peaks at PEAKS_NORM and
# subtract a Gaussian "plain" depression centred on PLAIN_CENTER_NORM
# so the central area is genuinely flat.
# ---------------------------------------------------------------------------


def _norm_xy(x, y):
    return (x - XMIN) / WIDTH, (y - YMIN) / HEIGHT


def _gaussian2d(nx, ny, cx, cy, sigma):
    dx = nx - cx
    dy = ny - cy
    return math.exp(-(dx * dx + dy * dy) / (2.0 * sigma * sigma))


def get_elevation(x, y):
    """Deterministic compound elevation field over the bbox.

    Returns elevation in meters.  Stable for the same (x, y) across
    the whole script: every downstream stage reads from this oracle.
    """
    nx, ny = _norm_xy(x, y)

    # Base rolling terrain (5 octaves of compound sin/cos).
    base = (
        0.50 * math.sin(nx * 6.2831853 * 1.3 + 0.7)
        * math.cos(ny * 6.2831853 * 1.1 + 1.3)
        + 0.30 * math.sin(nx * 6.2831853 * 2.7 + 2.1)
        * math.cos(ny * 6.2831853 * 2.9 + 0.4)
        + 0.15 * math.sin(nx * 6.2831853 * 5.3 + 4.2)
        * math.cos(ny * 6.2831853 * 4.7 + 1.9)
        + 0.07 * math.sin(nx * 6.2831853 * 11.1 + 0.3)
        * math.cos(ny * 6.2831853 * 9.7 + 3.5)
        + 0.03 * math.sin(nx * 6.2831853 * 21.7)
        * math.cos(ny * 6.2831853 * 19.3)
    )

    # Mountain peaks: dominant Gaussian bumps above the base.
    peak_sum = 0.0
    for (pcx, pcy, pscale) in PEAKS_NORM:
        peak_sum += pscale * _gaussian2d(nx, ny, pcx, pcy, 0.10)

    # Central plain: a wide soft Gaussian "press-down" zeroing out the
    # base oscillation in the middle so the plain is truly flat.
    plain_g = _gaussian2d(nx, ny, PLAIN_CENTER_NORM[0],
                          PLAIN_CENTER_NORM[1], 0.20)

    # Combine.  Outside the plain, terrain ~= base + peaks.  Inside the
    # plain, the base is damped towards zero so we get ~ELEV_PLAIN_LEVEL.
    rough = base * (1.0 - 0.92 * plain_g)

    elev = (
        ELEV_BASE
        + ELEV_AMPLITUDE * rough
        + ELEV_AMPLITUDE * 1.20 * peak_sum
        - 0.05 * ELEV_AMPLITUDE * plain_g  # gentle dip toward plain level
    )
    return elev


def get_gradient(x, y, h=20.0):
    """Numerical gradient of the elevation field via central differences."""
    dz_dx = (get_elevation(x + h, y) - get_elevation(x - h, y)) / (2.0 * h)
    dz_dy = (get_elevation(x, y + h) - get_elevation(x, y - h)) / (2.0 * h)
    return dz_dx, dz_dy


def get_slope(x, y, h=20.0):
    dx, dy = get_gradient(x, y, h)
    return math.hypot(dx, dy)


def is_on_plain(x, y):
    """True if (x, y) is inside the flat central plain band."""
    e = get_elevation(x, y)
    if abs(e - ELEV_PLAIN_LEVEL) > ELEV_PLAIN_BAND:
        return False
    nx, ny = _norm_xy(x, y)
    plain_g = _gaussian2d(nx, ny, PLAIN_CENTER_NORM[0],
                          PLAIN_CENTER_NORM[1], 0.20)
    return plain_g > 0.45


def is_on_mountain_foot(x, y):
    """Spring-band membership."""
    e = get_elevation(x, y)
    return ELEV_MOUNTAIN_FOOT <= e <= (ELEV_MOUNTAIN_FOOT + ELEV_MOUNTAIN_BAND)


def find_peak_world_xy(idx):
    """Return the (x, y) of the idx-th designated peak in world coords."""
    pcx, pcy, _ = PEAKS_NORM[idx]
    return XMIN + pcx * WIDTH, YMIN + pcy * HEIGHT


# ---------------------------------------------------------------------------
# Geodatabase / SR setup
# ---------------------------------------------------------------------------


def resolve_output_dir():
    if len(sys.argv) >= 2 and sys.argv[1].strip():
        return os.path.abspath(sys.argv[1])
    return os.path.dirname(os.path.abspath(__file__))


def create_gdb(out_dir):
    if not os.path.isdir(out_dir):
        os.makedirs(out_dir)
    gdb_path = os.path.join(out_dir, GDB_NAME)
    if arcpy.Exists(gdb_path):
        log("Existing {0} found - deleting.".format(GDB_NAME))
        arcpy.Delete_management(gdb_path)
    log("Creating {0} in {1}".format(GDB_NAME, out_dir))
    arcpy.CreateFileGDB_management(out_dir, GDB_NAME)
    return gdb_path


def get_projected_sr():
    return arcpy.SpatialReference(PROJECTED_WKID)


def get_geographic_sr():
    return arcpy.SpatialReference(GEOGRAPHIC_WKID)


def create_fc(gdb, name, geom_type, sr, fields):
    arcpy.CreateFeatureclass_management(
        out_path=gdb,
        out_name=name,
        geometry_type=geom_type,
        spatial_reference=sr,
    )
    fc = os.path.join(gdb, name)
    for fname, ftype, flen in fields:
        if ftype == "TEXT":
            arcpy.AddField_management(fc, fname, ftype, field_length=flen or 64)
        else:
            arcpy.AddField_management(fc, fname, ftype)
    return fc



# ===========================================================================
# STAGE 1 - Elevation_Points, Contours, Titan Ridge
# ===========================================================================
#
# Elevation_Points: Halton-ish quasirandom samples across the bbox with
#   z = get_elevation(x, y).  Used by P03/P04 evaluation and for sanity
#   checking the surface.
#
# Contours: traced as polylines along iso-value bands of the elevation
#   field.  We use a coarse marching-grid approach: for each elevation
#   level, walk a perimeter sampling and connect adjacent grid cells
#   that straddle the level.  Output is intentionally noisy/jagged to
#   mimic real contour generators without depending on Spatial Analyst.
#
# Titan Ridge: a single 500k-vertex polyline placed AT the NW peak.
#   We use a fractal-perturbed logarithmic spiral, recentered on the
#   peak and scaled to fit just below the peak summit.
# ---------------------------------------------------------------------------


def build_stage1_terrain(gdb, sr):
    log("STAGE 1: Elevation_Points + Contours + Titan Ridge")

    # ------------------------------------------------------------------
    # Elevation_Points
    # ------------------------------------------------------------------
    elev_fc = create_fc(
        gdb, "Elevation_Points", "POINT", sr,
        [
            ("PointID", "LONG", None),
            ("Elevation", "DOUBLE", None),
            ("Slope", "DOUBLE", None),
            ("Zone", "TEXT", 32),
        ],
    )
    log("  sampling Elevation_Points ({0})...".format(N_ELEVATION_POINTS))
    with arcpy.da.InsertCursor(
            elev_fc,
            ["SHAPE@", "PointID", "Elevation", "Slope", "Zone"]) as ec:
        for i in range(N_ELEVATION_POINTS):
            x = XMIN + random.random() * WIDTH
            y = YMIN + random.random() * HEIGHT
            z = get_elevation(x, y)
            s = get_slope(x, y)
            if is_on_plain(x, y):
                zone = "Plain"
            elif z >= ELEV_BASE + 0.5 * ELEV_AMPLITUDE:
                zone = "Mountain"
            elif is_on_mountain_foot(x, y):
                zone = "MountainFoot"
            else:
                zone = "Hill"
            ec.insertRow([make_point(x, y, sr), i, z, s, zone])
        del ec

    # ------------------------------------------------------------------
    # Contours via a marching-segments pass over a coarse grid
    # ------------------------------------------------------------------
    contours_fc = create_fc(
        gdb, "Contours", "POLYLINE", sr,
        [
            ("ContourID", "LONG", None),
            ("Elevation", "DOUBLE", None),
            ("Index_Contour", "SHORT", None),
            ("EdgeCase", "TEXT", 32),
        ],
    )

    # Choose elevation levels spanning a realistic range, every ~70 m.
    z_min = ELEV_BASE - ELEV_AMPLITUDE * 0.6
    z_max = ELEV_BASE + ELEV_AMPLITUDE * 1.4
    levels = []
    step = (z_max - z_min) / float(N_CONTOUR_LEVELS - 1)
    for i in range(N_CONTOUR_LEVELS):
        levels.append(round((z_min + i * step) / 10.0) * 10.0)

    # Marching grid: 240 x 240 cells (~83 m per cell over 20 km).
    grid_n = 240
    cell_w = WIDTH / float(grid_n)
    cell_h = HEIGHT / float(grid_n)

    # Pre-compute the elevation field on the grid corners once.
    log("  pre-computing elevation grid {0}x{0}...".format(grid_n))
    Z = [[0.0] * (grid_n + 1) for _ in range(grid_n + 1)]
    for j in range(grid_n + 1):
        yj = YMIN + j * cell_h
        row = Z[j]
        for i in range(grid_n + 1):
            row[i] = get_elevation(XMIN + i * cell_w, yj)

    next_id = 0
    contour_v_apexes = []   # collected (x, y) apex points for P04 hotspots
    contour_fields = ["SHAPE@", "ContourID", "Elevation",
                      "Index_Contour", "EdgeCase"]
    with arcpy.da.InsertCursor(contours_fc, contour_fields) as cc:
        for level in levels:
            log("  contour level {0:.0f} m...".format(level))
            # Walk every cell, emit short segments where the level crosses
            # the cell's bilinear isoline.  We then chain adjacent
            # segments into longer polylines per cell-row to keep things
            # cheap and avoid building a full graph.
            row_segments = []  # accumulator for the current scan-row
            current_row_y = None
            emitted_this_level = 0
            for j in range(grid_n):
                if emitted_this_level >= N_CONTOURS_PER_LEVEL_MAX:
                    break
                row_chain = []   # current chain of (x, y) for this row
                for i in range(grid_n):
                    z00 = Z[j][i]
                    z10 = Z[j][i + 1]
                    z01 = Z[j + 1][i]
                    z11 = Z[j + 1][i + 1]
                    z_lo = min(z00, z10, z01, z11)
                    z_hi = max(z00, z10, z01, z11)
                    if level < z_lo or level > z_hi:
                        if row_chain and len(row_chain) >= 2:
                            row_segments.append(row_chain)
                            row_chain = []
                        continue
                    # Find one representative point on each cell edge that
                    # the level crosses; connect them.
                    pts = []
                    x0 = XMIN + i * cell_w
                    y0 = YMIN + j * cell_h
                    x1 = x0 + cell_w
                    y1 = y0 + cell_h
                    # Bottom edge
                    if (z00 <= level <= z10) or (z10 <= level <= z00):
                        if abs(z10 - z00) > 1e-9:
                            t = (level - z00) / (z10 - z00)
                        else:
                            t = 0.5
                        pts.append((x0 + cell_w * t, y0))
                    # Top edge
                    if (z01 <= level <= z11) or (z11 <= level <= z01):
                        if abs(z11 - z01) > 1e-9:
                            t = (level - z01) / (z11 - z01)
                        else:
                            t = 0.5
                        pts.append((x0 + cell_w * t, y1))
                    # Left edge
                    if (z00 <= level <= z01) or (z01 <= level <= z00):
                        if abs(z01 - z00) > 1e-9:
                            t = (level - z00) / (z01 - z00)
                        else:
                            t = 0.5
                        pts.append((x0, y0 + cell_h * t))
                    # Right edge
                    if (z10 <= level <= z11) or (z11 <= level <= z10):
                        if abs(z11 - z10) > 1e-9:
                            t = (level - z10) / (z11 - z10)
                        else:
                            t = 0.5
                        pts.append((x1, y0 + cell_h * t))
                    if len(pts) >= 2:
                        # If chain is empty, seed it; else connect.
                        if not row_chain:
                            row_chain.append(pts[0])
                            row_chain.append(pts[-1])
                        else:
                            row_chain.append(pts[-1])
                    else:
                        if row_chain and len(row_chain) >= 2:
                            row_segments.append(row_chain)
                        row_chain = []
                if row_chain and len(row_chain) >= 2:
                    row_segments.append(row_chain)

            # Emit polylines for this level (cap to N_CONTOURS_PER_LEVEL_MAX)
            for chain in row_segments:
                if emitted_this_level >= N_CONTOURS_PER_LEVEL_MAX:
                    break
                if len(chain) < 2:
                    continue
                # ---- V-shape sharpening (creek/valley apex injection) ----
                # Walk the chain; if the elevation field indicates a deep
                # local minimum near a vertex (i.e., a creek line cutting
                # this contour level), pull that vertex toward the local
                # minimum so the contour forms a tight V at the apex.
                sharp = list(chain)
                for vi in range(1, len(sharp) - 1):
                    vx, vy = sharp[vi]
                    # Sample slope; only sharpen where slope is meaningful.
                    sl = get_slope(vx, vy)
                    if sl < 0.05:
                        continue
                    gx, gy = get_gradient(vx, vy, h=10.0)
                    gmag = math.hypot(gx, gy)
                    if gmag < 1e-6:
                        continue
                    # Pull the vertex along -grad by a small amount so the
                    # apex points toward the lower elevation (V-tip).
                    pull = min(80.0, sl * 250.0)
                    vx2 = vx - (gx / gmag) * pull
                    vy2 = vy - (gy / gmag) * pull
                    sharp[vi] = (clamp(vx2, XMIN + 1.0, XMAX - 1.0),
                                 clamp(vy2, YMIN + 1.0, YMAX - 1.0))
                # ---- Detect V-apex vertices (interior angles < 80 deg) --
                for vi in range(1, len(sharp) - 1):
                    ax, ay = sharp[vi - 1]
                    bx, by = sharp[vi]
                    cx_, cy_ = sharp[vi + 1]
                    v1x = ax - bx
                    v1y = ay - by
                    v2x = cx_ - bx
                    v2y = cy_ - by
                    L1 = math.hypot(v1x, v1y)
                    L2 = math.hypot(v2x, v2y)
                    if L1 < 1e-6 or L2 < 1e-6:
                        continue
                    cos_t = (v1x * v2x + v1y * v2y) / (L1 * L2)
                    if cos_t > math.cos(math.radians(80.0)):
                        # Interior angle smaller than 80 deg => sharp V.
                        if len(contour_v_apexes) < 2000:
                            contour_v_apexes.append((bx, by))
                polyline = make_polyline(sharp, sr)
                idx_flag = 1 if int(round(level)) % 100 == 0 else 0
                cc.insertRow([
                    polyline, next_id, float(level), idx_flag, "Normal",
                ])
                next_id += 1
                emitted_this_level += 1

        # Titan Ridge: single 500k-vertex polyline anchored on NW peak.
        log("  building Titan Ridge ({0} vertices)...".format(
            TITAN_RIDGE_VERTICES))
        peak_x, peak_y = find_peak_world_xy(0)
        titan_coords = _build_titan_ridge_at(
            peak_x, peak_y, TITAN_RIDGE_VERTICES)
        titan_arr = arcpy.Array()
        add = titan_arr.add
        for (x, y) in titan_coords:
            add(arcpy.Point(x, y))
        titan_polyline = arcpy.Polyline(titan_arr, sr)
        # Use the elevation at the peak as nominal contour value.
        peak_elev = get_elevation(peak_x, peak_y)
        cc.insertRow([
            titan_polyline, next_id, peak_elev, 1, "Titan_Ridge",
        ])
        next_id += 1
        log("  Titan Ridge inserted (length ~{0:.1f} m, peak elev ~{1:.0f} m)"
            .format(titan_polyline.length, peak_elev))
        del cc

    log("STAGE 1: contours total = {0}, V-apexes captured = {1}"
        .format(next_id, len(contour_v_apexes)))
    return elev_fc, contours_fc, contour_v_apexes


def _build_titan_ridge_at(cx, cy, n_vertices):
    """Fractal-perturbed logarithmic spiral centered on (cx, cy).

    Stays within ~1.5 km of the peak so the ridge actually decorates the
    summit instead of sprawling across the world.
    """
    a = 1.0
    b = 0.05
    octaves = [
        (1.0, 0.013),
        (0.5, 0.041),
        (0.25, 0.137),
        (0.12, 0.421),
        (0.06, 1.113),
        (0.03, 3.371),
    ]
    max_radius = 1500.0
    coords = []
    append = coords.append
    sin = math.sin
    cos = math.cos
    exp = math.exp
    for i in range(n_vertices):
        theta = i * 0.0008
        r = a * exp(b * theta)
        if r > max_radius:
            r = max_radius
        pert = 0.0
        for amp, freq in octaves:
            pert += amp * sin(theta * freq * 12.566 + i * 0.0001)
        rr = r * (1.0 + 0.07 * pert)
        x = cx + rr * cos(theta)
        y = cy + rr * sin(theta)
        if x < XMIN + 1.0:
            x = XMIN + 1.0
        elif x > XMAX - 1.0:
            x = XMAX - 1.0
        if y < YMIN + 1.0:
            y = YMIN + 1.0
        elif y > YMAX - 1.0:
            y = YMAX - 1.0
        append((x, y))
    return coords



# ===========================================================================
# STAGE 2 - Hydrology: rivers descend along the negative gradient
# ===========================================================================
#
# Each river starts at a high-elevation seed point (mountainous area) and
# is integrated downstream by following -grad(elevation) with a small
# random walk to keep it from looking too clinical.  Rivers stop when:
#   * they reach the bbox edge,
#   * they enter the central plain and slope is near zero,
#   * step count exceeds a hard cap.
# Vertices and segments are sampled into pools used by P01 edge cases
# (T-junctions, collinear overlaps).
# ---------------------------------------------------------------------------


def build_stage2_hydrology(gdb, sr):
    log("STAGE 2: Drainage (River_L / Seasonal_River_L / Abreez / Canal)")

    rivers_fc = create_fc(
        gdb, "Drainage", "POLYLINE", sr,
        [
            ("RiverID", "LONG", None),
            ("Stream", "TEXT", 16),
            ("HydroClass", "TEXT", 24),  # River_L | Seasonal_River_L | Abreez | Canal
            ("Name", "TEXT", 64),
            ("EdgeCase", "TEXT", 32),
        ],
    )

    river_vertex_pool = []   # for T-junctions
    river_segment_pool = []  # for collinear overlaps
    river_polylines_coords = []   # for road bridge planning later

    river_fields = ["SHAPE@", "RiverID", "Stream", "HydroClass",
                    "Name", "EdgeCase"]

    def trace_descent(seed_xy, step_size, max_steps, wander_amp,
                      stop_on_plain=True):
        sx, sy = seed_xy
        sx = clamp(sx, XMIN + 50.0, XMAX - 50.0)
        sy = clamp(sy, YMIN + 50.0, YMAX - 50.0)
        coords = [(sx, sy)]
        x, y = sx, sy
        for _step in range(max_steps):
            gx, gy = get_gradient(x, y)
            gmag = math.hypot(gx, gy)
            if gmag < 1e-6:
                ang = random.uniform(0.0, 2.0 * math.pi)
                ux, uy = math.cos(ang), math.sin(ang)
            else:
                ux = -gx / gmag
                uy = -gy / gmag
            wander = random.uniform(-wander_amp, wander_amp)
            cs = math.cos(wander)
            sn = math.sin(wander)
            ux2 = ux * cs - uy * sn
            uy2 = ux * sn + uy * cs
            x += ux2 * step_size
            y += uy2 * step_size
            if not in_bbox(x, y, pad=20.0):
                coords.append((clamp(x, XMIN + 1.0, XMAX - 1.0),
                               clamp(y, YMIN + 1.0, YMAX - 1.0)))
                break
            coords.append((x, y))
            if stop_on_plain and is_on_plain(x, y) and gmag < 0.005:
                break
        return coords

    def sample_pools(coords):
        for k in range(0, len(coords), 4):
            if len(river_vertex_pool) < 50000:
                river_vertex_pool.append(coords[k])
        for k in range(0, len(coords) - 1, 6):
            if len(river_segment_pool) < 5000:
                river_segment_pool.append((coords[k], coords[k + 1]))

    def near_peak_seed():
        peak_idx = random.randint(0, len(PEAKS_NORM) - 1)
        pcx, pcy, _ = PEAKS_NORM[peak_idx]
        sx = XMIN + (pcx + jitter(0.06)) * WIDTH
        sy = YMIN + (pcy + jitter(0.06)) * HEIGHT
        return sx, sy

    n_done = 0
    plans = [
        # (label, count, step, max_steps, wander, min_seed_elev)
        ("River_L",          N_RIVERS_PERENNIAL,  60.0, 600, 0.35,
            ELEV_BASE + 0.30 * ELEV_AMPLITUDE),
        ("Seasonal_River_L", N_RIVERS_SEASONAL,   55.0, 400, 0.55,
            ELEV_BASE + 0.20 * ELEV_AMPLITUDE),
        ("Abreez",           N_ABREEZ,            35.0, 200, 0.20,
            ELEV_BASE + 0.55 * ELEV_AMPLITUDE),
    ]

    with arcpy.da.InsertCursor(rivers_fc, river_fields) as rc:
        for (hclass, target_n, step, max_steps, wander, min_elev) in plans:
            log("  hydrology class {0}: target {1}".format(hclass, target_n))
            done = 0
            attempts = 0
            while done < target_n and attempts < target_n * 30:
                attempts += 1
                sx, sy = near_peak_seed()
                if get_elevation(sx, sy) < min_elev:
                    continue
                raw = trace_descent((sx, sy), step, max_steps, wander)
                if len(raw) < 5:
                    continue
                # Densify with spline to get organic curves.
                coords = densify_polyline_spline(
                    raw, vertices_per_km=ROAD_VERTICES_PER_KM)
                if len(coords) < 5:
                    continue
                stream = ("Perennial" if hclass == "River_L"
                          else "Intermittent" if hclass == "Seasonal_River_L"
                          else "Ephemeral")
                polyline = make_polyline(coords, sr)
                rc.insertRow([
                    polyline, n_done, stream, hclass,
                    "{0}_{1:04d}".format(hclass, done),
                    "Normal",
                ])
                river_polylines_coords.append(coords)
                sample_pools(coords)
                done += 1
                n_done += 1

        # Canals: man-made; arrow-straight in the plain, mild curves.
        log("  hydrology class Canal: target {0}".format(N_CANALS))
        plain_cx = XMIN + PLAIN_CENTER_NORM[0] * WIDTH
        plain_cy = YMIN + PLAIN_CENTER_NORM[1] * HEIGHT
        for ci in range(N_CANALS):
            ang = ci * (math.pi / N_CANALS) + 0.07
            half = 4500.0
            ax = plain_cx - math.cos(ang) * half
            ay = plain_cy - math.sin(ang) * half
            bx = plain_cx + math.cos(ang) * half
            by = plain_cy + math.sin(ang) * half
            mid_a = (plain_cx - math.cos(ang) * half * 0.5
                     + jitter(40.0),
                     plain_cy - math.sin(ang) * half * 0.5
                     + jitter(40.0))
            mid_b = (plain_cx + math.cos(ang) * half * 0.5
                     + jitter(40.0),
                     plain_cy + math.sin(ang) * half * 0.5
                     + jitter(40.0))
            raw = [(ax, ay), mid_a, (plain_cx, plain_cy), mid_b, (bx, by)]
            coords = densify_polyline_spline(
                raw, vertices_per_km=ROAD_VERTICES_PER_KM)
            polyline = make_polyline(coords, sr)
            rc.insertRow([
                polyline, n_done, "Perennial", "Canal",
                "Canal_{0:04d}".format(ci), "Normal",
            ])
            river_polylines_coords.append(coords)
            sample_pools(coords)
            n_done += 1

    del rc
    log("STAGE 2: complete. hydrology features = {0}".format(n_done))
    return rivers_fc, river_polylines_coords, \
        river_vertex_pool, river_segment_pool


# ===========================================================================
# STAGE 3 - Roads (flat terrain), with bridge crossings + edge cases
# ===========================================================================
#
# Trunks: long polylines that stay in the plain or low-slope valleys
# (slope < threshold).  When the unconstrained path would cross a river
# we snap the crossing point so that the road meets the river at
# 90 degrees (or 45 for diagonal-feel variation), guaranteeing a valid
# bridge/culvert event.
#
# Local connectors: shorter polylines branching off trunks.
#
# Edge cases injected at the end, into a dedicated "EdgeCase" column:
#   * T-junction stubs    -> end-vertex on a river vertex (P01 stress)
#   * Collinear overlaps  -> road segment coincident with river segment
# ---------------------------------------------------------------------------


def _segments_from_polyline(coords):
    """yield ((x1, y1), (x2, y2)) segments."""
    for i in range(1, len(coords)):
        yield coords[i - 1], coords[i]


def _seg_intersect(p1, p2, p3, p4):
    """Returns the intersection point of segments p1-p2 and p3-p4 or None.

    Pure-python 2D segment intersection.  Returns (x, y, t, u) where
    t is param along p1->p2 and u along p3->p4.
    """
    x1, y1 = p1
    x2, y2 = p2
    x3, y3 = p3
    x4, y4 = p4
    denom = (x1 - x2) * (y3 - y4) - (y1 - y2) * (x3 - x4)
    if abs(denom) < 1e-12:
        return None
    t_num = (x1 - x3) * (y3 - y4) - (y1 - y3) * (x3 - x4)
    u_num = (x1 - x3) * (y1 - y2) - (y1 - y3) * (x1 - x2)
    t = t_num / denom
    u = u_num / denom
    if 0.0 <= t <= 1.0 and 0.0 <= u <= 1.0:
        ix = x1 + t * (x2 - x1)
        iy = y1 + t * (y2 - y1)
        return ix, iy, t, u
    return None


def _find_river_bridge(road_seg, river_polylines_coords):
    """If road_seg crosses a river, return (cx, cy, river_idx, river_seg_idx,
    river_dir_unit_vec).  Otherwise return None.
    """
    p1, p2 = road_seg
    for ridx, rcoords in enumerate(river_polylines_coords):
        # Quick AABB check
        rxs = [c[0] for c in rcoords]
        rys = [c[1] for c in rcoords]
        if (max(p1[0], p2[0]) < min(rxs) or
                min(p1[0], p2[0]) > max(rxs) or
                max(p1[1], p2[1]) < min(rys) or
                min(p1[1], p2[1]) > max(rys)):
            continue
        for j in range(1, len(rcoords)):
            r1 = rcoords[j - 1]
            r2 = rcoords[j]
            hit = _seg_intersect(p1, p2, r1, r2)
            if hit is not None:
                ix, iy, _t, _u = hit
                rdx = r2[0] - r1[0]
                rdy = r2[1] - r1[1]
                rL = math.hypot(rdx, rdy)
                if rL < 1e-9:
                    continue
                return ix, iy, ridx, j - 1, (rdx / rL, rdy / rL)
    return None


def _trace_road_in_low_slope(start_xy, target_xy, river_polylines_coords,
                             max_pts=120, out_bridges=None):
    """Walk from start_xy toward target_xy in low-slope corridors.

    The walk biases each step toward the target but rejects steps where
    the slope at the new location is high (mountains / cliffs).  When a
    proposed step would cross a river segment we snap the next vertex
    to the river crossing so the road meets the river at near 90 deg.
    If ``out_bridges`` is a list, each crossing (cx, cy, river_idx,
    river_dir_unit_vec) is appended as we go - used by Stage 3 to emit
    Bridge_P / Culvert_Pnt anchor points.
    """
    coords = [start_xy]
    x, y = start_xy
    tx, ty = target_xy
    step = 90.0
    slope_max_plain = 0.04
    slope_max_valley = 0.10
    bridge_count = 0

    for _ in range(max_pts):
        dxg = tx - x
        dyg = ty - y
        d_to_target = math.hypot(dxg, dyg)
        if d_to_target < step:
            coords.append((tx, ty))
            break
        ux = dxg / d_to_target
        uy = dyg / d_to_target

        # Try the direct step; if slope too high, sample alternative
        # directions (jittered) and take the one with lowest slope that
        # still moves us closer.
        candidates = []
        for ang_off in (0.0, 0.25, -0.25, 0.55, -0.55, 0.9, -0.9):
            cos_a = math.cos(ang_off)
            sin_a = math.sin(ang_off)
            cx = ux * cos_a - uy * sin_a
            cy = ux * sin_a + uy * cos_a
            nx_ = x + cx * step
            ny_ = y + cy * step
            if not in_bbox(nx_, ny_, pad=10.0):
                continue
            slope_here = get_slope(nx_, ny_)
            limit = (slope_max_plain
                     if is_on_plain(nx_, ny_)
                     else slope_max_valley)
            if slope_here > limit:
                continue
            # Penalty: how much we deviated from target direction.
            penalty = 1.0 - (cx * ux + cy * uy)
            candidates.append((slope_here + penalty * 0.05, nx_, ny_))
        if not candidates:
            # Stuck: bail with what we have.
            break
        candidates.sort()
        _, nx_, ny_ = candidates[0]

        # River-crossing snap: if the segment (x,y)->(nx_,ny_) crosses a
        # river, replace the move so we land exactly on the crossing,
        # then add an extra perpendicular departure point so the next
        # step naturally exits at ~90 deg.
        bridge = _find_river_bridge(
            ((x, y), (nx_, ny_)), river_polylines_coords)
        if bridge is not None and bridge_count < 8:
            ix, iy, _ridx, _seg_idx, (rux, ruy) = bridge
            # Perpendicular to river: choose the side of (nx_, ny_).
            # Two candidates: rotate +90 or -90 around river dir.
            perp_a = (-ruy, rux)
            perp_b = (ruy, -rux)
            # Pick the one that moves us toward the target.
            ax = ix + perp_a[0] * step * 0.5
            ay = iy + perp_a[1] * step * 0.5
            bx = ix + perp_b[0] * step * 0.5
            by = iy + perp_b[1] * step * 0.5
            da = (ax - tx) ** 2 + (ay - ty) ** 2
            db = (bx - tx) ** 2 + (by - ty) ** 2
            if da < db:
                ex_x, ex_y = ax, ay
            else:
                ex_x, ex_y = bx, by
            # Approach point: opposite side of river from exit.
            ax_x = 2.0 * ix - ex_x
            ax_y = 2.0 * iy - ex_y
            # Insert: approach -> crossing -> exit (perpendicular).
            coords.append((ax_x, ax_y))
            coords.append((ix, iy))
            coords.append((ex_x, ex_y))
            x, y = ex_x, ex_y
            if out_bridges is not None:
                out_bridges.append((ix, iy, _ridx, (rux, ruy)))
            bridge_count += 1
            continue

        coords.append((nx_, ny_))
        x, y = nx_, ny_

    # Clamp all to bbox
    coords = [(clamp(cx, XMIN + 1.0, XMAX - 1.0),
               clamp(cy, YMIN + 1.0, YMAX - 1.0)) for (cx, cy) in coords]
    return coords


def build_stage3_roads(gdb, sr, river_polylines_coords,
                       river_vertex_pool, river_segment_pool):
    log("STAGE 3: Roads (flat terrain, bridges over rivers)")

    roads_fc = create_fc(
        gdb, "Roads", "POLYLINE", sr,
        [
            ("RoadID", "LONG", None),
            ("RoadClass", "TEXT", 32),    # Freeway|Highway|Parkway|Asphalt_Road|Gravel|Local|Track|TJunction_Stub|Collinear_Overlay
            ("Surface", "TEXT", 16),
            ("SpeedLimit", "SHORT", None),
            ("EdgeCase", "TEXT", 32),
        ],
    )

    # Per-class plans: (RoadClass, surface, speed, count, max_pts).
    # Higher-class roads get more samples so they stretch further.
    road_class_plans = [
        ("Freeway",      "Paved",  120, N_FREEWAY,  220),
        ("Highway",      "Paved",  100, N_HIGHWAY,  200),
        ("Parkway",      "Paved",   80, N_PARKWAY,  180),
        ("Asphalt_Road", "Paved",   60, N_ASPHALT,  160),
        ("Gravel",       "Gravel",  40, N_GRAVEL,   140),
    ]

    plain_cx = XMIN + PLAIN_CENTER_NORM[0] * WIDTH
    plain_cy = YMIN + PLAIN_CENTER_NORM[1] * HEIGHT

    next_road_id = 0
    trunk_endpoints = []   # used to seed local connectors
    trunk_classes = []     # parallel list: RoadClass per trunk
    bridge_points = []     # (x, y, river_idx, river_dir, road_class)

    road_fields = ["SHAPE@", "RoadID", "RoadClass", "Surface",
                   "SpeedLimit", "EdgeCase"]
    with arcpy.da.InsertCursor(roads_fc, road_fields) as rc:
        # ----- Trunks per class -----
        ti_global = 0
        for (klass, surface, speed_limit, count, max_pts) in road_class_plans:
            log("  laying {0} {1}(s)...".format(count, klass))
            for ci in range(count):
                side = ti_global % 4
                ti_global += 1
                if side == 0:
                    start = (XMIN + 200.0,
                             YMIN + random.random() * HEIGHT)
                    end = (XMAX - 200.0,
                           YMIN + random.random() * HEIGHT)
                elif side == 1:
                    start = (XMIN + random.random() * WIDTH,
                             YMIN + 200.0)
                    end = (XMIN + random.random() * WIDTH,
                           YMAX - 200.0)
                elif side == 2:
                    start = (XMIN + 200.0,
                             YMIN + random.random() * HEIGHT)
                    end = (plain_cx + jitter(2000.0),
                           plain_cy + jitter(2000.0))
                else:
                    start = (plain_cx + jitter(2000.0),
                             plain_cy + jitter(2000.0))
                    end = (XMAX - 200.0,
                           YMIN + random.random() * HEIGHT)
                local_bridges = []
                raw = _trace_road_in_low_slope(
                    start, end, river_polylines_coords,
                    max_pts=max_pts, out_bridges=local_bridges)
                if len(raw) < 2:
                    continue
                # Densify with spline so the road meanders organically.
                coords = densify_polyline_spline(
                    raw, vertices_per_km=ROAD_VERTICES_PER_KM)
                polyline = make_polyline(coords, sr)
                rc.insertRow([
                    polyline, next_road_id, klass,
                    surface, speed_limit, "Trunk",
                ])
                trunk_endpoints.append(coords)
                trunk_classes.append(klass)
                # Tag bridge points with road class for later use.
                for (bx, by, ridx, rdir) in local_bridges:
                    bridge_points.append((bx, by, ridx, rdir, klass))
                next_road_id += 1

        # ----- Local connectors -----
        log("  building local connectors off trunks...")
        for trunk_coords in trunk_endpoints:
            for _li in range(N_ROAD_LOCAL_PER_TRUNK):
                if len(trunk_coords) < 2:
                    continue
                k = random.randint(0, len(trunk_coords) - 1)
                bx, by = trunk_coords[k]
                ang = random.uniform(0.0, 2.0 * math.pi)
                length = random.uniform(500.0, 1500.0)
                ex_x = bx + math.cos(ang) * length
                ex_y = by + math.sin(ang) * length
                ex_x = clamp(ex_x, XMIN + 50.0, XMAX - 50.0)
                ex_y = clamp(ex_y, YMIN + 50.0, YMAX - 50.0)
                if get_slope(ex_x, ex_y) > 0.12:
                    continue
                local_bridges = []
                raw = _trace_road_in_low_slope(
                    (bx, by), (ex_x, ex_y), river_polylines_coords,
                    max_pts=40, out_bridges=local_bridges)
                if len(raw) < 2:
                    continue
                coords = densify_polyline_spline(
                    raw, vertices_per_km=ROAD_VERTICES_PER_KM)
                polyline = make_polyline(coords, sr)
                klass_l = random.choice(["Local", "Track"])
                rc.insertRow([
                    polyline, next_road_id, klass_l,
                    random.choice(["Paved", "Gravel", "Dirt"]),
                    random.choice([30, 40, 50]),
                    "Local",
                ])
                for (bxp, byp, ridx, rdir) in local_bridges:
                    bridge_points.append(
                        (bxp, byp, ridx, rdir, klass_l))
                next_road_id += 1

        # ----- Edge case: T-junction stubs -----
        log("  injecting {0} T-junction stubs...".format(N_T_JUNCTIONS))
        injected = 0
        for _i in range(N_T_JUNCTIONS):
            if not river_vertex_pool:
                break
            tx, ty = random.choice(river_vertex_pool)
            ang = random.uniform(0.0, 2.0 * math.pi)
            length = random.uniform(20.0, 120.0)
            ex_x = clamp(tx + math.cos(ang) * length,
                         XMIN + 1.0, XMAX - 1.0)
            ex_y = clamp(ty + math.sin(ang) * length,
                         YMIN + 1.0, YMAX - 1.0)
            polyline = make_polyline([(ex_x, ex_y), (tx, ty)], sr)
            rc.insertRow([
                polyline, next_road_id, "TJunction_Stub",
                "Paved", 30, "T_Junction",
            ])
            next_road_id += 1
            injected += 1
        log("  T-junctions injected: {0}".format(injected))

        # ----- Edge case: Collinear overlaps -----
        log("  injecting {0} collinear road/river overlaps..."
            .format(N_COLLINEAR_OVERLAPS))
        n_over = min(N_COLLINEAR_OVERLAPS, len(river_segment_pool))
        for i in range(n_over):
            (rx1, ry1), (rx2, ry2) = river_segment_pool[i]
            dx = rx2 - rx1
            dy = ry2 - ry1
            seg_len = math.hypot(dx, dy)
            if seg_len < 1e-6:
                continue
            ux = dx / seg_len
            uy = dy / seg_len
            ext = random.uniform(50.0, 250.0)
            ax = clamp(rx1 - ux * ext, XMIN + 1.0, XMAX - 1.0)
            ay = clamp(ry1 - uy * ext, YMIN + 1.0, YMAX - 1.0)
            bx = clamp(rx2 + ux * ext, XMIN + 1.0, XMAX - 1.0)
            by = clamp(ry2 + uy * ext, YMIN + 1.0, YMAX - 1.0)
            coords = [(ax, ay), (rx1, ry1), (rx2, ry2), (bx, by)]
            polyline = make_polyline(coords, sr)
            rc.insertRow([
                polyline, next_road_id, "Collinear_Overlay",
                "Paved", 50, "Collinear_Overlap",
            ])
            next_road_id += 1

    del rc
    log("STAGE 3: roads = {0}, captured {1} bridge crossings"
        .format(next_road_id, len(bridge_points)))

    # ----- Bridge_P / Culvert_Pnt feature classes -----
    # Bridge_P sits on higher-class roads (Freeway, Highway, Parkway).
    # Culvert_Pnt sits on lower-class crossings (Asphalt_Road, Gravel,
    # Local, Track).  Each carries the river direction so a downstream
    # rotation tool has something to rotate against.
    bridge_fc = create_fc(
        gdb, "Bridge_P", "POINT", sr,
        [
            ("BridgeID", "LONG", None),
            ("RoadClass", "TEXT", 32),
            ("RiverIdx", "LONG", None),
            ("RiverDirX", "DOUBLE", None),
            ("RiverDirY", "DOUBLE", None),
            ("EdgeCase", "TEXT", 32),
        ],
    )
    culvert_fc = create_fc(
        gdb, "Culvert_Pnt", "POINT", sr,
        [
            ("CulvertID", "LONG", None),
            ("RoadClass", "TEXT", 32),
            ("RiverIdx", "LONG", None),
            ("RiverDirX", "DOUBLE", None),
            ("RiverDirY", "DOUBLE", None),
            ("EdgeCase", "TEXT", 32),
        ],
    )
    bridge_classes = ("Freeway", "Highway", "Parkway")
    nb = nc = 0
    # File geodatabases do not allow two InsertCursors to be open
    # concurrently against the same workspace (the first cursor opens
    # an implicit transaction and the second raises
    # "workspace already in transaction mode").  We therefore buffer
    # bridges and culverts into in-memory lists in a single dispatch
    # pass, then write each feature class with its own cursor in its
    # own `with` block.
    bridge_rows = []
    culvert_rows = []
    for (bx, by, ridx, rdir, klass) in bridge_points:
        rdx, rdy = rdir
        if klass in bridge_classes:
            bridge_rows.append([
                make_point(bx, by, sr), nb, klass, ridx,
                rdx, rdy, "Road_River_Crossing",
            ])
            nb += 1
        else:
            culvert_rows.append([
                make_point(bx, by, sr), nc, klass, ridx,
                rdx, rdy, "Road_River_Crossing",
            ])
            nc += 1

    with arcpy.da.InsertCursor(
            bridge_fc,
            ["SHAPE@", "BridgeID", "RoadClass", "RiverIdx",
             "RiverDirX", "RiverDirY", "EdgeCase"]) as bcur:
        for row in bridge_rows:
            bcur.insertRow(row)
    del bcur

    with arcpy.da.InsertCursor(
            culvert_fc,
            ["SHAPE@", "CulvertID", "RoadClass", "RiverIdx",
             "RiverDirX", "RiverDirY", "EdgeCase"]) as ccur:
        for row in culvert_rows:
            ccur.insertRow(row)
    del ccur

    log("STAGE 3: Bridge_P = {0}, Culvert_Pnt = {1}".format(nb, nc))
    return roads_fc, trunk_endpoints, trunk_classes



# ===========================================================================
# STAGE 4 - Megacity & Utilities
# ===========================================================================
#
# Buildings: small rectangles offset perpendicular to a city road that
# lives in the plain.  We pick the longest plain-resident road (by
# length actually inside the plain mask) and pack 10,000+ buildings
# along its first ~1 km.  Buildings are validated against:
#   * not on roads (offset is away from road centerline)
#   * not on rivers (rejection sampling)
#   * elevation is in plain band (rejection sampling)
#
# Gas pipes: half copy a road geometry verbatim (parallel-under-road),
# the rest run as background pipes outside the plain.
#
# Power lines: half run alongside roads at a small lateral offset,
# a dedicated zone of 400 lines crosses roads at acute angles to stress
# Plugin 02, the rest are background.
#
# Label_Candidate_Boxes: heavy clustering hot-spots for P04 numpy AABB
# collision logic.
# ---------------------------------------------------------------------------


def _polyline_total_length(coords):
    total = 0.0
    for i in range(1, len(coords)):
        x1, y1 = coords[i - 1]
        x2, y2 = coords[i]
        total += math.hypot(x2 - x1, y2 - y1)
    return total


def _sample_along_polyline(coords, target_length, count):
    """Yield ``count`` (x, y, tx, ty) samples along the first
    ``target_length`` meters of the polyline.
    """
    cum = [0.0]
    for i in range(1, len(coords)):
        x1, y1 = coords[i - 1]
        x2, y2 = coords[i]
        cum.append(cum[-1] + math.hypot(x2 - x1, y2 - y1))
    total = cum[-1]
    use_len = min(target_length, total)
    out = []
    for k in range(count):
        s = (k / float(max(count - 1, 1))) * use_len
        seg_idx = 0
        while seg_idx < len(cum) - 2 and cum[seg_idx + 1] < s:
            seg_idx += 1
        s0 = cum[seg_idx]
        s1 = cum[seg_idx + 1] if seg_idx + 1 < len(cum) else cum[-1]
        if s1 - s0 < 1e-9:
            t = 0.0
        else:
            t = (s - s0) / (s1 - s0)
        x1, y1 = coords[seg_idx]
        x2, y2 = coords[min(seg_idx + 1, len(coords) - 1)]
        px = x1 + (x2 - x1) * t
        py = y1 + (y2 - y1) * t
        dx = x2 - x1
        dy = y2 - y1
        L = math.hypot(dx, dy)
        if L < 1e-9:
            tx, ty = 1.0, 0.0
        else:
            tx, ty = dx / L, dy / L
        out.append((px, py, tx, ty))
    return out


def _pick_city_road(trunk_endpoints):
    """Return the trunk that spends the most length inside the plain."""
    best_len = -1.0
    best_coords = None
    for coords in trunk_endpoints:
        plain_len = 0.0
        for i in range(1, len(coords)):
            mx = (coords[i - 1][0] + coords[i][0]) * 0.5
            my = (coords[i - 1][1] + coords[i][1]) * 0.5
            if is_on_plain(mx, my):
                plain_len += math.hypot(
                    coords[i][0] - coords[i - 1][0],
                    coords[i][1] - coords[i - 1][1])
        if plain_len > best_len:
            best_len = plain_len
            best_coords = coords
    return best_coords, best_len


def _point_distance_to_polylines(px, py, polylines, cap=200.0):
    """Approximate distance from (px, py) to the nearest segment in any
    of the given polyline coord lists, early-exiting if below cap."""
    best = float("inf")
    for coords in polylines:
        for i in range(1, len(coords)):
            x1, y1 = coords[i - 1]
            x2, y2 = coords[i]
            dx = x2 - x1
            dy = y2 - y1
            L2 = dx * dx + dy * dy
            if L2 < 1e-12:
                d = math.hypot(px - x1, py - y1)
            else:
                t = ((px - x1) * dx + (py - y1) * dy) / L2
                if t < 0.0:
                    t = 0.0
                elif t > 1.0:
                    t = 1.0
                qx = x1 + dx * t
                qy = y1 + dy * t
                d = math.hypot(px - qx, py - qy)
            if d < best:
                best = d
                if best < cap * 0.05:
                    return best
    return best


def build_stage4_megacity(gdb, sr, roads_fc, trunk_endpoints,
                          river_polylines_coords,
                          trunk_classes=None,
                          contour_v_apexes=None):
    log("STAGE 4: Megacity (Buildings + Gas_Pipe_L + Power_Line_L + Labels)")
    if trunk_classes is None:
        trunk_classes = [None] * len(trunk_endpoints)
    if contour_v_apexes is None:
        contour_v_apexes = []

    # ------------------------------------------------------------------
    # Materialize trunk + local road geometries we have on hand
    # (trunk_endpoints already holds trunk coord lists).  We don't need
    # to re-read the FC; trunk_endpoints is sufficient for placement.
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Buildings
    # ------------------------------------------------------------------
    bld_fc = create_fc(
        gdb, "Buildings", "POLYGON", sr,
        [
            ("BldID", "LONG", None),
            ("Height_m", "FLOAT", None),
            ("EdgeCase", "TEXT", 32),
        ],
    )

    city_road, city_len = _pick_city_road(trunk_endpoints)
    if city_road is None or city_len < 800.0:
        # Fallback: synthetic 2 km horizontal segment in the plain.
        plain_cx = XMIN + PLAIN_CENTER_NORM[0] * WIDTH
        plain_cy = YMIN + PLAIN_CENTER_NORM[1] * HEIGHT
        city_road = [(plain_cx - 1000.0, plain_cy),
                     (plain_cx + 1000.0, plain_cy)]
        city_len = 2000.0
    log("  city road (plain-resident length ~{0:.0f} m)".format(city_len))

    next_bld_id = 0
    bld_fields = ["SHAPE@", "BldID", "Height_m", "EdgeCase"]
    with arcpy.da.InsertCursor(bld_fc, bld_fields) as bc:
        # ----- Background: 4000 buildings scattered on the plain -----
        log("  placing {0} background buildings (plain only)..."
            .format(N_BUILDINGS_BACKGROUND))
        attempts = 0
        n_bg = 0
        while n_bg < N_BUILDINGS_BACKGROUND and attempts < N_BUILDINGS_BACKGROUND * 8:
            attempts += 1
            cx_ = XMIN + random.random() * WIDTH
            cy_ = YMIN + random.random() * HEIGHT
            if not is_on_plain(cx_, cy_):
                continue
            # Reject if too close to a road (must NOT intersect roads).
            if _point_distance_to_polylines(cx_, cy_, trunk_endpoints,
                                            cap=80.0) < 18.0:
                continue
            # Reject if too close to a river.
            if _point_distance_to_polylines(cx_, cy_, river_polylines_coords,
                                            cap=80.0) < 20.0:
                continue
            w = random.uniform(8.0, 35.0)
            h = random.uniform(8.0, 35.0)
            poly = make_polygon([
                (cx_ - w * 0.5, cy_ - h * 0.5),
                (cx_ + w * 0.5, cy_ - h * 0.5),
                (cx_ + w * 0.5, cy_ + h * 0.5),
                (cx_ - w * 0.5, cy_ + h * 0.5),
                (cx_ - w * 0.5, cy_ - h * 0.5),
            ], sr)
            bc.insertRow([poly, next_bld_id,
                          random.uniform(3.0, 30.0), "Plain_Background"])
            next_bld_id += 1
            n_bg += 1

        # ----- Dense cluster: 10,500 along the city road, all on plain -----
        log("  packing {0} dense buildings along city road..."
            .format(N_BUILDINGS_CLUSTER))
        positions = _sample_along_polyline(
            city_road,
            target_length=min(1000.0, city_len),
            count=N_BUILDINGS_CLUSTER)
        n_cluster = 0
        for (px, py, tx, ty) in positions:
            # Build candidates on both sides of the road; pick first that
            # is on plain, off-road, off-river.
            placed = False
            for side in (1.0, -1.0):
                for off_amt in (16.0, 24.0, 34.0):
                    nx_ = -ty
                    ny_ = tx
                    ox = px + nx_ * side * off_amt
                    oy = py + ny_ * side * off_amt
                    if not is_on_plain(ox, oy):
                        continue
                    if _point_distance_to_polylines(
                            ox, oy, river_polylines_coords, cap=60.0) < 18.0:
                        continue
                    # Tight building footprint, rotated to align road.
                    w = random.uniform(4.0, 8.0)
                    h = random.uniform(4.0, 8.0)
                    cos_a, sin_a = tx, ty
                    poly_pts = []
                    for (lx, ly) in [
                            (-w * 0.5, -h * 0.5),
                            (w * 0.5, -h * 0.5),
                            (w * 0.5, h * 0.5),
                            (-w * 0.5, h * 0.5)]:
                        gx = ox + lx * cos_a - ly * sin_a
                        gy = oy + lx * sin_a + ly * cos_a
                        gx = clamp(gx, XMIN + 1.0, XMAX - 1.0)
                        gy = clamp(gy, YMIN + 1.0, YMAX - 1.0)
                        poly_pts.append((gx, gy))
                    poly_pts.append(poly_pts[0])
                    poly = make_polygon(poly_pts, sr)
                    bc.insertRow([
                        poly, next_bld_id,
                        random.uniform(3.0, 12.0),
                        "Dense_Cluster_1km",
                    ])
                    next_bld_id += 1
                    n_cluster += 1
                    placed = True
                    break
                if placed:
                    break
            if n_cluster % 2000 == 0 and n_cluster > 0:
                log("    cluster buildings: {0}/{1}"
                    .format(n_cluster, N_BUILDINGS_CLUSTER))

    del bc
    log("STAGE 4: buildings = {0} (background + cluster)"
        .format(next_bld_id))

    # ------------------------------------------------------------------
    # Gas_Pipe_L  (tangent overlay: shift road vertices by 0.0..0.5 m
    # so the pipe runs visually on top of the road edge).
    # ------------------------------------------------------------------
    gas_fc = create_fc(
        gdb, "Gas_Pipe_L", "POLYLINE", sr,
        [
            ("PipeID", "LONG", None),
            ("Pressure_PSI", "SHORT", None),
            ("EdgeCase", "TEXT", 32),
        ],
    )
    log("  Gas_Pipe_L: {0} tangent + {1} background..."
        .format(N_GAS_PIPES_TANGENT, N_GAS_PIPES_BACKGROUND))
    next_pipe_id = 0
    gas_fields = ["SHAPE@", "PipeID", "Pressure_PSI", "EdgeCase"]
    with arcpy.da.InsertCursor(gas_fc, gas_fields) as gc:
        # Tangent overlay: pick meandering trunks and shift each vertex
        # perpendicular to local tangent by an offset in [0.0, 0.5] m.
        # All shifts on a given pipe use the SAME side so the pipe runs
        # cleanly along the road edge (not crossing it).
        chosen_idx = list(range(len(trunk_endpoints)))
        random.shuffle(chosen_idx)
        for k in range(min(N_GAS_PIPES_TANGENT, len(chosen_idx))):
            road_coords = trunk_endpoints[chosen_idx[k]]
            if len(road_coords) < 2:
                continue
            side_sign = random.choice([-1.0, 1.0])
            shifted = []
            for i in range(len(road_coords)):
                if i == 0:
                    dx = road_coords[1][0] - road_coords[0][0]
                    dy = road_coords[1][1] - road_coords[0][1]
                elif i == len(road_coords) - 1:
                    dx = road_coords[-1][0] - road_coords[-2][0]
                    dy = road_coords[-1][1] - road_coords[-2][1]
                else:
                    dx = road_coords[i + 1][0] - road_coords[i - 1][0]
                    dy = road_coords[i + 1][1] - road_coords[i - 1][1]
                L = math.hypot(dx, dy)
                if L < 1e-9:
                    nx_, ny_ = 0.0, 0.0
                else:
                    nx_ = -dy / L
                    ny_ = dx / L
                # Per-vertex offset, deterministic-feel jitter.
                off = random.uniform(GAS_TANGENT_OFFSET_MIN,
                                     GAS_TANGENT_OFFSET_MAX) * side_sign
                ox = road_coords[i][0] + nx_ * off
                oy = road_coords[i][1] + ny_ * off
                shifted.append((clamp(ox, XMIN + 1.0, XMAX - 1.0),
                                clamp(oy, YMIN + 1.0, YMAX - 1.0)))
            polyline = make_polyline(shifted, sr)
            gc.insertRow([
                polyline, next_pipe_id,
                random.choice([60, 100, 250, 600]),
                "Pipe_Tangent_To_Road",
            ])
            next_pipe_id += 1

        # Background pipes outside the plain (in valleys/foothills).
        for _ in range(N_GAS_PIPES_BACKGROUND):
            x = XMIN + random.random() * WIDTH
            y = YMIN + random.random() * HEIGHT
            if is_on_plain(x, y):
                if random.random() < 0.7:
                    continue
            n_pts = random.randint(4, 10)
            ang = random.uniform(0.0, 2.0 * math.pi)
            seg = random.uniform(80.0, 250.0)
            coords = []
            for j in range(n_pts):
                coords.append((clamp(x, XMIN + 1.0, XMAX - 1.0),
                               clamp(y, YMIN + 1.0, YMAX - 1.0)))
                ang += random.uniform(-0.3, 0.3)
                x += math.cos(ang) * seg
                y += math.sin(ang) * seg
            polyline = make_polyline(coords, sr)
            gc.insertRow([
                polyline, next_pipe_id,
                random.choice([60, 100, 250, 600]),
                "Background",
            ])
            next_pipe_id += 1
    del gc
    log("  Gas_Pipe_L total: {0}".format(next_pipe_id))

    # ------------------------------------------------------------------
    # Power_Line_L: long zig-zag lines crossing highways at 5..10 deg.
    # ------------------------------------------------------------------
    power_fc = create_fc(
        gdb, "Power_Line_L", "POLYLINE", sr,
        [
            ("LineID", "LONG", None),
            ("Voltage_kV", "SHORT", None),
            ("EdgeCase", "TEXT", 32),
        ],
    )
    log("  Power_Line_L: {0} parallel + {1} acute + {2} background..."
        .format(N_POWER_LINES_PARALLEL, N_POWER_LINES_ACUTE,
                N_POWER_LINES_BACKGROUND))

    # Highway-class subset for acute crossings.
    HIGHWAY_CLASSES = ("Freeway", "Highway", "Parkway")
    highway_trunks = [
        coords for coords, klass in zip(trunk_endpoints, trunk_classes)
        if klass in HIGHWAY_CLASSES and len(coords) >= 4
    ]
    if not highway_trunks:
        highway_trunks = [c for c in trunk_endpoints if len(c) >= 4]

    next_line_id = 0
    pw_fields = ["SHAPE@", "LineID", "Voltage_kV", "EdgeCase"]
    with arcpy.da.InsertCursor(power_fc, pw_fields) as pc:
        # Parallel-along-roads: offset the road geometry sideways by ~30 m.
        ch = list(trunk_endpoints)
        random.shuffle(ch)
        n_par = 0
        for coords in ch:
            if n_par >= N_POWER_LINES_PARALLEL:
                break
            if len(coords) < 2:
                continue
            offset = random.choice([-1.0, 1.0]) * random.uniform(20.0, 40.0)
            offs = []
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
                L = math.hypot(dx, dy)
                if L < 1e-9:
                    nx_, ny_ = 0.0, 0.0
                else:
                    nx_ = -dy / L
                    ny_ = dx / L
                ox = coords[i][0] + nx_ * offset
                oy = coords[i][1] + ny_ * offset
                offs.append((clamp(ox, XMIN + 1.0, XMAX - 1.0),
                             clamp(oy, YMIN + 1.0, YMAX - 1.0)))
            polyline = make_polyline(offs, sr)
            pc.insertRow([
                polyline, next_line_id,
                random.choice([69, 138, 230, 500]),
                "Parallel_To_Road",
            ])
            next_line_id += 1
            n_par += 1

        # Acute-cross zone: 5..10 deg deviation across HIGHWAY trunks,
        # rendered as a multi-vertex zig-zag so the line jitters above and
        # below the trunk while staying within the acute band.
        log("  acute-cross zone (5-10 deg, zig-zag) on highways...")
        for _ in range(N_POWER_LINES_ACUTE):
            trunk = random.choice(highway_trunks)
            if len(trunk) < 4:
                continue
            mid_idx = len(trunk) // 2
            p1 = trunk[max(0, mid_idx - 1)]
            p2 = trunk[min(len(trunk) - 1, mid_idx + 1)]
            road_ang = math.atan2(p2[1] - p1[1], p2[0] - p1[0])
            dev_deg = random.uniform(POWER_ACUTE_DEG_MIN,
                                     POWER_ACUTE_DEG_MAX)
            dev = math.radians(dev_deg)
            if random.random() < 0.5:
                dev = -dev
            line_ang = road_ang + dev
            mx = (p1[0] + p2[0]) * 0.5
            my = (p1[1] + p2[1]) * 0.5
            length = random.uniform(2200.0, 3500.0)
            cos_a = math.cos(line_ang)
            sin_a = math.sin(line_ang)
            # Build zig-zag vertices along the line direction with small
            # perpendicular swings (typical pylon zig-zag).
            zz_amp = 18.0
            zz_pts = []
            for s in range(POWER_ZIGZAG_SEGMENTS + 1):
                t = -0.5 + s / float(POWER_ZIGZAG_SEGMENTS)
                bx = mx + cos_a * length * t
                by = my + sin_a * length * t
                # Perpendicular zig-zag (alternating sign).
                sign = 1.0 if (s % 2 == 0) else -1.0
                px = bx + (-sin_a) * zz_amp * sign
                py = by + (cos_a) * zz_amp * sign
                zz_pts.append((clamp(px, XMIN + 1.0, XMAX - 1.0),
                               clamp(py, YMIN + 1.0, YMAX - 1.0)))
            polyline = make_polyline(zz_pts, sr)
            pc.insertRow([
                polyline, next_line_id,
                random.choice([69, 138, 230, 500]),
                "Acute_Cross_Highway",
            ])
            next_line_id += 1

        # Background random power lines.
        for _ in range(N_POWER_LINES_BACKGROUND):
            x1 = XMIN + random.random() * WIDTH
            y1 = YMIN + random.random() * HEIGHT
            ang = random.uniform(0.0, 2.0 * math.pi)
            length = random.uniform(500.0, 3000.0)
            x2 = clamp(x1 + math.cos(ang) * length,
                       XMIN + 1.0, XMAX - 1.0)
            y2 = clamp(y1 + math.sin(ang) * length,
                       YMIN + 1.0, YMAX - 1.0)
            polyline = make_polyline([(x1, y1), (x2, y2)], sr)
            pc.insertRow([
                polyline, next_line_id,
                random.choice([69, 138, 230, 500]),
                "Background",
            ])
            next_line_id += 1
    del pc
    log("  Power_Line_L total: {0}".format(next_line_id))

    # ------------------------------------------------------------------
    # Label_Candidate_Boxes (P04) - heavy clusters at contour V-apexes
    # ------------------------------------------------------------------
    log("  Label_Candidate_Boxes ({0}) - V-apex clusters..."
        .format(N_LABEL_BOXES))
    labels_fc = create_fc(
        gdb, "Label_Candidate_Boxes", "POLYGON", sr,
        [
            ("LabelID", "LONG", None),
            ("LabelText", "TEXT", 32),
            ("Cluster", "SHORT", None),
            ("EdgeCase", "TEXT", 32),
        ],
    )
    # Build hotspots: prefer V-apex points from Stage 1 contours.  Pad
    # with random plain points if Stage 1 didn't emit enough V apexes.
    hotspots = list(contour_v_apexes)[:N_LABEL_VAPEX_HOTSPOTS]
    while len(hotspots) < N_LABEL_VAPEX_HOTSPOTS:
        hotspots.append((
            XMIN + WIDTH * random.random(),
            YMIN + HEIGHT * random.random(),
        ))
    next_lab = 0
    lab_fields = ["SHAPE@", "LabelID", "LabelText", "Cluster", "EdgeCase"]
    with arcpy.da.InsertCursor(labels_fc, lab_fields) as lc:
        for i in range(N_LABEL_BOXES):
            if random.random() < 0.78:
                ck = random.randint(0, len(hotspots) - 1)
                ccx, ccy = hotspots[ck]
                # Tight cluster radius - boxes pile right on the apex
                # to maximise pairwise AABB overlap pressure.
                cx_ = ccx + jitter(70.0)
                cy_ = ccy + jitter(50.0)
                edge = "VApex_Overlap"
            else:
                ck = -1
                cx_ = XMIN + random.random() * WIDTH
                cy_ = YMIN + random.random() * HEIGHT
                edge = "Normal"
            w = random.uniform(40.0, 120.0)
            h = random.uniform(12.0, 28.0)
            poly = make_polygon([
                (cx_ - w * 0.5, cy_ - h * 0.5),
                (cx_ + w * 0.5, cy_ - h * 0.5),
                (cx_ + w * 0.5, cy_ + h * 0.5),
                (cx_ - w * 0.5, cy_ + h * 0.5),
                (cx_ - w * 0.5, cy_ - h * 0.5),
            ], sr)
            lc.insertRow([
                poly, next_lab,
                "Label_{0:05d}".format(next_lab),
                ck, edge,
            ])
            next_lab += 1
    del lc
    log("  label boxes: {0} ({1} hotspots used)"
        .format(next_lab, len(hotspots)))

    return bld_fc, gas_fc, power_fc, labels_fc



# ===========================================================================
# STAGE 5 - Geological anomalies & cartographic edges
# ===========================================================================
#
# Springs:
#   * scattered springs anywhere in the mountain-foot elevation band
#   * 5 perfectly collinear springs along a steep contour (P06 SVD test)
#   * 1 isolated spring in the SW corner
#
# Map_Frame: clean rectangle inset 250 m.
# Custom_AOI: a polygon located so its sawtooth edge cuts through dense
#   mountainous contours, generating sub-decimeter slivers when contours
#   are clipped against it.
# ---------------------------------------------------------------------------


def _find_steep_contour_polyline(start_search_xy, length_target=2000.0):
    """Walk along a contour at a steep location and return the trace.

    We pick a point with high slope, then take the local elevation level
    and walk in both directions following an iso-elevation curve via a
    "rotate gradient by 90 deg" integrator.  This is approximate but
    yields a long line draped over a slope.
    """
    sx, sy = start_search_xy
    z0 = get_elevation(sx, sy)
    coords_fwd = [(sx, sy)]
    coords_bwd = []
    step = 30.0
    for direction in (+1.0, -1.0):
        x, y = sx, sy
        last_dx = 0.0
        last_dy = 0.0
        for _ in range(int(length_target / step)):
            gx, gy = get_gradient(x, y, h=10.0)
            gmag = math.hypot(gx, gy)
            if gmag < 1e-6:
                break
            # Tangent to contour (perpendicular to gradient).
            tx_ = -gy / gmag * direction
            ty_ = gx / gmag * direction
            # Maintain rough continuity with previous step.
            if last_dx * tx_ + last_dy * ty_ < 0.0:
                tx_ = -tx_
                ty_ = -ty_
            x += tx_ * step
            y += ty_ * step
            if not in_bbox(x, y, pad=20.0):
                break
            # Snap back toward original elevation.
            zh = get_elevation(x, y)
            err = zh - z0
            if abs(err) > 1.0 and gmag > 1e-6:
                # Move along -grad/+grad to correct.
                corr = err / gmag
                # Cap the correction so we don't fly off.
                if corr > 8.0:
                    corr = 8.0
                elif corr < -8.0:
                    corr = -8.0
                x -= (gx / gmag) * corr
                y -= (gy / gmag) * corr
            if direction > 0:
                coords_fwd.append((x, y))
            else:
                coords_bwd.append((x, y))
            last_dx, last_dy = tx_, ty_
    coords_bwd.reverse()
    return coords_bwd + coords_fwd


def _find_steep_seed():
    """Find a (x, y) with high slope (mountain flank)."""
    best_slope = -1.0
    best_xy = None
    for _ in range(2000):
        x = XMIN + random.random() * WIDTH
        y = YMIN + random.random() * HEIGHT
        s = get_slope(x, y)
        if s > best_slope:
            best_slope = s
            best_xy = (x, y)
            if best_slope > 0.4:
                break
    return best_xy


def build_stage5_anomalies_and_edges(gdb, sr, contours_fc):
    log("STAGE 5: Springs + Map_Frame + Custom_AOI + Grids")

    # ------------------------------------------------------------------
    # Springs
    # ------------------------------------------------------------------
    springs_fc = create_fc(
        gdb, "Springs", "POINT", sr,
        [
            ("SpringID", "LONG", None),
            ("Flow_LPS", "FLOAT", None),
            ("EdgeCase", "TEXT", 32),
        ],
    )
    next_spring = 0
    sp_fields = ["SHAPE@", "SpringID", "Flow_LPS", "EdgeCase"]
    with arcpy.da.InsertCursor(springs_fc, sp_fields) as sc:
        # Springs MUST sit on steep mathematical slopes - no plains, no
        # exact peaks.  Plugin 06 derives downhill rotation from the
        # local gradient, so a near-zero slope yields an undefined
        # rotation.  We therefore enforce slope > SPRING_SLOPE_MIN.
        log("  scattering {0} springs on steep slopes (min slope {1})..."
            .format(N_SPRINGS_RANDOM, SPRING_SLOPE_MIN))
        attempts = 0
        n_random = 0
        while n_random < N_SPRINGS_RANDOM and \
                attempts < N_SPRINGS_RANDOM * 60:
            attempts += 1
            x = XMIN + random.random() * WIDTH
            y = YMIN + random.random() * HEIGHT
            if is_on_plain(x, y):
                continue
            sl = get_slope(x, y)
            if sl < SPRING_SLOPE_MIN:
                continue
            # Reject if elevation is essentially at a peak summit
            # (within 50 m of the absolute local max we modelled).
            elev = get_elevation(x, y)
            if elev > ELEV_BASE + 1.85 * ELEV_AMPLITUDE * 0.5:
                # On the very tip of a peak: still allowed only if
                # there's measurable slope (which our filter already
                # enforces).  Keep an extra guard against perfect peaks.
                if sl < SPRING_SLOPE_MIN * 1.5:
                    continue
            sc.insertRow([
                make_point(x, y, sr),
                next_spring,
                random.uniform(0.1, 50.0),
                "Steep_Slope",
            ])
            next_spring += 1
            n_random += 1
        log("  spring slope count: {0} (after {1} attempts)"
            .format(n_random, attempts))

        # Collinear fault line: 5 springs perfectly along a slope contour.
        log("  injecting 5 perfectly collinear springs on a steep slope...")
        seed = _find_steep_seed()
        if seed is None:
            seed = (XMIN + WIDTH * 0.3, YMIN + HEIGHT * 0.7)
        contour_trace = _find_steep_contour_polyline(seed,
                                                     length_target=1500.0)
        # Pick two endpoints far enough apart along the trace and
        # interpolate exactly 5 collinear points between them.
        if len(contour_trace) >= 2:
            a = contour_trace[0]
            b = contour_trace[-1]
        else:
            a = (XMIN + WIDTH * 0.30, YMIN + HEIGHT * 0.70)
            b = (XMIN + WIDTH * 0.42, YMIN + HEIGHT * 0.78)
        for k in range(N_SPRINGS_FAULT_LINE):
            t = k / float(N_SPRINGS_FAULT_LINE - 1)
            # Exact double precision: same line equation for every point.
            x = a[0] + (b[0] - a[0]) * t
            y = a[1] + (b[1] - a[1]) * t
            sc.insertRow([
                make_point(x, y, sr),
                next_spring, 12.5,
                "Fault_Line_Collinear",
            ])
            next_spring += 1

        # Isolated spring far from anything (SW corner).
        sc.insertRow([
            make_point(XMIN + 50.0, YMIN + 50.0, sr),
            next_spring, 0.2, "Isolated",
        ])
        next_spring += 1
    del sc
    log("STAGE 5: springs total = {0}".format(next_spring))

    # ------------------------------------------------------------------
    # Map_Frame
    # ------------------------------------------------------------------
    frame_fc = create_fc(
        gdb, "Map_Frame", "POLYGON", sr,
        [
            ("FrameID", "LONG", None),
            ("Name", "TEXT", 64),
        ],
    )
    pad = 250.0
    frame_coords = [
        (XMIN + pad, YMIN + pad),
        (XMAX - pad, YMIN + pad),
        (XMAX - pad, YMAX - pad),
        (XMIN + pad, YMAX - pad),
        (XMIN + pad, YMIN + pad),
    ]
    with arcpy.da.InsertCursor(
            frame_fc, ["SHAPE@", "FrameID", "Name"]) as fc:
        fc.insertRow([make_polygon(frame_coords, sr), 0, "Master Frame"])
    del fc

    # ------------------------------------------------------------------
    # Custom_AOI: sawtooth edge biting into mountainous contours.
    # Place the AOI so it covers BOTH a chunk of the plain and slices
    # through one of the mountain peaks; the sawtooth lives along the
    # mountain side of the polygon.
    # ------------------------------------------------------------------
    aoi_fc = create_fc(
        gdb, "Custom_AOI", "POLYGON", sr,
        [
            ("AoiID", "LONG", None),
            ("Name", "TEXT", 64),
            ("EdgeCase", "TEXT", 32),
        ],
    )
    # Place AOI from plain center into the NW peak (Titan Ridge area).
    plain_cx = XMIN + PLAIN_CENTER_NORM[0] * WIDTH
    plain_cy = YMIN + PLAIN_CENTER_NORM[1] * HEIGHT
    nw_x = XMIN + PEAKS_NORM[0][0] * WIDTH
    nw_y = YMIN + PEAKS_NORM[0][1] * HEIGHT

    # AOI bounding rectangle: from plain center to a point past the NW peak.
    aoi_x0 = min(plain_cx, nw_x) - 800.0
    aoi_x1 = max(plain_cx, nw_x) + 800.0
    aoi_y0 = min(plain_cy, nw_y) - 800.0
    aoi_y1 = max(plain_cy, nw_y) + 800.0
    aoi_x0 = clamp(aoi_x0, XMIN + 50.0, XMAX - 50.0)
    aoi_x1 = clamp(aoi_x1, XMIN + 50.0, XMAX - 50.0)
    aoi_y0 = clamp(aoi_y0, YMIN + 50.0, YMAX - 50.0)
    aoi_y1 = clamp(aoi_y1, YMIN + 50.0, YMAX - 50.0)

    teeth_per_edge = max(1, N_AOI_SAWTOOTH_TEETH // 4)
    tooth_height = 0.05  # 5 cm -> microscopic slivers when clipped

    def sawtooth_along(start, end, n_teeth, perp_sign, axis):
        sx, sy = start
        ex, ey = end
        pts = []
        for i in range(n_teeth):
            t0 = i / float(n_teeth)
            t1 = (i + 0.5) / float(n_teeth)
            for tt in (t0, t1):
                bx = sx + (ex - sx) * tt
                by = sy + (ey - sy) * tt
                is_apex = (tt == t1)
                offset = tooth_height if is_apex else 0.0
                if axis == 'x':
                    pts.append((bx, by + offset * perp_sign))
                else:
                    pts.append((bx + offset * perp_sign, by))
        return pts

    aoi_pts = []
    aoi_pts.extend(sawtooth_along(
        (aoi_x0, aoi_y0), (aoi_x1, aoi_y0),
        teeth_per_edge, perp_sign=+1, axis='x'))
    aoi_pts.extend(sawtooth_along(
        (aoi_x1, aoi_y0), (aoi_x1, aoi_y1),
        teeth_per_edge, perp_sign=-1, axis='y'))
    aoi_pts.extend(sawtooth_along(
        (aoi_x1, aoi_y1), (aoi_x0, aoi_y1),
        teeth_per_edge, perp_sign=-1, axis='x'))
    aoi_pts.extend(sawtooth_along(
        (aoi_x0, aoi_y1), (aoi_x0, aoi_y0),
        teeth_per_edge, perp_sign=+1, axis='y'))
    aoi_pts.append(aoi_pts[0])
    with arcpy.da.InsertCursor(
            aoi_fc, ["SHAPE@", "AoiID", "Name", "EdgeCase"]) as ac:
        ac.insertRow([
            make_polygon(aoi_pts, sr),
            0, "Sawtooth AOI (plain -> NW peak)",
            "Sawtooth_Slivers",
        ])
    del ac
    log("  Custom_AOI sawtooth vertices ~{0}".format(len(aoi_pts)))


# ===========================================================================
# STAGE 5b - Grids (P07)
# ===========================================================================


def build_stage5_grids(gdb, projected_sr, geographic_sr):
    log("  P07: Index_Grid (projected, contiguous {0}x{1} sheets)..."
        .format(INDEX_SHEET_BLOCK_COLS, INDEX_SHEET_BLOCK_ROWS))
    grid_fc = create_fc(
        gdb, "Index_Grid", "POLYGON", projected_sr,
        [
            ("GridID", "LONG", None),
            ("Row", "LONG", None),
            ("Col", "LONG", None),
            ("Label", "TEXT", 16),
            ("SheetCode", "TEXT", 32),
            ("EdgeCase", "TEXT", 32),
        ],
    )
    # Build a contiguous block of map sheets centered on the bbox.
    # 5 cols x 4 rows = 20 sheets (within the 16..24 target range).
    sheets_total = INDEX_SHEET_BLOCK_COLS * INDEX_SHEET_BLOCK_ROWS
    sheet_w = WIDTH / float(INDEX_SHEET_BLOCK_COLS)
    sheet_h = HEIGHT / float(INDEX_SHEET_BLOCK_ROWS)
    next_id = 0
    with arcpy.da.InsertCursor(
            grid_fc,
            ["SHAPE@", "GridID", "Row", "Col", "Label",
             "SheetCode", "EdgeCase"]) as gc:
        for r in range(INDEX_SHEET_BLOCK_ROWS):
            for c in range(INDEX_SHEET_BLOCK_COLS):
                x0 = XMIN + c * sheet_w
                y0 = YMIN + r * sheet_h
                x1 = x0 + sheet_w
                y1 = y0 + sheet_h
                poly = make_polygon([
                    (x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0),
                ], projected_sr)
                # Sheet code like "TW-A1", "TW-B3" - common cartographic
                # naming for index-sheet products.
                col_letter = chr(ord('A') + c)
                gc.insertRow([
                    poly, next_id, r, c,
                    "{0}{1}".format(col_letter, r + 1),
                    "TW-{0}{1}".format(col_letter, r + 1),
                    "Index_Sheet",
                ])
                next_id += 1
    del gc
    log("  Index_Grid sheets emitted: {0} (target 16..24)"
        .format(next_id))
    log("  P07: GCS_Grid (WGS84, GCS-warning trigger)...")
    gcs_fc = create_fc(
        gdb, "GCS_Grid", "POLYGON", geographic_sr,
        [
            ("GridID", "LONG", None),
            ("Row", "LONG", None),
            ("Col", "LONG", None),
            ("EdgeCase", "TEXT", 32),
        ],
    )
    lon_min, lon_max = -116.0, -115.5
    lat_min, lat_max = 35.0, 35.5
    dlon = (lon_max - lon_min) / float(GCS_GRID_COLS)
    dlat = (lat_max - lat_min) / float(GCS_GRID_ROWS)
    next_gcs = 0
    with arcpy.da.InsertCursor(
            gcs_fc, ["SHAPE@", "GridID", "Row", "Col", "EdgeCase"]) as gc:
        for r in range(GCS_GRID_ROWS):
            for c in range(GCS_GRID_COLS):
                x0 = lon_min + c * dlon
                y0 = lat_min + r * dlat
                x1 = x0 + dlon
                y1 = y0 + dlat
                poly = make_polygon([
                    (x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0),
                ], geographic_sr)
                gc.insertRow([
                    poly, next_gcs, r, c, "GCS_Warning",
                ])
                next_gcs += 1

    del gc
    log("  P07: HUGE_Grid_Sparse (MAX_TICKS_PER_AXIS trigger)...")
    huge_fc = create_fc(
        gdb, "HUGE_Grid_Sparse", "POLYGON", projected_sr,
        [
            ("GridID", "LONG", None),
            ("Row", "LONG", None),
            ("Col", "LONG", None),
            ("MaxRow", "LONG", None),
            ("MaxCol", "LONG", None),
            ("EdgeCase", "TEXT", 32),
        ],
    )
    huge_cell_w = WIDTH / float(HUGE_GRID_TICKS)
    huge_cell_h = HEIGHT / float(HUGE_GRID_TICKS)
    next_huge = 0
    anchor_rc = [
        (0, 0),
        (0, HUGE_GRID_TICKS - 1),
        (HUGE_GRID_TICKS - 1, 0),
        (HUGE_GRID_TICKS - 1, HUGE_GRID_TICKS - 1),
    ]
    with arcpy.da.InsertCursor(
            huge_fc,
            ["SHAPE@", "GridID", "Row", "Col", "MaxRow", "MaxCol",
             "EdgeCase"]) as gc:
        for (r, c) in anchor_rc:
            x0 = XMIN + c * huge_cell_w
            y0 = YMIN + r * huge_cell_h
            x1 = x0 + huge_cell_w
            y1 = y0 + huge_cell_h
            poly = make_polygon([
                (x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0),
            ], projected_sr)
            gc.insertRow([
                poly, next_huge, r, c,
                HUGE_GRID_TICKS - 1, HUGE_GRID_TICKS - 1,
                "Anchor_Cap_Trigger",
            ])
            next_huge += 1

        for _ in range(HUGE_GRID_SAMPLE_LIMIT - len(anchor_rc)):
            r = random.randint(0, HUGE_GRID_TICKS - 1)
            c = random.randint(0, HUGE_GRID_TICKS - 1)
            x0 = XMIN + c * huge_cell_w
            y0 = YMIN + r * huge_cell_h
            x1 = x0 + huge_cell_w
            y1 = y0 + huge_cell_h
            poly = make_polygon([
                (x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0),
            ], projected_sr)
            gc.insertRow([
                poly, next_huge, r, c,
                HUGE_GRID_TICKS - 1, HUGE_GRID_TICKS - 1,
                "Sparse_Sample",
            ])
            next_huge += 1
            if next_huge % 25000 == 0:
                log("    HUGE sparse cells: {0}/{1}"
                    .format(next_huge, HUGE_GRID_SAMPLE_LIMIT))
    del gc
    log("  P07 complete: index={0} gcs={1} huge={2}"
        .format(next_id, next_gcs, next_huge))


# ===========================================================================
# Main entrypoint
# ===========================================================================


def main():
    out_dir = resolve_output_dir()
    log("Titan World 3.0 generator starting (topologically dependent).")
    log("Output dir: {0}".format(out_dir))
    log("Random seed: {0}".format(RANDOM_SEED))

    arcpy.env.overwriteOutput = True

    gdb_path = create_gdb(out_dir)
    projected_sr = get_projected_sr()
    geographic_sr = get_geographic_sr()

    # STAGE 1: Terrain Foundation
    _, contours_fc, contour_v_apexes = build_stage1_terrain(
        gdb_path, projected_sr)

    # STAGE 2: Hydrology (depends on STAGE 1 oracle)
    _, river_polylines_coords, river_vertex_pool, river_segment_pool = \
        build_stage2_hydrology(gdb_path, projected_sr)

    # STAGE 3: Roads (depends on STAGE 1 slope + STAGE 2 rivers)
    roads_fc, trunk_endpoints, trunk_classes = build_stage3_roads(
        gdb_path, projected_sr, river_polylines_coords,
        river_vertex_pool, river_segment_pool)

    # STAGE 4: Megacity (depends on STAGE 3 roads + STAGE 2 rivers + plain)
    build_stage4_megacity(
        gdb_path, projected_sr, roads_fc, trunk_endpoints,
        river_polylines_coords,
        trunk_classes=trunk_classes,
        contour_v_apexes=contour_v_apexes)

    # STAGE 5: Anomalies & cartographic edges
    build_stage5_anomalies_and_edges(gdb_path, projected_sr, contours_fc)
    build_stage5_grids(gdb_path, projected_sr, geographic_sr)

    log("Titan World 3.0 generation complete.")
    log("Geodatabase: {0}".format(gdb_path))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        import traceback
        tb = traceback.format_exc()
        try:
            arcpy.AddError(tb)
        except Exception:
            pass
        sys.stderr.write(tb)
        raise
