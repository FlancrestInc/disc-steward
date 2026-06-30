from __future__ import annotations

from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin
from urllib.request import Request, urlopen

from .config import JellyfinConfig


def trigger_library_scan(config: JellyfinConfig) -> bool:
    if not config.base_url or not config.api_key or not config.refresh_enabled:
        return False
    return True


def refresh_after_import(
    db,
    job_id: int,
    config: JellyfinConfig,
    post: Callable[[str, dict[str, str]], tuple[int, str]] | None = None,
) -> dict:
    if not config.base_url or not config.api_key or not config.refresh_enabled:
        result = {"status": "skipped", "reason": "jellyfin refresh disabled"}
        db.save_jellyfin_refresh(job_id, "skipped", result)
        return result
    poster = post or _post
    headers = {"X-Emby-Token": config.api_key}
    try:
        responses = []
        if config.library_ids:
            for library_id in config.library_ids:
                url = urljoin(config.base_url.rstrip("/") + "/", f"Items/{library_id}/Refresh?Recursive=true")
                status, body = poster(url, headers)
                responses.append({"library_id": library_id, "status_code": status, "body": body})
        else:
            url = urljoin(config.base_url.rstrip("/") + "/", "Library/Refresh")
            status, body = poster(url, headers)
            responses.append({"status_code": status, "body": body})
        result = {"status": "triggered", "responses": responses}
        db.save_jellyfin_refresh(job_id, "triggered", result)
        db.audit("jellyfin_refresh_triggered", "Triggered Jellyfin library refresh", job_id, result)
        return result
    except (HTTPError, URLError, OSError) as exc:
        result = {"status": "warning", "error": str(exc)}
        db.save_jellyfin_refresh(job_id, "warning", result)
        db.audit("jellyfin_refresh_warning", "Jellyfin refresh failed after import", job_id, result)
        return result


def _post(url: str, headers: dict[str, str]) -> tuple[int, str]:
    request = Request(url, method="POST", headers=headers)
    with urlopen(request, timeout=20) as response:  # noqa: S310 - configured local homelab URL
        return response.status, response.read().decode("utf-8", errors="replace")
