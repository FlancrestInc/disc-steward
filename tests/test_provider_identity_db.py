from pathlib import Path

from disc_steward.db import Database


def test_verified_provider_ids_are_upserted_separately_from_review(tmp_path: Path):
    db = Database(tmp_path / "disc_steward.sqlite3")
    db.initialize()
    job_id = db.upsert_job(tmp_path / "disc")
    identity = {
        "provider": "tmdb",
        "provider_id": "123",
        "provider_url": "https://www.themoviedb.org/movie/123",
        "evidence_url": "https://example.test/release",
        "confidence": 0.9,
        "verification": "syntax_and_source",
    }

    db.save_verified_provider_ids(job_id, [identity])
    db.save_verified_provider_ids(job_id, [{**identity, "confidence": 1.0}])

    rows = db.list_verified_provider_ids(job_id)
    assert len(rows) == 1
    assert rows[0]["provider_id"] == "123"
    assert rows[0]["confidence"] == 1.0
