"""Private per-assignment rework journal and versioned delegation lessons.

The service is intentionally independent of ``coordination`` and ``worker`` so both
can import it without cycles, and it uses only the standard library. All state is
private per-project local state outside the project tree:

``<state>/lessons/<sha256(resolved project path)[:24]>/``

with ``journal.sqlite3`` (canonical append-only outcome/review history),
``lessons.lock`` (cross-process write lock) and ``DELEGATION_LESSONS.md``
(generated Markdown, recoverable from the canonical rules). The SQLite history
is the authority: the Markdown mirror is published only after the review that
it describes has committed, and a stale or missing mirror is regenerated from
canonical state by the next review. The default state
base is ``~/.local/state/codex-deepseek``; ``DEEPSEEK_TEAM_STATE_DIR`` overrides
it exactly like the routing and coordination stores.

Read-only queries never create files and never mutate state. Rules are advisory
task-planning guidance; this module never executes instructions, calls a model or
edits project files such as ``AGENTS.md``.
"""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import time

DISPOSITIONS = ("incorporated", "reproduced", "needs-rework", "rejected")
CAUSES = ("worker_error", "brief_gap", "context_gap", "requirements_changed",
          "integration", "environment", "mixed", "unknown")
SEVERITIES = ("minor", "major", "redo", "unknown")
WHEN_KEYS = ("kind", "domain", "operation", "runtime", "effort")
KIND_VALUES = frozenset((
    "implementation", "test", "fixture", "documentation", "metadata",
    "review", "research", "diagnostic", "test_plan",
    "architecture", "security", "integration", "final_verification",
    "commit_push", "production", "secret_signing",
))
DOMAIN_VALUES = frozenset((
    "python", "javascript", "typescript", "java", "go", "rust", "shell",
    "documentation", "other", "unknown",
))
OPERATION_VALUES = frozenset((
    "diagnose", "fix", "extend", "refactor", "test", "document", "review", "unknown",
))
RUNTIME_VALUES = frozenset(("codex", "claude"))
CANONICAL_EFFORTS = frozenset(("low", "high", "max"))
LEGACY_EFFORT_ALIASES = {"medium": "high"}
UNKNOWN = "unknown"

MAX_RULES = 20
MAX_IDENTIFIER = 128
MAX_TEXT = 2000
MAX_EVIDENCE = 4000
MAX_EVIDENCE_CASES = 100
MAX_CONTEXT_BYTES = 1024 * 1024
CASE_REVIEW_THRESHOLD = 10
REWORK_REVIEW_THRESHOLD = 3

_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}")

_SCHEMA = {
    "metadata": "CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
    "events": ("CREATE TABLE events (seq INTEGER PRIMARY KEY AUTOINCREMENT, "
               "event_key TEXT NOT NULL UNIQUE, case_id TEXT NOT NULL, at REAL NOT NULL, "
               "disposition TEXT NOT NULL, evidence TEXT NOT NULL, context TEXT NOT NULL, "
               "rework TEXT)"),
    "events_case": "CREATE INDEX events_case ON events (case_id, seq)",
    "reviews": ("CREATE TABLE reviews (version INTEGER PRIMARY KEY, at REAL NOT NULL, "
                "expected_version INTEGER NOT NULL, through_event INTEGER NOT NULL, "
                "summary TEXT NOT NULL, rules TEXT NOT NULL)"),
    "rules": ("CREATE TABLE rules (version INTEGER NOT NULL, ordinal INTEGER NOT NULL, "
              "value TEXT NOT NULL, PRIMARY KEY (version, ordinal))"),
}


class LessonError(ValueError):
    """Invalid lessons input, unsafe state or an unsafe review transition."""


# --------------------------------------------------------------------------
# Private state locations and safe file handling
# --------------------------------------------------------------------------
def _normalize_effort(value):
    if not isinstance(value, str):
        return None
    value = LEGACY_EFFORT_ALIASES.get(value, value)
    return value if value in CANONICAL_EFFORTS else None


def _state_base() -> Path:
    configured = os.environ.get("DEEPSEEK_TEAM_STATE_DIR")
    if configured:
        path = Path(configured)
        if not path.is_absolute() or ".." in path.parts:
            raise LessonError("DEEPSEEK_TEAM_STATE_DIR must be an absolute canonical path.")
        return path
    return Path.home() / ".local/state/codex-deepseek"


def _project(root) -> Path:
    try:
        candidate = Path(root)
        return candidate.resolve()
    except (TypeError, OSError, ValueError):
        raise LessonError("root must be a resolvable project path.") from None


