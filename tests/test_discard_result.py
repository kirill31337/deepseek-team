"""Explicit, typed result discard for copies whose capture is impossible.

The flag is destructive: it is accepted only for cancellation, only with an
explicit reason, and only after one of the closed capture failures.  Unknown
exceptions and protocol/signal states must preserve the execution copy.
"""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from codex_deepseek_team import (cli, coordination, coordinator_activity, git_integrity, lessons,
                                 workspace, workspace_gc, workspace_lifecycle)
from tests.test_preparation_finalization import PreparationFinalizationFixture
from tests.test_workspace_lifecycle import LifecycleFixture


DISCARD_CODES = (
    'git_integrity_mismatch', 'capture_too_large',
    'capture_unavailable', 'capture_failed')


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _control(payload):
    return json.dumps(payload, separators=(',', ':')).encode('ascii')


def _run_cli(*arguments):
    output, errors = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
        code = cli.main([str(value) for value in arguments])
    return subprocess.CompletedProcess(arguments, code, output.getvalue(), errors.getvalue())


class DiscardWorkspaceFixture(LifecycleFixture):
    def completed(self, content='accepted fix\n', require_namespace=False):
        if require_namespace:
            self.require_capture()
        copy = self.copy()
        (copy.path / 'app.txt').write_text(content)
        with copy.lock():
            copy.begin('execution')
            copy.finish('succeeded')
        return copy

    def failed_capture(self, code='capture_unavailable'):
        return workspace_gc.CaptureFailure(code, 'fixture typed capture failure')

    def retain_prior_result(self, copy, *, attempt_id, patch=None):
        """Model a durable earlier result manifest and its retained bytes."""
        patch = (copy.directory / 'changes.patch').read_bytes() if patch is None else patch
        result = {'schema': 1, 'workspace_id': copy.id, 'source': str(copy.source),
                  'base_head': copy.metadata.get('base_head'),
                  'patch': {'file': 'changes.patch', 'bytes': len(patch),
                            'sha256': _sha(patch)}}
        if attempt_id is not None:
            result['attempt_id'] = attempt_id
        target = copy.directory / 'result.json'
        target.write_text(json.dumps(result) + '\n')
        target.chmod(0o600)
        return result


