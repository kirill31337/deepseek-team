"""Bootstrap refresh of existing owned blocks for both coordinator runtimes.

These tests exercise the real temporary repositories, the real settings
resolution and the real coordinator hook entry point. They reproduce the
stale-render bug (a global/package change or an off/init/on cycle leaving an
old snapshot) before asserting the refresh contract.
"""
import contextlib
import io
import os
from pathlib import Path
import shlex
import subprocess
import tempfile
import unittest
from unittest import mock

from codex_deepseek_team import (activation, cli, coordinator_hooks, onboarding,
                                 project, sandbox, settings)


FOREIGN = b'# Project rules\n\nKeep this text byte-for-byte.\n'
DISABLED_MARKER = 'DeepSeek Team is disabled for this project'
ENABLED_HEADER = '### Effective delegation profile'


class BootstrapRefreshCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='dst-boot-refresh-')
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
            'CODEX_HOME': str(self.root / 'codex'),
            'CLAUDE_CONFIG_DIR': str(self.root / 'claude'),
            'XDG_CONFIG_HOME': str(self.root / 'config'),
            'DEEPSEEK_TEAM_STATE_DIR': str(self.root / 'state'),
        }, clear=False)
        environment.start()
        self.addCleanup(environment.stop)
        self.addCleanup(os.environ.pop, 'DEEPSEEK_TEAM_DISABLED', None)
        os.environ.pop('DEEPSEEK_TEAM_DISABLED', None)

    def target(self, runtime):
        return self.repo / ('AGENTS.md' if runtime == 'codex' else 'CLAUDE.md')

    def hook(self, event, runtime='codex', **extra):
        payload = dict(session_id='session-1', turn_id='turn-1', cwd=str(self.repo),
                       hook_event_name=event)
        payload.update(extra)
        return coordinator_hooks.handle(payload, runtime=runtime)

    def context(self, result):
        return result['hookSpecificOutput']['additionalContext']

    def set_global(self, **options):
        return settings.set_values(settings.global_file(), **options)

    def run_cli(self, *args):
        output, errors = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            code = cli.main(list(args))
        return subprocess.CompletedProcess(args, code, output.getvalue(), errors.getvalue())


class StaleRenderTests(BootstrapRefreshCase):
    def test_bootstrap_refreshes_stale_global_policy_for_both_runtimes(self):
        self.set_global(effort='low')
        project.attach(self.repo, coordinator='both')
        for runtime in ('codex', 'claude'):
            self.assertIn(b'effort=low', self.target(runtime).read_bytes())

        self.set_global(effort='max')
        # Reproduce the stale render: no refresh has reached the files yet.
        for runtime in ('codex', 'claude'):
            with self.subTest(runtime=runtime, phase='stale'):
                self.assertNotIn(b'effort=max', self.target(runtime).read_bytes())

        self.hook('SessionStart', 'codex', source='startup')

        for runtime in ('codex', 'claude'):
            body = self.target(runtime).read_bytes()
            with self.subTest(runtime=runtime, phase='refreshed'):
                self.assertIn(b'effort=max', body)
                self.assertNotIn(b'effort=low', body)

    def test_bootstrap_refreshes_a_changed_package_template_for_both_runtimes(self):
        project.attach(self.repo, coordinator='both')
        template = self.root / 'delegation.md'
        template.write_bytes(project.DATA_FILE.read_bytes() + b'\nTEMPLATE-MARKER-UNIQUE\n')

        with mock.patch.object(project, 'DATA_FILE', template):
            self.hook('SessionStart', 'codex', source='startup')

        for runtime in ('codex', 'claude'):
            with self.subTest(runtime=runtime):
                self.assertIn(b'TEMPLATE-MARKER-UNIQUE', self.target(runtime).read_bytes())

    def test_codex_session_start_injects_full_current_guidance_despite_stale_file(self):
        self.set_global(effort='low')
        project.attach(self.repo, coordinator='codex')
        self.set_global(effort='max')
        self.assertIn(b'effort=low', self.target('codex').read_bytes())

        text = self.context(self.hook('SessionStart', 'codex', source='startup'))
        current = settings.instructions(settings.resolve(self.repo), 'codex')
        self.assertIn(current, text)
        self.assertIn('effort=max', text)
        # The complete policy is injected once, with no duplicated reporting/rework.
        self.assertEqual(text.count('subjective estimate, not measured'), 1)
        self.assertEqual(text.count('Grade each assignment yourself'), 1)

    def test_off_init_on_is_repaired_by_the_next_bootstrap(self):
        activation.set_enabled(self.repo, False)
        project.attach(self.repo, coordinator='both')
        for runtime in ('codex', 'claude'):
            self.assertIn(DISABLED_MARKER.encode(), self.target(runtime).read_bytes())

        activation.set_enabled(self.repo, True)
        for runtime in ('codex', 'claude'):
            with self.subTest(runtime=runtime, phase='stale'):
                self.assertIn(DISABLED_MARKER.encode(), self.target(runtime).read_bytes())

        self.hook('UserPromptSubmit', 'codex', prompt='implement feature')

        for runtime in ('codex', 'claude'):
            body = self.target(runtime).read_bytes()
            with self.subTest(runtime=runtime, phase='repaired'):
                self.assertNotIn(DISABLED_MARKER.encode(), body)
                self.assertIn(ENABLED_HEADER.encode(), body)


