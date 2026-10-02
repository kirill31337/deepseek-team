"""Incident regressions at real namespace, CLI and persistent-ledger boundaries."""
import io
import json
import os
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from codex_deepseek_team import (coordination, delegation_cli, development, doctor,
                               managed, runtime_preflight, settings, toolchains,
                               worker, workspace)
from codex_deepseek_team.routing import RoutingService
from tests.test_coordination import CoordinationCase
from tests.test_live_delegation import LiveBase


class NamespaceReadinessTests(LiveBase):
    def assignment(self, check):
        settings.set_values(self.source / settings.PROJECT_FILE,
                            delegation_level='auto', access='full-access')
        policy = settings.resolve(self.source)
        task = coordination.open_task(self.source, session_id='readiness',
            turn_id='1', prompt='fix calc.py', policy=policy, runtime='claude')
        features = dict(kind='implementation', domain='python', operation='fix',
            localization='known', coupling='local', verification='tests', clarity='clear',
            risk='low', scope_size='small', runtime='claude', model='deepseek-flash',
            effort='high', context_version='readiness-v1')
        task = coordination.plan_task(self.source, task['id'], {
            'classification': 'substantial', 'deliverables': [{
                'id': 'fix', 'kind': 'implementation', 'scope': ['calc.py'],
                'executor': 'auto', 'acceptance': ['sum fixed'],
                'dependencies': [], 'checks': [check], 'features': features}]})
        return policy, task['id'], task['assignments'][0]['id']

    def test_red_baseline_does_not_prevent_a_successful_bugfix(self):
        check = 'python3 -c "from calc import add; assert add(2,3)==5"'
        policy, tid, aid = self.assignment(check)
        copy, sibling = [workspace.create(self.source, self.state) for _ in range(2)]
        result = managed.run(self.args(self.task('fix', sibling), coord_task=tid,
                             coord_assignment=aid), policy, worker, copy)
        self.assertEqual(result, 0)
        row = coordination.load_task(self.source, tid)['assignments'][0]
        self.assertNotEqual(row.get('baseline_checks', [{}])[0].get('exit_code'), 0)
        self.assertEqual(row['checks'][0]['exit_code'], 0)
        self.assertIn('test_calc.py', row['worker_changes'])
        baseline = row['baseline_checks'][0]
        self.assertIn(b'AssertionError',
            (copy.directory / 'diagnostics' / baseline['stderr_path']).read_bytes())

    def test_verify_only_preserves_failure_and_never_reads_provider_credentials(self):
        check = 'python3 -c "from pathlib import Path; assert Path(\'ready.flag\').exists()"'
        policy, tid, aid = self.assignment(check)
        copy, sibling = [workspace.create(self.source, self.state) for _ in range(2)]
        self.assertEqual(managed.run(self.args(self.task('fix', sibling),
            coord_task=tid, coord_assignment=aid), policy, worker, copy), 65)
        before = workspace.load(self.state, copy.id).metadata
        original = coordination.load_task(self.source, tid)['assignments'][0]
        (copy.path / 'ready.flag').write_text('prepared by coordinator')
        verify = getattr(workspace, 'verify_checks', None)
        if verify is None:
            self.fail('provider-free workspace verification is unavailable')
        with patch.object(worker, 'load_api_key', side_effect=AssertionError('provider credential read')), \
                patch.object(worker, 'execute', side_effect=AssertionError('model rerun')), \
                patch('codex_deepseek_team.relay.ProviderRelay',
                      side_effect=AssertionError('provider relay started')):
            result = verify(copy)
        self.assertEqual(result['status'], 'passed')
        after = workspace.load(self.state, copy.id).metadata
        for key in ('status', 'error_kind', 'exit_code', 'attempt'):
            self.assertEqual(after[key], before[key], key)
        row = coordination.load_task(self.source, tid)['assignments'][0]
        for key in ('status', 'error_kind', 'exit_code', 'checks', 'worker_changes'):
            self.assertEqual(row[key], original[key], key)
        self.assertEqual(row['verification_runs'][-1]['status'], 'passed')

    def test_failing_environment_smoke_probe_stops_before_provider_key(self):
        copy = workspace.create(self.source, self.state)
        toolchains.prepare(copy, probes=['python3 -c "raise SystemExit(7)"'])
        with patch.object(worker, 'load_api_key', side_effect=AssertionError('provider key read')):
            with self.assertRaises(worker.WorkerError) as caught:
                managed.run(self.args('bounded analysis'), self.policy(50), worker, copy)
        self.assertEqual(caught.exception.code, 78)
        record = workspace.load(self.state, copy.id).metadata
        self.assertEqual(record['status'], 'failed')
        self.assertEqual(record['verification_cause'], 'probe_failure')
        self.assertEqual(record['readiness_checks'][0]['exit_code'], 7)

    def test_verify_retries_default_runtime_candidates_but_preserves_explicit_pin(self):
        binaries = self.root / 'available-runtime'
        binaries.mkdir()
        launcher = binaries / 'claude'
        launcher.write_text(self.driver.read_text())
        launcher.chmod(0o700)
        for explicit in (False, True):
            with self.subTest(explicit=explicit):
                copy = workspace.create(self.source, self.state)
                copy.metadata.update(runtime='claude', runtime_binary=str(self.root / 'removed-claude'),
                                     runtime_explicit=explicit)
                copy.save()
                with patch.dict(os.environ, {'PATH': str(binaries) + ':/usr/bin:/bin'}), \
                        patch.object(worker, 'load_api_key', side_effect=AssertionError('key read')):
                    if explicit:
                        with self.assertRaises(runtime_preflight.RuntimePreflightError):
                            workspace.verify_checks(copy, ['python3 -c "pass"'])
                    else:
                        self.assertEqual(workspace.verify_checks(copy, ['python3 -c "pass"'])['status'],
                                         'passed')

    def test_verification_publication_replays_after_canonical_workspace_save(self):
        policy, tid, aid = self.assignment('python3 -c "pass"')
        copy = workspace.create(self.source, self.state)
        coordination.assignment_started(self.source, tid, aid, copy.id, 'claude', [], effort='high')
        coordination.assignment_finished(self.source, tid, aid, 'failed', 'original failed attempt',
            [], [{'command': 'python3', 'exit_code': 7}], error_kind='verification', exit_code=65)
        copy.metadata.update(runtime='claude', runtime_binary=str(self.driver), runtime_explicit=True,
                             coord_task=tid, coord_assignment=aid,
                             verification_commands=['python3 -c "pass"'])
        copy.save()
        with patch.object(coordination, 'record_verification', side_effect=OSError('publication interrupted')):
            with self.assertRaises(OSError):
                workspace.verify_checks(copy)
        saved = workspace.load(self.state, copy.id)
        first = saved.metadata['verification_runs'][0]
        self.assertEqual(coordination.load_task(self.source, tid)['assignments'][0]['verification_runs'], [])
        workspace.verify_checks(saved)
        row = coordination.load_task(self.source, tid)['assignments'][0]
        self.assertEqual(len(row['verification_runs']), 2)
        self.assertEqual(row['verification_runs'][0], first)
        self.assertEqual(row['status'], 'failed')
        self.assertFalse(saved.metadata.get('pending_verifications'))

    def test_verify_refuses_another_live_workspace_owner_without_changing_metadata(self):
        copy = workspace.create(self.source, self.state)
        other = workspace.load(self.state, copy.id)
        before = dict(copy.metadata)
        with copy.lock():
            with self.assertRaises(workspace.WorkspaceError) as caught:
                workspace.verify_checks(other, ['python3 -c "pass"'])
        self.assertEqual(caught.exception.code, 75)
        self.assertEqual(workspace.load(self.state, copy.id).metadata, before)

    def test_offline_doctor_rejects_host_only_wrapper_in_real_namespace(self):
        target = self.root / 'host-only-runtime'
        target.write_text('#!/bin/sh\necho "--bare --tools --allowedTools --disallowedTools '
            '--permission-mode dontAsk --disable-slash-commands --setting-sources '
            '--strict-mcp-config --mcp-config --no-session-persistence --output-format"\n')
        target.chmod(0o700)
        binaries = self.root / 'doctor-bin'; binaries.mkdir()
        launcher = binaries / 'claude'
        launcher.write_text('#!/bin/sh\nexec "' + str(target) + '" "$@"\n')
        launcher.chmod(0o700)
        with patch.dict(os.environ, {'PATH': str(binaries) + ':/usr/bin:/bin'}), \
                patch.object(worker, 'load_api_key', side_effect=AssertionError('provider key read')):
            with self.assertRaises(worker.WorkerError):
                doctor.check_policy_runtime('claude', self.policy(50))

    def test_prepared_ignored_config_changes_stay_out_of_source_diff(self):
        source_input = self.root / 'local.properties'
        source_input.write_text('sdk.dir=prepared\n')
        copy = workspace.create(self.source, self.state)
        toolchains.prepare(copy, copies=[(source_input, 'local.properties')])
        fixture = self.root / 'configuration-runtime'
        flags = ' '.join(runtime_preflight.CAPABILITIES['claude'][1])
        fixture.write_text('#!/usr/bin/env python3\nimport json,sys\nfrom pathlib import Path\n'
            'if "--version" in sys.argv: print("fixture 1.0"); raise SystemExit(0)\n'
            f'if "--help" in sys.argv: print({flags!r}); raise SystemExit(0)\n'
            'sys.stdin.read()\nPath("local.properties").write_text("sdk.dir=changed\\n")\n'
            'print(json.dumps({"type":"result","is_error":False,"result":"configuration changed"}))\n')
        fixture.chmod(0o700)
        self.assertEqual(managed.run(self.args('inspect configuration', claude=str(fixture)),
            self.policy(50), worker, copy), 0)
        metadata = workspace.load(self.state, copy.id).metadata
        self.assertEqual(metadata['prepared_input_changes'], ['local.properties'])
        self.assertNotIn('local.properties', metadata['changed_files'])
        self.assertNotIn('sdk.dir', copy.diff())
        self.assertEqual(source_input.read_text(), 'sdk.dir=prepared\n')

    def test_real_java_security_readonly_tools_and_persistent_cache(self):
        jdk = Path('/usr/lib/jvm/java-17-openjdk-amd64')
        if not (jdk / 'bin/javac').is_file():
            self.skipTest('Stock Debian OpenJDK17 is not installed on this host.')
        copy = workspace.create(self.source, self.state)
        cache = copy.path / 'build/gradle-cache'
        cache.mkdir(parents=True)
        toolchains.prepare(copy, tools={'jdk': jdk}, environment={
            'JAVA_HOME': '{workspace}/.deepseek-tools/jdk',
            'PATH': '{workspace}/.deepseek-tools/jdk/bin',
            'GRADLE_USER_HOME': '{workspace}/build/gradle-cache'},
            probes=['java -version', 'javac -version'])
        code = copy.path / 'build/check-java/VerifySecurity.java'
        code.parent.mkdir()
        code.write_text('import java.security.*; import javax.net.ssl.*;\n'
            'public class VerifySecurity { public static void main(String[] args) throws Exception {\n'
            'if (Security.getProviders().length == 0) throw new AssertionError();\n'
            'TrustManagerFactory f = TrustManagerFactory.getInstance(TrustManagerFactory.getDefaultAlgorithm());\n'
            'f.init((KeyStore)null); if (f.getTrustManagers().length == 0) throw new AssertionError();\n'
            'System.out.println("JAVA_SECURITY_READY"); }}\n')
        commands = [
            'python3 -c "import os; from pathlib import Path; '
            'p=Path(os.environ[\'JAVA_HOME\'])/\'bin/java\'; '
            'exec(\'try:\\n p.write_bytes(b\\\"forbidden\\\")\\nexcept OSError:\\n pass\\nelse:\\n raise AssertionError(\\\"tool writable\\\")\'); '
            'Path(os.environ[\'GRADLE_USER_HOME\'],\'kept\').write_text(\'cache\')"',
            'javac -d build/check-java build/check-java/VerifySecurity.java && '
            'java -cp build/check-java VerifySecurity']
        before = dict(copy.metadata)
        with patch.object(worker, 'load_api_key', side_effect=AssertionError('credential read')), \
                patch.object(worker, 'execute', side_effect=AssertionError('model called')):
            result = workspace.verify_checks(copy, commands)
            self.assertEqual(result['status'], 'passed', result)
            self.assertIn(b'JAVA_SECURITY_READY', (copy.directory / 'diagnostics' /
                result['checks'][1]['stdout_path']).read_bytes())
            again = workspace.verify_checks(copy, ['test -f "$GRADLE_USER_HOME/kept"'])
            self.assertEqual(again['status'], 'passed', again)
        self.assertEqual(copy.metadata['attempt'], before['attempt'])
        self.assertEqual(copy.metadata['status'], before['status'])
        self.assertNotIn('.deepseek-tools', copy.diff())

    def test_host_successful_explicit_wrapper_fails_before_provider_key_read(self):
        target = self.root / 'host-only/runtime-real'
        target.parent.mkdir()
        target.write_text(
            '#!/bin/sh\n'
            'printf "%s\\n" "--bare --tools --allowedTools --disallowedTools '
            '--permission-mode dontAsk --disable-slash-commands --setting-sources '
            '--strict-mcp-config --mcp-config --no-session-persistence --output-format"\n')
        target.chmod(0o700)
        launcher = self.root / 'launcher/claude'
        launcher.parent.mkdir()
        launcher.write_text('#!/bin/sh\nexec "' + str(target) + '" "$@"\n')
        launcher.chmod(0o700)
        copy = workspace.create(self.source, self.state)
        marker = self.root / 'provider-key-was-read'
        policy, tid, aid = self.assignment('python3 -c "pass"')

        def key_reader():
            marker.write_text('read')
            return 'disposable-test-credential'

        result = None
        with patch.object(worker, 'load_api_key', side_effect=key_reader), \
                patch.object(worker, 'execute', return_value=(0, json.dumps({
                    'type': 'result', 'is_error': False, 'result': 'fixture completed'}), '')), \
                redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            try:
                result = managed.run(self.args('bounded analysis', claude=str(launcher),
                    claude_explicit=True, coord_task=tid, coord_assignment=aid),
                    policy, worker, copy)
            except worker.WorkerError as error:
                result = error.code
        self.assertFalse(marker.exists(), 'unrunnable namespace runtime reached provider credentials')
        self.assertEqual(result, 78)
        record = workspace.load(self.state, copy.id).metadata
        self.assertEqual(record['status'], 'failed')
        self.assertEqual((record.get('coord_task'), record.get('coord_assignment')), (tid, aid))


