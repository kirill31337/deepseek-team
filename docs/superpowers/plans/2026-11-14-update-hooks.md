# Package Update and Hook Refresh Implementation Plan

> **For agentic workers** Use superpowers:executing-plans for assigned implementation and verify each task before returning it. Only the coordinator integrates, commits or publishes.

**Goal** Upgrade the package and its existing hooks together and expose required user actions to humans and AI agents.

**Architecture** Add a focused hook-refresh service and a manager-aware updater. Existing safe hook installers provide ownership and atomic configuration changes. The updater invokes newly installed code in a fresh process, while lifecycle and CLI first use cover ordinary package-manager upgrades.

**Tech Stack** Python 3.11+, standard library, unittest, pip/pipx/uv, existing Codex/Claude native hooks.

**Spec** `docs/superpowers/specs/2026-11-14-update-hooks-design.md`

## Global constraints

- Target release is 0.12.0. Public CLI, JSON report, exit codes and action requirements follow the spec.
- Preserve existing user changes, foreign configuration and saved permission/off choices.
- Workers use isolated owned copies and the mandatory Linux sandbox with runtime codex and model deepseek-flash. Never stage, commit, push or publish.
- Every setup/hook test isolates HOME, CODEX_HOME, CLAUDE_CONFIG_DIR, XDG_CONFIG_HOME and state paths.
- Source edits remain limited to each assigned scope. Changes to the common CLI are reconciled by the coordinator after review.

## Review focus

- A direct package-manager upgrade bypasses package callbacks, so agent instructions require a post-upgrade refresh and user-action report.
- PATH discovery must not silently add a coordinator integration or reactivate disabled hooks.
- An executing updater can retain old imports after replacing its own package, so only a fresh process confirms the result.
- JSON and native hook protocols must remain valid while required actions reach redirected output and agent context.
- Malformed, concurrent or symlinked configuration must retain data and existing gate behavior.

## Task 1 — Hook refresh and first use

Files are `src/codex_deepseek_team/hook_refresh.py`, `src/codex_deepseek_team/cli.py`, `src/codex_deepseek_team/coordinator_hooks.py`, `tests/test_hook_refresh.py` and focused existing hook fixtures if necessary.

Produces `refresh_installed(runtime='auto', *, notify=False)`, `refresh_for_cli(argv)` and `main(argv=None)` with the exact report and exit contract from the spec. `hooks refresh` delegates to that main. Keep CLI changes near integration-management dispatch and ordinary refresh. The updater's early dispatch belongs to Task 2.

- [x] Add focused failing tests for ownership, both runtimes, disabled/missing/stale configurations, noninteractive/JSON/context action notices, idempotence and controlled config failures.
- [x] Run the focused tests and confirm the new behavior fails before implementation.
- [x] Implement the refresh service with existing installers and lifecycle/CLI wiring.
- [x] Pass `PYTHONPATH=src python3 -m unittest discover -s tests -p test_hook_refresh.py -v` plus affected config/bootstrap/native fixtures.
- [x] Return actual diff, checks, assumptions and limitations for coordinator acceptance.

## Task 2 — Unified updater

Files are `src/codex_deepseek_team/updates.py`, a disjoint early update dispatch/help entry in `src/codex_deepseek_team/cli.py`, and `tests/test_updates.py`.

Consumes Task 1's fresh-process module interface. Unit checks may control manager subprocess responses, and final integration verifies the real new module. Produces `main(argv=None)` and a stable combined report with package outcome and required actions.

- [x] Add failing tests for manager provenance, explicit version validation, command arrays, old-import isolation, installer failure and post-upgrade action/failure reporting.
- [x] Confirm RED for the new public behavior.
- [x] Implement the updater and CLI dispatch, preserving ordinary callers and unsupported-install recovery guidance.
- [x] Pass `PYTHONPATH=src python3 -m unittest discover -s tests -p test_updates.py -v` and relevant CLI/installation checks.
- [x] Return actual diff and evidence without invoking a real host package update.

## Task 3 — User and agent update contract

Files are `README.md`, `README.ru.md`, `src/codex_deepseek_team/data/delegation.md`, `src/codex_deepseek_team/data/delegation-linked.md`, `src/codex_deepseek_team/onboarding.py`, `pyproject.toml`, `src/codex_deepseek_team/__init__.py`, and `docs/releases/0.12.0.md`.

