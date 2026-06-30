from pathlib import Path

from disc_steward.config import AppConfig
from disc_steward.db import Database
from disc_steward.scanner import parse_ffprobe, scan_disc_folder


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
