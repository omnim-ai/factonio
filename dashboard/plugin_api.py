"""Factonio dashboard API routes mounted at `/api/plugins/factonio/`."""

from __future__ import annotations

import codecs
import importlib
import json
import os
import re
import sqlite3
import stat
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Optional

from fastapi import APIRouter, Body, HTTPException, Query

from hermes_constants import get_hermes_home

# Ensure the plugin root directory is importable as a package even when the dashboard
# host imports this module directly by file path.
_PLUGIN_ROOT = Path(__file__).resolve().parent.parent
_PKG_NAME = "factonio_dashboard_pkg"
if _PKG_NAME not in sys.modules:
    import importlib.util

    _init_spec = importlib.util.spec_from_file_location(
        _PKG_NAME,
        _PLUGIN_ROOT / "__init__.py",
        submodule_search_locations=[str(_PLUGIN_ROOT)],
    )
    assert _init_spec is not None and _init_spec.loader is not None
    _pkg_mod = importlib.util.module_from_spec(_init_spec)
    sys.modules[_PKG_NAME] = _pkg_mod
    # Do not call register(); only bind package namespace so submodules import cleanly.

_arena = importlib.import_module(f"{_PKG_NAME}.arena")
_config = importlib.import_module(f"{_PKG_NAME}.config")
_harness = importlib.import_module(f"{_PKG_NAME}.harness")
_metrics = importlib.import_module(f"{_PKG_NAME}.metrics")
_orchestrator = importlib.import_module(f"{_PKG_NAME}.orchestrator")
_profile_setup = importlib.import_module(f"{_PKG_NAME}.profile_setup")
_repos = importlib.import_module(f"{_PKG_NAME}.repos")
_trail = importlib.import_module(f"{_PKG_NAME}.trail")

router = APIRouter()
Range = Literal["7d", "30d", "90d", "all"]


def _kanban_db() -> Path:
    return Path(get_hermes_home()) / "kanban.db"


# --- small fail-closed request validators (never coerce bools/ints/nulls/objects to strings) -----

_TENANT_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_SHA_RE = re.compile(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}")
_MULTILINE_OK = frozenset("\n\r\t")


def _unprocessable(detail: Any) -> HTTPException:
    return HTTPException(status_code=422, detail=detail)


def _require_json_object(payload: Any, *, optional: bool = False) -> dict[str, Any]:
    """The request body must be a JSON object (an omitted body is ``{}`` only when ``optional``)."""
    if payload is None and optional:
        return {}
    if not isinstance(payload, dict):
        raise _unprocessable("Request body must be a JSON object.")
    return payload


def _reject_unknown_keys(payload: dict[str, Any], allowed: tuple[str, ...]) -> None:
    unknown = sorted(str(k) for k in payload if k not in allowed)
    if unknown:
        raise _unprocessable(f"Unknown field(s): {', '.join(unknown)}. Allowed fields: {', '.join(allowed)}")


def _require_clean_str(
    payload: dict[str, Any],
    key: str,
    *,
    required: bool = False,
    max_len: int = 200,
    multiline: bool = False,
) -> Optional[str]:
    """A present field must be a real ``str``: UTF-8 encodable, no NUL/control characters, non-blank.

    ``multiline`` fields may also carry newlines and tabs. Absent optional fields are ``None``; an
    explicit JSON ``null`` is rejected like any other non-string.
    """
    if key not in payload:
        if required:
            raise _unprocessable(f"{key} is required.")
        return None
    value = payload[key]
    if not isinstance(value, str):
        raise _unprocessable(f"{key} must be a string; got {type(value).__name__}.")
    try:
        value.encode("utf-8")
    except UnicodeError:
        raise _unprocessable(f"{key} is not valid UTF-8.") from None
    if any((ord(ch) < 32 or 0x7F <= ord(ch) < 0xA0) and not (multiline and ch in _MULTILINE_OK) for ch in value):
        raise _unprocessable(f"{key} must not contain NUL or control characters.")
    if not value.strip():
        raise _unprocessable(f"{key} must be a non-empty string.")
    if len(value) > max_len:
        raise _unprocessable(f"{key} must be at most {max_len} characters.")
    return value


