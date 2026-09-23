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
these commands persist across sessions for both Codex and Claude Code. They preserve
instructions, credentials and delegation/access/effort settings. Never turn delegation
back on without the user's request. If disabled, continue locally: the delegation and
coordination-plan requirements below do not apply. Existing workers are not cancelled;
inspect their results before integration. Check again before every new assignment:


```bash
deepseek-team config show --effective --instructions --runtime codex
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

**Auto delegates useful bounded work immediately by default**. Small or medium tasks with low or medium risk, known or partial localization, local or component coupling, clear requirements and declared tests or a reproducer can be assigned from the first session. Manual verification is allowed for read, review, research, diagnostic, test-plan and documentation tasks with explicit acceptance criteria. Implementation requires executable checks and `full-access`.

Unknown costs stay unknown and do not block eligible work; supported poor measured economics still veto delegation. One rework is recorded without a family pause. A rejection or three distinct rework cases within the cooldown window pause the family for the configured `failure_cooldown_seconds` (300 seconds by default). Immediate admission resumes after the pause. Failed implementation never retries automatically.

Fresh Auto access remains **read-only**. To delegate implementation, explicitly opt in:

```bash
deepseek-team config set --project --access full-access
```

The coordinator decomposes meaningful independent slices, reviews actual diffs and declared checks, and reports accepted work and rework without repeating the worker's investigation or inventing percentage savings. Architecture, security, integration, final verification, commits and production remain coordinator-owned. Saved manual profiles, explicit permissions and `off` retain priority.

### Final reporting

Enabled final summaries of performed work add short bullets, in the user's language, that
separate work the coordinator completed personally from work actually delegated to DeepSeek,
and name accepted results plus any rework, rejection or failure. Cover the reported task,
including work and assignments from earlier turns. Planned, running, failed and
rejected work is never described as completed, and when nothing was delegated the summary
says so explicitly. After the bullets the coordinator states a coarse approximate
coordinator/DeepSeek split of ACCEPTED WORK as two whole-number percentages totaling 100 percent, in the
user's language and labelled exactly "subjective estimate, not measured" (translated). It is
judged qualitatively from accepted scope and complexity and from coordinator review and rework;
it is never derived from counts of calls, tasks, deliverables, files, lines, tokens, time or
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

The coordinator may also use its own **native subagents** when this materially improves
parallelism, isolated context, independent verification, or access to a native capability.
For a substantial planned deliverable, record this as `executor: "native-agent"` and add a
concrete `delegation_reason`. A native subagent is additive: it does **not** satisfy a
DeepSeek worker assignment required by the 50/75 profiles. Architecture/security decisions,
final integration/verification, secrets/signing, commit/push and production actions remain
coordinator-owned. DeepSeek workers themselves remain leaf workers and must not delegate.

### Process

For a substantial task, identify concrete deliverables before duplicating their
implementation. Each deliverable needs: id, kind, concrete scope, executor,
acceptance criteria, dependencies, and checks. `native-agent` executors additionally
need `delegation_reason` explaining why native delegation is useful for that scope. Architecture, security decisions,
final verification, integration, secrets/signing, commit/push and production stay
with the coordinator. That responsibility alone does not reserve ordinary
implementation, tests, fixtures, documentation, or non-secret metadata.

At 75/full-access, separable implementation/tests/fixtures/docs/non-secret metadata
default to worker assignments. Retaining worker-eligible work requires a supported
constraint; arbitrary prose such as "I own quality" or "this is release work" does
not satisfy the gate. At 50/full-access, a review-only worker does not substitute
for delegating a separable implementation/test/docs slice. Read-only overrides
never expand write authority.

For Codex, the installed stable user-level lifecycle hook is active only in projects
explicitly attached with `deepseek-team init --coordinator codex`. After native
Codex hook trust, SessionStart/UserPromptSubmit inject the persistent task state,
including after compaction. Before source mutation, register the distribution:

```bash
deepseek-team coordination plan --task TASK_ID <<'JSON'
{
  "classification": "substantial",
  "deliverables": [
    {
      "id": "implementation",
      "kind": "implementation",
      "scope": ["src/example.py"],
      "executor": "worker",
      "acceptance": ["focused behavior implemented"],
      "dependencies": [{"kind": "command", "value": "python3"}],
      "checks": ["python3 -m unittest tests.test_example -q"],
      "features": {
        "kind": "implementation", "domain": "python", "operation": "extend",
        "localization": "known", "coupling": "local", "verification": "tests",
        "clarity": "clear", "risk": "low", "scope_size": "small",
        "runtime": "codex", "model": "deepseek-flash", "effort": "high",
        "context_version": "project-v1"
      }
    }
  ]
}
JSON
```

This example explicitly chooses a worker for a manual profile. With profile Auto,
set its executor to `auto` and inspect the resolved plan. Start a worker only when
the plan returns a worker assignment; retain a coordinator decision with the coordinator.
Use the assignment id returned by the plan:

```bash
deepseek-team worker --runtime codex --effort high \
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

