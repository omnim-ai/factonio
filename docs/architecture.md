# Factonio Architecture

Factonio is a pure-Hermes software factory plugin that turns any GitHub repository into a structured, multi-phase delivery pipeline using only Hermes Agent, `git`, and `gh`.

![Factonio Software Factory Pipeline](images/architecture.svg)

## Execution model: Orchestrator + dedicated headless worker

Factonio cleanly separates interactive coordination from autonomous execution:

- **Interactive Orchestrator (your default Hermes profile):** Loads a compact prompt section (`factonio-routing`) and the `factonio:orchestrator` skill. It handles intake, clarifies ambiguity with the user, registers/configures repositories, fans out Kanban cards, monitors verification verdicts, and executes the merge gate.
- **Dedicated Headless Worker (`factonio` profile):** Created by `hermes factonio init` with `approvals.mode: off` and linked directly to the Factonio plugin so workers resolve every `factonio:*` skill. The orchestrator prompt section is automatically suppressed inside worker sessions (`HERMES_KANBAN_TASK` set or active profile equals `factonio`).

Every card spawned by Factonio automatically subscribes the calling chat session (`notify+wake`) so the orchestrator can end its turn immediately after dispatching cards and wake automatically when a card completes or blocks.

## Four-phase workflow and the Information Barrier

### Phase 1 — Definition (`factonio:define`)

- Runs in `goal_mode: true` on the `factonio_define` model slot.
- Explores the repository, diagnoses bugs or designs features, and writes `spec.md` (acceptance criteria, technical approach, parallel task DAG, and `has_ui` flag) inside the workstream's run directory.
- Authors hidden end-to-end acceptance checks under `holdout/`.

### Phase 1b — Optional UI/UX Design (`factonio:design`)

- Spawned when Definition marks `has_ui: true` (or when explicitly requested) and `design_provider != "off"`.
- Supports `auto` (uses interactive Claude Design `/design` PTY when `claude` CLI + credentials are present, else falls back to pure-Hermes HTML/CSS prototyping), `claude-design`, `hermes`, or `off`.
- Writes `DESIGN.md`, static HTML previews, and screenshots into the workstream's `design/` directory without ever seeing `holdout/`.
- Respects `design_approval` (`ask` pauses to present previews to the user; `auto` proceeds directly to Implementation).

### Phase 2 — Parallel Implementation (`factonio:implement`)

- Each independent unit from the spec DAG runs in its own isolated git worktree anchored to a Hermes Project (`<repo-slug>`).
- Workers read `spec.md` and approved `design/` artifacts only — `holdout/` paths are stripped from Implementation card briefs.
- Workers implement changes, run repository tests, push a branch, and open a GitHub PR with its `head_sha`.

### Phase 3 — Isolated Parallel Verification (`factonio:verify-diff` + `factonio:verify-blackbox`)

Every PR launches two concurrent, mutually isolated verification cards on the `factonio_verify` model slot:

| Check | Inputs allowed | Inputs forbidden | Purpose |
|---|---|---|---|
| **Diff Check** (`factonio:verify-diff`) | `spec.md`, `git diff`, PR metadata | `holdout/` | Catch logic errors, security issues, missing edge cases, and scope drift without overfitting to hidden tests |
| **Black-Box Check** (`factonio:verify-blackbox`) | Built artifact, acceptance criteria, `holdout/` | Source code, `git diff` | Verify observable end-to-end behavior without confirmation bias from reading the implementation |

If either check returns `verdict: reject`:

- Within `max_fix_rounds` (default `3`), the orchestrator dispatches a bounded Implementation fix round reporting only observable failure symptoms (never leaking `holdout/` internals), then re-verifies the new `head_sha`.
- Once `max_fix_rounds` is exhausted, the orchestrator escalates the concrete findings to the user.

### Exact-HEAD Merge Gate

Before merging or reporting a PR ready:

