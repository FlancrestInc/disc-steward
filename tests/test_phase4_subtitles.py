from __future__ import annotations

import json
from pathlib import Path

from disc_steward.config import AppConfig
from disc_steward.db import Database
from disc_steward.models import AudioStream, FileReviewDecision, JobReviewMetadata, ScannedFile, SubtitleStream, VideoInfo
from disc_steward.subtitle_planner import generate_subtitle_plan, validate_subtitle_plan_result
from disc_steward.validation import validate_job_outputs
from disc_steward.work_orders import build_fileflows_item_payload, create_ffmpeg_processing_jobs, generate_final_paths


def _config(tmp_path: Path) -> AppConfig:
    config = AppConfig.default_for_root(tmp_path)
    config.fileflows_work_order_path = tmp_path / "media-pipeline" / "04_ready_for_fileflows"
    config.validation_needed_path = tmp_path / "media-pipeline" / "06_validation_needed"
    config.eddy_library_roots = {"Movies": tmp_path / "eddy" / "Movies", "Anime": tmp_path / "eddy" / "Anime"}
    return config


def _source(path: Path, *, audio_lang: str = "eng", subtitles: list[SubtitleStream] | None = None) -> ScannedFile:
    return ScannedFile(
        path=str(path),
        filename=path.name,
        parent_disc_folder=str(path.parent),
        size_bytes=8000,
        modified_time=1.0,
        duration_seconds=100.0,
        container_format="matroska,webm",
        video=VideoInfo(codec="hevc", profile="Main 10", pixel_format="yuv420p10le", bit_depth=10, width=1920, height=1080),
        audio_streams=[AudioStream(index=1, codec="flac", language=audio_lang)],
        subtitle_streams=subtitles or [],
        chapter_count=4,
    )


def _job_review(job_id: int, *, content_type: str = "movie", library_root: str = "Movies") -> JobReviewMetadata:
    return JobReviewMetadata(job_id=job_id, title="Test Movie", year=2001, content_type=content_type, library_root=library_root, review_status="reviewed")


def _decision(source_id: int, **overrides) -> FileReviewDecision:
    data = {
        "source_file_id": source_id,
        "role": "main_feature",
        "content_type": "movie",
        "final_display_name": "Test Movie",
        "encoding_profile": "universal_h264_aac_srt",
        "subtitle_policy": "ocr_image_subtitles_to_srt_preserve_original",
    }
    data.update(overrides)
    return FileReviewDecision(**data)


def _ffprobe_output(*, subtitles: list[dict]) -> str:
    streams = [
        {
            "index": 0,
            "codec_type": "video",
            "codec_name": "h264",
            "profile": "High",
            "pix_fmt": "yuv420p",
            "bits_per_raw_sample": "8",
            "width": 1920,
            "height": 1080,
        },
        {"index": 1, "codec_type": "audio", "codec_name": "aac", "tags": {"language": "eng"}},
    ]
    for index, subtitle in enumerate(subtitles, start=2):
        streams.append(
            {
                "index": index,
                "codec_type": "subtitle",
                "codec_name": subtitle["codec_name"],
                "disposition": {"default": subtitle.get("default", 0), "forced": subtitle.get("forced", 0)},
                "tags": {"language": subtitle.get("language", "eng"), "title": subtitle.get("title", "")},
            }
        )
    return json.dumps({"format": {"duration": "100", "format_name": "matroska,webm", "size": "9000"}, "streams": streams})


def test_subtitle_plan_generation_with_srt_already_present(tmp_path):
    scanned = _source(tmp_path / "movie.mkv", subtitles=[SubtitleStream(index=2, codec="subrip", language="eng", default=True)])

    plan = generate_subtitle_plan(scanned, content_type="movie", subtitle_policy="prefer_srt_preserve_original")

    assert plan.statuses == ["no_action_needed"]
    assert plan.text_subtitles_detected is True
    assert plan.actions == []


def test_subtitle_plan_generation_with_pgs_only_and_default_cleanup(tmp_path):
    scanned = _source(tmp_path / "movie.mkv", subtitles=[SubtitleStream(index=4, codec="hdmv_pgs_subtitle", language="eng", default=True)])

    plan = generate_subtitle_plan(scanned, content_type="movie", subtitle_policy="ocr_image_subtitles_to_srt_preserve_original")

    assert "needs_ocr_to_srt" in plan.statuses
    assert "needs_default_flag_cleanup" in plan.statuses
    assert plan.image_subtitles_detected is True
    assert plan.image_subtitles_default is True
    assert {"type": "unset_default", "source_stream_index": 4, "reason": "image subtitle should not be default"} in plan.actions
    assert any(action["type"] == "ocr_to_srt" and action["source_stream_index"] == 4 for action in plan.actions)


def test_subtitle_plan_generation_with_ass_anime_content(tmp_path):
    scanned = _source(tmp_path / "anime.mkv", audio_lang="jpn", subtitles=[SubtitleStream(index=3, codec="ass", language="eng", title="Signs & Songs")])

    plan = generate_subtitle_plan(scanned, content_type="anime", subtitle_policy="preserve_ass_add_srt_fallback")

    assert plan.ass_subtitles_detected is True
    assert plan.japanese_or_anime is True
    assert "needs_ass_srt_fallback" in plan.statuses
    assert any(action["type"] == "ass_to_srt_fallback" for action in plan.actions)
    assert any("ASS subtitles may include important styling" in warning for warning in plan.warnings)


