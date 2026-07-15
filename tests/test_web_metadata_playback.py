from __future__ import annotations

import json
import threading
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from disc_steward.config import AppConfig, MetadataProviderConfig
from disc_steward.db import Database
from disc_steward.metadata import MetadataCandidate, MetadataLookupResult
from disc_steward.models import AudioStream, FileReviewDecision, ScannedFile, VideoInfo
from disc_steward import web
from disc_steward import preview as preview_worker
from disc_steward.work_orders import generate_final_paths, create_ffmpeg_processing_jobs
from disc_steward.validation import validate_job_outputs


def test_page_includes_favicon_link(tmp_path):
    html = web.page("Test", "<p>hello</p>")

    assert 'rel="icon" type="image/png" href="/favicon.ico?v=' in html


def test_page_links_versioned_design_system_stylesheet_before_inline_css():
    html = web.page("Test", "<p>hello</p>")

    stylesheet_index = html.index('rel="stylesheet" href="/static/win31-core.css?v=')
    inline_css_index = html.index("<style>")
    assert stylesheet_index < inline_css_index


def test_page_activates_win31_theme_and_maps_legacy_tokens_to_semantic_tokens():
    html = web.page("Test", "<p>hello</p>")

    assert '<body data-ds-theme="win31">' in html
    assert "body[data-ds-theme=\"win31\"]" in html
    assert "--bg: var(--ds-surface-canvas);" in html
    assert "--surface: var(--ds-surface-window);" in html
    assert "--title-start: var(--ds-accent-primary);" in html
    assert "--font-body: var(--ds-font-ui);" in html


def test_review_markup_adopts_win31_control_primitives(tmp_path):
    config = _config(tmp_path)
    db, job_id, _source_id, _media = _job_with_source(tmp_path, config)

    html = web.render_job_review(db, config, job_id)

    assert 'class="ds-field">Title <input class="ds-control"' in html
    assert '<select class="ds-control" name="content_type">' in html
    assert 'class="ds-button ds-button--primary primary-action"' in html
    assert 'class="ds-button ds-button--danger danger-action"' in html
    assert '<table class="ds-table"' in web.render_phase4_sections(db, config, job_id)


def test_inline_styles_do_not_override_win31_primitives():
    html = web.page("Test", "<p>hello</p>")

    assert 'button:not(.ds-button)' in html
    assert 'input:not(.ds-control)' in html
    assert 'table:not(.ds-table)' in html
    assert '.primary-action:not(.ds-button)' in html
    assert '.danger-action:not(.ds-button)' in html


def test_static_design_system_stylesheet_is_served_and_unknown_asset_is_not_found(tmp_path):
    config = _config(tmp_path)
    db = Database(tmp_path / "disc_steward.sqlite3")
    db.initialize()
    handler = web.make_review_handler(db, config)
    server = web.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_port}"
    try:
        with urlopen(f"{base_url}/static/win31-core.css", timeout=5) as response:
            assert response.status == 200
            assert response.headers["Content-Type"].startswith("text/css")
            assert response.read().startswith(b"/* Generated from src/index.css")
        try:
            urlopen(f"{base_url}/static/not-a-real-asset.css", timeout=5)
        except HTTPError as error:
            assert error.code == 404
        else:  # pragma: no cover
            raise AssertionError("unknown static asset should 404")
    finally:
        server.shutdown()
        server.server_close()


def _config(tmp_path: Path) -> AppConfig:
    config = AppConfig.default_for_root(tmp_path)
    config.metadata.enabled = True
    config.metadata.providers["tmdb"] = MetadataProviderConfig(enabled=True, api_key="tmdb-key")
    config.metadata.providers["mal"] = MetadataProviderConfig(enabled=True, api_key="mal-client-id")
    return config


def _source(path: Path) -> ScannedFile:
    return ScannedFile(
        path=str(path),
        filename=path.name,
        parent_disc_folder=str(path.parent),
        size_bytes=path.stat().st_size,
        modified_time=path.stat().st_mtime,
        duration_seconds=100.0,
        container_format="matroska,webm",
        video=VideoInfo(codec="h264", profile="High", pixel_format="yuv420p"),
        audio_streams=[AudioStream(index=1, codec="aac", language="eng")],
    )


