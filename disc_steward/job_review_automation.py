from __future__ import annotations

import json
from typing import Callable

from .automatic_review import AutomaticLabel, build_automatic_bonus_labels
from .classifier import classify_disc_files
from .hermes_bonus_review import request_hermes_bonus_review
from .media_evidence import MediaEvidence, build_media_evidence, detect_aggregate_relations
from .disc_research import BoundedResearchAdapter, DiscResearchPacket, ResearchLimits, build_research_queries, facts_to_content_candidates
from .models import AudioStream, Classification, ScannedFile, SubtitleStream, VideoInfo
from .release_inventory import inventory_from_research_facts
from .release_matching import rank_release_inventories
from .review import clear_automatic_review_labels, seed_automatic_review


def run_automatic_review(
    db,
    config,
    job_id: int,
    scanned_files: list[ScannedFile],
    classifications: dict[str, Classification],
    source_ids: dict[str, int],
    folder_name: str,
    *,
    text_extractor: Callable[[ScannedFile], str] | None = None,
    hermes_review: Callable[..., dict] | None = None,
    research_adapter: BoundedResearchAdapter | None = None,
) -> dict[str, AutomaticLabel]:
    """Run local prefill plus Hermes review and persist blank-field suggestions."""
    if not config.automatic_review.enabled:
        return {}
    local_labels = build_automatic_bonus_labels(
        scanned_files,
        classifications,
        folder_name,
        config.ffmpeg_path,
        ocr_enabled=config.automatic_review.bonus_ocr_enabled,
        sample_count=config.automatic_review.ocr_sample_count,
        max_chars=config.automatic_review.ocr_max_chars,
        text_extractor=text_extractor,
        include_all_files=True,
    )
    automatic_labels = dict(local_labels)
    research_packet: DiscResearchPacket | None = None
    if config.automatic_review.research_enabled:
        queries = build_research_queries(
            folder_name,
            content_type="anime" if any(classification.possible_episode for classification in classifications.values()) else None,
            max_queries=config.automatic_review.research_max_queries,
        )
        adapter = research_adapter or BoundedResearchAdapter(
            limits=ResearchLimits(
                max_queries=config.automatic_review.research_max_queries,
                max_results_per_query=config.automatic_review.research_max_results_per_query,
                max_sources=config.automatic_review.research_max_sources,
                max_fetched_chars=config.automatic_review.research_max_fetched_chars,
                max_evidence_chars=config.automatic_review.research_max_evidence_chars,
                timeout_seconds=config.automatic_review.research_timeout_seconds,
            )
        )
        existing_packet = db.get_research_packet(job_id)
        if existing_packet:
            research_packet = DiscResearchPacket.from_dict(existing_packet)
            db.audit(
                "disc_research_reused",
                f"Reused existing research packet: {research_packet.status}",
                job_id,
                {
                    "status": research_packet.status,
                    "query_count": len(research_packet.queries),
                    "source_count": len(research_packet.sources),
                    "fact_count": len(research_packet.facts),
                },
            )
        else:
            research_packet = adapter.collect(queries)
            db.save_research_packet(job_id, research_packet.to_dict())
            db.audit(
                "disc_research",
                f"Research packet status: {research_packet.status}",
                job_id,
                {
                    "status": research_packet.status,
                    "query_count": len(research_packet.queries),
                    "source_count": len(research_packet.sources),
                    "fact_count": len(research_packet.facts),
                    "warnings": research_packet.warnings,
                },
            )
    if config.automatic_review.hermes_enabled:
        clear_automatic_review_labels(db, job_id)
        automatic_labels = {}
    hermes_labels: dict[str, AutomaticLabel] = {}
    if config.automatic_review.hermes_enabled and scanned_files:
        reviewer = hermes_review or request_hermes_bonus_review
        review_files = [
            (
                source_ids[scanned.path],
                scanned,
                local_labels.get(
                    scanned.path,
                    AutomaticLabel("extra", "Unclassified video", "unclassified", "awaiting Hermes review", 0.1),
                ),
            )
            for scanned in scanned_files
            if scanned.path in source_ids
        ]
        try:
            aggregate_relations = detect_aggregate_relations(
                [(source_ids[scanned.path], scanned) for scanned in scanned_files if scanned.path in source_ids]
            )
            media_evidence: dict[int, MediaEvidence] = {}
            for source_id, scanned, _ in review_files:
                ocr_text = None
                ocr_warning = None
                if text_extractor is not None:
                    try:
                        ocr_text = str(text_extractor(scanned) or "")[: config.automatic_review.ocr_max_chars]
                    except Exception as error:
                        ocr_warning = f"bounded OCR extraction failed: {type(error).__name__}"
                evidence = build_media_evidence(scanned, source_file_id=source_id, ocr_text=ocr_text, max_chars=config.automatic_review.ocr_max_chars)
                if ocr_warning:
                    evidence.warnings.append(ocr_warning)
                if scanned.subtitle_streams and all(
                    stream.codec in {"dvd_subtitle", "hdmv_pgs_subtitle", "dvb_subtitle", "xsub"}
                    for stream in scanned.subtitle_streams
                ):
                    evidence.warnings.append("image subtitle stream present; bounded text extraction requires OCR")
                media_evidence[source_id] = evidence
            if aggregate_relations:
                db.audit(
                    "aggregate_title_candidates",
                    f"Detected {len(aggregate_relations)} possible aggregate title relationship(s)",
                    job_id,
                    {
                        "relations": [
                            {
                                "aggregate_file_id": relation.aggregate_file_id,
                                "component_file_ids": list(relation.component_file_ids),
                                "duration_delta_seconds": relation.duration_delta_seconds,
                                "confidence": relation.confidence,
                            }
                            for relation in aggregate_relations
                        ]
                    },
                )
            candidate_inventory = facts_to_content_candidates(research_packet.facts) if research_packet else []
            release_ranking = None
            if research_packet and candidate_inventory:
                inventory = inventory_from_research_facts(
                    release_key=f"research:{job_id}",
                    title=folder_name,
                    facts=research_packet.facts,
                    warnings=["research-derived inventory; release edition is not independently confirmed"],
                )
                release_ranking = rank_release_inventories(scanned_files, [inventory])
                db.audit(
                    "research_inventory_discovered",
                    f"Discovered {sum(candidate.kind == 'episode' for candidate in inventory.candidates)} episode and {sum(candidate.kind == 'extra' for candidate in inventory.candidates)} extra candidate(s)",
                    job_id,
                    {
                        "candidate_count": len(inventory.candidates),
                        "episode_count": sum(candidate.kind == "episode" for candidate in inventory.candidates),
                        "extra_count": sum(candidate.kind == "extra" for candidate in inventory.candidates),
                        "source_count": len(inventory.sources),
                        "warnings": inventory.warnings,
                    },
                )
                ranking_payload = {
                    "matches": [
                        {
                            "release_key": match.release_key,
                            "title": match.title,
                            "score": match.score,
                            "confidence": match.confidence,
                            "components": match.components,
                            "warnings": list(match.warnings),
                        }
                        for match in release_ranking.matches
                    ],
                    "warnings": release_ranking.warnings,
                }
                db.save_release_ranking(job_id, ranking_payload)
                db.audit("release_ranking", "Stored research-derived release ranking", job_id, ranking_payload)
            hermes_labels = reviewer(
                job_id=job_id,
                disc_title=folder_name,
                files=review_files,
                command=config.automatic_review.hermes_command,
                timeout_seconds=config.automatic_review.hermes_timeout_seconds,
                batch_size=config.automatic_review.hermes_batch_size,
                ffmpeg_path=config.ffmpeg_path,
                aggregate_relations=aggregate_relations,
                media_evidence=media_evidence,
                candidate_inventory=candidate_inventory,
                research_packet=research_packet,
                release_ranking=release_ranking,
            )
            automatic_labels = hermes_labels
            if hermes_labels:
                db.audit(
                    "hermes_media_review",
                    f"Hermes returned labels for {len(hermes_labels)} file(s)",
                    job_id,
                    {"files": sorted(hermes_labels)},
                )
        except Exception as error:
            db.audit("hermes_media_review_failed", str(error), job_id)
    seed_automatic_review(db, config, job_id, scanned_files, classifications, automatic_labels)
    if automatic_labels:
        db.audit(
            "automatic_review_seed",
            f"Generated automatic review labels for {len(automatic_labels)} file(s)",
            job_id,
            {
                "files": {
                    path: {
                        "role": label.role,
                        "display_name": label.display_name,
                        "extra_type": label.extra_type,
                        "confidence": label.confidence,
                        "evidence": label.evidence,
                    }
                    for path, label in automatic_labels.items()
                }
            },
        )
    return automatic_labels


