"""Auto-profile distribution enforcement and routing diagnostics.

These are regression tests for the coordinator-approved behaviour: under an
adaptive (Auto) delegation profile a substantial worker-eligible deliverable must
request ``executor: "auto"`` and let the saved routing decision choose worker or
coordinator. Protected responsibilities stay explicit coordinator, the attested
native exception and the small/manual profiles are unchanged, and identity
metadata cannot silently change after a deliverable started.
"""
import copy
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from codex_deepseek_team import coordination, coordination_cli, settings
from codex_deepseek_team.routing import RoutingService


class AutoDistributionCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='dst-auto-')
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        self.root = base / 'project'
        self.root.mkdir()
        subprocess.run(['git', 'init', '-q', str(self.root)], check=True)
        subprocess.run(['git', '-C', str(self.root), '-c', 'user.name=Tests',
                        '-c', 'user.email=tests@example.invalid', 'commit', '-qm',
                        'initial', '--allow-empty'], check=True)
        environment = mock.patch.dict(os.environ, {
            'DEEPSEEK_TEAM_STATE_DIR': str(base / 'state'),
            'XDG_CONFIG_HOME': str(base / 'config'),
        })
        environment.start()
        self.addCleanup(environment.stop)
        settings.set_values(self.root / settings.PROJECT_FILE,
                            delegation_level='auto', access='full-access')
        self.service = RoutingService(self.root)
        self.service.configure({'failure_cooldown_seconds': 300})

    # -- fixtures ---------------------------------------------------------
    def policy(self, level='auto', access='full-access'):
        return settings.Policy(level, access, {'access': 'test', 'delegation_level': 'test'})

    def task(self, turn='1', level='auto', access='full-access'):
        return coordination.open_task(self.root, session_id='auto-session', turn_id=turn,
                                      prompt='redacted task', policy=self.policy(level, access))

    def features(self, kind='implementation', **changes):
        card = dict(kind=kind, domain='python', operation='fix', localization='known',
                    coupling='local', verification='tests', clarity='clear', risk='low',
                    scope_size='small', runtime='codex', model='deepseek-flash',
                    effort='high', context_version='default')
        card.update(changes)
        return card

    def deliverable(self, kind='implementation', executor='auto', **changes):
        item = dict(id='impl', kind=kind, executor=executor, scope=['src/example.py'],
                    acceptance=['Tests pass'], checks=['python3 -V'], dependencies=[],
                    features=self.features(kind))
        item.update(changes)
        return item

    def plan(self, task, deliverables, classification='substantial'):
        return coordination.plan_task(self.root, task['id'],
                                      {'classification': classification, 'deliverables': deliverables})

    def decisions(self):
        return self.service.status()['decisions']

    # -- enforcement ------------------------------------------------------
    def test_auto_writes_must_request_auto_and_never_convert_silently(self):
        for executor in ('worker', 'coordinator'):
            with self.subTest(executor=executor):
                task = self.task(turn='write-' + executor)
                with self.assertRaises(coordination.CoordinationError) as caught:
                    self.plan(task, [self.deliverable(executor=executor)])
                self.assertEqual(caught.exception.code, 64)
                self.assertIn("executor:'auto'", str(caught.exception))
                self.assertIn('bypass', str(caught.exception))
                # Nothing durable may change before the bypass is rejected.
                self.assertEqual(coordination.load_task(self.root, task['id'])['deliverables'], [])
                self.assertEqual(self.decisions(), 0)

    def test_auto_reads_must_request_auto(self):
        for kind in ('review', 'research', 'diagnostic', 'test_plan'):
            for executor in ('worker', 'coordinator'):
                with self.subTest(kind=kind, executor=executor):
                    task = self.task(turn=f'{kind}-{executor}')
                    item = self.deliverable(kind=kind, executor=executor,
                                            features=self.features(kind, verification='manual'),
                                            checks=[], acceptance=['independent findings'])
                    with self.assertRaises(coordination.CoordinationError) as caught:
                        self.plan(task, [item])
                    self.assertEqual(caught.exception.code, 64)
                    self.assertIn("executor:'auto'", str(caught.exception))
                    self.assertEqual(self.decisions(), 0)

    def test_auto_read_slice_still_delegates_through_the_saved_decision(self):
        task = self.task(turn='read-auto', access='read-only')
        item = self.deliverable(kind='review', executor='auto',
                                features=self.features('review', operation='review',
                                                       verification='manual'),
                                checks=[], acceptance=['independent findings'])
        planned = self.plan(task, [item])
        self.assertEqual(planned['deliverables'][0]['executor'], 'worker')
        self.assertEqual(planned['deliverables'][0]['routing']['requested_executor'], 'auto')
        self.assertEqual(coordination.validate_task(self.root, task['id']), [])
        self.assertEqual(len(planned['assignments']), 1)

    def test_rejected_bypass_does_not_disturb_an_existing_valid_plan(self):
        task = self.task(turn='replan')
        valid = self.plan(task, [self.deliverable()])
        self.assertEqual(valid['deliverables'][0]['executor'], 'worker')
        with self.assertRaises(coordination.CoordinationError):
            self.plan(task, [self.deliverable(executor='worker')])
        self.assertEqual(coordination.load_task(self.root, task['id']), valid)
        self.assertEqual(self.decisions(), 1)

    def test_protected_responsibilities_keep_explicit_coordinator(self):
        task = self.task(turn='protected')
        protected = self.deliverable(kind='architecture', executor='coordinator',
                                     scope=['design'], checks=[], acceptance=['decision'])
        planned = self.plan(task, [protected])
        self.assertEqual(planned['deliverables'][0]['executor'], 'coordinator')
        self.assertEqual(planned['assignments'], [])
        self.assertEqual(coordination.validate_task(self.root, task['id']), [])
        for executor in ('auto', 'worker', 'native-agent'):
            with self.subTest(executor=executor):
                other = self.task(turn='protected-' + executor)
                item = self.deliverable(kind='security', executor=executor, scope=['design'],
                                        checks=[], acceptance=['decision'],
                                        delegation_reason='native security review requested '
                                                          'with a local capability',
                                        native_exception={'code': 'explicit_user_request',
                                                          'evidence': 'user asked for this review'})
                with self.assertRaises(coordination.CoordinationError):
                    self.plan(other, [item])

    def test_read_only_protected_plan_needs_no_mutation_authorization(self):
        task = self.task(turn='protected-readonly', access='read-only')
        item = self.deliverable(kind='security', executor='coordinator', scope=['design'],
                                checks=[], acceptance=['reviewed threat model'],
                                features=self.features('security', operation='review',
                                                       verification='manual'))
        planned = self.plan(task, [item])
        self.assertEqual(planned['deliverables'][0]['executor'], 'coordinator')
        self.assertEqual(coordination.validate_task(self.root, task['id']), [])
        self.assertNotIn('write_scope', planned['deliverables'][0])

    def test_small_exemption_still_requires_evidence_and_one_concrete_scope(self):
        item = self.deliverable(kind='documentation', executor='coordinator', scope=['README.md'])
        with self.assertRaises(coordination.CoordinationError):
            self.plan(self.task(turn='small-missing'), [item], classification='small')
        with self.assertRaises(coordination.CoordinationError):
            coordination.plan_task(self.root, self.task(turn='small-two')['id'], {
                'classification': 'small', 'small_evidence': 'one concrete change',
                'deliverables': [item, copy.deepcopy(item)]})
        with self.assertRaises(coordination.CoordinationError):
            coordination.plan_task(self.root, self.task(turn='small-wildcard')['id'], {
                'classification': 'small', 'small_evidence': 'one concrete change',
                'deliverables': [self.deliverable(kind='documentation', executor='coordinator',
                                                  scope=['docs/*.md'])]})

    def test_small_task_with_evidence_keeps_its_explicit_executor(self):
        task = self.task(turn='small-ok')
        item = self.deliverable(kind='documentation', executor='coordinator', scope=['README.md'])
        planned = coordination.plan_task(self.root, task['id'], {
            'classification': 'small', 'small_evidence': 'one concrete typo in one file',
            'deliverables': [item]})
        self.assertEqual(planned['deliverables'][0]['executor'], 'coordinator')
        self.assertEqual(coordination.validate_task(self.root, task['id']), [])

    def test_manual_profiles_keep_explicit_executors(self):
        for level in (25, 75):
            with self.subTest(level=level):
                worker = self.task(turn=f'manual-{level}-worker', level=level)
                planned = self.plan(worker, [self.deliverable(executor='worker')])
                self.assertEqual(planned['deliverables'][0]['executor'], 'worker')
                self.assertEqual(coordination.validate_task(self.root, worker['id']), [])
                coordinator = self.task(turn=f'manual-{level}-coordinator', level=level)
                kept = self.plan(coordinator, [self.deliverable(executor='coordinator')])
                # A manual profile retains the explicit executor even when the
                # prediction would have chosen the worker.
                self.assertEqual(kept['deliverables'][0]['executor'], 'coordinator')
                self.assertEqual(kept['deliverables'][0]['routing']['action'], 'worker')
                self.assertEqual(kept['assignments'], [])

    def test_native_attestation_exception_survives_auto(self):
        task = self.task(turn='native')
        item = self.deliverable(kind='review', executor='native-agent', checks=[],
                                delegation_reason='independent native review in isolated context',
                                native_exception={'code': 'explicit_user_request',
                                                  'evidence': 'user asked for a native second reviewer'})
        planned = self.plan(task, [item])
        self.assertEqual(planned['deliverables'][0]['executor'], 'native-agent')
        self.assertEqual(coordination.validate_task(self.root, task['id']), [])
        for broken in ({}, {'delegation_reason': 'too'}):
            with self.subTest(broken=broken):
                other = self.task(turn='native-bad-' + str(len(broken)))
                bad = self.deliverable(kind='review', executor='native-agent', checks=[])
                bad.update(broken)
                with self.assertRaises(coordination.CoordinationError):
                    self.plan(other, [bad])

    def test_auto_routed_coordinator_outcomes_are_not_flagged(self):
        cases = {
            'risk': (self.task(turn='risk'), dict(features=self.features(risk='high'))),
            'unknown-localization': (self.task(turn='localization'),
                                     dict(features=self.features(localization='unknown'))),
            'no-checks': (self.task(turn='no-checks'), dict(checks=[])),
            'revoked-access': (self.task(turn='revoked', access='read-only'), {}),
        }
        for name, (task, changes) in cases.items():
            with self.subTest(name=name):
                planned = self.plan(task, [self.deliverable(**changes)])
                deliverable = planned['deliverables'][0]
                self.assertEqual(deliverable['executor'], 'coordinator')
                self.assertEqual(deliverable['routing']['requested_executor'], 'auto')
                self.assertEqual(planned['assignments'], [])
                self.assertEqual(coordination.validate_task(self.root, task['id']), [])
                diagnostics = coordination.routing_diagnostics(planned)[0]
                self.assertTrue(diagnostics['next_step'])
                self.assertTrue(diagnostics['reason_codes'])

    def test_forged_or_historical_explicit_executor_is_flagged(self):
        task = self.task(turn='forged')
        planned = self.plan(task, [self.deliverable()])
        forged = copy.deepcopy(coordination.load_task(self.root, task['id']))
        forged['deliverables'][0]['executor'] = 'coordinator'
        forged['deliverables'][0]['routing'] = {
            'decision_id': planned['deliverables'][0]['routing']['decision_id'],
            'requested_executor': 'coordinator', 'action': 'coordinator',
            'reason_codes': ['protected_kind'],
        }
        coordination._atomic(coordination._task_path(self.root, task['id']), forged)
        issues = coordination.validate_task(self.root, task['id'])
        self.assertTrue(any("executor:'auto'" in issue for issue in issues), issues)

    def test_started_legacy_explicit_plans_can_finish_with_original_binding(self):
        for mode in ('auto', 'advisory', 'shadow', 'off'):
            RoutingService(self.root).configure({'mode': mode})
            for executor in ('worker', 'coordinator'):
                with self.subTest(executor=executor, mode=mode):
                    task = self.task(turn='legacy-' + mode + '-' + executor)
                    item = self.deliverable(executor=executor)
                    # Simulate the previous release registering and starting a real
                    # explicit plan, including its original persisted decision.
                    with mock.patch.object(coordination, '_auto_executor_issue', return_value=None):
                        planned = self.plan(task, [copy.deepcopy(item)])
                        if executor == 'worker':
                            aid = planned['assignments'][0]['id']
                            coordination.assignment_started(
                                self.root, task['id'], aid, 'legacy-workspace', 'codex', [], effort='high')
                        else:
                            coordination.record_coordinator_event(
                                self.root, task['id'], 'mutation_requested', ['src/example.py'])
                    self.assertEqual(coordination.validate_task(self.root, task['id']), [])
                    self.assertEqual(self.plan(task, [copy.deepcopy(item)])['deliverables'][0]['routing'],
                                     planned['deliverables'][0]['routing'])
                    altered = coordination.load_task(self.root, task['id'])
                    altered['deliverables'][0]['scope'] = ['src/other.py']
                    coordination._atomic(coordination._task_path(self.root, task['id']), altered)
                    self.assertTrue(coordination.validate_task(self.root, task['id']))

    def test_unstarted_legacy_explicit_plan_still_requires_auto(self):
        task = self.task(turn='legacy-unstarted')
        with mock.patch.object(coordination, '_auto_executor_issue', return_value=None):
            self.plan(task, [self.deliverable(executor='coordinator')])
        self.assertTrue(coordination.validate_task(self.root, task['id']))

    def test_non_auto_router_mode_has_an_actionable_registration_error(self):
        for mode in ('advisory', 'shadow', 'off'):
            with self.subTest(mode=mode):
                self.service.configure({'mode': mode})
                with self.assertRaisesRegex(coordination.CoordinationError, 'routing configure --mode auto'):
                    self.plan(self.task(turn='mode-' + mode), [self.deliverable()])

    # -- identity metadata ------------------------------------------------
    def test_optional_metadata_joins_and_freezes_deliverable_identity(self):
        task = self.task(turn='identity')
        item = self.deliverable(write_scope=['src/example.py'],
                                decision_artifacts=['docs/decision.md'],
                                integration_of=['impl'])
        planned = self.plan(task, [copy.deepcopy(item)])
        stored = planned['deliverables'][0]
        self.assertEqual(stored['write_scope'], ['src/example.py'])
        self.assertEqual(stored['decision_artifacts'], ['docs/decision.md'])
        self.assertEqual(stored['integration_of'], ['impl'])
        aid = planned['assignments'][0]['id']
        coordination.assignment_started(self.root, task['id'], aid, 'ws', 'codex', [], effort='high')
        # An unchanged replan keeps the started deliverable and its decision.
        again = self.plan(task, [copy.deepcopy(item)])
        self.assertEqual(again['deliverables'][0]['routing'], stored['routing'])
        self.assertEqual(coordination.validate_task(self.root, task['id']), [])
        for changed in ({'write_scope': ['src/other.py']},
                        {'decision_artifacts': ['docs/other.md']},
                        {'integration_of': []}):
            with self.subTest(changed=changed):
                with self.assertRaisesRegex(coordination.CoordinationError, 'immutable'):
                    self.plan(task, [self.deliverable(**changed)])

    def test_absent_optional_metadata_keeps_historical_routing_bindings(self):
        task = self.task(turn='binding')
        planned = self.plan(task, [self.deliverable()])
        item = planned['deliverables'][0]
        self.assertNotIn('write_scope', item)
        identity = ('id', 'kind', 'scope', 'acceptance', 'dependencies', 'checks', 'features')
        self.assertEqual(coordination._definition(item), {key: item[key] for key in identity})
        legacy = coordination._binding(task['id'], dict(item))
        self.assertNotEqual(legacy, coordination._binding(
            task['id'], dict(item, write_scope=['src/example.py'])))
        aid = planned['assignments'][0]['id']
        coordination.assignment_started(self.root, task['id'], aid, 'ws', 'codex', [], effort='high')
        self.assertEqual(coordination.validate_task(self.root, task['id']), [])

    def test_malformed_optional_metadata_is_rejected(self):
        bad_fields = {
            'integration_of': ['not-a-list', [1], [''], ['bad id!'], 'impl'],
            'write_scope': ['src/example.py', [1], [''], ['/etc/passwd'], ['../escape.py'],
                            ['src/*.py'], ['src/../a.py'], ['..'], ['a\\b.py']],
            'decision_artifacts': ['docs/x.md', [None], [''], ['docs/../x.md'], ['docs/?x.md']],
        }
        for field, values in bad_fields.items():
            for value in values:
                with self.subTest(field=field, value=value):
                    task = self.task(turn=f'{field}-{len(str(value))}')
                    with self.assertRaises(coordination.CoordinationError) as caught:
                        self.plan(task, [self.deliverable(**{field: value})])
                    self.assertEqual(caught.exception.code, 64)
                    self.assertEqual(coordination.load_task(self.root, task['id'])['deliverables'], [])
                    self.assertEqual(self.decisions(), 0)

    # -- diagnostics ------------------------------------------------------
    def test_diagnostics_expose_retention_and_next_steps(self):
        expected = {
            'no-checks': ('declared_verification_required', 'executable checks'),
            'unknown-localization': ('ineligible_localization', 'diagnostic'),
            'revoked': ('write_requires_full_access', 'opt into full access'),
            'risk': ('ineligible_risk', 'coordinator'),
        }
        for name, (code, step_fragment) in expected.items():
            with self.subTest(name=name):
                access = 'read-only' if name == 'revoked' else 'full-access'
                changes = {}
                if name == 'no-checks':
                    changes['checks'] = []
                if name == 'unknown-localization':
                    changes['features'] = self.features(localization='unknown')
                if name == 'risk':
                    changes['features'] = self.features(risk='high')
                task = self.task(turn='diag-' + name, access=access)
                planned = self.plan(task, [self.deliverable(**changes)])
                row = coordination.routing_diagnostics(planned)[0]
                self.assertEqual(set(row), {'id', 'requested_executor', 'resolved_executor',
                                            'reason_codes', 'next_step'})
                self.assertEqual(row['id'], 'impl')
                self.assertEqual(row['requested_executor'], 'auto')
                self.assertEqual(row['resolved_executor'], 'coordinator')
                self.assertIn(code, row['reason_codes'])
                self.assertIn(step_fragment, row['next_step'])
                self.assertNotIn('redacted task', json.dumps(row))

    def test_delegated_diagnostics_have_no_next_step(self):
        task = self.task(turn='diag-worker')
        planned = self.plan(task, [self.deliverable()])
        row = coordination.routing_diagnostics(planned)[0]
        self.assertEqual(row['resolved_executor'], 'worker')
        self.assertIsNone(row['next_step'])
        self.assertIn('immediate_eligible', row['reason_codes'])

    def test_plan_cli_json_keeps_existing_keys_and_adds_diagnostics(self):
        task = self.task(turn='cli')
        payload = {'classification': 'substantial',
                   'deliverables': [self.deliverable(features=self.features(risk='high'))]}
        stream = io.StringIO()
        with mock.patch('sys.stdin', io.StringIO(json.dumps(payload))), \
                mock.patch('sys.stdout', stream):
            code = coordination_cli.main(['coordination', 'plan', '--path', str(self.root),
                                          '--task', task['id']])
        self.assertEqual(code, 0)
        printed = json.loads(stream.getvalue())
        for key in ('task', 'assignments', 'issues', 'diagnostics'):
            self.assertIn(key, printed)
        self.assertEqual(printed['diagnostics'][0]['id'], 'impl')
        self.assertEqual(printed['diagnostics'][0]['requested_executor'], 'auto')
        self.assertEqual(printed['diagnostics'][0]['resolved_executor'], 'coordinator')
        self.assertIn('ineligible_risk', printed['diagnostics'][0]['reason_codes'])
        self.assertTrue(printed['diagnostics'][0]['next_step'])

    def test_status_summary_includes_concise_routing_diagnostics(self):
        task = self.task(turn='summary')
        planned = self.plan(task, [self.deliverable(features=self.features(risk='high'))])
        text = coordination.summary(planned)
        self.assertIn('routing deliverable=impl requested=auto resolved=coordinator', text)
        self.assertIn('reasons=ineligible_risk', text)
        self.assertIn('next_step=', text)
        self.assertNotIn('redacted task', text)


if __name__ == '__main__':
    unittest.main()