def _job_with_source(tmp_path: Path, config: AppConfig) -> tuple[Database, int, int, Path]:
    db = Database(tmp_path / "disc_steward.sqlite3")
    db.initialize()
    disc = tmp_path / "DISC"
    disc.mkdir()
    media = disc / "movie.mkv"
    media.write_bytes(b"0123456789")
    job_id = db.upsert_job(disc)
    source_id = db.upsert_source_file(job_id, _source(media))
    return db, job_id, source_id, media


def _job_with_two_sources(tmp_path: Path, config: AppConfig) -> tuple[Database, int, int, int, Path, Path]:
    db = Database(tmp_path / "disc_steward.sqlite3")
    db.initialize()
    disc = tmp_path / "DOUBLE_FEATURE"
    disc.mkdir()
    first = disc / "movie_one.mkv"
    second = disc / "movie_two.mkv"
    first.write_bytes(b"1111111111")
    second.write_bytes(b"2222222222")
    job_id = db.upsert_job(disc)
    first_id = db.upsert_source_file(job_id, _source(first))
    second_id = db.upsert_source_file(job_id, _source(second))
    review = db.get_job_review(job_id)
    review.title = "Double Feature Disc"
    review.content_type = "movie"
    review.library_root = "Movies"
    db.save_job_review(review)
    return db, job_id, first_id, second_id, first, second


def _automation_ffprobe() -> str:
    return json.dumps(
        {
            "format": {"duration": "100.0", "format_name": "matroska,webm", "size": "4096"},
            "streams": [
                {
                    "index": 0,
                    "codec_type": "video",
                    "codec_name": "h264",
                    "profile": "High",
                    "pix_fmt": "yuv420p",
                    "bits_per_raw_sample": "8",
                    "width": 1920,
                    "height": 1080,
                    "avg_frame_rate": "24000/1001",
                    "r_frame_rate": "24000/1001",
                },
                {
                    "index": 1,
                    "codec_type": "audio",
                    "codec_name": "aac",
                    "channels": 2,
                    "channel_layout": "stereo",
                    "tags": {"language": "eng"},
                },
            ],
        }
    )


def test_lookup_metadata_action_saves_current_form_before_applying_lookup(tmp_path, monkeypatch):
    config = _config(tmp_path)
    db, job_id, source_id, _media = _job_with_source(tmp_path, config)
    seen_title = None

    def fake_lookup(db_arg, _config, job_id_arg):
        nonlocal seen_title
        seen_title = db_arg.get_job_review(job_id_arg).title
        review = db_arg.get_job_review(job_id_arg)
        review.year = 2001
        review.tmdb_id = "129"
        db_arg.save_job_review(review)
        return MetadataLookupResult(
            candidates=[MetadataCandidate(provider="tmdb", title="Typed Title", year=2001, tmdb_id="129", confidence=1.0)],
            applied_fields={"job": ["year", "tmdb_id"]},
        )

    monkeypatch.setattr(web, "lookup_job_metadata", fake_lookup)

    message = web.handle_job_action(
        db,
        config,
        job_id,
        "lookup-metadata",
        {
            "title": "Typed Title",
            "content_type": "movie",
            "library_root": "Movies",
            f"file_{source_id}_include": "on",
            f"file_{source_id}_role": "main_feature",
            f"file_{source_id}_content_type": "movie",
            f"file_{source_id}_final_display_name": "Typed Title",
            f"file_{source_id}_encoding_profile": config.preferred_video_profile,
            f"file_{source_id}_subtitle_policy": "preserve_existing",
        },
    )

    review = db.get_job_review(job_id)
    assert message == "metadata-lookup:1"
    assert seen_title == "Typed Title"
    assert review.title == "Typed Title"
    assert review.year == 2001
    assert review.tmdb_id == "129"


def test_preview_queue_panel_is_hidden_when_empty(tmp_path):
    config = _config(tmp_path)
    config.preview.enabled = True
    db = Database(tmp_path / "disc_steward.sqlite3")
    db.initialize()

    assert web.render_preview_queue_panel(db, config) == ""
    assert "Preview queue" not in web.render_job_list(db, config)


