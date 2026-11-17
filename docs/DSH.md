# Qwen coordinator in the DeepSeek Harness (DSH)

DeepSeek Team supports three coordinator runtimes: **Codex**, **Claude Code**
and **Qwen** — the Qwen model (for example Qwen 4 Max) acting as the coding
agent inside the [DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness).
This guide covers the Qwen/DSH integration only; the delegation contract,
routing, worker isolation and lifecycle are identical for every runtime and are
documented in the [README](../README.md) and the [routing guide](ROUTING.md).

## Architecture

DSH is a cordis-based plugin host, not a CLI with a Codex/Claude-style hooks
file. The package therefore ships a **bridge plugin**
(`codex_deepseek_team/data/dsh-bridge/index.js`, dependency-free ESM) and
installs one owned inserted plugin row into the active DSH profile's
`cordis.patch.yml`:

```yaml
# deepseek-team:qwen:start
- insert:
    - id: deepseek-team-qwen
      name: "/abs/path/to/codex_deepseek_team/data/dsh-bridge/index.js"
      config:
        entrypoint: "/abs/path/to/deepseek-team"
        runtime: qwen
# deepseek-team:qwen:end
```

DSH applies a patch row that carries `id` but no `insert` as an **update** of
an existing entry, so a brand-new plugin has to be inserted explicitly: the
owned block is one `insert:` patch adding the bridge row, whose `name` is the
absolute filesystem path of the packaged bridge module. No pnpm invocation,
network access or `package.json` change is involved. Every byte outside the
marker block is preserved; the write is atomic with a concurrent-change check,
and symlinked or non-regular patch files are refused. A foreign row that
already uses the id `deepseek-team-qwen` outside the markers is diagnosed,
never adopted.

The bridge binds **root agents only** and translates DSH events into
`deepseek-team coordinator-hook --runtime qwen` subprocess calls (JSON stdin,
bounded stdout/stderr, 10 s timeout by default):

| DSH event | Hook event | Decision applied |
|---|---|---|
| `agent/created` (`startup`/`resume`/`clear`/`compact`) | `SessionStart` | `additionalContext` via `agent.inject()` |
| `agent/pre-step` with a fresh `source.kind === 'user'` message | `UserPromptSubmit` | context appended to the admitted step batch; duplicate message ids are ignored |
| `tools/pre-execute` | `PreToolUse` | `permissionDecision: deny` → `{kind:'deny'}`; an early-planning nudge is delivered after the call via `tools/post-execute` `additionalContexts` |
| `agent/turn-stopping` | `Stop` | `decision: block` → `agent.steer(reason)` keeps the turn open; a repeated stop (`stop_hook_active`) only warns |

Native DSH subagents, forks and team members are child agents (registry
ownership or session header lineage): they are never bound, so they cannot open
or complete the coordinator ledger. Dispatching them from the root agent still
passes the native-delegation gate and needs the registered
`[deepseek-team:TASK_ID:DELIVERABLE_ID]` marker.

### Fail-closed semantics

- A failed or timed-out `PreToolUse` hook **denies the tool call** (Claude
  parity).
- A failed `UserPromptSubmit` hook never rejects the step — a DSH pre-step
  rejection would consume the user prompt without processing it. Instead the
  session is marked *ledger-down*: every tool call is denied until a later
  prompt initializes the ledger again, and the model sees a diagnostic notice.
- `SessionStart` and `Stop` hook failures are nonblocking.
- The bridge never reads credentials and never logs payloads.

### Coverage limits

The bridge covers the events DSH actually delivers: recognized read/edit/write,
bash and `terminal_send` mutations, and native dispatch tools (`subagent`,
`subagent_fork`, `review_code`, `spawn_teammate`, `send_message`, `workflow`,
`ralph`). It is **not** a universal interception layer; arbitrary plugin
activity outside these events is not gated, and the instructions remain
authoritative there. DSH plan mode has no native plan-file exception: mutations
stay gated (the harness itself also blocks them). The mandatory worker OS
sandbox is unchanged.

## Workers: the native DeepSeek loop (no Codex/Claude required)

The Qwen coordinator runs inside DSH; DSH is not a launchable worker CLI. The
package therefore ships its own **native worker runtime** (`--runtime qwen`): a
self-contained, stdlib-only Python loop (`native_worker.py`) that talks to the
DeepSeek Responses API directly. The Qwen + DeepSeek combination works with
**no Codex CLI and no Claude Code CLI installed**.

