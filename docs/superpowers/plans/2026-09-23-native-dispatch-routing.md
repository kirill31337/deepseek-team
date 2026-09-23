# Native dispatch routing implementation plan

> **For agentic workers:** execute the assigned isolated slice; the coordinator integrates and publishes.

**Goal:** Route delegated research and implementation through DeepSeek before a native agent can replace it.

**Architecture:** Require an evidenced native exception in the existing coordination ledger. Check native launch and work-message events in the parent hook before source-mutation classification. Bind each allowed call to `[deepseek-team:TASK_ID:DELIVERABLE_ID]`; preserve direct coordinator answers, disabled delegation, access restrictions, and coordinator-only responsibilities.

**Tech stack:** Python 3.11+, unittest, existing Codex/Claude hooks and sandboxed DeepSeek workers.

**Spec:** User-approved four-point design in this conversation: routing before delegation, constrained native exceptions, dispatch-time enforcement, and regression coverage.

## Constraints and verification focus

- Workers never commit or publish; root owns security decisions, review, integration, release and final verification.
- Parallel user changes introducing 0.8.2 effort levels are preserved. This release is 0.8.3.
- Native exceptions are explicit user request or a specific unavailable native capability, with evidence. They cannot transfer protected duties or widen read-only access.
- Reject missing/foreign/ambiguous markers, stale outcomes, forged request reuse, and changed exceptions after dispatch.
- Empty control calls and simple coordinator-only status answers do not create work.
- Verify actual runtime event delivery independently of handler unit tests. Do not claim coverage where delivery is absent.

## Tasks

- [x] DeepSeek: add native exception validation and locked dispatch authorization in `coordination.py` and `native_delegation.py`; test before implementation, preserve ordinary routing bindings. Accepted with coordinator rework recorded in the ledger.
- [x] DeepSeek: update generated/packaged guidance, bilingual docs, release notes and version metadata; preserve low/high/max effort changes.
- [x] Coordinator: first reproduce native dispatch bypass in `test_coordinator_dispatch.py`, then intercept launch and work-message events before mutation detection and expand both runtime matchers. Independent DeepSeek review findings verified and corrected.
- [x] Coordinator: extend real Codex and Claude offline-provider fixtures to prove native launches reach PreToolUse and are denied.
- [x] Coordinator: inspect actual worker patches, integrate on the isolated branch, run the full suite (660 tests, no skips), build wheel/sdist, strict metadata checks and pip/pipx/uv lifecycle checks.

Release procedure: publish only verified changes to main, then the version tag and PyPI;
verify GitHub workflows and released artifact hashes. Publication outcomes are recorded
in the persistent coordination ledger and GitHub release, after this source commit.
The primary checkout contains unrelated in-progress changes, so publish main from the
isolated worktree without resetting, stashing or rewriting that checkout.

No additional design approval is required: the user explicitly authorized implementing the proposed changes and publishing to main and PyPI.
