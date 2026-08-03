from pathlib import Path

from disc_steward.config import AppConfig
from disc_steward.db import Database
from disc_steward.web import render_research_panel, render_verified_provider_panel


def test_research_panel_is_collapsed_and_escapes_source_data(tmp_path: Path):
    db = Database(tmp_path / "disc_steward.sqlite3")
    db.initialize()
    job_id = db.upsert_job(tmp_path / "disc")
    db.save_research_packet(
        job_id,
        {
            "status": "partial",
            "queries": [{"query": "disc DVD extras"}],
            "sources": [
                {
                    "url": "https://example.test/?q=\"x\"",
                    "title": "<unsafe>",
                    "status": "fetched",
                    "snippet": "<script>alert(1)</script>",
                }
            ],
            "warnings": ["different edition possible"],
        },
    )

    html = render_research_panel(db, job_id)

    assert "<details" in html
    assert "Disc research provenance" in html
    assert "&lt;unsafe&gt;" in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "target=\"_blank\"" in html
