from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Callable

from .config import AppConfig
from .models import AudioStream, JobValidationSummary, OutputValidationItem, ScannedFile, SubtitleStream, ValidationResult, VideoInfo
from .scanner import IMAGE_SUBTITLE_CODECS, parse_ffprobe, run_ffprobe
from .subtitle_planner import validate_subtitle_plan_result
from .notifications import send_notification


VIDEO_OPTIONAL_ROLES = {"audio_only", "subtitle_only"}
TEXT_SUBTITLE_CODECS = {"subrip", "srt", "ass", "ssa", "webvtt", "mov_text"}


def validate_output(
    source: ScannedFile,
    output_path: Path,
    final_path: Path,
    ffprobe_runner: Callable[[Path], str],
    duration_tolerance_seconds: int = 5,
    require_aac_fallback: bool = True,
) -> ValidationResult:
    issues: list[str] = []
    warnings: list[str] = []
    if not output_path.exists():
        return ValidationResult(False, [f"output file does not exist: {output_path}"], warnings)
    if not output_path.is_file():
        issues.append(f"output path is not a file: {output_path}")
    try:
        parsed = parse_ffprobe(ffprobe_runner(output_path), output_path)
    except Exception as exc:  # pragma: no cover - depends on ffprobe failures
        return ValidationResult(False, [f"ffprobe failed: {exc}"], warnings)
    if source.duration_seconds and parsed.duration_seconds:
        delta = abs(source.duration_seconds - parsed.duration_seconds)
        if delta > duration_tolerance_seconds:
            issues.append(f"duration differs by {delta:.1f}s")
    if parsed.video.codec != "h264":
        issues.append("video codec does not match universal H.264 target")
    if require_aac_fallback and not any(stream.codec == "aac" for stream in parsed.audio_streams):
        issues.append("AAC fallback audio is missing")
    for stream in parsed.subtitle_streams:
        if stream.codec in {"hdmv_pgs_subtitle", "dvd_subtitle", "dvb_subtitle"} and stream.default:
            issues.append("image subtitle is marked default")
        if stream.language is None:
            warnings.append("subtitle language tag is missing")
    if not final_path.name.endswith(".mkv"):
        issues.append("final filename must end with .mkv")
    if source.size_bytes and output_path.stat().st_size < max(1, source.size_bytes * 0.02):
        warnings.append("output is suspiciously tiny compared to source")
    return ValidationResult(not issues, issues, warnings)


def validate_job_outputs(
    db,
    config: AppConfig,
    job_id: int,
    ffprobe_runner: Callable[[Path], str] | None = None,
) -> JobValidationSummary:
    work_orders = db.list_work_order_payloads(job_id)
    if not work_orders:
        raise ValueError(f"No processing outputs were recorded for job {job_id}")
    runner = ffprobe_runner or (lambda path: run_ffprobe(config.ffprobe_path, path))
    source_rows = {row["id"]: row for row in db.source_file_payloads(job_id)}
    items: list[OutputValidationItem] = []
    matched_paths: set[str] = set()
    warnings: list[str] = []

    for payload in work_orders:
        source_id = int(payload.get("item_id") or payload.get("_source_file_id"))
        item = OutputValidationItem(
            source_file_id=source_id,
            expected_output_name=payload.get("output_name") or Path(payload.get("final_library_path", "")).name,
            expected_final_path=payload.get("final_library_path") or "",
            profile=payload.get("profile") or "",
            subtitle_policy=payload.get("subtitle_policy") or "",
            subtitle_outputs=payload.get("subtitle_outputs") or [],
        )
        source = _source_from_row(source_rows[source_id])
        output_dir = _controller_validation_output_dir(config, payload, job_id)
        output_path, match_warnings = _match_output(output_dir, item.expected_output_name, source_id, payload)
        item.warnings.extend(match_warnings)
        if output_path is None:
            item.status = "failed"
            item.errors.append(f"missing output for {item.expected_output_name}")
            items.append(item)
            continue
        item.matched_output_path = str(output_path)
        if str(output_path) in matched_paths:
            item.errors.append("output conflicts with another item in this job")
        matched_paths.add(str(output_path))
        _validate_matched_output(config, item, source, output_path, runner, payload.get("subtitle_outputs") or [])
        item.status = "passed" if not item.errors else "failed"
        items.append(item)

    for output_dir in {_controller_validation_output_dir(config, payload, job_id) for payload in work_orders}:
        for extra in sorted(output_dir.glob("*.mkv")) if output_dir.exists() else []:
            if str(extra) not in matched_paths:
                warnings.append(f"unmatched processing output: {extra}")

    passed = all(item.status == "passed" or item.manually_accepted for item in items)
    status = "validated" if passed else "validation_failed"
    summary = JobValidationSummary(job_id=job_id, status=status, passed=passed, items=items, warnings=warnings)
    db.save_validation_summary(job_id, _summary_dict(summary), passed)
    db.update_job_status(job_id, "transfer_ready" if passed else "validation_failed")
    db.audit(
        "validation_passed" if passed else "validation_failed",
        f"Validation {'passed' if passed else 'failed'} for {len(items)} output(s)",
        job_id,
        {"warnings": warnings},
    )
    send_notification(
        config,
        f"Validation {'passed' if passed else 'failed'}: job {job_id}",
        f"Validation {'passed' if passed else 'failed'} for job {job_id} with {len(items)} item(s).",
        priority="default" if passed else "high",
        tags=["validation", "success"] if passed else ["validation", "warning"],
    )
    return summary


