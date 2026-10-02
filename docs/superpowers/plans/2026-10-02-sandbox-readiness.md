# Sandbox readiness implementation plan

> **For agentic workers:** Follow the task contracts below with test-first
> implementation. DeepSeek Team routing and private coordination state own
> assignment lifecycle, review and recovery. Workers never stage or commit.

**Goal:** Prevent missing runtime/toolchain dependencies before model execution
and retain reusable evidence when project checks fail.

**Architecture:** Four independent stdlib modules implement runtime selection,
private toolchain recipes, bounded check evidence and prepared-input fingerprints.
The coordinator integrates these into the existing sparse managed runner and
ledger, preserving security boundaries and old records.

**Tech stack:** Python 3.11+, unittest, Git, Bubblewrap, AppArmor, stdlib only.

**Spec:** `docs/superpowers/specs/2026-10-02-sandbox-readiness.md`

## Global constraints

- Follow every constraint and exact API in the spec.
- Runtime probes and toolchain smoke probes precede real provider credentials.
- No broad host mounts, general worker networking, secret/config export or retries.
- Worker changes are confined to their named module/test files in owned copies.
- Preserve one assignment/history across context versions and verification reruns.
- Coordinator integration stays in the current clean source checkout; reuse worker
  private copies rather than creating synthetic project branches or commits.

## Review focus

- Host-successful HOME wrapper with missing sibling target: namespace failure or
  verified default fallback, never a paid late launch failure (Task 1, Task 5).
- Symlink chain/cycle from selected SDK/JDK into unapproved host data: reject
  before publication; approved Java configuration materializes safely (Task 2).
- Secret straddling an output chunk/limit and a noisy timed-out child: bounded
  redacted evidence and process cleanup (Task 3).
- Imported ignored file changes/deletion/symlink escape: private fingerprint
  evidence without config contents in source patches (Task 4).
- Originally red checks and successful re-verification: baseline and old worker
  outcome preserved; no provider read/model call or extra quality case (Task 5).

## Task 1 Runtime selection and namespace readiness

**Files:** `src/codex_deepseek_team/runtime_preflight.py`,
`tests/test_runtime_preflight.py`.

**Produces:** `RuntimeSelection`, `RuntimePreflightError`, `probe_runtime` and
`select_runtime` exactly as specified. Consumes the existing capability rules and
the caller's `(binary, runtime) -> (layout, environment)` factory.

- [ ] Write failing behavioral tests for namespace-unrunnable wrappers, explicit
  pin preservation, default PATH/system candidate ordering and capabilities.
- [ ] Observe RED; implement the module without changing shared runner files.
- [ ] Run `PYTHONPATH=src python3 -m unittest discover -s tests -p 'test_runtime_preflight.py' -v`.
  Expected: focused tests pass; real nested-sandbox limitations reported explicitly.
- [ ] Report actual diff, RED/GREEN evidence and full-suite outcome; no commits.

## Task 2 Reusable toolchain and environment preparation

**Files:** `src/codex_deepseek_team/toolchains.py`, `tests/test_toolchains.py`.

**Produces:** `ToolchainError`, `prepare`, `apply_saved_recipe`, `environment_for`,
`probes_for`, `read_only_roots`. Consumes existing `Workspace` lock/save/finish APIs.

- [ ] Write failing tests for safe software/copy preparation, workspace-variable
  replay in a fresh copy, allowed Java configuration links, escaping links/cycles,
  forbidden roots/credential-like inputs, invalid env and atomic recipe writes.
- [ ] Observe RED; implement private version-1 metadata/recipes and narrow roots.
- [ ] Run `PYTHONPATH=src python3 -m unittest discover -s tests -p 'test_toolchains.py' -v`.
  Expected: focused tests pass, source and runtime homes remain untouched.
- [ ] Report diff and checks; never edit shared CLI/runner files or commit.

## Task 3 Bounded diagnostic evidence and causes

**Files:** `src/codex_deepseek_team/check_evidence.py`,
`tests/test_check_evidence.py`.

**Produces:** `run_checks` and `classify_checks` exactly as specified. Consumes an
already built namespace argv/environment, no provider/auth code.

- [ ] Write failing tests for baseline/post phase isolation, 65,536-byte limits,
  known-value redaction across chunk/limit boundaries, timeout process cleanup,
  failed launch, atomic owner-only evidence and legacy row compatibility.
- [ ] Observe RED; implement streaming capture and evidence classification.
- [ ] Run `PYTHONPATH=src python3 -m unittest discover -s tests -p 'test_check_evidence.py' -v`.
  Expected: real disposable command checks pass with no secret publication.
- [ ] Report checks/diff, including full-suite environment failures; no commits.

## Task 4 Explicit prepared ignored inputs

**Files:** `src/codex_deepseek_team/prepared_inputs.py`,
`tests/test_prepared_inputs.py`.

