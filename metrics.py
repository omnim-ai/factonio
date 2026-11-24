"""Read-only SQLite metrics and Thread Cards queries over Hermes kanban.db."""

from __future__ import annotations

import json
import re
import sqlite3
import statistics
import time
from contextlib import closing
from pathlib import Path
from typing import Any, Optional

from .config import WORKER_PROFILE

RANGE_SECONDS: dict[str, Optional[int]] = {
    "7d": 7 * 86400,
    "30d": 30 * 86400,
    "90d": 90 * 86400,
    "all": None,
}

_PR_REF_RE = re.compile(r"([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)#(\d+)")
_PR_URL_RE = re.compile(r"https://github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/pull/(\d+)")


def _open_ro(db_path: Path) -> Optional[sqlite3.Connection]:
    if not db_path.is_file():
        return None
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=3.0)
    conn.row_factory = sqlite3.Row
    return conn


def _parse_json_list(raw: Any) -> list[str]:
    if isinstance(raw, list):
        return [str(x) for x in raw]
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                return [str(x) for x in parsed]
        except ValueError:
            pass
    return []


def _parse_json_dict(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                return parsed
        except ValueError:
            pass
    return {}


def classify_phase(task: dict[str, Any]) -> str:
    """Classify a Factonio Kanban card into its pipeline phase."""
    skills = _parse_json_list(task.get("skills"))
    skill_set = {s.lower() for s in skills}
    if "factonio:verify-diff" in skill_set:
        return "verify-diff"
    if "factonio:verify-blackbox" in skill_set:
        return "verify-blackbox"
    if "factonio:define" in skill_set:
        return "define"
    if "factonio:implement" in skill_set:
        return "implement"
    title = str(task.get("title") or "").lower()
    if title.startswith("[define]") or "definition:" in title:
        return "define"
    if title.startswith("[verify:diff]") or "diff check" in title:
        return "verify-diff"
    if title.startswith("[verify:blackbox]") or "black-box" in title:
        return "verify-blackbox"
    if title.startswith("[implement]"):
        return "implement"
    return "other"


def _extract_pr_ref(task: dict[str, Any], runs: list[dict[str, Any]]) -> Optional[dict[str, Any]]:
    for r in reversed(runs):
        meta = _parse_json_dict(r.get("metadata"))
        pr_url = meta.get("published_pr") or meta.get("pr_url")
        if isinstance(pr_url, str):
            m = _PR_URL_RE.search(pr_url)
            if m:
                return {"repo": m.group(1), "number": int(m.group(2)), "url": pr_url}
        pr_num = meta.get("pr_number")
        pr_repo = meta.get("repo")
        if pr_num and pr_repo:
            return {
                "repo": str(pr_repo),
                "number": int(pr_num),
                "url": f"https://github.com/{pr_repo}/pull/{int(pr_num)}",
            }

    blob = f"{task.get('title') or ''}\n{task.get('body') or ''}\n{task.get('result') or ''}"
    m_url = _PR_URL_RE.search(blob)
    if m_url:
        return {"repo": m_url.group(1), "number": int(m_url.group(2)), "url": m_url.group(0)}
    m_ref = _PR_REF_RE.search(blob)
    if m_ref:
        return {
            "repo": m_ref.group(1),
            "number": int(m_ref.group(2)),
            "url": f"https://github.com/{m_ref.group(1)}/pull/{int(m_ref.group(2))}",
        }
    return None


def _percentile(values: list[float], pct: float) -> Optional[float]:
    if not values:
        return None
    s = sorted(values)
    idx = min(len(s) - 1, max(0, int(round((pct / 100.0) * (len(s) - 1)))))
    return round(s[idx], 1)


def compute_metrics(
    db_path: Path,
    *,
    range_key: str = "30d",
    tenant: Optional[str] = None,
    assignee: str = WORKER_PROFILE,
    now: Optional[float] = None,
) -> dict[str, Any]:
    """Compute phase durations, verification pass rates, rework, and escape rate."""
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
            for p in ("define", "implement", "verify-diff", "verify-blackbox")
        },
        "verification": {
            "diff": {"pass": 0, "reject": 0, "pass_rate": None},
            "blackbox": {"pass": 0, "reject": 0, "pass_rate": None},
        },
        "rework": {"avg_runs_per_implement_card": None, "cards_with_rework": 0},
        "escapes": {"count": 0, "verified_total": 0, "escape_rate": None},
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
            placeholders = ",".join("?" for _ in task_ids)
            runs_by_task: dict[str, list[dict[str, Any]]] = {}
            for r in conn.execute(
                f"SELECT id, task_id, started_at, ended_at, outcome, metadata FROM task_runs WHERE task_id IN ({placeholders}) ORDER BY id ASC",
                task_ids,
            ).fetchall():
                runs_by_task.setdefault(r["task_id"], []).append(dict(r))

            phase_durations: dict[str, list[float]] = {
                "define": [],
                "implement": [],
                "verify-diff": [],
                "verify-blackbox": [],
            }
            phase_counts = {p: {"count": 0, "done": 0} for p in phase_durations}
            tenants_seen: set[str] = set()

            diff_pass = diff_reject = bb_pass = bb_reject = 0
            impl_run_counts: list[int] = []
            impl_rework_cards = 0
            escape_count = 0

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

                if phase in ("verify-diff", "verify-blackbox"):
                    last_meta = _parse_json_dict(runs[-1].get("metadata")) if runs else {}
                    verdict = str(last_meta.get("verdict") or "").lower()
                    if not verdict and status == "done":
                        verdict = "pass"
                    elif not verdict and runs and runs[-1].get("outcome") == "changes_requested":
                        verdict = "reject"
                    if phase == "verify-diff":
                        if verdict == "pass":
                            diff_pass += 1
                        elif verdict in {"reject", "fail", "block", "changes_requested"}:
                            diff_reject += 1
                    else:
                        if verdict == "pass":
                            bb_pass += 1
                        elif verdict in {"reject", "fail", "block", "changes_requested"}:
                            bb_reject += 1

            empty["totals"]["workstreams"] = len(tenants_seen)
            for p, durs in phase_durations.items():
                empty["phases"][p] = {
                    "count": phase_counts[p]["count"],
                    "done": phase_counts[p]["done"],
                    "median_seconds": round(statistics.median(durs), 1) if durs else None,
                    "p90_seconds": _percentile(durs, 90),
                }

            diff_total = diff_pass + diff_reject
            bb_total = bb_pass + bb_reject
            empty["verification"] = {
                "diff": {
                    "pass": diff_pass,
                    "reject": diff_reject,
                    "pass_rate": round(diff_pass / diff_total, 3) if diff_total else None,
                },
                "blackbox": {
                    "pass": bb_pass,
                    "reject": bb_reject,
                    "pass_rate": round(bb_pass / bb_total, 3) if bb_total else None,
                },
            }

            if impl_run_counts:
                empty["rework"] = {
                    "avg_runs_per_implement_card": round(sum(impl_run_counts) / len(impl_run_counts), 2),
                    "cards_with_rework": impl_rework_cards,
                }

            verified_total = max(diff_pass, bb_pass)
            empty["escapes"] = {
                "count": escape_count,
                "verified_total": verified_total,
                "escape_rate": round(escape_count / verified_total, 3) if verified_total else None,
            }
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
) -> dict[str, Any]:
    """Return Factonio Kanban cards for a chat session (or all active cards if omitted)."""
    conn = _open_ro(db_path)
    if conn is None:
        return {"cards": [], "counts": {"running": 0, "blocked": 0, "triage": 0, "ready": 0, "done": 0}}

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

            rows = [
                dict(r)
                for r in conn.execute(
                    f"""
                    SELECT id, title, assignee, status, tenant, session_id, skills,
                           model_override, provider_override, block_kind,
                           created_at, started_at, completed_at, result, body
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
                ph = ",".join("?" for _ in task_ids)
                for run in conn.execute(
                    f"SELECT id, task_id, started_at, ended_at, outcome, metadata FROM task_runs WHERE task_id IN ({ph}) ORDER BY id ASC",
                    task_ids,
                ).fetchall():
                    runs_by_task.setdefault(run["task_id"], []).append(dict(run))
                for link_table in ("task_links", "task_deps"):
                    try:
                        for edge in conn.execute(
                            f"SELECT parent_id, child_id FROM {link_table} WHERE child_id IN ({ph})",
                            task_ids,
                        ).fetchall():
                            parents_by_child.setdefault(edge["child_id"], []).append(edge["parent_id"])
                        break
                    except sqlite3.Error:
                        continue

            counts = {"running": 0, "blocked": 0, "triage": 0, "ready": 0, "done": 0}
            cards: list[dict[str, Any]] = []
            now_ts = time.time()
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
                cards.append({
                    "id": r["id"],
                    "title": r["title"],
                    "status": st,
                    "phase": classify_phase(r),
                    "tenant": r.get("tenant"),
                    "parents": parents_by_child.get(r["id"], []),
                    "model": r.get("model_override"),
                    "provider": r.get("provider_override"),
                    "block_kind": r.get("block_kind"),
                    "block_reason": r.get("result") if st in ("blocked", "triage") else None,
                    "pr": _extract_pr_ref(r, runs),
                    "runs": len(runs),
                    "elapsed_seconds": elapsed,
                    "created_at": created,
                    "completed_at": completed,
                })
            return {"cards": cards, "counts": counts}
    except sqlite3.Error:
        return {"cards": [], "counts": {"running": 0, "blocked": 0, "triage": 0, "ready": 0, "done": 0}}
