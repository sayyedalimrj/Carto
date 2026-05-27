# -*- coding: utf-8 -*-
"""
Generate_TitanWorld_Pro.py
==========================

Procedural stress-test world generator for the "Carto" toolbox suite
(ArcGIS Pro 3.x / Python 3.9+).

This standalone script builds a File Geodatabase named ``TitanWorld_Pro.gdb``
that is intentionally engineered to exercise every cartographic edge-case
addressed by Plugins 01-07 of the Carto project. The output is *not* a
realistic map -- it is an adversarial dataset designed to push arcpy and
the toolbox algorithms to their breaking points.

Embedded edge-cases
-------------------

P01 (Bridges/Culverts):
    A dense Roads x Drainage network with 50,000+ true crossings,
    plus deliberately injected T-junction (end-touch) nodes and
    short collinear overlap segments to defeat naive crossing filters.

P02 (Road Deconflict):
    Highways that include genuine **True Curves** (circular arc segments
    authored via the Pro JSON geometry spec) and a 1 km arc segment
    densely packed with 10,000+ buildings + trees within 2-15 m, to
    stress GenerateNearTable.

P03/P04 (Contour Labels / Elevation Text):
    A "Titan Ridge" contour with 500,000+ vertices generated from a
    log-spiral fractal, plus thousands of overlapping label candidate
    boxes for vectorized AABB stress.

P05 (Safe Contour Cleaner):
    A jagged Custom_AOI whose edges differ from the Map_Frame by
    sub-decimeter slivers, designed to produce microscopic clipped
    polygons.

P06 (Spring Rotation):
    A "Fault Line" of 5 mathematically collinear springs (singular SVD)
    plus an Isolated_Spring placed far from any contour.

P07 (Grid Builder):
    A regional Index_Grid in the projected SR plus a parallel grid in
    GCS_WGS_1984 (to trigger the GCS warning) and a deliberately huge
    grid extent to exercise MAX_TICKS_PER_AXIS safety caps.

Usage
-----

    > propy Generate_TitanWorld_Pro.py [--out C:\\path\\to\\workspace]

The script is idempotent: existing output GDBs at the target location are
deleted and regenerated.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import random
import sys
import time
from typing import Iterable, List, Sequence, Tuple

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
GEOGRAPHIC_WKID = 4326  # GCS_WGS_1984 -- used to trigger P07's GCS warning.

# 20 km x 20 km region in UTM meters.
X_MIN = 500_000.0
X_MAX = 520_000.0
Y_MIN = 3_900_000.0
Y_MAX = 3_920_000.0

WIDTH = X_MAX - X_MIN
HEIGHT = Y_MAX - Y_MIN

# Reproducibility.
RANDOM_SEED = 20260527  # deterministic, reproducible seed
random.seed(RANDOM_SEED)

# Stress-test scale factors. These can be reduced for smoke testing but the
# project proposal asks for true breaking-point scale.
ROAD_GRID_LINES_PER_AXIS = 160      # ~160 + 160 = 320 long lines
DRAINAGE_LINES = 220                # diagonal/sinuous drainage strands
BUILDINGS_GLOBAL = 8_000            # buildings scattered globally
BUILDINGS_ARC_BAND = 10_500         # tightly packed near the test arc
TREES_ARC_BAND = 4_500              # scatter trees too (Points)
POWER_LINES = 35
GAS_PIPES = 28
LABEL_BOXES = 6_500                 # heavily overlapping AABBs
RIDGE_VERTICES = 500_000            # mandated by spec
CONTOURS_AMBIENT = 220              # additional ambient contour curves
SPRINGS_RANDOM = 600                # ambient springs
INDEX_GRID_CELLS_PER_AXIS = 250     # 250x250 = 62,500 cells (projected)
INDEX_GRID_GCS_CELLS_PER_AXIS = 60  # smaller GCS grid
HUGE_GRID_TICKS = 25_000            # used to trigger MAX_TICKS_PER_AXIS guard

# Spatial reference handles -- created lazily after arcpy is available.
SR_PROJECTED = arcpy.SpatialReference(PROJECTED_WKID)
SR_GEOGRAPHIC = arcpy.SpatialReference(GEOGRAPHIC_WKID)


# ---------------------------------------------------------------------------
# Logging helpers
# ---------------------------------------------------------------------------

_T0 = time.time()


def log(msg: str) -> None:
    """Lightweight timestamped logger that respects arcpy messaging."""
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
# Geometry / math utilities (pure-python, no numpy dependency required)
# ---------------------------------------------------------------------------

def rand_point(margin: float = 0.0) -> Tuple[float, float]:
    """Uniform random point inside the AOI (with optional inward margin)."""
    x = random.uniform(X_MIN + margin, X_MAX - margin)
    y = random.uniform(Y_MIN + margin, Y_MAX - margin)
    return x, y


def jitter(value: float, amp: float) -> float:
    return value + random.uniform(-amp, amp)


def lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * t


def polyline_array(coords: Sequence[Tuple[float, float]]) -> arcpy.Array:
    """Convert an iterable of (x, y) tuples into an arcpy.Array of Points."""
    arr = arcpy.Array()
    for x, y in coords:
        arr.add(arcpy.Point(x, y))
    return arr


def make_polyline(coords: Sequence[Tuple[float, float]],
                  sr: arcpy.SpatialReference = SR_PROJECTED) -> arcpy.Polyline:
    return arcpy.Polyline(polyline_array(coords), sr)


def make_polygon(rings: Sequence[Sequence[Tuple[float, float]]],
                 sr: arcpy.SpatialReference = SR_PROJECTED) -> arcpy.Polygon:
    outer = arcpy.Array()
    for ring in rings:
        ring_arr = arcpy.Array()
        for x, y in ring:
            ring_arr.add(arcpy.Point(x, y))
        # Close ring if not closed.
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
    """Create (or recreate) the TitanWorld_Pro.gdb at *out_dir*."""
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
    """Create a feature class inside the active workspace and add fields."""
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
# P01 :: Roads / Drainage / Bridges / Culverts
# ===========================================================================

def _generate_road_grid_segments() -> List[Tuple[List[Tuple[float, float]], str]]:
    """Build the orthogonal-ish road grid with mild jitter.

    Returns a list of (coords, road_class) tuples where road_class is one
    of {"PRIMARY", "SECONDARY", "LOCAL"}.
    """
    segments: List[Tuple[List[Tuple[float, float]], str]] = []

    # E-W roads (horizontal sweepers)
    for i in range(ROAD_GRID_LINES_PER_AXIS):
        t = (i + 0.5) / ROAD_GRID_LINES_PER_AXIS
        y = lerp(Y_MIN, Y_MAX, t)
        # Sample 220 vertices with mild perpendicular jitter.
        coords = []
        steps = 220
        for s in range(steps + 1):
            x = lerp(X_MIN, X_MAX, s / steps)
            yy = jitter(y, 12.0)
            coords.append((x, yy))
        cls = "PRIMARY" if i % 25 == 0 else ("SECONDARY" if i % 5 == 0 else "LOCAL")
        segments.append((coords, cls))

    # N-S roads
    for j in range(ROAD_GRID_LINES_PER_AXIS):
        t = (j + 0.5) / ROAD_GRID_LINES_PER_AXIS
        x = lerp(X_MIN, X_MAX, t)
        coords = []
        steps = 220
        for s in range(steps + 1):
            y = lerp(Y_MIN, Y_MAX, s / steps)
            xx = jitter(x, 12.0)
            coords.append((xx, y))
        cls = "PRIMARY" if j % 25 == 0 else ("SECONDARY" if j % 5 == 0 else "LOCAL")
        segments.append((coords, cls))

    return segments


def _generate_drainage_segments() -> List[List[Tuple[float, float]]]:
    """Sinuous diagonal rivers/streams that will cross the road grid."""
    segments: List[List[Tuple[float, float]]] = []
    for k in range(DRAINAGE_LINES):
        # Random start/end on opposite sides; carve a sinusoidal channel.
        side = random.choice(("EW", "NS", "DIAG"))
        if side == "EW":
            x0, x1 = X_MIN, X_MAX
            y_mid = random.uniform(Y_MIN + 200, Y_MAX - 200)
            amp = random.uniform(150, 800)
            freq = random.uniform(2.0, 7.0)
            steps = 600
            coords = []
            for s in range(steps + 1):
                t = s / steps
                x = lerp(x0, x1, t)
                y = y_mid + amp * math.sin(freq * math.tau * t + random.random())
                coords.append((x, y))
        elif side == "NS":
            y0, y1 = Y_MIN, Y_MAX
            x_mid = random.uniform(X_MIN + 200, X_MAX - 200)
            amp = random.uniform(150, 800)
            freq = random.uniform(2.0, 7.0)
            steps = 600
            coords = []
            for s in range(steps + 1):
                t = s / steps
                y = lerp(y0, y1, t)
                x = x_mid + amp * math.sin(freq * math.tau * t + random.random())
                coords.append((x, y))
        else:  # DIAG
            x0, y0 = X_MIN, random.uniform(Y_MIN, Y_MAX)
            x1, y1 = X_MAX, random.uniform(Y_MIN, Y_MAX)
            amp = random.uniform(80, 400)
            freq = random.uniform(3.0, 10.0)
            steps = 700
            coords = []
            for s in range(steps + 1):
                t = s / steps
                x = lerp(x0, x1, t)
                y = lerp(y0, y1, t) + amp * math.sin(freq * math.tau * t)
                coords.append((x, y))
        segments.append(coords)
    return segments


def _inject_t_junctions(road_segments) -> List[List[Tuple[float, float]]]:
    """Create dead-end stubs whose endpoints touch existing road interiors.

    These are the classic T-junction "end-touches" that should NOT count as
    true crossings under Plugin01's filter.
    """
    stubs: List[List[Tuple[float, float]]] = []
    n_stubs = 1500
    for _ in range(n_stubs):
        target = random.choice(road_segments)
        coords, _cls = target
        # Pick an interior vertex (not first/last) of the chosen road.
        if len(coords) < 4:
            continue
        idx = random.randint(1, len(coords) - 2)
        anchor = coords[idx]
        # Build a stub starting AT the anchor and shooting outward.
        angle = random.uniform(0, math.tau)
        length = random.uniform(40, 180)
        end = (anchor[0] + length * math.cos(angle),
               anchor[1] + length * math.sin(angle))
        # 2-3 vertex stub (T-junction end-touch).
        stub = [anchor, end]
        stubs.append(stub)
    return stubs


def _inject_collinear_overlaps(road_segments) -> List[List[Tuple[float, float]]]:
    """Create short segments that sit *on top of* existing roads (collinear overlap).

    These should also be excluded by a robust true-crossing filter (the lines
    do not transversally cross -- they overlap).
    """
    overlaps: List[List[Tuple[float, float]]] = []
    n_overlaps = 800
    for _ in range(n_overlaps):
        target = random.choice(road_segments)
        coords, _cls = target
        if len(coords) < 5:
            continue
        i = random.randint(0, len(coords) - 4)
        # Take a 3-vertex slice so the overlap shares >= 1 segment exactly.
        slice_ = list(coords[i:i + 3])
        overlaps.append(slice_)
    return overlaps


def build_p01_roads_and_drainage(gdb: str) -> None:
    log("[P01] Building Roads + Drainage with crossings/T-junctions/overlaps")
    roads_fc = create_fc(
        "Roads",
        "POLYLINE",
        fields=(
            ("RoadID", "LONG", 0),
            ("RoadClass", "TEXT", 32),
            ("Origin", "TEXT", 24),
        ),
    )
    drainage_fc = create_fc(
        "Drainage",
        "POLYLINE",
        fields=(
            ("DrainID", "LONG", 0),
            ("StreamType", "TEXT", 24),
        ),
    )

    road_segments = _generate_road_grid_segments()
    t_junction_stubs = _inject_t_junctions(road_segments)
    collinear_overlaps = _inject_collinear_overlaps(road_segments)
    drainage_segments = _generate_drainage_segments()

    # --- Insert roads -------------------------------------------------------
    log(f"  Inserting {len(road_segments)} grid roads + "
        f"{len(t_junction_stubs)} T-junctions + "
        f"{len(collinear_overlaps)} collinear overlaps")
    rid = 0
    with arcpy.da.InsertCursor(roads_fc, ["SHAPE@", "RoadID", "RoadClass", "Origin"]) as cur:
        for coords, cls in road_segments:
            cur.insertRow([make_polyline(coords), rid, cls, "GRID"])
            rid += 1
        for stub in t_junction_stubs:
            cur.insertRow([make_polyline(stub), rid, "LOCAL", "T_JUNCTION"])
            rid += 1
        for ov in collinear_overlaps:
            cur.insertRow([make_polyline(ov), rid, "LOCAL", "COLLINEAR_OVERLAP"])
            rid += 1

    # --- Insert drainage ----------------------------------------------------
    log(f"  Inserting {len(drainage_segments)} drainage strands")
    did = 0
    with arcpy.da.InsertCursor(drainage_fc, ["SHAPE@", "DrainID", "StreamType"]) as cur:
        stream_types = ("RIVER", "STREAM", "CANAL", "DITCH")
        for coords in drainage_segments:
            cur.insertRow([
                make_polyline(coords),
                did,
                random.choice(stream_types),
            ])
            did += 1

    # Quick sanity report on intersection density.
    n_axis = ROAD_GRID_LINES_PER_AXIS
    approx_road_x_road = n_axis * n_axis
    approx_road_x_drain = (2 * n_axis) * len(drainage_segments)
    log(f"  Approx Road x Road intersections : {approx_road_x_road:,}")
    log(f"  Approx Road x Drainage crossings : {approx_road_x_drain:,}")
    log(f"  Total expected true crossings    : "
        f"{approx_road_x_road + approx_road_x_drain:,} (>> 50,000)")



# ===========================================================================
# P02 :: Road Deconflict (True Curves, Power Lines, Gas Pipes, Buildings, Trees)
# ===========================================================================

# Anchor for the "test arc" that will be densely surrounded by buildings and
# trees. Stored as a module-level constant so subsequent generators can
# place features along it.
ARC_CENTER = (
    X_MIN + 0.55 * WIDTH,   # ~511 km easting
    Y_MIN + 0.40 * HEIGHT,  # ~3 908 km northing
)
ARC_RADIUS = 800.0          # meters; circumference ~ 5 km, 1 km arc ~ 1.25 rad
ARC_START_ANGLE = math.radians(20.0)
ARC_END_ANGLE = ARC_START_ANGLE + (1000.0 / ARC_RADIUS)  # 1 km along the arc


def _arc_point(t: float) -> Tuple[float, float]:
    """Parametric point along the test arc (t in [0, 1])."""
    a = lerp(ARC_START_ANGLE, ARC_END_ANGLE, t)
    return (ARC_CENTER[0] + ARC_RADIUS * math.cos(a),
            ARC_CENTER[1] + ARC_RADIUS * math.sin(a))


def _highway_truecurve_json(start_xy: Tuple[float, float],
                            end_xy: Tuple[float, float],
                            mid_xy: Tuple[float, float]) -> dict:
    """Construct a Pro JSON polyline with a circular arc segment.

    Pro's REST/JSON spec encodes a circular arc as
    ``{"c": [endX, endY, midX, midY]}`` inside a path -- ``arcpy.AsShape``
    preserves this as a true curve when ``True`` is passed for the second
    argument.
    """
    start_x, start_y = start_xy
    end_x, end_y = end_xy
    mid_x, mid_y = mid_xy
    return {
        "hasZ": False,
        "hasM": False,
        "curvePaths": [[
            [start_x, start_y],
            {"c": [[end_x, end_y], [mid_x, mid_y]]},
        ]],
        "spatialReference": {"wkid": PROJECTED_WKID},
    }


def _build_truecurve_polyline(start_xy, end_xy, mid_xy) -> arcpy.Polyline:
    """Return a polyline geometry that *preserves* its circular arc segment."""
    payload = _highway_truecurve_json(start_xy, end_xy, mid_xy)
    return arcpy.AsShape(payload, True)  # esri_json=True -> respects curves


def build_p02_deconflict_layers(gdb: str) -> None:
    log("[P02] Building Power_Lines, Gas_Pipes, Buildings, Trees + True-Curve Highway")

    power_fc = create_fc(
        "Power_Lines", "POLYLINE",
        fields=(("Voltage_kV", "SHORT", 0), ("LineID", "LONG", 0)),
    )
    gas_fc = create_fc(
        "Gas_Pipes", "POLYLINE",
        fields=(("Diameter_in", "SHORT", 0), ("PipeID", "LONG", 0)),
    )
    bldg_fc = create_fc(
        "Buildings", "POLYGON",
        fields=(
            ("BldgID", "LONG", 0),
            ("Category", "TEXT", 24),
            ("HeightM", "FLOAT", 0),
        ),
    )
    trees_fc = create_fc(
        "Trees", "POINT",
        fields=(
            ("TreeID", "LONG", 0),
            ("Species", "TEXT", 24),
        ),
    )
    highway_fc = create_fc(
        "Highways_TrueCurve", "POLYLINE",
        fields=(
            ("HwyID", "LONG", 0),
            ("Designation", "TEXT", 24),
            ("HasTrueCurve", "SHORT", 0),
        ),
    )

    # ------------------------------------------------------------------ Power
    log(f"  Inserting {POWER_LINES} power transmission lines")
    with arcpy.da.InsertCursor(power_fc, ["SHAPE@", "Voltage_kV", "LineID"]) as cur:
        for i in range(POWER_LINES):
            x0, y0 = rand_point(margin=200)
            x1, y1 = rand_point(margin=200)
            steps = 60
            coords = []
            for s in range(steps + 1):
                t = s / steps
                x = lerp(x0, x1, t) + random.uniform(-3, 3)
                y = lerp(y0, y1, t) + random.uniform(-3, 3)
                coords.append((x, y))
            cur.insertRow([
                make_polyline(coords),
                random.choice([69, 115, 230, 345, 500]),
                i,
            ])

    # -------------------------------------------------------------------- Gas
    log(f"  Inserting {GAS_PIPES} gas pipelines")
    with arcpy.da.InsertCursor(gas_fc, ["SHAPE@", "Diameter_in", "PipeID"]) as cur:
        for i in range(GAS_PIPES):
            x0, y0 = rand_point(margin=300)
            x1, y1 = rand_point(margin=300)
            steps = 80
            coords = []
            for s in range(steps + 1):
                t = s / steps
                x = lerp(x0, x1, t) + math.sin(8 * t) * 25
                y = lerp(y0, y1, t) + math.cos(6 * t) * 25
                coords.append((x, y))
            cur.insertRow([
                make_polyline(coords),
                random.choice([6, 8, 12, 16, 24, 36]),
                i,
            ])

    # ------------------------------------------------------------ True-Curves
    # Build several highway interchange arcs, including the "test arc" that
    # is densely surrounded by buildings/trees in the next step.
    log("  Inserting True-Curve highway segments (circular arcs preserved as JSON)")
    hwy_id = 0
    with arcpy.da.InsertCursor(
            highway_fc, ["SHAPE@", "HwyID", "Designation", "HasTrueCurve"]) as cur:

        # The mandated test arc -- exactly 1 km along a circle of radius 800 m.
        start = _arc_point(0.0)
        end = _arc_point(1.0)
        mid = _arc_point(0.5)
        cur.insertRow([
            _build_truecurve_polyline(start, end, mid),
            hwy_id, "INTERCHANGE_TEST_ARC", 1,
        ])
        hwy_id += 1

        # Additional decorative interchanges scattered around the AOI to give
        # Plugin02's true-curve preservation logic more to chew on.
        for _ in range(18):
            cx, cy = rand_point(margin=1500)
            r = random.uniform(150, 600)
            a0 = random.uniform(0, math.tau)
            sweep = random.uniform(math.pi / 3, 2 * math.pi / 3)
            s = (cx + r * math.cos(a0), cy + r * math.sin(a0))
            e = (cx + r * math.cos(a0 + sweep),
                 cy + r * math.sin(a0 + sweep))
            m = (cx + r * math.cos(a0 + sweep / 2),
                 cy + r * math.sin(a0 + sweep / 2))
            cur.insertRow([
                _build_truecurve_polyline(s, e, m),
                hwy_id, "INTERCHANGE_RAMP", 1,
            ])
            hwy_id += 1

    # ------------------------------------------------------ Buildings + Trees
    log(f"  Inserting {BUILDINGS_GLOBAL:,} ambient buildings + "
        f"{BUILDINGS_ARC_BAND:,} arc-band buildings")
    bld_id = 0
    with arcpy.da.InsertCursor(
            bldg_fc, ["SHAPE@", "BldgID", "Category", "HeightM"]) as cur:
        # Ambient buildings -- random rectangles across the AOI.
        for _ in range(BUILDINGS_GLOBAL):
            cx, cy = rand_point(margin=20)
            w = random.uniform(6, 40)
            h = random.uniform(6, 40)
            ang = random.uniform(0, math.tau)
            ca, sa = math.cos(ang), math.sin(ang)
            corners = [(-w / 2, -h / 2), (w / 2, -h / 2),
                       (w / 2, h / 2), (-w / 2, h / 2)]
            ring = [(cx + cx_r * ca - cy_r * sa, cy + cx_r * sa + cy_r * ca)
                    for cx_r, cy_r in corners]
            cur.insertRow([
                make_polygon([ring]),
                bld_id,
                random.choice(["RESIDENTIAL", "COMMERCIAL", "INDUSTRIAL", "MIXED"]),
                random.uniform(3.0, 60.0),
            ])
            bld_id += 1

        # Densely packed band along the test arc (within 2-15 m of the curve).
        for k in range(BUILDINGS_ARC_BAND):
            t = random.random()
            cx_arc, cy_arc = _arc_point(t)
            # Outward-pointing normal at parameter t.
            angle = lerp(ARC_START_ANGLE, ARC_END_ANGLE, t)
            nx = math.cos(angle)
            ny = math.sin(angle)
            # Offset 2-15 m perpendicular to either side of the curve.
            side = random.choice((-1.0, 1.0))
            offset = side * random.uniform(2.0, 15.0)
            cx = cx_arc + nx * offset
            cy = cy_arc + ny * offset
            w = random.uniform(4, 14)
            h = random.uniform(4, 14)
            ring = [(cx - w / 2, cy - h / 2), (cx + w / 2, cy - h / 2),
                    (cx + w / 2, cy + h / 2), (cx - w / 2, cy + h / 2)]
            cur.insertRow([
                make_polygon([ring]),
                bld_id,
                "ARC_BAND",
                random.uniform(3.0, 18.0),
            ])
            bld_id += 1

    log(f"  Inserting {TREES_ARC_BAND:,} trees within the arc band")
    species = ("OAK", "PINE", "MAPLE", "JUNIPER", "COTTONWOOD")
    with arcpy.da.InsertCursor(trees_fc, ["SHAPE@", "TreeID", "Species"]) as cur:
        for k in range(TREES_ARC_BAND):
            t = random.random()
            cx_arc, cy_arc = _arc_point(t)
            angle = lerp(ARC_START_ANGLE, ARC_END_ANGLE, t)
            side = random.choice((-1.0, 1.0))
            offset = side * random.uniform(2.0, 15.0)
            x = cx_arc + math.cos(angle) * offset
            y = cy_arc + math.sin(angle) * offset
            cur.insertRow([make_point(x, y), k, random.choice(species)])



# ===========================================================================
# P03 / P04 :: Contours + Label Candidate Boxes
# ===========================================================================

def _titan_ridge_coords(n_vertices: int) -> List[Tuple[float, float]]:
    """Generate the Titan Ridge: a single fractal log-spiral contour.

    The curve combines a logarithmic spiral envelope with a multi-octave
    sinusoidal perturbation -- producing an extremely long, self-avoiding
    line that hammers the algorithm's RAM headroom.
    """
    cx = X_MIN + 0.65 * WIDTH
    cy = Y_MIN + 0.70 * HEIGHT

    coords: List[Tuple[float, float]] = []
    a = 1.5      # spiral scale
    b = 0.085    # spiral growth
    # Total angular sweep -- chosen so the spiral fills ~ 6 km of radius.
    theta_max = 60.0  # radians
    for i in range(n_vertices):
        t = i / (n_vertices - 1)
        theta = t * theta_max
        r = a * math.exp(b * theta)
        # High-frequency fractal perturbation.
        wob = (
            0.08 * math.sin(11.0 * theta)
            + 0.04 * math.sin(37.0 * theta + 1.3)
            + 0.02 * math.sin(91.0 * theta + 2.7)
        )
        r_eff = r * (1.0 + wob)
        x = cx + r_eff * math.cos(theta)
        y = cy + r_eff * math.sin(theta)
        # Clamp inside the AOI.
        x = min(max(x, X_MIN + 1.0), X_MAX - 1.0)
        y = min(max(y, Y_MIN + 1.0), Y_MAX - 1.0)
        coords.append((x, y))
    return coords


def _ambient_contour(elev: float, idx: int) -> List[Tuple[float, float]]:
    """A reasonably-shaped ambient contour line."""
    cx = X_MIN + (0.2 + 0.6 * random.random()) * WIDTH
    cy = Y_MIN + (0.2 + 0.6 * random.random()) * HEIGHT
    radius = random.uniform(120, 1800)
    steps = random.randint(180, 360)
    coords = []
    phase = random.uniform(0, math.tau)
    eccentricity = random.uniform(0.6, 1.4)
    for s in range(steps):
        t = s / (steps - 1)
        a = phase + t * math.tau
        r = radius * (1.0 + 0.15 * math.sin(3 * a + idx))
        x = cx + r * math.cos(a)
        y = cy + r * math.sin(a) * eccentricity
        x = min(max(x, X_MIN + 1.0), X_MAX - 1.0)
        y = min(max(y, Y_MIN + 1.0), Y_MAX - 1.0)
        coords.append((x, y))
    return coords


def build_p03_p04_contours_and_labels(gdb: str) -> None:
    log("[P03/P04] Building Contours + Label_Candidate_Boxes")

    contour_fc = create_fc(
        "Contours", "POLYLINE",
        fields=(
            ("ContourID", "LONG", 0),
            ("Elevation", "DOUBLE", 0),
            ("Kind", "TEXT", 24),
        ),
    )
    label_fc = create_fc(
        "Label_Candidate_Boxes", "POLYGON",
        fields=(
            ("LabelID", "LONG", 0),
            ("LabelText", "TEXT", 64),
            ("Priority", "SHORT", 0),
        ),
    )

    # ---- Titan Ridge -------------------------------------------------------
    log(f"  Generating Titan Ridge with {RIDGE_VERTICES:,} vertices "
        "(fractal log-spiral)")
    ridge_coords = _titan_ridge_coords(RIDGE_VERTICES)

    # arcpy.Polyline construction with 500k+ vertices is the actual stress
    # point. We build the array iteratively to keep peak memory finite.
    log("  Allocating arcpy.Array for ridge ...")
    ridge_array = arcpy.Array()
    pt = arcpy.Point()
    for x, y in ridge_coords:
        pt.X = x
        pt.Y = y
        ridge_array.add(arcpy.Point(pt.X, pt.Y))
    ridge_polyline = arcpy.Polyline(ridge_array, SR_PROJECTED)
    log(f"  Titan Ridge length: {ridge_polyline.length:,.1f} m")

    cid = 0
    with arcpy.da.InsertCursor(
            contour_fc, ["SHAPE@", "ContourID", "Elevation", "Kind"]) as cur:
        cur.insertRow([ridge_polyline, cid, 2480.0, "TITAN_RIDGE"])
        cid += 1

        log(f"  Inserting {CONTOURS_AMBIENT} ambient contours")
        for i in range(CONTOURS_AMBIENT):
            elev = 1000.0 + 5.0 * i + random.uniform(-1.5, 1.5)
            coords = _ambient_contour(elev, i)
            cur.insertRow([
                make_polyline(coords),
                cid,
                elev,
                random.choice(["INDEX", "INTERMEDIATE", "SUPPLEMENTARY"]),
            ])
            cid += 1

    # ---- Heavily overlapping label candidate boxes -------------------------
    log(f"  Inserting {LABEL_BOXES:,} overlapping label candidate boxes")

    # Cluster centers: thousands of small AABBs piled on top of each other to
    # produce O(N^2) overlaps if naive code is used. Vectorized AABB code in
    # Plugin03 should still cope.
    cluster_centers: List[Tuple[float, float]] = []
    for _ in range(80):
        cluster_centers.append(rand_point(margin=300))

    with arcpy.da.InsertCursor(
            label_fc, ["SHAPE@", "LabelID", "LabelText", "Priority"]) as cur:
        for k in range(LABEL_BOXES):
            cx_, cy_ = random.choice(cluster_centers)
            cx_ += random.uniform(-40, 40)
            cy_ += random.uniform(-40, 40)
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
            ])



# ===========================================================================
# P05 :: Map_Frame + Custom_AOI (jagged sliver-producing edges)
# ===========================================================================

def build_p05_frames_and_aoi(gdb: str) -> None:
    log("[P05] Building Map_Frame + jagged Custom_AOI (sliver factory)")

    frame_fc = create_fc(
        "Map_Frame", "POLYGON",
        fields=(("FrameName", "TEXT", 32),),
    )
    aoi_fc = create_fc(
        "Custom_AOI", "POLYGON",
        fields=(("AOIName", "TEXT", 32), ("Notes", "TEXT", 128)),
    )

    # ---- Map_Frame: clean rectangle covering the full AOI -----------------
    frame_ring = [
        (X_MIN, Y_MIN),
        (X_MAX, Y_MIN),
        (X_MAX, Y_MAX),
        (X_MIN, Y_MAX),
    ]
    with arcpy.da.InsertCursor(frame_fc, ["SHAPE@", "FrameName"]) as cur:
        cur.insertRow([make_polygon([frame_ring]), "TITAN_FRAME"])

    # ---- Custom_AOI: same shape but with microscopic jaggies along the
    # ----            edges to produce sub-decimeter slivers when clipped.
    log("  Constructing jagged AOI edges (sub-decimeter slivers)")
    jagged: List[Tuple[float, float]] = []

    # Bottom edge
    n_bot = 4000
    for i in range(n_bot):
        t = i / n_bot
        x = lerp(X_MIN, X_MAX, t)
        y = Y_MIN + random.uniform(-0.08, 0.08)  # sliver width < 0.1 m
        jagged.append((x, y))
    # Right edge
    n_rt = 4000
    for i in range(n_rt):
        t = i / n_rt
        y = lerp(Y_MIN, Y_MAX, t)
        x = X_MAX + random.uniform(-0.08, 0.08)
        jagged.append((x, y))
    # Top edge (reverse)
    n_top = 4000
    for i in range(n_top):
        t = i / n_top
        x = lerp(X_MAX, X_MIN, t)
        y = Y_MAX + random.uniform(-0.08, 0.08)
        jagged.append((x, y))
    # Left edge (reverse)
    n_lf = 4000
    for i in range(n_lf):
        t = i / n_lf
        y = lerp(Y_MAX, Y_MIN, t)
        x = X_MIN + random.uniform(-0.08, 0.08)
        jagged.append((x, y))

    with arcpy.da.InsertCursor(aoi_fc, ["SHAPE@", "AOIName", "Notes"]) as cur:
        cur.insertRow([
            make_polygon([jagged]),
            "TITAN_AOI",
            "Edges intentionally jagged to produce <0.1 m clip slivers",
        ])


# ===========================================================================
# P06 :: Springs (collinear fault line, isolated spring, ambient scatter)
# ===========================================================================

def build_p06_springs(gdb: str) -> None:
    log("[P06] Building Springs (collinear Fault Line + Isolated Spring)")

    springs_fc = create_fc(
        "Springs", "POINT",
        fields=(
            ("SpringID", "LONG", 0),
            ("FlowGPM", "FLOAT", 0),
            ("Kind", "TEXT", 24),
        ),
    )

    sid = 0
    with arcpy.da.InsertCursor(
            springs_fc, ["SHAPE@", "SpringID", "FlowGPM", "Kind"]) as cur:

        # ----- Fault Line: 5 mathematically perfectly collinear points -----
        # (used to provoke SVD singular-matrix failure in PCA-based
        #  rotation logic).
        log("  Inserting 5 collinear Fault Line springs (SVD trap)")
        x0 = X_MIN + 0.30 * WIDTH
        y0 = Y_MIN + 0.30 * HEIGHT
        x1 = X_MIN + 0.50 * WIDTH
        y1 = Y_MIN + 0.50 * HEIGHT  # same direction vector for all 5
        for k in range(5):
            t = k / 4.0
            x = lerp(x0, x1, t)
            y = lerp(y0, y1, t)
            cur.insertRow([
                make_point(x, y),
                sid, random.uniform(1.0, 12.0), "FAULT_LINE",
            ])
            sid += 1

        # ----- Isolated Spring: deliberately far from any contour ----------
        # We place it near the SE corner where the Titan Ridge / ambient
        # contours do not reach.
        log("  Inserting 1 Isolated Spring")
        cur.insertRow([
            make_point(X_MIN + 0.05 * WIDTH, Y_MIN + 0.05 * HEIGHT),
            sid, 0.5, "ISOLATED",
        ])
        sid += 1

        # ----- Ambient springs ---------------------------------------------
        log(f"  Inserting {SPRINGS_RANDOM} ambient springs")
        for _ in range(SPRINGS_RANDOM):
            x, y = rand_point(margin=50)
            cur.insertRow([
                make_point(x, y),
                sid, random.uniform(0.5, 50.0), "RANDOM",
            ])
            sid += 1


# ===========================================================================
# P07 :: Index Grid (projected + GCS variant + huge extent)
# ===========================================================================

def _grid_cells(x_min: float, y_min: float, x_max: float, y_max: float,
                nx: int, ny: int) -> Iterable[Tuple[int, int, List[Tuple[float, float]]]]:
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
    log("[P07] Building Index_Grid (projected + GCS) and Huge_Grid_Extent")

    # ---- Projected index grid ---------------------------------------------
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
    log(f"  Inserting projected grid: {cells} x {cells} = {cells*cells:,} cells")
    cell_id = 0
    with arcpy.da.InsertCursor(
            grid_fc, ["SHAPE@", "CellID", "Col", "Row", "Label"]) as cur:
        for i, j, ring in _grid_cells(X_MIN, Y_MIN, X_MAX, Y_MAX, cells, cells):
            cur.insertRow([
                make_polygon([ring], sr=SR_PROJECTED),
                cell_id, i, j,
                f"R{j:04d}C{i:04d}",
            ])
            cell_id += 1

    # ---- GCS index grid (deliberately wrong SR -> triggers GCS warning) ---
    grid_gcs_fc = create_fc(
        "Index_Grid_GCS", "POLYGON",
        fields=(
            ("CellID", "LONG", 0),
            ("Col", "LONG", 0),
            ("Row", "LONG", 0),
        ),
        sr=SR_GEOGRAPHIC,
    )
    # Approximate GCS bbox covering roughly the same ground area
    # (UTM 11N centered ~ -116 deg W, 35 deg N). The exact bounds don't
    # matter -- what matters is that the SR is geographic.
    gcs_xmin, gcs_ymin = -116.20, 35.20
    gcs_xmax, gcs_ymax = -116.00, 35.40
    n = INDEX_GRID_GCS_CELLS_PER_AXIS
    log(f"  Inserting GCS_WGS_1984 grid: {n} x {n} = {n*n:,} cells "
        "(this layer should trigger Plugin07's GCS warning)")
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

    # ---- Huge grid extent driver -----------------------------------------
    # We do NOT actually instantiate 25,000 x 25,000 = 625 million cells in
    # the GDB (that would be a quarter-terabyte of geometry). Instead we
    # publish an extent feature class whose single polygon, combined with a
    # tick-spacing parameter recorded in its attributes, will cause
    # Plugin07's grid builder to *attempt* MAX_TICKS_PER_AXIS calculations
    # and trip the safety cap.
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
    huge_ring = [
        (X_MIN, Y_MIN),
        (X_MAX, Y_MIN),
        (X_MAX, Y_MAX),
        (X_MIN, Y_MAX),
    ]
    with arcpy.da.InsertCursor(
            huge_fc,
            ["SHAPE@", "Name", "RequestedTicksX", "RequestedTicksY", "Notes"]) as cur:
        cur.insertRow([
            make_polygon([huge_ring]),
            "TITAN_HUGE_EXTENT",
            HUGE_GRID_TICKS, HUGE_GRID_TICKS,
            "Drives MAX_TICKS_PER_AXIS guard in Plugin07",
        ])



# ===========================================================================
# Orchestration
# ===========================================================================

def _summarise(gdb: str) -> None:
    """Print row counts for every feature class in the output GDB."""
    arcpy.env.workspace = gdb
    log("--- TitanWorld_Pro.gdb summary ---")
    for fc in sorted(arcpy.ListFeatureClasses() or []):
        try:
            n = int(arcpy.management.GetCount(fc).getOutput(0))
        except Exception:
            n = -1
        log(f"  {fc:30s} : {n:>10,} features")


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Generate the TitanWorld_Pro stress-test GDB.",
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
        choices=["P01", "P02", "P03", "P04", "P05", "P06", "P07"],
        help="Optional list of plugin sections to skip.",
    )
    return p.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    log("=" * 72)
    log(" TitanWorld_Pro :: procedural stress-test generator")
    log(f" Random seed     : {RANDOM_SEED}")
    log(f" AOI extent      : X[{X_MIN}, {X_MAX}]  Y[{Y_MIN}, {Y_MAX}]  "
        f"({WIDTH/1000:.1f} km x {HEIGHT/1000:.1f} km)")
    log(f" Projected SR    : EPSG:{PROJECTED_WKID}  ({SR_PROJECTED.name})")
    log(f" Output workspace: {args.out}")
    log("=" * 72)

    gdb = build_gdb(args.out)

    if "P01" not in args.skip:
        build_p01_roads_and_drainage(gdb)
    if "P02" not in args.skip:
        build_p02_deconflict_layers(gdb)
    if "P03" not in args.skip or "P04" not in args.skip:
        # P03 and P04 share the same layers, so we run them together unless
        # both are skipped.
        build_p03_p04_contours_and_labels(gdb)
    if "P05" not in args.skip:
        build_p05_frames_and_aoi(gdb)
    if "P06" not in args.skip:
        build_p06_springs(gdb)
    if "P07" not in args.skip:
        build_p07_index_grid(gdb)

    _summarise(gdb)
    log("DONE.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
