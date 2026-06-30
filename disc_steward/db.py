from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict
from pathlib import Path

from .models import Classification, FileReviewDecision, Job, JobReviewMetadata, ScannedFile, SourceFileRecord


class Database:
    def __init__(self, path: str | Path):
        self.path = Path(path)

    def connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def initialize(self) -> None:
        with self.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS disc_jobs (
                    id INTEGER PRIMARY KEY,
                    disc_title TEXT NOT NULL,
                    disc_path TEXT NOT NULL UNIQUE,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS source_files (
                    id INTEGER PRIMARY KEY,
                    job_id INTEGER NOT NULL REFERENCES disc_jobs(id) ON DELETE CASCADE,
                    path TEXT NOT NULL UNIQUE,
                    filename TEXT NOT NULL,
                    parent_disc_folder TEXT NOT NULL,
                    size_bytes INTEGER NOT NULL,
                    modified_time REAL NOT NULL,
                    identity_key TEXT NOT NULL,
                    duration_seconds REAL,
                    container_format TEXT,
                    video_json TEXT NOT NULL,
                    audio_json TEXT NOT NULL,
                    subtitle_json TEXT NOT NULL,
                    chapter_count INTEGER NOT NULL DEFAULT 0,
                    embedded_title TEXT,
                    makemkv_title TEXT,
                    raw_ffprobe_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS classifications (
                    source_file_id INTEGER PRIMARY KEY REFERENCES source_files(id) ON DELETE CASCADE,
                    classification_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS review_decisions (
                    id INTEGER PRIMARY KEY,
                    source_file_id INTEGER NOT NULL REFERENCES source_files(id) ON DELETE CASCADE,
                    decision_json TEXT NOT NULL,
                    approved INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS job_reviews (
                    job_id INTEGER PRIMARY KEY REFERENCES disc_jobs(id) ON DELETE CASCADE,
                    title TEXT NOT NULL DEFAULT '',
                    original_title TEXT,
                    romanized_title TEXT,
                    translated_title TEXT,
                    language_script_hints TEXT,
                    anime_flag INTEGER NOT NULL DEFAULT 0,
                    japanese_media_flag INTEGER NOT NULL DEFAULT 0,
                    confidence REAL,
                    manual_review_notes TEXT,
                    year INTEGER,
                    content_type TEXT NOT NULL DEFAULT 'unknown',
                    library_root TEXT NOT NULL DEFAULT 'Movies',
                    imdb_id TEXT,
                    tmdb_id TEXT,
                    tvdb_id TEXT,
                    anidb_id TEXT,
                    anilist_id TEXT,
                    mal_id TEXT,
                    notes TEXT,
                    review_status TEXT NOT NULL DEFAULT 'review_needed',
                    work_order_folder TEXT,
                    work_order_created_at TEXT,
                    warnings_json TEXT NOT NULL DEFAULT '[]',
                    conflicts_json TEXT NOT NULL DEFAULT '[]',
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS file_reviews (
                    source_file_id INTEGER PRIMARY KEY REFERENCES source_files(id) ON DELETE CASCADE,
                    include_in_work_order INTEGER NOT NULL DEFAULT 1,
                    role TEXT NOT NULL DEFAULT '',
                    content_type TEXT NOT NULL DEFAULT 'unknown',
                    final_display_name TEXT,
                    final_filename TEXT,
                    original_title TEXT,
                    translated_title TEXT,
                    romanized_title TEXT,
                    imdb_id TEXT,
                    tmdb_id TEXT,
                    tvdb_id TEXT,
                    anidb_id TEXT,
                    anilist_id TEXT,
                    mal_id TEXT,
                    extra_type TEXT,
                    season_number INTEGER,
                    episode_number INTEGER,
                    sort_order INTEGER,
                    encoding_profile TEXT NOT NULL DEFAULT '',
                    subtitle_policy TEXT NOT NULL DEFAULT '',
                    generated_final_path TEXT,
                    notes TEXT,
                    warnings_json TEXT NOT NULL DEFAULT '[]',
                    conflicts_json TEXT NOT NULL DEFAULT '[]',
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS work_orders (
                    id INTEGER PRIMARY KEY,
                    job_id INTEGER NOT NULL REFERENCES disc_jobs(id) ON DELETE CASCADE,
                    source_file_id INTEGER NOT NULL REFERENCES source_files(id) ON DELETE CASCADE,
                    work_order_path TEXT NOT NULL,
                    status TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS validation_results (
                    id INTEGER PRIMARY KEY,
                    job_id INTEGER NOT NULL REFERENCES disc_jobs(id) ON DELETE CASCADE,
                    result_json TEXT NOT NULL,
                    passed INTEGER NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS transfer_results (
                    id INTEGER PRIMARY KEY,
                    job_id INTEGER NOT NULL REFERENCES disc_jobs(id) ON DELETE CASCADE,
                    result_json TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS output_files (
                    id INTEGER PRIMARY KEY,
                    job_id INTEGER NOT NULL REFERENCES disc_jobs(id) ON DELETE CASCADE,
                    source_file_id INTEGER NOT NULL REFERENCES source_files(id) ON DELETE CASCADE,
                    expected_output_name TEXT NOT NULL,
                    matched_output_path TEXT,
                    final_library_path TEXT NOT NULL,
                    status TEXT NOT NULL,
                    ffprobe_summary_json TEXT NOT NULL DEFAULT '{}',
                    detected_streams_json TEXT NOT NULL DEFAULT '{}',
                    warnings_json TEXT NOT NULL DEFAULT '[]',
                    errors_json TEXT NOT NULL DEFAULT '[]',
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS validation_warnings (
                    id INTEGER PRIMARY KEY,
                    job_id INTEGER NOT NULL REFERENCES disc_jobs(id) ON DELETE CASCADE,
                    source_file_id INTEGER,
                    severity TEXT NOT NULL,
                    message TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS transfer_attempts (
                    id INTEGER PRIMARY KEY,
                    job_id INTEGER NOT NULL REFERENCES disc_jobs(id) ON DELETE CASCADE,
                    status TEXT NOT NULL,
                    result_json TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS transfer_items (
                    id INTEGER PRIMARY KEY,
                    job_id INTEGER NOT NULL REFERENCES disc_jobs(id) ON DELETE CASCADE,
                    source_file_id INTEGER NOT NULL REFERENCES source_files(id) ON DELETE CASCADE,
                    source_output_path TEXT NOT NULL,
                    incoming_path TEXT NOT NULL,
                    final_path TEXT NOT NULL,
                    status TEXT NOT NULL,
                    conflict TEXT,
                    error TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS jellyfin_refresh_attempts (
                    id INTEGER PRIMARY KEY,
                    job_id INTEGER NOT NULL REFERENCES disc_jobs(id) ON DELETE CASCADE,
                    status TEXT NOT NULL,
                    response_json TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS conflict_records (
                    id INTEGER PRIMARY KEY,
                    job_id INTEGER NOT NULL REFERENCES disc_jobs(id) ON DELETE CASCADE,
                    source_file_id INTEGER,
                    path TEXT,
                    reason TEXT NOT NULL,
                    resolved INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS manual_overrides (
                    id INTEGER PRIMARY KEY,
                    job_id INTEGER NOT NULL REFERENCES disc_jobs(id) ON DELETE CASCADE,
                    source_file_id INTEGER NOT NULL REFERENCES source_files(id) ON DELETE CASCADE,
                    override_type TEXT NOT NULL,
                    note TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS audit_log (
                    id INTEGER PRIMARY KEY,
                    job_id INTEGER,
                    event_type TEXT NOT NULL,
                    message TEXT NOT NULL,
                    payload_json TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS subtitle_plans (
                    source_file_id INTEGER PRIMARY KEY REFERENCES source_files(id) ON DELETE CASCADE,
                    plan_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS subtitle_validation_results (
                    id INTEGER PRIMARY KEY,
                    job_id INTEGER NOT NULL REFERENCES disc_jobs(id) ON DELETE CASCADE,
                    source_file_id INTEGER NOT NULL REFERENCES source_files(id) ON DELETE CASCADE,
                    result_json TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS metadata_candidates (
                    id INTEGER PRIMARY KEY,
                    job_id INTEGER NOT NULL REFERENCES disc_jobs(id) ON DELETE CASCADE,
                    source_file_id INTEGER,
                    provider TEXT NOT NULL,
                    candidate_json TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS llm_suggestion_requests (
                    id INTEGER PRIMARY KEY,
                    job_id INTEGER NOT NULL REFERENCES disc_jobs(id) ON DELETE CASCADE,
                    provider TEXT NOT NULL,
                    request_json TEXT NOT NULL,
                    response_json TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS llm_suggestions (
                    id INTEGER PRIMARY KEY,
                    job_id INTEGER NOT NULL REFERENCES disc_jobs(id) ON DELETE CASCADE,
                    source_file_id INTEGER,
                    suggestion_type TEXT NOT NULL,
                    suggestion_json TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'suggested',
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS cleanup_eligibility (
                    id INTEGER PRIMARY KEY,
                    job_id INTEGER NOT NULL REFERENCES disc_jobs(id) ON DELETE CASCADE,
                    path TEXT NOT NULL,
                    item_type TEXT NOT NULL,
                    eligible INTEGER NOT NULL,
                    reason TEXT NOT NULL,
                    archive_path TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS cleanup_holds (
                    job_id INTEGER PRIMARY KEY REFERENCES disc_jobs(id) ON DELETE CASCADE,
                    hold INTEGER NOT NULL DEFAULT 0,
                    reason TEXT,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS cleanup_attempts (
                    id INTEGER PRIMARY KEY,
                    job_id INTEGER,
                    status TEXT NOT NULL,
                    result_json TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS archive_results (
                    id INTEGER PRIMARY KEY,
                    job_id INTEGER NOT NULL REFERENCES disc_jobs(id) ON DELETE CASCADE,
                    source_path TEXT NOT NULL,
                    archive_path TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS status_dashboard_cache (
                    id INTEGER PRIMARY KEY,
                    summary_json TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS jellyfin_transcode_findings (
                    id INTEGER PRIMARY KEY,
                    job_id INTEGER,
                    final_path TEXT,
                    reason TEXT,
                    finding_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_review_decisions_source_file_id
                ON review_decisions(source_file_id);
                """
            )
            self._ensure_column(conn, "job_reviews", "romanized_title", "TEXT")
            self._ensure_column(conn, "job_reviews", "translated_title", "TEXT")
            self._ensure_column(conn, "job_reviews", "language_script_hints", "TEXT")
            self._ensure_column(conn, "job_reviews", "anime_flag", "INTEGER NOT NULL DEFAULT 0")
            self._ensure_column(conn, "job_reviews", "japanese_media_flag", "INTEGER NOT NULL DEFAULT 0")
            self._ensure_column(conn, "job_reviews", "confidence", "REAL")
            self._ensure_column(conn, "job_reviews", "manual_review_notes", "TEXT")

    @staticmethod
    def _ensure_column(conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
        columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        if column not in columns:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    def upsert_job(self, disc_path: Path, status: str = "scanned") -> int:
        resolved = str(disc_path.resolve())
        title = disc_path.name
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO disc_jobs (disc_title, disc_path, status)
                VALUES (?, ?, ?)
                ON CONFLICT(disc_path) DO UPDATE SET
                    status=excluded.status,
                    updated_at=CURRENT_TIMESTAMP
                """,
                (title, resolved, status),
            )
            return int(conn.execute("SELECT id FROM disc_jobs WHERE disc_path = ?", (resolved,)).fetchone()["id"])

    def upsert_source_file(self, job_id: int, scanned: ScannedFile) -> int:
        identity = f"{scanned.path}|{scanned.size_bytes}|{scanned.modified_time}"
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO source_files (
                    job_id, path, filename, parent_disc_folder, size_bytes, modified_time, identity_key,
                    duration_seconds, container_format, video_json, audio_json, subtitle_json,
                    chapter_count, embedded_title, makemkv_title, raw_ffprobe_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(path) DO UPDATE SET
                    job_id=excluded.job_id,
                    filename=excluded.filename,
                    parent_disc_folder=excluded.parent_disc_folder,
                    size_bytes=excluded.size_bytes,
                    modified_time=excluded.modified_time,
                    identity_key=excluded.identity_key,
                    duration_seconds=excluded.duration_seconds,
                    container_format=excluded.container_format,
                    video_json=excluded.video_json,
                    audio_json=excluded.audio_json,
                    subtitle_json=excluded.subtitle_json,
                    chapter_count=excluded.chapter_count,
                    embedded_title=excluded.embedded_title,
                    makemkv_title=excluded.makemkv_title,
                    raw_ffprobe_json=excluded.raw_ffprobe_json,
                    updated_at=CURRENT_TIMESTAMP
                """,
                (
                    job_id,
                    scanned.path,
                    scanned.filename,
                    scanned.parent_disc_folder,
                    scanned.size_bytes,
                    scanned.modified_time,
                    identity,
                    scanned.duration_seconds,
                    scanned.container_format,
                    json.dumps(scanned.video.__dict__, ensure_ascii=False),
                    json.dumps([s.__dict__ for s in scanned.audio_streams], ensure_ascii=False),
                    json.dumps([s.__dict__ for s in scanned.subtitle_streams], ensure_ascii=False),
                    scanned.chapter_count,
                    scanned.embedded_title,
                    scanned.makemkv_title,
                    json.dumps(scanned.raw_ffprobe, ensure_ascii=False),
                ),
            )
            return int(conn.execute("SELECT id FROM source_files WHERE path = ?", (scanned.path,)).fetchone()["id"])

    def save_classification(self, source_file_id: int, classification: Classification) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO classifications (source_file_id, classification_json)
                VALUES (?, ?)
                ON CONFLICT(source_file_id) DO UPDATE SET
                    classification_json=excluded.classification_json,
                    updated_at=CURRENT_TIMESTAMP
                """,
                (source_file_id, json.dumps(classification.__dict__, ensure_ascii=False)),
            )

    def list_jobs(self) -> list[Job]:
        with self.connect() as conn:
            rows = conn.execute("SELECT id, disc_title, disc_path, status FROM disc_jobs ORDER BY id").fetchall()
        return [Job(id=row["id"], disc_title=row["disc_title"], disc_path=row["disc_path"], status=row["status"]) for row in rows]

    def get_job(self, job_id: int) -> Job | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT id, disc_title, disc_path, status FROM disc_jobs WHERE id = ?",
                (job_id,),
            ).fetchone()
        if row is None:
            return None
        return Job(id=row["id"], disc_title=row["disc_title"], disc_path=row["disc_path"], status=row["status"])

    def update_job_status(self, job_id: int, status: str) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE disc_jobs SET status = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (status, job_id),
            )

    def list_source_files(self, job_id: int) -> list[SourceFileRecord]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT id, job_id, path, filename, size_bytes, modified_time, duration_seconds
                FROM source_files WHERE job_id = ? ORDER BY duration_seconds DESC NULLS LAST, filename
                """,
                (job_id,),
            ).fetchall()
        return [
            SourceFileRecord(
                id=row["id"],
                job_id=row["job_id"],
                path=row["path"],
                filename=row["filename"],
                size_bytes=row["size_bytes"],
                modified_time=row["modified_time"],
                duration_seconds=row["duration_seconds"],
            )
            for row in rows
        ]

    def source_file_payloads(self, job_id: int) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT sf.*, c.classification_json
                FROM source_files sf
                LEFT JOIN classifications c ON c.source_file_id = sf.id
                WHERE sf.job_id = ?
                ORDER BY sf.duration_seconds DESC NULLS LAST, sf.filename
                """,
                (job_id,),
            ).fetchall()
        return [{key: row[key] for key in row.keys()} for row in rows]

    def list_job_summaries(self) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT
                    dj.id,
                    dj.disc_title,
                    dj.disc_path,
                    dj.status,
                    COALESCE(jr.review_status, dj.status, 'review_needed') AS review_status,
                    COUNT(sf.id) AS scanned_file_count,
                    SUM(CASE WHEN json_extract(c.classification_json, '$.probable_main_feature') THEN 1 ELSE 0 END) AS main_count,
                    SUM(CASE WHEN json_extract(c.classification_json, '$.probable_extra') THEN 1 ELSE 0 END) AS extra_count,
                    SUM(CASE WHEN json_extract(c.classification_json, '$.needs_subtitle_conversion')
                           OR json_extract(c.classification_json, '$.needs_subtitle_generation')
                           OR json_extract(c.classification_json, '$.image_subtitle_is_default')
                           OR json_extract(c.classification_json, '$.missing_language_tags')
                        THEN 1 ELSE 0 END) AS subtitle_issue_count,
                    SUM(CASE WHEN json_extract(c.classification_json, '$.likely_jellyfin_transcode_risk') THEN 1 ELSE 0 END) AS transcode_risk_count,
                    (
                        SELECT sf2.filename
                        FROM source_files sf2
                        JOIN classifications c2 ON c2.source_file_id = sf2.id
                        WHERE sf2.job_id = dj.id
                        ORDER BY json_extract(c2.classification_json, '$.probable_main_feature') DESC,
                                 sf2.duration_seconds DESC NULLS LAST,
                                 sf2.filename
                        LIMIT 1
                    ) AS likely_main_feature
                FROM disc_jobs dj
                LEFT JOIN source_files sf ON sf.job_id = dj.id
                LEFT JOIN classifications c ON c.source_file_id = sf.id
                LEFT JOIN job_reviews jr ON jr.job_id = dj.id
                GROUP BY dj.id, dj.disc_title, dj.disc_path, dj.status, jr.review_status
                ORDER BY dj.id
                """
            ).fetchall()
        return [{key: row[key] for key in row.keys()} for row in rows]

    def get_job_review(self, job_id: int) -> JobReviewMetadata:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM job_reviews WHERE job_id = ?", (job_id,)).fetchone()
            job = conn.execute("SELECT disc_title, status FROM disc_jobs WHERE id = ?", (job_id,)).fetchone()
        if row is None:
            return JobReviewMetadata(
                job_id=job_id,
                title=job["disc_title"] if job else "",
                review_status=job["status"] if job and job["status"] in {"review_needed", "review_in_progress", "reviewed", "ready_for_fileflows", "fileflows_work_orders_created", "manual_review"} else "review_needed",
            )
        return JobReviewMetadata(
            job_id=row["job_id"],
            title=row["title"],
            original_title=row["original_title"],
            romanized_title=row["romanized_title"] if "romanized_title" in row.keys() else None,
            translated_title=row["translated_title"] if "translated_title" in row.keys() else None,
            language_script_hints=row["language_script_hints"] if "language_script_hints" in row.keys() else None,
            anime_flag=bool(row["anime_flag"]) if "anime_flag" in row.keys() else False,
            japanese_media_flag=bool(row["japanese_media_flag"]) if "japanese_media_flag" in row.keys() else False,
            confidence=row["confidence"] if "confidence" in row.keys() else None,
            manual_review_notes=row["manual_review_notes"] if "manual_review_notes" in row.keys() else None,
            year=row["year"],
            content_type=row["content_type"],
            library_root=row["library_root"],
            imdb_id=row["imdb_id"],
            tmdb_id=row["tmdb_id"],
            tvdb_id=row["tvdb_id"],
            anidb_id=row["anidb_id"],
            anilist_id=row["anilist_id"],
            mal_id=row["mal_id"],
            notes=row["notes"],
            review_status=row["review_status"],
            work_order_folder=row["work_order_folder"],
            work_order_created_at=row["work_order_created_at"],
            warnings=json.loads(row["warnings_json"] or "[]"),
            conflicts=json.loads(row["conflicts_json"] or "[]"),
        )

    def save_job_review(self, review: JobReviewMetadata) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO job_reviews (
                    job_id, title, original_title, romanized_title, translated_title, language_script_hints,
                    anime_flag, japanese_media_flag, confidence, manual_review_notes, year, content_type, library_root,
                    imdb_id, tmdb_id, tvdb_id, anidb_id, anilist_id, mal_id, notes,
                    review_status, work_order_folder, work_order_created_at, warnings_json, conflicts_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(job_id) DO UPDATE SET
                    title=excluded.title,
                    original_title=excluded.original_title,
                    romanized_title=excluded.romanized_title,
                    translated_title=excluded.translated_title,
                    language_script_hints=excluded.language_script_hints,
                    anime_flag=excluded.anime_flag,
                    japanese_media_flag=excluded.japanese_media_flag,
                    confidence=excluded.confidence,
                    manual_review_notes=excluded.manual_review_notes,
                    year=excluded.year,
                    content_type=excluded.content_type,
                    library_root=excluded.library_root,
                    imdb_id=excluded.imdb_id,
                    tmdb_id=excluded.tmdb_id,
                    tvdb_id=excluded.tvdb_id,
                    anidb_id=excluded.anidb_id,
                    anilist_id=excluded.anilist_id,
                    mal_id=excluded.mal_id,
                    notes=excluded.notes,
                    review_status=excluded.review_status,
                    work_order_folder=excluded.work_order_folder,
                    work_order_created_at=excluded.work_order_created_at,
                    warnings_json=excluded.warnings_json,
                    conflicts_json=excluded.conflicts_json,
                    updated_at=CURRENT_TIMESTAMP
                """,
                (
                    review.job_id,
                    review.title,
                    review.original_title,
                    review.romanized_title,
                    review.translated_title,
                    review.language_script_hints,
                    int(review.anime_flag),
                    int(review.japanese_media_flag),
                    review.confidence,
                    review.manual_review_notes,
                    review.year,
                    review.content_type,
                    review.library_root,
                    review.imdb_id,
                    review.tmdb_id,
                    review.tvdb_id,
                    review.anidb_id,
                    review.anilist_id,
                    review.mal_id,
                    review.notes,
                    review.review_status,
                    review.work_order_folder,
                    review.work_order_created_at,
                    json.dumps(review.warnings, ensure_ascii=False),
                    json.dumps(review.conflicts, ensure_ascii=False),
                ),
            )
            conn.execute(
                "UPDATE disc_jobs SET status = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (review.review_status, review.job_id),
            )

    def list_file_reviews(self, job_id: int) -> list[FileReviewDecision]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT sf.id AS source_file_id, fr.*
                FROM source_files sf
                LEFT JOIN file_reviews fr ON fr.source_file_id = sf.id
                WHERE sf.job_id = ?
                ORDER BY sf.duration_seconds DESC NULLS LAST, sf.filename
                """,
                (job_id,),
            ).fetchall()
        reviews: list[FileReviewDecision] = []
        for row in rows:
            if row["role"] is None:
                reviews.append(FileReviewDecision(source_file_id=row["source_file_id"]))
                continue
            reviews.append(
                FileReviewDecision(
                    source_file_id=row["source_file_id"],
                    include_in_work_order=bool(row["include_in_work_order"]),
                    role=row["role"],
                    content_type=row["content_type"],
                    final_display_name=row["final_display_name"],
                    final_filename=row["final_filename"],
                    original_title=row["original_title"],
                    translated_title=row["translated_title"],
                    romanized_title=row["romanized_title"],
                    imdb_id=row["imdb_id"],
                    tmdb_id=row["tmdb_id"],
                    tvdb_id=row["tvdb_id"],
                    anidb_id=row["anidb_id"],
                    anilist_id=row["anilist_id"],
                    mal_id=row["mal_id"],
                    extra_type=row["extra_type"],
                    season_number=row["season_number"],
                    episode_number=row["episode_number"],
                    sort_order=row["sort_order"],
                    encoding_profile=row["encoding_profile"],
                    subtitle_policy=row["subtitle_policy"],
                    generated_final_path=row["generated_final_path"],
                    notes=row["notes"],
                    warnings=json.loads(row["warnings_json"] or "[]"),
                    conflicts=json.loads(row["conflicts_json"] or "[]"),
                )
            )
        return reviews

    def save_file_review(self, review: FileReviewDecision) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO file_reviews (
                    source_file_id, include_in_work_order, role, content_type, final_display_name, final_filename,
                    original_title, translated_title, romanized_title, imdb_id, tmdb_id, tvdb_id, anidb_id,
                    anilist_id, mal_id, extra_type, season_number, episode_number, sort_order, encoding_profile,
                    subtitle_policy, generated_final_path, notes, warnings_json, conflicts_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(source_file_id) DO UPDATE SET
                    include_in_work_order=excluded.include_in_work_order,
                    role=excluded.role,
                    content_type=excluded.content_type,
                    final_display_name=excluded.final_display_name,
                    final_filename=excluded.final_filename,
                    original_title=excluded.original_title,
                    translated_title=excluded.translated_title,
                    romanized_title=excluded.romanized_title,
                    imdb_id=excluded.imdb_id,
                    tmdb_id=excluded.tmdb_id,
                    tvdb_id=excluded.tvdb_id,
                    anidb_id=excluded.anidb_id,
                    anilist_id=excluded.anilist_id,
                    mal_id=excluded.mal_id,
                    extra_type=excluded.extra_type,
                    season_number=excluded.season_number,
                    episode_number=excluded.episode_number,
                    sort_order=excluded.sort_order,
                    encoding_profile=excluded.encoding_profile,
                    subtitle_policy=excluded.subtitle_policy,
                    generated_final_path=excluded.generated_final_path,
                    notes=excluded.notes,
                    warnings_json=excluded.warnings_json,
                    conflicts_json=excluded.conflicts_json,
                    updated_at=CURRENT_TIMESTAMP
                """,
                (
                    review.source_file_id,
                    int(review.include_in_work_order),
                    review.role,
                    review.content_type,
                    review.final_display_name,
                    review.final_filename,
                    review.original_title,
                    review.translated_title,
                    review.romanized_title,
                    review.imdb_id,
                    review.tmdb_id,
                    review.tvdb_id,
                    review.anidb_id,
                    review.anilist_id,
                    review.mal_id,
                    review.extra_type,
                    review.season_number,
                    review.episode_number,
                    review.sort_order,
                    review.encoding_profile,
                    review.subtitle_policy,
                    review.generated_final_path,
                    review.notes,
                    json.dumps(review.warnings, ensure_ascii=False),
                    json.dumps(review.conflicts, ensure_ascii=False),
                ),
            )
            conn.execute(
                """
                INSERT INTO review_decisions (source_file_id, decision_json, approved)
                VALUES (?, ?, ?)
                ON CONFLICT(source_file_id) DO UPDATE SET
                    decision_json=excluded.decision_json,
                    approved=excluded.approved,
                    updated_at=CURRENT_TIMESTAMP
                """,
                (review.source_file_id, json.dumps(asdict(review), ensure_ascii=False), int(review.include_in_work_order)),
            )

    def mark_work_orders_created(self, job_id: int, folder: str, created_at: str) -> None:
        review = self.get_job_review(job_id)
        review.review_status = "fileflows_work_orders_created"
        review.work_order_folder = folder
        review.work_order_created_at = created_at
        self.save_job_review(review)

    def save_work_order_record(
        self,
        job_id: int,
        source_file_id: int,
        work_order_path: str,
        payload: dict,
        status: str = "created",
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO work_orders (job_id, source_file_id, work_order_path, status, payload_json)
                VALUES (?, ?, ?, ?, ?)
                """,
                (job_id, source_file_id, work_order_path, status, json.dumps(payload, ensure_ascii=False)),
            )

    def list_work_order_payloads(self, job_id: int) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT id, source_file_id, work_order_path, status, payload_json
                FROM work_orders
                WHERE job_id = ?
                ORDER BY id
                """,
                (job_id,),
            ).fetchall()
        payloads = []
        for row in rows:
            payload = json.loads(row["payload_json"])
            payload["_work_order_record_id"] = row["id"]
            payload["_source_file_id"] = row["source_file_id"]
            payload["_work_order_path"] = row["work_order_path"]
            payload["_status"] = row["status"]
            payloads.append(payload)
        return payloads

    def save_validation_summary(self, job_id: int, summary: dict, passed: bool) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO validation_results (job_id, result_json, passed) VALUES (?, ?, ?)",
                (job_id, json.dumps(summary, ensure_ascii=False), int(passed)),
            )
            for item in summary.get("items", []):
                conn.execute(
                    """
                    INSERT INTO output_files (
                        job_id, source_file_id, expected_output_name, matched_output_path,
                        final_library_path, status, ffprobe_summary_json, detected_streams_json,
                        warnings_json, errors_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        job_id,
                        item["source_file_id"],
                        item.get("expected_output_name") or "",
                        item.get("matched_output_path"),
                        item.get("expected_final_path") or "",
                        item.get("status") or "",
                        json.dumps(item.get("ffprobe_summary") or {}, ensure_ascii=False),
                        json.dumps(item.get("detected_streams") or {}, ensure_ascii=False),
                        json.dumps(item.get("warnings") or [], ensure_ascii=False),
                        json.dumps(item.get("errors") or [], ensure_ascii=False),
                    ),
                )
                for warning in item.get("warnings") or []:
                    conn.execute(
                        "INSERT INTO validation_warnings (job_id, source_file_id, severity, message) VALUES (?, ?, ?, ?)",
                        (job_id, item["source_file_id"], "warning", warning),
                    )
                for error in item.get("errors") or []:
                    conn.execute(
                        "INSERT INTO validation_warnings (job_id, source_file_id, severity, message) VALUES (?, ?, ?, ?)",
                        (job_id, item["source_file_id"], "error", error),
                    )

    def latest_validation_summary(self, job_id: int) -> dict | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT result_json FROM validation_results WHERE job_id = ? ORDER BY id DESC LIMIT 1",
                (job_id,),
            ).fetchone()
        return json.loads(row["result_json"]) if row else None

    def save_transfer_summary(self, job_id: int, summary: dict) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO transfer_results (job_id, result_json) VALUES (?, ?)",
                (job_id, json.dumps(summary, ensure_ascii=False)),
            )
            conn.execute(
                "INSERT INTO transfer_attempts (job_id, status, result_json) VALUES (?, ?, ?)",
                (job_id, summary.get("status", ""), json.dumps(summary, ensure_ascii=False)),
            )
            for item in summary.get("items", []):
                conn.execute(
                    """
                    INSERT INTO transfer_items (
                        job_id, source_file_id, source_output_path, incoming_path,
                        final_path, status, conflict, error
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        job_id,
                        item["source_file_id"],
                        item.get("source_output_path") or "",
                        item.get("incoming_path") or "",
                        item.get("final_path") or "",
                        item.get("status") or "",
                        item.get("conflict"),
                        item.get("error"),
                    ),
                )
                if item.get("conflict"):
                    conn.execute(
                        "INSERT INTO conflict_records (job_id, source_file_id, path, reason) VALUES (?, ?, ?, ?)",
                        (job_id, item["source_file_id"], item.get("final_path"), item["conflict"]),
                    )

    def latest_transfer_summary(self, job_id: int) -> dict | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT result_json FROM transfer_results WHERE job_id = ? ORDER BY id DESC LIMIT 1",
                (job_id,),
            ).fetchone()
        return json.loads(row["result_json"]) if row else None

    def save_jellyfin_refresh(self, job_id: int, status: str, response: dict) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO jellyfin_refresh_attempts (job_id, status, response_json) VALUES (?, ?, ?)",
                (job_id, status, json.dumps(response, ensure_ascii=False)),
            )

    def save_manual_override(self, job_id: int, source_file_id: int, override_type: str, note: str) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO manual_overrides (job_id, source_file_id, override_type, note) VALUES (?, ?, ?, ?)",
                (job_id, source_file_id, override_type, note),
            )

    def latest_jellyfin_refresh(self, job_id: int) -> dict | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT status, response_json, created_at FROM jellyfin_refresh_attempts WHERE job_id = ? ORDER BY id DESC LIMIT 1",
                (job_id,),
            ).fetchone()
        if row is None:
            return None
        data = json.loads(row["response_json"])
        data.setdefault("status", row["status"])
        data.setdefault("created_at", row["created_at"])
        return data

    def list_audit_events(self, job_id: int) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT id, event_type, message, payload_json, created_at FROM audit_log WHERE job_id = ? ORDER BY id",
                (job_id,),
            ).fetchall()
        return [
            {
                "id": row["id"],
                "event_type": row["event_type"],
                "message": row["message"],
                "payload": json.loads(row["payload_json"] or "{}"),
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def audit(self, event_type: str, message: str, job_id: int | None = None, payload: dict | None = None) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO audit_log (job_id, event_type, message, payload_json) VALUES (?, ?, ?, ?)",
                (job_id, event_type, message, json.dumps(payload or {}, ensure_ascii=False)),
            )

    def save_subtitle_plan(self, source_file_id: int, plan: dict) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO subtitle_plans (source_file_id, plan_json)
                VALUES (?, ?)
                ON CONFLICT(source_file_id) DO UPDATE SET
                    plan_json=excluded.plan_json,
                    updated_at=CURRENT_TIMESTAMP
                """,
                (source_file_id, json.dumps(plan, ensure_ascii=False)),
            )

    def get_subtitle_plan(self, source_file_id: int) -> dict | None:
        with self.connect() as conn:
            row = conn.execute("SELECT plan_json FROM subtitle_plans WHERE source_file_id = ?", (source_file_id,)).fetchone()
        return json.loads(row["plan_json"]) if row else None

    def save_subtitle_validation_result(self, job_id: int, source_file_id: int, result: dict) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO subtitle_validation_results (job_id, source_file_id, result_json) VALUES (?, ?, ?)",
                (job_id, source_file_id, json.dumps(result, ensure_ascii=False)),
            )

    def save_metadata_candidate(self, job_id: int, provider: str, candidate: dict, source_file_id: int | None = None) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO metadata_candidates (job_id, source_file_id, provider, candidate_json) VALUES (?, ?, ?, ?)",
                (job_id, source_file_id, provider, json.dumps(candidate, ensure_ascii=False)),
            )

    def save_llm_request_response(self, job_id: int, provider: str, request: dict, response: dict) -> None:
        redacted_request = _redact_secrets(request)
        redacted_response = _redact_secrets(response)
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO llm_suggestion_requests (job_id, provider, request_json, response_json) VALUES (?, ?, ?, ?)",
                (job_id, provider, json.dumps(redacted_request, ensure_ascii=False), json.dumps(redacted_response, ensure_ascii=False)),
            )
            for suggestion in redacted_response.get("suggestions", []) or []:
                conn.execute(
                    """
                    INSERT INTO llm_suggestions (job_id, source_file_id, suggestion_type, suggestion_json, status)
                    VALUES (?, ?, ?, ?, 'suggested')
                    """,
                    (
                        job_id,
                        suggestion.get("source_file_id"),
                        suggestion.get("type", "unknown"),
                        json.dumps(suggestion, ensure_ascii=False),
                    ),
                )

    def list_llm_suggestions(self, job_id: int) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT id, source_file_id, suggestion_type, suggestion_json, status, created_at
                FROM llm_suggestions
                WHERE job_id = ?
                ORDER BY id
                """,
                (job_id,),
            ).fetchall()
        suggestions = []
        for row in rows:
            payload = json.loads(row["suggestion_json"])
            payload.update(
                {
                    "id": row["id"],
                    "source_file_id": row["source_file_id"],
                    "type": row["suggestion_type"],
                    "status": row["status"],
                    "created_at": row["created_at"],
                }
            )
            suggestions.append(payload)
        return suggestions

    def set_cleanup_hold(self, job_id: int, hold: bool, reason: str | None = None) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO cleanup_holds (job_id, hold, reason)
                VALUES (?, ?, ?)
                ON CONFLICT(job_id) DO UPDATE SET
                    hold=excluded.hold,
                    reason=excluded.reason,
                    updated_at=CURRENT_TIMESTAMP
                """,
                (job_id, int(hold), reason),
            )
        self.audit("cleanup_hold_set" if hold else "cleanup_hold_removed", reason or "", job_id)

    def has_cleanup_hold(self, job_id: int) -> bool:
        with self.connect() as conn:
            row = conn.execute("SELECT hold FROM cleanup_holds WHERE job_id = ?", (job_id,)).fetchone()
        return bool(row and row["hold"])

    def replace_cleanup_eligibility(self, rows: list[dict]) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM cleanup_eligibility")
            for row in rows:
                conn.execute(
                    """
                    INSERT INTO cleanup_eligibility (job_id, path, item_type, eligible, reason, archive_path)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        row["job_id"],
                        row["path"],
                        row["item_type"],
                        int(row["eligible"]),
                        row["reason"],
                        row.get("archive_path"),
                    ),
                )

    def list_cleanup_eligibility(self, job_id: int | None = None) -> list[dict]:
        query = "SELECT * FROM cleanup_eligibility"
        params: tuple = ()
        if job_id is not None:
            query += " WHERE job_id = ?"
            params = (job_id,)
        query += " ORDER BY id"
        with self.connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [{key: row[key] for key in row.keys()} for row in rows]

    def save_cleanup_attempt(self, status: str, result: dict, job_id: int | None = None) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO cleanup_attempts (job_id, status, result_json) VALUES (?, ?, ?)",
                (job_id, status, json.dumps(result, ensure_ascii=False)),
            )

    def save_archive_result(self, job_id: int, source_path: str, archive_path: str, status: str) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO archive_results (job_id, source_path, archive_path, status) VALUES (?, ?, ?, ?)",
                (job_id, source_path, archive_path, status),
            )

    def cache_status_summary(self, summary: dict) -> None:
        with self.connect() as conn:
            conn.execute("INSERT INTO status_dashboard_cache (summary_json) VALUES (?)", (json.dumps(summary, ensure_ascii=False),))


def _redact_secrets(value):
    if isinstance(value, dict):
        return {
            key: "***REDACTED***" if any(marker in key.lower() for marker in ["api_key", "secret", "token", "password"]) else _redact_secrets(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_secrets(item) for item in value]
    return value
