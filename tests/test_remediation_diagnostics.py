"""C6 causal diagnostics for coordination plans, access and settings.

These controls exercise the public plan CLI plus the internal ``problems``
contract. They deliberately avoid dumping submitted values: a started
definition change names only the deliverable id and the logical keys that
changed.
"""
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from codex_deepseek_team import coordination, settings
from codex_deepseek_team.routing import RoutingService


SOURCE_ROOT = Path(__file__).resolve().parents[1]
SOURCE_PATH = SOURCE_ROOT / 'src'


class RemediationDiagnosticsCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='dst-remediation-diagnostics-')
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        self.repo = base / 'repo'
        self.repo.mkdir()
        self.cwd = base / 'cwd'
        self.cwd.mkdir()
        self.home = base / 'home'
        self.codex_home = base / 'codex-home'
        self.claude_config = base / 'claude-config'
        self.xdg_config = base / 'xdg-config'
        self.xdg_state = base / 'xdg-state'
        self.state = base / 'state'
        for directory in (self.home, self.codex_home, self.claude_config,
                          self.xdg_config, self.xdg_state, self.state):
            directory.mkdir(mode=0o700)
        environment = mock.patch.dict(os.environ, {
            'HOME': str(self.home),
            'CODEX_HOME': str(self.codex_home),
            'CLAUDE_CONFIG_DIR': str(self.claude_config),
            'XDG_CONFIG_HOME': str(self.xdg_config),
            'XDG_STATE_HOME': str(self.xdg_state),
            'DEEPSEEK_TEAM_STATE_DIR': str(self.state),
        })
        environment.start()
        self.addCleanup(environment.stop)
        previous = os.getcwd()
        os.chdir(self.cwd)
        self.addCleanup(os.chdir, previous)

        subprocess.run(['git', 'init', '-q', str(self.repo)], check=True)
        (self.repo / 'a.py').write_text('VALUE = 1\n')
        subprocess.run(['git', '-C', str(self.repo), 'add', '.'], check=True)
        subprocess.run(
            ['git', '-C', str(self.repo), '-c', 'user.name=Tests',
             '-c', 'user.email=tests@example.invalid', 'commit', '-qm', 'base'],
            check=True)
        settings.set_values(self.repo / settings.PROJECT_FILE,
                            delegation_level=25, access='full-access')

        self.env = dict(os.environ)
        existing_pythonpath = self.env.get('PYTHONPATH')
        self.env['PYTHONPATH'] = str(SOURCE_PATH) + (
            os.pathsep + existing_pythonpath if existing_pythonpath else '')

    def features(self, kind='implementation', **changes):
        card = {
            'kind': kind,
            'domain': 'python',
            'operation': 'extend',
            'localization': 'known',
            'coupling': 'local',
            'verification': 'tests',
            'clarity': 'clear',
            'risk': 'low',
            'scope_size': 'small',
            'runtime': 'codex',
            'model': 'deepseek-flash',
            'effort': 'high',
            'context_version': 'remediation-v1',
        }
        card.update(changes)
        return card

    def deliverable(self, identifier='impl', *, kind='implementation',
                    executor='worker', features=None, **changes):
        item = {
            'id': identifier,
            'kind': kind,
            'scope': ['a.py'],
            'executor': executor,
            'acceptance': ['focused behavior accepted'],
            'dependencies': [],
            'checks': ['python3 -V'],
            'features': self.features(kind=kind) if features is None else features,
        }
        item.update(changes)
        return item

    def task(self, turn='1', *, level=25, access='full-access', session=None):
        settings.set_values(self.repo / settings.PROJECT_FILE,
                            delegation_level=level, access=access)
        return coordination.open_task(
            self.repo, session_id=session or 'remediation-diagnostics',
            turn_id=turn, prompt='redacted remediation diagnostics task',
            policy=settings.resolve(self.repo), runtime='codex')

    def plan(self, task, deliverables, classification='substantial', **extra):
        payload = {'classification': classification, 'deliverables': deliverables}
        payload.update(extra)
        return coordination.plan_task(self.repo, task['id'], payload)

    def ledger_snapshot(self, task_id):
        path = coordination._task_path(self.repo, task_id)
        info = path.stat()
        return path.read_bytes(), info.st_mtime_ns, info.st_ino

    def decision_count(self):
        return RoutingService(self.repo).status()['decisions']

    def started_worker_task(self, turn='started'):
        task = self.task(turn)
        planned = self.plan(task, [self.deliverable()])
        assignment = planned['assignments'][0]
        coordination.assignment_started(
            self.repo, task['id'], assignment['id'], 'ws', 'codex', [],
            effort='high')
        return task, coordination.load_task(self.repo, task['id'])

    def plan_cli(self, task_id, payload):
        return subprocess.run(
            [sys.executable, '-m', 'codex_deepseek_team', 'coordination',
             'plan', '--path', str(self.repo), '--task', task_id],
            cwd=self.repo, env=self.env, input=json.dumps(payload),
            text=True, capture_output=True, timeout=30)

    def status_cli(self, task_id):
        return subprocess.run(
            [sys.executable, '-m', 'codex_deepseek_team', 'coordination',
             'status', '--path', str(self.repo), '--task', task_id],
            cwd=self.repo, env=self.env, text=True, capture_output=True, timeout=30)

    def assert_problem_shape(self, problems):
        self.assertTrue(problems)
        for problem in problems:
            self.assertEqual(set(problem),
                             {'deliverable_id', 'field', 'code', 'message'})
            self.assertTrue(problem['message'])


