"""Source preflight: committed HEAD blockers are found before any allocation.

These tests use temporary real Git repositories and assert observable behaviour: exit
codes, allocated directories, blocking rules, escaping and source refs/index/status/
worktree state. They never read or print the contents of a blocking file.
"""
import contextlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import unittest
from unittest import mock

from codex_deepseek_team import delegation_cli, workspace

SECRET_MARKER = 'synthetic-secret-marker-value'


def _git(root, *args, check=True):
    result = subprocess.run(['git', '-C', str(root), *args],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=check)
    return result.stdout.decode('utf-8', 'replace')


def _commit(root, *args):
    _git(root, '-c', 'user.name=Preflight', '-c', 'user.email=preflight@example.test', *args)


class PreflightFixture(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='dst-preflight-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.state = self.root / 'state'
        self._counter = 0

    def source_repo(self, files=None, *, name=None):
        self._counter += 1
        source = self.root / (name or f'source-{self._counter}')
        source.mkdir()
        _git(source, 'init', '-q', '-b', 'main')
        for relative, text in (files or {}).items():
            path = source / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        if files:
            _git(source, 'add', '-A')
        _commit(source, 'commit', '-q', '--allow-empty', '-m', 'base')
        return source

    def state_entries(self):
        workspaces = self.state / 'workspaces'
        return sorted(p.name for p in workspaces.iterdir()) if workspaces.is_dir() else []

    def source_state(self, source):
        return {
            'head': _git(source, 'rev-parse', 'HEAD').strip(),
            'branch': _git(source, 'symbolic-ref', 'HEAD').strip(),
            'refs': _git(source, 'for-each-ref',
                         '--format=%(refname) %(objectname) %(objecttype)'),
            'index': _git(source, 'ls-files', '-s'),
            'status': _git(source, 'status', '--porcelain=v1', '--untracked-files=all'),
            'worktrees': _git(source, 'worktree', 'list', '--porcelain'),
        }

    def cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = delegation_cli.main(list(args))
        return code, out.getvalue(), err.getvalue()


class CheckSourceTests(PreflightFixture):
    def test_report_shape_and_canonical_source(self):
        source = self.source_repo({'src/app.py': 'print(1)\n'})
        report = workspace.check_source(source)
        self.assertEqual(set(report), {'source', 'head', 'eligible', 'blockers'})
        self.assertEqual(report['source'], str(source.resolve()))
        self.assertEqual(report['head'], _git(source, 'rev-parse', 'HEAD').strip())
        self.assertTrue(re.fullmatch(r'[0-9a-f]{40}', report['head']))
        self.assertIs(report['eligible'], True)
        self.assertEqual(report['blockers'], [])

    def test_committed_env_backup_anywhere_in_head_blocks_and_names_the_offender(self):
        source = self.source_repo({'infra/config/.env.backup': SECRET_MARKER,
                                   'src/app.py': 'print(1)\n'})
        report = workspace.check_source(source)
        self.assertIs(report['eligible'], False)
        self.assertEqual([item['path'] for item in report['blockers']],
                         ['infra/config/.env.backup'])
        self.assertEqual(set(report['blockers'][0]), {'path', 'reason'})
        self.assertIn('credential', report['blockers'][0]['reason'])
        before = self.source_state(source)
        for _ in range(2):
            with self.assertRaises(workspace.WorkspaceError) as caught:
                workspace.create(source, self.state)
            message = str(caught.exception)
            self.assertEqual(caught.exception.code, 78)
            self.assertIn('infra/config/.env.backup', message)
            self.assertNotIn(SECRET_MARKER, message)
            self.assertNotIn('Retained workspace', message)
            self.assertIsNone(caught.exception.workspace_id)
        self.assertEqual(self.state_entries(), [])
        self.assertFalse(self.state.exists())
        self.assertEqual(self.source_state(source), before)

    def test_blockers_are_reported_deterministically_and_completely(self):
        source = self.source_repo({'z/.env.backup': 'x\n'})
        (source / '.env').write_text('x\n')
        (source / 'a').mkdir()
        (source / 'a/.env.local').write_text('x\n')
        (source / '.env.example').write_text('allowed\n')
        _git(source, 'add', '-A')
        _commit(source, 'commit', '-qm', 'blockers')
        report = workspace.check_source(source)
        self.assertEqual([item['path'] for item in report['blockers']],
                         ['.env', 'a/.env.local', 'z/.env.backup'])
        self.assertEqual(report['blockers'], workspace.check_source(source)['blockers'])

    def test_exact_template_names_allowed_while_env_test_example_is_blocked(self):
        allowed = ['.env.example', '.env.sample', '.env.template', 'docs/.env.example']
        source = self.source_repo({name: 'placeholder\n' for name in allowed})
        report = workspace.check_source(source)
        self.assertTrue(report['eligible'], report['blockers'])
        copy = workspace.create(source, self.state)
        self.assertEqual(copy.metadata['base_head'], report['head'])
        for name in ['.env.test.example', '.env.backup', '.env.local',
                     '.env.production.example']:
            with self.subTest(name=name):
                blocked = self.source_repo({name: 'synthetic\n'})
                report = workspace.check_source(blocked)
                self.assertFalse(report['eligible'])
                self.assertEqual([item['path'] for item in report['blockers']], [name])
                with self.assertRaises(workspace.WorkspaceError):
                    workspace.create(blocked, self.state)

    def test_only_committed_head_blocks_even_when_staged_untracked_or_ignored(self):
        source = self.source_repo({'app.py': 'x\n', '.gitignore': '.env\n'})
        (source / '.env').write_text('local ignored file\n')
        _git(source, 'add', '-f', '.env')
        report = workspace.check_source(source)
        self.assertTrue(report['eligible'], report['blockers'])
        copy = workspace.create(source, self.state)
        self.assertFalse((copy.path / '.env').exists())
        self.assertIn('.env', _git(source, 'ls-files', '-s'))

    def test_worktree_deletion_and_gitignore_do_not_unblock_committed_env(self):
        source = self.source_repo({'.env': 'committed synthetic\n'})
        (source / 'app.py').write_text('x\n')
        _git(source, 'add', 'app.py')
        _commit(source, 'commit', '-qm', 'add app')
        (source / '.gitignore').write_text('.env\n')
        _git(source, 'add', '.gitignore')
        _commit(source, 'commit', '-qm', 'ignore env locally')
        (source / '.env').unlink()
        report = workspace.check_source(source)
        self.assertFalse(report['eligible'])
        self.assertEqual([item['path'] for item in report['blockers']], ['.env'])
        with self.assertRaises(workspace.WorkspaceError):
            workspace.create(source, self.state)

    def test_removing_the_blocker_from_head_permits_a_fresh_copy(self):
        source = self.source_repo({'app.py': 'x\n', '.env': 'synthetic secret\n'})
        self.assertFalse(workspace.check_source(source)['eligible'])
        _git(source, 'rm', '-q', '--cached', '.env')
        _commit(source, 'commit', '-qm', 'drop env from committed head')
        self.assertTrue((source / '.env').is_file())
        report = workspace.check_source(source)
        self.assertTrue(report['eligible'], report['blockers'])
        copy = workspace.create(source, self.state)
        self.assertEqual(copy.metadata['base_head'], report['head'])
        self.assertEqual(copy.metadata['status'], 'ready')
        self.assertFalse((copy.path / '.env').exists())
        self.assertEqual(workspace.load(self.state, copy.id).metadata['base_head'],
                         report['head'])
        copy.verify()

    def test_submodule_gitlink_blocks_with_reason(self):
        source = self.source_repo({'app.py': 'x\n'})
        head = _git(source, 'rev-parse', 'HEAD').strip()
        _git(source, 'update-index', '--add', '--cacheinfo', f'160000,{head},vendor/lib')
        _commit(source, 'commit', '-qm', 'add gitlink')
        report = workspace.check_source(source)
        self.assertFalse(report['eligible'])
        self.assertEqual([item['path'] for item in report['blockers']], ['vendor/lib'])
        self.assertIn('submodule', report['blockers'][0]['reason'])
        with self.assertRaises(workspace.WorkspaceError) as caught:
            workspace.create(source, self.state)
        self.assertIn('vendor/lib', str(caught.exception))
        self.assertNotIn('Retained workspace', str(caught.exception))
        self.assertEqual(self.state_entries(), [])

    def test_check_source_allocates_nothing_and_leaves_source_untouched(self):
        clean = self.source_repo({'app.py': 'x\n'})
        blocked = self.source_repo({'.env': SECRET_MARKER})
        for source in (clean, blocked):
            before = self.source_state(source)
            workspace.check_source(source)
            self.cli('workspace', 'check', str(source))
            self.cli('workspace', 'check', str(source), '--json')
            self.assertEqual(self.source_state(source), before)
            self.assertEqual(self.state_entries(), [])

    def test_plain_and_json_rendering_escape_control_names_without_contents(self):
        weird = '.env.\x1b[31m\nspoofed'
        source = self.source_repo({'app.py': 'x\n'})
        (source / weird).write_text(SECRET_MARKER + '\n')
        _git(source, 'add', '--', weird)
        _commit(source, 'commit', '-qm', 'weird blocker name')
        report = workspace.check_source(source)
        self.assertEqual([item['path'] for item in report['blockers']], [weird])
        code, out, err = self.cli('workspace', 'check', str(source))
        self.assertEqual(code, 78)
        combined = out + err
        self.assertNotIn(weird, combined)
        self.assertNotIn('\x1b', combined)
        self.assertNotIn(SECRET_MARKER, combined)
        self.assertIn('spoofed', combined)
        self.assertIn('\\x1b', combined)
        code, out, err = self.cli('workspace', 'check', str(source), '--json')
        self.assertEqual(code, 78)
        self.assertNotIn('\x1b', out)
        self.assertNotIn(SECRET_MARKER, out)
        parsed = json.loads(out)
        self.assertEqual(parsed['blockers'][0]['path'], weird)
        self.assertEqual(set(parsed), {'source', 'head', 'eligible', 'blockers'})

    def test_non_utf8_blocker_name_is_escaped_and_json_stays_ascii(self):
        raw = b'.env.\xff\xfe'
        source = self.source_repo({'app.py': 'x\n'})
        descriptor = os.open(os.path.join(os.fsencode(source), raw),
                             os.O_CREAT | os.O_WRONLY, 0o600)
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(SECRET_MARKER.encode())
        _git(source, 'add', '-A')
        _commit(source, 'commit', '-qm', 'non-utf8 blocker name')
        report = workspace.check_source(source)
        self.assertEqual([item['path'] for item in report['blockers']], [os.fsdecode(raw)])
        code, out, err = self.cli('workspace', 'check', str(source))
        self.assertEqual(code, 78)
        combined = out + err
        self.assertIn('\\xff\\xfe', combined)
        combined.encode('utf-8')  # no lone surrogates may reach a terminal
        self.assertNotIn(SECRET_MARKER, combined)
        code, out, err = self.cli('workspace', 'check', str(source), '--json')
        self.assertEqual(code, 78)
        out.encode('ascii')  # ensure_ascii output is transport-safe
        self.assertEqual(json.loads(out)['blockers'][0]['path'], os.fsdecode(raw))

    def test_cli_check_exit_codes_for_eligible_and_blocked(self):
        clean = self.source_repo({'app.py': 'x\n'})
        code, out, err = self.cli('workspace', 'check', str(clean))
        self.assertEqual(code, 0)
        self.assertIn(workspace.check_source(clean)['head'], out)
        code, out, err = self.cli('workspace', 'check', str(clean), '--json')
        self.assertEqual((code, json.loads(out)['eligible']), (0, True))
        blocked = self.source_repo({'.env': SECRET_MARKER})
        code, out, err = self.cli('workspace', 'check', str(blocked), '--json')
        self.assertEqual((code, json.loads(out)['eligible']), (78, False))

    def test_check_and_import_share_one_credential_predicate(self):
        names = {'.env': True, '.env.example': False, '.env.sample': False,
                 '.env.template': False, '.env.test.example': True, '.env.backup': True,
                 'auth.json': True, '.netrc': True, 'id_ed25519': True,
                 'deploy/server.pem': True, 'ops/.kube/config': True, 'plain.txt': False,
                 'keys/keystore.jks': True}
        clean = self.source_repo({'plain.txt': 'x\n'})
        copy = workspace.create(clean, self.state)
        for name, blocked in names.items():
            with self.subTest(name=name):
                repo = self.source_repo({name: 'synthetic\n'})
                self.assertEqual(workspace.check_source(repo)['eligible'], not blocked)
                candidate = clean / name
                candidate.parent.mkdir(parents=True, exist_ok=True)
                candidate.write_text('synthetic import\n')
                try:
                    if blocked:
                        with self.assertRaises(workspace.WorkspaceError):
                            workspace.import_paths(copy, [name])
                    else:
                        self.assertEqual(workspace.import_paths(copy, [name]), [name])
                finally:
                    candidate.unlink()


class PreflightRaceTests(PreflightFixture):
    def test_preflight_head_is_pinned_for_the_fetch(self):
        source = self.source_repo({'app.py': 'x\n'})
        real_check = workspace.check_source
        reports = []

        def racing_check(path):
            report = real_check(path)
            reports.append(report)
            (source / '.env').write_text(SECRET_MARKER + '\n')
            _git(source, 'add', '.env')
            _commit(source, 'commit', '-qm', 'introduce blocker after preflight')
            return report

        with mock.patch.object(workspace, 'check_source', side_effect=racing_check):
            copy = workspace.create(source, self.state)
        self.assertEqual(copy.metadata['base_head'], reports[0]['head'])
        self.assertNotEqual(_git(source, 'rev-parse', 'HEAD').strip(), reports[0]['head'])
        self.assertFalse((copy.path / '.env').exists())
        copy.verify()
        with copy.lock():
            names, _ = copy.changes()
        self.assertEqual(names, [])

    def test_fetched_tree_is_rechecked_before_any_checkout(self):
        source = self.source_repo({'app.py': 'x\n'})
        (source / '.env').write_text(SECRET_MARKER + '\n')
        _git(source, 'add', '.env')
        _commit(source, 'commit', '-qm', 'blocked head')
        blocked = workspace.check_source(source)
        self.assertFalse(blocked['eligible'])
        forged = dict(blocked, eligible=True, blockers=[])
        with mock.patch.object(workspace, 'check_source', return_value=forged):
            with self.assertRaises(workspace.WorkspaceError) as caught:
                workspace.create(source, self.state)
        error = caught.exception
        self.assertRegex(error.workspace_id or '', r'^[0-9a-f]{32}$')
        self.assertIn('.env', str(error))
        self.assertNotIn(SECRET_MARKER, str(error))
        retained = self.state / 'workspaces' / error.workspace_id
        record = json.loads((retained / 'record.json').read_text())
        self.assertEqual(record['status'], 'failed')
        self.assertNotIn('base_head', record)
        self.assertNotIn('git_digest', record)
        work = retained / 'work'
        self.assertEqual(sorted(p.name for p in work.iterdir()), ['.git'])
        self.assertFalse((work / '.env').exists())
        # Neither a forged baseline nor a recomputed metadata digest may make
        # a rejected fetched tree launchable: its HEAD must remain unborn.
        record.update(base_head=blocked['head'], git_digest=workspace._git_digest(work))
        (retained / 'record.json').write_text(json.dumps(record))
        loaded = workspace.load(self.state, error.workspace_id)
        with self.assertRaises(workspace.WorkspaceError):
            loaded.verify()
        with self.assertRaises(workspace.WorkspaceError):
            with loaded.lock(recover=True):
                self.fail('forged rejected copy became launchable')
        marker = self.root / 'forged-prepare-marker'
        with self.assertRaises(workspace.WorkspaceError):
            workspace.prepare(loaded, ['/usr/bin/touch', str(marker)], recover=True)
        self.assertFalse(marker.exists())

    def test_post_allocation_failure_carries_structured_workspace_id(self):
        source = self.source_repo({'app.py': 'x\n'})
        with mock.patch.object(workspace, '_check_source_tree',
                               side_effect=workspace.WorkspaceError('injected fetched-tree failure')):
            with self.assertRaises(workspace.WorkspaceError) as caught:
                workspace.create(source, self.state)
        error = caught.exception
        self.assertRegex(error.workspace_id or '', r'^[0-9a-f]{32}$')
        self.assertIn(f'Retained workspace {error.workspace_id}', str(error))
        retained = self.state / 'workspaces' / error.workspace_id
        self.assertTrue(retained.is_dir())
        record = json.loads((retained / 'record.json').read_text())
        self.assertEqual((record['status'], record['error_kind']), ('failed', 'preparation'))
        self.assertEqual(workspace.load(self.state, error.workspace_id).id, error.workspace_id)

    def test_metadata_failure_after_allocation_preserves_copy_id_and_safe_error(self):
        source = self.source_repo({'app.py': 'x\n'})
        with mock.patch.object(workspace.Workspace, 'save', side_effect=OSError('PRIVATE_PAYLOAD')):
            with self.assertRaises(workspace.WorkspaceError) as caught:
                workspace.create(source, self.state)
        self.assertRegex(caught.exception.workspace_id or '', r'^[0-9a-f]{32}$')
        self.assertTrue((self.state / 'workspaces' / caught.exception.workspace_id).is_dir())
        self.assertNotIn('PRIVATE_PAYLOAD', str(caught.exception))

    def test_interrupted_creation_retains_failure_and_identifies_copy(self):
        source = self.source_repo({'app.py': 'x\n'})
        with mock.patch.object(workspace, '_check_source_tree', side_effect=KeyboardInterrupt()):
            with self.assertRaises(KeyboardInterrupt) as caught:
                workspace.create(source, self.state)
        identifier = getattr(caught.exception, 'workspace_id', None)
        self.assertRegex(identifier or '', r'^[0-9a-f]{32}$')
        record = workspace.load(self.state, identifier).metadata
        self.assertEqual((record['status'], record['exit_code']), ('failed', 130))


class IncompletePreparationTests(PreflightFixture):
    def incomplete_copy(self):
        source = self.source_repo({'app.py': 'x\n'})
        copy = workspace.create(source, self.state)
        record_path = copy.directory / 'record.json'
        record = json.loads(record_path.read_text())
        record.pop('base_head', None)
        record.pop('git_digest', None)
        record['status'] = 'preparing'
        record_path.write_text(json.dumps(record))
        return copy, record_path

    def test_incomplete_preparation_remains_quarantined_but_inspectable(self):
        copy, record_path = self.incomplete_copy()
        loaded = workspace.load(self.state, copy.id)
        self.assertEqual(loaded.id, copy.id)
        with self.assertRaises(workspace.WorkspaceError) as caught:
            loaded.verify()
        self.assertIn('preparation never completed', str(caught.exception).lower())
        self.assertIn('workspace show', str(caught.exception))
        for recover in (False, True):
            with self.assertRaises(workspace.WorkspaceError) as caught:
                with loaded.lock(recover=recover):
                    self.fail('an incomplete copy must never be locked for work')
            self.assertIn('preparation never completed', str(caught.exception).lower())
            self.assertIn('workspace show', str(caught.exception))
            self.assertIn('fresh copy', str(caught.exception))
        marker = self.root / 'prepare-marker'
        with self.assertRaises(workspace.WorkspaceError):
            workspace.prepare(loaded, ['/bin/sh', '-c', f'touch {marker}'], recover=True)
        self.assertFalse(marker.exists())
        code, out, err = self.cli('workspace', 'show', copy.id, '--state-dir', str(self.state))
        self.assertEqual(code, 0)
        self.assertIn(copy.id, out)
        self.assertTrue(record_path.is_file())
        self.assertTrue((copy.path / 'app.py').is_file())

    def test_metadata_bypass_is_not_available_for_incomplete_copies(self):
        copy, record_path = self.incomplete_copy()
        record = json.loads(record_path.read_text())
        record.update(base_head='0' * 40)
        record_path.write_text(json.dumps(record))
        loaded = workspace.load(self.state, copy.id)
        with self.assertRaises(workspace.WorkspaceError):
            loaded.verify()


if __name__ == '__main__':
    unittest.main()
