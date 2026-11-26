"""Append-only SQLite decision trail (`<root HERMES_HOME>/plugin-data/factonio/trail.db`).

`trail.db` is Factonio's own SQLite home: `schema_meta` versions the store so metrics
tables live here without touching Hermes's `kanban.db`. The `decisions` table is
append-only, enforced by SQLite triggers; a correction is a new row whose
`supersedes_id` points at the row it replaces. Schema v2 adds the equally append-only
`metric_snapshots` time series, so factory KPIs survive `kanban.db` archival or reset.
"""

from __future__ import annotations

import json
import logging
import random
import re
import sqlite3
import threading
import time
import uuid
from collections import Counter
from contextlib import closing, contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Mapping, Optional, TypeVar

try:
    import fcntl
except ImportError:  # non-POSIX: fall back to the in-process lock plus retry
    fcntl = None  # type: ignore[assignment]

from .config import PLUGIN_ID, get_root_hermes_home_path

logger = logging.getLogger("factonio.trail")

SCHEMA_VERSION = "2"
DEFAULT_LIMIT = 100
MAX_LIMIT = 1000

_BUSY_TIMEOUT_MS = 5000
_RETRY_ATTEMPTS = 8
_RETRY_BASE_DELAY = 0.02
_RETRY_MAX_DELAY = 0.5
_INIT_LOCK = threading.Lock()
_T = TypeVar("_T")

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

_SNAPSHOT_INT_COLUMNS = (
    "workstreams_total",
    "cards_total",
    "cards_done",
    "cards_active",
    "cards_blocked",
    "diff_pass",
    "diff_reject",
    "diff_inconclusive",
    "blackbox_pass",
    "blackbox_reject",
    "blackbox_inconclusive",
    "escape_count",
    "verified_total",
    "rework_cards",
    "panels_total",
    "panels_pass",
    "panels_reject",
    "panels_inconclusive",
    "panels_pending",
    "panels_disagreements",
    "trail_decisions_total",
)
_SNAPSHOT_REAL_COLUMNS = (
    "diff_pass_rate",
    "blackbox_pass_rate",
    "first_pass_rate",
    "escape_rate",
    "avg_runs_per_implement_card",
    "median_define_s",
    "median_design_s",
    "median_implement_s",
    "median_verify_diff_s",
    "median_verify_blackbox_s",
)
# Physical column order of `metric_snapshots` (and the key order of returned snapshot rows).
SNAPSHOT_COLUMNS = (
    "id",
    "ts",
    "source",
    "range_key",
    "repo_slug",
    "tenant",
    "workstreams_total",
    "cards_total",
    "cards_done",
    "cards_active",
    "cards_blocked",
    "diff_pass",
    "diff_reject",
    "diff_inconclusive",
    "diff_pass_rate",
    "blackbox_pass",
    "blackbox_reject",
    "blackbox_inconclusive",
    "blackbox_pass_rate",
    "first_pass_rate",
    "escape_count",
    "verified_total",
    "escape_rate",
    "rework_cards",
    "avg_runs_per_implement_card",
    "median_define_s",
    "median_design_s",
    "median_implement_s",
    "median_verify_diff_s",
    "median_verify_blackbox_s",
    "panels_total",
    "panels_pass",
    "panels_reject",
    "panels_inconclusive",
    "panels_pending",
    "panels_disagreements",
    "trail_decisions_total",
    "payload_json",
)
SNAPSHOT_KPI_COLUMNS = tuple(c for c in SNAPSHOT_COLUMNS if c in _SNAPSHOT_INT_COLUMNS or c in _SNAPSHOT_REAL_COLUMNS)


def _snapshot_column_ddl(name: str) -> str:
    if name == "id":
        return "id TEXT PRIMARY KEY"
    if name == "ts":
        return "ts TEXT NOT NULL"
    if name == "source":
        return "source TEXT NOT NULL DEFAULT 'manual'"
    if name == "range_key":
        return "range_key TEXT NOT NULL DEFAULT 'all'"
    if name in ("repo_slug", "tenant"):
        return f"{name} TEXT NOT NULL DEFAULT ''"
    if name == "payload_json":
        return "payload_json TEXT NOT NULL DEFAULT '{}'"
    if name in _SNAPSHOT_INT_COLUMNS:
        return f"{name} INTEGER NOT NULL DEFAULT 0"
    return f"{name} REAL NULL"


