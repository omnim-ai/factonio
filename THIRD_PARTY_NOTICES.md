# Third-Party Notices

Factonio adapts engineering patterns and instruction text from the third-party project listed below. Its license notice is reproduced here as its license requires.

## pstack

- Source: https://github.com/cursor/plugins/tree/main/pstack
- Author: Lauren Tan ([@poteto](https://x.com/poteto))
- License: MIT
- Used in: `profile_setup.py` (worker execution contract) and the bundled phase skills under `skills/` (`define`, `implement`, `verify-diff`, `verify-blackbox`, `orchestrator`).
- Wave 2 adapted patterns:
  - `show-me-your-work`: the append-only decision trail (`trail.py`, `factonio_log_decision`, `hermes factonio trail`).
  - `ketchup`: decision-ready human handoff rules (`skills/orchestrator/SKILL.md`).
  - `/correct`: recurring-mistake mining, the enforcement ladder, and the prove-it-fails rule in the retro blueprint (`__init__.py`) and Verification Gap Analysis (`orchestrator.py`, `skills/define/SKILL.md`).
  - `worktree-audit.sh`: safety-bucketed worktree audit and opt-in pruning (`repos.py`, `hermes factonio worktrees`).
  - side-effect liveness: watchdog progress detection beyond heartbeats (`watchdog.py`).
- Wave 3 adapted patterns:
  - `create-verification-skill`: the repo-local verification harness with `Launch` / `Doctor` / `Drive` / `Evidence` / `Cleanup` sections and a feature map (`harness.py`, `.factonio/verify.md` + `.factonio/harness.json`, `factonio_bootstrap_verify`, `hermes factonio harness init`), fed to Black-Box Verification (`orchestrator.py`, `skills/verify-blackbox/SKILL.md`).
  - `maintain-verification-skill`: scheduled read-only feature-map drift audits that report `clean` / `stale` / `missing` and never edit product code (`audit_harness`, `hermes factonio harness audit`, the `harness-audit` blueprint in `__init__.py`).
  - stacked PR landing: `base_branch` / `stack_parent_pr` implementation cards, true-base PR gate/merge, and clean-worktree `git rebase --onto` restacks with `git patch-id` verdict continuity (`repos.py`, `orchestrator.py`, `factonio_restack_pr`, `hermes factonio restack`).
  - multi-model review panel: opt-in `rigor="high"` verification that fans independent reviewers out across distinct vendor families and only passes on unanimous agreement, surfacing disagreements for escalation (`config.py` `resolve_verify_panel`, `orchestrator.py` `aggregate_panel_verdicts` / `evaluate_verification_panel`, `factonio_evaluate_panel`, `hermes factonio panel`).
- Wave 4 adapted patterns:
  - `create-verification-skill` proof step: `harness init --prove` runs Doctor, required hooks and an evidence-survives-cleanup probe and fails closed with rollback (`harness.py` `bootstrap_verify_harness(prove=True)`, `factonio_bootstrap_verify`, `hermes factonio harness init --prove`).
  - `maintain-verification-skill` live pass: read-only gate-loosening detection (`detect_harness_gate_loosening`, `harness check --base`), the `doc_drift` / `harness_gap` / `product_regression` drift rubric (`classify_harness_drift`, the `harness-audit` blueprint in `__init__.py`), and Black-Box user-POV preconditions plus the `verified-unreachable` reporting rule (`skills/verify-blackbox/SKILL.md`).
  - gate integrity, `comment-sicko` `MUST KILL`, the blast-radius certainty ladder, and `dropped` hypothesis transparency in Diff Check reviews (`skills/verify-diff/SKILL.md`).
  - tiered fix-round re-verification and a cross-model pre-handoff audit of the `show-me-your-work` trail (`skills/orchestrator/SKILL.md`, `trail.py` `audit_trail_evidence`, `hermes factonio trail --audit`).
  - `/correct` Rule-to-Enforcer ledger with forced ladder promotion, rule pruning, and 1-commit-per-class hardening cards (`RETRO_BLUEPRINT_PROMPT` in `__init__.py`).
  - first-tick silent-start liveness check (`watchdog.py` `silent_start`, `initial_progress_minutes`).

```text
MIT License

Copyright (c) 2026 Lauren Tan

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```
