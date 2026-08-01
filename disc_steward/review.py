from __future__ import annotations

import json
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Iterable

from .models import Classification, FileReviewDecision, GeneratedPath, JobReviewMetadata, ScannedFile, SubtitlePolicySuggestion


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


BONUS_DISC_ROLES = {
    "extra",
    "trailer",
    "featurette",
    "deleted_scene",
    "interview",
    "music_video",
    "short_film",
    "promo",
    "alternate_cut",
    "commentary_variant",
    "menu_or_bumper",
}


def seed_automatic_review(
    db,
    config,
    job_id: int,
    scanned_files: list[ScannedFile],
    classifications: dict[str, Classification],
) -> dict[str, list[str]]:
    """Populate safe, reversible review defaults for a newly scanned job.

    This does not mark a job reviewed or create work orders, and it never
    overwrites a value already saved by the operator.
    """
    applied: dict[str, list[str]] = {}
    saved = {decision.source_file_id: decision for decision in db.list_file_reviews(job_id)}
    source_ids = {row["path"]: int(row["id"]) for row in db.source_file_payloads(job_id)}

    for scanned in scanned_files:
        source_file_id = source_ids.get(scanned.path)
        if source_file_id is None:
            continue
        decision = saved.get(source_file_id) or FileReviewDecision(source_file_id=source_file_id)
        classification = classifications[scanned.path]
        fields: list[str] = []
        role = _automatic_role(scanned, classification)
        content_type = "movie" if role == "main_feature" else "extra" if role else "unknown"
        display_name = _automatic_display_name(scanned, role)
        subtitle_policy = suggest_subtitle_policy(
            classification,
            [stream.language for stream in scanned.audio_streams],
            [stream.codec for stream in scanned.subtitle_streams],
        ).policy

        def set_if_blank(field_name: str, value: object) -> None:
            if value in {None, ""}:
                return
            if getattr(decision, field_name) in {None, "", "unknown"}:
                setattr(decision, field_name, value)
                fields.append(field_name)

        set_if_blank("role", role)
        set_if_blank("content_type", content_type)
        set_if_blank("final_display_name", display_name)
        set_if_blank("encoding_profile", config.preferred_video_profile)
        set_if_blank("subtitle_policy", subtitle_policy)
        if classification.reasons and not decision.notes:
            decision.notes = "Automatic scan: " + "; ".join(classification.reasons)
            fields.append("notes")
        if fields:
            db.save_file_review(decision)
            applied[f"file:{source_file_id}"] = fields

    if applied:
        db.audit(
            "automatic_review_seed",
            f"Seeded automatic review defaults for {len(applied)} file(s)",
            job_id,
            {"applied_fields": applied},
        )
    return applied


def _automatic_role(scanned: ScannedFile, classification: Classification) -> str:
    title = " ".join([scanned.filename, scanned.embedded_title or "", scanned.makemkv_title or ""]).lower()
    if classification.probable_main_feature and not classification.possible_alternate_cut:
        return "main_feature"
    if classification.probable_trailer or any(token in title for token in ("trailer", "teaser", "promo")):
        return "trailer"
    if classification.possible_commentary_variant or "commentary" in title:
        return "commentary_variant"
    if classification.probable_menu_or_bumper:
        return "menu_or_bumper"
    if classification.probable_featurette:
        return "featurette"
    if classification.probable_extra:
        return "extra"
    return ""


def _automatic_display_name(scanned: ScannedFile, role: str) -> str:
    for value in (scanned.embedded_title, scanned.makemkv_title):
        if value and value.strip():
            return value.strip()
    stem = scanned.filename.rsplit(".", 1)[0].replace("_", " ").replace(".", " ").replace("-", " ").strip()
    if stem.lower() in {"title", "title t00", "title_t00", "video"}:
        return {"trailer": "Trailer", "featurette": "Featurette", "menu_or_bumper": "Menu / bumper"}.get(role, "")
    return stem


def validate_review_ready(
    job_review: JobReviewMetadata,
    decisions: list[FileReviewDecision],
    generated_paths: dict[int, GeneratedPath],
    *,
    job_kind: str = "standard",
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
    if content_type == "movie" and job_kind != "bonus_disc" and not any(decision.role == "main_feature" for decision in included):
        messages.append("movie jobs require an included main feature")
    if job_kind == "bonus_disc" and any(decision.role not in BONUS_DISC_ROLES for decision in included):
        messages.append("bonus-disc files must use an extras-compatible role")
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
