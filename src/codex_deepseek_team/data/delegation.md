## DeepSeek delegation

Before delegating, check `deepseek-team status` and resolve the CURRENT policy.
The user can request `deepseek-team off` or `deepseek-team on` for this project;
these commands persist across sessions for Codex, Claude Code and Qwen (DSH). They preserve
instructions, credentials and delegation/access/effort settings. Never turn delegation
back on without the user's request. If disabled, continue locally: the delegation and
coordination-plan requirements below do not apply. Existing workers are not cancelled;
inspect their results before integration. Check again before every new assignment:


```bash
deepseek-team config show --effective --instructions --runtime {runtime}
```

### Package updates and required actions

When `deepseek-team update` is available, prefer it for package upgrades. It
replaces the package through its existing manager and refreshes already configured
hooks from the new package in a fresh process. An installation that predates that
command must first upgrade once through pipx, uv or its virtual environment's pip.
The command becomes available only after that package upgrade.

After any direct pip, pipx or uv package-manager update, run the new package's fresh
check before reporting completion, including in noninteractive and JSON work.

```bash
deepseek-team hooks refresh --runtime auto --notify --json
```

Wheels have no standard post-install callback, so the direct manager's own output
cannot carry a DeepSeek Team notice. Read the refresh report and relay every entry
in `required_actions` faithfully to the user in their language. Explain the exit
code. Exit `3` with `status: "action_required"` means the package or local refresh
completed with pending user actions. `0` means complete and `78` means failure. A
`--notify` refresh and every changed hook definition require the user to start a new
session and review DeepSeek Team in `/hooks`, approving native trust if prompted.
State that native review and user confirmation remain pending until the user acts. Surface these actions even when the update
was automated or its streams were redirected.

The session/prompt bootstrap checks private worker storage before opening a
coordination task. Before the first worker in a session, confirm local readiness
with `deepseek-team doctor --runtime {runtime} --offline`, including read-only
assignments. Storage must use ordinary current-user-owned directories with mode
700; symbolic links in the state path or its `workspaces` child are rejected.
Do not repair this by following a link, deleting retained copies, changing a
foreign owner or weakening the sandbox. Setup/doctor/enabled bootstrap can
automatically select and persist a private replacement for an unusable idle
default, without variables. All commands use the saved choice. Active slots,
live queued tickets or an uninspectable old root forbid switching. Preserve all
old files. Broken saved choices and explicit overrides are diagnosed, never
silently replaced. The selected root is shown by bootstrap and doctor.
Optional `DEEPSEEK_TEAM_STATE_DIR` overrides the saved shared root;
`--state-dir` overrides worker/workspace/doctor storage for that command only.
Changing the shared root does not migrate prior coordination tasks: preserve the
old root and register new work in the newly selected root.
After an upgrade, setup/init use the same provider-free safe collector for known
owned roots. `deepseek-team gc --dry-run` is the default and reports keeps,
deletions, allocated bytes and inodes; `gc --apply` performs only safe deletions.
GC protects creation, preparation, start, import, review and verification, plus
live leases and locks. Historical task status is not a lease. Unsafe or uncertain
cleanup is reported and kept, never guessed from mtime alone. User guides and
delegation configuration are preserved.

An attached, enabled project uses its own DeepSeek hook context. When launching
Codex or Claude Code from a common parent, select one explicitly attached root with
`DEEPSEEK_TEAM_PROJECT_ROOT=/absolute/repo/root`. This selects the DeepSeek hook
context only; it does not make the runtime discover child or native instructions
automatically. Use the actual project workdir for tools and the worker CLI, and pass
`--path` to coordination commands. An attached, enabled target without a bound
selection keeps showing selection guidance, a workdir in another repository cannot
reuse this plan, and an ambiguous working-directory change stays unscoped. The
variable never attaches a project, widens access or changes the mandatory sandbox.

Percentages are target work-distribution profiles, not measured contribution. Never
derive an "actual percentage" from calls, tasks, files, lines, tokens, time, or a
subjective list of bullets. Access remains independent of the target profile:
explicit read-only always remains read-only.

