# Contributing to Factonio

Thanks for helping make Factonio better. Bug reports, skill improvements, orchestrator and watchdog fixes, Desktop UI polish, tests, and documentation are all welcome.

## Reporting a bug

Open an [issue](https://github.com/omnim-ai/factonio/issues/new/choose) and include:

- Your Hermes Agent version (`hermes --version`)
- The output of `hermes factonio doctor --json` (redact any local paths you'd rather not share)
- Your phase model matrix (`hermes factonio --json models`) if the issue involves model selection or verification
- What you did, what happened, and what you expected instead

Report security vulnerabilities privately through [SECURITY.md](SECURITY.md), not in a public issue.

Problems inside Hermes Agent itself (the core runtime, Kanban dispatcher, Desktop host, or `@hermes/plugin-sdk`) belong in [NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent/issues).

## Setting up a development environment

Factonio is a pure-Python + zero-build-ESM Hermes plugin. To develop against your local Hermes installation:

```bash
git clone git@github.com:omnim-ai/factonio.git
cd factonio
```

Link your checkout into your Hermes plugins directory (and into the dedicated `factonio` worker profile via `init`):

```bash
ln -sfn "$PWD" "${HERMES_HOME:-$HOME/.hermes}/plugins/factonio"
hermes plugins enable factonio
hermes factonio init --apply-config
hermes factonio doctor
hermes skills trust   # optional: auto-loads repo contributor skills in .agents/skills/
```

Run the test suite and static checks:

```bash
# Full clean-room test suite (always runs in a temporary HERMES_HOME; never touches ~/.hermes)
PYTHONPATH=/path/to/hermes-agent python3 -m unittest discover -s tests -v

# Static checks (no Hermes checkout needed)
python3 -W error -m compileall -q .
uvx ruff check --select E9,F .
node --check desktop/plugin.js
python3 tools/check_repo_integrity.py
```

See [docs/development.md](docs/development.md) for full environment details, running against a pinned `hermes-agent` checkout, and regenerating README screenshots and animations.

## Core invariants

Every contribution must preserve these invariants:

1. **The `holdout/` information barrier.** Implementation workers (`factonio:implement`), Design workers (`factonio:design`), and Diff Verification (`factonio:verify-diff`) must never receive or read `holdout/` paths or contents. Black-Box Verification (`factonio:verify-blackbox`) must never inspect source files or `git diff`.
2. **Exact-HEAD verification and merge gate.** Pull requests are only merged when live GitHub CI is green, both isolated verification checks passed, and the live PR `head_sha` matches the verified commit SHA.
3. **Zero-build Desktop UI on public SDK surfaces.** `desktop/plugin.js` must remain a standalone ES module importing only `@hermes/plugin-sdk`, `react`, and `react/jsx-runtime`, using host CSS theme tokens only (no private host imports, no build step, no hardcoded light/dark palette surfaces).
4. **Isolated tests.** Every test must use `tests._env.isolated_hermes_home()` and never read or mutate the operator's real `~/.hermes` or `kanban.db`.

## Pull requests

- **Keep each PR to one focused change.** Use a clear, descriptive title such as `feat(desktop): add phase filter persistence` or `fix(repos): reject branch names containing '..'`.
- **Say why, then what, then how you know it works.** Include the exact commands you ran and their outcomes, plus anything you could not check. For bugs, describe the failing behavior before the fix and the verified behavior afterward.
- **CI must pass before merging.** `main` is protected and requires the `CI passed` check (lint, manifest/link integrity, README media freshness, full test suite against the pinned Hermes Agent commit, and changelog check). Draft PRs skip CI until marked ready for review.
- **Keep README screenshots and GIFs fresh on UI PRs.** Any PR that changes `desktop/plugin.js` or `tools/capture_readme_media.py` must regenerate and visually verify the affected assets in `docs/images/` using the project skill [`.agents/skills/update-readme-media/SKILL.md`](.agents/skills/update-readme-media/SKILL.md) (`python3 tools/capture_readme_media.py --capture`), **or** carry the `no-visual-change` label when the change has no visible effect. CI enforces this automatically in the **README media freshness** job, and code reviewers verify the rendered diffs. If you use Hermes, Codex, or Claude Code, trust/load `.agents/skills/` so your agent runs this workflow for you automatically.
- **Update docs and `CHANGELOG.md`.** Add a short bullet under `## Unreleased` in [CHANGELOG.md](CHANGELOG.md). CI checks for the changelog entry; add the `no-changelog` label on PRs with no user-facing change (internal refactors, CI tweaks, or test-only changes).
- **Test observable behavior.** Prefer tests that exercise real state transitions, API responses, CLI exit codes, and DOM interactions rather than asserting that an internal helper was merely called.
- **Preserve authorship.** Keep contributor authorship and attribution intact when rebasing, fixing, or building on someone else's PR.

### Choose the relevant documentation and checks

| Change area | Documentation to review | Required verification |
|---|---|---|
| Orchestrator, repos, config, watchdog, metrics | [docs/architecture.md](docs/architecture.md), [README.md](README.md) | Full `unittest` suite (`test_orchestrator_and_repos.py`, `test_profile_and_models.py`, `test_watchdog_metrics_api.py`), including concurrency and `holdout/` barrier tests |
| Bundled phase skills (`skills/*/SKILL.md`) | [docs/architecture.md](docs/architecture.md) | `tools/check_repo_integrity.py` and `tests/test_plugin_registration.py`; verify skill instructions do not leak `holdout/` or bypass merge gates |
| CLI commands, agent tools, `plugin.yaml` | [README.md](README.md), [docs/architecture.md](docs/architecture.md) | `tests/test_cli_and_tools.py`, `tests/test_plugin_registration.py`, `tools/check_repo_integrity.py` |
| Dashboard API & Desktop UI (`dashboard/`, `desktop/plugin.js`) | [README.md](README.md), [docs/development.md](docs/development.md); follow [`.agents/skills/update-readme-media/SKILL.md`](.agents/skills/update-readme-media/SKILL.md) to refresh `docs/images/` | `node --check desktop/plugin.js`, `tests/test_desktop_plugin.py` (including Desktop SDK surface lint), `tests/test_watchdog_metrics_api.py`, and `python3 tools/capture_readme_media.py --capture` (or `no-visual-change` label if non-visual) |
| Docs, templates, or CI workflows | [docs/development.md](docs/development.md), [CONTRIBUTING.md](CONTRIBUTING.md) | `python3 tools/check_repo_integrity.py` |

Keep `README.md` concise and scannable; put deep architectural or development reference material under `docs/`.

## Licensing

By contributing to Factonio, you agree that your contributions will be licensed under the [MIT License](LICENSE). Do not copy third-party code without a compatible license and explicit attribution.
