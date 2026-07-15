# Win31 Primitive Adoption Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Adopt the existing Win31 core button, field/control, and table primitives in Disc Steward without changing form submission, routing, or review data.

**Architecture:** Keep Disc Steward's generated HTML and its inline application CSS. Add `ds-*` hooks only to semantic native elements; the vendored core stylesheet owns the shared control appearance while app CSS remains responsible for the review workflow's layout. Test the rendered HTML contracts and verify the review page at desktop and narrow widths.

**Tech Stack:** Python 3.10, standard-library HTTP server, Pytest, vendored `@flancrestinc/win31-core` CSS, Playwright where available.

---

### Task 1: Establish primitive markup contracts

**Files:**
- Modify: `tests/test_web_metadata_playback.py`
- Modify: `disc_steward/web.py`

- [x] Write focused render tests requiring `ds-field` / `ds-control` on review metadata inputs, `ds-button` variants on primary and destructive actions, and `ds-table` on operational tables.
- [x] Run the test and confirm it fails because the hooks are absent.
- [x] Add only the relevant classes to current native labels, controls, buttons, and tables; retain every `name`, `form`, `formaction`, `method`, `disabled`, and confirmation attribute.
- [x] Run the focused test and confirm it passes.

### Task 2: Keep application styling from overriding shared primitives

**Files:**
- Modify: `disc_steward/web.py`
- Test: `tests/test_web_metadata_playback.py`

- [x] Write a render-level assertion that the inline stylesheet scopes legacy generic control/table rules away from `ds-*` primitives.
- [x] Run the test and confirm it fails.
- [x] Scope only the conflicting generic selectors so shared primitive classes retain their core appearance, preserving review-specific layouts and checkbox behavior.
- [x] Run the focused web-render module.

### Task 3: Browser coverage

**Files:**
- Modify: `pyproject.toml`, `uv.lock`
- Create: `tests/browser/test_review_ui.py`
- [x] Write browser tests that start the real review handler, assert semantic keyboard access and design-system controls, and capture desktop/narrow screenshots into ignored test output.
- [x] Run the test and confirm it fails because the pytest `page` fixture is unavailable.
- [x] Add `pytest-playwright` to the test extra and install Chromium with the Playwright CLI; do not add any browser dependency to runtime requirements.
- [x] Run the browser tests and retain screenshots only as test output, not versioned source assets.

### Task 4: Verify and commit

- [x] Run `git diff --check`, compile the package, and run the focused web tests.
- [ ] Run the full suite and record its final status; if the environment does not return one, report that limitation without claiming a full pass.
- [ ] Commit only this primitive-adoption change.