class PreservationTests(BootstrapRefreshCase):
    def test_refresh_preserves_manual_read_only_forced_effort_and_capacity(self):
        self.set_global(delegation_level=25, access='read-only', effort='max', max_workers=5)
        project.attach(self.repo, coordinator='both')
        template = self.root / 'delegation.md'
        template.write_bytes(project.DATA_FILE.read_bytes() + b'\nTEMPLATE-MARKER-UNIQUE\n')

        with mock.patch.object(project, 'DATA_FILE', template):
            self.hook('SessionStart', 'codex', source='startup')

        for runtime in ('codex', 'claude'):
            body = self.target(runtime).read_bytes()
            with self.subTest(runtime=runtime):
                self.assertIn(b'TEMPLATE-MARKER-UNIQUE', body)
                self.assertIn(b'25% / read-only', body)
                self.assertIn(b'effort=max', body)
                self.assertIn(b'Execution capacity is 5', body)

    def test_refresh_preserves_user_prefix_suffix_and_mode_and_is_idempotent(self):
        self.set_global(effort='low')
        target = self.target('codex')
        project.attach(self.repo, coordinator='codex')
        prefix = b'# User prefix\n\nkeep me\n'
        suffix = b'\n## User suffix\nkeep this too\n'
        target.write_bytes(prefix + target.read_bytes() + suffix)
        target.chmod(0o600)

        self.set_global(effort='max')
        self.hook('SessionStart', 'codex', source='startup')

        body = target.read_bytes()
        self.assertTrue(body.startswith(prefix))
        self.assertTrue(body.endswith(suffix))
        self.assertIn(b'effort=max', body)
        self.assertEqual(target.stat().st_mode & 0o777, 0o600)

        # An unchanged policy must not rewrite the file at all.
        with mock.patch.object(project, '_write_atomic',
                               side_effect=AssertionError('unexpected write')) as write:
            self.hook('SessionStart', 'codex', source='startup')
        write.assert_not_called()
        self.assertEqual(target.read_bytes(), body)

    def test_refresh_preserves_crlf_line_endings_outside_the_block(self):
        self.set_global(effort='low')
        project.attach(self.repo, coordinator='codex')
        target = self.target('codex')
        prefix = b'# CRLF header\r\nline two\r\n'
        suffix = b'\r\n## tail\r\nkept\r\n'
        target.write_bytes(prefix + target.read_bytes() + suffix)

        self.set_global(effort='max')
        self.hook('SessionStart', 'codex', source='startup')

        body = target.read_bytes()
        self.assertTrue(body.startswith(prefix))
        self.assertTrue(body.endswith(suffix))
        self.assertIn(b'effort=max', body)

    def test_private_lessons_are_injected_but_never_persisted(self):
        project.attach(self.repo, coordinator='codex')
        self.set_global(effort='max')
        marker = 'LOCAL-LESSON-MARKER-DO-NOT-PERSIST'
        with mock.patch.object(coordinator_hooks, '_lessons_guidance', return_value=marker):
            text = self.context(self.hook('SessionStart', 'codex', source='startup'))
        self.assertIn(marker, text)
        self.assertNotIn(marker.encode(), self.target('codex').read_bytes())

    def test_unattached_and_foreign_files_are_untouched(self):
        agents = self.target('codex')
        agents.write_bytes(FOREIGN)
        self.assertEqual(self.hook('SessionStart', 'codex', source='startup'), {})
        self.assertEqual(agents.read_bytes(), FOREIGN)

        project.attach(self.repo, coordinator='codex')
        claude = self.target('claude')
        claude.write_bytes(FOREIGN)
        self.set_global(effort='max')
        self.hook('SessionStart', 'codex', source='startup')
        # A foreign CLAUDE.md is never adopted as an owned block.
        self.assertEqual(claude.read_bytes(), FOREIGN)

    def test_native_claude_subagent_is_untouched(self):
        project.attach(self.repo, coordinator='claude')
        before = self.target('claude').read_bytes()
        self.set_global(effort='max')
        result = coordinator_hooks.handle({
            'cwd': str(self.repo), 'hook_event_name': 'SessionStart', 'source': 'startup',
            'session_id': 'session-1', 'agent_id': 'child'}, runtime='claude')
        self.assertEqual(result, {})
        self.assertEqual(self.target('claude').read_bytes(), before)

    def test_pre_tool_use_and_stop_never_mutate_files(self):
        project.attach(self.repo, coordinator='codex')
        self.set_global(effort='max')
        before = self.target('codex').read_bytes()
        with mock.patch.object(project, '_write_atomic',
                               side_effect=AssertionError('unexpected write')) as write:
            self.hook('PreToolUse', 'codex', tool_name='Read', tool_input={'file_path': 'a.py'})
            self.hook('Stop', 'codex')
        write.assert_not_called()
        self.assertEqual(self.target('codex').read_bytes(), before)


