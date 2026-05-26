# Carto - Migration Plan (Py2.7 ArcMap -> Pro Native)

Last updated: 2026-05-26

The reusable recipe for porting any Plugin's `..._ArcMap_v<N>_hardened.pyt`
to `..._Pro_v<N>_native.pyt`. Validated end-to-end on Plugin 6.

> **Cardinal rule**: behavior parity first, idiom modernization second.
> Same parameters, same defaults, same outputs, same `[DIAG]` messages.

## Step 0 - Inspect first (do not write yet)

Read in this order; stop reading once you have what you need:

1. The Py2.7 source: top-level docstring, helper utility surface, then
   the `Toolbox` and the single Tool class (parameter list + `execute`).
2. One **already-ported** Pro file from a sibling plugin (e.g. Plugin 05
   Pro). This is your idiom reference for: `_msg/_warn/_err/_diag`,
   `_unique`, `_scratch_unique`, `_ensure_scratch`, `_add_to_current_map`.
3. `PLUGIN_<NN>_STATUS.md` if it exists. If not, create it with at minimum:
   - Working/Missing matrix
   - Pipeline (numbered)
   - Parameter index map
   - Output schema
   - Pro must-preserve list
   - Pro should-change list

You should now be able to migrate without rereading the Py2.7 file.

## Step 1 - Plan the file in numbered sections

Adopt the same section banner layout used across the Pro plugins:

```
0  Messaging + small utilities
1  Describe / SR helpers
2  Selection-bypass
3  Pro map integration
4  AOI helpers (if applicable)
5  Near + geometry helpers (if applicable)
6  Domain methods
7  Output writers
8  Main runner (_run_suite or equivalent)
9  Python Toolbox (Pro UI)
```

This makes diffs reviewable and lets you split work into micro-parts.

## Step 2 - Split into micro-parts

Default split:

- **3a**: sections 0..8 (helpers + runner). File parses but has no
  `Toolbox` class yet -- this is fine.
- **3b**: section 9 only (Toolbox + Tool class). File now loads in Pro.
- Optional **3c**: extra UI/UX polish if requested.

Rules:

- Each micro-part is one response.
- Update doc files only at the end of a micro-part where the working
  state changed meaningfully (not after every tool call).
- Never start the next micro-part by re-reading the same Py2.7 source --
  rely on the index map and pipeline you captured in Step 0.

## Step 3 - Port runtime logic safely (Part 3a recipe)

For every helper and method in the Py2.7 file, copy the **logic**
verbatim and apply only these mechanical edits:

| Py2.7 idiom | Pro replacement |
|---|---|
| `from __future__ import division` | remove |
| `print "..."` (none here, but watch for it) | `_msg(...)` |
| `arcpy.AddMessage("[PROFILE] {0}: {1:.3f}s".format(...))` | f-string |
| `_to_bytes_utf8(s)` only for hashlib | keep as-is |
| `os.path.join(scratch, "fixed_name")` then `_safe_delete` | `arcpy.CreateUniqueName(_unique(prefix), arcpy.env.scratchGDB)` via `_scratch_unique` |
| `expr_mode = "PYTHON3" if hasattr(arcpy, "mp") else "PYTHON_9.3"` | `"PYTHON3"` unconditionally |
| `import arcpy.mapping` fallback | delete; use `arcpy.mp` only |
| `mapping.AddLayer(df, lyr, "TOP")` + `ApplySymbologyFromLayer` dance | `m.addDataFromPath(path)` then optional `ApplySymbologyFromLayer` |
| Plain `def f(x, y):` | add `typing` annotations on signatures |
| `dict.items()` returning lists | fine in Py3; no change |
| `map`, `filter` returning lists | wrap in `list(...)` only if a list is required |
| `xrange` | `range` |
| `unicode` | `str` |

Things to NOT change in Part 3a:

- Method math, constants, thresholds, control flow.
- Field names, field types, field lengths in output schema.
- Order of operations in `_run_suite`.
- `[DIAG]` message wording (operators search logs for these strings).
- Parameter names that the runner consumes by keyword.

End-of-3a checks:

```bash
python3 -c "import ast; ast.parse(open('<file>.pyt').read()); print('PARSE OK')"
```

The file is intentionally not loadable in Pro yet (no `Toolbox` class).
That is the boundary between 3a and 3b.

## Step 4 - Port the UI classes (Part 3b recipe)

In one append:

1. Add a `# Parameter index map` comment block listing every parameter,
   its datatype, default, ValueList (if any), and category. Same indices
   as Py2.7 -- do not renumber.
2. Add `_set_category` and `_make_aoi_featureset_schema` (if Py2.7 had
   one) verbatim, with type hints.
3. Add `class Toolbox(object):` with the same `tools = [...]` list. The
   `label`/`alias` should clearly say "(Pro v<N> native)".
