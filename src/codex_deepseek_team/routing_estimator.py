"""Pure contextual Beta forecasts and chronological evaluation.

Feature cards describe the candidate worker execution conditions. Coordinator
observations carry that same candidate card, allowing like-for-like task costs
without treating the coordinator's quality as the worker's quality. Costs are
recorded totals, including any review and rework; no token-price model is used.
"""
from __future__ import annotations

from collections import defaultdict
import heapq
import itertools
import math
import time

from . import routing_quality
from .routing_models import RoutingError, normalized_features, validate_config, validate_features

_IDENTITY = ("runtime", "model", "effort", "context_version")
_ANCHORS = ("kind", "domain", "operation")
_SOFT_CONTEXT = ("localization", "coupling", "verification", "clarity", "risk", "scope_size")
_CONTEXT = _ANCHORS + _SOFT_CONTEXT
_LABELLED = frozenset(("accepted", "rework", "rejected"))


def _canonical_observations(observations):
    """Treat a stored legacy 'medium' effort as the same level as 'high'."""
    result = []
    for row in observations:
        features = row.get("features")
        canonical = normalized_features(features)
        result.append(row if canonical is features else dict(row, features=canonical))
    return result


def _beta_fraction(a: float, b: float, x: float) -> float:
    """Modified Lentz continued fraction for the incomplete beta function."""
    tiny = 1e-300
    def nonzero(value):
        return value if abs(value) >= tiny else math.copysign(tiny, value)
    qab, qap, qam = a + b, a + 1., a - 1.
    c = 1.
    d = 1. / nonzero(1. - qab * x / qap)
    h = d
    for m in range(1, 2001):
        twice = 2 * m
        aa = m * (b - m) * x / ((qam + twice) * (a + twice))
        d = 1. / nonzero(1. + aa * d)
        c = nonzero(1. + aa / c)
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + twice) * (qap + twice))
        d = 1. / nonzero(1. + aa * d)
        c = nonzero(1. + aa / c)
        change = d * c
        h *= change
        if abs(change - 1.) <= 3e-14:
            return h
    raise RoutingError("Posterior quantile calculation did not converge.")


def _beta_cdf(x: float, a: float, b: float) -> float:
    if x <= 0.:
        return 0.
    if x >= 1.:
        return 1.
    scale = math.exp(math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b)
                     + a * math.log(x) + b * math.log1p(-x))
    if x < (a + 1.) / (a + b + 2.):
        value = scale * _beta_fraction(a, b, x) / a
    else:
        value = 1. - scale * _beta_fraction(b, a, 1. - x) / b
    return max(0., min(1., value))


def _beta_quantile(probability: float, a: float, b: float) -> float:
    # Reflect into the lower tail, avoiding subtraction in the CDF inversion.
    if probability > .5:
        return 1. - _beta_quantile(1. - probability, b, a)
    if a == 1.:
        return -math.expm1(math.log1p(-probability) / b)
    if b == 1.:
        return math.exp(math.log(probability) / a)
    low, high = 0., 1.
    for _ in range(80):
        middle = (low + high) / 2.
        if middle == low or middle == high:
            break
        if _beta_cdf(middle, a, b) < probability:
            low = middle
        else:
            high = middle
    return (low + high) / 2.


def _timestamp(now):
    now = time.time() if now is None else now
    if type(now) not in (int, float) or not math.isfinite(now) or now < 0:
        raise RoutingError("Inference time must be finite and nonnegative.")
    return float(now)


def _case_key(row):
    # source_id is deliberately absent: importing the same case from another
    # mirror/run must never turn repeated attempts into independent evidence.
    # Task-family labels are intentionally absent: retagging a retry cannot
    # create an independent case or erase an earlier failure.
    return (row["origin"], row["case_id"], row["action"],
            *(row["features"][key] for key in _IDENTITY))


