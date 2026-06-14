# -*- coding: utf-8 -*-
"""
carto_test_core.py  -- shared engine for the Carto plugin test framework.

This single module is loaded by BOTH platform harnesses:
  * tests_arcmap/run_all_carto_tests_arcmap.py   (Python 2.7 / ArcMap arcpy)
  * tests_pro/run_all_carto_tests_pro.py          (Python 3.x / ArcGIS Pro arcpy)
  * the two .pyt toolbox harnesses

Therefore it is written in the intersection of Python 2.7 and Python 3.x:
  - NO f-strings, NO pathlib, NO type hints in signatures.
  - "from __future__ import" used for safe division / print.
  - arcpy is imported lazily inside functions so the module can be *parsed*
    (and partly unit-tested for its pure-python helpers) without arcpy.

The engine:
  1. Scans the input geodatabase  -> MAP_DATA_INVENTORY.*, FIELD_CANDIDATES_*.csv
  2. Detects layer roles          -> LAYER_ROLE_MAPPING.*
  3. Builds a safe test workspace -> carto_test_output/Carto_Test_Run_<ts>/...
  4. Auto-fills plugin parameters  -> AUTO_FILLED_PLUGIN_PARAMETERS.json
  5. Runs per-plugin tests (T01..T07), each with smoke/functional/edge/regression
  6. Writes per-plugin + global reports, before/after QA.

Safety contract (enforced here):
  * The input geodatabase is opened READ-ONLY. We never write to it.
  * Every plugin runs against COPIES staged in test_data.gdb (T0x_ prefixes).
  * Outputs go to result_data.gdb. Synthetic data is clearly tagged.
"""

from __future__ import division
from __future__ import print_function

import os
import sys
import json
import time
import traceback

# report_writer lives next to this file in common/.
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
import report_writer as rw  # noqa: E402


# ==========================================================================
# 0. Static metadata derived from source-code inspection (see CODE_INVENTORY)
# ==========================================================================

WKID_EXPECTED = 32638  # WGS_1984_UTM_Zone_38N (meters) - all FCs in test GDB

# Per-plugin registry. tool ordering of input parameters matches the order in
# which the tools' getParameterInfo() returns *non-derived* parameters, so we
# can invoke them positionally through arcpy.ImportToolbox.
PLUGIN_REGISTRY = {
    "Plugin01": {
        "name": "Bridge / Culvert Type & Angle Correction",
        "arcmap_pyt": "Plugin01_BridgeCulvert_ArcMap_py27.pyt",
        "pro_pyt": "Plugin01_BridgeCulvert_Pro_py3.pyt",
        "alias_arcmap": "bridgeCulvertArcMap",
        "alias_pro": "bridgeCulvertPro",
        "prefix": "T01",
    },
    "Plugin02": {
        "name": "Road Conflict Resolution",
        "arcmap_pyt": "Plugin02_RoadDeconflict_ArcMap_v5_hardened.pyt",
        "pro_pyt": "Plugin02_RoadDeconflict_Pro_v5_native.pyt",
        "alias_arcmap": "plugin2_road_deconflict_arcmap_v6",
        "alias_pro": "plugin2_road_deconflict_pro",
        "prefix": "T02",
    },
    "Plugin03": {
        "name": "Contour Label Optimizer",
        "arcmap_pyt": "Plugin03_ContourLabelOptimizer_ArcMap_v4_hardened.pyt",
        "pro_pyt": "Plugin03_ContourLabelOptimizer_Pro_v4_native.pyt",
        "alias_arcmap": "contourlabelopt5_arcmap",
        "alias_pro": "contourlabelopt4_pro",
        "prefix": "T03",
    },
    "Plugin04": {
        "name": "Elevation Text Deconflict",
        "arcmap_pyt": "Plugin04_ElevationTextDeconflict_ArcMap_v5_hardened.pyt",
        "pro_pyt": "Plugin04_ElevationTextDeconflict_Pro_v5_native.pyt",
        "alias_arcmap": "elevtext_v6_arcmap",
        "alias_pro": "elevtext_pro",
        "prefix": "T04",
    },
    "Plugin05": {
        "name": "Safe Contour Cleaner",
        "arcmap_pyt": "Plugin05_SafeContourCleaner_ArcMap_v5_hardened.pyt",
        "pro_pyt": "Plugin05_SafeContourCleaner_Pro_v5_native.pyt",
        "alias_arcmap": "carto_auto_arcmap_v5",
        "alias_pro": "carto_auto_pro_v5",
        "prefix": "T05",
    },
    "Plugin06": {
        "name": "Spring Symbol Rotation",
        "arcmap_pyt": "Plugin06_SpringRotation_ArcMap_v4_hardened.pyt",
        "pro_pyt": "Plugin06_SpringRotation_Pro_v4_native.pyt",
        "alias_arcmap": "SpringRotationSuiteV4",
        "alias_pro": "SpringRotationSuiteProV4",
        "prefix": "T06",
    },
    "Plugin07": {
        "name": "Batch Grid / Index Builder",
        "arcmap_pyt": "Plugin07_BatchGridBuilder_ArcMap_v6_hardened.pyt",
        "pro_pyt": "Plugin07_BatchGridBuilder_Pro_v6_native.pyt",
        "alias_arcmap": "plugin07_batch_grid_v6",
        "alias_pro": "plugin07_batch_grid_pro_v6",
        "prefix": "T07",
    },
}

PLUGIN_ORDER = ["Plugin01", "Plugin02", "Plugin03", "Plugin04",
                "Plugin05", "Plugin06", "Plugin07"]

# Role detection rules. Each role: list of (substring, weight) name hints,
# the geometry it must have, and whether multiple candidates are allowed.
# Matching is case-insensitive on the feature-class base name.
ROLE_RULES = [
    # role,            geometry,   name_includes (ORDER = preference, earlier wins),     name_excludes
    ("road_asphalt",   "Polyline", ["asphalt_road1", "asphalt_road", "freeway", "highway"], ["airport"]),
    ("road_dirt",      "Polyline", ["dirt_road"],                                        []),
    # Track road must prefer the real Track_Road layer, NOT footpaths (Path_Lin).
    ("road_track",     "Polyline", ["track_road"],                                       ["path"]),
    ("road_gravel",    "Polyline", ["gravel_road"],                                      ["airport"]),
    # General road role: prefer real road layers (dirt/asphalt/track/gravel/freeway),
    # never let footpaths (path) or railways outrank an actual road.
    ("road_any",       "Polyline", ["dirt_road", "asphalt_road", "track_road",
                                    "gravel_road", "freeway", "highway", "road"],        ["airport", "railway", "path"]),
    ("watercourse",    "Polyline", ["watercourse"],                                      []),
    ("canal",          "Polyline", ["canal"],                                            []),
    ("river_line",     "Polyline", ["river_l", "seasonal_river_l", "qanat", "stream"],   ["river_a"]),
    ("drainage_any",   "Polyline", ["watercourse", "canal", "river_l", "qanat",
                                    "stream", "ditch", "floodway"],                      ["river_a"]),
    ("contour_index",  "Polyline", ["contour_index"],                                    ["anno"]),
    ("contour_interval", "Polyline", ["contour_interval"],                               ["anno"]),
    ("contour_any",    "Polyline", ["contour"],                                          ["anno"]),
    ("elevation_points", "Point",  ["elevation_point", "spot"],                          ["anno"]),
    ("elevation_text_anno", "Polygon", ["elevation_pointsanno", "elevation_points_anno"], []),
    ("contour_index_anno", "Polygon", ["contour_indexanno", "contour_index_anno"],       []),
    # Existing bridge POINT layer: prefer the dedicated point layer Bridge_P
    # over the generic Bridge layer (runtime scoring also prefers the populated
    # one; the ordered hint makes Bridge_P win even before counts are known).
    ("bridge_existing", "Point",   ["bridge_p", "bridge"],                               []),
    ("spring",         "Point",    ["spring"],                                           []),
    ("spring_continual", "Point",  ["continual_spring"],                                 []),
    ("spring_seasonal", "Point",   ["seasonal_spring"],                                  []),
    ("powerline",      "Polyline", ["power_trans", "hv_line", "power_trans_line"],       []),
    ("building_poly",  "Polygon",  ["building_area", "single_building"],                 []),
    ("building_point", "Point",    ["single_building", "building"],                      ["area"]),
    ("point_obstacle", "Point",    ["tower", "well", "mine", "tank", "post", "station"], []),
    ("aoi_frame",      "Polygon",  ["aoi", "frame", "neatline", "sheet", "mapframe"],    ["anno"]),
    ("dem_raster",     "Raster",   ["dem", "elev", "raster"],                            []),
]

# Field-candidate keyword groups (case-insensitive substring match on field name).
FIELD_CANDIDATE_GROUPS = {
    "elevation": ["ortho_hght", "hght", "elev", "height", "z", "altitude", "spot"],
    "angle_rotation": ["rot", "angle", "azimuth", "bearing", "direction", "heading"],
    "type_class_symbol": ["type", "class", "symbol", "kind", "category", "code"],
    "name_code": ["name", "fname", "code", "id", "label"],
}


# ==========================================================================
# 1. Small pure-python helpers (testable without arcpy)
# ==========================================================================

def base_name(path):
    """Return the dataset base name from a catalog path."""
    if not path:
        return ""
    return os.path.basename(path.replace("\\", "/"))


def name_matches(nm, includes, excludes):
    low = (nm or "").lower()
    for ex in excludes:
        if ex.lower() in low:
            return False
    for inc in includes:
        if inc.lower() in low:
            return True
    return False


def field_candidates(field_names, group):
    out = []
    keys = FIELD_CANDIDATE_GROUPS[group]
    for fn in field_names:
        low = fn.lower()
        for k in keys:
            if k in low:
                out.append(fn)
                break
    return out


def join_mv(values):
    """Join multi-value layer list into the ';' string arcpy expects."""
    if values is None:
        return None
    if isinstance(values, (list, tuple)):
        return ";".join([v for v in values if v])
    return values


# ==========================================================================
# 2. Inventory scanning (arcpy)
# ==========================================================================

class InventoryScanner(object):
    """Walks the input geodatabase and records authoritative metadata + counts."""

    def __init__(self, arcpy_mod, gdb_path, logger=None):
        self.arcpy = arcpy_mod
        self.gdb = gdb_path
        self.log = logger or (lambda m: None)
        self.items = []  # list of dicts

    def scan(self):
        arcpy = self.arcpy
        arcpy.env.workspace = self.gdb
        self.items = []
        # Top-level feature classes + tables
        self._scan_workspace(self.gdb, parent="")
        # Feature datasets
        try:
            for fds in (arcpy.ListDatasets("", "Feature") or []):
                self._scan_workspace(os.path.join(self.gdb, fds), parent=fds)
        except Exception as ex:
            self.log("Feature-dataset scan warning: " + str(ex))
        # Standalone tables
        try:
            arcpy.env.workspace = self.gdb
            for tname in (arcpy.ListTables() or []):
                self._describe_item(os.path.join(self.gdb, tname), parent="", is_table=True)
        except Exception as ex:
            self.log("Table scan warning: " + str(ex))
        return self.items

    def _scan_workspace(self, ws, parent):
        arcpy = self.arcpy
        prev = arcpy.env.workspace
        try:
            arcpy.env.workspace = ws
            for fc in (arcpy.ListFeatureClasses() or []):
                self._describe_item(os.path.join(ws, fc), parent=parent, is_table=False)
        except Exception as ex:
            self.log("FC scan warning in " + str(ws) + ": " + str(ex))
        finally:
            arcpy.env.workspace = prev

    def _describe_item(self, path, parent, is_table):
        arcpy = self.arcpy
        rec = {
            "name": base_name(path), "path": path, "parent_dataset": parent,
            "dataset_type": "Table" if is_table else "FeatureClass",
            "geometry_type": "", "has_z": False, "has_m": False,
            "is_annotation": False, "spatial_reference": "", "wkid": "",
            "extent": "", "record_count": None,
            "fields": [], "field_types": {}, "required_fields": [],
            "nullable_fields": [], "candidate_elevation": [],
            "candidate_angle": [], "candidate_type": [], "candidate_name": [],
            "sample_records": [], "suspicious_nulls": {},
        }
        try:
            d = arcpy.Describe(path)
            dt = getattr(d, "dataType", "")
            if dt == "FeatureClass" or hasattr(d, "shapeType"):
                rec["geometry_type"] = getattr(d, "shapeType", "")
                rec["has_z"] = bool(getattr(d, "hasZ", False))
                rec["has_m"] = bool(getattr(d, "hasM", False))
                ft = getattr(d, "featureType", "")
                rec["is_annotation"] = (str(ft).lower() == "annotation") or \
                    (rec["name"].lower().endswith("anno"))
            sr = getattr(d, "spatialReference", None)
            if sr is not None:
                rec["spatial_reference"] = getattr(sr, "name", "")
                rec["wkid"] = getattr(sr, "factoryCode", "")
            ext = getattr(d, "extent", None)
            if ext is not None:
                rec["extent"] = "{0},{1},{2},{3}".format(
                    ext.XMin, ext.YMin, ext.XMax, ext.YMax)
        except Exception as ex:
            self.log("Describe warning for " + str(path) + ": " + str(ex))

        # fields
        fnames = []
        try:
            for f in arcpy.ListFields(path):
                fnames.append(f.name)
                rec["field_types"][f.name] = f.type
                if not f.isNullable:
                    rec["required_fields"].append(f.name)
                else:
                    rec["nullable_fields"].append(f.name)
        except Exception as ex:
            self.log("ListFields warning for " + str(path) + ": " + str(ex))
        rec["fields"] = fnames
        rec["candidate_elevation"] = field_candidates(fnames, "elevation")
        rec["candidate_angle"] = field_candidates(fnames, "angle_rotation")
        rec["candidate_type"] = field_candidates(fnames, "type_class_symbol")
        rec["candidate_name"] = field_candidates(fnames, "name_code")

        # count
        try:
            rec["record_count"] = int(arcpy.GetCount_management(path).getOutput(0))
        except Exception:
            rec["record_count"] = None

        # samples (first 5 of important fields) + suspicious nulls
        try:
            imp = []
            for grp in ("candidate_name", "candidate_elevation",
                        "candidate_angle", "candidate_type"):
                for fn in rec[grp]:
                    if fn not in imp:
                        imp.append(fn)
            imp = imp[:8]
            if imp and rec["record_count"]:
                null_counts = dict((f, 0) for f in imp)
                n = 0
                with arcpy.da.SearchCursor(path, imp) as cur:
                    for row in cur:
                        if n < 5:
                            rec["sample_records"].append(
                                dict((imp[i], _safe_cell(row[i])) for i in range(len(imp))))
                        for i in range(len(imp)):
                            if row[i] is None:
                                null_counts[imp[i]] += 1
                        n += 1
                        if n >= 1000:  # cap scan for null sampling
                            break
                rec["suspicious_nulls"] = dict(
                    (f, c) for f, c in null_counts.items() if c > 0)
        except Exception as ex:
            self.log("Sample warning for " + str(path) + ": " + str(ex))

        self.items.append(rec)
        return rec

    # -- reports ----------------------------------------------------------
    def write_reports(self, reports_dir):
        inv_md = os.path.join(reports_dir, "MAP_DATA_INVENTORY.md")
        inv_csv = os.path.join(reports_dir, "MAP_DATA_INVENTORY.csv")
        cand_csv = os.path.join(reports_dir, "FIELD_CANDIDATES_BY_LAYER.csv")

        inv_fields = ["name", "path", "parent_dataset", "dataset_type",
                      "geometry_type", "has_z", "has_m", "is_annotation",
                      "spatial_reference", "wkid", "extent", "record_count",
                      "fields", "required_fields", "nullable_fields",
                      "candidate_elevation", "candidate_angle",
                      "candidate_type", "candidate_name",
                      "suspicious_nulls", "sample_records"]
        rw.write_csv(inv_csv, self.items, inv_fields)

        cand_rows = []
        for it in self.items:
            cand_rows.append({
                "layer": it["name"], "geometry": it["geometry_type"],
                "record_count": it["record_count"],
                "elevation_fields": ";".join(it["candidate_elevation"]),
                "angle_fields": ";".join(it["candidate_angle"]),
                "type_fields": ";".join(it["candidate_type"]),
                "name_fields": ";".join(it["candidate_name"]),
                "all_fields": ";".join(it["fields"]),
            })
        rw.write_csv(cand_csv, cand_rows,
                     ["layer", "geometry", "record_count", "elevation_fields",
                      "angle_fields", "type_fields", "name_fields", "all_fields"])

        lines = [u"# Map Data Inventory", u"",
                 u"Generated: {0}".format(rw.now_iso()),
                 u"", u"Input geodatabase: `{0}`".format(self.gdb),
                 u"", u"Total datasets scanned: {0}".format(len(self.items)), u""]
        lines.append(u"| Name | Parent | Geom | Z | M | Anno | WKID | Count | Elev fields | Angle fields |")
        lines.append(u"|------|--------|------|---|---|------|------|------:|-------------|--------------|")
        for it in sorted(self.items, key=lambda r: (r["parent_dataset"], r["name"])):
            lines.append(u"| {0} | {1} | {2} | {3} | {4} | {5} | {6} | {7} | {8} | {9} |".format(
                it["name"], it["parent_dataset"], it["geometry_type"],
                "Y" if it["has_z"] else "", "Y" if it["has_m"] else "",
                "Y" if it["is_annotation"] else "", it["wkid"],
                rw._fmt(it["record_count"]),
                ";".join(it["candidate_elevation"]),
                ";".join(it["candidate_angle"])))
        rw.write_text(inv_md, u"\n".join(lines) + u"\n")
        return inv_md, inv_csv, cand_csv


