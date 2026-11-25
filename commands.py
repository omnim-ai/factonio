"""CLI subcommand registration (`hermes factonio ...`)."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .config import (
    check_cross_model_warning,
    list_repos,
    resolve_all_phase_models,
)
from .orchestrator import workstream_status
from .profile_setup import init_factonio_profile, run_doctor
from .repos import (
    RepoNotFoundError,
    add_repo,
    audit_worktrees,
    inspect_pr_gate,
    prune_safe_worktrees,
    remove_repo,
    update_repo,
)


def _print_result(data: dict[str, Any], as_json: bool) -> None:
    if as_json:
        print(json.dumps(data, indent=2, default=str))
        return
    print(json.dumps(data, indent=2, default=str))


def _print_audit(res: dict[str, Any]) -> None:
    for repo in res["repos"]:
        base = repo.get("base_ref") or "?"
        print(f"{repo.get('repo') or repo.get('slug')} ({repo['path']}) base={base}")
        if repo.get("error"):
            print(f"  Error: {repo['error']}")
            continue
        if repo.get("pr_error"):
            print(f"  Note: PR lookup skipped ({repo['pr_error']})")
        if not repo["worktrees"]:
            print("  (no linked worktrees)")
        for wt in repo["worktrees"]:
            age = "?" if wt.get("age_days") is None else f"{wt['age_days']}d"
            card = wt.get("active_card") or "-"
            print(
                f"  {wt['bucket']:<16} {wt['size_human']:>10} {age:>7}  {wt.get('branch') or '(detached)'}"
                f"  merged={wt['merged']} dirty={wt['dirty']} remote={wt['remote']} pr={wt['pr']} card={card}"
            )
            print(f"    {wt['path']}" + (f"  [{wt['hold_reason']}]" if wt.get("hold_reason") else ""))
    s = res["summary"]
    counts = " ".join(f"{k}={v}" for k, v in s["by_bucket"].items())
    print(f"\n{s['count']} worktrees, {s['total_human']} total; {counts}")
    if s["by_bucket"].get("safe"):
        print(f"Reclaimable with --prune-safe: {s['safe_human']}")


def _print_prune(res: dict[str, Any]) -> None:
    for item in res["removed"]:
        print(f"Removed {item['path']} ({item['size_human']}, {item['method']})")
        dropped = item.get("dropped_scratch") or []
        if dropped:
            print(f"        dropped untracked scratch: {', '.join(dropped)}")
    for item in res["held"]:
        print(f"Held    {item['path']} [{item['bucket']}: {item['hold_reason']}]")
    for item in res["errors"]:
        print(f"Error   {item['path']}: {item['error']}")
    print(f"\nReclaimed {res['reclaimed_human']} from {len(res['removed'])} worktree(s); held {len(res['held'])}.")


def register_cli(ctx: Any, plugin_dir: Path) -> None:
    """Register `hermes factonio <subcommand>`."""

    def setup_parser(parser: Any) -> None:
        parser.add_argument("--json", action="store_true", help="Output machine-readable JSON.")
        sub = parser.add_subparsers(dest="factonio_cmd")

        p_init = sub.add_parser("init", help="Create or update the dedicated headless `factonio` worker profile.")
        p_init.add_argument("--apply-config", action="store_true", help="Apply recommended root kanban config keys.")
        p_init.add_argument("--force", action="store_true", help="Overwrite the factonio worker SOUL.md.")
        p_init.add_argument("--workers", type=int, default=None, help="Set max concurrent workers (default: 4).")

        p_add = sub.add_parser("add", help="Register a GitHub repository (`owner/repo`) with Factonio.")
        p_add.add_argument("repo", help="GitHub repository ('owner/repo' or URL).")
        p_add.add_argument("--path", dest="local_path", default=None, help="Local checkout path override.")
        p_add.add_argument("--merge-policy", choices=["auto", "ask"], default=None, help="PR merge policy.")
        p_add.add_argument("--max-fix-rounds", type=int, default=None, help="Max verification fix rounds per PR.")
        p_add.add_argument("--no-clone", action="store_true", help="Do not clone if local directory is absent.")

        p_upd = sub.add_parser(
            "update",
            aliases=["edit"],
            help="Update configuration parameters for a registered repository.",
        )
        p_upd.add_argument("repo", help="Repo slug or 'owner/repo'.")
        p_upd.add_argument("--merge-policy", choices=["auto", "ask"], default=None, help="PR merge policy.")
        p_upd.add_argument("--max-fix-rounds", type=int, default=None, help="Max verification fix rounds per PR (>= 1).")
        p_upd.add_argument("--default-branch", default=None, help="Base branch for PRs and worktrees.")
        p_upd.add_argument(
            "--design-provider",
            choices=["auto", "claude-design", "hermes", "off", "inherit"],
            default=None,
            help="Phase 1b design provider override ('inherit' clears it to use the global default).",
        )
        p_upd.add_argument(
            "--design-approval",
            choices=["ask", "auto", "inherit"],
            default=None,
            help="Design approval override ('inherit' clears it to use the global default).",
        )
        p_upd.add_argument(
            "--json",
            action="store_true",
            default=argparse.SUPPRESS,
            help="Output machine-readable JSON.",
        )

        p_rm = sub.add_parser("remove", help="Unregister a repository from Factonio (keeps local files).")
        p_rm.add_argument("repo", help="Repo slug or 'owner/repo'.")

        sub.add_parser("list", help="List registered Factonio repositories.")

        p_models = sub.add_parser("models", help="Show resolved provider/model per Factonio phase.")
        p_models.add_argument("--repo", default=None, help="Optional repo slug to include per-repo overrides.")

        p_status = sub.add_parser("status", help="Show active Factonio Kanban workstreams and cards.")
        p_status.add_argument("--tenant", default=None, help="Filter by workstream tenant.")

        p_pr = sub.add_parser("pr-gate", help="Inspect a GitHub PR's live HEAD and CI check status.")
        p_pr.add_argument("repo", help="Repo slug or 'owner/repo'.")
        p_pr.add_argument("pr_number", type=int, help="Pull request number.")

        p_wt = sub.add_parser(
            "worktrees",
            help="Audit git worktrees into safety buckets (read-only unless --prune-safe).",
        )
        p_wt.add_argument("repo", nargs="?", default=None, help="Repo slug, 'owner/repo', or local path (default: all registered).")
        p_wt.add_argument("--base", default=None, help="Base ref for merged detection (default: origin/<default_branch>).")
        p_wt.add_argument(
            "--prune-safe",
            action="store_true",
            help="Remove ONLY worktrees in the 'safe' bucket; held/review worktrees are never touched.",
        )
        p_wt.add_argument(
            "--json",
            action="store_true",
            default=argparse.SUPPRESS,
            help="Output machine-readable JSON.",
        )

        sub.add_parser("doctor", help="Verify git, gh auth, worker profile, Kanban config, and phase models.")

    def handle_cli(args: Any) -> None:
        cmd = getattr(args, "factonio_cmd", None)
        as_json = bool(getattr(args, "json", False))

        if not cmd:
            print("Usage: hermes factonio {init|add|update|remove|list|models|status|pr-gate|worktrees|doctor} [--json]")
            return

        if cmd == "init":
            res = init_factonio_profile(
                plugin_dir,
                apply_root_config=bool(args.apply_config),
                force_soul=bool(args.force),
                max_concurrent_workers=args.workers,
            )
            if as_json:
                print(json.dumps(res, indent=2, default=str))
                return
            state = "Created" if res["created"] else "Updated"
            print(f"{state} dedicated profile '{res['profile']}' at {res['profile_dir']}")
            print(f"  Plugin link: {res['plugin_link']['action']} -> {res['plugin_link']['target']}")
            if res["worker_config"]["changed"]:
                print(f"  Worker config updated: {', '.join(res['worker_config']['changed'])}")
            pending = res["root_config"]["pending"]
            if pending and not res["root_config"]["applied"]:
                print("  Recommended root config.yaml updates (re-run with --apply-config to apply):")
                for k, v in pending.items():
                    print(f"    {k}: {v['current']!r} -> {v['recommended']!r}")
            elif res["root_config"]["applied"]:
                print(f"  Applied root config: {', '.join(res['root_config']['applied'])}")
            return

        if cmd == "add":
            res = add_repo(
                args.repo,
                local_path=args.local_path,
                merge_policy=args.merge_policy,
                max_fix_rounds=args.max_fix_rounds,
                clone=not bool(args.no_clone),
            )
            if as_json:
                print(json.dumps(res, indent=2, default=str))
                return
            r = res["repo"]
            print(f"Registered {r['repo']} (slug: {r['slug']})")
            print(f"  Path: {r['path']} (project: {r['project_slug']})")
            print(f"  Default branch: {r['default_branch']} | Required checks: {len(r['required_checks'])}")
            print(f"  Merge policy: {r['merge_policy']} | Max fix rounds: {r['max_fix_rounds']}")
            return

        if cmd in ("update", "edit"):
            flag_map = {
                "merge_policy": getattr(args, "merge_policy", None),
                "max_fix_rounds": getattr(args, "max_fix_rounds", None),
                "default_branch": getattr(args, "default_branch", None),
                "design_provider": getattr(args, "design_provider", None),
                "design_approval": getattr(args, "design_approval", None),
            }
            kwargs = {k: v for k, v in flag_map.items() if v is not None}
            try:
                res = update_repo(args.repo, **kwargs)
            except (RepoNotFoundError, ValueError) as exc:
                if as_json:
                    print(json.dumps({"ok": False, "error": str(exc)}, indent=2))
                else:
                    print(f"Error: {exc}", file=sys.stderr)
                sys.exit(1)
            if as_json:
                print(json.dumps(res, indent=2, default=str))
                return
            r = res["repo"]
            print(f"Updated {r.get('repo')} (slug: {r['slug']}): {', '.join(res['updated_fields'])}")
            print(
                f"  Branch: {r.get('default_branch', 'main')} | Merge policy: {r.get('merge_policy', 'auto')}"
                f" | Max fix rounds: {r.get('max_fix_rounds', 3)}"
            )
            print(
                f"  Design provider: {r.get('design_provider') or 'inherit'}"
                f" | Design approval: {r.get('design_approval') or 'inherit'}"
            )
            return

        if cmd == "remove":
            res = remove_repo(args.repo)
            if as_json:
                print(json.dumps(res, indent=2, default=str))
            else:
                print(f"Removed {res['removed']}" if res.get("ok") else res.get("error"))
            if not res.get("ok"):
                sys.exit(1)
            return

        if cmd == "list":
            repos = list_repos()
            if as_json:
                print(json.dumps({"repos": repos}, indent=2, default=str))
                return
            if not repos:
                print("No repositories registered. Run: hermes factonio add owner/repo")
                return
            for slug, r in sorted(repos.items()):
                tags = [
                    f"branch={r.get('default_branch', 'main')}",
                    f"merge={r.get('merge_policy', 'auto')}",
                    f"fix_rounds={r.get('max_fix_rounds', 3)}",
                ]
                if r.get("design_provider"):
                    tags.append(f"design={r['design_provider']}")
                if r.get("design_approval"):
                    tags.append(f"design_approval={r['design_approval']}")
                print(f"- {slug}: {r.get('repo')} ({r.get('path')}) [{' '.join(tags)}]")
            print("\nEdit settings with: hermes factonio update <repo> --merge-policy ... (see --help)")
            return

        if cmd == "models":
            models = resolve_all_phase_models(repo_slug=args.repo)
            warn = check_cross_model_warning(repo_slug=args.repo)
            if as_json:
                print(json.dumps({"models": models, "warning": warn}, indent=2, default=str))
                return
            for phase, info in models.items():
                label = f"{info['provider']}/{info['model']}" if info.get("provider") else (info.get("model") or "(unset)")
                print(f"  {phase:<10} -> {label:<36} ({info['source']})")
            if warn:
                print(f"\nWarning: {warn}")
            print("\nConfigure per-phase models in `hermes model` -> Auxiliary Models (factonio_*).")
            return

        if cmd == "status":
            res = workstream_status(tenant=args.tenant)
            if as_json:
                print(json.dumps(res, indent=2, default=str))
                return
            c = res["counts"]
            print(f"Factonio cards: running={c['running']} ready={c['ready']} blocked={c['blocked']} triage={c['triage']} done={c['done']}")
            for card in res["cards"][:20]:
                pr = f" [{card['pr']['repo']}#{card['pr']['number']}]" if card.get("pr") else ""
                print(f"  {card['id']} | {card['status']:<7} | {card['phase']:<15} | {card['title']}{pr}")
            return

        if cmd == "pr-gate":
            res = inspect_pr_gate(args.repo, args.pr_number)
            if as_json:
                print(json.dumps(res, indent=2, default=str))
            else:
                if not res.get("ok"):
                    print(f"Error: {res.get('error')}")
                    sys.exit(1)
                if res["ready_to_merge"]:
                    verdict = "READY"
                else:
                    tier = res.get("blocker_tier")
                    verdict = f"BLOCKED (tier {tier})" if tier else "BLOCKED"
                print(f"{verdict}: {res['repo']}#{res['pr_number']} ({res['state']}, head={str(res['head_sha'])[:10]})")
                print(f"  Checks: passed={len(res['checks']['passed'])} failed={len(res['checks']['failed'])} pending={len(res['checks']['pending'])}")
                print(
                    f"  Review: decision={res.get('review_decision') or 'NONE'} "
                    f"unresolved_threads={len(res.get('unresolved_threads') or [])}"
                )
                for b in res["blockers"]:
                    print(f"  Blocker: {b}")
            if not res.get("ready_to_merge"):
                sys.exit(1)
            return

        if cmd == "worktrees":
            try:
                if getattr(args, "prune_safe", False):
                    res = prune_safe_worktrees(args.repo, base_ref=args.base)
                else:
                    res = audit_worktrees(args.repo, base_ref=args.base)
            except (RepoNotFoundError, ValueError) as exc:
                if as_json:
                    print(json.dumps({"ok": False, "error": str(exc)}, indent=2))
                else:
                    print(f"Error: {exc}", file=sys.stderr)
                sys.exit(1)
            if as_json:
                print(json.dumps(res, indent=2, default=str))
            elif getattr(args, "prune_safe", False):
                _print_prune(res)
            else:
                _print_audit(res)
            if not res.get("ok"):
                sys.exit(1)
            return

        if cmd == "doctor":
            res = run_doctor(plugin_dir)
            if as_json:
                print(json.dumps(res, indent=2, default=str))
            else:
                for chk in res["checks"]:
                    mark = "OK  " if chk["ok"] else "FAIL"
                    print(f"  [{mark}] {chk['name']}: {chk['detail']}")
                if res["warnings"]:
                    print("\nWarnings:")
                    for w in res["warnings"]:
                        print(f"  - {w}")
            if not res["ok"]:
                sys.exit(1)

    ctx.register_cli_command(
        "factonio",
        help="Manage the Factonio pure-Hermes software factory (init, add, update, models, status, worktrees, doctor).",
        setup_fn=setup_parser,
        handler_fn=handle_cli,
    )
