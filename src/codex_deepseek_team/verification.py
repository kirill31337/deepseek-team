"""Pure, side-effect-free parsing of declared check commands.

Both the host-side readiness gate and the sandbox-side dependency probe need to
know which executable a declared check command starts with. Re-implementing that
per call site let assignment prefixes such as ``PYTHONPATH=src`` be mistaken for
the executable, so this module centralizes the resolution.

Skipping an assignment prefix needs more than the decoded word text: ``"A=1"``
and ``A\\=1`` both decode to the word ``A=1``, yet a POSIX shell treats them as
ordinary command words because the name or the ``=`` was quoted or escaped. The
lexer below therefore records, for every decoded character, whether it came from
a quoted or escaped region, and only an entirely unquoted ``NAME=`` prefix is
skipped.

The parser only tokenizes with POSIX quoting rules. It never evaluates a shell:
no command substitution, no variable expansion, no globbing, no I/O. Callers
keep running the original, unchanged command string.
"""
from __future__ import annotations

import re

_NAME = re.compile(r'[A-Za-z_][A-Za-z0-9_]*\Z')
_WHITESPACE = ' \t\r\n'
_ESCAPE = '\\'
_ESCAPABLE_IN_DOUBLE_QUOTES = '"\\'


class CheckCommandError(ValueError):
    """A declared check command could not yield a first executable token.

    ``reason`` is one of ``empty``, ``assignment-only`` or
    ``malformed-quoting`` so callers can map the rejection to their own surface.
    """

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


def _lex_words(command: str) -> list[tuple[str, tuple[bool, ...]]]:
    """Split a command into words, keeping the quoting of every character.

    Each result pairs the decoded word with a flag per decoded character:
    ``True`` marks a character that came from a quoted or escaped region of the
    source. ``"A=1"`` therefore decodes to ``('A=1', (True, True, True))``,
    ``A\\=1`` to ``('A=1', (False, True, False))`` and a real ``A=1`` assignment
    to ``('A=1', (False, False, False))``.

    An empty quoted region marks the next decoded character in the same word,
    retaining the assignment-disqualifying boundary in forms such as ``A""=1``.

    Word splitting mirrors POSIX ``shlex`` rules: words separate on unquoted
    whitespace, single quotes are literal, a double-quoted backslash only
    escapes ``"`` and ``\\``, and a backslash outside quotes escapes the next
    character literally. Unbalanced quoting raises ``ValueError``.
    """
    words: list[tuple[str, tuple[bool, ...]]] = []
    characters: list[str] = []
    quoted: list[bool] = []
    started = False
    quoted_boundary = False

    def flush() -> None:
        nonlocal characters, quoted, started, quoted_boundary
        if started:
            words.append((''.join(characters), tuple(quoted)))
        characters = []
        quoted = []
        started = False
        quoted_boundary = False

    index = 0
    length = len(command)
    while index < length:
        character = command[index]
        if character in _WHITESPACE:
            flush()
            index += 1
        elif character in '"\'':
            started = True
            quoted_boundary = True
            closing = character
            index += 1
            while index < length and command[index] != closing:
                escaped = closing == '"' and command[index] == _ESCAPE
                if escaped and index + 1 < length:
                    following = command[index + 1]
                    if following in _ESCAPABLE_IN_DOUBLE_QUOTES:
                        characters.append(following)
                        quoted.append(True)
                        quoted_boundary = False
                        index += 2
                        continue
                characters.append(command[index])
                quoted.append(True)
                quoted_boundary = False
                index += 1
            if index >= length:
                raise ValueError('No closing quotation')
            index += 1
        elif character == _ESCAPE:
            if index + 1 >= length:
                raise ValueError('No escaped character')
            characters.append(command[index + 1])
            quoted.append(True)
            quoted_boundary = False
            started = True
            index += 2
        else:
            characters.append(character)
            quoted.append(quoted_boundary)
            quoted_boundary = False
            started = True
            index += 1
    flush()
    return words


def _is_assignment_prefix(value: str, quoted: tuple[bool, ...]) -> bool:
    """Report whether a word is an entirely unquoted, shell-valid ``NAME=`` word.

    A quoted or escaped character anywhere in the name, or on the ``=`` itself,
    makes the word an ordinary command word for a POSIX shell. The value after
    the ``=`` may be quoted, escaped or empty and is not inspected.
    """
    for index, character in enumerate(value):
        if quoted[index]:
            return False
        if character == '=':
            return bool(_NAME.match(value[:index]))
    return False


def first_executable(command: str) -> str:
    """Return the executable token a declared check command starts with.

    Leading ``NAME=value`` assignments are skipped, including several in a row
    and values that use POSIX quoting (``LANG='en US' python3 app.py``) or that
    are empty (``A= python3 app.py``). A word whose name or ``=`` was quoted or
    escaped (``"A=1"``, ``A\\=1``, ``'A'=1``, ``A"="1``) is a command word for a
    shell and is returned instead of being skipped.

    Only the command's own quoting is interpreted; the shell is never invoked,
    so ``$VAR``, ``$(...)`` and backticks stay literal and produce no side
    effects.

    Raises :class:`CheckCommandError` for an empty command, a command made only
    of assignments, a non-string command, an empty quoted word, or unbalanced
    quoting.
    """
    if not isinstance(command, str):
        raise CheckCommandError('malformed-quoting', 'declared check command must be a string')
    try:
        words = _lex_words(command)
    except ValueError as error:
        raise CheckCommandError('malformed-quoting',
                                'malformed quoting in declared check command') from error
    for value, quoted in words:
        if not value:
            raise CheckCommandError('empty', 'declared check command has an empty executable')
        if _is_assignment_prefix(value, quoted):
            continue
        return value
    raise CheckCommandError('empty' if not words else 'assignment-only',
                            'declared check command has no executable')
