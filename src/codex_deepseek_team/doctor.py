"""Generic local diagnostics and optional synthetic DeepSeek smoke tests."""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('worker', HERE / 'worker.py')
worker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(worker)


def call(task, cwd=None, runtime='codex', os_sandbox='required'):
    return subprocess.run([sys.executable, str(HERE / 'worker.py'), '--runtime', runtime,
                           '--os-sandbox', os_sandbox],
                          input=task, text=True, capture_output=True, cwd=cwd)


def repository_fingerprint(root=None):
    """Detect changes even when a file was already dirty; print no file contents."""
    root = Path(root) if root is not None else Path(subprocess.check_output(['git', 'rev-parse', '--show-toplevel'], text=True).strip())
    names = subprocess.check_output(['git', '-C', str(root), 'ls-files', '-z', '--cached', '--others', '--exclude-standard'])
    digest = hashlib.sha256(subprocess.check_output(['git', '-C', str(root), 'status', '--porcelain=v1', '-uall']))
    for name in sorted(set(names.split(b'\0')) - {b''}):
        path = root / os.fsdecode(name)
        digest.update(name + b'\0')
        if path.is_symlink():
            digest.update(os.fsencode(os.readlink(path)))
        elif path.is_file():
            with path.open('rb') as source:
                for chunk in iter(lambda: source.read(1024 * 1024), b''):
                    digest.update(chunk)
    return digest.digest()


def probe_error(error, elapsed):
    """Classify failures without formatting exceptions, headers or response bodies."""
    cause = getattr(error, 'reason', error)
    category = type(cause).__name__
    if isinstance(error, json.JSONDecodeError):
        category = 'InvalidJSONResponse'
    return f'API probe: FAIL — {category}, elapsed={elapsed:.1f}s; credentials and response body omitted.'


def stream_model(lines):
    """Read only response metadata; never retain tokens/reasoning or await full generation."""
    for line in lines:
        if not line.startswith(b'data:'):
            continue
        event = json.loads(line[5:])
        response = event.get('response', {})
        actual = response.get('model')
        if isinstance(actual, str) and actual:
            return actual
    raise ValueError('Stream did not expose model metadata')


def api_probe():
    """Runs in a separate, bounded process. Never writes credentials or raw responses."""
    if os.environ.get('DEEPSEEK_TEAM_DISABLED') == '1' or os.environ.get('CODEX_DEEPSEEK_DISABLED') == '1':
        print('API probe: DISABLED — remove the DeepSeek delegation disable switch to run live tests.')
        return 69
    key = worker.load_api_key()
    if not key.strip():
        print('API probe: BLOCKED — DEEPSEEK_API_KEY and saved credential are absent or empty.')
        return 78
    started = time.monotonic()
    try:
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *_args, **_kwargs):
                return None
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        headers = {'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json'}
        print('API probe: checking /models (socket timeout 20s)...', flush=True)
        request = urllib.request.Request('https://api.deepseek.com/models', headers=headers)
        with opener.open(request, timeout=20) as response:
            models = [item.get('id') for item in json.load(response).get('data', [])]
        available = [model for model in models if isinstance(model, str) and re.fullmatch(r'[a-zA-Z0-9._-]{1,80}', model)]
        if worker.MODEL not in available:
            print('API probe: requested model is absent from /models; available IDs: ' + worker.redact(', '.join(available), key))
            return 78
        print('API probe: requested model is listed; opening Responses stream...', flush=True)
        request = urllib.request.Request('https://api.deepseek.com/responses',
            data=json.dumps({'model': worker.MODEL, 'input': 'Return exactly DEEPSEEK_WORKER_OK.',
                             'reasoning': {'effort': 'none'},
                             'stream': True, 'store': False, 'max_output_tokens': 1024}).encode(), headers=headers)
        with opener.open(request, timeout=60) as response:
            actual_model = stream_model(response)
        if actual_model != worker.MODEL:
            print('API probe: returned model metadata differs from the requested model.')
            return 78
        print('API probe model metadata:', worker.redact(actual_model, key), flush=True)
        return 0
    except urllib.error.HTTPError as error:
        print(f'API probe: FAIL — HTTP {error.code}; response body omitted to protect credentials.')
        return 1
    except (OSError, ValueError) as error:
        print(probe_error(error, time.monotonic() - started))
        return 1


