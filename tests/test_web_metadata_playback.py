from __future__ import annotations

import threading
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from disc_steward.config import AppConfig, MetadataProviderConfig
from disc_steward.db import Database
from disc_steward.metadata import MetadataCandidate, MetadataLookupResult
from disc_steward.models import AudioStream, ScannedFile, VideoInfo
from disc_steward import web


def _config(tmp_path: Path) -> AppConfig:
    config = AppConfig.default_for_root(tmp_path)
    config.metadata.enabled = True
    config.metadata.providers["tmdb"] = MetadataProviderConfig(enabled=True, api_key="tmdb-key")
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


def test_review_page_renders_lookup_button_and_inline_player(tmp_path):
    config = _config(tmp_path)
    db, job_id, source_id, _media = _job_with_source(tmp_path, config)

    html = web.render_job_review(db, config, job_id)

    assert "Lookup All" in html
    assert f"/media/{source_id}" in html
    assert "<video" in html
    assert "External player path" in html


def test_review_page_explains_provider_id_formats(tmp_path):
    config = _config(tmp_path)
    db, job_id, _source_id, _media = _job_with_source(tmp_path, config)

    html = web.render_job_review(db, config, job_id)

    assert 'placeholder="tt0245429"' in html
    assert 'placeholder="268 or TMDb movie URL"' in html
    assert "Lookup All uses the disc-level provider ID fields first" in html


def test_media_review_link_uses_http_stream_instead_of_file_uri(tmp_path):
    config = _config(tmp_path)
    db, _job_id, source_id, media = _job_with_source(tmp_path, config)
    row = db.source_file_payload(source_id)

    html = web.render_media_review_controls(config, row)

    assert f'href="/media/{source_id}"' in html
    assert 'target="_blank"' in html
    assert 'href="file:' not in html
    assert str(media) in html


def test_media_route_streams_known_file_with_range_support(tmp_path):
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
