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

**Auto delegates useful bounded work immediately by default**. Small or medium tasks with low or medium risk, known or partial localization, local or component coupling, clear requirements and declared tests or a reproducer can be assigned from the first session. Manual verification is allowed for read, review, research, diagnostic, test-plan and documentation tasks with explicit acceptance criteria. Implementation requires executable checks and `full-access`.

Unknown costs stay unknown and do not block eligible work; supported poor measured economics still veto delegation. One rework is recorded without a family pause. A rejection or three distinct rework cases within the cooldown window pause the family for the configured `failure_cooldown_seconds` (300 seconds by default). Immediate admission resumes after the pause. Failed implementation never retries automatically.

Fresh Auto access remains **read-only**. To delegate implementation, explicitly opt in:

```bash
deepseek-team config set --project --access full-access
```

The coordinator decomposes meaningful independent slices, reviews actual diffs and declared checks, and reports accepted work and rework without repeating the worker's investigation or inventing percentage savings. Architecture, security, integration, final verification, commits and production remain coordinator-owned. Saved manual profiles, explicit permissions and `off` retain priority.

Every worker requires the Linux OS sandbox. Do not weaken Ubuntu AppArmor
user-namespace restrictions to make delegation pass.

### Model, effort and native subagents

DeepSeek Team workers always use **`deepseek-flash`**. Do not route to another DeepSeek
model. Resolve the current effort policy with the effective config before assigning work.
The default is `effort=auto`: in that mode the frontier coordinator chooses the effort
from the actual delegated scope and passes `--effort low|medium|high` (`low` for bounded
or mechanical work, `medium` for the normal case, `high` for difficult debugging,
cross-file reasoning, security-sensitive review or adversarial verification). If the
resolved policy is explicitly `low`, `medium` or `high`, treat it as the saved forced
level for new DeepSeek jobs and do not auto-select another value. `config set` can persist
that choice per project or globally, and setting it back to `auto` restores frontier
selection. A direct worker launched while policy is auto falls back to `medium` only when
no frontier-selected concrete effort reaches the runner.

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
        "runtime": "{runtime}", "model": "deepseek-flash", "effort": "medium",
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
deepseek-team worker --runtime {runtime} --effort medium \
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
