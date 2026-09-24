"""Parent-hook native dispatch policy, distinct from source mutation checks."""
import re

from test_coordination import CoordinationCase
from codex_deepseek_team import activation, config, coordination, coordinator_hooks, project, settings
from codex_deepseek_team import claude_config


class NativeDispatchHookTests(CoordinationCase):
    def setUp(self):
        super().setUp()
        settings.set_values(self.repo / settings.PROJECT_FILE, delegation_level='auto')
        project.attach(self.repo, coordinator='both')

    def hook(self, event, runtime='codex', **extra):
        payload = dict(session_id='dispatch-session', turn_id='dispatch-turn',
                       cwd=str(self.repo), hook_event_name=event)
        payload.update(extra)
        return coordinator_hooks.handle(payload, runtime=runtime)

    def start_hook(self, runtime='codex'):
        self.hook('UserPromptSubmit', runtime, prompt='Explain the historical limit')
        return coordination.latest_task(self.repo, 'dispatch-session')

    def assert_denied(self, result):
        self.assertEqual(result.get('hookSpecificOutput', {}).get('permissionDecision'), 'deny', result)

    def test_small_readonly_question_cannot_spawn_without_distribution(self):
        self.start_hook()
        for name in ('spawn_agent', 'collaboration.spawn_agent', 'functions.spawn_agent', 'Agent', 'Task'):
            with self.subTest(name=name):
                self.assert_denied(self.hook('PreToolUse', tool_name=name,
                    tool_input={'prompt': 'Inspect git history independently'}))

    def test_new_work_sent_to_existing_agent_also_requires_routing(self):
        self.start_hook()
        for name in ('send_message', 'followup_task', 'send_input', 'assign_agent_task', 'resume_agent'):
            with self.subTest(name=name):
                self.assert_denied(self.hook('PreToolUse', tool_name=name,
                    tool_input={'id': 'existing-agent', 'message': 'Investigate another file'}))

    def test_control_and_exempt_reads_do_not_create_work(self):
        task = self.start_hook()
        for name, data in (('wait_agent', {}), ('resume_agent', {'id': 'existing-agent'}),
                           ('Read', {'file_path': 'AGENTS.md'}),
                           ('Bash', {'command': 'git log -1'})):
            self.assertEqual(self.hook('PreToolUse', tool_name=name, tool_input=data), {})
        self.assertEqual(self.hook('Stop'), {'continue': True})
        self.assertEqual(coordination.load_task(self.repo, task['id'])['status'], 'closed')

    def test_unplanned_source_read_nudges_then_denies(self):
        task = self.start_hook()
        reminder = self.hook('PreToolUse', tool_name='Read', tool_input={'file_path': 'a.py'})
        self.assertIn('early-planning', reminder['hookSpecificOutput']['additionalContext'])
        denied = self.hook('PreToolUse', tool_name='Read', tool_input={'file_path': 'a.py'})
        self.assertEqual(denied['hookSpecificOutput']['permissionDecision'], 'deny', denied)
        self.assertEqual(self.hook('Stop').get('decision'), 'block')
        self.assertEqual(coordination.load_task(self.repo, task['id'])['status'], 'planning')

    def test_off_bypasses_native_gate(self):
        task = self.start_hook()
        activation.set_enabled(self.repo, False)
        self.assertEqual(self.hook('PreToolUse', tool_name='spawn_agent', tool_input={'prompt': 'Research'}), {})
        self.assertEqual(coordination.load_task(self.repo, task['id'])['coordinator_events'], [])

    def test_claude_parent_launch_is_checked_but_child_lifecycle_is_not_rebound(self):
        self.start_hook('claude')
        self.assert_denied(self.hook('PreToolUse', 'claude', tool_name='Agent', tool_input={'prompt': 'Research'}))
        self.assertEqual(self.hook('PreToolUse', 'claude', agent_id='child',
                                  tool_name='Agent', tool_input={'prompt': 'Research'}), {})

    def test_installed_matchers_include_native_launch_and_work_messages(self):
        for matcher in (config.CODEX_HOOK_EVENTS['PreToolUse']['matcher'],
                        claude_config.HOOK_EVENTS['PreToolUse']):
            for name in ('spawn_agent', 'collaboration.spawn_agent', 'send_message', 'followup_task', 'Agent', 'Task'):
                with self.subTest(matcher=matcher, name=name):
                    self.assertIsNotNone(re.fullmatch(matcher, name))

    def test_foreign_or_ambiguous_marker_is_denied(self):
        task = self.start_hook()
        marker = '[deepseek-team:' + task['id'] + ':history]'
        for text in ('[deepseek-team:task-00000000000000000000:history]',
                     marker + ' ' + marker, marker + ' [deepseek-team:broken]'):
            with self.subTest(text=text):
                self.assert_denied(self.hook('PreToolUse', tool_name='spawn_agent',
                                            tool_input={'message': text}))

    def test_structured_work_cannot_bypass_marker_check(self):
        self.start_hook()
        for data in ({'message': {'text': 'Do work'}}, {'items': [{'type': 'text', 'text': 'Do work'}]}):
            self.assert_denied(self.hook('PreToolUse', tool_name='send_message', tool_input=data))

    def test_unknown_message_fields_fail_closed(self):
        self.start_hook()
        for key in ('content', 'body', 'text', 'description'):
            self.assert_denied(self.hook('PreToolUse', tool_name='send_message',
                                        tool_input={'id': 'existing-agent', key: 'Do new work'}))

    def test_structured_text_accepts_approved_marker(self):
        task = self.start_hook()
        self.native_plan(task)
        text = '[deepseek-team:' + task['id'] + ':history] Perform the scoped audit'
        result = self.hook('PreToolUse', tool_name='send_input', tool_use_id='structured-call',
                           tool_input={'id': 'existing-agent', 'items': [{'type': 'text', 'text': text}]})
        self.assertEqual(result, {})
        self.assertEqual(coordination.load_task(self.repo, task['id'])['coordinator_events'][-1]['kind'],
                         'native_dispatch')

    def test_freeform_patch_input_obeys_small_task_scope(self):
        task = self.start_hook()
        coordination.plan_task(self.repo, task['id'], {
            'classification': 'small', 'small_evidence': 'One local edit', 'deliverables': [{
                'id': 'edit', 'kind': 'implementation', 'scope': ['a.py'], 'executor': 'coordinator',
                'acceptance': ['Updated'], 'dependencies': [], 'checks': []}]})
        patch = '*** Begin Patch\n*** Add File: outside.py\n+VALUE = 2\n*** End Patch'
        for data in (patch, {'input': patch}, {'patch': patch}, {'unknown': patch}):
            self.assert_denied(self.hook('PreToolUse', tool_name='apply_patch', tool_input=data))

    def native_plan(self, task):
        return coordination.plan_task(self.repo, task['id'], {
            'classification': 'small', 'small_evidence': 'One independent tool-specific check',
            'deliverables': [{
                'id': 'history', 'kind': 'research', 'scope': ['a.py'], 'executor': 'native-agent',
                'delegation_reason': 'Required connector is only exposed to the native agent',
                'native_exception': {'code': 'native_capability', 'capability': 'connected issue tracker',
                                     'evidence': 'The requested issue audit needs the native connector unavailable in the worker sandbox'},
                'acceptance': ['Return source references'], 'dependencies': [], 'checks': [],
                'features': {'kind': 'research', 'domain': 'python', 'operation': 'review',
                             'localization': 'known', 'coupling': 'local', 'verification': 'manual',
                             'clarity': 'clear', 'risk': 'low', 'scope_size': 'small',
                             'runtime': 'codex', 'model': 'deepseek-flash', 'effort': 'high',
                             'context_version': 'dispatch-test'}
            }]})

    def test_approved_exception_records_dispatch_and_requires_result(self):
        task = self.start_hook()
        self.native_plan(task)
        marker = '[deepseek-team:' + task['id'] + ':history]'
        result = self.hook('PreToolUse', tool_name='spawn_agent', tool_use_id='call-1',
                           tool_input={'prompt': marker + ' Perform the scoped audit'})
        self.assertEqual(result, {})
        saved = coordination.load_task(self.repo, task['id'])
        self.assertEqual(saved['status'], 'active')
        self.assertEqual(saved['coordinator_events'][-1]['kind'], 'native_dispatch')
        self.assertEqual(self.hook('Stop').get('decision'), 'block')
        coordination.observe_coordinator_result(self.repo, task['id'], 'history', 'accepted', 'Reviewed source references')
        self.assertEqual(self.hook('Stop'), {'continue': True})

    def test_marker_cannot_reassign_worker_owned_scope(self):
        task = self.start_hook()
        planned = coordination.plan_task(self.repo, task['id'], {
            'classification': 'substantial', 'deliverables': [{
                'id': 'history', 'kind': 'research', 'scope': ['a.py'], 'executor': 'auto',
                'acceptance': ['Return commit references'], 'dependencies': [], 'checks': [],
                'features': {
                    'kind': 'research', 'domain': 'python', 'operation': 'review',
                    'localization': 'known', 'coupling': 'local', 'verification': 'manual',
                    'clarity': 'clear', 'risk': 'low', 'scope_size': 'small',
                    'runtime': 'codex', 'model': 'deepseek-flash', 'effort': 'high',
                    'context_version': 'dispatch-test',
                }}]})
        self.assertEqual(planned['deliverables'][0]['executor'], 'worker')
        self.assert_denied(self.hook('PreToolUse', tool_name='spawn_agent', tool_use_id='call-2',
            tool_input={'prompt': '[deepseek-team:' + task['id'] + ':history] Inspect history'}))