class StartedDefinitionDiagnosticTests(RemediationDiagnosticsCase):
    def base_definition(self):
        return {
            'id': 'impl',
            'kind': 'implementation',
            'scope': ['a.py'],
            'acceptance': ['focused behavior accepted'],
            'dependencies': [{'kind': 'command', 'value': 'python3'}],
            'checks': ['python3 -V'],
            'checks_reusable': False,
            'retention': {'code': 'technical', 'evidence': 'runner constraint'},
            'features': self.features(),
            'routing': {'requested_executor': 'auto'},
            'integration_of': ['impl'],
            'write_scope': ['a.py'],
            'decision_artifacts': ['docs/decision.md'],
            'correction_of': {'task_id': 'task-' + 'a' * 20,
                              'assignment_id': 'as-' + 'b' * 16},
            'delegation_reason': 'specific native implementation requirement',
            'native_exception': {
                'code': 'explicit_user_request',
                'evidence': 'user explicitly requested a native implementation',
            },
        }

    def test_logical_field_ordering_is_complete_and_matches_c6(self):
        self.assertEqual(coordination.LOGICAL_DEFINITION_FIELDS, (
            'id', 'kind', 'scope', 'acceptance', 'dependencies', 'checks',
            'checks_reusable', 'retention', 'features', 'requested_executor',
            'integration_of', 'write_scope', 'decision_artifacts',
            'correction_of', 'delegation_reason', 'native_exception',
        ))

    def test_every_logical_field_is_a_changed_key(self):
        task = self.task('all-fields')
        base = self.base_definition()
        changes = {
            'id': 'other',
            'kind': 'test',
            'scope': ['b.py'],
            'acceptance': ['different acceptance'],
            'dependencies': [{'kind': 'path', 'value': 'tools/helper'}],
            'checks': ['python3 -c pass'],
            'checks_reusable': True,
            'retention': {'code': 'environment_incompatible',
                          'evidence': 'different constraint'},
            'features': self.features(risk='medium'),
            'requested_executor': {'routing': {'requested_executor': 'worker'}},
            'integration_of': [],
            'write_scope': ['b.py'],
            'decision_artifacts': ['docs/other.md'],
            'correction_of': {'task_id': 'task-' + 'c' * 20,
                              'assignment_id': 'as-' + 'd' * 16},
            'delegation_reason': 'different native implementation requirement',
            'native_exception': {
                'code': 'native_capability',
                'capability': 'private native browser control',
                'evidence': 'the worker sandbox cannot provide browser control',
            },
        }
        for field, replacement in changes.items():
            with self.subTest(field=field):
                incoming = copy.deepcopy(base)
                if field == 'requested_executor':
                    incoming.update(replacement)
                else:
                    incoming[field] = replacement
                self.assertEqual(
                    coordination._definition_differences(incoming, base, task),
                    [field])

    def test_canonical_effort_and_legacy_defaults_are_not_false_changes(self):
        task = self.task('canonical')
        base = self.base_definition()
        medium = copy.deepcopy(base)
        medium['features']['effort'] = 'medium'
        self.assertEqual(
            coordination._definition_differences(medium, base, task), [])

        legacy = {
            'id': 'impl',
            'kind': 'implementation',
            'scope': ['a.py'],
            'acceptance': ['focused behavior accepted'],
            'dependencies': [],
            'checks': ['python3 -V'],
            'features': {'kind': 'implementation'},
        }
        with_defaults = copy.deepcopy(legacy)
        with_defaults.update(
            checks_reusable=False,
            retention=None,
            integration_of=None,
            write_scope=None,
            decision_artifacts=None,
            correction_of=None,
            delegation_reason=None,
            native_exception=None)
        with_defaults['features'] = coordination._canonical_features(
            with_defaults, task)
        self.assertEqual(
            coordination._definition_differences(with_defaults, legacy, task), [])

    def test_derived_routing_and_result_state_is_not_part_of_the_definition(self):
        task = self.task('derived-state')
        base = self.base_definition()
        derived = copy.deepcopy(base)
        derived.update(
            routing={'requested_executor': 'auto', 'action': 'coordinator',
                     'reason_codes': ['protected_risk']},
            result={'outcome': 'accepted', 'evidence': 'ignored'},
            result_invalidated_at=123.0,
            native_dispatches=[{'tool_use_id': 'ignored'}],
        )
        self.assertEqual(
            coordination._definition_differences(derived, base, task), [])
        definition = coordination._logical_definition(base, task)
        self.assertEqual(tuple(definition), coordination.LOGICAL_DEFINITION_FIELDS)
        self.assertNotIn('routing', definition)
        self.assertNotIn('result', definition)

    def test_public_started_change_names_keys_without_values(self):
        task, saved = self.started_worker_task('public-keys')
        before = self.ledger_snapshot(task['id'])
        decisions = self.decision_count()
        item = copy.deepcopy(saved['deliverables'][0])
        item.update(
            scope=['private-changed-scope.py'],
            acceptance=['private-acceptance-value'],
            dependencies=[{'kind': 'path', 'value': 'private-dependency-value'}],
            checks=['private-check-value'],
            features=dict(item['features'], risk='medium'))
        with mock.patch.object(
                coordination, '_routing_batch',
                side_effect=AssertionError('routing must not open before revision validation')):
            with self.assertRaises(coordination.CoordinationError) as caught:
                self.plan(task, [item])
        error = caught.exception
        self.assertEqual(error.code, 64)
        self.assert_problem_shape(error.problems)
        self.assertEqual([problem['field'] for problem in error.problems],
                         ['scope', 'acceptance', 'dependencies', 'checks', 'features'])
        self.assertTrue(all(problem['deliverable_id'] == 'impl'
                            for problem in error.problems))
        message = str(error)
        self.assertIn('immutable', message.lower())
        self.assertIn('impl', message)
        for field in ('scope', 'acceptance', 'dependencies', 'checks', 'features'):
            self.assertIn(field, message)
        for submitted in ('private-changed-scope.py', 'private-acceptance-value',
                          'private-dependency-value', 'private-check-value'):
            self.assertNotIn(submitted, message)
        self.assertEqual(self.ledger_snapshot(task['id']), before)
        self.assertEqual(self.decision_count(), decisions)

    def test_public_started_id_change_is_an_id_removal_diagnostic(self):
        task, saved = self.started_worker_task('public-id-change')
        before = self.ledger_snapshot(task['id'])
        renamed = copy.deepcopy(saved['deliverables'][0])
        renamed['id'] = 'renamed'
        with self.assertRaises(coordination.CoordinationError) as caught:
            self.plan(task, [renamed])
        self.assertEqual([(problem['deliverable_id'], problem['field'], problem['code'])
                          for problem in caught.exception.problems],
                         [('impl', 'id', 'immutable_removal')])
        self.assertIn('cannot remove', caught.exception.message)
        self.assertNotIn('renamed', caught.exception.message)
        self.assertEqual(self.ledger_snapshot(task['id']), before)

    def test_public_started_executor_change_names_requested_executor(self):
        task, saved = self.started_worker_task('executor-key')
        before = self.ledger_snapshot(task['id'])
        item = copy.deepcopy(saved['deliverables'][0])
        item['executor'] = 'coordinator'
        with self.assertRaises(coordination.CoordinationError) as caught:
            self.plan(task, [item])
        self.assertEqual(caught.exception.code, 64)
        self.assertEqual([problem['field'] for problem in caught.exception.problems],
                         ['requested_executor'])
        self.assertEqual(caught.exception.problems[0]['deliverable_id'], 'impl')
        self.assertEqual(self.ledger_snapshot(task['id']), before)

    def test_public_manual_started_executor_rejects_auto_alias(self):
        task, saved = self.started_worker_task('manual-executor-auto')
        before = self.ledger_snapshot(task['id'])
        item = copy.deepcopy(saved['deliverables'][0])
        item['executor'] = 'auto'
        with self.assertRaises(coordination.CoordinationError) as caught:
            self.plan(task, [item])
        self.assertEqual([problem['field'] for problem in caught.exception.problems],
                         ['requested_executor'])
        self.assertEqual(self.ledger_snapshot(task['id']), before)

    def test_public_native_attestation_change_names_native_keys(self):
        task = self.task('native-keys')
        item = self.deliverable(
            'native-work', kind='review', executor='native-agent',
            features=self.features(kind='review', verification='manual'),
            delegation_reason='specific native review requirement',
            native_exception={
                'code': 'explicit_user_request',
                'evidence': 'user explicitly requested a native review here',
            })
        planned = self.plan(task, [item])
        coordination.authorize_native_dispatch(
            self.repo, task['id'], 'native-work', 'Task', 'tool-native-1')
        saved = coordination.load_task(self.repo, task['id'])['deliverables'][0]
        before = self.ledger_snapshot(task['id'])
        changed = copy.deepcopy(saved)
        changed['delegation_reason'] = 'different native review requirement'
        changed['native_exception'] = {
            'code': 'native_capability',
            'capability': 'private native reviewer',
            'evidence': 'the worker sandbox has no private native reviewer',
        }
        with self.assertRaises(coordination.CoordinationError) as caught:
            self.plan(task, [changed])
        self.assertEqual([problem['field'] for problem in caught.exception.problems],
                         ['delegation_reason', 'native_exception'])
        self.assertEqual(self.ledger_snapshot(task['id']), before)


