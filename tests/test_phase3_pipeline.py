from __future__ import annotations

import json
from pathlib import Path
from urllib.error import HTTPError

from disc_steward.config import AppConfig, JellyfinConfig
from disc_steward.db import Database
from disc_steward.jellyfin import refresh_after_import
from disc_steward.models import AudioStream, FileReviewDecision, JobReviewMetadata, ScannedFile, SubtitleStream, VideoInfo
from disc_steward.transfer import transfer_job_to_eddy
from disc_steward.validation import validate_job_outputs
from disc_steward.work_orders import create_ffmpeg_processing_jobs, generate_final_paths


def _config(tmp_path: Path) -> AppConfig:
    config = AppConfig.default_for_root(tmp_path)
    config.fileflows_work_order_path = tmp_path / "media-pipeline" / "04_ready_for_fileflows"
    config.validation_needed_path = tmp_path / "media-pipeline" / "06_validation_needed"
    config.eddy_incoming_path = tmp_path / "eddy" / ".incoming" / "disc-steward"
    config.eddy_library_roots = {
        "Movies": tmp_path / "eddy" / "Movies",
        "Shows": tmp_path / "eddy" / "Shows",
        "Anime": tmp_path / "eddy" / "Anime",
        "Family Videos": tmp_path / "eddy" / "Family Videos",
    }
    config.dry_run = False
    config.duration_tolerance_seconds = 3
    return config


def _source(media_path: Path, duration: float = 120.0, size: int = 4000) -> ScannedFile:
    return ScannedFile(
        path=str(media_path),
        filename=media_path.name,
        parent_disc_folder=str(media_path.parent),
        size_bytes=size,
        modified_time=1.0,
        duration_seconds=duration,
        container_format="matroska,webm",
        video=VideoInfo(codec="hevc", profile="Main 10", pixel_format="yuv420p10le", bit_depth=10, width=1920, height=1080),
        audio_streams=[AudioStream(index=1, codec="truehd", language="jpn")],
        subtitle_streams=[SubtitleStream(index=2, codec="hdmv_pgs_subtitle", language="eng", default=True)],
    )


def _output_ffprobe(*, duration: float = 120.0, codec: str = "h264", audio: list[str] | None = None, subtitles: list[dict] | None = None) -> str:
    streams = [
        {
            "index": 0,
            "codec_type": "video",
            "codec_name": codec,
            "profile": "High",
            "pix_fmt": "yuv420p",
            "bits_per_raw_sample": "8",
            "width": 1920,
            "height": 1080,
            "avg_frame_rate": "24000/1001",
            "r_frame_rate": "24000/1001",
        }
    ]
    for index, codec_name in enumerate(audio or ["aac", "truehd"], start=1):
        streams.append(
            {
                "index": index,
                "codec_type": "audio",
                "codec_name": codec_name,
                "channels": 2,
                "channel_layout": "stereo",
                "tags": {"language": "eng"},
            }
        )
    for offset, subtitle in enumerate(subtitles or [{"codec_name": "subrip", "default": 1}], start=10):
        streams.append(
            {
                "index": offset,
                "codec_type": "subtitle",
                "codec_name": subtitle["codec_name"],
                "disposition": {"default": subtitle.get("default", 0), "forced": 0},
                "tags": {"language": "eng"},
            }
        )
    return json.dumps({"format": {"duration": str(duration), "format_name": "matroska,webm", "size": "4096"}, "streams": streams})


