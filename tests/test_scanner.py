import os
from pathlib import Path

from disc_steward.config import AppConfig, TitleDiscoveryConfig
from disc_steward.db import Database
from disc_steward.models import TitleDiscoveryResult, TitleDiscoverySignal
from disc_steward.scanner import parse_ffprobe, scan_completed_rips, scan_disc_folder, watch_completed_rips
from disc_steward.title_discovery import discover_title_from_scan, refine_title_discovery_with_ollama


def test_parse_ffprobe_collects_media_details():
    data = Path("tests/fixtures/ffprobe_movie.json").read_text()
    scanned = parse_ffprobe(data, Path("/media/raw/SPIRITED_AWAY/title_t00.mkv"))

    assert scanned.duration_seconds == 7500.5
    assert scanned.container_format == "matroska,webm"
    assert scanned.video.codec == "hevc"
    assert scanned.video.profile == "Main 10"
    assert scanned.video.pixel_format == "yuv420p10le"
    assert scanned.video.bit_depth == 10
    assert scanned.video.width == 1920
    assert scanned.video.height == 1080
    assert scanned.video.frame_rate == "24000/1001"
    assert scanned.video.frame_rate_mode == "constant"
    assert scanned.video.hdr_indicators
    assert scanned.chapter_count == 2
    assert scanned.embedded_title == "Spirited Away"
    assert scanned.makemkv_title == "title_t00"
    assert scanned.audio_streams[0].codec == "truehd"
    assert scanned.audio_streams[0].language == "jpn"
    assert scanned.subtitle_streams[0].codec == "hdmv_pgs_subtitle"
    assert scanned.subtitle_streams[0].hearing_impaired is True
    assert scanned.subtitle_streams[1].forced is True


def test_repeated_scan_updates_existing_file_without_duplicate(tmp_path):
    raw = tmp_path / "raw"
    disc = raw / "日本映画_DISC1"
    disc.mkdir(parents=True)
    media = disc / "title_t00.mkv"
    media.write_bytes(b"fake-media")
    fixture = Path("tests/fixtures/ffprobe_movie.json")

    config = AppConfig.default_for_root(tmp_path)
    db = Database(tmp_path / "disc_steward.sqlite3")
    db.initialize()

    def fake_probe(path: Path) -> str:
        assert path == media
        return fixture.read_text()

    scan_disc_folder(db, config, disc, ffprobe_runner=fake_probe)
    scan_disc_folder(db, config, disc, ffprobe_runner=fake_probe)

    jobs = db.list_jobs()
    files = db.list_source_files(jobs[0].id)
    assert len(jobs) == 1
    assert jobs[0].disc_title == "日本映画_DISC1"
    assert len(files) == 1
    assert files[0].path == str(media.resolve())


def test_scan_completed_rips_skips_already_known_disc_paths(tmp_path):
    raw = tmp_path / "raw"
    disc = raw / "Batman 1989"
    disc.mkdir(parents=True)
    media = disc / "title_t00.mkv"
    media.write_bytes(b"fake-media")
    fixture = Path("tests/fixtures/ffprobe_movie.json")

    config = AppConfig.default_for_root(tmp_path)
    db = Database(tmp_path / "disc_steward.sqlite3")
    db.initialize()

    def fake_probe(path: Path) -> str:
        assert path == media
        return fixture.read_text()

    scan_disc_folder(db, config, disc, ffprobe_runner=fake_probe)
    assert scan_completed_rips(db, config) == []


def test_scan_completed_rips_skips_ignored_disc_paths(tmp_path, monkeypatch):
    raw = tmp_path / "raw"
    disc = raw / "Ignored Rip"
    disc.mkdir(parents=True)
    media = disc / "title_t00.mkv"
    media.write_bytes(b"fake-media")

    config = AppConfig.default_for_root(tmp_path)
    config.raw_rip_path = raw
    db = Database(tmp_path / "disc_steward.sqlite3")
    db.initialize()
    db.ignore_disc_path(disc, reason="deleted job 26")

    called = []

    def fake_scan(*args, **kwargs):
        called.append(True)
        raise AssertionError("ignored discs should not be scanned")

    monkeypatch.setattr("disc_steward.scanner.scan_disc_folder", fake_scan)

    assert scan_completed_rips(db, config) == []
    assert called == []


def test_scan_completed_rips_waits_for_settled_folder(tmp_path, monkeypatch):
    raw = tmp_path / "raw"
    ready = raw / "Ready disc"
    busy = raw / "Busy disc"
    ready.mkdir(parents=True)
    busy.mkdir(parents=True)
    ready_media = ready / "title_t00.mkv"
    busy_media = busy / "title_t00.mkv"
    ready_media.write_bytes(b"ready-media")
    busy_media.write_bytes(b"busy-media")

    now = 1_700_000_000.0
    old = now - 3600
    recent = now - 120
    os.utime(ready_media, (old, old))
    os.utime(busy_media, (recent, recent))

    config = AppConfig.default_for_root(tmp_path)
    config.raw_rip_path = raw
    config.raw_rip_settle_seconds = 900
    db = Database(tmp_path / "disc_steward.sqlite3")
    db.initialize()
    scanned: list[Path] = []

    monkeypatch.setattr("disc_steward.scanner.time.time", lambda: now)

    def fake_scan(db, config, disc_folder, ffprobe_runner=None, metadata_lookup=None, title_discovery_sender=None):
        scanned.append(disc_folder)
        return len(scanned)

    monkeypatch.setattr("disc_steward.scanner.scan_disc_folder", fake_scan)

    assert scan_completed_rips(db, config) == [1]
    assert scanned == [ready]


