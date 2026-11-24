"""Agent-callable tools registered by Factonio."""

from __future__ import annotations

import json
from typing import Any

from .config import PLUGIN_ID, check_cross_model_warning, list_repos, resolve_all_phase_models
from .orchestrator import (
    spawn_implementation_card,
    spawn_verification_pair,
    start_workstream,
    workstream_status,
)
from .repos import add_repo, inspect_pr_gate, merge_pr


def _json_ok(payload: dict[str, Any]) -> str:
    return json.dumps(payload, default=str)


def _json_err(exc: Exception) -> str:
    return json.dumps({"ok": False, "error": str(exc)})


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
                "linked to `spec.md` without exposing `holdout/`, using the configured Implementation model."
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
                "no holdout) and Black-Box Check (built artifact + holdout, no diff), both using the Verification model."
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
                },
                "required": ["repo", "tenant", "pr_number"],
            },
        },
        handler=handle_spawn_verify,
    )

    def handle_status(args: dict[str, Any], **kwargs: Any) -> str:
        try:
            res = workstream_status(
                tenant=args.get("tenant"),
                session_id=None if args.get("all") else kwargs.get("session_id"),
            )
            res["repos"] = list(list_repos().keys())
            res["models"] = resolve_all_phase_models(repo_slug=args.get("repo"))
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
        try:
            return _json_ok(inspect_pr_gate(str(args["repo"]), int(args["pr_number"])))
        except Exception as exc:
            return _json_err(exc)

    ctx.register_tool(
        name="factonio_pr_gate",
        toolset=PLUGIN_ID,
        schema={
            "name": "factonio_pr_gate",
            "description": (
                "Check a GitHub PR's live HEAD SHA, draft/mergeable status, and CI status checks via `gh`. "
                "Always run before reporting a PR ready or merging."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "repo": {"type": "string", "description": "Registered repo slug or 'owner/repo'."},
                    "pr_number": {"type": "integer", "description": "GitHub pull request number."},
                },
                "required": ["repo", "pr_number"],
            },
        },
        handler=handle_pr_gate,
    )

    def handle_merge_pr(args: dict[str, Any], **kwargs: Any) -> str:
        try:
            return _json_ok(
                merge_pr(
                    str(args["repo"]),
                    int(args["pr_number"]),
                    expected_head_sha=args.get("expected_head_sha"),
                    method=str(args.get("method") or "squash"),
                )
            )
        except Exception as exc:
            return _json_err(exc)

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
                },
                "required": ["repo", "pr_number"],
            },
        },
        handler=handle_merge_pr,
    )
