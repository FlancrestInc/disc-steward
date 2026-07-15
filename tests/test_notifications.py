from __future__ import annotations

from pathlib import Path

from disc_steward.config import AppConfig, config_from_dict
from disc_steward.db import Database
from disc_steward.notifications import send_notification
from disc_steward.scanner import scan_disc_folder


class _FakeResponse:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


def test_notification_config_reads_env_token(monkeypatch, tmp_path):
    monkeypatch.setenv("DISC_STEWARD_NTFY_TOKEN", "secret-token")

    config = config_from_dict(
        {
            "notifications": {
                "enabled": True,
                "provider": "ntfy",
                "ntfy_url": "https://ntfy.example",
                "ntfy_topic": "dvd-rips",
            }
        }
    )

    assert config.notifications.enabled is True
    assert config.notifications.ntfy_url == "https://ntfy.example"
    assert config.notifications.ntfy_topic == "dvd-rips"
    assert config.notifications.ntfy_token == "secret-token"


def test_send_notification_posts_to_ntfy(monkeypatch, tmp_path):
    config = AppConfig.default_for_root(tmp_path)
    config.notifications.enabled = True
    config.notifications.provider = "ntfy"
    config.notifications.ntfy_url = "https://ntfy.example"
    config.notifications.ntfy_topic = "dvd-rips"
    config.notifications.ntfy_token = "secret-token"

    captured = {}

    def fake_urlopen(request, timeout=0):
        captured["url"] = request.full_url
        captured["user_agent"] = request.get_header("User-agent")
        captured["content_type"] = request.get_header("Content-type")
        captured["title"] = request.get_header("Title")
        captured["priority"] = request.get_header("Priority")
        captured["authorization"] = request.get_header("Authorization")
        captured["body"] = request.data.decode("utf-8")
        captured["timeout"] = timeout
        return _FakeResponse()

    monkeypatch.setattr("disc_steward.notifications.urlopen", fake_urlopen)

    assert send_notification(config, "Disc ready", "Review is waiting.", priority="high", tags=["disc", "review"]) is True
    assert captured["url"] == "https://ntfy.example/dvd-rips"
    assert captured["user_agent"] == "disc-steward/1.0"
    assert captured["content_type"] == "text/plain; charset=utf-8"
    assert captured["title"] == "Disc ready"
    assert captured["priority"] == "high"
    assert captured["authorization"] == "Bearer secret-token"
    assert captured["body"] == "Review is waiting."
    assert captured["timeout"] == 10


def test_scan_disc_folder_emits_review_notification(tmp_path, monkeypatch):
    raw = tmp_path / "raw"
    disc = raw / "Batman 1989"
    disc.mkdir(parents=True)
    media = disc / "title_t00.mkv"
    media.write_bytes(b"fake-media")
    fixture = Path("tests/fixtures/ffprobe_movie.json")

    config = AppConfig.default_for_root(tmp_path)
    config.notifications.enabled = True
    config.notifications.provider = "ntfy"
    config.notifications.ntfy_url = "https://ntfy.example"
    config.notifications.ntfy_topic = "dvd-rips"
    db = Database(tmp_path / "disc_steward.sqlite3")
    db.initialize()
    notices: list[tuple[str, str]] = []

    def fake_probe(path: Path) -> str:
        assert path == media
        return fixture.read_text()

    monkeypatch.setattr("disc_steward.scanner.send_notification", lambda *args, **kwargs: notices.append((args[1], args[2])))

    scan_disc_folder(db, config, disc, ffprobe_runner=fake_probe)

    assert len(notices) == 1
    assert "ready for review" in notices[0][0].lower()
    assert "Open the review UI" in notices[0][1]