class FailureAndOffTests(BootstrapRefreshCase):
    def test_failed_prompt_supplies_policy_and_repair_keeps_runtime_attachment(self):
        project.attach(self.repo, coordinator='codex')
        self.set_global(effort='max')
        with mock.patch.object(project, '_write_atomic',
                               side_effect=project.ProjectError('synthetic write failure')):
            text = self.context(self.hook('UserPromptSubmit', 'codex', prompt='status'))

        self.assertIn(settings.instructions(settings.resolve(self.repo), 'codex'), text)
        command = text.split('then rerun ', 1)[1].split(' from the repository root', 1)[0]
        with contextlib.chdir(self.repo):
            repaired = self.run_cli(*shlex.split(command)[1:])
        self.assertEqual(repaired.returncode, 0, repaired.stderr)
        self.assertIn(b'effort=max', self.target('codex').read_bytes())
        self.assertFalse(self.target('claude').exists())

    def test_malformed_current_runtime_binding_is_diagnosed_not_adopted(self):
        target = self.target('codex')
        target.write_bytes(project.START_MARKER + b'\n<!-- corrupted -->\n'
                           + project.END_MARKER + b'\n')
        before = target.read_bytes()

        text = self.context(self.hook('SessionStart', 'codex', source='startup'))
        self.assertIn('malformed', text.lower())
        self.assertEqual(target.read_bytes(), before)

        # Non-lifecycle events stay inert instead of adopting the bad block.
        self.assertEqual(self.hook('PreToolUse', 'codex', tool_name='Read',
                                   tool_input={'file_path': 'a.py'}), {})
        self.assertEqual(target.read_bytes(), before)

    def test_paired_refresh_failure_rolls_back_and_reports_recovery(self):
        self.set_global(effort='low')
        project.attach(self.repo, coordinator='both')
        self.set_global(effort='max')
        before = {runtime: self.target(runtime).read_bytes() for runtime in ('codex', 'claude')}
        real_write = project._write_atomic
        calls = {'count': 0}

        def flaky(target, original, mode, content):
            calls['count'] += 1
            if calls['count'] == 2:
                raise project.ProjectError('synthetic second-write failure')
            return real_write(target, original, mode, content)

        with mock.patch.object(project, '_write_atomic', side_effect=flaky):
            text = self.context(self.hook('SessionStart', 'codex', source='startup'))

        self.assertIn('could not refresh', text.lower())
        self.assertIn('rerun deepseek-team init', text)
        for runtime in ('codex', 'claude'):
            with self.subTest(runtime=runtime):
                self.assertEqual(self.target(runtime).read_bytes(), before[runtime])
        self.assertGreaterEqual(calls['count'], 3)

    def test_symlinked_target_is_refused_without_following_it(self):
        real = self.root / 'outside.md'
        real.write_bytes(FOREIGN)
        link = self.target('codex')
        os.symlink(real, link)

        text = self.context(self.hook('SessionStart', 'codex', source='startup'))
        self.assertIn('malformed', text.lower())
        self.assertTrue(link.is_symlink())
        self.assertEqual(real.read_bytes(), FOREIGN)

    def test_nonregular_target_is_refused(self):
        target = self.target('codex')
        target.mkdir()

        text = self.context(self.hook('SessionStart', 'codex', source='startup'))
        self.assertIn('malformed', text.lower())
        self.assertTrue(target.is_dir())

    def test_on_off_cli_do_not_rewrite_the_managed_files(self):
        project.attach(self.repo, coordinator='both')
        before = {runtime: self.target(runtime).read_bytes() for runtime in ('codex', 'claude')}

        self.assertEqual(self.run_cli('off', str(self.repo)).returncode, 0)
        for runtime in ('codex', 'claude'):
            self.assertEqual(self.target(runtime).read_bytes(), before[runtime])
        self.assertEqual(self.run_cli('on', str(self.repo)).returncode, 0)
        for runtime in ('codex', 'claude'):
            self.assertEqual(self.target(runtime).read_bytes(), before[runtime])

    def test_off_still_bypasses_gates_and_renders_disabled_blocks(self):
        project.attach(self.repo, coordinator='both')
        activation.set_enabled(self.repo, False)

        for event in ('SessionStart', 'UserPromptSubmit'):
            result = self.hook(event, 'codex', source='startup', prompt='hi')
            with self.subTest(event=event):
                self.assertIn('disabled', self.context(result).lower())
        self.assertEqual(self.hook('PreToolUse', 'codex', tool_name='Write',
                                   tool_input={'file_path': 'a.py'}), {})
        self.assertNotIn('decision', self.hook('Stop', 'codex'))

        for runtime in ('codex', 'claude'):
            self.assertIn(DISABLED_MARKER.encode(), self.target(runtime).read_bytes())

    def test_prompt_injects_full_policy_only_when_refresh_changes_files(self):
        self.set_global(effort='low')
        project.attach(self.repo, coordinator='both')

        unchanged = self.context(self.hook('UserPromptSubmit', 'codex', prompt='status'))
        self.assertNotIn(ENABLED_HEADER, unchanged)

        self.set_global(effort='max')
        changed = self.context(self.hook('UserPromptSubmit', 'codex', turn_id='turn-2',
                                         prompt='implement feature'))
        self.assertIn(ENABLED_HEADER, changed)
        self.assertIn('effort=max', changed)
        self.assertEqual(changed.count('subjective estimate, not measured'), 1)


