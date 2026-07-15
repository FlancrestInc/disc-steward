from __future__ import annotations

from pathlib import Path
import sqlite3
from dataclasses import asdict

from disc_steward.cleanup import execute_cleanup, plan_cleanup
from disc_steward.config import AppConfig, CleanupConfig, LLMConfig, MetadataConfig, config_from_dict
from disc_steward.db import Database
from disc_steward.llm import build_disc_job_packet, request_suggestions
from disc_steward.metadata import metadata_provider_status
from disc_steward.models import AudioStream, FileReviewDecision, JobReviewMetadata, ScannedFile, SubtitleStream, TitleDiscoveryResult, TitleDiscoverySignal, VideoInfo
from disc_steward.status import build_status_summary
from disc_steward.web import render_job_review
from disc_steward.work_orders import generate_final_paths


def _config(tmp_path: Path) -> AppConfig:
    config = AppConfig.default_for_root(tmp_path)
    config.validation_needed_path = tmp_path / "media-pipeline" / "06_validation_needed"
    config.eddy_library_roots = {"Movies": tmp_path / "eddy" / "Movies"}
    config.cleanup = CleanupConfig(enabled=False, dry_run=True, raw_rip_retention_days_after_import=0, working_file_retention_days_after_import=0)
    return config


def _source(path: Path) -> ScannedFile:
    return ScannedFile(
        path=str(path),
        filename=path.name,
        parent_disc_folder=str(path.parent),
        size_bytes=100,
        modified_time=1.0,
        duration_seconds=100.0,
        container_format="matroska,webm",
        video=VideoInfo(codec="hevc"),
        audio_streams=[AudioStream(index=1, codec="dts", language="jpn")],
        subtitle_streams=[SubtitleStream(index=2, codec="ass", language="eng", title="A" * 1000)],
    )


def _imported_job(tmp_path: Path, config: AppConfig) -> tuple[Database, int, int, Path, Path, Path]:
    db = Database(tmp_path / "disc_steward.sqlite3")
    db.initialize()
    disc = tmp_path / "media-pipeline" / "01_disc_rips_raw" / "DISC"
    disc.mkdir(parents=True)
    raw = disc / "movie.mkv"
    raw.write_bytes(b"raw")
    working_dir = config.fileflows_working_path / "job_1"
    working_dir.mkdir(parents=True)
    working = working_dir / "movie-working.mkv"
    working.write_bytes(b"working")
    job_id = db.upsert_job(disc, "imported_to_jellyfin")
    source_id = db.upsert_source_file(job_id, _source(raw))
    review = JobReviewMetadata(job_id=job_id, title="Test Movie", year=2001, content_type="movie", library_root="Movies", review_status="reviewed")
    db.save_job_review(review)
    final_path = generate_final_paths(
        config,
        review,
        [FileReviewDecision(source_file_id=source_id, role="main_feature", content_type="movie", final_display_name="Test Movie")],
    )[source_id].final_path
    final_path.parent.mkdir(parents=True)
    final_path.write_bytes(b"final")
    validation = {
        "job_id": job_id,
        "status": "validated",
        "passed": True,
        "warnings": [],
        "items": [
            {
                "source_file_id": source_id,
                "expected_output_name": final_path.name,
                "expected_final_path": str(final_path),
                "matched_output_path": str(working),
                "status": "passed",
                "warnings": [],
                "errors": [],
            }
        ],
    }
    db.save_validation_summary(job_id, validation, True)
    db.save_transfer_summary(
        job_id,
        {
            "job_id": job_id,
            "status": "imported_to_jellyfin",
            "warnings": [],
            "items": [{"source_file_id": source_id, "source_output_path": str(working), "incoming_path": "", "final_path": str(final_path), "status": "placed"}],
        },
    )
    db.update_job_status(job_id, "imported_to_jellyfin")
    return db, job_id, source_id, raw, working, final_path


def test_config_plumbing_keeps_phase4_risky_features_disabled_by_default():
    config = config_from_dict({})

    assert config.cleanup.enabled is False
    assert config.cleanup.dry_run is True
    assert config.metadata.enabled is False
    assert config.llm.enabled is False
    assert config.llm.allow_shell_commands is False
    assert config.title_discovery.enabled is False
    assert config.title_discovery.provider == "ollama"
    assert config.title_discovery.min_confidence_to_auto_fill == 0.75
    assert config.jellyfin_logs.enabled is False


