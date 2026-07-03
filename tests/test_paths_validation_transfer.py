from pathlib import Path
import json

from disc_steward.cleanup import plan_cleanup
from disc_steward.config import AppConfig, config_from_dict
from disc_steward.db import Database
from disc_steward.models import AudioStream, FileReviewDecision, JobReviewMetadata, ReviewDecision, ScannedFile, SubtitleStream, VideoInfo
import disc_steward.transfer as transfer
from disc_steward.transfer import detect_transfer_conflict, transfer_job_to_eddy
from disc_steward.validation import validate_job_outputs, validate_output
from disc_steward.work_orders import build_fileflows_item_payload, build_final_library_path, create_ffmpeg_processing_jobs, generate_final_paths


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


def test_path_mappings_translate_between_controller_barnabas_and_eddy():
    config = config_from_dict(
        {
            "pipeline_root": "/gospel/Barnabas/media-pipeline",
            "paths": {
                "raw_rip_path": "/gospel/Barnabas/media-pipeline/01_disc_rips_raw",
                "validation_needed_path": "/gospel/Barnabas/media-pipeline/06_validation_needed",
            },
            "transfer": {
                "eddy_incoming_root": "/gospel/Eddy/jellyfin-media/.incoming/disc-steward",
                "eddy_final_roots": {"Movies": "/gospel/Eddy/jellyfin-media/Movies"},
            },
            "path_mappings": {
                "barnabas": [
                    {
                        "controller_path": "/gospel/Barnabas/media-pipeline",
                        "barnabas_path": "/mnt/data2/media-pipeline",
                    }
                ],
                "eddy": [
                    {
                        "controller_path": "/gospel/Eddy/jellyfin-media",
                        "eddy_path": "/mnt/jellyfin-media",
                    }
                ],
            },
        }
    )

    assert config.to_barnabas_path(Path("/gospel/Barnabas/media-pipeline/01_disc_rips_raw/DISC/title.mkv")) == Path(
        "/mnt/data2/media-pipeline/01_disc_rips_raw/DISC/title.mkv"
    )
    assert config.to_eddy_path(Path("/gospel/Eddy/jellyfin-media/Movies/Movie/Movie.mkv")) == Path(
        "/mnt/jellyfin-media/Movies/Movie/Movie.mkv"
    )
    assert config.to_controller_path(Path("/mnt/jellyfin-media/Movies/Movie/Movie.mkv"), "eddy") == Path(
        "/gospel/Eddy/jellyfin-media/Movies/Movie/Movie.mkv"
    )


def test_fileflows_payload_uses_barnabas_paths_and_eddy_final_path(tmp_path):
    config = AppConfig.default_for_root(tmp_path)
    config.raw_rip_path = tmp_path / "gospel" / "Barnabas" / "media-pipeline" / "01_disc_rips_raw"
    config.validation_needed_path = tmp_path / "gospel" / "Barnabas" / "media-pipeline" / "06_validation_needed"
    config.eddy_library_roots = {"Movies": tmp_path / "gospel" / "Eddy" / "jellyfin-media" / "Movies"}
    config.path_mappings = config.path_mappings_for(
        barnabas=[(tmp_path / "gospel" / "Barnabas" / "media-pipeline", Path("/mnt/data2/media-pipeline"))],
        eddy=[(tmp_path / "gospel" / "Eddy" / "jellyfin-media", Path("/mnt/jellyfin-media"))],
    )
    source_path = config.raw_rip_path / "SPIRITED_AWAY" / "title_t00.mkv"
    job = JobReviewMetadata(job_id=1, title="Spirited Away", year=2001, content_type="movie", library_root="Movies")
    decision = FileReviewDecision(
        source_file_id=1,
        role="main_feature",
        content_type="movie",
        final_display_name="Spirited Away",
        encoding_profile="universal_h264_aac_srt",
        subtitle_policy="preserve_existing",
    )
    generated = generate_final_paths(config, job, [decision])[1]
    decision.generated_final_path = str(generated.final_path)

    payload = build_fileflows_item_payload(config, 184, 1, source_path, job, decision)

    assert payload["source_path"] == "/mnt/data2/media-pipeline/01_disc_rips_raw/SPIRITED_AWAY/title_t00.mkv"
    assert payload["barnabas_validation_output_dir"] == "/mnt/data2/media-pipeline/06_validation_needed/job_184"
    assert payload["final_library_path"] == "/mnt/jellyfin-media/Movies/Spirited Away (2001)/Spirited Away (2001).mkv"
    assert payload["subtitle_outputs"] == []
    assert generated.controller_path == tmp_path / "gospel" / "Eddy" / "jellyfin-media" / "Movies" / "Spirited Away (2001)" / "Spirited Away (2001).mkv"


