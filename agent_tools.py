"""Agent-callable tools registered by Factonio."""

from __future__ import annotations

import json
import os
import re
from typing import Any, Optional

from .config import (
    PLUGIN_ID,
    check_cross_model_warning,
    list_repos,
    resolve_all_phase_models,
    resolve_design_provider,
)
from .harness import (
    HOOK_KINDS,
    audit_harness,
    bootstrap_verify_harness,
    check_harness,
    show_harness,
)
from .metrics import capture_metrics_snapshot
from .orchestrator import (
    evaluate_verification_panel,
    spawn_design_card,
    spawn_implementation_card,
    spawn_verification_pair,
    start_workstream,
    workstream_status,
)
from .repos import (
    EDITABLE_REPO_FIELDS,
    add_repo,
    inspect_pr_gate,
    inspect_stack_gate,
    merge_pr,
    restack_pr,
    restack_stack,
    update_repo,
)
from .trail import _GATE_LOOSENING_TOKENS, _norm_token_text, log_decision, record_transition


def _json_ok(payload: dict[str, Any]) -> str:
    return json.dumps(payload, default=str)


def _json_err(exc: Exception) -> str:
    return json.dumps({"ok": False, "error": str(exc)})


def _trail_context(args: dict[str, Any]) -> dict[str, Any]:
    """Tenant/card/actor for tool-originated trail rows, defaulting to the Kanban worker env."""
    worker_card = os.environ.get("HERMES_KANBAN_TASK") or ""
    return {
        "tenant": args.get("tenant") or os.environ.get("HERMES_TENANT") or "",
        "card_id": args.get("card_id") or worker_card,
        "actor": args.get("actor") or ("worker" if worker_card else "orchestrator"),
    }


def _scrub_loosening_hints(why: str, fallback: str) -> str:
    """Drop clauses that merely *mention* a gate-loosening knob (e.g. a refusal's override hint).

    `trail.audit_trail_evidence` flags `_GATE_LOOSENING_TOKENS` anywhere in `why`; real overrides are
    recorded in `evidence`, so a hint in refusal prose would be a permanent false positive.
    """
    tokens = [_norm_token_text(t) for t in _GATE_LOOSENING_TOKENS]
    clauses = re.split(r"\s*;\s*|,\s+or\s+", why)
    kept = [c for c in clauses if c and not any(t in _norm_token_text(c) for t in tokens)]
    return "; ".join(kept) or fallback


def _gate_result(gate: dict[str, Any]) -> str:
    if not gate.get("ok"):
        return "error"
    return "ready" if gate.get("ready_to_merge") else "blocked"


def _record_pr_gate(args: dict[str, Any], gate: dict[str, Any]) -> None:
    checks = gate.get("checks") if isinstance(gate.get("checks"), dict) else {}
    blockers = list(gate.get("blockers") or [])
    result = _gate_result(gate)
    why = (
        "All merge gate tiers clear"
        if result == "ready"
        else ("; ".join(blockers) if blockers else str(gate.get("error") or "gate inspection failed"))
    )
    record_transition(
        "pr_gate",
        repo_slug=str(args.get("repo") or ""),
        phase="gate",
        why=_scrub_loosening_hints(why, f"PR gate {result}")[:500],
        evidence={
            "pr_number": int(args["pr_number"]),
            "head_sha": gate.get("head_sha"),
            "blocker_tier": gate.get("blocker_tier"),
            "checks_passed": len(checks.get("passed") or []),
            "checks_failed": len(checks.get("failed") or []),
            "checks_pending": len(checks.get("pending") or []),
        },
        result=result,
        **_trail_context(args),
    )


def _record_stack_gate(args: dict[str, Any], stack: dict[str, Any], pr_numbers: list[int]) -> None:
    result = _gate_result(stack)
    next_action = stack.get("next_action") if isinstance(stack.get("next_action"), dict) else {}
    if result == "ready":
        why = "All stacked PRs clear every merge gate tier"
    elif result == "error":
        why = str(stack.get("error") or "stack gate inspection failed")
    else:
        why = f"next_action={next_action.get('action')} on PR #{next_action.get('pr_number')}: " + "; ".join(
            str(b) for b in (next_action.get("blockers") or [])
        )
    record_transition(
        "pr_gate",
        repo_slug=str(args.get("repo") or ""),
        phase="gate",
        why=_scrub_loosening_hints(why, f"Stack gate {result}")[:500],
        evidence={
            "stack_pr_numbers": pr_numbers,
            "order": stack.get("order"),
            "contiguous_ready_run": stack.get("contiguous_ready_run"),
            "ceiling": stack.get("ceiling"),
            "highest_blocker_tier": stack.get("highest_blocker_tier"),
        },
        result=result,
        **_trail_context(args),
    )