def _require_strict_bool(payload: dict[str, Any], key: str, *, default: Optional[bool] = None) -> Optional[bool]:
    if key not in payload:
        return default
    value = payload[key]
    if not isinstance(value, bool):
        raise _unprocessable(f"{key} must be a JSON boolean; got {type(value).__name__}.")
    return value


def _require_strict_pos_int(payload: dict[str, Any], key: str) -> Optional[int]:
    if key not in payload:
        return None
    value = payload[key]
    if type(value) is not int or value <= 0:
        raise _unprocessable(f"{key} must be a positive JSON integer; got {value!r}.")
    return value


def _validate_tenant_slug(value: Any, field: str = "tenant") -> str:
    """A workstream tenant usable as one path component: no separators, ``..``, controls or holdout."""
    if (
        not isinstance(value, str)
        or not _TENANT_RE.fullmatch(value)
        or ".." in value
        or _trail.contains_holdout_reference(value)
    ):
        raise _unprocessable(f"{field} must match ^[A-Za-z0-9][A-Za-z0-9._-]{{0,127}}$ (no '..').")
    return value


def _require_confirmed(payload: dict[str, Any]) -> None:
    if payload.get("confirmed") is not True:
        raise _unprocessable("confirmed must be the JSON boolean true for this action.")


@router.get("/status")
def status(profile: Optional[str] = Query(None)):
    """High-level status pill and readiness summary."""
    repos = _config.list_repos()
    cards = _metrics.thread_cards(_kanban_db(), limit=200)
    models = _config.resolve_all_phase_models()
    warning = _config.check_cross_model_warning()
    return {
        "ok": True,
        "repos_count": len(repos),
        "counts": cards["counts"],
        "models": models,
        "cross_model_warning": warning,
    }


def _safe_publish_policy(info: Optional[dict[str, Any]] = None) -> Optional[str]:
    """Effective publish policy, or None when a stored value is invalid.

    Never falls back to ``"auto"`` on bad config: ``None`` tells the UI the value is unknown
    instead of implying Factonio may publish on its own.
    """
    try:
        if info is None:
            return _config.resolve_default_publish_policy()
        return _config.resolve_publish_policy(repo=info)
    except ValueError:
        return None


def _repo_defaults(settings: dict[str, Any]) -> dict[str, Any]:
    return {
        "merge_policy": settings.get("default_merge_policy") or "auto",
        "publish_policy": _safe_publish_policy(),
        "max_fix_rounds": settings.get("default_max_fix_rounds") or 3,
        "design_provider": settings.get("design_provider") or "auto",
        "design_approval": settings.get("design_approval") or "ask",
        "default_rigor": settings.get("default_rigor") or "standard",
    }


def _safe_rigor(repo_slug: Optional[str] = None) -> Optional[str]:
    """Effective rigor, or None when a stored value is invalid (surfaced instead of raising a 500)."""
    try:
        return _config.resolve_rigor(repo_slug=repo_slug)
    except ValueError:
        return None