def test_render_job_list_shows_deleted_ignored_jobs_with_unignore_action(tmp_path):
    config = _config(tmp_path)
    db = Database(tmp_path / "disc_steward.sqlite3")
    db.initialize()
    disc = tmp_path / "media-pipeline" / "01_disc_rips_raw" / "IGNORED_RIP"
    disc.mkdir(parents=True)
    db.ignore_disc_path(disc, reason="deleted job 26", job_id=26, disc_title="IGNORED_RIP", status="deleted")

    html = web.render_job_list(db, config)

    assert "Deleted / ignored" in html
    assert "IGNORED_RIP" in html
    assert 'action="/ignored/unignore"' in html
    assert 'action="/ignored/open-folder"' in html


def test_media_review_open_file_button_uses_fetch_action(tmp_path):
    config = _config(tmp_path)
    db, job_id, source_id, _media = _job_with_source(tmp_path, config)
    row = db.source_file_payload(source_id)
    assert row is not None

    html = web.render_media_review_action_buttons(config, job_id, row)

    assert 'type="button"' in html
    assert 'data-system-open-action="/jobs/' in html
    assert 'open-source-file-' in html
    assert 'formaction="/jobs/1/open-source-file-' not in html


def test_destination_preview_is_live_wired_to_job_form(tmp_path):
    config = _config(tmp_path)
    db, job_id, _source_id, _media = _job_with_source(tmp_path, config)

    html = web.render_job_review(db, config, job_id)

    assert 'id="job-review-form"' in html
    assert 'updateAllDestinationPreviews' in html
    assert "querySelectorAll('.file-card')" in html
    assert "Destination preview" in html


def test_automated_flow_completes_and_imports_after_review(tmp_path):
    config = _config(tmp_path)
    config.dry_run = False
    db, job_id, _source_id, final_path = _job_with_source(tmp_path, config)
    review = db.get_job_review(job_id)
    review.title = "Spirited Away"
    review.year = 2001
    review.content_type = "movie"
    review.library_root = "Movies"
    review.review_status = "reviewed"
    db.save_job_review(review)
    decision = FileReviewDecision(
        source_file_id=db.source_file_payloads(job_id)[0]["id"],
        include_in_work_order=True,
        role="main_feature",
        content_type="movie",
        final_display_name="Spirited Away",
        encoding_profile=config.preferred_video_profile,
        subtitle_policy="ocr_image_subtitles_to_srt_preserve_original",
    )
    db.save_file_review(decision)
    final_path = generate_final_paths(config, review, [decision])[_source_id].final_path

    result = web._run_automated_pipeline(
        db,
        config,
        job_id,
        force_reprocess=True,
        ffmpeg_runner=lambda command: Path(command[-1]).write_bytes(b"ffmpeg-output" * 300),
        ffprobe_runner=lambda path: _automation_ffprobe(),
    )

    assert result == "automation:imported_to_jellyfin"
    assert db.get_job(job_id).status == "imported_to_jellyfin"
    assert db.latest_validation_summary(job_id)["passed"] is True
    assert db.latest_transfer_summary(job_id)["status"] == "imported_to_jellyfin"
    assert final_path.exists()


