"""One-project coordinator context: explicit binding, real workdir scope, targets.

An optional DEEPSEEK_TEAM_PROJECT_ROOT binds a Codex/Claude session to exactly
one existing attached Git root (including a non-Git parent session), while every
scope decision still uses the real tool workdir/cwd instead of the selected
root.  The hook-level ``cd``/control-flow and real Bash-effect regressions live
in tests/test_hook_project_context.py, which imports ContextCase from here.
"""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from codex_deepseek_team import (activation, coordination, coordinator_hooks, project,
                                 settings)
from codex_deepseek_team.coordinator_context import SELECTION_VARIABLE


def _repository(path):
    path.mkdir(parents=True)
    subprocess.run(['git', 'init', '-q', str(path)], check=True)
    (path / 'a.py').write_text('VALUE = 1\n')
    subprocess.run(['git', '-C', str(path), 'add', '.'], check=True)
    subprocess.run(['git', '-C', str(path), '-c', 'user.name=Test', '-c',
                    'user.email=test@example.test', 'commit', '-qm', 'base'], check=True)


class ContextCase(unittest.TestCase):
    SESSION = 'session-1'

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='dst-context-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.home = self.root / 'home'
        self.codex_home = self.root / 'codex-home'
        self.claude_home = self.root / 'claude-home'
        self.config = self.root / 'config'
        self.state = self.root / 'state'
        for path in (self.home, self.codex_home, self.claude_home, self.config):
            path.mkdir()
        self._saved_environment = {name: os.environ.get(name) for name in (
            'HOME', 'CODEX_HOME', 'CLAUDE_CONFIG_DIR', 'XDG_CONFIG_HOME',
            'DEEPSEEK_TEAM_STATE_DIR', SELECTION_VARIABLE,
            activation.DISABLE_VARIABLE)}
        self.addCleanup(self._restore_environment)
        os.environ.update({
            'HOME': str(self.home),
            'CODEX_HOME': str(self.codex_home),
            'CLAUDE_CONFIG_DIR': str(self.claude_home),
            'XDG_CONFIG_HOME': str(self.config),
            'DEEPSEEK_TEAM_STATE_DIR': str(self.state),
        })
        os.environ.pop(SELECTION_VARIABLE, None)
        os.environ.pop(activation.DISABLE_VARIABLE, None)
        self.parent = self.root / 'parent'
        self.parent.mkdir()
        self.alpha = self.root / 'alpha'
        self.beta = self.root / 'beta'
        self.gamma = self.root / 'gamma'
        for repo in (self.alpha, self.beta, self.gamma):
            _repository(repo)
        self.canary = self.root / 'canary.txt'
        self.canary.write_text('outside canary\n')
        for repo in (self.alpha, self.beta, self.gamma):
            settings.set_values(repo / settings.PROJECT_FILE,
                                delegation_level=25, access='full-access')
        project.attach(self.alpha, coordinator='both')

    def _restore_environment(self):
        for name, value in self._saved_environment.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        self.assertEqual(self.canary.read_text(), 'outside canary\n')

    @staticmethod
    def _tree(path):
        if not path.exists():
            return ()
        return tuple(sorted(
            f'{item.relative_to(path)}:{item.stat().st_mtime_ns}'
            for item in path.rglob('*')))

    def bind(self, root):
        os.environ[SELECTION_VARIABLE] = str(root)

    def hook(self, event, runtime='codex', **extra):
        payload = {'session_id': self.SESSION, 'turn_id': 'turn-1',
                   'cwd': str(self.parent), 'hook_event_name': event}
        payload.update(extra)
        return coordinator_hooks.handle(payload, runtime=runtime)

    def submit(self, prompt='Implement feature', runtime='codex', **extra):
        return self.hook('UserPromptSubmit', runtime=runtime, prompt=prompt, **extra)

    def plan(self, deliverables, classification='small', session=None):
        task = coordination.latest_task(self.alpha, session or self.SESSION)
        body = {'classification': classification, 'deliverables': deliverables}
        if classification == 'small':
            body['small_evidence'] = 'One concrete deliverable with declared checks.'
        return coordination.plan_task(self.alpha, task['id'], body)

    @staticmethod
    def deliverable(identity, scope, **extra):
        item = {'id': identity, 'kind': 'implementation', 'scope': list(scope),
                'executor': 'coordinator', 'acceptance': ['checked'],
                'dependencies': [], 'checks': []}
        item.update(extra)
        return item

    @staticmethod
    def decision(result):
        return result.get('hookSpecificOutput', {}).get('permissionDecision')

    @staticmethod
    def reason(result):
        return result.get('hookSpecificOutput', {}).get('permissionDecisionReason', '')

    @staticmethod
    def context(result):
        return result.get('hookSpecificOutput', {}).get('additionalContext', '')


