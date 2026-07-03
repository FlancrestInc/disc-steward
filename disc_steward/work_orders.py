from __future__ import annotations

import json
import re
import subprocess
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .config import AppConfig
from .models import AudioStream, FileReviewDecision, GeneratedPath, JobReviewMetadata, ReviewDecision, ScannedFile, SubtitleStream, VideoInfo
from .review import validate_review_ready
from .subtitle_planner import generate_subtitle_plan, plan_to_dict

INVALID_FILENAME_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
METADATA_FILENAME_KEYS = (
    ("imdb_id", "imdbid"),
    ("tmdb_id", "tmdbid"),
    ("tvdb_id", "tvdbid"),
    ("anidb_id", "anidbid"),
    ("anilist_id", "anilistid"),
    ("mal_id", "malid"),
)
EXTRA_ROLES = {
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
}
TEXT_SUBTITLE_CODECS = {"subrip", "srt", "webvtt", "mov_text"}
ASS_SUBTITLE_CODECS = {"ass", "ssa"}
MUXABLE_SUBTITLE_CODECS = TEXT_SUBTITLE_CODECS | ASS_SUBTITLE_CODECS


def sanitize_filename_component(value: str | None) -> str:
    cleaned = INVALID_FILENAME_RE.sub(" ", value or "").strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    cleaned = cleaned.strip(". ")
    return cleaned or "Untitled"


def clean_filename(value: str) -> str:
    return sanitize_filename_component(value)


def _metadata_suffix(obj: object) -> str:
    parts = []
    for attr, tag in METADATA_FILENAME_KEYS:
        value = getattr(obj, attr, None)
        if value:
            parts.append(f"[{tag}-{sanitize_filename_component(str(value))}]")
    return f" {' '.join(parts)}" if parts else ""


def _parent_title(job: JobReviewMetadata) -> str:
    return sanitize_filename_component(job.title)


def _year_suffix(year: int | None) -> str:
    return f" ({year})" if year else ""


def _file_label(job: JobReviewMetadata, decision: FileReviewDecision) -> str:
    return sanitize_filename_component(
        decision.final_display_name
        or decision.translated_title
        or decision.romanized_title
        or decision.original_title
        or job.title
    )


def _movie_folder(job: JobReviewMetadata) -> str:
    return f"{_parent_title(job)}{_year_suffix(job.year)}"


def _library_root(config: AppConfig, target_library: str) -> Path:
    library_root = config.eddy_library_roots.get(target_library)
    if library_root is None:
        raise ValueError(f"Unknown target library: {target_library}")
    return library_root


def _is_episode_like(job: JobReviewMetadata, decision: FileReviewDecision) -> bool:
    return job.content_type in {"show", "anime"} or decision.role == "episode" or decision.season_number is not None


def _build_path_for_file(config: AppConfig, job: JobReviewMetadata, decision: FileReviewDecision) -> Path:
    library_root = _library_root(config, job.library_root)
    parent = _parent_title(job)
    label = _file_label(job, decision)
    if decision.final_filename:
        filename = sanitize_filename_component(decision.final_filename)
        if not filename.lower().endswith(".mkv"):
            filename = f"{filename}.mkv"
    elif _is_episode_like(job, decision):
        season = decision.season_number if decision.season_number is not None else 1
        episode = decision.episode_number if decision.episode_number is not None else 1
        filename = f"{parent} - S{season:02d}E{episode:02d} - {label}.mkv"
        return library_root / parent / f"Season {season:02d}" / filename
    elif decision.role in EXTRA_ROLES or decision.content_type == "extra":
        filename = f"{label}.mkv"
        return library_root / _movie_folder(job) / "extras" / filename
    else:
        folder = _movie_folder(job)
        filename = f"{folder}{_metadata_suffix(job)}.mkv"
    if _is_episode_like(job, decision):
        season = decision.season_number if decision.season_number is not None else 1
        return library_root / parent / f"Season {season:02d}" / filename
    return library_root / _movie_folder(job) / filename


def generate_final_paths(
    config: AppConfig,
    job: JobReviewMetadata,
    decisions: list[FileReviewDecision],
) -> dict[int, GeneratedPath]:
    included = [decision for decision in decisions if decision.include_in_work_order]
    controller_paths = {decision.source_file_id: _build_path_for_file(config, job, decision) for decision in included}
    final_paths = {source_file_id: config.to_eddy_path(path) for source_file_id, path in controller_paths.items()}
    counts = Counter(str(path) for path in final_paths.values())
    generated: dict[int, GeneratedPath] = {}
    for source_file_id, final_path in final_paths.items():
        controller_path = controller_paths[source_file_id]
        conflicts: list[str] = []
        if counts[str(final_path)] > 1:
            conflicts.append("duplicate generated final path")
        if controller_path.exists():
            conflicts.append("final path already exists")
        generated[source_file_id] = GeneratedPath(
            source_file_id=source_file_id,
            final_path=final_path,
            output_name=final_path.name,
            controller_path=controller_path,
            conflicts=conflicts,
        )
    return generated