Fresh installations use the **Auto** delegation profile. In Auto, register eligible
deliverables with `executor: "auto"`; the recorded routing decision resolves each to
worker or coordinator. Saved manual 25/50/75 profiles retain priority. Capture a
structured `features` card before execution in both Auto and manual profiles so real
outcomes continue to teach the router. Match its runtime/model/effort/context to the
actual assignment, and keep unknown task attributes unknown. Protected coordinator
responsibilities keep an explicit coordinator executor.

Orient before you solve. Register the bounded diagnostic, test-plan and implementation slices you
need before extensively investigating unfamiliar source, and treat an unknown global attribute as
unknown: decompose it into a bounded diagnostic deliverable instead of claiming a safe
implementation. Batch every independent, eligible scope into one distribution and launch those
assignments before duplicating that work yourself, so one plan keeps the overhead down. In a
SUBSTANTIAL Auto plan every ordinary read or write deliverable must carry `executor: "auto"`; an
explicit `executor: "coordinator"` or `executor: "worker"` is rejected there. A genuinely small
single-output task, a manual 25/50/75 profile, a protected coordinator responsibility and a valid
native exception keep their existing behavior. Never promise a contribution percentage and never
widen access automatically.

**Auto delegates useful bounded work immediately by default**. Small or medium tasks with low or medium risk, known or partial localization, local or component coupling, clear requirements and declared tests or a reproducer can be assigned from the first session. Manual verification is allowed for read, review, research, diagnostic, test-plan and documentation tasks with explicit acceptance criteria. Implementation requires executable checks and `full-access`.

Unknown costs stay unknown and do not block eligible work; supported poor measured economics still veto delegation. Under Auto, a sufficiently supported poor local quality history for the task class also vetoes it, while missing or uncertain quality history never blocks an eligible task and aged evidence lets admission resume. The two policy defaults for that check are `quality_min_evidence` (10.0 effective local cases) and `quality_min_success_probability` (0.70), set with `deepseek-team routing configure --quality-min-evidence` and `--quality-min-success-probability`. Review the pooled local category history with `deepseek-team routing stats --path PROJECT` (`--json`, `--sort score|cases|category`); the case unit is each reviewed worker assignment, not the whole session, and the command makes no routing decision. One rework is recorded without a family pause. A rejection or three distinct rework cases within the cooldown window pause the family for the configured `failure_cooldown_seconds` (300 seconds by default). With explicit quality, only worker/shared `major_gaps` or `unusable` qualify for a failure pause; cosmetic, minor-gap and neutral cases do not. After the pause, ordinary admission checks resume, including the learned local quality check. Failed implementation never retries automatically.

Fresh Auto access remains **read-only**. To delegate implementation, explicitly opt in:

```bash
deepseek-team config set --project --access full-access
```

The coordinator decomposes meaningful independent slices, reviews actual diffs and declared checks, and reports accepted work and rework without repeating the worker's investigation or inventing percentage savings. Architecture, security, integration, final verification, commits and production remain coordinator-owned. Saved manual profiles, explicit permissions and `off` retain priority.

### Delegation lessons

After review, record every verified result through `coordination use`, including clean successes. When a result needs corrections, attach a structured reason with `coordination use --rework-json FILE` (`cause`, `severity`, `summary`, `prevention`) and keep the verifiable evidence in `--evidence`; `unknown` is a valid finding. Supply the explicit quality assessment in the same call with `--quality-json FILE`: exactly `{grade, attribution, evidence}`, grades `met` (1.0), `minor_gaps` (0.8), `major_gaps` (0.3), `unusable` (0.0) and neutral `unassessable`, attribution `worker`, `shared`, `coordinator`, `environment` or `unknown`. Only worker/shared with an assessable grade scores, judged against the supplied criteria and evidence; a missing brief or context and changed requirements are neutral on their own, a cosmetic edit can still be `met`, and these declared heuristic weights are not measured probabilities. Omitted quality keeps the legacy binary reading, older cases can be reassessed only by appending an explicit assessment without rewriting history, and the lowest scored worker grade is retained. The journal is private per project. The coordinator reviews it when due - 10 distinct newly reviewed or updated cases, or 3 distinct rework cases with the same task kind and a known cause - and is reminded at a completed-task boundary. Use `deepseek-team lessons review --json` to read the bundle and `deepseek-team lessons review --apply FILE` to submit a full bounded replacement ruleset, or an explicit no-change review; version snapshots preserve historic context. The coordinator performs this review itself: there is no background model, no automatic rewrite or retry, and a worker never revises host rules. Rules are advisory planning guidance subordinate to user instructions, access and forced effort settings, and sandbox requirements, and lessons rules do not themselves change router weights. See the delegation lessons guide for the exact payloads.

