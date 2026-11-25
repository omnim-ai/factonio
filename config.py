"""Factonio settings, phase model slots, and per-repository configuration."""

from __future__ import annotations

import contextlib
import copy
import os
import tempfile
import threading
from pathlib import Path
from typing import Any, Iterator, Mapping, Optional

try:  # POSIX cross-process advisory locking; absent on Windows.
    import fcntl as _fcntl
except ImportError:  # pragma: no cover - non-POSIX fallback (in-process lock only)
    _fcntl = None  # type: ignore[assignment]

PLUGIN_ID = "factonio"
WORKER_PROFILE = "factonio"

PHASES: tuple[str, ...] = ("define", "design", "implement", "verify", "watchdog")

DESIGN_PROVIDERS: tuple[str, ...] = ("auto", "claude-design", "hermes", "off")
DESIGN_APPROVAL_POLICIES: tuple[str, ...] = ("ask", "auto")
MERGE_POLICIES: tuple[str, ...] = ("auto", "ask")

AUX_TASK_SPECS: tuple[dict[str, str], ...] = (
    {
        "key": "factonio_define",
        "phase": "define",
        "display_name": "Factonio: Definition",
        "description": "Codebase exploration, spec and task DAG authoring, bug root-cause diagnosis, and hidden holdout checks.",
    },
    {
        "key": "factonio_design",
        "phase": "design",
        "display_name": "Factonio: Design",
        "description": "Optional UI/UX design subphase (Phase 1b): supervises interactive Claude Design (/design) or generates pure-Hermes HTML/CSS prototypes.",
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
    "design_provider": "auto",
    "design_approval": "ask",
    "watchdog_interval_minutes": 5,
    "stale_heartbeat_minutes": 20,
    "blocked_reminder_minutes": 45,
    "repos": {},
}

DEFAULT_STALE_HEARTBEAT_MINUTES = 20

# Per-phase stale thresholds: verification cards are short-lived, design cards legitimately idle longer.
PHASE_STALE_HEARTBEAT_MINUTES: dict[str, int] = {
    "verify-diff": 10,
    "verify-blackbox": 15,
    "define": 20,
    "implement": 25,
    "design": 30,
}


def _positive_minutes(value: Any) -> Optional[int]:
    if value is None or isinstance(value, bool):
        return None
    try:
        minutes = int(value)
    except (TypeError, ValueError):
        return None
    return minutes if minutes > 0 else None


def resolve_stale_heartbeat_minutes(
    phase: Optional[str],
    *,
    stale_heartbeat_minutes: Optional[int] = None,
    phase_overrides: Optional[Mapping[str, Any]] = None,
) -> int:
    """Return the effective stale threshold (minutes) for a card phase.

    - ``phase_overrides[phase]`` (a positive int) wins outright.
    - Otherwise the phase default applies (``DEFAULT_STALE_HEARTBEAT_MINUTES`` for unknown phases).
    - A global ``stale_heartbeat_minutes`` above the default acts as a floor; below the default it
      acts as a cap; ``None`` or exactly the default leaves phase defaults untouched.
    """
    key = str(phase or "").strip().lower()
    if isinstance(phase_overrides, Mapping):
        override = _positive_minutes(phase_overrides.get(key))
        if override is not None:
            return override

    base = PHASE_STALE_HEARTBEAT_MINUTES.get(key, DEFAULT_STALE_HEARTBEAT_MINUTES)
    configured = _positive_minutes(stale_heartbeat_minutes)
    if configured is None or configured == DEFAULT_STALE_HEARTBEAT_MINUTES:
        return base
    if configured > DEFAULT_STALE_HEARTBEAT_MINUTES:
        return max(base, configured)
    return min(base, configured)


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


def _repos_store_path() -> Path:
    return get_root_hermes_home_path() / "plugin-data" / PLUGIN_ID / "repos.json"


def _read_repos_store() -> dict[str, dict[str, Any]]:
    import json

    path = _repos_store_path()
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return dict(data) if isinstance(data, dict) else {}
    except Exception:
        return {}


_REPOS_STORE_THREAD_LOCK = threading.RLock()
_REPOS_STORE_LOCK_STATE = threading.local()


def _repos_store_lock_path() -> Path:
    return _repos_store_path().with_name("repos.json.lock")


@contextlib.contextmanager
def repos_store_lock() -> Iterator[None]:
    """Serialize repository-store read-modify-write transactions.

    Acquires an in-process re-entrant lock plus an exclusive ``fcntl.flock`` on a sibling
    ``repos.json.lock`` file so writers in other threads *and* other processes are
    serialized. Re-entrant within the same thread: nested acquisitions reuse the outer
    lock (the lockfile is opened once per outermost acquisition so each holder has its
    own open file description, which is what makes ``flock`` exclude across processes).
    """
    with _REPOS_STORE_THREAD_LOCK:
        depth = getattr(_REPOS_STORE_LOCK_STATE, "depth", 0)
        if depth > 0:
            _REPOS_STORE_LOCK_STATE.depth = depth + 1
            try:
                yield
            finally:
                _REPOS_STORE_LOCK_STATE.depth -= 1
            return

        lock_path = _repos_store_lock_path()
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o644)
        try:
            if _fcntl is not None:
                _fcntl.flock(fd, _fcntl.LOCK_EX)
            _REPOS_STORE_LOCK_STATE.depth = 1
            try:
                yield
            finally:
                _REPOS_STORE_LOCK_STATE.depth = 0
                if _fcntl is not None:
                    _fcntl.flock(fd, _fcntl.LOCK_UN)
        finally:
            os.close(fd)