def _record_merge(args: dict[str, Any], res: dict[str, Any], expected_head_sha: Optional[str]) -> None:
    if res.get("merged"):
        result = "merged"
    elif res.get("queued"):
        result = "queued"
    else:
        result = "refused"
    pr_number = int(args["pr_number"])
    gate = res.get("gate") if isinstance(res.get("gate"), dict) else {}
    if result == "refused" and gate.get("is_stacked") and not args.get("allow_stacked_merge"):
        why = f"Refused stacked PR #{pr_number} on {gate.get('base_branch')!r}: merge the stack bottom-up and restack"
    else:
        why = str(res.get("error") or f"PR #{pr_number} {result}")
    method = res.get("method") or args.get("method") or "squash"
    record_transition(
        "merge_pr",
        repo_slug=str(args.get("repo") or ""),
        phase="merge",
        why=_scrub_loosening_hints(why, f"PR #{pr_number} {result}")[:500],
        evidence={
            "pr_number": pr_number,
            "head_sha": res.get("head_sha") or gate.get("head_sha"),
            "expected_head_sha": expected_head_sha,
            # merge_pr falls back to squash for unknown methods; never echo caller text into evidence.
            "method": method if method in ("squash", "merge", "rebase") else "squash",
            "url": res.get("url"),
            # Overrides are logged only when used so trail audits can flag them.
            **({"allow_stacked_merge": True} if args.get("allow_stacked_merge") else {}),
            **({"allow_patch_continuity": True} if args.get("allow_patch_continuity") else {}),
        },
        result=result,
        **_trail_context(args),
    )


