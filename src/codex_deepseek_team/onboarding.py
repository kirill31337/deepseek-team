"""User-level setup with explicit Linux prerequisite checks."""
import getpass
import os
import platform
import shutil
import subprocess
import sys

from . import claude_config, config, doctor, sandbox, worker


def _configure(runtimes):
    if 'codex' in runtimes:
        changed = config.configure(worker.codex_home())
        hooks_changed = config.install_codex_hooks(worker.codex_home())
        print('DeepSeek Codex provider configured.' if changed
              else 'Compatible DeepSeek Codex provider already configured.')
        print('Codex coordination hooks installed.' if hooks_changed
              else 'Codex coordination hooks already installed.')
        print('Codex primary model and OpenAI authentication were preserved.')
        print('Review/trust the DeepSeek Team hook in Codex /hooks; setup cannot grant native trust.')
    if 'claude' in runtimes:
        changed = claude_config.install()
        print('Claude coordination hooks installed.' if changed
              else 'Claude coordination hooks already installed.')
        print('Claude model, permissions, authentication and unrelated hooks were preserved.')
        print('Start a new Claude session and check /hooks; native settings may disable hooks.')


def _ubuntu_sandbox():
    try:
        release = platform.freedesktop_os_release()
    except OSError:
        release = {}
    if release.get('ID') != 'ubuntu' and 'ubuntu' not in release.get('ID_LIKE', '').split():
        raise ValueError('--with-sandbox manages system packages only on Ubuntu. '
                         'Install Bubblewrap with your distribution package manager, '
                         'then run deepseek-team sandbox status and deepseek-team setup.')
    try:
        sandbox.probe_backend()
        print('Existing Bubblewrap isolation works; no system changes needed.')
        return
    except sandbox.SandboxError:
        pass
    apt = shutil.which('apt-get')
    if not apt:
        raise ValueError('apt-get is unavailable; install Bubblewrap and AppArmor, '
                         'then rerun deepseek-team setup.')
    prefix = []
    if os.geteuid() != 0:
        sudo = shutil.which('sudo')
        if not sudo:
            raise ValueError('sudo is required for --with-sandbox; ask your administrator '
                             'to install Bubblewrap and the DeepSeek Team AppArmor profile.')
        prefix = [sudo]
    print('Installing Ubuntu sandbox prerequisites (explicit --with-sandbox request).', flush=True)
    try:
        subprocess.run([*prefix, apt, 'update'], check=True)
        subprocess.run([*prefix, apt, 'install', '-y', 'bubblewrap', 'apparmor'], check=True)
    except (OSError, subprocess.CalledProcessError):
        raise ValueError('Ubuntu package installation failed; check apt/network and administrator '
                         'permissions, then rerun deepseek-team setup --with-sandbox.') from None
    try:
        sandbox.probe_backend()
        print('Installed Bubblewrap isolation works; no AppArmor profile change needed.')
        return
    except sandbox.SandboxError:
        pass
    sandbox.install_apparmor()


def _prerequisites(runtimes, with_sandbox):
    for runtime in runtimes:
        if not shutil.which(runtime):
            name = 'Codex CLI' if runtime == 'codex' else 'Claude Code CLI'
            raise ValueError(f'{name} ({runtime}) is not on PATH. Install it and restart your '
                             f'terminal, then rerun deepseek-team setup --runtime {runtime}.')
    if not shutil.which('git'):
        raise ValueError('Git is not on PATH. Install Git with your system package manager '
                         'and rerun deepseek-team setup.')
    if not shutil.which('deepseek-team'):
        raise ValueError('deepseek-team must be on PATH so coordinator hooks can run it. '
                         'For pipx run `pipx ensurepath`; for uv run `uv tool update-shell`; '
                         'for a venv activate it. Restart your terminal and rerun setup.')
    if with_sandbox:
        _ubuntu_sandbox()
    try:
        backend = sandbox.probe_backend()
    except sandbox.SandboxError as error:
        raise sandbox.SandboxError(error.code, error.message + '\nOn Ubuntu run '
                                  '`deepseek-team setup --with-sandbox`; elsewhere install '
                                  'Bubblewrap and run `deepseek-team sandbox status`.') from None
    print(f'Bubblewrap isolation: available ({backend.source}).')


def _credential(no_key):
    if no_key:
        print('Authentication deferred (--no-key); no credential was read. '
              'If needed, run deepseek-team auth set before using workers.')
        return True
    if worker.load_api_key().strip():
        print('DeepSeek credential is available; no API request was made.')
        return True
    if sys.stdin.isatty():
        config.save_key(getpass.getpass('DeepSeek API key (hidden): '))
        print('Key stored privately; no API request was made.')
        return True
    print('Setup incomplete: enter your key in a terminal with deepseek-team auth set, '
          'then rerun setup. Use --no-key to explicitly defer authentication.')
    return False


def run(runtimes, *, no_key=False, with_sandbox=False, configure_only=False):
    """Configure selected runtimes without inferring hook trust or attaching a project."""
    if not configure_only:
        _prerequisites(runtimes, with_sandbox)
    _configure(runtimes)
    if configure_only:
        print('Configuration saved; runtime and sandbox readiness were not checked (--configure-only).')
    else:
        runtime_arg = 'both' if len(runtimes) == 2 else runtimes[0]
        code = doctor.main(['--runtime', runtime_arg, '--offline'])
        if code:
            print('Setup incomplete: resolve the local diagnostic above and rerun setup.')
            return code
        for runtime in runtimes:
            healthy = (config.codex_hooks_status(worker.codex_home()) if runtime == 'codex'
                       else claude_config.status())
            if not healthy:
                print(f'Setup incomplete: {runtime} hooks are missing or disabled. '
                      'Review your hook settings and /hooks, then rerun setup.')
                return 78
    if not _credential(no_key):
        return 78
    coordinator = 'both' if len(runtimes) == 2 else runtimes[0]
    if not configure_only:
        print('Local setup checks passed. Native hook trust/session loading still requires /hooks.')
    print(f'Attach your project explicitly: deepseek-team init --coordinator {coordinator} /path/to/project')
    print(f'Then in that project: deepseek-team doctor --runtime {coordinator} --offline')
    return 0
