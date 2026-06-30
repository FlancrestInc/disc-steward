from pathlib import Path

from disc_steward.config import AppConfig
from disc_steward.models import ReviewDecision, ScannedFile, VideoInfo
from disc_steward.transfer import detect_transfer_conflict
from disc_steward.validation import validate_output
from disc_steward.work_orders import build_final_library_path


def test_movie_final_path_includes_metadata_id(tmp_path):
    config = AppConfig.default_for_root(tmp_path)
    decision = ReviewDecision(
        source_file_id=1,
        role="main_feature",
        content_type="movie",
        title="Spirited Away",
        year=2001,
        imdb_id="tt0245429",
        target_library="Movies",
        final_display_name="Spirited Away",
        encoding_profile="universal_h264_aac_srt",
        subtitle_policy="ocr_image_subtitles_to_srt_preserve_original",
    )

    path = build_final_library_path(config, decision)

    assert path == tmp_path / "eddy" / "Movies" / "Spirited Away (2001)" / "Spirited Away (2001) [imdbid-tt0245429].mkv"


def test_validation_rejects_missing_output(tmp_path):
    source = ScannedFile(
        path="/raw/title.mkv",
        filename="title.mkv",
        parent_disc_folder="/raw",
        size_bytes=1000,
        modified_time=1.0,
        duration_seconds=100.0,
        container_format="matroska,webm",
        video=VideoInfo(codec="h264", profile="High", pixel_format="yuv420p", bit_depth=8),
    )

    result = validate_output(
        source=source,
        output_path=tmp_path / "missing.mkv",
        final_path=tmp_path / "final.mkv",
        ffprobe_runner=lambda path: "{}",
    )

    assert result.passed is False
    assert any("does not exist" in issue for issue in result.issues)


def test_transfer_conflict_detection_respects_overwrite_flag(tmp_path):
    target = tmp_path / "Movies" / "Existing.mkv"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"already here")

    assert detect_transfer_conflict(target, overwrite=False).conflict is True
    assert detect_transfer_conflict(target, overwrite=True).conflict is False
