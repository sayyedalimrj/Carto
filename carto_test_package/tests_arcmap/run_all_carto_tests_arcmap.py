# -*- coding: utf-8 -*-
"""
run_all_carto_tests_arcmap.py
=============================
Standalone ArcMap / ArcGIS Desktop (Python 2.7, arcpy Desktop) test runner for
all Carto plugins.

It loads the shared engine in ../common/carto_test_core.py and runs the test
suite against COPIES of the data (the source geodatabase is never modified).

Usage (ArcMap 10.x Python 2.7)::

    C:\\Python27\\ArcGIS10.x\\python.exe run_all_carto_tests_arcmap.py ^
        --input-gdb "C:\\data\\Test_Cartography1.gdb" ^
        --carto-repo "C:\\code\\Carto" ^
        --output "C:\\data\\carto_test_output"

Optional flags::

    --plugins Plugin01,Plugin06      run only some plugins (default: all)
    --test-types smoke,functional    run only some test types
                                      (smoke|functional|edge|regression)
    --mxd-folder "C:\\sheets"         enable Plugin07 FOLDER_OF_MXDS mode
    --role-map-csv "overrides.csv"   manual layer-role override table
    --unsafe                          (NOT recommended) disable safe-mode guard

Outputs land in <output>/Carto_Test_Run_<timestamp>/ (test_data.gdb,
result_data.gdb, logs/, reports/, snapshots_before/, snapshots_after/,
qa_layers/). Read reports/ALL_TEST_SUMMARY.md first.
"""

import os
import sys

try:
    import arcpy
except ImportError:
    sys.stderr.write(
        "ERROR: arcpy is not importable. Run this with the ArcMap 10.x "
        "Python 2.7 interpreter, e.g. C:\\Python27\\ArcGIS10.x\\python.exe\n")
    raise

# Locate the shared engine.
_HERE = os.path.dirname(os.path.abspath(__file__))
_COMMON = os.path.normpath(os.path.join(_HERE, "..", "common"))
if _COMMON not in sys.path:
    sys.path.insert(0, _COMMON)

import carto_test_core as core            # noqa: E402
import arcmap_test_utils as platutils      # noqa: E402


def main(argv):
    cfg = core.parse_cli(argv)
    missing = [k for k in ("input_gdb", "carto_repo") if not cfg.get(k)]
    if missing:
        sys.stderr.write("Missing required arguments: " + ", ".join(
            "--" + m.replace("_", "-") for m in missing) + "\n")
        sys.stderr.write(__doc__)
        return 2
    cfg["arcpy"] = arcpy
    cfg["platform"] = "ArcMap"
    cfg["has_map"] = platutils.detect_active_map(arcpy)
    if not cfg.get("output"):
        cfg["output"] = os.path.join(os.getcwd(), "carto_test_output")

    print("Carto ArcMap test harness")
    print("  input gdb : " + str(cfg["input_gdb"]))
    print("  carto repo: " + str(cfg["carto_repo"]))
    print("  output    : " + str(cfg["output"]))
    print("  has map   : " + str(cfg["has_map"]))

    run_dir = core.run(cfg)
    print("")
    print("DONE. See: " + os.path.join(run_dir, "reports", "ALL_TEST_SUMMARY.md"))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
