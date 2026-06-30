from __future__ import annotations

import argparse
import logging

from .config import load_config
from .db import Database
from .reports import generate_reports
from .scanner import scan_completed_rips
from .utils import configure_logging
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
    sub.add_parser("cleanup", parents=[shared], help="Evaluate cleanup eligibility; deletion disabled by default")
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
        LOG.warning("validate is scaffolded for manual invocation in Phase 3")
        return 2
    if args.command == "transfer":
        LOG.warning("transfer is scaffolded and dry-run guarded for Phase 3")
        return 2
    if args.command == "cleanup":
        if not config.cleanup_enabled:
            LOG.info("cleanup disabled; no files will be deleted")
            return 0
        LOG.warning("cleanup automation is not implemented in Phase 1")
        return 2
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
