# Win31 Adoption Hardening Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Finish the practical design-system adoption work with reusable state surfaces, accessible heading/error semantics, visual baselines, and artifact provenance.

**Architecture:** Add semantic state primitives to the shared core package and apply them where Disc Steward already has errors and empty states. Keep notifications deferred until the existing server flow exposes a durable event contract. Browser tests own stable desktop/narrow reference images; packaged artifact provenance documents the exact source package/version/commit used by Disc Steward.

**Tech Stack:** CSS packages, Python 3.10, Pytest, pytest-playwright, Pillow-based image comparison, `prefers-reduced-motion`.

---

### Task 1: State and accessibility primitives

- [x] Add `ds-status` and `ds-empty-state` to the core package with semantic variants.
- [x] Render the window title as the page’s single H1 and turn user-facing error summaries into alert regions.
- [x] Apply shared state classes to existing empty/error output without changing workflow behavior.

### Task 2: Visual regression coverage

- [x] Add a browser screenshot comparator with checked-in desktop and narrow baselines.
- [x] Update the baseline only through an explicit environment flag.
- [x] Exercise standard and reduced-motion browser contexts.

### Task 3: Distribution and test-suite handoff

- [x] Record vendored core/motion artifact provenance and refresh instructions in Disc Steward.
- [x] Run Style package checks plus Disc Steward focused/browser/full-suite commands; diagnose rather than claim a full pass if the runner does not complete.
- [x] User authorized publishing and pushing after the checks completed.
