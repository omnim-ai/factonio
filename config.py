"""Factonio settings, phase model slots, and per-repository configuration."""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any, Mapping, Optional

PLUGIN_ID = "factonio"
WORKER_PROFILE = "factonio"

PHASES: tuple[str, ...] = ("define", "implement", "verify", "watchdog")

AUX_TASK_SPECS: tuple[dict[str, str], ...] = (
    {
        "key": "factonio_define",
        "phase": "define",
        "display_name": "Factonio: Definition",
        "description": "Codebase exploration, spec and task DAG authoring, bug root-cause diagnosis, and hidden holdout checks.",
    },
    {
        "key": "factonio_implement",
        "phase": "implement",
        "display_name": "Factonio: Implementation",
        "description": "Scoped coding, tests, PR creation, and bounded fix rounds inside isolated git worktrees.",
    },
    {
        "key": "factonio_verify",
        "phase": "verify",
        "display_name": "Factonio: Verification",
        "description": "Independent Diff Check and Black-Box Check on PRs (pick a different model/provider from Implementation).",
    },
    {
        "key": "factonio_watchdog",
        "phase": "watchdog",
        "display_name": "Factonio: Watchdog",
        "description": "Optional low-cost model for ambiguous stall triage when deterministic tick checks flag a stuck card.",
    },
)

DEFAULT_SETTINGS: dict[str, Any] = {
    "repos_root": "~/dev",
    "max_concurrent_workers": 4,
    "default_merge_policy": "auto",
    "default_max_fix_rounds": 3,
    "watchdog_interval_minutes": 5,
    "stale_heartbeat_minutes": 20,
    "blocked_reminder_minutes": 45,
    "repos": {},
}


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


def load_settings(*, use_root: bool = False) -> dict[str, Any]:
    """Load Factonio settings merged with defaults.

    When running inside the dedicated ``factonio`` worker profile, callers can pass
    ``use_root=True`` (or fall back automatically) so workers see the orchestrator's
    registered repositories and defaults stored in the root profile's config.yaml.
    """
    from hermes_cli.config import load_config_readonly

    raw_settings: dict[str, Any] = {}
    if use_root:
        root_cfg_path = get_root_hermes_home_path() / "config.yaml"
        raw_settings = _read_plugin_settings_from_file(root_cfg_path)
    else:
        try:
            cfg = load_config_readonly() or {}
            entry = ((cfg.get("plugins") or {}).get("entries") or {}).get(PLUGIN_ID) or {}
            settings = entry.get("settings")
            if isinstance(settings, dict):
                raw_settings = copy.deepcopy(settings)
        except Exception:
            raw_settings = {}
        if not raw_settings:
            root_cfg_path = get_root_hermes_home_path() / "config.yaml"
            raw_settings = _read_plugin_settings_from_file(root_cfg_path)

    merged = copy.deepcopy(DEFAULT_SETTINGS)
    for key, value in raw_settings.items():
        if key == "repos" and isinstance(value, dict):
            merged["repos"] = copy.deepcopy(value)
        elif value is not None:
            merged[key] = value
    return merged


def _read_plugin_settings_from_file(config_path: Path) -> dict[str, Any]:
    if not config_path.is_file():
        return {}
    try:
        import hermes_yaml as yaml

        data = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        if not isinstance(data, dict):
            return {}
        entry = ((data.get("plugins") or {}).get("entries") or {}).get(PLUGIN_ID) or {}
        settings = entry.get("settings")
        return copy.deepcopy(settings) if isinstance(settings, dict) else {}
    except Exception:
        return {}


