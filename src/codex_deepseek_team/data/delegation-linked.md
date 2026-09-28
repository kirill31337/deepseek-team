## DeepSeek Team: linked bootstrap (short form)

This runtime opted into the **linked bootstrap**. The full, authoritative delegation
guide is human-maintained at `docs/agents/delegation.md` in this repository root;
this package never generates, overwrites or replaces that guide.

**Before any substantive source work, planning or delegation you MUST read
`docs/agents/delegation.md`.** Treat it as the source of truth, not this block.

Always read the live state; never reuse a profile snapshot from an earlier session.
This block intentionally records no effective-profile snapshot:

```bash
deepseek-team status
deepseek-team config show --effective --instructions --runtime {runtime}
```

- `deepseek-team off` stays authoritative: while disabled, continue locally and do
  not delegate. Fresh or current access is never widened automatically, and
  read-only never becomes write access.
- Plan every delegated deliverable before assigning it, and keep the native-subagent
  exception rules from the linked guide; nothing here relaxes either rule.
- The coordinator still owns architecture, integration, security, final
  verification, secrets/signing and commit/push. The mandatory Linux worker OS
  sandbox is retained, not weakened.