### Rework and corrective work

After review, record the operational outcome and, when corrections were needed, the concrete defect evidence with the original `needs-rework`. A minor, tightly coupled correction may be performed by the coordinator when its concrete reason is recorded in the rework `summary`/`prevention` or the outer `--evidence`; substantive corrections normally return to DeepSeek as a bounded correction brief. That brief links the original assignment, states the concrete defect and its evidence, supplies the context the first attempt lacked, and carries a reproducer or executable acceptance checks; route it as its own deliverable, then verify the actual delta and integrate it. Correction size and worker quality are separate judgements: a one-line correctness or security defect can still be serious, a cosmetic edit can keep full credit, and a first failed attempt never mandates takeover. Architecture, security, integration and final verification stay coordinator-owned. No blind retries, no `off` bypass, no sandbox bypass and no automatic access widening; a completed assignment cannot be resumed directly, so a succeeded assignment needs a new planned correction that declares `correction_of` for the original reviewed assignment. The runner resolves that assignment's saved workspace automatically, so the correction needs no remembered `--workspace`; a conflicting explicit value, or missing, ambiguous, stale, foreign or finalized predecessor state, blocks safely instead of allocating a fresh copy. A failed, inspected attempt of the same assignment may resume only explicitly with `--resume-after-failure`, in the same copy. Repeated dispositions or attempts of one assignment stay one case, and the original failure is never laundered into a clean first pass; nothing here requires an endless loop or an artificial fixed quota.

### Final reporting

Enabled final summaries of performed work add short bullets, in the user's language, that
separate work the coordinator completed personally from work actually delegated to DeepSeek,
and name accepted results plus any rework, rejection or failure. Cover the reported task,
including work and assignments from earlier turns. Planned, running, failed and
rejected work is never described as completed, and when nothing was delegated the summary
says so explicitly. After the bullets the coordinator states a coarse approximate
coordinator/DeepSeek split of ACCEPTED WORK as two whole-number percentages totaling 100 percent, in the
user's language and labelled exactly "subjective estimate, not measured" (translated). It is
judged qualitatively from accepted scope and complexity and from coordinator review and rework.
Describe the accepted scope and any specific rework first. The split is not a measured delegation
rate, a performance metric or routing feedback; it only summarizes accepted work. It is never
derived from counts of calls, tasks, deliverables, files, lines, tokens, time or
bullets, never copied from a configured Auto/25/50/75 profile, and never presented as measured
productivity or money/time/token savings. If even a rough estimate lacks supporting evidence the
summary reports the estimate as unavailable instead of inventing numbers; with no accepted
worker contribution it uses 100/0 for accepted work while still disclosing failed or rejected
attempts. Coordinator-native subagent work is credited separately, never as the coordinator's
own personal work and never as DeepSeek work, and if it is included on the coordinator side it
is labelled as such. This is a reporting instruction only: it calculates no ratio, records no
telemetry, adds no flag and changes no ledger schema, and counting-derived percentage metrics
remain banned. Disabled delegation, status-only turns and turns without performed work need no
performed-work report.

Every worker requires the Linux OS sandbox. Do not weaken Ubuntu AppArmor
user-namespace restrictions to make delegation pass.

### Model, effort and native subagents

DeepSeek Team workers always use **`deepseek-flash`**. Do not route to another DeepSeek
model. Resolve the current effort policy with the effective config before assigning work.
The default is `effort=auto`: in that mode the frontier coordinator chooses the effort
from the actual delegated scope and passes `--effort low|high|max` (`low` for bounded
or mechanical work, `high` for the normal case, `max` for difficult debugging,
cross-file reasoning, security-sensitive review or adversarial verification). The legacy
spelling `--effort medium` is still accepted and is treated exactly as `high`, so old
commands, saved settings and feature cards keep working. If the resolved policy is
explicitly `low`, `high` or `max`, treat it as the saved forced level for new DeepSeek
jobs and do not auto-select another value (a saved legacy `medium` normalizes to `high`).
`config set` can persist that choice per project or globally, and setting it back to
`auto` restores frontier selection. A direct worker launched while policy is auto falls
back to `high` only when no frontier-selected concrete effort reaches the runner.

