from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class AudioStream:
    index: int
    codec: str | None = None
    channels: int | None = None
    channel_layout: str | None = None
    language: str | None = None
    title: str | None = None
    default: bool = False
    forced: bool = False


@dataclass
class SubtitleStream:
    index: int
    codec: str | None = None
    language: str | None = None
    title: str | None = None
    default: bool = False
    forced: bool = False
    hearing_impaired: bool = False


@dataclass
class VideoInfo:
    codec: str | None = None
    profile: str | None = None
    pixel_format: str | None = None
    bit_depth: int | None = None
    width: int | None = None
    height: int | None = None
    frame_rate: str | None = None
    frame_rate_mode: str | None = None
    hdr_indicators: list[str] = field(default_factory=list)


@dataclass
class ScannedFile:
    path: str
    filename: str
    parent_disc_folder: str
    size_bytes: int
    modified_time: float
    duration_seconds: float | None
    container_format: str | None
    video: VideoInfo = field(default_factory=VideoInfo)
    audio_streams: list[AudioStream] = field(default_factory=list)
    subtitle_streams: list[SubtitleStream] = field(default_factory=list)
    chapter_count: int = 0
    embedded_title: str | None = None
    makemkv_title: str | None = None
    raw_ffprobe: dict[str, Any] = field(default_factory=dict)


@dataclass
class Classification:
    probable_main_feature: bool = False
    probable_extra: bool = False
    probable_trailer: bool = False
    probable_featurette: bool = False
    probable_deleted_scene: bool = False
    probable_menu_or_bumper: bool = False
    possible_episode: bool = False
    possible_alternate_cut: bool = False
    possible_commentary_variant: bool = False
    manual_review_required: bool = False
    needs_video_encode: bool = False
    needs_audio_fallback: bool = False
    needs_subtitle_conversion: bool = False
    needs_subtitle_generation: bool = False
    has_image_subtitles: bool = False
    has_text_subtitles: bool = False
    image_subtitle_is_default: bool = False
    missing_language_tags: bool = False
    likely_jellyfin_transcode_risk: bool = False
    confidence: float = 0.0
    reasons: list[str] = field(default_factory=list)


@dataclass
class Job:
    id: int
    disc_title: str
    disc_path: str
    status: str


@dataclass
class SourceFileRecord:
    id: int
    job_id: int
    path: str
    filename: str
    size_bytes: int
    modified_time: float
    duration_seconds: float | None


@dataclass
class ReviewDecision:
    source_file_id: int
    role: str
    content_type: str
    title: str
    year: int | None = None
    imdb_id: str | None = None
    tmdb_id: str | None = None
    tvdb_id: str | None = None
    anidb_id: str | None = None
    anilist_id: str | None = None
    mal_id: str | None = None
    original_title: str | None = None
    season: int | None = None
    episode: int | None = None
    extra_type: str | None = None
    target_library: str = "Movies"
    final_display_name: str | None = None
    encoding_profile: str = "universal_h264_aac_srt"
    subtitle_policy: str = "ocr_image_subtitles_to_srt_preserve_original"


@dataclass
class JobReviewMetadata:
    job_id: int
    title: str = ""
    original_title: str | None = None
    romanized_title: str | None = None
    translated_title: str | None = None
    language_script_hints: str | None = None
    anime_flag: bool = False
    japanese_media_flag: bool = False
    confidence: float | None = None
    manual_review_notes: str | None = None
    year: int | None = None
    content_type: str = "unknown"
    library_root: str = "Movies"
    imdb_id: str | None = None
    tmdb_id: str | None = None
    tvdb_id: str | None = None
    anidb_id: str | None = None
    anilist_id: str | None = None
    mal_id: str | None = None
    notes: str | None = None
    review_status: str = "review_needed"
    work_order_folder: str | None = None
    work_order_created_at: str | None = None
    warnings: list[str] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)


@dataclass
class FileReviewDecision:
    source_file_id: int
    include_in_work_order: bool = True
    role: str = ""
    content_type: str = "unknown"
    final_display_name: str | None = None
    final_filename: str | None = None
    original_title: str | None = None
    translated_title: str | None = None
    romanized_title: str | None = None
    imdb_id: str | None = None
    tmdb_id: str | None = None
    tvdb_id: str | None = None
    anidb_id: str | None = None
    anilist_id: str | None = None
    mal_id: str | None = None
    extra_type: str | None = None
    season_number: int | None = None
    episode_number: int | None = None
    sort_order: int | None = None
    encoding_profile: str = ""
    subtitle_policy: str = ""
    generated_final_path: str | None = None
    notes: str | None = None
    warnings: list[str] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)


@dataclass
class GeneratedPath:
    source_file_id: int
    final_path: Path
    output_name: str
    warnings: list[str] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)


@dataclass
class SubtitlePolicySuggestion:
    policy: str
    warnings: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)


@dataclass
class SubtitlePlan:
    policy: str
    preferred_format: str = "srt"
    preserve_original_subtitles: bool = True
    image_subtitles_detected: bool = False
    image_subtitles_default: bool = False
    text_subtitles_detected: bool = False
    ass_subtitles_detected: bool = False
    forced_subtitle_candidates: list[dict[str, Any]] = field(default_factory=list)
    statuses: list[str] = field(default_factory=list)
    actions: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    japanese_or_anime: bool = False
    generated_subtitles_unverified: bool = False


@dataclass
class ValidationResult:
    passed: bool
    issues: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


@dataclass
class OutputValidationItem:
    source_file_id: int
    expected_output_name: str
    expected_final_path: str
    profile: str
    subtitle_policy: str
    matched_output_path: str | None = None
    status: str = "pending"
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    ffprobe_summary: dict[str, Any] = field(default_factory=dict)
    detected_streams: dict[str, Any] = field(default_factory=dict)
    profile_compliance: dict[str, str] = field(default_factory=dict)
    manually_accepted: bool = False
    manual_acceptance_note: str | None = None


@dataclass
class JobValidationSummary:
    job_id: int
    status: str
    passed: bool
    items: list[OutputValidationItem] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


@dataclass
class TransferConflict:
    conflict: bool
    path: Path
    reason: str | None = None


@dataclass
class TransferItemResult:
    source_file_id: int
    source_output_path: str
    incoming_path: str
    final_path: str
    status: str = "pending"
    verification: str = "size"
    conflict: str | None = None
    error: str | None = None


@dataclass
class TransferSummary:
    job_id: int
    status: str
    items: list[TransferItemResult] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


@dataclass
class CleanupEligibilityItem:
    job_id: int
    path: str
    item_type: str
    eligible: bool
    reason: str
    archive_path: str | None = None


@dataclass
class CleanupPlanSummary:
    eligible: list[CleanupEligibilityItem] = field(default_factory=list)
    ineligible: list[CleanupEligibilityItem] = field(default_factory=list)
    dry_run: bool = True
    deleted: list[str] = field(default_factory=list)
    archived: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
