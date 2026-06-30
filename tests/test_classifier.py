from pathlib import Path

from disc_steward.classifier import classify_disc_files
from disc_steward.scanner import parse_ffprobe


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
