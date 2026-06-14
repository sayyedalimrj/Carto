# Carto Plugin Test Suite - Summary (generated at run time)

This file is a **placeholder**. Running the suite produces the real summary in
`<run_dir>/reports/ALL_TEST_SUMMARY.md`, alongside `ALL_TEST_RESULTS.json` and
`ALL_TEST_RESULTS.csv`.

The generated summary contains:

* total test-case count and a PASS / FAIL / WARN / SKIP tally,
* a per-plugin breakdown table,
* a detail table (one row per test case) with before/after/changed counts and a
  short note.

## How to read statuses

| Status | Meaning |
|--------|---------|
| PASS | Tool ran and all assertions for the case were satisfied. |
| FAIL | Assertion failed, or the tool crashed unexpectedly. See `error_message`/`traceback`/`arcpy_messages`. |
| WARN | Tool produced output but a soft check was not met (e.g. no measurable conflict reduction, angle slightly out of band). |
| SKIP | Pre-conditions not met (missing layer/field, or environment cannot run the tool, e.g. Plugin 07 with no active map). `skip_reason` explains why. |

The per-test schema is documented in `common/test_schema.json`.