def test_validation_reads_fileflows_outputs_through_controller_mount(tmp_path):
    config = _gospel_config(tmp_path)
    db, job_id, _source_id, final_path = _reviewed_job(config, tmp_path)
    output = config.validation_needed_path / f"job_{job_id}" / Path(final_path).name
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(b"output" * 900)

    summary = validate_job_outputs(db, config, job_id, ffprobe_runner=lambda _path: _output_ffprobe())

    assert summary.passed is True
    assert summary.items[0].matched_output_path == str(output)
    assert summary.items[0].expected_final_path == final_path


def test_local_mount_transfer_places_to_controller_eddy_path_but_records_eddy_path(tmp_path):
    config = _gospel_config(tmp_path)
    config.dry_run = False
    (tmp_path / "gospel" / "Eddy" / "jellyfin-media").mkdir(parents=True)
    db, job_id, _source_id, final_path = _reviewed_job(config, tmp_path)
    output = config.validation_needed_path / f"job_{job_id}" / Path(final_path).name
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(b"validated-output" * 300)
    validate_job_outputs(db, config, job_id, ffprobe_runner=lambda _path: _output_ffprobe())

    summary = transfer_job_to_eddy(db, config, job_id)

    controller_final = config.to_controller_path(Path(final_path), "eddy")
    validation_summary = db.latest_validation_summary(job_id)
    assert validation_summary is not None
    assert summary.status == "imported_to_jellyfin"
    assert summary.items[0].final_path == final_path
    assert controller_final.read_bytes() == output.read_bytes()
    for subtitle in validation_summary["items"][0]["subtitle_outputs"]:
        assert (controller_final.parent / subtitle["output_name"]).exists()


def test_rsync_transfer_includes_subtitle_sidecars(tmp_path, monkeypatch):
    config = AppConfig.default_for_root(tmp_path)
    config.transfer_method = "rsync"
    config.rsync_target = "eddy:/incoming"
    config.dry_run = False
    output = tmp_path / "Movie.mkv"
    output.write_bytes(b"movie")
    subtitle = tmp_path / "Movie.eng.srt"
    subtitle.write_text("1\n00:00:00,000 --> 00:00:01,000\nSubtitle\n\n", encoding="utf-8")
    commands: list[list[str]] = []

    monkeypatch.setattr(transfer.subprocess, "run", lambda command, check: commands.append(command))

    summary = transfer._transfer_with_rsync(
        None,
        config,
        42,
        [
            {
                "source_file_id": 7,
                "matched_output_path": str(output),
                "expected_final_path": "/media/Movies/Movie/Movie.mkv",
                "subtitle_outputs": [{"output_name": subtitle.name}],
            }
        ],
    )

    assert commands == [
        ["rsync", str(output), "eddy:/incoming/job_42/Movie.mkv"],
        ["rsync", str(subtitle), "eddy:/incoming/job_42/Movie.eng.srt"],
    ]
    assert summary.items[0].subtitle_paths == ["eddy:/incoming/job_42/Movie.eng.srt"]



def test_cleanup_treats_missing_controller_mount_as_unavailable_not_deleted_media(tmp_path):
    config = _gospel_config(tmp_path)
    db = Database(tmp_path / "disc_steward.sqlite3")
    db.initialize()
    missing_source = config.raw_rip_path / "DISC" / "title_t00.mkv"
    job_id = db.upsert_job(missing_source.parent, "imported_to_jellyfin")
    source_id = db.upsert_source_file(job_id, _source(missing_source))
    db.save_validation_summary(
        job_id,
        {
            "passed": True,
            "items": [
                {
                    "source_file_id": source_id,
                    "status": "passed",
                    "matched_output_path": str(config.validation_needed_path / "job_1" / "out.mkv"),
                    "expected_final_path": "/mnt/jellyfin-media/Movies/Movie/Movie.mkv",
                }
            ],
        },
        True,
    )
    controller_final = tmp_path / "gospel" / "Eddy" / "jellyfin-media" / "Movies" / "Movie" / "Movie.mkv"
    controller_final.parent.mkdir(parents=True)
    controller_final.write_bytes(b"final")
    db.save_transfer_summary(
        job_id,
        {
            "status": "imported_to_jellyfin",
            "items": [{"source_file_id": source_id, "status": "placed", "final_path": "/mnt/jellyfin-media/Movies/Movie/Movie.mkv"}],
        },
    )
    config.cleanup.delete_raw_rips = True

    summary = plan_cleanup(db, config)

    assert summary.ineligible
    assert "mount unavailable" in summary.ineligible[0].reason


