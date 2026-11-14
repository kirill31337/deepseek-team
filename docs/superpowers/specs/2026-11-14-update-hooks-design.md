# Package updates and hook refresh

The owner approved the combined update command and first-use refresh on 2026-11-14, then required that every necessary user action appear during updating, including updates performed by an AI agent. Target release is 0.12.0, chosen by the coordinator for this new functionality.

## Intended behavior

`deepseek-team update` upgrades the installed package using its existing installation manager, then invokes the newly installed code in a fresh process to refresh already configured coordinator hooks. Supported installations are pip in a virtual environment, pipx and uv tool. Installation provenance determines the manager. Ambiguous provenance, editable/local-source installs and a conflicting explicit manager produce actionable diagnostics before package replacement. Ordinary updates retain the installation channel.

Direct pip/pipx/uv upgrades are followed by first-use refresh when DeepSeek Team runs again. Wheel installation has no standard post-install callback. The direct manager command alone therefore cannot print a DeepSeek-owned post-install notice. The documented agent update procedure must run the updated `deepseek-team hooks refresh --runtime auto --notify --json` before reporting completion. This is explicit in the shipped coordinator instructions and user update guides.

## Hook refresh interface

New `src/codex_deepseek_team/hook_refresh.py` owns these public interfaces.

- `refresh_installed(runtime='auto', *, notify=False) -> dict` returns a schema-versioned JSON-compatible report.
- `main(argv=None) -> int` provides `--runtime auto|codex|claude|both`, `--notify` and `--json`, and supports `python -m codex_deepseek_team.hook_refresh`.
- `refresh_for_cli(argv) -> None` attempts automatic refresh for ordinary coordinator-facing CLI invocations, writing actionable notices to stderr without corrupting stdout or changing the command's exit status.

The report contains `schema_version=1`, `version`, `status`, `hooks`, `required_actions` and `requires_user_action`. Hook rows identify `runtime`, `status` and `changed`. Action rows contain `code`, `runtime`, `message` and `command`. Status is `completed`, `action_required` or `failed`. Explicit refresh/update returns 0 for completion, 3 for completed package/local refresh with outstanding user actions, and 78 for failure. Package-install failure keeps its failure and reports the incomplete update.

Auto discovers package-owned hook records in existing configuration, independently of installed coordinator executables. A stale record is sufficient ownership evidence. Explicit runtimes select existing integration, and missing integration receives setup guidance. Automatic refresh never attaches a project or creates a new integration from PATH discovery. Missing configurations in auto are a quiet no-op. Explicitly disabled native hooks retain their setting and receive a user action instead of automatic re-enablement. Foreign hooks, settings, auth, access, effort and project on/off choices are preserved.

Reuse existing owned-hook recognition and installation functions. Reject malformed, symlinked or inaccessible configuration using existing safe readers and controlled errors. Configuration changes remain atomic and preserve concurrent-write refusal. Repeated and overlapping refreshes must be idempotent. A failed refresh leaves actionable evidence and the original command usable with existing gates intact.

`--notify` is the post-upgrade contract. It always includes restart/review guidance for configured runtimes, including current definitions whose native trust cannot be inspected. Changed definitions also require that guidance without `--notify`. Messages instruct the user to start a new session and review DeepSeek Team in `/hooks`, approving native trust if prompted. They are printed with redirected streams and noninteractive input as well as a TTY. AI agents must relay all returned actions verbatim or faithfully in the user's language and state that user confirmation remains pending.

Automatic CLI refresh skips help/version, explicit integration management, worker execution and credential-management commands. Lifecycle SessionStart/UserPromptSubmit also refresh their own already configured runtime. Hook responses carry notices in native additional context so the agent receives them. Existing block/continuation decisions and valid JSON protocols retain their behavior. PreToolUse/Stop do not run refresh. Managed worker sessions must not alter coordinator host settings.

### Updates inside a running AI session

The coordinator identified a coverage gap in the initial plan. An agent can run an ordinary package-manager upgrade and immediately finish in the same turn, before another lifecycle bootstrap loads the new instructions. The already installed PreToolUse/Stop hooks must therefore detect a newly observed package version and deliver an explicit instruction to run the new package's `deepseek-team hooks refresh --runtime RUNTIME --notify --json`, relay every required user action and report pending native review.

New `upgrade_notices.py` maintains a small owner-only receipt per runtime home, keyed by coordinator session and package version. It inspects only existing owned integration. Lifecycle observations seed healthy new sessions, and an old/unknown session arriving directly at PreToolUse or Stop after replacement receives the notice. Bound old session bookkeeping without dropping current-version evidence. Receipt reads/writes use safe regular-file checks and atomic publication. They never update coordinator hook definitions, project instructions, policy, trust or coordination outcomes on PreToolUse/Stop.

