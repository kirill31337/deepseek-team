# Version 0.8.3 verification

Date: 2026-09-23. Scope: route every delegated subtask before native dispatch,
require explicit native exceptions, and bind parent launch/message events to the
coordination ledger without widening worker access.

The final local suite ran **660 tests, all passing without skips**, with Codex
**0.153.2** and Claude Code **2.1.278** available. The command was
`PYTHONPATH=src python3 -m unittest discover -s tests -v`, with the isolated Claude
installation added to PATH. Real runtime fixtures used local offline providers:
both native launch attempts reached the parent hook and were denied before
unplanned delegation. Codex additionally verified that no child request reached
the model fixture. Synthetic hook tests cover message dispatch, structured text,
binding replay, revoked access, outcome closure and freeform patch scope.

Wheel and sdist builds passed `twine check --strict`. The exact wheel passed
install/replacement/uninstall checks with pip, pipx and uv, preserving external
configuration. Archive inspection found no local configuration, credentials or
raw logs. The installed-package gate in GitHub Actions also checks native denial
for both coordinator hook protocols; workflow results are attached to the release
commit rather than inferred from local checks.

Three real DeepSeek assignments supplied ledger implementation, instructions/docs
and independent hook review. The ledger implementation required coordinator
corrections for replay, immutable started scopes and manual-profile enforcement;
the hook review led to three additional regression fixes. No native subagent
performed the release work. A separate paid `doctor --runtime codex --live
--effort low` passed provider streaming and sandboxed worker checks, preserving
primary coordinator configuration/authentication. The Ubuntu unprivileged-userns
restriction remained enabled.

Coverage applies to hook events actually delivered by each tested runtime.
Attestation text is coordinator evidence, not cryptographic proof of user intent.
Native hook trust remains owned by Codex. Worker OS isolation was not weakened.

---

# Version 0.5.0 verification

