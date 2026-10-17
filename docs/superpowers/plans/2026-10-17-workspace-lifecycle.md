# Workspace lifecycle implementation plan

Spec: `docs/superpowers/specs/2026-10-17-workspace-lifecycle.md`.
Baseline: `1f35e7a`; 1434 tests passed, 3 unavailable-Claude skips.
Execution follows the project's Auto/full-access distribution. Independent
DeepSeek work owns budget/cache modules; coordinator owns deletion security,
cross-component lifecycle integration, final review and publication.

## 1. Persistent job state and fence

- Write `tests/test_workspace_lifecycle.py` with real temporary repositories.
  Assert a stable job/workspace/inode over three distinct attempts, immutable
  earlier attempt outcomes, retained review/retry without worker PID, identity
  checks for boot/start-time/PID and explicit ownership generation on recovery.
- Run the new tests and observe missing lifecycle behavior (RED).
- Add `workspace_lifecycle.py`: `fence(state)`, `initialize(copy, owner=None)`,
  `read(copy)`, `begin(copy, operation)`, `finish(copy)`,
  `renew(copy, owner=None, recover=False)`, `rework(copy)` and owner identity
  helpers. Store `lease.json` outside `work/`, atomically under the fence.
- Test no-follow metadata, malformed identities, concurrent access and a
  finalized lease refusing another attempt. Run lifecycle tests to GREEN.

## 2. Compact artifacts and safe collection

- Write `tests/test_workspace_gc.py`: binary/untracked patch survives deletion;
  acceptance/cancel removes work immediately; live leases/locks/retry preserve
  it; legacy orphan states clean independently of parent-task status;
  dry-run changes no bytes; partial deletion recovers; symlink/inode/mount/UID
  adversaries preserve outside canaries.
- Add `workspace_gc.py`: `finalize(copy, reason='accepted')`,
  `cancel(copy, reason='cancelled')`, `collect(state=None, apply=False)` and
  `recover(copy, owner=None)`. Readers use the retained record and patch after
  cleanup. Deletion intent is separate from historical execution status.
- Lock order: state fence → nonblocking workspace owner lock; release fence
  before rmtree. Never obtain a workspace lock while holding a coordination
  ledger lock. Archive publication failure keeps the execution tree.
- Verify exact patch application to a fresh repo and no root Git execution
  during collection. Run destructive tests only against disposable state.

## 3. Budget and recipe cache (independent worker deliverables)

- `storage_budget.py`: `measure(path)` returns allocated bytes and inodes;
  `reserve(state, disk_bytes=..., inodes=..., minimum_free_bytes=268435456,
  minimum_free_inodes=1024)` serializes concurrent reservations against actual
  filesystem availability and uses held flocks for crash recovery.
- `toolchain_cache.py`: `snapshot(state, source, max_bytes=2147483648,
  max_inodes=200000)` creates an immutable validated content source;
  `collect(state, apply=False, ...)` reports/evicts within a finite cap.
  Tool recipes use permanent cache sources. Preserve existing sandbox mapping
  and independently materialize active writable inputs; never hardlink them.
- Worker acceptance includes RED/GREEN, concrete diffs and focused tests.
  Coordinator reviews and integrates; add reservations around checkout,
  staging and runtime allocation after both interfaces are available.

## 4. Runner and coordination integration

- Add `tests/test_job_integration.py` before changing consumers. Exercise a
  failed attempt, reviewed explicit continuation, successful attempt needing
  another linked correction, final acceptance and immediate deletion. Assert
  stable workspace and unchanged historical failures throughout.
- Wire `Workspace.create/lock/begin/finish` to fence and lease state. The lease
  precedes materialization. `load` and inspection tolerate cleaned workspaces;
  executable operations refuse finalized state clearly.
- Runner claims coordinator ownership and keeps it through review/retry.
  Failed/resumed attempts cannot silently switch workspaces. A planned linked
  correction may reuse an unfinalized logical job; finalized jobs cannot run.
- Preserve repeatable `use_result` feedback: explicit `finalize` marks that no
  further corrections are expected. CLI final acceptance can combine use and
  finalization with `--final`; `needs-rework` never deletes. Explicit cancel
  and abandon finalize cancellation after processes are stopped.
- Read patch provenance without loading `work/`; add attempt identities as
  optional fields, preserving existing history and quality aggregation.
- Hooks renew the current coordinator's leases; Stop/compaction preserve
  unfinished leases. Do not interpret a parent task's status as liveness.

## 5. CLI, upgrade recovery and documentation

- Route a bounded implementation slice after lifecycle interfaces exist:
  top-level `gc --dry-run|--apply [--state-dir] [--json]`, workspace
  `resume/finalize/cancel/recover/list/history`, and coordination finalization.
