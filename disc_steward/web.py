from __future__ import annotations

import json
from html import escape
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .config import AppConfig
from .cleanup import plan_cleanup
from .db import Database
from .llm import request_suggestions
from .metadata import metadata_provider_status
from .models import AudioStream, Classification, FileReviewDecision, JobReviewMetadata, ScannedFile, SubtitleStream, VideoInfo
from .review import ReviewValidationError, classification_from_json, suggest_subtitle_policy, validate_review_ready
from .status import build_status_summary, format_status_summary
from .subtitle_planner import generate_subtitle_plan
from .transfer import transfer_job_to_eddy
from .validation import validate_job_outputs
from .work_orders import create_fileflows_work_orders, generate_final_paths


ROLE_CHOICES = [
    "main_feature",
    "episode",
    "extra",
    "trailer",
    "featurette",
    "deleted_scene",
    "interview",
    "music_video",
    "short_film",
    "promo",
    "alternate_cut",
    "commentary_variant",
    "menu_or_bumper",
    "ignore_candidate",
    "manual_review",
]
CONTENT_TYPES = ["movie", "show", "anime", "family_video", "extra", "unknown"]
LIBRARY_ROOTS = ["Movies", "Shows", "Anime", "Family Videos"]
GROUPS = [
    ("main", "Main Feature Candidates"),
    ("episodes", "Possible Episodes"),
    ("extras", "Possible Extras"),
    ("trailers", "Trailers/Promos"),
    ("featurettes", "Featurettes/Documentaries"),
    ("deleted", "Deleted Scenes"),
    ("menus", "Menu/Logo/Bumper Candidates"),
    ("manual", "Manual Review"),
]


def serve_review_ui(db: Database, config: AppConfig, host: str = "127.0.0.1", port: int = 8765) -> None:
    class Handler(ReviewRequestHandler):
        database = db
        app_config = config

    ThreadingHTTPServer((host, port), Handler).serve_forever()


class ReviewRequestHandler(BaseHTTPRequestHandler):
    database: Database
    app_config: AppConfig

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path in {"", "/"}:
            self._send_html(render_job_list(self.database, self.app_config))
            return
        if path.startswith("/jobs/"):
            job_id = _job_id_from_path(path)
            if job_id is None:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            self._send_html(render_job_review(self.database, self.app_config, job_id))
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        job_id = _job_id_from_path(path)
        if job_id is None:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        form = self._read_form()
        try:
            message = handle_job_action(self.database, self.app_config, job_id, path.rsplit("/", 1)[-1], form)
            self._redirect(f"/jobs/{job_id}?message={message}")
        except ReviewValidationError as error:
            self._send_html(render_job_review(self.database, self.app_config, job_id, errors=error.messages), HTTPStatus.BAD_REQUEST)
        except ValueError as error:
            self._send_html(render_job_review(self.database, self.app_config, job_id, errors=[str(error)]), HTTPStatus.BAD_REQUEST)

    def log_message(self, format: str, *args) -> None:
        return

    def _read_form(self) -> dict[str, str]:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length).decode("utf-8")
        parsed = parse_qs(raw, keep_blank_values=True)
        return {key: values[-1] for key, values in parsed.items()}

    def _send_html(self, body: str, status: HTTPStatus = HTTPStatus.OK) -> None:
        data = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _redirect(self, location: str) -> None:
        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", location)
        self.end_headers()


def _job_id_from_path(path: str) -> int | None:
    parts = [part for part in path.split("/") if part]
    if len(parts) < 2 or parts[0] != "jobs":
        return None
    try:
        return int(parts[1])
    except ValueError:
        return None


