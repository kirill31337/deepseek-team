"""Native-agent delegation requires a supported coordinator attestation."""
from codex_deepseek_team import activation, coordination, native_delegation, settings
from test_coordination import CoordinationCase

USER_EXCEPTION = {"code": "explicit_user_request",
                  "evidence": "user asked to run this review with a native subagent"}
CAPABILITY_EXCEPTION = {"code": "native_capability",
                        "capability": "browser rendering harness",
                        "evidence": "rendering needs the browser tool that the worker sandbox lacks"}


class NativeCase(CoordinationCase):
    def new_task(self, level=75):
        self.plans = getattr(self, 'plans', 0) + 1
        return coordination.open_task(self.repo, session_id='native', turn_id=str(self.plans),
                                      prompt='implement feature', policy=self.policy(level))

    def plan(self, classification='substantial', executor='native-agent', item_id='native-work',
             kind='review', scope=None, level=25, **changes):
        task = self.new_task(level)
        item = dict(id=item_id, kind=kind, scope=scope or ['a.py'], executor=executor,
                    acceptance=['verified'], dependencies=[], checks=[])
        if executor == 'native-agent':
            item['delegation_reason'] = 'native subagent for this bounded deliverable'
        item.update(changes)
        plan = {'classification': classification, 'deliverables': [item]}
        if classification == 'small':
            plan['small_evidence'] = 'one bounded deliverable with a single result'
        return coordination.plan_task(self.repo, task['id'], plan)

    def save(self, task):
        coordination._atomic(coordination._task_path(self.repo, task['id']), task)

    def authorize(self, task, deliverable='native-work', tool='Task', tool_id='tu-1'):
        return coordination.authorize_native_dispatch(
            self.repo, task['id'], deliverable, tool, tool_id)


class AttestationTests(NativeCase):
    def test_native_plan_without_attestation_is_rejected_even_when_small(self):
        for classification in ('substantial', 'small'):
            with self.subTest(classification=classification):
                with self.assertRaises(coordination.CoordinationError) as caught:
                    self.plan(classification)
                self.assertIn('native_exception', str(caught.exception))

    def test_malformed_attestations_are_rejected(self):
        cases = [
            'parallelism',
            {'code': 'parallelism', 'evidence': 'independent isolated context convenience'},
            {'code': 'explicit_user_request'},
            {'code': 'explicit_user_request', 'evidence': 'short'},
            {'code': 'explicit_user_request', 'evidence': 'isolated context convenience'},
            {'code': 'native_capability', 'capability': 'parallelism',
             'evidence': 'run this next to the worker'},
            {'code': 'native_capability', 'capability': 'parallelism and isolated context',
             'evidence': 'run this next to the worker'},
            {'code': 'native_capability', 'evidence': 'missing the capability field'},
        ]
        for exception in cases:
            with self.subTest(exception=exception):
                with self.assertRaises(coordination.CoordinationError):
                    self.plan(native_exception=exception)

    def test_supported_user_request_and_capability_attestations_compile(self):
        for exception in (USER_EXCEPTION, CAPABILITY_EXCEPTION):
            with self.subTest(code=exception['code']):
                task = self.plan(level=25, native_exception=dict(exception))
                item = task['deliverables'][0]
                self.assertEqual(item['native_exception'], exception)
                self.assertEqual(item['delegation_reason'],
                                 'native subagent for this bounded deliverable')
                self.assertEqual(coordination.validate_task(self.repo, task['id']), [])

    def test_small_native_item_and_loaded_plan_are_validated(self):
        task = self.plan('small', native_exception=dict(USER_EXCEPTION))
        self.assertEqual(coordination.validate_task(self.repo, task['id']), [])
        task['deliverables'][0].pop('native_exception')
        self.save(task)
        issues = coordination.validate_task(self.repo, task['id'])
        self.assertTrue(any('native_exception' in issue for issue in issues), issues)
        with self.assertRaises(coordination.CoordinationError):
            coordination.complete_task(self.repo, task['id'])
        self.assertIsNotNone(coordination.native_exception_issue(task['deliverables'][0]))

    def test_validator_ignores_worker_and_coordinator_deliverables(self):
        for executor, level in (('worker', 75), ('coordinator', 25)):
            with self.subTest(executor=executor):
                self.assertIsNone(native_delegation.validate_native_exception(
                    {'id': 'x', 'kind': 'review', 'executor': executor}))
                task = self.plan('substantial', executor=executor, item_id='plain',
                                 level=level, checks=['python3 -V'])
                self.assertEqual(coordination.validate_task(self.repo, task['id']), [])

    def test_protected_and_unknown_kinds_stay_coordinator_only(self):
        with self.assertRaises(coordination.CoordinationError):
            self.plan(kind='architecture', native_exception=dict(USER_EXCEPTION))
        task = self.plan(native_exception=dict(USER_EXCEPTION))
        for kind in ('architecture', 'security', 'mystery_kind'):
            with self.subTest(kind=kind):
                task['deliverables'][0]['kind'] = kind
                self.save(task)
                events = len(task['coordinator_events'])
                with self.assertRaises(coordination.CoordinationError):
                    self.authorize(task, tool_id='tu-' + kind)
                self.assertEqual(len(coordination.load_task(self.repo, task['id'])
                                     ['coordinator_events']), events)

    def test_attestation_becomes_immutable_after_a_native_dispatch(self):
        task = self.plan(native_exception=dict(USER_EXCEPTION))
        dispatched = self.authorize(task)
        self.assertEqual(dispatched['status'], 'active')
        replanned = self.plan_input_replay(task)
        repeated = coordination.plan_task(self.repo, task['id'], replanned)
        self.assertEqual(repeated['deliverables'][0]['native_exception'], USER_EXCEPTION)
        self.assertEqual(len(repeated['deliverables'][0]['native_dispatches']), 1)
        replanned['deliverables'][0]['native_exception'] = dict(CAPABILITY_EXCEPTION)
        with self.assertRaises(coordination.CoordinationError):
            coordination.plan_task(self.repo, task['id'], replanned)

    def test_attestation_is_immutable_after_an_accepted_outcome(self):
        task = self.plan(level=25, native_exception=dict(USER_EXCEPTION))
        coordination.observe_coordinator_result(
            self.repo, task['id'], 'native-work', 'accepted', 'native findings verified')
        accepted = coordination.load_task(self.repo, task['id'])
        self.assertFalse(coordination.completion_issues(accepted))
        replay = self.plan_input_replay(accepted)
        replay['deliverables'][0]['native_exception'] = dict(CAPABILITY_EXCEPTION)
        with self.assertRaises(coordination.CoordinationError):
            coordination.plan_task(self.repo, task['id'], replay)
        unchanged = self.plan_input_replay(accepted)
        self.assertEqual(coordination.plan_task(self.repo, task['id'], unchanged)
                         ['deliverables'][0]['result']['outcome'], 'accepted')

    def plan_input_replay(self, task):
        item = dict(task['deliverables'][0])
        for key in ('routing', 'result', 'native_dispatches', 'native_dispatch_started_at'):
            item.pop(key, None)
        return {'classification': 'substantial', 'deliverables': [item]}


