# Tester feedback corrections for 0.10.3

## Independent review follow-up

The review reproduced two additional admission/readiness boundaries. JSON event
names must be strings before set membership, including inside the error handler.
The coordinator made the small adjacent guard correction and added public CLI
list/dict/null/bool/number regressions: the list/dict cases failed with exit 1
before the fix; all now block with exit 2 without a traceback. The coordinator
recorded this correction after the linked native-harness operation terminated;
an active operation correctly prevents changing its review state meanwhile.

A missing shared storage pool is ready only if its nearest existing ancestor
permits effective current-user creation and search on writable storage. An
access check alone is insufficient under root: `/proc` reports write/search
access despite mode 0555 and rejects creating the pool. The linked correction
must check this conservatively without creating probes or changing permissions.

Keep known missing-payload recipe invalidation, but preserve incomplete or
corrupt cache entries and their recipe for inspection. Replay must retain its
original failure/code and provide explicit re-preparation guidance. Legacy tools
and copies need correct `--tool` and `--copy` guidance. These recovery-message
changes do not authorize deleting uncertain cache data or replaying host inputs.

Successful provider-free preparation is a separate operation after a reviewed
execution. Linked corrections retain the execution identity when every later
operation is a completed successful preparation matching current metadata;
pending, unknown, failed or newer execution history still blocks. The real
retained Claude job now plans and starts its linked correction in the same copy.

The user approved implementing the previously investigated corrections, testing
them, and publishing the release. The source baseline is
`78965f9beb5f612bf390737b9d752dde148c73db`; existing `AGENTS.md` changes belong to
the user and are excluded from this work and its commits.

## Required behavior

1. Missing feature information must identify the undeclared fields rather than
   describe the task as high risk. New ordinary substantial Auto assignments
   require the semantic feature card; protected and historical started scopes
   retain their existing contracts. Canonical defaults and old plan/observation
   identities remain unchanged. Kotlin is accepted by routing and lessons, and
   valid feature values are available through errors and a discoverable schema.
2. Offline doctor validates the same shared budget-pool ancestry that managed
   workers will use. Validation is provider-free and does not create pools,
   chmod existing paths, follow unsafe links, or relax ownership/mount rules.
   Unsafe paths identify the precise ancestor and narrowly applicable remedy.
3. Heavy JVM builds use the existing prepared sandbox. Saved external copy
   inputs must be immutable snapshots, and large caches need an explicit bounded
   capacity contract. The toolchain diagnostic determines the smallest compatible
   CLI/recipe change before implementation. Documentation must include wrapper
   distributions, sanitized caches, prepared JDK/environment, offline checks,
   allocation cost, and baseline-versus-environment failure distinctions.
4. Add a public regression for appending a deliverable after a failed assignment.
   Existing IDs/history, idempotent no-ops, stdout JSON and immutable-scope errors
   remain intact. Do not claim the unconfirmed standalone CLI silence is fixed.
5. The shell classifier excludes only literal `/dev/null`, `/dev/stdout`, and
   `/dev/stderr` output destinations. Command mutations, real output paths,
   dynamic targets and uncertain working directories remain guarded.
6. Claude hook installation uses a resolvable absolute package entrypoint and
   safely migrates managed definitions while preserving unrelated settings and
   hooks. Internal PreToolUse failures explicitly block. Native PreToolUse
   `onFailure: "block"` covers process/timeout failures on supporting Claude
   versions (2.1.295+); document that native version boundary. Stop and session
   events retain event-appropriate behavior. Supported qualified Write/Edit names
   use the same normalization as ordinary mutation detection. Native plan-file
   exceptions remain exact and do not authorize general source writes.

## Architecture and safety constraints

- The package remains dependency-free at runtime and supports Python 3.11+.
- Every worker runs `deepseek-flash` with explicit Codex runtime and its assigned
  effort in an owned managed development copy. No worker commits or publishes.
- Worker OS containment, provider relay, read-only Git boundary, permissions and
  explicit read-only policy remain unchanged. No sandbox bypass or global
  AppArmor changes are permitted.
- Tests isolate HOME, CODEX_HOME, CLAUDE_CONFIG_DIR, XDG_CONFIG_HOME, state and cwd;
  setup tests include external configuration canaries. No credentials or raw
  model logs enter source, release notes, artifacts or CI output.
- New schema fields must preserve reads of current records and stable identities
  when their behavior is unchanged. Unsupported future schemas fail explicitly.
- Coordinator-only verification is a separate potential feature, not required to
  resolve the tester's either/or request: this release completes the existing
  executable worker-preparation path and does not add a new host-execution trust
  boundary.
- Existing managed worker copies provide implementation isolation. Integration
  uses the existing checkout with explicit per-file staging excluding user edits;
  no synthetic branch/worktree is created merely to invoke delegation.

## Release acceptance

### Selected saved-copy capacity contract

`workspace prepare --cache-max-bytes BYTES --cache-max-inodes COUNT` is valid
only with `--save-project`. Defaults remain 2147483648 bytes and 200000 inodes;
explicit decimal integers must be in 1..2**63-1 and invalid input fails with 64
before materialization. Limits apply to the whole save batch/shared cache and
are not added to v1 metadata/recipes. Replay reads existing immutable entries;
later finite-cache eviction retains explicit stale-recipe recovery semantics.

Every new saved tool/copy source is snapshotted from its materialized destination.
Keep one cache lock and then the recipe lock through snapshot validation and
atomic recipe publication. Protect entries already selected by the batch and
valid cache entries referenced by the prior recipe until publication; capacity
failure preserves the prior recipe and usable referenced entries. Existing safe
cache-backed v1 recipes replay unchanged. Legacy external-copy recipes are parsed
but require explicit re-saving before fresh replay: their original content cannot
be attested. Preserve those records and provide actionable guidance instead of
silently migrating them or falling back to current mutable host content. This
intentional correction closes the same immutability bug for upgrades. Do not add a cache
digest shortcut to preparation guards/check reuse: unknown large inputs keep
their existing conservative behavior.

Run the complete repository unittest command after integration, current real
Claude CLI integration against a deterministic local API fixture, wheel/sdist
build and strict Twine checks, and pip/pipx/uv installation lifecycle checks.
Obtain an independently routed review and resolve material findings. Update both
version declarations and release notes. Publish the reviewed tag and verify
GitHub CI assets before uploading those exact artifacts to PyPI through the
existing configured route. Verify registry hashes and a clean installation.
