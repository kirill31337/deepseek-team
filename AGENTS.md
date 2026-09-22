# DeepSeek Team

- Preserve user changes and credentials; never include keys, local config or raw model logs in Git.
- Codex or Claude Code may be the coordinator. The coordinator owns architecture, security, integration and final verification.
- Delegate bounded independent work to DeepSeek only when it replaces coordinator work. Use the explicit matching runtime (`--runtime codex` or `--runtime claude`).
- Resolve `deepseek-team config show --effective --instructions` before each assignment. Respect explicit access independently of the 25/50/75 target profile. At 50/75 full-access, delegate independent implementation before doing that same work yourself.
- Full-access workers use owned isolated development copies, may change project files and run local tests/builds. The coordinator prepares copies/dependencies and owns final integration.
- The Linux OS sandbox is required for workers. Do not add `--os-sandbox off` to normal/project-managed workflows and do not disable Ubuntu's AppArmor unprivileged-userns restriction globally to make tests pass.
- Read-only jobs retain the hybrid native Codex / outer Claude boundary. Managed development copies use a sparse outer Bubblewrap namespace for both, a read-only Git directory and a private network with only a fixed provider relay; never launch the managed runtime command outside that boundary.
- Do not duplicate a live worker's assigned investigation. Wait for the full result without an overall timeout; review its actual diff and run meaningful tests.
- Workers must not stage, commit, push, publish or deploy. Read-only jobs cannot write; full-access permits local checks in its owned copy. Managed implementation jobs do not retry automatically; inspect partial work before explicit continuation.
- Run tests with `PYTHONPATH=src python3 -m unittest discover -s tests -v`.
- This repository contains the reusable package only; do not copy unrelated product code, project policies, secrets, or history.
