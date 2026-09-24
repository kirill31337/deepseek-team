"""Bounded classification of coordinator activity for the lifecycle hooks.

Two independent decisions live here so ``coordinator_hooks`` stays a thin
adapter over the coordination ledger.  The module imports only
:mod:`scope_matching`; ledger identity and persistence remain with the hook and
the existing coordination helpers.

``classify_inspection``
    Classify a read-only tool call:

    * ``source`` -- substantive source inspection; only this verdict takes part
      in the early planning gate;
    * ``metadata`` -- exempt status/bootstrap metadata or an instruction read;
    * ``opaque`` -- unrecognized or unbounded, so outside covered scope;
    * ``none`` -- not an inspection tool at all.

``mutation_authorizers``
    A protected coordinator deliverable declares a *context/read* scope, so a
    source mutation is authorized only by explicit decision artifacts contained
    in that scope, or by a validated integration write scope whose exact files
    are backed by referenced terminal worker changes or a currently accepted
    ordinary coordinator write.  Ordinary deliverables keep scope authority.
"""
from __future__ import annotations

import os
from pathlib import Path
import re
import shlex

from . import scope_matching


SOURCE_EXTENSIONS = frozenset({
    '.py', '.pyi', '.pyx', '.pxd', '.js', '.jsx', '.mjs', '.cjs', '.ts', '.tsx',
    '.rs', '.go', '.java', '.kt', '.kts', '.c', '.h', '.cc', '.cpp', '.cxx',
    '.hpp', '.hh', '.hxx', '.cs', '.rb', '.php', '.swift', '.scala', '.sh',
    '.bash', '.zsh', '.ksh', '.fish', '.pl', '.pm', '.lua', '.r', '.sql',
    '.vue', '.svelte', '.dart', '.ex', '.exs', '.erl', '.hrl', '.hs', '.ml',
    '.mli', '.clj', '.cljs', '.groovy', '.m', '.mm', '.proto', '.gradle',
})
DOCUMENT_EXTENSIONS = frozenset({'.md', '.rst', '.txt'})
EXEMPT_FILENAMES = frozenset({'AGENTS.md', 'CLAUDE.md', '.deepseek-team.toml'})
EXEMPT_PREFIXES = frozenset({'.git', '.deepseek-team', '.codex', '.claude'})

SHELL_TOOLS = frozenset({'Bash', 'bash', 'sh', 'exec_command', 'shell_command',
                         'run_command', 'run_shell'})
READ_TOOLS = frozenset({'Read', 'read', 'read_file', 'read_text_file', 'view_file',
                        'open_file'})
SEARCH_TOOLS = frozenset({'Grep', 'grep', 'search', 'search_text', 'code_search'})
LIST_TOOLS = frozenset({'Glob', 'glob', 'list_dir', 'list_files', 'find_files'})
CONTROL_TOOLS = frozenset({'wait', 'wait_agent', 'wait_for', 'wait_tool'})

METADATA_COMMANDS = frozenset({
    'ls', 'pwd', 'echo', 'printf', 'which', 'type', 'command', 'env', 'printenv',
    'stat', 'file', 'du', 'df', 'tree', 'date', 'whoami', 'hostname', 'uname',
    'id', 'git', 'find', 'true', 'false', 'test', '[', 'sleep',
    'deepseek-team', 'deepseek_team',
})
SOURCE_COMMANDS = frozenset({
    'cat', 'bat', 'batcat', 'head', 'tail', 'less', 'more', 'nl', 'sed', 'awk',
    'gawk', 'mawk', 'grep', 'egrep', 'fgrep', 'rg', 'ag', 'ack', 'pt', 'wc',
    'sort', 'uniq', 'cut', 'tr', 'column', 'diff', 'cmp', 'comm', 'xxd', 'od',
    'hexdump', 'strings', 'tac', 'rev', 'jq', 'yq', 'expand', 'unexpand',
    'paste', 'fold', 'fmt', 'look',
})
GIT_PATCH_FLAGS = frozenset({'-p', '--patch', '-u', '--word-diff', '-U'})
GIT_STAT_FLAGS = frozenset({'-s', '--stat', '--numstat', '--shortstat',
                            '--name-only', '--name-status'})