def _paths(root):
    project = _project(root)
    key = hashlib.sha256(os.fsencode(str(project))).hexdigest()[:24]
    directory = _state_base() / "lessons" / key
    return (project, directory, directory / "journal.sqlite3",
            directory / "lessons.lock", directory / "DELEGATION_LESSONS.md")


def _open_directory(path: Path, *, create: bool):
    """Open a private directory by descriptor, never following symlinks.

    Returns an owned descriptor, or ``None`` when a component is missing and
    ``create`` is false. Only the final directory's ownership and mode are
    enforced, mirroring the routing store.
    """
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:]:
            if create:
                try:
                    os.mkdir(part, mode=0o700, dir_fd=fd)
                except FileExistsError:
                    pass
            try:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            except FileNotFoundError:
                os.close(fd)
                return None
            except OSError as error:
                raise LessonError("Lessons state path must not contain symlinks.") from error
            os.close(fd)
            fd = child
        info = os.fstat(fd)
        if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise LessonError("Lessons state directories must be private and user-owned.")
        return fd
    except BaseException:
        os.close(fd)
        raise


def _private_fd(fd, kind):
    info = os.fstat(fd)
    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
            or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o600):
        raise LessonError(f"Lessons {kind} must be a private, user-owned, ordinary file.")
    return info


def _verify_schema(db):
    present = set()
    for kind, name, sql in db.execute("SELECT type, name, sql FROM sqlite_master"):
        if name.startswith("sqlite_"):
            continue
        expected = _SCHEMA.get(name)
        if expected is None or kind not in ("table", "index"):
            raise LessonError("Unexpected lessons database schema; refusing changed tables, "
                              "views or triggers.")
        if re.sub(r"\s+", " ", sql or "").strip() != re.sub(r"\s+", " ", expected).strip():
            raise LessonError("Unexpected lessons database schema; refusing changed tables, "
                              "views or triggers.")
        present.add(name)
    if set(_SCHEMA) - present:
        raise LessonError("Lessons database schema is incomplete.")


def _initialize(db, project: str):
    version = db.execute("PRAGMA user_version").fetchone()[0]
    if version == 0:
        for sql in _SCHEMA.values():
            db.execute(sql)
        db.execute("INSERT INTO metadata VALUES ('project', ?)", (project,))
        db.execute("PRAGMA user_version=1")
    elif version != 1:
        raise LessonError("Unsupported lessons state format; refusing to migrate it.")
    _verify_schema(db)
    row = db.execute("SELECT value FROM metadata WHERE key='project'").fetchone()
    if row is None or row[0] != project:
        raise LessonError("Lessons state belongs to another project.")


@contextmanager
def _write_db(root, *, after_commit=None):
    project, directory, database, lock_name, _ = _paths(root)
    directory_fd = lock_fd = db_fd = connection = None
    try:
        directory_fd = _open_directory(directory, create=True)
        lock_fd = os.open(lock_name.name, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600,
                          dir_fd=directory_fd)
        _private_fd(lock_fd, "lock file")
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        for suffix in ("", "-journal", "-wal", "-shm"):
            try:
                sidecar = os.open(database.name + suffix,
                                  os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                                  dir_fd=directory_fd)
            except FileNotFoundError:
                continue
            except OSError as error:
                raise LessonError("Lessons database sidecars must not be symlinks.") from error
            try:
                _private_fd(sidecar, "database")
            finally:
                os.close(sidecar)
        db_fd = os.open(database.name, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600,
                        dir_fd=directory_fd)
        before = _private_fd(db_fd, "database")
        connection = sqlite3.connect(f"/proc/self/fd/{directory_fd}/{database.name}",
                                     timeout=30, isolation_level=None)
        after = os.stat(database.name, dir_fd=directory_fd, follow_symlinks=False)
        if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
            raise LessonError("Lessons database changed while opening.")
        connection.execute("PRAGMA trusted_schema=OFF")
        connection.execute("PRAGMA journal_mode=DELETE")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("BEGIN IMMEDIATE")
        _initialize(connection, str(project))
        yield connection
        connection.commit()
        if after_commit is not None:
            after_commit()
    except LessonError:
        raise
    except (OSError, sqlite3.Error):
        raise LessonError("Cannot safely access lessons state; check ownership, permissions "
                          "and database integrity.") from None
    finally:
        if connection is not None:
            connection.close()
        for fd in (db_fd, lock_fd, directory_fd):
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass


@contextmanager
def _read_db(root):
    _, directory, database, _, _ = _paths(root)
    directory_fd = db_fd = connection = None
    try:
        directory_fd = _open_directory(directory, create=False)
        if directory_fd is None:
            yield None
            return
        try:
            db_fd = os.open(database.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory_fd)
        except FileNotFoundError:
            yield None
            return
        except OSError as error:
            raise LessonError("Lessons database must not be a symlink.") from error
        _private_fd(db_fd, "database")
        connection = sqlite3.connect(f"file:/proc/self/fd/{db_fd}?mode=ro", uri=True, timeout=30)
        connection.execute("PRAGMA trusted_schema=OFF")
        connection.execute("PRAGMA query_only=ON")
        # One deferred read transaction for the whole state read: every statement
        # afterwards sees the same committed snapshot, so a concurrent review can
        # never be mixed into a partially visible state. Closing rolls it back.
        connection.execute("BEGIN")
        yield connection
    except LessonError:
        raise
    except (OSError, sqlite3.Error):
        raise LessonError("Cannot safely read lessons state; check ownership, permissions "
                          "and database integrity.") from None
    finally:
        if connection is not None:
            connection.close()
        for fd in (db_fd, directory_fd):
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass


# --------------------------------------------------------------------------
# Canonical reads
# --------------------------------------------------------------------------
def _events(db):
    rows = db.execute("SELECT seq, case_id, at, disposition, evidence, context, rework "
                      "FROM events ORDER BY seq").fetchall()
    events = []
    for seq, case_id, at, disposition, evidence, context, rework in rows:
        try:
            context_value = json.loads(context)
            rework_value = None if rework is None else json.loads(rework)
        except (TypeError, ValueError):
            raise LessonError("Stored lessons state is malformed.") from None
        if (type(seq) is not int or not isinstance(case_id, str) or not isinstance(at, (int, float))
                or disposition not in DISPOSITIONS or not isinstance(evidence, str)
                or not isinstance(context_value, dict)
                or (rework_value is not None and not isinstance(rework_value, dict))):
            raise LessonError("Stored lessons state is malformed.")
        events.append({
            "seq": seq, "case_id": case_id, "at": at, "disposition": disposition,
            "evidence": evidence, "context": context_value, "rework": rework_value,
        })
    return events


def _stored_rule(value):
    keys = {"id", "when", "condition", "action", "evidence"}
    if not isinstance(value, dict) or set(value) != keys:
        raise LessonError("Stored lessons rules are malformed.")
    if (not isinstance(value["id"], str) or not isinstance(value["when"], dict)
            or set(value["when"]) - set(WHEN_KEYS) or not isinstance(value["condition"], str)
            or not isinstance(value["action"], str) or not isinstance(value["evidence"], list)
            or not value["evidence"] or not all(isinstance(case, str) for case in value["evidence"])):
        raise LessonError("Stored lessons rules are malformed.")
    return value


def _reviews(db):
    row = db.execute("SELECT version, through_event, rules FROM reviews "
                     "ORDER BY version DESC LIMIT 1").fetchone()
    if row is None:
        return 0, 0, []
    version, through_event, raw_rules = row
    if type(version) is not int or version < 1 \
            or type(through_event) is not int or through_event < 0:
        raise LessonError("Stored lessons state is malformed.")
    stored = []
    for (value,) in db.execute("SELECT value FROM rules WHERE version=? ORDER BY ordinal",
                               (version,)):
        try:
            stored.append(_stored_rule(json.loads(value)))
        except ValueError:
            raise LessonError("Stored lessons rules are malformed.") from None
    try:
        stored_rules = [_stored_rule(rule) for rule in json.loads(raw_rules)]
    except (TypeError, ValueError):
        raise LessonError("Stored lessons rules are malformed.") from None
    if stored_rules != stored:
        raise LessonError("Stored lessons reviews and rules disagree.")
    return version, through_event, stored


def _project_check(db, project: str):
    row = db.execute("SELECT value FROM metadata WHERE key='project'").fetchone()
    if row is None or row[0] != project:
        raise LessonError("Lessons state belongs to another project.")


def _state_from_db(db, project: str):
    version = db.execute("PRAGMA user_version").fetchone()[0]
    if version == 0:
        return {"events": [], "version": 0, "reviewed_through": 0, "rules": []}
    if version != 1:
        raise LessonError("Unsupported lessons state format; refusing to open it.")
    _verify_schema(db)
    _project_check(db, project)
    events = _events(db)
    rules_version, through_event, rules = _reviews(db)
    return {"events": events, "version": rules_version,
            "reviewed_through": through_event, "rules": rules}


