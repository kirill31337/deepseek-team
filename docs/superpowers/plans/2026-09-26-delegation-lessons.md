# Delegation Lessons Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** Record per-assignment rework evidence and use coordinator-reviewed, versioned lessons to improve later delegation.
**Architecture:** Independent private journal/rules service with immutable events and transactional reviews. Coordination captures execution snapshots and outcomes; CLI and lifecycle expose evidence and apply advisory lessons.
**Tech Stack:** Python 3.11+, standard library SQLite/unittest, existing DeepSeek Team CLI.
**Spec:** docs/superpowers/specs/2026-09-26-delegation-lessons.md

## Global Constraints

Preserve earlier uncommitted changes. Fixed DeepSeek Flash, required sandbox, explicit codex runtime. No commits/publish, raw logs/secrets in Git, implicit permissions, automatic retries. Graded routing-quality changes are explicitly authorized in the later spec extension. Existing owned worker copies provide isolation. All shared APIs are defined by the spec; coordinate interface changes before using them.

## Review Focus

- Repeated dispositions must not manufacture more cases or erase prior failures.
- Concurrent review must not acknowledge outcomes arriving after its snapshot.
- Historic assignments must not receive rules learned only after their execution.
- Missing metadata must remain unknown and must not break old state.
- Lessons cannot override off, access, effort or sandbox; empty read commands allocate no state.

### Task 1: Journal and lesson service
Files: create src/codex_deepseek_team/lessons.py (split private persistence helper if needed), tests/test_lessons.py.
Interfaces: exactly the shared Python contract in the spec.
- [x] Write failing behavior tests for append/idempotence/counting, 9/10 and 2/3 thresholds, snapshot filtering, apply/version/watermark, private state and legacy metadata.
- [x] Implement service and minimal persistence; run focused tests.
- [x] Report actual files/diff, RED/GREEN evidence, unknown limitations.

### Task 2: Coordination and user-facing integration
Files: coordination.py, coordination_cli.py, worker.py if needed, coordinator_hooks.py, delegation_cli.py, cli.py; create lessons_cli.py, tests/test_lessons_integration.py, tests/test_lessons_cli.py.
Consumes Task1 API; no duplicated rule/counting logic. Snapshot before execution and record outcomes with original context. CLI supports stdin feedback/review; both runtimes receive advisory guidance/due reminders.
- [x] Test CLI roundtrip, per-assignment capture, rework plus incorporation, legacy omission, hooks/off and no repeated Stop veto with real temporary state.
- [x] Implement minimal integration, focused tests and existing coordination/lifecycle regression tests.
- [x] Import exact Task1 files into owned worker copy for final integration checks; no fake equivalent verification.

### Task 3: Documentation
Files: README.md, README.ru.md, docs/DELEGATION_LESSONS.md, docs/DELEGATION_LESSONS.ru.md, src/codex_deepseek_team/data/delegation.md.
- [x] Explain local storage, coordinator responsibility, commands with full valid JSON, automatic reminders vs coordinator review, versioning/effectiveness limits and privacy.
- [x] Preserve current routing/statistics documentation; validate examples against contract.

### Task 4: Coordinator integration and final verification
- [x] Review actual worker diffs and merge only their scope, preserving pre-existing changes.
- [x] Exercise real CLI end to end in temporary project/home; obtain independent adversarial review; fix meaningful findings with failing regressions first.
- [x] Run PYTHONPATH=src python3 -m unittest discover -s tests -v.
- [x] Build/install verified local package into existing user installation, validate installed CLI and preserved effective settings; no release/version claim.
- [x] Record dispositions and accepted coordinator results; report actual rework and limitations.

### Task 5: Canonical bootstrap correction workflow (user clarification)
Files: settings.py, project.py if needed, coordinator_hooks.py after Task2 output is integrated, data/delegation.md, README.md/.ru.md, docs/DELEGATION_LESSONS.md/.ru.md, tests/test_bootstrap_rework.py.
- [x] Define one canonical runtime-aware corrective workflow and expose it through generated bootstrap/effective instructions and both runtime lifecycle contexts.
- [x] Allow minor coordinator fixes, return substantive corrections, distinguish operational rework from explicit quality; keep off/access/sandbox authority and explicit bounded correction decisions.
- [x] Exercise actual Codex/Claude project attach/refresh and hook payloads, preserving foreign project text, idempotence and disabled behavior.
- [x] No standalone local AGENTS addition; local installed-package refresh uses the canonical managed block after verification.

### Task 6: Graded ranking and feedback (latest user clarification)
- [x] Implement validated explicit quality rubric and graded case aggregation in routing, statistics and failure cooldown; preserve legacy evidence, costs and case identity.
- [x] Wire --quality-json through durable coordination feedback and lesson context; test validation and replay with real services.
- [x] Document cosmetic vs actual missed requirements, attribution, legacy counts and heuristic score semantics; integrate with canonical bootstrap workflow.
- [x] Verify end-to-end assessment affects ranking independently of rework counts and does not erase original failures.

## Verification evidence

Integrated source: 1125 tests pass (3 skips) using the required full unittest discovery command. The public journal/CLI replay, graded-outcome replay and positive-feedback recovery simulation also pass. Independent reviews led to explicit DeepSeek corrective assignments for transactional journal snapshots, canonical rules display and case identity across context versions. Coordinator performed bounded documentation corrections; raw rework history and explicit quality remain separate.

Local installation verified from the built wheel (development source remains version 0.8.5). Normal installed CLI exposes graded feedback and lessons commands. Canonical Codex managed block refreshed without changing surrounding project text or saved settings. Fourteen assignment cases were recorded with explicit coordinator assessments; the initial review published two evidence-based advisory rules as local lessons version 1.
