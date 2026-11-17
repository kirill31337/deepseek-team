# DeepSeek Team DSH bridge

Dependency-free ESM cordis plugin for the Qwen coordinator inside the DeepSeek
Harness. Installed by `deepseek-team hooks install --runtime qwen` as one owned,
marker-delimited row in the active DSH profile's `cordis.patch.yml`; the row's
`name` is the absolute path to this `index.js` and `config.entrypoint` is the
absolute `deepseek-team` command.

The plugin translates root-agent DSH events (`agent/created`, `agent/pre-step`,
`tools/pre-execute`, `tools/post-execute`, `agent/turn-stopping`) into
`deepseek-team coordinator-hook --runtime qwen` calls and applies the returned
decisions. See `docs/DSH.md` in the package repository for the event contract,
fail-closed semantics and coverage limits. Tests: `tests/dsh_bridge/`.