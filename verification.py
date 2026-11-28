"""Read-only verification projections over Hermes `kanban.db` (Layer 2: vocabulary, validators, store, trail).

Card phase classification, verification panel / arena / workstream-mode stamp parsing, card targets
and verdicts, fail-closed panel consensus, and local-branch verification status. `metrics` and
`orchestrator` re-export these names unchanged.
"""

from __future__ import annotations

import json
import re
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any, Mapping, Optional

from .store_kanban import chunked_in_query, open_kanban_ro, table_columns
from .trail import query_decisions
from .validators import FULL_SHA_LOWER_PATTERN, FULL_SHA_RE
from .vocabulary import (
    ARENA_TARGETS,
    INCONCLUSIVE_VERDICTS,
    PASS_VERDICTS,
    REJECT_VERDICTS,
    VERIFY_PHASES,
    WORKER_PROFILE,
    WORKSTREAM_KINDS,
    contains_holdout_reference,
)

_PR_REF_RE = re.compile(r"([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)#(\d+)")
_PR_URL_RE = re.compile(r"https://github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/pull/(\d+)")

# Panel identity (id, rigor, vendor) is parsed independently of the free-form display `model=` label,
# so no label content can make a lane vanish from aggregation.
_PANEL_TAG_RE = re.compile(r"Verification panel: `(vp_[a-z0-9]+)` \(rigor=`([^`\n]+)`, vendor=`([^`\n]+)`")
_PANEL_MODEL_RE = re.compile(r", model=`([^\n]*)`\)")
# Arena identity is parsed independently of the free-form `model=` tail (see `_PANEL_TAG_RE`).
_ARENA_TAG_RE = re.compile(
    r"- Arena: `(ar_[0-9a-f]{6,32})` \(role=`([^`\n]+)`, target=`([^`\n]+)`(?:, slot=`([^`\n]+)`)?, vendor=`([^`\n]+)`"
)
_ARENA_ROLES: tuple[str, ...] = ("candidate", "judge")
_MODE_LINE_RE = re.compile(r"^- Mode: `([a-z]+)`$", re.MULTILINE)
_REPORT_LINE_RE = re.compile(r"^- Investigation report path: `([^`\n]+)`$", re.MULTILINE)
# Mode contract headings stamped by `orchestrator.mode_contract_section` (incl. skip_define cards).
_MODE_HEADINGS: tuple[tuple[str, str], ...] = (
    ("## Investigation Contract (read-only)", "investigate"),
    ("## Refactor Contract (behavior preservation)", "refactor"),
    ("## Behavior-Preservation Contract (refactor)", "refactor"),
    ("## Performance Contract (benchmark-checklist)", "perf"),
)
# Orchestrator-written stamp of an unpublished local-branch verification target (spawn_verification_pair).
_LOCAL_TARGET_RE = re.compile(
    r"^- Local branch target: `([^`\n]+)` \(base=`([^`\n]+)`, head=`(" + FULL_SHA_LOWER_PATTERN + r")`\)$", re.MULTILINE
)
_REPO_LINE_RE = re.compile(r"^- Repository: `([^`\n]+)`", re.MULTILINE)