def build_final_library_path(config: AppConfig, decision: ReviewDecision) -> Path:
    job = JobReviewMetadata(
        job_id=0,
        title=decision.title,
        year=decision.year,
        content_type=decision.content_type,
        library_root=decision.target_library,
        imdb_id=decision.imdb_id,
        tmdb_id=decision.tmdb_id,
        tvdb_id=decision.tvdb_id,
        anidb_id=decision.anidb_id,
        anilist_id=decision.anilist_id,
        mal_id=decision.mal_id,
    )
    file_decision = FileReviewDecision(
        source_file_id=decision.source_file_id,
        role=decision.role,
        content_type=decision.content_type,
        final_display_name=decision.final_display_name,
        extra_type=decision.extra_type,
        season_number=decision.season,
        episode_number=decision.episode,
        encoding_profile=decision.encoding_profile,
        subtitle_policy=decision.subtitle_policy,
    )
    return config.to_eddy_path(_build_path_for_file(config, job, file_decision))


def _metadata_ids(job: JobReviewMetadata, decision: FileReviewDecision) -> dict[str, str]:
    pairs = {
        "imdb": decision.imdb_id or job.imdb_id,
        "tmdb": decision.tmdb_id or job.tmdb_id,
        "tvdb": decision.tvdb_id or job.tvdb_id,
        "anidb": decision.anidb_id or job.anidb_id,
        "anilist": decision.anilist_id or job.anilist_id,
        "mal": decision.mal_id or job.mal_id,
    }
    return {key: value for key, value in pairs.items() if value}


def _subtitle_streams_for_mux(source: ScannedFile) -> list[SubtitleStream]:
    return [stream for stream in source.subtitle_streams if (stream.codec or "").lower() in MUXABLE_SUBTITLE_CODECS]


def _subtitle_warning_messages(source: ScannedFile) -> list[str]:
    warnings: list[str] = []
    image_streams = [stream for stream in source.subtitle_streams if (stream.codec or "").lower() not in MUXABLE_SUBTITLE_CODECS]
    if image_streams:
        warnings.append("image subtitle streams were detected and are not converted by the ffmpeg fallback path")
    if any((stream.codec or "").lower() in ASS_SUBTITLE_CODECS for stream in source.subtitle_streams):
        warnings.append("ASS/SSA subtitles are preserved by the ffmpeg fallback path and converted to SRT when mapped")
    return warnings


def _subtitle_output_positions(subtitles: list[SubtitleStream]) -> dict[int, int]:
    return {stream.index: position for position, stream in enumerate(subtitles)}


def _subtitle_disposition_args(source: ScannedFile, subtitle_plan: dict | None, subtitles: list[SubtitleStream]) -> list[str]:
    if not subtitle_plan:
        return []
    positions = _subtitle_output_positions(subtitles)
    args: list[str] = []
    for action in subtitle_plan.get("actions", []):
        position = positions.get(action.get("source_stream_index"))
        if position is None:
            continue
        action_type = action.get("type")
        if action_type == "unset_default":
            args.extend([f"-disposition:s:{position}", "0"])
        elif action_type == "mark_default":
            args.extend([f"-disposition:s:{position}", "default"])
        elif action_type == "mark_forced":
            args.extend([f"-disposition:s:{position}", "forced"])
    return args