def handle_job_action(db: Database, config: AppConfig, job_id: int, action: str, form: dict[str, str]) -> str:
    if action in {"save", "mark-reviewed"}:
        status = "reviewed" if action == "mark-reviewed" else "review_in_progress"
        job_review, decisions = parse_review_form(db, config, job_id, form, status)
        paths = generate_final_paths(config, job_review, decisions)
        for decision in decisions:
            generated = paths.get(decision.source_file_id)
            if generated:
                decision.generated_final_path = str(generated.final_path)
                decision.conflicts = generated.conflicts
        if action == "mark-reviewed":
            validate_review_ready(job_review, decisions, paths)
        db.save_job_review(job_review)
        for decision in decisions:
            db.save_file_review(decision)
        db.audit(action, f"{action.replace('-', ' ').title()} for review", job_id)
        return "saved"
    if action == "create-work-orders":
        job_review, decisions = parse_review_form(db, config, job_id, form, "ready_for_fileflows")
        paths = generate_final_paths(config, job_review, decisions)
        validate_review_ready(job_review, decisions, paths)
        db.save_job_review(job_review)
        for decision in decisions:
            generated = paths.get(decision.source_file_id)
            if generated:
                decision.generated_final_path = str(generated.final_path)
                decision.conflicts = generated.conflicts
            db.save_file_review(decision)
        folder = create_fileflows_work_orders(db, config, job_id)
        return f"work-orders-created:{folder}"
    if action == "manual-review":
        review = db.get_job_review(job_id)
        review.review_status = "manual_review"
        db.save_job_review(review)
        db.audit("manual_review", "Sent job to manual review", job_id)
        return "manual-review"
    if action == "validate":
        summary = validate_job_outputs(db, config, job_id)
        return summary.status
    if action == "transfer":
        summary = transfer_job_to_eddy(db, config, job_id)
        return summary.status
    if action == "cleanup-plan":
        summary = plan_cleanup(db, config)
        return f"cleanup-plan:{len(summary.eligible)}-eligible"
    if action == "cleanup-hold":
        db.set_cleanup_hold(job_id, True, form.get("cleanup_hold_reason", "manual hold"))
        return "cleanup-hold-set"
    if action == "remove-cleanup-hold":
        db.set_cleanup_hold(job_id, False, "manual hold removed")
        return "cleanup-hold-removed"
    if action == "llm-suggestions":
        result = request_suggestions(db, config, job_id)
        return f"llm-suggestions:{len(result.get('suggestions', []))}"
    if action == "generate-subtitle-plans":
        decisions = {decision.source_file_id: decision for decision in db.list_file_reviews(job_id)}
        count = 0
        for row in db.source_file_payloads(job_id):
            decision = decisions.get(row["id"]) or FileReviewDecision(source_file_id=row["id"])
            audio = json.loads(row["audio_json"] or "[]")
            subtitles = json.loads(row["subtitle_json"] or "[]")
            source = _source_for_plan(row, audio, subtitles)
            plan = generate_subtitle_plan(source, decision.content_type, decision.subtitle_policy or "manual_review")
            db.save_subtitle_plan(row["id"], plan.__dict__)
            count += 1
        db.audit("subtitle_plans_generated", f"Generated {count} subtitle plan(s)", job_id)
        return f"subtitle-plans:{count}"
    if action == "manual-accept-output":
        if not config.validation_allow_manual_acceptance:
            raise ValueError("Manual validation acceptance is disabled in config")
        note = form.get("manual_acceptance_note", "").strip()
        source_file_id = int(form.get("source_file_id", "0"))
        if not note:
            raise ValueError("Manual acceptance requires a note")
        summary = db.latest_validation_summary(job_id)
        if not summary:
            raise ValueError("No validation result exists for this job")
        for item in summary.get("items", []):
            if int(item["source_file_id"]) == source_file_id:
                item["manually_accepted"] = True
                item["manual_acceptance_note"] = note
                item["status"] = "passed"
                item.setdefault("warnings", []).append(f"manually accepted: {note}")
                break
        else:
            raise ValueError(f"Unknown output source id: {source_file_id}")
        passed = all(item.get("status") == "passed" or item.get("manually_accepted") for item in summary.get("items", []))
        summary["passed"] = passed
        summary["status"] = "validated" if passed else "validation_failed"
        db.save_manual_override(job_id, source_file_id, "validation_acceptance", note)
        db.save_validation_summary(job_id, summary, passed)
        db.update_job_status(job_id, "transfer_ready" if passed else "validation_failed")
        db.audit("manual_output_acceptance", "Manually accepted validation output", job_id, {"source_file_id": source_file_id, "note": note})
        return "manual-output-accepted"
    if action == "reopen":
        review = db.get_job_review(job_id)
        review.review_status = "review_in_progress"
        db.save_job_review(review)
        db.audit("reopen_review", "Reopened review", job_id)
        return "reopened"
    raise ValueError(f"Unknown action: {action}")


