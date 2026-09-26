"""Descriptive local worker quality statistics grouped by task category.

This module is a pure read-only summary. It never records a routing decision,
feedback event or experiment, and it never touches project state beyond the
observations and configuration already loaded by the caller. It pools local
worker cases across task contexts by ``features.kind`` so an operator can see
which categories appear to work better or worse.

The summary shares assignment identity and first-labelled-failure selection
with local learned quality, and reuses the estimator's canonical effort,
evidence aging and Beta credible interval. Context-version changes cannot
make one assignment appear as several cases in either local quality view.
Categories are a pooled descriptive view, not a contextual routing decision.
"""
from __future__ import annotations

from . import routing_estimator, routing_learning, routing_quality
from .routing_models import RoutingError, validate_config

CASE_UNIT = "each distinct coordinator-reviewed worker assignment"
SCORE_BASIS = ("lower bound of the configured credible interval over aging-weighted "
               "rubric-based local worker quality (explicit graded assessments, or the "
               "legacy binary outcome)")
SORT_CHOICES = ("score", "cases", "category")
SUMMARY_SCOPE = "Pooled category history across task contexts, not a routing decision."
QUALITY_BASIS = ("explicit graded worker/shared assessments replace the legacy binary inference: "
                 "met=1.0, minor_gaps=0.8, major_gaps=0.3, unusable=0.0; unassessable or "
                 "non-worker attribution stays neutral and is excluded from the score")
CLEAN_FIRST_PASS_BASIS = ("cases with accepted feedback and no recorded rework or rejection: "
                          "the actual clean-first-pass count, not the rubric-based score")
CATEGORY_COLUMNS = ("CATEGORY", "SCORE", "ESTIMATE", "INTERVAL", "CASES", "EFFECTIVE", "ACCEPTED",
                    "REWORK", "REJECTED", "UNLABELLED", "EXPLICIT", "LEGACY", "NEUTRAL", "CLEAN")


def _blank():
    return {"cases": 0, "accepted": 0, "rework": 0, "rejected": 0, "unlabelled": 0,
            "explicit": 0, "legacy": 0, "neutral": 0, "clean": 0,
            "effective_support": 0.0, "accepted_weight": 0.0, "failed_weight": 0.0,
            "has_evidence": False}


def _round(value):
    return None if value is None else round(float(value), 6)


def _estimate(accepted_weight, failed_weight, confidence, *, has_evidence):
    if not has_evidence:
        return {"score": None, "lower": None, "upper": None, "mean": None}
    alpha = 1.0 + accepted_weight
    beta = 1.0 + failed_weight
    tail = (1.0 - confidence) / 2.0
    lower = routing_estimator._beta_quantile(tail, alpha, beta)
    upper = 1.0 - routing_estimator._beta_quantile(tail, beta, alpha)
    return {"score": _round(lower), "lower": _round(lower), "upper": _round(upper),
            "mean": _round(alpha / (alpha + beta))}


def _sort_key(sort):
    if sort == "category":
        return lambda item: (item["category"],)
    if sort == "cases":
        return lambda item: (-item["cases"], -item["effective_support"], item["category"])
    # Default: conservative score descending, then effective support, then name.
    # A category without usable evidence has no score and sorts last.
    return lambda item: (item["score"] is None, -(item["score"] or 0.0),
                         -item["effective_support"], item["category"])