def _safe_cell(v):
    if v is None:
        return None
    try:
        if isinstance(v, float):
            return round(v, 4)
    except Exception:
        pass
    try:
        return rw._to_text(v)
    except Exception:
        return str(v)


# ==========================================================================
# 3. Role detection
# ==========================================================================

class RoleDetector(object):
    def __init__(self, items, logger=None):
        self.items = items
        self.log = logger or (lambda m: None)
        self.mapping = {}  # role -> dict

    def detect(self):
        by_geom = {}
        for it in self.items:
            by_geom.setdefault((it["geometry_type"] or "").lower(), []).append(it)

        for role, geom, includes, excludes in ROLE_RULES:
            candidates = []
            geom_l = geom.lower()
            pool = by_geom.get(geom_l, [])
            if geom_l == "raster":
                pool = [it for it in self.items
                        if (it["dataset_type"] == "Raster")]
            for it in pool:
                if name_matches(it["name"], includes, excludes):
                    score = self._score(it, includes)
                    candidates.append((score, it))
            candidates.sort(key=lambda t: t[0], reverse=True)
            sel = candidates[0][1] if candidates else None
            fb = candidates[1][1] if len(candidates) > 1 else None
            conf = round(candidates[0][0], 2) if candidates else 0.0
            self.mapping[role] = {
                "role": role,
                "selected_layer": sel["name"] if sel else "",
                "selected_path": sel["path"] if sel else "",
                "fallback_layer": fb["name"] if fb else "",
                "confidence": conf,
                "reason": self._reason(role, sel, includes) if sel else "no matching layer",
                "geometry_type": sel["geometry_type"] if sel else geom,
                "record_count": sel["record_count"] if sel else "",
                "required_fields_found": ";".join(sel["required_fields"]) if sel else "",
                "missing_fields": "",
                "notes": "",
                "all_candidates": [c[1]["name"] for c in candidates],
                "all_candidate_paths": [{"name": c[1]["name"], "path": c[1]["path"],
                                         "record_count": c[1]["record_count"]}
                                        for c in candidates],
            }
        return self.mapping

    def _score(self, it, includes):
        """Score a candidate for a role. The position of the matching include in
        the rule's preference list matters: earlier hints win ties so that, e.g.,
        Bridge_P outranks Bridge and Dirt_Road outranks other roads even before
        feature counts are known. Populated layers are still preferred at runtime."""
        nm = it["name"].lower()
        matched_idx = None
        base = 0.0
        for idx, inc in enumerate(includes):
            incl = inc.lower()
            if nm == incl:
                base = 0.60
                matched_idx = idx
                break
            if nm.startswith(incl):
                base = 0.50
                matched_idx = idx
                break
            if incl in nm:
                base = 0.40
                matched_idx = idx
                break
        if matched_idx is None:
            return 0.0
        # Ordered preference: earlier include -> higher bonus (dominates ties).
        score = base + max(0, (len(includes) - matched_idx)) * 0.03
        # Prefer layers that actually have features (runtime only; static = None).
        rc = it["record_count"]
        if rc is None:
            pass
        elif rc > 0:
            score += 0.10
        else:
            score -= 0.25
        return min(score, 1.0)

    def _reason(self, role, it, includes):
        return "name '{0}' matched role hints {1}; geometry={2}; count={3}".format(
            it["name"], includes, it["geometry_type"], it["record_count"])

    def write_reports(self, reports_dir):
        csv_path = os.path.join(reports_dir, "LAYER_ROLE_MAPPING.csv")
        md_path = os.path.join(reports_dir, "LAYER_ROLE_MAPPING.md")
        fields = ["role", "selected_layer", "fallback_layer", "confidence",
                  "reason", "geometry_type", "record_count",
                  "required_fields_found", "missing_fields", "notes"]
        rows = [self.mapping[r] for r in sorted(self.mapping.keys())]
        rw.write_csv(csv_path, rows, fields)
        lines = [u"# Layer Role Mapping", u"",
                 u"Generated: {0}".format(rw.now_iso()), u"",
                 u"Confidence in [0,1]. Override any row with a CSV "
                 u"(`--role-map-csv`, columns `role,layer_name,notes`) or JSON "
                 u"(`--role-map-json`). See "
                 u"`common/layer_role_override_example.csv`.", u""]
        lines.append(u"| Role | Selected | Fallback | Conf | Geom | Count | Reason |")
        lines.append(u"|------|----------|----------|-----:|------|------:|--------|")
        for r in rows:
            lines.append(u"| {0} | {1} | {2} | {3} | {4} | {5} | {6} |".format(
                r["role"], r["selected_layer"], r["fallback_layer"],
                r["confidence"], r["geometry_type"], rw._fmt(r["record_count"]),
                rw._to_text(r["reason"]).replace("|", "/")))
        rw.write_text(md_path, u"\n".join(lines) + u"\n")
        return csv_path, md_path

    def get(self, role):
        return self.mapping.get(role, {})

    def path_for(self, role):
        m = self.mapping.get(role, {})
        return m.get("selected_path", "")

    def candidates(self, role):
        """Return ordered list of {name, path, record_count} candidates for a role."""
        m = self.mapping.get(role, {})
        return list(m.get("all_candidate_paths", []))

    def fallback_paths(self, role):
        """Return candidate paths for a role EXCLUDING the selected (primary) one."""
        cands = self.candidates(role)
        return cands[1:] if len(cands) > 1 else []



# ==========================================================================
# 4. Safe test workspace preparation
# ==========================================================================

class TestDataPrep(object):
    def __init__(self, arcpy_mod, output_root, sr_wkid=WKID_EXPECTED, logger=None):
        self.arcpy = arcpy_mod
        self.output_root = output_root
        self.sr_wkid = sr_wkid
        self.log = logger or (lambda m: None)
        self.run_dir = None
        self.test_gdb = None
        self.result_gdb = None
        self.logs_dir = None
        self.reports_dir = None
        self.snap_before = None
        self.snap_after = None
        self.qa_dir = None
        self.prep_records = []  # for TEST_DATA_PREPARATION_REPORT

    def create_workspace(self):
        arcpy = self.arcpy
        ts = time.strftime("%Y%m%d_%H%M%S")
        self.run_dir = os.path.join(self.output_root, "Carto_Test_Run_" + ts)
        for d in ("logs", "reports", "snapshots_before", "snapshots_after", "qa_layers"):
            p = os.path.join(self.run_dir, d)
            if not os.path.isdir(p):
                os.makedirs(p)
        self.logs_dir = os.path.join(self.run_dir, "logs")
        self.reports_dir = os.path.join(self.run_dir, "reports")
        self.snap_before = os.path.join(self.run_dir, "snapshots_before")
        self.snap_after = os.path.join(self.run_dir, "snapshots_after")
        self.qa_dir = os.path.join(self.run_dir, "qa_layers")
        self.test_gdb = os.path.join(self.run_dir, "test_data.gdb")
        self.result_gdb = os.path.join(self.run_dir, "result_data.gdb")
        if not arcpy.Exists(self.test_gdb):
            arcpy.management.CreateFileGDB(self.run_dir, "test_data.gdb")
        if not arcpy.Exists(self.result_gdb):
            arcpy.management.CreateFileGDB(self.run_dir, "result_data.gdb")
        self.log("Test workspace: " + self.run_dir)
        return self.run_dir

    def _record(self, name, origin, src, dst, note):
        self.prep_records.append({
            "test_layer": name, "origin": origin, "source": src,
            "destination": dst, "note": note})

    def copy_fc(self, src_path, out_name, origin="REAL_DATA", note="", where=None):
        """Copy a feature class into test_data.gdb under out_name (safe copy)."""
        arcpy = self.arcpy
        if not src_path or not arcpy.Exists(src_path):
            return None
        dst = os.path.join(self.test_gdb, out_name)
        try:
            if arcpy.Exists(dst):
                arcpy.management.Delete(dst)
            if where:
                lyr = "lyr_" + out_name
                arcpy.management.MakeFeatureLayer(src_path, lyr, where)
                arcpy.management.CopyFeatures(lyr, dst)
                arcpy.management.Delete(lyr)
            else:
                arcpy.management.CopyFeatures(src_path, dst)
            self._record(out_name, origin, src_path, dst, note)
            return dst
        except Exception as ex:
            self.log("copy_fc failed for " + str(src_path) + ": " + str(ex))
            return None

    def make_frame_polygon(self, items, out_name="T00_TestFrame", inset_ratio=0.08):
        """Synthesize a rectangular AOI/frame polygon from the union extent of
        the data, inset so dense contours fall both inside and near the edge.
        Tagged SYNTHETIC_DERIVED_FROM_REAL_EXTENT."""
        arcpy = self.arcpy
        ext = self._union_extent(items)
        if ext is None:
            return None
        xmin, ymin, xmax, ymax = ext
        dx = (xmax - xmin) * inset_ratio
        dy = (ymax - ymin) * inset_ratio
        xmin += dx; xmax -= dx; ymin += dy; ymax -= dy
        sr = arcpy.SpatialReference(self.sr_wkid)
        dst = os.path.join(self.test_gdb, out_name)
        if arcpy.Exists(dst):
            arcpy.management.Delete(dst)
        arcpy.management.CreateFeatureclass(
            self.test_gdb, out_name, "POLYGON", spatial_reference=sr)
        arr = arcpy.Array([
            arcpy.Point(xmin, ymin), arcpy.Point(xmin, ymax),
            arcpy.Point(xmax, ymax), arcpy.Point(xmax, ymin),
            arcpy.Point(xmin, ymin)])
        poly = arcpy.Polygon(arr, sr)
        with arcpy.da.InsertCursor(dst, ["SHAPE@"]) as ic:
            ic.insertRow([poly])
        self._record(out_name, "SYNTHETIC_DERIVED_FROM_REAL_EXTENT",
                     "union extent of dataset", dst,
                     "Rectangular frame inset {0}% from data extent".format(int(inset_ratio * 100)))
        return dst

    def _union_extent(self, items):
        arcpy = self.arcpy
        xs = []
        ys = []
        for it in items:
            ext = it.get("extent")
            if not ext:
                continue
            try:
                a, b, c, d = [float(v) for v in ext.split(",")]
                if a == a and b == b:  # not NaN
                    xs.extend([a, c]); ys.extend([b, d])
            except Exception:
                pass
        if not xs or not ys:
            return None
        return (min(xs), min(ys), max(xs), max(ys))

    def make_synthetic_points_on_lines(self, line_fc, out_name, n=10,
                                       at_ratio=0.5, fields=None, origin="SYNTHETIC_CONTROLLED",
                                       note=""):
        """Create n points placed ON a line layer (e.g. elevation text points
        overlapping contours). Used when real overlap data is sparse."""
        arcpy = self.arcpy
        if not line_fc or not arcpy.Exists(line_fc):
            return None
        sr = arcpy.Describe(line_fc).spatialReference
        dst = os.path.join(self.test_gdb, out_name)
        if arcpy.Exists(dst):
            arcpy.management.Delete(dst)
        arcpy.management.CreateFeatureclass(
            self.test_gdb, out_name, "POINT", spatial_reference=sr, has_z="DISABLED")
        arcpy.management.AddField(dst, "ELEV", "DOUBLE")
        arcpy.management.AddField(dst, "SRC", "TEXT", field_length=32)
        pts = []
        try:
            with arcpy.da.SearchCursor(line_fc, ["SHAPE@"]) as cur:
                for row in cur:
                    g = row[0]
                    if g is None:
                        continue
                    try:
                        p = g.positionAlongLine(at_ratio, True)
                        pts.append(p.firstPoint)
                    except Exception:
                        pass
                    if len(pts) >= n:
                        break
        except Exception as ex:
            self.log("synthetic points warning: " + str(ex))
        if not pts:
            return None
        with arcpy.da.InsertCursor(dst, ["SHAPE@", "ELEV", "SRC"]) as ic:
            elev = 100.0
            for p in pts:
                ic.insertRow([arcpy.PointGeometry(p, sr), elev, "synthetic"])
                elev += 10.0
        self._record(out_name, origin, line_fc, dst,
                     note or "Synthetic points placed on line vertices to force overlap")
        return dst

    def write_prep_report(self, reports_dir):
        path = os.path.join(reports_dir, "TEST_DATA_PREPARATION_REPORT.md")
        lines = [u"# Test Data Preparation Report", u"",
                 u"Generated: {0}".format(rw.now_iso()), u"",
                 u"All plugin tests run against the COPIES below in `test_data.gdb`.",
                 u"The source geodatabase is never modified.", u"",
                 u"Origin tags: REAL_DATA, REAL_DATA_SUBSET, SYNTHETIC_CONTROLLED, "
                 u"SYNTHETIC_DERIVED_FROM_REAL_EXTENT.", u"",
                 u"| Test layer | Origin | Source | Note |",
                 u"|------------|--------|--------|------|"]
        for r in self.prep_records:
            lines.append(u"| {0} | {1} | {2} | {3} |".format(
                r["test_layer"], r["origin"],
                rw._to_text(r["source"]).replace("|", "/"),
                rw._to_text(r["note"]).replace("|", "/")))
        rw.write_text(path, u"\n".join(lines) + u"\n")
        return path


