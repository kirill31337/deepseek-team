# Overhead Contract Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans for the coordinator's integration. DeepSeek leaf workers follow their registered bounded assignment and must not delegate, stage or commit.

**Goal:** Ship and test the five overhead controls without weakening evidence or isolation.

**Architecture:** A workspace-local positive check cache and a project-condition
negative preparation guard serve separate purposes. Idempotent distributions and
notification waits reduce coordination overhead. One packaged versioned rule set
is rendered into both guidance modes and live hook/policy output.

**Tech Stack:** Python 3.11+, standard library, Git, required Linux Bubblewrap.

**Spec:** `docs/superpowers/specs/2026-10-21-overhead-contract-design.md`

## Global Constraints

- No new dependencies, commits, staging, publishing or sandbox weakening.
- Preserve credentials, user guide, settings, retained work and evidence history.
- Old state/API compatibility and migrations are out of scope by explicit user request.
- DeepSeek Flash workers use owned managed copies, runtime codex, explicit effort.
- Unknown identity means execute; only explicitly deterministic checks may reuse.
- Existing immutable started definitions and final coordinator review remain.
- Use `PYTHONPATH=src python3 -m unittest discover -s tests -v` for final regression.

## Review Focus

1. Ignored or credential-like files and changing inputs must not yield false cache hits (Task 1).
2. Evidence tampering and changed tools/environment require fresh execution (Task 1).
3. New tasks sharing an unchanged failure must not repeat preparation or expose credentials (Task 2).
4. Descriptor-close storms, lost wakeups and worker death must preserve efficient FIFO waits (Task 3).
5. Linked upgrades must expose current minimum rules without touching the human guide or external homes (Task 4).

## Task 1: Check reuse — DeepSeek, effort max

**Files:** `check_reuse.py`, managed/workspace runner and CLI integration;
`tests/test_check_reuse.py`, relevant existing verification tests.

**Interfaces:** `run_checks(copy, layout, env, commands, *, timeout=120,
deadline=None, directory, phase, reusable=False, force=False, redact_values=())`
returns existing check rows plus explicit reuse provenance. The module computes
its own conservative identity and stores successful sets in locked workspace
metadata. It consumes `coord_item.get('checks_reusable', False)` (Task 3).
`workspace.verify_checks` gains `reuse_checks=False, force=False`; CLI exposes
`--reuse-checks` and `--force`. Managed force is exposed as `--force-checks`.

- [x] Add real-command counter tests for hit, no attestation, invalidation, evidence failure and force; observe failures before implementing.
- [x] Implement bounded identity, retained evidence validation and positive-only publication; integrate both runner paths.
- [x] Run new tests and existing verification/managed preparation tests; report red/green evidence and actual diff.

## Task 2: Preparation failure guard — DeepSeek, effort max

**Files:** `preparation_guard.py`, managed preflight and declarative toolchain
preparation, worker/prepare CLI; `tests/test_preparation_guard.py` and ledger tests.

**Interfaces:** self-contained module with condition fingerprint and guarded
failure record functions; no dependency on Task 1's private cache format. Integrate
before workspace allocation/provider access. Add `--recheck-environment` to worker
and declarative prepare; pass through without bypassing recovery/admission.

- [x] Add real bounded failure fixtures for repeated unchanged conditions, changed runtime/tool/recipe and explicit diagnostic retry; observe red.
- [x] Implement private stable locking, atomic bounded records, eligible failure allowlist and runner/prepare integration.
- [x] Test concurrency, unsafe paths, no false classification of ordinary checks or provider failure, and preservation of original evidence.
- [x] Run new and existing preparation ledger tests; report red/green and actual delta.

## Task 3: Logical plans and efficient waits — DeepSeek, effort max

**Files:** `coordination.py`, `worker_slots.py`, optional `slot_notifications.py`;
`tests/test_plan_reuse.py`, `tests/test_wait_notifications.py`, existing slot tests.

