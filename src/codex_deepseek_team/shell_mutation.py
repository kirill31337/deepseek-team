"""Bounded shell mutation detection for coordination hooks, not a shell sandbox.

Keep shell operators distinct from quoted words and skip heredoc program text.
Only the commands listed below and shell redirections are recognized; arbitrary
programs, aliases, eval and shell functions can still write files. A nonempty
path list is returned only when every recognized write has a literal scope.

Two entry points share that bounded lexer:

* :func:`classify_shell_mutation` keeps the conservative contract for callers
  that cannot resolve a base working directory: any ``cd`` leaves the mutation
  unscoped.
* :func:`classify_shell_mutation_scoped` implements the one-project coordinator
  context: at most one leading ``cd <absolute-dir> &&`` prefix is applied to the
  literal write paths.  Only a plain literal absolute directory without a ``..``
  component is provable, only ``&&`` may follow it, and every following command
  must stay in an AND-only guarded chain that allows no ``;``, newline, ``||``,
  ``&``, pipe or subshell escape.  A relative, quoted, dynamic, option-bearing,
  repeated or nested ``cd``, a ``..`` component, or any other control-flow
  separator, or a shell-evoked directory change through ``time``, ``eval``,
  ``source``/``.`` and their ``command``/``builtin``/assignment forms marks the
  whole mutation cwd-uncertain so a partial or wrong scope is never authorized.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import posixpath
import re


_OPERATORS = ('&>>', '<<-', '<<<', '>>', '>|', '>&', '&>', '<>', '<<', '<&',
              '&&', '||', ';;', ';', '&', '|', '(', ')', '>', '<', '\n')
_OUTPUT = {'>', '>>', '>|', '&>', '&>>', '<>', '>&'}
_REDIRECT = _OUTPUT | {'<', '<<', '<<-', '<<<', '<&'}
_ASSIGNMENT = re.compile(r'[A-Za-z_][A-Za-z_0-9]*=')
_COMMAND_PREFIXES = {'if', 'then', 'else', 'elif', 'while', 'until', 'for',
                     'do', 'done', 'fi', '{', '}', '!'}
_CWD_BUILTINS = frozenset(('cd', 'pushd', 'popd'))
_CWD_EVALUATORS = frozenset(('eval', 'source', '.'))
_CWD_WRAPPERS = frozenset(('command', 'builtin', 'exec', 'time'))


@dataclass
class _Token:
    value: str
    operator: bool = False
    dynamic: bool = False
    quoted: bool = False
    start: int = 0
    end: int = 0


@dataclass
class _Result:
    mutation: bool = False
    paths: list[str] = field(default_factory=list)
    unknown: bool = False
    cwd: bool = False

    def write(self, paths: list[_Token]) -> None:
        self.mutation = True
        if not paths or any(token.dynamic or not token.value for token in paths):
            self.unknown = True
        else:
            self.paths.extend(token.value for token in paths)

    def merge(self, other: _Result) -> None:
        self.mutation |= other.mutation
        self.unknown |= other.unknown
        self.cwd |= other.cwd
        self.paths.extend(other.paths)


class _Lexer:
    """Read shell boundaries without executing expansions or removing their context."""

    def __init__(self, source: str, *, scoped: bool = False):
        self.source = source
        self.index = 0
        self.scoped = scoped
        self.nested = _Result()
        self.incomplete = False

    def scan(self, substitution: bool = False) -> list[_Token]:
        tokens = []
        heredocs = []
        depth = 0
        while self.index < len(self.source):
            char = self.source[self.index]
            if char in ' \t\r':
                self.index += 1
                continue
            if self.source.startswith('\\\n', self.index):
                self.index += 2
                continue
            if char == '#':
                end = self.source.find('\n', self.index)
                self.index = len(self.source) if end < 0 else end
                continue
            if substitution and char == ')' and depth == 0:
                self.index += 1
                return tokens
            conditional = self.source.startswith('[[', self.index) and (
                self.index + 2 == len(self.source) or self.source[self.index + 2] in ' \t\r\n;&|()<>')
            arithmetic = self.source.startswith('((', self.index)
            if (conditional or arithmetic) and _command_position(tokens):
                start = self.index
                self.index += 2
                self.expression(arithmetic)
                tokens.append(_Token(':', start=start, end=self.index))
                continue
            operator = next((op for op in _OPERATORS if self.source.startswith(op, self.index)), None)
            if operator:
                start = self.index
                self.index += len(operator)
                tokens.append(_Token(operator, operator=True, start=start, end=self.index))
                depth += (operator == '(') - (operator == ')')
                if operator == '\n':
                    for delimiter, tabs in heredocs:
                        self.heredoc(delimiter, tabs)
                    heredocs.clear()
                continue
            token = self.word()
            if tokens and tokens[-1].operator and tokens[-1].value in ('<<', '<<-'):
                heredocs.append((token, tokens[-1].value == '<<-'))
            tokens.append(token)
        if substitution or heredocs:
            self.incomplete = True
        return tokens

    def expression(self, arithmetic: bool) -> None:
        # Bash comparisons have their own operators, but their substitutions
        # still run commands. Retain quote boundaries around closing markers.
        quote = None
        depth = 2
        while self.index < len(self.source):
            char = self.source[self.index]
            if char == quote:
                quote = None
                self.index += 1
                continue
            if quote is None and char in "'\"":
                quote = char
                self.index += 1
                continue
            if char == '\\' and quote != "'":
                self.index += 2
                continue
            if quote != "'" and char in '$`':
                self.expansion()
                continue
            if quote is None:
                if arithmetic:
                    depth += (char == '(') - (char == ')')
                    if depth == 0:
                        self.index += 1
                        return
                elif self.source.startswith(']]', self.index) and (
                        self.source[self.index - 1] in ' \t\r\n;&|()<>') and (
                        self.index + 2 == len(self.source) or
                        self.source[self.index + 2] in ' \t\r\n;&|()<>'):
                    self.index += 2
                    return
            self.index += 1
        self.incomplete = True

    def word(self) -> _Token:
        start = self.index
        value = []
        quote = None
        quoted = dynamic = False
        while self.index < len(self.source):
            char = self.source[self.index]
            if quote is None and (char in ' \t\r\n;&|()<>'):
                break
            if char == quote:
                quote = None
                self.index += 1
                continue
            if quote is None and char in "'\"":
                quote = char
                quoted = True
                self.index += 1
                continue
            if char == '\\' and quote != "'":
                quoted = True
                self.index += 1
                if self.index < len(self.source):
                    escaped = self.source[self.index]
                    if escaped != '\n':
                        if quote == '"' and escaped not in '$`"\\':
                            value.append('\\')
                        value.append(escaped)
                    self.index += 1
                continue
            if quote != "'" and char in '$`':
                dynamic = True
                before = self.index
                self.expansion()
                value.append(self.source[before:self.index])
                continue
            if quote is None and char in '*?[{}~':
                dynamic = True
            value.append(char)
            self.index += 1
        if quote is not None:
            self.incomplete = True
        return _Token(''.join(value), dynamic=dynamic, quoted=quoted, start=start, end=self.index)

    def expansion(self) -> None:
        if self.source.startswith('$((', self.index):
            self.index += 3
            self.balanced('(', ')', 2)
        elif self.source.startswith('$(', self.index):
            self.index += 2
            self.nested.merge(_classify(self.scan(substitution=True)))
        elif self.source.startswith('$' + '{', self.index):
            self.index += 2
            self.balanced('{', '}', 1)
        elif self.source[self.index] == '`':
            self.index += 1
            body = []
            while self.index < len(self.source):
                char = self.source[self.index]
                self.index += 1
                if char == '`':
                    inner = _Lexer(''.join(body))
                    self.nested.merge(inner.result())
                    return
                if char == '\\' and self.index < len(self.source) and self.source[self.index] in '$`\\':
                    char = self.source[self.index]
                    self.index += 1
                body.append(char)
            self.incomplete = True
        else:
            self.index += 1

    def balanced(self, opening: str, closing: str, depth: int) -> None:
        # Arithmetic/parameter expansion operators are not shell redirections.
        while self.index < len(self.source):
            char = self.source[self.index]
            if char == '\\':
                self.index += 2
                continue
            if char in '$`':
                self.expansion()
                continue
            self.index += 1
            depth += (char == opening) - (char == closing)
            if depth == 0:
                return
        self.incomplete = True

    def heredoc(self, delimiter: _Token, strip_tabs: bool) -> None:
        start = self.index
        while self.index < len(self.source):
            end = self.source.find('\n', self.index)
            end = len(self.source) if end < 0 else end
            line = self.source[self.index:end]
            if (line.lstrip('\t') if strip_tabs else line) == delimiter.value:
                body = self.source[start:self.index]
                self.index = min(end + 1, len(self.source))
                if not delimiter.quoted:
                    # Only expansions execute in an unquoted heredoc. Quotes,
                    # command names and > in its program body remain data.
                    inner = _Lexer(body)
                    while inner.index < len(body):
                        if body[inner.index] == '\\':
                            inner.index += 2
                        elif body[inner.index] in '$`':
                            inner.expansion()
                        else:
                            inner.index += 1
                    self.nested.merge(inner.nested)
                    self.incomplete |= inner.incomplete
                return
            self.index = min(end + 1, len(self.source))
        self.incomplete = True

    def result(self) -> _Result:
        tokens = self.scan()
        prefix, remaining, ambiguous = (None, tokens, False)
        if self.scoped:
            prefix, remaining, ambiguous = _split_leading_cd(tokens)
        result = _classify(remaining)
        result.merge(self.nested)
        result.unknown |= self.incomplete
        if prefix is not None:
            if not result.unknown:
                result.paths = [posixpath.join(prefix, path) for path in result.paths]
        elif ambiguous:
            result.cwd = True
        return result


def _operands(command: str, arguments: list[_Token]) -> list[_Token] | None:
    """Parse only options whose effect on write scope is understood."""
    short_flags = {'rm': 'firdvRI', 'touch': 'acmh', 'tee': 'ai',
                   'truncate': 'co', 'cp': 'afipPrRLv', 'mv': 'finv'}
    value_options = {'touch': {'-d', '-t', '-r', '--date', '--reference'},
                     'truncate': {'-s', '-r', '--size', '--reference'}}
    paths = []
    options = True
    index = 0
    while index < len(arguments):
        token = arguments[index]
        value = token.value
        index += 1
        if token.dynamic:
            return None
        if options and value == '--':
            options = False
        elif options and value in value_options.get(command, set()):
            if index >= len(arguments) or arguments[index].dynamic:
                return None
            index += 1
        elif options and value.startswith('-') and value != '-':
            if value.startswith('--') or any(flag not in short_flags[command] for flag in value[1:]):
                return None
        else:
            paths.append(token)
    return paths


def _command_position(tokens: list[_Token]) -> bool:
    words = []
    redirect = False
    for token in tokens:
        if token.operator:
            if token.value in _REDIRECT:
                redirect = True
                if words and words[-1].value.isdigit() and words[-1].end == token.start:
                    words.pop()
            else:
                words = []
                redirect = False
        elif redirect:
            redirect = False
        else:
            words.append(token)
    return all(not word.quoted and (
        word.value in _COMMAND_PREFIXES or _ASSIGNMENT.match(word.value)) for word in words)


def _cwd_change(words: list[_Token]) -> bool:
    """Whether the command reached after shell wrappers can change the cwd.

    Skips assignment prefixes, ``command``/``builtin``/``exec`` with their own
    option terminators, a repeated ``time`` keyword and its options; the first
    remaining word decides by its literal name.  ``command -v``/``-V`` only
    prints how a name resolves and runs nothing.  This stays a bounded lexical
    scan, not a shell parser: dynamic command words and arbitrary programs keep
    the existing documented detector limitation.
    """
    index = 0
    while index < len(words):
        token = words[index]
        if token.operator:
            return False
        value = token.value
        if value == 'command':
            index += 1
            while index < len(words) and not words[index].operator:
                option = words[index].value
                if not option.startswith('-') or option == '-':
                    break
                index += 1
                if option == '--':
                    break
                if any(flag in 'vV' for flag in option[1:]):
                    return False
            continue
        if value in ('builtin', 'exec'):
            index += 1
            if (index < len(words) and not words[index].operator
                    and words[index].value == '--'):
                index += 1
            continue
        if value in _CWD_WRAPPERS or value in _COMMAND_PREFIXES:
            index += 1
            continue
        if value.startswith('-') and value != '-':
            index += 1
            continue
        if _ASSIGNMENT.match(value):
            index += 1
            continue
        return value.rsplit('/', 1)[-1] in _CWD_BUILTINS | _CWD_EVALUATORS
    return False


def _command(words: list[_Token]) -> _Result:
    result = _Result()
    if _cwd_change(words):
        result.cwd = True
    while words:
        if words[0].value in _COMMAND_PREFIXES and not words[0].quoted:
            result.unknown = True
        elif words[0].value == 'command':
            words = words[1:]
            while words and words[0].value.startswith('-'):
                option = words[0]
                words = words[1:]
                if option.dynamic:
                    result.write([])
                    return result
                if option.value == '--':
                    break
                if len(option.value) < 2 or any(flag not in 'pvV' for flag in option.value[1:]):
                    return result
                if 'v' in option.value or 'V' in option.value:
                    return result
            continue
        elif not (_ASSIGNMENT.match(words[0].value) or words[0].value in ('builtin', 'exec')):
            break
        words = words[1:]
    if not words:
        return result
    name = words[0].value.rsplit('/', 1)[-1]
    args = words[1:]
    if name in _CWD_BUILTINS:
        result.unknown = True
        result.cwd = True
    elif name == 'patch' or (name == 'git' and args and args[0].value == 'apply'):
        result.write([])
    elif name == 'sed':
        if any(arg.value == '--in-place' or arg.value.startswith('--in-place=') or
               (arg.value.startswith('-') and not arg.value.startswith('--') and 'i' in arg.value[1:])
               for arg in args):
            result.write([])
    elif name in ('rm', 'mv', 'cp', 'touch', 'truncate', 'tee'):
        paths = _operands(name, args)
        if paths is None:
            result.write([])
        elif name == 'cp':
            result.write(paths[-1:] if len(paths) >= 2 else [])
        elif name == 'mv':
            result.write(paths if len(paths) >= 2 else [])
        elif paths:
            result.write(paths)
    return result


def _classify(tokens: list[_Token]) -> _Result:
    result = _Result()
    words = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        index += 1
        if not token.operator:
            words.append(token)
            continue
        if token.value not in _REDIRECT:
            result.merge(_command(words))
            words = []
            continue
        if words and words[-1].value.isdigit() and not words[-1].quoted and words[-1].end == token.start:
            words.pop()
        target = tokens[index] if index < len(tokens) and not tokens[index].operator else None
        if target is not None:
            index += 1
        if token.value in _OUTPUT:
            if token.value == '>&' and target and not target.dynamic and re.fullmatch(r'(?:[0-9]+-?|-)', target.value):
                continue
            result.write([target] if target is not None else [])
    result.merge(_command(words))
    return result


def classify_shell_mutation(command: str) -> tuple[bool, list[str]]:
    """Return recognized mutation status and all reliable literal write paths.

    An empty scope for a mutation is deliberately conservative: callers must not
    authorize just the known subset when another write uses expansions, complex
    options, a patch, or a changed working directory.
    """
    result = _Lexer(command).result()
    return result.mutation, [] if result.unknown else list(dict.fromkeys(result.paths))


def _cd_target_provable(value: str) -> bool:
    """True only for a plain literal absolute directory with no ``..`` component.

    Relative targets depend on the inherited ``PWD``/``CDPATH`` and can silently
    resolve into another directory; a ``..`` component is applied lexically by
    Bash but physically by ``Path.resolve``, so a symlink in the prefix can make
    the two disagree.  Quoted and dynamic targets are rejected by the caller.
    """
    if not value.startswith('/'):
        return False
    return '..' not in value.split('/')


def _and_only_guarded(tokens: list[_Token]) -> bool:
    """True when control flow after the ``cd`` prefix stays AND/redirect-only."""
    return all(token.value == '&&' or token.value in _REDIRECT
               for token in tokens if token.operator)


def _split_leading_cd(tokens: list[_Token]) -> tuple[str | None, list[_Token], bool]:
    """Split one provable leading literal ``cd <absolute-dir> &&`` prefix.

    Returns ``(prefix, remaining, ambiguous)``.  ``prefix`` is the absolute
    literal target when a single option-free, unquoted, non-dynamic ``cd`` leads
    the command, names a path without ``..``, is followed by ``&&``, and every
    following command is joined by ``&&`` or redirection only.  Then a failed
    ``cd`` skips the whole guarded body instead of running it in the original
    working directory.  ``ambiguous`` marks any other *leading* ``cd`` shape so
    the caller keeps the whole mutation unscoped.  Repeated and nested cwd
    changes stay visible to normal classification and set :attr:`_Result.cwd`.
    """
    if len(tokens) < 2 or tokens[0].operator or tokens[0].quoted or tokens[0].value != 'cd':
        return None, tokens, False
    target = tokens[1]
    if (target.operator or target.dynamic or target.quoted or not target.value
            or not _cd_target_provable(target.value)):
        return None, tokens, True
    if len(tokens) < 3:
        return None, tokens, False
    separator = tokens[2]
    if (not separator.operator or separator.value != '&&'
            or not _and_only_guarded(tokens[3:])):
        return None, tokens, True
    return target.value, tokens[3:], False


def classify_shell_mutation_scoped(command: str) -> tuple[bool, list[str], bool]:
    """Return mutation status, reliable literal paths and cwd-uncertainty.

    At most one leading literal ``cd <absolute-dir> &&`` prefix is applied to the
    literal write paths.  The third value is true when the command was recognized
    as a mutation but changes the working directory in a way this bounded lexer
    cannot prove; callers must then treat the scope as unknown instead of
    authorizing a partial or wrong path list, so the path list is empty.
    """
    result = _Lexer(command, scoped=True).result()
    if result.mutation and result.cwd:
        return True, [], True
    return (result.mutation,
            [] if result.unknown else list(dict.fromkeys(result.paths)),
            False)
