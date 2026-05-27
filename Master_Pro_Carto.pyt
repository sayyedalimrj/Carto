# -*- coding: utf-8 -*-
"""
Master_Pro_Carto.pyt
====================
Master wrapper Python Toolbox for the Carto suite (ArcGIS Pro / Python 3).

This single .pyt re-exposes the seven Carto plugin tools (Plugins 01-07)
under one toolbox so that all tools appear together in the ArcGIS Pro
"Catalog" pane.  Each tool here is a stub that delegates to the actual
implementation in its own .pyt file.  The stubs implement the standard
ArcGIS Pro Python Toolbox lifecycle methods so the tools can be wired
into model builder, batch tools, geoprocessing history, and the catalog
metadata system without modification.

Lifecycle methods present on every tool class:

  * ``__init__``           - tool label + description
  * ``getParameterInfo``   - parameter declarations
  * ``isLicensed``         - product / extension gating
  * ``updateParameters``   - dynamic parameter behaviour
  * ``updateMessages``     - validation / warnings / errors
  * ``execute``            - actual run
  * ``postExecute``        - post-run hook (e.g. add to map, refresh)

================================================================================
ArcGIS Pro Catalog Integration: Metadata, Help and User Guide
================================================================================

ArcGIS Pro stores tool documentation in three layers, which all hang off
this single .pyt file:

1. Item-level metadata (XML).
   Each Python Toolbox and each tool inside it can have its own
   ArcGIS metadata record.  In Pro: open the Catalog pane, right-click
   ``Master_Pro_Carto.pyt`` (or one of its tools) and choose
   "Edit Metadata...".  This opens the metadata editor where you can
   fill in:

     * Title / Tags / Summary / Description
     * Credits / Use limitations
     * Thumbnail
     * Resources (links to PDFs, URLs, images)

   Pro persists these fields in a sidecar ``.xml`` file alongside the
   ``.pyt`` (for the toolbox) and inside the ``.pyt.<toolname>.xml``
   files (for each tool).  Commit those XML files alongside the .pyt
   so the metadata travels with the code.

2. Tool help (docstrings + parameter descriptions).
   The text of each tool's class docstring AND each parameter's
   ``description`` / ``displayName`` is surfaced in the geoprocessing
   tool dialog ("info" bubbles).  Keep the docstring concise; use the
   parameter ``description`` attribute for per-input guidance.

3. External user guide (HTML / PDF / Markdown).
   For longer-form documentation (the "How to use this tool" guide,
   release notes, cookbook examples), publish a separate document and
   link to it from the metadata editor's "Resources" tab.  Recommended
   layout in this repository:

       /docs/
           UI_GUIDE.md
           Plugin01_BridgeCulvert/Help.md
           Plugin02_RoadDeconflict/Help.md
           ...
           Plugin07_BatchGridBuilder/Help.md

   Convert the Markdown to HTML or PDF for distribution and add a
   resource link to each tool's metadata pointing at the rendered
   asset.

4. Inline parameter "help" text.
   The ``arcpy.Parameter.description`` attribute set in
   ``getParameterInfo`` is shown next to the input in the tool dialog
   and in the auto-generated tool reference page.  Always set it.

================================================================================
This wrapper is a STUB.
================================================================================

The seven tool classes below implement the lifecycle but their
``execute`` methods do NOT contain the real plugin logic.  Instead they
load the corresponding ``Plugin0N_..._Pro_..._native.pyt`` file from
the same folder, instantiate the underlying tool, and forward the
``parameters`` / ``messages`` arguments.  This keeps the master file
small while preserving the master-rules-hardened implementations as
the single source of truth.

To regenerate the wrapper:

  * Edit only the per-tool stubs below; do not duplicate logic.
  * If you add a new plugin, register its tool class in
    ``Toolbox.tools`` and add a stub class here.

Author: Ali Mirjafari + Kiro
Version: 1.0 (Pro / Python 3 / Master Wrapper)
"""

from __future__ import annotations

