# Carto - UI Guide

Last updated: 2026-05-26

How tool dialogs are structured across the Carto plugins, and the rules for
keeping the ArcMap (Py2.7) and Pro (native) flavors of the same plugin
visually identical.

## Why parity matters

A user moving a saved tool history or a documented workflow from ArcMap to
Pro should see the **same dialog**: same categories in the same order, same
parameter labels, same defaults, same dropdown options. Mismatches break
muscle memory and break automation that reads parameters by index.

The parity bar is enforced statically (no Pro install needed):

- 27 / 27 parameters present in both flavors
- 9 / 9 attributes per parameter agree (`displayName`, `name`, `datatype`,
  `parameterType`, `direction`, `category`, `value`, `filter.list`,
  `parameterDependencies`)
- ValueList dropdowns are byte-identical
- Output schema field names are present in both files
- Tool class names are byte-identical so saved tool history works

See the **Final parity report (Plugin 6)** at the bottom for the actual
numbers from the most recent run.

## Category convention

Every Tool's parameters are grouped with `param.category = "<NN Group>"`.
The two-digit prefix controls dialog order. Plugin 6 uses:

```
01 Inputs       0..3   springs, contours, elev_field, ignore_selection
02 Outputs      4..7   out_gdb, out_base_name, output_mode, create_summary
                26     outputs  (derived)
03 Advanced     14..16 global_offset, k_near, tangent_step
04 Processing   11..13 work_sr_mode, near_method, search_radius
05 Symbology    8..10  sym_layer, auto_symbology, symbol_size
06 AOI          17..18 aoi_sketch, aoi_buffer
07 Performance  19..20 cache_mode, profile
08 Methods      21..25 run_01..run_05
```

The category numbers do **not** have to match parameter indices -- they
are independent. Indices are stable across versions for tool-history
compatibility; categories control visual grouping.

Plugin 5 uses different category labels (`1) Brush Inputs`, etc.) but the
same numeric-prefix idiom. New plugins should follow whichever pattern
fits the domain better, and document it in their `PLUGIN_<NN>_STATUS.md`.

## Parameter conventions

### Naming
- `name` is `snake_case`, stable across versions.
- `displayName` is human-readable; use sentence case, no trailing period.
- ValueList tokens are `UPPER_SNAKE_CASE` (e.g. `AUTO_UTM`, `NEAR_ONLY`).

### Required fields
- Inputs that are essential for the algorithm: `parameterType="Required"`.
- Everything else: `parameterType="Optional"` with a sensible `value`.
- Derived outputs: `parameterType="Derived"`, `direction="Output"`.

### Defaults that are part of the public contract
| Plugin | Parameter | Default | Why |
|---|---|---|---|
| 06 | `ignore_selection` | `True` | Selection-trap fix is opt-out, not opt-in |
| 06 | `output_mode` | `SEPARATE_LAYERS` | Easier to symbolize per-method outputs |
| 06 | `work_sr_mode` | `AUTO_UTM` | Planar math needs meters |
| 06 | `near_method` | `PLANAR` | Pairs with AUTO_UTM |
| 06 | `cache_mode` | `NEAR_ONLY` | Bounds memory on huge sheets |
| 06 | `k_near` | `8` | Empirically good for 02/04/05 |
| 06 | `tangent_step` | `5.0` | Empirically good for 01 |
| 06 | `run_01` | `True` | NearTangent is the recommended primary |
| 06 | `run_02` | `True` | HighLow is the recommended secondary |
| 06 | `run_03..run_05` | `False` | Off by default; users opt in |

Changing any of these in either flavor requires changing both flavors and
bumping the plugin version.

### Field type for elevation / geometry-typed inputs

`elev_field` uses `datatype="Field"` with `parameterDependencies=["contours"]`
so the dropdown auto-populates from the contour layer's fields.
`updateMessages` enforces numeric-only via `_field_is_numeric`.

### AOI sketch parameter

The AOI input is a `GPFeatureRecordSetLayer` with a polygon FeatureSet
schema pre-loaded in `getParameterInfo`. Pro and ArcMap both call this
parameter `aoi_sketch` and label it `"AOI sketch (polygon)"` (aligned in
this round of UI work).

## updateParameters / updateMessages rules

- `updateParameters` only enables/disables UI fields. It must NOT change
  `value` (that surprises the user).