class StructuralAggregationTests(RemediationDiagnosticsCase):
    def test_independent_structural_and_enum_errors_are_stable(self):
        task = self.task('aggregate')
        before = self.ledger_snapshot(task['id'])
        deliverables = [
            self.deliverable('alpha'),
            {
                'id': 'beta',
                'kind': 'not-a-kind',
                'scope': ['b.py'],
                'executor': 'not-an-executor',
                'acceptance': ['checked'],
                'dependencies': [],
                'checks': [],
                'checks_reusable': 'yes',
                'features': {'risk': 'critical', 'effort': 'extreme'},
            },
            {
                'id': 'gamma',
                'kind': 'implementation',
                'scope': {'path': 'b.py'},
                'executor': 'worker',
                'acceptance': ['checked'],
                'dependencies': 7,
                'checks': ['python3 -V'],
                'features': ['not-an-object'],
            },
        ]
        with mock.patch.object(
                coordination, '_routing_batch',
                side_effect=AssertionError('routing must not open for invalid structure')):
            with self.assertRaises(coordination.CoordinationError) as caught:
                self.plan(task, deliverables)
        error = caught.exception
        self.assertEqual(error.code, 64)
        self.assert_problem_shape(error.problems)
        self.assertEqual(
            [(problem['deliverable_id'], problem['field']) for problem in error.problems],
            [
                ('beta', 'kind'),
                ('beta', 'executor'),
                ('beta', 'checks_reusable'),
                ('beta', 'features.risk'),
                ('beta', 'features.effort'),
                ('gamma', 'scope'),
                ('gamma', 'dependencies'),
                ('gamma', 'features'),
            ])
        self.assertEqual(error.message,
                         '\n'.join(problem['message'] for problem in error.problems))
        self.assertEqual(self.ledger_snapshot(task['id']), before)
        self.assertEqual(self.decision_count(), 0)

    def test_public_plan_accepts_scalar_scope_and_normalizes_it(self):
        task = self.task('scalar-scope')
        item = self.deliverable('scalar')
        item['scope'] = 'a.py'
        result = self.plan_cli(
            task['id'],
            {'classification': 'substantial', 'deliverables': [item]})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn('Traceback', result.stderr)
        stored = coordination.load_task(self.repo, task['id'])['deliverables'][0]
        self.assertEqual(stored['scope'], ['a.py'])
        self.assertEqual(coordination.validate_task(self.repo, task['id']), [])

    def test_unhashable_kind_shapes_are_controlled_errors(self):
        native = {
            'code': 'explicit_user_request',
            'evidence': 'user explicitly requested this native implementation',
        }
        for executor in ('worker', 'native-agent'):
            for kind in ([], {'name': 'implementation'}):
                with self.subTest(executor=executor, kind=type(kind).__name__):
                    item = self.deliverable(
                        'kind-shape', executor=executor,
                        features=self.features(kind='implementation'))
                    item['kind'] = kind
                    if executor == 'native-agent':
                        item['delegation_reason'] = (
                            'specific native implementation requirement')
                        item['native_exception'] = native
                    with self.assertRaises(coordination.CoordinationError) as caught:
                        coordination._normalize_deliverable(item)
                    self.assertEqual(caught.exception.code, 64)
                    self.assert_problem_shape(caught.exception.problems)
                    self.assertIn(
                        ('kind-shape', 'kind', 'invalid_type'),
                        [(problem['deliverable_id'], problem['field'], problem['code'])
                         for problem in caught.exception.problems])

    def test_public_plan_rejects_unhashable_kind_without_writes(self):
        task = self.task('kind-shape-plan')
        before = self.ledger_snapshot(task['id'])
        item = self.deliverable(
            'bad-kind', executor='worker',
            features=self.features(kind='implementation'))
        item['kind'] = ['implementation']
        result = self.plan_cli(
            task['id'],
            {'classification': 'substantial', 'deliverables': [item]})
        self.assertEqual(result.returncode, 64)
        self.assertEqual(result.stdout, '')
        self.assertNotIn('Traceback', result.stderr)
        self.assertIn('kind', result.stderr)
        self.assertEqual(self.ledger_snapshot(task['id']), before)

    def test_malformed_parent_does_not_cascade_into_children(self):
        task = self.task('parent-shape')
        before = self.ledger_snapshot(task['id'])
        with self.assertRaises(coordination.CoordinationError) as caught:
            self.plan(task, {'id': 'nested'})
        problems = caught.exception.problems
        self.assertEqual(problems, [{
            'deliverable_id': None,
            'field': 'deliverables',
            'code': 'invalid_type',
            'message': 'deliverables must be a list.',
        }])
        self.assertEqual(self.ledger_snapshot(task['id']), before)

    def test_plan_level_errors_use_null_deliverable_id(self):
        task = self.task('plan-level')
        with self.assertRaises(coordination.CoordinationError) as caught:
            coordination.plan_task(self.repo, task['id'],
                                   {'classification': 'invalid', 'deliverables': []})
        self.assertEqual(caught.exception.problems, [{
            'deliverable_id': None,
            'field': 'classification',
            'code': 'invalid_enum',
            'message': 'classification must be substantial or small.',
        }])

    def test_invalid_enum_precedes_started_immutability(self):
        task, saved = self.started_worker_task('enum-before-immutable')
        before = self.ledger_snapshot(task['id'])
        item = copy.deepcopy(saved['deliverables'][0])
        item['features'] = dict(item['features'], risk='critical')
        with self.assertRaises(coordination.CoordinationError) as caught:
            self.plan(task, [item])
        self.assertEqual(caught.exception.code, 64)
        self.assertEqual([(problem['deliverable_id'], problem['field'], problem['code'])
                          for problem in caught.exception.problems],
                         [('impl', 'features.risk', 'invalid_enum')])
        self.assertNotIn('immutable', caught.exception.message.lower())
        self.assertEqual(self.ledger_snapshot(task['id']), before)

    def test_public_cli_reports_64_text_and_changes_nothing(self):
        task = self.task('cli-errors')
        before = self.ledger_snapshot(task['id'])
        payload = {
            'classification': 'substantial',
            'deliverables': [
                self.deliverable('first', features=self.features(risk='critical')),
                self.deliverable('second', executor='invalid'),
            ],
        }
        result = self.plan_cli(task['id'], payload)
        self.assertEqual(result.returncode, 64)
        self.assertEqual(result.stdout, '')
        self.assertIn('first', result.stderr)
        self.assertIn('features.risk', result.stderr)
        self.assertIn('second', result.stderr)
        self.assertIn('executor', result.stderr)
        self.assertNotIn('Traceback', result.stderr)
        self.assertEqual(self.ledger_snapshot(task['id']), before)

    def test_public_cli_rejects_unsupported_schema_without_changes(self):
        task = self.task('unsupported-schema')
        saved = coordination.load_task(self.repo, task['id'])
        saved['schema'] = 1
        coordination._atomic(coordination._task_path(self.repo, task['id']), saved)
        before = self.ledger_snapshot(task['id'])
        payload = {
            'classification': 'substantial',
            'deliverables': [self.deliverable()],
        }
        result = self.plan_cli(task['id'], payload)
        self.assertEqual(result.returncode, 78)
        self.assertEqual(result.stdout, '')
        self.assertIn('Unsupported coordination plan format', result.stderr)
        self.assertNotIn('Traceback', result.stderr)
        self.assertEqual(self.ledger_snapshot(task['id']), before)


