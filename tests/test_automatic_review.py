from pathlib import Path

from disc_steward.automatic_review import (
    build_automatic_bonus_labels,
    extract_title_card_text,
    infer_bonus_label,
    looks_like_bonus_disc,
)
from disc_steward.models import Classification, ScannedFile


def _scanned(name: str, duration: float = 120.0) -> ScannedFile:
    return ScannedFile(
        path=f"/media/{name}",
        filename=name,
        parent_disc_folder="/media/BONUS_DISC",
        size_bytes=100,
        modified_time=1.0,
        duration_seconds=duration,
        container_format="matroska",
    )


def test_bonus_folder_markers_are_detected():
    assert looks_like_bonus_disc("The LEGO Movie - Bonus Features")
    assert looks_like_bonus_disc("DISC_2_EXTRAS")
    assert not looks_like_bonus_disc("The LEGO Movie")


def test_title_card_evidence_gets_specific_bonus_label():
    label = infer_bonus_label(
        _scanned("title_t05.mkv"),
        Classification(probable_extra=True),
        ocr_text="Everything Is Awesome | Music Video",
    )

    assert label is not None
    assert label.role == "music_video"
    assert label.extra_type == "music_video"
    assert label.confidence >= 0.9
    assert label.display_name == "Everything Is Awesome"


def test_bonus_batch_uses_injected_text_extractor_without_touching_media(tmp_path):
    scanned = _scanned("title_t01.mkv", duration=150.0)
    calls = []

    labels = build_automatic_bonus_labels(
        [scanned],
        {scanned.path: Classification(probable_extra=True)},
        "BONUS_DISC",
        "ffmpeg",
        text_extractor=lambda item: calls.append(item.path) or "Once Upon a Ninjago",
    )

    assert calls == [scanned.path]
    assert labels[scanned.path].role == "short_film"
    assert labels[scanned.path].display_name == "Once Upon a Ninjago"


def test_ocr_sampling_is_best_effort_and_uses_three_frames(monkeypatch):
    scanned = _scanned("title_t01.mkv", duration=100.0)
    calls = []

    class FakeEngine:
        def __call__(self, image_path):
            return ([[[], "Bringing LEGO to Life", 0.9]], None)

    def fake_runner(command, **kwargs):
        calls.append(command)

    monkeypatch.setattr("rapidocr_onnxruntime.RapidOCR", lambda: FakeEngine())
    text = extract_title_card_text(scanned, "ffmpeg", runner=fake_runner)

    assert text == "Bringing LEGO to Life"
    assert len(calls) == 3
    assert all(command[0] == "ffmpeg" for command in calls)
