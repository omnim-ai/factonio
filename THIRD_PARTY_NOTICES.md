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