# Applied in one transaction by the v1 -> v2 migration; every statement is idempotent.
_MIGRATION_V2_STATEMENTS = (
    "CREATE TABLE IF NOT EXISTS metric_snapshots (\n    "
    + ",\n    ".join(_snapshot_column_ddl(c) for c in SNAPSHOT_COLUMNS)
    + "\n)",
    "CREATE INDEX IF NOT EXISTS idx_metric_snapshots_ts ON metric_snapshots(ts)",
    "CREATE INDEX IF NOT EXISTS idx_metric_snapshots_repo_ts ON metric_snapshots(repo_slug, ts)",
    "CREATE INDEX IF NOT EXISTS idx_metric_snapshots_tenant_ts ON metric_snapshots(tenant, ts)",
    "CREATE INDEX IF NOT EXISTS idx_metric_snapshots_source_ts ON metric_snapshots(source, ts)",
    """CREATE TRIGGER IF NOT EXISTS trg_metric_snapshots_no_update
BEFORE UPDATE ON metric_snapshots
BEGIN
    SELECT RAISE(ABORT, 'metric_snapshots is append-only: UPDATE is not allowed');
END""",
    """CREATE TRIGGER IF NOT EXISTS trg_metric_snapshots_no_delete
BEFORE DELETE ON metric_snapshots
BEGIN
    SELECT RAISE(ABORT, 'metric_snapshots is append-only: DELETE is not allowed');
END""",
)
_MIGRATION_V2_SQL = ";\n\n".join(_MIGRATION_V2_STATEMENTS) + ";\n"

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


def _is_transient(exc: BaseException) -> bool:
    """SQLITE_BUSY / SQLITE_LOCKED surface as OperationalError; corruption never does."""
    if not isinstance(exc, sqlite3.OperationalError):
        return False
    msg = str(exc).lower()
    return "locked" in msg or "busy" in msg


def _with_retry(op: Callable[[], _T]) -> _T:
    """Run ``op``, retrying transient lock contention with jittered exponential backoff."""
    delay = _RETRY_BASE_DELAY
    for attempt in range(_RETRY_ATTEMPTS):
        try:
            return op()
        except sqlite3.OperationalError as exc:
            if not _is_transient(exc) or attempt == _RETRY_ATTEMPTS - 1:
                raise
            time.sleep(delay * (0.5 + random.random()))
            delay = min(delay * 2, _RETRY_MAX_DELAY)
    raise AssertionError("unreachable")


@contextmanager
def _init_guard(path: Path) -> Iterator[None]:
    """Serialize WAL switch + schema creation across threads and (on POSIX) processes.

    Switching a fresh file to WAL needs an exclusive lock and SQLite does not always
    invoke the busy handler for it, so racing first writers would otherwise fail fast.
    """
    with _INIT_LOCK:
        if fcntl is None:
            yield
            return
        with open(path.with_name(path.name + ".lock"), "a") as fh:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def _schema_version(conn: sqlite3.Connection) -> int:
    """Stored `schema_meta.schema_version` as an integer; missing or unparseable is ``0``."""
    row = conn.execute("SELECT value FROM schema_meta WHERE key = 'schema_version'").fetchone()
    try:
        return int(str(row[0]).strip()) if row is not None else 0
    except (TypeError, ValueError):
        return 0


def _migrate(conn: sqlite3.Connection) -> None:
    """Bring the store up to `SCHEMA_VERSION` (v1 -> v2) atomically; never downgrade a newer store."""
    target = int(SCHEMA_VERSION)
    if _schema_version(conn) >= target:
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        # Re-read under the write lock: another (non-flock) writer may have migrated meanwhile.
        if _schema_version(conn) < target:
            for stmt in _MIGRATION_V2_STATEMENTS:
                conn.execute(stmt)
            conn.execute(
                """
                INSERT INTO schema_meta(key, value, updated_at) VALUES ('schema_version', ?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
                """,
                (SCHEMA_VERSION, _now_iso()),
            )
        conn.commit()
    except BaseException:
        conn.rollback()
        raise


