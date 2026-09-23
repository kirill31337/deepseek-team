# Universal Codex / Claude + DeepSeek design

> Historical record: this document describes an earlier implementation or verification run. Commands, defaults and compatibility claims here are not current instructions. See [README](https://github.com/kirill31337/deepseek-team/blob/main/README.md) and the current routing guide for supported behavior.

## Goal

Keep one Linux package that lets either Codex or Claude Code coordinate bounded DeepSeek workers, including isolated code creation/editing, without requiring Claude users to install Codex or changing either coordinator's primary authentication.

## Compatibility

- Linux and Python 3.11+ remain the only supported platform in this release.
- Existing `codex-deepseek-team` commands and Codex behavior remain compatible by default.
- The neutral repository/primary CLI name is `deepseek-team`; the legacy Python distribution name, import namespace and CLI alias remain for upgrade compatibility.
- Keep the existing DeepSeek credential location and `CODEX_DEEPSEEK_DISABLED` switch for compatibility; add `DEEPSEEK_TEAM_DISABLED` as the neutral equivalent.
- Keep the shared three-worker limit, unlimited default deadline, single-attempt writer policy and post-run Git verification.

## Runtime architecture

A worker gets an explicit `--runtime codex|claude|auto` selector. The default remains `codex` so existing automation does not silently change harness. `auto` prefers Codex when both are installed, then Claude Code.

The Codex runtime keeps the current Responses API provider and temporary `CODEX_HOME` behavior.

The Claude runtime uses DeepSeek's official Anthropic-compatible endpoint only in the child process:

- `ANTHROPIC_BASE_URL=https://api.deepseek.com/anthropic`
- `ANTHROPIC_AUTH_TOKEN=<DeepSeek key>`
- DeepSeek model environment variables recommended by DeepSeek

Claude Code runs non-interactively with `--bare`, `--no-session-persistence` and JSON output. `--bare` prevents discovery of CLAUDE.md, hooks, skills, plugins, MCP servers, auto memory, OAuth and keychain credentials. The child HOME/config is temporary. Parent Anthropic/Claude credentials are not inherited.

Read-only Claude workers expose only `Read,Glob,Grep`. Writer workers expose only `Read,Glob,Grep,Edit,Write`; Bash, web tools, agents and external services are unavailable. `dontAsk` plus an explicit allowlist makes unattended edits possible without `bypassPermissions`.

## Writer boundary

Both runtimes share `WriteScope`. The worker must start in a clean linked worktree on a dedicated `codex/` or `deepseek/` branch with exact `--allow-write` paths. Runtime tool restrictions are an inner safety layer; Git verification remains authoritative for accepting a result. Rejected or partial work is preserved for coordinator inspection.

## Coordinator integration

`init --coordinator codex|claude|both` manages the same bounded-delegation policy in the coordinator-native instruction file:

- Codex -> `AGENTS.md`, invoking `worker --runtime codex`
- Claude Code -> `CLAUDE.md`, invoking `worker --runtime claude`
- both -> both files

Default `init` remains Codex-compatible. Attach/detach preserves every byte outside the package-owned managed block in each target file.

## Setup and diagnostics

`setup --runtime codex|claude|both|auto` stores the same private DeepSeek key. Codex setup also manages the existing provider block. Claude setup never edits the user's Claude configuration because the DeepSeek endpoint is injected only into worker child processes.

`doctor --runtime ...` performs runtime-specific CLI capability checks. Offline checks never read the key or access the network. Live checks use a synthetic repository and verify the selected DeepSeek runtime without modifying coordinator auth/config.

## Failure behavior

Missing requested runtimes fail closed with exit 78. Malformed JSON, empty successful results and invalid UTF-8 remain rejected. Retry behavior applies only to completed read-only runtime failures matching the existing transient-error classifier; writers never retry.

## Testing

Add fake-Claude integration tests that inspect the actual argv/environment boundary, JSON result parsing and failure handling. Add real Git tests for Claude coordinator instruction files and `deepseek/` writer branches. Keep every existing Codex regression. GitHub Actions remains the release gate on Python 3.11, 3.12 and 3.13.
