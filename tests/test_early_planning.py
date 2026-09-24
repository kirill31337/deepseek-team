"""Focused checks for the early planning source-inspection gate.

Both coordinator runtimes share one implementation. The first recognized source
inspection of an unclassified task records a ``source_inspection`` coordinator
event and returns additional context; a later recognized read is denied until
the task is classified; a repeated delivery of the same nonempty tool use id is
idempotent. Status/bootstrap/instruction reads, waits, worker children and
disabled policy stay exempt, and opaque scripts remain outside bounded coverage.
"""
from codex_deepseek_team import (activation, claude_config, coordination,
                                 coordinator_hooks, project, settings)
from test_coordination import CoordinationCase

RUNTIMES = ('codex', 'claude')


class EarlyPlanningCase(CoordinationCase):
    def setUp(self):
        super().setUp()
        settings.set_values(self.repo / settings.PROJECT_FILE,
                            delegation_level=25, access='full-access')
        project.attach(self.repo, coordinator='both')
        (self.repo / 'src').mkdir()
        (self.repo / 'src' / 'core.py').write_text('VALUE = 1\n')

    def hook(self, runtime, session, event, **extra):
        payload = {'session_id': session, 'turn_id': 'turn-1', 'cwd': str(self.repo),
                   'hook_event_name': event, 'permission_mode': 'default'}
        payload.update(extra)
        return coordinator_hooks.handle(payload, runtime=runtime)

    def start(self, runtime, tag='main'):
        session = f'early-{runtime}-{tag}'
        self.hook(runtime, session, 'UserPromptSubmit', prompt='Investigate the parser')
        return coordination.latest_task(self.repo, session), session

    def stored(self, session):
        return coordination.latest_task(self.repo, session)

    @staticmethod
    def context(result):
        return result.get('hookSpecificOutput', {}).get('additionalContext', '')

    @staticmethod
    def decision(result):
        return result.get('hookSpecificOutput', {}).get('permissionDecision')

    @staticmethod
    def reason(result):
        return result.get('hookSpecificOutput', {}).get('permissionDecisionReason', '')

    @staticmethod
    def inspections(task):
        return [event for event in task['coordinator_events']
                if event['kind'] == 'source_inspection']

    def read(self, runtime, session, path, tool_use_id='read-1', tool='Read'):
        return self.hook(runtime, session, 'PreToolUse', tool_name=tool, tool_use_id=tool_use_id,
                         tool_input={'file_path': str(self.repo / path)})


