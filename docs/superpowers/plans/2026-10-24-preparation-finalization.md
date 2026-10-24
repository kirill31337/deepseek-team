# Preparation Failure Finalization Implementation Plan

> **For agentic workers:** Use `superpowers:executing-plans` task by task. DeepSeek
> workers remain leaf workers and must not delegate, stage, commit or publish.

**Goal:** Safely cancel preparation failures and recover previously cleaned jobs
without provider execution or fabricated attempts.

**Architecture:** Associate the job with its assignment before preparation can
fail. Share authoritative identity validation between pre-cleanup attestation and
publication; replay durable intent after cleanup interruption.

**Tech Stack:** Python standard library, unittest, existing Linux lifecycle locks
and Bubblewrap sandbox.

**Spec:** `docs/superpowers/specs/2026-10-24-preparation-finalization-design.md`

## Global Constraints

- No provider calls in regressions; no invented execution attempts.
- Preserve user changes and credentials; no raw model logs in Git.
- Mandatory Linux worker sandbox and current AppArmor restrictions remain enabled.
- Coordinator owns security decisions, integration and final verification.
- Test command: `PYTHONPATH=src python3 -m unittest discover -s tests -v`.

## Review Focus

- A preparation failure in a linked correction must retain one stable job and
  cancellation authority without fabricating execution history.
- Terminal leases still require owner/lock and identity checks during repair.
- Crashes before the first ledger publication must recover from durable intent.
- A binding written by attestation must survive the cleanup callback boundary.
- Restored/replaced execution paths and conflicting history must be preserved.

## Task 1: Regressions and durable preparation association

**Files:** `src/codex_deepseek_team/workspace_lifecycle.py`,
`src/codex_deepseek_team/coordination.py`, `src/codex_deepseek_team/managed.py`,
and, if needed for the initial allocation boundary,
`src/codex_deepseek_team/workspace.py`;
`tests/test_preparation_finalization.py` and affected existing lifecycle tests.
Adversarial historical cases live in
`tests/test_preparation_recovery_boundaries.py`, sharing a helper-only fixture
from `test_preparation_finalization.py` without inheriting its test methods.

**Interfaces:** Add a preparation-association operation separate from
`prepare_attempt(copy, task_id, assignment_id, ...)`; use a coordination entrypoint
to validate and publish matching job/workspace identity without starting a model.
Keep existing assignment-start and correction/resume public contracts.

- [x] Add real provider-free missing-command and failing-probe reproductions.
- [x] Run the focused regressions against the old implementation and retain the
  expected failures as verification evidence.
- [x] Implement durable preparation binding and history, including correction
  continuity and preparation failure reconciliation.
- [x] Verify binding is present before the failure while attempts remain absent,
  and run existing preparation/continuity/lifecycle tests.
- [x] Freeze bound preparation deliverables against plan removal, reassignment and
  definition changes without inventing an execution start; verify equivalent-plan
  replay and later cancellation in `tests/test_preparation_binding_plan.py`.

## Task 2: Authorized, repeatable cleanup and historical recovery

**Files:** `src/codex_deepseek_team/coordination.py`,
`src/codex_deepseek_team/workspace_gc.py`,
`src/codex_deepseek_team/workspace_lifecycle.py`,
`src/codex_deepseek_team/coordinator_hooks.py`;
`tests/test_preparation_finalization.py` and affected existing lifecycle tests.

**Interfaces:** Keep `finalize_assignment(..., cancelled=...)` and the existing CLI.
Its pre-cleanup attestation and `_publish_job_finalization` must share complete
identity validation. `_reconcile_job_publications` may replay only validated
durable intent and terminal cleanup state, including narrowly proven old records.

- [x] Add red tests for reject/cancel/complete, old null-binding recovery,
  repeated finalize and failure injected after cleanup but before publication.
- [x] Add refusal tests for foreign owner, active lock, conflicting binding or
  history, mismatched source/task/job/workspace/assignment and replacement work.
- [x] Implement preflight validation and narrowly authorized binding recovery,
  ensuring cleanup reloads lease updates made by attestation.
- [x] Implement idempotent publication replay without changing original failure,
  attempt history, disposition or terminal decision.
- [x] Cover explicit-copy early sandbox failure, pending-intent resume refusal,
  pinned allocation HEAD, Stop reconciliation, real preparation-only legacy
  history, disposition races and missing job identity in
  `tests/test_preparation_review_regressions.py`.
- [x] Run focused regressions and existing job/GC/review/continuity tests.

## Task 3: Coordinator review and integration

- [x] Inspect the worker's actual diff, red/green evidence and declared checks.
- [x] Review identity corroboration, locks and crash boundaries against the spec.
- [x] Integrate accepted changes without touching `AGENTS.md` or private state.
- [x] Run the full repository test command and inspect all failures/skips.
- [x] Verify the packaged source includes the fix and tests exercise public
  completion/Stop behavior without calling a provider.
- [x] Record worker quality and disposition, finalize accepted worker copies,
  record coordinator outcomes and explicitly complete the coordination task.

No source commit or package publication is requested.

## Final verification

- `PYTHONPATH=src python3 -m unittest discover -s tests -v`: 1814 tests,
  exit 0, five existing capability/opt-in skips (Claude, stress and nested mount).
- All 35 new lifecycle regressions ran without a provider; real foreign UID and
  namespace capture checks passed on the coordinator host.
- Final wheel built successfully; all six modified production modules matched
  the repository source byte for byte.
- Worker reports and diffs were reviewed, accepted with explicit quality and
  finalized through the normal CLI. Source findings were corrected by the
  coordinator; expected-red test checks remain recorded as such.