def _controller_validation_output_dir(config: AppConfig, payload: dict, job_id: int) -> Path:
    value = payload.get("barnabas_validation_output_dir")
    if value:
        return config.to_controller_path(Path(value), "barnabas")
    return config.validation_needed_path / f"job_{job_id}"


def _match_output(output_dir: Path, expected_name: str, source_file_id: int, payload: dict) -> tuple[Path | None, list[str]]:
    warnings: list[str] = []
    expected = output_dir / expected_name
    if expected.exists():
        return expected, warnings
    if output_dir.exists():
        for sidecar in sorted(output_dir.glob("*.json")):
            try:
                data = json.loads(sidecar.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            references = {
                data.get("item_id"),
                data.get("source_file_id"),
                data.get("disc_steward_item_id"),
                data.get("job_item_id"),
            }
            if source_file_id in references or str(source_file_id) in references:
                candidate = sidecar.with_suffix(".mkv")
                if candidate.exists():
                    warnings.append(f"output filename differs from expected: {candidate.name} != {expected_name}")
                    return candidate, warnings
    candidates = sorted(output_dir.glob("*.mkv")) if output_dir.exists() else []
    if len(candidates) == 1:
        warnings.append(f"output filename differs from expected: {candidates[0].name} != {expected_name}")
        return candidates[0], warnings
    source_path = payload.get("source_path")
    if source_path:
        source_stem = Path(source_path).stem
        for candidate in candidates:
            if source_stem and source_stem in candidate.stem:
                warnings.append(f"output filename differs from expected: {candidate.name} != {expected_name}")
                return candidate, warnings
    return None, warnings


def _validate_subtitle_sidecars(item: OutputValidationItem, output_dir: Path, subtitle_outputs: list[dict]) -> None:
    for subtitle in subtitle_outputs:
        output_name = subtitle.get("output_name")
        if not output_name:
            item.errors.append("subtitle sidecar output name is missing")
            continue
        output_path = output_dir / output_name
        if not output_path.exists():
            item.errors.append(f"missing subtitle sidecar: {output_name}")
            continue
        if output_path.stat().st_size <= 0:
            item.errors.append(f"subtitle sidecar is empty: {output_name}")
            continue
        try:
            contents = output_path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            item.errors.append(f"subtitle sidecar is not valid UTF-8 text: {output_name}")
            continue
        if not contents.strip():
            item.errors.append(f"subtitle sidecar is empty text: {output_name}")


def _validate_matched_output(
    config: AppConfig,
    item: OutputValidationItem,
    source: ScannedFile,
    output_path: Path,
    ffprobe_runner: Callable[[Path], str],
    subtitle_outputs: list[dict] | None = None,
) -> None:
    if not output_path.is_file():
        item.errors.append(f"output path is not a file: {output_path}")
        return
    if output_path.stat().st_size <= 0:
        item.errors.append("output file is empty")
    if source.size_bytes and output_path.stat().st_size < max(1, source.size_bytes * 0.02):
        item.errors.append("output is suspiciously tiny compared to source")
    try:
        parsed = parse_ffprobe(ffprobe_runner(output_path), output_path)
    except Exception as exc:  # pragma: no cover - ffprobe runner details vary
        item.errors.append(f"ffprobe failed: {exc}")
        return
    item.ffprobe_summary = {
        "container_format": parsed.container_format,
        "duration_seconds": parsed.duration_seconds,
        "size_bytes": parsed.size_bytes,
        "chapter_count": parsed.chapter_count,
    }
    item.detected_streams = {
        "video": parsed.video.__dict__,
        "audio": [stream.__dict__ for stream in parsed.audio_streams],
        "subtitles": [stream.__dict__ for stream in parsed.subtitle_streams],
    }
    _validate_duration(config, source, parsed, item)
    if subtitle_outputs:
        _validate_subtitle_sidecars(item, output_path.parent, subtitle_outputs)
    if not item.expected_final_path:
        item.errors.append("generated final library path is missing")
    role_allows_no_video = item.profile in VIDEO_OPTIONAL_ROLES
    if not role_allows_no_video and not parsed.video.codec:
        item.errors.append("output has no video stream")
    if not parsed.audio_streams:
        item.errors.append("output has no audio stream")
    _validate_profile(config, source, parsed, item)


def _validate_duration(config: AppConfig, source: ScannedFile, parsed: ScannedFile, item: OutputValidationItem) -> None:
    if not source.duration_seconds or not parsed.duration_seconds:
        item.warnings.append("duration comparison unavailable")
        return
    delta = abs(source.duration_seconds - parsed.duration_seconds)
    percent = delta / source.duration_seconds * 100
    if delta > config.duration_tolerance_seconds and percent > config.duration_tolerance_percent:
        item.errors.append(f"duration differs by {delta:.1f}s ({percent:.2f}%)")


def _validate_profile(config: AppConfig, source: ScannedFile, parsed: ScannedFile, item: OutputValidationItem) -> None:
    profile = item.profile or "universal_h264_aac_srt"
    if profile == "universal_h264_aac_srt":
        _expect_container(parsed, item, {"matroska", "matroska,webm", "mkv"})
        _expect_video(parsed, item, {"h264"}, "video_codec")
        _expect_pixel_format(parsed, item, {"yuv420p"})
        _expect_bit_depth(parsed, item, {8})
        _expect_aac(config, parsed, item)
        _expect_no_default_image_subtitles(config, parsed, item)
        item.profile_compliance["subtitles"] = "external_srt"
        return
    if profile == "remux_only":
        item.profile_compliance["video_codec"] = "not_enforced"
        _expect_aac(config, parsed, item)
        _expect_no_default_image_subtitles(config, parsed, item)
        item.profile_compliance["subtitles"] = "external_srt"
        return
    if profile == "subtitle_fix_only":
        if source.video.codec and parsed.video.codec != source.video.codec:
            item.errors.append("video codec changed during subtitle_fix_only")
            item.profile_compliance["video_codec"] = "fail"
        else:
            item.profile_compliance["video_codec"] = "pass"
        _expect_no_default_image_subtitles(config, parsed, item)
        item.profile_compliance["subtitles"] = "external_srt"
        return
    if profile == "h265_archive_friendly":
        _expect_video(parsed, item, {"hevc", "h265"}, "video_codec")
        _expect_aac(config, parsed, item)
        _expect_no_default_image_subtitles(config, parsed, item)
        item.profile_compliance["subtitles"] = "external_srt"
        return


    item.warnings.append(f"profile compliance is not defined for {profile}")


def _expect_container(parsed: ScannedFile, item: OutputValidationItem, acceptable: set[str]) -> None:
    value = parsed.container_format or ""
    ok = value in acceptable or any(part in acceptable for part in value.split(","))
    item.profile_compliance["container"] = "pass" if ok else "fail"
    if not ok:
        item.errors.append(f"container is not acceptable: {value or 'unknown'}")


def _expect_video(parsed: ScannedFile, item: OutputValidationItem, codecs: set[str], key: str) -> None:
    ok = (parsed.video.codec or "").lower() in codecs
    item.profile_compliance[key] = "pass" if ok else "fail"
    if not ok:
        item.errors.append(f"video codec is not acceptable: {parsed.video.codec or 'none'}")


def _expect_pixel_format(parsed: ScannedFile, item: OutputValidationItem, formats: set[str]) -> None:
    ok = (parsed.video.pixel_format or "").lower() in formats
    item.profile_compliance["pixel_format"] = "pass" if ok else "fail"
    if not ok:
        item.errors.append(f"pixel format is not acceptable: {parsed.video.pixel_format or 'unknown'}")


def _expect_bit_depth(parsed: ScannedFile, item: OutputValidationItem, depths: set[int]) -> None:
    if parsed.video.bit_depth is None:
        item.warnings.append("video bit depth could not be detected")
        item.profile_compliance["bit_depth"] = "unknown"
        return
    ok = parsed.video.bit_depth in depths
    item.profile_compliance["bit_depth"] = "pass" if ok else "fail"
    if not ok:
        item.errors.append(f"video bit depth is not acceptable: {parsed.video.bit_depth}")


def _expect_aac(config: AppConfig, parsed: ScannedFile, item: OutputValidationItem) -> None:
    has_aac = any((stream.codec or "").lower() == "aac" for stream in parsed.audio_streams)
    item.profile_compliance["aac_fallback"] = "pass" if has_aac else "fail"
    if config.validation_require_aac_fallback and not has_aac:
        item.errors.append("AAC fallback audio is missing")


def _expect_no_default_image_subtitles(config: AppConfig, parsed: ScannedFile, item: OutputValidationItem) -> None:
    default_image = any((stream.codec or "").lower() in IMAGE_SUBTITLE_CODECS and stream.default for stream in parsed.subtitle_streams)
    item.profile_compliance["no_default_image_subtitles"] = "pass" if not default_image else "fail"
    if config.validation_require_no_default_image_subtitles and default_image:
        item.errors.append("image subtitle is marked default")


def _expect_text_subtitles_for_policy(parsed: ScannedFile, item: OutputValidationItem) -> None:
    if "srt" not in item.subtitle_policy and "text" not in item.subtitle_policy:
        return
    has_text = any((stream.codec or "").lower() in TEXT_SUBTITLE_CODECS for stream in parsed.subtitle_streams)
    item.profile_compliance["subtitle_policy"] = "pass" if has_text else "fail"
    if not has_text:
        if any(marker in item.subtitle_policy for marker in ["ocr_image_subtitles", "generate_missing_srt", "preserve_ass_add_srt_fallback"]):
            item.warnings.append("expected text/SRT subtitle is missing; generated subtitle workflows require manual confirmation")
        else:
            item.errors.append("expected text/SRT subtitle is missing")


def _source_from_row(row: dict) -> ScannedFile:
    video_data = json.loads(row["video_json"] or "{}")
    audio_data = json.loads(row["audio_json"] or "[]")
    subtitle_data = json.loads(row["subtitle_json"] or "[]")
    return ScannedFile(
        path=row["path"],
        filename=row["filename"],
        parent_disc_folder=str(Path(row["path"]).parent),
        size_bytes=row["size_bytes"],
        modified_time=row["modified_time"],
        duration_seconds=row["duration_seconds"],
        container_format=row["container_format"],
        video=VideoInfo(**video_data),
        audio_streams=[AudioStream(**stream) for stream in audio_data],
        subtitle_streams=[SubtitleStream(**stream) for stream in subtitle_data],
        chapter_count=row["chapter_count"],
        embedded_title=row["embedded_title"],
        makemkv_title=row["makemkv_title"],
        raw_ffprobe=json.loads(row["raw_ffprobe_json"] or "{}"),
    )


def _summary_dict(summary: JobValidationSummary) -> dict:
    return {
        "job_id": summary.job_id,
        "status": summary.status,
        "passed": summary.passed,
        "warnings": summary.warnings,
        "items": [asdict(item) for item in summary.items],
    }