class EarlyPlanningGateTests(EarlyPlanningCase):
    def test_first_inspection_nudges_and_later_reads_deny(self):
        for runtime in RUNTIMES:
            with self.subTest(runtime=runtime):
                task, session = self.start(runtime)
                first = self.read(runtime, session, 'src/core.py', tool_use_id='read-1')
                self.assertIsNone(self.decision(first))
                self.assertIn('coordination plan', self.context(first))
                self.assertEqual(len(self.inspections(self.stored(session))), 1)

                second = self.read(runtime, session, 'src/core.py', tool_use_id='read-2')
                self.assertEqual(self.decision(second), 'deny')
                self.assertIn('unclassified', self.reason(second))
                self.assertEqual(len(self.inspections(self.stored(session))), 1)
                self.assertEqual(self.stored(session)['id'], task['id'])

    def test_repeated_tool_use_id_is_idempotent(self):
        for runtime in RUNTIMES:
            with self.subTest(runtime=runtime):
                _, session = self.start(runtime)
                first = self.read(runtime, session, 'src/core.py', tool_use_id='tool-7')
                repeated = self.read(runtime, session, 'src/core.py', tool_use_id='tool-7')
                self.assertIsNone(self.decision(repeated))
                self.assertEqual(self.context(repeated), self.context(first))
                self.assertEqual(len(self.inspections(self.stored(session))), 1)

    def test_status_bootstrap_and_instruction_reads_are_exempt(self):
        exempt = [
            ('Read', {'file_path': str(self.repo / 'AGENTS.md')}),
            ('Read', {'file_path': str(self.repo / 'src')}),
            ('Grep', {'pattern': 'worker', 'path': str(self.repo / 'CLAUDE.md')}),
            ('Bash', {'command': 'git status --short'}),
            ('Bash', {'command': 'git rev-parse HEAD'}),
            ('Bash', {'command': 'cat AGENTS.md'}),
            ('exec_command', {'cmd': 'deepseek-team status'}),
        ]
        for runtime in RUNTIMES:
            for index, (tool, data) in enumerate(exempt):
                with self.subTest(runtime=runtime, tool=tool, data=data):
                    _, session = self.start(runtime, tag=f'exempt-{index}')
                    result = self.hook(runtime, session, 'PreToolUse', tool_name=tool,
                                       tool_use_id=f'exempt-{index}', tool_input=data)
                    self.assertEqual(result, {})
                    self.assertEqual(self.inspections(self.stored(session)), [])

    def test_mixed_status_and_source_read_counts(self):
        for runtime in RUNTIMES:
            with self.subTest(runtime=runtime):
                _, session = self.start(runtime, tag='mixed')
                result = self.hook(runtime, session, 'PreToolUse', tool_name='Bash',
                                   tool_use_id='mixed-1',
                                   tool_input={'command': 'git status --short && cat src/core.py'})
                self.assertIn('coordination plan', self.context(result))
                self.assertEqual(len(self.inspections(self.stored(session))), 1)

    def test_git_metadata_is_exempt_but_source_reads_count(self):
        for runtime in RUNTIMES:
            with self.subTest(runtime=runtime):
                _, session = self.start(runtime, tag='git')
                for index, command in enumerate(('git status', 'git rev-parse HEAD',
                                                 'git diff --stat', 'git diff --numstat')):
                    self.assertEqual(
                        self.hook(runtime, session, 'PreToolUse', tool_name='Bash',
                                  tool_use_id=f'git-{index}',
                                  tool_input={'command': command}), {})
                self.assertEqual(self.inspections(self.stored(session)), [])
                shown = self.hook(runtime, session, 'PreToolUse', tool_name='Bash',
                                  tool_use_id='git-show',
                                  tool_input={'command': 'git show HEAD'})
                self.assertIn('coordination plan', self.context(shown))
                denied = self.hook(runtime, session, 'PreToolUse', tool_name='Bash',
                                   tool_use_id='git-diff',
                                   tool_input={'command': 'git diff'})
                self.assertEqual(self.decision(denied), 'deny')

    def test_opaque_scripts_stay_outside_bounded_coverage(self):
        opaque = [
            "python3 -c \"print(open('src/core.py').read())\"",
            "bash tools/report.sh",
            "node -e \"require('fs').readFileSync('src/core.py')\"",
        ]
        for runtime in RUNTIMES:
            for index, command in enumerate(opaque):
                with self.subTest(runtime=runtime, command=command):
                    _, session = self.start(runtime, tag=f'opaque-{index}')
                    self.assertEqual(
                        self.hook(runtime, session, 'PreToolUse', tool_name='Bash',
                                  tool_use_id=f'opaque-{index}',
                                  tool_input={'command': command}), {})
                    self.assertEqual(self.inspections(self.stored(session)), [])
                    # An opaque script never consumes the first-inspection slot.
                    nudge = self.read(runtime, session, 'src/core.py', tool_use_id='after-opaque')
                    self.assertIn('coordination plan', self.context(nudge))

    def test_shell_inspection_covers_qualified_tool_names_and_input_keys(self):
        cases = [
            ('exec_command', {'cmd': 'sed -n 1,5p src/core.py'}),
            ('shell_command', {'command': 'head -n 5 src/core.py'}),
            ('functions.exec_command', {'cmd': 'cat src/core.py'}),
            ('mcp__shell__exec_command', {'cmd': 'rg -n VALUE src/core.py'}),
            ('Bash', {'command': 'grep -rn VALUE src'}),
            ('Bash', {'command': 'rg -n VALUE .'}),
        ]
        for runtime in RUNTIMES:
            for index, (tool, data) in enumerate(cases):
                with self.subTest(runtime=runtime, tool=tool):
                    _, session = self.start(runtime, tag=f'shell-{index}')
                    result = self.hook(runtime, session, 'PreToolUse', tool_name=tool,
                                       tool_use_id=f'shell-{index}', tool_input=data)
                    self.assertIn('coordination plan', self.context(result))
                    self.assertEqual(len(self.inspections(self.stored(session))), 1)

    def test_search_and_list_tools_are_recognized(self):
        for runtime in RUNTIMES:
            for index, (tool, data) in enumerate((
                    ('Grep', {'pattern': 'VALUE', 'path': 'src'}),
                    ('Glob', {'pattern': 'src/**/*.py'}),
                    ('search_text', {'query': 'VALUE'}))):
                with self.subTest(runtime=runtime, tool=tool):
                    _, session = self.start(runtime, tag=f'search-{index}')
                    result = self.hook(runtime, session, 'PreToolUse', tool_name=tool,
                                       tool_use_id=f'search-{index}', tool_input=data)
                    self.assertIn('coordination plan', self.context(result))
                    self.assertEqual(len(self.inspections(self.stored(session))), 1)

    def test_root_and_default_directory_searches_require_planning(self):
        cases = [('Grep', {'pattern': 'VALUE', 'path': '.'}),
                 ('Glob', {'pattern': '**/*.py', 'path': str(self.repo)}),
                 ('Bash', {'command': 'rg VALUE'}),
                 ('exec_command', {'cmd': 'rg --files'})]
        cases += [('Bash', {'command': command}) for command in (
            'git -C . diff', 'git -c core.pager=cat show HEAD:src/core.py',
            'git diff --stat -p',
            'git --git-dir=.git --work-tree=. log -p',
            'grep -rn VALUE', 'egrep --recursive VALUE', 'fgrep -Rn VALUE')]
        for runtime in RUNTIMES:
            for index, (tool, data) in enumerate(cases):
                with self.subTest(runtime=runtime, tool=tool, data=data):
                    _, session = self.start(runtime, tag=f'root-search-{index}')
                    first = self.hook(runtime, session, 'PreToolUse', tool_name=tool,
                                      tool_input=data, tool_use_id='first')
                    self.assertIn('coordination plan', self.context(first))
                    second = self.hook(runtime, session, 'PreToolUse', tool_name=tool,
                                       tool_input=data, tool_use_id='second')
                    self.assertEqual(self.decision(second), 'deny')

    def test_waits_and_worker_children_are_exempt(self):
        for runtime in RUNTIMES:
            _, session = self.start(runtime, tag='control')
            for index, (tool, data) in enumerate((
                    ('wait_agent', {}),
                    ('resume_agent', {'id': 'existing-agent'}),
                    ('Bash', {'command': 'deepseek-team worker --runtime codex --coord-task x'}))):
                self.assertEqual(self.hook(runtime, session, 'PreToolUse', tool_name=tool,
                                           tool_use_id=f'control-{index}', tool_input=data), {})
            self.assertEqual(self.inspections(self.stored(session)), [])

    def test_disabled_policy_exempts_inspection(self):
        for runtime in RUNTIMES:
            _, session = self.start(runtime, tag='disabled')
            activation.set_enabled(self.repo, False)
            self.assertEqual(self.read(runtime, session, 'src/core.py'), {})
            self.assertEqual(self.inspections(self.stored(session)), [])
            activation.set_enabled(self.repo, True)
            nudge = self.read(runtime, session, 'src/core.py', tool_use_id='after-on')
            self.assertIn('coordination plan', self.context(nudge))

    def test_inspection_counts_as_work_for_stop(self):
        for runtime in RUNTIMES:
            with self.subTest(runtime=runtime):
                # A conversational turn with no inspection still closes silently.
                self.hook(runtime, 'early-%s-quiet' % runtime, 'UserPromptSubmit', prompt='Hello')
                quiet = self.stored('early-%s-quiet' % runtime)
                self.assertNotEqual(
                    self.hook(runtime, 'early-%s-quiet' % runtime, 'Stop',
                              stop_hook_active=False).get('decision'), 'block')
                self.assertEqual(self.stored('early-%s-quiet' % runtime)['status'], 'closed')

                _, session = self.start(runtime, tag='stop')
                self.read(runtime, session, 'src/core.py')
                stopped = self.hook(runtime, session, 'Stop', stop_hook_active=False)
                self.assertEqual(stopped['decision'], 'block')
                self.assertIn('plan', stopped['reason'].lower())
                self.assertNotIn(self.stored(session)['status'], ('completed', 'closed'))

    def test_classified_task_allows_reads_without_a_new_event(self):
        for runtime in RUNTIMES:
            with self.subTest(runtime=runtime):
                task, session = self.start(runtime, tag='classified')
                coordination.plan_task(self.repo, task['id'], {
                    'classification': 'small',
                    'small_evidence': 'One constant changes in a single file.',
                    'deliverables': [{'id': 'change', 'kind': 'implementation', 'scope': ['src/core.py'],
                                      'executor': 'coordinator', 'acceptance': ['value checked'],
                                      'dependencies': [], 'checks': []}],
                })
                for index in range(2):
                    self.assertEqual(self.read(runtime, session, 'src/core.py',
                                               tool_use_id=f'classified-{index}'), {})
                self.assertEqual(self.inspections(self.stored(session)), [])

    def test_claude_plan_file_write_exception_is_preserved(self):
        _, session = self.start('claude', tag='plan')
        plans = claude_config.plans_directory(self.repo)
        plans.mkdir(parents=True, exist_ok=True)
        plan_file = plans / 'feature.md'
        for tool in ('Write', 'Edit'):
            result = self.hook('claude', session, 'PreToolUse', tool_name=tool,
                               tool_use_id=f'plan-{tool}', permission_mode='plan',
                               tool_input={'file_path': str(plan_file), 'content': '# Plan'})
            self.assertEqual(result, {})
        # Source edits in plan mode still require a distribution.
        denied = self.hook('claude', session, 'PreToolUse', tool_name='Write',
                           tool_use_id='plan-source', permission_mode='plan',
                           tool_input={'file_path': str(self.repo / 'src/core.py'),
                                       'content': 'VALUE = 2\n'})
        self.assertEqual(self.decision(denied), 'deny')


if __name__ == '__main__':
    import unittest
    unittest.main()
