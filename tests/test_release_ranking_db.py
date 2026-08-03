from pathlib import Path

from disc_steward.db import Database


def test_release_ranking_is_upserted_and_read_back(tmp_path: Path):
    db = Database(tmp_path / "disc_steward.sqlite3")
    db.initialize()
    job_id = db.upsert_job(tmp_path / "disc")

    db.save_release_ranking(job_id, {"matches": [{"release_key": "one", "score": 0.4}]})
    db.save_release_ranking(job_id, {"matches": [{"release_key": "two", "score": 0.9}]})

    ranking = db.get_release_ranking(job_id)
    assert ranking is not None
    assert ranking["matches"][0]["release_key"] == "two"
    assert ranking["matches"][0]["score"] == 0.9
