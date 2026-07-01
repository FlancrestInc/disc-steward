from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable
from urllib.parse import urlencode, quote
from urllib.request import Request, urlopen

from .config import MetadataConfig
from .models import FileReviewDecision, JobReviewMetadata


@dataclass
class MetadataCandidate:
    provider: str
    title: str
    year: int | None = None
    content_type: str = "unknown"
    library_root: str | None = None
    original_title: str | None = None
    japanese_title: str | None = None
    romanized_title: str | None = None
    translated_title: str | None = None
    episode_titles: list[str] = field(default_factory=list)
    extras: list[str] = field(default_factory=list)
    confidence: float = 0.0
    provider_id: str | None = None
    imdb_id: str | None = None
    tmdb_id: str | None = None
    tvdb_id: str | None = None
    anidb_id: str | None = None
    anilist_id: str | None = None
    mal_id: str | None = None
    raw: dict | None = None


@dataclass
class MetadataLookupResult:
    candidates: list[MetadataCandidate] = field(default_factory=list)
    applied_fields: dict[str, list[str]] = field(default_factory=dict)
    provider_results: list[dict[str, object]] = field(default_factory=list)
    warnings: list[dict[str, str]] = field(default_factory=list)


class MetadataProvider:
    name = "manual"

    def configured(self) -> bool:
        return False

    def lookup(self, query: str, year: int | None = None) -> list[MetadataCandidate]:
        return []


class ManualImdbProvider(MetadataProvider):
    name = "imdb_manual"

    def configured(self) -> bool:
        return True


JsonSender = Callable[[str, dict | None], dict]
HeaderJsonSender = Callable[[str, dict | None, dict[str, str]], dict]


class TmdbProvider(MetadataProvider):
    name = "tmdb"
    base_url = "https://api.themoviedb.org/3"

    def __init__(self, api_key: str, sender: JsonSender | None = None) -> None:
        self.api_key = api_key
        self.sender = sender or _request_json

    def configured(self) -> bool:
        return bool(self.api_key)

    def lookup_by_ids(
        self,
        imdb_id: str | None = None,
        tmdb_id: str | None = None,
        anilist_id: str | None = None,
        mal_id: str | None = None,
    ) -> list[MetadataCandidate]:
        imdb_id = _imdb_id(imdb_id)
        tmdb_id = _numeric_provider_id(tmdb_id, "tmdbid")
        candidates = []
        if tmdb_id:
            for path, mapper in [(f"/movie/{quote(tmdb_id)}", self._movie_candidate), (f"/tv/{quote(tmdb_id)}", self._tv_candidate)]:
                try:
                    item = self._get(path, {})
                except Exception:
                    continue
                if item.get("id"):
                    candidates.append(mapper(item, confidence=1.0))
            if candidates:
                return candidates
        if imdb_id:
            data = self._get(f"/find/{quote(imdb_id)}", {"external_source": "imdb_id"})
            for item in data.get("movie_results", []) or []:
                candidates.append(self._movie_candidate(item, confidence=1.0, imdb_id=imdb_id))
            for item in data.get("tv_results", []) or []:
                candidates.append(self._tv_candidate(item, confidence=1.0, imdb_id=imdb_id))
            return candidates
        return []

    def lookup(self, query: str, year: int | None = None) -> list[MetadataCandidate]:
        candidates = []
        movie_params: dict[str, object] = {"query": query}
        tv_params: dict[str, object] = {"query": query}
        if year:
            movie_params["year"] = year
            tv_params["first_air_date_year"] = year
        movie_data = self._get("/search/movie", movie_params)
        tv_data = self._get("/search/tv", tv_params)
        for item in (movie_data.get("results") or [])[:5]:
            candidates.append(self._movie_candidate(item, confidence=_match_confidence(query, year, item.get("title"), item.get("release_date"))))
        for item in (tv_data.get("results") or [])[:5]:
            candidates.append(self._tv_candidate(item, confidence=_match_confidence(query, year, item.get("name"), item.get("first_air_date"))))
        return candidates

    def _get(self, path: str, params: dict[str, object]) -> dict:
        query = {"api_key": self.api_key, **params}
        return self.sender(f"{self.base_url}{path}?{urlencode(query)}", None)

    def _movie_candidate(self, item: dict, confidence: float, imdb_id: str | None = None) -> MetadataCandidate:
        year = _year_from_date(item.get("release_date"))
        tmdb_id = str(item["id"]) if item.get("id") is not None else None
        return MetadataCandidate(
            provider=self.name,
            provider_id=tmdb_id,
            title=item.get("title") or item.get("original_title") or "",
            original_title=item.get("original_title"),
            year=year,
            content_type="movie",
            library_root="Movies",
            imdb_id=imdb_id or item.get("imdb_id"),
            tmdb_id=tmdb_id,
            confidence=confidence,
            raw=item,
        )

    def _tv_candidate(self, item: dict, confidence: float, imdb_id: str | None = None) -> MetadataCandidate:
        year = _year_from_date(item.get("first_air_date"))
        tmdb_id = str(item["id"]) if item.get("id") is not None else None
        return MetadataCandidate(
            provider=self.name,
            provider_id=tmdb_id,
            title=item.get("name") or item.get("original_name") or "",
            original_title=item.get("original_name"),
            year=year,
            content_type="show",
            library_root="Shows",
            imdb_id=imdb_id or item.get("imdb_id"),
            tmdb_id=tmdb_id,
            confidence=confidence,
            raw=item,
        )


