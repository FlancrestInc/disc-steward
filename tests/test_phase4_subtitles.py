from __future__ import annotations

import json
from pathlib import Path

import pytest

from disc_steward.config import AppConfig
from disc_steward.db import Database
from disc_steward.models import AudioStream, FileReviewDecision, JobReviewMetadata, OutputValidationItem, ScannedFile, SubtitleStream, VideoInfo
from disc_steward.subtitle_extraction import extract_subtitle_sidecars
import disc_steward.subtitle_extraction as subtitle_extraction
from disc_steward.subtitle_planner import generate_subtitle_plan, validate_subtitle_plan_result
from disc_steward.validation import _validate_subtitle_sidecars, validate_job_outputs
import disc_steward.work_orders as work_orders
from disc_steward.work_orders import build_fileflows_item_payload, create_ffmpeg_processing_jobs, generate_final_paths


def _config(tmp_path: Path) -> AppConfig:
    config = AppConfig.default_for_root(tmp_path)
    config.fileflows_work_order_path = tmp_path / "media-pipeline" / "04_ready_for_fileflows"
    config.validation_needed_path = tmp_path / "media-pipeline" / "06_validation_needed"
    config.eddy_library_roots = {"Movies": tmp_path / "eddy" / "Movies", "Anime": tmp_path / "eddy" / "Anime"}
    return config


def _source(path: Path, *, audio_lang: str = "eng", subtitles: list[SubtitleStream] | None = None) -> ScannedFile:
    return ScannedFile(
        path=str(path),
        filename=path.name,
        parent_disc_folder=str(path.parent),
        size_bytes=8000,
        modified_time=1.0,
        duration_seconds=100.0,
        container_format="matroska,webm",
        video=VideoInfo(codec="hevc", profile="Main 10", pixel_format="yuv420p10le", bit_depth=10, width=1920, height=1080),
        audio_streams=[AudioStream(index=1, codec="flac", language=audio_lang)],
        subtitle_streams=subtitles or [],
        chapter_count=4,
    )


def _job_review(job_id: int, *, content_type: str = "movie", library_root: str = "Movies") -> JobReviewMetadata:
    return JobReviewMetadata(job_id=job_id, title="Test Movie", year=2001, content_type=content_type, library_root=library_root, review_status="reviewed")


def _decision(source_id: int, **overrides) -> FileReviewDecision:
    data = {
        "source_file_id": source_id,
        "role": "main_feature",
        "content_type": "movie",
        "final_display_name": "Test Movie",
        "encoding_profile": "universal_h264_aac_srt",
        "subtitle_policy": "ocr_image_subtitles_to_srt_preserve_original",
    }
    data.update(overrides)
    return FileReviewDecision(**data)


def _ffprobe_output(*, subtitles: list[dict]) -> str:
    streams = [
        {
            "index": 0,
            "codec_type": "video",
            "codec_name": "h264",
            "profile": "High",
            "pix_fmt": "yuv420p",
            "bits_per_raw_sample": "8",
            "width": 1920,
            "height": 1080,
        },
        {"index": 1, "codec_type": "audio", "codec_name": "aac", "tags": {"language": "eng"}},
    ]
    for index, subtitle in enumerate(subtitles, start=2):
        streams.append(
            {
                "index": index,
                "codec_type": "subtitle",
                "codec_name": subtitle["codec_name"],
                "disposition": {"default": subtitle.get("default", 0), "forced": subtitle.get("forced", 0)},
                "tags": {"language": subtitle.get("language", "eng"), "title": subtitle.get("title", "")},
            }
        )
    return json.dumps({"format": {"duration": "100", "format_name": "matroska,webm", "size": "9000"}, "streams": streams})


def test_subtitle_plan_generation_with_srt_already_present(tmp_path):
    scanned = _source(tmp_path / "movie.mkv", subtitles=[SubtitleStream(index=2, codec="subrip", language="eng", default=True)])

    plan = generate_subtitle_plan(scanned, content_type="movie", subtitle_policy="prefer_srt_preserve_original")

    assert plan.statuses == ["no_action_needed"]
    assert plan.text_subtitles_detected is True
    assert plan.actions == []


