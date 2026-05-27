# -*- coding: utf-8 -*-
"""
Master_ArcMap_Carto.pyt
=======================

Master Python Toolbox (PYT) for Project Carto - ArcMap 10.x track (Python 2.7).

This toolbox is a thin *aggregator*. It does NOT re-implement any plugin
logic. Instead, it discovers the 7 sibling plugin Python Toolboxes that
live next to this file in the repository root, loads them as Python
modules, and re-exposes one representative tool class from each plugin
under a single unified ArcMap toolbox.

ArcMap users who add ONLY this single ``Master_ArcMap_Carto.pyt`` to
ArcToolbox will see all 7 Carto tools in one place, with the alias
``carto_arcmap``.

------------------------------------------------------------------------
PLUGINS AGGREGATED
------------------------------------------------------------------------
    01  Plugin01_BridgeCulvert_ArcMap_py27.pyt
            -> BuildBridgePoints                    (representative)
    02  Plugin02_RoadDeconflict_ArcMap_v5_hardened.pyt
            -> RoadDeconflictTool
    03  Plugin03_ContourLabelOptimizer_ArcMap_v4_hardened.pyt
            -> OptimizeContourLabelAnchorsV4
    04  Plugin04_ElevationTextDeconflict_ArcMap_v5_hardened.pyt
            -> ElevationTextDeconflictV5
    05  Plugin05_SafeContourCleaner_ArcMap_v5_hardened.pyt
            -> SafeContourCleaner
    06  Plugin06_SpringRotation_ArcMap_v4_hardened.pyt
            -> SpringRotationFinalSuiteTool
    07  Plugin07_BatchGridBuilder_ArcMap_v6_hardened.pyt
            -> BatchGridBuilder07

------------------------------------------------------------------------
IMPORT STRATEGY (Option 2 - explicit sys.path injection)
------------------------------------------------------------------------
ArcMap's PYT machinery cannot ``import`` another ``*.pyt`` file using a
plain ``import`` statement (the ``.pyt`` extension is not recognised by
Python's default importer). The agreed-upon pattern is therefore:

    1. Resolve the directory holding THIS file via ``os.path.dirname(__file__)``.
    2. Insert that directory at the front of ``sys.path`` so any ordinary
       ``.py`` modules dropped beside the toolbox can be imported normally.
    3. For every sibling ``*.pyt`` file, load it as a Python source module
       using the stdlib ``imp`` module (``imp.load_source``), which is the
       Python 2.7 way to load a source file regardless of its extension.
    4. Pull the desired tool class off the loaded module via ``getattr``.

This keeps each plugin file 100% self-contained and independently
maintainable; the Master toolbox is purely a re-export layer.

------------------------------------------------------------------------
RESILIENCE
------------------------------------------------------------------------
If a plugin file is missing or fails to load (e.g. a developer pulled an
incomplete checkout), the Master toolbox does NOT raise at module load
time. Instead it substitutes a small ``_BrokenTool`` placeholder so the
remaining 6 tools still appear in ArcToolbox and the failure is reported
inside the offending tool's ``execute`` as a clean ``arcpy.AddError``.

------------------------------------------------------------------------
HELP / GUIDE DOCUMENTATION (.xml sidecar files - native ArcMap)
------------------------------------------------------------------------
ArcMap natively supports per-tool help via XML sidecar files placed next
to the toolbox. For a Python Toolbox the naming convention is:

    <ToolboxStem>.<ToolClassName>.pyt.xml

For THIS master toolbox the stem is ``Master_ArcMap_Carto`` and the
exposed tool class names are listed below. Drop the corresponding XML
files alongside this ``.pyt`` to populate the "Item Description" /
"Tool Help" panels inside ArcCatalog and ArcMap:

    Master_ArcMap_Carto.BuildBridgePoints.pyt.xml
    Master_ArcMap_Carto.RoadDeconflictTool.pyt.xml
    Master_ArcMap_Carto.OptimizeContourLabelAnchorsV4.pyt.xml
    Master_ArcMap_Carto.ElevationTextDeconflictV5.pyt.xml
    Master_ArcMap_Carto.SafeContourCleaner.pyt.xml
    Master_ArcMap_Carto.SpringRotationFinalSuiteTool.pyt.xml
    Master_ArcMap_Carto.BatchGridBuilder07.pyt.xml

To author/edit these XML files use ArcCatalog:
    1. Browse to ``Master_ArcMap_Carto.pyt`` in the Catalog tree.
    2. Expand it, right-click the desired tool, choose **Item Description**.
    3. Click **Edit** and fill in Summary / Usage / Syntax / Code Samples.
    4. Save. ArcCatalog writes the ``*.pyt.xml`` sidecar automatically.

The XML files can be version-controlled alongside the ``.pyt`` and will
travel with the toolbox.
"""

