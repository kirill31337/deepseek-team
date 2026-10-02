"""Runtime readiness for the managed worker namespace.

Every test here executes real scripts through real subprocess probes. The
namespace-unavailable test uses the caller's namespace layout prefix; the one
test that needs a genuine nested Bubblewrap namespace is skipped with an
explicit reason when this worker cannot create nested user namespaces.
"""
from __future__ import annotations

import os
from pathlib import Path
import shlex
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from codex_deepseek_team import check_evidence, runtime_preflight, sandbox

CODEX_VERSION = 'fake-codex 9.9'
CLAUDE_VERSION = 'fake-claude 8.8'
REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
FACTORY_CANARY = 'CANARY-factory-text-2f4c1d-must-not-leak'
CODEX_FLAGS = (
    '--strict-config', '--sandbox', '--ephemeral', '--json', '--ignore-rules',
    'danger-full-access',
)
CLAUDE_FLAGS = (
    '--bare', '--tools', '--allowedTools', '--disallowedTools', '--permission-mode',
    'dontAsk', '--disable-slash-commands', '--setting-sources', '--strict-mcp-config',
    '--mcp-config', '--no-session-persistence', '--output-format',
)


def write_executable(path, body):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding='utf-8')
    path.chmod(stat.S_IRWXU)
    return path


def codex_body(*, version=CODEX_VERSION, flags=CODEX_FLAGS, exit_code=0):
    lines = [
        "if [ -n \"${CALL_LOG:-}\" ]; then printf '%s\\n' \"$*\" >> \"$CALL_LOG\"; fi",
        f'if [ "${{1:-}}" = "--version" ]; then echo {shlex.quote(version)}; exit {exit_code}; fi',
        'if [ "${1:-}" = "exec" ] && [ "${2:-}" = "--help" ]; then',
    ]
    lines.extend(f'  echo {shlex.quote(flag)}' for flag in flags)
    lines.extend(['  exit 0', 'fi', 'echo "unexpected codex invocation" >&2', 'exit 64'])
    return '#!/bin/sh\n' + '\n'.join(lines) + '\n'


def claude_body(*, version=CLAUDE_VERSION, flags=CLAUDE_FLAGS):
    lines = [
        "if [ -n \"${CALL_LOG:-}\" ]; then printf '%s\\n' \"$*\" >> \"$CALL_LOG\"; fi",
        f'if [ "${{1:-}}" = "--version" ]; then echo {shlex.quote(version)}; exit 0; fi',
        'if [ "${1:-}" = "--help" ]; then',
    ]
    lines.extend(f'  echo {shlex.quote(flag)}' for flag in flags)
    lines.extend(['  exit 0', 'fi', 'echo "unexpected claude invocation" >&2', 'exit 64'])
    return '#!/bin/sh\n' + '\n'.join(lines) + '\n'


def noisy_codex_body(noise_bytes):
    """Codex fixture that emits ``noise_bytes`` of stderr noise per probe."""
    lines = [
        "if [ -n \"${CALL_LOG:-}\" ]; then printf '%s\\n' \"$*\" >> \"$CALL_LOG\"; fi",
        'if [ "${1:-}" = "--version" ]; then',
        f'  head -c {noise_bytes} /dev/zero >&2',
        f'  echo {shlex.quote(CODEX_VERSION)}',
        '  exit 0',
        'fi',
        'if [ "${1:-}" = "exec" ] && [ "${2:-}" = "--help" ]; then',
        f'  head -c {noise_bytes} /dev/zero >&2',
    ]
    lines.extend(f'  echo {shlex.quote(flag)}' for flag in CODEX_FLAGS)
    lines.extend(['  exit 0', 'fi', 'echo "unexpected codex invocation" >&2', 'exit 64'])
    return '#!/bin/sh\n' + '\n'.join(lines) + '\n'


def slow_codex_body(delay):
    """Codex fixture that sleeps before answering either probe."""
    lines = [
        "if [ -n \"${CALL_LOG:-}\" ]; then printf '%s\\n' \"$*\" >> \"$CALL_LOG\"; fi",
        f'sleep {delay}',
        f'if [ "${{1:-}}" = "--version" ]; then echo {shlex.quote(CODEX_VERSION)}; exit 0; fi',
        'if [ "${1:-}" = "exec" ] && [ "${2:-}" = "--help" ]; then',
    ]
    lines.extend(f'  echo {shlex.quote(flag)}' for flag in CODEX_FLAGS)
    lines.extend(['  exit 0', 'fi', 'echo "unexpected codex invocation" >&2', 'exit 64'])
    return '#!/bin/sh\n' + '\n'.join(lines) + '\n'