class WorkspaceDiscardTests(DiscardWorkspaceFixture):
    def test_cancel_without_discard_preserves_every_typed_capture_failure(self):
        for code in DISCARD_CODES:
            with self.subTest(code=code):
                copy = self.completed()
                patch_before = (copy.directory / 'changes.patch').read_bytes()
                result_before = (copy.directory / 'result.json').read_text() if (
                    copy.directory / 'result.json').exists() else None
                with mock.patch.object(workspace_gc, '_capture',
                                       side_effect=self.failed_capture(code)):
                    with self.assertRaises(workspace.WorkspaceError) as caught:
                        workspace_gc.cancel(copy, reason='ordinary rejection')
                self.assertEqual(caught.exception.reason_code, code)
                self.assertIn(caught.exception.code, (73, 78))
                self.assertTrue(copy.path.exists())
                self.assertEqual((copy.directory / 'changes.patch').read_bytes(),
                                 patch_before)
                if result_before is None:
                    self.assertFalse((copy.directory / 'result.json').exists())
                else:
                    self.assertEqual((copy.directory / 'result.json').read_text(),
                                     result_before)

    def test_explicit_discard_records_schema_two_and_removes_only_execution_data(self):
        copy = self.completed()
        patch_before = (copy.directory / 'changes.patch').read_bytes()
        diagnostics = copy.directory / 'diagnostics'
        diagnostics.mkdir(mode=0o700)
        (diagnostics / 'check.log').write_text('retained check evidence\n')
        extra = copy.directory / 'runtime-fixture'
        extra.mkdir(mode=0o700)
        (extra / 'payload.bin').write_bytes(b'runtime')
        with mock.patch.object(workspace_gc, '_capture',
                               side_effect=self.failed_capture('git_integrity_mismatch')):
            report = workspace_gc.cancel(copy, reason='user authorized losing this copy',
                                         discard_result=True)
        self.assertEqual(report['action'], 'cleaned')
        self.assertTrue(report['discarded'])
        self.assertFalse(copy.path.exists())
        self.assertFalse(extra.exists())
        self.assertEqual((diagnostics / 'check.log').read_text(), 'retained check evidence\n')
        self.assertEqual((copy.directory / 'changes.patch').read_bytes(), patch_before)
        result = json.loads((copy.directory / 'result.json').read_text())
        self.assertEqual(result['schema'], 2)
        self.assertIsNone(result['patch'])
        self.assertEqual(result['capture']['status'], 'discarded')
        self.assertEqual(result['capture']['reason_code'], 'git_integrity_mismatch')
        self.assertEqual(result['capture']['reason'], 'user authorized losing this copy')
        self.assertEqual(result['historical_artifacts'], [{
            'file': 'changes.patch', 'bytes': len(patch_before),
            'sha256': _sha(patch_before), 'attempt_id': None,
            'provenance': 'legacy_unattributed'}])
        lease = workspace_lifecycle.read(copy)
        self.assertEqual(lease['phase'], 'cancelled')
        self.assertEqual(lease['cleanup']['state'], 'cleaned')
        self.assertTrue(lease['finalization']['discarded'])

    def test_discard_requires_cancellation_and_an_explicit_nondefault_reason(self):
        copy = self.completed()
        with self.assertRaises(workspace.WorkspaceError):
            workspace_gc.finalize(copy, reason='accept', discard_result=True)
        with self.assertRaises(workspace.WorkspaceError):
            workspace_gc.cancel(copy, reason='cancelled', discard_result=True)
        with self.assertRaises(workspace.WorkspaceError):
            workspace_gc.cancel(copy, reason='', discard_result=True)
        self.assertTrue(copy.path.exists())

    def test_discard_is_refused_when_the_capture_succeeds(self):
        copy = self.completed()
        with mock.patch.object(workspace_gc, '_capture', return_value=b'successful patch\n'):
            report = workspace_gc.cancel(copy, reason='explicit but capture succeeded',
                                         discard_result=True)
        self.assertFalse(report['discarded'])
        result = json.loads((copy.directory / 'result.json').read_text())
        self.assertEqual(result['schema'], 1)
        self.assertEqual(result['patch']['bytes'], len(b'successful patch\n'))
        self.assertEqual((copy.directory / 'changes.patch').read_bytes(), b'successful patch\n')

    def test_unknown_capture_exception_preserves_copy_and_has_no_discard_code(self):
        copy = self.completed()
        with mock.patch.object(workspace_gc, '_capture', side_effect=RuntimeError('private text')):
            with self.assertRaises(RuntimeError):
                workspace_gc.cancel(copy, reason='authorized loss', discard_result=True)
        self.assertTrue(copy.path.exists())
        self.assertFalse((copy.directory / 'result.json').exists())
        lease = workspace_lifecycle.read(copy)
        self.assertNotIn(lease['phase'], workspace_lifecycle.FINAL_PHASES)

    def test_each_closed_capture_failure_is_recorded_truthfully(self):
        for code in DISCARD_CODES:
            with self.subTest(code=code):
                copy = self.completed()
                with mock.patch.object(workspace_gc, '_capture',
                                       side_effect=self.failed_capture(code)):
                    workspace_gc.cancel(copy, reason='authorized ' + code,
                                        discard_result=True)
                result = json.loads((copy.directory / 'result.json').read_text())
                self.assertEqual(result['schema'], 2)
                self.assertEqual(result['capture']['reason_code'], code)
                self.assertIsNone(result['patch'])

    def test_historical_patch_with_verified_attempt_provenance_survives(self):
        copy = self.completed()
        attempt_id = workspace_lifecycle.read(copy)['attempts'][-1]['id']
        patch = (copy.directory / 'changes.patch').read_bytes()
        self.retain_prior_result(copy, attempt_id=attempt_id, patch=patch)
        with mock.patch.object(workspace_gc, '_capture',
                               side_effect=self.failed_capture('capture_failed')):
            workspace_gc.cancel(copy, reason='authorized result loss', discard_result=True)
        result = json.loads((copy.directory / 'result.json').read_text())
        artifact = result['historical_artifacts'][0]
        self.assertEqual(artifact['provenance'], 'verified_attempt')
        self.assertEqual(artifact['attempt_id'], attempt_id)
        self.assertEqual((copy.directory / 'changes.patch').read_bytes(), patch)

    def test_historical_patch_without_attempt_association_is_legacy_unattributed(self):
        copy = self.completed()
        patch = (copy.directory / 'changes.patch').read_bytes()
        self.retain_prior_result(copy, attempt_id=None, patch=patch)
        with mock.patch.object(workspace_gc, '_capture',
                               side_effect=self.failed_capture('capture_failed')):
            workspace_gc.cancel(copy, reason='authorized result loss', discard_result=True)
        result = json.loads((copy.directory / 'result.json').read_text())
        artifact = result['historical_artifacts'][0]
        self.assertEqual(artifact['provenance'], 'legacy_unattributed')
        self.assertIsNone(artifact['attempt_id'])
        self.assertEqual((copy.directory / 'changes.patch').read_bytes(), patch)

    def test_schema_two_reader_rejects_hidden_or_unattributed_history(self):
        for mutation in ('hidden_patch', 'foreign_attempt'):
            with self.subTest(mutation=mutation):
                copy = self.completed()
                attempt_id = workspace_lifecycle.read(copy)['attempts'][-1]['id']
                patch = (copy.directory / 'changes.patch').read_bytes()
                self.retain_prior_result(copy, attempt_id=attempt_id, patch=patch)
                with mock.patch.object(workspace_gc, '_capture',
                                       side_effect=self.failed_capture('capture_failed')):
                    workspace_gc.cancel(copy, reason='authorized result loss',
                                        discard_result=True)
                result = json.loads((copy.directory / 'result.json').read_text())
                if mutation == 'hidden_patch':
                    result['historical_artifacts'] = []
                else:
                    result['historical_artifacts'][0]['attempt_id'] = 'f' * 32
                (copy.directory / 'result.json').write_text(json.dumps(result))
                with self.assertRaises(workspace.WorkspaceError):
                    workspace_gc.read_result(copy)
                shown = _run_cli('workspace', 'show', copy.id,
                                 '--state-dir', self.state)
                self.assertNotEqual(shown.returncode, 0)
                self.assertEqual((copy.directory / 'changes.patch').read_bytes(), patch)

    def test_schema_two_does_not_supply_a_current_patch(self):
        copy = self.completed()
        with mock.patch.object(workspace_gc, '_capture',
                               side_effect=self.failed_capture('capture_too_large')):
            workspace_gc.cancel(copy, reason='authorized oversized result loss',
                                discard_result=True)
        inspection = workspace_gc.inspect_workspace(self.state, copy.id)
        self.assertEqual(inspection['result']['schema'], 2)
        self.assertIsNone(inspection['result']['patch'])
        denied = _run_cli('workspace', 'diff', copy.id, '--state-dir', self.state)
        self.assertNotEqual(denied.returncode, 0)
        self.assertIn('patch', denied.stderr.lower())

    def test_schema_two_reader_rejects_contradictory_capture_and_request(self):
        for mutation in ('baseline_status', 'capture_reason', 'request_identity'):
            with self.subTest(mutation=mutation):
                copy = self.completed()
                with mock.patch.object(workspace_gc, '_capture',
                                       side_effect=self.failed_capture('capture_failed')):
                    workspace_gc.cancel(copy, reason='authorized result loss',
                                        discard_result=True)
                result = json.loads((copy.directory / 'result.json').read_text())
                if mutation == 'baseline_status':
                    self.assertIsNotNone(result['base_head'])
                    result['capture']['baseline_status'] = 'unavailable'
                elif mutation == 'capture_reason':
                    result['capture']['reason'] = 'a different request'
                else:
                    result['finalization']['request_id'] = 'f' * 32
                (copy.directory / 'result.json').write_text(json.dumps(result))
                with self.assertRaises(workspace.WorkspaceError):
                    workspace_gc.read_result(copy)
                shown = _run_cli('workspace', 'show', copy.id,
                                 '--state-dir', self.state)
                self.assertNotEqual(shown.returncode, 0)

    def test_workspace_cli_cancel_discard_requires_explicit_reason(self):
        copy = self.completed()
        denied = _run_cli('workspace', 'cancel', copy.id, '--state-dir', self.state,
                          '--discard-result')
        self.assertEqual(denied.returncode, 64)
        self.assertTrue(copy.path.exists())
        with mock.patch.object(workspace_gc, '_capture',
                               side_effect=workspace_gc.CaptureFailure(
                                   'capture_too_large', 'fixture 64 MiB bound')):
            accepted = _run_cli('workspace', 'cancel', copy.id, '--state-dir', self.state,
                                '--discard-result', '--reason',
                                'user authorized losing this copy')
        self.assertEqual(accepted.returncode, 0, accepted.stderr)
        self.assertFalse(copy.path.exists())
        self.assertEqual(workspace_gc.inspect_workspace(
            self.state, copy.id)['result']['capture']['reason_code'], 'capture_too_large')

    def test_workspace_cli_cancel_discard_rejects_default_reason(self):
        copy = self.completed()
        for reason in ('cancelled', '  CANCELLED  ', ''):
            with self.subTest(reason=reason):
                denied = _run_cli('workspace', 'cancel', copy.id,
                                  '--state-dir', self.state,
                                  '--discard-result', '--reason', reason)
                self.assertEqual(denied.returncode, 64)
                self.assertTrue(copy.path.exists())

    def test_open_execution_file_blocks_discard_before_capture(self):
        copy = self.completed()
        with (copy.path / 'app.txt').open('rb'):
            with mock.patch.object(workspace_gc, '_capture',
                                   side_effect=AssertionError('capture after open file')) as capture:
                with self.assertRaises(workspace.WorkspaceError) as caught:
                    workspace_gc.cancel(copy, reason='authorized loss',
                                        discard_result=True)
        self.assertEqual(caught.exception.code, 75)
        capture.assert_not_called()
        self.assertTrue(copy.path.exists())

    def test_unknown_result_schema_fails_closed_for_inspection(self):
        copy = self.completed()
        (copy.directory / 'changes.patch').write_bytes(b'patch\n')
        (copy.directory / 'result.json').write_text(json.dumps({
            'schema': 99, 'workspace_id': copy.id, 'source': str(copy.source),
            'base_head': copy.metadata.get('base_head'), 'patch': None}) + '\n')
        with self.assertRaises(workspace.WorkspaceError):
            workspace_gc.inspect_workspace(self.state, copy.id)

    def test_incomplete_baseline_empty_tree_still_gets_successful_schema_one(self):
        copy = self.copy()
        copy.metadata.pop('base_head', None)
        copy.save()
        for entry in copy.path.iterdir():
            if entry.name != '.git':
                if entry.is_dir():
                    shutil.rmtree(entry)
                else:
                    entry.unlink()
        report = workspace_gc.cancel(copy, reason='empty pre-baseline copy',
                                     discard_result=True)
        self.assertFalse(report['discarded'])
        result = json.loads((copy.directory / 'result.json').read_text())
        self.assertEqual(result['schema'], 1)
        self.assertEqual(result['patch']['bytes'], 0)

    def test_incomplete_nonempty_baseline_discards_only_after_typed_unavailable(self):
        copy = self.copy()
        copy.metadata.pop('base_head', None)
        copy.save()
        (copy.path / 'untracked.txt').write_text('survives only as discarded evidence\n')
        before = (copy.path / 'untracked.txt').read_bytes()
        with mock.patch.object(workspace_gc, '_capture',
                               side_effect=self.failed_capture('capture_unavailable')):
            report = workspace_gc.cancel(copy, reason='authorized pre-baseline loss',
                                         discard_result=True)
        self.assertTrue(report['discarded'])
        result = json.loads((copy.directory / 'result.json').read_text())
        self.assertIsNone(result['base_head'])
        self.assertEqual(result['capture']['baseline_status'], 'unavailable')
        self.assertNotEqual(before, b'')
        self.assertFalse(copy.path.exists())

    def test_canary_symlink_and_swapped_inode_block_discard_before_deletion(self):
        for variant in ('symlink', 'swapped'):
            with self.subTest(variant=variant):
                copy = self.completed()
                outside = self.root / ('outside-' + variant)
                outside.mkdir()
                (outside / 'canary').write_text('preserve')
                if variant == 'symlink':
                    copy.path.rename(self.root / ('detached-' + variant))
                    copy.path.symlink_to(outside, target_is_directory=True)
                else:
                    copy.path.rename(self.root / ('detached-' + variant))
                    copy.path.mkdir(mode=0o700)
                with mock.patch.object(workspace_gc, '_capture',
                                       side_effect=self.failed_capture('capture_failed')) as capture:
                    with self.assertRaises(workspace.WorkspaceError):
                        workspace_gc.cancel(copy, reason='authorized loss', discard_result=True)
                capture.assert_not_called()
                self.assertEqual((outside / 'canary').read_text(), 'preserve')

    def test_nested_bind_mount_blocks_discard_before_capture(self):
        from tests import test_gc_adversarial as adversarial
        adversarial._skip_without_namespace(self, 'mount')
        copy = self.completed()
        source = self.root / 'discard-mounted-canary'
        source.write_text('outside mount must remain\n')
        script = '''
import ctypes, os, sys
from pathlib import Path
from unittest import mock
from codex_deepseek_team import workspace, workspace_gc, workspace_lifecycle
root, identifier = Path(sys.argv[1]), sys.argv[2]
source = root / 'discard-mounted-canary'
target = root / 'state/workspaces' / identifier / 'work/app.txt'
libc = ctypes.CDLL(None, use_errno=True)
if libc.mount(os.fsencode(source), os.fsencode(target), None, 4096, None):
    raise SystemExit('mount errno ' + str(ctypes.get_errno()))
copy = workspace_gc._load_record(root / 'state', identifier)
# This fixture finalizes inside its own observable PID/user namespace. Record
# that namespace's live owner so the separate unobservable-owner refusal does
# not pre-empt the mount guard under test.
lease = workspace_lifecycle.read(copy)
lease['owner'] = workspace_lifecycle.process_identity(os.getpid())
workspace_lifecycle.write(copy, lease)
with mock.patch.object(workspace_gc, '_capture',
                       side_effect=AssertionError('capture ran after mount')):
    try:
        workspace_gc.cancel(copy, reason='authorized loss', discard_result=True)
    except workspace.WorkspaceError as error:
        assert error.code == 73 and 'mount' in error.message.lower(), error.message
    else:
        raise SystemExit('discard crossed a nested mount')
assert target.exists()
print('nested discard mount preserved')
'''
        command = [*adversarial._NAMESPACE_PREFIX['mount'], '--die-with-parent',
            '--unshare-user', '--unshare-pid', '--unshare-net',
            '--cap-add', 'CAP_SYS_ADMIN', '--ro-bind', '/', '/', '--proc', '/proc',
            '--dev', '/dev', '--tmpfs', '/tmp', '--bind', str(self.root), str(self.root),
            '--', '/usr/bin/python3', '-c', script, str(self.root), copy.id]
        result = subprocess.run(
            command, capture_output=True, text=True, timeout=30,
            env=dict(os.environ,
                     PYTHONPATH=str(Path(__file__).resolve().parents[1] / 'src')))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(source.read_text(), 'outside mount must remain\n')
        self.assertTrue(copy.path.exists())
        self.assertFalse((copy.directory / 'result.json').exists())

    def test_active_owner_lock_blocks_discard(self):
        copy = self.completed()
        with copy.lock():
            with self.assertRaises(workspace.WorkspaceError) as caught:
                workspace_gc.cancel(copy, reason='authorized loss', discard_result=True)
        self.assertEqual(caught.exception.code, 75)
        self.assertTrue(copy.path.exists())

    def test_noninteger_lease_result_schema_preserves_execution_data(self):
        for schema in (True, 1.0):
            with self.subTest(schema=schema):
                copy = self.completed(require_namespace=False)
                (copy.directory / 'changes.patch').write_bytes(b'')
                lease = workspace_lifecycle.read(copy)
                lease['finalization'] = {
                    'request_id': 'f' * 32, 'at': 1.0,
                    'cancelled': True, 'discard_result': False,
                    'reason': 'temporary schema regression',
                    'result_schema': schema, 'discarded': False,
                    'capture': {'bytes': 0, 'sha256': _sha(b''),
                                'attempt_id': copy.metadata.get('attempt_id')},
                }
                with workspace_lifecycle.directory(copy) as fd:
                    workspace_lifecycle.atomic_file(
                        fd, 'lease.json', (json.dumps(lease) + '\n').encode())
                with self.assertRaises(workspace.WorkspaceError):
                    workspace_gc.cancel(copy, reason='temporary schema regression')
                self.assertTrue(copy.path.exists())
                self.assertEqual((copy.directory / 'changes.patch').read_bytes(), b'')

    def test_foreign_owner_file_blocks_discard(self):
        copy = self.completed()
        target = copy.path / 'app.txt'
        original = target.stat()
        foreign_uid = (os.geteuid() + 1) % (2 ** 32)
        try:
            os.chown(target, foreign_uid, original.st_gid)
        except OSError as error:
            self.skipTest('host cannot represent a foreign UID: ' + str(error))
        try:
            with mock.patch.object(workspace_gc, '_capture',
                                   side_effect=self.failed_capture('capture_failed')) as capture:
                with self.assertRaises(workspace.WorkspaceError):
                    workspace_gc.cancel(copy, reason='authorized loss', discard_result=True)
            capture.assert_not_called()
            self.assertTrue(copy.path.exists())
        finally:
            os.chown(target, original.st_uid, original.st_gid)

    def test_crash_after_terminal_lease_before_manifest_repairs_without_capture(self):
        for schema in (1, 2):
            with self.subTest(schema=schema):
                copy = self.completed()
                real_atomic = workspace_lifecycle.atomic_file
                crashed = []

                def crash_manifest(target_fd, name, raw):
                    if name == 'result.json' and not crashed:
                        crashed.append(1)
                        raise RuntimeError('fixture crash before result publication')
                    return real_atomic(target_fd, name, raw)

                capture = (mock.patch.object(workspace_gc, '_capture',
                                             return_value=b'captured patch\n')
                           if schema == 1 else
                           mock.patch.object(workspace_gc, '_capture',
                                             side_effect=self.failed_capture('capture_failed')))
                with capture, mock.patch.object(
                        workspace_lifecycle, 'atomic_file', side_effect=crash_manifest):
                    with self.assertRaises(RuntimeError):
                        workspace_gc.cancel(copy, reason='authorized loss',
                                            discard_result=True)
                self.assertTrue(copy.path.exists())
                self.assertFalse((copy.directory / 'result.json').exists())
                lease = workspace_lifecycle.read(copy)
                self.assertIn(lease['phase'], workspace_lifecycle.FINAL_PHASES)
                self.assertEqual(lease['finalization']['result_schema'], schema)
                self.assertEqual(lease['cleanup']['state'], 'pending')
                if schema == 1:
                    self.assertEqual(lease['finalization']['capture']['bytes'],
                                     len(b'captured patch\n'))
                    self.assertNotIn('reason_code', lease['finalization'])
                else:
                    self.assertEqual(lease['finalization']['reason_code'], 'capture_failed')
                    self.assertNotIn('capture', lease['finalization'])
                with mock.patch.object(workspace_gc, '_capture',
                                       side_effect=AssertionError('repeated capture')) as retried:
                    report = workspace_gc.cancel(copy, reason='authorized loss',
                                                 discard_result=True)
                retried.assert_not_called()
                self.assertEqual(report['action'], 'cleaned')
                result = json.loads((copy.directory / 'result.json').read_text())
                self.assertEqual(result['schema'], schema)
                self.assertEqual(result['finalization']['request_id'],
                                 lease['finalization']['request_id'])
                self.assertFalse(copy.path.exists())

    def test_crash_after_terminal_lease_keeps_historical_artifact_provenance(self):
        copy = self.completed()
        attempt_id = workspace_lifecycle.read(copy)['attempts'][-1]['id']
        patch = (copy.directory / 'changes.patch').read_bytes()
        self.retain_prior_result(copy, attempt_id=attempt_id, patch=patch)
        real_atomic = workspace_lifecycle.atomic_file
        crashed = []

        def crash_manifest(fd, name, raw):
            if name == 'result.json' and not crashed:
                crashed.append(1)
                raise RuntimeError('fixture crash before discarded manifest')
            return real_atomic(fd, name, raw)

        with mock.patch.object(workspace_gc, '_capture',
                               side_effect=self.failed_capture('capture_failed')), \
                mock.patch.object(workspace_lifecycle, 'atomic_file',
                                  side_effect=crash_manifest):
            with self.assertRaises(RuntimeError):
                workspace_gc.cancel(copy, reason='authorized historical loss',
                                    discard_result=True)
        self.assertEqual((copy.directory / 'changes.patch').read_bytes(), patch)
        with mock.patch.object(workspace_gc, '_capture',
                               side_effect=AssertionError('repeated capture')) as capture:
            workspace_gc.cancel(copy, reason='authorized historical loss',
                                discard_result=True)
        capture.assert_not_called()
        result = json.loads((copy.directory / 'result.json').read_text())
        self.assertEqual(result['schema'], 2)
        artifact = result['historical_artifacts'][0]
        self.assertEqual(artifact['provenance'], 'verified_attempt')
        self.assertEqual(artifact['attempt_id'], attempt_id)
        self.assertEqual(artifact['sha256'], _sha(patch))
        self.assertEqual((copy.directory / 'changes.patch').read_bytes(), patch)

    def test_crash_after_successful_capture_supersedes_stale_historical_manifest(self):
        copy = self.completed()
        attempt_id = workspace_lifecycle.read(copy)['attempts'][-1]['id']
        old_patch = b'old historical patch\n'
        self.retain_prior_result(copy, attempt_id=attempt_id, patch=old_patch)
        new_patch = b'new verified capture\n'
        real_atomic = workspace_lifecycle.atomic_file
        crashed = []

        def crash_manifest(fd, name, raw):
            if name == 'result.json' and not crashed:
                crashed.append(1)
                raise RuntimeError('fixture crash before fresh manifest')
            return real_atomic(fd, name, raw)

        with mock.patch.object(workspace_gc, '_capture', return_value=new_patch), \
                mock.patch.object(workspace_lifecycle, 'atomic_file',
                                  side_effect=crash_manifest):
            with self.assertRaises(RuntimeError):
                workspace_gc.cancel(copy, reason='cancel with fresh capture')
        self.assertEqual((copy.directory / 'changes.patch').read_bytes(), new_patch)
        with mock.patch.object(workspace_gc, '_capture',
                               side_effect=AssertionError('repeated capture')) as capture:
            report = workspace_gc.cancel(copy, reason='cancel with fresh capture')
        capture.assert_not_called()
        self.assertFalse(report['discarded'])
        result = json.loads((copy.directory / 'result.json').read_text())
        self.assertEqual(result['schema'], 1)
        self.assertEqual(result['patch']['sha256'], _sha(new_patch))
        self.assertEqual((copy.directory / 'changes.patch').read_bytes(), new_patch)
        self.assertFalse(copy.path.exists())

    def test_crash_after_manifest_publication_keeps_result_bytes(self):
        copy = self.completed()
        real_move = workspace_gc.move
        crashed = []

        def crash_once(source_fd, name, destination_fd, target):
            if not crashed:
                crashed.append(1)
                raise RuntimeError('fixture crash after manifest publication')
            return real_move(source_fd, name, destination_fd, target)

        with mock.patch.object(workspace_gc, '_capture',
                               return_value=b'captured patch\n'), \
                mock.patch.object(workspace_gc, 'move', side_effect=crash_once):
            with self.assertRaises(RuntimeError):
                workspace_gc.cancel(copy, reason='cancel with successful capture')
        self.assertTrue(copy.path.exists())
        result_before = (copy.directory / 'result.json').read_bytes()
        patch_before = (copy.directory / 'changes.patch').read_bytes()
        self.assertEqual(json.loads(result_before)['schema'], 1)
        with mock.patch.object(workspace_gc, '_capture',
                               side_effect=AssertionError('repeated capture')) as capture:
            report = workspace_gc.cancel(copy, reason='cancel with successful capture')
        capture.assert_not_called()
        self.assertEqual(report['action'], 'cleaned')
        self.assertFalse(report['discarded'])
        self.assertEqual((copy.directory / 'result.json').read_bytes(), result_before)
        self.assertEqual((copy.directory / 'changes.patch').read_bytes(), patch_before)

    def test_crash_after_rename_resumes_cleanup_without_capture(self):
        copy = self.completed()
        real_remove = workspace_gc._remove_tree
        calls = []

        def interrupted(parent, name, expected):
            if not calls:
                calls.append(1)
                raise OSError('fixture interrupted deletion')
            return real_remove(parent, name, expected)

        with mock.patch.object(workspace_gc, '_capture',
                               side_effect=self.failed_capture('capture_failed')), \
                mock.patch.object(workspace_gc, '_remove_tree', side_effect=interrupted):
            with self.assertRaises(OSError):
                workspace_gc.cancel(copy, reason='authorized loss', discard_result=True)
        self.assertFalse(copy.path.exists())
        with mock.patch.object(workspace_gc, '_capture',
                               side_effect=AssertionError('repeated capture')) as capture:
            report = workspace_gc.cancel(copy, reason='authorized loss', discard_result=True)
        capture.assert_not_called()
        self.assertEqual(report['action'], 'cleaned')
        self.assertTrue(report['discarded'])


