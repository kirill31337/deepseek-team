# Immediate delegation and queued workers implementation plan

> **For agentic workers:** Use superpowers:executing-plans for coordinator implementation. DeepSeek owns the isolated worker-capacity deliverable; do not duplicate it.

**Goal:** Deliver useful DeepSeek work immediately after setup, queue eligible assignments without changing their executor when capacity is busy, and consolidate all project branches into main.

**Architecture:** Separate suitability from process scheduling. Auto defaults to immediate admission of bounded, reviewable work; evidence remains observable and measured negative outcomes still constrain work. A FIFO process queue enforces configurable concurrency independently of how many assignments have been planned. The coordinator retains architecture, security, final verification and integration.

**Tech Stack:** Python 3.11 standard library, SQLite routing ledger, Linux flock/Bubblewrap, unittest.

**Spec:** User-approved proposals in this conversation: immediate useful delegation, active decomposition, proportionate failure handling, visible verified outcomes, deferred execution instead of capacity refusal, configurable parallelism, then integrate every branch and publish main.

## Global constraints

- DeepSeek workers use deepseek-flash with explicit runtime codex and resolved effort.
- Preserve explicit access, disabled state, required Linux isolation and fixed provider relay.
- Never automatically retry an implementation or erase its first quality failure.
- No credentials, local configuration or raw model logs in Git.
- Keep pre-existing installation work and already-merged branch history intact.
- Default runtime concurrency: 8; supported explicit range: 1 through 64. Eight is a throughput-oriented initial setting, not a benchmark-derived optimum.
- Default admission policy: immediate; evidence-based admission remains an explicit compatibility option. Unknown quality/cost cannot by itself prevent an eligible assignment.
- Immediate admission allows small/medium independent low/medium-risk tasks with clear requirements, known/partial localization, local/component coupling, and concrete verification. High/protected/unknown-risk or unverifiable work stays with the coordinator. Manual verification is limited to read-only kinds and documentation with explicit acceptance criteria.
- No bootstrap assignment cap or periodic coordinator holdout under immediate admission. Existing assignment identities and observed history remain stable.
- Rejected outcomes pause the relevant family for 300 seconds by default. One rework does not pause it; three distinct recent rework cases do. Evidence remains recorded truthfully.

## Review focus

- More than eight valid assignments stay delegated and later execute when slots are released.
- Waiting jobs recheck project enabled/access/routing state and HEAD before provider credentials or runtime launch.
- Interrupted or killed waiters cannot deadlock the FIFO queue; active work and failed work never restart automatically.
- Lower concurrency settings count running jobs in higher-numbered slots; unsafe lock paths fail closed.
- Existing saved policies, historical failures and the uncommitted installation feature survive integration and branch cleanup.

### Task 1: Isolated worker queue (DeepSeek)

Files: new worker_slots.py and test_worker_slots.py. Public acquire(state, limit=8, timeout=0, wait=True, on_wait=None, validate=None) returns an owned locked slot FD released with os.close. Add process-level tests before implementation for capacity, FIFO, cancellation, timeout, revalidation and lock-path safety. Run its declared isolated unittest check. Coordinator reviews actual diff before integration.

### Task 2: Immediate routing (coordinator)

- Add failing behavioral tests for cold-start assignments beyond three, medium/component work, registered manual documentation review, retained permission/risk guards, and no holdout.
- Add admission_policy validation and immediate routing, preserving the evidence policy and historical immutable decisions.
- Test and implement distinction between rework and rejection, distinct-case counting, cooldown expiry and infrastructure neutrality.
- Revalidate queued decisions at launch without turning a queue wait into a new observation/opportunity.

### Task 3: Runtime/configuration integration (coordinator)

- Add failing settings/CLI tests for max_workers default, precedence, validation and persistence.
- Integrate reviewed queue module with both runtime entry paths, total timeout accounting, policy/HEAD checks and visible waiting notices.
- Cover disabled/revoked/stale queued jobs and more-than-limit worker progress; preserve cancellation and failed-workspace semantics.

### Task 4: User-visible behavior and branch integration (coordinator)

- Preserve and integrate installation-product changes onto lifecycle fixes; reconcile overlapping README and hook tests.
- Update coordinator guidance to actively decompose and delegate, avoid duplicate investigations, and report accepted work/checks/rework without invented economics.
- Update English/Russian routing and onboarding documentation and 0.8.0 release notes.
- Preserve unique historical planning documents once; archive pre-existing hybrid dirty state in Git history without reverting newer code.

### Task 5: Verify, review and publish (coordinator plus native review)

- Run PYTHONPATH=src python3 -m unittest discover -s tests -v; inspect skips/failures.
- Build wheel/sdist and execute applicable installation lifecycle checks.
- Request independent review of the combined change and fix substantiated issues with regression checks.
- Commit and merge all preserved branch tips; push main without force; verify remote SHA and CI where available.
- Remove only branches whose work is preserved, then leave the root checkout on main with only main local/remote heads.

## Progress

- Distribution registered; existing-work review complete. Baseline suite running.
- DeepSeek assignment as-b7ed4340b634c10a owns worker-capacity in workspace 36a2f10cea854f5e92718b933085a3a2. Initial readiness check stopped before execution due command parsing; corrected check to env PYTHONPATH=src and launched the inspected unchanged workspace.
