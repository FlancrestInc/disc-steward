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
    refresh_after_import: bool = False
    library_ids: list[str] = field(default_factory=list)


@dataclass
class LLMConfig:
    enabled: bool = False
    provider: str = "hermes"
    endpoint: str = ""
    max_items_per_request: int = 10
    max_chars_per_field: int = 500
    allow_full_subtitle_text: bool = False
    allow_shell_commands: bool = False


@dataclass
class MetadataProviderConfig:
    enabled: bool = False
    api_key: str = ""


@dataclass
class MetadataConfig:
    enabled: bool = False
    providers: dict[str, MetadataProviderConfig] = field(
        default_factory=lambda: {
            "tmdb": MetadataProviderConfig(),
            "tvdb": MetadataProviderConfig(),
            "anilist": MetadataProviderConfig(),
            "anidb": MetadataProviderConfig(),
            "mal": MetadataProviderConfig(),
        }
    )


@dataclass
class SubtitlePlanningConfig:
    preferred_format: str = "srt"
    preserve_original_subtitles: bool = True
    convert_image_subtitles_to_srt: bool = False
    preserve_ass_for_anime: bool = True
    add_srt_fallback_for_ass: bool = True


@dataclass
class JapaneseAnimeConfig:
    preserve_unicode_metadata: bool = True
    english_filename_with_original_metadata: bool = True
    allow_original_title_in_filename: bool = False
    preserve_ass_by_default: bool = True
    auto_translate_metadata: bool = False


@dataclass
class CleanupConfig:
    enabled: bool = False
    dry_run: bool = True
    raw_rip_retention_days_after_import: int = 14
    working_file_retention_days_after_import: int = 7
    delete_raw_rips: bool = False
    delete_working_files: bool = False
    archive_raw_rips_to_eddy: bool = False
    raw_rip_archive_path: str = ""
    require_successful_jellyfin_import: bool = True


@dataclass
class JellyfinLogsConfig:
    enabled: bool = False
    log_path: str = ""
    scan_recent_days: int = 7


@dataclass
class PathMapping:
    controller_path: Path
    barnabas_path: Path | None = None
    eddy_path: Path | None = None

    def native_path_for(self, machine: str) -> Path | None:
        if machine == "barnabas":
            return self.barnabas_path
        if machine == "eddy":
            return self.eddy_path
        raise ValueError(f"Unknown path mapping machine: {machine}")


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
    duration_tolerance_percent: float = 1.0
    validation_require_aac_fallback: bool = True
    validation_require_no_default_image_subtitles: bool = True
    validation_allow_manual_acceptance: bool = True
    cleanup_enabled: bool = False
    raw_rip_retention_days: int = 30
    working_file_retention_days: int = 14
    overwrite_existing: bool = False
    transfer_verify: str = "size"
    create_final_directories: bool = True
    rsync_target: str | None = None
    ssh_options: list[str] = field(default_factory=list)
    dry_run: bool = True
    jellyfin: JellyfinConfig = field(default_factory=JellyfinConfig)
    llm: LLMConfig = field(default_factory=LLMConfig)
    metadata: MetadataConfig = field(default_factory=MetadataConfig)
    subtitle_planning: SubtitlePlanningConfig = field(default_factory=SubtitlePlanningConfig)
    japanese_anime: JapaneseAnimeConfig = field(default_factory=JapaneseAnimeConfig)
    cleanup: CleanupConfig = field(default_factory=CleanupConfig)
    jellyfin_logs: JellyfinLogsConfig = field(default_factory=JellyfinLogsConfig)
    path_mappings: dict[str, list[PathMapping]] = field(default_factory=dict)

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

    @staticmethod
    def path_mappings_for(
        barnabas: list[tuple[Path, Path]] | None = None,
        eddy: list[tuple[Path, Path]] | None = None,
    ) -> dict[str, list[PathMapping]]:
        return {
            "barnabas": [PathMapping(_path(controller), barnabas_path=_path(native)) for controller, native in (barnabas or [])],
            "eddy": [PathMapping(_path(controller), eddy_path=_path(native)) for controller, native in (eddy or [])],
        }

    def to_barnabas_path(self, path: str | Path) -> Path:
        return self._controller_to_native(path, "barnabas")

    def to_eddy_path(self, path: str | Path) -> Path:
        return self._controller_to_native(path, "eddy")

    def to_controller_path(self, path: str | Path, machine: str) -> Path:
        source = _path(path)
        for mapping in _longest_mappings(self.path_mappings.get(machine, []), machine, native=True):
            native_root = mapping.native_path_for(machine)
            if native_root is None:
                continue
            translated = _replace_prefix(source, native_root, mapping.controller_path)
            if translated is not None:
                return translated
        return source

    def mount_unavailable_for(self, path: str | Path) -> Path | None:
        controller_path = _path(path)
        for mappings in self.path_mappings.values():
            for mapping in _longest_mappings(mappings):
                if _replace_prefix(controller_path, mapping.controller_path, mapping.controller_path) is not None:
                    return None if mapping.controller_path.exists() else mapping.controller_path
        return None

    def _controller_to_native(self, path: str | Path, machine: str) -> Path:
        source = _path(path)
        for mapping in _longest_mappings(self.path_mappings.get(machine, [])):
            native_root = mapping.native_path_for(machine)
            if native_root is None:
                continue
            translated = _replace_prefix(source, mapping.controller_path, native_root)
            if translated is not None:
                return translated
        return source


