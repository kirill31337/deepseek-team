# Versioned overhead contract

Contract v1 is introduced in package 0.10.0. Current planned records use schema 2;
old planned formats are diagnosed and preserved without migration.

DeepSeek Team ships one versioned minimum ruleset in
`src/codex_deepseek_team/data/overhead-contract.md`. Full and linked generated
instruction blocks, `config show --effective --instructions`, SessionStart and every
UserPromptSubmit expose that current section. Upgrading the package and rerunning
setup, or the next lifecycle event, refreshes a stale owned block. The package never
edits the human-maintained `docs/agents/delegation.md` and preserves text outside its
own block, line endings, file modes, on/off/access/effort settings and native hook
trust.

Fresh setup installs user-level integration; init attaches a project. Installing a
wheel alone does not attach projects or establish native hook trust. Live delivery
requires enabled delegation and delivered hooks.

This guide separates what the software guarantees from what an agent should do.
Neither category makes arbitrary shell semantics universally provable.

## Software-enforced guarantees

### Deterministic check reuse

- A deliverable's `checks_reusable` field is an optional strict boolean and defaults
  to `false`. Only an explicit `true` attests that every command is a deterministic,
  project-local check.
- The attestation means the check set has no network, clock, random, provider,
  production, mutable external service, runtime-home or temporary-directory
  dependency. The package does not claim to discover every dependency of an
  arbitrary shell command. Without the attestation, or when an input is unknown,
  the command executes.
- A reusable set requires unchanged ordered commands, timeout, runtime and access
  policy, executable/toolchain identity, prepared declarations, ordinary project
  files (including ignored and untracked inputs), modes and sanitized environment.
  Credential-like files, links, racy or bounded-out inputs disable reuse. Complete
  exit-zero evidence and matching before/after input snapshots are required.
- The key hashes direct command executables and the shell, not every transitive
  library or OS package. Attestation must account for dependencies outside the
  represented inputs. If those may have changed, withhold attestation or use force.
  Reuse is private to one locked workspace, not a global green-result cache.
- Changed commands, source, tools, environment or prepared inputs, force, missing,
  altered, truncated or failed evidence, and files changing during checks force a
  fresh run. Readiness, security and containment probes always execute before any
  project check can be reused.
- `workspace verify --reuse-checks` requires the same explicit attestation.
  `workspace verify --force` and managed worker `--force-checks` always execute.
  Reused results cite their original evidence and are marked as reused. Final
  coordinator acceptance is never inferred from a reused worker check alone.

Changed redactions require fresh publication when retained output would expose a
newly sensitive value. Already-safe output can be reused. Historical diagnostic
files remain immutable; the current publication is checked against current values.

### Preparation-failure memory

- Only well-identified pre-provider environmental failures are remembered: runtime
  capability or availability, declared dependencies, declared smoke probes and
  validated materialization sources.
- The private record is bounded and keyed by canonical project plus relevant
  conditions, not by task or workspace id. An unchanged condition reuses the
  original diagnostic without repeated preparation, an additional copy or a provider
  request. Fresh explicitly prepared copies participate using their actual tree and
  tool declarations; the guard does not undo allocation already requested by a caller.
- Suppression requires a retained valid record and a known bounded fingerprint.
  Unknown inputs, including unresolved relative dependencies, are uncached; record
  storage is finite. Indirect environmental changes may require explicit recheck.
- Changed relevant conditions permit a new attempt. `--recheck-environment` on a
  worker or declarative `workspace prepare` explicitly requests one fresh diagnostic
  observation; it does not bypass sandbox, permission, admission or recovery gates.
- Provider/authentication errors, ordinary project-test failures,
  permission/admission changes, cancellation and unknown faults are not cached as
  environmental.

### Logical packages and plans

- Re-registering an identical normalized distribution is a true no-op. It preserves
  deliverable ids, routing decisions, phase, current outcomes and timestamps.
