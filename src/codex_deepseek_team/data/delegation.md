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

Keep the required Linux OS sandbox enabled for managed workers. Never add `--os-sandbox off`
to ordinary coordination flows and do not weaken Ubuntu AppArmor
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
      "checks": ["python3 -m unittest tests.test_example -q"]
    }
  ]
}
JSON
```

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
state. At most three workers may run and the default total timeout remains unlimited.

### Integration boundary

Codex: distribution/source-mutation gates use supported lifecycle hooks, but native
hook trust is owned by Codex and must be reviewed there once; DeepSeek Team does not
bypass or infer it. Hooks are a coordinator-process guardrail, not a replacement for
the worker Bubblewrap/AppArmor security boundary.

Claude Code: managed instructions and worker accounting are available, but coordinator
distribution enforcement is instruction-driven in this release; no Claude
PreToolUse technical block is claimed.

Coordinator-native subagents run under the coordinator runtime's own native agent model,
permissions and sandboxing; they are not placed inside the DeepSeek Team worker sandbox.
This does not change the isolation of DeepSeek workers.

No DeepSeek worker stages, commits, pushes, deploys, publishes, accesses production,
reads coordinator secrets, or delegates to another agent.
