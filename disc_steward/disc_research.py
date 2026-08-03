from __future__ import annotations

import hashlib
import html
import json
import re
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Iterable
from urllib.parse import parse_qs, quote, urldefrag, urlparse
from urllib.request import Request, urlopen

_RE_EPISODE = re.compile(
    r"(?:episode|ep\.?|chapter)\s*(?P<number>\d{1,3})\s*[:.\-–—]?\s*(?P<title>[^|\n]{2,120})",
    re.IGNORECASE,
)
_RE_SEASON_EPISODE = re.compile(r"\bS(?P<season>\d{1,2})E(?P<episode>\d{1,3})\b", re.IGNORECASE)
_RE_DISC = re.compile(r"\b(?:disc|disk|volume|vol\.?)\s*(?P<number>\d{1,2})\b", re.IGNORECASE)
_RE_EXTRA = re.compile(
    r"\b(?P<kind>deleted scenes?|featurettes?|making[- ]of|interviews?|trailers?|music videos?|promos?|commentary|bonus features?)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class DiscResearchQuery:
    query: str
    reason: str
    content_type: str | None = None


@dataclass
class DiscResearchSource:
    url: str
    title: str = ""
    source_kind: str = "unknown"
    status: str = "unfetched"
    fetched_text: str = ""
    snippet: str = ""
    error: str | None = None
    retrieved_at: str | None = None
    source_id: str = ""

    def __post_init__(self) -> None:
        self.url = normalize_url(self.url)
        self.source_id = self.source_id or source_identifier(self.url)


@dataclass(frozen=True)
class DiscResearchFact:
    fact_type: str
    title: str
    source_id: str
    source_url: str
    snippet: str
    episode_number: int | None = None
    season_number: int | None = None
    extra_type: str | None = None
    disc_number: int | None = None
    confidence: float = 0.0


@dataclass
class DiscResearchPacket:
    queries: list[DiscResearchQuery] = field(default_factory=list)
    sources: list[DiscResearchSource] = field(default_factory=list)
    facts: list[DiscResearchFact] = field(default_factory=list)
    status: str = "not_requested"
    warnings: list[str] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)
    packet_version: str = "1"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "DiscResearchPacket":
        return cls(
            queries=[DiscResearchQuery(**item) for item in payload.get("queries", [])],
            sources=[DiscResearchSource(**item) for item in payload.get("sources", [])],
            facts=[DiscResearchFact(**item) for item in payload.get("facts", [])],
            status=str(payload.get("status", "not_requested")),
            warnings=[str(item) for item in payload.get("warnings", [])],
            conflicts=[str(item) for item in payload.get("conflicts", [])],
            packet_version=str(payload.get("packet_version", "1")),
        )


def normalize_text(text: str, max_chars: int = 12000) -> str:
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    return text[:max(0, max_chars)]


def normalize_url(url: str) -> str:
    clean, _ = urldefrag(str(url or "").strip())
    return clean


def source_identifier(url: str) -> str:
    return hashlib.sha256(normalize_url(url).encode("utf-8")).hexdigest()[:16]


def build_research_queries(
    disc_title: str,
    *,
    content_type: str | None = None,
    folder_name: str | None = None,
    disc_hint: str | None = None,
    max_queries: int = 8,
) -> list[DiscResearchQuery]:
    title = normalize_text(disc_title, 180)
    if not title:
        title = normalize_text(folder_name or "", 180)
    if not title:
        return []
    suffixes = [("", "exact title identity"), ("DVD contents", "release contents"), ("DVD episodes", "episode listing"), ("DVD extras", "extras inventory")]
    if disc_hint:
        suffixes.append((f"{disc_hint} episodes", "disc-specific episode listing"))
        suffixes.append((f"{disc_hint} extras", "disc-specific extras listing"))
    if content_type in {"anime", "show"}:
        suffixes.append(("complete episode list", "series episode list"))
    if content_type == "anime":
        suffixes.append(("Anime News Network episode list", "anime episode source"))
    queries: list[DiscResearchQuery] = []
    seen: set[str] = set()
    for suffix, reason in suffixes:
        query = f"{title} {suffix}".strip()
        key = query.casefold()
        if key in seen:
            continue
        seen.add(key)
        queries.append(DiscResearchQuery(query, reason, content_type))
        if len(queries) >= max(1, max_queries):
            break
    return queries