import os
import sys
import imp
import traceback

import arcpy


# =============================================================================
# Module discovery & loading (Option 2: explicit sys.path injection)
# =============================================================================

# 1. Inject this file's directory into sys.path so plain .py helpers
#    that ship with the plugins (if any) become importable.
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
if _THIS_DIR not in sys.path:
    sys.path.insert(0, _THIS_DIR)


# Mapping of:
#   internal_key -> (sibling_pyt_filename, tool_class_name_inside_that_file)
#
# The internal key is also used as the module name passed to imp.load_source,
# so each plugin gets a stable, unique entry in sys.modules.
_PLUGIN_MAP = [
    ("carto_plugin01_bridge_culvert",
     "Plugin01_BridgeCulvert_ArcMap_py27.pyt",
     "BuildBridgePoints"),
    ("carto_plugin02_road_deconflict",
     "Plugin02_RoadDeconflict_ArcMap_v5_hardened.pyt",
     "RoadDeconflictTool"),
    ("carto_plugin03_contour_label_optimizer",
     "Plugin03_ContourLabelOptimizer_ArcMap_v4_hardened.pyt",
     "OptimizeContourLabelAnchorsV4"),
    ("carto_plugin04_elevation_text_deconflict",
     "Plugin04_ElevationTextDeconflict_ArcMap_v5_hardened.pyt",
     "ElevationTextDeconflictV5"),
    ("carto_plugin05_safe_contour_cleaner",
     "Plugin05_SafeContourCleaner_ArcMap_v5_hardened.pyt",
     "SafeContourCleaner"),
    ("carto_plugin06_spring_rotation",
     "Plugin06_SpringRotation_ArcMap_v4_hardened.pyt",
     "SpringRotationFinalSuiteTool"),
    ("carto_plugin07_batch_grid_builder",
     "Plugin07_BatchGridBuilder_ArcMap_v6_hardened.pyt",
     "BatchGridBuilder07"),
]


def _load_plugin_class(module_key, filename, class_name):
    """Load a tool class from a sibling *.pyt file.

    Uses ``imp.load_source`` (Python 2.7 friendly) because ArcMap's PYT
    files do not have a ``.py`` extension and therefore cannot be picked
    up by a plain ``import`` statement.

    Returns the class object on success, or ``None`` on failure (the
    caller substitutes a ``_BrokenTool`` placeholder).
    """
    full_path = os.path.join(_THIS_DIR, filename)
    if not os.path.isfile(full_path):
        arcpy.AddWarning(
            "[Carto Master] Plugin file not found: {0}".format(full_path)
        )
        return None
    try:
        mod = imp.load_source(module_key, full_path)
    except (ImportError, SyntaxError, RuntimeError):
        arcpy.AddWarning(
            "[Carto Master] Failed to load {0}:\n{1}".format(
                filename, traceback.format_exc()
            )
        )
        return None
    cls = getattr(mod, class_name, None)
    if cls is None:
        arcpy.AddWarning(
            "[Carto Master] Class {0!r} not found in {1}".format(
                class_name, filename
            )
        )
        return None
    return cls