import importlib.util
import os
import traceback
from typing import List, Optional

import arcpy


# =============================================================================
# Wrapper plumbing: load the real plugin .pyt as a module and instantiate
# the underlying tool by class name.
# =============================================================================

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))


def _load_plugin_module(pyt_filename: str, module_name: str):
    """
    Load a sibling ``*.pyt`` file as a Python module so that we can
    instantiate its tool classes.  Returns the loaded module object,
    or raises ``arcpy.ExecuteError`` with a clear message.

    ``importlib`` is used (instead of ``imp`` or naked ``exec``) so
    that the plugin's relative imports and ``__name__`` work as
    expected at runtime.
    """
    pyt_path = os.path.join(_THIS_DIR, pyt_filename)
    if not os.path.isfile(pyt_path):
        raise arcpy.ExecuteError(
            f"Master_Pro_Carto: companion file not found: {pyt_path}")
    spec = importlib.util.spec_from_file_location(module_name, pyt_path)
    if spec is None or spec.loader is None:
        raise arcpy.ExecuteError(
            f"Master_Pro_Carto: cannot build import spec for {pyt_path}")
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    except (arcpy.ExecuteError, RuntimeError, ImportError) as ex:
        raise arcpy.ExecuteError(
            f"Master_Pro_Carto: failed to load {pyt_filename}: {ex}")
    return mod


def _delegate(method: str, pyt_filename: str, module_name: str,
              tool_class_name: str, parameters, messages=None):
    """
    Forward a lifecycle call to the real plugin tool's method.

    method: one of {"getParameterInfo", "updateParameters",
                    "updateMessages", "execute", "postExecute",
                    "isLicensed"}
    """
    mod = _load_plugin_module(pyt_filename, module_name)
    cls = getattr(mod, tool_class_name, None)
    if cls is None:
        raise arcpy.ExecuteError(
            f"Master_Pro_Carto: class '{tool_class_name}' not found in "
            f"{pyt_filename}")
    instance = cls()
    fn = getattr(instance, method, None)
    if fn is None:
        # Lifecycle hook not implemented downstream; default behaviours.
        if method == "isLicensed":
            return True
        if method == "getParameterInfo":
            return []
        return None
    if method in ("getParameterInfo", "isLicensed"):
        return fn()
    if method == "updateParameters":
        return fn(parameters)
    if method == "updateMessages":
        return fn(parameters)
    if method == "execute":
        return fn(parameters, messages)
    if method == "postExecute":
        return fn(parameters)
    raise arcpy.ExecuteError(
        f"Master_Pro_Carto: unknown lifecycle method '{method}'")


# =============================================================================
# Toolbox
# =============================================================================

class Toolbox:
    """
    Carto Toolbox (Pro) - master wrapper.

    Registers all seven Carto plugin tools under a single toolbox so
    they appear together in the ArcGIS Pro Catalog pane.

    Metadata: edit via Catalog -> right-click this .pyt -> "Edit Metadata".
    Per-tool help: edit each tool's class docstring and per-parameter
    ``description`` strings in the underlying plugin .pyt files.
    """

    def __init__(self):
        self.label = "Carto Toolbox (Pro)"
        self.alias = "carto_pro"
        self.tools = [
            BridgeCulvertTool,            # Plugin 01
            RoadDeconflictTool,           # Plugin 02
            ContourLabelOptimizerTool,    # Plugin 03
            ElevationTextDeconflictTool,  # Plugin 04
            SafeContourCleanerTool,       # Plugin 05
            SpringRotationTool,           # Plugin 06
            BatchGridBuilderTool,         # Plugin 07
        ]


# =============================================================================
# Plugin 01 - Bridge / Culvert
# =============================================================================

