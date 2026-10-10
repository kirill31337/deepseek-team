"""Hook-level project context: real Bash effects and shell target candidates.

A leading ``cd`` may only be applied to a literal write when the hook can prove
the real working directory: one plain literal absolute directory without ``..``,
followed by ``&&`` and an AND-only guarded body.  Every regression below runs
the real command in a disposable temp repository first, so Bash semantics - not
only the classifier string - decide the expected effect, then requires the hook
to deny the unsafe forms and allow the one provable form.  ContextCase (and its
temp-local HOME/state fixtures) is shared with tests/test_coordinator_context.py.
"""
import os
import subprocess
import unittest

try:
    from tests.test_coordinator_context import ContextCase
except ImportError:  # unittest discovery executes modules from inside tests/
    from test_coordinator_context import ContextCase

from codex_deepseek_team import coordination


def _run_bash(command, cwd, *, env=None):
    """Run one command against a disposable temp repo; never a host path."""
    environment = dict(os.environ)
    for name in ('CDPATH', 'BASH_ENV', 'ENV'):
        environment.pop(name, None)
    if env:
        environment.update(env)
    return subprocess.run(['bash', '-c', command], cwd=str(cwd), env=environment,
                          capture_output=True, text=True, timeout=30)


class ScopedShellClassifierTests(unittest.TestCase):
    def test_only_a_proved_absolute_and_guarded_cd_prefix_is_resolved(self):
        from codex_deepseek_team.shell_mutation import classify_shell_mutation_scoped
        cases = (
            ('cd /work/sub && printf x > a.py', (True, ['/work/sub/a.py'], False)),
            ('cd /work/sub && rm -f a.py b.py',
             (True, ['/work/sub/a.py', '/work/sub/b.py'], False)),
            ('cd /work/sub && printf x > /work/other/a.py',
             (True, ['/work/other/a.py'], False)),
            ('cd /work/sub && printf x > a.py > b.py',
             (True, ['/work/sub/a.py', '/work/sub/b.py'], False)),
            ('cd /work/sub && touch a.py && touch b.py',
             (True, ['/work/sub/a.py', '/work/sub/b.py'], False)),
            ('cd /work/sub', (False, [], False)),
            ('printf x > a.py', (True, ['a.py'], False)),
        )
        for command, expected in cases:
            with self.subTest(command=command):
                self.assertEqual(classify_shell_mutation_scoped(command), expected)

    def test_relative_quoted_dynamic_option_or_parent_traversal_cd_is_uncertain(self):
        from codex_deepseek_team.shell_mutation import classify_shell_mutation_scoped
        commands = (
            'cd sub && printf x > a.py',
            "cd 'sub dir' && printf x > a.py",
            "cd '/work/sub' && printf x > a.py",
            'cd "$D" && printf x > a.py',
            'cd -P /work/sub && printf x > a.py',
            'cd /work/sub/../other && printf x > a.py',
            'cd one && cd two && printf x > a.py',
        )
        for command in commands:
            with self.subTest(command=command):
                self.assertEqual(classify_shell_mutation_scoped(command),
                                 (True, [], True))

    def test_non_and_control_flow_after_a_cd_prefix_is_uncertain(self):
        from codex_deepseek_team.shell_mutation import classify_shell_mutation_scoped
        commands = (
            'cd /work/sub; printf x > a.py',
            'cd /work/sub\nprintf x > a.py',
            'cd /work/sub && printf x > a.py || touch b.py',
            'cd /work/sub && printf x > a.py; touch b.py',
            'cd /work/sub && printf x > a.py &',
            'cd /work/sub | tee out.txt',
            'cd /work/sub && ( printf x > a.py )',
            'cd /work/sub && printf x > a.py && cd /work/other',
            'printf x > a.py && cd /work/sub',
            'echo "$(cd /work/sub && touch nested.py)"',
        )
        for command in commands:
            with self.subTest(command=command):
                self.assertEqual(classify_shell_mutation_scoped(command),
                                 (True, [], True))

    def test_shell_wrappers_that_can_change_cwd_make_the_scope_uncertain(self):
        """Reproduced defect: `time`/`eval`/`source`/`.` hide a real cwd change."""
        from codex_deepseek_team.shell_mutation import classify_shell_mutation_scoped
        variants = (
            'cd /work/alpha && time cd /work/beta && printf x > docs/design.md',
            'cd /work/alpha && time -p cd /work/beta && printf x > docs/design.md',
            "cd /work/alpha && eval 'cd /work/beta' && printf x > docs/design.md",
            'cd /work/alpha && source /work/jump.sh && printf x > docs/design.md',
            'cd /work/alpha && . /work/jump.sh && printf x > docs/design.md',
            "cd /work/alpha && builtin eval 'cd /work/beta' && printf x > docs/design.md",
            "cd /work/alpha && command eval 'cd /work/beta' && printf x > docs/design.md",
            'cd /work/alpha && command cd /work/beta && printf x > docs/design.md',
            'cd /work/alpha && builtin cd /work/beta && printf x > docs/design.md',
            "cd /work/alpha && builtin -- eval 'cd /work/beta' && printf x > docs/design.md",
            'cd /work/alpha && builtin -- cd /work/beta && printf x > docs/design.md',
            'cd /work/alpha && STEP=1 time cd /work/beta && printf x > docs/design.md',
        )
        for command in variants:
            with self.subTest(command=command):
                self.assertEqual(classify_shell_mutation_scoped(command), (True, [], True))

    def test_legacy_classifier_stays_conservative_for_cd(self):
        from codex_deepseek_team.shell_mutation import classify_shell_mutation
        self.assertEqual(classify_shell_mutation('cd sub && touch a.py'), (True, []))
        self.assertEqual(classify_shell_mutation('cd "$D" && touch a.py'), (True, []))
        self.assertEqual(classify_shell_mutation('cd sub'), (False, []))
        # The legacy entry point keeps its historical output for shell wrappers
        # this bounded lexer has never executed; the scoped entry point is the
        # one that must treat them as cwd-uncertain.
        self.assertEqual(classify_shell_mutation('time cd sub && printf x > a.py'),
                         (True, ['a.py']))
        self.assertEqual(classify_shell_mutation("eval 'cd sub' && printf x > a.py"),
                         (True, ['a.py']))


