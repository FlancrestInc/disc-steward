# Disc Steward

Disc Steward is a safe, observable control-plane service for a Barnabas-to-Eddy media pipeline. The recommended deployment runs Disc Steward on Gospel. Barnabas ingests and processes ripped discs with MakeMKV and FileFlows; Eddy stores the final Jellyfin-ready library and runs Jellyfin.

The current version implements Phases 1-4 safety scaffolding: SQLite state on the controller, configurable path mappings, MKV scanning with `ffprobe`, rule-based classification, static HTML reports, an interactive local review UI, Jellyfin-style final path previews, FileFlows-ready work-order JSON with subtitle plans, FileFlows output validation through Gospel's Barnabas mount, transfer to Eddy incoming storage through Gospel's Eddy mount, verified final placement, optional Jellyfin refresh, cleanup planning, dry-run cleanup execution, status reporting, metadata provider placeholders, and disabled-by-default Hermes/LLM suggestion hooks.

## Workflow

1. MakeMKV writes completed disc rips to Barnabas raw staging.
2. Disc Steward runs on Gospel and scans each disc folder through Gospel's Barnabas mount.
3. Scan metadata and classifications are stored in SQLite.
4. Review decisions and metadata are entered in the local web UI.
5. Approved jobs generate FileFlows work-order JSON under Gospel's Barnabas mount, using Barnabas-native paths inside the JSON.
6. FileFlows writes processed outputs back to Barnabas under each work order's `barnabas_validation_output_dir`.
7. Disc Steward validates each output against the work order and source scan.
8. Validated files are copied through Gospel's Eddy mount to Eddy `.incoming/disc-steward/job_<job_id>/`, verified, then moved into final Jellyfin library folders.
9. If configured, Disc Steward asks Jellyfin to refresh after final placement.

Raw rips stay on Barnabas. FileFlows outputs stay on Barnabas. Only final validated files are transferred to Eddy.

## Recommended Hosts

Use Gospel as the control-plane host:

- Gospel runs Disc Steward, its SQLite database, review UI, reports, validation orchestration, and local-mount transfer/final placement logic.
- Barnabas runs MakeMKV and FileFlows.
- Eddy stores the final Jellyfin media library and runs/serves Jellyfin.

In this deployment, Disc Steward config paths are controller paths: the paths Gospel uses to reach mounted Barnabas and Eddy filesystems. `path_mappings` translate those controller paths to the native paths that Barnabas/FileFlows and Eddy/Jellyfin use.

```yaml
pipeline_root: /mnt/Barnabas/data2/media-pipeline
database_path: /var/lib/disc-steward/disc_steward.sqlite3

paths:
  raw_rip_path: /mnt/Barnabas/data2/media-pipeline/01_disc_rips_raw
  fileflows_work_order_path: /mnt/Barnabas/data2/media-pipeline/04_ready_for_fileflows
  validation_needed_path: /mnt/Barnabas/data2/media-pipeline/06_validation_needed

path_mappings:
  barnabas:
    - controller_path: /mnt/Barnabas/data2/media-pipeline
      barnabas_path: /mnt/data2/media-pipeline
  eddy:
    - controller_path: /mnt/Eddy/jellyfin-media
      eddy_path: /mnt/jellyfin-media

transfer:
  method: local_mount
  eddy_incoming_root: /mnt/Eddy/jellyfin-media/.incoming/disc-steward
  eddy_final_roots:
    Movies: /mnt/Eddy/jellyfin-media/Movies
```

With this setup, the scanner reads `controller_path` values, FileFlows work orders use Barnabas-native `source_path` and `barnabas_validation_output_dir`, and final library paths stored in work orders and validation/transfer records use Eddy-native paths. Missing mapped controller roots are treated as unavailable mounts rather than evidence that media was deleted.

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
python -m disc_steward cleanup-plan --config config.yaml
python -m disc_steward cleanup --config config.yaml
python -m disc_steward status --config config.yaml
```

`serve` starts the interactive review UI at `http://127.0.0.1:8765` by default. `prepare-fileflows` creates local work-order JSON for jobs that have passed review. `validate` inspects FileFlows outputs for a reviewed job and records pass/fail details. `transfer` copies only validated outputs to Eddy incoming storage, verifies them, and performs final placement. `cleanup-plan` never changes files. `cleanup` only runs when explicitly enabled, respects dry-run, and refuses ambiguous jobs. `status` prints a post-import dashboard summary.

## Review UI

Run:

```bash
python -m disc_steward serve --config config.yaml
```

The job list shows each scanned disc folder, status, file count, likely main feature, probable extras, subtitle issues, transcode-risk issues, and review status. Open a job to review all ripped files grouped as main feature candidates, possible episodes, extras, trailers/promos, featurettes/documentaries, deleted scenes, menu/logo/bumper candidates, and manual review.

