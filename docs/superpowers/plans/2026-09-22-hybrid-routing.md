# Hybrid Routing Implementation Plan

> **For agentic workers:** Use the supplied task contracts; coordinator integrates and verifies. Workers must not stage, commit, publish, or delegate.

**Goal:** Ship evidence-backed hybrid delegation with persistent learning, conservative automatic decisions and safe recovery from uncertain evidence, explicit source ingestion, cost accounting, budget reservations and chronological evaluation.

**Architecture:** Strict feature and evidence contracts feed a pure probabilistic estimator. A private transactional store and service connect it to the existing coordination ledger and CLI. All source/network actions remain explicit; hooks only use local evidence.

**Tech Stack:** Python 3.11+, sqlite3, unittest, standard library only.

**Spec:** docs/superpowers/specs/2026-09-22-hybrid-routing.md

## Global constraints

- Preserve user state, native runtime configuration and worker OS boundaries.
- No credentials, raw model logs, code samples or prompts in routing state or Git.
- Modes and routing recommendations never grant access or overwrite explicit executor choices.
- External data cannot silently acquire local evidentiary weight.
- No automatic paid experiments or implementation retries.

## Review focus

- Repeated cases, retries and revised dispositions must not inflate success evidence.
- Known kind/domain/operation task families cannot borrow evidence from another family; the earliest meaningful failure cannot be erased by later acceptance.
- NaN/Infinity, future timestamps, huge JSON, hostile URLs and unsafe state paths fail clearly.
- Low support, new runtime/model/context and unknown task features cause abstention.
- Concurrent reservations cannot exceed the configured budget; crashes retain reservations conservatively.
- Auto recovery stays limited to safe normal tasks: default rate 0.1, first eligible canary then spacing 10, one active ticket, one-hour same-family/context cooldown and pending TTL, and no running timeout.
- Replanning, chronological corrections and the durable feedback outbox remain correct across crashes, compaction, disabled routing and legacy tasks.
- SQLite schema version 2 migration and streaming export preserve the existing export JSON shape without materializing all state.

## Tasks

- [x] Coordinator: implement strict models, private SQLite schema version 2/store, service and atomic experiment budget ledger with focused tests.
- [x] Estimator component: implement exact task-family isolation, execution-context matching, failure-dominant immutable corrections, decay, conservative policy and chronological evaluation.
- [x] Source component: implement strict native/SWE-bench parsing plus explicit integrity-pinned HTTPS fetch with network and payload guards.
- [x] CLI/docs component: implement project-scoped JSON commands, recovery status/release, bounded input and streamed/atomic export; document behavior in English and Russian.
- [x] Auto settings and recovery components: preserve saved numeric profiles, default new installs to Auto, and implement guarded normal-task recovery with rate, active-ticket, TTL and cooldown limits.
- [x] Coordinator integration: bind automatic decisions, preserve explicit executors and immutable feedback history, capture actual execution identity, classify failures, and replay the durable observation outbox.
- [x] Worker integration: import and coordinator-review all three bounded DeepSeek sandbox worker results without worker commits or publication.
- [x] Coordinator: completed independent correctness/security review, statistical/concurrency checks, full-suite verification, wheel/sdist and clean-install smoke, and integration into the user's checkout.

## Execution record

- Clean baseline: 221 tests, two native Codex integration failures and 10 skips (`/tmp/deepseek-hybrid-baseline.log`). Both failures were diagnosed as fixture defects; their fixes are independent of routing behavior.
- User authorized complete implementation in `/tmp/deepseek-team-hybrid-routing`. Fresh installs now default to Auto while existing saved 25/50/75 profiles retain priority and continue supplying evidence.
- Three actual managed DeepSeek workers ran inside the required Linux sandbox for the budget ledger, Auto settings and recovery scheduler. Their changes were imported and coordinator-reviewed. AppArmor remained enabled throughout; the provider key stayed private and outside Git/logs.
- Native Codex `hooks/list` plus `config/batchWrite` API calls recorded trust for all four package hooks. No hook-trust bypass was added to product behavior.
- Independent lifecycle review found and resolved plan/start persistence gaps, stale access snapshots and abandoned-process recovery. Fault-injection and real process-exit tests cover recovery across the ledger/SQLite commit boundary; active workers never expire automatically.
- Final required suite in the original checkout: 472 tests, OK with three skips because Claude CLI is absent. Real installed Codex protocol and Linux sandbox tests passed.
- Wheel/sdist build and clean-venv CLI smoke passed. Installed 0.7.0 matches source; offline doctor passed. The original project is attached to four API-verified trusted hooks, retains explicit 75/full-access, and starts with an empty evidence history. Private key and local preference files remain outside Git. No fabricated public benchmark scores were seeded.