def _load(root):
    project, _, _, _, _ = _paths(root)
    with _read_db(root) as db:
        if db is None:
            return {"events": [], "version": 0, "reviewed_through": 0, "rules": []}
        return _state_from_db(db, str(project))


def journal(root) -> list[dict]:
    """Return chronological append-only outcome events with stable sequence numbers."""
    return _load(root)["events"]


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------
def _text(value, name: str, limit: int = MAX_TEXT) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit \
            or "\x00" in value:
        raise LessonError(f"{name} must be a bounded nonempty string.")
    return value


def _identifier(value, name: str) -> str:
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
        raise LessonError(f"{name} must be an identifier without slashes or control characters.")
    return value


def _case_reference(value, name: str) -> str:
    if not isinstance(value, str) or len(value) > 2 * MAX_IDENTIFIER + 1:
        raise LessonError(f"{name} must be a case identifier.")
    task, separator, assignment = value.partition("/")
    if not separator or "/" in assignment:
        raise LessonError(f"{name} must be a task/assignment case identifier.")
    _identifier(task, name)
    _identifier(assignment, name)
    return value


def _bounded_int(value, name: str) -> int:
    if type(value) is not int or value < 0 or value > 2 ** 53:
        raise LessonError(f"{name} must be a non-negative integer.")
    return value


def _canonical(value) -> str:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=True, allow_nan=False)
    except (TypeError, ValueError, RecursionError):
        raise LessonError("Lessons values must be JSON serializable without NaN.") from None


def _bounded_json(value, name: str, limit: int) -> str:
    text = _canonical(value)
    if len(text.encode("utf-8")) > limit:
        raise LessonError(f"{name} exceeds the {limit} byte limit.")
    return text


def validate_rework(value):
    """Validate and normalize coordinator-supplied rework attribution.

    ``None`` stays ``None``. A supplied value requires exactly ``cause``,
    ``severity``, ``summary`` and ``prevention``; the outer outcome ``evidence``
    remains the required verifiable reference for the correction.
    """
    if value is None:
        return None
    if not isinstance(value, dict):
        raise LessonError("rework must be an object or null.")
    if set(value) != {"cause", "severity", "summary", "prevention"}:
        raise LessonError("rework requires exactly cause, severity, summary and prevention.")
    cause, severity = value["cause"], value["severity"]
    if cause not in CAUSES:
        raise LessonError("Unknown rework cause; use a known cause or 'unknown'.")
    if severity not in SEVERITIES:
        raise LessonError("Unknown rework severity; use a known severity or 'unknown'.")
    return {"cause": cause, "severity": severity,
            "summary": _text(value["summary"], "rework summary"),
            "prevention": _text(value["prevention"], "rework prevention")}


# --------------------------------------------------------------------------
# Outcome recording
# --------------------------------------------------------------------------
def _event_key(case_id: str, disposition: str, evidence: str, context, rework) -> str:
    payload = _canonical([case_id, disposition, evidence, context, rework])
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def record_outcome(root, *, task_id, assignment_id, disposition, evidence, context, rework=None) -> dict:
    """Append one outcome event; an exact replay returns the original event.

    ``case_id`` is ``task_id + '/' + assignment_id``. ``context`` is the
    integration-supplied JSON execution snapshot; missing metadata stays unknown
    on read. The cause never implies quality.
    """
    task_id = _identifier(task_id, "task_id")
    assignment_id = _identifier(assignment_id, "assignment_id")
    case_id = f"{task_id}/{assignment_id}"
    if disposition not in DISPOSITIONS:
        raise LessonError("Invalid outcome disposition.")
    evidence = _text(evidence, "evidence", MAX_EVIDENCE)
    if not isinstance(context, dict):
        raise LessonError("context must be a JSON object.")
    raw_context = _bounded_json(context, "context", MAX_CONTEXT_BYTES)
    rework = validate_rework(rework)
    raw_rework = None if rework is None else _canonical(rework)
    key = _event_key(case_id, disposition, evidence, context, rework)
    at = time.time()
    with _write_db(root) as db:
        row = db.execute("SELECT seq, at, disposition, evidence, context, rework FROM events "
                         "WHERE event_key=?", (key,)).fetchone()
        if row is not None:
            return {"seq": row[0], "case_id": case_id, "at": row[1], "disposition": row[2],
                    "evidence": row[3], "context": json.loads(row[4]),
                    "rework": None if row[5] is None else json.loads(row[5])}
        cursor = db.execute(
            "INSERT INTO events (event_key, case_id, at, disposition, evidence, context, rework) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (key, case_id, at, disposition, evidence, raw_context, raw_rework),
        )
        if cursor.lastrowid is None:
            raise LessonError("Cannot append the outcome event.")
        seq = cursor.lastrowid
    return {"seq": seq, "case_id": case_id, "at": at, "disposition": disposition,
            "evidence": evidence, "context": context, "rework": rework}