class AniListProvider(MetadataProvider):
    name = "anilist"
    endpoint = "https://graphql.anilist.co"

    def __init__(self, sender: JsonSender | None = None) -> None:
        self.sender = sender or _request_json

    def configured(self) -> bool:
        return True

    def lookup_by_ids(
        self,
        imdb_id: str | None = None,
        tmdb_id: str | None = None,
        anilist_id: str | None = None,
        mal_id: str | None = None,
    ) -> list[MetadataCandidate]:
        variables: dict[str, object] = {}
        anilist_id = _numeric_provider_id(anilist_id, "anilistid")
        mal_id = _numeric_provider_id(mal_id, "malid")
        if anilist_id:
            variables["id"] = _int_or_none(anilist_id)
        if mal_id:
            variables["idMal"] = _int_or_none(mal_id)
        if not variables:
            return []
        data = self.sender(self.endpoint, {"query": _ANILIST_QUERY, "variables": variables})
        media = (data.get("data") or {}).get("Media")
        return [self._candidate(media, confidence=1.0)] if media else []

    def lookup(self, query: str, year: int | None = None) -> list[MetadataCandidate]:
        variables: dict[str, object] = {"search": query}
        data = self.sender(self.endpoint, {"query": _ANILIST_QUERY, "variables": variables})
        media = (data.get("data") or {}).get("Media")
        if not media:
            return []
        candidate = self._candidate(media, confidence=_match_confidence(query, year, _preferred_anilist_title(media), str((media.get("startDate") or {}).get("year") or "")))
        return [candidate]

    def _candidate(self, media: dict, confidence: float) -> MetadataCandidate:
        title = media.get("title") or {}
        anilist_id = str(media["id"]) if media.get("id") is not None else None
        mal_id = str(media["idMal"]) if media.get("idMal") is not None else None
        year = (media.get("startDate") or {}).get("year")
        return MetadataCandidate(
            provider=self.name,
            provider_id=anilist_id,
            title=title.get("english") or title.get("romaji") or title.get("native") or "",
            original_title=title.get("native"),
            romanized_title=title.get("romaji"),
            translated_title=title.get("english"),
            year=year,
            content_type="anime",
            library_root="Anime",
            anilist_id=anilist_id,
            mal_id=mal_id,
            confidence=confidence,
            raw=media,
        )


class MalProvider(MetadataProvider):
    name = "mal"
    base_url = "https://api.myanimelist.net/v2"

    def __init__(self, client_id: str, sender: HeaderJsonSender | None = None) -> None:
        self.client_id = client_id
        self.sender = sender or _request_json_with_headers

    def configured(self) -> bool:
        return bool(self.client_id)

    def lookup_by_ids(
        self,
        imdb_id: str | None = None,
        tmdb_id: str | None = None,
        anilist_id: str | None = None,
        mal_id: str | None = None,
    ) -> list[MetadataCandidate]:
        mal_id = _numeric_provider_id(mal_id, "malid")
        if not mal_id:
            return []
        data = self._get(f"/anime/{quote(mal_id)}", {"fields": _MAL_FIELDS})
        return [self._candidate(data, confidence=1.0)] if data.get("id") else []

    def lookup(self, query: str, year: int | None = None) -> list[MetadataCandidate]:
        data = self._get("/anime", {"q": query, "limit": 5, "fields": _MAL_FIELDS})
        candidates = []
        for item in data.get("data") or []:
            node = item.get("node") or {}
            candidates.append(self._candidate(node, confidence=_match_confidence(query, year, _preferred_mal_title(node), node.get("start_date"))))
        return candidates

    def _get(self, path: str, params: dict[str, object]) -> dict:
        return self.sender(
            f"{self.base_url}{path}?{urlencode(params)}",
            None,
            {"X-MAL-CLIENT-ID": self.client_id},
        )

    def _candidate(self, item: dict, confidence: float) -> MetadataCandidate:
        titles = item.get("alternative_titles") or {}
        mal_id = str(item["id"]) if item.get("id") is not None else None
        return MetadataCandidate(
            provider=self.name,
            provider_id=mal_id,
            title=titles.get("en") or item.get("title") or titles.get("ja") or "",
            original_title=titles.get("ja"),
            romanized_title=item.get("title"),
            translated_title=titles.get("en"),
            year=_year_from_date(item.get("start_date")),
            content_type="anime",
            library_root="Anime",
            mal_id=mal_id,
            confidence=confidence,
            raw=item,
        )


