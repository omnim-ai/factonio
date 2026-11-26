# Verification Harness: omnim-ai/factonio

Black-box playbook for Factonio `[verify:blackbox]` cards and humans. Run every stage in order
(Launch -> Doctor -> Drive -> Evidence -> Cleanup) against a fresh checkout of the change under test.
This file describes how to run and observe the software; it is not implementation source.

- Schema: `.factonio/harness.json` (schema_version 1)
- Detected stack: hermes-plugin, github-actions, python, node
- Generated at: 2026-11-26T15:55:00Z from `Wave 4 polish (trail --audit false positives, PR-scoped closure, malformed pr_number, lock budget) rebased onto main`

## Launch

Factonio is a zero-build Hermes plugin. Load the checkout under test into a throwaway `HERMES_HOME`
(never a real `~/.hermes` profile) and run every command below from the checkout root in one shell.

```bash
export FACTONIO_VERIFY_HOME="$(mktemp -d)"
mkdir -p "$FACTONIO_VERIFY_HOME/plugins" && ln -s "$PWD" "$FACTONIO_VERIFY_HOME/plugins/factonio"
printf 'plugins:\n  enabled: [factonio]\n' > "$FACTONIO_VERIFY_HOME/config.yaml"
HERMES_HOME="$FACTONIO_VERIFY_HOME" HERMES_KANBAN_DB="$FACTONIO_VERIFY_HOME/kanban.db" hermes factonio --help
```

## Doctor

Prove the environment is ready before driving it (each command must exit 0).

```bash
git --version
python3 --version
python3 -c "import hermes_cli"
node --version
uv --version
uvx --version
```

## Drive

Exercise the black-box surfaces: commands, HTTP probes, CLI or UI interactions.

Tests import the plugin as the `factonio` package, so the first command symlinks the checkout as
`$d/factonio` inside a `mktemp -d` directory and prepends it to `PYTHONPATH`; this works from any
checkout or worktree directory name. `HERMES_DELEGATED_CHILD_CONTEXT` is unset so tests do not
inherit a delegated agent's context. Hermes must be importable (`hermes_cli`), e.g. by adding a
`hermes-agent` checkout to `PYTHONPATH` as CI does.

```bash
sh -c 'd=$(mktemp -d); trap "rm -rf \"$d\"" EXIT; ln -s "$PWD" "$d/factonio"; env -u HERMES_DELEGATED_CHILD_CONTEXT PYTHONPATH="$d${PYTHONPATH:+:$PYTHONPATH}" python3 -m unittest discover -s tests -v'
python3 -W error -m compileall -q .
uvx ruff check --select E9,F .
node --check desktop/plugin.js
uv run --with pyyaml python tools/check_repo_integrity.py
HERMES_HOME="$FACTONIO_VERIFY_HOME" HERMES_KANBAN_DB="$FACTONIO_VERIFY_HOME/kanban.db" hermes factonio harness check "$PWD" --run-doctor --run-hooks --json
HERMES_HOME="$FACTONIO_VERIFY_HOME" HERMES_KANBAN_DB="$FACTONIO_VERIFY_HOME/kanban.db" hermes factonio harness audit "$PWD" --json
HERMES_HOME="$FACTONIO_VERIFY_HOME" HERMES_KANBAN_DB="$FACTONIO_VERIFY_HOME/kanban.db" hermes factonio --json models
HERMES_HOME="$FACTONIO_VERIFY_HOME" HERMES_KANBAN_DB="$FACTONIO_VERIFY_HOME/kanban.db" hermes factonio --json list
```

## Evidence

Capture what proves each behavior.

- Record every command with its exit code and the last lines of stdout/stderr.
- Keep the unittest summary line (`Ran N tests` / `OK`) and any FAIL/ERROR tracebacks.
- Keep the `harness check` JSON (`valid`, `errors`, `hook_results`) and the `harness audit` JSON (`severity`, `drifted`).
- For Desktop UI changes, capture screenshots via tools/capture_readme_media.py --qa-dir into a temp dir.

## Cleanup

Tear down everything Launch and Drive started.

- rm -rf "$FACTONIO_VERIFY_HOME" && unset FACTONIO_VERIFY_HOME
- Remove any temporary checkouts, QA media directories, and scratch kanban databases created during Drive.
- Never point a real ~/.hermes profile's plugins/factonio link at the checkout under test.

### Discovered surfaces

