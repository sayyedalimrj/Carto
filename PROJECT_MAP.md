# Carto - Project Map

Last updated: 2026-05-26

A collection of ArcGIS Python Toolboxes (`.pyt`) for cartographic automation.
Each plugin solves one problem and ships in two flavors:

- **ArcMap (Py2.7)** - hardened build for ArcMap 10.x.
- **Pro (native)**  - native ArcGIS Pro / Python 3 build using `arcpy.mp`.

Some plugins also keep an older "v3" or "fixedUIUX" reference build for history;
those are not part of active development.

## File inventory

### Plugin 02 - Road Deconflict
- `Plugin02_RoadDeconflict_ArcMap_v4_fixedUIUX_py27.pyt`  (legacy reference)
- `Plugin02_RoadDeconflict_ArcMap_v5_hardened.pyt`        (current ArcMap)
- `Plugin02_RoadDeconflict_Pro_v5_native.pyt`             (current Pro)

### Plugin 03 - Contour Label Optimizer
- `Plugin03_ContourLabelOptimizer_v3.pyt`                  (legacy reference)
- `Plugin03_ContourLabelOptimizer_ArcMap_v4_hardened.pyt`  (current ArcMap)
- `Plugin03_ContourLabelOptimizer_Pro_v4_native.pyt`       (current Pro)

### Plugin 04 - Elevation Text Deconflict
- `Plugin04.pyt`                                            (legacy reference)
- `Plugin04_ElevationTextDeconflict_ArcMap_v5_hardened.pyt` (current ArcMap)
- `Plugin04_ElevationTextDeconflict_Pro_v5_native.pyt`      (current Pro)

### Plugin 05 - Safe Contour Cleaner
- `Plugin05_SafeContourCleaner_ULTIMATE.pyt`               (legacy reference)
- `Plugin05_SafeContourCleaner_ArcMap_v5_hardened.pyt`     (current ArcMap)
- `Plugin05_SafeContourCleaner_Pro_v5_native.pyt`          (current Pro)

### Plugin 06 - Spring Rotation
- `Plugin06_SpringRotation_ArcMap_v4_hardened.pyt`         (current ArcMap, working)
- `Plugin06_SpringRotation_Pro_v4_native.pyt`              (current Pro, complete)

### Plugin 07 - Batch Grid Builder
- `Plugin07_BatchGridBuilder_ArcMap27_v5_EN_Full.pyt`      (legacy reference)
- `Plugin07_BatchGridBuilder_ArcMap_v6_hardened.pyt`       (current ArcMap)
- `Plugin07_BatchGridBuilder_Pro_v6_native.pyt`            (current Pro)

## Documentation

- `PROJECT_MAP.md`         - this file
- `ARCHITECTURE_SUMMARY.md`- shared design across plugins
- `MIGRATION_PLAN.md`      - how to take Py2.7 -> Pro
- `PLUGIN_06_STATUS.md`    - Plugin 6 specific status
- `UI_GUIDE.md`            - shared UI conventions

## Naming convention

```
Plugin<NN>_<Topic>_<Flavor>_<Version>_<Tag>.pyt

  NN       - 02..07 (each plugin is a topic)
  Topic    - PascalCase identifier (RoadDeconflict, SpringRotation, ...)
  Flavor   - "ArcMap" (Py2.7) or "Pro" (native Py3)
  Version  - vN matching the engineering generation
  Tag      - "hardened" (ArcMap) | "native" (Pro) | descriptive
```

The Pro and ArcMap files of the **same Plugin/version** must stay
behaviorally equivalent (same parameters, same defaults, same outputs).

## Repository layout

```
/projects/sandbox/Carto/
  README.md
  PROJECT_MAP.md
  ARCHITECTURE_SUMMARY.md
  MIGRATION_PLAN.md
  PLUGIN_06_STATUS.md
  UI_GUIDE.md
  Plugin02_*.pyt
  Plugin03_*.pyt
  Plugin04_*.pyt
  Plugin05_*.pyt
  Plugin06_*.pyt
  Plugin07_*.pyt
```

There is no Python package; each `.pyt` is self-contained so ArcGIS can load
it as a Python toolbox without setup. Shared conventions are documented, not
imported.

## What is "active" right now

| Plugin | ArcMap (Py2.7) | Pro (native) | Notes |
|--------|----------------|--------------|-------|
| 02     | v5 hardened    | v5 native    | OK |
| 03     | v4 hardened    | v4 native    | OK |
| 04     | v5 hardened    | v5 native    | OK |
| 05     | v5 hardened    | v5 native    | OK |
| 06     | v4 hardened    | v4 native    | Pro build added in Parts 3a/3b. UI verified parity with Py2.7. |
| 07     | v6 hardened    | v6 native    | OK |
