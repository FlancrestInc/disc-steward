from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from .models import Classification, Job, ScannedFile, SourceFileRecord


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
                CREATE TABLE IF NOT EXISTS audit_log (
                    id INTEGER PRIMARY KEY,
                    job_id INTEGER,
                    event_type TEXT NOT NULL,
                    message TEXT NOT NULL,
                    payload_json TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                """
            )

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

    def audit(self, event_type: str, message: str, job_id: int | None = None, payload: dict | None = None) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO audit_log (job_id, event_type, message, payload_json) VALUES (?, ?, ?, ?)",
                (job_id, event_type, message, json.dumps(payload or {}, ensure_ascii=False)),
            )