def _serialize_repo(slug: str, info: dict[str, Any], settings: dict[str, Any]) -> dict[str, Any]:
    """Serialize one registered repository with explicit and effective settings."""
    defaults = _repo_defaults(settings)
    explicit_provider = info.get("design_provider") or None
    explicit_approval = info.get("design_approval") or None
    try:
        design = _config.resolve_design_provider(repo_slug=slug)
        effective_provider = design.get("configured")
        effective_approval = design.get("approval")
        resolved_provider = design.get("effective")
    except ValueError:
        effective_provider = explicit_provider or defaults["design_provider"]
        effective_approval = explicit_approval or defaults["design_approval"]
        resolved_provider = None
    return {
        "slug": slug,
        "repo": info.get("repo"),
        "url": info.get("url"),
        "path": info.get("path"),
        "default_branch": info.get("default_branch", "main"),
        "merge_policy": info.get("merge_policy", defaults["merge_policy"]),
        "publish_policy": _safe_publish_policy(info),
        "max_fix_rounds": info.get("max_fix_rounds", defaults["max_fix_rounds"]),
        "design_provider": explicit_provider,
        "design_approval": explicit_approval,
        "effective_design_provider": effective_provider,
        "effective_design_approval": effective_approval,
        "resolved_design_provider": resolved_provider,
        "default_rigor": info.get("default_rigor") or None,
        "effective_default_rigor": _safe_rigor(slug),
        "has_required_checks": bool(info.get("has_required_checks")),
        "models": _config.resolve_all_phase_models(repo_slug=slug),
        "harness": _harness.summarize_harness_readonly(info.get("path")),
    }


@router.get("/repos")
def repos(profile: Optional[str] = Query(None)):
    """Registered repositories with explicit/effective settings plus global defaults."""
    settings = _config.load_settings(use_root=True)
    items = []
    for slug, info in sorted(_config.list_repos().items()):
        if not isinstance(info, dict):
            continue
        items.append(_serialize_repo(slug, info, settings))
    return {"repos": items, "defaults": _repo_defaults(settings)}


@router.patch("/repos/{slug}")
def patch_repo(
    slug: str,
    payload: Any = Body(...),
    profile: Optional[str] = Query(None),
):
    """Update editable parameters of a registered repository."""
    if not isinstance(payload, dict):
        raise HTTPException(status_code=422, detail="Request body must be a JSON object.")
    if not payload:
        raise HTTPException(
            status_code=422,
            detail="At least one editable field must be provided: " + ", ".join(_repos.EDITABLE_REPO_FIELDS),
        )
    unknown = sorted(set(payload) - set(_repos.EDITABLE_REPO_FIELDS))
    if unknown:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Unknown or immutable field(s): {', '.join(unknown)}. "
                f"Editable fields: {', '.join(_repos.EDITABLE_REPO_FIELDS)}"
            ),
        )
    try:
        res = _repos.update_repo(slug, **payload)
    except _repos.RepoNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    entry = res["repo"]
    settings = _config.load_settings(use_root=True)
    return {"ok": True, "repo": _serialize_repo(str(entry["slug"]), entry, settings)}


@router.get("/models")
def models(repo: Optional[str] = Query(None), profile: Optional[str] = Query(None)):
    """Resolved per-phase provider and model matrix."""
    return {
        "models": _config.resolve_all_phase_models(repo_slug=repo),
        "cross_model_warning": _config.check_cross_model_warning(repo_slug=repo),
        "verify_panel": _config.resolve_verify_panel(repo_slug=repo),
        "default_rigor": _safe_rigor(repo),
    }


@router.get("/metrics")
def metrics(
    range: Range = Query("30d"),
    tenant: Optional[str] = Query(None),
    profile: Optional[str] = Query(None),
):
    """3-phase durations, Diff vs Black-Box verification pass rates, rework, and escape rate."""
    try:
        return _metrics.compute_metrics(_kanban_db(), range_key=range, tenant=tenant)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/metrics/history")
def metrics_history(
    repo: Optional[str] = Query(None),
    tenant: Optional[str] = Query(None),
    source: Optional[str] = Query(None),
    since: Optional[str] = Query(None),
    limit: int = Query(100, ge=1, le=1000),
    profile: Optional[str] = Query(None),
):
    """Long-term `trail.db` metric snapshots, oldest -> newest: `{snapshots, summary, filters}`."""
    try:
        return _trail.metrics_history_payload(tenant=tenant, repo=repo, source=source, since=since, limit=limit)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except (sqlite3.Error, OSError) as exc:
        raise HTTPException(status_code=503, detail=f"trail.db metric history unavailable: {exc}") from exc


