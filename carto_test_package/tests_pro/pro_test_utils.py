# -*- coding: utf-8 -*-
"""
pro_test_utils.py -- ArcGIS Pro (Python 3.x) platform helpers for the Carto
test framework.

The shared test logic lives in common/carto_test_core.py. This module only
holds the Pro-specific glue:

  * locating the shared 'common' package on sys.path,
  * detecting an active ArcGIS Pro project / map (arcpy.mp only),
  * a thin add-to-map helper (arcpy.mp only - no arcpy.mapping).
"""

import os
import sys


def add_common_to_path():
    here = os.path.dirname(os.path.abspath(__file__))
    common = os.path.normpath(os.path.join(here, "..", "common"))
    if common not in sys.path:
        sys.path.insert(0, common)
    return common


def detect_active_map(arcpy):
    """True when a CURRENT ArcGIS Pro project with a map is available."""
    try:
        aprx = arcpy.mp.ArcGISProject("CURRENT")
        maps = aprx.listMaps()
        return bool(maps)
    except Exception:
        return False


def add_to_current_map(arcpy, fc_path):
    """Best-effort add of a dataset to the active Pro map. Never raises."""
    try:
        aprx = arcpy.mp.ArcGISProject("CURRENT")
        m = getattr(aprx, "activeMap", None) or aprx.listMaps()[0]
        m.addDataFromPath(fc_path)
        return True
    except Exception:
        return False