def parse_review_form(
    db: Database,
    config: AppConfig,
    job_id: int,
    form: dict[str, str],
    status: str,
) -> tuple[JobReviewMetadata, list[FileReviewDecision]]:
    def text(name: str) -> str | None:
        value = form.get(name, "").strip()
        return value or None

    def integer(name: str) -> int | None:
        value = text(name)
        return int(value) if value is not None else None

    job_review = JobReviewMetadata(
        job_id=job_id,
        title=text("title") or "",
        original_title=text("original_title"),
        romanized_title=text("romanized_title"),
        translated_title=text("translated_title"),
        language_script_hints=text("language_script_hints"),
        anime_flag=form.get("anime_flag") == "on",
        japanese_media_flag=form.get("japanese_media_flag") == "on",
        year=integer("year"),
        content_type=text("content_type") or "unknown",
        library_root=text("library_root") or "Movies",
        imdb_id=text("imdb_id"),
        tmdb_id=text("tmdb_id"),
        tvdb_id=text("tvdb_id"),
        anidb_id=text("anidb_id"),
        anilist_id=text("anilist_id"),
        mal_id=text("mal_id"),
        notes=text("notes"),
        review_status=status,
    )
    decisions: list[FileReviewDecision] = []
    for row in db.source_file_payloads(job_id):
        source_id = row["id"]
        prefix = f"file_{source_id}_"
        decisions.append(
            FileReviewDecision(
                source_file_id=source_id,
                include_in_work_order=form.get(prefix + "include") == "on",
                role=text(prefix + "role") or "",
                content_type=text(prefix + "content_type") or job_review.content_type,
                final_display_name=text(prefix + "final_display_name"),
                final_filename=text(prefix + "final_filename"),
        original_title=text(prefix + "original_title"),
        translated_title=text(prefix + "translated_title"),
        romanized_title=text(prefix + "romanized_title"),
                imdb_id=text(prefix + "imdb_id"),
                tmdb_id=text(prefix + "tmdb_id"),
                tvdb_id=text(prefix + "tvdb_id"),
                anidb_id=text(prefix + "anidb_id"),
                anilist_id=text(prefix + "anilist_id"),
                mal_id=text(prefix + "mal_id"),
                extra_type=text(prefix + "extra_type"),
                season_number=integer(prefix + "season_number"),
                episode_number=integer(prefix + "episode_number"),
                sort_order=integer(prefix + "sort_order"),
                encoding_profile=text(prefix + "encoding_profile") or config.preferred_video_profile,
                subtitle_policy=text(prefix + "subtitle_policy") or "manual_review",
                notes=text(prefix + "notes"),
            )
        )
    return job_review, decisions


def render_job_list(db: Database, config: AppConfig) -> str:
    rows = db.list_job_summaries()
    table_rows = "\n".join(
        f"""
        <tr>
          <td><a href="/jobs/{row['id']}">{row['id']}</a></td>
          <td>{escape(row['status'] or '')}</td>
          <td>{escape(row['disc_title'] or '')}</td>
          <td>{row['scanned_file_count'] or 0}</td>
          <td>{escape(row['likely_main_feature'] or '')}</td>
          <td>{row['extra_count'] or 0}</td>
          <td>{row['subtitle_issue_count'] or 0}</td>
          <td>{row['transcode_risk_count'] or 0}</td>
          <td>{escape(row['review_status'] or '')}</td>
        </tr>
        """
        for row in rows
    )
    return page(
        "Disc Steward Review",
        f"""
        <h1>Disc Steward Review</h1>
        {render_dashboard(db, config)}
        <table>
          <thead>
            <tr>
              <th>Job</th><th>Status</th><th>Disc Folder</th><th>Files</th><th>Likely Main Feature</th>
              <th>Extras</th><th>Subtitle Issues</th><th>Transcode Risks</th><th>Review</th>
            </tr>
          </thead>
          <tbody>{table_rows}</tbody>
        </table>
        """,
    )


