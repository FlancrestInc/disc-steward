from __future__ import annotations

from collections import Counter

from .config import AppConfig


def build_status_summary(db, config: AppConfig) -> dict:
    jobs = db.list_jobs()
    counts = Counter(job.status for job in jobs)
    validation_failures = 0
    transfer_conflicts = 0
    recent_errors: list[dict] = []
    for job in jobs:
        validation = db.latest_validation_summary(job.id)
        if job.status == "validation_failed" or (validation and not validation.get("passed")):
            validation_failures += 1
        transfer = db.latest_transfer_summary(job.id)
        if job.status == "transfer_conflict" or (transfer and transfer.get("status") == "transfer_conflict"):
            transfer_conflicts += 1
    subtitle_issues = _count_subtitle_issues(db)
    cleanup_items = db.list_cleanup_eligibility()
    for job in jobs:
        for event in db.list_audit_events(job.id)[-5:]:
            if "error" in event["event_type"] or "failed" in event["event_type"]:
                recent_errors.append(event)
    summary = {
        "jobs_discovered": len(jobs),
        "jobs_needing_review": counts["review_needed"] + counts["review_in_progress"] + counts["manual_review"],
        "jobs_waiting_for_processing": counts["reviewed"] + counts["ready_for_fileflows"] + counts["fileflows_work_orders_created"] + counts["ready_for_processing"],
        "jobs_needing_validation": counts["validation_needed"],
        "jobs_ready_for_transfer": counts["transfer_ready"] + counts["validated"],
        "jobs_imported": counts["imported_to_jellyfin"],
        "validation_failures": validation_failures,
        "transfer_conflicts": transfer_conflicts,
        "subtitle_issues_outstanding": subtitle_issues,
        "cleanup_eligible_items": sum(1 for item in cleanup_items if item["eligible"]),
        "recent_errors": recent_errors[-10:],
        "cleanup_enabled": config.cleanup.enabled,
        "cleanup_dry_run": config.cleanup.dry_run,
        "llm_enabled": config.llm.enabled,
        "metadata_enabled": config.metadata.enabled,
    }
    db.cache_status_summary(summary)
    return summary


def format_status_summary(summary: dict) -> str:
    lines = [
        f"jobs discovered: {summary['jobs_discovered']}",
        f"needs review: {summary['jobs_needing_review']}",
        f"waiting for processing: {summary['jobs_waiting_for_processing']}",
        f"needs validation: {summary['jobs_needing_validation']}",
        f"ready for transfer: {summary['jobs_ready_for_transfer']}",
        f"imported: {summary['jobs_imported']}",
        f"validation failures: {summary['validation_failures']}",
        f"transfer conflicts: {summary['transfer_conflicts']}",
        f"subtitle issues outstanding: {summary['subtitle_issues_outstanding']}",
        f"cleanup eligible items: {summary['cleanup_eligible_items']}",
        f"cleanup: {'enabled' if summary['cleanup_enabled'] else 'disabled'} ({'dry-run' if summary['cleanup_dry_run'] else 'live'})",
        f"LLM: {'enabled' if summary['llm_enabled'] else 'disabled'}",
        f"metadata lookup: {'enabled' if summary['metadata_enabled'] else 'disabled'}",
    ]
    for event in summary.get("recent_errors", []):
        lines.append(f"recent error: job {event.get('job_id', '?')} {event.get('event_type')}: {event.get('message')}")
    return "\n".join(lines)


def _count_subtitle_issues(db) -> int:
    count = 0
    for job in db.list_jobs():
        for row in db.source_file_payloads(job.id):
            plan = db.get_subtitle_plan(row["id"])
            if plan and plan.get("statuses") and plan["statuses"] != ["no_action_needed"]:
                count += 1
    return count