4. Add the Tool class with the **same class name** as Py2.7 so saved
   tool history keeps working.
5. In `getParameterInfo`, build parameters in the exact same order. Use
   the same `name=`, `displayName=`, `datatype=`, `parameterType=`,
   `direction=`, defaults, and `filter.list` values.
6. In `updateParameters` and `updateMessages`, copy the Py2.7 logic 1:1.
   Convert `.format(...)` to f-strings; keep guard `try/except` blocks.
7. In `execute`, read parameters by index in the same order. Wrap the
   `_run_suite` call in `try/except` and `_err(traceback.format_exc())`
   on failure. Write the derived `outputs` parameter at the end.

End-of-3b checks:

```bash
python3 -c "import ast; ast.parse(open('<file>.pyt').read()); print('PARSE OK')"
grep -c "arcpy.Parameter(" <file>.pyt   # must equal Py2.7 count
grep -E "^class " <file>.pyt            # Toolbox + same Tool class name
```

## Step 5 - Verify parity (no Pro install needed in CI)

Static checks that catch ~all regressions:

1. `python3 -m ast` parses the file.
2. `arcpy.Parameter(` count matches Py2.7.
3. Tool class name matches Py2.7.
4. Method functions present: every `_method_*` symbol in Py2.7 exists
   in the Pro file with the same signature shape.
5. Output schema strings match: search both files for the literal
   field names (`"ROT"`, `"OK"`, `"NOTE"`, `"SPR_TMPID"`, etc.).
6. `[DIAG]` strings match (grep both files for `\[DIAG\]`).

When a Pro install is available (out-of-band):

7. Open the `.pyt` in Pro -- toolbox and tool show up.
8. Open the tool dialog -- categories appear in the same order with the
   same parameters and defaults.
9. Run on a small fixture; compare `ROT` values to the Py2.7 build
   within floating tolerance.
10. Run with an active selection on the input layer plus
    `Process ALL = True` -- full dataset is processed.

## Step 6 - Update docs as source of truth

After the migration is complete:

- `PROJECT_MAP.md`: flip the inventory entry from "MISSING" to
  "current Pro, complete".
- `PLUGIN_<NN>_STATUS.md`:
  - Update the file table.
  - Add a **Pro file structure** section (the numbered banners).
  - Add a **Pro vs Py2.7 parity** subsection listing the static-check
    results from Step 5.
  - Add a **Pro-only changes** subsection.
- `ARCHITECTURE_SUMMARY.md`: only touch if a new shared rule was
  discovered during the port. Otherwise leave it alone.
- `MIGRATION_PLAN.md`: this file. Edit only if the recipe itself
  changed (e.g. a new Pro idiom became standard).

Doc rule: each document has one job. Do not duplicate the parameter
index map across files -- it lives in `PLUGIN_<NN>_STATUS.md`.

## Step 7 - Splitting bigger plugins

If `_run_suite` is huge or there are multiple Tool classes (like
Plugin 05's `AOIBrushBuilder` + `SafeContourCleaner`), split further:

- 3a-i: sections 0..3 (messaging + helpers + selection bypass + map).
- 3a-ii: sections 4..7 (domain helpers + methods + writers).
- 3a-iii: section 8 (`_run_suite`).
- 3b-i: Toolbox + first Tool.
- 3b-ii: second Tool.

Each micro-part still ends with the parse check and a one-line status
update in `PLUGIN_<NN>_STATUS.md`.

## Common pitfalls (seen during Plugin 6 port)

- **Renumbering parameters** breaks saved tool history. Insert new
  toggles at the end or keep the exact original index.
- **`scratchGDB` name collisions** if you keep Py2.7's stable temp
  names (`spr_tmp_in`, etc.) and run two tools at once -- prefer
  `_scratch_unique` for everything in Pro.
- **`CalculateField` expression type** must be `"PYTHON3"` in Pro;
  Py2.7's conditional fallback to `"PYTHON_9.3"` is wrong on Pro.
- **`arcpy.mapping`** does not exist in Pro -- importing it at module
  top will break tool discovery. Keep all `arcpy.mp` calls inside
  function bodies, never at import time.
- **AOI `GPFeatureRecordSetLayer` schema FC** must be created at
  `getParameterInfo` time. Wrap creation in `try/except` so the tool
  still loads when scratchGDB is unavailable.
- **Selection-trap toggle name** (`ignore_selection`) and default
  (`True`) are part of the public contract -- do not rename or flip.

## Migration done; what next

Once a Plugin's Pro build passes Step 5:

1. Mark it green in `PROJECT_MAP.md`.
2. Move on to the **next-smallest** task in the master plan
   (typically: UI polish or the next plugin's port).
3. Resist the urge to refactor shared helpers into a common module --
   each `.pyt` must remain self-contained per `ARCHITECTURE_SUMMARY.md`.