def selected_runtimes(value):
    if value == 'both':
        return ('codex', 'claude')
    if value in ('codex', 'claude'):
        return (value,)
    found = tuple(name for name in ('codex', 'claude') if shutil.which(name))
    if found:
        return found
    raise worker.WorkerError(78, 'Neither Codex nor Claude Code is available for --runtime auto.')


def check_runtime(runtime):
    if runtime == 'codex':
        config = worker.provider_config(worker.codex_home())
        version = subprocess.check_output(['codex', '--version'], text=True).strip()
        help_text = subprocess.check_output(['codex', 'exec', '--help'], text=True)
        required = ['--strict-config', '--ephemeral', '--json', '--sandbox', '--ignore-rules']
        if not all(flag in help_text for flag in required):
            raise worker.WorkerError(78, 'Codex CLI lacks required options; update Codex before using workers.')
        print(version)
        print('Configured coordinator:', config.get('model', '(Codex default)'), '/', config.get('model_provider', 'openai'))
        return
    version = subprocess.check_output(['claude', '--version'], text=True).strip()
    help_text = subprocess.check_output(['claude', '--help'], text=True)
    required = ['--bare', '--output-format', '--no-session-persistence', '--permission-mode',
                '--tools', '--allowedTools', '--disallowedTools']
    if not all(flag in help_text for flag in required):
        raise worker.WorkerError(78, 'Claude Code lacks required non-interactive isolation options; update Claude Code before using workers.')
    print(version)
    print('Claude Code worker routing: isolated DeepSeek Anthropic-compatible child; parent Claude auth/config untouched.')


def resolve_policy(*, delegation_level=None, access=None, root=None):
    """Use the same layered policy resolver as config, workers and managed instructions."""
    from codex_deepseek_team import settings
    try:
        return settings.resolve(Path.cwd() if root is None else Path(root),
                                delegation_level=delegation_level, access=access)
    except settings.SettingsError as error:
        raise worker.WorkerError(78, str(error)) from None


def check_policy_runtime(runtime, policy):
    """Check the runtime surface that the effective access will actually use."""
    if policy.effective_access != 'full-access':
        return check_runtime(runtime)
    from codex_deepseek_team import development
    binary = shutil.which(runtime)
    if not binary:
        raise worker.WorkerError(78, f'{runtime} executable is unavailable for managed full-access.')
    try:
        version = subprocess.check_output([binary, '--version'], text=True).strip()
        development.check_runtime(binary, runtime)
    except development.DevelopmentError as error:
        raise worker.WorkerError(error.code, str(error)) from None
    except (OSError, subprocess.SubprocessError):
        raise worker.WorkerError(78, f'{runtime} managed full-access capability check failed.') from None
    print(version)
    print(f'Managed {runtime} runtime capabilities: PASS (effective access={policy.effective_access}).')