_MAL_FIELDS = "alternative_titles,start_date,num_episodes"


_ANILIST_QUERY = """
query ($id: Int, $idMal: Int, $search: String) {
  Media(id: $id, idMal: $idMal, search: $search, type: ANIME) {
    id
    idMal
    title { english romaji native }
    startDate { year }
    episodes
  }
}
"""


def lookup_job_metadata(db, config, job_id: int, providers: list[Callable] | None = None) -> MetadataLookupResult:
    if not config.metadata.enabled:
        return MetadataLookupResult()
    candidates: list[MetadataCandidate] = []
    warnings: list[dict[str, str]] = []
    provider_results: list[dict[str, object]] = []
    provider_callables = providers or _provider_callables(config)
    for provider_name, provider in _provider_entries(provider_callables):
        try:
            provider_candidates = provider(db, config, job_id)
            candidates.extend(provider_candidates)
            provider_results.append({"provider": provider_name, "candidate_count": len(provider_candidates), "status": "ok"})
        except Exception as error:
            provider_results.append({"provider": provider_name, "candidate_count": 0, "status": "failed"})
            warnings.append({"provider": provider_name, "message": str(error)})
    db.clear_metadata_candidates(job_id)
    for candidate in candidates:
        db.save_metadata_candidate(job_id, candidate.provider, _candidate_payload(candidate))
    best = _best_auto_candidate(candidates)
    applied: dict[str, list[str]] = {}
    if best is not None:
        applied = _apply_candidate(db, config, job_id, best)
    db.audit(
        "metadata_lookup",
        f"Metadata lookup found {len(candidates)} candidate(s), applied {sum(len(value) for value in applied.values())} field(s)",
        job_id,
        {"applied_fields": applied, "provider_results": provider_results, "warnings": warnings},
    )
    return MetadataLookupResult(candidates=candidates, applied_fields=applied, provider_results=provider_results, warnings=warnings)


def lookup_file_metadata(
    db,
    config,
    job_id: int,
    source_file_id: int,
    providers: list[Callable] | None = None,
) -> MetadataLookupResult:
    if not config.metadata.enabled:
        return MetadataLookupResult()
    candidates: list[MetadataCandidate] = []
    warnings: list[dict[str, str]] = []
    provider_results: list[dict[str, object]] = []
    provider_callables = providers or _file_provider_callables(config)
    for provider_name, provider in _provider_entries(provider_callables):
        try:
            provider_candidates = provider(db, config, job_id, source_file_id)
            candidates.extend(provider_candidates)
            provider_results.append({"provider": provider_name, "candidate_count": len(provider_candidates), "status": "ok"})
        except Exception as error:
            provider_results.append({"provider": provider_name, "candidate_count": 0, "status": "failed"})
            warnings.append({"provider": provider_name, "message": str(error)})
    db.clear_metadata_candidates(job_id, source_file_id)
    for candidate in candidates:
        db.save_metadata_candidate(job_id, candidate.provider, _candidate_payload(candidate), source_file_id)
    best = _best_auto_candidate(candidates)
    applied: dict[str, list[str]] = {}
    if best is not None:
        applied = _apply_file_candidate(db, config, job_id, source_file_id, best)
    db.audit(
        "metadata_lookup",
        f"Metadata lookup for file {source_file_id} found {len(candidates)} candidate(s), applied {sum(len(value) for value in applied.values())} field(s)",
        job_id,
        {
            "source_file_id": source_file_id,
            "applied_fields": applied,
            "provider_results": provider_results,
            "warnings": warnings,
        },
    )
    return MetadataLookupResult(candidates=candidates, applied_fields=applied, provider_results=provider_results, warnings=warnings)


