# AppArmor + Bubblewrap Worker Isolation Implementation Plan

> Historical record: this document describes an earlier implementation or verification run. Commands, defaults and compatibility claims here are not current instructions. See [README](https://github.com/kirill31337/deepseek-team/blob/main/README.md) and the current routing guide for supported behavior.

> **For agentic workers:** use superpowers:executing-plans/TDD and verify before completion.

**Goal:** require a working Bubblewrap/AppArmor isolation path for DeepSeek workers on Linux, with conservative Ubuntu setup and no global weakening of AppArmor user-namespace policy.

**Spec:** `docs/superpowers/specs/2026-09-16-apparmor-bwrap-sandbox-design.md`

## Architecture correction discovered during implementation

Current Codex on Linux already constructs its own Bubblewrap sandbox and prefers the first suitable `bwrap` found on `PATH`. An early plan to place **both** Codex and Claude Code inside an outer `bwrap --disable-userns` was therefore rejected: that would prevent Codex from creating its own user namespace.

Final implementation is hybrid:

- **Codex:** probe a working direct/AppArmor-aware Bubblewrap backend, then put a private `bwrap` shim first on the temporary worker PATH. Codex itself builds the normal `read-only` / `workspace-write` sandbox through that verified executable.
- **Claude Code:** run the entire isolated `--bare` worker harness inside DeepSeek Team's outer Bubblewrap namespace.
- **Both:** keep the shared `WriteScope` Git verifier and fail closed before DeepSeek credentials are read when required OS isolation is unavailable.

The package never writes `kernel.apparmor_restrict_unprivileged_userns=0` and never overwrites foreign/administrator-modified AppArmor policy.

## Task 1 — capability layer and named AppArmor profile

- [x] Add RED tests for direct bwrap, AppArmor fallback, missing features and fail-closed behavior.
- [x] Add `src/codex_deepseek_team/sandbox.py`.
- [x] Ship named no-attachment `deepseek-team-bwrap` profile with `flags=(unconfined)` + `userns`.
- [x] Probe direct bwrap first and use `aa-exec -p deepseek-team-bwrap` only when Ubuntu AppArmor userns mediation blocks it.
- [x] Add Claude mount layout: read-only root, fresh process/user/IPC/UTS namespaces, dropped capabilities, private tmp, hidden real HOME, runtime-root re-exposure, credential masks, temporary HOME, RO/RW worktree.
- [x] Add private Codex bwrap shim for direct/AppArmor backends.
- [x] Add conservative profile install/remove functions; refuse foreign/symlink/modified policy.
- [x] Package AppArmor data in wheel/sdist.

## Task 2 — worker integration

- [x] Add RED tests proving sandbox resolution occurs before key reads.
- [x] Add `--os-sandbox required|off`, default `required`.
- [x] Keep explicit `off` warning-only and never make it an automatic fallback.
- [x] Claude: outer-wrap the runtime command and map writer mode to RW worktree mount.
- [x] Codex: preserve native sandbox and prepare its private bwrap PATH shim instead of double-wrapping.
- [x] Give both runtimes a temporary HOME.
- [x] Preserve output validation, retry behavior, cancellation and `WriteScope` result verification.
- [x] Keep synthetic legacy transport tests explicit with `--os-sandbox off`; dedicated sandbox tests cover the secure default.

## Task 3 — CLI, doctor and Ubuntu installer

- [x] Add RED tests for `sandbox` CLI, doctor propagation and explicit installer setup.
- [x] Add `deepseek-team sandbox status`.
- [x] Add `sandbox install-apparmor` and `sandbox remove-apparmor`.
- [x] Make `doctor` verify required OS containment before coordinator-runtime checks.
- [x] Propagate sandbox policy into live synthetic worker calls.
- [x] Add `python3 install.py --with-sandbox` for Ubuntu: explicit `sudo apt-get install -y bubblewrap apparmor`, named-profile install, then status probe.
- [x] Keep plain installer rootless; never invoke sudo unless `--with-sandbox` is explicitly requested.

## Task 4 — release 0.3.0 and documentation

- [x] RED assertions for version 0.3.0 and managed OS-sandbox instruction.
- [x] Bump package/module version to 0.3.0.
- [x] Managed AGENTS/CLAUDE guidance requires the OS sandbox and forbids normal use of `--os-sandbox off`.
- [x] README documents Ubuntu setup, named-profile collision avoidance, hybrid Codex/Claude architecture, network limitation and stronger-isolation caveat.
- [x] HARDENING documents the 0.3 boundary.
- [x] Add Ubuntu Bubblewrap/AppArmor CI smoke attempt.
- [x] Run Python 3.11/3.12/3.13 suite, wheel/sdist install and both CLI aliases on the release candidate.
- [x] Record verification evidence in `docs/VERIFICATION.md`, including the live Ubuntu named-profile + Bubblewrap probe.
- [ ] Compare the final branch to `main` and fast-forward only after the exact final SHA passes the mandatory matrix and sandbox-smoke jobs.

## Release verification commands

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
python -m build
python -m pip install dist/*.whl
codex-deepseek-team --version
deepseek-team --version
```

CI additionally runs on Ubuntu 24.04:

```bash
apparmor_parser -Q -K src/codex_deepseek_team/data/apparmor/deepseek-team-bwrap
deepseek-team sandbox install-apparmor
deepseek-team sandbox status
aa-exec -p deepseek-team-bwrap -- bwrap ... /usr/bin/true
```

The release candidate demonstrated a working AppArmor backend with `kernel.apparmor_restrict_unprivileged_userns=1`; the final documentation-only SHA is still required to pass the same CI before `main` moves.
