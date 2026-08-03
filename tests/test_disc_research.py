from disc_steward.disc_research import (
    BoundedResearchAdapter,
    DiscResearchPacket,
    DiscResearchQuery,
    DiscResearchSource,
    DiscResearchFact,
    ResearchLimits,
    build_research_queries,
    detect_research_conflicts,
    extract_research_facts,
    facts_to_content_candidates,
)


def test_query_planner_generates_dvd_episode_and_extra_variants():
    queries = build_research_queries(
        "Marvel Anime Wolverine",
        content_type="anime",
        disc_hint="disc 1",
        max_queries=8,
    )
    text = [query.query for query in queries]
    assert any("DVD contents" in query for query in text)
    assert any("DVD episodes" in query for query in text)
    assert any("DVD extras" in query for query in text)
    assert any("disc 1 episodes" in query for query in text)
    assert len(text) == len({query.casefold() for query in text})


def test_research_packet_round_trips_deterministically():
    source = DiscResearchSource("https://example.test/page#section", title="Example", status="empty")
    packet = DiscResearchPacket(sources=[source], status="partial", warnings=["limited"])

    restored = DiscResearchPacket.from_dict(packet.to_dict())

    assert restored.to_dict() == packet.to_dict()
    assert restored.sources[0].url == "https://example.test/page"
    assert restored.sources[0].source_id == source.source_id


def test_bounded_adapter_caps_queries_sources_and_survives_failures():
    searches = []

    def search(query):
        searches.append(query)
        if query == "bad":
            raise RuntimeError("offline")
        return [
            {"url": "https://example.test/a#one", "title": "A"},
            {"url": "https://example.test/a#two", "title": "duplicate"},
            {"url": "https://example.test/b", "title": "B"},
        ]

    def fetch(url):
        if url.endswith("/b"):
            raise TimeoutError("slow")
        return {"text": "Episode 1: Ambush. Disc 1. Deleted scenes and interview."}

    packet = BoundedResearchAdapter(
        search=search,
        fetch=fetch,
        limits=ResearchLimits(max_queries=2, max_results_per_query=3, max_sources=2),
    ).collect([] if False else [
        DiscResearchQuery("good", "test"),
        DiscResearchQuery("bad", "test"),
        DiscResearchQuery("ignored", "test"),
    ])

    assert searches == ["good", "bad"]
    assert len(packet.sources) == 2
    assert {source.status for source in packet.sources} == {"fetched", "failed"}
    assert packet.status == "completed"
    assert packet.warnings
    assert any(fact.fact_type == "episode" and fact.episode_number == 1 for fact in packet.facts)
    assert any(fact.extra_type == "deleted_scene" for fact in packet.facts)


def test_extract_facts_preserves_conflicting_sources():
    sources = [
        DiscResearchSource(
            "https://example.test/a",
            title="Source A",
            status="fetched",
            fetched_text="Episode 1: Ambush",
        ),
        DiscResearchSource(
            "https://example.test/b",
            title="Source B",
            status="fetched",
            fetched_text="Episode 1: Rising Malevolence",
        ),
    ]

    facts = extract_research_facts(sources)

    assert {fact.title for fact in facts if fact.fact_type == "episode"} == {"Ambush", "Rising Malevolence"}
    conflicts = detect_research_conflicts(facts)
    assert len(conflicts) == 1
    assert "episode 1" in conflicts[0]


def test_research_facts_become_advisory_candidates_without_merging_conflicts():
    facts = [
        DiscResearchFact("episode", "Ambush", "source-a", "https://a", "", episode_number=1),
        DiscResearchFact("episode", "Rising Malevolence", "source-b", "https://b", "", episode_number=1),
        DiscResearchFact("extra", "Interview", "source-a", "https://a", "", extra_type="interview"),
    ]

    candidates = facts_to_content_candidates(facts)

    assert [candidate.title for candidate in candidates] == ["Ambush", "Rising Malevolence", "Interview"]
    assert all(candidate.source_url for candidate in candidates)
