from __future__ import annotations

from pathlib import Path
from typing import Callable

from .models import ScannedFile, ValidationResult
from .scanner import parse_ffprobe


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
