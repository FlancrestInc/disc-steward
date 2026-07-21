from __future__ import annotations

import shutil
import time
from dataclasses import asdict
from pathlib import Path

from .config import AppConfig
from .models import CleanupEligibilityItem, CleanupPlanSummary


FINAL_SUCCESS_STATUSES = {"imported_to_jellyfin"}


def plan_cleanup(db, config: AppConfig, job_id: int | None = None) -> CleanupPlanSummary:
    summary = CleanupPlanSummary(dry_run=config.cleanup.dry_run)
    jobs = db.list_jobs()
    if config.cleanup.delete_raw_rip_folders:
        _plan_raw_rip_folders(summary, db, config, jobs, job_id)
    for job in jobs:
        if job_id is not None and job.id != job_id:
            continue
        validation = db.latest_validation_summary(job.id)
        transfer = db.latest_transfer_summary(job.id)
        hold = db.has_cleanup_hold(job.id)
        final_success = _final_success(config, job.status, validation, transfer)
        if not config.cleanup.delete_raw_rip_folders:
            for row in db.source_file_payloads(job.id):
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
                    config,
                )
        if not config.cleanup.delete_raw_rip_folders:
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
                    config,
                )
    db.replace_cleanup_eligibility([asdict(item) for item in [*summary.eligible, *summary.ineligible]])
    return summary


def execute_cleanup(db, config: AppConfig, job_id: int | None = None) -> CleanupPlanSummary:
    summary = plan_cleanup(db, config, job_id=job_id)
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
                    if item.item_type == "raw_rip_folder":
                        shutil.copytree(path, archive_path, dirs_exist_ok=True)
                        _verify_archive_tree(path, archive_path)
                    else:
                        archive_path.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(path, archive_path)
                        if archive_path.stat().st_size != path.stat().st_size:
                            raise IOError("archive verification failed: size mismatch")
                    db.save_archive_result(item.job_id, str(path), str(archive_path), "verified")
                summary.archived.append(str(archive_path))
            should_delete = (
                item.item_type == "working_file"
                or config.cleanup.delete_raw_rips
                or (item.item_type == "raw_rip_folder" and config.cleanup.delete_raw_rip_folders)
            )
            if should_delete:
                if not config.cleanup.dry_run:
                    if item.item_type == "raw_rip_folder":
                        shutil.rmtree(path)
                    else:
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


def _plan_raw_rip_folders(summary: CleanupPlanSummary, db, config: AppConfig, jobs, job_id: int | None) -> None:
    groups: dict[Path, list] = {}
    invalid: list[tuple[object, Path, str]] = []
    for job in jobs:
        raw_folder = Path(job.source_disc_path or job.disc_path)
        target, reason = _canonical_raw_rip_folder(config, raw_folder)
        if reason:
            if job_id is None or job.id == job_id:
                invalid.append((job, raw_folder, reason))
            continue
        groups.setdefault(target, []).append(job)
    for job, raw_folder, reason in invalid:
        _add_folder_item(summary, job.id, raw_folder, False, reason, None)
    for folder, members in groups.items():
        selected = [member for member in members if job_id is None or member.id == job_id]
        if not selected:
            continue
        representative = selected[0] if job_id is not None else members[0]
        eligible, reason = _folder_eligibility(db, config, folder, members)
        _add_folder_item(
            summary,
            representative.id,
            folder,
            eligible,
            reason,
            _archive_path(config, folder) if config.cleanup.archive_raw_rips_to_eddy else None,
        )


def _add_folder_item(
    summary: CleanupPlanSummary,
    job_id: int,
    folder: Path,
    eligible: bool,
    reason: str,
    archive_path: Path | None,
) -> None:
    item = CleanupEligibilityItem(
        job_id=job_id,
        path=str(folder),
        item_type="raw_rip_folder",
        eligible=eligible,
        reason=reason,
        archive_path=str(archive_path) if archive_path else None,
    )
    (summary.eligible if eligible else summary.ineligible).append(item)


