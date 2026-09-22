# Worker boundary hardening

## 0.5.0 — coordinator process enforcement (2026-09-22)

0.5.0 adds a coordinator-process guardrail on top of the existing worker sandbox. These are separate boundaries and must not be conflated.

For **Codex**, DeepSeek Team installs one stable user-level lifecycle hook definition. It is active only when the current repository already contains the package-owned managed Codex block created by explicit `deepseek-team init --coordinator codex`. Codex owns native hook trust; DeepSeek Team neither bypasses nor infers that decision.

The supported technical enforcement points are:

- `SessionStart` / `UserPromptSubmit`: inject or restore the persistent task ledger, including after compaction;
- `PreToolUse`: deny supported coordinator mutation calls before execution when no compliant distribution exists, when scope is unplanned, or when a pending worker assignment owns the path;
- `Stop`: block silent completion while assignments are pending or completed worker results have not been dispositioned.

These hooks are **not** claimed to intercept every possible Codex implementation path. Specialized or future tool surfaces that do not traverse the supported hook path remain an environment limitation and must be re-verified against upstream Codex. The release CI therefore runs a real Codex new-session scenario and checks an observed PreToolUse denial before the attempted file mutation reaches disk.

For **Claude Code**, 0.5.0 does not claim equivalent coordinator-side technical gating. Managed instructions and the persistent ledger/worker accounting are supplied, while coordinator compliance with work distribution remains instruction-driven. Worker filesystem/network permissions remain technically enforced by the existing sandbox.

Coordinator-native subagents are deliberately a **coordinator capability**, not a DeepSeek-worker capability. A planned native-agent deliverable requires a concrete `delegation_reason`, cannot own protected coordinator responsibilities, and does not satisfy DeepSeek worker requirements in the 50/75 profiles. Native subagents execute under Codex/Claude's own native agent model, permissions and sandboxing; DeepSeek Team does not wrap them in the DeepSeek worker Bubblewrap boundary. Conversely, DeepSeek workers keep agent tools disabled and remain leaf workers.

DeepSeek workers stay fixed to `deepseek-flash`. The frontier coordinator selects only `low`/`medium`/`high` reasoning effort per DeepSeek assignment; that selected value is propagated consistently through the Codex or Claude harness and recorded for coordinated managed assignments.

### Persistent coordination state

The ledger is stored under the user's private DeepSeek Team state directory, outside the repository. It records only the minimum process state needed for continuity: session/task/deliverable/assignment ids, executor/scope/acceptance/dependencies/checks, native delegation reasons, selected DeepSeek effort, workspace id, coordinator-prepared inputs, worker-only delta, result summary, checks, result disposition and structured constraints.

It is not a scheduler. It does not scan arbitrary projects, auto-attach repositories, commit, push, publish or deploy. Existing workspace records remain the source of truth for owned development copies.

At 75/full-access, free-form reasons such as "quality ownership" or "release work" do not technically satisfy the distribution gate. Technical retention reasons such as missing dependencies must be recorded from the runner/preflight. Signing/secrets remain coordinator-only through a structured sensitive marker. Semantic inseparability is not presented as a deterministic proof: genuinely small work uses the explicit small-task classification and is structurally restricted to one concrete scope.

### Workspace readiness and attribution

A coordinated assignment checks:

1. workspace base HEAD matches the task base;
2. declared paths/commands/check executables exist;
3. the same requirements are observable **inside the actual sparse Bubblewrap namespace**;
4. only then may the provider credential be read.

A host-only JDK/SDK/tool therefore does not become available merely because access is full-access. Missing dependencies must be prepared explicitly inside the owned copy. Stubs are not treated as equivalent project verification.

If selected dirty source is needed, `workspace import --include FILE` copies only explicitly named ordinary non-secret files. The imported baseline is recorded as coordinator-prepared input. The runner snapshots content immediately before worker execution and records only the delta after that snapshot as worker changes, avoiding false worker authorship for prepared source.