# --------------------------------------------------------------------------
# Consolidated status and snapshots
# --------------------------------------------------------------------------
_FEATURE_VALUES = {
    "kind": KIND_VALUES,
    "domain": DOMAIN_VALUES,
    "operation": OPERATION_VALUES,
    "runtime": RUNTIME_VALUES,
}


def _feature(context, name: str):
    """Read one feature; unrecognized values stay unknown instead of inventing a cohort.

    A malformed value is treated like a missing one, exactly as ``effort`` already
    normalizes ``medium`` and rejects anything else, so metadata can never define a
    concrete cohort or a same-kind rework group.
    """
    if not isinstance(context, dict):
        return None
    features = context.get("features")
    sources = [features if isinstance(features, dict) else {}, context]
    allowed = _FEATURE_VALUES.get(name)
    for source in sources:
        value = source.get(name)
        if name == "effort":
            normalized = _normalize_effort(value)
            if normalized is not None:
                return normalized
        elif isinstance(value, str) and 0 < len(value) <= MAX_IDENTIFIER:
            if allowed is None or value in allowed:
                return value
    return None


def _lessons_version(context):
    if not isinstance(context, dict):
        return None
    for key in ("lessons", "snapshot"):
        block = context.get(key)
        if isinstance(block, dict):
            value = block.get("version")
            if type(value) is int and 0 <= value <= 2 ** 53:
                return value
    value = context.get("lessons_version")
    if type(value) is int and 0 <= value <= 2 ** 53:
        return value
    return None


def _case_facts(events):
    facts = {}
    for event in events:
        case_id = event["case_id"]
        case = facts.get(case_id)
        if case is None:
            case = {"case_id": case_id, "events": [], "last_seq": event["seq"],
                    "kind": None, "effort": None, "version": None,
                    "rework": False, "rejected": False, "unknown_attribution": False,
                    "causes": set(), "severities": set()}
            facts[case_id] = case
        case["events"].append(event)
        case["last_seq"] = event["seq"]
        if case["kind"] is None:
            case["kind"] = _feature(event["context"], "kind")
        if case["effort"] is None:
            case["effort"] = _feature(event["context"], "effort")
        if case["version"] is None:
            case["version"] = _lessons_version(event["context"])
        disposition = event["disposition"]
        if disposition == "needs-rework" or (disposition in ("incorporated", "reproduced")
                                            and event["rework"] is not None):
            case["rework"] = True
        if disposition == "rejected":
            case["rejected"] = True
        structured = event["rework"] if isinstance(event["rework"], dict) else None
        if disposition in ("needs-rework", "rejected") or structured is not None:
            cause = structured.get("cause") if structured else UNKNOWN
            severity = structured.get("severity") if structured else UNKNOWN
            if cause not in CAUSES:
                cause = UNKNOWN
            if severity not in SEVERITIES:
                severity = UNKNOWN
            case["causes"].add(cause)
            case["severities"].add(severity)
            if cause == UNKNOWN:
                # A structured correction with an unknown cause is unclassified
                # wherever it is attached, including on incorporation.
                case["unknown_attribution"] = True
    return facts


