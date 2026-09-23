"""Command-line boundary tests for evidence-backed routing."""
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest import mock

from codex_deepseek_team import coordination_cli, routing_cli


class FakeRoutingService:
    instances = []
    export_failure = False

    def __init__(self, root):
        self.root = root
        self.calls = []
        self.__class__.instances.append(self)

    def status(self):
        self.calls.append(('status',))
        return {'mode': 'auto', 'observations': 2}

    def config(self):
        self.calls.append(('config',))
        return {'mode': 'auto', 'require_cost_evidence': True}

    def configure(self, changes):
        self.calls.append(('configure', changes))
        return dict(changes)

    def import_snapshot(self, data, source_id, source_url, sha256, reliability=0.5):
        self.calls.append(('import_snapshot', data, source_id, source_url, sha256, reliability))
        return {'source': source_id, 'imported': 1}

    def predict(self, features, access=None, record=True):
        self.calls.append(('predict', features, access, record))
        return {'action': 'abstain', 'reason_codes': ['insufficient_evidence']}

    def observe(self, record):
        self.calls.append(('observe', record))
        return {'id': record['id'], 'outcome': record['outcome']}

    def evaluate(self):
        self.calls.append(('evaluate',))
        return {'cases': 2, 'coverage': 0.5}

    def export(self):
        self.calls.append(('export',))
        return {'schema_version': 1, 'observations': []}

    def export_to(self, stream):
        self.calls.append(('export_to',))
        if self.export_failure:
            stream.write('{"partial":')
            raise routing_cli.RoutingError('Synthetic export interruption.')
        json.dump({'schema_version': 1, 'observations': []}, stream,
                  indent=2, sort_keys=True, allow_nan=False)
        stream.write('\n')

    def reserve_experiment(self, identifier, amount_usd):
        self.calls.append(('reserve_experiment', identifier, amount_usd))
        return {'id': identifier, 'status': 'reserved', 'amount_usd': amount_usd}

    def settle_experiment(self, identifier, actual_usd):
        self.calls.append(('settle_experiment', identifier, actual_usd))
        return {'id': identifier, 'status': 'settled', 'actual_usd': actual_usd}

    def release_experiment(self, identifier):
        self.calls.append(('release_experiment', identifier))
        return {'id': identifier, 'status': 'released'}

    def recovery_status(self):
        self.calls.append(('recovery_status',))
        return {'pending': 1, 'tickets': [{'id': 'recovery-1', 'status': 'pending'}]}

    def release_recovery(self, identifier):
        self.calls.append(('release_recovery', identifier))
        return {'id': identifier, 'status': 'released'}


