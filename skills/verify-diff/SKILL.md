---
name: verify-diff
description: "Use inside a Factonio Diff Verification card to independently audit a GitHub pull request diff against spec.md for correctness, edge cases, security, and test quality. Never read holdout scenarios or modify repository code."
license: MIT
---

# Factonio Diff Verification

Independently audit a pull request's code diff against the written specification so defects are caught before merge without mixing with black-box testing.

## When to Use

- Preloaded on `[verify:diff]` Kanban cards running under the `factonio` worker profile.
- Runs in parallel with `factonio:verify-blackbox` on a separate model from Implementation.
- Read-only review: do not edit source files, push commits, or merge the pull request.

## Information Barrier

- Read `spec.md` (if linked), the pull request metadata, and the git diff (`gh pr diff <pr> --repo <owner/repo>`).
- Never search for, open, or read any `holdout/` directory. Holdout scenarios belong exclusively to Black-Box Verification.

## Review Checklist

1. **Exact HEAD check:** Call `factonio_pr_gate(repo, pr_number)` and confirm the live `head_sha` matches the card's expected SHA (note any mismatch).
2. **Spec fidelity:** Check every requirement and non-goal in `spec.md` against the diff. Flag missing requirements or unrequested scope creep.
3. **Correctness and edge cases:** Trace boundary values, error paths, concurrency/race conditions, resource cleanup, and backwards compatibility.
4. **Security and hygiene:** Check for secret leaks, injection vectors, unsafe shell/SQL construction, path traversal, or bypassed validation.
5. **Test substance:** Confirm tests verify real behavior (not mocked tautologies) and cover both happy and failure paths.
6. **Simplicity:** Flag unnecessary abstractions, dead code introduced by the PR, or over-engineered indirection.

## Verdict Rules

- Return `verdict: "pass"` when there are zero blocking findings (minor optional nits do not block).
- Return `verdict: "reject"` for any correctness bug, security issue, missing spec requirement, or missing critical test coverage. Every blocking finding must cite `file:line`, concrete impact, and a specific fix.

## Output Format

Complete the card with `kanban_complete`, passing a short `summary` and this exact `metadata` shape:

```json
{
  "phase": "verify-diff",
  "verdict": "pass",
  "repo": "owner/repo",
  "pr_number": 42,
  "head_sha": "<40-char commit sha>",
  "findings": [
    {
      "severity": "blocking",
      "location": "src/webhooks.py:84",
      "issue": "Retry delay overflows when attempt > 30.",
      "fix": "Cap backoff at max_delay_seconds before exponentiation."
    }
  ]
}
```

## Error Handling

- **PR closed, empty diff, or inaccessible:** Call `kanban_block(kind="needs_input", reason=...)` with the exact `gh` error rather than guessing.
- **Unrelated pre-existing issues:** Mention only if they directly break this PR; do not reject a PR for pre-existing repo debt outside its scope.