Declared post-worker checks run inside the same sparse/no-network sandbox and are stored with exit codes. Worker result summaries and review findings survive compaction/continuation through the ledger; the coordinator must record an explicit disposition before the task can finish.

## 0.4.0 — configurable delegation and managed development copies (2026-09-21)

The 25/50/75 delegation levels are policy for **how the coordinator distributes work**, not a security primitive and not a measured utilization percentage. Security-relevant access is resolved separately as `auto | read-only | full-access` with precedence `CLI > project > global > defaults`. The resolved policy is snapshotted for each new job.

`read-only` remains enforced by launch permissions. In managed-copy mode the whole runtime is inside a Bubblewrap namespace with the project copy mounted read-only; model instructions are additional guidance, not the write barrier.

`full-access` means development access to one package-owned copy only. It is **not** host full access. The managed boundary for both Codex and Claude uses:

- a sparse mount namespace rather than a read-only bind of the host root;
- only required system/runtime files plus the assigned copy, temporary HOME and control directory;
- `--unshare-user --unshare-pid --unshare-ipc --unshare-uts --unshare-net`;
- dropped capabilities and disabled nested user namespaces;
- the working tree writable, but its `.git` administrative directory remounted read-only;
- no visibility of the original checkout or sibling workers;
- no direct network route to provider or host services;
- a per-run Unix-socket provider capability with a fixed DeepSeek destination and endpoint allowlist;
- the real DeepSeek credential retained only in the host-side relay.

The runtime may create/edit/delete arbitrary project files and run local tests/builds inside that copy. This is intentionally broader than the legacy exact-file writer, but publication, deployment, production services, secrets and Git integration/commits remain coordinator-owned.

Owned workspaces are created from committed HEAD. Source dirty/untracked/ignored files are neither cleaned nor silently copied. Reopening a copy verifies its identity and Git administrative digest; another worker cannot take an active copy because an exclusive owner lock is required. A failed/interrupted copy is retained and cannot be resumed without explicit `--resume-after-failure`; no automatic implementation retry is performed over uncertain state.

The legacy `--write --allow-write` path remains unchanged for users who want an exact file allowlist. It continues to use the older writer rules and does not gain test/build permission.

Managed AGENTS.md/CLAUDE.md text describes the current policy but is not treated as containment. The worker and doctor independently resolve policy and verify the actual runtime/sandbox surface before access is granted.

## 0.3.0 — Bubblewrap + Ubuntu AppArmor (2026-09-16)

DeepSeek Team now requires a usable Linux Bubblewrap backend before a worker reads the DeepSeek credential. There is no automatic unsandboxed fallback. An explicit `--os-sandbox off` exists only for diagnosis/legacy compatibility, prints a warning, and is never emitted by managed `AGENTS.md` / `CLAUDE.md` instructions.

### Ubuntu user namespaces

Ubuntu can mediate unprivileged user namespace creation through AppArmor. DeepSeek Team does **not** disable `kernel.apparmor_restrict_unprivileged_userns` and does not replace a distro `/usr/bin/bwrap` attachment policy. It ships a named profile, `deepseek-team-bwrap`, with no executable attachment and selects it explicitly through `aa-exec` only when a direct Bubblewrap probe is blocked by the Ubuntu AppArmor userns restriction.

The profile is intentionally unconfined for ordinary resources and grants `userns`. Its role is to permit the initial namespace; Bubblewrap defines the actual process/mount/capability policy. Installation/removal is conservative:

- a different, symlinked or non-regular `/etc/apparmor.d/deepseek-team-bwrap` is refused;
- an exact package profile can be reloaded with `apparmor_parser -r`;
- removal is allowed only when installed bytes still exactly match the package copy;
- administrator-modified policy is never deleted automatically;
- no sysctl is changed.

### Codex: preserve the native Linux sandbox

Current Codex on Linux already builds its own Bubblewrap sandbox. DeepSeek Team therefore does not put Codex inside a second user namespace. It probes a working direct/AppArmor-aware `bwrap` backend, creates a private temporary `bwrap` shim and places it first on the worker `PATH`. Codex then constructs its normal `read-only` / `workspace-write` sandbox through that verified executable.

