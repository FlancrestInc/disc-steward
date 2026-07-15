# Disc Steward Pipeline Automation Implementation Plan

> **For Hermes:** Use `subagent-driven-development` to implement this plan task-by-task.

**Goal:** Automate Disc Steward from new-disc detection through scan, metadata/title inference, ffmpeg processing, validation, transfer, and Jellyfin refresh, with only a final human verification/correction step for metadata.

**Architecture:** Keep the existing scan → review → process → validate → transfer pipeline intact, but make review mostly pre-populated by deterministic signals plus local title inference. Add a small background watcher/runner so new rip folders are discovered automatically, then use a local evidence bundle and confidence scoring to fill in the review form before the user confirms it. Preserve safety by keeping final approval manual, making the auto-path additive rather than magical, and leaving all destructive or externally visible actions behind explicit validation gates. Barnabas should remain the heavy-work host; the controller may orchestrate over SSH, but the actual encode should run inside Barnabas’s Dockerized processing environment rather than depending on host-level tools.

**Tech Stack:** Python 3.11, existing `ffprobe`/`ffmpeg` tooling, SQLite, the current Disc Steward web UI, optional Ollama HTTP endpoint on Barnabas, existing `rapidocr-onnxruntime` for title-card OCR, and the existing test suite under `pytest`.

---

## Task 1: Add a title-discovery data model and config knobs

**Objective:** Represent title discovery evidence, confidence, and local-model settings without disturbing the current review model.

**Files:**
- Modify: `disc_steward/models.py`
- Modify: `disc_steward/config.py`
- Modify: `disc_steward/db.py` (only if persistence needs new columns/tables)
- Test: `tests/test_scanner.py`
- Test: `tests/test_phase4_cleanup_llm_status.py`

**Step 1: Write failing tests**

Add tests that assert:
- a discovery result can represent multiple title signals and a confidence score;
- config can enable/disable local title inference independently of the general LLM suggestion hook;
- the database can store the chosen discovery result or at least the raw evidence bundle if persistence is added.

**Step 2: Run the tests to confirm they fail**

Run:
```bash
uv run pytest tests/test_scanner.py tests/test_phase4_cleanup_llm_status.py -v
```
Expected: fail because the new data model/config fields do not exist yet.

**Step 3: Implement the minimal data structures**

Add small dataclasses such as:
```python
@dataclass
class TitleDiscoverySignal:
    source: str
    value: str
    weight: float = 1.0
    confidence: float = 0.0
    notes: list[str] = field(default_factory=list)

@dataclass
class TitleDiscoveryResult:
    title: str = ""
    original_title: str | None = None
    romanized_title: str | None = None
    translated_title: str | None = None
    year: int | None = None
    content_type: str = "unknown"
    library_root: str = "Movies"
    confidence: float = 0.0
    signals: list[TitleDiscoverySignal] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
```

Add config fields to `LLMConfig` or a sibling config block so local discovery can be enabled separately, for example:
- `title_discovery.enabled`
- `title_discovery.endpoint`
- `title_discovery.model`
- `title_discovery.min_confidence_to_auto_fill`
- `title_discovery.max_candidates`

If persistence is needed, add only the smallest required SQLite columns or a JSON blob field; do not add a broad metadata schema unless the tests force it.

**Step 4: Run the tests again**

Run:
```bash
uv run pytest tests/test_scanner.py tests/test_phase4_cleanup_llm_status.py -v
```
Expected: pass for the new structural tests.

**Step 5: Commit**

```bash
git add disc_steward/models.py disc_steward/config.py disc_steward/db.py tests/test_scanner.py tests/test_phase4_cleanup_llm_status.py
git commit -m "feat: add title discovery data model"
```

---

## Task 2: Extract deterministic title evidence from scans

**Objective:** Build title candidates from file/folder metadata before involving any model.

**Files:**
- Modify: `disc_steward/scanner.py`
- Create: `disc_steward/title_discovery.py`
- Modify: `disc_steward/models.py` if needed for extra fields on `ScannedFile`
- Test: `tests/test_scanner.py`
- Test: `tests/test_metadata_lookup.py`

**Step 1: Write failing tests**

Add tests for signals derived from:
- disc folder name
- embedded MKV title tags (`title`, `MAKEMKV_TITLE`)
- chapter names if present
- filename stem
- title-card OCR input hooks, if a frame is available

A good test shape is:
- build a `ScannedFile` fixture with an embedded title and `MAKEMKV_TITLE`
- assert the discovery helper returns a preferred title and a list of source signals

**Step 2: Run the tests and confirm the failure**

Run:
```bash
uv run pytest tests/test_scanner.py tests/test_metadata_lookup.py -v
```
Expected: failure until the helper exists.

**Step 3: Implement deterministic evidence collection**

Create a helper module that does the cheap, local work first:
- normalize the disc folder name
- strip obvious rip suffixes like `DISC1`, `A1_t00`, `title_t00`
- prefer embedded `title` / `MAKEMKV_TITLE` when it looks sane
- collect chapter labels if present
- optionally inspect a small set of video frames for title cards using existing OCR tooling

