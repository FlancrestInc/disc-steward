from __future__ import annotations

import logging
import shlex
import subprocess
import tempfile
import time
from pathlib import Path
from threading import Event

from .config import AppConfig
from .db import Database

LOG = logging.getLogger(__name__)


def preview_output_path(config: AppConfig, job_id: int, source_file_id: int) -> Path:
    return Path(config.preview.output_path) / f"job_{job_id}" / f"source_{source_file_id}.mp4"


def preview_is_current(row: dict, scanned_modified_time: float, scanned_size_bytes: int, output_path: Path) -> bool:
    if row.get("preview_status") != "ready":
        return False
    if not output_path.exists():
        return False
    if row.get("preview_source_modified_time") != scanned_modified_time:
        return False
    if row.get("preview_source_size_bytes") != scanned_size_bytes:
        return False
    return True


def queue_preview_for_source_row(
    db: Database,
    config: AppConfig,
    job_id: int,
    row: dict,
    *,
    force_reprocess: bool = False,
) -> bool:
    output_path = preview_output_path(config, job_id, int(row["id"]))
    if not force_reprocess and preview_is_current(
        row,
        float(row["modified_time"]),
        int(row["size_bytes"]),
        output_path,
    ):
        return False
    db.queue_preview_job(
        job_id,
        int(row["id"]),
        str(row["path"]),
        str(output_path),
        source_size_bytes=int(row["size_bytes"]),
        source_modified_time=float(row["modified_time"]),
        force_reprocess=force_reprocess,
    )
    db.audit(
        "preview_queued",
        "Queued preview generation",
        job_id,
        {
            "source_file_id": int(row["id"]),
            "source_path": str(row["path"]),
            "preview_path": str(output_path),
            "force_reprocess": force_reprocess,
        },
    )
    return True


def queue_previews_for_job(
    db: Database,
    config: AppConfig,
    job_id: int,
    *,
    force_reprocess: bool = False,
) -> int:
    if not config.preview.enabled:
        return 0
    source_rows = db.source_file_payloads(job_id)
    queued = 0
    for row in source_rows:
        if queue_preview_for_source_row(db, config, job_id, row, force_reprocess=force_reprocess):
            queued += 1
    return queued


def _preview_command(config: AppConfig, source_path: Path, output_path: Path, *, encoder: str) -> list[str]:
    base = [
        config.preview.ffmpeg_path,
        "-hide_banner",
        "-y",
        "-i",
        str(source_path),
        "-t",
        str(config.preview.clip_duration_seconds),
        "-vf",
        f"yadif,scale=-2:{config.preview.height}",
        "-map",
        "0:v:0",
        "-map",
        "0:a:0?",
        "-c:v",
        encoder,
        "-c:a",
        "aac",
        "-b:a",
        "96k",
        "-ac",
        "2",
        "-movflags",
        "+faststart",
    ]
    if encoder == "h264_nvenc":
        base.extend(["-preset", "p5", "-cq", str(config.preview.quality)])
    else:
        base.extend(["-preset", "veryfast", "-crf", str(config.preview.quality)])
    base.append(str(output_path))
    return base


def _preview_runner(config: AppConfig):
    method = (config.processing.method or "local").strip().lower()
    if method in {"", "local"}:
        return lambda command: subprocess.run(command, check=True, capture_output=True, text=True)
    if method == "ssh":
        target = config.processing.ssh_target.strip()
        if not target:
            raise ValueError("processing.ssh_target is required when processing.method is ssh")
        user = config.processing.ssh_user.strip()
        ssh_destination = f"{user}@{target}" if user else target
        ssh_command = ["ssh", *config.processing.ssh_options, ssh_destination]
        host_pipeline_root = config.to_barnabas_path(config.pipeline_root)
        docker_image = config.processing.docker_image.strip()
        if not docker_image:
            raise ValueError("processing.docker_image is required when processing.method is ssh")
        docker_state_root = config.processing.docker_state_root.strip()
        if not docker_state_root:
            raise ValueError("processing.docker_state_root is required when processing.method is ssh")

        def run_remote(command: list[str]) -> object:
            translated = [str(config.to_barnabas_path(part)) for part in command]
            docker_command = [
                "docker",
                "run",
                "--rm",
                "--init",
                "--gpus",
                "all",
                "-e",
                "NVIDIA_VISIBLE_DEVICES=all",
                "-v",
                f"{host_pipeline_root}:{host_pipeline_root}",
                "-v",
                f"{docker_state_root}:{docker_state_root}",
                "-w",
                str(host_pipeline_root),
                docker_image,
                *translated,
            ]
            return subprocess.run([*ssh_command, shlex.join(docker_command)], check=True, capture_output=True, text=True)

        return run_remote
    raise ValueError(f"Unknown processing.method: {config.processing.method}")


