from __future__ import annotations

from dataclasses import asdict

from .models import ScannedFile, SubtitlePlan, SubtitleStream, ValidationResult
from .scanner import IMAGE_SUBTITLE_CODECS


TEXT_SUBTITLE_CODECS = {"subrip", "srt", "webvtt", "mov_text"}
ASS_SUBTITLE_CODECS = {"ass", "ssa"}
SUPPORTED_SUBTITLE_CODECS = TEXT_SUBTITLE_CODECS | ASS_SUBTITLE_CODECS | IMAGE_SUBTITLE_CODECS
JAPANESE_LANGUAGE_CODES = {"ja", "jpn", "jp", "japanese"}


def generate_subtitle_plan(
    source: ScannedFile,
    content_type: str,
    subtitle_policy: str,
    preferred_format: str = "srt",
    preserve_original_subtitles: bool = True,
) -> SubtitlePlan:
    subtitles = source.subtitle_streams
    audio_languages = {_norm(stream.language) for stream in source.audio_streams if stream.language}
    japanese_or_anime = content_type == "anime" or bool(audio_languages & JAPANESE_LANGUAGE_CODES)
    image_subtitles = [stream for stream in subtitles if _codec(stream) in IMAGE_SUBTITLE_CODECS]
    text_subtitles = [stream for stream in subtitles if _codec(stream) in TEXT_SUBTITLE_CODECS]
    ass_subtitles = [stream for stream in subtitles if _codec(stream) in ASS_SUBTITLE_CODECS]
    forced = [stream for stream in subtitles if stream.forced or "forced" in (stream.title or "").lower()]
    statuses: list[str] = []
    actions: list[dict] = []
    warnings: list[str] = []

    plan = SubtitlePlan(
        policy=subtitle_policy,
        preferred_format=preferred_format,
        preserve_original_subtitles=preserve_original_subtitles,
        image_subtitles_detected=bool(image_subtitles),
        image_subtitles_default=any(stream.default for stream in image_subtitles),
        text_subtitles_detected=bool(text_subtitles),
        ass_subtitles_detected=bool(ass_subtitles),
        forced_subtitle_candidates=[_stream_candidate(stream) for stream in forced],
        japanese_or_anime=japanese_or_anime,
    )

    for stream in subtitles:
        if stream.language is None:
            _add_once(statuses, "needs_language_tag_cleanup")
            actions.append({"type": "set_language", "source_stream_index": stream.index, "language": "und", "reason": "subtitle language is missing"})
        if _codec(stream) not in SUPPORTED_SUBTITLE_CODECS:
            _add_once(statuses, "unsupported")
            warnings.append(f"Unsupported subtitle codec: {stream.codec or 'unknown'}")

    if plan.image_subtitles_default:
        _add_once(statuses, "needs_default_flag_cleanup")
        for stream in image_subtitles:
            if stream.default:
                actions.append({"type": "unset_default", "source_stream_index": stream.index, "reason": "image subtitle should not be default"})
                warnings.append(f"{stream.codec or 'image'} subtitle is currently default and may cause Jellyfin burn-in transcoding")

    if forced:
        _add_once(statuses, "needs_forced_subtitle_review")
        for stream in forced:
            if not stream.forced:
                actions.append({"type": "mark_forced", "source_stream_index": stream.index, "reason": "title suggests forced subtitles"})

    if image_subtitles and not text_subtitles and "ocr_image_subtitles_to_srt" in subtitle_policy:
        _add_once(statuses, "needs_ocr_to_srt")
        for stream in image_subtitles:
            actions.append(
                {
                    "type": "ocr_to_srt",
                    "source_stream_index": stream.index,
                    "language": stream.language or "und",
                    "mark_default": not stream.forced and not text_subtitles,
                    "mark_forced": stream.forced,
                    "preserve_original": preserve_original_subtitles,
                    "generated_unverified": True,
                }
            )
        plan.generated_subtitles_unverified = True

    if ass_subtitles and (content_type == "anime" or "preserve_ass" in subtitle_policy):
        warnings.append("ASS subtitles may include important styling/sign placement. Preserve original ASS and add SRT fallback.")
        if "add_srt_fallback" in subtitle_policy:
            _add_once(statuses, "needs_ass_srt_fallback")
            for stream in ass_subtitles:
                actions.append(
                    {
                        "type": "ass_to_srt_fallback",
                        "source_stream_index": stream.index,
                        "language": stream.language or "und",
                        "preserve_original": True,
                        "generated_unverified": True,
                    }
                )
            plan.generated_subtitles_unverified = True

    if not subtitles and (japanese_or_anime or "generate_missing_srt" in subtitle_policy):
        _add_once(statuses, "needs_missing_subtitle_generation")
        actions.append({"type": "generate_missing_srt", "language": "eng", "generated_unverified": True, "reason": "no subtitle streams detected"})
        plan.generated_subtitles_unverified = True
        warnings.append("No subtitles detected for Japanese/anime or configured missing-subtitle generation; generated subtitles require review.")

    if japanese_or_anime:
        warnings.append("Japanese/anime content detected. Review title and subtitle handling.")

    plan.statuses = _ordered_statuses(statuses) or ["no_action_needed"]
    plan.actions = actions
    plan.warnings = _dedupe(warnings)
    if "unsupported" in plan.statuses:
        _add_once(plan.statuses, "manual_review_required")
    return plan


