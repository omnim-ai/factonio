"""CLI subcommand registration (`hermes factonio ...`)."""

from __future__ import annotations

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
from .repos import add_repo, inspect_pr_gate, remove_repo


def _print_result(data: dict[str, Any], as_json: bool) -> None:
    if as_json:
        print(json.dumps(data, indent=2, default=str))
        return
    print(json.dumps(data, indent=2, default=str))


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

        sub.add_parser("doctor", help="Verify git, gh auth, worker profile, Kanban config, and phase models.")

    def handle_cli(args: Any) -> None:
        cmd = getattr(args, "factonio_cmd", None)
        as_json = bool(getattr(args, "json", False))

        if not cmd:
            print("Usage: hermes factonio {init|add|remove|list|models|status|pr-gate|doctor} [--json]")
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
                print(f"- {slug}: {r.get('repo')} ({r.get('path')}) [merge={r.get('merge_policy', 'auto')}]")
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
                verdict = "READY" if res["ready_to_merge"] else "BLOCKED"
                print(f"{verdict}: {res['repo']}#{res['pr_number']} ({res['state']}, head={str(res['head_sha'])[:10]})")
                print(f"  Checks: passed={len(res['checks']['passed'])} failed={len(res['checks']['failed'])} pending={len(res['checks']['pending'])}")
                for b in res["blockers"]:
                    print(f"  Blocker: {b}")
            if not res.get("ready_to_merge"):
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
        help="Manage the Factonio pure-Hermes software factory (init, add, models, status, doctor).",
        setup_fn=setup_parser,
        handler_fn=handle_cli,
    )