**Interfaces:** `checks_reusable` optional strict bool (false by default) in
normalized/immutable deliverables. Existing `plan_task` API returns unchanged task
for an identical logical distribution without routing/write churn. Slot acquire
retains its existing signature/FD ownership contract.

Use the current normalized plan format and its crash recovery. Remove newly
introduced historical binding adapters and assumptions about old routing commits;
unsupported old planned state may be diagnosed without rewriting or migrating it.

- [x] Add tests for same-plan byte/timestamp/outcome/assignment identity preservation, real revisions and immutable started reuse declarations; observe red.
- [x] Implement normalization/no-op comparison before routing side effects.
- [x] Add real multiprocess notification/release/death/FIFO/deadline/fallback tests, then replace rapid polling with safe notification waits.
- [x] Run new tests and existing slot/auto distribution tests; report real evidence.

## Task 4: Shipped rules and upgrade delivery — DeepSeek, effort high

**Files:** packaged contract/module, settings/project/hooks/templates, installation
resource checker, target tests, bilingual overhead docs and README links.

**Interfaces:** `overhead_contract.instructions() -> str` returns versioned current
packaged rules. Include once in full/linked rendering and live policy/context;
unmodified prompt events must also provide the current rules. Do not edit the
human guide. Document Task 1/2/3's fixed public names from this plan.

- [x] Add consumer-visible setup/init/policy/hook tests for full and linked, stale package rules, repeated no-op refresh, file mode and outside decoys; observe red.
- [x] Implement one authoritative versioned rule source, generated delivery and installed resource checks.
- [x] Document program guarantees, opt-in/force/diagnostic boundaries and agent recommendations separately in both languages.
- [x] Run new and existing linked/bootstrap tests; report red/green and actual delta.

## Task 5: Coordinator integration and final verification

Execution note: the installed ownership hook also reserves the scope of a merely
planned read-only reviewer. Register the final reviewer only when the integrated
source is ready; retaining that unstarted broad scope during integration would
block the authorized writes. No review was launched or discarded. Keep the same
logical coordination task and record the review before final acceptance.

- [x] Review actual diffs and declared checks; merge overlapping managed/CLI edits, importing only needed current source and matching tests into workers when required.
- [x] Record accepted/rework outcomes without duplicating live worker investigation; do bounded corrections locally only with concrete evidence.
- [x] Run targeted combined integration tests and the full required suite in isolated runtime homes/state.
- [x] Build a wheel from the current uncommitted package snapshot outside the project; verify installed resources and fresh/full/linked upgrade preservation, without provider requests.
- [x] Record current coordinator outcomes, finalize accepted jobs and complete the one coordination task. Report guarantees, limitations, tests and any rework.

## Integration evidence

Package version is 0.10.0 and the packaged instruction contract is v1. The source
baseline is e4db0809c4a518093e053dc783d9ce8521dc5994 (v0.9.0). No old-plan migrations
or newly introduced historical binding adapters remain.

Coordinator integration corrected reuse provenance publication and failed-deadline
invalidation. Independent review then reproduced a missing terminal-result
validator and newly sensitive cached output. Focused failing tests preceded the
bounded corrections. Current retained output is checked against current redactions;
already-safe baseline evidence remains reusable without reading provider secrets
early. Terminal reuse-bearing rows are validated before any ledger mutation.

The initial full regression exposed one outdated deadline-test spy. The worker
adapted only its signature/forwarding; host verification with the real required
sandbox passed, and the corrected candidate passed 1746 tests with five expected
skips. After the review corrections, 81 focused tests passed; the final full run
also includes a cached-stderr privacy regression. Final wheel installation,
replacement/removal, six full/linked fresh/update scenarios and six installed-code
functional checks have passed. The final current-source full run passed 1751 tests with five expected skips
(three missing-Claude-CLI checks, optional GC stress and an absent nested-mount
scenario). The final wheel matches every current package file byte-for-byte.
Previously observed nested-worker namespace failures did not cause sandbox
weakening or repeated dependency preparation. A provider-free GC dry-run reclaimed
nothing; finalization is limited to the reviewed jobs and retains their evidence.