def _path(value: str | Path) -> Path:
    return Path(value).expanduser()


def _replace_prefix(path: Path, old_root: Path, new_root: Path) -> Path | None:
    try:
        return new_root / path.relative_to(old_root)
    except ValueError:
        return None


def _longest_mappings(mappings: list[PathMapping], machine: str | None = None, native: bool = False) -> list[PathMapping]:
    def key(mapping: PathMapping) -> int:
        root = mapping.native_path_for(machine or "") if native and machine else mapping.controller_path
        return len(root.parts) if root else 0

    return sorted(mappings, key=key, reverse=True)


def load_config(path: str | Path) -> AppConfig:
    text = Path(path).read_text()
    data = yaml.safe_load(text) if yaml is not None else _parse_simple_yaml(text)
    data = data or {}
    if not isinstance(data, dict):
        raise ValueError("Config file must contain a YAML mapping")
    return config_from_dict(data)


def _parse_simple_yaml(text: str) -> dict[str, Any]:
    root: dict[str, Any] = {}
    stack: list[tuple[int, Any]] = [(-1, root)]
    pending: list[tuple[int, dict[str, Any], str]] = []

    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.rstrip()
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        indent = len(line) - len(line.lstrip(" "))
        stripped = line.strip()

        while pending and indent <= pending[-1][0]:
            pending.pop()
        while len(stack) > 1 and indent <= stack[-1][0]:
            stack.pop()

        if pending and indent > pending[-1][0]:
            parent_indent, parent, key = pending.pop()
            container: dict[str, Any] | list[Any] = [] if stripped.startswith("- ") else {}
            parent[key] = container
            stack.append((parent_indent + 1, container))

        parent = stack[-1][1]
        if stripped.startswith("- "):
            if not isinstance(parent, list):
                raise ValueError(f"Unsupported YAML list item at line {line_number}")
            item = stripped[2:].strip()
            if not item:
                child: dict[str, Any] = {}
                parent.append(child)
                stack.append((indent, child))
            elif ":" in item:
                child = {}
                parent.append(child)
                _assign_yaml_key_value(child, item, indent, pending, line_number)
                stack.append((indent, child))
            else:
                parent.append(_parse_yaml_scalar(item))
            continue

        if not isinstance(parent, dict):
            raise ValueError(f"Unsupported YAML mapping entry at line {line_number}")
        _assign_yaml_key_value(parent, stripped, indent, pending, line_number)

    return root


def _assign_yaml_key_value(
    parent: dict[str, Any],
    text: str,
    indent: int,
    pending: list[tuple[int, dict[str, Any], str]],
    line_number: int,
) -> None:
    key, separator, value = text.partition(":")
    if not separator:
        raise ValueError(f"Unsupported YAML line {line_number}: missing ':'")
    key = key.strip()
    value = value.strip()
    if not key:
        raise ValueError(f"Unsupported YAML line {line_number}: empty key")
    if value == "":
        parent[key] = None
        pending.append((indent, parent, key))
    else:
        parent[key] = _parse_yaml_scalar(value)


