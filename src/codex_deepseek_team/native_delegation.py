"""Supported coordinator attestations for native-agent deliverables.

Coordinator-native subagents are a narrow exception: the coordinator may run
them only on an explicit user instruction or when the deliverable needs a
capability the DeepSeek worker sandbox cannot provide. Parallelism or isolated
context alone is convenience, not an exception. Recorded exceptions are
coordinator attestations; they are never proof of user provenance.
"""
from __future__ import annotations

import re

EXPLICIT_USER_REQUEST = "explicit_user_request"
NATIVE_CAPABILITY = "native_capability"
NATIVE_EXCEPTION_CODES = (EXPLICIT_USER_REQUEST, NATIVE_CAPABILITY)
NATIVE_EXECUTOR = "native-agent"
MINIMUM_EVIDENCE = 12
MINIMUM_CAPABILITY = 4
# Words that only describe convenience; a statement built solely from them is
# not a supported capability or user instruction.
_CONVENIENCE_WORDS = frozenset((
    "parallel", "parallelism", "isolated", "isolate", "isolation", "context",
    "contexts", "convenience", "convenient", "comparison", "independent",
    "separate", "otherwise", "complementary",
    "and", "or", "for", "the", "a", "an", "to", "with", "in", "of",
    "agent", "agents", "native", "subagent", "subagents", "review",
))


def _only_convenience(text: str) -> bool:
    words = re.findall(r"[a-z]+", text.lower())
    return bool(words) and all(word in _CONVENIENCE_WORDS for word in words)


def validate_native_exception(item) -> None:
    """Return None for a supported native exception; raise ValueError otherwise.

    Non native-agent items are out of scope and also return None. Unknown
    attributes are left untouched; nothing is invented or defaulted.
    """
    if not isinstance(item, dict):
        raise ValueError("native exception validation requires a deliverable object")
    if item.get("executor") != NATIVE_EXECUTOR:
        return None
    exception = item.get("native_exception")
    if not isinstance(exception, dict):
        raise ValueError(
            "native-agent deliverables require native_exception with "
            '{"code": "explicit_user_request", "evidence": "specific user instruction"} '
            'or {"code": "native_capability", "capability": "...", "evidence": "..."}'
        )
    code = exception.get("code")
    if code not in NATIVE_EXCEPTION_CODES:
        raise ValueError(
            "native_exception code must be one of " + ", ".join(NATIVE_EXCEPTION_CODES) +
            "; generic parallelism or isolated-context convenience is not a supported exception"
        )
    evidence = exception.get("evidence")
    if not isinstance(evidence, str) or len(evidence.strip()) < MINIMUM_EVIDENCE:
        raise ValueError(
            "native_exception requires concrete evidence of at least "
            f"{MINIMUM_EVIDENCE} characters explaining why the native agent is required"
        )
    if _only_convenience(evidence):
        raise ValueError(
            "native_exception evidence only cites generic convenience; cite the explicit "
            "user instruction or the concrete capability required for the scope"
        )
    if code == NATIVE_CAPABILITY:
        capability = exception.get("capability")
        if not isinstance(capability, str) or len(capability.strip()) < MINIMUM_CAPABILITY:
            raise ValueError(
                "native_capability requires the specific capability or tool that is "
                "unavailable to a DeepSeek worker"
            )
        if _only_convenience(capability):
            raise ValueError(
                "native_capability requires a concrete capability/tool, not generic "
                "parallelism or isolated context"
            )
    return None
