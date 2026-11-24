---
name: orchestrator
description: "Use when coordinating a Factonio software factory workstream on any registered GitHub repository, from intake and Definition through parallel Implementation, isolated Verification, and PR merge. Do not use inside headless worker cards."
license: MIT
---

# Factonio Orchestrator

Coordinate software delivery on registered GitHub repositories across Definition, optional Phase 1b Design (interactive Claude Design `/design` or pure-Hermes fallback), Implementation, and isolated parallel Verification using the dedicated `factonio` worker profile.

## When to Use

- The user asks Factonio to build a feature, fix a bug, or resolve a GitHub issue on a repository.
- A Factonio Kanban card completes, blocks with `needs_input`, or triggers a watchdog alert.
- Do not load this skill for quick one-line local edits outside Factonio or inside headless `factonio` worker cards.

## Prerequisites

- The repository is registered via `factonio_add_repo` (or `hermes factonio add owner/repo`).
- The dedicated `factonio` worker profile has been initialized via `hermes factonio init`.

## Workflow

### 1. Intake and Clarification

1. Confirm the target repository is registered using `factonio_status`. If missing, register it with `factonio_add_repo`.
2. Ask at most one concise batch of clarifying questions if scope, acceptance criteria, or constraints are genuinely ambiguous.
3. If the user asks to change a registered repository's settings (merge policy, max fix rounds, default branch, design provider/approval), call `factonio_update_repo(repo, <only the fields to change>)` (CLI: `hermes factonio update <repo> --merge-policy ask ...`). Pass `"inherit"` for `design_provider`/`design_approval` to clear the per-repo override and fall back to the global default. Do not re-run `factonio_add_repo` just to edit settings.
4. Choose the entry path:
   - **Feature (`kind="feature"`):** Definition -> optional Phase 1b Design (when `has_ui: true` or requested) -> parallel Implementation -> parallel Verification -> Merge.
   - **Bug (`kind="bug"`):** Definition (diagnosis mode) -> Implementation -> parallel Verification -> Merge.
   - **Small obvious change (`kind="quick"`):** Implementation -> Verification -> Merge.
5. If the user reports a regression in work that previously passed Verification, pass `escaped_from="<verify_card_id>"` to `factonio_start` so escape rate stays accurate.

### 2. Optional Phase 1b Design (UI Work)

1. When Definition completes with `has_ui: true` (or the user explicitly requests a design pass) and `design_provider` is not `off`, call `factonio_spawn_design(repo, tenant, title=..., brief=..., parents=[define_task_id])`.
2. Non-UI implementation units from the spec DAG can still run in parallel; UI units wait on Design approval.
3. When the Design card completes:
   - Inspect `design_dir`, `artifact_url` (if `claude-design` ran), `preview_files`, and `screenshots` in completion metadata.
   - If `approval_policy == "ask"`: present the `claude.ai/artifact/...` link, inline static HTML previews (`::preview{file="..."}`), and screenshots (`MEDIA:...`) to the user and wait for explicit approval (or revision feedback) before spawning UI Implementation cards.
   - If `approval_policy == "auto"`: proceed directly to Implementation.

### 3. Fan-Out Implementation

1. When Definition (and approved Phase 1b Design, if applicable) completes, inspect `spec.md` and completion metadata. Never open or quote `holdout/` files in Implementation briefs or chat summaries meant for implementers.
2. Dispatch every independent task unit from the spec DAG in parallel using `factonio_spawn_implement` (which automatically attaches approved `design/` artifacts when present), chaining only true dependencies via `parents`.
3. End your turn after dispatching cards; Kanban notifies you on block or completion. Do not sleep-poll in the foreground.

### 4. Isolated Parallel Verification

1. When an Implementation card finishes with a PR number and `head_sha`, immediately call `factonio_spawn_verify` with `repo`, `tenant`, `pr_number`, `impl_card_id`, and `head_sha`.
2. That launches two independent cards concurrently on the configured Verification model:
   - `factonio:verify-diff` (reads `spec.md` and `git diff`, never `holdout/`).
   - `factonio:verify-blackbox` (runs the built artifact against acceptance criteria and `holdout/`, never reading source code or diffs).
3. If either card returns `verdict: reject`:
   - Check the fix-round count against the repo's `max_fix_rounds` (default `3`).
   - Within budget: spawn or reopen an Implementation fix round with the concrete findings (still never leaking `holdout/` text; report only observable failure symptoms from Black-Box Check).
   - Over budget: stop and escalate the findings and options to the user.

### 5. Merge Gate

1. When both `verify-diff` and `verify-blackbox` complete with `verdict: pass`, call `factonio_pr_gate(repo, pr_number)`.
2. Verify `ready_to_merge: true` and that live `head_sha` matches the verified commit SHA.
3. Follow the repository's `merge_policy`:
   - `auto`: call `factonio_merge_pr(repo, pr_number, expected_head_sha=...)` and report the merged PR URL and verification summary.
   - `ask`: present the PR URL, check results, and verification verdicts and wait for the user's go-ahead.

## Error Handling

- **Card blocks with `needs_input`:** Decide routine implementation choices within approved scope yourself, comment the decision, and unblock; escalate genuine product or policy questions to the user with a short recommendation.
- **Stale HEAD on merge:** If commits landed after Verification passed, re-run `factonio_spawn_verify` on the new `head_sha` before merging.
- **Unregistered repo or missing `gh` auth:** Stop and report the exact prerequisite command (`hermes factonio add owner/repo` or `gh auth login`).

## Example

```text
User: Fix issue #18 on acme/payments where duplicate webhooks double-credit accounts.
Orchestrator:
1. Calls factonio_start(repo="acme/payments", request="Fix issue #18...", kind="bug", issue_number=18)
2. Definition card reproduces the bug, writes spec.md and holdout/duplicate-webhook.md.
3. Orchestrator calls factonio_spawn_implement(...) for the idempotency fix unit.
4. Implementation opens PR #21 (head abc1234) with unit tests green.
5. Orchestrator calls factonio_spawn_verify(repo="acme/payments", tenant="acme-payments-i18", pr_number=21, head_sha="abc1234").
6. Both verify-diff and verify-blackbox return verdict="pass".
7. Orchestrator calls factonio_pr_gate -> ready_to_merge=true -> factonio_merge_pr -> reports merged PR #21.
```