def save_settings(updates: Mapping[str, Any], *, config_path: Optional[Path] = None) -> dict[str, Any]:
    """Persist Factonio settings into config.yaml under plugins.entries.factonio.settings."""
    import hermes_yaml as yaml

    target = config_path or (get_root_hermes_home_path() / "config.yaml")
    target.parent.mkdir(parents=True, exist_ok=True)
    data: dict[str, Any] = {}
    if target.is_file():
        loaded = yaml.safe_load(target.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            data = loaded

    plugins = data.setdefault("plugins", {})
    if not isinstance(plugins, dict):
        plugins = {}
        data["plugins"] = plugins
    entries = plugins.setdefault("entries", {})
    if not isinstance(entries, dict):
        entries = {}
        plugins["entries"] = entries
    entry = entries.setdefault(PLUGIN_ID, {})
    if not isinstance(entry, dict):
        entry = {}
        entries[PLUGIN_ID] = entry
    settings = entry.setdefault("settings", {})
    if not isinstance(settings, dict):
        settings = {}
        entry["settings"] = settings

    for key, value in updates.items():
        settings[key] = copy.deepcopy(value)

    target.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return load_settings(use_root=config_path is None)


def list_repos() -> dict[str, dict[str, Any]]:
    """Return registered repositories keyed by slug."""
    repos = load_settings().get("repos")
    return dict(repos) if isinstance(repos, dict) else {}


def get_repo(slug_or_full: str) -> Optional[dict[str, Any]]:
    """Find a registered repository by slug or owner/repo name."""
    needle = (slug_or_full or "").strip().lower()
    if not needle:
        return None
    repos = list_repos()
    if needle in repos:
        entry = dict(repos[needle])
        entry.setdefault("slug", needle)
        return entry
    for slug, info in repos.items():
        if not isinstance(info, dict):
            continue
        if str(info.get("repo") or "").strip().lower() == needle:
            entry = dict(info)
            entry.setdefault("slug", slug)
            return entry
    return None


def _profile_default_model(profile_name: str = WORKER_PROFILE) -> tuple[str, str]:
    """Read (provider, model) from a named profile's config.yaml, falling back to root."""
    import hermes_yaml as yaml

    candidates = [
        get_root_hermes_home_path() / "profiles" / profile_name / "config.yaml",
        get_root_hermes_home_path() / "config.yaml",
    ]
    for path in candidates:
        if not path.is_file():
            continue
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            model_cfg = data.get("model") if isinstance(data, dict) else None
            if isinstance(model_cfg, dict):
                provider = str(model_cfg.get("provider") or "").strip()
                model = str(model_cfg.get("default") or model_cfg.get("model") or "").strip()
                if model:
                    return provider, model
            elif isinstance(model_cfg, str) and model_cfg.strip():
                return "", model_cfg.strip()
        except Exception:
            continue
    return "", ""


def _read_aux_slot(task_key: str) -> tuple[str, str]:
    """Read configured (provider, model) for a Hermes auxiliary task slot."""
    try:
        from agent.auxiliary_task_config import _get_auxiliary_task_config

        cfg = _get_auxiliary_task_config(task_key) or {}
        provider = str(cfg.get("provider") or "").strip()
        model = str(cfg.get("model") or "").strip()
        if provider.lower() == "auto":
            provider = ""
        return provider, model
    except Exception:
        # Direct YAML fallback when auxiliary_task_config isn't hydrated (e.g. cold CLI tests).
        import hermes_yaml as yaml

        cfg_path = get_root_hermes_home_path() / "config.yaml"
        if cfg_path.is_file():
            try:
                data = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
                aux = (data.get("auxiliary") or {}).get(task_key) if isinstance(data, dict) else None
                if isinstance(aux, dict):
                    provider = str(aux.get("provider") or "").strip()
                    model = str(aux.get("model") or "").strip()
                    if provider.lower() == "auto":
                        provider = ""
                    return provider, model
            except Exception:
                pass
    return "", ""


def resolve_phase_model(
    phase: str,
    *,
    repo_slug: Optional[str] = None,
    subphase: Optional[str] = None,
) -> dict[str, Any]:
    """Resolve effective provider and model for a Factonio phase.

    Resolution order:
    1. Per-repo override (``repos.<slug>.models.<subphase>`` then ``<phase>``)
    2. Hermes auxiliary task slot (``auxiliary.factonio_<phase>``) when not ``auto``
    3. Fallback to the ``factonio`` worker profile default model (then root profile)
    """
    canon_phase = (phase or "").strip().lower()
    if canon_phase not in PHASES:
        raise ValueError(f"Unknown Factonio phase {phase!r} (expected one of {', '.join(PHASES)})")

    if repo_slug:
        repo = get_repo(repo_slug)
        models_map = repo.get("models") if repo is not None else None
        if repo is not None and isinstance(models_map, dict):
            resolved_slug = str(repo.get("slug") or repo_slug)
            for key in ([subphase, canon_phase] if subphase else [canon_phase]):
                override = models_map.get(key) if key else None
                if isinstance(override, dict) and str(override.get("model") or "").strip():
                    return {
                        "phase": canon_phase,
                        "provider": str(override.get("provider") or "").strip() or None,
                        "model": str(override.get("model") or "").strip(),
                        "source": f"repo:{resolved_slug}.models.{key}",
                    }
                if isinstance(override, str) and override.strip():
                    return {
                        "phase": canon_phase,
                        "provider": None,
                        "model": override.strip(),
                        "source": f"repo:{resolved_slug}.models.{key}",
                    }

    aux_key = f"factonio_{canon_phase}"
    aux_provider, aux_model = _read_aux_slot(aux_key)
    if aux_model:
        return {
            "phase": canon_phase,
            "provider": aux_provider or None,
            "model": aux_model,
            "source": f"auxiliary.{aux_key}",
        }

    default_provider, default_model = _profile_default_model(WORKER_PROFILE)
    return {
        "phase": canon_phase,
        "provider": default_provider or None,
        "model": default_model or None,
        "source": f"profile:{WORKER_PROFILE}" if default_model else "unset",
    }


def resolve_all_phase_models(*, repo_slug: Optional[str] = None) -> dict[str, dict[str, Any]]:
    """Return resolved model metadata for every Factonio phase."""
    return {phase: resolve_phase_model(phase, repo_slug=repo_slug) for phase in PHASES}


def check_cross_model_warning(*, repo_slug: Optional[str] = None) -> Optional[str]:
    """Return a non-blocking warning if Verification resolves to the same model as Implementation."""
    impl = resolve_phase_model("implement", repo_slug=repo_slug)
    verify = resolve_phase_model("verify", repo_slug=repo_slug)
    if not impl.get("model") or not verify.get("model"):
        return None
    if (impl.get("provider") or "", impl["model"]) == (verify.get("provider") or "", verify["model"]):
        label = f"{impl['provider']}/{impl['model']}" if impl.get("provider") else impl["model"]
        return (
            f"Verification uses the same model as Implementation ({label}). "
            "Configure `factonio_verify` in `hermes model` (Auxiliary Models) for cross-model independence."
        )
    return None