def _canonical_raw_rip_folder(config: AppConfig, raw_folder: Path) -> tuple[Path | None, str | None]:
    root = config.raw_rip_path.resolve(strict=False)
    lexical_folder = raw_folder.absolute()
    try:
        relative_folder = lexical_folder.relative_to(root)
    except ValueError:
        return None, "raw rip folder resolves outside raw rip root"
    current = root
    for component in relative_folder.parts:
        current = current / component
        if current.is_symlink():
            return None, "raw rip folder path contains a symlink"
    target = lexical_folder.resolve(strict=False)
    if target == root:
        return None, "raw rip root cannot be cleaned"
    try:
        target.relative_to(root)
    except ValueError:
        return None, "raw rip folder resolves outside raw rip root"
    return target, None


def _folder_eligibility(db, config: AppConfig, folder: Path, members) -> tuple[bool, str]:
    if config.cleanup.archive_raw_rips_to_eddy and not config.cleanup.raw_rip_archive_path:
        return False, "raw rip archive destination is required for folder cleanup"
    for job in members:
        validation = db.latest_validation_summary(job.id)
        transfer = db.latest_transfer_summary(job.id)
        if db.has_cleanup_hold(job.id):
            return False, "shared source folder has a cleanup hold"
        if not _final_success(config, job.status, validation, transfer, require_verified_transfer=True):
            return False, "shared source folder has a job without verified transfer, final import, validation, or final paths"
    preflight_eligible, preflight_reason = _eligibility(config, folder, True, True, False, 0)
    if not preflight_eligible:
        return False, preflight_reason
    return _eligibility(
        config,
        folder,
        True,
        True,
        False,
        config.cleanup.raw_rip_retention_days_after_import,
        _newest_tree_mtime(folder),
    )


def _verify_archive_tree(source: Path, archive: Path) -> None:
    for source_file in source.rglob("*"):
        if not source_file.is_file():
            continue
        archived_file = archive / source_file.relative_to(source)
        if not archived_file.is_file() or archived_file.stat().st_size != source_file.stat().st_size:
            raise IOError(f"archive verification failed: {source_file}")


def _newest_tree_mtime(folder: Path) -> float:
    newest = folder.stat().st_mtime
    for child in folder.rglob("*"):
        try:
            newest = max(newest, child.stat().st_mtime)
        except FileNotFoundError:
            continue
    return newest


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
    config: AppConfig,
) -> None:
    eligible, reason = _eligibility(config, path, action_configured, final_success, hold, retention_days)
    item = CleanupEligibilityItem(
        job_id=job_id,
        path=str(path),
        item_type=item_type,
        eligible=eligible,
        reason=reason,
        archive_path=str(archive_path) if archive_path else None,
    )
    (summary.eligible if eligible else summary.ineligible).append(item)


def _eligibility(
    config: AppConfig,
    path: Path,
    action_configured: bool,
    final_success: bool,
    hold: bool,
    retention_days: int,
    modified_time: float | None = None,
) -> tuple[bool, str]:
    if hold:
        return False, "job is on cleanup hold"
    if not final_success:
        return False, "job has not completed final import, validation, and transfer"
    if not action_configured:
        return False, "cleanup action is not enabled for this item type"
    unavailable = config.mount_unavailable_for(path)
    if unavailable is not None:
        return False, f"mount unavailable: {unavailable}"
    if not path.exists():
        return False, "path does not exist"
    age_days = (time.time() - (modified_time if modified_time is not None else path.stat().st_mtime)) / 86400
    if age_days < retention_days:
        return False, f"retention period has not elapsed ({age_days:.1f}/{retention_days} days)"
    return True, "validated, transferred, final path exists, and retention elapsed"


def _final_success(
    config: AppConfig,
    job_status: str,
    validation: dict | None,
    transfer: dict | None,
    require_verified_transfer: bool = False,
) -> bool:
    if job_status not in FINAL_SUCCESS_STATUSES:
        return False
    if not validation or validation.get("passed") is not True:
        return False
    if not transfer or transfer.get("status") != "imported_to_jellyfin":
        return False
    items = transfer.get("items", [])
    if require_verified_transfer and (
        config.transfer_verify not in {"size", "sha256"}
        or not items
        or any(item.get("verification") not in {"size", "sha256"} for item in items)
    ):
        return False
    for item in items:
        final_path = item.get("final_path")
        if not final_path:
            return False
        controller_final_path = config.to_controller_path(Path(final_path), "eddy")
        unavailable = config.mount_unavailable_for(controller_final_path)
        if unavailable is not None or not controller_final_path.exists():
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
