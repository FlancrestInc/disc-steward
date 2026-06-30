from __future__ import annotations

from dataclasses import asdict

from .config import LLMConfig
from .models import ScannedFile


def build_compact_media_summary(file: ScannedFile) -> dict:
    return {
        "filename": file.filename,
        "duration_seconds": file.duration_seconds,
        "embedded_title": file.embedded_title,
        "video": asdict(file.video),
        "audio_languages": [stream.language for stream in file.audio_streams],
        "subtitle_summary": [
            {"codec": stream.codec, "language": stream.language, "forced": stream.forced, "default": stream.default}
            for stream in file.subtitle_streams
        ],
    }


def suggest_with_hermes(config: LLMConfig, packet: dict) -> dict:
    if not config.enabled:
        return {"enabled": False, "suggestions": []}
    raise NotImplementedError("Hermes/LLM integration is intentionally disabled in Phase 1")
