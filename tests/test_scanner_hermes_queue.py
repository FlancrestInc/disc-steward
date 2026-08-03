from pathlib import Path

from disc_steward.config import AppConfig
from disc_steward.db import Database
from disc_steward.scanner import scan_disc_folder


def test_scan_queues_hermes_before_readiness_notification(tmp_path, monkeypatch):
    disc = tmp_path / "Bonus Disc"
    disc.mkdir()
    media = disc / "title_t00.mkv"
    media.write_bytes(b"fake-media")
    fixture = Path("tests/fixtures/ffprobe_movie.json")
    config = AppConfig.default_for_root(tmp_path)
    config.automatic_review.hermes_enabled = True
    config.preview.enabled = False
    config.metadata.enabled = False
    events = []

    def fake_probe(path: Path) -> str:
        return fixture.read_text()

    def fake_notification(*args, **kwargs):
        events.append("notification")

    monkeypatch.setattr("disc_steward.scanner.send_notification", fake_notification)
    db = Database(tmp_path / "disc_steward.sqlite3")
    db.initialize()

    job_id = scan_disc_folder(db, config, disc, ffprobe_runner=fake_probe)

    assert job_id is not None
    queued = db.get_hermes_review_job(job_id)
    assert queued is not None
    assert queued["state"] == "queued"
    assert events == ["notification"]
