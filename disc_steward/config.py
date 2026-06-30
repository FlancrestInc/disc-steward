from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:  # pragma: no cover - exercised when dependency is absent
    yaml = None


@dataclass
class JellyfinConfig:
    base_url: str | None = None
    api_key: str | None = None
    refresh_enabled: bool = False
    library_ids: list[str] = field(default_factory=list)


@dataclass
class LLMConfig:
    enabled: bool = False
    provider: str = "hermes"


@dataclass
class AppConfig:
    pipeline_root: Path
    raw_rip_path: Path
    scan_complete_path: Path
    review_needed_path: Path
    fileflows_work_order_path: Path
    fileflows_working_path: Path
    validation_needed_path: Path
    ready_for_eddy_path: Path
    transferred_to_eddy_path: Path
    manual_review_path: Path
    failed_path: Path
    database_path: Path
    ffprobe_path: str = "ffprobe"
    ffmpeg_path: str = "ffmpeg"
    eddy_incoming_path: Path = Path("/mnt/jellyfin-media/.incoming")
    eddy_library_roots: dict[str, Path] = field(default_factory=dict)
    transfer_method: str = "local_mount"
    rsync_destination: str | None = None
    preferred_video_profile: str = "universal_h264_aac_srt"
    preferred_audio_fallback_codec: str = "aac"
    preferred_subtitle_format: str = "srt"
    encoding_profiles: list[str] = field(
        default_factory=lambda: [
            "remux_only",
            "universal_h264_aac_srt",
            "subtitle_fix_only",
            "h265_archive_friendly",
            "manual_review",
        ]
    )
    subtitle_policies: list[str] = field(
        default_factory=lambda: [
            "preserve_existing",
            "prefer_srt_preserve_original",
            "ocr_image_subtitles_to_srt_preserve_original",
            "generate_missing_srt_unverified",
            "preserve_ass_add_srt_fallback",
            "manual_review",
        ]
    )
    minimum_title_duration_seconds: int = 30
    duration_tolerance_seconds: int = 5
    cleanup_enabled: bool = False
    raw_rip_retention_days: int = 30
    working_file_retention_days: int = 14
    overwrite_existing: bool = False
    dry_run: bool = True
    jellyfin: JellyfinConfig = field(default_factory=JellyfinConfig)
    llm: LLMConfig = field(default_factory=LLMConfig)

    @classmethod
    def default_for_root(cls, root: Path) -> "AppConfig":
        pipeline = root / "media-pipeline"
        eddy = root / "eddy"
        return cls(
            pipeline_root=pipeline,
            raw_rip_path=pipeline / "01_disc_rips_raw",
            scan_complete_path=pipeline / "02_scan_complete",
            review_needed_path=pipeline / "03_review_needed",
            fileflows_work_order_path=pipeline / "04_ready_for_fileflows",
            fileflows_working_path=pipeline / "05_fileflows_working",
            validation_needed_path=pipeline / "06_validation_needed",
            ready_for_eddy_path=pipeline / "07_ready_for_eddy",
            transferred_to_eddy_path=pipeline / "08_transferred_to_eddy",
            manual_review_path=pipeline / "90_manual_review",
            failed_path=pipeline / "99_failed",
            database_path=pipeline / "disc_steward.sqlite3",
            eddy_incoming_path=eddy / ".incoming",
            eddy_library_roots={
                "Movies": eddy / "Movies",
                "Shows": eddy / "Shows",
                "Anime": eddy / "Anime",
                "Family Videos": eddy / "Family Videos",
            },
        )


def _path(value: str | Path) -> Path:
    return Path(value).expanduser()


def load_config(path: str | Path) -> AppConfig:
    if yaml is None:
        raise RuntimeError("PyYAML is required to load YAML config files")
    data = yaml.safe_load(Path(path).read_text()) or {}
    return config_from_dict(data)


