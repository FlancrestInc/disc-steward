# Win31 File Control Adoption Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Apply shared field and control primitives to every editable text, select, and textarea field in Disc Steward’s file-review workflow.

**Architecture:** Preserve all existing input names, values, fieldsets, and form actions. Add `ds-field` to textual labels and `ds-control` to their controls; retain native checkbox markup so dense include/exclude decisions remain compact and familiar.

**Tech Stack:** Python HTML rendering, Pytest, pytest-playwright, vendored `@flancrest/win31-core` CSS.

---

### Task 1: File-review primitive hooks

**Files:**
- Modify: `tests/test_web_metadata_playback.py`
- Modify: `disc_steward/web.py`

- [x] Add a failing rendered-markup test for primary and advanced file fields, including the native checkbox exception.
- [x] Run the test and confirm it fails.
- [x] Add only `ds-field` and `ds-control` classes to the applicable labels and controls.
- [x] Run focused render and browser tests, compile the package, and check the diff.
