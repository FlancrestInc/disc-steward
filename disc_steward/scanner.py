from __future__ import annotations

import json
import logging
import subprocess
import time
from dataclasses import asdict
from pathlib import Path
from typing import Callable

from .classifier import classify_disc_files
from .config import AppConfig
from .db import Database
from .models import AudioStream, ScannedFile, SubtitleStream, VideoInfo
from .title_discovery import discover_title_from_scan, refine_title_discovery_with_ollama
from .notifications import send_notification
from .preview import queue_previews_for_job

LOG = logging.getLogger(__name__)

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
    title_discovery_sender: Callable[[str, dict], dict] | None = None,
) -> int | None:
    db.initialize()
    resolved_disc_folder = str(disc_folder.resolve())
    if db.is_ignored_disc_path(resolved_disc_folder):
        LOG.info("Skipping ignored rip folder: %s", resolved_disc_folder)
        return None
    job_id = db.upsert_job(disc_folder, "review_needed")
    runner = ffprobe_runner or (lambda path: run_ffprobe(config.ffprobe_path, path))
    scanned_files: list[ScannedFile] = []
    source_ids: dict[str, int] = {}
    for media_path in sorted(disc_folder.rglob("*.mkv")):
        if media_path.stat().st_size == 0:
            continue
        scanned = parse_ffprobe(runner(media_path), media_path)
        scanned_files.append(scanned)
        source_ids[scanned.path] = db.upsert_source_file(job_id, scanned)
    classifications = classify_disc_files(scanned_files)
    for scanned in scanned_files:
        source_id = source_ids[scanned.path]
        db.save_classification(source_id, classifications[scanned.path])
    discovery = discover_title_from_scan(disc_folder, scanned_files)
    if config.title_discovery.enabled:
        discovery = refine_title_discovery_with_ollama(config, discovery, sender=title_discovery_sender)
    review = db.get_job_review(job_id)
    review.title_discovery_json = asdict(discovery)
    db.save_job_review(review)
    db.audit(
        "title_discovery",
        f"Discovered title candidate '{discovery.title or review.title}' with {len(discovery.signals)} signal(s)",
        job_id,
        {"disc_folder": str(disc_folder), "title_discovery": asdict(discovery)},
    )
    db.audit("scan", f"Scanned {len(scanned_files)} MKV file(s)", job_id, {"disc_folder": str(disc_folder)})
    if config.preview.enabled:
        queued = queue_previews_for_job(db, config, job_id)
        if queued:
            db.audit("preview_batch_queued", f"Queued {queued} preview job(s)", job_id, {"queued": queued})
    send_notification(
        config,
        f"Disc ready for review: {discovery.title or review.title or disc_folder.name}",
        f"Job {job_id} was discovered in {disc_folder}. Open the review UI to confirm metadata before processing.",
        priority="default",
        tags=["disc", "review"],
    )
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


def _folder_latest_mtime(folder: Path) -> float | None:
    latest: float | None = None
    for path in folder.rglob("*"):
        try:
            mtime = path.stat().st_mtime
        except FileNotFoundError:
            continue
        if latest is None or mtime > latest:
            latest = mtime
    return latest


def _folder_is_settled(folder: Path, settle_seconds: int, now: float | None = None) -> bool:
    latest_mtime = _folder_latest_mtime(folder)
    if latest_mtime is None:
        return False
    current_time = time.time() if now is None else now
    return current_time - latest_mtime >= settle_seconds


def scan_completed_rips(db: Database, config: AppConfig) -> list[int]:
    job_ids: list[int] = []
    known_paths = set(db.list_disc_paths()) | set(db.list_ignored_disc_paths())
    for folder in sorted(config.raw_rip_path.iterdir() if config.raw_rip_path.exists() else []):
        if not folder.is_dir() or not any(folder.rglob("*.mkv")):
            continue
        if not _folder_is_settled(folder, config.raw_rip_settle_seconds):
            continue
        resolved = str(folder.resolve())
        if resolved in known_paths:
            continue
        job_id = scan_disc_folder(db, config, folder)
        if job_id is None:
            known_paths.add(resolved)
            continue
        job_ids.append(job_id)
        known_paths.add(resolved)
    return job_ids


def watch_completed_rips(
    db: Database,
    config: AppConfig,
    interval_seconds: float = 30.0,
    *,
    max_cycles: int | None = None,
    sleep_fn=None,
) -> list[int]:
    import time

    sleeper = sleep_fn or time.sleep
    discovered: list[int] = []
    seen_job_ids: set[int] = set()
    cycles = 0
    while True:
        try:
            for job_id in scan_completed_rips(db, config):
                if job_id in seen_job_ids:
                    continue
                seen_job_ids.add(job_id)
                discovered.append(job_id)
                LOG.info("watcher_discovered_job=%s", job_id)
        except Exception as exc:
            LOG.exception("watcher_scan_failed")
            send_notification(config, "Disc Steward watcher error", str(exc), priority="high", tags=["warning", "watcher"])
        cycles += 1
        if max_cycles is not None and cycles >= max_cycles:
            return discovered
        sleeper(interval_seconds)