def live_tests(runtimes=('codex',), os_sandbox='required'):
    if os.environ.get('DEEPSEEK_TEAM_DISABLED') == '1' or os.environ.get('CODEX_DEEPSEEK_DISABLED') == '1':
        print('Live check disabled by DeepSeek delegation switch.')
        return 69
    key = worker.load_api_key()
    if not key.strip():
        print('Live check blocked: set a DeepSeek key with deepseek-team auth set.')
        return 78
    code, out, err = worker.execute(
        [sys.executable, str(Path(__file__).resolve()), '--api-probe'],
        worker.child_environment(worker.codex_home(), key), '', 120)
    if out.strip():
        print(worker.redact(out.strip(), key))
    if code:
        print('API probe failed; no worker started.')
        return code
    state = Path.home() / '.local/state/codex-deepseek'
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    for runtime in runtimes:
        with tempfile.TemporaryDirectory(prefix='doctor-', dir=state) as directory:
            root = Path(directory)
            subprocess.run(['git', 'init', '-q', str(root)], check=True, capture_output=True)
            (root / 'evidence.txt').write_text('DEEPSEEK_TEAM_SYNTHETIC_EVIDENCE')
            before = repository_fingerprint(root)
            print(f'Running one {runtime} read-only worker on synthetic data; no total deadline...', flush=True)
            result = call('Read only evidence.txt. Return its exact content and DEEPSEEK_TEAM_OK. Do not read other files, run tests, use network or write anything.', cwd=root, runtime=runtime, os_sandbox=os_sandbox)
            ok = (result.returncode == 0 and 'DEEPSEEK_TEAM_SYNTHETIC_EVIDENCE' in result.stdout
                  and 'DEEPSEEK_TEAM_OK' in result.stdout and before == repository_fingerprint(root))
            print(f'Synthetic {runtime} worker check: ' + ('PASS' if ok else 'FAIL'))
            if not ok:
                print('Worker failed or returned incomplete evidence; raw output omitted.')
                return result.returncode or 70
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group()
    group.add_argument('--offline', action='store_true', help='Local checks only (default); no network or key reads.')
    group.add_argument('--live', action='store_true', help='Also verify DeepSeek API routing and synthetic worker(s); API charges apply.')
    parser.add_argument('--runtime', choices=['codex', 'claude', 'both', 'auto'], default='codex',
                        help='Runtime(s) to verify; default codex preserves legacy behavior.')
    parser.add_argument('--os-sandbox', choices=['required', 'off'], default='required',
                        help='required: verify Bubblewrap/AppArmor containment (default); off: explicitly skip only this OS-layer check.')
    parser.add_argument('--delegation-level', type=int, choices=[25, 50, 75],
                        help='Per-diagnostic override; resolved with project/global/default settings.')
    parser.add_argument('--access', choices=['auto', 'read-only', 'full-access'],
                        help='Per-diagnostic access override; independent of delegation level.')
    args = parser.parse_args(argv)
    try:
        from codex_deepseek_team import settings
        policy = resolve_policy(delegation_level=args.delegation_level, access=args.access)
        print(settings.describe(policy))
        if policy.effective_access == 'full-access' and args.os_sandbox == 'off':
            raise worker.WorkerError(
                64, 'Effective full-access requires the OS sandbox; diagnostics cannot validate it with --os-sandbox off.')
        if args.os_sandbox == 'required':
            sandbox, backend = worker.resolve_os_sandbox('required')
            print(f'OS sandbox: PASS ({backend.source}, {backend.bwrap})')
            restriction = sandbox.apparmor_restriction()
            if restriction is not None:
                print(f'kernel.apparmor_restrict_unprivileged_userns={restriction}')
        else:
            print('OS sandbox check: SKIPPED by explicit --os-sandbox off.')
        runtimes = selected_runtimes(args.runtime)
        for runtime in runtimes:
            check_policy_runtime(runtime, policy)
        print('Local runtime checks: PASS.')
        if args.live and policy.effective_access == 'full-access':
            print('Live provider smoke remains read-only by design; full-access permissions are checked locally.')
        if not args.live:
            print('No network requests or credential validation performed. Use --live for API and worker checks.')
            return 0
        paths = []
        if 'codex' in runtimes:
            paths = [worker.codex_home() / 'config.toml', worker.codex_home() / 'auth.json']
        before = [path.read_bytes() if path.exists() else None for path in paths]
        code = live_tests(runtimes, os_sandbox=args.os_sandbox)
        unchanged = before == [path.read_bytes() if path.exists() else None for path in paths]
        if paths:
            print('Primary Codex configuration/auth unchanged:', unchanged)
        return code if unchanged else 1
    except worker.WorkerError as error:
        print(error.message, file=sys.stderr)
        return error.code
    except (OSError, subprocess.SubprocessError):
        print('Diagnostics could not complete; check selected CLI, Git and local configuration.', file=sys.stderr)
        return 78


if __name__ == '__main__':
    try:
        sys.exit(api_probe() if sys.argv[1:] == ['--api-probe'] else main())
    except worker.WorkerError as error:
        print(error.message, file=sys.stderr)
        sys.exit(error.code)