class EnvironmentBindingTests(ContextCase):
    def test_parent_binding_gets_alpha_lifecycle_task_gates_and_stop(self):
        for runtime in ('codex', 'claude'):
            with self.subTest(runtime=runtime):
                session = 'bound-' + runtime
                self.bind(self.alpha)
                started = self.hook('SessionStart', runtime=runtime, session_id=session,
                                    source='startup')
                self.assertIn('lifecycle enforcement is active',
                              self.context(started).lower())
                prompt = self.hook('UserPromptSubmit', runtime=runtime, session_id=session,
                                   prompt='Implement feature')
                task = coordination.latest_task(self.alpha, session)
                self.assertIsNotNone(task)
                self.assertIn('coordination plan --task', self.context(prompt))
                self.assertIn(task['id'], self.context(prompt))
                self.assertIn('25%/full-access', self.context(prompt))
                self.assertIn('effort=auto', self.context(prompt))
                planned = self.plan([self.deliverable('work', ['a.py'])], session=session)
                allowed = self.hook('PreToolUse', runtime=runtime, session_id=session,
                                    tool_name='exec_command',
                                    tool_input={'cmd': 'printf x > a.py',
                                                'workdir': str(self.alpha)})
                self.assertEqual(allowed, {}, allowed)
                denied = self.hook('PreToolUse', runtime=runtime, session_id=session,
                                   tool_name='exec_command',
                                   tool_input={'cmd': 'printf x > b.py',
                                               'workdir': str(self.alpha)})
                self.assertEqual(self.decision(denied), 'deny', denied)
                blocked = self.hook('Stop', runtime=runtime, session_id=session)
                self.assertEqual(blocked.get('decision'), 'block', blocked)
                coordination.observe_coordinator_result(self.alpha, planned['id'], 'work',
                                                        'accepted', 'checked')
                finished = self.hook('Stop', runtime=runtime, session_id=session)
                self.assertTrue(finished.get('continue', True), finished)
                self.assertEqual(coordination.latest_task(self.alpha, session)['status'],
                                 'completed')
                os.environ.pop(SELECTION_VARIABLE, None)

    def test_bound_subdirs_absolute_and_relative_paths_resolve_inside_alpha(self):
        (self.alpha / 'src').mkdir()
        self.bind(self.alpha)
        self.submit()
        self.plan([self.deliverable('work', ['src/a.py'])], classification='substantial')
        allowed = self.hook('PreToolUse', tool_name='exec_command',
                            tool_input={'cmd': 'printf x > a.py',
                                        'workdir': str(self.alpha / 'src')})
        self.assertEqual(allowed, {}, allowed)
        denied = self.hook('PreToolUse', tool_name='exec_command',
                           tool_input={'cmd': 'printf x > a.py',
                                       'workdir': str(self.alpha)})
        self.assertEqual(self.decision(denied), 'deny', denied)
        absolute = self.hook('PreToolUse', tool_name='exec_command',
                             tool_input={'cmd': 'printf x > ' + str(self.alpha / 'src/a.py'),
                                         'workdir': str(self.beta)})
        self.assertEqual(absolute, {}, absolute)

    def test_bound_alpha_never_authorizes_a_write_from_another_workdir(self):
        project.attach(self.beta, coordinator='both')
        self.bind(self.alpha)
        self.submit()
        planned = self.plan([self.deliverable('work', ['a.py'])])
        cases = (
            ('exec_command', {'cmd': 'printf x > a.py', 'workdir': str(self.beta)}),
            ('exec_command', {'cmd': 'printf x > a.py', 'cwd': str(self.beta)}),
            ('exec_command', {'cmd': 'printf x > a.py', 'workdir': str(self.parent)}),
            ('Write', {'file_path': str(self.beta / 'a.py'), 'content': 'x'}),
        )
        for runtime in ('codex', 'claude'):
            for tool, data in cases:
                with self.subTest(runtime=runtime, tool=tool, data=data):
                    denied = self.hook('PreToolUse', runtime=runtime, tool_name=tool,
                                       tool_input=data)
                    self.assertEqual(self.decision(denied), 'deny', denied)
                    self.assertIn('outside the attached project',
                                  self.reason(denied).lower())
        task = coordination.load_task(self.alpha, planned['id'])
        self.assertEqual([event for event in task.get('coordinator_events', [])
                          if event.get('kind') == 'mutation_requested'], [])

    def test_symlink_escape_from_the_real_workdir_is_rejected(self):
        outside = self.root / 'outside'
        outside.mkdir()
        (self.alpha / 'link').symlink_to(outside, target_is_directory=True)
        self.bind(self.alpha)
        self.submit()
        self.plan([self.deliverable('work', ['*'])], classification='substantial')
        denied = self.hook('PreToolUse', tool_name='exec_command',
                           tool_input={'cmd': 'printf x > link/a.py',
                                       'workdir': str(self.alpha)})
        self.assertEqual(self.decision(denied), 'deny', denied)
        self.assertIn('outside the attached project', self.reason(denied).lower())
        allowed = self.hook('PreToolUse', tool_name='exec_command',
                            tool_input={'cmd': 'printf x > a.py', 'workdir': str(self.alpha)})
        self.assertEqual(allowed, {}, allowed)

    def test_binding_to_an_attached_off_project_stays_inert(self):
        project.attach(self.gamma, coordinator='both')
        activation.set_enabled(self.gamma, False)
        self.bind(self.gamma)
        before = self._tree(self.state)
        text = self.context(self.submit())
        self.assertIn('disabled', text.lower())
        self.assertEqual(self.hook('PreToolUse', tool_name='Write',
                                   tool_input={'file_path': str(self.gamma / 'a.py'),
                                               'content': 'x'}), {})
        self.assertEqual(self.hook('Stop'), {})
        self.assertEqual(self._tree(self.state), before)