class CaptureProtocolTests(DiscardWorkspaceFixture):
    @contextlib.contextmanager
    def _capture_boundary(self, **run):
        import tempfile
        directory = tempfile.TemporaryDirectory(prefix='capture-index-')
        self.addCleanup(directory.cleanup)
        index = Path(directory.name) / 'index'
        index.write_bytes(b'private index fixture')
        @contextlib.contextmanager
        def temporary_index(*arguments, **keywords):
            yield {'GIT_INDEX_FILE': str(index)}
        with mock.patch('codex_deepseek_team.sandbox.probe_backend',
                        return_value=SimpleNamespace(prefix=())), \
                mock.patch.object(git_integrity, 'verify'), \
                mock.patch.object(git_integrity, 'temporary_index',
                                  side_effect=temporary_index), \
                mock.patch.object(workspace_gc.subprocess, 'run', **run):
            yield

    def capture_with(self, copy, **result):
        with self._capture_boundary(return_value=SimpleNamespace(**result)):
            return workspace_gc._capture(copy)

    def test_structured_success_separates_control_from_patch_bytes(self):
        copy = self.completed(require_namespace=False)
        patch = b'patch bytes\n'
        control = _control({'status': 'ok', 'bytes': len(patch), 'sha256': _sha(patch)})
        self.assertEqual(self.capture_with(copy, returncode=0, stdout=patch, stderr=control), patch)

    def test_missing_or_corrupt_protocol_is_not_a_typed_failure(self):
        copy = self.completed(require_namespace=False)
        for stderr in (b'', b'not json', _control({'status': 'ok'})):
            with self.subTest(stderr=stderr):
                with self.assertRaises(workspace.WorkspaceError) as caught:
                    self.capture_with(copy, returncode=73, stdout=b'partial', stderr=stderr)
                self.assertNotIsInstance(caught.exception, workspace_gc.CaptureFailure)

    def test_signal_exit_is_not_a_typed_failure_even_with_error_control(self):
        copy = self.completed(require_namespace=False)
        stderr = _control({'status': 'error', 'code': 'capture_failed', 'step': 'diff'})
        with self.assertRaises(workspace.WorkspaceError) as caught:
            self.capture_with(copy, returncode=-9, stdout=b'', stderr=stderr)
        self.assertNotIsInstance(caught.exception, workspace_gc.CaptureFailure)

    def test_unexpected_oserror_is_not_a_typed_failure(self):
        copy = self.completed(require_namespace=False)
        import errno
        with self._capture_boundary(side_effect=OSError(errno.EIO, 'device failure')):
            with self.assertRaises(OSError):
                workspace_gc._capture(copy)

    def test_git_adapter_io_failure_cannot_authorize_public_discard(self):
        import errno
        for stage in ('administrative_read', 'private_index'):
            with self.subTest(stage=stage):
                copy = self.completed(require_namespace=False)
                patch = (copy.directory / 'changes.patch').read_bytes()
                real_open = os.open
                real_mkdtemp = tempfile.mkdtemp
                injected = []

                def open_file(path, *args, **kwargs):
                    if stage == 'administrative_read' and path == copy.path / '.git' / 'index':
                        injected.append(stage)
                        raise OSError(errno.EIO, 'fixture device failure')
                    return real_open(path, *args, **kwargs)

                def make_directory(*args, **kwargs):
                    if stage == 'private_index' and kwargs.get('prefix') == 'deepseek-git-index-':
                        injected.append(stage)
                        raise OSError(errno.ENOSPC, 'fixture device full')
                    return real_mkdtemp(*args, **kwargs)

                # Both injected adapter failures precede namespace process launch.
                with mock.patch.object(os, 'open', side_effect=open_file), \
                        mock.patch.object(tempfile, 'mkdtemp', side_effect=make_directory), \
                        mock.patch('codex_deepseek_team.sandbox.probe_backend',
                                   return_value=SimpleNamespace(prefix=[])):
                    with self.assertRaises(workspace.WorkspaceError) as caught:
                        workspace_gc.cancel(copy, reason='explicit authorized loss',
                                            discard_result=True)
                self.assertNotIsInstance(caught.exception, workspace_gc.CaptureFailure)
                self.assertEqual(injected, [stage])
                self.assertIn('Git integrity could not be inspected', str(caught.exception))
                self.assertTrue(copy.path.exists())
                self.assertFalse((copy.directory / 'result.json').exists())
                self.assertEqual((copy.directory / 'changes.patch').read_bytes(), patch)

    def test_launch_enoent_is_capture_unavailable(self):
        copy = self.completed(require_namespace=False)
        import errno
        with self._capture_boundary(side_effect=OSError(errno.ENOENT, 'missing')):
            with self.assertRaises(workspace_gc.CaptureFailure) as caught:
                workspace_gc._capture(copy)
        self.assertEqual(caught.exception.reason_code, 'capture_unavailable')

    def test_timeout_is_capture_failed(self):
        copy = self.completed(require_namespace=False)
        with self._capture_boundary(
                side_effect=subprocess.TimeoutExpired('capture', 120)):
            with self.assertRaises(workspace_gc.CaptureFailure) as caught:
                workspace_gc._capture(copy)
        self.assertEqual(caught.exception.reason_code, 'capture_failed')

    def test_output_over_limit_is_capture_too_large(self):
        copy = self.completed(require_namespace=False)
        patch = b'012345678'
        control = _control({'status': 'ok', 'bytes': len(patch), 'sha256': _sha(patch)})
        with mock.patch.object(workspace_gc, 'MAX_PATCH_BYTES', 8):
            with self.assertRaises(workspace_gc.CaptureFailure) as caught:
                self.capture_with(copy, returncode=0, stdout=patch, stderr=control)
        self.assertEqual(caught.exception.reason_code, 'capture_too_large')

    def test_oversized_unknown_capture_preserves_copy_on_public_cancel(self):
        patch = b'012345678'
        error = _control({'status': 'error', 'code': 'capture_failed', 'step': 'diff'})
        cases = [
            (-9, error),
            (7, error),
            (0, b''),
            (0, b'not-json'),
            (0, _control({'status': 'ok', 'bytes': len(patch) - 1,
                          'sha256': _sha(patch)})),
        ]
        for code, control in cases:
            with self.subTest(exit_code=code, control=control):
                copy = self.completed(require_namespace=False)
                with mock.patch.object(workspace_gc, 'MAX_PATCH_BYTES', 8), \
                        self._capture_boundary(return_value=SimpleNamespace(
                            returncode=code, stdout=patch, stderr=control)):
                    with self.assertRaises(workspace.WorkspaceError) as caught:
                        workspace_gc.cancel(copy, reason='temporary protocol regression',
                                            discard_result=True)
                self.assertNotIsInstance(caught.exception, workspace_gc.CaptureFailure)
                self.assertTrue(copy.path.exists())
                self.assertFalse((copy.directory / 'result.json').exists())
                self.assertNotIn(workspace_lifecycle.read(copy)['phase'],
                                 workspace_lifecycle.FINAL_PHASES)

    def test_unknown_failure_step_preserves_copy_on_public_cancel(self):
        for step in (None, [], 'unknown-command'):
            with self.subTest(step=step):
                copy = self.completed(require_namespace=False)
                control = {'status': 'error', 'code': 'capture_failed'}
                if step is not None:
                    control['step'] = step
                with self._capture_boundary(return_value=SimpleNamespace(
                        returncode=1, stdout=b'', stderr=_control(control))):
                    with self.assertRaises(workspace.WorkspaceError) as caught:
                        workspace_gc.cancel(copy, reason='temporary protocol regression',
                                            discard_result=True)
                self.assertNotIsInstance(caught.exception, workspace_gc.CaptureFailure)
                self.assertTrue(copy.path.exists())
                self.assertFalse((copy.directory / 'result.json').exists())

    def test_nonbinary_capture_output_preserves_copy_on_public_cancel(self):
        copy = self.completed(require_namespace=False)
        control = _control({'status': 'ok', 'bytes': 0, 'sha256': _sha(b'')})
        with self._capture_boundary(return_value=SimpleNamespace(
                returncode=0, stdout='unexpected text output', stderr=control)):
            with self.assertRaises(workspace.WorkspaceError) as caught:
                workspace_gc.cancel(copy, reason='temporary protocol regression',
                                    discard_result=True)
        self.assertNotIsInstance(caught.exception, workspace_gc.CaptureFailure)
        self.assertTrue(copy.path.exists())
        self.assertFalse((copy.directory / 'result.json').exists())