- Read-only workers expose `read_file`, `glob_files` and `grep_search`, confined
  to the checkout (path/credential guards), and run inside the same
  `sandbox.wrap_command` Bubblewrap boundary as Claude workers.
- Full-access workers add `write_file`, `edit_file` and `run_shell` inside the
  managed development copy. The managed boundary copies the single-file loop
  into its private control directory and runs it with `python -I` inside the
  sparse, network-isolated namespace; provider traffic passes only through the
  fixed `api.deepseek.com` relay, exactly like the CLI harnesses.
- The loop emits one Claude-shaped result object, so result parsing, checks,
  capture, lifecycle and lessons are shared with every runtime. It never
  retries implementation; read-only transient retries belong to the outer
  runner.
- `worker --runtime auto` tries Codex, then Claude Code, then the native qwen
  loop, so existing coordinators also gain a CLI-free fallback. `--qwen` pins
  the interpreter explicitly (default: the running interpreter).

The coordinator's own model and authentication (for example `qwen4-max`) stay
entirely under your DSH configuration — the package never reads or changes them.

## Installation

```bash
# 1. Prepare the bridge for the detected DSH profile.
deepseek-team setup --runtime qwen --no-key
# 2. Store the DeepSeek key for workers (hidden prompt; never echoed).
deepseek-team auth set
# 3. Attach the project. Qwen shares the AGENTS.md managed block with Codex.
deepseek-team init --coordinator qwen /path/to/project
# 4. Optional: allow implementation inside an isolated development copy.
deepseek-team config set --project --access full-access
# 5. Offline readiness check.
deepseek-team doctor --runtime qwen --offline
```

Profile discovery: `DEEPSEEK_TEAM_DSH_PROFILE=<name>` selects one profile under
`$DSH_HOME/profiles` (default `~/.dsh`); without it the single existing profile
is used. Several profiles are an actionable error, never a guess.

**Loading the plugin.** The shipped `web` profile reloads configuration
changes through host HMR; other profiles apply patches at startup. After
`hooks install --runtime qwen`, reload the profile or restart DSH, then start a
new session. Verify with:

```bash
deepseek-team hooks status --runtime qwen
```

The package cannot inspect or grant the native profile load; confirmation
remains pending until you reload/restart and start a new session.

## Shared AGENTS.md block

DSH natively reads `AGENTS.md` (and `CLAUDE.md`), so Qwen shares the `AGENTS.md`
managed block with Codex: one file holds one block. `init --coordinator qwen`
renders it for Qwen; a later Codex attachment re-renders it for Codex, and
`--coordinator all` renders the shared file once for the first selected runtime
(Codex). This is safe because every coordinator's lifecycle hooks inject the
complete current policy for their own runtime at session start and on each
prompt — the shared file snapshot never governs a session alone. `both` keeps
its historical meaning (Codex + Claude Code); `all` adds Qwen.

## Updates, refresh and removal

```bash
deepseek-team hooks refresh --runtime qwen --notify --json   # after a package upgrade
deepseek-team hooks remove --runtime qwen                    # owned bridge block only
deepseek-team reset --runtime qwen                           # same, via reset
deepseek-team detach --coordinator qwen /path/to/project     # managed block only
```

Refresh migrates only the owned marker block (including a stale entrypoint
recorded by an older installation and a row written in the pre-`insert:` bare
shape) and reports the profile reload/restart as a pending user action. Foreign
patch rows, bundles, credentials and DSH settings are preserved.

## Tests

- Python: `tests/test_qwen_registration.py`, `tests/test_qwen_hooks.py`,
  `tests/test_qwen_cli.py`, `tests/test_qwen_native_worker.py` (part of the
  standard unittest discovery). The native-worker suite includes real managed
  sandbox executions of the loop against a local fixture provider — no public
  endpoint or real credential is used.
- Bridge: `node --test tests/dsh_bridge/bridge.test.mjs` — a fake cordis
  context plus a fixture hook entrypoint; no DSH installation is required.
  `tests/test_qwen_bridge.py` runs it from unittest discovery when Node is on
  PATH or `DEEPSEEK_TEAM_NODE` points at a Node executable; otherwise it skips.