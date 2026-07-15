from __future__ import annotations

import logging
from urllib.parse import quote
from urllib.request import Request, urlopen

from .config import AppConfig

LOG = logging.getLogger(__name__)


def send_notification(
    config: AppConfig,
    title: str,
    message: str,
    *,
    priority: str = "default",
    tags: list[str] | None = None,
) -> bool:
    notifications = config.notifications
    if not notifications.enabled:
        return False
    if (notifications.provider or "ntfy").strip().lower() != "ntfy":
        return False
    if not notifications.ntfy_url.strip() or not notifications.ntfy_topic.strip():
        return False

    endpoint = f"{notifications.ntfy_url.rstrip('/')}/{quote(notifications.ntfy_topic.strip(), safe='')}"
    headers = {
        "User-Agent": "disc-steward/1.0",
        "Content-Type": "text/plain; charset=utf-8",
        "Title": title,
        "Priority": priority,
    }
    if tags:
        headers["Tags"] = ",".join(tags)
    token = notifications.ntfy_token.strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"

    request = Request(endpoint, data=message.encode("utf-8"), headers=headers, method="POST")
    try:
        with urlopen(request, timeout=10) as response:
            if 200 <= getattr(response, "status", 200) < 300:
                return True
            LOG.warning("ntfy notification returned unexpected status: %s", getattr(response, "status", "?"))
            return False
    except Exception as exc:  # pragma: no cover - depends on server/network conditions
        LOG.warning("ntfy notification failed: %s", exc)
        return False