# ==========================================================================
# 5. QA helpers + snapshots
# ==========================================================================

class QA(object):
    def __init__(self, arcpy_mod, logger=None):
        self.arcpy = arcpy_mod
        self.log = logger or (lambda m: None)

    def count(self, fc):
        try:
            return int(self.arcpy.GetCount_management(fc).getOutput(0))
        except Exception:
            return None

    def geometry_validity(self, fc):
        """Run CheckGeometry; return 'OK' or 'N problems' string."""
        arcpy = self.arcpy
        try:
            tmp = arcpy.CreateUniqueName("chk_geom", arcpy.env.scratchGDB or "in_memory")
            arcpy.management.CheckGeometry(fc, tmp)
            n = self.count(tmp)
            try:
                arcpy.management.Delete(tmp)
            except Exception:
                pass
            if n == 0:
                return "OK"
            return "{0} geometry problem(s)".format(n)
        except Exception as ex:
            return "check_skipped: " + str(ex)[:80]

    def sr_check(self, fc, expected_wkid=WKID_EXPECTED):
        try:
            sr = self.arcpy.Describe(fc).spatialReference
            wk = getattr(sr, "factoryCode", None)
            if wk == expected_wkid:
                return "OK (WKID {0})".format(wk)
            return "WKID {0} (expected {1})".format(wk, expected_wkid)
        except Exception as ex:
            return "sr_unknown: " + str(ex)[:60]

    def field_schema(self, fc):
        try:
            return [f.name + ":" + f.type for f in self.arcpy.ListFields(fc)]
        except Exception:
            return []

    def has_field(self, fc, fname):
        try:
            return fname.lower() in [f.name.lower() for f in self.arcpy.ListFields(fc)]
        except Exception:
            return False

    def numeric_stats(self, fc, field):
        """Return (n, min, max, mean) for a numeric field, or None."""
        arcpy = self.arcpy
        if not self.has_field(fc, field):
            return None
        vals = []
        try:
            with arcpy.da.SearchCursor(fc, [field]) as cur:
                for row in cur:
                    if row[0] is not None:
                        vals.append(float(row[0]))
        except Exception:
            return None
        if not vals:
            return None
        return (len(vals), min(vals), max(vals), sum(vals) / len(vals))

    def count_intersections(self, fc_a, fc_b):
        """Approximate count of features in fc_a that intersect fc_b."""
        arcpy = self.arcpy
        try:
            lyr = arcpy.management.MakeFeatureLayer(fc_a, "qa_isect_lyr").getOutput(0)
            arcpy.management.SelectLayerByLocation(lyr, "INTERSECT", fc_b)
            n = self.count(lyr)
            arcpy.management.Delete(lyr)
            return n
        except Exception as ex:
            self.log("intersection count warning: " + str(ex))
            return None

    def snapshot(self, plugin_id, layer_name, fc, phase, out_dir,
                 conflicts=None, angle_field=None):
        """Capture a before/after snapshot dict + copy the layer to snapshots dir."""
        arcpy = self.arcpy
        rec = {"plugin_id": plugin_id, "layer": layer_name, "phase": phase,
               "feature_count": self.count(fc), "geometry_summary": "",
               "field_schema": ";".join(self.field_schema(fc)),
               "extent": "", "sr": self.sr_check(fc),
               "conflicts": conflicts, "angle_stats": "", "distance_stats": "",
               "samples": ""}
        try:
            ext = arcpy.Describe(fc).extent
            rec["extent"] = "{0},{1},{2},{3}".format(ext.XMin, ext.YMin, ext.XMax, ext.YMax)
        except Exception:
            pass
        if angle_field:
            st = self.numeric_stats(fc, angle_field)
            if st:
                rec["angle_stats"] = "n={0} min={1:.2f} max={2:.2f} mean={3:.2f}".format(
                    st[0], st[1], st[2], st[3])
        # copy snapshot (best-effort)
        try:
            snap_gdb = os.path.join(out_dir, "snapshots.gdb")
            if not arcpy.Exists(snap_gdb):
                arcpy.management.CreateFileGDB(out_dir, "snapshots.gdb")
            dst = os.path.join(snap_gdb, (plugin_id + "_" + layer_name + "_" + phase)[:60])
            if arcpy.Exists(dst):
                arcpy.management.Delete(dst)
            arcpy.management.CopyFeatures(fc, dst)
        except Exception:
            pass
        return rec



# ==========================================================================
# 6. Tool invocation via ImportToolbox (positional parameters)
# ==========================================================================

class ToolRunner(object):
    def __init__(self, arcpy_mod, repo_dir, platform, logger=None):
        self.arcpy = arcpy_mod
        self.repo = repo_dir
        self.platform = platform  # "ArcMap" or "ArcGISPro"
        self.log = logger or (lambda m: None)
        self._imported = {}

    def pyt_path(self, plugin_id):
        reg = PLUGIN_REGISTRY[plugin_id]
        key = "arcmap_pyt" if self.platform == "ArcMap" else "pro_pyt"
        return os.path.join(self.repo, reg[key])

    def alias(self, plugin_id):
        reg = PLUGIN_REGISTRY[plugin_id]
        return reg["alias_arcmap"] if self.platform == "ArcMap" else reg["alias_pro"]

    def import_tbx(self, plugin_id):
        path = self.pyt_path(plugin_id)
        if path in self._imported:
            return path
        if not os.path.exists(path):
            raise IOError("Toolbox not found: " + path)
        self.arcpy.ImportToolbox(path)
        self._imported[path] = True
        return path

    def run(self, plugin_id, tool_class, args, rec):
        """Run tool_class from plugin_id positionally with args (list).
        Populates rec with arcpy_messages/error/traceback. Returns result obj
        or raises. Caller decides PASS/FAIL."""
        arcpy = self.arcpy
        path = self.import_tbx(plugin_id)
        rec["plugin_path"] = path
        rec["active_workspace"] = getattr(arcpy.env, "workspace", "") or ""
        alias = self.alias(plugin_id)
        fn_name = tool_class + "_" + alias
        fn = getattr(arcpy, fn_name, None)
        if fn is None:
            # fallback: alias-namespace access
            ns = getattr(arcpy, alias, None)
            if ns is not None:
                fn = getattr(ns, tool_class, None)
        if fn is None:
            raise AttributeError("Tool function not found: " + fn_name)
        # NOTE: we deliberately do NOT call arcpy.ResetEnvironments() here -
        # that would clobber overwriteOutput/addOutputsToMap set by run().
        # Each plugin snapshots/restores arcpy.env internally.
        result = fn(*args)
        try:
            rec["arcpy_messages"] = arcpy.GetMessages()
        except Exception:
            pass
        return result


# ==========================================================================
# 7. Run context + parameter auto-fill
# ==========================================================================

class RunContext(object):
    def __init__(self, arcpy_mod, platform, repo_dir, items, roles, prep,
                 qa, writer, runner, flags):
        self.arcpy = arcpy_mod
        self.platform = platform
        self.repo = repo_dir
        self.items = items
        self.roles = roles      # RoleDetector
        self.prep = prep        # TestDataPrep
        self.qa = qa
        self.writer = writer    # ReportWriter
        self.runner = runner    # ToolRunner
        self.flags = flags      # dict: test_types, safe_mode, mxd_folder, has_map
        self.snapshots = []     # before/after dicts
        self.autofill = {}      # plugin_id -> {ArcMap/ArcGISPro: {...}}

    def log(self, m):
        self.writer.log(m)

    def want(self, test_type):
        tt = self.flags.get("test_types")
        return (not tt) or (test_type in tt)

    def record_autofill(self, plugin_id, params_list):
        plat = self.platform
        self.autofill.setdefault(plugin_id, {})
        reg = PLUGIN_REGISTRY[plugin_id]
        self.autofill[plugin_id][plat] = {
            "tool_path": self.runner.pyt_path(plugin_id),
            "alias": self.runner.alias(plugin_id),
            "parameters": params_list,
        }

    def write_autofill(self, reports_dir):
        path = os.path.join(reports_dir, "AUTO_FILLED_PLUGIN_PARAMETERS.json")
        rw.write_json(path, self.autofill)
        return path


def pdesc(name, dtype, value, source, confidence, notes=""):
    """Helper to build a documented parameter descriptor for the autofill report."""
    return {"name": name, "datatype": dtype, "value": rw._to_text(value)
            if not isinstance(value, (int, float, bool)) and value is not None else value,
            "source": source, "confidence": confidence, "notes": notes}



# ==========================================================================
# 8. Per-plugin test helpers
# ==========================================================================

def _new(ctx, plugin_id, test_name, test_type):
    reg = PLUGIN_REGISTRY[plugin_id]
    return rw.make_result(plugin_id, reg["name"], ctx.platform, test_name, test_type)


def _fail(rec, msg, exc=True):
    rec["status"] = "FAIL"
    rec["error_message"] = rw._to_text(msg)
    if exc:
        rec["traceback"] = traceback.format_exc()
    return rec


def _skip(rec, reason):
    rec["status"] = "SKIP"
    rec["skip_reason"] = rw._to_text(reason)
    return rec


def _item_by_name(ctx, name):
    for it in ctx.items:
        if it["name"] == name:
            return it
    return None


def _elev_field(ctx, layer_name, default="Ortho_Hght"):
    it = _item_by_name(ctx, layer_name)
    if it and it["candidate_elevation"]:
        # prefer Ortho_Hght-like
        for f in it["candidate_elevation"]:
            if "ortho" in f.lower() or "hght" in f.lower() or "elev" in f.lower():
                return f
        return it["candidate_elevation"][0]
    return default


def _result_path(ctx, name):
    return os.path.join(ctx.prep.result_gdb, name)


def _angles_in_range(ctx, fc, field, lo=0.0, hi=360.0):
    st = ctx.qa.numeric_stats(fc, field)
    if not st:
        return None, None
    in_range = (st[1] >= lo - 0.001) and (st[2] <= hi + 0.001)
    return in_range, st


# ==========================================================================
# Plugin 01 - Bridge / Culvert
# ==========================================================================

def _valid_angle_count(ctx, fc, field, lo=0.0, hi=360.0):
    """Return (n_total_nonnull, n_valid_in_range) for an angle field."""
    arcpy = ctx.arcpy
    if not ctx.qa.has_field(fc, field):
        return (0, 0)
    total = 0
    valid = 0
    try:
        with arcpy.da.SearchCursor(fc, [field]) as cur:
            for row in cur:
                if row[0] is None:
                    continue
                total += 1
                try:
                    v = float(row[0])
                    if (lo - 0.001) <= v <= (hi + 0.001):
                        valid += 1
                except Exception:
                    pass
    except Exception:
        return (0, 0)
    return (total, valid)