@router.get("/cards")
def cards(
    session_id: Optional[str] = Query(None),
    tenant: Optional[str] = Query(None),
    limit: int = Query(100, ge=1, le=200),
    profile: Optional[str] = Query(None),
):
    """Kanban cards for the focused chat session or workstream tenant."""
    return _metrics.thread_cards(_kanban_db(), session_id=session_id, tenant=tenant, limit=limit)


def _scrub_public(value: Any) -> Any:
    """Public serializer (`trail.scrub_public`): drop holdout-named keys, redact holdout strings, and null
    non-finite floats so every payload stays JSON-safe. Other strings pass through byte-for-byte.
    """
    return _trail.scrub_public(value)


@router.get("/trail")
def trail(
    tenant: Optional[str] = Query(None),
    repo: Optional[str] = Query(None),
    phase: Optional[str] = Query(None),
    card_id: Optional[str] = Query(None),
    limit: int = Query(100, ge=1, le=1000),
    actor: Optional[str] = Query(None),
    q: Optional[str] = Query(None),
    signal: Optional[str] = Query(None),
    include_superseded: Optional[bool] = Query(None),
    profile: Optional[str] = Query(None),
):
    """Append-only decision trail from `trail.db` (read-only): `{decisions, summary, filters}`."""
    try:
        payload = _trail.trail_payload(
            tenant=tenant, repo=repo, phase=phase, card_id=card_id, limit=limit,
            actor=actor, q=q, signal=signal, include_superseded=include_superseded,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except (sqlite3.Error, OSError) as exc:
        raise HTTPException(status_code=503, detail="trail.db unreadable") from exc
    return _scrub_public(payload)


@router.get("/trail/audit")
def trail_audit(
    tenant: Optional[str] = Query(None),
    repo: Optional[str] = Query(None),
    phase: Optional[str] = Query(None),
    card_id: Optional[str] = Query(None),
    profile: Optional[str] = Query(None),
):
    """Read-only `trail --audit` findings (`mode=ro`); `503` when `trail.db` is unreadable."""
    audit = _trail.audit_trail_evidence(tenant=tenant, repo=repo, phase=phase, card_id=card_id)
    if not audit.get("ok"):
        raise HTTPException(status_code=503, detail="trail.db unreadable: the decision trail cannot be audited.")
    return _scrub_public(audit)


@router.get("/trail/{decision_id}")
def trail_decision(decision_id: str, profile: Optional[str] = Query(None)):
    """One decision by id, `{decision}`, so lineage links can reach rows outside a `/trail` window."""
    try:
        row = _trail.get_decision(decision_id)
    except (sqlite3.Error, OSError) as exc:
        raise HTTPException(status_code=503, detail="trail.db unreadable") from exc
    if row is None:
        raise HTTPException(status_code=404, detail=_scrub_public(f"Decision {decision_id!r} not found."))
    return {"decision": _scrub_public(row)}


_HUMAN_DECISION_PHASES = (
    "define", "design", "implement", "verify", "publish", "gate", "merge", "intake", "harness", "eval",
    "escape", "watchdog", "retro",
)
_HUMAN_DECISION_FIELDS = (
    "decision", "phase", "why", "confirmed", "tenant", "repo", "card_id", "result", "supersedes_id", "evidence",
)
_EVIDENCE_MAX_BYTES = 16 * 1024


@router.post("/trail/decisions", status_code=201)
def log_human_decision(payload: Any = Body(None), profile: Optional[str] = Query(None)):
    """Append one human decision/note (`actor` is always `human`); corrections supersede, never mutate."""
    body = _require_json_object(payload)
    if "actor" in body:
        raise _unprocessable("actor is set by the server (always 'human') and must not be supplied.")
    _reject_unknown_keys(body, _HUMAN_DECISION_FIELDS)
    _require_confirmed(body)
    decision = _require_clean_str(body, "decision", required=True)
    why = _require_clean_str(body, "why", required=True, max_len=4000, multiline=True)
    phase = _require_clean_str(body, "phase", required=True)
    if phase not in _HUMAN_DECISION_PHASES:
        raise _unprocessable(f"phase must be one of {', '.join(_HUMAN_DECISION_PHASES)}.")
    tenant = _validate_tenant_slug(body["tenant"]) if "tenant" in body else None
    repo = _require_clean_str(body, "repo")
    card_id = _require_clean_str(body, "card_id")
    result = _require_clean_str(body, "result")
    supersedes_id = _require_clean_str(body, "supersedes_id")
    evidence = body.get("evidence", {})
    if not isinstance(evidence, dict):
        raise _unprocessable("evidence must be a JSON object.")
    try:
        encoded = json.dumps(evidence, allow_nan=False, sort_keys=True)
    except (TypeError, ValueError):
        raise _unprocessable("evidence must be finite, JSON-serializable data.") from None
    if len(encoded.encode("utf-8")) > _EVIDENCE_MAX_BYTES:
        raise _unprocessable(f"evidence must be at most {_EVIDENCE_MAX_BYTES} bytes of JSON.")
    try:
        if supersedes_id is not None and _trail.get_decision(supersedes_id) is None:
            raise HTTPException(status_code=404, detail="supersedes_id does not reference an existing decision.")
        row = _trail.log_decision(
            decision, tenant=tenant, repo_slug=repo, phase=phase, card_id=card_id, actor="human", why=why,
            evidence=evidence, result=result, supersedes_id=supersedes_id,
        )
    except ValueError as exc:
        raise _unprocessable(_scrub_public(str(exc))) from exc
    except (sqlite3.Error, OSError) as exc:
        raise HTTPException(status_code=503, detail="trail.db unavailable: the decision was not recorded.") from exc
    return {"ok": True, "decision": _scrub_public(row)}


# --- read-only investigation report viewer ------------------------------------------------------

_REPORT_NAME = "investigation.md"
_REPORT_MAX_BYTES = 256 * 1024
# Root-independent absolute filesystem paths: file:// URLs, UNC, Windows drive (either slash) and any
# multi-segment POSIX path. http(s) URLs match first as a pass-through group; relative paths never match.
_PATH_TAIL = r"[^\s`'\"<>)\]]+"
_ABS_PATH_RE = re.compile(
    rf"(?P<url>(?i:https?)://{_PATH_TAIL})"
    rf"|(?i:file)://{_PATH_TAIL}"
    rf"|(?<![A-Za-z0-9_.:/\\])(?:\\\\|//)[A-Za-z0-9._$-]+[\\/]{_PATH_TAIL}"
    rf"|(?<![A-Za-z0-9_])[A-Za-z]:[\\/]{_PATH_TAIL}"
    rf"|(?<![A-Za-z0-9_.:/\\])/[A-Za-z0-9._-]+(?:/{_PATH_TAIL})+"
)


def _scrub_abs_paths(text: str) -> tuple[str, bool]:
    hits = 0

    def repl(match: re.Match) -> str:
        nonlocal hits
        if match.group("url") is not None:
            return match.group(0)
        hits += 1
        return "[REDACTED]"

    return _ABS_PATH_RE.sub(repl, text), hits > 0


_DIR_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
_FILE_FLAGS = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)


