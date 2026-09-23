# Automatic routing stages implementation plan

> Historical record: this document describes an earlier implementation or verification run. Commands, defaults and compatibility claims here are not current instructions. See [README](https://github.com/kirill31337/deepseek-team/blob/main/README.md) and the current routing guide for supported behavior.

**Goal:** Auto automatically learns by actively delegating safe ordinary work, uses comparable outcomes when available, and backs off only for supported problems. The user approved these three stages and explicitly requires no manual stage switch.

**Architecture:** Keep the conservative pure quality/economics estimator. Add a pure stage assessment and extend the existing transactional admission scheduler with bootstrap admission, sharing durable ticket lifecycle, outcome handling and a project-wide cap. Bootstrap and cost calibration retain one comparable coordinator opportunity per ten eligible cases; recovery retains its existing separate stride and one-slot limit. Every phase respects access, explicit executors and disabled/manual policies.

## Constraints and decisions

- Workers stay deepseek-flash, runtime codex, required Linux sandbox, maximum three concurrent workers. Credentials/config/raw logs never enter Git.
- Bootstrap is only small, low-risk, known/local, clearly specified work with tests/reproducer and concrete declared checks. No extra paid benchmarks.
- Quality support means existing lower confidence bound and local support both pass, not just five successes. Unknown price cannot become measured savings. Existing strict learned cost gate stays intact; safe bounded cost-calibration tasks can collect evidence while it remains unknown.
- Actual comparable local failure evidence of at least half an effective case routes unsupported quality into recovery. Symmetric evidence decay and expiry remain; recovery does not erase first-pass failures. A quality-supported category is adaptive, subject to fresh failure cooldown.
- Measured uneconomic outcomes with sufficient worker and coordinator evidence veto all trial selection. Missing cost evidence permits bounded learning, with a coordinator comparison every tenth opportunity in that family.
- Up to three total pending/running trial tickets, with recovery at most one. Pending tickets expire after one hour; running workers never expire or retry automatically.
- Canonical immutable decision per binding, including denied admissions. Failed start/write transactions must roll back all new records. Legacy state remains readable.
- All worker quality failures can start a family cooldown, including ordinary adaptive/manual outcomes. Worker tickets cannot be closed by coordinator feedback. Queued automatic starts recheck cooldown.

## Tasks

- [ ] 1. Add failing behavioral tests for consecutive cold-start admission, five-success bridge, adaptive transition, missing prices/comparison sample, economic veto, per-family failure/recovery, deduplicated retries, access/manual guards, immutable bindings, concurrency, rollback and ticket lifecycle. Run focused unittest discovery and inspect expected failures.
- [ ] 2. Implement stage assessment, scheduler admission metadata and canonical bindings; integrate observation/start/status/export. Run routing tests and preserve legacy recovery tests.
- [ ] 3. Update generated coordinator guidance and RU/EN README/routing docs. Integrate DeepSeek-owned 0.7.1 metadata and release notes after actual diff/check review.
- [ ] 4. Obtain fresh independent diff review; fix verified defects. Run full unittest suite and package build. Review exact publish diff for user-file/secret exclusion; commit and push main, verify remote hash.

## Review focus

No five-success confidence cliff; unknown dollars do not cause permanent abstention; real negative economics do not become exploration; old failures/retries never forge success; concurrent/expired/replanned tickets cannot bypass capacity or current permissions.

## Progress

- Baseline commit e803ba6; isolated worktree /tmp/deepseek-team-auto-bootstrap, branch feat/auto-bootstrap. Existing user AGENTS.md and earlier planning files remain untouched in main checkout.
- Coordination task task-603e0cb0ba2839505915 registered. Auto retained core/docs with coordinator and selected release-metadata for DeepSeek as-a1ede9705898805d. Native independent-review checked phase/lifecycle boundaries.
- Ruling: The active project hook rejects source edits outside the attached checkout, including the new worktree. Work continues in the attached checkout on feat/automatic-routing-stages, preserving user changes; the initial worktree supplied a clean baseline (472 tests, 3 skipped).
- New stage contracts were run before implementation: 17 tests, 6 assertion failures and 10 missing-feature errors. Implementation made all 17 pass; updated legacy routing expectations then passed all 254 routing tests.
- Further regression contracts cover concurrent connections, transaction rollback, read-only review, independent recovery disable, mode revocation, adaptive failure feedback and already-running reconciliation. Reconciliation test reproduced a cooldown regression before the targeted fix.
- DeepSeek completed three metadata files in owned sandbox copy. A coordinator-supplied quoting defect broke the registered command; review also found one misleading unknown-family sentence. Recorded needs-rework, imported only actual three-file output, corrected prose and package/CI assertions. Original failure provenance retained.