def config_from_dict(data: dict[str, Any]) -> AppConfig:
    root = _path(data.get("pipeline_root", "/mnt/data2/media-pipeline"))
    paths = data.get("paths", {})
    eddy = data.get("eddy", {})
    defaults = AppConfig.default_for_root(root.parent)
    library_roots = {
        name: _path(value)
        for name, value in eddy.get(
            "library_roots",
            {
                "Movies": "/mnt/jellyfin-media/Movies",
                "Shows": "/mnt/jellyfin-media/Shows",
                "Anime": "/mnt/jellyfin-media/Anime",
                "Family Videos": "/mnt/jellyfin-media/Family Videos",
            },
        ).items()
    }
    jellyfin = data.get("jellyfin", {})
    llm = data.get("llm", {})
    return AppConfig(
        pipeline_root=root,
        raw_rip_path=_path(paths.get("raw_rip_path", root / "01_disc_rips_raw")),
        scan_complete_path=_path(paths.get("scan_complete_path", root / "02_scan_complete")),
        review_needed_path=_path(paths.get("review_needed_path", root / "03_review_needed")),
        fileflows_work_order_path=_path(paths.get("fileflows_work_order_path", root / "04_ready_for_fileflows")),
        fileflows_working_path=_path(paths.get("fileflows_working_path", root / "05_fileflows_working")),
        validation_needed_path=_path(paths.get("validation_needed_path", root / "06_validation_needed")),
        ready_for_eddy_path=_path(paths.get("ready_for_eddy_path", root / "07_ready_for_eddy")),
        transferred_to_eddy_path=_path(paths.get("transferred_to_eddy_path", root / "08_transferred_to_eddy")),
        manual_review_path=_path(paths.get("manual_review_path", root / "90_manual_review")),
        failed_path=_path(paths.get("failed_path", root / "99_failed")),
        database_path=_path(data.get("database_path", defaults.database_path)),
        ffprobe_path=data.get("ffprobe_path", "ffprobe"),
        ffmpeg_path=data.get("ffmpeg_path", "ffmpeg"),
        eddy_incoming_path=_path(eddy.get("incoming_path", "/mnt/jellyfin-media/.incoming")),
        eddy_library_roots=library_roots,
        transfer_method=data.get("transfer_method", "local_mount"),
        rsync_destination=data.get("rsync_destination"),
        preferred_video_profile=data.get("preferred_video_profile", "universal_h264_aac_srt"),
        preferred_audio_fallback_codec=data.get("preferred_audio_fallback_codec", "aac"),
        preferred_subtitle_format=data.get("preferred_subtitle_format", "srt"),
        encoding_profiles=list(
            data.get(
                "encoding_profiles",
                [
                    "remux_only",
                    "universal_h264_aac_srt",
                    "subtitle_fix_only",
                    "h265_archive_friendly",
                    "manual_review",
                ],
            )
        ),
        subtitle_policies=list(
            data.get(
                "subtitle_policies",
                [
                    "preserve_existing",
                    "prefer_srt_preserve_original",
                    "ocr_image_subtitles_to_srt_preserve_original",
                    "generate_missing_srt_unverified",
                    "preserve_ass_add_srt_fallback",
                    "manual_review",
                ],
            )
        ),
        minimum_title_duration_seconds=int(data.get("minimum_title_duration_seconds", 30)),
        duration_tolerance_seconds=int(data.get("duration_tolerance_seconds", 5)),
        cleanup_enabled=bool(data.get("cleanup_enabled", False)),
        raw_rip_retention_days=int(data.get("raw_rip_retention_days", 30)),
        working_file_retention_days=int(data.get("working_file_retention_days", 14)),
        overwrite_existing=bool(data.get("overwrite_existing", False)),
        dry_run=bool(data.get("dry_run", True)),
        jellyfin=JellyfinConfig(
            base_url=jellyfin.get("base_url"),
            api_key=jellyfin.get("api_key"),
            refresh_enabled=bool(jellyfin.get("refresh_enabled", False)),
            library_ids=list(jellyfin.get("library_ids", [])),
        ),
        llm=LLMConfig(enabled=bool(llm.get("enabled", False)), provider=llm.get("provider", "hermes")),
    )
