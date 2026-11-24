# Factonio

Pure-Hermes, phase-based software factory plugin for any GitHub repository.

Factonio runs software delivery across three explicit phases on a dedicated headless `factonio` worker profile using only Hermes, `git`, and `gh`:

1. **Definition (`factonio:define`)** — explores the repository, writes `spec.md` (acceptance criteria, technical approach, parallel task DAG), and authors hidden black-box checks under `holdout/`.
2. **Implementation (`factonio:implement`)** — executes scoped units in parallel git worktrees, runs repository tests, opens pull requests, and runs bounded repair loops.
3. **Isolated parallel Verification (`factonio:verify-diff` + `factonio:verify-blackbox`)** — launches two concurrent, mutually isolated checks on every PR:
   - **Diff Check** inspects `spec.md` and `git diff` without seeing `holdout/`.
   - **Black-Box Check** runs the built artifact against acceptance criteria and `holdout/` without reading source code or diffs.
   - Pull requests auto-merge once GitHub CI and both verification checks pass (configurable per repo to `ask`).

## Install

```bash
hermes plugins install git@github.com:tozes/factonio.git
hermes plugins enable factonio
```

If using Hermes Desktop, toggle **Factonio** on once under **Capabilities -> Plugins** to activate the Desktop UI half (Factonio dashboard, Thread Cards pane, and status pill).

## Setup

```bash
hermes factonio init --apply-config
hermes factonio doctor
```

`hermes factonio init` creates the dedicated headless `factonio` profile (`approvals.mode: off`), links the plugin into it so workers resolve every `factonio:*` skill, and checks root Kanban settings (`kanban.dispatch_in_gateway: true`, `kanban.auto_decompose: false`).

## Configure phase models

Factonio registers four native Hermes auxiliary task slots:

- `factonio_define` (Definition)
- `factonio_implement` (Implementation)
- `factonio_verify` (Verification — pick a different provider/model from Implementation for cross-model independence)
- `factonio_watchdog` (optional low-cost model for ambiguous stall triage)

Configure them in `hermes model` -> **Auxiliary Models** or Desktop **Settings -> Models**, then inspect the active matrix with:

```bash
hermes factonio models
```

## Register a repository and start work

```bash
hermes factonio add owner/repo
```

Then ask your Hermes chat session to implement a feature or fix an issue on `owner/repo`.

## Running tests

Tests run strictly in an isolated temporary `HERMES_HOME` and never touch your real `~/.hermes`:

```bash
python3 -m unittest discover -s tests -v
```