def summarize(observations, config, *, now=None, sort="score") -> dict:
    """Summarize local worker quality by category without mutating the inputs."""
    if sort not in SORT_CHOICES:
        raise RoutingError("Unknown statistics sort order.")
    config = validate_config(config)
    now = routing_estimator._timestamp(now)
    rows = [row for row in routing_estimator._canonical_observations(observations)
            if row.get("origin") == "local" and row.get("action") == "worker"
            and row.get("observed_at", 0) <= now]
    labelled = list(routing_learning._case_outcomes(rows, now))
    labelled_keys = {routing_learning._case_key(row) for row in labelled}
    cases = routing_learning._graded_cases(rows, now)

    groups: dict = {}
    for row in labelled:
        age = (now - row["observed_at"]) / 86400.0
        if age > config["max_evidence_age_days"]:
            continue
        group = groups.setdefault(row["features"]["kind"], _blank())
        group["cases"] += 1
        outcome = row["outcome"]
        if outcome in ("accepted", "rework", "rejected"):
            group[outcome] += 1
        if outcome == "accepted":
            group["clean"] += 1
        resolved = routing_quality.case_quality(cases.get(routing_learning._case_key(row), (row,)))
        if resolved["kind"] == "explicit":
            group["explicit"] += 1
            score = resolved["score"]
        elif resolved["kind"] == "neutral":
            group["neutral"] += 1
            score = None
        else:
            group["legacy"] += 1
            score = routing_quality.legacy_score(outcome)
        weight = row["reliability"] * 2.0 ** (-age / config["half_life_days"])
        if weight <= 0.0 or score is None:
            continue
        group["effective_support"] += weight
        group["has_evidence"] = True
        group["accepted_weight"] += weight * score
        group["failed_weight"] += weight * (1.0 - score)

    unlabelled: dict = {}
    for row in rows:
        if (now - row["observed_at"]) / 86400.0 > config["max_evidence_age_days"]:
            continue
        key = routing_learning._case_key(row)
        if key not in labelled_keys:
            unlabelled[key] = row["features"]["kind"]
    for kind in unlabelled.values():
        group = groups.setdefault(kind, _blank())
        group["cases"] += 1
        group["unlabelled"] += 1

    categories = []
    for kind, group in groups.items():
        estimate = _estimate(group["accepted_weight"], group["failed_weight"],
                             config["confidence"], has_evidence=group["has_evidence"])
        categories.append({
            "category": kind,
            "score": estimate["score"],
            "lower": estimate["lower"],
            "upper": estimate["upper"],
            "mean": estimate["mean"],
            "cases": group["cases"],
            "effective_support": _round(group["effective_support"]),
            "accepted": group["accepted"],
            "rework": group["rework"],
            "rejected": group["rejected"],
            "unlabelled": group["unlabelled"],
            "explicit": group["explicit"],
            "legacy": group["legacy"],
            "neutral": group["neutral"],
            "clean": group["clean"],
        })
    categories.sort(key=_sort_key(sort))
    return {
        "scope": SUMMARY_SCOPE,
        "case_unit": CASE_UNIT,
        "score_basis": SCORE_BASIS,
        "quality_basis": QUALITY_BASIS,
        "clean_first_pass_basis": CLEAN_FIRST_PASS_BASIS,
        "sort": sort,
        "confidence": config["confidence"],
        "half_life_days": config["half_life_days"],
        "max_evidence_age_days": config["max_evidence_age_days"],
        "categories": categories,
    }


def _format_row(cells, widths):
    rendered = [cells[0].ljust(widths[0])]
    rendered.extend(cell.rjust(width) for cell, width in zip(cells[1:], widths[1:]))
    return "  ".join(rendered).rstrip()


def render_table(summary) -> str:
    """Render a fixed, simple English table plus a short explanation."""
    rows = []
    for item in summary["categories"]:
        rows.append((
            item["category"],
            "n/a" if item["score"] is None else f"{item['score']:.3f}",
            "n/a" if item["mean"] is None else f"{item['mean']:.3f}",
            "n/a" if item["lower"] is None else f"[{item['lower']:.3f}, {item['upper']:.3f}]",
            str(item["cases"]),
            f"{item['effective_support']:.2f}",
            str(item["accepted"]),
            str(item["rework"]),
            str(item["rejected"]),
            str(item["unlabelled"]),
            str(item["explicit"]),
            str(item["legacy"]),
            str(item["neutral"]),
            str(item["clean"]),
        ))
    widths = [len(header) for header in CATEGORY_COLUMNS]
    for row in rows:
        widths = [max(width, len(cell)) for width, cell in zip(widths, row)]
    lines = [_format_row(CATEGORY_COLUMNS, widths)]
    if rows:
        lines.extend(_format_row(row, widths) for row in rows)
    else:
        lines.append("No local worker cases recorded.")
    lines.append("")
    lines.append(summary["scope"])
    lines.append(f"score = {summary['score_basis']} "
                 f"({summary['confidence'] * 100:.1f}% confidence).")
    lines.append(f"case unit = {summary['case_unit']}.")
    lines.append(f"Only local worker outcomes within {summary['max_evidence_age_days']:g} days count; "
                 "infrastructure, cancelled and "
                 "unknown outcomes are unlabelled.")
    lines.append(f"quality = {summary['quality_basis']}; the lowest scored worker assessment "
                 "of one assignment wins.")
    lines.append(f"CLEAN counts {summary['clean_first_pass_basis']}.")
    return "\n".join(lines)
