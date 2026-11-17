# Legacy cancellation replay implementation plan

> **For agentic workers:** Follow the registered DeepSeek Team assignment. Use a failing behavioral regression before implementation; do not commit, stage, publish or delegate.

**Goal:** Close tasks whose already-published legacy preparation cancellation has no lease binding or saved finalization request, including a task/workspace baseline mismatch.

**Architecture:** Add a read-only replay predicate in `coordination.py`, inside the existing owner, lifecycle and ledger locks before normal finalization authority. It may corroborate an existing cancellation; it must never create authority for cleanup, launch, binding repair or publication. All other paths retain strict authority checks.

**Tech stack:** Python standard library and unittest.

**Spec:** User-supplied `deepseek-team-issue.md` and `repro-legacy-cancel.py`; accepted diagnostic assignment `as-fa98dc5a4795b116` in `task-6e43c3cf1a195362c051`.

## Constraints and approaches

Keep existing user changes, credentials and raw worker logs outside this delta. Only implementation files are `src/codex_deepseek_team/coordination.py` and `tests/test_legacy_cancellation_replay.py`.

The coordinator selects corroborated read-only replay. Enabling generic missing-binding recovery would still require a missing intent and baseline equality, and relaxing those checks would affect new jobs. A separate force-close command is unnecessary for the fully corroborated empty-result case; ambiguous cases remain refused.

Replay requires the freshly loaded row's exact existing report, matching project/task/assignment/workspace/job identities, failed preparation, rejected disposition, cancelled terminal lease, completed cleanup, no binding/request/correction/execution evidence and an intact empty schema-1 patch. Both baselines must be valid and retained workspace provenance must agree, but task A need not equal workspace B: no execution occurred. Validate exact types for boolean and schema fields; Python bool/int aliases must not grant authority. Reuse existing private-state ownership, liveness and retained-result checks. Do not synthesize binding, attempts or intent, recapture artifacts, recreate checkout data, alter disposition or rewrite history.

## Review focus

- Conflicting report, record, lease or ledger identity must refuse and preserve bytes.
- A live/unknown/foreign owner, held lock or replacement work tree must refuse.
- Execution attempts, pending reservations, correction lineage or conflicting binding history must remain on strict authority paths.
- A nonempty patch, invalid digest, mismatched retained baseline or discarded/schema-2 result must refuse.
- Replayed finalize must preserve task bytes; completion may retain its existing timestamp behavior but must preserve assignment, workspace and artifact history. Both Stop variants must stop blocking for this cancellation.

## Task 1: Implement and regress terminal replay

- [x] Add a synthetic A != B, cancelled/cleaned, no-binding/no-request, zero-attempt fixture with a valid empty schema-1 result. Isolate HOME/runtime/config/package state in temporary directories.
- [x] Test public `finalize --cancel`, `complete` and both Stop variants, idempotence, no provider/capture calls and preserved artifacts/history. Run the regression and observe the reported missing-binding failure before editing production code.
- [x] Implement a narrow helper before `_finalization_authority` in `_publish_job_finalization`. Return only when the persisted terminal cancellation is fully corroborated under the existing locks. Leave generic authority and worker-start baselines unchanged.
- [x] Add table-driven controls for the review-focus cases. Assert rejected attempts do not mutate retained artifacts or ledger history.
- [x] Inspect worker focused checks; the valid module command uses `PYTHONPATH=src:tests` because `test_cancel_unstarted` imports `test_coordination` by its bare name. The originally declared `PYTHONPATH=src` module command failed on this import in both baseline and post-check; this is not a passing managed check. Final coordinator verification uses the repository's discovery command below.

## Task 2: Coordinator review and integration

- [x] Review actual diff and RED/GREEN evidence; independently verify critical safety boundaries.
- [x] Integrate only the accepted two-file patch, preserving current changes.
- [x] Run `PYTHONPATH=src python3 -m unittest discover -s tests -v` and compare any failures with the pre-fix baseline.
- [x] Record quality, disposition, accepted coordinator outcomes and finalization in the coordination ledger. Report any failures or rework accurately.

## Review and correction evidence

The first implementation created its fixture with modern `workspace_gc.finalize`, which adds `lease.finalization`. The literal supplied state has no such field, and the first helper still refused it. Coordinator review reproduced that refusal against the supplied script and recorded `needs-rework` before making a small optional-field correction. A new minimal-manifest regression failed for finalize, complete and both Stop flags before the correction, then passed. Existing conflicting modern finalization remains refused.

The coordinator also pinned valid baseline lengths to exactly 40 or 64 hex characters; 41/63-character inputs failed the new refusal test before that guard correction. Three coordinator regression tests then passed, and a separate run using the literal attached fixture confirmed repeated CLI finalization/completion, both Stop flags and unchanged retained artifacts/assignment history. An independent provider-free control confirmed fresh mismatched-HEAD launches and preparation remain blocked before credentials or model execution.

The pre-fix current-tree baseline passed 2226 tests with 13 skips. During work, another session advanced HEAD to the verified 0.12.0 release acceptance; the target module's committed blob remained unchanged. The optional additional DeepSeek review was cancelled before launch to preserve the old task's immutable baseline. Its unused preparation copy was cancelled normally with captured history. No task rebase, synthetic branch or provider bypass was used. Coordinator final review and the integrated full-suite result remain the acceptance authority.

Final integrated verification: **2241 tests, OK, 13 skips**, using `PYTHONPATH=src python3 -m unittest discover -s tests -v`. The skips match the baseline categories: missing Claude CLI, optional GC stress, missing Gradle distribution and unavailable same-device nested mount. Both implementation/test file hashes were unchanged throughout final verification. Scoped `git diff --check` passed.