class ChecksPresenceDiagnosticTests(RemediationDiagnosticsCase):
    def test_empty_checks_are_accepted_and_not_replaced_with_dummy_work(self):
        task = self.task('manual-empty-checks')
        item = self.deliverable(
            'review', kind='review', executor='auto', checks=[],
            features=self.features(kind='review', verification='manual'))
        result = self.plan_cli(
            task['id'], {'classification': 'substantial', 'deliverables': [item]})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['issues'], [])
        self.assertEqual(coordination.load_task(self.repo, task['id'])
                         ['deliverables'][0]['checks'], [])
        self.assertEqual(coordination.validate_task(self.repo, task['id']), [])
        self.assertIn('empty', coordination.ROUTING_NEXT_STEPS[
            'ineligible_verification'].lower())
        self.assertNotIn('dummy', coordination.ROUTING_NEXT_STEPS[
            'ineligible_verification'].lower())

    def test_missing_checks_key_is_diagnosed_separately_from_an_empty_list(self):
        task = self.task('missing-checks')
        item = self.deliverable()
        item.pop('checks')
        with self.assertRaises(coordination.CoordinationError) as caught:
            self.plan(task, [item])
        self.assertEqual([(problem['deliverable_id'], problem['field'], problem['code'])
                          for problem in caught.exception.problems],
                         [('impl', 'checks', 'missing_field')])
        self.assertIn('empty list is allowed',
                      caught.exception.problems[0]['message'])