def test_subtitle_plan_generation_with_pgs_only_and_default_cleanup(tmp_path):
    scanned = _source(tmp_path / "movie.mkv", subtitles=[SubtitleStream(index=4, codec="hdmv_pgs_subtitle", language="eng", default=True)])

    plan = generate_subtitle_plan(scanned, content_type="movie", subtitle_policy="ocr_image_subtitles_to_srt_preserve_original")

    assert "needs_ocr_to_srt" in plan.statuses
    assert "needs_default_flag_cleanup" in plan.statuses
    assert plan.image_subtitles_detected is True
    assert plan.image_subtitles_default is True
    assert {"type": "unset_default", "source_stream_index": 4, "reason": "image subtitle should not be default"} in plan.actions
    assert any(action["type"] == "ocr_to_srt" and action["source_stream_index"] == 4 for action in plan.actions)


def test_subtitle_plan_generation_with_ass_anime_content(tmp_path):
    scanned = _source(tmp_path / "anime.mkv", audio_lang="jpn", subtitles=[SubtitleStream(index=3, codec="ass", language="eng", title="Signs & Songs")])

    plan = generate_subtitle_plan(scanned, content_type="anime", subtitle_policy="preserve_ass_add_srt_fallback")

    assert plan.ass_subtitles_detected is True
    assert plan.japanese_or_anime is True
    assert "needs_ass_srt_fallback" in plan.statuses
    assert any(action["type"] == "ass_to_srt_fallback" for action in plan.actions)
    assert any("ASS subtitles may include important styling" in warning for warning in plan.warnings)


def test_subtitle_plan_generation_with_no_subtitles_and_japanese_audio(tmp_path):
    scanned = _source(tmp_path / "movie.mkv", audio_lang="jpn", subtitles=[])

    plan = generate_subtitle_plan(scanned, content_type="movie", subtitle_policy="generate_missing_srt_unverified")

    assert "needs_missing_subtitle_generation" in plan.statuses
    assert plan.generated_subtitles_unverified is True
    assert any(action["type"] == "generate_missing_srt" for action in plan.actions)


def test_work_order_payload_includes_subtitle_plan_json(tmp_path):
    config = _config(tmp_path)
    source = _source(tmp_path / "movie.mkv", audio_lang="jpn", subtitles=[SubtitleStream(index=4, codec="hdmv_pgs_subtitle", language="eng", default=True)])
    job = _job_review(1)
    decision = _decision(7)

    payload = build_fileflows_item_payload(config, 1, 7, Path(source.path), job, decision, source)

    assert payload["subtitle_plan"]["policy"] == "ocr_image_subtitles_to_srt_preserve_original"
    assert payload["subtitle_plan"]["preferred_format"] == "srt"
    assert payload["subtitle_plan"]["preserve_original_subtitles"] is False
    assert any(action["type"] == "ocr_to_srt" for action in payload["subtitle_plan"]["actions"])


