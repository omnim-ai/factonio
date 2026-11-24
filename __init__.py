"""Factonio: pure-Hermes, phase-based software factory plugin for any GitHub repository."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from .commands import register_cli
from .config import AUX_TASK_SPECS, PLUGIN_ID, WORKER_PROFILE
from .agent_tools import register_tools
from .watchdog import run_watchdog_tick

logger = logging.getLogger("factonio")

PLUGIN_DIR = Path(__file__).resolve().parent

SKILL_NAMES: tuple[str, ...] = (
    "orchestrator",
    "define",
    "design",
    "implement",
    "verify-diff",
    "verify-blackbox",
)

ORCHESTRATOR_PROMPT_SECTION = """## Factonio Software Factory

Factonio is active for orchestrating software delivery on registered GitHub repositories using Definition, optional Phase 1b Design (interactive Claude Design `/design` or pure-Hermes fallback), Implementation, and isolated parallel Verification on the dedicated headless `factonio` worker profile.

- When the user asks to work on a GitHub repo through Factonio, load `factonio:orchestrator` via `skill_view(name="factonio:orchestrator")` for the full workflow.
- Register new repositories with `factonio_add_repo` (or `hermes factonio add owner/repo`). Change a registered repo's merge_policy, max_fix_rounds, default_branch, design_provider, or design_approval with `factonio_update_repo` (or `hermes factonio update <repo>`); pass `inherit` to clear design overrides.
- Start workstreams with `factonio_start`, spawn Phase 1b UI design with `factonio_spawn_design` when a task touches UI (or on request), fan out parallel worktree units with `factonio_spawn_implement`, and always verify PRs with `factonio_spawn_verify` (which launches Diff Check and Black-Box Check in parallel with mutually exclusive inputs to prevent confirmation bias).
- Before reporting a PR ready or merging, verify live HEAD and CI via `factonio_pr_gate`. Auto-merge with `factonio_merge_pr` only when CI and both isolated verification checks pass (unless the repo sets `merge_policy: ask`).
- If the user reports a bug in work that passed Verification, start the fix with `escaped_from=<verify_card_id>` so escape rate stays measurable.
"""

RETRO_BLUEPRINT_PROMPT = """Inspect recently merged Factonio pull requests and Kanban phase metrics using `factonio_status` and `read_file` on recent worker outputs.
Identify any phase whose duration exceeded 2x the repository median or required multiple fix rounds, determine the concrete root cause, and write a brief, actionable improvement proposal (skill or workflow adjustment) without changing repository code."""


def _parse_frontmatter(skill_md: Path) -> tuple[str, dict[str, Any]]:
    text = skill_md.read_text(encoding="utf-8")
    if not text.startswith("---"):
        return "", {}
    parts = text.split("---", 2)
    if len(parts) < 3:
        return "", {}
    try:
        import hermes_yaml as yaml

        fm = yaml.safe_load(parts[1]) or {}
        if not isinstance(fm, dict):
            return "", {}
        return str(fm.get("description") or "").strip(), fm
    except Exception:
        return "", {}


def register(ctx: Any) -> None:
    """Register Factonio's auxiliary model slots, skills, tools, CLI, watchdog, and blueprint."""
    # 1. Per-phase configurable model slots (Hermes native Auxiliary Models picker)
    for spec in AUX_TASK_SPECS:
        try:
            ctx.register_auxiliary_task(
                spec["key"],
                display_name=spec["display_name"],
                description=spec["description"],
                defaults={"provider": "auto", "model": ""},
            )
        except Exception as exc:
            logger.debug("factonio auxiliary task %s skipped: %s", spec["key"], exc)

    # 2. Bundled phase skills (read-only, resolvable as `factonio:<name>`)
    skills_root = PLUGIN_DIR / "skills"
    for skill_name in SKILL_NAMES:
        skill_md = skills_root / skill_name / "SKILL.md"
        if skill_md.is_file():
            desc, fm = _parse_frontmatter(skill_md)
            ctx.register_skill(skill_name, skill_md, desc, fm)

    # 3. Slim orchestrator prompt section (only on interactive/non-worker sessions)
    def _prompt_section(session_info: Any) -> str:
        if os.environ.get("HERMES_KANBAN_TASK"):
            return ""
        try:
            if ctx.profile_name() == WORKER_PROFILE:
                return ""
        except Exception:
            pass
        return ORCHESTRATOR_PROMPT_SECTION

    ctx.register_system_prompt_section(
        f"{PLUGIN_ID}-routing",
        _prompt_section,
        position="after_memory",
        max_chars=3000,
    )

    # 4. Agent tools & CLI subcommand
    register_tools(ctx)
    register_cli(ctx, PLUGIN_DIR)

    # 5. In-process Kanban dispatch-tick watchdog
    def _on_tick(**kwargs: Any) -> None:
        run_watchdog_tick(
            ctx,
            board=kwargs.get("board"),
            dry_run=bool(kwargs.get("dry_run", False)),
        )

    ctx.register_hook("on_kanban_dispatch_tick", _on_tick)

    # 6. Optional Automation Blueprint for outlier retrospectives
    try:
        ctx.register_automation_blueprint(
            "retro",
            title="Factonio Phase Retro",
            description="Inspect recent merged PRs with slow phases or rework and propose concrete improvements.",
            schedule_template="every 6h",
            prompt_template=RETRO_BLUEPRINT_PROMPT,
            category="engineering",
            deliver_default="origin",
            skills=[f"{PLUGIN_ID}:orchestrator"],
            tags=["factonio", "retro", "engineering"],
        )
    except Exception as exc:
        logger.debug("factonio blueprint registration skipped: %s", exc)