def test_subtitle_plan_generation_with_no_subtitles_and_japanese_audio(tmp_path):
    scanned = _source(tmp_path / "movie.mkv", audio_lang="jpn", subtitles=[])

    plan = generate_subtitle_plan(scanned, content_type="movie", subtitle_policy="generate_missing_srt_unverified")

    assert "needs_missing_subtitle_generation" in plan.statuses
    assert plan.generated_subtitles_unverified is True
    assert any(action["type"] == "generate_missing_srt" for action in plan.actions)


def test_work_order_payload_includes_subtitle_plan_json(tmp_path):
    config = _config(tmp_path)
    source = _source(tmp_path / "movie.mkv", audio_lang="jpn", subtitles=[SubtitleStream(index=4, codec="hdmv_pgs_subtitle", language="eng", default=True)])
    job = _job_review(1)
    decision = _decision(7)

    payload = build_fileflows_item_payload(config, 1, 7, Path(source.path), job, decision, source)

    assert payload["subtitle_plan"]["policy"] == "ocr_image_subtitles_to_srt_preserve_original"
    assert payload["subtitle_plan"]["preferred_format"] == "srt"
    assert payload["subtitle_plan"]["preserve_original_subtitles"] is True
    assert any(action["type"] == "ocr_to_srt" for action in payload["subtitle_plan"]["actions"])


def test_create_ffmpeg_processing_jobs_persists_subtitle_plan(tmp_path):
    config = _config(tmp_path)
    db = Database(tmp_path / "disc_steward.sqlite3")
    db.initialize()
    disc = tmp_path / "media-pipeline" / "01_disc_rips_raw" / "DISC"
    disc.mkdir(parents=True)
    media = disc / "movie.mkv"
    media.write_bytes(b"source" * 1000)
    job_id = db.upsert_job(disc, "reviewed")
    source_id = db.upsert_source_file(job_id, _source(media, subtitles=[SubtitleStream(index=4, codec="hdmv_pgs_subtitle", language="eng", default=True)]))
    review = _job_review(job_id)
    db.save_job_review(review)
    decision = _decision(source_id, encoding_profile="remux_only", generated_final_path=str(generate_final_paths(config, review, [_decision(source_id, encoding_profile="remux_only")])[source_id].final_path))
    db.save_file_review(decision)

    folder = create_ffmpeg_processing_jobs(db, config, job_id, ffmpeg_runner=lambda command: Path(command[-1]).write_bytes(b"ffmpeg-output" * 300))

    item = json.loads((folder / "items" / "item_001.process.json").read_text(encoding="utf-8"))
    assert item["subtitle_plan"]["image_subtitles_default"] is True
    disposition_index = item["ffmpeg_command"].index("-disposition:s:0")
    assert item["ffmpeg_command"][disposition_index + 1] == "0"
    assert db.get_subtitle_plan(source_id)["statuses"] == ["needs_ocr_to_srt", "needs_default_flag_cleanup"]


def test_subtitle_validation_checks_selected_plan_with_warnings(tmp_path):
    source = _source(tmp_path / "movie.mkv", subtitles=[SubtitleStream(index=4, codec="hdmv_pgs_subtitle", language="eng", default=True)])
    plan = generate_subtitle_plan(source, content_type="movie", subtitle_policy="ocr_image_subtitles_to_srt_preserve_original")
    parsed = _source(
        tmp_path / "out.mkv",
        subtitles=[SubtitleStream(index=4, codec="hdmv_pgs_subtitle", language="eng", default=False)],
    )

    result = validate_subtitle_plan_result(plan, parsed)

    assert result.passed is True
    assert any("required SRT is expected but was not detected" in warning for warning in result.warnings)


def test_job_validation_warns_when_subtitle_plan_cannot_confirm_srt(tmp_path):
    config = _config(tmp_path)
    db = Database(tmp_path / "disc_steward.sqlite3")
    db.initialize()
    disc = tmp_path / "media-pipeline" / "01_disc_rips_raw" / "DISC"
    disc.mkdir(parents=True)
    media = disc / "movie.mkv"
    media.write_bytes(b"source" * 1000)
    job_id = db.upsert_job(disc, "reviewed")
    source_id = db.upsert_source_file(job_id, _source(media, subtitles=[SubtitleStream(index=4, codec="hdmv_pgs_subtitle", language="eng", default=True)]))
    review = _job_review(job_id)
    db.save_job_review(review)
    decision = _decision(source_id, generated_final_path=str(generate_final_paths(config, review, [_decision(source_id)])[source_id].final_path))
    db.save_file_review(decision)
    previous_dry_run = config.dry_run
    config.dry_run = True
    try:
        create_ffmpeg_processing_jobs(db, config, job_id, ffmpeg_runner=lambda command: Path(command[-1]).write_bytes(b"ffmpeg-output" * 300))
    finally:
        config.dry_run = previous_dry_run
    output = config.validation_needed_path / f"job_{job_id}" / Path(decision.generated_final_path).name
    output.parent.mkdir(parents=True)
    output.write_bytes(b"output" * 1000)

    summary = validate_job_outputs(db, config, job_id, ffprobe_runner=lambda _path: _ffprobe_output(subtitles=[{"codec_name": "hdmv_pgs_subtitle", "default": 0}]))

    assert summary.passed is True
    assert any("required SRT is expected but was not detected" in warning for warning in summary.items[0].warnings)