def test_automated_flow_resumes_after_transfer_conflict(tmp_path):
    config = _config(tmp_path)
    config.dry_run = False
    db, job_id, _source_id, _source_media = _job_with_source(tmp_path, config)
    review = db.get_job_review(job_id)
    review.title = "Spirited Away"
    review.year = 2001
    review.content_type = "movie"
    review.library_root = "Movies"
    review.review_status = "reviewed"
    db.save_job_review(review)
    decision = FileReviewDecision(
        source_file_id=db.source_file_payloads(job_id)[0]["id"],
        include_in_work_order=True,
        role="main_feature",
        content_type="movie",
        final_display_name="Spirited Away",
        encoding_profile=config.preferred_video_profile,
        subtitle_policy="ocr_image_subtitles_to_srt_preserve_original",
    )
    db.save_file_review(decision)
    final_path = generate_final_paths(config, review, [decision])[_source_id].final_path

    create_ffmpeg_processing_jobs(
        db,
        config,
        job_id,
        ffmpeg_runner=lambda command: Path(command[-1]).write_bytes(b"ffmpeg-output" * 300),
    )
    validation = validate_job_outputs(db, config, job_id, ffprobe_runner=lambda path: _automation_ffprobe())
    assert validation.passed is True
    assert db.get_job(job_id).status == "transfer_ready"

    final_path.parent.mkdir(parents=True, exist_ok=True)
    final_path.write_bytes(b"already-there")

    blocked = web._run_automated_pipeline(db, config, job_id)

    assert blocked == "automation:transfer_conflict"
    assert db.get_job(job_id).status == "transfer_conflict"
    assert db.latest_validation_summary(job_id)["passed"] is True

    final_path.unlink()
    resumed = web._run_automated_pipeline(
        db,
        config,
        job_id,
        ffmpeg_runner=lambda command: Path(command[-1]).write_bytes(b"ffmpeg-output" * 300),
        ffprobe_runner=lambda path: _automation_ffprobe(),
    )

    assert resumed == "automation:imported_to_jellyfin"
    assert db.get_job(job_id).status == "imported_to_jellyfin"
    assert db.latest_transfer_summary(job_id)["status"] == "imported_to_jellyfin"
    assert final_path.exists()


def test_review_page_renders_per_file_lookup_button(tmp_path):
    config = _config(tmp_path)
    db, job_id, source_id, _media = _job_with_source(tmp_path, config)

    html = web.render_job_review(db, config, job_id)

    assert "Lookup file metadata" in html
    assert f'formaction="/jobs/{job_id}/lookup-file-metadata-{source_id}"' in html
    assert "Advanced file details" in html


def test_split_source_file_creates_child_job_and_moves_the_file(tmp_path):
    config = _config(tmp_path)
    db, job_id, first_id, second_id, _first, _second = _job_with_two_sources(tmp_path, config)

    html = web.render_job_review(db, config, job_id)
    assert f'action="/jobs/{job_id}/split-source-file"' in html
    assert f'>movie_one.mkv</option>' in html
    assert f'>movie_two.mkv</option>' in html

    form = {
        "title": "Double Feature Disc",
        "content_type": "movie",
        "library_root": "Movies",
        "source_file_id": str(second_id),
        f"file_{first_id}_include": "on",
        f"file_{first_id}_role": "main_feature",
        f"file_{second_id}_include": "on",
        f"file_{second_id}_role": "main_feature",
    }

    result = web.handle_job_action(db, config, job_id, "split-source-file", form)

    assert result.startswith("redirect:/jobs/")
    new_job_id = int(result.rsplit("/", 1)[-1])
    parent = db.get_job(job_id)
    child = db.get_job(new_job_id)
    assert parent is not None
    assert child is not None
    assert child.split_from_job_id == job_id
    assert child.source_disc_path == parent.source_disc_path == parent.disc_path
    assert db.source_file_payload(first_id)["job_id"] == job_id
    assert db.source_file_payload(second_id)["job_id"] == new_job_id
    assert {row["id"] for row in db.source_file_payloads(job_id)} == {first_id}
    assert {row["id"] for row in db.source_file_payloads(new_job_id)} == {second_id}
    assert db.get_job_review(job_id).review_status == "review_in_progress"
    assert db.get_job_review(new_job_id).review_status == "review_needed"
    dashboard = web.render_job_list(db, config)
    assert f"Split from job {job_id}" in dashboard


def test_review_page_explains_provider_id_formats(tmp_path):
    config = _config(tmp_path)
    db, job_id, _source_id, _media = _job_with_source(tmp_path, config)

    html = web.render_job_review(db, config, job_id)

    assert 'placeholder="tt0245429"' in html
    assert 'placeholder="268 or TMDb movie URL"' in html
    assert "Advanced metadata" in html
    assert "Metadata lookup" in html


