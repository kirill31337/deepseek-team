"""Regressions for shared declared-check parsing and dependency probing.

Both readiness stages (host preflight and the in-sandbox dependency probe) must
resolve the same first executable from a declared check command, skip leading
NAME=value assignment prefixes without evaluating a shell, and probe
repository-relative paths with valid POSIX ``test`` syntax.
"""
from __future__ import annotations

import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from codex_deepseek_team import coordination, development, sandbox, settings, verification, workspace


class FirstExecutableParserTests(unittest.TestCase):
    def test_plain_command_returns_its_executable(self):
        self.assertEqual(verification.first_executable('python3 -m unittest'), 'python3')

    def test_single_assignment_prefix_is_skipped(self):
        self.assertEqual(verification.first_executable('PYTHONPATH=src python3 -m unittest'),
                         'python3')

    def test_multiple_assignment_prefixes_are_skipped(self):
        self.assertEqual(
            verification.first_executable('A=1 B=2 C=3 /usr/bin/python3 script.py'),
            '/usr/bin/python3')

    def test_quoted_assignment_values_with_spaces_are_skipped(self):
        for command in ('LANG="en US" python3 app.py',
                        "LANG='en US' OTHER=x python3 app.py",
                        'GREETING="hello world" python3 app.py'):
            with self.subTest(command=command):
                self.assertEqual(verification.first_executable(command), 'python3')

    def test_relative_executable_with_assignment_prefix_is_kept_verbatim(self):
        self.assertEqual(verification.first_executable('PYTHONPATH=src ./scripts/run.sh -v'),
                         './scripts/run.sh')

    def test_shell_syntax_in_value_stays_literal(self):
        self.assertEqual(verification.first_executable('GREETING=$HOME python3 app.py'),
                         'python3')
        self.assertEqual(verification.first_executable('WORK=$(pwd) python3 app.py'),
                         'python3')

    def test_parser_never_executes_command_substitution(self):
        directory = tempfile.mkdtemp(prefix='dst-verify-subst-')
        self.addCleanup(shutil.rmtree, directory, ignore_errors=True)
        marker = Path(directory) / 'substituted'
        command = 'TOKEN=`touch %s` true' % shlex.quote(str(marker))
        # A real shell runs the substitution: prove the fixture is meaningful.
        subprocess.run(['/bin/sh', '-c', command], check=False)
        self.assertTrue(marker.exists(), 'fixture did not exercise shell substitution')
        marker.unlink()
        parsed = verification.first_executable(command)
        self.assertIsInstance(parsed, str)
        self.assertFalse(marker.exists(), 'pure parser executed a command substitution')

    def test_empty_and_whitespace_commands_are_rejected(self):
        for command in ('', '   ', '\t\n'):
            with self.subTest(command=command), self.assertRaises(verification.CheckCommandError):
                verification.first_executable(command)

    def test_assignment_only_commands_are_rejected(self):
        for command in ('PYTHONPATH=src', 'A=1 B=2', 'LANG="en US"'):
            with self.subTest(command=command), self.assertRaises(verification.CheckCommandError):
                verification.first_executable(command)

    def test_empty_quoted_executable_is_rejected(self):
        for command in ('"" python3 -c pass', "A=1 '' python3 -c pass"):
            with self.subTest(command=command), self.assertRaises(verification.CheckCommandError):
                verification.first_executable(command)

    def test_malformed_quoting_is_rejected(self):
        for command in ('python3 "unterminated', "python3 'unterminated"):
            with self.subTest(command=command), self.assertRaises(verification.CheckCommandError):
                verification.first_executable(command)


