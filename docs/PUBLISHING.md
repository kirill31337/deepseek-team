# Publishing DeepSeek Team

The distribution is **deepseek-team**. It installs the `deepseek-team` command. Version **0.8.1** is the first PyPI release under this name. Only the coordinator publishes releases; workers may prepare changes and checks. Never include API keys, credentials, local configuration or raw model logs in the repository, issues, logs or CI output.

There are two supported publication routes:

- **Manual token publishing** - the coordinator uploads verified release artifacts with Twine and a private PyPI API token. This is the route for the first 0.8.1 upload.
- **Optional GitHub OIDC (Trusted Publishing)** - the existing `publish-release.yml` can upload without a stored token, but only after the owner registers the trusted publisher and enables the switch below. It is **not enabled** yet. Production PyPI publication is skipped unless `PYPI_PUBLISHING_ENABLED=true`; TestPyPI runs only when explicitly selected through `workflow_dispatch` and needs its own publisher.

## Manual token publishing

Use this when you publish from a maintained machine without GitHub OIDC.

1. Create a scoped PyPI API token (Account settings -> API tokens) for the `deepseek-team` project, or an account-scoped token only if the project does not exist yet. The token value is a secret.
2. Store it **only** in a private `~/.pypirc` that is never committed:

   ```bash
   chmod 600 ~/.pypirc
   ```

   ```ini
   # ~/.pypirc  (mode 0600, outside the repository)
   [distutils]
   index-servers =
       pypi
       testpypi

   [pypi]
   repository = https://upload.pypi.org/legacy/
   username = __token__
   password = pypi-REPLACE-WITH-YOUR-TOKEN

   [testpypi]
   repository = https://test.pypi.org/legacy/
   username = __token__
   password = pypi-REPLACE-WITH-THE-TESTPYPI-TOKEN
   ```

   - The username is the literal `__token__`; Twine sends the token itself as the password. Never use your PyPI account login password.
   - Use PyPI's official legacy upload endpoint `https://upload.pypi.org/legacy/` (TestPyPI: `https://test.pypi.org/legacy/`). Do not point Twine at other hosts.
   - Keep the file outside the repository (`~/.pypirc` is the default), never echo the token, and never paste it into logs, issue text, chat or shell arguments. Do not pass the token directly on the command line; let Twine read the private `~/.pypirc` instead. If it leaks, revoke the token on PyPI and issue a new one.
3. Upload the verified release artifacts using the reusable procedure below.
4. Confirm the accepted upload, then verify the registry installation in a clean environment:

   ```bash
   pipx install deepseek-team==0.8.1
   deepseek-team --version
   ```

## Publish a verified release artifact

This procedure is reusable for any version. It publishes the exact wheel and sdist already attached to a tagged GitHub release, without rebuilding. Set the version once and follow the steps.

```bash
VERSION=0.8.1
mkdir -p "/tmp/dst-$VERSION" && cd "/tmp/dst-$VERSION"
gh release download "v$VERSION" --repo kirill31337/deepseek-team \
  --pattern '*.whl' --pattern '*.tar.gz'
ls -l
```

Verify the downloaded artifacts before uploading:

```bash
# 1. Confirm the released artifacts pass registry-grade metadata checks.
python -m twine check --strict *.whl *.tar.gz

# 2. Confirm the distributed metadata is exactly the expected neutral identity.
VERSION="$VERSION" python - <<'PY'
import email.parser, glob, os, tarfile, zipfile
version = os.environ['VERSION']
wheel_path, = glob.glob(f'*{version}*.whl')
sdist_path, = glob.glob(f'*{version}*.tar.gz')
with zipfile.ZipFile(wheel_path) as archive:
    metadata_name, = [n for n in archive.namelist() if n.endswith('.dist-info/METADATA')]
    wheel = archive.read(metadata_name)
with tarfile.open(sdist_path) as archive:
    metadata_name, = [n for n in archive.getnames() if n.count('/') == 1 and n.endswith('/PKG-INFO')]
    sdist = archive.extractfile(metadata_name).read()
for raw in (wheel, sdist):
    metadata = email.parser.BytesParser().parsebytes(raw)
    assert metadata['Name'] == 'deepseek-team', metadata['Name']
    assert metadata['Version'] == version, metadata['Version']
print(f'Both distributions identify deepseek-team {version}.')
PY

# 3. Verify local digests against the GitHub release asset metadata.
gh api "repos/kirill31337/deepseek-team/releases/tags/v$VERSION" \
  --jq '.assets[] | select(.name | test("\\.(whl|tar\\.gz)$")) | "\(.digest | sub("^sha256:"; ""))  \(.name)"' \
  > SHA256SUMS
sha256sum --check SHA256SUMS
```

Then upload with the private-token route:

```bash
python -m twine check --strict *.whl *.tar.gz
python -m twine upload --repository pypi *.whl *.tar.gz
```

Prefer `--repository pypi` so the token is read from the private `~/.pypirc` instead of appearing in the command line or shell history. PyPI filenames are immutable: if a file already exists, compare hashes instead of uploading again, renaming or rebuilding. Changed source needs a new version and a new tag, not a replacement of an existing one.

## One-time owner setup for optional GitHub OIDC

This route is optional and **not enabled**; skip it while publishing manually with a token. Preparing the workflow does not create a PyPI project: the account owner must register its Trusted Publisher first.

