from __future__ import annotations

import json
import shlex
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
from disc_steward.scanner import scan_completed_rips
from disc_steward.review import (
    ReviewValidationError,
    suggest_subtitle_policy,
    validate_review_ready,
)
from disc_steward.web import handle_job_action, render_job_review
from disc_steward.work_orders import (
    build_ffmpeg_runner,
    build_fileflows_item_payload,
    create_ffmpeg_processing_jobs,
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


def test_review_page_renders_job_summary_counts(tmp_path):
    config = _config(tmp_path)
    db = Database(tmp_path / "disc_steward.sqlite3")
    db.initialize()
    disc = tmp_path / "media-pipeline" / "01_disc_rips_raw" / "BATMAN_1989"
    disc.mkdir(parents=True)
    main_media = disc / "title_t00.mkv"
    extra_media = disc / "trailer.mkv"
    unresolved_media = disc / "bonus.mkv"
    main_media.write_bytes(b"fake-main")
    extra_media.write_bytes(b"fake-extra")
    unresolved_media.write_bytes(b"fake-unresolved")
    job_id = db.upsert_job(disc, "review_needed")
    main_source = db.upsert_source_file(job_id, _scanned(main_media))
    extra_source = db.upsert_source_file(job_id, _scanned(extra_media))
    unresolved_source = db.upsert_source_file(job_id, _scanned(unresolved_media))
    db.save_classification(main_source, Classification(probable_main_feature=True, confidence=0.95))
    db.save_classification(extra_source, Classification(probable_trailer=True, confidence=0.82))
    db.save_classification(unresolved_source, Classification(manual_review_required=True, confidence=0.41))
    db.save_job_review(_job_review(job_id=job_id, review_status="review_needed"))
    db.save_file_review(_file_decision(source_file_id=main_source, role="main_feature", final_display_name="Batman"))
    db.save_file_review(_file_decision(source_file_id=extra_source, include_in_work_order=False, role="ignore_candidate", content_type="extra", final_display_name="Trailer"))

    html = render_job_review(db, config, job_id)

    assert "Job summary" in html
    assert "Back to jobs" in html
    assert "Advanced file details" in html
    assert "Destination preview" in html
    assert "Processing and transfer" not in html
    assert "Metadata automation and cleanup" not in html
    assert "lookup-strip advanced-card" in html
    assert 'lookup-strip advanced-card" open' not in html
    assert html.count('<details class="advanced-panel file-advanced ds-motion-disclosure">') >= 1
    assert "dashboard-lane-collapsed" in html
    assert "lane-badges" in html
    assert html.count('<details class="file-task-panel"') == 0
    assert html.count('<details class="file-preview-panel">') == 0
    assert "Needs attention" not in html
    assert "Attention:" in html
    assert "Total files" in html and ">3<" in html
    assert "Reviewed" in html and ">2<" in html
    assert "Unresolved" in html and ">1<" in html
    assert "Skipped" in html and ">1<" in html
    assert "Main feature selected: 1" in html
    assert "Warnings" in html
    assert "Conflicts" in html
    assert "Delete job" in html
    assert f'formaction="/jobs/{job_id}/delete-job"' in html
    assert "pipeline-progress" in html
    assert "pipeline-arrow" in html


def test_delete_job_removes_it_and_related_queue_rows(tmp_path):
    config = _config(tmp_path)
    db = Database(tmp_path / "disc_steward.sqlite3")
    db.initialize()
    disc = tmp_path / "media-pipeline" / "01_disc_rips_raw" / "FAILED_RIP"
    disc.mkdir(parents=True)
    media = disc / "rip.mkv"
    media.write_bytes(b"failed-rip")
    job_id = db.upsert_job(disc, "review_needed")
    source_id = db.upsert_source_file(job_id, _scanned(media))
    db.save_job_review(_job_review(job_id=job_id, review_status="review_needed"))
    db.save_file_review(_file_decision(source_file_id=source_id, role="ignore_candidate", include_in_work_order=False, content_type="extra"))
    db.enqueue_automation_job(job_id)

    result = handle_job_action(db, config, job_id, "delete-job", {})

    assert result == "redirect:/"
    assert db.get_job(job_id) is None
    assert db.source_file_payload(source_id) is None
    assert db.list_file_reviews(job_id) == []
    assert db.get_automation_job(job_id) is None
    assert db.list_job_summaries() == []
    assert db.list_audit_events(job_id)[-1]["event_type"] == "job_deleted"
    assert db.list_ignored_disc_paths() == [str(disc.resolve())]
    ignored = db.list_ignored_jobs()[0]
    assert ignored["job_id"] == job_id
    assert ignored["disc_title"] == "FAILED_RIP"
    assert ignored["status"] == "deleted"


def test_unignore_restores_deleted_job_folder_to_future_scans(tmp_path, monkeypatch):
    config = _config(tmp_path)
    config.raw_rip_path = tmp_path / "media-pipeline" / "01_disc_rips_raw"
    config.raw_rip_settle_seconds = 0
    db = Database(tmp_path / "disc_steward.sqlite3")
    db.initialize()
    disc = tmp_path / "media-pipeline" / "01_disc_rips_raw" / "UNIGNORE_ME"
    disc.mkdir(parents=True)
    media = disc / "rip.mkv"
    media.write_bytes(b"retry-me")
    job_id = db.upsert_job(disc, "review_needed")
    db.ignore_disc_path(disc, reason=f"deleted job {job_id}", job_id=job_id, disc_title="UNIGNORE_ME", status="deleted")
    db.delete_job(job_id)

    monkeypatch.setattr("disc_steward.scanner.scan_disc_folder", lambda *args, **kwargs: job_id)

    assert db.unignore_disc_path(disc) is True
    assert db.list_ignored_disc_paths() == []
    assert scan_completed_rips(db, config) == [job_id]


def test_skipped_file_saves_as_ignore_candidate(tmp_path):
    config = _config(tmp_path)
    db = Database(tmp_path / "disc_steward.sqlite3")
    db.initialize()
    disc = tmp_path / "media-pipeline" / "01_disc_rips_raw" / "SPIRITED_AWAY"
    disc.mkdir(parents=True)
    media = disc / "trailer.mkv"
    media.write_bytes(b"fake-media")
    job_id = db.upsert_job(disc, "review_needed")
    source_id = db.upsert_source_file(job_id, _scanned(media))
    db.save_job_review(_job_review(job_id=job_id, review_status="review_needed"))

    form = {
        f"file_{source_id}_content_type": "extra",
        f"file_{source_id}_role": "trailer",
        f"file_{source_id}_final_display_name": "Trailer",
        f"file_{source_id}_encoding_profile": "universal_h264_aac_srt",
        f"file_{source_id}_subtitle_policy": "manual_review",
        "title": "Spirited Away",
        "content_type": "movie",
        "library_root": "Movies",
        "year": "2001",
    }
    handle_job_action(db, config, job_id, "save", form)

    saved = db.list_file_reviews(job_id)[0]
    assert saved.include_in_work_order is False
    assert saved.role == "ignore_candidate"


def test_movie_extra_path_uses_featurettes_folder_and_display_name(tmp_path):
    decision = _file_decision(source_file_id=2, role="featurette", content_type="extra", final_display_name="Making Of")

    paths = generate_final_paths(_config(tmp_path), _job_review(), [decision])

    assert paths[2].final_path == tmp_path / "eddy" / "Movies" / "Spirited Away (2001)" / "featurettes" / "Making Of.mkv"


def test_movie_trailer_path_uses_trailers_folder(tmp_path):
    decision = _file_decision(source_file_id=3, role="trailer", content_type="extra", final_display_name="Theatrical Trailer")

    paths = generate_final_paths(_config(tmp_path), _job_review(), [decision])

    assert paths[3].final_path == tmp_path / "eddy" / "Movies" / "Spirited Away (2001)" / "trailers" / "Theatrical Trailer.mkv"


def test_skipped_file_is_omitted_from_path_generation_and_review_preview(tmp_path):
    config = _config(tmp_path)
    db = Database(tmp_path / "disc_steward.sqlite3")
    db.initialize()
    disc = tmp_path / "media-pipeline" / "01_disc_rips_raw" / "SPIRITED_AWAY"
    disc.mkdir(parents=True)
    main_source = db.upsert_source_file(db.upsert_job(disc, "review_needed"), _scanned(disc / "main.mkv"))
    extra_source = db.upsert_source_file(1, _scanned(disc / "trailer.mkv"))
    db.save_job_review(_job_review(job_id=1, review_status="review_needed"))
    db.save_file_review(_file_decision(source_file_id=main_source, role="main_feature", final_display_name="Spirited Away"))
    db.save_file_review(_file_decision(source_file_id=extra_source, include_in_work_order=False, role="ignore_candidate", content_type="extra", final_display_name="Spirited Away"))

    paths = generate_final_paths(config, _job_review(), [
        _file_decision(source_file_id=main_source, role="main_feature", final_display_name="Spirited Away"),
        _file_decision(source_file_id=extra_source, include_in_work_order=False, role="ignore_candidate", content_type="extra", final_display_name="Spirited Away"),
    ])

    assert set(paths) == {main_source}
    html = render_job_review(db, config, 1)
    assert "Skipped / do not process" in html


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


def test_path_generation_auto_renames_collisions(tmp_path):
    config = _config(tmp_path)
    existing = tmp_path / "eddy" / "Movies" / "Spirited Away (2001)" / "featurettes" / "Making Of.mkv"
    existing.parent.mkdir(parents=True)
    existing.write_bytes(b"already")
    decisions = [
        _file_decision(source_file_id=1, role="featurette", content_type="extra", final_display_name="Making Of"),
        _file_decision(source_file_id=2, role="featurette", content_type="extra", final_display_name="Making Of"),
    ]

    paths = generate_final_paths(config, _job_review(), decisions)

    assert paths[1].final_path == tmp_path / "eddy" / "Movies" / "Spirited Away (2001)" / "featurettes" / "Making Of_1.mkv"
    assert paths[2].final_path == tmp_path / "eddy" / "Movies" / "Spirited Away (2001)" / "featurettes" / "Making Of_2.mkv"
    assert paths[1].conflicts == []
    assert paths[2].conflicts == []


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


def test_create_ffmpeg_processing_jobs_writes_manifest_and_item_json(tmp_path):
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

    folder = create_ffmpeg_processing_jobs(db, config, job_id, ffmpeg_runner=lambda command: Path(command[-1]).write_bytes(b"ffmpeg-output" * 300))

    manifest = json.loads((folder / "job_manifest.json").read_text())
    item = json.loads((folder / "items" / "item_001.process.json").read_text(encoding="utf-8"))
    assert manifest["job_id"] == job_id
    assert manifest["included_items"] == 1
    assert manifest["target_library_root"] == "Movies"
    assert item["item_id"] == source_id
    assert item["profile"] == "universal_h264_aac_srt"


def test_build_ffmpeg_runner_wraps_remote_ssh_command(tmp_path, monkeypatch):
    config = _config(tmp_path)
    config.processing.method = "ssh"
    config.processing.ssh_target = "barnabas.lan"
    config.processing.ssh_user = "flan"
    config.processing.ssh_options = ["-o", "BatchMode=yes"]
    config.processing.docker_image = "disc-steward-ffmpeg:bookworm"
    config.processing.docker_state_root = "/mnt/data1/docker/disc-steward-ffmpeg"
    remote_root = tmp_path / "barnabas-media-pipeline"
    config.path_mappings = AppConfig.path_mappings_for(barnabas=[(config.pipeline_root, remote_root)])
    captured: dict[str, list[str]] = {}

    def fake_run(command, check=True):
        captured["command"] = command
        return object()

    monkeypatch.setattr("disc_steward.work_orders.subprocess.run", fake_run)

    run_ffmpeg = build_ffmpeg_runner(config)
    run_ffmpeg(["ffmpeg", "-hide_banner", "-i", f"{remote_root}/input file.mkv", f"{remote_root}/output file.mkv"])

    assert captured["command"][:4] == ["ssh", "-o", "BatchMode=yes", "flan@barnabas.lan"]
    remote_command = shlex.split(captured["command"][4])
    assert remote_command[:8] == ["docker", "run", "--rm", "--init", "-v", f"{remote_root}:{remote_root}", "-v", "/mnt/data1/docker/disc-steward-ffmpeg:/mnt/data1/docker/disc-steward-ffmpeg"]
    assert remote_command[8:11] == ["-w", str(remote_root), "disc-steward-ffmpeg:bookworm"]
    assert remote_command[11:] == ["ffmpeg", "-hide_banner", "-i", f"{remote_root}/input file.mkv", f"{remote_root}/output file.mkv"]


def test_create_ffmpeg_processing_jobs_uses_remote_runner_when_configured(tmp_path, monkeypatch):
    config = _config(tmp_path)
    config.dry_run = False
    config.processing.method = "ssh"
    config.processing.ssh_target = "barnabas.lan"
    config.processing.ssh_user = "flan"
    remote_root = tmp_path / "barnabas-media-pipeline"
    config.path_mappings = AppConfig.path_mappings_for(barnabas=[(config.pipeline_root, remote_root)])
    db = Database(tmp_path / "disc_steward.sqlite3")
    db.initialize()
    disc = tmp_path / "media-pipeline" / "01_disc_rips_raw" / "SPIRITED_AWAY"
    disc.mkdir(parents=True)
    media = disc / "title_t00.mkv"
    media.write_bytes(b"fake-media")
    job_id = db.upsert_job(disc, "reviewed")
    source = _scanned(media)
    source.subtitle_streams = []
    source_id = db.upsert_source_file(job_id, source)
    job_review = _job_review(job_id=job_id, review_status="reviewed")
    db.save_job_review(job_review)
    final_path = generate_final_paths(config, job_review, [_file_decision(source_file_id=source_id)])[source_id].final_path
    decision = _file_decision(source_file_id=source_id, generated_final_path=str(final_path))
    db.save_file_review(decision)
    remote_validation_root = config.to_barnabas_path(config.validation_needed_path)
    captured: list[list[str]] = []

    def fake_run(command, check=True):
        captured.append(command)
        if command[0] == "ssh":
            remote_command = shlex.split(command[-1])
            if remote_command[-3:] == ["mkdir", "-p", remote_command[-1]]:
                Path(remote_command[-1]).mkdir(parents=True, exist_ok=True)
            else:
                target = Path(remote_command[-1])
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(b"ffmpeg-output" * 300)
                controller_target = config.to_controller_path(target, "barnabas")
                controller_target.parent.mkdir(parents=True, exist_ok=True)
                controller_target.write_bytes(b"ffmpeg-output" * 300)
        return object()

    monkeypatch.setattr("disc_steward.work_orders.subprocess.run", fake_run)

    folder = create_ffmpeg_processing_jobs(db, config, job_id)

    assert captured and captured[0][0] == "ssh"
    assert (remote_validation_root / f"job_{job_id}" / final_path.name).exists()
    assert (folder / "items" / "item_001.process.json").exists()
