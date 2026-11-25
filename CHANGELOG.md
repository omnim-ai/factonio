# Changelog

All notable changes to Factonio are documented in this file.
Every user-facing pull request adds an entry under `## Unreleased`.

## Unreleased

- Watchdog liveness and worktree hygiene: the dispatch-tick watchdog now flags dead `worker_pid`s (`dead_worker`) and exceeded `max_runtime_seconds` (`runtime_exceeded`) immediately, uses phase-aware stale thresholds (`resolve_stale_heartbeat_minutes`), detects heartbeating workers with no side effects (`no_side_effects`), and optionally triages ambiguous stalls with an explicitly configured `factonio_watchdog` model under a per-task cooldown and 15s timeout with deterministic fallback. New `hermes factonio worktrees [repo] [--base <ref>] [--prune-safe] [--json]` audits git worktrees into `hold-wip` / `hold-active-card` / `hold-open-pr` / `safe` / `review` buckets and removes only `safe` ones on request.
- Hardened the PR gate and merge flow: `factonio_pr_gate` now uses paginated `gh api graphql` (>100 checks, review threads, `reviewDecision`, head rollup state) with 4-tier blocker classification (`blocker_tier`, `unresolved_threads`); `factonio_merge_pr` confirms the live merge state (`merged` vs. `queued`) and safely fast-forwards the local default branch; added `git patch-id` continuity and trunk-drift helpers; and verification metrics/cards support tri-state `inconclusive` verdicts.
- Upgraded the worker execution contract (`WORKER_SOUL_MD`) and all five phase skills with pstack-derived rigor: Architect step, empirical prototypes, Throughput Checkpoint and binary-search bug diagnosis (`define`); Attack the Premise, skeptical review-thread triage with prompt-injection hygiene, comment hygiene and a `<=40-line` PR briefing (`implement`); run-proven findings, blast-radius proof and base-vs-PR attribution (`verify-diff`); tri-state `pass`/`reject`/`inconclusive` verdicts (`verify-blackbox`); and the 4-tier PR gate, live merge readback, fresh-worker fix rounds and `git patch-id` verdict continuity (`orchestrator`). Credited Lauren Tan (@poteto) and pstack in `README.md` and added `THIRD_PARTY_NOTICES.md` with pstack's MIT notice.
- Added Concept A ("Machined Winged Cogwheel") Factorio × Hermes brand assets under `docs/images/` (dark/light README banners, dark/light/monochrome logo variants, PNG exports, and 1280×640 2:1 social/marketplace cover), wired the light/dark `<picture>` hero banner into `README.md`, and extended `tools/check_repo_integrity.py` to verify HTML `srcset` paths.
- Moved canonical repository to `omnim-ai/factonio` and updated `plugin.yaml` metadata (`homepage`, `tags`).
- Added GitHub Actions CI pipeline (lint, manifest/link integrity, full 90-test suite against pinned Hermes Agent, and changelog gate) plus daily upstream Hermes compatibility checks and Dependabot config.
- Added open-source contributor documentation: `CONTRIBUTING.md`, `SECURITY.md`, `AGENTS.md`, `docs/development.md`, `docs/architecture.md`, PR/issue templates, and visual `README.md` with Desktop UI screenshots, architecture diagram, and animated feature demos.
- Added project-local contributor skill `.agents/skills/update-readme-media/SKILL.md`, upgraded `tools/capture_readme_media.py` (`--check-deps`, `--only`, `--qa-dir`, cross-platform browser discovery, image header validation), and added the mechanical `README media freshness` CI check (with `no-visual-change` label override) and review checklist.
- Added reproducible media capture harness (`tools/capture_readme_media.py`) and static integrity checker (`tools/check_repo_integrity.py`).

## 0.1.0

- Initial pure-Hermes, phase-based software factory plugin (`factonio`).
- Dedicated headless `factonio` worker profile setup and readiness diagnostics (`hermes factonio init`, `hermes factonio doctor`).
- Four-phase orchestration across Definition (`factonio:define`), optional Phase 1b UI/UX Design (`factonio:design` with Claude Design `/design` and pure-Hermes fallback), parallel git-worktree Implementation (`factonio:implement`), and mutually isolated parallel Verification (`factonio:verify-diff` + `factonio:verify-blackbox` separated by a `holdout/` information barrier).
- Exact-HEAD PR verification gate and squash-merge tooling (`factonio_pr_gate`, `factonio_merge_pr`), plus per-repo configuration updates via CLI (`hermes factonio update`), agent tool (`factonio_update_repo`), and Dashboard API (`PATCH /api/plugins/factonio/repos/{slug}`).
- Five native Hermes Auxiliary Model slots (`factonio_define`, `factonio_design`, `factonio_implement`, `factonio_verify`, `factonio_watchdog`) with cross-model independence warnings.
- In-process Kanban dispatch-tick watchdog, SQLite phase metrics and escape-rate tracking, and Factonio Phase Retro automation blueprint.
- Zero-build Hermes Desktop UI (`desktop/plugin.js`): `/factonio` metrics dashboard with inline editable Registered Repositories table, hideable right-sidebar Factonio tab docked beside Files (with workstream pipeline bars, scope/status/phase filters, and expandable card drawers), Settings landing page with Auxiliary Models deep-links, status bar pill, and `::your-turn` transcript directive.