class InvalidBindingTests(ContextCase):
    def test_invalid_bindings_diagnose_and_never_fall_back(self):
        nongit = self.root / 'nongit'
        nongit.mkdir()
        (self.alpha / 'sub').mkdir()
        cases = (
            ('relative', 'alpha', 'absolute'),
            ('missing', str(self.root / 'missing'), 'existing'),
            ('not-a-repository', str(nongit), 'git'),
            ('subdirectory', str(self.alpha / 'sub'), 'root'),
            ('unattached', str(self.beta), 'attached'),
        )
        for name, value, fragment in cases:
            for runtime in ('codex', 'claude'):
                with self.subTest(case=name, runtime=runtime):
                    os.environ[SELECTION_VARIABLE] = value
                    before = self._tree(self.state)
                    prompt = self.hook('UserPromptSubmit', runtime=runtime,
                                       cwd=str(self.alpha), prompt='Implement feature')
                    text = self.context(prompt)
                    self.assertIn(SELECTION_VARIABLE, text)
                    self.assertIn(fragment, text.lower())
                    self.assertNotIn('coordination plan --task', text)
                    denied = self.hook('PreToolUse', runtime=runtime, cwd=str(self.alpha),
                                       tool_name='Write',
                                       tool_input={'file_path': str(self.alpha / 'a.py'),
                                                   'content': 'x'})
                    self.assertEqual(self.decision(denied), 'deny', denied)
                    self.assertIn(SELECTION_VARIABLE, self.reason(denied))
                    read = self.hook('PreToolUse', runtime=runtime, cwd=str(self.alpha),
                                     tool_name='Read',
                                     tool_input={'file_path': str(self.alpha / 'a.py')})
                    self.assertNotEqual(self.decision(read), 'deny', read)
                    self.assertIn(SELECTION_VARIABLE, self.context(read))
                    self.assertEqual(self._tree(self.state), before)

    def test_malformed_managed_block_is_diagnosed_without_adoption(self):
        agents = self.alpha / project.TARGETS['codex']
        agents.write_text(agents.read_text().replace(
            '<!-- codex-deepseek-team:original:created -->',
            '<!-- codex-deepseek-team:original:tampered -->'))
        self.bind(self.alpha)
        before = self._tree(self.state)
        text = self.context(self.submit())
        self.assertIn('malformed', text.lower())
        self.assertNotIn('coordination plan --task', text)
        self.assertEqual(self._tree(self.state), before)
        os.environ.pop(SELECTION_VARIABLE, None)
        target = self.hook('PreToolUse', tool_name='Write',
                           tool_input={'file_path': str(self.alpha / 'a.py'),
                                       'content': 'x'})
        self.assertEqual(self.decision(target), 'deny', target)
        self.assertIn('malformed', self.reason(target).lower())
        self.assertEqual(self._tree(self.state), before)


