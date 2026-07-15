# Design-System Static Asset Seam Implementation Plan

**Goal:** Let Disc Steward serve and link a versioned compiled Win31 core stylesheet while preserving its current inline CSS as the visual fallback.

**Architecture:** A vendored, built CSS artifact is served from a fixed `/static/` route. `page()` links that file before the existing inline style block, so the current application styles continue to win until the later token/primitives migration. The asset URL includes its modification timestamp for cache invalidation; the route is deliberately allowlisted rather than a general filesystem server.

**Tech Stack:** Python 3.10 standard-library HTTP server, Pytest, compiled CSS from `@flancrest/win31-core`.

---

### Task 1: Link the static design-system stylesheet

**Files:**
- Modify: `disc_steward/web.py`
- Modify: `tests/test_web_metadata_playback.py`

- [x] Write a failing test asserting `page()` emits a versioned `/static/win31-core.css` stylesheet link before its inline style block.
- [x] Run the focused test and confirm it fails because no static stylesheet link is rendered.
- [x] Add asset-path/version helpers and the stylesheet link without changing the existing inline CSS or body markup.
- [x] Run focused tests and the existing web-render test module.

### Task 2: Serve the allowlisted asset

**Files:**
- Modify: `disc_steward/web.py`
- Modify: `tests/test_web_metadata_playback.py`

- [x] Write a failing HTTP-level test that verifies `/static/win31-core.css` returns CSS and unknown static paths return 404.
- [x] Run the focused test and confirm it fails because `/static/` is not routed.
- [x] Add a fixed asset route and response helper with CSS MIME type and cache policy.
- [x] Run the focused tests.
- [ ] Run the full test suite to a final completion status (the current command runner did not return one during this task).

### Task 3: Vendor the compiled artifact and document sync ownership

**Files:**
- Create: `disc_steward/static/win31-core.css`
- Modify: `README.md`

- [x] Copy the built `@flancrest/win31-core` artifact once as a versioned vendor asset; preserve its generated-source header.
- [x] Document that the asset is a temporary packaged artifact, refreshed from a released/packed Style package rather than hand-edited.
- [x] Verify the stylesheet is non-empty, tests pass, and `git diff --check` is clean.
