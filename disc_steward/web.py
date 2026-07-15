from __future__ import annotations

import json
import mimetypes
import shutil
import subprocess
import threading
import time
from collections import Counter
from dataclasses import replace
from html import escape
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .config import AppConfig
from .cleanup import plan_cleanup
from .db import Database
from .llm import request_suggestions
from .metadata import apply_stored_metadata_candidate, lookup_file_metadata, lookup_job_metadata, metadata_provider_status
from .models import AudioStream, Classification, FileReviewDecision, GeneratedPath, JobReviewMetadata, ScannedFile, SubtitleStream, VideoInfo
from .review import ReviewValidationError, classification_from_json, suggest_subtitle_policy, validate_review_ready
from .status import build_status_summary, format_status_summary
from .scanner import scan_completed_rips, scan_disc_folder, watch_completed_rips
from .subtitle_planner import generate_subtitle_plan
from .transfer import transfer_job_to_eddy
from .validation import validate_job_outputs
from .preview import queue_previews_for_job, queue_preview_for_source_row
from .work_orders import create_ffmpeg_processing_jobs, create_fileflows_work_orders, generate_final_paths


LOGO_PATH = Path(__file__).resolve().parent.parent / "disc-steward-logo.png"
STATIC_DIR = Path(__file__).resolve().parent / "static"
DESIGN_SYSTEM_STYLESHEET = STATIC_DIR / "win31-core.css"
MOTION_STYLESHEET = STATIC_DIR / "win31-motion.css"


def _favicon_href() -> str:
    if not LOGO_PATH.exists():
        return "/favicon.ico"
    return f"/favicon.ico?v={LOGO_PATH.stat().st_mtime_ns}"


def _design_system_stylesheet_href() -> str:
    if not DESIGN_SYSTEM_STYLESHEET.exists():
        return "/static/win31-core.css"
    return f"/static/win31-core.css?v={DESIGN_SYSTEM_STYLESHEET.stat().st_mtime_ns}"


def _motion_stylesheet_href() -> str:
    if not MOTION_STYLESHEET.exists():
        return "/static/win31-motion.css"
    return f"/static/win31-motion.css?v={MOTION_STYLESHEET.stat().st_mtime_ns}"

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


def _job_source_path(job) -> str:
    return job.source_disc_path or job.disc_path


def serve_review_ui(db: Database, config: AppConfig, host: str = "127.0.0.1", port: int = 8765) -> None:
    ThreadingHTTPServer((host, port), make_review_handler(db, config)).serve_forever()


def make_review_handler(db: Database, config: AppConfig):
    class Handler(ReviewRequestHandler):
        database = db
        app_config = config

    return Handler


class ReviewRequestHandler(BaseHTTPRequestHandler):
    database: Database
    app_config: AppConfig

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path in {"", "/"}:
            self._send_html(render_job_list(self.database, self.app_config))
            return
        if path == "/favicon.ico":
            self._send_favicon()
            return
        if path == "/static/win31-core.css":
            self._send_design_system_stylesheet()
            return
        if path == "/static/win31-motion.css":
            self._send_motion_stylesheet()
            return
        if path.startswith("/media/"):
            source_id, variant = _source_id_and_variant_from_media_path(path)
            if source_id is None:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            if variant == "thumbnail":
                self._send_media_thumbnail(source_id)
                return
            if variant == "preview":
                self._send_media_preview(source_id)
                return
            self._send_media(source_id)
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
        if path == "/ignored/unignore":
            form = self._read_form()
            try:
                message = handle_ignored_action(self.database, self.app_config, "unignore", form)
                if message.startswith("redirect:"):
                    self._redirect(message.removeprefix("redirect:"))
                    return
                self._redirect(f"/?message={message}")
            except ValueError as error:
                self.send_error(HTTPStatus.BAD_REQUEST, str(error))
            return
        if path == "/ignored/open-folder":
            form = self._read_form()
            try:
                message = handle_ignored_action(self.database, self.app_config, "open-folder", form)
                if message.startswith("redirect:"):
                    self._redirect(message.removeprefix("redirect:"))
                    return
                self._redirect(f"/?message={message}")
            except ValueError as error:
                self.send_error(HTTPStatus.BAD_REQUEST, str(error))
            return
        job_id = _job_id_from_path(path)
        if job_id is None:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        form = self._read_form()
        try:
            message = handle_job_action(self.database, self.app_config, job_id, path.rsplit("/", 1)[-1], form)
            if message.startswith("redirect:"):
                self._redirect(message.removeprefix("redirect:"))
                return
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

    def _send_favicon(self) -> None:
        if not LOGO_PATH.exists():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        data = LOGO_PATH.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "image/png")
        self.send_header("Cache-Control", "no-cache, max-age=0, must-revalidate")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_design_system_stylesheet(self) -> None:
        if not DESIGN_SYSTEM_STYLESHEET.exists():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        data = DESIGN_SYSTEM_STYLESHEET.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/css; charset=utf-8")
        self.send_header("Cache-Control", "public, max-age=3600")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_motion_stylesheet(self) -> None:
        if not MOTION_STYLESHEET.exists():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        data = MOTION_STYLESHEET.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/css; charset=utf-8")
        self.send_header("Cache-Control", "public, max-age=3600")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_media(self, source_file_id: int) -> None:
        row = self.database.source_file_payload(source_file_id)
        if row is None:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        path = _media_path_for(self.app_config, row)
        if not path.exists() or not path.is_file():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        size = path.stat().st_size
        start, end, partial = _parse_range(self.headers.get("Range"), size)
        content_length = end - start + 1
        self.send_response(HTTPStatus.PARTIAL_CONTENT if partial else HTTPStatus.OK)
        self.send_header("Content-Type", mimetypes.guess_type(path.name)[0] or "video/x-matroska")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(content_length))
        if partial:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        _write_media_range(path, self.wfile, start, content_length)

    def _send_media_thumbnail(self, source_file_id: int) -> None:
        row = self.database.source_file_payload(source_file_id)
        if row is None:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        mime_type, data = _media_thumbnail_bytes(self.app_config, row)
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", mime_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_media_preview(self, source_file_id: int) -> None:
        row = self.database.source_file_payload(source_file_id)
        if row is None:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        preview_path = row.get("preview_path")
        if not preview_path:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        path = Path(preview_path)
        if not path.exists() or not path.is_file() or row.get("preview_status") not in {"queued", "processing", "ready"}:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        size = path.stat().st_size
        start, end, partial = _parse_range(self.headers.get("Range"), size)
        content_length = end - start + 1
        self.send_response(HTTPStatus.PARTIAL_CONTENT if partial else HTTPStatus.OK)
        self.send_header("Content-Type", mimetypes.guess_type(path.name)[0] or "video/mp4")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(content_length))
        if partial:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        _write_media_range(path, self.wfile, start, content_length)


def _job_id_from_path(path: str) -> int | None:
    parts = [part for part in path.split("/") if part]
    if len(parts) < 2 or parts[0] != "jobs":
        return None
    try:
        return int(parts[1])
    except ValueError:
        return None


def _source_id_and_variant_from_media_path(path: str) -> tuple[int | None, str | None]:
    parts = [part for part in path.split("/") if part]
    if len(parts) == 2 and parts[0] == "media":
        try:
            return int(parts[1]), None
        except ValueError:
            return None, None
    if len(parts) == 3 and parts[0] == "media":
        try:
            source_id = int(parts[1])
        except ValueError:
            return None, None
        return source_id, parts[2]
    return None, None


def _media_path_for(config: AppConfig, row: dict) -> Path:
    mapped = config.to_barnabas_path(Path(row["path"]))
    return mapped if mapped.exists() else Path(row["path"])


def _media_thumbnail_bytes(config: AppConfig, row: dict) -> tuple[str, bytes]:
    path = _media_path_for(config, row)
    if not path.exists() or not path.is_file():
        return "image/svg+xml", _missing_thumbnail_svg(row.get("filename") or path.name, "Media file not found")
    ffmpeg = getattr(config, "ffmpeg_path", "ffmpeg")
    timestamp = _thumbnail_timestamp_seconds(row.get("duration_seconds"))
    command = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-ss",
        f"{timestamp:.2f}",
        "-i",
        str(path),
        "-frames:v",
        "1",
        "-vf",
        "scale=640:-1",
        "-f",
        "image2pipe",
        "-vcodec",
        "png",
        "pipe:1",
    ]
    try:
        completed = subprocess.run(command, check=True, capture_output=True)
        if completed.stdout:
            return "image/png", completed.stdout
    except (FileNotFoundError, subprocess.CalledProcessError):
        pass
    return "image/svg+xml", _missing_thumbnail_svg(row.get("filename") or path.name, "Preview unavailable")


def _thumbnail_timestamp_seconds(duration: float | None) -> float:
    if isinstance(duration, (int, float)) and duration > 0:
        if duration <= 6:
            return max(1.0, duration / 2)
        return max(1.0, min(duration * 0.15, duration - 3.0))
    return 10.0


def _missing_thumbnail_svg(name: str, message: str) -> bytes:
    safe_name = escape(str(name))
    safe_message = escape(str(message))
    return f"""<svg xmlns='http://www.w3.org/2000/svg' width='640' height='360' viewBox='0 0 640 360'>
      <rect width='640' height='360' fill='#1f2937'/>
      <rect x='24' y='24' width='592' height='312' rx='18' fill='#111827' stroke='#374151'/>
      <text x='50%' y='46%' dominant-baseline='middle' text-anchor='middle' fill='#f9fafb' font-family='system-ui, sans-serif' font-size='26' font-weight='700'>{safe_name}</text>
      <text x='50%' y='58%' dominant-baseline='middle' text-anchor='middle' fill='#d1d5db' font-family='system-ui, sans-serif' font-size='18'>{safe_message}</text>
    </svg>""".encode("utf-8")


def _parse_range(header: str | None, size: int) -> tuple[int, int, bool]:
    if not header or not header.startswith("bytes="):
        return 0, max(0, size - 1), False
    spec = header.removeprefix("bytes=").split(",", 1)[0].strip()
    start_text, _, end_text = spec.partition("-")
    try:
        if start_text:
            start = int(start_text)
            end = int(end_text) if end_text else size - 1
        else:
            suffix = int(end_text)
            start = max(0, size - suffix)
            end = size - 1
    except ValueError:
        return 0, max(0, size - 1), False
    start = max(0, min(start, max(0, size - 1)))
    end = max(start, min(end, max(0, size - 1)))
    return start, end, True


def _write_media_range(path: Path, writer, start: int, length: int) -> bool:
    try:
        with path.open("rb") as handle:
            handle.seek(start)
            remaining = length
            while remaining > 0:
                chunk = handle.read(min(1024 * 1024, remaining))
                if not chunk:
                    break
                writer.write(chunk)
                remaining -= len(chunk)
    except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
        return False
    return True


