"""GitHub repository registration, Hermes Project anchoring, and PR gate operations."""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Any, Optional

from .config import (
    DESIGN_APPROVAL_POLICIES,
    DESIGN_PROVIDERS,
    MERGE_POLICIES,
    get_repo,
    list_repos,
    load_settings,
    repos_store_lock,
    save_settings,
)

_GITHUB_URL_RE = re.compile(
    r"^(?:https?://github\.com/|git@github\.com:)([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?/?$"
)
_OWNER_REPO_RE = re.compile(r"^([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)$")
_SLUG_SAFE_RE = re.compile(r"[^a-z0-9-]+")


class RepoNotFoundError(LookupError):
    """Raised when a repository slug or `owner/repo` is not registered with Factonio."""


_UNSET: Any = object()

EDITABLE_REPO_FIELDS: tuple[str, ...] = (
    "merge_policy",
    "max_fix_rounds",
    "default_branch",
    "design_provider",
    "design_approval",
)

_INHERIT_TOKENS = frozenset({"", "inherit", "default"})


def _validate_merge_policy(value: Any) -> str:
    if isinstance(value, str):
        norm = value.strip().lower()
        if norm in MERGE_POLICIES:
            return norm
    raise ValueError(f"merge_policy must be one of {', '.join(MERGE_POLICIES)}; got {value!r}")


def _validate_max_fix_rounds(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"max_fix_rounds must be an integer >= 1; got {value!r}")
    if value < 1:
        raise ValueError(f"max_fix_rounds must be an integer >= 1; got {value!r}")
    return int(value)


def _validate_default_branch(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"default_branch must be a non-empty branch name; got {value!r}")
    branch = value.strip()
    if branch.startswith("-"):
        raise ValueError(f"default_branch must not start with '-'; got {value!r}")
    if ".." in branch:
        raise ValueError(f"default_branch must not contain '..'; got {value!r}")
    if any(ch.isspace() for ch in branch):
        raise ValueError(f"default_branch must not contain whitespace; got {value!r}")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in branch):
        raise ValueError(f"default_branch must not contain control characters; got {value!r}")
    return branch


def _validate_design_override(field: str, value: Any, allowed: tuple[str, ...]) -> Optional[str]:
    """Validate a per-repo design override; returns None when the override should be cleared."""
    if value is None:
        return None
    if isinstance(value, str):
        norm = value.strip().lower()
        if norm in _INHERIT_TOKENS:
            return None
        if norm in allowed:
            return norm
    raise ValueError(
        f"{field} must be one of {', '.join(allowed)} (or 'inherit'/null to use the global default); got {value!r}"
    )


def _validate_editable_fields(fields: dict[str, Any]) -> dict[str, Any]:
    """Validate supplied editable fields; returns normalized values (None = clear override)."""
    validated: dict[str, Any] = {}
    for field, value in fields.items():
        if field == "merge_policy":
            validated[field] = _validate_merge_policy(value)
        elif field == "max_fix_rounds":
            validated[field] = _validate_max_fix_rounds(value)
        elif field == "default_branch":
            validated[field] = _validate_default_branch(value)
        elif field == "design_provider":
            validated[field] = _validate_design_override(field, value, DESIGN_PROVIDERS)
        elif field == "design_approval":
            validated[field] = _validate_design_override(field, value, DESIGN_APPROVAL_POLICIES)
        else:
            raise ValueError(f"Unknown editable repository field {field!r}")
    return validated


def normalize_repo(repo_spec: str) -> tuple[str, str]:
    """Normalize `owner/repo` or GitHub URL into (`owner/repo`, `slug`)."""
    raw = (repo_spec or "").strip()
    m = _GITHUB_URL_RE.match(raw) or _OWNER_REPO_RE.match(raw)
    if not m:
        raise ValueError(
            f"Invalid GitHub repository {repo_spec!r}; expected 'owner/repo' or a GitHub HTTPS/SSH URL."
        )
    owner, name = m.group(1), m.group(2)
    full = f"{owner}/{name}"
    slug = _SLUG_SAFE_RE.sub("-", f"{owner}-{name}".lower()).strip("-")[:64]
    return full, slug


