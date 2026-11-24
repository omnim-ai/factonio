"""In-process Kanban dispatch-tick watchdog for Factonio workstreams."""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import time
from contextlib import closing
from pathlib import Path
from typing import Any, Callable, Optional

from .config import WORKER_PROFILE, get_root_hermes_home_path, load_settings

logger = logging.getLogger("factonio.watchdog")

STATE_KEY_LAST_SCAN = "watchdog.last_scan_ts"
STATE_KEY_ALERTS = "watchdog.reported_digests"


def _open_kanban_ro(db_path: Path) -> Optional[sqlite3.Connection]:
    if not db_path.is_file():
        return None
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=3.0)
    conn.row_factory = sqlite3.Row
    return conn


def _card_revision_digest(task: dict[str, Any], comments: list[dict[str, Any]]) -> str:
    payload = {
        "status": task.get("status"),
        "block_kind": task.get("block_kind"),
        "result": task.get("result"),
        "consecutive_failures": task.get("consecutive_failures"),
        "block_recurrences": task.get("block_recurrences"),
        "comments": [(c.get("author"), c.get("body"), c.get("created_at")) for c in comments[:4]],
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode("utf-8")).hexdigest()[:16]


def scan_stuck_cards(
    db_path: Path,
    *,
    now: Optional[float] = None,
    stale_heartbeat_minutes: int = 20,
    blocked_reminder_minutes: int = 45,
    max_fix_rounds: int = 3,
    assignee: str = WORKER_PROFILE,
) -> list[dict[str, Any]]:
    """Inspect kanban.db read-only and return structured alerts for stuck Factonio cards."""
    now_ts = float(now if now is not None else time.time())
    conn = _open_kanban_ro(db_path)
    if conn is None:
        return []

    findings: list[dict[str, Any]] = []
    stale_s = max(60, int(stale_heartbeat_minutes) * 60)
    blocked_s = max(60, int(blocked_reminder_minutes) * 60)

    try:
        with closing(conn):
            rows = conn.execute(
                """
                SELECT id, title, assignee, status, tenant, session_id,
                       block_kind, result, consecutive_failures, block_recurrences,
                       last_heartbeat_at, started_at, created_at
                  FROM tasks
                 WHERE assignee = ?
                   AND status IN ('running', 'blocked', 'triage')
                 ORDER BY created_at ASC
                """,
                (assignee,),
            ).fetchall()

            for row in rows:
                task = dict(row)
                tid = task["id"]
                status = task["status"]
                comments = [
                    dict(c)
                    for c in conn.execute(
                        "SELECT author, body, created_at FROM task_comments WHERE task_id = ? ORDER BY created_at DESC LIMIT 4",
                        (tid,),
                    ).fetchall()
                ]
                run_row = conn.execute(
                    """
                    SELECT id, status, worker_pid, started_at, ended_at, last_heartbeat_at, outcome, error
                      FROM task_runs
                     WHERE task_id = ?
                     ORDER BY id DESC
                     LIMIT 1
                    """,
                    (tid,),
                ).fetchone()
                run = dict(run_row) if run_row else None
                last_ev = conn.execute(
                    "SELECT created_at FROM task_events WHERE task_id = ? ORDER BY id DESC LIMIT 1",
                    (tid,),
                ).fetchone()
                last_activity = (
                    (last_ev["created_at"] if last_ev else None)
                    or (run or {}).get("ended_at")
                    or (run or {}).get("started_at")
                    or task.get("started_at")
                    or task.get("created_at")
                )

                reason: Optional[str] = None
                kind: Optional[str] = None

                if status == "triage":
                    kind = "triage"
                    reason = task.get("result") or "Card escalated to triage and needs intervention."
                elif status == "running":
                    last_beat = (
                        (run or {}).get("last_heartbeat_at")
                        or task.get("last_heartbeat_at")
                        or (run or {}).get("started_at")
                        or task.get("started_at")
                        or task.get("created_at")
                    )
                    if last_beat and (now_ts - float(last_beat)) >= stale_s:
                        mins = int((now_ts - float(last_beat)) // 60)
                        kind = "stale_running"
                        reason = f"Running card has no heartbeat for {mins}m (threshold {stale_heartbeat_minutes}m)."
                elif status == "blocked":
                    failures = int(task.get("consecutive_failures") or 0)
                    if failures >= max_fix_rounds:
                        kind = "fix_cap_reached"
                        reason = f"Blocked after {failures} consecutive failures (cap {max_fix_rounds}): {task.get('result') or 'no reason'}"
                    elif last_activity and (now_ts - float(last_activity)) >= blocked_s:
                        mins = int((now_ts - float(last_activity)) // 60)
                        kind = "blocked_waiting"
                        reason = f"Blocked ({task.get('block_kind') or 'unknown'}) for {mins}m: {task.get('result') or 'waiting on input'}"

                if kind and reason:
                    findings.append({
                        "task_id": tid,
                        "title": task.get("title") or "",
                        "tenant": task.get("tenant") or "",
                        "session_id": task.get("session_id") or "",
                        "status": status,
                        "kind": kind,
                        "reason": reason,
                        "digest": _card_revision_digest(task, comments),
                    })
    except sqlite3.Error as exc:
        logger.debug("factonio watchdog scan skipped (%s)", exc)
        return []

    return findings


def run_watchdog_tick(
    ctx: Any,
    *,
    board: Optional[str] = None,
    dry_run: bool = False,
    now: Optional[float] = None,
    db_path_override: Optional[Path] = None,
    notify_fn: Optional[Callable[[dict[str, Any]], None]] = None,
) -> dict[str, Any]:
    """Throttled tick handler invoked from `on_kanban_dispatch_tick`."""
    if dry_run:
        return {"ran": False, "reason": "dry_run", "alerts": []}

    settings = load_settings(use_root=True)
    interval_s = max(30, int(settings.get("watchdog_interval_minutes") or 5) * 60)
    now_ts = float(now if now is not None else time.time())

    last_ts = float(ctx.state.get(STATE_KEY_LAST_SCAN, 0.0) or 0.0)
    if last_ts and (now_ts - last_ts) < interval_s:
        return {"ran": False, "reason": "throttled", "alerts": []}

    ctx.state.set(STATE_KEY_LAST_SCAN, now_ts)

    db_path = db_path_override
    if db_path is None:
        try:
            from hermes_cli.kanban_db import kanban_db_path

            db_path = Path(kanban_db_path(board=board))
        except Exception:
            db_path = get_root_hermes_home_path() / "kanban.db"

    findings = scan_stuck_cards(
        db_path,
        now=now_ts,
        stale_heartbeat_minutes=int(settings.get("stale_heartbeat_minutes") or 20),
        blocked_reminder_minutes=int(settings.get("blocked_reminder_minutes") or 45),
        max_fix_rounds=int(settings.get("default_max_fix_rounds") or 3),
    )

    reported: dict[str, str] = dict(ctx.state.get(STATE_KEY_ALERTS, {}) or {})
    emitted: list[dict[str, Any]] = []
    active_ids = {f["task_id"] for f in findings}

    # Prune resolved tasks from deduplication map so future stalls re-alert cleanly.
    reported = {tid: dig for tid, dig in reported.items() if tid in active_ids}

    for item in findings:
        tid = item["task_id"]
        if reported.get(tid) == item["digest"]:
            continue
        reported[tid] = item["digest"]
        emitted.append(item)
        try:
            ctx.emit("watchdog_alert", item)
        except Exception:
            pass
        if notify_fn is not None:
            notify_fn(item)

    ctx.state.set(STATE_KEY_ALERTS, reported)
    return {"ran": True, "scanned": len(findings), "alerts": emitted}