def _gospel_config(tmp_path: Path) -> AppConfig:
    config = AppConfig.default_for_root(tmp_path)
    config.raw_rip_path = tmp_path / "gospel" / "Barnabas" / "media-pipeline" / "01_disc_rips_raw"
    config.fileflows_work_order_path = tmp_path / "gospel" / "Barnabas" / "media-pipeline" / "04_ready_for_fileflows"
    config.validation_needed_path = tmp_path / "gospel" / "Barnabas" / "media-pipeline" / "06_validation_needed"
    config.eddy_incoming_path = tmp_path / "gospel" / "Eddy" / "jellyfin-media" / ".incoming" / "disc-steward"
    config.eddy_library_roots = {"Movies": tmp_path / "gospel" / "Eddy" / "jellyfin-media" / "Movies"}
    config.path_mappings = config.path_mappings_for(
        barnabas=[(tmp_path / "gospel" / "Barnabas" / "media-pipeline", Path("/mnt/data2/media-pipeline"))],
        eddy=[(tmp_path / "gospel" / "Eddy" / "jellyfin-media", Path("/mnt/jellyfin-media"))],
    )
    config.duration_tolerance_seconds = 3
    return config


def _reviewed_job(config: AppConfig, tmp_path: Path) -> tuple[Database, int, int, str]:
    db = Database(tmp_path / "disc_steward.sqlite3")
    db.initialize()
    disc = config.raw_rip_path / "SPIRITED_AWAY"
    disc.mkdir(parents=True)
    source_path = disc / "title_t00.mkv"
    source_path.write_bytes(b"source" * 800)
    job_id = db.upsert_job(disc, "reviewed")
    source_id = db.upsert_source_file(job_id, _source(source_path, size=source_path.stat().st_size))
    review = JobReviewMetadata(job_id=job_id, title="Spirited Away", year=2001, content_type="movie", library_root="Movies", review_status="reviewed")
    db.save_job_review(review)
    decision = FileReviewDecision(
        source_file_id=source_id,
        role="main_feature",
        content_type="movie",
        final_display_name="Spirited Away",
        encoding_profile="universal_h264_aac_srt",
        subtitle_policy="preserve_existing",
    )
    final_path = str(generate_final_paths(config, review, [decision])[source_id].final_path)
    decision.generated_final_path = final_path
    db.save_file_review(decision)
    previous_dry_run = config.dry_run
    config.dry_run = True
    try:
        work_order_dir = create_ffmpeg_processing_jobs(db, config, job_id, ffmpeg_runner=lambda command: Path(command[-1]).write_bytes(b"ffmpeg-output" * 300))
    finally:
        config.dry_run = previous_dry_run
    item_payload = json.loads(next((work_order_dir / "items").glob("*.process.json")).read_text(encoding="utf-8"))
    output_dir = config.validation_needed_path / f"job_{job_id}"
    output_dir.mkdir(parents=True, exist_ok=True)
    for subtitle in item_payload.get("subtitle_outputs", []):
        (output_dir / subtitle["output_name"]).write_text("1\n00:00:00,000 --> 00:00:01,000\nSubtitle\n\n", encoding="utf-8")
    return db, job_id, source_id, final_path



def _source(media_path: Path, duration: float = 120.0, size: int = 4000) -> ScannedFile:
    return ScannedFile(
        path=str(media_path),
        filename=media_path.name,
        parent_disc_folder=str(media_path.parent),
        size_bytes=size,
        modified_time=1.0,
        duration_seconds=duration,
        container_format="matroska,webm",
        video=VideoInfo(codec="hevc", profile="Main 10", pixel_format="yuv420p10le", bit_depth=10, width=1920, height=1080),
        audio_streams=[AudioStream(index=1, codec="truehd", language="jpn")],
        subtitle_streams=[SubtitleStream(index=2, codec="subrip", language="eng")],
    )


def _output_ffprobe() -> str:
    return """{
        "format": {"duration": "120.0", "format_name": "matroska,webm", "size": "4096"},
        "streams": [
            {"index": 0, "codec_type": "video", "codec_name": "h264", "profile": "High", "pix_fmt": "yuv420p", "bits_per_raw_sample": "8"},
            {"index": 1, "codec_type": "audio", "codec_name": "aac", "channels": 2, "channel_layout": "stereo", "tags": {"language": "eng"}},
            {"index": 2, "codec_type": "subtitle", "codec_name": "subrip", "disposition": {"default": 0}, "tags": {"language": "eng"}}
        ]
    }"""