def test_create_ffmpeg_processing_jobs_persists_subtitle_plan(tmp_path):
    config = _config(tmp_path)
    db = Database(tmp_path / "disc_steward.sqlite3")
    db.initialize()
    disc = tmp_path / "media-pipeline" / "01_disc_rips_raw" / "DISC"
    disc.mkdir(parents=True)
    media = disc / "movie.mkv"
    media.write_bytes(b"source" * 1000)
    job_id = db.upsert_job(disc, "reviewed")
    source_id = db.upsert_source_file(job_id, _source(media, subtitles=[SubtitleStream(index=4, codec="hdmv_pgs_subtitle", language="eng", default=True)]))
    review = _job_review(job_id)
    db.save_job_review(review)
    decision = _decision(source_id, encoding_profile="remux_only", generated_final_path=str(generate_final_paths(config, review, [_decision(source_id, encoding_profile="remux_only")])[source_id].final_path))
    db.save_file_review(decision)

    folder = create_ffmpeg_processing_jobs(db, config, job_id, ffmpeg_runner=lambda command: Path(command[-1]).write_bytes(b"ffmpeg-output" * 300))

    item = json.loads((folder / "items" / "item_001.process.json").read_text(encoding="utf-8"))
    assert item["subtitle_plan"]["image_subtitles_default"] is True
    assert item["subtitle_plan"]["statuses"] == ["needs_ocr_to_srt", "needs_default_flag_cleanup"]
    assert item["subtitle_outputs"] == [
        {
            "source_stream_index": 4,
            "source_stream_ordinal": 0,
            "codec": "hdmv_pgs_subtitle",
            "language": "eng",
            "kind": "ocr",
            "output_name": "Test Movie (2001).sub01.eng.hdmv_pgs_subtitle.srt",
            "generated_unverified": True,
        }
    ]
    assert db.get_subtitle_plan(source_id)["statuses"] == ["needs_ocr_to_srt", "needs_default_flag_cleanup"]


def test_extract_text_subtitle_sidecar_does_not_require_ocr_runtime(tmp_path, monkeypatch):
    source = _source(tmp_path / "movie.mkv", subtitles=[SubtitleStream(index=6, codec="subrip", language="eng")])
    monkeypatch.setattr(subtitle_extraction, "RapidOCR", None)

    def fake_ffmpeg(command: list[str]) -> None:
        Path(command[-1]).write_text("1\n00:00:00,000 --> 00:00:01,000\nSubtitle\n\n", encoding="utf-8")

    results = extract_subtitle_sidecars("ffmpeg", "ffprobe", source, tmp_path, "Movie.mkv", ffmpeg_runner=fake_ffmpeg)

    assert len(results) == 1
    assert results[0].kind == "text"
    assert (tmp_path / "Movie.sub01.eng.subrip.srt").read_text(encoding="utf-8").startswith("1\n")


def test_extract_subtitle_sidecars_preserves_every_text_track(tmp_path):
    source = _source(
        tmp_path / "movie.mkv",
        subtitles=[
            SubtitleStream(index=6, codec="subrip", language="eng"),
            SubtitleStream(index=7, codec="subrip", language="spa"),
        ],
    )
    seen_streams: list[str] = []

    def fake_ffmpeg(command: list[str]) -> None:
        stream = command[command.index("-map") + 1]
        seen_streams.append(stream)
        language = "English" if stream == "0:6" else "Spanish"
        Path(command[-1]).write_text(
            f"1\n00:00:00,000 --> 00:00:01,000\n{language}\n\n",
            encoding="utf-8",
        )

    results = extract_subtitle_sidecars("ffmpeg", "ffprobe", source, tmp_path, "Movie.mkv", ffmpeg_runner=fake_ffmpeg)

    assert seen_streams == ["0:6", "0:7"]
    assert [result.output_name for result in results] == [
        "Movie.sub01.eng.subrip.srt",
        "Movie.sub02.spa.subrip.srt",
    ]
    assert (tmp_path / "Movie.sub01.eng.subrip.srt").read_text(encoding="utf-8").find("English") >= 0
    assert (tmp_path / "Movie.sub02.spa.subrip.srt").read_text(encoding="utf-8").find("Spanish") >= 0


