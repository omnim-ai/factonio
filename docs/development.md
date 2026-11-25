# Developing Factonio

This guide covers repository layout, local development against Hermes Agent, running every test suite and static check, how CI works, and regenerating README media.

## Repository layout

```text
__init__.py                  Plugin entrypoint: registers aux models, skills, tools, CLI, watchdog, blueprint
plugin.yaml                  Hermes plugin manifest (v2) and config_schema
agent_tools.py               11 agent-callable tools (`factonio_*`), including `factonio_log_decision` and `factonio_bootstrap_verify`
commands.py                  CLI subcommands (`hermes factonio init|add|update|remove|list|models|status|pr-gate|worktrees|harness|trail|doctor`)
config.py                    Settings, durable repos.json store (atomic writes + flock), phase model & design resolution
orchestrator.py              Workstream lifecycle, Kanban DAG creation, holdout/ isolation, session notify+wake subscriptions
profile_setup.py             Dedicated headless `factonio` profile initializer and `doctor` diagnostics
harness.py                   Repo-local `.factonio/` verification harness: stack/route/CLI detection, init/check/show, hooks, read-only drift audit
repos.py                     GitHub repo registration, validation, exact-HEAD PR gate, and squash-merge helpers
trail.py                     Append-only SQLite decision trail (`trail.db`: WAL, schema_meta, immutability triggers)
metrics.py                   Read-only SQLite queries over `kanban.db` (phase timings, pass rates, rework, escapes, enriched cards)
watchdog.py                  In-process `on_kanban_dispatch_tick` PID/side-effect liveness, phase-aware stall detection, bounded triage
dashboard/
  manifest.json              Hermes Dashboard API discovery manifest
  plugin_api.py              FastAPI router mounted at `/api/plugins/factonio/*`
desktop/
  plugin.js                  Zero-build ESM Desktop plugin (`/factonio`, right-sidebar tab, Settings page, status pill, `::your-turn`)
skills/
  orchestrator/SKILL.md      Interactive coordinator workflow
  define/SKILL.md            Phase 1: repo exploration, spec.md + task DAG, private holdout/ checks
  design/SKILL.md            Phase 1b: Claude Design `/design` PTY or pure-Hermes HTML/CSS previews
  implement/SKILL.md         Phase 2: scoped worktree coding, tests, PR creation, fix rounds
  verify-diff/SKILL.md       Phase 3a: diff & spec review (blind to holdout/)
  verify-blackbox/SKILL.md   Phase 3b: live artifact & holdout/ execution (blind to git diff)
tests/
  _env.py                    Clean-room `isolated_hermes_home()` context manager
  test_plugin_registration.py
  test_profile_and_models.py
  test_orchestrator_and_repos.py
  test_cli_and_tools.py
  test_watchdog_metrics_api.py
  test_watchdog_liveness.py   PID/side-effect liveness, phase thresholds, bounded triage
  test_worktree_audit.py     Real-git worktree safety buckets, --prune-safe, `worktrees` CLI
  test_verify_harness.py     Real-git `.factonio/` harness detection, init/check/hooks, read-only drift audit, CLI/tool/blueprint, Black-Box injection
  test_trail.py              trail.db store, transition hooks, CLI/API/tool, retro, cross-vendor warning
  test_desktop_plugin.py     Node ESM smoke tests + Desktop SDK surface lint (59 tests)
tools/
  check_repo_integrity.py    Manifest, skill frontmatter (runtime + contributor), and Markdown link verifier
  capture_readme_media.py    Offline Chromium harness & ffmpeg GIF encoder for docs/images/
.agents/
  skills/
    update-readme-media/     Project-local contributor skill for auto-regenerating & QA'ing README visuals
docs/
  architecture.md            Deep dive on phases, information barrier, storage, and APIs
  development.md             This file
  images/                    Brand logos/banners, 2:1 marketplace/social cover, screenshots, architecture SVG, and GIFs
```

## Local setup

### Prerequisites

- **Hermes Agent** (with Python 3.14 runtime via `uv`, matching `.github/workflows/ci.yml`)
- **Node.js 22.15+** (required by `tests/test_desktop_plugin.py`, which uses `node:module`'s `registerHooks`)
- **`git`** and **`gh`** (authenticated via `gh auth login` for live GitHub workflows; unit tests do not make network calls)
- Optional: **`uv`** / **`uvx`** (for `ruff` and syncing a local `hermes-agent` checkout) and **`ffmpeg`** + Chromium (only if regenerating README screenshots/GIFs)

### Link your local checkout into Hermes

```bash
git clone git@github.com:omnim-ai/factonio.git
cd factonio

ln -sfn "$PWD" "${HERMES_HOME:-$HOME/.hermes}/plugins/factonio"
hermes plugins enable factonio
hermes factonio init --apply-config
hermes factonio doctor
```

Restart Hermes Desktop or reload plugins after editing `desktop/plugin.js` or `dashboard/plugin_api.py`.

## Test suites and checks

| Check | Command | Needs |
|---|---|---|
| Full test suite (90 tests) | `PYTHONPATH=/path/to/hermes-agent /path/to/hermes-agent/.venv/bin/python -m unittest discover -s tests -v` | Hermes Agent checkout + Node 22.15+ |
| Single test module | `PYTHONPATH=/path/to/hermes-agent /path/to/hermes-agent/.venv/bin/python -m unittest tests/test_desktop_plugin.py -v` | Hermes Agent checkout + Node 22.15+ |
| Python syntax check | `python3 -W error -m compileall -q .` | Python 3.11+ |
| Static lint (pyflakes / syntax) | `uvx ruff check --select E9,F .` | `uv` or `ruff` |
| Desktop JS syntax check | `node --check desktop/plugin.js` | Node 22+ |
| Manifest, skills & doc links | `python3 tools/check_repo_integrity.py` | Python + `pyyaml` |

### Running tests against the pinned Hermes Agent commit

If you do not already have a local `hermes-agent` checkout, fetch the exact commit pinned in `.github/workflows/ci.yml`:

```bash
HERMES_REF=$(grep 'HERMES_REF:' .github/workflows/ci.yml | awk '{print $2}')
git init ../hermes-agent
git -C ../hermes-agent fetch --depth 1 https://github.com/NousResearch/hermes-agent "$HERMES_REF"
git -C ../hermes-agent checkout FETCH_HEAD
(cd ../hermes-agent && uv sync --locked --python 3.14 --group dev)

PYTHONPATH="$PWD/../hermes-agent" ../hermes-agent/.venv/bin/python -m unittest discover -s tests -v
```

### Clean-room test isolation

Every Python test runs inside `tests._env.isolated_hermes_home()`, which creates a temporary `HOME` and `HERMES_HOME` (`kanban.db`, `workspaces/`, `config.yaml`, `plugin-data/`) and clears Hermes's cached SQLite initialization paths. Running the test suite never reads or modifies your real `~/.hermes`.

### How Desktop UI tests work without a build step

`tests/test_desktop_plugin.py` executes the real `desktop/plugin.js` directly in Node.js using `node:module` `registerHooks` to stub `@hermes/plugin-sdk`, `react`, and `react/jsx-runtime` in memory. It verifies:

- Settings landing page registration and older-host compatibility
- `/factonio` dashboard metrics, Auxiliary Models deep-links, and the editable Registered Repositories table (dirty tracking, validation, background-refresh isolation, error preservation, `PATCH` payload filtering)
- Docked right-sidebar Factonio tab (visibility-aware polling, workstream grouping, phase pipeline resolution across multi-round verification, status/phase filters, card detail drawer, external URL sanitization)
- Hermes's official Desktop SDK surface lint (`hermes_cli.plugin_validate_desktop`) and theme-token-only CSS checks

No `npm install`, bundler, or build artifact is required.

## CI workflows

Factonio runs two GitHub Actions workflows:

1. **`.github/workflows/ci.yml`** (runs on pushes to `main`, non-draft pull requests, and `workflow_dispatch`):
   - **Changelog entry:** Ensures non-draft PRs update `CHANGELOG.md` (under `## Unreleased`) or carry the `no-changelog` label.
   - **Lint & manifest:** Runs `compileall`, `ruff check --select E9,F`, `node --check desktop/plugin.js`, and `tools/check_repo_integrity.py`.
   - **README media freshness:** Ensures PRs modifying `desktop/plugin.js` or `tools/capture_readme_media.py` also refresh `docs/images/`, unless the PR carries the `no-visual-change` label.
   - **Test suite on Hermes (pinned):** Checks out `NousResearch/hermes-agent` at `HERMES_REF`, syncs with `uv`, and runs all unit and Desktop UI tests.
   - **CI passed:** Single aggregator check required by branch protection on `main`.
2. **`.github/workflows/hermes-upstream.yml`** (runs daily and on `workflow_dispatch`):
   - Runs the full test suite against `NousResearch/hermes-agent` `main` (or a custom `hermes_ref`) to catch upstream API changes early.

## Regenerating README screenshots and animations

The screenshots and animated GIFs in `docs/images/` are rendered directly from the unmodified `desktop/plugin.js` inside headless Chromium via `tools/capture_readme_media.py`.

Contributors using AI coding agents (Hermes Agent, Codex, Claude Code, etc.) can let their agent handle this automatically via the project-local skill at [`.agents/skills/update-readme-media/SKILL.md`](../.agents/skills/update-readme-media/SKILL.md) (in Hermes, run `hermes skills trust` once inside this repository to auto-load `.agents/skills/`).

```bash
# Check local Chrome/Chromium and ffmpeg availability (override with CHROME_BIN=/path/to/chrome)
python3 tools/capture_readme_media.py --check-deps

# Preview every harness scene interactively in your browser
python3 tools/capture_readme_media.py --serve-only --port 18432

# Regenerate only affected surfaces and export GIF keyframe PNGs for visual QA
python3 tools/capture_readme_media.py --capture --only sidebar,workflow --qa-dir /tmp/factonio-media-qa

# Or regenerate every PNG screenshot and animated GIF
python3 tools/capture_readme_media.py --capture --only all --qa-dir /tmp/factonio-media-qa
```

Valid `--only` targets: `all`, `dashboard`, `sidebar`, `settings`, `workflow`, `repo-editor`, `architecture`. Always visually inspect the regenerated PNGs and `--qa-dir` keyframes before committing (and delete the temporary `--qa-dir` folder afterward). For non-visual refactors to `desktop/plugin.js`, skip media churn and add the `no-visual-change` label to the PR.