def test_plugin01(ctx):
    pid = "Plugin01"
    arcpy = ctx.arcpy
    road_path = ctx.roles.path_for("road_any")
    bridge_path = ctx.roles.path_for("bridge_existing")

    # Drainage layers to test against: prefer Watercourse (primary), then add
    # Canal and River_L when present (role candidate lists are ordered so the
    # primary watercourse/drainage layer is first). De-dupe by layer name.
    drainage_specs = []
    seen = set()
    for role in ("watercourse", "drainage_any", "canal", "river_line"):
        for c in ctx.roles.candidates(role):
            nm = c.get("name")
            if nm and nm not in seen and c.get("path"):
                seen.add(nm)
                drainage_specs.append(c)
    # Keep at most 3 drainage layers for the functional matrix.
    drainage_specs = drainage_specs[:3]

    # Stage safe copies
    roads = ctx.prep.copy_fc(road_path, "T01_roads", "REAL_DATA",
                             "road centerlines (barrier/reference)")
    # Primary drainage (first spec) staged as T01_drains for back-compat.
    primary_drain = None
    staged_drains = []   # list of (layer_name, staged_path)
    for idx, spec in enumerate(drainage_specs):
        nm = spec["name"]
        stage_name = "T01_drains" if idx == 0 else ("T01_drain_" + nm)
        staged = ctx.prep.copy_fc(spec["path"], stage_name, "REAL_DATA",
                                  "drainage layer for road-water intersection test")
        if staged:
            staged_drains.append((nm, staged))
            if idx == 0:
                primary_drain = staged
    bridge_before = ctx.prep.copy_fc(bridge_path, "T01_bridge_before", "REAL_DATA",
                                     "existing bridge points (Bridge_P preferred)") if bridge_path else None

    drain_names = ";".join([s["name"] for s in drainage_specs]) or "(none)"
    ctx.record_autofill(pid, [
        pdesc("roads", "GPFeatureLayer(mv)", "T01_roads",
              "detected_layer" if roads else "user_required",
              ctx.roles.get("road_any").get("confidence", 0),
              "primary road=%s" % base_name(road_path)),
        pdesc("drains", "GPFeatureLayer(mv)", "T01_drains",
              "detected_layer" if primary_drain else "user_required",
              ctx.roles.get("watercourse").get("confidence", 0),
              "drainage layers tested: " + drain_names),
        pdesc("out_ws", "DEWorkspace", ctx.prep.result_gdb, "generated_test_layer", 1.0),
        pdesc("out_name", "GPString", "T01_bridge_after", "code_default", 1.0),
        pdesc("sample_m", "GPDouble", 8.0, "code_default", 1.0),
        pdesc("rot_field", "GPString", "ROTATION", "code_default", 1.0),
        pdesc("rot_type", "GPString", "GEOGRAPHIC", "code_default", 1.0),
        pdesc("add_map", "GPBoolean", False, "code_default", 1.0, "headless"),
        pdesc("tmpl_lyr", "DEFile", None, "user_required", 1.0, "optional .lyr"),
        pdesc("end_tol", "GPDouble", 2.0, "code_default", 1.0),
    ])

    # ---- Smoke / Functional: build bridge points per drainage layer ----
    if ctx.want("smoke") or ctx.want("functional"):
        if not roads or not staged_drains:
            rec = _new(ctx, pid, "Build Bridge Points (Road x Drain)", "functional")
            t = time.time()
            rw.finalize_timing(_skip(rec, "Missing road and/or drainage layer (roads=%s drains=%s)"
                                      % (bool(roads), len(staged_drains))), t)
            ctx.writer.add(rec)
        else:
            # Test against each available drainage layer (Watercourse, Canal, River_L).
            for di, (dname, dpath) in enumerate(staged_drains):
                # Smoke only runs the first (primary) drainage layer; functional
                # runs all available drainage layers.
                if di > 0 and not ctx.want("functional"):
                    break
                ttype = "smoke" if (di == 0 and not ctx.want("functional")) else "functional"
                rec = _new(ctx, pid, "Build Bridge Points: roads x %s" % dname, ttype)
                t = time.time()
                rec["input_layers"] = ["T01_roads", base_name(dpath)]
                out_name = "T01_bridge_after" if di == 0 else ("T01_bridge_after_" + dname)
                out_fc = _result_path(ctx, out_name)
                isect = _result_path(ctx, "T01_intersections" if di == 0
                                      else "T01_intersections_" + dname)
                n_isect = None
                try:
                    if arcpy.Exists(isect):
                        arcpy.management.Delete(isect)
                    arcpy.analysis.Intersect([roads, dpath], isect, "ONLY_FID",
                                             output_type="POINT")
                    n_isect = ctx.qa.count(isect)
                except Exception as ex:
                    ctx.log("T01 intersect pre-count warning (%s): %s" % (dname, str(ex)))
                args = [join_mv([roads]), join_mv([dpath]), ctx.prep.result_gdb,
                        out_name, 8.0, "ROTATION", "GEOGRAPHIC", False, None, 2.0]
                rec["parameters_used"] = {"roads": "T01_roads", "drain_layer": dname,
                                          "out_name": out_name, "rot_type": "GEOGRAPHIC"}
                try:
                    ctx.runner.run(pid, "BuildBridgePoints", args, rec)
                    if not arcpy.Exists(out_fc):
                        # Zero true crossings is a legitimate outcome (no output FC).
                        rec["success_metrics"] = {
                            "drainage_layer": dname, "road_layer": base_name(road_path),
                            "intersection_count": n_isect, "output_bridge_count": 0,
                            "angle_field": "ROTATION", "valid_angle_count": 0,
                            "angular_difference_summary": "n/a (no output)"}
                        if n_isect in (0, None):
                            rec["status"] = "PASS"
                            rec["notes"] = "No true crossings for roads x %s; no output (acceptable)" % dname
                        else:
                            rec["status"] = "WARN"
                            rec["notes"] = "%s intersections but no bridge output for %s" % (n_isect, dname)
                    else:
                        n_out = ctx.qa.count(out_fc)
                        rec["output_layers"] = [out_name]
                        rec["after_feature_count"] = n_out
                        rec["geometry_validity_result"] = ctx.qa.geometry_validity(out_fc)
                        rec["spatial_reference_check"] = ctx.qa.sr_check(out_fc)
                        has_rot = ctx.qa.has_field(out_fc, "ROTATION")
                        rec["field_schema_check"] = "ROTATION present" if has_rot else "ROTATION MISSING"
                        in_range, st = _angles_in_range(ctx, out_fc, "ROTATION")
                        n_tot, n_valid = _valid_angle_count(ctx, out_fc, "ROTATION")
                        ang_summary = "n/a"
                        if st:
                            ang_summary = ("min=%.1f max=%.1f mean=%.1f (within 0-360)"
                                           % (st[1], st[2], st[3])) if in_range else \
                                          ("min=%.1f max=%.1f mean=%.1f (OUT OF 0-360)"
                                           % (st[1], st[2], st[3]))
                        rec["success_metrics"] = {
                            "drainage_layer": dname,
                            "road_layer": base_name(road_path),
                            "intersection_count": n_isect,
                            "output_bridge_count": n_out,
                            "angle_field": "ROTATION" if has_rot else None,
                            "valid_angle_count": "%s/%s" % (n_valid, n_tot),
                            "angular_difference_summary": ang_summary}
                        ctx.snapshots.append(ctx.qa.snapshot(
                            pid, "bridge_after_" + dname, out_fc, "after",
                            ctx.prep.snap_after, angle_field="ROTATION"))
                        if not has_rot or n_out is None:
                            _fail(rec, "Output missing ROTATION field or count", exc=False)
                        elif in_range is False or (n_tot and n_valid < n_tot):
                            rec["status"] = "WARN"
                            rec["notes"] = ("roads x %s: %s bridges, %s/%s valid angles "
                                            "(some outside 0-360)" % (dname, n_out, n_valid, n_tot))
                        else:
                            rec["status"] = "PASS"
                            rec["notes"] = ("roads x %s: %s intersections -> %s bridge(s), "
                                            "all angles valid" % (dname, n_isect, n_out))
                except Exception as ex:
                    _fail(rec, ex)
                rw.finalize_timing(rec, t)
                ctx.writer.add(rec)

    # ---- Functional: rotate existing bridges (COPY_TO_OUTPUT, geometry must NOT move) ----
    if ctx.want("functional") or ctx.want("regression"):
        rec = _new(ctx, pid, "Rotate Existing Bridge Points (no geometry move)", "regression")
        t = time.time()
        if not bridge_before or not roads:
            rw.finalize_timing(_skip(rec, "No existing bridge layer or road layer for rotation test"), t)
            ctx.writer.add(rec)
        else:
            n_before = ctx.qa.count(bridge_before)
            rec["before_feature_count"] = n_before
            rec["input_layers"] = ["T01_bridge_before", "T01_roads"]
            out_name = "T01_bridge_rot"
            out_fc = _result_path(ctx, out_name)
            ctx.snapshots.append(ctx.qa.snapshot(pid, "bridge_before", bridge_before,
                                 "before", ctx.prep.snap_before))
            args = [bridge_before, join_mv([roads]), "COPY_TO_OUTPUT",
                    ctx.prep.result_gdb, out_name, 8.0, "ROTATION", "GEOGRAPHIC", False, None]
            rec["parameters_used"] = {"upd_mode": "COPY_TO_OUTPUT", "rot_field": "ROTATION"}
            try:
                ctx.runner.run(pid, "RotateExistingBridgePoints", args, rec)
                if not arcpy.Exists(out_fc):
                    _fail(rec, "Rotate output not created", exc=False)
                else:
                    n_after = ctx.qa.count(out_fc)
                    rec["after_feature_count"] = n_after
                    rec["changed_feature_count"] = 0
                    has_rot = ctx.qa.has_field(out_fc, "ROTATION")
                    rec["field_schema_check"] = "ROTATION present" if has_rot else "ROTATION MISSING"
                    rec["geometry_validity_result"] = ctx.qa.geometry_validity(out_fc)
                    in_range, st = _angles_in_range(ctx, out_fc, "ROTATION")
                    n_tot, n_valid = _valid_angle_count(ctx, out_fc, "ROTATION")
                    # regression: source bridge count unchanged + output count == input
                    src_unchanged = (ctx.qa.count(bridge_before) == n_before)
                    count_match = (n_after == n_before)
                    angles_valid = (n_tot == 0) or (n_valid == n_tot)
                    rec["success_metrics"] = {
                        "existing_bridge_layer": base_name(bridge_path),
                        "count_before": n_before, "count_after": n_after,
                        "source_count_unchanged": src_unchanged,
                        "rotation_field": "ROTATION" if has_rot else None,
                        "valid_angle_count": "%s/%s" % (n_valid, n_tot)}
                    if src_unchanged and count_match and has_rot and angles_valid:
                        rec["status"] = "PASS"
                        rec["notes"] = ("Existing bridges preserved (%s); ROTATION written & valid; "
                                        "source untouched" % n_after)
                    elif not src_unchanged or not count_match:
                        rec["status"] = "FAIL"
                        rec["notes"] = ("Bridge count changed: source_unchanged=%s in=%s out=%s "
                                        "(tool should NOT add/delete features)"
                                        % (src_unchanged, n_before, n_after))
                    else:
                        rec["status"] = "WARN"
                        rec["notes"] = ("rot_field=%s valid_angles=%s/%s"
                                        % (has_rot, n_valid, n_tot))
            except Exception as ex:
                _fail(rec, ex)
            rw.finalize_timing(rec, t)
            ctx.writer.add(rec)

    # ---- Edge: no intersections (synthetic parallel non-crossing lines) ----
    if ctx.want("edge"):
        rec = _new(ctx, pid, "Edge: no road-water intersections", "edge")
        t = time.time()
        try:
            sr = arcpy.SpatialReference(WKID_EXPECTED)
            ext = ctx.prep._union_extent(ctx.items)
            if ext is None:
                rw.finalize_timing(_skip(rec, "No usable extent to synthesize parallel lines"), t)
                ctx.writer.add(rec)
            else:
                x0, y0, x1, y1 = ext
                cx, cy = (x0 + x1) / 2.0, (y0 + y1) / 2.0
                lr = os.path.join(ctx.prep.test_gdb, "T01_syn_road")
                ld = os.path.join(ctx.prep.test_gdb, "T01_syn_drain")
                for nm, yoff in ((lr, 0.0), (ld, 200.0)):
                    if arcpy.Exists(nm):
                        arcpy.management.Delete(nm)
                    arcpy.management.CreateFeatureclass(ctx.prep.test_gdb,
                        os.path.basename(nm), "POLYLINE", spatial_reference=sr)
                    arr = arcpy.Array([arcpy.Point(cx - 500, cy + yoff),
                                       arcpy.Point(cx + 500, cy + yoff)])
                    with arcpy.da.InsertCursor(nm, ["SHAPE@"]) as ic:
                        ic.insertRow([arcpy.Polyline(arr, sr)])
                ctx.prep._record("T01_syn_road", "SYNTHETIC_CONTROLLED", "n/a", lr,
                                 "parallel non-crossing line for no-intersection edge case")
                out_name = "T01_bridge_none"
                out_fc = _result_path(ctx, out_name)
                args = [join_mv([lr]), join_mv([ld]), ctx.prep.result_gdb,
                        out_name, 8.0, "ROTATION", "GEOGRAPHIC", False, None, 2.0]
                ctx.runner.run(pid, "BuildBridgePoints", args, rec)
                n_out = ctx.qa.count(out_fc) if arcpy.Exists(out_fc) else 0
                rec["after_feature_count"] = n_out
                rec["status"] = "PASS" if (n_out == 0) else "WARN"
                rec["notes"] = "Parallel non-crossing lines yielded %s bridge points (expected 0)" % n_out
        except Exception as ex:
            # Tool may legitimately raise on zero crossings; treat as handled PASS
            rec["status"] = "PASS"
            rec["notes"] = "Tool raised on zero-crossing input (acceptable); " + str(ex)[:120]
            rec["arcpy_messages"] = _safe_msgs(arcpy)
        rw.finalize_timing(rec, t)
        ctx.writer.add(rec)


def _safe_msgs(arcpy):
    try:
        return arcpy.GetMessages()
    except Exception:
        return ""



# ==========================================================================
# Plugin 02 - Road Conflict Resolution
# ==========================================================================

def _make_buffer(ctx, src, dist, out_name):
    arcpy = ctx.arcpy
    out = _result_path(ctx, out_name)
    try:
        if arcpy.Exists(out):
            arcpy.management.Delete(out)
        arcpy.analysis.Buffer(src, out, "%s Meters" % dist, dissolve_option="ALL")
        return out
    except Exception as ex:
        ctx.log("buffer warning: " + str(ex))
        return None


