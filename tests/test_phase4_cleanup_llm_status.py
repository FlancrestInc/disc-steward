from __future__ import annotations

from pathlib import Path

from disc_steward.cleanup import execute_cleanup, plan_cleanup
from disc_steward.config import AppConfig, CleanupConfig, LLMConfig, MetadataConfig, config_from_dict
from disc_steward.db import Database
from disc_steward.llm import build_disc_job_packet, request_suggestions
from disc_steward.metadata import metadata_provider_status
from disc_steward.models import AudioStream, FileReviewDecision, JobReviewMetadata, ScannedFile, SubtitleStream, VideoInfo
from disc_steward.status import build_status_summary
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
    assert config.jellyfin_logs.enabled is False


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