- HTTP routes: `GET /cards`, `GET /doctor`, `GET /metrics`, `GET /metrics/history`, `GET /models`, `GET /repos`, `GET /status`, `GET /trail`, `GET /trail/{decision_id}`, `PATCH /repos/{slug}`
- CLI commands: `add`, `doctor`, `harness`, `init`, `list`, `metrics`, `models`, `panel`, `pr-gate`, `remove`, `restack`, `start`, `status`, `trail`, `update`, `worktrees`

## Feature Map

| Feature | Surface | Paths | Verify with |
|---|---|---|---|
| `plugin` | Hermes plugin manifest & registration | `plugin.yaml`, `__init__.py` | `sh -c 'd=$(mktemp -d); trap "rm -rf \"$d\"" EXIT; ln -s "$PWD" "$d/factonio"; env -u HERMES_DELEGATED_CHILD_CONTEXT PYTHONPATH="$d${PYTHONPATH:+:$PYTHONPATH}" python3 -m unittest tests.test_plugin_registration -v'`; Plugin loads in an isolated HERMES_HOME and `hermes factonio --help` lists every subcommand; plugin.yaml declares every factonio_* tool and bundled skill; harness-audit blueprint pairs `harness audit` with a live `harness check --run-doctor --run-hooks` and the doc_drift / harness_gap / product_regression rubric; plugin.yaml config_schema declares `initial_progress_minutes` (default 10) and the retro blueprint maintains the AGENTS.md Rule-to-Enforcer ledger |
| `setup` | Settings, repo registry & worker profile | `config.py`, `profile_setup.py` | `sh -c 'd=$(mktemp -d); trap "rm -rf \"$d\"" EXIT; ln -s "$PWD" "$d/factonio"; env -u HERMES_DELEGATED_CHILD_CONTEXT PYTHONPATH="$d${PYTHONPATH:+:$PYTHONPATH}" python3 -m unittest tests.test_profile_and_models -v'`; `init` is idempotent and links plugins/factonio to the checkout; `init` refuses an ephemeral (.worktrees/, kanban/workspaces/, cache/scratch/) link target on a persistent profile unless --force-link; `doctor` warns when a persistent worker plugin link points into an ephemeral path; `resolve_initial_progress_minutes` caps the silent-start window at the phase stale threshold and `0` disables it |
| `cli` | CLI | `commands.py` | `sh -c 'd=$(mktemp -d); trap "rm -rf \"$d\"" EXIT; ln -s "$PWD" "$d/factonio"; env -u HERMES_DELEGATED_CHILD_CONTEXT PYTHONPATH="$d${PYTHONPATH:+:$PYTHONPATH}" python3 -m unittest tests.test_cli_and_tools tests.test_metrics_history tests.test_workstream_modes -v'`; `add` subcommand runs and exits 0 with --help; `doctor` subcommand runs and exits 0 with --help; `harness` subcommand runs and exits 0 with --help; `start` subcommand runs and exits 0 with --help; `start <repo> <request> --kind feature|bug|quick|refactor|perf|investigate --json` starts a workstream with additive kind / read_only / report_path; `--kind investigate --skip-define` exits 1 without creating a card; `trail --audit` exits 0 when clean and 1 with findings, with `--json` / `--markdown` and tenant/repo/phase/card filters |
| `agent-tools` | Agent tools | `agent_tools.py` | `sh -c 'd=$(mktemp -d); trap "rm -rf \"$d\"" EXIT; ln -s "$PWD" "$d/factonio"; env -u HERMES_DELEGATED_CHILD_CONTEXT PYTHONPATH="$d${PYTHONPATH:+:$PYTHONPATH}" python3 -m unittest tests.test_cli_and_tools tests.test_plugin_registration tests.test_workstream_modes -v'`; Every factonio_* tool in plugin.yaml provides_tools is registered with a JSON schema; factonio_bootstrap_verify accepts prove (init) and base_ref (check/audit); Refused merge/gate trail rows never carry override hints (allow_stacked_merge) in why; only real overrides read as gate_loosening in trail --audit (tests.test_pr_gate_hardening.TestMergeTrailAudit); factonio_start `kind` enum equals WORKSTREAM_KINDS (feature, bug, quick, refactor, perf, investigate) |
| `orchestrator` | Workflow card spawning & verification panels | `orchestrator.py` | `sh -c 'd=$(mktemp -d); trap "rm -rf \"$d\"" EXIT; ln -s "$PWD" "$d/factonio"; env -u HERMES_DELEGATED_CHILD_CONTEXT PYTHONPATH="$d${PYTHONPATH:+:$PYTHONPATH}" python3 -m unittest tests.test_orchestrator_and_repos tests.test_verify_panel tests.test_workstream_modes -v'`; Black-box cards receive holdout/ and the harness; implement/diff cards never do; rigor=high panels fail closed unless every expected lane passes; refactor cards pin behavior, perf cards embed the 7+7 benchmark checklist, and investigate rejects skip_define and spawns a read-only `[define] [investigate]` card that writes only investigation.md |
| `repos` | PR gate, merge, stacked PRs & worktrees | `repos.py` | `sh -c 'd=$(mktemp -d); trap "rm -rf \"$d\"" EXIT; ln -s "$PWD" "$d/factonio"; env -u HERMES_DELEGATED_CHILD_CONTEXT PYTHONPATH="$d${PYTHONPATH:+:$PYTHONPATH}" python3 -m unittest tests.test_pr_gate_and_git tests.test_pr_gate_hardening tests.test_stacked_prs tests.test_worktree_audit -v'`; `pr-gate` reports live head_sha and ready_to_merge; `pr-gate` reports owner-approval checks as Tier 4 approval_gates (never Tier 3 CI failures) and pending review automation; `pr-gate --stack 12,13,14` gates a stack root-to-tip in tier-major order with contiguous_ready_run, ceiling and lowest_pending_frontier objects (with pr_number), and next_action; Rebase-required trunk drift (overlap or .github/workflows/, including non-ASCII and non-UTF-8 byte paths compared as raw bytes, never failing open) is a Tier 1 blocker; clean, disjoint and lockfile_only are not; fetch=True fetches the exact head (refs/pull/<n>/head fallback) for drift and patch continuity; Merging a stacked PR is refused unless allow_stacked_merge; a rebased head merges only with allow_patch_continuity and an identical patch-id; `restack` keeps patch-id continuity; `worktrees` audit is read-only unless --prune-safe |
| `harness` | Repo-local verification harness | `harness.py` | `sh -c 'd=$(mktemp -d); trap "rm -rf \"$d\"" EXIT; ln -s "$PWD" "$d/factonio"; env -u HERMES_DELEGATED_CHILD_CONTEXT PYTHONPATH="$d${PYTHONPATH:+:$PYTHONPATH}" python3 -m unittest tests.test_verify_harness -v'`; `harness check "$PWD" --run-doctor --run-hooks --json` exits 0 with valid: true; `harness audit "$PWD" --json` reports severity clean without mutating .git/index; Route/CLI discovery ignores tests/, test/, spec/, __tests__/ fixtures; `harness init --prove` fails closed (ok/written false, prior .factonio/ bytes restored, proof_failed trail row, exit 1) unless Doctor, required hooks and evidence survival pass; `harness check --base <ref>` fails when a required hook is removed or downgraded to optional or a Doctor command is dropped; `audit` reports gate_loosening as stale; `audit` drift_classification ranks product_regression > harness_gap > doc_drift > clean; Malformed `harness.json` (invalid JSON or unsupported schema_version) makes `audit` stale and classifies as harness_gap; removing a required hook a hook-less `harness.json` inherits from verify.md is gate loosening |
| `observability` | Decision trail, metrics & watchdog | `trail.py`, `metrics.py`, `watchdog.py` | `sh -c 'd=$(mktemp -d); trap "rm -rf \"$d\"" EXIT; ln -s "$PWD" "$d/factonio"; env -u HERMES_DELEGATED_CHILD_CONTEXT PYTHONPATH="$d${PYTHONPATH:+:$PYTHONPATH}" python3 -m unittest tests.test_trail tests.test_watchdog_liveness tests.test_watchdog_metrics_api tests.test_metrics_history -v'`; `trail --json` lists append-only decisions; Watchdog ticks flag silent and blocked cards; `metrics --history --json` returns chronological append-only snapshots from trail.db v2 with summary.snapshot_count; `trail --audit --json` flags missing_evidence, weak_evidence, unclosed_escalation and gate_loosening read-only, never creating trail.db; Watchdog flags `silent_start` before the phase threshold when only heartbeats/startup bookkeeping exist, then escalates to stale_running / no_side_effects; `trail --audit --json` fails closed (ok/clean false, exit 1) when trail.db is a directory or dangling symlink; `trail --audit` flags allow_patch_continuity overrides, ignores gate-loosening prose on start/spawn_*/refused/rejecting-consensus rows without a truthy override key, and closes escalations by evidence pr_number across cards in a tenant; A spent best-effort trail budget still records an uncontended cold snapshot while a held trail.db.lock fails fast; `trail --audit` treats malformed evidence pr_number strings (Unicode digits such as '²', overlong digit strings) as absent and returns a structured audit instead of crashing |
| `dashboard-api` | HTTP API | `dashboard/**` | `sh -c 'd=$(mktemp -d); trap "rm -rf \"$d\"" EXIT; ln -s "$PWD" "$d/factonio"; env -u HERMES_DELEGATED_CHILD_CONTEXT PYTHONPATH="$d${PYTHONPATH:+:$PYTHONPATH}" python3 -m unittest tests.test_watchdog_metrics_api tests.test_orchestrator_and_repos tests.test_metrics_history -v'`; `GET /cards` under /api/plugins/factonio responds as documented; `GET /doctor` under /api/plugins/factonio responds as documented; `GET /metrics` under /api/plugins/factonio responds as documented |
| `desktop-ui` | Desktop UI | `desktop/**` | `node --check desktop/plugin.js`; `sh -c 'd=$(mktemp -d); trap "rm -rf \"$d\"" EXIT; ln -s "$PWD" "$d/factonio"; env -u HERMES_DELEGATED_CHILD_CONTEXT PYTHONPATH="$d${PYTHONPATH:+:$PYTHONPATH}" python3 -m unittest tests.test_desktop_plugin -v'`; Zero-build ESM importing only @hermes/plugin-sdk, react and react/jsx-runtime; Dashboard, sidebar and repo editor render with host theme tokens only; Repo editor sends default_rigor standard/high, or null to inherit, with partial-diff dirty tracking; Trends strip renders populated/empty/loading/error states from GET /metrics/history; Sidebar verification panel consensus is fail-closed and never shows pass without the recorded verify_panel_consensus; Sidebar panel shows inconclusive (never pass) when any expected lane from spawn_verify panel_lanes or the recorded consensus lanes is missing |
| `skills` | Bundled & contributor skills | `skills/**`, `.agents/skills/**` | `uv run --with pyyaml python tools/check_repo_integrity.py`; Every SKILL.md has valid frontmatter and keeps the holdout/ information barrier; verify-blackbox drives from the user's point of view and reports verified-unreachable scenarios as inconclusive, never pass; verify-diff keeps the Gate-Integrity Audit, MUST KILL rule, certainty ladder and `dropped` list; orchestrator section 4 tiers rigor=high fix-round re-verification and runs a pre-handoff `trail --audit` Attention check; define §1c, verify-diff checklist item 11 and orchestrator §1.4 document the refactor, perf and read-only investigate modes; orchestrator section 5 caps trunk-drift rebases (escalate before a 3rd rebase of the same PR) and runs merge-result caller checks on disjoint drift |
| `repo-tooling` | Repo tooling, docs & CI | `tools/**`, `docs/**`, `.github/**`, `*.md` | `uv run --with pyyaml python tools/check_repo_integrity.py`; `uvx ruff check --select E9,F .`; Manifest, skill frontmatter and local doc links stay valid; README media regenerates with tools/capture_readme_media.py; CHANGELOG Unreleased, docs/architecture.md and THIRD_PARTY_NOTICES.md document each Wave 4 and Wave 5 unit; CHANGELOG Unreleased and docs/architecture.md document the Wave 4 polish trail-audit, malformed-pr_number and lock-budget changes |
| `tests` | Tests | `tests/**` | `sh -c 'd=$(mktemp -d); trap "rm -rf \"$d\"" EXIT; ln -s "$PWD" "$d/factonio"; env -u HERMES_DELEGATED_CHILD_CONTEXT PYTHONPATH="$d${PYTHONPATH:+:$PYTHONPATH}" python3 -m unittest discover -s tests -v'`; Full suite passes from any checkout or worktree directory name; Sidebar missing-lane regressions (shrunk or extended live lane sets) fail closed in tests.test_desktop_plugin; silent_start, trail audit and skill prose contracts are covered in tests.test_watchdog_liveness, tests.test_trail and tests.test_plugin_registration; Trail-audit false positives, PR-scoped closure, malformed pr_number evidence and the cold-init lock-budget regression are covered in tests.test_trail, tests.test_pr_gate_hardening and tests.test_metrics_history |

## Hooks

Repo-defined checks (`visual_diff`, `invariant`, `custom`). Required hook failures reject; optional ones are disclosed limits.

- **compileall** (`invariant`, required): `python3 -W error -m compileall -q .`
- **ruff-static** (`invariant`, required): `uvx ruff check --select E9,F .`
- **desktop-syntax** (`invariant`, required): `node --check desktop/plugin.js`
- **repo-integrity** (`invariant`, required): `uv run --with pyyaml python tools/check_repo_integrity.py`