def _trim_utf8_bytes(value: str | bytes, max_bytes: int = _REPORT_MAX_BYTES) -> tuple[str, bool]:
    """Cut to at most ``max_bytes`` of UTF-8 on a character boundary; returns ``(text, trimmed)``.

    A multi-byte sequence split by the cut is dropped, never turned into U+FFFD.
    """
    raw = value.encode("utf-8") if isinstance(value, str) else value
    trimmed = len(raw) > max_bytes
    text = codecs.getincrementaldecoder("utf-8")("replace").decode(raw[:max_bytes], final=not trimmed)
    return text, trimmed


def _redact_report(text: str) -> tuple[str, bool]:
    """Withhold every line naming holdout (the whole report if a reference spans lines), then absolute paths."""
    lines = text.split("\n")
    kept = ["[redacted]" if _trail.contains_holdout_reference(line) else line for line in lines]
    out = "\n".join(kept)
    if _trail.contains_holdout_reference(out):
        return "[redacted]", True
    out, paths = _scrub_abs_paths(out)
    return out, kept != lines or paths


def _read_confined_report(tenant_dir: str) -> Optional[tuple[bytes, os.stat_result]]:
    """Read ``<hermes_home>/plugin-data/factonio/runs/<tenant>/investigation.md`` through pinned dir fds.

    No component below the Hermes home may be a symlink or change identity mid-walk (``403``); a
    missing component returns ``None``. Every descriptor is closed before returning.
    """
    forbidden = HTTPException(status_code=403, detail="The investigation report is not a regular file.")
    parts = ("plugin-data", _config.PLUGIN_ID, "runs", tenant_dir)
    fds: list[int] = []
    try:
        try:
            fds.append(os.open(_config.get_root_hermes_home_path(), os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)))
            for name in parts:
                fds.append(os.open(name, _DIR_FLAGS, dir_fd=fds[-1]))
            fds.append(os.open(_REPORT_NAME, _FILE_FLAGS, dir_fd=fds[-1]))
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise forbidden from exc
        st = os.fstat(fds[-1])
        if not stat.S_ISREG(st.st_mode):
            raise forbidden
        # Re-check every pinned component still names the opened inode (no swap during the walk).
        for parent, name, fd in zip(fds, (*parts, _REPORT_NAME), fds[1:]):
            try:
                now = os.stat(name, dir_fd=parent, follow_symlinks=False)
            except OSError as exc:
                raise forbidden from exc
            pinned = os.fstat(fd)
            if (now.st_dev, now.st_ino) != (pinned.st_dev, pinned.st_ino):
                raise forbidden
        with os.fdopen(fds.pop(), "rb") as fh:
            return fh.read(_REPORT_MAX_BYTES + 1), st
    finally:
        for fd in fds:
            os.close(fd)