class ContextFactory:
    """Stand-in for the coordinator-owned caller preparation."""

    def __init__(self, *, environment=None, layout=(), failures=None, log=None):
        self.environment = {'PATH': '/usr/bin:/bin'} if environment is None else dict(environment)
        self.layout = list(layout)
        self.failures = dict(failures or {})
        self.calls = []
        if log is not None:
            self.environment['CALL_LOG'] = str(log)

    def __call__(self, binary, runtime):
        self.calls.append((str(binary), runtime))
        failure = self.failures.get(str(binary))
        if failure is not None:
            if isinstance(failure, BaseException):
                raise failure
            raise RuntimeError(failure)
        return list(self.layout), dict(self.environment)


class RuntimePreflightCase(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='dst-runtime-preflight-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.host_path = os.environ.get('PATH', '')
        self.home = self._dir('home')
        self.codex_home = self._dir('codex-home')
        self.claude_config = self._dir('claude-config')
        self.xdg_config = self._dir('xdg-config')
        self.state = self._dir('state')
        self.bin = self._dir('bin')
        patcher = mock.patch.dict(os.environ, {
            'HOME': str(self.home),
            'CODEX_HOME': str(self.codex_home),
            'CLAUDE_CONFIG_DIR': str(self.claude_config),
            'XDG_CONFIG_HOME': str(self.xdg_config),
            'XDG_STATE_HOME': str(self.state),
            'XDG_CACHE_HOME': str(self._dir('cache')),
            'DEEPSEEK_TEAM_STATE_DIR': str(self.state / 'deepseek-team'),
            'PATH': str(self.bin),
        })
        patcher.start()
        self.addCleanup(patcher.stop)
        system = mock.patch.object(runtime_preflight, 'SYSTEM_DIRECTORIES', ())
        system.start()
        self.addCleanup(system.stop)

    def _dir(self, name):
        path = self.root / name
        path.mkdir(parents=True, exist_ok=True)
        return path

    def use_path(self, *directories, system=()):
        for patcher in (
            mock.patch.dict(os.environ, {'PATH': os.pathsep.join(str(p) for p in directories)}),
            mock.patch.object(runtime_preflight, 'SYSTEM_DIRECTORIES',
                              tuple(str(p) for p in system)),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def env(self, path='/usr/bin:/bin', **extra):
        environment = {'PATH': path}
        environment.update(extra)
        return environment


class ProbeRuntimeTests(RuntimePreflightCase):
    def test_probe_runs_only_local_version_and_capability_help(self):
        log = self.root / 'calls.log'
        binary = write_executable(self.bin / 'codex', codex_body())
        version = runtime_preflight.probe_runtime(
            str(binary), 'codex', [], self.env(CALL_LOG=str(log)))
        self.assertEqual(version, CODEX_VERSION)
        self.assertEqual(log.read_text(encoding='utf-8').splitlines(),
                         ['--version', 'exec --help'])

    def test_probe_uses_caller_namespace_prefix_and_environment(self):
        log = self.root / 'calls.log'
        binary = write_executable(self.bin / 'codex', codex_body())
        harness = (
            "import os, sys\n"
            "assert sys.argv[1] == '--', sys.argv\n"
            "assert os.environ.get('NAMESPACE_MARKER') == 'present'\n"
            "binary = sys.argv[2]\n"
            "os.execv(binary, [binary] + sys.argv[3:])\n"
        )
        args = [sys.executable, '-I', '-c', harness]
        version = runtime_preflight.probe_runtime(
            str(binary), 'codex', args,
            self.env(NAMESPACE_MARKER='present', CALL_LOG=str(log)))
        self.assertEqual(version, CODEX_VERSION)
        self.assertEqual(log.read_text(encoding='utf-8').splitlines(),
                         ['--version', 'exec --help'])

    def test_probe_rejects_missing_codex_capability(self):
        flags = tuple(flag for flag in CODEX_FLAGS if flag != '--json')
        binary = write_executable(self.bin / 'codex', codex_body(flags=flags))
        with self.assertRaises(runtime_preflight.RuntimePreflightError) as caught:
            runtime_preflight.probe_runtime(str(binary), 'codex', [], self.env())
        self.assertEqual(caught.exception.code, 78)
        self.assertIn('--json', str(caught.exception))
        self.assertIn(str(binary), str(caught.exception))

    def test_probe_claude_uses_plain_help_and_rejects_missing_capability(self):
        log = self.root / 'calls.log'
        binary = write_executable(self.bin / 'claude', claude_body())
        version = runtime_preflight.probe_runtime(
            str(binary), 'claude', [], self.env(CALL_LOG=str(log)))
        self.assertEqual(version, CLAUDE_VERSION)
        self.assertEqual(log.read_text(encoding='utf-8').splitlines(),
                         ['--version', '--help'])
        flags = tuple(flag for flag in CLAUDE_FLAGS if flag != '--tools')
        broken = write_executable(self.bin / 'claude-broken', claude_body(flags=flags))
        with self.assertRaises(runtime_preflight.RuntimePreflightError) as caught:
            runtime_preflight.probe_runtime(str(broken), 'claude', [], self.env())
        self.assertIn('--tools', str(caught.exception))

    def test_probe_reports_unrunnable_launcher_with_bounded_explanation(self):
        noise = 'x' * 5000
        binary = write_executable(
            self.bin / 'codex',
            '#!/bin/sh\nprintf \'%s\\n\' ' + shlex.quote(noise + ' missing-sibling') + ' >&2\nexit 127\n')
        with self.assertRaises(runtime_preflight.RuntimePreflightError) as caught:
            runtime_preflight.probe_runtime(str(binary), 'codex', [], self.env())
        message = str(caught.exception)
        self.assertLess(len(message), 1000)
        self.assertIn('127', message)
        self.assertIn('missing-sibling', message)
        self.assertEqual(caught.exception.code, 78)

    def test_probe_reports_absent_binary_instead_of_oserror(self):
        with self.assertRaises(runtime_preflight.RuntimePreflightError) as caught:
            runtime_preflight.probe_runtime(
                str(self.bin / 'absent-codex'), 'codex', [], self.env())
        self.assertIn('absent-codex', str(caught.exception))

    def test_probe_enforces_finite_local_deadline(self):
        binary = write_executable(self.bin / 'codex', '#!/bin/sh\nexec sleep 5\n')
        started = time.monotonic()
        with mock.patch.object(runtime_preflight, 'PROBE_TIMEOUT', 0.4):
            with self.assertRaises(runtime_preflight.RuntimePreflightError) as caught:
                runtime_preflight.probe_runtime(str(binary), 'codex', [], self.env())
        self.assertLess(time.monotonic() - started, 4.0)
        self.assertIn('timed out', str(caught.exception).lower())

    def test_probe_rejects_unsupported_runtime(self):
        binary = write_executable(self.bin / 'codex', codex_body())
        with self.assertRaises(runtime_preflight.RuntimePreflightError):
            runtime_preflight.probe_runtime(str(binary), 'other', [], self.env())


    def test_probe_noisy_streams_are_captured_within_bounded_memory(self):
        noise = 32 * 1024 * 1024
        binary = write_executable(self.bin / 'codex', noisy_codex_body(noise))
        source_root = str(Path(runtime_preflight.__file__).resolve().parents[1])
        script = (
            'import resource, sys\n'
            f'sys.path.insert(0, {source_root!r})\n'
            'from codex_deepseek_team import runtime_preflight\n'
            'before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss\n'
            f'version = runtime_preflight.probe_runtime({str(binary)!r}, "codex", [], '
            '{"PATH": "/usr/bin:/bin"})\n'
            'growth = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss - before\n'
            'print(version.splitlines()[0])\n'
            'print(growth)\n'
        )
        result = subprocess.run([sys.executable, '-I', '-c', script], text=True,
                                capture_output=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stderr)
        version, growth_kib = result.stdout.splitlines()[:2]
        self.assertEqual(version, CODEX_VERSION)
        growth = int(growth_kib) * 1024
        self.assertLess(growth, noise // 2,
                        f'probing retained {growth} bytes of child output')

    def test_probe_evidence_is_private_bounded_and_removed(self):
        noise = check_evidence.MAX_OUTPUT_BYTES * 3
        binary = write_executable(self.bin / 'codex', noisy_codex_body(noise))
        real_run_checks = runtime_preflight.run_checks
        observed = []

        def spy(args, env, commands, **kwargs):
            rows = real_run_checks(args, env, commands, **kwargs)
            directory = Path(kwargs['directory'])
            observed.append({
                'directory': directory,
                'mode': stat.S_IMODE(directory.stat().st_mode),
                'sizes': [item.stat().st_size for item in directory.iterdir()],
            })
            return rows

        with mock.patch.object(runtime_preflight, 'run_checks', spy):
            version = runtime_preflight.probe_runtime(str(binary), 'codex', [], self.env())
        self.assertTrue(version.startswith(CODEX_VERSION))
        self.assertEqual(len(observed), 2)
        for entry in observed:
            self.assertEqual(entry['mode'] & 0o077, 0)
            self.assertTrue(entry['sizes'])
            self.assertLessEqual(max(entry['sizes']), check_evidence.MAX_OUTPUT_BYTES)
            self.assertIn(check_evidence.MAX_OUTPUT_BYTES, entry['sizes'])
            self.assertFalse(entry['directory'].exists())
            self.assertFalse(entry['directory'].resolve().is_relative_to(REPOSITORY_ROOT))

    def test_local_probe_timeout_stays_78_with_and_without_budget(self):
        binary = write_executable(self.bin / 'codex', '#!/bin/sh\nsleep 30\n')
        with mock.patch.object(runtime_preflight, 'PROBE_TIMEOUT', 0.4):
            with self.assertRaises(runtime_preflight.RuntimePreflightError) as caught:
                runtime_preflight.probe_runtime(str(binary), 'codex', [], self.env())
            self.assertEqual(caught.exception.code, 78)
            self.assertIn('timed out', str(caught.exception).lower())
            with self.assertRaises(runtime_preflight.RuntimePreflightError) as caught:
                runtime_preflight.probe_runtime(
                    str(binary), 'codex', [], self.env(),
                    deadline=time.monotonic() + 30)
            self.assertEqual(caught.exception.code, 78)

    def test_expired_deadline_stops_before_any_probe_process(self):
        log = self.root / 'calls.log'
        binary = write_executable(self.bin / 'codex', codex_body())
        with self.assertRaises(runtime_preflight.RuntimePreflightError) as caught:
            runtime_preflight.probe_runtime(
                str(binary), 'codex', [], self.env(CALL_LOG=str(log)),
                deadline=time.monotonic() - 1.0)
        self.assertEqual(caught.exception.code, 124)
        self.assertIn('budget', str(caught.exception).lower())
        self.assertFalse(log.exists())

    def test_budget_expiry_cleans_up_probe_process_group(self):
        pid_path = self.root / 'probe.pid'
        binary = write_executable(
            self.bin / 'codex',
            '#!/bin/sh\n'
            'if [ "${1:-}" = "--version" ]; then\n'
            f'  echo $$ > {shlex.quote(str(pid_path))}\n'
            '  sleep 30\n'
            'fi\n'
            'echo "unexpected codex invocation" >&2\n'
            'exit 64\n')
        started = time.monotonic()
        with self.assertRaises(runtime_preflight.RuntimePreflightError) as caught:
            runtime_preflight.probe_runtime(str(binary), 'codex', [], self.env(),
                                            deadline=time.monotonic() + 1.0)
        self.assertEqual(caught.exception.code, 124)
        self.assertLess(time.monotonic() - started, 10.0)
        self.assertTrue(pid_path.exists(), 'probe fixture never started')
        self._assert_process_gone(int(pid_path.read_text(encoding='utf-8').strip()))

    def _assert_process_gone(self, pid, timeout=5.0):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return
            try:
                with open(f'/proc/{pid}/stat', encoding='ascii') as handle:
                    state = handle.read().split()[2]
            except (OSError, IndexError):
                return
            if state == 'Z':
                return
            time.sleep(0.05)
        self.fail(f'probe child process {pid} survived the probe budget expiry')


class DefaultCandidateTests(RuntimePreflightCase):
    def test_default_candidates_follow_path_order_then_system_dirs(self):
        first = write_executable(self._dir('first') / 'codex', codex_body())
        second = write_executable(self._dir('second') / 'codex', codex_body(exit_code=3))
        unused = write_executable(self._dir('system') / 'codex', codex_body())
        self.use_path(second.parent, first.parent, second.parent, system=(unused.parent,))
        factory = ContextFactory()
        selection = runtime_preflight.select_runtime(
            'codex', 'codex', 'claude', factory)
        self.assertEqual(selection.binary, str(first))
        self.assertEqual([call[0] for call in factory.calls], [str(second), str(first)])

    def test_system_dirs_are_used_after_path_when_path_candidates_fail(self):
        broken = write_executable(self._dir('path') / 'codex',
                                  codex_body(exit_code=3))
        broken_system = write_executable(self._dir('usr-local') / 'codex',
                                         codex_body(exit_code=3))
        working_system = write_executable(self._dir('usr-bin') / 'codex', codex_body())
        self.use_path(broken.parent, system=(broken_system.parent, working_system.parent))
        factory = ContextFactory()
        selection = runtime_preflight.select_runtime('codex', 'codex', 'claude', factory)
        self.assertEqual(selection.binary, str(working_system))
        self.assertEqual([call[0] for call in factory.calls],
                         [str(broken), str(broken_system), str(working_system)])

    def test_default_candidates_deduplicate_resolved_identity(self):
        real = self._dir('real')
        target = write_executable(real / 'codex', codex_body())
        alias = self._dir('alias')
        (alias / 'codex').symlink_to(target)
        self.use_path(alias, real)
        factory = ContextFactory()
        selection = runtime_preflight.select_runtime('codex', 'codex', 'claude', factory)
        self.assertEqual(selection.binary, str(alias / 'codex'))
        self.assertEqual([call[0] for call in factory.calls], [str(alias / 'codex')])

    def test_path_precedes_system_dirs_when_both_work(self):
        path_binary = write_executable(self.bin / 'codex', codex_body())
        system_binary = write_executable(self._dir('system') / 'codex', codex_body())
        self.use_path(self.bin, system=(system_binary.parent,))
        factory = ContextFactory()
        selection = runtime_preflight.select_runtime('codex', 'codex', 'claude', factory)
        self.assertEqual(selection.binary, str(path_binary))
        self.assertEqual([call[0] for call in factory.calls], [str(path_binary)])

    def test_default_candidate_with_missing_capabilities_falls_through(self):
        old = write_executable(
            self._dir('old') / 'codex',
            codex_body(flags=tuple(f for f in CODEX_FLAGS if f != '--json')))
        new = write_executable(self._dir('new') / 'codex', codex_body())
        self.use_path(old.parent, new.parent)
        factory = ContextFactory()
        selection = runtime_preflight.select_runtime('codex', 'codex', 'claude', factory)
        self.assertEqual(selection.binary, str(new))


class ExplicitPinTests(RuntimePreflightCase):
    def test_explicit_path_pin_missing_is_fatal_and_never_replaced(self):
        working = write_executable(self._dir('system') / 'codex', codex_body())
        self.use_path(self.bin, system=(working.parent,))
        factory = ContextFactory()
        with self.assertRaises(runtime_preflight.RuntimePreflightError) as caught:
            runtime_preflight.select_runtime(
                'codex', str(self.bin / 'missing-codex'), 'claude', factory,
                explicit_codex=True)
        self.assertEqual(factory.calls, [])
        self.assertIn('missing-codex', str(caught.exception))
        self.assertIn('explicit', str(caught.exception).lower())

    def test_explicit_pin_with_default_name_is_not_replaced_by_system_fallback(self):
        broken = write_executable(self.bin / 'codex', codex_body(exit_code=3))
        system = write_executable(self._dir('system') / 'codex', codex_body())
        self.use_path(self.bin, system=(system.parent,))
        factory = ContextFactory()
        with self.assertRaises(runtime_preflight.RuntimePreflightError) as caught:
            runtime_preflight.select_runtime(
                'codex', 'codex', 'claude', factory, explicit_codex=True)
        self.assertEqual([call[0] for call in factory.calls], [str(broken)])
        self.assertIn(str(broken), str(caught.exception))
        self.assertIn('explicit', str(caught.exception).lower())

    def test_default_name_with_same_value_does_fall_back(self):
        broken = write_executable(self.bin / 'codex', codex_body(exit_code=3))
        system = write_executable(self._dir('system') / 'codex', codex_body())
        self.use_path(self.bin, system=(system.parent,))
        factory = ContextFactory()
        selection = runtime_preflight.select_runtime('codex', 'codex', 'claude', factory)
        self.assertEqual(selection.binary, str(system))
        self.assertEqual([call[0] for call in factory.calls], [str(broken), str(system)])

    def test_explicit_pin_missing_capabilities_is_fatal(self):
        broken = write_executable(
            self.bin / 'codex',
            codex_body(flags=tuple(f for f in CODEX_FLAGS if f != '--sandbox')))
        system = write_executable(self._dir('system') / 'codex', codex_body())
        self.use_path(self.bin, system=(system.parent,))
        factory = ContextFactory()
        with self.assertRaises(runtime_preflight.RuntimePreflightError) as caught:
            runtime_preflight.select_runtime(
                'codex', 'codex', 'claude', factory, explicit_codex=True)
        self.assertEqual([call[0] for call in factory.calls], [str(broken)])
        self.assertIn('--sandbox', str(caught.exception))


class SelectionTests(RuntimePreflightCase):
    def test_layout_and_environment_come_from_the_selected_candidate(self):
        first = write_executable(self._dir('first') / 'codex', codex_body())
        second = write_executable(self._dir('second') / 'codex', codex_body())
        self.use_path(first.parent, second.parent)
        harness = (
            "import os, sys\n"
            "os.execv(sys.argv[2], [sys.argv[2]] + sys.argv[3:])\n"
        )
        namespace = [sys.executable, '-I', '-c', harness]

        def factory(binary, runtime):
            return list(namespace), self.env(PROBE_TOKEN=str(binary))

        selection = runtime_preflight.select_runtime('codex', 'codex', 'claude', factory)
        self.assertEqual(selection.runtime, 'codex')
        self.assertEqual(selection.binary, str(first))
        self.assertEqual(selection.layout, namespace)
        self.assertEqual(selection.environment['PROBE_TOKEN'], str(first))
        self.assertIsInstance(selection.layout, list)
        self.assertIsInstance(selection.environment, dict)

    def test_auto_tries_codex_then_claude(self):
        broken = write_executable(self._dir('broken') / 'codex', codex_body(exit_code=3))
        claude = write_executable(self._dir('claude') / 'claude', claude_body())
        self.use_path(broken.parent, claude.parent)
        factory = ContextFactory()
        selection = runtime_preflight.select_runtime('auto', 'codex', 'claude', factory)
        self.assertEqual(selection.runtime, 'claude')
        self.assertEqual(selection.binary, str(claude))

    def test_auto_explicit_codex_failure_does_not_silently_use_claude(self):
        codex = write_executable(self.bin / 'codex', codex_body(exit_code=3))
        claude = write_executable(self._dir('claude') / 'claude', claude_body())
        self.use_path(self.bin, claude.parent)
        factory = ContextFactory()
        with self.assertRaises(runtime_preflight.RuntimePreflightError) as caught:
            runtime_preflight.select_runtime(
                'auto', 'codex', 'claude', factory, explicit_codex=True)
        self.assertEqual([call[0] for call in factory.calls], [str(codex)])
        self.assertIn(str(codex), str(caught.exception))

    def test_auto_explicit_claude_failure_is_reported(self):
        broken = write_executable(self._dir('codex') / 'codex', codex_body(exit_code=3))
        claude = write_executable(self.bin / 'claude', claude_body(flags=()))
        self.use_path(broken.parent, self.bin)
        factory = ContextFactory()
        with self.assertRaises(runtime_preflight.RuntimePreflightError) as caught:
            runtime_preflight.select_runtime(
                'auto', 'codex', 'claude', factory, explicit_claude=True)
        self.assertIn(str(claude), str(caught.exception))

    def test_explicit_runtime_never_crosses(self):
        claude = write_executable(self._dir('claude') / 'claude', claude_body())
        self.use_path(self.bin, claude.parent)
        # Codex is missing while Claude works: no cross-runtime fallback.
        with self.assertRaises(runtime_preflight.RuntimePreflightError) as caught:
            runtime_preflight.select_runtime('codex', 'codex', 'claude', ContextFactory())
        self.assertIn('Codex', str(caught.exception))
        with self.assertRaises(runtime_preflight.RuntimePreflightError) as caught:
            runtime_preflight.select_runtime('claude', 'codex', 'absent-claude',
                                             ContextFactory())
        self.assertIn('Claude Code', str(caught.exception))

    def test_context_factory_failure_falls_through_for_defaults(self):
        first = write_executable(self._dir('first') / 'codex', codex_body())
        second = write_executable(self._dir('second') / 'codex', codex_body())
        self.use_path(first.parent, second.parent)
        factory = ContextFactory(failures={str(first): 'unsafe runtime prefix'})
        selection = runtime_preflight.select_runtime('codex', 'codex', 'claude', factory)
        self.assertEqual(selection.binary, str(second))

    def test_context_factory_failure_for_explicit_pin_is_fatal(self):
        pin = write_executable(self.bin / 'codex', codex_body())
        system = write_executable(self._dir('system') / 'codex', codex_body())
        self.use_path(self.bin, system=(system.parent,))
        factory = ContextFactory(
            failures={str(pin): sandbox.SandboxError(78, 'unsafe runtime prefix')})
        with self.assertRaises(runtime_preflight.RuntimePreflightError) as caught:
            runtime_preflight.select_runtime(
                'codex', 'codex', 'claude', factory, explicit_codex=True)
        self.assertEqual([call[0] for call in factory.calls], [str(pin)])
        self.assertIn('unsafe runtime prefix', str(caught.exception))

    def test_unknown_factory_errors_are_not_echoed(self):
        first = write_executable(self._dir('first') / 'codex', codex_body())
        second = write_executable(self._dir('second') / 'codex', codex_body())
        self.use_path(first.parent, second.parent)
        factory = ContextFactory(failures={str(first): RuntimeError(FACTORY_CANARY),
                                           str(second): OSError(FACTORY_CANARY)})
        with self.assertRaises(runtime_preflight.RuntimePreflightError) as caught:
            runtime_preflight.select_runtime('codex', 'codex', 'claude', factory)
        message = str(caught.exception)
        self.assertNotIn(FACTORY_CANARY, message)
        self.assertIn('could not be prepared', message)
        self.assertEqual(caught.exception.code, 78)

    def test_pinned_oserror_canary_is_not_echoed(self):
        pin = write_executable(self.bin / 'codex', codex_body())
        self.use_path(self.bin)
        factory = ContextFactory(failures={str(pin): OSError(FACTORY_CANARY)})
        with self.assertRaises(runtime_preflight.RuntimePreflightError) as caught:
            runtime_preflight.select_runtime(
                'codex', 'codex', 'claude', factory, explicit_codex=True)
        message = str(caught.exception)
        self.assertNotIn(FACTORY_CANARY, message)
        self.assertIn('could not be prepared', message)

    def test_known_coordinator_error_messages_are_summarized(self):
        first = write_executable(self._dir('first') / 'codex', codex_body())
        second = write_executable(self._dir('second') / 'codex', codex_body())
        self.use_path(first.parent, second.parent)
        factory = ContextFactory(failures={
            str(first): sandbox.SandboxError(78, 'safe first explanation'),
            str(second): check_evidence.CheckEvidenceError('safe second explanation'),
        })
        with self.assertRaises(runtime_preflight.RuntimePreflightError) as caught:
            runtime_preflight.select_runtime('codex', 'codex', 'claude', factory)
        message = str(caught.exception)
        self.assertIn('safe first explanation', message)
        self.assertIn('safe second explanation', message)

    def test_shared_budget_expiry_stops_candidate_fallback(self):
        hanging = write_executable(self.bin / 'codex', '#!/bin/sh\nsleep 30\n')
        working = write_executable(self._dir('second') / 'codex', codex_body())
        self.use_path(self.bin, working.parent)
        factory = ContextFactory()
        started = time.monotonic()
        with self.assertRaises(runtime_preflight.RuntimePreflightError) as caught:
            runtime_preflight.select_runtime('codex', 'codex', 'claude', factory,
                                             deadline=time.monotonic() + 0.6)
        self.assertEqual(caught.exception.code, 124)
        self.assertLess(time.monotonic() - started, 8.0)
        self.assertIn('budget', str(caught.exception).lower())
        self.assertEqual([call[0] for call in factory.calls], [str(hanging)])

    def test_deadline_is_shared_by_both_probes_of_one_candidate(self):
        slow = write_executable(self.bin / 'codex', slow_codex_body(0.6))
        with self.assertRaises(runtime_preflight.RuntimePreflightError) as caught:
            runtime_preflight.probe_runtime(str(slow), 'codex', [], self.env(),
                                            deadline=time.monotonic() + 0.9)
        self.assertEqual(caught.exception.code, 124)
        self.assertIn('budget', str(caught.exception).lower())

    def test_expired_budget_stops_before_context_factory(self):
        binary = write_executable(self.bin / 'codex', codex_body())
        self.use_path(self.bin)
        factory = ContextFactory()
        with self.assertRaises(runtime_preflight.RuntimePreflightError) as caught:
            runtime_preflight.select_runtime('auto', 'codex', 'claude', factory,
                                             deadline=time.monotonic() - 1.0)
        self.assertEqual(caught.exception.code, 124)
        self.assertIn('budget', str(caught.exception).lower())
        self.assertEqual(factory.calls, [])

    def test_missing_runtime_reports_all_default_candidates(self):
        broken = write_executable(self.bin / 'codex', codex_body(exit_code=3))
        self.use_path(self.bin)
        with self.assertRaises(runtime_preflight.RuntimePreflightError) as caught:
            runtime_preflight.select_runtime('codex', 'codex', 'claude', ContextFactory())
        message = str(caught.exception)
        self.assertLess(len(message), 1000)
        self.assertIn('Codex', message)
        self.assertIn(str(broken), message)

    def test_unsupported_requested_runtime_is_rejected(self):
        with self.assertRaises(runtime_preflight.RuntimePreflightError) as caught:
            runtime_preflight.select_runtime('other', 'codex', 'claude', ContextFactory())
        self.assertEqual(caught.exception.code, 64)

    def test_probe_error_exposes_message_and_default_code(self):
        error = runtime_preflight.RuntimePreflightError('synthetic failure')
        self.assertEqual(error.code, 78)
        self.assertEqual(error.message, 'synthetic failure')
        self.assertEqual(str(error), 'synthetic failure')


class RealNamespaceTests(RuntimePreflightCase):
    def test_host_successful_wrapper_with_unmounted_sibling_is_rejected(self):
        real = self._dir('real')
        write_executable(real / 'codex-real', codex_body())
        wrapper = write_executable(
            self.bin / 'codex', '#!/bin/sh\nexec codex-real "$@"\n')
        host = subprocess.run(
            [str(wrapper), '--version'],
            env={'PATH': os.pathsep.join([str(real), '/usr/bin', '/bin'])},
            text=True, capture_output=True, timeout=15)
        self.assertEqual(host.returncode, 0, host.stderr)
        self.assertIn(CODEX_VERSION, host.stdout)
        factory = ContextFactory()
        self.use_path(self.bin)
        with self.assertRaises(runtime_preflight.RuntimePreflightError) as caught:
            runtime_preflight.select_runtime('codex', 'codex', 'claude', factory)
        self.assertEqual([call[0] for call in factory.calls], [str(wrapper)])
        self.assertIn('127', str(caught.exception))

    def test_default_fallback_after_namespace_unrunnable_wrapper(self):
        real = self._dir('real')
        write_executable(real / 'codex-real', codex_body())
        wrapper = write_executable(
            self.bin / 'codex', '#!/bin/sh\nexec codex-real "$@"\n')
        system = write_executable(self._dir('system') / 'codex', codex_body())
        self.use_path(self.bin, system=(system.parent,))
        factory = ContextFactory()
        selection = runtime_preflight.select_runtime('codex', 'codex', 'claude', factory)
        self.assertEqual(selection.binary, str(system))
        self.assertEqual([call[0] for call in factory.calls], [str(wrapper), str(system)])

    def test_real_nested_bubblewrap_namespace_probe(self):
        from codex_deepseek_team import development, sandbox
        try:
            with mock.patch.dict(os.environ, {'PATH': self.host_path}):
                backend = sandbox.probe_backend()
        except sandbox.SandboxError as error:
            self.skipTest(
                'real nested Bubblewrap namespace unavailable in this worker: ' + str(error))
        real = self._dir('real')
        write_executable(real / 'codex-real', codex_body())
        wrapper = write_executable(
            self.bin / 'codex', f'#!/bin/sh\nexec "{real}/codex-real" "$@"\n')
        host = subprocess.run([str(wrapper), '--version'], env={'PATH': '/usr/bin:/bin'},
                              text=True, capture_output=True, timeout=15)
        self.assertEqual(host.returncode, 0, host.stderr)
        work = self._dir('work')
        (work / '.git').mkdir()
        control = self._dir('control')
        layout = development.layout(backend, work, self.home, control, [str(wrapper)])
        env = {'PATH': '/usr/bin:/bin', 'HOME': str(self.home)}
        smoke = subprocess.run([*layout, '--', '/bin/sh', '-c', 'echo NS_OK'], env=env,
                               text=True, capture_output=True, timeout=30)
        self.assertEqual(smoke.returncode, 0, smoke.stderr)
        self.assertIn('NS_OK', smoke.stdout)
        with self.assertRaises(runtime_preflight.RuntimePreflightError) as caught:
            runtime_preflight.probe_runtime(str(wrapper), 'codex', layout, env)
        self.assertIn('127', str(caught.exception))


if __name__ == '__main__':
    unittest.main()
