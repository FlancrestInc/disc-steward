from disc_steward.provider_identity import verify_provider_id_set, verify_provider_ids


def test_provider_ids_require_syntax_and_matching_source_url():
    verified, warnings = verify_provider_ids(
        {
            "imdb_id": "tt1234567",
            "tmdb_id": "abc",
            "provider_url": "https://www.imdb.com/title/tt1234567/",
            "source_url": "https://example.test/release",
            "confidence": 0.9,
        }
    )

    assert [(item.provider, item.provider_id) for item in verified] == [("imdb", "tt1234567")]
    assert any("malformed tmdb" in warning for warning in warnings)


def test_provider_id_set_deduplicates_only_verified_ids():
    verified, warnings = verify_provider_id_set(
        [
            {"tmdb_id": "123", "provider_url": "https://www.themoviedb.org/movie/123", "confidence": 1.0},
            {"tmdb_id": "123", "provider_url": "https://www.themoviedb.org/movie/123", "confidence": 0.8},
            {"tvdb_id": "9", "provider_url": "https://example.test/9"},
        ]
    )

    assert [(item.provider, item.provider_id) for item in verified] == [("tmdb", "123")]
    assert any("tvdb" in warning for warning in warnings)
