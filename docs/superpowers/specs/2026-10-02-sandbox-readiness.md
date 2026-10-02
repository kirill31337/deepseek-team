# Sandbox readiness and recoverable verification

The approved change prevents host-only runtime and toolchain availability from
being mistaken for readiness inside the managed worker namespace. It also makes
failed checks diagnosable and rerunnable without another model request. The user
approved implementing all five recommendations from the incident analysis.

## Constraints

- Python 3.11+, Linux, stdlib only.
- Bubblewrap/AppArmor remains required; never mount the host root, HOME, /run or
  /var wholesale and never enable general worker networking.
- Preserve the native read-only Codex / outer read-only Claude boundary. Managed
  copies retain the sparse outer namespace, private provider relay and read-only
  Git directory for both runtimes.
- No real provider credential is read until namespace runtime checks, declared
  dependency readiness and explicit environment smoke probes have succeeded.
- Preserve saved access, delegation, effort, off state, user files and credentials.
- Explicit runtime pins are never silently replaced. No automatic model retries.
- Recipes, diagnostic output and prepared configuration stay in private state,
  outside source Git. No keys, local configuration or raw model logs in patches.
- Keep one assignment identity/history across contexts, reviews and verification
  reruns. A rerun never rewrites the original failed attempt or its quality grade.

## Runtime readiness

Create `runtime_preflight.py` with:

```python
class RuntimePreflightError(Exception):  # message, code (78 by default)
    ...

@dataclass
class RuntimeSelection:
    runtime: str
    binary: str
    layout: list[str]
    environment: dict[str, str]

def probe_runtime(binary: str, runtime: str, args: list[str],
                  env: dict[str, str]) -> str: ...

def select_runtime(requested: str, codex: str, claude: str,
                   context_factory, *, explicit_codex: bool = False,
                   explicit_claude: bool = False) -> RuntimeSelection: ...
```

`context_factory(binary, runtime)` returns `(layout, environment)` prepared by
the coordinator-owned caller for that candidate. Probe `--version` and required
CLI capabilities (`exec --help` for Codex, `--help` for Claude) in that exact
namespace, with a finite 15-second local probe deadline and no provider request.
Keep the existing capability requirements. Do not parse arbitrary launcher
scripts or broaden mounts to guess their dependencies: an unrunnable launcher
fails the probe with a useful bounded explanation.

For an explicit executable, test only that executable. For defaults, enumerate
deduplicated executable candidates in PATH order, then `/usr/local/bin` and
`/usr/bin`, and select the first candidate that really runs in its namespace.
`runtime=auto` may try Codex then Claude, as it already does. Do not cross runtimes
for an explicit `runtime=codex|claude`. CLI parsing must preserve whether
`--codex` or `--claude` was explicitly present, even when its value is the default
name. Offline full-access doctor uses the same namespace probe in a disposable
empty work directory, without allocating a model job or reading a key.

## Explicit reusable preparation

Create `toolchains.py` with:

```python
class ToolchainError(Exception):  # message, code (78 by default)
    ...

def prepare(copy, *, tools=None, copies=None, environment=None, probes=None,
            save_project: bool = False, recover: bool = False) -> dict: ...
def apply_saved_recipe(copy) -> dict: ...
def environment_for(copy, base: dict[str, str]) -> dict[str, str]: ...
def probes_for(copy) -> list[str]: ...
def read_only_roots(copy) -> list[Path]: ...
```

`tools` maps a short `[A-Za-z][A-Za-z0-9_-]{0,63}` name to an explicitly selected
host software directory. Copy it into `.deepseek-tools/NAME`, materializing safe
symlinks. `copies` is a list of `(host_source, relative_destination)` pairs for
explicit non-secret files or cache directories. Destination paths cannot escape
the copy, touch Git, overlap the private tool area, or overwrite tracked source.
Respect the package's credential-like path exclusions. Add private Git excludes
for preparation artifacts rather than modifying the product's tracked ignore file.