def _status(root, state):
    _, _, _, _, markdown = _paths(root)
    facts = _case_facts(state["events"])
    reviewed = state["reviewed_through"]
    pending = [case for case in facts.values() if case["last_seq"] > reviewed]
    due_reasons = []
    if len(pending) >= CASE_REVIEW_THRESHOLD:
        due_reasons.append(f"cases_since_review={len(pending)}/{CASE_REVIEW_THRESHOLD}")
    groups = {}
    for case in pending:
        for event in case["events"]:
            corrected = (event["disposition"] == "needs-rework"
                         or (event["disposition"] in ("incorporated", "reproduced")
                             and event["rework"] is not None))
            if event["seq"] <= reviewed or not corrected:
                continue
            structured = event["rework"] if isinstance(event["rework"], dict) else {}
            cause = structured.get("cause")
            if case["kind"] not in (None, UNKNOWN) and cause in CAUSES and cause != UNKNOWN:
                groups.setdefault((case["kind"], cause), set()).add(case["case_id"])
    for (kind, cause), case_ids in sorted(groups.items()):
        if len(case_ids) >= REWORK_REVIEW_THRESHOLD:
            due_reasons.append(f"rework_cases={len(case_ids)}/{REWORK_REVIEW_THRESHOLD} "
                               f"kind={kind} cause={cause}")
    counts = {"cases": len(facts), "clean": 0, "rework": 0, "rejected": 0,
              "unknown_attribution": 0}
    causes, severities, cohorts = {}, {}, {}
    for case in facts.values():
        if case["rework"]:
            counts["rework"] += 1
        if case["rejected"]:
            counts["rejected"] += 1
        if not case["rework"] and not case["rejected"]:
            counts["clean"] += 1
        if case["unknown_attribution"]:
            counts["unknown_attribution"] += 1
        for cause in case["causes"]:
            causes[cause] = causes.get(cause, 0) + 1
        for severity in case["severities"]:
            severities[severity] = severities.get(severity, 0) + 1
        key = (case["kind"] or UNKNOWN, case["effort"] or UNKNOWN,
               case["version"] if case["version"] is not None else UNKNOWN)
        bucket = cohorts.setdefault(key, {"cases": 0, "clean": 0, "rework": 0, "rejected": 0})
        bucket["cases"] += 1
        if case["rework"]:
            bucket["rework"] += 1
        if case["rejected"]:
            bucket["rejected"] += 1
        if not case["rework"] and not case["rejected"]:
            bucket["clean"] += 1
    cohort_rows = [
        {"kind": kind, "effort": effort, "version": version,
         "cases": bucket["cases"], "clean": bucket["clean"],
         "rework": bucket["rework"], "rejected": bucket["rejected"]}
        for (kind, effort, version), bucket in sorted(
            cohorts.items(), key=lambda item: (item[0][0], item[0][1], str(item[0][2])))
    ]
    return {
        "version": state["version"],
        "rules_file": str(markdown),
        "review_due": bool(due_reasons),
        "due_reasons": due_reasons,
        "cases_since_review": len(pending),
        "through_event": max((event["seq"] for event in state["events"]), default=0),
        "counts": counts,
        "cohorts": cohort_rows,
        "causes": {key: causes[key] for key in sorted(causes)},
        "severities": {key: severities[key] for key in sorted(severities)},
    }


def status(root) -> dict:
    """Return consolidated case counts, cohorts, causes and review due state.

    ``through_event`` is the current journal head; ``cases_since_review`` counts
    distinct cases updated after the acknowledged review watermark.
    """
    return _status(root, _load(root))


def _matches(when: dict, features) -> bool:
    if features is None:
        return True
    if not isinstance(features, dict):
        raise LessonError("features must be an object or null.")
    for key, expected in when.items():
        supplied = features.get(key)
        if key == "effort":
            supplied = _normalize_effort(supplied)
        if supplied in (None, UNKNOWN) or supplied != expected:
            return False
    return True


def snapshot(root, features=None) -> dict:
    """Return the active rules applicable to ``features``.

    Without ``features`` every active rule is returned. With features, a rule
    matches only when every field it constrains is supplied exactly; a missing or
    unknown feature never satisfies a constrained rule. Effort ``medium``
    normalizes to ``high``. The snapshot never mutates policy or state.
    """
    if features is not None and not isinstance(features, dict):
        raise LessonError("features must be an object or null.")
    state = _load(root)
    rules = [rule for rule in state["rules"] if _matches(rule["when"], features)]
    return {"version": state["version"],
            "rule_ids": [rule["id"] for rule in rules],
            "rules": rules}


def review_bundle(root) -> dict:
    """Return the status plus the consolidated evidence a coordinator reviews.

    ``expected_version`` is the optimistic version to submit back and
    ``reviewed_through`` is the last acknowledged review watermark. All cases are
    included so every active rule keeps its supporting evidence.
    """
    state = _load(root)
    result = _status(root, state)
    result["expected_version"] = state["version"]
    result["reviewed_through"] = state["reviewed_through"]
    result["cases"] = [
        {"case_id": case["case_id"],
         "kind": case["kind"] or UNKNOWN,
         "effort": case["effort"] or UNKNOWN,
         "version": case["version"] if case["version"] is not None else UNKNOWN,
         "events": case["events"]}
        for case in _case_facts(state["events"]).values()
    ]
    result["rules"] = state["rules"]
    return result