def _run_preview_ffmpeg(config: AppConfig, source_path: Path, output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temp_handle = tempfile.NamedTemporaryFile(prefix=output_path.stem + ".", suffix=".part.mp4", dir=output_path.parent, delete=False)
    temp_handle.close()
    temp_path = Path(temp_handle.name)
    try:
        encoders = [config.preview.encoder]
        if config.preview.fallback_encoder not in encoders:
            encoders.append(config.preview.fallback_encoder)
        last_error: subprocess.CalledProcessError | None = None
        runner = _preview_runner(config)
        for encoder in encoders:
            command = _preview_command(config, source_path, temp_path, encoder=encoder)
            LOG.info("preview encode start source=%s output=%s encoder=%s", source_path, output_path, encoder)
            try:
                runner(command)
                temp_path.replace(output_path)
                return
            except subprocess.CalledProcessError as error:
                last_error = error
                stderr = (error.stderr or "") + (error.stdout or "")
                if encoder != config.preview.fallback_encoder and not any(
                    phrase in stderr for phrase in ["Unknown encoder", "Error initializing output stream", "Error while opening encoder"]
                ):
                    raise
                LOG.warning("preview encoder %s failed for %s: %s", encoder, source_path, error.stderr or error.stdout or error)
        assert last_error is not None
        raise last_error
    finally:
        if temp_path.exists():
            temp_path.unlink(missing_ok=True)


def _process_next_preview_job(db: Database, config: AppConfig, worker_name: str | None = None) -> bool:
    queued = db.claim_next_preview_job(worker_name)
    if queued is None:
        return False
    source_file_id = int(queued["source_file_id"])
    source_path = Path(queued["source_path"])
    preview_path = Path(queued["preview_path"])
    try:
        if not source_path.exists():
            raise FileNotFoundError(f"Source file not found: {source_path}")
        _run_preview_ffmpeg(config, source_path, preview_path)
        db.finish_preview_job(
            source_file_id,
            state="ready",
            generated_at=time.strftime("%Y-%m-%d %H:%M:%S"),
            preview_path=str(preview_path),
        )
        db.audit(
            "preview_complete",
            "Preview generation finished",
            int(queued["job_id"]),
            {"source_file_id": source_file_id, "preview_path": str(preview_path)},
        )
    except Exception as error:  # pragma: no cover - defensive logging for background work
        message = str(error)
        db.finish_preview_job(source_file_id, state="failed", error=message)
        db.audit(
            "preview_failed",
            f"Preview generation failed: {message}",
            int(queued["job_id"]),
            {"source_file_id": source_file_id, "preview_path": str(preview_path), "error": message},
        )
    return True


def run_preview_worker(
    db: Database,
    config: AppConfig,
    *,
    poll_interval: float = 1.0,
    stop_event: Event | None = None,
    worker_name: str | None = None,
) -> None:
    db.reset_stuck_preview_jobs()
    while True:
        processed = _process_next_preview_job(db, config, worker_name)
        if stop_event is not None and stop_event.is_set():
            return
        if processed:
            continue
        if stop_event is not None and stop_event.wait(timeout=poll_interval):
            return
        time.sleep(poll_interval)


def preview_job_status_summary(db: Database, job_id: int) -> dict[str, int]:
    rows = [row for row in db.list_preview_jobs() if row["job_id"] == job_id]
    counts: dict[str, int] = {"queued": 0, "running": 0, "ready": 0, "failed": 0}
    for row in rows:
        state = row["state"]
        counts[state] = counts.get(state, 0) + 1
    counts["total"] = len(rows)
    return counts