class BridgeCulvertTool:
    """
    Plugin 01 - Bridge / Culvert builder (Pro).

    Catalog metadata: see ``Master_Pro_Carto.pyt`` module docstring for
    instructions on filling in title / summary / description / tags.
    Inline parameter help: edit the ``description`` attribute of each
    ``arcpy.Parameter`` in the underlying plugin file's
    ``getParameterInfo``.
    """

    _PYT = "Plugin01_BridgeCulvert_Pro_py3.pyt"
    _MOD = "carto_plugin01_bridgeculvert"
    _CLS = "BridgeCulvert"  # canonical name; falls back below if needed.

    def __init__(self):
        self.label = "01) Bridge / Culvert (Pro)"
        self.description = (
            "Build bridge / culvert features per the Carto plugin 01 "
            "specification.  See companion file "
            f"'{self._PYT}' for the implementation.")
        self.canRunInBackground = False

    def isLicensed(self) -> bool:
        try:
            return bool(_delegate("isLicensed", self._PYT, self._MOD,
                                   self._CLS, None))
        except (arcpy.ExecuteError, RuntimeError):
            return True

    def getParameterInfo(self):
        return _delegate("getParameterInfo", self._PYT, self._MOD,
                          self._CLS, None)

    def updateParameters(self, parameters):
        return _delegate("updateParameters", self._PYT, self._MOD,
                          self._CLS, parameters)

    def updateMessages(self, parameters):
        return _delegate("updateMessages", self._PYT, self._MOD,
                          self._CLS, parameters)

    def execute(self, parameters, messages):
        return _delegate("execute", self._PYT, self._MOD,
                          self._CLS, parameters, messages)

    def postExecute(self, parameters):
        return _delegate("postExecute", self._PYT, self._MOD,
                          self._CLS, parameters)


# =============================================================================
# Plugin 02 - Road Deconflict
# =============================================================================

class RoadDeconflictTool:
    """
    Plugin 02 - Road deconfliction (Pro).

    Resolves overlapping road symbology / casing per Carto plugin 02.
    Catalog metadata + extended help: see module docstring.
    """

    _PYT = "Plugin02_RoadDeconflict_Pro_v5_native.pyt"
    _MOD = "carto_plugin02_roaddeconflict"
    _CLS = "RoadDeconflict"

    def __init__(self):
        self.label = "02) Road Deconflict (Pro)"
        self.description = (
            "Deconflict road centrelines and casings per the Carto "
            "plugin 02 specification.  See companion file "
            f"'{self._PYT}' for the implementation.")
        self.canRunInBackground = False

    def isLicensed(self) -> bool:
        try:
            return bool(_delegate("isLicensed", self._PYT, self._MOD,
                                   self._CLS, None))
        except (arcpy.ExecuteError, RuntimeError):
            return True

    def getParameterInfo(self):
        return _delegate("getParameterInfo", self._PYT, self._MOD,
                          self._CLS, None)

    def updateParameters(self, parameters):
        return _delegate("updateParameters", self._PYT, self._MOD,
                          self._CLS, parameters)

    def updateMessages(self, parameters):
        return _delegate("updateMessages", self._PYT, self._MOD,
                          self._CLS, parameters)

    def execute(self, parameters, messages):
        return _delegate("execute", self._PYT, self._MOD,
                          self._CLS, parameters, messages)

    def postExecute(self, parameters):
        return _delegate("postExecute", self._PYT, self._MOD,
                          self._CLS, parameters)


# =============================================================================
# Plugin 03 - Contour Label Optimizer
# =============================================================================