def scanned_files_for_job(db, job_id: int) -> tuple[list[ScannedFile], dict[str, Classification], dict[str, int]]:
    scanned_files: list[ScannedFile] = []
    classifications: dict[str, Classification] = {}
    source_ids: dict[str, int] = {}
    for row in db.source_file_payloads(job_id):
        audio = json.loads(row["audio_json"] or "[]")
        subtitles = json.loads(row["subtitle_json"] or "[]")
        scanned = ScannedFile(
            path=row["path"],
            filename=row["filename"],
            parent_disc_folder=row["parent_disc_folder"],
            size_bytes=row["size_bytes"],
            modified_time=row["modified_time"],
            duration_seconds=row["duration_seconds"],
            container_format=row["container_format"],
            video=VideoInfo(**json.loads(row["video_json"] or "{}")),
            audio_streams=[AudioStream(**item) for item in audio],
            subtitle_streams=[SubtitleStream(**item) for item in subtitles],
            chapter_count=row["chapter_count"],
            embedded_title=row["embedded_title"],
            makemkv_title=row["makemkv_title"],
        )
        scanned_files.append(scanned)
        source_ids[scanned.path] = int(row["id"])
        classification = json.loads(row.get("classification_json") or "{}")
        classifications[scanned.path] = Classification(**{key: value for key, value in classification.items() if key in Classification.__dataclass_fields__})
    if not classifications:
        classifications = classify_disc_files(scanned_files)
    return scanned_files, classifications, source_ids