# --------------------------------------------------------------------------
# Reviews
# --------------------------------------------------------------------------
def _validate_when(value):
    if not isinstance(value, dict) or set(value) - set(WHEN_KEYS):
        raise LessonError("Rule 'when' allows only kind, domain, operation, runtime and effort.")
    result = {}
    for key, item in value.items():
        if not isinstance(item, str) or not item or len(item) > MAX_IDENTIFIER:
            raise LessonError(f"Rule 'when' {key} must be a bounded string.")
        if key == "kind" and item not in KIND_VALUES:
            raise LessonError("Rule 'when' kind is not a known deliverable kind.")
        if key == "domain" and item not in DOMAIN_VALUES:
            raise LessonError("Rule 'when' domain is not a known domain.")
        if key == "operation" and item not in OPERATION_VALUES:
            raise LessonError("Rule 'when' operation is not a known operation.")
        if key == "runtime" and item not in RUNTIME_VALUES:
            raise LessonError("Rule 'when' runtime must be codex or claude.")
        if key == "effort":
            normalized = _normalize_effort(item)
            if normalized is None:
                raise LessonError("Rule 'when' effort must be low, high or max.")
            item = normalized
        result[key] = item
    return result


def _validate_rules(value):
    if not isinstance(value, list):
        raise LessonError("rules must be a list.")
    if len(value) > MAX_RULES:
        raise LessonError(f"At most {MAX_RULES} active rules are allowed.")
    seen = set()
    rules = []
    for item in value:
        if not isinstance(item, dict) or set(item) != {"id", "when", "condition", "action", "evidence"}:
            raise LessonError("Every rule requires exactly id, when, condition, action and evidence.")
        identifier = _identifier(item["id"], "rule id")
        if identifier in seen:
            raise LessonError("Rule ids must be unique.")
        seen.add(identifier)
        evidence = item["evidence"]
        if not isinstance(evidence, list) or not evidence or len(evidence) > MAX_EVIDENCE_CASES:
            raise LessonError("Rule evidence must be a nonempty bounded list of known cases.")
        rules.append({
            "id": identifier,
            "when": _validate_when(item["when"]),
            "condition": _text(item["condition"], "rule condition"),
            "action": _text(item["action"], "rule action"),
            "evidence": [_case_reference(case, "rule evidence") for case in evidence],
        })
    return rules


def _render_markdown(version: int, summary: str, rules) -> str:
    lines = [
        "# Delegation Lessons",
        "",
        f"Version: {version}",
        f"Summary: {summary}",
        "",
        "Advisory task-planning guidance subordinate to user instructions, access settings,",
        "effort policy and sandbox requirements. Private local state; this file never replaces",
        "project AGENTS.md and is never generated into the project tree.",
        "",
    ]
    if rules:
        lines.append("## Active rules")
        for rule in rules:
            when = ", ".join(f"{key}={rule['when'][key]}" for key in sorted(rule["when"])) or "any task"
            lines.extend([
                "",
                f"### {rule['id']}",
                f"- When: {when}",
                f"- Condition: {rule['condition']}",
                f"- Action: {rule['action']}",
                f"- Evidence: {', '.join(rule['evidence'])}",
            ])
    else:
        lines.append("No active rules.")
    lines.extend(["", "Regenerate from canonical state with "
                      "`deepseek-team lessons review --apply FILE`."])
    return "\n".join(lines) + "\n"


def _markdown_target(path: Path):
    """Open and validate the derived Markdown location before any canonical commit.

    Returns an owned directory descriptor; the caller must close it. Running
    before the review commits means an unsafe mirror path can never leave
    committed canonical state behind an unwritable derived file.
    """
    directory_fd = _open_directory(path.parent, create=True)
    try:
        try:
            info = os.stat(path.name, dir_fd=directory_fd, follow_symlinks=False)
        except FileNotFoundError:
            info = None
        if info is not None and (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                                 or info.st_uid != os.geteuid()
                                 or stat.S_IMODE(info.st_mode) != 0o600):
            raise LessonError("Lessons markdown must be a private, user-owned, ordinary file.")
    except BaseException:
        os.close(directory_fd)
        raise
    return directory_fd