- [x] Describe unified update, direct-manager first-use behavior and the explicit post-upgrade agent refresh command accurately in EN/RU.
- [x] Require AI agents to surface all required actions and pending native confirmation even for noninteractive/JSON upgrades.
- [x] Update only the setup completion guidance and shipped instruction additions, preserving unrelated policy text.
- [x] Set both version sources to 0.12.0 and write EN/RU release notes with the direct-manager callback limitation.
- [x] Run metadata/instruction/onboarding checks and `git diff --check`, then return the bounded delta.

## Task 4 — Coordinator integration and release

- [x] Review all actual diffs and declared checks. Record accepted work and any rework, integrate exact paths and reconcile CLI hunks.
- [x] Exercise normal public commands in isolated homes, including code 3, JSON/stderr, disabled hooks, malformed config, repeat updates and a real old-to-new wheel replacement with each manager.
- [x] Run `PYTHONPATH=src python3 -m unittest discover -s tests -v` and required available native Codex/Claude controls. Diagnose and fix recoverable failures through routed correction work.
- [x] Build wheel/sdist, run strict metadata checks and real pip/pipx/uv install/replacement verification. Review artifact contents and user-change preservation.
- [x] Commit only accepted scoped files, push and wait for relevant CI. Create the immutable version tag after green verification.
- [x] Monitor tagged publication, publish exact verified release artifacts to PyPI and verify GitHub/PyPI digests and a clean registry install.
- [x] Finalize owned worker copies, record all terminal outcomes and close coordination. Report release links, usage, required user actions and accepted contributions.

## Task 5 — In-session upgrade notices

Coordinator ruling during implementation adds the spec's in-session coverage requirement. A direct package-manager update can occur after the turn's bootstrap, so a Stop-only ending needs an explicit agent notice too. This requirement supplements the first-use mechanism and preserves the existing PreToolUse/Stop prohibition on configuration and project writes.

Files are `src/codex_deepseek_team/upgrade_notices.py`, a bounded post-input/pre-output integration in `src/codex_deepseek_team/coordinator_hooks.py`, and `tests/test_upgrade_notices.py`. This task is independent of the refresh service's implementation and produces the three exact interfaces defined in the spec.

- [x] Add RED tests for an old/unknown session's first PreToolUse/Stop after package replacement, both native formats, another session seeing the version first, and repeated delivery.
- [x] Implement owner-only bounded per-session/version receipts and notice annotation while preserving gate decisions, repeated-Stop behavior and one-JSON publication.
- [x] Pass `PYTHONPATH=src python3 -m unittest discover -s tests -p test_upgrade_notices.py -v`, including unsafe receipts and publication failures.
- [x] Return actual changes and focused checks for coordinator CLI/hook merge and real native verification.

## Execution record

- The coordinator created `/tmp/deepseek-update-release-0h52telw/validation` as an owned detached worktree to isolate verification from preexisting user changes. Three foreign stale worktree records were preserved.
- Baseline installation checks passed 5 tests. Real Codex and Claude baseline controls passed 2 tests against local provider-free API fixtures.
- Task 3 is integrated after 77 independent coordinator checks. The coordinator clarified the compact linked instruction and cleaned up the added full-guide copy. Original worker quality is met.
- Task 2 returned 16 focused updater checks and 17 short CLI/installation checks. Its nested full suite is environment evidence only. Coordinator review reproduced an incorrect unchanged claim after manager failure and routed same-copy correction `as-50fdcc6cc036a3be` for accurate partial-state reporting, stable update serialization and INSTALLER provenance.
- Task 5 returned 10 focused notice checks and adjacent hook regressions. Coordinator review reproduced mandatory text after 240002 characters of policy context and routed same-copy correction `as-aed8a94934642bab` for notice priority and observable preparation errors.
- Native-agent deliverable `github-release-observation` uses authenticated GitHub access unavailable through worker provider relays. Main 01ea59a has green Tests, Delegation verification and Release verification. PyPI OIDC is disabled, so the coordinator will publish the exact tagged GitHub assets through the already authorized private-token route.