_ASSIGNMENT = re.compile(r'[A-Za-z_][A-Za-z_0-9]*=')


def normalize_tool_name(tool_name) -> str:
    """Last qualified segment, so ``functions.exec_command`` is ``exec_command``."""
    text = str(tool_name or '').strip().replace('/', '.')
    if not text:
        return ''
    if '.' in text:
        text = text.rsplit('.', 1)[-1]
    if '__' in text:
        text = text.rsplit('__', 1)[-1]
    return text


def is_shell_tool(tool_name) -> bool:
    return normalize_tool_name(tool_name) in SHELL_TOOLS


def _target(value: str) -> str:
    return value.split('::', 1)[0]


def _exempt_target(root, value, cwd=None) -> bool:
    canonical = scope_matching.canonical(root, value, cwd=cwd)
    if canonical is None:
        # Reading outside the attached project is not our source inspection.
        return True
    parts = canonical.split('/')
    if parts[0] in EXEMPT_PREFIXES:
        return True
    return parts[-1] in EXEMPT_FILENAMES


def _looks_like_source(root, value, cwd=None) -> bool:
    canonical = scope_matching.canonical(root, _target(value), cwd=cwd)
    if canonical is None:
        return False
    if canonical == '.':
        return True
    parts = canonical.split('/')
    if parts[0] in EXEMPT_PREFIXES or parts[-1] in EXEMPT_FILENAMES:
        return False
    if Path(parts[-1]).suffix.lower() in SOURCE_EXTENSIONS:
        return True
    try:
        return (Path(root) / canonical).is_dir()
    except OSError:
        return False


def _git_verdict(args) -> str:
    # Global options may consume values which are not Git subcommands.
    index = 0
    value_options = {'-C', '-c', '--git-dir', '--work-tree', '--namespace', '--config-env'}
    while index < len(args) and args[index].startswith('-'):
        index += 2 if args[index] in value_options else 1
    sub = args[index] if index < len(args) else ''
    args = args[index + 1:]
    patch = any(arg.startswith(('--patch', '-U')) or arg in GIT_PATCH_FLAGS for arg in args)
    if sub == 'log':
        if patch:
            return 'source'
        return 'metadata'
    if sub in ('show', 'diff', 'grep', 'blame', 'whatchanged'):
        if sub in ('show', 'diff') and not patch and any(arg in GIT_STAT_FLAGS for arg in args):
            return 'metadata'
        return 'source'
    return 'metadata'


def _source_command_verdict(args, root, cwd) -> str:
    for arg in args:
        if not arg or arg.startswith('-') or arg == '-':
            continue
        if _looks_like_source(root, arg, cwd=cwd):
            return 'source'
    return 'metadata'


def _segment_verdict(segment: str, root, cwd) -> str:
    try:
        tokens = shlex.split(segment, posix=True)
    except ValueError:
        return 'opaque'
    while tokens and _ASSIGNMENT.match(tokens[0]):
        tokens.pop(0)
    if not tokens:
        return 'metadata'
    command = tokens[0].rsplit('/', 1)[-1]
    args = tokens[1:]
    if command == 'git':
        return _git_verdict(args)
    if command in SOURCE_COMMANDS:
        # Recursive search defaults to the working directory when no target is
        # supplied. A pattern is not a filename, and must not exempt that read.
        recursive_grep = command in ('grep', 'egrep', 'fgrep') and any(
            arg in ('--recursive', '--dereference-recursive') or
            (arg.startswith('-') and not arg.startswith('--') and ('r' in arg or 'R' in arg))
            for arg in args)
        if command in ('rg', 'ag', 'ack', 'pt') or recursive_grep:
            if not any(_looks_like_source(root, arg, cwd=cwd) for arg in args):
                targets = [arg for arg in args if not arg.startswith('-')
                           and (Path(cwd or root) / arg).is_file()]
                return 'metadata' if targets else 'source'
        return _source_command_verdict(args, root, cwd)
    if command in METADATA_COMMANDS:
        return 'metadata'
    return 'opaque'


