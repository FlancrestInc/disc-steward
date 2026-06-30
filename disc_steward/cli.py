from __future__ import annotations

import argparse
import logging

from .config import load_config
from .cleanup import execute_cleanup, plan_cleanup
from .db import Database
from .reports import generate_reports
from .scanner import scan_completed_rips
from .status import build_status_summary, format_status_summary
from .transfer import transfer_job_to_eddy
from .utils import configure_logging
from .validation import validate_job_outputs
from .web import serve_review_ui
from .work_orders import create_fileflows_work_orders

LOG = logging.getLogger(__name__)


def build_parser() -> argparse.ArgumentParser:
    shared = argparse.ArgumentParser(add_help=False)
    shared.add_argument("--config", default=argparse.SUPPRESS, help="Path to config YAML")
    shared.add_argument("--verbose", action="store_true", default=argparse.SUPPRESS)
    parser = argparse.ArgumentParser(prog="disc-steward", parents=[shared])
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("scan", parents=[shared], help="Scan completed rip folders")
    sub.add_parser("report", parents=[shared], help="Generate static HTML reports")
    serve = sub.add_parser("serve", parents=[shared], help="Serve review/report UI")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8765)
    prepare = sub.add_parser("prepare-fileflows", parents=[shared], help="Create FileFlows work orders for reviewed jobs")
    prepare.add_argument("--job-id", type=int, required=True)
    validate = sub.add_parser("validate", parents=[shared], help="Validate FileFlows output")
    validate.add_argument("--job-id", type=int, required=True)
    transfer = sub.add_parser("transfer", parents=[shared], help="Transfer validated output to Eddy")
    transfer.add_argument("--job-id", type=int, required=True)
    sub.add_parser("cleanup-plan", parents=[shared], help="Plan cleanup eligibility without changing files")
    sub.add_parser("cleanup", parents=[shared], help="Execute configured cleanup; disabled and dry-run by default")
    sub.add_parser("status", parents=[shared], help="Show pipeline status summary")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    configure_logging(getattr(args, "verbose", False))
    config = load_config(getattr(args, "config", "config.yaml"))
    db = Database(config.database_path)
    db.initialize()
    if args.command == "scan":
        job_ids = scan_completed_rips(db, config)
        LOG.info("scanned_jobs=%s", job_ids)
        return 0
    if args.command == "report":
        reports = generate_reports(db, config)
        for report in reports:
            print(report)
        return 0
    if args.command == "serve":
        print(f"Serving Disc Steward review UI at http://{args.host}:{args.port}")
        serve_review_ui(db, config, args.host, args.port)
        return 0
    if args.command == "prepare-fileflows":
        folder = create_fileflows_work_orders(db, config, args.job_id)
        print(folder)
        return 0
    if args.command == "validate":
        summary = validate_job_outputs(db, config, args.job_id)
        print(f"{summary.status}: {len(summary.items)} item(s)")
        for item in summary.items:
            print(f"{item.source_file_id}: {item.status} {item.matched_output_path or item.expected_output_name}")
            for warning in item.warnings:
                print(f"  warning: {warning}")
            for error in item.errors:
                print(f"  error: {error}")
        for warning in summary.warnings:
            print(f"warning: {warning}")
        return 0 if summary.passed else 2
    if args.command == "transfer":
        summary = transfer_job_to_eddy(db, config, args.job_id)
        print(f"{summary.status}: {len(summary.items)} item(s)")
        for item in summary.items:
            print(f"{item.source_file_id}: {item.status} -> {item.final_path}")
            if item.conflict:
                print(f"  conflict: {item.conflict}")
            if item.error:
                print(f"  error: {item.error}")
        for warning in summary.warnings:
            print(f"warning: {warning}")
        return 0 if summary.status == "imported_to_jellyfin" else 2
    if args.command == "cleanup-plan":
        summary = plan_cleanup(db, config)
        print(f"eligible: {len(summary.eligible)}")
        for item in summary.eligible:
            suffix = f" -> archive {item.archive_path}" if item.archive_path else ""
            print(f"  {item.item_type}: {item.path} ({item.reason}){suffix}")
        print(f"ineligible: {len(summary.ineligible)}")
        for item in summary.ineligible:
            print(f"  {item.item_type}: {item.path} ({item.reason})")
        return 0
    if args.command == "cleanup":
        summary = execute_cleanup(db, config)
        print(f"cleanup {'dry-run' if summary.dry_run else 'live'}: deleted={len(summary.deleted)} archived={len(summary.archived)} errors={len(summary.errors)}")
        for error in summary.errors:
            print(f"error: {error}")
        return 0 if not summary.errors or config.cleanup.dry_run else 2
    if args.command == "status":
        print(format_status_summary(build_status_summary(db, config)))
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
