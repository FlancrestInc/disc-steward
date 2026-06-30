from __future__ import annotations

import shutil
from pathlib import Path

from .models import TransferConflict


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