def test_review_page_prefills_title_discovery_fields_and_primary_continue_action(tmp_path):
    config = _config(tmp_path)
    db = Database(tmp_path / "disc_steward.sqlite3")
    db.initialize()
    disc = tmp_path / "media-pipeline" / "01_disc_rips_raw" / "Batman 1989"
    disc.mkdir(parents=True)
    media = disc / "title_t00.mkv"
    media.write_bytes(b"raw")
    job_id = db.upsert_job(disc, "review_needed")
    db.upsert_source_file(job_id, _source(media))
    review = JobReviewMetadata(
        job_id=job_id,
        title="Batman 1989",
        content_type="unknown",
        library_root="Movies",
        review_status="review_needed",
        title_discovery_json={
            "title": "Batman (1989)",
            "original_title": "Batman",
            "content_type": "movie",
            "library_root": "Movies",
            "confidence": 0.96,
            "signals": [{"source": "embedded_title", "value": "Batman", "confidence": 0.9}],
            "warnings": ["normalized from disc art"],
        },
    )
    db.save_job_review(review)

    html = render_job_review(db, config, job_id)

    assert "Batman (1989)" in html
    assert "normalized from disc art" in html
    assert 'name="confidence" value="0.96"' in html
    assert "Confirm and queue ffmpeg" in html


def test_title_discovery_config_plumbing_supports_separate_local_inference_settings():
    config = config_from_dict(
        {
            "title_discovery": {
                "enabled": True,
                "provider": "ollama",
                "endpoint": "http://localhost:11434",
                "model": "llama3.2:3b",
                "min_confidence_to_auto_fill": 0.9,
                "max_candidates": 7,
            }
        }
    )

    assert config.title_discovery.enabled is True
    assert config.title_discovery.endpoint == "http://localhost:11434"
    assert config.title_discovery.model == "llama3.2:3b"
    assert config.title_discovery.min_confidence_to_auto_fill == 0.9
    assert config.title_discovery.max_candidates == 7
    assert config.llm.enabled is False


