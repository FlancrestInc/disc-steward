from __future__ import annotations

import json
from pathlib import Path

from disc_steward.config import AppConfig
from disc_steward.db import Database
from disc_steward.models import (
    AudioStream,
    Classification,
    FileReviewDecision,
    JobReviewMetadata,
    ScannedFile,
    SubtitleStream,
    VideoInfo,
)
from disc_steward.review import (
    ReviewValidationError,
    suggest_subtitle_policy,
    validate_review_ready,
)
from disc_steward.work_orders import (
    build_fileflows_item_payload,
    create_fileflows_work_orders,
    generate_final_paths,
    sanitize_filename_component,
)


def _config(tmp_path: Path) -> AppConfig:
    config = AppConfig.default_for_root(tmp_path)
    config.fileflows_work_order_path = tmp_path / "media-pipeline" / "04_ready_for_fileflows"
    config.validation_needed_path = tmp_path / "media-pipeline" / "06_validation_needed"
    return config


def _job_review(**overrides) -> JobReviewMetadata:
    data = {
        "job_id": 1,
        "title": "Spirited Away",
        "original_title": "千と千尋の神隠し",
        "year": 2001,
        "content_type": "movie",
        "library_root": "Movies",
        "imdb_id": "tt0245429",
        "tmdb_id": "129",
    }
    data.update(overrides)
    return JobReviewMetadata(**data)


def _file_decision(**overrides) -> FileReviewDecision:
    data = {
        "source_file_id": 1,
        "include_in_work_order": True,
        "role": "main_feature",
        "content_type": "movie",
        "final_display_name": "Spirited Away",
        "encoding_profile": "universal_h264_aac_srt",
        "subtitle_policy": "ocr_image_subtitles_to_srt_preserve_original",
    }
    data.update(overrides)
    return FileReviewDecision(**data)


def _scanned(path: Path, duration: float = 7500.0) -> ScannedFile:
    return ScannedFile(
        path=str(path),
        filename=path.name,
        parent_disc_folder=str(path.parent),
        size_bytes=1234,
        modified_time=1.0,
        duration_seconds=duration,
        container_format="matroska,webm",
        video=VideoInfo(codec="hevc", profile="Main 10", pixel_format="yuv420p10le", width=1920, height=1080),
        audio_streams=[AudioStream(index=1, codec="truehd", language="jpn")],
        subtitle_streams=[SubtitleStream(index=2, codec="hdmv_pgs_subtitle", language="eng", default=True)],
        chapter_count=12,
    )


def test_movie_path_preserves_unicode_and_formats_metadata_ids(tmp_path):
    paths = generate_final_paths(_config(tmp_path), _job_review(), [_file_decision()])

    assert paths[1].final_path == (
        tmp_path
        / "eddy"
        / "Movies"
        / "Spirited Away (2001)"
        / "Spirited Away (2001) [imdbid-tt0245429] [tmdbid-129].mkv"
    )
    assert paths[1].conflicts == []


def test_movie_extra_path_uses_extras_folder_and_display_name(tmp_path):
    decision = _file_decision(source_file_id=2, role="featurette", content_type="extra", final_display_name="Making Of")

    paths = generate_final_paths(_config(tmp_path), _job_review(), [decision])

    assert paths[2].final_path == tmp_path / "eddy" / "Movies" / "Spirited Away (2001)" / "extras" / "Making Of.mkv"


def test_show_episode_path_and_season_zero_special_path(tmp_path):
    job = _job_review(title="Show Name", year=2020, content_type="show", library_root="Shows", imdb_id=None, tmdb_id=None)
    episode = _file_decision(
        source_file_id=10,
        role="episode",
        content_type="show",
        final_display_name="Episode Title",
        season_number=1,
        episode_number=1,
    )
    special = _file_decision(
        source_file_id=11,
        role="interview",
        content_type="extra",
        final_display_name="Interview with the Cast",
        season_number=0,
        episode_number=1,
    )

    paths = generate_final_paths(_config(tmp_path), job, [episode, special])

    assert paths[10].final_path == tmp_path / "eddy" / "Shows" / "Show Name" / "Season 01" / "Show Name - S01E01 - Episode Title.mkv"
    assert paths[11].final_path == tmp_path / "eddy" / "Shows" / "Show Name" / "Season 00" / "Show Name - S00E01 - Interview with the Cast.mkv"


def test_filename_sanitization_preserves_japanese_and_removes_invalid_characters():
    assert sanitize_filename_component('千と千尋: 神/隠し?* "2001"') == "千と千尋 神 隠し 2001"
    assert sanitize_filename_component("...") == "Untitled"