def test_homepage_renders_dashboard_lanes_and_cards(tmp_path):
    config = _config(tmp_path)
    db = Database(tmp_path / "disc_steward.sqlite3")
    db.initialize()
    for idx, status in enumerate(["review_needed", "reviewed", "imported_to_jellyfin"], start=1):
        disc = tmp_path / f"DISC{idx}"
        disc.mkdir()
        db.upsert_job(disc, status)

    html = web.render_job_list(db, config)

    assert "Operational overview" in html
    assert "Review queue" in html
    assert "Ready / queued" in html
    assert "Imported to Jellyfin" in html
    assert "dashboard-lane-collapsed" in html
    assert "<table>" not in html
    assert "DISC1" in html
    assert "DISC2" in html
    assert "DISC3" in html


def test_metadata_lookup_strip_shows_last_lookup_warning(tmp_path):
    config = _config(tmp_path)
    db, job_id, _source_id, _media = _job_with_source(tmp_path, config)
    db.audit(
        "metadata_lookup",
        "Metadata lookup found 0 candidate(s), applied 0 field(s)",
        job_id,
        {
            "provider_results": [{"provider": "tmdb", "candidate_count": 0, "status": "failed"}],
            "warnings": [{"provider": "tmdb", "message": "HTTP Error 401: Unauthorized"}],
        },
    )

    html = web.render_metadata_lookup_strip(db, config, job_id)

    assert "Last lookup:" in html
    assert "Metadata lookup found 0 candidate(s), applied 0 field(s)" in html
    assert "tmdb: 0 candidate(s), failed" in html
    assert "tmdb: HTTP Error 401: Unauthorized" in html


def test_metadata_lookup_strip_renders_selectable_candidates(tmp_path):
    config = _config(tmp_path)
    db, job_id, source_id, _media = _job_with_source(tmp_path, config)
    db.save_metadata_candidate(
        job_id,
        "tmdb",
        {
            "provider": "tmdb",
            "title": "Barnyard",
            "year": 2006,
            "content_type": "movie",
            "library_root": "Movies",
            "confidence": 0.93,
            "provider_id": "9907",
            "provider_url": "https://www.themoviedb.org/movie/9907",
            "tmdb_id": "9907",
        },
    )
    db.save_metadata_candidate(
        job_id,
        "tmdb",
        {
            "provider": "tmdb",
            "title": "Barnyard: The Original Party Animals",
            "year": 2006,
            "content_type": "movie",
            "library_root": "Movies",
            "confidence": 0.79,
            "provider_id": "12345",
            "provider_url": "https://www.themoviedb.org/movie/12345",
            "tmdb_id": "12345",
            "source_file_id": source_id,
        },
        source_id,
    )

    html = web.render_metadata_lookup_strip(db, config, job_id)

    assert "Use this match" in html
    assert "Open tmdb page" in html
    assert "Applies to: job review" in html
    assert "Applies to: file movie.mkv" in html
    assert "Barnyard" in html


def test_apply_metadata_candidate_populates_job_and_main_file_fields(tmp_path, monkeypatch):
    config = _config(tmp_path)
    db, job_id, source_id, _media = _job_with_source(tmp_path, config)
    db.save_classification(source_id, web.Classification(probable_main_feature=True))
    candidate = {
        "provider": "tmdb",
        "title": "Barnyard",
        "year": 2006,
        "content_type": "movie",
        "library_root": "Movies",
        "confidence": 0.93,
        "provider_id": "9907",
        "provider_url": "https://www.themoviedb.org/movie/9907",
        "tmdb_id": "9907",
    }
    db.save_metadata_candidate(job_id, "tmdb", candidate)
    candidate_id = db.list_metadata_candidates(job_id)[0]["id"]
    called = []

    monkeypatch.setattr(web, "_queue_automated_pipeline", lambda *args, **kwargs: called.append((args, kwargs)) or "automation:queued")

    message = web.handle_job_action(
        db,
        config,
        job_id,
        f"apply-metadata-candidate-{candidate_id}",
        {
            "title": "",
            "content_type": "unknown",
            "library_root": "Movies",
            f"file_{source_id}_include": "on",
            f"file_{source_id}_role": "main_feature",
            f"file_{source_id}_content_type": "unknown",
            f"file_{source_id}_final_display_name": "",
            f"file_{source_id}_encoding_profile": config.preferred_video_profile,
            f"file_{source_id}_subtitle_policy": "preserve_existing",
        },
    )

    review = db.get_job_review(job_id)
    decision = {item.source_file_id: item for item in db.list_file_reviews(job_id)}[source_id]
    assert message == "automation:queued"
    assert called and called[0][1]["force_reprocess"] is True
    assert review.title == "Barnyard"
    assert review.year == 2006
    assert review.tmdb_id == "9907"
    assert decision.final_display_name == "Barnyard"
    assert decision.tmdb_id == "9907"


