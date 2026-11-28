"""Read-only SQLite metrics and Thread Cards queries over Hermes kanban.db."""

from __future__ import annotations

import logging
import sqlite3
import statistics
import time
from contextlib import closing, nullcontext
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Optional

from .arena import list_arena_summaries
from .config import PLUGIN_ID, WORKER_PROFILE, get_root_hermes_home_path
from .store_kanban import chunked_in_query, open_kanban_ro, resolve_kanban_db_path, table_columns
from .trail import (
    BEST_EFFORT_BUDGET_S,
    best_effort,
    contains_holdout_reference,
    record_metric_snapshot,
    summarize_metric_snapshots,
    summarize_trail,
    workstream_modes_from_trail,
)
from .validators import safe_tenant_dirname
from .verification import (  # public re-exports: verification projections moved out of metrics
    aggregate_panel_verdicts as aggregate_panel_verdicts,
    card_targets as card_targets,
    card_verdict as card_verdict,
    classify_phase as classify_phase,
    expected_panel_lane_ids as expected_panel_lane_ids,
    extract_local_target as extract_local_target,
    load_panel_lanes as load_panel_lanes,
    local_branch_verification_status as local_branch_verification_status,
    local_target_from_metadata as local_target_from_metadata,
    panel_lane,
    parse_arena_tag as parse_arena_tag,
    parse_panel_tag as parse_panel_tag,
    parse_workstream_mode as parse_workstream_mode,
    load_runs_by_task,
    summarize_panel as summarize_panel,
    text_or_none,
)
from .vocabulary import INCONCLUSIVE_VERDICTS, REJECT_VERDICTS, VERIFY_PHASES

logger = logging.getLogger("factonio.metrics")

RANGE_SECONDS: dict[str, Optional[int]] = {
    "7d": 7 * 86400,
    "30d": 30 * 86400,
    "90d": 90 * 86400,
    "all": None,
}


# Legacy private names (imported by older callers and tests).
_card_targets = card_targets
_card_verdict = card_verdict
_extract_local_target = extract_local_target
_local_target_from_metadata = local_target_from_metadata
_panel_lane = panel_lane
_text_or_none = text_or_none
_open_ro = open_kanban_ro
_table_columns = table_columns
_REJECT_VERDICTS = REJECT_VERDICTS
_INCONCLUSIVE_VERDICTS = INCONCLUSIVE_VERDICTS
_VERIFY_PHASES = VERIFY_PHASES

_COUNT_KEYS = ("running", "blocked", "triage", "ready", "done", "todo", "review")
# Run outcomes that are not failures: a clean latest run clears any stale error.
_CLEAN_OUTCOMES = frozenset({"completed", "review_requested", "changes_requested", "scheduled", "approved"})


def _empty_counts() -> dict[str, int]:
    counts = {k: 0 for k in _COUNT_KEYS}
    counts.update({"rejected": 0, "inconclusive": 0, "attention": 0, "total": 0})
    return counts


def _active_duration_seconds(runs: list[dict[str, Any]], now_ts: float) -> Optional[int]:
    """Sum of run durations, including live elapsed time for an in-flight run."""
    total = 0.0
    seen = False
    for run in runs:
        started = run.get("started_at")
        if started is None:
            continue
        try:
            start_f = float(started)
            ended = run.get("ended_at")
            end_f = float(ended) if ended is not None else now_ts
        except (TypeError, ValueError):
            continue
        seen = True
        total += max(0.0, end_f - start_f)
    return int(total) if seen else None


def _card_summary(task: dict[str, Any], status: str, runs: list[dict[str, Any]]) -> Optional[str]:
    for run in reversed(runs):
        text = _text_or_none(run.get("summary"))
        if text:
            return text
    if status not in ("blocked", "triage"):
        return _text_or_none(task.get("result"))
    return None


def _card_last_error(task: dict[str, Any], status: str, runs: list[dict[str, Any]]) -> Optional[str]:
    if runs:
        last = runs[-1]
        if last.get("outcome") in _CLEAN_OUTCOMES:
            return None
        err = _text_or_none(last.get("error"))
        if err:
            return err
    if status != "done":
        return _text_or_none(task.get("last_failure_error"))
    return None


def _report_exists(path: str) -> bool:
    try:
        p = Path(path)
        return p.is_file() and p.stat().st_size > 0
    except (OSError, ValueError):
        return False


