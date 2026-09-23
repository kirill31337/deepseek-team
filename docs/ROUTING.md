# Hybrid delegation routing

DeepSeek Team can combine a bounded prior from imported public results with outcomes observed in the current project. It stores routing state privately for each project and returns conservative, explainable recommendations. It does not fine-tune a model, promise a benchmark score, or treat a public leaderboard percentage as the probability of success on your code.

Every command takes `--path PROJECT`. Commands return JSON so results can be inspected or passed to other tools. `status` reports the current state; `config --json` prints the effective routing configuration.

```bash
deepseek-team routing status --path /path/to/repository --json
deepseek-team routing config --path /path/to/repository --json
```

## Modes and executor selection

Fresh installations default to automatic delegation level selection (`delegation_level = "auto"`) and routing mode `auto`. A cold start remains conservative: without enough comparable local evidence the ordinary recommendation retains the coordinator. The recovery mechanism below admits a bounded number of verifiable low-risk tasks so new evidence can still arrive. Public imports provide only a weak, capped prior and never start paid trials automatically.

An existing saved numeric delegation preference is not silently overwritten. The manual `25`, `50`, and `75` profiles remain available and their real worker outcomes continue to supply local evidence:

```bash
deepseek-team config set --project --path /path/to/repository --delegation-level 50
```

Routing modes are:

- `off` disables routing decisions.
- `shadow` records what the policy would recommend without changing executor selection.
- `advisory` returns a recommendation for the coordinator to review.
- `auto` may resolve an explicitly requested `executor: auto`. Existing plans with an explicit worker or coordinator keep their executor. If the policy abstains, the coordinator retains the task and the reason is recorded.

Automatic routing cannot widen the configured access level. Architecture, security, integration, final verification, commit/push, production, and secret-signing work remains coordinator-owned. Unknown or high risk, weak verification, too little local support, or insufficient cost evidence causes a conservative coordinator recommendation or abstention.

Configure only the values you want to change:

```bash
deepseek-team routing configure --path /path/to/repository \
  --mode advisory \
  --confidence 0.95 \
  --min-success-probability 0.80 \
  --min-local-evidence 5 \
  --external-weight-cap 5 \
  --source-weight-cap 2 \
  --half-life-days 90 \
  --max-evidence-age-days 365 \
  --min-similarity 0.60 \
  --minimum-savings-fraction 0.10 \
  --recovery-rate 0.10 \
  --recovery-cooldown-seconds 3600 \
  --require-cost-evidence
```

Use `--no-require-cost-evidence` only when you intentionally want recommendations without measured cost comparisons. Confidence bounds and minimum evidence rules still apply.

## Safe recovery from routing uncertainty

When both the delegation profile and routing mode are `auto`, the policy can assign a small number of ordinary, eligible tasks to the worker while local evidence is still uncertain. Recovery is limited to fully classified tasks that are low risk, small, locally coupled, have known localization, use `tests` or a `reproducer`, declare concrete checks, and fit the configured access. The first eligible case is a canary. After that, `recovery_rate` allows at most one recovery assignment per `ceil(1 / recovery_rate)` distinct eligible uncertain cases across the project. Its default is `0.10`, its range is `0` through `0.25`, and zero disables recovery.

Only one recovery ticket may be pending or running at a time. An unused pending ticket expires after one hour. A quality failure starts the finite `recovery_cooldown_seconds` for the same kind, domain, operation, and execution context; the default is one hour and the allowed range is `0` through `2592000` seconds. This pending-plan expiry is not a worker runtime timeout. A running assignment must be inspected and dispositioned through the normal coordination flow.

```bash
deepseek-team routing recovery status --path /path/to/repository
# Release only an unused pending ticket:
deepseek-team routing recovery release RECOVERY_ID --path /path/to/repository
```

If the coordinator process is lost after an assignment starts, inspect the retained worker diff and workspace first. Find and stop any remaining worker OS processes, then verify that they are stopped. Only then explicitly abandon the orphaned running assignment with concrete evidence:

```bash
deepseek-team coordination abandon \
  --path /path/to/repository \
  --task TASK_ID \
  --assignment ASSIGNMENT_ID \
  --evidence "Inspected retained diff; stopped and verified the worker process group" \
  --confirmed-stopped
```

`--confirmed-stopped` is an operator attestation, not a request for DeepSeek Team to kill processes. The command requires a running assignment and also verifies through an exclusive workspace lock that no active owner remains. It refuses a currently owned live workspace. Successful abandonment records `cancelled`, contributes no quality failure, and resolves its recovery ticket. There is no automatic abandonment timeout, retry, or process killing.

Recovery never overrides explicit worker or coordinator choices, the manual `25`, `50`, or `75` profiles, routing mode, access, eligibility, or worker availability. It does not create a universal delegation floor for unsafe, ineligible, unfunded, or disabled workers. Recovery assignments are normal project tasks with the same review and outcome requirements as other delegated work. They are not extra paid benchmark reruns and do not consume the experiment-budget ledger.

## Runnable synthetic example

The following snapshot is deliberately synthetic. It demonstrates the import format and must not be presented as a real benchmark or a claim about either executor.

```bash
PROJECT=/path/to/repository
NOW=$(date +%s)
cat > /tmp/deepseek-routing-example.json <<JSON
{
  "schema_version": 1,
  "format": "deepseek-team-evidence",
  "source_family": "synthetic-documentation-example",
  "observations": [
    {
      "id": "synthetic-public-observation-1",
      "case_id": "synthetic-public-case-1",
      "source_family": "synthetic-documentation-example",
      "features": {
        "kind": "implementation",
        "domain": "python",
        "operation": "extend",
        "localization": "known",
        "coupling": "local",
        "verification": "tests",
        "clarity": "clear",
        "risk": "low",
        "scope_size": "small",
        "runtime": "codex",
        "model": "deepseek-flash",
        "effort": "medium",
        "context_version": "example-v1"
      },
      "action": "worker",
      "outcome": "accepted",
      "observed_at": $NOW,
      "cost_usd": 0.04,
      "duration_seconds": 42
    }
  ]
}
JSON
SHA=$(sha256sum /tmp/deepseek-routing-example.json | cut -d' ' -f1)
deepseek-team routing import /tmp/deepseek-routing-example.json \
  --path "$PROJECT" \
  --source-id synthetic-doc-v1 \
  --source-url https://example.org/deepseek-routing-example.json \
  --sha256 "$SHA" \
  --reliability 0.2
```

Imported public evidence remains external evidence. Reliability, similarity, freshness, source-family caps, and a total external cap bound its influence; imported volume cannot silently become project experience.

Ask for a recommendation with a feature card. The model, runtime, effort, and context version identify the candidate worker conditions and prevent evidence from silently crossing execution identities.

```bash
cat > /tmp/feature-card.json <<'JSON'
{
  "kind": "implementation",
  "domain": "python",
  "operation": "extend",
  "localization": "known",
  "coupling": "local",
  "verification": "tests",
  "clarity": "clear",
  "risk": "low",
  "scope_size": "small",
  "runtime": "codex",
  "model": "deepseek-flash",
  "effort": "medium",
  "context_version": "example-v1"
}
JSON
deepseek-team routing recommend --path "$PROJECT" --file /tmp/feature-card.json --access read-only
```

A single synthetic external result is intentionally inadequate evidence for confident automatic routing. The response includes the action, reason codes, posterior uncertainty, and available economics. New models or context versions begin uncertain.

After a real project assignment reaches its first meaningful outcome, record what actually happened. `accepted` means final acceptance without rework; `rework` and `rejected` are failures. `infrastructure`, `cancelled`, and `unknown` remain unlabelled. `cost_usd` is total measured cost, including worker execution plus review and rework. Never invent the unchosen executor's result or cost.

