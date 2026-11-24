---
name: implement
description: "Use inside a Factonio Implementation Kanban card to implement one scoped work unit in an isolated git worktree, run tests, open or update a GitHub pull request, and address verification fix rounds. Never read holdout scenarios."
license: MIT
---

# Factonio Implementation Phase

Deliver one scoped, tested change inside your assigned git worktree and publish a clean GitHub pull request ready for independent verification.

## When to Use

- Preloaded on `[implement]` Kanban cards running under the `factonio` worker profile.
- Also used for bounded fix rounds when Diff Check or Black-Box Check returns findings.
- Do not use to merge pull requests directly unless explicitly instructed; the orchestrator runs the merge gate after independent verification.

## Information Barrier

- Read only the repository files, `spec.md` (if linked), and review/fix findings attached to this card.
- Never search for, open, or reference any `holdout/` directory or hidden test files. Holding back those scenarios is what keeps Black-Box Verification independent.

## Procedure

### 1. Orient in the Worktree

1. Confirm `git status` and `git branch --show-current` inside the worktree.
2. Read the card's Work Unit section, `spec.md` (if present), and the repository's `AGENTS.md` or contributing guidelines.
3. Inspect existing code and tests before editing; match repository conventions and reuse existing utilities.

### 2. Implement and Verify Locally

1. Write failing tests (or a reproducing test for bugs) first when the repository has a test suite.
2. Make minimal, surgical code changes traced directly to the work unit. Avoid unrelated refactors.
3. Run the repository's build, lint/typecheck, and test commands until every check passes cleanly.

### 3. Publish or Update the Pull Request

1. Stage and commit only intended files with a clear commit message.
2. Push the branch: `git push -u origin HEAD`.
3. Create a PR if none exists yet:
   ```bash
   gh pr create --repo <owner/repo> --base <default_branch> --title "<concise title>" --body "<summary + test output>"
   ```
   If updating an existing PR for a fix round, push the fix commits to the same branch and leave a brief comment summarizing what changed.
4. Capture the live PR number, URL, and `head_sha` via `factonio_pr_gate(repo, pr_number)`.
5. If CI checks fail, inspect the failed logs (`gh run view <id> --log-failed`), fix the cause, push, and re-check `factonio_pr_gate` until checks pass or you hit `max_fix_rounds`.

### 4. Complete the Card

Call `kanban_complete` with a concise `summary` and structured `metadata`:

```json
{
  "phase": "implement",
  "repo": "owner/repo",
  "pr_number": 42,
  "published_pr": "https://github.com/owner/repo/pull/42",
  "head_sha": "<40-char commit sha>",
  "changed_files": ["src/foo.py", "tests/test_foo.py"],
  "checks_run": ["pytest -q"]
}
```

## Error Handling

- **Merge conflicts with base branch:** Fetch and rebase cleanly on `origin/<default_branch>`, re-run tests, and force-push with `--force-with-lease`.
- **Missing secrets or external service required for tests:** Do not fabricate passing output; record the exact blocker and call `kanban_block(kind="needs_input", reason=...)`.
- **Exceeded fix rounds:** Stop pushing speculative changes and block with `needs_input` summarizing what still fails and why.

## Example

```text
Work unit: Add exponential backoff to webhook retries (spec.md unit-1).
Steps:
1. Read spec.md and src/webhooks.py.
2. Add test_retry_backoff in tests/test_webhooks.py (confirm it fails).
3. Implement capped backoff in src/webhooks.py; run pytest (14 passed).
4. Commit, push, run `gh pr create`, verify green checks with `factonio_pr_gate`.
5. Call `kanban_complete` with pr_number, published_pr, head_sha, and changed_files.
```
