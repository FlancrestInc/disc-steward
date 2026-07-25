# Recent Errors Timestamps and Clear Action Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Show timestamps for dashboard errors and let users dismiss the current error list while preserving audit history.

**Architecture:** Reuse `audit_log.created_at` and `dismissed`. Add a database method that dismisses error and failed events, a root-level POST route that calls it, and dashboard markup with timestamped rows plus a clear form.

**Tech Stack:** Python, SQLite, stdlib HTTP server, pytest.

---

## Chunk 1: Database and dashboard behavior

### Task 1: Add failing coverage for clearing audit errors

**Files:**
- Modify: `tests/test_web_metadata_playback.py`

- [ ] **Step 1: Write the failing test**

Add a test that creates a database, inserts an `automation_failed` event and a non-error event, calls the new database clear method, and asserts the error is dismissed, the non-error remains visible, and both rows still exist.

- [ ] **Step 2: Run the focused test**

Run: `PYTHONPATH=. uv run pytest -q tests/test_web_metadata_playback.py -k clear_recent_errors`

Expected: FAIL because the clear method does not exist.

### Task 2: Implement the database clear operation

**Files:**
- Modify: `disc_steward/db.py` near `dismiss_audit_event`

- [ ] **Step 1: Add the minimal method**

Add a transaction that updates `audit_log.dismissed = 1` where `dismissed = 0` and `event_type` contains `error` or `failed`. Return the number of rows changed.

- [ ] **Step 2: Run the focused test**

Run: `PYTHONPATH=. uv run pytest -q tests/test_web_metadata_playback.py -k clear_recent_errors`

Expected: PASS.

### Task 3: Add failing coverage for dashboard timestamps and action

**Files:**
- Modify: `tests/test_web_metadata_playback.py`

- [ ] **Step 1: Write the failing test**

Create a known audit event with a fixed `created_at` value, render the job list, and assert the timestamp, a POST form targeting `/clear-errors`, and a `Clear errors` button are present. Also assert the clear action returns `redirect:/` and the dashboard no longer shows the dismissed error.

- [ ] **Step 2: Run the focused test**

Run: `PYTHONPATH=. uv run pytest -q tests/test_web_metadata_playback.py -k dashboard_recent_errors`

Expected: FAIL because the route and markup do not exist.

### Task 4: Implement the route and dashboard markup

**Files:**
- Modify: `disc_steward/web.py` in `ReviewRequestHandler.do_POST`
- Modify: `disc_steward/web.py` in `handle_ignored_action`-adjacent action helpers and `render_dashboard`

- [ ] **Step 1: Add the root POST route**

Handle `/clear-errors`, call the database clear method, and redirect to `/`.

- [ ] **Step 2: Render timestamps and clear form**

Render `created_at` beside each error, escaped like the other event fields. Add an accessible POST form and button beside the section heading. Keep the existing five-item display limit and omit the panel when no errors remain.

- [ ] **Step 3: Run the focused tests**

Run: `PYTHONPATH=. uv run pytest -q tests/test_web_metadata_playback.py -k 'clear_recent_errors or dashboard_recent_errors'`

Expected: PASS.

## Chunk 2: Full verification

### Task 5: Run the relevant test suite and inspect the diff

**Files:**
- Verify: `disc_steward/db.py`, `disc_steward/web.py`, `tests/test_web_metadata_playback.py`

- [ ] **Step 1: Run all Python tests**

Run: `PYTHONPATH=. uv run pytest -q`

Expected: all tests pass.

- [ ] **Step 2: Check formatting and scope**

Run: `git diff --check` and `git status --short`.

Expected: no whitespace errors; unrelated pre-existing changes remain untouched.

- [ ] **Step 3: Commit the implementation**

```bash
git add disc_steward/db.py disc_steward/web.py tests/test_web_metadata_playback.py
git commit -m "feat: add timestamps and clear action to recent errors"
```