def _split_segments(command: str):
    """Split top-level ``;|&`` sequences; ``None`` for unbounded shell text."""
    if '`' in command or '$(' in command or '<<' in command:
        return None
    segments, current, quote = [], [], None
    index, length = 0, len(command)
    while index < length:
        char = command[index]
        if quote is not None:
            if char == '\\' and quote == '"' and index + 1 < length:
                current.append(char)
                current.append(command[index + 1])
                index += 2
                continue
            if char == quote:
                quote = None
            current.append(char)
            index += 1
            continue
        if char in '\'"':
            quote = char
            current.append(char)
            index += 1
            continue
        if char == '\\' and index + 1 < length:
            current.append(char)
            current.append(command[index + 1])
            index += 2
            continue
        if char in ';|&\n':
            segments.append(''.join(current))
            current = []
            while index < length and command[index] in ';|&\n':
                index += 1
            continue
        current.append(char)
        index += 1
    if quote is not None:
        return None
    segments.append(''.join(current))
    return [segment for segment in segments if segment.strip()]


def _shell_verdict(command, root, cwd) -> str:
    if not isinstance(command, str) or not command.strip():
        return 'none'
    segments = _split_segments(command)
    if segments is None:
        return 'opaque'
    verdicts = [_segment_verdict(segment, root, cwd) for segment in segments]
    if 'source' in verdicts:
        return 'source'
    if 'opaque' in verdicts:
        return 'opaque'
    return 'metadata' if verdicts else 'none'


def _read_verdict(root, data, cwd) -> str:
    value = None
    for key in ('file_path', 'path', 'notebook_path', 'target_file', 'file'):
        candidate = data.get(key)
        if isinstance(candidate, str) and candidate:
            value = candidate
            break
    if value is None:
        return 'opaque'
    if _exempt_target(root, value, cwd=cwd):
        return 'metadata'
    return 'source' if Path(value).suffix.lower() in SOURCE_EXTENSIONS else 'metadata'


def _search_verdict(root, data, cwd) -> str:
    for key in ('path', 'glob', 'target', 'file_path'):
        value = data.get(key)
        if isinstance(value, str) and value and _exempt_target(root, value, cwd=cwd):
            return 'metadata'
    return 'source'


def classify_inspection(tool_name, tool_input, root, *, cwd=None) -> str:
    """Classify one read-only tool call; see the module docstring for verdicts."""
    name = normalize_tool_name(tool_name)
    data = tool_input if isinstance(tool_input, dict) else {}
    if name in READ_TOOLS:
        return _read_verdict(root, data, cwd)
    if name in SEARCH_TOOLS or name in LIST_TOOLS:
        return _search_verdict(root, data, cwd)
    if name in CONTROL_TOOLS:
        return 'none'
    if name in SHELL_TOOLS:
        command = None
        for key in ('command', 'cmd', 'shell', 'script'):
            candidate = data.get(key)
            if isinstance(candidate, str) and candidate.strip():
                command = candidate
                break
        return _shell_verdict(command, root, cwd)
    return 'none'


def exact_file(root, value):
    """Canonical project-relative exact file, or ``None`` when it grants nothing.

    Rejects globs, absolute values, directories, escapes above the project root
    and link aliases: the lexical relative path must equal the canonical one,
    and an existing file must have one hard link, so documentation paths cannot
    grant indirect write access to another source or config file.
    """
    if not isinstance(value, str) or not value.strip() or scope_matching.is_glob(value):
        return None
    if Path(value).is_absolute():
        return None
    canonical = scope_matching.canonical(root, value)
    if canonical is None or canonical == '.':
        return None
    lexical = os.path.normpath(value).replace(os.sep, '/')
    if lexical != canonical:
        return None
    if (Path(root) / canonical).is_dir():
        return None
    try:
        if (Path(root) / canonical).stat().st_nlink > 1:
            return None
    except FileNotFoundError:
        pass  # An exact, new artifact may be created.
    except OSError:
        return None
    return canonical