Because the AppArmor profile grants permission to create the **initial** user namespace, the private shim also guarantees Bubblewrap `--disable-userns` exactly once. If Codex already supplies the flag it is preserved without duplication; otherwise the shim injects it. This prevents processes inside the completed Codex sandbox from using the inherited AppArmor `userns` permission to create further user namespaces.

The shim contains only the executable/profile prefix and this fixed hardening rule; provider credentials remain in the sanitized child environment and are never written into the script or argv.

### Claude Code: outer Bubblewrap

The isolated Claude worker harness runs inside an outer Bubblewrap namespace. The policy uses a read-only root, fresh user/PID/IPC/UTS namespaces, dropped capabilities, private temporary directories, a temporary writable worker HOME, and a worktree mounted read-only for review or read-write for writer mode. The real user HOME is hidden and only runtime roots needed to start the CLI are re-exposed read-only; common credential locations are then masked again. The outer sandbox passes `--disable-userns`, so the Claude payload cannot create another user namespace after setup.

The outer Claude policy intentionally keeps the host network namespace because the CLI must reach the DeepSeek API. This feature does not claim network isolation. Claude still exposes no Bash/web/agent tools to the worker and explicitly denies MCP tools.

### What this does not prove

Bubblewrap/AppArmor significantly strengthen the host boundary, but this package is not a replacement for a separate OS user/container/VM for arbitrary hostile source trees. The worktree is intentionally visible to the worker, and runtime files required to start the coordinator can be exposed read-only. A secret committed or stored inside an allowed source tree should be treated as readable project data.

`WriteScope` remains the authoritative result acceptance boundary for writer work: exact allowed paths, clean linked worktree, index/HEAD/branch checks, and rejected partial work preservation are unchanged.

## 0.1/0.2 — Git and output integrity (2026-09-15)

### Writer verification

Ordinary `git diff` can miss protected-file edits when index entries use `assume-unchanged` or `skip-worktree`, or when `core.filemode=false` hides an executable-bit change. Inherited `GIT_INDEX_FILE` can redirect verification to a different index. DeepSeek Team therefore uses a minimal Git environment, ignores inherited index/global/system overrides, forces executable-bit checks, and refuses nonstandard index flags before execution and before accepting a result.

The linked-worktree `.git` pointer must not be a symlink/hardlink and must remain unchanged. Verification itself runs outside the model sandbox, so repository-configured `core.fsmonitor`, hooks, clean/process filters, external diff and textconv are disabled or refused as appropriate. Submodule index entries are refused rather than recursively inspected.

Writer mode requires an ordinary, clean, full linked worktree on a `codex/` or `deepseek/` branch. It refuses repositories containing submodule entries, nonstandard index flags, or tracked/allowed files using locally configured external clean/process filters. Admission failures return 78; detected result violations return 73. Rejected/partial files are preserved for coordinator inspection.

### Runtime and output integrity

Environment API keys receive the same ASCII/length/whitespace checks as saved keys. The state directory and worker locks must be private, current-user-owned regular filesystem objects; symlinked directories, FIFO locks, hardlinked locks and public locks are rejected without mutating them.

Malformed nonblank JSON events, invalid event types and invalid UTF-8 output are rejected without publishing candidate answers. Unknown named event types and blank lines remain forward-compatible. Failures never publish partial answers or raw malformed model output.

## Verification commands

```bash
deepseek-team sandbox status
deepseek-team doctor --runtime both --offline
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

`doctor --live` remains a separate, billable end-to-end check requiring a configured DeepSeek credential. Unit/CI tests use synthetic credentials and transports and must not be represented as proof of a live provider request.

## References

- Git index flags: https://git-scm.com/docs/git-ls-files
- Git attributes: https://git-scm.com/docs/gitattributes
- Git configuration: https://git-scm.com/docs/git-config
- Bubblewrap: https://github.com/containers/bubblewrap
- AppArmor unprivileged user namespaces: https://documentation.ubuntu.com/security/security-features/privilege-restriction/apparmor/
