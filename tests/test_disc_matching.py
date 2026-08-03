from disc_steward.disc_matching import ContentCandidate, FileAssignment, match_ordered_candidates, validate_assignments
from disc_steward.media_evidence import detect_aggregate_relations
from disc_steward.models import ScannedFile


def _file(name: str, duration: float) -> ScannedFile:
    return ScannedFile(
        path=f"/media/{name}.mkv",
        filename=f"{name}.mkv",
        parent_disc_folder="/media",
        size_bytes=1,
        modified_time=0,
        duration_seconds=duration,
        container_format="matroska,webm",
    )


def test_matching_excludes_aggregate_duplicate_and_matches_components_once():
    files = [
        (1, _file("aggregate", 8532.1)),
        (2, _file("episode-1", 1420.0)),
        (3, _file("episode-2", 1423.0)),
        (4, _file("episode-3", 1422.0)),
        (5, _file("episode-4", 1423.0)),
        (6, _file("episode-5", 1422.0)),
        (7, _file("episode-6", 1422.0)),
    ]
    candidates = [
        ContentCandidate(f"e{i}", f"Episode {i}", "episode", duration_seconds=1422, season_number=1, episode_number=i)
        for i in range(1, 7)
    ]
    relations = detect_aggregate_relations(files)

    result = match_ordered_candidates(files, candidates, aggregate_relations=relations)

    assert result.assignments[0].relation == "aggregate_duplicate_of_components"
    assert result.assignments[0].candidate_id is None
    assert [item.candidate_id for item in result.assignments[1:]] == [f"e{i}" for i in range(1, 7)]
    assert result.unmatched_candidate_ids == []
    assert any("overlaps 6" in warning for warning in result.warnings)


def test_matching_leaves_unresolved_files_unmatched():
    files = [(1, _file("unknown", 300))]
    candidates = [ContentCandidate("episode-1", "Episode 1", "episode", duration_seconds=1400)]

    result = match_ordered_candidates(files, candidates)

    assert result.assignments[0].relation == "unmatched_file"
    assert result.assignments[0].candidate_id is None
    assert result.unmatched_candidate_ids == ["episode-1"]


def test_global_validator_reports_unmatched_and_duplicate_assignments():
    candidates = [
        ContentCandidate("e1", "Episode 1", "episode"),
        ContentCandidate("e2", "Episode 2", "episode"),
    ]
    assignments = [
        FileAssignment(1, "e1", "direct", 0.9),
        FileAssignment(2, "e1", "direct", 0.8),
    ]

    conflicts = validate_assignments(assignments, candidates)

    assert any("multiple files" in conflict for conflict in conflicts)
    assert any("unmatched episode" in conflict for conflict in conflicts)
