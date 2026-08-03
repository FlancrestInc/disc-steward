from disc_steward.media_evidence import (
    build_media_evidence,
    build_media_evidence_from_sidecars,
    detect_aggregate_relations,
    subtitle_excerpts,
)
from disc_steward.models import ScannedFile, SubtitleStream


def _file(name: str, duration: float) -> ScannedFile:
    return ScannedFile(
        path=f"/media/{name}.mkv",
        filename=f"{name}.mkv",
        parent_disc_folder="/media",
        size_bytes=1,
        modified_time=0,
        duration_seconds=duration,
        container_format="matroska,webm",
    )


def test_subtitle_excerpts_are_bounded_and_title_relevant():
    text = """1
00:00:01,000 --> 00:00:02,000
Previously on the series.

2
00:12:00,000 --> 00:12:04,000
Chapter 3: The Final Decision

3
00:30:00,000 --> 00:30:04,000
A long distinctive character name appears here.

4
00:40:00,000 --> 00:40:02,000
The end.
"""

    excerpts = subtitle_excerpts(text, max_excerpts=3, max_chars=200)

    assert len(excerpts) <= 3
    assert sum(len(item.text) for item in excerpts) <= 200
    assert any("Chapter 3" in item.text for item in excerpts)
    assert all(item.start_seconds <= item.end_seconds for item in excerpts)


def test_build_media_evidence_preserves_language_and_warns_when_empty():
    scanned = _file("episode", 1200)
    scanned.subtitle_streams.append(SubtitleStream(index=1, codec="subrip", language="eng"))

    evidence = build_media_evidence(
        scanned,
        source_file_id=42,
        subtitle_tracks=[("eng", "1\n00:00:01,000 --> 00:00:02,000\nEpisode: Mariko")],
        credit_text=["Story: Wolverine"],
        chapter_titles=["Opening"],
    )

    assert evidence.source_file_id == 42
    assert evidence.subtitle_excerpts[0].language == "eng"
    assert evidence.credit_text == ["Story: Wolverine"]
    assert evidence.chapter_titles == ["Opening"]
    assert evidence.warnings == []


def test_build_media_evidence_includes_bounded_ocr_title_lines():
    scanned = _file("episode", 1200)
    evidence = build_media_evidence(
        scanned,
        source_file_id=43,
        ocr_text="Episode 4: Omega Red\nrandom short\nDirected by Example Person\n" + "x" * 300,
        max_excerpts=3,
        max_chars=120,
    )

    assert "Episode 4: Omega Red" in evidence.credit_text
    assert any("OCR-derived" in warning for warning in evidence.warnings)
    assert sum(len(value) for value in evidence.credit_text) <= 120


def test_detect_aggregate_relation_for_compilation_and_components():
    files = [
        (1, _file("aggregate", 8532.1)),
        (2, _file("episode-1", 1420.0)),
        (3, _file("episode-2", 1423.0)),
        (4, _file("episode-3", 1422.0)),
        (5, _file("episode-4", 1423.0)),
        (6, _file("episode-5", 1422.0)),
        (7, _file("episode-6", 1422.0)),
    ]

    relations = detect_aggregate_relations(files)

    assert len(relations) == 1
    assert relations[0].aggregate_file_id == 1
    assert relations[0].component_file_ids == (2, 3, 4, 5, 6, 7)
    assert relations[0].duration_delta_seconds < 1
    assert relations[0].confidence > 0.8


def test_detect_aggregate_relation_does_not_force_unrelated_files():
    files = [
        (1, _file("feature", 3600)),
        (2, _file("extra-1", 600)),
        (3, _file("extra-2", 700)),
    ]

    assert detect_aggregate_relations(files) == []


def test_sidecar_reader_caps_local_text_and_records_missing_files(tmp_path):
    scanned = _file("episode", 1200)
    scanned.subtitle_streams = [SubtitleStream(index=0, codec="subrip", language="eng")]
    sidecar = tmp_path / "episode.srt"
    sidecar.write_text("1\n00:00:01,000 --> 00:00:02,000\nChapter 1: Arrival\n", encoding="utf-8")

    evidence = build_media_evidence_from_sidecars(
        scanned,
        [sidecar, tmp_path / "missing.srt"],
        source_file_id=42,
        max_sidecar_bytes=200,
    )

    assert evidence.source_file_id == 42
    assert evidence.subtitle_excerpts[0].text == "Chapter 1: Arrival"
    assert any("missing" in warning for warning in evidence.warnings)
