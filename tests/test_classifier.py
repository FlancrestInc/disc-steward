from pathlib import Path

from disc_steward.classifier import classify_disc_files
from disc_steward.config import AppConfig
from disc_steward.db import Database
from disc_steward.models import FileReviewDecision
from disc_steward.scanner import parse_ffprobe, scan_disc_folder


def _parsed(name: str):
    return parse_ffprobe(Path(f"tests/fixtures/{name}").read_text(), Path(f"/raw/{name}.mkv"))


def test_classifies_longest_title_as_main_and_featurette_extra():
    movie = _parsed("ffprobe_movie.json")
    extra = _parsed("ffprobe_extra.json")

    results = classify_disc_files([movie, extra])

    assert results[movie.path].probable_main_feature is True
    assert results[movie.path].possible_alternate_cut is False
    assert results[extra.path].probable_featurette is True
    assert results[extra.path].probable_extra is True


def test_detects_subtitle_and_jellyfin_risks():
    movie = _parsed("ffprobe_movie.json")

    result = classify_disc_files([movie])[movie.path]

    assert result.has_image_subtitles is True
    assert result.has_text_subtitles is True
    assert result.image_subtitle_is_default is True
    assert result.needs_subtitle_conversion is True
    assert result.needs_video_encode is True
    assert result.likely_jellyfin_transcode_risk is True
    assert result.manual_review_required is True
    assert any("default image subtitle" in reason for reason in result.reasons)


def test_scan_seeds_roles_names_and_policies_for_incoming_job(tmp_path):
    config = AppConfig.default_for_root(tmp_path)
    config.preview.enabled = False
    db = Database(tmp_path / "disc_steward.sqlite3")
    db.initialize()
    disc = tmp_path / "raw" / "SPIRITED_AWAY"
    disc.mkdir(parents=True)
    (disc / "title_t00.mkv").write_bytes(b"movie")
    (disc / "bonus_featurette.mkv").write_bytes(b"extra")
    fixtures = {
        "title_t00.mkv": Path("tests/fixtures/ffprobe_movie.json").read_text(),
        "bonus_featurette.mkv": Path("tests/fixtures/ffprobe_extra.json").read_text(),
    }

    job_id = scan_disc_folder(db, config, disc, ffprobe_runner=lambda path: fixtures[path.name])

    assert job_id is not None
    review = db.get_job_review(job_id)
    assert review.title == "Spirited Away"
    decisions = {item.source_file_id: item for item in db.list_file_reviews(job_id)}
    sources = {row["filename"]: row["id"] for row in db.source_file_payloads(job_id)}
    assert decisions[sources["title_t00.mkv"]].role == "main_feature"
    assert decisions[sources["title_t00.mkv"]].final_display_name == "Spirited Away"
    assert decisions[sources["title_t00.mkv"]].encoding_profile == config.preferred_video_profile
    assert decisions[sources["bonus_featurette.mkv"]].role == "featurette"
    assert decisions[sources["bonus_featurette.mkv"]].content_type == "extra"
    assert decisions[sources["bonus_featurette.mkv"]].final_display_name == "bonus featurette"
    assert decisions[sources["bonus_featurette.mkv"]].subtitle_policy == "preserve_existing"
    assert db.get_job_review(job_id).review_status == "review_needed"


def test_scan_does_not_overwrite_manual_file_label_on_rescan(tmp_path):
    config = AppConfig.default_for_root(tmp_path)
    config.preview.enabled = False
    db = Database(tmp_path / "disc_steward.sqlite3")
    db.initialize()
    disc = tmp_path / "raw" / "DISC"
    disc.mkdir(parents=True)
    media = disc / "title_t00.mkv"
    media.write_bytes(b"movie")
    fixture = Path("tests/fixtures/ffprobe_movie.json").read_text()
    job_id = scan_disc_folder(db, config, disc, ffprobe_runner=lambda _path: fixture)
    source_id = db.source_file_payloads(job_id)[0]["id"]
    db.save_file_review(FileReviewDecision(source_file_id=source_id, role="featurette", final_display_name="Manual label"))

    scan_disc_folder(db, config, disc, ffprobe_runner=lambda _path: fixture)

    decision = db.list_file_reviews(job_id)[0]
    assert decision.role == "featurette"
    assert decision.final_display_name == "Manual label"