class CoordinationDiscardTests(PreparationFinalizationFixture):
    def test_cross_task_discard_never_backs_integration_during_publication_recovery(self):
        parent, assignment = self.new_task()
        copy = self.copy()
        self.run_attempt(parent['id'], assignment, copy, 'succeeded')
        coordination.use_result(self.source, parent['id'], assignment,
                                'needs-rework', 'fixture predecessor inspected')

        def integration_files(task, reference):
            item = {'id': 'integration-probe', 'kind': 'integration',
                    'write_scope': ['app.txt'], 'integration_of': [reference]}
            return coordinator_activity._integration_files(
                self.source, task, item, coordination.WORKER_WRITE_KINDS)

        parent = coordination.load_task(self.source, parent['id'])
        self.assertEqual(integration_files(parent, 'fix'), (['app.txt'], []))
        child, correction = self.new_task(deliverables=[self.deliverable(
            'correction', correction={'task_id': parent['id'],
                                      'assignment_id': assignment})])
        self.run_attempt(child['id'], correction, copy, 'succeeded')
        coordination.use_result(self.source, child['id'], correction,
                                'needs-rework', 'fixture correction inspected')
        config = copy.path / '.git' / 'config'
        config.write_text(config.read_text() + '\n[fixture]\nchanged=true\n')
        with mock.patch.object(coordination, '_publish_job_finalization',
                               side_effect=RuntimeError('fixture publication interruption')):
            with self.assertRaises(RuntimeError):
                coordination.finalize_assignment(
                    self.source, child['id'], correction,
                    'fixture explicitly authorizes uncapturable result loss',
                    cancelled=True, discard_result=True)
        self.assertFalse(copy.path.exists())
        for task_id, identifier, reference in (
                (parent['id'], assignment, 'fix'),
                (child['id'], correction, 'correction')):
            task = coordination.load_task(self.source, task_id)
            row = next(a for a in task['assignments'] if a['id'] == identifier)
            self.assertTrue(row['job_finalization_request']['discard_result'])
            self.assertEqual(row['job_finalization_request']['task_id'], task_id)
            self.assertEqual(row['worker_changes'], ['app.txt'])
            files, issues = integration_files(task, reference)
            self.assertEqual(files, [])
            self.assertTrue(any('discard' in issue.lower() for issue in issues))
        with mock.patch.object(workspace_gc, '_capture',
                               side_effect=AssertionError('repeated capture')):
            self.assertEqual(coordination.complete_task(
                self.source, parent['id'])['status'], 'completed')
        for task_id, reference in ((parent['id'], 'fix'),
                                   (child['id'], 'correction')):
            task = coordination.load_task(self.source, task_id)
            files, issues = integration_files(task, reference)
            self.assertEqual(files, [])
            self.assertTrue(any('discard' in issue.lower() for issue in issues))

    def run_attempt(self, task_id, assignment_id, copy, status):
        with copy.lock(recover=True):
            coordination.assignment_started(
                self.source, task_id, assignment_id, copy.id, 'codex', [])
            copy.begin('execution')
            (copy.path / 'app.txt').write_text(status + '\n')
            copy.finish(status, exit_code=0 if status == 'succeeded' else 9)
            coordination.assignment_finished(
                self.source, task_id, assignment_id, status, 'attempt result',
                ['app.txt'], [], exit_code=copy.metadata['exit_code'])

    def write_result(self, copy, value):
        target = copy.directory / 'result.json'
        target.write_text(json.dumps(value) + '\n')
        target.chmod(0o600)

    def failed_capture(self, code='capture_unavailable'):
        return workspace_gc.CaptureFailure(code, 'fixture typed capture failure')

    def completed(self, content='accepted fix\n'):
        copy = self.copy()
        (copy.path / 'app.txt').write_text(content)
        with copy.lock():
            copy.begin('execution')
            copy.finish('succeeded')
        return copy

    def test_finalize_cancel_discard_records_rejection_and_neutral_closure(self):
        task_id, assignment_id, copy, _row = self.run_preparation_failure(
            dependencies=[{'kind': 'command',
                           'value': 'dst-missing-preparation-command-7f42'}],
            verify_binding=False)
        with mock.patch.object(workspace_gc, '_capture',
                               side_effect=workspace_gc.CaptureFailure(
                                   'git_integrity_mismatch', 'fixture mismatch')):
            task = coordination.finalize_assignment(
                self.source, task_id, assignment_id,
                'user authorized discarding the uncapturable result',
                cancelled=True, discard_result=True)
        row = next(item for item in task['assignments'] if item['id'] == assignment_id)
        self.assertEqual(row['disposition']['kind'], 'rejected')
        self.assertTrue(row['job_finalization']['discarded'])
        self.assertTrue(row['job_finalization']['cancelled'])
        self.assertEqual(row['job_finalization']['reason_code'], 'git_integrity_mismatch')
        self.assertNotIn('job_finalization_request', row)
        self.assertFalse(copy.path.exists())
        result = json.loads((copy.directory / 'result.json').read_text())
        self.assertEqual(result['schema'], 2)
        self.assertEqual(
            coordination.complete_task(self.source, task_id)['status'], 'completed')

    def test_discard_is_refused_for_an_already_accepted_result(self):
        task_id, assignment_id, copy = self.finalizable_failure()
        before = coordination._task_path(self.source, task_id).read_bytes()
        with self.assertRaises(coordination.CoordinationError):
            coordination.finalize_assignment(
                self.source, task_id, assignment_id,
                'authorized loss after acceptance', cancelled=True, discard_result=True)
        self.assertTrue(copy.path.exists())
        self.assertEqual(coordination._task_path(self.source, task_id).read_bytes(), before)

    def test_discard_requires_cancel_and_explicit_evidence(self):
        task_id, assignment_id, copy, _row = self.run_preparation_failure(
            dependencies=[{'kind': 'command',
                           'value': 'dst-missing-preparation-command-7f42'}],
            verify_binding=False)
        with self.assertRaises(coordination.CoordinationError):
            coordination.finalize_assignment(
                self.source, task_id, assignment_id, 'evidence', discard_result=True)
        with self.assertRaises(coordination.CoordinationError):
            coordination.finalize_assignment(
                self.source, task_id, assignment_id, 'cancelled',
                cancelled=True, discard_result=True)
        self.assertTrue(copy.path.exists())

    def test_discard_never_integrates_or_accepts(self):
        task_id, assignment_id, copy, _row = self.run_preparation_failure(
            dependencies=[{'kind': 'command',
                           'value': 'dst-missing-preparation-command-7f42'}],
            verify_binding=False)
        with mock.patch.object(workspace_gc, '_capture',
                               side_effect=workspace_gc.CaptureFailure(
                                   'capture_unavailable', 'fixture sandbox refusal')):
            coordination.finalize_assignment(
                self.source, task_id, assignment_id,
                'authorized loss for rejection', cancelled=True, discard_result=True)
        with self.assertRaises(coordination.CoordinationError):
            coordination.use_result(self.source, task_id, assignment_id,
                                    'incorporated', 'late integration attempt')
        row = self.row(task_id, assignment_id)
        self.assertEqual(row['disposition']['kind'], 'rejected')
        self.assertTrue(row['job_finalization']['discarded'])

    def test_crash_before_ledger_publication_reconciles_without_capture(self):
        task_id, assignment_id, copy, _row = self.run_preparation_failure(
            dependencies=[{'kind': 'command',
                           'value': 'dst-missing-preparation-command-7f42'}],
            verify_binding=False)
        real_publish = coordination._publish_job_finalization
        calls = []

        def crash_once(*arguments, **keywords):
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError('fixture crash before ledger publication')
            return real_publish(*arguments, **keywords)

        with mock.patch.object(workspace_gc, '_capture',
                               side_effect=workspace_gc.CaptureFailure(
                                   'capture_failed', 'fixture controlled failure')), \
                mock.patch.object(coordination, '_publish_job_finalization',
                                  side_effect=crash_once):
            with self.assertRaises(RuntimeError):
                coordination.finalize_assignment(
                    self.source, task_id, assignment_id,
                    'authorized loss before ledger publication',
                    cancelled=True, discard_result=True)
        request = self.row(task_id, assignment_id)['job_finalization_request']
        self.assertTrue(request['discard_result'])
        self.assertFalse(copy.path.exists())
        with mock.patch.object(workspace_gc, '_capture',
                               side_effect=AssertionError('repeated capture')) as capture:
            completed = coordination.complete_task(self.source, task_id)
        capture.assert_not_called()
        row = next(item for item in completed['assignments'] if item['id'] == assignment_id)
        self.assertTrue(row['job_finalization']['discarded'])
        self.assertEqual(row['job_finalization']['evidence'], request['evidence'])
        self.assertEqual(completed['status'], 'completed')

    def test_cli_finalize_requires_cancel_and_discard_is_explicit(self):
        task_id, assignment_id, copy, _row = self.run_preparation_failure(
            dependencies=[{'kind': 'command',
                           'value': 'dst-missing-preparation-command-7f42'}],
            verify_binding=False)
        denied = self.command('coordination', 'finalize', '--path', str(self.source),
                              '--task', task_id, '--assignment', assignment_id,
                              '--evidence', 'authorized loss', '--discard-result')
        self.assertEqual(denied[0], 64)
        self.assertTrue(copy.path.exists())
        with mock.patch.object(workspace_gc, '_capture',
                               side_effect=workspace_gc.CaptureFailure(
                                   'capture_failed', 'fixture controlled failure')):
            accepted = self.command('coordination', 'finalize', '--path', str(self.source),
                                    '--task', task_id, '--assignment', assignment_id,
                                    '--evidence', 'authorized loss', '--cancel',
                                    '--discard-result')
        self.assertEqual(accepted[0], 0, accepted[2])
        self.assertTrue(self.row(task_id, assignment_id)['job_finalization']['discarded'])
        self.assertFalse(copy.path.exists())

    def test_discarded_outcome_context_has_no_current_patch(self):
        task_id, assignment_id, copy, _row = self.run_preparation_failure(
            dependencies=[{'kind': 'command',
                           'value': 'dst-missing-preparation-command-7f42'}],
            verify_binding=False)
        with mock.patch.object(workspace_gc, '_capture',
                               side_effect=workspace_gc.CaptureFailure(
                                   'capture_failed', 'fixture controlled failure')):
            coordination.finalize_assignment(
                self.source, task_id, assignment_id,
                'authorized loss with no current result',
                cancelled=True, discard_result=True)
        coordination.use_result(
            self.source, task_id, assignment_id, 'rejected',
            'quality reassessment after discard', quality={
                'grade': 'unassessable', 'attribution': 'unknown',
                'evidence': 'the result was explicitly discarded'})
        event = lessons.journal(self.source)[-1]
        self.assertIsNone(event['context']['execution']['changes_patch'])
        self.assertFalse(copy.path.exists())

    def test_coordination_discard_flag_keeps_schema_one_when_capture_succeeds(self):
        task_id, assignment_id, copy, _row = self.run_preparation_failure(
            dependencies=[{'kind': 'command',
                           'value': 'dst-missing-preparation-command-7f42'}],
            verify_binding=False)
        patch = b'verified captured patch\n'
        with mock.patch.object(workspace_gc, '_capture', return_value=patch):
            final = coordination.finalize_assignment(
                self.source, task_id, assignment_id,
                'destructive flag authorized but capture succeeded',
                cancelled=True, discard_result=True)
        row = next(item for item in final['assignments']
                   if item['id'] == assignment_id)
        self.assertFalse(row['job_finalization']['discarded'])
        self.assertEqual(row['job_finalization']['result_schema'], 1)
        self.assertNotIn('reason_code', row['job_finalization'])
        result = json.loads((copy.directory / 'result.json').read_text())
        self.assertEqual(result['schema'], 1)
        self.assertEqual(result['patch']['sha256'], _sha(patch))
        self.assertEqual((copy.directory / 'changes.patch').read_bytes(), patch)
        self.assertFalse(copy.path.exists())

    def test_coordination_crash_before_manifest_repairs_without_capture(self):
        task_id, assignment_id, copy, _row = self.run_preparation_failure(
            dependencies=[{'kind': 'command',
                           'value': 'dst-missing-preparation-command-7f42'}],
            verify_binding=False)
        real_atomic = workspace_lifecycle.atomic_file
        crashed = []

        def crash_manifest(fd, name, raw):
            if name == 'result.json' and not crashed:
                crashed.append(1)
                raise RuntimeError('fixture crash before result publication')
            return real_atomic(fd, name, raw)

        with mock.patch.object(workspace_gc, '_capture',
                               side_effect=workspace_gc.CaptureFailure(
                                   'capture_failed', 'fixture controlled failure')), \
                mock.patch.object(workspace_lifecycle, 'atomic_file',
                                  side_effect=crash_manifest):
            with self.assertRaises(RuntimeError):
                coordination.finalize_assignment(
                    self.source, task_id, assignment_id,
                    'authorized loss before manifest publication',
                    cancelled=True, discard_result=True)
        lease = workspace_lifecycle.read(copy)
        self.assertEqual(lease['finalization']['result_schema'], 2)
        self.assertFalse((copy.directory / 'result.json').exists())
        status = self.command('coordination', 'status', '--path', str(self.source),
                              '--task', task_id)
        self.assertNotEqual(status[0], 0)
        with mock.patch.object(workspace_gc, '_capture',
                               side_effect=AssertionError('repeated capture')) as capture:
            final = coordination.finalize_assignment(
                self.source, task_id, assignment_id,
                'authorized loss before manifest publication',
                cancelled=True, discard_result=True)
        capture.assert_not_called()
        row = next(item for item in final['assignments']
                   if item['id'] == assignment_id)
        self.assertTrue(row['job_finalization']['discarded'])
        self.assertEqual(json.loads((copy.directory / 'result.json').read_text())['schema'], 2)
        self.assertFalse(copy.path.exists())

    def test_linked_correction_discard_closes_every_assignment_consistently(self):
        self.assert_linked_discard_closes_chain(accept_parent=False)

    def test_linked_discard_rejects_predecessor_accepted_after_correction_started(self):
        self.assert_linked_discard_closes_chain(accept_parent=True)

    def assert_linked_discard_closes_chain(self, *, accept_parent):
        task, assignment = self.new_task()
        copy = self.copy()
        self.run_attempt(task['id'], assignment, copy, 'failed')
        coordination.use_result(
            self.source, task['id'], assignment, 'needs-rework',
            'first failure reviewed', quality={
                'grade': 'major_gaps', 'attribution': 'worker',
                'evidence': 'the failed attempt needs a correction'})
        self.run_attempt(task['id'], assignment, copy, 'succeeded')
        coordination.use_result(
            self.source, task['id'], assignment, 'needs-rework',
            'second review requires an explicit correction')
        task = coordination.plan_task(self.source, task['id'], {
            'classification': 'substantial', 'deliverables': [
                self.deliverable('fix'),
                self.deliverable('correction', correction={
                    'task_id': task['id'], 'assignment_id': assignment}),
            ]})
        correction = task['assignments'][-1]['id']
        self.run_attempt(task['id'], correction, copy, 'succeeded')
        if accept_parent:
            coordination.use_result(
                self.source, task['id'], assignment, 'incorporated',
                'predecessor reviewed after correction started')
        before_first = dict(coordination.load_task(
            self.source, task['id'])['assignments'][0])
        before_lease = workspace_lifecycle.read(copy)
        with mock.patch.object(workspace_gc, '_capture',
                               side_effect=workspace_gc.CaptureFailure(
                                   'capture_failed', 'fixture controlled failure')):
            final = coordination.finalize_assignment(
                self.source, task['id'], correction,
                'user authorized one discarded logical result',
                cancelled=True, discard_result=True)
        rows = {row['id']: row for row in final['assignments']}
        first, latest = rows[assignment], rows[correction]
        self.assertEqual(first['job_finalization'], latest['job_finalization'])
        self.assertTrue(latest['job_finalization']['discarded'])
        self.assertEqual(latest['disposition']['kind'], 'rejected')
        self.assertEqual(first['disposition']['kind'], 'rejected')
        self.assertEqual(first['disposition_history'][-1], before_first['disposition'])
        self.assertEqual(first['attempt_history'], before_first['attempt_history'])
        self.assertGreaterEqual(len(first['attempt_history']), 1)
        self.assertEqual(first['routing_feedback'][:-1], before_first['routing_feedback'])
        self.assertEqual(first['routing_feedback'][-1]['observation']['quality']['grade'],
                         'unassessable')
        self.assertEqual(workspace_lifecycle.read(copy)['attempts'],
                         before_lease['attempts'])
        self.assertFalse(copy.path.exists())
        self.assertEqual(coordination.complete_task(
            self.source, task['id'])['status'], 'completed')

    def test_unknown_result_schema_blocks_status_complete_and_finalize(self):
        task_id, assignment_id, copy, _row = self.run_preparation_failure(
            dependencies=[{'kind': 'command',
                           'value': 'dst-missing-preparation-command-7f42'}],
            verify_binding=False)
        self.mutate_task(task_id, lambda task: next(
            row for row in task['assignments']
            if row['id'] == assignment_id).update(
                disposition={'kind': 'rejected', 'evidence': 'reviewed',
                             'at': 1.0}))
        self.write_result(copy, {
            'schema': 99, 'workspace_id': copy.id, 'source': str(copy.source),
            'base_head': copy.metadata.get('base_head'), 'patch': None})
        status = self.command('coordination', 'status', '--path', str(self.source),
                              '--task', task_id)
        self.assertNotEqual(status[0], 0)
        self.assertIn('schema', status[2].lower())
        with self.assertRaises(coordination.CoordinationError) as complete:
            coordination.complete_task(self.source, task_id)
        self.assertIn('schema', complete.exception.message.lower())
        with self.assertRaises(coordination.CoordinationError) as finalize:
            coordination.finalize_assignment(
                self.source, task_id, assignment_id, 'final review',
                cancelled=True)
        self.assertIn('schema', finalize.exception.message.lower())
        self.assertTrue(copy.path.exists())

    def test_discarded_history_never_supplies_patch_show_diff_or_lessons(self):
        copy = self.completed()
        patch_before = (copy.directory / 'changes.patch').read_bytes()
        with mock.patch.object(workspace_gc, '_capture',
                               side_effect=self.failed_capture('capture_too_large')):
            workspace_gc.cancel(copy, reason='authorized historical loss',
                                discard_result=True)
        self.assertIsNone(coordination._changes_patch_reference(copy.id))
        show = _run_cli('workspace', 'show', copy.id, '--state-dir', self.state)
        self.assertEqual(show.returncode, 0, show.stderr)
        self.assertIn('discarded', show.stdout.lower())
        self.assertNotIn('Result patch:', show.stdout)
        history = _run_cli('workspace', 'history', copy.id, '--state-dir', self.state)
        self.assertEqual(history.returncode, 0, history.stderr)
        self.assertIn('discarded', history.stdout.lower())
        self.assertNotIn('Result patch:', history.stdout)
        denied = _run_cli('workspace', 'diff', copy.id, '--state-dir', self.state)
        self.assertNotEqual(denied.returncode, 0)
        self.assertEqual((copy.directory / 'changes.patch').read_bytes(), patch_before)
        # An interrupted cleanup that still has an execution tree must not
        # expose the historical bytes either.
        copy.metadata['status'] = 'succeeded'
        copy.save()
        (copy.directory / 'work').mkdir(mode=0o700)
        denied = _run_cli('workspace', 'diff', copy.id, '--state-dir', self.state)
        self.assertNotEqual(denied.returncode, 0)
        shutil.rmtree(copy.directory / 'work')

    def test_workspace_finalize_rejects_the_discard_flag(self):
        copy = self.completed()
        denied = _run_cli('workspace', 'finalize', copy.id, '--state-dir', self.state,
                          '--discard-result', '--reason', 'authorized loss')
        self.assertEqual(denied.returncode, 64)
        self.assertTrue(copy.path.exists())

    def test_linked_workspace_cli_cancel_refuses_before_deletion(self):
        task_id, assignment_id, copy, _row = self.run_preparation_failure(
            dependencies=[{'kind': 'command',
                           'value': 'dst-missing-preparation-command-7f42'}],
            verify_binding=False)
        denied = _run_cli('workspace', 'cancel', copy.id, '--state-dir', self.state,
                          '--discard-result', '--reason', 'authorized loss')
        self.assertEqual(denied.returncode, 64)
        self.assertTrue(copy.path.exists())
        self.assertEqual(self.row(task_id, assignment_id)['status'], 'failed')
        ordinary = _run_cli('workspace', 'cancel', copy.id, '--state-dir', self.state,
                            '--reason', 'ordinary linked cancellation')
        self.assertEqual(ordinary.returncode, 64)
        self.assertTrue(copy.path.exists())
        self.assertFalse((copy.directory / 'result.json').exists())

    def test_real_namespace_success_keeps_schema_one_even_with_discard_flag(self):
        from tests import test_gc_adversarial
        test_gc_adversarial._skip_without_namespace(self, 'capture')
        task_id, assignment_id, copy, _row = self.run_preparation_failure(
            dependencies=[{'kind': 'command',
                           'value': 'dst-missing-preparation-command-7f42'}],
            verify_binding=False)
        final = coordination.finalize_assignment(
            self.source, task_id, assignment_id,
            'capture succeeds despite the authorized destructive flag',
            cancelled=True, discard_result=True)
        row = next(item for item in final['assignments']
                   if item['id'] == assignment_id)
        self.assertFalse(row['job_finalization']['discarded'])
        self.assertEqual(row['job_finalization']['result_schema'], 1)
        result = json.loads((copy.directory / 'result.json').read_text())
        self.assertEqual(result['schema'], 1)
        self.assertIsNotNone(result['patch'])
        self.assertFalse(copy.path.exists())


if __name__ == '__main__':
    unittest.main()