def _provider_callables(config) -> list[Callable]:
    callables = []
    tmdb = config.metadata.providers.get("tmdb")
    if tmdb and _configured("tmdb", tmdb.enabled, tmdb.api_key):
        provider = TmdbProvider(tmdb.api_key)
        callables.append(("tmdb", lambda db, config, job_id, provider=provider: _lookup_with_provider(db, job_id, provider)))
    anilist = config.metadata.providers.get("anilist")
    if anilist and _configured("anilist", anilist.enabled, anilist.api_key):
        provider = AniListProvider()
        callables.append(("anilist", lambda db, config, job_id, provider=provider: _lookup_with_provider(db, job_id, provider)))
    mal = config.metadata.providers.get("mal")
    if mal and _configured("mal", mal.enabled, mal.api_key):
        provider = MalProvider(mal.api_key)
        callables.append(("mal", lambda db, config, job_id, provider=provider: _lookup_with_provider(db, job_id, provider)))
    return callables


def _file_provider_callables(config) -> list[Callable]:
    callables = []
    tmdb = config.metadata.providers.get("tmdb")
    if tmdb and _configured("tmdb", tmdb.enabled, tmdb.api_key):
        provider = TmdbProvider(tmdb.api_key)
        callables.append(("tmdb", lambda db, config, job_id, source_file_id, provider=provider: _lookup_file_with_provider(db, job_id, source_file_id, provider)))
    anilist = config.metadata.providers.get("anilist")
    if anilist and _configured("anilist", anilist.enabled, anilist.api_key):
        provider = AniListProvider()
        callables.append(("anilist", lambda db, config, job_id, source_file_id, provider=provider: _lookup_file_with_provider(db, job_id, source_file_id, provider)))
    mal = config.metadata.providers.get("mal")
    if mal and _configured("mal", mal.enabled, mal.api_key):
        provider = MalProvider(mal.api_key)
        callables.append(("mal", lambda db, config, job_id, source_file_id, provider=provider: _lookup_file_with_provider(db, job_id, source_file_id, provider)))
    return callables


def _provider_entries(providers: list[Callable]) -> list[tuple[str, Callable]]:
    entries = []
    for index, provider in enumerate(providers, start=1):
        if isinstance(provider, tuple):
            entries.append((str(provider[0]), provider[1]))
        else:
            entries.append((getattr(provider, "name", f"provider_{index}"), provider))
    return entries


def _lookup_with_provider(db, job_id: int, provider: MetadataProvider) -> list[MetadataCandidate]:
    job = db.get_job(job_id)
    review = db.get_job_review(job_id)
    candidates: list[MetadataCandidate] = []
    lookup_by_ids = getattr(provider, "lookup_by_ids", None)
    if lookup_by_ids:
        candidates.extend(
            lookup_by_ids(
                imdb_id=review.imdb_id,
                tmdb_id=review.tmdb_id,
                anilist_id=review.anilist_id,
                mal_id=review.mal_id,
            )
        )
    query = _lookup_query(job.disc_title if job else "", review, db.source_file_payloads(job_id))
    if query:
        candidates.extend(provider.lookup(query, review.year))
    return candidates


def _lookup_file_with_provider(db, job_id: int, source_file_id: int, provider: MetadataProvider) -> list[MetadataCandidate]:
    job = db.get_job(job_id)
    review = db.get_job_review(job_id)
    row = db.source_file_payload(source_file_id)
    if row is None or int(row["job_id"]) != job_id:
        return []
    decisions = {decision.source_file_id: decision for decision in db.list_file_reviews(job_id)}
    decision = decisions.get(source_file_id) or FileReviewDecision(source_file_id=source_file_id)
    candidates: list[MetadataCandidate] = []
    lookup_by_ids = getattr(provider, "lookup_by_ids", None)
    if lookup_by_ids:
        candidates.extend(
            lookup_by_ids(
                imdb_id=decision.imdb_id or review.imdb_id,
                tmdb_id=decision.tmdb_id or review.tmdb_id,
                anilist_id=decision.anilist_id or review.anilist_id,
                mal_id=decision.mal_id or review.mal_id,
            )
        )
    query = _file_lookup_query(job.disc_title if job else "", review, decision, row)
    if query:
        candidates.extend(provider.lookup(query, review.year))
    return candidates


