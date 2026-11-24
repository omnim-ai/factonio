"""Factonio read-only dashboard API routes mounted at `/api/plugins/factonio/`."""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from typing import Literal, Optional

from fastapi import APIRouter, HTTPException, Query

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

_config = importlib.import_module(f"{_PKG_NAME}.config")
_metrics = importlib.import_module(f"{_PKG_NAME}.metrics")
_profile_setup = importlib.import_module(f"{_PKG_NAME}.profile_setup")

router = APIRouter()
Range = Literal["7d", "30d", "90d", "all"]


def _kanban_db() -> Path:
    return Path(get_hermes_home()) / "kanban.db"


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


@router.get("/repos")
def repos(profile: Optional[str] = Query(None)):
    """Registered repositories and their effective settings."""
    items = []
    for slug, info in sorted(_config.list_repos().items()):
        if not isinstance(info, dict):
            continue
        items.append({
            "slug": slug,
            "repo": info.get("repo"),
            "url": info.get("url"),
            "path": info.get("path"),
            "default_branch": info.get("default_branch", "main"),
            "merge_policy": info.get("merge_policy", "auto"),
            "max_fix_rounds": info.get("max_fix_rounds", 3),
            "has_required_checks": bool(info.get("has_required_checks")),
            "models": _config.resolve_all_phase_models(repo_slug=slug),
        })
    return {"repos": items}


@router.get("/models")
def models(repo: Optional[str] = Query(None), profile: Optional[str] = Query(None)):
    """Resolved per-phase provider and model matrix."""
    return {
        "models": _config.resolve_all_phase_models(repo_slug=repo),
        "cross_model_warning": _config.check_cross_model_warning(repo_slug=repo),
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


@router.get("/cards")
def cards(
    session_id: Optional[str] = Query(None),
    tenant: Optional[str] = Query(None),
    limit: int = Query(100, ge=1, le=200),
    profile: Optional[str] = Query(None),
):
    """Kanban cards for the focused chat session or workstream tenant."""
    return _metrics.thread_cards(_kanban_db(), session_id=session_id, tenant=tenant, limit=limit)


@router.get("/doctor")
def doctor(profile: Optional[str] = Query(None)):
    """Read-only environment and profile readiness report."""
    return _profile_setup.run_doctor(_PLUGIN_ROOT)
