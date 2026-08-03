from pathlib import Path

from disc_steward.db import Database


def test_research_packet_is_upserted_and_read_back(tmp_path: Path):
    db = Database(tmp_path / "disc_steward.sqlite3")
    db.initialize()
    job_id = db.upsert_job(tmp_path / "disc")
    packet = {"status": "partial", "queries": [{"query": "disc DVD extras"}], "sources": [], "facts": []}

    db.save_research_packet(job_id, packet)
    packet["status"] = "completed"
    db.save_research_packet(job_id, packet)

    restored = db.get_research_packet(job_id)
    assert restored is not None
    assert restored["status"] == "completed"
    assert restored["queries"][0]["query"] == "disc DVD extras"
