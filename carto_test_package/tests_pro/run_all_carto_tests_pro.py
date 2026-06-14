# -*- coding: utf-8 -*-
"""
run_all_carto_tests_pro.py
==========================
Standalone ArcGIS Pro (Python 3.x, arcpy Pro) test runner for all Carto plugins.

It loads the shared engine in ../common/carto_test_core.py and runs the test
suite against COPIES of the data (the source geodatabase is never modified).

Usage (ArcGIS Pro Python 3)::

    "C:\\Program Files\\ArcGIS\\Pro\\bin\\Python\\envs\\arcgispro-py3\\python.exe" ^
        run_all_carto_tests_pro.py ^
        --input-gdb "C:\\data\\Test_Cartography1.gdb" ^
        --carto-repo "C:\\code\\Carto" ^
        --output "C:\\data\\carto_test_output"

Optional flags::

    --plugins Plugin01,Plugin06
    --test-types smoke,functional       (smoke|functional|edge|regression)
    --mxd-folder "C:\\projects"          enable Plugin07 FOLDER_OF_MXDS mode (.aprx)
    --role-map-csv "overrides.csv"
    --unsafe

Outputs land in <output>/Carto_Test_Run_<timestamp>/. Read
reports/ALL_TEST_SUMMARY.md first.
"""

import os
import sys

try:
    import arcpy
except ImportError:
    sys.stderr.write(
        "ERROR: arcpy is not importable. Run this with the ArcGIS Pro Python 3 "
        "interpreter (arcgispro-py3 environment).\n")
    raise

_HERE = os.path.dirname(os.path.abspath(__file__))
_COMMON = os.path.normpath(os.path.join(_HERE, "..", "common"))
if _COMMON not in sys.path:
    sys.path.insert(0, _COMMON)

import carto_test_core as core          # noqa: E402
import pro_test_utils as platutils       # noqa: E402


def main(argv):
    cfg = core.parse_cli(argv)
    missing = [k for k in ("input_gdb", "carto_repo") if not cfg.get(k)]
    if missing:
        sys.stderr.write("Missing required arguments: " + ", ".join(
            "--" + m.replace("_", "-") for m in missing) + "\n")
        sys.stderr.write(__doc__)
        return 2
    cfg["arcpy"] = arcpy
    cfg["platform"] = "ArcGISPro"
    cfg["has_map"] = platutils.detect_active_map(arcpy)
    if not cfg.get("output"):
        cfg["output"] = os.path.join(os.getcwd(), "carto_test_output")

    print("Carto ArcGIS Pro test harness")
    print("  input gdb : {0}".format(cfg["input_gdb"]))
    print("  carto repo: {0}".format(cfg["carto_repo"]))
    print("  output    : {0}".format(cfg["output"]))
    print("  has map   : {0}".format(cfg["has_map"]))

    run_dir = core.run(cfg)
    print("")
    print("DONE. See: {0}".format(os.path.join(run_dir, "reports", "ALL_TEST_SUMMARY.md")))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