**Produces:** `input_snapshot` and `input_changes`. Consumes only explicit
`prepared_paths`, safe Workspace paths and existing credential-name rules.

- [ ] Write failing tests for ignored imported files, changes/deletion/mode,
  malformed/credential/escaping registrations and unregistered ignored files.
- [ ] Observe RED; implement private hashes and change names, never raw config diff.
- [ ] Run `PYTHONPATH=src python3 -m unittest discover -s tests -p 'test_prepared_inputs.py' -v`.
  Expected: focused behavioral tests pass.
- [ ] Report checks/diff; no shared workspace/ledger edits or commits.

## Task 5 Coordinator runner and ledger integration

**Files:** `managed.py`, `development.py`, `worker.py`, `doctor.py`, `workspace.py`,
`delegation_cli.py`, `coordination.py`, `verification.py` in the package; their
registered existing test files and `tests/test_sandbox_readiness_integration.py`.

**Consumes:** All four accepted APIs. **Produces:** prepare flags, offline namespace
doctor, baseline/probe/post evidence, `workspace verify`, optional ledger metadata
and neutral automatic verification feedback.

- [ ] Review every actual foundation diff and focused result; integrate only
  accepted module/test changes and record dispositions with explicit quality.
- [ ] Write and observe failing integration tests for pre-key ordering, explicit
  pins, Java/environment preparation, baseline-red work, ignored fingerprints,
  verify-only recovery and original assignment/quality preservation.
- [ ] Wire the modules through the current runner/CLI; preserve old command forms
  and records. Add no automatic implementation retries or host access.
- [ ] Exercise real Bubblewrap reproductions from the incident analysis.
- [ ] Run `PYTHONPATH=src python3 -m unittest discover -s tests -v`.
  Expected: complete suite passes; report every unavailable-runtime skip.
- [ ] Review privacy/security and record coordinator implementation acceptance.

## Task 6 Documentation and installable package

**Files:** Registered English/Russian READMEs, hardening/verification/toolchain
guides, `docs/releases/0.8.9.md`, packaged delegation guidance, version metadata.

**Consumes:** Final implemented prepare/verify behavior and evidence semantics.

- [ ] Give the documentation worker matching current source and tests through
  explicit workspace imports, then describe only implemented behavior.
- [ ] Review examples against actual CLI help and focused executable checks.
- [ ] Build/inspect the installable 0.8.9 package and verify fresh installation
  in isolated homes/state, retaining user settings and credentials.
- [ ] Record accepted documentation, integration and final verification outcomes.

## Progress and decisions

- Completed 0.8.9 integration: all four foundation APIs, namespace probes, narrow
  recipe preparation, baseline/post evidence, ignored-input hashes and provider-free
  verification are integrated. Existing fixtures and bilingual documentation are
  updated; no project branch, commit, publication or sandbox relaxation was needed.
- Coordinator full verification: 1344 tests in 397.095 seconds, OK with 3 Claude
  unavailability skips. All 26 new incident integrations passed, including real
  Java security/truststore, writable cache and read-only tooling checks.
- Built wheel/sdist and installed the wheel offline in an isolated virtualenv/home;
  version/help and real full-access offline Codex doctor passed. All 48 packaged
  files matched source, private files were absent, AppArmor restriction remained 1.
- Both archives passed strict Twine metadata checks; pip, pipx and uv passed the
  repository's install/replacement/uninstall lifecycle with configuration sentinels
  preserved. Tested artifacts and SHA256SUMS are retained under `dist/0.8.9/`.
- Recorded rework: the original toolchain implementation required a substantive
  DeepSeek correction for credential aliases and destination-parent ordering;
  coordinator fixed a runtime fixture, one total-deadline classification edge and
  bounded documentation examples. Runtime hardening did not follow the requested
  test-first order; that process gap remains in its explicit minor-gap assessment.
- Further coordinator regressions protect early assignment linking, readiness
  history, typed verification evidence, explicit versus default runtime selection,
  active-copy exclusion and interrupted publication into the original attempt.
- Fixed a reproduced existing Git-index refresh problem on unchanged imported
  files without accepting tampered metadata or weakening the read-only Git mount.
- Historical task checklists above record the original brief; this completion
  evidence and the private reviewed ledger record the actual accepted result.

- Planning: prior analysis reproduced runtime and Java failures in real Bubblewrap;
  unchanged 0.8.7/0.8.8 modules and the 1165-test baseline were verified.
- Ruling: use the user's explicit implementation approval as the execution handoff;
  do not ask again for approval of the already accepted remediation.
- Ruling: ordinary project-test failures remain neutral until review; baseline
  exit-code equality alone does not prove environment or worker attribution.
- Ruling: saved recipes never contain or auto-run arbitrary host preparation
  commands. Only explicit artifact/env declarations replay into a new owned copy.