```bash
NOW=$(date +%s)
cat > /tmp/local-observation.json <<JSON
{
  "id": "local-observation-1",
  "case_id": "project-case-1",
  "origin": "local",
  "features": {
    "kind": "implementation",
    "domain": "python",
    "operation": "extend",
    "localization": "known",
    "coupling": "local",
    "verification": "tests",
    "clarity": "clear",
    "risk": "low",
    "scope_size": "small",
    "runtime": "codex",
    "model": "deepseek-flash",
    "effort": "medium",
    "context_version": "example-v1"
  },
  "action": "worker",
  "outcome": "accepted",
  "observed_at": $NOW,
  "cost_usd": 0.06,
  "duration_seconds": 75
}
JSON
deepseek-team routing observe --path "$PROJECT" --file /tmp/local-observation.json
```

Normal coordinated work should record its verified result through the coordination commands. `--cost-usd` is the full measured total, including execution, coordinator review, and any rework:

```bash
deepseek-team coordination use --path "$PROJECT" \
  --task TASK_ID --assignment ASSIGNMENT_ID \
  --disposition incorporated --evidence "Declared checks and review passed" \
  --cost-usd 0.06

deepseek-team coordination result --path "$PROJECT" \
  --task TASK_ID --deliverable DELIVERABLE_ID \
  --outcome accepted --evidence "Declared checks and final review passed" \
  --cost-usd 0.40
```

Coordinator outcomes accept `accepted`, `rework`, `rejected`, `infrastructure`, `cancelled`, or `unknown`. For a coordinator observation, the feature card still describes the candidate worker identity so comparable worker and coordinator observations can be evaluated.

The lower-level `routing observe` command is for trusted operator-reported historical data. Without a valid recorded decision ID it cannot guarantee that the feature card was captured before execution, so do not use it as a substitute for the coordination lifecycle when that lifecycle is available. Corrections for a local case are chronological and immutable: every phase remains in the audit history, and any observed `rework` or `rejected` quality failure dominates an earlier or later `accepted` phase. Repeated attempts at one case do not become independent evidence.

## Import, fetch, evaluation, and export

`import` reads local bytes and never accesses the network. It requires an HTTPS provenance URL and the SHA-256 of the exact bytes. Network access occurs only with the explicit `fetch` command, which also requires a pinned digest and rejects unsafe destinations:

```bash
deepseek-team routing fetch https://data.example.org/run.json \
  --path /path/to/repository \
  --source-id public-run-2026-09 \
  --sha256 0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef \
  --reliability 0.5
```

Evaluate cases chronologically: each prediction is scored before that case's outcome is learned, and repeated case IDs are grouped. A retrospective correction preserves the original forecast; evaluation learns the correction only when its timestamp is reached.

```bash
deepseek-team routing evaluate --path /path/to/repository
deepseek-team routing export --path /path/to/repository --output /tmp/routing-export.json
```

`export` streams the existing JSON export format instead of loading the full project history into memory. Without `--output` it writes that JSON directly to stdout. File output is written privately and replaced atomically, so an interrupted export does not replace an existing valid file. Export is a local read operation: it does not run a worker, contact a model provider, or start a paid action.

Evaluation reports Brier score, log loss, calibration, coverage, and observed policy outcomes when available. These are conditional summaries of recorded outcomes. They do not establish formal correctness or counterfactual savings.

## Experiment budgets

Reservations are explicit, persistent, atomic, and idempotent. They govern the amount authorized for a manually requested extra experiment under per-run and monthly settings. Routing never launches paid trials automatically. The ledger does not apply to normal project jobs, including uncertainty-recovery assignments. Reservations are accounting controls inside DeepSeek Team, not a provider billing limit, and they cannot guarantee a hard dollar cap. A settlement overrun prevents further experiment reservations; no automatic worker retry is performed.

```bash
deepseek-team routing configure --path /path/to/repository \
  --monthly-experiment-budget-usd 10 --per-experiment-limit-usd 2
deepseek-team routing budget reserve trial-2026-09-22 --path /path/to/repository --amount-usd 1.50
deepseek-team routing budget settle trial-2026-09-22 --path /path/to/repository --actual-usd 1.35
# Or release an unused reservation:
deepseek-team routing budget release trial-2026-09-22 --path /path/to/repository
```
