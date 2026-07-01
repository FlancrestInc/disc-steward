from __future__ import annotations

import json
from pathlib import Path

from disc_steward.config import AppConfig, MetadataProviderConfig
from disc_steward.db import Database
from disc_steward import metadata
from disc_steward.metadata import MetadataCandidate
from disc_steward.models import AudioStream, FileReviewDecision, JobReviewMetadata, ScannedFile, VideoInfo
from disc_steward.scanner import scan_disc_folder


def _config(tmp_path: Path) -> AppConfig:
    config = AppConfig.default_for_root(tmp_path)
    config.metadata.enabled = True
    config.metadata.providers["tmdb"] = MetadataProviderConfig(enabled=True, api_key="tmdb-key")
    config.metadata.providers["anilist"] = MetadataProviderConfig(enabled=True)
    return config


def _source(path: Path, duration: float = 3600.0) -> ScannedFile:
    return ScannedFile(
        path=str(path),
        filename=path.name,
        parent_disc_folder=str(path.parent),
        size_bytes=100,
        modified_time=1.0,
        duration_seconds=duration,
        container_format="matroska,webm",
        video=VideoInfo(codec="h264", profile="High", pixel_format="yuv420p"),
        audio_streams=[AudioStream(index=1, codec="aac", language="eng")],
    )


def test_tmdb_provider_uses_imdb_id_find_and_maps_movie_candidate():
    assert hasattr(metadata, "TmdbProvider")
    calls: list[str] = []

    def sender(url: str, _payload: dict | None = None) -> dict:
        calls.append(url)
        return {
            "movie_results": [
                {
                    "id": 129,
                    "title": "Spirited Away",
                    "original_title": "千と千尋の神隠し",
                    "release_date": "2001-07-20",
                }
            ]
        }

    provider = metadata.TmdbProvider(api_key="tmdb-key", sender=sender)

    candidates = provider.lookup_by_ids(imdb_id="tt0245429")

    assert "external_source=imdb_id" in calls[0]
    assert candidates[0].title == "Spirited Away"
    assert candidates[0].tmdb_id == "129"
    assert candidates[0].imdb_id == "tt0245429"
    assert candidates[0].year == 2001
    assert candidates[0].content_type == "movie"
    assert candidates[0].confidence == 1.0


def test_tmdb_provider_accepts_common_tmdb_id_formats():
    calls: list[str] = []

    def sender(url: str, _payload: dict | None = None) -> dict:
        calls.append(url)
        if "/movie/268?" in url:
            return {"id": 268, "title": "Batman", "release_date": "1989-06-23"}
        raise ValueError(f"unexpected TMDb URL: {url}")

    provider = metadata.TmdbProvider(api_key="tmdb-key", sender=sender)

    for value in ["268", "tmdbid-268", "[tmdbid-268]", "https://www.themoviedb.org/movie/268-batman"]:
        candidates = provider.lookup_by_ids(tmdb_id=value)
        assert candidates[0].title == "Batman"
        assert candidates[0].tmdb_id == "268"

    assert any("/movie/268?" in url for url in calls)


def test_tmdb_provider_prefers_tmdb_id_when_imdb_id_is_also_present():
    calls: list[str] = []

    def sender(url: str, _payload: dict | None = None) -> dict:
        calls.append(url)
        if "/movie/268?" in url:
            return {"id": 268, "title": "Batman", "release_date": "1989-06-23"}
        if "/find/tt0245429?" in url:
            return {
                "movie_results": [
                    {
                        "id": 129,
                        "title": "Spirited Away",
                        "release_date": "2001-07-20",
                    }
                ]
            }
        raise ValueError(f"unexpected TMDb URL: {url}")

    provider = metadata.TmdbProvider(api_key="tmdb-key", sender=sender)

    candidates = provider.lookup_by_ids(imdb_id="tt0245429", tmdb_id="tmdbid-268")

    assert candidates[0].title == "Batman"
    assert candidates[0].tmdb_id == "268"
    assert "/movie/268?" in calls[0]


def test_anilist_provider_maps_mal_id_lookup_to_anime_candidate():
    assert hasattr(metadata, "AniListProvider")

    def sender(_url: str, payload: dict | None = None) -> dict:
        assert payload is not None
        assert payload["variables"]["idMal"] == 199
        return {
            "data": {
                "Media": {
                    "id": 20954,
                    "idMal": 199,
                    "title": {"english": "Spirited Away", "romaji": "Sen to Chihiro no Kamikakushi", "native": "千と千尋の神隠し"},
                    "startDate": {"year": 2001},
                    "episodes": 1,
                }
            }
        }

    provider = metadata.AniListProvider(sender=sender)

    candidates = provider.lookup_by_ids(mal_id="199")

    assert candidates[0].title == "Spirited Away"
    assert candidates[0].anilist_id == "20954"
    assert candidates[0].mal_id == "199"
    assert candidates[0].original_title == "千と千尋の神隠し"
    assert candidates[0].romanized_title == "Sen to Chihiro no Kamikakushi"
    assert candidates[0].content_type == "anime"