def text_or_none(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _metadata_verdict(runs: list[dict[str, Any]]) -> Optional[str]:
    last = runs[-1] if runs else {}
    verdict = str(parse_json_dict(last.get("metadata")).get("verdict") or "").strip().lower()
    if verdict in INCONCLUSIVE_VERDICTS:
        return "inconclusive"
    return verdict or None


def explicit_verify_verdict(runs: list[dict[str, Any]]) -> Optional[str]:
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
    if outcome in INCONCLUSIVE_VERDICTS:
        return "inconclusive"
    return None


def card_verdict(phase: str, status: str, runs: list[dict[str, Any]]) -> Optional[str]:
    """Resolve a card's verdict: explicit `metadata.verdict` first, then verify-phase fallbacks.

    `inconclusive` (from metadata or run outcome) is returned as-is and never defaults to `pass`;
    `pass` is only inferred for a done verify card that recorded no verdict at all (legacy
    display/metrics behavior; panel consensus uses `explicit_verify_verdict` instead).
    """
    if phase not in VERIFY_PHASES:
        return _metadata_verdict(runs)
    verdict = explicit_verify_verdict(runs)
    if verdict:
        return verdict
    return "pass" if status == "done" else None


def parse_json_list(raw: Any) -> list[str]:
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


def parse_json_dict(raw: Any) -> dict[str, Any]:
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


def parse_arena_tag(body: Any) -> Optional[dict[str, Optional[str]]]:
    """Parse the ``- Arena: `ar_...` (role=..., target=..., [slot=...,] vendor=..., model=...)`` card stamp."""
    text = str(body or "")
    m = _ARENA_TAG_RE.search(text)
    if not m:
        return None
    model = _PANEL_MODEL_RE.match(text, m.end())
    tag = {
        "arena_id": m.group(1),
        "role": m.group(2) if m.group(2) in _ARENA_ROLES else None,
        "target": m.group(3) if m.group(3) in ARENA_TARGETS else None,
        "slot": m.group(4),
        "vendor": m.group(5),
        "model": model.group(1) if model else None,
    }
    # Card bodies are free text: fail closed on any stamp field that names holdout.
    return {k: None if v is None or contains_holdout_reference(v) else v for k, v in tag.items()}


def parse_workstream_mode(task: Mapping[str, Any], runs: Optional[list[dict[str, Any]]] = None) -> Optional[dict[str, Any]]:
    """``{kind, report_path}`` stamped on a workstream card, or ``None`` for cards without mode data.

    Sources, in order: the ``- Mode:`` header line, the ``[define] [investigate]`` title tag, then a
    mode contract heading. ``report_path`` is only set for ``investigate``: the
    ``- Investigation report path:`` line, else the latest run's ``metadata.report_path``.
    """
    body = str(task.get("body") or "")
    kind: Optional[str] = None
    m = _MODE_LINE_RE.search(body)
    if m and m.group(1) in WORKSTREAM_KINDS:
        kind = m.group(1)
    elif "[define] [investigate]" in str(task.get("title") or "").lower():
        kind = "investigate"
    else:
        lines = {line.rstrip() for line in body.splitlines()}
        kind = next((k for heading, k in _MODE_HEADINGS if heading in lines), None)
    if kind is None:
        return None
    report_path: Optional[str] = None
    if kind == "investigate":
        rm = _REPORT_LINE_RE.search(body)
        if rm:
            report_path = rm.group(1).strip()
        elif runs:
            meta_path = parse_json_dict(runs[-1].get("metadata")).get("report_path")
            report_path = meta_path.strip() if isinstance(meta_path, str) and meta_path.strip() else None
    return {"kind": kind, "report_path": report_path}

def _known_vendor(vendor: Any) -> Optional[str]:
    text = str(vendor or "").strip().lower()
    return text if text and text not in ("unknown", "none") else None


def verdict_bucket(verdict: Optional[str]) -> Optional[str]:
    """Bucket a lane verdict; any unrecognized completed verdict is `inconclusive` (never a pass)."""
    if verdict is None:
        return None
    norm = str(verdict).strip().lower()
    if not norm:
        return None
    if norm in REJECT_VERDICTS:
        return "reject"
    if norm in PASS_VERDICTS:
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
        text = next((str(item[k]) for k in _EVIDENCE_TEXT_KEYS if text_or_none(item.get(k))), None)
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
        bucket = verdict_bucket(verdict)
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


def local_target_from_body(body: Any) -> Optional[dict[str, Any]]:
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


def _has_pr_metadata(meta: Mapping[str, Any]) -> bool:
    return bool(meta.get("pr_number") or meta.get("published_pr") or meta.get("pr_url"))


def local_target_from_metadata(meta: Mapping[str, Any]) -> Optional[dict[str, Any]]:
    if _has_pr_metadata(meta):
        return None
    branch, sha = meta.get("branch"), meta.get("head_sha")
    if not (isinstance(branch, str) and branch.strip() and isinstance(sha, str)):
        return None
    sha = sha.strip().lower()
    if not FULL_SHA_RE.fullmatch(sha) or any(c.isspace() or c == "`" for c in branch.strip()):
        return None
    base = meta.get("base_branch")
    repo = meta.get("repo")
    target: dict[str, Any] = {
        "repo": str(repo) if isinstance(repo, str) and repo.strip() else None,
        "branch": branch.strip(),
        "base_branch": base.strip() if isinstance(base, str) and base.strip() else None,
        "head_sha": sha,
        "source": "metadata",
    }
    # Prefill for the Desktop Publish PR confirmation; present only as a clean, barrier-safe string.
    for key in ("pr_title", "pr_body"):
        value = meta.get(key)
        if isinstance(value, str) and value.strip() and not contains_holdout_reference(value):
            target[key] = value
    return target


def extract_local_target(task: dict[str, Any], runs: list[dict[str, Any]]) -> Optional[dict[str, Any]]:
    """Unpublished local-branch target (``repo``, ``branch``, ``base_branch``, ``head_sha``) or ``None``.

    The orchestrator-written ``Local branch target:`` card stamp wins over worker-reported metadata;
    metadata (latest run first) counts only with a full commit SHA and no PR reference, e.g. a
    ``publish_policy: ask`` Implementation card's completion handoff. Scanning stops at the newest
    run that published a PR, so an earlier unpublished run's handoff never reads as current.
    """
    stamped = local_target_from_body(task.get("body"))
    if stamped is not None:
        return stamped
    for run in reversed(runs):
        meta = parse_json_dict(run.get("metadata"))
        if _has_pr_metadata(meta):
            return None
        found = local_target_from_metadata(meta)
        if found is not None:
            return found
    return None


def card_targets(task: dict[str, Any], runs: list[dict[str, Any]]) -> tuple[Optional[dict[str, Any]], Optional[dict[str, Any]]]:
    """``(pr, local_target)``; at most one is set.

    A stamped local card has no PR, even if its branch name looks like ``a/b#1``. Verification cards
    only ever take their target from the orchestrator stamp: worker-reported ``branch`` / ``head_sha``
    metadata on a PR-target lane must never turn it into a local-branch lane. Otherwise the newest run
    carrying a structured target (PR or local handoff) wins; free-text ``owner/repo#N`` mentions in the
    title/body/result (e.g. ``- GitHub Issue:`` or ``- Stack parent PR:`` lines) are only a fallback.
    """
    stamped = local_target_from_body(task.get("body"))
    if stamped is not None:
        return None, stamped
    if classify_phase(task) in VERIFY_PHASES:
        return extract_pr_ref(task, runs), None
    for run in reversed(runs):
        meta = parse_json_dict(run.get("metadata"))
        if _has_pr_metadata(meta):
            return extract_pr_ref(task, runs), None
        local = local_target_from_metadata(meta)
        if local is not None:
            return None, local
    return pr_ref_from_text(task), None


def panel_lane(task: dict[str, Any], runs: list[dict[str, Any]], tag: dict[str, str]) -> dict[str, Any]:
    phase = classify_phase(task)
    status = str(task.get("status") or "")
    meta = parse_json_dict(runs[-1].get("metadata")) if runs else {}
    pr, local = card_targets(task, runs)
    return {
        "task_id": task.get("id"),
        "title": task.get("title"),
        "phase": phase,
        "status": status,
        "verdict": explicit_verify_verdict(runs) if phase in VERIFY_PHASES else None,
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


def load_runs_by_task(conn: sqlite3.Connection, task_ids: list[Any]) -> dict[str, list[dict[str, Any]]]:
    """``{task_id: [run, ...]}`` (oldest first) for ``task_ids``, chunked under SQLite's variable limit.

    Optional ``summary`` / ``error`` columns are selected only when the board schema has them.
    """
    run_cols = table_columns(conn, "task_runs")
    run_extra = "".join(f", {c}" for c in ("summary", "error") if c in run_cols)
    out: dict[str, list[dict[str, Any]]] = {}
    for r in chunked_in_query(
        conn,
        f"SELECT id, task_id, started_at, ended_at, outcome, metadata{run_extra} FROM task_runs "
        "WHERE task_id IN ({placeholders}) ORDER BY id ASC",
        task_ids,
    ):
        out.setdefault(r["task_id"], []).append(dict(r))
    return out


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
    conn = open_kanban_ro(db_path)
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
            runs_by_task = load_runs_by_task(conn, [t["id"] for t in tasks])
    except sqlite3.Error:
        return {}

    wanted = set(panel_ids or [])
    for task in tasks:
        tag = parse_panel_tag(task.get("body"))
        if tag is None or (wanted and tag["panel_id"] not in wanted):
            continue
        lane = panel_lane(task, runs_by_task.get(task["id"], []), tag)
        if pr_number is not None and int((lane.get("pr") or {}).get("number") or -1) != int(pr_number):
            continue
        if branch is not None:
            local = lane.get("local_target") or {}
            if lane.get("pr") is not None or local.get("source") != "card" or local.get("branch") != branch:
                continue
        grouped.setdefault(tag["panel_id"], []).append(lane)
    return grouped


def _status_result(**fields: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "verified": False,
        "mode": "none",
        "consensus": "none",
        "reason": "",
        "panel_id": None,
        "base_branch": None,
        "lanes": [],
    }
    base.update(fields)
    return base


def _load_local_verify_cards(
    conn: sqlite3.Connection, *, branch: str, tenant: Optional[str]
) -> tuple[list[dict[str, Any]], dict[str, list[dict[str, Any]]]]:
    where = ["assignee = ?", "status != 'archived'", "instr(body, ?) > 0"]
    params: list[Any] = [WORKER_PROFILE, f"- Local branch target: `{branch}` (base=`"]
    if tenant:
        where.append("tenant = ?")
        params.append(tenant)
    tasks = [
        dict(r)
        for r in conn.execute(
            f"SELECT rowid AS seq, id, title, status, tenant, skills, created_at, result, body FROM tasks "
            f"WHERE {' AND '.join(where)} ORDER BY created_at ASC, rowid ASC",
            params,
        ).fetchall()
    ]
    runs_by_task = load_runs_by_task(conn, [t["id"] for t in tasks]) if tasks else {}
    return tasks, runs_by_task


def local_branch_verification_status(
    db_path: Path,
    *,
    repo: str,
    branch: str,
    head_sha: str,
    tenant: Optional[str] = None,
    trail_db: Optional[Path] = None,
) -> dict[str, Any]:
    """Has the exact unpublished ``(repo, branch, head_sha)`` passed local verification? Read-only.

    Only Verification cards carrying the orchestrator-written ``Local branch target:`` stamp for that
    branch and full SHA (and ``- Repository:`` line for ``repo``) count. When a high-rigor panel exists
    its newest round decides: ``summarize_panel`` with the lane roster from ``spawn_verify`` must reach
    ``consensus: pass`` with ``merge_allowed`` and no missing lane (an unknown roster fails closed).
    Otherwise the standard pair needs a ``done`` ``verify-diff`` and a ``done`` ``verify-blackbox``
    card, every matching card ``done`` with an explicit ``pass`` verdict. Any rejecting or inconclusive
    standard card at that SHA refuses in both modes. Returns ``{verified, mode, consensus, reason,
    panel_id, base_branch, lanes}``; `kanban.db` is opened ``mode=ro`` and never created.
    """
    sha = head_sha.strip().lower() if isinstance(head_sha, str) else ""
    repo_key = repo.strip().lower() if isinstance(repo, str) else ""
    if not (repo_key and isinstance(branch, str) and branch.strip() and FULL_SHA_RE.fullmatch(sha)):
        return _status_result(reason="Invalid verification target: repo, branch and a full head SHA are required.")
    conn = open_kanban_ro(Path(db_path))
    if conn is None:
        return _status_result(reason="kanban.db not found: no local verification recorded.")
    try:
        with closing(conn):
            tasks, runs_by_task = _load_local_verify_cards(conn, branch=branch, tenant=tenant)
    except sqlite3.Error:
        return _status_result(reason="kanban.db unreadable: local verification cannot be confirmed.")

    matched: list[tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any]]] = []
    for task in tasks:
        phase = classify_phase(task)
        local = local_target_from_body(task.get("body"))
        if phase not in VERIFY_PHASES or local is None:
            continue
        if local["branch"] != branch or local["head_sha"] != sha or str(local.get("repo") or "").lower() != repo_key:
            continue
        runs = runs_by_task.get(task["id"], [])
        tag = parse_panel_tag(task.get("body"))
        lane = {
            "task_id": task["id"],
            "phase": phase,
            "status": str(task.get("status") or ""),
            "verdict": explicit_verify_verdict(runs),
            "panel_id": tag["panel_id"] if tag else None,
            "vendor": tag["vendor"] if tag else None,
        }
        matched.append((task, runs, {**lane, "_base": local["base_branch"]}))
    if not matched:
        return _status_result(reason=f"No local verification cards found for {branch} at {sha}.")

    lanes = [{k: v for k, v in lane.items() if k != "_base"} for _, _, lane in matched]
    bases = sorted({lane["_base"] for _, _, lane in matched})
    if len(bases) != 1:
        return _status_result(lanes=lanes, reason=f"Conflicting verified base branches: {', '.join(bases)}.")
    out = _status_result(lanes=lanes, base_branch=bases[0])
    standard = [lane for lane in lanes if not lane["panel_id"]]
    blockers = [lane for lane in standard if verdict_bucket(lane["verdict"]) in ("reject", "inconclusive")]
    blocker_note = (
        f"{len(blockers)} standard verification card(s) rejected or were inconclusive at this SHA: "
        + ", ".join(str(b["task_id"]) for b in blockers)
    ) if blockers else ""

    panels: dict[str, list[tuple[dict[str, Any], list[dict[str, Any]]]]] = {}
    for task, runs, lane in matched:
        if lane["panel_id"]:
            panels.setdefault(lane["panel_id"], []).append((task, runs))
    if panels:
        def _recency(pid: str) -> tuple[float, int]:
            return max((float(t.get("created_at") or 0), int(t.get("seq") or 0)) for t, _ in panels[pid])

        pid = max(panels, key=_recency)
        panel_lanes = [panel_lane(t, r, parse_panel_tag(t.get("body")) or {}) for t, r in panels[pid]]
        expected = expected_panel_lane_ids(pid, panel_lanes, db_path=trail_db)
        summary = summarize_panel(pid, panel_lanes, expected_lane_ids=expected)
        out.update(mode="panel", panel_id=pid, consensus=str(summary["consensus"]))
        if expected is None:
            out["reason"] = f"Panel {pid} has no recorded lane roster (spawn_verify); it cannot be confirmed complete."
        elif summary["consensus"] != "pass" or summary.get("merge_allowed") is not True or summary["missing_lane_ids"]:
            out["reason"] = f"Panel {pid} consensus is {summary['consensus']}: {summary.get('reason') or 'not passing'}"
        elif blockers:
            out["reason"] = blocker_note
        else:
            out.update(verified=True, reason=f"Panel {pid} passed with every expected lane at {sha}.")
        return out

    diff_ok = any(c["phase"] == "verify-diff" and c["status"] == "done" and c["verdict"] == "pass" for c in standard)
    bb_ok = any(c["phase"] == "verify-blackbox" and c["status"] == "done" and c["verdict"] == "pass" for c in standard)
    all_ok = all(c["status"] == "done" and c["verdict"] == "pass" for c in standard)
    out["mode"] = "standard"
    if blockers:
        rejected = any(verdict_bucket(b["verdict"]) == "reject" for b in blockers)
        out.update(consensus="reject" if rejected else "inconclusive", reason=blocker_note)
    elif diff_ok and bb_ok and all_ok:
        out.update(verified=True, consensus="pass", reason=f"verify-diff and verify-blackbox passed at {sha}.")
    else:
        missing = [p for p, ok in (("verify-diff", diff_ok), ("verify-blackbox", bb_ok)) if not ok]
        detail = f"missing a done explicit pass for {', '.join(missing)}" if missing else "a card is not done with an explicit pass"
        out.update(consensus="pending", reason=f"Local verification incomplete: {detail}.")
    return out


