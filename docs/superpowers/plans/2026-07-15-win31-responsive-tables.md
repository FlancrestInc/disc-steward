# Win31 Responsive Table Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Keep complete Disc Steward data tables usable at narrow widths through an accessible shared horizontal-scroll primitive.

**Architecture:** Reuse the existing core `ds-table-wrap` overflow primitive. Disc Steward wraps each operational `ds-table` in a labelled, keyboard-focusable scroll region; table semantics and all columns stay intact.

**Tech Stack:** CSS, Python HTML rendering, Pytest, pytest-playwright, vendored `@flancrestinc/win31-core` CSS.

---

### Task 1: Disc Steward table wrappers

**Files:**
- Modify: `tests/test_web_metadata_playback.py`, `tests/browser/test_review_ui.py`, `disc_steward/web.py`
- Update: `disc_steward/static/win31-core.css`

- [x] Add failing render and browser tests for labelled, focusable table-scroll regions.
- [x] Wrap each operational table while preserving table headers, cell content, and density attributes.
- [x] Verify a narrow viewport keeps a wide table scrollable rather than clipping it.
- [x] Run focused tests, compilation, and diff checks.