def _build_ffmpeg_command(
    config: AppConfig,
    source: ScannedFile,
    decision: FileReviewDecision,
    output_path: Path,
    subtitle_plan: dict | None = None,
) -> list[str]:
    profile = (decision.encoding_profile or "").strip() or "universal_h264_aac_srt"
    command = [config.ffmpeg_path, "-hide_banner", "-y", "-nostdin", "-i", str(source.path), "-map_metadata", "0", "-map_chapters", "0"]
    subtitles = source.subtitle_streams if profile == "remux_only" else _subtitle_streams_for_mux(source)
    if subtitle_plan is None:
        subtitle_plan = plan_to_dict(
            generate_subtitle_plan(
                source,
                content_type=decision.content_type or "movie",
                subtitle_policy=decision.subtitle_policy,
                preferred_format=config.subtitle_planning.preferred_format,
                preserve_original_subtitles=config.subtitle_planning.preserve_original_subtitles,
            )
        )
    if profile == "remux_only":
        command.extend(["-map", "0", "-c", "copy"])
    else:
        command.extend(["-map", "0:v:0", "-map", "0:a?"])
        if profile == "subtitle_fix_only":
            command.extend(["-c:v", "copy", "-c:a", "copy"])
        elif profile == "h265_archive_friendly":
            command.extend(["-c:v", "libx265", "-preset", "medium", "-crf", "20", "-pix_fmt", "yuv420p10le", "-c:a", "aac", "-b:a", "192k"])
        else:
            command.extend(["-c:v", "libx264", "-preset", "medium", "-crf", "18", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k"])
        if subtitles:
            for subtitle in subtitles:
                command.extend(["-map", f"0:{subtitle.index}"])
            if profile == "subtitle_fix_only":
                command.extend(["-c:s", "copy"])
            else:
                command.extend(["-c:s", "srt"])
        else:
            command.append("-sn")
    command.extend(_subtitle_disposition_args(source, subtitle_plan, subtitles))
    command.append(str(output_path))
    return command


def build_ffmpeg_item_payload(
    config: AppConfig,
    job_id: int,
    item_id: int,
    source_path: Path,
    job: JobReviewMetadata,
    decision: FileReviewDecision,
    source: ScannedFile | None = None,
) -> dict:
    final_path = (
        config.to_eddy_path(Path(decision.generated_final_path))
        if decision.generated_final_path
        else config.to_eddy_path(_build_path_for_file(config, job, decision))
    )
    subtitle_plan = (
        plan_to_dict(
            generate_subtitle_plan(
                source,
                content_type=decision.content_type or job.content_type,
                subtitle_policy=decision.subtitle_policy,
                preferred_format=config.subtitle_planning.preferred_format,
                preserve_original_subtitles=config.subtitle_planning.preserve_original_subtitles,
            )
        )
        if source is not None
        else {
            "policy": decision.subtitle_policy,
            "preferred_format": config.preferred_subtitle_format,
            "preserve_original_subtitles": True,
            "statuses": ["manual_review_required"],
            "actions": [],
            "warnings": ["source stream details were unavailable when the subtitle plan was generated"],
        }
    )
    warnings = list(getattr(decision, "warnings", []))
    if source is not None:
        warnings.extend(_subtitle_warning_messages(source))
    payload = {
        "job_id": job_id,
        "item_id": item_id,
        "source_path": str(config.to_barnabas_path(source_path)),
        "content_type": decision.content_type or job.content_type,
        "role": decision.role,
        "title": job.title,
        "original_title": decision.original_title or job.original_title,
        "translated_title": decision.translated_title,
        "romanized_title": decision.romanized_title,
        "year": job.year,
        "metadata_ids": _metadata_ids(job, decision),
        "profile": decision.encoding_profile,
        "subtitle_policy": decision.subtitle_policy,
        "subtitle_plan": subtitle_plan,
        "output_name": final_path.name,
        "barnabas_validation_output_dir": str(config.to_barnabas_path(config.validation_needed_path / f"job_{job_id}")),
        "final_library_path": str(final_path),
        "preserve_original_audio": True,
        "preserve_original_subtitles": True,
        "processing_engine": "ffmpeg",
        "warnings": warnings,
        "created_by": "disc-steward",
    }
    if source is not None:
        payload["ffmpeg_command"] = _build_ffmpeg_command(
            config,
            source,
            decision,
            config.to_barnabas_path(config.validation_needed_path / f"job_{job_id}") / final_path.name,
            subtitle_plan,
        )
    return payload


# Legacy name kept for compatibility with older tests and callers.
build_fileflows_item_payload = build_ffmpeg_item_payload


def _source_from_row(row: dict) -> ScannedFile:
    return ScannedFile(
        path=row["path"],
        filename=row["filename"],
        parent_disc_folder=row["parent_disc_folder"],
        size_bytes=row["size_bytes"],
        modified_time=row["modified_time"],
        duration_seconds=row["duration_seconds"],
        container_format=row["container_format"],
        video=VideoInfo(**json.loads(row["video_json"] or "{}")),
        audio_streams=[AudioStream(**stream) for stream in json.loads(row["audio_json"] or "[]")],
        subtitle_streams=[SubtitleStream(**stream) for stream in json.loads(row["subtitle_json"] or "[]")],
        chapter_count=row["chapter_count"],
        embedded_title=row["embedded_title"],
        makemkv_title=row["makemkv_title"],
        raw_ffprobe=json.loads(row["raw_ffprobe_json"] or "{}"),
    )


def _run_ffmpeg(command: list[str], runner: Callable[[list[str]], object] | None = None) -> None:
    if runner is None:
        subprocess.run(command, check=True)
        return
    runner(command)


def create_ffmpeg_processing_jobs(
    db,
    config: AppConfig,
    job_id: int,
    ffmpeg_runner: Callable[[list[str]], object] | None = None,
) -> Path:
    job = db.get_job(job_id)
    if job is None:
        raise ValueError(f"Unknown job_id: {job_id}")
    job_review = db.get_job_review(job_id)
    if job_review.review_status not in {"reviewed", "ready_for_fileflows", "fileflows_work_orders_created", "ready_for_processing"}:
        raise ValueError("job must be reviewed before processing outputs can be created")
    decisions = db.list_file_reviews(job_id)
    paths = generate_final_paths(config, job_review, decisions)
    validate_review_ready(job_review, decisions, paths)
    for decision in decisions:
        generated = paths.get(decision.source_file_id)
        if generated:
            decision.generated_final_path = str(generated.final_path)
            decision.conflicts = generated.conflicts
            db.save_file_review(decision)

    included = [decision for decision in decisions if decision.include_in_work_order]
    source_rows = {row["id"]: row for row in db.source_file_payloads(job_id)}
    job_dir = config.fileflows_work_order_path / f"job_{job_id}"
    items_dir = job_dir / "items"
    items_dir.mkdir(parents=True, exist_ok=True)
    created = datetime.now(timezone.utc).isoformat()
    item_paths: list[Path] = []
    warnings: list[str] = []
    run_ffmpeg = ffmpeg_runner or (lambda command: subprocess.run(command, check=True))
    processing_status = "validation_needed"
    for index, decision in enumerate(included, start=1):
        row = source_rows[decision.source_file_id]
        source = _source_from_row(row)
        payload = build_ffmpeg_item_payload(
            config,
            job_id,
            decision.source_file_id,
            Path(row["path"]),
            job_review,
            decision,
            source,
        )
        item_path = items_dir / f"item_{index:03d}.process.json"
        item_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        db.save_work_order_record(job_id, decision.source_file_id, str(item_path), payload, status="planned" if config.dry_run else "processed")
        db.save_subtitle_plan(decision.source_file_id, payload["subtitle_plan"])
        item_paths.append(item_path)
        warnings.extend(decision.warnings)
        warnings.extend(payload.get("warnings", []))
        if not config.dry_run:
            output_path = Path(payload["barnabas_validation_output_dir"]) / payload["output_name"]
            if output_path.exists() and not config.overwrite_existing:
                raise FileExistsError(f"output already exists: {output_path}")
            output_path.parent.mkdir(parents=True, exist_ok=True)
            _run_ffmpeg(payload["ffmpeg_command"], run_ffmpeg)
            if not output_path.exists():
                raise RuntimeError(f"ffmpeg did not create expected output: {output_path}")
    manifest = {
        "job_id": job_id,
        "disc_folder": job.disc_title,
        "disc_path": str(config.to_barnabas_path(Path(job.disc_path))),
        "controller_disc_path": job.disc_path,
        "parent_title": job_review.title,
        "year": job_review.year,
        "content_type": job_review.content_type,
        "created_time": created,
        "included_items": len(included),
        "excluded_ignored_items": len([decision for decision in decisions if not decision.include_in_work_order]),
        "target_library_root": job_review.library_root,
        "warnings": [*job_review.warnings, *warnings],
        "notes": job_review.notes,
        "items": [str(config.to_barnabas_path(path)) for path in item_paths],
        "todo": "Run reviewed ffmpeg processing for the job; Disc Steward does not depend on ffmpeg.",
    }
    (job_dir / "job_manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    job_review.review_status = processing_status if not config.dry_run else "ready_for_processing"
    job_review.work_order_folder = str(job_dir)
    job_review.work_order_created_at = created
    if warnings:
        job_review.warnings = list(dict.fromkeys([*job_review.warnings, *warnings]))
    db.save_job_review(job_review)
    db.audit(
        "ffmpeg_processing_created",
        f"Processed {len(item_paths)} item(s) with ffmpeg",
        job_id,
        {"folder": str(job_dir), "dry_run": config.dry_run},
    )
    return job_dir


# Legacy name kept for compatibility with older commands/tests.
create_fileflows_work_orders = create_ffmpeg_processing_jobs
