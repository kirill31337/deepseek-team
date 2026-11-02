# Tester Feedback Release Implementation Plan

> **For agentic workers:** Use the registered DeepSeek Team assignments for
> implementation. Workers must not commit or publish; the coordinator performs
> integration, final verification and release.

**Goal:** Correct the investigated tester failures and publish verified 0.10.3.

**Architecture:** Keep routing identity contracts stable, share storage readiness
validation, and fix Claude policy decisions at their installation/error boundaries.
Independent implementation runs in managed copies; the coordinator reviews actual
diffs before integration. Heavy toolchain preparation retains the current sandbox.

**Tech Stack:** Python 3.11+, unittest, Bubblewrap, native Codex/Claude CLIs,
setuptools/build/Twine, GitHub Actions and PyPI.

**Spec:** `docs/superpowers/specs/2026-11-02-tester-feedback-design.md`

## Global Constraints

- Runtime dependencies remain empty; Python minimum remains 3.11.
- Use `PYTHONPATH=src python3 -m unittest discover -s tests -v` for final tests.
- Preserve user changes, credentials, old records, worker containment and foreign
  hooks; workers cannot stage, commit, push, publish, deploy or delegate.
- Feature cards reflect the actual task and concrete effort. Unknown attributes
  stay unknown. Auto routing decisions are respected.

## Review Focus

1. Historical partial feature cards and started-plan replays retain identities.
2. XDG pool safety fails identically in doctor and worker without side effects.
3. Hook installation/error failures block guarded native Claude actions while
   native plan files and unrelated configuration remain usable.
4. Standard output sinks never hide a real mutation, dynamic target or cwd change.
5. Large saved copies survive source mutation/removal and replay within explicit
   byte/inode caps; old recipes remain readable and unsafe inputs stay rejected.

## Task 1: Routing cards and Kotlin

**Files:** routing models/admission/service/CLI, coordination feature boundary,
lessons domain enum, focused routing tests and `tests/test_feature_diagnostics.py`.

**Interfaces:** Preserve `validate_features` canonical values and old feature/plan
hashes. Expose a bounded machine-readable schema and actionable risk diagnostics.

- [x] Add failing missing-card, unknown/high-risk, Kotlin and schema tests.
- [x] Run focused tests and confirm failures describe the required behavior.
- [x] Implement the bounded validation/diagnostic changes and run focused checks.
- [x] Coordinator reviews actual diff and historical identity compatibility.

## Task 2: Shared storage readiness

**Files:** storage budget, execution allocation, doctor and storage/doctor tests.

**Interfaces:** Add a public check-only pool validation helper reused by doctor;
actual worker reservations remain authoritative against later filesystem changes.

- [x] Regress safe state plus writable/symlink/foreign XDG ancestors.
- [x] Implement read-only validation and actionable clean code-78 reporting.
- [x] Run focused tests and verify no chmod, pool creation or credential access.

## Task 3: Claude installation and policy failures

**Files:** Claude config, coordinator hooks/activity, onboarding and Claude tests.

**Interfaces:** Recognize legacy and new owned hook definitions precisely; use a
resolvable absolute entrypoint and event-appropriate blocking error responses.

- [x] Regress missing executable, exception and malformed-input behavior first.
- [x] Add PreToolUse failure blocking and consistent native-plan normalization.
- [x] Verify foreign-hook preservation, upgrade/idempotency and scope boundaries.
- [x] Coordinator runs real current Claude with local API fixtures and checks
  healthy controls, error cases, `onFailure`, and default/custom plan directories.

## Task 4: Shell sinks and plan regression

**Files:** shell classifier, shell/project-context tests and public plan CLI test.

**Interfaces:** Keep scoped classifier return types and cwd-uncertainty semantics.

- [x] Regress exact sink-only and mixed real-target commands.
- [x] Implement narrow literal destination classification and rerun checks.
- [x] Regress failed-then-append through CLI/heredoc/pipe, IDs and explicit errors.

## Task 5: Toolchain snapshots and JVM workflow

**Files:** toolchains/cache/workspace CLI, focused tests, TOOLCHAINS.md and README.

**Interfaces:** First record exact compatible recipe/capacity contracts from the
bounded diagnostic; then register the implementation as a new Auto deliverable.

- [x] Review diagnostic evidence and choose explicit CLI/recipe interfaces.
- [x] Regress external-source mutation/removal, replay, byte/inode boundaries,
  old recipes, unsafe sources and source/input fingerprint behavior.
- [x] Implement immutable saved copies and usable explicit bounded limits.
- [x] Document and verify a complete offline JVM/Gradle/Kotlin preparation path.

## Task 6: Version, integration and publication

**Files:** pyproject.toml, package version, docs/releases/0.10.3.md and accepted diffs.

- [x] Prepare matching 0.10.3 metadata and accurate release notes in managed copy.
- [x] Review and integrate actual worker patches; record accepted outcomes/rework.
- [x] Obtain an independently routed review of the combined current source/tests.
- [x] Run complete tests, native Claude scenarios, build/Twine and installer checks.
- [ ] Stage only approved files, commit, tag and push the reviewed release.
- [ ] Verify CI release assets; publish the identical artifacts to PyPI through
  the configured route and confirm hashes plus clean registry installation.

## Review corrections and verification evidence

- [x] Preserve legacy saved risk codes while correcting their displayed advice.
- [x] Correct Gradle documentation to copy one sanitized home; real JDK 17,
  Gradle 8.12.1 and Kotlin 2.1.21 offline builds passed before and after saved
  replay, input removal and original-workspace finalization.
- [x] Replace obsolete snapshot interception with prepared-byte/replay evidence;
  verify real finalization removes execution and preserves compact history.
- [x] Preserve reviewed execution identity across successful preparation tails;
  nine planning regressions pass and the real retained job starts its correction.
- [x] Reject non-string Claude lifecycle names with native blocking exit 2.
- [x] Separate auxiliary classifier requests and admission/tool failures in the
  native Claude fixture; require a healthy allowed-write control.
- [x] Reject missing pools beneath uncreatable or read-only ancestors without
  a readiness probe or filesystem mutation.
- [x] Keep corrupt-cache evidence with actionable recovery and correct legacy
  tool/copy guidance; clarify the matching documentation.

The first full native-enabled run executed 1,895 tests and found five failures:
three in the native fixture and two corrected toolchain test contracts. Final
publication requires a fresh complete passing run after these review corrections.

The final candidate also passed the real native Claude fixture (nine cases),
55 focused doctor/storage tests, the root `/proc` doctor/reservation controls,
wheel/sdist metadata and boundary checks, Twine, pip/pipx/uv install/replace/remove
checks, and another real immutable offline JVM replay/build/finalize cycle.
The native-harness worker attempt ended with a malformed event stream; its
recorded test-only partial delta was reviewed and accepted after the coordinator's
nine native checks. Its failed process status and neutral environment assessment
remain in the private journal; no automatic retry or clean first-pass claim was
made. Final full-suite and post-tag publication evidence remain separate gates.

Final pre-tag verification: the required complete command passed 1,923 tests in
795 seconds with two conditional skips and no failures. The source contained in
the candidate wheel/sdist matches the reviewed working tree. The remaining two
publication items are post-commit gates; their CI, asset digest and registry
installation evidence is recorded in the release and coordination ledger after
this committed pre-tag snapshot.
