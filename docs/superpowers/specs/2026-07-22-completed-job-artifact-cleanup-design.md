# Completed Job Artifact Cleanup Design

## Goal

Remove temporary media-pipeline files as soon as a job has a verified final
import, without touching incomplete, held, shared, or unsafe source folders.

## Scope

After a successful transfer, Disc Steward will:

- cancel queued preview work and remove tracked previews for the completed job;
- remove the completed raw-rip folder when folder cleanup is enabled;
- remove validation/working outputs for that job;
- record cleanup errors as warnings without changing a successful transfer.

The cleanup remains protected by verified validation and transfer results,
existing final media, mount checks, cleanup holds, shared-folder checks, and
canonical containment checks. Raw folders must be under the raw-rip root;
working outputs must be children of either the configured FileFlows working
root or validation root; previews must be children of the preview-cache root.
Each root itself and any symlinked parent are rejected. Canonical containment,
mount availability, and transfer verification are checked again immediately
before every unlink or tree removal; planned paths are never trusted later.

Preview cleanup cancels the job's preview queue state before deletion. Preview
publishing uses an atomic database state transition: a worker may publish its
temporary file only while it still owns a running, non-cancelled row. Cleanup
cancels that row atomically, so a late worker discards its temporary file.
A failed preview unlink leaves that preview's database metadata intact and
becomes a transfer warning; it cannot change the completed transfer result.

## Existing jobs

A command path will remove previews for already-completed jobs only when their
database records identify the preview and the file safely resolves under the
configured preview root. It will use the same completion checks as raw and
working cleanup. It will not remove unknown files merely because they reside
in the preview cache.

## Testing

Tests will prove folder mode also selects working outputs, transfer completion
invokes every configured cleanup type, previews cannot escape their cache root,
failed preview deletion preserves metadata and transfer success, cancellation
between the final worker check and publish cannot recreate a preview, and a
post-plan symlink swap cannot delete outside a configured root. The tracked
example configuration will document cleanup enabled, live, folder and working
deletion, zero retention, and preview deletion. Applying these settings to the
ignored live config is a separate operator step.