def test_discover_title_from_scan_prefers_embedded_title_and_records_signals(tmp_path):
    disc = tmp_path / "Batman 1989"
    media = disc / "title_t00.mkv"
    scanned = parse_ffprobe(Path("tests/fixtures/ffprobe_movie.json").read_text(), media)
    scanned.raw_ffprobe["chapters"] = [{"tags": {"title": "Opening"}}]

    result = discover_title_from_scan(disc, [scanned])

    assert result.title == "Spirited Away"
    assert result.confidence > 0.5
    assert {signal.source for signal in result.signals} >= {"disc_folder", "embedded_title", "chapter_title"}
    assert any(signal.source == "filename_stem" for signal in result.signals) is False
    assert result.warnings


def test_scan_disc_folder_uses_ollama_title_refinement_when_enabled(tmp_path):
    raw = tmp_path / "raw"
    disc = raw / "Batman 1989"
    disc.mkdir(parents=True)
    media = disc / "title_t00.mkv"
    media.write_bytes(b"fake-media")
    fixture = Path("tests/fixtures/ffprobe_movie.json")

    config = AppConfig.default_for_root(tmp_path)
    config.title_discovery = TitleDiscoveryConfig(
        enabled=True,
        provider="ollama",
        endpoint="http://barnabas.lan:11434",
        model="llama3.2:3b",
        min_confidence_to_auto_fill=0.8,
    )
    db = Database(tmp_path / "disc_steward.sqlite3")
    db.initialize()
    seen = {}

    def fake_probe(path: Path) -> str:
        assert path == media
        return fixture.read_text()

    def fake_sender(endpoint: str, payload: dict) -> dict:
        seen["endpoint"] = endpoint
        seen["payload"] = payload
        return {
            "message": {
                "content": "{\"preferred_title\":\"Batman (1989)\",\"original_title\":\"Batman\",\"confidence\":0.97,\"content_type\":\"movie\",\"library_root\":\"Movies\",\"warnings\":[\"normalized from disc art\"]}"
            }
        }

    job_id = scan_disc_folder(db, config, disc, ffprobe_runner=fake_probe, title_discovery_sender=fake_sender)
    review = db.get_job_review(job_id)

    assert seen["endpoint"].endswith("/api/chat")
    assert seen["payload"]["model"] == "llama3.2:3b"
    assert review.title_discovery_json is not None
    assert review.title_discovery_json["title"] == "Batman (1989)"
    assert any(signal["source"] == "ollama" for signal in review.title_discovery_json["signals"])
    assert review.title_discovery_json["confidence"] >= 0.97


def test_refine_title_discovery_with_ollama_keeps_low_confidence_result_when_response_is_weak(tmp_path):
    config = AppConfig.default_for_root(tmp_path)
    config.title_discovery = TitleDiscoveryConfig(
        enabled=True,
        provider="ollama",
        endpoint="http://barnabas.lan:11434",
        model="llama3.2:3b",
        min_confidence_to_auto_fill=0.9,
    )
    discovery = TitleDiscoveryResult(
        title="Batman 1989",
        confidence=0.42,
        signals=[
            TitleDiscoverySignal(source="disc_folder", value="Batman 1989", confidence=0.4),
            TitleDiscoverySignal(source="embedded_title", value="Batman", confidence=0.5),
        ],
    )

    refined = refine_title_discovery_with_ollama(
        config,
        discovery,
        sender=lambda _endpoint, _payload: {
            "message": {
                "content": "{\"preferred_title\":\"Batman (1989)\",\"confidence\":0.62,\"warnings\":[\"uncertain\"]}"
            }
        },
    )

    assert refined.title == "Batman 1989"
    assert refined.confidence == 0.62
    assert any(signal.source == "ollama" for signal in refined.signals)
    assert any("below auto-fill threshold" in warning for warning in refined.warnings)


def test_watch_completed_rips_reports_each_job_once(tmp_path, monkeypatch):
    config = AppConfig.default_for_root(tmp_path)
    db = Database(tmp_path / "disc_steward.sqlite3")
    db.initialize()
    cycles = iter([[1, 2], [2, 3]])
    sleeps: list[float] = []

    monkeypatch.setattr("disc_steward.scanner.scan_completed_rips", lambda _db, _config: next(cycles))

    discovered = watch_completed_rips(db, config, interval_seconds=0.0, max_cycles=2, sleep_fn=sleeps.append)

    assert discovered == [1, 2, 3]
    assert sleeps == [0.0]


def test_refine_title_discovery_with_ollama_updates_best_candidate_when_confident(tmp_path):
    config = AppConfig.default_for_root(tmp_path)
    config.title_discovery = TitleDiscoveryConfig(
        enabled=True,
        provider="ollama",
        endpoint="http://barnabas.lan:11434",
        model="llama3.2:3b",
        min_confidence_to_auto_fill=0.8,
    )
    discovery = TitleDiscoveryResult(
        title="Batman 1989",
        confidence=0.43,
        signals=[TitleDiscoverySignal(source="disc_folder", value="Batman 1989", confidence=0.4)],
    )

    refined = refine_title_discovery_with_ollama(
        config,
        discovery,
        sender=lambda _endpoint, _payload: {
            "message": {
                "content": "{\"preferred_title\":\"Batman (1989)\",\"original_title\":\"Batman\",\"confidence\":0.96,\"content_type\":\"movie\",\"library_root\":\"Movies\",\"warnings\":[\"matched art card\"]}"
            }
        },
    )

    assert refined.title == "Batman (1989)"
    assert refined.original_title == "Batman"
    assert refined.content_type == "movie"
    assert refined.library_root == "Movies"
    assert refined.confidence == 0.96
    assert any(signal.source == "ollama" for signal in refined.signals)
