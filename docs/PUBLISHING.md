# Publishing DeepSeek Team

The distribution is **codex-deepseek-team**. It installs `deepseek-team` and the compatible `codex-deepseek-team` command. Preparing the workflow does not create a PyPI project: the account owner must register its Trusted Publisher first. Never include API keys, local configuration or raw model logs in the repository.

## One-time owner setup

1. Sign in to [PyPI](https://pypi.org/) and open [Publishing](https://pypi.org/manage/account/publishing/). If the project does not exist, add a **pending publisher** using the exact values below. For an existing project you own, use its Publishing settings.
2. Create the `pypi` GitHub environment in [repository environments](https://github.com/kirill31337/deepseek-team/settings/environments). Restrict production deployment to version tags (`v*`); configure a required reviewer if appropriate for your release policy.
3. Repeat on [TestPyPI Publishing](https://test.pypi.org/manage/account/publishing/) with the `testpypi` environment. TestPyPI requires separate account/publisher configuration.
4. Set the repository Actions variable `PYPI_PUBLISHING_ENABLED` to the literal `true` in [Actions variables](https://github.com/kirill31337/deepseek-team/settings/variables/actions). Without it, GitHub releases still work, but the PyPI job is skipped. This is a non-secret switch, not a credential.

| Field | Production | Test registry |
| --- | --- | --- |
| PyPI project name | `codex-deepseek-team` | `codex-deepseek-team` |
| GitHub owner | `kirill31337` | `kirill31337` |
| Repository | `deepseek-team` | `deepseek-team` |
| Workflow filename | `publish-release.yml` | `publish-release.yml` |
| Environment | `pypi` | `testpypi` |

Do not include `.github/workflows/` in the PyPI workflow filename field. A pending publisher does not reserve the name. These steps use short-lived GitHub OIDC credentials; no manually copied token is needed. See [PyPI's pending-publisher guide](https://docs.pypi.org/trusted-publishers/creating-a-project-through-oidc/) and [Trusted Publishing configuration](https://docs.pypi.org/trusted-publishers/adding-a-publisher/).

## Validate a candidate locally

Use a dedicated development venv with Python 3.11 or later:

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install build twine pipx uv
PYTHONPATH=src python3 -m unittest discover -s tests -v
python -m build
python -m twine check --strict dist/*
for installer in pip pipx uv; do
  python3 scripts/check_installation.py --installer "$installer" \
    --wheel dist/codex_deepseek_team-0.8.0-py3-none-any.whl
done
```

Use a clean `dist` directory for each release. The script creates private temporary homes, checks both installed commands and package resources, replaces the installation, and uninstalls it while checking retained user-state sentinels. It does not run workers, read your credential or configure your real coordinators. Without `--previous-wheel` this is **same-version replacement**, not an upgrade claim. To verify a real upgrade, supply an older release wheel:

```bash
python3 scripts/check_installation.py --installer pipx \
  --previous-wheel /path/to/codex_deepseek_team-0.7.1-py3-none-any.whl \
  --wheel dist/codex_deepseek_team-0.8.0-py3-none-any.whl
```

Repeat with pip and uv. The CI installer matrix covers Python 3.11 and 3.13; the unit/build matrix also covers 3.12. These checks validate package lifecycle, not provider credentials or complete host sandbox installation. Use `deepseek-team setup` on a supported Linux host for local readiness.

## TestPyPI rehearsal

Once the workflow is on the default branch, open [Publish release](https://github.com/kirill31337/deepseek-team/actions/workflows/publish-release.yml), choose **Run workflow**, select the candidate ref and `target: testpypi`. It builds and checks artifacts before publishing to TestPyPI, without publishing to production PyPI.

Install the result in a disposable venv using only TestPyPI:

```bash
python3 -m venv /tmp/deepseek-team-testpypi
/tmp/deepseek-team-testpypi/bin/python -m pip install \
  --index-url https://test.pypi.org/simple/ --only-binary=:all: --no-deps \
  codex-deepseek-team==0.8.0
/tmp/deepseek-team-testpypi/bin/deepseek-team --version
```

## Production release

Releases now run on version tags, rather than every push to `main`. Update both version declarations and `docs/releases/VERSION.md`, review and merge the candidate, then tag the reviewed commit:

```bash
git tag -a v0.8.0 -m 'DeepSeek Team 0.8.0'
git push origin v0.8.0
```

The workflow checks tag/version identity, tests and builds wheel/sdist, validates their metadata, and exercises the exact wheel with pip, pipx and uv. Separate GitHub release and PyPI jobs use these verified artifacts. Only registry jobs have `id-token: write`; only the GitHub release job has `contents: write`. Publishing jobs do not check out or execute repository code. The PyPA action produces attestations through Trusted Publishing.

If the tag was pushed before PyPI was enabled, enable the switch and manually run the workflow on **that tag**, with `target: pypi`. Production publication from a branch or mismatched tag fails before upload. Existing GitHub release assets are preserved.

After success, verify registry installation in a clean environment:

```bash
pipx install codex-deepseek-team==0.8.0
deepseek-team --version
```

Then update the README's pending-publication notice and make the registry command the default. Do not announce PyPI availability before verifying an accepted upload.

## Failures and retries

- **Publisher identity mismatch:** check owner, repository, exact workflow filename and environment against the table. Check PyPI and TestPyPI separately.
- **PyPI job skipped:** check `PYPI_PUBLISHING_ENABLED=true`, selected tag and manual target. A successful GitHub release alone is not proof of a PyPI upload.
- **Version already uploaded:** PyPI artifacts are immutable. Inspect existing files; use a new version for changed source.
- **Partial upload or late network failure:** inspect the registry before retrying. The workflow deliberately does not silently accept existing filenames; recover the interrupted upload after reviewing artifacts.
- **Installation checks fail:** fix the candidate before creating another release tag. Keep worker sandbox requirements intact.
