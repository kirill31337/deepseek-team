# Delegation enforcement implementation plan

> Execution: coordinator integration with bounded DeepSeek implementation jobs,
> using the project coordination ledger and isolated managed copies.

**Goal:** enforce early useful delegation and narrow protected source authority.

**Architecture:** preserve the router's safety admission; enforce distribution in
the ledger, exploration/mutation boundaries in shared runtime hooks, and explain
the resulting workflow through CLI diagnostics and generated guidance.

**Tech stack:** Python 3.11+, standard library, unittest, existing Bubblewrap runner.

**Spec:** [2026-09-24-delegation-enforcement.md](../specs/2026-09-24-delegation-enforcement.md)

## Constraints and review focus

- Existing preparation/recovery changes must survive selected imports/integration.
- Disabled delegation, manual/small/native exceptions retain intended behavior.
- Exact file permissions cannot become directory, glob or symlink-escape authority.
- Forged or legacy ledger metadata must fail safely without duplicating live work.
- Integration references never grant authority from a review or a running worker.
- Source inspection tracking must not turn status/bootstrap calls into fake work.

## 1. Auto distribution and diagnostics — DeepSeek

Files: `coordination.py`, `coordination_cli.py`, `test_auto_distribution.py`,
`test_routing_integration.py`.

- [x] Reproduce explicit coordinator/worker bypasses under substantial Auto.
- [x] Require `auto`, preserve explicit allowed exceptions and saved decisions.
- [x] Validate and freeze `integration_of`, `write_scope`, `decision_artifacts`.
- [x] Expose requested/resolved routing and actionable next-step explanations.
- [x] Run both focused test modules and review the actual worker delta.

## 2. Early planning and protected mutation — DeepSeek

Files: `coordinator_hooks.py`, new `coordinator_activity.py`,
`test_early_planning.py`, `test_protected_mutations.py`.

- [x] Reproduce unrecorded investigation and broad protected-scope source writes.
- [x] Classify supported inspections and preserve status/bootstrap/worker exemptions.
- [x] Record the first inspection; require a plan for further inspection and closure.
- [x] Implement runtime protected authority using the metadata from the spec.
- [x] Test canonical exact paths, malformed fields, worker output provenance,
  accepted coordinator changes, directory deletion and pending-worker precedence.
- [x] Run both focused modules and review the actual worker delta.

Interfaces use existing coordination event/ledger APIs. Hook helpers must not
depend on an unavailable parallel worker API or stub missing functionality.

## 3. Guidance and access visibility — DeepSeek

Files: `settings.py`, `onboarding.py`, `data/delegation.md`, both READMEs,
`test_settings.py`, `test_onboarding.py`.

- [x] Add regression tests for access visibility and generated workflow guidance.
- [x] Explain early decomposition, mandatory Auto and explicit integration fields.
- [x] Print read-only default and explicit full-access opt-in during setup.
- [x] Clarify subjective reporting and instruction refresh without new telemetry.
- [x] Preserve imported README additions and run both focused test modules.

Coordinator review corrected the credential wording and setup command order;
26 focused tests pass in the integrated checkout. The independent Claude matcher
slice is also integrated with five passing installation/repair tests.

## 4. Installed hook coverage — DeepSeek

Files: `config.py`, `test_config.py`, `test_codex_coordination_integration.py`.
The independent Claude registration slice owns `claude_config.py` and
`test_claude_registration.py` and applies the same installed-matcher checks.

- [x] Extend managed Codex and Claude matchers to inspection tools and supported aliases.
- [x] Test idempotent repair of obsolete managed matchers and preservation of user hooks.
- [x] Add an offline real-Codex scenario proving inspection events reach the hook.
- [x] Run configuration tests in isolation; run the new lifecycle scenario after
  integration with the early-planning worker's implementation.

## 5. Integration and final verification — coordinator

- [x] Register distribution; prepare three existing-style owned isolated copies.
- [x] Import only needed changed source/README files and retain a private baseline.
- [x] Review outputs and actual deltas; integrate without overwriting user changes.
- [x] Adapt relevant existing fixtures for deliberate behavior changes.
- [x] Run targeted cross-runtime and adversarial regression checks.
- [x] Run `PYTHONPATH=src python3 -m unittest discover -s tests -v`.
- [x] Refresh attached project guidance using the verified local package if needed.
- [x] Record worker dispositions and accepted coordinator outcome; report scope,
  limitations, tests and any rework. Do not commit or publish.


Integration review tightened several worker results before acceptance:

- Authentic started legacy explicit-executor plans retain their original immutable
  binding; unstarted bypasses still require Auto. Coordinator mutations now freeze
  their deliverable identity as well.
- Every integration file must be backed by an actual referenced file change;
  qualified mutation names, root/default-directory searches and malformed metadata
  have regression coverage. Parent-directory deletion remains forbidden.
- The real Codex inspection scenario now fails if enforcement is absent instead of
  silently skipping. The full first integration run exercised this successfully.
- Guidance distinguishes newly discovered scope from corrections to reviewed output;
  the latter are reported as coordinator rework. Generated AGENTS instructions were
  refreshed while preserving the handwritten prefix and local settings.

The delegated compatibility slice updated the old lifecycle fixtures and bilingual
coverage documentation. The independent review and test-only timing correction were
also integrated: real preparation had consumed most of the old two-second budget
before the declared checks. The corrected host test reaches real checks and verifies
the original shared absolute deadline. A final full-suite run follows review fixes.


Independent review delivered three reproducible findings after an explicitly
resumed provider interruption. All were accepted for correction: legacy explicit
plans must validate their original binding without requiring an historical Auto
router mode; Git global options and recursive grep must reach the inspection gate;
protected file authority must reject hard-link aliases. Regression probes failed
before the fixes. No worker source changes were made by the reviewer.


Final host verification: `PYTHONPATH=src python3 -m unittest discover -s tests -v`
completed with **903 tests, OK (skipped=3)** in 377.050 seconds. All skips require
the unavailable Claude CLI. Offline real-Codex hook scenarios and real Linux OS
isolation/check-timeout tests passed. `git diff --check` passed. The first full
integration run exposed deliberate fixture changes and the timing premise; an
intermediate run was stopped after independent-review changes, then the complete
suite was restarted on final source. No package installation, commit or publishing
was performed. Existing preparation/recovery changes and local settings were preserved.