@router.get("/workstreams/{tenant}/report")
def workstream_report(tenant: str, profile: Optional[str] = Query(None)):
    """`<runs>/<tenant>/investigation.md` as bounded, holdout-scrubbed text; never a path, never a write."""
    safe = _metrics._SAFE_TENANT_RE.sub("-", _validate_tenant_slug(tenant).lower()).strip("-") or "default"
    result: dict[str, Any] = {
        "ok": True, "exists": False, "tenant": tenant, "filename": _REPORT_NAME, "content": None,
        "updated_at": None, "size_bytes": 0, "truncated": False, "redacted": False,
    }
    read = _read_confined_report(safe)
    if read is None:
        return result
    data, st = read
    if b"\x00" in data:
        raise _unprocessable("The investigation report contains binary (NUL) data.")
    text, truncated = _trim_utf8_bytes(data)
    content, redacted = _redact_report(text)
    content, trimmed = _trim_utf8_bytes(content)
    if trimmed and _trail.contains_holdout_reference(content):
        content = "[redacted]"
    result.update(
        exists=True,
        content=content,
        updated_at=datetime.fromtimestamp(st.st_mtime, timezone.utc).isoformat().replace("+00:00", "Z"),
        size_bytes=int(st.st_size),
        truncated=truncated or trimmed,
        redacted=redacted,
    )
    return result


# --- on-demand control-plane actions ------------------------------------------------------------


