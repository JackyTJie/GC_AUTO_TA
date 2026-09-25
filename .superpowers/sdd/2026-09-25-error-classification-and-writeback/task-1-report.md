# Task 1 Report

## Changed files

- `gc_auto_ta/state.py`
  - Added the `stages.error_kind` column with an empty-string default.
  - Added an idempotent additive migration for existing SQLite databases.
  - Extended `set_stage` with the backward-compatible `error_kind=""` argument.
  - Normalized unknown category values to empty and added `first_student_error` and
    `first_system_error` queries in pipeline order.
  - Preserved `first_failed_stage` behavior for all failed rows, including legacy
    rows without a category.
- `tests/test_smoke.py`
  - Added coverage for opening an old-schema database, preserving existing student
    and stage data, category-specific queries, unknown categories, and ordering.

## Tests

- `python tests\\test_smoke.py` — passed, 75 checks and 0 failures.
- `python -m compileall -q gc_auto_ta tests` — passed.

## Concerns

- Existing failed rows have no category after migration by design. They remain
  visible through `first_failed_stage` but do not count as either a student or
  system error until a later stage update explicitly classifies them.
- `error_kind` is normalized at the state API boundary rather than enforced with a
  SQLite `CHECK` constraint, preserving compatibility with existing databases and
  callers.
