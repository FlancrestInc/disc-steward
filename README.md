# Disc Steward

Disc Steward is a safe, observable control-plane service for a Barnabas-to-Eddy media pipeline. Barnabas ingests and processes ripped discs; Eddy stores the final Jellyfin-ready library.

The first version implements Phase 1: SQLite state, configurable paths, MKV scanning with `ffprobe`, rule-based classification, static HTML reports, and safe repeated scans. It does not move, rename, delete, encode, or transfer media by default.

## Workflow

1. MakeMKV writes completed disc rips to Barnabas raw staging.
2. Disc Steward scans each disc folder in place.
3. Scan metadata and classifications are stored in SQLite.
4. Static reports are written to the review folder.
5. Later phases will store review decisions, generate FileFlows work orders, validate output, transfer final files to Eddy, and optionally trigger Jellyfin scans.

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
python -m disc_steward.cli --config config.yaml scan
python -m disc_steward.cli --config config.yaml report
python -m disc_steward.cli --config config.yaml serve
python -m disc_steward.cli --config config.yaml prepare-fileflows --job-id 184
python -m disc_steward.cli --config config.yaml validate --job-id 184
python -m disc_steward.cli --config config.yaml transfer --job-id 184
python -m disc_steward.cli --config config.yaml cleanup
```

`prepare-fileflows`, `validate`, and `transfer` are scaffolded for later phases. `cleanup` is disabled unless `cleanup_enabled: true`, and deletion is not implemented in Phase 1.

## Classification

Disc Steward records likely main features, extras, trailers, featurettes, menu/bumpers, episode candidates, alternate cuts, commentary variants, subtitle conversion needs, language-tag gaps, and Jellyfin transcoding risks.

The broad compatibility target is MKV, H.264 High 8-bit `yuv420p`, AAC fallback audio, SRT text subtitles where practical, preserved chapters, and no default image subtitles.

## Safety

- No destructive behavior is enabled by default.
- Repeated scans upsert by path and file identity rather than duplicating rows.
- Raw rips are not moved or renamed in Phase 1.
- Eddy transfer uses an incoming/partial staging design in the scaffold.
- Existing destination files are treated as conflicts unless overwrite is explicitly configured.
- Hermes/LLM integration is stubbed, compact, and disabled by default.

## Tests

```bash
python -m pytest -q
```

The test suite covers ffprobe JSON parsing, classification rules, repeated scan idempotency, subtitle risk detection, final path generation, validation failure handling, and transfer conflict detection.

## Future Work

Phase 2 will add the interactive review UI, persisted user decisions, final naming, target library selection, and FileFlows work-order generation.

Phase 3 will validate FileFlows output on Barnabas, transfer only validated files to Eddy `.incoming`, verify transfer size/checksum, move into final Jellyfin folders, and optionally trigger Jellyfin API library scans.

Phase 4 will add subtitle conversion integration, online metadata lookup, compact Hermes assistance for Japanese titles and extras naming, and retention-based cleanup automation.
