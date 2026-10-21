# Shipped overhead contract

## Intent and scope

Make the five requested overhead controls a versioned package contract: reuse one
logical distribution, reuse verified deterministic checks, wait for completion,
remember unchanged preparation failures, and review a worker's evidence without
repeating its investigation. Ship current rules through full and linked guidance,
live policy and lifecycle hooks; never replace the human-maintained linked guide.
Preserve Linux isolation, provider secrecy, permissions, retained jobs and explicit
recovery. No publishing, commits or external product content are part of this task.

The user explicitly does not require compatibility with old APIs or state formats.
Prefer a correct current format and a clear unsupported-format diagnostic over
migrations or historical binding adapters. Preserve existing files, user guides
and settings; instruction refresh on package replacement remains required.

## Decisions

Use conservative, opt-in evidence reuse, rather than pretend arbitrary shell
commands have enumerable inputs. A deliverable may declare `checks_reusable: true`
only for deterministic project-local checks. It attests no network, clock, random,
provider, production, mutable external service, runtime-home or temporary-directory
content dependency. The default is false. Unknown dependencies require execution.
`workspace verify --reuse-checks` supplies the same explicit attestation;
`--force` always executes. Managed baseline/post checks honor the declaration.
Readiness/security/containment probes always execute; successful project checks
may be reused only after readiness is established for the current namespace.

The alternative of automatic reuse for arbitrary commands risks false green
results. Text-only recommendations would leave the user's requested controls
unverifiable. Do not add a broad agent rate limiter or forbid all coordinator reads:
security review and final verification still need access to actual code.

## Check evidence identity and safety

A reuse key includes a schema/contract version, exact ordered commands, timeout,
runtime and access policy, executable/toolchain identity, prepared declarations,
all ordinary project files including ignored and untracked inputs, file modes,
and meaningful sanitized environment values. Exclude Git control data as project
content but retain the verified immutable base identity. Refuse unknown snapshots,
escaping or unresolved links, special files, changing files, exceeded bounds and
unverifiable tools. Never store credentials, raw environment or raw model output.
Do not silently ignore credential-like project inputs: they make reuse ineligible.

The explicit deterministic-check attestation allows known per-run harness HOME,
control socket and temporary paths to be normalized; it never permits normalizing
arbitrary external inputs. State which normalization the implementation supports.
Only complete exit-zero check sets with unchanged before/after inputs are eligible.
Validate retained evidence paths, ownership/type and stream digests before a hit;
missing, altered, failed, truncated, timed-out or incomplete evidence is a miss.
Reused results name their original evidence and are marked as reused, not rerun.
Both terminal assignment results and appended verification runs validate reuse
provenance before publication. Revalidate retained stdout/stderr against current
redaction values on every hit. Newly unsafe evidence requires a fresh redacted
observation; already-safe evidence remains reusable and historical files stay
immutable. Redaction values are never persisted as cache metadata.
Keep history and original failure evidence. Store workspace-local cache metadata
under the existing exclusive workspace lock; no cross-workspace green cache.

Mandatory execution: force, no attestation, changed commands/timeout/source/tools/
environment/prepared inputs, unknown inputs, failed previous check, missing or
invalid evidence, and diagnostics whose purpose requires a fresh observation.
Final coordinator acceptance is never inferred from a cached worker check.

## Preparation failure memory

Persist bounded private records under the selected state root, keyed by canonical
project plus non-secret relevant conditions rather than task/workspace IDs. Check
the guard before a new copy or model is prepared. Record only well-identified
pre-provider environmental failures: runtime capability/availability, declared
dependency, declared smoke probe, and validated materialization-source failures.
Never cache provider/authentication errors, ordinary project-test failures,
admission/permission changes, cancellation, or unknown faults as environmental.

Fingerprint source baseline, runtime options/candidates and executable identities,
PATH/tool lookup, relevant platform/sandbox conditions, prepared recipe/input
declarations and available source identities. Unknown identity is not a claimed
unchanged condition. Changed relevant conditions permit a new attempt; time alone
does not. `--recheck-environment` explicitly requests one diagnostic observation
when an indirect condition changed or a fresh probe is needed. It does not bypass
the sandbox, permission, assignment or failed-workspace recovery gates.

Fresh explicit copies, including successful declarative preparation, use their
actual bounded working tree and stable declarations. Workspace IDs and timestamps
do not substitute for that identity. A supplied copy has already been allocated
by its caller; suppression avoids additional allocation and repeated preparation.

Blocking a new assignment references the original diagnostic safely and allocates
no new workspace or provider request. Preserve the original retained workspace and
evidence. Protect shared records with a stable private lock and atomic publication;
reject unsafe symlinks/ownership and never serialize secrets or arbitrary errors.
Declarative workspace preparation should use the same guard for known failures.
Legacy arbitrary host preparation commands are not silently classified as stable.

## Plans, waiting and review

An identical normalized distribution is a true no-op: retain assignment IDs,
routing decisions, task phase, current outcomes and timestamps; do not append
feedback or rewrite the ledger. Revised scope, requirements/checks or dependencies
can change a plan. Started scope remains immutable; newly discovered scope and
corrections use new deliverable IDs within the same logical package where valid.
`checks_reusable` is validated as a boolean, defaults false, and belongs to the
immutable definition. Do not force a new plan for each implementation/check step.

Queue waits use kernel notifications where available and a conservative bounded
fallback, rather than 50 ms loops. Register before probing to avoid lost wakeups;
retain FIFO, slot locks, deadline/cancellation/admission checks and descriptor
cleanup. Process exit must wake waiters. Notifications are hints: always revalidate
before admission. Do not treat each close of a waiter-owned descriptor as a reason
to spin. Agent-level waiting remains a recommendation: await completion using a
blocking/event wait, inspect status on an actual failure/stall/cancellation signal.

Review actual diff, declared evidence and critical affected paths. Do additional
investigation for missing, contradictory, failed or security-sensitive evidence.
Existing hooks prohibit conflicting recognized writes, not semantic duplication of
research; describe this boundary explicitly.

## Delivery and updates

One packaged `data/overhead-contract.md`, exposed through `overhead_contract.py`,
is the current minimum contract. Full and linked generated blocks include it;
live settings and SessionStart/UserPromptSubmit expose it too, including unchanged
blocks. Link to but never edit `docs/agents/delegation.md`. Refresh stale blocks
using existing ownership parsing and atomic update paths, preserving prefix,
suffix, line endings and file modes. A package install alone does not attach a
project or grant native hook trust. Keep user on/off/access/effort settings intact.
Document software guarantees separately from agent recommendations in English and
Russian. Package resources must be checked from a built installed wheel.

## Acceptance and boundaries

Use real command counters for reuse/invalidation, not assertions on mock calls.
Cover source, ignored input, command, timeout, environment and tool changes;
tampered/missing evidence; failures; force; no attestation; files changing during
checks; concurrency/lock cleanup; and bounded snapshots.
Prove identical failed preparation does not reallocate/reprobe/reach provider,
changed conditions and explicit diagnostics allow progress, and original evidence
survives. Cover concurrent records and unsafe cache paths.
Prove idempotent plans preserve bytes/IDs/outcomes, actual changes revise, and
started scope stays immutable. Queue tests cover release/death, lost wakeups,
FIFO, cancellation/deadlines and fallback without rapid repeated validation.
Run setup/init/bootstrap with isolated HOME/CODEX_HOME/CLAUDE_CONFIG_DIR/config/
state and outside decoys; verify current contract after full/linked upgrades with
the guide byte-identical. Run the required full unittest suite and real wheel
install/replacement checks. Report actual skips and remaining boundaries.