def save_repos_store(repos: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """Persist registered repositories to durable plugin-data/factonio/repos.json.

    Writes go to a unique temporary file in the same directory (fsynced) which then
    atomically replaces ``repos.json``; the whole write runs under ``repos_store_lock()``
    (re-entrant, so callers already holding the lock for a read-modify-write are fine).
    """
    import json

    payload = json.dumps(dict(repos), indent=2, sort_keys=True)
    with repos_store_lock():
        path = _repos_store_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path: Optional[str] = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=path.parent,
                prefix=".repos.",
                suffix=".tmp",
                delete=False,
            ) as fh:
                tmp_path = fh.name
                fh.write(payload)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp_path, path)
            tmp_path = None
        finally:
            if tmp_path is not None:
                with contextlib.suppress(OSError):
                    os.unlink(tmp_path)
        return _read_repos_store()


def save_settings(updates: Mapping[str, Any], *, config_path: Optional[Path] = None) -> dict[str, Any]:
    """Persist Factonio settings into config.yaml (and durable repos.json for `repos`)."""
    import hermes_yaml as yaml

    remaining = dict(updates)
    if "repos" in remaining and isinstance(remaining["repos"], Mapping) and config_path is None:
        save_repos_store(remaining.pop("repos"))
        if not remaining:
            return load_settings(use_root=True)

    target = config_path or (get_root_hermes_home_path() / "config.yaml")
    if target.is_symlink():
        target = target.resolve()
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

    for key, value in remaining.items():
        settings[key] = copy.deepcopy(value)

    target.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return load_settings(use_root=config_path is None)


def list_repos() -> dict[str, dict[str, Any]]:
    """Return registered repositories keyed by slug (config.yaml merged with plugin-data/repos.json).

    Always reads the root profile's settings so worker profiles see the same canonical
    store as the orchestrator. Returned dicts are deep copies, safe for callers to mutate.
    """
    from_cfg = load_settings(use_root=True).get("repos")
    merged: dict[str, dict[str, Any]] = copy.deepcopy(dict(from_cfg)) if isinstance(from_cfg, dict) else {}
    merged.update(copy.deepcopy(_read_repos_store()))
    return merged


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


def detect_claude_design_support() -> dict[str, Any]:
    """Detect whether Claude Code CLI and local auth credentials are present for `/design`."""
    import os
    import shutil

    claude_bin = shutil.which("claude")
    home = Path(os.environ.get("HOME") or str(Path.home())).expanduser()
    creds_path = home / ".claude" / ".credentials.json"
    has_creds = creds_path.is_file() or bool(os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"))
    return {
        "claude_bin": claude_bin,
        "has_credentials": has_creds,
        "available": bool(claude_bin and has_creds),
    }


def resolve_design_provider(
    *,
    repo_slug: Optional[str] = None,
    override: Optional[str] = None,
) -> dict[str, Any]:
    """Resolve the effective Phase 1b design provider and approval policy."""
    settings = load_settings(use_root=True)
    repo = get_repo(repo_slug) if repo_slug else None

    configured = (
        override
        or (repo.get("design_provider") if isinstance(repo, dict) else None)
        or settings.get("design_provider")
        or "auto"
    )
    configured = str(configured).strip().lower()
    if configured not in DESIGN_PROVIDERS:
        raise ValueError(
            f"Unsupported design_provider {configured!r} (expected one of {', '.join(DESIGN_PROVIDERS)})"
        )

    approval = (
        (repo.get("design_approval") if isinstance(repo, dict) else None)
        or settings.get("design_approval")
        or "ask"
    )
    approval = str(approval).strip().lower()
    if approval not in DESIGN_APPROVAL_POLICIES:
        approval = "ask"

    probe = detect_claude_design_support()
    if configured == "off":
        effective = "off"
        reason = "Phase 1b (Design) is disabled by design_provider=off."
    elif configured == "hermes":
        effective = "hermes"
        reason = "Configured to use pure-Hermes prototyping."
    elif configured == "claude-design":
        effective = "claude-design"
        reason = (
            "Claude Code CLI and credentials detected."
            if probe["available"]
            else "design_provider=claude-design requires `claude` CLI on PATH and Claude login (~/.claude/.credentials.json)."
        )
    else:
        if probe["available"]:
            effective = "claude-design"
            reason = "auto selected claude-design (claude CLI + credentials detected)."
        else:
            effective = "hermes"
            reason = "auto fell back to pure-Hermes prototyping (claude CLI or credentials not found)."

    return {
        "configured": configured,
        "effective": effective,
        "approval": approval,
        "claude_available": probe["available"],
        "claude_bin": probe["claude_bin"],
        "reason": reason,
    }


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
