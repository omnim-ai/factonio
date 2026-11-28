"""Layer 0 read-only access to Hermes's `kanban.db` (stdlib + `vocabulary`; Hermes imports stay lazy).

`resolve_kanban_db_path` and `open_kanban_ro` are the only way Factonio locates and opens the
board: the connection is always ``mode=ro`` on a ``file:`` URI built by ``Path.as_uri()``, so
paths containing ``?`` or ``#`` can never be misparsed as URI query/fragment parts.
"""

from __future__ import annotations

import os
import sqlite3
import stat
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence, Union

from .vocabulary import INACTIVE_CARD_STATUSES as INACTIVE_CARD_STATUSES  # re-export for callers

PathLike = Union[str, Path]
DEFAULT_CHUNK_SIZE = 500  # well under SQLite's historical 999 SQLITE_MAX_VARIABLE_NUMBER


def get_hermes_home_path() -> Path:
    """Resolve the active HERMES_HOME path via Hermes core."""
    from hermes_constants import get_hermes_home

    return Path(get_hermes_home())


def get_root_hermes_home_path() -> Path:
    """Resolve the root (default) HERMES_HOME even when running inside a named profile."""
    active = get_hermes_home_path()
    if active.parent.name == "profiles":
        return active.parent.parent
    return active


def resolve_kanban_db_path(board: Optional[str] = None) -> Path:
    """Hermes's active `kanban.db`: Hermes ``kanban_db_path(board=)`` (which honors ``HERMES_KANBAN_DB``
    and boards), else ``HERMES_KANBAN_DB``, else ``<root HERMES_HOME>/kanban.db``."""
    try:
        from hermes_cli.kanban_db import kanban_db_path

        return Path(kanban_db_path(board=board))
    except Exception:
        pinned = os.environ.get("HERMES_KANBAN_DB", "").strip()
        if pinned:
            return Path(pinned)
        return get_root_hermes_home_path() / "kanban.db"


def open_kanban_ro(db_path: PathLike, *, timeout: float = 3.0, strict: bool = False) -> Optional[sqlite3.Connection]:
    """Read-only ``sqlite3.Row`` connection, or ``None`` when the database is absent.

    Lenient (default): anything that is not a regular file reads as absent. ``strict=True`` treats only
    a genuinely missing path as absent and raises ``sqlite3.OperationalError`` for an unstattable path,
    a dangling symlink, a non-regular file, or an open failure, so callers can fail closed.
    """
    path = Path(db_path)
    if strict:
        try:
            os.lstat(path)
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise sqlite3.OperationalError(f"cannot stat {path}: {exc}") from None
        try:
            st = os.stat(path)
        except OSError as exc:
            raise sqlite3.OperationalError(f"cannot stat {path}: {exc}") from None
        if not stat.S_ISREG(st.st_mode):
            raise sqlite3.OperationalError(f"{path} is not a regular file")
    elif not path.is_file():
        return None
    try:
        conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=timeout)
    except sqlite3.Error as exc:
        if strict:
            raise sqlite3.OperationalError(f"cannot open {path}: {exc}") from None
        raise
    conn.row_factory = sqlite3.Row
    return conn


def chunked_in_query(
    conn: sqlite3.Connection,
    sql_template: str,
    ids: Iterable[Any],
    *,
    prefix_params: Sequence[Any] = (),
    suffix_params: Sequence[Any] = (),
    chunk_size: int = DEFAULT_CHUNK_SIZE,
) -> list[Any]:
    """Run ``sql_template`` (with one ``{placeholders}`` slot) per chunk of ``ids`` and concatenate rows.

    Bound parameters are ``prefix_params + chunk + suffix_params``. Rows for one id never straddle
    chunks, so per-id ``ORDER BY`` ordering inside the template is preserved.
    """
    values = list(ids)
    if chunk_size < 1:
        raise ValueError(f"chunk_size must be >= 1; got {chunk_size!r}")
    rows: list[Any] = []
    for start in range(0, len(values), chunk_size):
        chunk = values[start:start + chunk_size]
        sql = sql_template.format(placeholders=",".join("?" for _ in chunk))
        rows.extend(conn.execute(sql, [*prefix_params, *chunk, *suffix_params]).fetchall())
    return rows


def table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    try:
        return {str(r[1]) for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    except sqlite3.Error:
        return set()


def select_list(wanted: tuple[str, ...], available: set[str]) -> str:
    return ", ".join(c if c in available else f"NULL AS {c}" for c in wanted)


def resolve_calling_session_id(explicit: Optional[str] = None) -> Optional[str]:
    """Resolve the active chat session id from explicit kwargs or Hermes session context."""
    if explicit and str(explicit).strip():
        return str(explicit).strip()
    try:
        from gateway.session_context import get_session_env

        for key in ("HERMES_SESSION_CHAT_ID", "HERMES_SESSION_KEY"):
            val = (get_session_env(key, "") or "").strip()
            if val:
                return val
    except Exception:
        pass
    return None


def auto_subscribe_task(conn: Any, task_id: str, *, session_id: Optional[str] = None) -> bool:
    """Subscribe the calling chat session to completion/block wakes (matches `kanban_create`)."""
    try:
        from tools.kanban_tools import _maybe_auto_subscribe

        if _maybe_auto_subscribe(conn, task_id):
            return True
    except Exception:
        pass

    # Fallback when called with an explicit session_id outside gateway ContextVars (e.g. API/tests).
    if not session_id:
        return False
    try:
        from hermes_cli import kanban_db_notify as kbn

        chat_id = str(session_id).strip()
        if not chat_id:
            return False
        if any(
            sub["platform"] == "local" and sub["chat_id"] == chat_id
            for sub in kbn.list_notify_subs(conn, task_id)
        ):
            return True
        kbn.add_notify_sub(
            conn,
            task_id=task_id,
            platform="local",
            chat_id=chat_id,
            notifier_profile="default",
            delivery_mode="notify+wake",
        )
        return True
    except Exception:
        return False