The coordinator may use its own **native subagents** only as an explicit exception, never for
generic parallelism, isolated context or convenience alone. Every native-agent deliverable needs
a concrete `delegation_reason` and a `native_exception`:

- `{"code": "explicit_user_request", "evidence": "specific user request"}`, or
- `{"code": "native_capability", "capability": "specific capability or tool unavailable to a DeepSeek worker", "evidence": "why it is required"}`.

The coordinator attests this evidence; it is not mechanically proven user provenance. The native
prompt or message must include `[deepseek-team:TASK_ID:DELIVERABLE_ID]` binding the registered
native-agent scope, and its accepted or cancelled outcome is recorded like any other deliverable.
A native subagent is additive: it does **not** satisfy a DeepSeek worker assignment required by the
50/75 profiles. Architecture/security decisions, final integration/verification, secrets/signing,
commit/push and production actions remain coordinator-owned. DeepSeek workers themselves remain
leaf workers and must not delegate.

### Process

Plan and route every delegated subtask before assigning another agent, even a small
read-only history, search or review task. In Auto mode register `executor: "auto"` and
inspect the resolved plan; a worker decision means DeepSeek. Ordinary short answers the
coordinator gives directly need no fake worker, wait and control calls create no work,
and new work sent to an already running agent also requires routing. Identify concrete
deliverables before duplicating their implementation. Each deliverable needs: id, kind,
concrete scope, executor, acceptance criteria, dependencies, checks, and for Auto a
`features` card. `native-agent` executors additionally need `delegation_reason` and a
`native_exception`. Architecture, security decisions, final verification, integration,
secrets/signing, commit/push and production stay with the coordinator. That responsibility
alone does not reserve ordinary implementation, tests, fixtures, documentation, or
non-secret metadata.

At 75/full-access, separable implementation/tests/fixtures/docs/non-secret metadata
default to worker assignments. Retaining worker-eligible work requires a supported
constraint; arbitrary prose such as "I own quality" or "this is release work" does
not satisfy the gate. At 50/full-access, a review-only worker does not substitute
for delegating a separable implementation/test/docs slice. Read-only overrides
never expand write authority.

For Codex, the installed stable user-level lifecycle hook is active only in projects
explicitly attached with `deepseek-team init --coordinator codex`. After native
Codex hook trust, SessionStart/UserPromptSubmit inject the persistent task state,
including after compaction. The lifecycle hook permits the first recognized source
inspection with an early-planning reminder and records it as work; a registered
distribution is required before any further supported source read. Real status/bootstrap
checks and purely conversational turns stay exempt, and tool recognition is bounded, never
universal. Before source mutation, register the distribution:

```bash
deepseek-team coordination plan --task TASK_ID <<'JSON'
{
  "classification": "substantial",
  "deliverables": [
    {
      "id": "implementation",
      "kind": "implementation",
      "scope": ["src/example.py"],
      "executor": "auto",
      "acceptance": ["focused behavior implemented"],
      "dependencies": [{"kind": "command", "value": "python3"}],
      "checks": ["python3 -m unittest tests.test_example -q"],
      "features": {
        "kind": "implementation", "domain": "python", "operation": "extend",
        "localization": "known", "coupling": "local", "verification": "tests",
        "clarity": "clear", "risk": "low", "scope_size": "small",
        "runtime": "{runtime}", "model": "deepseek-flash", "effort": "high",
        "context_version": "project-v1"
      }
    },
    {
      "id": "integration",
      "kind": "integration",
      "scope": ["src/example.py"],
      "executor": "coordinator",
      "integration_of": ["implementation"],
      "write_scope": ["src/example.py"],
      "acceptance": ["accepted implementation merged after review"],
      "dependencies": [{"kind": "path", "value": "src/example.py"}],
      "checks": ["python3 -m unittest tests.test_example -q"]
    }
  ]
}
JSON
```

