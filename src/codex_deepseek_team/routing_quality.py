"""Declared rubric for explicit graded worker quality.

Pure, stdlib-only helpers shared by the closed observation contract, the local
learned-quality assessment, contextual estimates, descriptive statistics, the
chronological evaluation and the quality-failure cooldown. The rubric weights
are declared evaluation grades supplied by the coordinator, not measured
probabilities, work-share percentages or inferred causes.

One assignment remains one quality case per execution identity
(``origin``, ``case_id``, ``action``, ``runtime``, ``model``, canonical
``effort``). ``context_version`` is provenance, never a second quality case: an
explicit grade recorded under a later context version still belongs to the same
assignment's earlier forecast-context evidence, but only from the moment that
event is observed. Exact-context forecast matching and cost identity keep their
original context version. Explicit assessments replace the case's legacy binary
inference; the lowest scored worker/shared assessment wins (earliest tie), so a
later incorporation or neutral attribution can never launder a known worker
defect. Assessments attached to neutral outer outcomes (infrastructure,
cancelled, unknown) never grade worker quality.
"""
from __future__ import annotations

GRADES = ("met", "minor_gaps", "major_gaps", "unusable", "unassessable")
ATTRIBUTIONS = ("worker", "shared", "coordinator", "environment", "unknown")
GRADE_WEIGHTS = {"met": 1.0, "minor_gaps": .8, "major_gaps": .3, "unusable": 0.0,
                 "unassessable": None}
ASSESSMENT_KEYS = frozenset(("grade", "attribution", "evidence"))
MAX_EVIDENCE_CHARS = 4000
WORKER_ATTRIBUTIONS = frozenset(("worker", "shared"))
SUBSTANTIVE_GRADES = frozenset(("major_gaps", "unusable"))
LABELLED_OUTCOMES = frozenset(("accepted", "rework", "rejected"))
LEGACY_FAILURE_OUTCOMES = frozenset(("rework", "rejected"))

__all__ = [
    "GRADES", "ATTRIBUTIONS", "GRADE_WEIGHTS", "ASSESSMENT_KEYS", "MAX_EVIDENCE_CHARS",
    "WORKER_ATTRIBUTIONS", "SUBSTANTIVE_GRADES", "LABELLED_OUTCOMES",
    "LEGACY_FAILURE_OUTCOMES", "QUALITY_IDENTITY", "rubric_weight", "assessment_score",
    "substantive_failure", "legacy_score", "case_quality", "case_score", "case_substantive",
    "quality_case_key",
]

# Exact execution identity for quality comparison. ``context_version`` is
# deliberately absent: it is provenance, not part of the assignment's identity.
QUALITY_IDENTITY = ("runtime", "model", "effort")


def quality_case_key(row):
    """Return the graded-quality identity of one observation.

    Rows must carry canonical features (the shared canonical-observation path
    normalizes a legacy ``medium`` effort to ``high``). Unlike the contextual
    forecast key, this key omits ``context_version`` so one reviewed assignment
    stays one quality case across context versions.
    """
    features = row["features"]
    return (row["origin"], row["case_id"], row["action"],
            *(features[field] for field in QUALITY_IDENTITY))


def rubric_weight(grade):
    """Return the declared weight for a grade, or ``None`` when unassessable."""
    return GRADE_WEIGHTS.get(grade) if isinstance(grade, str) else None


def assessment_score(assessment):
    """Return the scored weight of a worker/shared assessment, else ``None``.

    Only ``worker`` and ``shared`` attributions with an assessable grade supply
    a quality score; coordinator, environment and unknown attributions stay
    neutral, and ``unassessable`` never scores.
    """
    if not isinstance(assessment, dict):
        return None
    if assessment.get("attribution") not in WORKER_ATTRIBUTIONS:
        return None
    return rubric_weight(assessment.get("grade"))


def substantive_failure(assessment):
    """Whether one assessment is a scored ``major_gaps`` or ``unusable`` worker gap."""
    return bool(isinstance(assessment, dict)
                and assessment.get("attribution") in WORKER_ATTRIBUTIONS
                and assessment.get("grade") in SUBSTANTIVE_GRADES)


def legacy_score(outcome):
    """The compatibility binary credit used when a case carries no assessment."""
    return 1.0 if outcome == "accepted" else 0.0


def case_quality(rows):
    """Resolve one assignment's explicit graded quality from its stored events.

    Returns ``{"kind", "score", "grade", "attribution", "assessment", "row_id"}``
    where ``kind`` is ``explicit`` (scored worker/shared assessment),
    ``neutral`` (explicit assessments exist but none are scored) or ``legacy``
    (no explicit assessment, so the binary first-labelled-failure rule applies).
    Only labelled outer outcomes carry worker quality; infrastructure, cancelled
    and unknown outcomes stay neutral regardless of any attached assessment.
    """
    explicit, scored = False, []
    for row in rows:
        if not isinstance(row, dict) or row.get("outcome") not in LABELLED_OUTCOMES:
            continue
        assessment = row.get("quality")
        if not isinstance(assessment, dict):
            continue
        explicit = True
        score = assessment_score(assessment)
        if score is None:
            continue
        scored.append((score, row.get("observed_at", 0.0), str(row.get("id", "")),
                       assessment, row))
    if scored:
        score, _, _, assessment, row = min(scored, key=lambda item: item[:3])
        return {"kind": "explicit", "score": score, "grade": assessment.get("grade"),
                "attribution": assessment.get("attribution"), "assessment": assessment,
                "row_id": row.get("id")}
    if explicit:
        return {"kind": "neutral", "score": None, "grade": None, "attribution": None,
                "assessment": None, "row_id": None}
    return {"kind": "legacy", "score": None, "grade": None, "attribution": None,
            "assessment": None, "row_id": None}


def case_score(rows, representative):
    """Graded success mass for one case.

    Returns the lowest scored worker assessment, the legacy binary credit when
    the case carries no assessment, or ``None`` when the case is neutral and
    must be excluded from both success and failure mass.
    """
    resolved = case_quality(rows)
    if resolved["kind"] == "explicit":
        return resolved["score"]
    if resolved["kind"] == "neutral":
        return None
    return legacy_score(representative.get("outcome"))


def case_substantive(rows):
    """Whether a whole case is a substantive worker failure for the cooldown.

    Explicit scored ``major_gaps``/``unusable`` cases are substantive, explicit
    neutral cases are not, and cases without an assessment keep the legacy
    binary rework/rejection rule.
    """
    resolved = case_quality(rows)
    if resolved["kind"] == "explicit":
        return resolved["grade"] in SUBSTANTIVE_GRADES
    if resolved["kind"] == "neutral":
        return False
    return any(isinstance(row, dict) and row.get("outcome") in LEGACY_FAILURE_OUTCOMES
               for row in rows)
