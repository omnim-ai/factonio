"""Append-only SQLite decision trail (`<root HERMES_HOME>/plugin-data/factonio/trail.db`).

`trail.db` is Factonio's own SQLite home: `schema_meta` versions the store so future
metrics tables can be added without touching Hermes's `kanban.db`. The `decisions`
table is append-only, enforced by SQLite triggers; a correction is a new row whose
`supersedes_id` points at the row it replaces.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import uuid
from collections import Counter
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional

from .config import PLUGIN_ID, get_root_hermes_home_path

logger = logging.getLogger("factonio.trail")

SCHEMA_VERSION = "1"
DEFAULT_LIMIT = 100
MAX_LIMIT = 1000

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS decisions (
    id TEXT PRIMARY KEY,
    ts TEXT NOT NULL,
    tenant TEXT NOT NULL DEFAULT '',
    repo_slug TEXT NOT NULL DEFAULT '',
    phase TEXT NOT NULL DEFAULT '',
    card_id TEXT NOT NULL DEFAULT '',
    actor TEXT NOT NULL DEFAULT 'orchestrator',
    decision TEXT NOT NULL,
    why TEXT NOT NULL DEFAULT '',
    evidence_json TEXT NOT NULL DEFAULT '{}',
    result TEXT NOT NULL DEFAULT '',
    supersedes_id TEXT NULL REFERENCES decisions(id)
);

CREATE INDEX IF NOT EXISTS idx_decisions_tenant_ts ON decisions(tenant, ts);
CREATE INDEX IF NOT EXISTS idx_decisions_repo_ts ON decisions(repo_slug, ts);
CREATE INDEX IF NOT EXISTS idx_decisions_card ON decisions(card_id);
CREATE INDEX IF NOT EXISTS idx_decisions_supersedes ON decisions(supersedes_id);

CREATE TRIGGER IF NOT EXISTS trg_decisions_no_update
BEFORE UPDATE ON decisions
BEGIN
    SELECT RAISE(ABORT, 'decisions is append-only: UPDATE is not allowed (log a superseding row instead)');
END;

CREATE TRIGGER IF NOT EXISTS trg_decisions_no_delete
BEFORE DELETE ON decisions
BEGIN
    SELECT RAISE(ABORT, 'decisions is append-only: DELETE is not allowed');
END;
"""

_COLUMNS = (
    "id",
    "ts",
    "tenant",
    "repo_slug",
    "phase",
    "card_id",
    "actor",
    "decision",
    "why",
    "evidence_json",
    "result",
    "supersedes_id",
)


def trail_db_path() -> Path:
    """Location of the decision trail store (always under the root HERMES_HOME)."""
    return get_root_hermes_home_path() / "plugin-data" / PLUGIN_ID / "trail.db"


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _connect(db_path: Optional[Path] = None) -> sqlite3.Connection:
    """Open (creating if needed) the trail store with WAL, busy timeout, and FK enforcement."""
    path = Path(db_path) if db_path is not None else trail_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=5.0)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.executescript(_SCHEMA_SQL)
        conn.execute(
            "INSERT OR IGNORE INTO schema_meta(key, value, updated_at) VALUES ('schema_version', ?, ?)",
            (SCHEMA_VERSION, _now_iso()),
        )
        conn.commit()
    except Exception:
        conn.close()
        raise
    return conn


def _resolve_repo_slug(repo: Optional[str]) -> str:
    """Map a registered `owner/repo` (or slug) to its slug; fall back to the raw value."""
    raw = str(repo or "").strip()
    if not raw:
        return ""
    try:
        from .config import get_repo

        entry = get_repo(raw)
        if entry and entry.get("slug"):
            return str(entry["slug"])
    except Exception:
        pass
    return raw


def _clamp_limit(limit: Any) -> int:
    try:
        value = int(limit)
    except (TypeError, ValueError):
        value = DEFAULT_LIMIT
    return max(1, min(MAX_LIMIT, value))


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    """Public row shape; key order follows the documented `/trail` API contract."""
    try:
        evidence = json.loads(row["evidence_json"] or "{}")
    except ValueError:
        evidence = {}
    out: dict[str, Any] = {}
    for key in _COLUMNS:
        if key == "evidence_json":
            out["evidence"] = evidence if isinstance(evidence, dict) else {"value": evidence}
        else:
            out[key] = row[key]
    return out


def _superseded_by_map(conn: sqlite3.Connection, ids: Iterable[str]) -> dict[str, list[str]]:
    id_list = [i for i in ids if i]
    mapping: dict[str, list[str]] = {i: [] for i in id_list}
    if not id_list:
        return mapping
    for start in range(0, len(id_list), 500):
        chunk = id_list[start : start + 500]
        placeholders = ",".join("?" for _ in chunk)
        for r in conn.execute(
            f"SELECT id, supersedes_id FROM decisions WHERE supersedes_id IN ({placeholders}) ORDER BY ts ASC, rowid ASC",
            chunk,
        ).fetchall():
            mapping.setdefault(r["supersedes_id"], []).append(r["id"])
    return mapping