class QuotedAssignmentWordTests(unittest.TestCase):
    """Quoted or escaped ``NAME=`` words are commands, not assignment prefixes.

    A POSIX shell decides assignment status before expansion: quoting or escaping
    the name or the ``=`` makes the word an ordinary command word. Decoding a
    command with ``shlex`` alone loses that distinction, because ``"A=1"`` and
    ``A\\=1`` both decode to the word ``A=1``.
    """

    def test_quoted_assignment_word_is_the_executable(self):
        self.assertEqual(verification.first_executable('"A=1" python3 -c pass'), 'A=1')

    def test_empty_quotes_in_name_disqualify_assignment(self):
        for command, expected in (('A""=1 true', 'A=1'), ("''A=1 true", 'A=1'),
                                  ("A''B=1 true", 'AB=1'), ('A""""=1 true', 'A=1')):
            with self.subTest(command=command):
                self.assertEqual(verification.first_executable(command), expected)

    def test_empty_quotes_in_value_do_not_taint_next_word(self):
        for command in ('A="" B=2 true', 'A=x"" B=2 true', 'A=""x B=2 true'):
            with self.subTest(command=command):
                self.assertEqual(verification.first_executable(command), 'true')

    def test_escaped_equals_word_is_the_executable(self):
        self.assertEqual(verification.first_executable(r'A\=1 python3 -c pass'), 'A=1')

    def test_quoted_or_escaped_name_is_not_a_skipped_prefix(self):
        for command, expected in (("'A'=1 python3 -c pass", 'A=1'),
                                  ('"A"=1 python3 -c pass', 'A=1'),
                                  (r'\A=1 python3 -c pass', 'A=1'),
                                  (r'A\ \=1 python3 -c pass', 'A =1'),
                                  (r'PYTHONPATH\=src python3 -c pass', 'PYTHONPATH=src')):
            with self.subTest(command=command):
                self.assertEqual(verification.first_executable(command), expected)

    def test_quoted_word_after_a_real_assignment_prefix_is_the_executable(self):
        for command, expected in (('A=1 "B=2" true', 'B=2'),
                                  ("A=1 'B=2' true", 'B=2'),
                                  ('PYTHONPATH=src "X=1"', 'X=1'),
                                  (r'C=3 X\=1 true', 'X=1')):
            with self.subTest(command=command):
                self.assertEqual(verification.first_executable(command), expected)

    def test_assignment_looking_single_word_is_returned_verbatim(self):
        self.assertEqual(verification.first_executable('"PYTHONPATH=src"'), 'PYTHONPATH=src')
        self.assertEqual(verification.first_executable(r'PYTHONPATH\=src'), 'PYTHONPATH=src')

    def test_unquoted_prefix_with_quoted_or_empty_value_stays_skipped(self):
        for command in ('A= python3 -c pass', 'A="" python3 -c pass', "A='' python3 -c pass",
                        'A="1" B=2 python3 -c pass', r'A=1\ 2 python3 -c pass',
                        'LANG="en US" python3 app.py', 'A="x=y" python3 app.py'):
            with self.subTest(command=command):
                self.assertEqual(verification.first_executable(command), 'python3')

    def test_quoted_assignment_word_is_not_evaluated(self):
        directory = tempfile.mkdtemp(prefix='dst-verify-quoted-')
        self.addCleanup(shutil.rmtree, directory, ignore_errors=True)
        marker = Path(directory) / 'substituted'
        command = '"A=`touch %s`" true' % shlex.quote(str(marker))
        self.assertEqual(verification.first_executable(command),
                         'A=`touch %s`' % shlex.quote(str(marker)))
        self.assertFalse(marker.exists(), 'pure parser executed a command substitution')


