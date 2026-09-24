"""Claude's documented hook protocol, exercised through the installed CLI boundary."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from codex_deepseek_team import activation, coordination, project


COMMAND = 'deepseek-team coordinator-hook --runtime claude'


class ClaudeHooksTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='dst-claude-hooks-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.repo = self.root / 'repo'
        self.repo.mkdir()
        subprocess.run(['git', 'init', '-q', str(self.repo)], check=True)
        (self.repo / 'a.py').write_text('VALUE = 1\n')
        subprocess.run(['git', '-C', str(self.repo), 'add', '.'], check=True)
        subprocess.run(['git', '-C', str(self.repo), '-c', 'user.name=Test', '-c',
                        'user.email=test@example.test', 'commit', '-qm', 'base'], check=True)
        self.home = self.root / 'home'
        self.home.mkdir()
        self.claude = self.root / 'custom-claude'
        self.settings = self.claude / 'settings.json'
        self.state = self.root / 'state'
        environment = mock.patch.dict(os.environ, {
            'HOME': str(self.home), 'CLAUDE_CONFIG_DIR': str(self.claude),
            'CODEX_HOME': str(self.home / '.codex'),
            'XDG_CONFIG_HOME': str(self.root / 'config'),
            'DEEPSEEK_TEAM_STATE_DIR': str(self.state),
            'DEEPSEEK_TEAM_DISABLED': '',
            'PYTHONPATH': str(Path(__file__).resolve().parents[1] / 'src'),
        })
        environment.start()
        self.addCleanup(environment.stop)

    def cli(self, *args, input=''):
        return subprocess.run([sys.executable, '-m', 'codex_deepseek_team', *args],
                              cwd=self.repo, input=input, text=True,
                              capture_output=True, timeout=15)

    def hook(self, event, **extra):
        # Claude does not supply a turn_id. Absolute file paths are normal.
        payload = dict(hook_event_name=event, cwd=str(self.repo), session_id='claude-session')
        payload.update(extra)
        result = self.cli('coordinator-hook', '--runtime', 'claude', input=json.dumps(payload))
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        return json.loads(result.stdout) if result.stdout.strip() else {}

    def start(self):
        project.attach(self.repo, coordinator='claude')
        result = self.hook('UserPromptSubmit', prompt='Implement the feature')
        self.assertIn('coordination task', result['hookSpecificOutput']['additionalContext'].lower())
        return coordination.latest_task(self.repo, 'claude-session')

    def plan(self, task, executor='coordinator', scope='a.py'):
        deliverable = {
            'id': 'change', 'kind': 'implementation', 'executor': executor,
            'scope': [scope], 'acceptance': ['value checked'],
            'dependencies': [], 'checks': [],
        }
        if executor == 'auto':
            # A substantial Auto plan needs a real feature card; the saved routing
            # decision must resolve this read deliverable to a DeepSeek worker.
            deliverable['kind'] = 'review'
            deliverable['features'] = {
                'kind': 'review', 'domain': 'python', 'operation': 'review',
                'localization': 'known', 'coupling': 'local', 'verification': 'manual',
                'clarity': 'clear', 'risk': 'low', 'scope_size': 'small',
                'runtime': 'claude', 'model': 'deepseek-flash', 'effort': 'high',
                'context_version': 'claude-hooks-fixture',
            }
        planned = coordination.plan_task(self.repo, task['id'], {
            'classification': 'small' if executor == 'coordinator' else 'substantial',
            'small_evidence': 'One constant changes in a single file.',
            'deliverables': [deliverable],
        })
        if executor == 'auto':
            self.assertEqual(planned['deliverables'][0]['executor'], 'worker')
        return planned

    def assert_denied(self, result, reason):
        specific = result['hookSpecificOutput']
        self.assertEqual(specific['hookEventName'], 'PreToolUse')
        self.assertEqual(specific['permissionDecision'], 'deny')
        self.assertIn(reason, specific['permissionDecisionReason'].lower())

    def test_install_is_idempotent_and_remove_preserves_user_settings_and_auth(self):
        self.claude.mkdir()
        original = {
            'model': 'user-model', 'permissions': {'deny': ['Bash(rm *)']},
            'hooks': {'PreToolUse': [{'matcher': 'Read', 'hooks': [
                {'type': 'command', 'command': 'my-hook'},
                {'type': 'command', 'command': COMMAND + ' --custom'},
            ]}]},
        }
        self.settings.write_text(json.dumps(original))
        self.settings.chmod(0o640)
        auth = self.claude / '.credentials.json'
        auth.write_bytes(b'private authentication fixture')
        self.assertEqual(self.cli('hooks', 'install', '--runtime', 'claude').returncode, 0)
        first = self.settings.read_bytes()
        self.assertEqual(self.cli('hooks', 'install', '--runtime', 'claude').returncode, 0)
        self.assertEqual(self.settings.read_bytes(), first)
        self.assertEqual(self.settings.stat().st_mode & 0o777, 0o640)
        self.assertEqual(self.cli('hooks', 'status', '--runtime', 'claude').returncode, 0)
        data = json.loads(first)
        self.assertEqual(set(data['hooks']), {'SessionStart', 'UserPromptSubmit', 'PreToolUse', 'Stop'})
        for groups in data['hooks'].values():
            for group in groups:
                for handler in group['hooks']:
                    self.assertNotIn('additionalContextLimit', handler)  # Codex-only field
        self.assertEqual(self.cli('hooks', 'remove', '--runtime', 'claude').returncode, 0)
        self.assertEqual(json.loads(self.settings.read_bytes()), original)
        self.assertEqual(auth.read_bytes(), b'private authentication fixture')
        self.assertFalse((self.home / '.codex').exists())
        self.assertFalse((self.home / '.claude').exists())

    def test_status_detects_wrong_event_matcher_and_disabled_hooks(self):
        self.assertEqual(self.cli('hooks', 'install', '--runtime', 'claude').returncode, 0)
        original = json.loads(self.settings.read_bytes())
        mutations = [
            lambda data: data['hooks']['PreToolUse'][0].update(matcher='Read'),
            lambda data: data['hooks']['PreToolUse'][0]['hooks'][0].update({'async': True}),
            lambda data: data.update(disableAllHooks=True),
            lambda data: data['hooks'].update(Stop=[]),
        ]
        for change in mutations:
            data = json.loads(json.dumps(original))
            change(data)
            self.settings.write_text(json.dumps(data))
            result = self.cli('hooks', 'status', '--runtime', 'claude')
            self.assertEqual(result.returncode, 78, result.stdout + result.stderr)

    def test_install_preserves_explicit_disable_all_hooks(self):
        self.claude.mkdir()
        self.settings.write_text('{"disableAllHooks": true, "model": "mine"}')
        self.assertEqual(self.cli('hooks', 'install', '--runtime', 'claude').returncode, 0)
        self.assertTrue(json.loads(self.settings.read_bytes())['disableAllHooks'])
        self.assertEqual(self.cli('hooks', 'status', '--runtime', 'claude').returncode, 78)

    def test_invalid_or_symlinked_settings_are_preserved(self):
        self.claude.mkdir()
        for raw in (b'broken JSON', b'[]', b'{"hooks": []}', b'{"hooks": {"Stop": {}}}'):
            self.settings.write_bytes(raw)
            for action in ('install', 'remove', 'status'):
                result = self.cli('hooks', action, '--runtime', 'claude')
                self.assertEqual(result.returncode, 78, result.stdout + result.stderr)
                self.assertEqual(self.settings.read_bytes(), raw)
        target = self.root / 'foreign.json'
        target.write_bytes(b'{}')
        self.settings.unlink()
        self.settings.symlink_to(target)
        self.assertEqual(self.cli('hooks', 'install', '--runtime', 'claude').returncode, 78)
        self.assertEqual(target.read_bytes(), b'{}')

    def test_install_and_reset_manage_only_claude_hooks(self):
        result = self.cli('hooks', 'install', '--runtime', 'claude')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.cli('hooks', 'status', '--runtime', 'claude').returncode, 0)
        self.assertEqual(self.cli('reset', '--runtime', 'claude').returncode, 0)
        self.assertEqual(self.cli('hooks', 'status', '--runtime', 'claude').returncode, 78)
        self.assertFalse((self.home / '.codex').exists())

    def test_unattached_or_codex_only_project_is_inert(self):
        for attach in (False, True):
            if attach:
                project.attach(self.repo, coordinator='codex')
            self.assertEqual(self.hook('UserPromptSubmit', prompt='edit'), {})
            self.assertEqual(self.hook('Stop'), {})
        self.assertFalse(self.state.exists())

    def test_context_is_restored_on_resume_and_compaction(self):
        task = self.start()
        for source in ('resume', 'compact'):
            restored = self.hook('SessionStart', source=source)
            context = restored['hookSpecificOutput']['additionalContext']
            self.assertIn(task['id'], context)
            self.assertIn('Claude', context)
            self.assertIn('--runtime claude', context)

    def test_edit_write_and_notebook_edit_require_distribution(self):
        self.start()
        for tool, key in [('Edit', 'file_path'), ('Write', 'file_path'), ('NotebookEdit', 'notebook_path')]:
            self.assert_denied(self.hook('PreToolUse', tool_name=tool,
                                        tool_input={key: str(self.repo / 'a.py')}), 'distribution')
        # The first recognized source read of an unclassified task returns a
        # planning reminder; the next one is denied until a plan exists.
        reminder = self.hook('PreToolUse', tool_name='Read', tool_input={'file_path': 'a.py'})
        self.assertIn('early-planning', reminder['hookSpecificOutput']['additionalContext'])
        self.assert_denied(self.hook('PreToolUse', tool_name='Read',
                                    tool_input={'file_path': 'b.py'}), 'distribution')
        self.assertEqual(self.hook('PreToolUse', tool_name='Bash',
                                   tool_input={'command': 'deepseek-team off'}), {})

    def test_absolute_relative_and_symlink_paths_use_project_scopes(self):
        task = self.start()
        (self.repo / 'sub').mkdir()
        # The symlinked scope is checked before any recorded mutation, because a
        # started deliverable keeps its registered scope and features.
        (self.repo / 'outside.py').symlink_to(self.root / 'foreign.py')
        self.plan(task, scope='outside.py')
        self.assert_denied(self.hook('PreToolUse', tool_name='Write',
                                    tool_input={'file_path': str(self.repo / 'outside.py')}), 'scope')
        self.plan(task, scope='a.py')
        for path in (str(self.repo / 'a.py'), 'a.py', 'sub/../a.py'):
            self.assertEqual(self.hook('PreToolUse', tool_name='Edit',
                                       tool_input={'file_path': path}), {})
        self.assertEqual(self.hook('PreToolUse', cwd=str(self.repo / 'sub'), tool_name='Write',
                                   tool_input={'file_path': '../a.py'}), {})

    def test_absolute_and_dot_prefixed_plan_scopes_match_native_file_paths(self):
        self.start()
        # A recorded mutation freezes the registered scope, so each spelling is
        # checked on its own task instead of replanning one deliverable.
        for index, scope in enumerate((str(self.repo / 'a.py'), './a.py')):
            with self.subTest(scope=scope):
                session = 'scope-' + str(index)
                self.hook('UserPromptSubmit', prompt='Implement the feature', session_id=session)
                task = coordination.latest_task(self.repo, session)
                self.plan(task, scope=scope)
                self.assertEqual(self.hook('PreToolUse', session_id=session, tool_name='Edit',
                                           tool_input={'file_path': str(self.repo / 'a.py')}), {})

    def test_plan_mode_can_write_native_plan_but_not_source_or_symlink_escape(self):
        self.start()
        plan = self.claude / 'plans' / 'feature.md'
        plan.parent.mkdir(parents=True)
        for tool in ('Write', 'Edit'):
            self.assertEqual(self.hook('PreToolUse', permission_mode='plan', tool_name=tool,
                                       tool_input={'file_path': str(plan)}), {})
        self.assert_denied(self.hook('PreToolUse', permission_mode='plan', tool_name='Write',
                                    tool_input={'file_path': str(self.repo / 'a.py')}), 'distribution')
        plan.symlink_to(self.repo / 'README.md')
        self.assert_denied(self.hook('PreToolUse', permission_mode='plan', tool_name='Write',
                                    tool_input={'file_path': str(plan)}), 'distribution')
        self.assertEqual(self.hook('Stop', permission_mode='plan'), {})
        self.assertEqual(coordination.latest_task(self.repo, 'claude-session')['status'], 'planning')

    def test_plan_mode_respects_local_plans_directory_override(self):
        self.start()
        config_dir = self.repo / '.claude'
        config_dir.mkdir()
        self.claude.mkdir()
        self.settings.write_text(json.dumps({'plansDirectory': '~/personal-plans'}))
        (config_dir / 'settings.json').write_text(json.dumps({'plansDirectory': './shared-plans'}))
        (config_dir / 'settings.local.json').write_text(json.dumps({'plansDirectory': './local-plans'}))
        self.assertEqual(self.hook('PreToolUse', permission_mode='plan', tool_name='Write',
                                   tool_input={'file_path': str(self.repo / 'local-plans' / 'feature.md')}), {})
        self.assert_denied(self.hook('PreToolUse', permission_mode='plan', tool_name='Write',
                                    tool_input={'file_path': str(self.repo / 'shared-plans' / 'feature.md')}), 'distribution')

    def test_unplanned_scope_and_pending_worker_are_blocked(self):
        task = self.start()
        self.plan(task, executor='auto')
        self.assert_denied(self.hook('PreToolUse', tool_name='Edit',
                                    tool_input={'file_path': str(self.repo / 'a.py')}), 'pending worker')
        self.assert_denied(self.hook('PreToolUse', tool_name='Write',
                                    tool_input={'file_path': str(self.repo / 'other.py')}), 'unplanned')

    def test_stop_checks_pending_and_undisposed_results_without_looping(self):
        task = self.start()
        planned = self.plan(task, executor='auto')
        aid = planned['assignments'][0]['id']
        blocked = self.hook('Stop', stop_hook_active=False)
        self.assertEqual(blocked['decision'], 'block')
        self.assertIn('pending', blocked['reason'])
        retry = self.hook('Stop', stop_hook_active=True)
        self.assertNotEqual(retry.get('decision'), 'block')
        self.assertIn('pending', retry['systemMessage'])
        self.assertNotEqual(coordination.load_task(self.repo, task['id'])['status'], 'completed')
        coordination.assignment_started(self.repo, task['id'], aid, '', 'claude', [])
        coordination.assignment_finished(self.repo, task['id'], aid, 'succeeded', 'Review complete', [], [])
        self.assertIn('disposition', self.hook('Stop')['reason'])
        coordination.use_result(self.repo, task['id'], aid, 'incorporated', 'Reviewed findings')
        self.assertNotEqual(self.hook('Stop', stop_hook_active=True).get('decision'), 'block')
        self.assertEqual(coordination.load_task(self.repo, task['id'])['status'], 'completed')

    def test_new_prompt_after_completion_gets_fresh_task_without_turn_id(self):
        task = self.start()
        self.plan(task)
        coordination.observe_coordinator_result(
            self.repo, task['id'], 'change', 'accepted', 'Value checked')
        self.hook('Stop')
        self.hook('UserPromptSubmit', prompt='Implement the feature')
        following = coordination.latest_task(self.repo, 'claude-session')
        self.assertNotEqual(task['id'], following['id'])
        self.assertEqual(following['status'], 'planning')
        self.hook('UserPromptSubmit', prompt='Also check one thing')
        self.assertEqual(coordination.latest_task(self.repo, 'claude-session')['id'], following['id'])

    def test_native_subagents_do_not_create_or_complete_coordinator_tasks(self):
        task = self.start()
        for event in ('SessionStart', 'UserPromptSubmit', 'PreToolUse', 'Stop', 'SubagentStop'):
            self.assertEqual(self.hook(event, agent_id='native-agent', tool_name='Write',
                                       tool_input={'file_path': 'a.py'}, prompt='subtask'), {})
        self.assertEqual(coordination.latest_task(self.repo, 'claude-session')['id'], task['id'])
        self.assertEqual(coordination.load_task(self.repo, task['id'])['status'], 'planning')

    def test_saved_and_environment_switches_disable_gates_and_keep_ledger(self):
        task = self.start()
        for switch in ('saved', 'environment'):
            if switch == 'saved':
                activation.set_enabled(self.repo, False)
            else:
                activation.set_enabled(self.repo, True)
                os.environ['DEEPSEEK_TEAM_DISABLED'] = '1'
            for event in ('SessionStart', 'UserPromptSubmit'):
                self.assertIn('disabled', self.hook(event)['hookSpecificOutput']['additionalContext'].lower())
            self.assertEqual(self.hook('PreToolUse', tool_name='Write', tool_input={'file_path': 'a.py'}), {})
            self.assertEqual(self.hook('Stop'), {})
            self.assertEqual(coordination.load_task(self.repo, task['id'])['status'], 'planning')
        os.environ['DEEPSEEK_TEAM_DISABLED'] = ''
        self.assert_denied(self.hook('PreToolUse', tool_name='Write', tool_input={'file_path': 'a.py'}), 'distribution')


if __name__ == '__main__':
    unittest.main()