def _open(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), timeout=_BUSY_TIMEOUT_MS / 1000)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
        conn.execute("PRAGMA foreign_keys=ON")
        with _init_guard(path):
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(_SCHEMA_SQL)
            _migrate(conn)
    except BaseException:
        conn.close()
        raise
    return conn


def _connect(db_path: Optional[Path] = None) -> sqlite3.Connection:
    """Open (creating if needed) the trail store with WAL, busy timeout, and FK enforcement."""
    path = Path(db_path) if db_path is not None else trail_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    return _with_retry(lambda: _open(path))


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

    row_id = f"d_{uuid.uuid4().hex[:12]}"
    values = (
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
    )

    def _append() -> dict[str, Any]:
        with closing(_connect(db_path)) as conn:
            if sup is not None:
                if conn.execute("SELECT 1 FROM decisions WHERE id = ?", (sup,)).fetchone() is None:
                    raise ValueError(f"supersedes_id {sup!r} does not reference an existing decision")
            # Idempotent under retry: a commit that landed before a transient error is not duplicated.
            if conn.execute("SELECT 1 FROM decisions WHERE id = ?", (row_id,)).fetchone() is None:
                conn.execute(
                    """
                    INSERT INTO decisions(id, ts, tenant, repo_slug, phase, card_id, actor, decision, why, evidence_json, result, supersedes_id)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    values,
                )
                conn.commit()
            stored = conn.execute("SELECT * FROM decisions WHERE id = ?", (row_id,)).fetchone()
            return _enrich(conn, [stored])[0]

    return _with_retry(_append)


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


def get_decision(decision_id: str, *, db_path: Optional[Path] = None) -> Optional[dict[str, Any]]:
    """One decision by id (enriched like `query_decisions`), or ``None`` when absent."""
    key = str(decision_id or "").strip()
    path = Path(db_path) if db_path is not None else trail_db_path()
    if not key or not path.is_file():
        return None
    with closing(_connect(path)) as conn:
        row = conn.execute("SELECT * FROM decisions WHERE id = ?", (key,)).fetchone()
        return _enrich(conn, [row])[0] if row is not None else None


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


# --- metric_snapshots (schema v2): append-only long-term factory KPI time series -------------

_SINCE_REL_RE = re.compile(r"^(\d{1,5})d$")
_SNAPSHOT_TEXT_MAX = 200


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _parse_iso(value: str) -> datetime:
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    dt = datetime.fromisoformat(text)
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def resolve_since(since: Any, *, now: Optional[datetime] = None) -> Optional[str]:
    """Resolve ``since`` (``7d``/``30d``/``90d``/``<N>d``, ``all``, or ISO-8601) to a UTC `ts` bound.

    ``None``, ``""`` and ``all`` mean unbounded (``None``). Raises ``ValueError`` otherwise.
    """
    if since is None:
        return None
    text = str(since).strip()
    if not text or text.lower() == "all":
        return None
    rel = _SINCE_REL_RE.match(text.lower())
    if rel:
        base = now if now is not None else datetime.now(timezone.utc)
        return _iso(base - timedelta(days=int(rel.group(1))))
    try:
        return _iso(_parse_iso(text))
    except ValueError:
        raise ValueError(
            f"Invalid since {since!r}: expected 7d, 30d, 90d, all, or an ISO-8601 timestamp"
        ) from None


def _num(value: Any, kind: type) -> Any:
    if value is None or isinstance(value, bool):
        return None
    try:
        return kind(value)
    except (TypeError, ValueError):
        return None


def flatten_metrics(metrics: Mapping[str, Any]) -> dict[str, Any]:
    """Map a `compute_metrics` result onto the typed `metric_snapshots` KPI columns.

    Top-level keys that already name a KPI column take precedence, so callers may pass
    pre-flattened values. Missing counts are ``0``; missing rates/durations are ``None``.
    """
    m = metrics if isinstance(metrics, Mapping) else {}

    def sub(*keys: str) -> Mapping[str, Any]:
        node: Any = m
        for key in keys:
            node = node.get(key) if isinstance(node, Mapping) else None
        return node if isinstance(node, Mapping) else {}

    totals, rework, escapes = sub("totals"), sub("rework"), sub("escapes")
    diff, bb = sub("verification", "diff"), sub("verification", "blackbox")
    panels, trail_block = sub("verification_panels"), sub("trail")
    raw: dict[str, Any] = {
        "workstreams_total": totals.get("workstreams"),
        "cards_total": totals.get("cards"),
        "cards_done": totals.get("done"),
        "cards_active": totals.get("running"),
        "cards_blocked": totals.get("blocked"),
        "diff_pass": diff.get("pass"),
        "diff_reject": diff.get("reject"),
        "diff_inconclusive": diff.get("inconclusive"),
        "diff_pass_rate": diff.get("pass_rate"),
        "blackbox_pass": bb.get("pass"),
        "blackbox_reject": bb.get("reject"),
        "blackbox_inconclusive": bb.get("inconclusive"),
        "blackbox_pass_rate": bb.get("pass_rate"),
        "first_pass_rate": rework.get("first_pass_rate"),
        "escape_count": escapes.get("count"),
        "verified_total": escapes.get("verified_total"),
        "escape_rate": escapes.get("escape_rate"),
        "rework_cards": rework.get("cards_with_rework"),
        "avg_runs_per_implement_card": rework.get("avg_runs_per_implement_card"),
        "panels_total": panels.get("total"),
        "panels_pass": panels.get("pass"),
        "panels_reject": panels.get("reject"),
        "panels_inconclusive": panels.get("inconclusive"),
        "panels_pending": panels.get("pending"),
        "panels_disagreements": panels.get("disagreements"),
        "trail_decisions_total": trail_block.get("decision_count"),
    }
    for phase in ("define", "design", "implement", "verify-diff", "verify-blackbox"):
        raw[f"median_{phase.replace('-', '_')}_s"] = sub("phases", phase).get("median_seconds")
    for key in SNAPSHOT_KPI_COLUMNS:
        if key in m:
            raw[key] = m[key]
    out: dict[str, Any] = {}
    for key in SNAPSHOT_KPI_COLUMNS:
        if key in _SNAPSHOT_INT_COLUMNS:
            out[key] = max(0, _num(raw.get(key), int) or 0)
        else:
            out[key] = _num(raw.get(key), float)
    return out


def _snapshot_to_dict(row: sqlite3.Row, *, include_payload: bool = True) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key in SNAPSHOT_COLUMNS:
        if key != "payload_json":
            out[key] = row[key]
    if include_payload:
        try:
            payload = json.loads(row["payload_json"] or "{}")
        except ValueError:
            payload = {}
        out["payload"] = payload if isinstance(payload, dict) else {"value": payload}
    return out


def _clean_text(value: Any, default: str = "") -> str:
    return (str(value).strip() if value is not None else "")[:_SNAPSHOT_TEXT_MAX] or default


def record_metric_snapshot(
    metrics: Optional[Mapping[str, Any]] = None,
    *,
    source: str = "manual",
    range_key: Optional[str] = None,
    repo_slug: Optional[str] = None,
    tenant: Optional[str] = None,
    extra: Optional[Mapping[str, Any]] = None,
    ts: Optional[str] = None,
    db_path: Optional[Path] = None,
) -> dict[str, Any]:
    """Append one point-in-time KPI snapshot and return it (with parsed ``payload``).

    ``metrics`` is a `compute_metrics` result (stored whole in ``payload_json.metrics``);
    ``extra`` is free-form trigger context (``payload_json.extra``). ``ts`` (ISO-8601) defaults
    to now. Raises ``ValueError`` for non-mapping input or an invalid ``ts``.
    """
    if metrics is not None and not isinstance(metrics, Mapping):
        raise ValueError("metrics must be a mapping (a compute_metrics result)")
    if extra is not None and not isinstance(extra, Mapping):
        raise ValueError("extra must be a JSON object (mapping)")
    metrics_obj = dict(metrics or {})
    kpis = flatten_metrics(metrics_obj)
    stamp = _iso(_parse_iso(ts)) if ts else _now_iso()
    rk = _clean_text(range_key if range_key is not None else metrics_obj.get("range"), "all")
    payload_json = json.dumps({"metrics": metrics_obj, "extra": dict(extra or {})}, sort_keys=True, default=str)
    row_id = f"ms_{uuid.uuid4().hex[:12]}"
    values = {
        "id": row_id,
        "ts": stamp,
        "source": _clean_text(source, "manual"),
        "range_key": rk,
        "repo_slug": _resolve_repo_slug(repo_slug),
        "tenant": _clean_text(tenant),
        **kpis,
        "payload_json": payload_json,
    }
    cols = ", ".join(SNAPSHOT_COLUMNS)
    marks = ", ".join("?" for _ in SNAPSHOT_COLUMNS)

    def _append() -> dict[str, Any]:
        with closing(_connect(db_path)) as conn:
            # Idempotent under retry: a commit that landed before a transient error is not duplicated.
            if conn.execute("SELECT 1 FROM metric_snapshots WHERE id = ?", (row_id,)).fetchone() is None:
                conn.execute(
                    f"INSERT INTO metric_snapshots({cols}) VALUES ({marks})",
                    [values[c] for c in SNAPSHOT_COLUMNS],
                )
                conn.commit()
            stored = conn.execute("SELECT * FROM metric_snapshots WHERE id = ?", (row_id,)).fetchone()
            return _snapshot_to_dict(stored)

    return _with_retry(_append)


def _snapshot_filter_sql(
    *, tenant: Optional[str], repo: Optional[str], source: Optional[str], since_ts: Optional[str]
) -> tuple[str, list[Any]]:
    where: list[str] = []
    params: list[Any] = []
    if tenant:
        where.append("tenant = ?")
        params.append(str(tenant).strip())
    if repo:
        raw = str(repo).strip()
        where.append("repo_slug IN (?, ?)")
        params.extend([_resolve_repo_slug(raw), raw])
    if source:
        where.append("source = ?")
        params.append(str(source).strip())
    if since_ts:
        where.append("ts >= ?")
        params.append(since_ts)
    return (" WHERE " + " AND ".join(where)) if where else "", params


def _snapshot_store(db_path: Optional[Path]) -> Optional[Path]:
    path = Path(db_path) if db_path is not None else trail_db_path()
    return path if path.is_file() else None


def query_metric_snapshots(
    *,
    tenant: Optional[str] = None,
    repo: Optional[str] = None,
    source: Optional[str] = None,
    since: Any = None,
    limit: Any = DEFAULT_LIMIT,
    order: str = "asc",
    include_payload: bool = True,
    db_path: Optional[Path] = None,
) -> list[dict[str, Any]]:
    """The latest ``limit`` matching snapshots, oldest -> newest (``order="desc"`` for newest first).

    Returns ``[]`` when `trail.db` does not exist; never creates it. Raises ``ValueError`` on
    an invalid ``since`` or ``order``.
    """
    if order not in ("asc", "desc"):
        raise ValueError("order must be 'asc' or 'desc'")
    since_ts = resolve_since(since)
    path = _snapshot_store(db_path)
    if path is None:
        return []
    where_sql, params = _snapshot_filter_sql(tenant=tenant, repo=repo, source=source, since_ts=since_ts)
    with closing(_connect(path)) as conn:
        rows = conn.execute(
            f"SELECT * FROM metric_snapshots{where_sql} ORDER BY ts DESC, rowid DESC LIMIT ?",
            [*params, _clamp_limit(limit)],
        ).fetchall()
    items = [_snapshot_to_dict(r, include_payload=include_payload) for r in rows]
    return list(reversed(items)) if order == "asc" else items


def _empty_snapshot_summary() -> dict[str, Any]:
    return {
        "total": 0,
        "first_ts": None,
        "latest_ts": None,
        "by_source": {},
        "by_repo": {},
        "by_tenant": {},
        "latest": None,
    }


def summarize_metric_snapshots(
    *,
    tenant: Optional[str] = None,
    repo: Optional[str] = None,
    source: Optional[str] = None,
    since: Any = None,
    db_path: Optional[Path] = None,
) -> dict[str, Any]:
    """Aggregate over every matching snapshot (not truncated by a display limit).

    ``latest`` is the newest matching snapshot's KPI row (without ``payload``).
    """
    since_ts = resolve_since(since)
    path = _snapshot_store(db_path)
    if path is None:
        return _empty_snapshot_summary()
    where_sql, params = _snapshot_filter_sql(tenant=tenant, repo=repo, source=source, since_ts=since_ts)
    with closing(_connect(path)) as conn:
        rows = conn.execute(
            f"SELECT ts, source, repo_slug, tenant FROM metric_snapshots{where_sql}", params
        ).fetchall()
        if not rows:
            return _empty_snapshot_summary()
        latest = conn.execute(
            f"SELECT * FROM metric_snapshots{where_sql} ORDER BY ts DESC, rowid DESC LIMIT 1", params
        ).fetchone()
    return {
        "total": len(rows),
        "first_ts": min(r["ts"] for r in rows),
        "latest_ts": max(r["ts"] for r in rows),
        "by_source": dict(sorted(Counter(r["source"] or "unknown" for r in rows).items())),
        "by_repo": dict(sorted(Counter(r["repo_slug"] or "unscoped" for r in rows).items())),
        "by_tenant": dict(sorted(Counter(r["tenant"] or "unscoped" for r in rows).items())),
        "latest": _snapshot_to_dict(latest, include_payload=False) if latest is not None else None,
    }


def metrics_history_payload(
    *,
    tenant: Optional[str] = None,
    repo: Optional[str] = None,
    source: Optional[str] = None,
    since: Any = None,
    limit: Any = DEFAULT_LIMIT,
    include_payload: bool = True,
    db_path: Optional[Path] = None,
) -> dict[str, Any]:
    """The `{snapshots, summary, filters}` contract shared by `GET /metrics/history` and the CLI."""
    clamped = _clamp_limit(limit)
    since_ts = resolve_since(since)
    filters = {
        "tenant": tenant or None,
        "repo": repo or None,
        "source": source or None,
        "since": (str(since).strip() or None) if since is not None else None,
        "since_ts": since_ts,
        "limit": clamped,
    }
    query = {"tenant": tenant, "repo": repo, "source": source, "since": since_ts, "db_path": db_path}
    return {
        "snapshots": query_metric_snapshots(limit=clamped, include_payload=include_payload, **query),
        "summary": summarize_metric_snapshots(**query),
        "filters": filters,
    }


def _fmt_rate(value: Any) -> str:
    return "-" if value is None else f"{float(value) * 100:.0f}%"


def render_metric_snapshots_text(rows: list[Mapping[str, Any]]) -> str:
    """One line per snapshot (oldest first) for terminals."""
    if not rows:
        return "No metric snapshots recorded."
    lines = []
    for r in rows:
        scope = " ".join(x for x in (r.get("repo_slug") or "", r.get("tenant") or "") if x) or "-"
        lines.append(
            f"{r.get('ts')} {r.get('id')} [{r.get('source')}] {scope}: "
            f"workstreams={r.get('workstreams_total')} done={r.get('cards_done')}/{r.get('cards_total')} "
            f"diff={_fmt_rate(r.get('diff_pass_rate'))} blackbox={_fmt_rate(r.get('blackbox_pass_rate'))} "
            f"first_pass={_fmt_rate(r.get('first_pass_rate'))} escape={_fmt_rate(r.get('escape_rate'))} "
            f"panels={r.get('panels_total')} decisions={r.get('trail_decisions_total')}"
        )
    return "\n".join(lines)