def _reviewed_job(tmp_path: Path, config: AppConfig) -> tuple[Database, int, int, Path]:
    db = Database(tmp_path / "disc_steward.sqlite3")
    db.initialize()
    disc = tmp_path / "media-pipeline" / "01_disc_rips_raw" / "SPIRITED_AWAY"
    disc.mkdir(parents=True)
    source_path = disc / "title_t00.mkv"
    source_path.write_bytes(b"source" * 800)
    job_id = db.upsert_job(disc, "reviewed")
    source_id = db.upsert_source_file(job_id, _source(source_path, size=source_path.stat().st_size))
    review = JobReviewMetadata(
        job_id=job_id,
        title="Spirited Away",
        year=2001,
        content_type="movie",
        library_root="Movies",
        imdb_id="tt0245429",
        review_status="reviewed",
    )
    db.save_job_review(review)
    path = generate_final_paths(
        config,
        review,
        [
            FileReviewDecision(
                source_file_id=source_id,
                role="main_feature",
                content_type="movie",
                final_display_name="Spirited Away",
                encoding_profile="universal_h264_aac_srt",
                subtitle_policy="ocr_image_subtitles_to_srt_preserve_original",
            )
        ],
    )[source_id].final_path
    db.save_file_review(
        FileReviewDecision(
            source_file_id=source_id,
            role="main_feature",
            content_type="movie",
            final_display_name="Spirited Away",
            encoding_profile="universal_h264_aac_srt",
            subtitle_policy="ocr_image_subtitles_to_srt_preserve_original",
            generated_final_path=str(path),
        )
    )
    previous_dry_run = config.dry_run
    config.dry_run = True
    try:
        create_ffmpeg_processing_jobs(db, config, job_id, ffmpeg_runner=lambda command: Path(command[-1]).write_bytes(b"ffmpeg-output" * 300))
    finally:
        config.dry_run = previous_dry_run
    return db, job_id, source_id, path


def _write_output(config: AppConfig, job_id: int, final_path: Path, data: bytes = b"output" * 900) -> Path:
    output = config.validation_needed_path / f"job_{job_id}" / final_path.name
    output.parent.mkdir(parents=True)
    output.write_bytes(data)
    return output


def test_validate_job_passes_universal_h264_aac_srt_and_records_audit(tmp_path):
    config = _config(tmp_path)
    db, job_id, source_id, final_path = _reviewed_job(tmp_path, config)
    output = _write_output(config, job_id, final_path)

    summary = validate_job_outputs(db, config, job_id, ffprobe_runner=lambda path: _output_ffprobe())

    assert summary.passed is True
    assert summary.status == "validated"
    assert summary.items[0].source_file_id == source_id
    assert summary.items[0].matched_output_path == str(output)
    assert summary.items[0].profile_compliance["video_codec"] == "pass"
    assert db.get_job(job_id).status == "transfer_ready"
    assert db.latest_validation_summary(job_id)["status"] == "validated"
    assert db.list_audit_events(job_id)[-1]["event_type"] == "validation_passed"


def test_validate_job_fails_when_output_missing(tmp_path):
    config = _config(tmp_path)
    db, job_id, _source_id, _final_path = _reviewed_job(tmp_path, config)

    summary = validate_job_outputs(db, config, job_id, ffprobe_runner=lambda path: _output_ffprobe())

    assert summary.passed is False
    assert summary.status == "validation_failed"
    assert "missing output" in "; ".join(summary.items[0].errors)


def test_validate_job_fails_when_duration_exceeds_tolerance(tmp_path):
    config = _config(tmp_path)
    db, job_id, _source_id, final_path = _reviewed_job(tmp_path, config)
    _write_output(config, job_id, final_path)

    summary = validate_job_outputs(db, config, job_id, ffprobe_runner=lambda path: _output_ffprobe(duration=130.0))

    assert summary.passed is False
    assert any("duration differs" in error for error in summary.items[0].errors)


def test_validate_job_warns_when_filename_differs_but_matches_item_id_sidecar(tmp_path):
    config = _config(tmp_path)
    db, job_id, source_id, _final_path = _reviewed_job(tmp_path, config)
    renamed = config.validation_needed_path / f"job_{job_id}" / "fileflows-renamed-output.mkv"
    renamed.parent.mkdir(parents=True)
    renamed.write_bytes(b"output" * 900)
    (renamed.with_suffix(".json")).write_text(json.dumps({"item_id": source_id}), encoding="utf-8")

    summary = validate_job_outputs(db, config, job_id, ffprobe_runner=lambda path: _output_ffprobe())

    assert summary.passed is True
    assert summary.items[0].matched_output_path == str(renamed)
    assert any("filename differs" in warning for warning in summary.items[0].warnings)


