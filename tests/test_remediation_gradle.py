"""Provider-free Gradle 8.14.3/OpenJDK 17 integration at the real sandbox boundary.

Set ``DEEPSEEK_TEAM_TEST_GRADLE_HOME`` to an extracted Gradle 8.14.3 distribution
whose ``bin/gradle`` and ``lib/gradle-launcher-8.14.3.jar`` are present. Missing
Gradle, OpenJDK 17, the C3 ``cache_dirs`` interface or a live Bubblewrap namespace
skips with an explicit reason on ordinary developer runs. Set
``DEEPSEEK_TEAM_REQUIRE_GRADLE_TEST=1`` to turn every such skip into a failure.

The build uses only Gradle's built-in Java plugin and committed local Java sources.
It therefore runs with ``--offline`` without Maven, remote repositories or stubbed
Gradle dependencies. The runtime executable below only answers the provider-free
readiness probes used by ``workspace.verify_checks``; no model or relay is started.
"""
from __future__ import annotations

import inspect
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import tempfile
import textwrap
import unittest
from unittest import mock

from codex_deepseek_team import toolchains, worker, workspace

GRADLE_VERSION = '8.14.3'
REQUIRED_ENV = 'DEEPSEEK_TEAM_REQUIRE_GRADLE_TEST'
GRADLE_ENV = 'DEEPSEEK_TEAM_TEST_GRADLE_HOME'
LARGE_CACHE_BYTES = 65 * 1024 * 1024


def _git(root, *args, ok=(0,)):
    result = subprocess.run(['git', '-C', str(root), *args], stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, check=False)
    if result.returncode not in ok:
        raise AssertionError(result.stderr.decode('utf-8', 'replace'))
    return result.stdout.decode('utf-8', 'replace')


def _commit(root, *args):
    _git(root, '-c', 'user.name=GradleFixture', '-c', 'user.email=gradle@example.test',
         *args)


RUNTIME_DRIVER = '''#!/bin/sh
if [ "$1" = "--version" ]; then
  echo "provider-free-runtime-fixture 1.0"
  exit 0
fi
if [ "$1" = "exec" ] && [ "$2" = "--help" ]; then
  echo "--strict-config --sandbox --ephemeral --json --ignore-rules danger-full-access"
  exit 0
fi
echo "runtime fixture only supports provider-free readiness probes" >&2
exit 2
'''


ENVIRONMENT_CHECK = r'''
import os
from pathlib import Path
import sys

home = Path(os.environ["HOME"])
assert home.is_dir(), "isolated HOME is missing"
assert home.name.startswith("verify-"), "HOME is not a per-verification directory"
assert Path(os.environ["CODEX_HOME"]) == home, "runtime config home is not isolated"
for name in ("CLAUDE_CONFIG_DIR", "XDG_CONFIG_HOME", "XDG_STATE_HOME", "TMPDIR"):
    assert name not in os.environ, f"host config variable leaked: {name}"
for hidden in sys.argv[1:]:
    assert not Path(hidden).exists(), f"host path is visible in the sandbox: {hidden}"

work = Path.cwd()
for relative in (".deepseek-tools/jdk", ".deepseek-tools/gradle"):
    root = work / relative
    assert root.is_dir(), f"prepared tool root is missing: {relative}"
    try:
        (root / "deepseek-readonly-probe").write_text("forbidden")
    except OSError:
        pass
    else:
        raise AssertionError(f"prepared tool root is writable: {relative}")

cache = Path(os.environ["GRADLE_USER_HOME"])
assert cache.is_dir(), "declared GRADLE_USER_HOME is missing or not a directory"
probe = cache / "deepseek-writable-probe.txt"
probe.write_text("writable", encoding="utf-8")
assert probe.read_text(encoding="utf-8") == "writable"
print("ISOLATED_HOMES_TOOLS_READONLY_CACHE_WRITABLE_OK")
'''