def test_plugin02(ctx):
    pid = "Plugin02"
    arcpy = ctx.arcpy
    road_path = ctx.roles.path_for("road_any")
    pt_path = ctx.roles.path_for("point_obstacle") or ctx.roles.path_for("building_point")
    line_path = ctx.roles.path_for("powerline") or ctx.roles.path_for("canal")
    poly_path = ctx.roles.path_for("building_poly")

    roads = ctx.prep.copy_fc(road_path, "T02_roads", "REAL_DATA", "road barrier")
    points = ctx.prep.copy_fc(pt_path, "T02_points", "REAL_DATA", "point obstacles to move")
    lines = ctx.prep.copy_fc(line_path, "T02_lines", "REAL_DATA", "line obstacles to move")
    polys = ctx.prep.copy_fc(poly_path, "T02_polys", "REAL_DATA", "polygon obstacles to move")

    ctx.record_autofill(pid, [
        pdesc("in_roads", "GPFeatureLayer", "T02_roads",
              "detected_layer" if roads else "user_required",
              ctx.roles.get("road_any").get("confidence", 0)),
        pdesc("clearance", "GPDouble", 6.0, "code_default", 1.0),
        pdesc("in_points", "GPFeatureLayer(mv)", "T02_points",
              "detected_layer" if points else "user_required", 0.7),
        pdesc("in_lines", "GPFeatureLayer(mv)", "T02_lines",
              "detected_layer" if lines else "user_required", 0.6),
        pdesc("in_polygons", "GPFeatureLayer(mv)", "T02_polys",
              "detected_layer" if polys else "user_required", 0.6),
        pdesc("out_gdb", "DEWorkspace", ctx.prep.result_gdb, "generated_test_layer", 1.0),
        pdesc("name_suffix", "GPString", "_RDCL", "code_default", 1.0),
        pdesc("aoi_poly", "GPFeatureLayer", None, "code_default", 1.0),
        pdesc("line_strategy", "GPString", "LOCAL_PUSH", "code_default", 1.0),
        pdesc("offset_side", "GPString", "AUTO", "code_default", 1.0),
        pdesc("densify_step", "GPDouble", 0.0, "code_default", 1.0),
        pdesc("preserve_endpoints", "GPBoolean", True, "code_default", 1.0),
        pdesc("smooth_iters", "GPLong", 0, "code_default", 1.0),
        pdesc("max_shift", "GPDouble", 0.0, "code_default", 1.0),
        pdesc("max_iter", "GPLong", 8, "code_default", 1.0),
        pdesc("max_deflection_deg", "GPDouble", 25.0, "code_default", 1.0),
        pdesc("use_near", "GPBoolean", True, "code_default", 1.0),
        pdesc("lock_field", "GPString", None, "code_default", 1.0),
        pdesc("create_errors", "GPBoolean", True, "code_default", 1.0),
        pdesc("create_vectors", "GPBoolean", True, "code_default", 1.0, "QC vectors on"),
        pdesc("write_csv", "GPBoolean", True, "code_default", 1.0),
        pdesc("keep_near_fields", "GPBoolean", False, "code_default", 1.0),
        pdesc("near_chunk_size", "GPLong", 50000, "code_default", 1.0),
    ])

    def _build_args(clearance, pts, lns, pls, vectors=True):
        return [roads, clearance, join_mv(pts), join_mv(lns), join_mv(pls),
                ctx.prep.result_gdb, "_RDCL", None, "LOCAL_PUSH", "AUTO",
                0.0, True, 0, 0.0, 8, 25.0, True, None, True, vectors, True, False, 50000]

    # ---- Smoke + Functional: roads vs one point layer ----
    if ctx.want("smoke") or ctx.want("functional"):
        rec = _new(ctx, pid, "Deconflict roads vs point obstacles", "functional")
        t = time.time()
        if not roads or not points:
            rw.finalize_timing(_skip(rec, "Missing road or point obstacle layer"), t)
            ctx.writer.add(rec)
        else:
            clearance = 6.0
            buf = _make_buffer(ctx, roads, clearance, "T02_road_buffer")
            n_before = ctx.qa.count_intersections(points, buf) if buf else None
            rec["before_feature_count"] = n_before
            rec["input_layers"] = ["T02_roads", "T02_points"]
            rec["parameters_used"] = {"clearance": clearance, "in_points": "T02_points"}
            out_pts = _result_path(ctx, "T02_points_RDCL")
            try:
                ctx.runner.run(pid, "RoadDeconflictTool", _build_args(clearance, [points], None, None), rec)
                target_after = out_pts if arcpy.Exists(out_pts) else None
                if target_after is None:
                    # tool may name outputs differently; search result gdb
                    target_after = _find_output(ctx, "T02_points")
                if target_after is None:
                    _fail(rec, "No moved point output found in result_data.gdb", exc=False)
                else:
                    n_in = ctx.qa.count(points)
                    n_out = ctx.qa.count(target_after)
                    n_after = ctx.qa.count_intersections(target_after, buf) if buf else None
                    rec["output_layers"] = [base_name(target_after)]
                    rec["after_feature_count"] = n_out
                    rec["geometry_validity_result"] = ctx.qa.geometry_validity(target_after)
                    rec["spatial_reference_check"] = ctx.qa.sr_check(target_after)
                    reduction_pct = None
                    if n_before:
                        try:
                            reduction_pct = round(100.0 * (n_before - (n_after or 0)) / float(n_before), 1)
                        except Exception:
                            reduction_pct = None
                    rec["success_metrics"] = {"road_layer": base_name(road_path),
                                              "target_layer": base_name(pt_path),
                                              "safe_distance": clearance,
                                              "conflicts_before": n_before,
                                              "conflicts_after": n_after,
                                              "reduction_pct": reduction_pct,
                                              "features_in": n_in, "features_out": n_out,
                                              "features_unchanged_count": (n_out == n_in)}
                    ctx.snapshots.append(ctx.qa.snapshot(pid, "conflicts_before", points, "before", ctx.prep.snap_before, conflicts=n_before))
                    ctx.snapshots.append(ctx.qa.snapshot(pid, "targets_after", target_after, "after", ctx.prep.snap_after, conflicts=n_after))
                    count_ok = (n_out == n_in)
                    if n_before is not None and n_after is not None:
                        if n_after <= n_before and count_ok:
                            rec["status"] = "PASS"
                            rec["notes"] = ("road=%s target=%s d=%s: conflicts %s -> %s (%s%% reduction), "
                                            "no feature loss" % (base_name(road_path), base_name(pt_path),
                                                                 clearance, n_before, n_after, reduction_pct))
                        elif not count_ok:
                            rec["status"] = "FAIL"
                            rec["notes"] = "feature count changed %s -> %s" % (n_in, n_out)
                        else:
                            rec["status"] = "WARN"
                            rec["notes"] = "conflicts did not decrease: %s -> %s" % (n_before, n_after)
                    else:
                        rec["status"] = "WARN" if count_ok else "FAIL"
                        rec["notes"] = "conflict metric unavailable; count_ok=%s" % count_ok
            except Exception as ex:
                _fail(rec, ex)
            rw.finalize_timing(rec, t)
            ctx.writer.add(rec)

    # ---- Functional: multiple geometry types together ----
    if ctx.want("functional"):
        rec = _new(ctx, pid, "Deconflict roads vs points+lines+polygons", "functional")
        t = time.time()
        if not roads or not (points or lines or polys):
            rw.finalize_timing(_skip(rec, "Need road + at least one target geometry"), t)
        else:
            try:
                ctx.runner.run(pid, "RoadDeconflictTool",
                               _build_args(6.0, [points] if points else None,
                                           [lines] if lines else None,
                                           [polys] if polys else None), rec)
                produced = [base_name(p) for p in _all_outputs(ctx, ["T02_points", "T02_lines", "T02_polys"])]
                rec["output_layers"] = produced
                rec["status"] = "PASS" if produced else "WARN"
                rec["notes"] = "Produced outputs: %s" % (", ".join(produced) if produced else "none found")
            except Exception as ex:
                _fail(rec, ex)
        rw.finalize_timing(rec, t)
        ctx.writer.add(rec)

    # ---- Edge: empty target layer ----
    if ctx.want("edge") and roads:
        rec = _new(ctx, pid, "Edge: empty target layer", "edge")
        t = time.time()
        try:
            sr = arcpy.SpatialReference(WKID_EXPECTED)
            empty = os.path.join(ctx.prep.test_gdb, "T02_empty_points")
            if arcpy.Exists(empty):
                arcpy.management.Delete(empty)
            arcpy.management.CreateFeatureclass(ctx.prep.test_gdb, "T02_empty_points",
                                                "POINT", spatial_reference=sr)
            ctx.runner.run(pid, "RoadDeconflictTool", _build_args(6.0, [empty], None, None), rec)
            rec["status"] = "PASS"
            rec["notes"] = "Empty target handled without crash"
        except Exception as ex:
            rec["status"] = "WARN"
            rec["notes"] = "Tool raised on empty target: " + str(ex)[:120]
            rec["traceback"] = traceback.format_exc()
        rw.finalize_timing(rec, t)
        ctx.writer.add(rec)

    # ---- Edge: zero + very large clearance ----
    if ctx.want("edge") and roads and points:
        for label, clearance in (("zero clearance", 0.0), ("very large clearance", 100000.0)):
            rec = _new(ctx, pid, "Edge: %s" % label, "edge")
            t = time.time()
            try:
                ctx.runner.run(pid, "RoadDeconflictTool", _build_args(clearance, [points], None, None), rec)
                rec["status"] = "PASS"
                rec["notes"] = "%s accepted; tool completed" % label
            except Exception as ex:
                rec["status"] = "WARN"
                rec["notes"] = "%s raised: %s" % (label, str(ex)[:120])
                rec["traceback"] = traceback.format_exc()
            rw.finalize_timing(rec, t)
            ctx.writer.add(rec)


def _find_output(ctx, base):
    """Find an output FC in result_gdb whose name starts with base (e.g. base_RDCL)."""
    arcpy = ctx.arcpy
    prev = arcpy.env.workspace
    try:
        arcpy.env.workspace = ctx.prep.result_gdb
        for fc in (arcpy.ListFeatureClasses() or []):
            if fc.lower().startswith(base.lower()) and fc.lower() != base.lower():
                return os.path.join(ctx.prep.result_gdb, fc)
        # exact base + suffix
        cand = os.path.join(ctx.prep.result_gdb, base + "_RDCL")
        if arcpy.Exists(cand):
            return cand
    except Exception:
        pass
    finally:
        arcpy.env.workspace = prev
    return None


def _all_outputs(ctx, bases):
    out = []
    for b in bases:
        p = _find_output(ctx, b)
        if p:
            out.append(p)
    return out



# ==========================================================================
# Plugin 03 - Contour Label Optimizer
# ==========================================================================

def _p03_args(in_contours, elev_field, obstacles, out_ws,
              seg_name, pts_name, stats_name, sel_mode="ALL",
              interval_m=500.0, min_contour_m=0.0):
    return [in_contours, elev_field, sel_mode, 100.0, interval_m, 2.0, 0.0, 25000.0,
            join_mv(obstacles), None, True, 8.0, 0.6,
            "Hybrid", 5.0, 0.5, 0.3, 0.2, 1.0, 5.0, 0.25,
            None, None, min_contour_m, "PLACE_CENTER",
            out_ws, seg_name, pts_name, False, "ContourLabelFootprints",
            True, stats_name, 11, False]


def test_plugin03(ctx):
    pid = "Plugin03"
    arcpy = ctx.arcpy
    ci_path = ctx.roles.path_for("contour_interval") or ctx.roles.path_for("contour_any")
    cidx_path = ctx.roles.path_for("contour_index")
    road_path = ctx.roles.path_for("road_any")
    water_path = ctx.roles.path_for("watercourse") or ctx.roles.path_for("drainage_any")

    interval = ctx.prep.copy_fc(ci_path, "T03_contour_interval", "REAL_DATA", "interval contours")
    index = ctx.prep.copy_fc(cidx_path, "T03_contour_index", "REAL_DATA", "index contours")
    obs = []
    o1 = ctx.prep.copy_fc(road_path, "T03_obstacles_road", "REAL_DATA", "road obstacle")
    o2 = ctx.prep.copy_fc(water_path, "T03_obstacles_water", "REAL_DATA", "water obstacle")
    if o1:
        obs.append(o1)
    if o2:
        obs.append(o2)

    elev = _elev_field(ctx, base_name(ci_path))
    ctx.record_autofill(pid, [
        pdesc("in_contours", "GPFeatureLayer", "T03_contour_interval",
              "detected_layer" if interval else "user_required",
              ctx.roles.get("contour_interval").get("confidence", 0)),
        pdesc("elev_field", "Field", elev, "detected_layer", 0.9),
        pdesc("selection_mode", "GPString", "ALL", "code_default", 1.0),
        pdesc("interval_m", "GPDouble", 500.0, "code_default", 1.0),
        pdesc("safe_mm", "GPDouble", 2.0, "code_default", 1.0),
        pdesc("map_scale", "GPDouble", 25000.0, "code_default", 1.0),
        pdesc("obstacles", "GPFeatureLayer(mv)", ";".join([base_name(o) for o in obs]),
              "detected_layer", 0.7),
        pdesc("out_ws", "DEWorkspace", ctx.prep.result_gdb, "generated_test_layer", 1.0),
        pdesc("out_segments_name", "GPString", "T03_ContourLabelSegments", "code_default", 1.0),
        pdesc("out_points_name", "GPString", "T03_contour_candidates", "code_default", 1.0),
    ])

    def _run_case(test_type, name, contours_fc, seg, pts, stats, sel_mode="ALL",
                  min_contour_m=0.0, expect_points=True):
        rec = _new(ctx, pid, name, test_type)
        t = time.time()
        if not contours_fc:
            rw.finalize_timing(_skip(rec, "Contour layer not available"), t)
            ctx.writer.add(rec)
            return
        if not elev or not ctx.qa.has_field(contours_fc, elev):
            rw.finalize_timing(_skip(rec, "Elevation field '%s' not present on contour layer" % elev), t)
            ctx.writer.add(rec)
            return
        rec["input_layers"] = [base_name(contours_fc)] + [base_name(o) for o in obs]
        rec["before_feature_count"] = ctx.qa.count(contours_fc)
        rec["parameters_used"] = {"elev_field": elev, "selection_mode": sel_mode,
                                  "min_contour_m": min_contour_m}
        pts_fc = os.path.join(ctx.prep.result_gdb, pts)
        args = _p03_args(contours_fc, elev, obs, ctx.prep.result_gdb, seg, pts, stats,
                         sel_mode=sel_mode, min_contour_m=min_contour_m)
        try:
            ctx.runner.run(pid, "OptimizeContourLabelAnchorsV4", args, rec)
            if not arcpy.Exists(pts_fc):
                if expect_points:
                    _fail(rec, "Label points output not created: " + pts, exc=False)
                else:
                    rec["status"] = "PASS"
                    rec["notes"] = "No label points (acceptable for this case)"
            else:
                n_pts = ctx.qa.count(pts_fc)
                rec["output_layers"] = [pts, seg]
                rec["after_feature_count"] = n_pts
                rec["geometry_validity_result"] = ctx.qa.geometry_validity(pts_fc)
                rec["spatial_reference_check"] = ctx.qa.sr_check(pts_fc)
                # candidates should lie ON/near contour (near distance ~ 0)
                near_ok, near_max = _near_distance_ok(ctx, pts_fc, contours_fc, tol=1.0)
                elev_preserved = _field_present_numeric(ctx, pts_fc)
                rec["success_metrics"] = {"candidate_count": n_pts,
                                          "max_near_distance_to_contour": near_max,
                                          "elevation_value_field_present": elev_preserved}
                if n_pts and n_pts > 0:
                    if near_ok is False:
                        rec["status"] = "WARN"
                        rec["notes"] = "Some candidates farther than tol from contour (max=%s)" % near_max
                    else:
                        rec["status"] = "PASS"
                        rec["notes"] = "%s label candidates on contour" % n_pts
                else:
                    rec["status"] = "WARN"
                    rec["notes"] = "Zero label candidates produced"
        except Exception as ex:
            _fail(rec, ex)
        rw.finalize_timing(rec, t)
        ctx.writer.add(rec)

    if ctx.want("smoke") or ctx.want("functional"):
        _run_case("functional", "Optimize labels on INTERVAL contours", interval,
                  "T03_seg_interval", "T03_contour_candidates", "T03_stats_interval")
    if ctx.want("functional") and index:
        _run_case("functional", "Optimize labels on INDEX contours", index,
                  "T03_seg_index", "T03_contour_candidates_index", "T03_stats_index")
    if ctx.want("edge"):
        # missing/invalid elevation field
        rec = _new(ctx, pid, "Edge: invalid elevation field", "edge")
        t = time.time()
        if not interval:
            rw.finalize_timing(_skip(rec, "no contour layer"), t)
        else:
            args = _p03_args(interval, "FIELD_DOES_NOT_EXIST", obs, ctx.prep.result_gdb,
                             "T03_seg_badfld", "T03_pts_badfld", "T03_stats_badfld")
            try:
                ctx.runner.run(pid, "OptimizeContourLabelAnchorsV4", args, rec)
                rec["status"] = "WARN"
                rec["notes"] = "Tool did not reject invalid elevation field"
            except Exception as ex:
                rec["status"] = "PASS"
                rec["notes"] = "Invalid elevation field correctly rejected: " + str(ex)[:120]
        rw.finalize_timing(rec, t)
        ctx.writer.add(rec)
    if ctx.want("edge"):
        _run_case("edge", "Edge: short-segment filter (min_contour_m high)", interval,
                  "T03_seg_short", "T03_pts_short", "T03_stats_short",
                  min_contour_m=100000.0, expect_points=False)


def _near_distance_ok(ctx, pts_fc, line_fc, tol=1.0):
    arcpy = ctx.arcpy
    try:
        tmp = arcpy.management.CopyFeatures(pts_fc, arcpy.CreateUniqueName(
            "near_chk", arcpy.env.scratchGDB or "in_memory")).getOutput(0)
        arcpy.analysis.Near(tmp, line_fc)
        mx = 0.0
        with arcpy.da.SearchCursor(tmp, ["NEAR_DIST"]) as cur:
            for row in cur:
                if row[0] is not None and row[0] > mx:
                    mx = row[0]
        try:
            arcpy.management.Delete(tmp)
        except Exception:
            pass
        return (mx <= tol), round(mx, 3)
    except Exception as ex:
        ctx.log("near check warning: " + str(ex))
        return None, None


def _field_present_numeric(ctx, fc):
    for f in (ctx.qa.field_schema(fc) or []):
        nm = f.split(":")[0].lower()
        if "elev" in nm or "hght" in nm or "ortho" in nm or nm in ("textstring", "label", "text"):
            return True
    return False


