# Error Classification and Safe Writeback Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Distinguish student-caused submission errors from system/infrastructure errors so only the former skip the remaining grading stages and receive a 0-point Canvas overwrite.

**Architecture:** Add an explicit error category to per-student stage state and propagate it through `pull`, `unpack`, `provision`, and `grade`. A student error causes later stages to record `skipped`; a system error is recorded and reported without being converted into a grade. `writeback` uses only the student-error marker to force a zero, while normal evaluator results of zero remain ordinary scores.

**Tech Stack:** Python 3, SQLite state store, Canvas API wrapper, Gitea API wrapper, existing smoke-test harness.

**Spec:** The approved error-classification requirements in the current task conversation.

**Global Constraints**

- Student-caused errors must follow: mark error → skip remaining stages → overwrite Canvas with 0.
- System, network, permission, Canvas, Gitea, runner, and local infrastructure errors are reported only and must not generate a 0 overwrite.
- A JOJ evaluation result of score 0 is a valid grading result, not a pipeline error.
- `dry` mode must never submit a Canvas grade.
- Existing state databases must remain readable; new error metadata must be backward-compatible with missing values.
- Use the existing `State`, pipeline stage, and smoke-test patterns; do not introduce a parallel persistence system.

---

### Task 1: Define backward-compatible error categories in state

**Files:**
- Modify: `gc_auto_ta/state.py` near `stages` schema and `set_stage`
- Test: `tests/test_smoke.py`

**Interfaces:**
- Extend stage records with an optional `error_kind` value: `student`, `system`, or empty for ordinary statuses.
- Preserve existing `set_stage(assignment, student_key, stage, status, detail)` callers by adding an optional `error_kind` argument with a default of `""`.
- Add `first_student_error(...)` and `first_system_error(...)` queries that return `(stage, detail)` or `None` in pipeline order.

- [ ] **Step 1: Write failing state tests**

  Add tests that write one `student` error and one `system` error, retrieve each category independently, and verify old stage rows without a category remain readable.

- [ ] **Step 2: Run the focused test and confirm failure**

  Run: `python tests\test_smoke.py`

  Expected: the new state-category assertions fail because stage rows do not yet store or query `error_kind`.

- [ ] **Step 3: Implement the state migration**

  Add the nullable/defaulted column using an idempotent migration after schema creation. Update `set_stage` and add category-specific queries. Treat missing or unknown values as empty rather than student errors.

- [ ] **Step 4: Run the focused test and confirm it passes**

  Run: `python tests\test_smoke.py`

  Expected: the new state-category assertions pass and existing state tests remain green.

- [ ] **Step 5: Review compatibility**

  Open an existing temporary SQLite database created with the old schema, initialize `State`, and verify the migration does not delete students, scores, runs, or writebacks.

### Task 2: Classify pull and unpack failures

**Files:**
- Modify: `gc_auto_ta/canvas_api.py` in `download`
- Modify: `gc_auto_ta/pipeline.py` in `pull` and `unpack_stage`
- Test: `tests/test_smoke.py`

**Interfaces:**
- `CanvasGateway.download` must distinguish a failed attachment/API download from a valid empty or unsupported submission. Return an explicit failure indicator without treating a transport failure as a student error.
- `pull` records download/API failures as `pull/system`; malformed submission metadata/content that can be attributed to the submitted artifact is `pull/student`.
- `unpack_stage` records corrupt archives, unsafe archive entries, and invalid submitted file layouts as `unpack/student`; filesystem/read/write failures are `unpack/system`.

- [ ] **Step 1: Add failing classification tests**

  Cover: corrupt zip → `unpack/student`; attachment download exception → `pull/system`; unsupported or empty submission → no system error; and a missing local artifact caused by the manifest → `unpack/system` unless the submission itself is explicitly identified as invalid.

- [ ] **Step 2: Run the tests and verify the expected failures**

  Run: `python tests\test_smoke.py`

  Expected: the tests fail because current pull/unpack handling only records `failed` or `skipped` without distinguishing cause.

- [ ] **Step 3: Implement classification at the source**

  Make `download` expose whether the failure came from the Canvas/API operation. In `pull`, catch student-level exceptions, persist the student and stage category, clear stale manifest file references, and continue other students. In `unpack_stage`, map archive/content validation failures to `student` and local I/O failures to `system`.

- [ ] **Step 4: Run the tests and verify the classifications**

  Run: `python tests\test_smoke.py`

  Expected: all pull/unpack category assertions pass.

### Task 3: Propagate student errors and report system errors through provision and grade

**Files:**
- Modify: `gc_auto_ta/provision.py`
- Modify: `gc_auto_ta/grading.py`
- Modify: `gc_auto_ta/cli.py` only if exit/report behavior needs to expose categorized failures
- Test: `tests/test_smoke.py`

**Interfaces:**
- Before provisioning, query `first_student_error` and `first_system_error` for `pull` and `unpack`.
- Student error: record `provision=skipped` with the propagated cause; do not call Gitea.
- System error: record/report the system failure but do not create a student-zero decision; do not continue student grading when the prerequisite is unavailable.
- The shared `config_repo` setup must be wrapped separately: a shared infrastructure failure is reported as system error and must not be assigned to students as student faults.
- Before grading, repeat the same distinction for `pull`, `unpack`, and `provision`; a normal JOJ score of `0` must still call `save_score(..., score=0)` and mark `grade=done`.

