from __future__ import annotations

import json
from dataclasses import asdict
from urllib.request import Request, urlopen

from .config import LLMConfig
from .models import AudioStream, ScannedFile, SubtitleStream, VideoInfo


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


def build_disc_job_packet(db, config, job_id: int) -> dict:
    job = db.get_job(job_id)
    review = db.get_job_review(job_id)
    rows = db.source_file_payloads(job_id)[: config.llm.max_items_per_request]
    return {
        "job_id": job_id,
        "disc_title": _truncate(job.disc_title if job else "", config.llm.max_chars_per_field),
        "review": {
            "title": _truncate(review.title, config.llm.max_chars_per_field),
            "original_title": _truncate(review.original_title, config.llm.max_chars_per_field),
            "romanized_title": _truncate(review.romanized_title, config.llm.max_chars_per_field),
            "translated_title": _truncate(review.translated_title, config.llm.max_chars_per_field),
            "content_type": review.content_type,
            "library_root": review.library_root,
            "warnings": [_truncate(value, config.llm.max_chars_per_field) for value in review.warnings],
        },
        "limits": {
            "max_items_per_request": config.llm.max_items_per_request,
            "max_chars_per_field": config.llm.max_chars_per_field,
            "allow_full_subtitle_text": config.llm.allow_full_subtitle_text,
            "allow_shell_commands": False,
        },
        "files": [_compact_row(row, config.llm) for row in rows],
        "allowed_suggestion_types": [
            "metadata_match_suggestion",
            "japanese_title_interpretation",
            "extras_name_suggestion",
            "subtitle_policy_suggestion",
            "failure_summary",
            "manual_review_priority",
        ],
        "warnings": ["Suggestions are advisory only and must be approved by a user before application."],
    }


def request_suggestions(db, config, job_id: int, sender=None) -> dict:
    if not config.llm.enabled:
        return {"enabled": False, "suggestions": []}
    packet = build_disc_job_packet(db, config, job_id)
    if not config.llm.endpoint:
        response = {"enabled": True, "suggestions": [], "warnings": ["llm.endpoint is not configured"]}
    else:
        send = sender or _post_json
        response = send(config.llm.endpoint, packet)
        response.setdefault("enabled", True)
        response.setdefault("suggestions", [])
    db.save_llm_request_response(job_id, config.llm.provider, packet, response)
    return response


def suggest_with_hermes(config: LLMConfig, packet: dict) -> dict:
    if not config.enabled:
        return {"enabled": False, "suggestions": []}
    if not config.endpoint:
        return {"enabled": True, "suggestions": [], "warnings": ["llm.endpoint is not configured"]}
    return _post_json(config.endpoint, packet)


def _compact_row(row: dict, llm: LLMConfig) -> dict:
    video = VideoInfo(**json.loads(row["video_json"] or "{}"))
    audio = [AudioStream(**stream) for stream in json.loads(row["audio_json"] or "[]")]
    subtitles = [SubtitleStream(**stream) for stream in json.loads(row["subtitle_json"] or "[]")]
    classification = json.loads(row.get("classification_json") or "{}")
    return {
        "source_file_id": row["id"],
        "filename": _truncate(row["filename"], llm.max_chars_per_field),
        "duration_seconds": row["duration_seconds"],
        "embedded_title": _truncate(row.get("embedded_title"), llm.max_chars_per_field),
        "video": {
            "codec": video.codec,
            "profile": _truncate(video.profile, llm.max_chars_per_field),
            "width": video.width,
            "height": video.height,
            "hdr_indicators": [_truncate(value, llm.max_chars_per_field) for value in video.hdr_indicators],
        },
        "audio_languages": [_truncate(stream.language, llm.max_chars_per_field) for stream in audio],
        "subtitle_summary": [
            {
                "codec": stream.codec,
                "language": stream.language,
                "forced": stream.forced,
                "default": stream.default,
                "title": _truncate(stream.title, llm.max_chars_per_field),
            }
            for stream in subtitles
        ],
        "classification": {
            key: classification.get(key)
            for key in [
                "probable_main_feature",
                "probable_extra",
                "possible_episode",
                "manual_review_required",
                "confidence",
                "reasons",
            ]
            if key in classification
        },
    }


def _truncate(value: object, limit: int) -> object:
    if value is None or not isinstance(value, str):
        return value
    if len(value) <= limit:
        return value
    return value[: max(0, limit - 3)] + "..."


def _post_json(endpoint: str, packet: dict) -> dict:
    data = json.dumps(packet, ensure_ascii=False).encode("utf-8")
    request = Request(endpoint, data=data, headers={"Content-Type": "application/json"}, method="POST")
    with urlopen(request, timeout=30) as response:  # noqa: S310 - endpoint is user-configured and disabled by default
        return json.loads(response.read().decode("utf-8") or "{}")
