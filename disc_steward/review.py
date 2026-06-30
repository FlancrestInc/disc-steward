from __future__ import annotations

import json
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Iterable

from .models import Classification, FileReviewDecision, GeneratedPath, JobReviewMetadata, SubtitlePolicySuggestion


REVIEW_STATUSES = {
    "review_needed",
    "review_in_progress",
    "reviewed",
    "ready_for_fileflows",
    "fileflows_work_orders_created",
    "manual_review",
}


class ReviewValidationError(ValueError):
    def __init__(self, messages: list[str]):
        super().__init__("; ".join(messages))
        self.messages = messages


def _is_non_english_audio(audio_languages: Iterable[str | None]) -> bool:
    languages = {language for language in audio_languages if language}
    return bool(languages) and "eng" not in languages


def suggest_subtitle_policy(
    classification: Classification,
    audio_languages: list[str | None],
    subtitle_codecs: list[str | None],
) -> SubtitlePolicySuggestion:
    codecs = {codec for codec in subtitle_codecs if codec}
    warnings: list[str] = []
    reasons: list[str] = []
    if classification.has_image_subtitles or codecs.intersection({"hdmv_pgs_subtitle", "dvd_subtitle", "dvb_subtitle", "xsub"}):
        reasons.append("image subtitles detected")
        if classification.image_subtitle_is_default:
            warnings.append("default image subtitle may force Jellyfin subtitle burn-in/transcoding")
        return SubtitlePolicySuggestion("ocr_image_subtitles_to_srt_preserve_original", warnings, reasons)
    if codecs.intersection({"ass", "ssa"}):
        return SubtitlePolicySuggestion(
            "preserve_ass_add_srt_fallback",
            warnings,
            ["ASS/SSA subtitles detected; preserve styling and add SRT fallback when practical"],
        )
    if not classification.has_text_subtitles and _is_non_english_audio(audio_languages):
        return SubtitlePolicySuggestion(
            "generate_missing_srt_unverified",
            ["non-English audio has no detected text subtitles; manual review recommended"],
            ["missing text subtitles for non-English audio"],
        )
    if codecs.intersection({"subrip", "srt", "webvtt", "mov_text"}):
        return SubtitlePolicySuggestion("preserve_existing", warnings, ["existing text subtitles detected"])
    return SubtitlePolicySuggestion("preserve_existing", warnings, ["no subtitle conversion risk detected"])


def validate_review_ready(
    job_review: JobReviewMetadata,
    decisions: list[FileReviewDecision],
    generated_paths: dict[int, GeneratedPath],
) -> None:
    if job_review.review_status == "manual_review":
        return
    messages: list[str] = []
    included = [decision for decision in decisions if decision.include_in_work_order]
    if not any(decision.role for decision in included):
        messages.append("at least one included file must have a role")
    content_type = job_review.content_type
    if content_type in {"movie", "show"}:
        if not job_review.title:
            messages.append("title is required for movie/show jobs")
        if job_review.year is None:
            messages.append("year is required for movie/show jobs")
    if content_type == "movie" and not any(decision.role == "main_feature" for decision in included):
        messages.append("movie jobs require an included main feature")
    if any(not decision.encoding_profile for decision in included):
        messages.append("included files require an encoding profile")
    if any(not decision.subtitle_policy for decision in included):
        messages.append("included files require a subtitle policy")
    path_conflicts = [
        conflict
        for decision in included
        for conflict in generated_paths.get(decision.source_file_id, GeneratedPath(decision.source_file_id, Path(""), "")).conflicts
    ]
    if path_conflicts:
        messages.append("final paths must be generated without conflicts")
    if messages:
        raise ReviewValidationError(messages)


def serve_static_reports(directory: Path, host: str = "127.0.0.1", port: int = 8765) -> None:
    class Handler(SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(directory), **kwargs)

    ThreadingHTTPServer((host, port), Handler).serve_forever()


def classification_from_json(value: str | None) -> Classification:
    if not value:
        return Classification()
    data = json.loads(value)
    return Classification(**data)
