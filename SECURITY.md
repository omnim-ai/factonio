# Security Policy

## Reporting a vulnerability

Please report security vulnerabilities privately through GitHub's **[Report a vulnerability](https://github.com/omnim-ai/factonio/security/advisories/new)** form on this repository rather than opening a public issue or pull request.

Include:

- A description of the vulnerability and the affected component
- Steps to reproduce or a minimal proof of concept
- Impact assessment (e.g., unintended repo write, credential exposure, information barrier bypass, or XSS in Desktop UI)

You will receive a response on the advisory so the fix can be coordinated before public disclosure. Reporters are credited in the advisory unless anonymity is requested.

Security issues in Hermes Agent itself belong in [NousResearch/hermes-agent/security](https://github.com/NousResearch/hermes-agent/security).

## Supported versions

Fixes are applied to `main` and the latest release. Older versions do not receive backports; update your installed plugin with `hermes plugins update factonio`.

## Scope

In scope:

- Plugin runtime and orchestration (`__init__.py`, `agent_tools.py`, `commands.py`, `config.py`, `orchestrator.py`, `profile_setup.py`, `repos.py`, `watchdog.py`, `metrics.py`)
- Bundled phase skills (`skills/*/SKILL.md`)
- Dashboard FastAPI router (`dashboard/plugin_api.py`)
- Hermes Desktop UI module (`desktop/plugin.js`)

## Trust and security model

Factonio orchestrates autonomous code generation and GitHub pull request workflows on your machine. Understanding how the components trust each other is essential before running it on sensitive repositories:

- **Dedicated headless worker profile (`factonio`).** `hermes factonio init` configures the `factonio` profile with `approvals.mode: off` so Kanban workers can run tests, git commands, and `gh` non-interactively. Workers execute with the OS user's permissions and `gh` authentication token. Only register repositories you trust autonomous workers to edit, test, and open PRs against.
- **Repository merge policy and exact-HEAD gate.** By default, registered repositories use `merge_policy: auto`, which squash-merges once GitHub CI and both isolated verification checks pass *and* the live PR `head_sha` matches the verified commit SHA. Set `--merge-policy ask` (globally via `default_merge_policy: ask` or per repo via `hermes factonio update <repo> --merge-policy ask`) on repositories that require human merge approval.
- **Repository publish policy and exact-SHA publish gate.** By default (`publish_policy: auto`), Implementation workers push branches and open GitHub PRs themselves. Set `publish_policy: ask` (globally via `default_publish_policy: ask` or per repo via `hermes factonio update <repo> --publish-policy ask`) to keep Implementation and Verification on local, unpublished branches: workers are instructed never to run `git push` or `gh pr create`, and code reaches GitHub only through the explicit `publish_pr` gate (`factonio_publish_pr` / `hermes factonio publish-pr`) after a human approves. `publish_pr` fails closed before any remote call on invalid input, an unregistered repo or missing checkout, an unknown remote, `branch == base`, or any drift between `refs/heads/<branch>` and the full `expected_head_sha`; it pushes exactly `<expected_head_sha>:refs/heads/<branch>` (never the moving branch name), passes the PR body through a temporary `--body-file` (never shell-interpolated, always removed), reports `published` only after live `gh pr view` readback, and records every attempt in `trail.db`. The policy is an instruction to workers plus a dedicated gate, not an OS sandbox: workers still run with the user's `gh` token, so keep branch protection enabled. Publishing never bypasses GitHub CI, `factonio_pr_gate`, or `merge_policy`.
- **GitHub branch protection.** Always enable GitHub branch protection on `main` (requiring status checks and/or human reviews) on target repositories so repository-side controls enforce merge requirements independently of Factonio.
- **Holdout information barrier.** Factonio separates `holdout/` from Implementation and Diff Verification by prompt/workspace isolation, not by OS-level sandboxing. A worker running on the same OS user account could technically traverse the filesystem if prompted adversarially; treat `holdout/` isolation as an anti-bias and anti-overfitting safeguard rather than a hard security sandbox.
- **Local Dashboard API and Desktop UI.** `/api/plugins/factonio/*` runs inside Hermes Desktop's local dashboard server and persists repository settings to `<HERMES_HOME>/plugin-data/factonio/repos.json` using strict field allow-lists and input validation (rejecting invalid branch names, non-positive integers, and unknown keys). `desktop/plugin.js` only opens external PR URLs validated to start with `https://`.