def test_path_generation_detects_duplicate_and_existing_conflicts(tmp_path):
    config = _config(tmp_path)
    existing = tmp_path / "eddy" / "Movies" / "Spirited Away (2001)" / "extras" / "Making Of.mkv"
    existing.parent.mkdir(parents=True)
    existing.write_bytes(b"already")
    decisions = [
        _file_decision(source_file_id=1, role="featurette", content_type="extra", final_display_name="Making Of"),
        _file_decision(source_file_id=2, role="featurette", content_type="extra", final_display_name="Making Of"),
    ]

    paths = generate_final_paths(config, _job_review(), decisions)

    assert "duplicate generated final path" in paths[1].conflicts
    assert "final path already exists" in paths[1].conflicts
    assert "duplicate generated final path" in paths[2].conflicts


def test_review_validation_requires_movie_main_feature_and_selected_policies(tmp_path):
    reviewed_paths = generate_final_paths(_config(tmp_path), _job_review(), [_file_decision(role="", encoding_profile="", subtitle_policy="")])

    try:
        validate_review_ready(_job_review(), [_file_decision(role="", encoding_profile="", subtitle_policy="")], reviewed_paths)
    except ReviewValidationError as error:
        messages = error.messages
    else:  # pragma: no cover
        raise AssertionError("expected validation failure")

    assert "at least one included file must have a role" in messages
    assert "movie jobs require an included main feature" in messages
    assert "included files require an encoding profile" in messages
    assert "included files require a subtitle policy" in messages


def test_subtitle_policy_suggestions_cover_image_ass_missing_and_clean_srt():
    image_default = Classification(has_image_subtitles=True, image_subtitle_is_default=True)
    missing_non_english = Classification(has_text_subtitles=False)
    ass = Classification(has_text_subtitles=True)
    clean_srt = Classification(has_text_subtitles=True)

    assert suggest_subtitle_policy(image_default, audio_languages=["jpn"], subtitle_codecs=["hdmv_pgs_subtitle"]).policy == "ocr_image_subtitles_to_srt_preserve_original"
    assert suggest_subtitle_policy(missing_non_english, audio_languages=["jpn"], subtitle_codecs=[]).policy == "generate_missing_srt_unverified"
    assert suggest_subtitle_policy(ass, audio_languages=["eng"], subtitle_codecs=["ass"]).policy == "preserve_ass_add_srt_fallback"
    assert suggest_subtitle_policy(clean_srt, audio_languages=["eng"], subtitle_codecs=["subrip"]).policy == "preserve_existing"


def test_work_order_payload_and_files_include_barnabas_and_final_paths(tmp_path):
    config = _config(tmp_path)
    job_review = _job_review()
    decision = _file_decision(generated_final_path=str(generate_final_paths(config, _job_review(), [_file_decision()])[1].final_path))
    source_path = Path("/mnt/media-pipeline/01_disc_rips_raw/SPIRITED_AWAY/title_t00.mkv")

    payload = build_fileflows_item_payload(config, 184, 1, source_path, job_review, decision)

    assert payload["source_path"] == str(source_path)
    assert payload["barnabas_validation_output_dir"] == str(config.validation_needed_path / "job_184")
    assert payload["preserve_original_audio"] is True
    assert payload["preserve_original_subtitles"] is True
    assert payload["created_by"] == "disc-steward"


def test_create_fileflows_work_orders_writes_manifest_and_item_json(tmp_path):
    config = _config(tmp_path)
    db = Database(tmp_path / "disc_steward.sqlite3")
    db.initialize()
    disc = tmp_path / "media-pipeline" / "01_disc_rips_raw" / "SPIRITED_AWAY"
    disc.mkdir(parents=True)
    media = disc / "title_t00.mkv"
    media.write_bytes(b"fake-media")
    job_id = db.upsert_job(disc, "reviewed")
    source_id = db.upsert_source_file(job_id, _scanned(media))
    job_review = _job_review(job_id=job_id, review_status="reviewed")
    db.save_job_review(job_review)
    decision = _file_decision(source_file_id=source_id, generated_final_path=str(generate_final_paths(config, job_review, [_file_decision(source_file_id=source_id)])[source_id].final_path))
    db.save_file_review(decision)

    folder = create_fileflows_work_orders(db, config, job_id)

    manifest = json.loads((folder / "job_manifest.json").read_text())
    item = json.loads((folder / "items" / "item_001.work_order.json").read_text())
    assert manifest["job_id"] == job_id
    assert manifest["included_items"] == 1
    assert manifest["target_library_root"] == "Movies"
    assert item["item_id"] == source_id
    assert item["profile"] == "universal_h264_aac_srt"