def render_job_review(
    db: Database,
    config: AppConfig,
    job_id: int,
    errors: list[str] | None = None,
) -> str:
    job = db.get_job(job_id)
    if job is None:
        return page("Missing Job", "<h1>Missing job</h1>")
    job_review = db.get_job_review(job_id)
    saved_decisions = {decision.source_file_id: decision for decision in db.list_file_reviews(job_id)}
    rows = db.source_file_payloads(job_id)
    decisions = [_decision_for_row(config, job_review, row, saved_decisions.get(row["id"])) for row in rows]
    paths = generate_final_paths(config, job_review, decisions)
    grouped = {key: [] for key, _ in GROUPS}
    for row, decision in zip(rows, decisions, strict=False):
        grouped[_group_for(classification_from_json(row.get("classification_json")))].append((row, decision))
    error_html = ""
    if errors:
        error_html = "<div class='errors'>" + "".join(f"<p>{escape(error)}</p>" for error in errors) + "</div>"
    groups_html = "\n".join(
        f"<section><h2>{label}</h2>{''.join(render_file_card(config, row, decision, paths.get(decision.source_file_id)) for row, decision in grouped[key]) or '<p class=\"muted\">No files in this group.</p>'}</section>"
        for key, label in GROUPS
    )
    return page(
        f"Review Job {job_id}",
        f"""
        <p><a href="/">Back to jobs</a></p>
        <h1>{escape(job.disc_title)}</h1>
        <p class="muted">Job {job.id} · {escape(job.status)} · Controller path: <code>{escape(job.disc_path)}</code></p>
        {error_html}
        <form method="post" action="/jobs/{job_id}/save">
          {render_job_fields(config, job_review)}
          {groups_html}
          <div class="actions">
            <button formaction="/jobs/{job_id}/save">Save draft review</button>
            <button formaction="/jobs/{job_id}/mark-reviewed">Mark reviewed</button>
            <button formaction="/jobs/{job_id}/create-work-orders">Create FileFlows work orders</button>
            <button formaction="/jobs/{job_id}/manual-review">Send job to manual review</button>
            <button formaction="/jobs/{job_id}/reopen">Reopen review</button>
          </div>
        </form>
        {render_phase3_sections(db, job_id)}
        {render_phase4_sections(db, config, job_id)}
        """,
    )


def _decision_for_row(
    config: AppConfig,
    job_review: JobReviewMetadata,
    row: dict,
    saved: FileReviewDecision | None,
) -> FileReviewDecision:
    classification = classification_from_json(row.get("classification_json"))
    audio = json.loads(row["audio_json"] or "[]")
    subtitles = json.loads(row["subtitle_json"] or "[]")
    suggestion = suggest_subtitle_policy(
        classification,
        [stream.get("language") for stream in audio],
        [stream.get("codec") for stream in subtitles],
    )
    if saved:
        if not saved.encoding_profile:
            saved.encoding_profile = config.preferred_video_profile
        if not saved.subtitle_policy:
            saved.subtitle_policy = suggestion.policy
        return saved
    return FileReviewDecision(
        source_file_id=row["id"],
        include_in_work_order=True,
        role=_suggest_role(classification),
        content_type=job_review.content_type,
        final_display_name=Path(row["filename"]).stem,
        encoding_profile=config.preferred_video_profile,
        subtitle_policy=suggestion.policy,
        warnings=suggestion.warnings,
    )


def _suggest_role(classification: Classification) -> str:
    if classification.probable_main_feature:
        return "main_feature"
    if classification.possible_episode:
        return "episode"
    if classification.probable_trailer:
        return "trailer"
    if classification.probable_featurette:
        return "featurette"
    if classification.probable_deleted_scene:
        return "deleted_scene"
    if classification.probable_menu_or_bumper:
        return "menu_or_bumper"
    if classification.probable_extra:
        return "extra"
    if classification.manual_review_required:
        return "manual_review"
    return ""


def _group_for(classification: Classification) -> str:
    if classification.probable_main_feature:
        return "main"
    if classification.possible_episode:
        return "episodes"
    if classification.probable_trailer:
        return "trailers"
    if classification.probable_featurette:
        return "featurettes"
    if classification.probable_deleted_scene:
        return "deleted"
    if classification.probable_menu_or_bumper:
        return "menus"
    if classification.probable_extra:
        return "extras"
    return "manual"


def render_job_fields(config: AppConfig, review: JobReviewMetadata) -> str:
    metadata_status = metadata_provider_status(config.metadata)
    return f"""
    <fieldset>
      <legend>Disc Metadata</legend>
      <label>Title <input name="title" value="{escape(review.title)}"></label>
      <label>Original title <input name="original_title" value="{escape(review.original_title or '')}"></label>
      <label>Romanized title <input name="romanized_title" value="{escape(review.romanized_title or '')}"></label>
      <label>Translated title <input name="translated_title" value="{escape(review.translated_title or '')}"></label>
      <label>Language/script hints <input name="language_script_hints" value="{escape(review.language_script_hints or '')}"></label>
      <label><input type="checkbox" name="anime_flag" {"checked" if review.anime_flag else ""}> Anime</label>
      <label><input type="checkbox" name="japanese_media_flag" {"checked" if review.japanese_media_flag else ""}> Japanese media</label>
      <label>Year <input name="year" value="{escape(str(review.year or ''))}" inputmode="numeric"></label>
      <label>Content type {select("content_type", CONTENT_TYPES, review.content_type)}</label>
      <label>Library root {select("library_root", list(config.eddy_library_roots.keys()) or LIBRARY_ROOTS, review.library_root)}</label>
      <label>IMDb ID <input name="imdb_id" value="{escape(review.imdb_id or '')}"></label>
      <label>TMDb ID <input name="tmdb_id" value="{escape(review.tmdb_id or '')}"></label>
      <label>TVDb ID <input name="tvdb_id" value="{escape(review.tvdb_id or '')}"></label>
      <label>AniDB ID <input name="anidb_id" value="{escape(review.anidb_id or '')}"></label>
      <label>AniList ID <input name="anilist_id" value="{escape(review.anilist_id or '')}"></label>
      <label>MAL ID <input name="mal_id" value="{escape(review.mal_id or '')}"></label>
      <p class="wide muted">Metadata lookup: {'enabled' if metadata_status['enabled'] else 'disabled'} · LLM/Hermes: {'enabled' if config.llm.enabled else 'disabled'}</p>
      <label class="wide">Notes <textarea name="notes">{escape(review.notes or '')}</textarea></label>
    </fieldset>
    """


