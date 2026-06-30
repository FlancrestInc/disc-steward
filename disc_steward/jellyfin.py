from __future__ import annotations

from .config import JellyfinConfig


def trigger_library_scan(config: JellyfinConfig) -> bool:
    if not config.base_url or not config.api_key or not config.refresh_enabled:
        return False
    raise NotImplementedError("Jellyfin API triggering is scaffolded for a later phase")