def _quality_case_key(row):
    """Graded-quality identity shared with the learned assessment and cooldown.

    Unlike :func:`_case_key` it omits ``context_version``: one reviewed
    assignment stays one quality case across context versions. Rows must carry
    canonical features, which :func:`_canonical_observations` guarantees.
    """
    return routing_quality.quality_case_key(row)


def _case_outcomes(observations, now, *, corrections=True, key=None):
    """Keep the earliest observed failure, or the first acceptance if none.

With corrections=False this selects initial evaluation forecast events only;
future corrections must never move those forecasts to a later timestamp.
``key`` overrides the case identity; the chronological evaluation groups
retrospective targets by graded-quality identity while contextual forecasts
keep the exact-context identity above.
"""
    case_key = _case_key if key is None else key
    selected = {}
    order = lambda r: (r["observed_at"], r["outcome"] == "accepted", r["id"])
    for row in sorted(observations, key=order):
        if row["observed_at"] > now or row["outcome"] not in _LABELLED:
            continue
        key = case_key(row)
        previous = selected.get(key)
        if previous is None or (corrections and previous["outcome"] == "accepted"
                                and row["outcome"] != "accepted"):
            selected[key] = row
    yield from sorted(selected.values(), key=order)


def _cost_outcomes(observations, now):
    # Each recorded cost is a total, not an increment. Later completion totals
    # replace earlier partial totals, independently of immutable quality labels.
    labelled, unlabelled = {}, {}
    for row in sorted(observations, key=lambda r: (r["observed_at"],
                      r.get("cost_usd") if r.get("cost_usd") is not None else -1., r["id"])):
        if row["observed_at"] > now or row["origin"] != "local":
            continue
        key = _case_key(row)
        if row["outcome"] in _LABELLED:
            labelled[key] = row
        elif row.get("cost_usd") is not None:
            unlabelled[key] = row
    yield from labelled.values()
    yield from (row for key, row in unlabelled.items() if key not in labelled)


def _similarity(features, other):
    if any(features[key] != other[key] for key in _IDENTITY):
        return 0.
    # Task kind, language/domain and operation define the comparable family.
    # Even matching unknown anchors cannot transfer evidence to another task.
    if any(features[key] == "unknown" or features[key] != other[key] for key in _ANCHORS):
        return 0.
    matches = sum(features[key] == other[key] and features[key] != "unknown"
                  for key in _SOFT_CONTEXT)
    return matches / len(_SOFT_CONTEXT)


def _graded_cases(observations, now):
    """Group every known event per case for case-level graded quality.

    Context versions are provenance here: a later explicit grade under another
    context version belongs to the same assignment, so it reaches that
    assignment's earlier forecast-context evidence. Evidence selection and
    similarity still require the exact context version below, and cost identity
    is untouched.
    """
    grouped = defaultdict(list)
    for row in observations:
        if row["observed_at"] <= now:
            grouped[_quality_case_key(row)].append(row)
    return grouped


def _weighted(features, observations, config, now, *, for_cost=False):
    result = []
    selected = _cost_outcomes(observations, now) if for_cost else _case_outcomes(observations, now)
    cases = None if for_cost else _graded_cases(observations, now)
    for row in selected:
        age = (now - row["observed_at"]) / 86400.
        if age > config["max_evidence_age_days"]:
            continue
        similarity = _similarity(features, row["features"])
        if similarity <= 0 or similarity < config["min_similarity"]:
            continue
        weight = similarity * row["reliability"] * 2. ** (-age / config["half_life_days"])
        if weight <= 0:
            continue
        if cases is not None:
            # Graded explicit quality replaces this case's binary inference;
            # neutral explicit quality contributes no success or failure mass.
            score = routing_quality.case_score(cases.get(_quality_case_key(row), (row,)), row)
            if score is None:
                continue
            if score != (1.0 if row["outcome"] == "accepted" else 0.0):
                row = dict(row, _score=score)
        result.append((row, weight))
    return _cap_external(result, config)