def _run_gh(args: list[str], *, cwd: Optional[Path] = None, timeout: int = 30) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["gh", *args],
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def inspect_github_repo(full_repo: str) -> dict[str, Any]:
    """Query default branch and required status checks via `gh api` (best-effort)."""
    default_branch = "main"
    required_checks: list[str] = []

    proc = _run_gh(["repo", "view", full_repo, "--json", "defaultBranchRef,url,isPrivate"])
    repo_url = f"https://github.com/{full_repo}"
    is_private: Optional[bool] = None
    if proc.returncode == 0 and proc.stdout.strip():
        try:
            data = json.loads(proc.stdout)
            branch_ref = data.get("defaultBranchRef")
            if isinstance(branch_ref, dict) and branch_ref.get("name"):
                default_branch = str(branch_ref["name"])
            if data.get("url"):
                repo_url = str(data["url"])
            if "isPrivate" in data:
                is_private = bool(data["isPrivate"])
        except ValueError:
            pass

    prot = _run_gh([
        "api",
        f"repos/{full_repo}/branches/{default_branch}/protection/required_status_checks",
    ])
    if prot.returncode == 0 and prot.stdout.strip():
        try:
            pdata = json.loads(prot.stdout)
            contexts = pdata.get("contexts") or []
            checks = pdata.get("checks") or []
            names = [str(c) for c in contexts if c] + [
                str(c.get("context")) for c in checks if isinstance(c, dict) and c.get("context")
            ]
            required_checks = list(dict.fromkeys(names))
        except ValueError:
            pass

    return {
        "repo": full_repo,
        "url": repo_url,
        "is_private": is_private,
        "default_branch": default_branch,
        "required_checks": required_checks,
        "has_required_checks": bool(required_checks),
    }


def _ensure_hermes_project(slug: str, full_repo: str, local_path: Path) -> Optional[str]:
    """Create or reuse the Hermes Project anchoring worktrees to `local_path`."""
    try:
        from hermes_cli import projects_db as pdb

        with pdb.connect_closing() as conn:
            existing = pdb.find_by_primary_path(conn, str(local_path))
            if existing is not None:
                return existing.slug
            by_slug = pdb.get_project(conn, slug)
            if by_slug is not None:
                return by_slug.slug
            pdb.create_project(
                conn,
                name=full_repo,
                slug=slug,
                primary_path=str(local_path),
                description=f"Factonio repository {full_repo}",
            )
            return slug
    except Exception:
        return None


def add_repo(
    repo_spec: str,
    *,
    local_path: Optional[str] = None,
    merge_policy: Optional[str] = None,
    max_fix_rounds: Optional[int] = None,
    clone: bool = True,
    inspect_remote: bool = True,
    default_branch: Optional[str] = None,
    design_provider: Any = _UNSET,
    design_approval: Any = _UNSET,
) -> dict[str, Any]:
    """Register a GitHub repository with Factonio and anchor a Hermes Project.

    Re-running on an already-registered repository preserves existing per-repo overrides
    (merge policy, fix rounds, default branch, design settings, CI contract, models)
    unless explicitly overridden by the caller.
    """
    full_repo, slug = normalize_repo(repo_spec)
    settings = load_settings(use_root=True)

    raw_existing = list_repos().get(slug)
    existing: dict[str, Any] = raw_existing if isinstance(raw_existing, dict) else {}

    # Validate every explicit argument before any side effects (clone, project, disk writes).
    if merge_policy is not None:
        policy = _validate_merge_policy(merge_policy)
    elif existing.get("merge_policy"):
        policy = str(existing["merge_policy"])
    else:
        policy = _validate_merge_policy(settings.get("default_merge_policy") or "auto")

    if max_fix_rounds is not None:
        rounds = _validate_max_fix_rounds(max_fix_rounds)
    elif existing.get("max_fix_rounds") is not None:
        rounds = existing["max_fix_rounds"]
    else:
        rounds = _validate_max_fix_rounds(int(settings.get("default_max_fix_rounds") or 3))

    explicit_branch = _validate_default_branch(default_branch) if default_branch is not None else None
    design_updates: dict[str, Optional[str]] = {}
    if design_provider is not _UNSET:
        design_updates["design_provider"] = _validate_design_override(
            "design_provider", design_provider, DESIGN_PROVIDERS
        )
    if design_approval is not _UNSET:
        design_updates["design_approval"] = _validate_design_override(
            "design_approval", design_approval, DESIGN_APPROVAL_POLICIES
        )

    repo_name = full_repo.split("/", 1)[1]
    if local_path:
        checkout = Path(local_path).expanduser().resolve()
    else:
        repos_root = Path(str(settings.get("repos_root") or "~/dev")).expanduser().resolve()
        checkout = repos_root / repo_name

    cloned = False
    if clone and not (checkout / ".git").exists():
        checkout.parent.mkdir(parents=True, exist_ok=True)
        proc = _run_gh(["repo", "clone", full_repo, str(checkout)], timeout=120)
        if proc.returncode != 0:
            err = (proc.stderr or proc.stdout or "").strip()
            raise RuntimeError(f"gh repo clone {full_repo} failed: {err}")
        cloned = True

    remote_info = (
        inspect_github_repo(full_repo)
        if inspect_remote
        else {
            "repo": full_repo,
            "url": f"https://github.com/{full_repo}",
            "default_branch": "main",
            "required_checks": [],
            "has_required_checks": False,
        }
    )

    project_slug = _ensure_hermes_project(slug, full_repo, checkout) if checkout.exists() else None

    # Re-read the live store under the transaction lock so concurrent writers that
    # landed during the (slow, unlocked) clone/inspect above are merged, not clobbered.
    with repos_store_lock():
        repos = list_repos()
        raw_existing = repos.get(slug)
        existing = raw_existing if isinstance(raw_existing, dict) else {}
        if merge_policy is None and existing.get("merge_policy"):
            policy = str(existing["merge_policy"])
        if max_fix_rounds is None and existing.get("max_fix_rounds") is not None:
            rounds = existing["max_fix_rounds"]
        entry = _build_added_entry(
            existing,
            slug=slug,
            full_repo=full_repo,
            remote_info=remote_info,
            checkout=checkout,
            project_slug=project_slug,
            explicit_branch=explicit_branch,
            policy=policy,
            rounds=rounds,
            design_updates=design_updates,
        )
        repos[slug] = entry
        save_settings({"repos": repos})

    return {
        "ok": True,
        "cloned": cloned,
        "repo": entry,
    }