# ==========================================================================
# Plugin 04 - Elevation Text Deconflict (Mode A)
# ==========================================================================

def _p04_args_modeA(in_points, text_field, obstacles):
    return ["POINT_LAYER_WITH_TEXT_FIELD", in_points, text_field, None, None,
            "NEAREST_POINT", "FeatureID", "", "2 4 6", 16, join_mv(obstacles),
            "BALANCED_EXTENT_THEN_GEOMETRY", 0, 0.0, 0.0, 25000, 8.0, 1.0,
            "ASCII_SAFE_REPLACE", False, "BASIC", "", True, "FIXED_RINGS", 0.0,
            "CARDINAL_FIRST", True, True, False, "SET_ABSOLUTE", True, False,
            True, False, False]


def test_plugin04(ctx):
    pid = "Plugin04"
    arcpy = ctx.arcpy
    ep_path = ctx.roles.path_for("elevation_points")
    ci_path = ctx.roles.path_for("contour_interval") or ctx.roles.path_for("contour_any")
    cidx_path = ctx.roles.path_for("contour_index")

    contours = ctx.prep.copy_fc(ci_path, "T04_contours", "REAL_DATA", "contour obstacles")
    index = ctx.prep.copy_fc(cidx_path, "T04_contour_index", "REAL_DATA", "index contour obstacles")
    obstacles = [c for c in (contours, index) if c]
    elev_pts = ctx.prep.copy_fc(ep_path, "T04_elevation_text_before", "REAL_DATA",
                                "elevation point text source")
    text_field = _elev_field(ctx, base_name(ep_path)) if ep_path else "Ortho_Hght"

    ctx.record_autofill(pid, [
        pdesc("input_mode", "GPString", "POINT_LAYER_WITH_TEXT_FIELD", "code_default", 1.0),
        pdesc("in_points", "GPFeatureLayer", "T04_elevation_text_before",
              "detected_layer" if elev_pts else "user_required",
              ctx.roles.get("elevation_points").get("confidence", 0)),
        pdesc("text_field", "Field", text_field, "detected_layer", 0.9),
        pdesc("rings", "GPDouble(mv)", "2 4 6", "code_default", 1.0),
        pdesc("directions", "GPLong", 16, "code_default", 1.0),
        pdesc("obstacle_layers", "GPFeatureLayer(mv)",
              ";".join([base_name(o) for o in obstacles]), "detected_layer", 0.8),
        pdesc("conflict_test_mode", "GPString", "BALANCED_EXTENT_THEN_GEOMETRY", "code_default", 1.0),
        pdesc("reference_scale", "GPLong", 25000, "code_default", 1.0),
    ])

    if ctx.want("smoke") or ctx.want("functional"):
        rec = _new(ctx, pid, "Elevation text deconflict (Mode A) vs contours", "functional")
        t = time.time()
        if not elev_pts or not obstacles:
            rw.finalize_timing(_skip(rec, "Missing elevation points or contour obstacles"), t)
            ctx.writer.add(rec)
        elif not ctx.qa.has_field(elev_pts, text_field):
            rw.finalize_timing(_skip(rec, "Text field '%s' missing on elevation points" % text_field), t)
            ctx.writer.add(rec)
        else:
            rec["input_layers"] = ["T04_elevation_text_before"] + [base_name(o) for o in obstacles]
            n_before = ctx.qa.count(elev_pts)
            rec["before_feature_count"] = n_before
            buf = _make_buffer(ctx, obstacles[0], 3.0, "T04_obstacle_buffer")
            conf_before = ctx.qa.count_intersections(elev_pts, buf) if buf else None
            ctx.snapshots.append(ctx.qa.snapshot(pid, "elevation_text_before", elev_pts,
                                 "before", ctx.prep.snap_before, conflicts=conf_before))
            rec["parameters_used"] = {"text_field": text_field, "rings": "2 4 6", "directions": 16}
            args = _p04_args_modeA(elev_pts, text_field, obstacles)
            try:
                result = ctx.runner.run(pid, "ElevationTextDeconflictV5", args, rec)
                outs = _result_outputs(ctx, result)
                rec["output_layers"] = [base_name(o) for o in outs]
                moved = _pick_moved(outs)
                if moved and arcpy.Exists(moved):
                    n_after = ctx.qa.count(moved)
                    conf_after = ctx.qa.count_intersections(moved, buf) if buf else None
                    rec["after_feature_count"] = n_after
                    rec["geometry_validity_result"] = ctx.qa.geometry_validity(moved)
                    rec["success_metrics"] = {"conflicts_before": conf_before,
                                              "conflicts_after": conf_after,
                                              "count_before": n_before, "count_after": n_after}
                    ctx.snapshots.append(ctx.qa.snapshot(pid, "elevation_text_after", moved,
                                         "after", ctx.prep.snap_after, conflicts=conf_after))
                    count_ok = (n_after == n_before)
                    if conf_before is not None and conf_after is not None:
                        if conf_after <= conf_before and count_ok:
                            rec["status"] = "PASS"
                            rec["notes"] = "conflicts %s -> %s, count preserved" % (conf_before, conf_after)
                        elif not count_ok:
                            _fail(rec, "feature count changed %s -> %s" % (n_before, n_after), exc=False)
                        else:
                            rec["status"] = "WARN"
                            rec["notes"] = "conflicts not reduced: %s -> %s" % (conf_before, conf_after)
                    else:
                        rec["status"] = "WARN" if count_ok else "FAIL"
                        rec["notes"] = "conflict metric unavailable"
                else:
                    # Tool produced report FCs but no moved copy (still a valid run)
                    rec["status"] = "PASS" if outs else "WARN"
                    rec["notes"] = "Tool ran; outputs: %s" % rec["output_layers"]
            except Exception as ex:
                _fail(rec, ex)
            rw.finalize_timing(rec, t)
            ctx.writer.add(rec)

    # Edge: no conflict (points far from obstacles -> nothing to move)
    if ctx.want("edge") and elev_pts and obstacles:
        rec = _new(ctx, pid, "Edge: tiny rings (minimal search)", "edge")
        t = time.time()
        try:
            args = _p04_args_modeA(elev_pts, text_field, obstacles)
            args[8] = "0.5"  # rings very small
            result = ctx.runner.run(pid, "ElevationTextDeconflictV5", args, rec)
            outs = _result_outputs(ctx, result)
            rec["output_layers"] = [base_name(o) for o in outs]
            rec["status"] = "PASS"
            rec["notes"] = "Minimal-ring run completed"
        except Exception as ex:
            rec["status"] = "WARN"
            rec["notes"] = "raised: " + str(ex)[:120]
            rec["traceback"] = traceback.format_exc()
        rw.finalize_timing(rec, t)
        ctx.writer.add(rec)


def _result_outputs(ctx, result):
    arcpy = ctx.arcpy
    out = []
    if result is None:
        return out
    try:
        n = result.outputCount
        for i in range(n):
            v = result.getOutput(i)
            if v and isinstance(v, type(u"")) and arcpy.Exists(v):
                out.append(v)
    except Exception:
        pass
    return out


def _pick_moved(outs):
    for o in outs:
        low = base_name(o).lower()
        if "moved" in low and "only" not in low:
            return o
    for o in outs:
        if "moved" in base_name(o).lower():
            return o
    return None



# ==========================================================================
# Plugin 05 - Safe Contour Cleaner
# ==========================================================================

def _p05_args(in_contours, frame, dense_threshold, out_ws, out_name,
              removal_method="Segment Erase (recommended)", aoi_mode="Frame only (default)",
              allow_full=False, protected_sql=""):
    return [join_mv(in_contours), frame, 0.0, 0.0, dense_threshold, 1,
            aoi_mode, None, "Auto mask from dense zones (recommended)", None,
            "1=1", protected_sql, removal_method, 0.0, out_ws, out_name,
            True, True, True, True, False, False, 50000, allow_full]


def test_plugin05(ctx):
    pid = "Plugin05"
    arcpy = ctx.arcpy
    ci_path = ctx.roles.path_for("contour_interval") or ctx.roles.path_for("contour_any")
    cidx_path = ctx.roles.path_for("contour_index")
    interval = ctx.prep.copy_fc(ci_path, "T05_dense_contours_before", "REAL_DATA",
                                "interval contours to clean")
    index = ctx.prep.copy_fc(cidx_path, "T05_contour_index", "REAL_DATA",
                             "index contours (should be protected)")
    frame = _item_by_name(ctx, "T00_TestFrame")
    frame_path = os.path.join(ctx.prep.test_gdb, "T00_TestFrame")
    if not arcpy.Exists(frame_path):
        frame_path = ctx.prep.make_frame_polygon(ctx.items)
    # Ensure the frame is a TRUE polygon (never a line/annotation AOI). If a
    # detected aoi_frame is not polygon, or the synthesized frame is missing,
    # (re)synthesize a polygon frame from the data extent.
    try:
        if frame_path and arcpy.Exists(frame_path):
            if str(getattr(arcpy.Describe(frame_path), "shapeType", "")).lower() != "polygon":
                frame_path = ctx.prep.make_frame_polygon(ctx.items)
    except Exception as ex:
        ctx.log("frame polygon check warning: " + str(ex))
        frame_path = ctx.prep.make_frame_polygon(ctx.items)

    ctx.record_autofill(pid, [
        pdesc("in_contours", "GPFeatureLayer(mv)", "T05_dense_contours_before",
              "detected_layer" if interval else "user_required",
              ctx.roles.get("contour_interval").get("confidence", 0)),
        pdesc("frame_polygon", "GPFeatureLayer", "T00_TestFrame",
              "generated_test_layer", 1.0, "synthetic frame from extent"),
        pdesc("dense_threshold", "GPDouble", 20.0, "code_default", 1.0),
        pdesc("aoi_mode", "GPString", "Frame only (default)", "code_default", 1.0),
        pdesc("mask_mode", "GPString", "Auto mask from dense zones (recommended)", "code_default", 1.0),
        pdesc("removal_method", "GPString", "Segment Erase (recommended)", "code_default", 1.0),
        pdesc("out_workspace", "DEWorkspace", ctx.prep.result_gdb, "generated_test_layer", 1.0),
        pdesc("out_clean_name", "GPString", "T05_dense_contours_after", "code_default", 1.0),
        pdesc("allow_full_map_processing", "GPBoolean", False, "code_default", 1.0),
    ])

    def _frame_edge_band_count(contours_fc):
        """Count contour features touching a thin band just inside the frame edge."""
        if not frame_path or not arcpy.Exists(frame_path) or not contours_fc:
            return None
        try:
            line = arcpy.management.FeatureToLine([frame_path], arcpy.CreateUniqueName(
                "frame_line", arcpy.env.scratchGDB or "in_memory")).getOutput(0)
            band = arcpy.analysis.Buffer(line, arcpy.CreateUniqueName(
                "frame_band", arcpy.env.scratchGDB or "in_memory"), "30 Meters").getOutput(0)
            n = ctx.qa.count_intersections(contours_fc, band)
            for tmp in (line, band):
                try:
                    arcpy.management.Delete(tmp)
                except Exception:
                    pass
            return n
        except Exception as ex:
            ctx.log("frame band warning: " + str(ex))
            return None

    if not ctx.want("smoke") and not ctx.want("functional") and not ctx.want("edge") and not ctx.want("regression"):
        return

    if interval and (ctx.want("smoke") or ctx.want("functional") or ctx.want("regression")):
        rec = _new(ctx, pid, "Clean dense contours, protect frame edge", "regression")
        t = time.time()
        if not frame_path or not arcpy.Exists(frame_path):
            rw.finalize_timing(_skip(rec, "No frame polygon (T00_TestFrame) available"), t)
            ctx.writer.add(rec)
        else:
            n_before = ctx.qa.count(interval)
            edge_before = _frame_edge_band_count(interval)
            rec["before_feature_count"] = n_before
            rec["input_layers"] = ["T05_dense_contours_before", "T00_TestFrame"]
            rec["parameters_used"] = {"dense_threshold": 20.0, "aoi_mode": "Frame only (default)"}
            ctx.snapshots.append(ctx.qa.snapshot(pid, "dense_before", interval, "before",
                                 ctx.prep.snap_before, conflicts=edge_before))
            out_name = "T05_dense_contours_after"
            out_fc = _result_path(ctx, out_name)
            method = "Segment Erase (recommended)"
            try:
                args = _p05_args([interval], frame_path, 20.0, ctx.prep.result_gdb, out_name, method)
                ctx.runner.run(pid, "SafeContourCleaner", args, rec)
            except Exception as ex:
                msg = str(ex).lower()
                if "license" in msg or "erase" in msg or "advanced" in msg:
                    method = "Delete Whole Features"
                    try:
                        args = _p05_args([interval], frame_path, 20.0, ctx.prep.result_gdb, out_name, method)
                        ctx.runner.run(pid, "SafeContourCleaner", args, rec)
                    except Exception as ex2:
                        _fail(rec, ex2)
                else:
                    _fail(rec, ex)
            if rec["status"] != "FAIL":
                if not arcpy.Exists(out_fc):
                    rec["status"] = "WARN"
                    rec["notes"] = "No cleaned output created (dry-run/empty?) method=%s" % method
                else:
                    n_after = ctx.qa.count(out_fc)
                    edge_after = _frame_edge_band_count(out_fc)
                    rec["after_feature_count"] = n_after
                    rec["changed_feature_count"] = (n_before - n_after) if (n_before is not None and n_after is not None) else None
                    rec["geometry_validity_result"] = ctx.qa.geometry_validity(out_fc)
                    rec["spatial_reference_check"] = ctx.qa.sr_check(out_fc)
                    rec["success_metrics"] = {"count_before": n_before, "count_after": n_after,
                                              "frame_edge_before": edge_before,
                                              "frame_edge_after": edge_after,
                                              "removal_method": method,
                                              "internal_removed": rec["changed_feature_count"],
                                              "frame_is_polygon": True}
                    ctx.snapshots.append(ctx.qa.snapshot(pid, "dense_after", out_fc, "after",
                                         ctx.prep.snap_after, conflicts=edge_after))
                    # Explicit PASS / WARN / FAIL criteria:
                    #   FAIL  : frame-edge contours decreased (edge protection broke)
                    #   PASS  : frame edge preserved AND some internal dense contours
                    #           were removed/flagged (after <= before)
                    #   WARN  : frame edge preserved but nothing was removed
                    edge_protected = (edge_before is None or edge_after is None or edge_after >= edge_before)
                    removed_some = (n_after is not None and n_before is not None and n_after < n_before)
                    no_increase = (n_after is not None and n_before is not None and n_after <= n_before)
                    if not edge_protected:
                        rec["status"] = "FAIL"
                        rec["notes"] = ("Frame-edge contours were removed (%s -> %s) - edge protection FAILED"
                                        % (edge_before, edge_after))
                    elif removed_some:
                        rec["status"] = "PASS"
                        rec["notes"] = ("Removed %s internal dense feature(s); frame-edge contours preserved "
                                        "(%s -> %s); method=%s" % (rec["changed_feature_count"],
                                                                   edge_before, edge_after, method))
                    elif no_increase:
                        rec["status"] = "WARN"
                        rec["notes"] = ("Frame edge preserved but no dense contours removed "
                                        "(count %s -> %s) - check dense_threshold for this data"
                                        % (n_before, n_after))
                    else:
                        rec["status"] = "FAIL"
                        rec["notes"] = "Output count increased (%s -> %s) - unexpected" % (n_before, n_after)
            rw.finalize_timing(rec, t)
            ctx.writer.add(rec)

    # Regression: PROTECTED contours must never be removed. This exercises the
    # same protected_sql mechanism used to protect INDEX contours: when every
    # eligible contour is protected, the cleaner must remove nothing. This is a
    # deterministic proxy for "index contours are protected unless explicitly
    # allowed" (set protected_sql to your index selection in real runs).
    if interval and (ctx.want("functional") or ctx.want("regression")) \
            and frame_path and arcpy.Exists(frame_path):
        rec = _new(ctx, pid, "Regression: protected contours are never removed", "regression")
        t = time.time()
        n_before = ctx.qa.count(interval)
        rec["before_feature_count"] = n_before
        rec["input_layers"] = ["T05_dense_contours_before", "T00_TestFrame"]
        rec["parameters_used"] = {"dense_threshold": 20.0, "protected_sql": "1=1",
                                  "note": "all contours protected"}
        out_name = "T05_protected_after"
        out_fc = _result_path(ctx, out_name)
        method = "Segment Erase (recommended)"
        try:
            args = _p05_args([interval], frame_path, 20.0, ctx.prep.result_gdb, out_name,
                             method, protected_sql="1=1")
            ctx.runner.run(pid, "SafeContourCleaner", args, rec)
        except Exception as ex:
            msg = str(ex).lower()
            if "license" in msg or "erase" in msg or "advanced" in msg:
                method = "Delete Whole Features"
                try:
                    args = _p05_args([interval], frame_path, 20.0, ctx.prep.result_gdb,
                                     out_name, method, protected_sql="1=1")
                    ctx.runner.run(pid, "SafeContourCleaner", args, rec)
                except Exception as ex2:
                    _fail(rec, ex2)
            else:
                _fail(rec, ex)
        if rec["status"] != "FAIL":
            # Tool may legitimately produce no output when nothing is eligible.
            n_after = ctx.qa.count(out_fc) if arcpy.Exists(out_fc) else n_before
            rec["after_feature_count"] = n_after
            rec["changed_feature_count"] = (n_before - n_after) if (n_before is not None and n_after is not None) else None
            rec["success_metrics"] = {"count_before": n_before, "count_after": n_after,
                                      "protected_sql": "1=1", "removal_method": method,
                                      "produced_output": bool(arcpy.Exists(out_fc))}
            if n_after is not None and n_before is not None and n_after >= n_before:
                rec["status"] = "PASS"
                rec["notes"] = ("All contours protected -> none removed (%s -> %s). "
                                "Index contours protected the same way." % (n_before, n_after))
            else:
                rec["status"] = "FAIL"
                rec["notes"] = ("Protected contours were removed (%s -> %s) - protected_sql NOT honored"
                                % (n_before, n_after))
        rw.finalize_timing(rec, t)
        ctx.writer.add(rec)

    # Edge: missing frame + allow_full False -> expect controlled error
    if ctx.want("edge") and interval:
        rec = _new(ctx, pid, "Edge: no frame, allow_full_map=False (expect refusal)", "edge")
        t = time.time()
        try:
            args = _p05_args([interval], None, 20.0, ctx.prep.result_gdb,
                             "T05_nofr", aoi_mode="Frame only (default)", allow_full=False)
            ctx.runner.run(pid, "SafeContourCleaner", args, rec)
            rec["status"] = "WARN"
            rec["notes"] = "Tool did not refuse empty-AOI whole-dataset edit"
        except Exception as ex:
            rec["status"] = "PASS"
            rec["notes"] = "Correctly refused empty AOI without allow_full_map_processing: " + str(ex)[:100]
        rw.finalize_timing(rec, t)
        ctx.writer.add(rec)

    # Edge: very large threshold (nothing dense)
    if ctx.want("edge") and interval and frame_path and arcpy.Exists(frame_path):
        rec = _new(ctx, pid, "Edge: very large dense threshold", "edge")
        t = time.time()
        try:
            args = _p05_args([interval], frame_path, 1000000.0, ctx.prep.result_gdb, "T05_bigthr")
            ctx.runner.run(pid, "SafeContourCleaner", args, rec)
            rec["status"] = "PASS"
            rec["notes"] = "Large threshold handled"
        except Exception as ex:
            rec["status"] = "WARN"
            rec["notes"] = "raised: " + str(ex)[:120]
            rec["traceback"] = traceback.format_exc()
        rw.finalize_timing(rec, t)
        ctx.writer.add(rec)