def _lookup_query(job_title: str, review: JobReviewMetadata, rows: list[dict]) -> str:
    for value in [review.title, *(row.get("embedded_title") for row in rows), *(row.get("makemkv_title") for row in rows), job_title]:
        if value:
            return str(value)
    return ""


def _file_lookup_query(job_title: str, review: JobReviewMetadata, decision: FileReviewDecision, row: dict) -> str:
    filename_stem = Path(row["filename"]).stem
    for value in [
        decision.final_display_name,
        decision.translated_title,
        decision.romanized_title,
        decision.original_title,
        row.get("embedded_title"),
        row.get("makemkv_title"),
        filename_stem,
        review.title,
        job_title,
    ]:
        if value:
            return str(value)
    return ""


def _best_auto_candidate(candidates: list[MetadataCandidate]) -> MetadataCandidate | None:
    if not candidates:
        return None
    best = max(candidates, key=lambda candidate: candidate.confidence)
    return best if best.confidence >= 0.9 else None


def _apply_candidate(db, config, job_id: int, candidate: MetadataCandidate) -> dict[str, list[str]]:
    job = db.get_job(job_id)
    review = db.get_job_review(job_id)
    applied: dict[str, list[str]] = {}

    def set_job(field_name: str, value: object, *, default_title: bool = False) -> None:
        if value in {None, ""}:
            return
        current = getattr(review, field_name)
        can_fill = current in {None, "", "unknown"}
        if default_title and job and current == job.disc_title:
            can_fill = True
        if can_fill:
            setattr(review, field_name, value)
            applied.setdefault("job", []).append(field_name)

    set_job("title", candidate.title, default_title=True)
    set_job("original_title", candidate.original_title or candidate.japanese_title)
    set_job("romanized_title", candidate.romanized_title)
    set_job("translated_title", candidate.translated_title)
    set_job("year", candidate.year)
    set_job("content_type", candidate.content_type)
    set_job("library_root", candidate.library_root)
    for field_name in ["imdb_id", "tmdb_id", "tvdb_id", "anidb_id", "anilist_id", "mal_id"]:
        set_job(field_name, getattr(candidate, field_name))
    if candidate.content_type == "anime":
        if not review.anime_flag:
            review.anime_flag = True
            applied.setdefault("job", []).append("anime_flag")
        if not review.japanese_media_flag:
            review.japanese_media_flag = True
            applied.setdefault("job", []).append("japanese_media_flag")
    if applied.get("job"):
        db.save_job_review(review)

    if candidate.episode_titles:
        rows = sorted(db.source_file_payloads(job_id), key=lambda row: row["filename"])
        saved = {decision.source_file_id: decision for decision in db.list_file_reviews(job_id)}
        for index, (row, episode_title) in enumerate(zip(rows, candidate.episode_titles, strict=False), start=1):
            decision = saved.get(row["id"]) or FileReviewDecision(source_file_id=row["id"])
            file_fields = _apply_episode_fields(decision, candidate, episode_title, index, config)
            if file_fields:
                db.save_file_review(decision)
                applied[f"file:{row['id']}"] = file_fields
    return applied


def _apply_file_candidate(db, config, job_id: int, source_file_id: int, candidate: MetadataCandidate) -> dict[str, list[str]]:
    row = db.source_file_payload(source_file_id)
    if row is None or int(row["job_id"]) != job_id:
        return {}
    decisions = {decision.source_file_id: decision for decision in db.list_file_reviews(job_id)}
    decision = decisions.get(source_file_id) or FileReviewDecision(source_file_id=source_file_id)
    fields: list[str] = []
    filename_stem = Path(row["filename"]).stem

    def set_file(field_name: str, value: object, *, default_display_name: bool = False) -> None:
        if value in {None, ""}:
            return
        current = getattr(decision, field_name)
        can_fill = current in {None, "", "unknown"}
        if default_display_name and current == filename_stem:
            can_fill = True
        if can_fill:
            setattr(decision, field_name, value)
            fields.append(field_name)

    display_name = candidate.translated_title or candidate.romanized_title or candidate.title
    set_file("content_type", candidate.content_type)
    set_file("final_display_name", display_name, default_display_name=True)
    set_file("original_title", candidate.original_title or candidate.japanese_title)
    set_file("translated_title", candidate.translated_title)
    set_file("romanized_title", candidate.romanized_title)
    for field_name in ["imdb_id", "tmdb_id", "tvdb_id", "anidb_id", "anilist_id", "mal_id"]:
        set_file(field_name, getattr(candidate, field_name))
    if fields:
        db.save_file_review(decision)
        return {f"file:{source_file_id}": fields}
    return {}


