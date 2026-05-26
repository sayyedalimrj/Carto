# Carto - Architecture Summary

Last updated: 2026-05-26

## Goal

Each plugin is a single-file ArcGIS Python Toolbox (`.pyt`) that:

1. Loads in ArcMap or ArcGIS Pro with **zero install** (just drop the file in).
2. Exposes one or more geoprocessing tools with rich UI parameters.
3. Runs deterministic, observable cartographic logic with stage-by-stage
   `[DIAG]` messages.
4. Adds its outputs to the active map.

## Two flavors per plugin

The same logic is implemented twice on purpose.

### ArcMap flavor (Py2.7, "hardened")

- Filename suffix: `..._ArcMap_v<N>_hardened.pyt`
- `from __future__ import division` for safe division.
- Defensive `bytes/str` handling for hashlib (`_to_bytes_utf8`).
- Map integration tries `arcpy.mp` first, falls back to `arcpy.mapping`.
- Compatible with the ArcMap 10.x 32-bit `arcpy` runtime.

### Pro flavor (Py3 native)

- Filename suffix: `..._Pro_v<N>_native.pyt`
- `from __future__ import annotations`, type hints, f-strings.
- Map integration is `arcpy.mp` only - no `arcpy.mapping`.
- `arcpy.CreateUniqueName(...)` for scratch staging.
- `arcpy.management.*` / `arcpy.analysis.*` namespaces consistently.

Both flavors of the same plugin/version must have:

- The **same tool labels and parameter order**.
- The **same defaults**.
- The **same output schemas** (field names, types).
- The **same `[DIAG]` messages** so operators can compare runs side by side.

## Shared design rules

These rules show up in every current plugin file. New plugins must follow
them; new flavors of an existing plugin must preserve them.

### 1. Selection bypass (the "selection trap")

When a feature *layer* with an active selection is passed to a tool, ArcGIS
silently restricts processing to the selection. We make this visible and
opt-out:

```python
def _selection_info(layer):  # -> (has_selection, n_selected) or (sel, total, name)
    ...
def _resolve_full_source(layer):  # -> on-disk catalogPath when present
    ...
```

Every tool exposes a `Process ALL features (ignore any active selection)`
boolean (default `True`) and routes inputs through `_resolve_full_source`
when it is on. A `[DIAG]` message announces the choice.

### 2. Scratch GDB staging

All intermediate datasets land in `arcpy.env.scratchGDB`. Pro builds use
`arcpy.CreateUniqueName(_unique(prefix), arcpy.env.scratchGDB)` to avoid
name collisions. ArcMap builds use stable named temps (`spr_tmp_in`,
`con_tmp_proj`, ...) plus `_safe_delete()` before write.

### 3. Stage-by-stage `[DIAG]` logging

Every tool prints feature counts after every stage that can lose features:
input -> selection -> AOI -> Near -> rotated/cleaned. This makes silent
filtering impossible.

### 4. Working SR auto-pick (where geometry math matters)

Tools that need planar math (Plugin 06 in particular) auto-pick a UTM zone
from the input centroid (`_pick_work_sr`) unless the user forces
`USE_INPUT`.

### 5. UI categories

Parameters are grouped with `param.category = "<NN Group>"`. The first
digit-prefixed token controls dialog order. Plugin 06 categories:

```
01 Inputs
02 Outputs
03 Advanced
04 Processing
05 Symbology
06 AOI
07 Performance
08 Methods
```

Plugin 05 uses different category labels but the same numeric-prefix idiom.

### 6. Validation in `updateParameters` / `updateMessages`

- `updateParameters` only enables/disables UI fields based on choices.
- `updateMessages` validates types, ranges, GDB existence, output names.
- Live selection warnings appear on the input layer parameters.

### 7. Map integration

ArcMap (`mapping.MapDocument("CURRENT")`) and Pro (`arcpy.mp.ArcGISProject("CURRENT")`)
are isolated in one helper, e.g. `_add_to_current_map(...)`.

### 8. No external dependencies

Only `arcpy` and the standard library. Each `.pyt` is self-contained so the
toolbox loader sees one file.

## Pipeline pattern (used by all the spatial pipelines)

```
inputs -> resolve full source -> copy to scratch -> AOI clip ->
project to working SR -> Near / GenerateNearTable -> per-method math ->
write outputs (separate or single fields) -> optional summary ->
add to current map
```

Plugin 06 is the canonical example. Plugins 02/04/05/07 are variants of the
same shape with different math in the middle.

## Output conventions

- Output workspace must be a `*.gdb` (validated in `updateMessages`).
- Output base name passes through `arcpy.ValidateTableName(...)`.
- Per-method outputs get a code suffix: `<base>_01_NearTangent`, etc.
- Single-fields layout uses field-name suffixes: `ROT_01_NearTangent`.
- Summary tables use schema:
  `METHOD TEXT(40), N_TOTAL LONG, N_OK LONG, N_FAIL LONG, OK_PCT DOUBLE`.

## How a Pro version is derived from an ArcMap version

See `MIGRATION_PLAN.md` for the step-by-step recipe. In short: keep the
same public surface (tool class name, parameters, defaults, output schema),
swap helpers to Py3 idioms, replace `arcpy.mapping` with `arcpy.mp`.
