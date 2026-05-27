# -*- coding: utf-8 -*-
"""
Generate_TitanWorld_ArcMap.py
=============================

Procedural stress-test generator for the Carto ArcMap (Python 2.7 / arcpy 10.x)
plugin suite.

This script creates a single, very large File Geodatabase named
``TitanWorld_ArcMap.gdb`` populated with feature classes that exercise every
cartographic edge case targeted by Plugin01..Plugin07:

    * P01 BridgeCulvert        -> Roads + Drainage with true crossings,
                                  T-junctions, and collinear overlaps.
    * P02 RoadDeconflict       -> Power_Lines, Gas_Pipes, Buildings with
                                  pipe-under-road, acute angle crossings,
                                  and a dense 1km building cluster.
    * P03 ContourLabelOpt      -> Contours including a single half-million
                                  vertex "Titan Ridge" fractal/spiral.
    * P04 ElevationTextDecon   -> Label_Candidate_Boxes with heavy overlap.
    * P05 SafeContourCleaner   -> Map_Frame and Custom_AOI with a sawtooth
                                  edge designed to produce sub-decimeter
                                  slivers when contours are clipped.
    * P06 SpringRotation       -> Springs with five perfectly collinear
                                  points (SVD singular fault line) and an
                                  isolated spring far from any contour.
    * P07 BatchGridBuilder     -> A projected Index_Grid plus a geographic
                                  WGS84 grid (GCS warning trigger) and a
                                  very large grid intended to trip the
                                  MAX_TICKS_PER_AXIS safety cap.

The script is purposefully self-contained: only ``arcpy``, ``math``, ``os``,
``sys``, ``time``, and ``random`` are used.  It is intended to be executed
from inside an ArcMap 10.x Python 2.7 environment.  It is NOT meant to be
imported.

Usage::

    C:\\Python27\\ArcGIS10.8\\python.exe Generate_TitanWorld_ArcMap.py [out_dir]

If ``out_dir`` is omitted the script writes ``TitanWorld_ArcMap.gdb`` next to
itself.

Author: Carto / Titan World stress-test rig
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

# Deterministic chaos: same seed -> same Titan World.
RANDOM_SEED = 20260527
random.seed(RANDOM_SEED)

# 20 km x 20 km projected bounding box (UTM-style coordinates).
XMIN = 500000.0
YMIN = 3900000.0
XMAX = 520000.0
YMAX = 3920000.0
WIDTH = XMAX - XMIN     # 20000 m
HEIGHT = YMAX - YMIN    # 20000 m

# Projected coordinate system: WGS 1984 UTM Zone 11N (WKID 32611).
PROJECTED_WKID = 32611
GEOGRAPHIC_WKID = 4326

GDB_NAME = "TitanWorld_ArcMap.gdb"

# Feature counts (tuned for stress, not realism).
N_ROADS = 6000              # individual road line features
N_RIVERS = 3500             # individual drainage features
N_TRUE_CROSSINGS_TARGET = 50000  # informational; emerges from network density
N_T_JUNCTIONS = 1500
N_COLLINEAR_OVERLAPS = 400

N_POWER_LINES = 1200
N_GAS_PIPES = 1500
N_BUILDINGS_BACKGROUND = 4000
N_BUILDINGS_DENSE_CLUSTER = 10500   # along a single 1 km road segment

N_CONTOURS_BACKGROUND = 1500
TITAN_RIDGE_VERTICES = 500000        # half-million vertex single line
N_LABEL_BOXES = 12000

N_AOI_SAWTOOTH_TEETH = 4000          # very jagged AOI edge

N_SPRINGS_RANDOM = 2500
N_SPRINGS_FAULT_LINE = 5             # perfectly collinear -> SVD singular

# Grid stress: large enough to trip a MAX_TICKS_PER_AXIS cap (~5000).
HUGE_GRID_TICKS = 6000               # 6000 x 6000 cells nominally
HUGE_GRID_SAMPLE_LIMIT = 250000      # actually emit at most this many cells

# A modest projected grid that the grid builder should accept normally.
NORMAL_GRID_ROWS = 40
NORMAL_GRID_COLS = 40

# A geographic (GCS) grid that should trigger the "GCS not recommended" warning.
GCS_GRID_ROWS = 20
GCS_GRID_COLS = 20


# ---------------------------------------------------------------------------
# Logging helpers
# ---------------------------------------------------------------------------

_T0 = time.time()


def log(msg):
    """Print a timestamped message to both stdout and arcpy messages."""
    elapsed = time.time() - _T0
    line = "[{0:8.2f}s] {1}".format(elapsed, msg)
    try:
        arcpy.AddMessage(line)
    except Exception:
        pass
    print(line)


# ---------------------------------------------------------------------------
# Geometry helpers (pure Python, no numpy dependency required)
# ---------------------------------------------------------------------------

def clamp(v, lo, hi):
    if v < lo:
        return lo
    if v > hi:
        return hi
    return v


def jitter(amount):
    """Symmetric random jitter in [-amount, +amount]."""
    return (random.random() * 2.0 - 1.0) * amount


def point_in_bbox(x, y, pad=0.0):
    return (XMIN + pad) <= x <= (XMAX - pad) and (YMIN + pad) <= y <= (YMAX - pad)


def make_polyline(coords, sr):
    """coords: list of (x, y); returns arcpy.Polyline."""
    arr = arcpy.Array([arcpy.Point(x, y) for (x, y) in coords])
    return arcpy.Polyline(arr, sr)


def make_polygon(coords, sr):
    arr = arcpy.Array([arcpy.Point(x, y) for (x, y) in coords])
    return arcpy.Polygon(arr, sr)


def make_point(x, y, sr):
    return arcpy.PointGeometry(arcpy.Point(x, y), sr)


# ---------------------------------------------------------------------------
# Geodatabase / spatial reference setup
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
    """Create a feature class with a list of (name, type, length_or_none) fields."""
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
# P01 - Roads + Drainage (Bridges / Culverts edge cases)
# ===========================================================================
#
# We build two interleaved networks:
#
#   * Roads:    long, mostly E-W or N-S oriented polylines with random
#               kinks.  Many of them are intentionally placed so they
#               run perfectly along a river (collinear overlap).
#
#   * Rivers:   long sinuous polylines using a sin-wave perturbation;
#               gives many proper crossings with the road network.
#
# Edge cases injected:
#
#   * True crossings: ~50,000 emerging from network density.
#     With ~6000 roads and ~3500 rivers in a 20km box the expected
#     pairwise true-crossing count comfortably exceeds 50k.
#
#   * T-junctions: 1500 short road stubs whose end-vertex is placed
#     EXACTLY on a river vertex.  These should be rejected by a
#     "true crossing" filter and accepted by a naive "intersects" check.
#
#   * Collinear overlaps: 400 road segments coincident with a river
#     segment for a fixed length.  These create infinite intersection
#     sets and must be filtered out.
# ---------------------------------------------------------------------------


def _build_road_polyline(idx, sr):
    """Return (coords, attrs) for a single road feature."""
    # Choose orientation: 0=horizontal, 1=vertical, 2=diagonal.
    orient = random.choice([0, 0, 1, 1, 2])
    n_pts = random.randint(6, 18)

    if orient == 0:
        y0 = YMIN + random.random() * HEIGHT
        x0 = XMIN + random.random() * (WIDTH * 0.05)
        x_step = (WIDTH * random.uniform(0.2, 0.95)) / float(n_pts - 1)
        coords = []
        x, y = x0, y0
        for i in range(n_pts):
            coords.append((x + jitter(15.0), y + jitter(60.0)))
            x += x_step
    elif orient == 1:
        x0 = XMIN + random.random() * WIDTH
        y0 = YMIN + random.random() * (HEIGHT * 0.05)
        y_step = (HEIGHT * random.uniform(0.2, 0.95)) / float(n_pts - 1)
        coords = []
        x, y = x0, y0
        for i in range(n_pts):
            coords.append((x + jitter(60.0), y + jitter(15.0)))
            y += y_step
    else:
        x0 = XMIN + random.random() * WIDTH * 0.4
        y0 = YMIN + random.random() * HEIGHT * 0.4
        ang = random.uniform(-math.pi / 3.0, math.pi / 3.0)
        seg_len = random.uniform(800.0, 4500.0) / float(n_pts - 1)
        coords = []
        x, y = x0, y0
        for i in range(n_pts):
            coords.append((x + jitter(20.0), y + jitter(20.0)))
            x += math.cos(ang) * seg_len
            y += math.sin(ang) * seg_len

    coords = [(clamp(cx, XMIN + 1.0, XMAX - 1.0),
               clamp(cy, YMIN + 1.0, YMAX - 1.0)) for (cx, cy) in coords]

    attrs = {
        "RoadID": idx,
        "RoadClass": random.choice(["Primary", "Secondary", "Local",
                                    "Track", "Highway"]),
        "Surface": random.choice(["Paved", "Gravel", "Dirt"]),
        "SpeedLimit": random.choice([30, 40, 50, 60, 80, 100, 120]),
    }
    return coords, attrs


def _build_river_polyline(idx, sr):
    """Sinuous river polyline, returns (coords, attrs)."""
    n_pts = random.randint(20, 60)

    if random.random() < 0.5:
        # Mostly east-flowing.
        x0 = XMIN + random.random() * WIDTH * 0.05
        y_base = YMIN + random.random() * HEIGHT
        x_step = (WIDTH * random.uniform(0.5, 0.98)) / float(n_pts - 1)
        amp = random.uniform(80.0, 400.0)
        wavelength = random.uniform(800.0, 3500.0)
        coords = []
        x = x0
        for i in range(n_pts):
            y = y_base + amp * math.sin((x - x0) * 2.0 * math.pi / wavelength)
            coords.append((x + jitter(10.0), y + jitter(10.0)))
            x += x_step
    else:
        # Mostly south-flowing.
        y0 = YMIN + random.random() * HEIGHT * 0.05
        x_base = XMIN + random.random() * WIDTH
        y_step = (HEIGHT * random.uniform(0.5, 0.98)) / float(n_pts - 1)
        amp = random.uniform(80.0, 400.0)
        wavelength = random.uniform(800.0, 3500.0)
        coords = []
        y = y0
        for i in range(n_pts):
            x = x_base + amp * math.sin((y - y0) * 2.0 * math.pi / wavelength)
            coords.append((x + jitter(10.0), y + jitter(10.0)))
            y += y_step

    coords = [(clamp(cx, XMIN + 1.0, XMAX - 1.0),
               clamp(cy, YMIN + 1.0, YMAX - 1.0)) for (cx, cy) in coords]

    attrs = {
        "RiverID": idx,
        "Stream": random.choice(["Perennial", "Intermittent", "Ephemeral"]),
        "Name": "River_{0:05d}".format(idx),
    }
    return coords, attrs


def build_p01_roads_and_rivers(gdb, sr):
    log("P01: building Roads + Drainage networks...")

    roads_fc = create_fc(
        gdb, "Roads", "POLYLINE", sr,
        [
            ("RoadID", "LONG", None),
            ("RoadClass", "TEXT", 32),
            ("Surface", "TEXT", 16),
            ("SpeedLimit", "SHORT", None),
            ("EdgeCase", "TEXT", 32),
        ],
    )
    rivers_fc = create_fc(
        gdb, "Drainage", "POLYLINE", sr,
        [
            ("RiverID", "LONG", None),
            ("Stream", "TEXT", 16),
            ("Name", "TEXT", 64),
            ("EdgeCase", "TEXT", 32),
        ],
    )

    # ----- Rivers first; we'll reuse some river vertices for T-junctions -----
    river_vertex_pool = []   # list of (x, y) for T-junction snapping
    river_segment_pool = []  # list of ((x1, y1), (x2, y2)) for collinear overlap
    river_geoms_for_collinear = []

    river_fields = ["SHAPE@", "RiverID", "Stream", "Name", "EdgeCase"]
    with arcpy.da.InsertCursor(rivers_fc, river_fields) as rc:
        for i in range(N_RIVERS):
            coords, attrs = _build_river_polyline(i, sr)
            polyline = make_polyline(coords, sr)
            rc.insertRow([
                polyline,
                attrs["RiverID"],
                attrs["Stream"],
                attrs["Name"],
                "Normal",
            ])
            # Sample a few vertices and segments for later edge-case use.
            if len(river_vertex_pool) < 50000 and random.random() < 0.5:
                vx = random.choice(coords)
                river_vertex_pool.append(vx)
            if len(river_segment_pool) < 5000 and len(coords) >= 2:
                k = random.randint(0, len(coords) - 2)
                river_segment_pool.append((coords[k], coords[k + 1]))
                river_geoms_for_collinear.append(coords)
            if (i + 1) % 1000 == 0:
                log("  rivers inserted: {0}/{1}".format(i + 1, N_RIVERS))

    # ----- Roads -----
    road_fields = ["SHAPE@", "RoadID", "RoadClass", "Surface",
                   "SpeedLimit", "EdgeCase"]
    next_road_id = 0
    with arcpy.da.InsertCursor(roads_fc, road_fields) as rc:
        # Background random roads (drives the true-crossing count).
        for i in range(N_ROADS):
            coords, attrs = _build_road_polyline(next_road_id, sr)
            polyline = make_polyline(coords, sr)
            rc.insertRow([
                polyline,
                attrs["RoadID"],
                attrs["RoadClass"],
                attrs["Surface"],
                attrs["SpeedLimit"],
                "Normal",
            ])
            next_road_id += 1
            if (i + 1) % 1000 == 0:
                log("  roads inserted: {0}/{1}".format(i + 1, N_ROADS))

        # T-junction stubs: a tiny road segment whose endpoint is exactly
        # on a river vertex.  This is the classic "end-touches" false positive
        # that the bridge/culvert plugin's true-crossing filter must reject.
        log("  injecting {0} T-junction stubs...".format(N_T_JUNCTIONS))
        for i in range(N_T_JUNCTIONS):
            if not river_vertex_pool:
                break
            tx, ty = random.choice(river_vertex_pool)
            ang = random.uniform(0.0, 2.0 * math.pi)
            length = random.uniform(20.0, 120.0)
            ex = tx + math.cos(ang) * length
            ey = ty + math.sin(ang) * length
            ex = clamp(ex, XMIN + 1.0, XMAX - 1.0)
            ey = clamp(ey, YMIN + 1.0, YMAX - 1.0)
            coords = [(ex, ey), (tx, ty)]  # endpoint AT the river vertex
            polyline = make_polyline(coords, sr)
            rc.insertRow([
                polyline, next_road_id, "TJunction_Stub",
                "Paved", 30, "T_Junction",
            ])
            next_road_id += 1

        # Collinear overlaps: a road that lies exactly along a river segment
        # for the full length of that segment, then continues a bit further.
        log("  injecting {0} collinear road/river overlaps..."
            .format(N_COLLINEAR_OVERLAPS))
        for i in range(min(N_COLLINEAR_OVERLAPS, len(river_segment_pool))):
            (rx1, ry1), (rx2, ry2) = river_segment_pool[i]
            # Extend the segment along its own direction so the road also
            # reaches beyond the river segment (still collinear).
            dx = rx2 - rx1
            dy = ry2 - ry1
            seg_len = math.hypot(dx, dy)
            if seg_len < 1e-6:
                continue
            ux, uy = dx / seg_len, dy / seg_len
            ext = random.uniform(50.0, 250.0)
            ax = rx1 - ux * ext
            ay = ry1 - uy * ext
            bx = rx2 + ux * ext
            by = ry2 + uy * ext
            ax = clamp(ax, XMIN + 1.0, XMAX - 1.0)
            ay = clamp(ay, YMIN + 1.0, YMAX - 1.0)
            bx = clamp(bx, XMIN + 1.0, XMAX - 1.0)
            by = clamp(by, YMIN + 1.0, YMAX - 1.0)
            coords = [(ax, ay), (rx1, ry1), (rx2, ry2), (bx, by)]
            polyline = make_polyline(coords, sr)
            rc.insertRow([
                polyline, next_road_id, "Collinear_Overlay",
                "Paved", 50, "Collinear_Overlap",
            ])
            next_road_id += 1

    log("P01: complete. roads={0} rivers={1}".format(next_road_id, N_RIVERS))
    return roads_fc, rivers_fc



# ===========================================================================
# P02 - Road Deconflict (Power_Lines, Gas_Pipes, Buildings)
# ===========================================================================
#
# Edge cases:
#   * Gas pipes running EXACTLY under road centerlines (full coincident).
#   * Power lines crossing roads at acute (<= 15 degree) angles.
#   * 10,000+ buildings tightly packed along a SINGLE 1km road segment to
#     stress GenerateNearTable's spatial index.
# ---------------------------------------------------------------------------


def _sample_road_geometries(roads_fc, max_count=4000):
    """Return a list of polyline geometries from the roads FC."""
    out = []
    with arcpy.da.SearchCursor(roads_fc, ["SHAPE@", "OID@", "EdgeCase"]) as sc:
        for row in sc:
            shp, _oid, edge = row
            if edge != "Normal":
                continue
            if shp is None or shp.length < 200.0:
                continue
            out.append(shp)
            if len(out) >= max_count:
                break
    return out


def _polyline_coords(polyline):
    coords = []
    for part in polyline:
        for pt in part:
            if pt is None:
                continue
            coords.append((pt.X, pt.Y))
    return coords


def build_p02_power_gas_buildings(gdb, sr, roads_fc):
    log("P02: building Power_Lines, Gas_Pipes, Buildings...")

    power_fc = create_fc(
        gdb, "Power_Lines", "POLYLINE", sr,
        [
            ("LineID", "LONG", None),
            ("Voltage_kV", "SHORT", None),
            ("EdgeCase", "TEXT", 32),
        ],
    )
    gas_fc = create_fc(
        gdb, "Gas_Pipes", "POLYLINE", sr,
        [
            ("PipeID", "LONG", None),
            ("Pressure_PSI", "SHORT", None),
            ("EdgeCase", "TEXT", 32),
        ],
    )
    bld_fc = create_fc(
        gdb, "Buildings", "POLYGON", sr,
        [
            ("BldID", "LONG", None),
            ("Height_m", "FLOAT", None),
            ("EdgeCase", "TEXT", 32),
        ],
    )

    road_shapes = _sample_road_geometries(roads_fc, max_count=4000)
    log("  sampled {0} background road geometries".format(len(road_shapes)))

    # ----- Gas pipes: many of them lie EXACTLY on a road centerline -----
    next_pipe_id = 0
    with arcpy.da.InsertCursor(
            gas_fc, ["SHAPE@", "PipeID", "Pressure_PSI", "EdgeCase"]) as gc:
        # Pipe-under-road: copy a road geometry verbatim.
        n_under = min(len(road_shapes), int(N_GAS_PIPES * 0.4))
        for i in range(n_under):
            road_shape = road_shapes[i]
            coords = _polyline_coords(road_shape)
            if len(coords) < 2:
                continue
            polyline = make_polyline(coords, sr)
            gc.insertRow([polyline, next_pipe_id,
                          random.choice([60, 100, 250, 600]),
                          "Pipe_Under_Road"])
            next_pipe_id += 1

        # Random gas pipes elsewhere.
        for i in range(N_GAS_PIPES - n_under):
            n_pts = random.randint(4, 12)
            x = XMIN + random.random() * WIDTH
            y = YMIN + random.random() * HEIGHT
            ang = random.uniform(0.0, 2.0 * math.pi)
            seg = random.uniform(80.0, 400.0)
            coords = []
            for j in range(n_pts):
                coords.append((clamp(x, XMIN + 1.0, XMAX - 1.0),
                               clamp(y, YMIN + 1.0, YMAX - 1.0)))
                ang += random.uniform(-0.3, 0.3)
                x += math.cos(ang) * seg
                y += math.sin(ang) * seg
            polyline = make_polyline(coords, sr)
            gc.insertRow([polyline, next_pipe_id,
                          random.choice([60, 100, 250, 600]), "Normal"])
            next_pipe_id += 1

    # ----- Power lines: many cross roads at acute angles -----
    next_line_id = 0
    with arcpy.da.InsertCursor(
            power_fc, ["SHAPE@", "LineID", "Voltage_kV", "EdgeCase"]) as pc:
        # Acute-angle crossings: pick a road, sample a midpoint, draw a
        # power line whose direction is within +/- 5..15 degrees of the
        # road's local tangent.
        n_acute = min(len(road_shapes), int(N_POWER_LINES * 0.5))
        for i in range(n_acute):
            road_shape = road_shapes[i % len(road_shapes)]
            coords = _polyline_coords(road_shape)
            if len(coords) < 2:
                continue
            mid_idx = len(coords) // 2
            (x1, y1) = coords[max(0, mid_idx - 1)]
            (x2, y2) = coords[min(len(coords) - 1, mid_idx + 1)]
            road_ang = math.atan2(y2 - y1, x2 - x1)
            # Acute deviation: 3 - 12 degrees.
            dev = math.radians(random.uniform(3.0, 12.0))
            if random.random() < 0.5:
                dev = -dev
            line_ang = road_ang + dev
            mx = (x1 + x2) * 0.5
            my = (y1 + y2) * 0.5
            length = random.uniform(800.0, 2500.0)
            ax = mx - math.cos(line_ang) * length * 0.5
            ay = my - math.sin(line_ang) * length * 0.5
            bx = mx + math.cos(line_ang) * length * 0.5
            by = my + math.sin(line_ang) * length * 0.5
            ax = clamp(ax, XMIN + 1.0, XMAX - 1.0)
            ay = clamp(ay, YMIN + 1.0, YMAX - 1.0)
            bx = clamp(bx, XMIN + 1.0, XMAX - 1.0)
            by = clamp(by, YMIN + 1.0, YMAX - 1.0)
            polyline = make_polyline([(ax, ay), (mx, my), (bx, by)], sr)
            pc.insertRow([polyline, next_line_id,
                          random.choice([69, 138, 230, 500]),
                          "Acute_Cross"])
            next_line_id += 1

        # Background random power lines.
        for i in range(N_POWER_LINES - n_acute):
            x1 = XMIN + random.random() * WIDTH
            y1 = YMIN + random.random() * HEIGHT
            ang = random.uniform(0.0, 2.0 * math.pi)
            length = random.uniform(500.0, 3500.0)
            x2 = clamp(x1 + math.cos(ang) * length, XMIN + 1.0, XMAX - 1.0)
            y2 = clamp(y1 + math.sin(ang) * length, YMIN + 1.0, YMAX - 1.0)
            polyline = make_polyline([(x1, y1), (x2, y2)], sr)
            pc.insertRow([polyline, next_line_id,
                          random.choice([69, 138, 230, 500]), "Normal"])
            next_line_id += 1

    # ----- Buildings: 10,000+ tightly packed along a 1km road segment -----
    # Pick a single road that is at least 1.2 km long; if none, fall back
    # to a synthetic horizontal segment.
    cluster_segment = None
    for shp in road_shapes:
        if shp.length >= 1200.0:
            coords = _polyline_coords(shp)
            if len(coords) >= 2:
                cluster_segment = coords
                break
    if cluster_segment is None:
        cy_seg = YMIN + HEIGHT * 0.5
        cluster_segment = [
            (XMIN + WIDTH * 0.4, cy_seg),
            (XMIN + WIDTH * 0.5, cy_seg),  # 2km horizontal
        ]

    log("  dense building cluster anchored on a road of length ~{0:.0f} m"
        .format(_polyline_total_length(cluster_segment)))

    next_bld_id = 0
    with arcpy.da.InsertCursor(
            bld_fc, ["SHAPE@", "BldID", "Height_m", "EdgeCase"]) as bc:
        # Background buildings: sparse across the bbox.
        for i in range(N_BUILDINGS_BACKGROUND):
            cx = XMIN + random.random() * WIDTH
            cy = YMIN + random.random() * HEIGHT
            w = random.uniform(8.0, 60.0)
            h = random.uniform(8.0, 60.0)
            polygon = make_polygon([
                (cx - w * 0.5, cy - h * 0.5),
                (cx + w * 0.5, cy - h * 0.5),
                (cx + w * 0.5, cy + h * 0.5),
                (cx - w * 0.5, cy + h * 0.5),
                (cx - w * 0.5, cy - h * 0.5),
            ], sr)
            bc.insertRow([polygon, next_bld_id,
                          random.uniform(3.0, 30.0), "Normal"])
            next_bld_id += 1
            if (i + 1) % 2000 == 0:
                log("  background buildings: {0}".format(i + 1))

        # Dense cluster along the chosen road.
        positions = _sample_along_polyline(cluster_segment,
                                            target_length=1000.0,
                                            count=N_BUILDINGS_DENSE_CLUSTER)
        for i, (px, py, tx, ty) in enumerate(positions):
            # Building is a tiny rectangle offset perpendicular to the road.
            nx, ny = -ty, tx  # perpendicular unit vector
            offset = random.choice([-1.0, 1.0]) * random.uniform(8.0, 35.0)
            ox = px + nx * offset
            oy = py + ny * offset
            w = random.uniform(4.0, 9.0)
            h = random.uniform(4.0, 9.0)
            # Rotate the rectangle to align with the road.
            cos_a, sin_a = tx, ty
            corners_local = [
                (-w * 0.5, -h * 0.5),
                (w * 0.5, -h * 0.5),
                (w * 0.5, h * 0.5),
                (-w * 0.5, h * 0.5),
            ]
            poly_pts = []
            for (lx, ly) in corners_local:
                gx = ox + lx * cos_a - ly * sin_a
                gy = oy + lx * sin_a + ly * cos_a
                gx = clamp(gx, XMIN + 1.0, XMAX - 1.0)
                gy = clamp(gy, YMIN + 1.0, YMAX - 1.0)
                poly_pts.append((gx, gy))
            poly_pts.append(poly_pts[0])
            polygon = make_polygon(poly_pts, sr)
            bc.insertRow([polygon, next_bld_id,
                          random.uniform(3.0, 12.0),
                          "Dense_Cluster_1km"])
            next_bld_id += 1
            if (i + 1) % 2000 == 0:
                log("  dense cluster buildings: {0}/{1}"
                    .format(i + 1, N_BUILDINGS_DENSE_CLUSTER))

    log("P02: complete. power_lines={0} gas_pipes={1} buildings={2}"
        .format(next_line_id, next_pipe_id, next_bld_id))
    return power_fc, gas_fc, bld_fc


def _polyline_total_length(coords):
    total = 0.0
    for i in range(1, len(coords)):
        x1, y1 = coords[i - 1]
        x2, y2 = coords[i]
        total += math.hypot(x2 - x1, y2 - y1)
    return total


def _sample_along_polyline(coords, target_length, count):
    """Yield ``count`` points along the first ``target_length`` meters of the
    polyline.  Returns list of (x, y, tx, ty) where (tx, ty) is the local
    unit tangent.
    """
    # Pre-compute cumulative length.
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
        # find segment containing s
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
        dx, dy = x2 - x1, y2 - y1
        L = math.hypot(dx, dy)
        if L < 1e-9:
            tx, ty = 1.0, 0.0
        else:
            tx, ty = dx / L, dy / L
        out.append((px, py, tx, ty))
    return out



# ===========================================================================
# P03 / P04 - Contours + Label Candidate Boxes
# ===========================================================================
#
# * Contours: a few thousand reasonable contour lines (sinusoidal terrain
#   approximation) plus ONE colossal "Titan Ridge" line whose geometry has
#   500,000+ vertices generated from a fractal-perturbed logarithmic
#   spiral.  This single feature is meant to push arcpy's vertex
#   marshalling and any in-memory copy in the contour-label optimizer to
#   the breaking point.
#
# * Label_Candidate_Boxes: thousands of small axis-aligned rectangles with
#   heavy overlap, designed to stress the numpy AABB collision logic in
#   Plugin04_ElevationTextDeconflict.
# ---------------------------------------------------------------------------


def _terrain_elevation(x, y):
    """Synthetic terrain: layered sin waves giving rolling contours."""
    nx = (x - XMIN) / WIDTH
    ny = (y - YMIN) / HEIGHT
    z = (
        300.0 * math.sin(nx * 6.28318 * 1.5)
        + 200.0 * math.cos(ny * 6.28318 * 1.2)
        + 120.0 * math.sin((nx + ny) * 6.28318 * 2.7)
        + 60.0 * math.sin(nx * ny * 19.0)
        + 1500.0
    )
    return z


def _build_background_contour(idx, sr):
    """Build a single moderately complex contour polyline."""
    n_pts = random.randint(60, 300)
    # Choose a base elevation in 50 m steps.
    base_elev = round(random.uniform(900.0, 2100.0) / 50.0) * 50.0
    # Walk along the terrain following a noisy isoline-ish path.
    x = XMIN + random.random() * WIDTH
    y = YMIN + random.random() * HEIGHT
    ang = random.uniform(0.0, 2.0 * math.pi)
    coords = []
    for i in range(n_pts):
        coords.append((clamp(x, XMIN + 1.0, XMAX - 1.0),
                       clamp(y, YMIN + 1.0, YMAX - 1.0)))
        ang += random.uniform(-0.4, 0.4)
        step = random.uniform(20.0, 80.0)
        x += math.cos(ang) * step
        y += math.sin(ang) * step
    return coords, base_elev


def _build_titan_ridge(n_vertices, sr):
    """Generate a single polyline with ``n_vertices`` vertices.

    Geometry: a logarithmic spiral, modulated by a fractal sum of sines
    (poor man's 1D Brownian noise), then scaled to fit inside the bbox.
    The resulting line self-approaches and curls densely - exactly the
    kind of pathological topology that triggers worst-case behavior in
    spatial indexes and label-placement engines.
    """
    cx = XMIN + WIDTH * 0.5
    cy = YMIN + HEIGHT * 0.5
    # Spiral parameters tuned so the radius stays inside ~9 km from center.
    a = 1.0
    b = 0.05
    # Pre-generate fractal noise coefficients.
    octaves = [
        (1.0, 0.013),
        (0.5, 0.041),
        (0.25, 0.137),
        (0.12, 0.421),
        (0.06, 1.113),
        (0.03, 3.371),
    ]
    max_radius = min(WIDTH, HEIGHT) * 0.45
    coords = []
    # Using append in a tight loop; this is fine in CPython 2.7 but we
    # still keep it lean.
    append = coords.append
    sin = math.sin
    cos = math.cos
    exp = math.exp

    for i in range(n_vertices):
        theta = i * 0.0008  # ~400 radians total over 500k pts -> ~64 turns
        r = a * exp(b * theta)
        if r > max_radius:
            r = max_radius
        # Add fractal radial perturbation.
        pert = 0.0
        for amp, freq in octaves:
            pert += amp * sin(theta * freq * 12.566 + i * 0.0001)
        rr = r * (1.0 + 0.07 * pert)
        x = cx + rr * cos(theta)
        y = cy + rr * sin(theta)
        # Gentle clamp; we *want* coordinates in-range without flattening
        # the spiral structure.
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


def build_p03_p04_contours_and_labels(gdb, sr):
    log("P03: building Contours (incl. Titan Ridge)...")

    contours_fc = create_fc(
        gdb, "Contours", "POLYLINE", sr,
        [
            ("ContourID", "LONG", None),
            ("Elevation", "DOUBLE", None),
            ("Index_Contour", "SHORT", None),
            ("EdgeCase", "TEXT", 32),
        ],
    )

    next_id = 0
    with arcpy.da.InsertCursor(
            contours_fc,
            ["SHAPE@", "ContourID", "Elevation",
             "Index_Contour", "EdgeCase"]) as cc:
        # Background contours.
        for i in range(N_CONTOURS_BACKGROUND):
            coords, elev = _build_background_contour(next_id, sr)
            polyline = make_polyline(coords, sr)
            cc.insertRow([
                polyline, next_id, elev,
                1 if int(elev) % 100 == 0 else 0,
                "Normal",
            ])
            next_id += 1
            if (i + 1) % 250 == 0:
                log("  contours: {0}/{1}".format(i + 1, N_CONTOURS_BACKGROUND))

        # Titan Ridge: half-million vertices in a single feature.
        log("  building Titan Ridge with {0} vertices...".format(
            TITAN_RIDGE_VERTICES))
        titan_coords = _build_titan_ridge(TITAN_RIDGE_VERTICES, sr)
        # arcpy.Array can take a Python list of arcpy.Point.  For very
        # large geometries we batch-construct to avoid intermediate
        # tuple boxing surprises.
        titan_arr = arcpy.Array()
        add = titan_arr.add
        for (x, y) in titan_coords:
            add(arcpy.Point(x, y))
        titan_polyline = arcpy.Polyline(titan_arr, sr)
        cc.insertRow([
            titan_polyline, next_id,
            1850.0, 1, "Titan_Ridge",
        ])
        next_id += 1
        log("  Titan Ridge inserted (length ~{0:.1f} m)".format(
            titan_polyline.length))

    log("P03: contours complete.  total={0}".format(next_id))

    # ----- P04: heavily overlapping label candidate boxes -----
    log("P04: building Label_Candidate_Boxes ({0})...".format(N_LABEL_BOXES))
    labels_fc = create_fc(
        gdb, "Label_Candidate_Boxes", "POLYGON", sr,
        [
            ("LabelID", "LONG", None),
            ("LabelText", "TEXT", 32),
            ("Cluster", "SHORT", None),
            ("EdgeCase", "TEXT", 32),
        ],
    )

    # Hot spots: 8 cluster centers where boxes pile on each other.
    cluster_centers = [
        (XMIN + WIDTH * random.random(), YMIN + HEIGHT * random.random())
        for _ in range(8)
    ]

    next_lab = 0
    with arcpy.da.InsertCursor(
            labels_fc,
            ["SHAPE@", "LabelID", "LabelText", "Cluster", "EdgeCase"]) as lc:
        for i in range(N_LABEL_BOXES):
            if random.random() < 0.65:
                # Heavy clustering -> high overlap pressure on AABB tests.
                ck = random.randint(0, len(cluster_centers) - 1)
                ccx, ccy = cluster_centers[ck]
                cx = ccx + jitter(180.0)
                cy = ccy + jitter(120.0)
                edge = "Cluster_Overlap"
            else:
                ck = -1
                cx = XMIN + random.random() * WIDTH
                cy = YMIN + random.random() * HEIGHT
                edge = "Normal"
            w = random.uniform(40.0, 120.0)
            h = random.uniform(12.0, 28.0)
            poly = make_polygon([
                (cx - w * 0.5, cy - h * 0.5),
                (cx + w * 0.5, cy - h * 0.5),
                (cx + w * 0.5, cy + h * 0.5),
                (cx - w * 0.5, cy + h * 0.5),
                (cx - w * 0.5, cy - h * 0.5),
            ], sr)
            lc.insertRow([
                poly, next_lab,
                "Label_{0:05d}".format(next_lab),
                ck, edge,
            ])
            next_lab += 1
            if (i + 1) % 2000 == 0:
                log("  labels: {0}/{1}".format(i + 1, N_LABEL_BOXES))

    log("P04: complete. label_boxes={0}".format(next_lab))
    return contours_fc, labels_fc



# ===========================================================================
# P05 - Safe Contour Cleaner (Map_Frame + Custom_AOI)
# ===========================================================================
#
# Map_Frame: a clean rectangle inset 250 m from the bbox edge.
# Custom_AOI: a polygon whose outer ring is a sawtooth (high frequency,
# tiny amplitude).  When contours are clipped against this AOI the
# resulting line endings can be < 0.1 m apart, producing the microscopic
# "sliver" artefacts the cleaner is designed to remove.
# ---------------------------------------------------------------------------


def build_p05_frame_and_aoi(gdb, sr):
    log("P05: building Map_Frame and Custom_AOI (sawtooth slivers)...")

    frame_fc = create_fc(
        gdb, "Map_Frame", "POLYGON", sr,
        [
            ("FrameID", "LONG", None),
            ("Name", "TEXT", 64),
        ],
    )
    aoi_fc = create_fc(
        gdb, "Custom_AOI", "POLYGON", sr,
        [
            ("AoiID", "LONG", None),
            ("Name", "TEXT", 64),
            ("EdgeCase", "TEXT", 32),
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

    # ----- Sawtooth AOI -----
    # Rectangle nominally inset 1 km, but every edge replaced by a high-
    # frequency triangular sawtooth.  Tooth width ~ (edge_len / N_TEETH/4),
    # tooth height ~ 0.05 m -> sub-decimeter slivers when clipped.
    inset = 1000.0
    cx_min = XMIN + inset
    cx_max = XMAX - inset
    cy_min = YMIN + inset
    cy_max = YMAX - inset

    teeth_per_edge = max(1, N_AOI_SAWTOOTH_TEETH // 4)
    tooth_height = 0.05  # 5 cm -> microscopic slivers when clipped

    def sawtooth_along(start, end, n_teeth, perp_sign, axis):
        """Return list of points along the segment with sawtooth pattern.

        ``axis`` is 'x' (segment varies in x) or 'y' (varies in y);
        ``perp_sign`` (+1/-1) chooses which side the teeth poke out.
        """
        sx, sy = start
        ex, ey = end
        pts = []
        for i in range(n_teeth):
            t0 = i / float(n_teeth)
            t1 = (i + 0.5) / float(n_teeth)
            for tt in (t0, t1):
                bx = sx + (ex - sx) * tt
                by = sy + (ey - sy) * tt
                # Apex points are on the half-step.
                is_apex = (tt == t1)
                offset = tooth_height if is_apex else 0.0
                if axis == 'x':
                    pts.append((bx, by + offset * perp_sign))
                else:
                    pts.append((bx + offset * perp_sign, by))
        return pts

    aoi_pts = []
    aoi_pts.extend(sawtooth_along(
        (cx_min, cy_min), (cx_max, cy_min), teeth_per_edge,
        perp_sign=+1, axis='x'))
    aoi_pts.extend(sawtooth_along(
        (cx_max, cy_min), (cx_max, cy_max), teeth_per_edge,
        perp_sign=-1, axis='y'))
    aoi_pts.extend(sawtooth_along(
        (cx_max, cy_max), (cx_min, cy_max), teeth_per_edge,
        perp_sign=-1, axis='x'))
    aoi_pts.extend(sawtooth_along(
        (cx_min, cy_max), (cx_min, cy_min), teeth_per_edge,
        perp_sign=+1, axis='y'))
    aoi_pts.append(aoi_pts[0])

    with arcpy.da.InsertCursor(
            aoi_fc, ["SHAPE@", "AoiID", "Name", "EdgeCase"]) as ac:
        ac.insertRow([
            make_polygon(aoi_pts, sr),
            0, "Sawtooth AOI", "Sawtooth_Slivers",
        ])

    log("P05: complete. AOI vertex count ~{0}".format(len(aoi_pts)))
    return frame_fc, aoi_fc


# ===========================================================================
# P06 - Spring Rotation (Springs)
# ===========================================================================
#
# Edge cases:
#   * Random scattered springs (typical workload).
#   * One "Fault Line" of EXACTLY 5 perfectly collinear springs - this is
#     the configuration that yields a singular covariance matrix and
#     causes naive SVD-based principal-axis fitting to fail (or warn
#     about degenerate singular values).
#   * One "Isolated Spring" placed > 2 km from any other point, so it
#     sits well outside the contour neighborhood.
# ---------------------------------------------------------------------------


def build_p06_springs(gdb, sr):
    log("P06: building Springs...")
    springs_fc = create_fc(
        gdb, "Springs", "POINT", sr,
        [
            ("SpringID", "LONG", None),
            ("Flow_LPS", "FLOAT", None),
            ("EdgeCase", "TEXT", 32),
        ],
    )

    next_id = 0
    with arcpy.da.InsertCursor(
            springs_fc,
            ["SHAPE@", "SpringID", "Flow_LPS", "EdgeCase"]) as sc:
        for i in range(N_SPRINGS_RANDOM):
            x = XMIN + random.random() * WIDTH
            y = YMIN + random.random() * HEIGHT
            sc.insertRow([
                make_point(x, y, sr),
                next_id, random.uniform(0.1, 50.0), "Normal",
            ])
            next_id += 1

        # Perfectly collinear fault line (SVD singular trigger).
        fx0 = XMIN + WIDTH * 0.25
        fy0 = YMIN + HEIGHT * 0.25
        fx1 = XMIN + WIDTH * 0.75
        fy1 = YMIN + HEIGHT * 0.75
        for k in range(N_SPRINGS_FAULT_LINE):
            t = k / float(N_SPRINGS_FAULT_LINE - 1)
            # Use exact double arithmetic without any jitter.
            x = fx0 + (fx1 - fx0) * t
            y = fy0 + (fy1 - fy0) * t
            sc.insertRow([
                make_point(x, y, sr),
                next_id, 12.5, "Fault_Line_Collinear",
            ])
            next_id += 1

        # Isolated spring far from contour mass.
        iso_x = XMIN + 50.0   # tucked into the SW corner
        iso_y = YMIN + 50.0
        sc.insertRow([
            make_point(iso_x, iso_y, sr),
            next_id, 0.2, "Isolated",
        ])
        next_id += 1

    log("P06: complete. springs={0} (incl. {1} collinear + 1 isolated)"
        .format(next_id, N_SPRINGS_FAULT_LINE))
    return springs_fc



# ===========================================================================
# P07 - Batch Grid Builder (Index_Grid + GCS_Grid + HUGE_Grid)
# ===========================================================================
#
# We emit three grid feature classes:
#
#   * Index_Grid   - normal projected grid, valid input.
#   * GCS_Grid     - polygons in WGS84 lat/lon, intended to make the grid
#                    builder warn about a Geographic CS being unsuitable
#                    for distance/area calculations.
#   * HUGE_Grid_Sparse - tile cells whose row/col indices imply an axis
#                        tick count of HUGE_GRID_TICKS, exceeding any
#                        reasonable MAX_TICKS_PER_AXIS safety cap.  We do
#                        NOT actually emit HUGE_GRID_TICKS^2 polygons (that
#                        would be 36 million); we emit a sparse subset
#                        covering the same row/col index range so the
#                        cap-detection logic can read the implied extent.
# ---------------------------------------------------------------------------


def build_p07_grids(gdb, projected_sr, geographic_sr):
    log("P07: building Index_Grid (projected)...")
    grid_fc = create_fc(
        gdb, "Index_Grid", "POLYGON", projected_sr,
        [
            ("GridID", "LONG", None),
            ("Row", "LONG", None),
            ("Col", "LONG", None),
            ("Label", "TEXT", 16),
            ("EdgeCase", "TEXT", 32),
        ],
    )
    cell_w = WIDTH / float(NORMAL_GRID_COLS)
    cell_h = HEIGHT / float(NORMAL_GRID_ROWS)
    next_id = 0
    with arcpy.da.InsertCursor(
            grid_fc, ["SHAPE@", "GridID", "Row", "Col", "Label", "EdgeCase"]) as gc:
        for r in range(NORMAL_GRID_ROWS):
            for c in range(NORMAL_GRID_COLS):
                x0 = XMIN + c * cell_w
                y0 = YMIN + r * cell_h
                x1 = x0 + cell_w
                y1 = y0 + cell_h
                poly = make_polygon([
                    (x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0),
                ], projected_sr)
                gc.insertRow([
                    poly, next_id, r, c,
                    "{0}-{1}".format(r, c), "Normal",
                ])
                next_id += 1
    log("  Index_Grid cells: {0}".format(next_id))

    # ---- GCS grid: should trigger "GCS not recommended" warning ----
    log("P07: building GCS_Grid (WGS84 lat/lon, GCS warning trigger)...")
    gcs_fc = create_fc(
        gdb, "GCS_Grid", "POLYGON", geographic_sr,
        [
            ("GridID", "LONG", None),
            ("Row", "LONG", None),
            ("Col", "LONG", None),
            ("EdgeCase", "TEXT", 32),
        ],
    )
    # Pick a small lon/lat window roughly corresponding to UTM zone 11N
    # mid-latitudes; exact location doesn't matter, only the SR does.
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
    log("  GCS_Grid cells: {0}".format(next_gcs))

    # ---- HUGE grid: spans HUGE_GRID_TICKS x HUGE_GRID_TICKS index space ----
    log("P07: building HUGE_Grid_Sparse (MAX_TICKS_PER_AXIS trigger)...")
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

    # Emit anchor cells at corners and a random sparse sample so the
    # bounding extent of the grid implies HUGE_GRID_TICKS ticks per axis.
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
                log("  huge grid sparse cells: {0}/{1}"
                    .format(next_huge, HUGE_GRID_SAMPLE_LIMIT))

    log("P07: complete. index_grid={0} gcs_grid={1} huge_sparse={2}"
        .format(next_id, next_gcs, next_huge))
    return grid_fc, gcs_fc, huge_fc


# ===========================================================================
# Main entrypoint
# ===========================================================================


def main():
    out_dir = resolve_output_dir()
    log("Titan World generator starting.")
    log("Output dir: {0}".format(out_dir))
    log("Random seed: {0}".format(RANDOM_SEED))

    arcpy.env.overwriteOutput = True

    gdb_path = create_gdb(out_dir)
    projected_sr = get_projected_sr()
    geographic_sr = get_geographic_sr()

    # P01
    roads_fc, _ = build_p01_roads_and_rivers(gdb_path, projected_sr)

    # P02 (depends on P01 roads for cluster + pipe-under-road).
    build_p02_power_gas_buildings(gdb_path, projected_sr, roads_fc)

    # P03 + P04
    build_p03_p04_contours_and_labels(gdb_path, projected_sr)

    # P05
    build_p05_frame_and_aoi(gdb_path, projected_sr)

    # P06
    build_p06_springs(gdb_path, projected_sr)

    # P07
    build_p07_grids(gdb_path, projected_sr, geographic_sr)

    log("Titan World generation complete.")
    log("Geodatabase: {0}".format(gdb_path))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # Surface the full traceback both to ArcMap and stderr.
        import traceback
        tb = traceback.format_exc()
        try:
            arcpy.AddError(tb)
        except Exception:
            pass
        sys.stderr.write(tb)
        raise
