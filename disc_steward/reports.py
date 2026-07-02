from __future__ import annotations

import html
import json
from pathlib import Path

from .config import AppConfig
from .db import Database
from .status import build_status_summary, format_status_summary


def generate_reports(db: Database, config: AppConfig) -> list[Path]:
    config.review_needed_path.mkdir(parents=True, exist_ok=True)
    reports: list[Path] = []
    dashboard = config.review_needed_path / "dashboard.html"
    dashboard.write_text(render_dashboard_report(db, config), encoding="utf-8")
    reports.append(dashboard)
    for job in db.list_jobs():
        output = config.review_needed_path / f"job_{job.id}_report.html"
        output.write_text(render_job_report(db, job.id), encoding="utf-8")
        reports.append(output)
    return reports


def render_dashboard_report(db: Database, config: AppConfig) -> str:
    summary = build_status_summary(db, config)
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Disc Steward Dashboard</title>
  <style>
    body {{ font-family: system-ui, sans-serif; margin: 2rem; line-height: 1.45; color: #17202a; }}
    pre {{ background: #f6f8fa; border: 1px solid #d8dee4; padding: 1rem; white-space: pre-wrap; }}
  </style>
</head>
<body>
  <h1>Disc Steward Dashboard</h1>
  <pre>{html.escape(format_status_summary(summary))}</pre>
</body>
</html>
"""


def render_job_report(db: Database, job_id: int) -> str:
    job = next(job for job in db.list_jobs() if job.id == job_id)
    rows = db.source_file_payloads(job_id)
    file_sections = "\n".join(_render_file(row) for row in rows)
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Disc Steward Job {job.id}</title>
  <style>
    body {{ font-family: system-ui, sans-serif; margin: 2rem; line-height: 1.45; color: #17202a; }}
    table {{ border-collapse: collapse; width: 100%; }}
    th, td {{ border-bottom: 1px solid #d8dee4; padding: .45rem; text-align: left; vertical-align: top; }}
    .warn {{ color: #9a3412; font-weight: 600; }}
    .ok {{ color: #166534; font-weight: 600; }}
    code {{ white-space: pre-wrap; }}
  </style>
</head>
<body>
  <h1>{html.escape(job.disc_title)}</h1>
  <p>Status: <strong>{html.escape(job.status)}</strong><br>Path: <code>{html.escape(job.disc_path)}</code></p>
  <h2>Files</h2>
  <table>
    <thead><tr><th>File</th><th>Duration</th><th>Likely Role</th><th>Risks</th><th>Reasons</th></tr></thead>
    <tbody>{file_sections}</tbody>
  </table>
  <h2>Suggested Next Action</h2>
  <p>Review main feature and extras, add metadata IDs, then run the ffmpeg processing step. Cleanup and transfer actions remain gated by validation, configuration, and audit logging.</p>
</body>
</html>
"""


def _render_file(row: dict) -> str:
    classification = json.loads(row.get("classification_json") or "{}")
    role = _role_label(classification)
    risks = [key for key, value in classification.items() if key.startswith("needs_") and value]
    if classification.get("likely_jellyfin_transcode_risk"):
        risks.append("transcode risk")
    duration = row["duration_seconds"] or 0
    return (
        "<tr>"
        f"<td><code>{html.escape(row['filename'])}</code></td>"
        f"<td>{duration / 60:.1f} min</td>"
        f"<td>{html.escape(role)}</td>"
        f"<td>{html.escape(', '.join(risks) or 'none detected')}</td>"
        f"<td>{html.escape('; '.join(classification.get('reasons', [])))}</td>"
        "</tr>"
    )


def _role_label(classification: dict) -> str:
    for key, label in [
        ("probable_main_feature", "main feature candidate"),
        ("probable_trailer", "trailer / promo candidate"),
        ("probable_featurette", "featurette / extra candidate"),
        ("probable_deleted_scene", "deleted scene candidate"),
        ("probable_menu_or_bumper", "menu / bumper candidate"),
    ]:
        if classification.get(key):
            return label
    return "manual review"
