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
- **Allowed:** Acceptance criteria from `spec.md`, approved `design/` previews and `DESIGN.md` (if linked on this card), every scenario file under the card's `holdout/` directory, build/run/test entrypoints (`README.md`, `package.json`, `Makefile`, `pyproject.toml`), the repo verification harness `.factonio/verify.md` and `.factonio/harness.json` (black-box playbooks, not implementation source), and running the built binary, CLI, API, or web UI (`terminal` or `browser_exec`).
- **Forbidden:** Never run `git diff`, `git show`, `gh pr diff`, or read implementation source files under `src/`, `lib/`, `app/`, etc. Judge the software strictly by what a user or caller observes. The `.factonio/` harness never lifts this ban: follow its commands, but do not open the source files its Feature Map globs point at.
- **Panel lanes:** a card stamped `- Verification panel: \`vp_...\`` is one independent lane of a `rigor: high` multi-vendor panel. The barrier and harness instructions above apply unchanged to every lane. Do not read, wait for, or echo other lanes' cards or verdicts; return your own `pass`/`reject`/`inconclusive`. Any lane's `reject` or `inconclusive` blocks the panel consensus.

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
3. **Pin an isolated Hermes home before probing CLI/API flows.** Commands such as `hermes factonio init`, `doctor`, `add`, or `harness` read and write `HERMES_HOME` and the Kanban DB. Run them from the scratch checkout only with a throwaway home so real profiles, `kanban.db`, and `plugins/` symlinks are never mutated:
   ```bash
   export HERMES_HOME="$(mktemp -d)"   # own statement: later exports must expand the NEW home
   export HERMES_KANBAN_DB="$HERMES_HOME/kanban.db"
   export HERMES_KANBAN_WORKSPACES_ROOT="$HERMES_HOME/kanban/workspaces"
   mkdir -p "$HERMES_HOME/plugins" && ln -s "$PWD" "$HERMES_HOME/plugins/factonio"   # only when probing Factonio itself
   printf 'plugins:\n  enabled: [factonio]\n' > "$HERMES_HOME/config.yaml"
   ```
   Never run `hermes factonio init --force-link` against a real profile from a scratch checkout; the persistent worker link guard refuses ephemeral targets for exactly this reason. Remove the temporary home during cleanup.

### 2. Run Acceptance and Holdout Scenarios

1. Read the Goals and Acceptance Criteria section of `spec.md` (stop before Technical approach).
2. Read every scenario file inside the card's `holdout/` directory.
3. Build or start the application using the documented command or `run_hint`.
   - **Repo verification harness:** when the card has a `## Repo Verification Harness` section (or the checkout contains `.factonio/verify.md`), run its stages in order: `Launch -> Doctor -> Drive -> Evidence -> Cleanup`. A failing Doctor command means the environment is not ready (fix it or report `inconclusive`, never `pass`). Run every declared hook (`visual_diff`, `invariant`, `custom`) from the checkout, or `hermes factonio harness check . --run-doctor --run-hooks --json`: a failing required hook is a `reject`; a failing or unrunnable optional hook goes under `disclosed_limits`.
   - **Live harness pass:** also run `hermes factonio harness check . --run-doctor --run-hooks --json` and `hermes factonio harness audit . --json` and read their classification: a required hook that runs and fails is a product failure (`reject`); a gate that cannot run (Doctor failure, hook timeout, command not found) is a harness gap (fix the environment or report `inconclusive`); `gate_loosening` findings (required hooks removed or downgraded to optional, Doctor commands dropped) mean the PR weakened its own gates, so report them as findings and never treat the loosened harness as the bar to `pass`.
4. **Drive from the user's point of view.** Before each scenario, establish the preconditions a real user or caller would have, and avoid the gotchas that make a probe lie:
   - **Fresh state:** start from a clean checkout and an isolated `HERMES_HOME`, never from state left by a previous scenario, a cached build, or your own earlier probe.
   - **Real entrypoints only:** use the documented CLI, HTTP API, or UI a user would touch, with the documented flags; do not import internals or call private helpers to "reach" a behavior.
   - **Observe, don't assume:** confirm exit codes, response bodies, files on disk, and rendered UI yourself. A command that prints nothing is not a pass until you check its exit code and side effects.
   - **Common gotchas:** inherited env vars (`HERMES_HOME`, `HERMES_KANBAN_DB`, proxies), stale `PATH` entries, missing `--json`, relative paths resolved from the wrong directory, timezones/clocks, and pagination or truncation hiding later rows.
5. Execute every holdout scenario and acceptance check, capturing exact commands, exit codes, stdout/stderr, HTTP responses, or browser observations. When an Approved Design directory is linked, render the built UI in a browser and compare visual hierarchy, states, and layout fidelity against the approved design previews.
6. Shut down any temporary server processes you started before finishing.

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
- **`verified-unreachable` reporting rule:** when you prove (with a concrete command and its observed output) that a required surface or scenario cannot be reached from the user's point of view in this environment (service down, credential absent, platform unsupported, no browser), record it in `disclosed_limits` as `verified-unreachable: <scenario or surface> — <command> -> <observed output>`. A `verified-unreachable` required scenario makes the verdict `verdict: "inconclusive"`, never `pass`, however strong the remaining evidence. It is outranked by `reject`: if any runnable scenario failed, the verdict is `reject` and the `verified-unreachable` entries stay listed under `disclosed_limits`. "I did not try" is not `verified-unreachable`; run the probe first.

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
    "verified-unreachable: payment webhook scenario — `curl -X POST .../webhook` -> 401 (STRIPE_TEST_KEY unavailable in worker environment)."
  ],
  "findings": []
}
```

## Error Handling

- **Build or startup fails:** Return `verdict: "reject"` with the exact failing build/startup command and stderr output.
- **No holdout scenarios exist (e.g., quick task):** Execute the acceptance criteria and repository smoke/integration tests against the checked-out PR commit, then record `scenarios_run` accurately.
- **Missing external credentials or unreachable deployment:** List each affected scenario under `disclosed_limits` and return `verdict: "inconclusive"` (or `reject` if anything runnable failed); block with `needs_input` only if zero meaningful verification could run.