def render_file_card(config: AppConfig, row: dict, decision: FileReviewDecision, generated) -> str:
    source_id = row["id"]
    prefix = f"file_{source_id}_"
    classification = classification_from_json(row.get("classification_json"))
    video = json.loads(row["video_json"] or "{}")
    audio = json.loads(row["audio_json"] or "[]")
    subtitles = json.loads(row["subtitle_json"] or "[]")
    source_for_plan = _source_for_plan(row, audio, subtitles)
    subtitle_plan = generate_subtitle_plan(source_for_plan, decision.content_type, decision.subtitle_policy)
    issues = _issues(classification)
    final_path = escape(str(generated.final_path)) if generated else ""
    controller_final_path = ""
    if generated and generated.controller_path and Path(generated.final_path) != generated.controller_path:
        controller_final_path = f"<p><strong>Gospel final placement path:</strong> <code>{escape(str(generated.controller_path))}</code></p>"
    conflicts = generated.conflicts if generated else []
    return f"""
    <article class="file-card">
      <h3>{escape(row['filename'])}</h3>
      <p class="muted"><strong>Controller path:</strong> <code>{escape(row['path'])}</code></p>
      {_mapped_path_line('Barnabas path', config.to_barnabas_path(Path(row['path'])), Path(row['path']))}
      <div class="tech">
        <span>Duration: {format_duration(row['duration_seconds'])}</span>
        <span>Resolution: {video.get('width') or '?'}x{video.get('height') or '?'}</span>
        <span>Video: {escape(' / '.join(str(video.get(key) or '') for key in ('codec', 'profile', 'pixel_format')).strip(' / '))}</span>
        <span>Audio: {escape(format_streams(audio))}</span>
        <span>Subtitles: {escape(format_streams(subtitles))}</span>
        <span>Chapters: {row['chapter_count']}</span>
        <span>Size: {format_size(row['size_bytes'])}</span>
        <span>Confidence: {classification.confidence:.2f}</span>
      </div>
      <p><strong>Reasons:</strong> {escape('; '.join(classification.reasons) or 'None recorded')}</p>
      <p><strong>Issues:</strong> {escape('; '.join(issues) or 'None detected')}</p>
      {"<p class='errors'>Japanese/anime content detected. Review title and subtitle handling.</p>" if subtitle_plan.japanese_or_anime else ""}
      <p><strong>Subtitle plan:</strong> {escape(', '.join(subtitle_plan.statuses))}</p>
      {"<p class='errors'>" + escape('; '.join(subtitle_plan.warnings)) + "</p>" if subtitle_plan.warnings else ""}
      <p><strong>Final destination preview:</strong> <code>{final_path}</code></p>
      {controller_final_path}
      {"<p class='errors'>" + escape('; '.join(conflicts)) + "</p>" if conflicts else ""}
      <div class="file-fields">
        <label><input type="checkbox" name="{prefix}include" {"checked" if decision.include_in_work_order else ""}> Include in FileFlows</label>
        <label>Role {select(prefix + "role", ROLE_CHOICES, decision.role, blank=True)}</label>
        <label>Display name <input name="{prefix}final_display_name" value="{escape(decision.final_display_name or '')}"></label>
        <label>Final filename <input name="{prefix}final_filename" value="{escape(decision.final_filename or '')}"></label>
        <label>Content type {select(prefix + "content_type", CONTENT_TYPES, decision.content_type)}</label>
        <label>Extra type <input name="{prefix}extra_type" value="{escape(decision.extra_type or '')}"></label>
        <label>Season <input name="{prefix}season_number" value="{escape(str(decision.season_number if decision.season_number is not None else ''))}" inputmode="numeric"></label>
        <label>Episode <input name="{prefix}episode_number" value="{escape(str(decision.episode_number if decision.episode_number is not None else ''))}" inputmode="numeric"></label>
        <label>Sort order <input name="{prefix}sort_order" value="{escape(str(decision.sort_order if decision.sort_order is not None else ''))}" inputmode="numeric"></label>
        <label>Encoding profile {select(prefix + "encoding_profile", config.encoding_profiles, decision.encoding_profile)}</label>
        <label>Subtitle policy {select(prefix + "subtitle_policy", config.subtitle_policies, decision.subtitle_policy)}</label>
        <label>Original title <input name="{prefix}original_title" value="{escape(decision.original_title or '')}"></label>
        <label>Translated title <input name="{prefix}translated_title" value="{escape(decision.translated_title or '')}"></label>
        <label>Romanized title <input name="{prefix}romanized_title" value="{escape(decision.romanized_title or '')}"></label>
        <label>IMDb ID <input name="{prefix}imdb_id" value="{escape(decision.imdb_id or '')}"></label>
        <label>TMDb ID <input name="{prefix}tmdb_id" value="{escape(decision.tmdb_id or '')}"></label>
        <label>TVDb ID <input name="{prefix}tvdb_id" value="{escape(decision.tvdb_id or '')}"></label>
        <label>AniDB ID <input name="{prefix}anidb_id" value="{escape(decision.anidb_id or '')}"></label>
        <label>AniList ID <input name="{prefix}anilist_id" value="{escape(decision.anilist_id or '')}"></label>
        <label>MAL ID <input name="{prefix}mal_id" value="{escape(decision.mal_id or '')}"></label>
        <label class="wide">Notes <textarea name="{prefix}notes">{escape(decision.notes or '')}</textarea></label>
      </div>
    </article>
    """