class DispatchCase(NativeCase):
    def plan_pair(self, level=75, native_kind='review'):
        """One worker deliverable plus one native deliverable in the same task."""
        task = self.new_task(level)
        worker = {'id': 'impl', 'kind': 'implementation', 'scope': ['a.py'],
                  'executor': 'worker', 'acceptance': ['implemented'],
                  'dependencies': [], 'checks': ['python3 -V']}
        native = {'id': 'native-work', 'kind': native_kind, 'scope': ['a.py'],
                  'executor': 'native-agent',
                  'delegation_reason': 'native subagent for this bounded deliverable',
                  'native_exception': dict(USER_EXCEPTION),
                  'acceptance': ['verified'], 'dependencies': [], 'checks': []}
        return coordination.plan_task(self.repo, task['id'], {
            'classification': 'substantial', 'deliverables': [worker, native]})


class DispatchAuthorizationTests(DispatchCase):
    def test_dispatch_records_only_binding_tool_and_tool_use_id(self):
        task = self.plan_pair()
        event_count = len(task['coordinator_events'])
        authorized = self.authorize(task, tool='Task', tool_id='tu-alpha')
        self.assertEqual(authorized['status'], 'active')
        events = authorized['coordinator_events']
        self.assertEqual(len(events), event_count + 1)
        self.assertEqual(events[-1], {
            'kind': 'native_dispatch', 'task_id': task['id'],
            'deliverable_id': 'native-work', 'tool_name': 'Task',
            'tool_use_id': 'tu-alpha', 'at': events[-1]['at']})
        self.assertNotIn('prompt', events[-1])
        item = authorized['deliverables'][1]
        self.assertEqual(item['native_dispatches'][0]['tool_use_id'], 'tu-alpha')
        self.assertIn('native_dispatch_started_at', item)
        self.assertEqual(coordination.summary(authorized).count('native-agent deliverable=native-work'), 1)

    def test_repeated_tool_use_id_is_idempotent_for_the_same_binding(self):
        task = self.plan_pair()
        first = self.authorize(task, tool_id='tu-alpha')
        again = self.authorize(task, tool_id='tu-alpha')
        self.assertEqual(again['coordinator_events'], first['coordinator_events'])
        self.assertEqual(len(again['deliverables'][1].get('native_dispatches', [])), 1)

    def test_tool_use_id_cannot_be_reused_for_another_binding(self):
        task = self.new_task(25)
        items = [
            {'id': 'first', 'kind': 'review', 'scope': ['a.py'], 'executor': 'native-agent',
             'delegation_reason': 'native subagent for this bounded deliverable',
             'native_exception': dict(USER_EXCEPTION),
             'acceptance': ['verified'], 'dependencies': [], 'checks': []},
            {'id': 'second', 'kind': 'review', 'scope': ['b.py'], 'executor': 'native-agent',
             'delegation_reason': 'native subagent for this bounded deliverable',
             'native_exception': dict(USER_EXCEPTION),
             'acceptance': ['verified'], 'dependencies': [], 'checks': []},
        ]
        task = coordination.plan_task(self.repo, task['id'], {
            'classification': 'substantial', 'deliverables': items})
        self.authorize(task, deliverable='first', tool_id='tu-shared')
        with self.assertRaises(coordination.CoordinationError) as caught:
            self.authorize(task, deliverable='second', tool_id='tu-shared')
        self.assertIn('idempotent', str(caught.exception))
        self.assertEqual(len(coordination.load_task(self.repo, task['id'])
                             ['deliverables'][1].get('native_dispatches', [])), 0)

    def test_foreign_task_binding_is_rejected(self):
        other = self.plan_pair()
        self.authorize(other, tool_id='tu-foreign')
        fresh = self.plan_pair()
        before = coordination.load_task(self.repo, fresh['id'])
        with self.assertRaises(coordination.CoordinationError) as caught:
            self.authorize(fresh, tool_id='tu-foreign')
        self.assertIn('foreign', str(caught.exception))
        self.assertEqual(coordination.load_task(self.repo, fresh['id']), before)

    def test_missing_binding_and_non_native_deliverables_are_rejected(self):
        task = self.plan_pair()
        before = coordination.load_task(self.repo, task['id'])
        for deliverable, tool_id in (('missing', 'tu-1'), ('impl', 'tu-2'),
                                     ('native-work', ''), ('native-work', None)):
            with self.subTest(deliverable=deliverable, tool_id=tool_id):
                with self.assertRaises(coordination.CoordinationError):
                    self.authorize(task, deliverable=deliverable, tool_id=tool_id)
                self.assertEqual(coordination.load_task(self.repo, task['id']), before)

    def test_dispatch_requires_current_activation_and_write_access(self):
        task = self.plan_pair()
        activation.set_enabled(self.repo, False)
        with self.assertRaises(coordination.CoordinationError) as caught:
            self.authorize(task, tool_id='tu-off')
        self.assertIn('disabled', str(caught.exception))
        activation.set_enabled(self.repo, True)
        settings.set_values(self.repo / settings.PROJECT_FILE, access='read-only')
        write_target = self.plan_pair(native_kind='implementation')
        with self.assertRaises(coordination.CoordinationError) as caught:
            self.authorize(write_target, tool_id='tu-write')
        self.assertIn('read-only', str(caught.exception))
        self.assertEqual(len(coordination.load_task(self.repo, write_target['id'])
                             ['deliverables'][1].get('native_dispatches', [])), 0)
        reader = self.plan(level=25, kind='review', native_exception=dict(USER_EXCEPTION))
        allowed = self.authorize(reader, tool_id='tu-read')
        self.assertEqual(allowed['deliverables'][0]['native_dispatches'][0]['tool_use_id'], 'tu-read')
        self.assertEqual(len(coordination.load_task(self.repo, task['id'])
                             ['deliverables'][1].get('native_dispatches', [])), 0)

    def test_dispatch_is_refused_for_terminal_items_and_finished_tasks(self):
        task = self.plan(level=25, native_exception=dict(USER_EXCEPTION))
        self.authorize(task, tool_id='tu-1')
        with self.assertRaises(coordination.CoordinationError):
            coordination.complete_task(self.repo, task['id'])
        coordination.observe_coordinator_result(
            self.repo, task['id'], 'native-work', 'accepted', 'native findings verified')
        with self.assertRaises(coordination.CoordinationError) as caught:
            self.authorize(task, tool_id='tu-2')
        self.assertIn('terminal', str(caught.exception))
        self.assertEqual(coordination.complete_task(self.repo, task['id'])['status'], 'completed')
        finished = coordination.load_task(self.repo, task['id'])
        finished['status'] = 'completed'
        self.save(finished)
        with self.assertRaises(coordination.CoordinationError) as caught:
            self.authorize(finished, tool_id='tu-3')
        self.assertIn('terminal', str(caught.exception))

    def test_a_stale_completed_task_without_outcomes_is_refused(self):
        task = self.plan(level=25, native_exception=dict(USER_EXCEPTION))
        stale = coordination.load_task(self.repo, task['id'])
        stale['status'] = 'completed'
        stale['completed_at'] = 0
        self.save(stale)
        with self.assertRaises(coordination.CoordinationError) as caught:
            self.authorize(stale, tool_id='tu-stale')
        self.assertIn('completed', str(caught.exception))
        cancelled = coordination.load_task(self.repo, task['id'])
        cancelled['status'] = 'active'
        cancelled['deliverables'][0]['result'] = {'outcome': 'cancelled', 'evidence': 'withdrawn'}
        self.save(cancelled)
        with self.assertRaises(coordination.CoordinationError) as caught:
            self.authorize(cancelled, tool_id='tu-cancelled')
        self.assertIn('cancelled', str(caught.exception))

    def test_dispatch_conflicts_with_a_pending_worker_write(self):
        task = self.new_task()
        items = [
            {'id': 'impl', 'kind': 'implementation', 'scope': ['src/core.py'],
             'executor': 'worker', 'acceptance': ['implemented'],
             'dependencies': [], 'checks': ['python3 -V']},
            {'id': 'native-work', 'kind': 'implementation', 'scope': ['src/core.py'],
             'executor': 'native-agent',
             'delegation_reason': 'native subagent for this bounded deliverable',
             'native_exception': dict(CAPABILITY_EXCEPTION),
             'acceptance': ['verified'], 'dependencies': [], 'checks': []},
        ]
        task = coordination.plan_task(self.repo, task['id'], {
            'classification': 'substantial', 'deliverables': items})
        with self.assertRaises(coordination.CoordinationError) as caught:
            self.authorize(task, tool_id='tu-conflict')
        self.assertIn('conflicts with pending worker assignment', str(caught.exception))
        self.assertEqual(len(coordination.load_task(self.repo, task['id'])
                             ['deliverables'][1].get('native_dispatches', [])), 0)

    def test_completion_still_requires_the_native_outcome(self):
        task = self.plan(level=25, native_exception=dict(USER_EXCEPTION))
        self.authorize(task, tool_id='tu-1')
        issues = coordination.completion_issues(coordination.load_task(self.repo, task['id']))
        self.assertTrue(any('native-agent outcome is outstanding' in issue for issue in issues), issues)
        coordination.observe_coordinator_result(
            self.repo, task['id'], 'native-work', 'cancelled', 'user withdrew the native request')
        self.assertEqual(coordination.complete_task(self.repo, task['id'])['status'], 'completed')

    def test_replayed_dispatch_rechecks_current_access_and_terminal_state(self):
        for change in ('access', 'disabled', 'accepted'):
            with self.subTest(change=change):
                activation.set_enabled(self.repo, True)
                settings.set_values(self.repo / settings.PROJECT_FILE, access='full-access')
                task = self.plan(level=25, kind='implementation', native_exception=dict(USER_EXCEPTION))
                call_id = 'replay-' + change
                self.authorize(task, tool_id=call_id)
                if change == 'access':
                    settings.set_values(self.repo / settings.PROJECT_FILE, access='read-only')
                elif change == 'disabled':
                    activation.set_enabled(self.repo, False)
                else:
                    coordination.observe_coordinator_result(self.repo, task['id'], 'native-work',
                                                           'accepted', 'Reviewed the final result')
                with self.assertRaises(coordination.CoordinationError):
                    self.authorize(task, tool_id=call_id)

    def test_same_call_id_cannot_change_tool(self):
        task = self.plan(level=25, native_exception=dict(USER_EXCEPTION))
        self.authorize(task, tool='spawn_agent', tool_id='same-call')
        with self.assertRaises(coordination.CoordinationError):
            self.authorize(task, tool='send_message', tool_id='same-call')

    def test_dispatched_native_deliverable_cannot_disappear_from_plan(self):
        task = self.plan(level=25, native_exception=dict(USER_EXCEPTION))
        self.authorize(task)
        with self.assertRaises(coordination.CoordinationError):
            coordination.plan_task(self.repo, task['id'], {'classification': 'substantial', 'deliverables': []})

    def test_manual_distribution_requirements_apply_before_native_dispatch(self):
        task = self.plan(level=75, native_exception=dict(USER_EXCEPTION))
        with self.assertRaises(coordination.CoordinationError):
            self.authorize(task)