def handle_job_action(db: Database, config: AppConfig, job_id: int, action: str, form: dict[str, str]) -> str:
    if action == "rescan-job":
        job = db.get_job(job_id)
        if job is None:
            raise ValueError(f"Unknown job: {job_id}")
        disc_folder = Path(_job_source_path(job))
        if not disc_folder.exists():
            raise ValueError(f"Job folder not found: {disc_folder}")
        scan_disc_folder(db, config, disc_folder)
        db.audit("job_rescanned", "Rescanned job folder from web UI", job_id, {"disc_folder": str(disc_folder)})
        return f"rescan-job:{job_id}"
    if action == "generate-previews":
        queued = queue_previews_for_job(db, config, job_id)
        db.audit("preview_batch_requested", f"Requested preview generation for {queued} file(s)", job_id, {"queued": queued})
        return f"preview-queued:{queued}"
    if action.startswith("generate-preview-"):
        source_file_id = int(action.removeprefix("generate-preview-"))
        row = db.source_file_payload(source_file_id)
        if row is None:
            raise ValueError(f"Unknown source file: {source_file_id}")
        if int(row["job_id"]) != job_id:
            raise ValueError("Source file does not belong to this job")
        if not queue_preview_for_source_row(db, config, job_id, row, force_reprocess=True):
            return f"preview-already-current:{source_file_id}"
        return f"preview-queued:{source_file_id}"
    if action == "open-job-folder":
        job = db.get_job(job_id)
        if job is None:
            raise ValueError(f"Unknown job: {job_id}")
        folder = Path(_job_source_path(job))
        _open_path_with_system_handler(folder)
        db.audit("job_folder_opened", "Opened job folder from web UI", job_id, {"disc_folder": str(folder)})
        return f"open-job-folder:{job_id}"
    if action == "delete-job":
        job = db.get_job(job_id)
        if job is None:
            raise ValueError(f"Unknown job: {job_id}")
        ignore_path = job.source_disc_path or job.disc_path
        db.ignore_disc_path(
            ignore_path,
            reason=f"deleted job {job_id}",
            job_id=job.id,
            disc_title=job.disc_title,
            status="deleted",
        )
        db.audit("job_deleted", "Deleted job from the queue", job_id, {"disc_title": job.disc_title, "status": job.status, "ignored_path": str(ignore_path)})
        if not db.delete_job(job_id):
            raise ValueError(f"Unknown job: {job_id}")
        return "redirect:/"
    if action.startswith("open-source-file-folder-"):
        source_file_id = int(action.removeprefix("open-source-file-folder-"))
        row = db.source_file_payload(source_file_id)
        if row is None:
            raise ValueError(f"Unknown source file: {source_file_id}")
        _open_path_with_system_handler(_media_path_for(config, row).parent)
        db.audit("source_file_folder_opened", "Opened source file folder from web UI", job_id, {"source_file_id": source_file_id, "path": row["path"]})
        return f"open-source-file-folder:{source_file_id}"
    if action.startswith("open-source-file-"):
        source_file_id = int(action.removeprefix("open-source-file-"))
        row = db.source_file_payload(source_file_id)
        if row is None:
            raise ValueError(f"Unknown source file: {source_file_id}")
        _open_path_with_system_handler(_media_path_for(config, row))
        db.audit("source_file_opened", "Opened source file from web UI", job_id, {"source_file_id": source_file_id, "path": row["path"]})
        return f"open-source-file:{source_file_id}"
    if action == "split-source-file":
        raw_source_file_id = form.get("source_file_id", "").strip()
        if not raw_source_file_id:
            raise ValueError("Split job requires a source file selection")
        source_file_id = int(raw_source_file_id)
        job_review, decisions = parse_review_form(db, config, job_id, form, "review_in_progress")
        db.save_job_review(job_review)
        for decision in decisions:
            db.save_file_review(decision)
        new_job_id = db.create_split_job(job_id, source_file_id)
        return f"redirect:/jobs/{new_job_id}"
    if action.startswith("split-source-file-"):
        source_file_id = int(action.removeprefix("split-source-file-"))
        job_review, decisions = parse_review_form(db, config, job_id, form, "review_in_progress")
        db.save_job_review(job_review)
        for decision in decisions:
            db.save_file_review(decision)
        new_job_id = db.create_split_job(job_id, source_file_id)
        return f"redirect:/jobs/{new_job_id}"
    if action.startswith("lookup-file-metadata-"):
        source_file_id = int(action.removeprefix("lookup-file-metadata-"))
        job_review, decisions = parse_review_form(db, config, job_id, form, "review_in_progress")
        db.save_job_review(job_review)
        for decision in decisions:
            db.save_file_review(decision)
        result = lookup_file_metadata(db, config, job_id, source_file_id)
        return f"metadata-file-lookup:{source_file_id}:{len(result.candidates)}"
    if action.startswith("apply-metadata-candidate-"):
        candidate_id = int(action.removeprefix("apply-metadata-candidate-"))
        job_review, decisions = parse_review_form(db, config, job_id, form, "reviewed")
        db.save_job_review(job_review)
        for decision in decisions:
            db.save_file_review(decision)
        applied = apply_stored_metadata_candidate(db, config, job_id, candidate_id)
        db.audit("metadata_candidate_applied", f"Applied metadata candidate {candidate_id}", job_id, {"candidate_id": candidate_id, "applied_fields": applied})
        return _queue_automated_pipeline(db, config, job_id, force_reprocess=True)
    if action == "lookup-metadata":
        job_review, decisions = parse_review_form(db, config, job_id, form, "review_in_progress")
        db.save_job_review(job_review)
        for decision in decisions:
            db.save_file_review(decision)
        result = lookup_job_metadata(db, config, job_id)
        return f"metadata-lookup:{len(result.candidates)}"
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
        if action == "mark-reviewed":
            return _queue_automated_pipeline(db, config, job_id, force_reprocess=True)
        return "saved"
    if action == "resume-flow":
        return _queue_automated_pipeline(db, config, job_id)
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


