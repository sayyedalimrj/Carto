# -*- coding: utf-8 -*-
"""
report_writer.py  -- Carto plugin test framework, shared reporting layer.

This module is intentionally written to load and run under BOTH:
  * Python 2.7 (ArcMap 10.x arcpy)   -- no f-strings, no pathlib, no Py3-only syntax
  * Python 3.x  (ArcGIS Pro arcpy)

It has NO arcpy dependency, so it can also be unit-tested with a plain
interpreter. Only the Python standard library is used.

Responsibilities
-----------------
* Collect per-test-case result records (dicts that follow common/test_schema.json).
* Serialize the global result set to:
    - reports/ALL_TEST_RESULTS.json
    - reports/ALL_TEST_RESULTS.csv
    - reports/ALL_TEST_SUMMARY.md
* Write per-plugin JSON + CSV reports (e.g. T01_bridge_culvert_test_report.json/.csv).
* Write before/after QA reports (BEFORE_AFTER_SUMMARY.md / .csv).
* Append-only run log helper.
"""

import os
import io
import csv
import json
import time
import datetime


# Field order used for CSV flattening (matches common/test_schema.json).
RESULT_FIELDS = [
    "plugin_id", "plugin_name", "platform", "test_name", "test_type",
    "input_layers", "output_layers", "parameters_used",
    "start_time", "end_time", "elapsed_seconds",
    "status", "skip_reason", "error_message", "traceback", "arcpy_messages",
    "before_feature_count", "after_feature_count", "changed_feature_count",
    "geometry_validity_result", "spatial_reference_check", "field_schema_check",
    "success_metrics", "notes", "active_workspace", "plugin_path",
]


def now_iso():
    """ISO-8601 timestamp (local time), Py2/3 safe."""
    return datetime.datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def _to_text(value):
    """Return a unicode/str representation safe for CSV in Py2 and Py3."""
    if value is None:
        return ""
    if isinstance(value, (dict, list, tuple)):
        try:
            return json.dumps(value, ensure_ascii=False, sort_keys=True)
        except Exception:
            return str(value)
    try:
        return u"{0}".format(value)
    except Exception:
        return str(value)


def make_result(plugin_id, plugin_name, platform, test_name, test_type):
    """Create a result record pre-populated with required keys + sane defaults."""
    rec = {}
    for k in RESULT_FIELDS:
        rec[k] = None
    rec["plugin_id"] = plugin_id
    rec["plugin_name"] = plugin_name
    rec["platform"] = platform
    rec["test_name"] = test_name
    rec["test_type"] = test_type
    rec["input_layers"] = []
    rec["output_layers"] = []
    rec["parameters_used"] = {}
    rec["success_metrics"] = {}
    rec["status"] = "SKIP"
    rec["skip_reason"] = ""
    rec["error_message"] = ""
    rec["traceback"] = ""
    rec["arcpy_messages"] = ""
    rec["notes"] = ""
    rec["start_time"] = now_iso()
    return rec


def finalize_timing(rec, t_start):
    rec["end_time"] = now_iso()
    try:
        rec["elapsed_seconds"] = round(time.time() - t_start, 3)
    except Exception:
        rec["elapsed_seconds"] = None
    return rec


