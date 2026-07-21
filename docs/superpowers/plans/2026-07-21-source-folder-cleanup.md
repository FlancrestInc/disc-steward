# Source Folder Cleanup Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove completed raw disc folders after verified transfers and remove source folders when users delete jobs, with path and shared-job safeguards.

**Architecture:** Extend the existing cleanup planner with a job-scoped `raw_rip_folder` candidate and canonical containment checks. Transfer completion will persist its successful summary, invoke job-scoped cleanup, then preserve transfer success while reporting cleanup warnings. The web Delete job action will use a shared filesystem helper before deleting database state; shared folders remain while sibling jobs reference them.

**Tech Stack:** Python 3.10+, pathlib, shutil, SQLite, pytest, existing dataclass/YAML configuration.

---

## Chunk 1: Configuration and cleanup primitives

**Files:**
- Modify: `disc_steward/config.py` (`CleanupConfig`, `config_from_dict`)
- Modify: `disc_steward/cleanup.py`
- Modify: `disc_steward/models.py` only if the cleanup item type needs a typed field change
- Test: `tests/test_phase4_cleanup_llm_status.py`
- Test: `tests/test_paths_validation_transfer.py`

- [ ] **Step 1: Add failing configuration tests.**

  Test legacy `delete_raw_rips` parsing, folder-only parsing, and rejection when both modes are enabled. Test that `transfer_verify: none` cannot produce an eligible folder candidate.

- [ ] **Step 2: Run the focused tests and verify they fail for missing configuration and verification behavior.**

  Run: `uv run pytest tests/test_phase4_cleanup_llm_status.py tests/test_paths_validation_transfer.py -k 'cleanup or config' -q`

  Expected: FAIL because the new setting and folder eligibility do not exist.

- [ ] **Step 3: Add `delete_raw_rip_folders` and mutual-exclusion validation.**

  Preserve the old `delete_raw_rips` field and parser behavior. Parse the new key and raise a clear `ValueError` if both deletion modes are true.

- [ ] **Step 4: Add failing cleanup-core behavior tests.**

  Add tests for canonical raw-root and symlink-escape rejection, rejecting the raw root itself, shared/split-folder eligibility, incomplete jobs, cleanup holds, dry runs, and full-tree archive verification and deletion.

- [ ] **Step 5: Run the cleanup-core tests and verify they fail.**

  Run: `uv run pytest tests/test_phase4_cleanup_llm_status.py tests/test_paths_validation_transfer.py -k 'cleanup or raw_rip or archive' -q`

  Expected: FAIL because folder candidates, shared-folder checks, and canonical guards do not exist.

- [ ] **Step 6: Add canonical raw-folder target validation.**

  Resolve both root and target with `Path.resolve(strict=False)`. Require the target to be an existing or missing child of the resolved raw-rip root, reject the root itself, and reject symlink escapes. Check this before treating a missing target as already absent.

- [ ] **Step 7: Add folder candidates and shared-job checks to `plan_cleanup`.**

  Group jobs by canonical `source_disc_path`. For a folder candidate, require all jobs referencing that folder to satisfy terminal success, verified transfer (`verification` is `size` or `sha256`), final-path existence, no cleanup hold, mount availability, and retention. Do not mix folder candidates with per-file raw candidates.

- [ ] **Step 8: Implement folder archive/delete execution.**

  For folder mode, archive the complete folder tree to the configured archive root, verify every copied file before deletion, then remove the source with `shutil.rmtree`. Keep dry-run behavior and audit records. Preserve existing per-file archive/delete behavior for legacy mode.

- [ ] **Step 9: Run all cleanup tests.**

  Run: `uv run pytest tests/test_phase4_cleanup_llm_status.py tests/test_paths_validation_transfer.py -q`

  Expected: PASS.

- [ ] **Step 10: Commit the cleanup core.**

  Run: `git add disc_steward/config.py disc_steward/cleanup.py disc_steward/models.py tests/test_phase4_cleanup_llm_status.py tests/test_paths_validation_transfer.py && git commit -m "feat: support safe raw folder cleanup"`

## Chunk 2: Automatic cleanup after transfer

**Files:**
- Modify: `disc_steward/cleanup.py` to expose job-scoped execution if needed
- Modify: `disc_steward/transfer.py` after persisted transfer completion
- Test: `tests/test_phase3_pipeline.py`