def _resolve_workstreams(
    tenants: list[str], card_modes: Mapping[str, dict[str, Any]], *, trail_db: Optional[Path] = None
) -> dict[str, dict[str, Any]]:
    """``{tenant: {kind, read_only, report_path, report_exists}}``; strictly read-only (never mkdir)."""
    trail_modes = workstream_modes_from_trail(tenants, db_path=trail_db)
    runs_root = get_root_hermes_home_path() / "plugin-data" / PLUGIN_ID / "runs"
    out: dict[str, dict[str, Any]] = {}
    for tenant in tenants:
        card = card_modes.get(tenant) or {}
        kind = (trail_modes.get(tenant) or {}).get("kind") or card.get("kind")
        entry: dict[str, Any] = {"kind": kind, "read_only": False, "report_path": None, "report_exists": False}
        if kind == "investigate":
            safe = safe_tenant_dirname(tenant)
            path = card.get("report_path") or str(runs_root / safe / "investigation.md")
            entry["read_only"] = True
            if not contains_holdout_reference(path):
                entry["report_path"] = path
                entry["report_exists"] = _report_exists(path)
        out[tenant] = entry
    return out


def _percentile(values: list[float], pct: float) -> Optional[float]:
    if not values:
        return None
    s = sorted(values)
    idx = min(len(s) - 1, max(0, int(round((pct / 100.0) * (len(s) - 1)))))
    return round(s[idx], 1)