SearchFn = Callable[[str], Iterable[dict[str, Any]]]
FetchFn = Callable[[str], dict[str, Any] | str]


def duckduckgo_search(query: str, *, timeout_seconds: float = 15.0, max_results: int = 5) -> list[dict[str, str]]:
    """Fetch public DuckDuckGo HTML results without credentials or cookies."""
    endpoint = f"https://html.duckduckgo.com/html/?q={quote(query)}"
    request = Request(endpoint, headers={"User-Agent": "disc-steward-research/1.0"})
    with urlopen(request, timeout=timeout_seconds) as response:
        body = response.read(500_000).decode("utf-8", errors="replace")
    results: list[dict[str, str]] = []
    for match in re.finditer(
        r'<a[^>]+class="result__a"[^>]+href="(?P<href>[^"]+)"[^>]*>(?P<title>.*?)</a>',
        body,
        flags=re.IGNORECASE | re.DOTALL,
    ):
        href = html.unescape(match.group("href"))
        parsed = urlparse(href)
        redirected = parse_qs(parsed.query).get("uddg", [""])[0]
        url = redirected or href
        if not url.startswith(("http://", "https://")):
            continue
        title = re.sub(r"<[^>]+>", " ", html.unescape(match.group("title")))
        results.append({"url": url, "title": normalize_text(title, 300), "source_kind": "duckduckgo"})
        if len(results) >= max_results:
            break
    return results


def wikipedia_search(query: str, *, timeout_seconds: float = 15.0, max_results: int = 5) -> list[dict[str, str]]:
    endpoint = "https://en.wikipedia.org/w/api.php?action=query&list=search&format=json&utf8=1&srnamespace=0&srlimit=" + str(max_results) + "&srsearch=" + quote(query)
    request = Request(endpoint, headers={"User-Agent": "disc-steward-research/1.0 (bounded metadata lookup)"})
    with urlopen(request, timeout=timeout_seconds) as response:
        payload = json.loads(response.read(500_000).decode("utf-8", errors="replace"))
    results = []
    for item in payload.get("query", {}).get("search", [])[:max_results]:
        title = str(item.get("title", "")).strip()
        if not title:
            continue
        results.append({
            "url": "https://en.wikipedia.org/wiki/" + quote(title.replace(" ", "_")),
            "title": title,
            "snippet": re.sub(r"<[^>]+>", " ", html.unescape(str(item.get("snippet", "")))),
            "source_kind": "wikipedia",
        })
    return results


def configured_research_adapter(config: Any) -> BoundedResearchAdapter:
    """Construct the explicitly configured production research adapter."""
    provider = str(getattr(config.automatic_review, "research_provider", "none") or "none").strip().lower()
    limits = ResearchLimits(
        max_queries=config.automatic_review.research_max_queries,
        max_results_per_query=config.automatic_review.research_max_results_per_query,
        max_sources=config.automatic_review.research_max_sources,
        max_fetched_chars=config.automatic_review.research_max_fetched_chars,
        max_evidence_chars=config.automatic_review.research_max_evidence_chars,
        timeout_seconds=config.automatic_review.research_timeout_seconds,
    )
    if provider == "duckduckgo":
        return BoundedResearchAdapter(
            search=lambda query: duckduckgo_search(query, timeout_seconds=limits.timeout_seconds, max_results=limits.max_results_per_query),
            fetch=lambda url: fetch_url(url, timeout_seconds=limits.timeout_seconds),
            limits=limits,
        )
    if provider == "wikipedia":
        return BoundedResearchAdapter(
            search=lambda query: wikipedia_search(query, timeout_seconds=limits.timeout_seconds, max_results=limits.max_results_per_query),
            fetch=lambda url: fetch_url(url, timeout_seconds=limits.timeout_seconds),
            limits=limits,
        )
    return BoundedResearchAdapter(limits=limits)