At the disc level, enter the title, original/romanized/translated titles, year, content type, library root/category, IMDb/TMDb/TVDb/AniDB/AniList/MAL IDs, Japanese/anime hints, and notes. Online metadata lookup remains optional scaffolding; IDs are stored exactly as entered unless a user later approves a suggestion.

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

The review page previews intended Jellyfin-style final paths before any work orders are created. In Gospel deployments it shows controller paths for source files and Eddy/Jellyfin-native final destination previews, with Gospel final-placement paths shown when they differ. It sanitizes path components, preserves safe Unicode such as Japanese titles, formats metadata IDs in filenames, detects duplicate generated paths, and refuses existing final paths as conflicts.

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

Each item JSON references the original `source_path` in Barnabas-native raw rip storage, the chosen role/content type, metadata IDs, encoding profile, subtitle policy, Barnabas-native validation output directory, Eddy/Jellyfin-native final intended library path, and preservation flags for original audio/subtitles.

Each item also includes a `subtitle_plan` block. Disc Steward plans, but does not perform, risky subtitle replacement. Plans prefer UTF-8 SRT where practical, preserve originals by default, warn about default image subtitles, preserve ASS/SSA for anime or styled content, suggest SRT fallback for ASS when configured, tag forced-subtitle candidates for review, and mark generated subtitles as unverified. Image subtitle OCR and ASS fallback conversion are work-order instructions for FileFlows or future helper scripts.

Configure FileFlows to write each processed file to the item's `barnabas_validation_output_dir`, preferably using the work-order `output_name`. If FileFlows changes the filename, Disc Steward can still match by a sidecar JSON containing `item_id` or `source_file_id`, or by a unique job-folder output, but it will record a warning.

Disc Steward does not call the FileFlows API. The JSON structure is intended for a watched-folder script or manual FileFlows setup.

## Subtitle Helpers

Phase 4 includes conservative helper scaffolds under `scripts/`:

- `extract_subtitles`
- `convert_ass_to_srt_fallback`
- `normalize_srt_utf8`
- `validate_srt`
- `tag_subtitle_streams`

Only `normalize_srt_utf8` and `validate_srt` perform simple sidecar-file work. The other helpers print planned intent and do not mutate media containers. They are placeholders for reviewed FileFlows integration, not automatic replacement tools.

## Validation

Run:

```bash
python -m disc_steward validate --config config.yaml --job-id 184
```

Validation checks that each expected output exists, is readable, probes with `ffprobe`, has close duration, has sane size, has required video/audio streams, resolves to one final library path, and does not collide with another job output. The `universal_h264_aac_srt` profile requires MKV-compatible container metadata, H.264 8-bit `yuv420p`, AAC fallback audio, and no default image subtitle. Subtitle-plan validation checks for expected SRT, language tags, forced flags, ASS preservation, and original preservation where it can. Uncertain OCR/fallback results are warnings, not hard failures, so they stay visible for review. Other profiles enforce only the checks that fit their purpose.

All validation results, warnings, detected streams, and ffprobe summaries are stored in SQLite and shown on the job page. Manual acceptance is available from the UI when enabled, requires a note, and does not bypass transfer conflict checks.

## Eddy Transfer

Run:

```bash
python -m disc_steward transfer --config config.yaml --job-id 184
```

Transfer only runs after validation passes or failed items are manually accepted with notes. Files first go to:

```text
/mnt/jellyfin-media/.incoming/disc-steward/job_<job_id>/
```

Disc Steward verifies the incoming copy using `transfer.verify` (`size` by default, optionally `sha256` or `none`). Only after verification does it create final directories if configured and move files into their final Eddy library paths through Gospel's Eddy mount. Existing destination files are conflicts when `allow_overwrite: false`.

Local mount configuration:

```yaml
transfer:
  method: local_mount
  eddy_incoming_root: /mnt/Eddy/jellyfin-media/.incoming/disc-steward
  verify: size
  allow_overwrite: false
  create_final_directories: true
```

Rsync configuration:

```yaml
transfer:
  method: rsync
  rsync_target: eddy:/mnt/jellyfin-media/.incoming/disc-steward
  ssh_options: []
  verify: size
  allow_overwrite: false
```

Rsync support transfers to Eddy incoming and records `requires_final_placement` unless a local final-placement path is available.

## Jellyfin Refresh

Jellyfin refresh is optional and disabled by default:

```yaml
jellyfin:
  enabled: false
  base_url: http://eddy:8096
  api_key: ""
  refresh_after_import: false
  library_ids: []
```

When enabled with `refresh_after_import: true`, Disc Steward triggers a full library refresh if `library_ids` is empty, or refreshes the listed libraries. API errors are recorded as warnings and do not undo a completed import.