The first slice is a normal Auto read/write deliverable (`executor: "auto"`): inspect the resolved
plan and start a worker only when it returns a worker assignment; retain a coordinator decision
with the coordinator. The second slice is the coordinator integration step: it names the completed
implementation with `integration_of` and the exact files it will touch with `write_scope`, backed
by the actual worker changes, or by accepted coordinator edits recorded in the ledger. An
architecture or security report instead lists `decision_artifacts` with the exact relative `.md`,
`.rst` or `.txt` files contained in its scope; a protected scope describes read/review context only
and never grants generic source-write permission. A read-only review never confers integration
write rights. Newly discovered implementation needs a separately routed deliverable; substantial
Auto plans use `executor: "auto"`. Minor corrections actually performed by the coordinator are reported as coordinator
rework; substantive corrections normally return to DeepSeek. Under a saved manual profile an ordinary slice may name
`executor: "worker"` explicitly instead. Use the assignment id returned by the plan:

```bash
deepseek-team worker --runtime {worker_runtime} --effort high \
  --coord-task TASK_ID --coord-assignment ASSIGNMENT_ID <<'TASK'
Implement the assigned deliverable and satisfy its registered acceptance criteria.
TASK
```

The runner records assignment start, workspace, result, worker-only file delta, and
declared checks automatically. After inspection, record disposition:

```bash
deepseek-team coordination use --task TASK_ID --assignment ASSIGNMENT_ID \
--disposition incorporated --evidence "reviewed diff and accepted result"
```

Record verified coordinator and native-agent outcomes with `coordination result --task TASK_ID
--deliverable DELIVERABLE_ID --outcome accepted --evidence "checks passed"`.
Every such deliverable needs an explicit current accepted or cancelled result before
completion; recorded feedback alone does not establish completion. A later recognized mutation in its scope requires fresh acceptance.
Native-agent outcomes do not train the coordinator routing estimates.
Both result commands accept `--cost-usd` for the measured total, including review
and rework; omit unknown costs rather than inventing a subscription dollar value.
Use `needs-rework` when the worker result needed fixes. Repeated attempts remain one
case, and a recorded quality failure cannot be converted into a clean first-pass
success by a later incorporation. Provider/environment failures stay unlabelled.
Process success is only awaiting review: integrate and verify accepted changes
before finalization. Repeated `coordination use` feedback remains allowed;
`--disposition needs-rework` keeps the copy, while `--final` combines the final
accepted or rejected disposition with finalization.

If selected uncommitted source is required, never copy the whole checkout:

```bash
deepseek-team workspace import WORKSPACE_ID --include path/to/needed.py
```

Those files are coordinator-prepared source and remain distinct from worker changes.
Prepare missing SDK/runtime/dependencies explicitly with `workspace prepare`. The
declarative form accepts repeatable `--tool NAME=HOST_DIR` (copied into the read-only
`.deepseek-tools/NAME` area), `--copy SOURCE=REL_DEST` (explicit non-secret files or
caches, writable in the copy), `--env NAME=VALUE` (only `JAVA_HOME`, `ANDROID_HOME`,
`ANDROID_SDK_ROOT`, `GRADLE_USER_HOME`, `M2_HOME`, `KOTLIN_HOME` and `PATH`, each rooted
in the copy through the literal `{workspace}` placeholder) and `--probe COMMAND`
(smoke commands that run only inside the worker namespace before the provider key).
`--save-project` stores a private recipe that replays only those declarations into a
later copy and never executes host commands. The finite immutable content cache keeps
saved recipes independent of execution trees; each active workspace still
materializes its tools independently inside the sandbox, mutable files are never
hardlinked, and a missing or stale entry invalidates the recipe with re-preparation
guidance. Aggregate byte/inode reservations with free margins honor the configured
storage root and TMPDIR rather than assuming `/mnt/data`. The legacy `workspace
prepare ID -- COMMAND` form still runs on the coordinator host with the prepared
variables and PATH.
Full-access never implies access to host-only JDK/SDK/tools, secrets, databases,
services, or the network. Default runtimes are namespace-verified candidates, an explicit
`--codex`/`--claude` pin is preserved rather than replaced, and runtime selection shares
the job's total deadline. Declared dependencies are probed inside the real sandbox
before the provider credential is read; declared checks run in that sandbox after
the worker. Do not replace missing project dependencies with stubs and report that
as equivalent verification.

