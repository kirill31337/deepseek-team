"""Single shipped source for the versioned overhead contract.

Both generated guidance modes, live policy output and lifecycle hooks read this
module. The resource is deliberately re-read on every call: a package upgrade
must be able to refresh stale project blocks and prompt context from the newly
installed data file without a long-lived process retaining an old copy.
"""
from pathlib import Path

DATA_FILE = Path(__file__).resolve().parent / "data" / "overhead-contract.md"
CONTRACT_VERSION = "1"
_VERSION_MARKER = (
    f"<!-- codex-deepseek-team:overhead-contract:v{CONTRACT_VERSION} -->".encode())


class OverheadContractError(Exception):
    """The packaged contract is missing, unreadable or not the current version."""


def instructions() -> str:
    """Return the current packaged minimum rules with one terminal newline."""
    try:
        raw = DATA_FILE.read_bytes()
    except OSError as error:
        raise OverheadContractError("packaged overhead contract is unavailable") from error
    if not raw.splitlines() or raw.splitlines()[0].strip() != _VERSION_MARKER:
        raise OverheadContractError(
            "packaged overhead contract has a missing or unsupported version marker")
    try:
        text = raw.decode("utf-8").strip()
    except UnicodeDecodeError as error:
        raise OverheadContractError("packaged overhead contract is not UTF-8") from error
    if not text:
        raise OverheadContractError("packaged overhead contract is empty")
    return text + "\n"
