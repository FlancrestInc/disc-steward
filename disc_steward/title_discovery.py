from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable

from .models import ScannedFile, TitleDiscoveryResult, TitleDiscoverySignal

_GENERIC_TOKENS = {
    "a",
    "b",
    "disc",
    "disk",
    "dvd",
    "blu",
    "bluray",
    "mkv",
    "movie",
    "part",
    "sample",
    "title",
    "video",
}

_SUFFIX_PATTERNS = [
    r"(?i)\bdisc\s*\d+\b",
    r"(?i)\bdisk\s*\d+\b",
    r"(?i)\bdisc\s*[a-z]\b",
    r"(?i)\btitle[_\s.-]*t?\d+\b",
    r"(?i)\ba\d+_t\d+\b",
    r"(?i)\btitle_t\d+\b",
    r"(?i)\bfeature\b$",
]


def discover_title_from_scan(disc_folder: Path, scanned_files: Iterable[ScannedFile]) -> TitleDiscoveryResult:
    signals: list[TitleDiscoverySignal] = []
    candidates: dict[str, float] = {}

    folder_candidate = _clean_title(disc_folder.name)
    if folder_candidate:
        signal = TitleDiscoverySignal(source="disc_folder", value=folder_candidate, weight=2.5, confidence=0.45)
        signals.append(signal)
        _add_candidate(candidates, folder_candidate, signal.weight)

    for scanned in scanned_files:
        if scanned.embedded_title:
            title = _clean_title(scanned.embedded_title)
            if title:
                signal = TitleDiscoverySignal(source="embedded_title", value=title, weight=4.0, confidence=0.9)
                signals.append(signal)
                _add_candidate(candidates, title, signal.weight)

        if scanned.makemkv_title:
            title = _clean_title(scanned.makemkv_title)
            if title:
                signal = TitleDiscoverySignal(source="makemkv_title", value=title, weight=2.0, confidence=0.35)
                signals.append(signal)
                _add_candidate(candidates, title, signal.weight)

        stem = _clean_title(Path(scanned.filename).stem)
        if stem:
            signal = TitleDiscoverySignal(source="filename_stem", value=stem, weight=1.5, confidence=0.25)
            signals.append(signal)
            _add_candidate(candidates, stem, signal.weight)

        for chapter_title in _chapter_titles(scanned.raw_ffprobe):
            cleaned = _clean_title(chapter_title)
            if cleaned:
                signal = TitleDiscoverySignal(source="chapter_title", value=cleaned, weight=1.0, confidence=0.2)
                signals.append(signal)
                _add_candidate(candidates, cleaned, signal.weight)

    if not candidates:
        fallback = folder_candidate or _clean_title(disc_folder.stem) or disc_folder.name
        result = TitleDiscoveryResult(
            title=fallback,
            content_type="unknown",
            library_root="Movies",
            confidence=0.0,
            signals=signals,
            warnings=["no title signals found"],
        )
        return result

    best_title = max(candidates.items(), key=lambda item: item[1])[0]
    best_score = candidates[best_title]
    total_score = sum(candidates.values())
    confidence = round(min(0.99, best_score / (best_score + 1.5)), 3)
    warnings: list[str] = []
    if len(candidates) > 1:
        warnings.append("multiple title candidates detected")
    if best_score / total_score < 0.6:
        warnings.append("title evidence is mixed")

    return TitleDiscoveryResult(
        title=best_title,
        content_type="unknown",
        library_root="Movies",
        confidence=confidence,
        signals=signals,
        warnings=warnings,
    )


def title_discovery_payload(result: TitleDiscoveryResult) -> dict:
    return asdict(result)


