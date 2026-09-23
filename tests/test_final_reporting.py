"""Enabled final-summary reporting contract for both coordinators.

The behaviour is exercised through the real render/attach/hook functions on
temporary repositories and state, not through copied implementation literals.
"""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from codex_deepseek_team import activation, coordinator_hooks, project, settings


# Short semantic markers every enabled surface must carry.
MARKERS = (
    'Final reporting',
    'subjective estimate, not measured',
    'coordinator/DeepSeek',
    'ACCEPTED WORK',
    'never reuse a configured',
    'nothing was delegated',
    '100/0',
    'native subagent',
)


class FinalReportingCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='dst-report-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo = self.root / 'repo'
        self.repo.mkdir()
        subprocess.run(['git', 'init', '-q', str(self.repo)], check=True)
        (self.repo / 'a.py').write_text('VALUE = 1\n')
        subprocess.run(['git', '-C', str(self.repo), 'add', '.'], check=True)
        subprocess.run(['git', '-C', str(self.repo), '-c', 'user.name=Test', '-c',
                        'user.email=test@example.test', 'commit', '-qm', 'base'], check=True)
        (self.root / 'home').mkdir()
        environment = mock.patch.dict(os.environ, {
            'DEEPSEEK_TEAM_STATE_DIR': str(self.root / 'state'),
            'XDG_CONFIG_HOME': str(self.root / 'config'),
            'HOME': str(self.root / 'home'),
        })
        environment.start()
        self.addCleanup(environment.stop)

    def assert_reporting_text(self, text):
        self.assertIsInstance(text, str)
        for marker in MARKERS:
            with self.subTest(marker=marker):
                self.assertIn(marker, text)
        # The contract distinguishes permitted subjective estimates from real metrics.
        self.assertIn('never claim measured', text)
        self.assertIn('ban on percentages calculated from counts', text)

    def hook(self, event, runtime='codex', **extra):
        payload = {'hook_event_name': event, 'cwd': str(self.repo),
                   'session_id': 'sess-1', 'turn_id': 'turn-1'}
        payload.update(extra)
        return coordinator_hooks.handle(payload, runtime=runtime)


class InstructionRenderTests(FinalReportingCase):
    def test_enabled_instructions_carry_reporting_for_every_profile_and_runtime(self):
        settings.set_values(self.repo / settings.PROJECT_FILE,
                            delegation_level=75, access='full-access')
        for runtime in ('codex', 'claude'):
            for level, access in (('auto', 'auto'), (25, 'read-only'), (75, 'full-access')):
                policy = settings.resolve(self.repo, delegation_level=level, access=access)
                self.assertTrue(policy.enabled)
                with self.subTest(runtime=runtime, level=level):
                    self.assert_reporting_text(settings.instructions(policy, runtime))

    def test_auto_instructions_avoid_a_percent_symbol(self):
        policy = settings.resolve(self.repo, delegation_level='auto', access='auto')
        for runtime in ('codex', 'claude'):
            self.assertNotIn('%', settings.instructions(policy, runtime))

    def test_shared_helper_is_the_single_source_of_the_text(self):
        policy = settings.resolve(self.repo, delegation_level='auto', access='auto')
        shared = settings.final_reporting_guidance()
        self.assertIn(shared.strip(), settings.instructions(policy, 'codex'))
        self.assertIn(shared.strip(), settings.instructions(policy, 'claude'))
        self.assertIn('subjective estimate, not measured', shared)

    def test_disabled_instructions_omit_the_report_requirement(self):
        activation.set_enabled(self.repo, False)
        policy = settings.resolve(self.repo, delegation_level='auto', access='auto')
        self.assertFalse(policy.enabled)
        for runtime in ('codex', 'claude'):
            text = settings.instructions(policy, runtime)
            self.assertIn('disabled', text.lower())
            for marker in MARKERS:
                with self.subTest(runtime=runtime, marker=marker):
                    self.assertNotIn(marker, text)


class AttachedBlockTests(FinalReportingCase):
    def test_attach_writes_reporting_into_both_coordinator_files(self):
        for runtime, filename in (('codex', 'AGENTS.md'), ('claude', 'CLAUDE.md')):
            self.assertTrue(project.attach(self.repo, coordinator=runtime))
            block = (self.repo / filename).read_text()
            with self.subTest(runtime=runtime):
                self.assert_reporting_text(block)


class HookPropagationTests(FinalReportingCase):
    def test_prompt_and_startup_propagate_reporting_for_both_runtimes(self):
        for runtime in ('codex', 'claude'):
            project.attach(self.repo, coordinator=runtime)
            prompt = self.hook('UserPromptSubmit', runtime=runtime, prompt='implement feature')
            self.assert_reporting_text(prompt['hookSpecificOutput']['additionalContext'])
            for source in ('startup', 'resume', 'compact'):
                start = self.hook('SessionStart', runtime=runtime, source=source)
                context = start['hookSpecificOutput']['additionalContext']
                with self.subTest(runtime=runtime, source=source):
                    self.assert_reporting_text(context)

    def test_claude_startup_does_not_duplicate_the_shared_text(self):
        project.attach(self.repo, coordinator='claude')
        context = self.hook('SessionStart', runtime='claude',
                            source='compact')['hookSpecificOutput']['additionalContext']
        self.assertEqual(context.count('subjective estimate, not measured'), 1)

    def test_disabled_hooks_do_not_require_the_report(self):
        for runtime in ('codex', 'claude'):
            project.attach(self.repo, coordinator=runtime)
            activation.set_enabled(self.repo, False)
            for event in ('SessionStart', 'UserPromptSubmit'):
                result = self.hook(event, runtime=runtime, source='compact', prompt='hi')
                context = result['hookSpecificOutput']['additionalContext']
                with self.subTest(runtime=runtime, event=event):
                    self.assertIn('disabled', context.lower())
                    self.assertNotIn('subjective estimate, not measured', context)
            activation.set_enabled(self.repo, True)

    def test_background_subagent_events_stay_inert(self):
        project.attach(self.repo, coordinator='claude')
        result = coordinator_hooks.handle({'hook_event_name': 'UserPromptSubmit',
                                           'cwd': str(self.repo), 'session_id': 's',
                                           'turn_id': 't', 'agent_id': 'sub-1',
                                           'prompt': 'x'}, runtime='claude')
        self.assertEqual(result, {})


if __name__ == '__main__':
    unittest.main()