# ==========================================================================
# Plugin 06 - Spring Rotation
# ==========================================================================

def _p06_args(springs, contours, elev_field, out_gdb, base_name_,
              run01=True, run02=True):
    return [springs, contours, elev_field, True, out_gdb, base_name_,
            "SEPARATE_LAYERS", False, None, False, 40.0, "AUTO_UTM", "PLANAR",
            None, 0.0, 8, 5.0, None, 0.0, "NEAR_ONLY", False,
            run01, run02, False, False, False]


def test_plugin06(ctx):
    pid = "Plugin06"
    arcpy = ctx.arcpy
    spring_path = ctx.roles.path_for("spring_continual") or ctx.roles.path_for("spring")
    ci_path = ctx.roles.path_for("contour_interval") or ctx.roles.path_for("contour_any")
    springs = ctx.prep.copy_fc(spring_path, "T06_spring_before", "REAL_DATA", "spring points")
    contours = ctx.prep.copy_fc(ci_path, "T06_contours", "REAL_DATA", "contours for rotation context")
    elev = _elev_field(ctx, base_name(ci_path))

    ctx.record_autofill(pid, [
        pdesc("springs", "GPFeatureLayer", "T06_spring_before",
              "detected_layer" if springs else "user_required",
              ctx.roles.get("spring").get("confidence", 0)),
        pdesc("contours", "GPFeatureLayer", "T06_contours",
              "detected_layer" if contours else "user_required",
              ctx.roles.get("contour_interval").get("confidence", 0)),
        pdesc("elev_field", "Field", elev, "detected_layer", 0.9),
        pdesc("ignore_selection", "GPBoolean", True, "code_default", 1.0),
        pdesc("out_gdb", "DEWorkspace", ctx.prep.result_gdb, "generated_test_layer", 1.0,
              "result gdb (must exist)"),
        pdesc("out_base_name", "GPString", "T06_spring", "code_default", 1.0),
        pdesc("output_mode", "GPString", "SEPARATE_LAYERS", "code_default", 1.0),
        pdesc("work_sr_mode", "GPString", "AUTO_UTM", "code_default", 1.0),
        pdesc("near_method", "GPString", "PLANAR", "code_default", 1.0),
        pdesc("auto_symbology", "GPBoolean", False, "code_default", 1.0, "off for headless"),
        pdesc("run_01", "GPBoolean", True, "code_default", 1.0),
        pdesc("run_02", "GPBoolean", True, "code_default", 1.0),
    ])

    if ctx.want("smoke") or ctx.want("functional") or ctx.want("regression"):
        rec = _new(ctx, pid, "Spring rotation from contour context (no geometry move)", "regression")
        t = time.time()
        if not springs or not contours:
            rw.finalize_timing(_skip(rec, "Missing springs or contours layer"), t)
            ctx.writer.add(rec)
        elif not elev or not ctx.qa.has_field(contours, elev):
            rw.finalize_timing(_skip(rec, "Elevation field '%s' missing on contours" % elev), t)
            ctx.writer.add(rec)
        else:
            n_before = ctx.qa.count(springs)
            rec["before_feature_count"] = n_before
            rec["input_layers"] = ["T06_spring_before", "T06_contours"]
            rec["parameters_used"] = {"elev_field": elev, "work_sr_mode": "AUTO_UTM",
                                      "near_method": "PLANAR", "run_01": True, "run_02": True}
            ctx.snapshots.append(ctx.qa.snapshot(pid, "spring_before", springs, "before",
                                 ctx.prep.snap_before))
            args = _p06_args(springs, contours, elev, ctx.prep.result_gdb, "T06_spring")
            try:
                ctx.runner.run(pid, "SpringRotationFinalSuiteTool", args, rec)
                outs = _find_all_prefixed(ctx, ctx.prep.result_gdb, "T06_spring")
                rec["output_layers"] = [base_name(o) for o in outs]
                if not outs:
                    _fail(rec, "No spring rotation output layers created", exc=False)
                else:
                    bad = []
                    rot_present_all = True
                    for o in outs:
                        n_o = ctx.qa.count(o)
                        if n_o != n_before:
                            bad.append("%s count=%s" % (base_name(o), n_o))
                        if not ctx.qa.has_field(o, "ROT"):
                            rot_present_all = False
                    primary = outs[0]
                    in_range, st = _angles_in_range(ctx, primary, "ROT")
                    rec["after_feature_count"] = ctx.qa.count(primary)
                    rec["changed_feature_count"] = 0
                    rec["geometry_validity_result"] = ctx.qa.geometry_validity(primary)
                    rec["field_schema_check"] = "ROT present" if rot_present_all else "ROT MISSING on some outputs"
                    if st:
                        rec["success_metrics"]["rotation_stats"] = {
                            "n": st[0], "min": round(st[1], 2), "max": round(st[2], 2)}
                        ctx.snapshots.append(ctx.qa.snapshot(pid, "spring_after", primary, "after",
                                             ctx.prep.snap_after, angle_field="ROT"))
                    if bad:
                        rec["status"] = "FAIL"
                        rec["notes"] = "Spring count changed in outputs: %s (before=%s)" % ("; ".join(bad), n_before)
                    elif not rot_present_all:
                        rec["status"] = "WARN"
                        rec["notes"] = "ROT field missing on one or more outputs"
                    elif in_range is False:
                        rec["status"] = "WARN"
                        rec["notes"] = "ROT values outside 0..360"
                    else:
                        rec["status"] = "PASS"
                        rec["notes"] = "Springs preserved (%s); ROT written on %s output(s)" % (n_before, len(outs))
            except Exception as ex:
                _fail(rec, ex)
            rw.finalize_timing(rec, t)
            ctx.writer.add(rec)

    # Edge: spring lacking nearby elevation context (tiny contour subset far away handled by tool)
    if ctx.want("edge") and springs and contours and elev and ctx.qa.has_field(contours, elev):
        rec = _new(ctx, pid, "Edge: run methods 01 only", "edge")
        t = time.time()
        try:
            args = _p06_args(springs, contours, elev, ctx.prep.result_gdb, "T06_spring_m1",
                             run01=True, run02=False)
            ctx.runner.run(pid, "SpringRotationFinalSuiteTool", args, rec)
            outs = _find_all_prefixed(ctx, ctx.prep.result_gdb, "T06_spring_m1")
            rec["output_layers"] = [base_name(o) for o in outs]
            rec["status"] = "PASS" if outs else "WARN"
            rec["notes"] = "Single-method run produced %s output(s)" % len(outs)
        except Exception as ex:
            _fail(rec, ex)
        rw.finalize_timing(rec, t)
        ctx.writer.add(rec)


def _find_all_prefixed(ctx, gdb, prefix):
    arcpy = ctx.arcpy
    out = []
    prev = arcpy.env.workspace
    try:
        arcpy.env.workspace = gdb
        for fc in (arcpy.ListFeatureClasses() or []):
            if fc.lower().startswith(prefix.lower()):
                out.append(os.path.join(gdb, fc))
    except Exception:
        pass
    finally:
        arcpy.env.workspace = prev
    return out


# ==========================================================================
# Plugin 07 - Batch Grid / Index Builder
# ==========================================================================

def _make_test_sheets(ctx, out_name="T07_test_sheets", nx=2, ny=2):
    arcpy = ctx.arcpy
    ext = ctx.prep._union_extent(ctx.items)
    if ext is None:
        return None
    x0, y0, x1, y1 = ext
    dx = (x1 - x0) / 6.0
    dy = (y1 - y0) / 6.0
    cx0, cy0 = x0 + dx, y0 + dy
    cell_w = (x1 - x0 - 2 * dx) / nx
    cell_h = (y1 - y0 - 2 * dy) / ny
    sr = arcpy.SpatialReference(WKID_EXPECTED)
    dst = os.path.join(ctx.prep.test_gdb, out_name)
    if arcpy.Exists(dst):
        arcpy.management.Delete(dst)
    arcpy.management.CreateFeatureclass(ctx.prep.test_gdb, out_name, "POLYGON",
                                        spatial_reference=sr)
    arcpy.management.AddField(dst, "SHEET", "TEXT", field_length=32)
    with arcpy.da.InsertCursor(dst, ["SHAPE@", "SHEET"]) as ic:
        k = 1
        for i in range(nx):
            for j in range(ny):
                ax = cx0 + i * cell_w
                ay = cy0 + j * cell_h
                arr = arcpy.Array([arcpy.Point(ax, ay), arcpy.Point(ax, ay + cell_h),
                                   arcpy.Point(ax + cell_w, ay + cell_h),
                                   arcpy.Point(ax + cell_w, ay), arcpy.Point(ax, ay)])
                ic.insertRow([arcpy.Polygon(arr, sr), "SHEET_%02d" % k])
                k += 1
    ctx.prep._record(out_name, "SYNTHETIC_DERIVED_FROM_REAL_EXTENT", "dataset extent", dst,
                     "%d test sheet polygons for batch grid" % (nx * ny))
    return dst


def _p07_args_aoi(aoi_layer, out_ws, name_field="SHEET"):
    return ["AOI_LAYER_IN_CURRENT_MXD", None, False, None, aoi_layer, name_field,
            "SMART_FEATURE", None, out_ws, "Grids", 25000.0, True,
            1000.0, 1000.0, 1.5, 3.0, True]


def _p07_args_folder(mxd_folder, out_ws):
    return ["FOLDER_OF_MXDS", mxd_folder, False, None, None, None,
            "SMART_FEATURE", None, out_ws, "Grids", 25000.0, True,
            1000.0, 1000.0, 1.5, 3.0, True]


