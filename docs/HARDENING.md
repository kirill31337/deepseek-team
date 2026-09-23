# Current coordinator and worker boundaries

**English** | [Русский](https://github.com/kirill31337/deepseek-team/blob/main/docs/HARDENING.ru.md)

DeepSeek Team separates coordinator process enforcement from worker isolation. The coordinator owns architecture, security decisions, final verification, integration, secrets/signing, commits, publishing and production actions. DeepSeek workers use `deepseek-flash`, remain leaf workers, and cannot delegate or publish.

## Coordinator lifecycle

Both Codex and Claude Code use the persistent coordination ledger in explicitly attached projects. `SessionStart` and `UserPromptSubmit` restore task state; `PreToolUse` checks supported mutations against the registered distribution, scope and pending worker ownership. `Stop` requests continuation for pending assignments, undispositioned results or missing coordinator/native results. Repeated Stop leaves unfinished work recorded without an endless veto loop. An unplanned turn with no recorded work can close without claiming implementation completion.

Every coordinator or native-agent deliverable needs an explicit current accepted or cancelled result. A later recognized mutation in its scope requires renewed acceptance. Feedback observations alone do not complete a deliverable.

These are process guardrails, not universal interception of arbitrary programs or future runtime tools. Claude plan-mode files use the native configured plans directory; native-subagent events are outside its coordinator gate. Codex owns native hook trust, reviewed through `/hooks`; the package does not bypass or infer it. Runtime settings can disable hooks and `hooks status` cannot establish what a running session loaded.

Native coordinator agents run under the coordinator's own model, permissions and sandbox. They require a concrete delegation reason for planned work and do not substitute for required DeepSeek implementation assignments at manual 50/75 profiles.

## Required Linux isolation

Every DeepSeek worker requires a working Linux OS sandbox before credential access. AppArmor user-namespace restrictions remain enabled. Ubuntu can use the package-owned named `deepseek-team-bwrap` profile; the profile permits namespace creation while Bubblewrap enforces the filesystem/process boundary.

Read-only Codex uses its native sandbox with the private Bubblewrap launcher. Read-only Claude runs inside an outer Bubblewrap namespace; its network namespace remains available for the direct provider connection. Runtime tool permissions enforce the read-only tool surface.

Managed full-access copies for both runtimes use a sparse outer Bubblewrap namespace. Only the owned copy, required runtime prefixes, temporary HOME and per-run controls are exposed. Git administrative files are read-only. The network namespace is private: a fixed provider relay supplies the supported model API over a per-run Unix socket and local bridge. The actual provider credential stays host-side; this is not an arbitrary network proxy. Sibling copies and the user's source checkout are not exposed.

Full-access authorizes development inside the owned copy, not host SDKs, databases, services or secrets. The coordinator prepares missing dependencies explicitly. Declared requirements are probed inside the real sandbox before credential access, and declared checks run there after the worker. Stubs are not equivalent to real project verification.

## Queue, access and retained work

Fresh Auto access is read-only. Explicit permissions, manual profiles and project `off` remain authoritative. Independent implementation needs explicit full-access. Admission is immediate for eligible bounded work, with truthful quality feedback and measured economic vetoes; unknown costs are never claimed as savings.

The worker queue separates assignment from execution. It defaults to eight concurrent workers and waits FIFO; busy slots do not reassign work to the coordinator. Queued launches recheck enabled state, access, routing authorization and project HEAD before secrets or provider launch. Workspace ownership locks prevent concurrent writes to one copy. Waiting and total timeout are unlimited by default; an explicit timeout includes queue time.

A failed implementation retains its workspace and diff. Inspect them before explicit continuation; implementation never retries automatically. Orphaned assignments require stopped-process evidence and an exclusive workspace-lock check before explicit abandonment. Neither silence nor elapsed time alone proves the worker failed.

## Private state and credentials

Coordination state and routing state live outside Git. Routing uses format 3 in `routing-v3.sqlite3`; unsupported current-database versions are rejected. Older database files are neither read nor migrated and remain untouched. Completion uses current explicit result records.

The DeepSeek key remains at `~/.config/codex-deepseek/api-key`, with a private directory and mode 600 file; `DEEPSEEK_API_KEY` can supply it instead. Never commit credentials, local configuration or raw model logs. Preserve the primary coordinator model, authentication and unrelated settings during setup and removal.

These controls are not a complete confidentiality boundary for hostile repositories or coordinator binaries. Repository files available to the model can contain secrets. Use a dedicated OS user, container or VM when stronger isolation is required. Offline fixtures verify local behavior; only an explicitly authorized live provider check exercises real inference.