1. Sign in to [PyPI](https://pypi.org/) and open [Publishing](https://pypi.org/manage/account/publishing/). If the project does not exist, add a **pending publisher** using the exact values below. For an existing project you own, use its Publishing settings.
2. Create the `pypi` GitHub environment in [repository environments](https://github.com/kirill31337/deepseek-team/settings/environments). Restrict production deployment to version tags (`v*`); configure a required reviewer if appropriate for your release policy.
3. Repeat on [TestPyPI Publishing](https://test.pypi.org/manage/account/publishing/) with the `testpypi` environment. TestPyPI requires separate account/publisher configuration.
4. Set the repository Actions variable `PYPI_PUBLISHING_ENABLED` to the literal `true` in [Actions variables](https://github.com/kirill31337/deepseek-team/settings/variables/actions). Without it, GitHub releases still work, but the PyPI job is skipped. This is a non-secret switch, not a credential. It does **not** turn on OIDC on its own - the trusted publisher registration must exist too.

| Field | Production | Test registry |
| --- | --- | --- |
| PyPI project name | `deepseek-team` | `deepseek-team` |
| GitHub owner | `kirill31337` | `kirill31337` |
| Repository | `deepseek-team` | `deepseek-team` |
| Workflow filename | `publish-release.yml` | `publish-release.yml` |
| Environment | `pypi` | `testpypi` |

Do not include `.github/workflows/` in the PyPI workflow filename field. A pending publisher does not reserve the name. Once enabled, these steps use short-lived GitHub OIDC credentials; no manually copied token is needed. See [PyPI's pending-publisher guide](https://docs.pypi.org/trusted-publishers/creating-a-project-through-oidc/) and [Trusted Publishing configuration](https://docs.pypi.org/trusted-publishers/adding-a-publisher/).

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
    --wheel dist/deepseek_team-0.8.1-py3-none-any.whl
done
```

Use a clean `dist` directory for each release. The script creates private temporary homes, checks the installed `deepseek-team` command and package resources, replaces the current-version installation, and uninstalls it while checking retained user-state sentinels. It does not run workers, read your credential or configure your real coordinators. These are current-version installation, replacement and removal checks.

This local build validates a *candidate*. For an already-tagged release, publish the downloaded release artifacts with the reusable procedure above instead of a fresh local build.

The CI installer matrix covers Python 3.11 and 3.13; the unit/build matrix also covers 3.12. These checks validate package lifecycle, not provider credentials or complete host sandbox installation. Use `deepseek-team setup` on a supported Linux host for local readiness.

## TestPyPI rehearsal

Only with OIDC enabled: once the workflow is on the default branch, open [Publish release](https://github.com/kirill31337/deepseek-team/actions/workflows/publish-release.yml), choose **Run workflow**, select the candidate ref and `target: testpypi`. It builds and checks artifacts before publishing to TestPyPI, without publishing to production PyPI.

Without OIDC, rehearse the same way with a TestPyPI token and `python -m twine upload --repository testpypi dist/*`.

Install the result in a disposable venv using only TestPyPI:

```bash
python3 -m venv /tmp/deepseek-team-testpypi
/tmp/deepseek-team-testpypi/bin/python -m pip install \
  --index-url https://test.pypi.org/simple/ --only-binary=:all: --no-deps \
  deepseek-team==0.8.1
/tmp/deepseek-team-testpypi/bin/deepseek-team --version
```

## Production release through tags

Releases run on version tags, rather than every push to `main`. This flow works today for GitHub releases; the PyPI job runs only when the optional OIDC setup above is complete. Update both version declarations and `docs/releases/VERSION.md`, review and merge the candidate, then tag the reviewed commit:

```bash
git tag -a v0.8.1 -m 'DeepSeek Team 0.8.1'
git push origin v0.8.1
```

The workflow checks tag/version identity, tests and builds wheel/sdist, validates their metadata, and exercises the exact wheel with pip, pipx and uv. Separate GitHub release and PyPI jobs use these verified artifacts. Only registry jobs have `id-token: write`; only the GitHub release job has `contents: write`. Publishing jobs do not check out or execute repository code. The PyPA action produces attestations through Trusted Publishing.

If a tag was pushed before PyPI was enabled, enable the switch and manually run the workflow on **that tag**, with `target: pypi`. Production publication from a branch or mismatched tag fails before upload. Existing GitHub release assets are preserved. While OIDC stays disabled, publish the verified release artifacts with the manual token route above without rebuilding.

After success, verify registry installation in a clean environment:

```bash
pipx install deepseek-team==0.8.1
deepseek-team --version
```

Then confirm the README already documents the registry install as the default. Do not announce PyPI availability before verifying an accepted upload.

## Failures and retries

- **Authentication failed (manual):** confirm the token is current, that `~/.pypirc` is mode `0600` and points at `https://upload.pypi.org/legacy/`, and that the username is `__token__`. Never print the token to diagnose this.
- **Publisher identity mismatch (OIDC):** check owner, repository, exact workflow filename and environment against the table. Check PyPI and TestPyPI separately.
- **PyPI job skipped:** check `PYPI_PUBLISHING_ENABLED=true`, the trusted publisher registration, selected tag and manual target. A successful GitHub release alone is not proof of a PyPI upload.
- **Version already uploaded:** PyPI artifacts are immutable. Inspect existing files and compare hashes; use a new version for changed source.
- **Partial upload or late network failure:** inspect the registry before retrying. Twine and the workflow deliberately do not silently overwrite existing filenames; recover the interrupted upload after reviewing artifacts.
- **Installation checks fail:** fix the candidate before creating another release tag. Keep worker sandbox requirements intact.