class AccessAndRetentionDiagnosticTests(RemediationDiagnosticsCase):
    def test_revoked_write_access_names_effective_access_and_sources(self):
        task = self.task('access-revoked')
        self.plan(task, [self.deliverable()])
        settings.set_values(self.repo / settings.PROJECT_FILE, access='read-only')
        issues = coordination.validate_task(self.repo, task['id'])
        text = '\n'.join(issues)
        self.assertIn('Current effective access no longer permits writes.', text)
        self.assertIn('explicitly read-only', text)
        self.assertIn('access source=project:', text)
        self.assertIn('delegation_level source=project:', text)
        self.assertIn('impl', text)
        self.assertNotIn('a.py', text)
        status = self.status_cli(task['id'])
        self.assertEqual(status.returncode, 0, status.stderr)
        self.assertIn('Distribution issues:', status.stdout)
        self.assertIn('Current effective access no longer permits writes.', status.stdout)
        self.assertIn('explicitly read-only', status.stdout)

    def test_missing_access_layer_and_profile_default_are_distinguished(self):
        (self.repo / settings.PROJECT_FILE).unlink()
        policy = settings.resolve(self.repo)
        explanation = settings.access_explanation(policy)
        self.assertIn('no saved access layer', explanation)
        self.assertIn('access=auto', explanation)
        self.assertIn('default', explanation)
        self.assertIn('read-only', explanation)

    def test_settings_read_failure_is_a_controlled_access_error(self):
        task = self.task('settings-read-error')
        self.plan(task, [self.deliverable()])
        (self.repo / settings.PROJECT_FILE).write_text('access = "full-access"\n[broken\n')
        with self.assertRaises(coordination.CoordinationError) as caught:
            coordination.validate_task(self.repo, task['id'])
        self.assertEqual(caught.exception.code, 78)
        self.assertIn('Invalid delegation TOML', caught.exception.message)
        self.assertNotIn('Traceback', caught.exception.message)

    def test_queue_access_error_is_actionable_and_has_no_settings_content(self):
        task = self.task('queue-access')
        self.plan(task, [self.deliverable()])
        settings.set_values(self.repo / settings.PROJECT_FILE, access='read-only')
        saved = coordination.load_task(self.repo, task['id'])
        item = coordination.assignment_deliverable(
            saved, saved['assignments'][0]['id'])
        with self.assertRaises(coordination.CoordinationError) as caught:
            coordination._check_assignment_access(self.repo, task, item)
        self.assertEqual(caught.exception.code, 78)
        message = caught.exception.message
        self.assertTrue(message.startswith(
            'Current effective access no longer permits writes.'))
        self.assertIn('explicitly read-only', message)
        self.assertIn('access source=project:', message)
        self.assertIn('delegation_level source=project:', message)

    def test_manual75_explains_router_admission_does_not_replace_retention(self):
        task = self.task('manual-75', level=75, access='full-access')
        self.plan(task, [self.deliverable(executor='coordinator')])
        issues = coordination.validate_task(self.repo, task['id'])
        relevant = [issue for issue in issues
                    if issue.startswith('impl: worker-eligible implementation')]
        self.assertTrue(relevant, issues)
        message = relevant[0]
        self.assertIn('retained by coordinator', message)
        self.assertIn('router admission', message)
        self.assertIn('retention rule', message)
        self.assertIn('do not reclassify', message.lower())
        self.assertIn('protected kind', message)
        stored = coordination.load_task(self.repo, task['id'])['deliverables'][0]
        self.assertEqual(stored['kind'], 'implementation')
        status = self.status_cli(task['id'])
        self.assertEqual(status.returncode, 0, status.stderr)
        self.assertIn('Distribution issues:', status.stdout)
        self.assertIn('router admission', status.stdout)
        self.assertIn('retention rule', status.stdout)