class WordLexerParityTests(unittest.TestCase):
    """The quote-aware lexer must split words exactly like POSIX ``shlex``.

    The parser no longer decodes words with ``shlex.split``, because that drops
    the quote and escape information needed to recognize assignment prefixes.
    Word values must still match, otherwise the executable used for probing
    would drift from the word the shell actually runs.
    """

    CORPUS = (
        'python3 -m unittest',
        'PYTHONPATH=src python3 -m unittest',
        'A=1 B=2 C=3 /usr/bin/python3 script.py',
        'LANG="en US" python3 app.py',
        "LANG='en US' OTHER=x python3 app.py",
        'PYTHONPATH=src ./scripts/run.sh -v',
        'GREETING=$HOME python3 app.py',
        'WORK=$(pwd) python3 app.py',
        'TOKEN=`touch /tmp/x` true',
        '"A=1" python3 -c pass',
        "'A'=1 python3 -c pass",
        r'A\=1 python3 -c pass',
        r'\A=1 python3 -c pass',
        r'PYTHONPATH\=src python3 -c pass',
        'A= python3 -c pass',
        'A="" python3 -c pass',
        "A='' python3 -c pass",
        'A="x=y" python3 app.py',
        r'A=1\ 2 python3 -c pass',
        r'"a\\b" c',
        r'"a\qb" c',
        r"'a\nb' c",
        'a""b c',
        '"" python3',
        "'' python3",
        '  spaced   words  ',
        'x\ty z',
        'cmd\t-a\r\n',
        'A=1 "B=2" true',
    )

    def test_decoded_words_match_shlex_splitting(self):
        for command in self.CORPUS:
            with self.subTest(command=command):
                self.assertEqual([word for word, _ in verification._lex_words(command)],
                                 shlex.split(command))

    def test_quote_flags_mark_only_the_quoted_characters(self):
        self.assertEqual(verification._lex_words('A=1 "B=2" C\\=3'),
                         [('A=1', (False, False, False)),
                          ('B=2', (True, True, True)),
                          ('C=3', (False, True, False))])

    def test_unbalanced_quoting_raises_like_shlex(self):
        for command in ('python3 "unterminated', "python3 'unterminated",
                        "python3 \"trailing\\", "python3 trailing\\"):
            with self.subTest(command=command):
                with self.assertRaises(ValueError):
                    shlex.split(command)
                with self.assertRaises(ValueError):
                    verification._lex_words(command)


class HostReadinessTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='dst-verify-host-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo = self.root / 'repo'
        self.repo.mkdir()
        subprocess.run(['git', 'init', '-q', str(self.repo)], check=True)
        (self.repo / 'a.py').write_text('VALUE = 1\n')
        subprocess.run(['git', '-C', str(self.repo), 'add', '.'], check=True)
        subprocess.run(['git', '-C', str(self.repo), '-c', 'user.name=Test', '-c',
                        'user.email=test@example.test', 'commit', '-qm', 'base'], check=True)
        self.state = self.root / 'state'
        (self.root / 'home').mkdir()
        self.env = mock.patch.dict(os.environ, {
            'DEEPSEEK_TEAM_STATE_DIR': str(self.state),
            'XDG_CONFIG_HOME': str(self.root / 'config'),
            'HOME': str(self.root / 'home'),
        })
        self.env.start()
        self.addCleanup(self.env.stop)
        settings.set_values(self.repo / settings.PROJECT_FILE,
                            delegation_level=75, access='full-access')

    def _assignment(self, checks):
        policy = settings.Policy(75, 'full-access',
                                 {'delegation_level': 'test', 'access': 'test'})
        task = coordination.open_task(self.repo, session_id='s', turn_id='t',
                                      prompt='implement', policy=policy)
        plan = coordination.plan_task(self.repo, task['id'], {
            'classification': 'substantial',
            'deliverables': [{
                'id': 'd', 'kind': 'implementation', 'scope': ['a.py'],
                'executor': 'worker', 'acceptance': ['ok'],
                'dependencies': [], 'checks': checks,
            }],
        })
        copy = workspace.create(self.repo, self.state)
        return task['id'], plan['assignments'][0]['id'], copy

    def test_host_readiness_accepts_assignment_prefix(self):
        task_id, assignment_id, copy = self._assignment(
            ['PYTHONPATH=src python3 -c pass'])
        item = coordination.ensure_assignment_ready(self.repo, task_id, assignment_id, copy)
        self.assertEqual(item['id'], 'd')

    def test_host_readiness_labels_missing_executable_after_prefix(self):
        task_id, assignment_id, copy = self._assignment(
            ['PYTHONPATH=src definitely-missing-tool-xyz arg'])
        with self.assertRaises(coordination.CoordinationError) as caught:
            coordination.ensure_assignment_ready(self.repo, task_id, assignment_id, copy)
        self.assertIn('check-command:definitely-missing-tool-xyz', str(caught.exception))

    def test_host_readiness_rejects_malformed_quoting(self):
        task_id, assignment_id, copy = self._assignment(['python3 "unterminated'])
        with self.assertRaises(coordination.CoordinationError) as caught:
            coordination.ensure_assignment_ready(self.repo, task_id, assignment_id, copy)
        self.assertEqual(caught.exception.code, 64)

    def test_host_readiness_rejects_assignment_only_and_empty(self):
        for command in ('PYTHONPATH=src', '', '"" python3 -c pass', "A=1 '' python3 -c pass"):
            with self.subTest(command=command):
                task_id, assignment_id, copy = self._assignment([command])
                with self.assertRaises(coordination.CoordinationError) as caught:
                    coordination.ensure_assignment_ready(self.repo, task_id, assignment_id, copy)
                self.assertEqual(caught.exception.code, 64)