class SetupRefreshTests(BootstrapRefreshCase):
    def run_setup(self):
        backend = sandbox.SandboxBackend(('/test/bwrap',), '/test/bwrap', 'direct')
        which = mock.patch('shutil.which', side_effect=lambda name: '/test/bin/' + name)
        output = io.StringIO()
        with which, \
             mock.patch.object(sandbox, 'probe_backend', return_value=backend), \
             mock.patch.object(onboarding.doctor, 'main', return_value=0), \
             mock.patch.object(onboarding.config, 'codex_hooks_status', return_value=True), \
             contextlib.chdir(self.repo), contextlib.redirect_stdout(output):
            code = onboarding.run(['codex'], no_key=True)
        return code, output.getvalue()

    def test_setup_refreshes_owned_blocks_in_the_current_repository(self):
        self.set_global(effort='low')
        project.attach(self.repo, coordinator='both')
        self.set_global(effort='max')

        code, output = self.run_setup()

        self.assertEqual(code, 0, output)
        for runtime in ('codex', 'claude'):
            self.assertIn(b'effort=max', self.target(runtime).read_bytes())

    def test_setup_does_not_attach_a_fresh_repository(self):
        code, output = self.run_setup()

        self.assertEqual(code, 0, output)
        self.assertFalse(self.target('codex').exists())
        self.assertFalse(self.target('claude').exists())
        # Upgrade guidance: rerun setup per runtime, then sessions refresh blocks;
        # init is only for a first attachment and pip/uv alone are not promised.
        self.assertIn('rerun setup once per installed runtime', output)
        self.assertIn('first attachment', output)
        self.assertNotIn('pipx', output)
        self.assertNotIn('uv tool', output)

    def test_setup_reports_failure_when_owned_block_cannot_be_refreshed(self):
        project.attach(self.repo, coordinator='both')
        target = self.target('claude')
        target.write_bytes(target.read_bytes().replace(
            b'original:created', b'original:modified'))
        before = {runtime: self.target(runtime).read_bytes()
                  for runtime in ('codex', 'claude')}

        code, output = self.run_setup()

        self.assertNotEqual(code, 0, output)
        self.assertIn('Setup incomplete', output)
        self.assertNotIn('Local setup checks passed', output)
        for runtime, original in before.items():
            self.assertEqual(self.target(runtime).read_bytes(), original)


if __name__ == '__main__':
    unittest.main()