class EntryStringValidationTests(RemediationDiagnosticsCase):
    """New plans accept only meaningful acceptance and check strings."""

    INVALID_ENTRIES = (
        ('integer', 1),
        ('none', None),
        ('boolean', True),
        ('empty', ''),
        ('spaces', '   '),
        ('tabs', '\t\n'),
        ('nul', 'accepted\x00criteria'),
    )

    def assert_rejected_without_writes(self, task, item, expected):
        before = self.ledger_snapshot(task['id'])
        with mock.patch.object(
                coordination, '_routing_batch',
                side_effect=AssertionError('routing must not open for invalid entries')):
            with self.assertRaises(coordination.CoordinationError) as caught:
                self.plan(task, [item])
        self.assertEqual(caught.exception.code, 64)
        self.assert_problem_shape(caught.exception.problems)
        self.assertEqual(
            [(problem['deliverable_id'], problem['field'], problem['code'])
             for problem in caught.exception.problems],
            expected)
        self.assertEqual(self.ledger_snapshot(task['id']), before)
        self.assertEqual(self.decision_count(), 0)

    def test_invalid_acceptance_entries_are_rejected_without_writes(self):
        for index, (label, value) in enumerate(self.INVALID_ENTRIES):
            with self.subTest(entry=label):
                task = self.task('acceptance-entry-' + str(index))
                item = self.deliverable('impl')
                item['acceptance'] = ['focused behavior accepted', value]
                self.assert_rejected_without_writes(
                    task, item, [('impl', 'acceptance', 'invalid_type')])

    def test_invalid_checks_entries_are_rejected_without_writes(self):
        for index, (label, value) in enumerate(self.INVALID_ENTRIES):
            with self.subTest(entry=label):
                task = self.task('checks-entry-' + str(index))
                item = self.deliverable('impl')
                item['checks'] = ['python3 -V', value]
                self.assert_rejected_without_writes(
                    task, item, [('impl', 'checks', 'invalid_type')])

    def test_multiple_invalid_entries_are_aggregated_in_stable_order(self):
        task = self.task('entry-order')
        item = self.deliverable('multi')
        item['acceptance'] = ['valid', 7, '', 'tail\x00']
        item['checks'] = ['python3 -V', None, ' \t ', 'bad\x00']
        self.assert_rejected_without_writes(
            task, item,
            [('multi', 'acceptance', 'invalid_type'),
             ('multi', 'acceptance', 'invalid_type'),
             ('multi', 'acceptance', 'invalid_type'),
             ('multi', 'checks', 'invalid_type'),
             ('multi', 'checks', 'invalid_type'),
             ('multi', 'checks', 'invalid_type')])

    def test_multiple_deliverables_report_entries_in_deliverable_order(self):
        task = self.task('entry-deliverables')
        first = self.deliverable('first')
        first['acceptance'] = [1]
        second = self.deliverable('second')
        second['checks'] = ['']
        before = self.ledger_snapshot(task['id'])
        with mock.patch.object(
                coordination, '_routing_batch',
                side_effect=AssertionError('routing must not open for invalid entries')):
            with self.assertRaises(coordination.CoordinationError) as caught:
                self.plan(task, [first, second])
        self.assertEqual(
            [(problem['deliverable_id'], problem['field'], problem['code'])
             for problem in caught.exception.problems],
            [('first', 'acceptance', 'invalid_type'),
             ('second', 'checks', 'invalid_type')])
        self.assertEqual(self.ledger_snapshot(task['id']), before)
        self.assertEqual(self.decision_count(), 0)

    def test_wrong_entry_container_shape_does_not_cascade(self):
        task = self.task('entry-parent-shape')
        item = self.deliverable('shape')
        item['acceptance'] = 'one acceptance string is not a list'
        item['checks'] = {'command': 'python3 -V'}
        self.assert_rejected_without_writes(
            task, item,
            [('shape', 'acceptance', 'invalid_type'),
             ('shape', 'checks', 'invalid_type')])

    def test_public_cli_rejects_invalid_entries_without_writes(self):
        task = self.task('entry-cli')
        before = self.ledger_snapshot(task['id'])
        item = self.deliverable('cli')
        item['acceptance'] = [1]
        item['checks'] = ['']
        result = self.plan_cli(
            task['id'],
            {'classification': 'substantial', 'deliverables': [item]})
        self.assertEqual(result.returncode, 64)
        self.assertEqual(result.stdout, '')
        self.assertIn('acceptance', result.stderr)
        self.assertIn('checks', result.stderr)
        self.assertNotIn('Traceback', result.stderr)
        self.assertEqual(self.ledger_snapshot(task['id']), before)
        self.assertEqual(self.decision_count(), 0)

    def test_valid_entries_and_empty_checks_keep_planning(self):
        task = self.task('valid-entries')
        item = self.deliverable(
            'review', kind='review', executor='worker',
            features=self.features(kind='review', verification='manual'))
        item['acceptance'] = ['  focused behavior accepted  ']
        item['checks'] = []
        planned = self.plan(task, [item])
        stored = planned['deliverables'][0]
        self.assertEqual(stored['acceptance'], ['  focused behavior accepted  '])
        self.assertEqual(stored['checks'], [])


class HistoricalRecordCompletionTests(RemediationDiagnosticsCase):
    def test_old_malformed_acceptance_completes_without_rewriting_history(self):
        task = self.task('historical-acceptance')
        self.plan(task, [self.deliverable('legacy', executor='coordinator')])
        path = coordination._task_path(self.repo, task['id'])
        stored = coordination.load_task(self.repo, task['id'])
        stored['deliverables'][0]['acceptance'] = [1]
        coordination._atomic(path, stored)

        loaded = coordination.load_task(self.repo, task['id'])
        self.assertEqual(loaded['deliverables'][0]['acceptance'], [1])
        coordination.observe_coordinator_result(
            self.repo, task['id'], 'legacy', 'accepted',
            'verified historical outcome')
        completed = coordination.complete_task(self.repo, task['id'])

        self.assertEqual(completed['status'], 'completed')
        final = coordination.load_task(self.repo, task['id'])
        self.assertEqual(final['deliverables'][0]['acceptance'], [1])
        self.assertEqual(
            final['deliverables'][0]['result']['outcome'], 'accepted')


if __name__ == '__main__':
    unittest.main()
