from __future__ import annotations

from dataclasses import dataclass, field

from .disc_matching import ContentCandidate
from .disc_research import DiscResearchFact, facts_to_content_candidates


@dataclass(frozen=True)
class InventorySource:
    url: str
    title: str
    retrieved_at: str | None = None


@dataclass
class ReleaseInventory:
    release_key: str
    title: str
    content_type: str
    candidates: list[ContentCandidate] = field(default_factory=list)
    sources: list[InventorySource] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def episode_inventory(
    *,
    release_key: str,
    title: str,
    episode_titles: list[str],
    source_url: str,
    source_title: str,
    content_type: str = "show",
    season_number: int = 1,
    warnings: list[str] | None = None,
) -> ReleaseInventory:
    """Create a cited, ordered episode inventory for later matching.

    Episode durations are intentionally absent until release-specific evidence
    supplies them. A generic episode list must not be mistaken for proof that a
    particular DVD contains every listed episode.
    """
    candidates = [
        ContentCandidate(
            candidate_id=f"{release_key}:s{season_number:02d}e{episode_number:02d}",
            title=episode_title,
            kind="episode",
            season_number=season_number,
            episode_number=episode_number,
            source_url=source_url,
        )
        for episode_number, episode_title in enumerate(episode_titles, start=1)
    ]
    return ReleaseInventory(
        release_key=release_key,
        title=title,
        content_type=content_type,
        candidates=candidates,
        sources=[InventorySource(source_url, source_title)],
        warnings=list(warnings or []),
    )


def extra_inventory(
    *,
    release_key: str,
    title: str,
    extras: list[tuple[str, str, str | None, float | None]],
    source_url: str,
    source_title: str,
    content_type: str = "extra",
    warnings: list[str] | None = None,
) -> ReleaseInventory:
    """Create a cited inventory of extras.

    Each tuple is ``(display_title, extra_type, stable_suffix, duration_seconds)``.
    Extra titles may be aggregate reels or compilations; the matcher still treats
    duration relationships as overlap evidence rather than a one-to-one guarantee.
    """
    candidates = [
        ContentCandidate(
            candidate_id=f"{release_key}:extra:{suffix if suffix is not None else f'{index:02d}'}",
            title=display_title,
            kind="extra",
            duration_seconds=duration,
            extra_type=extra_type,
            source_url=source_url,
        )
        for index, (display_title, extra_type, suffix, duration) in enumerate(extras, start=1)
    ]
    return ReleaseInventory(
        release_key=release_key,
        title=title,
        content_type=content_type,
        candidates=candidates,
        sources=[InventorySource(source_url, source_title)],
        warnings=list(warnings or []),
    )


def inventory_from_research_facts(
    *,
    release_key: str,
    title: str,
    facts: list[DiscResearchFact],
    warnings: list[str] | None = None,
) -> ReleaseInventory:
    """Build an advisory combined episode/extra inventory from cited facts."""
    candidates = facts_to_content_candidates(facts)
    source_by_id = {}
    for fact in facts:
        source_by_id.setdefault(fact.source_id, InventorySource(fact.source_url, fact.source_id))
    inventory_warnings = list(warnings or [])
    if not candidates:
        inventory_warnings.append("research returned no episode or extra candidates")
    return ReleaseInventory(
        release_key=release_key,
        title=title,
        content_type="research-derived",
        candidates=candidates,
        sources=list(source_by_id.values()),
        warnings=inventory_warnings,
    )


def combine_inventories(*inventories: ReleaseInventory) -> ReleaseInventory:
    if not inventories:
        raise ValueError("at least one inventory is required")
    first = inventories[0]
    return ReleaseInventory(
        release_key="+".join(item.release_key for item in inventories),
        title=first.title,
        content_type=first.content_type,
        candidates=[candidate for item in inventories for candidate in item.candidates],
        sources=[source for item in inventories for source in item.sources],
        warnings=[warning for item in inventories for warning in item.warnings],
    )


def with_durations(
    inventory: ReleaseInventory,
    durations_seconds: list[float],
) -> ReleaseInventory:
    """Return a copy with release-specific expected durations when documented."""
    if len(durations_seconds) != len(inventory.candidates):
        raise ValueError("duration count must equal inventory candidate count")
    inventory.candidates = [
        ContentCandidate(
            candidate_id=candidate.candidate_id,
            title=candidate.title,
            kind=candidate.kind,
            duration_seconds=duration,
            season_number=candidate.season_number,
            episode_number=candidate.episode_number,
            extra_type=candidate.extra_type,
            source_url=candidate.source_url,
        )
        for candidate, duration in zip(inventory.candidates, durations_seconds, strict=True)
    ]
    return inventory
