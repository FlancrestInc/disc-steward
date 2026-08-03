from disc_steward.calibration_eval import build_blind_prediction_report, evaluate_assignments, evaluate_blind_safety


def test_labeled_evaluation_counts_accuracy_and_duplicates():
    predicted = [
        {"source_file_id": 1, "candidate_id": "e1", "relation": "direct"},
        {"source_file_id": 2, "candidate_id": "e1", "relation": "direct"},
        {"source_file_id": 3, "candidate_id": None, "relation": "unmatched_file"},
    ]

    result = evaluate_assignments(55, predicted, expected={1: "e1", 2: "e2", 3: None})

    assert result.correct_assignments == 2
    assert result.accuracy == 2 / 3
    assert result.duplicate_candidate_assignments == 1
    assert result.unmatched_files == 1


def test_blind_evaluation_does_not_report_label_accuracy():
    result = evaluate_blind_safety(
        112,
        [{"source_file_id": 1, "candidate_id": None, "relation": "unmatched_file"}],
    )

    assert result.labeled is False
    assert result.accuracy is None


def test_blind_prediction_report_forces_undisclosed_safety_fields():
    report = build_blind_prediction_report({
        35: [{"source_file_id": 1, "candidate_id": None, "relation": "unmatched_file"}],
        112: [],
    })

    assert report["ground_truth_included"] is False
    assert report["identities_included"] is False
    assert [job["job_id"] for job in report["jobs"]] == [35, 112]
    assert all(job["blind_safety"]["accuracy"] is None for job in report["jobs"])
