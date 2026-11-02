"""Doctor and allocation readiness for the shared storage-budget pool."""
import contextlib
import io
import os
from pathlib import Path
import stat
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from codex_deepseek_team import (
    doctor, execution_allocation, sandbox, storage_budget, worker)


class DoctorBudgetFixture(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='dst-doctor-budget-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.home = self.root / 'home'
        self.codex = self.root / 'codex'
        self.claude = self.root / 'claude'
        self.xdg = self.root / 'config'
        self.xdg_state = self.root / 'state-home'
        self.state = self.root / 'work-state'
        for path in (self.home, self.codex, self.claude, self.xdg,
                     self.xdg_state):
            path.mkdir(mode=0o700)
        self.xdg.chmod(0o755)
        self.state.mkdir(mode=0o700)
        self.credential_canary = 'synthetic-doctor-budget-credential'
        (self.codex / 'auth.json').write_text('{"synthetic":"canary"}')
        (self.claude / 'settings.json').write_text('{"synthetic":"canary"}')
        (self.xdg / 'canary.conf').write_text('preserve external config')
        environment = mock.patch.dict(os.environ, {
            'HOME': str(self.home),
            'CODEX_HOME': str(self.codex),
            'CLAUDE_CONFIG_DIR': str(self.claude),
            'XDG_CONFIG_HOME': str(self.xdg),
            'XDG_STATE_HOME': str(self.xdg_state),
            'DEEPSEEK_TEAM_STATE_DIR': str(self.state),
            'DEEPSEEK_API_KEY': self.credential_canary,
        })
        environment.start()
        self.addCleanup(environment.stop)
        for name in ('DEEPSEEK_TEAM_DISABLED', 'CODEX_DEEPSEEK_DISABLED'):
            os.environ.pop(name, None)
        self.previous = Path.cwd()
        os.chdir(self.root)
        self.addCleanup(os.chdir, self.previous)

    def pool_path(self):
        return storage_budget.shared_pool_dir(self.state)

    def external_snapshot(self):
        snapshot = {}
        for base in (self.home, self.codex, self.claude, self.xdg,
                     self.xdg_state):
            if not base.exists():
                continue
            paths = [base, *sorted(base.rglob('*'))]
            for path in paths:
                info = path.lstat()
                if stat.S_ISLNK(info.st_mode):
                    value = ('link', os.readlink(path))
                elif stat.S_ISREG(info.st_mode):
                    value = ('file', path.read_bytes())
                elif stat.S_ISDIR(info.st_mode):
                    value = ('directory',)
                else:
                    value = ('other', stat.S_IFMT(info.st_mode))
                snapshot[str(path)] = (
                    stat.S_IMODE(info.st_mode), info.st_uid, info.st_gid, value)
        return snapshot

    def run_doctor(self, *extra):
        backend = sandbox.SandboxBackend(
            ('/usr/bin/bwrap',), '/usr/bin/bwrap', 'direct')
        out, err = io.StringIO(), io.StringIO()
        before = self.external_snapshot()
        with mock.patch.object(
                doctor.worker, 'resolve_os_sandbox', return_value=(sandbox, backend)), \
             mock.patch.object(doctor, 'selected_runtimes', return_value=('codex',)), \
             mock.patch.object(doctor, 'check_policy_runtime'), \
             mock.patch.object(
                 doctor, 'coordination_status',
                 return_value=('installed', 'attached', 'fixture runtime only')), \
             mock.patch.object(
                 worker, 'load_api_key',
                 side_effect=AssertionError('offline doctor read a credential')), \
             mock.patch(
                 'urllib.request.build_opener',
                 side_effect=AssertionError('offline doctor used the network')), \
             contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = doctor.main([
                '--offline', '--runtime', 'codex', '--access', 'read-only',
                *extra])
        self.assertEqual(self.external_snapshot(), before)
        output = out.getvalue() + err.getvalue()
        self.assertNotIn(self.credential_canary, output)
        return code, output

    def make_pool(self):
        pool = self.pool_path()
        pool.mkdir(mode=0o700, parents=True)
        pool.chmod(0o700)
        return pool


class PublicPoolReadinessTests(DoctorBudgetFixture):
    def test_missing_pool_is_reported_safe_to_create_without_side_effects(self):
        pool = self.pool_path()
        self.assertFalse(pool.exists())
        before = self.external_snapshot()
        report = storage_budget.check_pool_readiness(self.state)
        self.assertEqual(report['path'], pool)
        self.assertFalse(report['exists'])
        self.assertTrue(report['safe_to_create'])
        self.assertFalse(pool.exists())
        self.assertEqual(self.external_snapshot(), before)

    def test_missing_pool_below_nonwritable_owned_ancestor_fails_closed(self):
        self.xdg.chmod(0o555)
        pool = self.pool_path()
        before = self.external_snapshot()
        with self.assertRaises(storage_budget.BudgetError) as caught:
            storage_budget.check_pool_readiness(self.state)
        message = str(caught.exception)
        self.assertEqual(caught.exception.code, 78)
        self.assertIn('nearest existing ancestor', message)
        self.assertIn(str(self.xdg), message)
        self.assertIn(str(pool), message)
        self.assertIn('XDG_CONFIG_HOME', message)
        self.assertIn('current-user-owned', message)
        self.assertNotIn('chmod', message)
        self.assertNotIn('--state-dir', message)
        self.assertFalse(pool.exists())
        self.assertEqual(self.external_snapshot(), before)

    def test_missing_pool_requires_actual_effective_access(self):
        pool = self.pool_path()
        before = self.external_snapshot()
        denied = mock.patch.object(
            storage_budget.os, 'access', return_value=False)
        with denied:
            with self.assertRaises(storage_budget.BudgetError) as caught:
                storage_budget.check_pool_readiness(self.state)
        message = str(caught.exception)
        self.assertEqual(caught.exception.code, 78)
        self.assertIn('nearest existing ancestor', message)
        self.assertIn(str(self.xdg), message)
        self.assertIn(str(pool), message)
        self.assertIn('XDG_CONFIG_HOME', message)
        self.assertNotIn('chmod', message)
        self.assertFalse(pool.exists())
        self.assertEqual(self.external_snapshot(), before)

    def test_existing_pool_on_readonly_filesystem_fails_closed(self):
        pool = self.make_pool()
        before = self.external_snapshot()
        readonly = SimpleNamespace(f_flag=os.ST_RDONLY)
        with mock.patch.object(
                storage_budget.os, 'fstatvfs', return_value=readonly):
            with self.assertRaises(storage_budget.BudgetError) as caught:
                storage_budget.check_pool_readiness(self.state)
        message = str(caught.exception)
        self.assertEqual(caught.exception.code, 78)
        self.assertIn(str(pool), message)
        self.assertIn('read-only', message)
        self.assertIn('XDG_CONFIG_HOME', message)
        self.assertIn('current-user-owned', message)
        self.assertNotIn('chmod', message)
        self.assertFalse((pool / 'budget.lock').exists())
        self.assertEqual(self.external_snapshot(), before)

    def test_missing_pool_on_readonly_filesystem_fails_closed(self):
        pool = self.pool_path()
        before = self.external_snapshot()
        readonly = SimpleNamespace(f_flag=os.ST_RDONLY)
        with mock.patch.object(
                storage_budget.os, 'fstatvfs', return_value=readonly):
            with self.assertRaises(storage_budget.BudgetError) as caught:
                storage_budget.check_pool_readiness(self.state)
        message = str(caught.exception)
        self.assertEqual(caught.exception.code, 78)
        self.assertIn('nearest existing ancestor', message)
        self.assertIn(str(self.xdg), message)
        self.assertIn(str(pool), message)
        self.assertIn('read-only', message)
        self.assertIn('XDG_CONFIG_HOME', message)
        self.assertIn('current-user-owned', message)
        self.assertNotIn('chmod', message)
        self.assertFalse(pool.exists())
        self.assertEqual(self.external_snapshot(), before)

    def test_proc_xdg_pool_is_not_advertised_create_ready(self):
        proc = Path('/proc')
        if not proc.is_dir():
            self.skipTest('/proc is unavailable')
        with mock.patch.dict(
                os.environ, {'XDG_CONFIG_HOME': str(proc)}):
            pool = self.pool_path()
            with self.assertRaises(storage_budget.BudgetError) as caught:
                storage_budget.check_pool_readiness(self.state)
        message = str(caught.exception)
        self.assertEqual(caught.exception.code, 78)
        self.assertIn('nearest existing ancestor', message)
        self.assertIn(str(proc), message)
        self.assertIn(str(pool), message)
        self.assertIn('XDG_CONFIG_HOME', message)
        self.assertIn('current-user-owned', message)
        self.assertNotIn('chmod', message)
        self.assertFalse((proc / 'deepseek-team').exists())

    def test_existing_private_pool_passes_the_check_only_validation(self):
        pool = self.make_pool()
        report = storage_budget.check_pool_readiness(self.state)
        self.assertEqual(report['path'], pool)
        self.assertTrue(report['exists'])
        self.assertFalse(report['safe_to_create'])

    def test_other_writable_ancestor_rejects_with_owned_chmod_guidance(self):
        self.xdg.chmod(0o757)
        pool = self.pool_path()
        with self.assertRaises(storage_budget.BudgetError) as caught:
            storage_budget.check_pool_readiness(self.state)
        self.assertEqual(caught.exception.code, 78)
        self.assertIn(str(self.xdg), str(caught.exception))
        self.assertIn(str(pool), str(caught.exception))
        self.assertIn('chmod go-w', str(caught.exception))
        self.assertNotIn('--state-dir', str(caught.exception))
        self.assertFalse(pool.exists())
        self.assertEqual(self.xdg.stat().st_mode & 0o777, 0o757)

    def test_final_pool_mode_is_checked_without_chmod(self):
        pool = self.make_pool()
        pool.chmod(0o775)
        with self.assertRaises(storage_budget.BudgetError) as caught:
            storage_budget.check_pool_readiness(self.state)
        self.assertEqual(caught.exception.code, 78)
        self.assertIn(str(pool), str(caught.exception))
        self.assertIn('chmod 700', str(caught.exception))
        self.assertNotIn('--state-dir', str(caught.exception))
        self.assertEqual(pool.stat().st_mode & 0o777, 0o775)

    def test_final_pool_symlink_is_not_followed_or_chmod_suggested(self):
        target = self.root / 'pool-target'
        target.mkdir(mode=0o700)
        (target / 'preserve').write_text('keep')
        pool = self.pool_path()
        pool.parent.mkdir(mode=0o700, parents=True)
        pool.symlink_to(target, target_is_directory=True)
        with self.assertRaises(storage_budget.BudgetError) as caught:
            storage_budget.check_pool_readiness(self.state)
        self.assertEqual(caught.exception.code, 78)
        self.assertIn(str(pool), str(caught.exception))
        self.assertIn('symbolic link', str(caught.exception))
        self.assertNotIn('chmod', str(caught.exception))
        self.assertTrue(pool.is_symlink())
        self.assertEqual(sorted(path.name for path in target.iterdir()),
                         ['preserve'])
        self.assertEqual((target / 'preserve').read_text(), 'keep')

    def test_symlinked_pool_ancestor_is_not_followed_or_chmod_suggested(self):
        target = self.root / 'ancestor-target'
        target.mkdir(mode=0o700)
        (self.xdg / 'deepseek-team').symlink_to(
            target, target_is_directory=True)
        pool = self.pool_path()
        with self.assertRaises(storage_budget.BudgetError) as caught:
            storage_budget.check_pool_readiness(self.state)
        self.assertEqual(caught.exception.code, 78)
        self.assertIn(str(self.xdg / 'deepseek-team'), str(caught.exception))
        self.assertIn(str(pool), str(caught.exception))
        self.assertIn('symbolic link', str(caught.exception))
        self.assertNotIn('chmod', str(caught.exception))
        self.assertEqual(list(target.iterdir()), [])

    def test_pool_ancestor_identity_change_fails_closed(self):
        pool = self.pool_path()
        before = self.external_snapshot()
        original = storage_budget._open_measured_directory

        def changed_identity(parent, name, path, *,
                             require_current_owner=False):
            child, info = original(
                parent, name, path,
                require_current_owner=require_current_owner)
            if path != self.xdg:
                return child, info
            return child, SimpleNamespace(
                st_dev=info.st_dev, st_ino=info.st_ino + 1)

        with mock.patch.object(
                storage_budget, '_open_measured_directory',
                side_effect=changed_identity):
            with self.assertRaises(storage_budget.BudgetError) as caught:
                storage_budget.check_pool_readiness(self.state)
        message = str(caught.exception)
        self.assertEqual(caught.exception.code, 78)
        self.assertIn(str(self.xdg), message)
        self.assertIn(str(pool), message)
        self.assertIn('changed during its safety check', message)
        self.assertNotIn('chmod', message)
        self.assertFalse(pool.exists())
        self.assertEqual(self.external_snapshot(), before)

    def test_root_owned_sticky_ancestor_remains_acceptable(self):
        if os.geteuid() == 0:
            root = self.root / 'root-sticky'
            root.mkdir(mode=0o700)
            root.chmod(0o1777)
            config = root / 'config'
            config.mkdir(mode=0o755)
            config.chmod(0o755)
            state = root / 'state'
            state.mkdir(mode=0o700)
            with mock.patch.dict(os.environ, {'XDG_CONFIG_HOME': str(config)}):
                report = storage_budget.check_pool_readiness(state)
        else:
            sticky = Path('/tmp')
            if (not sticky.is_dir() or sticky.stat().st_uid != 0
                    or not sticky.stat().st_mode & stat.S_ISVTX):
                self.skipTest('no root-owned sticky test directory is available')
            with tempfile.TemporaryDirectory(
                    prefix='dst-doctor-sticky-', dir=sticky) as directory:
                root = Path(directory)
                config = root / 'config'
                config.mkdir(mode=0o755)
                config.chmod(0o755)
                state = root / 'state'
                state.mkdir(mode=0o700)
                with mock.patch.dict(
                        os.environ, {'XDG_CONFIG_HOME': str(config)}):
                    report = storage_budget.check_pool_readiness(state)
        self.assertFalse(report['exists'])
        self.assertFalse(report['path'].exists())

    def test_foreign_final_pool_rejects_without_chmod_guidance(self):
        pool = self.make_pool()
        foreign_uid = 424242
        real_stat = storage_budget.os.stat

        def foreign_stat(path, *args, **kwargs):
            info = real_stat(path, *args, **kwargs)
            if (os.fspath(path) in (str(pool), pool.name)
                    and kwargs.get('follow_symlinks') is False):
                return SimpleNamespace(
                    st_mode=info.st_mode, st_uid=foreign_uid,
                    st_dev=info.st_dev, st_ino=info.st_ino)
            return info

        with mock.patch.object(
                storage_budget.os, 'stat', side_effect=foreign_stat):
            with self.assertRaises(storage_budget.BudgetError) as caught:
                storage_budget.check_pool_readiness(self.state)
        self.assertEqual(caught.exception.code, 78)
        self.assertIn(str(pool), str(caught.exception))
        self.assertIn('foreign', str(caught.exception).lower())
        self.assertNotIn('chmod', str(caught.exception))
        self.assertEqual(pool.stat().st_uid, os.geteuid())

    def test_foreign_pool_ancestor_rejects_without_chmod_guidance(self):
        ancestor = self.xdg / 'deepseek-team'
        ancestor.mkdir(mode=0o700)
        foreign_uid = 424242
        real_stat = storage_budget.os.stat

        def foreign_stat(path, *args, **kwargs):
            info = real_stat(path, *args, **kwargs)
            if (os.fspath(path) == ancestor.name
                    and kwargs.get('dir_fd') is not None):
                return SimpleNamespace(
                    st_mode=info.st_mode, st_uid=foreign_uid,
                    st_dev=info.st_dev, st_ino=info.st_ino)
            return info

        with mock.patch.object(
                storage_budget.os, 'stat', side_effect=foreign_stat):
            with self.assertRaises(storage_budget.BudgetError) as caught:
                storage_budget.check_pool_readiness(self.state)
        self.assertEqual(caught.exception.code, 78)
        self.assertIn(str(ancestor), str(caught.exception))
        self.assertIn('foreign', str(caught.exception).lower())
        self.assertNotIn('chmod', str(caught.exception))
        self.assertEqual(ancestor.stat().st_uid, os.geteuid())


class DoctorPoolGateTests(DoctorBudgetFixture):
    def test_unsafe_xdg_pool_fails_doctor_before_success_lines(self):
        self.xdg.chmod(0o775)
        pool = self.pool_path()
        code, output = self.run_doctor()
        self.assertEqual(code, 78, output)
        self.assertIn('Storage budget pool: FAIL', output)
        self.assertIn(str(self.xdg), output)
        self.assertIn(str(pool), output)
        self.assertIn('chmod go-w', output)
        self.assertNotIn('Worker storage: PASS', output)
        self.assertNotIn('Local runtime checks: PASS', output)
        self.assertFalse(pool.exists())
        self.assertEqual(self.xdg.stat().st_mode & 0o777, 0o775)

    def test_readable_nonwritable_xdg_pool_is_safe_to_create(self):
        self.xdg.chmod(0o755)
        pool = self.pool_path()
        before = self.external_snapshot()
        code, output = self.run_doctor()
        self.assertEqual(code, 0, output)
        self.assertIn('Worker storage: PASS', output)
        self.assertIn('Storage budget pool: PASS', output)
        self.assertIn(str(pool), output)
        self.assertIn('safe to create', output)
        self.assertIn('Local runtime checks: PASS', output)
        self.assertFalse(pool.exists())
        self.assertEqual(self.external_snapshot(), before)

    def test_existing_private_pool_passes_doctor(self):
        pool = self.make_pool()
        code, output = self.run_doctor()
        self.assertEqual(code, 0, output)
        self.assertIn('Storage budget pool: PASS', output)
        self.assertIn(str(pool), output)

    def test_public_final_pool_fails_doctor_without_repairing_mode(self):
        pool = self.make_pool()
        pool.chmod(0o775)
        code, output = self.run_doctor()
        self.assertEqual(code, 78, output)
        self.assertIn('Storage budget pool: FAIL', output)
        self.assertIn('chmod 700', output)
        self.assertNotIn('Local runtime checks: PASS', output)
        self.assertEqual(pool.stat().st_mode & 0o777, 0o775)

    def test_explicit_state_dir_does_not_claim_to_fix_xdg_pool_failure(self):
        self.xdg.chmod(0o775)
        selected = self.root / 'explicit-state'
        code, output = self.run_doctor('--state-dir', str(selected))
        self.assertEqual(code, 78, output)
        self.assertIn('Storage budget pool: FAIL', output)
        self.assertNotIn('--state-dir', output)
        self.assertEqual(selected.stat().st_mode & 0o777, 0o700)
        self.assertFalse(self.pool_path().exists())

    def test_actual_reservation_reports_pool_error_and_exits_78(self):
        self.xdg.chmod(0o775)
        with self.assertRaises(storage_budget.BudgetError) as caught:
            with execution_allocation.reserve(
                    self.state, disk_bytes=0, inodes=0):
                self.fail('unsafe pool unexpectedly admitted a reservation')
        self.assertEqual(caught.exception.code, 78)
        self.assertIn(str(self.xdg), str(caught.exception))
        self.assertIn('chmod go-w', str(caught.exception))
        self.assertNotIn('--state-dir', str(caught.exception))
        self.assertFalse(self.pool_path().exists())

    def test_actual_reservation_rejects_unsafe_final_pool_without_chmod(self):
        pool = self.make_pool()
        pool.chmod(0o775)
        with self.assertRaises(storage_budget.BudgetError) as caught:
            with execution_allocation.reserve(
                    self.state, disk_bytes=0, inodes=0):
                self.fail('unsafe final pool unexpectedly admitted a reservation')
        self.assertEqual(caught.exception.code, 78)
        self.assertIn(str(pool), str(caught.exception))
        self.assertIn('chmod 700', str(caught.exception))
        self.assertNotIn('--state-dir', str(caught.exception))
        self.assertEqual(pool.stat().st_mode & 0o777, 0o775)

    def test_actual_reservation_checks_existing_pool_create_readiness(self):
        pool = self.make_pool()
        before = self.external_snapshot()
        denied = mock.patch.object(
            storage_budget.os, 'access', return_value=False)
        with denied:
            with self.assertRaises(storage_budget.BudgetError) as caught:
                with execution_allocation.reserve(
                        self.state, disk_bytes=0, inodes=0):
                    self.fail('unwritable pool unexpectedly admitted a reservation')
        message = str(caught.exception)
        self.assertEqual(caught.exception.code, 78)
        self.assertIn(str(pool), message)
        self.assertIn('lock or reservation', message)
        self.assertIn('XDG_CONFIG_HOME', message)
        self.assertIn('current-user-owned', message)
        self.assertNotIn('chmod', message)
        self.assertFalse((pool / 'budget.lock').exists())
        self.assertEqual(self.external_snapshot(), before)

    def test_proc_xdg_pool_fails_doctor_before_success_lines(self):
        proc = Path('/proc')
        if not proc.is_dir():
            self.skipTest('/proc is unavailable')
        with mock.patch.dict(
                os.environ, {'XDG_CONFIG_HOME': str(proc)}):
            code, output = self.run_doctor()
        self.assertEqual(code, 78, output)
        self.assertIn('Storage budget pool: FAIL', output)
        self.assertIn(str(proc), output)
        self.assertIn('nearest existing ancestor', output)
        self.assertIn('XDG_CONFIG_HOME', output)
        self.assertNotIn('Storage budget pool: PASS', output)
        self.assertNotIn('Worker storage: PASS', output)
        self.assertNotIn('Local runtime checks: PASS', output)
        self.assertNotIn('chmod', output)
        self.assertFalse((proc / 'deepseek-team').exists())

    def test_actual_reservation_rejects_proc_pool_with_exact_ancestor(self):
        proc = Path('/proc')
        if not proc.is_dir():
            self.skipTest('/proc is unavailable')
        with mock.patch.dict(
                os.environ, {'XDG_CONFIG_HOME': str(proc)}):
            with self.assertRaises(storage_budget.BudgetError) as caught:
                with execution_allocation.reserve(
                        self.state, disk_bytes=0, inodes=0):
                    self.fail('read-only /proc pool unexpectedly admitted a reservation')
        message = str(caught.exception)
        self.assertEqual(caught.exception.code, 78)
        self.assertIn('nearest existing ancestor', message)
        self.assertIn(str(proc), message)
        self.assertIn('XDG_CONFIG_HOME', message)
        self.assertNotIn('chmod', message)
        self.assertFalse((proc / 'deepseek-team').exists())


if __name__ == '__main__':
    unittest.main()