class RuntimePinParsingTests(CoordinationCase):
    def test_factory_budget_expiry_keeps_total_timeout_classification(self):
        with patch.object(runtime_preflight.time, 'monotonic', return_value=0) as clock:
            def factory(binary, runtime):
                clock.return_value = 2
                raise development.DevelopmentError('Sandbox probe exceeded total budget.', 124)
            with self.assertRaises(runtime_preflight.RuntimePreflightError) as caught:
                runtime_preflight.select_runtime('codex', '/usr/bin/true', 'claude',
                    factory, explicit_codex=True, deadline=1)
        self.assertEqual(caught.exception.code, 124)

    def test_explicit_default_runtime_name_remains_an_explicit_pin(self):
        with patch('sys.argv', ['worker', '--codex', 'codex', 'bounded work']):
            args = worker.parse_args()
        self.assertTrue(getattr(args, 'codex_explicit', False))

    def test_implicit_default_runtime_is_eligible_for_verified_candidates(self):
        with patch('sys.argv', ['worker', 'bounded work']):
            args = worker.parse_args()
        self.assertFalse(getattr(args, 'codex_explicit', False))


class PreparationCliTests(CoordinationCase):
    def test_reading_diff_after_unchanged_import_preserves_git_metadata(self):
        copy = workspace.create(self.repo, self.state)
        workspace.import_paths(copy, ['a.py'])
        before = copy.metadata['git_digest']
        self.assertEqual(copy.changes()[0], [])
        self.assertEqual(workspace._git_digest(copy.path), before)
        self.assertEqual(copy.diff(), '')
        self.assertEqual(workspace._git_digest(copy.path), before)

    def test_declarative_prepare_saves_a_reusable_workspace_environment(self):
        copy = workspace.create(self.repo, self.state)
        software = self.root / 'software'
        (software / 'bin').mkdir(parents=True)
        binary = software / 'bin' / 'sdk-check'
        binary.write_text('#!/bin/sh\necho ready\n'); binary.chmod(0o700)
        with redirect_stdout(io.StringIO()):
            code = delegation_cli.main(['workspace', 'prepare', copy.id,
                '--state-dir', str(self.state), '--tool', 'sdk=' + str(software),
                '--env', 'PATH={workspace}/.deepseek-tools/sdk/bin',
                '--probe', 'sdk-check', '--save-project'])
        self.assertEqual(code, 0)
        fresh = workspace.create(self.repo, self.state)
        with fresh.lock():
            toolchains.apply_saved_recipe(fresh)
        env = toolchains.environment_for(fresh, {'PATH': '/usr/bin'})
        self.assertEqual(env['PATH'].split(':')[0], str(fresh.path / '.deepseek-tools/sdk/bin'))
        self.assertEqual(toolchains.probes_for(fresh), ['sdk-check'])
        self.assertNotIn('.deepseek-tools', fresh.diff())

    def test_legacy_prepare_command_uses_prepared_environment_and_path(self):
        copy = workspace.create(self.repo, self.state)
        software = self.root / 'software'
        (software / 'bin').mkdir(parents=True)
        binary = software / 'bin' / 'sdk-check'
        binary.write_text('#!/bin/sh\ntest "$JAVA_HOME" = "$PWD/.deepseek-tools/sdk"\n')
        binary.chmod(0o700)
        with redirect_stdout(io.StringIO()):
            code = delegation_cli.main(['workspace', 'prepare', copy.id,
                '--state-dir', str(self.state), '--tool', 'sdk=' + str(software),
                '--env', 'JAVA_HOME={workspace}/.deepseek-tools/sdk',
                '--env', 'PATH={workspace}/.deepseek-tools/sdk/bin', '--', 'sdk-check'])
        self.assertEqual(code, 0)

    def test_verify_rejects_missing_checks_without_a_provider_or_worker(self):
        copy = workspace.create(self.repo, self.state)
        with patch.object(worker, 'load_api_key', side_effect=AssertionError('credential read')), \
                patch.object(worker, 'execute', side_effect=AssertionError('worker started')), \
                redirect_stderr(io.StringIO()):
            result = delegation_cli.main(['workspace', 'verify', copy.id,
                '--state-dir', str(self.state), '--json'])
        self.assertEqual(result, 64)
        self.assertEqual(workspace.load(self.state, copy.id).metadata['status'], 'ready')