def _build_added_entry(
    existing: dict[str, Any],
    *,
    slug: str,
    full_repo: str,
    remote_info: dict[str, Any],
    checkout: Path,
    project_slug: Optional[str],
    explicit_branch: Optional[str],
    policy: Any,
    rounds: Any,
    design_updates: dict[str, Optional[str]],
) -> dict[str, Any]:
    entry: dict[str, Any] = {
        **existing,
        "slug": slug,
        "repo": full_repo,
        "url": remote_info["url"],
        "path": str(checkout),
        "project_slug": project_slug or existing.get("project_slug") or slug,
        "default_branch": explicit_branch or existing.get("default_branch") or remote_info["default_branch"],
        "has_required_checks": bool(remote_info["has_required_checks"]),
        "required_checks": list(remote_info["required_checks"]),
        "merge_policy": policy,
        "max_fix_rounds": rounds,
        "ci_contract": existing.get("ci_contract") or "auto",
    }
    for field, value in design_updates.items():
        if value is None:
            entry.pop(field, None)
        else:
            entry[field] = value
    return entry


def update_repo(
    slug_or_full: str,
    *,
    merge_policy: Any = _UNSET,
    max_fix_rounds: Any = _UNSET,
    default_branch: Any = _UNSET,
    design_provider: Any = _UNSET,
    design_approval: Any = _UNSET,
) -> dict[str, Any]:
    """Update editable parameters of a registered repository (only fields explicitly supplied).

    All supplied values are validated before anything is written (all-or-nothing). The full
    entry (including fields that only lived in config.yaml) is persisted atomically to
    ``plugin-data/factonio/repos.json``. Passing ``None``/``""``/``"inherit"``/``"default"`` for
    ``design_provider`` or ``design_approval`` removes the per-repo override so the global
    default applies.
    """
    found = get_repo(slug_or_full)
    if found is None:
        raise RepoNotFoundError(f"Repository {slug_or_full!r} is not registered.")

    supplied = {
        "merge_policy": merge_policy,
        "max_fix_rounds": max_fix_rounds,
        "default_branch": default_branch,
        "design_provider": design_provider,
        "design_approval": design_approval,
    }
    supplied = {k: v for k, v in supplied.items() if v is not _UNSET}
    if not supplied:
        raise ValueError(
            "At least one editable field must be provided (" + ", ".join(EDITABLE_REPO_FIELDS) + ")."
        )
    validated = _validate_editable_fields(supplied)

    slug = str(found["slug"])
    # Serialize the whole read-modify-write: re-read the live store while holding the
    # cross-process lock and apply only the supplied fields to the fresh entry.
    with repos_store_lock():
        repos = list_repos()
        base = repos.get(slug)
        if not isinstance(base, dict):
            raise RepoNotFoundError(f"Repository {slug_or_full!r} is not registered.")
        entry: dict[str, Any] = dict(base)
        entry["slug"] = slug
        for field, value in validated.items():
            if field in ("design_provider", "design_approval") and value is None:
                entry.pop(field, None)
            else:
                entry[field] = value
        repos[slug] = entry
        save_settings({"repos": repos})

    return {"ok": True, "repo": entry, "updated_fields": sorted(validated)}


def remove_repo(slug_or_full: str) -> dict[str, Any]:
    """Remove a repository from Factonio's settings (does not delete local files)."""
    found = get_repo(slug_or_full)
    if found is None:
        return {"ok": False, "error": f"Repository {slug_or_full!r} is not registered."}
    slug = str(found["slug"])
    with repos_store_lock():
        repos = list_repos()
        repos.pop(slug, None)
        save_settings({"repos": repos})
    return {"ok": True, "removed": slug, "repo": found.get("repo")}


