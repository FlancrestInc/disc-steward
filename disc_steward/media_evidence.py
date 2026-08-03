from __future__ import annotations

import itertools
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from .models import ScannedFile

_SRT_TIMESTAMP = re.compile(
    r"(?P<start>\d{2}:\d{2}:\d{2}(?:,|\.)\d{3})\s+-->\s+"
    r"(?P<end>\d{2}:\d{2}:\d{2}(?:,|\.)\d{3})"
)
_WORD_RE = re.compile(r"\b[\w'’-]{3,}\b", re.UNICODE)
_TITLE_HINTS = re.compile(
    r"\b(episode|chapter|part|story|season|disc|volume|special|trailer|interview|featurette)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class SubtitleExcerpt:
    start_seconds: float
    end_seconds: float
    text: str
    reason: str
    language: str | None = None


@dataclass
class MediaEvidence:
    source_file_id: int | None = None
    subtitle_excerpts: list[SubtitleExcerpt] = field(default_factory=list)
    credit_text: list[str] = field(default_factory=list)
    chapter_titles: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class AggregateRelation:
    aggregate_file_id: int
    component_file_ids: tuple[int, ...]
    duration_delta_seconds: float
    confidence: float
    reason: str = "aggregate duration matches component durations"


def subtitle_excerpts(
    srt_text: str,
    *,
    max_excerpts: int = 8,
    max_chars: int = 1200,
    language: str | None = None,
) -> list[SubtitleExcerpt]:
    """Select bounded, title-relevant excerpts from an SRT-like subtitle track.

    This intentionally does not return the whole subtitle track. Subtitle text is
    evidence for matching, not an automatic instruction source or a prompt dump.
    """
    cues = _parse_srt_cues(srt_text)
    if not cues or max_excerpts <= 0 or max_chars <= 0:
        return []

    selected: list[tuple[int, tuple[float, float, str], str]] = []
    selected.append((0, cues[0], "first subtitle cue"))
    if len(cues) > 1:
        selected.append((len(cues) - 1, cues[-1], "last subtitle cue"))

    for index, cue in enumerate(cues):
        text = cue[2]
        if _TITLE_HINTS.search(text):
            selected.append((index, cue, "title-like subtitle cue"))
        elif _has_distinctive_words(text):
            selected.append((index, cue, "distinctive subtitle cue"))

    unique: list[SubtitleExcerpt] = []
    seen: set[tuple[float, str]] = set()
    for _, (start, end, text), reason in sorted(selected, key=lambda item: item[0]):
        cleaned = _clean_text(text)
        key = (start, cleaned)
        if not cleaned or key in seen:
            continue
        seen.add(key)
        unique.append(SubtitleExcerpt(start, end, cleaned, reason, language))
        if len(unique) >= max_excerpts:
            break

    while _serialized_chars(unique) > max_chars and unique:
        unique.pop()
    return unique


def build_media_evidence(
    scanned: ScannedFile,
    *,
    source_file_id: int | None = None,
    subtitle_tracks: Iterable[tuple[str | None, str]] = (),
    credit_text: Iterable[str] = (),
    chapter_titles: Iterable[str] = (),
    ocr_text: str | None = None,
    max_excerpts: int = 8,
    max_chars: int = 1200,
) -> MediaEvidence:
    evidence = MediaEvidence(source_file_id=source_file_id)
    for language, text in subtitle_tracks:
        evidence.subtitle_excerpts.extend(
            subtitle_excerpts(text, max_excerpts=max_excerpts, max_chars=max_chars, language=language)
        )
    evidence.subtitle_excerpts = evidence.subtitle_excerpts[:max_excerpts]
    evidence.credit_text = [_clean_text(value) for value in credit_text if _clean_text(value)]
    evidence.credit_text = evidence.credit_text[:max_excerpts]
    evidence.chapter_titles = [_clean_text(value) for value in chapter_titles if _clean_text(value)]
    evidence.chapter_titles = evidence.chapter_titles[:max_excerpts]
    if ocr_text:
        evidence.credit_text.extend(_ocr_title_lines(ocr_text, max_excerpts=max_excerpts, max_chars=max_chars))
        evidence.credit_text = evidence.credit_text[:max_excerpts]
        evidence.warnings.append("OCR-derived title/credit evidence is advisory and bounded")
    if not scanned.subtitle_streams and not evidence.credit_text and not evidence.chapter_titles:
        evidence.warnings.append("no subtitle, credit, or chapter evidence supplied")
    return evidence


def build_media_evidence_from_sidecars(
    scanned: ScannedFile,
    sidecar_paths: Iterable[Path],
    *,
    source_file_id: int | None = None,
    max_sidecar_bytes: int = 256_000,
    max_excerpts: int = 8,
    max_chars: int = 1200,
) -> MediaEvidence:
    """Read bounded subtitle sidecars and convert them to review evidence.

    Sidecars are local artifacts produced by the existing extraction pipeline. The
    reader never sends a complete track: each file is capped before parsing and
    the resulting excerpts are capped again by ``subtitle_excerpts``.
    """
    evidence = MediaEvidence(source_file_id=source_file_id)
    streams = list(scanned.subtitle_streams)
    for index, path in enumerate(sidecar_paths):
        try:
            if not path.is_file():
                evidence.warnings.append(f"subtitle sidecar missing: {path.name}")
                continue
            if path.stat().st_size > max_sidecar_bytes:
                evidence.warnings.append(f"subtitle sidecar truncated at {max_sidecar_bytes} bytes: {path.name}")
            text = path.read_text(encoding="utf-8", errors="replace")[:max_sidecar_bytes]
            language = streams[index].language if index < len(streams) else None
            evidence.subtitle_excerpts.extend(
                subtitle_excerpts(text, max_excerpts=max_excerpts, max_chars=max_chars, language=language)
            )
        except OSError as exc:
            evidence.warnings.append(f"subtitle sidecar read failed for {path.name}: {type(exc).__name__}")
    evidence.subtitle_excerpts = evidence.subtitle_excerpts[:max_excerpts]
    if not evidence.subtitle_excerpts and not evidence.warnings and streams:
        evidence.warnings.append("subtitle sidecars contained no parseable text")
    return evidence


def detect_aggregate_relations(
    files: list[tuple[int, ScannedFile]],
    *,
    tolerance_seconds: float = 5.0,
    max_components: int = 12,
    min_component_duration: float = 120.0,
) -> list[AggregateRelation]:
    """Detect likely aggregate title tracks whose duration equals smaller tracks.

    This is deliberately advisory. It identifies overlapping representations such
    as a full compilation plus individual episodes; it does not decide which file
    should ultimately be imported.
    """
    relations: list[AggregateRelation] = []
    for aggregate_id, aggregate in files:
        aggregate_duration = float(aggregate.duration_seconds or 0)
        if aggregate_duration <= 0:
            continue
        eligible = [
            (file_id, float(file.duration_seconds or 0))
            for file_id, file in files
            if file_id != aggregate_id
            and min_component_duration <= float(file.duration_seconds or 0) < aggregate_duration
        ]
        if len(eligible) < 2:
            continue
        eligible = eligible[:max_components]
        best: tuple[float, tuple[int, ...]] | None = None
        for size in range(2, len(eligible) + 1):
            for subset in itertools.combinations(eligible, size):
                total = sum(duration for _, duration in subset)
                delta = abs(total - aggregate_duration)
                if delta <= tolerance_seconds and (best is None or delta < best[0]):
                    best = (delta, tuple(file_id for file_id, _ in subset))
        if best is None:
            continue
        delta, component_ids = best
        confidence = max(0.0, min(1.0, 1.0 - delta / max(tolerance_seconds, 1.0)))
        relations.append(AggregateRelation(aggregate_id, component_ids, round(delta, 3), confidence))
    return relations


def _parse_srt_cues(text: str) -> list[tuple[float, float, str]]:
    cues: list[tuple[float, float, str]] = []
    blocks = re.split(r"\n\s*\n", text.replace("\r\n", "\n").strip())
    for block in blocks:
        lines = block.splitlines()
        timing_index = next((index for index, line in enumerate(lines) if _SRT_TIMESTAMP.search(line)), None)
        if timing_index is None:
            continue
        match = _SRT_TIMESTAMP.search(lines[timing_index])
        if match is None:
            continue
        body = " ".join(line.strip() for line in lines[timing_index + 1 :] if line.strip())
        if not body:
            continue
        cues.append(
            (
                _timestamp_seconds(match.group("start")),
                _timestamp_seconds(match.group("end")),
                body,
            )
        )
    return cues


def _timestamp_seconds(value: str) -> float:
    hours, minutes, seconds = value.replace(",", ".").split(":")
    return float(hours) * 3600 + float(minutes) * 60 + float(seconds)


def _clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _has_distinctive_words(text: str) -> bool:
    words = [word.lower() for word in _WORD_RE.findall(text)]
    return len(set(words)) >= 3 and any(len(word) >= 7 for word in words)


def _ocr_title_lines(text: str, *, max_excerpts: int, max_chars: int) -> list[str]:
    lines: list[str] = []
    seen: set[str] = set()
    consumed = 0
    for raw_line in str(text or "")[:max_chars].splitlines():
        line = _clean_text(raw_line)
        if not line or len(line) < 3 or line.casefold() in seen:
            continue
        if not (_TITLE_HINTS.search(line) or _has_distinctive_words(line) or "©" in line or "story" in line.casefold() or "directed" in line.casefold()):
            continue
        if consumed + len(line) > max_chars:
            break
        seen.add(line.casefold())
        lines.append(line)
        consumed += len(line)
        if len(lines) >= max_excerpts:
            break
    return lines


def _serialized_chars(excerpts: list[SubtitleExcerpt]) -> int:
    return sum(len(excerpt.text) for excerpt in excerpts)
