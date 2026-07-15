# Disc Steward Large Review Job Usability Implementation Plan

> **For Hermes:** Use subagent-driven-development skill to implement this plan task-by-task.

**Goal:** Make large Disc Steward job review pages easier to work through by turning the review surface into a guided triage workspace instead of a long, overwhelming form.

**Architecture:** Keep the existing review workflow and POST actions, but change the page layout and default disclosure state. Add a compact job summary strip, group files into role-based lanes, collapse low-priority and already-reviewed content by default, and expose one-file-at-a-time review behavior for unresolved items. Preserve the current save/review/automation actions so this remains a UI and navigation refactor rather than a workflow rewrite.

**Tech Stack:** Python 3.11, Disc Steward HTML renderer in `disc_steward/web.py`, existing review/test helpers in `tests/test_phase2_review.py`, pytest, browser verification against the live review UI.

---

## Background

The current review page is workable for small jobs, but jobs with dozens of files become hard to scan because too many cards compete for attention at once. The main usability issue is not the data itself; it is the amount of information shown at the same level of emphasis.

The target behavior is:
- show the important decisions first
- hide repetitive or low-signal content until needed
- let a reviewer move through unresolved items sequentially
- keep bulk review possible for large jobs
- preserve all existing review actions and metadata controls

## Proposed UX shape

- Top summary strip with counts and blockers
- Role-based file lanes
- Compact file cards by default
- Expanded details only on demand
- Unresolved-only focus for large jobs
- Bulk actions for repetitive decisions
- Sticky save / approve actions

---

## Task 1: Add a job-level summary strip for large review pages

**Objective:** Show the reviewer a compact overview of the job before they reach the file list.

**Files:**
- Modify: `disc_steward/web.py`
- Test: `tests/test_phase2_review.py`

**Step 1: Write the test first**

Add assertions that the review page renders a job summary area with the important counts and blockers. The exact text can match the current job data, but the page should expose:
- total file count
- unresolved count
- skipped count
- likely main feature count or main feature status
- warning / conflict indicators

Example test shape:

```python
def test_review_page_renders_job_summary_counts(tmp_path):
    # build a job with multiple files and one skipped decision
    html = render_job_review(db, config, job_id)
    assert "Job summary" in html
    assert "files" in html
    assert "unresolved" in html
    assert "skipped" in html
    assert "warnings" in html or "conflicts" in html
```

**Step 2: Run the test and confirm it fails**

Run:

```bash
uv run pytest tests/test_phase2_review.py -k summary -v
```

Expected: failure because the summary strip does not exist yet.

**Step 3: Implement the smallest possible summary helper**

In `disc_steward/web.py`, add a helper near `render_job_review(...)` that computes page-level counts from the job review and file review data. Keep it simple and deterministic.

Suggested output fields:
- `total_files`
- `included_files`
- `skipped_files`
- `unresolved_files`
- `main_feature_candidates`
- `warning_count`
- `conflict_count`

Render it near the top of the page as a compact strip or panel.

**Step 4: Run the test again**

Run:

```bash
uv run pytest tests/test_phase2_review.py -k summary -v
```

Expected: pass.

**Step 5: Commit-ready verification**

Run the phase 2 review suite:

```bash
uv run pytest tests/test_phase2_review.py -v
```

Expected: pass.

---

## Task 2: Group files into collapsible role-based lanes

**Objective:** Replace the single undifferentiated file list with role-based lanes so big jobs are easier to scan.

**Files:**
- Modify: `disc_steward/web.py`
- Test: `tests/test_phase2_review.py`

**Step 1: Write the test first**

Add assertions that the page renders the expected lanes and lane counts. At minimum:
- Main feature candidates
- Episodes
- Extras
- Trailers/Promos
- Featurettes/Documentaries
- Deleted Scenes
- Menu/Logo/Bumper Candidates
- Manual Review
- Skipped / Do Not Process

Example assertion shape:

```python
assert "Main Feature Candidates" in html
assert "Manual Review" in html
assert "Skipped / Do Not Process" in html
assert html.count("dashboard-lane") >= 3
```

**Step 2: Verify the failure**

Run:

```bash
uv run pytest tests/test_phase2_review.py -k lane -v
```

Expected: fail or show missing lane structure until implemented.

**Step 3: Implement the lane grouping helper**

Add a helper in `disc_steward/web.py` that:
- reads each file review decision
- assigns it to a lane based on `role`, `content_type`, and `include_in_work_order`
- sorts files into lanes in a stable order
- returns HTML for each lane section

Keep the grouping logic explicit. Do not infer too much from metadata; prefer the current saved review decision where available.

**Step 4: Make low-signal lanes collapsible by default**

Use `<details>` for lanes that are large or low-priority.
Open by default:
- main feature candidates
- unresolved/manual review lane

Collapsed by default:
- skipped
- extras-heavy lanes
- already-reviewed lanes when the user has many files

**Step 5: Re-run the lane tests**

Run:

```bash
uv run pytest tests/test_phase2_review.py -k lane -v
```

Expected: pass.

**Step 6: Re-run the full review suite**

Run:

```bash
uv run pytest tests/test_phase2_review.py tests/test_web_metadata_playback.py -v
```

Expected: pass.

---

## Task 3: Make file cards compact by default and expand only when needed

**Objective:** Reduce vertical density by showing only the high-signal fields on each file card unless the reviewer opens the detail panel.

