# Completed Job Artifact Cleanup Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove completed raw-rip folders, working outputs, and tracked previews immediately after verified transfer.

**Architecture:** Extend the existing job-scoped cleanup planner so folder mode includes deduplicated working outputs only after verified transfer and canonical containment under configured working or validation roots. Destructive execution rechecks all safety invariants to prevent post-plan path swaps. Transfer completion calls configured live cleanup after persistence whenever any deletion type is enabled, then atomically cancels previews and clears only safely-contained preview files. The preview worker publishes only through an atomic owned-running-state transition.

**Tech Stack:** Python 3.10+, pathlib, shutil, SQLite, pytest, YAML.

---

## Chunk 1: Cleanup lifecycle

### Task 1: Clean all completed-job media artifacts

**Files:**
- Modify: `disc_steward/cleanup.py`
- Modify: `disc_steward/transfer.py`
- Modify: `disc_steward/db.py`
- Modify: `disc_steward/cli.py`
- Test: `tests/test_phase4_cleanup_llm_status.py`
- Test: `tests/test_video_previews.py`

- [ ] Write failing tests for folder-mode deduplicated working-output cleanup, automatic cleanup after transfer, safe tracked-preview sweeping, outside-root and symlink preview rejection, unlink failure retention, post-plan symlink swapping, and cancellation forced between a worker's final check and publish.
- [ ] Run the focused tests and confirm each fails for the missing behavior.
- [ ] Add canonical containment and symlink checks for working files under the FileFlows working or validation roots and for previews under the preview-cache root. Reject every root itself and deduplicate a shared working path. Require size/SHA-256 transfer verification for every automatic deletion. Recheck containment, symlinks, mounts, and verified completion immediately before every destructive operation. Atomically cancel preview jobs before cleanup; publish only from a still-owned running row, using a temporary file that is discarded on cancellation. Preserve transfer success and preview metadata if a preview unlink fails.
- [ ] Make the minimal cleanup, transfer, database, and CLI changes. Trigger cleanup only after the successful transfer summary and final job status are persisted, and whenever any live deletion type is configured.
- [ ] Add a `cleanup-previews` command for already-completed jobs. It must use the same verified-completion checks, delete only database-tracked safely-contained previews, retain metadata after a failed deletion, and report skipped unsafe or incomplete rows.
- [ ] Run focused tests, then the full suite.

## Chunk 2: Deployment settings

### Task 2: Enable safe immediate cleanup in the active deployment config

**Files:**
- Modify: `config.example.yaml`
- Test: `tests/test_config_yaml_fallback.py`

- [ ] Write failing example-configuration assertions.
- [ ] Run them and confirm the example does not yet describe immediate cleanup.
- [ ] Document `cleanup.enabled: true`, `cleanup.dry_run: false`, `cleanup.delete_raw_rip_folders: true`, `cleanup.delete_working_files: true`, zero retention values, and `preview.delete_after_transfer: true` in the tracked example config. Keep defaults non-destructive. Apply matching settings to the ignored deployment config only as a separate operator step after code verification.
- [ ] Run configuration and full-suite checks.
