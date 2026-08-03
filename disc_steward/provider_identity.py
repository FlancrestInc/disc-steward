from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Any, Iterable
from urllib.parse import urlparse


_PROVIDER_PATTERNS = {
    "imdb": re.compile(r"^tt\d{7,9}$", re.IGNORECASE),
    "tmdb": re.compile(r"^\d{1,10}$"),
    "tvdb": re.compile(r"^\d{1,10}$"),
    "anidb": re.compile(r"^\d{1,10}$"),
    "anilist": re.compile(r"^\d{1,10}$"),
    "mal": re.compile(r"^\d{1,10}$"),
}
_PROVIDER_DOMAINS = {
    "imdb": {"imdb.com", "www.imdb.com"},
    "tmdb": {"themoviedb.org", "www.themoviedb.org"},
    "tvdb": {"thetvdb.com", "www.thetvdb.com"},
    "anidb": {"anidb.net", "www.anidb.net"},
    "anilist": {"anilist.co", "www.anilist.co"},
    "mal": {"myanimelist.net", "www.myanimelist.net"},
}


@dataclass(frozen=True)
class VerifiedProviderId:
    provider: str
    provider_id: str
    provider_url: str
    evidence_url: str
    confidence: float = 1.0
    verification: str = "syntax_and_source"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def verify_provider_ids(candidate: dict[str, Any]) -> tuple[list[VerifiedProviderId], list[str]]:
    """Accept only syntactically valid IDs backed by a matching provider URL."""
    verified: list[VerifiedProviderId] = []
    warnings: list[str] = []
    evidence_url = str(candidate.get("provider_url") or candidate.get("source_url") or "")
    for provider, pattern in _PROVIDER_PATTERNS.items():
        raw = candidate.get(f"{provider}_id")
        if raw in (None, ""):
            continue
        value = str(raw).strip()
        if not pattern.fullmatch(value):
            warnings.append(f"rejected malformed {provider} ID")
            continue
        provider_url = str(candidate.get("provider_url") or "")
        domain = (urlparse(provider_url).hostname or "").lower()
        if domain not in _PROVIDER_DOMAINS[provider]:
            warnings.append(f"rejected {provider} ID without matching provider URL")
            continue
        verified.append(VerifiedProviderId(provider, value, provider_url, evidence_url, float(candidate.get("confidence") or 0.0)))
    return verified, warnings


def verify_provider_id_set(candidates: Iterable[dict[str, Any]]) -> tuple[list[VerifiedProviderId], list[str]]:
    verified: list[VerifiedProviderId] = []
    warnings: list[str] = []
    seen: set[tuple[str, str]] = set()
    for candidate in candidates:
        candidate_verified, candidate_warnings = verify_provider_ids(candidate)
        for item in candidate_verified:
            key = (item.provider, item.provider_id)
            if key not in seen:
                seen.add(key)
                verified.append(item)
        warnings.extend(candidate_warnings)
    return verified, warnings