def inspect_pr_gate(repo_spec: str, pr_number: int) -> dict[str, Any]:
    """Inspect a GitHub PR's exact HEAD, draft/merge state, and CI check status via `gh`."""
    repo_entry = get_repo(repo_spec)
    full_repo = str(repo_entry["repo"]) if repo_entry else normalize_repo(repo_spec)[0]

    fields = (
        "number,url,title,state,isDraft,headRefName,headRefOid,baseRefName,"
        "mergeable,mergeStateStatus,statusCheckRollup"
    )
    proc = _run_gh(["pr", "view", str(int(pr_number)), "--repo", full_repo, "--json", fields])
    if proc.returncode != 0:
        return {
            "ok": False,
            "repo": full_repo,
            "pr_number": int(pr_number),
            "error": (proc.stderr or proc.stdout or f"gh exit {proc.returncode}").strip(),
        }

    data = json.loads(proc.stdout)
    rollup = data.get("statusCheckRollup") or []
    passed: list[str] = []
    failed: list[str] = []
    pending: list[str] = []

    for item in rollup:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or item.get("context") or "unnamed")
        status = str(item.get("status") or "").upper()
        conclusion = str(item.get("conclusion") or item.get("state") or "").upper()
        if conclusion in {"SUCCESS", "SKIPPED", "NEUTRAL"}:
            passed.append(name)
        elif conclusion in {"FAILURE", "ERROR", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED", "STARTUP_FAILURE"}:
            failed.append(f"{name} ({conclusion.lower()})")
        elif status in {"QUEUED", "IN_PROGRESS", "PENDING", "WAITING", "REQUESTED"} or conclusion in {"PENDING", ""}:
            pending.append(name)
        else:
            pending.append(f"{name} ({conclusion or status})")

    blockers: list[str] = []
    if data.get("state") != "OPEN":
        blockers.append(f"PR state is {data.get('state')}")
    if data.get("isDraft"):
        blockers.append("PR is still a draft")
    if str(data.get("mergeable") or "").upper() == "CONFLICTING":
        blockers.append("PR has merge conflicts")
    if failed:
        blockers.append("Failed checks: " + ", ".join(failed))
    if pending:
        blockers.append("Pending checks: " + ", ".join(pending))

    checks_green = not failed and not pending
    return {
        "ok": True,
        "repo": full_repo,
        "pr_number": int(data.get("number") or pr_number),
        "url": data.get("url"),
        "title": data.get("title"),
        "state": data.get("state"),
        "is_draft": bool(data.get("isDraft")),
        "head_branch": data.get("headRefName"),
        "head_sha": data.get("headRefOid"),
        "base_branch": data.get("baseRefName"),
        "mergeable": data.get("mergeable"),
        "merge_state_status": data.get("mergeStateStatus"),
        "checks": {"passed": passed, "failed": failed, "pending": pending},
        "checks_green": checks_green,
        "ready_to_merge": not blockers,
        "blockers": blockers,
    }


def merge_pr(
    repo_spec: str,
    pr_number: int,
    *,
    expected_head_sha: Optional[str] = None,
    method: str = "squash",
) -> dict[str, Any]:
    """Verify PR gates and merge via `gh pr merge` (refuses on stale HEAD or red checks)."""
    gate = inspect_pr_gate(repo_spec, pr_number)
    if not gate.get("ok"):
        return gate
    if expected_head_sha and gate.get("head_sha") != expected_head_sha:
        return {
            "ok": False,
            "merged": False,
            "error": f"HEAD mismatch: expected {expected_head_sha}, live PR is at {gate.get('head_sha')}",
            "gate": gate,
        }
    if not gate.get("ready_to_merge"):
        return {
            "ok": False,
            "merged": False,
            "error": "PR failed merge gate: " + "; ".join(gate.get("blockers") or []),
            "gate": gate,
        }

    flag = {"squash": "--squash", "merge": "--merge", "rebase": "--rebase"}.get(method, "--squash")
    args = ["pr", "merge", str(int(pr_number)), "--repo", str(gate["repo"]), flag, "--delete-branch"]
    if gate.get("head_sha"):
        args.extend(["--match-head-commit", str(gate["head_sha"])])

    proc = _run_gh(args, timeout=60)
    if proc.returncode != 0:
        return {
            "ok": False,
            "merged": False,
            "error": (proc.stderr or proc.stdout or f"gh merge exit {proc.returncode}").strip(),
            "gate": gate,
        }
    return {
        "ok": True,
        "merged": True,
        "repo": gate["repo"],
        "pr_number": gate["pr_number"],
        "url": gate["url"],
        "head_sha": gate["head_sha"],
        "method": method,
    }