def _mapped_path_line(label: str, mapped: Path, original: Path) -> str:
    if mapped == original:
        return ""
    return f"<p class='muted'><strong>{escape(label)}:</strong> <code>{escape(str(mapped))}</code></p>"


def render_phase3_sections(db: Database, job_id: int) -> str:
    validation = db.latest_validation_summary(job_id)
    transfer = db.latest_transfer_summary(job_id)
    validation_html = render_validation_section(job_id, validation)
    transfer_html = render_transfer_section(job_id, validation, transfer)
    return f"{validation_html}{transfer_html}"


def render_phase4_sections(db: Database, config: AppConfig, job_id: int) -> str:
    suggestions = db.list_llm_suggestions(job_id)
    suggestion_rows = "".join(
        f"<tr><td>{escape(item.get('type', ''))}</td><td>{escape(item.get('status', ''))}</td><td><code>{escape(str({k: v for k, v in item.items() if k not in {'id', 'status', 'created_at'}}))}</code></td></tr>"
        for item in suggestions
    )
    cleanup_hold = db.has_cleanup_hold(job_id)
    cleanup_rows = "".join(
        f"<tr><td>{escape(item['item_type'])}</td><td>{'yes' if item['eligible'] else 'no'}</td><td><code>{escape(item['path'])}</code></td><td>{escape(item['reason'])}</td></tr>"
        for item in db.list_cleanup_eligibility(job_id)
    )
    return f"""
    <section class="ops">
      <h2>Phase 4 Assistance</h2>
      <p>Metadata providers: <strong>{'enabled' if config.metadata.enabled else 'disabled'}</strong> · LLM/Hermes: <strong>{'enabled' if config.llm.enabled else 'disabled'}</strong> · Cleanup: <strong>{'enabled' if config.cleanup.enabled else 'disabled'}</strong> ({'dry-run' if config.cleanup.dry_run else 'live'})</p>
      <form method="post" action="/jobs/{job_id}/llm-suggestions" class="inline-form">
        <button {'disabled' if not config.llm.enabled else ''}>Request LLM suggestions</button>
      </form>
      <form method="post" action="/jobs/{job_id}/generate-subtitle-plans" class="inline-form"><button>Generate subtitle plan</button></form>
      <table>
        <thead><tr><th>Suggestion</th><th>Status</th><th>Payload</th></tr></thead>
        <tbody>{suggestion_rows or '<tr><td colspan="3">No LLM suggestions stored.</td></tr>'}</tbody>
      </table>
      <h3>Cleanup</h3>
      <p>Cleanup hold: <strong>{'on' if cleanup_hold else 'off'}</strong></p>
      <form method="post" action="/jobs/{job_id}/cleanup-plan" class="inline-form"><button>Generate cleanup plan</button></form>
      <form method="post" action="/jobs/{job_id}/cleanup-hold" class="inline-form">
        <label>Hold reason <input name="cleanup_hold_reason" value="manual hold"></label>
        <button>Mark job as cleanup hold</button>
      </form>
      <form method="post" action="/jobs/{job_id}/remove-cleanup-hold" class="inline-form"><button>Remove cleanup hold</button></form>
      <table>
        <thead><tr><th>Type</th><th>Eligible</th><th>Path</th><th>Reason</th></tr></thead>
        <tbody>{cleanup_rows or '<tr><td colspan="4">No cleanup plan recorded.</td></tr>'}</tbody>
      </table>
    </section>
    """


