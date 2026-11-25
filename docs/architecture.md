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

1. `factonio_pr_gate` queries GitHub live via `gh` for PR state, draft status, mergeability, required status checks, and live `head_sha`.
2. `factonio_merge_pr` refuses to merge if CI is red/pending or if `head_sha` drifted after Verification passed.
3. On `merge_policy: auto`, the PR is squash-merged and its branch deleted; on `merge_policy: ask`, the orchestrator presents verification results and waits for human approval.

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

- **Tick Watchdog (`watchdog.py`):** Hooks `on_kanban_dispatch_tick` (throttled by `watchdog_interval_minutes`, default `5`). Flags running cards silent beyond `stale_heartbeat_minutes` (default `20`), `needs_input` cards blocked beyond `blocked_reminder_minutes` (default `45`), and `triage` cards, deduplicating alerts via plugin state.
- **Metrics (`metrics.py`):** Queries `kanban.db` read-only (`?mode=ro`) to compute workstream totals, per-phase median/P90 durations, Diff vs Black-Box pass rates, rework rate (`avg_runs_per_implement_card`), and post-merge escape rate (`escaped_from` cards).
- **Dashboard API (`dashboard/plugin_api.py`):** Mounted at `/api/plugins/factonio/`:
  - `GET /status` — active/blocked counts, repo count, phase models, cross-model warning
  - `GET /repos` — registered repositories with explicit overrides, effective inherited settings, and global defaults
  - `PATCH /repos/{slug}` — validated partial updates (`merge_policy`, `max_fix_rounds`, `default_branch`, `design_provider`, `design_approval`)
  - `GET /models` — resolved per-phase provider/model matrix
  - `GET /metrics?range=7d|30d|90d|all` — factory KPIs and phase timing percentiles
  - `GET /cards?session_id=...&tenant=...` — enriched Kanban cards for the right-sidebar Factonio tab
  - `GET /doctor` — environment and worker profile readiness checks
