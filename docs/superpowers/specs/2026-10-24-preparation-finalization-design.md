# Preparation failure finalization

The supplied bug report defines the requested behavior: preparation failures must
remain reviewable and cancellable without a provider call or invented execution.
The current runner saves coordination metadata before probes, but reserves the
lease binding only in `assignment_started`. Cancellation therefore deletes the
execution tree before discovering that ledger publication cannot be authorized.
Reconciliation also cannot repair a crash before the first publication exists.

## Approach and responsibility

Keep the durable job lease authoritative and publish its terminal state to the
coordination ledger. Separate assignment association from attempt reservation;
validate publication authority before destructive cleanup, and replay publication
from durable finalization intent. This follows the user's requested approach.
Treating cleanup alone as acceptance loses authorization, and reserving an
execution attempt during preparation falsely records execution; both are excluded.

The coordinator owns these identity/security decisions and final integration.
DeepSeek may implement the bounded transition and regression tests in an isolated
managed copy. Existing user changes, credentials, hooks and sandbox policy remain
outside the change. No commit, publication, real-job migration or provider retry is
part of this fix.

## Shared identity and history

- One logical job retains the same `job_id`, `workspace_id`, source project and
  execution inode/device through preparation, review, cancellation and correction.
- Associate task/assignment durably before saved-tool materialization, runtime
  preparation, probes and dependency checks. If association is threaded into copy
  creation, do it immediately after durable record/lease allocation and before Git
  preparation can fail. Do not add a pending attempt, execution snapshot, start
  timestamp, model execution evidence, routing observation or synthetic attempt.
- Explicitly supplied and linked copies acquire their owner lock and association
  before guard/sandbox checks. New allocations compare the preflighted source HEAD
  with the immutable task HEAD before creating state; refusal remains a reviewable
  preparation failure without an execution copy.
- Record actual binding transitions durably. Explicit correction links may move
  the current binding, while preserving predecessor identity/history and existing
  failed-resume and same-copy guarantees. An early failed correction must also be
  cancellable without falsely borrowing its predecessor's execution attempt.
- Once a logical job is associated, its deliverable identity and definition are
  immutable even while its assignment remains `planned`. Equivalent plans remain
  no-ops; a plan cannot remove or reassign the binding before execution starts.
- Every publication must agree with the lease, metadata, requested assignment,
  authoritative correction chain and known attempt history. Validate the project
  recorded in each task as well as the workspace source. Caller-supplied IDs alone
  grant no authority.

## Finalization and recovery

Use the existing lifecycle fence, owner lock and ledger lock discipline. Validate
private directory/file ownership and lock identity; reject unrelated live owners,
unknown process namespaces, foreign UIDs, busy owner locks and conflicting records,
including already-terminal records. Do not loosen cleanup's inode/mount checks.

Before any quarantine rename or deletion, resolve and validate all information
needed for publication, and persist bounded intent identifying project, workspace,
job, task/assignment, terminal decision and review evidence. If a callback updates
the lease, cleanup must reload it rather than overwrite the new association from
a stale local object. Publication and repeated finalize must be idempotent, keeping
the original terminal decision and producing no new attempts or cleanup effects.
Inspect the actual execution inode, owners and open references before recording
attestation. Recheck the disposition used to choose cancellation while holding the
ledger lock. A saved finalization intent blocks preparation, resume and execution
reservation until it is explicitly resolved.

For old `cancelled`/`cleaned` leases with no binding, the normal explicit
`coordination finalize --cancel` path may recover an association only from
corroborated durable metadata, ledger identity, saved finalization request and
binding/attempt/correction history. The historical no-execution case must prove no
execution attempt or contradictory reservation exists. Check result/cleanup
identity and the absence of a replacement execution tree. Missing, malformed,
ambiguous or conflicting evidence fails closed and preserves all history.
Actual completed `operation: preparation` attempts are permitted only when their
IDs, counter and latest metadata agree. They remain preparation attempts; any
execution history prevents this narrow missing-binding repair.

After cleanup but before ledger publication, the terminal lease and authorized
saved intent are sufficient to resume publication through repeated finalize or
normal completion reconciliation. A terminal/cleaned lease without corroborated
authority is never silently accepted. Keep cleanup and publication histories;
never rewrite an original failure into success.
Stop runs this reconciliation before computing outstanding-job reasons. An owned
workspace reference with missing job identity remains unfinished instead of being
silently omitted from completion checks.

## Acceptance

Provider-free regressions must cover missing command/dependency and failed probe
before model start, reject/cancel/complete and Stop behavior, old null-binding
recovery, repeated finalization, interruption after cleanup, conflicting owner and
locks, project/task/job/workspace/assignment mismatches, binding history conflicts,
and existing correction/resume behavior. Tests isolate state and runtime homes.
Run the repository suite with
`PYTHONPATH=src python3 -m unittest discover -s tests -v`.

## Operator recovery

After installing the corrected package, an inspected preparation failure with an
already recorded rejected disposition uses the existing commands:

```sh
deepseek-team coordination finalize --path /absolute/project \
  --task task-00000000000000000000 --assignment as-0000000000000000 \
  --cancel --evidence "Inspected preparation failure; no model execution occurred"
deepseek-team coordination complete --path /absolute/project \
  --task task-00000000000000000000
```

Replace the example identifiers with the saved task and assignment. The first
command validates retained evidence and publishes cancellation; an authorized
saved cancellation can also be reconciled by completion. Repeating finalization
preserves its original decision. Conflicting evidence remains an explicit refusal
with retained history rather than authorization to reconstruct arbitrary state.