def render_dashboard(db: Database, config: AppConfig) -> str:
    try:
        summary = build_status_summary(db, config)
    except Exception:
        return ""
    return f"<pre class='ops'>{escape(format_status_summary(summary))}</pre>"


def render_validation_section(job_id: int, summary: dict | None) -> str:
    if summary is None:
        body = "<p class='muted'>No FileFlows output validation has been recorded.</p>"
    else:
        item_rows = "".join(render_validation_item(job_id, item) for item in summary.get("items", []))
        warnings = "".join(f"<p class='errors'>{escape(warning)}</p>" for warning in summary.get("warnings", []))
        body = f"""
        <p>Status: <strong>{escape(summary.get('status', 'unknown'))}</strong></p>
        {warnings}
        <table>
          <thead><tr><th>Source</th><th>Status</th><th>Expected</th><th>Matched</th><th>Profile</th><th>Warnings / Errors</th></tr></thead>
          <tbody>{item_rows}</tbody>
        </table>
        """
    return f"""
    <section class="ops">
      <h2>FileFlows Output Validation</h2>
      {body}
      <form method="post" action="/jobs/{job_id}/validate">
        <button>Run validation for this job</button>
      </form>
    </section>
    """


def render_validation_item(job_id: int, item: dict) -> str:
    warnings = [f"warning: {value}" for value in item.get("warnings", [])]
    errors = [f"error: {value}" for value in item.get("errors", [])]
    compliance = ", ".join(f"{key}: {value}" for key, value in (item.get("profile_compliance") or {}).items())
    manual_form = ""
    if item.get("status") == "failed":
        manual_form = f"""
        <form method="post" action="/jobs/{job_id}/manual-accept-output" class="inline-form">
          <input type="hidden" name="source_file_id" value="{int(item['source_file_id'])}">
          <label>Acceptance note <input name="manual_acceptance_note" required></label>
          <button>Manually accept</button>
        </form>
        """
    return f"""
    <tr>
      <td>{int(item['source_file_id'])}</td>
      <td>{escape(item.get('status', ''))}{"<br>manual" if item.get('manually_accepted') else ""}</td>
      <td><code>{escape(item.get('expected_output_name') or '')}</code><br><code>{escape(item.get('expected_final_path') or '')}</code></td>
      <td><code>{escape(item.get('matched_output_path') or '')}</code></td>
      <td>{escape(compliance)}</td>
      <td>{escape('; '.join([*warnings, *errors]) or 'none')}{manual_form}</td>
    </tr>
    """


def render_transfer_section(job_id: int, validation: dict | None, summary: dict | None) -> str:
    ready = validation is not None and bool(validation.get("passed"))
    if summary is None:
        body = "<p class='muted'>No Eddy transfer has been recorded.</p>"
    else:
        rows = "".join(
            f"""
            <tr>
              <td>{int(item['source_file_id'])}</td>
              <td>{escape(item.get('status', ''))}</td>
              <td><code>{escape(item.get('incoming_path') or '')}</code></td>
              <td><code>{escape(item.get('final_path') or '')}</code></td>
              <td>{escape(item.get('conflict') or item.get('error') or '')}</td>
            </tr>
            """
            for item in summary.get("items", [])
        )
        body = f"""
        <p>Status: <strong>{escape(summary.get('status', 'unknown'))}</strong></p>
        <table>
          <thead><tr><th>Source</th><th>Status</th><th>Eddy Incoming</th><th>Final Eddy Path</th><th>Conflict / Error</th></tr></thead>
          <tbody>{rows}</tbody>
        </table>
        """
    return f"""
    <section class="ops">
      <h2>Eddy Transfer and Import</h2>
      <p>Readiness: <strong>{'ready' if ready else 'not ready'}</strong></p>
      {body}
      <form method="post" action="/jobs/{job_id}/transfer">
        <button {'disabled' if not ready else ''}>Transfer validated outputs</button>
      </form>
    </section>
    """