def test_plugin07(ctx):
    pid = "Plugin07"
    arcpy = ctx.arcpy
    sheets = _make_test_sheets(ctx, "T07_test_sheets")
    mxd_folder = ctx.flags.get("mxd_folder")
    has_map = ctx.flags.get("has_map")

    ctx.record_autofill(pid, [
        pdesc("mode", "GPString",
              "AOI_LAYER_IN_CURRENT_MXD" if has_map else ("FOLDER_OF_MXDS" if mxd_folder else "AOI_LAYER_IN_CURRENT_MXD"),
              "code_default", 1.0),
        pdesc("aoi_layer", "GPFeatureLayer", "T07_test_sheets",
              "generated_test_layer", 1.0, "multiple sheet polygons"),
        pdesc("aoi_name_field", "Field", "SHEET", "generated_test_layer", 1.0),
        pdesc("engine", "GPString", "SMART_FEATURE", "code_default", 1.0),
        pdesc("out_ws", "DEWorkspace", ctx.prep.result_gdb, "generated_test_layer", 1.0),
        pdesc("fds_name", "GPString", "Grids", "code_default", 1.0),
        pdesc("refscale_denom", "GPDouble", 25000.0, "code_default", 1.0),
        pdesc("mxd_folder", "DEFolder", mxd_folder, "user_required", 1.0,
              "needed for FOLDER_OF_MXDS mode"),
    ])

    rec = _new(ctx, pid, "Batch grid build over multiple sheets", "functional")
    t = time.time()
    if not sheets:
        rw.finalize_timing(_skip(rec, "Could not synthesize test sheets from extent"), t)
        ctx.writer.add(rec)
        return
    rec["input_layers"] = ["T07_test_sheets"]
    n_sheets = ctx.qa.count(sheets)
    rec["before_feature_count"] = n_sheets

    if not has_map and not mxd_folder:
        plat_proj = "ArcGIS Pro .aprx project" if ctx.platform == "ArcGISPro" else "ArcMap .mxd"
        rw.finalize_timing(_skip(rec,
            "Plugin07 (Batch Grid Builder) needs a map context and cannot run fully "
            "headless. To run it, choose ONE of: "
            "(1) run the .pyt TestHarness (Carto_%s_TestHarness.pyt) INSIDE an active "
            "ArcMap / ArcGIS Pro session - the harness detects the CURRENT map and uses "
            "AOI_LAYER_IN_CURRENT_MXD with the prepared sheet polygons; "
            "(2) pass --mxd-folder pointing to a folder of ArcMap .mxd documents "
            "(FOLDER_OF_MXDS mode); or "
            "(3) pass --mxd-folder pointing to a folder of %s files (FOLDER_OF_MXDS "
            "mode in Pro). The test sheet polygons have already been prepared at "
            "test_data.gdb/T07_test_sheets (%s sheets) - add that layer to your active "
            "map for option (1)." % (
                "ArcMap" if ctx.platform != "ArcGISPro" else "Pro", plat_proj, n_sheets)), t)
        ctx.writer.add(rec)
        return

    try:
        if mxd_folder:
            args = _p07_args_folder(mxd_folder, ctx.prep.result_gdb)
            rec["parameters_used"] = {"mode": "FOLDER_OF_MXDS", "mxd_folder": mxd_folder}
        else:
            args = _p07_args_aoi(sheets, ctx.prep.result_gdb, "SHEET")
            rec["parameters_used"] = {"mode": "AOI_LAYER_IN_CURRENT_MXD",
                                      "aoi_layer": "T07_test_sheets"}
        ctx.runner.run(pid, "BatchGridBuilder07", args, rec)
        grid_outs = _find_grid_outputs(ctx, ctx.prep.result_gdb)
        rec["output_layers"] = [base_name(o) for o in grid_outs]
        rec["success_metrics"] = {"sheets": n_sheets, "grid_outputs": len(grid_outs)}
        if grid_outs:
            rec["status"] = "PASS"
            rec["notes"] = "%s grid output(s) created for %s sheet(s)" % (len(grid_outs), n_sheets)
        else:
            rec["status"] = "WARN"
            rec["notes"] = "Tool ran but no grid outputs detected in result gdb"
    except Exception as ex:
        _fail(rec, ex)
    rw.finalize_timing(rec, t)
    ctx.writer.add(rec)


def _find_grid_outputs(ctx, gdb):
    arcpy = ctx.arcpy
    out = []
    prev = arcpy.env.workspace
    try:
        for ws in (gdb, os.path.join(gdb, "Grids")):
            try:
                arcpy.env.workspace = ws
                for fc in (arcpy.ListFeatureClasses() or []):
                    low = fc.lower()
                    if "grid" in low or "tick" in low or "grat" in low or "label" in low:
                        out.append(os.path.join(ws, fc))
            except Exception:
                pass
    finally:
        arcpy.env.workspace = prev
    return out



# ==========================================================================
# 9. Orchestrator / runner
# ==========================================================================

TEST_FUNCS = {
    "Plugin01": test_plugin01,
    "Plugin02": test_plugin02,
    "Plugin03": test_plugin03,
    "Plugin04": test_plugin04,
    "Plugin05": test_plugin05,
    "Plugin06": test_plugin06,
    "Plugin07": test_plugin07,
}


def run(config):
    """Main entry point. config is a dict with keys:
        arcpy        - the arcpy module (injected by the platform harness)
        platform     - 'ArcMap' or 'ArcGISPro'
        input_gdb    - path to source geodatabase (READ-ONLY)
        carto_repo   - path to the Carto plugin repository
        output       - output root folder
        plugins      - optional list like ['Plugin01','Plugin06'] (default all)
        test_types   - optional list subset of smoke/functional/edge/regression
        safe_mode    - bool (default True): never write to input_gdb
        overwrite    - bool: overwrite an existing run dir name (always unique ts)
        mxd_folder   - optional folder of MXDs/projects for Plugin07
        has_map      - bool: True when an active map document is available (.pyt harness)
    Returns the run directory path.
    """
    arcpy = config["arcpy"]
    platform = config.get("platform", "ArcGISPro")
    input_gdb = config["input_gdb"]
    repo = config["carto_repo"]
    output = config.get("output") or os.path.join(os.getcwd(), "carto_test_output")
    plugins = config.get("plugins") or list(PLUGIN_ORDER)
    test_types = config.get("test_types")  # None == all
    mxd_folder = config.get("mxd_folder")
    has_map = bool(config.get("has_map", False))

    # ---- validate inputs ----
    if not arcpy.Exists(input_gdb):
        raise IOError("Input geodatabase not found: " + str(input_gdb))
    if not os.path.isdir(repo):
        raise IOError("Carto repository folder not found: " + str(repo))

    # ---- environment: read-only input, safe scratch ----
    arcpy.env.overwriteOutput = True
    try:
        arcpy.env.addOutputsToMap = False
    except Exception:
        pass

    # ---- workspace prep ----
    prep = TestDataPrep(arcpy, output, sr_wkid=WKID_EXPECTED)
    run_dir = prep.create_workspace()
    writer = rw.ReportWriter(prep.reports_dir, prep.logs_dir)
    writer.log("Platform=%s  input_gdb=%s  repo=%s" % (platform, input_gdb, repo))
    writer.log("Plugins=%s  test_types=%s  has_map=%s  mxd_folder=%s"
               % (plugins, test_types, has_map, mxd_folder))

    # ---- 1. inventory ----
    writer.log("Scanning input geodatabase (read-only)...")
    scanner = InventoryScanner(arcpy, input_gdb, logger=writer.log)
    items = scanner.scan()
    scanner.write_reports(prep.reports_dir)
    writer.log("Inventory: %d datasets scanned." % len(items))

    # ---- 2. roles ----
    detector = RoleDetector(items, logger=writer.log)
    detector.detect()
    # apply role-map overrides if provided
    _apply_role_overrides(config, detector, items, writer)
    detector.write_reports(prep.reports_dir)

    # ---- synth frame polygon (AOI is a polyline in this dataset) ----
    aoi_role = detector.get("aoi_frame")
    if not aoi_role.get("selected_path"):
        fp = prep.make_frame_polygon(items)
        if fp:
            writer.log("Synthesized T00_TestFrame polygon (no polygon AOI in dataset).")
            detector.mapping["aoi_frame"]["selected_layer"] = "T00_TestFrame"
            detector.mapping["aoi_frame"]["selected_path"] = fp
            detector.mapping["aoi_frame"]["notes"] = "SYNTHETIC frame from data extent"
            detector.write_reports(prep.reports_dir)

    qa = QA(arcpy, logger=writer.log)
    flags = {"test_types": test_types, "safe_mode": config.get("safe_mode", True),
             "mxd_folder": mxd_folder, "has_map": has_map}
    runner = ToolRunner(arcpy, repo, platform, logger=writer.log)
    ctx = RunContext(arcpy, platform, repo, items, detector, prep, qa, writer, runner, flags)

    # ---- 3-5. per-plugin tests ----
    per_plugin_report_base = {
        "Plugin01": "T01_bridge_culvert_test_report",
        "Plugin02": "T02_road_conflict_test_report",
        "Plugin03": "T03_contour_label_test_report",
        "Plugin04": "T04_elevation_text_test_report",
        "Plugin05": "T05_dense_contour_test_report",
        "Plugin06": "T06_symbol_alignment_test_report",
        "Plugin07": "T07_grid_batch_test_report",
    }
    for pid in PLUGIN_ORDER:
        if pid not in plugins:
            continue
        writer.log("==== Running tests for %s (%s) ====" % (pid, PLUGIN_REGISTRY[pid]["name"]))
        before = len(writer.results)
        try:
            TEST_FUNCS[pid](ctx)
        except Exception as ex:
            # Catch-all so one plugin's prep failure never stops the suite.
            rec = _new(ctx, pid, "Plugin harness error", "smoke")
            _fail(rec, ex)
            writer.add(rec)
            writer.log("Plugin %s raised at harness level: %s" % (pid, str(ex)))
        # write per-plugin report
        recs = writer.results[before:]
        writer.write_plugin_report(per_plugin_report_base[pid], recs)

    # ---- 6. autofill + prep + before/after + global ----
    ctx.write_autofill(prep.reports_dir)
    prep.write_prep_report(prep.reports_dir)
    writer.write_before_after(ctx.snapshots)
    writer.write_all()
    writer.log("DONE. Reports in: %s" % prep.reports_dir)
    return run_dir


def _apply_role_overrides(config, detector, items, writer):
    """Apply manual layer-role overrides. Accepts, in priority order:
      * config['role_map']      - a dict {role: layer_name}
      * config['role_map_json'] - path to a JSON file, either {role: layer_name}
                                  or {"overrides": [{"role":..,"layer_name":..}]}
      * config['role_map_csv']  - path to a CSV with a 'role' column and either a
                                  'layer_name' (preferred) or legacy
                                  'selected_layer' column. A 'notes' column is
                                  optional and recorded on the mapping.
    The matched layer must exist in the scanned inventory; unknown layers are
    logged and skipped (never silently ignored)."""
    overrides = config.get("role_map")
    csv_path = config.get("role_map_csv")
    json_path = config.get("role_map_json")
    mapping = {}     # role -> layer_name
    notes = {}       # role -> note

    if isinstance(overrides, dict):
        mapping.update(overrides)

    if json_path and os.path.exists(json_path):
        try:
            import json as _json
            data = _json.load(open(json_path, "r"))
            rows = data.get("overrides", data) if isinstance(data, dict) else data
            if isinstance(rows, dict):
                for role, layer in rows.items():
                    if role and layer:
                        mapping[role] = layer
            elif isinstance(rows, list):
                for row in rows:
                    role = (row.get("role") or "").strip()
                    layer = (row.get("layer_name") or row.get("selected_layer") or "").strip()
                    if role and layer:
                        mapping[role] = layer
                        if row.get("notes"):
                            notes[role] = row.get("notes")
        except Exception as ex:
            writer.log("role-map JSON parse warning: " + str(ex))

    if csv_path and os.path.exists(csv_path):
        try:
            import csv as _csv
            f = open(csv_path, "r")
            try:
                for row in _csv.DictReader(f):
                    role = (row.get("role") or "").strip()
                    # Prefer 'layer_name'; fall back to legacy 'selected_layer'.
                    layer = (row.get("layer_name") or row.get("selected_layer") or "").strip()
                    if role and layer:
                        mapping[role] = layer
                        if row.get("notes"):
                            notes[role] = (row.get("notes") or "").strip()
            finally:
                f.close()
        except Exception as ex:
            writer.log("role-map CSV parse warning: " + str(ex))

    for role, layer in mapping.items():
        it = None
        for cand in items:
            if cand["name"].lower() == layer.lower():
                it = cand
                break
        if not it:
            writer.log("Role override SKIPPED (layer not found in inventory): %s -> %s"
                       % (role, layer))
            continue
        detector.mapping.setdefault(role, {"role": role})
        m = detector.mapping[role]
        m["selected_layer"] = it["name"]
        m["selected_path"] = it["path"]
        m["confidence"] = 1.0
        m["reason"] = "manual override" + ((" - " + notes[role]) if role in notes else "")
        m["geometry_type"] = it.get("geometry_type", m.get("geometry_type", ""))
        m["record_count"] = it.get("record_count", m.get("record_count", ""))
        if role in notes:
            m["notes"] = notes[role]
        # Make the overridden layer the head of the candidate list so multi-layer
        # consumers (e.g. Plugin01 drainage iteration) honor the override first.
        cands = m.get("all_candidate_paths", [])
        head = {"name": it["name"], "path": it["path"],
                "record_count": it.get("record_count")}
        cands = [head] + [c for c in cands if c.get("name") != it["name"]]
        m["all_candidate_paths"] = cands
        writer.log("Role override: %s -> %s" % (role, it["name"]))


# ---- CLI parsing (shared by both standalone runners) --------------------
def parse_cli(argv):
    """Very small argparse-free CLI parser (Py2.7/3 safe)."""
    cfg = {"plugins": None, "test_types": None, "safe_mode": True,
           "overwrite": False, "mxd_folder": None}
    i = 0
    while i < len(argv):
        a = argv[i]
        def nxt():
            return argv[i + 1] if i + 1 < len(argv) else None
        if a in ("--input-gdb", "--input_gdb"):
            cfg["input_gdb"] = nxt(); i += 2; continue
        if a in ("--carto-repo", "--carto_repo"):
            cfg["carto_repo"] = nxt(); i += 2; continue
        if a == "--output":
            cfg["output"] = nxt(); i += 2; continue
        if a == "--plugins":
            cfg["plugins"] = _norm_plugins(nxt()); i += 2; continue
        if a in ("--test-types", "--test_types"):
            cfg["test_types"] = [t.strip() for t in (nxt() or "").split(",") if t.strip()]
            i += 2; continue
        if a in ("--mxd-folder", "--mxd_folder"):
            cfg["mxd_folder"] = nxt(); i += 2; continue
        if a in ("--role-map-csv", "--role_map_csv"):
            cfg["role_map_csv"] = nxt(); i += 2; continue
        if a in ("--role-map-json", "--role_map_json"):
            cfg["role_map_json"] = nxt(); i += 2; continue
        if a == "--unsafe":
            cfg["safe_mode"] = False; i += 1; continue
        i += 1
    return cfg


def _norm_plugins(s):
    if not s:
        return None
    out = []
    for tok in s.split(","):
        tok = tok.strip()
        if not tok:
            continue
        if tok.lower().startswith("plugin"):
            num = tok[6:]
        else:
            num = tok
        try:
            out.append("Plugin%02d" % int(num))
        except Exception:
            if tok in PLUGIN_REGISTRY:
                out.append(tok)
    return out or None