Declared checks are also captured as a baseline before the model runs: a nonzero
baseline is evidence, not a blanket rejection, so a bugfix assignment may proceed
against an already-red suite. After the worker, automatic terminal feedback for a
failed project check is neutral `unknown` until the coordinator reviews the actual diff
and evidence; only a reviewed explicit quality/disposition grades the worker.
`workspace verify ID [--check COMMAND] [--timeout SECONDS] [--json]` rebuilds the
prepared environment and sparse namespace, reruns the declared or supplied checks and
appends a unique verification run. It never loads the provider credential, starts a
relay or starts a model, and it preserves the original worker status, error, checks and
quality. Bounded per-stream output is redacted and published owner-only under the
workspace's private `diagnostics/` directory.

One logical job (`job_id`) owns one stable workspace through preparation, a failed
process, check or API error, a review gap without a PID, `needs-rework`, explicit
resume and linked corrections. Each run records a distinct `attempt_id`; there is
no automatic implementation retry. Final acceptance explicitly closes the lease and
removes the checkout, its `.git`, `.deepseek-tools` and owned temporary
environments. Rejecting, cancelling or abandoning the job finalizes it the same
way. Compact job, attempt, check, exit and `work_inode` history, the final binary and
untracked `changes.patch`, diagnostics, provenance and lessons are preserved; no
checkout or runtime tree is archived and there is no TTL backlog. A parent feature
may continue.

Finalize explicitly with `coordination use ... --final` or
`coordination finalize --task ID --assignment ID --evidence TEXT [--cancel]`.
`workspace finalize ID`, `workspace cancel ID`, `workspace recover ID`,
`workspace resume ID`, `workspace list` and `workspace history` expose the same
lifecycle. `workspace recover` and `workspace resume` repair ownership/state only
and never run a model. A failed attempt continues explicitly with
`worker --workspace ID --resume-after-failure` in the same inode. A succeeded result
that needs substantive correction gets `needs-rework`, then a newly planned correction
declaring `correction_of` for the original assignment; the runner resolves the same
still-unfinalized workspace automatically, with `--workspace` optional unless an
explicit value conflicts. Missing, ambiguous, stale, foreign or finalized predecessor
or copy state blocks safely instead of falling back to a new environment; deliberately
independent work can still start a new job, and a finalized job needs deliberate new
work. Stable project files, `workspace_id`, `job_id` and prepared tools are reused
while process, HOME, sandbox and `attempt_id` are new; mandatory security and readiness
checks are not bypassed. A verification rerun
may inspect an unfinalized copy without resuming implementation. The default
concurrency limit is eight workers (`max_workers`, configurable 1–64).
Eligible assignments remain worker-owned when slots are busy; launches wait FIFO.
Use `config set --project --max-workers N` or `--global` to save a limit, or
`worker --max-workers N` for one launch. `--no-wait` returns capacity code 75.
Default queue and total timeout are unlimited; explicit `--timeout` includes queue time.
Before secrets or provider launch, queued work rechecks off/access/routing/HEAD.

### Cache declarations, check limits and explicit recovery

Declare mutable build caches with repeatable `workspace prepare --cache-dir REL_DIR`,
not with an environment variable alone. A declaration must be rooted strictly inside
the copy, must not be the project root, `.git`, `.deepseek-tools` or a credential-like
path, and must not contain tracked files. Add it before execution only for a new or
empty tree, or for a tree covered by declared prepared copies; existing unprepared files
are preserved with a refusal and a new copy is the safe recovery. Do not expand the set
after execution. Removing a declaration before execution removes only its owned anchored
exclusions, not foreign rules. Cache contents stay out of the result patch but remain
inputs to check and preparation fingerprints, so large or unknown inputs can prevent
reuse. Recipe version 2 records `cache_dirs`; version 1 reads as `[]`, and an unknown
version is preserved.

```bash
deepseek-team workspace prepare WORKSPACE_ID \
  --copy /prepared/wrapper=gradle-home/wrapper \
  --copy /prepared/modules-2=gradle-home/caches/modules-2 \
  --cache-dir gradle-home \
  --env 'GRADLE_USER_HOME={workspace}/gradle-home' --save-project
```

