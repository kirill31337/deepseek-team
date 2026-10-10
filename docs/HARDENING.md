# Current coordinator and worker boundaries

**English** | [Русский](https://github.com/kirill31337/deepseek-team/blob/main/docs/HARDENING.ru.md)

DeepSeek Team separates coordinator process enforcement from worker isolation. The coordinator owns architecture, security decisions, final verification, integration, secrets/signing, commits, publishing and production actions. DeepSeek workers use `deepseek-flash`, remain leaf workers, and cannot delegate or publish.

## Coordinator lifecycle

Both Codex and Claude Code use the persistent coordination ledger in explicitly attached projects. `SessionStart` and `UserPromptSubmit` restore task state; `PreToolUse` requires early planning for supported source inspection and checks recognized mutations against the registered distribution, scope and pending worker ownership. `Stop` requests continuation for pending assignments, undispositioned results or missing coordinator/native results. Repeated Stop leaves unfinished work recorded without an endless veto loop. An unplanned turn with no recorded work can close without claiming implementation completion.

Every coordinator or native-agent deliverable needs an explicit current accepted or cancelled result. A later recognized mutation in its scope requires renewed acceptance. Feedback observations alone do not complete a deliverable.

An attached, enabled project is bound to its DeepSeek hook context. Launching Codex or Claude Code from a common parent can select one explicitly attached root with `DEEPSEEK_TEAM_PROJECT_ROOT=/absolute/repo/root`; this selects the DeepSeek hook context only and does not make the runtime discover child or native instructions automatically. An attached, enabled target without a bound selection keeps showing selection guidance, a workdir in a different repository cannot reuse another plan, and an ambiguous working-directory change stays unscoped. No variable attaches a project, widens access or changes the mandatory worker sandbox.

Once every worker result has a disposition and every coordinator or native-agent outcome is a current `accepted` or `cancelled`, the coordinator can close the task explicitly with `deepseek-team coordination complete --path REPO --task TASK_ID`. It reuses the same validation on demand, independently of `Stop`, which still completes a delivered task automatically.

These are process guardrails, not universal interception of arbitrary programs or future runtime tools. Claude plan-mode files use the native configured plans directory; native-subagent events are outside its coordinator gate. Codex owns native hook trust, reviewed through `/hooks`; the package does not bypass or infer it. Runtime settings can disable hooks and `hooks status` cannot establish what a running session loaded.

Native coordinator agents run under the coordinator's own model, permissions and sandbox. They require a concrete delegation reason for planned work and do not substitute for required DeepSeek implementation assignments at manual 50/75 profiles.

### Early source inspection

The installed `PreToolUse` matcher names the supported read tools (`Read`, `Grep`, `Glob`), the shell spellings the runtimes deliver (`Bash`, `exec_command`, `shell_command`), the mutation tools (`apply_patch`, `Edit`, `Write`, `NotebookEdit`) and the native launch/message names, each with an optional qualified prefix. Shell inspection commands such as `cat`, `rg`, `sed`, `git log -p`, `git diff`, `git show`, `git grep` and `git blame` are recognized as source reads, while status/bootstrap commands, instruction reads (`AGENTS.md`, `CLAUDE.md`, `.deepseek-team.toml`), waits, worker children and purely conversational turns stay exempt. Opaque or unbounded shell text is not claimed to be intercepted.

On an unclassified task the first recognized source inspection returns an early-planning reminder and records `source_inspection` as work. Repeated delivery of the same nonempty tool-use identity is idempotent; any other recognized source read is denied until the coordinator registers a classification and a concrete distribution. Inspection therefore counts as work, and `Stop` no longer closes such a turn as an untouched draft. A small source-informed answer may register a small concrete plan; no fabricated worker is required.

Instance coverage is bounded and measured, not assumed. The offline Codex **0.153.2** fixture delivers its shell tool as `Bash`, and the `exec_command`/`shell_command`, `Read`, `Grep` and `Glob` names plus the Claude installation matcher are exercised by unit tests over the installed matcher text. No live Claude event coverage and no universal hook interception are claimed.

### Protected write authority

A protected coordinator responsibility (`architecture`, `security`, `integration`, `final_verification`, `commit_push`, `production`, `secret_signing`) keeps `executor: "coordinator"`, and its `scope` remains a context/read boundary that grants no source write by itself. Architecture/security documentation uses `decision_artifacts` listing exact in-scope `.md`, `.rst` or `.txt` files. Integration writes require `integration_of` plus a `write_scope` of exact files backed by a referenced terminal worker's actual changed files, or by a currently accepted ordinary coordinator write with a recorded concrete mutation. Directories, globs, path traversal outside the project and symbolic or hard-link aliases confer no authority; decision artifacts cannot authorize source or configuration files, and a file authorization never permits deleting its parent directory. `integration_of`, `write_scope` and `decision_artifacts` join the immutable identity of a started or accepted deliverable, together with its scope and feature card, so a revised slice needs a new deliverable id.

An ordinary deliverable still authorizes its contained scope. In a substantial Auto plan every ordinary read or write deliverable must request `executor: "auto"` so the saved routing decision chooses worker or coordinator; an explicit `executor: "worker"` or `executor: "coordinator"` is rejected there with an actionable error. Protected responsibilities, attested native exceptions and genuinely small single-output tasks keep their existing behavior.

## Required Linux isolation

Every DeepSeek worker requires a working Linux OS sandbox before credential access. AppArmor user-namespace restrictions remain enabled. Ubuntu can use the package-owned named `deepseek-team-bwrap` profile; the profile permits namespace creation while Bubblewrap enforces the filesystem/process boundary.

Uncoordinated read-only Codex without an owned copy uses its native sandbox with the private Bubblewrap launcher. The corresponding read-only Claude mode runs inside an outer Bubblewrap namespace; its network namespace remains available for the direct provider connection. Runtime tool permissions enforce the read-only tool surface.

Managed copies (including coordinated read-only assignments) for both runtimes use a sparse outer Bubblewrap namespace. Only the owned copy, required runtime prefixes, temporary HOME and per-run controls are exposed. Git administrative files are read-only. The network namespace is private: a fixed provider relay supplies the supported model API over a per-run Unix socket and local bridge. The actual provider credential stays host-side; this is not an arbitrary network proxy. Sibling copies and the user's source checkout are not exposed.

Full-access authorizes development inside the owned copy, not host SDKs, databases, services or secrets. The coordinator prepares missing dependencies explicitly. Declared requirements are probed inside the real sandbox before credential access, and declared checks run there after the worker. Stubs are not equivalent to real project verification.

### Readiness, preparation and namespace probes

Readiness is verified inside the namespace, not inferred from the host. A default runtime name is resolved to deduplicated executable candidates in `PATH` order, then `/usr/local/bin` and `/usr/bin`, and the first candidate that actually runs its `--version` and required capability help in the prepared namespace is selected. An explicit `--codex`/`--claude` pin is probed alone and its failure is fatal: it is never silently replaced by a PATH, system or cross-runtime fallback. `runtime=auto` may try Codex and then Claude. These local probes make no provider request and read no credential, and they share the job's total deadline; its expiry is a code-124 budget error that stops further candidates.

Host-only tooling is not exposed. The coordinator prepares what a task needs explicitly with `workspace prepare`, copying an explicitly selected software directory into the read-only `.deepseek-tools/NAME` area and explicit non-secret files or build caches into workspace-relative destinations that stay writable in the owned copy. Only `JAVA_HOME`, `ANDROID_HOME`, `ANDROID_SDK_ROOT`, `GRADLE_USER_HOME`, `M2_HOME`, `KOTLIN_HOME` and `PATH` may be prepared, each using the literal `{workspace}` placeholder; `HOME`, provider variables, hook/config overrides and loader injection variables are never accepted. Declared `--probe` commands run only inside the namespace and must succeed before the provider key is read. Only these prepared roots are added as narrow read-only binds; the whole `/etc`, `/usr` or home trees are never mounted. Broad roots, credential-like paths, escaping links and cycles are rejected before anything is published. For a Java runtime, only ordinary files below `/etc/java-[0-9]+-openjdk/` and the exact `/etc/ssl/certs/java/cacerts` are materialized; the public `docs` and `src.zip`/`lib/src.zip` links are skipped, and every other escaping link fails.

`--save-project` records a private recipe that replays only these declarations into a later copy, keyed by the canonical source-project path under private state. It never stores or automatically executes host commands. A malformed, unsafe or failed recipe fails closed; an absent recipe preserves normal explicit preparation. The legacy `workspace prepare ID -- COMMAND` form still runs on the coordinator host with the existing safe environment and the prepared workspace variables and `PATH`. Full-access never implies host SDKs, databases, services or secrets.

### Diagnostics, evidence and re-verification

Declared checks run in the namespace before the worker (baseline) and after it (post). Each stream is retained up to 65,536 bytes, redacted for known sensitive values and published owner-only in the workspace's private `diagnostics/` directory; larger output is drained and marked truncated, and a timed-out command's process group is cleaned up. A nonzero baseline is evidence, not a blanket rejection, so bugfix work may run against an already-red suite. Explicitly registered prepared inputs are fingerprinted (type, mode and content hash, or a missing marker) and any change is recorded separately from the source patch and regular worker changes; input content is never stored. Timeout, launch and probe failures are `environment`; ordinary project-check failures stay `verification`. Automatic terminal feedback for a failed project check is neutral `unknown` until the coordinator reviews the actual diff and evidence.

`workspace verify ID [--check COMMAND] [--timeout SECONDS] [--json]` reruns the declared or supplied checks in the rebuilt prepared environment and sparse namespace. It never loads the provider key, starts a relay or starts a model, and it appends a unique verification run while preserving the original worker status, error, checks and quality. It creates no new quality case, holds the workspace ownership lock for the operation, and may inspect an originally failed copy without automatically resuming implementation. `doctor --offline` checks a managed full-access runtime by actually launching a candidate in a disposable empty sparse namespace with a per-candidate context; it allocates no model job and reads no key. The read-only native Codex and outer read-only Claude boundary is unchanged.

## Queue, access and retained work

Fresh Auto access is read-only. Explicit permissions, manual profiles and project `off` remain authoritative. Independent implementation needs explicit full-access. Admission is immediate for eligible bounded work, with truthful quality feedback and measured economic vetoes; unknown costs are never claimed as savings.

The worker queue separates assignment from execution. It defaults to eight concurrent workers and waits FIFO; busy slots do not reassign work to the coordinator. Queued launches recheck enabled state, access, routing authorization and project HEAD before secrets or provider launch. Workspace ownership locks prevent concurrent writes to one copy. Waiting and total timeout are unlimited by default; an explicit timeout includes queue time.

A failed implementation retains its workspace and diff. Inspect them before explicit continuation; implementation never retries automatically. Orphaned assignments require stopped-process evidence and an exclusive workspace-lock check before explicit abandonment. Neither silence nor elapsed time alone proves the worker failed.

## Preflight and recovery

Before it allocates an owned copy, the coordinator can verify the committed source with a read-only check:

```bash
deepseek-team workspace check --json
```

An optional path before `--json` selects another checkout. The command reads committed HEAD `names and modes only` - never file contents - and reports `source`, `head`, `eligible` and any `blockers` (each with `path` and `reason`). It calls no model and creates no copy, so it is safe to run at any time. It exits `0` when eligible and `78` when blocked. It examines the whole HEAD, including paths outside the current task scope. Adding a tracked path to `.gitignore`, removing it from the index without committing, or deleting only its working-tree file does not remove the HEAD blocker; only a committed change to the tracked entry does. The filter is a name heuristic, not proof that a file contains a real secret, and its exact exceptions are only `.env.example`, `.env.sample` and `.env.template`. No relaxed-sandbox or renamed-secret workaround exists.

New copy creation rejects credential-like paths and submodules before it allocates the copy, so repeatedly launching against the same blocked HEAD does not accumulate new copies. An early preparation failure in a coordinated managed launch records `failed`/`environment` with `failure_stage: preparation`, the reason and, when one exists, a workspace ID. The recorded `preparation_cause` distinguishes environment failures from admission refusals. Concrete environment failures retain their specific technical constraint; revoked permissions, routing refusals and admission timeouts do not grant a technical-retention exception. Cancellation is recorded as `cancelled` with exit code `130`. It reports no model work and no quality rejection, and it never retries automatically. The coordinator must inspect and disposition the attempt; a plain repeated launch of the same failed assignment is refused. When preflight blocked the source, there is no copy to resume.

Two cases stay distinct. An old failed initial copy that lacks a baseline remains quarantined: `workspace show` still reports it, but `prepare`, `import` and `resume` do not rehabilitate it. An existing prepared copy whose later execution failed keeps the normal explicit inspect/disposition/resume workflow. Coordinated read-only work also uses a managed copy and the same guard; full-access is access to that isolated copy, never to production databases, logs or secrets.

For legitimate project cleanup, preserve the server files and permissions you still need, remove the unwanted credential backup from Git tracking while preserving any required server file, commit that source change, and start a new coordination task based on the new HEAD. Do not rewrite history or delete server files blindly. A separate sanitized clone with a different HEAD is not a transparent replacement for the old assignment, and this release ships no supported automatic sanitized-snapshot feature.

## Private state and credentials

Coordination state and routing state live outside Git. Routing uses format 3 in `routing-v3.sqlite3`; unsupported current-database versions are rejected. Older database files are neither read nor migrated and remain untouched. Completion uses current explicit result records.

The DeepSeek key remains at `~/.config/codex-deepseek/api-key`, with a private directory and mode 600 file; `DEEPSEEK_API_KEY` can supply it instead. Never commit credentials, local configuration or raw model logs. Preserve the primary coordinator model, authentication and unrelated settings during setup and removal.

These controls are not a complete confidentiality boundary for hostile repositories or coordinator binaries. Repository files available to the model can contain secrets. Use a dedicated OS user, container or VM when stronger isolation is required. Offline fixtures verify local behavior; only an explicitly authorized live provider check exercises real inference.