Keep this logic deterministic and explainable. The helper should return both a chosen title and the evidence that led there.

**Step 4: Wire the helper into scanning**

Update `scan_disc_folder` so it records discovery evidence alongside the job review seed rather than overwriting user-entered review fields.

Prefer a small, reversible hook:
- `scan_disc_folder(...)` calls `discover_job_title(...)`
- the result is stored as job-level metadata or audit payload
- the review UI can display it later

**Step 5: Re-run the tests**

Run:
```bash
uv run pytest tests/test_scanner.py tests/test_metadata_lookup.py -v
```
Expected: pass.

**Step 6: Commit**

```bash
git add disc_steward/scanner.py disc_steward/title_discovery.py tests/test_scanner.py tests/test_metadata_lookup.py
git commit -m "feat: extract deterministic title evidence"
```

---

## Task 3: Add local Ollama ranking for ambiguous titles

**Objective:** Use a small local model only after deterministic extraction, to rank or normalize title candidates.

**Files:**
- Create: `disc_steward/title_discovery.py` or extend it
- Modify: `disc_steward/config.py`
- Modify: `disc_steward/llm.py` if the existing transport can be reused cleanly
- Modify: `disc_steward/metadata.py` if the model should feed the existing metadata candidate flow
- Test: `tests/test_phase4_cleanup_llm_status.py`
- Test: `tests/test_metadata_lookup.py`

**Step 1: Write failing tests**

Add tests that verify:
- a mocked Ollama response can refine two or more candidate titles into one preferred title;
- the code does not auto-apply a low-confidence guess;
- the model output is parsed defensively and falls back to deterministic evidence when malformed.

**Step 2: Run the targeted tests**

Run:
```bash
uv run pytest tests/test_metadata_lookup.py tests/test_phase4_cleanup_llm_status.py -v
```
Expected: fail before the model wrapper exists.

**Step 3: Implement a tiny model wrapper**

Use a narrow prompt and structured response format. The prompt should include only:
- candidate titles
- source signals
- content-type hints
- year guesses
- language hints

The response should be parsed into a small JSON object with fields like:
- `preferred_title`
- `original_title`
- `romanized_title`
- `translated_title`
- `content_type`
- `library_root`
- `confidence`
- `reason`

Set the bar low: if the model is ambiguous or the JSON is invalid, keep the deterministic candidate and flag the job for review.

**Step 4: Keep the model optional and memory-conscious**

Because the user has an 8 GB VRAM ceiling, avoid making the system depend on a large model. Prefer a compact model choice in the plan, and keep the code path compatible with the existing Ollama endpoint on Barnabas.

Good behavior:
- deterministic evidence first
- model only for normalization/ranking
- no silent overwrite of user-confirmed metadata

**Step 5: Re-run tests**

Run:
```bash
uv run pytest tests/test_metadata_lookup.py tests/test_phase4_cleanup_llm_status.py -v
```
Expected: pass.

**Step 6: Commit**

```bash
git add disc_steward/title_discovery.py disc_steward/config.py disc_steward/llm.py disc_steward/metadata.py tests/test_metadata_lookup.py tests/test_phase4_cleanup_llm_status.py
git commit -m "feat: add local title ranking"
```

---

## Task 4: Pre-fill the review UI and add a one-step confirm/continue path

**Objective:** Make the review step the only human action, while keeping the user in control of the final decision.

**Files:**
- Modify: `disc_steward/web.py`
- Modify: `disc_steward/review.py`
- Modify: `disc_steward/metadata.py`
- Modify: `disc_steward/models.py` if review state needs extra fields
- Test: `tests/test_web_metadata_playback.py`
- Test: `tests/test_phase2_review.py`
- Test: `tests/test_phase4_cleanup_llm_status.py`

**Step 1: Write failing tests**

Add tests that verify:
- the review page pre-populates title/year/content type from discovery results when available;
- the page shows the evidence trail and confidence;
- there is a single action that means “confirm this metadata and proceed”;
- low-confidence jobs stay in review rather than auto-advancing.

**Step 2: Run the review tests**

Run:
```bash
uv run pytest tests/test_phase2_review.py tests/test_web_metadata_playback.py tests/test_phase4_cleanup_llm_status.py -v
```
Expected: failure until the UI and review flow changes exist.

**Step 3: Implement the prefill flow**

Update the review handlers so the job view can:
- display discovered metadata in the form fields
- show a compact evidence summary per job and per file
- preserve manual edits as the source of truth

Add a single explicit confirm action if useful, but avoid “auto-submit” behavior that could surprise the user.

**Step 4: Keep the confirmation step safe**

On confirm, validate the same invariants already enforced today:
- required metadata exists
- final paths are conflict-free
- included files have roles, encoding profiles, and subtitle policies

The new behavior should be “pre-fill and confirm,” not “pre-fill and skip validation.”

**Step 5: Re-run tests**

