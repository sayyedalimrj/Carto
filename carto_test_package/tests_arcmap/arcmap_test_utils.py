# -*- coding: utf-8 -*-
"""
arcmap_test_utils.py -- ArcMap (Python 2.7) platform helpers for the Carto
test framework.

Most of the test logic is platform-neutral and lives in
common/carto_test_core.py. This module only holds the few things that differ
between ArcMap and Pro:

  * locating the shared 'common' package on sys.path,
  * detecting whether an active ArcMap map document is available
    (so Plugin07 can run in AOI_LAYER_IN_CURRENT_MXD mode),
  * a thin, defensive add-to-map helper (arcpy.mapping, with arcpy.mp fallback).

It is intentionally written for Python 2.7: no f-strings, no pathlib,
no Py3-only syntax.
"""

import os
import sys


def add_common_to_path():
    """Put <package>/common on sys.path and return it."""
    here = os.path.dirname(os.path.abspath(__file__))
    common = os.path.normpath(os.path.join(here, "..", "common"))
    if common not in sys.path:
        sys.path.insert(0, common)
    return common


def detect_active_map(arcpy):
    """Return True when a CURRENT ArcMap document (or Pro project) is available."""
    # ArcMap path
    try:
        import arcpy.mapping as mapping
        mxd = mapping.MapDocument("CURRENT")
        if mxd is not None:
            del mxd
            return True
    except Exception:
        pass
    # If someone loaded this in Pro by mistake, try arcpy.mp
    try:
        aprx = arcpy.mp.ArcGISProject("CURRENT")
        if aprx is not None:
            del aprx
            return True
    except Exception:
        pass
    return False


def add_to_current_map(arcpy, fc_path):
    """Best-effort add of a dataset to the active ArcMap map document.
    Tries arcpy.mapping first, then arcpy.mp. Never raises."""
    try:
        import arcpy.mapping as mapping
        mxd = mapping.MapDocument("CURRENT")
        df = mapping.ListDataFrames(mxd)[0]
        lyr = mapping.Layer(fc_path)
        mapping.AddLayer(df, lyr, "TOP")
        arcpy.RefreshActiveView()
        arcpy.RefreshTOC()
        return True
    except Exception:
        pass
    try:
        aprx = arcpy.mp.ArcGISProject("CURRENT")
        m = aprx.activeMap or aprx.listMaps()[0]
        m.addDataFromPath(fc_path)
        return True
    except Exception:
        return False