def _write_markdown(directory_fd, name: str, text: str):
    """Atomically publish the derived Markdown into an already validated directory."""
    temp_name = f".{name}.{os.getpid()}.{time.time_ns()}.tmp"
    try:
        handle = None
        try:
            handle = os.open(temp_name, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW,
                             0o600, dir_fd=directory_fd)
            os.write(handle, text.encode("utf-8"))
            os.fsync(handle)
            _private_fd(handle, "markdown")
        finally:
            if handle is not None:
                os.close(handle)
        os.replace(temp_name, name, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
        os.fsync(directory_fd)
    finally:
        try:
            os.unlink(temp_name, dir_fd=directory_fd)
        except FileNotFoundError:
            pass


def _publish_markdown(directory_fd, name: str, text: str, version: int):
    """Publish the mirror after its review committed, still holding the writer lock.

    The review can no longer be rolled back, so a failure is reported as a
    committed review whose derived file will be regenerated instead of being
    reported (or treated) as a rollback.
    """
    try:
        _write_markdown(directory_fd, name, text)
    except (LessonError, OSError) as error:
        raise LessonError(
            f"Review version {version} is committed to canonical lessons state, but the "
            f"derived Markdown could not be updated; the next review regenerates it from "
            f"the canonical rules: {error}") from None
    finally:
        os.close(directory_fd)


def apply_review(root, payload: dict) -> dict:
    """Apply a bounded full replacement ruleset or a justified no-change review.

    The payload is exactly ``expected_version``, ``through_event``, ``summary``
    and ``rules``. Every rule requires exactly ``id``, ``when``, ``condition``,
    ``action`` and ``evidence``; evidence must reference known cases at or before
    ``through_event``. The optimistic version and the forward-only watermark are
    checked before any write, and every review stays immutable with its own
    version even when the rules do not change. The derived Markdown is validated
    before the commit and published only afterwards, while the writer lock is
    still held; a publish failure is reported as a committed review. Returns the
    fresh status.
    """
    if not isinstance(payload, dict) or set(payload) != {"expected_version", "through_event",
                                                        "summary", "rules"}:
        raise LessonError("Review payload requires exactly expected_version, through_event, "
                          "summary and rules.")
    expected_version = _bounded_int(payload["expected_version"], "expected_version")
    through_event = _bounded_int(payload["through_event"], "through_event")
    summary = _text(payload["summary"], "summary")
    rules = _validate_rules(payload["rules"])
    project, _, _, _, markdown = _paths(root)
    mirror = {}

    def publish():
        _publish_markdown(mirror.pop("directory_fd"), markdown.name, mirror["text"],
                          mirror["version"])

    try:
        with _write_db(root, after_commit=publish) as db:
            state = _state_from_db(db, str(project))
            if expected_version != state["version"]:
                raise LessonError("Stale expected_version; review the current rules before applying.")
            if through_event < state["reviewed_through"]:
                raise LessonError("through_event cannot move the review watermark backwards.")
            sequences = {event["seq"] for event in state["events"]}
            if through_event != 0 and through_event not in sequences:
                raise LessonError("through_event must be a valid journal snapshot sequence.")
            known = set()
            for event in state["events"]:
                if event["seq"] <= through_event:
                    known.add(event["case_id"])
            for rule in rules:
                for case_id in rule["evidence"]:
                    if case_id not in known:
                        raise LessonError(f"Rule evidence references a case outside the reviewed "
                                          f"snapshot: {case_id}")
            version = state["version"] + 1
            db.execute("INSERT INTO reviews VALUES (?, ?, ?, ?, ?, ?)",
                       (version, time.time(), expected_version, through_event, summary,
                        _canonical(rules)))
            for ordinal, rule in enumerate(rules):
                db.execute("INSERT INTO rules VALUES (?, ?, ?)", (version, ordinal, _canonical(rule)))
            # Validate the mirror target before the commit and render the exact
            # committed version for publication after the commit succeeds.
            text = _render_markdown(version, summary, rules)
            mirror["directory_fd"] = _markdown_target(markdown)
            mirror["text"] = text
            mirror["version"] = version
    finally:
        directory_fd = mirror.pop("directory_fd", None)
        if directory_fd is not None:
            os.close(directory_fd)
    return status(root)


def render_guidance(root, features=None) -> str:
    """Render concise advisory rules and the due-review reminder.

    Read-only: no files are created, no state is modified and nothing is
    executed. Empty or inapplicable state returns an empty string.
    """
    if features is not None and not isinstance(features, dict):
        raise LessonError("features must be an object or null.")
    state = _load(root)
    result = _status(root, state)
    matching = [rule for rule in state["rules"] if _matches(rule["when"], features)]
    lines = []
    if matching:
        lines.append(f"Delegation lessons v{state['version']} (advisory, private local state):")
        for rule in matching:
            when = ", ".join(f"{key}={rule['when'][key]}"
                             for key in sorted(rule["when"])) or "any task"
            lines.append(f"- [{rule['id']}] when {when}: {rule['condition']} "
                         f"Action: {rule['action']} (evidence: {', '.join(rule['evidence'])})")
    if result["review_due"]:
        lines.append("Review due (" + "; ".join(result["due_reasons"]) + "). Run "
                     "`deepseek-team lessons review --json` and apply with "
                     "`deepseek-team lessons review --apply FILE`.")
    return "\n".join(lines)
