# Carto — Documentation Overview (English)

> The **complete, beginner-friendly documentation set is written in Persian** under
> [`docs/fa/`](../../fa/overview/00_Carto_Overview_FA.md). This English page is a concise
> map so non-Persian readers can navigate. Per-plugin English guides are planned; until
> then, the Persian guides are the authoritative source.

---

## What is Carto?

Carto is a set of **ArcGIS Python Toolboxes** (`.pyt` files) that automate repetitive
cartographic tasks. Each plugin solves one problem and works with **zero install** — just
add the `.pyt` file to ArcToolbox (ArcMap) or Catalog (ArcGIS Pro).

## ArcMap vs ArcGIS Pro builds

Every plugin ships in **two flavors** with identical parameters, defaults, and outputs:

| | ArcMap flavor | Pro flavor |
|--|--------------|------------|
| Target | ArcMap 10.x | ArcGIS Pro |
| Python | 2.7 | 3 |
| Filename | `..._ArcMap_..._hardened.pyt` / `..._ArcMap_py27.pyt` | `..._Pro_..._native.pyt` / `..._Pro_py3.pyt` |
| Map integration | `arcpy.mp`, falls back to `arcpy.mapping` | `arcpy.mp` only |

See `ARCHITECTURE_SUMMARY.md` (repo root) for the shared design rules.

## Plugin index

| # | Plugin | What it does | Full guide (Persian) |
|:--:|--------|--------------|----------------------|
| 01 | Bridge & Culvert | Place/rotate bridge & culvert symbols at road×drainage crossings | [FA](../../fa/plugins/Plugin01_BridgeCulvert_FA.md) |
| 02 | Road Deconflict | Push nearby features away from roads to enforce a clearance | [FA](../../fa/plugins/Plugin02_RoadDeconflict_FA.md) |
| 03 | Contour Label Optimizer | Pick the best anchor point for contour labels | [FA](../../fa/plugins/Plugin03_ContourLabelOptimizer_FA.md) |
| 04 | Elevation Text Deconflict | Move elevation text off obstacles | [FA](../../fa/plugins/Plugin04_ElevationTextDeconflict_FA.md) |
| 05 | Safe Contour Cleaner | Safely remove dense contours within an AOI | [FA](../../fa/plugins/Plugin05_SafeContourCleaner_FA.md) |
| 06 | Spring Rotation | Compute downhill-facing rotation for spring symbols | [FA](../../fa/plugins/Plugin06_SpringRotation_FA.md) |
| 07 | Batch Grid Builder | Batch-build grids/graticules across sheets | [FA](../../fa/plugins/Plugin07_BatchGridBuilder_FA.md) |

## Repository structure

- `docs/fa/` — full Persian documentation (overview, per-plugin guides, glossary, troubleshooting, FAQ, migration, status, template).
- `docs/en/` — this English summary.
- `organized/` — tidy copies of each plugin grouped as `PluginNN/{ArcMap,Pro,Legacy}` plus `Master/`.
- Root `.pyt` files — the original, untouched toolboxes (kept in place so the Master toolboxes can load them).

## Which tool should I use?

- Symbols where a road crosses a river/canal → **Plugin 01**
- Features too close to roads → **Plugin 02**
- Best placement for contour labels → **Plugin 03**
- Elevation text overlapping other features → **Plugin 04**
- Dense contours cluttering a print → **Plugin 05**
- Spring symbols should face downhill → **Plugin 06**
- Coordinate grids/graticules across many sheets → **Plugin 07**

Then pick the **ArcMap** or **Pro** file based on your software.