1. `factonio_pr_gate` queries GitHub live via `gh api graphql` for PR state, draft status, mergeability (`mergeable`, `mergeStateStatus`), `reviewDecision`, unresolved review threads, head-commit check runs / status contexts, the head `statusCheckRollup.state`, and live `head_sha`. Review threads and check contexts are cursor-paginated (100 per page, up to 20 pages), so PRs with more than 100 checks are gated correctly.
2. Blockers are classified in strict priority tiers and reported in that order, with `blocker_tier` set to the highest-priority active tier (`None` when ready):
   1. merge conflicts (`mergeable == CONFLICTING` or `mergeStateStatus` `DIRTY`/`CONFLICTING`);
   2. unresolved review threads (each returned with `id`, `path`, `line`, `author`, `body` snippet, `created_at`);
   3. failing/pending CI checks, or `mergeStateStatus == BLOCKED` with a head rollup of `ERROR`/`FAILURE`;
   4. merge gates (PR not `OPEN`, draft, or `reviewDecision == CHANGES_REQUESTED`).
3. `factonio_merge_pr` refuses to merge on any blocker or if `head_sha` drifted after Verification passed.
4. On `merge_policy: auto`, the PR is squash-merged and its branch deleted; on `merge_policy: ask`, the orchestrator presents verification results and waits for human approval.
5. After `gh pr merge` exits 0, `factonio_merge_pr` reads the PR back (`state,mergedAt,baseRefName`). Only a confirmed `MERGED` state reports `merged: true`; merge-queue / auto-merge PRs report `merged: false, queued: true`; a PR closed without merging is an error. On a confirmed merge the registered checkout's default branch is fast-forwarded when safe (`local_synced`, `local_sync_note`): a dirty working tree, a diverged branch, or a branch checked out in another worktree is never touched, and a skipped sync never fails the merge.

`repos.py` also provides pure-Python git helpers for verdict continuity: `compute_patch_id` / `verify_patch_continuity` (stable `git patch-id` comparison, so a verified patch survives a mechanical rebase) and `assess_trunk_drift` (files changed on `origin/<default_branch>` vs. the PR since their merge-base).

### Tri-state verification verdicts

Verification cards report `verdict: pass | reject | inconclusive`. `inconclusive` (from run metadata or a run outcome) is never counted as a pass: `/metrics` reports `verification.{diff,blackbox}.inconclusive` and includes it in the `pass_rate` denominator, and `/cards` reports `counts.inconclusive`, which is included in `counts.attention`.

## Per-phase model resolution

Factonio registers five native Hermes Auxiliary Task slots:

- `factonio_define`
- `factonio_design`
- `factonio_implement`
- `factonio_verify`
- `factonio_watchdog`

Resolution precedence (highest to lowest):

1. Per-repository override (`repos.<slug>.models.<subphase>` then `<phase>`)
2. Hermes auxiliary task slot (`auxiliary.factonio_<phase>`) when configured (not `auto`)
3. Fallback to the `factonio` worker profile's default model (then root profile default)

If `implement` and `verify` resolve to the same `(provider, model)`, Factonio surfaces a non-blocking cross-model independence warning in `hermes factonio models`, `hermes factonio doctor`, and the Desktop UI.

## Durable repository storage

Registered repository records live in `<HERMES_HOME>/plugin-data/factonio/repos.json` (read from the root `HERMES_HOME` so worker profiles share the exact same registry as the orchestrator). Legacy entries from `config.yaml` are merged seamlessly and migrated on first update.

Writes acquire an in-process re-entrant thread lock plus an exclusive POSIX `fcntl.flock` on `repos.json.lock`, flush and `fsync` to a unique sibling temp file, and atomically `os.replace()` into place so concurrent CLI, tool, and Dashboard API updates never corrupt the store.

## Watchdog, metrics, and Dashboard API

