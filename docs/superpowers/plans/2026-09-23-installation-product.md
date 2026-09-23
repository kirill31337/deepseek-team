# Installation product readiness implementation plan

**Goal:** Make the existing Linux CLI straightforward to install, configure, update and remove, and prepare its first PyPI publication.

**Design:** Keep `codex-deepseek-team` as the distribution name and both existing commands. Install once per user using pipx, uv or a venv; explicitly attach each target repository. `setup` defaults to detected runtimes, checks local prerequisites, preserves configuration and credentials, and reports remaining native hook trust. Only `--with-sandbox` permits Ubuntu system changes. `--configure-only` retains the previous configuration-only operation for automation and never claims readiness.

**Constraints:** Linux, Python >=3.11; no runtime Python dependencies; preserve user changes and credentials; retain mandatory worker OS isolation. No automatic project attachment, paid provider probes, hook-trust bypass, or global AppArmor changes. PyPI registration requires an authenticated owner; do not claim publication before registry verification.

**Execution:** User authorized all feasible audit recommendations. Existing checkout is on `feat/installation-product`; unrelated AGENTS.md and untracked plans remain untouched. Auto routing retained setup and initially abstained on the test harness. Native agents own disjoint README and installer-test files; coordinator owns setup, publication security, integration and final verification. No source work is duplicated.

## Deliverables

- [x] Setup: add failing behavior tests for runtime detection, missing dependencies, explicit privilege, credential ordering, configuration preservation, and readiness reporting; implement `onboarding.py` and CLI flags; adapt configuration-only fixtures.
- [x] Installer lifecycle: add a temporary-environment script covering pip/pipx/uv installation, installed resources, replacement or real upgrade, uninstall and retained user state. Exercise real installers.
- [x] Documentation: matching EN/RU quickstarts, explicit target paths, pending-PyPI caveat, source fallback, installation alternatives and lifecycle commands.
- [x] Release: version 0.8.0; validate wheel/sdist metadata, add CI lifecycle matrix and separate least-privilege Trusted Publishing jobs for PyPI/TestPyPI; document exact owner configuration and recovery.
- [x] Verification: run `PYTHONPATH=src python3 -m unittest discover -s tests -v`, build wheel/sdist, run `twine check`, validate workflows, exercise pip/pipx/uv against current and previous wheels, and review the integrated diff.

## Review focus

- Missing/old coordinator executables or Git must yield actionable failures without reading credentials.
- Sandbox failures must never trigger implicit sudo or an isolation bypass.
- Noninteractive setup without a key must report incomplete readiness; `--no-key` explicitly defers authentication.
- Isolated tool environments must expose commands to hooks and keep upgrades/removal away from unrelated user state.
- Public artifacts and workflow identities must match a versioned release; publication must not depend on an event suppressed by GitHub's automatic-release token.

## Baseline and external access

Baseline at 5f598cc: 499 tests, OK with 3 skips; wheel/sdist 0.7.1 build and isolated CLI/resource smoke passed during the preceding audit. GitHub connector identifies the repository owner, but local `gh` is unauthenticated. No PyPI account-management capability is available in this session.

## Verification outcome

Final suite: 518 tests, OK with 3 skips. Both distributions build and pass `twine check --strict`. Changed workflows pass actionlint 1.7.12. Real pip, pipx 1.17.6 and uv 0.12.18 passed upgrade from 0.7.1 to 0.8.0 and uninstall checks in isolated homes. Installed 0.8.0 passed setup/init/offline doctor/detach/reset using the actual Codex CLI and working OS sandbox, without credential entry. Actual apt/profile mutations were covered with controlled tests, not applied to the host.

Independent review found one Ubuntu edge: package installation may make direct Bubblewrap isolation work before an AppArmor profile is needed. Added a failing regression, fixed the post-install probe order, and reran the full suite. README upgrade commands also avoid requiring newer pipx `upgrade --spec` syntax.

Publication remains external: no PyPI/TestPyPI project was registered, no artifact uploaded, and no tag or remote branch pushed. `docs/PUBLISHING.md` contains the exact owner settings and activation sequence.