def _trail_metrics(
    *, tenant: Optional[str], cutoff: Optional[float], trail_db: Optional[Path] = None
) -> dict[str, Any]:
    """Additive `trail` block summarizing `trail.db` decisions for the same tenant/range window."""
    since = (
        datetime.fromtimestamp(cutoff, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        if cutoff is not None
        else None
    )
    try:
        summary = summarize_trail(tenant=tenant, since=since, db_path=trail_db)
    except (sqlite3.Error, OSError):
        # An unreadable trail.db must not take down the Kanban KPIs it is appended to.
        summary = {}
    return {
        "decision_count": int(summary.get("total") or 0),
        "active_count": int(summary.get("active") or 0),
        "superseded_count": int(summary.get("superseded") or 0),
        "by_tenant": dict(summary.get("by_tenant") or {}),
        "by_phase": dict(summary.get("by_phase") or {}),
    }


def _history_metrics(*, tenant: Optional[str], trail_db: Optional[Path] = None) -> dict[str, Any]:
    """Additive `history` block: how many long-term `metric_snapshots` exist (all time) for ``tenant``."""
    try:
        summary = summarize_metric_snapshots(tenant=tenant, db_path=trail_db)
    except (sqlite3.Error, OSError, ValueError):
        # A missing or unreadable trail.db must not take down the Kanban KPIs.
        summary = {}
    return {"snapshot_count": int(summary.get("total") or 0), "latest_ts": summary.get("latest_ts")}


def compute_metrics(
    db_path: Path,
    *,
    range_key: str = "30d",
    tenant: Optional[str] = None,
    assignee: str = WORKER_PROFILE,
    now: Optional[float] = None,
    trail_db: Optional[Path] = None,
) -> dict[str, Any]:
    """Compute phase durations, verification pass rates, rework, escape rate, trail and history counts."""
    result = _compute_kanban_metrics(db_path, range_key=range_key, tenant=tenant, assignee=assignee, now=now)
    window = RANGE_SECONDS[range_key]
    now_ts = float(now if now is not None else time.time())
    result["trail"] = _trail_metrics(
        tenant=tenant, cutoff=(now_ts - window) if window is not None else None, trail_db=trail_db
    )
    result["history"] = _history_metrics(tenant=tenant, trail_db=trail_db)
    return result


def default_kanban_db_path(board: Optional[str] = None) -> Path:
    """Hermes's active `kanban.db` (honors ``HERMES_KANBAN_DB`` / boards); see `store_kanban.resolve_kanban_db_path`."""
    return resolve_kanban_db_path(board)


def capture_metrics_snapshot(
    db_path: Optional[Path] = None,
    *,
    source: str = "manual",
    range_key: str = "all",
    repo_slug: Optional[str] = None,
    tenant: Optional[str] = None,
    scope_to_tenant: bool = False,
    extra: Optional[Mapping[str, Any]] = None,
    trail_db: Optional[Path] = None,
    lock_budget_s: Optional[float] = BEST_EFFORT_BUDGET_S,
) -> Optional[dict[str, Any]]:
    """Best-effort: compute metrics and append them to `trail.db` `metric_snapshots`.

    Metrics are factory-wide (``scope_to_tenant=False``) so milestone trends stay cumulative,
    while ``repo_slug`` / ``tenant`` tag the triggering scope for filtering. Returns the stored
    snapshot, or ``None`` on any failure; it never raises, so it cannot break factory execution.
    Every `trail.db` read and write here shares one ``lock_budget_s`` wall-clock budget, so lock
    contention skips the snapshot instead of stalling the caller; ``None`` (an explicit capture)
    keeps the bounded durable retry semantics instead.
    """
    budget = best_effort(lock_budget_s) if lock_budget_s is not None else nullcontext()
    try:
        with budget:
            kanban = Path(db_path) if db_path is not None else default_kanban_db_path()
            metrics = compute_metrics(
                kanban,
                range_key=range_key,
                tenant=tenant if scope_to_tenant and tenant else None,
                trail_db=trail_db,
            )
            return record_metric_snapshot(
                metrics,
                source=source,
                range_key=range_key,
                repo_slug=repo_slug,
                tenant=tenant,
                extra=extra,
                db_path=trail_db,
            )
    except Exception as exc:
        logger.warning("factonio metrics: failed to capture %s snapshot: %s", source, exc)
        return None


def _empty_panel_counts() -> dict[str, int]:
    return {"total": 0, "pass": 0, "reject": 0, "inconclusive": 0, "pending": 0, "disagreements": 0}


def _compute_kanban_metrics(
    db_path: Path,
    *,
    range_key: str,
    tenant: Optional[str],
    assignee: str,
    now: Optional[float],
) -> dict[str, Any]:
    if range_key not in RANGE_SECONDS:
        raise ValueError(f"Unsupported range {range_key!r} (expected one of {', '.join(RANGE_SECONDS)})")

    now_ts = float(now if now is not None else time.time())
    window = RANGE_SECONDS[range_key]
    cutoff = (now_ts - window) if window is not None else None

    empty = {
        "range": range_key,
        "tenant": tenant,
        "totals": {"cards": 0, "done": 0, "running": 0, "blocked": 0, "triage": 0, "workstreams": 0},
        "phases": {
            p: {"count": 0, "done": 0, "median_seconds": None, "p90_seconds": None}
            for p in ("define", "design", "implement", "verify-diff", "verify-blackbox")
        },
        "verification": {
            "diff": {"pass": 0, "reject": 0, "inconclusive": 0, "pass_rate": None},
            "blackbox": {"pass": 0, "reject": 0, "inconclusive": 0, "pass_rate": None},
        },
        "rework": {"avg_runs_per_implement_card": None, "cards_with_rework": 0, "first_pass_rate": None},
        "escapes": {"count": 0, "verified_total": 0, "escape_rate": None},
        "verification_panels": _empty_panel_counts(),
    }

    conn = _open_ro(db_path)
    if conn is None:
        return empty

    try:
        with closing(conn):
            where = ["assignee = ?"]
            params: list[Any] = [assignee]
            if tenant:
                where.append("tenant = ?")
                params.append(tenant)
            if cutoff is not None:
                where.append("created_at >= ?")
                params.append(cutoff)

            tasks = [
                dict(r)
                for r in conn.execute(
                    f"""
                    SELECT id, title, status, tenant, skills, created_at, completed_at, consecutive_failures, result, body
                      FROM tasks
                     WHERE {' AND '.join(where)}
                     ORDER BY created_at ASC
                    """,
                    params,
                ).fetchall()
            ]
            if not tasks:
                return empty

            task_ids = [t["id"] for t in tasks]
            runs_by_task: dict[str, list[dict[str, Any]]] = {}
            for r in chunked_in_query(
                conn,
                "SELECT id, task_id, started_at, ended_at, outcome, metadata FROM task_runs"
                " WHERE task_id IN ({placeholders}) ORDER BY id ASC",
                task_ids,
            ):
                runs_by_task.setdefault(r["task_id"], []).append(dict(r))

            phase_durations: dict[str, list[float]] = {
                "define": [],
                "design": [],
                "implement": [],
                "verify-diff": [],
                "verify-blackbox": [],
            }
            phase_counts = {p: {"count": 0, "done": 0} for p in phase_durations}
            tenants_seen: set[str] = set()

            diff_pass = diff_reject = diff_inconclusive = 0
            bb_pass = bb_reject = bb_inconclusive = 0
            impl_run_counts: list[int] = []
            impl_rework_cards = 0
            escape_count = 0
            panel_lanes: dict[str, list[dict[str, Any]]] = {}

            for t in tasks:
                if t.get("tenant"):
                    tenants_seen.add(str(t["tenant"]))
                status = str(t.get("status") or "")
                empty["totals"]["cards"] += 1
                if status in ("done", "running", "blocked", "triage"):
                    empty["totals"][status] += 1

                title_str = str(t.get("title") or "")
                if title_str.lower().startswith("[escape]"):
                    escape_count += 1

                phase = classify_phase(t)
                runs = runs_by_task.get(t["id"], [])

                if phase in phase_counts:
                    phase_counts[phase]["count"] += 1
                    if status == "done":
                        phase_counts[phase]["done"] += 1

                    # Active duration from runs (fallback to completed_at - created_at)
                    active_s = 0.0
                    for r in runs:
                        if r.get("started_at") and r.get("ended_at") and r["ended_at"] >= r["started_at"]:
                            active_s += float(r["ended_at"]) - float(r["started_at"])
                    if active_s <= 0 and t.get("completed_at") and t.get("created_at"):
                        active_s = max(0.0, float(t["completed_at"]) - float(t["created_at"]))
                    if status == "done" and active_s > 0:
                        phase_durations[phase].append(active_s)

                if phase == "implement" and runs:
                    impl_run_counts.append(len(runs))
                    if len(runs) > 1:
                        impl_rework_cards += 1

                if phase in _VERIFY_PHASES:
                    tag = parse_panel_tag(t.get("body"))
                    if tag is not None:
                        panel_lanes.setdefault(tag["panel_id"], []).append(_panel_lane(t, runs, tag))
                    verdict = _card_verdict(phase, status, runs)
                    if phase == "verify-diff":
                        if verdict == "pass":
                            diff_pass += 1
                        elif verdict in _REJECT_VERDICTS:
                            diff_reject += 1
                        elif verdict in _INCONCLUSIVE_VERDICTS:
                            diff_inconclusive += 1
                    else:
                        if verdict == "pass":
                            bb_pass += 1
                        elif verdict in _REJECT_VERDICTS:
                            bb_reject += 1
                        elif verdict in _INCONCLUSIVE_VERDICTS:
                            bb_inconclusive += 1

            empty["totals"]["workstreams"] = len(tenants_seen)
            for p, durs in phase_durations.items():
                empty["phases"][p] = {
                    "count": phase_counts[p]["count"],
                    "done": phase_counts[p]["done"],
                    "median_seconds": round(statistics.median(durs), 1) if durs else None,
                    "p90_seconds": _percentile(durs, 90),
                }

            # `inconclusive` counts toward the denominator: it is neither a pass nor hidden.
            diff_total = diff_pass + diff_reject + diff_inconclusive
            bb_total = bb_pass + bb_reject + bb_inconclusive
            empty["verification"] = {
                "diff": {
                    "pass": diff_pass,
                    "reject": diff_reject,
                    "inconclusive": diff_inconclusive,
                    "pass_rate": round(diff_pass / diff_total, 3) if diff_total else None,
                },
                "blackbox": {
                    "pass": bb_pass,
                    "reject": bb_reject,
                    "inconclusive": bb_inconclusive,
                    "pass_rate": round(bb_pass / bb_total, 3) if bb_total else None,
                },
            }

            if impl_run_counts:
                empty["rework"] = {
                    "avg_runs_per_implement_card": round(sum(impl_run_counts) / len(impl_run_counts), 2),
                    "cards_with_rework": impl_rework_cards,
                    "first_pass_rate": round((len(impl_run_counts) - impl_rework_cards) / len(impl_run_counts), 3),
                }

            verified_total = max(diff_pass, bb_pass)
            empty["escapes"] = {
                "count": escape_count,
                "verified_total": verified_total,
                "escape_rate": round(escape_count / verified_total, 3) if verified_total else None,
            }
            panel_counts = _empty_panel_counts()
            for lanes in panel_lanes.values():
                agg = aggregate_panel_verdicts(lanes)
                panel_counts["total"] += 1
                panel_counts[agg["consensus"]] += 1
                if agg["has_disagreement"]:
                    panel_counts["disagreements"] += 1
            empty["verification_panels"] = panel_counts
            return empty
    except sqlite3.Error:
        return empty


def thread_cards(
    db_path: Path,
    *,
    session_id: Optional[str] = None,
    tenant: Optional[str] = None,
    assignee: str = WORKER_PROFILE,
    limit: int = 100,
    trail_db: Optional[Path] = None,
) -> dict[str, Any]:
    """Return Factonio Kanban cards for a chat session (or all active cards if omitted).

    Additive read-only context: ``workstreams`` (mode per in-scope tenant, plus an explicitly queried
    ``tenant``) and ``arenas`` (Dashboard-safe `/arena` summaries for the same tenants, newest first).
    """
    result, card_modes = _thread_card_rows(db_path, session_id=session_id, tenant=tenant, assignee=assignee, limit=limit)
    tenants = sorted({str(c["tenant"]) for c in result["cards"] if c.get("tenant")} | ({tenant} if tenant else set()))
    workstreams = _resolve_workstreams(tenants, card_modes, trail_db=trail_db)
    for card in result["cards"]:
        card["workstream_kind"] = (workstreams.get(str(card.get("tenant") or "")) or {}).get("kind")
    result["workstreams"] = workstreams
    result["arenas"] = _arena_summaries(tenants, db_path)
    return result


def _arena_summaries(tenants: list[str], db_path: Path) -> list[dict[str, Any]]:
    if not tenants:
        return []
    try:
        return list_arena_summaries(tenants=tenants, db_path=db_path)
    except (sqlite3.Error, OSError, ValueError):
        return []


def _thread_card_rows(
    db_path: Path,
    *,
    session_id: Optional[str],
    tenant: Optional[str],
    assignee: str,
    limit: int,
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    """``({cards, counts}, {tenant: card-stamped mode})`` from a read-only `kanban.db` connection."""
    empty: tuple[dict[str, Any], dict[str, dict[str, Any]]] = ({"cards": [], "counts": _empty_counts()}, {})
    conn = _open_ro(db_path)
    if conn is None:
        return empty

    try:
        with closing(conn):
            where = ["assignee = ?"]
            params: list[Any] = [assignee]
            if tenant:
                where.append("tenant = ?")
                params.append(tenant)
            elif session_id:
                # Include cards directly stamped with session_id, subscribed via kanban_notify_subs,
                # and sibling cards sharing their tenant.
                matched_ids: set[str] = set()
                seed_tenants_set: set[str] = set()
                for r in conn.execute(
                    "SELECT id, tenant FROM tasks WHERE session_id = ?",
                    (session_id,),
                ).fetchall():
                    matched_ids.add(r["id"])
                    if r["tenant"]:
                        seed_tenants_set.add(r["tenant"])
                try:
                    for r in conn.execute(
                        """
                        SELECT t.id, t.tenant
                          FROM kanban_notify_subs s
                          JOIN tasks t ON t.id = s.task_id
                         WHERE s.chat_id = ?
                        """,
                        (session_id,),
                    ).fetchall():
                        matched_ids.add(r["id"])
                        if r["tenant"]:
                            seed_tenants_set.add(r["tenant"])
                except sqlite3.Error:
                    pass
                clauses = ["session_id = ?"]
                params.append(session_id)
                if matched_ids:
                    ph_ids = ",".join("?" for _ in matched_ids)
                    clauses.append(f"id IN ({ph_ids})")
                    params.extend(sorted(matched_ids))
                if seed_tenants_set:
                    ph_t = ",".join("?" for _ in seed_tenants_set)
                    clauses.append(f"tenant IN ({ph_t})")
                    params.extend(sorted(seed_tenants_set))
                where.append("(" + " OR ".join(clauses) + ")")

            task_cols = _table_columns(conn, "tasks")
            task_extra = ", last_failure_error" if "last_failure_error" in task_cols else ""
            rows = [
                dict(r)
                for r in conn.execute(
                    f"""
                    SELECT id, title, assignee, status, tenant, session_id, skills,
                           model_override, provider_override, block_kind,
                           created_at, started_at, completed_at, result, body{task_extra}
                      FROM tasks
                     WHERE {' AND '.join(where)}
                     ORDER BY created_at DESC
                     LIMIT ?
                    """,
                    [*params, int(limit)],
                ).fetchall()
            ]

            task_ids = [r["id"] for r in rows]
            runs_by_task: dict[str, list[dict[str, Any]]] = {}
            parents_by_child: dict[str, list[str]] = {}
            if task_ids:
                runs_by_task = load_runs_by_task(conn, task_ids)
                for link_table in ("task_links", "task_deps"):
                    try:
                        for edge in chunked_in_query(
                            conn,
                            f"SELECT parent_id, child_id FROM {link_table} WHERE child_id IN ({{placeholders}})",
                            task_ids,
                        ):
                            parents_by_child.setdefault(edge["child_id"], []).append(edge["parent_id"])
                        break
                    except sqlite3.Error:
                        continue

            counts = _empty_counts()
            cards: list[dict[str, Any]] = []
            card_modes: dict[str, dict[str, Any]] = {}
            now_ts = time.time()
            # Oldest first, so a workstream's root card stamps its mode before any follow-up card.
            for r in reversed(rows):
                mode = parse_workstream_mode(r, runs_by_task.get(r["id"], [])) if r.get("tenant") else None
                if mode is not None:
                    tenant_modes = card_modes.setdefault(str(r["tenant"]), {"kind": mode["kind"], "report_path": None})
                    if tenant_modes["kind"] == mode["kind"] and not tenant_modes["report_path"]:
                        tenant_modes["report_path"] = mode["report_path"]
            for r in rows:
                st = str(r.get("status") or "")
                if st in counts:
                    counts[st] += 1
                runs = runs_by_task.get(r["id"], [])
                created = r.get("created_at")
                completed = r.get("completed_at")
                elapsed = None
                if created:
                    end = float(completed) if completed else now_ts
                    elapsed = max(0, int(end - float(created)))
                phase = classify_phase(r)
                verdict = _card_verdict(phase, st, runs)
                panel_tag = parse_panel_tag(r.get("body")) if phase in _VERIFY_PHASES else None
                arena_tag = parse_arena_tag(r.get("body"))
                pr_ref, local_target = _card_targets(r, runs)
                if verdict in _REJECT_VERDICTS:
                    counts["rejected"] += 1
                elif verdict in _INCONCLUSIVE_VERDICTS:
                    counts["inconclusive"] += 1
                cards.append({
                    "id": r["id"],
                    "title": r["title"],
                    "status": st,
                    "phase": phase,
                    "tenant": r.get("tenant"),
                    "parents": parents_by_child.get(r["id"], []),
                    "model": r.get("model_override"),
                    "provider": r.get("provider_override"),
                    "block_kind": r.get("block_kind"),
                    "block_reason": r.get("result") if st in ("blocked", "triage") else None,
                    "pr": pr_ref,
                    "local_target": local_target,
                    "runs": len(runs),
                    "elapsed_seconds": elapsed,
                    "created_at": created,
                    "completed_at": completed,
                    "started_at": r.get("started_at"),
                    "session_id": r.get("session_id"),
                    "active_duration_seconds": _active_duration_seconds(runs, now_ts),
                    "verdict": verdict,
                    "summary": _card_summary(r, st, runs),
                    "last_outcome": runs[-1].get("outcome") if runs else None,
                    "last_error": _card_last_error(r, st, runs),
                    "panel_id": (panel_tag or {}).get("panel_id"),
                    "panel_vendor": (panel_tag or {}).get("vendor"),
                    "arena_id": (arena_tag or {}).get("arena_id"),
                    "arena_role": (arena_tag or {}).get("role"),
                    "arena_slot": (arena_tag or {}).get("slot"),
                    "arena_vendor": (arena_tag or {}).get("vendor"),
                    "arena_target": (arena_tag or {}).get("target"),
                })
            counts["attention"] = (
                counts["blocked"] + counts["triage"] + counts["rejected"] + counts["inconclusive"]
            )
            counts["total"] = len(cards)
            return {"cards": cards, "counts": counts}, card_modes
    except sqlite3.Error:
        return empty