- [ ] **Step 1: Add failing transfer integration tests.**

  Cover cleanup only when enabled/live and folder mode is enabled; dry-run and disabled cleanup leave the folder; cleanup errors leave the transfer status as `imported_to_jellyfin`, append a warning, audit the cleanup error, and persist the amended transfer summary.

- [ ] **Step 2: Run the focused tests and verify they fail.**

  Run: `uv run pytest tests/test_phase3_pipeline.py -k 'transfer and cleanup' -q`

  Expected: FAIL because transfer currently does not invoke cleanup.

- [ ] **Step 3: Invoke job-scoped cleanup after transfer state is persisted.**

  In `transfer_job_to_eddy`, after saving the successful transfer summary and status/audit, call the cleanup executor for that job only when configured. Inspect the returned `summary.errors`; append a cleanup warning and re-save the transfer summary when errors exist. Keep an exception guard that calls `db.audit("cleanup_error", ...)`, records a warning, re-saves the amended transfer summary, and does not change the successful transfer status. Test persistence in both error paths.

- [ ] **Step 4: Run transfer and regression tests.**

  Run: `uv run pytest tests/test_phase3_pipeline.py tests/test_phase4_cleanup_llm_status.py -q`

  Expected: PASS.

- [ ] **Step 5: Commit automatic cleanup integration.**

  Run: `git add disc_steward/transfer.py disc_steward/cleanup.py tests/test_phase3_pipeline.py && git commit -m "feat: clean raw folders after verified transfer"`

## Chunk 3: Deleted-job source cleanup

**Files:**
- Modify: `disc_steward/db.py` with a query/helper for other jobs sharing a source folder
- Modify: `disc_steward/web.py` in `handle_job_action` Delete job branch
- Test: `tests/test_web_metadata_playback.py`

- [ ] **Step 1: Add failing Delete job tests.**

  Test an exclusive job deletes its raw folder before the database row, records the ignored path, and succeeds. Test a missing folder is treated as already absent. Test a shared/split folder keeps the folder and removes only the requested job. Test raw-root and symlink-escape targets raise `ValueError`, retain the job, create no ignored-path record, and write a failure audit event. Test filesystem deletion failure has the same retention and audit behavior.

- [ ] **Step 2: Run the focused tests and verify they fail.**

  Run: `uv run pytest tests/test_web_metadata_playback.py -k 'delete_job or deleted_job' -q`

  Expected: FAIL because Delete job currently never removes source files or validates the path.

- [ ] **Step 3: Add the shared-reference query and use cleanup path validation.**

  Compare canonical source paths for all jobs before deleting. For an exclusively referenced target, validate containment, remove the folder recursively if present, and audit success. For shared targets, retain the folder and audit that it was preserved.

- [ ] **Step 4: Preserve database state on failure.**

  On unsafe paths or filesystem errors, audit the failure with the path and reason, then raise `ValueError` before writing ignored-path state or deleting the job row. On success, retain the current ignored-path and job-delete sequence.

- [ ] **Step 5: Run web and full regression tests.**

  Run: `uv run pytest tests/test_web_metadata_playback.py tests/test_phase3_pipeline.py tests/test_phase4_cleanup_llm_status.py tests/test_paths_validation_transfer.py -q`

  Expected: PASS.

- [ ] **Step 6: Commit deleted-job cleanup.**

  Run: `git add disc_steward/db.py disc_steward/web.py tests/test_web_metadata_playback.py && git commit -m "feat: remove source folders for deleted jobs"`

## Chunk 4: Documentation and final verification

**Files:**
- Modify: `README.md` cleanup configuration and workflow sections
- Modify: config example file if present after locating it with `rg --files`
- Test: existing full suite

- [ ] **Step 1: Document both cleanup modes and automatic timing.**

  Explain the opt-in folder setting, verified transfer requirement, shared-folder behavior, deleted-job behavior, dry-run, archive ordering, and failure handling.

- [ ] **Step 2: Run the complete test suite and static checks.**

  Run: `uv run pytest -q` and `git diff --check`

  Expected: all tests PASS and no whitespace errors.

- [ ] **Step 3: Review the final diff and commit documentation.**

  Run: `git diff --check && git diff -- README.md config.example.yaml && git add README.md <updated config example> && git diff --cached --check && git diff --cached --stat`

  Commit: `git commit -m "docs: document raw folder cleanup"`