@router.post("/repos/{slug}/harness/audit")
def audit_repo_harness(slug: str, payload: Any = Body(None), profile: Optional[str] = Query(None)):
    """Read-only `.factonio/` drift audit of a registered checkout, logged as `harness_audit` (actor human)."""
    body = _require_json_object(payload, optional=True)
    _reject_unknown_keys(body, ("base_ref",))
    base_ref = _require_clean_str(body, "base_ref")
    if base_ref is not None and (base_ref.startswith("-") or any(ch.isspace() for ch in base_ref)):
        raise _unprocessable("base_ref must not start with '-' or contain whitespace.")
    entry = _config.get_repo(slug)
    if not isinstance(entry, dict) or not entry.get("slug"):
        raise HTTPException(status_code=404, detail=_scrub_public(f"Repository {slug!r} is not registered."))
    repo_slug = str(entry["slug"])
    path = entry.get("path")
    if not (isinstance(path, str) and path.strip() and Path(path).expanduser().is_dir()):
        raise HTTPException(status_code=409, detail=f"Local checkout for {repo_slug} is missing.")
    try:
        audit = _harness.audit_harness(repo_slug, base_ref=base_ref)
    except ValueError as exc:
        message = str(exc)
        aliases = {path, str(Path(path).expanduser()), str(Path(path).expanduser().resolve())}
        for alias in sorted(aliases, key=len, reverse=True):
            message = message.replace(alias, "<checkout>")
        raise _unprocessable(_scrub_public(message)) from exc
    out = {k: v for k, v in audit.items() if k != "repo_path"}
    classification = out.get("drift_classification") or {}
    _trail.record_transition(
        "harness_audit",
        repo_slug=repo_slug,
        phase="harness",
        actor="human",
        why=f"On-demand Desktop harness audit: severity {out.get('severity')}",
        evidence={
            "drifted": bool(out.get("drifted")),
            "severity": out.get("severity"),
            "drift_classification": classification.get("classification") if isinstance(classification, dict) else None,
            "base_ref": base_ref,
            "base_commit": out.get("base_commit"),
            "head_commit": out.get("head_commit"),
            "stale_invariants": len(out.get("stale_invariants") or []),
            "manifest_errors": len(out.get("manifest_errors") or []),
        },
        result=str(out.get("severity") or ""),
    )
    return _scrub_public({**out, "ok": bool(out.get("ok", True)), "slug": repo_slug})


_PUBLISH_FIELDS = (
    "repo", "branch", "expected_head_sha", "title", "body", "base_branch", "draft", "tenant", "card_id", "confirmed",
)


@router.post("/publish-pr")
def publish_pr_route(payload: Any = Body(None), profile: Optional[str] = Query(None)):
    """Publish a locally verified branch at its exact verified SHA (explicit human confirmation)."""
    body = _require_json_object(payload)
    _reject_unknown_keys(body, _PUBLISH_FIELDS)
    _require_confirmed(body)
    repo = _require_clean_str(body, "repo", required=True)
    branch = _require_clean_str(body, "branch", required=True)
    sha_raw = _require_clean_str(body, "expected_head_sha", required=True)
    if not _SHA_RE.fullmatch(sha_raw or ""):
        raise _unprocessable("expected_head_sha must be a full 40- or 64-character hex commit SHA.")
    sha = str(sha_raw).lower()
    title = _require_clean_str(body, "title", required=True, max_len=256)
    pr_body = _require_clean_str(body, "body", max_len=65536, multiline=True)
    base_branch = _require_clean_str(body, "base_branch")
    draft = _require_strict_bool(body, "draft", default=False)
    tenant = _validate_tenant_slug(body["tenant"]) if "tenant" in body else None
    card_id = _require_clean_str(body, "card_id")

    entry = _config.get_repo(str(repo))
    if not isinstance(entry, dict) or not entry.get("repo"):
        raise HTTPException(status_code=404, detail=_scrub_public(f"Repository {repo!r} is not registered."))
    full_repo, repo_slug = str(entry["repo"]), str(entry.get("slug") or "")
    status = _metrics.local_branch_verification_status(
        _kanban_db(), repo=full_repo, branch=str(branch), head_sha=sha, tenant=tenant
    )
    refusal: Optional[str] = None
    if status.get("verified") is not True:
        refusal = f"Not locally verified at {sha}: {status.get('reason')}"
    elif base_branch is not None and base_branch != status.get("base_branch"):
        refusal = f"base_branch {base_branch!r} differs from the verified base {status.get('base_branch')!r}"
    verification = {k: status.get(k) for k in ("verified", "mode", "consensus", "reason", "panel_id", "base_branch")}
    if refusal is not None:
        _trail.record_transition(
            "publish_pr",
            tenant=tenant or "",
            repo_slug=repo_slug,
            phase="publish",
            card_id=card_id or "",
            actor="human",
            why=refusal[:500],
            evidence={
                "repo": full_repo, "branch": branch, "base_branch": base_branch, "head_sha": None,
                "expected_head_sha": sha, "pr_number": None, "url": None, "draft": draft,
                "gate": {"check": "local_verification", "pushed": False, "created": False, "published": False},
                "verification": verification,
            },
            result="refused",
        )
        raise HTTPException(
            status_code=409,
            detail=_scrub_public({
                "ok": False, "published": False, "pushed": False, "created": False, "refused": True,
                "error": refusal, "verification": verification,
            }),
        )
    res = _repos.publish_pr(
        full_repo, str(branch), expected_head_sha=sha, title=str(title), body=pr_body,
        base_branch=base_branch or status.get("base_branch"), draft=bool(draft), remote="origin",
        tenant=tenant, card_id=card_id, actor="human",
    )
    public = _scrub_public({**res, "verification": verification})
    if res.get("ok") and res.get("published"):
        return public
    raise HTTPException(status_code=409 if res.get("refused") else 502, detail=public)


