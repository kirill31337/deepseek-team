# Preparing toolchains and re-verifying

**English** | [Русский](https://github.com/kirill31337/deepseek-team/blob/main/docs/TOOLCHAINS.ru.md)

A managed worker runs in a sparse Bubblewrap namespace: it sees the owned copy,
the runtime prefixes it needs and a temporary HOME, but **not** the host root,
your HOME, `/etc`, `/usr/lib` or installed SDKs. A launcher that works on the host
can therefore fail inside the namespace when a sibling target or helper was never
mounted. Preparation materializes an explicitly selected toolchain into the owned
copy so it is really visible there, and the declared smoke probes decide readiness
before the provider key is read. Saved recipes use a finite immutable content
cache, so they survive the removal of any one execution checkout.

This page describes the implemented `workspace prepare` and `workspace verify`
commands. Wording of individual options is the actual CLI surface; run
`deepseek-team workspace prepare --help` or `deepseek-team workspace verify --help`
for the live help.

## Declarative preparation

```text
deepseek-team workspace prepare ID [--tool NAME=HOST_DIR] [--copy SOURCE=REL_DEST]
  [--env NAME=VALUE] [--probe COMMAND] [--save-project]
  [--cache-max-bytes BYTES] [--cache-max-inodes COUNT]
  [--resume-after-failure] [-- COMMAND ARG ...]
```

Every option is repeatable, and declarative-only preparation (no legacy command)
is valid. The copy lock is owned for the whole operation and a version-1 recipe is
recorded in the private workspace metadata.

- **`--tool NAME=HOST_DIR`** copies an explicitly selected host software directory
  into `.deepseek-tools/NAME`. The name must match
  `[A-Za-z][A-Za-z0-9_-]{0,63}`. The materialized tree is bound **read-only**
  inside the namespace, so the worker cannot modify the copied SDK/JDK. Symlinks
  are materialized only when they stay inside the selected software root;
  escaping links and cycles are rejected before anything is published.
- **`--copy SOURCE=REL_DEST`** copies one explicit non-secret file or build-cache
  directory into a workspace-relative destination. Copied caches stay **writable**
  in the owned copy. A destination cannot escape the copy, contain `.`/`..`, NUL,
  CR or LF, touch Git metadata, overlap `.deepseek-tools/`, be credential-like, or
  overwrite tracked source. Recorded copy destinations are stored in private
  `prepared_paths`, so a later change to an ignored input such as
  `local.properties` is fingerprinted as evidence instead of landing in the diff.
- **`--env NAME=VALUE`** declares one prepared variable. Only `JAVA_HOME`,
  `ANDROID_HOME`, `ANDROID_SDK_ROOT`, `GRADLE_USER_HOME`, `M2_HOME`, `KOTLIN_HOME`
  and `PATH` are accepted, and every path value must use the literal
  `{workspace}` placeholder that resolves inside the copy. Each `PATH` element must
  be such a workspace directory; the prepared `PATH` is prepended to the caller's
  sanitized `PATH`. `HOME`, provider variables, hook/config overrides and loader
  injection variables are never accepted.
- **`--probe COMMAND`** declares an explicit smoke command. Probes run only inside
  the worker namespace, before the provider key is read, up to 64 probes. A failed
  probe is a preparation failure (`environment` / `probe_failure`) that stops the
  job; it never silently claims success.
- **`--save-project`** saves a private recipe, keyed by the canonical source-project
  path under the private state root, that replays these declarations into a later
  copy of the same project. The recipe contains only declarations and **never
  executes or automatically replays a host command**. A malformed, unsafe,
  incompatible or failed recipe fails closed; a missing recipe preserves the old
  behavior. Every tool and copy source recorded by the recipe is an immutable
  `toolchain-cache/entries/<sha256>` entry snapshotted from the actual prepared
  destination, not a mutable host path. Recipes, diagnostics and hashes stay in
  private state outside the source patch.
- **`--cache-max-bytes BYTES` / `--cache-max-inodes COUNT`** set the finite
  aggregate saved-cache ceiling for one save operation. Both accept only positive
  decimal integers from `1` through `2**63 - 1`; zero, negative, boolean,
  fractional and oversized values are rejected with exit code `64` before any
  materialization or provider action. They are valid only together with
  `--save-project`. Defaults are `2147483648` bytes and `200000` inodes.
- **Legacy `-- COMMAND ARG ...`** is unchanged: it still runs on the coordinator
  host with the existing safe environment, and the prepared workspace variables and
  `PATH` are supplied to it. Keep artifacts and caches inside the copy rather than
  the separate preparation HOME.

Repeated preparation replaces the declaration set exactly: artifacts that are no
longer declared are removed, and tracked source below a destination is preserved
and refuses replacement. Preparation artifacts are added to the copy's private Git
excludes (`.git/info/exclude`), not to the product's tracked ignore file, so they
do not appear in the worker diff.

## Immutable content cache and storage reservations

The content cache is finite and immutable: entries have stable content identity,
saved tool recipes remain independent of execution trees, and removing a finalized
workspace does not invalidate a still-present entry. Each active workspace still
materializes every declared tool and copy independently inside the existing sandbox
mapping. Cache content is copied again into the workspace; mutable workspace files
are never hardlinked from the cache.

`--cache-max-bytes` and `--cache-max-inodes` apply to the whole save batch and the
shared cache, not to each declaration separately. A save that cannot fit every
new source and every still-referenced prior recipe entry fails before publishing the
new recipe. The current recipe stays byte-identical and its usable cache entries
remain protected. Valid entries selected earlier in the same batch are protected
from eviction too, so one declaration cannot evict another or the previous recipe's
references. The limits are operation options: they are not written into version-1
metadata or the recipe and do not change its key. Replay reads already cached
entries and does not impose the old save limit again.

Capacity accounting is aggregate. A prepared save temporarily needs staging room,
then retains both the workspace copy and the shared cache entry; those are separate
copies and both consume the configured storage budget. Identical cached content is
deduplicated by digest, while each active workspace still gets its own independent
copy. Ordinary idle entries may be evicted when a later operation needs room;
invalid, foreign, protected and currently selected entries are never deleted for
capacity.

A missing or unusable cache source blocks replay with actionable guidance to run
preparation again. A known missing payload invalidates the stale recipe under its
lock. Incomplete or corrupt entries and their recipe are preserved for inspection;
no uncertain cache data is deleted to recover automatically. Legacy version-1
recipes that still name mutable
host sources are parsed but cannot prove the bytes that were originally prepared.
Fresh replay is refused with explicit `workspace prepare` guidance for the matching
`--tool NAME=HOST_DIR` and/or `--copy SOURCE=REL_DEST` declarations plus
`--save-project`, and the old recipe is preserved rather than silently
migrated, deleted or replayed from the current host path. An already prepared live
workspace remains usable under its existing metadata; an explicit re-preparation
may replace the legacy recipe normally.

Before allocation, aggregate byte and inode requirements are reserved together
with free-space margins. Reservations honor the configured private storage root and
`TMPDIR`; `/mnt/data` is not a portable default.

Preparation-failure memory and `--reuse-checks` are deliberately conservative for
large inputs. Bounded hashing that cannot represent every file may become
`unknown`; in that case the preparation guard does not suppress the attempt and
declared checks execute again. An unrepresented mutation is never promoted to a
confirmed identity merely to make reuse cheaper.

## Debian/OpenJDK materialization

For an explicitly selected Java runtime, preparation also materializes the ordinary
runtime configuration that lives outside the software root: files below
`/etc/java-[0-9]+-openjdk/` and the exact public truststore
`/etc/ssl/certs/java/cacerts`. It never mounts the whole `/etc` tree.

Only the two non-runtime public links are omitted, without reading, following or
binding their targets: `docs` pointing at `/usr/share/doc/openjdk-N-*`, and
`src.zip` / `lib/src.zip` pointing at `/usr/lib/jvm/openjdk-N/src.zip`. They are
counted as skipped. Every other escaping link still fails preparation. This makes
Debian Java start-up and truststore access possible; it does not make missing
runtime configuration acceptable — the declared real Java/security smoke probes
still decide readiness.

## Realistic recipes

Each declarative preparation replaces its previous declaration set. Include all
tools, copies, variables and probes needed by the project in the same command;
separate examples below are complete starting points, not cumulative steps.

These examples make a toolchain visible and declare how to check it. They do not
promise that an unprepared SDK or build will succeed: a declared probe or the
project build still decides readiness, and a missing component fails early rather
than claiming a real build.

**OpenJDK (Debian/Ubuntu package layout):**

```bash
deepseek-team workspace prepare WORKSPACE_ID \
  --tool JAVA_HOME=/usr/lib/jvm/java-17-openjdk-amd64 \
  --env 'JAVA_HOME={workspace}/.deepseek-tools/JAVA_HOME' \
  --env 'PATH={workspace}/.deepseek-tools/JAVA_HOME/bin' \
  --probe 'java -version' \
  --probe 'javac -version' \
  --save-project
```

**Android SDK plus the ignored `local.properties`:**

```bash
deepseek-team workspace prepare WORKSPACE_ID \
  --tool JAVA_HOME=/usr/lib/jvm/java-17-openjdk-amd64 \
  --tool ANDROID_HOME=/opt/android-sdk \
  --copy /path/to/local.properties=local.properties \
  --env 'JAVA_HOME={workspace}/.deepseek-tools/JAVA_HOME' \
  --env 'ANDROID_HOME={workspace}/.deepseek-tools/ANDROID_HOME' \
  --env 'ANDROID_SDK_ROOT={workspace}/.deepseek-tools/ANDROID_HOME' \
  --env 'PATH={workspace}/.deepseek-tools/JAVA_HOME/bin:{workspace}/.deepseek-tools/ANDROID_HOME/cmdline-tools/latest/bin:{workspace}/.deepseek-tools/ANDROID_HOME/platform-tools' \
  --probe 'sdkmanager --version' \
  --probe 'adb version' \
  --save-project
```

`local.properties` is usually ignored and often only holds a `sdk.dir` path; when
registered through `--copy` it is fingerprinted privately. Copy it only when its
`sdk.dir` points at the prepared location — otherwise the `ANDROID_HOME` and
`ANDROID_SDK_ROOT` variables above decide the SDK. An absolute `sdk.dir` is not
rewritten when a saved recipe creates another workspace: omit `--copy` from a
reusable Android recipe unless that file remains valid in each copy, and prepare
any required workspace-specific file explicitly. A signing keystore (`.jks`,
`.keystore`) is credential-like and is deliberately rejected — never prepare
signing material.

**Complete offline JDK, Gradle wrapper and Kotlin workflow:**

This is one complete declaration set for a project with a committed Gradle
wrapper. First build a temporary input containing only the required wrapper
distributions and dependency artifacts. Copy that input as one destination so
`GRADLE_USER_HOME` resolves to a prepared directory during validation:

```bash
gradle_snapshot=$(mktemp -d)
mkdir -p "$gradle_snapshot/wrapper" "$gradle_snapshot/caches"
cp -a /path/to/.gradle/wrapper/dists "$gradle_snapshot/wrapper/"
cp -a /path/to/.gradle/caches/modules-2 "$gradle_snapshot/caches/"

deepseek-team workspace prepare WORKSPACE_ID \
  --tool JAVA_HOME=/usr/lib/jvm/java-17-openjdk-amd64 \
  --copy "$gradle_snapshot=gradle-home" \
  --env 'JAVA_HOME={workspace}/.deepseek-tools/JAVA_HOME' \
  --env 'GRADLE_USER_HOME={workspace}/gradle-home' \
  --env 'PATH={workspace}/.deepseek-tools/JAVA_HOME/bin' \
  --probe 'java -version' \
  --probe 'javac -version' \
  --probe './gradlew --offline --version' \
  --cache-max-bytes 8589934592 \
  --cache-max-inodes 400000 \
  --save-project
```

Choose ceilings that fit the selected inputs and available storage; the example
allows an 8 GiB shared cache. The ordinary free-space and inode reservations still
apply. After a successful saved preparation, the temporary input is no longer
needed for replay. Projects without a wrapper instead need an explicitly prepared
Gradle installation, with its `bin` directory added to the workspace-rooted PATH.

If the project needs a standalone Kotlin compiler in addition to the Kotlin Gradle
plugin, add `--tool KOTLIN_HOME=/opt/kotlin`,
`--env 'KOTLIN_HOME={workspace}/.deepseek-tools/KOTLIN_HOME'`, the
`{workspace}/.deepseek-tools/KOTLIN_HOME/bin` PATH element and
`--probe 'kotlinc -version'` to the same complete command. This compiler is optional:
Kotlin Gradle plugin artifacts must already be present in the copied Gradle
dependency caches, or the offline build will fail even when `kotlinc` exists.

Do **not** copy a whole `~/.gradle` directory. In particular, do not copy host
`gradle.properties`, `init.d`, `daemon`, signing configuration or credentials. Copy
only the selected wrapper distributions and dependency caches into the workspace,
and keep all resulting paths rooted at `{workspace}` through `JAVA_HOME`,
`KOTLIN_HOME`, `GRADLE_USER_HOME` and `PATH`. The saved recipe snapshots these
prepared destinations into the cache under the declared aggregate byte/inode
limits, so a later workspace can replay them without a host Gradle home.

Then verify the real build inside the worker namespace before spending a model call:

```bash
deepseek-team workspace verify WORKSPACE_ID \
  --check './gradlew --offline --no-daemon :app:assembleDebug' \
  --timeout 600
```

The probes catch a missing JDK, wrapper distribution or offline dependency before
the provider key is read. They do not promise that every project dependency is in
the copied caches; the real `--offline` build is the acceptance check. A cache-only
recipe without a wrapper distribution or a real Gradle binary on the prepared PATH
cannot build offline.

## Re-verify without another model call

```text
deepseek-team workspace verify ID [--check COMMAND] [--timeout SECONDS] [--json]
```

`workspace verify` rebuilds the exact prepared environment and sparse namespace,
replays any saved recipe and reruns the declared or supplied checks. If no
`--check` is given it uses the privately recorded declared checks; an empty set is
rejected. It **never loads the provider key, starts a relay or starts a model**.

- Exit `0` when the checks pass, `65` when a check fails and `124` when a check
  times out.
- It appends a unique verification run to private `verification_runs`, associated
  with the original task/assignment when one was recorded, and preserves the
  original worker status, error, checks and quality. Re-verification never rewrites
  the failed attempt or its grade and creates no new quality case.
  A publication interrupted after the workspace record is saved remains pending;
  the next verification publishes it once to the original attempt before new checks.
  Default runtime candidates may be rediscovered, while an explicit pin stays fixed.
- Per-stream output is retained up to 65,536 bytes, redacted for known sensitive
  values and published owner-only under the workspace's private `diagnostics/`
  directory; larger output is drained and flagged truncated. Use `--json` for the
  machine-readable result.
- It may inspect an originally failed copy without automatically resuming
  implementation. Continuation still requires an explicit decision. Finalizing the
  job removes this checkout and its owned temporary environments, so integrate and
  re-verify before final acceptance.

## Baseline and evidence causes

Before the model runs, the declared checks are captured as a **baseline**. An
ordinary nonzero project baseline is evidence, not a blanket rejection, so a
bugfix assignment may proceed against an already-red suite. A missing offline
dependency, an unavailable tool, a failed readiness probe or a timed-out launch is
an environment failure and stops the run before provider work. After the worker,
failures stay operational: `kind` is `environment` for timeout/launch/probe evidence
and `verification` for ordinary project-check failures, and a matching exit code
alone is not proof of a matching root cause.

| Cause | Meaning |
| --- | --- |
| `baseline_failure` | The same command already failed in the baseline with an identical non-truncated failure signature. |
| `regression_candidate` | The same command passed in the baseline and failed after the worker. |
| `unknown` | A project-check failure without a matching baseline signature. |
| `timeout` / `launch_failure` / `probe_failure` | Environment causes; the model is not blamed. |

Automatic terminal feedback for a failed project check is neutral `unknown` until
the coordinator reviews the actual diff and evidence. Only a reviewed explicit
quality/disposition grades the worker, and a previous explicit worker failure is
preserved when a later rerun passes.

## Related

- [Hardening](https://github.com/kirill31337/deepseek-team/blob/main/docs/HARDENING.md) — namespace, credential and readiness boundaries.
- [Verification](https://github.com/kirill31337/deepseek-team/blob/main/docs/VERIFICATION.md) — release-verification records.
- [0.8.9 release notes](https://github.com/kirill31337/deepseek-team/blob/main/docs/releases/0.8.9.md).