def _parse_yaml_scalar(value: str) -> Any:
    if value in {"", "null", "Null", "NULL", "~"}:
        return None
    if value in {"true", "True", "TRUE"}:
        return True
    if value in {"false", "False", "FALSE"}:
        return False
    if value in {"[]", "[ ]"}:
        return []
    if value in {"{}", "{ }"}:
        return {}
    if (value.startswith('"') and value.endswith('"')) or (value.startswith("'") and value.endswith("'")):
        return value[1:-1]
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        return value


def config_from_dict(data: dict[str, Any]) -> AppConfig:
    root = _path(data.get("pipeline_root", "/mnt/data2/media-pipeline"))
    paths = data.get("paths", {})
    eddy = data.get("eddy", {})
    validation = data.get("validation", {})
    transfer = data.get("transfer", {})
    cleanup = data.get("cleanup", {})
    defaults = AppConfig.default_for_root(root.parent)
    library_roots = {
        name: _path(value)
        for name, value in transfer.get(
            "eddy_final_roots",
            eddy.get(
                "library_roots",
                {
                    "Movies": "/mnt/jellyfin-media/Movies",
                    "Shows": "/mnt/jellyfin-media/Shows",
                    "Anime": "/mnt/jellyfin-media/Anime",
                    "Family Videos": "/mnt/jellyfin-media/Family Videos",
                },
            ),
        ).items()
    }
    jellyfin = data.get("jellyfin", {})
    llm = data.get("llm", {})
    metadata = data.get("metadata", {})
    subtitle_planning = data.get("subtitle_planning", {})
    japanese_anime = data.get("japanese_anime", {})
    jellyfin_logs = data.get("jellyfin_logs", {})
    metadata_providers = metadata.get("providers", {})
    path_mappings = _parse_path_mappings(data.get("path_mappings", {}))
    cleanup_config = CleanupConfig(
        enabled=bool(cleanup.get("enabled", data.get("cleanup_enabled", False))),
        dry_run=bool(cleanup.get("dry_run", data.get("dry_run", True))),
        raw_rip_retention_days_after_import=int(
            cleanup.get("raw_rip_retention_days_after_import", data.get("raw_rip_retention_days", 14))
        ),
        working_file_retention_days_after_import=int(
            cleanup.get("working_file_retention_days_after_import", data.get("working_file_retention_days", 7))
        ),
        delete_raw_rips=bool(cleanup.get("delete_raw_rips", False)),
        delete_working_files=bool(cleanup.get("delete_working_files", False)),
        archive_raw_rips_to_eddy=bool(cleanup.get("archive_raw_rips_to_eddy", False)),
        raw_rip_archive_path=str(cleanup.get("raw_rip_archive_path", "") or ""),
        require_successful_jellyfin_import=bool(cleanup.get("require_successful_jellyfin_import", True)),
    )
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
        eddy_incoming_path=_path(transfer.get("eddy_incoming_root", eddy.get("incoming_path", "/mnt/jellyfin-media/.incoming"))),
        eddy_library_roots=library_roots,
        transfer_method=transfer.get("method", data.get("transfer_method", "local_mount")),
        rsync_destination=transfer.get("rsync_target", data.get("rsync_destination")),
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
        duration_tolerance_seconds=int(validation.get("duration_tolerance_seconds", data.get("duration_tolerance_seconds", 5))),
        duration_tolerance_percent=float(validation.get("duration_tolerance_percent", 1.0)),
        validation_require_aac_fallback=bool(validation.get("require_aac_fallback", True)),
        validation_require_no_default_image_subtitles=bool(validation.get("require_no_default_image_subtitles", True)),
        validation_allow_manual_acceptance=bool(validation.get("allow_manual_acceptance", True)),
        cleanup_enabled=cleanup_config.enabled,
        raw_rip_retention_days=cleanup_config.raw_rip_retention_days_after_import,
        working_file_retention_days=cleanup_config.working_file_retention_days_after_import,
        overwrite_existing=bool(transfer.get("allow_overwrite", data.get("overwrite_existing", False))),
        transfer_verify=transfer.get("verify", "size"),
        create_final_directories=bool(transfer.get("create_final_directories", True)),
        rsync_target=transfer.get("rsync_target", data.get("rsync_destination")),
        ssh_options=list(transfer.get("ssh_options", [])),
        dry_run=bool(data.get("dry_run", cleanup_config.dry_run)),
        jellyfin=JellyfinConfig(
            base_url=jellyfin.get("base_url"),
            api_key=jellyfin.get("api_key"),
            refresh_enabled=bool(jellyfin.get("enabled", jellyfin.get("refresh_enabled", False))),
            refresh_after_import=bool(jellyfin.get("refresh_after_import", False)),
            library_ids=list(jellyfin.get("library_ids", [])),
        ),
        llm=LLMConfig(
            enabled=bool(llm.get("enabled", False)),
            provider=llm.get("provider", "hermes"),
            endpoint=llm.get("endpoint", ""),
            max_items_per_request=int(llm.get("max_items_per_request", 10)),
            max_chars_per_field=int(llm.get("max_chars_per_field", 500)),
            allow_full_subtitle_text=bool(llm.get("allow_full_subtitle_text", False)),
            allow_shell_commands=bool(llm.get("allow_shell_commands", False)),
        ),
        metadata=MetadataConfig(
            enabled=bool(metadata.get("enabled", False)),
            providers={
                name: MetadataProviderConfig(
                    enabled=bool(metadata_providers.get(name, {}).get("enabled", False)),
                    api_key=metadata_providers.get(name, {}).get("api_key", ""),
                )
                for name in ["tmdb", "tvdb", "anilist", "anidb", "mal"]
            },
        ),
        subtitle_planning=SubtitlePlanningConfig(
            preferred_format=subtitle_planning.get("preferred_format", data.get("preferred_subtitle_format", "srt")),
            preserve_original_subtitles=bool(subtitle_planning.get("preserve_original_subtitles", True)),
            convert_image_subtitles_to_srt=bool(subtitle_planning.get("convert_image_subtitles_to_srt", False)),
            preserve_ass_for_anime=bool(subtitle_planning.get("preserve_ass_for_anime", True)),
            add_srt_fallback_for_ass=bool(subtitle_planning.get("add_srt_fallback_for_ass", True)),
        ),
        japanese_anime=JapaneseAnimeConfig(
            preserve_unicode_metadata=bool(japanese_anime.get("preserve_unicode_metadata", True)),
            english_filename_with_original_metadata=bool(japanese_anime.get("english_filename_with_original_metadata", True)),
            allow_original_title_in_filename=bool(japanese_anime.get("allow_original_title_in_filename", False)),
            preserve_ass_by_default=bool(japanese_anime.get("preserve_ass_by_default", True)),
            auto_translate_metadata=bool(japanese_anime.get("auto_translate_metadata", False)),
        ),
        cleanup=cleanup_config,
        jellyfin_logs=JellyfinLogsConfig(
            enabled=bool(jellyfin_logs.get("enabled", False)),
            log_path=jellyfin_logs.get("log_path", ""),
            scan_recent_days=int(jellyfin_logs.get("scan_recent_days", 7)),
        ),
        path_mappings=path_mappings,
    )


def _parse_path_mappings(raw: dict[str, Any]) -> dict[str, list[PathMapping]]:
    parsed: dict[str, list[PathMapping]] = {"barnabas": [], "eddy": []}
    for machine in parsed:
        entries = raw.get(machine, [])
        if isinstance(entries, dict):
            entries = [entries]
        for entry in entries or []:
            controller = entry.get("controller_path")
            native = entry.get(f"{machine}_path", entry.get("native_path"))
            if not controller or not native:
                continue
            parsed[machine].append(
                PathMapping(
                    controller_path=_path(controller),
                    barnabas_path=_path(native) if machine == "barnabas" else None,
                    eddy_path=_path(native) if machine == "eddy" else None,
                )
            )
    return parsed
