<h1 align="center">Factonio</h1>

<p align="center">
  <b>Pure-Hermes, phase-based software factory plugin for any GitHub repository.</b><br>
  Definition, optional UI Design, parallel git-worktree Implementation, and mutually isolated Diff + Black-Box Verification — using only <a href="https://github.com/NousResearch/hermes-agent">Hermes Agent</a>, <code>git</code>, and <code>gh</code>.
</p>

<p align="center">
  <a href="https://github.com/omnim-ai/factonio/actions/workflows/ci.yml"><img src="https://github.com/omnim-ai/factonio/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <a href="https://github.com/omnim-ai/factonio/actions/workflows/hermes-upstream.yml"><img src="https://github.com/omnim-ai/factonio/actions/workflows/hermes-upstream.yml/badge.svg" alt="Hermes Upstream"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-blue.svg" alt="MIT License"></a>
</p>

<p align="center">
  <a href="#quick-start"><b>Quick Start</b></a> ·
  <a href="#how-it-works"><b>How It Works</b></a> ·
  <a href="#desktop-ui"><b>Desktop UI</b></a> ·
  <a href="#developing-and-contributing"><b>Develop &amp; Contribute</b></a> ·
  <a href="docs/architecture.md">Architecture</a>
</p>

<p align="center">
  <img src="docs/images/demo-workflow.gif" width="900" alt="Animated walkthrough of Factonio taking an issue from Definition and Design through parallel Implementation, isolated Black-Box catch, Fix Round 1, Verification pass, and PR merge in Hermes Desktop">
  <br><sub>End-to-end Factonio workstream in Hermes Desktop: Define → Design → Implement → isolated Diff + Black-Box Verification → bounded fix round → exact-HEAD merge.</sub>
</p>

## Choose your starting point

