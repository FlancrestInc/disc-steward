from __future__ import annotations

import shutil
import time
from dataclasses import asdict
from pathlib import Path

from .config import AppConfig
from .models import CleanupEligibilityItem, CleanupPlanSummary


FINAL_SUCCESS_STATUSES = {"imported_to_jellyfin"}


def plan_cleanup(db, config: AppConfig) -> CleanupPlanSummary:
    summary = CleanupPlanSummary(dry_run=config.cleanup.dry_run)
    for job in db.list_jobs():
        validation = db.latest_validation_summary(job.id)
        transfer = db.latest_transfer_summary(job.id)
        hold = db.has_cleanup_hold(job.id)
        final_success = _final_success(job.status, validation, transfer)
        source_rows = db.source_file_payloads(job.id)
        for row in source_rows:
            raw_path = Path(row["path"])
            _add_candidate(
                summary,
                job.id,
                raw_path,
                "raw_rip",
                config.cleanup.delete_raw_rips or config.cleanup.archive_raw_rips_to_eddy,
                final_success,
                hold,
                config.cleanup.raw_rip_retention_days_after_import,
                _archive_path(config, raw_path) if config.cleanup.archive_raw_rips_to_eddy else None,
            )
        for item in (validation or {}).get("items", []):
            matched = item.get("matched_output_path")
            if not matched:
                continue
            _add_candidate(
                summary,
                job.id,
                Path(matched),
                "working_file",
                config.cleanup.delete_working_files,
                final_success,
                hold,
                config.cleanup.working_file_retention_days_after_import,
                None,
            )
    db.replace_cleanup_eligibility([asdict(item) for item in [*summary.eligible, *summary.ineligible]])
    return summary


def execute_cleanup(db, config: AppConfig) -> CleanupPlanSummary:
    summary = plan_cleanup(db, config)
    if not config.cleanup.enabled:
        summary.errors.append("cleanup.enabled is false; no files were changed")
        db.save_cleanup_attempt("disabled", _summary_dict(summary))
        return summary
    for item in summary.eligible:
        path = Path(item.path)
        if not path.exists():
            summary.errors.append(f"eligible path no longer exists: {path}")
            continue
        try:
            if item.archive_path:
                archive_path = Path(item.archive_path)
                if not config.cleanup.dry_run:
                    archive_path.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(path, archive_path)
                    if archive_path.stat().st_size != path.stat().st_size:
                        raise IOError("archive verification failed: size mismatch")
                    db.save_archive_result(item.job_id, str(path), str(archive_path), "verified")
                summary.archived.append(str(archive_path))
            should_delete = item.item_type == "working_file" or config.cleanup.delete_raw_rips
            if should_delete:
                if not config.cleanup.dry_run:
                    path.unlink()
                    summary.deleted.append(str(path))
                db.audit("cleanup_delete" if not config.cleanup.dry_run else "cleanup_dry_run", f"Cleanup eligible: {path}", item.job_id, asdict(item))
        except Exception as exc:
            summary.errors.append(f"{path}: {exc}")
            db.audit("cleanup_error", str(exc), item.job_id, asdict(item))
    status = "dry_run" if config.cleanup.dry_run else "completed"
    if summary.errors:
        status = "warning"
    db.save_cleanup_attempt(status, _summary_dict(summary))
    return summary


def _add_candidate(
    summary: CleanupPlanSummary,
    job_id: int,
    path: Path,
    item_type: str,
    action_configured: bool,
    final_success: bool,
    hold: bool,
    retention_days: int,
    archive_path: Path | None,
) -> None:
    eligible, reason = _eligibility(path, action_configured, final_success, hold, retention_days)
    item = CleanupEligibilityItem(
        job_id=job_id,
        path=str(path),
        item_type=item_type,
        eligible=eligible,
        reason=reason,
        archive_path=str(archive_path) if archive_path else None,
    )
    (summary.eligible if eligible else summary.ineligible).append(item)


def _eligibility(path: Path, action_configured: bool, final_success: bool, hold: bool, retention_days: int) -> tuple[bool, str]:
    if hold:
        return False, "job is on cleanup hold"
    if not final_success:
        return False, "job has not completed final import, validation, and transfer"
    if not action_configured:
        return False, "cleanup action is not enabled for this item type"
    if not path.exists():
        return False, "path does not exist"
    age_days = (time.time() - path.stat().st_mtime) / 86400
    if age_days < retention_days:
        return False, f"retention period has not elapsed ({age_days:.1f}/{retention_days} days)"
    return True, "validated, transferred, final path exists, and retention elapsed"


def _final_success(job_status: str, validation: dict | None, transfer: dict | None) -> bool:
    if job_status not in FINAL_SUCCESS_STATUSES:
        return False
    if not validation or validation.get("passed") is not True:
        return False
    if not transfer or transfer.get("status") != "imported_to_jellyfin":
        return False
    for item in transfer.get("items", []):
        final_path = item.get("final_path")
        if not final_path or not Path(final_path).exists():
            return False
        if item.get("status") not in {"placed", "imported"}:
            return False
    return True


def _archive_path(config: AppConfig, source_path: Path) -> Path | None:
    if not config.cleanup.raw_rip_archive_path:
        return None
    try:
        relative = source_path.relative_to(config.raw_rip_path)
    except ValueError:
        relative = Path(source_path.parent.name) / source_path.name
    return Path(config.cleanup.raw_rip_archive_path) / relative


def _summary_dict(summary: CleanupPlanSummary) -> dict:
    return {
        "dry_run": summary.dry_run,
        "eligible": [asdict(item) for item in summary.eligible],
        "ineligible": [asdict(item) for item in summary.ineligible],
        "deleted": summary.deleted,
        "archived": summary.archived,
        "errors": summary.errors,
    }
