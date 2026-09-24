"""Regression coverage for conversational turns and honest hook completion."""
from codex_deepseek_team import coordination, coordinator_hooks, project
from test_coordination import CoordinationCase


class LifecycleRegressions(CoordinationCase):
    def setUp(self):
        super().setUp()
        for runtime in ('codex', 'claude'):
            project.attach(self.repo, coordinator=runtime)

    def hook(self, runtime, event, **extra):
        return coordinator_hooks.handle({
            'session_id': runtime + '-session', 'turn_id': 'turn-1',
            'cwd': str(self.repo), 'hook_event_name': event, **extra,
        }, runtime=runtime)

    def task(self, runtime):
        return coordination.latest_task(self.repo, runtime + '-session')

    def plan(self, runtime, executor='coordinator'):
        task = self.task(runtime)
        deliverable = {
            'id': 'work', 'kind': 'review', 'scope': ['a.py'],
            'executor': executor, 'acceptance': ['reviewed'],
            'dependencies': [], 'checks': [],
        }
        if executor == 'native-agent':
            deliverable['delegation_reason'] = 'Independent review in isolated context'
            deliverable['native_exception'] = {
                'code': 'explicit_user_request',
                'evidence': 'User requested an independent native review for this scope',
            }
        return coordination.plan_task(self.repo, task['id'], {
            'classification': 'small',
            'small_evidence': 'One bounded review with a single result',
            'deliverables': [deliverable],
        })

    def test_conversational_turn_closes_without_distribution_or_completion_claim(self):
        for runtime in ('codex', 'claude'):
            with self.subTest(runtime=runtime):
                self.hook(runtime, 'UserPromptSubmit', prompt='Ты закончил выполнение задачи?')
                original = self.task(runtime)
                result = self.hook(runtime, 'Stop', stop_hook_active=False)
                self.assertNotEqual(result.get('decision'), 'block', result)
                self.assertNotEqual(result.get('continue'), False, result)
                self.assertEqual(self.task(runtime)['status'], 'closed')
                self.hook(runtime, 'UserPromptSubmit', turn_id='turn-2', prompt='Implement feature')
                self.assertNotEqual(self.task(runtime)['id'], original['id'])

    def test_status_prompt_retains_unfinished_assignment_and_repeated_stop_has_no_veto(self):
        for runtime in ('codex', 'claude'):
            with self.subTest(runtime=runtime):
                self.hook(runtime, 'UserPromptSubmit', prompt='Review feature')
                planned = self.plan(runtime, 'worker')
                aid = planned['assignments'][0]['id']
                coordination.assignment_started(self.repo, planned['id'], aid, 'ws', runtime, [])
                self.hook(runtime, 'UserPromptSubmit', turn_id='turn-2', prompt='What is the status?')
                self.assertEqual(self.task(runtime)['id'], planned['id'])
                first = self.hook(runtime, 'Stop', stop_hook_active=False)
                self.assertEqual(first['decision'], 'block')
                repeated = self.hook(runtime, 'Stop', stop_hook_active=True)
                self.assertNotIn('continue', repeated, repeated)
                self.assertNotIn('decision', repeated, repeated)
                self.assertIn('unfinished', repeated['systemMessage'])
                self.assertEqual(self.task(runtime)['assignments'][0]['status'], 'running')
                self.assertNotIn(self.task(runtime)['status'], ('completed', 'closed'))

    def test_stop_requires_coordinator_and_native_outcomes(self):
        for runtime in ('codex', 'claude'):
            for executor in ('coordinator', 'native-agent'):
                with self.subTest(runtime=runtime, executor=executor):
                    self.hook(runtime, 'UserPromptSubmit', turn_id='turn-' + executor,
                              prompt='Review feature')
                    planned = self.plan(runtime, executor)
                    blocked = self.hook(runtime, 'Stop', stop_hook_active=False)
                    self.assertEqual(blocked.get('decision'), 'block', blocked)
                    self.assertIn('work', blocked['reason'])
                    coordination.observe_coordinator_result(
                        self.repo, planned['id'], 'work', 'accepted', 'Review completed')
                    result = self.hook(runtime, 'Stop', stop_hook_active=False)
                    self.assertTrue(result.get('continue', True), result)
                    self.assertEqual(self.task(runtime)['status'], 'completed')

    def test_read_only_shell_text_does_not_require_distribution(self):
        # The quoted '>' only exercises the shell lexer: documentation stays a
        # metadata read, while a source path would now open the planning gate.
        (self.repo / 'README.md').write_text('Documented value threshold.\n')
        commands = [
            "python3 -c 'print(2 > 1)'",
            "rg 'value>=limit' README.md",
            "python3 - <<'PY'\nprint(2 >= 1)\nPY\n",
        ]
        for runtime in ('codex', 'claude'):
            self.hook(runtime, 'UserPromptSubmit', prompt='Inspect status')
            for command in commands:
                with self.subTest(runtime=runtime, command=command):
                    self.assertEqual(self.hook(runtime, 'PreToolUse', tool_name='Bash',
                                               tool_input={'command': command}), {})

    def test_mixed_coordinator_command_cannot_bypass_mutation_gate(self):
        for runtime in ('codex', 'claude'):
            self.hook(runtime, 'UserPromptSubmit', prompt='Implement feature')
            denied = self.hook(runtime, 'PreToolUse', tool_name='Bash', tool_input={
                'command': 'deepseek-team coordination status; touch a.py',
            })
            self.assertEqual(denied['hookSpecificOutput']['permissionDecision'], 'deny')

    def test_later_edit_requires_fresh_acceptance(self):
        for runtime in ('codex', 'claude'):
            self.hook(runtime, 'UserPromptSubmit', prompt='Review feature')
            planned = self.plan(runtime)
            coordination.observe_coordinator_result(
                self.repo, planned['id'], 'work', 'accepted', 'Initial check passed')
            self.assertEqual(self.hook(runtime, 'PreToolUse', tool_name='Write',
                                       tool_input={'file_path': 'a.py', 'content': 'VALUE=2'}), {})
            blocked = self.hook(runtime, 'Stop', stop_hook_active=False)
            self.assertEqual(blocked.get('decision'), 'block', blocked)
            self.assertNotEqual(self.task(runtime)['status'], 'completed')

    def test_absolute_and_nested_cwd_edits_invalidate_acceptance(self):
        nested = self.repo / 'nested'
        nested.mkdir()
        for runtime in ('codex', 'claude'):
            self.hook(runtime, 'UserPromptSubmit', prompt='Review feature')
            planned = self.plan(runtime)
            for cwd, path in ((self.repo, str(self.repo / 'a.py')), (nested, '../a.py')):
                with self.subTest(runtime=runtime, cwd=cwd, path=path):
                    coordination.observe_coordinator_result(
                        self.repo, planned['id'], 'work', 'accepted', 'Checked current contents')
                    self.assertEqual(self.hook(runtime, 'PreToolUse', cwd=str(cwd),
                                               tool_name='Write', tool_input={'file_path': path}), {})
                    self.assertEqual(self.hook(runtime, 'Stop').get('decision'), 'block')

    def test_apply_patch_rename_destination_must_also_be_planned(self):
        for runtime in ('codex', 'claude'):
            self.hook(runtime, 'UserPromptSubmit', prompt='Review feature')
            self.plan(runtime)
            denied = self.hook(runtime, 'PreToolUse', tool_name='apply_patch', tool_input={
                'command': '*** Begin Patch\n*** Update File: a.py\n*** Move to: other.py\n'
                           '@@\n-VALUE = 1\n+VALUE = 2\n*** End Patch',
            })
            self.assertEqual(denied.get('hookSpecificOutput', {}).get('permissionDecision'),
                             'deny', denied)

    def test_directory_mutation_cannot_cover_a_pending_workers_file(self):
        self.check_directory_mutation('src/worker.py')

    def test_directory_mutation_cannot_cover_a_pending_workers_glob(self):
        self.check_directory_mutation('src/subdir/*')

    def test_project_root_copy_cannot_cover_a_pending_workers_file(self):
        self.check_directory_mutation('a.py', '.', 'cp replacement/a.py .')

    def check_directory_mutation(self, worker_scope, directory='src', command='rm -r src'):
        for runtime in ('codex', 'claude'):
            self.hook(runtime, 'UserPromptSubmit', prompt='Implement feature')
            task = self.task(runtime)
            coordination.plan_task(self.repo, task['id'], {
                'classification': 'substantial',
                'deliverables': [
                    {'id': 'directory', 'kind': 'integration', 'scope': [directory],
                     'executor': 'coordinator', 'acceptance': ['integrated'],
                     'dependencies': [], 'checks': []},
                    {'id': 'worker', 'kind': 'implementation', 'scope': [worker_scope],
                     'executor': 'worker', 'acceptance': ['implemented'],
                     'dependencies': [], 'checks': []},
                ],
            })
            denied = self.hook(runtime, 'PreToolUse', tool_name='Bash',
                               tool_input={'command': command})
            self.assertEqual(denied.get('hookSpecificOutput', {}).get('permissionDecision'),
                             'deny', denied)
