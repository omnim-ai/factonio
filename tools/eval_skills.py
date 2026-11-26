#!/usr/bin/env python3
"""Standalone blind skill & model eval harness (stdlib only, no Hermes install needed).

Usage:
    python3 tools/eval_skills.py ab A.json B.json --phase implement [--seed S] [--judge-model M] [--dry-run] [--json]
    python3 tools/eval_skills.py audit transcript.json --phase verify-blackbox [--json]
    python3 tools/eval_skills.py bloat skills [--baseline OLD.md] [--strict] [--json]   # file(s) or skill directories

Same behavior as `hermes factonio eval ...`; exits 1 on errors or barrier leaks
(and on over-budget skills only with `bloat --strict` / `--fail-on-over-budget`).
"""

from __future__ import annotations

import argparse
import importlib
import sys
import types
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
_PKG = "_factonio_eval_standalone"


def _load_eval_module() -> types.ModuleType:
    # Load the plugin modules as a synthetic package so relative imports resolve without
    # executing the plugin's __init__.py (which needs a Hermes runtime).
    if _PKG not in sys.modules:
        pkg = types.ModuleType(_PKG)
        pkg.__path__ = [str(REPO_ROOT)]
        sys.modules[_PKG] = pkg
    return importlib.import_module(f"{_PKG}.eval_skills")


def main(argv: list[str] | None = None) -> int:
    mod = _load_eval_module()
    parser = argparse.ArgumentParser(
        prog="eval_skills.py",
        description="Deterministic blind A/B, Information Barrier audit, and skill bloat checks for Factonio.",
    )
    mod.add_eval_arguments(parser)
    return mod.run_eval_cli(parser.parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