| Run Factonio on your repos | Tune cross-model verification | Develop or contribute |
|---|---|---|
| [Quick Start](#quick-start) | [Configure phase models](#3-configure-phase-models) | [Development guide](docs/development.md) |
| Install the plugin, initialize the headless `factonio` worker profile, and register a GitHub repo. | Assign distinct models to `factonio_implement` and `factonio_verify` in Hermes Auxiliary Models. | Run all 90 clean-room tests locally, inspect the Desktop UI harness, and read [CONTRIBUTING.md](CONTRIBUTING.md). |

## How it works

Factonio runs autonomous software delivery on a dedicated headless `factonio` worker profile across four explicit phases, with a strict **Information Barrier** during verification to prevent confirmation bias and test overfitting:

![Factonio Software Factory Pipeline](docs/images/architecture.svg)

1. **Phase 1 — Definition (`factonio:define`)** explores the repository, writes `spec.md` (acceptance criteria, technical plan, and a parallel task DAG), and authors hidden black-box acceptance checks under `holdout/`.
2. **Phase 1b — Optional UI/UX Design (`factonio:design`)** runs when a task touches UI (`has_ui: true` or on request). It supervises interactive Claude Design (`/design`) when the `claude` CLI is authenticated, or falls back automatically to pure-Hermes HTML/CSS previews.
3. **Phase 2 — Parallel Implementation (`factonio:implement`)** dispatches independent spec units in parallel git worktrees (reading `spec.md` and approved `design/` only — never `holdout/`), runs repository tests, and opens GitHub pull requests.
4. **Phase 3 — Isolated Parallel Verification (`factonio:verify-diff` + `factonio:verify-blackbox`)** launches two concurrent, mutually isolated checks on every PR:
   - **Diff Check** inspects `spec.md` and `git diff` without ever seeing `holdout/`.
   - **Black-Box Check** runs the built artifact against acceptance criteria and `holdout/` without reading source code or diffs.
   - On rejection, Factonio runs bounded repair rounds (up to `max_fix_rounds`, reporting only observable symptoms). Once GitHub CI and both checks pass on the **exact live `head_sha`**, the PR squash-merges automatically (or waits for approval when `merge_policy: ask`).

For a full technical breakdown of worktrees, storage locking, watchdog ticks, and API endpoints, see [docs/architecture.md](docs/architecture.md).

## Desktop UI

Factonio ships a zero-build Hermes Desktop UI (`desktop/plugin.js`) using only public `@hermes/plugin-sdk` surfaces and live host theme tokens.

### Metrics Dashboard (`/factonio`)

Track workstream throughput, Diff vs Black-Box pass rates, rework rate, post-merge escape rate, per-phase median/P90 durations, phase model assignments, and registered repositories in one place:

![Factonio Dashboard showing KPI cards, Phase Durations, Phase Model Configuration, and Registered Repositories](docs/images/dashboard.png)

### Inline Repository Settings Editor

Edit any registered repository's `default_branch`, `merge_policy`, `max_fix_rounds`, `design_provider`, or `design_approval` directly in `/factonio` — clearly distinguishing inherited global defaults (`value · global`) from per-repo overrides:

![Animated walkthrough of editing a registered repository inline on the Factonio dashboard](docs/images/demo-repo-editor.gif)

### Docked Right-Sidebar Tab & Settings

Monitor active workstreams beside your chat transcript in the hideable **Factonio** tab docked next to **Files** — with live 4-stage pipeline bars, scope/status/phase filters, attention callouts, PR shortcuts, and expandable card detail drawers:

| Docked Factonio Sidebar Tab | Settings → Plugins → Factonio |
|:---:|:---:|
| ![Factonio Right Sidebar tab beside Files with workstream pipeline and open card detail drawer](docs/images/sidebar.png) | ![Settings Plugins Factonio landing page showing Phase Models and Agent settings](docs/images/settings.png) |

## Quick start

### Prerequisites

- [Hermes Agent](https://github.com/NousResearch/hermes-agent) installed and configured
- `git` and GitHub CLI (`gh`), authenticated via `gh auth login`
- Optional: `claude` CLI (authenticated) if you want Phase 1b to use interactive Claude Design (`/design`); otherwise Factonio falls back cleanly to pure-Hermes prototyping

### 1. Install and enable the plugin

```bash
hermes plugins install git@github.com:omnim-ai/factonio.git
hermes plugins enable factonio
```

> If using Hermes Desktop, toggle **Factonio** on once under **Capabilities → Plugins** to enable the Desktop dashboard, docked sidebar tab, Settings page, and status bar pill.

### 2. Initialize the headless worker profile

```bash
hermes factonio init --apply-config
hermes factonio doctor
```

`hermes factonio init` creates the dedicated headless `factonio` profile (`approvals.mode: off`), links the Factonio plugin into it so workers resolve every `factonio:*` skill, and applies the recommended root Kanban settings (`kanban.dispatch_in_gateway: true`, `kanban.auto_decompose: false`).

### 3. Configure phase models

Factonio registers five native Hermes Auxiliary Model slots:

| Slot | Phase | Recommendation |
|---|---|---|
| `factonio_define` | Definition | Strong reasoning/planning model |
| `factonio_design` | Phase 1b Design (optional) | UI/frontend-capable model |
| `factonio_implement` | Implementation | Fast, accurate coding model |
| `factonio_verify` | Diff + Black-Box Verification | **Different provider/model from Implementation** for cross-model independence |
| `factonio_watchdog` | Ambiguous stall triage (optional) | Low-cost fast model |

Set them in `hermes model` → **Auxiliary Models** or Hermes Desktop **Settings → Model → Auxiliary**, then verify the resolved matrix:

```bash
hermes factonio models
```

### 4. Register a repository and start work

```bash
hermes factonio add owner/repo
```

Then ask your Hermes chat session to build a feature or fix an issue on `owner/repo` — Factonio's orchestrator coordinates Definition, optional Design, parallel Implementation, isolated Verification, and the PR merge gate automatically.

### Edit repository settings anytime

Adjust any registered repository's settings without re-adding it (only passed flags change):

```bash
hermes factonio update owner/repo --merge-policy ask --max-fix-rounds 5
hermes factonio update owner-repo --default-branch develop
hermes factonio update owner/repo --design-provider hermes --design-approval auto
hermes factonio update owner/repo --design-provider inherit   # revert to global default
hermes factonio edit owner/repo --merge-policy auto --json    # `edit` is an alias
```

You can also edit settings from chat via `factonio_update_repo`, or inline on `/factonio` (`PATCH /api/plugins/factonio/repos/<slug>`).

## CLI and agent tools reference

<details>
<summary>CLI subcommands (<code>hermes factonio ...</code>)</summary>

| Command | Purpose |
|---|---|
| `hermes factonio init [--apply-config] [--workers N] [--force]` | Create or update the headless `factonio` worker profile and plugin link |
| `hermes factonio doctor [--json]` | Check `git`, `gh auth`, worker profile, Kanban config, and cross-model setup |
| `hermes factonio add <owner/repo> [--path DIR] [--merge-policy auto\|ask] [--max-fix-rounds N]` | Register, clone (unless `--no-clone`), anchor Hermes Project, and inspect CI |
| `hermes factonio update\|edit <repo> [--merge-policy ...] [--max-fix-rounds N] [--default-branch B] [--design-provider ...] [--design-approval ...]` | Update per-repo settings (`inherit` clears design overrides) |
| `hermes factonio list [--json]` | List registered repositories and active settings |
| `hermes factonio remove <repo>` | Unregister a repository (keeps local files intact) |
| `hermes factonio models [--repo SLUG] [--json]` | Show resolved provider/model per phase and cross-model warning |
| `hermes factonio status [--tenant T] [--json]` | Show active Factonio Kanban workstreams and cards |
| `hermes factonio pr-gate <repo> <pr_number> [--json]` | Inspect live PR HEAD SHA, mergeability, and CI check status |

</details>

<details>
<summary>Agent tools registered in toolset <code>factonio</code></summary>

- `factonio_add_repo` — register a GitHub repository and anchor its Hermes Project
- `factonio_update_repo` — update per-repository settings (`merge_policy`, `max_fix_rounds`, `default_branch`, `design_provider`, `design_approval`)
- `factonio_start` — start a workstream (`feature`, `bug`, `quick`, or `escaped_from` for post-merge regressions)
- `factonio_spawn_design` — spawn a Phase 1b UI/UX Design card
- `factonio_spawn_implement` — spawn a scoped git-worktree Implementation card
- `factonio_spawn_verify` — spawn mutually isolated Diff Check and Black-Box Check cards in parallel
- `factonio_status` — inspect active workstream cards, registered repos, and resolved phase models
- `factonio_pr_gate` — verify live PR HEAD SHA and CI status checks via `gh`
- `factonio_merge_pr` — verify exact HEAD SHA and squash-merge a verified PR

</details>

## Developing and contributing

Contributions are welcome — bug reports, skill improvements, orchestrator/watchdog fixes, Desktop UI enhancements, tests, and documentation.

### Local development checkout

```bash
git clone git@github.com:omnim-ai/factonio.git
cd factonio
ln -sfn "$PWD" "${HERMES_HOME:-$HOME/.hermes}/plugins/factonio"
hermes plugins enable factonio
```

### Run tests and checks

Every test runs in an isolated temporary `HERMES_HOME` (`tests._env.isolated_hermes_home()`) and never touches your real `~/.hermes`:

```bash
# Full 90-test suite (requires a Hermes Agent checkout + Node 22.15+)
PYTHONPATH=/path/to/hermes-agent /path/to/hermes-agent/.venv/bin/python -m unittest discover -s tests -v

# Standalone static & integrity checks
python3 -W error -m compileall -q .
uvx ruff check --select E9,F .
node --check desktop/plugin.js
python3 tools/check_repo_integrity.py
```

Read **[CONTRIBUTING.md](CONTRIBUTING.md)** for PR expectations, core invariants, and the change-to-checks guide, and **[docs/development.md](docs/development.md)** for setting up a pinned Hermes checkout, understanding the zero-build Node ESM Desktop test harness, and regenerating README screenshots and animations.

Report security vulnerabilities privately through **[SECURITY.md](SECURITY.md)**. See **[CHANGELOG.md](CHANGELOG.md)** for release history.

## Affiliation and trademarks

Factonio is an **independent open-source project by [Omnim AI](https://github.com/omnim-ai)**. It is built for compatibility with [Hermes Agent](https://github.com/NousResearch/hermes-agent) and is not officially affiliated with, endorsed by, or sponsored by Nous Research.

## License

Licensed under the [MIT License](LICENSE).