- [ ] **Step 1: Add failing propagation tests**

  Cover: pull student error skips provision and grade; unpack student error skips provision and grade; Gitea push failure is system-only; config-repo failure is system-only; and a JOJ result containing score 0 is stored as a valid score.

- [ ] **Step 2: Run the tests and verify they fail**

  Run: `python tests\test_smoke.py`

  Expected: current code either treats all failures alike or lacks the category needed by the assertions.

- [ ] **Step 3: Implement propagation and shared-failure handling**

  Add category-aware guards before per-student work. Keep per-student exceptions isolated. Catch shared config-repo setup once, report it as system failure, and return categorized results without pretending any student failed because of their submission.

- [ ] **Step 4: Run the tests and verify they pass**

  Run: `python tests\test_smoke.py`

  Expected: all propagation and valid-zero assertions pass.

### Task 4: Make writeback zero only for student errors

**Files:**
- Modify: `gc_auto_ta/writeback.py`
- Modify: `gc_auto_ta/canvas_api.py` only if bulk result details need normalization
- Test: `tests/test_smoke.py`

**Interfaces:**
- `writeback.run` checks `first_student_error` across `pull`, `unpack`, `provision`, and `grade`.
- Student error with a missing or stale prior score: build a 0-point payload and preserve `previous_score` for audit.
- System error: return/report an error and do not add a grade payload.
- `uid is None`, Canvas initialization failure, anonymous-ID failure, single-submit failure, bulk-submit exception, and failed bulk progress must all be system errors.
- For bulk writes, only mark writebacks done when the Canvas progress state is successful; failed or unknown progress must produce system error results and `writeback=failed` records.
- In anonymous mode, missing anonymous IDs must stop that student from being submitted through the ordinary user endpoint.
- `dry` mode must still build/log the same student-error zero payload but must not call any live Canvas mutation.

- [ ] **Step 1: Add failing writeback tests**

  Cover: student unpack error with old score → live 0; system Gitea/provision error → no payload; failed bulk progress → no successful writeback records; anonymous-ID lookup failure → no ordinary endpoint call; dry student-error path → no live submission; normal JOJ score 0 → ordinary writeback.

- [ ] **Step 2: Run the tests and verify they fail**

  Run: `python tests\test_smoke.py`

  Expected: current writeback treats all `failed` stages as zero and accepts failed bulk outcomes.

- [ ] **Step 3: Implement the decision table**

  Separate `student_error_rows` from `system_error_rows`, construct zero payloads only for the former, wrap all Canvas setup and bulk calls, validate bulk workflow state, and record per-student system failures without aborting unrelated students.

- [ ] **Step 4: Run the tests and verify they pass**

  Run: `python tests\test_smoke.py`

  Expected: every writeback classification assertion passes and dry mode performs zero live mutations.

### Task 5: Prevent non-grade report failures from silently blocking classified writeback

**Files:**
- Modify: `gc_auto_ta/report.py` only if report failures need a structured result
- Modify: `gc_auto_ta/cli.py` in `cmd_run`
- Test: `tests/test_smoke.py`

**Interfaces:**
- Report generation failures remain system errors and must be visible.
- The full pipeline must not convert a report filesystem failure into a student grade decision. Decide explicitly whether `cmd_run` should continue to writeback after a report-only failure while returning a nonzero final status.

- [ ] **Step 1: Add a failing orchestration test**

  Simulate report output failure after grade completion and assert that writeback still receives categorized rows, while the command reports a system failure.

- [ ] **Step 2: Run the test and verify the current abort behavior**

  Run: `python tests\test_smoke.py`

  Expected: current `cmd_run` stops when report returns a nonzero result or raises.

- [ ] **Step 3: Implement explicit report-failure policy**

  Preserve the report error for operators, continue only to the already-authorized writeback stage when student state is complete, and return a nonzero aggregate status after all required stages have been attempted.

- [ ] **Step 4: Run the test and verify it passes**

  Run: `python tests\test_smoke.py`

  Expected: writeback is attempted despite report-only failure, and the final command status still signals the report problem.

### Task 6: End-to-end verification and documentation

**Files:**
- Modify: `docs/01-architecture.md` error semantics section
- Modify: `docs/06-canvas-writeback.md` writeback/error policy section
- Modify: `README.md` only if command behavior or status output changes
- Test: `tests/test_smoke.py`

- [ ] **Step 1: Add a matrix test covering every category**

  Use one fixture per case: student corrupt archive, student missing source, Canvas download failure, Gitea provision failure, runner/collection failure, Canvas single-write failure, Canvas bulk failure, normal JOJ score 0, and dry mode.

- [ ] **Step 2: Run the complete verification suite**

  Run: `python tests\test_smoke.py`

  Expected: all category, propagation, zero-overwrite, and dry-mode checks pass.

- [ ] **Step 3: Run static checks**

  Run: `python -m compileall -q gc_auto_ta tests` and `git diff --check`.

  Expected: both commands exit successfully with no diff whitespace errors.

- [ ] **Step 4: Update documentation**

  Document that only `student` errors trigger zero overwrite; system errors remain report-only; normal evaluator score 0 is distinct from a pipeline failure; and bulk writeback success requires a completed Canvas progress state.

- [ ] **Step 5: Review the final diff**

  Confirm no unrelated files, configuration secrets, or user changes were altered. Confirm every new error path has a state record or an explicit global system-error report.