def register_tools(ctx: Any) -> None:
    """Register Factonio's agent tools on `PluginContext`."""

    def handle_add_repo(args: dict[str, Any], **kwargs: Any) -> str:
        try:
            res = add_repo(
                str(args["repo"]),
                local_path=args.get("local_path"),
                merge_policy=args.get("merge_policy"),
                max_fix_rounds=args.get("max_fix_rounds"),
                clone=bool(args.get("clone", True)),
            )
            return _json_ok(res)
        except Exception as exc:
            return _json_err(exc)

    ctx.register_tool(
        name="factonio_add_repo",
        toolset=PLUGIN_ID,
        schema={
            "name": "factonio_add_repo",
            "description": (
                "Register a GitHub repository (`owner/repo` or URL) with Factonio, clone it locally if missing, "
                "anchor a Hermes Project for deterministic worktrees, and inspect required GitHub CI checks."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "repo": {"type": "string", "description": "GitHub repo as 'owner/repo' or full GitHub URL."},
                    "local_path": {"type": "string", "description": "Optional local checkout path override."},
                    "merge_policy": {"type": "string", "enum": ["auto", "ask"], "description": "PR merge policy."},
                    "max_fix_rounds": {"type": "integer", "minimum": 1, "description": "Max repair loops per PR."},
                    "clone": {"type": "boolean", "default": True, "description": "Clone via gh if not present locally."},
                },
                "required": ["repo"],
            },
        },
        handler=handle_add_repo,
    )

    def handle_update_repo(args: dict[str, Any], **kwargs: Any) -> str:
        try:
            updates = {k: v for k, v in args.items() if k != "repo"}
            unknown = sorted(set(updates) - set(EDITABLE_REPO_FIELDS))
            if unknown:
                raise ValueError(
                    f"Unknown or immutable field(s): {', '.join(unknown)}. "
                    f"Editable fields: {', '.join(EDITABLE_REPO_FIELDS)}"
                )
            return _json_ok(update_repo(str(args["repo"]), **updates))
        except Exception as exc:
            return _json_err(exc)

    ctx.register_tool(
        name="factonio_update_repo",
        toolset=PLUGIN_ID,
        schema={
            "name": "factonio_update_repo",
            "description": (
                "Update settings of an already-registered Factonio repository. Only fields you pass are changed: "
                "merge_policy, max_fix_rounds, default_branch, design_provider, design_approval, default_rigor. Pass "
                "'inherit' (or null) for design_provider/design_approval/default_rigor to clear the per-repo override "
                "and use the global default."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "repo": {"type": "string", "description": "Registered repo slug or 'owner/repo'."},
                    "merge_policy": {"type": "string", "enum": ["auto", "ask"], "description": "PR merge policy."},
                    "max_fix_rounds": {"type": "integer", "minimum": 1, "description": "Max repair loops per PR."},
                    "default_branch": {"type": "string", "description": "Base branch for PRs and worktrees."},
                    "design_provider": {
                        "type": "string",
                        "enum": ["auto", "claude-design", "hermes", "off", "inherit"],
                        "description": "Phase 1b design provider override; 'inherit' clears it.",
                    },
                    "design_approval": {
                        "type": "string",
                        "enum": ["ask", "auto", "inherit"],
                        "description": "Design approval override; 'inherit' clears it.",
                    },
                    "default_rigor": {
                        "type": "string",
                        "enum": ["standard", "high", "inherit"],
                        "description": (
                            "Verification rigor override: 'high' runs a multi-vendor verify panel; 'inherit' clears it."
                        ),
                    },
                },
                "required": ["repo"],
            },
        },
        handler=handle_update_repo,
    )

    def handle_start(args: dict[str, Any], **kwargs: Any) -> str:
        try:
            res = start_workstream(
                str(args["repo"]),
                str(args["request"]),
                kind=str(args.get("kind") or "feature"),
                title=args.get("title"),
                issue_number=args.get("issue_number"),
                skip_define=bool(args.get("skip_define", False)),
                escaped_from=args.get("escaped_from"),
                session_id=kwargs.get("session_id"),
            )
            return _json_ok(res)
        except Exception as exc:
            return _json_err(exc)

    ctx.register_tool(
        name="factonio_start",
        toolset=PLUGIN_ID,
        schema={
            "name": "factonio_start",
            "description": (
                "Start a Factonio software factory workstream on a registered GitHub repo. Creates the initial "
                "Definition card (or direct Implementation card for quick tasks) on the dedicated `factonio` "
                "worker profile with the configured phase model."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "repo": {"type": "string", "description": "Registered repo slug or 'owner/repo'."},
                    "request": {"type": "string", "description": "Full problem statement, goal, or issue context."},
                    "kind": {"type": "string", "enum": ["feature", "bug", "quick"], "default": "feature"},
                    "title": {"type": "string", "description": "Short workstream title."},
                    "issue_number": {"type": "integer", "description": "Optional GitHub issue number."},
                    "skip_define": {"type": "boolean", "default": False, "description": "Skip Definition phase."},
                    "escaped_from": {"type": "string", "description": "Prior verified card id if recording an escaped bug."},
                },
                "required": ["repo", "request"],
            },
        },
        handler=handle_start,
    )

    def handle_spawn_design(args: dict[str, Any], **kwargs: Any) -> str:
        try:
            res = spawn_design_card(
                str(args["repo"]),
                str(args["tenant"]),
                title=args.get("title"),
                brief=args.get("brief"),
                provider=args.get("provider"),
                parents=args.get("parents"),
                session_id=kwargs.get("session_id"),
                idempotency_key=args.get("idempotency_key"),
            )
            return _json_ok(res)
        except Exception as exc:
            return _json_err(exc)

    ctx.register_tool(
        name="factonio_spawn_design",
        toolset=PLUGIN_ID,
        schema={
            "name": "factonio_spawn_design",
            "description": (
                "Spawn a Phase 1b Design card for UI/UX work in an existing Factonio workstream. "
                "Drives interactive Claude Design (/design) when available or falls back to pure-Hermes prototyping, "
                "writing DESIGN.md, static HTML previews, and screenshots to the run's design/ directory without exposing holdout/."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "repo": {"type": "string", "description": "Registered repo slug or 'owner/repo'."},
                    "tenant": {"type": "string", "description": "Workstream tenant slug."},
                    "title": {"type": "string", "description": "Short design task title."},
                    "brief": {"type": "string", "description": "UI/UX design brief, constraints, and surface scope."},
                    "provider": {
                        "type": "string",
                        "enum": ["auto", "claude-design", "hermes", "off"],
                        "description": "Optional per-card override for design_provider.",
                    },
                    "parents": {"type": "array", "items": {"type": "string"}, "description": "Parent card ids (e.g. Definition card)."},
                    "idempotency_key": {"type": "string", "description": "Optional retry-safe key."},
                },
                "required": ["repo", "tenant"],
            },
        },
        handler=handle_spawn_design,
    )

    def handle_spawn_implement(args: dict[str, Any], **kwargs: Any) -> str:
        try:
            res = spawn_implementation_card(
                str(args["repo"]),
                str(args["tenant"]),
                str(args["title"]),
                str(args["body"]),
                parents=args.get("parents"),
                session_id=kwargs.get("session_id"),
                idempotency_key=args.get("idempotency_key"),
                base_branch=args.get("base_branch") or None,
                stack_parent_pr=args.get("stack_parent_pr"),
            )
            return _json_ok(res)
        except Exception as exc:
            return _json_err(exc)

    ctx.register_tool(
        name="factonio_spawn_implement",
        toolset=PLUGIN_ID,
        schema={
            "name": "factonio_spawn_implement",
            "description": (
                "Create one scoped Implementation worktree card in an existing Factonio workstream, "
                "linked to `spec.md` without exposing `holdout/`, using the configured Implementation model. "
                "Pass `base_branch` (and `stack_parent_pr`) to stack the unit on a parent PR's branch."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "repo": {"type": "string", "description": "Registered repo slug or 'owner/repo'."},
                    "tenant": {"type": "string", "description": "Workstream tenant slug."},
                    "title": {"type": "string", "description": "Short task title."},
                    "body": {"type": "string", "description": "Scoped work unit instructions and acceptance criteria."},
                    "parents": {"type": "array", "items": {"type": "string"}, "description": "Parent card ids."},
                    "idempotency_key": {"type": "string", "description": "Optional retry-safe key."},
                    "base_branch": {
                        "type": "string",
                        "description": "Stacked PR base branch (default: the repo's default branch).",
                    },
                    "stack_parent_pr": {
                        "type": "integer",
                        "minimum": 1,
                        "description": "Parent PR number this unit stacks on.",
                    },
                },
                "required": ["repo", "tenant", "title", "body"],
            },
        },
        handler=handle_spawn_implement,
    )

    def handle_spawn_verify(args: dict[str, Any], **kwargs: Any) -> str:
        try:
            res = spawn_verification_pair(
                str(args["repo"]),
                str(args["tenant"]),
                int(args["pr_number"]),
                impl_card_id=args.get("impl_card_id"),
                head_sha=args.get("head_sha"),
                run_hint=args.get("run_hint"),
                rigor=args.get("rigor") or None,
                panel_models=args.get("panel_models") or None,
                session_id=kwargs.get("session_id"),
            )
            return _json_ok(res)
        except Exception as exc:
            return _json_err(exc)

    ctx.register_tool(
        name="factonio_spawn_verify",
        toolset=PLUGIN_ID,
        schema={
            "name": "factonio_spawn_verify",
            "description": (
                "Spawn Factonio's two isolated parallel Verification cards for a PR: Diff Check (spec + git diff, "
                "no holdout) and Black-Box Check (built artifact + holdout, no diff), both using the Verification model. "
                "rigor='high' (or the repo's default_rigor) instead fans out one Diff + Black-Box pair per member of a "
                "multi-vendor verify panel under a shared panel_id, failing closed unless the panel spans >= 2 vendor "
                "families; aggregate the lanes with factonio_evaluate_panel."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "repo": {"type": "string", "description": "Registered repo slug or 'owner/repo'."},
                    "tenant": {"type": "string", "description": "Workstream tenant slug."},
                    "pr_number": {"type": "integer", "description": "GitHub pull request number."},
                    "impl_card_id": {"type": "string", "description": "Optional parent Implementation card id."},
                    "head_sha": {"type": "string", "description": "Expected PR HEAD commit SHA."},
                    "run_hint": {"type": "string", "description": "Optional build/run/preview command hint for Black-Box Check."},
                    "rigor": {
                        "type": "string",
                        "enum": ["standard", "high"],
                        "description": "Verification rigor (default: repo default_rigor, then global, then 'standard').",
                    },
                    "panel_models": {
                        "type": "array",
                        "items": {
                            "anyOf": [
                                {"type": "string", "description": "'provider/model' or 'model'."},
                                {
                                    "type": "object",
                                    "properties": {
                                        "provider": {"type": "string"},
                                        "model": {"type": "string"},
                                        "label": {"type": "string"},
                                    },
                                    "required": ["model"],
                                },
                            ]
                        },
                        "description": "rigor=high: explicit panel members overriding the configured verify_panel.",
                    },
                },
                "required": ["repo", "tenant", "pr_number"],
            },
        },
        handler=handle_spawn_verify,
    )

    def handle_evaluate_panel(args: dict[str, Any], **kwargs: Any) -> str:
        try:
            pr_number = args.get("pr_number")
            res = evaluate_verification_panel(
                panel_id=args.get("panel_id") or None,
                tenant=args.get("tenant") or None,
                pr_number=int(pr_number) if pr_number is not None else None,
                record_trail=bool(args.get("record", True)),
            )
            return _json_ok(res)
        except Exception as exc:
            return _json_err(exc)

    ctx.register_tool(
        name="factonio_evaluate_panel",
        toolset=PLUGIN_ID,
        schema={
            "name": "factonio_evaluate_panel",
            "description": (
                "Aggregate a rigor='high' verification panel's lanes (read-only from Kanban) into a fail-closed "
                "consensus: any reject -> reject, any inconclusive -> inconclusive, unfinished lanes -> pending, pass "
                "only when every lane passed across >= 2 vendor families. Returns merge_allowed, per-verdict lanes, "
                "disagreement_summary and aggregated findings, and records terminal consensus in trail.db."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "panel_id": {"type": "string", "description": "Panel id (vp_...) from factonio_spawn_verify."},
                    "tenant": {"type": "string", "description": "Workstream tenant (latest panel when panel_id is omitted)."},
                    "pr_number": {"type": "integer", "description": "PR number (latest panel when panel_id is omitted)."},
                    "record": {
                        "type": "boolean",
                        "default": True,
                        "description": "Record a terminal consensus as verify_panel_consensus in trail.db.",
                    },
                },
            },
        },
        handler=handle_evaluate_panel,
    )

    def handle_status(args: dict[str, Any], **kwargs: Any) -> str:
        try:
            res = workstream_status(
                tenant=args.get("tenant"),
                session_id=None if args.get("all") else kwargs.get("session_id"),
            )
            res["repos"] = list(list_repos().keys())
            res["models"] = resolve_all_phase_models(repo_slug=args.get("repo"))
            res["design"] = resolve_design_provider(repo_slug=args.get("repo"))
            res["cross_model_warning"] = check_cross_model_warning(repo_slug=args.get("repo"))
            return _json_ok(res)
        except Exception as exc:
            return _json_err(exc)

    ctx.register_tool(
        name="factonio_status",
        toolset=PLUGIN_ID,
        schema={
            "name": "factonio_status",
            "description": (
                "Inspect active Factonio workstream cards, phase progress, registered repositories, "
                "and resolved per-phase provider/model assignments."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "tenant": {"type": "string", "description": "Optional workstream tenant filter."},
                    "repo": {"type": "string", "description": "Optional repo slug to inspect repo-specific model overrides."},
                    "all": {"type": "boolean", "default": False, "description": "Include cards from all sessions."},
                },
            },
        },
        handler=handle_status,
    )

    def handle_pr_gate(args: dict[str, Any], **kwargs: Any) -> str:
        stack_numbers = args.get("stack_pr_numbers")
        try:
            if stack_numbers:
                if not isinstance(stack_numbers, list):
                    raise ValueError("stack_pr_numbers must be a list of PR numbers")
                stack = inspect_stack_gate(str(args["repo"]), stack_numbers, fetch=bool(args.get("fetch", False)))
            elif args.get("pr_number") is not None:
                gate = inspect_pr_gate(
                    str(args["repo"]),
                    int(args["pr_number"]),
                    verified_head_sha=args.get("verified_head_sha") or None,
                    fetch=bool(args.get("fetch", False)),
                )
            else:
                raise ValueError("pass pr_number or stack_pr_numbers")
        except Exception as exc:
            return _json_err(exc)
        if stack_numbers:
            _record_stack_gate(args, stack, list(stack.get("pr_numbers") or stack_numbers))
            return _json_ok(stack)
        _record_pr_gate(args, gate)
        return _json_ok(gate)

    ctx.register_tool(
        name="factonio_pr_gate",
        toolset=PLUGIN_ID,
        schema={
            "name": "factonio_pr_gate",
            "description": (
                "Check a GitHub PR's live HEAD SHA, draft/mergeable status, and CI status checks via `gh`. "
                "Always run before reporting a PR ready or merging. Reports Tier 4 `approval_gates`, "
                "`review_automation_running`, `had_previous_passing_ci` (flake signal), tiered `trunk_drift` and "
                "`patch_continuity`; pass `stack_pr_numbers` to gate a whole stack bottom-to-top in tier-major order."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "repo": {"type": "string", "description": "Registered repo slug or 'owner/repo'."},
                    "pr_number": {"type": "integer", "description": "GitHub pull request number (or use stack_pr_numbers)."},
                    "stack_pr_numbers": {
                        "type": "array",
                        "items": {"type": "integer"},
                        "description": "Gate a stack of PRs (any order; ordered root-to-tip via base/head branches).",
                    },
                    "verified_head_sha": {
                        "type": "string",
                        "description": "HEAD the verdicts were given on; reports `patch_continuity` against the live head.",
                    },
                    "fetch": {
                        "type": "boolean",
                        "default": False,
                        "description": "Fetch the base branch and PR head into the registered checkout before drift checks.",
                    },
                    "tenant": {"type": "string", "description": "Workstream tenant for the decision trail (defaults to the worker's tenant)."},
                    "card_id": {"type": "string", "description": "Kanban card id for the decision trail (defaults to the worker's card)."},
                },
                "required": ["repo"],
            },
        },
        handler=handle_pr_gate,
    )

    def handle_merge_pr(args: dict[str, Any], **kwargs: Any) -> str:
        try:
            res = merge_pr(
                str(args["repo"]),
                int(args["pr_number"]),
                expected_head_sha=args.get("expected_head_sha"),
                method=str(args.get("method") or "squash"),
                delete_branch=bool(args.get("delete_branch", True)),
                tenant=_trail_context(args)["tenant"] or None,
                allow_patch_continuity=bool(args.get("allow_patch_continuity", False)),
                allow_stacked_merge=bool(args.get("allow_stacked_merge", False)),
                fetch=bool(args.get("fetch", False)),
            )
        except Exception as exc:
            return _json_err(exc)
        _record_merge(args, res, args.get("expected_head_sha"))
        return _json_ok(res)

    ctx.register_tool(
        name="factonio_merge_pr",
        toolset=PLUGIN_ID,
        schema={
            "name": "factonio_merge_pr",
            "description": (
                "Verify PR gates and exact HEAD commit SHA, then squash-merge a verified GitHub PR and delete its branch."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "repo": {"type": "string", "description": "Registered repo slug or 'owner/repo'."},
                    "pr_number": {"type": "integer", "description": "GitHub pull request number."},
                    "expected_head_sha": {"type": "string", "description": "Exact HEAD commit SHA verified by Diff and Black-Box checks."},
                    "method": {"type": "string", "enum": ["squash", "merge", "rebase"], "default": "squash"},
                    "delete_branch": {
                        "type": "boolean",
                        "default": True,
                        "description": "Delete the head branch after merging (keep it while child PRs still target it).",
                    },
                    "allow_stacked_merge": {
                        "type": "boolean",
                        "default": False,
                        "description": "Merge a stacked PR into its parent branch. Default refuses: land stacks bottom-up.",
                    },
                    "allow_patch_continuity": {
                        "type": "boolean",
                        "default": False,
                        "description": (
                            "Accept a live head that differs from expected_head_sha only when `git patch-id --stable` "
                            "proves the identical verified patch (mechanical rebase)."
                        ),
                    },
                    "fetch": {
                        "type": "boolean",
                        "default": False,
                        "description": "Fetch base/head into the registered checkout before drift and continuity checks.",
                    },
                    "tenant": {"type": "string", "description": "Workstream tenant for the decision trail (defaults to the worker's tenant)."},
                    "card_id": {"type": "string", "description": "Kanban card id for the decision trail (defaults to the worker's card)."},
                },
                "required": ["repo", "pr_number"],
            },
        },
        handler=handle_merge_pr,
    )

    def handle_bootstrap_verify(args: dict[str, Any], **kwargs: Any) -> str:
        try:
            action = str(args.get("action") or "init").strip().lower()
            repo = str(args["repo"])
            if action == "init":
                ctx_args = _trail_context(args)
                res = bootstrap_verify_harness(
                    repo,
                    force=bool(args.get("force", False)),
                    hooks=args.get("hooks"),
                    dry_run=bool(args.get("dry_run", False)),
                    tenant=ctx_args["tenant"] or None,
                    card_id=ctx_args["card_id"] or None,
                    prove=bool(args.get("prove", False)),
                )
            elif action == "check":
                res = check_harness(
                    repo,
                    run_doctor=bool(args.get("run_doctor", False)),
                    run_hooks=bool(args.get("run_hooks", False)),
                    base_ref=args.get("base_ref") or None,
                )
            elif action == "show":
                res = show_harness(repo)
            elif action == "audit":
                res = audit_harness(repo, base_ref=args.get("base_ref") or None)
            else:
                raise ValueError(f"action must be one of init, check, show, audit; got {action!r}")
            return _json_ok({"action": action, **res})
        except Exception as exc:
            return _json_err(exc)

    ctx.register_tool(
        name="factonio_bootstrap_verify",
        toolset=PLUGIN_ID,
        schema={
            "name": "factonio_bootstrap_verify",
            "description": (
                "Manage a repository's git-tracked verification harness under `.factonio/` (`verify.md` "
                "Launch/Doctor/Drive/Evidence/Cleanup playbook + Feature Map + Hooks, and `harness.json` schema v1). "
                "action=init auto-detects stack/routes/CLI and writes it (refuses to overwrite unless force; dry_run "
                "previews; prove runs Doctor, required hooks and an evidence-survival probe and fails closed with "
                "rollback); check validates it (optionally running Doctor commands and hooks) and fails when required "
                "hooks or Doctor commands were removed/downgraded vs base_ref (default HEAD); show returns it; audit "
                "reports read-only drift (uncovered changed files, stale features, new/removed routes and CLI "
                "commands, orphaned globs, gate_loosening) with a drift_classification (clean, doc_drift, "
                "harness_gap, product_regression). Black-Box Verification cards receive the harness automatically."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["init", "check", "show", "audit"], "default": "init"},
                    "repo": {"type": "string", "description": "Registered repo slug, 'owner/repo', or local checkout path."},
                    "force": {"type": "boolean", "default": False, "description": "init: overwrite an existing harness."},
                    "dry_run": {"type": "boolean", "default": False, "description": "init: return generated content without writing."},
                    "prove": {
                        "type": "boolean",
                        "default": False,
                        "description": "init: prove the harness (Doctor + required hooks + evidence survives cleanup); "
                        "on failure nothing is left written and ok is false.",
                    },
                    "hooks": {
                        "type": "array",
                        "description": "init: hooks to declare (kept from an existing harness when omitted).",
                        "items": {
                            "type": "object",
                            "properties": {
                                "name": {"type": "string"},
                                "kind": {"type": "string", "enum": list(HOOK_KINDS)},
                                "command": {"type": "string", "description": "Shell command run in the repo root."},
                                "optional": {"type": "boolean", "default": False},
                                "timeout": {"type": "integer", "minimum": 1},
                            },
                            "required": ["name", "kind", "command"],
                        },
                    },
                    "base_ref": {
                        "type": "string",
                        "description": "audit: compare against this git ref instead of harness.json base_commit; "
                        "check: fail when required hooks/Doctor commands committed at this ref (default HEAD) were "
                        "removed or downgraded.",
                    },
                    "run_doctor": {"type": "boolean", "default": False, "description": "check: also run the Doctor commands."},
                    "run_hooks": {"type": "boolean", "default": False, "description": "check: also run declared hooks."},
                    "tenant": {"type": "string", "description": "Workstream tenant for the decision trail (defaults to the worker's tenant)."},
                    "card_id": {"type": "string", "description": "Kanban card id for the decision trail (defaults to the worker's card)."},
                },
                "required": ["repo"],
            },
        },
        handler=handle_bootstrap_verify,
    )

    def handle_restack(args: dict[str, Any], **kwargs: Any) -> str:
        try:
            trail = _trail_context(args)
            common = {
                "new_base": args.get("new_base") or None,
                "old_base_sha": args.get("old_base_sha") or None,
                "verified_head_sha": args.get("verified_head_sha") or None,
                "push": bool(args.get("push", True)),
                "retarget": bool(args.get("retarget", True)),
                "dry_run": bool(args.get("dry_run", False)),
                "tenant": trail["tenant"],
                "card_id": trail["card_id"],
                "actor": trail["actor"],
            }
            if args.get("pr_number") is not None:
                res = restack_pr(
                    str(args["repo"]), int(args["pr_number"]), parent_pr=args.get("parent_pr"), **common
                )
            else:
                res = restack_stack(
                    str(args["repo"]),
                    parent_pr=args.get("parent_pr"),
                    from_branch=args.get("from_branch") or None,
                    **common,
                )
            return _json_ok(res)
        except Exception as exc:
            return _json_err(exc)

    ctx.register_tool(
        name="factonio_restack_pr",
        toolset=PLUGIN_ID,
        schema={
            "name": "factonio_restack_pr",
            "description": (
                "Restack a stacked PR (or every open child of a parent PR) onto its new base after the parent lands: "
                "rebases in a throwaway worktree with `git rebase --onto`, pushes with --force-with-lease, retargets via "
                "`gh pr edit --base`, and reports `verdict_continuous` by comparing `git patch-id --stable` before/after."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "repo": {"type": "string", "description": "Registered repo slug or 'owner/repo'."},
                    "pr_number": {"type": "integer", "description": "Child PR to restack (omit to restack all children)."},
                    "parent_pr": {"type": "integer", "description": "Parent PR whose head was the old base."},
                    "from_branch": {"type": "string", "description": "Restack all open PRs targeting this branch."},
                    "new_base": {"type": "string", "description": "New base branch (default: parent's base or repo default)."},
                    "old_base_sha": {"type": "string", "description": "Commit the PR was stacked on (overrides parent_pr)."},
                    "verified_head_sha": {"type": "string", "description": "HEAD the existing verdict was given on."},
                    "push": {"type": "boolean", "default": True, "description": "Force-with-lease push the rebased head."},
                    "retarget": {"type": "boolean", "default": True, "description": "Retarget the PR base via gh pr edit."},
                    "dry_run": {"type": "boolean", "default": False, "description": "Rebase and compare only; no push/retarget."},
                    "tenant": {"type": "string", "description": "Workstream tenant for the decision trail."},
                    "card_id": {"type": "string", "description": "Kanban card id for the decision trail."},
                },
                "required": ["repo"],
            },
        },
        handler=handle_restack,
    )

    def handle_log_decision(args: dict[str, Any], **kwargs: Any) -> str:
        try:
            evidence = args.get("evidence")
            if isinstance(evidence, str) and evidence.strip():
                evidence = json.loads(evidence)
            row = log_decision(
                str(args.get("decision") or ""),
                repo_slug=args.get("repo"),
                phase=args.get("phase"),
                why=args.get("why"),
                evidence=evidence,
                result=args.get("result"),
                supersedes_id=args.get("supersedes_id"),
                **_trail_context(args),
            )
            out: dict[str, Any] = {"ok": True, "decision": row}
            if str(row.get("phase") or "").lower() == "retro":
                snapshot = capture_metrics_snapshot(
                    source="retro",
                    repo_slug=row.get("repo_slug") or None,
                    tenant=row.get("tenant") or None,
                    extra={"decision_id": row["id"], "decision": row["decision"]},
                )
                out["metrics_snapshot_id"] = (snapshot or {}).get("id")
            return _json_ok(out)
        except Exception as exc:
            return _json_err(exc)

    ctx.register_tool(
        name="factonio_log_decision",
        toolset=PLUGIN_ID,
        schema={
            "name": "factonio_log_decision",
            "description": (
                "Append a decision to Factonio's append-only trail.db (show-me-your-work): forks, pivots, "
                "Attack the Premise probes, gate countersigns, and corrections. Rows are immutable; to correct one, "
                "log a new row with `supersedes_id` set to the old row's id."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "decision": {"type": "string", "description": "Short decision label, e.g. 'pivot_to_wal' or 'countersign_merge'."},
                    "why": {"type": "string", "description": "Plain-language reason a reviewer can audit."},
                    "phase": {
                        "type": "string",
                        "description": "Phase: define, design, implement, verify, gate, merge, escape, retro, or watchdog.",
                    },
                    "result": {"type": "string", "description": "Outcome, e.g. 'adopted', 'rejected', 'pass', 'blocked'."},
                    "evidence": {
                        "type": "object",
                        "description": "Structured evidence (pr_number, head_sha, patch_id, tests_passed, command, ...).",
                    },
                    "repo": {"type": "string", "description": "Registered repo slug or 'owner/repo'."},
                    "tenant": {"type": "string", "description": "Workstream tenant (defaults to the worker's tenant)."},
                    "card_id": {"type": "string", "description": "Kanban card id (defaults to the worker's card)."},
                    "actor": {"type": "string", "description": "Who decided (defaults to 'worker' inside a card, else 'orchestrator')."},
                    "supersedes_id": {"type": "string", "description": "Id (d_...) of an earlier decision this row corrects."},
                },
                "required": ["decision", "why"],
            },
        },
        handler=handle_log_decision,
    )