- `updateMessages` validates types, ranges, GDB existence, and surfaces
  active selection on input layers as a non-blocking warning. It must
  NOT raise -- always wrap in `try/except`.

Plugin 6 enables/disables based on:

```
sym_layer set?           -> auto_symbology, symbol_size disabled
run_02 or run_04 or run_05? -> k_near enabled
run_01?                  -> tangent_step, cache_mode enabled
aoi_sketch set?          -> aoi_buffer enabled
```

## Map integration

ArcMap (Py2.7):
- Tries `arcpy.mp.ArcGISProject("CURRENT")` first (handles people running
  the toolbox in Pro by accident).
- Falls back to `arcpy.mapping.MapDocument("CURRENT")`.
- `_add_to_current_map(...)` lives in the same file.

Pro (native):
- `arcpy.mp` only. No `arcpy.mapping` import anywhere (would break tool
  discovery in Pro).
- Adds via `m.addDataFromPath(path)` and applies optional symbology.

The function name (`_add_to_current_map`) is the same in both flavors.

## Symbology UI

Two paths:

1. **Symbology template layer** (`sym_layer`, optional): if set, the tool
   calls `ApplySymbologyFromLayer` on the new output. `auto_symbology`
   and `symbol_size` are disabled in this mode.
2. **Auto-symbology** (`auto_symbology=True`, default): the tool tries
   to set rotation field to `ROT`, plus a simple symbol size and a
   per-method color derived from a hash of the method code.

The Pro auto-symbology path is best-effort -- different renderer types
expose different APIs. Failures are silent.

## Diagnostics UI

Every tool prints:
- `Springs available for processing: N` -- after selection bypass
- `[DIAG] springs after copy: N`
- `[DIAG] springs inside AOI: N` (when AOI used)
- `[DIAG] contours used: N`
- `[DIAG] springs with a NEAR contour: N / total`
- `[DIAG] <method>: rotated=A  OK=B  of N` -- one line per method
- `[PROFILE] <stage>: <s>` when "Profiling messages" is on

These strings are **part of the public contract** -- operators search logs
for them. Changing wording requires changing both flavors and updating
`PLUGIN_<NN>_STATUS.md`.

## Final parity report (Plugin 6)

Run after the UI alignment pass:

```
Files
  Plugin06_SpringRotation_ArcMap_v4_hardened.pyt   PARSE OK
  Plugin06_SpringRotation_Pro_v4_native.pyt        PARSE OK

Static parameter parity (AST-level)
  Py2.7 params: 27
  Pro   params: 27
  Mismatches across (displayName, name, datatype, parameterType,
                     direction, category, value, filter.list,
                     parameterDependencies):  0   (243 checks)

Helper / method symbols
  16 / 16 present in both files

Output schema field literals
  9 / 9 ("SPR_TMPID", "ROT", "OK", "NOTE",
         "METHOD", "N_TOTAL", "N_OK", "N_FAIL", "OK_PCT")

DIAG phrases
  4 / 4 key phrases present
  ("springs after copy", "springs inside AOI",
   "contours used", "springs with a NEAR contour")

ValueList dropdowns
  ["SEPARATE_LAYERS", "SINGLE_FIELDS"]   match
  ["AUTO_UTM", "USE_INPUT"]              match
  ["PLANAR", "GEODESIC"]                 match
  ["NEAR_ONLY", "ALL"]                   match

Class names
  class Toolbox(object):                          match
  class SpringRotationFinalSuiteTool(object):     match
```

## Adding a new parameter to an existing plugin

1. Decide the index. Insert at the **end** of the input parameter list to
   preserve tool history compatibility, even if it visually belongs in an
   earlier category. Categories handle visual order; indices handle
   history.
2. Add it to the Py2.7 file first, then mirror to the Pro file. Run the
   AST parity script (template in `MIGRATION_PLAN.md` Step 5) to
   confirm 0 mismatches.
3. Update `PLUGIN_<NN>_STATUS.md` parameter index map.
4. If the new parameter changes the output schema, update the schema
   table in `PLUGIN_<NN>_STATUS.md` and the schema string list above.

## Renaming a label

Allowed: change `displayName` in lockstep on both flavors, leave `name`
alone. Add a one-line note to `PLUGIN_<NN>_STATUS.md` so the change is
discoverable.

Not allowed without a version bump: change `name`, change parameter
order, change `datatype`, change `filter.list`, change category numeric
prefix.
