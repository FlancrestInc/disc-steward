import pytest

from disc_steward.release_inventory import combine_inventories, episode_inventory, extra_inventory, inventory_from_research_facts, with_durations
from disc_steward.disc_research import DiscResearchFact


def test_episode_inventory_preserves_order_and_citation():
    inventory = episode_inventory(
        release_key="wolverine-disc-1",
        title="Marvel Anime: Wolverine",
        episode_titles=["Mariko", "Yukio"],
        source_url="https://example.test/episodes",
        source_title="Episode list",
        content_type="anime",
    )

    assert [candidate.episode_number for candidate in inventory.candidates] == [1, 2]
    assert [candidate.title for candidate in inventory.candidates] == ["Mariko", "Yukio"]
    assert inventory.sources[0].url.endswith("episodes")
    assert all(candidate.source_url and candidate.source_url.endswith("episodes") for candidate in inventory.candidates)
    assert all(candidate.duration_seconds is None for candidate in inventory.candidates)


def test_with_durations_requires_one_duration_per_candidate():
    inventory = episode_inventory(
        release_key="iron-man",
        title="Marvel Anime: Iron Man",
        episode_titles=["One", "Two"],
        source_url="https://example.test/episodes",
        source_title="Episode list",
    )

    with pytest.raises(ValueError):
        with_durations(inventory, [1400])

    with_durations(inventory, [1418.0, 1419.0])
    assert [candidate.duration_seconds for candidate in inventory.candidates] == [1418.0, 1419.0]


def test_extra_inventory_preserves_type_and_combines_with_episodes():
    episodes = episode_inventory(
        release_key="series",
        title="Series",
        episode_titles=["Episode 1"],
        source_url="https://example.test/episodes",
        source_title="Episodes",
    )
    extras = extra_inventory(
        release_key="series",
        title="Series",
        extras=[("Deleted Scenes", "deleted_scene", "deleted", 240.0)],
        source_url="local://job/1/media-evidence",
        source_title="Observed title card",
    )

    combined = combine_inventories(episodes, extras)

    assert [candidate.kind for candidate in combined.candidates] == ["episode", "extra"]
    assert combined.candidates[1].extra_type == "deleted_scene"
    assert combined.candidates[1].source_url == "local://job/1/media-evidence"


def test_research_inventory_preserves_episode_extra_types_and_sources():
    inventory = inventory_from_research_facts(
        release_key="research:8",
        title="Series Disc",
        facts=[
            DiscResearchFact("episode", "Episode One", "source-a", "https://example.test/a", "episode 1", episode_number=1),
            DiscResearchFact("extra", "Deleted Scenes", "source-a", "https://example.test/a", "deleted scenes", extra_type="deleted_scene"),
        ],
    )

    assert [candidate.kind for candidate in inventory.candidates] == ["episode", "extra"]
    assert inventory.candidates[1].extra_type == "deleted_scene"
    assert inventory.sources[0].url == "https://example.test/a"
