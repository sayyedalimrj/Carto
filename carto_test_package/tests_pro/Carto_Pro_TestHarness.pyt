# -*- coding: utf-8 -*-
"""
Carto_Pro_TestHarness.pyt
=========================
Python Toolbox (ArcGIS Pro, Python 3.x) exposing the Carto plugin test suite as
a geoprocessing tool. Running it from inside Pro provides an active project/map,
which also enables Plugin07's AOI_LAYER_IN_CURRENT_MXD mode.

Drop this .pyt next to the 'common' and 'tests_pro' folders of the
carto_test_package. It imports the shared engine from ../common.
"""

import os
import sys

import arcpy

_HERE = os.path.dirname(os.path.abspath(__file__))
_COMMON = os.path.normpath(os.path.join(_HERE, "..", "common"))
if _COMMON not in sys.path:
    sys.path.insert(0, _COMMON)


class Toolbox(object):
    def __init__(self):
        self.label = "Carto Pro Test Harness"
        self.alias = "cartoTestPro"
        self.tools = [RunCartoTests]


class RunCartoTests(object):
    def __init__(self):
        self.label = "Run Carto Plugin Tests (Pro)"
        self.description = ("Runs the automated Carto plugin test suite against "
                            "COPIES of the input data. The source geodatabase is "
                            "never modified.")
        self.canRunInBackground = False

    def getParameterInfo(self):
        p_gdb = arcpy.Parameter(displayName="Input Geodatabase (read-only)",
                                name="input_gdb", datatype="DEWorkspace",
                                parameterType="Required", direction="Input")
        p_repo = arcpy.Parameter(displayName="Carto Repository Path",
                                 name="carto_repo", datatype="DEFolder",
                                 parameterType="Required", direction="Input")
        p_out = arcpy.Parameter(displayName="Output Folder",
                                name="output", datatype="DEFolder",
                                parameterType="Required", direction="Input")
        p_plat = arcpy.Parameter(displayName="Platform",
                                 name="platform", datatype="GPString",
                                 parameterType="Required", direction="Input")
        p_plat.filter.type = "ValueList"
        p_plat.filter.list = ["ArcGISPro"]
        p_plat.value = "ArcGISPro"
        p_plugins = arcpy.Parameter(displayName="Plugin Selection",
                                    name="plugins", datatype="GPString",
                                    parameterType="Optional", direction="Input",
                                    multiValue=True)
        p_plugins.filter.type = "ValueList"
        p_plugins.filter.list = ["Plugin01", "Plugin02", "Plugin03", "Plugin04",
                                 "Plugin05", "Plugin06", "Plugin07"]
        p_types = arcpy.Parameter(displayName="Test Type Selection",
                                  name="test_types", datatype="GPString",
                                  parameterType="Optional", direction="Input",
                                  multiValue=True)
        p_types.filter.type = "ValueList"
        p_types.filter.list = ["smoke", "functional", "edge", "regression"]
        p_safe = arcpy.Parameter(displayName="Safe Mode (never edit source data)",
                                 name="safe_mode", datatype="GPBoolean",
                                 parameterType="Optional", direction="Input")
        p_safe.value = True
        p_over = arcpy.Parameter(displayName="Overwrite Existing Test Output",
                                 name="overwrite", datatype="GPBoolean",
                                 parameterType="Optional", direction="Input")
        p_over.value = True
        p_mxd = arcpy.Parameter(displayName="Project Folder (optional, Plugin07 FOLDER_OF_MXDS)",
                                name="mxd_folder", datatype="DEFolder",
                                parameterType="Optional", direction="Input")
        p_run = arcpy.Parameter(displayName="Run directory (output)",
                                name="run_dir", datatype="GPString",
                                parameterType="Derived", direction="Output")
        return [p_gdb, p_repo, p_out, p_plat, p_plugins, p_types,
                p_safe, p_over, p_mxd, p_run]

    def isLicensed(self):
        return True

    def updateParameters(self, parameters):
        return

    def updateMessages(self, parameters):
        return

    def execute(self, parameters, messages):
        import carto_test_core as core
        import pro_test_utils as platutils

        plugins = None
        if parameters[4].valueAsText:
            plugins = [p.strip() for p in parameters[4].valueAsText.split(";") if p.strip()]
        test_types = None
        if parameters[5].valueAsText:
            test_types = [t.strip() for t in parameters[5].valueAsText.split(";") if t.strip()]

        cfg = {
            "arcpy": arcpy,
            "platform": "ArcGISPro",
            "input_gdb": parameters[0].valueAsText,
            "carto_repo": parameters[1].valueAsText,
            "output": parameters[2].valueAsText,
            "plugins": plugins,
            "test_types": test_types,
            "safe_mode": bool(parameters[6].value),
            "overwrite": bool(parameters[7].value),
            "mxd_folder": parameters[8].valueAsText,
            "has_map": platutils.detect_active_map(arcpy),
        }
        arcpy.AddMessage("Carto Pro Test Harness starting...")
        run_dir = core.run(cfg)
        parameters[9].value = run_dir
        arcpy.AddMessage("DONE. Reports: {0}".format(
            os.path.join(run_dir, "reports", "ALL_TEST_SUMMARY.md")))
        return