def _enrich(conn: sqlite3.Connection, rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
    items = [_row_to_dict(r) for r in rows]
    sup = _superseded_by_map(conn, (d["id"] for d in items))
    for d in items:
        d["superseded_by"] = sup.get(d["id"], [])
        d["is_superseded"] = bool(d["superseded_by"])
    return items


def log_decision(
    decision: str,
    *,
    tenant: Optional[str] = None,
    repo_slug: Optional[str] = None,
    phase: Optional[str] = None,
    card_id: Optional[str] = None,
    actor: Optional[str] = None,
    why: Optional[str] = None,
    evidence: Optional[Mapping[str, Any]] = None,
    result: Optional[str] = None,
    supersedes_id: Optional[str] = None,
    db_path: Optional[Path] = None,
) -> dict[str, Any]:
    """Append one decision row and return it (enriched like `query_decisions`).

    Raises ``ValueError`` for an empty decision, non-object evidence, or a
    ``supersedes_id`` that does not reference an existing row.
    """
    text = str(decision or "").strip()
    if not text:
        raise ValueError("decision must be a non-empty string")
    if evidence is None:
        evidence_obj: dict[str, Any] = {}
    elif isinstance(evidence, Mapping):
        evidence_obj = dict(evidence)
    else:
        raise ValueError("evidence must be a JSON object (mapping)")
    evidence_json = json.dumps(evidence_obj, sort_keys=True, default=str)
    sup = str(supersedes_id).strip() if supersedes_id else None

    with closing(_connect(db_path)) as conn:
        if sup is not None:
            if conn.execute("SELECT 1 FROM decisions WHERE id = ?", (sup,)).fetchone() is None:
                raise ValueError(f"supersedes_id {sup!r} does not reference an existing decision")
        row_id = f"d_{uuid.uuid4().hex[:12]}"
        conn.execute(
            """
            INSERT INTO decisions(id, ts, tenant, repo_slug, phase, card_id, actor, decision, why, evidence_json, result, supersedes_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row_id,
                _now_iso(),
                str(tenant or "").strip(),
                _resolve_repo_slug(repo_slug),
                str(phase or "").strip(),
                str(card_id or "").strip(),
                str(actor or "").strip() or "orchestrator",
                text,
                str(why or "").strip(),
                evidence_json,
                str(result or "").strip(),
                sup,
            ),
        )
        conn.commit()
        stored = conn.execute("SELECT * FROM decisions WHERE id = ?", (row_id,)).fetchone()
        return _enrich(conn, [stored])[0]


def record_transition(decision: str, **kwargs: Any) -> Optional[dict[str, Any]]:
    """Best-effort automatic transition logging: a trail failure never breaks the factory."""
    try:
        return log_decision(decision, **kwargs)
    except Exception as exc:
        logger.warning("factonio trail: failed to record %s: %s", decision, exc)
        return None


def _filter_sql(
    *,
    tenant: Optional[str],
    repo: Optional[str],
    phase: Optional[str],
    card_id: Optional[str],
    since: Optional[str],
    alias: str = "",
) -> tuple[str, list[Any]]:
    a = f"{alias}." if alias else ""
    where: list[str] = []
    params: list[Any] = []
    if tenant:
        where.append(f"{a}tenant = ?")
        params.append(str(tenant).strip())
    if repo:
        raw = str(repo).strip()
        slug = _resolve_repo_slug(raw)
        where.append(f"{a}repo_slug IN (?, ?)")
        params.extend([slug, raw])
    if phase:
        where.append(f"{a}phase = ?")
        params.append(str(phase).strip())
    if card_id:
        where.append(f"{a}card_id = ?")
        params.append(str(card_id).strip())
    if since:
        where.append(f"{a}ts >= ?")
        params.append(since)
    return (" WHERE " + " AND ".join(where)) if where else "", params


def query_decisions(
    *,
    tenant: Optional[str] = None,
    repo: Optional[str] = None,
    phase: Optional[str] = None,
    card_id: Optional[str] = None,
    limit: Any = DEFAULT_LIMIT,
    since: Optional[str] = None,
    db_path: Optional[Path] = None,
) -> list[dict[str, Any]]:
    """Return matching decisions newest-first, each with `evidence`, `superseded_by`, `is_superseded`."""
    path = Path(db_path) if db_path is not None else trail_db_path()
    if not path.is_file():
        return []
    where_sql, params = _filter_sql(tenant=tenant, repo=repo, phase=phase, card_id=card_id, since=since)
    with closing(_connect(path)) as conn:
        rows = conn.execute(
            f"SELECT * FROM decisions{where_sql} ORDER BY ts DESC, rowid DESC LIMIT ?",
            [*params, _clamp_limit(limit)],
        ).fetchall()
        return _enrich(conn, rows)


def _empty_summary() -> dict[str, Any]:
    return {
        "total": 0,
        "active": 0,
        "superseded": 0,
        "by_phase": {},
        "by_actor": {},
        "by_tenant": {},
        "latest_ts": None,
    }


def summarize_trail(
    *,
    tenant: Optional[str] = None,
    repo: Optional[str] = None,
    phase: Optional[str] = None,
    card_id: Optional[str] = None,
    since: Optional[str] = None,
    db_path: Optional[Path] = None,
) -> dict[str, Any]:
    """Aggregate counts over every matching decision (not truncated by any display limit)."""
    path = Path(db_path) if db_path is not None else trail_db_path()
    if not path.is_file():
        return _empty_summary()
    where_sql, params = _filter_sql(tenant=tenant, repo=repo, phase=phase, card_id=card_id, since=since, alias="d")
    with closing(_connect(path)) as conn:
        rows = conn.execute(
            f"""
            SELECT d.ts, d.tenant, d.phase, d.actor,
                   EXISTS(SELECT 1 FROM decisions s WHERE s.supersedes_id = d.id) AS superseded
              FROM decisions d{where_sql}
            """,
            params,
        ).fetchall()
    if not rows:
        return _empty_summary()
    superseded = sum(1 for r in rows if r["superseded"])
    return {
        "total": len(rows),
        "active": len(rows) - superseded,
        "superseded": superseded,
        "by_phase": dict(sorted(Counter(r["phase"] or "unknown" for r in rows).items())),
        "by_actor": dict(sorted(Counter(r["actor"] or "unknown" for r in rows).items())),
        "by_tenant": dict(sorted(Counter(r["tenant"] or "unknown" for r in rows).items())),
        "latest_ts": max(r["ts"] for r in rows),
    }


def trail_payload(
    *,
    tenant: Optional[str] = None,
    repo: Optional[str] = None,
    phase: Optional[str] = None,
    card_id: Optional[str] = None,
    limit: Any = DEFAULT_LIMIT,
    db_path: Optional[Path] = None,
) -> dict[str, Any]:
    """The `{decisions, summary, filters}` contract shared by the REST API and `--json` CLI."""
    clamped = _clamp_limit(limit)
    filters = {
        "tenant": tenant or None,
        "repo": repo or None,
        "phase": phase or None,
        "card_id": card_id or None,
        "limit": clamped,
    }
    query = {"tenant": tenant, "repo": repo, "phase": phase, "card_id": card_id, "db_path": db_path}
    return {
        "decisions": query_decisions(limit=clamped, **query),
        "summary": summarize_trail(**query),
        "filters": filters,
    }


def _evidence_inline(evidence: Mapping[str, Any]) -> str:
    parts = []
    for key, value in evidence.items():
        if isinstance(value, (dict, list)):
            value = json.dumps(value, sort_keys=True, default=str)
        parts.append(f"{key}={value}")
    return ", ".join(parts)


def _lineage(row: Mapping[str, Any]) -> str:
    bits = []
    if row.get("supersedes_id"):
        bits.append(f"supersedes {row['supersedes_id']}")
    if row.get("superseded_by"):
        bits.append(f"superseded by {', '.join(row['superseded_by'])}")
    return "; ".join(bits)


def render_trail_text(rows: list[Mapping[str, Any]]) -> str:
    """Plain-text, one-decision-per-block rendering for terminals."""
    if not rows:
        return "No decisions recorded."
    lines: list[str] = []
    for r in rows:
        marker = "~" if r.get("is_superseded") else "*"
        scope = " ".join(
            x for x in (r.get("tenant") or "", f"[{r['phase']}]" if r.get("phase") else "", r.get("card_id") or "") if x
        )
        result = f" -> {r['result']}" if r.get("result") else ""
        lines.append(f"{marker} {r.get('ts')} {r.get('id')} {scope} {r.get('actor')}: {r.get('decision')}{result}".rstrip())
        if r.get("why"):
            lines.append(f"    why: {r['why']}")
        if r.get("evidence"):
            lines.append(f"    evidence: {_evidence_inline(r['evidence'])}")
        lineage = _lineage(r)
        if lineage:
            lines.append(f"    lineage: {lineage}")
    return "\n".join(lines)


def _md_cell(value: Any) -> str:
    return str(value if value is not None else "").replace("|", "\\|").replace("\n", " ").strip()


def render_trail_markdown(rows: list[Mapping[str, Any]]) -> str:
    """Markdown table rendering (show-me-your-work style) for PR bodies, retros, and handoffs."""
    header = "## Decision Trail\n\n"
    if not rows:
        return header + "_No decisions recorded._\n"
    out = [
        "| Time (UTC) | Tenant | Phase | Card | Actor | Decision | Why | Evidence | Result | Lineage |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        decision = _md_cell(r.get("decision"))
        if r.get("is_superseded"):
            decision = f"~~{decision}~~"
        out.append(
            "| "
            + " | ".join(
                [
                    _md_cell(r.get("ts")),
                    _md_cell(r.get("tenant")),
                    _md_cell(r.get("phase")),
                    f"`{_md_cell(r['card_id'])}`" if r.get("card_id") else "",
                    _md_cell(r.get("actor")),
                    decision,
                    _md_cell(r.get("why")),
                    _md_cell(_evidence_inline(r.get("evidence") or {})),
                    _md_cell(r.get("result")),
                    _md_cell(_lineage(r)),
                ]
            )
            + " |"
        )
    return header + "\n".join(out) + "\n"