def _cap_external(weighted, config):
    families = defaultdict(float)
    for row, weight in weighted:
        if row["origin"] == "external":
            families[row["source_family"]] += weight
    family_scale = {family: min(1., config["source_weight_cap"] / total)
                    for family, total in families.items()}
    external_total = math.fsum(total * family_scale[family] for family, total in families.items())
    global_scale = min(1., config["external_weight_cap"] / external_total) if external_total else 0.
    return [(row, weight if row["origin"] == "local" else
             weight * family_scale[row["source_family"]] * global_scale)
            for row, weight in weighted]


def _mass_score(row):
    """Fractional success mass for one case; legacy rows keep the binary rule."""
    score = row.get("_score")
    return (1.0 if row["outcome"] == "accepted" else 0.0) if score is None else score


def _posterior(weighted, config, *, intervals=True, evidence_ids=True):
    rows = [(row, weight) for row, weight in weighted if row["action"] == "worker"
            and row["outcome"] in _LABELLED and weight > 0]
    alpha = 1. + math.fsum(weight * _mass_score(row) for row, weight in rows)
    beta = 1. + math.fsum(weight * (1. - _mass_score(row)) for row, weight in rows)
    tail = (1. - config["confidence"]) / 2.
    return {"mean": alpha / (alpha + beta),
            "lower": _beta_quantile(tail, alpha, beta) if intervals else None,
            "upper": 1. - _beta_quantile(tail, beta, alpha) if intervals else None,
            "alpha": alpha, "beta": beta,
            "local_effective": math.fsum(w for r, w in rows if r["origin"] == "local"),
            "local_failure_effective": math.fsum(w * (1. - _mass_score(r)) for r, w in rows
                if r["origin"] == "local"),
            "external_effective": math.fsum(w for r, w in rows if r["origin"] == "external"),
            "matched_local": sum(r.get("_count", 1) for r, _ in rows if r["origin"] == "local"),
            "matched_external": sum(r.get("_count", 1) for r, _ in rows if r["origin"] == "external"),
            "evidence_ids": [r["id"] for r, _ in rows] if evidence_ids else []}


def estimate(features, observations, config, *, now=None) -> dict:
    """Estimate acceptance without rework under the recorded candidate context."""
    features, config, now = validate_features(features), validate_config(config), _timestamp(now)
    return _posterior(_weighted(features, _canonical_observations(observations), config, now), config)


def _economics(weighted):
    result = {}
    for action in ("worker", "coordinator"):
        rows = [(row, weight) for row, weight in weighted
                if row["origin"] == "local" and row["action"] == action
                and row.get("cost_usd") is not None and weight > 0]
        total = math.fsum(w for _, w in rows)
        # Normalize first to avoid overflowing a large accumulated total cost.
        result[action + "_mean_cost_usd"] = (math.fsum(row["cost_usd"] * (w / total)
                                                       for row, w in rows) if total else None)
        result[action + "_cost_effective"] = total
        result[action + "_cost_cases"] = sum(r.get("_count", 1) for r, _ in rows)
    worker, coordinator = result["worker_mean_cost_usd"], result["coordinator_mean_cost_usd"]
    result["expected_total_cost_usd"] = worker
    result["expected_savings_usd"] = None if worker is None or coordinator is None else coordinator - worker
    result["savings_fraction"] = (None if coordinator is None or worker is None or coordinator <= 0
                                  else 1. - worker / coordinator)
    if result["savings_fraction"] is not None and not math.isfinite(result["savings_fraction"]):
        result["savings_fraction"] = None
    result["basis"] = "observed_local_total_costs"
    return result


def forecast(features, observations, config, *, now=None) -> dict:
    """Forecast worker quality and measured costs without choosing an executor."""
    features, config, now = validate_features(features), validate_config(config), _timestamp(now)
    observations = _canonical_observations(observations)
    weighted = _weighted(features, observations, config, now)
    posterior = _posterior(weighted, config)
    economics = _economics(_weighted(features, observations, config, now, for_cost=True))
    return {"posterior": posterior, "economics": economics}


