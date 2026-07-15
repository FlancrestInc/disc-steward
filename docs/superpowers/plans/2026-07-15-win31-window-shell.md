# Win31 Window Shell Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Present each Disc Steward page inside a reusable Win31 window shell and mark the major review surfaces as shared panels.

**Architecture:** `page()` keeps the existing document title, route behavior, and `main` landmark, but nests the generated page body inside `ds-window`, `ds-titlebar`, and `ds-window__body`. Existing review sections receive `ds-panel` hooks without changing their content or layout-specific classes.

**Tech Stack:** Python 3.10 HTML rendering, Pytest, pytest-playwright, vendored `@flancrest/win31-core` CSS.

---

### Task 1: Window-shell contract

**Files:**
- Modify: `tests/test_web_metadata_playback.py`
- Modify: `disc_steward/web.py`

- [x] Add a failing render test requiring the page title in a `ds-titlebar`, a `ds-window` section, and body content within `ds-window__body` inside the existing `main` landmark.
- [x] Run the test and confirm it fails.
- [x] Add the shell without changing links, forms, scripts, or document metadata.
- [x] Run the focused test and confirm it passes.

### Task 2: Panel hooks and browser verification

**Files:**
- Modify: `tests/test_web_metadata_playback.py`
- Modify: `tests/browser/test_review_ui.py`
- Modify: `disc_steward/web.py`

- [x] Add a failing test requiring `ds-panel` on the job summary, primary metadata, file cards, and operations surfaces.
- [x] Add the hooks while retaining existing class names and element types.
- [x] Update the browser test to assert the window shell and capture both viewport screenshots.
- [x] Run focused render and browser tests, compile the package, and check the diff.
