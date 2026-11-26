"""Read-only SQLite metrics and Thread Cards queries over Hermes kanban.db."""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import statistics
import time
from contextlib import closing, nullcontext
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Optional

from .config import WORKER_PROFILE, get_root_hermes_home_path
from .trail import (
    BEST_EFFORT_BUDGET_S,
    best_effort,
    record_metric_snapshot,
    summarize_metric_snapshots,
    summarize_trail,
)

logger = logging.getLogger("factonio.metrics")

RANGE_SECONDS: dict[str, Optional[int]] = {
    "7d": 7 * 86400,
    "30d": 30 * 86400,
    "90d": 90 * 86400,
    "all": None,
}

_PR_REF_RE = re.compile(r"([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)#(\d+)")
_PR_URL_RE = re.compile(r"https://github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/pull/(\d+)")


_COUNT_KEYS = ("running", "blocked", "triage", "ready", "done", "todo", "review")
_REJECT_VERDICTS = frozenset({"reject", "fail", "block", "changes_requested"})
# Tri-state verification: `inconclusive` is never a pass and always needs attention.
_INCONCLUSIVE_VERDICTS = frozenset({"inconclusive"})
_VERIFY_PHASES = frozenset({"verify-diff", "verify-blackbox"})
# Panel identity (id, rigor, vendor) is parsed independently of the free-form display `model=` label,
# so no label content can make a lane vanish from aggregation.
_PANEL_TAG_RE = re.compile(r"Verification panel: `(vp_[a-z0-9]+)` \(rigor=`([^`\n]+)`, vendor=`([^`\n]+)`")
_PANEL_MODEL_RE = re.compile(r", model=`([^\n]*)`\)")
_PASS_VERDICTS = frozenset({"pass"})
# Orchestrator-written stamp of an unpublished local-branch verification target (spawn_verification_pair).
_LOCAL_TARGET_RE = re.compile(
    r"^- Local branch target: `([^`\n]+)` \(base=`([^`\n]+)`, head=`([0-9a-f]{40}|[0-9a-f]{64})`\)$", re.MULTILINE
)
_REPO_LINE_RE = re.compile(r"^- Repository: `([^`\n]+)`", re.MULTILINE)
_FULL_SHA_RE = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
# Run outcomes that are not failures: a clean latest run clears any stale error.
_CLEAN_OUTCOMES = frozenset({"completed", "review_requested", "changes_requested", "scheduled", "approved"})


