"""Generated delegation-guidance contract for the coordinator policy surfaces.

These checks assert the semantic markers that must survive on every rendered
profile and on the packaged guidance. They deliberately avoid full-paragraph
snapshots so wording can be tightened without rewriting brittle fixtures.
"""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from codex_deepseek_team import project, settings


# Fragments every Auto (adaptive) instruction surface must carry.
EARLY_ORIENTATION = (
    'Orient before you solve',
    'bounded diagnostic',
    'instead of claiming a safe implementation',
    'Batch every independent, eligible scope',
    'SUBSTANTIAL Auto plan',
    'executor: "auto"',
    'is rejected there',
    'manual 25/50/75 profile',
    'native exception keep their existing behavior',
    'Never promise a contribution percentage',
    'never widen access automatically',
)

PROTECTED_SCOPES = (
    'Protected scopes describe read or review context',
    'no longer grant generic',
    'decision_artifacts',
    'integration_of',
    'write_scope',
    'accepted coordinator edits recorded in the ledger',
    'read-only review never confers integration',
    'substantial Auto plans use executor: "auto"',
    'Corrections within reviewed worker output remain coordinator rework',
)

HOOK_RECOGNITION = (
    'first recognized source inspection',
    'early-planning reminder',
    'before any further supported source read',
    'status/bootstrap checks',
    'purely conversational turns stay exempt',
    'tool recognition is bounded',
)

REPORTING_CLARITY = (
    'subjective estimate, not measured',
    'Describe the accepted scope and any specific rework first',
    'not a measured',
    'routing feedback',
    'only summarizes accepted work',
)


class GuidanceCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='dst-guidance-')
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
            'HOME': str(self.root / 'home'),
            'XDG_CONFIG_HOME': str(self.root / 'config'),
            'DEEPSEEK_TEAM_STATE_DIR': str(self.root / 'state'),
        })
        environment.start()
        self.addCleanup(environment.stop)

    def assert_markers(self, text, markers, label):
        for marker in markers:
            with self.subTest(surface=label, marker=marker):
                self.assertIn(marker, text)


class InstructionGuidanceTests(GuidanceCase):
    def test_auto_full_access_guidance_registers_bounded_slices_and_auto_executor(self):
        policy = settings.resolve(self.repo, delegation_level='auto', access='full-access')
        for runtime in ('codex', 'claude'):
            text = settings.instructions(policy, runtime)
            self.assert_markers(text, EARLY_ORIENTATION, f'auto/full-access/{runtime}')
            self.assert_markers(text, PROTECTED_SCOPES, f'auto/full-access/{runtime}')
            self.assert_markers(text, HOOK_RECOGNITION, f'auto/full-access/{runtime}')

    def test_auto_read_only_guidance_keeps_the_contract_without_a_percent_symbol(self):
        policy = settings.resolve(self.repo, delegation_level='auto', access='auto')
        for runtime in ('codex', 'claude'):
            text = settings.instructions(policy, runtime)
            self.assert_markers(text, EARLY_ORIENTATION, f'auto/read-only/{runtime}')
            self.assert_markers(text, PROTECTED_SCOPES, f'auto/read-only/{runtime}')
            self.assertIn('Read-only auto does not delegate writing tasks', text)
            self.assertNotIn('%', text)

    def test_manual_profile_preserves_explicit_worker_and_feedback_behavior(self):
        policy = settings.resolve(self.repo, delegation_level=75, access='full-access')
        text = settings.instructions(policy, 'codex')
        self.assertIn('Effective delegation profile: 75% / full-access', text)
        self.assertIn('collect feedback', text)
        self.assertIn('Explicit executor choices', text)

    def test_hook_recognition_guidance_is_bounded_for_both_runtimes(self):
        policy = settings.resolve(self.repo, delegation_level='auto', access='full-access')
        for runtime in ('codex', 'claude'):
            text = settings.instructions(policy, runtime)
            self.assert_markers(text, HOOK_RECOGNITION, f'hooks/{runtime}')
            self.assertIn('never universal', text)

    def test_reporting_clarifies_the_split_is_not_a_measured_rate(self):
        policy = settings.resolve(self.repo, delegation_level='auto', access='full-access')
        for runtime in ('codex', 'claude'):
            self.assert_markers(settings.instructions(policy, runtime),
                                REPORTING_CLARITY, f'reporting/{runtime}')
            self.assertIn('ban on percentages calculated from counts',
                          settings.instructions(policy, runtime))

    def test_shared_reporting_helper_remains_the_single_source(self):
        policy = settings.resolve(self.repo, delegation_level='auto', access='full-access')
        shared = settings.final_reporting_guidance()
        self.assert_markers(shared, REPORTING_CLARITY, 'shared-helper')
        for runtime in ('codex', 'claude'):
            self.assertIn(shared.strip(), settings.instructions(policy, runtime))


# Contiguous fragments that survive the packaged file's own line wrapping.
PACKAGED_PLANNING = (
    'Orient before you solve',
    'bounded diagnostic deliverable',
    'Batch every independent, eligible scope',
    'SUBSTANTIAL Auto plan',
    'is rejected there',
    'manual 25/50/75 profile',
    'Never promise a contribution percentage',
    'widen access automatically',
)


class PackagedGuidanceTests(GuidanceCase):
    def test_packaged_guidance_carries_the_planning_and_scope_contract(self):
        body = project.DATA_FILE.read_text(encoding='utf-8')
        self.assert_markers(body, PACKAGED_PLANNING, 'packaged')
        self.assertIn('`executor: "auto"`', body)
        for marker in ('early-planning reminder', 'status/bootstrap',
                       'tool recognition is bounded',
                       'measured delegation'):
            with self.subTest(marker=marker):
                self.assertIn(marker, body)
        for marker in ('integration_of', 'write_scope', 'decision_artifacts',
                       'read-only review never confers integration'):
            with self.subTest(marker=marker):
                self.assertIn(marker, body)
        # The compact example must show a real implementation plus its integration step.
        self.assertIn('"executor": "auto"', body)
        self.assertIn('"integration_of": ["implementation"]', body)
        self.assertIn('"write_scope"', body)


if __name__ == '__main__':
    unittest.main()