def handle_ignored_action(db: Database, config: AppConfig, action: str, form: dict[str, str]) -> str:
    if action == "unignore":
        raw_disc_path = form.get("disc_path", "").strip()
        if not raw_disc_path:
            raise ValueError("Ignored job requires a disc path")
        if not db.unignore_disc_path(raw_disc_path):
            raise ValueError(f"Unknown ignored job path: {raw_disc_path}")
        return "redirect:/"
    if action == "open-folder":
        raw_disc_path = form.get("disc_path", "").strip()
        if not raw_disc_path:
            raise ValueError("Ignored job requires a disc path")
        _open_path_with_system_handler(Path(raw_disc_path))
        return "ignored-open-folder"
    raise ValueError(f"Unknown ignored action: {action}")


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

    existing_review = db.get_job_review(job_id)
    confidence_text = text("confidence")
    job_review = JobReviewMetadata(
        job_id=job_id,
        title=text("title") or "",
        original_title=text("original_title"),
        romanized_title=text("romanized_title"),
        translated_title=text("translated_title"),
        language_script_hints=text("language_script_hints"),
        anime_flag=form.get("anime_flag") == "on",
        japanese_media_flag=form.get("japanese_media_flag") == "on",
        confidence=float(confidence_text) if confidence_text is not None else existing_review.confidence,
        title_discovery_json=existing_review.title_discovery_json,
        manual_review_notes=existing_review.manual_review_notes,
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
                role=("ignore_candidate" if form.get(prefix + "include") != "on" else (text(prefix + "role") or "")),
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


def _prefill_review_from_title_discovery(job, review: JobReviewMetadata) -> JobReviewMetadata:
    discovery = review.title_discovery_json
    if not isinstance(discovery, dict) or not discovery:
        return review

    def pick_text(current: str | None, key: str) -> str | None:
        candidate = discovery.get(key)
        if isinstance(candidate, str) and candidate.strip():
            return current if current not in {None, ""} else candidate.strip()
        return current

    def pick_int(current: int | None, key: str) -> int | None:
        if current is not None:
            return current
        candidate = discovery.get(key)
        if isinstance(candidate, int):
            return candidate
        if isinstance(candidate, str):
            try:
                return int(candidate)
            except ValueError:
                return None
        return None

    def pick_float(current: float | None, key: str) -> float | None:
        if current is not None:
            return current
        candidate = discovery.get(key)
        if isinstance(candidate, (int, float)):
            return float(candidate)
        if isinstance(candidate, str):
            try:
                return float(candidate)
            except ValueError:
                return None
        return None

    title = review.title
    discovered_title = discovery.get("title")
    if isinstance(discovered_title, str) and discovered_title.strip() and (not title or (job is not None and title == job.disc_title)):
        title = discovered_title.strip()
    content_type = review.content_type
    discovered_content_type = discovery.get("content_type")
    if content_type in {"", "unknown"} and isinstance(discovered_content_type, str) and discovered_content_type.strip():
        content_type = discovered_content_type.strip()
    library_root = review.library_root
    discovered_library_root = discovery.get("library_root")
    if library_root in {"", "Movies"} and isinstance(discovered_library_root, str) and discovered_library_root.strip():
        library_root = discovered_library_root.strip()
    return replace(
        review,
        title=title,
        original_title=pick_text(review.original_title, "original_title"),
        romanized_title=pick_text(review.romanized_title, "romanized_title"),
        translated_title=pick_text(review.translated_title, "translated_title"),
        language_script_hints=pick_text(review.language_script_hints, "language_script_hints"),
        confidence=pick_float(review.confidence, "confidence"),
        year=pick_int(review.year, "year"),
        content_type=content_type,
        library_root=library_root,
    )


def _render_title_discovery_panel(review: JobReviewMetadata) -> str:
    discovery = review.title_discovery_json
    if not isinstance(discovery, dict) or not discovery:
        return '<p class="muted">No title-discovery evidence saved yet.</p>'
    signals = discovery.get("signals") if isinstance(discovery.get("signals"), list) else []
    signal_bits = []
    for signal in signals:
        if not isinstance(signal, dict):
            continue
        source = escape(str(signal.get("source") or "signal"))
        value = escape(str(signal.get("value") or ""))
        confidence = signal.get("confidence")
        confidence_text = f" ({float(confidence):.2f})" if isinstance(confidence, (int, float)) else ""
        signal_bits.append(f"<li><strong>{source}</strong>: {value}{confidence_text}</li>")
    warning_bits = []
    for warning in discovery.get("warnings") if isinstance(discovery.get("warnings"), list) else []:
        if isinstance(warning, str) and warning:
            warning_bits.append(f"<li>{escape(warning)}</li>")
    confidence = discovery.get("confidence")
    confidence_text = f"{float(confidence):.2f}" if isinstance(confidence, (int, float)) else "n/a"
    title = escape(str(discovery.get("title") or review.title or ""))
    return f"""
    <section class="lookup-strip">
      <div>
        <strong>Title discovery</strong>
        <span class="muted">Suggested title: {title} · confidence {confidence_text}</span>
      </div>
      <p class="wide muted">Review the prefilled metadata below, correct anything that looks off, then use <strong>Confirm and queue ffmpeg</strong> to continue.</p>
      <details class="ds-motion-disclosure">
        <summary>Evidence</summary>
        <ul>{''.join(signal_bits) or '<li>No structured evidence stored.</li>'}</ul>
        {f"<p class='errors'>Warnings: {'; '.join(warning_bits)}</p>" if warning_bits else ''}
      </details>
    </section>
    """


def render_job_list(db: Database, config: AppConfig) -> str:
    rows = db.list_job_summaries()
    lanes = {"review": [], "queue": [], "processing": [], "done": [], "other": []}
    for row in rows:
        lanes[_dashboard_lane_for_job(row)].append(row)

    lane_sections = [
        render_dashboard_lane(label, lane_rows)
        for label, lane_rows in [
            ("Review queue", lanes["review"]),
            ("Ready / queued", lanes["queue"]),
            ("Processing / validation", lanes["processing"]),
            ("Other", lanes["other"]),
        ]
        if lane_rows
    ]
    if lanes["done"]:
        lane_sections.append(render_dashboard_lane("Imported to Jellyfin", lanes["done"], collapsed=True))
    ignored_rows = db.list_ignored_jobs()
    if ignored_rows:
        lane_sections.append(render_ignored_jobs_lane(ignored_rows))

    return page(
        "Disc Steward Review",
        f"""
        <h1>Disc Steward Review</h1>
        {render_dashboard(db, config)}
        {render_preview_queue_panel(db, config)}
        <section class="dashboard-queue">
          {''.join(lane_sections)}
        </section>
        """,
    )


def _render_review_lane(
    key: str,
    label: str,
    items: list[tuple[dict, FileReviewDecision]],
    *,
    job_id: int,
    config: AppConfig,
    paths: dict[int, GeneratedPath],
    saved_source_ids: set[int],
) -> str:
    total = len(items)
    reviewed = sum(1 for row, _decision in items if row["id"] in saved_source_ids)
    unresolved = total - reviewed
    skipped = sum(1 for _row, decision in items if not decision.include_in_work_order)
    included = total - skipped
    cards_html = "".join(
        render_file_card(config, job_id, row, decision, paths.get(decision.source_file_id), is_saved=row["id"] in saved_source_ids)
        for row, decision in items
    )
    if not cards_html:
        cards_html = '<p class="muted">No files in this group.</p>'
    open_attr = "open" if key == "main" or unresolved > 0 else ""
    return f"""
    <details class="dashboard-lane dashboard-lane-collapsed ds-motion-disclosure" {open_attr}>
      <summary>
        <span>{escape(label)}</span>
        <span class="lane-badges">
          <span>{total} files</span>
          <span>{reviewed} reviewed</span>
          <span>{unresolved} unresolved</span>
          <span>{included} included</span>
          <span>{skipped} skipped</span>
        </span>
      </summary>
      <div class="dashboard-lane-body">
        {cards_html}
      </div>
    </details>
    """



def _pipeline_progress_state(job_status: str, validation: dict | None, transfer: dict | None) -> tuple[list[tuple[str, str]], str]:
    stages = [
        ("Start", "done"),
        ("Review", "done" if job_status not in {"review_needed"} else "current"),
        ("Processing", "done" if job_status in {"transfer_ready", "transferring_to_eddy", "imported_to_jellyfin"} or (validation and validation.get("passed")) else "pending"),
        ("Validation", "done" if validation and validation.get("passed") else "blocked" if validation and not validation.get("passed") else "pending"),
        ("Transfer", "done" if transfer and transfer.get("status") == "imported_to_jellyfin" else "blocked" if transfer and transfer.get("status") in {"failed", "transfer_conflict"} else "pending"),
        ("Finish", "done" if transfer and transfer.get("status") == "imported_to_jellyfin" else "pending"),
    ]
    if transfer and transfer.get("status") == "imported_to_jellyfin":
        reason = "Imported to Jellyfin."
    elif transfer and transfer.get("status") in {"failed", "transfer_conflict"}:
        reason = f"Transfer blocked: {transfer.get('status')}"
    elif validation and not validation.get("passed"):
        failed = sum(1 for item in validation.get("items", []) if item.get("status") != "passed")
        reason = f"Validation blocked: {failed} item(s) need attention."
    elif validation and validation.get("passed"):
        reason = "Validation passed; ready for transfer."
    elif job_status == "review_needed":
        reason = "Waiting on file review before processing can start."
    elif job_status == "transfer_ready":
        reason = "Ready for processing."
    elif job_status == "transferring_to_eddy":
        reason = "Transfer in progress."
    else:
        reason = f"Current status: {job_status.replace('_', ' ')}"
    return stages, reason


def _render_job_review_summary_strip(
    db: Database,
    job,
    job_review: JobReviewMetadata,
    rows: list[dict],
    decisions: list[FileReviewDecision],
    saved_decisions: dict[int, FileReviewDecision],
    grouped: dict[str, list[tuple[dict, FileReviewDecision]]],
    paths: dict[int, GeneratedPath],
    validation: dict | None = None,
    transfer: dict | None = None,
) -> str:
    total_files = len(rows)
    reviewed_files = len(saved_decisions)
    unresolved_files = total_files - reviewed_files
    skipped_files = sum(1 for decision in decisions if not decision.include_in_work_order)
    included_files = total_files - skipped_files
    main_feature_selected = sum(1 for decision in decisions if decision.include_in_work_order and decision.role == "main_feature")
    main_feature_candidates = len(grouped["main"])
    if main_feature_selected:
        main_feature_text = f"Main feature selected: {main_feature_selected}"
    elif main_feature_candidates:
        main_feature_text = f"Main feature candidates: {main_feature_candidates}"
    else:
        main_feature_text = "Main feature: missing"
    warnings = len(job_review.warnings) + sum(len(decision.warnings) for decision in decisions)
    conflicts = len(job_review.conflicts) + sum(len(decision.conflicts) for decision in decisions)
    conflicts += sum(len(path.conflicts) for path in paths.values())
    stages, _reason = _pipeline_progress_state(job.status, validation, transfer)
    automation_queue_html = _render_automation_queue_table(db, job.id)
    split_options_html = ""
    create_job_html = ""
    if len(rows) > 1:
        split_options_html = "".join(
            '            <option value="{id}">{label}</option>'.format(
                id=row["id"],
                label=escape(row["filename"]),
            )
            for row in rows
        )
        create_job_html = f"""
        <div class="job-summary-create-box">
          <label class="split-picker">Split into new job
            <select name="source_file_id" form="job-review-form">
{split_options_html}
            </select>
          </label>
          <button form="job-review-form" class="ds-button" formaction="/jobs/{job.id}/split-source-file" formmethod="post">Create split job</button>
        </div>
        """
    summary_actions_html = f"""
      <div class="inline-form job-summary-actions">
        <button form="job-review-form" class="ds-button" formaction="/jobs/{job.id}/open-job-folder" formmethod="post">Open job folder</button>
        <button form="job-review-form" class="ds-button" formaction="/jobs/{job.id}/rescan-job" formmethod="post">Rescan job folder</button>
        <button form="job-review-form" class="ds-button" formaction="/jobs/{job.id}/generate-previews" formmethod="post">Queue previews</button>
        <button form="job-review-form" class="ds-button" formaction="/jobs/{job.id}/resume-flow" formmethod="post">Resume automated flow</button>
        <button form="job-review-form" class="ds-button" formaction="/jobs/{job.id}/save" formmethod="post">Save draft review</button>
        <button form="job-review-form" class="ds-button ds-button--primary primary-action" formaction="/jobs/{job.id}/mark-reviewed" formmethod="post">Save and run pipeline</button>
        <button form="job-review-form" class="ds-button" formaction="/jobs/{job.id}/manual-review" formmethod="post">Send job to manual review</button>
        <button form="job-review-form" class="ds-button" formaction="/jobs/{job.id}/reopen" formmethod="post">Reopen review</button>
        <button
          form="job-review-form"
          class="ds-button ds-button--danger danger-action"
          formaction="/jobs/{job.id}/delete-job"
          formmethod="post"
          onclick="return confirm('Delete this job from the queue? This removes all saved review data for the job.')"
        >Delete job</button>
        {create_job_html}
      </div>
    """

    progress_bits = []
    for index, (label, state) in enumerate(stages):
        progress_bits.append(f"<span class='pipeline-step pipeline-step-{state}'>{escape(label)}</span>")
        if index < len(stages) - 1:
            progress_bits.append("<span class='pipeline-arrow'>→</span>")
    progress_html = "".join(progress_bits)
    return f"""
    <section class="dashboard-summary job-review-summary review-header ds-panel">
      <div class="dashboard-summary-header">
        <div>
          <div class="job-summary-topline">
            <p class="eyebrow">Job summary</p>
            <span class="job-summary-number">Job {job.id}</span>
          </div>
          <h2>{escape(job.disc_title)}</h2>
          <p class="muted">Controller path: <code>{escape(job.source_disc_path or job.disc_path)}</code></p>
          {f'<p class="muted">Split from job {job.split_from_job_id}</p>' if job.split_from_job_id is not None else ''}
        </div>
        <div class="dashboard-flags muted">
          <span>{escape(main_feature_text)}</span>
          <span>{reviewed_files} reviewed</span>
          <span>{unresolved_files} unresolved</span>
        </div>
      </div>
      {summary_actions_html}
      {automation_queue_html}
      <div class="pipeline-progress" aria-label="Pipeline progress">
        {progress_html}
      </div>
      <div class="dashboard-metrics">
        <div class="dashboard-metric"><span>Total files</span><strong>{total_files}</strong></div>
        <div class="dashboard-metric"><span>Reviewed</span><strong>{reviewed_files}</strong></div>
        <div class="dashboard-metric"><span>Unresolved</span><strong>{unresolved_files}</strong></div>
        <div class="dashboard-metric"><span>Included</span><strong>{included_files}</strong></div>
        <div class="dashboard-metric"><span>Skipped</span><strong>{skipped_files}</strong></div>
        <div class="dashboard-metric"><span>Warnings</span><strong>{warnings}</strong></div>
        <div class="dashboard-metric"><span>Conflicts</span><strong>{conflicts}</strong></div>
      </div>
    </section>
    """


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
    display_review = _prefill_review_from_title_discovery(job, job_review)
    saved_decisions = {decision.source_file_id: decision for decision in db.list_file_reviews(job_id)}
    saved_source_ids = db.list_saved_file_review_ids(job_id)
    rows = db.source_file_payloads(job_id)
    decisions = [_decision_for_row(config, display_review, row, saved_decisions.get(row["id"])) for row in rows]
    paths = generate_final_paths(config, display_review, decisions)
    grouped = {key: [] for key, _ in GROUPS}
    for row, decision in zip(rows, decisions, strict=False):
        grouped[_group_for(classification_from_json(row.get("classification_json")))].append((row, decision))
    error_html = ""
    if errors:
        error_html = "<div class='errors job-errors'>" + "".join(f"<p>{escape(error)}</p>" for error in errors) + "</div>"
    group_sections = []
    for key, label in GROUPS:
        group_sections.append(
            _render_review_lane(
                key,
                label,
                grouped[key],
                job_id=job_id,
                config=config,
                paths=paths,
                saved_source_ids=saved_source_ids,
            )
        )
    groups_html = "\n".join(group_sections)
    validation = db.latest_validation_summary(job_id)
    transfer = db.latest_transfer_summary(job_id)
    summary_html = _render_job_review_summary_strip(db, job, display_review, rows, decisions, saved_decisions, grouped, paths, validation, transfer)
    return page(
        f"Review Job {job_id}",
        f"""
        <a class="floating-back-link" href="/">Back to jobs</a>
        {summary_html}
        {error_html}
        <form id="job-review-form" method="post" action="/jobs/{job_id}/save" oninput="window.updateDestinationPreviews && window.updateDestinationPreviews()" onchange="window.updateDestinationPreviews && window.updateDestinationPreviews()">
          {render_job_fields(config, display_review)}
          {render_metadata_lookup_strip(db, config, job_id, display_review.title)}
          {groups_html}
        </form>
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
    confidence_html = f'<input type="hidden" name="confidence" value="{escape(str(review.confidence))}">' if review.confidence is not None else ''
    return f"""
    <section class="review-stack">
      <fieldset class="primary-metadata ds-panel">
        <legend>Primary metadata</legend>
        <p class="wide muted">Set the title, year, content type, and library root first. Everything else lives in the advanced section below.</p>
        <label class="ds-field">Title <input class="ds-control" name="title" value="{escape(review.title)}"></label>
        <label class="ds-field">Year <input class="ds-control" name="year" value="{escape(str(review.year or ''))}" inputmode="numeric"></label>
        <label class="ds-field">Content type {select("content_type", CONTENT_TYPES, review.content_type)}</label>
        <label class="ds-field">Library root {select("library_root", list(config.eddy_library_roots.keys()) or LIBRARY_ROOTS, review.library_root)}</label>
      </fieldset>
      <details class="advanced-panel ds-motion-disclosure">
        <summary>Advanced metadata</summary>
        <div class="advanced-grid">
          <label>Original title <input name="original_title" value="{escape(review.original_title or '')}"></label>
          <label>Romanized title <input name="romanized_title" value="{escape(review.romanized_title or '')}"></label>
          <label>Translated title <input name="translated_title" value="{escape(review.translated_title or '')}"></label>
          <label>Language/script hints <input name="language_script_hints" value="{escape(review.language_script_hints or '')}"></label>
          <label><input type="checkbox" name="anime_flag" {"checked" if review.anime_flag else ""}> Anime</label>
          <label><input type="checkbox" name="japanese_media_flag" {"checked" if review.japanese_media_flag else ""}> Japanese media</label>
          <label>IMDb ID <input name="imdb_id" value="{escape(review.imdb_id or '')}" placeholder="tt0245429"></label>
          <label>TMDb ID <input name="tmdb_id" value="{escape(review.tmdb_id or '')}" placeholder="268 or TMDb movie URL"></label>
          <label>TVDb ID <input name="tvdb_id" value="{escape(review.tvdb_id or '')}"></label>
          <label>AniDB ID <input name="anidb_id" value="{escape(review.anidb_id or '')}"></label>
          <label>AniList ID <input name="anilist_id" value="{escape(review.anilist_id or '')}" placeholder="20954 or AniList URL"></label>
          <label>MAL ID <input name="mal_id" value="{escape(review.mal_id or '')}" placeholder="199"></label>
          <label class="wide">Notes <textarea name="notes">{escape(review.notes or '')}</textarea></label>
          <div class="wide advanced-note muted">Metadata lookup is available below. LLM/Hermes is {'enabled' if config.llm.enabled else 'disabled'}.</div>
          {confidence_html}
          <div class="wide">{_render_title_discovery_panel(review)}</div>
        </div>
      </details>
    </section>
    """


def render_metadata_lookup_strip(db: Database, config: AppConfig, job_id: int, title: str | None = None) -> str:
    metadata_status = metadata_provider_status(config.metadata)
    candidates = db.list_metadata_candidates(job_id)
    last_lookup = _last_metadata_lookup_event(db, job_id)
    file_names = {
        row["id"]: row["filename"]
        for row in db.source_file_payloads(job_id)
    }
    provider_bits = [
        f"{escape(name)}:{'ready' if details['configured'] else 'off'}"
        for name, details in metadata_status["providers"].items()
    ]
    candidate_cards = []
    for candidate in candidates:
        candidate_cards.append(_render_metadata_candidate_card(job_id, candidate, file_names))
    candidates_html = "".join(candidate_cards) if candidate_cards else "<p class='muted wide'>No stored metadata candidates yet. Run a lookup to collect matches, then choose one.</p>"
    last_lookup_html = ""
    if last_lookup:
        payload = last_lookup.get("payload", {})
        provider_results = payload.get("provider_results") or []
        provider_html = " · ".join(_metadata_provider_result_text(result) for result in provider_results)
        provider_html = f"<p class='wide muted'>{escape(provider_html)}</p>" if provider_html else ""
        warnings = payload.get("warnings") or []
        warnings_html = "".join(f"<p class='wide errors'>{escape(_metadata_warning_text(warning))}</p>" for warning in warnings)
        last_lookup_html = f"<p class='wide muted'><strong>Last lookup:</strong> {escape(last_lookup.get('message') or '')}</p>{provider_html}{warnings_html}"
    disabled = "disabled" if not metadata_status["enabled"] else ""
    expanded = "open" if not (title or "").strip() else ""
    return f"""
    <details class="lookup-strip advanced-card ds-motion-disclosure" {expanded}>
      <summary>
        <strong>Metadata lookup</strong>
        <span class="muted">{'enabled' if metadata_status['enabled'] else 'disabled'} · {' · '.join(provider_bits)}</span>
      </summary>
      <button class="ds-button" formaction="/jobs/{job_id}/lookup-metadata" {disabled}>Lookup All</button>
      <p class="wide muted">Choose a match below to apply it to this review. Provider links open the source page so you can verify ambiguous titles before applying.</p>
      {last_lookup_html}
      <div class="wide candidate-grid">{candidates_html}</div>
    </details>
    """


def _render_metadata_candidate_card(job_id: int, candidate: dict, file_names: dict[int, str]) -> str:
    candidate_id = int(candidate.get("id") or 0)
    provider = str(candidate.get("provider") or "provider")
    title = escape(str(candidate.get("title") or "Untitled match"))
    year = candidate.get("year")
    year_text = f" ({int(year)})" if isinstance(year, int) else ""
    confidence = candidate.get("confidence")
    confidence_text = f" · confidence {float(confidence):.2f}" if isinstance(confidence, (int, float)) else ""
    provider_id = candidate.get("provider_id")
    provider_url = candidate.get("provider_url")
    artwork_url = candidate.get("image_url") or candidate.get("cover_url") or candidate.get("poster_url")
    target = candidate.get("source_file_id")
    target_text = "job review"
    if target is not None:
        target_text = f"file {target}"
        filename = file_names.get(int(target))
        if filename:
            target_text = f"file {escape(filename)}"
    field_bits = []
    for label, key in [
        ("content", "content_type"),
        ("root", "library_root"),
        ("IMDb", "imdb_id"),
        ("TMDb", "tmdb_id"),
        ("TVDb", "tvdb_id"),
        ("AniDB", "anidb_id"),
        ("AniList", "anilist_id"),
        ("MAL", "mal_id"),
    ]:
        value = candidate.get(key)
        if value:
            field_bits.append(f"<span>{escape(label)}: {escape(str(value))}</span>")
    episode_titles = candidate.get("episode_titles") or []
    if episode_titles:
        field_bits.append(f"<span>{len(episode_titles)} episode title(s)</span>")
    extras = candidate.get("extras") or []
    if extras:
        field_bits.append(f"<span>{len(extras)} extra hint(s)</span>")
    fields_html = "".join(field_bits)
    provider_link = f'<a href="{escape(str(provider_url))}" target="_blank" rel="noopener">Open {escape(provider)} page</a>' if provider_url else f"<span class='muted'>No provider page available</span>"
    candidate_label = f"{provider} · {title}{year_text}{confidence_text}"
    if provider_id:
        candidate_label += f" · ID {escape(str(provider_id))}"
    artwork_html = f'<img src="{escape(str(artwork_url))}" alt="{provider} cover art">' if artwork_url else ""
    return f"""
    <article class="candidate-card">
      <div class="file-card-header">
        <div>
          <h3>{candidate_label}</h3>
          <p class="muted">Applies to: {target_text}</p>
        </div>
        <button class="ds-button" formaction="/jobs/{job_id}/apply-metadata-candidate-{candidate_id}">Use this match</button>
      </div>
      {artwork_html}
      <p class="muted">{provider_link}</p>
      <div class="candidate-fields">{fields_html}</div>
    </article>
    """

def _last_metadata_lookup_event(db: Database, job_id: int) -> dict | None:
    events = [event for event in db.list_audit_events(job_id) if event.get("event_type") == "metadata_lookup"]
    return events[-1] if events else None


def _metadata_provider_result_text(result: object) -> str:
    if not isinstance(result, dict):
        return str(result)
    provider = result.get("provider") or "provider"
    count = result.get("candidate_count", 0)
    status = result.get("status") or "unknown"
    return f"{provider}: {count} candidate(s), {status}"


def _metadata_warning_text(warning: object) -> str:
    if not isinstance(warning, dict):
        return str(warning)
    provider = warning.get("provider") or "provider"
    message = warning.get("message") or ""
    return f"{provider}: {message}"


def render_file_card(config: AppConfig, job_id: int, row: dict, decision: FileReviewDecision, generated, *, is_saved: bool = False) -> str:
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
        controller_final_path = f"<p class='muted'><strong>Gospel final placement path:</strong> <code>{escape(str(generated.controller_path))}</code></p>"
    conflicts = generated.conflicts if generated else []
    attention_bits = [*conflicts, *issues, *subtitle_plan.warnings]
    if subtitle_plan.japanese_or_anime:
        attention_bits.append("Japanese/anime content detected; review title and subtitle handling.")
    if attention_bits:
        attention_html = f"<p class='file-card-badge file-card-badge-attention'>Attention: {escape(attention_bits[0])}</p>"
    elif not decision.include_in_work_order:
        attention_html = '<span class="file-card-badge file-card-badge-skip">Skipped</span>'
    elif not is_saved:
        attention_html = '<span class="file-card-badge file-card-badge-new">New</span>'
    else:
        attention_html = ""
    duration_text = format_duration(row["duration_seconds"])
    quick_summary = " · ".join(
        bit for bit in [
            f"Duration: {duration_text}" if duration_text else "",
            f"Include: {'yes' if decision.include_in_work_order else 'no'}",
            f"Role: {escape(decision.role or 'unspecified')}",
            f"Display: {escape(decision.final_display_name or row['filename'])}",
            f"Type: {escape(decision.content_type or '')}",
        ]
        if bit
    )
    destination_preview = final_path if decision.include_in_work_order else "Skipped / do not process."
    return f"""
    <article class="file-card ds-panel" data-source-file-id="{source_id}">
      <div class="file-card-header">
        <div>
          <h3>{escape(row['filename'])}</h3>
          <p class="muted">{quick_summary}</p>
        </div>
        <div class="file-card-header-actions">
          <button class="ds-button" formaction="/jobs/{job_id}/lookup-file-metadata-{source_id}">Lookup file metadata</button>
          {render_media_review_action_buttons(config, job_id, row)}
        </div>
      </div>
      <div class="file-task-columns">
        {render_media_review_controls(config, job_id, row)}
        <div class="file-fields file-fields-primary">
          <label><input type="checkbox" name="{prefix}include" {"checked" if decision.include_in_work_order else ""}> Include in processing</label>
          <label class="ds-field">Role {select(prefix + "role", ROLE_CHOICES, decision.role, blank=True)}</label>
          <label class="ds-field">Content type {select(prefix + "content_type", CONTENT_TYPES, decision.content_type)}</label>
          <label class="ds-field">Extra type <input class="ds-control" name="{prefix}extra_type" value="{escape(decision.extra_type or '')}"></label>
          <label class="ds-field">Display name <input class="ds-control" name="{prefix}final_display_name" value="{escape(decision.final_display_name or '')}"></label>
          <label class="ds-field">Final filename <input class="ds-control" name="{prefix}final_filename" value="{escape(decision.final_filename or '')}"></label>
          <label class="ds-field">Season <input class="ds-control" name="{prefix}season_number" value="{escape(str(decision.season_number if decision.season_number is not None else ''))}" inputmode="numeric"></label>
          <label class="ds-field">Episode <input class="ds-control" name="{prefix}episode_number" value="{escape(str(decision.episode_number if decision.episode_number is not None else ''))}" inputmode="numeric"></label>
          <label class="ds-field">Sort order <input class="ds-control" name="{prefix}sort_order" value="{escape(str(decision.sort_order if decision.sort_order is not None else ''))}" inputmode="numeric"></label>
        </div>
      </div>
      <section class="destination-preview">
        <p class="eyebrow">Destination preview</p>
        <p class="muted">This updates as you edit the file controls above.</p>
        <p><code data-preview-path="{source_id}" data-current-path="{final_path}">{escape(destination_preview)}</code></p>
        {controller_final_path}
      </section>
      {attention_html}
      <details class="advanced-panel file-advanced ds-motion-disclosure">
        <summary>Advanced file details</summary>
        <div class="advanced-grid">
          <p class="wide muted"><strong>Controller path:</strong> <code>{escape(row['path'])}</code></p>
          {_mapped_path_line('Barnabas path', config.to_barnabas_path(Path(row['path'])), Path(row['path']))}
          <div class="tech wide">
            <span>Duration: {duration_text}</span>
            <span>Resolution: {video.get('width') or '?'}x{video.get('height') or '?'}</span>
            <span>Video: {escape(' / '.join(str(video.get(key) or '') for key in ('codec', 'profile', 'pixel_format')).strip(' / '))}</span>
            <span>Audio: {escape(format_streams(audio))}</span>
            <span>Subtitles: {escape(format_streams(subtitles))}</span>
            <span>Chapters: {row['chapter_count']}</span>
            <span>Size: {format_size(row['size_bytes'])}</span>
            <span>Confidence: {classification.confidence:.2f}</span>
          </div>
          <p class="wide"><strong>Reasons:</strong> {escape('; '.join(classification.reasons) or 'None recorded')}</p>
          <p class="wide"><strong>Issues:</strong> {escape('; '.join(issues) or 'None detected')}</p>
          <p class="wide"><strong>Subtitle plan:</strong> {escape(', '.join(subtitle_plan.statuses))}</p>
          {"<p class='errors wide'>" + escape('; '.join(subtitle_plan.warnings)) + "</p>" if subtitle_plan.warnings else ""}
          <div class="file-fields wide file-fields-advanced">
            <label class="ds-field">Original title <input class="ds-control" name="{prefix}original_title" value="{escape(decision.original_title or '')}"></label>
            <label class="ds-field">Translated title <input class="ds-control" name="{prefix}translated_title" value="{escape(decision.translated_title or '')}"></label>
            <label class="ds-field">Romanized title <input class="ds-control" name="{prefix}romanized_title" value="{escape(decision.romanized_title or '')}"></label>
            <label class="ds-field">Encoding profile {select(prefix + "encoding_profile", config.encoding_profiles, decision.encoding_profile)}</label>
            <label class="ds-field">Subtitle policy {select(prefix + "subtitle_policy", config.subtitle_policies, decision.subtitle_policy)}</label>
            <label class="ds-field">IMDb ID <input class="ds-control" name="{prefix}imdb_id" value="{escape(decision.imdb_id or '')}"></label>
            <label class="ds-field">TMDb ID <input class="ds-control" name="{prefix}tmdb_id" value="{escape(decision.tmdb_id or '')}"></label>
            <label class="ds-field">TVDb ID <input class="ds-control" name="{prefix}tvdb_id" value="{escape(decision.tvdb_id or '')}"></label>
            <label class="ds-field">AniDB ID <input class="ds-control" name="{prefix}anidb_id" value="{escape(decision.anidb_id or '')}"></label>
            <label class="ds-field">AniList ID <input class="ds-control" name="{prefix}anilist_id" value="{escape(decision.anilist_id or '')}"></label>
            <label class="ds-field">MAL ID <input class="ds-control" name="{prefix}mal_id" value="{escape(decision.mal_id or '')}"></label>
            <label class="ds-field wide">Notes <textarea class="ds-control" name="{prefix}notes">{escape(decision.notes or '')}</textarea></label>
          </div>
        </div>
      </details>
    </article>
    """


def render_media_review_action_buttons(config: AppConfig, job_id: int, row: dict) -> str:
    source_id = int(row["id"])
    preview_button = f'<button class="ds-button" formaction="/jobs/{job_id}/generate-preview-{source_id}" formnovalidate>Regenerate preview</button>' if config.preview.enabled else ""
    return f"""
      <div class="media-actions">
        {preview_button}
        <button
          type="button"
          class="ds-button system-open-action"
          data-system-open-action="/jobs/{job_id}/open-source-file-{source_id}"
        >Open file in system handler</button>
        <button
          type="button"
          class="ds-button system-open-action"
          data-system-open-action="/jobs/{job_id}/open-source-file-folder-{source_id}"
        >Open containing folder</button>
        <button class="ds-button" formaction="/jobs/{job_id}/split-source-file-{source_id}" formnovalidate>Split into new movie job</button>
      </div>
    """


def render_media_review_controls(config: AppConfig, job_id: int, row: dict) -> str:
    source_id = int(row["id"])
    filename = escape(row["filename"])
    preview_status = row.get("preview_status") or "missing"
    preview_path = row.get("preview_path")
    preview_ready = preview_status == "ready" and preview_path and Path(preview_path).exists()
    preview_html = ""
    if preview_ready:
        preview_html = f"""
        <video class="media-preview" controls preload="metadata" playsinline poster="/media/{source_id}/thumbnail">
          <source src="/media/{source_id}/preview" type="video/mp4">
          Your browser does not support the video element.
        </video>
        <p class="muted">Browser-native preview generated on Barnabas.</p>
        """
    else:
        preview_html = f"""
        <a href="/media/{source_id}/thumbnail" target="_blank" rel="noopener">
          <img class="media-thumb" src="/media/{source_id}/thumbnail" alt="Representative thumbnail for {filename}">
        </a>
        <p class="muted">Thumbnail preview from the file itself.</p>
        """
    preview_label = escape(preview_status.replace("_", " "))
    preview_error_html = ""
    if preview_status == "failed" and row.get("preview_error"):
        preview_error_html = f"<p class='errors'>Preview error: {escape(str(row['preview_error']))}</p>"
    return f"""
      <div class="media-review">
        {preview_html}
        <p class="muted"><strong>Preview status:</strong> <span class="pill pill-{preview_status}">{preview_label}</span></p>
        {preview_error_html}
      </div>
    """


def _mapped_path_line(label: str, mapped: Path, original: Path) -> str:
    if mapped == original:
        return ""
    return f"<p class='muted'><strong>{escape(label)}:</strong> <code>{escape(str(mapped))}</code></p>"


def _open_path_with_system_handler(path: Path) -> None:
    if not path.exists():
        raise ValueError(f"Path does not exist: {path}")
    openers = (("xdg-open",), ("gio", "open"), ("open",))
    last_error: Exception | None = None
    for opener in openers:
        if shutil.which(opener[0]) is None:
            continue
        try:
            subprocess.Popen([*opener, str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
            return
        except OSError as error:
            last_error = error
    if last_error is not None:
        raise ValueError(f"Unable to open {path}: {last_error}")
    raise ValueError("No supported file opener found (xdg-open, gio open, open)")


def _pipeline_status_text(db: Database, job_id: int) -> str:
    automation = db.get_automation_job(job_id)
    if automation:
        if automation["state"] == "queued":
            return "queued for automation"
        if automation["state"] == "running":
            return "automation running"
        if automation["state"] == "failed":
            return f"automation failed ({automation.get('last_error') or 'unknown error'})"
    validation = db.latest_validation_summary(job_id)
    transfer = db.latest_transfer_summary(job_id)
    if transfer and transfer.get("status") == "imported_to_jellyfin":
        return "completed"
    if transfer and transfer.get("status") in {"failed", "transfer_conflict"}:
        return f"stalled on transfer ({transfer.get('status')})"
    if validation and not validation.get("passed"):
        return f"stalled on validation ({validation.get('status', 'unknown')})"
    if validation and validation.get("passed"):
        return "waiting on transfer"
    return "waiting on processing"


def _run_automated_pipeline(
    db: Database,
    config: AppConfig,
    job_id: int,
    *,
    force_reprocess: bool = False,
    ffmpeg_runner=None,
    ffprobe_runner=None,
) -> str:
    latest_transfer = db.latest_transfer_summary(job_id)
    if latest_transfer and latest_transfer.get("status") == "imported_to_jellyfin":
        return "automation:imported_to_jellyfin"

    latest_validation = db.latest_validation_summary(job_id)
    if force_reprocess or not (latest_validation and latest_validation.get("passed")):
        db.clear_processing_results(job_id)
        create_ffmpeg_processing_jobs(db, config, job_id, ffmpeg_runner=ffmpeg_runner)
        latest_validation = validate_job_outputs(db, config, job_id, ffprobe_runner=ffprobe_runner)
        if not latest_validation.passed:
            return f"automation:{latest_validation.status}"

    transfer_summary = transfer_job_to_eddy(db, config, job_id)
    return f"automation:{transfer_summary.status}"


def run_automation_worker(
    db: Database,
    config: AppConfig,
    *,
    poll_interval: float = 1.0,
    stop_event: threading.Event | None = None,
) -> None:
    db.reset_stuck_automation_jobs()
    while True:
        processed = _process_next_automation_job(db, config)
        if stop_event is not None and stop_event.is_set():
            return
        if processed:
            continue
        if stop_event is not None and stop_event.wait(timeout=poll_interval):
            return
        time.sleep(poll_interval)


def _process_next_automation_job(db: Database, config: AppConfig) -> bool:
    queued = db.claim_next_automation_job()
    if queued is None:
        return False
    job_id = int(queued["job_id"])
    force_reprocess = bool(queued["force_reprocess"])
    db.audit("automation_started", "Started automated pipeline run", job_id, {"force_reprocess": force_reprocess, "attempts": queued["attempts"] + 1})
    try:
        result = _run_automated_pipeline(db, config, job_id, force_reprocess=force_reprocess)
        db.finish_automation_job(job_id, state="succeeded", result=result)
        db.audit("automation_complete", f"Automated pipeline finished: {result}", job_id)
    except Exception as error:  # pragma: no cover - defensive logging for background work
        message = str(error)
        db.finish_automation_job(job_id, state="failed", error=message)
        db.audit("automation_failed", f"Automated pipeline failed: {message}", job_id, {"error": message})
    return True


def _queue_automated_pipeline(
    db: Database,
    config: AppConfig,
    job_id: int,
    *,
    force_reprocess: bool = False,
    ffmpeg_runner=None,
    ffprobe_runner=None,
) -> str:
    db.enqueue_automation_job(job_id, force_reprocess=force_reprocess)
    db.audit("automation_queued", "Queued automated pipeline run", job_id, {"force_reprocess": force_reprocess})
    return "automation:queued"






def _render_automation_queue_table(db: Database, job_id: int) -> str:
    automation_jobs = [
        row
        for row in db.list_automation_jobs()
        if row["state"] in {"queued", "running", "failed"} or row["job_id"] == job_id
    ]
    if not automation_jobs:
        return ""
    queued_jobs = sorted(
        (row for row in automation_jobs if row["state"] == "queued"),
        key=lambda row: (row.get("queued_at") or "", row["job_id"]),
    )
    queue_positions = {row["job_id"]: index + 1 for index, row in enumerate(queued_jobs)}
    rows_html = []
    for row in sorted(automation_jobs, key=lambda item: (item["state"] != "running", item.get("queued_at") or "", item["job_id"])):
        state = row["state"]
        current = row["job_id"] == job_id
        if state == "queued":
            queue_note = f"position {queue_positions.get(row['job_id'], 0)} in queue"
        elif state == "running":
            queue_note = "this job is running" if current else "running now"
        else:
            queue_note = row.get("last_error") or row.get("last_result") or "failed"
        labels = [f"Job {row['job_id']}"]
        if current:
            labels.append("current job")
        rows_html.append(
            "<tr>"
            f"<td>{escape(' · '.join(labels))}</td>"
            f"<td>{escape(state)}</td>"
            f"<td>{int(row.get('attempts') or 0)}</td>"
            f"<td>{escape(str(row.get('queued_at') or ''))}</td>"
            f"<td>{escape(str(row.get('started_at') or ''))}</td>"
            f"<td>{escape(str(row.get('finished_at') or ''))}</td>"
            f"<td>{escape(queue_note)}</td>"
            "</tr>"
        )
    return f"""
    <details class="automation-queue-panel ds-motion-disclosure">
      <summary>Automation queue <span class="muted">({len(automation_jobs)} active)</span></summary>
      <section class="ops ds-panel">
        <table class="ds-table" data-density="compact">
          <thead><tr><th>Job</th><th>State</th><th>Attempts</th><th>Queued</th><th>Started</th><th>Finished</th><th>Note</th></tr></thead>
          <tbody>{''.join(rows_html)}</tbody>
        </table>
      </section>
    </details>
    """


def render_phase3_sections(db: Database, config: AppConfig, job_id: int) -> str:
    validation = db.latest_validation_summary(job_id)
    transfer = db.latest_transfer_summary(job_id)
    validation_html = render_validation_section(job_id, validation)
    transfer_html = render_transfer_section(job_id, validation, transfer)
    pipeline_status = _pipeline_status_text(db, job_id)
    automation_queue_html = _render_automation_queue_table(db, job_id)
    return f"""
    <details class="advanced-panel ds-motion-disclosure">
      <summary>Processing and transfer</summary>
      <section class="ops ds-panel">
        <p>Automated flow status: <strong>{escape(pipeline_status)}</strong></p>
        <p class="muted">If something fails, fix the issue and press resume to continue from the last successful step.</p>
        <form method="post" action="/jobs/{job_id}/resume-flow" class="inline-form">
          <button class="ds-button">Resume automated flow</button>
        </form>
      </section>
      {automation_queue_html}
      {validation_html}
      {transfer_html}
    </details>
    """


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
    <details class="advanced-panel ds-motion-disclosure">
      <summary>Metadata automation and cleanup</summary>
      <section class="ops ds-panel">
        <p>Metadata providers: <strong>{'enabled' if config.metadata.enabled else 'disabled'}</strong> · LLM/Hermes: <strong>{'enabled' if config.llm.enabled else 'disabled'}</strong> · Cleanup: <strong>{'enabled' if config.cleanup.enabled else 'disabled'}</strong> ({'dry-run' if config.cleanup.dry_run else 'live'})</p>
        <form method="post" action="/jobs/{job_id}/llm-suggestions" class="inline-form">
          <button class="ds-button" {'disabled' if not config.llm.enabled else ''}>Request LLM suggestions</button>
        </form>
        <form method="post" action="/jobs/{job_id}/generate-subtitle-plans" class="inline-form"><button class="ds-button">Generate subtitle plan</button></form>
        <table class="ds-table" data-density="compact">
          <thead><tr><th>Suggestion</th><th>Status</th><th>Payload</th></tr></thead>
          <tbody>{suggestion_rows or '<tr><td colspan="3">No LLM suggestions stored.</td></tr>'}</tbody>
        </table>
        <h3>Cleanup</h3>
        <p>Cleanup hold: <strong>{'on' if cleanup_hold else 'off'}</strong></p>
        <form method="post" action="/jobs/{job_id}/cleanup-plan" class="inline-form"><button class="ds-button">Generate cleanup plan</button></form>
        <form method="post" action="/jobs/{job_id}/cleanup-hold" class="inline-form">
          <label>Hold reason <input name="cleanup_hold_reason" value="manual hold"></label>
          <button class="ds-button">Mark job as cleanup hold</button>
        </form>
        <form method="post" action="/jobs/{job_id}/remove-cleanup-hold" class="inline-form"><button class="ds-button">Remove cleanup hold</button></form>
        <table class="ds-table" data-density="compact">
          <thead><tr><th>Type</th><th>Eligible</th><th>Path</th><th>Reason</th></tr></thead>
          <tbody>{cleanup_rows or '<tr><td colspan="4">No cleanup plan recorded.</td></tr>'}</tbody>
        </table>
      </section>
    </details>
    """


def render_dashboard(db: Database, config: AppConfig) -> str:
    try:
        summary = build_status_summary(db, config)
    except Exception:
        return ""
    cards = [
        ("Needs review", "Needs review", summary["jobs_needing_review"]),
        ("Waiting for processing", "Waiting", summary["jobs_waiting_for_processing"]),
        ("Needs validation", "Validation", summary["jobs_needing_validation"]),
        ("Ready for transfer", "Transfer", summary["jobs_ready_for_transfer"]),
        ("Imported", "Imported", summary["jobs_imported"]),
        ("Validation failures", "Validation fails", summary["validation_failures"]),
        ("Transfer conflicts", "Conflicts", summary["transfer_conflicts"]),
        ("Subtitle issues", "Subtitles", summary["subtitle_issues_outstanding"]),
        ("Cleanup eligible", "Cleanup", summary["cleanup_eligible_items"]),
    ]
    metrics = "".join(
        f"<div class='dashboard-metric'><span title='{escape(full_label)}'>{escape(label)}</span><strong>{int(value)}</strong></div>"
        for full_label, label, value in cards
    )
    recent_errors = summary.get("recent_errors", [])
    error_html = ""
    if recent_errors:
        error_html = "".join(
            f"<li><strong>Job {escape(str(event.get('job_id', '?')))}</strong> · {escape(str(event.get('event_type', 'event')))} · {escape(str(event.get('message', '')))}</li>"
            for event in recent_errors[-5:]
        )
        error_html = f"<section class='dashboard-notes'><h2>Recent errors</h2><ul>{error_html}</ul></section>"
    return f"""
    <section class="dashboard-summary">
      <div class="dashboard-summary-header">
        <div>
          <p class="eyebrow">Operational overview</p>
          <h2>What needs attention</h2>
          <p class="muted">This is the queue view: scan the lanes, open the next job, and keep the backlog moving.</p>
        </div>
        <div class="dashboard-flags muted">
          <span>Metadata: {'on' if summary['metadata_enabled'] else 'off'}</span>
          <span>LLM: {'on' if summary['llm_enabled'] else 'off'}</span>
          <span>Cleanup: {'on' if summary['cleanup_enabled'] else 'off'} ({'dry-run' if summary['cleanup_dry_run'] else 'live'})</span>
        </div>
      </div>
      <div class="dashboard-metrics dashboard-status-grid">
        {metrics}
      </div>
      {error_html}
    </section>
    """


def render_preview_queue_panel(db: Database, config: AppConfig) -> str:
    if not config.preview.enabled:
        return ""
    rows = [row for row in db.list_preview_jobs() if row["state"] in {"queued", "running", "failed"}]
    if not rows:
        return ""
    active_total = len(rows)
    counts = {state: sum(1 for row in rows if row["state"] == state) for state in {"queued", "running", "failed"}}
    queued_rows = sorted((row for row in rows if row["state"] == "queued"), key=lambda row: (row.get("queued_at") or "", row["source_file_id"]))
    queue_positions = {row["source_file_id"]: index + 1 for index, row in enumerate(queued_rows)}
    display_rows = sorted(
        rows,
        key=lambda row: (
            row["state"] != "running",
            row["state"] != "queued",
            row["state"] != "failed",
            row.get("queued_at") or "",
            row["job_id"],
            row["source_file_id"],
        ),
    )
    row_html = []
    for row in display_rows:
        job = db.get_job(int(row["job_id"]))
        job_label = escape(job.disc_title if job else f"Job {row['job_id']}")
        source_row = db.source_file_payload(int(row["source_file_id"])) or {}
        source_label = escape(str(source_row.get("filename") or row["source_file_id"]))
        state = row["state"]
        note = ""
        if state == "queued":
            note = f"position {queue_positions.get(row['source_file_id'], 0)} in queue"
        elif state == "running":
            note = "encoding now"
        else:
            error = (row.get("last_error") or source_row.get("preview_error") or "").strip()
            note = error[:180] if error else "failed"
        row_html.append(
            f"<tr><td><a href='/jobs/{row['job_id']}'>{job_label}</a></td><td>{source_label}</td><td><span class='pill pill-{state}'>{escape(state)}</span></td><td>{int(row.get('attempts') or 0)}</td><td>{escape(str(row.get('queued_at') or ''))}</td><td>{escape(str(row.get('started_at') or ''))}</td><td>{escape(note)}</td></tr>"
        )
    return f"""
    <details class="dashboard-lane preview-queue-panel ds-motion-disclosure">
      <summary>Preview queue <span class="muted">({active_total} active · {counts['queued']} queued · {counts['running']} running · {counts['failed']} failed)</span></summary>
      <section class="ops ds-panel">
        <table class="ds-table" data-density="compact">
          <thead><tr><th>Job</th><th>File</th><th>State</th><th>Attempts</th><th>Queued</th><th>Started</th><th>Note</th></tr></thead>
          <tbody>{''.join(row_html)}</tbody>
        </table>
      </section>
    </details>
    """



def _dashboard_lane_for_job(row: dict) -> str:
    job_status = (row.get("status") or "").strip()
    review_status = (row.get("review_status") or "").strip()
    status = job_status or review_status
    if status in {"imported_to_jellyfin"}:
        return "done"
    if status in {"review_needed", "review_in_progress", "manual_review"}:
        return "review"
    if status in {"reviewed", "ready_for_fileflows", "fileflows_work_orders_created", "ready_for_processing"}:
        return "queue"
    if status in {"validation_needed", "validated", "transfer_ready", "validation_failed", "transfer_conflict"}:
        return "processing"
    return "other"


def render_dashboard_lane(title: str, rows: list[dict], *, collapsed: bool = False) -> str:
    cards = "".join(render_job_card(row) for row in rows)
    panel = f"""
      <header>
        <h2>{escape(title)}</h2>
        <p class="muted">{len(rows)} job{'s' if len(rows) != 1 else ''}</p>
      </header>
      <div class="dashboard-card-grid">{cards}</div>
    """
    if collapsed:
        return f"""
        <details class="dashboard-lane dashboard-lane-collapsed ds-motion-disclosure">
          <summary>{escape(title)} <span class="muted">({len(rows)} job{'s' if len(rows) != 1 else ''})</span></summary>
          {panel}
        </details>
        """
    return f"""
    <section class="dashboard-lane">
      {panel}
    </section>
    """


def render_job_card(row: dict) -> str:
    status = row.get("status") or row.get("review_status") or "unknown"
    review_state = row.get("review_status") or ""
    status_class = "status-badge"
    if status in {"review_needed", "review_in_progress", "manual_review"}:
        status_class += " status-warm"
    elif status in {"reviewed", "ready_for_fileflows", "fileflows_work_orders_created", "ready_for_processing"}:
        status_class += " status-cool"
    elif status in {"validation_failed", "transfer_conflict"}:
        status_class += " status-hot"
    elif status in {"imported_to_jellyfin"}:
        status_class += " status-done"
    main_feature = row.get("likely_main_feature") or "not identified yet"
    title = row['disc_title'] or f'Job {row["id"]}'
    chips = [
        f"{int(row.get('scanned_file_count') or 0)} files",
        f"{int(row.get('extra_count') or 0)} extras",
        f"{int(row.get('subtitle_issue_count') or 0)} subtitle issues",
        f"{int(row.get('transcode_risk_count') or 0)} transcode risks",
    ]
    if row.get("main_count"):
        chips.insert(1, f"{int(row.get('main_count') or 0)} main-feature candidates")
    chip_html = "".join(f"<span>{escape(chip)}</span>" for chip in chips)
    return f"""
    <a class="job-card-link" href="/jobs/{row['id']}" aria-label="Open job {escape(title)}">
      <article class="job-card">
        <div class="job-card-header">
          <div>
            <p class="job-title">{escape(title)}</p>
            {f'<p class="muted">Split from job {int(row["split_from_job_id"])}.</p>' if row.get('split_from_job_id') is not None else ''}
          </div>
          <span class="{status_class}">{escape(str(status))}</span>
        </div>
        <p><strong>Main:</strong> {escape(str(main_feature))}</p>
        <div class="job-chips">{chip_html}</div>
        <p class="muted">Review state: {escape(str(review_state))}</p>
      </article>
    </a>
    """


def render_ignored_jobs_lane(rows: list[dict]) -> str:
    cards = "".join(render_ignored_job_card(row) for row in rows)
    if not cards:
        cards = '<p class="muted">No deleted jobs have been ignored.</p>'
    return f"""
    <details class="dashboard-lane dashboard-lane-collapsed ds-motion-disclosure">
      <summary>
        <span>Deleted / ignored</span>
        <span class="lane-badges"><span>{len(rows)} job{'s' if len(rows) != 1 else ''}</span></span>
      </summary>
      <div class="dashboard-lane-body">
        <div class="dashboard-card-grid">{cards}</div>
      </div>
    </details>
    """


def render_ignored_job_card(row: dict) -> str:
    disc_path = row.get("disc_path") or ""
    title = row.get("disc_title") or Path(disc_path).name or f"Job {row.get('job_id') or 'unknown'}"
    reason = row.get("reason") or "deleted"
    status = row.get("status") or "deleted"
    created_at = row.get("created_at") or ""
    return f"""
    <article class="job-card ignored-job-card">
      <div class="job-card-header">
        <div>
          <p class="job-title">{escape(str(title))}</p>
          <p class="muted">{escape(str(disc_path))}</p>
        </div>
        <span class="status-badge status-done">{escape(str(status))}</span>
      </div>
      <p><strong>Reason:</strong> {escape(str(reason))}</p>
      {f'<p class="muted">Ignored at {escape(str(created_at))}</p>' if created_at else ''}
      <div class="inline-form job-summary-actions ignored-job-actions">
        <form method="post" action="/ignored/open-folder" class="inline-form">
          <input type="hidden" name="disc_path" value="{escape(str(disc_path))}">
          <button type="submit" class="ds-button">Open folder</button>
        </form>
        <form method="post" action="/ignored/unignore" class="inline-form">
          <input type="hidden" name="disc_path" value="{escape(str(disc_path))}">
          <button type="submit" class="ds-button ds-button--primary primary-action">Unignore job</button>
        </form>
      </div>
    </article>
    """


def render_validation_section(job_id: int, summary: dict | None) -> str:
    if summary is None:
        body = "<p class='muted'>No processing validation has been recorded.</p>"
    else:
        item_rows = "".join(render_validation_item(job_id, item) for item in summary.get("items", []))
        warnings = "".join(f"<p class='errors'>{escape(warning)}</p>" for warning in summary.get("warnings", []))
        body = f"""
        <p>Status: <strong>{escape(summary.get('status', 'unknown'))}</strong></p>
        {warnings}
        <table class="ds-table" data-density="compact">
          <thead><tr><th>Source</th><th>Status</th><th>Expected</th><th>Matched</th><th>Profile</th><th>Warnings / Errors</th></tr></thead>
          <tbody>{item_rows}</tbody>
        </table>
        """
    return f"""
    <section class="ops ds-panel">
      <h2>Processing Validation</h2>
      {body}
      <form method="post" action="/jobs/{job_id}/validate">
        <button class="ds-button">Run validation for this job</button>
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
          <button class="ds-button">Manually accept</button>
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
        <table class="ds-table" data-density="compact">
          <thead><tr><th>Source</th><th>Status</th><th>Eddy Incoming</th><th>Final Eddy Path</th><th>Conflict / Error</th></tr></thead>
          <tbody>{rows}</tbody>
        </table>
        """
    return f"""
    <section class="ops ds-panel">
      <h2>Eddy Transfer and Import</h2>
      <p>Readiness: <strong>{'ready' if ready else 'not ready'}</strong></p>
      {body}
      <form method="post" action="/jobs/{job_id}/transfer">
        <button class="ds-button" {'disabled' if not ready else ''}>Transfer validated outputs</button>
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
    html = [f'<select class="ds-control" name="{escape(name)}">']
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
      <link rel="icon" type="image/png" href="{_favicon_href()}">
      <link rel="stylesheet" href="{_design_system_stylesheet_href()}">
      <link rel="stylesheet" href="{_motion_stylesheet_href()}">
      <style>
        body[data-ds-theme="win31"] {{
          color-scheme: light;
          font-family: var(--ds-font-ui);
          --font-body: var(--ds-font-ui);
          --font-heading: var(--ds-font-ui);
          --bg: var(--ds-surface-canvas);
          --bg-shadow: var(--ds-surface-canvas-shadow);
          --surface: var(--ds-surface-window);
          --surface-soft: var(--ds-surface-control-hover);
          --surface-raised: var(--ds-surface-control);
          --title-start: var(--ds-accent-primary);
          --title-end: var(--ds-accent-primary-end);
          --text: var(--ds-text-primary);
          --text-muted: var(--ds-text-secondary);
          --border-dark: var(--ds-border-strong);
          --border-mid: var(--ds-border-raised-shadow);
          --border-light: var(--ds-border-raised-highlight);
          --focus-ring: var(--ds-focus-ring);
          --shadow-sm: var(--ds-shadow-raised);
          --shadow-md: 4px 4px 0 rgba(16, 16, 16, 0.21);
        }}
        body {{
          margin: 0;
          min-height: 100vh;
          color: var(--text);
          background:
            radial-gradient(circle at 18px 18px, rgba(255, 255, 255, 0.12) 0 1px, transparent 1.5px) 0 0 / 18px 18px,
            linear-gradient(180deg, rgba(255, 255, 255, 0.08), rgba(0, 0, 0, 0.08)),
            linear-gradient(180deg, var(--bg), var(--bg-shadow));
          font-family: var(--font-body);
          font-size: 16px;
          line-height: 1.5;
        }}
        body::before {{
          content: "";
          position: fixed;
          inset: 0;
          pointer-events: none;
          background: repeating-linear-gradient(180deg, rgba(255, 255, 255, 0.02) 0 2px, transparent 2px 6px);
          opacity: 0.35;
        }}
        h1, h2, h3 {{ margin: 0.8rem 0 0.4rem; font-family: var(--font-heading); line-height: 1.05; }}
        h1 {{
          display: inline-block;
          margin: 0.6rem 0 0.9rem;
          padding: 0.45rem 0.7rem;
          border: 1px solid var(--border-dark);
          background: linear-gradient(90deg, var(--title-start), var(--title-end));
          color: #fff;
          box-shadow: inset 1px 1px 0 rgba(255, 255, 255, 0.35), inset -1px -1px 0 rgba(0, 0, 0, 0.2), var(--shadow-sm);
          font-size: 1.35rem;
          letter-spacing: 0.02em;
        }}
        h2 {{ font-size: 1.35rem; }}
        h3 {{ font-size: 1rem; }}
        main {{ max-width: 1400px; margin: 0 auto; padding: 62px 14px 24px; }}
        table:not(.ds-table) {{ width: 100%; border-collapse: collapse; background: #efefef; }}
        table:not(.ds-table) th, table:not(.ds-table) td {{ border-bottom: 1px solid #8f8f8f; padding: 8px; text-align: left; vertical-align: top; }}
        fieldset, .file-card, .lookup-strip, .ops, .primary-callout, .dashboard-summary, .dashboard-lane, .job-card, .job-admin {{
          border: 1px solid var(--border-dark);
          background: var(--surface);
          box-shadow:
            inset 1px 1px 0 var(--border-light),
            inset -1px -1px 0 var(--border-mid),
            var(--shadow-sm);
        }}
        fieldset, .file-card, .lookup-strip, .ops, .primary-callout, .dashboard-summary, .dashboard-lane, .job-card, .job-admin {{ margin: 16px 0; padding: 16px; }}
        .file-card {{ padding: 12px; }}
        details.advanced-panel {{
          border: 1px solid var(--border-dark);
          background: var(--surface);
          box-shadow:
            inset 1px 1px 0 var(--border-light),
            inset -1px -1px 0 var(--border-mid),
            var(--shadow-sm);
          margin: 16px 0;
          padding: 0;
        }}
        details.advanced-panel > summary,
        details.lookup-strip > summary {{
          cursor: pointer;
          list-style: none;
          padding: 0.45rem 0.65rem;
          background: linear-gradient(90deg, var(--title-start), var(--title-end));
          color: #fff;
          text-shadow: 1px 1px 0 rgba(0, 0, 0, 0.3);
          font-weight: 700;
          user-select: none;
        }}
        details.lookup-strip > summary strong,
        details.lookup-strip > summary .muted {{ color: #fff; }}
        details.lookup-strip > summary .muted {{ opacity: 0.92; }}
        details.advanced-panel > summary::-webkit-details-marker,
        details.lookup-strip > summary::-webkit-details-marker {{ display: none; }}
        details.advanced-panel > summary::before,
        details.lookup-strip > summary::before,
        details.dashboard-lane > summary::before,
        details.file-preview-panel > summary::before,
        details.file-advanced > summary::before {{ content: "▸"; display: inline-block; margin-right: 0.45rem; }}
        details.advanced-panel[open] > summary::before,
        details.lookup-strip[open] > summary::before,
        details.dashboard-lane[open] > summary::before,
        details.file-preview-panel[open] > summary::before,
        details.file-advanced[open] > summary::before {{ content: "▾"; }}
        details.advanced-panel[open] > summary,
        details.lookup-strip[open] > summary {{ margin-bottom: 12px; }}
        details.advanced-panel > :not(summary),
        details.lookup-strip > :not(summary) {{ padding-left: 16px; padding-right: 16px; }}
        details.advanced-panel > :last-child,
        details.lookup-strip > :last-child {{ padding-bottom: 16px; }}
        details.lookup-strip {{ display: grid; gap: 12px; align-items: start; }}
        .lookup-strip .wide {{ grid-column: 1 / -1; }}
        .candidate-grid {{ display: grid; gap: 12px; grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)); }}
        .candidate-card {{ border: 1px solid var(--border-dark); background: linear-gradient(180deg, #f2f2f2, #dadada); box-shadow: inset 1px 1px 0 var(--border-light), inset -1px -1px 0 var(--border-mid); padding: 12px; display: grid; gap: 8px; }}
        .candidate-card img {{ width: 100%; max-width: 140px; aspect-ratio: 2 / 3; object-fit: cover; border: 1px solid var(--border-dark); box-shadow: inset 1px 1px 0 var(--border-light), inset -1px -1px 0 var(--border-mid); background: #d0d0d0; }}
        .candidate-fields {{ display: flex; flex-wrap: wrap; gap: 8px; }}
        .candidate-fields span {{ display: inline-block; padding: 3px 8px; border: 1px solid var(--border-dark); background: #e8e8e8; box-shadow: inset 1px 1px 0 var(--border-light), inset -1px -1px 0 var(--border-mid); font-size: 0.82rem; }}
        label {{ display: flex; flex-direction: column; gap: 4px; font-size: 0.9rem; }}
        input:not(.ds-control), select:not(.ds-control), textarea:not(.ds-control), button:not(.ds-button) {{ font: inherit; }}
        input:not(.ds-control), select:not(.ds-control), textarea:not(.ds-control) {{
          border: 1px solid var(--border-dark);
          background: #fff;
          box-shadow: inset 1px 1px 0 var(--border-light), inset -1px -1px 0 var(--border-mid);
          padding: 7px 8px;
        }}
        button:not(.ds-button) {{
          border: 1px solid var(--border-dark);
          background: var(--surface-soft);
          box-shadow: inset 1px 1px 0 var(--border-light), inset -1px -1px 0 var(--border-mid), var(--shadow-sm);
          padding: 7px 12px;
          min-height: 2.3rem;
          cursor: pointer;
          font-weight: 700;
        }}
        button:not(.ds-button):active {{
          box-shadow: inset -1px -1px 0 var(--border-light), inset 1px 1px 0 var(--border-mid);
          transform: translate(1px, 1px);
        }}
        .floating-back-link {{
          position: fixed;
          top: 12px;
          left: 12px;
          z-index: 1000;
          display: inline-block;
          padding: 7px 12px;
          border: 1px solid var(--border-dark);
          background: var(--surface-soft);
          color: var(--text);
          text-decoration: none;
          box-shadow: inset 1px 1px 0 var(--border-light), inset -1px -1px 0 var(--border-mid), var(--shadow-sm);
          font-weight: 700;
        }}
        .floating-back-link:active {{
          box-shadow: inset -1px -1px 0 var(--border-light), inset 1px 1px 0 var(--border-mid);
          transform: translate(1px, 1px);
        }}
        .review-header {{ display: grid; gap: 12px; }}
        .review-header > .dashboard-summary-header {{
          margin: -16px -16px 0;
          padding: 12px 16px;
          background: linear-gradient(90deg, var(--title-start), var(--title-end));
          color: #fff;
          box-shadow: inset 1px 1px 0 rgba(255,255,255,0.35), inset -1px -1px 0 rgba(0,0,0,0.2);
        }}
        .review-header {{ padding: 12px 14px; }}
        .review-header > .dashboard-summary-header {{ gap: 10px; align-items: flex-start; }}
        .review-header > .dashboard-summary-header h2 {{ margin: 2px 0 4px; font-size: 1.35rem; line-height: 1.08; }}
        .review-header > .dashboard-summary-header p {{ margin: 0.15rem 0; }}
        .review-header > .dashboard-summary-header .muted {{ color: #fff; }}
        .review-header > .dashboard-summary-header .dashboard-flags {{ align-items: flex-end; gap: 6px; }}
        .job-summary-topline {{ display: flex; align-items: center; gap: 12px; flex-wrap: wrap; }}
        .job-summary-topline .eyebrow {{ margin: 0; }}
        .job-summary-number {{ display: inline-block; padding: 3px 8px; border: 1px solid rgba(255,255,255,0.7); background: rgba(255,255,255,0.14); color: #fff; box-shadow: inset 1px 1px 0 rgba(255,255,255,0.28); font-size: 0.78rem; font-weight: 700; letter-spacing: 0.02em; }}
        .job-summary-actions {{ margin-top: 8px; gap: 6px; align-items: center; }}
        .job-summary-actions button:not(.ds-button) {{ padding: 4px 8px; font-size: 0.86rem; line-height: 1.05; }}
        .job-summary-actions .primary-action:not(.ds-button) {{ background: linear-gradient(180deg, #eaf3ff, #c5ddff); }}
        .job-summary-actions .danger-action:not(.ds-button) {{ background: linear-gradient(180deg, #ffe7e7, #f2b6b6); color: #611111; }}
        .job-summary-actions .danger-action:not(.ds-button):hover {{ background: linear-gradient(180deg, #ffd9d9, #efaaaa); }}
        .job-summary-create-box {{ display: inline-flex; align-items: center; gap: 6px; padding: 5px 7px; margin-left: auto; border: 1px solid var(--border-dark); background: var(--surface-raised); box-shadow: inset 1px 1px 0 var(--border-light), inset -1px -1px 0 var(--border-mid); flex-wrap: wrap; }}
        .job-summary-create-box .split-picker {{ display: inline-flex; align-items: center; gap: 6px; margin: 0; white-space: nowrap; }}
        .job-summary-create-box select {{ min-width: 140px; }}
        .pipeline-progress {{ display: flex; flex-wrap: wrap; gap: 8px; align-items: center; }}
        .pipeline-step {{ display: inline-block; padding: 4px 8px; border: 1px solid var(--border-dark); background: #e8e8e8; box-shadow: inset 1px 1px 0 var(--border-light), inset -1px -1px 0 var(--border-mid); font-size: 0.82rem; font-weight: 700; }}
        .pipeline-arrow {{ font-weight: 700; color: var(--text-muted); }}
        .pipeline-step-done {{ background: #cfe8cf; }}
        .pipeline-step-current {{ background: #f5d48d; }}
        .pipeline-step-blocked {{ background: #f1c3c3; }}
        .pipeline-step-pending {{ background: #e5e5e5; color: #595959; }}
        .job-errors {{ display: block; width: 100%; clear: both; margin: 0 0 12px; }}
        .job-errors p {{ margin: 0.25rem 0; }}
        textarea:not(.ds-control) {{ min-height: 70px; resize: vertical; }}
        .review-stack, .advanced-grid, .file-fields, fieldset.primary-metadata {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 12px; }}
        fieldset.primary-metadata {{ position: relative; padding: 18px 16px 16px; }}
        fieldset.primary-metadata > legend {{
          padding: 0.35rem 0.65rem;
          border: 1px solid var(--border-dark);
          background: linear-gradient(90deg, var(--title-start), var(--title-end));
          color: #fff;
          font-weight: 700;
          font-size: 0.78rem;
          text-shadow: 1px 1px 0 rgba(0, 0, 0, 0.3);
          margin-left: 6px;
          box-shadow: inset 1px 1px 0 rgba(255, 255, 255, 0.35), inset -1px -1px 0 rgba(0, 0, 0, 0.2);
        }}
        fieldset.primary-metadata::before {{ content: none; }}
        .file-task-panel {{
          border: 1px solid var(--border-dark);
          background: #d6d6d6;
          box-shadow: inset 1px 1px 0 var(--border-light), inset -1px -1px 0 var(--border-mid);
          padding: 0;
        }}
        .file-task-panel > summary {{
          cursor: pointer;
          list-style: none;
          padding: 0.45rem 0.65rem;
          background: linear-gradient(90deg, var(--title-start), var(--title-end));
          color: #fff;
          text-shadow: 1px 1px 0 rgba(0, 0, 0, 0.3);
          font-weight: 700;
          user-select: none;
        }}
        .file-task-panel > summary::-webkit-details-marker {{ display: none; }}
        .file-task-panel[open] > summary {{ margin-bottom: 12px; }}
        .file-task-panel > :not(summary) {{ padding-left: 12px; padding-right: 12px; padding-bottom: 12px; }}
        .file-task-summary {{ display: flex; justify-content: space-between; align-items: center; gap: 10px; flex-wrap: wrap; }}
        .file-task-summary strong {{ display: block; font-size: 1rem; }}
        .file-task-body {{ display: grid; gap: 10px; }}
        .file-task-columns {{ display: grid; grid-template-columns: minmax(260px, 340px) minmax(0, 1fr); gap: 10px; align-items: start; }}
        .file-fields-primary {{ grid-template-columns: repeat(auto-fit, minmax(170px, 1fr)); }}
        .file-fields-advanced {{ grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); }}
        .file-task-copy p {{ margin-bottom: 0; }}
        @media (max-width: 900px) {{
          .file-task-columns {{ grid-template-columns: 1fr; }}
        }}
        .file-card-header, .dashboard-summary-header {{ display: flex; justify-content: space-between; gap: 10px; align-items: start; flex-wrap: wrap; }}
        .file-card-header h3 {{ margin-top: 0; }}
        .file-card-header-actions {{ display: flex; flex-wrap: wrap; gap: 6px; justify-content: flex-end; align-items: flex-start; margin-left: auto; }}
        .file-card-header-actions button {{ white-space: nowrap; }}
        .file-card-badges {{ display: inline-flex; gap: 8px; flex-wrap: wrap; align-items: center; margin-top: 8px; }}
        .file-card-badge {{ display: inline-block; padding: 3px 8px; border: 1px solid var(--border-dark); background: #e7e7e7; box-shadow: inset 1px 1px 0 var(--border-light), inset -1px -1px 0 var(--border-mid); font-size: 0.82rem; font-weight: 700; }}
        .file-card-badge-attention {{ background: #f0d6d6; color: #651515; }}
        .file-card-badge-skip {{ background: #e4e4e4; color: #5f5f5f; }}
        .file-card-badge-new {{ background: #dde8f5; color: #173b66; }}
        .file-preview-panel {{
          border: 1px solid var(--border-dark);
          background: #e4e4e4;
          box-shadow: inset 1px 1px 0 var(--border-light), inset -1px -1px 0 var(--border-mid);
          margin: 12px 0;
          padding: 0;
        }}
        .file-preview-panel > summary {{
          cursor: pointer;
          list-style: none;
          padding: 0.35rem 0.6rem;
          background: linear-gradient(90deg, #8f8f8f, #b0b0b0);
          color: #111;
          font-weight: 700;
          user-select: none;
        }}
        .file-preview-panel > summary::-webkit-details-marker {{ display: none; }}
        .file-preview-panel[open] > summary {{ margin-bottom: 8px; }}
        .file-preview-panel > :not(summary) {{ padding: 0 12px 12px; }}
        .file-preview-body p {{ margin: 0; }}
        .wide {{ grid-column: 1 / -1; }}
        .tech, .dashboard-metrics {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 8px; }}
        .dashboard-status-grid {{ grid-template-columns: repeat(9, minmax(0, 1fr)); gap: 8px; }}
        .dashboard-status-grid .dashboard-metric {{ padding: 10px 12px; gap: 2px; }}
        .dashboard-status-grid .dashboard-metric span {{ font-size: 0.92rem; line-height: 1.1; }}
        .dashboard-status-grid .dashboard-metric strong {{ font-size: 1.2rem; }}
        .job-chips {{ display: flex; flex-wrap: wrap; gap: 8px; }}
        .job-chips span {{ display: inline-block; padding: 4px 8px; border: 1px solid var(--border-dark); background: #e7e7e7; box-shadow: inset 1px 1px 0 var(--border-light), inset -1px -1px 0 var(--border-mid); font-size: 0.85rem; }}
        .dashboard-queue {{ display: grid; gap: 16px; }}
        .dashboard-card-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); gap: 12px; align-items: stretch; }}
        .dashboard-card-grid > * {{ min-width: 0; }}
        .dashboard-metric {{ padding: 12px; border: 1px solid var(--border-dark); background: #e8e8e8; box-shadow: inset 1px 1px 0 var(--border-light), inset -1px -1px 0 var(--border-mid); display: flex; flex-direction: column; gap: 4px; }}
        .dashboard-metric strong {{ font-size: 1.35rem; }}
        .dashboard-lane {{ display: grid; gap: 12px; }}
        .dashboard-lane > summary {{
          cursor: pointer;
          list-style: none;
          display: flex;
          justify-content: space-between;
          align-items: center;
          gap: 12px;
          flex-wrap: wrap;
          padding: 0.45rem 0.65rem;
          background: linear-gradient(90deg, var(--title-start), var(--title-end));
          color: #fff;
          text-shadow: 1px 1px 0 rgba(0, 0, 0, 0.3);
          font-weight: 700;
          user-select: none;
        }}
        .dashboard-lane > summary::-webkit-details-marker {{ display: none; }}
        .dashboard-lane[open] > summary {{ margin-bottom: 12px; }}
        .dashboard-lane-body {{ display: grid; gap: 12px; }}
        .lane-badges {{ display: flex; flex-wrap: wrap; gap: 8px; justify-content: flex-end; }}
        .lane-badges span {{ display: inline-block; padding: 3px 8px; border: 1px solid rgba(255,255,255,0.8); background: rgba(255,255,255,0.16); color: #fff; box-shadow: inset 1px 1px 0 rgba(255,255,255,0.25); font-size: 0.82rem; }}
        .dashboard-lane header {{ display: flex; justify-content: space-between; align-items: baseline; gap: 12px; flex-wrap: wrap; }}
        .job-card-link {{ display: block; color: inherit; text-decoration: none; min-width: 0; width: 100%; height: 100%; }}
        .job-card-link:focus-visible .job-card,
        .job-card-link:hover .job-card {{ outline: 2px solid var(--title-start); outline-offset: 2px; box-shadow: 0 3px 0 rgba(0,0,0,0.18), inset 1px 1px 0 var(--border-light), inset -1px -1px 0 var(--border-mid); transform: translateY(-1px); }}
        .job-card-link:active .job-card {{ transform: translateY(1px); box-shadow: inset 2px 2px 0 rgba(0,0,0,0.18), inset -1px -1px 0 var(--border-light); }}
        .job-card {{ margin: 0; display: flex; flex-direction: column; gap: 10px; min-width: 0; width: 100%; height: 100%; overflow: hidden; overflow-wrap: anywhere; word-break: break-word; box-sizing: border-box; }}
        .job-title {{ margin: 0; font-size: 1.1rem; font-weight: 700; color: var(--title-start); overflow-wrap: anywhere; word-break: break-word; }}
        .job-card-header {{ display: flex; justify-content: space-between; align-items: flex-start; gap: 12px; min-width: 0; flex-wrap: wrap; }}
        .job-card-header > div {{ min-width: 0; flex: 1 1 0; }}
        .status-badge {{ display: inline-block; padding: 4px 8px; border: 1px solid var(--border-dark); background: #e8e8e8; box-shadow: inset 1px 1px 0 var(--border-light), inset -1px -1px 0 var(--border-mid); font-size: 0.8rem; }}
        .status-warm {{ background: #f4d48a; }}
        .status-cool {{ background: #cfe0f7; }}
        .status-hot {{ background: #f0b6b6; }}
        .status-done {{ background: #c9e8cb; }}
        .dashboard-lane-collapsed > summary {{ cursor: pointer; font-weight: 700; list-style: none; }}
        .dashboard-lane-collapsed > summary::-webkit-details-marker {{ display: none; }}
        .dashboard-lane-collapsed[open] > summary {{ margin-bottom: 12px; }}
        .media-review {{ margin: 0; display: grid; gap: 6px; align-self: start; max-width: 320px; }}
        .media-review > video,
        .media-thumb {{ display: block; width: 100%; max-width: 100%; aspect-ratio: 16 / 9; object-fit: cover; background: #111827; border: 1px solid var(--border-dark); box-shadow: inset 1px 1px 0 var(--border-light), inset -1px -1px 0 var(--border-mid); }}
        .media-review a {{ display: block; width: 100%; }}
        .media-review code {{ display: block; overflow-wrap: anywhere; word-break: break-word; white-space: normal; font-size: 0.92rem; }}
        .media-actions {{ display: flex; flex-wrap: wrap; gap: 6px; align-items: flex-start; }}
        .media-actions button {{ white-space: nowrap; }}
        .inline-form {{ display: flex; align-items: start; gap: 6px; margin-top: 4px; flex-wrap: wrap; }}
        .job-admin .inline-form {{ margin-top: 0; }}
        .destination-preview {{ margin: 10px 0 0; padding: 10px 12px; border: 1px solid var(--border-dark); background: var(--surface-raised); box-shadow: inset 1px 1px 0 var(--border-light), inset -1px -1px 0 var(--border-mid); }}
        .destination-preview p {{ margin: 0.2rem 0; }}
        .destination-preview code {{ display: block; overflow-wrap: anywhere; word-break: break-word; white-space: normal; font-size: 0.92rem; }}
        .file-card .file-task-columns {{ margin-top: 10px; }}
        .file-card .file-fields-primary {{ align-content: start; }}
        .file-card-header p {{ margin-bottom: 0; }}
        .file-card-badges {{ display: inline-flex; gap: 8px; flex-wrap: wrap; align-items: center; margin-top: 8px; }}
        .file-card-badge {{ display: inline-block; padding: 3px 8px; border: 1px solid var(--border-dark); background: #e7e7e7; box-shadow: inset 1px 1px 0 var(--border-light), inset -1px -1px 0 var(--border-mid); font-size: 0.82rem; font-weight: 700; }}
        .file-card-badge-attention {{ background: #f0d6d6; color: #651515; }}
        .file-card-badge-skip {{ background: #e4e4e4; color: #5f5f5f; }}
        .file-card-badge-new {{ background: #dde8f5; color: #173b66; }}
        .errors {{ color: #7d1010; background: #f4c6c6; border: 1px solid #7d1010; box-shadow: inset 1px 1px 0 rgba(255,255,255,0.7); border-radius: 0; padding: 8px; }}
        .muted {{ color: var(--text-muted); }}
        .advanced-note, .eyebrow {{ font-size: 0.92rem; line-height: 1.4; }}
        .eyebrow {{ text-transform: uppercase; letter-spacing: 0.08em; color: var(--text-muted); margin: 0; }}
        .dashboard-flags {{ display: flex; flex-direction: column; gap: 4px; align-items: flex-end; }}
        .dashboard-notes ul {{ margin: 0.5rem 0 0; padding-left: 1.2rem; }}
        code {{ word-break: break-all; }}
      </style>
    </head>
    <body data-ds-theme="win31"><main><section class="ds-window app-window ds-motion-enter-window"><div class="ds-titlebar"><span>{escape(title)}</span></div><div class="ds-window__body">{body}</div></section></main>
      <script>
      (() => {{
        window.updateDestinationPreviews = () => {{}};
        const init = () => {{
        const form = document.getElementById('job-review-form');
        if (!form) return;

        const sanitize = (value) => {{
          const cleaned = String(value || '')
            .replace(/[<>:\"/\\|?*\x00-\x1f]/g, ' ')
            .replace(/\s+/g, ' ')
            .trim()
            .replace(/[. ]+$/g, '');
          return cleaned || 'Untitled';
        }};

        const fieldValue = (name) => form.querySelector(`[name="${{name}}"]`)?.value?.trim() || '';
        const extraRoles = new Set(['extra', 'trailer', 'featurette', 'deleted_scene', 'interview', 'music_video', 'short_film', 'promo', 'alternate_cut', 'commentary_variant']);
        const roleSubdirectories = {{
          trailer: 'trailers',
          promo: 'trailers',
          featurette: 'featurettes',
          deleted_scene: 'deleted-scenes',
          menu_or_bumper: 'menus',
        }};

        const updatePreview = (card) => {{
          const preview = card.querySelector('[data-preview-path]');
          if (!preview) return;
          const include = card.querySelector('input[type="checkbox"][name$="include"]');
          if (include && !include.checked) {{
            preview.dataset.currentPath = 'Skipped / do not process.';
            preview.textContent = 'Skipped / do not process.';
            return;
          }}

          const jobTitle = sanitize(fieldValue('title'));
          const yearValue = fieldValue('year');
          const contentType = fieldValue('content_type');
          const libraryRoot = fieldValue('library_root');
          const role = card.querySelector('select[name$="role"]')?.value || '';
          const fileContentType = card.querySelector('select[name$="content_type"]')?.value || '';
          const extraType = card.querySelector('input[name$="extra_type"]')?.value?.trim() || '';
          const displayName = card.querySelector('input[name$="final_display_name"]')?.value?.trim();
          const translatedTitle = card.querySelector('input[name$="translated_title"]')?.value?.trim();
          const romanizedTitle = card.querySelector('input[name$="romanized_title"]')?.value?.trim();
          const originalTitle = card.querySelector('input[name$="original_title"]')?.value?.trim();
          const finalFilename = card.querySelector('input[name$="final_filename"]')?.value?.trim();
          const season = card.querySelector('input[name$="season_number"]')?.value?.trim();
          const episode = card.querySelector('input[name$="episode_number"]')?.value?.trim();
          const label = sanitize(displayName || translatedTitle || romanizedTitle || originalTitle || jobTitle);
          const movieFolder = yearValue ? `${{jobTitle}} (${{parseInt(yearValue, 10)}})` : jobTitle;
          const episodeLike = contentType === 'show' || contentType === 'anime' || role === 'episode' || !!season;
          const extraLike = extraRoles.has(role) || fileContentType === 'extra' || extraType === 'extra';

          let filename = '';
          if (finalFilename) {{
            filename = sanitize(finalFilename);
            if (!filename.toLowerCase().endsWith('.mkv')) {{
              filename = `${{filename}}.mkv`;
            }}
          }} else if (episodeLike) {{
            const seasonValue = String(parseInt(season || '1', 10)).padStart(2, '0');
            const episodeValue = String(parseInt(episode || '1', 10)).padStart(2, '0');
            filename = `${{jobTitle}} - S${{seasonValue}}E${{episodeValue}} - ${{label}}.mkv`;
          }} else if (extraLike) {{
            filename = `${{label}}.mkv`;
          }} else {{
            filename = `${{movieFolder}}.mkv`;
          }}

          let path = '';
          if (episodeLike) {{
            const seasonValue = String(parseInt(season || '1', 10)).padStart(2, '0');
            path = [libraryRoot, jobTitle, `Season ${{seasonValue}}`, filename].filter(Boolean).join('/');
          }} else if (extraLike) {{
            const subdirectory = roleSubdirectories[role] || 'extras';
            path = [libraryRoot, movieFolder, subdirectory, filename].filter(Boolean).join('/');
          }} else {{
            path = [libraryRoot, movieFolder, filename].filter(Boolean).join('/');
          }}

          preview.dataset.currentPath = path;
          preview.textContent = path;
        }};

        const updateAllDestinationPreviews = () => {{
          for (const card of document.querySelectorAll('.file-card')) {{
            updatePreview(card);
          }}
        }};

        window.updateDestinationPreviews = updateAllDestinationPreviews;

        document.addEventListener('input', (event) => {{
          if (!event.target.closest('#job-review-form')) return;
          updateAllDestinationPreviews();
        }});
        document.addEventListener('change', (event) => {{
          if (!event.target.closest('#job-review-form')) return;
          updateAllDestinationPreviews();
        }});
        document.addEventListener('click', async (event) => {{
          const button = event.target.closest('button[data-system-open-action]');
          if (!button) return;
          event.preventDefault();
          event.stopPropagation();
          const actionUrl = button.dataset.systemOpenAction;
          if (!actionUrl) return;
          const originalText = button.textContent;
          button.disabled = true;
          try {{
            await fetch(actionUrl, {{ method: 'POST', credentials: 'same-origin' }});
          }} catch (error) {{
            console.error('Failed to open file in system handler', error);
          }} finally {{
            button.disabled = false;
            button.textContent = originalText;
          }}
        }});

        updateAllDestinationPreviews();
        }};
        if (document.readyState === 'loading') {{
          document.addEventListener('DOMContentLoaded', init, {{ once: true }});
        }} else {{
          init();
        }}
      }})();

    </html>"""
