#!/usr/bin/env python3
"""Bounded, read-only smoke check for Disc Steward evidence plumbing."""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path


def main() -> int:
    database = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("/var/lib/disc-steward/disc_steward.sqlite3")
    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        tables = {
            row["name"]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        required = {"disc_jobs", "source_files", "research_packets", "release_rankings", "verified_provider_ids"}
        missing = sorted(required - tables)
        jobs = {}
        for job_id in (33, 35, 55, 112):
            row = connection.execute(
                "SELECT COUNT(*) AS file_count FROM source_files WHERE job_id = ?",
                (job_id,),
            ).fetchone()
            jobs[str(job_id)] = {"file_count": int(row["file_count"])}
        status = "ok" if not missing else "needs_schema_migration"
        print(json.dumps({"database": str(database), "read_only": True, "status": status, "missing_tables": missing, "required_tables": sorted(required), "jobs": jobs}))
        return 0 if not missing else 2
    finally:
        connection.close()


if __name__ == "__main__":
    raise SystemExit(main())
