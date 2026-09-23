# Universal Coordinators Implementation Plan

> Historical record: this document describes an earlier implementation or verification run. Commands, defaults and compatibility claims here are not current instructions. See [README](https://github.com/kirill31337/deepseek-team/blob/main/README.md) and the current routing guide for supported behavior.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the Linux package support Codex and Claude Code coordinators delegating read-only and writer tasks to DeepSeek through their native CLI harnesses.

**Architecture:** Keep one generic worker lifecycle and common Git writer verifier. Add runtime dispatch inside the worker for Codex and Claude Code, then make project instructions/setup/doctor runtime-aware while preserving Codex defaults.

**Tech Stack:** Python 3.11+ stdlib, Git, Codex CLI, Claude Code CLI, DeepSeek Responses/Anthropic-compatible APIs, unittest, GitHub Actions.

**Spec:** `docs/superpowers/specs/2026-09-15-universal-coordinators-design.md`

## Global Constraints

- Linux only.
- No Python runtime dependencies.
- Existing Codex behavior is the default and must remain backward-compatible.
- Never persist or expose DeepSeek, OpenAI or Anthropic credentials.
- Writers never stage, commit, push, deploy, run builds/tests or retry automatically.
- Existing rejected/partial writer work is preserved.

---

### Task 1: Claude runtime boundary

**Files:**
- Modify: `src/codex_deepseek_team/worker.py`
- Create: `tests/test_claude_runtime.py`

**Interfaces:**
- `command(binary, write_paths=(), runtime='codex') -> list[str]`
- `child_environment(home, key, runtime='codex') -> dict[str, str]`
- `resolve_runtime(requested, codex='codex', claude='claude') -> tuple[str, str]`
- `runtime_result(runtime, raw) -> tuple[str, str, bool]`

- [ ] Add failing fake-Claude subprocess tests proving bare mode, restricted tools, isolated credentials and JSON-only result handling.
- [ ] Run the focused tests and verify they fail because runtime dispatch does not exist.
- [ ] Implement Claude command/environment/result parsing while preserving Codex wrappers.
- [ ] Run worker, hardening and Claude runtime tests.

### Task 2: Coordinator-native project instructions

**Files:**
- Modify: `src/codex_deepseek_team/project.py`
- Modify: `src/codex_deepseek_team/data/delegation.md`
- Create: `tests/test_coordinators.py`

**Interfaces:**
- `attach(root, coordinator='codex') -> bool`
- `detach(root, coordinator='codex') -> bool`
- coordinator values: `codex`, `claude`, `both`

- [ ] Add failing tests for `CLAUDE.md`, both targets, byte preservation and runtime-specific command text.
- [ ] Verify failure against the current one-target implementation.
- [ ] Generalize managed-file operations without changing the default Codex target.
- [ ] Run all project tests.

### Task 3: Universal CLI, setup and diagnostics

**Files:**
- Modify: `src/codex_deepseek_team/cli.py`
- Modify: `src/codex_deepseek_team/doctor.py`
- Modify: `src/codex_deepseek_team/write_scope.py`
- Modify: `pyproject.toml`
- Modify: `install.py`
- Modify: `tests/test_cli.py`
- Modify: `tests/test_codex_deepseek_checks.py`
- Modify: `tests/test_codex_deepseek_write.py`

**Interfaces:**
- `worker --runtime codex|claude|auto`
- `init|detach --coordinator codex|claude|both`
- `setup|reset|doctor --runtime codex|claude|both|auto`
- console aliases `codex-deepseek-team` and `deepseek-team`

- [ ] Add failing CLI/doctor/writer compatibility tests.
- [ ] Implement runtime-aware setup/doctor and neutral disable switch.
- [ ] Allow dedicated `deepseek/` writer branches while retaining `codex/`.
- [ ] Add the neutral console alias and installer link without removing the legacy command.
- [ ] Run the full unittest suite and wheel build/install checks.

### Task 4: Documentation and release verification

**Files:**
- Modify: `README.md`
- Modify: `AGENTS.md`
- Modify: `docs/VERIFICATION.md`

- [ ] Document Codex-only, Claude-only and both-coordinator setup flows, including the fact that worker DeepSeek auth is independent from coordinator auth.
- [ ] Document Claude tool restrictions and the shared writer verification boundary.
- [ ] Run GitHub Actions on Python 3.11/3.12/3.13.
- [ ] Review the final diff and fast-forward `main` only after the matrix is green.