Expose `prepare_notice(payload, runtime) -> dict | None`, `annotate_result(result, payload, runtime, notice) -> dict` and `mark_delivered(notice) -> None`. Hook main prepares the notice after validating input, annotates the normal result, prints/flushed exactly one native JSON response and only then records delivery. Receipt publication failure retains the notice for a future retry and emits a controlled stderr diagnostic without printing a second JSON response or changing an existing tool deny. Skip native worker/subagent sessions. SessionStart/UserPromptSubmit notices use additional context. PreToolUse preserves existing permission decisions and makes the instruction visible in supported context/reason fields. A first Stop with pending notice requests one continuation so the updating agent performs the post-check and reports actions. Preserve an existing Stop continuation and its reason, and honor repeated-Stop semantics to avoid a notification loop.

This addition is a coordinator requirement correction. Original worker results retain their quality when they satisfied the original bounded brief. Verify a raw-manager upgrade inside an active Codex/Claude session, another session observing the new version first, repeated hooks/Stop, unsafe receipts and JSON publication failure controls.

Required action notices precede existing long policy context and Stop reasons so native context limits keep the actions visible. Retain all existing context, decisions and reasons after the notice. Unexpected notice preparation/annotation failure emits a short controlled stderr recovery instruction and preserves the normal gate response. It must not silently suppress the failed check or print another JSON response. Regression controls cover long existing context and an injected unexpected preparation error.

## Updater interface

New `src/codex_deepseek_team/updates.py` exposes `main(argv=None) -> int` through `deepseek-team update`. It accepts `--manager auto|pip|pipx|uv`, `--runtime auto|codex|claude|both`, `--version VERSION` for an explicit release and `--json`.

Use public manager commands with argument arrays and no shell. Detect pipx via validated environment metadata, uv via its receipt and pip via installed distribution provenance plus a venv interpreter. Preserve lexical executable/interpreter paths for virtual environments. Verify the running module belongs to that installed distribution. Reject unsafe manager ambiguity, source/editable context, unsupported global pip and version arguments beginning with an option or containing command syntax. Do not expose credentials in diagnostic output.

After a successful manager process, launch `sys.executable -m codex_deepseek_team.hook_refresh --runtime SELECTED --notify --json` in a fresh process with source-import overrides removed. Do not use old imported package code to assert the new version or configuration. Manager output goes to stderr or a bounded sanitized diagnostic while JSON stdout remains one valid object. The final report includes the manager, previous/current version, package update outcome and all hook required actions. Nonzero action-required return is explicitly distinguished from package failure. A post-upgrade failure reports that the package changed and hook refresh remains incomplete, with exact recovery commands. Subprocess launch errors are controlled. No automatic retries, credentials, provider calls, sudo, native trust approval or coordinator restart are involved.

Manager failure after launch can leave partial changes. Report `package_update.changed=null` and a clear recovery instruction in that case. An inability to start the manager remains a verified no-change outcome. Include a known manager exit code. Serialize updater invocations for the same installation using an owner-only stable lock outside the replaceable virtual environment. A busy update receives controlled code 78 before launching another manager. Preserve unsafe locks and retain release of the held descriptor on every exit. Plain pip detection verifies the installed distribution's `INSTALLER` provenance in addition to its venv and module path, and rejects unknown/unsupported automatic provenance before mutation.

## Independent work

One worker owns hook refresh, lifecycle context, focused refresh tests and its CLI integration. Another owns the updater, provenance checks, focused updater tests and a disjoint CLI update-dispatch hunk. A third owns user/agent documentation, setup completion copy and version metadata. The coordinator reviews actual deltas, resolves the shared CLI integration, verifies security and both native runtimes, and publishes.

## Acceptance

- Healthy and stale owned Codex/Claude definitions, missing/foreign-only configs and disabled native hooks behave as specified under isolated HOME/CODEX_HOME/CLAUDE_CONFIG_DIR/XDG_CONFIG_HOME.
- Malformed/symlink configuration, concurrent refresh and replacement failures retain user data and report actionable failure.
- Noninteractive text, JSON, lifecycle additional context and agent instructions expose every required user action.
- Installer detection covers pip/pipx/uv, source/editable/ambiguous provenance, manager failure and failure after package replacement. A fresh post-update process reads the new package.
- Existing focused config/onboarding/bootstrap/native tests and the full unittest suite pass. Build metadata and shipped resources agree on 0.12.0. Real isolated install/replacement with pip/pipx/uv and native Codex/Claude controls pass before release.
- Publish verified tagged artifacts to GitHub and PyPI and verify a clean registry installation, digests, version and new CLI behavior.
