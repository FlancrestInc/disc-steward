# Source Folder Cleanup Design

## Goal

Free space in raw rip storage by removing a whole completed disc folder after a
verified transfer. Also remove a raw disc folder when its job is deleted in the
review UI.

## Configuration

Add `cleanup.delete_raw_rip_folders`, disabled by default. The existing
`cleanup.delete_raw_rips` flag keeps its current per-file behavior for backward
compatibility. The two deletion modes are mutually exclusive: configuration
with both enabled is rejected. `cleanup.enabled` and `cleanup.dry_run` remain
mandatory safeguards.

## Completed transfer cleanup

After a transfer reaches `imported_to_jellyfin` and its summary has been
persisted, Disc Steward will run a job-scoped cleanup plan for that job. This
happens only when cleanup is enabled and `delete_raw_rip_folders` is enabled;
dry-run records the plan without removing anything. Cleanup errors are audited,
but do not undo or mark the completed transfer as failed.

A raw disc folder is eligible only when all existing cleanup checks pass:
successful validation, verified final placement, available mounts, no cleanup
hold, and elapsed retention period. The cleanup plan will identify the folder
as `raw_rip_folder`, and live cleanup will recursively remove it.

The target and raw-rip root are canonicalized before containment is checked.
The target must be a child of, never the root itself, and symlink escapes are
rejected. This check happens before a missing folder can be considered absent.

Folder cleanup is allowed only when no other active job needs that source
folder. For shared or split-job folders, every job referencing the folder must
independently meet the same terminal-success and cleanup requirements before
the folder is eligible. Folder cleanup replaces per-file raw-rip deletion when
enabled, so a single cleanup run cannot mix file and folder candidates.

When raw-rip archival is enabled, folder mode archives the complete folder as
one unit, verifies every copied file, and only then deletes the source folder.

## Deleted jobs

The review UI's Delete job action will delete the raw disc folder before
removing the database job. It uses the same canonical raw-root child check. If
another job still references the folder, it removes only the requested job
record and retains the shared source folder for that remaining job. If the
folder is exclusively referenced and deletion fails, it leaves the job and its
saved metadata intact and reports the error. On success, it writes the existing
ignored-path record and audit event, then removes the job.

## Safety and observability

Dry runs never remove folders. Cleanup holds block only completed-transfer
cleanup; a deliberate Delete job action removes its raw folder immediately.
Both paths record auditable outcomes. Missing source folders are treated as
already absent when deleting a job, allowing the database record to be removed.

## Tests and documentation

Tests will cover transfer-triggered cleanup only when enabled and live, cleanup
errors that preserve transfer success, dry runs, cleanup holds, incomplete
transfers, raw-root/symlink path guards, shared split-job folders, folder
archival ordering and verification, deleted-job cleanup, and deletion failures.
The README sample will document the new opt-in setting and when it runs.
