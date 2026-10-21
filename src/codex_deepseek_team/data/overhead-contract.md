<!-- codex-deepseek-team:overhead-contract:v1 -->
# Versioned overhead contract (v1)

Supplements, never edits, `docs/agents/delegation.md`.

## Software-enforced guarantees
- `checks_reusable`: strict optional bool, default `false`; `true` attests deterministic project-local checks with no network, clock, random, provider, production, mutable service, runtime-home or temporary dependency. No universal proof exists for arbitrary shell dependencies; absent or unknown executes.
- Reuse requires unchanged ordered commands, timeout, runtime/access, direct executables, prepared declarations, ordinary project inputs (including ignored/untracked), modes, sanitized environment, complete exit-zero evidence safe under current redactions and matching input snapshots before/after. Credential-like files or unknown inputs disable reuse. The key is not a transitive OS/library snapshot: unrepresented environment changes require fresh checks. Force, no attestation, failure, missing/altered/truncated evidence or changing files execute. Readiness/security/containment probes always run. `workspace verify --reuse-checks` needs attestation; `--force` and managed `--force-checks` execute. Reused results cite original evidence; final acceptance never relies only on a reused check.
- Identical normalized distribution is a no-op preserving IDs, routing, phase, outcomes and timestamps. Scope, criteria, checks, dependencies, routing features and reuse declarations join that identity. Confirmed `checks_reusable` and started scope stay immutable; new scope gets new IDs.
- Bounded private records keyed by canonical project and verified conditions remember identified pre-provider runtime, dependency, probe and materialization failures. A matching retained record avoids repeated preparation, extra copy allocation and provider access, including fresh explicitly prepared copies. Unknown inputs are uncached. Changes or `--recheck-environment` allow a fresh observation without bypassing recovery/admission/sandbox. Provider/auth, project-test, permission/admission and cancellation are not remembered.
- Enabled hooks expose this at SessionStart and every UserPromptSubmit, enforce delivered events, and never weaken mandatory Linux sandbox.

### Correction and workspace continuity

- A newly planned correction of a reviewed `needs-rework` assignment declares
  `correction_of` with the original `task_id` and `assignment_id` in its deliverable
  JSON. Before allocating or preparing a new copy, the runner resolves the original
  assignment's authoritative saved workspace; `--workspace` is optional and a
  conflicting value
  fails. Missing, ambiguous, stale, foreign, finalized or otherwise unsafe
  predecessor/copy state blocks safely instead of falling back to a new environment.
  Independent work can still start a new job, and a finalized job needs deliberate new
  work.
  The omitted-link guard checks overlapping known `needs-rework` scopes within the
  current coordination task at planning and launch, including unavailable copies.
  Cross-task semantic intent requires an explicit `correction_of` reference.
- A succeeded result needing corrections is marked `needs-rework`, then corrected
  through an explicitly planned `correction_of` assignment launched normally with
  `worker --coord-task ... --coord-assignment ...`, without remembering
  `--workspace`. A failed attempt of the same assignment continues only with explicit
  `--resume-after-failure` and cannot switch copies. Stable workspace, `workspace_id`,
  `job_id`, project files and prepared tools are reused; process, HOME, sandbox and
  `attempt_id` are new, and mandatory security and readiness checks still run.
- Both runtimes' SessionStart compact and UserPromptSubmit summaries show durable
  `workspace_id`, `job_id`, `attempt_id` and correction reference from the private
  ledger, independent of model recall. Unstructured semantic intent, missing host hook
  delivery and automatic agent obedience remain instruction limits, not universal
  program enforcement.

## Agent recommendations (not universal enforcement)
- Use one logical package/plan, batching independent scopes; revise for meaningful scope, requirement or dependency changes. Reuse immutable started work; record new scope separately. Software cannot prove semantic package boundaries or prevent invented IDs.
- Use blocking/event waits; inspect status only on failure, stall or cancellation, not frequent polling.
- Review actual diff, evidence and critical paths; investigate further only for missing, contradictory, failed or security-sensitive evidence, without repeating full investigation or rewriting correct code.
