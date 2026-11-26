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
- Register new repositories with `factonio_add_repo` (or `hermes factonio add owner/repo`). Change a registered repo's merge_policy, publish_policy, max_fix_rounds, default_branch, design_provider, or design_approval with `factonio_update_repo` (or `hermes factonio update <repo>`); pass `inherit` to clear design overrides. `default_rigor` is editable the same way.
- Start workstreams with `factonio_start`, spawn Phase 1b UI design with `factonio_spawn_design` when a task touches UI (or on request), fan out parallel worktree units with `factonio_spawn_implement`, and always verify with `factonio_spawn_verify` (parallel Diff Check and Black-Box Check with mutually exclusive inputs to prevent confirmation bias).
- Before reporting a PR ready or merging, verify live HEAD and CI via `factonio_pr_gate`. Auto-merge with `factonio_merge_pr` only when CI and both isolated verification checks pass (unless the repo sets `merge_policy: ask`).
- With `publish_policy: ask`, Implementation is local-only (no push/PR): verify the local branch with `factonio_spawn_verify(branch=..., head_sha=...)`, then publish with `factonio_publish_pr` only after explicit human approval.
- Stack a unit on an unmerged parent PR with `factonio_spawn_implement(base_branch=..., stack_parent_pr=...)`; after the parent merges, run `factonio_restack_pr` and re-verify only when it reports `reverify_required`.
- For risky PRs, `factonio_spawn_verify(rigor="high")` (or repo `default_rigor: high`) fans out a multi-vendor verify panel; merge only when `factonio_evaluate_panel` returns `consensus: pass`.
- For a contested spec or design, `factonio_spawn_arena` (opt-in) runs competing cross-vendor candidates plus a judge; track it with `factonio_evaluate_arena`.
- If the user reports a bug in work that passed Verification, start the fix with `escaped_from=<verify_card_id>` so escape rate stays measurable.
- Bootstrap or refresh a repo's `.factonio/verify.md` black-box harness with `factonio_bootstrap_verify` (or `hermes factonio harness init|check|show|audit <repo>`); `factonio_spawn_verify` feeds it to Black-Box cards automatically.
- Factory transitions are recorded in the append-only decision trail (`trail.db`); log forks, pivots, and merge countersigns with `factonio_log_decision`, and correct a decision by logging a new row with `supersedes_id`.
"""

RETRO_BLUEPRINT_PROMPT = """Run a Factonio `/correct` retrospective. Do not change repository code; produce a proposal.

1. Gather evidence from BOTH sources:
   - Kanban phase metrics: `factonio_status` (all=true) and the Desktop `/metrics` data (phase medians/P90, Diff vs Black-Box pass rates, rework, escape rate).
   - The append-only decision trail `trail.db`: run `hermes factonio trail --json --limit 500` (or `--markdown`) and read gate outcomes, fix rounds, superseded decisions, Attack the Premise probes, and `escape` rows.
   - Long-term KPI trends from `trail.db` `metric_snapshots` (captured automatically on merges, panel consensus, and retros; they survive `kanban.db` resets): run `hermes factonio metrics --history --json` and compare pass rates, first-pass rate, escape rate, and rework across snapshots.
   - Evidence gaps: run `hermes factonio trail --audit --json` (read-only) and treat `missing_evidence`, `weak_evidence`, `unclosed_escalation`, and `gate_loosening` findings as candidate mistake classes too.
   - The Rule-to-Enforcer ledger: read the `## Rule-to-Enforcer Ledger` table in each affected repository's `AGENTS.md` (columns: Rule | Enforcer | Rung | Occurrences | Last seen). A missing table is itself a finding: propose creating it.
2. Flag outliers: phases slower than 2x the repository median, cards with multiple fix rounds, verification rejects, and regressions that escaped verification.
3. Mine recurring mistake classes: group failures by root cause (not by symptom or card). Only a class with >= 2 occurrences qualifies; cite every occurrence (card id, PR, decision id). Single occurrences are listed as watch items only.
4. For each recurring class, apply the `/correct` enforcement ladder and pick the HIGHEST rung that can make the mistake impossible or loudly caught:
   Architecture (make it unrepresentable) -> Types -> Lint/CI check whose failure message names the fix -> Behavioral test -> Skill text last.
   Skill/prompt text is acceptable only when you show why every higher rung cannot work.
   Ledger maintenance (Rule-to-Enforcer):
   - Forced promotion: when a class matches a ledger rule that is enforced only by prose (Skill text or `AGENTS.md` wording) and it repeated (>= 2 occurrences since that rule was adopted), the prose has failed. You MUST propose promoting it at least one rung up the ladder; re-wording the prose is not an option.
   - Pruning: when Architecture or Types now make the mistake unrepresentable, propose dropping the prose rule from the ledger and the skills, citing the enforcing `file:line`. Rules that no longer guard anything only add noise.
   - Every proposal includes the exact ledger row diff (new row, promoted rung, updated occurrences, or removed row).
