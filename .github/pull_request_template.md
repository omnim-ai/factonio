## What and why

<!-- Describe the problem this solves and what changed. Link related issues (e.g. Fixes #12). -->

## How you know it works

<!-- Exact commands run and their outcomes (tests, CLI commands, Desktop UI checks), plus anything you could not verify. For bug fixes, describe the failing behavior before and the verified behavior after. -->

## Documentation

<!-- Link updated guides/README/CHANGELOG sections, or explain why no documentation changes are needed. -->

## Checklist

- [ ] Focused on one change with a descriptive title (e.g. `feat(desktop): ...`, `fix(repos): ...`)
- [ ] `python3 -m unittest discover -s tests -v` passes (runs in an isolated temporary `HERMES_HOME`)
- [ ] `python3 tools/check_repo_integrity.py` and `node --check desktop/plugin.js` pass
- [ ] Preserves the `holdout/` information barrier (Implement & Diff Check never see `holdout/`; Black-Box Check never sees `git diff`)
- [ ] `desktop/plugin.js` uses only public `@hermes/plugin-sdk` surfaces and host theme tokens (if touched)
- [ ] Added a bullet under `## Unreleased` in [CHANGELOG.md](../CHANGELOG.md), or labeled the PR `no-changelog`
- [ ] Relevant docs updated, or explained why none apply
