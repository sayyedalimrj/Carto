# Before / After QA Summary (generated at run time)

This file is a **placeholder**. The harness overwrites it (and writes
`BEFORE_AFTER_SUMMARY.csv`) into each run's `<run_dir>/reports/` folder after
executing the plugins.

For every plugin run the harness captures a snapshot before and after, with:

* feature count
* geometry summary + `CheckGeometry` validity
* field schema
* spatial extent and spatial-reference check (expects WKID 32638)
* important field statistics (e.g. rotation/angle min/max/mean)
* conflict counts (Plugins 02/04/05) before vs after
* angle statistics (Plugins 01/06)
* distance statistics where relevant

Snapshots are also copied to `<run_dir>/snapshots_before/snapshots.gdb` and
`<run_dir>/snapshots_after/snapshots.gdb` so you can open the actual before/after
features in ArcMap/Pro.

Example shape of a row (illustrative):

| Plugin | Layer | Phase | Count | Conflicts | Angle stats | Distance stats |
|--------|-------|-------|------:|-----------|-------------|----------------|
| Plugin02 | conflicts_before | before | 1234 | 87 | | |
| Plugin02 | targets_after | after | 1234 | 0 | | |
| Plugin06 | spring_before | before | 42 | | | |
| Plugin06 | spring_after | after | 42 | | n=42 min=0.0 max=359.4 mean=181.2 | |
