# AppArmor + Bubblewrap Worker Isolation Design

> Historical record: this document describes an earlier implementation or verification run. Commands, defaults and compatibility claims here are not current instructions. See [README](https://github.com/kirill31337/deepseek-team/blob/main/README.md) and the current routing guide for supported behavior.

## Goal

Add a fail-closed Linux OS isolation layer to DeepSeek workers with first-class Ubuntu AppArmor support, while preserving the coordinator-native sandbox architecture of both Codex and Claude Code and the shared Git writer verifier.

## Security model

The OS layer is **hybrid**, because current Codex on Linux already creates its own Bubblewrap/user-namespace sandbox:

- **Codex runtime:** keep Codex `read-only` / `workspace-write` as the actual process/filesystem sandbox. DeepSeek Team first probes a known-working `bwrap` backend, then places a private `bwrap` shim at the front of the temporary worker `PATH`. The shim invokes either the probed system `bwrap` directly or `aa-exec -p deepseek-team-bwrap -- bwrap`, and guarantees `--disable-userns` exactly once. This makes Codex's own native sandbox use the verified/AppArmor-aware executable without nesting Codex inside another user namespace, while preventing further user namespaces inside the completed worker sandbox.
- **Claude Code runtime:** run the whole isolated `--bare` Claude harness inside an outer DeepSeek Team Bubblewrap namespace. Claude keeps its restricted built-in tools, path-scoped writer permissions, no Bash/web/agents and explicit MCP denial.
- **Both runtimes:** `WriteScope` remains the result-acceptance boundary for writer work and still verifies HEAD, branch, index and changed paths after execution.

A usable Bubblewrap backend is required before the DeepSeek credential is read. There is no automatic unsandboxed fallback. `--os-sandbox off` is an explicit unsafe compatibility/diagnostic escape hatch and prints a warning; package-managed coordinator instructions never use it.

## Why Codex is not outer-wrapped

Putting current Codex inside an outer `bwrap --disable-userns` would block Codex from constructing its own inner Linux sandbox. Allowing arbitrary nested user namespaces just to make double-bwrap work would weaken the intended boundary. Therefore DeepSeek Team verifies and supplies the `bwrap` path that Codex itself uses rather than wrapping Codex a second time.

The private shim applies `--disable-userns` only to the Bubblewrap invocation itself. If upstream Codex already supplies the flag, the shim detects it and does not add a duplicate. This lets Codex create the one required sandbox user namespace but prevents processes inside that sandbox from opening additional user namespaces afterwards.

This preserves Codex's native sandbox semantics and avoids a second namespace layer whose behavior could drift from Codex releases.

## Claude Bubblewrap layout

The Claude worker harness uses:

- `--die-with-parent`
- `--new-session`
- `--unshare-user`
- `--unshare-pid`
- `--unshare-ipc`
- `--unshare-uts`
- `--unshare-cgroup-try` when supported
- `--disable-userns`
- `--cap-drop ALL`
- read-only root filesystem
- fresh `/proc` and minimal `/dev`
- private tmpfs `/tmp` and `/var/tmp`
- temporary worker HOME read-write
- repository/worktree read-only for review and read-write for writer mode
- real user HOME masked by tmpfs; only top-level runtime roots needed by the resolved executable/PATH are re-exposed read-only, followed by explicit masks for common credential stores

Bubblewrap resolves bind sources from its preserved host root, so a host path can be re-exposed after the visible HOME has been replaced by tmpfs.

The Claude API client must still reach DeepSeek. The outer sandbox therefore intentionally keeps the host network namespace. This feature **does not claim network isolation**; model-facing network capability remains denied by the Claude tool surface and Codex's own sandbox rules.

## Ubuntu AppArmor integration

Ubuntu 24.04+ can mediate unprivileged user namespace creation through AppArmor. DeepSeek Team never disables `kernel.apparmor_restrict_unprivileged_userns` globally.

To avoid collisions with distro or third-party profiles attached directly to `/usr/bin/bwrap`, the package ships a named profile with no executable attachment:

```text
profile deepseek-team-bwrap flags=(unconfined) {
  userns,
}
```

The profile is selected only for our Bubblewrap path through:

```text
aa-exec -p deepseek-team-bwrap -- /usr/bin/bwrap ...
```

The AppArmor profile's narrow role is to permit the initial user namespace on hosts where the Ubuntu restriction blocks direct Bubblewrap. Bubblewrap itself supplies the mount/process/capability sandbox. For the outer Claude sandbox, `--disable-userns` prevents the payload from creating further user namespaces. For Codex, the AppArmor-aware bwrap shim both selects the profile and guarantees `--disable-userns` on Codex's own native Bubblewrap invocation.

Backend selection is capability based:

1. Find `bwrap`, verify the required options, and probe direct namespace creation.
2. If direct bwrap is blocked and `kernel.apparmor_restrict_unprivileged_userns=1`, probe the named profile through `aa-exec`.
3. If neither path works, fail closed with remediation instructions.

No distro `/usr/bin/bwrap` profile is overwritten and no global sysctl is changed.

## System setup and removal

`sandbox.py` owns probing, Claude command wrapping, the Codex bwrap shim, and conservative AppArmor lifecycle operations.

`deepseek-team sandbox status` reports the selected bwrap path/backend and the AppArmor userns restriction value when available. It never reads or displays provider credentials.

`deepseek-team sandbox install-apparmor` installs/reloads only the exact package-owned profile using `apparmor_parser -r`. A symlink, non-file, or different existing policy is refused.

`deepseek-team sandbox remove-apparmor` unloads/removes only an installed file whose bytes still exactly match the packaged profile. Administrator-modified policy is never deleted automatically.

On Ubuntu, `python3 install.py --with-sandbox` explicitly installs the `bubblewrap` and `apparmor` packages via `sudo apt-get install -y`, installs the named profile, then runs `sandbox status`. Plain `python3 install.py` remains rootless and never invokes sudo; workers remain fail-closed until `sandbox status` succeeds.

## Compatibility

- Linux and Python 3.11+ remain required.
- Existing Codex/Claude runtime selection and coordinator instruction files remain compatible.
- Existing credential locations, environment switches, Python distribution name/namespace, and legacy `codex-deepseek-team` CLI remain compatible.
- Version becomes `0.3.0` because a usable OS sandbox is now required by default.
- No Python runtime dependency is added; Bubblewrap/AppArmor are system dependencies.
- Non-Ubuntu Linux can use a directly working Bubblewrap without the package AppArmor profile.

## Error handling

Sandbox setup failures contain no environment values or raw provider output. Required containment is resolved before the DeepSeek credential is read. Timeouts and cancellation still terminate the whole launched process group.

The system setup path is conservative: no global sysctl edits, no replacement of foreign AppArmor profiles, no automatic removal of modified policy, and no silent fallback to unsandboxed workers.

## Testing

Regression coverage includes:

- direct Bubblewrap and AppArmor-fallback selection;
- required feature probing and fail-closed behavior;
- Claude read-only/read-write worktree mount modes;
- real HOME masking, temporary HOME binding, runtime-root re-exposure and credential-store masks;
- private direct/AppArmor Codex `bwrap` shims, including exact-once `--disable-userns` injection;
- worker sandbox resolution before API-key reads;
- outer Claude wrapping versus native Codex sandbox preparation;
- `--os-sandbox off` as an explicit-only bypass;
- AppArmor install/remove refusal for foreign, symlinked or modified policy;
- `sandbox` CLI, doctor propagation and Ubuntu installer flow;
- package data containing the profile;
- the complete pre-0.3 regression suite.

CI keeps deterministic unit coverage on Python 3.11-3.13 and adds an Ubuntu Bubblewrap/AppArmor smoke attempt. Hosted-kernel limitations must be reported explicitly rather than represented as proof of enforcement.