5. Prove before adopting: a proposed check or test MUST be shown to fail on a real past failure from the trail (replay the bad commit, input, or fixture) and pass on the fix. Report the exact command and both outcomes; drop any proposal you cannot prove.
6. Hardening cards: for each adopted class, propose one scoped `[implement]` hardening card (one commit per class: the enforcer, the proving test or fixture, and the `AGENTS.md` ledger row together; never batch classes). The body carries the occurrences, the target rung, and the fail-before/pass-after proof command. Prepare cards with `factonio_spawn_implement` only when the user has approved the proposal or the schedule explicitly allows it; otherwise list them as ready-to-spawn.
7. Record the outcome with `factonio_log_decision` (phase `retro`, evidence = occurrences + proof command), superseding earlier retro decisions it replaces; a `retro` decision also captures a metrics snapshot, so the next retro can measure whether the correction moved the trend.
8. Deliver a decision-ready handoff: plain language, the key table or chart first, then one recommendation per class with its evidence and risk."""


HARNESS_AUDIT_BLUEPRINT_PROMPT = """Run a Factonio repo verification harness live pass: a drift audit plus a live gate check. Do not edit repository code or push commits.

1. List registered repositories with `hermes factonio --json list`.
2. For each repo, run `hermes factonio harness audit <repo> --json` (strictly read-only: git probes use `--no-optional-locks`; the repo is never mutated) and then the live pass `hermes factonio harness check <repo> --run-doctor --run-hooks --json` (Doctor commands + every declared hook, failing on any required hook or Doctor command removed/downgraded since `HEAD`).
3. Report per repo: `severity` (`clean` | `stale` | `missing`), `uncovered_changed_files`, `stale_features`, `orphaned_globs`, `new_routes` / `removed_routes`, `new_cli_commands` / `removed_cli_commands`, `gate_loosening` (`removed_required_hooks`, `downgraded_hooks`, `removed_doctor_commands`), the check's `valid` / `errors` / failing hooks, and the `recommendations`.
4. Classify every repo with the 3-way drift rubric (the audit's `drift_classification` plus the check results; priority `product_regression` > `harness_gap` > `doc_drift` > `clean`) and recommend exactly the matching action:
   - `doc_drift` (the `.factonio/` playbook no longer describes the repo: missing/stale harness, section errors, dead globs, loosened gates) -> fix `.factonio/`: `hermes factonio harness init <repo> --force` (declared hooks are kept; add `--prove`) or a scoped `[implement]` card via `factonio_spawn_implement` for human-quality Drive/Feature Map edits, followed by a reviewed commit. Restore removed or downgraded required gates; never drop them.
   - `harness_gap` (the harness is broken or a gate could not run: malformed `harness.json` / unsupported `schema_version`, Doctor failure, hook timeout, command not found) -> fix the harness helper or environment (repair the manifest, prerequisites, hook command paths, timeouts) so the gate runs again; do not touch product code and do not weaken the gate.
   - `product_regression` (a required hook ran and failed) -> report the product bug (a scoped `[implement]` fix card). Never edit `.factonio/` to paper over broken product behavior: do not remove, downgrade to optional, or loosen the failing hook.
5. Log one `factonio_log_decision` row per non-clean repo (phase `harness`, decision `harness_drift`, result = the classification, evidence = the audit counts, failing hooks, gate_loosening findings and base commit).
6. Deliver a short, decision-ready summary: a table of repos x severity x classification first, then the recommended actions."""


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
            description="/correct retro: mine Kanban metrics and the trail.db decision trail for recurring mistakes and propose proven, highest-rung fixes.",
            schedule_template="every 6h",
            prompt_template=RETRO_BLUEPRINT_PROMPT,
            category="engineering",
            deliver_default="origin",
            skills=[f"{PLUGIN_ID}:orchestrator"],
            tags=["factonio", "retro", "engineering"],
        )
    except Exception as exc:
        logger.debug("factonio blueprint registration skipped: %s", exc)

    # 7. Automation Blueprint for read-only repo verification harness drift audits
    try:
        ctx.register_automation_blueprint(
            "harness-audit",
            title="Factonio Harness Drift Audit",
            description="Read-only audit of every registered repo's .factonio/ verification harness for uncovered changes, stale features, and new/removed routes or CLI commands.",
            schedule_template="every 24h",
            prompt_template=HARNESS_AUDIT_BLUEPRINT_PROMPT,
            category="engineering",
            deliver_default="origin",
            skills=[f"{PLUGIN_ID}:orchestrator"],
            tags=["factonio", "harness", "verification", "engineering"],
        )
    except Exception as exc:
        logger.debug("factonio harness-audit blueprint registration skipped: %s", exc)