- Setup/init run provider-free recovery on explicit/env/saved/default and
  recorded previous state roots. Do not recursively discover arbitrary paths.
  Preserve user delegation guides, settings and disabled status byte-for-byte.
- Replace the old promise of indefinite retained copies in generated guidance
  and English/Russian docs. Explain explicit final acceptance and safe upgrade
  cleanup. Prepare 0.9.0 version declarations and release notes.

## 6. Final verification and publication

- Run focused lifecycle/storage/toolchain/coordination tests, then the full
  `PYTHONPATH=src python3 -m unittest discover -s tests -v` suite once integrated.
- Run 100+ finalized jobs against disposable state and report measured
  allocated bytes/inodes, no retained execution trees or quarantine.
- Verify real Bubblewrap boundaries; keep default max_workers=8, model
  deepseek-flash and Linux/AppArmor policies unchanged.
- Obtain an independently routed review, address concrete defects with
  regression checks and record worker outcomes/rework in the private ledger.
- Build wheel/sdist, strict Twine metadata and clean pip/pipx/uv installation
  checks; inspect artifacts for credentials/config/model logs.
- Commit verified source, publish the immutable GitHub release and exact
  artifacts to PyPI; verify registry installation before claiming publication.

## Review focus

Interrupted creation before owner.lock; collection after quarantine rename;
same-device bind mounts and parent replacement; live retry with no process;
reused PID/reboot; stale recipe source after prior checkout deletion; root Git
hooks/fsmonitor/filters; full untracked binary patch; allocation on multiple
state roots sharing one filesystem; cleanup without rewriting historical
work_inode/status/check/exit evidence.

## Progress

- Diagnostics accepted: current implementation lacks job leases/finalization;
  prepare and runner hold workspace → coordination lock order. No source diff
  from either diagnostic assignment.
- Baseline verified: 1434 tests in 422.298s, OK (3 Claude unavailable skips).
- Implementation/budget/cache distribution registered before source changes.
- User clarification: no server connection; publish universal package update.
- Lease/GC and runner regressions passed on the coordinator host, including
  same-copy failed retry, linked correction and per-process attempt identities,
  explicit finalization before parent completion, interrupted source fetch,
  missing lease preservation and recovery after coordinator loss.
- Independent adversarial coverage ran 100 completed jobs: 2,060,288 allocated
  bytes and 604 inodes retained, with no checkout/runtime/quarantine/archive
  trees; binary/untracked patches and historical records remained usable.
- Cache correction passed 92 host checks, including real foreign-UID and bind
  mount adversaries. Budget correction passed 30 checks (one existing-mount
  fixture skipped); coordinator retained an unlocked lock-inode descriptor to
  close the reviewed lifetime gap. Original failures remain in the ledger.
- Runtime scratch/control now live under the owned job directory and reserve
  per-filesystem headroom before payload/probes/credential access. A pinned
  descriptor alias keeps Unix socket addresses short without external temp
  payloads. A crashed runtime's stopped socket can be removed on finalization.
- Universal CLI/setup/init cleanup uses bounded known state roots and never
  scans arbitrary project directories. Upgrade fixtures preserve user settings,
  keys, coordinator configuration and unknown/unsafe data.
- Materialization reservations passed 139 host checks. Coordinator regressions
  exposed and fixed missing source-owner guards for direct cache snapshots and
  inputs from another state root; original worker rework grades remain recorded.
- Independent read-only review delivered six concrete transition reproducers.
  Reserved retries and reviewed results now remain protected until explicit
  finalization; preparation failures carry job identity; final review evidence
  is immutable; preparation cannot consume a reserved execution attempt; setup
  errors close the attempt. Historic quality reassessment remains append-only.
- A full discovery with 100-job stress passed 1598 tests in 581.332s (4 skips:
  Claude unavailable and one existing-mount fixture). Subsequent real file-bind
  mount regression exposed a missing regular-file mount check; the corrected
  GC/atomic-rename suite passed all 21 tests. Final full discovery on the complete
  tree and installed 0.9.0 candidate passed 1599 tests in 589.622s (4 skips),
  including the 100-job stress result above. The Python-version CI matrix is the
  remaining integration gate before the version tag is published.
- Wheel/sdist builds, strict Twine metadata, credential/path audit and isolated
  pip/pipx/uv installation/replacement/removal checks passed. The local lifecycle
  CLI was upgraded to the candidate; only this repository's owned guide was
  refreshed, with its human prefix verified unchanged. The handoff server was
  not contacted.
- All nine owned worker copies were explicitly finalized after review. Their
  compact patch/result/history remain; original failed checks/statuses were not
  rewritten, and no foreign state was garbage-collected.