class UnboundParentTargetTests(ContextCase):
    def test_parent_targeting_an_attached_enabled_repo_explains_selection(self):
        before = self._tree(self.state)
        (self.parent / 'alpha-link').symlink_to(self.alpha, target_is_directory=True)
        cases = (
            ('Write', {'file_path': str(self.alpha / 'a.py'), 'content': 'x'}),
            ('Write', {'file_path': str(self.alpha / 'src' / 'a.py'), 'content': 'x'}),
            ('NotebookEdit', {'notebook_path': str(self.alpha / 'nb.ipynb')}),
            ('Glob', {'pattern': '**/*.py', 'path': str(self.alpha)}),
            ('exec_command', {'cmd': 'printf x > a.py', 'workdir': str(self.alpha)}),
            ('exec_command', {'cmd': 'printf x > a.py', 'cwd': str(self.alpha)}),
            ('Write', {'file_path': str(self.parent / 'alpha-link' / 'a.py'), 'content': 'x'}),
        )
        for runtime in ('codex', 'claude'):
            for tool, data in cases:
                with self.subTest(runtime=runtime, tool=tool, data=data):
                    result = self.hook('PreToolUse', runtime=runtime, tool_name=tool,
                                       tool_input=data)
                    self.assertEqual(self.decision(result), 'deny', result)
                    reason = self.reason(result)
                    self.assertIn(SELECTION_VARIABLE, reason)
                    self.assertIn('start', reason.lower())
        self.assertEqual(self._tree(self.state), before)

    def test_parent_targeting_unattached_or_off_repos_stays_inert_without_state(self):
        project.attach(self.gamma, coordinator='both')
        activation.set_enabled(self.gamma, False)
        before = self._tree(self.state)
        cases = (
            ('Write', {'file_path': str(self.beta / 'a.py'), 'content': 'x'}),
            ('exec_command', {'cmd': 'printf x > a.py', 'workdir': str(self.beta)}),
            ('Write', {'file_path': str(self.gamma / 'a.py'), 'content': 'x'}),
            ('exec_command', {'cmd': 'printf x > a.py', 'workdir': str(self.gamma)}),
        )
        for runtime in ('codex', 'claude'):
            for tool, data in cases:
                with self.subTest(runtime=runtime, tool=tool, data=data):
                    self.assertEqual(
                        self.hook('PreToolUse', runtime=runtime, tool_name=tool,
                                  tool_input=data), {})
                    self.assertEqual(self.hook('SessionStart', runtime=runtime), {})
        self.assertEqual(self._tree(self.state), before)

    def test_parent_literal_shell_and_patch_targets_are_explicit_candidates(self):
        """A cmd-only redirection or apply_patch body names a real target too."""
        before = self._tree(self.state)
        patch = ('*** Begin Patch\n*** Update File: ' + str(self.alpha / 'a.py')
                 + '\n@@\n-VALUE = 1\n+VALUE = 2\n*** End Patch\n')
        cases = (
            # No workdir/path key: the literal redirection destination is the target.
            ('exec_command', {'cmd': f'printf x > {self.alpha}/a.py'}),
            ('Bash', {'command': f'printf x > {self.alpha}/src/a.py'}),
            # A proved absolute cd resolves the following relative write.
            ('exec_command', {'cmd': f'cd {self.alpha}/src && printf x > a.py'}),
            ('apply_patch', {'command': patch}),
            ('apply_patch', patch),
        )
        for runtime in ('codex', 'claude'):
            for index, (tool, data) in enumerate(cases):
                with self.subTest(runtime=runtime, index=index, tool=tool):
                    result = self.hook('PreToolUse', runtime=runtime, tool_name=tool,
                                       tool_input=data)
                    self.assertEqual(self.decision(result), 'deny', result)
                    self.assertIn(SELECTION_VARIABLE, self.reason(result))
        self.assertEqual(self._tree(self.state), before)

    def test_parent_literal_targets_in_unattached_or_off_repos_stay_inert(self):
        project.attach(self.gamma, coordinator='both')
        activation.set_enabled(self.gamma, False)
        before = self._tree(self.state)
        patch = ('*** Begin Patch\n*** Update File: ' + str(self.beta / 'a.py')
                 + '\n@@\n-VALUE = 1\n+VALUE = 2\n*** End Patch\n')
        cases = (
            ('exec_command', {'cmd': f'printf x > {self.beta}/a.py'}),
            ('exec_command', {'cmd': f'printf x > {self.gamma}/a.py'}),
            ('exec_command', {'cmd': 'printf x > outside-not-a-project.txt'}),
            ('exec_command', {'cmd': 'printf x > ../outside-not-a-project.txt'}),
            ('apply_patch', {'command': patch}),
        )
        for runtime in ('codex', 'claude'):
            for index, (tool, data) in enumerate(cases):
                with self.subTest(runtime=runtime, index=index, tool=tool):
                    self.assertEqual(
                        self.hook('PreToolUse', runtime=runtime, tool_name=tool,
                                  tool_input=data), {})
        self.assertEqual(self._tree(self.state), before)


