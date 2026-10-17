# Workspace lifecycle and bounded execution storage

The developer handoff dated 2026-10-17 is the acceptance contract. The user
also requests safe cleanup of old unused copies when upgrading in other
projects. This release is universal package work; it does not connect to or
modify the server described in the handoff.

## Invariants

One logical job owns one workspace until final acceptance or explicit
cancellation. Process attempts have distinct identities and append their
results. Failure, review, retry queues, Stop and compaction preserve the job,
workspace id, inode and previous edits. A process exit is never finalization.
No implicit implementation retry is introduced.

Finalization is per job, independent of a parent feature task. Before deletion,
publish a binary-capable patch including nonignored untracked source changes,
provenance and bounded diagnostic evidence. Preserve historical status, inode,
exit codes and checks. Append separate finalized and cleaned events. Never
store a checkout archive, hidden backup or reproducible runtime as history.
Finalized jobs cannot execute again; a new job gets a new copy.

## Lease and recovery contract

Persist an explicit job lease before allocating its execution tree. A lease
records its generation, owner identity, current phase, heartbeat and attempts.
Linux owner identity includes PID, process start time and boot id, so a reused
PID or a reboot cannot inherit ownership. Explicit resume can adopt an orphan
after inspection, with a new ownership generation. Retry/review phases remain
leased when no worker process exists. Coordinator hooks renew their own jobs;
Stop does not finalize them. Historic assignment references and a parent task's
active/planned status do not constitute live leases.

A live coordinator may perform library/CLI operations through an actual child
process. Revalidate its PID/start/boot/namespace identity and ancestry, retain
the original coordinator owner and generation, and never adopt an unrelated
live owner. Leases and kernel owner locks remain authoritative. Process scans
add positive argv/cwd/fd/maps evidence scoped to a copy; a ptrace restriction on
an unrelated keyring or runner process is not evidence against every copy.
An unobservable recorded job owner still preserves its job.

Standalone/library creation participates in the same protocol. Recovery uses
lease ownership, real operation locks, retry state and process references; a
directory age alone is insufficient evidence. Malformed or unsafe metadata
fails closed with an explanation, without executing Git or following paths.

## Concurrency and deletion boundary

A shared per-state creation/collection fence covers initial creation and every
operation that uses a copy (prepare, execution, resume, import, review and
verification), including direct Python APIs. Acquire it before the per-copy
owner lock, validate identity, establish the operation/lease, then release the
shared fence. Collection takes the same fence and never waits for a busy copy.

The destructive primitive uses pinned directory descriptors, no-follow opens,
UID/device/inode checks and a mount-boundary check, including bind mounts.
Quarantine is an atomic rename on the same filesystem into a private owned
directory. Release the creation fence before recursive deletion. Recheck live
references after rename; restore only the same inode and only into an absent
original destination. Partial deletion is recoverable and idempotent. Never
delete source repositories, credentials, coordinator configuration or live
services. Never execute an execution copy's Git hooks, filters or fsmonitor as
root when collecting its result.

## Storage and recipes

Honor explicit state configuration and TMPDIR. Prefer appropriate writable
data storage without embedding one server's paths. Admission reserves actual
bytes and inodes under a shared lock, accounts for all admitted jobs and leaves
a free reserve. Check each affected filesystem rather than treating unrelated
mounts as a single pool. Keep the default worker limit at eight.

Tool recipes cannot retain dependencies on a finalized execution tree. Tools
may use an immutable content-verified shared cache with a finite budget and
safe sandbox mounts. Mutable worker files must not be hardlinked together.
Reject or invalidate stale recipe inputs with an actionable error; never replay
a missing source indefinitely. Code results come from a permanent source
repository and the retained patch.

## User interfaces and upgrades

Provide explicit resume, finalize, cancel, recover and GC interfaces, plus
`gc --dry-run` and `gc --apply`. Reports include allocation bytes/inodes, action
and a concrete reason for every retained/deleted copy. Inspection and history
must work after execution-tree deletion.

Final accepted/rejected/cancelled dispositions finalize the individual job.
`needs-rework` preserves its copy and permits a linked correction to reuse it.
Bootstrap/setup/init run the same recovery collector for discoverable owned
state roots, preserving active jobs and user-managed delegation guides. Do not
scan arbitrary project directories, change delegation/access/model/effort or
weaken the mandatory Linux sandbox. Upgrade cleanup is provider-free.

## Verification

Use disposable state roots and real small Git fixtures, never destruction of
the operator's real state. Prove RED then GREEN for the three-attempt cycle,
review gaps, final acceptance/cancel, legacy orphan recovery, concurrent API
creation/prepare/import/review/GC, interrupted quarantine deletion, malformed
metadata and path/mount/PID adversaries. A 100-job experiment must leave no
completed execution trees and report allocated bytes/inodes. Run the existing
workspace/toolchain/storage/runner/coordination/sandbox suite, package metadata
checks and clean installer checks before publishing a new immutable version.

## Coordinator workspace

Implementation uses branch `feat/workspace-lifecycle` in the clean source
checkout. A coordinator-owned detached worktree at
`/mnt/data/deepseek-team-lifecycle` runs the pristine baseline independently;
remove it after its checks and reachability verification. Worker isolation is
provided by the existing managed runner, with no synthetic source commits.