def _apply_episode_fields(
    decision: FileReviewDecision,
    candidate: MetadataCandidate,
    episode_title: str,
    index: int,
    config,
) -> list[str]:
    fields = []

    def set_file(field_name: str, value: object) -> None:
        if value in {None, ""}:
            return
        current = getattr(decision, field_name)
        if current in {None, "", "unknown"}:
            setattr(decision, field_name, value)
            fields.append(field_name)

    set_file("role", "episode")
    set_file("content_type", candidate.content_type)
    set_file("final_display_name", episode_title)
    set_file("season_number", 1)
    set_file("episode_number", index)
    set_file("encoding_profile", config.preferred_video_profile)
    set_file("subtitle_policy", "manual_review")
    for field_name in ["imdb_id", "tmdb_id", "tvdb_id", "anidb_id", "anilist_id", "mal_id"]:
        set_file(field_name, getattr(candidate, field_name))
    return fields


def _candidate_payload(candidate: MetadataCandidate) -> dict:
    payload = asdict(candidate)
    payload.pop("raw", None)
    return payload


def _request_json(url: str, payload: dict | None = None) -> dict:
    if payload is None:
        with urlopen(url, timeout=30) as response:  # noqa: S310 - provider URLs are fixed and config-gated
            return json.loads(response.read().decode("utf-8") or "{}")
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = Request(url, data=data, headers={"Content-Type": "application/json"}, method="POST")
    with urlopen(request, timeout=30) as response:  # noqa: S310 - provider URL is fixed
        return json.loads(response.read().decode("utf-8") or "{}")


def _request_json_with_headers(url: str, payload: dict | None = None, headers: dict[str, str] | None = None) -> dict:
    if payload is None:
        request = Request(url, headers=headers or {})
    else:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = Request(url, data=data, headers={"Content-Type": "application/json", **(headers or {})}, method="POST")
    with urlopen(request, timeout=30) as response:  # noqa: S310 - provider URL is fixed
        return json.loads(response.read().decode("utf-8") or "{}")


def _year_from_date(value: str | None) -> int | None:
    if not value:
        return None
    try:
        return int(str(value)[:4])
    except ValueError:
        return None


def _match_confidence(query: str, year: int | None, title: str | None, date: str | None) -> float:
    if _normalize(query) == _normalize(title):
        if year is None or _year_from_date(date) == year:
            return 0.92
    return 0.7


def _normalize(value: str | None) -> str:
    return "".join(char.lower() for char in (value or "") if char.isalnum())


def _preferred_anilist_title(media: dict) -> str:
    title = media.get("title") or {}
    return title.get("english") or title.get("romaji") or title.get("native") or ""


def _preferred_mal_title(item: dict) -> str:
    titles = item.get("alternative_titles") or {}
    return titles.get("en") or item.get("title") or titles.get("ja") or ""


def _int_or_none(value: str | None) -> int | None:
    try:
        return int(value) if value is not None else None
    except ValueError:
        return None


def _imdb_id(value: str | None) -> str | None:
    if not value:
        return None
    match = re.search(r"tt\d+", value, re.IGNORECASE)
    return match.group(0).lower() if match else value.strip()


def _numeric_provider_id(value: str | None, tag: str) -> str | None:
    if not value:
        return None
    text = value.strip()
    tag_match = re.search(rf"{re.escape(tag)}[-_\s:]*(\d+)", text, re.IGNORECASE)
    if tag_match:
        return tag_match.group(1)
    url_match = re.search(r"/(?:movie|tv|anime|manga)/(\d+)(?:[^\d]|$)", text, re.IGNORECASE)
    if url_match:
        return url_match.group(1)
    if text.isdigit():
        return text
    return None


def metadata_provider_status(config: MetadataConfig) -> dict:
    return {
        "enabled": config.enabled,
        "providers": {
            name: {
                "enabled": provider.enabled,
                "configured": _configured(name, provider.enabled, provider.api_key),
            }
            for name, provider in config.providers.items()
        },
    }


def _configured(name: str, enabled: bool, api_key: str) -> bool:
    if not enabled:
        return False
    if name in {"tmdb", "tvdb", "anidb", "mal"}:
        return bool(api_key)
    if name == "anilist":
        return True
    return False