def classify_phase(task: dict[str, Any]) -> str:
    """Classify a Factonio Kanban card into its pipeline phase."""
    skills = parse_json_list(task.get("skills"))
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


def extract_pr_ref(task: dict[str, Any], runs: list[dict[str, Any]]) -> Optional[dict[str, Any]]:
    for r in reversed(runs):
        meta = parse_json_dict(r.get("metadata"))
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
    return pr_ref_from_text(task)


def pr_ref_from_text(task: dict[str, Any]) -> Optional[dict[str, Any]]:
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

def expected_panel_lane_ids(
    panel_id: str, lanes: list[dict[str, Any]], *, db_path: Optional[Path] = None
) -> Optional[list[str]]:
    """Every task id ``spawn_verification_pair`` recorded in `trail.db` for ``panel_id``; ``None`` if unknown."""
    tenant = next((lane.get("tenant") for lane in lanes if lane.get("tenant")), None)
    try:
        rows = query_decisions(tenant=tenant or None, phase="verify", limit=500, db_path=db_path)
    except (sqlite3.Error, OSError):
        return None
    for row in rows:
        ev = row.get("evidence") or {}
        if row.get("decision") != "spawn_verify" or ev.get("panel_id") != panel_id:
            continue
        ids = [
            str(lane[key])
            for lane in ev.get("panel_lanes") or []
            if isinstance(lane, dict)
            for key in ("verify_diff_task_id", "verify_blackbox_task_id")
            if lane.get(key)
        ]
        return ids or None
    return None
