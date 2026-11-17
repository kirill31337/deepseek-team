# DeepSeek Team

- Preserve user changes and credentials; never include keys, local config or raw model logs in Git.
- Codex or Claude Code may be the coordinator. The coordinator owns architecture, security, integration and final verification.
- Delegate bounded independent work to DeepSeek only when it replaces coordinator work. Use the explicit matching runtime (`--runtime codex` or `--runtime claude`).
- Resolve `deepseek-team config show --effective --instructions` before each assignment. Respect explicit access independently of the 25/50/75 target profile. At 50/75 full-access, delegate independent implementation before doing that same work yourself.
- Full-access workers use owned isolated development copies, may change project files and run local tests/builds. The coordinator prepares copies/dependencies and owns final integration.
- The Linux OS sandbox is required for workers. Do not add `--os-sandbox off` to normal/project-managed workflows and do not disable Ubuntu's AppArmor unprivileged-userns restriction globally to make tests pass.
- Read-only jobs retain the hybrid native Codex / outer Claude boundary. Managed development copies use a sparse outer Bubblewrap namespace for both, a read-only Git directory and a private network with only a fixed provider relay; never launch the managed runtime command outside that boundary.
- Do not duplicate a live worker's assigned investigation. Wait for the full result without an overall timeout; review its actual diff and run meaningful tests.
- Workers must not stage, commit, push, publish or deploy. Read-only jobs cannot write; full-access permits local checks in its owned copy. Managed implementation jobs do not retry automatically; inspect partial work before explicit continuation.
- Run tests with `PYTHONPATH=src python3 -m unittest discover -s tests -v`.
- This repository contains the reusable package only; do not copy unrelated product code, project policies, secrets, or history.

<!-- codex-deepseek-team:managed-block:start -->
<!-- codex-deepseek-team:original:existing-content -->
## DeepSeek delegation

Before delegating, check `deepseek-team status` and resolve the CURRENT policy.
The user can request `deepseek-team off` or `deepseek-team on` for this project;
these commands persist across sessions for Codex, Claude Code and Qwen (DSH). They preserve
instructions, credentials and delegation/access/effort settings. Never turn delegation
back on without the user's request. If disabled, continue locally: the delegation and
coordination-plan requirements below do not apply. Existing workers are not cancelled;
inspect their results before integration. Check again before every new assignment:


```bash
deepseek-team config show --effective --instructions --runtime qwen
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
with `deepseek-team doctor --runtime qwen --offline`, including read-only
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
        "runtime": "qwen", "model": "deepseek-flash", "effort": "high",
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
deepseek-team worker --runtime qwen --effort high \
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

### Effective delegation profile: auto (adaptive) / full-access; effort=auto
Percentages are target profiles of useful work, not call/token/line quotas. Do not manufacture tasks to reach a percentage. For a genuinely small single-output task, record the small classification and concrete scope instead of creating a fake worker. The coordinator owns architecture, security decisions, final verification, integration, secrets/signing, commit/push and production actions. These responsibilities do not by themselves reserve ordinary implementation, tests, fixtures, documentation or non-secret metadata from workers. Do not calculate an actual useful-work percentage from calls, deliverable counts, lines or files.
DeepSeek Team workers always use deepseek-flash. Effort policy is auto, so before each DeepSeek assignment the frontier coordinator must choose --effort low, --effort high or --effort max from the assigned task without asking the user: low for bounded/mechanical work, high for the normal case, and max for difficult debugging, cross-file reasoning or adversarial review. The legacy spelling --effort medium is still accepted and treated exactly as high. If a DeepSeek worker is launched directly without a frontier-selected effort, the runner uses high as an execution fallback only.
Orient before you solve: register the bounded diagnostic, test-plan and implementation slices you need before extensive self-investigation of unfamiliar source. An unknown global attribute stays unknown - decompose it into a bounded diagnostic deliverable whose result informs the plan instead of claiming a safe implementation. Batch every independent, eligible scope into one distribution and launch those assignments before duplicating that work yourself; one plan keeps the overhead down. In a SUBSTANTIAL Auto plan every ordinary read or write deliverable must carry executor: "auto"; an explicit executor: "coordinator" or executor: "worker" is rejected there. A genuinely small single-output task, a manual 25/50/75 profile, a protected coordinator responsibility and a valid native exception keep their existing behavior. Never promise a contribution percentage and never widen access automatically.
Plan and route every delegated subtask before assigning another agent, even a small read-only history, search or review task; a worker decision means DeepSeek. Generic parallelism, isolated context or convenience alone is not a sufficient reason to use a native subagent. Ordinary short answers the coordinator gives directly need no fake worker or plan; wait and control calls create no work, and new work sent to an already running agent also requires routing.
Coordinator-native subagents remain available only as an explicit exception. Represent that choice in the plan with executor: "native-agent", a concrete delegation_reason, and a native_exception: either {"code": "explicit_user_request", "evidence": "specific user request"} or {"code": "native_capability", "capability": "specific capability or tool unavailable to a DeepSeek worker", "evidence": "why it is required"}. The coordinator attests this evidence; it is not mechanically proven user provenance. In auto mode use executor: "auto" and let the router resolve DeepSeek or coordinator. The native prompt or message must include [deepseek-team:TASK_ID:DELIVERABLE_ID] binding the registered native-agent scope, and its accepted or cancelled outcome is recorded like any other deliverable. Native agents complement DeepSeek workers and do not satisfy DeepSeek worker assignments required by the effective profile. Protected coordinator responsibilities remain with the coordinator: architecture, security, integration, final verification, secrets/signing and publishing stay coordinator-only. DeepSeek workers themselves remain leaf workers and must never delegate.
Protected scopes describe read or review context; they no longer grant generic source-write permission. An architecture or security report may list decision_artifacts with exact relative .md, .rst or .txt files contained in its scope (for example decision_artifacts: ["docs/design.md"]). An integration deliverable requires integration_of naming the completed implementation deliverable ids and write_scope naming the exact relative files (for example integration_of: ["implementation"] and write_scope: ["src/example.py"]), backed by actual worker changes or by accepted coordinator edits recorded in the ledger. A read-only review never confers integration write rights. Newly discovered implementation needs a separately routed deliverable; substantial Auto plans use executor: "auto". Corrections within reviewed worker output remain coordinator rework when performed by the coordinator; substantive corrections normally return to DeepSeek.
Auto delegates suitable work immediately by default. Actively split substantial work into meaningful independent implementation, test, fixture, documentation and review deliverables with executor:auto. Prepare interfaces and acceptance criteria before assigning; do not retain eligible work merely because history is missing or worker slots are busy.
Delegation level is auto: the profile is adaptive per task, chosen from local outcomes and bounded external evidence instead of a fixed percentage. New auto plans must record executor: "auto" together with a "features" card capturing the task before execution: kind, domain, operation, localization, coupling, verification, clarity, risk, scope_size, runtime, model, effort and context_version. The coordinator classifies the task; the router resolves its saved feature card. Workers must not self-select a profile or stage. Admission is immediate: suitable small/medium low/medium-risk local/component tasks with clear requirements, known/partial localization and declared tests or a reproducer can start without prior evidence. Read-only deliverables and documentation may use explicit manual acceptance criteria. High/protected/unknown-risk, unbounded or unverifiable work stays with the coordinator. There is no initial trial quota or periodic coordinator holdout. Missing data and unknown prices do not block suitable work and never imply measured savings; supported poor economics still veto delegation. A rejected result or three distinct recent rework cases pause only that family for the configured cooldown (300 seconds by default). Explicit assessments restrict these failures to worker/shared major_gaps or unusable; cosmetic, minor-gap and neutral assessments never trigger the pause. One rework is recorded without pausing the family; after a pause immediate admission resumes. There is no automatic paid exploration outside the normal task stream and no automatic permission widening; keep the resolved access and explicit executor choices. Inspect adaptive routing state with deepseek-team routing status and adjust it with deepseek-team routing configure.
Full-access is development inside an owned isolated copy, not host access. Allow the worker to create/edit/delete project files in its assigned copy and run declared local checks. Prepare missing dependencies with workspace prepare. If selected uncommitted source is required, import only those files with workspace import; it is recorded as coordinator-prepared source, not worker output. Host SDK/JDK/tools are not assumed to exist inside the sandbox.
### Workspace and branch hygiene
Coordinator process guidance, separate from the worker OS sandbox. Each write-capable worker already runs in a fully independent Git repository created under the private state directory by workspace.create; its internal deepseek/<id> ref lives only in that private copy and is never a branch or worktree of the source project. Reuse that existing isolation instead of building isolation in the project. Create no synthetic branch, worktree or commit merely to invoke DeepSeek: a worker launch needs no project branch. When new coordinator isolation is genuinely required, prefer a detached worktree (git worktree add --detach) over a named task branch, and record which temporary branch or worktree the coordinator created and why. After accepted integration, verify a clean status and commit reachability before removing only the coordinator's own temporary worktree and its fully merged branch. Preserve active, failed, unaccepted and foreign work: never bulk-prune, force-delete or rewrite work you do not own. Finalization is explicit and limited to the accepted or cancelled job: workspace finalize/cancel removes its checkout, .git, .deepseek-tools and owned temporary environments while preserving compact history, the final changes.patch, diagnostics, provenance and lessons. Use gc --dry-run before any cleanup of known owned roots; mtime and a broad prune never authorize deletion. Worker copies and coordinator worktrees remain outside the project until that lifecycle completes. This is not worker OS enforcement.
Qwen (DSH) process integration: after project init and enabling hooks in the runtime, SessionStart/UserPromptSubmit provide the current coordination task id. Plan and route every delegated subtask, even a small read-only history, search or review task, before assigning another agent; Auto registers executor:auto and a worker decision means DeepSeek. Before coordinator source edits, submit a concrete JSON distribution with deepseek-team coordination plan --task TASK_ID; include deliverable id/kind/scope, executor, acceptance criteria, dependencies and checks. Run each worker assignment with deepseek-team worker --runtime qwen --effort high --coord-task TASK_ID --coord-assignment ASSIGNMENT_ID. Because effort policy is auto, replace high with low or max when the assigned task warrants it (medium is the accepted legacy alias for high). The runner records start/result/workspace/checks automatically. One job_id owns one stable workspace across preparation, failed process/check/API, review gaps, needs-rework, explicit resume and linked corrections; each run has a distinct attempt_id and there is no automatic retry. Process success is awaiting review: integrate and verify accepted changes before finalization. After reviewing a result, record its use with deepseek-team coordination use; repeated feedback is allowed, --disposition needs-rework keeps the copy, and --final makes the final accepted or rejected disposition finalize the job. Explicit alternatives are coordination finalize and workspace finalize/cancel. workspace recover and workspace resume repair ownership only and never run a model; worker --workspace ID --resume-after-failure continues a failed attempt in the same inode. After needs-rework, a succeeded result that needs a substantive correction is planned with correction_of for the original reviewed assignment; the runner resolves the same unfinalized workspace automatically, --workspace is optional unless an explicit value conflicts, and missing/ambiguous/stale/foreign/finalized state blocks instead of allocating a fresh copy. Stable project files, workspace_id/job_id and prepared tools are reused while process/HOME/sandbox/attempt_id are new; security and readiness checks are never bypassed. SessionStart compact and UserPromptSubmit summaries expose the durable workspace_id, job_id, attempt_id and correction reference from the private ledger. A failed same-assignment attempt still resumes only explicitly with --resume-after-failure and cannot switch copies. Record verified coordinator and native-agent outcomes with deepseek-team coordination result; accepted or explicitly cancelled outcomes are required for completion. Later recognized scope mutations require fresh acceptance. New substantial scope requires a revised plan. Qwen (DSH) PreToolUse blocks recognized source edits while the distribution is missing/noncompliant, blocks unplanned scope, and blocks duplicate work owned by a pending worker assignment. This parent launch/message gate covers only the events the runtime actually delivers, not every possible write; each coordinator runtime's coverage is verified separately and universal hook coverage must never be claimed. Where the runtime cannot intercept an action, these instructions remain authoritative. Hooks do not replace the mandatory, unchanged worker OS sandbox. An unplanned turn without recorded work closes without a distribution plan; status prompts retain existing unfinished tasks. Stop requests continuation for unfinished assignments/results, including coordinator and native-agent outcomes. On repeated Stop it warns and keeps the ledger unfinished without vetoing another hook continuation. The lifecycle hook lets the first recognized source inspection through with an early-planning reminder and records it as work; a registered distribution is required before any further supported source read. Real status/bootstrap checks and purely conversational turns stay exempt, and tool recognition is bounded, never universal. Qwen coordinates inside the DeepSeek Harness (DSH). The package-owned DSH bridge plugin translates agent/created, agent/pre-step, tools/pre-execute and agent/turn-stopping events into the same coordination hooks; it covers recognized read/edit/write/bash and native dispatch tools, never every possible action. Workers use the package-native DeepSeek loop (--runtime qwen) in the same mandatory OS sandbox; no Codex or Claude Code installation is required. Stop requests a continuation for pending/undispositioned results; if stop_hook_active is already true, it warns and leaves the task unfinished instead of blocking again. Native DSH subagents and team members do not change the coordinator ledger. Check the bridge with deepseek-team hooks status --runtime qwen; the DSH profile must load the plugin (profile reload or restart), and deepseek-team init --coordinator qwen manages the shared AGENTS.md block.
Execution capacity is 8; configure max_workers independently of access. Plan all eligible deliverables; launch independent jobs concurrently and let excess jobs wait in FIFO order. Capacity is not a reason to retain their implementation with the coordinator. Report accepted work, verification results and any rework; only report money savings when measured.
While a worker runs, work only on independent scope. Review the actual diff and recorded checks without repeating the whole investigation or rewriting correct code. DeepSeek workers never stage, commit, push, publish, deploy, access production services or delegate.
### Graded review and corrective work
Grade each assignment yourself after reviewing its actual diff and declared checks; the coordinator supplies the assessment and a worker never self-grades. Record the operational outcome with `coordination use --disposition ... --evidence ...`, add the structured cause with `coordination use --rework-json FILE` when the result needed corrections, and add the explicit assessment with `coordination use --quality-json FILE` in the same call. An assessment is exactly `{grade, attribution, evidence}`: grades `met` (1.0), `minor_gaps` (0.8), `major_gaps` (0.3), `unusable` (0.0) and `unassessable` (neutral); attribution is `worker`, `shared`, `coordinator`, `environment` or `unknown`. Only worker or shared attribution with an assessable grade supplies a quality score, and shared evidence must name the worker-attributable gap against the supplied requirements or acceptance criteria. These rubric weights are declared heuristics, not measured probabilities or work-share percentages. Missing brief or context, or requirements changed during the assignment, is neutral for worker quality on its own; a cosmetic or preference edit still counts as `met`, while a low editing cost never excuses a serious defect. Omitted quality keeps the legacy binary reading for compatibility, and older outcomes may be reassessed only by appending an explicit assessment: recorded history is never rewritten, and if several explicit assessments exist the lowest scored worker grade is retained. New independent successful assignments add positive evidence and can restore the class rating; old evidence loses weight over time, while duplicate feedback never manufactures another success.
Corrections: record the concrete defect evidence and the original `needs-rework` first, then route a bounded correction brief as its own assignment declaring `correction_of`; it links the original assignment, states the concrete defect and the context the first attempt lacked, and carries executable acceptance checks. The runner resolves its saved workspace automatically, so a conflicting explicit `--workspace` fails rather than selecting another copy. Substantive corrections normally return to DeepSeek; a minor bounded correction may be performed by the coordinator with its concrete reason recorded. Correction size and worker quality are separate judgements. No blind retries, no direct resume of a completed assignment, no `off` bypass, no sandbox bypass and no access widening; architecture, security, integration and final verification stay with the coordinator.
### Final reporting of performed work
For final summaries of performed work while delegation is enabled, end with short bullets in the user's language that separate work the coordinator completed personally from work actually delegated to DeepSeek, and name accepted worker results plus any rework, rejection or failure. Cover the reported task, including work and assignments from earlier turns. Never describe planned, running, failed or rejected work as completed. If nothing was delegated for the reported task, say so explicitly. After the bullets, give a coarse approximate coordinator/DeepSeek split of ACCEPTED WORK as two whole-number percentages totaling 100 percent, in the user's language, and label it exactly "subjective estimate, not measured" (translated into that language). Judge that split qualitatively from accepted scope and complexity and from coordinator review and rework. Describe the accepted scope and any specific rework first. The split is not a measured delegation rate, a performance metric or routing feedback; it only summarizes accepted work. Never derive it from counts of calls, tasks, deliverables, files, lines, tokens, time or bullets; never reuse a configured Auto/25/50/75 profile; and never claim measured productivity or money, time or token savings. If even a rough estimate lacks supporting evidence, report the estimate as unavailable instead of inventing numbers. With no accepted worker contribution, use 100/0 for accepted work while still disclosing any failed or rejected attempts. Credit coordinator-native subagent work separately, never as the coordinator's own personal work and never as DeepSeek work; if it is included on the coordinator side of the split, say so explicitly. This is a reporting instruction only: it calculates no ratio, records no telemetry, adds no flag and changes no ledger schema, and the existing ban on percentages calculated from counts still stands. Disabled delegation, status-only turns and turns without performed work need no performed-work report.

<!-- codex-deepseek-team:overhead-contract:v1 -->
# Versioned overhead contract (v1)

Supplements, never edits, `docs/agents/delegation.md`.

## Software-enforced guarantees
- `checks_reusable`: strict optional bool, default `false`; `true` attests deterministic project-local checks with no network, clock, random, provider, production, mutable service, runtime-home or temporary dependency. No universal proof exists for arbitrary shell dependencies; absent or unknown executes.
- Reuse requires unchanged ordered commands, timeout, runtime/access, direct executables, prepared declarations, ordinary project inputs (including ignored/untracked), modes, sanitized environment, complete exit-zero evidence safe under current redactions and matching input snapshots before/after. Credential-like files or unknown inputs disable reuse. The key is not a transitive OS/library snapshot: unrepresented environment changes require fresh checks. Force, no attestation, failure, missing/altered/truncated evidence or changing files execute. Readiness/security/containment probes always run. `workspace verify --reuse-checks` needs attestation; `--force` and managed `--force-checks` execute. Reused results cite original evidence; final acceptance never relies only on a reused check.
- Identical normalized distribution is a no-op preserving IDs, routing, phase, outcomes and timestamps. Scope, criteria, checks, dependencies, routing features and reuse declarations join that identity. Confirmed `checks_reusable` and started scope stay immutable; new scope gets new IDs.
- Bounded private records keyed by canonical project and verified conditions remember identified pre-provider runtime, dependency, probe and materialization failures. A matching retained record avoids repeated preparation, extra copy allocation and provider access, including fresh explicitly prepared copies. Unknown inputs are uncached. Changes or `--recheck-environment` allow a fresh observation without bypassing recovery/admission/sandbox. Provider/auth, project-test, permission/admission and cancellation are not remembered.
- Enabled hooks expose this at SessionStart and every UserPromptSubmit, enforce delivered events, and never weaken mandatory Linux sandbox.

### Correction and workspace continuity

- A newly planned correction of a reviewed `needs-rework` assignment declares
  `correction_of` with the original `task_id` and `assignment_id` in its deliverable
  JSON. Before allocating or preparing a new copy, the runner resolves the original
  assignment's authoritative saved workspace; `--workspace` is optional and a
  conflicting value
  fails. Missing, ambiguous, stale, foreign, finalized or otherwise unsafe
  predecessor/copy state blocks safely instead of falling back to a new environment.
  Independent work can still start a new job, and a finalized job needs deliberate new
  work.
  The omitted-link guard checks overlapping known `needs-rework` scopes within the
  current coordination task at planning and launch, including unavailable copies.
  Cross-task semantic intent requires an explicit `correction_of` reference.
- A succeeded result needing corrections is marked `needs-rework`, then corrected
  through an explicitly planned `correction_of` assignment launched normally with
  `worker --coord-task ... --coord-assignment ...`, without remembering
  `--workspace`. A failed attempt of the same assignment continues only with explicit
  `--resume-after-failure` and cannot switch copies. Stable workspace, `workspace_id`,
  `job_id`, project files and prepared tools are reused; process, HOME, sandbox and
  `attempt_id` are new, and mandatory security and readiness checks still run.
- Both runtimes' SessionStart compact and UserPromptSubmit summaries show durable
  `workspace_id`, `job_id`, `attempt_id` and correction reference from the private
  ledger, independent of model recall. Unstructured semantic intent, missing host hook
  delivery and automatic agent obedience remain instruction limits, not universal
  program enforcement.

## Agent recommendations (not universal enforcement)
- Use one logical package/plan, batching independent scopes; revise for meaningful scope, requirement or dependency changes. Reuse immutable started work; record new scope separately. Software cannot prove semantic package boundaries or prevent invented IDs.
- Use blocking/event waits; inspect status only on failure, stall or cancellation, not frequent polling.
- Review actual diff, evidence and critical paths; investigate further only for missing, contradictory, failed or security-sensitive evidence, without repeating full investigation or rewriting correct code.

<!-- codex-deepseek-team:managed-block:end -->