def _legacy_job_review_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE disc_jobs (
            id INTEGER PRIMARY KEY,
            disc_title TEXT NOT NULL,
            disc_path TEXT NOT NULL UNIQUE,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE job_reviews (
            job_id INTEGER PRIMARY KEY REFERENCES disc_jobs(id) ON DELETE CASCADE,
            title TEXT NOT NULL DEFAULT '',
            original_title TEXT,
            romanized_title TEXT,
            translated_title TEXT,
            language_script_hints TEXT,
            anime_flag INTEGER NOT NULL DEFAULT 0,
            japanese_media_flag INTEGER NOT NULL DEFAULT 0,
            confidence REAL,
            manual_review_notes TEXT,
            year INTEGER,
            content_type TEXT NOT NULL DEFAULT 'unknown',
            library_root TEXT NOT NULL DEFAULT 'Movies',
            imdb_id TEXT,
            tmdb_id TEXT,
            tvdb_id TEXT,
            anidb_id TEXT,
            anilist_id TEXT,
            mal_id TEXT,
            notes TEXT,
            review_status TEXT NOT NULL DEFAULT 'review_needed',
            work_order_folder TEXT,
            work_order_created_at TEXT,
            warnings_json TEXT NOT NULL DEFAULT '[]',
            conflicts_json TEXT NOT NULL DEFAULT '[]',
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        """
    )


def test_job_review_title_discovery_json_round_trips_through_sqlite_migration(tmp_path):
    db_path = tmp_path / "disc_steward.sqlite3"
    conn = sqlite3.connect(db_path)
    try:
        _legacy_job_review_schema(conn)
        conn.commit()
    finally:
        conn.close()

    db = Database(db_path)
    db.initialize()

    with db.connect() as conn:
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(job_reviews)").fetchall()}
    assert "title_discovery_json" in columns

    disc_path = tmp_path / "media-pipeline" / "01_disc_rips_raw" / "DISC"
    disc_path.mkdir(parents=True)
    job_id = db.upsert_job(disc_path, "reviewed")
    discovery = TitleDiscoveryResult(
        title="Example Movie",
        original_title="元の題名",
        confidence=0.93,
        signals=[
            TitleDiscoverySignal(source="embedded_title", value="Example Movie", confidence=0.95, notes=["matches filename"]),
        ],
        warnings=["low sample count"],
    )
    review = JobReviewMetadata(job_id=job_id, title="Example Movie", year=2001, content_type="movie", title_discovery_json=asdict(discovery))

    db.save_job_review(review)
    loaded = db.get_job_review(job_id)

    assert loaded.title_discovery_json == asdict(discovery)
    assert loaded.title == "Example Movie"
    assert loaded.year == 2001


def test_metadata_provider_disabled_behavior():
    config = MetadataConfig()

    status = metadata_provider_status(config)

    assert status["enabled"] is False
    assert status["providers"]["tmdb"]["configured"] is False
    assert status["providers"]["anilist"]["configured"] is False


def test_llm_packet_truncation_and_disabled_behavior(tmp_path):
    config = _config(tmp_path)
    config.llm = LLMConfig(enabled=False, max_items_per_request=1, max_chars_per_field=12, allow_full_subtitle_text=False)
    db, job_id, _source_id, _raw, _working, _final = _imported_job(tmp_path, config)

    packet = build_disc_job_packet(db, config, job_id)
    result = request_suggestions(db, config, job_id)

    assert len(packet["files"]) == 1
    assert packet["files"][0]["subtitle_summary"][0]["title"].endswith("...")
    assert result["enabled"] is False
    assert db.list_llm_suggestions(job_id) == []


def test_llm_suggestion_stored_but_not_applied_automatically(tmp_path):
    config = _config(tmp_path)
    config.llm = LLMConfig(enabled=True, endpoint="http://hermes.invalid", max_items_per_request=1)
    db, job_id, source_id, _raw, _working, _final = _imported_job(tmp_path, config)

    result = request_suggestions(
        db,
        config,
        job_id,
        sender=lambda _endpoint, _packet: {"suggestions": [{"type": "metadata_match_suggestion", "source_file_id": source_id, "title": "Suggested"}]},
    )

    assert result["suggestions"][0]["title"] == "Suggested"
    assert db.list_llm_suggestions(job_id)[0]["status"] == "suggested"
    assert db.get_job_review(job_id).title == "Test Movie"


def test_cleanup_plan_eligibility_after_successful_import(tmp_path):
    config = _config(tmp_path)
    config.cleanup.delete_raw_rips = True
    config.cleanup.delete_working_files = True
    db, job_id, _source_id, raw, working, _final = _imported_job(tmp_path, config)

    summary = plan_cleanup(db, config)

    paths = {item.path for item in summary.eligible}
    assert str(raw) in paths
    assert str(working) in paths
    assert db.list_cleanup_eligibility(job_id)


def test_cleanup_plan_ineligibility_before_validation_or_import(tmp_path):
    config = _config(tmp_path)
    db, job_id, _source_id, raw, _working, _final = _imported_job(tmp_path, config)
    db.update_job_status(job_id, "reviewed")

    summary = plan_cleanup(db, config)

    assert str(raw) in {item.path for item in summary.ineligible}
    assert any("job has not completed final import" in item.reason for item in summary.ineligible)


def test_cleanup_hold_prevents_cleanup(tmp_path):
    config = _config(tmp_path)
    config.cleanup.delete_raw_rips = True
    db, job_id, _source_id, raw, _working, _final = _imported_job(tmp_path, config)
    db.set_cleanup_hold(job_id, True, "keep this one")

    summary = plan_cleanup(db, config)

    assert str(raw) in {item.path for item in summary.ineligible}
    assert any("cleanup hold" in item.reason for item in summary.ineligible)


def test_cleanup_dry_run_does_not_delete(tmp_path):
    config = _config(tmp_path)
    config.cleanup.enabled = True
    config.cleanup.dry_run = True
    config.cleanup.delete_raw_rips = True
    db, _job_id, _source_id, raw, _working, _final = _imported_job(tmp_path, config)

    summary = execute_cleanup(db, config)

    assert summary.deleted == []
    assert raw.exists()
    assert summary.dry_run is True


def test_archive_path_verification_behavior(tmp_path):
    config = _config(tmp_path)
    config.cleanup.archive_raw_rips_to_eddy = True
    config.cleanup.delete_raw_rips = True
    config.cleanup.raw_rip_archive_path = str(tmp_path / "eddy" / "Raw Archive")
    db, _job_id, _source_id, raw, _working, _final = _imported_job(tmp_path, config)

    summary = plan_cleanup(db, config)

    raw_item = next(item for item in summary.eligible if item.path == str(raw))
    assert raw_item.archive_path.endswith("Raw Archive/DISC/movie.mkv")
    assert raw.exists()


def test_status_command_summary_counts_jobs_and_outstanding_issues(tmp_path):
    config = _config(tmp_path)
    config.cleanup.delete_raw_rips = True
    db, job_id, _source_id, _raw, _working, _final = _imported_job(tmp_path, config)
    db.save_subtitle_plan(_source_id, {"statuses": ["needs_ocr_to_srt"], "warnings": ["needs OCR"]})
    plan_cleanup(db, config)

    summary = build_status_summary(db, config)

    assert summary["jobs_discovered"] == 1
    assert summary["jobs_imported"] == 1
    assert summary["subtitle_issues_outstanding"] == 1
    assert summary["cleanup_eligible_items"] >= 1
