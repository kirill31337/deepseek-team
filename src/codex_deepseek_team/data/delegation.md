## DeepSeek delegation

Always resolve the CURRENT policy for a new assignment:

```bash
deepseek-team config show --effective --instructions --runtime {runtime}
```

Percentages are target work-distribution profiles, not measured contribution. Never
derive an "actual percentage" from calls, tasks, files, lines, tokens, time, or a
subjective list of bullets. Access remains independent of the target profile:
explicit read-only always remains read-only.

Keep the required Linux OS sandbox enabled for managed workers. Do not add
--os-sandbox off to ordinary coordination flows and do not weaken Ubuntu AppArmor
user-namespace restrictions to make delegation pass.

### Process

For a substantial task, identify concrete deliverables before duplicating their
implementation. Each deliverable needs: id, kind, concrete scope, executor,
acceptance criteria, dependencies, and checks. Architecture, security decisions,
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
deepseek-team worker --runtime {runtime} \
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

The legacy `--write --allow-write FILE` exact-file mode remains separate and cannot
be combined with coordinated managed-access flags.

### Integration boundary

Codex: distribution/source-mutation gates use supported lifecycle hooks, but native
hook trust is owned by Codex and must be reviewed there once; DeepSeek Team does not
bypass or infer it. Hooks are a coordinator-process guardrail, not a replacement for
the worker Bubblewrap/AppArmor security boundary.

Claude Code: managed instructions and worker accounting are available, but coordinator
distribution enforcement is instruction-driven in this release; no Claude
PreToolUse technical block is claimed.

No worker stages, commits, pushes, deploys, publishes, accesses production, or reads
coordinator secrets.