class _RunningSum:
    """Compensated add/remove sums, including small values beside large costs."""
    __slots__ = ("value", "correction")

    def __init__(self):
        self.value = self.correction = 0.

    def add(self, amount):
        updated = self.value + amount
        if abs(self.value) >= abs(amount):
            self.correction += (self.value - updated) + amount
        else:
            self.correction += (amount - updated) + self.value
        self.value = updated

    def total(self, subtract=()):
        return math.fsum((self.value, self.correction, *(-value for value in subtract)))


class _EvidenceBucket:
    """Statistics at one fixed decay anchor, avoiding cancellation on expiry.

An anchor spans at most 256 half-lives. Additions and removals use exactly the
same normalized coefficient; decay is applied only when reading the aggregate.
"""
    __slots__ = ("row", "identity", "key", "anchor", "weight", "cost", "count", "half_life")

    def __init__(self, row, identity, key, half_life):
        self.row, self.identity, self.key = row, identity, key
        self.anchor, self.half_life = row["observed_at"], half_life
        self.weight, self.cost, self.count = _RunningSum(), _RunningSum(), 0

    def contribution(self, row):
        elapsed = ((row["observed_at"] - self.anchor) / 86400.) / self.half_life
        return row["reliability"] * 2. ** elapsed

    def update(self, row, sign):
        weight = self.contribution(row)
        self.weight.add(sign * weight)
        self.cost.add(sign * weight * (row.get("cost_usd") or 0.))
        self.count += sign

    def snapshot(self, now, exclude):
        count = self.count - len(exclude)
        if not count:
            return None
        excluded_weights = [self.contribution(row) for row in exclude]
        weight = self.weight.total(excluded_weights)
        if weight <= 0:
            return None
        cost = self.cost.total(w * (row.get("cost_usd") or 0.)
                               for row, w in zip(exclude, excluded_weights))
        row = dict(self.row, _count=count)
        if self.key[0] == "cost":
            row["cost_usd"] = max(0., cost) / weight
        # Splitting the exponential avoids underflowing the decay factor before
        # multiplying a large normalized sum by it.
        elapsed = ((now - self.anchor) / 86400.) / self.half_life
        half_decay = 2. ** (-elapsed / 2.)
        weight = (weight * half_decay) * half_decay
        return (row, weight) if weight > 0 else None


