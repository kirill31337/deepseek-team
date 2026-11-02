"""Truthful feature-card diagnostics, schema exposure and Kotlin lessons."""
import copy
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from codex_deepseek_team import coordination, lessons, routing_admission, routing_cli, settings
from codex_deepseek_team.routing import RoutingService
from codex_deepseek_team.routing_models import (
    FEATURE_DEFAULTS,
    FEATURE_VALUES,
    REQUIRED_SEMANTIC_FIELDS,
    RoutingError,
    missing_semantic_fields,
    validate_features,
)


_MISSING = object()


def complete_card(kind='implementation', **changes):
    card = {
        'kind': kind,
        'domain': 'python',
        'operation': 'fix',
        'localization': 'known',
        'coupling': 'local',
        'verification': 'tests',
        'clarity': 'clear',
        'risk': 'low',
        'scope_size': 'small',
        'runtime': 'codex',
        'model': 'deepseek-flash',
        'effort': 'high',
        'context_version': 'feature-diagnostics',
    }
    card.update(changes)
    return card


class FeatureContractTests(unittest.TestCase):
    def test_invalid_values_name_deterministic_allowed_choices(self):
        for field, value in (('risk', 'critical'), ('domain', 'swift')):
            with self.subTest(field=field):
                with self.assertRaises(RoutingError) as caught:
                    validate_features({field: value})
                message = str(caught.exception)
                self.assertIn(field, message)
                for allowed in sorted(FEATURE_VALUES[field]):
                    self.assertIn(allowed, message)

    def test_kotlin_is_valid_without_changing_legacy_defaults(self):
        self.assertEqual(validate_features({'domain': 'kotlin'})['domain'], 'kotlin')
        self.assertEqual(FEATURE_DEFAULTS['risk'], 'unknown')
        self.assertEqual(FEATURE_DEFAULTS['kind'], 'implementation')
        self.assertEqual(FEATURE_DEFAULTS['runtime'], 'codex')
        self.assertEqual(FEATURE_DEFAULTS['effort'], 'high')
        self.assertIn('java', FEATURE_VALUES['domain'])
        self.assertIn('other', FEATURE_VALUES['domain'])

    def test_missing_semantic_fields_uses_the_required_contract(self):
        self.assertEqual(missing_semantic_fields({}), list(REQUIRED_SEMANTIC_FIELDS))
        self.assertEqual(missing_semantic_fields({'risk': 'unknown'}),
                         [field for field in REQUIRED_SEMANTIC_FIELDS if field != 'risk'])
        self.assertEqual(missing_semantic_fields(
            {field: 'unknown' for field in REQUIRED_SEMANTIC_FIELDS}), [])
        self.assertEqual(validate_features({})['risk'], 'unknown')

    def test_schema_command_is_machine_readable_and_read_only(self):
        with mock.patch.object(routing_cli, 'RoutingService',
                               side_effect=AssertionError('schema must not open project state')):
            with mock.patch('sys.stdout', new_callable=io.StringIO) as output:
                code = routing_cli.main(['routing', 'schema'])
        self.assertEqual(code, 0)
        schema = json.loads(output.getvalue())
        self.assertEqual(schema['required_semantic_fields'], list(REQUIRED_SEMANTIC_FIELDS))
        self.assertEqual(schema['defaults'], FEATURE_DEFAULTS)
        self.assertEqual(schema['values'], {
            key: sorted(values) for key, values in FEATURE_VALUES.items()
        })
        self.assertIn('kotlin', schema['values']['domain'])

    def test_schema_is_discoverable_from_routing_help(self):
        help_text = routing_cli._parser().format_help()
        self.assertIn('schema', help_text)