def test_extract_subtitle_sidecars_skips_ignored_stream_but_preserves_source_ordinal(tmp_path):
    source = _source(
        tmp_path / "movie.mkv",
        subtitles=[
            SubtitleStream(index=6, codec="subrip", language="eng"),
            SubtitleStream(index=8, codec="dvd_subtitle", language="spa"),
            SubtitleStream(index=10, codec="subrip", language="eng"),
        ],
    )
    seen_streams: list[str] = []

    def fake_ffmpeg(command: list[str]) -> None:
        if "-map" in command:
            seen_streams.append(command[command.index("-map") + 1])
            Path(command[-1]).write_text("1\n00:00:00,000 --> 00:00:01,000\nSubtitle\n\n", encoding="utf-8")

    results = extract_subtitle_sidecars(
        "ffmpeg",
        "ffprobe",
        source,
        tmp_path,
        "Movie.mkv",
        ffmpeg_runner=fake_ffmpeg,
        ignored_source_stream_indexes={8},
    )

    assert seen_streams == ["0:6", "0:10"]
    assert [result.source_stream_index for result in results] == [6, 10]
    assert [result.output_name for result in results] == [
        "Movie.sub01.eng.subrip.srt",
        "Movie.sub03.eng.subrip.srt",
    ]


def test_extract_subtitle_sidecars_rejects_disabled_image_ocr(tmp_path, monkeypatch):
    source = _source(
        tmp_path / "movie.mkv",
        subtitles=[
            SubtitleStream(index=6, codec="subrip", language="eng"),
            SubtitleStream(index=7, codec="dvd_subtitle", language="eng"),
        ],
    )
    monkeypatch.setattr(subtitle_extraction, "RapidOCR", None)

    def fake_ffmpeg(command: list[str]) -> None:
        Path(command[-1]).write_text("1\n00:00:00,000 --> 00:00:01,000\nSubtitle\n\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="must be enabled"):
        extract_subtitle_sidecars(
            "ffmpeg",
            "ffprobe",
            source,
            tmp_path,
            "Movie.mkv",
            ffmpeg_runner=fake_ffmpeg,
            convert_image_subtitles_to_srt=False,
        )


def test_sidecar_coverage_rejects_a_missing_source_stream(tmp_path):
    source = _source(
        tmp_path / "movie.mkv",
        subtitles=[SubtitleStream(index=3, codec="subrip", language="eng"), SubtitleStream(index=4, codec="subrip", language="jpn")],
    )

    with pytest.raises(RuntimeError, match="source stream 4"):
        work_orders._require_subtitle_sidecar_coverage(source, [{"source_stream_index": 3}])


def test_ffprobe_runner_captures_packet_json_locally(tmp_path, monkeypatch):
    calls: list[tuple[list[str], dict]] = []

    class Result:
        stdout = '{"packets": []}'

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return Result()

    monkeypatch.setattr(work_orders.subprocess, "run", fake_run)

    result = work_orders.build_ffprobe_runner(_config(tmp_path))(["ffprobe", "-show_packets", "movie.mkv"])

    assert result == '{"packets": []}'
    assert calls == [(["ffprobe", "-show_packets", "movie.mkv"], {"check": True, "capture_output": True, "text": True})]


def test_extract_image_subtitle_keeps_blank_ocr_frames_aligned(tmp_path, monkeypatch):
    source = _source(tmp_path / "movie.mkv", subtitles=[SubtitleStream(index=3, codec="dvd_subtitle", language="eng")])
    monkeypatch.setattr(
        subtitle_extraction,
        "_subtitle_packets",
        lambda _ffprobe_path, _source_path, _subtitle_ordinal: [
            {"pts_time": "10.0", "duration_time": "1.0"},
            {"pts_time": "20.0", "duration_time": "1.0"},
        ],
    )

    def fake_render(
        _ffmpeg_path,
        _source_path,
        _subtitle_ordinal,
        _width,
        _height,
        _duration_seconds,
        _fps,
        frame_indices,
        output_dir,
        **_kwargs,
    ) -> None:
        for index, _frame_index in enumerate(frame_indices, start=1):
            (output_dir / f"frame_{index:05d}.png").write_bytes(b"png")

    monkeypatch.setattr(subtitle_extraction, "_render_subtitle_frame_sequence", fake_render)

    class FakeOcr:
        calls = 0

        def __call__(self, _image_path):
            self.calls += 1
            if self.calls == 1:
                return [None]
            return [[([[0, 0], [1, 0], [1, 1], [0, 1]], "Correct cue", 0.9)]]

    results = extract_subtitle_sidecars("ffmpeg", "ffprobe", source, tmp_path, "Movie.mkv", ocr_engine=FakeOcr())

    assert len(results) == 1
    contents = (tmp_path / "Movie.sub01.eng.dvd_subtitle.srt").read_text(encoding="utf-8")
    assert "00:00:20,000 --> 00:00:21,000" in contents
    assert "Correct cue" in contents
    assert "00:00:10,000 --> 00:00:11,000" not in contents


def test_image_subtitle_without_packets_fails_instead_of_being_dropped(tmp_path, monkeypatch):
    source = _source(tmp_path / "movie.mkv", subtitles=[SubtitleStream(index=3, codec="dvd_subtitle", language="fra")])
    monkeypatch.setattr(subtitle_extraction, "_subtitle_packets", lambda *_args, **_kwargs: [])

    with pytest.raises(RuntimeError, match="no packets"):
        extract_subtitle_sidecars("ffmpeg", "ffprobe", source, tmp_path, "Movie.mkv", ocr_engine=object())


def test_image_subtitle_with_no_ocr_cues_fails_instead_of_writing_empty_srt(tmp_path, monkeypatch):
    source = _source(tmp_path / "movie.mkv", subtitles=[SubtitleStream(index=3, codec="dvd_subtitle", language="jpn")])
    monkeypatch.setattr(
        subtitle_extraction,
        "_subtitle_packets",
        lambda *_args, **_kwargs: [{"pts_time": "10.0", "duration_time": "1.0"}],
    )

    def fake_render(*args, **kwargs):
        output_dir = args[8]
        (output_dir / "frame_00001.png").write_bytes(b"png")

    monkeypatch.setattr(subtitle_extraction, "_render_subtitle_frame_sequence", fake_render)

    with pytest.raises(RuntimeError, match="no recognized text"):
        extract_subtitle_sidecars("ffmpeg", "ffprobe", source, tmp_path, "Movie.mkv", ocr_engine=lambda _path: [None])


def test_image_subtitle_packet_probe_can_use_injected_runner(tmp_path, monkeypatch):
    source = _source(tmp_path / "movie.mkv", subtitles=[SubtitleStream(index=3, codec="dvd_subtitle", language="deu")])
    commands: list[list[str]] = []

    def probe_runner(command: list[str]) -> str:
        commands.append(command)
        return json.dumps({"packets": [{"pts_time": "10.0", "duration_time": "1.0"}]})

    def fake_render(*args, **kwargs):
        output_dir = args[8]
        (output_dir / "frame_00001.png").write_bytes(b"png")

    monkeypatch.setattr(subtitle_extraction, "_render_subtitle_frame_sequence", fake_render)

    class FakeOcr:
        def __call__(self, _image_path):
            return [[([[0, 0], [1, 0], [1, 1], [0, 1]], "Hallo", 0.9)]]

    results = extract_subtitle_sidecars(
        "ffmpeg",
        "ffprobe",
        source,
        tmp_path,
        "Movie.mkv",
        ocr_engine=FakeOcr(),
        ffprobe_runner=probe_runner,
    )

    assert commands[0][commands[0].index("-select_streams") + 1] == "s:0"
    assert results[0].output_name.endswith(".deu.dvd_subtitle.srt")


def test_subtitle_planning_enables_image_ocr_by_default(tmp_path):
    assert AppConfig.default_for_root(tmp_path).subtitle_planning.convert_image_subtitles_to_srt is True


def test_sidecar_validation_rejects_malformed_srt_and_missing_source_stream(tmp_path):
    source = _source(
        tmp_path / "movie.mkv",
        subtitles=[
            SubtitleStream(index=3, codec="subrip", language="eng"),
            SubtitleStream(index=4, codec="subrip", language="jpn"),
        ],
    )
    malformed = tmp_path / "Movie.sub01.eng.subrip.srt"
    malformed.write_text("subtitle text only\n", encoding="utf-8")
    item = OutputValidationItem(
        source_file_id=1,
        expected_output_name="Movie.mkv",
        expected_final_path="/library/Movie.mkv",
        profile="universal_h264_aac_srt",
        subtitle_policy="prefer_srt_preserve_original",
    )

    _validate_subtitle_sidecars(
        item,
        tmp_path,
        source.subtitle_streams,
        [{"source_stream_index": 3, "output_name": malformed.name}],
    )

    assert any("invalid SRT" in error for error in item.errors)
    assert any("missing subtitle sidecar result for source stream 4" in error for error in item.errors)


def test_image_subtitle_render_is_bounded_to_synthetic_clip(tmp_path):
    commands: list[list[str]] = []

    def fake_ffmpeg(command: list[str]) -> None:
        commands.append(command)
        (tmp_path / "frame_00001.png").write_bytes(b"png")

    subtitle_extraction._render_subtitle_frame_sequence(
        "ffmpeg",
        "movie.mkv",
        0,
        720,
        480,
        5.0,
        2,
        [1],
        tmp_path,
        ffmpeg_runner=fake_ffmpeg,
        source_offset=10.0,
    )

    duration_input = commands[0][commands[0].index("-i") + 1]
    assert duration_input.endswith("r=2:d=5.000")


def test_image_subtitle_render_keeps_synthetic_clip_alive_after_subtitle_eof(tmp_path):
    commands: list[list[str]] = []

    def fake_ffmpeg(command: list[str]) -> None:
        commands.append(command)
        (tmp_path / "frame_00001.png").write_bytes(b"png")

    subtitle_extraction._render_subtitle_frame_sequence(
        "ffmpeg",
        "movie.mkv",
        0,
        720,
        480,
        189.044,
        2,
        [4, 12, 19],
        tmp_path,
        ffmpeg_runner=fake_ffmpeg,
        source_offset=67.468,
    )

    filter_value = commands[0][commands[0].index("-filter_complex") + 1]
    assert "shortest=0" in filter_value
    assert "eof_action=pass" in filter_value
    assert "shortest=1" not in filter_value
    assert "eof_action=endall" not in filter_value


def test_subtitle_validation_checks_selected_plan_with_warnings(tmp_path):
    source = _source(tmp_path / "movie.mkv", subtitles=[SubtitleStream(index=4, codec="hdmv_pgs_subtitle", language="eng", default=True)])
    plan = generate_subtitle_plan(source, content_type="movie", subtitle_policy="ocr_image_subtitles_to_srt_preserve_original")
    parsed = _source(
        tmp_path / "out.mkv",
        subtitles=[SubtitleStream(index=4, codec="hdmv_pgs_subtitle", language="eng", default=False)],
    )

    result = validate_subtitle_plan_result(plan, parsed)

    assert result.passed is True
    assert result.warnings == []


def test_job_validation_fails_when_expected_image_sidecar_is_missing(tmp_path):
    config = _config(tmp_path)
    db = Database(tmp_path / "disc_steward.sqlite3")
    db.initialize()
    disc = tmp_path / "media-pipeline" / "01_disc_rips_raw" / "DISC"
    disc.mkdir(parents=True)
    media = disc / "movie.mkv"
    media.write_bytes(b"source" * 1000)
    job_id = db.upsert_job(disc, "reviewed")
    source_id = db.upsert_source_file(job_id, _source(media, subtitles=[SubtitleStream(index=4, codec="hdmv_pgs_subtitle", language="eng", default=True)]))
    review = _job_review(job_id)
    db.save_job_review(review)
    decision = _decision(source_id, generated_final_path=str(generate_final_paths(config, review, [_decision(source_id)])[source_id].final_path))
    db.save_file_review(decision)
    previous_dry_run = config.dry_run
    config.dry_run = True
    try:
        folder = create_ffmpeg_processing_jobs(db, config, job_id, ffmpeg_runner=lambda command: Path(command[-1]).write_bytes(b"ffmpeg-output" * 300))
    finally:
        config.dry_run = previous_dry_run
    item = json.loads((folder / "items" / "item_001.process.json").read_text(encoding="utf-8"))
    output = config.validation_needed_path / f"job_{job_id}" / Path(decision.generated_final_path).name
    output.parent.mkdir(parents=True)
    output.write_bytes(b"output" * 1000)

    summary = validate_job_outputs(db, config, job_id, ffprobe_runner=lambda _path: _ffprobe_output(subtitles=[{"codec_name": "hdmv_pgs_subtitle", "default": 0}]))

    assert summary.passed is False
    assert any("missing subtitle sidecar" in error for error in summary.items[0].errors)


def test_auto_backend_uses_tesseract_for_japanese_image_subtitles(tmp_path, monkeypatch):
    source = _source(
        tmp_path / "anime.mkv",
        subtitles=[SubtitleStream(index=4, codec="dvd_subtitle", language="jpn")],
    )
    created: list[tuple[str, str]] = []

    class FakeTesseract:
        def __init__(self, *, tesseract_path, language):
            created.append((tesseract_path, language))

        def __call__(self, _image_path):
            return [[([[0, 0], [1, 0], [1, 1], [0, 1]], "こんにちは", 0.99)]]

    monkeypatch.setattr(subtitle_extraction, "TesseractOCR", FakeTesseract)
    monkeypatch.setattr(
        subtitle_extraction,
        "_subtitle_packets",
        lambda *_args, **_kwargs: [{"pts_time": "1.0", "duration_time": "1.0"}],
    )

    def fake_render(*args, **kwargs):
        args[8].joinpath("frame_00001.png").write_bytes(b"png")

    monkeypatch.setattr(subtitle_extraction, "_render_subtitle_frame_sequence", fake_render)

    results = extract_subtitle_sidecars(
        "ffmpeg",
        "ffprobe",
        source,
        tmp_path,
        "Anime.mkv",
        ocr_backend="auto",
        tesseract_path="/usr/bin/tesseract",
    )

    assert len(results) == 1
    assert created == [("/usr/bin/tesseract", "jpn+eng")]
    assert "こんにちは" in (tmp_path / results[0].output_name).read_text(encoding="utf-8")


def test_tesseract_backend_returns_utf8_lines(tmp_path, monkeypatch):
    calls: list[list[str]] = []

    class Result:
        stdout = "こんにちは\n世界。\n"
        stderr = ""

    def fake_run(command, **kwargs):
        calls.append(command)
        assert kwargs["encoding"] == "utf-8"
        return Result()

    monkeypatch.setattr(subtitle_extraction.subprocess, "run", fake_run)
    engine = subtitle_extraction.TesseractOCR(tesseract_path="tesseract", language="jpn+eng")

    result = engine(tmp_path / "frame.png")

    assert [entry[1] for entry in result[0]] == ["こんにちは", "世界。"]
    assert calls[0][-2:] == ["-l", "jpn+eng"]


def test_sidecar_validation_ignores_selected_source_stream(tmp_path):
    source_streams = [
        SubtitleStream(index=3, codec="subrip", language="eng"),
        SubtitleStream(index=4, codec="dvd_subtitle", language="jpn"),
    ]
    sidecar = tmp_path / "Movie.sub01.eng.subrip.srt"
    sidecar.write_text("1\n00:00:00,000 --> 00:00:01,000\nEnglish\n\n", encoding="utf-8")
    item = OutputValidationItem(
        source_file_id=1,
        expected_output_name="Movie.mkv",
        expected_final_path="/library/Movie.mkv",
        profile="universal_h264_aac_srt",
        subtitle_policy="prefer_srt_preserve_original",
    )

    _validate_subtitle_sidecars(
        item,
        tmp_path,
        source_streams,
        [{"source_stream_index": 3, "output_name": sidecar.name}],
        ignored_source_stream_indexes={4},
    )

    assert item.errors == []