Run:
```bash
uv run pytest tests/test_phase2_review.py tests/test_web_metadata_playback.py tests/test_phase4_cleanup_llm_status.py -v
```
Expected: pass.

**Step 6: Commit**

```bash
git add disc_steward/web.py disc_steward/review.py disc_steward/metadata.py tests/test_phase2_review.py tests/test_web_metadata_playback.py tests/test_phase4_cleanup_llm_status.py
git commit -m "feat: prefill review metadata from discovery"
```

---

## Task 5: Add an automatic watcher/runner for new disc folders

**Objective:** Detect completed rips automatically and queue them for scanning without manual CLI invocation.

**Files:**
- Modify: `disc_steward/cli.py`
- Modify: `disc_steward/__main__.py` if needed
- Create: `disc_steward/watcher.py`
- Modify: `README.md`
- Test: `tests/test_scanner.py`
- Test: add a new watcher test file if needed, for example `tests/test_watcher.py`

**Step 1: Write failing tests**

Add tests for one of these approaches:
- a poller that scans `raw_rip_path` on a timer and avoids duplicate jobs
- a watch command that exits cleanly on interruption

The test should assert that two scans of the same folder still produce one job, which is already the codebase’s preferred idempotency shape.

**Step 2: Run the tests**

Run:
```bash
uv run pytest tests/test_scanner.py -v
```
Expected: pass for idempotency tests, fail for new watcher tests until implemented.

**Step 3: Implement the smallest useful runner**

Prefer the simplest maintainable choice:
- a `watch` subcommand in `disc_steward/cli.py`
- a loop that calls `scan_completed_rips`
- a sleep interval from config or CLI flag
- clean shutdown handling

If file-system notifications are easy to add without extra dependencies, great; otherwise a poller is acceptable and much simpler to support. On Barnabas, the worker stage should enter the Docker container before invoking ffmpeg so the host stays Docker-first.

**Step 4: Document deployment**

Add a short README section showing how to run it under systemd or another service manager on the control-plane host.

**Step 5: Re-run tests**

Run:
```bash
uv run pytest tests/test_scanner.py -v
```
Expected: pass.

**Step 6: Commit**

```bash
git add disc_steward/cli.py disc_steward/__main__.py disc_steward/watcher.py README.md tests/test_scanner.py
git commit -m "feat: add automatic scan watcher"
```

---

## Task 6: End-to-end automation and safety verification

**Objective:** Prove the whole path works from new disc folder to Jellyfin refresh with only the final metadata verification step requiring input, while keeping the heavy encode on Barnabas’s Dockerized processing stack.

**Files:**
- Modify: `tests/test_phase3_pipeline.py`
- Modify: `tests/test_phase4_cleanup_llm_status.py`
- Modify: `README.md`
- Create: `docs/automation.md` if a deeper operator guide is useful

**Step 1: Write an end-to-end test**

Build one test that simulates:
- a disc folder appearing
- scan and discovery running automatically
- review metadata being prefilled
- confirm action creating work orders
- validation passing
- transfer succeeding
- Jellyfin refresh being requested when enabled

Use mocks or local fixtures where necessary, but keep the flow realistic.

**Step 2: Run only the end-to-end test**

Run:
```bash
uv run pytest tests/test_phase3_pipeline.py -v
```
Expected: failure until the automation wiring is complete.

**Step 3: Implement the integration glue**

Make sure the new automation path does not bypass existing safety gates:
- no direct transfer before validation
- no work-order creation before review confirmation
- no metadata auto-commit without a confidence threshold or explicit confirmation

**Step 4: Verify against a real disc rip**

Use the Batman 1989 rip or another real test folder and confirm:
- the title is discovered
- the review page is prefilled
- the human confirmation step is the only manual action
- the rest completes without intervention
- the encode actually runs on Barnabas via the Dockerized path

**Step 5: Re-run the full suite**

Run:
```bash
uv run pytest -v
```
Expected: all tests pass.

**Step 6: Commit**

```bash
git add tests/test_phase3_pipeline.py tests/test_phase4_cleanup_llm_status.py README.md docs/automation.md disc_steward/*
git commit -m "feat: automate disc steward pipeline end to end"
```

---

## Verification checklist

- [ ] New disc folders are discovered automatically.
- [ ] Deterministic metadata is collected before any model call.
- [ ] Local Ollama only ranks/normalizes ambiguous candidates.
- [ ] The review UI is prefilled, not empty.
- [ ] The user still has one clear confirmation step.
- [ ] Process → validate → transfer → Jellyfin refresh still require passing safety gates.
- [ ] The heavy encode runs on Barnabas’s Dockerized processing environment, not on Gospel.
- [ ] Existing tests still pass.
- [ ] A real disc rip can complete with no manual steps beyond final metadata confirmation.

## Implementation note

Keep the first pass conservative. If any automation choice threatens safety or makes the flow hard to reason about, prefer a slower but explicit path. The goal is not to remove the human from the loop entirely; it is to remove the repetitive parts and leave only the final judgment call.
