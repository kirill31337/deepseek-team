"""One managed development job; no scheduler and no automatic writer retry."""
from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import time

from . import development, relay, settings, workspace


def run(args, policy: settings.Policy, api, copy=None) -> int:
    """Resolve containment before credential access; preserve every started copy."""
    if args.os_sandbox != 'required':
        raise api.WorkerError(64, 'Managed access requires --os-sandbox required; no unsafe fallback.')
    writable = policy.effective_access == 'full-access'
    if getattr(args, 'attempts_explicit', False) and args.attempts != 1:
        raise api.WorkerError(64, 'Managed copies use one attempt; inspect and explicitly continue instead of retrying.')
    task = args.task if args.task is not None else api.sys.stdin.read()
    if not task.strip():
        raise api.WorkerError(64, 'Pass a task on stdin or as one argument.')
    slot = api.acquire_slot(args.state_dir)
    started = time.monotonic()
    try:
        runtime, binary = api.resolve_runtime(args.runtime, args.codex, args.claude)
        development.check_runtime(binary, runtime)
        sb, backend = api.resolve_os_sandbox('required')
        if copy is None:
            copy = workspace.create(Path.cwd(), args.state_dir)
        print(f'Workspace: {copy.id}\nWorking copy: {copy.path}\n'
              'Source: committed HEAD only; source uncommitted changes were not copied or modified.', file=api.sys.stderr)
        with copy.lock(recover=getattr(args, 'resume_after_failure', False)):
            with tempfile.TemporaryDirectory(prefix='session-', dir=args.state_dir) as session, \
                    tempfile.TemporaryDirectory(prefix='dst-') as transport:
                home, control = Path(session), Path(transport)
                env = api.child_environment(home, relay.LOCAL_CREDENTIAL, runtime)
                if runtime == 'codex':
                    api.transient_config(home)
                else:
                    env['ANTHROPIC_BASE_URL'] = '@DEEPSEEK_TEAM_ENDPOINT@/anthropic'
                    env['ANTHROPIC_API_KEY'] = relay.LOCAL_CREDENTIAL
                # Dependencies prepared in the copy take precedence; inherited
                # PATH entries are deliberately not exposed wholesale to bwrap.
                public_path = ':'.join(str(p) for p in (
                    copy.path / '.venv/bin', copy.path / 'node_modules/.bin',
                    Path(binary).parent, Path(api.sys.executable).parent))
                env['PATH'] = public_path + ':/usr/local/bin:/usr/bin:/bin'
                env.update(PYTHONDONTWRITEBYTECODE='1', GIT_CONFIG_NOSYSTEM='1',
                           GIT_CONFIG_GLOBAL='/dev/null', GIT_OPTIONAL_LOCKS='0',
                           GIT_TERMINAL_PROMPT='0')
                layout = development.layout(backend, copy.path, home, control,
                                            [binary], writable=writable)
                development.probe(layout, env)
                # Only after the actual namespace layout has executed successfully.
                key = api.load_api_key()
                if not key.strip():
                    raise api.WorkerError(78, 'Provider credential is absent; configure it locally. Workspace retained.')
                development.write_launch(control, binary, runtime, env, writable=writable)
                copy.begin('execution')
                copy.metadata.update(delegation_level=policy.delegation_level,
                                     requested_access=policy.access, effective_access=policy.effective_access,
                                     configuration_sources=dict(policy.sources), runtime=runtime)
                copy.save()
                provider = None
                try:
                    with relay.ProviderRelay(control / 'provider.sock', key) as provider:
                        timeout = max(0.001, args.timeout - (time.monotonic() - started)) if args.timeout else None
                        code, out, err = api.execute(development.bridge_command(layout), env, task, timeout)
                        message, errors, completed = api.runtime_result(runtime, out)
                        if code == 0 and (not completed or not message.strip()):
                            code = 70
                        kind = 'provider' if provider.failures else 'execution'
                        result = copy.finish('succeeded' if code == 0 else 'failed',
                                             error_kind=kind if code else None, exit_code=code)
                    if code:
                        print(f'{kind.title()} failure (exit {code}); partial files and diff retained. '
                              f'Inspect workspace {copy.id} before --resume-after-failure.', file=api.sys.stderr)
                        # Diagnostics may contain project content; provider secrets
                        # are redacted and raw malformed protocol is never printed.
                        diagnostics = api.redact(err + '\n' + errors, key).strip()
                        if diagnostics:
                            print(diagnostics, file=api.sys.stderr)
                    else:
                        print(api.redact(message, key))
                    print('Workspace result: ' + json.dumps({
                        'id': copy.id, 'status': result['status'],
                        'changed_files': result['changed_files'],
                        'ignored_artifacts': result['ignored_artifacts'],
                        'elapsed_seconds': round(time.monotonic() - started, 3),
                    }), file=api.sys.stderr)
                    return code
                except BaseException as error:
                    code = 130 if isinstance(error, KeyboardInterrupt) else getattr(error, 'code', 71)
                    if isinstance(error, workspace.WorkspaceError) and code == 73:
                        kind = 'verification'
                    elif provider is not None and getattr(provider, 'failures', ()):
                        kind = 'provider'
                    else:
                        kind = 'execution'
                    try:
                        copy.finish('failed', error_kind=kind, exit_code=code)
                    except (workspace.WorkspaceError, OSError):
                        copy.failed(kind, code)
                    print(f'{kind.title()} failure; retained workspace {copy.id}. '
                          'Explicit recovery is required; no automatic implementation retry.', file=api.sys.stderr)
                    raise
    except (workspace.WorkspaceError, development.DevelopmentError) as error:
        raise api.WorkerError(error.code, str(error)) from None
    finally:
        os.close(slot)