class ContourLabelOptimizerTool:
    """
    Plugin 03 - Contour label optimizer (Pro).

    Places contour labels deterministically and avoids collisions.
    Catalog metadata + extended help: see module docstring.
    """

    _PYT = "Plugin03_ContourLabelOptimizer_Pro_v4_native.pyt"
    _MOD = "carto_plugin03_contourlabeloptimizer"
    _CLS = "ContourLabelOptimizer"

    def __init__(self):
        self.label = "03) Contour Label Optimizer (Pro)"
        self.description = (
            "Optimise contour label placement per the Carto plugin 03 "
            "specification.  See companion file "
            f"'{self._PYT}' for the implementation.")
        self.canRunInBackground = False

    def isLicensed(self) -> bool:
        try:
            return bool(_delegate("isLicensed", self._PYT, self._MOD,
                                   self._CLS, None))
        except (arcpy.ExecuteError, RuntimeError):
            return True

    def getParameterInfo(self):
        return _delegate("getParameterInfo", self._PYT, self._MOD,
                          self._CLS, None)

    def updateParameters(self, parameters):
        return _delegate("updateParameters", self._PYT, self._MOD,
                          self._CLS, parameters)

    def updateMessages(self, parameters):
        return _delegate("updateMessages", self._PYT, self._MOD,
                          self._CLS, parameters)

    def execute(self, parameters, messages):
        return _delegate("execute", self._PYT, self._MOD,
                          self._CLS, parameters, messages)

    def postExecute(self, parameters):
        return _delegate("postExecute", self._PYT, self._MOD,
                          self._CLS, parameters)


# =============================================================================
# Plugin 04 - Elevation Text Deconflict
# =============================================================================

class ElevationTextDeconflictTool:
    """
    Plugin 04 - Elevation text deconflict (Pro).

    Deconflicts elevation text features against obstacles.
    Catalog metadata + extended help: see module docstring.
    """

    _PYT = "Plugin04_ElevationTextDeconflict_Pro_v5_native.pyt"
    _MOD = "carto_plugin04_elevationtextdeconflict"
    _CLS = "ElevationTextDeconflict"

    def __init__(self):
        self.label = "04) Elevation Text Deconflict (Pro)"
        self.description = (
            "Deconflict elevation text features per the Carto plugin 04 "
            "specification.  See companion file "
            f"'{self._PYT}' for the implementation.")
        self.canRunInBackground = False

    def isLicensed(self) -> bool:
        try:
            return bool(_delegate("isLicensed", self._PYT, self._MOD,
                                   self._CLS, None))
        except (arcpy.ExecuteError, RuntimeError):
            return True

    def getParameterInfo(self):
        return _delegate("getParameterInfo", self._PYT, self._MOD,
                          self._CLS, None)

    def updateParameters(self, parameters):
        return _delegate("updateParameters", self._PYT, self._MOD,
                          self._CLS, parameters)

    def updateMessages(self, parameters):
        return _delegate("updateMessages", self._PYT, self._MOD,
                          self._CLS, parameters)

    def execute(self, parameters, messages):
        return _delegate("execute", self._PYT, self._MOD,
                          self._CLS, parameters, messages)

    def postExecute(self, parameters):
        return _delegate("postExecute", self._PYT, self._MOD,
                          self._CLS, parameters)


# =============================================================================
# Plugin 05 - Safe Contour Cleaner
# =============================================================================

class SafeContourCleanerTool:
    """
    Plugin 05 - Safe contour cleaner (Pro).

    Builds a cleaned COPY of contour line layers for cartographic
    output, with per-axis tick safety, optional full-map fallback,
    and sliver removal.  Catalog metadata + extended help: see
    module docstring.
    """

    _PYT = "Plugin05_SafeContourCleaner_Pro_v5_native.pyt"
    _MOD = "carto_plugin05_safecontourcleaner"
    _CLS = "SafeContourCleaner"

    def __init__(self):
        self.label = "05) Safe Contour Cleaner (Pro)"
        self.description = (
            "Clean dense contour segments while preserving frame safety, "
            "per the Carto plugin 05 specification.  See companion file "
            f"'{self._PYT}' for the implementation.")
        self.canRunInBackground = False

    def isLicensed(self) -> bool:
        try:
            return bool(_delegate("isLicensed", self._PYT, self._MOD,
                                   self._CLS, None))
        except (arcpy.ExecuteError, RuntimeError):
            return True

    def getParameterInfo(self):
        return _delegate("getParameterInfo", self._PYT, self._MOD,
                          self._CLS, None)

    def updateParameters(self, parameters):
        return _delegate("updateParameters", self._PYT, self._MOD,
                          self._CLS, parameters)

    def updateMessages(self, parameters):
        return _delegate("updateMessages", self._PYT, self._MOD,
                          self._CLS, parameters)

    def execute(self, parameters, messages):
        return _delegate("execute", self._PYT, self._MOD,
                          self._CLS, parameters, messages)

    def postExecute(self, parameters):
        return _delegate("postExecute", self._PYT, self._MOD,
                          self._CLS, parameters)