@dataclass(frozen=True)
class ResearchLimits:
    max_queries: int = 8
    max_results_per_query: int = 5
    max_sources: int = 10
    max_fetched_chars: int = 20000
    max_evidence_chars: int = 6000
    timeout_seconds: float = 15.0


class BoundedResearchAdapter:
    """Provider-neutral bounded search/fetch adapter.

    Search and fetch functions are injected so production deployments can use a
    configured backend while tests remain offline. A missing backend produces a
    visible unavailable packet rather than fabricated evidence.
    """

    def __init__(
        self,
        *,
        search: SearchFn | None = None,
        fetch: FetchFn | None = None,
        limits: ResearchLimits | None = None,
    ) -> None:
        self.search = search
        self.fetch = fetch
        self.limits = limits or ResearchLimits()

    def collect(self, queries: Iterable[DiscResearchQuery]) -> DiscResearchPacket:
        packet = DiscResearchPacket(queries=list(queries)[: self.limits.max_queries])
        if not packet.queries:
            packet.status = "unavailable"
            packet.warnings.append("no research queries were generated")
            return packet
        if self.search is None or self.fetch is None:
            packet.status = "unavailable"
            packet.warnings.append("no configured search and page-fetch backend")
            return packet

        seen_urls: set[str] = set()
        for query in packet.queries:
            try:
                results = list(self.search(query.query))[: self.limits.max_results_per_query]
            except Exception as exc:  # provider failures must not abort review
                packet.warnings.append(f"search failed for {query.query!r}: {type(exc).__name__}")
                continue
            for result in results:
                if len(packet.sources) >= self.limits.max_sources:
                    break
                url = normalize_url(str(result.get("url", "")))
                if not url or url in seen_urls:
                    continue
                seen_urls.add(url)
                source = DiscResearchSource(
                    url=url,
                    title=normalize_text(str(result.get("title", "")), 300),
                    source_kind=str(result.get("source_kind", "search_result")),
                    snippet=normalize_text(str(result.get("snippet", "")), 700),
                )
                try:
                    fetched = self.fetch(url)
                    if isinstance(fetched, str):
                        fetched = {"text": fetched}
                    source.fetched_text = normalize_text(str(fetched.get("text", "")), self.limits.max_fetched_chars)
                    source.title = source.title or normalize_text(str(fetched.get("title", "")), 300)
                    source.status = "fetched" if source.fetched_text else "empty"
                    source.retrieved_at = datetime.now(timezone.utc).isoformat()
                except Exception as exc:
                    source.status = "failed"
                    source.error = type(exc).__name__
                packet.sources.append(source)
        packet.facts = extract_research_facts(packet.sources, self.limits.max_evidence_chars)
        packet.conflicts = detect_research_conflicts(packet.facts)
        if packet.conflicts:
            packet.warnings.append(f"{len(packet.conflicts)} conflicting research claim(s)")
        packet.status = "completed" if packet.sources and any(s.status == "fetched" for s in packet.sources) else "partial"
        if not packet.sources:
            packet.status = "unavailable"
            packet.warnings.append("search returned no usable sources")
        return packet


def fetch_url(url: str, *, timeout_seconds: float = 15.0, max_bytes: int = 200000) -> dict[str, str]:
    request = Request(url, headers={"User-Agent": "disc-steward-research/1.0"})
    with urlopen(request, timeout=timeout_seconds) as response:
        data = response.read(max_bytes + 1)
    if len(data) > max_bytes:
        data = data[:max_bytes]
    text = data.decode("utf-8", errors="replace")
    return {"title": "", "text": text}