def test_save_and_run_pipeline_queues_background_automation(tmp_path):
    config = _config(tmp_path)
    db, job_id, source_id, _media = _job_with_source(tmp_path, config)

    message = web.handle_job_action(
        db,
        config,
        job_id,
        "mark-reviewed",
        {
            "title": "Spirited Away",
            "year": "2001",
            "content_type": "movie",
            "library_root": "Movies",
            f"file_{source_id}_include": "on",
            f"file_{source_id}_role": "main_feature",
            f"file_{source_id}_content_type": "movie",
            f"file_{source_id}_final_display_name": "Spirited Away",
            f"file_{source_id}_encoding_profile": config.preferred_video_profile,
            f"file_{source_id}_subtitle_policy": "ocr_image_subtitles_to_srt_preserve_original",
        },
    )

    review = db.get_job_review(job_id)
    queued = db.get_automation_job(job_id)
    assert message == "automation:queued"
    assert queued is not None
    assert queued["state"] == "queued"
    assert queued["force_reprocess"] == 1
    assert review.review_status == "reviewed"
    assert review.title == "Spirited Away"


def test_queue_automated_pipeline_persists_job_and_can_be_claimed(tmp_path, monkeypatch):
    config = _config(tmp_path)
    db, job_id, _source_id, _media = _job_with_source(tmp_path, config)
    web._queue_automated_pipeline(db, config, job_id, force_reprocess=True)

    reopened = Database(db.path)
    queued = reopened.get_automation_job(job_id)
    assert queued is not None
    assert queued["state"] == "queued"
    assert queued["force_reprocess"] == 1

    processed = []

    def fake_run(db_arg, _config, job_id_arg, *, force_reprocess=False, **_kwargs):
        processed.append((job_id_arg, force_reprocess))
        return "automation:imported_to_jellyfin"

    monkeypatch.setattr(web, "_run_automated_pipeline", fake_run)

    assert web._process_next_automation_job(reopened, config) is True
    assert processed == [(job_id, True)]
    final = reopened.get_automation_job(job_id)
    assert final is not None
    assert final["state"] == "succeeded"
    assert final["last_result"] == "automation:imported_to_jellyfin"


def test_restart_resets_stuck_automation_jobs(tmp_path):
    config = _config(tmp_path)
    db, job_id, _source_id, _media = _job_with_source(tmp_path, config)
    db.enqueue_automation_job(job_id, force_reprocess=True)
    db.claim_next_automation_job()
    running = db.get_automation_job(job_id)
    assert running is not None
    assert running["state"] == "running"

    reopened = Database(db.path)
    assert reopened.reset_stuck_automation_jobs() == 1
    reset = reopened.get_automation_job(job_id)
    assert reset is not None
    assert reset["state"] == "queued"
    assert reset["started_at"] is None
    assert reset["finished_at"] is None


def test_run_automation_worker_stops_on_event(tmp_path, monkeypatch):
    config = _config(tmp_path)
    db, job_id, _source_id, _media = _job_with_source(tmp_path, config)
    stop_event = threading.Event()
    calls = []

    def fake_process(db_arg, config_arg):
        calls.append((db_arg.path, job_id))
        stop_event.set()
        return True

    monkeypatch.setattr(web, "_process_next_automation_job", fake_process)

    thread = threading.Thread(
        target=web.run_automation_worker,
        kwargs={"db": db, "config": config, "poll_interval": 0.01, "stop_event": stop_event},
        daemon=True,
    )
    thread.start()
    thread.join(1)

    assert not thread.is_alive()
    assert calls == [(db.path, job_id)]


