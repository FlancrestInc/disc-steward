from __future__ import annotations

import threading
from pathlib import Path

import pytest
from playwright.sync_api import Page

from disc_steward import web
from disc_steward.config import AppConfig
from disc_steward.db import Database
from disc_steward.models import AudioStream, ScannedFile, VideoInfo


@pytest.fixture
def review_ui(tmp_path: Path):
    config = AppConfig.default_for_root(tmp_path)
    db = Database(tmp_path / "disc_steward.sqlite3")
    db.initialize()
    disc = tmp_path / "DISC"
    disc.mkdir()
    media = disc / "movie.mkv"
    media.write_bytes(b"0123456789")
    job_id = db.upsert_job(disc)
    db.upsert_source_file(
        job_id,
        ScannedFile(
            path=str(media),
            filename=media.name,
            parent_disc_folder=str(disc),
            size_bytes=media.stat().st_size,
            modified_time=media.stat().st_mtime,
            duration_seconds=100.0,
            container_format="matroska,webm",
            video=VideoInfo(codec="h264", profile="High", pixel_format="yuv420p"),
            audio_streams=[AudioStream(index=1, codec="aac", language="eng")],
        ),
    )
    server = web.ThreadingHTTPServer(("127.0.0.1", 0), web.make_review_handler(db, config))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/jobs/{job_id}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_review_page_supports_keyboard_controls_and_captures_viewports(page: Page, review_ui: str, tmp_path: Path):
    page.set_viewport_size({"width": 1440, "height": 960})
    page.goto(review_ui)

    title = page.locator('input[name="title"]')
    title.focus()
    assert title.evaluate("element => document.activeElement === element")
    assert page.locator(".ds-button").count() > 0
    assert page.locator(".ds-field .ds-control").count() >= 4
    assert page.title().startswith("Review")
    assert page.locator("main").is_visible()
    assert page.locator(".ds-window .ds-titlebar").inner_text() == page.title()

    desktop = tmp_path / "review-desktop.png"
    page.screenshot(path=str(desktop), full_page=True)
    assert desktop.stat().st_size > 0

    page.set_viewport_size({"width": 390, "height": 844})
    narrow = tmp_path / "review-narrow.png"
    page.screenshot(path=str(narrow), full_page=True)
    assert narrow.stat().st_size > 0
