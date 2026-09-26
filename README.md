![DeepSeek Team banner](https://raw.githubusercontent.com/kirill31337/deepseek-team/main/assets/deepseek-team-banner-4b37ec05.jpg)

# DeepSeek Team

**English** | [Русский](https://github.com/kirill31337/deepseek-team/blob/main/README.ru.md)

[![PyPI version](https://img.shields.io/pypi/v/deepseek-team?cacheSeconds=300)](https://pypi.org/project/deepseek-team/) [![Tests on main](https://img.shields.io/github/actions/workflow/status/kirill31337/deepseek-team/test.yml?branch=main&label=tests)](https://github.com/kirill31337/deepseek-team/actions/workflows/test.yml) [![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue)](#prerequisites) [![Linux](https://img.shields.io/badge/platform-Linux-lightgrey)](#prerequisites) [![MIT license](https://img.shields.io/badge/license-MIT-green)](https://github.com/kirill31337/deepseek-team/blob/main/LICENSE)

**DeepSeek Flash subagents inside your Codex or Claude Code session.**

Delegate implementation, tests and documentation from your existing Codex or Claude Code session. The coordinator plans bounded assignments, reviews worker changes and checks, and integrates accepted results.

The coordinator assesses worker quality separately from problems in the brief, context or environment. A private lessons journal helps improve later assignments.

```bash
pipx install deepseek-team
```

Requires Linux, Python 3.11+ and [pipx](https://pipx.pypa.io/latest/how-to/install-pipx.html). Then [set up your API key, hooks and project](#quick-start).

[Install](#install) · [Quick start](#quick-start) · [How delegation works](#how-routing-works) · [Docs](#further-reading) · [PyPI](https://pypi.org/project/deepseek-team/)

![Workflow diagram: a user request, the Codex or Claude coordinator, DeepSeek Flash workers, then review and integration](https://raw.githubusercontent.com/kirill31337/deepseek-team/main/assets/deepseek-team-demo.gif?v=fc55527882e4)

*Workflow illustration with `full-access` enabled.*

This README follows the `main` branch. Install the latest released package from [PyPI](https://pypi.org/project/deepseek-team/) and see [Releases](https://github.com/kirill31337/deepseek-team/releases/latest) for version notes. It installs the single `deepseek-team` executable.

Workers use the `deepseek-flash` model and a separate **DeepSeek API key**. By default the coordinator chooses the reasoning effort (`low`, `high` or `max`) for each assignment; you can also save a fixed level. `low` suits bounded or mechanical work, `high` is the normal case, and `max` covers difficult debugging, cross-file reasoning and adversarial review. If no level is selected, the worker runs with `high`. The legacy value `medium` is still accepted as an alias of `high`, so old settings and commands keep working. See the DeepSeek [thinking mode guide](https://api-docs.deepseek.com/guides/thinking_mode/) when reasoning depth matters.

Fresh installations use **read-only** access: workers inspect the project and report their findings. To let them change files inside isolated development copies, explicitly enable `full-access`.

## Prerequisites

- **Linux.** Native Windows and macOS are not supported.
- **Python 3.11 or newer** and **Git** on `PATH`.
- **Codex CLI and/or Claude Code CLI** on `PATH`, normally configured. Install whichever coordinator you intend to use; `both` covers both.
- **Bubblewrap** (`bwrap`) and a working Linux OS sandbox. Every worker needs it before any credential is read, and it cannot be turned off.

On Ubuntu, `setup --with-sandbox` can install the required system components (see below). On other distributions, install Bubblewrap and any AppArmor prerequisites with your own package manager first, then check `deepseek-team sandbox status`.

## Install

The package is published on [PyPI](https://pypi.org/project/deepseek-team/), so install the released package by name. [pipx](https://pipx.pypa.io/latest/how-to/install-pipx.html) keeps the CLI in its own environment and is the recommended route:

```bash
sudo apt-get install pipx      # Ubuntu, once
pipx ensurepath
# Open a new terminal so PATH is refreshed.
pipx install deepseek-team
```

Compact alternatives, if you already prefer another manager:

```bash
# uv
uv tool install deepseek-team

# pip inside a virtual environment only
python3 -m venv ~/venvs/deepseek-team
. ~/venvs/deepseek-team/bin/activate
python -m pip install deepseek-team
```

To install from a local checkout instead:

```bash
git clone https://github.com/kirill31337/deepseek-team.git
cd deepseek-team
pipx install .
```

## Quick start

`setup` prepares your user-level integration. `init` then attaches the Git project where you want to use delegation.

```bash
# 1. Prepare the user-level integration for Codex. --no-key defers the credential.
deepseek-team setup --runtime codex --no-key

# 2. Store the DeepSeek API key. The prompt is hidden; the key is never echoed.
deepseek-team auth set

# 3. Attach a project so its coordinator reads the delegation instructions.
cd /path/to/project
deepseek-team init --coordinator codex .

# 4. Optional: allow implementation inside an isolated development copy.
deepseek-team config set --project --access full-access

# 5. Confirm local readiness. This stays offline: no key is read and no request is sent.
deepseek-team doctor --runtime codex --offline
```

A few notes:

- `--no-key` deliberately defers authentication so `setup` never prompts for or reads a secret. Store the key separately with `deepseek-team auth set`, or omit `--no-key` on a terminal when you are ready.
- Fresh access defaults are **read-only**. Step 4 lets Auto delegate implementation by explicitly granting write access. Full-access means a private, owned development copy, not the host system. When `setup` finishes it explains this and prints the opt-in command; the notice changes nothing by itself.
- On Ubuntu, the very first setup may need to install the sandbox package and a named AppArmor profile. Use `deepseek-team setup --runtime codex --with-sandbox --no-key` for that explicit, administrator-authorized step. Ordinary setup never invokes `sudo`; on other distributions install the system prerequisites yourself.
- For Claude Code, substitute `claude` for `codex` in `--runtime` and `--coordinator`, or pass `both` to prepare both coordinators.
- Codex owns native hook trust: review and trust the installed hook once with `/hooks`. In Claude Code, start a fresh session and check `/hooks` there. The package cannot trust hooks for you, and after an update you should restart the session so refreshed guidance loads.
- Attached projects keep their own managed blocks: on each session start and prompt, an explicitly attached project checks and, when stale, updates both existing `AGENTS.md` and `CLAUDE.md` blocks from the current packaged template and effective settings. Unattached projects are never created, scanned or registered.

## What gets delegated

Auto delegation admits suitable work immediately; it does not wait for prior history to accumulate. A task is a good candidate when it is:

- small or medium, with low or medium risk;
- localized to a known or partially known place, with local or component-level coupling;
- clear about acceptance, with a way to check the result.

Implementation needs an executable check - tests, a build or a reproducer. Review, research and documentation tasks may use manual acceptance criteria instead. Unknown costs never block eligible work, and missing or uncertain quality history never blocks an eligible task. Measured economics that do not justify delegation, or a sufficiently supported poor local quality history for a task class, can keep the task with the coordinator; as that evidence ages out, admission resumes.

Every delegated subtask - even a small read-only search or review - is planned and routed before another agent is assigned; in Auto a resolved worker decision means DeepSeek. The coordinator's own native subagents are an explicit exception that needs a recorded reason and either a native capability a DeepSeek worker cannot reach or an explicit user request.

The coordinator also treats an unknown task attribute as unknown: it registers a bounded diagnostic instead of claiming a safe implementation, and it batches independent, eligible scopes into one plan before duplicating that work.

## How routing works

The coordinator classifies each task before execution. Routing, quality estimates and review use that recorded context:

- **Classifier - the coordinator, before execution.** The coordinator records what the task is on a structured *feature card*: kind, domain, operation, localization, coupling, risk, scope size, clarity and verification. Unspecified attributes stay `unknown` instead of being guessed, and protected work - architecture, security, integration, final verification, secret signing and production - stays with the coordinator.
- **Admission and router.** Admission checks the task criteria, access, cooldown, measured economics and learned local quality, then the router chooses worker or coordinator within those rules. Auto admits eligible bounded work immediately, and access is never widened automatically.
- **Estimator.** Comparable recorded cases produce a rubric-based quality estimate with an uncertainty interval. It is a descriptive estimate from real cases, not a calibrated probability of success on your code, and it never predicts a universal delegation percentage.
- **Reviewer and feedback.** After checking the real diff and the declared checks, the coordinator records the observed outcome and, separately, the worker's *graded quality*: `met`, `minor_gaps`, `major_gaps`, `unusable` or neutral `unassessable`, attributed to the worker, shared, coordinator, environment or unknown. A brief, context or requirements gap does not lower worker quality on its own, and only worker/shared `major_gaps` or `unusable` count toward a failure pause.

Recorded outcomes - clean successes included - go into a private, per-project, append-only **lessons journal** kept outside Git. The coordinator reviews it when due (10 distinct newly reviewed or updated cases, or 3 distinct rework cases with the same task kind and a known cause) and may apply bounded, versioned, advisory rules that improve later briefs; a review may also explicitly change nothing. There is no model training and no background model call, and public benchmark evidence is used only when it is explicitly imported - public results are never scraped or bundled automatically.

Inspect the local state without changing it:

```bash
deepseek-team routing status --path PROJECT --json
deepseek-team routing stats --path PROJECT           # quality by category; --json for full counts
deepseek-team lessons status --path PROJECT --json
deepseek-team lessons review --path PROJECT --json  # review bundle only; nothing is applied
```

`routing stats` groups the coordinator's recorded worker outcomes by task category and is read-only; `lessons review --json` only prints the review bundle for the coordinator to act on. The [routing guide](https://github.com/kirill31337/deepseek-team/blob/main/docs/ROUTING.md) and the [delegation lessons guide](https://github.com/kirill31337/deepseek-team/blob/main/docs/DELEGATION_LESSONS.md) cover the full evidence rules and payloads.

Access is independent of effort and history:

- **Read-only** (fresh default): workers inspect and report, and cannot modify the project.
- **Full-access** (explicit): workers implement, run local checks and write documentation inside an isolated copy owned by the job.

Workers never commit, push, publish, deploy, touch production services or spawn further agents. Those remain coordinator actions.

## Asking for help in a session

Once setup and attachment are done, you do not run workers by hand. Ask in your normal coordinator session, in plain language:

> Implement the new cache layer under `src/cache/`. Delegate the independent implementation and its tests to DeepSeek workers, run the test suite inside the worker copies, and integrate only what you have verified. Keep the public API stable and show me the final diff before you commit.

The coordinator splits that into bounded assignments, runs them through the worker queue and reports the accepted work.

Final-summary guidance ships with the package for both Codex and Claude Code. While DeepSeek Team is enabled, summaries of performed work include short bullets separating the coordinator's personal work from accepted DeepSeek results, including any rework or failed attempts, and then an approximate coordinator/DeepSeek split in whole-number percentages labelled **“subjective estimate, not measured.”** The split reflects accepted scope and complexity with review and rework; it is never derived from call, task, file, line, token or time counts and is not router feedback. Session start injects the current policy for Codex and Claude Code; when a block refresh changes or fails, the next prompt supplies fresh guidance so a stale loaded block does not linger. Updating the package refreshes hook guidance, each attached project refreshes its own blocks at the next session or prompt, and `setup` refreshes the blocks in its current repository. Run `init` only to attach a project for the first time or to deliberately repair its blocks.

## Current defaults

| Setting | Current default | Notes |
| --- | --- | --- |
| Delegation | `auto` | Chooses the executor per task; no fixed quota. |
| Access | `auto` -> read-only in Auto | Full-access enables isolated implementation. |
| Model | `deepseek-flash` | Fixed for all workers. |
| Effort | `auto` | Coordinator picks `low`/`high`/`max` per assignment; `medium` is a legacy alias of `high`. |
| Workers | `8` | Configurable 1-64; further jobs queue FIFO. |
| Total timeout | unlimited | An explicit timeout also includes queue time. |

Inspect or change the settings per project:

```bash
deepseek-team config show --effective
deepseek-team config set --project --max-workers 8
deepseek-team on      # enable delegation for this project
deepseek-team off     # disable it without deleting settings
deepseek-team status  # show the saved and effective state
```

The optional manual **25/50/75** profiles are target distributions, not measured quotas. Saved manual profiles retain priority over Auto, while recorded outcomes continue to inform learning. With `access=auto`, profile 25 uses read-only and profiles 50/75 use full-access. An explicitly saved `read-only` setting always takes priority. See the [routing guide](https://github.com/kirill31337/deepseek-team/blob/main/docs/ROUTING.md) for how admission, evidence and feedback work.

The package ships the delegation rules and the managed blocks. Each project keeps its own access, manual profile, `effort`, `max_workers` and `off` state, and its private lessons journal stays local, so an upgrade refreshes the shipped rules but never changes those saved choices, exports full-access settings or learned rules, or promises an identical delegation split.

## Isolation

<a id="workspace-and-branch-hygiene"></a>

Workers require the Linux OS sandbox with Bubblewrap. Full-access work runs in an owned development copy with restricted filesystem and network access. Read-only and full-access workers have different filesystem, credential and network limits, and worker copies are never deleted automatically. The coordinator retains architecture, security decisions, final verification and integration. See [Hardening](https://github.com/kirill31337/deepseek-team/blob/main/docs/HARDENING.md) for the exact rules.

<a id="preflight-and-recovery"></a>

Before allocating a copy, the coordinator can run an optional read-only preflight over the committed source: `deepseek-team workspace check --json` reads only committed `HEAD` names and modes, calls no model, creates no copy, and exits `0` when eligible or `78` when blocked. A failed job retains its copy for inspection with `deepseek-team workspace show WORKSPACE_ID`; continuation stays explicit, and there is no automatic cleanup engine. See [Hardening](https://github.com/kirill31337/deepseek-team/blob/main/docs/HARDENING.md) for the full preflight and recovery rules.

## Diagnostics

```bash
deepseek-team doctor --runtime codex --offline   # local readiness; no key read
deepseek-team hooks status --runtime codex       # whether managed hooks are installed
deepseek-team sandbox status                     # Bubblewrap and AppArmor backend
deepseek-team auth status                        # whether a saved key exists
```

These checks make no provider requests. Local readiness does not validate the key with DeepSeek; live worker requests use your DeepSeek API account.

## Update and uninstall

Upgrade the released package through the same manager, then run `setup` once for each coordinator runtime you installed:

```bash
pipx upgrade deepseek-team
deepseek-team setup --runtime codex --no-key   # or claude / both
deepseek-team doctor --runtime codex --offline
```

`setup` updates the selected user-level hooks and refreshes both managed instruction files (`AGENTS.md` and `CLAUDE.md`) in the repository you run it from when they are already attached. Every other attached project refreshes its own blocks automatically on its next session or prompt, so repeating `init` in each existing project is no longer required; run `init` only to attach a project for the first time or to deliberately repair its managed blocks. Only the package-owned blocks are rewritten, so your own instruction text and saved settings are preserved. Installing or upgrading with pip, pipx or uv does not run setup automatically. Run `setup` to update hook definitions; native trust is reviewed separately in the coordinator. A project that stays closed refreshes when you next open it. With uv, use `uv tool upgrade deepseek-team`. In a virtual environment, use its `python -m pip install --upgrade deepseek-team`. Keep one install channel per machine; a local-clone pipx installation is refreshed with `pipx install --force .` from the updated clone.

To remove DeepSeek Team, detach each project before uninstalling. If you also want to delete the saved DeepSeek key, run `deepseek-team auth remove` while the command is still installed.

```bash
deepseek-team detach --coordinator codex /path/to/project   # for each attached project
deepseek-team reset --runtime codex                         # remove managed integration
pipx uninstall deepseek-team
```

`detach` preserves your own instruction content, and `reset` removes only the package-owned provider block and hooks; your primary authentication and unrelated configuration are kept. Saved keys are **not** deleted by uninstall. For uv use `uv tool uninstall deepseek-team`; for a venv use its `python -m pip uninstall deepseek-team`. Substitute `claude` or `both` for `codex` wherever your runtime differs.

## Further reading

- [Routing guide](https://github.com/kirill31337/deepseek-team/blob/main/docs/ROUTING.md) - classifier, router, admission, evidence and feedback.
- [Delegation lessons](https://github.com/kirill31337/deepseek-team/blob/main/docs/DELEGATION_LESSONS.md) - the private journal, review cadence and bounded advisory rules.
- [Hardening](https://github.com/kirill31337/deepseek-team/blob/main/docs/HARDENING.md) - coordinator and worker boundaries, preflight and recovery.
- [Publishing](https://github.com/kirill31337/deepseek-team/blob/main/docs/PUBLISHING.md) - release-maintainer details.
- [Releases](https://github.com/kirill31337/deepseek-team/releases) - version notes for every release.
- Russian README: [README.ru.md](https://github.com/kirill31337/deepseek-team/blob/main/README.ru.md)

## License

[MIT](https://github.com/kirill31337/deepseek-team/blob/main/LICENSE), copyright 2026 kirill31337.