## Metadata and Japanese/Anime Handling

Metadata lookup is optional and disabled by default. The config includes provider stubs for TMDb, TVDb, AniList, AniDB, MAL, and manual IMDb ID entry. No API keys are hardcoded, and offline/manual review remains the default path.

The review model stores original, romanized, and translated title fields plus Japanese/anime flags and language/script hints. Filenames can stay English while original Japanese titles remain in metadata. Disc Steward preserves Unicode safely, warns when Japanese/anime content is detected, preserves ASS subtitles by default when styling may matter, and recommends SRT fallback rather than replacing ASS. It does not auto-translate metadata without review.

## Hermes/LLM Assistance

LLM/Hermes support is suggestion-only and disabled by default. Disc Steward can build compact JSON packets with short summaries, limited file lists, truncated fields, subtitle summaries, confidence, and warnings. It does not include full ffprobe dumps, full logs, or full subtitle files. Suggestions are stored in SQLite as `suggested` and are not applied automatically.

LLM suggestions are allowed for metadata matches, Japanese title interpretation, extras naming, subtitle policy suggestions, failure summaries, and manual-review priority. They are never allowed to run shell commands, delete, move, rename, encode, transfer files, bypass validation, or bypass review.

## Cleanup and Status

Cleanup remains disabled by default:

```yaml
cleanup:
  enabled: false
  dry_run: true
  delete_raw_rips: false
  delete_working_files: false
```

`cleanup-plan` records eligible and ineligible files with reasons and never deletes anything. `cleanup` requires `cleanup.enabled: true`, honors `dry_run`, requires successful validation and final Eddy placement, requires final paths to exist, respects retention days, writes audit records, and refuses jobs on cleanup hold. Raw-rip archival can be configured, but source deletion only follows verified archive copy and explicit `delete_raw_rips: true`.

`status` summarizes discovered jobs, review queues, FileFlows waits, validation needs, transfer readiness, imported jobs, validation failures, transfer conflicts, subtitle issues, cleanup eligibility, and recent errors. The web UI shows the same operational posture on the dashboard/job pages.

## Jellyfin Log Scaffold

`jellyfin_logs` config is present for future transcode-log review. It is disabled by default and currently records no findings unless later parsing is added.

## Classification

Disc Steward records likely main features, extras, trailers, featurettes, menu/bumpers, episode candidates, alternate cuts, commentary variants, subtitle conversion needs, language-tag gaps, and Jellyfin transcoding risks.

The broad compatibility target is MKV, H.264 High 8-bit `yuv420p`, AAC fallback audio, SRT text subtitles where practical, preserved chapters, and no default image subtitles.

## Safety

- No destructive behavior is enabled by default.
- Repeated scans upsert by path and file identity rather than duplicating rows.
- Raw rips are not moved or renamed.
- Phase 2 creates only review database rows and small work-order files.
- FileFlows outputs are validated before transfer.
- Eddy transfer uses job-specific incoming staging and partial filenames.
- Final Jellyfin folders do not see partial transfers.
- Existing destination files are treated as conflicts unless overwrite is explicitly configured.
- Manual validation acceptance requires an audit note and still honors destination conflicts.
- Hermes/LLM integration is compact, suggestion-only, and disabled by default.
- Cleanup remains disabled by default, dry-run by default, and blocked by cleanup holds.

## Troubleshooting

Validation failures usually mean FileFlows wrote to the wrong folder, changed filenames without a sidecar, produced a duration mismatch, missed AAC fallback audio, left a default image subtitle, or generated an output that is too small to trust. Check the validation section on the job page; it records matched paths, ffprobe summaries, profile compliance, warnings, and errors.

Transfer conflicts usually mean the final Eddy path already exists, incoming staging has a leftover file, or verification failed. Resolve the existing destination manually, adjust metadata/final paths, or rerun transfer after clearing only the failed incoming staging file. Do not delete source rips or FileFlows outputs as part of conflict resolution.

## Tests

```bash
python -m pytest -q
```

The test suite covers ffprobe JSON parsing, classification rules, repeated scan idempotency, subtitle risk detection, subtitle plan generation, subtitle plan validation, movie/extra/show/special path generation, filename sanitization, metadata ID filename formatting, work-order JSON generation, review validation, FileFlows output matching, profile validation failures, audit logging, local Eddy transfer, conflict detection, verification behavior, Unicode/Japanese handling, cleanup planning/holds/dry-run behavior, LLM packet truncation and inert suggestions, status summaries, and Jellyfin refresh warning handling.

## Future Work

Future work includes real provider-backed metadata lookup, reviewed FileFlows helper integration for OCR/conversion/tagging, richer SRT parse/UTF-8 sidecar validation, Jellyfin transcode-log parsing, and broader UI flows for accepting/editing individual suggestions.