def test_lookup_file_metadata_action_saves_current_form_before_applying_lookup(tmp_path, monkeypatch):
    config = _config(tmp_path)
    db, job_id, source_id, _media = _job_with_source(tmp_path, config)
    seen_display_name = None

    def fake_lookup(db_arg, _config, job_id_arg, source_id_arg):
        nonlocal seen_display_name
        assert job_id_arg == job_id
        assert source_id_arg == source_id
        decision = {item.source_file_id: item for item in db_arg.list_file_reviews(job_id_arg)}[source_id_arg]
        seen_display_name = decision.final_display_name
        decision.content_type = "movie"
        db_arg.save_file_review(decision)
        return MetadataLookupResult(
            candidates=[MetadataCandidate(provider="tmdb", title="Batman", tmdb_id="268", confidence=1.0)],
            applied_fields={f"file:{source_id}": ["content_type"]},
        )

    monkeypatch.setattr(web, "lookup_file_metadata", fake_lookup)

    message = web.handle_job_action(
        db,
        config,
        job_id,
        f"lookup-file-metadata-{source_id}",
        {
            "title": "Typed Disc",
            "content_type": "movie",
            "library_root": "Movies",
            f"file_{source_id}_include": "on",
            f"file_{source_id}_role": "main_feature",
            f"file_{source_id}_content_type": "unknown",
            f"file_{source_id}_final_display_name": "Typed File",
            f"file_{source_id}_encoding_profile": config.preferred_video_profile,
            f"file_{source_id}_subtitle_policy": "preserve_existing",
        },
    )

    decision = {item.source_file_id: item for item in db.list_file_reviews(job_id)}[source_id]
    assert message == f"metadata-file-lookup:{source_id}:1"
    assert seen_display_name == "Typed File"
    assert db.get_job_review(job_id).title == "Typed Disc"
    assert decision.final_display_name == "Typed File"
    assert decision.content_type == "movie"


def test_media_review_link_uses_http_stream_instead_of_file_uri(tmp_path):
    config = _config(tmp_path)
    db, _job_id, source_id, media = _job_with_source(tmp_path, config)
    row = db.source_file_payload(source_id)
    assert row is not None

    controls_html = web.render_media_review_controls(config, _job_id, row)
    actions_html = web.render_media_review_action_buttons(config, _job_id, row)

    assert f'/media/{source_id}/thumbnail' in controls_html
    assert 'media-thumb' in controls_html
    assert 'Open file in system handler' in actions_html
    assert 'Open containing folder' in actions_html
    assert 'href="file:' not in controls_html


def test_job_admin_actions_can_rescan_and_open_paths(tmp_path, monkeypatch):
    config = _config(tmp_path)
    db, job_id, source_id, media = _job_with_source(tmp_path, config)
    seen = {}

    def fake_scan(db_arg, config_arg, disc_folder):
        seen["scan"] = (db_arg, config_arg, disc_folder)
        return job_id

    def fake_open(path):
        seen.setdefault("open", []).append(path)

    monkeypatch.setattr(web, "scan_disc_folder", fake_scan)
    monkeypatch.setattr(web, "_open_path_with_system_handler", fake_open)

    rescan_message = web.handle_job_action(db, config, job_id, "rescan-job", {})
    open_folder_message = web.handle_job_action(db, config, job_id, "open-job-folder", {})
    open_file_message = web.handle_job_action(db, config, job_id, f"open-source-file-{source_id}", {})
    open_file_folder_message = web.handle_job_action(db, config, job_id, f"open-source-file-folder-{source_id}", {})

    job = db.get_job(job_id)
    assert job is not None
    assert rescan_message == f"rescan-job:{job_id}"
    assert seen["scan"][2] == Path(job.disc_path)
    assert open_folder_message == f"open-job-folder:{job_id}"
    assert open_file_message == f"open-source-file:{source_id}"
    assert open_file_folder_message == f"open-source-file-folder:{source_id}"
    assert seen["open"][0] == Path(job.disc_path)
    assert seen["open"][1] == media
    assert seen["open"][2] == media.parent


