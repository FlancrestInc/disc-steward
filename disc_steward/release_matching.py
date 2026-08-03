from __future__ import annotations

from dataclasses import dataclass, field

from .models import ScannedFile
from .release_inventory import ReleaseInventory


@dataclass(frozen=True)
class ReleaseFit:
    release_key: str
    title: str
    score: float
    confidence: str
    components: dict[str, float]
    warnings: tuple[str, ...] = ()


@dataclass
class ReleaseRanking:
    matches: list[ReleaseFit] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def rank_release_inventories(
    files: list[ScannedFile],
    inventories: list[ReleaseInventory],
    *,
    duration_tolerance_seconds: float = 180.0,
    tie_margin: float = 0.05,
) -> ReleaseRanking:
    """Rank cited inventories against a scanned file vector without assigning labels."""
    if not inventories:
        return ReleaseRanking(warnings=["no release inventories supplied"])
    actual = [float(file.duration_seconds or 0) for file in files if (file.duration_seconds or 0) > 0]
    matches: list[ReleaseFit] = []
    for inventory in inventories:
        expected = [float(candidate.duration_seconds or 0) for candidate in inventory.candidates if (candidate.duration_seconds or 0) > 0]
        count_score = _similarity(len(actual), len(inventory.candidates))
        components = {"file_count": count_score}
        warnings = list(inventory.warnings)
        if expected and len(expected) == len(actual):
            duration_score = _duration_vector_score(actual, expected, duration_tolerance_seconds)
            components["duration_vector"] = duration_score
            score = count_score * 0.45 + duration_score * 0.55
        elif expected:
            components["duration_vector"] = 0.0
            score = count_score * 0.65
            warnings.append("release-specific duration vector length differs from scanned files")
        else:
            score = count_score
            warnings.append("inventory has no release-specific durations")
        score = max(0.0, min(1.0, score))
        confidence = "high_confidence" if score >= 0.85 else "plausible" if score >= 0.6 else "manual_review"
        matches.append(ReleaseFit(inventory.release_key, inventory.title, score, confidence, components, tuple(warnings)))
    matches.sort(key=lambda item: (-item.score, item.release_key))
    ranking_warnings: list[str] = []
    if len(matches) > 1 and matches[0].score - matches[1].score <= tie_margin:
        ranking_warnings.append("top release candidates are too close to select automatically")
        matches = [ReleaseFit(item.release_key, item.title, item.score, "manual_review" if index < 2 else item.confidence, item.components, item.warnings) for index, item in enumerate(matches)]
    if matches and matches[0].confidence == "manual_review":
        ranking_warnings.append("best release candidate does not meet the confidence threshold")
    return ReleaseRanking(matches, ranking_warnings)


def _similarity(actual: int, expected: int) -> float:
    if actual == expected:
        return 1.0
    if not actual and not expected:
        return 1.0
    return max(0.0, 1.0 - abs(actual - expected) / max(actual, expected, 1))


def _duration_vector_score(actual: list[float], expected: list[float], tolerance: float) -> float:
    if not actual or not expected or len(actual) != len(expected):
        return 0.0
    pairs = zip(sorted(actual), sorted(expected), strict=True)
    scores = [max(0.0, 1.0 - abs(left - right) / max(tolerance, left, right, 1.0)) for left, right in pairs]
    return sum(scores) / len(scores)