class VerificationAttributionTests(CoordinationCase):
    def started_assignment(self, *, start=True):
        settings.set_values(self.repo / settings.PROJECT_FILE,
                            delegation_level='auto', access='full-access')
        policy = settings.resolve(self.repo)
        task = coordination.open_task(self.repo, session_id='verification-neutral',
            turn_id='1', prompt='fix a.py', policy=policy)
        features = dict(kind='implementation', domain='python', operation='fix',
            localization='known', coupling='local', verification='tests', clarity='clear',
            risk='low', scope_size='small', runtime='codex', model='deepseek-flash',
            effort='high', context_version='verification-neutral-v1')
        task = coordination.plan_task(self.repo, task['id'], {
            'classification': 'substantial', 'deliverables': [{
                'id': 'fix', 'kind': 'implementation', 'scope': ['a.py'],
                'executor': 'auto', 'acceptance': ['fix verified'],
                'dependencies': [], 'checks': ['python3 -c "raise SystemExit(7)"'],
                'features': features}]})
        aid = task['assignments'][0]['id']
        copy = workspace.create(self.repo, self.state)
        if start:
            coordination.assignment_started(self.repo, task['id'], aid,
                                            copy.id, 'codex', [], effort='high')
        return task, aid, copy

    def test_resuming_preparation_archives_and_clears_old_readiness_failure(self):
        task, aid, copy = self.started_assignment(start=False)
        probes = [{'command': 'python3 -c "raise SystemExit(7)"', 'exit_code': 7}]
        coordination.assignment_preparation_failed(self.repo, task['id'], aid,
            'toolchain missing', workspace_id=copy.id,
            exit_code=78, readiness_checks=probes, verification_cause='probe_failure')
        coordination.use_result(self.repo, task['id'], aid, 'reproduced',
            'Inspected preparation failure before explicit continuation.')
        coordination.assignment_started(self.repo, task['id'], aid,
                                        copy.id, 'codex', [], effort='high')
        row = coordination.load_task(self.repo, task['id'])['assignments'][0]
        self.assertEqual(row.get('readiness_checks'), [])
        self.assertEqual(row['attempt_history'][0].get('readiness_checks'), probes)

    def test_verification_evidence_rejects_false_success_and_malformed_values(self):
        task, aid, copy = self.started_assignment()
        checks = [{'command': 'python3', 'exit_code': 7}]
        coordination.assignment_finished(self.repo, task['id'], aid, 'failed',
            'check failed', [], checks, error_kind='verification', exit_code=65)
        before = coordination.load_task(self.repo, task['id'])
        base = dict(id='invalid', workspace_id=copy.id, status='passed',
                    checks=[{'command': 'python3', 'exit_code': 0}])
        invalid = [dict(base, checks=[]), dict(base, checks=checks),
                   dict(base, at=float('nan')),
                   dict(base, prepared_input_changes=['a.py\nprivate']),
                   dict(base, checks=[{'command': 'python3', 'exit_code': 0,
                                      'stdout_sha256': 'invalid'}]),
                   dict(base, checks=[{'command': 'python3', 'exit_code': 0,
                                      'timed_out': 'false'}])]
        for run in invalid:
            with self.subTest(run=run), self.assertRaises(coordination.CoordinationError):
                coordination.record_verification(self.repo, task['id'], aid, run)
            self.assertEqual(coordination.load_task(self.repo, task['id']), before)

    def test_verification_identity_is_idempotent_and_requires_terminal_assignment(self):
        task, aid, copy = self.started_assignment()
        run = dict(id='replayed', workspace_id=copy.id, status='passed',
                   checks=[{'command': 'python3', 'exit_code': 0}])
        before = coordination.load_task(self.repo, task['id'])
        with self.assertRaises(coordination.CoordinationError):
            coordination.record_verification(self.repo, task['id'], aid, run)
        self.assertEqual(coordination.load_task(self.repo, task['id']), before)
        coordination.assignment_finished(self.repo, task['id'], aid, 'failed',
            'failed', [], [{'command': 'python3', 'exit_code': 7}],
            error_kind='verification', exit_code=65)
        coordination.record_verification(self.repo, task['id'], aid, run)
        first = coordination.load_task(self.repo, task['id'])
        coordination.record_verification(self.repo, task['id'], aid, dict(run))
        self.assertEqual(coordination.load_task(self.repo, task['id']), first)
        with self.assertRaises(coordination.CoordinationError):
            coordination.record_verification(self.repo, task['id'], aid,
                                            dict(run, checks=[{'command': 'true', 'exit_code': 0}]))
        self.assertEqual(coordination.load_task(self.repo, task['id']), first)

    def test_pending_verification_stays_with_its_original_attempt_after_continuation(self):
        task, aid, copy = self.started_assignment()
        coordination.assignment_finished(self.repo, task['id'], aid, 'failed',
            'failed', [], [{'command': 'python3', 'exit_code': 7}],
            error_kind='verification', exit_code=65)
        finished_at = coordination.load_task(self.repo, task['id'])['assignments'][0]['finished_at']
        coordination.use_result(self.repo, task['id'], aid, 'reproduced', 'Inspected before continuation.')
        coordination.assignment_started(self.repo, task['id'], aid, copy.id, 'codex', [], effort='high')
        run = dict(id='saved-before-continuation', workspace_id=copy.id, status='passed',
                   checks=[{'command': 'python3', 'exit_code': 0}])
        coordination.record_verification(self.repo, task['id'], aid, run, finished_at=finished_at)
        row = coordination.load_task(self.repo, task['id'])['assignments'][0]
        self.assertEqual(row['status'], 'running')
        self.assertEqual(row['verification_runs'], [])
        self.assertEqual(row['attempt_history'][0]['verification_runs'], [run])
        before = coordination.load_task(self.repo, task['id'])
        with self.assertRaises(coordination.CoordinationError):
            coordination.record_verification(self.repo, task['id'], aid, run, finished_at=finished_at + 1)
        self.assertEqual(coordination.load_task(self.repo, task['id']), before)

    def test_unreviewed_verification_failure_does_not_grade_worker_quality(self):
        task, aid, copy = self.started_assignment()
        coordination.assignment_finished(self.repo, task['id'], aid, 'failed',
            'check failed; cause not yet reviewed', ['a.py'],
            [{'command': 'python3 -c "raise SystemExit(7)"', 'exit_code': 7}],
            error_kind='verification', exit_code=65)
        service = RoutingService(self.repo)
        observations = service.observations()
        self.assertEqual(observations[0]['outcome'], 'unknown')
        self.assertFalse(service.status()['admission']['active_cooldowns'])
        row = coordination.load_task(self.repo, task['id'])['assignments'][0]
        self.assertEqual(row['status'], 'failed')
        self.assertEqual(row['worker_changes'], ['a.py'])

    def test_baseline_and_prepared_input_evidence_are_retained_separately(self):
        task, aid, copy = self.started_assignment()
        baseline = [{'command': 'python3 -c "raise SystemExit(7)"', 'exit_code': 7}]
        try:
            coordination.assignment_finished(self.repo, task['id'], aid, 'failed',
                'baseline was already red', ['a.py'], list(baseline),
                error_kind='verification', exit_code=65, baseline_checks=baseline,
                verification_cause='baseline_failure',
                prepared_input_changes=['local.properties'])
        except TypeError as error:
            self.fail('terminal evidence API does not preserve baseline/input data: ' + str(error))
        row = coordination.load_task(self.repo, task['id'])['assignments'][0]
        self.assertEqual(row['baseline_checks'], baseline)
        self.assertEqual(row['verification_cause'], 'baseline_failure')
        self.assertEqual(row['prepared_input_changes'], ['local.properties'])
        self.assertEqual(row['worker_changes'], ['a.py'])

    def test_successful_reverification_preserves_original_failure_and_quality(self):
        task, aid, copy = self.started_assignment()
        checks = [{'command': 'python3 -c "raise SystemExit(7)"', 'exit_code': 7}]
        coordination.assignment_finished(self.repo, task['id'], aid, 'failed',
            'implementation defect reviewed', ['a.py'], checks,
            error_kind='verification', exit_code=65)
        coordination.use_result(self.repo, task['id'], aid, 'needs-rework',
            'Reviewed implementation defect.', quality={
                'grade': 'major_gaps', 'attribution': 'worker',
                'evidence': 'Declared behavior remained wrong.'})
        original = coordination.load_task(self.repo, task['id'])['assignments'][0]
        observations = RoutingService(self.repo).observations()
        record = getattr(coordination, 'record_verification', None)
        if record is None:
            self.fail('verification-only evidence recording is unavailable')
        run = {'id': 'verified-after-environment-preparation', 'workspace_id': copy.id,
               'status': 'passed', 'checks': [{'command': checks[0]['command'], 'exit_code': 0}],
               'verification_cause': None, 'prepared_input_changes': []}
        record(self.repo, task['id'], aid, run)
        row = coordination.load_task(self.repo, task['id'])['assignments'][0]
        for key in ('status', 'error_kind', 'exit_code', 'checks', 'worker_changes',
                    'disposition', 'routing_feedback'):
            self.assertEqual(row[key], original[key], key)
        self.assertEqual(row['verification_runs'], [run])
        self.assertEqual(RoutingService(self.repo).observations(), observations)

    def test_reverification_rejects_a_different_workspace_without_mutating_history(self):
        task, aid, copy = self.started_assignment()
        coordination.assignment_finished(self.repo, task['id'], aid, 'failed',
            'check failed', [], [{'command': 'python3', 'exit_code': 7}],
            error_kind='verification', exit_code=65)
        record = getattr(coordination, 'record_verification', None)
        if record is None:
            self.fail('verification-only evidence recording is unavailable')
        before = coordination.load_task(self.repo, task['id'])
        with self.assertRaises(coordination.CoordinationError):
            record(self.repo, task['id'], aid, {
                'id': 'wrong-copy', 'workspace_id': 'different-copy', 'status': 'passed',
                'checks': [{'command': 'python3', 'exit_code': 0}],
                'verification_cause': None, 'prepared_input_changes': []})
        self.assertEqual(coordination.load_task(self.repo, task['id']), before)

    def test_explicit_new_attempt_archives_and_clears_previous_check_evidence(self):
        task, aid, copy = self.started_assignment()
        checks = [{'command': 'python3', 'exit_code': 7}]
        coordination.assignment_finished(self.repo, task['id'], aid, 'failed',
            'baseline failure', [], checks, error_kind='verification', exit_code=65,
            baseline_checks=checks, verification_cause='baseline_failure',
            prepared_input_changes=['local.properties'])
        coordination.use_result(self.repo, task['id'], aid, 'needs-rework',
            'Inspected before explicit continuation.', quality={
                'grade': 'minor_gaps', 'attribution': 'worker', 'evidence': 'Minor defect.'})
        coordination.assignment_started(self.repo, task['id'], aid,
                                        copy.id, 'codex', [], effort='high')
        coordination.assignment_finished(self.repo, task['id'], aid, 'succeeded',
            'verified', [], [{'command': 'python3', 'exit_code': 0}],
            baseline_checks=[], verification_cause=None, prepared_input_changes=[])
        row = coordination.load_task(self.repo, task['id'])['assignments'][0]
        self.assertIsNone(row.get('verification_cause'))
        self.assertEqual(row['baseline_checks'], [])
        self.assertEqual(row['prepared_input_changes'], [])
        self.assertEqual(row['attempt_history'][0]['verification_cause'], 'baseline_failure')