def _canonical_scopes(root, deliverable) -> list[str]:
    found = []
    for raw in deliverable.get('scope', []) or []:
        canonical = scope_matching.canonical(root, raw)
        if canonical is not None:
            found.append(canonical)
    return found


def has_protected_deliverable(task, protected_kinds) -> bool:
    return any(isinstance(item, dict) and item.get('kind') in protected_kinds
               for item in task.get('deliverables', []) or [])


def _decision_artifact_files(root, item, scopes):
    files, issues = [], []
    declared = item.get('decision_artifacts')
    if not isinstance(declared, list) or not declared:
        return [], [f"{item.get('id')}: protected source writes require decision_artifacts of "
                    "exact .md/.rst/.txt files contained in scope"]
    for value in declared:
        canonical = exact_file(root, value)
        if canonical is None:
            issues.append(f"{item.get('id')}: decision artifact {value!r} is not an exact "
                          "in-project file; directories, globs, symlink aliases and outside "
                          "paths are rejected")
            continue
        if Path(canonical).suffix.lower() not in DOCUMENT_EXTENSIONS:
            issues.append(f"{item.get('id')}: decision artifact {canonical!r} must be a "
                          "markdown, reStructuredText or text file")
            continue
        if not any(scope_matching.contained(canonical, scope) for scope in scopes):
            issues.append(f"{item.get('id')}: decision artifact {canonical!r} is outside the "
                          "declared context scope")
            continue
        files.append(canonical)
    if issues:
        return [], issues
    return files, []


def _recorded_mutation_files(root, task, target) -> set[str]:
    scopes = _canonical_scopes(root, target)
    files = set()
    for event in task.get('coordinator_events', []) or []:
        if not isinstance(event, dict) or event.get('kind') != 'mutation_requested':
            continue
        for raw in event.get('paths') or []:
            canonical = exact_file(root, raw)
            if canonical is None:
                continue
            if any(scope_matching.contained(canonical, scope)
                   for scope in scopes):
                files.add(canonical)
    return files


def _reference_files(root, task, reference):
    target = next(item for item in task['deliverables'] if item['id'] == reference)
    if target['executor'] == 'coordinator':
        return _recorded_mutation_files(root, task, target)
    assignment = next(row for row in task['assignments']
                      if row['deliverable_id'] == reference)
    scopes = _canonical_scopes(root, target)
    return {path for raw in assignment['worker_changes']
            if (path := exact_file(root, raw)) is not None
            and any(scope_matching.contained(path, scope) for scope in scopes)}


def _reference_problem(root, task, item, reference, worker_write_kinds):
    identity = item.get('id')
    if not isinstance(reference, str) or not reference.strip():
        return f"{identity}: integration_of entries must be deliverable ids"
    if reference == identity:
        return f"{identity}: an integration deliverable cannot reference itself"
    target = next((entry for entry in task.get('deliverables', []) or []
                   if isinstance(entry, dict) and entry.get('id') == reference), None)
    if target is None:
        return f"{identity}: integration_of references unknown deliverable {reference!r}"
    if target.get('kind') not in worker_write_kinds:
        return (f"{identity}: integration_of {reference!r} is a {target.get('kind')!r} "
                "deliverable, not a write deliverable")
    executor = target.get('executor')
    if executor == 'worker':
        assignment = next((row for row in task.get('assignments', []) or []
                           if isinstance(row, dict)
                           and row.get('deliverable_id') == reference), None)
        if assignment is None:
            return f"{identity}: integration_of {reference!r} has no worker assignment"
        if assignment.get('status') not in ('succeeded', 'failed'):
            return (f"{identity}: integration_of {reference!r} is still pending; review its "
                    "terminal result first")
        if (not isinstance(assignment.get('worker_changes'), list)
                or not assignment['worker_changes']):
            return (f"{identity}: integration_of {reference!r} recorded no actual "
                    "worker_changes")
        return None
    if executor == 'coordinator':
        if 'result_invalidated_at' in target:
            return (f"{identity}: integration_of {reference!r} accepted outcome is stale; a "
                    "later mutation invalidated it")
        result = target.get('result')
        if not isinstance(result, dict) or result.get('outcome') != 'accepted':
            return (f"{identity}: integration_of {reference!r} is not a currently accepted "
                    "coordinator result")
        if not _recorded_mutation_files(root, task, target):
            return (f"{identity}: integration_of {reference!r} has no recorded concrete "
                    "mutation to integrate")
        return None
    return (f"{identity}: integration_of {reference!r} must be a terminal worker or a "
            "currently accepted coordinator write")


