# Source Folder Cleanup Design

## Goal

Free space in raw rip storage by removing a whole completed disc folder after a
verified transfer. Also remove a raw disc folder when its job is deleted in the
review UI.

## Configuration

Add `cleanup.delete_raw_rip_folders`, disabled by default. The existing
`cleanup.enabled` and `cleanup.dry_run` flags remain mandatory safeguards.

## Completed transfer cleanup

After a transfer reaches `imported_to_jellyfin`, Disc Steward will run cleanup
for that job. A raw disc folder is eligible only when all existing cleanup
checks pass: successful validation, verified final placement, available mounts,
no cleanup hold, and elapsed retention period. The cleanup plan will identify
the folder as `raw_rip_folder`, and live cleanup will recursively remove it.

The folder must be inside the configured raw-rip root. Folder cleanup replaces
per-file raw-rip deletion when enabled, so a single cleanup run cannot try to
delete files that have already been removed with their parent folder.

## Deleted jobs

The review UI's Delete job action will delete the raw disc folder before
removing the database job. It will reject paths outside the configured raw-rip
root. If deletion fails, it will leave the job and its saved metadata intact
and report the error. On success, it will write the existing ignored-path
record and audit event, then remove the job.

## Safety and observability

Dry runs never remove folders. Cleanup holds block only completed-transfer
cleanup; a deliberate Delete job action removes its raw folder immediately.
Both paths record auditable outcomes. Missing source folders are treated as
already absent when deleting a job, allowing the database record to be removed.

## Tests and documentation

Tests will cover successful automatic cleanup, dry runs, cleanup holds,
incomplete transfers, raw-root path guards, deleted-job cleanup, and deletion
failures. The README sample will document the new opt-in setting and when it
runs.