`worker --check-timeout SECONDS` sets the limit for each managed check. It accepts finite
fractional values with `0 < SECONDS <= 86400` and defaults to `1800` (30 minutes) for a new job; invalid values are
rejected before a slot, copy or credential. Baseline and post checks use the same value,
clamped by the remaining total deadline; readiness and security probes keep their own
limits, and `worker --timeout` remains the total deadline including queue time. Record
the used value in attempt and reuse evidence. Resume inherits the last started attempt;
an explicit override starts a new attempt and invalidates reuse. Without `--timeout`,
`workspace verify` uses the recorded execution limit; an explicit verification override
does not change the limit inherited by a later resume or correction.
Legacy execution attempts without a saved limit retain 120 seconds. Supply
`--check-timeout 1800` explicitly when continuing one with the new default.

An assignment that is still planned and has no workspace, job, attempt or start can be
cancelled without launch:

```bash
deepseek-team coordination cancel --path PROJECT --task TASK_ID \
  --assignment ASSIGNMENT_ID --evidence TEXT
```

A live launch or queue ticket refuses with code `75`; stop it first. History records a
cancelled-before-start assignment, pending scope is released, no fictional execution,
checks, cost or quality is created, and repetition is idempotent. `base_head` is
immutable. Finish or cancel assignments, record outcomes and complete the task, then let
the next `UserPromptSubmit` open a new task at the current `HEAD`; do not rebase the old
task or create a task ID by hand.

Ordinary cancellation, finalization and GC attempt safe capture first. If capture fails,
preserve the copy and unknown state. Discard requires real user authorization to lose that
copy's work; never infer it or add `--discard-result` automatically:

```bash
deepseek-team workspace cancel WORKSPACE_ID --discard-result --reason TEXT
deepseek-team coordination finalize --path PROJECT --task TASK_ID \
  --assignment ASSIGNMENT_ID --cancel --discard-result --evidence TEXT
```

Discard is for cancellation only, never acceptance. It needs a non-empty reason or
evidence and still passes ownership, inode, mount, symlink and liveness checks. Only the
named capture failures `git_integrity_mismatch`, `capture_too_large`,
`capture_unavailable` and `capture_failed` permit it; unknown failures preserve the copy.
A successful capture remains schema 1 even when the flag was supplied. An uncaptured
result is schema 2 with `capture.status="discarded"`, `capture.reason_code` and
`patch=null`; it never satisfies acceptance or integration.

Schema 2 keeps an existing `changes.patch` at its path and lists it under
`historical_artifacts` with `file`, `bytes`, `sha256`, `attempt_id` and `provenance`. Use
`verified_attempt` only when durable history links those bytes to that attempt; otherwise
use `legacy_unattributed` with no attempt ID. The filename or a fresh hash never makes
the file the current patch. Existing checks, diagnostics and history stay at their paths;
arbitrary files are not evidence. Status, history, completion, finalization and GC read
schemas 1 and 2; unknown schemas fail safely. A 0.10.5 binary does not support schema 2, so
downgrade is not recovery. A durable `job_finalization_request` resumes cleanup after a
crash without another model run or capture.

Plan validation reports every independent structural or enum error before any partial
routing or ledger write, keeps text on `stderr`, exits `64`, and names the deliverable and
changed fields without dumping values. Invalid or unsafe settings exit `78` before the
provider; missing settings keep the normal fallback. A queued launch whose access changed
reports `Current effective access no longer permits writes.` with the access and safe
source names, without dumping settings. Do not turn a refused implementation into a
protected kind, and do not invent placeholder checks.

### Workspace and branch hygiene

This is coordinator process guidance, separate from the worker OS sandbox. Each write-capable
worker already runs in a fully independent Git repository created under the private state
directory by `workspace.create`; its internal `deepseek/<id>` ref lives only in that private
copy and is never a branch or worktree of the source project. Reuse that existing isolation
instead of building isolation in the project. Do not create a synthetic branch, worktree or
commit merely to invoke DeepSeek: a worker launch needs no project branch.

