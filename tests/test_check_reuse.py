"""Deterministic check reuse is conservative, locked and evidence-backed.

Every counter below lives outside the owned working copy, so executing a check
is observable without changing the project snapshot that is being cached.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest import mock

from codex_deepseek_team import (check_reuse, coordination, delegation_cli,
                                 development, managed, relay, runtime_preflight,
                                 settings, toolchains, worker, workspace)
from tests import test_coordination as fixtures

LAUNCHER = shutil.which('env') or '/usr/bin/env'


def private_python(root: Path, name: str, *, mode: int = 0o700) -> Path:
    """Return an independently copied interpreter owned by this fixture."""
    directory = Path(root) / name
    directory.mkdir()
    directory.chmod(0o700)
    executable = directory / 'python'
    shutil.copyfile(sys.executable, executable)
    executable.chmod(mode)
    info = os.lstat(executable)
    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
            or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) != mode):
        raise RuntimeError('fixture interpreter is not an isolated private copy')
    return executable


class CheckReuseCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='dst-check-reuse-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo = self.root / 'repo'
        self.repo.mkdir()
        subprocess.run(['git', 'init', '-q', str(self.repo)], check=True)
        (self.repo / 'a.py').write_text('VALUE = 1\n')
        (self.repo / '.gitignore').write_text('ignored.txt\n')
        subprocess.run(['git', '-C', str(self.repo), 'add', '.'], check=True)
        subprocess.run(['git', '-C', str(self.repo), '-c', 'user.name=Test', '-c',
                        'user.email=test@example.test', 'commit', '-qm', 'base'], check=True)
        self.state = self.root / 'state'
        (self.root / 'home').mkdir()
        self.env_patch = mock.patch.dict(os.environ, {
            'DEEPSEEK_TEAM_STATE_DIR': str(self.state),
            'XDG_CONFIG_HOME': str(self.root / 'config'),
            'HOME': str(self.root / 'home'),
        })
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)
        settings.set_values(self.repo / settings.PROJECT_FILE,
                            delegation_level=75, access='full-access')
        self.copy = workspace.create(self.repo, self.state)
        self.python = private_python(self.root, 'fixture-python')
        self.counter = self.root / 'counter'
        self.env = {
            'PATH': os.environ.get('PATH', '/usr/local/bin:/usr/bin:/bin'),
            'LANG': 'C.UTF-8',
            'HOME': str(self.root / 'runtime-home'),
            'PYTHONHOME': sys.base_prefix,
        }

    def counter_code(self, *, counter=None, extra=''):
        counter = self.counter if counter is None else counter
        return '\n'.join((
            'import os', 'from pathlib import Path',
            f'os.chdir({str(self.copy.path)!r})',
            f'p = Path({str(counter)!r})',
            "p.write_text(str(int(p.read_text()) + 1) if p.exists() else '1')",
            extra,
        ))

    def command(self, *, counter=None, extra='', suffix=''):
        code = self.counter_code(counter=counter, extra=extra)
        return (f'{shlex.quote(str(self.python))} -c {shlex.quote(code)}' + suffix)

    def count(self, path=None):
        path = self.counter if path is None else path
        return int(path.read_text()) if path.exists() else 0

    def run_checks(self, *, reusable=True, force=False, commands=None,
                   timeout=120, env=None, phase='post', directory=None,
                   layout=None, redact_values=(), deadline=None):
        with self.copy.lock():
            return check_reuse.run_checks(
                self.copy, [LAUNCHER] if layout is None else layout, env or self.env,
                commands if commands is not None else [self.command()],
                timeout=timeout,
                deadline=deadline,
                directory=directory or self.copy.directory / 'diagnostics',
                phase=phase, reusable=reusable, force=force,
                redact_values=redact_values)

    def evidence_directory(self):
        return self.copy.directory / 'diagnostics'

    def cache_entries(self):
        return (self.copy.metadata.get('check_reuse') or {}).get('entries', [])


class PositiveReuseTests(CheckReuseCase):
    def test_changed_redactions_execute_before_publishing_new_evidence(self):
        value = 'newly-sensitive-output-value'
        (self.copy.path / 'message.txt').write_text(value)
        commands = [self.command(extra='print(Path("message.txt").read_text())')]
        first = self.run_checks(commands=commands)
        original = self.evidence_directory() / first[0]['stdout_path']
        self.assertIn(value, original.read_text())
        second = self.run_checks(commands=commands, redact_values=(value,))
        self.assertFalse(second[0]['reused'])
        self.assertNotIn(value, (self.evidence_directory() / second[0]['stdout_path']).read_text())
        self.assertIn(value, original.read_text())  # Historic evidence is immutable.
        third = self.run_checks(commands=commands, redact_values=(value,))
        self.assertTrue(third[0]['reused'])
        self.assertEqual(self.count(), 2)
        self.assertNotIn(value, json.dumps(self.copy.metadata))

    def test_new_redactions_also_protect_cached_stderr(self):
        value = 'newly-sensitive-stderr-value'
        (self.copy.path / 'message.txt').write_text(value)
        commands = [self.command(extra=(
            'import sys; print(Path("message.txt").read_text(), file=sys.stderr)'))]
        self.run_checks(commands=commands)
        rows = self.run_checks(commands=commands, redact_values=(value,))
        self.assertFalse(rows[0]['reused'])
        self.assertNotIn(value, (self.evidence_directory() / rows[0]['stderr_path']).read_text())
        self.assertEqual(self.count(), 2)

    def test_safe_evidence_reuses_with_reordered_redactions(self):
        self.run_checks(redact_values=('policy-value-a', 'policy-value-b'))
        second = self.run_checks(redact_values=(
            'policy-value-b', 'policy-value-a', 'policy-value-b'))
        self.assertTrue(second[0]['reused'])
        self.assertEqual(self.count(), 1)

    def test_provider_rotation_reuses_output_that_is_already_safe(self):
        commands = [self.command(extra='print("safe output")')]
        first_env = dict(self.env, DEEPSEEK_API_KEY='provider-value-one')
        second_env = dict(self.env, DEEPSEEK_API_KEY='provider-value-two')
        self.run_checks(commands=commands, env=first_env)
        rows = self.run_checks(commands=commands, env=second_env)
        self.assertTrue(rows[0]['reused'])
        self.assertNotIn('provider-value-two',
                         (self.evidence_directory() / rows[0]['stdout_path']).read_text())
        self.assertEqual(self.count(), 1)

    def test_unchanged_inputs_hit_and_name_the_original_evidence(self):
        first = self.run_checks(phase='baseline')
        second = self.run_checks(phase='post')
        self.assertFalse(first[0]['reused'])
        self.assertTrue(second[0]['reused'])
        self.assertTrue(second[0]['reused_from'])
        self.assertEqual(second[0]['stdout_path'], first[0]['stdout_path'])
        self.assertEqual(second[0]['stdout_sha256'], first[0]['stdout_sha256'])
        self.assertEqual(self.count(), 1)
        self.assertEqual(len(self.cache_entries()), 1)
        self.assertTrue((self.evidence_directory() / first[0]['stdout_path']).is_file())

    def test_no_attestation_never_reuses_or_publishes(self):
        self.run_checks(reusable=False)
        rows = self.run_checks(reusable=False)
        self.assertEqual(self.count(), 2)
        self.assertFalse(rows[0]['reused'])
        self.assertNotIn('check_reuse', self.copy.metadata)

    def test_force_executes_and_refreshes_positive_evidence(self):
        self.run_checks(force=True)
        forced = self.run_checks(force=True)
        self.assertEqual(self.count(), 2)
        self.assertFalse(forced[0]['reused'])
        hit = self.run_checks()
        self.assertTrue(hit[0]['reused'])
        self.assertEqual(self.count(), 2)

    def test_runtime_home_tmp_normalization_keeps_the_same_redaction_policy(self):
        first_env = dict(self.env, HOME=str(self.root / 'home-a'),
                         CODEX_HOME=str(self.root / 'home-a' / 'codex'),
                         CLAUDE_CONFIG_DIR=str(self.root / 'home-a' / 'claude'),
                         TMPDIR=str(self.root / 'tmp-a'),
                         TEMP=str(self.root / 'tmp-a'),
                         TMP=str(self.root / 'tmp-a'),
                         XDG_RUNTIME_DIR=str(self.root / 'tmp-a'),
                         DEEPSEEK_API_KEY='canary-provider-key-a')
        second_env = dict(self.env, HOME=str(self.root / 'home-b'),
                          CODEX_HOME=str(self.root / 'home-b' / 'codex'),
                          CLAUDE_CONFIG_DIR=str(self.root / 'home-b' / 'claude'),
                          TMPDIR=str(self.root / 'tmp-b'),
                          TEMP=str(self.root / 'tmp-b'),
                          TMP=str(self.root / 'tmp-b'),
                          XDG_RUNTIME_DIR=str(self.root / 'tmp-b'),
                          DEEPSEEK_API_KEY='canary-provider-key-a')
        (self.root / 'tmp-a').mkdir()
        (self.root / 'tmp-b').mkdir()
        self.run_checks(env=first_env)
        rows = self.run_checks(env=second_env)
        self.assertTrue(rows[0]['reused'])
        self.assertEqual(self.count(), 1)
        stored = json.dumps(self.copy.metadata, sort_keys=True)
        self.assertNotIn('canary-provider-key-a', stored)
        self.assertNotIn(str(self.root / 'home-a'), stored)
        self.assertNotIn(str(self.root / 'tmp-a'), stored)

    def test_known_credential_environment_value_is_redacted_from_evidence(self):
        command = self.command(extra='print(os.environ.get("DEEPSEEK_API_KEY", ""))')
        env = dict(self.env, DEEPSEEK_API_KEY='canary-check-output-key')
        first = self.run_checks(commands=[command], env=env)
        evidence = (self.evidence_directory() / first[0]['stdout_path']).read_text()
        self.assertNotIn('canary-check-output-key', evidence)
        second = self.run_checks(commands=[command], env=env)
        self.assertTrue(second[0]['reused'])
        self.assertEqual(self.count(), 1)


class DeadlineTests(CheckReuseCase):
    def test_expired_observation_invalidates_the_previous_success(self):
        self.run_checks()
        expired = self.run_checks(deadline=time.monotonic() - 1)
        self.assertEqual(expired[0]['exit_code'], 124)
        next_run = self.run_checks()
        self.assertFalse(next_run[0]['reused'])
        self.assertEqual(self.count(), 2)

    def test_elapsed_deadline_never_uses_a_cached_success(self):
        first = self.run_checks()
        self.assertEqual(first[0]['exit_code'], 0)
        rows = self.run_checks(deadline=time.monotonic() - 1)
        self.assertEqual(rows[0]['exit_code'], 124)
        self.assertFalse(rows[0]['reused'])
        self.assertEqual(self.count(), 1)

    def test_deadline_expiring_during_cache_validation_is_a_miss(self):
        self.run_checks()
        calls = 0

        def clock():
            nonlocal calls
            calls += 1
            return 0.0 if calls <= 3 else 2.0

        with mock.patch.object(check_reuse.time, 'monotonic', side_effect=clock):
            rows = self.run_checks(deadline=1.0)
        self.assertEqual(rows[0]['exit_code'], 124)
        self.assertFalse(rows[0]['reused'])
        self.assertEqual(self.count(), 1)


class LedgerReuseEvidenceTests(CheckReuseCase):
    def test_terminal_reuse_validation_precedes_any_ledger_mutation(self):
        self.run_checks()
        valid = self.run_checks()[0]
        task = coordination.open_task(
            self.repo, session_id='terminal-reuse', turn_id='1', prompt='verify',
            policy=settings.Policy(75, 'full-access', {
                'access': 'test', 'delegation_level': 'test'}))
        planned = coordination.plan_task(self.repo, task['id'], {
            'classification': 'substantial', 'deliverables': [{
                'id': 'impl', 'kind': 'implementation', 'scope': ['a.py'],
                'executor': 'worker', 'acceptance': ['checked'],
                'dependencies': [], 'checks': [self.command()]}]})
        assignment = planned['assignments'][0]['id']
        coordination.assignment_started(self.repo, task['id'], assignment,
                                        self.copy.id, 'codex', [], effort='high')
        path = coordination._task_path(self.repo, task['id'])
        before = path.read_bytes()
        incomplete = {'command': 'ok', 'exit_code': 0, 'reused': True}
        for row in (incomplete, dict(valid, reused=1), dict(valid, reused=False),
                    dict(valid, reused_from='bad'), dict(valid, exit_code=1)):
            with self.subTest(row=row), self.assertRaises(coordination.CoordinationError):
                coordination.assignment_finished(self.repo, task['id'], assignment,
                                                 'succeeded', 'checked', [], [row])
            self.assertEqual(path.read_bytes(), before)
        saved = coordination.assignment_finished(
            self.repo, task['id'], assignment, 'succeeded', 'checked', [], [valid])
        self.assertEqual(saved['assignments'][0]['checks'], [valid])

    def test_real_ledger_retains_original_evidence_and_reuse_provenance(self):
        self.run_checks()
        rows = self.run_checks()
        task = coordination.open_task(
            self.repo, session_id='reuse-evidence', turn_id='1', prompt='verify',
            policy=settings.Policy(75, 'full-access', {
                'access': 'test', 'delegation_level': 'test'}))
        planned = coordination.plan_task(self.repo, task['id'], {
            'classification': 'substantial', 'deliverables': [{
                'id': 'impl', 'kind': 'implementation', 'scope': ['a.py'],
                'executor': 'worker', 'acceptance': ['checked'],
                'dependencies': [], 'checks': [self.command()]}]})
        assignment = planned['assignments'][0]['id']
        coordination.assignment_started(self.repo, task['id'], assignment,
                                        self.copy.id, 'codex', [], effort='high')
        coordination.assignment_finished(self.repo, task['id'], assignment,
                                         'succeeded', 'checked', [], [])
        run = {'id': 'reuse-evidence', 'workspace_id': self.copy.id,
               'status': 'passed', 'checks': rows, 'at': 1.0}
        coordination.record_verification(self.repo, task['id'], assignment, run)
        saved = coordination.load_task(self.repo, task['id'])
        self.assertEqual(saved['assignments'][0]['verification_runs'][0]['checks'], rows)
        self.assertTrue(rows[0]['reused'])
        self.assertEqual(self.count(), 1)

    def test_invalid_reuse_claims_are_rejected(self):
        self.run_checks()
        valid = self.run_checks()[0]
        variants = [dict(valid, reused=1), dict(valid, reused=None),
                    dict(valid, reused_from='bad'), dict(valid, reused=False),
                    dict(valid, exit_code=1), dict(valid, stdout_truncated=True)]
        missing = dict(valid)
        missing.pop('reused_from')
        variants.append(missing)
        for row in variants:
            with self.subTest(row=row), self.assertRaises(coordination.CoordinationError):
                coordination._validate_check_evidence([row])


class LayoutIdentityTests(CheckReuseCase):
    def layout_command(self):
        code = '\n'.join((
            'import sys', 'from pathlib import Path',
            f'p = Path({str(self.counter)!r})',
            "p.write_text(str(int(p.read_text()) + 1) if p.exists() else '1')",
            "sys.stdout.write(Path('input.txt').read_text())",
        ))
        return f'{shlex.quote(str(self.python))} -c {shlex.quote(code)}'

    def test_project_bind_layout_suffixes_stay_distinct(self):
        command = self.layout_command()
        for name in ('a', 'b'):
            directory = self.copy.path / name
            directory.mkdir()
            (directory / 'input.txt').write_text(name)
        first = self.run_checks(
            layout=[LAUNCHER, '-C', str(self.copy.path / 'a')], commands=[command])
        second = self.run_checks(
            layout=[LAUNCHER, '-C', str(self.copy.path / 'b')], commands=[command])
        self.assertEqual(first[0]['exit_code'], 0)
        self.assertEqual(second[0]['exit_code'], 0)
        self.assertFalse(second[0]['reused'])
        self.assertEqual(self.count(), 2)
        self.assertEqual(
            (self.evidence_directory() / second[0]['stdout_path']).read_text(), 'b')

    def test_descriptor_layout_targets_are_resolved_and_distinct(self):
        first = self.root / 'descriptor-a'
        second = self.root / 'descriptor-b'
        first.mkdir()
        second.mkdir()
        handles = []
        try:
            for directory in (first, second):
                handles.append(os.open(directory, os.O_RDONLY | os.O_DIRECTORY))
            one = check_reuse._normalized_layout(
                self.copy,
                [LAUNCHER, f'/proc/self/fd/{handles[0]}/child', 'tail'], self.env)
            two = check_reuse._normalized_layout(
                self.copy,
                [LAUNCHER, f'/proc/self/fd/{handles[1]}/child', 'tail'], self.env)
        finally:
            for handle in handles:
                os.close(handle)
        self.assertNotEqual(one, two)
        self.assertEqual(one[0], LAUNCHER)
        self.assertEqual(one[2], 'tail')
        self.assertEqual(two[2], 'tail')
        self.assertTrue(one[1].endswith(str(first.resolve()) + '/child'))
        self.assertTrue(two[1].endswith(str(second.resolve()) + '/child'))

    def test_unrelated_state_and_tmp_paths_are_not_collapsed(self):
        state_paths = [self.state / 'alpha', self.state / 'beta']
        tmp_paths = [Path(tempfile.gettempdir()) / 'dst-unrelated-alpha',
                     Path(tempfile.gettempdir()) / 'dst-unrelated-beta']
        normalized = [
            check_reuse._normalized_layout(
                self.copy, [LAUNCHER, '-C', str(path)], self.env)
            for path in [*state_paths, *tmp_paths]
        ]
        self.assertEqual(len({value[2] for value in normalized}), 4)


class InvalidationTests(CheckReuseCase):
    def assert_miss(self, *, commands=None, timeout=120, env=None):
        self.run_checks()
        rows = self.run_checks(commands=commands, timeout=timeout, env=env)
        self.assertFalse(rows[0]['reused'])
        self.assertEqual(self.count(), 2)

    def test_source_change_misses(self):
        self.run_checks()
        (self.copy.path / 'a.py').write_text('VALUE = 2\n')
        rows = self.run_checks()
        self.assertFalse(rows[0]['reused'])
        self.assertEqual(self.count(), 2)

    def test_ignored_untracked_change_misses(self):
        (self.copy.path / 'ignored.txt').write_text('one\n')
        self.run_checks()
        (self.copy.path / 'ignored.txt').write_text('two\n')
        rows = self.run_checks()
        self.assertFalse(rows[0]['reused'])
        self.assertEqual(self.count(), 2)

    def test_command_change_misses(self):
        self.assert_miss(commands=[self.command(suffix='\n# changed')])

    def test_timeout_change_misses(self):
        self.assert_miss(timeout=121)

    def test_environment_change_misses(self):
        self.assert_miss(env=dict(self.env, LANG='C'))

    def test_redaction_value_does_not_hide_other_environment_inputs(self):
        first_env = dict(self.env, CUSTOM_INPUT='alpha secret-canary')
        second_env = dict(self.env, CUSTOM_INPUT='beta secret-canary')
        self.run_checks(env=first_env, redact_values=('secret-canary',))
        rows = self.run_checks(env=second_env, redact_values=('secret-canary',))
        self.assertFalse(rows[0]['reused'])
        self.assertEqual(self.count(), 2)

    def test_access_policy_change_misses(self):
        self.run_checks()
        with self.copy.lock():
            self.copy.metadata['effective_access'] = 'read-only'
            self.copy.save()
        rows = self.run_checks()
        self.assertFalse(rows[0]['reused'])
        self.assertEqual(self.count(), 2)

    def test_external_tool_change_misses(self):
        bindir = self.root / 'bin'
        bindir.mkdir()
        tool = bindir / 'counter-tool'
        # A tiny POSIX launcher keeps the command name stable while its bytes change.
        tool.write_text('#!/bin/sh\nexec ' + shlex.quote(str(self.python)) + ' -c ' +
                        shlex.quote(self.counter_code()) + '\n')
        tool.chmod(0o700)
        env = dict(self.env, PATH=str(bindir) + os.pathsep + self.env['PATH'])
        self.run_checks(commands=['counter-tool'], env=env)
        with tool.open('a') as stream:
            stream.write('# changed\n')
        rows = self.run_checks(commands=['counter-tool'], env=env)
        self.assertFalse(rows[0]['reused'])
        self.assertEqual(self.count(), 2)

    def test_layout_executable_change_misses(self):
        launcher = self.root / 'layout-launcher'
        launcher.write_text('#!/bin/sh\nexec ' + shlex.quote(LAUNCHER) + ' "$@"\n')
        launcher.chmod(0o700)
        layout = [str(launcher)]
        self.run_checks(layout=layout)
        with launcher.open('a') as stream:
            stream.write('# changed\n')
        rows = self.run_checks(layout=layout)
        self.assertFalse(rows[0]['reused'])
        self.assertEqual(self.count(), 2)

    def test_prepared_input_change_misses(self):
        prepared = self.copy.path / 'local.properties'
        prepared.write_text('sdk=one\n')
        with self.copy.lock():
            self.copy.metadata['prepared_paths'] = ['local.properties']
            self.copy.save()
        self.run_checks()
        prepared.write_text('sdk=two\n')
        rows = self.run_checks()
        self.assertFalse(rows[0]['reused'])
        self.assertEqual(self.count(), 2)


class EvidenceFailureTests(CheckReuseCase):
    def test_missing_evidence_misses(self):
        first = self.run_checks()
        (self.evidence_directory() / first[0]['stdout_path']).unlink()
        rows = self.run_checks()
        self.assertFalse(rows[0]['reused'])
        self.assertEqual(self.count(), 2)

    def test_tampered_evidence_misses(self):
        first = self.run_checks()
        (self.evidence_directory() / first[0]['stdout_path']).write_text('tampered\n')
        rows = self.run_checks()
        self.assertFalse(rows[0]['reused'])
        self.assertEqual(self.count(), 2)

    def test_symlinked_evidence_misses(self):
        first = self.run_checks()
        target = self.root / 'outside-evidence'
        target.write_text('')
        evidence = self.evidence_directory() / first[0]['stdout_path']
        evidence.unlink()
        evidence.symlink_to(target)
        rows = self.run_checks()
        self.assertFalse(rows[0]['reused'])
        self.assertEqual(self.count(), 2)

    def test_unsafe_cached_evidence_path_misses(self):
        self.run_checks()
        with self.copy.lock():
            entry = self.copy.metadata['check_reuse']['entries'][0]
            entry['rows'][0]['stdout_path'] = '../outside'
            self.copy.save()
        rows = self.run_checks()
        self.assertFalse(rows[0]['reused'])
        self.assertEqual(self.count(), 2)

    def test_failed_command_is_not_published(self):
        commands = [self.command(suffix='; exit 7')]
        self.run_checks(commands=commands)
        rows = self.run_checks(commands=commands)
        self.assertFalse(rows[0]['reused'])
        self.assertEqual(rows[0]['exit_code'], 7)
        self.assertEqual(self.count(), 2)
        self.assertNotIn('check_reuse', self.copy.metadata)

    def test_timeout_is_not_published(self):
        commands = [self.command(suffix='; sleep 2')]
        self.run_checks(commands=commands, timeout=0.2)
        rows = self.run_checks(commands=commands, timeout=0.2)
        self.assertFalse(rows[0]['reused'])
        self.assertEqual(rows[0]['exit_code'], 124)
        self.assertEqual(self.count(), 2)

    def test_truncated_success_is_not_published(self):
        commands = [self.command(suffix='; head -c 200000 /dev/zero')]
        self.run_checks(commands=commands)
        rows = self.run_checks(commands=commands)
        self.assertFalse(rows[0]['reused'])
        self.assertTrue(rows[0]['stdout_truncated'])
        self.assertEqual(self.count(), 2)

    def test_incomplete_success_set_is_not_published(self):
        commands = [self.command(), 'exit 5']
        self.run_checks(commands=commands)
        self.run_checks(commands=commands)
        self.assertEqual(self.count(), 2)

    def test_files_changing_during_check_are_not_published(self):
        changing = self.command(extra="Path('changing.txt').write_text('changed')")
        first = self.run_checks(commands=[changing])
        self.assertNotIn('check_reuse', self.copy.metadata)
        second = self.run_checks(commands=[changing])
        self.assertEqual(first[0]['exit_code'], 0)
        self.assertFalse(second[0]['reused'])
        self.assertEqual(self.count(), 2)

    def test_failed_forced_execution_invalidates_prior_success(self):
        command = self.command(
            extra='import sys\nif int(p.read_text()) >= 2:\n    sys.exit(7)')
        first = self.run_checks(commands=[command])
        self.assertFalse(first[0]['reused'])
        forced = self.run_checks(commands=[command], force=True)
        self.assertEqual(forced[0]['exit_code'], 7)
        third = self.run_checks(commands=[command])
        self.assertFalse(third[0]['reused'])
        self.assertEqual(self.count(), 3)

    def test_failed_unattested_forced_execution_invalidates_prior_success(self):
        command = self.command(
            extra='import sys\nif int(p.read_text()) >= 2:\n    sys.exit(7)')
        self.run_checks(commands=[command])
        forced = self.run_checks(commands=[command], reusable=False, force=True)
        self.assertEqual(forced[0]['exit_code'], 7)
        rows = self.run_checks(commands=[command])
        self.assertFalse(rows[0]['reused'])
        self.assertEqual(self.count(), 3)

    def test_unattested_failed_observation_invalidates_cached_success(self):
        command = self.command(
            extra='raise SystemExit(9 if int(p.read_text()) == 2 else 0)')
        first = self.run_checks(commands=[command])
        self.assertEqual(first[0]['exit_code'], 0)
        with self.copy.lock():
            fresh = development.run_checks(
                [LAUNCHER], self.env, [command],
                directory=self.evidence_directory(), copy=self.copy, reusable=False)
        self.assertEqual(fresh[0]['exit_code'], 9)
        later = self.run_checks(commands=[command])
        self.assertFalse(later[0]['reused'])
        self.assertEqual(self.count(), 3)


class IneligibleInputTests(CheckReuseCase):
    def assert_never_cached(self):
        self.run_checks()
        rows = self.run_checks()
        self.assertFalse(rows[0]['reused'])
        self.assertEqual(self.count(), 2)
        self.assertNotIn('check_reuse', self.copy.metadata)

    def test_credential_like_project_input(self):
        (self.copy.path / '.env').write_text('DUMMY_VALUE = "not-a-real-credential"\n')
        self.assert_never_cached()

    def test_symbolic_link_input(self):
        (self.copy.path / 'linked.py').symlink_to('a.py')
        self.assert_never_cached()

    def test_special_file_input(self):
        os.mkfifo(self.copy.path / 'pipe')
        self.assert_never_cached()

    def test_bounded_out_snapshot(self):
        with mock.patch.object(check_reuse, 'MAX_TREE_ENTRIES', 1):
            self.assert_never_cached()

    def test_deeply_nested_snapshot_is_bounded(self):
        directory = self.copy.path
        for _ in range(130):
            directory = directory / 'd'
            directory.mkdir()
        self.assert_never_cached()

    def test_bounded_command_set_executes_without_reuse(self):
        commands = [self.command(), self.command()]
        with mock.patch.object(check_reuse, 'MAX_COMMANDS', 1):
            self.run_checks(commands=commands)
            rows = self.run_checks(commands=commands)
        self.assertFalse(rows[0]['reused'])
        self.assertEqual(self.count(), 4)
        self.assertNotIn('check_reuse', self.copy.metadata)

    def test_unknown_credential_like_environment_is_not_normalized(self):
        env = dict(self.env, CUSTOM_PASSWORD='not-safe-to-inspect')
        self.run_checks(env=env)
        rows = self.run_checks(env=env)
        self.assertFalse(rows[0]['reused'])
        self.assertEqual(self.count(), 2)

    def test_command_containing_known_credential_is_not_cached(self):
        key = 'canary-command-key'
        env = dict(self.env, DEEPSEEK_API_KEY=key)
        command = self.command(extra=f'print({key!r})')
        self.run_checks(commands=[command], env=env)
        rows = self.run_checks(commands=[command], env=env)
        self.assertFalse(rows[0]['reused'])
        self.assertEqual(self.count(), 2)
        self.assertNotIn('check_reuse', self.copy.metadata)

    def test_unsafe_evidence_directory_is_rejected_before_execution(self):
        outside = self.root / 'outside'
        outside.mkdir()
        (self.copy.directory / 'diagnostics').symlink_to(outside)
        with self.assertRaises(check_reuse.CheckReuseError):
            self.run_checks()
        self.assertEqual(self.count(), 0)

    def test_evidence_directory_outside_workspace_is_rejected_before_execution(self):
        outside = self.root / 'outside-evidence'
        outside.mkdir()
        with self.assertRaises(check_reuse.CheckReuseError):
            self.run_checks(directory=outside)
        self.assertEqual(self.count(), 0)

    def test_exclusive_workspace_lock_is_required(self):
        with self.assertRaises(check_reuse.CheckReuseError):
            check_reuse.run_checks(
                self.copy, [LAUNCHER], self.env, [self.command()],
                directory=self.copy.directory / 'diagnostics', reusable=True)
        self.assertEqual(self.count(), 0)

    def test_unsafe_command_interpreter_is_never_reused_until_safe(self):
        info = os.lstat(self.python)
        self.assertTrue(stat.S_ISREG(info.st_mode))
        self.assertEqual(info.st_nlink, 1)
        self.assertEqual(info.st_uid, os.geteuid())
        self.assertEqual(stat.S_IMODE(info.st_mode), 0o700)
        self.python.chmod(0o775)
        self.addCleanup(self.python.chmod, 0o700)
        self.run_checks()
        unsafe = self.run_checks()
        self.assertFalse(unsafe[0]['reused'])
        self.assertEqual(self.count(), 2)
        self.assertNotIn('check_reuse', self.copy.metadata)

        self.python.chmod(0o700)
        safe = self.run_checks()
        reused = self.run_checks()
        self.assertFalse(safe[0]['reused'])
        self.assertTrue(reused[0]['reused'])
        self.assertTrue(reused[0]['reused_from'])
        self.assertEqual(reused[0]['stdout_path'], safe[0]['stdout_path'])
        self.assertEqual(self.count(), 3)
        self.assertEqual(len(self.cache_entries()), 1)

    def test_root_owned_system_executable_is_hashable_for_nonroot_workspace(self):
        executable = Path(shutil.which('env') or LAUNCHER)
        if os.stat(executable).st_uid != 0:
            self.skipTest('the local env executable is not root-owned')
        with mock.patch.object(check_reuse.os, 'geteuid', return_value=1000):
            identity = check_reuse._hash_executable(executable)
        self.assertEqual(identity['size'], executable.stat().st_size)


class WorkspaceVerifyCallPathTests(CheckReuseCase):
    def setUp(self):
        super().setUp()
        self.probe_counter = self.root / 'probe-counter'
        self.probe = self.command(counter=self.probe_counter)
        self.selections = 0

    def verify(self, *, reuse_checks=True, force=False, command=None):
        env = dict(self.env, HOME=str(self.root / f'verify-home-{self.selections}'),
                   TMPDIR=str(self.root / f'verify-tmp-{self.selections}'),
                   DEEPSEEK_API_KEY=f'canary-{self.selections}')
        self.selections += 1
        command = self.command() if command is None else command

        def select(*unused, **unused_keywords):
            return runtime_preflight.RuntimeSelection(
                runtime='codex', binary=str(self.python), layout=[LAUNCHER], environment=env)

        with mock.patch.object(worker, 'resolve_os_sandbox', return_value=(None, None)), \
                mock.patch.object(runtime_preflight, 'select_runtime', side_effect=select), \
                mock.patch.object(toolchains, 'probes_for', return_value=[self.probe]):
            return workspace.verify_checks(self.copy, [command],
                                           reuse_checks=reuse_checks, force=force)

    def test_workspace_verify_reuses_only_the_check_and_always_runs_probes(self):
        first = self.verify()
        original = first['checks'][0]
        original_path = self.evidence_directory() / original['stdout_path']
        original_bytes = original_path.read_bytes()
        second = self.verify()
        reused = second['checks'][0]
        self.assertTrue(reused['reused'])
        self.assertEqual(reused['stdout_path'], original['stdout_path'])
        self.assertEqual(original_path.read_bytes(), original_bytes)
        self.assertEqual(self.count(), 1)
        self.assertEqual(self.count(self.probe_counter), 2)
        self.assertEqual(len(list(self.evidence_directory().glob('verify-probe-*'))), 4)

    def test_workspace_verify_force_executes(self):
        self.verify()
        forced = self.verify(force=True)
        self.assertFalse(forced['checks'][0]['reused'])
        self.assertEqual(self.count(), 2)

    def test_workspace_verify_default_failure_invalidates_cached_success(self):
        command = self.command(
            extra='raise SystemExit(9 if int(p.read_text()) == 2 else 0)')
        first = self.verify(reuse_checks=True, command=command)
        self.assertEqual(first['checks'][0]['exit_code'], 0)
        failed = self.verify(reuse_checks=False, command=command)
        self.assertEqual(failed['checks'][0]['exit_code'], 9)
        later = self.verify(reuse_checks=True, command=command)
        self.assertFalse(later['checks'][0]['reused'])
        self.assertEqual(self.count(), 3)

    def test_workspace_cli_passes_reuse_and_force(self):
        run = {'status': 'passed', 'checks': [{'command': 'ok', 'exit_code': 0}]}
        with mock.patch.object(workspace, 'verify_checks', return_value=run) as verify:
            code = delegation_cli.main([
                'workspace', 'verify', self.copy.id, '--check', 'ok',
                '--reuse-checks', '--force', '--state-dir', str(self.state)])
        self.assertEqual(code, 0)
        self.assertEqual(verify.call_args.kwargs.get('reuse_checks'), True)
        self.assertEqual(verify.call_args.kwargs.get('force'), True)

    def test_workspace_ledger_publication_preserves_reuse_provenance(self):
        run = {
            'id': 'a' * 32, 'workspace_id': self.copy.id, 'status': 'passed',
            'checks': [{'command': 'ok', 'exit_code': 0, 'reused': True,
                        'reused_from': 'b' * 32}],
            'at': 1.0,
        }
        with self.copy.lock():
            self.copy.metadata['pending_verifications'] = [{
                'task_id': 'task', 'assignment_id': 'assignment',
                'finished_at': 1.0, 'run': run,
            }]
            self.copy.save()
            with mock.patch.object(coordination, 'record_verification') as record:
                workspace._publish_verifications(self.copy)
        published = record.call_args.args[3]
        self.assertEqual(published['checks'], run['checks'])


class ManagedCheckReuseTests(fixtures.CoordinationCase):
    def setUp(self):
        super().setUp()
        self.copy = workspace.create(self.repo, self.state)
        self.python = private_python(self.root, 'managed-command-python')
        self.runtime_python = private_python(self.root, 'managed-runtime-python')
        self.counter = self.root / 'managed-counter'
        self.probe_counter = self.root / 'managed-probe-counter'
        self.check = self.command(self.counter)
        self.probe = self.command(self.probe_counter)
        self.task = coordination.open_task(
            self.repo, session_id='check-reuse', turn_id='1',
            prompt='run checks', policy=self.policy())
        self.plan = coordination.plan_task(self.repo, self.task['id'], {
            'classification': 'substantial',
            'deliverables': [{
                'id': 'one', 'kind': 'implementation', 'scope': ['a.py'],
                'executor': 'worker', 'acceptance': ['checks pass'],
                'dependencies': [], 'checks': [self.check], 'checks_reusable': True,
            }],
        })
        self.assignment = self.plan['assignments'][0]['id']

    def command(self, counter, extra=''):
        code = ('from pathlib import Path\n'
                f'p = Path({str(counter)!r})\n'
                "p.write_text(str(int(p.read_text()) + 1) if p.exists() else '1')")
        if extra:
            code += '\n' + extra
        return f'{shlex.quote(str(self.python))} -c {shlex.quote(code)}'

    def count(self, path):
        return int(path.read_text()) if path.exists() else 0

    def make_task(self, *, suffix, check, checks_reusable, correction_of=None):
        task = coordination.open_task(
            self.repo, session_id='check-reuse-' + suffix, turn_id='1',
            prompt='run checks', policy=self.policy())
        deliverable = {
            'id': 'one', 'kind': 'implementation', 'scope': ['a.py'],
            'executor': 'worker', 'acceptance': ['checks pass'],
            'dependencies': [], 'checks': [check],
            'checks_reusable': checks_reusable,
        }
        if correction_of is not None:
            deliverable['correction_of'] = correction_of
        plan = coordination.plan_task(self.repo, task['id'], {
            'classification': 'substantial',
            'deliverables': [deliverable],
        })
        return task, plan['assignments'][0]['id']

    def run_managed(self, *, force_checks=False, task=None, assignment=None):
        env = {
            'PATH': os.environ.get('PATH', '/usr/local/bin:/usr/bin:/bin'),
            'LANG': 'C.UTF-8',
            'HOME': str(self.root / 'managed-runtime-home'),
            'TMPDIR': str(self.root / 'managed-runtime-tmp'),
            'PYTHONHOME': sys.base_prefix,
            'DEEPSEEK_API_KEY': 'canary-managed-provider-key',
        }
        self.managed_env = env
        (self.root / 'managed-runtime-tmp').mkdir(exist_ok=True)
        task = self.task if task is None else task
        assignment = self.assignment if assignment is None else assignment

        class Relay:
            def __init__(self, *unused, **unused_keywords):
                self.failures = []

            def __enter__(self):
                return self

            def __exit__(self, *unused):
                return False

        result = '\n'.join((
            json.dumps({'type': 'item.completed',
                        'item': {'type': 'agent_message', 'text': 'NO_PROJECT_CHANGE'}}),
            json.dumps({'type': 'turn.completed'}),
        ))
        args = SimpleNamespace(
            runtime='codex', codex='codex', codex_explicit=False,
            claude='claude', claude_explicit=False, task='run checks',
            state_dir=self.state, os_sandbox='required', timeout=0,
            attempts=1, attempts_explicit=False, effort='high', no_wait=False,
            workspace=None, resume_after_failure=False, access=None,
            delegation_level=None, max_workers=None,
            coord_task=task['id'], coord_assignment=assignment,
            force_checks=force_checks)
        with mock.patch.object(worker, 'resolve_os_sandbox', return_value=(None, None)), \
                mock.patch.object(runtime_preflight, 'select_runtime',
                    return_value=runtime_preflight.RuntimeSelection(
                        runtime='codex', binary=str(self.runtime_python),
                        layout=[LAUNCHER], environment=env)), \
                mock.patch.object(toolchains, 'probes_for', return_value=[self.probe]), \
                mock.patch.object(worker, 'load_api_key', return_value='test-provider-key'), \
                mock.patch.object(worker, 'execute', return_value=(0, result, '')), \
                mock.patch.object(relay, 'ProviderRelay', Relay):
            code = managed.run(args, self.policy(), worker, self.copy)
        self.copy = workspace.load(self.state, self.copy.id)
        return code

    def test_managed_baseline_post_reuse_uses_real_call_path(self):
        self.assertEqual(self.run_managed(force_checks=False), 0)
        self.assertEqual(self.count(self.counter), 1)
        self.assertEqual(self.count(self.probe_counter), 1)
        stored = json.dumps(self.copy.metadata, sort_keys=True)
        self.assertNotIn('canary-managed-provider-key', stored)
        self.assertNotIn('test-provider-key', stored)
        first = coordination.load_task(self.repo, self.task['id'])['assignments'][0]
        self.assertEqual(first['status'], 'succeeded')
        self.assertEqual(set(first['checks'][0]) - {
            'command', 'exit_code', 'phase', 'stdout_path', 'stderr_path',
            'stdout_truncated', 'stderr_truncated', 'timed_out', 'launch_failed',
            'stdout_sha256', 'stderr_sha256', 'reused', 'reused_from'}, set())
        self.assertTrue(first['checks'][0]['reused'])
        self.assertTrue(first['checks'][0]['reused_from'])
        self.assertFalse(self.copy.metadata['baseline_checks'][0]['reused'])
        self.assertTrue(self.copy.metadata['checks'][0]['reused'])

    def correction_of(self, previous):
        task, assignment = previous
        coordination.use_result(
            self.repo, task['id'], assignment, 'needs-rework',
            'focused test continues in the same logical workspace')
        return {'task_id': task['id'], 'assignment_id': assignment}

    def run_fresh_managed(self, suffix, expected_count, previous=None):
        task, assignment = self.make_task(
            suffix=suffix, check=self.check, checks_reusable=True,
            correction_of=self.correction_of(previous) if previous else None)
        self.assertEqual(self.run_managed(task=task, assignment=assignment), 0)
        self.assertNotIn('check_reuse', self.copy.metadata)
        self.assertFalse(self.copy.metadata['baseline_checks'][0]['reused'])
        self.assertFalse(self.copy.metadata['checks'][0]['reused'])
        self.assertEqual(self.count(self.counter), expected_count)
        return task, assignment

    def run_reused_managed(self, suffix, expected_count, previous):
        task, assignment = self.make_task(
            suffix=suffix, check=self.check, checks_reusable=True,
            correction_of=self.correction_of(previous))
        self.assertEqual(self.run_managed(task=task, assignment=assignment), 0)
        self.assertFalse(self.copy.metadata['baseline_checks'][0]['reused'])
        self.assertTrue(self.copy.metadata['checks'][0]['reused'])
        self.assertTrue(self.copy.metadata['checks'][0]['reused_from'])
        self.assertEqual(self.count(self.counter), expected_count)
        return task, assignment

    def test_managed_unsafe_command_interpreter_never_reuses_until_safe(self):
        self.python.chmod(0o775)
        self.addCleanup(self.python.chmod, 0o700)
        first = self.run_fresh_managed('unsafe-command-one', 2)
        second = self.run_fresh_managed('unsafe-command-two', 4, previous=first)
        self.python.chmod(0o700)
        self.run_reused_managed('safe-command', 5, previous=second)

    def test_managed_unsafe_runtime_binary_never_reuses_until_safe(self):
        self.runtime_python.chmod(0o775)
        self.addCleanup(self.runtime_python.chmod, 0o700)
        first = self.run_fresh_managed('unsafe-runtime-one', 2)
        second = self.run_fresh_managed('unsafe-runtime-two', 4, previous=first)
        self.runtime_python.chmod(0o700)
        self.run_reused_managed('safe-runtime', 5, previous=second)

    def test_managed_force_checks_executes_baseline_and_post(self):
        self.assertEqual(self.run_managed(force_checks=True), 0)
        self.assertEqual(self.count(self.counter), 2)
        self.assertEqual(self.count(self.probe_counter), 1)
        self.assertFalse(self.copy.metadata['baseline_checks'][0]['reused'])
        self.assertFalse(self.copy.metadata['checks'][0]['reused'])

    def test_managed_unattested_failure_invalidates_prior_cached_success(self):
        command = self.command(
            self.counter,
            extra='raise SystemExit(9 if int(p.read_text()) == 2 else 0)')
        first_task, first_assignment = self.make_task(
            suffix='attested', check=command, checks_reusable=True)
        self.assertEqual(self.run_managed(
            task=first_task, assignment=first_assignment), 0)
        self.assertEqual(self.count(self.counter), 1)
        coordination.use_result(
            self.repo, first_task['id'], first_assignment, 'needs-rework',
            'focused test needs a fresh failed observation')
        second_task, second_assignment = self.make_task(
            suffix='unattested', check=command, checks_reusable=False,
            correction_of={'task_id': first_task['id'],
                           'assignment_id': first_assignment})
        self.assertEqual(self.run_managed(
            task=second_task, assignment=second_assignment), 0)
        self.assertEqual(self.count(self.counter), 3)
        with self.copy.lock():
            rows = check_reuse.run_checks(
                self.copy, [LAUNCHER], self.managed_env, [command],
                directory=self.copy.directory / 'diagnostics', reusable=True)
        self.assertFalse(rows[0]['reused'])
        self.assertEqual(self.count(self.counter), 4)

    def test_managed_parser_exposes_force_checks(self):
        with mock.patch.object(sys, 'argv', ['deepseek-team', '--force-checks', 'task']):
            args = worker.parse_args()
        self.assertTrue(args.force_checks)


if __name__ == '__main__':
    unittest.main()
