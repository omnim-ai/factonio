# Repository Work Guide (AGENTS.md)

Read [CONTRIBUTING.md](CONTRIBUTING.md) before changing this repository. It defines the core invariants, documentation expectations, verification commands, and changelog requirements for every PR.

## Where to look first

- **Architecture & invariants:** [docs/architecture.md](docs/architecture.md) covers the 4-phase workflow, the `holdout/` information barrier, phase model precedence, durable `repos.json` locking, watchdog ticks, and Dashboard API routes.
- **Development & test suites:** [docs/development.md](docs/development.md) documents repo layout, running tests with a `hermes-agent` checkout, Node 22+ Desktop smoke tests, and CI workflows.
- **Desktop UI (`desktop/plugin.js`):** Must remain zero-build ESM using only `@hermes/plugin-sdk`, `react`, and `react/jsx-runtime`, styled exclusively with host CSS theme tokens. Verify with `tests/test_desktop_plugin.py` (which also runs Hermes's Desktop SDK surface lint).
- **Bundled skills (`skills/*/SKILL.md`):** Never leak `holdout/` into `define` handoffs for implementers, `design`, `implement`, or `verify-diff`; never let `verify-blackbox` inspect source files or `git diff`.

## Completing a change

1. Keep changes scoped to the requested task and preserve unrelated local work.
2. Never touch the user's real `~/.hermes` in tests — always wrap tests in `tests._env.isolated_hermes_home()`.
3. Update affected docs and add a bullet under `## Unreleased` in [CHANGELOG.md](CHANGELOG.md) (unless the PR is internal/test-only and labeled `no-changelog`).
4. Run all verification commands before reporting done:
   - `python3 -m unittest discover -s tests -v`
   - `python3 -W error -m compileall -q .`
   - `uvx ruff check --select E9,F .`
   - `node --check desktop/plugin.js`
   - `python3 tools/check_repo_integrity.py`
5. Report exact commands run, outcomes, and any remaining limitations. Do not weaken assertions to make a failing test pass.
