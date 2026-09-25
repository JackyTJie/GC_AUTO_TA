# Report and Writeback Separation Implementation Plan

> **For agentic workers:** Execute this plan task-by-task with verification after each behavior change.

**Goal:** Make the main pipeline stop after generating the report, while keeping Canvas writeback as a separate state-backed command.

**Architecture:** `run` owns `pull` through `report` only. `writeback` remains an independent command and continues to build its input from SQLite state, preserving existing grade conversion, dry/live behavior, and audit records.

**Tech Stack:** Python 3, argparse, SQLite state store, existing smoke test script, Markdown documentation.

## Global Constraints

- `run` must never invoke `writeback`.
- `run --live` must no longer be accepted because `run` has no Canvas mutation stage.
- `writeback` must continue to read from the state database, not report files.
- No Canvas/Gitea integration behavior changes outside the command boundary.

### Task 1: CLI boundary

**Files:**
- Modify: `gc_auto_ta/cli.py`
- Test: `tests/test_smoke.py`

- [x] Add failing tests for stopping after `report` and rejecting `run --live`.
- [x] Remove `writeback` from the default `run` stages and constrain `--stages` to pre-writeback stages.
- [x] Remove `run --live`, writeback-only options, and stale writeback continuation handling.
- [x] Run the smoke test and verify the new CLI tests pass.

### Task 2: User-facing commands and documentation

**Files:**
- Modify: `Makefile`
- Modify: `README.md`
- Modify: `docs/07-operations.md`
- Modify: `docs/01-architecture.md`

- [x] Describe `run` as ending at report generation.
- [x] Document separate `report` then `writeback --live` execution.
- [x] Remove the `run-live` target and one-command writeback claims.
- [x] Run help output and the smoke test again.
