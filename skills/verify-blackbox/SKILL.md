---
name: verify-blackbox
description: "Use inside a Factonio Black-Box Verification card to execute hidden holdout scenarios and acceptance checks against the built PR artifact without reading git diffs or implementation source code. Do not edit code or merge PRs."
license: MIT
---

# Factonio Black-Box Verification

Verify observable behavior end to end against acceptance criteria and hidden holdout scenarios with zero knowledge of how the change was implemented.

## When to Use

- Preloaded on `[verify:blackbox]` Kanban cards running under the `factonio` worker profile.
- Runs in parallel with `factonio:verify-diff` so behavioral testing stays free of confirmation bias.
- Read-only verification: do not modify repository source code, push commits, or merge the pull request.

## Strict Information Barrier

This barrier is the entire reason Black-Box Verification runs in an isolated card:
- **Allowed:** Acceptance criteria from `spec.md`, approved `design/` previews and `DESIGN.md` (if linked on this card), every scenario file under the card's `holdout/` directory, build/run/test entrypoints (`README.md`, `package.json`, `Makefile`, `pyproject.toml`), and running the built binary, CLI, API, or web UI (`terminal` or `browser_exec`).
- **Forbidden:** Never run `git diff`, `git show`, `gh pr diff`, or read implementation source files under `src/`, `lib/`, `app/`, etc. Judge the software strictly by what a user or caller observes.

## Procedure

### 1. Prepare an Isolated Checkout of the PR

1. Confirm live PR state and `head_sha` using `factonio_pr_gate(repo, pr_number)`.
2. Inside your scratch workspace, check out the exact PR branch or commit without inspecting diffs:
   ```bash
   gh repo clone <owner/repo> app -- --depth 50
   cd app && gh pr checkout <pr_number>
   git rev-parse HEAD
   ```
   Verify `git rev-parse HEAD` matches the PR's `head_sha`.

### 2. Run Acceptance and Holdout Scenarios

1. Read the Goals and Acceptance Criteria section of `spec.md` (stop before Technical approach).
2. Read every scenario file inside the card's `holdout/` directory.
3. Build or start the application using the documented command or `run_hint`.
4. Execute every holdout scenario and acceptance check, capturing exact commands, exit codes, stdout/stderr, HTTP responses, or browser observations. When an Approved Design directory is linked, render the built UI in a browser and compare visual hierarchy, states, and layout fidelity against the approved design previews.
5. Shut down any temporary server processes you started before finishing.

### 3. Record Verdict

Every Black-Box card ends in exactly one of three verdicts:

| Verdict | When | Effect |
|---|---|---|
| `pass` | Every acceptance criterion and holdout scenario was run against the PR head and succeeded. | Eligible for the merge gate. |
| `reject` | Any scenario fails, crashes, or disagrees with expected observable behavior. | Triggers an Implementation fix round. |
| `inconclusive` | One or more required scenarios or acceptance surfaces could not be run (missing credentials, unreachable service, unbuildable environment, no browser), and none of the runnable ones failed. | Blocks auto-merge; the orchestrator must resolve the gap or ask the user. |

- Return `verdict: "pass"` only when every acceptance criterion and holdout scenario was actually executed and succeeded. Optional environment checks you could not reach may still be listed under `disclosed_limits`, but a skipped **required** scenario means the verdict is not `pass`.
- Return `verdict: "reject"` if any scenario fails, crashes, or disagrees with expected observable behavior. A failure outranks unrunnable scenarios: report `reject`, not `inconclusive`. Describe failures purely in terms of **inputs, observed behavior, and expected behavior** — never quote hidden holdout filenames or full holdout scripts if they would let an implementer hardcode answers; summarize the failing behavioral contract instead.
- Return `verdict: "inconclusive"` when required scenarios could not run. `inconclusive` must never silently pass: do not round it up to `pass`, do not omit it from `summary`, and list **every** skipped or unrunnable surface, scenario, or check under `disclosed_limits` with the exact reason and the command or step that failed to run.
- Any verdict with a non-empty `disclosed_limits` must mention those limits in the card `summary`.

## Output Format

Complete the card with `kanban_complete` using a concise `summary` and this `metadata` shape (`verdict` is one of `pass`, `reject`, or `inconclusive`):

```json
{
  "phase": "verify-blackbox",
  "verdict": "inconclusive",
  "repo": "owner/repo",
  "pr_number": 42,
  "head_sha": "<40-char commit sha>",
  "scenarios_run": 3,
  "scenarios_passed": 3,
  "disclosed_limits": [
    "Payment webhook scenario not run: STRIPE_TEST_KEY unavailable in worker environment (`curl -X POST .../webhook` returned 401)."
  ],
  "findings": []
}
```

## Error Handling

- **Build or startup fails:** Return `verdict: "reject"` with the exact failing build/startup command and stderr output.
- **No holdout scenarios exist (e.g., quick task):** Execute the acceptance criteria and repository smoke/integration tests against the checked-out PR commit, then record `scenarios_run` accurately.
- **Missing external credentials or unreachable deployment:** List each affected scenario under `disclosed_limits` and return `verdict: "inconclusive"` (or `reject` if anything runnable failed); block with `needs_input` only if zero meaningful verification could run.
