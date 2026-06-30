from __future__ import annotations

import json
import re
from pathlib import Path

from .config import AppConfig
from .models import ReviewDecision


def clean_filename(value: str) -> str:
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', " ", value).strip()


def build_final_library_path(config: AppConfig, decision: ReviewDecision) -> Path:
    library_root = config.eddy_library_roots.get(decision.target_library)
    if library_root is None:
        raise ValueError(f"Unknown target library: {decision.target_library}")
    title = clean_filename(decision.final_display_name or decision.title)
    year = f" ({decision.year})" if decision.year else ""
    imdb = f" [imdbid-{decision.imdb_id}]" if decision.imdb_id else ""
    if decision.content_type in {"show", "anime"} and decision.season is not None:
        season_dir = f"Season {decision.season:02d}"
        episode = f"S{decision.season:02d}E{(decision.episode or 1):02d}"
        return library_root / title / season_dir / f"{title} - {episode} - {clean_filename(decision.extra_type or decision.role)}.mkv"
    folder = library_root / f"{title}{year}"
    if decision.role != "main_feature":
        return folder / "extras" / f"{clean_filename(decision.extra_type or title)}.mkv"
    return folder / f"{title}{year}{imdb}.mkv"


def build_work_order_payload(config: AppConfig, job_id: int, source_path: Path, decision: ReviewDecision) -> dict:
    final_path = build_final_library_path(config, decision)
    return {
        "job_id": job_id,
        "source_path": str(source_path),
        "content_type": decision.content_type,
        "role": decision.role,
        "title": decision.title,
        "year": decision.year,
        "metadata_ids": {
            "imdb": decision.imdb_id,
            "tmdb": decision.tmdb_id,
            "tvdb": decision.tvdb_id,
            "anidb": decision.anidb_id,
            "anilist": decision.anilist_id,
            "mal": decision.mal_id,
        },
        "profile": decision.encoding_profile,
        "subtitle_policy": decision.subtitle_policy,
        "output_name": final_path.name,
        "final_library_path": str(final_path),
    }


def write_work_order(config: AppConfig, job_id: int, source_path: Path, decision: ReviewDecision, dry_run: bool = True) -> Path:
    job_dir = config.fileflows_work_order_path / f"job_{job_id}"
    work_order_path = job_dir / "work_order.json"
    if dry_run:
        return work_order_path
    job_dir.mkdir(parents=True, exist_ok=True)
    work_order_path.write_text(json.dumps(build_work_order_payload(config, job_id, source_path, decision), indent=2), encoding="utf-8")
    return work_order_path