def _integration_files(root, task, item, worker_write_kinds):
    files, issues = [], []
    declared = item.get('write_scope')
    if not isinstance(declared, list) or not declared:
        return [], [f"{item.get('id')}: an integration write requires a non-empty "
                    "write_scope of exact files"]
    for value in declared:
        canonical = exact_file(root, value)
        if canonical is None:
            issues.append(f"{item.get('id')}: write_scope entry {value!r} is not an exact "
                          "in-project file; directories, globs, symlink aliases and outside "
                          "paths are rejected")
            continue
        files.append(canonical)
    references = item.get('integration_of')
    if not isinstance(references, list) or not references:
        issues.append(f"{item.get('id')}: an integration write requires integration_of "
                      "deliverable ids")
    else:
        for reference in references:
            problem = _reference_problem(root, task, item, reference, worker_write_kinds)
            if problem:
                issues.append(problem)
    if issues:
        return [], issues
    backed = set().union(*(_reference_files(root, task, ref) for ref in references))
    missing = sorted(set(files) - backed)
    if missing:
        return [], [f"{item.get('id')}: write_scope is not backed by actual referenced "
                    "file changes: " + ', '.join(missing) +
                    ". Route newly discovered implementation as its own deliverable."]
    return files, []


def _protected_files(root, task, item, scopes, worker_write_kinds):
    if item.get('kind') == 'integration' and (item.get('integration_of') is not None
                                              or item.get('write_scope') is not None):
        return _integration_files(root, task, item, worker_write_kinds)
    if item.get('decision_artifacts') is not None:
        return _decision_artifact_files(root, item, scopes)
    return [], []


def _protected_authorization(root, task, item, scopes, path, worker_write_kinds):
    files, issues = _protected_files(root, task, item, scopes, worker_write_kinds)
    if path in files:
        return True, None
    if issues:
        return False, issues[0]
    if any(scope_matching.contained(path, scope) for scope in scopes):
        return False, (
            f"DeepSeek Team protected-scope gate: {path} is inside the context/read scope of "
            f"protected deliverable {item.get('id')} ({item.get('kind')}), which grants no "
            "write authority. Declare decision_artifacts of exact .md/.rst/.txt files contained "
            "in scope, or use kind integration with integration_of and an exact write_scope.")
    return False, None


def mutation_authorizers(root, task, path, *, protected_kinds, worker_write_kinds):
    """Return ``(allowed_deliverables, problem)`` for one canonical mutation path.

    Ordinary deliverables authorize by scope containment.  Protected
    coordinator deliverables authorize only by an explicit decision artifact or
    a validated integration ``write_scope`` file.  ``problem`` is the first
    actionable denial reason when nothing authorizes the path.
    """
    allowed, problems = [], []
    for item in task.get('deliverables', []) or []:
        if not isinstance(item, dict):
            continue
        scopes = _canonical_scopes(root, item)
        if item.get('kind') in protected_kinds:
            authorized, issue = _protected_authorization(
                root, task, item, scopes, path, worker_write_kinds)
        else:
            authorized = any(scope_matching.contained(path, scope) for scope in scopes)
            issue = None
        if authorized:
            allowed.append(item)
        elif issue:
            problems.append(issue)
    return allowed, (problems[0] if problems and not allowed else None)
