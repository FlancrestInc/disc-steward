from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Callable

from .classifier import classify_disc_files
from .config import AppConfig
from .db import Database
from .models import AudioStream, ScannedFile, SubtitleStream, VideoInfo

IMAGE_SUBTITLE_CODECS = {"hdmv_pgs_subtitle", "dvd_subtitle", "dvb_subtitle", "xsub"}


def run_ffprobe(ffprobe_path: str, media_path: Path) -> str:
    result = subprocess.run(
        [
            ffprobe_path,
            "-v",
            "quiet",
            "-print_format",
            "json",
            "-show_format",
            "-show_streams",
            "-show_chapters",
            str(media_path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout


def _bool_disposition(stream: dict, key: str) -> bool:
    return bool((stream.get("disposition") or {}).get(key, 0))


def _language(stream: dict) -> str | None:
    language = (stream.get("tags") or {}).get("language")
    return None if language in {"", "und"} else language


def _bit_depth(stream: dict) -> int | None:
    if stream.get("bits_per_raw_sample"):
        try:
            return int(stream["bits_per_raw_sample"])
        except ValueError:
            pass
    pix_fmt = stream.get("pix_fmt") or ""
    if "10" in pix_fmt:
        return 10
    if "12" in pix_fmt:
        return 12
    if pix_fmt:
        return 8
    return None


def _frame_rate_mode(stream: dict) -> str | None:
    avg = stream.get("avg_frame_rate")
    real = stream.get("r_frame_rate")
    if not avg or avg == "0/0":
        return None
    return "constant" if avg == real else "variable_or_unknown"


def _hdr_indicators(stream: dict) -> list[str]:
    indicators: list[str] = []
    for side_data in stream.get("side_data_list") or []:
        label = side_data.get("side_data_type")
        if label and ("Mastering" in label or "Content light" in label or "DOVI" in label):
            indicators.append(label)
    color_transfer = stream.get("color_transfer")
    if color_transfer in {"smpte2084", "arib-std-b67"}:
        indicators.append(color_transfer)
    return indicators


def parse_ffprobe(ffprobe_json: str, media_path: Path) -> ScannedFile:
    data = json.loads(ffprobe_json)
    media_path = media_path.resolve()
    stat = media_path.stat() if media_path.exists() else None
    fmt = data.get("format") or {}
    streams = data.get("streams") or []
    video_stream = next((stream for stream in streams if stream.get("codec_type") == "video"), {})
    tags = fmt.get("tags") or {}
    video = VideoInfo(
        codec=video_stream.get("codec_name"),
        profile=video_stream.get("profile"),
        pixel_format=video_stream.get("pix_fmt"),
        bit_depth=_bit_depth(video_stream),
        width=video_stream.get("width"),
        height=video_stream.get("height"),
        frame_rate=video_stream.get("avg_frame_rate"),
        frame_rate_mode=_frame_rate_mode(video_stream),
        hdr_indicators=_hdr_indicators(video_stream),
    )
    audio = [
        AudioStream(
            index=int(stream.get("index", -1)),
            codec=stream.get("codec_name"),
            channels=stream.get("channels"),
            channel_layout=stream.get("channel_layout"),
            language=_language(stream),
            title=(stream.get("tags") or {}).get("title"),
            default=_bool_disposition(stream, "default"),
            forced=_bool_disposition(stream, "forced"),
        )
        for stream in streams
        if stream.get("codec_type") == "audio"
    ]
    subtitles = [
        SubtitleStream(
            index=int(stream.get("index", -1)),
            codec=stream.get("codec_name"),
            language=_language(stream),
            title=(stream.get("tags") or {}).get("title"),
            default=_bool_disposition(stream, "default"),
            forced=_bool_disposition(stream, "forced"),
            hearing_impaired="sdh" in ((stream.get("tags") or {}).get("title") or "").lower()
            or "hearing" in ((stream.get("tags") or {}).get("title") or "").lower(),
        )
        for stream in streams
        if stream.get("codec_type") == "subtitle"
    ]
    duration = float(fmt["duration"]) if fmt.get("duration") else None
    return ScannedFile(
        path=str(media_path),
        filename=media_path.name,
        parent_disc_folder=str(media_path.parent),
        size_bytes=stat.st_size if stat else int(fmt.get("size") or 0),
        modified_time=stat.st_mtime if stat else 0.0,
        duration_seconds=duration,
        container_format=fmt.get("format_name"),
        video=video,
        audio_streams=audio,
        subtitle_streams=subtitles,
        chapter_count=len(data.get("chapters") or []),
        embedded_title=tags.get("title"),
        makemkv_title=tags.get("MAKEMKV_TITLE") or tags.get("makemkv_title"),
        raw_ffprobe=data,
    )


def scan_disc_folder(
    db: Database,
    config: AppConfig,
    disc_folder: Path,
    ffprobe_runner: Callable[[Path], str] | None = None,
    metadata_lookup: Callable[[Database, AppConfig, int], object] | None = None,
) -> int:
    db.initialize()
    job_id = db.upsert_job(disc_folder, "review_needed")
    runner = ffprobe_runner or (lambda path: run_ffprobe(config.ffprobe_path, path))
    scanned_files: list[ScannedFile] = []
    for media_path in sorted(disc_folder.rglob("*.mkv")):
        if media_path.stat().st_size == 0:
            continue
        scanned = parse_ffprobe(runner(media_path), media_path)
        scanned_files.append(scanned)
        db.upsert_source_file(job_id, scanned)
    classifications = classify_disc_files(scanned_files)
    for scanned in scanned_files:
        source_id = db.upsert_source_file(job_id, scanned)
        db.save_classification(source_id, classifications[scanned.path])
    db.audit("scan", f"Scanned {len(scanned_files)} MKV file(s)", job_id, {"disc_folder": str(disc_folder)})
    if config.metadata.enabled:
        lookup = metadata_lookup
        if lookup is None:
            from .metadata import lookup_job_metadata

            lookup = lookup_job_metadata
        try:
            lookup(db, config, job_id)
        except Exception as error:
            db.audit("metadata_lookup_failed", str(error), job_id)
    return job_id


def scan_completed_rips(db: Database, config: AppConfig) -> list[int]:
    job_ids: list[int] = []
    for folder in sorted(config.raw_rip_path.iterdir() if config.raw_rip_path.exists() else []):
        if folder.is_dir() and any(folder.rglob("*.mkv")):
            job_ids.append(scan_disc_folder(db, config, folder))
    return job_ids
