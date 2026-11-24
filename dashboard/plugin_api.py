"""Factonio dashboard API routes mounted at `/api/plugins/factonio/`."""

from __future__ import annotations

import importlib
import sys
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

_config = importlib.import_module(f"{_PKG_NAME}.config")
_metrics = importlib.import_module(f"{_PKG_NAME}.metrics")
_profile_setup = importlib.import_module(f"{_PKG_NAME}.profile_setup")
_repos = importlib.import_module(f"{_PKG_NAME}.repos")

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


def _repo_defaults(settings: dict[str, Any]) -> dict[str, Any]:
    return {
        "merge_policy": settings.get("default_merge_policy") or "auto",
        "max_fix_rounds": settings.get("default_max_fix_rounds") or 3,
        "design_provider": settings.get("design_provider") or "auto",
        "design_approval": settings.get("design_approval") or "ask",
    }


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
        "max_fix_rounds": info.get("max_fix_rounds", defaults["max_fix_rounds"]),
        "design_provider": explicit_provider,
        "design_approval": explicit_approval,
        "effective_design_provider": effective_provider,
        "effective_design_approval": effective_approval,
        "resolved_design_provider": resolved_provider,
        "has_required_checks": bool(info.get("has_required_checks")),
        "models": _config.resolve_all_phase_models(repo_slug=slug),
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
    """Environment and profile readiness report."""
    return _profile_setup.run_doctor(_PLUGIN_ROOT)
