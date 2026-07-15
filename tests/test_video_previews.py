from __future__ import annotations

from pathlib import Path

from disc_steward.config import AppConfig
from disc_steward.db import Database
from disc_steward.models import AudioStream, ScannedFile, VideoInfo
from disc_steward import preview as preview_worker
from disc_steward import scanner, web


def _source(path: Path) -> ScannedFile:
    return ScannedFile(
        path=str(path),
        filename=path.name,
        parent_disc_folder=str(path.parent),
        size_bytes=path.stat().st_size,
        modified_time=path.stat().st_mtime,
        duration_seconds=120.0,
        container_format="matroska,webm",
        video=VideoInfo(codec="h264", profile="High", pixel_format="yuv420p"),
        audio_streams=[AudioStream(index=1, codec="aac", language="eng")],
    )


def _db_with_source(tmp_path: Path) -> tuple[Database, AppConfig, int, int, Path, Path]:
    config = AppConfig.default_for_root(tmp_path)
    db = Database(tmp_path / "disc_steward.sqlite3")
    db.initialize()
    disc = tmp_path / "DISC"
    disc.mkdir()
    media = disc / "movie.mkv"
    media.write_bytes(b"fake-media")
    job_id = db.upsert_job(disc)
    source_id = db.upsert_source_file(job_id, _source(media))
    preview_path = Path(config.preview.output_path) / f"job_{job_id}" / f"source_{source_id}.mp4"
    return db, config, job_id, source_id, media, preview_path


def test_scan_disc_folder_queues_previews_when_enabled(tmp_path, monkeypatch):
    config = AppConfig.default_for_root(tmp_path)
    config.preview.auto_generate = False
    db = Database(tmp_path / "disc_steward.sqlite3")
    db.initialize()
    disc = tmp_path / "DISC"
    disc.mkdir()
    media = disc / "movie.mkv"
    media.write_bytes(b"fake-media")
    queued: list[int] = []

    def fake_probe(path: Path) -> str:
        assert path == media
        return """
        {"format": {"duration": "120.0", "format_name": "matroska,webm", "size": "10"}, "streams": []}
        """

    def fake_queue(db_arg, config_arg, job_id_arg):
        queued.append(job_id_arg)
        return 1

    monkeypatch.setattr(scanner, "queue_previews_for_job", fake_queue)

    job_id = scanner.scan_disc_folder(db, config, disc, ffprobe_runner=fake_probe)

    assert queued == [job_id]


def test_preview_worker_generates_and_marks_ready(tmp_path, monkeypatch):
    db, config, job_id, source_id, media, preview_path = _db_with_source(tmp_path)
    db.queue_preview_job(
        job_id,
        source_id,
        str(media),
        str(preview_path),
        source_size_bytes=media.stat().st_size,
        source_modified_time=media.stat().st_mtime,
    )

    written: list[Path] = []

    def fake_encode(_config, source_path: Path, output_path: Path) -> None:
        assert source_path == media
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(b"preview-mp4")
        written.append(output_path)

    monkeypatch.setattr(preview_worker, "_run_preview_ffmpeg", fake_encode)

    processed = preview_worker._process_next_preview_job(db, config, worker_name="barnabas")

    payload = db.source_file_payload(source_id)
    queued_row = db.preview_job(source_id)

    assert processed is True
    assert written == [preview_path]
    assert preview_path.exists()
    assert payload is not None
    assert payload["preview_status"] == "ready"
    assert payload["preview_path"] == str(preview_path)
    assert payload["preview_error"] is None
    assert payload["preview_generated_at"] is not None
    assert queued_row is not None
    assert queued_row["state"] == "ready"
    assert queued_row["last_error"] is None


def test_review_page_uses_browser_video_preview_when_ready(tmp_path):
    db, config, job_id, source_id, media, preview_path = _db_with_source(tmp_path)
    db.queue_preview_job(
        job_id,
        source_id,
        str(media),
        str(preview_path),
        source_size_bytes=media.stat().st_size,
        source_modified_time=media.stat().st_mtime,
    )
    preview_path.parent.mkdir(parents=True, exist_ok=True)
    preview_path.write_bytes(b"preview-mp4")
    db.finish_preview_job(source_id, state="ready", generated_at="2026-07-08 09:00:00", preview_path=str(preview_path))

    html = web.render_job_review(db, config, job_id)

    assert f"/media/{source_id}/preview" in html
    assert "<video class=\"media-preview\"" in html
    assert "Thumbnail preview from the file itself." not in html
    assert "Preview status:" in html
    assert "Queue previews" in html