class ReportWriter(object):
    """Accumulates results and writes all report artifacts under reports_dir."""

    def __init__(self, reports_dir, logs_dir=None):
        self.reports_dir = reports_dir
        self.logs_dir = logs_dir or reports_dir
        self.results = []
        self._log_path = os.path.join(self.logs_dir, "test_run.log")
        if not os.path.isdir(self.reports_dir):
            os.makedirs(self.reports_dir)
        if not os.path.isdir(self.logs_dir):
            os.makedirs(self.logs_dir)

    # -- logging ----------------------------------------------------------
    def log(self, msg):
        line = u"[{0}] {1}".format(now_iso(), _to_text(msg))
        try:
            f = io.open(self._log_path, "a", encoding="utf-8")
            try:
                f.write(line + u"\n")
            finally:
                f.close()
        except Exception:
            pass
        return line

    # -- result accumulation ---------------------------------------------
    def add(self, rec):
        self.results.append(rec)
        self.log(u"{0} | {1} | {2} | {3} -> {4} {5}".format(
            rec.get("platform"), rec.get("plugin_id"), rec.get("test_type"),
            rec.get("test_name"), rec.get("status"),
            (u"(" + _to_text(rec.get("skip_reason") or rec.get("error_message")) + u")")
            if rec.get("status") in ("SKIP", "FAIL", "WARN") else u""))
        return rec

    # -- per-plugin reports ----------------------------------------------
    def write_plugin_report(self, base_filename, records):
        """Write reports/<base_filename>.json and .csv for one plugin's records."""
        json_path = os.path.join(self.reports_dir, base_filename + ".json")
        csv_path = os.path.join(self.reports_dir, base_filename + ".csv")
        _write_json(json_path, records)
        _write_csv(csv_path, records, RESULT_FIELDS)
        return json_path, csv_path

    # -- global reports ---------------------------------------------------
    def write_all(self):
        json_path = os.path.join(self.reports_dir, "ALL_TEST_RESULTS.json")
        csv_path = os.path.join(self.reports_dir, "ALL_TEST_RESULTS.csv")
        md_path = os.path.join(self.reports_dir, "ALL_TEST_SUMMARY.md")
        _write_json(json_path, self.results)
        _write_csv(csv_path, self.results, RESULT_FIELDS)
        self._write_summary_md(md_path)
        return json_path, csv_path, md_path

    def _write_summary_md(self, md_path):
        counts = {"PASS": 0, "FAIL": 0, "WARN": 0, "SKIP": 0}
        by_plugin = {}
        for r in self.results:
            st = r.get("status", "SKIP")
            counts[st] = counts.get(st, 0) + 1
            pid = r.get("plugin_id", "?")
            by_plugin.setdefault(pid, {"PASS": 0, "FAIL": 0, "WARN": 0, "SKIP": 0})
            by_plugin[pid][st] = by_plugin[pid].get(st, 0) + 1

        lines = []
        lines.append(u"# Carto Plugin Test Suite - Summary")
        lines.append(u"")
        lines.append(u"Generated: {0}".format(now_iso()))
        lines.append(u"")
        lines.append(u"Total test cases: {0}".format(len(self.results)))
        lines.append(u"")
        lines.append(u"| Status | Count |")
        lines.append(u"|--------|------:|")
        for st in ("PASS", "FAIL", "WARN", "SKIP"):
            lines.append(u"| {0} | {1} |".format(st, counts.get(st, 0)))
        lines.append(u"")
        lines.append(u"## Per-plugin breakdown")
        lines.append(u"")
        lines.append(u"| Plugin | PASS | FAIL | WARN | SKIP |")
        lines.append(u"|--------|-----:|-----:|-----:|-----:|")
        for pid in sorted(by_plugin.keys()):
            b = by_plugin[pid]
            lines.append(u"| {0} | {1} | {2} | {3} | {4} |".format(
                pid, b.get("PASS", 0), b.get("FAIL", 0), b.get("WARN", 0), b.get("SKIP", 0)))
        lines.append(u"")
        lines.append(u"## Detail")
        lines.append(u"")
        lines.append(u"| Plugin | Platform | Type | Test | Status | Before | After | Changed | Note |")
        lines.append(u"|--------|----------|------|------|--------|-------:|------:|--------:|------|")
        for r in self.results:
            note = r.get("skip_reason") or r.get("error_message") or r.get("notes") or ""
            note = _to_text(note).replace("|", "/").replace("\n", " ")
            if len(note) > 80:
                note = note[:77] + "..."
            lines.append(u"| {0} | {1} | {2} | {3} | {4} | {5} | {6} | {7} | {8} |".format(
                r.get("plugin_id"), r.get("platform"), r.get("test_type"),
                _to_text(r.get("test_name")).replace("|", "/"), r.get("status"),
                _fmt(r.get("before_feature_count")), _fmt(r.get("after_feature_count")),
                _fmt(r.get("changed_feature_count")), note))
        _write_text(md_path, u"\n".join(lines) + u"\n")
        return md_path

    # -- before/after QA --------------------------------------------------
    def write_before_after(self, snapshots):
        """snapshots: list of dicts with keys:
        plugin_id, layer, phase(before/after), feature_count, geometry_summary,
        field_schema, extent, sr, samples, conflicts, angle_stats, distance_stats.
        """
        csv_path = os.path.join(self.reports_dir, "BEFORE_AFTER_SUMMARY.csv")
        md_path = os.path.join(self.reports_dir, "BEFORE_AFTER_SUMMARY.md")
        ba_fields = ["plugin_id", "layer", "phase", "feature_count",
                     "geometry_summary", "field_schema", "extent", "sr",
                     "conflicts", "angle_stats", "distance_stats", "samples"]
        _write_csv(csv_path, snapshots, ba_fields)
        lines = [u"# Before / After QA Summary", u"",
                 u"Generated: {0}".format(now_iso()), u""]
        lines.append(u"| Plugin | Layer | Phase | Count | Conflicts | Angle stats | Distance stats |")
        lines.append(u"|--------|-------|-------|------:|-----------|-------------|----------------|")
        for s in snapshots:
            lines.append(u"| {0} | {1} | {2} | {3} | {4} | {5} | {6} |".format(
                s.get("plugin_id"), s.get("layer"), s.get("phase"),
                _fmt(s.get("feature_count")),
                _to_text(s.get("conflicts")).replace("|", "/"),
                _to_text(s.get("angle_stats")).replace("|", "/"),
                _to_text(s.get("distance_stats")).replace("|", "/")))
        _write_text(md_path, u"\n".join(lines) + u"\n")
        return csv_path, md_path


