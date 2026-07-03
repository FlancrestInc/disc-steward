from __future__ import annotations

import hashlib
import shutil
import subprocess
from dataclasses import asdict
from pathlib import Path
from typing import Callable

from .config import AppConfig
from .jellyfin import refresh_after_import
from .models import TransferConflict, TransferItemResult, TransferSummary


def detect_transfer_conflict(final_path: Path, overwrite: bool = False) -> TransferConflict:
    if final_path.exists() and not overwrite:
        return TransferConflict(True, final_path, "destination already exists")
    return TransferConflict(False, final_path)


def transfer_via_local_mount(source: Path, incoming_dir: Path, final_path: Path, overwrite: bool = False, dry_run: bool = True) -> Path:
    conflict = detect_transfer_conflict(final_path, overwrite)
    if conflict.conflict:
        raise FileExistsError(conflict.reason)
    incoming_dir.mkdir(parents=True, exist_ok=True)
    incoming_path = incoming_dir / f"{final_path.name}.partial"
    verified_path = incoming_dir / final_path.name
    if dry_run:
        return final_path
    if incoming_path.exists() or verified_path.exists():
        raise FileExistsError(f"incoming file already exists for {final_path.name}")
    shutil.copy2(source, incoming_path)
    if incoming_path.stat().st_size != source.stat().st_size:
        raise IOError("transfer verification failed: size mismatch")
    incoming_path.rename(verified_path)
    final_path.parent.mkdir(parents=True, exist_ok=True)
    verified_path.rename(final_path)
    return final_path


def transfer_job_to_eddy(
    db,
    config: AppConfig,
    job_id: int,
    copy_file: Callable[[Path, Path], None] | None = None,
) -> TransferSummary:
    validation = db.latest_validation_summary(job_id)
    if not validation:
        raise ValueError("job has no validation result")
    ready_items = [
        item
        for item in validation.get("items", [])
        if item.get("status") == "passed" or (item.get("manually_accepted") and item.get("manual_acceptance_note"))
    ]
    if len(ready_items) != len(validation.get("items", [])):
        raise ValueError("job validation has not passed and not all failed items are manually accepted")
    db.update_job_status(job_id, "transferring_to_eddy")
    db.audit("transfer_started", f"Transfer started for {len(ready_items)} output(s)", job_id)
    if config.transfer_method == "rsync":
        summary = _transfer_with_rsync(db, config, job_id, ready_items)
    else:
        summary = _transfer_with_local_mount(config, job_id, ready_items, copy_file or shutil.copy2)
    db.save_transfer_summary(job_id, _summary_dict(summary))
    db.update_job_status(job_id, summary.status)
    db.audit(
        "transfer_completed" if summary.status == "imported_to_jellyfin" else summary.status,
        f"Transfer finished with status {summary.status}",
        job_id,
        {"warnings": summary.warnings},
    )
    if summary.status == "imported_to_jellyfin" and config.jellyfin.refresh_after_import:
        jellyfin_result = refresh_after_import(db, job_id, config.jellyfin)
        if jellyfin_result.get("status") == "warning":
            summary.warnings.append(f"Jellyfin refresh warning: {jellyfin_result.get('error')}")
            db.save_transfer_summary(job_id, _summary_dict(summary))
    return summary