def test_job_level_queue_previews_action_queues_all_sources(tmp_path, monkeypatch):
    db, config, job_id, source_id, media, preview_path = _db_with_source(tmp_path)
    queued: list[tuple[int, bool]] = []

    def fake_queue(db_arg, config_arg, job_arg, *, force_reprocess=False):
        queued.append((job_arg, force_reprocess))
        return 1

    monkeypatch.setattr(web, "queue_previews_for_job", fake_queue)

    result = web.handle_job_action(db, config, job_id, "generate-previews", {})

    assert result == "preview-queued:1"
    assert queued == [(job_id, False)]


def test_preview_worker_uses_controller_paths_on_ssh(tmp_path, monkeypatch):
    db, config, job_id, source_id, media, preview_path = _db_with_source(tmp_path)
    config.processing.method = "ssh"
    config.processing.ssh_target = "barnabas.lan"
    config.processing.ssh_user = "flan"
    config.processing.ssh_options = []
    config.processing.docker_image = "disc-steward-ffmpeg:bookworm"
    config.processing.docker_state_root = str(tmp_path / "docker-state")
    db.queue_preview_job(
        job_id,
        source_id,
        str(media),
        str(preview_path),
        source_size_bytes=media.stat().st_size,
        source_modified_time=media.stat().st_mtime,
    )

    seen_commands: list[list[str]] = []

    def fake_runner(config_arg):
        def run(command: list[str]):
            seen_commands.append(command)
            output_path = Path(command[-1])
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_bytes(b"preview-mp4")

        return run

    monkeypatch.setattr(preview_worker, "_preview_runner", fake_runner)

    processed = preview_worker._process_next_preview_job(db, config, worker_name="barnabas")

    assert processed is True
    assert seen_commands
    assert str(media) in seen_commands[0]
    assert str(preview_path.parent) in " ".join(seen_commands[0])
    assert preview_path.exists()
    payload = db.source_file_payload(source_id)
    assert payload is not None
    assert payload["preview_status"] == "ready"


def test_preview_temp_output_uses_media_extension(tmp_path, monkeypatch):
    db, config, job_id, source_id, media, preview_path = _db_with_source(tmp_path)
    config.processing.method = "local"
    db.queue_preview_job(
        job_id,
        source_id,
        str(media),
        str(preview_path),
        source_size_bytes=media.stat().st_size,
        source_modified_time=media.stat().st_mtime,
    )

    temp_outputs: list[Path] = []

    def fake_runner(config_arg):
        def run(command: list[str]):
            temp_outputs.append(Path(command[-1]))
            output_path = Path(command[-1])
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_bytes(b"preview-mp4")

        return run

    monkeypatch.setattr(preview_worker, "_preview_runner", fake_runner)

    processed = preview_worker._process_next_preview_job(db, config, worker_name="barnabas")

    assert processed is True
    assert temp_outputs
    assert temp_outputs[0].suffix == ".mp4"


def test_render_job_list_cards_are_full_width_links_without_paths(tmp_path):
    db, config, job_id, source_id, media, preview_path = _db_with_source(tmp_path)
    html = web.render_job_list(db, config)

    assert f'<a class="job-card-link" href="/jobs/{job_id}"' in html
    assert "source_disc_path" not in html
    assert str(media.parent) not in html


def test_render_job_list_shows_preview_queue_panel_and_error(tmp_path):
    db, config, job_id, source_id, media, preview_path = _db_with_source(tmp_path)
    db.queue_preview_job(
        job_id,
        source_id,
        str(media),
        str(preview_path),
        source_size_bytes=media.stat().st_size,
        source_modified_time=media.stat().st_mtime,
    )
    db.finish_preview_job(source_id, state="failed", error="Source file not found: /mnt/data2/media-pipeline/example.mkv")

    html = web.render_job_list(db, config)

    assert "Preview queue" in html
    assert "failed" in html
    assert "Source file not found" in html
    assert f"/jobs/{job_id}" in html
    assert '<details class="dashboard-lane preview-queue-panel">' in html