# =============================================================================
# Plugin 06 - Spring Rotation
# =============================================================================

class SpringRotationTool:
    """
    Plugin 06 - Spring rotation comparison suite (Pro).

    Compares up to five spring-rotation methods (NearTangent, HighLow,
    NearNormal, PlaneFit, CentroidHL).  Catalog metadata + extended
    help: see module docstring.
    """

    _PYT = "Plugin06_SpringRotation_Pro_v4_native.pyt"
    _MOD = "carto_plugin06_springrotation"
    _CLS = "SpringRotationFinalSuiteTool"

    def __init__(self):
        self.label = "06) Spring Rotation (Pro)"
        self.description = (
            "Compute and compare spring rotations per the Carto plugin "
            "06 specification.  See companion file "
            f"'{self._PYT}' for the implementation.")
        self.canRunInBackground = False

    def isLicensed(self) -> bool:
        try:
            return bool(_delegate("isLicensed", self._PYT, self._MOD,
                                   self._CLS, None))
        except (arcpy.ExecuteError, RuntimeError):
            return True

    def getParameterInfo(self):
        return _delegate("getParameterInfo", self._PYT, self._MOD,
                          self._CLS, None)

    def updateParameters(self, parameters):
        return _delegate("updateParameters", self._PYT, self._MOD,
                          self._CLS, parameters)

    def updateMessages(self, parameters):
        return _delegate("updateMessages", self._PYT, self._MOD,
                          self._CLS, parameters)

    def execute(self, parameters, messages):
        return _delegate("execute", self._PYT, self._MOD,
                          self._CLS, parameters, messages)

    def postExecute(self, parameters):
        return _delegate("postExecute", self._PYT, self._MOD,
                          self._CLS, parameters)


# =============================================================================
# Plugin 07 - Batch Grid Builder
# =============================================================================

class BatchGridBuilderTool:
    """
    Plugin 07 - Batch Grid / Graticule builder (Pro).

    Batch builds grids and graticules for many sheets, with per-axis
    tick safety cap and SMART_FEATURE / ESRI_XML engines.  Catalog
    metadata + extended help: see module docstring.
    """

    _PYT = "Plugin07_BatchGridBuilder_Pro_v6_native.pyt"
    _MOD = "carto_plugin07_batchgridbuilder"
    _CLS = "BatchGridBuilder07"

    def __init__(self):
        self.label = "07) Batch Grid Builder (Pro)"
        self.description = (
            "Batch build grids and graticules per the Carto plugin 07 "
            "specification.  See companion file "
            f"'{self._PYT}' for the implementation.")
        self.canRunInBackground = False

    def isLicensed(self) -> bool:
        try:
            return bool(_delegate("isLicensed", self._PYT, self._MOD,
                                   self._CLS, None))
        except (arcpy.ExecuteError, RuntimeError):
            return True

    def getParameterInfo(self):
        return _delegate("getParameterInfo", self._PYT, self._MOD,
                          self._CLS, None)

    def updateParameters(self, parameters):
        return _delegate("updateParameters", self._PYT, self._MOD,
                          self._CLS, parameters)

    def updateMessages(self, parameters):
        return _delegate("updateMessages", self._PYT, self._MOD,
                          self._CLS, parameters)

    def execute(self, parameters, messages):
        return _delegate("execute", self._PYT, self._MOD,
                          self._CLS, parameters, messages)

    def postExecute(self, parameters):
        return _delegate("postExecute", self._PYT, self._MOD,
                          self._CLS, parameters)