def _transfer_with_local_mount(
    config: AppConfig,
    job_id: int,
    items: list[dict],
    copy_file: Callable[[Path, Path], None],
) -> TransferSummary:
    results: list[TransferItemResult] = []
    any_conflict = False
    any_failed = False
    any_dry_run = False
    incoming_job_dir = config.eddy_incoming_path / f"job_{job_id}"
    incoming_external_dir = config.to_eddy_path(incoming_job_dir)
    unavailable = config.mount_unavailable_for(incoming_job_dir)
    if unavailable is not None:
        return TransferSummary(
            job_id=job_id,
            status="failed",
            warnings=[f"mount unavailable: {unavailable}"],
        )
    incoming_job_dir.mkdir(parents=True, exist_ok=True)
    for item in items:
        source = Path(item["matched_output_path"])
        final_external_path = Path(item["expected_final_path"])
        final_path = config.to_controller_path(final_external_path, "eddy")
        incoming_path = incoming_job_dir / final_path.name
        incoming_external_path = incoming_external_dir / final_path.name
        result = TransferItemResult(
            source_file_id=int(item["source_file_id"]),
            source_output_path=str(source),
            incoming_path=str(incoming_external_path),
            final_path=str(final_external_path),
            verification=config.transfer_verify,
        )
        unavailable = config.mount_unavailable_for(final_path)
        if unavailable is not None:
            result.status = "failed"
            result.error = f"mount unavailable: {unavailable}"
            any_failed = True
            results.append(result)
            continue
        conflict = detect_transfer_conflict(final_path, config.overwrite_existing)
        if conflict.conflict:
            result.status = "conflict"
            result.conflict = conflict.reason
            any_conflict = True
            results.append(result)
            continue
        try:
            if config.dry_run:
                result.status = "dry_run"
                any_dry_run = True
                results.append(result)
                continue
            partial_path = incoming_path.with_name(f"{incoming_path.name}.partial")
            if partial_path.exists() or incoming_path.exists():
                raise FileExistsError(f"incoming file already exists: {incoming_path}")
            copy_file(source, partial_path)
            _verify_paths(source, partial_path, config.transfer_verify)
            partial_path.rename(incoming_path)
            if config.create_final_directories:
                final_path.parent.mkdir(parents=True, exist_ok=True)
            if not final_path.parent.exists():
                raise FileNotFoundError(f"final parent directory does not exist: {final_path.parent}")
            incoming_path.replace(final_path) if config.overwrite_existing else incoming_path.rename(final_path)
            _verify_paths(source, final_path, config.transfer_verify)
            subtitle_paths: list[str] = []
            for subtitle in item.get("subtitle_outputs", []) or []:
                subtitle_name = subtitle.get("output_name")
                if not subtitle_name:
                    continue
                subtitle_source = source.with_name(subtitle_name)
                subtitle_final = final_path.parent / subtitle_name
                subtitle_partial = subtitle_final.with_name(f"{subtitle_final.name}.partial")
                copy_file(subtitle_source, subtitle_partial)
                _verify_paths(subtitle_source, subtitle_partial, config.transfer_verify)
                subtitle_partial.rename(subtitle_final)
                _verify_paths(subtitle_source, subtitle_final, config.transfer_verify)
                subtitle_paths.append(str(subtitle_final))
            result.subtitle_paths = subtitle_paths
            result.status = "placed"

        except Exception as exc:
            result.status = "failed"
            result.error = f"verification failed: {exc}" if "verification" in str(exc).lower() else str(exc)
            any_failed = True
        results.append(result)
    status = "transfer_conflict" if any_conflict else "failed" if any_failed else "dry_run" if any_dry_run else "imported_to_jellyfin"
    return TransferSummary(job_id=job_id, status=status, items=results)


def _transfer_with_rsync(db, config: AppConfig, job_id: int, items: list[dict]) -> TransferSummary:
    if not config.rsync_destination and not config.rsync_target:
        raise ValueError("rsync transfer requires transfer.rsync_target")
    target_root = (config.rsync_destination or config.rsync_target or "").rstrip("/")
    results: list[TransferItemResult] = []
    for item in items:
        source = Path(item["matched_output_path"])
        final_path = Path(item["expected_final_path"])
        incoming = f"{target_root}/job_{job_id}/{final_path.name}"
        result = TransferItemResult(
            source_file_id=int(item["source_file_id"]),
            source_output_path=str(source),
            incoming_path=incoming,
            final_path=str(final_path),
            status="requires_final_placement",
            verification=config.transfer_verify,
        )
        if not config.dry_run:
            subprocess.run(["rsync", *config.ssh_options, str(source), incoming], check=True)
        subtitle_paths: list[str] = []
        for subtitle in item.get("subtitle_outputs", []) or []:
            subtitle_name = subtitle.get("output_name")
            if not subtitle_name:
                continue
            subtitle_source = source.with_name(subtitle_name)
            subtitle_incoming = f"{target_root}/job_{job_id}/{subtitle_name}"
            if not config.dry_run:
                subprocess.run(["rsync", *config.ssh_options, str(subtitle_source), subtitle_incoming], check=True)
            subtitle_paths.append(subtitle_incoming)
        result.subtitle_paths = subtitle_paths
        results.append(result)
    return TransferSummary(job_id=job_id, status="transferred_to_eddy_incoming", items=results, warnings=["rsync final placement is not configured"])


def _verify_paths(source: Path, destination: Path, mode: str) -> None:
    if mode == "none":
        return
    if not destination.exists():
        raise IOError("verification failed: destination missing")
    if mode == "size":
        if source.stat().st_size != destination.stat().st_size:
            raise IOError("verification failed: size mismatch")
        return
    if mode == "sha256":
        if _sha256(source) != _sha256(destination):
            raise IOError("verification failed: sha256 mismatch")
        return
    raise ValueError(f"unknown transfer verification mode: {mode}")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _summary_dict(summary: TransferSummary) -> dict:
    return {
        "job_id": summary.job_id,
        "status": summary.status,
        "warnings": summary.warnings,
        "items": [asdict(item) for item in summary.items],
    }
