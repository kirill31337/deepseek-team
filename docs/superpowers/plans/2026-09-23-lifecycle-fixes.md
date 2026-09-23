# Lifecycle completion fixes

Approved scope: four reproduced defects in draft turns, repeated Stop behavior,
coordinator/native result accounting, and shell mutation classification. The
Android delivery issue has no established cause in this package.

Work uses an isolated worktree from main and preserves existing installation work.
The user authorized publication of reviewed fixes to main.

## Design and ownership

1. Native shell-classifier deliverable: standard-library lexical classification
   preserving quote/heredoc boundaries and removing blanket command exemptions.
   Incomplete write scopes remain unscoped.
2. Native deliverable-outcomes deliverable: accepted/cancelled outcomes permit
   completion; native outcomes do not train coordinator routing. Preserve results
   across compatible replans and invalidate acceptance after later mutation. Close
   unstarted drafts distinctly from successful completion.
3. Coordinator hook-lifecycle deliverable: quiet draft closure, retention of active
   work on status prompts, one continuation request followed by a visible warning
   without a global continuation veto, complete outcome checks, shell integration.
4. Coordinator integration: update guidance, review diffs, run focused/full tests
   and independent code review.
5. Coordinator publication: commit only these fixes, fast-forward push to main,
   and verify the remote commit.

## Verification

Start each implementation with failing regressions. Cover both runtimes, status
turns, pending workers, missing outcomes, replans, stale acceptance, quoted shell
text, heredocs, real redirections and mixed commands. No paid API calls needed.

Clean main baseline passed 499 tests with 3 skips. Final checks:
`PYTHONPATH=src python3 -m unittest discover -s tests -v`, `git diff --check`,
independent review and remote SHA comparison.