def test_validate_job_fails_when_aac_missing_and_default_image_subtitle_present(tmp_path):
    config = _config(tmp_path)
    db, job_id, _source_id, final_path = _reviewed_job(tmp_path, config)
    _write_output(config, job_id, final_path)

    summary = validate_job_outputs(
        db,
        config,
        job_id,
        ffprobe_runner=lambda path: _output_ffprobe(audio=["truehd"], subtitles=[{"codec_name": "hdmv_pgs_subtitle", "default": 1}]),
    )

    assert summary.passed is False
    messages = "; ".join(summary.items[0].errors)
    assert "AAC fallback audio is missing" in messages
    assert "image subtitle is marked default" in messages


def test_transfer_uses_job_incoming_then_places_final_file_and_records_audit(tmp_path):
    config = _config(tmp_path)
    db, job_id, _source_id, final_path = _reviewed_job(tmp_path, config)
    output = _write_output(config, job_id, final_path, b"validated-output" * 300)
    validate_job_outputs(db, config, job_id, ffprobe_runner=lambda path: _output_ffprobe())

    summary = transfer_job_to_eddy(db, config, job_id)

    assert summary.status == "imported_to_jellyfin"
    assert summary.items[0].incoming_path == str(config.eddy_incoming_path / f"job_{job_id}" / final_path.name)
    assert summary.items[0].final_path == str(final_path)
    assert final_path.read_bytes() == output.read_bytes()
    assert not (config.eddy_incoming_path / f"job_{job_id}" / f"{final_path.name}.partial").exists()
    assert db.get_job(job_id).status == "imported_to_jellyfin"
    assert db.latest_transfer_summary(job_id)["status"] == "imported_to_jellyfin"
    assert db.list_audit_events(job_id)[-1]["event_type"] == "transfer_completed"


def test_transfer_detects_existing_final_path_when_overwrite_false(tmp_path):
    config = _config(tmp_path)
    db, job_id, _source_id, final_path = _reviewed_job(tmp_path, config)
    _write_output(config, job_id, final_path)
    validate_job_outputs(db, config, job_id, ffprobe_runner=lambda path: _output_ffprobe())
    final_path.parent.mkdir(parents=True)
    final_path.write_bytes(b"existing")

    summary = transfer_job_to_eddy(db, config, job_id)

    assert summary.status == "transfer_conflict"
    assert summary.items[0].conflict == "destination already exists"
    assert final_path.read_bytes() == b"existing"


def test_transfer_sha256_verification_detects_changed_incoming_copy(tmp_path):
    config = _config(tmp_path)
    config.transfer_verify = "sha256"
    db, job_id, _source_id, final_path = _reviewed_job(tmp_path, config)
    _write_output(config, job_id, final_path, b"validated-output" * 300)
    validate_job_outputs(db, config, job_id, ffprobe_runner=lambda path: _output_ffprobe())

    def corrupt_copy(source: Path, destination: Path) -> None:
        destination.write_bytes(source.read_bytes() + b"corrupt")

    summary = transfer_job_to_eddy(db, config, job_id, copy_file=corrupt_copy)

    assert summary.status == "failed"
    assert "verification failed" in summary.items[0].error
    assert not final_path.exists()


def test_jellyfin_disabled_records_skipped_without_error(tmp_path):
    db = Database(tmp_path / "disc_steward.sqlite3")
    db.initialize()
    job_id = db.upsert_job(tmp_path / "disc", "imported_to_jellyfin")

    result = refresh_after_import(db, job_id, JellyfinConfig())

    assert result["status"] == "skipped"
    assert db.latest_jellyfin_refresh(job_id)["status"] == "skipped"


def test_jellyfin_api_error_is_recorded_as_warning(tmp_path):
    db = Database(tmp_path / "disc_steward.sqlite3")
    db.initialize()
    job_id = db.upsert_job(tmp_path / "disc", "imported_to_jellyfin")

    def failing_post(_url: str, _headers: dict[str, str]) -> tuple[int, str]:
        raise HTTPError(_url, 500, "server error", hdrs=None, fp=None)

    result = refresh_after_import(
        db,
        job_id,
        JellyfinConfig(base_url="http://eddy:8096", api_key="secret", refresh_enabled=True),
        post=failing_post,
    )

    assert result["status"] == "warning"
    assert "server error" in result["error"]
    assert db.latest_jellyfin_refresh(job_id)["status"] == "warning"