# --------------------------------------------------------------------------
# Low-level helpers (module-level so other modules can reuse them)
# --------------------------------------------------------------------------
def _fmt(v):
    return "" if v is None else _to_text(v)


def _write_json(path, obj):
    f = io.open(path, "w", encoding="utf-8")
    try:
        data = json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=True)
        # Py2: json.dumps may return str; ensure unicode for io.open
        if not isinstance(data, type(u"")):
            data = data.decode("utf-8")
        f.write(data)
    finally:
        f.close()
    return path


def _write_text(path, text):
    f = io.open(path, "w", encoding="utf-8")
    try:
        if not isinstance(text, type(u"")):
            text = text.decode("utf-8")
        f.write(text)
    finally:
        f.close()
    return path


def _write_csv(path, records, fields):
    """CSV writer that works in Py2 and Py3 (utf-8)."""
    rows = []
    for r in records:
        row = []
        for k in fields:
            row.append(_to_text(r.get(k)) if isinstance(r, dict) else _to_text(r))
        rows.append(row)
    # Py3 path
    try:
        f = io.open(path, "w", encoding="utf-8", newline="")
        try:
            w = csv.writer(f)
            w.writerow(fields)
            for row in rows:
                w.writerow(row)
        finally:
            f.close()
        return path
    except TypeError:
        # Py2 path: open in binary, encode cells to utf-8
        f = open(path, "wb")
        try:
            w = csv.writer(f)
            w.writerow([c.encode("utf-8") for c in fields])
            for row in rows:
                w.writerow([c.encode("utf-8") for c in row])
        finally:
            f.close()
        return path


def write_json(path, obj):
    return _write_json(path, obj)


def write_csv(path, records, fields):
    return _write_csv(path, records, fields)


def write_text(path, text):
    return _write_text(path, text)