- Equality covers scope, criteria, checks, dependencies, routing features and reuse
  declarations. A confirmed `checks_reusable` declaration is part of the immutable
  definition. Started scope remains immutable; newly discovered work gets a new
  deliverable id where valid.

### Correction and workspace continuity

- A newly planned correction of a reviewed `needs-rework` assignment declares
  `correction_of` with the original `task_id` and `assignment_id` in its deliverable
  JSON. Before allocating or preparing a new copy, the runner resolves the original
  assignment's authoritative saved workspace. `--workspace` is optional; a conflicting
  explicit value fails. Missing, ambiguous, stale, foreign, finalized or otherwise
  unsafe predecessor or copy state blocks safely instead of falling back to a new
  environment. Deliberately independent work may still start a new job, and a
  finalized job needs deliberately planned new work.
  The omitted-link guard checks overlapping known `needs-rework` scopes within the
  current coordination task at planning and launch, including unavailable copies.
  It cannot infer that an unrelated cross-task request is a correction; supply
  `correction_of` explicitly for that relationship.
- A succeeded result that needs corrections is marked `needs-rework`, then corrected
  through an explicitly planned `correction_of` assignment launched normally with
  `worker --coord-task ... --coord-assignment ...`; no one has to remember or pass
  `--workspace`. A failed attempt of the same assignment continues only with explicit
  `--resume-after-failure` and cannot switch copies. The stable workspace, `job_id`,
  project files and prepared tools are reused; the process, HOME, sandbox and
  `attempt_id` are new, and mandatory security and readiness checks are not bypassed.
- Both runtimes' SessionStart compact and UserPromptSubmit summaries show durable
  `workspace_id`, `job_id`, `attempt_id` and any correction reference from the private
  ledger, independent of model recall. Unstructured semantic intent, a host that does
  not deliver a hook event, and automatic agent obedience remain instruction limits,
  not universal program enforcement.

### Waiting and hook boundaries

- Worker-slot waits use kernel notifications where available with a conservative
  fallback, preserve FIFO/deadline/cancellation behavior, and revalidate before
  admission. Process exit wakes waiters.
- When delegation is enabled, lifecycle hooks deliver the contract on SessionStart
  and every UserPromptSubmit.
  Codex and Claude hook coverage is verified separately and is not universal: hooks
  enforce recognized events the runtime delivers, not every possible write. Where a
  runtime cannot intercept an action, the written instructions remain authoritative.
- The mandatory Linux worker sandbox, provider secrecy, permissions, retained jobs
  and explicit recovery rules are unchanged.

## Agent recommendations

These are useful operating rules, not claims of semantic enforcement.

- Batch independent eligible scopes into one logical package and reuse immutable
  started work. Revise for changed scope, requirements or dependencies. Software
  cannot prove semantic package boundaries or prevent invented new ids.
- Wait for completion with a blocking or event wait. Inspect status on a real failure,
  stall or cancellation signal, not through frequent polling. The software can make
  each wait efficient, but it cannot prove that an agent did not poll elsewhere.
- Review the actual diff, declared evidence and critical affected paths. Investigate
  further when evidence is missing, contradictory, failed or security-sensitive.
  Do not repeat the worker's full investigation or rewrite correct code.
- Hooks can reject conflicting recognized writes; they cannot prove that prose
  research, reasoning or semantic review was not duplicated.

## Related interfaces

The names above are public: `checks_reusable`, `workspace verify --reuse-checks`,
`workspace verify --force`, managed worker `--force-checks`, and
`--recheck-environment` on worker or declarative `workspace prepare`.

## Targeted verification

`tests.test_plan_reuse`, `tests.test_check_reuse`, `tests.test_preparation_guard`,
`tests.test_wait_notifications` and `tests.test_overhead_contract` exercise real
command counters, public runner paths, multiprocess waits and setup/init/hook output.
`tests.test_project_linked` covers the human guide. Run the full regression with
`PYTHONPATH=src python3 -m unittest discover -s tests -v`; verify a built wheel with
`python3 scripts/check_installation.py --installer pip --wheel PATH`.