class HookCdScopeTests(ContextCase):
    def _plan_scope(self, scope, session):
        self.hook('UserPromptSubmit', session_id=session, prompt='Implement feature')
        self.plan([self.deliverable('work', scope)], session=session)

    def _assert_denied_uncertain(self, session, command, reason='working directory'):
        denied = self.hook('PreToolUse', session_id=session, tool_name='exec_command',
                           tool_input={'cmd': command, 'workdir': str(self.alpha)})
        self.assertEqual(self.decision(denied), 'deny', denied)
        self.assertIn(reason, self.reason(denied).lower())
        task = coordination.latest_task(self.alpha, session)
        self.assertEqual([event for event in task.get('coordinator_events', [])
                          if event.get('kind') == 'mutation_requested'], [])

    def test_failed_relative_cd_with_semicolon_write_is_denied(self):
        """Reproduced defect: `cd missing;` fails, then the write hits alpha/a.py."""
        self.bind(self.alpha)
        self._plan_scope(['missing/a.py'], 'cd-semicolon')
        command = 'cd missing; printf BYPASS > a.py'
        run = _run_bash(command, self.alpha)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual((self.alpha / 'a.py').read_text(), 'BYPASS')
        self._assert_denied_uncertain('cd-semicolon', command)

    def test_failed_relative_cd_with_or_escape_is_denied(self):
        """Reproduced defect: the `||` body runs in the original workdir."""
        self.bind(self.alpha)
        self._plan_scope(['missing/a.py'], 'cd-or-escape')
        command = 'cd missing && true || printf BYPASS > a.py'
        run = _run_bash(command, self.alpha)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual((self.alpha / 'a.py').read_text(), 'BYPASS')
        self._assert_denied_uncertain('cd-or-escape', command)

    def test_relative_cd_with_inherited_cdpath_is_denied(self):
        """Reproduced defect: CDPATH makes `cd sub` write into beta/sub/a.py."""
        (self.alpha / 'sub').mkdir()
        (self.beta / 'sub').mkdir()
        self.bind(self.alpha)
        self._plan_scope(['sub/a.py'], 'cd-cdpath')
        command = 'cd sub && printf BYPASS > a.py'
        run = _run_bash(command, self.alpha, env={'CDPATH': str(self.beta)})
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual((self.beta / 'sub' / 'a.py').read_text(), 'BYPASS')
        self.assertFalse((self.alpha / 'sub' / 'a.py').exists())
        self._assert_denied_uncertain('cd-cdpath', command)

    def test_absolute_cd_with_parent_traversal_is_denied(self):
        """Reproduced defect: logical `link/..` writes beta/a.py, not alpha/a.py."""
        (self.alpha / 'sub').mkdir()
        (self.beta / 'link').symlink_to(self.alpha / 'sub', target_is_directory=True)
        self.bind(self.alpha)
        self._plan_scope(['a.py'], 'cd-dotdot')
        command = f'cd {self.beta}/link/.. && printf BYPASS > a.py'
        run = _run_bash(command, self.alpha)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual((self.beta / 'a.py').read_text(), 'BYPASS')
        self.assertEqual((self.alpha / 'a.py').read_text(), 'VALUE = 1\n')
        self._assert_denied_uncertain('cd-dotdot', command)

    def test_proved_absolute_cd_prefix_is_allowed_and_writes_in_scope(self):
        (self.alpha / 'sub').mkdir()
        self.bind(self.alpha)
        self.hook('UserPromptSubmit', session_id='cd-ok', prompt='Implement feature')
        self.plan([self.deliverable('work', ['sub/a.py'])], session='cd-ok')
        command = f'cd {self.alpha}/sub && printf LIVE > a.py'
        run = _run_bash(command, self.alpha)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual((self.alpha / 'sub' / 'a.py').read_text(), 'LIVE')
        allowed = self.hook('PreToolUse', session_id='cd-ok', tool_name='exec_command',
                            tool_input={'cmd': command, 'workdir': str(self.alpha)})
        self.assertEqual(allowed, {}, allowed)
        task = coordination.latest_task(self.alpha, 'cd-ok')
        events = [event for event in task.get('coordinator_events', [])
                  if event.get('kind') == 'mutation_requested']
        self.assertTrue(events)
        self.assertIn('sub/a.py', events[-1].get('paths', []))

    def test_absolute_cd_with_non_and_control_flow_is_still_denied(self):
        (self.alpha / 'sub').mkdir()
        self.bind(self.alpha)
        self._plan_scope(['sub/a.py'], 'cd-strict')
        commands = (
            f'cd {self.alpha}/sub; printf BYPASS > a.py',
            f'cd {self.alpha}/sub\nprintf BYPASS > a.py',
            f'cd {self.alpha}/sub && printf BYPASS > a.py || touch b.py',
            f'cd {self.alpha}/sub && printf BYPASS > a.py &',
            f"cd '{self.alpha}/sub' && printf BYPASS > a.py",
            f'cd {self.alpha}/sub && printf BYPASS > a.py && cd {self.beta}',
            f'printf BYPASS > a.py && cd {self.alpha}/sub',
        )
        for command in commands:
            with self.subTest(command=command):
                self._assert_denied_uncertain('cd-strict', command)

    def _plan_protected_design(self, session):
        """Protected architecture plan authorizing only alpha/docs/design.md."""
        (self.alpha / 'docs').mkdir(exist_ok=True)
        (self.beta / 'docs').mkdir(exist_ok=True)
        (self.alpha / 'docs' / 'design.md').write_text('VALUE = 1\n')
        self.bind(self.alpha)
        self.hook('UserPromptSubmit', session_id=session, prompt='Implement feature')
        return self.plan([self.deliverable(
            'design', ['docs/design.md'], kind='architecture',
            decision_artifacts=['docs/design.md'])], session=session)

    def _wrapped_cd_variants(self):
        jump = self.root / 'jump.sh'
        jump.write_text(f'cd {self.beta}\n')
        return (
            f'cd {self.alpha} && time cd {self.beta} && printf BYPASS > docs/design.md',
            f'cd {self.alpha} && time -p cd {self.beta} && printf BYPASS > docs/design.md',
            f"cd {self.alpha} && eval 'cd {self.beta}' && printf BYPASS > docs/design.md",
            f'cd {self.alpha} && source {jump} && printf BYPASS > docs/design.md',
            f'cd {self.alpha} && . {jump} && printf BYPASS > docs/design.md',
            f"cd {self.alpha} && builtin eval 'cd {self.beta}' && printf BYPASS > docs/design.md",
            f"cd {self.alpha} && command eval 'cd {self.beta}' && printf BYPASS > docs/design.md",
        )

    def test_wrapped_cwd_changes_are_denied_without_ledger_authorization(self):
        """Reproduced defect: the wrappers hide a cd, yet alpha scope was authorized."""
        for index, command in enumerate(self._wrapped_cd_variants()):
            session = f'wrapped-cd-{index}'
            with self.subTest(command=command):
                self._plan_protected_design(session)
                (self.beta / 'docs' / 'design.md').unlink(missing_ok=True)
                (self.alpha / 'docs' / 'design.md').write_text('VALUE = 1\n')
                run = _run_bash(command, self.parent)
                self.assertEqual(run.returncode, 0, run.stderr)
                self.assertEqual((self.beta / 'docs' / 'design.md').read_text(), 'BYPASS')
                self.assertEqual((self.alpha / 'docs' / 'design.md').read_text(), 'VALUE = 1\n')
                denied = self.hook('PreToolUse', session_id=session, tool_name='exec_command',
                                   tool_input={'cmd': command, 'workdir': str(self.parent)})
                self.assertEqual(self.decision(denied), 'deny', denied)
                self.assertIn('working directory', self.reason(denied).lower())
                task = coordination.latest_task(self.alpha, session)
                self.assertEqual([event for event in task.get('coordinator_events', [])
                                  if event.get('kind') == 'mutation_requested'], [])

    def test_proved_guarded_writes_stay_allowed_next_to_wrapper_hazards(self):
        """The bounded wrapper rule must not deny provable or benign writes."""
        session = 'wrapped-cd-ok'
        self._plan_protected_design(session)
        commands = (
            f'cd {self.alpha} && printf LIVE > docs/design.md',
            f'cd {self.alpha} && time -p printf LIVE > docs/design.md',
            f'printf LIVE > {self.alpha}/docs/design.md',
        )
        for command in commands:
            with self.subTest(command=command):
                run = _run_bash(command, self.parent)
                self.assertEqual(run.returncode, 0, run.stderr)
                self.assertEqual((self.alpha / 'docs' / 'design.md').read_text(), 'LIVE')
                allowed = self.hook('PreToolUse', session_id=session, tool_name='exec_command',
                                    tool_input={'cmd': command, 'workdir': str(self.parent)})
                self.assertEqual(allowed, {}, allowed)
                task = coordination.latest_task(self.alpha, session)
                events = [event for event in task.get('coordinator_events', [])
                          if event.get('kind') == 'mutation_requested']
                self.assertTrue(events)
                self.assertIn('docs/design.md', events[-1].get('paths', []))

    def test_valid_absolute_cd_prefix_still_enforces_the_registered_scope(self):
        (self.alpha / 'sub').mkdir()
        self.bind(self.alpha)
        self._plan_scope(['sub/a.py'], 'cd-scope')
        out_of_scope = self.hook(
            'PreToolUse', session_id='cd-scope', tool_name='exec_command',
            tool_input={'cmd': f'cd {self.alpha}/sub && printf x > b.py',
                        'workdir': str(self.alpha)})
        self.assertEqual(self.decision(out_of_scope), 'deny', out_of_scope)
        external = self.hook(
            'PreToolUse', session_id='cd-scope', tool_name='exec_command',
            tool_input={'cmd': f'cd {self.alpha}/sub && printf x > {self.beta}/a.py',
                        'workdir': str(self.alpha)})
        self.assertEqual(self.decision(external), 'deny', external)
        self.assertIn('outside the attached project', self.reason(external).lower())


