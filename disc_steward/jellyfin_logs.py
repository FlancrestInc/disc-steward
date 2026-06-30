from __future__ import annotations

from .config import JellyfinLogsConfig


def scan_recent_transcode_logs(config: JellyfinLogsConfig) -> list[dict]:
    """Future scaffold for Jellyfin playback/transcode clues.

    Phase 4 intentionally does not parse or act on Jellyfin logs. Later versions
    can detect transcode reasons here and store findings in
    jellyfin_transcode_findings for review-driven reprocessing.
    """
    if not config.enabled:
        return []
    if not config.log_path:
        return []
    return []
