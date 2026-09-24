import os
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from codex_deepseek_team import activation, coordination, settings
from codex_deepseek_team.routing import RoutingService


class IntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        self.root = base / 'project'
        self.root.mkdir()
        subprocess.run(['git', 'init', '-q', str(self.root)], check=True)
        subprocess.run(['git', '-C', str(self.root), '-c', 'user.name=Tests', '-c', 'user.email=tests@example.invalid',
                        'commit', '-qm', 'initial', '--allow-empty'], check=True)
        env = patch.dict(os.environ, DEEPSEEK_TEAM_STATE_DIR=str(base / 'state'), XDG_CONFIG_HOME=str(base / 'config'))
        env.start()
        self.addCleanup(env.stop)
        settings.set_values(self.root / '.deepseek-team.toml', delegation_level=75, access='full-access')
        self.policy = settings.Policy('auto', 'full-access', {'access': 'test', 'delegation_level': 'test'})
        self.service = RoutingService(self.root)
        self.service.configure({'failure_cooldown_seconds': 300})
        self.features = dict(kind='implementation', domain='python', operation='fix', localization='known',
            coupling='local', verification='tests', clarity='clear', risk='low', scope_size='small',
            runtime='codex', model='deepseek-flash', effort='high', context_version='default')

    def task(self, policy=None, turn='1'):
        return coordination.open_task(self.root, session_id='test', turn_id=turn, prompt='redacted task', policy=policy or self.policy)

    def manual_policy(self, level=75, access='full-access'):
        """A fixed 25/50/75 snapshot, where an explicit executor stays allowed."""
        return settings.Policy(level, access, {'access': 'test', 'delegation_level': 'test'})

    def manual_task(self, turn='1', level=75, access='full-access'):
        return self.task(self.manual_policy(level, access), turn=turn)

    def plan(self, task, executor='auto', **changes):
        item = dict(id='fix', kind='implementation', executor=executor, scope=['src/example.py'],
                    acceptance=['Tests pass'], checks=['python3 -V'], dependencies=[], features=self.features)
        item.update(changes)
        return coordination.plan_task(self.root, task['id'], dict(classification='substantial', deliverables=[item]))

    def seed(self):
        for n in range(40):
            for action, cost in [('worker', .1), ('coordinator', 1.)]:
                self.service.observe(dict(id=f'{action}-{n}', case_id=f'case-{action}-{n}', origin='local',
                    features=self.features, action=action, outcome='accepted', observed_at=time.time()-10, cost_usd=cost))

    def test_cold_auto_delegates_without_cost_history(self):
        task = self.task()
        result = self.plan(task)
        self.assertEqual(result['deliverables'][0]['executor'], 'worker')
        self.assertEqual(coordination.validate_task(self.root, task['id']), [])
        self.assertIn('immediate_eligible', result['deliverables'][0]['routing']['reason_codes'])
        self.assertEqual(self.service.observations(), [])

    def test_supported_auto_delegates_and_freezes_before_execution(self):
        self.seed()
        task = self.task()
        result = self.plan(task)
        self.assertEqual(result['deliverables'][0]['executor'], 'worker')
        aid = result['assignments'][0]['id']
        coordination.assignment_started(self.root, task['id'], aid, 'workspace', 'codex', [], effort='medium')
        with self.assertRaises(coordination.CoordinationError):
            self.plan(task, scope=['src/changed.py'])
        with self.assertRaises(coordination.CoordinationError):
            self.plan(task, features={**self.features, 'domain': 'rust'})

    def test_actual_execution_must_match_auto_prediction(self):
        self.seed()
        task = self.task()
        result = self.plan(task)
        # Mismatched runtime or a different canonical level must never start the worker.
        for runtime, effort in (('claude', 'high'), ('codex', 'low'), ('codex', 'max')):
            with self.subTest(runtime=runtime, effort=effort), self.assertRaises(coordination.CoordinationError):
                coordination.assignment_started(self.root, task['id'], result['assignments'][0]['id'],
                                                 'workspace', runtime, [], effort=effort)
        self.assertEqual(coordination.load_task(self.root, task['id']), result)

    def test_legacy_saved_policy_preserves_unchanged_explicit_access(self):
        current = settings.Policy(25, 'read-only', dict(
            delegation_level='default', access='project:fixture',
            effort='project:fixture', max_workers='default'), effort='high')
        saved = dict(current.as_dict(), effort='medium')
        override = dict(saved, access='full-access', effective_access='full-access',
                        sources=dict(saved['sources'], access='cli'))
        task = {'policy': override, 'saved_policy': saved}
        self.assertEqual(coordination._effective_task_access(task, current), 'full-access')
        changed = settings.Policy(25, 'read-only', current.sources, effort='low')
        self.assertEqual(coordination._effective_task_access(task, changed), 'read-only')

    def test_legacy_medium_selection_matches_its_high_auto_decision(self):
        self.seed()
        task = self.task()
        result = self.plan(task)
        aid = result['assignments'][0]['id']
        # The legacy alias is normalized to high before the run-time comparison.
        started = coordination.assignment_started(self.root, task['id'], aid, 'workspace', 'codex', [], effort='medium')
        self.assertEqual(started['assignments'][0]['effort'], 'high')
        self.assertEqual(started['assignments'][0]['routing_features']['effort'], 'high')

    def test_exact_continuation_keeps_unchanged_explicit_access_override(self):
        settings.set_values(self.root / settings.PROJECT_FILE, delegation_level=25, access='read-only')
        policy = settings.resolve(self.root, access='full-access')
        task = self.task(policy)
        plan = self.plan(task, 'worker')
        aid = plan['assignments'][0]['id']
        started = coordination.assignment_started(self.root, task['id'], aid, 'ws', 'codex', [], effort='medium')
        again = coordination.assignment_started(self.root, task['id'], aid, 'ws', 'codex', [], effort='medium')
        self.assertEqual(again['assignments'], started['assignments'])
        activation.set_enabled(self.root, False)
        with self.assertRaises(coordination.CoordinationError):
            coordination.assignment_started(self.root, task['id'], aid, 'ws', 'codex', [], effort='medium')
        activation.set_enabled(self.root, True)
        settings.set_values(self.root / settings.PROJECT_FILE, delegation_level=75, access='read-only')
        with self.assertRaises(coordination.CoordinationError):
            coordination.assignment_started(self.root, task['id'], aid, 'ws', 'codex', [], effort='medium')

    def test_running_adaptive_reconciliation_does_not_relaunch_during_cooldown(self):
        self.enable_auto()
        self.seed()
        task = self.task()
        plan = self.plan(task)
        aid = plan['assignments'][0]['id']
        started = coordination.assignment_started(self.root, task['id'], aid, 'ws', 'codex', [], effort='medium')
        self.service.observe(dict(id='parallel-failure', case_id='parallel-case', origin='local',
            features=self.features, action='worker', outcome='rejected', observed_at=time.time()))
        again = coordination.assignment_started(self.root, task['id'], aid, 'ws', 'codex', [], effort='medium')
        self.assertEqual(again, started)

    def test_queued_automatic_start_rechecks_quality_failure_without_mutating_plan(self):
        self.enable_auto()
        task = self.task()
        plan = self.plan(task)
        aid = plan['assignments'][0]['id']
        decision_id = plan['deliverables'][0]['routing']['decision_id']
        decision = self.service.decision(decision_id)
        self.service.observe(dict(id='parallel-failure', case_id='parallel-case', origin='local',
            features=self.features, action='worker', outcome='rejected', observed_at=time.time()))
        with self.assertRaisesRegex(coordination.CoordinationError, 'cooldown'):
            coordination.assignment_started(self.root, task['id'], aid, 'ws', 'codex', [], effort='medium')
        self.assertEqual(coordination.load_task(self.root, task['id']), plan)
        self.assertEqual(self.service.decision(decision_id), decision)
        self.assertEqual(self.plan(task)['deliverables'][0]['routing'], plan['deliverables'][0]['routing'])
        other = self.plan(self.task(turn='other-family'), features={**self.features, 'domain': 'rust'})
        self.assertEqual(other['deliverables'][0]['executor'], 'worker')

    def test_queued_automatic_start_rechecks_current_routing_mode(self):
        self.enable_auto()
        task = self.task()
        plan = self.plan(task)
        aid = plan['assignments'][0]['id']
        for mode in ('off', 'shadow', 'advisory'):
            self.service.configure({'mode': mode})
            with self.subTest(mode=mode), self.assertRaisesRegex(coordination.CoordinationError, 'mode'):
                coordination.assignment_started(self.root, task['id'], aid, 'ws', 'codex', [], effort='medium')
        self.assertEqual(coordination.load_task(self.root, task['id'])['assignments'][0]['status'], 'planned')
        self.assertEqual(self.service.status()['decisions'], 1)

    def test_forged_routing_cannot_bypass_manual_profile(self):
        task = self.task(settings.resolve(self.root))
        self.plan(task, 'coordinator', routing={'action': 'coordinator', 'mode': 'auto', 'decision_id': 'fake'},
                  retention={'code': 'routing', 'evidence': 'fake decision'})
        self.assertTrue(coordination.validate_task(self.root, task['id']))

    def test_manual_worker_feedback_records_actual_conditions(self):
        task = self.task(settings.resolve(self.root))
        result = self.plan(task, 'worker')
        aid = result['assignments'][0]['id']
        coordination.assignment_started(self.root, task['id'], aid, 'workspace', 'codex', [], effort='high')
        coordination.assignment_finished(self.root, task['id'], aid, 'succeeded', 'done', ['src/example.py'], [])
        coordination.use_result(self.root, task['id'], aid, 'needs-rework', 'Tests caught incorrect behavior', cost_usd=.2)
        coordination.use_result(self.root, task['id'], aid, 'incorporated', 'Rework verified', cost_usd=.5)
        rows = self.service.observations()
        self.assertEqual(len(rows), 2)
        self.assertEqual({r['features']['effort'] for r in rows}, {'high'})
        self.assertEqual({r['case_id'] for r in rows}, {rows[0]['case_id']})
        forecast = self.service.predict({**self.features, 'effort': 'high'})
        self.assertLess(forecast['posterior']['mean'], .5)
        self.assertAlmostEqual(forecast['economics']['worker_mean_cost_usd'], .5)

    def test_feedback_outbox_survives_partial_failure(self):
        task = self.manual_task()
        result = self.plan(task, 'worker')
        aid = result['assignments'][0]['id']
        coordination.assignment_started(self.root, task['id'], aid, 'ws', 'codex', [], effort='medium')
        coordination.assignment_finished(self.root, task['id'], aid, 'succeeded', 'done', [], [])
        real_observe = RoutingService.observe
        def interrupted(service, observation):
            real_observe(service, observation)
            raise RuntimeError('interruption after SQLite commit')
        with patch.object(RoutingService, 'observe', autospec=True, side_effect=interrupted):
            with self.assertRaises(RuntimeError):
                coordination.use_result(self.root, task['id'], aid, 'incorporated', 'verified')
        coordination.sync_routing_feedback(self.root, task['id'])
        coordination.sync_routing_feedback(self.root, task['id'])
        self.assertEqual(len(self.service.observations()), 1)
        saved = coordination.load_task(self.root, task['id'])
        self.assertTrue(saved['assignments'][0]['routing_feedback'][0]['recorded'])

    def test_coordinator_feedback_freezes_scope_and_survives_replan(self):
        task = self.manual_task()
        self.plan(task, 'coordinator')
        coordination.observe_coordinator_result(self.root, task['id'], 'fix', 'rework', 'Needs correction', cost_usd=.2)
        revised = self.plan(task, 'coordinator')
        self.assertEqual(len(revised['deliverables'][0]['routing_feedback']), 1)
        with self.assertRaises(coordination.CoordinationError):
            self.plan(task, features={**self.features, 'domain': 'rust'})
        with self.assertRaises(coordination.CoordinationError):
            coordination.plan_task(self.root, task['id'], {'classification': 'substantial', 'deliverables': []})
        coordination.observe_coordinator_result(self.root, task['id'], 'fix', 'accepted', 'Corrected', cost_usd=.5)
        self.assertEqual(len(self.service.observations()), 2)

    def test_failed_checks_are_quality_evidence_and_retry_requires_review(self):
        task = self.manual_task()
        result = self.plan(task, 'worker')
        aid = result['assignments'][0]['id']
        coordination.assignment_started(self.root, task['id'], aid, 'ws', 'codex', [], effort='medium')
        coordination.assignment_finished(self.root, task['id'], aid, 'failed', 'Tests failed', [], [{'exit_code': 1}])
        self.assertEqual(self.service.observations()[0]['outcome'], 'rejected')
        with self.assertRaises(coordination.CoordinationError):
            coordination.assignment_started(self.root, task['id'], aid, 'ws', 'codex', [], effort='medium')
        coordination.use_result(self.root, task['id'], aid, 'needs-rework', 'Assertion fails')
        resumed = coordination.assignment_started(self.root, task['id'], aid, 'ws', 'codex', [], effort='medium')
        self.assertEqual(len(resumed['assignments'][0]['attempt_history']), 1)
        coordination.assignment_finished(self.root, task['id'], aid, 'succeeded', 'Corrected', [], [{'exit_code': 0}])
        coordination.use_result(self.root, task['id'], aid, 'incorporated', 'Rework verified')
        observations = self.service.observations()
        self.assertEqual([row['outcome'] for row in observations], ['rejected', 'rework', 'accepted'])
        self.assertEqual(len({row['case_id'] for row in observations}), 1)
        self.assertLess(self.service.predict(self.features)['posterior']['mean'], .5)

    def test_provider_failure_is_unlabelled(self):
        task = self.manual_task()
        result = self.plan(task, 'worker')
        aid = result['assignments'][0]['id']
        coordination.assignment_started(self.root, task['id'], aid, 'ws', 'codex', [], effort='medium')
        coordination.assignment_finished(self.root, task['id'], aid, 'failed', 'Transport unavailable', [], [], error_kind='provider')
        coordination.use_result(self.root, task['id'], aid, 'rejected', 'No result')
        self.assertTrue(all(row['outcome'] == 'infrastructure' for row in self.service.observations()))

    def test_permission_revocation_does_not_force_a_writer(self):
        task = self.task(settings.resolve(self.root))
        settings.set_values(self.root / '.deepseek-team.toml', access='read-only')
        result = self.plan(task)
        self.assertEqual(result['deliverables'][0]['executor'], 'coordinator')
        self.assertEqual(coordination.validate_task(self.root, task['id']), [])

    def test_manual_explicit_executor_overrides_prediction_but_auto_never_does(self):
        self.seed()
        # In Auto an explicit executor is a bypass, even when the prediction would
        # have chosen that same executor.
        with self.assertRaisesRegex(coordination.CoordinationError, "executor:'auto'"):
            self.plan(self.task(), 'coordinator')
        with self.assertRaisesRegex(coordination.CoordinationError, "executor:'auto'"):
            self.plan(self.task(turn='auto-worker'), 'worker')
        # A manual 25/50/75 snapshot keeps its explicit executor as before.
        result = self.plan(self.manual_task(turn='manual-coordinator'), 'coordinator')
        self.assertEqual(result['deliverables'][0]['routing']['action'], 'worker')
        self.assertEqual(result['assignments'], [])
        result = self.plan(self.manual_task(turn='native'), 'native-agent',
                           delegation_reason='Native connector required for the requested review',
                           native_exception={'code': 'native_capability', 'capability': 'connected issue tracker',
                                             'evidence': 'The review needs a connector unavailable in the worker sandbox'})
        self.assertEqual(result['deliverables'][0]['executor'], 'native-agent')

    def test_worker_snapshot_uses_actual_fixed_model(self):
        task = self.manual_task()
        result = self.plan(task, 'worker', features={**self.features, 'model': 'operator-mistake'})
        started = coordination.assignment_started(self.root, task['id'], result['assignments'][0]['id'], 'ws', 'codex', [], effort='medium')
        row = started['assignments'][0]
        self.assertEqual(row['routing_features']['model'], 'deepseek-flash')
        saved = self.service.decision(row['routing_decision_id'])
        self.assertLessEqual(saved['created_at'], row['started_at'])
        self.assertEqual(saved['features'], row['routing_features'])

    def test_coordinator_outcome_provides_cost_baseline(self):
        task = self.manual_task()
        self.plan(task, 'coordinator')
        coordination.observe_coordinator_result(self.root, task['id'], 'fix', 'accepted', 'Verified tests', cost_usd=1.)
        rows = self.service.observations()
        self.assertEqual(rows[0]['action'], 'coordinator')
        self.assertEqual(rows[0]['cost_usd'], 1.)

    def test_disabled_mode_never_resolves_auto(self):
        self.service.configure({'mode': 'shadow'})
        with self.assertRaises(coordination.CoordinationError):
            self.plan(self.task())

    def enable_auto(self):
        settings.set_values(self.root / '.deepseek-team.toml', delegation_level='auto', access='full-access')

    def test_invalid_or_unsaved_plan_does_not_persist_decisions_or_deliverables(self):
        self.enable_auto()
        task = self.task()
        item = dict(id='fix', kind='implementation', executor='auto', scope=['a.py'],
                    acceptance=['Tests pass'], checks=['python3 -V'], dependencies=[], features=self.features)
        for fail_write in (False, True):
            with self.subTest(fail_write=fail_write):
                plan = {'classification': 'substantial', 'deliverables': [item] if fail_write else [item, item]}
                if fail_write:
                    with patch.object(coordination, '_atomic', side_effect=OSError('simulated disk error')):
                        with self.assertRaises((coordination.CoordinationError, OSError)):
                            coordination.plan_task(self.root, task['id'], plan)
                else:
                    with self.assertRaises(coordination.CoordinationError):
                        coordination.plan_task(self.root, task['id'], plan)
                self.assertEqual(self.service.status()['decisions'], 0)
                self.assertFalse(coordination.load_task(self.root, task['id']).get('deliverables'))

    def test_failed_start_write_preserves_planned_assignment_and_decision(self):
        self.enable_auto()
        task = self.task()
        plan = self.plan(task)
        aid = plan['assignments'][0]['id']
        decision_id = plan['deliverables'][0]['routing']['decision_id']
        decision = self.service.decision(decision_id)
        with patch.object(coordination, '_atomic', side_effect=OSError('simulated disk error')):
            with self.assertRaises((coordination.CoordinationError, OSError)):
                coordination.assignment_started(self.root, task['id'], aid, 'ws', 'codex', [], effort='medium')
        self.assertEqual(coordination.load_task(self.root, task['id']), plan)
        self.assertEqual(self.service.decision(decision_id), decision)
        self.assertEqual(self.service.status()['decisions'], 1)
        started = coordination.assignment_started(self.root, task['id'], aid, 'ws', 'codex', [], effort='medium')
        self.assertEqual(started['assignments'][0]['status'], 'running')
        self.assertEqual(started['assignments'][0]['routing_decision_id'], decision_id)
        self.assertEqual(self.service.decision(decision_id), decision)

    def test_start_rechecks_current_access_for_auto_and_manual_writes(self):
        self.enable_auto()
        for executor in ('auto', 'worker'):
            with self.subTest(executor=executor):
                settings.set_values(self.root / '.deepseek-team.toml', access='full-access')
                policy = self.policy if executor == 'auto' else self.manual_policy()
                task = self.task(policy, turn=executor)
                plan = self.plan(task, executor)
                aid = plan['assignments'][0]['id']
                settings.set_values(self.root / '.deepseek-team.toml', access='read-only')
                with self.assertRaises(coordination.CoordinationError):
                    coordination.assignment_started(self.root, task['id'], aid, 'ws', 'codex', [], effort='medium')
                self.assertEqual(coordination.load_task(self.root, task['id'])['assignments'][0]['status'], 'planned')
        self.assertEqual(self.service.observations(), [])

    def test_stale_snapshot_cannot_grant_access_but_explicit_cli_override_is_preserved(self):
        settings.set_values(self.root / '.deepseek-team.toml', access='read-only')
        stale = self.manual_task(turn='stale', access='full-access')
        plan = self.plan(stale, 'worker')
        self.assertTrue(coordination.validate_task(self.root, stale['id']))
        with self.assertRaises(coordination.CoordinationError):
            coordination.assignment_started(self.root, stale['id'], plan['assignments'][0]['id'],
                                             'ws', 'codex', [], effort='medium')
        explicit = settings.resolve(self.root, delegation_level=75, access='full-access')
        allowed = self.task(explicit, turn='explicit-cli')
        plan = self.plan(allowed, 'worker')
        self.assertEqual(coordination.validate_task(self.root, allowed['id']), [])
        coordination.assignment_started(self.root, allowed['id'], plan['assignments'][0]['id'],
                                         'ws', 'codex', [], effort='medium')

    def test_never_started_or_already_finished_assignment_cannot_finish(self):
        self.enable_auto()
        task = self.task()
        plan = self.plan(task)
        aid = plan['assignments'][0]['id']
        with self.assertRaises(coordination.CoordinationError):
            coordination.assignment_finished(self.root, task['id'], aid, 'succeeded', 'invented', [], [])
        self.assertEqual(coordination.load_task(self.root, task['id']), plan)
        self.assertEqual(self.service.observations(), [])
        coordination.assignment_started(self.root, task['id'], aid, 'ws', 'codex', [], effort='medium')
        coordination.assignment_finished(self.root, task['id'], aid, 'succeeded', 'verified', [], [])
        with self.assertRaises(coordination.CoordinationError):
            coordination.assignment_finished(self.root, task['id'], aid, 'failed', 'changed', [], [])
        self.assertEqual(coordination.load_task(self.root, task['id'])['assignments'][0]['status'], 'succeeded')

    def test_replan_repairs_crash_after_ledger_rename_before_sqlite_commit(self):
        self.enable_auto()
        task = self.task()
        atomic = coordination._atomic
        def interrupted(path, value):
            atomic(path, value)
            raise OSError('simulated crash after ledger rename')
        with patch.object(coordination, '_atomic', side_effect=interrupted):
            with self.assertRaises(coordination.CoordinationError):
                self.plan(task)
        self.assertTrue(coordination.validate_task(self.root, task['id']))
        self.assertEqual(self.service.status()['decisions'], 0)
        repaired = self.plan(task)
        self.assertEqual(repaired['deliverables'][0]['executor'], 'worker')
        self.assertEqual(coordination.validate_task(self.root, task['id']), [])
        self.assertEqual(self.service.status()['decisions'], 1)

    def test_exact_start_continuation_reconciles_ledger_sqlite_crash_window(self):
        self.enable_auto()
        task = self.task()
        plan = self.plan(task)
        aid = plan['assignments'][0]['id']
        atomic = coordination._atomic
        def interrupted(path, value):
            atomic(path, value)
            raise OSError('simulated crash after ledger rename')
        with patch.object(coordination, '_atomic', side_effect=interrupted):
            with self.assertRaises(coordination.CoordinationError):
                coordination.assignment_started(self.root, task['id'], aid, 'ws', 'codex', [], effort='medium')
        interrupted_task = coordination.load_task(self.root, task['id'])
        self.assertEqual(interrupted_task['assignments'][0]['status'], 'running')
        decision_id = plan['deliverables'][0]['routing']['decision_id']
        decision = self.service.decision(decision_id)
        for _ in range(2):
            continued = coordination.assignment_started(self.root, task['id'], aid, 'ws', 'codex', [], effort='medium')
            self.assertEqual(continued, interrupted_task)
        self.assertEqual(self.service.decision(decision_id), decision)
        self.assertEqual(self.service.status()['decisions'], 1)
        with self.assertRaises(coordination.CoordinationError):
            coordination.assignment_started(self.root, task['id'], aid, 'another-ws', 'codex', [], effort='medium')

    def test_crash_reconciliation_bypasses_mode_but_respects_disable_and_access(self):
        self.enable_auto()
        task = self.task()
        plan = self.plan(task)
        aid = plan['assignments'][0]['id']
        atomic = coordination._atomic
        def interrupted(path, value):
            atomic(path, value)
            raise OSError('simulated crash after ledger rename')
        with patch.object(coordination, '_atomic', side_effect=interrupted):
            with self.assertRaises(coordination.CoordinationError):
                coordination.assignment_started(self.root, task['id'], aid, 'ws', 'codex', [], effort='medium')
        self.assertEqual(coordination.load_task(self.root, task['id'])['assignments'][0]['status'], 'running')
        for mode in ('off', 'shadow', 'advisory'):
            with self.subTest(mode=mode):
                self.service.configure({'mode': mode})
                continued = coordination.assignment_started(self.root, task['id'], aid, 'ws', 'codex', [], effort='medium')
                self.assertEqual(continued['assignments'][0]['status'], 'running')
        activation.set_enabled(self.root, False)
        with self.assertRaisesRegex(coordination.CoordinationError, 'disabled'):
            coordination.assignment_started(self.root, task['id'], aid, 'ws', 'codex', [], effort='medium')
        activation.set_enabled(self.root, True)
        settings.set_values(self.root / '.deepseek-team.toml', access='read-only')
        with self.assertRaisesRegex(coordination.CoordinationError, 'access'):
            coordination.assignment_started(self.root, task['id'], aid, 'ws', 'codex', [], effort='medium')
        self.assertEqual(self.service.status()['decisions'], 1)
        self.assertEqual(self.service.observations(), [])

    def test_real_process_exit_between_ledger_and_sqlite_commits_is_recoverable(self):
        self.enable_auto()
        task = self.task()
        script = '''
import json, os, sys
from pathlib import Path
from codex_deepseek_team import coordination
root, task_id, operation = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
atomic = coordination._atomic
def crash(path, value):
    atomic(path, value)
    os._exit(73)
coordination._atomic = crash
if operation == 'plan':
    coordination.plan_task(root, task_id, json.load(sys.stdin))
else:
    coordination.assignment_started(root, task_id, sys.argv[4], 'ws', 'codex', [], effort='medium')
'''
        item = dict(id='fix', kind='implementation', executor='auto', scope=['src/example.py'],
                    acceptance=['Tests pass'], checks=['python3 -V'], dependencies=[], features=self.features)
        payload = json.dumps({'classification': 'substantial', 'deliverables': [item]})
        command = [sys.executable, '-c', script, str(self.root), task['id']]
        result = subprocess.run([*command, 'plan'], input=payload, text=True, capture_output=True)
        self.assertEqual(result.returncode, 73, result.stderr)
        self.assertEqual(self.service.status()['decisions'], 0)
        self.assertTrue(coordination.validate_task(self.root, task['id']))
        repaired = self.plan(task)
        aid = repaired['assignments'][0]['id']
        result = subprocess.run([*command, 'start', aid], text=True, capture_output=True)
        self.assertEqual(result.returncode, 73, result.stderr)
        interrupted_task = coordination.load_task(self.root, task['id'])
        self.assertEqual(interrupted_task['assignments'][0]['status'], 'running')
        continued = coordination.assignment_started(self.root, task['id'], aid, 'ws', 'codex', [], effort='medium')
        self.assertEqual(continued, interrupted_task)
        self.assertEqual(self.service.status()['decisions'], 1)
        self.assertEqual(coordination.validate_task(self.root, task['id']), [])

    def test_abandon_interrupted_worker_requires_stopped_confirmation_and_no_active_owner(self):
        from codex_deepseek_team import workspace
        self.enable_auto()
        task = self.task()
        plan = self.plan(task)
        aid = plan['assignments'][0]['id']
        copy = workspace.create(self.root, Path(os.environ['DEEPSEEK_TEAM_STATE_DIR']))
        coordination.assignment_started(self.root, task['id'], aid, copy.id, 'codex', [], effort='medium')
        with self.assertRaises(coordination.CoordinationError):
            coordination.abandon_assignment(self.root, task['id'], aid, 'Inspected stopped processes')
        with copy.lock():
            with self.assertRaises(coordination.CoordinationError):
                coordination.abandon_assignment(self.root, task['id'], aid, 'Inspected', confirmed_stopped=True)
            self.assertEqual(coordination.load_task(self.root, task['id'])['assignments'][0]['status'], 'running')
        abandoned = coordination.abandon_assignment(self.root, task['id'], aid,
                              'Workspace inspected and worker process group confirmed stopped', confirmed_stopped=True)
        self.assertEqual(abandoned['assignments'][0]['status'], 'failed')
        self.assertEqual(abandoned['assignments'][0]['error_kind'], 'cancelled')
        self.assertEqual([row['outcome'] for row in self.service.observations()], ['cancelled'])

    def test_auto_cold_start_accounts_for_worker_outcome(self):
        self.enable_auto()
        task = self.task()
        result = self.plan(task)
        self.assertEqual(result['deliverables'][0]['executor'], 'worker')
        decision = self.service.decision(result['deliverables'][0]['routing']['decision_id'])
        self.assertEqual(decision['posterior']['matched_local'], 0)
        self.assertNotIn('evidence_reason_codes', decision)
        self.assertIn('immediate_eligible', decision['reason_codes'])
        aid = result['assignments'][0]['id']
        started = coordination.assignment_started(self.root, task['id'], aid, 'ws', 'codex', [], effort='medium')
        self.assertEqual(started['assignments'][0]['routing_decision_id'], decision['id'])
        coordination.assignment_finished(self.root, task['id'], aid, 'succeeded', 'Done', [], [])
        used = coordination.use_result(self.root, task['id'], aid, 'incorporated', 'Checks passed')
        self.assertEqual(used['assignments'][0]['disposition']['kind'], 'incorporated')
        observations = self.service.observations()
        self.assertEqual(len(observations), 1)
        self.assertEqual(observations[0]['outcome'], 'accepted')
        self.assertEqual(observations[0]['features'], self.features)
        self.assertEqual(self.service.decision(decision['id']), decision)

    def test_repeated_plan_reuses_decision_without_limiting_other_eligible_tasks(self):
        self.enable_auto()
        task = self.task()
        first = self.plan(task)
        repeated = self.plan(task)
        self.assertEqual(first['assignments'][0]['id'], repeated['assignments'][0]['id'])
        self.assertEqual(first['deliverables'][0]['routing'], repeated['deliverables'][0]['routing'])
        self.assertEqual(self.service.status()['decisions'], 1)
        for n in range(1, 12):
            result = self.plan(self.task(turn=f'other-{n}'))
            self.assertEqual(result['deliverables'][0]['executor'], 'worker')
        removed = coordination.plan_task(self.root, task['id'], {'classification': 'substantial', 'deliverables': []})
        self.assertEqual(removed['deliverables'], [])
        self.assertEqual(removed['assignments'], [])
        self.assertEqual(self.service.status()['decisions'], 12)
        result = self.plan(self.task(turn='after-removal'))
        self.assertEqual(result['deliverables'][0]['executor'], 'worker')

    def test_quality_cooldown_expires_without_erasing_failure_history(self):
        self.enable_auto()
        now = time.time()
        for index in range(30):
            self.service.observe(dict(id=f'failure-{index}', case_id=f'failed-case-{index}',
                origin='local', features=self.features, action='worker', outcome='rejected',
                observed_at=now - 7200))
        task = self.task()
        first = self.plan(task)
        decision = self.service.decision(first['deliverables'][0]['routing']['decision_id'])
        self.assertLess(decision['posterior']['mean'], .1)
        self.assertEqual(first['deliverables'][0]['executor'], 'worker')
        aid = first['assignments'][0]['id']
        coordination.assignment_started(self.root, task['id'], aid, 'ws', 'codex', [], effort='medium')
        coordination.assignment_finished(self.root, task['id'], aid, 'failed', 'Checks failed', [],
                                          [{'exit_code': 1}], error_kind='verification')
        coordination.use_result(self.root, task['id'], aid, 'rejected', 'Reproduced quality failure')
        for index in range(10):
            blocked = self.plan(self.task(turn=f'cooldown-{index}'))
            self.assertEqual(blocked['deliverables'][0]['executor'], 'coordinator')
        observations = self.service.observations()
        self.assertEqual(len(observations), 31)
        self.assertTrue(self.service.status()['admission']['active_cooldowns'])
        with patch('codex_deepseek_team.routing.time.time', return_value=now + 305):
            recovered = self.plan(self.task(turn='cooldown-finished'))
        self.assertEqual(recovered['deliverables'][0]['executor'], 'worker')
        self.assertIn('immediate_eligible', recovered['deliverables'][0]['routing']['reason_codes'])
        self.assertEqual(self.service.observations(), observations)
        decision = self.service.decision(recovered['deliverables'][0]['routing']['decision_id'])
        self.assertLess(decision['posterior']['mean'], .1)

    def test_immediate_admission_never_widens_permissions_or_relaxes_verification(self):
        self.enable_auto()
        settings.set_values(self.root / '.deepseek-team.toml', access='read-only')
        self.assertEqual(self.plan(self.task())['deliverables'][0]['executor'], 'coordinator')
        settings.set_values(self.root / '.deepseek-team.toml', access='full-access')
        self.assertEqual(self.plan(self.task(turn='no-checks'), checks=[])['deliverables'][0]['executor'], 'coordinator')
        self.assertEqual(self.plan(self.task(turn='high-risk'), features={**self.features, 'risk': 'high'})['deliverables'][0]['executor'], 'coordinator')
        self.assertEqual(self.service.observations(), [])


if __name__ == '__main__':
    unittest.main()
