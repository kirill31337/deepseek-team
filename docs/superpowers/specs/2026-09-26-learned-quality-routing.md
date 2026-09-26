# Learned quality routing and category statistics

The user wants immediate useful delegation for new or poorly sampled task classes, followed by executor choices that respond to accumulated results. The user explicitly defines the learning unit as each coordinator-reviewed DeepSeek worker assignment, not their whole prompt or session. They also requested a sorted CLI table showing which categories DeepSeek handles better or worse.

## Behavior and boundaries

- Keep current eligibility, explicit access, disabled/manual modes, sandbox, short failure cooldown and measured-economics protections.
- Add a local learned-quality veto for Auto. Little evidence or uncertainty never vetoes eligible work.
- Defaults: `quality_min_evidence = 10.0` effective local cases; `quality_min_success_probability = 0.70`. These are configurable policy defaults, not benchmark-derived optimal values. Both effective sample size and the upper end of the configured credible interval must support a poor result: veto only when support is at least the minimum and `posterior.upper < threshold`.
- Use the existing Beta(1,1), configured confidence, evidence aging, similarity, canonical effort and first-failure case semantics. Infrastructure, cancelled, unknown and coordinator outcomes are not DeepSeek quality failures. External data must not create the local learned veto.
- Quality comparison keeps exact runtime, model, effort, kind, domain and operation. It uses the existing six soft-context similarity fields and threshold. Task-specific `context_version` is provenance rather than a barrier for this new quality assessment; otherwise per-task version markers prevent class-level learning. Preserve the existing exact-context forecast and cost comparison unchanged.
- Do not rewrite old observation rows or widen access. New quality estimates are a separate explicit field in decisions so the behavior remains auditable.
- Distinct worker assignments in one coordinator task are distinct cases. Several feedback events or failed/resumed attempts for the same assignment are not independent successes; the first labelled quality failure remains a failure even after incorporation.
- Existing aging and maximum evidence age provide recovery: once effective support drops below the quality minimum, eligible work is admitted again and new results replenish the sample. Model/effort or truly different task classes have separate learning. No additional paid probes, forced retries, quota counters or recovery ticket subsystem in this change.
- Apply the same quality check to new decisions and queued starts. Already-running work is not retroactively cancelled. Only automatic delegation is affected; explicit manual profiles retain priority.

## Interfaces and independent ownership

1. `routing_learning.py`: pure `assess_quality(features, observations, config, *, now=None)` returning a JSON-safe assessment with `posterior`, `sufficient`, `veto`, policy threshold/minimum and comparison scope. Empty local history is insufficient, not bad. The module may reuse tested estimator internals while leaving exact-context estimator behavior intact.
2. `routing_models.py`: validate/store the two new quality settings. Minimum evidence must be finite and positive, bounded consistently with existing numeric limits; threshold finite in [0,1].
3. `RoutingService.predict`: record `quality` assessment and use `learned_quality_below_threshold` after existing eligibility/off/verification/cooldown/economics reasons, only under effective Auto. `validate_start` recomputes current quality for new queued starts with the same protections. Avoid retaining caller-supplied full-access over current read-only policy.
4. `routing_stats.py`: pure local worker category summary using canonical observations and unique quality cases, grouped by `features.kind`. It is descriptive pooled history across task contexts, not the exact contextual admission decision. Keep labelled acceptance/rework/rejection counts distinct from unlabelled cases, and show aging-adjusted effective evidence plus a Beta estimate/interval. Empty/unlabelled-only categories have unavailable score, not a prior advertised as measured success.
5. `routing stats --path PROJECT [--json] [--sort score|cases|category]`: default descending conservative lower credible bound, then effective support, then category for deterministic ties. The table shows category, score, sample/effective support, accepted, rework, rejected and unlabelled counts. Include a short explanation of the score and the case unit; JSON exposes all counts and interval values. This is a read operation on observations: no new routing decision, feedback or experiment record. Existing store initialization/locking conventions may remain.
6. CLI implementation uses existing `RoutingService.observations()` and `config()` and does not edit `routing.py`. The initial implementation scopes remain independent. Integration shares assignment-case identity and first-failure selection between statistics and local quality, so a changed context marker cannot duplicate an assignment in the table while learning counts it once. The historical exact-context forecast and cost identity remain unchanged.

## Verification

Write meaningful failing behavior tests first. Cover small and uncertain samples, sufficiently poor/good histories, context-version pooling, model/runtime/effort/anchor separation, external/coordinator exclusion, infrastructure neutrality, duplicate case updates, acceptance after rework, aging recovery, explicit access/off/manual protection and queued revalidation.

Statistics tests cover unique assignments rather than session totals, mixed/neutral outcomes, stale/future observations, legacy effort normalization, deterministic sorting, no-data behavior, JSON and absence of new decision/feedback records. Historical full-context estimates and cost semantics retain their existing tests.

Coordinator reviews actual worker diffs, integrates only their scoped changes, runs `PYTHONPATH=src python3 -m unittest discover -s tests -v`, exercises table/JSON output and demonstrates changed poor-history routing in temporary isolated state. No keys, private configuration, raw logs, staged files, commits, push or publication are part of the worker scope.
