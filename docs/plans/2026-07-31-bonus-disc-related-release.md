# Bonus Disc Related Release Implementation Plan

> **For Hermes:** Use this plan to implement the bonus-disc workflow.

**Goal:** Allow an extras-only bonus disc to continue through processing under an already-imported parent release without requiring a duplicate main feature.

**Architecture:** Add an explicit `parent_job_id` relationship and `bonus_disc` job mode. Bonus jobs inherit the parent release metadata for destination generation but retain independent review, validation, transfer, and cleanup records. The review validator skips the main-feature invariant only for bonus jobs, while requiring every included file to have an extras-compatible role.

**Tech Stack:** Python, SQLite migrations, existing review/transfer pipeline, pytest.

---

### Task 1: Add persisted bonus-disc relationship

**Files:** `disc_steward/models.py`, `disc_steward/db.py`, `tests/test_phase4_cleanup_llm_status.py`

Add `parent_job_id` and `job_kind` (`standard`/`bonus_disc`) to the job model and SQLite schema/migration. Preserve legacy rows as standard jobs. Add database helpers to update/read the relationship and test migration plus round-trip behavior.

### Task 2: Resolve effective release metadata and destination context

**Files:** `disc_steward/db.py`, `disc_steward/work_orders.py`, `disc_steward/cleanup.py`, tests

For a bonus job, resolve the parent review metadata when generating final paths and processing payloads, while retaining the bonus job’s own review decisions. Ensure destinations remain under the parent movie folder and role subfolders. Keep cleanup eligibility tied to the bonus job’s own validation and transfer records.

### Task 3: Relax review validation only for bonus jobs

**Files:** `disc_steward/review.py`, `disc_steward/web.py`, tests

Skip the main-feature requirement for `job_kind == bonus_disc`. Require included files to use an extras-compatible role (`extra`, `trailer`, `featurette`, `deleted_scene`, `interview`, `music_video`, `short_film`, `promo`, `alternate_cut`, `commentary_variant`, or `menu_or_bumper`). Preserve normal movie validation for standard jobs.

### Task 4: Add review UI controls and parent selection

**Files:** `disc_steward/web.py`, tests/browser fixtures

Show a bonus-disc mode and parent-release selector/action on the review page. Persist the selected parent and mode before continuing. Display the inherited parent title and destination context clearly. Do not auto-select a parent silently when multiple candidates exist.

### Task 5: Apply the workflow to job 8 and verify end to end

Use the existing completed Lego Movie job 7 as the explicit parent for job 8. Confirm the review page becomes actionable, save role decisions, run processing/validation/transfer only after review approval, and verify generated paths, independent audit/transfer state, and cleanup behavior. Run targeted and full tests, commit, push, restart the affected service, and verify the live page.