### Integration boundary

Codex: distribution/source-mutation gates use supported lifecycle hooks, but native
hook trust is owned by Codex and must be reviewed there once; DeepSeek Team does not
bypass or infer it. Hooks are a coordinator-process guardrail, not a replacement for
the worker Bubblewrap/AppArmor security boundary.

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

### Effective delegation profile: auto (adaptive) / full-access; effort=auto
Percentages are target profiles of useful work, not call/token/line quotas. Do not manufacture tasks to reach a percentage. For a genuinely small single-output task, record the small classification and concrete scope instead of creating a fake worker. The coordinator owns architecture, security decisions, final verification, integration, secrets/signing, commit/push and production actions. These responsibilities do not by themselves reserve ordinary implementation, tests, fixtures, documentation or non-secret metadata from workers. Do not calculate an actual useful-work percentage from calls, deliverable counts, lines or files.
DeepSeek Team workers always use deepseek-flash. Effort policy is auto, so before each DeepSeek assignment the frontier coordinator must choose --effort low, --effort high or --effort max from the assigned task without asking the user: low for bounded/mechanical work, high for the normal case, and max for difficult debugging, cross-file reasoning or adversarial review. The legacy spelling --effort medium is still accepted and treated exactly as high. If a DeepSeek worker is launched directly without a frontier-selected effort, the runner uses high as an execution fallback only.
Coordinator-native subagents remain available. Use them only when parallelism, isolated context or a native capability materially helps. Represent that choice in the plan with executor: "native-agent" plus a concrete delegation_reason. Native agents complement DeepSeek workers and do not satisfy DeepSeek worker assignments required by the effective profile. Protected coordinator responsibilities remain with the coordinator. DeepSeek workers themselves remain leaf workers and must never delegate.
Auto delegates suitable work immediately by default. Actively split substantial work into meaningful independent implementation, test, fixture, documentation and review deliverables with executor:auto. Prepare interfaces and acceptance criteria before assigning; do not retain eligible work merely because history is missing or worker slots are busy.
Delegation level is auto: the profile is adaptive per task, chosen from local outcomes and bounded external evidence instead of a fixed percentage. New auto plans must record executor: "auto" together with a "features" card capturing the task before execution: kind, domain, operation, localization, coupling, verification, clarity, risk, scope_size, runtime, model, effort and context_version. The coordinator classifies the task; the router resolves its saved feature card. Workers must not self-select a profile or stage. Admission is immediate: suitable small/medium low/medium-risk local/component tasks with clear requirements, known/partial localization and declared tests or a reproducer can start without prior evidence. Read-only deliverables and documentation may use explicit manual acceptance criteria. High/protected/unknown-risk, unbounded or unverifiable work stays with the coordinator. There is no initial trial quota or periodic coordinator holdout. Missing data and unknown prices do not block suitable work and never imply measured savings; supported poor economics still veto delegation. A rejected result or three distinct recent rework cases pause only that family for the configured cooldown (300 seconds by default). One rework is recorded without pausing the family; after a pause immediate admission resumes. There is no automatic paid exploration outside the normal task stream and no automatic permission widening; keep the resolved access and explicit executor choices. Inspect adaptive routing state with deepseek-team routing status and adjust it with deepseek-team routing configure.
Full-access is development inside an owned isolated copy, not host access. Allow the worker to create/edit/delete project files in its assigned copy and run declared local checks. Prepare missing dependencies with workspace prepare. If selected uncommitted source is required, import only those files with workspace import; it is recorded as coordinator-prepared source, not worker output. Host SDK/JDK/tools are not assumed to exist inside the sandbox.
Codex process integration: after project init and enabling hooks in the runtime, SessionStart/UserPromptSubmit provide the current coordination task id. For every substantial task, before coordinator source edits, submit a concrete JSON distribution with deepseek-team coordination plan --task TASK_ID; include deliverable id/kind/scope, executor, acceptance criteria, dependencies and checks. Run each worker assignment with deepseek-team worker --runtime codex --effort high --coord-task TASK_ID --coord-assignment ASSIGNMENT_ID. Because effort policy is auto, replace high with low or max when the assigned task warrants it (medium is the accepted legacy alias for high). The runner records start/result/workspace/checks automatically. After reviewing a result, record its use with deepseek-team coordination use. Record verified coordinator and native-agent outcomes with deepseek-team coordination result; accepted or explicitly cancelled outcomes are required for completion. Later recognized scope mutations require fresh acceptance. New substantial scope requires a revised plan. Codex PreToolUse blocks recognized source edits while the distribution is missing/noncompliant, blocks unplanned scope, and blocks duplicate work owned by a pending worker assignment. Hook coverage depends on the tools and native runtime settings; it does not replace the worker OS sandbox. An unplanned turn without recorded work closes without a distribution plan; status prompts retain existing unfinished tasks. Stop requests continuation for unfinished assignments/results, including coordinator and native-agent outcomes. On repeated Stop it warns and keeps the ledger unfinished without vetoing another hook continuation. Native hook trust is controlled by Codex and is not inferred by this package.
Execution capacity is 8; configure max_workers independently of access. Plan all eligible deliverables; launch independent jobs concurrently and let excess jobs wait in FIFO order. Capacity is not a reason to retain their implementation with the coordinator. Report accepted work, verification results and any rework; only report money savings when measured.
While a worker runs, work only on independent scope. Review the actual diff and recorded checks without repeating the whole investigation or rewriting correct code. DeepSeek workers never stage, commit, push, publish, deploy, access production services or delegate.
### Final reporting of performed work
For final summaries of performed work while delegation is enabled, end with short bullets in the user's language that separate work the coordinator completed personally from work actually delegated to DeepSeek, and name accepted worker results plus any rework, rejection or failure. Cover the reported task, including work and assignments from earlier turns. Never describe planned, running, failed or rejected work as completed. If nothing was delegated for the reported task, say so explicitly. After the bullets, give a coarse approximate coordinator/DeepSeek split of ACCEPTED WORK as two whole-number percentages totaling 100 percent, in the user's language, and label it exactly "subjective estimate, not measured" (translated into that language). Judge that split qualitatively from accepted scope and complexity and from coordinator review and rework. Never derive it from counts of calls, tasks, deliverables, files, lines, tokens, time or bullets; never reuse a configured Auto/25/50/75 profile; and never claim measured productivity or money, time or token savings. If even a rough estimate lacks supporting evidence, report the estimate as unavailable instead of inventing numbers. With no accepted worker contribution, use 100/0 for accepted work while still disclosing any failed or rejected attempts. Credit coordinator-native subagent work separately, never as the coordinator's own personal work and never as DeepSeek work; if it is included on the coordinator side of the split, say so explicitly. This is a reporting instruction only: it calculates no ratio, records no telemetry, adds no flag and changes no ledger schema, and the existing ban on percentages calculated from counts still stands. Disabled delegation, status-only turns and turns without performed work need no performed-work report.

<!-- codex-deepseek-team:managed-block:end -->