Reject broad roots (`/`, `/usr`, `/etc`, `/home`, `/root`, `/tmp`, `/var`, `/run`,
the user's HOME, mixed `~/.local`, source root and private state root). Reject
credential-like files, special files, escaping links and cycles before publishing
preparation metadata. Tool symlink targets may stay inside the selected software
root. Java tools may additionally materialize ordinary files below
`/etc/java-[0-9]+-openjdk/` and the exact public truststore
`/etc/ssl/certs/java/cacerts`; never bind the whole `/etc` tree. Reject symlink
escapes from these allowed targets as well. Software roots are bound read-only
inside the namespace; explicitly copied build caches remain writable in the copy.

Allowed prepared variables are `JAVA_HOME`, `ANDROID_HOME`, `ANDROID_SDK_ROOT`,
`GRADLE_USER_HOME`, `M2_HOME`, `KOTLIN_HOME` and `PATH`. Their path values must
resolve inside the copy after expanding the literal `{workspace}` placeholder.
PATH contains only prepared workspace directories and is prepended to the caller's
existing sanitized PATH. Never accept HOME, provider variables, hook/config
overrides, loader injection variables or generic inherited environment. Validate
names/values and smoke commands when configured and again when loading metadata.

`prepare` owns the copy lock, records preparation and saves version-1 metadata.
`apply_saved_recipe` requires the caller's existing copy lock and replays only a
private recipe previously saved by an explicit coordinator command, when the new
copy has no prepared toolchain yet. It never executes host commands. Recipes are
keyed by the canonical source-project path under the copy's private state root,
with owner-only atomic files and per-project locking. Missing recipes preserve
old behavior; malformed, unsafe, incompatible or failed recipes fail closed.
Unknown versions fail rather than being silently interpreted. Concurrent readers
see a complete recipe, never a partial publication. A failed replay preserves
the inspectable copy and must not be marked successful.

Extend the existing command without breaking its old form:

```text
workspace prepare ID [--tool NAME=HOST_DIR] [--copy SOURCE=REL_DEST]
  [--env NAME=VALUE] [--probe COMMAND] [--save-project]
  [--resume-after-failure] [-- COMMAND ARG ...]
```

Options are repeatable. Declarative-only preparation is valid. An explicit
legacy command continues to run on the coordinator host with the existing safe
environment; prepared workspace variables/PATH are supplied to it. Persist
artifacts/caches inside the copy, not the separate preparation HOME. Saved
recipes never persist or automatically rerun arbitrary legacy host commands.
Explicit smoke probes run only inside the worker namespace before any model key.

## Check evidence and baseline

Create `check_evidence.py` with:

```python
def run_checks(args: list[str], env: dict[str, str], commands: list[str],
               timeout: float = 120, *, deadline: float | None = None,
               directory: Path | None = None, phase: str = 'post',
               redact_values=()) -> list[dict]: ...
def classify_checks(baseline: list[dict], post: list[dict], *,
                    probes=()) -> dict: ...
```

Run each command through the same `/bin/sh -lc` namespace boundary as today.
Stop on the first failed command. Retain at most 65,536 bytes per stdout/stderr
stream, drain larger output without unbounded memory and record truncation. Kill
the process group on timeout and return 124; clean up children. Local checks use
the existing 120-second per-command ceiling and remaining total job deadline.
Catch launch failures as environment evidence rather than losing the copy.

With `directory=None`, preserve legacy `{command, exit_code}` row compatibility.
With a directory, atomically publish unique phase/run-specific owner-only output
files and rows containing phase, relative diagnostic paths and truncation flags.
Redact known sensitive values before publication, including values crossing read
chunk or retained-output boundaries. Never store full environment or raw runtime
model output. Private diagnostic publication failures must be explicit and must
not silently claim evidence was stored.

After runtime/dependency/probe readiness and before model execution, capture
declared project checks as a baseline. Nonzero baseline tests are evidence, not
a blanket rejection of a bugfix assignment. Explicit environment probe failures
are preparation failures and stop before credentials. Post-worker failures stay
operational `verification` with an evidence cause such as `unknown`,
`baseline_failure` or `regression_candidate`; none automatically grades worker
quality. Timeout/launch/environment probe failures are `environment`. The
classifier returns `{kind, cause}`; `kind` is `None`, `environment` or
`verification`, and success has `cause=None`. A matching exit code is not proof
of a matching root cause.

## Prepared ignored inputs

Create `prepared_inputs.py` with:

```python
def input_snapshot(copy) -> dict[str, str]: ...
def input_changes(copy, before: dict[str, str]) -> list[str]: ...
```

Fingerprint explicitly registered `copy.metadata['prepared_paths']` inputs,
including ignored `local.properties`, with safe relative-path validation and
the existing credential-name rules. Hash content/type/mode or a missing marker;
never follow escaping symlinks, read credential-like paths, enumerate all ignored
files, or store input content. Registered directories may be bounded to their
explicit prepared descendants; tools/cache trees do not need broad per-run
enumeration. Store resulting changes separately as `prepared_input_changes`,
outside source patches and regular `worker_changes`. Malformed registrations
fail clearly. Git ignores must not hide a change to an explicitly prepared input.

## Verify without another model call

Add `workspace verify ID [--check COMMAND ...] [--json]`. If no checks are supplied,
use the privately recorded declared checks for that copy; reject an empty set.
Rebuild the exact prepared environment and sparse namespace, run readiness probes
and checks, and save a unique verification run. This path never loads the provider
credential, starts a relay or starts a model. It may inspect an originally failed
copy without automatically resuming implementation. Preserve the original worker
status/error/checks/quality and append rerun evidence; expose latest verification
success/failure separately. Associate evidence with the original task/assignment
when recorded, without creating another quality case or rewriting old feedback.

## Integration and compatibility

The coordinator wires the new modules into `managed.py`, `development.py`, CLI,
doctor, workspace and ledger. Prepared roots are added as narrow read-only binds.
Record baseline checks, verification cause, prepared input changes, declared
commands, runtime pin provenance and rerun history in private metadata. Added
ledger fields are optional for old records. Change automatic terminal feedback
for a failed verification from `rejected` to neutral `unknown`; reviewed explicit
quality/disposition remains the only authority for the worker-attributable grade.
Preserve previous explicit worker failures when later reruns pass.

Document English/Russian setup, examples, baseline meaning and recovery. Ship
0.8.9 metadata/release notes. Build an installable artifact; publication is a
separate action. Existing attached projects receive new shipped guidance by the
existing refresh mechanism; linked human-maintained guides remain human-owned.

## Acceptance

Real Linux regressions cover a host-successful wrapper whose sibling target is
absent in the namespace, an explicit unusable pin, working system fallback,
Debian Java links/configuration, missing prepared variables, prepared ignored
inputs, baseline-red bugfix work and verification-only recovery. Unit boundary
tests cover invalid paths/links/env, bounded redacted output, deadlines/cleanup,
atomic recipe/evidence publication and unchanged assignment quality identity.
Run `PYTHONPATH=src python3 -m unittest discover -s tests -v` and report every
failure/skip. Never relax AppArmor or replace unavailable SDKs with stubs and
claim an equivalent real project build.

## Reviewed toolchain correction

Review of assignment `as-b7bf880d4459e963` requires canonical credential-like
source validation, safe destination parents before every publish/remove (also
recovery and stale-artifact cleanup), rejection of `.`/CR/LF destinations and
preservation of any tracked files below a tool destination. Record explicit
copied inputs in `prepared_paths`, so ignored preparation remains fingerprinted.

For Debian/OpenJDK compatibility, omit only the non-runtime links `docs` to a
public `/usr/share/doc/openjdk-N-*` location and `src.zip`/`lib/src.zip` to a
public `/usr/lib/jvm/openjdk-N/src.zip` location. Do not follow/copy those links;
count them as skipped. Every other unapproved escaping link still fails. This
adds no host mount and does not make missing runtime configuration acceptable;
declared real Java/security smoke probes still decide readiness.