def _issues(classification: Classification) -> list[str]:
    issues = []
    for attr, label in [
        ("needs_video_encode", "video encode likely needed"),
        ("needs_audio_fallback", "AAC fallback missing"),
        ("needs_subtitle_conversion", "subtitle conversion likely needed"),
        ("needs_subtitle_generation", "subtitle generation may be needed"),
        ("image_subtitle_is_default", "default image subtitle"),
        ("missing_language_tags", "missing language tags"),
        ("likely_jellyfin_transcode_risk", "Jellyfin transcode risk"),
    ]:
        if getattr(classification, attr):
            issues.append(label)
    return issues


def select(name: str, options: list[str], selected: str | None, blank: bool = False) -> str:
    values = [""] + options if blank else options
    html = [f'<select name="{escape(name)}">']
    for option in values:
        html.append(f'<option value="{escape(option)}" {"selected" if option == (selected or "") else ""}>{escape(option or "-")}</option>')
    html.append("</select>")
    return "".join(html)


def format_duration(value: float | None) -> str:
    if value is None:
        return "unknown"
    hours = int(value // 3600)
    minutes = int((value % 3600) // 60)
    seconds = int(value % 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def format_size(value: int) -> str:
    size = float(value)
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if size < 1024 or unit == "TB":
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{value} B"


def format_streams(streams: list[dict]) -> str:
    if not streams:
        return "none"
    return ", ".join(
        " ".join(str(part) for part in [stream.get("language") or "und", stream.get("codec"), stream.get("channel_layout")] if part)
        for stream in streams
    )


def _source_for_plan(row: dict, audio: list[dict], subtitles: list[dict]) -> ScannedFile:
    return ScannedFile(
        path=row["path"],
        filename=row["filename"],
        parent_disc_folder=row["parent_disc_folder"],
        size_bytes=row["size_bytes"],
        modified_time=row["modified_time"],
        duration_seconds=row["duration_seconds"],
        container_format=row["container_format"],
        video=VideoInfo(**json.loads(row["video_json"] or "{}")),
        audio_streams=[AudioStream(**stream) for stream in audio],
        subtitle_streams=[SubtitleStream(**stream) for stream in subtitles],
        chapter_count=row["chapter_count"],
        embedded_title=row["embedded_title"],
        makemkv_title=row["makemkv_title"],
        raw_ffprobe={},
    )


def page(title: str, body: str) -> str:
    return f"""<!doctype html>
    <html lang="en">
    <head>
      <meta charset="utf-8">
      <meta name="viewport" content="width=device-width, initial-scale=1">
      <title>{escape(title)}</title>
      <style>
        :root {{ color-scheme: light; font-family: system-ui, sans-serif; }}
        body {{ margin: 0; background: #f6f7f8; color: #1f2933; }}
        h1, h2, h3 {{ margin: 0.8rem 0 0.4rem; }}
        main {{ max-width: 1280px; margin: 0 auto; padding: 24px; }}
        table {{ width: 100%; border-collapse: collapse; background: white; }}
        th, td {{ border-bottom: 1px solid #d7dde3; padding: 8px; text-align: left; vertical-align: top; }}
        fieldset, .file-card {{ border: 1px solid #d7dde3; background: white; border-radius: 6px; margin: 16px 0; padding: 16px; }}
        .ops {{ border: 1px solid #d7dde3; background: white; border-radius: 6px; margin: 16px 0; padding: 16px; }}
        label {{ display: flex; flex-direction: column; gap: 4px; font-size: 0.9rem; }}
        input, select, textarea, button {{ font: inherit; padding: 7px; }}
        textarea {{ min-height: 70px; }}
        .file-fields, fieldset {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 12px; }}
        .wide {{ grid-column: 1 / -1; }}
        .tech {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 6px; font-size: 0.9rem; }}
        .actions {{ position: sticky; bottom: 0; background: #edf1f5; border-top: 1px solid #cbd4dd; padding: 12px; display: flex; flex-wrap: wrap; gap: 8px; }}
        .inline-form {{ display: flex; align-items: end; gap: 8px; margin-top: 8px; flex-wrap: wrap; }}
        .errors {{ color: #9f1d20; background: #fff2f2; border: 1px solid #f0b8b8; border-radius: 6px; padding: 8px; }}
        .muted {{ color: #5c6975; }}
        code {{ word-break: break-all; }}
      </style>
    </head>
    <body><main>{body}</main></body>
    </html>"""
