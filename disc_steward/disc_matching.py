from __future__ import annotations

from dataclasses import dataclass, field

from .media_evidence import AggregateRelation
from .models import ScannedFile


@dataclass(frozen=True)
class ContentCandidate:
    candidate_id: str
    title: str
    kind: str
    duration_seconds: float | None = None
    season_number: int | None = None
    episode_number: int | None = None
    extra_type: str | None = None
    source_url: str | None = None


@dataclass(frozen=True)
class FileAssignment:
    source_file_id: int
    candidate_id: str | None
    relation: str
    score: float
    reasons: tuple[str, ...] = ()


@dataclass
class DiscMatchResult:
    assignments: list[FileAssignment] = field(default_factory=list)
    aggregate_relations: list[AggregateRelation] = field(default_factory=list)
    unmatched_candidate_ids: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def match_ordered_candidates(
    files: list[tuple[int, ScannedFile]],
    candidates: list[ContentCandidate],
    *,
    aggregate_relations: list[AggregateRelation] | None = None,
    minimum_duration_score: float = 0.45,
) -> DiscMatchResult:
    """Return conservative file-to-candidate recommendations.

    This is intentionally a recommendation layer. It does not write review rows,
    rename files, or force every file/candidate into a match. Aggregate relations
    suppress duplicate assignment of the aggregate title while retaining the
    component files as independently matchable representations.
    """
    relations = aggregate_relations or []
    aggregate_by_id = {relation.aggregate_file_id: relation for relation in relations}
    component_to_aggregate = {
        component_id: relation.aggregate_file_id
        for relation in relations
        for component_id in relation.component_file_ids
    }
    available = {candidate.candidate_id: candidate for candidate in candidates}
    assignments: list[FileAssignment] = []
    warnings: list[str] = []

    for source_file_id, scanned in files:
        relation = aggregate_by_id.get(source_file_id)
        if relation is not None:
            assignments.append(
                FileAssignment(
                    source_file_id,
                    None,
                    "aggregate_duplicate_of_components",
                    relation.confidence,
                    (
                        f"duration matches component files {', '.join(str(item) for item in relation.component_file_ids)}",
                        "do not import as an additional independent item",
                    ),
                )
            )
            continue

        candidates_left = [
            candidate
            for candidate in available.values()
            if candidate.candidate_id not in {item.candidate_id for item in assignments if item.candidate_id}
        ]
        if not candidates_left:
            assignments.append(FileAssignment(source_file_id, None, "unmatched_file", 0.0, ("no candidates remain",)))
            continue

        best = _best_candidate(scanned, candidates_left)
        if best is None or best[1] < minimum_duration_score:
            assignments.append(
                FileAssignment(
                    source_file_id,
                    None,
                    "unmatched_file",
                    best[1] if best else 0.0,
                    ("no candidate met the duration threshold",),
                )
            )
            continue

        candidate, score = best
        reasons = [f"duration similarity {score:.3f}"]
        if source_file_id in component_to_aggregate:
            reasons.append(f"component of aggregate file {component_to_aggregate[source_file_id]}")
        assignments.append(FileAssignment(source_file_id, candidate.candidate_id, "direct", score, tuple(reasons)))

    assigned_ids = {assignment.candidate_id for assignment in assignments if assignment.candidate_id}
    unmatched = [candidate.candidate_id for candidate in candidates if candidate.candidate_id not in assigned_ids]
    for relation in relations:
        if not any(item.source_file_id in relation.component_file_ids for item in assignments):
            warnings.append(
                f"aggregate file {relation.aggregate_file_id} has no matched component files"
            )
        if len(relation.component_file_ids) > 1:
            warnings.append(
                f"aggregate file {relation.aggregate_file_id} overlaps {len(relation.component_file_ids)} component files"
            )
    return DiscMatchResult(assignments, relations, unmatched, warnings)


def validate_assignments(
    assignments: list[FileAssignment],
    candidates: list[ContentCandidate],
    *,
    aggregate_relations: list[AggregateRelation] | None = None,
) -> list[str]:
    """Return global consistency conflicts without changing recommendations."""
    conflicts: list[str] = []
    candidate_by_id = {candidate.candidate_id: candidate for candidate in candidates}
    seen: dict[str, list[int]] = {}
    for assignment in assignments:
        if assignment.candidate_id:
            seen.setdefault(assignment.candidate_id, []).append(assignment.source_file_id)
    for candidate_id, source_ids in seen.items():
        if len(source_ids) > 1:
            conflicts.append(f"candidate {candidate_id} assigned to multiple files: {source_ids}")
    main_features = [assignment for assignment in assignments if assignment.relation == "direct" and candidate_by_id.get(assignment.candidate_id or "", None) and candidate_by_id[assignment.candidate_id].kind == "main_feature"]
    if len(main_features) > 1:
        conflicts.append("multiple main-feature candidates assigned")
    if any(candidate.kind == "episode" for candidate in candidates):
        unmatched_episodes = [candidate.candidate_id for candidate in candidates if candidate.kind == "episode" and candidate.candidate_id not in seen]
        if unmatched_episodes:
            conflicts.append(f"unmatched episode candidates: {unmatched_episodes}")
    relation_by_aggregate = {relation.aggregate_file_id: relation for relation in (aggregate_relations or [])}
    for assignment in assignments:
        if assignment.source_file_id in relation_by_aggregate and assignment.candidate_id:
            conflicts.append(f"aggregate file {assignment.source_file_id} received an independent candidate assignment")
    return conflicts


def _best_candidate(
    scanned: ScannedFile,
    candidates: list[ContentCandidate],
) -> tuple[ContentCandidate, float] | None:
    if not candidates:
        return None
    actual = scanned.duration_seconds
    if actual is None or actual <= 0:
        ordered = sorted(candidates, key=_candidate_order)
        return ordered[0], 0.5

    scored = []
    for candidate in candidates:
        if candidate.duration_seconds is None or candidate.duration_seconds <= 0:
            score = 0.5
        else:
            delta = abs(actual - candidate.duration_seconds)
            score = max(0.0, 1.0 - delta / max(actual, candidate.duration_seconds))
        scored.append((score, _candidate_order(candidate), candidate))
    score, _, candidate = sorted(scored, key=lambda item: (-item[0], item[1]))[0]
    return candidate, score


def _candidate_order(candidate: ContentCandidate) -> tuple[int, int, str]:
    return (
        candidate.season_number if candidate.season_number is not None else 9999,
        candidate.episode_number if candidate.episode_number is not None else 9999,
        candidate.candidate_id,
    )