class EnvironmentOffInvalidBindingTests(ContextCase):
    def test_environment_off_with_invalid_binding_is_inert_and_creates_no_state(self):
        self.bind(self.parent)
        os.environ['DEEPSEEK_TEAM_DISABLED'] = '1'
        before = self._tree(self.state)
        patch = ('*** Begin Patch\n*** Update File: ' + str(self.alpha / 'a.py')
                 + '\n@@\n-VALUE = 1\n+VALUE = 2\n*** End Patch\n')
        for runtime in ('codex', 'claude'):
            with self.subTest(runtime=runtime):
                started = self.hook('SessionStart', runtime=runtime, source='startup')
                self.assertIn('disabled', self.context(started).lower())
                prompt = self.hook('UserPromptSubmit', runtime=runtime,
                                   prompt='Implement feature')
                self.assertIn('disabled', self.context(prompt).lower())
                for tool, data in (
                    ('exec_command', {'cmd': f'printf BYPASS > {self.alpha}/a.py'}),
                    ('exec_command', {'cmd': 'printf BYPASS > a.py',
                                      'workdir': str(self.alpha)}),
                    ('Write', {'file_path': str(self.alpha / 'a.py'), 'content': 'x'}),
                    ('apply_patch', {'command': patch}),
                    ('Read', {'file_path': str(self.alpha / 'a.py')}),
                ):
                    result = self.hook('PreToolUse', runtime=runtime, tool_name=tool,
                                       tool_input=data)
                    self.assertEqual(result, {}, result)
                plan_mode = self.hook('PreToolUse', runtime='claude', permission_mode='plan',
                                      tool_name='Write',
                                      tool_input={'file_path': str(self.alpha / 'plan.md'),
                                                  'content': '# plan\n'})
                self.assertEqual(plan_mode, {}, plan_mode)
                self.assertEqual(self.hook('Stop', runtime=runtime), {})
                self.assertEqual(self._tree(self.state), before)
        self.assertEqual((self.alpha / 'a.py').read_text(), 'VALUE = 1\n')


if __name__ == '__main__':
    unittest.main()
