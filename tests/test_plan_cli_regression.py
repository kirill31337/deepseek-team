"""Regression coverage for the public plan CLI and Claude Bash hook redirects.

The fixture keeps every runtime and configuration directory inside the
temporary tree, so planning never reads or writes a real user configuration.
"""
import copy
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from codex_deepseek_team import coordination, coordinator_hooks, project, settings


SOURCE_ROOT = Path(__file__).resolve().parents[1]
SOURCE_PATH = SOURCE_ROOT / 'src'


class PlanCliRegressionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='dst-plan-cli-')
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        self.repo = base / 'repo'
        self.repo.mkdir()
        self.addCleanup(os.chdir, os.getcwd())
        os.chdir(self.repo)
        self.home = base / 'home'
        self.codex_home = base / 'codex-home'
        self.claude_config = base / 'claude-config'
        self.xdg_config = base / 'xdg-config'
        self.xdg_state = base / 'xdg-state'
        self.state = base / 'state'
        for directory in (self.home, self.codex_home, self.claude_config,
                          self.xdg_config, self.xdg_state, self.state):
            directory.mkdir(mode=0o700)
        self.environment = {
            'HOME': str(self.home),
            'CODEX_HOME': str(self.codex_home),
            'CLAUDE_CONFIG_DIR': str(self.claude_config),
            'XDG_CONFIG_HOME': str(self.xdg_config),
            'XDG_STATE_HOME': str(self.xdg_state),
            'DEEPSEEK_TEAM_STATE_DIR': str(self.state),
        }
        patcher = mock.patch.dict(os.environ, self.environment)
        patcher.start()
        self.addCleanup(patcher.stop)

        subprocess.run(['git', 'init', '-q', str(self.repo)], check=True)
        for name in ('a.py', 'b.py', 'c.py'):
            (self.repo / name).write_text('VALUE = 1\n')
        subprocess.run(['git', '-C', str(self.repo), 'add', '.'], check=True)
        subprocess.run(
            ['git', '-C', str(self.repo), '-c', 'user.name=Fixture', '-c',
             'user.email=fixture@example.invalid', 'commit', '-qm', 'base'],
            check=True)
        settings.set_values(self.repo / settings.PROJECT_FILE,
                            delegation_level='auto', access='full-access')
        project.attach(self.repo, coordinator='both')

        self.env = dict(os.environ)
        existing_pythonpath = self.env.get('PYTHONPATH')
        self.env['PYTHONPATH'] = str(SOURCE_PATH) + (
            os.pathsep + existing_pythonpath if existing_pythonpath else '')

    def open_task(self, session='plan-cli', turn='1'):
        return coordination.open_task(
            self.repo, session_id=session, turn_id=turn, prompt='fixture plan',
            policy=settings.resolve(self.repo), runtime='codex')

    def features(self, kind='implementation'):
        return {
            'kind': kind,
            'domain': 'python',
            'operation': 'fix',
            'localization': 'known',
            'coupling': 'local',
            'verification': 'tests',
            'clarity': 'clear',
            'risk': 'low',
            'scope_size': 'small',
            'runtime': 'codex',
            'model': 'deepseek-flash',
            'effort': 'high',
            'context_version': 'project-v1',
        }

    def deliverable(self, identifier, path):
        return {
            'id': identifier,
            'kind': 'implementation',
            'executor': 'auto',
            'scope': [path],
            'acceptance': [f'{path} fixture accepted'],
            'dependencies': [],
            'checks': ['python3 -V'],
            'features': self.features(),
        }

    def plan_payload(self, *deliverables):
        return {'classification': 'substantial', 'deliverables': list(deliverables)}

    def cli(self, *arguments, input_text=None):
        return subprocess.run(
            [sys.executable, '-m', 'codex_deepseek_team', 'coordination',
             *arguments],
            cwd=self.repo, env=self.env, input=input_text, text=True,
            capture_output=True, timeout=30)

    def plan_cli(self, task_id, payload):
        return self.cli('plan', '--path', str(self.repo), '--task', task_id,
                        input_text=json.dumps(payload))

    def snapshot(self, task_id):
        path = coordination._task_path(self.repo, task_id)
        info = path.stat()
        return path.read_bytes(), info.st_mtime_ns, info.st_ino

    def initial_plan_with_failed_first(self, session='plan-cli'):
        task = self.open_task(session=session)
        first = self.deliverable('impl', 'a.py')
        second = self.deliverable('tests', 'b.py')
        planned = self.plan_cli(task['id'], self.plan_payload(first, second))
        self.assertEqual(planned.returncode, 0, planned.stderr)
        output = json.loads(planned.stdout)
        self.assertEqual(len(output['assignments']), 2)
        self.assertTrue(all(row['resolved_executor'] == 'worker'
                            for row in output['diagnostics']))
        assignments = {row['deliverable_id']: row['id']
                       for row in output['assignments']}
        failed_id = assignments['impl']
        coordination.assignment_started(
            self.repo, task['id'], failed_id, 'ws', 'codex', [], effort='high')
        coordination.assignment_finished(
            self.repo, task['id'], failed_id, 'failed', 'fixture failure', [], [])
        return task, [first, second], failed_id

    def plan_command(self, task_id, payload, style):
        command = (
            shlex.quote(sys.executable) + ' -m codex_deepseek_team coordination plan '
            '--path ' + shlex.quote(str(self.repo)) +
            ' --task ' + shlex.quote(task_id))
        if style == 'heredoc':
            return command + " 2>/dev/null <<'JSON'\n" + json.dumps(payload) + '\nJSON\n'
        return "printf '%s' " + shlex.quote(json.dumps(payload)) + ' | ' + command + ' 2>/dev/null'

    def hook(self, command, session):
        return coordinator_hooks.handle({
            'session_id': session,
            'transcript_path': str(self.repo / 'transcript.jsonl'),
            'cwd': str(self.repo),
            'permission_mode': 'default',
            'hook_event_name': 'PreToolUse',
            'tool_name': 'Bash',
            'tool_use_id': 'toolu-plan-fixture',
            'tool_input': {'command': command},
        }, runtime='claude')

    def test_append_after_failed_assignment_preserves_history_and_replay_is_noop(self):
        task, items, failed_id = self.initial_plan_with_failed_first()
        third = self.deliverable('docs', 'c.py')
        appended = self.plan_cli(task['id'], self.plan_payload(*items, third))
        self.assertEqual(appended.returncode, 0, appended.stderr)
        self.assertTrue(appended.stdout.strip())
        output = json.loads(appended.stdout)
        self.assertEqual(output['issues'], [])
        self.assertEqual(len(output['assignments']), 3)
        rows = {row['deliverable_id']: row for row in output['assignments']}
        self.assertEqual(rows['impl']['id'], failed_id)
        self.assertEqual(rows['impl']['status'], 'failed')
        self.assertEqual(rows['tests']['status'], 'planned')
        self.assertEqual(rows['docs']['status'], 'planned')

        before = self.snapshot(task['id'])
        replay = self.plan_cli(task['id'], self.plan_payload(*items, third))
        self.assertEqual(replay.returncode, 0, replay.stderr)
        self.assertTrue(replay.stdout.strip())
        replayed = json.loads(replay.stdout)
        self.assertEqual([row['id'] for row in replayed['assignments']],
                         [row['id'] for row in output['assignments']])
        self.assertEqual(replayed['assignments'][0]['status'], 'failed')
        self.assertEqual(self.snapshot(task['id']), before)

    def test_changing_or_removing_a_started_deliverable_reports_64(self):
        task, items, _ = self.initial_plan_with_failed_first(session='plan-errors')
        third = self.deliverable('docs', 'c.py')
        appended = self.plan_cli(task['id'], self.plan_payload(*items, third))
        self.assertEqual(appended.returncode, 0, appended.stderr)
        before = self.snapshot(task['id'])

        changed = copy.deepcopy(items)
        changed[0]['scope'] = ['renamed.py']
        changed_result = self.plan_cli(
            task['id'], self.plan_payload(*changed, third))
        self.assertEqual(changed_result.returncode, 64)
        self.assertTrue(changed_result.stderr.strip())
        self.assertIn('immutable', changed_result.stderr.lower())
        self.assertEqual(self.snapshot(task['id']), before)

        removed_result = self.plan_cli(
            task['id'], self.plan_payload(items[1], third))
        self.assertEqual(removed_result.returncode, 64)
        self.assertTrue(removed_result.stderr.strip())
        self.assertIn('remove', removed_result.stderr.lower())
        self.assertEqual(self.snapshot(task['id']), before)

    def test_malformed_json_reports_64_without_changing_the_ledger(self):
        task = self.open_task(session='plan-malformed')
        before = self.snapshot(task['id'])
        result = self.cli('plan', '--path', str(self.repo), '--task', task['id'],
                          input_text='{"classification":')
        self.assertEqual(result.returncode, 64)
        self.assertTrue(result.stderr.strip())
        self.assertEqual(self.snapshot(task['id']), before)
        self.assertIsNone(coordination.load_task(self.repo, task['id'])['classification'])

    def test_heredoc_and_printf_pipe_plan_commands_succeed(self):
        for style in ('heredoc', 'printf'):
            with self.subTest(style=style):
                session = 'plan-' + style
                task = self.open_task(session=session)
                payload = self.plan_payload(
                    self.deliverable('impl', style + '.py'))
                command = self.plan_command(task['id'], payload, style)
                self.assertEqual(self.hook(command, session), {})
                completed = subprocess.run(
                    ['bash', '-c', command], cwd=self.repo, env=self.env,
                    text=True, capture_output=True, timeout=30)
                self.assertEqual(completed.returncode, 0, completed.stderr)
                self.assertTrue(completed.stdout.strip())
                self.assertEqual(json.loads(completed.stdout)['issues'], [])

    def test_standard_sink_redirects_do_not_deny_the_plan_command(self):
        suffixes = (
            ' 2>/dev/null',
            ' >/dev/null',
            ' >>/dev/null',
            ' &>/dev/null',
            " >'/dev/null'",
            ' 2>/dev/stderr',
            ' >/dev/stdout',
        )
        for index, suffix in enumerate(suffixes):
            with self.subTest(suffix=suffix):
                session = f'plan-sink-{index}'
                task = self.open_task(session=session)
                command = (
                    shlex.quote(sys.executable) +
                    ' -m codex_deepseek_team coordination plan --path ' +
                    shlex.quote(str(self.repo)) + ' --task ' +
                    shlex.quote(task['id']) + suffix)
                self.assertEqual(self.hook(command, session), {})


if __name__ == '__main__':
    unittest.main()
