## DeepSeek delegation

Before delegating, check `deepseek-team status` and resolve the CURRENT policy.
The user can request `deepseek-team off` or `deepseek-team on` for this project;
these commands persist across sessions for both Codex and Claude Code. They preserve
instructions, credentials and delegation/access/effort settings. Never turn delegation
back on without the user's request. If disabled, continue locally: the delegation and
coordination-plan requirements below do not apply. Existing workers are not cancelled;
inspect their results before integration. Check again before every new assignment:


```bash
deepseek-team config show --effective --instructions --runtime {runtime}
```

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

After review, record the operational outcome and, when corrections were needed, the concrete defect evidence with the original `needs-rework`. A minor, tightly coupled correction may be performed by the coordinator when its concrete reason is recorded in the rework `summary`/`prevention` or the outer `--evidence`; substantive corrections normally return to DeepSeek as a bounded correction brief. That brief links the original assignment, states the concrete defect and its evidence, supplies the context the first attempt lacked, and carries a reproducer or executable acceptance checks; route it as its own deliverable, then verify the actual delta and integrate it. Correction size and worker quality are separate judgements: a one-line correctness or security defect can still be serious, a cosmetic edit can keep full credit, and a first failed attempt never mandates takeover. Architecture, security, integration and final verification stay coordinator-owned. No blind retries, no `off` bypass, no sandbox bypass and no automatic access widening; a completed assignment cannot be resumed directly, so a succeeded assignment needs a new planned correction linked to the original. A failed, inspected workspace may explicitly resume with `--resume-after-failure`. Repeated dispositions or attempts of one assignment stay one case, and the original failure is never laundered into a clean first pass; nothing here requires an endless loop or an artificial fixed quota.

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
deepseek-team worker --runtime {runtime} --effort high \
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

If selected uncommitted source is required, never copy the whole checkout:

```bash
deepseek-team workspace import WORKSPACE_ID --include path/to/needed.py
```

Those files are coordinator-prepared source and remain distinct from worker changes.
Prepare missing SDK/runtime/dependencies explicitly with `workspace prepare`.
Full-access never implies access to host-only JDK/SDK/tools, secrets, databases,
services, or the network. Declared dependencies are probed inside the real sandbox
before the provider credential is read; declared checks run in that sandbox after
the worker. Do not replace missing project dependencies with stubs and report that
as equivalent verification.

A failed or interrupted workspace is retained. Inspect it before explicit
`--resume-after-failure`; do not automatically repeat implementation over unknown
state. The default concurrency limit is eight workers (`max_workers`, configurable 1–64).
Eligible assignments remain worker-owned when slots are busy; launches wait FIFO.
Use `config set --project --max-workers N` or `--global` to save a limit, or
`worker --max-workers N` for one launch. `--no-wait` returns capacity code 75.
Default queue and total timeout are unlimited; explicit `--timeout` includes queue time.
Before secrets or provider launch, queued work rechecks off/access/routing/HEAD.

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
force-delete or rewrite work you do not own. There is no automatic cleanup engine and no broad
cleanup command; worker copies remain retained outside the project for review and recovery and
are never deleted automatically.

### Integration boundary

Codex: distribution/source-mutation gates use supported lifecycle hooks, but native
hook trust is owned by Codex and must be reviewed there once; DeepSeek Team does not
bypass or infer it. Hooks are a coordinator-process guardrail, not a replacement for
the worker Bubblewrap/AppArmor security boundary.

The parent launch/message gate covers only the events the runtime actually delivers, not
every possible write. Codex and Claude coverage is verified separately, so universal hook
coverage must never be claimed; where the runtime cannot intercept an action these
instructions remain authoritative. The mandatory worker OS sandbox is unchanged.

For both runtimes, an unplanned turn with no recorded work closes without requiring
a distribution plan or claiming implementation completion. Status prompts retain
existing unfinished tasks. Stop requests continuation for unfinished assignments,
undisposed worker results and missing coordinator/native outcomes. A repeated Stop
warns and keeps the ledger unfinished without vetoing another hook's continuation.

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

Coordinator-native subagents run under the coordinator runtime's own native agent model,
permissions and sandboxing; they are not placed inside the DeepSeek Team worker sandbox.
This does not change the isolation of DeepSeek workers.

No DeepSeek worker stages, commits, pushes, deploys, publishes, accesses production,
reads coordinator secrets, or delegates to another agent.