- All implementation slices and their linked corrections are integrated after 201 combined coordinator checks. GC dry-run and malformed falsy hook-section preservation have explicit coordinator RED/GREEN regressions. Full verification/publication remain pending.

- The corrected full host run passed 2225 tests in 1281.461 seconds with two skips, with required real Codex/Claude and Gradle controls. An initial verification environment placed source under a fixture-hidden `/tmp` and selected an inaccessible cached Codex wrapper. Moving the owned verification copy outside `/tmp` and prioritizing the real system executable resolved those preparation failures without weakening the sandbox.
- Independent DeepSeek review found that failed notified refresh recovery could lose `--notify`. The coordinator added a public regression and corrected both the recovery command and its pending native-review message. The final source passed 58 feature tests and then 76 exact-current feature/native tests. The full host result predates this final message-only correction and its new regression; exact committed CI is required before tagging.
- Real pip, pipx and uv installation, replacement and uninstall checks passed with external sentinels preserved. All three managers passed direct 0.11.0-to-0.12.0 upgrades and fresh-process updater transitions from a private prerelease fixture. Installed-wheel malformed-config and missing-entrypoint controls retained user data, returned 78 and kept notified recovery guidance.
- Final wheel/sdist build and strict Twine checks passed. All 67 installed package files match the reviewed source, and all 188 sdist package/test files match the integration tree. Publication will use the exact tagged CI assets, not these local candidates or the private fixture.
- All seven DeepSeek assignments have accepted reviewed outcomes and their four logical jobs plus the review job are finalized with safe capture and cleaned execution trees. Original rework/quality history remains intact. The coordinator reviewed lessons and added the bounded maintenance-failure contract rule.

## Release acceptance — 2026-11-14

- Accepted implementation commit `946ffaec5310c3a37170949e9352481f840a2ec2` contains exactly the 20 reviewed paths. Preexisting `AGENTS.md`, private attachments and concurrent legacy-cancellation work were excluded and preserved.
- Exact main CI passed Tests `43618614934` across Python 3.11–3.14, Delegation verification `43618614975` and Release verification `43618614855` before the annotated immutable tag `v0.12.0` was created.
- Tag publication `43619481364`, tagged Tests `43619481404` and tagged Delegation verification `43619481410` all passed on the same commit. Publisher discovery ran 2226 tests with 94 environment-dependent skips; Python 3.14 ran 2226 tests with 31 skips. Required real native and Gradle checks are separately covered by the full host and exact-current verification recorded above.
- GitHub release `445459373` is public at https://github.com/kirill31337/deepseek-team/releases/tag/v0.12.0. PyPI OIDC was disabled, so the coordinator uploaded the exact verified GitHub assets through the existing private-token configuration to the official PyPI endpoint. No candidate or private fixture was uploaded, and the tag was not moved.
- Wheel `deepseek_team-0.12.0-py3-none-any.whl` is 424018 bytes with SHA256 `6d4ef2261e9f94a0b28590459368b9e3bfb608c16d52e6529b41b8b10d021b81`. Sdist `deepseek_team-0.12.0.tar.gz` is 960755 bytes with SHA256 `d1e4f8f743ced6d43e23db272a20e405ca532fc68447767e2a01c712d58a4e45`.
- GitHub REST digests, official PyPI metadata hashes and sizes agree. All 67 wheel package files and 232 tracked sdist files match the exact tagged commit. Strict Twine checks passed on those downloaded assets.
- A clean virtual environment installed 0.12.0 from the official PyPI index, checked every installed package file against the release wheel, exercised version/help and notified refresh JSON, then completed a real unified pip update. An initial Simple-index propagation delay briefly exposed the old version list while version JSON was already available; after the index exposed 0.12.0, the same isolated verification completed successfully without package changes or another upload.
- Both coordinator-owned verification worktrees were removed normally after proving source reachability and retaining private manifests, patches and verification evidence. Foreign worktree registrations were preserved. All DeepSeek worker jobs were safely finalized, and the native GitHub observer confirmed exact tag identity, green workflows and public asset metadata.
- User-required restart/session and native `/hooks` review remain explicit actions in notified update/refresh output, including AI and noninteractive paths. No user confirmation is inferred or granted by the package.
