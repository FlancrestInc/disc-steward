from __future__ import annotations

from collections import defaultdict

from .models import Classification, ScannedFile

IMAGE_SUBTITLE_CODECS = {"hdmv_pgs_subtitle", "dvd_subtitle", "dvb_subtitle", "xsub"}
TEXT_SUBTITLE_CODECS = {"subrip", "srt", "ass", "ssa", "webvtt", "mov_text"}


def classify_disc_files(files: list[ScannedFile]) -> dict[str, Classification]:
    durations = [f.duration_seconds or 0 for f in files]
    longest = max(durations, default=0)
    near_longest = [f for f in files if longest and abs((f.duration_seconds or 0) - longest) < 180]
    by_duration_bucket: dict[int, list[ScannedFile]] = defaultdict(list)
    for file in files:
        by_duration_bucket[int((file.duration_seconds or 0) // 300)].append(file)

    results: dict[str, Classification] = {}
    for file in files:
        duration = file.duration_seconds or 0
        result = Classification()
        if duration >= 3600 and duration == longest:
            result.probable_main_feature = True
            result.confidence = 0.78
            result.reasons.append("longest title over 60 minutes")
        elif duration >= 3600:
            result.probable_extra = True
            result.manual_review_required = True
            result.reasons.append("long non-main title may be alternate cut, bonus film, or episode block")
        if len(near_longest) > 1 and duration >= 3600:
            result.possible_alternate_cut = True
            result.manual_review_required = True
            result.reasons.append("multiple similar-duration long titles")
        if 45 <= duration < 240:
            result.probable_trailer = True
            result.probable_extra = True
            result.confidence = max(result.confidence, 0.45)
            result.reasons.append("45 seconds to 4 minutes")
        elif 240 <= duration < 900:
            result.probable_featurette = True
            result.probable_extra = True
            result.confidence = max(result.confidence, 0.5)
            result.reasons.append("4 to 15 minutes")
        elif 900 <= duration < 3600:
            result.probable_featurette = True
            result.probable_extra = True
            result.manual_review_required = True
            result.reasons.append("15 to 60 minutes")
        elif 0 < duration < 45:
            result.probable_menu_or_bumper = True
            result.manual_review_required = True
            result.reasons.append("very short title")
        if len(by_duration_bucket[int(duration // 300)]) >= 3 and 900 <= duration <= 3900:
            result.possible_episode = True
            result.manual_review_required = True
            result.reasons.append("several titles in a similar episode-length bucket")
        title_text = " ".join([file.filename, file.embedded_title or "", file.makemkv_title or ""]).lower()
        if "commentary" in title_text:
            result.possible_commentary_variant = True
            result.manual_review_required = True
            result.reasons.append("commentary marker in title or filename")
        _classify_stream_risks(file, result)
        if result.image_subtitle_is_default:
            result.manual_review_required = True
            result.reasons.append("default image subtitle can trigger Jellyfin burn-in/transcoding")
        if result.missing_language_tags:
            result.manual_review_required = True
            result.reasons.append("one or more audio/subtitle streams lack language tags")
        results[file.path] = result
    return results


def _classify_stream_risks(file: ScannedFile, result: Classification) -> None:
    video = file.video
    if video.codec != "h264" or video.profile not in {None, "High"} or video.bit_depth not in {None, 8} or video.pixel_format not in {None, "yuv420p"}:
        result.needs_video_encode = True
        result.likely_jellyfin_transcode_risk = True
        result.reasons.append("video is outside broad H.264 High 8-bit yuv420p target")
    if video.frame_rate_mode == "variable_or_unknown":
        result.needs_video_encode = True
        result.reasons.append("frame rate may be variable")
    has_aac = any(stream.codec == "aac" for stream in file.audio_streams)
    if file.audio_streams and not has_aac:
        result.needs_audio_fallback = True
        result.likely_jellyfin_transcode_risk = True
        result.reasons.append("AAC fallback audio is missing")
    for stream in file.subtitle_streams:
        if stream.codec in IMAGE_SUBTITLE_CODECS:
            result.has_image_subtitles = True
            result.needs_subtitle_conversion = True
            result.likely_jellyfin_transcode_risk = True
            if stream.default:
                result.image_subtitle_is_default = True
        if stream.codec in TEXT_SUBTITLE_CODECS:
            result.has_text_subtitles = True
    streams = [*file.audio_streams, *file.subtitle_streams]
    result.missing_language_tags = any(stream.language is None for stream in streams)