def test_media_route_streams_known_file_and_thumbnail_support(tmp_path):
    config = _config(tmp_path)
    db, _job_id, source_id, _media = _job_with_source(tmp_path, config)
    handler = web.make_review_handler(db, config)
    server = web.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}/media/{source_id}"
    try:
        request = Request(url, headers={"Range": "bytes=2-5"})
        with urlopen(request, timeout=5) as response:
            assert response.status == 206
            assert response.headers["Content-Range"] == "bytes 2-5/10"
            assert response.read() == b"2345"
        thumbnail_url = f"http://127.0.0.1:{server.server_port}/media/{source_id}/thumbnail"
        with urlopen(thumbnail_url, timeout=5) as response:
            assert response.status == 200
            assert response.headers["Content-Type"].startswith("image/")
            assert response.read()
        try:
            urlopen(f"http://127.0.0.1:{server.server_port}/media/9999", timeout=5)
        except HTTPError as error:
            assert error.code == 404
        else:  # pragma: no cover
            raise AssertionError("unknown source id should 404")
    finally:
        server.shutdown()
        server.server_close()


def test_media_stream_handles_client_disconnect_without_error(tmp_path):
    config = _config(tmp_path)
    _db, _job_id, _source_id, media = _job_with_source(tmp_path, config)

    class DisconnectingWriter:
        def write(self, _chunk: bytes) -> None:
            raise BrokenPipeError()

    assert hasattr(web, "_write_media_range")
    assert web._write_media_range(media, DisconnectingWriter(), 0, media.stat().st_size) is False


def test_dashboard_uses_canonical_job_status_and_collapses_completed_lane():
    row = {
        "id": 1,
        "disc_title": "Batman 1989",
        "disc_path": "/mnt/Barnabas/data2/media-pipeline/01_disc_rips_raw/Batman 1989",
        "status": "imported_to_jellyfin",
        "review_status": "ready_for_processing",
        "likely_main_feature": "Batman (1989).mkv",
        "scanned_file_count": 2,
        "extra_count": 0,
        "subtitle_issue_count": 1,
        "transcode_risk_count": 0,
        "main_count": 1,
    }

    assert web._dashboard_lane_for_job(row) == "done"
    card_html = web.render_job_card(row)
    assert "imported_to_jellyfin" in card_html
    assert "Review state: ready_for_processing" in card_html
    assert "status-done" in card_html

    lane_html = web.render_dashboard_lane("Completed", [row], collapsed=True)
    assert "<details" in lane_html
    assert "<summary>Completed" in lane_html
    assert "dashboard-lane-collapsed" in lane_html


def test_phase3_sections_show_automation_queue_details(tmp_path):
    config = _config(tmp_path)
    db, queued_job_id, _source_id, _media = _job_with_source(tmp_path, config)

    running_dir = tmp_path / "RUNNING_DISC"
    running_dir.mkdir()
    running_media = running_dir / "movie_two.mkv"
    running_media.write_bytes(b"abcdefghij")
    running_job_id = db.upsert_job(running_dir)
    db.upsert_source_file(running_job_id, _source(running_media))

    db.enqueue_automation_job(running_job_id, force_reprocess=False)
    db.enqueue_automation_job(queued_job_id, force_reprocess=True)
    with db.connect() as conn:
        conn.execute("UPDATE automation_jobs SET queued_at = '2026-07-07 15:00:00' WHERE job_id = ?", (running_job_id,))
        conn.execute("UPDATE automation_jobs SET queued_at = '2026-07-07 15:01:00' WHERE job_id = ?", (queued_job_id,))
    claimed = db.claim_next_automation_job()
    assert claimed is not None
    assert claimed["job_id"] == running_job_id

    html = web.render_phase3_sections(db, config, queued_job_id)

    assert "Automation queue" in html
    assert f"Job {running_job_id}" in html
    assert f"Job {queued_job_id}" in html
    assert "current job" in html
    assert "running now" in html
    assert "position 1 in queue" in html
    assert "automation running" in html or "queued for automation" in html