@router.post("/panels/evaluate")
def evaluate_panel(payload: Any = Body(None), profile: Optional[str] = Query(None)):
    """Re-aggregate a verification panel from read-only `kanban.db`; logs a terminal consensus once."""
    body = _require_json_object(payload)
    _reject_unknown_keys(body, ("panel_id", "tenant", "pr_number", "branch"))
    panel_id = _require_clean_str(body, "panel_id")
    tenant = _validate_tenant_slug(body["tenant"]) if "tenant" in body else None
    pr_number = _require_strict_pos_int(body, "pr_number")
    branch = _require_clean_str(body, "branch")
    if pr_number is not None and branch is not None:
        raise _unprocessable("Pass either pr_number or branch, not both.")
    if panel_id is None and tenant is None and pr_number is None and branch is None:
        raise _unprocessable("Pass panel_id, tenant, pr_number or branch to select a verification panel.")
    try:
        res = _orchestrator.evaluate_verification_panel(
            panel_id=panel_id, tenant=tenant, pr_number=pr_number, branch=branch, record_trail=True
        )
    except ValueError as exc:
        raise _unprocessable(_scrub_public(str(exc))) from exc
    if not res.get("ok"):
        raise HTTPException(status_code=404, detail=_scrub_public(str(res.get("error") or "No verification panel found.")))
    return _scrub_public(res)


@router.post("/arenas/evaluate")
def evaluate_arena_route(payload: Any = Body(None), profile: Optional[str] = Query(None)):
    """Read-only arena progress (never spawns a judge), as a path-free public arena summary."""
    body = _require_json_object(payload)
    _reject_unknown_keys(body, ("arena_id", "tenant"))
    arena_id = _require_clean_str(body, "arena_id")
    tenant = _validate_tenant_slug(body["tenant"]) if "tenant" in body else None
    if arena_id is not None and not _arena.ARENA_ID_RE.fullmatch(arena_id):
        raise _unprocessable("arena_id must look like ar_<hex>.")
    if arena_id is None and tenant is None:
        raise _unprocessable("Pass arena_id and/or tenant to select an arena.")
    res = _arena.evaluate_arena_public(arena_id=arena_id, tenant=tenant)
    if not res.get("ok"):
        raise HTTPException(status_code=404, detail=str(res.get("error") or "No arena found."))
    return _scrub_public({"ok": True, **res["arena"]})


@router.get("/doctor")
def doctor(profile: Optional[str] = Query(None)):
    """Environment and profile readiness report."""
    return _profile_setup.run_doctor(_PLUGIN_ROOT)
