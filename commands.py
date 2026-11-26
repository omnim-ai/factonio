"""CLI subcommand registration (`hermes factonio ...`)."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

from .config import (
    check_cross_model_warning,
    list_repos,
    resolve_all_phase_models,
    resolve_rigor,
    resolve_verify_panel,
)
from .harness import audit_harness, bootstrap_verify_harness, check_harness, show_harness
from .metrics import RANGE_SECONDS, capture_metrics_snapshot, compute_metrics, default_kanban_db_path
from .orchestrator import WORKSTREAM_KINDS, evaluate_verification_panel, start_workstream, workstream_status
from .profile_setup import init_factonio_profile, run_doctor
from .repos import (
    RepoNotFoundError,
    add_repo,
    audit_worktrees,
    inspect_pr_gate,
    inspect_stack_gate,
    prune_safe_worktrees,
    remove_repo,
    restack_pr,
    restack_stack,
    update_repo,
)
from .trail import (
    audit_trail_evidence,
    render_trail_audit_markdown,
    render_trail_audit_text,
    metrics_history_payload,
    render_metric_snapshots_text,
    render_trail_markdown,
    render_trail_text,
    trail_payload,
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


def _print_harness(action: str, res: dict[str, Any]) -> None:
    if action == "init":
        if res.get("refused"):
            print(f"Refused: {res['error']}")
            return
        proof = res.get("proof") or {}
        m = res["manifest"]
        if proof.get("ran") and not proof.get("ok"):
            restored = "" if res.get("dry_run") else "; restored the prior .factonio/ state"
            print(f"Proof FAILED: nothing written for {res['repo']}{restored}")
        else:
            verb = "Would write" if res.get("dry_run") else ("Overwrote" if res.get("overwritten") else "Wrote")
            print(f"{verb} {res['verify_md']} and {res['harness_json']}")
        print(
            f"  Stack: {', '.join(m['stack']) or '-'} | features={len(m['feature_map'])} hooks={len(m['hooks'])}"
            f" routes={len(m['discovered_surfaces']['routes'])} cli={len(m['discovered_surfaces']['cli_commands'])}"
        )
        if proof.get("ran"):
            doctor, hooks = proof.get("doctor") or {}, proof.get("hooks") or {}
            print(
                f"  Proof: {'OK' if proof.get('ok') else 'FAILED'} | doctor={doctor.get('ran', 0)} "
                f"hooks={hooks.get('passed', 0)}/{hooks.get('ran', 0)} evidence_survived={proof.get('evidence_survived')}"
            )
            for err in proof.get("errors") or []:
                print(f"  Error: {err}")
        if res.get("dry_run"):
            print("\n" + res["verify_md_content"])
        return
    if action == "show":
        if not res.get("ok"):
            print(f"Error: {res.get('error')}")
            return
        print(res.get("verify_md_content") or "")
        if res.get("harness_json"):
            print(f"(manifest: {res['harness_json']}, schema_version={(res.get('manifest') or {}).get('schema_version')})")
        return
    if action == "check":
        print(f"{'OK' if res['ok'] else 'FAIL'}: {res['repo']} harness ({res['mode']})")
        for err in res["errors"]:
            print(f"  Error: {err}")
        for warn in res["warnings"]:
            print(f"  Warning: {warn}")
        for label, block in (("Doctor", res.get("doctor")), ("Hooks", res.get("hook_results"))):
            for r in (block or {}).get("results", []):
                name = r.get("name") or r["command"]
                print(f"  {label}: [{'OK  ' if r['ok'] else 'FAIL'}] {name} (exit {r['exit_code']})")
        return
    print(f"{res['severity'].upper()}: {res['repo']} harness drift (base={str(res.get('base_commit') or '-')[:12]})")
    for key in (
        "uncovered_changed_files", "stale_features", "orphaned_globs",
        "new_routes", "removed_routes", "new_cli_commands", "removed_cli_commands",
    ):
        if res.get(key):
            print(f"  {key}: {', '.join(res[key])}")
    classification = (res.get("drift_classification") or {}).get("classification")
    if classification and classification != "clean":
        print(f"  classification: {classification}")
    for warn in res.get("warnings") or []:
        print(f"  Warning: {warn}")
    for rec in res.get("recommendations") or []:
        print(f"  -> {rec}")


def _print_panel(res: dict[str, Any]) -> None:
    pr = res.get("pr") or {}
    pr_label = f" {pr.get('repo')}#{pr.get('number')}" if pr else ""
    merge = "merge allowed" if res.get("merge_allowed") else "merge blocked"
    print(f"{str(res['consensus']).upper()}: panel {res['panel_id']}{pr_label} ({merge})")
    print(f"  Vendors: {', '.join(res.get('vendors') or []) or '-'} | {res.get('reason')}")
    for lane in res.get("lanes") or []:
        verdict = lane.get("verdict") or "-"
        print(f"  {lane.get('task_id')} | {lane.get('status'):<8} | {lane.get('phase'):<15} | {lane.get('vendor')} ({lane.get('model')}) -> {verdict}")
    if res.get("disagreement_summary"):
        print(f"  Disagreement: {res['disagreement_summary']}")
    for finding in (res.get("findings") or [])[:20]:
        print(f"  Finding [{finding['lane']}]: {finding['item']}")
    if res.get("recorded"):
        print(f"  Recorded verify_panel_consensus {res.get('decision_id')} in trail.db")


def _rate(value: Any) -> str:
    return "-" if value is None else f"{float(value) * 100:.1f}%"


def _print_metrics(m: dict[str, Any]) -> None:
    t, v, rw, esc = m["totals"], m["verification"], m["rework"], m["escapes"]
    print(f"Factonio metrics (range={m['range']}" + (f", tenant={m['tenant']}" if m.get("tenant") else "") + ")")
    print(
        f"  Cards: total={t['cards']} done={t['done']} running={t['running']} blocked={t['blocked']}"
        f" triage={t['triage']} | workstreams={t['workstreams']}"
    )
    for label, key in (("Diff Check", "diff"), ("Black-Box Check", "blackbox")):
        b = v[key]
        print(f"  {label}: pass={b['pass']} reject={b['reject']} inconclusive={b['inconclusive']} pass_rate={_rate(b['pass_rate'])}")
    print(
        f"  Rework: cards_with_rework={rw['cards_with_rework']} avg_runs={rw['avg_runs_per_implement_card'] or '-'}"
        f" first_pass_rate={_rate(rw.get('first_pass_rate'))}"
    )
    print(f"  Escapes: {esc['count']}/{esc['verified_total']} escape_rate={_rate(esc['escape_rate'])}")
    for phase, info in m["phases"].items():
        med = "-" if info["median_seconds"] is None else f"{info['median_seconds']:.0f}s"
        print(f"  Phase {phase:<15} count={info['count']} done={info['done']} median={med}")
    vp = m.get("verification_panels") or {}
    if vp:
        print(
            f"  Panels: total={vp['total']} pass={vp['pass']} reject={vp['reject']} inconclusive={vp['inconclusive']}"
            f" pending={vp['pending']} disagreements={vp['disagreements']}"
        )
    h = m.get("history") or {}
    print(f"  History: {h.get('snapshot_count', 0)} snapshot(s), latest={h.get('latest_ts') or '-'}")


def _print_history(payload: dict[str, Any]) -> None:
    s = payload["summary"]
    by_source = " ".join(f"{k}={v}" for k, v in s["by_source"].items()) or "-"
    print(f"Metric snapshots: total={s['total']} (showing {len(payload['snapshots'])}) sources: {by_source}")
    print(render_metric_snapshots_text(payload["snapshots"]))


def _print_verify_panel(panel: dict[str, Any], rigor: Any) -> None:
    print(f"\n  default rigor -> {rigor or '(invalid)'}")
    if panel["entries"]:
        rendered = ", ".join(f"{e['label']} [{e.get('vendor') or 'unknown'}]" for e in panel["entries"])
        state = "valid" if panel["valid"] else "INVALID"
        print(f"  verify panel  -> {rendered} ({panel['source']}, {state})")
    else:
        print("  verify panel  -> (unset)")


def _print_restack_one(r: dict[str, Any]) -> None:
    if r.get("conflict"):
        verdict = "CONFLICT"
    elif not r.get("ok"):
        verdict = "ERROR"
    else:
        verdict = "CONTINUOUS" if r.get("verdict_continuous") else "REVERIFY"
    old = str(r.get("old_head_sha") or "?")[:10]
    new = str(r.get("new_head_sha") or "-")[:10]
    print(
        f"{verdict}: {r.get('repo')}#{r.get('pr_number')} {r.get('old_base_branch')} -> {r.get('target_base')}"
        f" head {old} -> {new} ({r.get('rebase_mode') or 'n/a'}, old base via {r.get('old_base_source') or 'n/a'})"
    )
    print(f"  patch-id before={r.get('before_patch_id')} after={r.get('after_patch_id')}")
    print(f"  pushed={r.get('pushed')} retargeted={r.get('retargeted')} dry_run={r.get('dry_run')}")
    if r.get("conflict_files"):
        print(f"  Conflicting files: {', '.join(r['conflict_files'])}")
    if r.get("error"):
        print(f"  Error: {r['error']}")


def _metrics_fail(message: str, as_json: bool) -> None:
    if as_json:
        print(json.dumps({"ok": False, "error": message}, indent=2))
    else:
        print(f"Error: {message}", file=sys.stderr)
    sys.exit(1)


def _handle_metrics(args: Any, as_json: bool) -> None:
    """`hermes factonio metrics`: live KPIs, `--snapshot` capture, and/or `--history` time series."""
    if not 1 <= int(args.limit) <= 1000:
        _metrics_fail("--limit must be between 1 and 1000", as_json)
    snapshot = None
    if args.snapshot:
        snapshot = capture_metrics_snapshot(
            source="manual",
            range_key=args.range_key or "all",
            repo_slug=args.repo,
            tenant=args.tenant,
            extra={"trigger": "cli"},
            lock_budget_s=None,
        )
        if snapshot is None:
            _metrics_fail("could not capture a metrics snapshot (trail.db unavailable; see logs)", as_json)
    if args.history:
        try:
            payload: dict[str, Any] = metrics_history_payload(
                tenant=args.tenant,
                repo=args.repo,
                source=args.source,
                since=args.since if args.since is not None else args.range_key,
                limit=args.limit,
            )
        except ValueError as exc:
            _metrics_fail(str(exc), as_json)
            return
        except (sqlite3.Error, OSError) as exc:
            _metrics_fail(f"trail.db metric history unavailable: {exc}", as_json)
            return
        if snapshot is not None:
            payload = {"snapshot": snapshot, **payload}
        if as_json:
            print(json.dumps(payload, indent=2, default=str))
        else:
            if snapshot is not None:
                print(f"Captured metric snapshot {snapshot['id']} at {snapshot['ts']}")
            _print_history(payload)
        return
    if snapshot is not None:
        if as_json:
            print(json.dumps({"ok": True, "snapshot": snapshot}, indent=2, default=str))
        else:
            print(f"Captured metric snapshot {snapshot['id']} at {snapshot['ts']} (source=manual, range={snapshot['range_key']})")
            print(render_metric_snapshots_text([snapshot]))
        return
    metrics = compute_metrics(default_kanban_db_path(), range_key=args.range_key or "30d", tenant=args.tenant)
    if as_json:
        print(json.dumps(metrics, indent=2, default=str))
    else:
        _print_metrics(metrics)


def _print_stack_gate(stack: dict) -> None:
    if not stack.get("ok"):
        print(f"Error: {stack.get('error')}")
        return
    verdict = "READY" if stack.get("ready_to_merge") else "BLOCKED"
    tier = stack.get("highest_blocker_tier")
    if tier and not stack.get("ready_to_merge"):
        verdict = f"BLOCKED (tier {tier})"
    order = " -> ".join(f"#{n}" for n in stack.get("order") or [])
    print(f"{verdict}: {stack.get('repo')} stack {order}")
    run = ", ".join(f"#{n}" for n in stack.get("contiguous_ready_run") or []) or "none"
    ceiling = (stack.get("ceiling") or {}).get("pr_number")
    frontier = (stack.get("lowest_pending_frontier") or {}).get("pr_number")
    print(f"  Ready run: {run}; ceiling: {f'#{ceiling}' if ceiling else 'none'}; pending frontier: "
          f"{f'#{frontier}' if frontier else 'none'}")
    for warning in stack.get("warnings") or []:
        print(f"  Warning: {warning}")
    for key, entries in (stack.get("tiers_summary") or {}).items():
        for entry in entries:
            print(f"  {key} #{entry['pr_number']}: {'; '.join(entry['blockers'])}")
    action = stack.get("next_action") or {}
    target = f" on #{action['pr_number']}" if action.get("pr_number") else ""
    print(f"  Next action: {action.get('action')}{target}")


def register_cli(ctx: Any, plugin_dir: Path) -> None:
    """Register `hermes factonio <subcommand>`."""

    def setup_parser(parser: Any) -> None:
        parser.add_argument("--json", action="store_true", help="Output machine-readable JSON.")
        sub = parser.add_subparsers(dest="factonio_cmd")

        p_init = sub.add_parser("init", help="Create or update the dedicated headless `factonio` worker profile.")
        p_init.add_argument("--apply-config", action="store_true", help="Apply recommended root kanban config keys.")
        p_init.add_argument("--force", action="store_true", help="Overwrite the factonio worker SOUL.md.")
        p_init.add_argument("--workers", type=int, default=None, help="Set max concurrent workers (default: 4).")
        p_init.add_argument(
            "--force-link",
            action="store_true",
            help="Point the worker plugins/factonio link at this checkout even if it is an ephemeral worktree/workspace.",
        )

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
            "--default-rigor",
            choices=["standard", "high", "inherit"],
            default=None,
            help="Verification rigor override; 'high' runs a multi-vendor verify panel ('inherit' clears it).",
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

        p_panel = sub.add_parser(
            "panel",
            help="Evaluate a high-rigor verification panel's fail-closed consensus (read-only Kanban).",
        )
        p_panel.add_argument("panel_id", nargs="?", default=None, help="Panel id (vp_...); default: latest match.")
        p_panel.add_argument("--tenant", default=None, help="Workstream tenant (latest panel when panel_id is omitted).")
        p_panel.add_argument("--pr", dest="pr_number", type=int, default=None, help="PR number filter.")
        p_panel.add_argument(
            "--record",
            action="store_true",
            help="Record a terminal consensus as verify_panel_consensus in trail.db.",
        )
        p_panel.add_argument(
            "--json",
            action="store_true",
            default=argparse.SUPPRESS,
            help="Output machine-readable JSON.",
        )

        p_start = sub.add_parser(
            "start",
            help="Start a workstream (feature, bug, quick, refactor, perf, or read-only investigate).",
        )
        p_start.add_argument("repo", help="Registered repo slug or 'owner/repo'.")
        p_start.add_argument("request", help="Full problem statement, goal, or issue context.")
        p_start.add_argument("--kind", choices=list(WORKSTREAM_KINDS), default="feature", help="Workstream mode.")
        p_start.add_argument("--title", default=None, help="Short workstream title.")
        p_start.add_argument("--issue", dest="issue_number", type=int, default=None, help="GitHub issue number.")
        p_start.add_argument(
            "--skip-define",
            action="store_true",
            help="Skip Definition and open an Implementation card (rejected for --kind investigate).",
        )
        p_start.add_argument(
            "--escaped-from", default=None, help="Verification card id the regression escaped from."
        )
        p_start.add_argument(
            "--json",
            action="store_true",
            default=argparse.SUPPRESS,
            help="Output machine-readable JSON.",
        )

        p_status = sub.add_parser("status", help="Show active Factonio Kanban workstreams and cards.")
        p_status.add_argument("--tenant", default=None, help="Filter by workstream tenant.")

        p_pr = sub.add_parser("pr-gate", help="Inspect a GitHub PR's live HEAD and CI check status.")
        p_pr.add_argument("repo", help="Repo slug or 'owner/repo'.")
        p_pr.add_argument("pr_number", type=int, nargs="?", default=None, help="Pull request number (or --stack).")
        p_pr.add_argument(
            "--stack",
            default=None,
            help="Comma-separated stacked PR numbers (e.g. 12,13,14) gated bottom-to-top in tier-major order.",
        )
        p_pr.add_argument("--verified-sha", default=None, help="HEAD the verdicts were given on (patch continuity).")
        p_pr.add_argument("--fetch", action="store_true", help="Fetch base/head into the registered checkout first.")

        p_rs = sub.add_parser(
            "restack",
            help="Rebase a stacked PR (or every child of a parent) onto its new base with patch-id continuity.",
        )
        p_rs.add_argument("repo", help="Repo slug or 'owner/repo'.")
        p_rs.add_argument("pr_number", type=int, nargs="?", default=None, help="Child PR (omit to restack all children).")
        p_rs.add_argument("--parent-pr", type=int, default=None, help="Parent PR whose head was the old base.")
        p_rs.add_argument("--from-branch", default=None, help="Restack all open PRs targeting this branch.")
        p_rs.add_argument("--new-base", default=None, help="New base branch (default: parent's base or repo default).")
        p_rs.add_argument("--old-base-sha", default=None, help="Commit the PR was stacked on (overrides --parent-pr).")
        p_rs.add_argument("--verified-sha", default=None, help="HEAD the existing verdict was given on.")
        p_rs.add_argument("--no-push", action="store_true", help="Do not push the rebased head.")
        p_rs.add_argument("--no-retarget", action="store_true", help="Do not retarget the PR base via gh pr edit.")
        p_rs.add_argument("--dry-run", action="store_true", help="Rebase and compare patch-ids only; no push/retarget.")
        p_rs.add_argument(
            "--json",
            action="store_true",
            default=argparse.SUPPRESS,
            help="Output machine-readable JSON.",
        )

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

        p_trail = sub.add_parser("trail", help="Show the append-only Factonio decision trail (trail.db).")
        p_trail.add_argument("--tenant", default=None, help="Filter by workstream tenant.")
        p_trail.add_argument("--repo", default=None, help="Filter by repo slug or 'owner/repo'.")
        p_trail.add_argument("--phase", default=None, help="Filter by phase (define, design, implement, verify, gate, merge, escape, ...).")
        p_trail.add_argument("--card", dest="card_id", default=None, help="Filter by Kanban card id.")
        p_trail.add_argument("--limit", type=int, default=50, help="Max decisions to show, newest first (default: 50).")
        p_trail.add_argument("--markdown", action="store_true", help="Render a Markdown table (for PRs, retros, handoffs).")
        p_trail.add_argument(
            "--audit",
            action="store_true",
            help="Read-only pre-handoff evidence audit over every matching active decision "
            "(exit 0 when clean, 1 when findings exist).",
        )
        p_trail.add_argument(
            "--json",
            action="store_true",
            default=argparse.SUPPRESS,
            help="Output the {decisions, summary, filters} JSON contract.",
        )

        p_metrics = sub.add_parser(
            "metrics",
            help="Show factory KPIs, capture a long-term trail.db snapshot, or show snapshot history.",
        )
        p_metrics.add_argument(
            "--range", dest="range_key", choices=list(RANGE_SECONDS), default=None,
            help="KPI window (default 30d; --snapshot defaults to all; with --history it is the --since fallback).",
        )
        p_metrics.add_argument("--tenant", default=None, help="Filter KPIs/history by tenant; tags a --snapshot.")
        p_metrics.add_argument("--repo", default=None, help="Filter history by repo slug or 'owner/repo'; tags a --snapshot.")
        p_metrics.add_argument("--history", action="store_true", help="Show long-term metric snapshots (oldest first).")
        p_metrics.add_argument("--snapshot", action="store_true", help="Capture a factory-wide snapshot into trail.db now.")
        p_metrics.add_argument("--source", default=None, help="history: filter by source (merge, panel_consensus, retro, manual).")
        p_metrics.add_argument("--since", default=None, help="history: 7d, 30d, 90d, all, or an ISO-8601 timestamp.")
        p_metrics.add_argument("--limit", type=int, default=100, help="history: latest N snapshots, 1..1000 (default: 100).")
        p_metrics.add_argument(
            "--json",
            action="store_true",
            default=argparse.SUPPRESS,
            help="Output machine-readable JSON.",
        )

        p_h = sub.add_parser(
            "harness",
            help="Bootstrap, validate, show, or drift-audit a repo's .factonio/ verification harness.",
        )
        p_h.add_argument("action", choices=["init", "check", "show", "audit"], help="Harness action.")
        p_h.add_argument("repo", help="Repo slug, 'owner/repo', or local checkout path.")
        p_h.add_argument("--force", action="store_true", help="init: overwrite an existing harness (hooks are kept).")
        p_h.add_argument("--dry-run", action="store_true", help="init: print the generated harness without writing.")
        p_h.add_argument(
            "--base",
            dest="base_ref",
            default=None,
            help="audit: git ref to compare against (default: harness base_commit); "
            "check: git ref whose committed harness gates must not be loosened (default: HEAD).",
        )
        p_h.add_argument(
            "--prove",
            action="store_true",
            help="init: run Doctor, required hooks and an evidence-survival probe; fail closed and roll back on failure.",
        )
        p_h.add_argument("--run-doctor", action="store_true", help="check: also run the Doctor commands.")
        p_h.add_argument("--run-hooks", action="store_true", help="check: also run declared hooks.")
        p_h.add_argument(
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
            print(
                "Usage: hermes factonio {init|add|update|remove|list|models|start|status|panel|pr-gate|restack|worktrees"
                "|harness|trail|metrics|doctor} [--json]"
            )
            return

        if cmd == "init":
            res = init_factonio_profile(
                plugin_dir,
                apply_root_config=bool(args.apply_config),
                force_soul=bool(args.force),
                max_concurrent_workers=args.workers,
                force_plugin_link=bool(args.force_link),
            )
            if as_json:
                print(json.dumps(res, indent=2, default=str))
                return
            state = "Created" if res["created"] else "Updated"
            print(f"{state} dedicated profile '{res['profile']}' at {res['profile_dir']}")
            link = res["plugin_link"]
            print(f"  Plugin link: {link['action']} -> {link['target'] or '(not linked)'}")
            for warning in res.get("warnings") or []:
                print(f"  Warning: {warning}")
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
                "default_rigor": getattr(args, "default_rigor", None),
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
                f" | Default rigor: {r.get('default_rigor') or 'inherit'}"
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
                if r.get("default_rigor"):
                    tags.append(f"rigor={r['default_rigor']}")
                print(f"- {slug}: {r.get('repo')} ({r.get('path')}) [{' '.join(tags)}]")
            print("\nEdit settings with: hermes factonio update <repo> --merge-policy ... (see --help)")
            return

        if cmd == "models":
            models = resolve_all_phase_models(repo_slug=args.repo)
            warn = check_cross_model_warning(repo_slug=args.repo)
            panel = resolve_verify_panel(repo_slug=args.repo)
            try:
                rigor: Any = resolve_rigor(repo_slug=args.repo)
            except ValueError:
                rigor = None
            if as_json:
                print(json.dumps(
                    {"models": models, "warning": warn, "verify_panel": panel, "default_rigor": rigor},
                    indent=2,
                    default=str,
                ))
                return
            for phase, info in models.items():
                label = f"{info['provider']}/{info['model']}" if info.get("provider") else (info.get("model") or "(unset)")
                print(f"  {phase:<10} -> {label:<36} ({info['source']})")
            _print_verify_panel(panel, rigor)
            if rigor == "high" and not panel["valid"]:
                print(f"\nWarning: {panel['warning']}")
            if warn:
                print(f"\nWarning: {warn}")
            print("\nConfigure per-phase models in `hermes model` -> Auxiliary Models (factonio_*).")
            return

        if cmd == "panel":
            try:
                res = evaluate_verification_panel(
                    panel_id=args.panel_id,
                    tenant=args.tenant,
                    pr_number=args.pr_number,
                    record_trail=bool(args.record),
                )
            except ValueError as exc:
                res = {"ok": False, "error": str(exc)}
            if as_json:
                print(json.dumps(res, indent=2, default=str))
            elif not res.get("ok"):
                print(f"Error: {res.get('error')}", file=sys.stderr)
            else:
                _print_panel(res)
            if not res.get("ok"):
                sys.exit(1)
            return

        if cmd == "start":
            try:
                res = start_workstream(
                    args.repo,
                    args.request,
                    kind=args.kind,
                    title=args.title,
                    issue_number=args.issue_number,
                    skip_define=bool(args.skip_define),
                    escaped_from=args.escaped_from,
                )
            except ValueError as exc:
                if as_json:
                    print(json.dumps({"ok": False, "error": str(exc)}, indent=2))
                else:
                    print(f"Error: {exc}", file=sys.stderr)
                sys.exit(1)
            if as_json:
                print(json.dumps(res, indent=2, default=str))
                return
            print(f"Started {res['kind']} workstream {res['tenant']} on {res['repo']}")
            print(f"  Card: {res['task_id']} ({res['phase']}, skill {res['skill']})")
            if res.get("read_only"):
                print(f"  Read-only investigation report: {res['report_path']}")
            elif res["phase"] == "define":
                print(f"  Spec: {res['spec_path']}")
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
            for panel_summary in res.get("panels") or []:
                print(
                    f"  Panel {panel_summary['panel_id']}: {panel_summary['consensus']}"
                    f" ({panel_summary['completed_count']}/{panel_summary['lane_count']} lanes done)"
                )
            return

        if cmd == "pr-gate":
            if getattr(args, "stack", None):
                try:
                    numbers = [int(tok) for tok in str(args.stack).replace(" ", "").split(",") if tok]
                    stack = inspect_stack_gate(args.repo, numbers, fetch=bool(args.fetch))
                except ValueError as exc:
                    stack = {"ok": False, "error": str(exc), "ready_to_merge": False}
                if as_json:
                    print(json.dumps(stack, indent=2, default=str))
                else:
                    _print_stack_gate(stack)
                if not stack.get("ready_to_merge"):
                    sys.exit(1)
                return
            if args.pr_number is None:
                msg = "pass a pr_number or --stack 12,13,14"
                if as_json:
                    print(json.dumps({"ok": False, "error": msg}, indent=2))
                else:
                    print(f"Error: {msg}")
                sys.exit(1)
            res = inspect_pr_gate(
                args.repo, args.pr_number, verified_head_sha=args.verified_sha, fetch=bool(args.fetch),
            )
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
                if res.get("approval_gates"):
                    print(f"  Approval gates: {', '.join(res['approval_gates'])}")
                if res.get("review_automation_running"):
                    print(f"  Review automation running: {', '.join(res.get('pending_review_automations') or [])}")
                if res.get("checks", {}).get("failed") and res.get("had_previous_passing_ci"):
                    print("  Flake signal: an earlier commit on this PR had passing CI (rerun failed jobs once)")
                drift = res.get("trunk_drift")
                if isinstance(drift, dict):
                    print(
                        f"  Trunk drift: policy={drift.get('policy')} rebase_required={bool(drift.get('rebase_required'))}"
                        f" ahead={drift.get('trunk_commits_ahead')}"
                    )
                cont = res.get("patch_continuity")
                if isinstance(cont, dict):
                    print(f"  Patch continuity: continuous={bool(cont.get('continuous'))} ({cont.get('reason') or cont.get('error')})")
                for b in res["blockers"]:
                    print(f"  Blocker: {b}")
            if not res.get("ready_to_merge"):
                sys.exit(1)
            return

        if cmd == "restack":
            common = {
                "new_base": args.new_base,
                "old_base_sha": args.old_base_sha,
                "verified_head_sha": args.verified_sha,
                "push": not args.no_push,
                "retarget": not args.no_retarget,
                "dry_run": bool(args.dry_run),
                "actor": "cli",
            }
            try:
                if args.pr_number is not None:
                    res = restack_pr(args.repo, args.pr_number, parent_pr=args.parent_pr, **common)
                elif args.parent_pr is None and not args.from_branch:
                    raise ValueError("pass a pr_number, --parent-pr, or --from-branch")
                else:
                    res = restack_stack(args.repo, parent_pr=args.parent_pr, from_branch=args.from_branch, **common)
            except (RepoNotFoundError, ValueError) as exc:
                if as_json:
                    print(json.dumps({"ok": False, "error": str(exc)}, indent=2))
                else:
                    print(f"Error: {exc}", file=sys.stderr)
                sys.exit(1)
            if as_json:
                print(json.dumps(res, indent=2, default=str))
            elif "results" in res:
                print(f"Restack children of {res.get('from_branch')} -> {res.get('new_base') or '(default)'}: {len(res['results'])} PR(s)")
                if res.get("error"):
                    print(f"  Error: {res['error']}")
                for item in res["results"]:
                    _print_restack_one(item)
            else:
                _print_restack_one(res)
            if not res.get("ok"):
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

        if cmd == "trail":
            if getattr(args, "audit", False):
                audit = audit_trail_evidence(
                    tenant=args.tenant, repo=args.repo, phase=args.phase, card_id=args.card_id
                )
                if as_json:
                    print(json.dumps(audit, indent=2, default=str))
                elif args.markdown:
                    print(render_trail_audit_markdown(audit), end="")
                else:
                    print(render_trail_audit_text(audit))
                if not audit.get("clean"):
                    sys.exit(1)
                return
            payload = trail_payload(
                tenant=args.tenant,
                repo=args.repo,
                phase=args.phase,
                card_id=args.card_id,
                limit=args.limit,
            )
            if as_json:
                print(json.dumps(payload, indent=2, default=str))
            elif args.markdown:
                print(render_trail_markdown(payload["decisions"]), end="")
            else:
                summary = payload["summary"]
                print(
                    f"Decision trail: total={summary['total']} active={summary['active']} "
                    f"superseded={summary['superseded']} (showing {len(payload['decisions'])})"
                )
                print(render_trail_text(payload["decisions"]))
            return

        if cmd == "metrics":
            _handle_metrics(args, as_json)
            return

        if cmd == "harness":
            action = args.action
            try:
                if action == "init":
                    res = bootstrap_verify_harness(
                        args.repo, force=bool(args.force), dry_run=bool(args.dry_run), prove=bool(args.prove)
                    )
                elif action == "check":
                    res = check_harness(
                        args.repo, run_doctor=bool(args.run_doctor), run_hooks=bool(args.run_hooks), base_ref=args.base_ref
                    )
                elif action == "show":
                    res = show_harness(args.repo)
                else:
                    res = audit_harness(args.repo, base_ref=args.base_ref)
            except ValueError as exc:
                if as_json:
                    print(json.dumps({"ok": False, "error": str(exc)}, indent=2))
                else:
                    print(f"Error: {exc}", file=sys.stderr)
                sys.exit(1)
            if as_json:
                print(json.dumps({"action": action, **res}, indent=2, default=str))
            else:
                _print_harness(action, res)
            failed = not res.get("ok") or (action == "audit" and res.get("drifted"))
            if failed:
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
        help=(
            "Manage the Factonio pure-Hermes software factory "
            "(init, add, update, models, start, status, panel, restack, worktrees, harness, trail, metrics, doctor)."
        ),
        setup_fn=setup_parser,
        handler_fn=handle_cli,
    )