class _ChronologicalEvidence:
    """Incremental failure-dominant quality and latest-total indexes with expiry.

Memory is O(N + B). Updating/expiring each representative takes O(log N)
heap work; querying visits B context/provenance buckets for the exact execution
identity and task family, rather than all preceding cases. No outcomes are preloaded.
"""
    def __init__(self, config):
        self.config = config
        self.buckets = defaultdict(dict)
        self.latest_bucket = {}
        self.quality_latest = {}
        self.cost_latest = {}
        self.active = {}
        self.by_case = defaultdict(set)
        self.expiry = []
        self.serial = 0

    def _add(self, row, kind, now):
        if row["reliability"] <= 0 or (kind == "cost" and row.get("cost_usd") is None):
            return None
        identity = tuple(row["features"][key] for key in _IDENTITY + _ANCHORS)
        context = tuple(row["features"][key] for key in _CONTEXT)
        family = row["source_family"] if row["origin"] == "external" else ""
        # Quality buckets stay homogeneous in their success mass so an
        # aggregated bucket carries one exact fractional score.
        key = (kind, context, row["origin"], family, row["action"],
               row["outcome"] if kind == "quality" else "",
               row.get("_score") if kind == "quality" else None)
        latest_key = (identity, key)
        bucket = self.latest_bucket.get(latest_key)
        if bucket is None or ((now - bucket.anchor) / 86400.) / bucket.half_life > 256.:
            key = (*key, now, self.serial)
            bucket = _EvidenceBucket(row, identity, key, self.config["half_life_days"])
            self.buckets[identity][key] = bucket
            self.latest_bucket[latest_key] = bucket
        bucket.update(row, 1)
        self.serial += 1
        entry = self.serial
        self.active[entry] = row, bucket
        self.by_case[row["case_id"]].add(entry)
        heapq.heappush(self.expiry, (row["observed_at"], entry))
        return entry

    def _remove(self, entry, now):
        record = self.active.pop(entry, None)
        if record is None:
            return
        row, bucket = record
        bucket.update(row, -1)
        entries = self.by_case[row["case_id"]]
        entries.remove(entry)
        if not entries:
            del self.by_case[row["case_id"]]
        if bucket.count == 0:
            latest_key = (bucket.identity, bucket.key[:-2])
            if self.latest_bucket.get(latest_key) is bucket:
                del self.latest_bucket[latest_key]
            del self.buckets[bucket.identity][bucket.key]
            if not self.buckets[bucket.identity]:
                del self.buckets[bucket.identity]

    def advance(self, now):
        while self.expiry:
            age = (now - self.expiry[0][0]) / 86400.
            if (age <= self.config["max_evidence_age_days"] and
                    age / self.config["half_life_days"] < 1075.):
                break
            # At 1075 half-lives every individual double-precision evidence
            # weight is exactly zero, independently of the configured age cap.
            _, entry = heapq.heappop(self.expiry)
            self._remove(entry, now)

    def _rescore_quality(self, state, case_state, now):
        """Apply the case-level graded score to one exact-context evidence entry.

        The representative row keeps its own context version, so the entry still
        matches only that context in a forecast, while the score comes from the
        assignment's graded history across context versions.
        """
        if case_state["assessed"] is not None:
            score = case_state["assessed"][0]
        elif case_state["explicit"]:
            score = None
        else:
            score = 1.0 if state["representative"]["outcome"] == "accepted" else 0.0
        graded = (str(state["representative"]["id"]), score)
        if graded == state["graded"]:
            return
        if state["entry"] is not None:
            self._remove(state["entry"], now)
            state["entry"] = None
        state["graded"] = graded
        if score is not None:
            state["entry"] = self._add(
                dict(state["representative"], _score=score), "quality", now)

    def learn(self, batch, now):
        # The caller's timestamp batch is sorted with tied failures first.
        for row in batch:
            if row["outcome"] not in _LABELLED:
                continue
            # Graded quality is case-level across context versions, while the
            # retained representative keeps the exact context identity per
            # version so contextual matching stays unchanged.
            case_state = self.quality_latest.get(_quality_case_key(row))
            if case_state is None:
                case_state = {"explicit": False, "assessed": None, "contexts": {}}
                self.quality_latest[_quality_case_key(row)] = case_state
            changed = False
            assessment = row.get("quality")
            if isinstance(assessment, dict):
                if not case_state["explicit"]:
                    case_state["explicit"] = True
                    changed = True
                score = routing_quality.assessment_score(assessment)
                if score is not None:
                    candidate = (score, row.get("observed_at", 0.), str(row.get("id", "")))
                    if case_state["assessed"] is None or candidate < case_state["assessed"]:
                        case_state["assessed"] = candidate
                        changed = True
            state = case_state["contexts"].get(_case_key(row))
            if state is None:
                state = {"failed": False, "entry": None, "representative": None, "graded": None}
                case_state["contexts"][_case_key(row)] = state
            failed = row["outcome"] != "accepted"
            # Failure-dominant representative selection, preserved per context.
            if state["representative"] is None or (failed and not state["failed"]):
                state["representative"], state["failed"] = row, failed
            if changed:
                # A new or lower explicit grade applies to every context version
                # of the assignment, but only from this batch onward.
                for item in case_state["contexts"].values():
                    self._rescore_quality(item, case_state, now)
            else:
                self._rescore_quality(state, case_state, now)
        # Costs have a separate tie rule: latest timestamp, then greatest total,
        # then ID. A labelled total always takes priority over partial spending.
        for row in sorted(batch, key=lambda r: (r.get("cost_usd") if r.get("cost_usd") is not None else -1., r["id"])):
            if row["origin"] != "local":
                continue
            key = _case_key(row)
            labelled = row["outcome"] in _LABELLED
            previous = self.cost_latest.get(key)
            if not labelled and (row.get("cost_usd") is None or (previous and previous[0])):
                continue
            if previous:
                self._remove(previous[1], now)
            self.cost_latest[key] = labelled, self._add(row, "cost", now)

    def forecast(self, features, excluded_case, now):
        identity = tuple(features[key] for key in _IDENTITY + _ANCHORS)
        exclusions = defaultdict(list)
        for entry in self.by_case.get(excluded_case, ()):
            row, bucket = self.active[entry]
            if bucket.identity == identity:
                exclusions[bucket].append(row)
        quality, costs, similarities = [], [], {}
        for bucket in self.buckets.get(identity, {}).values():
            context = bucket.key[1]
            if context not in similarities:
                similarities[context] = _similarity(features, bucket.row["features"])
            similarity = similarities[context]
            if similarity <= 0 or similarity < self.config["min_similarity"]:
                continue
            summary = bucket.snapshot(now, exclusions.get(bucket, ()))
            if summary is None:
                continue
            row, weight = summary
            weight *= similarity
            if weight <= 0:
                continue
            target = quality if bucket.key[0] == "quality" else costs
            target.append((row, weight))
        posterior = _posterior(_cap_external(quality, self.config), self.config,
                               intervals=False, evidence_ids=False)
        economics = _economics(costs)
        return {"posterior": posterior, "economics": economics}


