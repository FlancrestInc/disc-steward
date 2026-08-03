from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable


@dataclass(frozen=True)
class EvaluationResult:
    job_id: int
    labeled: bool
    total_files: int
    expected_assignments: int
    predicted_assignments: int
    correct_assignments: int
    accuracy: float | None
    unmatched_files: int
    duplicate_candidate_assignments: int
    aggregate_assignments: int
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "labeled": self.labeled,
            "total_files": self.total_files,
            "expected_assignments": self.expected_assignments,
            "predicted_assignments": self.predicted_assignments,
            "correct_assignments": self.correct_assignments,
            "accuracy": self.accuracy,
            "unmatched_files": self.unmatched_files,
            "duplicate_candidate_assignments": self.duplicate_candidate_assignments,
            "aggregate_assignments": self.aggregate_assignments,
            "warnings": list(self.warnings),
        }


def evaluate_assignments(
    job_id: int,
    predicted: Iterable[dict[str, Any]],
    *,
    expected: dict[int, str | None] | None = None,
) -> EvaluationResult:
    """Evaluate conservative assignments without mutating review/database state."""
    rows = list(predicted)
    expected = expected or {}
    predicted_by_file = {int(row["source_file_id"]): row.get("candidate_id") for row in rows}
    correct = sum(1 for file_id, candidate_id in expected.items() if predicted_by_file.get(file_id) == candidate_id)
    assigned_ids = [candidate_id for candidate_id in predicted_by_file.values() if candidate_id]
    duplicates = len(assigned_ids) - len(set(assigned_ids))
    aggregate_count = sum(1 for row in rows if row.get("relation") == "aggregate_duplicate_of_components")
    unmatched_count = sum(1 for row in rows if row.get("relation") == "unmatched_file")
    warnings: list[str] = []
    if duplicates:
        warnings.append("duplicate candidate assignments detected")
    if expected and len(expected) != len(rows):
        warnings.append("expected mapping does not cover every predicted file")
    return EvaluationResult(
        job_id=job_id,
        labeled=bool(expected),
        total_files=len(rows),
        expected_assignments=sum(1 for value in expected.values() if value),
        predicted_assignments=sum(1 for value in predicted_by_file.values() if value),
        correct_assignments=correct,
        accuracy=(correct / len(expected)) if expected else None,
        unmatched_files=unmatched_count,
        duplicate_candidate_assignments=duplicates,
        aggregate_assignments=aggregate_count,
        warnings=warnings,
    )


def build_blind_prediction_report(predictions_by_job: dict[int, Iterable[dict[str, Any]]]) -> dict[str, Any]:
    """Build a holdout report that cannot claim labeled accuracy."""
    jobs = []
    for job_id, predictions in sorted(predictions_by_job.items()):
        rows = list(predictions)
        safety = evaluate_blind_safety(job_id, rows).to_dict()
        safety["labeled"] = False
        safety["accuracy"] = None
        safety["expected_assignments"] = 0
        jobs.append({"job_id": job_id, "predictions": rows, "blind_safety": safety})
    return {
        "artifact_type": "blind_holdout_predictions",
        "ground_truth_included": False,
        "identities_included": False,
        "jobs": jobs,
    }


def evaluate_blind_safety(job_id: int, predicted: Iterable[dict[str, Any]]) -> EvaluationResult:
    """Evaluate only non-label safety properties for a still-undisclosed holdout."""
    return evaluate_assignments(job_id, predicted, expected=None)
