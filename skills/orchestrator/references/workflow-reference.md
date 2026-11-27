# Factonio Orchestrator Workflow Reference

Supporting detail for `skills/orchestrator/SKILL.md`. The skill is the operational contract; this file holds CLI equivalents, resolution chains, and examples. Load it with `skill_view(name="factonio:orchestrator", file_path="references/workflow-reference.md")`.

## Repository settings (§1)

- `factonio_update_repo(repo, ...)` edits merge policy, publish policy, max fix rounds, default branch, `default_rigor`, and design provider/approval. CLI: `hermes factonio update <repo> --merge-policy ask --publish-policy ask ...`.
- `publish_policy: auto` inherits from `default_publish_policy` when omitted. An explicit null, empty, or invalid value is refused.
- Pass `"inherit"` for `design_provider` / `design_approval` to clear the per-repo override and fall back to the global default.

## Arena (§1b)

- CLI spawn: `hermes factonio arena spawn <repo> --request "..." --candidate <provider/model> ...`. Candidates default to the configured `verify_panel`.
- Candidate outputs: `spec.md` + `holdout/` for `define`, `design/` for `design`, under `runs/<tenant>/arena/<arena_id>/candidates/<slot>/`.
- Status: `hermes factonio arena status --arena-id <id>` (read-only Kanban).
- Design judges write canonical `DESIGN.md` plus one self-contained zero-build `preview.html` variant switcher (Candidate A / Candidate B / ... / Synthesis, plus side-by-side comparison) in the canonical design directory.

## Design approval (§2)

- `artifact_url` is set when `claude-design` ran. Present `claude.ai/artifact/...` links, inline static HTML previews (`::preview{file="..."}`), and screenshots (`MEDIA:...`).

## Verification (§4)

- Harness: `hermes factonio harness init <repo>` bootstraps `.factonio/`; review and commit it like code. The harness stages are `Launch -> Doctor -> Drive -> Evidence -> Cleanup`. It is never injected into Diff Check.
- Panel resolution order: override `panel_models` -> `repos.<slug>.models.verify_panel` -> `auxiliary.factonio_verify_panel` -> `settings.verify_panel`. Router ids such as `openrouter/anthropic/...` count as their real vendor.
- Every Black-Box lane keeps `holdout/` and the harness; every Diff lane keeps the barrier.

## Local-first publish (§4b)

- Diff Check first confirms `refs/heads/<branch>` still equals the expected SHA.
- Panel CLI: `hermes factonio panel --branch <branch>`.
- Publish CLI: `hermes factonio publish-pr <repo> <branch> --expected-head-sha <sha> --title ... --body-file ...`. It pushes exactly `<sha>:refs/heads/<branch>`, opens the PR, and reads it back. Publishing never bypasses branch protection or required checks.

## Merge gate details (§5)

- Tier 3 includes a `BLOCKED` merge state with a failing head `statusCheckRollup`. Review automation covers Bugbot, security review, and PR review bots; their findings arrive as review threads.
- Tier 4 `approval_gates` are `ACTION_REQUIRED` / Code Review Gate checks.
- `factonio_pr_gate(repo, pr_number, verified_head_sha=...)` reports `patch_continuity`. The default `factonio_merge_pr` refuses any SHA mismatch and merges with `--match-head-commit`.
- Trunk drift needs a registered local checkout. `disjoint` means trunk touched other files; `overlap_rebase_required` means trunk and PR touched the same file; `workflow_rebase_required` means trunk changed `.github/workflows/`.
- Stacked PRs: `factonio_pr_gate` reports `base_branch`, `default_branch`, and `is_stacked`. `factonio_restack_pr` (or `pr_number=<child>`) rebases each child with `git rebase --onto` in a throwaway worktree, pushes with `--force-with-lease`, retargets the PR, and logs `restack_pr`. Pass `delete_branch: false` to `factonio_merge_pr` only to keep a parent branch for a manual follow-up.
- Stack-wide tier order: Tier 1 conflicts/drift -> Tier 2 threads -> Tier 3 CI failures -> Tier 4 human gates -> `lowest_pending_frontier`, plus `ceiling`.
- Local sync after merge (`local_synced` / `local_sync_note`): the registered checkout's default branch fast-forwards only when safe (clean tree on that branch, or a strict fast-forward ref update when another branch is checked out).

## Decision trail rows

- `spawn_verify` rows carry `rigor` / `panel_id`; `publish_pr` rows use phase `publish` with result `published` / `refused` / `error`.
- Recall covers escapes, superseded dead ends, rejects/blocks, and keyword-relevant decisions from other workstreams on the repo.

## Example

```text
User: Fix issue #18 on acme/payments where duplicate webhooks double-credit accounts.
Orchestrator:
1. Calls factonio_start(repo="acme/payments", request="Fix issue #18...", kind="bug", issue_number=18)
2. Definition reproduces the bug and writes spec.md plus hidden scenarios.
3. Orchestrator calls factonio_spawn_implement(...) for the idempotency fix unit.
4. Implementation opens PR #21 (head abc1234) with unit tests green.
5. Orchestrator calls factonio_spawn_verify(repo="acme/payments", tenant="acme-payments-i18", pr_number=21, head_sha="abc1234").
6. Both verify-diff and verify-blackbox return verdict="pass".
7. Orchestrator calls factonio_pr_gate -> ready_to_merge=true -> factonio_merge_pr -> reports merged PR #21.
```