def plan_to_dict(plan: SubtitlePlan) -> dict:
    return asdict(plan)


def plan_from_dict(data: dict) -> SubtitlePlan:
    return SubtitlePlan(**data)


def validate_subtitle_plan_result(plan: SubtitlePlan | dict, parsed: ScannedFile) -> ValidationResult:
    if isinstance(plan, dict):
        plan = plan_from_dict(plan)
    issues: list[str] = []
    warnings: list[str] = []
    parsed_subtitles = parsed.subtitle_streams
    codecs = {_codec(stream) for stream in parsed_subtitles}
    has_srt = bool(codecs & {"subrip", "srt"})
    has_ass = bool(codecs & ASS_SUBTITLE_CODECS)

    if any(action.get("type") in {"ocr_to_srt", "ass_to_srt_fallback", "generate_missing_srt"} for action in plan.actions) and not has_srt:
        warnings.append("required SRT is expected but was not detected; confirm sidecar or downstream processing output manually")
    if any(_codec(stream) in IMAGE_SUBTITLE_CODECS and stream.default for stream in parsed_subtitles):
        issues.append("image subtitle is marked default")
    for action in plan.actions:
        if action.get("type") == "mark_forced":
            source_index = action.get("source_stream_index")
            matching = [stream for stream in parsed_subtitles if stream.index == source_index]
            if matching and not matching[0].forced:
                warnings.append(f"forced subtitle candidate stream {source_index} is not marked forced")
    if "needs_language_tag_cleanup" in plan.statuses and any(stream.language is None for stream in parsed_subtitles):
        warnings.append("subtitle language tags are still missing")
    if plan.preserve_original_subtitles:
        original_actions = [action for action in plan.actions if action.get("preserve_original")]
        if original_actions and not parsed_subtitles:
            warnings.append("original subtitle preservation could not be confirmed")
    if plan.ass_subtitles_detected and not has_ass:
        warnings.append("ASS subtitles were expected to be preserved but were not detected")
    return ValidationResult(not issues, issues, warnings)


def _stream_candidate(stream: SubtitleStream) -> dict:
    return {"source_stream_index": stream.index, "language": stream.language, "title": stream.title, "forced": stream.forced}


def _codec(stream: SubtitleStream) -> str:
    return (stream.codec or "").lower()


def _norm(value: str | None) -> str:
    return (value or "").strip().lower()


def _add_once(values: list[str], value: str) -> None:
    if value not in values:
        values.append(value)


def _dedupe(values: list[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        if value not in result:
            result.append(value)
    return result


def _ordered_statuses(values: list[str]) -> list[str]:
    priority = [
        "needs_ocr_to_srt",
        "needs_ass_srt_fallback",
        "needs_missing_subtitle_generation",
        "needs_forced_subtitle_review",
        "needs_language_tag_cleanup",
        "needs_default_flag_cleanup",
        "manual_review_required",
        "unsupported",
    ]
    ordered = [value for value in priority if value in values]
    ordered.extend(value for value in values if value not in ordered)
    return ordered