def test_lookup_job_metadata_applies_high_confidence_candidate_to_blank_or_default_fields(tmp_path):
    config = _config(tmp_path)
    db = Database(tmp_path / "disc_steward.sqlite3")
    db.initialize()
    disc = tmp_path / "DISC_FOLDER"
    disc.mkdir()
    source_id = db.upsert_source_file(db.upsert_job(disc), _source(disc / "title_t00.mkv"))

    assert hasattr(metadata, "lookup_job_metadata")
    result = metadata.lookup_job_metadata(
        db,
        config,
        1,
        providers=[
            lambda _db, _config, _job_id: [
                MetadataCandidate(
                    provider="tmdb",
                    title="Spirited Away",
                    original_title="千と千尋の神隠し",
                    year=2001,
                    content_type="movie",
                    library_root="Movies",
                    imdb_id="tt0245429",
                    tmdb_id="129",
                    confidence=1.0,
                )
            ]
        ],
    )

    review = db.get_job_review(1)
    assert result.applied_fields["job"] == ["title", "original_title", "year", "content_type", "imdb_id", "tmdb_id"]
    assert review.title == "Spirited Away"
    assert review.original_title == "千と千尋の神隠し"
    assert review.year == 2001
    assert review.imdb_id == "tt0245429"
    assert db.list_source_files(1)[0].id == source_id
    assert db.list_metadata_candidates(1)[0]["title"] == "Spirited Away"


def test_lookup_job_metadata_preserves_manual_fields_and_stores_ambiguous_candidates(tmp_path):
    config = _config(tmp_path)
    db = Database(tmp_path / "disc_steward.sqlite3")
    db.initialize()
    disc = tmp_path / "DISC"
    disc.mkdir()
    job_id = db.upsert_job(disc)
    db.upsert_source_file(job_id, _source(disc / "title_t00.mkv"))
    db.save_job_review(JobReviewMetadata(job_id=job_id, title="Manual Title", year=1999, content_type="movie", library_root="Movies"))

    assert hasattr(metadata, "lookup_job_metadata")
    result = metadata.lookup_job_metadata(
        db,
        config,
        job_id,
        providers=[
            lambda _db, _config, _job_id: [
                MetadataCandidate(provider="tmdb", title="Possible Match", year=2001, content_type="movie", confidence=0.7, tmdb_id="129")
            ]
        ],
    )

    review = db.get_job_review(job_id)
    assert result.applied_fields == {}
    assert review.title == "Manual Title"
    assert review.year == 1999
    assert db.list_metadata_candidates(job_id)[0]["title"] == "Possible Match"


def test_lookup_job_metadata_fills_confident_episode_titles(tmp_path):
    config = _config(tmp_path)
    db = Database(tmp_path / "disc_steward.sqlite3")
    db.initialize()
    disc = tmp_path / "SHOW_DISC"
    disc.mkdir()
    job_id = db.upsert_job(disc)
    first = db.upsert_source_file(job_id, _source(disc / "episode1.mkv", 1500))
    second = db.upsert_source_file(job_id, _source(disc / "episode2.mkv", 1501))

    assert hasattr(metadata, "lookup_job_metadata")
    metadata.lookup_job_metadata(
        db,
        config,
        job_id,
        providers=[
            lambda _db, _config, _job_id: [
                MetadataCandidate(
                    provider="tmdb",
                    title="Example Show",
                    year=2020,
                    content_type="show",
                    library_root="Shows",
                    tmdb_id="500",
                    episode_titles=["Pilot", "Second"],
                    confidence=1.0,
                )
            ]
        ],
    )

    reviews = {decision.source_file_id: decision for decision in db.list_file_reviews(job_id)}
    assert reviews[first].role == "episode"
    assert reviews[first].season_number == 1
    assert reviews[first].episode_number == 1
    assert reviews[first].final_display_name == "Pilot"
    assert reviews[second].episode_number == 2
    assert reviews[second].final_display_name == "Second"


def test_scan_metadata_lookup_failure_is_non_fatal(tmp_path):
    config = _config(tmp_path)
    db = Database(tmp_path / "disc_steward.sqlite3")
    db.initialize()
    disc = tmp_path / "DISC"
    disc.mkdir()
    media = disc / "movie.mkv"
    media.write_bytes(b"fake-media")
    fixture = Path("tests/fixtures/ffprobe_movie.json").read_text()

    job_id = scan_disc_folder(
        db,
        config,
        disc,
        ffprobe_runner=lambda _path: fixture,
        metadata_lookup=lambda _db, _config, _job_id: (_ for _ in ()).throw(RuntimeError("provider unavailable")),
    )

    assert job_id == 1
    assert len(db.list_source_files(job_id)) == 1
    assert any(row["event_type"] == "metadata_lookup_failed" for row in db.list_audit_events(job_id))