**Files:**
- Modify: `disc_steward/web.py`
- Test: `tests/test_phase2_review.py`

**Step 1: Add a compact-card test**

Verify that each card renders:
- filename
- include/exclude state
- role/content summary
- destination preview
- a detail toggle

And that the advanced fields remain inside a collapsed section.

Example assertions:

```python
assert "Advanced file details" in html
assert "Include in processing" in html
assert "Final destination preview" in html
assert "Subtitle plan" in html
```

**Step 2: Confirm the current implementation is too verbose**

Run:

```bash
uv run pytest tests/test_phase2_review.py -k compact -v
```

Expected: identify where the current file card still shows too much by default.

**Step 3: Refactor `render_file_card(...)`**

Keep the existing controls, but restructure the card into two layers:
- summary block: filename, summary, warnings, destination preview, include toggle, role/content selectors
- collapsed details block: technical metadata, IDs, subtitle plan, notes, path details

Do not remove any fields. Only move them under progressive disclosure.

**Step 4: Keep existing metadata lookup behavior**

The metadata lookup section should remain collapsible when a title already exists. Preserve that behavior while changing the larger card layout.

**Step 5: Run the compact-card tests**

Run:

```bash
uv run pytest tests/test_phase2_review.py -k compact -v
```

Expected: pass.

---

## Task 4: Add a guided review path for unresolved files

**Objective:** Make it easy to work through a big job one unresolved file at a time instead of constantly re-scanning the page.

**Files:**
- Modify: `disc_steward/web.py`
- Test: `tests/test_phase2_review.py`

**Step 1: Add a test for unresolved-first ordering**

Create a test that seeds multiple file decisions and confirms the page puts unresolved or manual-review items first in the default expanded state.

Example assertions:

```python
assert "Unresolved" in html or "needs attention" in html
assert "open" in html  # if using <details open>
```

**Step 2: Implement a helper that marks the next unresolved file**

Add a helper that determines:
- which lane contains unresolved items
- which single file should be opened by default
- whether a reviewed file should start collapsed

Prefer deterministic ordering based on file order and role priority.

**Step 3: Add next / previous navigation if needed**

If the page supports a selected file or unresolved focus, add small links or buttons to move to the next unresolved file without scanning the whole page.

Keep this minimal; it should help navigation without changing the save flow.

**Step 4: Re-run the focused tests**

Run:

```bash
uv run pytest tests/test_phase2_review.py -k unresolved -v
```

Expected: pass.

---

## Task 5: Add bulk actions and a stronger sticky action bar

**Objective:** Reduce repetitive clicking on large jobs.

**Files:**
- Modify: `disc_steward/web.py`
- Test: `tests/test_phase2_review.py`

**Step 1: Write a bulk-action test**

Assert that the page exposes the action affordances the reviewer needs for large jobs:
- save draft
- save and continue
- mark reviewed
- resume automated flow
- bulk lane actions or section-level controls

**Step 2: Implement the action bar changes**

If the existing sticky actions are already present, reorganize them so the most common action is most visible.
Suggested order:
- Save draft review
- Save and next / continue
- Mark reviewed and run pipeline
- Resume automated flow
- manual review / reopen actions

**Step 3: Add lane-level bulk controls where safe**

For example:
- collapse all reviewed files in a lane
- mark all files in a lane as skipped
- set a shared role for selected items

Only add bulk actions where the semantics are obvious and safe.

**Step 4: Re-run the page tests**

Run:

```bash
uv run pytest tests/test_phase2_review.py tests/test_web_metadata_playback.py -v
```

Expected: pass.

---

## Task 6: Verify the redesign on a real large job

**Objective:** Confirm the new structure actually helps on a heavy job like Job 4.

**Files:**
- No code changes expected unless verification exposes a bug
- Possibly modify: `disc_steward/web.py`
- Possibly modify: `tests/test_phase2_review.py`

**Step 1: Restart the live review service**

Use the user service that serves the review UI, then refresh the browser.

**Step 2: Open Job 4 in the browser**

Check that:
- the summary strip is visible immediately
- large lanes are collapsed by default
- only the important section is expanded
- the page does not feel like one endless document

**Step 3: Adjust spacing or default-open state if needed**

If the page still feels too dense, reduce the amount of default-open content before changing the workflow.

**Step 4: Record what worked**

If the layout is good, keep the behavior and update the tests so it does not regress.

---

## Acceptance criteria

The implementation is done when:
- the review page shows a clear job summary at the top
- large jobs are broken into role-based lanes
- low-signal lanes are collapsed by default
- file cards show only the most important fields at first
- advanced fields remain available but out of the way
- reviewers can work through unresolved items without scanning the entire page
- existing save / approve / resume actions still work
- the review tests pass
- the live page looks better on a large job like Job 4

---

## Verification commands

Run these after each task or group of tasks:

```bash
uv run pytest tests/test_phase2_review.py -v
uv run pytest tests/test_phase2_review.py tests/test_web_metadata_playback.py -v
```

For live verification:
- restart `disc-steward-review.service`
- open a large job in the browser
- confirm the lane layout and collapsed sections are visible

---

## Notes

- Do not rewrite the review workflow itself unless a test proves it is necessary.
- Prefer collapsing and grouping over inventing a separate “big job mode” unless the simpler layout is insufficient.
- Keep all current review fields and save paths intact.
- The goal is to reduce cognitive load, not to hide data permanently.