def evaluate(observations, config, *, now=None) -> dict:
    """Chronologically score first local case outcomes using only earlier cases.

Repeated cases (across executors, sources and context versions) form one group.
No member can teach the forecast for another member of its own group. Rows at
the same timestamp are scored together before learning any of their outcomes.
Only worker outcomes score worker forecasts. Outcome and cost summaries report
the executor actually observed, without simulating a routing policy.

Forecasts retain their original timestamp/probability. Their reported targets
and costs are corrected retrospectively using events known by the evaluation
cutoff. Training incorporates each correction only at its observed timestamp,
so a later failure cannot influence an earlier forecast. Reported targets and
graded quality use the assignment's quality identity, which excludes the
provenance-only ``context_version``; probabilities, matched evidence and costs
keep their original exact-context identity.
"""
    config, now = validate_config(config), _timestamp(now)
    all_rows = sorted((r for r in _canonical_observations(observations) if r["observed_at"] <= now),
                      key=lambda r: (r["observed_at"], r["outcome"] == "accepted", r["id"]))
    rows = list(_case_outcomes(all_rows, now, corrections=False))
    targets = {_quality_case_key(row): row
               for row in _case_outcomes(all_rows, now, key=_quality_case_key)}
    cases = _graded_cases(all_rows, now)
    totals = {_case_key(row): row for row in _cost_outcomes(all_rows, now)}
    # Group globally by case, even if a source mirror or later execution context
    # would otherwise create a distinct posterior observation.
    first_case_time = {}
    for row in rows:
        first_case_time.setdefault(row["case_id"], row["observed_at"])
    rows = [row for row in rows if row["observed_at"] == first_case_time[row["case_id"]]]
    scoring_ids = {row["id"] for row in rows if row["origin"] == "local"}
    evidence = _ChronologicalEvidence(config)
    predictions, observed_rows = [], []
    seen_local_cases = set()
    for timestamp, batch_iterator in itertools.groupby(all_rows, key=lambda r: r["observed_at"]):
        batch = list(batch_iterator)
        evidence.advance(timestamp)
        forecasts = {}
        for row in batch:
            if row["id"] not in scoring_ids or row["case_id"] in seen_local_cases:
                continue
            seen_local_cases.add(row["case_id"])
            excluded_case = row["case_id"] if row["case_id"] in evidence.by_case else None
            cache_key = (tuple(sorted(row["features"].items())), excluded_case)
            if cache_key not in forecasts:
                forecasts[cache_key] = evidence.forecast(row["features"], excluded_case, timestamp)
            prediction = forecasts[cache_key]
            probability = prediction["posterior"]["mean"]
            target = targets[_quality_case_key(row)]
            resolved = routing_quality.case_quality(cases.get(_quality_case_key(row), (target,)))
            if resolved["kind"] == "explicit":
                quality_score = resolved["score"]
            elif resolved["kind"] == "neutral":
                quality_score = None
            else:
                quality_score = routing_quality.legacy_score(target["outcome"])
            predictions.append({"id": row["id"], "case_id": row["case_id"],
                                "observed_at": row["observed_at"], "probability": probability,
                                "observed_action": row["action"],
                                "economics": prediction["economics"],
                                "outcome": target["outcome"], "outcome_id": target["id"],
                                "outcome_observed_at": target["observed_at"],
                                "quality_score": quality_score, "quality_kind": resolved["kind"]})
            observed_rows.append(dict(row, outcome=target["outcome"],
                                      cost_usd=totals[_case_key(row)].get("cost_usd")))
        evidence.learn(batch, timestamp)
    scored = [p for p in predictions if p["observed_action"] == "worker"]
    # Neutral explicit quality supplies no success or failure mass and is
    # excluded from the scoring metrics; legacy rows keep the binary target.
    graded = [p for p in scored if p["quality_score"] is not None]
    bins = [[] for _ in range(10)]
    brier, loss = [], []
    for prediction in graded:
        p, y = prediction["probability"], prediction["quality_score"]
        brier.append((p - y) ** 2)
        bounded = max(1e-15, min(1. - 1e-15, p))
        # Fractional rubric credit uses the same cross-entropy as the binary
        # legacy rule, which it reproduces exactly at y in {0, 1}.
        loss.append(-(y * math.log(bounded) + (1. - y) * math.log1p(-bounded)))
        bins[min(9, int(p * 10))].append((p, y))
    calibration = [{"lower": i / 10, "upper": (i + 1) / 10, "count": len(bucket),
                    "mean_probability": math.fsum(p for p, _ in bucket) / len(bucket) if bucket else None,
                    "observed_acceptance": sum(y for _, y in bucket) / len(bucket) if bucket else None}
                   for i, bucket in enumerate(bins)]
    observed_outcomes = {}
    for action in ("worker", "coordinator"):
        actual = [row for row in observed_rows if row["action"] == action]
        costs = [row["cost_usd"] for row in actual if row.get("cost_usd") is not None]
        observed_outcomes[action] = {
            "cases": len(actual),
            "accepted": sum(row["outcome"] == "accepted" for row in actual),
            "rework": sum(row["outcome"] == "rework" for row in actual),
            "rejected": sum(row["outcome"] == "rejected" for row in actual),
            "cost_cases": len(costs),
            "total_cost_usd": math.fsum(costs),
            "mean_cost_usd": math.fsum(c / len(costs) for c in costs) if costs else None,
        }
    quality_cases = {
        "explicit": sum(p["quality_kind"] == "explicit" for p in scored),
        "legacy": sum(p["quality_kind"] == "legacy" for p in scored),
        "neutral": sum(p["quality_kind"] == "neutral" for p in scored),
        "clean_first_pass": sum(p["outcome"] == "accepted" for p in scored),
        "scored": len(graded),
    }
    return {"evaluated_cases": len(scored), "observed_cases": len(predictions),
            "brier_score": math.fsum(brier) / len(graded) if graded else None,
            "log_loss": math.fsum(loss) / len(graded) if graded else None,
            "calibration": calibration,
            "observed_outcomes": observed_outcomes,
            "quality_cases": quality_cases,
            "predictions": predictions}
