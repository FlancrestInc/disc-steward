from __future__ import annotations

import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .models import Classification, ScannedFile


@dataclass(frozen=True)
class AutomaticLabel:
    role: str
    display_name: str
    extra_type: str
    evidence: str
    confidence: float
    content_type: str | None = None
    season_number: int | None = None
    episode_number: int | None = None


_BONUS_MARKERS = (
    "bonus",
    "extra",
    "special features",
    "special_feature",
    "behind the scenes",
    "bonus disc",
)


def looks_like_bonus_disc(folder_name: str) -> bool:
    normalized = re.sub(r"[_-]+", " ", folder_name).lower()
    return any(marker in normalized for marker in _BONUS_MARKERS)


def extract_title_card_text(
    scanned: ScannedFile,
    ffmpeg_path: str,
    *,
    sample_count: int = 3,
    max_chars: int = 1600,
    ocr_engine=None,
    runner: Callable[..., object] | None = None,
) -> str:
    """OCR a few frames without writing anything into the media tree.

    OCR is deliberately best-effort. A failed OCR pass must not prevent a rip
    from entering the normal review queue.
    """
    if not scanned.duration_seconds or scanned.duration_seconds <= 0:
        return ""
    try:
        from rapidocr_onnxruntime import RapidOCR
    except ImportError:
        return ""
    engine = ocr_engine or RapidOCR()
    timestamps = _sample_timestamps(scanned.duration_seconds, sample_count)
    texts: list[str] = []
    command_runner = runner or subprocess.run
    with tempfile.TemporaryDirectory(prefix="disc-steward-ocr-") as temp_dir:
        for index, timestamp in enumerate(timestamps):
            frame_path = Path(temp_dir) / f"frame-{index}.png"
            try:
                command_runner(
                    [
                        ffmpeg_path,
                        "-hide_banner",
                        "-loglevel",
                        "error",
                        "-ss",
                        str(timestamp),
                        "-i",
                        scanned.path,
                        "-frames:v",
                        "1",
                        "-vf",
                        "scale=1280:-1",
                        "-y",
                        str(frame_path),
                    ],
                    check=True,
                    capture_output=True,
                )
                result, _ = engine(str(frame_path))
            except Exception:
                continue
            for item in result or []:
                if isinstance(item, (list, tuple)) and len(item) >= 2:
                    value = str(item[1]).strip()
                    if value and value not in texts:
                        texts.append(value)
    return " | ".join(texts)[:max_chars]


def infer_bonus_label(
    scanned: ScannedFile,
    classification: Classification,
    *,
    ocr_text: str = "",
) -> AutomaticLabel | None:
    """Infer a conservative local label from visible/title-card evidence."""
    evidence = " ".join(
        value for value in (scanned.filename, scanned.embedded_title, scanned.makemkv_title, ocr_text) if value
    )
    text = evidence.lower()

    rules = [
        (("self portrait", "rattlin cages", "fan-made", "fan made"), "extra", "fan_animation", "LEGO fan animation", 0.9),
        (("music video", "everything is awesome", "sing along", "sing-along"), "music_video", "music_video", "Music video", 0.94),
        (("deleted scene", "deleted scenes"), "deleted_scene", "deleted_scene", "Deleted scene", 0.94),
        (("once upon",), "short_film", "short_film", "Short film", 0.92),
        (("trailer", "teaser", "promo"), "trailer", "trailer", "Trailer", 0.91),
        (("interview", "art director", "modeling artist", "senior designer"), "interview", "interview", "Interview", 0.88),
        (("storyboard", "story board", "animation test"), "featurette", "animation_test", "Storyboard / animation test", 0.9),
        (("digital designer", "to download", "idd lego"), "extra", "digital_designer", "LEGO Digital Designer extra", 0.92),
        (("bringing lego to life", "building lego", "building a lego", "building the lego", "behind the scenes", "making of", "making-of"), "featurette", "making_of", "Behind the scenes", 0.9),
    ]
    for markers, role, extra_type, label, confidence in rules:
        matched = next((marker for marker in markers if marker in text), None)
        if matched:
            title = _title_from_evidence(ocr_text, label)
            return AutomaticLabel(role, title, extra_type, f"title-card/text marker: {matched}", confidence)

    duration = scanned.duration_seconds or 0
    if 45 <= duration < 240:
        return AutomaticLabel("trailer", _fallback_name(scanned, "Trailer"), "duration_45_to_240s", "duration heuristic", 0.42)
    if 240 <= duration < 3600:
        return AutomaticLabel("featurette", _fallback_name(scanned, "Featurette"), "duration_4_to_60m", "duration heuristic", 0.43)
    if 0 < duration < 45:
        return AutomaticLabel("menu_or_bumper", _fallback_name(scanned, "Menu / bumper"), "very_short_title", "duration heuristic", 0.38)
    if classification.probable_extra:
        return AutomaticLabel("extra", _fallback_name(scanned, "Bonus extra"), "extra", "classifier marked probable extra", 0.35)
    return None


