from __future__ import annotations

from dataclasses import dataclass, field

from .config import MetadataConfig


@dataclass
class MetadataCandidate:
    provider: str
    title: str
    year: int | None = None
    original_title: str | None = None
    japanese_title: str | None = None
    romanized_title: str | None = None
    episode_titles: list[str] = field(default_factory=list)
    extras: list[str] = field(default_factory=list)
    confidence: float = 0.0
    provider_id: str | None = None


class MetadataProvider:
    name = "manual"

    def configured(self) -> bool:
        return False

    def lookup(self, query: str, year: int | None = None) -> list[MetadataCandidate]:
        return []


class ManualImdbProvider(MetadataProvider):
    name = "imdb_manual"

    def configured(self) -> bool:
        return True


def metadata_provider_status(config: MetadataConfig) -> dict:
    return {
        "enabled": config.enabled,
        "providers": {
            name: {
                "enabled": provider.enabled,
                "configured": _configured(name, provider.enabled, provider.api_key),
            }
            for name, provider in config.providers.items()
        },
    }


def _configured(name: str, enabled: bool, api_key: str) -> bool:
    if not enabled:
        return False
    if name in {"tmdb", "tvdb", "anidb", "mal"}:
        return bool(api_key)
    if name == "anilist":
        return True
    return False
