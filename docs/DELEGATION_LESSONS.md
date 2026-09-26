# Delegation lessons and the rework journal

**English** | [Русский](https://github.com/kirill31337/deepseek-team/blob/main/docs/DELEGATION_LESSONS.ru.md)

DeepSeek Team keeps a private, per-project journal of what happened on each delegated assignment, including the assignments that needed no correction at all. The coordinator reviews that journal at a completed-task boundary and can revise a bounded set of task-planning rules from the evidence it actually recorded. This guide describes the journal, the structured correction reason, the review cadence, and the exact JSON the coordinator submits. It stores coordination-quality history only; the rules are advisory planning guidance, not an automatic controller.

## What this is not

- It does not run a model in the background. The application never calls a model and never executes a rule itself.
- It does not rewrite project `AGENTS.md` or any source file from model output. Only the coordinator edits rules, and only through an explicit review submission.
- It does not change routing weights in this feature. The journal and its rules are separate from the estimator and router, and this feature neither reads nor writes routing scores, access, forced `effort`, activation, or sandbox policy.
- The operating system cannot detect semantic rework by itself. A disposition is the coordinator's judgement after reviewing the actual diff and the declared checks; the tooling stores that judgement, it does not infer it.
- Before/after counts never prove that a rule caused an improvement. The reviewer compares repetition and comparable `kind`/`effort` cohorts, including the clean successes, but the journal is descriptive evidence, not a controlled experiment.

## Private local state

State is scoped to one resolved project path and stays on the local machine:

```text
~/.local/state/codex-deepseek/lessons/<sha256(resolved project path)[:24]>/
```

- Outcomes and reviews are stored in a SQLite **append-only** history. Recording the same event again with identical content is idempotent rather than duplicated.
- The Markdown rules file is generated from the canonical versioned ruleset, so generated Markdown can always be recovered from canonical rules.
- A read-only query against an empty state creates no files. Concurrent events are preserved by lock and atomic handling, and symlink files are rejected.
- Reuse the existing private evidence references. Never copy credentials or raw model logs into the journal, into project files, or into Git.

Use `--path PROJECT` on every `lessons` command so the journal is resolved for the intended project.

## Recording an outcome

Record each verified result through the coordination lifecycle, exactly as for routing feedback:

```bash
deepseek-team coordination use --path "$PROJECT" \
  --task TASK_ID --assignment ASSIGNMENT_ID \
  --disposition incorporated --evidence "Declared checks and review passed"
```

When a reviewed result needed a correction, attach a structured reason with `--rework-json`. The flag extends the existing `--disposition` and `--evidence`; it does not replace them, and the outer `--evidence` stays the verifiable evidence for the disposition.

```bash
deepseek-team coordination use --path "$PROJECT" \
  --task TASK_ID --assignment ASSIGNMENT_ID \
  --disposition needs-rework --evidence "Diff shows the second case is unhandled; tests still pass" \
  --rework-json /tmp/rework.json
```

`/tmp/rework.json` must be a JSON object with exactly these four bounded, non-empty string fields:

```json
{
  "cause": "brief_gap",
  "severity": "minor",
  "summary": "The worker handled the first input shape but left the second unhandled.",
  "prevention": "State both accepted input shapes and their expected results in the assignment brief."
}
```

`cause` is one of:

| Cause | Meaning |
| --- | --- |
| `worker_error` | The worker made a mistake the brief did not invite |
| `brief_gap` | The assignment brief was missing or ambiguous |
| `context_gap` | Needed context was unavailable to the worker |
| `requirements_changed` | The requirement moved after the assignment started |
| `integration` | The defect appeared when combining accepted work |
| `environment` | Tooling, sandbox, or runtime environment caused it |
| `mixed` | Several causes genuinely apply together |
| `unknown` | Not yet attributed — an explicit, valid finding |

`severity` is `minor`, `major`, `redo`, or `unknown`. `unknown` is a first-class value in both fields: prefer it to guessing. Unknown causes never count toward the same-cause review threshold, and unknown severity never becomes a quality label on its own.

Dispositions remain `incorporated`, `reproduced`, `needs-rework`, and `rejected`. Multiple dispositions and repeated attempts for one assignment are one case: a later clean incorporation does not erase an earlier `needs-rework`, and a rejection stays separately visible. If a tool supplies `--disposition needs-rework` or `--disposition rejected` without a compatible structured reason, the recorded cause and severity fall back to `unknown`. Older outcomes that were recorded before structured attribution stay `unknown`; they are not backfilled. A clean success needs no `--rework-json` at all, and clean cases form the denominator that keeps the review honest.

`--rework-json -` or `--quality-json -` reads that object from standard input; when both flags are
used, at most one of them may read from stdin.

### Graded quality

New independent successful assignments add positive evidence and raise the comparable class rating. A series of successes can lift a learned-quality veto; older success and failure evidence both lose weight over time. Repeated feedback on one assignment does not create new successes. A separately routed corrective call is assessed independently while preserving the original call’s history.

Operational rework and worker quality are separate judgements. `--rework-json` explains what needed
correcting and why it happened; `--quality-json` records how well the worker did against the
requirements it was given. Supply both on the same `coordination use` call when both apply:

```bash
deepseek-team coordination use --path "$PROJECT" \
  --task TASK_ID --assignment ASSIGNMENT_ID \
  --disposition needs-rework --evidence "The accepted input shape is still unhandled" \
  --rework-json /tmp/rework.json --quality-json /tmp/quality.json
```

`/tmp/quality.json` is exactly `{grade, attribution, evidence}` with bounded, non-empty evidence of
at most 4000 characters:

| Grade | Weight | Meaning |
| --- | --- | --- |
| `met` | 1.0 | every supplied requirement satisfied; cosmetic or preference edits keep full credit |
| `minor_gaps` | 0.8 | one bounded supplied requirement actually missed, with a usable main result |
| `major_gaps` | 0.3 | a substantial supplied requirement failed |
| `unusable` | 0.0 | the main requested result failed |
| `unassessable` | neutral | no supported quality judgement |

Attribution is `worker`, `shared`, `coordinator`, `environment`, or `unknown`. Only `worker` or
`shared` with an assessable grade supplies a quality score; shared evidence must name the
worker-attributable gap against the supplied requirements, without guessing a numerical
responsibility share. Infrastructure, cancelled or unknown outcomes stay neutral regardless of the
assessment. The coordinator supplies the assessment and its evidence; workers never self-grade.

The weights are declared rubric values, not measured probabilities or work-share percentages. A
missing brief or unavailable context, or requirements that changed during the assignment, is neutral
for worker quality on its own. A low editing cost never buys `met` for a serious defect: a one-line
correctness or security defect can still be a serious finding, while a cosmetic edit can keep full
credit.

Omitted quality keeps the legacy binary reading (`accepted` scores 1, `rework` 0, `rejected` 0) for
compatibility and is identified as legacy in statistics. Older binary cases can be reassessed by
appending an explicit assessment, never by rewriting recorded history. One assignment stays one
case: if several explicit assessments exist, the lowest scored worker assessment is retained
(earliest tie), so a later incorporation or a neutral attribution cannot launder a known worker
defect; if explicit assessments exist but none is scored, the case is neutral; and an ordinary
incorporation without quality never erases an assessment.

## Returning a correction to DeepSeek

When review finds that a result needs corrections, record the defect evidence and the original `needs-rework` disposition first. A minor, tightly coupled correction may be performed by the coordinator when its concrete reason is recorded in the rework `summary`/`prevention` or the outer `--evidence`; substantive corrections normally return to DeepSeek as a bounded correction brief. That brief links the original assignment, states the concrete defect and its evidence, supplies the context the first attempt lacked, and carries a reproducer or executable acceptance checks. Route it as its own deliverable, and only then verify the actual delta and integrate it. Correction size and worker quality are separate judgements: a first failed attempt never mandates a takeover, and a one-line correction can still address a serious defect. Architecture, security, integration and final verification remain coordinator-owned.

The runner forbids blind automatic retries, and a completed assignment cannot be resumed directly: a succeeded assignment needs a new planned correction linked to the original. A failed, inspected workspace may explicitly resume with `--resume-after-failure`. No route may bypass `off`, skip the worker sandbox or widen access automatically.

A separately routed correction is another reviewed call, not a second disposition on the same one. Record it with its own `coordination use` and link the original assignment in the evidence; the original `needs-rework` stays recorded and is never laundered into a clean first pass. Nothing here requires an endless loop or an artificial fixed quota of attempts.

## The review bundle

The read commands print human-readable output by default and structured JSON with `--json`:

```bash
deepseek-team lessons status --path "$PROJECT" --json   # current version, due state, counts, cohorts
deepseek-team lessons journal --path "$PROJECT" --json  # chronological events by stable sequence
deepseek-team lessons review --path "$PROJECT" --json   # the review bundle, ready to act on
deepseek-team lessons rules --path "$PROJECT" --json    # current rules and their generated Markdown
```

The review bundle in `lessons review --json` carries the status fields (including `version`, `through_event`, `review_due`, `due_reasons`, `cases_since_review`, `counts`, `cohorts`, `causes`, and `severities`) plus:

- `expected_version` — the version the payload must name to be accepted;
- `through_event` — the latest sequence currently recorded; a review acknowledges up to this watermark;
- `reviewed_through` — the sequence already acknowledged by the last review;
- `cases` — events consolidated per case, including original context, evidence, and the structured correction when present;
- `rules` — the current active rules with their supporting case IDs.

Take `expected_version` and the watermark from this bundle instead of inventing them. A `through_event` that is not a valid current sequence, or a case ID that does not exist at or before the watermark, makes the submission invalid.

The bundle includes enough past evidence to justify every existing active rule, and on the first release it includes all recorded cases.

A case's context records the original deliverable, its `features` card, acceptance criteria and declared checks, the actual runtime and `effort`, the workspace/source references and prepared/worker change references, the declared check results, the explicit quality assessment when one was supplied, and the version/IDs/content of the applicable lessons captured **before** execution. A snapshot references source `HEAD`, prepared changes, checks, and a prompt digest or reference when available. If an older case never captured a field, that field stays explicitly `unknown` instead of being invented.

## When a review is due

A review becomes due when either threshold is met since the last review:

- **10 distinct newly reviewed or updated cases** — counting cases, always including clean successes, not raw events; or
- **3 distinct rework cases with the same task `kind` and the same non-`unknown` cause.**

Repeated events for a single assignment cannot meet either threshold; a case must be distinct, and same-kind/same-cause reworks must be distinct cases. The coordinator receives a review reminder at a completed-task boundary and again on subsequent session, prompt, or policy resolution. That reminder never blocks a user stop, `off`, or plan mode, and it does not create a repeated-Stop veto. Nothing runs when the coordinator is idle: there is no background model call, no automatic rewrite, and no automatic retry.

## Applying a review

Submit the review payload with `lessons review --apply`:

```bash
deepseek-team lessons review --path "$PROJECT" --apply /tmp/lessons-review.json
```

The payload must be exactly:

```json
{
  "expected_version": 3,
  "through_event": 17,
  "summary": "Replace rule r2 and keep r1; the brief_gap repeats only in high-effort documentation cases.",
  "rules": [
    {
      "id": "r1",
      "when": { "kind": "documentation" },
      "condition": "A documentation assignment has more than one target file.",
      "action": "List every target file and its expected heading in the brief.",
      "evidence": ["TASK-12/impl"]
    }
  ]
}
```

- `expected_version` must equal the bundle's `expected_version`. This is an optimistic version check: if another review landed first, the submission is rejected rather than overwriting it. New outcome events after the supplied watermark remain pending for the next review.
- `through_event` must be a valid snapshot sequence from the bundle. It cannot go backwards and cannot acknowledge concurrent newer events; the review only acknowledges through that sequence.
- `summary` is a bounded, non-empty explanation of what changed and why, including for a no-change review.
- `rules` is the complete replacement ruleset. Each rule has exactly `id`, `when`, `condition`, `action`, and `evidence`. A rule that is absent from the payload is removed.

Each `when` is a match object with at most the scalar keys `kind`, `domain`, `operation`, `runtime`, and `effort`; no extra keys are allowed. `effort` normalizes the legacy `medium` to `high`. Rules match on the feature card of the task being planned, so a supplied feature that does not contain a constrained field cannot match that rule. `evidence` is a non-empty list of known case IDs that exist at or before `through_event`.

Rules are bounded: at most **20 active rules**, with bounded text in every field. Everything is validated before anything is written. Each accepted review creates a new immutable version snapshot; older versions and their reviews remain in history, so the context a past assignment ran under is preserved. Version increases even for an explicit no-change review: to justify no change, resubmit the current ruleset unchanged (or an empty `rules` list to drop all rules) with a `summary` explaining why the evidence did not warrant a change.

After a review the generated Markdown rules file is rewritten from the canonical ruleset with evidence references.

## Rules are advisory and subordinate

Matching rules are applied as planning guidance before subsequent delegation and retained with their exact version on the assignment. They are subordinate to user instructions, access settings, forced `effort`, and sandbox requirements: a rule can never widen access, change a saved profile, override a forced effort level, or relax isolation. This feature does not feed rule outcomes back into routing weights.

## Worked example

After inspecting the actual cases, if the evidence does not justify changing the rules, submit a no-change review using the bundle you inspected. New or revised rules must cite cases that substantiate their condition and action; merely choosing an existing case ID is insufficient.

```bash
PROJECT=/path/to/repository
deepseek-team lessons review --path "$PROJECT" --json > /tmp/lessons-bundle.json

python3 - <<'PYTHON'
import json

bundle = json.load(open("/tmp/lessons-bundle.json"))
payload = {
    "expected_version": bundle["expected_version"],
    "through_event": bundle["through_event"],
    "summary": 'Reviewed the cases; the evidence does not justify changing the current rules.',
    "rules": bundle["rules"],
}
json.dump(payload, open("/tmp/lessons-review.json", "w"), indent=2)
PYTHON

deepseek-team lessons review --path "$PROJECT" --apply /tmp/lessons-review.json
```

## Limits to keep in mind

- Rules are planning guidance, not enforcement; hooks and the worker sandbox remain the actual guardrails.
- Counts and cohort tables are descriptive. A clean cohort after a rule change does not establish causation, and no percentage of improvement is claimed.
- The journal is per project and local; it is not shared, published, or used to fine-tune a model.
- `unknown` attribution is expected for older or genuinely ambiguous cases and is not itself a failure.