class RiskDiagnosticTests(unittest.TestCase):
    def test_legacy_risk_advice_is_truthful_without_rewriting_history(self):
        for risk, advice in (
                ('unknown', 'declare risk in the features card'),
                ('protected', 'protected-risk')):
            with self.subTest(risk=risk):
                task = {'deliverables': [{
                    'id': 'legacy', 'kind': 'implementation',
                    'executor': 'coordinator',
                    'features': complete_card(risk=risk),
                    'routing': {'requested_executor': 'auto',
                                'reason_codes': ['ineligible_risk']},
                }]}
                before = copy.deepcopy(task)
                row, = coordination.routing_diagnostics(task)
                self.assertIn(advice, row['next_step'])
                self.assertEqual(row['reason_codes'], ['ineligible_risk'])
                self.assertEqual(task, before)

    def test_unknown_high_and_protected_risk_have_truthful_reasons(self):
        self.assertEqual(
            routing_admission.ineligible_reason(complete_card(risk='unknown'), 'full-access'),
            'undeclared_risk')
        self.assertEqual(
            routing_admission.ineligible_reason(complete_card(risk='high'), 'full-access'),
            'ineligible_risk')
        self.assertEqual(
            routing_admission.ineligible_reason(complete_card(risk='protected'), 'full-access'),
            'protected_risk')

    def test_next_steps_tell_the_coordinator_to_declare_risk_or_retain_it(self):
        self.assertIn('declare risk in the features card',
                      coordination.ROUTING_NEXT_STEPS['undeclared_risk'])
        self.assertIn('high-risk', coordination.ROUTING_NEXT_STEPS['ineligible_risk'])
        self.assertIn('protected-risk', coordination.ROUTING_NEXT_STEPS['protected_risk'])


class FeaturePlanCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='dst-feature-diagnostics-')
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.repo = self.base / 'project'
        self.repo.mkdir()
        self.cwd = self.base / 'cwd'
        self.cwd.mkdir()
        paths = {
            'HOME': self.base / 'home',
            'CODEX_HOME': self.base / 'codex-home',
            'CLAUDE_CONFIG_DIR': self.base / 'claude-config',
            'XDG_CONFIG_HOME': self.base / 'xdg-config',
            'XDG_STATE_HOME': self.base / 'xdg-state',
            'DEEPSEEK_TEAM_STATE_DIR': self.base / 'state',
        }
        for path in paths.values():
            path.mkdir(mode=0o700)
        self.environment = mock.patch.dict(os.environ, {
            **{name: str(path) for name, path in paths.items()},
            'DEEPSEEK_TEAM_DISABLED': '0',
        })
        self.environment.start()
        self.addCleanup(self.environment.stop)
        previous = os.getcwd()
        os.chdir(self.cwd)
        self.addCleanup(os.chdir, previous)
        self.canaries = {}
        for name, path in paths.items():
            canary = path / 'external-canary.txt'
            canary.write_text(name + ' remains external\n')
            self.canaries[canary] = canary.read_bytes()
        self.addCleanup(self.assert_canaries)
        subprocess.run(['git', 'init', '-q', str(self.repo)], check=True)
        subprocess.run(
            ['git', '-C', str(self.repo), '-c', 'user.name=Tests',
             '-c', 'user.email=tests@example.invalid', 'commit', '-qm', 'initial',
             '--allow-empty'],
            check=True)
        settings.set_values(self.repo / settings.PROJECT_FILE,
                            delegation_level='auto', access='full-access')
        self.policy = settings.resolve(self.repo)

    def assert_canaries(self):
        for canary, content in self.canaries.items():
            self.assertEqual(canary.read_bytes(), content)

    def task(self, turn='1'):
        return coordination.open_task(
            self.repo, session_id='feature-diagnostics', turn_id=turn,
            prompt='redacted feature-card task', policy=self.policy)

    def deliverable(self, *, features=_MISSING, kind='implementation',
                    executor='auto', **changes):
        item = {
            'id': 'impl',
            'kind': kind,
            'executor': executor,
            'scope': ['src/example.py'],
            'acceptance': ['focused checks pass'],
            'dependencies': [],
            'checks': ['python3 -V'],
        }
        if features is not _MISSING:
            item['features'] = features
        item.update(changes)
        return item

    def plan(self, task, deliverables, classification='substantial'):
        return coordination.plan_task(
            self.repo, task['id'],
            {'classification': classification, 'deliverables': deliverables})

    def decisions(self):
        return RoutingService(self.repo).status()['decisions']

    def assert_no_plan_side_effects(self, task_id):
        saved = coordination.load_task(self.repo, task_id)
        self.assertEqual(saved['deliverables'], [])
        self.assertEqual(saved['assignments'], [])
        self.assertEqual(self.decisions(), 0)

    def test_absent_feature_card_rejects_all_missing_fields_before_routing(self):
        task = self.task()
        with mock.patch.object(
                coordination, '_routing_batch',
                side_effect=AssertionError('routing must not open before validation')):
            with self.assertRaises(coordination.CoordinationError) as caught:
                self.plan(task, [self.deliverable()])
        self.assertEqual(caught.exception.code, 64)
        message = str(caught.exception)
        self.assertIn('missing semantic feature fields', message)
        for field in REQUIRED_SEMANTIC_FIELDS:
            self.assertIn(field, message)
        lowered = message.lower()
        for misleading in ('budget', 'lower', 'profile'):
            self.assertNotIn(misleading, lowered)
        self.assert_no_plan_side_effects(task['id'])

    def test_incomplete_card_rejects_only_the_missing_fields(self):
        task = self.task()
        with self.assertRaises(coordination.CoordinationError) as caught:
            self.plan(task, [self.deliverable(
                features={'domain': 'python', 'risk': 'low'})])
        message = str(caught.exception)
        expected_missing = [
            field for field in REQUIRED_SEMANTIC_FIELDS
            if field not in ('domain', 'risk')
        ]
        for field in expected_missing:
            self.assertIn(field, message)
        self.assertNotIn('missing semantic feature fields: domain', message)
        self.assertNotIn('missing semantic feature fields: risk', message)
        self.assert_no_plan_side_effects(task['id'])

    def test_invalid_card_value_names_allowed_choices_before_routing(self):
        task = self.task()
        with self.assertRaises(coordination.CoordinationError) as caught:
            self.plan(task, [self.deliverable(features=complete_card(risk='critical'))])
        message = str(caught.exception)
        for allowed in sorted(FEATURE_VALUES['risk']):
            self.assertIn(allowed, message)
        self.assert_no_plan_side_effects(task['id'])

    def test_explicit_unknown_values_are_honest_declarations(self):
        task = self.task()
        unknown = {field: 'unknown' for field in REQUIRED_SEMANTIC_FIELDS}
        planned = self.plan(task, [self.deliverable(features=unknown)])
        deliverable = planned['deliverables'][0]
        self.assertEqual(deliverable['executor'], 'coordinator')
        self.assertEqual(deliverable['routing']['reason_codes'], ['undeclared_risk'])
        diagnostics = coordination.routing_diagnostics(planned)[0]
        self.assertIn('declare risk in the features card', diagnostics['next_step'])
        self.assertEqual(planned['assignments'], [])

    def test_high_and_protected_risk_get_coordinator_ownership_advice(self):
        cases = {
            'high': ('ineligible_risk', 'high-risk'),
            'protected': ('protected_risk', 'protected-risk'),
        }
        for risk, (reason, fragment) in cases.items():
            with self.subTest(risk=risk):
                planned = self.plan(
                    self.task(turn='risk-' + risk),
                    [self.deliverable(features=complete_card(risk=risk))])
                deliverable = planned['deliverables'][0]
                self.assertEqual(deliverable['executor'], 'coordinator')
                diagnostics = coordination.routing_diagnostics(planned)[0]
                self.assertEqual(diagnostics['reason_codes'], [reason])
                self.assertIn(fragment, diagnostics['next_step'])
                self.assertIn('coordinator', diagnostics['next_step'])

    def test_protected_kind_keeps_legacy_cardless_plan_compatibility(self):
        task = self.task(turn='protected-kind')
        item = self.deliverable(kind='security', executor='coordinator', scope=['design'],
                                checks=[], acceptance=['reviewed'])
        planned = self.plan(task, [item])
        deliverable = planned['deliverables'][0]
        self.assertEqual(deliverable['executor'], 'coordinator')
        self.assertEqual(deliverable['routing']['reason_codes'], ['protected_kind'])

    def _historical_plan(self, *, routing_committed, started=True,
                         executor='coordinator'):
        task = self.task(turn='historical-' + str(routing_committed))
        saved = coordination.load_task(self.repo, task['id'])
        legacy = {
            'id': 'impl',
            'kind': 'implementation',
            'scope': ['src/example.py'],
            'executor': executor,
            'acceptance': ['focused checks pass'],
            'dependencies': [],
            'checks': ['python3 -V'],
            'checks_reusable': False,
            'features': validate_features({
                'kind': 'implementation', 'runtime': 'codex', 'effort': 'high'}),
        }
        saved.update(
            classification='substantial',
            small_evidence=None,
            deliverables=[legacy],
            assignments=[{
                'id': 'as-legacy', 'deliverable_id': 'impl', 'status': 'succeeded',
                'workspace_id': 'ws-legacy', 'runtime': 'codex', 'effort': 'high',
                'prepared_changes': [], 'worker_changes': [], 'checks': [],
                'result_summary': 'legacy result', 'disposition': None,
            }] if started else [],
            coordinator_events=[],
            constraints=[],
            status='planned',
        )
        if routing_committed:
            saved['routing_committed'] = True
        else:
            saved.pop('routing_committed', None)
        coordination._atomic(coordination._task_path(self.repo, task['id']), saved)
        return task, copy.deepcopy(legacy)

    def test_historical_started_plan_replays_with_partial_raw_card(self):
        task, saved_item = self._historical_plan(routing_committed=True)
        path = coordination._task_path(self.repo, task['id'])
        before = path.read_bytes()
        incoming = dict(saved_item, features={'kind': 'implementation'})
        repeated = self.plan(task, [incoming])
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(repeated['deliverables'][0]['features'], saved_item['features'])
        self.assertEqual(self.decisions(), 0)

    def test_crashed_legacy_plan_repairs_instead_of_rejecting_the_card(self):
        task, saved_item = self._historical_plan(
            routing_committed=False, started=False, executor='auto')
        incoming = dict(saved_item, features={'kind': 'implementation'})
        repaired = self.plan(task, [incoming])
        self.assertIs(repaired.get('routing_committed'), True)
        self.assertIn('routing', repaired['deliverables'][0])
        self.assertEqual(self.decisions(), 1)