- **Tick Watchdog (`watchdog.py`):** Hooks `on_kanban_dispatch_tick` (throttled by `watchdog_interval_minutes`, default `5`). Running cards are evaluated in strict order, all deterministic with zero LLM calls: a dead `worker_pid` (POSIX `os.kill(pid, 0)`, skipped when the claim lock names another host) is flagged at once as `dead_worker`; an exceeded `max_runtime_seconds` as `runtime_exceeded`; then side-effect progress (non-heartbeat `task_events`, non-watchdog `task_comments`, worktree commits/index/dirty-file mtimes, and `runs/<tenant>/` file mtimes — metadata only, never file contents) is compared with a phase-aware threshold (`verify-diff` 10m, `verify-blackbox` 15m, `define` 20m, `implement` 25m, `design` 30m; `stale_heartbeat_minutes` above `20` is a floor, below `20` a cap; optional `phase_stale_heartbeat_minutes` per-phase overrides). Fresh side effects mean no alert. Stale side effects with a stale heartbeat raise `stale_running`; with a fresh heartbeat (a looping worker) `no_side_effects`. Only these ambiguous stalls may be triaged by the `factonio_watchdog` model, and only when it is explicitly configured (`auxiliary.factonio_watchdog` or per-repo `models.watchdog`), with a per-task cooldown (`watchdog_triage_cooldown_minutes`, default `15`) and a `<= 15s` timeout: `working` suppresses the alert, `stalled` appends the rationale, `needs_input` becomes `triage_needs_input`; any error, timeout or invalid JSON falls back to the deterministic alert. Blocked cards past `blocked_reminder_minutes` (default `45`), fix-cap cards, and `triage` cards are reported as before. Alerts are deduplicated via plugin state on card revision digest + finding `kind` + current `run_id` (ignoring `factonio:watchdog` comments and volatile age/rationale text), so a repeated condition alerts once while an escalation such as `stale_running` -> `dead_worker`, or a new run, alerts immediately. Workspace progress reads `git status --porcelain=v1 -z --untracked-files=all` with `--no-optional-locks`, so fresh writes inside new untracked directories count and the probe never touches the worker's index.
- **Worktree hygiene (`repos.py`):** `hermes factonio worktrees [repo] [--base <ref>] [--prune-safe] [--json]` audits every linked worktree from `git worktree list --porcelain` (main checkout skipped) with size, age, branch, HEAD, merged-into-base, dirty (`clean` | `wip:N` | `scratch:N`, from `git status --porcelain=v1 -z --ignored=matching --untracked-files=all`: `wip:N` counts tracked edits plus ignored files outside the regenerable cache/build allowlist such as `.env` or local data; `scratch:N` counts untracked files when nothing is protected), remote (`pushed` | `ahead:N` | `no-remote` | `detached`), PR (`#N/state`), and active Kanban card (read-only `kanban.db`), then buckets it by priority `hold-wip` > `hold-active-card` > `hold-open-pr` > `safe` > `review`. The default is read-only; `--prune-safe` removes only `safe` worktrees (re-checking tracked edits and ignored local data first, and listing dropped scratch files as `dropped_scratch`), never deletes branches, and the watchdog never prunes automatically.
- **Metrics (`metrics.py`):** Queries `kanban.db` read-only (`?mode=ro`) to compute workstream totals, per-phase median/P90 durations, Diff vs Black-Box pass rates, rework rate (`avg_runs_per_implement_card`), and post-merge escape rate (`escaped_from` cards).
- **Dashboard API (`dashboard/plugin_api.py`):** Mounted at `/api/plugins/factonio/`:
  - `GET /status` — active/blocked counts, repo count, phase models, cross-model warning
  - `GET /repos` — registered repositories with explicit overrides, effective inherited settings, and global defaults
  - `PATCH /repos/{slug}` — validated partial updates (`merge_policy`, `max_fix_rounds`, `default_branch`, `design_provider`, `design_approval`)
  - `GET /models` — resolved per-phase provider/model matrix
  - `GET /metrics?range=7d|30d|90d|all` — factory KPIs and phase timing percentiles
  - `GET /cards?session_id=...&tenant=...` — enriched Kanban cards for the right-sidebar Factonio tab
  - `GET /doctor` — environment and worker profile readiness checks
