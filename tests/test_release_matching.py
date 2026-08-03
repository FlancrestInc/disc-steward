from disc_steward.models import ScannedFile
from disc_steward.release_inventory import episode_inventory, with_durations
from disc_steward.release_matching import rank_release_inventories


def _file(name: str, duration: float) -> ScannedFile:
    return ScannedFile(
        path=f"/media/{name}.mkv",
        filename=f"{name}.mkv",
        parent_disc_folder="/media",
        size_bytes=1,
        modified_time=0,
        duration_seconds=duration,
        container_format="matroska,webm",
    )


def test_release_ranking_prefers_matching_count_and_duration_vector():
    matching = with_durations(
        episode_inventory(
            release_key="matching",
            title="Series",
            episode_titles=["One", "Two", "Three"],
            source_url="https://example.test/matching",
            source_title="Matching release",
        ),
        [1400, 1420, 1410],
    )
    wrong = with_durations(
        episode_inventory(
            release_key="wrong",
            title="Series",
            episode_titles=["One", "Two"],
            source_url="https://example.test/wrong",
            source_title="Wrong release",
        ),
        [900, 910],
    )

    ranking = rank_release_inventories([_file("a", 1401), _file("b", 1419), _file("c", 1411)], [wrong, matching])

    assert ranking.matches[0].release_key == "matching"
    assert ranking.matches[0].confidence == "high_confidence"


def test_release_ranking_does_not_force_generic_inventory():
    inventory = episode_inventory(
        release_key="generic",
        title="Series",
        episode_titles=["One", "Two"],
        source_url="https://example.test/list",
        source_title="Generic list",
    )

    ranking = rank_release_inventories([_file("long", 4200)], [inventory])

    assert ranking.matches[0].confidence == "manual_review"
    assert "inventory has no release-specific durations" in ranking.matches[0].warnings