class SandboxDependencyProbeTests(unittest.TestCase):
    """Exercise the real /bin/sh probe inside a bwrap namespace when available."""

    def setUp(self):
        required = os.environ.get('DEEPSEEK_TEAM_REQUIRE_LIVE') == '1'
        try:
            self.backend = sandbox.probe_backend()
        except sandbox.SandboxError as error:
            if required:
                self.fail('Required sandbox unavailable: ' + str(error))
            self.skipTest('Sandbox unavailable on this host: ' + str(error))
        self.tmp = tempfile.TemporaryDirectory(prefix='dst-verify-sandbox-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.work = self.root / 'work'
        self.work.mkdir()
        (self.work / '.git').mkdir()
        self.home = self.root / 'home'
        self.home.mkdir()
        self.control = self.root / 'control'
        self.control.mkdir()
        self.args = development.layout(self.backend, self.work, self.home, self.control,
                                       [sys.executable], writable=True)
        self.env = {'PATH': '/usr/local/bin:/usr/bin:/bin', 'LANG': 'C.UTF-8'}
        development.probe(self.args, self.env)

    def probe(self, item):
        return development.missing_requirements(self.args, self.env, item)

    def test_existing_relative_path_is_present(self):
        (self.work / 'present.txt').write_text('x\n')
        self.assertEqual(self.probe({'dependencies': [{'kind': 'path', 'value': 'present.txt'}],
                                     'checks': []}), [])

    def test_paths_with_spaces_and_leading_dash_are_present(self):
        (self.work / 'my file.txt').write_text('x\n')
        (self.work / '-dash.txt').write_text('x\n')
        self.assertEqual(self.probe({'dependencies': [
            {'kind': 'path', 'value': 'my file.txt'},
            {'kind': 'path', 'value': '-dash.txt'}], 'checks': []}), [])

    def test_missing_relative_path_is_labeled(self):
        self.assertEqual(self.probe({'dependencies': [{'kind': 'path', 'value': 'nope.txt'}],
                                     'checks': []}), ['path:nope.txt'])

    def test_absolute_and_parent_paths_are_rejected(self):
        self.assertEqual(self.probe({'dependencies': [
            {'kind': 'path', 'value': '/etc/passwd'},
            {'kind': 'path', 'value': '../outside'}], 'checks': []}),
            ['path:../outside', 'path:/etc/passwd'])

    def test_sandbox_probe_accepts_assignment_prefix(self):
        self.assertEqual(self.probe({'dependencies': [], 'checks': ['PYTHONPATH=src python3 -c pass']}), [])

    def test_sandbox_probe_labels_missing_executable_after_prefix(self):
        self.assertEqual(self.probe({'dependencies': [],
                                     'checks': ['PYTHONPATH=src definitely-missing-tool-xyz arg']}),
                         ['check-command:definitely-missing-tool-xyz'])

    def test_sandbox_probe_rejects_malformed_and_assignment_only(self):
        self.assertEqual(self.probe({'dependencies': [], 'checks': ['python3 "unterminated']}),
                         ['check-command:invalid'])
        self.assertEqual(self.probe({'dependencies': [], 'checks': ['PYTHONPATH=src']}),
                         ['check-command:empty'])
        self.assertEqual(self.probe({'dependencies': [], 'checks': ['A=1 "" python3 -c pass']}),
                         ['check-command:empty'])


class ShellScriptProbeTests(unittest.TestCase):
    """Exercise the exact probe scripts against the real POSIX shell.

    The bwrap-gated :class:`SandboxDependencyProbeTests` is authoritative but is
    skipped where nested user namespaces are unavailable. This class still runs
    the same generated scripts through ``/bin/sh`` (dash), so path quoting and
    parser integration stay covered even without a namespace.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='dst-verify-sh-')
        self.addCleanup(self.tmp.cleanup)
        self.work = Path(self.tmp.name)
        self._cwd = os.getcwd()
        os.chdir(self.work)
        self.addCleanup(os.chdir, self._cwd)
        self.args = ['/usr/bin/env']
        self.env = {'PATH': os.environ.get('PATH', '/usr/bin:/bin')}

    def probe(self, item):
        return development.missing_requirements(self.args, self.env, item)

    def test_existing_paths_including_spaces_and_leading_dash(self):
        (self.work / 'present.txt').write_text('x\n')
        (self.work / 'my file.txt').write_text('x\n')
        (self.work / '-dash.txt').write_text('x\n')
        self.assertEqual(self.probe({'dependencies': [
            {'kind': 'path', 'value': 'present.txt'},
            {'kind': 'path', 'value': 'my file.txt'},
            {'kind': 'path', 'value': '-dash.txt'}], 'checks': []}), [])

    def test_missing_path_is_labeled(self):
        self.assertEqual(self.probe({'dependencies': [{'kind': 'path', 'value': 'nope.txt'}],
                                     'checks': []}), ['path:nope.txt'])

    def test_absolute_and_parent_paths_are_rejected(self):
        self.assertEqual(self.probe({'dependencies': [
            {'kind': 'path', 'value': '/etc/passwd'},
            {'kind': 'path', 'value': '../outside'}], 'checks': []}),
            ['path:../outside', 'path:/etc/passwd'])

    def test_assignment_prefix_check_command_is_available(self):
        self.assertEqual(self.probe({'dependencies': [], 'checks': ['PYTHONPATH=src python3 -c pass']}), [])

    def test_missing_executable_after_prefix_is_labeled(self):
        self.assertEqual(self.probe({'dependencies': [],
                                     'checks': ['PYTHONPATH=src definitely-missing-tool-xyz arg']}),
                         ['check-command:definitely-missing-tool-xyz'])

    def test_quoted_assignment_word_is_probed_as_the_executable(self):
        self.assertEqual(self.probe({'dependencies': [],
                                     'checks': ['"PYTHONPATH=src" python3 -c pass']}),
                         ['check-command:PYTHONPATH=src'])
        self.assertEqual(self.probe({'dependencies': [],
                                     'checks': [r'PYTHONPATH\=src python3 -c pass']}),
                         ['check-command:PYTHONPATH=src'])

    def test_malformed_and_assignment_only_checks_are_rejected(self):
        self.assertEqual(self.probe({'dependencies': [], 'checks': ['python3 "unterminated']}),
                         ['check-command:invalid'])
        self.assertEqual(self.probe({'dependencies': [], 'checks': ['PYTHONPATH=src']}),
                         ['check-command:empty'])


if __name__ == '__main__':
    unittest.main()
