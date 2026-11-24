"""Idempotent setup and diagnostics for the dedicated headless `factonio` worker profile."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, Optional

from .config import (
    PLUGIN_ID,
    WORKER_PROFILE,
    check_cross_model_warning,
    get_root_hermes_home_path,
    load_settings,
    resolve_all_phase_models,
    resolve_design_provider,
    save_settings,
)

WORKER_SOUL_MD = """# Factonio Worker

You are a headless Factonio worker executing one scoped Kanban card inside a dedicated git worktree or scratch workspace.

## Rules

1. Start every card by following the preloaded `factonio:*` skill instructions for your phase.
2. Stay strictly within the scope and information barrier of your card:
   - Design cards read `spec.md` and the repo; never look for or read `holdout/` scenarios.
   - Implementation cards read `spec.md`, approved `design/` artifacts (if linked), and code; never look for or read `holdout/` scenarios.
   - Diff Check cards read `spec.md`, approved `design/` artifacts (if linked), and the PR diff; never read `holdout/`.
   - Black-Box Check cards read acceptance criteria, approved `design/` previews (if linked), and `holdout/` and run the built artifact; never read `git diff` or implementation source files.
3. Verify claims with real commands and test output before completing a card.
4. Keep comments and completion summaries concise and structured. Put changed files, PR URLs, check results, and verdicts in `metadata`.
5. Never invent secrets, never bypass CI gates, and block with `needs_input` only when a genuine human decision is required.
"""


def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    import hermes_yaml as yaml

    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    return loaded if isinstance(loaded, dict) else {}


def _write_yaml(path: Path, data: dict[str, Any]) -> None:
    import hermes_yaml as yaml

    target = path.resolve() if path.is_symlink() else path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


def _ensure_plugin_link(profile_dir: Path, plugin_dir: Path) -> dict[str, Any]:
    plugins_dir = profile_dir / "plugins"
    plugins_dir.mkdir(parents=True, exist_ok=True)
    link_path = plugins_dir / PLUGIN_ID
    resolved_src = plugin_dir.resolve()

    if link_path.is_symlink():
        try:
            if link_path.resolve() == resolved_src:
                return {"action": "unchanged", "link": str(link_path), "target": str(resolved_src)}
        except OSError:
            pass
        link_path.unlink()
    elif link_path.exists():
        # Already a real directory (e.g. direct copy); leave untouched rather than clobbering.
        return {"action": "existing_dir", "link": str(link_path), "target": str(link_path)}

    os.symlink(str(resolved_src), str(link_path))
    return {"action": "linked", "link": str(link_path), "target": str(resolved_src)}


def _ensure_worker_config(profile_dir: Path, *, max_concurrent_workers: int) -> dict[str, Any]:
    cfg_path = profile_dir / "config.yaml"
    cfg = _read_yaml(cfg_path)
    changed: list[str] = []

    approvals = cfg.get("approvals")
    if not isinstance(approvals, dict) or approvals.get("mode") != "off":
        cfg["approvals"] = {"mode": "off", "timeout": 3600}
        changed.append("approvals.mode=off")

    plugins = cfg.get("plugins")
    if not isinstance(plugins, dict):
        plugins = {}
        cfg["plugins"] = plugins
    enabled = plugins.get("enabled")
    if not isinstance(enabled, list):
        enabled = []
    if PLUGIN_ID not in enabled:
        enabled.append(PLUGIN_ID)
        plugins["enabled"] = enabled
        changed.append(f"plugins.enabled+={PLUGIN_ID}")
    disabled = plugins.get("disabled")
    if isinstance(disabled, list) and PLUGIN_ID in disabled:
        plugins["disabled"] = [p for p in disabled if p != PLUGIN_ID]
        changed.append(f"plugins.disabled-={PLUGIN_ID}")

    kanban = cfg.get("kanban")
    if not isinstance(kanban, dict):
        kanban = {}
        cfg["kanban"] = kanban
    if kanban.get("max_in_progress_per_profile") != max_concurrent_workers:
        kanban["max_in_progress_per_profile"] = int(max_concurrent_workers)
        changed.append(f"kanban.max_in_progress_per_profile={int(max_concurrent_workers)}")

    if changed or not cfg_path.is_file():
        _write_yaml(cfg_path, cfg)
    return {"config_path": str(cfg_path), "changed": changed}


def _check_or_apply_root_config(
    *,
    apply_root_config: bool,
    max_concurrent_workers: int,
) -> dict[str, Any]:
    root_cfg_path = get_root_hermes_home_path() / "config.yaml"
    cfg = _read_yaml(root_cfg_path)
    raw_kb = cfg.get("kanban")
    kanban: dict[str, Any] = raw_kb if isinstance(raw_kb, dict) else {}

    desired = {
        "dispatch_in_gateway": True,
        "auto_decompose": False,
    }
    pending: dict[str, dict[str, Any]] = {}
    for key, expected in desired.items():
        if kanban.get(key) is not expected:
            pending[f"kanban.{key}"] = {"current": kanban.get(key), "recommended": expected}

    current_max = kanban.get("max_in_progress")
    if not isinstance(current_max, int) or current_max < max_concurrent_workers:
        pending["kanban.max_in_progress"] = {
            "current": current_max,
            "recommended": max_concurrent_workers,
        }

    applied: list[str] = []
    if apply_root_config and pending:
        kb = cfg.setdefault("kanban", {})
        if not isinstance(kb, dict):
            kb = {}
            cfg["kanban"] = kb
        for key, expected in desired.items():
            if kb.get(key) is not expected:
                kb[key] = expected
                applied.append(f"kanban.{key}={expected}")
        if not isinstance(kb.get("max_in_progress"), int) or kb["max_in_progress"] < max_concurrent_workers:
            kb["max_in_progress"] = int(max_concurrent_workers)
            applied.append(f"kanban.max_in_progress={int(max_concurrent_workers)}")
        _write_yaml(root_cfg_path, cfg)

    return {
        "config_path": str(root_cfg_path),
        "pending": pending,
        "applied": applied,
    }


def init_factonio_profile(
    plugin_dir: Path,
    *,
    apply_root_config: bool = False,
    force_soul: bool = False,
    max_concurrent_workers: Optional[int] = None,
) -> dict[str, Any]:
    """Create or reconcile the dedicated `factonio` worker profile."""
    from hermes_cli.profiles import create_profile, get_profile_dir, profile_exists

    settings = load_settings(use_root=True)
    workers = int(max_concurrent_workers or settings.get("max_concurrent_workers") or 4)
    if workers < 1:
        raise ValueError("max_concurrent_workers must be >= 1")

    created = False
    if not profile_exists(WORKER_PROFILE):
        profile_dir = create_profile(
            WORKER_PROFILE,
            no_skills=True,
            seed_model=True,
            description="Factonio headless worker profile (Definition, Implementation, Verification).",
        )
        created = True
    else:
        profile_dir = get_profile_dir(WORKER_PROFILE)

    soul_path = profile_dir / "SOUL.md"
    soul_written = False
    if created or force_soul or not soul_path.is_file() or not soul_path.read_text(encoding="utf-8").strip():
        soul_path.write_text(WORKER_SOUL_MD, encoding="utf-8")
        soul_written = True

    link_info = _ensure_plugin_link(profile_dir, plugin_dir)
    worker_cfg_info = _ensure_worker_config(profile_dir, max_concurrent_workers=workers)
    root_cfg_info = _check_or_apply_root_config(
        apply_root_config=apply_root_config,
        max_concurrent_workers=workers,
    )

    if max_concurrent_workers is not None:
        save_settings({"max_concurrent_workers": workers})

    return {
        "ok": True,
        "profile": WORKER_PROFILE,
        "profile_dir": str(profile_dir),
        "created": created,
        "soul_written": soul_written,
        "plugin_link": link_info,
        "worker_config": worker_cfg_info,
        "root_config": root_cfg_info,
    }


def _cmd_check(name: str, args: list[str]) -> dict[str, Any]:
    exe = shutil.which(name)
    if not exe:
        return {"name": name, "ok": False, "detail": f"{name} not found on PATH"}
    try:
        proc = subprocess.run(
            [exe, *args],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        ok = proc.returncode == 0
        out = (proc.stdout or proc.stderr or "").strip().splitlines()
        detail = out[0] if out else ("ok" if ok else f"exit {proc.returncode}")
        return {"name": name, "ok": ok, "detail": detail}
    except Exception as exc:
        return {"name": name, "ok": False, "detail": str(exc)}


def run_doctor(plugin_dir: Path) -> dict[str, Any]:
    """Run clean-room readiness checks for Factonio."""
    from hermes_cli.profiles import get_profile_dir, profile_exists

    checks: list[dict[str, Any]] = []
    warnings: list[str] = []

    checks.append(_cmd_check("git", ["--version"]))
    gh_ver = _cmd_check("gh", ["--version"])
    checks.append(gh_ver)
    if gh_ver["ok"]:
        checks.append({**_cmd_check("gh", ["auth", "status"]), "name": "gh_auth"})
    else:
        checks.append({"name": "gh_auth", "ok": False, "detail": "skipped (gh missing)"})

    has_profile = profile_exists(WORKER_PROFILE)
    profile_dir = get_profile_dir(WORKER_PROFILE)
    checks.append({
        "name": "worker_profile",
        "ok": has_profile,
        "detail": str(profile_dir) if has_profile else "missing — run `hermes factonio init`",
    })

    if has_profile:
        link_path = profile_dir / "plugins" / PLUGIN_ID
        link_ok = link_path.exists()
        checks.append({
            "name": "worker_plugin_link",
            "ok": link_ok,
            "detail": str(link_path.resolve()) if link_ok else "missing — run `hermes factonio init`",
        })
        wcfg = _read_yaml(profile_dir / "config.yaml")
        enabled = ((wcfg.get("plugins") or {}).get("enabled") or []) if isinstance(wcfg, dict) else []
        checks.append({
            "name": "worker_plugin_enabled",
            "ok": PLUGIN_ID in enabled,
            "detail": "enabled" if PLUGIN_ID in enabled else "not enabled in factonio/config.yaml",
        })
        approvals_mode = ((wcfg.get("approvals") or {}).get("mode")) if isinstance(wcfg, dict) else None
        checks.append({
            "name": "worker_approvals_off",
            "ok": approvals_mode == "off",
            "detail": f"approvals.mode={approvals_mode!r}",
        })
    else:
        for name in ("worker_plugin_link", "worker_plugin_enabled", "worker_approvals_off"):
            checks.append({"name": name, "ok": False, "detail": "worker profile not initialized"})

    root_cfg = _read_yaml(get_root_hermes_home_path() / "config.yaml")
    raw_root_kb = root_cfg.get("kanban")
    kb: dict[str, Any] = raw_root_kb if isinstance(raw_root_kb, dict) else {}
    if kb.get("dispatch_in_gateway") is not True:
        warnings.append("Root config.yaml has kanban.dispatch_in_gateway != true; run `hermes factonio init --apply-config`.")
    if kb.get("auto_decompose") is not False:
        warnings.append("Root config.yaml has kanban.auto_decompose != false; Factonio manages its own DAG.")

    models = resolve_all_phase_models()
    cross_warn = check_cross_model_warning()
    if cross_warn:
        warnings.append(cross_warn)

    design_info = resolve_design_provider()
    if design_info["configured"] == "claude-design":
        checks.append({
            "name": "claude_design",
            "ok": bool(design_info["claude_available"]),
            "detail": design_info["reason"],
        })
    elif design_info["configured"] == "auto" and not design_info["claude_available"]:
        warnings.append(
            "Phase 1b design_provider=auto: Claude CLI/credentials not detected, will use pure-Hermes prototyping fallback."
        )

    return {
        "ok": all(c["ok"] for c in checks),
        "checks": checks,
        "warnings": warnings,
        "models": models,
        "design": design_info,
        "plugin_dir": str(plugin_dir.resolve()),
    }
