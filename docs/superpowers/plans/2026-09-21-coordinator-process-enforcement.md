# Coordinator Process Enforcement Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make delegation policy observable and enforceable in new Codex sessions while preserving access independence, sandboxing, and legacy behavior.

**Architecture:** Add a minimal persistent coordination ledger outside the repository, wire it into Codex's supported lifecycle hooks, and connect managed worker start/finish to assignments. Codex hooks gate coordinator mutations until a valid distribution exists, restore state after compaction, and prevent task completion while worker results are undispositioned. Claude remains instruction-driven unless equivalent supported hooks are verified.

**Tech Stack:** Python 3.11+, JSON/atomic files/flock, Codex project hooks, existing Bubblewrap/AppArmor workspace runner.

**Spec:** user request in conversation dated 2026-09-21.

## Global Constraints
- Never increase the user's selected delegation level or access.
- Preserve legacy `--write --allow-write`.
- Keep OS sandbox mandatory for managed access.
- No automatic commit/push/deploy or production/secret access by workers.
- No arbitrary useful-work percentage metric.
- Do not scan arbitrary projects; hooks are installed only by explicit project attach.
- Keep state minimal and reuse workspace records rather than building a scheduler.

## Review Focus
- Hook trust/unavailable hooks must degrade transparently, not masquerade as enforcement.
- 75/full-access token delegation must fail when eligible tests/fixtures/docs are retained without an accepted constraint.
- Compaction must restore worker review results and dispositions.
- Coordinator-prepared source must not be attributed to worker changes.
- Missing dependencies must fail before provider execution and never be replaced by stubs.

---

### Task 1: Persistent coordination state and distribution validation
- [ ] Add failing state/validation tests.
- [ ] Implement atomic project/session/task records and profile-specific validation.
- [ ] Verify focused and full suites.

### Task 2: Codex lifecycle hooks
- [ ] Add failing hook/install tests.
- [ ] Implement SessionStart/UserPromptSubmit/PreToolUse/PostToolUse/Stop adapter.
- [ ] Manage package-owned entries in project .codex/hooks.json without changing user entries.
- [ ] Verify compaction restoration and mutation gates.

### Task 3: Runner registration, readiness and attribution
- [ ] Add runner tests for assignment start/end, selected source preparation, dependencies and checks.
- [ ] Connect managed runner to task/assignment state and workspace snapshots.
- [ ] Fail preparation before provider access when declared requirements are absent.

### Task 4: CLI and integration
- [ ] Add coordination CLI for plan/status/use and machine-readable output.
- [ ] Update managed instructions to invoke the ledger workflow.
- [ ] Add real Codex 0.154.0 hook integration test with observable blocked/allowed actions.

### Task 5: Release
- [ ] Update docs/version/changelog.
- [ ] Build/install release candidate and verify clean install + update path.
- [ ] Run independent review and full CI.
- [ ] Fast-forward main, tag and publish release, then verify published artifact/install.
