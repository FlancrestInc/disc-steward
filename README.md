# Disc Steward

Disc Steward is a safe, observable control-plane service for a Barnabas-to-Eddy media pipeline. Barnabas ingests and processes ripped discs; Eddy stores the final Jellyfin-ready library.

The current version implements Phase 1 plus the Phase 2 review handoff: SQLite state, configurable paths, MKV scanning with `ffprobe`, rule-based classification, static HTML reports, an interactive local review UI, Jellyfin-style final path previews, and FileFlows-ready work-order JSON. It does not move, rename, delete, encode, or transfer source media.

## Workflow

1. MakeMKV writes completed disc rips to Barnabas raw staging.
2. Disc Steward scans each disc folder in place.
3. Scan metadata and classifications are stored in SQLite.
4. Review decisions and metadata are entered in the local web UI.
5. Approved jobs generate FileFlows work-order JSON in Barnabas `04_ready_for_fileflows`.
6. Later phases will validate FileFlows output, transfer final files to Eddy, and optionally trigger Jellyfin scans.

Raw rips stay on Barnabas. Active processing stays on Barnabas. Only final validated files should be transferred to Eddy.

## Install

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e ".[test]"
cp config.example.yaml config.yaml
```

Install `ffprobe` from FFmpeg and make sure it is on `PATH`, or set `ffprobe_path` in `config.yaml`.

## Commands

```bash
python -m disc_steward scan --config config.yaml
python -m disc_steward report --config config.yaml
python -m disc_steward serve --config config.yaml
python -m disc_steward prepare-fileflows --config config.yaml --job-id 184
python -m disc_steward validate --config config.yaml --job-id 184
python -m disc_steward transfer --config config.yaml --job-id 184
python -m disc_steward cleanup --config config.yaml
```

`serve` starts the interactive review UI at `http://127.0.0.1:8765` by default. `prepare-fileflows` creates local work-order JSON for jobs that have passed review. `validate` and `transfer` remain scaffolded for Phase 3. `cleanup` is disabled unless `cleanup_enabled: true`, and deletion is still not implemented.

## Review UI

Run:

```bash
python -m disc_steward serve --config config.yaml
```

The job list shows each scanned disc folder, status, file count, likely main feature, probable extras, subtitle issues, transcode-risk issues, and review status. Open a job to review all ripped files grouped as main feature candidates, possible episodes, extras, trailers/promos, featurettes/documentaries, deleted scenes, menu/logo/bumper candidates, and manual review.

At the disc level, enter the title, original title, year, content type, library root/category, IMDb/TMDb/TVDb/AniDB/AniList/MAL IDs, and notes. There is no online metadata lookup in Phase 2; IDs are stored exactly as entered.

At the file level, choose the role, display name, optional final filename override, content type, extra type, season/episode/sort order, encoding profile, subtitle policy, include/exclude decision, per-file metadata IDs, title overrides, and notes. Ignoring a file stores only a review decision; it does not delete or hide the source file permanently.

Subtitle policy suggestions are based on scan findings:

- Image subtitles such as PGS/VobSub suggest `ocr_image_subtitles_to_srt_preserve_original`.
- Default image subtitles show a clear warning because they can force Jellyfin burn-in/transcoding.
- Non-English audio without text subtitles suggests `generate_missing_srt_unverified`.
- ASS/SSA subtitles suggest `preserve_ass_add_srt_fallback`.
- Existing SRT/text subtitles suggest `preserve_existing`.

Encoding profiles and subtitle policies come from `config.yaml`. The example config includes:

- `remux_only`
- `universal_h264_aac_srt`
- `subtitle_fix_only`
- `h265_archive_friendly`
- `manual_review`

and:

- `preserve_existing`
- `prefer_srt_preserve_original`
- `ocr_image_subtitles_to_srt_preserve_original`
- `generate_missing_srt_unverified`
- `preserve_ass_add_srt_fallback`
- `manual_review`

The review page previews intended Jellyfin-style final paths before any work orders are created. It sanitizes path components, preserves safe Unicode such as Japanese titles, formats metadata IDs in filenames, detects duplicate generated paths, and refuses existing final paths as conflicts.

Review actions:

- `Save draft review` stores decisions and sets `review_in_progress`.
- `Mark reviewed` validates required fields and sets `reviewed`.
- `Create FileFlows work orders` saves the current form, validates it, sets `ready_for_fileflows`, and writes work-order JSON.
- `Send job to manual review` sets `manual_review`.
- `Reopen review` sets `review_in_progress`.

Validation before review/work-order creation requires at least one included file with a role, title/year for movie/show jobs, a movie main feature for movie jobs, conflict-free final paths, and selected encoding/subtitle policies for included files.

## FileFlows Work Orders

Approved work orders are written on Barnabas under:

```text
/mnt/data2/media-pipeline/04_ready_for_fileflows/job_<job_id>/
  job_manifest.json
  items/
    item_001.work_order.json
    item_002.work_order.json
```

Each item JSON references the original `source_path` in Barnabas raw rip storage, the chosen role/content type, metadata IDs, encoding profile, subtitle policy, Barnabas validation output directory, final intended Jellyfin library path, and preservation flags for original audio/subtitles.

Phase 2 does not copy large media files by default and does not call the FileFlows API. The JSON structure is ready for a watched-folder script or a later API integration.

## Classification

Disc Steward records likely main features, extras, trailers, featurettes, menu/bumpers, episode candidates, alternate cuts, commentary variants, subtitle conversion needs, language-tag gaps, and Jellyfin transcoding risks.

The broad compatibility target is MKV, H.264 High 8-bit `yuv420p`, AAC fallback audio, SRT text subtitles where practical, preserved chapters, and no default image subtitles.

## Safety

- No destructive behavior is enabled by default.
- Repeated scans upsert by path and file identity rather than duplicating rows.
- Raw rips are not moved or renamed.
- Phase 2 creates only review database rows and small work-order files.
- Phase 2 does not transfer files to Eddy, move files into Jellyfin, trigger Jellyfin imports, or invoke Hermes/LLM.
- Eddy transfer uses an incoming/partial staging design in the scaffold.
- Existing destination files are treated as conflicts unless overwrite is explicitly configured.
- Hermes/LLM integration is stubbed, compact, and disabled by default.

## Tests

```bash
python -m pytest -q
```

The test suite covers ffprobe JSON parsing, classification rules, repeated scan idempotency, subtitle risk detection, movie/extra/show/special path generation, filename sanitization, metadata ID filename formatting, work-order JSON generation, review validation, conflict detection, Unicode/Japanese handling, validation failure handling, and transfer conflict detection.

## Future Work

Phase 3 will validate FileFlows output on Barnabas, transfer only validated files to Eddy `.incoming`, verify transfer size/checksum, move into final Jellyfin folders, and optionally trigger Jellyfin API library scans.

Phase 4 will add subtitle conversion integration, online metadata lookup, compact Hermes assistance for Japanese titles and extras naming, and retention-based cleanup automation.