class GradleRemediationFixture(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='dst-gradle-fixture-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.isolated = {
            'HOME': self.root / 'host-home',
            'CODEX_HOME': self.root / 'host-codex',
            'CLAUDE_CONFIG_DIR': self.root / 'host-claude',
            'XDG_CONFIG_HOME': self.root / 'host-xdg-config',
            'XDG_STATE_HOME': self.root / 'host-xdg-state',
            'GRADLE_USER_HOME': self.root / 'host-gradle-home',
            'TMPDIR': self.root / 'host-tmp',
        }
        for path in self.isolated.values():
            path.mkdir(parents=True)
        self.canaries = {
            self.isolated['HOME'] / 'canary.txt': 'home-canary\n',
            self.isolated['CODEX_HOME'] / 'auth.json': 'codex-canary\n',
            self.isolated['CLAUDE_CONFIG_DIR'] / 'settings.json': 'claude-canary\n',
            self.isolated['XDG_CONFIG_HOME'] / 'settings.toml': 'xdg-config-canary\n',
            self.isolated['XDG_STATE_HOME'] / 'state.json': 'xdg-state-canary\n',
            self.isolated['GRADLE_USER_HOME'] / 'init.gradle': 'gradle-canary\n',
        }
        for path, content in self.canaries.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
        self.addCleanup(self.assert_canaries)

        environment = {name: str(path) for name, path in self.isolated.items()}
        patch = mock.patch.dict(os.environ, environment)
        patch.start()
        self.addCleanup(patch.stop)

        self.state = self.root / 'state'
        self.source = self.root / 'source'
        self.source.mkdir()
        self.write_source()
        _git(self.source, 'init', '-q', '-b', 'main')
        _git(self.source, 'add', '-A')
        _commit(self.source, 'commit', '-qm', 'gradle fixture base')

        self.runtime = self.root / 'runtime-bin' / 'fixture-codex'
        self.runtime.parent.mkdir()
        self.runtime.write_text(RUNTIME_DRIVER)
        self.runtime.chmod(0o700)

    def assert_canaries(self):
        for path, content in self.canaries.items():
            self.assertEqual(path.read_text(), content, path)

    def unavailable(self, reason):
        if os.environ.get(REQUIRED_ENV) == '1':
            self.fail(reason)
        self.skipTest(reason)

    def write_source(self):
        (self.source / '.gitignore').write_text('build/\n.gradle/\n')
        (self.source / 'settings.gradle').write_text("rootProject.name = 'offline-fixture'\n")
        (self.source / 'gradle.properties').write_text(
            'org.gradle.daemon=false\n'
            'org.gradle.caching=false\n'
            'org.gradle.console=plain\n')
        (self.source / 'build.gradle').write_text(textwrap.dedent(f'''
            import java.util.Arrays

            plugins {{
                id 'java'
            }}

            if (gradle.gradleVersion != '{GRADLE_VERSION}') {{
                throw new GradleException("expected Gradle {GRADLE_VERSION}, got " + gradle.gradleVersion)
            }}
            if (JavaVersion.current() != JavaVersion.VERSION_17
                    || !System.getProperty('java.vm.name').contains('OpenJDK')) {{
                throw new GradleException("expected OpenJDK 17, got "
                    + JavaVersion.current() + " / " + System.getProperty('java.vm.name'))
            }}

            java {{
                sourceCompatibility = JavaVersion.VERSION_17
                targetCompatibility = JavaVersion.VERSION_17
            }}

            def generatedSource = layout.projectDirectory.file(
                'src/main/java/example/Generated.java')

            tasks.register('generateSource') {{
                outputs.file(generatedSource)
                doLast {{
                    def target = generatedSource.asFile
                    target.parentFile.mkdirs()
                    target.text = """package example;
            final class Generated {{
                static final String VALUE = "generated-outside-cache";
            }}
            """
                }}
            }}

            tasks.named('compileJava') {{
                dependsOn tasks.named('generateSource')
            }}

            tasks.register('seedCache') {{
                def userHome = providers.environmentVariable('GRADLE_USER_HOME')
                doLast {{
                    def target = new File(userHome.get(),
                        'caches/deepseek-fixture/large-cache.bin')
                    target.parentFile.mkdirs()
                    byte[] block = new byte[1024 * 1024]
                    Arrays.fill(block, (byte) 0x5a)
                    target.withOutputStream {{ output ->
                        for (int index = 0; index < 65; index++) {{
                            output.write(block)
                        }}
                    }}
                }}
            }}

            tasks.register('runFixture', JavaExec) {{
                dependsOn tasks.named('compileJava')
                dependsOn tasks.named('seedCache')
                classpath = sourceSets.main.runtimeClasspath
                mainClass = 'example.Main'
            }}
        ''').lstrip())
        main = self.source / 'src/main/java/example/Main.java'
        main.parent.mkdir(parents=True)
        main.write_text(textwrap.dedent('''
            package example;

            public final class Main {
                public static void main(String[] args) {
                    if (!"generated-outside-cache".equals(Generated.VALUE)) {
                        throw new AssertionError("generated local source was not compiled");
                    }
                    System.out.println("GRADLE_OFFLINE_FIXTURE_OK");
                }
            }
        ''').lstrip())
        helper = self.source / 'tools/verify_environment.py'
        helper.parent.mkdir()
        helper.write_text(ENVIRONMENT_CHECK.lstrip())

    def host_environment(self, java_home, gradle_home):
        version_home = self.root / 'version-home'
        version_home.mkdir(exist_ok=True)
        return {
            'HOME': str(version_home),
            'JAVA_HOME': str(java_home),
            'GRADLE_USER_HOME': str(version_home / 'gradle'),
            'TMPDIR': str(self.isolated['TMPDIR']),
            'PATH': f'{java_home}/bin:/usr/bin:/bin',
            'LANG': 'C.UTF-8',
        }

    def java_home(self):
        candidates = []
        explicit = os.environ.get('JAVA_HOME')
        if explicit:
            candidates.append(Path(explicit))
        candidates.extend((
            Path('/usr/lib/jvm/java-17-openjdk-amd64'),
            Path('/usr/lib/jvm/java-17-openjdk'),
            Path('/usr/lib/jvm/temurin-17-jdk-amd64'),
        ))
        try:
            candidates.extend(sorted(Path('/usr/lib/jvm').iterdir()))
        except OSError:
            pass
        javac = shutil.which('javac')
        if javac:
            candidates.append(Path(javac).resolve().parent.parent)
        seen = set()
        for candidate in candidates:
            try:
                home = candidate.resolve()
            except OSError:
                continue
            if home in seen or not (home / 'bin/java').is_file() or not (home / 'bin/javac').is_file():
                continue
            seen.add(home)
            if self.java_major(home) == 17:
                return home
        self.unavailable('OpenJDK 17 with javac is unavailable for the Gradle integration fixture.')

    def java_major(self, home):
        release = home / 'release'
        try:
            text = release.read_text(encoding='utf-8', errors='replace')
        except OSError:
            text = ''
        match = re.search(r'^JAVA_VERSION="?([^"\r\n]+)', text, re.MULTILINE)
        if match is None:
            result = subprocess.run([str(home / 'bin/java'), '-version'],
                                    env=self.host_environment(home, home),
                                    text=True, capture_output=True, timeout=15, check=False)
            match = re.search(r'version "([^"]+)"', result.stdout + result.stderr)
        if match is None:
            return None
        major = re.match(r'([0-9]+)', match.group(1))
        return int(major.group(1)) if major else None

    def gradle_distribution(self, java_home):
        supplied = os.environ.get(GRADLE_ENV)
        roots = []
        if supplied:
            value = Path(supplied).expanduser()
            if not value.is_absolute():
                self.unavailable(f'{GRADLE_ENV} must name an absolute Gradle distribution directory.')
            roots.extend((value, value / f'gradle-{GRADLE_VERSION}'))
        else:
            found = shutil.which('gradle')
            if found:
                roots.append(Path(found).resolve().parent.parent)
        for candidate in roots:
            try:
                root = candidate.resolve()
            except OSError:
                continue
            launcher = root / 'bin/gradle'
            marker = root / 'lib' / f'gradle-launcher-{GRADLE_VERSION}.jar'
            if not launcher.is_file() or not os.access(launcher, os.X_OK) or not marker.is_file():
                continue
            result = subprocess.run([str(launcher), '--version'],
                                    env=self.host_environment(java_home, root),
                                    text=True, capture_output=True, timeout=30, check=False)
            lines = {line.strip() for line in result.stdout.splitlines()}
            if result.returncode == 0 and f'Gradle {GRADLE_VERSION}' in lines:
                return root
        if supplied:
            self.unavailable(
                f'{GRADLE_ENV} does not contain a verified Gradle {GRADLE_VERSION} distribution.')
        self.unavailable(
            f'Gradle {GRADLE_VERSION} is unavailable; set {GRADLE_ENV} to an extracted distribution.')

    def require_cache_dirs_api(self):
        if 'cache_dirs' not in inspect.signature(toolchains.prepare).parameters:
            self.unavailable(
                'C3 cache_dirs preparation API is unavailable on this base; '
                'the Gradle fixture runs after C3 integration.')

    def runtime_ready(self, copy):
        with copy.lock():
            copy.metadata.update(runtime='codex', runtime_binary=str(self.runtime),
                                 runtime_explicit=True, effective_access='full-access')
            copy.save()

    def checks(self):
        hidden = (self.source, self.isolated['HOME'], self.isolated['CODEX_HOME'],
                  self.isolated['CLAUDE_CONFIG_DIR'], self.isolated['XDG_CONFIG_HOME'],
                  self.isolated['XDG_STATE_HOME'], self.isolated['GRADLE_USER_HOME'])
        isolation = 'python3 tools/verify_environment.py ' + ' '.join(
            shlex.quote(str(path)) for path in hidden)
        gradle = ('gradle --offline --no-daemon --no-build-cache --console=plain '
                  '--stacktrace runFixture')
        return [isolation, gradle]

    def failure_text(self, copy, result):
        chunks = []
        for row in result.get('checks', []):
            for name in ('stdout_path', 'stderr_path'):
                if row.get(name):
                    path = copy.directory / 'diagnostics' / row[name]
                    try:
                        chunks.append(path.read_text(encoding='utf-8', errors='replace'))
                    except OSError:
                        pass
        return '\n'.join(chunks) or 'no check output was retained'

    def run_verify(self, copy, commands):
        with mock.patch.object(worker, 'load_api_key', side_effect=AssertionError(
                'provider credential read')), \
                mock.patch.object(worker, 'execute', side_effect=AssertionError(
                    'model execution attempted')), \
                mock.patch('codex_deepseek_team.relay.ProviderRelay', side_effect=AssertionError(
                    'provider relay started')):
            result = workspace.verify_checks(copy, commands, timeout=300)
        if result['status'] != 'passed':
            self.fail('Gradle verification failed:\n' + self.failure_text(copy, result))
        self.assertEqual([row['command'] for row in result['checks']], commands)
        self.assertTrue(all(row['exit_code'] == 0 for row in result['checks']))
        output = self.failure_text(copy, result)
        self.assertIn('ISOLATED_HOMES_TOOLS_READONLY_CACHE_WRITABLE_OK', output)
        self.assertIn('GRADLE_OFFLINE_FIXTURE_OK', output)
        return result

    def assert_cached_and_generated(self, copy):
        large = copy.path / 'gradle-home/caches/deepseek-fixture/large-cache.bin'
        self.assertTrue(large.is_file(), 'Gradle did not generate the large cache sibling')
        self.assertGreaterEqual(large.stat().st_size, LARGE_CACHE_BYTES)
        self.assertTrue((copy.path / 'gradle-home/deepseek-writable-probe.txt').is_file())

        changed, ignored = copy.changes()
        self.assertIn('src/main/java/example/Generated.java', changed)
        self.assertFalse([name for name in changed if name == 'gradle-home'
                          or name.startswith('gradle-home/')], changed)
        self.assertGreater(ignored, 0, 'the cache exclusion was not reported as ignored')
        workspace.git(copy.path, 'check-ignore', '-q',
                      'gradle-home/caches/deepseek-fixture/large-cache.bin')
        diff = copy.diff_bytes()
        self.assertIn(b'src/main/java/example/Generated.java', diff)
        self.assertIn(b'generated-outside-cache', diff)
        self.assertNotIn(b'gradle-home/', diff)

    def test_real_offline_gradle_cache_and_saved_recipe_replay(self):
        try:
            worker.resolve_os_sandbox('required')
        except worker.WorkerError as error:
            self.unavailable('Required live Bubblewrap namespace is unavailable: ' + str(error))

        java_home = self.java_home()
        gradle_home = self.gradle_distribution(java_home)
        self.require_cache_dirs_api()

        copy = workspace.create(self.source, self.state)
        prepared = toolchains.prepare(
            copy,
            tools={'gradle': gradle_home, 'jdk': java_home},
            cache_dirs=['gradle-home'],
            environment={
                'JAVA_HOME': '{workspace}/.deepseek-tools/jdk',
                'PATH': ('{workspace}/.deepseek-tools/jdk/bin:'
                         '{workspace}/.deepseek-tools/gradle/bin'),
                'GRADLE_USER_HOME': '{workspace}/gradle-home',
            },
            probes=['gradle --version', 'java -version', 'javac -version'],
            save_project=True)
        self.assertTrue(prepared['recipe_saved'])
        self.assertEqual(prepared['environment']['GRADLE_USER_HOME'],
                         '{workspace}/gradle-home')
        self.assertEqual(copy.metadata['toolchain']['cache_dirs'], ['gradle-home'])
        self.assertIn('JAVA_VERSION="17', (copy.path / '.deepseek-tools/jdk/release').read_text())
        self.runtime_ready(copy)

        commands = self.checks()
        self.run_verify(copy, commands)
        self.assert_cached_and_generated(copy)

        fresh = workspace.create(self.source, self.state)
        self.assertIsNone(fresh.metadata.get('toolchain'))
        self.assertFalse((fresh.path / 'gradle-home').exists())
        self.runtime_ready(fresh)
        self.run_verify(fresh, self.checks())
        self.assertEqual(fresh.metadata['toolchain']['cache_dirs'], ['gradle-home'])
        self.assertEqual(fresh.metadata['toolchain']['environment']['GRADLE_USER_HOME'],
                         '{workspace}/gradle-home')
        self.assert_cached_and_generated(fresh)


if __name__ == '__main__':
    unittest.main()