> Historical record: this document describes an earlier implementation or verification run. Commands, defaults and compatibility claims here are not current instructions. See [README](https://github.com/kirill31337/deepseek-team/blob/main/README.md) and the current routing guide for supported behavior.

Date: 2026-09-22. Release scope: persistent coordinator task state, observable delegation-process enforcement for Codex, managed worker accounting/readiness, selective source preparation, and preserved 25/50/75 access semantics.

## Final release-candidate gate

Code SHA **377c33f11043ecf593b610f5471db43fd39f4848** passed all three release gates:

- GitHub Actions **Tests** run **35667826235**:
  - Python **3.11, 3.12 and 3.13** all passed;
  - Python 3.11 reported `Ran 212 tests` and `OK (skipped=12)`;
  - both wheel and sdist built successfully;
  - both console aliases reported `deepseek-team 0.5.0`;
  - Ubuntu Bubblewrap/AppArmor smoke passed.
- GitHub Actions **Delegation verification** run **35667826277**:
  - installed **Codex CLI 0.155.1** and **Claude Code 2.1.278**;
  - the real worker-permission suite reported `Ran 9 tests ... OK`;
  - the existing demo still reported: `25/read-only PASS; 50/full-access fix+test PASS; 75/full-access three parallel copies + integrated tests PASS; 75/read-only PASS`;
  - a separate real Codex coordinator/new-session test reported `Ran 1 test ... OK`;
  - that coordinator test observed a real lifecycle hook, registered a distribution, attempted duplicate source mutation, and verified the PreToolUse denial occurred before the file changed.
- GitHub Actions **Release verification** run **35667826281**:
  - built `codex_deepseek_team-0.5.0.tar.gz` and `codex_deepseek_team-0.5.0-py3-none-any.whl`;
  - clean-installed the wheel and verified `deepseek-team 0.5.0`;
  - installed **v0.4.0** through its normal `install.py`, created a 75/full-access attached Codex project and private credential, then upgraded the same prefix with 0.5.0;
  - verified project settings, credential hash and existing managed project binding were preserved;
  - verified the stable user-level Codex hook set was installed automatically on update without re-running project `init`;
  - invoked the installed 0.5.0 coordinator hook in a fresh synthetic session and observed the pre-plan mutation denial;
  - uploaded the release-candidate distribution artifact.

No live DeepSeek inference or paid provider request was required for these gates. Real Codex/Claude binaries were driven against local offline provider fixtures where model protocol behavior was needed.

## Regression for the reported 75/full-access failure mode

The required regression is covered by coordination policy tests and Codex lifecycle integration:

- a substantial 75/full-access plan that delegates one implementation but keeps separable tests, fixtures and documentation with the coordinator under reasons such as "quality ownership" or "release work" is rejected as insufficient distribution;
- merely having any worker/review assignment is not accepted as proof that 75% is being applied;
- 50/full-access cannot be satisfied by a review-only worker when worker-eligible implementation/test/docs work exists;
- technical reasons such as unavailable dependencies must be recorded from runner/preflight evidence rather than free-form coordinator prose;
- self-asserted `user_explicit` metadata is not treated as deterministic proof of a semantic user instruction;
- genuinely small work can stay local, but the small-task exemption is structurally limited to one concrete non-wildcard scope;
- new unplanned source scope after a worker result is blocked until the distribution is revised.

## Persistent state and attribution

The coordination ledger persists session/task/deliverable/assignment/workspace ids, criteria, dependencies, checks, results, dispositions and structured constraints outside the repository. SessionStart after compaction re-injects the recorded task state, including completed review results.

Managed runner start/finish accounting is automatic. Coordinator-prepared source imported with `workspace import` is snapshotted before worker execution and kept separate from worker-only file deltas. Declared checks are run after the worker inside the same sparse sandbox and stored with exit codes.

Missing command/path/check requirements are detected before provider credential access. A command visible only on the host PATH is rejected if it is unavailable inside the actual worker sandbox.

## Coordinator integration limits

Codex enforcement uses supported lifecycle hooks. `PreToolUse` can deny supported mutation calls before execution and `Stop` can force a continuation when task state is incomplete. DeepSeek Team installs one stable user-level hook definition and activates it only for repositories previously attached with the package-managed Codex block.

**Codex itself owns hook trust.** DeepSeek Team neither bypasses nor claims to infer that native trust state. CI uses Codex's explicit test-only trust bypass so the hook mechanics are reproducible without interactive UI.

Claude Code receives the same managed instructions, persistent ledger and worker accounting, but coordinator distribution enforcement is **instruction-driven** in 0.5.0; no Claude PreToolUse technical gate is claimed.

Hooks are not presented as a universal interception boundary. Future/specialized Codex tool surfaces that do not traverse the supported lifecycle hook path require re-verification.

## Review note

A GitHub Copilot Code Review was requested through GitHub's official reviewer API on PR #1. The repository/account did not retain the Copilot reviewer request and no Copilot review was produced, so this release does **not** claim an independent Copilot review. A separate manual diff/security review found and fixed additional issues before the final gate, including Edit/Write hook-path handling and removal of a self-asserted user-override bypass.

---

# Version 0.4.0 verification

Date: 2026-09-21. Release scope: configurable 25/50/75 delegation profiles, independent read-only/full-access policy, reusable managed development copies, and preserved legacy exact-file writer behavior. Release publication follows a green final release-candidate gate on this same implementation line.

## Verified code gate

Code SHA **5aa74b9ff63f3229169a75495dd47684719d8085** passed both required workflows:

- GitHub Actions **Tests** run **35636312526**:
  - Python **3.11, 3.12 and 3.13** all passed;
  - Python 3.11 reported `Ran 179 tests` and `OK (skipped=9)`;
  - the wheel/sdist built and installed successfully;
  - both `deepseek-team --version` and the legacy `codex-deepseek-team --version` executed successfully;
  - the Ubuntu AppArmor/Bubblewrap smoke job passed.
- GitHub Actions **Delegation verification** run **35636312503**:
  - installed pinned **Codex CLI 0.154.0** and **Claude Code 2.1.273**;
  - used Bubblewrap/AppArmor on Ubuntu 24.04;
  - used an offline local provider fixture, not a real DeepSeek credential or paid API request;
  - reported `Ran 7 tests ... OK`.

The dedicated demo reported:

```text
DEMO: 25/read-only PASS; 50/full-access fix+test PASS; 75/full-access three parallel copies + integrated tests PASS; 75/read-only PASS
```

## Covered feature contracts

- `25/50/75` profiles and `access=auto|read-only|full-access` resolve independently.
- Effective precedence is `CLI > project > global > defaults`, with the source of each value reported.
- Defaults remain backward-compatible: `25 + auto -> read-only`.
- Explicit access survives delegation-level changes until changed back to `auto`.
- `75 + read-only` keeps a high delegation target for analysis/review while retaining a real write prohibition.
- Legacy `--write --allow-write` remains available and conflicting new access/workspace flags are rejected.
- Managed full-access creates an owned copy from committed HEAD, never cleans/adopts the user's checkout, and does not silently copy dirty/untracked/ignored source files.
- A full-access worker can create previously unlisted files and run local tests/builds inside its assigned copy.
- Successful dirty owned copies can be reused for sequential iterations.
- Failed/partial copies are retained; continuation requires explicit `--resume-after-failure`; no automatic implementation retry occurs.
- Three independent workers can operate in parallel on separate copies; sibling copies and the original checkout are not visible inside the sandbox.
- Both real Codex and real Claude permission/protocol surfaces are exercised for read-only and full-access modes.
- Managed runtime mounts are sparse: npm runtimes expose the concrete launcher/package rather than a mixed user prefix such as `~/.local`.
- Managed full-access uses a private network namespace with only the fixed provider relay capability; the real provider credential remains host-side.
- Provider failures remain classified as provider failures even when the runtime emits malformed/empty protocol output; partial files remain preserved.
- Managed AGENTS.md/CLAUDE.md refreshes preserve user content outside the owned block and resolve the current effective policy before new assignments.
- `doctor` resolves the same policy as config/worker/instructions and checks the managed runtime capability surface when effective access is full-access.

## Deliberate limits

- Percentages are policy targets, not measurements of tokens, lines, wall-clock time, useful contribution or savings.
- Small/inseparable tasks may delegate less; the system does not manufacture work to hit a percentage.
- Full-access is full development access only to an owned isolated copy. Commits, integration, pushing, publishing, deployment, production databases/services and secrets remain coordinator-owned.
- The dedicated verification uses real coordinator binaries but an offline provider fixture. It is not proof of a successful live DeepSeek inference.
- The existing billable `doctor --live` remains the separate end-to-end provider-routing check.
- This feature work does not add a task scheduler and preserves the three-worker limit and unlimited total timeout default.

---

# Version 0.3.0 verification

Date: 2026-09-16. Release scope: Linux, Python 3.11+, Codex CLI and/or Claude Code CLI coordinating DeepSeek workers with required Bubblewrap/AppArmor-aware OS isolation.

## Completed checks

- Development used regression-first cycles for the Bubblewrap/AppArmor capability layer, worker integration, sandbox CLI/doctor/installer flow, release metadata, and the final Codex nested-userns hardening. Each new contract was observed failing before its production implementation.
- GitHub Actions run **35121359562** on code candidate `ce4c3830557a6555ba09122e63630bf3a5892b70` completed successfully on Python **3.11, 3.12 and 3.13** plus the dedicated Ubuntu 24.04 sandbox smoke job.
- The suite at that code gate contains **159 tests**. Python 3.11 reported `Ran 159 tests` and `OK (skipped=2)`. The two skips are the pre-existing real-Codex protocol tests because Codex CLI is not installed in the hosted matrix job; all other synthetic/runtime/Git/AppArmor tests ran.
- The Python matrix built both `codex_deepseek_team-0.3.0.tar.gz` and `codex_deepseek_team-0.3.0-py3-none-any.whl`, installed the wheel, and executed both console aliases successfully. `codex-deepseek-team --version` and `deepseek-team --version` both printed `deepseek-team 0.3.0`.
- Build logs confirm the packaged wheel and sdist contain `codex_deepseek_team/data/apparmor/deepseek-team-bwrap` and the new `sandbox.py` module.
- The sandbox unit suite verifies direct Bubblewrap selection, AppArmor fallback selection, fail-closed behavior, required Bubblewrap feature checks, Claude read-only/read-write mount modes, real-HOME masking, temporary-HOME binding, credential-store masking, refusal to expose the entire real HOME as a checkout, private Codex bwrap shims, nested-userns suppression, and conservative AppArmor install/remove semantics.
- Worker integration tests verify that required OS isolation is resolved before the DeepSeek credential is read; Claude commands are outer-wrapped in Bubblewrap; Claude writer mode exposes the worktree read-write only for the isolated writer; and Codex keeps its native sandbox while receiving a private verified/AppArmor-aware `bwrap` shim through the temporary worker PATH.
- A final security review checked current Codex Linux sandbox behavior and found that Codex creates the initial user namespace but does not itself pass Bubblewrap `--disable-userns`. A regression was added first: GitHub Actions run **35121194903** at `c9445cc5a2212aeff564e78d2118191ac04668fe` ran 159 tests and failed exactly the three new Codex-shim expectations while the rest of the suite remained green apart from the two normal real-Codex skips. The production shim now injects `--disable-userns` exactly once, or preserves an already supplied copy, so the AppArmor `userns` permission is not left available for nested user namespaces inside the worker payload.
- Sandbox CLI tests verify `status`, `install-apparmor`, and `remove-apparmor`; safe error mapping; doctor ordering and policy propagation; and that status output does not expose provider credentials. Installer tests verify `--with-sandbox` is explicit, Ubuntu-specific, and invokes system setup only when requested, while a plain install never invokes `sudo` or AppArmor lifecycle commands.
- Managed coordinator tests verify both `AGENTS.md` and `CLAUDE.md` instruct coordinators to keep the OS sandbox enabled and never add `--os-sandbox off` to normal worker commands.
- Existing 0.2 regression coverage remains in place for Codex provider routing, temporary coordinator homes, credential isolation/redaction, malformed-output rejection, retry/cancellation behavior, Claude `--bare` tool restrictions and path-scoped writer permissions, exact Git allowlists, index/HEAD/branch integrity, symlink/hardlink defenses, filter/fsmonitor/index-flag hardening and reversible project instructions.

## Live Ubuntu AppArmor + Bubblewrap smoke

A dedicated `ubuntu-24.04` GitHub Actions job ran against Ubuntu **24.04.5** with the distribution packages installed by APT. The exact `ce4c383…` smoke used Bubblewrap **0.9.0-1ubuntu0.1** and AppArmor **4.0.1really4.0.1-0ubuntu0.24.04.7**.

The hosted Ubuntu environment reported:

```text
kernel.apparmor_restrict_unprivileged_userns = 1
```

The smoke job then completed all of the following successfully:

1. `apparmor_parser -Q -K src/codex_deepseek_team/data/apparmor/deepseek-team-bwrap` — packaged profile syntax compiled without kernel load or cache writes.
2. `deepseek-team sandbox install-apparmor` — the named profile was installed and loaded.
3. `deepseek-team sandbox status` — selected the **apparmor** backend and reported `/usr/bin/bwrap`, `kernel.apparmor_restrict_unprivileged_userns=1`, and `deepseek-team-bwrap` selected through `aa-exec`.
4. An explicit `aa-exec -p deepseek-team-bwrap -- bwrap ... --disable-userns ... /usr/bin/true` user-namespace/process sandbox probe completed successfully.

This is real evidence that the packaged named profile permits Bubblewrap on an Ubuntu 24.04 hosted kernel while the AppArmor unprivileged-userns restriction remains enabled. It is not a claim that every Ubuntu kernel or local administrator policy is identical; `deepseek-team sandbox status` remains the host-specific gate.

## Deliberate limits

- This release remains **Linux-only**. Windows support is intentionally deferred rather than weakening the process/filesystem/writer guarantees.
- The 0.3.0 work did **not** make a billable live DeepSeek inference request and did not execute a real Claude Code or Codex CLI binary in the release matrix. The two tests requiring a real Codex CLI are explicitly skipped when Codex is absent; Claude protocol behavior is exercised through a synthetic CLI boundary. Use `deepseek-team doctor --runtime <codex|claude|both> --live` on the target host for end-to-end provider validation.
- Current Codex source was separately checked during development to confirm that its Linux sandbox uses Bubblewrap and selects a suitable `bwrap` from PATH. The release therefore supplies a private verified/AppArmor-aware bwrap shim to Codex instead of nesting Codex inside another user namespace. The shim also injects `--disable-userns` exactly once. This compatibility point should be re-verified if upstream Codex changes its Linux sandbox architecture.
- The outer Claude Bubblewrap sandbox intentionally keeps the host network namespace because the coordinator CLI must reach the DeepSeek API. The feature does **not** claim network isolation. Claude still exposes no Bash/web/agent tools to the worker and explicitly denies MCP tools.
- The named AppArmor profile is intentionally unconfined for ordinary resources and grants `userns`; Bubblewrap provides the actual filesystem/process/capability restrictions. DeepSeek Team never disables `kernel.apparmor_restrict_unprivileged_userns` globally and never replaces a different administrator/distro AppArmor policy.
- Bubblewrap/AppArmor materially strengthen host isolation but do not make arbitrary hostile repositories safe. The worktree is intentionally visible to the worker, and runtime files required to start the coordinator may be re-exposed read-only. Secrets stored inside delegated project content should be treated as readable project data. Use a dedicated OS user/container/VM for a stronger confidentiality boundary.
- `WriteScope` remains the acceptance boundary for writer output. Rejected or partial changes are preserved for coordinator inspection; writers never automatically retry, stage, commit, push or deploy.
- `--os-sandbox off` is an explicit unsafe compatibility/diagnostic bypass. Managed project instructions never invoke it, and no automatic fallback to it exists.
- No speed, token-saving or cost percentage is claimed. Delegation economics depend on task size, supplied context, retries and coordinator review.
- Keys, parent conversation history, primary coordinator auth and raw provider logs are not part of the repository or distributions.