class KotlinLessonsTests(unittest.TestCase):
    setUp = FeaturePlanCase.setUp
    assert_canaries = FeaturePlanCase.assert_canaries

    def test_kotlin_lessons_are_accepted_and_java_stays_valid(self):
        recorded = lessons.record_outcome(
            self.repo, task_id='task-0001', assignment_id='assignment-0001',
            disposition='incorporated', evidence='Kotlin guidance verified',
            context={'features': {'kind': 'implementation', 'domain': 'kotlin',
                                  'effort': 'high'}})
        case_id = recorded['case_id']
        status = lessons.status(self.repo)
        payload = {
            'expected_version': 0,
            'through_event': status['through_event'],
            'summary': 'Kotlin and legacy Java remain accepted',
            'rules': [
                {'id': 'kotlin', 'when': {'domain': 'kotlin'},
                 'condition': 'Check Kotlin coroutine boundaries.',
                 'action': 'Use the Kotlin-specific check.',
                 'evidence': [case_id]},
                {'id': 'java', 'when': {'domain': 'java'},
                 'condition': 'Check Java compatibility.',
                 'action': 'Use the Java-specific check.',
                 'evidence': [case_id]},
            ],
        }
        lessons.apply_review(self.repo, payload)
        self.assertEqual(
            lessons.snapshot(self.repo, {'domain': 'kotlin'})['rule_ids'], ['kotlin'])
        self.assertEqual(
            lessons.snapshot(self.repo, {'domain': 'java'})['rule_ids'], ['java'])

    def test_invalid_lessons_domain_names_allowed_values(self):
        recorded = lessons.record_outcome(
            self.repo, task_id='task-0002', assignment_id='assignment-0002',
            disposition='incorporated', evidence='Kotlin guidance verified',
            context={'features': {'kind': 'implementation', 'domain': 'kotlin',
                                  'effort': 'high'}})
        status = lessons.status(self.repo)
        payload = {
            'expected_version': 0,
            'through_event': status['through_event'],
            'summary': 'Reject an unknown domain',
            'rules': [{
                'id': 'swift', 'when': {'domain': 'swift'},
                'condition': 'Check Swift boundaries.',
                'action': 'Use the Swift-specific check.',
                'evidence': [recorded['case_id']],
            }],
        }
        with self.assertRaises(lessons.LessonError) as caught:
            lessons.apply_review(self.repo, payload)
        message = str(caught.exception)
        for allowed in sorted(lessons.DOMAIN_VALUES):
            self.assertIn(allowed, message)


if __name__ == '__main__':
    unittest.main()
