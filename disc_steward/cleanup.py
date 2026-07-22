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
    seen_working_paths: set[Path] = set()
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
        for item in (validation or {}).get("items", []):
            matched = item.get("matched_output_path")
            if not matched:
                continue
            working_path, safety_reason = _canonical_working_file(config, Path(matched))
            if working_path is None:
                _add_item(summary, job.id, Path(matched), "working_file", False, safety_reason or "unsafe working path")
                continue
            if working_path in seen_working_paths:
                continue
            seen_working_paths.add(working_path)
            _add_candidate(
                summary, job.id, working_path, "working_file", config.cleanup.delete_working_files,
                _final_success(config, job.status, validation, transfer, require_verified_transfer=True), hold,
                config.cleanup.working_file_retention_days_after_import, None, config,
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
            safe_path, reason = _execution_safe_path(db, config, item)
            if safe_path is None:
                summary.errors.append(f"{path}: {reason}")
                continue
            path = safe_path
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


def _add_item(summary: CleanupPlanSummary, job_id: int, path: Path, item_type: str, eligible: bool, reason: str) -> None:
    item = CleanupEligibilityItem(job_id=job_id, path=str(path), item_type=item_type, eligible=eligible, reason=reason)
    (summary.eligible if eligible else summary.ineligible).append(item)


def _canonical_raw_rip_folder(config: AppConfig, raw_folder: Path) -> tuple[Path | None, str | None]:
    target, reason = _canonical_child(raw_folder, [config.raw_rip_path], "raw rip")
    if reason == "raw rip path contains a symlink":
        return None, "raw rip folder path contains a symlink"
    if reason == "raw rip resolves outside configured root":
        return None, "raw rip folder resolves outside raw rip root"
    return target, reason


def _canonical_working_file(config: AppConfig, path: Path) -> tuple[Path | None, str | None]:
    return _canonical_child(path, [config.fileflows_working_path, config.validation_needed_path], "working file")


def _canonical_child(path: Path, roots: list[Path], label: str) -> tuple[Path | None, str | None]:
    lexical_folder = path.absolute()
    for configured_root in roots:
        root = configured_root.absolute()
        try:
            relative_folder = lexical_folder.relative_to(root)
        except ValueError:
            continue
        if not relative_folder.parts:
            return None, f"{label} root cannot be cleaned"
        current = root
        if current.is_symlink():
            return None, f"{label} path contains a symlink"
        for component in relative_folder.parts:
            current = current / component
            if current.is_symlink():
                return None, f"{label} path contains a symlink"
        resolved_root = root.resolve(strict=False)
        target = lexical_folder.resolve(strict=False)
        try:
            target.relative_to(resolved_root)
        except ValueError:
            return None, f"{label} resolves outside configured root"
        return target, None
    return None, f"{label} resolves outside configured root"


def _execution_safe_path(db, config: AppConfig, item: CleanupEligibilityItem) -> tuple[Path | None, str | None]:
    job = db.get_job(item.job_id)
    if job is None:
        return None, "job no longer exists"
    if db.has_cleanup_hold(job.id):
        return None, "job is on cleanup hold"
    validation = db.latest_validation_summary(job.id)
    transfer = db.latest_transfer_summary(job.id)
    if not _final_success(config, job.status, validation, transfer, require_verified_transfer=True):
        return None, "job no longer has verified final import"
    if item.item_type == "raw_rip_folder":
        path, reason = _canonical_raw_rip_folder(config, Path(item.path))
        if path is None:
            return None, reason
        members = []
        for candidate in db.list_jobs():
            folder, _ = _canonical_raw_rip_folder(config, Path(candidate.source_disc_path or candidate.disc_path))
            if folder == path:
                members.append(candidate)
        eligible, reason = _folder_eligibility(db, config, path, members)
        return (path, None) if eligible else (None, reason)
    if item.item_type == "working_file":
        path, reason = _canonical_working_file(config, Path(item.path))
        if path is None:
            return None, reason
        eligible, reason = _eligibility(
            config, path, True, True, False, config.cleanup.working_file_retention_days_after_import
        )
        return (path, None) if eligible else (None, reason)
    if item.item_type == "raw_rip":
        path, reason = _canonical_child(Path(item.path), [config.raw_rip_path], "raw rip")
        if path is None:
            return None, reason
        eligible, reason = _eligibility(
            config, path, True, True, False, config.cleanup.raw_rip_retention_days_after_import
        )
        return (path, None) if eligible else (None, reason)
    return None, "unknown cleanup item type"


def cleanup_previews(db, config: AppConfig, job_id: int | None = None) -> CleanupPlanSummary:
    summary = CleanupPlanSummary(dry_run=False)
    jobs = [job for job in db.list_jobs() if job_id is None or job.id == job_id]
    for job in jobs:
        validation = db.latest_validation_summary(job.id)
        transfer = db.latest_transfer_summary(job.id)
        if not _final_success(config, job.status, validation, transfer, require_verified_transfer=True):
            summary.errors.append(f"job {job.id}: job has not completed verified final import")
            continue
        db.cancel_preview_jobs_for_job(job.id)
        for row in db.source_file_payloads(job.id):
            value = row.get("preview_path")
            if not value:
                continue
            # Recheck all destructive conditions immediately before unlink.
            current = db.get_job(job.id)
            current_validation = db.latest_validation_summary(job.id)
            current_transfer = db.latest_transfer_summary(job.id)
            if current is None or db.has_cleanup_hold(job.id) or not _final_success(
                config, current.status, current_validation, current_transfer, require_verified_transfer=True
            ):
                summary.errors.append(f"{value}: job is no longer eligible for preview cleanup")
                continue
            preview_root = Path(config.preview.output_path)
            unavailable = config.mount_unavailable_for(preview_root)
            if unavailable is not None:
                summary.errors.append(f"{value}: mount unavailable: {unavailable}")
                continue
            path, reason = _canonical_child(Path(value), [preview_root], "preview")
            if path is None:
                summary.errors.append(f"{value}: {reason}")
                continue
            try:
                # Resolve again after planning to catch path swaps.
                path, reason = _canonical_child(Path(value), [preview_root], "preview")
                if path is None:
                    summary.errors.append(f"{value}: {reason}")
                    continue
                if path.exists():
                    path.unlink()
                db.clear_preview_metadata(int(row["id"]))
                summary.deleted.append(str(path))
            except Exception as exc:
                summary.errors.append(f"{path}: {exc}")
                db.audit("preview_cleanup_error", str(exc), job.id, {"source_file_id": row["id"], "preview_path": str(path)})
    return summary


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