def refine_title_discovery_with_ollama(config, discovery: TitleDiscoveryResult, sender=None) -> TitleDiscoveryResult:
    title_config = getattr(config, "title_discovery", None)
    if not title_config or not title_config.enabled:
        return discovery
    if title_config.provider != "ollama" or not title_config.endpoint or not title_config.model:
        return discovery
    distinct_titles = {signal.value for signal in discovery.signals if signal.value}
    if discovery.confidence >= title_config.min_confidence_to_auto_fill and len(distinct_titles) <= 1:
        return discovery
    payload = {
        "model": title_config.model,
        "stream": False,
        "format": "json",
        "messages": [
            {
                "role": "system",
                "content": (
                    "You normalize disc rip titles from structured evidence. Return JSON only with keys "
                    "preferred_title, original_title, romanized_title, translated_title, year, content_type, "
                    "library_root, confidence, warnings. Do not invent facts."
                ),
            },
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "disc_folder": discovery.signals[0].value if discovery.signals else discovery.title,
                        "current_title": discovery.title,
                        "confidence": discovery.confidence,
                        "signals": [asdict(signal) for signal in discovery.signals],
                    },
                    ensure_ascii=False,
                ),
            },
        ],
    }
    endpoint = _ollama_chat_endpoint(title_config.endpoint)
    send = sender or _default_sender
    response = send(endpoint, payload)
    parsed = _parse_ollama_payload(response)
    if not parsed:
        return discovery
    suggested_title = _clean_title(str(parsed.get("preferred_title") or parsed.get("title") or ""))
    if not suggested_title:
        return discovery
    confidence = _float_or_none(parsed.get("confidence")) or 0.0
    warnings = list(discovery.warnings)
    if confidence < title_config.min_confidence_to_auto_fill:
        warnings.append("ollama title suggestion below auto-fill threshold")
        return TitleDiscoveryResult(
            title=discovery.title,
            original_title=parsed.get("original_title", discovery.original_title),
            romanized_title=parsed.get("romanized_title", discovery.romanized_title),
            translated_title=parsed.get("translated_title", discovery.translated_title),
            year=_int_or_none(parsed.get("year"), discovery.year),
            content_type=str(parsed.get("content_type") or discovery.content_type),
            library_root=str(parsed.get("library_root") or discovery.library_root),
            confidence=max(discovery.confidence, confidence),
            signals=discovery.signals + [TitleDiscoverySignal(source="ollama", value=suggested_title, confidence=confidence, notes=["below threshold"])],
            warnings=warnings,
        )
    warnings.extend(_warning_list(parsed.get("warnings")))
    return TitleDiscoveryResult(
        title=suggested_title,
        original_title=parsed.get("original_title", discovery.original_title),
        romanized_title=parsed.get("romanized_title", discovery.romanized_title),
        translated_title=parsed.get("translated_title", discovery.translated_title),
        year=_int_or_none(parsed.get("year"), discovery.year),
        content_type=str(parsed.get("content_type") or discovery.content_type),
        library_root=str(parsed.get("library_root") or discovery.library_root),
        confidence=max(discovery.confidence, confidence),
        signals=discovery.signals + [TitleDiscoverySignal(source="ollama", value=suggested_title, confidence=confidence, notes=_warning_list(parsed.get("warnings")))],
        warnings=warnings,
    )


def _default_sender(endpoint: str, payload: dict) -> dict:
    from urllib.request import Request, urlopen

    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = Request(endpoint, data=body, headers={"Content-Type": "application/json"}, method="POST")
    with urlopen(request, timeout=30) as response:  # noqa: S310 - endpoint is user-configured and disabled by default
        return json.loads(response.read().decode("utf-8") or "{}")


def _ollama_chat_endpoint(endpoint: str) -> str:
    base = endpoint.rstrip("/")
    return base if base.endswith("/api/chat") else f"{base}/api/chat"


def _parse_ollama_payload(response: object) -> dict:
    if not isinstance(response, dict):
        return {}
    message = response.get("message")
    if isinstance(message, dict) and isinstance(message.get("content"), str):
        content = message["content"].strip()
        if content:
            try:
                parsed = json.loads(content)
            except json.JSONDecodeError:
                return {}
            return parsed if isinstance(parsed, dict) else {}
    return response


def _warning_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str) and item]


def _int_or_none(value: object, fallback: int | None = None) -> int | None:
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return fallback
    if isinstance(value, float):
        return int(value)
    return fallback


def _float_or_none(value: object) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def _add_candidate(candidates: dict[str, float], value: str, weight: float) -> None:
    candidates[value] = candidates.get(value, 0.0) + weight


def _clean_title(value: str) -> str:
    text = value.replace("_", " ").replace(".", " ").replace("-", " ")
    text = re.sub(r"\s+", " ", text).strip()
    for pattern in _SUFFIX_PATTERNS:
        text = re.sub(pattern, "", text).strip()
        text = re.sub(r"\s+", " ", text).strip()
    tokens = [token for token in re.split(r"\s+", text) if token]
    if tokens and all(_token_is_generic(token) for token in tokens):
        return ""
    if not tokens:
        return ""
    return " ".join(tokens)


def _token_is_generic(token: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]+", "", token.lower())
    return not normalized or normalized in _GENERIC_TOKENS or normalized.startswith("t00") or normalized.startswith("a1")


def _chapter_titles(raw_ffprobe: dict) -> list[str]:
    chapters = raw_ffprobe.get("chapters") if isinstance(raw_ffprobe, dict) else None
    if not chapters:
        return []
    titles: list[str] = []
    for chapter in chapters:
        tags = chapter.get("tags") if isinstance(chapter, dict) else None
        if not isinstance(tags, dict):
            continue
        title = tags.get("title") or tags.get("TITLE")
        if isinstance(title, str) and title.strip():
            titles.append(title.strip())
    return titles
