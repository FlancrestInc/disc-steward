from pathlib import Path

from disc_steward.db import Database
from disc_steward.web import render_verified_provider_panel


def test_verified_provider_panel_is_collapsed_and_escapes_values(tmp_path: Path):
    db = Database(tmp_path / "disc_steward.sqlite3")
    db.initialize()
    job_id = db.upsert_job(tmp_path / "disc")
    db.save_verified_provider_ids(
        job_id,
        [{
            "provider": "tmdb",
            "provider_id": "123",
            "provider_url": "https://www.themoviedb.org/movie/123?a=\"x\"",
            "evidence_url": "https://example.test/<unsafe>",
            "confidence": 0.95,
            "verification": "syntax_and_source",
        }],
    )

    html = render_verified_provider_panel(db, job_id)

    assert "<details" in html
    assert "Verified provider identities" in html
    assert "123" in html
    assert "&lt;unsafe&gt;" in html
    assert "target=\"_blank\"" in html