class _BrokenTool(object):
    """Stand-in tool used when a plugin fails to load.

    Keeps the master toolbox functional even if one plugin file is
    missing or invalid; the failure is surfaced cleanly when the user
    actually tries to run that one tool.
    """
    _missing_label = "Unavailable Carto Tool"
    _missing_reason = "Plugin module could not be loaded."

    def __init__(self):
        self.label = self._missing_label
        self.description = self._missing_reason
        self.canRunInBackground = False
        self.category = "Carto (unavailable)"

    def getParameterInfo(self):
        return []

    def isLicensed(self):
        return True

    def updateParameters(self, parameters):
        return

    def updateMessages(self, parameters):
        return

    def execute(self, parameters, messages):
        arcpy.AddError(
            "Carto plugin not available: {0}".format(self._missing_reason)
        )


def _make_broken(label, reason):
    """Factory producing a uniquely-named subclass of _BrokenTool.

    ArcMap registers tools by their class name; giving each placeholder
    its own subclass keeps them addressable individually in the toolbox.
    """
    cls = type(
        str("Broken_" + label.replace(" ", "_")),
        (_BrokenTool,),
        {"_missing_label": label, "_missing_reason": reason},
    )
    return cls


# =============================================================================
# Resolve every plugin tool class (or a Broken placeholder) at import time
# =============================================================================

_RESOLVED = {}
for _key, _fname, _cls_name in _PLUGIN_MAP:
    _resolved_cls = _load_plugin_class(_key, _fname, _cls_name)
    if _resolved_cls is None:
        _resolved_cls = _make_broken(
            _cls_name,
            "Could not load {0} from {1}".format(_cls_name, _fname),
        )
    _RESOLVED[_cls_name] = _resolved_cls


# Expose each tool class at module level under its ORIGINAL name so
# ArcMap's PYT loader (which inspects the module namespace) can find
# them, AND so help XML sidecars resolve correctly:
#     Master_ArcMap_Carto.<ClassName>.pyt.xml
BuildBridgePoints              = _RESOLVED["BuildBridgePoints"]
RoadDeconflictTool             = _RESOLVED["RoadDeconflictTool"]
OptimizeContourLabelAnchorsV4  = _RESOLVED["OptimizeContourLabelAnchorsV4"]
ElevationTextDeconflictV5      = _RESOLVED["ElevationTextDeconflictV5"]
SafeContourCleaner             = _RESOLVED["SafeContourCleaner"]
SpringRotationFinalSuiteTool   = _RESOLVED["SpringRotationFinalSuiteTool"]
BatchGridBuilder07             = _RESOLVED["BatchGridBuilder07"]


# =============================================================================
# The Toolbox itself
# =============================================================================

class Toolbox(object):
    """Master Carto Toolbox for ArcMap 10.x (Python 2.7).

    ``self.label`` is what ArcCatalog/ArcMap shows in the toolbox tree.
    ``self.alias`` is the scripting alias used when invoking tools from
    arcpy, e.g. ``arcpy.<ToolName>_carto_arcmap(...)``.

    Tool-level help is supplied through XML sidecars; see the module
    docstring for the naming convention and authoring workflow.
    """

    def __init__(self):
        self.label = "Carto Toolbox (ArcMap)"
        self.alias = "carto_arcmap"
        self.tools = [
            BuildBridgePoints,              # Plugin 01
            RoadDeconflictTool,             # Plugin 02
            OptimizeContourLabelAnchorsV4,  # Plugin 03
            ElevationTextDeconflictV5,      # Plugin 04
            SafeContourCleaner,             # Plugin 05
            SpringRotationFinalSuiteTool,   # Plugin 06
            BatchGridBuilder07,             # Plugin 07
        ]
