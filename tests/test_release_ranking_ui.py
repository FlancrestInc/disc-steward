from pathlib import Path

from disc_steward.db import Database
from disc_steward.web import render_release_ranking_panel


def test_release_ranking_panel_is_collapsed_and_escapes_values(tmp_path: Path):
    db = Database(tmp_path / "disc_steward.sqlite3")
    db.initialize()
    job_id = db.upsert_job(tmp_path / "disc")
    db.save_release_ranking(job_id, {
        "matches": [{
            "release_key": "release<1>",
            "title": "<unsafe>",
            "score": 0.91,
            "confidence": "high_confidence",
            "warnings": ["different edition possible"],
        }],
        "warnings": ["manual confirmation recommended"],
    })

    html = render_release_ranking_panel(db, job_id)

    assert "<details" in html
    assert "Release-fit ranking" in html
    assert "&lt;unsafe&gt;" in html
    assert "&lt;1&gt;" in html
    assert "manual confirmation recommended" in html