class PreservedBehaviorTests(ContextCase):
    def test_native_dispatch_gate_survives_the_binding(self):
        self.bind(self.alpha)
        self.submit()
        denied = self.hook('PreToolUse', tool_name='spawn_agent',
                           tool_input={'prompt': 'do the work'})
        self.assertEqual(self.decision(denied), 'deny', denied)
        self.assertIn('executor:auto', self.reason(denied).lower())

    def test_claude_plan_file_edits_stay_exempt_inside_a_bound_session(self):
        self.bind(self.alpha)
        self.submit(runtime='claude')
        plans = Path(os.environ['CLAUDE_CONFIG_DIR']) / 'plans'
        plans.mkdir(parents=True, exist_ok=True)
        target = plans / 'plan.md'
        target.write_text('# plan\n')
        allowed = self.hook('PreToolUse', runtime='claude', tool_name='Edit',
                            permission_mode='plan', tool_input={'file_path': str(target)})
        self.assertEqual(allowed, {}, allowed)

    def test_protected_scope_rules_survive_the_binding(self):
        self.bind(self.alpha)
        self.submit(prompt='Review the architecture')
        task = coordination.latest_task(self.alpha, self.SESSION)
        coordination.plan_task(self.alpha, task['id'], {
            'classification': 'substantial',
            'deliverables': [{
                'id': 'arch', 'kind': 'architecture', 'scope': ['docs'],
                'executor': 'coordinator', 'decision_artifacts': ['docs/design.md'],
                'acceptance': ['decision recorded'], 'dependencies': [], 'checks': []}],
        })
        (self.alpha / 'docs').mkdir()
        denied = self.hook('PreToolUse', tool_name='Write',
                           tool_input={'file_path': str(self.alpha / 'docs/other.md'),
                                       'content': 'x'})
        self.assertEqual(self.decision(denied), 'deny', denied)
        self.assertIn('protected-scope gate', self.reason(denied).lower())
        allowed = self.hook('PreToolUse', tool_name='Write',
                            tool_input={'file_path': str(self.alpha / 'docs/design.md'),
                                        'content': 'x'})
        self.assertEqual(allowed, {}, allowed)

    def test_storage_unavailable_keeps_local_continuation_under_the_binding(self):
        from codex_deepseek_team import state_storage
        self.bind(self.alpha)
        with mock.patch.object(state_storage, 'prepare_storage',
                               side_effect=state_storage.StorageError('storage offline')):
            result = self.submit()
        self.assertIn('storage is unavailable', self.context(result).lower())

    def test_task_identity_stays_per_session_under_the_binding(self):
        self.bind(self.alpha)
        self.hook('UserPromptSubmit', session_id='one', prompt='First task')
        self.hook('UserPromptSubmit', session_id='two', prompt='Second task')
        first = coordination.latest_task(self.alpha, 'one')
        second = coordination.latest_task(self.alpha, 'two')
        self.assertNotEqual(first['id'], second['id'])
        self.assertFalse((self.parent / 'AGENTS.md').exists())
        self.assertFalse((self.parent / 'CLAUDE.md').exists())


if __name__ == '__main__':
    unittest.main()
