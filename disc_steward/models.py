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
class ValidationResult:
    passed: bool
    issues: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


@dataclass
class TransferConflict:
    conflict: bool
    path: Path
    reason: str | None = None