class RoutingCliTests(unittest.TestCase):
    def setUp(self):
        FakeRoutingService.instances.clear()
        FakeRoutingService.export_failure = False
        self.service = mock.patch.object(routing_cli, 'RoutingService', FakeRoutingService)
        self.service.start()
        self.addCleanup(self.service.stop)

    def invoke(self, argv, stdin=''):
        with mock.patch('sys.stdin', io.StringIO(stdin)), \
             mock.patch('sys.stdout', new_callable=io.StringIO) as output, \
             mock.patch('sys.stderr', new_callable=io.StringIO) as errors:
            code = routing_cli.main(argv)
        return code, output.getvalue(), errors.getvalue()

    def test_status_uses_explicit_project_and_emits_json(self):
        code, output, errors = self.invoke(['routing', 'status', '--path', '/tmp/project', '--json'])
        self.assertEqual((code, errors), (0, ''))
        self.assertEqual(json.loads(output), {'mode': 'auto', 'observations': 2})
        self.assertEqual(FakeRoutingService.instances[-1].root, Path('/tmp/project'))

    def test_configure_converts_supported_settings(self):
        code, output, errors = self.invoke([
            'routing', 'configure', '--path', '/tmp/project', '--mode', 'auto',
            '--min-local-evidence', '3', '--external-weight-cap', '4.5',
            '--recovery-rate', '0.2', '--recovery-cooldown-seconds', '7200',
            '--no-require-cost-evidence',
        ])
        self.assertEqual((code, errors), (0, ''))
        self.assertEqual(json.loads(output), {
            'mode': 'auto', 'min_local_evidence': 3.0,
            'external_weight_cap': 4.5, 'require_cost_evidence': False,
            'recovery_rate': 0.2, 'recovery_cooldown_seconds': 7200.0,
        })

    def test_recommend_reads_one_json_object_from_stdin(self):
        features = {'kind': 'implementation', 'risk': 'low'}
        code, output, errors = self.invoke(
            ['routing', 'recommend', '--path', '/tmp/project', '--access', 'read-only'],
            json.dumps(features),
        )
        self.assertEqual((code, errors), (0, ''))
        self.assertEqual(json.loads(output)['action'], 'abstain')
        self.assertEqual(FakeRoutingService.instances[-1].calls,
                         [('predict', features, 'read-only', True)])

    def test_observe_reads_json_from_file(self):
        record = {'id': 'obs-1', 'outcome': 'accepted'}
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'observation.json'
            source.write_text(json.dumps(record), encoding='utf-8')
            code, output, errors = self.invoke([
                'routing', 'observe', '--path', directory, '--file', str(source),
            ])
        self.assertEqual((code, errors), (0, ''))
        self.assertEqual(json.loads(output), record)
        self.assertEqual(FakeRoutingService.instances[-1].calls, [('observe', record)])

    def test_malformed_input_does_not_echo_payload_or_traceback(self):
        secret = 'secret-token-value'
        code, output, errors = self.invoke(
            ['routing', 'recommend', '--path', '/tmp/project'], '{"token":"' + secret,
        )
        self.assertEqual(code, 64)
        self.assertEqual(output, '')
        error = json.loads(errors)
        self.assertEqual(error['code'], 64)
        self.assertNotIn(secret, errors)
        self.assertNotIn('Traceback', errors)

    def test_json_input_is_size_bounded(self):
        oversized = 'x' * (routing_cli.MAX_JSON_BYTES + 1)
        code, output, errors = self.invoke(
            ['routing', 'observe', '--path', '/tmp/project'], oversized,
        )
        self.assertEqual(code, 64)
        self.assertEqual(output, '')
        self.assertIn('too large', json.loads(errors)['error'].lower())
        self.assertEqual(FakeRoutingService.instances, [])

    def test_import_reads_local_bytes_without_network(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'snapshot.json'
            source.write_bytes(b'{"schema_version":1}')
            with mock.patch.object(routing_cli, 'fetch_snapshot') as fetch:
                code, output, errors = self.invoke([
                    'routing', 'import', '--path', directory, str(source),
                    '--source-id', 'public-run', '--source-url', 'https://example.test/run.json',
                    '--sha256', 'a' * 64, '--reliability', '0.7',
                ])
        self.assertEqual((code, errors), (0, ''))
        self.assertEqual(json.loads(output), {'source': 'public-run', 'imported': 1})
        fetch.assert_not_called()
        self.assertEqual(FakeRoutingService.instances[-1].calls[0], (
            'import_snapshot', b'{"schema_version":1}', 'public-run',
            'https://example.test/run.json', 'a' * 64, 0.7,
        ))

    def test_fetch_is_explicit_pinned_and_then_imported(self):
        payload = b'{"schema_version":1}'
        with mock.patch.object(routing_cli, 'fetch_snapshot', return_value=payload) as fetch:
            code, output, errors = self.invoke([
                'routing', 'fetch', '--path', '/tmp/project', 'https://example.test/run.json',
                '--source-id', 'public-run', '--sha256', 'b' * 64,
            ])
        self.assertEqual((code, errors), (0, ''))
        self.assertEqual(json.loads(output), {'source': 'public-run', 'imported': 1})
        fetch.assert_called_once_with('https://example.test/run.json')
        self.assertEqual(FakeRoutingService.instances[-1].calls[0][0], 'import_snapshot')

    def test_fetch_rejects_invalid_pin_before_network_access(self):
        with mock.patch.object(routing_cli, 'fetch_snapshot') as fetch:
            code, output, errors = self.invoke([
                'routing', 'fetch', '--path', '/tmp/project', 'https://example.test/run.json',
                '--source-id', 'public-run', '--sha256', 'not-a-digest',
            ])
        self.assertEqual(code, 64)
        self.assertEqual(output, '')
        self.assertIn('sha-256', json.loads(errors)['error'].lower())
        fetch.assert_not_called()
        self.assertEqual(FakeRoutingService.instances, [])

    def test_argument_errors_are_structured_and_do_not_fetch(self):
        with mock.patch.object(routing_cli, 'fetch_snapshot') as fetch:
            code, output, errors = self.invoke([
                'routing', 'fetch', '--path', '/tmp/project', 'https://example.test/run.json',
                '--source-id', 'public-run',
            ])
        self.assertEqual(code, 64)
        self.assertEqual(output, '')
        self.assertEqual(json.loads(errors)['code'], 64)
        self.assertNotIn('Traceback', errors)
        fetch.assert_not_called()

    def test_export_streams_json_to_stdout_without_materializing_service_export(self):
        code, output, errors = self.invoke([
            'routing', 'export', '--path', '/tmp/project',
        ])
        self.assertEqual((code, errors), (0, ''))
        self.assertEqual(json.loads(output), {'schema_version': 1, 'observations': []})
        self.assertEqual(FakeRoutingService.instances[-1].calls, [('export_to',)])

    def test_export_atomically_writes_private_file_and_keeps_stdout_structured(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / 'routing-export.json'
            destination.write_text('old export', encoding='utf-8')
            code, output, errors = self.invoke([
                'routing', 'export', '--path', directory, '--output', str(destination),
            ])
            saved = json.loads(destination.read_text(encoding='utf-8'))
            mode = destination.stat().st_mode & 0o777
        self.assertEqual((code, errors), (0, ''))
        self.assertEqual(saved, {'schema_version': 1, 'observations': []})
        self.assertEqual(mode, 0o600)
        self.assertEqual(json.loads(output), {'output': str(destination)})
        self.assertEqual(FakeRoutingService.instances[-1].calls, [('export_to',)])

    def test_export_error_preserves_existing_file_and_removes_private_temporary(self):
        FakeRoutingService.export_failure = True
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / 'routing-export.json'
            destination.write_text('stable export', encoding='utf-8')
            code, output, errors = self.invoke([
                'routing', 'export', '--path', directory, '--output', str(destination),
            ])
            self.assertEqual(destination.read_text(encoding='utf-8'), 'stable export')
            self.assertEqual([entry.name for entry in Path(directory).iterdir()],
                             ['routing-export.json'])
        self.assertEqual(code, 64)
        self.assertEqual(output, '')
        self.assertEqual(json.loads(errors)['error'], 'Synthetic export interruption.')

    def test_export_refuses_existing_symlink_without_touching_target(self):
        with tempfile.TemporaryDirectory() as directory:
            victim = Path(directory) / 'victim.json'
            victim.write_text('stable target', encoding='utf-8')
            destination = Path(directory) / 'routing-export.json'
            destination.symlink_to(victim)
            code, output, errors = self.invoke([
                'routing', 'export', '--path', directory, '--output', str(destination),
            ])
            self.assertTrue(destination.is_symlink())
            self.assertEqual(victim.read_text(encoding='utf-8'), 'stable target')
        self.assertEqual(code, 64)
        self.assertEqual(output, '')
        self.assertIn('regular', json.loads(errors)['error'].lower())
        self.assertEqual(FakeRoutingService.instances[-1].calls, [])

    def test_export_refuses_hardlinked_and_nonregular_targets(self):
        with tempfile.TemporaryDirectory() as directory:
            original = Path(directory) / 'original.json'
            original.write_text('stable', encoding='utf-8')
            hardlink = Path(directory) / 'hardlink.json'
            os.link(original, hardlink)
            code, output, errors = self.invoke([
                'routing', 'export', '--path', directory, '--output', str(hardlink),
            ])
            self.assertEqual((code, output), (64, ''))
            self.assertIn('hard link', json.loads(errors)['error'].lower())
            self.assertEqual(original.read_text(encoding='utf-8'), 'stable')

            nonregular = Path(directory) / 'directory-target'
            nonregular.mkdir()
            code, output, errors = self.invoke([
                'routing', 'export', '--path', directory, '--output', str(nonregular),
            ])
            self.assertEqual((code, output), (64, ''))
            self.assertIn('regular', json.loads(errors)['error'].lower())
            self.assertTrue(nonregular.is_dir())

        self.assertTrue(all(not service.calls for service in FakeRoutingService.instances))

    def test_budget_commands_emit_service_results(self):
        cases = [
            (['reserve', 'trial-1', '--amount-usd', '1.25'], 'reserved'),
            (['settle', 'trial-1', '--actual-usd', '1.50'], 'settled'),
            (['release', 'trial-1'], 'released'),
        ]
        for arguments, expected in cases:
            with self.subTest(command=arguments[0]):
                code, output, errors = self.invoke(
                    ['routing', 'budget', *arguments, '--path', '/tmp/project'])
                self.assertEqual((code, errors), (0, ''))
                self.assertEqual(json.loads(output)['status'], expected)

    def test_recovery_status_uses_explicit_project_and_emits_service_result(self):
        code, output, errors = self.invoke([
            'routing', 'recovery', 'status', '--path', '/tmp/project',
        ])
        self.assertEqual((code, errors), (0, ''))
        self.assertEqual(json.loads(output), {
            'pending': 1, 'tickets': [{'id': 'recovery-1', 'status': 'pending'}],
        })
        service = FakeRoutingService.instances[-1]
        self.assertEqual(service.root, Path('/tmp/project'))
        self.assertEqual(service.calls, [('recovery_status',)])

    def test_recovery_release_passes_ticket_identity_to_service(self):
        code, output, errors = self.invoke([
            'routing', 'recovery', 'release', 'recovery-1', '--path', '/tmp/project',
        ])
        self.assertEqual((code, errors), (0, ''))
        self.assertEqual(json.loads(output), {'id': 'recovery-1', 'status': 'released'})
        self.assertEqual(FakeRoutingService.instances[-1].calls,
                         [('release_recovery', 'recovery-1')])


class CoordinationCliTests(unittest.TestCase):
    def invoke(self, argv):
        with mock.patch('sys.stdout', new_callable=io.StringIO) as output, \
             mock.patch('sys.stderr', new_callable=io.StringIO) as errors:
            code = coordination_cli.main(argv)
        return code, output.getvalue(), errors.getvalue()

    def test_abandon_dispatches_explicit_stopped_attestation_and_prints_summary(self):
        task = {'id': 'task-1'}
        with mock.patch.object(coordination_cli.coordination, 'abandon_assignment',
                               create=True, return_value=task) as abandon, \
             mock.patch.object(coordination_cli.coordination, 'summary',
                               return_value='Task task-1: cancelled'):
            code, output, errors = self.invoke([
                'coordination', 'abandon', '--path', '/tmp/project',
                '--task', 'task-1', '--assignment', 'assignment-1',
                '--evidence', 'Inspected diff; process group is stopped.',
                '--confirmed-stopped',
            ])
        self.assertEqual((code, output, errors), (0, 'Task task-1: cancelled\n', ''))
        abandon.assert_called_once_with(
            Path('/tmp/project'), 'task-1', 'assignment-1',
            'Inspected diff; process group is stopped.', confirmed_stopped=True)

    def test_abandon_requires_stopped_attestation_before_backend_call(self):
        with mock.patch.object(coordination_cli.coordination, 'abandon_assignment',
                               create=True) as abandon, \
             mock.patch('sys.stderr', new_callable=io.StringIO) as errors:
            with self.assertRaises(SystemExit) as stopped:
                coordination_cli.main([
                    'coordination', 'abandon', '--path', '/tmp/project',
                    '--task', 'task-1', '--assignment', 'assignment-1',
                    '--evidence', 'Inspected diff only.',
                ])
        self.assertEqual(stopped.exception.code, 2)
        self.assertIn('--confirmed-stopped', errors.getvalue())
        abandon.assert_not_called()


class RoutingCliServiceIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / 'project'
        self.root.mkdir()
        subprocess.run(['git', 'init', '-q', str(self.root)], check=True)
        environment = mock.patch.dict(
            os.environ,
            DEEPSEEK_TEAM_STATE_DIR=str(Path(self.tmp.name) / 'state'),
            XDG_CONFIG_HOME=str(Path(self.tmp.name) / 'config'),
        )
        environment.start()
        self.addCleanup(environment.stop)

    def invoke(self, argv, stdin=''):
        with mock.patch('sys.stdin', io.StringIO(stdin)), \
             mock.patch('sys.stdout', new_callable=io.StringIO) as output, \
             mock.patch('sys.stderr', new_callable=io.StringIO) as errors:
            code = routing_cli.main(argv)
        return code, output.getvalue(), errors.getvalue()

    def test_configure_and_config_use_persistent_service(self):
        code, output, errors = self.invoke([
            'routing', 'configure', '--path', str(self.root), '--mode', 'shadow',
            '--confidence', '0.9',
        ])
        self.assertEqual((code, errors), (0, ''))
        self.assertEqual(json.loads(output)['mode'], 'shadow')

        code, output, errors = self.invoke([
            'routing', 'config', '--path', str(self.root), '--json',
        ])
        self.assertEqual((code, errors), (0, ''))
        self.assertEqual(json.loads(output)['confidence'], 0.9)

    def test_import_uses_real_source_parser_and_store(self):
        observed_at = int(time.time())
        snapshot = {
            'schema_version': 1,
            'format': 'deepseek-team-evidence',
            'source_family': 'synthetic-cli-test',
            'observations': [{
                'id': 'external-observation-1',
                'case_id': 'external-case-1',
                'source_family': 'synthetic-cli-test',
                'features': {'kind': 'test', 'domain': 'python'},
                'action': 'worker',
                'outcome': 'accepted',
                'observed_at': observed_at,
            }],
        }
        raw = json.dumps(snapshot).encode()
        source = Path(self.tmp.name) / 'snapshot.json'
        source.write_bytes(raw)
        code, output, errors = self.invoke([
            'routing', 'import', '--path', str(self.root), str(source),
            '--source-id', 'synthetic-cli-test-v1',
            '--source-url', 'https://example.test/snapshot.json',
            '--sha256', hashlib.sha256(raw).hexdigest(),
        ])
        self.assertEqual((code, errors), (0, ''))
        self.assertEqual(json.loads(output)['added'], 1)

    def test_export_streams_real_service_format(self):
        code, output, errors = self.invoke([
            'routing', 'export', '--path', str(self.root),
        ])
        self.assertEqual((code, errors), (0, ''))
        exported = json.loads(output)
        self.assertEqual(exported['schema_version'], 1)
        self.assertEqual(exported['format'], 'deepseek-team-routing-export')
        self.assertEqual(exported['observations'], [])
        self.assertEqual(exported['sources'], [])


if __name__ == '__main__':
    unittest.main()