When new coordinator isolation is genuinely required, prefer a detached worktree
(`git worktree add --detach`) over a named task branch, and record which temporary branch or
worktree the coordinator created and why. After accepted integration, verify a clean status
and commit reachability before removing only the coordinator's own temporary worktree and its
fully merged branch. Preserve active, failed, unaccepted and foreign work: never bulk-prune,
force-delete or rewrite work you do not own. Finalization is explicit and limited to the
accepted or cancelled job: `workspace finalize`/`workspace cancel` removes its checkout,
`.git`, `.deepseek-tools` and owned temporary environments while preserving compact history,
the final `changes.patch`, diagnostics, provenance and lessons. Use `gc --dry-run` before
cleanup of known owned roots; mtime and a broad prune never authorize deletion. Worker copies
and coordinator worktrees remain outside the project until that lifecycle completes.

### Integration boundary

Codex: distribution/source-mutation gates use supported lifecycle hooks, but native
hook trust is owned by Codex and must be reviewed there once; DeepSeek Team does not
bypass or infer it. Hooks are a coordinator-process guardrail, not a replacement for
the worker Bubblewrap/AppArmor security boundary.

The parent launch/message gate covers only the events the runtime actually delivers, not
every possible write. Each coordinator runtime's coverage is verified separately, so
universal hook coverage must never be claimed; where the runtime cannot intercept an action
these instructions remain authoritative. The mandatory worker OS sandbox is unchanged.

For every runtime, an unplanned turn with no recorded work closes without requiring
a distribution plan or claiming implementation completion. Status prompts retain
existing unfinished tasks. Stop requests continuation for unfinished assignments,
undisposed worker results and missing coordinator/native outcomes. A repeated Stop
warns and keeps the ledger unfinished without vetoing another hook's continuation.

Once every worker result has a disposition and every coordinator or native-agent
outcome is a current accepted or cancelled, close the task explicitly with
`deepseek-team coordination complete --path REPO --task TASK_ID`. It reuses the same
validation on demand, independently of Stop, which still completes a delivered task
automatically.

Claude Code: after `setup --runtime claude` and project attachment, user-level hooks
inject the current policy and enforce the same ledger distribution through PreToolUse.
They cover Edit, Write, NotebookEdit and recognized mutating Bash commands, not every
possible write through arbitrary commands or external tools. Stop requests a continuation
for unfinished assignments/results; on a repeated Stop it warns and keeps the task
unfinished in the ledger to avoid a loop. Native-subagent hook events are outside this
coordinator gate. Check active hooks with Claude `/hooks`; `--bare` and native settings
can disable them. DeepSeek child workers retain their isolated HOME and `--bare` mode.
In Claude plan mode, native Markdown plan files can be edited before registering the
distribution. The plan directory comes from the default or user/project/local
`plansDirectory` setting. Stop leaves the task open during planning.

Qwen (DeepSeek Harness): after `setup --runtime qwen` and project attachment, the
package-owned DSH bridge plugin in the active profile translates `agent/created`,
`agent/pre-step`, `tools/pre-execute` and `agent/turn-stopping` events into the same
coordination hooks (`coordinator-hook --runtime qwen`). It covers recognized read,
edit/write, bash and native-dispatch tool calls, not every possible action; a hook
failure on a recognized mutation denies the call. The Qwen coordinator runs inside
DSH and is not a worker CLI: DeepSeek workers use the package-native DeepSeek loop
(`worker --runtime qwen`) inside the same mandatory Bubblewrap/AppArmor sandbox, so
no Codex or Claude Code installation is required. Native DSH subagents and team
members never open or complete the coordinator ledger; their dispatch needs the same
`[deepseek-team:TASK_ID:DELIVERABLE_ID]` binding. Stop requests continuation for
unfinished assignments/results; on a repeated Stop it warns and keeps the task
unfinished. Check the bridge with `deepseek-team hooks status --runtime qwen`; the
DSH profile must load the plugin (profile reload or restart). Qwen shares the
`AGENTS.md` managed block with Codex; each session's hooks inject the complete
current policy for their own runtime, so the shared file snapshot never governs
alone. DSH plan mode has no native plan-file exception: mutations stay gated.

Coordinator-native subagents run under the coordinator runtime's own native agent model,
permissions and sandboxing; they are not placed inside the DeepSeek Team worker sandbox.
This does not change the isolation of DeepSeek workers.

No DeepSeek worker stages, commits, pushes, deploys, publishes, accesses production,
reads coordinator secrets, or delegates to another agent.