def build_automatic_bonus_labels(
    scanned_files: list[ScannedFile],
    classifications: dict[str, Classification],
    folder_name: str,
    ffmpeg_path: str,
    *,
    ocr_enabled: bool = True,
    sample_count: int = 3,
    max_chars: int = 1600,
    text_extractor: Callable[[ScannedFile], str] | None = None,
    include_all_files: bool = False,
) -> dict[str, AutomaticLabel]:
    """Build labels for likely extras while leaving uncertain files alone."""
    bonus_disc = looks_like_bonus_disc(folder_name)
    labels: dict[str, AutomaticLabel] = {}
    ocr_engine = None
    if ocr_enabled and not text_extractor:
        try:
            from rapidocr_onnxruntime import RapidOCR

            ocr_engine = RapidOCR()
        except Exception:
            ocr_enabled = False
    for scanned in scanned_files:
        classification = classifications[scanned.path]
        if not include_all_files and not bonus_disc and not _looks_like_extra_candidate(scanned, classification):
            continue
        ocr_text = ""
        ocr_candidate = bonus_disc or _looks_like_extra_candidate(scanned, classification)
        if ocr_enabled and ocr_candidate and (scanned.duration_seconds or 0) <= 3600:
            if text_extractor:
                ocr_text = text_extractor(scanned)
            else:
                ocr_text = extract_title_card_text(
                    scanned,
                    ffmpeg_path,
                    sample_count=sample_count,
                    max_chars=max_chars,
                    ocr_engine=ocr_engine,
                )
        label = infer_bonus_label(scanned, classification, ocr_text=ocr_text)
        if label:
            labels[scanned.path] = label
    return labels


def _looks_like_extra_candidate(scanned: ScannedFile, classification: Classification) -> bool:
    if classification.probable_main_feature:
        return False
    title = " ".join(value for value in (scanned.filename, scanned.embedded_title, scanned.makemkv_title) if value).lower()
    return (
        classification.probable_extra
        or classification.probable_trailer
        or classification.probable_featurette
        or any(marker in title for marker in ("bonus", "extra", "trailer", "teaser", "featurette", "interview", "deleted"))
    )


def _sample_timestamps(duration: float, sample_count: int) -> list[float]:
    count = max(1, min(sample_count, 5))
    if count == 1:
        return [min(3.0, max(0.1, duration / 2))]
    return [min(duration - 0.1, max(0.1, duration * fraction)) for fraction in (0.05, 0.5, 0.9)][:count]


def _title_from_evidence(ocr_text: str, fallback: str) -> str:
    cleaned = re.sub(r"\s+", " ", ocr_text or "").strip(" |-")
    if cleaned:
        for value in re.split(r"\s*[|]\s*", cleaned):
            value = value.strip()
            if 3 <= len(value) <= 90:
                return value
    return fallback


def _fallback_name(scanned: ScannedFile, fallback: str) -> str:
    for value in (scanned.embedded_title, scanned.makemkv_title):
        if value and value.strip():
            return value.strip()
    stem = Path(scanned.filename).stem.replace("_", " ").replace("-", " ").strip()
    return stem if stem and not re.fullmatch(r"[A-Za-z]\d+ t\d+", stem, flags=re.IGNORECASE) else fallback