def extract_research_facts(
    sources: Iterable[DiscResearchSource],
    max_evidence_chars: int = 6000,
) -> list[DiscResearchFact]:
    facts: list[DiscResearchFact] = []
    for source in sources:
        if source.status not in {"fetched", "unfetched"}:
            continue
        text = normalize_text(source.fetched_text or source.snippet, max_evidence_chars)
        if not text:
            continue
        disc_match = _RE_DISC.search(text)
        disc_number = int(disc_match.group("number")) if disc_match else None
        for match in _RE_SEASON_EPISODE.finditer(text):
            facts.append(DiscResearchFact("episode", match.group(0), source.source_id, source.url, _snippet(text, match.start()), int(match.group("episode")), int(match.group("season")), disc_number=disc_number, confidence=0.75))
        for match in _RE_EPISODE.finditer(text):
            title = normalize_text(match.group("title"), 120).strip(" -–—:;")
            if not title or title.casefold() in {"list", "title"}:
                continue
            number = int(match.group("number"))
            facts.append(DiscResearchFact("episode", title, source.source_id, source.url, _snippet(text, match.start()), episode_number=number, disc_number=disc_number, confidence=0.7))
        for match in _RE_EXTRA.finditer(text):
            kind = _normalize_extra_type(match.group("kind"))
            facts.append(DiscResearchFact("extra", normalize_text(match.group("kind"), 80), source.source_id, source.url, _snippet(text, match.start()), extra_type=kind, disc_number=disc_number, confidence=0.55))
        if disc_number is not None and disc_match is not None:
            facts.append(DiscResearchFact("disc_identity", f"Disc {disc_number}", source.source_id, source.url, _snippet(text, disc_match.start()), disc_number=disc_number, confidence=0.6))
    return _dedupe_facts(facts)


def facts_to_content_candidates(facts: Iterable[DiscResearchFact]) -> list[Any]:
    """Convert non-conflicting fact records into advisory matching candidates.

    The return type is kept provider-neutral at this boundary; callers can serialize
    these records into the existing ``ContentCandidate`` shape without mutating
    review state. Conflicting titles remain separate candidates.
    """
    from .disc_matching import ContentCandidate

    candidates: list[ContentCandidate] = []
    seen: set[tuple[str, str, int | None, int | None]] = set()
    for fact in facts:
        if fact.fact_type not in {"episode", "extra"} or not fact.title:
            continue
        kind = "episode" if fact.fact_type == "episode" else "extra"
        key = (kind, fact.title.casefold(), fact.season_number, fact.episode_number)
        if key in seen:
            continue
        seen.add(key)
        candidate_id = f"research:{fact.source_id}:{kind}:{fact.episode_number or fact.title.casefold()}"
        candidates.append(
            ContentCandidate(
                candidate_id=candidate_id,
                title=fact.title,
                kind=kind,
                season_number=fact.season_number,
                episode_number=fact.episode_number,
                extra_type=fact.extra_type,
                source_url=fact.source_url,
            )
        )
    return candidates


def detect_research_conflicts(facts: Iterable[DiscResearchFact]) -> list[str]:
    """Return disagreements without selecting a winning source."""
    episode_claims: dict[tuple[int | None, int | None], dict[str, set[str]]] = {}
    for fact in facts:
        if fact.fact_type != "episode" or fact.episode_number is None:
            continue
        key = (fact.disc_number, fact.episode_number)
        episode_claims.setdefault(key, {}).setdefault(fact.title.casefold(), set()).add(fact.source_id)
    conflicts = []
    for (disc_number, episode_number), titles in episode_claims.items():
        if len(titles) > 1:
            context = f"disc {disc_number}, " if disc_number is not None else ""
            claims = ", ".join(sorted(titles))
            conflicts.append(f"conflicting episode claims for {context}episode {episode_number}: {claims}")
    return conflicts


def _snippet(text: str, position: int, radius: int = 180) -> str:
    start = max(0, position - radius)
    return normalize_text(text[start : position + radius], 420)


def _normalize_extra_type(value: str) -> str:
    value = value.casefold()
    if "deleted" in value:
        return "deleted_scene"
    if "trailer" in value:
        return "trailer"
    if "interview" in value:
        return "interview"
    if "music" in value:
        return "music_video"
    if "feature" in value or "making" in value:
        return "featurette"
    if "commentary" in value:
        return "commentary_variant"
    return "promo"


def _dedupe_facts(facts: list[DiscResearchFact]) -> list[DiscResearchFact]:
    seen: set[tuple[str, str, str, int | None]] = set()
    result: list[DiscResearchFact] = []
    for fact in facts:
        key = (fact.fact_type, fact.title.casefold(), fact.source_id, fact.episode_number)
        if key not in seen:
            seen.add(key)
            result.append(fact)
    return result
