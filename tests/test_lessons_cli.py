"""Real CLI round trips and lifecycle guidance for delegation lessons.

The CLI tests launch ``python -m codex_deepseek_team`` as a subprocess against a
temporary Git project, HOME and private state, so stdin/file parsing, exit codes
and the "no writes from read commands" contract are exercised for real.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from codex_deepseek_team import (activation, coordination, coordinator_hooks,
                                 lessons, project, settings)

SOURCE_ROOT = Path(__file__).resolve().parents[1]
PYTHONPATH = str(SOURCE_ROOT / 'src')


class LessonsCliCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='dst-lessons-cli-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo = self.root / 'repo'
        self.repo.mkdir()
        subprocess.run(['git', 'init', '-q', str(self.repo)], check=True)
        (self.repo / 'a.py').write_text('VALUE = 1\n')
        subprocess.run(['git', '-C', str(self.repo), 'add', '.'], check=True)
        subprocess.run(['git', '-C', str(self.repo), '-c', 'user.name=Test', '-c',
                        'user.email=test@example.test', 'commit', '-qm', 'base'],
                       check=True)
        self.state = self.root / 'state'
        self.home = self.root / 'home'
        self.home.mkdir()
        self.config = self.root / 'config'
        self.environment = mock.patch.dict(os.environ, {
            'DEEPSEEK_TEAM_STATE_DIR': str(self.state),
            'HOME': str(self.home),
            'XDG_CONFIG_HOME': str(self.config),
            'DEEPSEEK_TEAM_DISABLED': '0',
        })
        self.environment.start()
        self.addCleanup(self.environment.stop)
        settings.set_values(self.repo / settings.PROJECT_FILE,
                            delegation_level=75, access='full-access')
        for runtime in ('codex', 'claude'):
            project.attach(self.repo, coordinator=runtime)

    # -- helpers ---------------------------------------------------------
    def cli(self, *arguments, input=''):
        env = dict(os.environ, PYTHONPATH=PYTHONPATH, HOME=str(self.home),
                   DEEPSEEK_TEAM_STATE_DIR=str(self.state),
                   XDG_CONFIG_HOME=str(self.config))
        env.pop('DEEPSEEK_TEAM_DISABLED', None)
        return subprocess.run([sys.executable, '-m', 'codex_deepseek_team', *arguments],
                              cwd=str(self.repo), env=env, input=input, text=True,
                              capture_output=True, timeout=90)

    def policy(self):
        return settings.Policy(75, 'full-access',
                               {'delegation_level': 'test', 'access': 'test'})

    def make_assignment(self, session='cli'):
        task = coordination.open_task(self.repo, session_id=session, turn_id='turn-1',
                                      prompt='implement feature', policy=self.policy())
        planned = coordination.plan_task(self.repo, task['id'], {
            'classification': 'substantial',
            'deliverables': [{'id': 'impl', 'kind': 'implementation', 'scope': ['a.py'],
                              'executor': 'worker', 'acceptance': ['implemented'],
                              'dependencies': [], 'checks': []}]})
        assignment_id = planned['assignments'][0]['id']
        coordination.assignment_started(self.repo, task['id'], assignment_id, 'ws-cli',
                                        'codex', [], effort='high')
        coordination.assignment_finished(self.repo, task['id'], assignment_id, 'succeeded',
                                         'reported done', [], [])
        return task['id'], assignment_id

    def seed_case(self):
        case = 'task-00000000000000000000/assignment-00000000'
        record = lessons.record_outcome(
            self.repo, task_id='task-00000000000000000000',
            assignment_id='assignment-00000000', disposition='needs-rework',
            evidence='reviewer found a gap',
            context={'features': {'kind': 'implementation', 'effort': 'high'}},
            rework={'cause': 'brief_gap', 'severity': 'minor',
                    'summary': 'the brief omitted the recorded check',
                    'prevention': 'state the acceptance check in the brief'})
        return {
            'expected_version': 0, 'through_event': record['seq'], 'summary': 'first review',
            'rules': [{'id': 'rule-guidance', 'when': {'kind': 'implementation'},
                       'condition': 'Check the recorded brief gap.',
                       'action': 'State the acceptance check in the brief.',
                       'evidence': [case]}]}

    def seed_rule(self):
        payload = self.seed_case()
        lessons.apply_review(self.repo, payload)
        return payload


class LessonsReadCommandTests(LessonsCliCase):
    def test_empty_state_read_commands_create_no_files(self):
        self.assertFalse(self.state.exists())
        for arguments in (['lessons', 'status'], ['lessons', 'journal'], ['lessons', 'rules'],
                          ['lessons', 'review']):
            with self.subTest(arguments=arguments):
                result = self.cli(*arguments, '--path', str(self.repo))
                self.assertEqual(result.returncode, 0, result.stderr)
        for arguments in (['lessons', 'status', '--json'], ['lessons', 'journal', '--json'],
                          ['lessons', 'rules', '--json'], ['lessons', 'review', '--json']):
            with self.subTest(arguments=arguments):
                result = self.cli(*arguments, '--path', str(self.repo))
                self.assertEqual(result.returncode, 0, result.stderr)
                json.loads(result.stdout)
        self.assertFalse(self.state.exists())

    def test_json_shapes_are_stable_and_human_output_is_useful(self):
        self.seed_rule()
        status = json.loads(self.cli('lessons', 'status', '--path', str(self.repo),
                                     '--json').stdout)
        self.assertEqual(sorted(status), sorted(['version', 'rules_file', 'review_due',
                                                 'due_reasons', 'cases_since_review',
                                                 'through_event', 'counts', 'cohorts',
                                                 'causes', 'severities']))
        journal = json.loads(self.cli('lessons', 'journal', '--path', str(self.repo),
                                      '--json').stdout)
        self.assertEqual(len(journal), 1)
        self.assertEqual(sorted(journal[0]), sorted(['seq', 'case_id', 'at', 'disposition',
                                                     'evidence', 'context', 'rework']))
        review = json.loads(self.cli('lessons', 'review', '--path', str(self.repo),
                                     '--json').stdout)
        self.assertEqual(review['expected_version'], 1)
        rules = json.loads(self.cli('lessons', 'rules', '--path', str(self.repo),
                                    '--json').stdout)
        self.assertEqual(rules['rule_ids'], ['rule-guidance'])
        human = self.cli('lessons', 'status', '--path', str(self.repo))
        self.assertIn('version', human.stdout.lower())
        self.assertIn('rule-guidance', self.cli('lessons', 'rules', '--path',
                                                str(self.repo)).stdout)


class LessonsReviewApplyTests(LessonsCliCase):
    def test_review_apply_from_file_then_stdin_and_watermark(self):
        payload = self.seed_case()
        review_file = self.root / 'review.json'
        review_file.write_text(json.dumps(payload))
        first = self.cli('lessons', 'review', '--path', str(self.repo),
                         '--apply', str(review_file), '--json')
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(json.loads(first.stdout)['version'], 1)
        no_change = {'expected_version': 1, 'through_event': payload['through_event'],
                     'summary': 'explicit no-change review', 'rules': payload['rules']}
        second = self.cli('lessons', 'review', '--path', str(self.repo), '--apply', '-',
                          '--json', input=json.dumps(no_change))
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(json.loads(second.stdout)['version'], 2)
        self.assertEqual(json.loads(second.stdout)['counts']['cases'], 1)
        after = lessons.status(self.repo)
        self.assertEqual(after['version'], 2)
        self.assertEqual(len(lessons.journal(self.repo)), 1)

    def test_invalid_review_payloads_do_not_mutate_state(self):
        payload = self.seed_case()
        review_file = self.root / 'review.json'
        review_file.write_text(json.dumps(payload))
        self.assertEqual(self.cli('lessons', 'review', '--path', str(self.repo),
                                  '--apply', str(review_file)).returncode, 0)
        head = self.cli('lessons', 'journal', '--path', str(self.repo), '--json').stdout
        bad_payloads = [
            {'expected_version': 0, 'through_event': payload['through_event'],
             'summary': 'stale', 'rules': payload['rules']},
            {'expected_version': 1, 'through_event': payload['through_event'],
             'summary': 'extra key', 'rules': payload['rules'], 'extra': True},
            {'expected_version': 1, 'through_event': payload['through_event'],
             'summary': 'unknown evidence',
             'rules': [dict(payload['rules'][0], evidence=['task-999/assignment-999'])]},
            {'expected_version': 1, 'through_event': 999, 'summary': 'bad watermark',
             'rules': payload['rules']},
        ]
        for bad in bad_payloads:
            with self.subTest(keys=sorted(bad)):
                result = self.cli('lessons', 'review', '--path', str(self.repo), '--apply', '-',
                                  input=json.dumps(bad))
                self.assertNotEqual(result.returncode, 0, result.stdout)
        malformed = self.cli('lessons', 'review', '--path', str(self.repo),
                             '--apply', '-', input='{not json')
        self.assertNotEqual(malformed.returncode, 0)
        missing = self.cli('lessons', 'review', '--path', str(self.repo), '--apply',
                           str(self.root / 'absent.json'))
        self.assertNotEqual(missing.returncode, 0)
        self.assertEqual(lessons.status(self.repo)['version'], 1)
        self.assertEqual(len(lessons.journal(self.repo)), 1)
        self.assertEqual(self.cli('lessons', 'journal', '--path', str(self.repo),
                                  '--json').stdout, head)


class UseResultReworkCliTests(LessonsCliCase):
    def test_rework_json_file_and_stdin_attach_structured_cause(self):
        task_id, assignment_id = self.make_assignment('rework-file')
        payload = {'cause': 'worker_error', 'severity': 'major',
                   'summary': 'the worker missed the recorded check',
                   'prevention': 'restate the check in the brief'}
        path = self.root / 'rework.json'
        path.write_text(json.dumps(payload))
        result = self.cli('coordination', 'use', '--path', str(self.repo),
                          '--task', task_id, '--assignment', assignment_id,
                          '--disposition', 'needs-rework', '--evidence', 'review found a gap',
                          '--rework-json', str(path))
        self.assertEqual(result.returncode, 0, result.stderr)
        events = lessons.journal(self.repo)
        self.assertEqual(events[-1]['rework']['cause'], 'worker_error')
        task_id, assignment_id = self.make_assignment('rework-stdin')
        stdin_payload = {'cause': 'context_gap', 'severity': 'minor',
                         'summary': 'a required input was missing',
                         'prevention': 'declare and import the input'}
        result = self.cli('coordination', 'use', '--path', str(self.repo),
                          '--task', task_id, '--assignment', assignment_id,
                          '--disposition', 'needs-rework', '--evidence',
                          'missing prepared input', '--rework-json', '-',
                          input=json.dumps(stdin_payload))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(lessons.journal(self.repo)[-1]['rework']['cause'], 'context_gap')
        self.assertNotIn(stdin_payload['summary'], result.stdout)

    def test_omitted_rework_stays_compatible_with_unknown_attribution(self):
        task_id, assignment_id = self.make_assignment('legacy-rework')
        result = self.cli('coordination', 'use', '--path', str(self.repo),
                          '--task', task_id, '--assignment', assignment_id,
                          '--disposition', 'needs-rework', '--evidence', 'older review')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIsNone(lessons.journal(self.repo)[-1]['rework'])
        self.assertEqual(lessons.status(self.repo)['counts']['unknown_attribution'], 1)
        task_id, assignment_id = self.make_assignment('null-rework')
        result = self.cli('coordination', 'use', '--path', str(self.repo),
                          '--task', task_id, '--assignment', assignment_id,
                          '--disposition', 'needs-rework', '--evidence', 'no attribution',
                          '--rework-json', '-', input='null')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIsNone(lessons.journal(self.repo)[-1]['rework'])
        task_id, assignment_id = self.make_assignment('clean-omission')
        result = self.cli('coordination', 'use', '--path', str(self.repo),
                          '--task', task_id, '--assignment', assignment_id,
                          '--disposition', 'incorporated', '--evidence', 'accepted output')
        self.assertEqual(result.returncode, 0, result.stderr)
        counts = lessons.status(self.repo)['counts']
        self.assertEqual(counts['clean'], 1)
        self.assertEqual(counts['rework'], 2)
        self.assertEqual(counts['unknown_attribution'], 2)

    def test_invalid_rework_inputs_change_nothing(self):
        task_id, assignment_id = self.make_assignment('invalid-rework-cli')
        bad_inputs = [
            json.dumps({'cause': 'nonsense', 'severity': 'major', 'summary': 's',
                        'prevention': 'p'}),
            json.dumps({'cause': 'worker_error', 'severity': 'major', 'summary': 's',
                        'prevention': 'p', 'extra': 'x'}),
            '{not json',
            json.dumps(['not', 'an', 'object']),
        ]
        for value in bad_inputs:
            with self.subTest(value=value):
                result = self.cli('coordination', 'use', '--path', str(self.repo),
                                  '--task', task_id, '--assignment', assignment_id,
                                  '--disposition', 'needs-rework', '--evidence', 'review',
                                  '--rework-json', '-', input=value)
                self.assertNotEqual(result.returncode, 0, result.stdout)
        row = coordination.load_task(self.repo, task_id)['assignments'][0]
        self.assertIsNone(row['disposition'])
        self.assertFalse((self.state / 'lessons').exists())


class PolicyAndLifecycleGuidanceTests(LessonsCliCase):
    def test_config_show_instructions_include_root_lessons_guidance_unless_disabled(self):
        self.seed_rule()
        result = self.cli('config', 'show', '--effective', '--path', str(self.repo),
                          '--instructions')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('rule-guidance', result.stdout)
        self.assertIn('Delegation lessons', result.stdout)
        activation.set_enabled(self.repo, False)
        disabled = self.cli('config', 'show', '--effective', '--path', str(self.repo),
                            '--instructions')
        self.assertEqual(disabled.returncode, 0, disabled.stderr)
        self.assertNotIn('rule-guidance', disabled.stdout)
        self.assertIn('disabled for this project', disabled.stdout)
        activation.set_enabled(self.repo, True)

    def test_runtime_guidance_stop_due_reminder_plan_mode_and_off(self):
        self.seed_rule()
        for runtime in ('codex', 'claude'):
            with self.subTest(runtime=runtime):
                prompt = coordinator_hooks.handle({
                    'session_id': runtime + '-guide', 'turn_id': 'turn-1',
                    'cwd': str(self.repo), 'hook_event_name': 'UserPromptSubmit',
                    'prompt': 'implement feature'}, runtime=runtime)
                self.assertIn('rule-guidance',
                              prompt['hookSpecificOutput']['additionalContext'])
                session = coordinator_hooks.handle({
                    'session_id': runtime + '-guide', 'cwd': str(self.repo),
                    'hook_event_name': 'SessionStart', 'source': 'startup'}, runtime=runtime)
                self.assertIn('rule-guidance',
                              session['hookSpecificOutput']['additionalContext'])
        for index in range(3):
            lessons.record_outcome(
                self.repo, task_id=f'task-{index:020d}', assignment_id=f'assignment-{index:08d}',
                disposition='needs-rework', evidence='repeated brief gap',
                context={'features': {'kind': 'implementation', 'effort': 'high'}},
                rework={'cause': 'brief_gap', 'severity': 'minor',
                        'summary': 'repeated brief gap', 'prevention': 'state the check'})
        self.assertTrue(lessons.status(self.repo)['review_due'])
        for runtime in ('codex', 'claude'):
            with self.subTest(runtime=runtime, event='Stop'):
                session_id = runtime + '-stop'
                coordinator_hooks.handle({
                    'session_id': session_id, 'turn_id': 'turn-1', 'cwd': str(self.repo),
                    'hook_event_name': 'UserPromptSubmit', 'prompt': 'one bounded review'},
                    runtime=runtime)
                task = coordination.latest_task(self.repo, session_id)
                coordination.plan_task(self.repo, task['id'], {
                    'classification': 'small',
                    'small_evidence': 'one bounded review with explicit acceptance',
                    'deliverables': [{'id': 'work', 'kind': 'review', 'scope': ['a.py'],
                                      'executor': 'coordinator', 'acceptance': ['reviewed'],
                                      'dependencies': [], 'checks': []}]})
                coordination.observe_coordinator_result(self.repo, task['id'], 'work',
                                                        'accepted', 'checked the current file')
                result = coordinator_hooks.handle({
                    'session_id': session_id, 'cwd': str(self.repo),
                    'hook_event_name': 'Stop'}, runtime=runtime)
                self.assertNotIn('decision', result)
                self.assertIn('review', result.get('systemMessage', '').lower())
                self.assertTrue(result.get('continue', True))
                repeated = coordinator_hooks.handle({
                    'session_id': session_id, 'cwd': str(self.repo),
                    'hook_event_name': 'Stop', 'stop_hook_active': True}, runtime=runtime)
                self.assertNotIn('decision', repeated)
        plan_session = 'claude-plan-mode'
        coordinator_hooks.handle({
            'session_id': plan_session, 'turn_id': 'turn-1', 'cwd': str(self.repo),
            'hook_event_name': 'UserPromptSubmit', 'prompt': 'plan the work'},
            runtime='claude')
        planned_stop = coordinator_hooks.handle({
            'session_id': plan_session, 'cwd': str(self.repo), 'hook_event_name': 'Stop',
            'permission_mode': 'plan'}, runtime='claude')
        self.assertEqual(planned_stop, {})
        activation.set_enabled(self.repo, False)
        off_prompt = coordinator_hooks.handle({
            'session_id': 'off-session', 'turn_id': 'turn-1', 'cwd': str(self.repo),
            'hook_event_name': 'UserPromptSubmit', 'prompt': 'implement feature'}, runtime='codex')
        self.assertNotIn('rule-guidance',
                         off_prompt['hookSpecificOutput']['additionalContext'])
        self.assertIn('disabled for this project',
                      off_prompt['hookSpecificOutput']['additionalContext'])
        self.assertEqual(coordinator_hooks.handle({
            'session_id': 'off-session', 'cwd': str(self.repo),
            'hook_event_name': 'Stop'}, runtime='codex'), {})
        activation.set_enabled(self.repo, True)


if __name__ == '__main__':
    unittest.main()