def _empty_counts() -> dict[str, int]:
    counts = {k: 0 for k in _COUNT_KEYS}
    counts.update({"rejected": 0, "inconclusive": 0, "attention": 0, "total": 0})
    return counts


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    try:
        return {str(r[1]) for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    except sqlite3.Error:
        return set()


def _text_or_none(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


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


def _metadata_verdict(runs: list[dict[str, Any]]) -> Optional[str]:
    last = runs[-1] if runs else {}
    verdict = str(_parse_json_dict(last.get("metadata")).get("verdict") or "").strip().lower()
    if verdict in _INCONCLUSIVE_VERDICTS:
        return "inconclusive"
    return verdict or None


def _explicit_verify_verdict(runs: list[dict[str, Any]]) -> Optional[str]:
    """Explicit verify verdict: `metadata.verdict`, else a fail-closed run outcome, else `None`.

    Never infers `pass`; high-rigor panel lanes use this directly so a done card without a
    recorded verdict stays pending and cannot unlock a merge.
    """
    verdict = _metadata_verdict(runs)
    if verdict:
        return verdict
    outcome = str((runs[-1] if runs else {}).get("outcome") or "").strip().lower()
    if outcome == "changes_requested":
        return "reject"
    if outcome in _INCONCLUSIVE_VERDICTS:
        return "inconclusive"
    return None


def _card_verdict(phase: str, status: str, runs: list[dict[str, Any]]) -> Optional[str]:
    """Resolve a card's verdict: explicit `metadata.verdict` first, then verify-phase fallbacks.

    `inconclusive` (from metadata or run outcome) is returned as-is and never defaults to `pass`;
    `pass` is only inferred for a done verify card that recorded no verdict at all (legacy
    display/metrics behavior; panel consensus uses `_explicit_verify_verdict` instead).
    """
    if phase not in _VERIFY_PHASES:
        return _metadata_verdict(runs)
    verdict = _explicit_verify_verdict(runs)
    if verdict:
        return verdict
    return "pass" if status == "done" else None


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


def parse_panel_tag(body: Any) -> Optional[dict[str, str]]:
    """Parse the ``- Verification panel: `vp_...` (rigor=..., vendor=..., model=...)`` card stamp."""
    text = str(body or "")
    m = _PANEL_TAG_RE.search(text)
    if not m:
        return None
    model = _PANEL_MODEL_RE.match(text, m.end())
    return {
        "panel_id": m.group(1),
        "rigor": m.group(2),
        "vendor": m.group(3),
        "model": model.group(1) if model else None,
    }


def _known_vendor(vendor: Any) -> Optional[str]:
    text = str(vendor or "").strip().lower()
    return text if text and text not in ("unknown", "none") else None


def _verdict_bucket(verdict: Optional[str]) -> Optional[str]:
    """Bucket a lane verdict; any unrecognized completed verdict is `inconclusive` (never a pass)."""
    if verdict is None:
        return None
    norm = str(verdict).strip().lower()
    if not norm:
        return None
    if norm in _REJECT_VERDICTS:
        return "reject"
    if norm in _PASS_VERDICTS:
        return "pass"
    return "inconclusive"


def _lane_label(lane: dict[str, Any]) -> str:
    phase = str(lane.get("phase") or "lane")
    vendor = lane.get("vendor") or "unknown"
    task_id = lane.get("task_id")
    return f"{phase}/{vendor}" + (f" ({task_id})" if task_id else "")


_EVIDENCE_TEXT_KEYS = ("issue", "title", "message", "summary", "description", "repro")
_EVIDENCE_MAX_ITEMS = 4
_EVIDENCE_MAX_CHARS = 160


def _evidence_text(item: Any) -> Optional[str]:
    """One-line human-readable excerpt of a finding / disclosed limit (dict or string)."""
    if isinstance(item, dict):
        text = next((str(item[k]) for k in _EVIDENCE_TEXT_KEYS if _text_or_none(item.get(k))), None)
        if text is None:
            text = json.dumps(item, sort_keys=True, default=str) if item else None
    else:
        text = str(item) if item is not None else None
    text = " ".join((text or "").split())
    if not text:
        return None
    return text if len(text) <= _EVIDENCE_MAX_CHARS else text[: _EVIDENCE_MAX_CHARS - 3].rstrip() + "..."


def _lane_items(lane: dict[str, Any], key: str) -> list[Any]:
    items = lane.get(key)
    if isinstance(items, (list, tuple)):
        return list(items)
    return [items] if items else []


def _dissent_evidence(lanes: list[dict[str, Any]]) -> str:
    """Bounded excerpts of findings / disclosed limits from non-pass (reject, inconclusive) lanes."""
    excerpts: list[str] = []
    for lane in lanes:
        if lane.get("bucket") not in ("reject", "inconclusive"):
            continue
        for key, kind in (("findings", "finding"), ("disclosed_limits", "limit")):
            for item in _lane_items(lane, key):
                text = _evidence_text(item)
                if text:
                    excerpts.append(f"[{lane['label']}] {kind}: {text}")
    shown = excerpts[:_EVIDENCE_MAX_ITEMS]
    extra = len(excerpts) - len(shown)
    return "; ".join(shown) + (f" (+{extra} more)" if extra > 0 else "")


def aggregate_panel_verdicts(lanes: list[dict[str, Any]]) -> dict[str, Any]:
    """Fail-closed consensus over high-rigor verification panel lanes.

    Each lane is a dict with ``status``, ``verdict`` and ``vendor`` (plus optional ``task_id``,
    ``phase``, ``model``, ``findings``, ``disclosed_limits``). Rules, in order: any reject-family
    verdict -> ``reject``; any inconclusive -> ``inconclusive``; any lane not done or without a
    verdict -> ``pending``; >= 2 lanes over >= 2 distinct known vendors -> ``pass``; otherwise
    ``inconclusive`` (degenerate single-vendor panel). Only ``pass`` sets ``merge_allowed``.
    """
    norm_lanes: list[dict[str, Any]] = []
    buckets: dict[str, list[str]] = {"pass": [], "reject": [], "inconclusive": [], "pending": []}
    findings: list[dict[str, Any]] = []
    limits: list[dict[str, Any]] = []
    vendors: list[str] = []
    for raw in lanes or []:
        lane = dict(raw or {})
        verdict = lane.get("verdict")
        verdict = str(verdict).strip().lower() if verdict is not None and str(verdict).strip() else None
        bucket = _verdict_bucket(verdict)
        status = str(lane.get("status") or "").strip().lower()
        complete = status == "done" and verdict is not None
        label = _lane_label(lane)
        lane.update({"verdict": verdict, "bucket": bucket, "complete": complete, "label": label})
        norm_lanes.append(lane)
        vendor = _known_vendor(lane.get("vendor"))
        if vendor and vendor not in vendors:
            vendors.append(vendor)
        if bucket is not None and (complete or bucket != "pass"):
            buckets[bucket].append(label)
        else:
            buckets["pending"].append(label)
        for key, sink in (("findings", findings), ("disclosed_limits", limits)):
            items = lane.get(key)
            if isinstance(items, (list, tuple)):
                for item in items:
                    sink.append({"lane": label, "vendor": lane.get("vendor"), "phase": lane.get("phase"), "item": item})
            elif items:
                sink.append({"lane": label, "vendor": lane.get("vendor"), "phase": lane.get("phase"), "item": items})

    if buckets["reject"]:
        consensus, reason = "reject", f"{len(buckets['reject'])} lane(s) rejected"
    elif buckets["inconclusive"]:
        consensus, reason = "inconclusive", f"{len(buckets['inconclusive'])} lane(s) inconclusive"
    elif any(not lane["complete"] for lane in norm_lanes):
        consensus, reason = "pending", f"{len(buckets['pending'])} lane(s) still pending"
    elif len(norm_lanes) >= 2 and len(vendors) >= 2:
        consensus, reason = "pass", f"all {len(norm_lanes)} lanes passed across {len(vendors)} vendor families"
    else:
        consensus = "inconclusive"
        reason = (
            f"degenerate panel: {len(norm_lanes)} lane(s) across {len(vendors)} known vendor famil"
            f"{'y' if len(vendors) == 1 else 'ies'}; a pass needs >= 2 lanes and >= 2 vendors"
        )

    decided = [b for b in ("pass", "reject", "inconclusive") if buckets[b]]
    has_disagreement = len(decided) > 1
    disagreement_summary: Optional[str] = None
    if has_disagreement:
        parts = [f"{b}: {', '.join(buckets[b])}" for b in decided]
        disagreement_summary = "Panel lanes disagree -- " + "; ".join(parts)
        evidence = _dissent_evidence(norm_lanes)
        if evidence:
            disagreement_summary += ". Dissenting evidence: " + evidence
        if buckets["pending"]:
            disagreement_summary += f" ({len(buckets['pending'])} lane(s) still pending)"
        disagreement_summary += "."

    return {
        "consensus": consensus,
        "merge_allowed": consensus == "pass",
        "reason": reason,
        "lane_count": len(norm_lanes),
        "completed_count": sum(1 for lane in norm_lanes if lane["complete"]),
        "vendors": sorted(vendors),
        "distinct_vendor_count": len(vendors),
        "verdicts": buckets,
        "has_disagreement": has_disagreement,
        "disagreement_summary": disagreement_summary,
        "findings": findings,
        "disclosed_limits": limits,
        "lanes": norm_lanes,
    }


def _local_target_from_body(body: Any) -> Optional[dict[str, Any]]:
    text = str(body or "")
    m = _LOCAL_TARGET_RE.search(text)
    if not m:
        return None
    repo = _REPO_LINE_RE.search(text)
    return {
        "repo": repo.group(1) if repo else None,
        "branch": m.group(1),
        "base_branch": m.group(2),
        "head_sha": m.group(3),
        "source": "card",
    }


def _local_target_from_metadata(meta: Mapping[str, Any]) -> Optional[dict[str, Any]]:
    if meta.get("pr_number") or meta.get("published_pr") or meta.get("pr_url"):
        return None
    branch, sha = meta.get("branch"), meta.get("head_sha")
    if not (isinstance(branch, str) and branch.strip() and isinstance(sha, str)):
        return None
    sha = sha.strip().lower()
    if not _FULL_SHA_RE.fullmatch(sha) or any(c.isspace() or c == "`" for c in branch.strip()):
        return None
    base = meta.get("base_branch")
    repo = meta.get("repo")
    return {
        "repo": str(repo) if isinstance(repo, str) and repo.strip() else None,
        "branch": branch.strip(),
        "base_branch": base.strip() if isinstance(base, str) and base.strip() else None,
        "head_sha": sha,
        "source": "metadata",
    }


def _extract_local_target(task: dict[str, Any], runs: list[dict[str, Any]]) -> Optional[dict[str, Any]]:
    """Unpublished local-branch target (``repo``, ``branch``, ``base_branch``, ``head_sha``) or ``None``.

    The orchestrator-written ``Local branch target:`` card stamp wins over worker-reported metadata;
    metadata (latest run first) counts only with a full commit SHA and no PR reference, e.g. a
    ``publish_policy: ask`` Implementation card's completion handoff.
    """
    stamped = _local_target_from_body(task.get("body"))
    if stamped is not None:
        return stamped
    for run in reversed(runs):
        found = _local_target_from_metadata(_parse_json_dict(run.get("metadata")))
        if found is not None:
            return found
    return None


def _card_targets(task: dict[str, Any], runs: list[dict[str, Any]]) -> tuple[Optional[dict[str, Any]], Optional[dict[str, Any]]]:
    """``(pr, local_target)``; a stamped local card has no PR, even if its branch name looks like ``a/b#1``."""
    local = _extract_local_target(task, runs)
    if local is not None and local.get("source") == "card":
        return None, local
    return _extract_pr_ref(task, runs), local


def _panel_lane(task: dict[str, Any], runs: list[dict[str, Any]], tag: dict[str, str]) -> dict[str, Any]:
    phase = classify_phase(task)
    status = str(task.get("status") or "")
    meta = _parse_json_dict(runs[-1].get("metadata")) if runs else {}
    pr, local = _card_targets(task, runs)
    return {
        "task_id": task.get("id"),
        "title": task.get("title"),
        "phase": phase,
        "status": status,
        "verdict": _explicit_verify_verdict(runs) if phase in _VERIFY_PHASES else None,
        "vendor": tag["vendor"],
        "model": tag["model"],
        "tenant": task.get("tenant"),
        "pr": pr,
        "local_target": local,
        "findings": meta.get("findings") if isinstance(meta.get("findings"), list) else [],
        "disclosed_limits": meta.get("disclosed_limits") if isinstance(meta.get("disclosed_limits"), list) else [],
        "created_at": task.get("created_at"),
        "seq": task.get("seq"),
    }


def _require_expected_lanes(agg: dict[str, Any], lanes: list[dict[str, Any]], expected: list[str]) -> list[str]:
    """Fail closed when spawned lanes are absent from ``lanes`` (missing card or unparseable stamp)."""
    observed = {str(lane.get("task_id")) for lane in lanes if lane.get("task_id")}
    missing = [tid for tid in dict.fromkeys(str(t) for t in expected if t) if tid not in observed]
    if not missing:
        return missing
    note = (
        f"{len(missing)} of {len(set(expected))} spawned panel lane(s) missing or unparseable: "
        f"{', '.join(missing)}; the panel cannot pass without every lane"
    )
    if agg["consensus"] != "reject":
        agg["consensus"] = "inconclusive"
        agg["reason"] = note
    agg["merge_allowed"] = False
    prior = agg.get("disagreement_summary")
    agg["disagreement_summary"] = f"{prior} {note}." if prior else f"{note}."
    for tid in missing:
        agg["disclosed_limits"].append(
            {"lane": tid, "vendor": None, "phase": None, "item": f"Expected panel lane {tid} missing or unparseable"}
        )
    return missing


def summarize_panel(
    panel_id: str, lanes: list[dict[str, Any]], *, expected_lane_ids: Optional[list[str]] = None
) -> dict[str, Any]:
    """`aggregate_panel_verdicts` plus panel identity (tenant, PR) derived from its lanes.

    ``expected_lane_ids`` (every task id spawned for the panel, when known) makes the summary fail
    closed if any spawned lane is absent, so a panel can never shrink into a passing subset.
    """
    agg = aggregate_panel_verdicts(lanes)
    missing = _require_expected_lanes(agg, lanes, expected_lane_ids) if expected_lane_ids else []
    first = lanes[0] if lanes else {}
    return {
        "panel_id": panel_id,
        "tenant": first.get("tenant"),
        "pr": next((lane.get("pr") for lane in lanes if lane.get("pr")), None),
        "local_target": next((lane.get("local_target") for lane in lanes if lane.get("local_target")), None),
        "created_at": min((lane.get("created_at") or 0 for lane in lanes), default=None),
        "seq": max((lane.get("seq") or 0 for lane in lanes), default=0),
        **agg,
        "expected_lane_count": len(set(expected_lane_ids)) if expected_lane_ids else None,
        "missing_lane_ids": missing,
    }


def load_panel_lanes(
    db_path: Path,
    *,
    panel_ids: Optional[list[str]] = None,
    tenant: Optional[str] = None,
    pr_number: Optional[int] = None,
    branch: Optional[str] = None,
    assignee: str = WORKER_PROFILE,
) -> dict[str, list[dict[str, Any]]]:
    """Read panel verification cards from `kanban.db` (read-only), grouped by ``panel_id``.

    ``pr_number`` keeps only PR-target lanes for that PR; ``branch`` only local-branch-target lanes
    for that branch.
    """
    conn = _open_ro(db_path)
    if conn is None:
        return {}
    grouped: dict[str, list[dict[str, Any]]] = {}
    try:
        with closing(conn):
            where = ["assignee = ?", "body LIKE ?"]
            params: list[Any] = [assignee, "%Verification panel: `vp_%"]
            if tenant:
                where.append("tenant = ?")
                params.append(tenant)
            if panel_ids:
                where.append("(" + " OR ".join("body LIKE ?" for _ in panel_ids) + ")")
                params.extend(f"%`{pid}`%" for pid in panel_ids)
            tasks = [
                dict(r)
                for r in conn.execute(
                    f"SELECT rowid AS seq, id, title, status, tenant, skills, created_at, result, body FROM tasks "
                    f"WHERE {' AND '.join(where)} ORDER BY created_at ASC, rowid ASC",
                    params,
                ).fetchall()
            ]
            if not tasks:
                return {}
            ids = [t["id"] for t in tasks]
            ph = ",".join("?" for _ in ids)
            run_cols = _table_columns(conn, "task_runs")
            run_extra = "".join(f", {c}" for c in ("summary", "error") if c in run_cols)
            runs_by_task: dict[str, list[dict[str, Any]]] = {}
            for r in conn.execute(
                f"SELECT id, task_id, started_at, ended_at, outcome, metadata{run_extra} FROM task_runs "
                f"WHERE task_id IN ({ph}) ORDER BY id ASC",
                ids,
            ).fetchall():
                runs_by_task.setdefault(r["task_id"], []).append(dict(r))
    except sqlite3.Error:
        return {}

    wanted = set(panel_ids or [])
    for task in tasks:
        tag = parse_panel_tag(task.get("body"))
        if tag is None or (wanted and tag["panel_id"] not in wanted):
            continue
        lane = _panel_lane(task, runs_by_task.get(task["id"], []), tag)
        if pr_number is not None and int((lane.get("pr") or {}).get("number") or -1) != int(pr_number):
            continue
        if branch is not None and (lane.get("local_target") or {}).get("branch") != branch:
            continue
        grouped.setdefault(tag["panel_id"], []).append(lane)
    return grouped


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
    if "factonio:design" in skill_set:
        return "design"
    if "factonio:implement" in skill_set:
        return "implement"
    title = str(task.get("title") or "").lower()
    if title.startswith("[define]") or "definition:" in title:
        return "define"
    if title.startswith("[design]") or "design:" in title:
        return "design"
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
    """Hermes's active `kanban.db` (honors ``HERMES_KANBAN_DB`` / boards when Hermes is importable)."""
    try:
        from hermes_cli.kanban_db import kanban_db_path

        return Path(kanban_db_path(board=board))
    except Exception:
        return get_root_hermes_home_path() / "kanban.db"


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
            placeholders = ",".join("?" for _ in task_ids)
            runs_by_task: dict[str, list[dict[str, Any]]] = {}
            for r in conn.execute(
                f"SELECT id, task_id, started_at, ended_at, outcome, metadata FROM task_runs WHERE task_id IN ({placeholders}) ORDER BY id ASC",
                task_ids,
            ).fetchall():
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
) -> dict[str, Any]:
    """Return Factonio Kanban cards for a chat session (or all active cards if omitted)."""
    conn = _open_ro(db_path)
    if conn is None:
        return {"cards": [], "counts": _empty_counts()}

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
                ph = ",".join("?" for _ in task_ids)
                run_cols = _table_columns(conn, "task_runs")
                run_extra = "".join(f", {c}" for c in ("summary", "error") if c in run_cols)
                for run in conn.execute(
                    f"SELECT id, task_id, started_at, ended_at, outcome, metadata{run_extra} FROM task_runs WHERE task_id IN ({ph}) ORDER BY id ASC",
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

            counts = _empty_counts()
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
                phase = classify_phase(r)
                verdict = _card_verdict(phase, st, runs)
                panel_tag = parse_panel_tag(r.get("body")) if phase in _VERIFY_PHASES else None
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
                })
            counts["attention"] = (
                counts["blocked"] + counts["triage"] + counts["rejected"] + counts["inconclusive"]
            )
            counts["total"] = len(cards)
            return {"cards": cards, "counts": counts}
    except sqlite3.Error:
        return {"cards": [], "counts": _empty_counts()}
