import contextlib
import io

from codex_deepseek_team import cli, coordination
from tests import test_job_integration as job_tests
from tests.test_workspace_lifecycle import LifecycleFixture


class FinalizationCliTests(LifecycleFixture):
    task = job_tests.JobIntegrationTests.task
    run_attempt = job_tests.JobIntegrationTests.run_attempt

    def command(self, *args):
        output, errors = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            try:
                code = cli.main(list(args))
            except SystemExit as cause:
                code = cause.code
        return code, output.getvalue(), errors.getvalue()

    def test_final_use_cleans_one_job_while_parent_stays_active(self):
        self.require_capture()
        task, assignment = self.task()
        copy = self.copy()
        self.run_attempt(task, assignment, copy, 'succeeded')
        code, _out, err = self.command('coordination', 'use', '--path', str(self.source),
            '--task', task['id'], '--assignment', assignment, '--disposition', 'incorporated',
            '--evidence', 'final code review and integration complete', '--final')
        self.assertEqual(code, 0, err)
        self.assertFalse(copy.path.exists())
        self.assertEqual(coordination.load_task(self.source, task['id'])['status'], 'active')

    def test_explicit_finalize_accepts_previously_reviewed_result(self):
        self.require_capture()
        task, assignment = self.task()
        copy = self.copy()
        self.run_attempt(task, assignment, copy, 'failed')
        coordination.use_result(self.source, task['id'], assignment, 'rejected', 'unusable result inspected')
        code, _out, err = self.command('coordination', 'finalize', '--path', str(self.source),
            '--task', task['id'], '--assignment', assignment, '--evidence', 'reject this stopped job', '--cancel')
        self.assertEqual(code, 0, err)
        self.assertFalse(copy.path.exists())

    def test_needs_rework_and_final_is_rejected_before_feedback_mutation(self):
        task, assignment = self.task()
        copy = self.copy()
        self.run_attempt(task, assignment, copy, 'succeeded')
        before = coordination._task_path(self.source, task['id']).read_bytes()
        code, _out, _err = self.command('coordination', 'use', '--path', str(self.source),
            '--task', task['id'], '--assignment', assignment, '--disposition', 'needs-rework',
            '--evidence', 'still needs correction', '--final')
        self.assertNotEqual(code, 0)
        self.assertEqual(coordination._task_path(self.source, task['id']).read_bytes(), before)
        self.assertTrue(copy.path.exists())
